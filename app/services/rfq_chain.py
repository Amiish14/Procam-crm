"""
The twelve steps from RFQ received to handover.

CRM-RFQ-CHAIN-01 (R2), behind `FEATURE_RFQ_CHAIN`.

The SCM team tracks this chain on a spreadsheet today: twelve
steps, a standard time per step derived from the RFQ's priority,
and a workload table per person. Nobody can see where an RFQ is
stuck, who is holding it, or how long each person took, because
the timestamps that would answer that are in nobody's hands.

This is not a second engine. The steps are `TaskDefinition` rows
and the live ones are `TaskInstance` rows, the same engine that
already runs twenty definitions and 3,693 instances. What R2 adds
is the four things that engine has no word for: which step of a
chain a task is, how long it was held, whether it finished early
or late, and why.

Two rules worth stating because they are easy to lose:

    Steps close on events, not on somebody remembering. The cost
    sheet saving closes step 6; approval closes step 7; the quote
    going out closes step 8. Only Screening, Technical Evaluation
    and Survey take a Confirm click, because only those three
    have no event of their own.

    A reason is asked only on an exception. Every step asking
    "why" trains people to pick the first option; asking only
    when a step ran late or unusually early keeps the answer
    worth reading.
"""
from __future__ import annotations

from datetime import datetime, timedelta

FLAG = 'FEATURE_RFQ_CHAIN'

#: The twelve, in order. `share` is the fraction of the priority
#: window this step is expected to take; `event` names what closes
#: it, and 'confirm' means a person clicks.
#:
#: Steps 9-11 carry no share: 9 and 10 run on a response clock per
#: revision, and 11 waits on the customer's own decision, which is
#: not a Procam delay and must never be coloured as one.
STEPS = (
    {'no': 1,  'key': 'rfq.received',     'title': 'RFQ Received',
     'who': 'sales',       'share': 0.00, 'event': 'rfq_mail_linked'},
    {'no': 2,  'key': 'rfq.registration', 'title': 'RFQ Registration',
     'who': 'coordinator', 'share': 0.05, 'event': 'registered'},
    {'no': 3,  'key': 'rfq.screening',    'title': 'Initial Screening',
     'who': 'scm',         'share': 0.05, 'event': 'confirm'},
    {'no': 4,  'key': 'rfq.technical',    'title': 'Technical Evaluation',
     'who': 'ops',         'share': 0.15, 'event': 'confirm',
     'optional': 'needs_technical'},
    {'no': 5,  'key': 'rfq.survey',       'title': 'Site / Route Survey',
     'who': 'ops',         'share': 0.15, 'event': 'confirm',
     'optional': 'needs_survey'},
    {'no': 6,  'key': 'rfq.costing',      'title': 'Cost Estimation',
     'who': 'scm',         'share': 0.30, 'event': 'cost_sheet_saved'},
    {'no': 7,  'key': 'rfq.approval',     'title': 'Commercial Approval',
     'who': 'head',        'share': 0.10, 'event': 'quote_approved'},
    {'no': 8,  'key': 'rfq.submission',   'title': 'Quotation Submission',
     'who': 'sales',       'share': 0.20, 'event': 'quote_sent'},
    {'no': 9,  'key': 'rfq.negotiation',  'title': 'Negotiation',
     'who': 'sales',       'share': None, 'event': 'customer_replied',
     'response_clock': True},
    {'no': 10, 'key': 'rfq.final_bid',    'title': 'Final Bid / Closure',
     'who': 'sales',       'share': None, 'event': 'award_date_recorded',
     'response_clock': True},
    {'no': 11, 'key': 'rfq.award',        'title': 'Award / PO',
     'who': 'sales',       'share': None, 'event': 'po_recorded',
     'customer_clock': True},
    {'no': 12, 'key': 'rfq.handover',     'title': 'Handover',
     'who': 'sales',       'share': None, 'event': 'handover_sent',
     'fixed_hours': 24},
)
BY_NO = {s['no']: s for s in STEPS}
BY_KEY = {s['key']: s for s in STEPS}

#: Steps a person may close by hand. Everything else waits for its
#: event: a chain whose steps can all be ticked is a spreadsheet
#: with extra clicks.
CONFIRMABLE = tuple(s['no'] for s in STEPS if s['event'] == 'confirm')

#: D3. Days from RFQ received to quote sent, per priority.
PRIORITY_DAYS = {
    'Critical': 2,
    'High': 4,
    'Medium': 7,
    'Low': 10,
}
DEFAULT_PRIORITY = 'Medium'

#: §4.1, step 9-10: Procam's own response time per revision.
RESPONSE_HOURS = {'Critical': 4, 'High': 8, 'Medium': 24, 'Low': 24}

#: D11. Inside this fraction of the standard time is "Early".
EARLY_FRACTION = 0.5
ON_TIME_TARGET = 0.98

EARLY, ON_TIME, DELAYED = 'Early', 'On time', 'Delayed'

RED, ORANGE, GREEN, PURPLE, BLUE, GREY = (
    'red', 'orange', 'green', 'purple', 'blue', 'grey')


# ── settings, with the spec's numbers as the fallback ───────────────
def _setting(key, default):
    try:
        from app.services import admin_settings
        got = admin_settings.get(key)
        return default if got in (None, '') else got
    except Exception:                                     # noqa: BLE001
        return default


def priority_days(priority):
    days = _setting('rfq_priority_days', None) or {}
    try:
        return float(days.get(priority))
    except (AttributeError, TypeError, ValueError):
        return float(PRIORITY_DAYS.get(priority,
                                       PRIORITY_DAYS[DEFAULT_PRIORITY]))


def share_of(step_no):
    shares = _setting('rfq_step_shares', None) or {}
    try:
        return float(shares[str(step_no)])
    except (KeyError, TypeError, ValueError):
        return BY_NO[step_no].get('share')


def standard_hours(step_no, priority):
    """How long this step is expected to take, in hours, or None.

    None is a real answer for steps 9-11: a response clock and the
    customer's own clock are not a share of our window, and
    pretending otherwise would colour a customer's silence as a
    Procam delay.
    """
    step = BY_NO.get(step_no)
    if step is None:
        return None
    if step.get('fixed_hours'):
        return float(step['fixed_hours'])
    if step.get('customer_clock'):
        return None
    if step.get('response_clock'):
        hours = _setting('rfq_response_hours', None) or {}
        try:
            return float(hours[priority])
        except (KeyError, TypeError, ValueError):
            return float(RESPONSE_HOURS.get(
                priority, RESPONSE_HOURS[DEFAULT_PRIORITY]))
    share = share_of(step_no)
    if not share:
        return None
    return priority_days(priority) * 24.0 * float(share)


def early_fraction():
    try:
        return float(_setting('rfq_early_fraction', EARLY_FRACTION))
    except (TypeError, ValueError):
        return EARLY_FRACTION


# ── the clock ────────────────────────────────────────────────────────
def elapsed_hours(task, *, now=None):
    """Hours this step has been live, with held time removed."""
    now = now or datetime.utcnow()
    started = getattr(task, 'started_at', None) \
        or getattr(task, 'created_at', None)
    if started is None:
        return 0.0
    end = getattr(task, 'completed_at', None) or now
    seconds = max(0.0, (end - started).total_seconds())
    seconds -= float(getattr(task, 'paused_seconds', 0) or 0)
    return max(0.0, seconds) / 3600.0


def verdict(task, *, priority=None, now=None):
    """(colour, words) for one live or finished step."""
    step_no = getattr(task, 'step_no', None)
    if step_no is None:
        return GREY, 'not part of the chain'
    if getattr(task, 'held_at', None):
        return PURPLE, 'on hold'

    step = BY_NO.get(step_no, {})
    standard = standard_hours(step_no, priority or DEFAULT_PRIORITY)
    if standard is None:
        return BLUE, ('waiting on the customer'
                      if step.get('customer_clock')
                      else 'no standard time')

    spent = elapsed_hours(task, now=now)
    left = standard - spent
    if left < 0:
        return RED, f'overdue {_hours(-left)}'
    # Orange inside the last 24 hours, or the last half of a short
    # step — a Critical step of three hours must warn before it is
    # two hours from due, which a flat 24h rule never would.
    soon = min(24.0, standard * 0.5)
    if left <= soon:
        return ORANGE, f'due in {_hours(left)}'
    return GREEN, f'due in {_hours(left)}'


def _hours(value):
    if value >= 48:
        return f'{value / 24:.0f}d'
    if value >= 1:
        return f'{value:.0f}h'
    return f'{max(1, int(value * 60))}m'


def result_for(task, *, priority=None):
    """Early / On time / Delayed, measured not typed."""
    standard = standard_hours(getattr(task, 'step_no', None) or 0,
                              priority or DEFAULT_PRIORITY)
    if standard is None:
        return None
    spent = elapsed_hours(task)
    if spent > standard:
        return DELAYED
    if spent <= standard * early_fraction():
        return EARLY
    return ON_TIME


def needs_reason(result):
    """Which reason a close must carry, or None.

    Only the exceptions. A reason on every step is a reason nobody
    reads, and the pre-selected first option becomes the answer.
    """
    if result == DELAYED:
        return 'delay'
    if result == EARLY:
        return 'good_practice'
    return None


def suggest_priority(deadline, *, today=None):
    """§4.4 — suggested from how long the customer has given us."""
    if deadline is None:
        return DEFAULT_PRIORITY
    today = today or datetime.utcnow().date()
    days = (deadline - today).days
    if days < 2:
        return 'Critical'
    if days < 4:
        return 'High'
    if days < 7:
        return 'Medium'
    return 'Low'


def applicable(rfq):
    """The steps this RFQ actually runs, in order.

    Technical evaluation and the survey are ticked at registration
    and are N/A for air and sea freight. A chain that always shows
    twelve boxes when two never happen reports a false bottleneck
    at the two that stay grey forever.
    """
    out = []
    for step in STEPS:
        flag = step.get('optional')
        if flag and not bool(getattr(rfq, flag, False)):
            continue
        out.append(step)
    return out


# ── the chain, as task instances ─────────────────────────────────────
def enabled(emp_code=None):
    from flask import has_request_context, session

    from app.services import flags

    if emp_code is None and has_request_context():
        # See account_gate.enabled: inside a request, no session
        # means no one the limit can admit.
        return flags.on_for(FLAG, session.get('emp_code'))
    if emp_code is None:
        return flags.on(FLAG)
    return flags.on_for(FLAG, emp_code)


def steps_for(rfq):
    """Every chain task on this RFQ, by step number."""
    from app.models.task_engine import TaskInstance

    rows = (TaskInstance.query
            .filter(TaskInstance.entity_type == 'RFQ',
                    TaskInstance.entity_id == rfq.id,
                    TaskInstance.step_no.isnot(None))
            .order_by(TaskInstance.step_no).all())
    return {row.step_no: row for row in rows}


def open_step(rfq, step_no, *, owner=None, actor='system'):
    """Open one step. Idempotent — a step already open stays as is."""
    from app import db
    from app.models.task_engine import TaskInstance, TaskInstanceStatus

    step = BY_NO.get(step_no)
    if step is None:
        return None
    existing = steps_for(rfq).get(step_no)
    if existing is not None and existing.status in \
            TaskInstanceStatus.OPEN:
        return existing
    if existing is not None:
        return None                    # already closed; never reopen

    hours = standard_hours(step_no, rfq.priority or DEFAULT_PRIORITY)
    now = datetime.utcnow()
    row = TaskInstance(
        task_key=step['key'], entity_type='RFQ', entity_id=rfq.id,
        entity_display=(rfq.rfq_number or f'RFQ #{rfq.id}')[:120],
        owner_user_id=owner or _owner_for(rfq, step),
        status=TaskInstanceStatus.PENDING, priority=3,
        step_no=step_no, paused_seconds=0,
        action_route=f'/rfqs/{rfq.id}',
        started_at=now, created_at=now,
        due_at=(now + timedelta(hours=hours)) if hours else None)
    db.session.add(row)
    db.session.flush()
    return row


def _owner_for(rfq, step):
    """Who holds this step. Never a name in code (§4.1)."""
    from app.services import admin_settings

    who = step.get('who')
    if who == 'sales':
        return _account_owner(rfq) or rfq.lead_driver
    if who == 'coordinator':
        return (admin_settings.get('rfq_coordinator') or '') or None
    if who in ('scm', 'ops'):
        table = admin_settings.get(f'rfq_{who}_by_vertical') or {}
        vertical = _vertical_of(rfq)
        if isinstance(table, dict) and vertical:
            found = table.get(vertical)
            if found:
                return found
        return None
    if who == 'head':
        return _head_of(_vertical_of(rfq))
    return None


def _vertical_of(record):
    from app.services.vertical_scope import vertical_name
    return vertical_name(record)


def _head_of(vertical):
    try:
        from app.services import vertical_heads as vh
        found = vh.heads_for_vertical(vertical) or []
        return found[0] if found else None
    except Exception:                                     # noqa: BLE001
        return None


def _account_owner(rfq):
    try:
        from app import Company, db
        from app.services import auto_assign
        if not rfq.account_id:
            return None
        account = db.session.get(Company, rfq.account_id)
        return auto_assign.owner_for_vertical(
            rfq.account_id, _vertical_of(rfq)) \
            or (getattr(account, 'pic_emp_code', None) or None)
    except Exception:                                     # noqa: BLE001
        return None


def close_step(rfq, step_no, *, actor, reason=None, remarks='',
               event='', owner_next=None, evidence=None):
    """Close a step and open the next. (ok, detail-or-why).

    The reason rules live here rather than in each caller, because
    there are six callers and the one that forgets is the one that
    makes the whole report meaningless.
    """
    from app import db
    from app.models.task_engine import TaskInstanceStatus
    from app.services import audit

    task = steps_for(rfq).get(step_no)
    if task is None:
        return False, f'step {step_no} is not open on this RFQ'
    if task.status not in TaskInstanceStatus.OPEN:
        return False, f'step {step_no} is already closed'
    if task.held_at:
        return False, 'the step is on hold — reactivate it first'

    step = BY_NO[step_no]
    if step['event'] == 'confirm':
        if event and event != 'confirm':
            return False, (f'{step["title"]} closes on a person '
                           f'confirming it, not on {event}')
    elif event and event != step['event']:
        return False, (f'{step["title"]} closes on {step["event"]}, '
                       f'not on {event}')

    result = result_for(task, priority=rfq.priority)
    wanted = needs_reason(result)
    if wanted and not (reason or '').strip():
        return False, (f'this step finished '
                       f'{"late" if result == DELAYED else "early"} — '
                       f'choose a {wanted.replace("_", " ")} reason')
    if (reason or '').strip().lower().startswith('other') \
            and not (remarks or '').strip():
        return False, '"Others" needs a remark saying what happened'

    task.status = TaskInstanceStatus.COMPLETED
    task.completed_at = datetime.utcnow()
    task.completed_by_id = actor
    task.result = result
    if wanted == 'delay':
        task.delay_reason = reason
    elif wanted == 'good_practice':
        task.good_practice_reason = reason
    task.reason_remarks = (remarks or '') or None
    if evidence:
        task.closed_evidence = dict(evidence)

    audit.record('rfq.step_closed', 'rfq', rfq.id,
                 new={'step_no': step_no, 'step': step['title'],
                      'result': result, 'reason': reason or '',
                      'by': actor, 'evidence': evidence or {}},
                 actor=actor,
                 reason=f'{step["title"]} closed '
                        f'({result or "no standard time"})')

    nxt = next_step(rfq, step_no)
    opened = open_step(rfq, nxt, owner=owner_next,
                       actor=actor) if nxt else None
    db.session.flush()
    return True, {'result': result, 'next': nxt,
                  'next_owner': getattr(opened, 'owner_user_id', None)}


def next_step(rfq, step_no):
    """The next step this RFQ actually runs, skipping the N/A ones."""
    for step in applicable(rfq):
        if step['no'] > step_no:
            return step['no']
    return None


def suggest_reason(rfq, step_no):
    """Pre-select a delay reason from evidence (§4.3).

    Evidence, not a guess: an open vendor rate request is why
    costing is late, and the CRM already knows it is open.
    """
    try:
        from app.models.ff import RateRequest
        waiting = RateRequest.query.filter(
            RateRequest.rfq_id == rfq.id,
            RateRequest.status.in_(('Sent', 'Awaiting'))).count()
        if waiting and step_no in (5, 6):
            return 'Awaiting vendor rate'
    except Exception:                                     # noqa: BLE001
        pass
    task = steps_for(rfq).get(step_no)
    if task is not None and getattr(task, 'waiting_for_customer',
                                    False):
        return 'Customer clarification pending'
    return ''


# ── hold (D5) ────────────────────────────────────────────────────────
def may_hold(emp_code, rfq):
    """Only the vertical head for this RFQ, or a director."""
    from app import Employee

    emp = Employee.query.filter_by(emp_code=emp_code).first()
    if emp is None:
        return False
    if (getattr(emp, 'role', '') or '').lower() in (
            'admin', 'super admin', 'superadmin', 'director'):
        return True
    return emp_code == _head_of(_vertical_of(rfq))


def hold(rfq, *, actor, reason, remarks=''):
    from app import db
    from app.models.task_engine import TaskInstanceStatus
    from app.services import audit

    if not may_hold(actor, rfq):
        return False, ('only the vertical head or a director can '
                       'hold an RFQ')
    if not (reason or '').strip():
        return False, 'a hold always needs a reason'
    if rfq.held_at:
        return False, 'already on hold'

    now = datetime.utcnow()
    rfq.held_at, rfq.held_by = now, actor
    rfq.hold_reason, rfq.hold_remarks = reason, (remarks or None)
    for task in steps_for(rfq).values():
        if task.status in TaskInstanceStatus.OPEN:
            task.held_at = now
    audit.record('rfq.held', 'rfq', rfq.id,
                 new={'reason': reason, 'remarks': remarks},
                 actor=actor, reason=f'on hold: {reason}')
    db.session.flush()
    return True, 'held'


def reactivate(rfq, *, actor, remarks=''):
    """Add the held time to each step, and start the clock again."""
    from app import db
    from app.models.task_engine import TaskInstanceStatus
    from app.services import audit

    if not may_hold(actor, rfq):
        return False, ('only the vertical head or a director can '
                       'reactivate an RFQ')
    if not rfq.held_at:
        return False, 'not on hold'

    now = datetime.utcnow()
    held_for = max(0.0, (now - rfq.held_at).total_seconds())
    for task in steps_for(rfq).values():
        if task.status in TaskInstanceStatus.OPEN and task.held_at:
            task.paused_seconds = int(
                (task.paused_seconds or 0)
                + max(0.0, (now - task.held_at).total_seconds()))
            task.held_at = None
            # The due date moves by the held time, or every step
            # comes back already overdue for a pause nobody caused.
            if task.due_at:
                task.due_at = task.due_at + timedelta(seconds=held_for)
    was = rfq.hold_reason
    rfq.held_at = rfq.held_by = rfq.hold_reason = None
    rfq.hold_remarks = None
    audit.record('rfq.reactivated', 'rfq', rfq.id,
                 old={'hold_reason': was},
                 new={'held_seconds': int(held_for),
                      'remarks': remarks},
                 actor=actor,
                 reason=f'reactivated after {held_for / 3600:.1f}h '
                        f'on hold; that time is not counted against '
                        f'anybody')
    db.session.flush()
    return True, {'held_seconds': int(held_for)}


def set_priority(rfq, priority, *, actor, remarks=''):
    """Change the priority, which moves every open step's due date."""
    from app import db
    from app.models.task_engine import TaskInstanceStatus
    from app.services import audit

    if not may_hold(actor, rfq):
        return False, ('only the vertical head or a director can '
                       'change the priority')
    if priority not in PRIORITY_DAYS:
        return False, f'priority must be one of {list(PRIORITY_DAYS)}'
    was = rfq.priority
    if was == priority:
        return False, f'already {priority}'

    rfq.priority = priority
    for task in steps_for(rfq).values():
        if task.status not in TaskInstanceStatus.OPEN:
            continue
        hours = standard_hours(task.step_no, priority)
        if hours and task.started_at:
            task.due_at = task.started_at + timedelta(
                hours=hours,
                seconds=int(task.paused_seconds or 0))
    audit.record('rfq.priority_changed', 'rfq', rfq.id,
                 old={'priority': was}, new={'priority': priority,
                                             'remarks': remarks},
                 actor=actor,
                 reason=f'priority {was or "unset"} -> {priority}')
    db.session.flush()
    return True, {'priority': priority}


def board(rfq):
    """The twelve boxes, for the journey strip and the board."""
    from app.models.task_engine import TaskInstanceStatus

    live = steps_for(rfq)
    out = []
    for step in applicable(rfq):
        task = live.get(step['no'])
        if task is None:
            state, colour, words = 'future', GREY, ''
        elif task.status in TaskInstanceStatus.OPEN:
            state = 'current'
            colour, words = verdict(task, priority=rfq.priority)
        else:
            state, colour = 'done', GREEN
            words = task.result or 'done'
        out.append({
            'step_no': step['no'], 'title': step['title'],
            'state': state, 'colour': colour, 'words': words,
            'owner': getattr(task, 'owner_user_id', '') or '',
            'result': getattr(task, 'result', '') or '',
            'closed_at': str(getattr(task, 'completed_at', '') or '')[:16],
            'confirmable': step['no'] in CONFIRMABLE,
        })
    return out


# ── Master Data (§8) ─────────────────────────────────────────────────
DELAY_REASONS = (
    'Awaiting vendor rate', 'Shipping line response pending',
    'Insufficient RFQ information', 'Technical clarification pending',
    'Route survey required', 'Customer clarification pending',
    'Internal approval pending', 'Workload / resource constraint',
    'System issue', 'Others',
)
GOOD_PRACTICE_REASONS = (
    'Existing vendor relationship', 'Historical rate / database used',
    'Complete customer information', 'Early vendor engagement',
    'Standard solution available',
    'Quick interdepartmental coordination', 'Others',
)
HOLD_REASONS = (
    'Requirement closed by customer', 'Cancelled by customer',
    'Postponed by customer', 'No longer commercially relevant',
    'Others',
)
CONDITION_TYPES = (
    'Condition', 'Assumption', 'Limitation', 'Exclusion',
    'Special requirement', 'Customer-facing term',
)
SCREENING_OUTCOMES = (
    'Complete — nothing missing',
    'Missing: drawings / dimensions',
    'Missing: site address / route',
    'Missing: scope of work',
    'Missing: required date',
)
COMPLETE_OUTCOME = SCREENING_OUTCOMES[0]

LISTS = {
    'rfq_delay_reason': DELAY_REASONS,
    'rfq_good_practice_reason': GOOD_PRACTICE_REASONS,
    'rfq_hold_reason': HOLD_REASONS,
    'rfq_condition_type': CONDITION_TYPES,
    'rfq_screening_outcome': SCREENING_OUTCOMES,
}


def seed_lists(*, actor='system'):
    """Put the §8 vocabularies in Master Data. Caller commits."""
    from app import db
    from app.models.master_data import MasterItem

    added, present = [], 0
    for list_key, values in LISTS.items():
        for order, code in enumerate(values):
            if MasterItem.query.filter_by(list_key=list_key,
                                          code=code).first():
                present += 1
                continue
            db.session.add(MasterItem(list_key=list_key, code=code,
                                      label=code, sort_order=order,
                                      is_active=True))
            added.append(f'{list_key}:{code}')
    return added, present


def reasons(kind):
    """The live list for a dropdown, falling back to the seed."""
    key = {'delay': 'rfq_delay_reason',
           'good_practice': 'rfq_good_practice_reason',
           'hold': 'rfq_hold_reason',
           'condition_type': 'rfq_condition_type',
           'screening': 'rfq_screening_outcome'}.get(kind)
    if key is None:
        return []
    try:
        from app.intel import master_codes
        found = [c for c in master_codes(key) if c]
        if found:
            return found
    except Exception:                                     # noqa: BLE001
        pass
    return list(LISTS.get(key, ()))


# ── events that close a step from elsewhere ─────────────────────────
def on_quote_sent(quote, *, actor, evidence=None):
    """Step 8 closes when a quotation actually goes to the customer.

    Called from the CRM's own send path and from a matched quote
    mail. Returns (ok, detail) and never raises: a chain that
    cannot be updated must not stop a quotation going out.

    Idempotent. The same quotation sent twice — once through the
    CRM and once by the matcher seeing it in the mailbox — closes
    one step, not two.
    """
    from app import db

    if not enabled():
        return False, 'the chain is off'
    rfq = _rfq_of(quote)
    if rfq is None:
        return False, 'the quote is not against an RFQ'
    task = steps_for(rfq).get(8)
    if task is None:
        return False, 'step 8 is not open on this RFQ'
    from app.models.task_engine import TaskInstanceStatus
    if task.status not in TaskInstanceStatus.OPEN:
        return False, 'step 8 is already closed'

    facts = {'quote_id': quote.id,
             'quote_number': quote.quote_number or '',
             'display': _display(quote),
             'rev_no': quote.rev_no if quote.rev_no is not None
             else max(0, (quote.revision_number or 1) - 1),
             'amount': float(quote.total_amount or 0),
             'currency': quote.currency or '',
             'sent_by': actor,
             'sent_at': datetime.utcnow().isoformat(timespec='minutes')}
    facts.update(evidence or {})

    ok, detail = close_step(rfq, 8, actor=actor, event='quote_sent',
                            reason=(evidence or {}).get('reason', ''),
                            remarks=(evidence or {}).get('remarks', ''),
                            evidence=facts)
    if ok:
        db.session.flush()
    return ok, detail


def _rfq_of(quote):
    from app import db
    from app.models.rfq import RFQ

    if getattr(quote, 'rfq_id', None):
        return db.session.get(RFQ, quote.rfq_id)
    return None


def _display(quote):
    try:
        from app.services import quote_rev
        return quote_rev.display_number(quote)
    except Exception:                                     # noqa: BLE001
        return quote.quote_number or ''


def on_po_recorded(rfq, *, actor, po_number, amount=None,
                   awarded_on=None):
    """Step 11 closes when a PO or LOI is actually recorded.

    The customer's own clock runs on step 11, so it is never
    late — but it does close, and the PO number is the evidence
    that it did. Recording the number is the whole point: "Won"
    without a reference is a claim nobody can check against the
    customer's system later.
    """
    from app import db

    if not enabled():
        return False, 'the chain is off'
    if not (po_number or '').strip():
        return False, ('a PO or LOI reference is required — "Won" '
                       'with no reference cannot be checked against '
                       'the customer later')
    ok, detail = close_step(
        rfq, 11, actor=actor, event='po_recorded',
        evidence={'po_number': po_number.strip()[:60],
                  'amount': float(amount) if amount else None,
                  'awarded_on': str(awarded_on or '')[:10],
                  'recorded_by': actor})
    if ok:
        db.session.flush()
    return ok, detail


def on_handover_sent(rfq, *, actor, pack_ref=''):
    """Step 12 closes when the existing Won handover pack goes.

    Not a new handover. The pack that `ff_handover` / `job_handover`
    already builds is the event; this only records that the chain
    has ended, so the twelve boxes finish where the work does.
    """
    from app import db

    if not enabled():
        return False, 'the chain is off'
    ok, detail = close_step(
        rfq, 12, actor=actor, event='handover_sent',
        evidence={'pack_ref': (pack_ref or '')[:120],
                  'sent_by': actor})
    if ok:
        db.session.flush()
    return ok, detail


def journey(lead):
    """The 8 pre-RFQ boxes and the 12 chain boxes, for one lead.

    §6.4. One flowchart, so a person sees where the work is
    without reading two screens and joining them in their head.
    """
    from app import Company, db
    from app.services import lead_journey as lj

    account = (db.session.get(Company, lead.company_id)
               if lead.company_id else None)
    stage = getattr(account, 'dev_stage', None) or ''
    here = lj.stage_rank(stage)

    before = []
    from presales.models import ACCOUNT_DEV_STAGES
    for name in ACCOUNT_DEV_STAGES[:8]:
        rank = lj.stage_rank(name)
        before.append({
            'title': name,
            'state': ('done' if here > rank else
                      'current' if here == rank else 'future'),
        })

    rfq = _rfq_of_lead(lead)
    after = board(rfq) if rfq is not None else [
        {'step_no': s['no'], 'title': s['title'], 'state': 'future',
         'colour': GREY, 'words': '', 'owner': '', 'result': '',
         'closed_at': '', 'confirmable': s['no'] in CONFIRMABLE}
        for s in STEPS]
    return {'before': before, 'after': after,
            'dev_stage': stage,
            'rfq_id': getattr(rfq, 'id', None),
            'rfq_number': getattr(rfq, 'rfq_number', '') or '',
            'closed': (lead.stage or '') in ('Won', 'Lost')}


def _rfq_of_lead(lead):
    from app.models.rfq import RFQ
    return (RFQ.query.filter_by(lead_id=lead.id)
            .order_by(RFQ.id.desc()).first())


# ── §4.8: what is waiting on one person ──────────────────────────────
def waiting_on(emp_code, *, limit=60):
    """This person's open chain steps, soonest first.

    My Work already lists these — they are TaskInstance rows like
    any other — but it lists them as bare task titles with a due
    date. A chain step without its timer and its RFQ is a line
    somebody skims past: "RFQ Registration" says nothing about
    whether it is three hours late or due on Friday.

    So this adds what the row needs to be acted on: the colour and
    the words behind it, the RFQ, the customer, and where the
    Act button goes.
    """
    from app import Company, db
    from app.models.rfq import RFQ
    from app.models.task_engine import TaskInstance, TaskInstanceStatus

    if not enabled(emp_code) or not emp_code:
        return []

    rows = (TaskInstance.query
            .filter(TaskInstance.owner_user_id == emp_code,
                    TaskInstance.step_no.isnot(None),
                    TaskInstance.entity_type == 'RFQ',
                    TaskInstance.status.in_(TaskInstanceStatus.OPEN))
            .order_by(TaskInstance.due_at.asc().nullslast())
            .limit(limit).all())

    out = []
    for task in rows:
        rfq = db.session.get(RFQ, task.entity_id)
        if rfq is None:
            continue
        colour, words = verdict(task, priority=rfq.priority)
        account = (db.session.get(Company, rfq.account_id)
                   if rfq.account_id else None)
        step = BY_NO.get(task.step_no, {})
        out.append({
            'task_id': task.id, 'step_no': task.step_no,
            'title': step.get('title', task.task_key),
            'rfq_id': rfq.id, 'rfq_number': rfq.rfq_number or '',
            'lead_id': rfq.lead_id,
            'customer': getattr(account, 'name', '') or '',
            'subject': (rfq.subject or '')[:80],
            'priority': rfq.priority or '',
            'colour': colour, 'words': words,
            'held': bool(rfq.held_at),
            'due_at': str(task.due_at or '')[:16],
            # The Act button opens the lead record on this step,
            # which is where its own form lives. Falling back to
            # the RFQ when a step somehow has no lead keeps the
            # button from going nowhere.
            'act': (f'/leads/{rfq.lead_id}/journey?step={task.step_no}'
                    if rfq.lead_id else f'/rfqs/{rfq.id}'),
            'confirmable': task.step_no in CONFIRMABLE,
            'waiting_for_customer': bool(
                getattr(task, 'waiting_for_customer', False)),
        })
    # Red first, then orange, then by due date: the order somebody
    # would work them in.
    rank = {RED: 0, ORANGE: 1, GREEN: 2, BLUE: 3, PURPLE: 4, GREY: 5}
    out.sort(key=lambda r: (rank.get(r['colour'], 9),
                            r['due_at'] or '9999'))
    return out


def step_detail(rfq, step_no):
    """Everything a person wants when they click a box (§6.4).

    Who held it, when it closed, how long against the standard,
    the result and the reason. A box that opens and says only
    "done" is a box nobody clicks twice.
    """
    from app import Employee, db
    from app.models.task_engine import TaskInstanceStatus

    step = BY_NO.get(step_no)
    if step is None:
        return None
    task = steps_for(rfq).get(step_no)
    out = {'step_no': step_no, 'title': step['title'],
           'responsible': step.get('who', ''),
           'confirmable': step_no in CONFIRMABLE,
           'event': step.get('event', '')}
    if task is None:
        out.update({'state': 'future', 'owner': '',
                    'words': 'not reached yet'})
        return out

    standard = standard_hours(step_no, rfq.priority)
    spent = elapsed_hours(task)
    who = task.owner_user_id or task.completed_by_id or ''
    name = ''
    if who:
        emp = Employee.query.filter_by(emp_code=who).first()
        name = getattr(emp, 'name', '') or ''
    open_now = task.status in TaskInstanceStatus.OPEN
    colour, words = (verdict(task, priority=rfq.priority)
                     if open_now else (GREEN, task.result or 'done'))
    out.update({
        'state': 'current' if open_now else 'done',
        'owner': who, 'owner_name': name,
        'started_at': str(task.started_at or '')[:16],
        'closed_at': str(task.completed_at or '')[:16],
        'closed_by': task.completed_by_id or '',
        'hours': round(spent, 1),
        'standard': round(standard, 1) if standard else None,
        'held_hours': round(int(task.paused_seconds or 0) / 3600, 1),
        'result': task.result or '',
        'reason': (task.delay_reason or task.good_practice_reason
                   or ''),
        'remarks': task.reason_remarks or '',
        'evidence': task.closed_evidence or {},
        'colour': colour, 'words': words,
        'waiting_for_customer': bool(
            getattr(task, 'waiting_for_customer', False)),
    })
    return out


def next_action(rfq, *, emp_code):
    """The one thing to do now, and whether this person can do it.

    §6.4's panel. "Waiting on X" for a viewer who cannot act is
    more use than a disabled form: it says who to chase.
    """
    from app.models.task_engine import TaskInstanceStatus

    if rfq is None:
        return {'step_no': None, 'title': '', 'can_act': False,
                'words': 'no RFQ on this lead yet', 'owner': ''}
    live = [t for t in steps_for(rfq).values()
            if t.status in TaskInstanceStatus.OPEN]
    if not live:
        return {'step_no': None, 'title': '', 'can_act': False,
                'words': ('on hold' if rfq.held_at
                          else 'nothing open on this chain'),
                'owner': ''}
    task = min(live, key=lambda t: t.step_no)
    step = BY_NO.get(task.step_no, {})
    colour, words = verdict(task, priority=rfq.priority)
    owner = task.owner_user_id or ''
    return {
        'step_no': task.step_no, 'title': step.get('title', ''),
        'owner': owner,
        'can_act': bool(emp_code) and (not owner or owner == emp_code
                                       or may_hold(emp_code, rfq)),
        'confirmable': task.step_no in CONFIRMABLE,
        'event': step.get('event', ''),
        'colour': colour, 'words': words,
        'held': bool(rfq.held_at),
        'rfq_id': rfq.id, 'rfq_number': rfq.rfq_number or '',
    }


# ── §4.6: conditions proposed from the terms library ────────────────
#: A line that is a heading or a warning rather than a clause.
_NOT_A_CLAUSE = ('draft', 'terms and conditions', 'standard terms')


def proposed_conditions(rfq):
    """Tickable clauses from this vertical's current term set.

    §4.6 says the tick list comes from the terms library. The
    library stores one body of text per vertical, so the clauses
    are split out of it — numbered lines, which is how all six
    current sets are written.

    Every clause comes back with `ticked: False`. Pre-ticking the
    standard terms would put a page of conditions on a quotation
    nobody read, which is the thing a tick list is supposed to
    prevent.

    The vertical's terms are the proposal, never the record: what
    a person ticks is written to `rfq_conditions` with their name
    on it.
    """
    import re

    from app.models.quote_terms import QuoteTermSet

    vertical = _vertical_of(rfq)
    if not vertical:
        return [], ''
    row = (QuoteTermSet.query
           .filter_by(vertical=vertical, is_current=True)
           .order_by(QuoteTermSet.version.desc()).first())
    if row is None:
        return [], (f'No standard terms on file for {vertical} — '
                    f'type the conditions, or ask an admin to add '
                    f'a term set.')

    out, draft = [], False
    for raw in (row.body or '').splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if any(low.startswith(p) for p in _NOT_A_CLAUSE):
            draft = draft or low.startswith('draft')
            continue
        # "1. Rates are valid …" -> the clause without its number.
        text = re.sub(r'^\s*\(?\d{1,2}[.)]\s*', '', line).strip()
        if len(text) < 12:
            continue
        out.append({'text': text, 'ticked': False,
                    'type': 'Condition', 'visibility': 'customer',
                    'source': f'{vertical} terms v{row.version}'})

    note = ''
    if draft:
        note = (f'The {vertical} terms are still marked DRAFT — '
                f'read each line before ticking it.')
    return out, note
