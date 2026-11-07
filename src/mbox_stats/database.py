import datetime
import email
import hashlib
import io
import logging
import os
import sqlite3
import time
from bs4 import BeautifulSoup
from email.message import EmailMessage, MIMEPart
from typing import Any, Iterable, overload
from email.policy import default
from email.utils import getaddresses, parsedate_to_datetime

from mbox_stats.mbox_file import compression_of, open_mbox_file
from mbox_stats.mbox_reader import MboxReader, unescape_from_line
from mbox_stats.stats import decode_mime_header_value, human_readable_size

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    mbox_offset INTEGER NOT NULL,
    mbox_length INTEGER NOT NULL,
    size INTEGER NOT NULL,
    sha1 TEXT NOT NULL,
    message_id TEXT,
    date_utc INTEGER,
    date_raw TEXT,
    subject TEXT,
    from_name TEXT,
    from_addr TEXT,
    from_domain TEXT,
    in_reply_to TEXT,
    references_hdr TEXT,
    gm_thread_id TEXT,
    content_type TEXT,
    attachment_count INTEGER NOT NULL DEFAULT 0,
    attachment_size INTEGER NOT NULL DEFAULT 0,
    preview TEXT,
    parse_error TEXT
);

CREATE TABLE recipients (
    message_id INTEGER NOT NULL REFERENCES messages(id),
    kind TEXT NOT NULL,
    name TEXT,
    address TEXT,
    domain TEXT
);

CREATE TABLE labels (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE message_labels (
    message_id INTEGER NOT NULL REFERENCES messages(id),
    label_id INTEGER NOT NULL REFERENCES labels(id),
    PRIMARY KEY (message_id, label_id)
) WITHOUT ROWID;

CREATE TABLE attachments (
    message_id INTEGER NOT NULL REFERENCES messages(id),
    filename TEXT,
    content_type TEXT,
    size INTEGER NOT NULL
);

CREATE VIEW messages_fts_source AS
SELECT
    m.id AS id,
    m.subject AS subject,
    m.from_name AS from_name,
    m.from_addr AS from_addr,
    (SELECT group_concat(coalesce(r.name, '') || ' ' || coalesce(r.address, ''), ' ')
       FROM recipients r WHERE r.message_id = m.id) AS recipients,
    (SELECT group_concat(l.name, ' ')
       FROM message_labels ml JOIN labels l ON l.id = ml.label_id
      WHERE ml.message_id = m.id) AS labels,
    m.preview AS preview
FROM messages m;

CREATE VIRTUAL TABLE messages_fts USING fts5(
    subject, from_name, from_addr, recipients, labels, preview,
    content='messages_fts_source', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
"""

# Created after the bulk load; much faster than maintaining them during inserts.
INDEXES = """
CREATE INDEX idx_messages_date ON messages(date_utc);
CREATE INDEX idx_messages_from_addr ON messages(from_addr);
CREATE INDEX idx_messages_from_domain ON messages(from_domain);
CREATE INDEX idx_messages_size ON messages(size);
CREATE INDEX idx_messages_message_id ON messages(message_id);
CREATE INDEX idx_messages_thread ON messages(gm_thread_id);
CREATE INDEX idx_recipients_message ON recipients(message_id);
CREATE INDEX idx_recipients_address ON recipients(address);
CREATE INDEX idx_recipients_domain ON recipients(domain);
CREATE INDEX idx_message_labels_label ON message_labels(label_id, message_id);
CREATE INDEX idx_attachments_message ON attachments(message_id);
"""

MESSAGE_COLUMNS = (
    'id', 'mbox_offset', 'mbox_length', 'size', 'sha1',
    'message_id', 'date_utc', 'date_raw', 'subject', 'from_name', 'from_addr', 'from_domain',
    'in_reply_to', 'references_hdr', 'gm_thread_id', 'content_type',
    'attachment_count', 'attachment_size', 'preview', 'parse_error',
)

_INSERT_MESSAGE = (
    f"INSERT INTO messages ({', '.join(MESSAGE_COLUMNS)}) "
    f"VALUES ({', '.join('?' * len(MESSAGE_COLUMNS))})"
)

_MAX_PREVIEW_SOURCE = 1_000_000


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    # Undecodable header bytes surface as lone surrogates, which sqlite3 refuses to store.
    value = ' '.join(value.encode('utf-8', 'replace').decode('utf-8').split())
    return value or None


def _header_all(msg: EmailMessage, name: str) -> list[str]:
    try:
        values = [_clean(str(v)) for v in (msg.get_all(name) or [])]
    except Exception:
        values = []
        for key, raw in msg.raw_items():
            if key.lower() == name.lower():
                try:
                    values.append(_clean(decode_mime_header_value(raw)))
                except Exception:
                    values.append(_clean(str(raw)))
    return [v for v in values if v]


def _header(msg: EmailMessage, name: str) -> str | None:
    values = _header_all(msg, name)
    return values[0] if values else None


def _addresses(values: list[str]) -> list[tuple[str | None, str | None, str | None]]:
    result = []
    for name, addr in getaddresses(values):
        name = _clean(name)
        addr = _clean(addr)
        addr = addr.lower() if addr else None
        if not name and not addr:
            continue
        domain = addr.rpartition('@')[2] if addr and '@' in addr else None
        result.append((name, addr, domain))
    return result


def _parse_date(value: str | None) -> int | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp())
    except (TypeError, ValueError, IndexError, OverflowError, OSError):
        return None


def _part_text(part: MIMEPart) -> str:
    try:
        return part.get_content()
    except Exception:
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            payload = b''
        try:
            return payload.decode(part.get_content_charset() or 'utf-8', errors='replace')
        except LookupError:
            return payload.decode('utf-8', errors='replace')


def _preview(msg: EmailMessage, length: int) -> str | None:
    if length <= 0:
        return None
    body = msg.get_body(preferencelist=('plain', 'html'))
    if body is None:
        return None
    text = _part_text(body)[:_MAX_PREVIEW_SOURCE]
    if body.get_content_subtype() == 'html':
        soup = BeautifulSoup(text, 'html.parser')
        for element in soup(['script', 'style']):
            element.decompose()
        text = soup.get_text(' ', strip=True)
    # Over-fetch before collapsing whitespace so the result still fills `length` chars.
    text = _clean(text[:length * 4])
    return text[:length] if text else None


def _attachments(msg: EmailMessage) -> list[tuple[str | None, str, int]]:
    result = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if part.get_content_disposition() != 'attachment' and not filename:
            continue
        payload = part.get_payload(decode=True)
        result.append((_clean(filename), part.get_content_type(), len(payload) if payload else 0))
    return result


def extract_message(raw: bytes, preview_length: int):
    """Return (row, recipients, labels, attachments) for a raw RFC 822 message."""
    row: dict[str, Any] = dict.fromkeys(MESSAGE_COLUMNS)
    row['attachment_count'] = 0
    row['attachment_size'] = 0
    recipients: list[tuple[str, str | None, str | None, str | None]] = []
    labels: list[str] = []
    attachments: list[tuple[str | None, str, int]] = []
    errors = []

    try:
        msg = email.message_from_bytes(raw, policy=default)
    except Exception as e:
        row['parse_error'] = f'parse: {type(e).__name__}: {e}'
        return row, recipients, labels, attachments

    try:
        row['message_id'] = _header(msg, 'Message-ID')
        row['date_raw'] = _header(msg, 'Date')
        row['date_utc'] = _parse_date(row['date_raw'])
        row['subject'] = _header(msg, 'Subject')
        senders = _addresses(_header_all(msg, 'From'))
        if senders:
            row['from_name'], row['from_addr'], row['from_domain'] = senders[0]
        row['in_reply_to'] = _header(msg, 'In-Reply-To')
        row['references_hdr'] = _header(msg, 'References')
        row['gm_thread_id'] = _header(msg, 'X-GM-THRID')
        row['content_type'] = msg.get_content_type()
        for kind in ('To', 'Cc', 'Bcc'):
            for name, addr, domain in _addresses(_header_all(msg, kind)):
                recipients.append((kind.lower(), name, addr, domain))
        gmail_labels = _header(msg, 'X-Gmail-Labels')
        if gmail_labels:
            labels = list(dict.fromkeys(l.strip() for l in gmail_labels.split(',') if l.strip()))
    except Exception as e:
        errors.append(f'headers: {type(e).__name__}: {e}')

    try:
        attachments = _attachments(msg)
        row['attachment_count'] = len(attachments)
        row['attachment_size'] = sum(a[2] for a in attachments)
    except Exception as e:
        errors.append(f'attachments: {type(e).__name__}: {e}')

    try:
        row['preview'] = _preview(msg, preview_length)
    except Exception as e:
        errors.append(f'preview: {type(e).__name__}: {e}')

    if errors:
        row['parse_error'] = '; '.join(errors)
    return row, recipients, labels, attachments


def _load_messages(conn: sqlite3.Connection, mbox_path: str, preview_length: int,
                   batch_size: int, progress_interval: int) -> int:
    total_size = os.path.getsize(mbox_path) if compression_of(mbox_path) is None else None
    label_ids: dict[str, int] = {}
    messages, recipients, label_rows, message_labels, attachments = [], [], [], [], []

    def flush():
        conn.execute('BEGIN')
        conn.executemany(_INSERT_MESSAGE, messages)
        conn.executemany('INSERT INTO recipients VALUES (?, ?, ?, ?, ?)', recipients)
        conn.executemany('INSERT INTO labels VALUES (?, ?)', label_rows)
        conn.executemany('INSERT INTO message_labels VALUES (?, ?)', message_labels)
        conn.executemany('INSERT INTO attachments VALUES (?, ?, ?, ?)', attachments)
        conn.execute('COMMIT')
        for batch in (messages, recipients, label_rows, message_labels, attachments):
            batch.clear()

    start = time.monotonic()
    count = 0
    with open_mbox_file(mbox_path) as fh:
        for count, (offset, raw_length, raw) in enumerate(MboxReader(fh).iter_with_offsets(), start=1):
            row, msg_recipients, msg_labels, msg_attachments = extract_message(raw, preview_length)
            row.update(id=count, mbox_offset=offset, mbox_length=raw_length, size=len(raw),
                       sha1=hashlib.sha1(raw).hexdigest())
            messages.append(tuple(row[c] for c in MESSAGE_COLUMNS))
            recipients.extend((count, *r) for r in msg_recipients)
            attachments.extend((count, *a) for a in msg_attachments)
            for label in msg_labels:
                label_id = label_ids.get(label)
                if label_id is None:
                    label_id = label_ids[label] = len(label_ids) + 1
                    label_rows.append((label_id, label))
                message_labels.append((count, label_id))

            if len(messages) >= batch_size:
                flush()
            if count % progress_interval == 0:
                elapsed = time.monotonic() - start
                progress = f'{human_readable_size(offset)}'
                if total_size:
                    progress += f' / {human_readable_size(total_size)} ({100 * offset / total_size:.1f}%)'
                logger.info(f'{count} messages, {progress}, {count / elapsed:.0f} msg/s')
    flush()
    return count


def _write_meta(conn: sqlite3.Connection, mbox_path: str, count: int, preview_length: int):
    st = os.stat(mbox_path)
    meta = {
        'schema_version': SCHEMA_VERSION,
        'mbox_path': os.path.abspath(mbox_path),
        'mbox_size': st.st_size,
        'mbox_mtime': st.st_mtime_ns,
        'compression': compression_of(mbox_path) or '',
        'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'message_count': count,
        'preview_length': preview_length,
    }
    conn.executemany('INSERT INTO meta VALUES (?, ?)', [(k, str(v)) for k, v in meta.items()])


def build_database(mbox_path: str, db_path: str, preview_length: int = 500,
                   batch_size: int = 1000, progress_interval: int = 10000) -> int:
    """Build a metadata index of mbox_path at db_path, replacing it only on success."""
    tmp_path = db_path + '.tmp'
    if os.path.exists(tmp_path):
        os.remove(tmp_path)

    conn = sqlite3.connect(tmp_path, isolation_level=None)
    try:
        conn.execute('PRAGMA journal_mode=OFF')
        conn.execute('PRAGMA synchronous=OFF')
        conn.execute('PRAGMA temp_store=MEMORY')
        conn.execute('PRAGMA cache_size=-262144')
        conn.executescript(SCHEMA)

        start = time.monotonic()
        count = _load_messages(conn, mbox_path, preview_length, batch_size, progress_interval)
        logger.info(f'Loaded {count} messages in {time.monotonic() - start:.0f}s, creating indexes')
        conn.executescript(INDEXES)
        logger.info('Building full-text index')
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        _write_meta(conn, mbox_path, count, preview_length)
        conn.execute('ANALYZE')
        conn.close()
    except BaseException:
        conn.close()
        os.remove(tmp_path)
        raise

    os.replace(tmp_path, db_path)
    return count


def read_meta(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute('SELECT key, value FROM meta').fetchall())


def check_mbox(conn: sqlite3.Connection, mbox_path: str | None = None) -> list[str]:
    """Return warnings if the mbox file no longer matches the one that was indexed."""
    meta = read_meta(conn)
    mbox_path = mbox_path or meta['mbox_path']
    if not os.path.exists(mbox_path):
        return [f'mbox file not found: {mbox_path}']
    st = os.stat(mbox_path)
    warnings = []
    if str(st.st_size) != meta.get('mbox_size'):
        warnings.append(f'mbox size changed: indexed {meta.get("mbox_size")}, now {st.st_size}')
    if str(st.st_mtime_ns) != meta.get('mbox_mtime'):
        warnings.append('mbox modification time changed since indexing')
    return warnings


@overload
def get_raw_message(conn: sqlite3.Connection, message_id: int,
                    mbox_path: str | None = None) -> bytes: ...


@overload
def get_raw_message(conn: sqlite3.Connection, message_id: Iterable[int],
                    mbox_path: str | None = None) -> list[bytes]: ...


def get_raw_message(conn: sqlite3.Connection, message_id: int | Iterable[int],
                    mbox_path: str | None = None) -> bytes | list[bytes]:
    """Read one message or an iterable of messages back from the mbox."""
    single = isinstance(message_id, int)
    message_ids = [message_id] if single else list(message_id)
    if not message_ids:
        return []

    rows = {}
    unique_ids = list(dict.fromkeys(message_ids))
    for start in range(0, len(unique_ids), 900):
        batch = unique_ids[start:start + 900]
        placeholders = ', '.join('?' for _ in batch)
        rows.update((row[0], row[1:]) for row in conn.execute(
            f'SELECT id, mbox_offset, mbox_length, sha1 FROM messages WHERE id IN ({placeholders})', batch
        ))
    for requested_id in message_ids:
        if requested_id not in rows:
            raise KeyError(requested_id)

    mbox_path = mbox_path or read_meta(conn)['mbox_path']
    raw_by_id = {}
    with open_mbox_file(mbox_path) as fh:
        for requested_id, (offset, length, sha1) in sorted(
            rows.items(), key=lambda item: item[1][0]
        ):
            fh.seek(offset)
            data = fh.read(length)
            raw = b''.join(unescape_from_line(line) for line in io.BytesIO(data))
            if hashlib.sha1(raw).hexdigest() != sha1:
                raise ValueError(
                    f'Message {requested_id}: checksum mismatch, mbox file differs from the indexed one'
                )
            raw_by_id[requested_id] = raw

    if single:
        return raw_by_id[message_ids[0]]
    return [raw_by_id[requested_id] for requested_id in message_ids]


@overload
def get_message(conn: sqlite3.Connection, message_id: int,
                mbox_path: str | None = None) -> EmailMessage: ...


@overload
def get_message(conn: sqlite3.Connection, message_id: Iterable[int],
                mbox_path: str | None = None) -> list[EmailMessage]: ...


def get_message(conn: sqlite3.Connection, message_id: int | Iterable[int],
                mbox_path: str | None = None) -> EmailMessage | list[EmailMessage]:
    raw_messages = get_raw_message(conn, message_id, mbox_path)
    if isinstance(raw_messages, bytes):
        return email.message_from_bytes(raw_messages, policy=default)
    return [email.message_from_bytes(raw, policy=default) for raw in raw_messages]
