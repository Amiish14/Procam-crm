"""
Reading what an attachment actually says — §20.

"Please find attached" is the entire body of a great many real
enquiries; the requirement is in the spreadsheet. Filenames already
carry some signal — RFQ_Heavy_Transport.xlsx is recognisable — but the
route, the tonnage and the cargo are inside.

Everything here is best-effort and bounded. An attachment that cannot be
read returns nothing, never an exception: losing a lead because a PDF
was malformed would be a worse failure than not reading it at all.

    extract(path)         → text, whatever the type
    extract_many(paths)   → the texts joined, capped

PDF support needs pypdf. When it is absent the module still reads
spreadsheets and text, and says so rather than failing.
"""
from __future__ import annotations

import csv
import io
import os
import re

#: Per-file and total caps. A tender pack can run to hundreds of pages
#: and the classifier only needs enough text to recognise an enquiry.
MAX_CHARS_PER_FILE = 20000
MAX_CHARS_TOTAL = 60000
MAX_BYTES = 15 * 1024 * 1024

SPREADSHEET = ('.xlsx', '.xlsm', '.xltx')
PLAIN = ('.txt', '.csv', '.tsv', '.md', '.eml')
PDF = ('.pdf',)

READABLE = SPREADSHEET + PLAIN + PDF


def pdf_supported():
    try:
        import pypdf                                          # noqa: F401
        return True
    except Exception:
        return False


def extract(path, *, max_chars=MAX_CHARS_PER_FILE):
    """Text from one file. Empty string when it cannot be read."""
    try:
        if not path or not os.path.isfile(path):
            return ''
        if os.path.getsize(path) > MAX_BYTES:
            return ''
        ext = os.path.splitext(path)[1].lower()
        if ext in SPREADSHEET:
            text = _from_spreadsheet(path)
        elif ext in PDF:
            text = _from_pdf(path)
        elif ext in PLAIN:
            text = _from_plain(path)
        else:
            return ''
        return _tidy(text)[:max_chars]
    except Exception:
        return ''


def extract_many(paths, *, max_total=MAX_CHARS_TOTAL):
    out, used = [], 0
    for p in (paths or []):
        text = extract(p)
        if not text:
            continue
        room = max_total - used
        if room <= 0:
            break
        out.append(text[:room])
        used += min(len(text), room)
    return '\n\n'.join(out)


def _from_spreadsheet(path):
    """Cell values, not formatting.

    read_only and values_only keep a large workbook from being loaded
    whole, and formulas are skipped — "=SUM(B2:B40)" tells the
    classifier nothing.
    """
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    parts = []
    try:
        for sheet in wb.worksheets[:5]:
            parts.append(f'[{sheet.title}]')
            for row in sheet.iter_rows(max_row=300, max_col=40,
                                       values_only=True):
                cells = [str(c).strip() for c in row
                         if c is not None and str(c).strip()]
                if cells:
                    parts.append(' | '.join(cells))
    finally:
        try:
            wb.close()
        except Exception:
            pass
    return '\n'.join(parts)


def _from_pdf(path):
    try:
        import pypdf
    except Exception:
        # Not installed. Silent by design at this level — the caller
        # reports capability through pdf_supported().
        return ''
    text = []
    reader = pypdf.PdfReader(path)
    for page in reader.pages[:30]:
        try:
            text.append(page.extract_text() or '')
        except Exception:
            continue
    return '\n'.join(text)


def _from_plain(path):
    ext = os.path.splitext(path)[1].lower()
    with open(path, 'rb') as fh:
        raw = fh.read(MAX_BYTES)
    body = raw.decode('utf-8', errors='replace')
    if ext in ('.csv', '.tsv'):
        delim = '\t' if ext == '.tsv' else ','
        rows = []
        for row in csv.reader(io.StringIO(body), delimiter=delim):
            cells = [c.strip() for c in row if c and c.strip()]
            if cells:
                rows.append(' | '.join(cells))
            if len(rows) > 300:
                break
        return '\n'.join(rows)
    return body


def _tidy(text):
    text = re.sub(r'[ \t]+', ' ', text or '')
    return re.sub(r'\n\s*\n\s*\n+', '\n\n', text).strip()


def for_lead(lead_id, *, limit=8):
    """Text from the attachments already saved against a lead."""
    try:
        from app import LeadAttachment
        rows = (LeadAttachment.query.filter_by(lead_id=lead_id)
                .limit(limit).all())
        return extract_many([r.storage_path for r in rows
                             if getattr(r, 'storage_path', None)])
    except Exception:
        return ''
