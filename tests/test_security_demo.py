"""Security regressions using the real HTTP handler without opening sockets."""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from invoices.demo import BUSINESS, DemoStore, OWNER, seed_documents
from invoices.extraction import extract_invoice, worker, result
from invoices.server import Handler, Server
from invoices.store import Store, QuotaExceeded


class Connection:
    def __init__(self, request):
        self.input = io.BytesIO(request)
        self.output = bytearray()

    def makefile(self, *args):
        return self.input

    def sendall(self, data):
        self.output.extend(data)

    def settimeout(self, seconds):
        self.timeout = seconds


class SecurityDemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DemoStore(Path(self.temp.name) / 'demo')
        self.server = SimpleNamespace(store=self.store, host='invoices.example.invalid',
                                      origin='https://invoices.example.invalid')

    def request(self, path, method='GET', headers=(), body=b''):
        raw = f'{method} {path} HTTP/1.1\r\nHost: {self.server.host}\r\n'.encode()
        raw += b''.join(f'{key}: {value}\r\n'.encode() for key, value in headers)
        connection = Connection(raw + b'\r\n' + body)
        Handler(connection, ('127.0.0.1', 1234), self.server)
        head, content = bytes(connection.output).split(b'\r\n\r\n', 1)
        return int(head.split()[1]), head, content

    def test_favicon_static_headers_host_and_demo_read_only(self):
        for store in (self.store, Store(Path(self.temp.name) / 'private')):
            self.server.store = store
            with self.subTest(demo=isinstance(store, DemoStore)):
                status, headers, body = self.request('/favicon.svg')
                self.assertEqual(status, 200)
                self.assertTrue(body)
                self.assertIn(b'Content-Type: image/svg+xml\r\n', headers)
                self.assertIn(b'X-Content-Type-Options: nosniff\r\n', headers)
                home_headers = self.request('/')[1]
                for header in headers.split(b'\r\n'):
                    if header.startswith((b'Content-Security-Policy:', b'Permissions-Policy:',
                                          b'Cross-Origin-', b'X-Frame-Options:',
                                          b'Referrer-Policy:', b'Cache-Control:')):
                        self.assertIn(header, home_headers.split(b'\r\n'))
                self.assertEqual(self.request('/favicon.svg', headers=[('Host', 'evil.invalid')])[0], 403)
                for method in ('POST', 'DELETE'):
                    status, _, body = self.request('/favicon.svg', method,
                        [('Origin', self.server.origin)])
                    self.assertEqual(status, 403 if isinstance(store, DemoStore) else 404)
                    if isinstance(store, DemoStore):
                        self.assertEqual(json.loads(body)['error'], 'Fictional demo is read-only.')
                for method in ('PUT', 'PATCH', 'HEAD', 'OPTIONS'):
                    self.assertEqual(self.request('/favicon.svg', method)[0], 501)

    def test_demo_no_key_browse_view_download(self):
        self.assertEqual(json.loads(self.request('/api/config')[2]), {'demo': True})
        self.assertEqual(json.loads(self.request('/api/me')[2]), BUSINESS)
        listing = json.loads(self.request('/api/invoices')[2])
        self.assertEqual(listing['total'], 3)
        for item in listing['invoices']:
            path = '/api/invoices/' + item['id']
            status, headers, data = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn(b'attachment;', headers)
            self.assertIn(b'FICTIONAL DEMO INVOICE - NOT PAYABLE', data)
            self.assertEqual(self.request(path + '/view')[2], data)
            self.assertIn(b'inline;', self.request(path + '/view')[1])
        self.assertEqual(self.request('/api/invoices/' + 'f' * 32)[0], 404)

    def test_demo_all_mutations_denied_even_with_key(self):
        key = next(seed_documents())[0]
        for path, method in [('/api/invoices', 'POST'),
                             ('/api/invoices/' + key, 'DELETE'),
                             ('/api/invoices/' + key + '/fields', 'POST'),
                             ('/api/invoices/' + key + '/extract', 'POST')]:
            self.assertEqual(self.request(path, method,
                [('Origin', self.server.origin), ('Authorization', 'Bearer public-demo'),
                 ('Content-Length', '999999999')])[0], 403)
        for method in ('PUT', 'PATCH', 'HEAD', 'OPTIONS'):
            self.assertEqual(self.request('/api/invoices', method)[0], 501)
        self.assertEqual(self.store.list(OWNER)['total'], 3)
        for method in ('add', 'delete', 'save_fields', 'reextract', 'create_business'):
            with self.assertRaises(ValueError):
                getattr(self.store, method)()

    def test_reset_reproducible_and_atomic_seed(self):
        before = self.store.list(OWNER)
        docs = list(seed_documents())
        self.assertEqual(docs, list(seed_documents()))
        self.store.reset_day = date(2000, 1, 1)
        self.store.reset_if_due()
        self.assertEqual(before, self.store.list(OWNER))
        self.assertEqual(before, DemoStore(self.store.folder).list(OWNER))
        with patch('invoices.demo.seed_documents', side_effect=RuntimeError('seed failure')):
            with self.assertRaises(RuntimeError):
                self.store.reset()
        self.assertEqual(before, self.store.list(OWNER))

    def test_private_directory_refused_without_changes(self):
        private = Path(self.temp.name) / 'private'
        store = Store(private)
        business = store.create_business('Private')
        original = store.path.read_bytes()
        with self.assertRaises(ValueError):
            DemoStore(private)
        self.assertEqual(store.path.read_bytes(), original)
        self.assertEqual(store.authenticate(business['token'])['name'], 'Private')
        with self.assertRaises(ValueError):
            DemoStore(Path.home() / '.invoice-vault')

    def test_headers_targets_and_duplicate_origin(self):
        for headers in ([('Host', self.server.host)], [('Origin', self.server.origin)] * 2,
                        [('Origin', 'null')], [('Origin', 'https://evil.invalid')]):
            self.assertEqual(self.request('/api/me', headers=headers)[0], 403)
        self.assertEqual(self.request('https://evil.invalid/api/me')[0], 400)
        for path in ('/../../README.md', '/%2e%2e/store.py', '/.git/HEAD', '/invoices.sqlite'):
            self.assertEqual(self.request(path)[0], 404)
        for offset in ('-1', 'a', '9' * 5000):
            self.assertEqual(self.request('/api/invoices?offset=' + offset)[0], 400)
        self.assertEqual(self.request('/api/me', headers=[('Authorization', 'a')] * 2)[0], 401)

    def test_all_errors_generic_and_hardened(self):
        with patch.object(self.store, 'list', side_effect=RuntimeError('SECRET DATABASE PATH')):
            status, headers, content = self.request('/api/invoices')
        self.assertEqual(status, 500)
        self.assertNotIn(b'SECRET DATABASE PATH', content)
        for status, headers, content in (self.request('/missing'), self.request('/bad', 'BAD<script>')):
            self.assertIn(status, (404, 501))
            self.assertIn(b'X-Frame-Options: DENY', headers)
            self.assertIn(b'Cross-Origin-Opener-Policy: same-origin', headers)
            self.assertIn(b'Content-Security-Policy:', headers)
            self.assertNotIn(b'<script>', content)
            self.assertNotIn(b'Python/', headers)

    def test_private_bearer_and_isolation_still_enforced(self):
        store = Store(Path(self.temp.name) / 'private')
        a, b = store.create_business('A'), store.create_business('B')
        self.server.store = store
        self.assertEqual(self.request('/api/me')[0], 401)
        self.assertFalse(json.loads(self.request('/api/config')[2])['demo'])
        pdf = next(seed_documents())[2]
        with patch('invoices.store.extract_invoice', return_value=result('needs_review', 'Test fixture')):
            item, _ = store.add(a['id'], '../../invoice.pdf', pdf)
        self.assertEqual(item['filename'], 'invoice.pdf')
        key = '/api/invoices/' + item['id']
        self.assertEqual(self.request(key, headers=[('Authorization', 'Bearer ' + b['token'])])[0], 404)
        self.assertEqual(self.request(key, headers=[('Authorization', 'Bearer ' + a['token'])])[2], pdf)
        # Missing Origin fails even for a valid business key (CSRF protection).
        self.assertEqual(self.request(key, 'DELETE', [('Authorization', 'Bearer ' + a['token'])])[0], 403)

    def test_parser_fails_closed_without_resource_limits(self):
        with patch('resource.setrlimit', side_effect=OSError('not supported')):
            self.assertEqual(worker(b'%PDF-')['extraction_status'], 'unavailable')
        self.assertEqual(extract_invoice(b'x' * (10 * 1024 * 1024 + 1), 'application/pdf')['extraction_status'], 'limited')

    def test_private_upload_framing_and_field_isolation(self):
        store = Store(Path(self.temp.name) / 'private')
        a, b = store.create_business('A'), store.create_business('B')
        self.server.store = store
        headers = [('Origin', self.server.origin), ('Authorization', 'Bearer ' + a['token']),
                   ('X-Filename', 'invoice.pdf')]
        pdf = next(seed_documents())[2]
        with patch('invoices.store.extract_invoice', return_value=result('needs_review', 'Test fixture')):
            status, _, content = self.request('/api/invoices', 'POST',
                headers + [('Content-Length', str(len(pdf)))], pdf)
        self.assertEqual(status, 201)
        key = json.loads(content)['invoice']['id']
        fields = dict(next(seed_documents())[1])
        raw = json.dumps(fields).encode()
        other = [('Origin', self.server.origin), ('Authorization', 'Bearer ' + b['token']),
                 ('Content-Length', str(len(raw)))]
        self.assertEqual(self.request('/api/invoices/' + key + '/fields', 'POST', other, raw)[0], 404)
        self.assertEqual(self.request('/api/invoices/' + key, 'DELETE', other)[0], 404)
        for framing, status in [([('Content-Length', '1')] * 2, 400),
                                ([('Transfer-Encoding', 'chunked'), ('Content-Length', '1')], 400),
                                ([('Content-Length', '-1')], 413),
                                ([('Content-Length', '10485761')], 413),
                                ([('Content-Length', '4')], 400)]:
            self.assertEqual(self.request('/api/invoices', 'POST', headers + framing, b'x')[0], status)
        self.assertEqual(store.list(a['id'])['total'], 1)

    def test_extractor_worker_protocol_is_bounded(self):
        with patch('invoices.extraction.subprocess.run') as run:
            run.return_value = SimpleNamespace(returncode=0, stdout=b'{}')
            self.assertEqual(extract_invoice(b'%PDF-', 'application/pdf')['extraction_status'], 'failed')
            self.assertEqual(run.call_args.kwargs['timeout'], 12)
            self.assertIn('-I', run.call_args.args[0])
            run.return_value = SimpleNamespace(returncode=0, stdout=b'x' * 8193)
            self.assertEqual(extract_invoice(b'%PDF-', 'application/pdf')['extraction_status'], 'failed')
            run.side_effect = subprocess.TimeoutExpired('worker', 12)
            self.assertEqual(extract_invoice(b'%PDF-', 'application/pdf')['extraction_status'], 'failed')
        with patch('invoices.extraction.SLOTS') as slots:
            slots.acquire.return_value = False
            self.assertEqual(extract_invoice(b'%PDF-', 'application/pdf')['extraction_status'], 'unavailable')
            slots.release.assert_not_called()

    def test_launcher_cli_requires_separate_demo_data_dir(self):
        launcher = Path(__file__).resolve().parents[1] / 'run.py'
        invocation = [sys.executable, '-I', str(launcher)]
        run = subprocess.run(invocation + ['serve', '--demo'], capture_output=True, timeout=5)
        self.assertEqual(run.returncode, 2)
        self.assertIn(b'explicit separate --data-dir', run.stderr)
        folder = Path(self.temp.name) / 'cli-demo'
        run = subprocess.run(invocation + ['--data-dir', str(folder), 'demo-reset'],
                             capture_output=True, timeout=5)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertEqual(DemoStore(folder).list(OWNER)['total'], 3)

    def test_tiny_uploads_cannot_bypass_metadata_quota(self):
        store = Store(Path(self.temp.name) / 'private')
        a, b = store.create_business('A'), store.create_business('B')
        data = b'\x89PNG\r\n\x1a\n'
        with patch('invoices.store.MAX_BUSINESS_INVOICES', 1):
            item, _ = store.add(a['id'], 'x' * 200, data, 'n' * 2000)
            with self.assertRaises(QuotaExceeded):
                store.add(a['id'], 'x', data + b'2', 'n' * 2000)
            self.assertEqual(store.add(a['id'], 'duplicate', data)[0]['id'], item['id'])
            self.assertEqual(store.list(a['id'])['total'], 1)
            self.assertFalse(store.add(b['id'], 'other', data)[1])

    def test_origin_configuration_validation(self):
        for origin in ('https://example.invalid\n', 'https://example.invalid:bad',
                       'https://example.invalid:', 'https://example.invalid:0', 'https://example.invalid/path'):
            with self.assertRaises(ValueError):
                Server(('127.0.0.1', 0), self.store, origin)


if __name__ == '__main__':
    unittest.main()
