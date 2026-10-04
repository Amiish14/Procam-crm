"""
Keep the client's original message.

The CRM has always stored a *rendering* of an inbound email — subject,
sender, a body truncated at eight thousand characters. That is enough
to read and not enough to forward, so the person who has to quote ends
up asking a colleague to forward the real thing. This module fetches
the message as the client sent it (`GET /messages/{id}/$value`, which
returns RFC-5322 MIME) and writes it next to the lead's attachments as
a `.eml` file that Outlook opens, forwards and replies to normally.

It is deliberately separate from `attachments.py`: the attachments are
the documents, this is the envelope, and a deployment may reasonably
have one without the other.

Failure is recorded rather than retried forever. A message moved out of
the Inbox, deleted, or past the mailbox's retention cannot be fetched,
and a row saying `missing` is a better answer than a job that tries
every night until someone turns it off.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import datetime

log = logging.getLogger(__name__)

#: A raw message bigger than this is not kept. The cap is about disk,
#: not about Graph: a 40 MB mail with a drawings pack would be stored
#: twice, once as the .eml and once as its extracted attachments.
MAX_RAW_BYTES = 25 * 1024 * 1024


def storage_root():
    from . import attachments as _att
    return _att.STORAGE_ROOT


def _dir_for(lead_id):
    return os.path.join(storage_root(), str(int(lead_id)), 'original')


def _filename(internet_message_id, received_at=None, subject=''):
    """A stable, safe name: the date, a little of the subject, a hash.

    The hash of the message id is what makes it stable — capturing the
    same message twice writes the same name, so a re-run overwrites
    rather than littering the directory with copies.
    """
    stamp = (received_at or datetime.utcnow()).strftime('%Y%m%d')
    slug = re.sub(r'[^\w\- ]', '', (subject or 'original'))[:60].strip()
    slug = re.sub(r'\s+', '-', slug) or 'original'
    digest = hashlib.sha256((internet_message_id or slug).encode()).hexdigest()[:10]
    return f'{stamp}-{slug}-{digest}.eml'


def fetch_mime(graph, mailbox, graph_message_id):
    """The original bytes. Raises on an HTTP error, returns None if gone."""
    from urllib.parse import quote

    path = (f'/users/{quote(mailbox)}/messages/'
            f'{quote(graph_message_id, safe="")}/$value')
    resp = graph._request('GET', path)
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise RuntimeError(f'Graph $value failed: HTTP {resp.status_code} — '
                           f'{resp.text[:300]}')
    return resp.content


def graph_id_for(graph, mailbox, internet_message_id):
    """Graph's id for a stored RFC-5322 Message-Id, or None.

    `_get_message_by_internet_id` raises when the message is not in the
    Inbox, because for the webhook that is an error worth shouting
    about — it was told the message exists. Here it is the ordinary
    outcome for anything older than the mailbox's retention, so it is
    turned into None and the caller records it as missing. Letting it
    raise meant the back-fill scored those leads as *failed*, wrote
    nothing, and re-tried the same three hundred of them on every run
    for ever.
    """
    from .webhook import _get_message_by_internet_id
    try:
        msg = _get_message_by_internet_id(graph, mailbox,
                                          internet_message_id)
    except Exception as exc:                                # noqa: BLE001
        log.info('no message for %s in %s: %s',
                 internet_message_id, mailbox, str(exc)[:120])
        return None
    return (msg or {}).get('id')


def capture(lead_id, *, graph, mailbox, graph_message_id=None,
            internet_message_id=None, subject='', from_addr='',
            received_at=None, lead_email_id=None, commit=True,
            known_missing=False):
    """Store one original message against a lead. Returns the row or None.

    Idempotent on `internet_message_id`: capturing the same message
    twice returns the row that is already there.
    """
    from app import db
    from app.models.mailops import LeadRawEmail

    if not (graph_message_id or internet_message_id):
        return None

    existing = None
    if internet_message_id:
        existing = LeadRawEmail.query.filter_by(
            internet_message_id=internet_message_id).first()
        if existing is not None and existing.status == 'stored' \
                and existing.storage_path \
                and os.path.exists(existing.storage_path):
            return existing

    row = existing or LeadRawEmail(
        lead_id=lead_id, internet_message_id=internet_message_id)
    row.lead_id = lead_id
    row.lead_email_id = lead_email_id or row.lead_email_id
    row.subject = (subject or row.subject or '')[:500]
    row.from_addr = (from_addr or row.from_addr or '')[:320]
    row.received_at = received_at or row.received_at
    row.captured_at = datetime.utcnow()

    try:
        gid = None if known_missing else (graph_message_id
                                          or row.graph_message_id)
        if not gid and internet_message_id and not known_missing:
            gid = graph_id_for(graph, mailbox, internet_message_id)
        if not gid:
            row.status, row.error = 'missing', 'not found in the mailbox'
            db.session.add(row)
            if commit:
                db.session.commit()
            return row
        row.graph_message_id = gid[:400]

        content = fetch_mime(graph, mailbox, gid)
        if content is None:
            row.status, row.error = 'missing', 'the mailbox no longer has it'
            db.session.add(row)
            if commit:
                db.session.commit()
            return row
        if len(content) > MAX_RAW_BYTES:
            row.status = 'failed'
            row.error = (f'{len(content)} bytes — over the '
                         f'{MAX_RAW_BYTES} byte cap')
            row.size_bytes = len(content)
            db.session.add(row)
            if commit:
                db.session.commit()
            return row

        folder = _dir_for(lead_id)
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, _filename(internet_message_id or gid,
                                              received_at, subject))
        with open(path, 'wb') as fh:
            fh.write(content)
        try:
            os.chmod(path, 0o640)
        except Exception:
            pass

        row.storage_path = path
        row.size_bytes = len(content)
        row.sha256 = hashlib.sha256(content).hexdigest()
        row.status = 'stored'
        row.error = None
    except Exception as exc:                                  # noqa: BLE001
        log.exception('could not capture the original email for lead %s',
                      lead_id)
        row.status = 'failed'
        row.error = str(exc)[:400]

    db.session.add(row)
    if commit:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            return None
    return row


def for_lead(lead_id):
    """Stored originals for a lead, newest first."""
    from app.models.mailops import LeadRawEmail
    return (LeadRawEmail.query
            .filter_by(lead_id=lead_id)
            .order_by(LeadRawEmail.received_at.desc().nullslast(),
                      LeadRawEmail.id.desc())
            .all())
