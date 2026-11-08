import argparse
import datetime
import io
import logging
import re
import sqlite3
from contextlib import closing
from email.message import EmailMessage, Message, MIMEPart
from pathlib import Path
from typing import Literal, NamedTuple, overload

from bs4 import BeautifulSoup
from flask import Flask, Response, abort, current_app, g, render_template, request, send_file
from werkzeug.exceptions import HTTPException, SecurityError
from werkzeug.utils import secure_filename

from mbox_stats import database
from mbox_stats.mbox_file import compression_of, open_mbox_file

logger = logging.getLogger(__name__)
PAGE_SIZE = 50
APP_CSP = (
    "default-src 'none'; style-src 'self'; frame-src 'self'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)
EMAIL_CSP = (
    "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; "
    "script-src 'none'; connect-src 'none'; object-src 'none'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
)


def _connect(uri: str) -> sqlite3.Connection:
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _connection() -> sqlite3.Connection:
    if 'database' not in g:
        g.database = _connect(current_app.config['DATABASE_URI'])
    return g.database


def create_app(db_path: str, mbox_path: str) -> Flask:
    db_file = Path(db_path).expanduser().resolve()
    mbox_file = Path(mbox_path).expanduser().resolve()
    for path in (db_file, mbox_file):
        if not path.is_file():
            raise ValueError(f'File not found: {path}')

    uri = db_file.as_uri() + '?mode=ro'
    with closing(_connect(uri)) as conn:
        try:
            meta = database.read_meta(conn)
            if meta.get('schema_version') != str(database.SCHEMA_VERSION):
                raise ValueError('Unsupported database schema; rebuild the index.')
            conn.execute(f"SELECT {', '.join(database.MESSAGE_COLUMNS)} FROM messages LIMIT 0")
            conn.execute('SELECT message_id, kind, name, address FROM recipients LIMIT 0')
            conn.execute('SELECT id, name FROM labels LIMIT 0')
            conn.execute('SELECT message_id, label_id FROM message_labels LIMIT 0')
            conn.execute('SELECT message_id, filename, content_type, size FROM attachments LIMIT 0')
            conn.execute("SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'startupcheck' LIMIT 0")
        except sqlite3.DatabaseError as exc:
            raise ValueError('Not a compatible email database; rebuild the index.') from exc
        if meta.get('mbox_size') != str(mbox_file.stat().st_size):
            raise ValueError('Mbox size differs from the indexed file; rebuild the index.')
        for warning in database.check_mbox(conn, str(mbox_file)):
            logger.warning(warning)

    with open_mbox_file(str(mbox_file)) as handle:
        handle.read(1)
    if compression_of(str(mbox_file)):
        logger.warning('Compressed mbox: viewing messages may be slow due to decompression.')

    app = Flask(__name__)
    app.config.update(
        DATABASE_URI=uri,
        MBOX_PATH=str(mbox_file),
        TRUSTED_HOSTS=['localhost', '127.0.0.1'],
    )

    @app.teardown_appcontext
    def close_database(error):
        conn = g.pop('database', None)
        if conn is not None:
            conn.close()

    _register_routes(app)
    return app


def _search_parameters() -> tuple[str, int]:
    query = request.args.get('q', '').strip()
    try:
        page = int(request.args.get('page', '1'))
    except ValueError:
        abort(400, description='Invalid page number.')
    if not 1 <= page <= 1_000_000_000:
        abort(400, description='Invalid page number.')
    if len(query) > 2000:
        abort(400, description='Search query is too long.')
    return query, page


class SearchExpressions(NamedTuple):
    included: str
    excluded: str
    date_filters: list[tuple[str, int, bool]]


def _search_expressions(query: str) -> SearchExpressions:
    fields = {
        'label': 'labels', 'labels': 'labels',
        'from': '{from_name from_addr}', 'from_name': 'from_name', 'from_addr': 'from_addr',
        'to': 'recipients', 'recipients': 'recipients',
        'subject': 'subject', 'preview': 'preview',
    }
    included = []
    excluded = []
    date_filters = []
    end = 0
    for match in re.finditer(r'(?:[^\s"]|"(?:""|[^"])*")+', query):
        if query[end:match.start()].strip():
            abort(400, description='Invalid search expression.')
        end = match.end()
        term = match.group()
        negative = term.startswith('-')
        if negative:
            term = term[1:]
        field = ''
        if ':' in term and not term.startswith('"'):
            name, _, term = term.partition(':')
            if name.lower() in ('before', 'after'):
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', term):
                    abort(400, description='Invalid search expression.')
                try:
                    date = datetime.date.fromisoformat(term)
                except ValueError:
                    abort(400, description='Invalid search expression.')
                timestamp = int(datetime.datetime.combine(
                    date, datetime.time.min, datetime.timezone.utc).timestamp())
                date_filters.append((name.lower(), timestamp, negative))
                continue
            field = fields.get(name.lower(), '')
            if not field:
                abort(400, description='Invalid search expression.')
        if term.startswith('"') and term.endswith('"') and len(term) >= 2:
            term = term[1:-1].replace('""', '"')
        elif '"' in term:
            abort(400, description='Invalid search expression.')
        if not term:
            abort(400, description='Invalid search expression.')
        expression = '"' + term.replace('"', '""') + '"'
        if field:
            expression = f'{field} : {expression}'
        (excluded if negative else included).append(expression)
    if query[end:].strip():
        abort(400, description='Invalid search expression.')
    return SearchExpressions(' AND '.join(included), ' OR '.join(excluded), date_filters)


@overload
def _load_message(message_id: int, raw: Literal[False] = False) -> EmailMessage: ...


@overload
def _load_message(message_id: int, raw: Literal[True]) -> bytes: ...


def _load_message(message_id: int, raw: bool = False) -> EmailMessage | bytes:
    try:
        if raw:
            return database.get_raw_message(_connection(), message_id, current_app.config['MBOX_PATH'])
        return database.get_message(_connection(), message_id, current_app.config['MBOX_PATH'])
    except KeyError:
        abort(404, description='Message not found.')
    except (OSError, ValueError, EOFError, ImportError):
        current_app.logger.exception('Unable to recover message %s', message_id)
        abort(409, description='The message could not be read from the matching mbox. Check or rebuild the archive index.')


def _body_text(message: EmailMessage) -> str:
    body = message.get_body(preferencelist=('plain', 'html'))
    if body is None:
        return ''
    text = database._part_text(body)
    if body.get_content_subtype() == 'html':
        soup = BeautifulSoup(text, 'html.parser')
        for element in soup(['script', 'style']):
            element.decompose()
        return soup.get_text('\n', strip=True)
    return text


def _protected_html(text: str) -> str:
    soup = BeautifulSoup(text, 'html.parser')
    for element in soup(['script', 'form', 'iframe', 'object', 'embed', 'base', 'meta', 'link',
                         'frame', 'frameset', 'applet', 'svg', 'math', 'audio', 'video', 'source',
                         'input', 'button', 'textarea', 'select']):
        element.decompose()
    allowed_attributes = {'style', 'class', 'id', 'title', 'dir', 'lang', 'colspan', 'rowspan',
                          'width', 'height', 'align', 'valign', 'bgcolor', 'alt'}
    for element in soup.find_all(True):
        for attribute in list(element.attrs):
            if attribute.lower() not in allowed_attributes:
                del element.attrs[attribute]
    return str(soup)


def _attachment_bytes(part: MIMEPart) -> bytes:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    nested = part.get_payload()
    if isinstance(nested, list):
        return b'\n'.join(message.as_bytes() for message in nested if isinstance(message, Message))
    return part.as_bytes()


def _download(data: bytes, filename: str) -> Response:
    return send_file(io.BytesIO(data), mimetype='application/octet-stream',
                     as_attachment=True, download_name=filename)


def _register_routes(app: Flask):
    @app.context_processor
    def search_reference():
        labels = _connection().execute('SELECT name FROM labels ORDER BY name COLLATE NOCASE, name').fetchall()
        return {'search_labels': [label['name'] for label in labels]}

    @app.after_request
    def response_headers(response):
        response.headers.setdefault('Content-Security-Policy', APP_CSP)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @app.errorhandler(HTTPException)
    def http_error(error):
        response = error.get_response()
        if isinstance(error, SecurityError):
            return response
        response.data = render_template('email_error.html', error=error, q='')
        response.content_type = 'text/html; charset=utf-8'
        return response

    @app.template_filter('email_date')
    def email_date(timestamp):
        if timestamp is None:
            return 'Unknown date'
        try:
            return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
        except (ValueError, OverflowError, OSError):
            return 'Unknown date'

    @app.get('/')
    def search():
        query, page = _search_parameters()
        conn = _connection()
        source = 'messages m'
        parameters = []
        expressions = _search_expressions(query)
        if query:
            try:
                for expression in (expressions.included, expressions.excluded):
                    if expression:
                        conn.execute('SELECT rowid FROM messages_fts WHERE messages_fts MATCH ? LIMIT 1',
                                     (expression,)).fetchone()
            except sqlite3.OperationalError as exc:
                if any(marker in str(exc).lower() for marker in
                       ('fts5:', 'unterminated string', 'no such column')):
                    abort(400, description='Invalid search expression.')
                raise
            conditions = []
            for expression, operator in ((expressions.included, 'IN'), (expressions.excluded, 'NOT IN')):
                if expression:
                    conditions.append(f'm.id {operator} (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ?)')
                    parameters.append(expression)
            for operator, timestamp, negative in expressions.date_filters:
                if operator == 'before':
                    condition = 'm.date_utc >= ?' if negative else 'm.date_utc < ?'
                else:
                    condition = 'm.date_utc < ?' if negative else 'm.date_utc >= ?'
                if negative:
                    condition = f'(m.date_utc IS NULL OR {condition})'
                conditions.append(condition)
                parameters.append(timestamp)
            if conditions:
                source += ' WHERE ' + ' AND '.join(conditions)
        total = conn.execute(f'SELECT count(*) FROM {source}', parameters).fetchone()[0]
        messages = conn.execute(
            f'SELECT m.* FROM {source} ORDER BY m.date_utc DESC, m.id DESC LIMIT ? OFFSET ?',
            (*parameters, PAGE_SIZE, (page - 1) * PAGE_SIZE),
        ).fetchall()
        return render_template('search.html', messages=messages, total=total, q=query,
                               page=page, pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
                               included=expressions.included, excluded=expressions.excluded)

    @app.get('/messages/<int:message_id>')
    def message_detail(message_id):
        query, page = _search_parameters()
        view = request.args.get('view', 'plain')
        if view not in ('plain', 'html'):
            abort(400, description='Invalid message view.')
        message = _load_message(message_id)
        conn = _connection()
        row = conn.execute('SELECT * FROM messages WHERE id = ?', (message_id,)).fetchone()
        recipients = conn.execute('SELECT kind, name, address FROM recipients WHERE message_id = ?',
                                  (message_id,)).fetchall()
        labels = conn.execute('SELECT l.name FROM labels l JOIN message_labels ml ON l.id = ml.label_id '
                              'WHERE ml.message_id = ? ORDER BY l.name', (message_id,)).fetchall()
        attachments = []
        for index, part in enumerate(message.iter_attachments()):
            attachments.append({'index': index, 'name': part.get_filename() or f'attachment-{index + 1}',
                                'type': part.get_content_type()})
        return render_template('message.html', message=row, recipients=recipients, labels=labels,
                               attachments=attachments, body=_body_text(message),
                               has_html=message.get_body(preferencelist=('html',)) is not None,
                               view=view, q=query, page=page)

    @app.get('/messages/<int:message_id>/html')
    def message_html(message_id):
        message = _load_message(message_id)
        body = message.get_body(preferencelist=('html',))
        if body is None:
            abort(404, description='This message has no HTML body.')
        return Response(_protected_html(database._part_text(body)), content_type='text/html; charset=utf-8',
                        headers={'Content-Security-Policy': EMAIL_CSP})

    @app.get('/messages/<int:message_id>/raw')
    def message_raw(message_id):
        return _download(_load_message(message_id, raw=True), f'message-{message_id}.eml')

    @app.get('/messages/<int:message_id>/attachments/<int:attachment_index>')
    def attachment(message_id, attachment_index):
        message = _load_message(message_id)
        parts = list(message.iter_attachments())
        if attachment_index >= len(parts):
            abort(404, description='Attachment not found.')
        part = parts[attachment_index]
        filename = secure_filename(part.get_filename() or '') or f'attachment-{attachment_index + 1}'
        return _download(_attachment_bytes(part), filename)


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('Port must be an integer.') from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError('Port must be between 1 and 65535.')
    return port


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description='Search and view an indexed mbox in a local browser.')
    parser.add_argument('--db', required=True, help='Existing SQLite email index')
    parser.add_argument('--mbox', required=True, help='Matching mbox file (optionally compressed)')
    parser.add_argument('--port', type=_port, default=5000, help='Local port (default: 5000)')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    try:
        app = create_app(args.db, args.mbox)
    except (OSError, ValueError, sqlite3.DatabaseError, ImportError, EOFError) as exc:
        parser.error(str(exc))
    app.run(host='127.0.0.1', port=args.port, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()