import gzip
import os
import sqlite3
from email.message import EmailMessage

import pytest
from bs4 import BeautifulSoup

from mbox_stats import database, server


@pytest.fixture
def archive(tmp_path):
    mbox = tmp_path / 'mail #1.mbox'
    db = tmp_path / 'mail #1.sqlite'
    mbox.write_bytes(
        b'From sender@example.com Sat Jan  1 00:00:00 2022\n'
        b'From: Alice <sender@example.com>\nTo: Bob <bob@example.com>\n'
        b'Date: Sat, 1 Jan 2022 00:00:00 +0000\n'
        b'Subject: First message\nX-Gmail-Labels: Important\n\nFirst body\n'
    )
    database.build_database(str(mbox), str(db))
    return db, mbox


@pytest.fixture
def app(archive):
    db, mbox = archive
    app = server.create_app(str(db), str(mbox))
    app.config['TESTING'] = True
    return app


def test_connection_is_read_only_and_closed_after_request(app):
    with app.test_request_context('/'):
        conn = server._connection()
        assert conn is server._connection()
        assert conn.execute('SELECT count(*) FROM messages').fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            conn.execute('DELETE FROM messages')
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        conn.execute('SELECT 1')


def test_missing_database_is_not_created(archive, tmp_path):
    missing = tmp_path / 'missing.sqlite'
    with pytest.raises(ValueError, match='File not found'):
        server.create_app(str(missing), str(archive[1]))
    assert not missing.exists()


def test_invalid_database_is_rejected(archive):
    archive[0].write_bytes(b'not sqlite')
    with pytest.raises(ValueError, match='compatible email database'):
        server.create_app(*(str(path) for path in archive))


def test_changed_mbox_size_is_rejected(archive):
    db, mbox = archive
    mbox.write_bytes(mbox.read_bytes() + b'changed')
    with pytest.raises(ValueError, match='Mbox size differs'):
        server.create_app(str(db), str(mbox))


def test_timestamp_only_change_warns(archive, caplog):
    db, mbox = archive
    stat = mbox.stat()
    os.utime(mbox, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    server.create_app(str(db), str(mbox))
    assert 'modification time changed' in caplog.text


@pytest.mark.parametrize('argv', [[], ['--db', 'x'], ['--db', 'x', '--mbox', 'y', '--port', '0'],
                                 ['--db', 'x', '--mbox', 'y', '--port', 'abc']])
def test_cli_rejects_invalid_arguments(argv):
    with pytest.raises(SystemExit) as exc:
        server.main(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize('query', ['First', 'Alice', 'bob', 'Important', 'preview:body', 'subject:First'])
def test_search_uses_existing_fts_fields(app, query):
    response = app.test_client().get('/', query_string={'q': query})
    assert response.status_code == 200
    assert b'First message' in response.data
    assert b'1 message' in response.data


@pytest.mark.parametrize('query', ['"', 'subject:', 'bogus:word', 'word AND'])
def test_bad_search_is_a_readable_error(app, query):
    response = app.test_client().get('/', query_string={'q': query})
    assert response.status_code == 400
    assert b'Invalid search expression' in response.data


@pytest.mark.parametrize('page', ['0', '-1', 'abc', '1000000001'])
def test_invalid_pages(app, page):
    assert app.test_client().get('/', query_string={'page': page}).status_code == 400


def test_message_details_and_raw_download(app):
    client = app.test_client()
    response = client.get('/messages/1?q=First&page=2')
    assert response.status_code == 200
    assert b'First body' in response.data
    assert b'bob@example.com' in response.data
    assert b'Important' in response.data
    assert b'page=2' in response.data
    assert b'<iframe' not in response.data
    raw = client.get('/messages/1/raw')
    assert raw.status_code == 200
    assert b'Subject: First message' in raw.data
    assert 'attachment;' in raw.headers['Content-Disposition']
    assert raw.headers['Cache-Control'] == 'no-store'


@pytest.mark.parametrize('path', ['/messages/999', '/messages/999/raw', '/messages/999/html',
                                  '/messages/1/html', '/messages/1/attachments/0'])
def test_missing_messages_and_parts(app, path):
    assert app.test_client().get(path).status_code == 404


def test_untrusted_hosts_are_rejected(app):
    assert app.test_client().get('/', headers={'Host': 'evil.example'}).status_code == 400


def test_mbox_corruption_is_not_served(app, archive):
    archive[1].write_bytes(archive[1].read_bytes().replace(b'First body', b'Wrong body'))
    for path in ('/messages/1', '/messages/1/raw'):
        response = app.test_client().get(path)
        assert response.status_code == 409
        assert b'could not be read' in response.data
        assert str(archive[1]).encode() not in response.data


def test_protected_html_removes_active_markup():
    text = ('<style>@import url(https://tracker.invalid/a); p {color:red}</style>'
            '<script>evil()</script><meta http-equiv="refresh" content="0;url=https://evil.invalid">'
            '<form action="https://evil.invalid"><input></form>'
            '<iframe src="https://evil.invalid"></iframe>'
            '<a href="https://evil.invalid" onclick="evil()">Hello</a>'
            '<img src="https://tracker.invalid/pixel" srcset="https://tracker.invalid/other">')
    result = server._protected_html(text)
    assert 'Hello' in result
    assert '<style>' in result
    for unsafe in ('<script', '<meta', '<form', '<iframe', 'href=', 'src=', 'srcset=', 'onclick='):
        assert unsafe not in result


@pytest.fixture
def make_archive(tmp_path):
    counter = 0

    def create(messages, preview_length=500, compressed=False):
        nonlocal counter
        counter += 1
        directory = tmp_path / f'archive-{counter}'
        directory.mkdir()
        mbox = directory / ('mail.mbox.gz' if compressed else 'mail.mbox')
        db = directory / 'mail.sqlite'
        content = b''.join(
            b'From sender@example.com Sat Jan  1 00:00:00 2022\n' + message.as_bytes() + b'\n'
            for message in messages
        )
        mbox.write_bytes(gzip.compress(content) if compressed else content)
        if messages:
            database.build_database(str(mbox), str(db), preview_length=preview_length)
        else:
            with sqlite3.connect(db) as conn:
                conn.executescript(database.SCHEMA + database.INDEXES)
                database._write_meta(conn, str(mbox), 0, preview_length)
        app = server.create_app(str(db), str(mbox))
        app.config['TESTING'] = True
        return app

    return create


@pytest.fixture
def multipart_message():
    message = EmailMessage()
    message['Subject'] = '<script>alert(1)</script> & report'
    message['From'] = 'Alice <alice@example.com>'
    message['To'] = 'Bob <bob@example.com>'
    message.set_content('Full plain body <script>not markup</script>')
    message.add_alternative(
        '<html><body><h2>HTML report</h2><p>Readable content</p>'
        '<script>parent.document.body.dataset.compromised="yes"</script>'
        '<img src="https://tracker.invalid/pixel" onerror="alert(1)">'
        '<iframe src="https://tracker.invalid/frame"></iframe>'
        '<meta http-equiv="refresh" content="0;url=https://tracker.invalid/refresh">'
        '<form action="https://tracker.invalid/form"><button>Submit</button></form>'
        '<a href="https://tracker.invalid/link" target="_top">Link</a>'
        '<style>@import url(https://tracker.invalid/css); p { background: url(https://tracker.invalid/bg); }</style>'
        '</body></html>', subtype='html',
    )
    message.add_attachment(b'attachment bytes\x00\xff', maintype='application', subtype='octet-stream',
                           filename='../../report.bin')
    return message


def test_mime_alternatives_and_html_security(make_archive, multipart_message):
    client = make_archive([multipart_message]).test_client()
    plain = client.get('/messages/1')
    assert plain.status_code == 200
    assert b'Full plain body &lt;script&gt;' in plain.data
    assert b'&lt;script&gt;alert(1)&lt;/script&gt; &amp; report' in plain.data
    assert b'<script>' not in plain.data
    assert b'HTML report' not in plain.data
    detail = client.get('/messages/1?view=html')
    soup = BeautifulSoup(detail.data, 'html.parser')
    iframe = soup.find('iframe')
    assert iframe['sandbox'] == []
    assert iframe['referrerpolicy'] == 'no-referrer'
    html = client.get(iframe['src'])
    assert html.status_code == 200
    assert b'HTML report' in html.data
    assert b'<script>' not in html.data
    assert b'src=' not in html.data
    assert b'href=' not in html.data
    assert 'sandbox;' in html.headers['Content-Security-Policy']
    assert "default-src 'none'" in html.headers['Content-Security-Policy']
    assert "img-src 'none'" in html.headers['Content-Security-Policy']
    assert html.headers['Cache-Control'] == 'no-store'
    assert html.headers['X-Content-Type-Options'] == 'nosniff'


def test_attachment_download_is_exact_and_filename_is_safe(make_archive, multipart_message):
    client = make_archive([multipart_message]).test_client()
    response = client.get('/messages/1/attachments/0')
    assert response.status_code == 200
    assert response.data == b'attachment bytes\x00\xff'
    assert response.headers['Content-Disposition'] == 'attachment; filename=report.bin'
    assert response.content_type == 'application/octet-stream'
    assert client.get('/messages/1/attachments/1').status_code == 404


def test_nested_email_and_unnamed_attachment_downloads(make_archive):
    nested = EmailMessage()
    nested['Subject'] = 'Nested subject'
    nested.set_content('Nested body')
    outer = EmailMessage()
    outer.set_content('Outer body')
    outer.add_attachment(nested)
    client = make_archive([outer]).test_client()
    response = client.get('/messages/1/attachments/0')
    assert response.status_code == 200
    assert b'Subject: Nested subject' in response.data
    assert 'filename=attachment-1' in response.headers['Content-Disposition']


def test_html_only_message_has_full_text_fallback(make_archive):
    message = EmailMessage()
    message.set_content('<style>hidden-css</style><script>hidden-script</script>'
                        '<p>First paragraph</p><p>Second paragraph</p>', subtype='html')
    response = make_archive([message]).test_client().get('/messages/1')
    assert response.status_code == 200
    assert b'First paragraph\nSecond paragraph' in response.data
    assert b'hidden-css' not in response.data
    assert b'hidden-script' not in response.data


def test_unknown_charset_is_decoded_with_replacement(make_archive):
    message = EmailMessage()
    message['Content-Type'] = 'text/plain; charset=nonexistent-charset'
    message.set_payload(b'Hello \xff')
    response = make_archive([message]).test_client().get('/messages/1')
    assert response.status_code == 200
    assert 'Hello \ufffd' in response.get_data(as_text=True)


def test_search_is_preview_limited_but_view_is_complete(make_archive):
    message = EmailMessage()
    message.set_content('visible ' + 'padding ' * 100 + 'hiddenword')
    client = make_archive([message], preview_length=10).test_client()
    assert b'No matching messages' in client.get('/?q=hiddenword').data
    assert b'hiddenword' in client.get('/messages/1').data


def test_stable_pagination_and_preserved_query(make_archive):
    messages = []
    for number in range(52):
        message = EmailMessage()
        message['Subject'] = f'Paginated {number}'
        message.set_content('body')
        messages.append(message)
    client = make_archive(messages).test_client()
    first = BeautifulSoup(client.get('/?q=Paginated').data, 'html.parser')
    rows = first.select('.message-row')
    assert len(rows) == 50
    assert rows[0]['href'] == '/messages/52?q=Paginated&page=1'
    assert rows[-1]['href'] == '/messages/3?q=Paginated&page=1'
    second = BeautifulSoup(client.get('/?q=Paginated&page=2').data, 'html.parser')
    assert len(second.select('.message-row')) == 2
    assert second.select_one('.message-row')['href'] == '/messages/2?q=Paginated&page=2'
    assert b'No messages on this page' in client.get('/?page=100').data


def test_empty_archive_and_gzip_retrieval(make_archive):
    assert b'The archive is empty' in make_archive([]).test_client().get('/').data
    message = EmailMessage()
    message.set_content('Compressed message body')
    app = make_archive([message], compressed=True)
    assert b'Compressed message body' in app.test_client().get('/messages/1').data


def test_archive_is_not_modified_by_routes(app, archive):
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in archive]
    client = app.test_client()
    for path in ('/', '/?q=First', '/messages/1', '/messages/1/raw'):
        assert client.get(path).status_code == 200
    assert before == [(path.read_bytes(), path.stat().st_mtime_ns) for path in archive]


def test_unsupported_schema_and_missing_mbox(archive):
    db, mbox = archive
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    with pytest.raises(ValueError, match='Unsupported database schema'):
        server.create_app(str(db), str(mbox))
    mbox.unlink()
    with pytest.raises(ValueError, match='File not found'):
        server.create_app(str(db), str(mbox))


def test_cli_launches_only_on_loopback(archive, monkeypatch):
    calls = []
    monkeypatch.setattr(server.Flask, 'run', lambda self, **options: calls.append(options))
    server.main(['--db', str(archive[0]), '--mbox', str(archive[1]), '--port', '5123'])
    assert calls == [{'host': '127.0.0.1', 'port': 5123, 'debug': False, 'use_reloader': False}]


def test_cli_reports_bad_files_without_traceback(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        server.main(['--db', str(tmp_path / 'missing'), '--mbox', 'missing'])
    assert exc.value.code == 2
    assert 'Traceback' not in capsys.readouterr().err