"""Offline store and HTTP tests. Camera permission and mobile UX need manual testing."""
import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from invoices.server import Server
from invoices.store import MAX_FILE_BYTES, QuotaExceeded, Store, detect_type

PDF = b'%PDF-1.4\nminimal signature fixture, not a renderable document\n%%EOF'


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.a = self.store.create_business('Acme')
        self.b = self.store.create_business('Other Business')

    def test_authentication_and_hashed_tokens(self):
        self.assertEqual(self.store.authenticate(self.a['token'])['name'], 'Acme')
        self.assertIsNone(self.store.authenticate('incorrect-key'))
        self.assertIsNone(self.store.authenticate(None))
        with closing(self.store.connect()) as db:
            digest = db.execute('SELECT token_hash FROM businesses WHERE id = ?', (self.a['id'],)).fetchone()[0]
        self.assertEqual(digest, hashlib.sha256(self.a['token'].encode()).hexdigest())
        self.assertNotEqual(digest, self.a['token'])

    def test_persistence_and_metadata(self):
        row, duplicate = self.store.add(self.a['id'], 'invoice.pdf', PDF, 'Office supplies')
        self.assertFalse(duplicate)
        reopened = Store(self.temp.name)
        actual = reopened.get(self.a['id'], row['id'])
        self.assertEqual(actual['document'], PDF)
        self.assertEqual(actual['notes'], 'Office supplies')
        self.assertEqual(actual['content_type'], 'application/pdf')
        self.assertEqual(actual['size'], len(PDF))
        self.assertTrue(actual['created_at'].endswith('+00:00'))
        listing = reopened.list(self.a['id'])
        self.assertEqual(listing['total'], 1)
        self.assertNotIn('document', listing['invoices'][0])
        self.assertEqual(Path(self.temp.name).stat().st_mode & 0o777, 0o700)
        self.assertEqual(reopened.path.stat().st_mode & 0o777, 0o600)

    def test_business_isolation_for_list_get_delete_and_duplicates(self):
        row, _ = self.store.add(self.a['id'], 'invoice.pdf', PDF)
        self.assertEqual(self.store.list(self.b['id'])['total'], 0)
        self.assertIsNone(self.store.get(self.b['id'], row['id']))
        self.assertFalse(self.store.delete(self.b['id'], row['id']))
        second, duplicate = self.store.add(self.a['id'], 'renamed.pdf', PDF)
        self.assertTrue(duplicate)
        self.assertEqual(row['id'], second['id'])
        third, duplicate = self.store.add(self.b['id'], 'invoice.pdf', PDF)
        self.assertFalse(duplicate)
        self.assertNotEqual(row['id'], third['id'])
        self.assertTrue(self.store.delete(self.a['id'], row['id']))
        self.assertFalse(self.store.delete(self.a['id'], row['id']))
        self.assertIsNotNone(self.store.get(self.b['id'], third['id']))

    def test_type_detection_and_invalid_input(self):
        self.assertEqual(detect_type(b'\xff\xd8\xfftest'), 'image/jpeg')
        self.assertEqual(detect_type(b'\x89PNG\r\n\x1a\ntest'), 'image/png')
        self.assertEqual(detect_type(b'RIFF0000WEBPtest'), 'image/webp')
        for data in (b'', b'<script>alert(1)</script>', b'%PDF-' + b'x' * MAX_FILE_BYTES):
            with self.assertRaises(ValueError):
                self.store.add(self.a['id'], 'invoice.pdf', data)
        for filename in ('', 'x\n.pdf', 'x' * 201):
            with self.assertRaises(ValueError):
                self.store.add(self.a['id'], filename, PDF)
        with self.assertRaises(ValueError):
            self.store.add(self.a['id'], 'a.pdf', PDF, 'n' * 2001)
        with self.assertRaises(ValueError):
            self.store.create_business(' ')
        row, _ = self.store.add(self.a['id'], '../../safe.pdf', PDF)
        self.assertEqual(row['filename'], 'safe.pdf')

    def test_quota_and_pagination(self):
        with patch('invoices.store.MAX_BUSINESS_BYTES', len(PDF)):
            self.store.add(self.a['id'], 'a.pdf', PDF)
            with self.assertRaises(QuotaExceeded):
                self.store.add(self.a['id'], 'b.pdf', PDF + b'2')
            self.assertTrue(self.store.add(self.a['id'], 'a.pdf', PDF)[1])
        self.store.add(self.a['id'], 'b.pdf', PDF + b'2')
        listing = self.store.list(self.a['id'], offset=1, limit=1)
        self.assertEqual(listing['total'], 2)
        self.assertEqual(len(listing['invoices']), 1)
        with self.assertRaises(ValueError):
            self.store.list(self.a['id'], offset=-1)

    def test_concurrent_duplicates_are_atomic(self):
        results, errors = [], []
        def upload():
            try:
                results.append(self.store.add(self.a['id'], 'a.pdf', PDF))
            except Exception as error:
                errors.append(error)
        threads = [threading.Thread(target=upload) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(errors)
        self.assertEqual(len(results), 4)
        self.assertEqual(sum(not duplicate for _, duplicate in results), 1)
        self.assertEqual(self.store.list(self.a['id'])['total'], 1)


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.a = self.store.create_business('Acme')
        self.b = self.store.create_business('Other')
        self.server = Server(('127.0.0.1', 0), self.store)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 5)
        self.addCleanup(self.server.shutdown)

    def request(self, path, method='GET', data=None, token=None, headers=None):
        actual = {'Origin': self.server.origin}
        if token is not None:
            actual['Authorization'] = 'Bearer ' + token
        actual.update(headers or {})
        request = Request(self.server.origin + path, data=data, method=method, headers=actual)
        try:
            response = urlopen(request, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def upload(self, data=PDF, **headers):
        return self.request('/api/invoices', 'POST', data, self.a['token'],
                            {'X-Filename': quote('invoice.pdf'), **headers})

    def test_static_assets_and_security_headers(self):
        for path in ('/', '/app.js', '/style.css'):
            status, headers, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertTrue(body)
            self.assertEqual(headers['Cache-Control'], 'no-store')
            self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
            self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
            self.assertNotIn(self.a['token'].encode(), body)
        self.assertEqual(self.request('/../../README.md')[0], 404)
        self.assertEqual(self.request('/invoices.sqlite')[0], 404)

    def test_auth_host_and_origin_enforcement(self):
        for path, method, data in [('/api/invoices', 'GET', None),
                                   ('/api/invoices', 'POST', PDF),
                                   ('/api/invoices/' + 'a' * 32, 'DELETE', None)]:
            self.assertEqual(self.request(path, method, data)[0], 401)
        self.assertEqual(self.request('/api/me', token=self.a['token'], headers={'Host': 'evil.example'})[0], 403)
        self.assertEqual(self.upload(Origin='https://evil.example')[0], 403)
        self.assertEqual(self.upload(Origin='null')[0], 403)
        self.assertEqual(self.upload(Origin='')[0], 403)
        status, _, body = self.request('/api/me', token=self.a['token'])
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['name'], 'Acme')

    def test_upload_duplicate_download_and_delete(self):
        status, _, body = self.upload(**{'X-Filename': quote('café.pdf'), 'X-Notes': quote('Paid today')})
        self.assertEqual(status, 201)
        invoice = json.loads(body)['invoice']
        path = '/api/invoices/' + invoice['id']
        self.assertEqual(self.upload()[0], 200)
        status, headers, document = self.request(path, token=self.a['token'])
        self.assertEqual(status, 200)
        self.assertEqual(document, PDF)
        self.assertEqual(headers['Content-Type'], 'application/pdf')
        self.assertTrue(headers['Content-Disposition'].startswith('attachment;'))
        self.assertIn('caf%C3%A9.pdf', headers['Content-Disposition'])
        self.assertEqual(self.request(path, token=self.b['token'])[0], 404)
        self.assertEqual(self.request(path, 'DELETE', token=self.b['token'])[0], 404)
        status, _, body = self.request('/api/invoices', token=self.b['token'])
        self.assertEqual(json.loads(body)['total'], 0)
        self.assertEqual(self.request(path, 'DELETE', token=self.a['token'])[0], 200)
        self.assertEqual(self.request(path, token=self.a['token'])[0], 404)

    def test_validation_and_limits(self):
        self.assertEqual(self.upload(b'<html>not a PDF</html>')[0], 400)
        self.assertEqual(self.upload(b'')[0], 413)
        self.assertEqual(self.upload(**{'Content-Length': str(MAX_FILE_BYTES + 1)})[0], 413)
        self.assertEqual(self.upload(**{'Content-Length': '-1'})[0], 413)
        self.assertEqual(self.upload(**{'Transfer-Encoding': 'chunked'})[0], 400)
        self.assertEqual(self.upload(**{'X-Filename': quote('bad\nname.pdf')})[0], 400)
        self.assertEqual(self.request('/api/invoices?offset=-1', token=self.a['token'])[0], 400)
        self.assertEqual(self.request('/api/invoices?offset=oops', token=self.a['token'])[0], 400)
        with patch('invoices.store.MAX_BUSINESS_BYTES', 1):
            self.assertEqual(self.upload()[0], 413)
        self.assertEqual(self.store.list(self.a['id'])['total'], 0)

    def test_no_direct_public_bind_or_invalid_origin(self):
        with self.assertRaises(ValueError):
            Server(('0.0.0.0', 0), self.store)
        for origin in ('http://example.com', 'https://example.com/path', 'https://user:pass@example.com'):
            with self.assertRaises(ValueError):
                Server(('127.0.0.1', 0), self.store, origin)

    def test_database_errors_are_sanitized(self):
        with patch.object(self.store, 'list', side_effect=sqlite3.OperationalError('private database path')):
            status, _, body = self.request('/api/invoices', token=self.a['token'])
        self.assertEqual(status, 503)
        self.assertNotIn(b'private database path', body)


if __name__ == '__main__':
    unittest.main()
