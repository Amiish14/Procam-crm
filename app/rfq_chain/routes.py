"""
The RFQ chain: the board, one RFQ's steps, and the five actions.

    GET  /rfqs/chain                      the board
    GET  /api/chain/board                 where every RFQ is sitting
    GET  /api/chain/<rfq_id>              one RFQ's twelve boxes
    POST /api/chain/<rfq_id>/register     step 2
    POST /api/chain/<rfq_id>/screening    step 3
    POST /api/chain/<rfq_id>/confirm      steps 4 and 5
    POST /api/chain/<rfq_id>/hold         hold / reactivate (D5)
    POST /api/chain/<rfq_id>/priority     change the priority (D5)

CRM-RFQ-CHAIN-01 (R2). Every route 404s while FEATURE_RFQ_CHAIN
is off.

There is no route that closes step 6, 7, 8, 11 or 12. Those close
when the work happens — the cost sheet saves, the quote is
approved, the quote goes out, the PO is recorded, the pack is
sent — and giving them a button would let the chain record what
people say happened rather than what did.
"""
from __future__ import annotations

from functools import wraps

from flask import Blueprint, jsonify, render_template, request, session

from app.services import rfq_chain as rc

bp = Blueprint('rfq_chain', __name__)


def _gate(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not rc.enabled():
            return jsonify(ok=False, error='Not found'), 404
        if not session.get('emp_code'):
            return jsonify(ok=False, error='Not authenticated'), 401
        return f(*a, **kw)
    return wrap


def _rfq_or_404(rfq_id):
    from app.models.rfq import RFQ
    from app.access import records

    rfq = RFQ.query.get(rfq_id)
    if rfq is None:
        return None, (jsonify(ok=False, error='RFQ not found'), 404)
    try:
        if not records.may_view_rfq(rfq):
            return None, (jsonify(ok=False,
                                  error='RFQ not found'), 404)
    except Exception:                                     # noqa: BLE001
        pass
    return rfq, None


@bp.route('/rfqs/chain')
def chain_page():
    if not rc.enabled():
        return jsonify(ok=False, error='Not found'), 404
    if not session.get('emp_code'):
        from flask import redirect, url_for
        return redirect(url_for('login'))
    return render_template('rfq_chain.html')


# ── reading ──────────────────────────────────────────────────────────
@bp.route('/api/chain/board')
@_gate
def api_board():
    """Where every RFQ in view is sitting now."""
    from collections import Counter

    from app.models.rfq import RFQ
    from app.models.task_engine import TaskInstanceStatus

    rows, per_step = [], Counter()
    for rfq in RFQ.query.order_by(RFQ.id.desc()).limit(400).all():
        try:
            from app.access import records
            if not records.may_view_rfq(rfq):
                continue
        except Exception:                                 # noqa: BLE001
            pass
        live = [t for t in rc.steps_for(rfq).values()
                if t.status in TaskInstanceStatus.OPEN]
        here = min((t.step_no for t in live), default=None)
        task = next((t for t in live if t.step_no == here), None)
        colour, words = (rc.verdict(task, priority=rfq.priority)
                         if task is not None else (rc.GREY, 'not started'))
        if here:
            per_step[here] += 1
        rows.append({
            'rfq_id': rfq.id, 'rfq_number': rfq.rfq_number or '',
            'subject': (rfq.subject or '')[:90],
            'priority': rfq.priority or '',
            'held': bool(rfq.held_at),
            'step_no': here, 'colour': colour, 'words': words,
            'holder': getattr(task, 'owner_user_id', '') or '',
            'strip': rc.board(rfq),
        })
    bars = [{'step_no': s['no'], 'title': s['title'],
             'count': per_step.get(s['no'], 0)} for s in rc.STEPS]
    return jsonify(ok=True, rows=rows, bars=bars, total=len(rows))


@bp.route('/api/chain/<int:rfq_id>')
@_gate
def api_one(rfq_id):
    rfq, refusal = _rfq_or_404(rfq_id)
    if refusal:
        return refusal
    return jsonify(
        ok=True,
        rfq={'id': rfq.id, 'rfq_number': rfq.rfq_number or '',
             'priority': rfq.priority or '',
             'customer_deadline': str(rfq.customer_deadline or ''),
             'held': bool(rfq.held_at),
             'hold_reason': rfq.hold_reason or '',
             'registered_at': str(rfq.registered_at or '')[:16]},
        steps=rc.board(rfq),
        may_hold=rc.may_hold(session['emp_code'], rfq),
        lists={'delay': rc.reasons('delay'),
               'good_practice': rc.reasons('good_practice'),
               'hold': rc.reasons('hold'),
               'screening': rc.reasons('screening'),
               'priority': list(rc.PRIORITY_DAYS)},
        suggested_delay={str(s['no']): rc.suggest_reason(rfq, s['no'])
                         for s in rc.STEPS})


# ── step 2: registration ─────────────────────────────────────────────
@bp.route('/api/chain/<int:rfq_id>/register', methods=['POST'])
@_gate
def api_register(rfq_id):
    from datetime import datetime

    from app import db
    from app.services import audit

    rfq, refusal = _rfq_or_404(rfq_id)
    if refusal:
        return refusal
    d = request.get_json(silent=True) or {}

    deadline = _date(d.get('customer_deadline'))
    priority = (d.get('priority') or '').strip() \
        or rc.suggest_priority(deadline)
    if priority not in rc.PRIORITY_DAYS:
        return jsonify(ok=False,
                       error=f'priority must be one of '
                             f'{list(rc.PRIORITY_DAYS)}'), 400

    rfq.priority = priority
    rfq.customer_deadline = deadline
    rfq.needs_technical = bool(d.get('needs_technical'))
    rfq.needs_survey = bool(d.get('needs_survey'))
    rfq.registered_at = datetime.utcnow()
    rfq.registered_by = session['emp_code']

    warning = ''
    if deadline is not None:
        window = rc.priority_days(priority)
        from datetime import timedelta
        ends = (rfq.registered_at + timedelta(days=window)).date()
        if deadline < ends:
            warning = (f'The customer wants it by {deadline}, which '
                       f'is before the {priority} window ends on '
                       f'{ends}. Either the priority is wrong or '
                       f'somebody has to be told we will be late.')

    audit.record('rfq.registered', 'rfq', rfq.id,
                 new={'priority': priority,
                      'customer_deadline': str(deadline or ''),
                      'needs_technical': rfq.needs_technical,
                      'needs_survey': rfq.needs_survey},
                 actor=session['emp_code'],
                 reason='registered by the RFQ coordinator')

    ok, detail = rc.close_step(rfq, 2, actor=session['emp_code'],
                               event='registered',
                               reason=(d.get('reason') or ''),
                               remarks=(d.get('remarks') or ''))
    if not ok:
        # Registration still stands even when step 2 was not open —
        # an RFQ registered before the chain existed has no step to
        # close, and refusing would make it unregisterable.
        db.session.commit()
        return jsonify(ok=True, priority=priority, warning=warning,
                       note=detail)
    db.session.commit()
    return jsonify(ok=True, priority=priority, warning=warning,
                   result=detail.get('result'),
                   next_step=detail.get('next'))


def _date(raw):
    from datetime import datetime as _dt
    raw = (raw or '').strip()
    if not raw:
        return None
    for fmt in ('%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            return _dt.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


# ── step 3: screening ────────────────────────────────────────────────
@bp.route('/api/chain/<int:rfq_id>/screening', methods=['POST'])
@_gate
def api_screening(rfq_id):
    from app import db
    from app.services import audit

    rfq, refusal = _rfq_or_404(rfq_id)
    if refusal:
        return refusal
    d = request.get_json(silent=True) or {}
    outcome = (d.get('outcome') or '').strip()
    if outcome not in rc.reasons('screening'):
        return jsonify(ok=False, error='Choose a screening '
                                       'outcome'), 400

    task = rc.steps_for(rfq).get(3)
    if task is None:
        return jsonify(ok=False, error='Screening is not open'), 400

    if outcome != rc.COMPLETE_OUTCOME:
        # Missing something: the step stays open and the customer is
        # asked. Closing here would record a question as an answer.
        task.waiting_for_customer = True
        audit.record('rfq.screening_incomplete', 'rfq', rfq.id,
                     new={'outcome': outcome},
                     actor=session['emp_code'],
                     reason=f'{outcome} — the customer has been '
                            f'asked; the step stays open')
        db.session.commit()
        return jsonify(ok=True, closed=False, outcome=outcome,
                       note='The step stays open until the customer '
                            'answers.')

    task.waiting_for_customer = False
    ok, detail = rc.close_step(rfq, 3, actor=session['emp_code'],
                               event='confirm',
                               reason=(d.get('reason') or ''),
                               remarks=(d.get('remarks') or ''))
    if not ok:
        db.session.rollback()
        return jsonify(ok=False, error=detail,
                       needs='reason'), 400
    db.session.commit()
    return jsonify(ok=True, closed=True, result=detail['result'],
                   next_step=detail['next'])


# ── steps 4 and 5: confirm, with conditions ──────────────────────────
@bp.route('/api/chain/<int:rfq_id>/confirm', methods=['POST'])
@_gate
def api_confirm(rfq_id):
    from app import db
    from app.models.rfq import RfqCondition

    rfq, refusal = _rfq_or_404(rfq_id)
    if refusal:
        return refusal
    d = request.get_json(silent=True) or {}
    try:
        step_no = int(d.get('step_no') or 0)
    except (TypeError, ValueError):
        step_no = 0
    if step_no not in rc.CONFIRMABLE:
        return jsonify(ok=False,
                       error=f'only steps {list(rc.CONFIRMABLE)} are '
                             f'closed by confirming; the rest close '
                             f'when the work happens'), 400

    for cond in (d.get('conditions') or []):
        text = (cond.get('text') or '').strip()
        if not text:
            continue
        db.session.add(RfqCondition(
            rfq_id=rfq.id, step_no=step_no,
            type=(cond.get('type') or '').strip() or None,
            text=text,
            visibility=(RfqCondition.CUSTOMER
                        if cond.get('visibility') == 'customer'
                        else RfqCondition.INTERNAL),
            created_by=session['emp_code']))

    ok, detail = rc.close_step(rfq, step_no,
                               actor=session['emp_code'],
                               event='confirm',
                               reason=(d.get('reason') or ''),
                               remarks=(d.get('remarks') or ''))
    if not ok:
        db.session.rollback()
        return jsonify(ok=False, error=detail, needs='reason'), 400
    db.session.commit()
    return jsonify(ok=True, result=detail['result'],
                   next_step=detail['next'])


# ── hold and priority (D5) ───────────────────────────────────────────
@bp.route('/api/chain/<int:rfq_id>/hold', methods=['POST'])
@_gate
def api_hold(rfq_id):
    from app import db

    rfq, refusal = _rfq_or_404(rfq_id)
    if refusal:
        return refusal
    d = request.get_json(silent=True) or {}
    if d.get('reactivate'):
        ok, detail = rc.reactivate(rfq, actor=session['emp_code'],
                                   remarks=(d.get('remarks') or ''))
    else:
        ok, detail = rc.hold(rfq, actor=session['emp_code'],
                             reason=(d.get('reason') or ''),
                             remarks=(d.get('remarks') or ''))
    if not ok:
        db.session.rollback()
        return jsonify(ok=False, error=detail), 400
    db.session.commit()
    return jsonify(ok=True, detail=detail, held=bool(rfq.held_at))


@bp.route('/api/chain/<int:rfq_id>/priority', methods=['POST'])
@_gate
def api_priority(rfq_id):
    from app import db

    rfq, refusal = _rfq_or_404(rfq_id)
    if refusal:
        return refusal
    d = request.get_json(silent=True) or {}
    ok, detail = rc.set_priority(rfq, (d.get('priority') or '').strip(),
                                 actor=session['emp_code'],
                                 remarks=(d.get('remarks') or ''))
    if not ok:
        db.session.rollback()
        return jsonify(ok=False, error=detail), 400
    _notify(rfq, detail['priority'], actor=session['emp_code'])
    db.session.commit()
    return jsonify(ok=True, priority=detail['priority'])


def _notify(rfq, priority, *, actor):
    """Tell everybody holding a step that the clock changed."""
    from app.models.task_engine import TaskInstanceStatus
    from app.services import outbox

    people = {t.owner_user_id for t in rc.steps_for(rfq).values()
              if t.status in TaskInstanceStatus.OPEN
              and t.owner_user_id}
    for code in sorted(people - {actor}):
        try:
            from app import Employee
            emp = Employee.query.filter_by(emp_code=code).first()
            to = (getattr(emp, 'email', '') or '').strip()
            if not to:
                continue
            outbox.enqueue(
                to=to,
                subject=f'{rfq.rfq_number}: priority is now {priority}',
                html=f'<p>{rfq.rfq_number} — '
                     f'{(rfq.subject or "")[:120]}</p>'
                     f'<p>The priority is now <b>{priority}</b>, so '
                     f'the time you have on your step has changed. '
                     f'Your due date has moved with it.</p>',
                dedupe_key=outbox.make_key('rfq.priority', rfq.id,
                                           priority, code),
                event_key='rfq.priority_changed', user_code=code,
                commit=False)
        except Exception:                                 # noqa: BLE001
            pass
