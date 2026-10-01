"""
Quote and RFQ escalation — Group C.

A customer who asks for a price by Friday and hears nothing on Monday
has not been let down by a missing feature; they have been let down
because nobody was told. This module is the telling: a ladder of levels
that climbs from a quiet reminder to the owner up to the level above the
vertical head, each one firing at a time an administrator sets.

The ladder
    rfq_received         an RFQ is logged — the owner is told it is theirs
    owner_reminder       a working day later, still no quote
    approaching_deadline the quote-by date is close
    overdue              the date has passed
    vertical_head        still nothing — the vertical head is told
    next_level           still nothing — the level above the head is told

Timings are configuration, not code
    Each level is a row in the Master Data list ``escalation_rule``,
    whose `meta` carries the hours and what they are counted from. An
    administrator changes when an escalation fires in the Master Data
    screen; nobody deploys. A level that has no row, or whose row has no
    usable hours, falls back to the default documented in DEFAULTS and in
    docs/operations/HYGIENE_AND_ESCALATION.md. Deactivating the row turns
    that level off.

What it reads, and what it does not re-decide
    Which RFQs are still waiting, and when their quote is due, come from
    ``app/services/sales_rules.py`` — the same answer the Workbench and
    the quote-ageing buckets give. There is no second opinion here about
    what "overdue" means.

Sending
    Every message goes through ``app/services/notify.py`` and nothing
    else, so an escalation is an in-app notification, an email where the
    level warrants one, and an audit entry, written the same way as every
    other thing the CRM tells somebody.

Not twice
    Each (record, level, recipient) is recorded in `escalation_log` once
    it has been accepted, and the unique key on that table — not a
    query — is what stops a second send. A delivery that failed records
    nothing, so the next sweep retries it.
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from app.services import sales_rules as rules

#: The Master Data list an administrator edits. One item per level.
LIST_KEY = 'escalation_rule'
LIST_LABEL = 'Escalation Timings'
LIST_DESCRIPTION = (
    'When each quote/RFQ escalation level fires. `hours` is counted from '
    'what `anchor` names: "received" (when the RFQ arrived) or "deadline" '
    '(the quote-by date, or the service standard where the customer gave '
    'none). Negative hours fire before the anchor. Deactivate an item to '
    'switch that level off.')

#: What the record is, in the log and in the notifications.
ENTITY = 'rfq'

#: Anchors a level can be counted from.
A_RECEIVED, A_DEADLINE = 'received', 'deadline'

#: Who a level reaches. Resolved against the employee master at send
#: time, by role, never by name.
T_OWNER, T_HEAD, T_NEXT = 'owner', 'vertical_head', 'next_level'


def _d(key, label, hours, anchor, to, email, subject, body):
    return {'key': key, 'label': label, 'hours': hours, 'anchor': anchor,
            'to': to, 'email': email, 'subject': subject, 'body': body,
            'active': True}


#: The ladder, in the order it climbs, with the timings that apply when
#: Master Data says nothing. These defaults are quoted in
#: docs/operations/HYGIENE_AND_ESCALATION.md — changing one here is a
#: change to a published document.
DEFAULTS = (
    _d('rfq_received', 'RFQ received', 0, A_RECEIVED, T_OWNER, False,
       'RFQ {ref} is yours to quote',
       'An RFQ from {account} is logged against you. A quote is expected '
       'by {due}.'),
    _d('owner_reminder', 'Owner reminder', 24, A_RECEIVED, T_OWNER, False,
       'RFQ {ref} still has no quote',
       'This RFQ from {account} arrived {age} day(s) ago and no quote has '
       'been recorded. It is due by {due}.'),
    _d('approaching_deadline', 'Approaching the deadline', -24, A_DEADLINE,
       T_OWNER, True,
       'RFQ {ref} is due by {due}',
       'The quote for {account} is due by {due} and none has been '
       'recorded yet.'),
    _d('overdue', 'Past the deadline', 0, A_DEADLINE, T_OWNER, True,
       'RFQ {ref} has missed its quote date',
       'The quote for {account} was due by {due} and none has been '
       'recorded. The customer is waiting.'),
    _d('vertical_head', 'Escalated to the vertical head', 24, A_DEADLINE,
       T_HEAD, True,
       'Escalation: RFQ {ref} is {late} day(s) past its quote date',
       'The quote for {account} was due by {due} and none has been '
       'recorded. It sits with {owner}.'),
    _d('next_level', 'Escalated to the next level', 72, A_DEADLINE, T_NEXT,
       True,
       'Escalation: RFQ {ref} is {late} day(s) past its quote date',
       'The quote for {account} was due by {due}, none has been recorded, '
       'and the reminder to the vertical head has not cleared it. It sits '
       'with {owner}.'),
)

STAGE_KEYS = tuple(d['key'] for d in DEFAULTS)
BY_KEY = {d['key']: d for d in DEFAULTS}

#: An escalation whose moment passed longer ago than this is not sent.
#: Without it, the first sweep after a deployment would mail every owner
#: about every RFQ the CRM has ever held. Configurable the same way, as
#: the item `max_backlog`.
MAX_BACKLOG_HOURS = 30 * 24
MAX_BACKLOG_KEY = 'max_backlog'


# ── configuration ────────────────────────────────────────────────────
def _meta_hours(meta, fallback):
    """`hours` out of an item's meta, whatever an administrator typed.

    A value that is not a number is ignored rather than guessed at: the
    documented default is always a safe answer, and a typo must not
    silently stop an escalation or fire it at the wrong hour.
    """
    if not isinstance(meta, dict):
        return fallback
    for key in ('hours', 'hour', 'h'):
        if key in meta and meta[key] not in (None, ''):
            try:
                return float(meta[key])
            except (TypeError, ValueError):
                return fallback
    return fallback


def _items():
    """{code: MasterItem} for the list, including deactivated ones."""
    try:
        from app.master_data import service as md
        return {i.code: i for i in md.items(LIST_KEY, include_inactive=True)}
    except Exception:
        # No Master Data yet (a fresh database, a script before the
        # migration) is not a reason to stop escalating.
        return {}


def ladder():
    """The levels as configured, in the order they climb.

    Every level appears, with `active` saying whether it will fire and
    `source` saying whether its timing came from Master Data or from the
    documented default — so an administrator can see what is actually in
    force rather than what they believe they set.
    """
    items = _items()
    out = []
    for d in DEFAULTS:
        item = items.get(d['key'])
        stage = dict(d)
        if item is None:
            stage['source'] = 'default'
        else:
            stage['source'] = 'master data'
            stage['hours'] = _meta_hours(item.meta, d['hours'])
            meta = item.meta if isinstance(item.meta, dict) else {}
            anchor = str(meta.get('anchor') or '').strip().lower()
            if anchor in (A_RECEIVED, A_DEADLINE):
                stage['anchor'] = anchor
            to = str(meta.get('to') or '').strip().lower()
            if to in (T_OWNER, T_HEAD, T_NEXT):
                stage['to'] = to
            if 'email' in meta:
                stage['email'] = bool(meta['email'])
            stage['active'] = bool(item.is_active)
            if item.label:
                stage['label'] = item.label
        out.append(stage)
    return out


def max_backlog_hours():
    item = _items().get(MAX_BACKLOG_KEY)
    return _meta_hours(getattr(item, 'meta', None), MAX_BACKLOG_HOURS)


def ensure_rules(actor='system'):
    """Register the list and seed the defaults. Safe to run repeatedly.

    Called by the migration and by the sweep, so a database that has
    never seen the escalation configuration still shows an administrator
    what they can change rather than an empty screen.
    """
    from app import db
    from app.models.master_data import MasterItem, MasterList

    created = 0
    if MasterList.query.filter_by(key=LIST_KEY).first() is None:
        db.session.add(MasterList(key=LIST_KEY, label=LIST_LABEL,
                                  description=LIST_DESCRIPTION,
                                  is_system=True, sort_order=900))
        created += 1
    have = {i.code for i in MasterItem.query.filter_by(list_key=LIST_KEY).all()}
    order = 0
    for d in DEFAULTS:
        order += 10
        if d['key'] in have:
            continue
        db.session.add(MasterItem(
            list_key=LIST_KEY, code=d['key'], label=d['label'],
            description=f"{d['hours']:g} hour(s) from the {d['anchor']}, "
                        f"to the {d['to'].replace('_', ' ')}.",
            meta={'hours': d['hours'], 'anchor': d['anchor'], 'to': d['to'],
                  'email': d['email']},
            sort_order=order, is_active=True, created_by=actor))
        created += 1
    if MAX_BACKLOG_KEY not in have:
        db.session.add(MasterItem(
            list_key=LIST_KEY, code=MAX_BACKLOG_KEY,
            label='Oldest escalation worth sending',
            description='An escalation whose moment passed longer ago than '
                        'this many hours is not sent at all.',
            meta={'hours': MAX_BACKLOG_HOURS}, sort_order=order + 10,
            is_active=True, created_by=actor))
        created += 1
    if created:
        db.session.commit()
    return created


def ensure_table():
    """Create `escalation_log` if the migration has not run yet.

    Additive and idempotent. The sweep is a cron job: failing because a
    table is missing means silence, and silence is the failure this
    module exists to prevent.
    """
    from app import db
    from app.models.escalation import EscalationLog
    EscalationLog.__table__.create(db.engine, checkfirst=True)


# ── when a level is due ──────────────────────────────────────────────
def received_at(rfq):
    """When the RFQ arrived, as an instant on the server's clock.

    `received_date` is a date, so the start of that business day is used
    where there is no creation timestamp to be more precise with.
    """
    created = getattr(rfq, 'created_at', None)
    if created:
        return created
    day = getattr(rfq, 'received_date', None)
    if not day:
        return None
    return datetime.combine(day, time(0, 0)) - rules.BUSINESS_TZ


def deadline_at(rfq):
    """The instant a quote becomes late.

    The quote is due *on* a date, so it is late at the end of that
    business day, not at its start — a quote sent on Friday afternoon
    against a Friday deadline is on time.
    """
    due = rules.quote_due_date(rfq)
    if not due:
        return None
    return datetime.combine(due, time(23, 59)) - rules.BUSINESS_TZ


def due_at(stage, rfq):
    """When this level fires for this RFQ, or None if it cannot."""
    anchor = (received_at(rfq) if stage['anchor'] == A_RECEIVED
              else deadline_at(rfq))
    if anchor is None:
        return None
    return anchor + timedelta(hours=float(stage['hours']))


# ── who it reaches ───────────────────────────────────────────────────
def _employee(code):
    from app import Employee
    code = (code or '').strip()
    return Employee.query.filter_by(emp_code=code).first() if code else None


def owner_of(rfq):
    """Whose RFQ this is: its driver, else the lead's owner, else the
    account's. Role terms throughout — the chain is about the seat, not
    the person sitting in it."""
    code = (getattr(rfq, 'lead_driver', '') or '').strip()
    if code:
        return code
    lead_id = getattr(rfq, 'lead_id', None)
    if lead_id:
        from app import Lead
        lead = Lead.query.get(lead_id)
        if lead and (lead.assigned_to or '').strip():
            return lead.assigned_to.strip()
    account_id = getattr(rfq, 'account_id', None)
    if account_id:
        from app import Company
        acc = Company.query.get(account_id)
        if acc and (acc.pic_emp_code or '').strip():
            return acc.pic_emp_code.strip()
    return ''


def head_of(emp_code):
    """The vertical head above an owner.

    Their named manager first; failing that the active head of their
    vertical. An owner who is their own manager is not escalated to
    themselves — that would be an escalation that changes nothing.
    """
    from app import Employee
    emp = _employee(emp_code)
    if emp is None:
        return ''
    if emp.vertical_head_id:
        head = Employee.query.get(emp.vertical_head_id)
        if head and head.is_active and head.emp_code != emp_code:
            return head.emp_code
    vertical = (emp.vertical or '').strip()
    if vertical:
        head = Employee.query.filter(
            Employee.is_active.is_(True),
            Employee.is_vertical_head.is_(True),
            Employee.vertical == vertical,
            Employee.emp_code != emp_code).first()
        if head:
            return head.emp_code
    return ''


def next_level_of(emp_code):
    """Above the vertical head: their own manager, else whoever holds the
    CRM at company level. A list, because that seat may be shared."""
    from app import Employee
    head = head_of(emp_code)
    if head:
        above = head_of(head)
        if above and above not in (emp_code, head):
            return [above]
    seen = []
    for emp in Employee.query.filter(
            Employee.is_active.is_(True),
            Employee.is_super_admin.is_(True)).all():
        if emp.emp_code and emp.emp_code not in (emp_code, head):
            seen.append(emp.emp_code)
    if seen:
        return seen
    for emp in Employee.query.filter(Employee.is_active.is_(True),
                                     Employee.role == 'admin').all():
        if emp.emp_code and emp.emp_code not in (emp_code, head):
            seen.append(emp.emp_code)
    return seen


def recipients(stage, rfq, owner=None):
    """Who this level of the ladder reaches, as a list of emp_codes."""
    owner = owner if owner is not None else owner_of(rfq)
    if stage['to'] == T_OWNER:
        return [owner] if owner else []
    if stage['to'] == T_HEAD:
        head = head_of(owner)
        return [head] if head else []
    return [c for c in next_level_of(owner) if c]


# ── what is waiting ──────────────────────────────────────────────────
def open_rfqs(sc=None):
    """RFQs still waiting on a quote.

    The same set the quote-ageing buckets count: still open by status,
    and with no quote recorded against them. Two queries for the whole
    set, as in sales_rules.rfq_ageing — this needs the records
    themselves, which that function does not hand back.
    """
    from app import db
    from app.data_quality import service as dq
    from app.models.quote import Quote
    # The sweep has no signed-in viewer, and the access helpers ask the
    # session for one. Outside a request the boundary is the whole
    # company, as it is for the other scheduled jobs.
    sc = sc if sc is not None else dq.resolve_scope(None)
    rows = rules.open_rfqs(sc=sc).all()
    ids = [r.id for r in rows]
    quoted = set()
    if ids:
        quoted = {q[0] for q in db.session.query(Quote.rfq_id)
                  .filter(Quote.rfq_id.in_(ids)).all() if q[0]}
    return [r for r in rows if r.id not in quoted]


def _dedupe_key(entity_id, stage_key, recipient):
    return f'{ENTITY}:{entity_id}:{stage_key}:{recipient}'


def already_sent(entity_id, stage_key, recipient):
    from app.models.escalation import EscalationLog
    return EscalationLog.query.filter_by(
        dedupe_key=_dedupe_key(entity_id, stage_key, recipient)).first()


def _account_name(rfq):
    account_id = getattr(rfq, 'account_id', None)
    if account_id:
        from app import Company
        acc = Company.query.get(account_id)
        if acc and acc.name:
            return acc.name
    return (getattr(rfq, 'subject', '') or 'this customer')


def _message(stage, rfq, owner, now):
    due = rules.quote_due_date(rfq)
    late = rules.days_between(due, rules.business_today()) if due else 0
    fields = {
        'ref': getattr(rfq, 'rfq_number', '') or f'#{rfq.id}',
        'account': _account_name(rfq),
        'due': str(due) if due else 'a date nobody has set',
        'age': rules.days_between(getattr(rfq, 'received_date', None)) or 0,
        'late': max(0, late or 0),
        'owner': owner or 'nobody',
    }
    return (stage['subject'].format(**fields),
            stage['body'].format(**fields))


def pending(now=None, sc=None, horizon=None):
    """Every escalation that is due and has not been sent.

    Pure: it reads, it decides, it writes nothing. The sweep and its
    --dry-run both go through this, so what a dry run prints is exactly
    what a real run would send.
    """
    now = now or datetime.utcnow()
    horizon = max_backlog_hours() if horizon is None else horizon
    oldest = now - timedelta(hours=float(horizon))
    stages = [s for s in ladder() if s['active']]
    out = []
    for rfq in open_rfqs(sc=sc):
        owner = owner_of(rfq)
        for stage in stages:
            when = due_at(stage, rfq)
            if when is None or when > now or when < oldest:
                continue
            for who in recipients(stage, rfq, owner=owner):
                if already_sent(rfq.id, stage['key'], who):
                    continue
                subject, body = _message(stage, rfq, owner, now)
                out.append({
                    'entity_type': ENTITY, 'entity_id': rfq.id,
                    'reference': getattr(rfq, 'rfq_number', '') or f'#{rfq.id}',
                    'stage': stage['key'], 'stage_label': stage['label'],
                    'to': who, 'owner': owner, 'due_at': when,
                    'email': bool(stage['email']),
                    'route': f'/rfqs/{rfq.id}',
                    'subject': subject, 'body': body})
    out.sort(key=lambda p: (p['due_at'], p['entity_id'],
                            STAGE_KEYS.index(p['stage'])
                            if p['stage'] in STAGE_KEYS else 99))
    return out


# ── sending ──────────────────────────────────────────────────────────
def send_one(plan, actor='system'):
    """Send one escalation and record it. Never raises.

    Recorded only once the notifier has accepted it, so a mail server
    that was down for an hour costs a delay, not a missed escalation.
    """
    from app import db
    from app.models.escalation import EscalationLog
    from app.services import notify

    try:
        out = notify.send(
            plan['to'], kind=f'escalation.{plan["stage"]}',
            title=plan['subject'], body=plan['body'], url=plan['route'],
            entity_type=plan['entity_type'], entity_id=plan['entity_id'],
            email=plan['email'], actor=actor,
            audit_action='escalation.sent',
            reason=f'{plan["stage_label"]} — {plan["reference"]}')
    except Exception as exc:                            # pragma: no cover
        return {'ok': False, 'error': str(exc)[:200], **plan}

    delivered = bool(out.get('notified') or out.get('suppressed'))
    if not delivered:
        return {'ok': False, 'error': out.get('error') or 'not delivered',
                **plan}
    try:
        db.session.add(EscalationLog(
            entity_type=plan['entity_type'], entity_id=plan['entity_id'],
            stage=plan['stage'], recipient=plan['to'],
            dedupe_key=_dedupe_key(plan['entity_id'], plan['stage'],
                                   plan['to']),
            due_at=plan['due_at'], sent_at=datetime.utcnow(),
            channel='notification+email' if out.get('emailed')
                    else 'notification',
            suppressed=bool(out.get('suppressed')),
            note=plan['stage_label'][:300]))
        db.session.commit()
    except Exception as exc:
        # The unique key did its job: another sweep got there first.
        db.session.rollback()
        return {'ok': True, 'duplicate': True, 'error': str(exc)[:120],
                **plan}
    return {'ok': True, 'emailed': bool(out.get('emailed')),
            'suppressed': bool(out.get('suppressed')), **plan}


def run(now=None, *, dry_run=False, sc=None, actor='system'):
    """One sweep. Returns what was sent, what failed and what was skipped.

    One record's failure never stops the sweep: a single RFQ with a
    broken account link must not keep every other escalation in the
    queue from going out.
    """
    now = now or datetime.utcnow()
    # Even a dry run has to read the log to know what has already gone
    # out, so an empty log table is created if it is missing. That is
    # the only thing a dry run writes: nothing is sent, and no row is
    # added to it.
    ensure_table()
    plans = pending(now=now, sc=sc)
    report = {'now': now, 'considered': len(plans), 'sent': [],
              'failed': [], 'dry_run': bool(dry_run)}
    if dry_run:
        report['would_send'] = plans
        return report
    for plan in plans:
        try:
            out = send_one(plan, actor=actor)
        except Exception as exc:                        # pragma: no cover
            out = {'ok': False, 'error': str(exc)[:200], **plan}
        (report['sent'] if out.get('ok') else report['failed']).append(out)
    return report
