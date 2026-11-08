"""The quotation that was made in Excel, kept on the lead it is for.

CRM-FINAL-CLOSEOUT-01 §7: "Quote upload: attach an uploaded quote PDF
to its lead until the quote engine is ready."

WHY THIS IS NOT A WORKAROUND

Quote builder v2 builds a quotation from the CRM's own figures and
prints it on the letterhead. Until every vertical's pricing is in the
CRM, most quotations are still made in Excel and mailed from Outlook,
and the lead record then holds no copy of the thing the customer was
actually sent. When the customer argues about a number three weeks
later, the only copy is in somebody's Sent items.

So this stores the file against the lead. It is the smallest honest
version of the feature: a file, who attached it, when, and the number
it went out under. It computes nothing and it prints nothing.

WHAT IT WILL NOT DO

It does not move the lead to Quoted. A stage change is a commercial
statement — it starts the quote-ageing clock and it shows up in the
funnel — and inferring one from a file somebody attached would move
leads on the strength of a draft. The screen offers the stage change
beside the upload; a person makes it.

It does not touch the value columns. `quote_value_num` is governed by
`value_gate`, with a margin floor and an approval limit behind it;
writing a figure in from an upload form would walk straight past
them. The value, if somebody types it, goes in the note where it is
read by people and acted on by nothing.

It writes `quote_no` and `quote_date` only where the lead has none.
Those two are what §5's date ladder and the reports read, so filling
a blank is worth doing — and overwriting one is how the CRM comes to
disagree with the quotation register about what went out when.
"""
from __future__ import annotations

import os
from datetime import date, datetime

#: Where the files go. Beside the other upload roots rather than
#: inside the repository, so a pull cannot remove a customer's
#: quotation.
STORAGE_ENV = 'LEAD_QUOTE_STORAGE_ROOT'
DEFAULT_ROOT = '/var/www/procam-crm/uploads/lead_quotes'

#: `lead_attachments.source` for these. Kept in step with
#: `app.lead_facts.routes.QUOTE_SOURCE`.
SOURCE = 'quote'


class Refused(ValueError):
    """The upload was understood and declined, with a reason to show."""


def storage_root():
    return os.environ.get(STORAGE_ENV) or DEFAULT_ROOT


def _parse_date(raw):
    """A date from what a form field holds, or None.

    Refuses rather than guesses on anything else: a quotation dated
    by a misread string is worse than one with no date, because the
    ageing report believes it.
    """
    text = (str(raw or '')).strip()
    if not text:
        return None
    for shape in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y'):
        try:
            return datetime.strptime(text, shape).date()
        except ValueError:
            continue
    raise Refused(f'{text!r} is not a date this understands. '
                  f'Use YYYY-MM-DD.')


def listing(lead):
    """The quotations attached to this lead, newest first."""
    from app import LeadAttachment

    rows = (LeadAttachment.query
            .filter_by(lead_id=lead.id, source=SOURCE)
            .order_by(LeadAttachment.uploaded_at.desc())
            .all())
    out = []
    for row in rows:
        body = row.to_dict()
        body['on_disk'] = bool(row.storage_path
                               and os.path.exists(row.storage_path))
        out.append(body)
    return out


def _unique_path(folder, name):
    """A path in `folder` for `name` that is not already taken.

    Never overwrites. Two revisions are routinely both called
    `Quotation.pdf`, and the second quietly replacing the first is
    how a lead ends up holding one file and two revisions' worth of
    history.
    """
    path = os.path.join(folder, name)
    stem, n = path, 1
    while os.path.exists(path):
        base, ext = os.path.splitext(stem)
        path = f'{base}_{n}{ext}'
        n += 1
    return path


def attach(lead, *, filename, blob, content_type='', quote_no=None,
           quote_date=None, note=None, actor=None):
    """Store the file against the lead. Returns what changed.

    One transaction: the row, the stamps, the timeline entry and the
    audit row either all happen or none do. A file on disk with no
    row pointing at it is litter; a row pointing at no file is a
    broken download, and the second is the one people report.
    """
    from app import LeadActivity, LeadAttachment, db
    from app.services import audit

    if lead is None:
        raise Refused('There is no such lead.')
    if not blob:
        raise Refused('That file is empty.')

    wanted_date = _parse_date(quote_date)
    number = (str(quote_no or '')).strip()[:60]

    folder = os.path.join(storage_root(), str(lead.id))
    os.makedirs(folder, exist_ok=True)
    path = _unique_path(folder, filename)
    with open(path, 'wb') as fh:
        fh.write(blob)

    stamped = {}
    try:
        row = LeadAttachment(
            lead_id=lead.id, filename=os.path.basename(path),
            content_type=(content_type or '')[:120] or None,
            size_bytes=len(blob), storage_path=path, source=SOURCE)
        db.session.add(row)

        # The two stamps, and only into a blank. See the docstring.
        if number and not (getattr(lead, 'quote_no', None) or '').strip():
            lead.quote_no = number
            stamped['quote_no'] = number
        if wanted_date and getattr(lead, 'quote_date', None) is None:
            lead.quote_date = wanted_date
            stamped['quote_date'] = wanted_date.isoformat()

        said = (str(note or '')).strip()
        body = [f'Quotation attached: {os.path.basename(path)}']
        if number:
            body.append(f'Quote number: {number}')
        if wanted_date:
            body.append(f'Quote date: {wanted_date.isoformat()}')
        if said:
            body.append(said)
        db.session.add(LeadActivity(
            lead_id=lead.id, kind='quote',
            subject=f'Quotation attached: {os.path.basename(path)}'[:255],
            body='\n'.join(body), performed_by=actor,
            occurred_at=datetime.utcnow()))

        db.session.flush()
        audit.record('lead.quote_file_attached', 'lead', str(lead.id),
                     new={'attachment_id': row.id,
                          'filename': row.filename,
                          'size_bytes': row.size_bytes,
                          'stamped': stamped},
                     actor=actor,
                     reason=(f'quotation attached to lead {lead.id} '
                             f'pending the quote engine')[:400])
        db.session.commit()
    except Exception:
        db.session.rollback()
        # The row did not land, so the file is litter. Remove it, and
        # if that fails too, leave it rather than raising a second
        # error over the first — the caller needs the original.
        try:
            os.remove(path)
        except OSError:
            pass
        raise

    return {
        'attachment': row.to_dict(),
        'stamped': stamped,
        'files': listing(lead),
        #: Said rather than done. See the docstring.
        'stage_hint': _stage_hint(lead),
    }


def _stage_hint(lead):
    """Whether the screen should offer to move the lead to Quoted."""
    from app.services import stage_map

    stage = stage_map.canonical(getattr(lead, 'stage', None))
    if stage in stage_map.TERMINAL:
        return ''
    if stage == 'Quoted':
        return ''
    try:
        index = stage_map.JOURNEY.index(stage)
        quoted = stage_map.JOURNEY.index('Quoted')
    except ValueError:
        return ''
    if index >= quoted:
        return ''
    return (f'This lead is on {stage}. A quotation has gone out — '
            f'move it to Quoted if that is what happened.')
