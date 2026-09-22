"""Local prototype HTTP API. Do not expose this server directly to the internet."""
import json
import re
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .store import MAX_FILE_BYTES, QuotaExceeded

UI = Path(__file__).parent / 'ui'
STATIC = {'/test-invoice.pdf': ('test-invoice.pdf', 'application/pdf'), '/': ('index.html', 'text/html; charset=utf-8'),
          '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
          '/style.css': ('style.css', 'text/css; charset=utf-8')}


class Server(ThreadingHTTPServer):
    """Threaded loopback server with a small concurrency limit."""
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, address, store, public_origin=None):
        if address[0] != '127.0.0.1':
            raise ValueError('The prototype must bind to 127.0.0.1.')
        if public_origin:
            parts = urlsplit(public_origin)
            if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                    or parts.path or parts.query or parts.fragment):
                raise ValueError('Public origin must be an HTTPS origin without a path.')
        self.store = store
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)
        self.origin = public_origin or 'http://127.0.0.1:' + str(self.server_port)
        self.host = urlsplit(self.origin).netloc

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    """Translate browser requests into authenticated Store operations."""
    server_version = 'InvoiceVault'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(20)

    def log_message(self, *args):
        # Never log credentials, uploaded names, document contents, or query strings.
        pass

    def output(self, status, data, kind='application/json', extra=None):
        """Send consistent no-cache responses and the app security headers."""
        raw = json.dumps(data).encode() if kind == 'application/json' else data
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Resource-Policy', 'same-origin')
        self.send_header('Permissions-Policy', 'camera=(self), microphone=()')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' blob:; media-src 'self' blob:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def do_DELETE(self):
        self.dispatch('DELETE')

    def dispatch(self, method):
        try:
            self.route(method)
        except QuotaExceeded as error:
            self.output(413, {'error': str(error)})
        except (ValueError, UnicodeError):
            self.output(400, {'error': 'Invalid input. Check file type, filename, notes, invoice fields (date: YYYY-MM-DD), and page number.'})
        except (TimeoutError, ConnectionError):
            self.close_connection = True
        except sqlite3.Error:
            self.output(503, {'error': 'Storage is unavailable. Please try again later.'})
        except Exception:
            self.output(500, {'error': 'The invoice service could not complete this request.'})

    def route(self, method):
        """Validate origin/authentication, then dispatch one safe API route."""
        if self.headers.get_all('Host', []) != [self.server.host]:
            return self.output(403, {'error': 'Invalid host.'})
        origin = self.headers.get('Origin')
        if origin is not None and origin != self.server.origin:
            return self.output(403, {'error': 'Cross-origin requests are not allowed.'})
        if method != 'GET' and origin != self.server.origin:
            return self.output(403, {'error': 'Same-origin requests are required.'})
        path = urlsplit(self.path)
        if method == 'GET' and path.path in STATIC:
            filename, kind = STATIC[path.path]
            return self.output(200, (UI / filename).read_bytes(), kind)
        if not path.path.startswith('/api/'):
            return self.output(404, {'error': 'Not found.'})
        authorization = self.headers.get('Authorization', '')
        token = authorization[7:] if authorization.startswith('Bearer ') else ''
        business = self.server.store.authenticate(token)
        if not business:
            return self.output(401, {'error': 'Enter a valid business access key.'})
        store, owner = self.server.store, business['id']
        if method == 'GET' and path.path == '/api/me':
            return self.output(200, business)
        if path.path == '/api/invoices':
            if method == 'GET':
                query = parse_qs(path.query)
                offset = int(query.get('offset', ['0'])[0])
                return self.output(200, store.list(owner, offset))
            if method == 'POST':
                if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                    return self.output(400, {'error': 'A single Content-Length is required.'})
                length = int(self.headers['Content-Length'])
                if not 0 < length <= MAX_FILE_BYTES:
                    return self.output(413, {'error': 'Each file must be nonempty and at most 10 MiB.'})
                filename = unquote(self.headers.get('X-Filename', ''), errors='strict')
                notes = unquote(self.headers.get('X-Notes', ''), errors='strict')
                data = self.rfile.read(length)
                if len(data) != length:
                    return self.output(400, {'error': 'Incomplete upload; nothing was saved.'})
                invoice, duplicate = store.add(owner, filename, data, notes)
                return self.output(200 if duplicate else 201, {'invoice': invoice, 'duplicate': duplicate})
        field_match = re.fullmatch(r'/api/invoices/([0-9a-f]{32})/(fields|extract)', path.path)
        if method == 'POST' and field_match:
            key, action = field_match.groups()
            if self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length', [])) != 1:
                return self.output(400, {'error': 'A single Content-Length is required.'})
            length = int(self.headers['Content-Length'])
            if not 0 <= length <= 4096:
                return self.output(413, {'error': 'Field request is too large.'})
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ValueError('Incomplete fields')
            if action == 'fields':
                item = store.save_fields(owner, key, json.loads(raw))
            else:
                if raw:
                    raise ValueError('Extraction request must have an empty body')
                item = store.reextract(owner, key)
            return self.output(200, {'invoice': item}) if item else self.output(404, {'error': 'Not found.'})
        match = re.fullmatch(r'/api/invoices/([0-9a-f]{32})', path.path)
        if match:
            key = match.group(1)
            if method == 'GET':
                item = store.get(owner, key)
                if item:
                    disposition = "attachment; filename=invoice; filename*=UTF-8''" + quote(item['filename'], safe='')
                    return self.output(200, item['document'], item['content_type'],
                                       {'Content-Disposition': disposition})
            elif method == 'DELETE':
                if store.delete(owner, key):
                    return self.output(200, {'deleted': True})
        return self.output(404, {'error': 'Not found.'})
