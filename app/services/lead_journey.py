"""
Before the RFQ: what happened, and what it means for the account.

CRM-LEAD-JOURNEY-01 (R4), behind `FEATURE_PRE_RFQ_FUNNEL` and
`FEATURE_LEAD_JOURNEY`.

Today a person records an activity and then, separately,
remembers to move the account's development stage. The second
half is the half that does not happen, so `dev_stage` is null on
641 accounts and "Active Account" on 392 with nothing in
between — a funnel with no middle.

So the activity moves the stage. One form, one save, and the
stage is a consequence rather than a second piece of admin.

Two things are deliberately not symmetrical:

    Meeting planned and Meeting held are different activities.
    The existing `ACTIVITY_KINDS` has one "Meeting", which cannot
    tell a diary entry from a thing that happened — and the
    difference is the whole point of the stage it moves.

    Stages only move forward. A call logged after a meeting does
    not drag the account back to Contact Established; going back
    needs an admin and a reason, because a funnel that walks
    backwards on ordinary activity reports progress that is not
    there.
"""
from __future__ import annotations

FUNNEL_FLAG = 'FEATURE_PRE_RFQ_FUNNEL'
JOURNEY_FLAG = 'FEATURE_LEAD_JOURNEY'

#: §6.2. The kinds a person picks, and the stage each moves to.
#: `None` means the activity is a checkpoint, not a stage.
ACTIVITIES = (
    ('Phone call',          'Contact Established'),
    ('Email',               'Contact Established'),
    ('LinkedIn message',    'Contact Established'),
    ('Meeting planned',     'Meeting Planned'),
    ('Meeting held',        'Meeting Completed'),
    ('Requirement received', 'Opportunity Identified'),
    ('Capability check',    None),
    ('RFQ expected',        'RFQ Expected'),
)
KINDS = tuple(k for k, _s in ACTIVITIES)
MOVES_TO = {k: s for k, s in ACTIVITIES}

#: The conditional field each kind asks for, and nothing else.
ASKS = {
    'Meeting held': ('expected_rfq_timing', 'Expected RFQ timing'),
    'Capability check': ('capability_fit', 'Capability fit'),
    'RFQ expected': ('rfq_expected_date', 'RFQ expected date'),
}

EXPECTED_TIMING = (
    'Immediate (≤ 1 month)', 'Short-term (1–3)',
    'Medium-term (3–6)', 'Long-term (> 6)', 'Nurturing', 'Dormant',
)
CAPABILITY_FIT = (
    'Yes — standard', 'Yes — needs site visit',
    'Partly — with partner', 'No',
)
CAPABILITY_NO = 'No'
CAPABILITY_LOST_REASON = 'We could not serve it'

#: §6.1 — the four channels the CRM sets for itself, and the rest
#: a person picks. Order matters: the first rule that fits wins.
CHANNELS = (
    'Management Leads', 'Existing Customer / Repeat',
    'Customer Referrals', 'Customer Visits', 'Networking',
    'LinkedIn', 'Email Outreach', 'Cold Calling',
    'Inbound Enquiries', 'Industry Events & Exhibitions',
    'Channel Partners', 'Tender / E-Procurement Portals',
)
AUTOMATIC = ('Management Leads', 'Existing Customer / Repeat',
             'Inbound Enquiries', 'Tender / E-Procurement Portals')
MANUAL_CHANNELS = tuple(c for c in CHANNELS if c not in AUTOMATIC)

#: §8 — the sheet's standard conversion per channel.
CONVERSION = {
    'Management Leads': 0.50, 'Existing Customer / Repeat': 0.60,
    'Customer Referrals': 0.30, 'Customer Visits': 0.35,
    'Networking': 0.10, 'LinkedIn': 0.10, 'Email Outreach': 0.05,
    'Cold Calling': 0.05, 'Inbound Enquiries': 0.50,
    'Industry Events & Exhibitions': 0.15,
    'Channel Partners': 0.20,
    'Tender / E-Procurement Portals': 0.10,
}


def funnel_on(emp_code=None):
    return _on(FUNNEL_FLAG, emp_code)


def journey_on(emp_code=None):
    return _on(JOURNEY_FLAG, emp_code)


def _on(flag, emp_code=None):
    from flask import has_request_context, session

    from app.services import flags

    if emp_code is None and has_request_context():
        return flags.on_for(flag, session.get('emp_code'))
    if emp_code is None:
        return flags.on(flag)
    return flags.on_for(flag, emp_code)


# ── §6.1: the channel the CRM can work out for itself ────────────────
def detect_channel(lead):
    """(channel, why) the CRM sets itself, or ('', why not).

    Four rules, and a lead that fits none is a question for the
    person entering it. Guessing the fifth would put a conversion
    rate on a lead nobody classified.
    """
    from app import Company, db

    #: An auction or tender is a portal lead whatever else is true.
    if (getattr(lead, 'lead_type', '') or '') == 'auction':
        return ('Tender / E-Procurement Portals',
                'the lead is an auction or tender posting')

    forwarder = (getattr(lead, 'forwarded_by_email', '') or '').lower()
    if forwarder and _is_director(forwarder):
        return 'Management Leads', f'forwarded in by {forwarder}'

    if lead.company_id:
        from app import Lead
        won = (Lead.query
               .filter(Lead.company_id == lead.company_id,
                       Lead.id != (lead.id or -1),
                       Lead.stage == 'Won').first())
        if won is not None:
            account = db.session.get(Company, lead.company_id)
            return ('Existing Customer / Repeat',
                    f'{getattr(account, "name", "this account")} has '
                    f'won work before (lead #{won.id})')

    if (getattr(lead, 'source', '') or '') == 'email' \
            and not _internal(getattr(lead, 'original_email_from', '')
                              or getattr(lead, 'email', '')):
        return 'Inbound Enquiries', 'it arrived at leads@ from outside'

    return '', 'none of the four automatic rules fit — ask'


def _is_director(email):
    from app import Employee

    emp = Employee.query.filter(
        Employee.email.ilike(email)).first()
    if emp is None:
        return False
    role = (getattr(emp, 'role', '') or '').lower()
    return role == 'director' or (emp.emp_code or '').startswith('DIR')


def _internal(address):
    try:
        from app.services.mail_signals import is_internal
        return is_internal(address)
    except Exception:                                     # noqa: BLE001
        return False


def set_channel(lead, *, actor='system', channel=None):
    """Record the channel. Automatic where a rule fits."""
    from app.services import audit

    if getattr(lead, 'lead_channel', None):
        return lead.lead_channel, 'already set'
    found, why = (channel, 'chosen on the form') if channel \
        else detect_channel(lead)
    if not found:
        return '', why
    lead.lead_channel = found
    lead.lead_channel_auto = channel is None
    audit.record('lead.channel_set', 'lead', lead.id,
                 new={'lead_channel': found,
                      'automatic': channel is None},
                 actor=actor, reason=why)
    return found, why


# ── §6.2: the activity moves the stage ───────────────────────────────
def stage_rank(stage):
    from presales.models import ACCOUNT_DEV_STAGES
    try:
        return ACCOUNT_DEV_STAGES.index(stage)
    except (ValueError, AttributeError):
        return -1


def log(lead, *, kind, actor, occurred_at=None, contact_id=None,
        note='', value=None):
    """One activity. (ok, detail) — writes the row and the stage.

    `value` is the one conditional field the kind asks for.
    """
    from datetime import datetime

    from app import Company, db
    from app.services import audit

    if kind not in KINDS:
        return False, f'kind must be one of {list(KINDS)}'
    field = ASKS.get(kind)
    if field and value in (None, ''):
        return False, f'{field[1]} is required when logging "{kind}"'

    account = (db.session.get(Company, lead.company_id)
               if lead.company_id else None)
    if account is None:
        return False, ('this lead has no account yet — confirm the '
                       'customer first, or the activity belongs to '
                       'nobody')

    from presales.services import log_activity
    row = log_activity(account=account, kind=_presales_kind(kind),
                       performed_by_code=actor,
                       subject=kind, body=note or None,
                       contact_id=contact_id,
                       occurred_at=occurred_at or datetime.utcnow())

    applied = {}
    if field:
        name, _label = field
        ok, why = _apply_value(lead, kind, name, value, actor=actor)
        if not ok:
            return False, why
        applied[name] = value

    moved = move_stage(account, kind, actor=actor)
    audit.record('lead.activity_logged', 'lead', lead.id,
                 new={'kind': kind, 'account_id': account.id,
                      'moved_to': moved, **applied},
                 actor=actor, reason=f'{kind} logged')
    return True, {'activity_id': getattr(row, 'id', None),
                  'moved_to': moved, 'applied': applied}


def _presales_kind(kind):
    """Map to an existing ACTIVITY_KINDS value where one fits."""
    from presales.models import ACTIVITY_KINDS

    direct = {'Phone call': 'Phone Call', 'Email': 'Email',
              'LinkedIn message': 'LinkedIn Outreach',
              'Meeting planned': 'Meeting', 'Meeting held': 'Meeting',
              'Requirement received': 'Project Identified',
              'Capability check': 'Internal Note',
              'RFQ expected': 'Follow-up'}
    got = direct.get(kind, 'Other')
    return got if got in ACTIVITY_KINDS else 'Other'


def _apply_value(lead, kind, name, value, *, actor):
    from app.services import audit

    if name == 'expected_rfq_timing':
        if value not in EXPECTED_TIMING:
            return False, f'timing must be one of {list(EXPECTED_TIMING)}'
        lead.expected_rfq_timing = value
    elif name == 'capability_fit':
        if value not in CAPABILITY_FIT:
            return False, f'fit must be one of {list(CAPABILITY_FIT)}'
        lead.capability_fit = value
        if value == CAPABILITY_NO:
            # §6.1 — "No" closes the lead. Said out loud in the
            # response, because a dropdown that quietly closes a
            # lead is a dropdown people learn to fear.
            lead.stage = 'Lost'
            lead.lost_reason = CAPABILITY_LOST_REASON
            audit.record('lead.closed_capability', 'lead', lead.id,
                         new={'stage': 'Lost',
                              'lost_reason': CAPABILITY_LOST_REASON},
                         actor=actor,
                         reason='capability check answered "No"')
    elif name == 'rfq_expected_date':
        from datetime import date, datetime as _dt
        if isinstance(value, str):
            try:
                value = _dt.strptime(value[:10], '%Y-%m-%d').date()
            except ValueError:
                return False, 'the date must be YYYY-MM-DD'
        if not isinstance(value, date):
            return False, 'the date must be YYYY-MM-DD'
        lead.rfq_expected_date = value
    return True, ''


def move_stage(account, kind, *, actor):
    """Move the account forward, never back. Returns the stage or ''."""
    from presales.services import change_account_stage

    target = MOVES_TO.get(kind)
    if not target:
        return ''
    now = getattr(account, 'dev_stage', None)
    if now and stage_rank(now) >= stage_rank(target):
        # Ordinary activity never walks the funnel backwards.
        return ''
    try:
        change_account_stage(account=account, new_stage=target,
                             changed_by_code=actor,
                             note=f'{kind} logged')
    except Exception:                                     # noqa: BLE001
        return ''
    return target


def on_rfq_received(account, *, actor='system'):
    """The RFQ arriving is what makes an account active (§6.2)."""
    from presales.services import change_account_stage

    if account is None:
        return ''
    if getattr(account, 'dev_stage', None) == 'Active Account':
        return ''
    try:
        change_account_stage(account=account,
                             new_stage='Active Account',
                             changed_by_code=actor,
                             note='an RFQ arrived')
    except Exception:                                     # noqa: BLE001
        return ''
    return 'Active Account'


# ── §6.3: targets against actuals ────────────────────────────────────
def month_key(when=None):
    from datetime import date
    when = when or date.today()
    return f'{when.year:04d}-{when.month:02d}'


def conversion_for(channel):
    from app.services import admin_settings
    try:
        table = admin_settings.get('channel_conversion') or {}
        if isinstance(table, dict) and channel in table:
            return float(table[channel])
    except Exception:                                     # noqa: BLE001
        pass
    return float(CONVERSION.get(channel, 0.0))


def leads_needed(target, channel_mix):
    """How many leads a target implies, at this person's mix.

    `channel_mix` is {channel: share}. Returns None when the mix
    is empty or converts at zero — dividing by it would print
    infinity, and "we do not know" is the honest cell.
    """
    if not target or not channel_mix:
        return None
    blended = sum(conversion_for(c) * share
                  for c, share in channel_mix.items())
    if blended <= 0:
        return None
    return target / blended


def targets_for(month=None, vertical=None):
    """[{person, vertical, target, received, pct, forecast}].

    `received` counts RFQs registered that month, from their
    timestamps. Nothing here is typed.
    """
    from collections import Counter

    from app import Lead, db
    from app.models.rfq import RFQ
    from app.models.rfq_target import RfqTarget

    month = month or month_key()
    rows = RfqTarget.query.filter_by(month=month)
    if vertical:
        rows = rows.filter_by(vertical=vertical)
    rows = rows.all()

    got = Counter()
    for rfq in RFQ.query.filter(
            RFQ.registered_at.isnot(None)).all():
        if month_key(rfq.registered_at.date()) != month:
            continue
        owner = (rfq.lead_driver or rfq.created_by_id or '')
        got[(owner, _rfq_vertical(rfq))] += 1

    expected = Counter()
    for lead in Lead.query.filter(
            Lead.rfq_expected_date.isnot(None)).all():
        if month_key(lead.rfq_expected_date) != month:
            continue
        expected[((lead.assigned_to or ''),
                  (lead.procam_vertical or ''))] += 1

    out = []
    for row in rows:
        key = (row.emp_code, row.vertical)
        received = got.get(key, 0)
        out.append({
            'month': month, 'emp_code': row.emp_code,
            'vertical': row.vertical,
            'target': row.target_rfqs or 0,
            'received': received,
            'pct': (round(received * 100 / row.target_rfqs)
                    if row.target_rfqs else None),
            'forecast': expected.get(key, 0),
            'mix': channel_mix(row.emp_code, row.vertical),
        })
    for row in out:
        row['leads_needed'] = leads_needed(row['target'], row['mix'])
    return sorted(out, key=lambda r: (-r['target'], r['emp_code']))


def _rfq_vertical(rfq):
    from app.services.vertical_scope import vertical_name
    return vertical_name(rfq)


def channel_mix(emp_code, vertical, *, months=6):
    """{channel: share} from this person's own recent leads."""
    from collections import Counter
    from datetime import datetime, timedelta

    from app import Lead

    since = datetime.utcnow() - timedelta(days=months * 30)
    rows = (Lead.query
            .filter(Lead.assigned_to == emp_code,
                    Lead.lead_channel.isnot(None),
                    Lead.created_at >= since).all())
    if vertical:
        rows = [l for l in rows
                if (l.procam_vertical or '') == vertical]
    counts = Counter(l.lead_channel for l in rows)
    total = sum(counts.values())
    if not total:
        return {}
    return {c: n / total for c, n in counts.items()}


# ── §6.3: the Funnels page panels ───────────────────────────────────
def channel_plan(month=None, vertical=None):
    """Plan against actual, per channel.

    `expected_rfqs` is deliberately not rounded per row: rounding
    twelve rows and adding them gives a different total from the
    one anybody computes by hand, and then the sheet and the
    screen disagree about the plan.
    """
    from collections import Counter

    from app import Lead

    month = month or month_key()
    rows = (Lead.query
            .filter(Lead.lead_channel.isnot(None),
                    Lead.is_archived.isnot(True)).all())
    if vertical:
        rows = [l for l in rows
                if (l.procam_vertical or '') == vertical]

    actual = Counter(l.lead_channel for l in rows)
    this_month = Counter(
        l.lead_channel for l in rows
        if l.created_at and month_key(l.created_at.date()) == month)

    out = []
    for channel in CHANNELS:
        rate = conversion_for(channel)
        leads = actual.get(channel, 0)
        out.append({
            'channel': channel,
            'conversion': rate,
            'leads': leads,
            'leads_this_month': this_month.get(channel, 0),
            'expected_rfqs': leads * rate,
            'automatic': channel in AUTOMATIC,
        })
    return out


def stage_funnel(vertical=None):
    """How many leads reach each development stage (§6.3).

    Inbound, repeat and tender leads skip the early stages by
    their nature, so the stage-to-stage percentage is computed
    against the leads that could have been there — otherwise the
    first two stages look like a catastrophic drop-off when they
    are simply not on those leads' path.
    """
    from collections import Counter

    from app import Company, Lead
    from presales.models import ACCOUNT_DEV_STAGES

    SKIPS_EARLY = {'Inbound Enquiries', 'Existing Customer / Repeat',
                   'Tender / E-Procurement Portals'}
    EARLY = set(ACCOUNT_DEV_STAGES[:4])

    leads = (Lead.query
             .filter(Lead.company_id.isnot(None),
                     Lead.is_archived.isnot(True)).all())
    if vertical:
        leads = [l for l in leads
                 if (l.procam_vertical or '') == vertical]

    stages = {c.id: (c.dev_stage or '') for c in Company.query.all()}
    reached, eligible = Counter(), Counter()
    for lead in leads:
        stage = stages.get(lead.company_id, '')
        here = stage_rank(stage)
        skips = (lead.lead_channel or '') in SKIPS_EARLY
        for name in ACCOUNT_DEV_STAGES[:10]:
            if skips and name in EARLY:
                continue
            eligible[name] += 1
            if here >= stage_rank(name) >= 0:
                reached[name] += 1

    out = []
    for name in ACCOUNT_DEV_STAGES[:10]:
        could, got = eligible.get(name, 0), reached.get(name, 0)
        out.append({
            'stage': name, 'reached': got, 'eligible': could,
            'pct': (round(got * 100 / could) if could else None),
        })
    return out


def timing_counts(vertical=None):
    """Expected RFQ timing as labels. Never a timer colour (§6.3)."""
    from collections import Counter

    from app import Lead

    rows = Lead.query.filter(
        Lead.expected_rfq_timing.isnot(None)).all()
    if vertical:
        rows = [l for l in rows
                if (l.procam_vertical or '') == vertical]
    counts = Counter(l.expected_rfq_timing for l in rows)
    return [{'timing': t, 'count': counts.get(t, 0)}
            for t in EXPECTED_TIMING]


def rfq_forecast(months=3, vertical=None):
    """RFQs expected, by the month somebody put on the lead."""
    from collections import Counter

    from app import Lead

    rows = Lead.query.filter(
        Lead.rfq_expected_date.isnot(None),
        Lead.is_archived.isnot(True)).all()
    if vertical:
        rows = [l for l in rows
                if (l.procam_vertical or '') == vertical]
    counts = Counter(month_key(l.rfq_expected_date) for l in rows)
    return [{'month': m, 'count': n}
            for m, n in sorted(counts.items())]
