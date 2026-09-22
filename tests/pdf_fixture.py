"""Generate a fictional text PDF without external files or customer data."""
import io
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

TEXT = ('FICTIONAL TEST INVOICE - NOT PAYABLE\n'
        'Seller: Example Supply Company\n'
        'Invoice Date: September 11, 2026\n'
        'Invoice Number: TEST-1001\n'
        'Bill To:\nExample Customer LLC\n'
        'Subtotal: USD 1,000.00\nTax: USD 80.00\n'
        'Total Quoted Amount: USD 1,080.00\n'
        'Due Date: October 11, 2026\nBalance Due: USD 500.00')


def make_pdf(text=TEXT, encrypted=False, blank=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    if not blank:
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                                 NameObject('/Subtype'): NameObject('/Type1'),
                                 NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): font})})
        commands = ['BT /F1 12 Tf 50 740 Td 20 TL']
        for line in text.splitlines():
            escaped = line.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
            commands.append('(' + escaped + ') Tj T*')
        commands.append('ET')
        stream = DecodedStreamObject()
        stream.set_data('\n'.join(commands).encode('latin-1'))
        page[NameObject('/Contents')] = stream
    if encrypted:
        writer.encrypt('fictional-test-password')
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()
