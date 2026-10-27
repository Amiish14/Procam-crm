"""
The quotation exactly as the customer received it.

CRM-QUOTE-REV-02 §5.5.

The PDF is generated on every request from whatever the quote
says now. So the document a customer is holding and the document
the CRM would print today are two different things the moment
anybody edits a line — and in an argument about what was quoted,
the CRM has no answer.

So the bytes are kept, once, against the revision they belong to.
Not regenerated: a regenerated PDF is a reconstruction, and the
whole point is to have the original.

Stored on disk beside the handover packs rather than in the
database. A 200 KB blob per revision in SQLite would bloat every
backup of a table that is read constantly.
"""
from __future__ import annotations

import hashlib
import os
import re

#: Beside ff_handover's packs, under the repo root.
ARCHIVE_DIR = os.path.join('data', 'quote_pdfs')


def _root():
    here = os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(here, ARCHIVE_DIR)


def path_for(quote):
    """Where this revision's PDF lives. One file per revision."""
    from app.services import quote_rev

    name = quote_rev.display_number(quote) or (
        quote.quote_number or f'quote-{quote.id}')
    safe = re.sub(r'[^A-Za-z0-9._-]+', '_', str(name)).strip('_')
    return os.path.join(_root(), f'{safe}__{quote.id}.pdf')


def store(quote, data, *, actor, sent_to=''):
    """Keep these bytes as what went out. (stored, detail).

    Never overwrites. A second send of the same revision is the
    same document; if it is not, the revision should have been a
    new one, and silently replacing the first would destroy the
    only record of what the customer first received.
    """
    from app import db
    from app.services import audit

    if not data:
        return False, 'nothing to store'
    os.makedirs(_root(), exist_ok=True)
    path = path_for(quote)
    digest = hashlib.sha256(data).hexdigest()

    if os.path.exists(path):
        return False, {'path': path, 'why': 'already archived',
                       'sha256': digest}

    with open(path, 'wb') as handle:
        handle.write(data)
    try:
        os.chmod(path, 0o640)
    except OSError:
        pass

    quote.sent_pdf_path = path
    quote.sent_pdf_sha256 = digest
    audit.record('quote.pdf_archived', 'quote', quote.id,
                 new={'path': path, 'sha256': digest,
                      'bytes': len(data), 'sent_to': sent_to},
                 actor=actor,
                 reason='the quotation exactly as it went out')
    return True, {'path': path, 'sha256': digest,
                  'bytes': len(data)}


def archived(quote):
    """(path, sha256) if the sent PDF is on file, else (None, None)."""
    path = getattr(quote, 'sent_pdf_path', None)
    if path and os.path.exists(path):
        return path, getattr(quote, 'sent_pdf_sha256', None)
    return None, None


def render(quote):
    """The PDF bytes for a quote, using the live route's own code.

    Imported lazily and called through the view so there is one
    renderer, not two — a second copy would drift and the archive
    would stop being what the customer saw.
    """
    from flask import current_app

    from app.quote.routes import quote_pdf

    with current_app.test_request_context(f'/quotes/{quote.id}/pdf'):
        from flask import session
        session['emp_code'] = 'system'
        resp = quote_pdf(quote.id)
    data = getattr(resp, 'data', None)
    if data and data[:4] == b'%PDF':
        return data
    return None
