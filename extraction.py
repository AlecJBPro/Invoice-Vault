"""Local, conservative invoice suggestions. Document text is data, never code."""
import io
import json
import re
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

FIELDS = ('invoice_date', 'invoice_number', 'total_quoted_amount', 'customer_name')
SLOTS = threading.BoundedSemaphore(2)
MAX_PAGES = 40
MAX_TEXT = 200_000
LABELS = {
    'invoice_date': (r'invoice\s+date', r'date\s+of\s+invoice', r'date'),
    'invoice_number': (r'invoice\s*(?:number|no\.?|#)', r'inv\.?\s*(?:number|no\.?|#)'),
    'total_quoted_amount': (r'total\s+quoted\s+amount', r'quoted\s+(?:total|amount)', r'quote\s+total', r'grand\s+total', r'invoice\s+total', r'total\s+amount', r'total'),
    # A bare Customer label needs a colon or its own cell/line; otherwise
    # company names such as Customer LLC are mistaken for field labels.
    'customer_name': (r'customer\s+name', r'bill(?:ed)?\s+to', r'sold\s+to', r'customer(?=\s*:|\s*$)'),
}
ALL_LABELS = '(?:' + '|'.join(p for group in LABELS.values() for p in group) + r'|due\s+date|ship\s+to|subtotal|tax|balance\s+due|amount\s+due|payment\s+terms)'


def result(status, message, fields=None):
    """Build the stable extraction response shape used by the API and UI."""
    return {**dict.fromkeys(FIELDS, ''), **(fields or {}),
            'extraction_status': status, 'extraction_message': message}


def normalize_date(value):
    """Normalize supported printed dates to the database format, YYYY-MM-DD."""
    value = value.strip().replace(',', '')
    for fmt in ('%Y-%m-%d', '%Y/%m/%d', '%m/%d/%Y', '%m-%d-%Y',
                '%B %d %Y', '%b %d %Y', '%d %B %Y', '%d %b %Y'):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return ''


# The parser is intentionally label-based and conservative: it prefers a blank
# suggestion over silently assigning a nearby value to the wrong field.
def candidates(text, labels):
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    label_start = re.compile(r'^' + ALL_LABELS + r'(?:\b|:)', re.I)
    for label in labels:
        found = []
        # A trailing hash is itself a delimiter, including Invoice #TEST-1001.
        pattern = re.compile(r'^' + label + r'(?:\s*[:#]\s*|\s+|(?<=#)|$)(.*)$', re.I)
        for index, line in enumerate(lines):
            cells = list(re.finditer(r'\S(?:.*?\S)?(?=\s{2,}|\t|$)', line))
            for position, cell in enumerate(cells):
                match = pattern.match(cell.group())
                if not match:
                    continue
                value = match.group(1).strip()
                if not value and position + 1 < len(cells):
                    following = cells[position + 1].group()
                    if not label_start.match(following):
                        value = following
                if not value and index + 1 < len(lines):
                    # Only consider a value aligned under this label, not another column.
                    below = list(re.finditer(r'\S(?:.*?\S)?(?=\s{2,}|\t|$)', lines[index + 1]))
                    aligned = [item.group() for item in below if abs(item.start() - cell.start()) <= 2]
                    if len(aligned) == 1:
                        value = aligned[0]
                if value and not label_start.match(value):
                    found.append(value)
        if found:
            return found
    return []


def parse_text(text):
    """Extract the four supported fields from a text layer without guessing."""
    fields = {}
    for field, labels in LABELS.items():
        values = set()
        for value in candidates(text[:MAX_TEXT], labels):
            if field == 'invoice_date':
                value = normalize_date(value)
            elif field == 'invoice_number':
                value = value if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/\-]{0,99}', value) else ''
            elif field == 'total_quoted_amount':
                # Preserve the printed amount and currency; do not use binary floats,
                # infer a currency, or mistake a balance/subtotal for an invoice total.
                value = value if re.fullmatch(r'(?:(?:USD|CAD|EUR|GBP|AUD)\s*)?[$€£]?\s*-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{2})?(?:\s*(?:USD|CAD|EUR|GBP|AUD))?', value, re.I) else ''
            else:
                value = value if len(value) <= 160 and not re.search(r'\s' + ALL_LABELS + r'\s*:', value, re.I) else ''
            if value:
                values.add(value)
        fields[field] = values.pop() if len(values) == 1 else ''
    return result('needs_review', 'Suggestions only: check all four fields against the original. Missing or conflicting values are blank. Numeric dates use month/day/year.', fields)


def extract_invoice(data, content_type):
    """Run bounded extraction in a separate worker process for PDF uploads."""
    if content_type != 'application/pdf':
        return result('needs_ocr', 'Photo/image extraction is not enabled. Enter the fields manually.')
    if not SLOTS.acquire(blocking=False):
        return result('unavailable', 'Extraction is busy. Use Extract fields to retry, or enter the fields manually.')
    try:
        run = subprocess.run([sys.executable, '-I', str(Path(__file__).resolve()), '--worker'],
                             input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=12, check=False)
        if run.returncode != 0 or len(run.stdout) > 8192:
            return result('failed', 'PDF extraction failed or exceeded resource limits. The original is saved; enter fields manually.')
        parsed = json.loads(run.stdout)
        if not isinstance(parsed, dict) or set(parsed) != set(FIELDS) | {'extraction_status', 'extraction_message'} or not all(isinstance(v, str) for v in parsed.values()):
            raise ValueError('Invalid worker output')
        return parsed
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return result('failed', 'PDF extraction was unavailable or timed out. The original is saved; retry or enter fields manually.')
    finally:
        SLOTS.release()


def worker(data):
    """Read and parse a PDF inside the restricted child process."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (8, 8))
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    except (ImportError, ValueError, OSError):
        pass  # Wall-clock timeout remains enforced on platforms without resource.
    try:
        from pypdf import PdfReader
    except ImportError:
        return result('unavailable', 'PDF extraction dependency is missing. Install requirements.txt in the server environment.')
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            return result('encrypted', 'Password-protected PDF: upload an unlocked copy or enter fields manually.')
        if len(reader.pages) > MAX_PAGES:
            return result('limited', 'PDF exceeds the 40-page extraction limit. Enter fields manually.')
        chunks, size = [], 0
        for page in reader.pages:
            chunk = (page.extract_text(extraction_mode='layout') or '') if page.get_contents() is not None else ''
            size += len(chunk)
            if size > MAX_TEXT:
                return result('limited', 'PDF exceeds the text extraction limit. Enter fields manually.')
            chunks.append(chunk)
        text = '\n'.join(chunks)
        if not text.strip():
            return result('needs_ocr', 'No readable text layer found. Scanned PDFs need OCR, which is not enabled; enter fields manually.')
        return parse_text(text)
    except Exception:
        return result('failed', 'This PDF could not be read. The original is saved; enter fields manually.')


if __name__ == '__main__':
    print(json.dumps(worker(sys.stdin.buffer.read(10 * 1024 * 1024 + 1))))
