"""Transactional invoice storage; business tokens are stored only as hashes."""
import hashlib
import secrets
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .extraction import FIELDS, extract_invoice

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_BUSINESS_BYTES = 250 * 1024 * 1024
EXTRA_COLUMNS = FIELDS + ('extraction_status', 'extraction_message', 'reviewed_at')
PUBLIC_COLUMNS = 'id, filename, content_type, created_at, notes, sha256, size, ' + ', '.join(EXTRA_COLUMNS)


class QuotaExceeded(ValueError):
    pass


def detect_type(data):
    """Identify an allowed upload from its bytes, not its filename or MIME claim."""
    """Basic signature checks, NOT malware scanning or full format validation."""
    if data.startswith(b'%PDF-'):
        return 'application/pdf'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    raise ValueError('Upload a PDF, JPEG, PNG, or WebP file.')


class Store:
    """Own the SQLite schema and enforce business-level data isolation."""
    def __init__(self, folder):
        self.folder = Path(folder).expanduser().resolve()
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.folder / 'invoices.sqlite'
        # This dedicated directory must not be shared with other local users.
        self.folder.chmod(0o700)
        with closing(self.connect()) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS businesses (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS invoices (
                    id TEXT PRIMARY KEY,
                    business_id TEXT NOT NULL REFERENCES businesses(id),
                    filename TEXT NOT NULL,
                    content_type TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    document BLOB NOT NULL,
                    UNIQUE(business_id, sha256)
                );
                CREATE INDEX IF NOT EXISTS invoice_business_date
                    ON invoices(business_id, created_at DESC);
            ''')
            existing = {row['name'] for row in db.execute('PRAGMA table_info(invoices)')}
            for column in EXTRA_COLUMNS:
                if column not in existing:
                    default = 'not_extracted' if column == 'extraction_status' else ''
                    db.execute(f"ALTER TABLE invoices ADD COLUMN {column} TEXT NOT NULL DEFAULT '{default}'")
        self.path.chmod(0o600)

    def connect(self):
        """Open a short-lived SQLite connection with foreign keys enabled."""
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys = ON')
        db.execute('PRAGMA secure_delete = ON')
        return db

    def create_business(self, name):
        """Create an account and return its plaintext key exactly once."""
        name = name.strip()
        if not name or len(name) > 160:
            raise ValueError('Business name must contain 1 to 160 characters.')
        token = secrets.token_urlsafe(32)
        key = uuid.uuid4().hex
        with closing(self.connect()) as db, db:
            db.execute('INSERT INTO businesses VALUES (?, ?, ?)',
                       (key, name, hashlib.sha256(token.encode()).hexdigest()))
        return {'id': key, 'name': name, 'token': token}

    def authenticate(self, token):
        """Authenticate by hashing the presented key and comparing its digest."""
        if not isinstance(token, str) or not 20 <= len(token) <= 200:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with closing(self.connect()) as db:
            row = db.execute('SELECT id, name FROM businesses WHERE token_hash = ?',
                             (digest,)).fetchone()
        return dict(row) if row else None

    def add(self, business_id, filename, data, notes=''):
        """Validate, extract, deduplicate, and transactionally store one document."""
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_FILE_BYTES:
            raise ValueError('Each file must be nonempty and at most 10 MiB.')
        kind = detect_type(data)
        if not isinstance(filename, str) or not isinstance(notes, str):
            raise ValueError('Filename and notes must be text.')
        filename = filename.replace('\\', '/').rsplit('/', 1)[-1].strip()
        if not filename or len(filename) > 200 or any(ord(c) < 32 or ord(c) == 127 for c in filename):
            raise ValueError('Use a filename of 1 to 200 characters without control characters.')
        if len(notes) > 2000:
            raise ValueError('Notes must be at most 2,000 characters.')
        digest = hashlib.sha256(data).hexdigest()
        with closing(self.connect()) as db:
            old = db.execute(f'SELECT {PUBLIC_COLUMNS} FROM invoices WHERE business_id = ? AND sha256 = ?',
                             (business_id, digest)).fetchone()
            if old:
                return dict(old), True
        extraction = extract_invoice(data, kind)
        with closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute(f'SELECT {PUBLIC_COLUMNS} FROM invoices WHERE business_id = ? AND sha256 = ?',
                             (business_id, digest)).fetchone()
            if old:
                return dict(old), True
            total = db.execute('SELECT COALESCE(SUM(size), 0) FROM invoices WHERE business_id = ?',
                               (business_id,)).fetchone()[0]
            if total + len(data) > MAX_BUSINESS_BYTES:
                raise QuotaExceeded('This business has reached its 250 MiB storage limit.')
            key = uuid.uuid4().hex
            created = datetime.now(timezone.utc).isoformat()
            db.execute('INSERT INTO invoices (id, business_id, filename, content_type, created_at, notes, sha256, size, document) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (key, business_id, filename, kind, created, notes, digest, len(data), data))
            columns = tuple(extraction)
            db.execute('UPDATE invoices SET ' + ', '.join(c + ' = ?' for c in columns) + ' WHERE id = ?',
                       tuple(extraction[c] for c in columns) + (key,))
            row = db.execute(f'SELECT {PUBLIC_COLUMNS} FROM invoices WHERE id = ?', (key,)).fetchone()
        return dict(row), False

    def list(self, business_id, offset=0, limit=50):
        """Return one paginated business-owned library view plus usage totals."""
        if not 0 <= offset or not 1 <= limit <= 100:
            raise ValueError('Invalid page.')
        with closing(self.connect()) as db, db:
            db.execute('BEGIN')
            rows = db.execute(f'SELECT {PUBLIC_COLUMNS} FROM invoices WHERE business_id = ? '
                              'ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?',
                              (business_id, limit, offset)).fetchall()
            count, size = db.execute('SELECT COUNT(*), COALESCE(SUM(size), 0) FROM invoices WHERE business_id = ?',
                                     (business_id,)).fetchone()
        return {'invoices': [dict(row) for row in rows], 'total': count,
                'bytes': size, 'offset': offset, 'limit': limit}

    def get(self, business_id, invoice_id):
        with closing(self.connect()) as db:
            row = db.execute('SELECT * FROM invoices WHERE business_id = ? AND id = ?',
                             (business_id, invoice_id)).fetchone()
        return dict(row) if row else None

    def delete(self, business_id, invoice_id):
        with closing(self.connect()) as db, db:
            return db.execute('DELETE FROM invoices WHERE business_id = ? AND id = ?',
                              (business_id, invoice_id)).rowcount == 1

    def save_fields(self, business_id, invoice_id, fields):
        """Validate and persist a human-reviewed set of four invoice fields."""
        if not isinstance(fields, dict) or set(fields) != set(FIELDS):
            raise ValueError('Supply exactly the four invoice fields.')
        cleaned = {}
        for key, value in fields.items():
            if not isinstance(value, str) or len(value) > 160 or any(ord(c) < 32 or ord(c) == 127 for c in value):
                raise ValueError('Fields must be text of at most 160 characters.')
            cleaned[key] = value.strip()
        if cleaned['invoice_date']:
            from datetime import date
            if date.fromisoformat(cleaned['invoice_date']).isoformat() != cleaned['invoice_date']:
                raise ValueError('Use YYYY-MM-DD for invoice dates.')
        with closing(self.connect()) as db, db:
            db.execute('UPDATE invoices SET ' + ', '.join(c + ' = ?' for c in FIELDS) +
                       ", extraction_status = 'reviewed', extraction_message = 'Manually reviewed; blank fields remain unspecified.', reviewed_at = ? WHERE business_id = ? AND id = ?",
                       tuple(cleaned[c] for c in FIELDS) + (datetime.now(timezone.utc).isoformat(), business_id, invoice_id))
            row = db.execute(f'SELECT {PUBLIC_COLUMNS} FROM invoices WHERE business_id = ? AND id = ?',
                             (business_id, invoice_id)).fetchone()
        return dict(row) if row else None

    def reextract(self, business_id, invoice_id):
        """Refresh suggestions without overwriting fields already reviewed by a person."""
        item = self.get(business_id, invoice_id)
        if not item:
            return None
        if not item['reviewed_at']:
            extracted = extract_invoice(item['document'], item['content_type'])
            with closing(self.connect()) as db, db:
                db.execute('UPDATE invoices SET ' + ', '.join(c + ' = ?' for c in extracted) +
                           " WHERE business_id = ? AND id = ? AND reviewed_at = ''",
                           tuple(extracted.values()) + (business_id, invoice_id))
        with closing(self.connect()) as db:
            row = db.execute(f'SELECT {PUBLIC_COLUMNS} FROM invoices WHERE business_id = ? AND id = ?',
                             (business_id, invoice_id)).fetchone()
        return dict(row) if row else None
