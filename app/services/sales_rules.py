"""
The sales rules, in one place.

The Daily Workbench, its emails, the Team Workbench, the sales reviews
and the Copilot all have to agree on what "overdue", "stale", "due
today" and "quote ageing" mean. Where the CRM already has a definition
this module imports it rather than writing a second one:

    what "contact" means         app/services/contact.py
    what a lead is worth         app/services/lead_value.py
    hygiene checks and their
    thresholds                   app/data_quality/{definitions,service}.py
    who may see a record         app/access/scope.py

What is new here is the **action matrix**: the conditions that put a
record in front of somebody today, each with its group, its priority and
how its due date is worked out. Nothing else in the codebase decides
that, so there is nothing to defer to.

Known conflicts this module does NOT resolve: the dashboard, the KPI
targets engine, PIC 360 and the Copilot each count "open" and "win rate"
their own way. Changing those is a behaviour change for existing
reports, so it is deliberately out of scope here; new surfaces read this
module, and the differences are listed in the release notes.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from app.data_quality import definitions as defs

# ── thresholds ───────────────────────────────────────────────────────
# The single place to change how impatient the Workbench is. Days unless
# the name says otherwise.

#: No contact (activity or email) for this long on an open lead and it
#: needs a nudge. The Copilot's "stale" default, kept the same.
IDLE_DAYS = 7
#: ... and for this long, it is properly stale.
STALE_DAYS = defs.NO_CONTACT_DAYS          # 30
#: A lead assigned within this window is "newly assigned".
NEW_ASSIGNMENT_HOURS = 48
#: A deal at or above this is a high-value priority (₹1 crore).
HIGH_VALUE_INR = Decimal('10000000')
#: When an RFQ carries no deadline of its own, a quote is expected this
#: many days after it was received.
QUOTE_SLA_DAYS = 3
#: An account nobody has touched for this long needs development.
ACCOUNT_QUIET_DAYS = 90

#: Quote/RFQ ageing buckets, in the order the dashboard shows them.
#: (key, label, lo, hi) — hi None means open-ended. Overdue is not a
#: bucket of age; it is a missed deadline and is counted separately.
AGEING_BUCKETS = (
    ('today', 'Today', 0, 0),
    ('d1_3', '1–3 days', 1, 3),
    ('d4_5', '4–5 days', 4, 5),
    ('d6_10', '6–10 days', 6, 10),
    ('d10_plus', 'More than 10 days', 11, None),
)

# ── groups (the Workbench's work groups) ─────────────────────────────
G_TODAY = 'action_today'
G_QUOTES = 'quotations'
G_FOLLOWUP = 'customer_followup'
G_STALE = 'stale'
G_DATA = 'data_update'
G_NEW = 'newly_assigned'
G_HIGH_VALUE = 'high_value'
G_ACCOUNTS = 'account_development'

GROUPS = (
    (G_TODAY, 'Action required today'),
    (G_QUOTES, 'Quotations'),
    (G_FOLLOWUP, 'Customer follow-up'),
    (G_STALE, 'Stale opportunities'),
    (G_DATA, 'Data update required'),
    (G_NEW, 'Newly assigned'),
    (G_HIGH_VALUE, 'High-value priorities'),
    (G_ACCOUNTS, 'Account development'),
)
GROUP_LABELS = dict(GROUPS)

#: Priority bands. 1 is the top of the list.
P_OVERDUE, P_TODAY, P_SOON, P_ROUTINE = 1, 2, 3, 4


#: The CRM's working day is India's. Comparing a business date against
#: utcnow().date() reads a day early every evening after 18:30 IST,
#: which is when "due today" and "days left" quietly go wrong.
BUSINESS_TZ = timedelta(hours=5, minutes=30)


def business_today():
    """Today where the business is, not where the server is."""
    return (datetime.utcnow() + BUSINESS_TZ).date()


# ── stage vocabulary ─────────────────────────────────────────────────
def open_stages():
    """Lead stages that are still in play."""
    from app import STAGES_PIPELINE
    return tuple(STAGES_PIPELINE)


def terminal_stages():
    from app import STAGES_TERMINAL
    return tuple(STAGES_TERMINAL)


def is_open(stage):
    return (stage or '') in open_stages()


def open_leads(sc=None, q=None):
    """Every open, unarchived lead the viewer may see."""
    from app import Lead
    from app.access import scope as sc_mod
    q = q if q is not None else Lead.query
    q = q.filter(Lead.is_archived.isnot(True),
                 Lead.stage.in_(open_stages()))
    return sc_mod.leads(q, sc=sc_mod.current() if sc is None else sc)


# ── ageing ───────────────────────────────────────────────────────────
def bucket_for(days):
    """Which ageing bucket a whole number of days falls in."""
    if days is None or days < 0:
        return None
    for key, _label, lo, hi in AGEING_BUCKETS:
        if days >= lo and (hi is None or days <= hi):
            return key
    return None


def empty_buckets():
    return {key: 0 for key, _l, _lo, _hi in AGEING_BUCKETS}


def days_between(earlier, later=None):
    """Whole days from `earlier` to `later` (today by default)."""
    if earlier is None:
        return None
    later = later or business_today()
    if isinstance(earlier, datetime):
        earlier = earlier.date()
    if isinstance(later, datetime):
        later = later.date()
    return (later - earlier).days


def idle_days_map(lead_ids):
    """{lead_id: days since the customer was last contacted}.

    None where nobody has ever been in touch. Two grouped queries for
    the whole set — never one per lead.
    """
    from app.services import contact
    if not lead_ids:
        return {}
    last = contact.last_contacts(list(lead_ids))
    today = business_today()
    return {lid: (days_between(last[lid], today) if last.get(lid) else None)
            for lid in lead_ids}


# ── value ────────────────────────────────────────────────────────────
def idle_days(lead, contacted_days):
    """How long this lead has been quiet.

    Days since the last contact where there has been one. Where nobody
    has ever been in touch, the lead's own age — a lead created this
    morning is not stale, and one created two months ago and never
    touched is the worst kind.
    """
    if contacted_days is not None:
        return contacted_days
    return days_between(getattr(lead, 'created_at', None))


def lead_value_inr(lead):
    from app.services import lead_value
    return lead_value.value_inr(lead)


def is_high_value(lead):
    v = lead_value_inr(lead)
    return v is not None and Decimal(str(v)) >= HIGH_VALUE_INR


# ── quote deadlines ──────────────────────────────────────────────────
def quote_due_date(rfq):
    """When a quote is expected for this RFQ.

    The RFQ's own deadline when it has one; otherwise the service
    standard, QUOTE_SLA_DAYS after it arrived. The two are distinguished
    by `quote_deadline_is_committed` so the UI never shows an assumed
    date as a promise to the customer.
    """
    if getattr(rfq, 'quote_by_date', None):
        return rfq.quote_by_date
    received = getattr(rfq, 'received_date', None)
    return (received + timedelta(days=QUOTE_SLA_DAYS)) if received else None


def quote_deadline_is_committed(rfq):
    return bool(getattr(rfq, 'quote_by_date', None))


def open_rfqs(sc=None, q=None):
    """RFQs still waiting on a quote, as the viewer may see them."""
    from app.models.rfq import RFQ
    from app.access import records
    q = q if q is not None else RFQ.query
    q = q.filter(RFQ.status.notin_(defs.RFQ_NO_QUOTE_NEEDED + ('Won', 'Quoted')))
    return records.rfqs(q, sc=sc)


def rfq_ageing(sc=None, today=None):
    """Every RFQ awaiting a quote, bucketed by age, with the overdue
    ones counted separately.

    Returns {'buckets': {key: [rows]}, 'overdue': [rows], 'counts': {...},
             'total': int}. One query for the RFQs, one for the quotes
    that already exist against them.
    """
    from app.models.quote import Quote
    from app import db
    today = today or business_today()
    rows, quoted = [], set()
    rfqs = open_rfqs(sc=sc).all()
    ids = [r.id for r in rfqs]
    if ids:
        quoted = {q[0] for q in db.session.query(Quote.rfq_id)
                  .filter(Quote.rfq_id.in_(ids)).all() if q[0]}
    counts, buckets = empty_buckets(), {k: [] for k, _l, _lo, _hi in AGEING_BUCKETS}
    overdue = []
    for r in rfqs:
        if r.id in quoted:
            continue
        age = days_between(r.received_date, today)
        due = quote_due_date(r)
        late = due is not None and due < today
        row = {
            'rfq_id': r.id,
            'rfq_number': r.rfq_number,
            'subject': r.subject or '',
            'account_id': r.account_id,
            'lead_id': r.lead_id,
            'received_date': str(r.received_date) if r.received_date else '',
            'quote_due': str(due) if due else '',
            'deadline_committed': quote_deadline_is_committed(r),
            'age_days': age,
            'days_late': (days_between(due, today) if late else 0),
            'status': r.status,
            'owner': r.lead_driver or '',
            'bucket': bucket_for(age),
        }
        rows.append(row)
        if late:
            overdue.append(row)
        key = row['bucket']
        if key:
            buckets[key].append(row)
            counts[key] += 1
    return {'buckets': buckets, 'overdue': overdue, 'counts': counts,
            'overdue_count': len(overdue), 'total': len(rows)}


# ── the action matrix ────────────────────────────────────────────────
#: (condition key, label, group, why it matters). The Workbench, the
#: daily email and the Copilot all read this list, so a condition is
#: added once and appears everywhere.
CONDITIONS = (
    ('followup_overdue', 'Follow-up overdue', G_TODAY,
     'The date you set for the next contact has passed.'),
    ('followup_today', 'Follow-up due today', G_TODAY,
     'You said you would come back to this today.'),
    ('quote_overdue', 'Quote past its deadline', G_QUOTES,
     'The customer is waiting beyond the date we committed to.'),
    ('quote_due_soon', 'Quote due', G_QUOTES,
     'A quote is expected within the next two days.'),
    ('quote_missing_detail', 'Quoted with no quote value or date', G_QUOTES,
     'Quote reporting and ageing cannot see this deal.'),
    ('quote_lapsed', 'Quote validity has passed', G_QUOTES,
     'The customer holds a price we no longer stand behind.'),
    ('negotiation_idle', 'Negotiation with no recent contact', G_FOLLOWUP,
     'A live negotiation nobody has touched.'),
    ('idle', 'No contact recently', G_FOLLOWUP,
     f'Nothing said to the customer for {IDLE_DAYS} days or more.'),
    ('stale', 'Going cold', G_STALE,
     f'No contact for {STALE_DAYS} days or more.'),
    ('no_next_action', 'No next action', G_DATA,
     'Nothing scheduled, so nothing will remind anyone.'),
    ('data_issue', 'Data update required', G_DATA,
     'A data-quality check is flagging this record.'),
    ('newly_assigned', 'Newly assigned', G_NEW,
     'Came to you in the last two days.'),
    ('high_value', 'High-value priority', G_HIGH_VALUE,
     f'Worth ₹{HIGH_VALUE_INR / 10000000:.0f} crore or more.'),
    ('account_quiet', 'Account not contacted', G_ACCOUNTS,
     f'No activity on the account for {ACCOUNT_QUIET_DAYS} days.'),
    ('account_next_action', 'Account action due', G_ACCOUNTS,
     'The account development plan has an action due.'),
)
CONDITION_LABELS = {k: lbl for k, lbl, _g, _w in CONDITIONS}
CONDITION_GROUP = {k: g for k, _l, g, _w in CONDITIONS}
CONDITION_WHY = {k: w for k, _l, _g, w in CONDITIONS}
