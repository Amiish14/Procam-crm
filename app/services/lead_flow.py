"""
One reading of a lead, for the screen a person actually works on.

CRM-LEAD-FLOW-01 §3. The lead pop-up shows about 25 fields in nine
sections to everybody at every stage: an RFQ-stage lead already
displays the quote number, the validity, the buy rate, the margin,
four activity date boxes and the reason the PIC was last changed.
It also shows the intake scoring — "decided by step_10 · Score: 0
base, 40 1 asks for a price" — which is the CRM talking to itself.

This module answers three questions instead, and the template draws
nothing it does not answer:

    where is this lead          the six-stage strip
    what should happen next     one sentence and at most two buttons
    what is missing             per stage, with the tab and field
                                each missing thing lives in

Nothing here writes. `move()` is the one exception and it goes
through the same stage history every other screen uses.

The six stages are a view, not a new vocabulary. `lead.stage` keeps
the values the reports count; "Call Done" and "Visit Done" are
Contacted on the strip and keep their own word in `sub_status`.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

log = logging.getLogger(__name__)

FLAG = 'FEATURE_LEAD_POPUP_V2'

#: The strip, in order. Lost, On Hold and Not Interested are not on
#: it — they are where the line stops, and putting them in the row a
#: person clicks along invites a mis-click that closes a deal.
STRIP = ('New', 'Contacted', 'RFQ', 'Quoted', 'Negotiation', 'Won')
OFF_STRIP = ('Lost', 'On Hold', 'Not Interested')

#: Every stage the database holds, and where it sits on the strip.
#: Two "new" stages existed; both are New here, and §6.6 migrates
#: the data so the dropdown can lose one.
ON_STRIP = {
    'New': 'New',
    'New Opportunity': 'New',
    'Call Done': 'Contacted',
    'Profile Sent': 'Contacted',
    'Business Discussion': 'Contacted',
    'Appointment': 'Contacted',
    'Visit Done': 'Contacted',
    'RFQ Generated': 'RFQ',
    'Quoted': 'Quoted',
    'Under Negotiation': 'Negotiation',
    'Won': 'Won',
}

#: Moving to a strip stage writes this back to `lead.stage`, so the
#: reports keep counting what they always counted.
TO_STAGE = {
    'New': 'New',
    'Contacted': 'Business Discussion',
    'RFQ': 'RFQ Generated',
    'Quoted': 'Quoted',
    'Negotiation': 'Under Negotiation',
    'Won': 'Won',
}

#: Contacted's detail. The dropdown on the screen, and what §6.6
#: migrates the old stage values onto.
SUB_STATUSES = ('Call done', 'Profile sent', 'Discussion',
                'Appointment', 'Visit done')

#: What each stage asks for before the lead may leave it (§3.3).
#: (field, label, tab) — the tab is what [Take me there] opens.
REQUIRED = {
    'New': (('company_id', 'Account', 'summary'),
            ('assigned_to', 'Owner', 'summary'),
            ('procam_vertical', 'Vertical', 'summary')),
    'Contacted': (('_activity', 'One activity logged', 'history'),),
    'RFQ': (('_scope', 'Cargo, and the route or the site', 'summary'),
            ('_target', 'Target date', 'summary'),
            ('opportunity_value_num', 'Opportunity value', 'summary'),
            ('value_basis', 'How the value was arrived at', 'summary')),
    'Quoted': (('quote_no', 'Quote no.', 'quote'),
               ('quote_value_num', 'Quote value', 'quote'),
               ('quote_date', 'Quote date', 'quote'),
               ('quote_validity_date', 'Valid until', 'quote')),
    'Negotiation': (('_revised', 'Latest revised value', 'quote'),),
    'Won': (('_final', 'Final value', 'quote'),),
}

TABS = ('summary', 'emails', 'rates', 'quote', 'notes', 'history')

#: The Quote tab is hidden before this point (§3.2). A quote number
#: on a lead nobody has sent an RFQ for is a field that can only be
#: filled in wrongly.
QUOTE_FROM = 'RFQ'


def enabled():
    try:
        from app.services import flags
        return bool(flags.on(FLAG))
    except Exception:                                     # noqa: BLE001
        return False


# ── where the lead is ────────────────────────────────────────────────
def strip_stage(lead):
    """Which of the six this lead is on, or '' when it is closed."""
    stage = (getattr(lead, 'stage', '') or '').strip()
    return ON_STRIP.get(stage, '')


def closed_as(lead):
    stage = (getattr(lead, 'stage', '') or '').strip()
    return stage if stage in OFF_STRIP else ''


def index_of(name):
    try:
        return STRIP.index(name)
    except ValueError:
        return -1


def quote_visible(lead):
    """§3.2 — the Quote tab appears from RFQ onwards."""
    here = strip_stage(lead)
    if not here:
        # A closed lead keeps its quote visible: that is the figure
        # the close was argued over.
        return bool(closed_as(lead))
    return index_of(here) >= index_of(QUOTE_FROM)


def tabs_for(lead):
    return [t for t in TABS if t != 'quote' or quote_visible(lead)]


# ── what is missing ──────────────────────────────────────────────────
def _has(lead, field):
    """Whether this requirement is met. The ones beginning with an
    underscore are questions about the lead, not single columns."""
    if field == '_activity':
        from app import LeadActivity, LeadEmail, db
        for model in (LeadActivity, LeadEmail):
            try:
                if db.session.query(model.id).filter(
                        model.lead_id == lead.id).count():
                    return True
            except Exception:                             # noqa: BLE001
                db.session.rollback()
        return bool((getattr(lead, 'phone_call_date', None)
                     or getattr(lead, 'meeting_date', None)
                     or getattr(lead, 'intro_mail_date', None)))
    if field == '_scope':
        cargo = (getattr(lead, 'project', '') or '').strip()
        route = ((getattr(lead, 'origin', '') or '').strip()
                 and (getattr(lead, 'destination', '') or '').strip())
        site = (getattr(lead, 'site_location', '') or '').strip()
        return bool(cargo and (route or site))
    if field == '_target':
        return bool(getattr(lead, 'rfq_expected_date', None)
                    or getattr(lead, 'opp_close_date', None)
                    or getattr(lead, 'followup_date', None))
    if field in ('_revised', '_final'):
        return bool(getattr(lead, 'quote_value_num', None)
                    or getattr(lead, 'opportunity_value_num', None))
    value = getattr(lead, field, None)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None and value != 0


def missing_for(lead, stage):
    """What `stage` still wants, as [{field, label, tab}]."""
    out = []
    for field, label, tab in REQUIRED.get(stage, ()):
        if not _has(lead, field):
            out.append({'field': field, 'label': label, 'tab': tab})
    return out


def may_move(lead, to):
    """(ok, missing) for a move to a strip stage.

    Everything behind the lead's current position is open: a person
    correcting a stage they overshot should not be asked for the
    next stage's fields.
    """
    to = (to or '').strip()
    if to not in STRIP:
        return False, []
    here = strip_stage(lead)
    if here and index_of(to) <= index_of(here):
        return True, []
    gaps = []
    start = index_of(here) if here else 0
    for name in STRIP[start:index_of(to) + 1]:
        gaps.extend(missing_for(lead, name))
    seen, unique = set(), []
    for gap in gaps:
        if gap['field'] in seen:
            continue
        seen.add(gap['field'])
        unique.append(gap)
    return (not unique), unique


# ── what to do now ───────────────────────────────────────────────────
def _days_to(value):
    if value is None:
        return None
    when = value.date() if isinstance(value, datetime) else value
    try:
        return (when - date.today()).days
    except Exception:                                     # noqa: BLE001
        return None


def due_of(lead):
    """(date, days, colour) — red overdue, orange under a day,
    green two days or more. The rule already agreed."""
    when = (getattr(lead, 'followup_date', None)
            or getattr(lead, 'rfq_expected_date', None)
            or getattr(lead, 'opp_close_date', None))
    days = _days_to(when)
    if when is None or days is None:
        return None, None, ''
    colour = 'red' if days < 0 else ('orange' if days < 2 else 'green')
    return when, days, colour


def what_to_do(lead):
    """One sentence and at most two buttons (§3.3).

    The most important new thing on the screen: a person opening a
    lead should not have to work out what it is waiting for.
    """
    here = strip_stage(lead)
    closed = closed_as(lead)
    if closed:
        return {'text': f'This lead is closed as {closed}. '
                        f'Nothing is due on it.',
                'actions': []}

    _when, days, _colour = due_of(lead)
    when_text = ('' if days is None
                 else (f' Overdue by {abs(days)} day(s).' if days < 0
                       else (' Due today.' if days == 0
                             else f' Due in {days} day(s).')))

    if here == 'New':
        return {'text': 'Confirm the account and call or mail the '
                        'client.' + when_text,
                'actions': [{'label': 'Confirm account',
                             'action': 'tab', 'target': 'summary'},
                            {'label': 'Log a call', 'action': 'tab',
                             'target': 'history'}]}
    if here == 'Contacted':
        return {'text': 'Get the RFQ or the requirement from the '
                        'client.' + when_text,
                'actions': [{'label': 'Log activity', 'action': 'tab',
                             'target': 'history'},
                            {'label': 'Ask client a question',
                             'action': 'draft', 'target': 'client'}]}
    if here == 'RFQ':
        return {'text': 'Prepare the quote.' + when_text,
                'actions': [{'label': 'Start rate sourcing',
                             'action': 'tab', 'target': 'rates'},
                            {'label': 'Open quote builder',
                             'action': 'tab', 'target': 'quote'}]}
    if here == 'Quoted':
        sent = _days_to(getattr(lead, 'quote_date', None))
        ago = (f' — sent {abs(sent)} day(s) ago' if sent is not None
               else '')
        number = (getattr(lead, 'quote_no', '') or '').strip()
        return {'text': f'Follow up on quote '
                        f'{number or "(no number yet)"}{ago}.',
                'actions': [{'label': 'Draft follow-up',
                             'action': 'draft', 'target': 'client'},
                            {'label': 'Log client reply',
                             'action': 'tab', 'target': 'emails'}]}
    if here == 'Negotiation':
        return {'text': 'Agree the final price, or revise the quote.',
                'actions': [{'label': 'Revise quote', 'action': 'tab',
                             'target': 'quote'},
                            {'label': 'Mark Won', 'action': 'stage',
                             'target': 'Won'}]}
    if here == 'Won':
        return {'text': 'Hand over to operations.',
                'actions': [{'label': 'Create handover',
                             'action': 'tab', 'target': 'quote'}]}
    return {'text': 'This lead is not on the main flow. Open More to '
                    'see what can be done with it.',
            'actions': []}


# ── the tabs' contents ───────────────────────────────────────────────
def emails_of(lead, limit=50):
    """The customer trail only. Supplier mail lives under rates —
    §3.2's bug was the two shown together."""
    from app import LeadEmail
    rows = (LeadEmail.query.filter_by(lead_id=lead.id)
            .order_by(LeadEmail.id.desc()).limit(limit).all())
    return [{'id': r.id, 'direction': r.direction or '',
             'from': r.from_addr or '', 'subject': r.subject or '',
             'at': str(r.sent_or_received_at or '')[:16]}
            for r in rows]


def rates_of(lead, limit=50):
    """Supplier requests and their replies, and nothing else."""
    from app.models.ff import RateRequest
    from app.models.rfq import RFQ
    from app import db

    try:
        rfq_ids = [r[0] for r in db.session.query(RFQ.id)
                   .filter(RFQ.lead_id == lead.id).all()]
    except Exception:                                     # noqa: BLE001
        db.session.rollback()
        return []
    if not rfq_ids:
        return []
    rows = (RateRequest.query.filter(RateRequest.rfq_id.in_(rfq_ids))
            .order_by(RateRequest.id.desc()).limit(limit).all())
    return [{'id': r.id, 'vendor': r.vendor_name or '',
             'email': r.vendor_email or '', 'status': r.status or '',
             'asked': str(r.requested_at or '')[:16]} for r in rows]


def history_of(lead, limit=80):
    """One timeline, filled from what happened (§3.2).

    Not four empty date boxes for somebody to type into: the stage
    moves, the owner changes with their reason, the mails and the
    activities, in order.
    """
    from app import (LeadActivity, LeadAssignmentHistory, LeadEmail,
                     LeadStageHistory)

    out = []
    for row in LeadStageHistory.query.filter_by(lead_id=lead.id).all():
        out.append({'at': str(row.changed_at or '')[:16],
                    'kind': 'stage',
                    'what': f'{row.from_stage or "—"} → '
                            f'{row.to_stage or "—"}',
                    'who': row.changed_by or '',
                    'why': row.note or ''})
    for row in LeadAssignmentHistory.query.filter_by(
            lead_id=lead.id).all():
        out.append({'at': str(row.changed_at or '')[:16],
                    'kind': 'owner',
                    'what': f'{row.from_primary or "nobody"} → '
                            f'{row.to_primary or "nobody"}',
                    'who': row.changed_by or '',
                    'why': row.note or ''})
    for row in LeadActivity.query.filter_by(lead_id=lead.id).all():
        out.append({'at': str(row.occurred_at or '')[:16],
                    'kind': (row.kind or 'activity'),
                    'what': row.subject or '',
                    'who': row.performed_by or '', 'why': ''})
    for row in LeadEmail.query.filter_by(lead_id=lead.id).all():
        out.append({'at': str(row.sent_or_received_at or '')[:16],
                    'kind': 'mail ' + (row.direction or ''),
                    'what': row.subject or '',
                    'who': row.from_addr or '', 'why': ''})
    out.sort(key=lambda r: r['at'], reverse=True)
    return out[:limit]


def intake_note(lead):
    """Why the CRM made this a lead. Admin only (§3.2).

    It is the software explaining itself, which is useful when a
    classification is wrong and noise to everybody else.
    """
    import json
    raw = getattr(lead, 'email_extracted_json', None)
    out = {'classification': getattr(lead, 'classification', '') or '',
           'confidence': getattr(lead, 'lead_confidence', None),
           'relevance': getattr(lead, 'relevance', '') or '',
           'duplicate_score': getattr(lead, 'duplicate_score', None),
           'vertical_reason': getattr(lead, 'vertical_reason', '') or '',
           'extract': {}}
    if raw:
        try:
            out['extract'] = json.loads(raw)
        except Exception:                                 # noqa: BLE001
            out['extract'] = {}
    return out


# ── the whole panel ──────────────────────────────────────────────────
def panel(lead, *, is_admin=False):
    """Everything the new screen draws, and nothing else."""
    from app import Company, db
    from app.services import rfq_board

    account = (db.session.get(Company, lead.company_id)
               if lead.company_id else None)
    here = strip_stage(lead)
    when, days, colour = due_of(lead)
    emails = emails_of(lead)
    rates = rates_of(lead)

    steps = []
    for name in STRIP:
        state = 'ahead'
        if here:
            if name == here:
                state = 'here'
            elif index_of(name) < index_of(here):
                state = 'done'
        ok, gaps = may_move(lead, name)
        steps.append({'name': name, 'state': state,
                      'can': ok, 'missing': gaps})

    return {
        'lead_id': lead.id,
        'account': {
            'id': account.id if account else None,
            'name': (account.name if account else lead.company) or '',
            'linked': account is not None,
        },
        'owner': lead.assigned_to or '',
        'owner_name': (lead.assigned_name
                       or rfq_board.name_of(lead.assigned_to or '')),
        'secondary': lead.secondary_owner or '',
        'vertical': lead.procam_vertical or '',
        'stage': lead.stage or '',
        'strip_stage': here,
        'closed_as': closed_as(lead),
        'sub_status': getattr(lead, 'sub_status', '') or '',
        'sub_statuses': list(SUB_STATUSES),
        'priority': _priority(lead),
        'due': {'date': str(when)[:10] if when else '',
                'days': days, 'colour': colour},
        'steps': steps,
        'what_to_do': what_to_do(lead),
        'tabs': tabs_for(lead),
        'counts': {'emails': len(emails), 'rates': len(rates)},
        'summary': {
            'contact': {'name': lead.pic or '',
                        'designation': lead.designation_pic or '',
                        'email': lead.email or '',
                        'phone': lead.phone or ''},
            'cargo': lead.project or '',
            'origin': lead.origin or '',
            'destination': lead.destination or '',
            'site': lead.site_location or '',
            'value': lead.opportunity_value_num,
            'value_basis': lead.value_basis or '',
            'next_follow_up': str(lead.followup_date or '')[:10],
            'source': lead.source or '',
        },
        'emails': emails,
        'rates': rates,
        'quote': {
            'visible': quote_visible(lead),
            'no': lead.quote_no or '',
            'value': lead.quote_value_num,
            'date': str(lead.quote_date or '')[:10],
            'valid_until': str(lead.quote_validity_date or '')[:10],
        },
        'notes': lead.notes or '',
        'history': history_of(lead),
        'intake': intake_note(lead) if is_admin else None,
        'merged_into': getattr(lead, 'merged_into_id', None),
    }


def _priority(lead):
    """The RFQ's priority, where there is an RFQ. Blank otherwise —
    a lead does not carry one and inventing a default would put a
    chip on the screen that means nothing."""
    try:
        from app.models.rfq import RFQ
        row = (RFQ.query.filter_by(lead_id=lead.id)
               .order_by(RFQ.id.desc()).first())
        return (getattr(row, 'priority', '') or '') if row else ''
    except Exception:                                     # noqa: BLE001
        from app import db
        db.session.rollback()
        return ''


# ── the one write ────────────────────────────────────────────────────
def move(lead, to, *, actor, sub_status=None, note=''):
    """Move a lead along the strip. (ok, detail-or-missing)."""
    from app import LeadStageHistory, db
    from app.services import audit

    to = (to or '').strip()
    if to not in STRIP:
        return False, {'error': f'{to or "That"} is not a stage on '
                                f'the strip.'}
    ok, gaps = may_move(lead, to)
    if not ok:
        return False, {'missing': gaps}

    was = lead.stage or ''
    lead.stage = TO_STAGE[to]
    if to == 'Contacted' and sub_status:
        if sub_status not in SUB_STATUSES:
            return False, {'error': f'{sub_status} is not one of the '
                                    f'Contacted sub-statuses.'}
        lead.sub_status = sub_status
    lead.stage_entered_at = datetime.utcnow()
    db.session.add(LeadStageHistory(
        lead_id=lead.id, from_stage=was, to_stage=lead.stage,
        changed_at=datetime.utcnow(), changed_by=actor,
        note=note or None))
    audit.record('lead.stage_moved', 'lead', lead.id,
                 old={'stage': was},
                 new={'stage': lead.stage, 'strip': to,
                      'sub_status': lead.sub_status or ''},
                 actor=actor, reason=note or f'moved to {to}')
    db.session.commit()
    return True, {'stage': lead.stage, 'strip': to,
                  'sub_status': lead.sub_status or ''}
