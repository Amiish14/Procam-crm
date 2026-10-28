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
    """Where every RFQ in view is sitting now.

    One row per RFQ in the shape `rfq_board.row` gives every other
    list, so the chain board and the cockpit cannot disagree about
    which step a lead is on.
    """
    from app import Lead, db
    from app.models.rfq import RFQ
    from app.services import rfq_board as rb

    rfqs = []
    for rfq in RFQ.query.order_by(RFQ.id.desc()).limit(400).all():
        try:
            from app.access import records
            if not records.may_view_rfq(rfq):
                continue
        except Exception:                                 # noqa: BLE001
            pass
        rfqs.append(rfq)

    leads = {}
    ids = [r.lead_id for r in rfqs if r.lead_id]
    if ids:
        for lead in Lead.query.filter(Lead.id.in_(ids)).all():
            leads[lead.id] = lead
    rb.names_for([r.lead_driver for r in rfqs]
                 + [lead.assigned_to for lead in leads.values()])

    rows = []
    for rfq in rfqs:
        lead = leads.get(rfq.lead_id)
        if lead is None:
            continue
        rows.append(rb.row(lead, rfq))
    rows.sort(key=_soonest)
    panel = rb.sitting(rows)
    return jsonify(ok=True, rows=rows, total=len(rows),
                   bars=panel['bars'], bottleneck=panel['bottleneck'],
                   steps=[{'step_no': s['no'], 'title': s['title']}
                          for s in rc.STEPS],
                   may_clear_mail=_may_clear_mail())


#: Overdue first, then due soonest; closed rows last. The board is
#: read top-down by somebody deciding what to do next.
_ORDER = {'rfq-t-red': 0, 'rfq-t-amb': 1, 'rfq-t-ok': 2,
          'rfq-t-info': 3, 'rfq-t-hold': 4}


def _soonest(row):
    return (_ORDER.get((row.get('now') or {}).get('tone'), 9),
            -(row.get('step_no') or 0))


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
        proposed=_proposed(rfq),
        lists={'delay': rc.reasons('delay'),
               'good_practice': rc.reasons('good_practice'),
               'hold': rc.reasons('hold'),
               'screening': rc.reasons('screening'),
               'condition_type': rc.reasons('condition_type'),
               'priority': list(rc.PRIORITY_DAYS)},
        suggested_delay={str(s['no']): rc.suggest_reason(rfq, s['no'])
                         for s in rc.STEPS})


def _proposed(rfq):
    """§4.6's tick list, with whatever the library could not give."""
    rows, note = rc.proposed_conditions(rfq)
    return {'rows': rows, 'note': note}


@bp.route('/api/chain/<int:rfq_id>/conditions')
@_gate
def api_conditions(rfq_id):
    """What steps 4-7 may tick, and what is already recorded."""
    from app.models.rfq import RfqCondition

    rfq, refusal = _rfq_or_404(rfq_id)
    if refusal:
        return refusal
    rows, note = rc.proposed_conditions(rfq)
    recorded = (RfqCondition.query.filter_by(rfq_id=rfq.id)
                .order_by(RfqCondition.step_no, RfqCondition.id)
                .all())
    # A clause already recorded is not proposed again.
    seen = {' '.join((c.text or '').lower().split())
            for c in recorded}
    rows = [r for r in rows
            if ' '.join(r['text'].lower().split()) not in seen]
    return jsonify(ok=True, proposed=rows, note=note,
                   recorded=[c.to_dict() for c in recorded],
                   types=rc.reasons('condition_type'))


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
                     reason=f'{outcome} — the customer is being '
                            f'asked; the step stays open')

        # §4.5 — actually ask them. Until now the step recorded
        # "waiting for customer" and nobody was chased.
        from app import Lead
        from app.services import rfq_clarify
        lead = (db.session.get(Lead, rfq.lead_id)
                if rfq.lead_id else None)
        status, detail = rfq_clarify.send(
            rfq, lead, outcome, actor=session['emp_code'],
            extra=(d.get('extra') or ''))
        db.session.commit()
        return jsonify(ok=True, closed=False, outcome=outcome,
                       mail=status, mail_detail=detail,
                       note=('The step stays open until the customer '
                             'answers.'
                             if status == 'queued' else
                             'The step stays open — and the mail was '
                             'NOT sent, see below.'))

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


# ── §5.4: the unmatched quote mail queue ─────────────────────────────
def _may_clear_mail(emp_code=None):
    """The RFQ Coordinator, a vertical head, or an admin."""
    from app import Employee
    from app.services import admin_settings

    code = emp_code or session.get('emp_code')
    emp = Employee.query.filter_by(emp_code=code).first()
    if emp is None:
        return False
    if (getattr(emp, 'role', '') or '').lower() in (
            'admin', 'super admin', 'superadmin', 'director'):
        return True
    if code == (admin_settings.get('rfq_coordinator') or ''):
        return True
    try:
        from app.models.email_admin import VerticalHead
        return VerticalHead.query.filter_by(
            head_emp_code=code, active=True).first() is not None
    except Exception:                                     # noqa: BLE001
        return False


@bp.route('/api/chain/unmatched')
@_gate
def api_unmatched():
    from app.models.quote import UnmatchedQuoteMail

    rows = (UnmatchedQuoteMail.query
            .filter(UnmatchedQuoteMail.status ==
                    UnmatchedQuoteMail.OPEN)
            .order_by(UnmatchedQuoteMail.created_at.desc())
            .limit(200).all())
    out = []
    for row in rows:
        item = row.to_dict()
        if row.suggested_lead_id:
            from app import Lead, db
            lead = db.session.get(Lead, row.suggested_lead_id)
            item['suggested'] = ({'id': lead.id,
                                  'company': lead.company or '',
                                  'stage': lead.stage or ''}
                                 if lead is not None else None)
        out.append(item)
    return jsonify(ok=True, rows=out, total=len(out),
                   may_clear=_may_clear_mail())


@bp.route('/api/chain/unmatched/<int:row_id>', methods=['POST'])
@_gate
def api_resolve_unmatched(row_id):
    """"Link to lead…" or "Not a quotation"."""
    from datetime import datetime

    from app import Lead, db
    from app.models.quote import UnmatchedQuoteMail
    from app.services import audit

    if not _may_clear_mail():
        return jsonify(ok=False,
                       error='Only the RFQ coordinator, a vertical '
                             'head or an admin can clear this '
                             'queue.'), 403

    row = db.session.get(UnmatchedQuoteMail, row_id)
    if row is None:
        return jsonify(ok=False, error='Not found'), 404
    if row.status != UnmatchedQuoteMail.OPEN:
        return jsonify(ok=False,
                       error=f'already {row.status}'), 400

    d = request.get_json(silent=True) or {}
    action = (d.get('action') or '').strip()

    if action == 'not_a_quote':
        row.status = UnmatchedQuoteMail.NOT_A_QUOTE
        row.resolved_by = session['emp_code']
        row.resolved_at = datetime.utcnow()
        audit.record('quote_mail.not_a_quotation', 'quote_mail',
                     row.id, new={'subject': row.subject},
                     actor=session['emp_code'],
                     reason='a person said this is not a quotation')
        db.session.commit()
        return jsonify(ok=True, status=row.status)

    if action != 'link':
        return jsonify(ok=False,
                       error='action must be link or '
                             'not_a_quote'), 400

    lead = db.session.get(Lead, d.get('lead_id') or 0)
    if lead is None:
        return jsonify(ok=False, error='Choose a lead'), 400

    row.status = UnmatchedQuoteMail.LINKED
    row.linked_lead_id = lead.id
    row.resolved_by = session['emp_code']
    row.resolved_at = datetime.utcnow()
    audit.record('quote_mail.linked', 'quote_mail', row.id,
                 new={'lead_id': lead.id, 'subject': row.subject},
                 actor=session['emp_code'],
                 reason=f'filed on lead {lead.id} by hand')
    db.session.commit()
    return jsonify(ok=True, status=row.status, lead_id=lead.id)
