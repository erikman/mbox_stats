from email.message import EmailMessage

from mbox_stats import database


def html_message(content: str) -> EmailMessage:
    message = EmailMessage()
    message.set_content(content, subtype='html')
    return message


def test_html_preview_extracts_text_decodes_entities_and_omits_scripts_and_styles():
    message = html_message(
        '<html><head><style>.hidden { display: none; }</style>'
        '<script>trackingCode()</script></head><body>'
        '<p>Hello&nbsp;<strong>there</strong> &amp; welcome</p>'
        '</body></html>'
    )

    assert database._preview(message, 100) == 'Hello there & welcome'


def test_html_preview_handles_malformed_nested_markup_with_spacing():
    message = html_message('<p>First line<p>Second <em>nested text</em>')

    assert database._preview(message, 100) == 'First line Second nested text'


def test_preview_prefers_plain_text_alternative():
    message = EmailMessage()
    message.set_content('Plain text version')
    message.add_alternative('<p>HTML version</p>', subtype='html')

    assert database._preview(message, 100) == 'Plain text version'


def test_preview_respects_length_and_zero_length():
    message = html_message('<p>Long preview text</p>')

    assert database._preview(message, 5) == 'Long '
    assert database._preview(message, 0) is None


def test_html_preview_caps_source_before_parsing(monkeypatch):
    monkeypatch.setattr(database, '_MAX_PREVIEW_SOURCE', 20)
    message = html_message('<p>Visible before</p><p>Hidden after cap</p>')

    assert 'Hidden after cap' not in database._preview(message, 100)


def test_get_messages_accepts_multiple_ids_and_preserves_input_order(tmp_path):
    mbox_path = tmp_path / 'messages.mbox'
    db_path = tmp_path / 'messages.sqlite'
    mbox_path.write_bytes(
        b'From sender@example.com Sat Jan  1 00:00:00 2022\n'
        b'From: sender@example.com\nSubject: first\n\nfirst body\n'
        b'From sender@example.com Sat Jan  1 00:00:01 2022\n'
        b'From: sender@example.com\nSubject: second\n\nsecond body\n'
    )
    database.build_database(str(mbox_path), str(db_path))

    with database.sqlite3.connect(db_path) as conn:
        messages = database.get_message(conn, [2, 1, 2])
        raw_messages = database.get_raw_message(conn, (2, 1))
        single_message = database.get_message(conn, 1)
        single_raw_message = database.get_raw_message(conn, 1)

    assert [message.get('Subject') for message in messages] == ['second', 'first', 'second']
    assert [b'Subject: second' in raw for raw in raw_messages] == [True, False]
    assert single_message.get('Subject') == 'first'
    assert b'Subject: first' in single_raw_message