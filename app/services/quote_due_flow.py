"""
A due date that moves, and one that passes without an offer.

CRM-COCKPIT-02 §2.4, §2.5 and §2.7. Three things that belong together
because all three are the same date changing state:

  * **an extension.** The client moves the deadline. Old date, new
    date, a reason from a dropdown and the mail it came from, all kept
    in `lead_quote_due_history`, and the lead says "Extended 2×". Not
    an overwrite: "this closed on the 29th, was extended twice, and we
    still did not bid" is a conversation, and a column holding only
    the current date cannot have it.

  * **a proposal.** A later mail on the same thread, or any mail
    carrying the same RA / eRFQ / GeM / tender number, states a new
    date. The CRM does not apply it — a reminder mail restating the
    original deadline would otherwise look like an extension, and a
    corrigendum nobody has read would move a date under the owner's
    feet. It is offered, with one tap to accept.

  * **a miss.** The deadline passes with no quote recorded. The lead is
    flagged, the owner, the vertical head and Nilesh are told once, and
    somebody records Bid or Not bid with a reason. Five Indo Tech
    auctions closed in October with nothing recorded either way, which
    is why §6.7 exists.
"""
from __future__ import annotations

import logging
from datetime import datetime

log = logging.getLogger(__name__)

#: §2.5 — a date read off a later mail, waiting for the owner.
PROPOSED = 'proposed'

#: What a miss is called on the lead and in the mail.
MISSED_KIND = 'lead.quote_due_missed'
REMINDER_KIND = 'lead.quote_due_soon'


# ── §2.4 the extension ───────────────────────────────────────────────
def extend(lead, new_at, *, reason_code, reason_text='', actor=None,
           email_message_id=None, source=None):
    """Move a due date, keeping the old one. (ok, detail-or-error).

    Refuses an extension to a date that is not later than the one the
    lead carries. "Extended" has a direction, and a portal mail
    restating an earlier deadline is not an extension — it is either a
    correction, which an owner makes by editing the date, or a reminder
    that should never have come through this path.
    """
    from app.models.cockpit02 import EXTENSION_CODES
    from app.services import cockpit02_gate as gate

    if reason_code not in EXTENSION_CODES:
        return False, (f'{reason_code or "That"} is not one of the '
                       f'extension reasons.')
    if reason_code == 'other' and not (reason_text or '').strip():
        return False, 'Say what the reason was.'
    try:
        value = gate.parse_due(new_at)
    except ValueError as exc:
        return False, str(exc)
    if value is None:
        return False, 'An extension needs a new date.'

    was = getattr(lead, 'quote_due_at', None)
    if was is not None and value <= was:
        from app.services import quote_due as qd
        return False, (f'That is not later than the date on the lead '
                       f'({qd.shown(was)}). To correct a wrong date, '
                       f'edit it instead.')

    row = gate.apply_due(lead, value, actor=actor,
                         source=source or 'extension',
                         reason_code=reason_code,
                         reason_text=reason_text,
                         email_message_id=email_message_id,
                         confirmed=True)
    from app.services import audit, quote_due as qd

    audit.record('lead.quote_due_extended', 'lead', lead.id,
                 old={'quote_due_at': qd.shown(was)},
                 new={'quote_due_at': qd.shown(value),
                      'reason': reason_code},
                 actor=actor,
                 reason=(reason_text or reason_code), commit=False)
    return True, {'quote_due_at': qd.shown(value),
                  'extensions': lead.quote_due_extensions or 0,
                  'history_id': getattr(row, 'id', None)}


def history(lead_id):
    """Every move of this lead's due date, newest first."""
    from app.models.cockpit02 import LeadQuoteDueHistory

    rows = (LeadQuoteDueHistory.query
            .filter(LeadQuoteDueHistory.lead_id == lead_id)
            .order_by(LeadQuoteDueHistory.id.desc()).all())
    return [r.to_dict() for r in rows]


# ── §2.5 the proposal ────────────────────────────────────────────────
def propose(lead, at, *, source, email_message_id=None, evidence=''):
    """Offer a date read off a mail. Returns the row, or None.

    Nothing is applied and nothing on the lead changes. The row is the
    offer; `accept()` is what moves the date.
    """
    from app import db
    from app.models.cockpit02 import LeadQuoteDueHistory

    if at is None:
        return None
    current = getattr(lead, 'quote_due_at', None)
    if current is not None and at <= current:
        # A reminder restating the deadline, which is the common case.
        # Not an offer, and not worth a row: the owner would be asked
        # to accept the date they already have.
        return None
    outstanding = pending(lead.id)
    if outstanding is not None and outstanding.new_due_at == at:
        return outstanding      # already offered, not offered twice
    row = LeadQuoteDueHistory(
        lead_id=lead.id, old_due_at=current, new_due_at=at,
        reason_code=PROPOSED, reason_text=(evidence or '')[:300],
        source=source, email_message_id=email_message_id,
        actor='system')
    db.session.add(row)
    log.info('lead %s: a new due date was proposed from %s',
             lead.id, source)
    return row


def pending(lead_id):
    """The outstanding proposal for a lead, or None."""
    from app.models.cockpit02 import LeadQuoteDueHistory

    return (LeadQuoteDueHistory.query
            .filter(LeadQuoteDueHistory.lead_id == lead_id,
                    LeadQuoteDueHistory.reason_code == PROPOSED)
            .order_by(LeadQuoteDueHistory.id.desc()).first())


def accept(lead, *, actor=None, reason_code='client_extension'):
    """Take the outstanding proposal. (ok, detail-or-error)."""
    from app import db

    row = pending(lead.id)
    if row is None:
        return False, 'There is no proposed date on this lead.'
    ok, detail = extend(lead, row.new_due_at, reason_code=reason_code,
                        reason_text=row.reason_text or 'from the mail',
                        actor=actor,
                        email_message_id=row.email_message_id,
                        source=row.source)
    if not ok:
        return False, detail
    # The offer is spent. Deleting it would lose the fact that the
    # CRM read it, so it is marked as taken instead.
    row.reason_code = 'accepted'
    row.actor = actor or row.actor
    db.session.add(row)
    return True, detail


def dismiss(lead, *, actor=None, why=''):
    """Say no to a proposal, keeping the record that it was made."""
    from app import db

    row = pending(lead.id)
    if row is None:
        return False, 'There is no proposed date on this lead.'
    row.reason_code = 'dismissed'
    row.reason_text = (why or row.reason_text or '')[:300]
    row.actor = actor or row.actor
    db.session.add(row)
    return True, {'dismissed': True}


def read_from_message(lead, *, subject='', body='', email_message_id=None,
                      received_at=None):
    """§2.5 — a mail arrives on a lead that already has a deadline.

    Called by the mail intake. Proposes rather than applies, and does
    nothing at all when the mail says nothing new.
    """
    from app.services import quote_due as qd

    reading = qd.read(subject, body, received_at=received_at)
    if reading is None:
        return None
    if getattr(lead, 'quote_due_at', None) is None:
        # Nothing to extend: the lead had no date, so this is the
        # first one. Applied unconfirmed, which is exactly what §2.3
        # describes — "auto — please confirm".
        from app.services import cockpit02_gate as gate
        gate.apply_due(lead, reading.at, actor='system',
                       source=reading.source, reason_code='set',
                       reason_text=reading.evidence,
                       email_message_id=email_message_id,
                       confirmed=False)
        return 'set'
    return 'proposed' if propose(
        lead, reading.at, source=reading.source,
        email_message_id=email_message_id,
        evidence=reading.evidence) else None


# ── §2.7 the miss ────────────────────────────────────────────────────
def mark_missed(lead, *, now=None, actor='CRM-COCKPIT-02'):
    """Flag a lead whose deadline passed with no offer. True if newly
    flagged — so the mail goes once, not every hour."""
    from app.services import audit, quote_due as qd

    if not qd.is_missed(lead, now=now):
        return False
    if getattr(lead, 'quote_due_missed_at', None) is not None:
        return False
    lead.quote_due_missed_at = now or datetime.utcnow()
    audit.record('lead.quote_due_missed', 'lead', lead.id,
                 new={'quote_due_at': qd.shown(lead.quote_due_at)},
                 actor=actor,
                 reason='the submission deadline passed with no quote '
                        'recorded',
                 commit=False)
    return True


def tell_about_miss(lead, *, dry_run=False):
    """Owner, vertical head and the standing CC, as one mail (§2.7).

    Through `notify.send_policy` so the existing CC policy decides who
    else is copied — this release does not get its own recipient list,
    because a second list is a second thing to keep in step.
    """
    from app.services import notify, quote_due as qd, vertical_heads

    owner = (getattr(lead, 'assigned_to', '') or '').strip()
    heads = []
    try:
        heads = [h for h in (vertical_heads.heads_for_vertical(
            getattr(lead, 'procam_vertical', '') or '') or [])]
    except Exception:                                     # noqa: BLE001
        heads = []
    heads = [getattr(h, 'emp_code', h) for h in heads]
    to = [owner] if owner else []
    cc = [h for h in heads if h and h != owner]
    if not to and not cc:
        return {'skipped': 'nobody to tell'}

    title = (f'Submission missed — {lead.company or "lead"} '
             f'#{lead.id}')
    body = (f'The quote for {lead.company or "this lead"} was due '
            f'{qd.shown(lead.quote_due_at)} and no quote is recorded '
            f'against it.\n\n'
            f'If an offer did go in, record the quote on the lead. '
            f'If it did not, record Bid or Not bid with the reason, '
            f'so the lane shows up in the win/loss figures as a '
            f'decision rather than a gap.')
    if dry_run:
        return {'would_tell': to + cc, 'title': title}
    return notify.send_policy(
        to, cc, kind=MISSED_KIND, title=title, body=body,
        url=f'/leads/{lead.id}', entity_type='lead',
        entity_id=str(lead.id), email=True,
        event_key=f'{MISSED_KIND}:{lead.id}')


def tell_about_soon(lead, hours, *, dry_run=False):
    """The reminder before a deadline (admin setting, §3.2's table)."""
    from app.services import notify, quote_due as qd

    owner = (getattr(lead, 'assigned_to', '') or '').strip()
    if not owner:
        return {'skipped': 'no owner'}
    title = (f'Quote due in {hours} h — {lead.company or "lead"} '
             f'#{lead.id}')
    body = (f'The submission deadline is '
            f'{qd.shown(lead.quote_due_at)}.')
    if dry_run:
        return {'would_tell': [owner], 'title': title}
    return notify.send_policy(
        [owner], (), kind=REMINDER_KIND, title=title, body=body,
        url=f'/leads/{lead.id}', entity_type='lead',
        entity_id=str(lead.id), email=True,
        event_key=f'{REMINDER_KIND}:{lead.id}:{hours}')
