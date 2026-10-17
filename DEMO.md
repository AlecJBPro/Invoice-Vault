# Demo mode

Invoice Vault has an opt-in, read-only demo mode with three fictional invoices. It powers the public demo linked from [alecbozzo.dev](https://alecbozzo.dev).

## Run it

Demo mode uses only the Python standard library (pypdf is not needed). `run.py` works even when the checkout folder isn't named `invoices`.

```sh
# Create or reset fictional demo data in a new, empty directory
python3 -I run.py --data-dir ./demo-data demo-reset

# Serve the demo (always binds to 127.0.0.1)
python3 -I run.py --data-dir ./demo-data serve --demo --port 7756 --public-origin https://demo.example.com
```

`--data-dir` goes before the command; the serve flags go after `serve`. The public origin must be an exact HTTPS origin with no path.

## What demo mode does

- Visitors enter automatically; no access key is needed.
- Visitors can browse the library, review fields, view and download the three fictional invoices.
- Uploads, field edits, re-extraction and deletion are refused by the server itself, not just hidden in the interface, even when a valid access key is sent.
- Data resets at startup, after every UTC midnight, and with `demo-reset`. Nothing a visitor does is saved.
- The demo directory is marked as demo-only. `demo-reset` refuses non-empty unmarked directories and the default private data path, so it can't overwrite real invoices.

Private mode is unchanged: `create-business`, `serve` on port 7755, per-business access keys and the database format work exactly as before.

## Deploying

Put the demo behind a reverse proxy that handles HTTPS, request timeouts, body limits and rate limiting. Python's built-in server is acceptable for this small, read-only, fictional demo, but not as a general production server or for real invoice data.
