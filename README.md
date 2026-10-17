# Invoice Vault

A standalone, local-first invoice web app alongside Ultron. PDF extraction uses the pinned pypdf dependency in requirements.txt.
Use Python 3.9+ and a project virtual environment.
This is a working prototype, not a deployed or production-hardened online service.
For the separate fictional read-only public demo, see [DEMO.md](DEMO.md).
It does not change Ultron's interface or integrations.

## Clone the repository

```sh
git clone git@github.com:AlecJBPro/Invoice-Vault.git invoices
cd invoices
```

Keep the checkout folder named `invoices`: the commands below import it as a
Python package from its parent directory.

## Start locally

From the `invoices` project folder, set up the environment once:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Then create a business (only once) and start the app from that same folder:

```sh
PYTHONPATH=.. .venv/bin/python -m invoices create-business "My Business"
PYTHONPATH=.. .venv/bin/python -m invoices serve
```

Restart an already-running server after updating the source, then refresh your browser.

Open `http://127.0.0.1:7755` in your browser. Enter the access key printed by the
first command. Save that key privately in a password manager: it grants full
read/upload/delete access to that business. Do not paste it into assistant chat.
The key is displayed only during creation and cannot be recovered from its hash.
Create another business with the same command and a different name; it receives
an independent account and key. Names are labels, not account identifiers.

The key stays in browser memory, not cookies or localStorage. Reloading requires
sign-in again. Sign out when finished, especially on a shared device. This MVP
has no individual staff logins, key rotation/recovery, or self-service registration.

## Features

- Drag-and-drop, keyboard-accessible file picker, and multiple uploads (20 per batch).
- Live rear-facing camera when available, JPEG capture, preview, upload, and discard.
- Native mobile photo capture/file picker fallback if live camera is unavailable.
- PDF, JPEG, PNG, and WebP uploads; 10 MiB maximum per file.
- Optional notes, UTC upload timestamps displayed in the browser's local time,
  filename, detected content type, size, and SHA-256 digest.
- Duplicate detection per business based on exact file bytes. Duplicates retain
  the original filename and notes; they do not overwrite an existing record.
- Paginated library, original-file downloads, and confirmed permanent deletion.
- Server-side business isolation for listing, downloading, deletion, and deduplication.
- 250 MiB logical document quota and 10,000 invoice maximum per business, with bounded concurrent connections.

## Automatic invoice fields

New text-based PDF uploads automatically suggest invoice date, invoice number,
total quoted amount (preserving printed currency), and customer name. Open
**Review / edit fields**, compare all four values against the downloaded original,
correct them, and choose **Save reviewed fields**. Unknown or conflicting values
remain blank. Numeric dates are interpreted as US month/day/year and stored as
YYYY-MM-DD. Suggestions use printed labels, not AI, and will not recognize every
invoice layout. No totals, taxes, exchange rates, or balances are calculated.

Existing documents have an **Extract fields** button; extraction does not replace
saved, reviewed values. Duplicate uploads preserve the existing record and its
corrections. Database columns are added automatically without replacing original
files. Back up existing data before upgrading.

Under **Add invoices**, download the fictional test invoice and upload it. Expected
results: September 11, 2026; TEST-1001; USD 1,080.00; Example Customer LLC.
It is clearly labeled as fictional and is not payable.

Scanned PDFs and photos need OCR, which is not enabled. Password-protected,
unreadable, or resource-limited PDFs remain stored with an explanatory message;
enter their fields manually. PDF extraction is limited to 40 pages, 200,000 text
characters, two simultaneous workers, and a 12-second timeout.

No OCR, tax/accounting advice, payment processing, email ingestion, or integration
with bookkeeping software is included.

## Storage and backup

By default the app creates `~/.invoice-vault/invoices.sqlite`, outside the source
repository so business documents are not accidentally committed with the app.
A dedicated private directory and owner-only file permissions are used on POSIX
systems. Do not point the app at a shared directory or untrusted symlinks.

SQLite stores both metadata and the original document bytes transactionally.
This intentionally replaces the initially proposed separate document folder:
a successful upload cannot leave a database record without its file. Nothing is
sent to an AI service or external database provider. Documents are not encrypted
by this app; use an encrypted disk and encrypted backups for sensitive data.

To choose another dedicated persistent directory, use the same option on BOTH
commands (before the subcommand):

```sh
PYTHONPATH=.. .venv/bin/python -m invoices --data-dir /absolute/private/invoice-data create-business "My Business"
PYTHONPATH=.. .venv/bin/python -m invoices --data-dir /absolute/private/invoice-data serve --port 7755
```

Stop the server before copying the entire data directory for a simple consistent
backup. Restore to a private directory and point both commands at it. For live
backups, an operator must use SQLite's backup API rather than copying a database
while it is being written. Test restores before trusting backups. Deleted documents
may remain in backups or storage snapshots. The logical quota is not a disk-size
limit: database overhead and freed pages can make the database larger. A transactional
10,000-invoice ceiling also bounds metadata growth from tiny documents; existing
rows are retained, and duplicates remain available when a quota is reached.

## Phone access and eventual online hosting

The local command serves only the computer running Python. It does not provision
hosting, register a domain, issue HTTPS certificates, or create a cloud database.
A phone cannot reach this server through its own localhost address.

Live browser camera access requires a secure context (HTTPS, or localhost for
local development) and the user's camera permission. A phone photo picker is
provided as a fallback. Mobile browser/device behavior needs hands-on testing.

For a controlled hosted pilot, an operator needs a persistent server, a dedicated
service account, private persistent storage, a domain, and an HTTPS reverse proxy.
Keep Python bound to `127.0.0.1`, and have the proxy forward to its local port.
Use `serve --public-origin https://invoices.example.com` with your exact origin
(no trailing slash). The proxy must preserve that origin's Host header. Requests
with other Host or Origin headers are rejected. Do not expose the Python port,
database, or data directory through the proxy. Never put access keys in URLs.

Before storing real customers' documents online, add and verify a production
serving layer, per-client rate limits at the proxy, connection/body/time limits,
operational monitoring, key revocation/rotation and account recovery, audit logs,
encrypted backups and restore procedures, retention policies, and malware
scanning. Python's standard-library HTTP server here is for development; a proxy
alone does not make this app production-ready. Multiple replicas or an ephemeral
hosting filesystem are not supported. A managed database/object store would be a
separate deployment/storage adaptation, not something this prototype provisions.

## Upload security scope

The backend checks file size and leading signatures independently of the filename
or claimed browser MIME type. Signature checks do NOT prove a file is well-formed
or safe. PDFs and images can contain malicious data. Downloads are attachments,
not embedded document previews; handle uploaded files as untrusted. The upload service parses PDFs locally using pypdf in a separate process with a
wall-clock timeout and CPU/memory limits where supported. This process is not an
OS security sandbox. Keep the parser dependency updated. No OCR engine, document
macro, or document-supplied command is executed.
File names never become filesystem paths. SQL uses parameterized values.

The UI inserts document names and notes as text, uses a restrictive content
security policy, and does not load third-party scripts or analytics. APIs require
a high-entropy bearer key, enforce host/origin restrictions, and do not enable
CORS. Anyone possessing a business key can delete that business's documents.
The administrator and anyone with filesystem access can access the database.

## Tests

Run after reviewing the local code and approving test execution:

```sh
PYTHONPATH=.. .venv/bin/python -m unittest discover -s tests -v
```

Focused run, if running manually:

```sh
PYTHONPATH=.. .venv/bin/python -m unittest discover -s tests -p 'test_invoices.py' -v
```

Tests use temporary directories, synthetic format signatures, and generated
fictional PDFs, not real customer documents. They cover authentication, isolation, persisted byte-for-byte
storage, per-business deduplication (including concurrent requests), quota,
metadata validation, pagination, downloads/deletion, static routes, request
origin/host checks, sanitized database failures, four-field PDF extraction,
manual review persistence, legacy schema migration, extraction failures and limits,
and the downloadable test invoice.

Manual browser checks still required: camera allow/deny/unavailable paths, rear
camera selection, preview readability, discarding and retrying photos, camera
release on close/sign-out/backgrounding, mobile picker behavior, drag/drop,
keyboard access, interrupted upload recovery, and downloads on supported devices.
