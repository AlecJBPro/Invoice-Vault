"""Deterministic fictional demo seed; no PDF dependency or uploaded input."""
import hashlib
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from .store import Store

OWNER = 'd' * 32
BUSINESS = {'id': OWNER, 'name': 'Example Demo Company (fictional)', 'demo': True}
MARKER = 'invoice-vault-fictional-demo-v1\n'


def seed_documents():
    """Generate byte-identical PDFs and metadata using only the standard library."""
    for index, customer in enumerate(('Example Customer LLC', 'Example Studio LLC', 'Example Workshop LLC'), 1):
        fields = dict(invoice_date='2026-09-11', invoice_number=f'DEMO-{index:04d}',
                      total_quoted_amount=f'USD {index * 100}.00', customer_name=customer)
        lines = ['FICTIONAL DEMO INVOICE - NOT PAYABLE', 'Seller: Example Demo Company',
                 'Invoice Date: ' + fields['invoice_date'], 'Invoice Number: ' + fields['invoice_number'],
                 'Bill To: ' + customer, 'Total Quoted Amount: ' + fields['total_quoted_amount']]
        stream = ('BT /F1 12 Tf 50 740 Td 22 TL\n' + '\n'.join(
            '(' + line + ') Tj T*' for line in lines) + '\nET').encode('ascii')
        objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
                   b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
                   b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
                   b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
                   b'<< /Length ' + str(len(stream)).encode() + b' >>\nstream\n' + stream + b'\nendstream']
        pdf, offsets = b'%PDF-1.4\n', [0]
        for number, obj in enumerate(objects, 1):
            offsets.append(len(pdf))
            pdf += str(number).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
        start = len(pdf)
        pdf += b'xref\n0 6\n0000000000 65535 f \n'
        pdf += b''.join(f'{offset:010d} 00000 n \n'.encode() for offset in offsets[1:])
        pdf += f'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode()
        yield f'{index:032x}', fields, pdf


class DemoStore(Store):
    """A dedicated marked directory, seeded atomically; all normal writes denied."""
    def __init__(self, folder):
        folder = Path(folder).expanduser().resolve()
        if folder == (Path.home() / '.invoice-vault').resolve():
            raise ValueError('Demo requires a separate data directory.')
        marker = folder / '.invoice-vault-demo'
        if folder.exists() and any(folder.iterdir()) and (not marker.is_file() or marker.read_text() != MARKER):
            raise ValueError('Demo refuses a nonempty directory without its demo marker.')
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker.write_text(MARKER)
        self.reset_lock = threading.Lock()
        super().__init__(folder)
        self.reset()

    def reset(self):
        with self.reset_lock, closing(self.connect()) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM invoices')
            db.execute('DELETE FROM businesses')
            db.execute('INSERT INTO businesses VALUES (?, ?, ?)', (OWNER, BUSINESS['name'], 'disabled-demo-auth'))
            for key, fields, document in seed_documents():
                db.execute('INSERT INTO invoices (id, business_id, filename, content_type, created_at, notes, sha256, size, document, invoice_date, invoice_number, total_quoted_amount, customer_name, extraction_status, extraction_message, reviewed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                           (key, OWNER, fields['invoice_number'] + '.pdf', 'application/pdf',
                            '2026-09-11T12:00:00+00:00', 'Fictional demo only; not payable.',
                            hashlib.sha256(document).hexdigest(), len(document), document,
                            *fields.values(), 'reviewed', 'Fictional seeded fields.', '2026-09-11T12:00:00+00:00'))
            self.reset_day = datetime.now(timezone.utc).date()

    def reset_if_due(self):
        if datetime.now(timezone.utc).date() != self.reset_day:
            self.reset()

    def authenticate(self, token):
        return dict(BUSINESS)

    def deny_write(self, *args, **kwargs):
        raise ValueError('Demo is read-only.')

    add = delete = save_fields = reextract = create_business = deny_write
