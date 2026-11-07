# MBox Stats
This is a tool for pulling statistics out of a MBox file. Main inspiration for this project was that my Gmail account
was nearing the free limit (15GB) and I had no obvious sources to where all that storage went (e.g. searching for
large attachments and removing them did not make any noticable difference).

The tool can process compressed MBox files (with `.gz`, `.bz2`, `.xz`, or `.zstd` extension).

## Email Browser

Install dependencies and build an index if you do not already have one:

```sh
uv sync
uv run python src/mbox-to-sqlite.py /path/mail.mbox --db /path/mail.sqlite
```

Start the local Flask server with an existing database and its matching mbox:

```sh
uv run mbox-serve --db /path/mail.sqlite --mbox /path/mail.mbox
```

Open http://127.0.0.1:5000. Use `--port 5001` to choose another port. The module
entry point also works: `uv run python -m mbox_stats.server --db ... --mbox ...`.

The interface lists newest messages first, with 50 messages per page. Search uses
the existing SQLite FTS5 index: subject, sender name/address, recipients, labels,
and body preview. Examples include `invoice`, `"project update"`, `subject:report`,
`labels:Important`, and `invoice OR receipt`. Quote addresses or terms containing
punctuation, for example `from_addr:"alice@example.com"`.

Search does **not** cover entire bodies. The indexer stores a 500-character preview
by default; `--preview-length` changes this when building the database, and zero
disables body preview indexing. Viewing a message retrieves its complete body
from the mbox. Messages can be viewed as plain text or HTML, with attachment and
raw `.eml` downloads. HTML-only messages also have a readable plain-text view.

The database and mbox are read-only. Keep the mbox unchanged: startup rejects a
size mismatch and warns about timestamp changes; retrieval verifies each
message's checksum. Rebuild the index after editing the archive. Compressed
archives work, but viewing messages can be slow because retrieval decompresses
up to their offsets. Consider decompressing **before** indexing. `.zstd` support
requires Python 3.14 or newer.

This server is local-only, binds to `127.0.0.1`, and has no authentication. Do not
expose it through a reverse proxy or port forwarding. Debug mode is disabled.
Email HTML is isolated with sandboxing and a restrictive Content Security Policy;
scripts, forms, navigation, remote content, and images (including inline/CID
images) are blocked. Downloads are served as attachments, not inline content.
Downloaded files are untrusted; opening them leaves the browser's protections.

Run the tests with `uv run pytest -q`.