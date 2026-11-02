"""
§5 — the three things the lead screen still asks a person for.

    GET  /api/leads/<id>/autofill          what the CRM filled, and
                                           what it would fill
    POST /api/leads/<id>/autofill/confirm  one field, or all of them
    POST /api/leads/<id>/call-log          §5.5 — one tap
    GET  /api/accounts/missing-details     §5.2 — the owner's list

§5.7 asks that the lead pop-up only ever ask for three things: confirm
the due date, confirm the follow-up, send the quote. Everything else
is filled and editable. These endpoints are the "filled and editable"
half — the screen reads what was auto-filled, shows it with "auto —
confirm" beside it, and a tap agrees with it.
"""
from __future__ import annotations

from datetime import datetime
from functools import wraps

from flask import Blueprint, jsonify, request, session

from app import db
from app.services import guided_help

bp = Blueprint('minimum_entry', __name__)


def _me():
    return session.get('emp_code')


def _auth(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not _me():
            body, status = guided_help.refusal('SESSION_EXPIRED',
                                               http=401)
            return jsonify(body), status
        return f(*a, **kw)
    return wrap


def _is_admin():
    from app import Employee

    emp = Employee.query.filter_by(emp_code=_me()).first()
    return (getattr(emp, 'role', '') or '').lower() in (
        'admin', 'super admin', 'superadmin', 'director', 'procam_admin')


def _lead_or_refusal(lead_id, *, editing=True):
    from app import Lead
    from app.access import scope as sc_mod

    lead = db.session.get(Lead, lead_id)
    if lead is None:
        body, status = guided_help.refusal('NOT_YOUR_LEAD',
                                           {'lead_id': lead_id},
                                           http=404)
        return None, (body, status)
    try:
        allowed = sc_mod.may_view(lead)
    except Exception:                                     # noqa: BLE001
        allowed = False
    if not allowed:
        body, status = guided_help.refusal('NOT_YOUR_LEAD',
                                           {'lead_id': lead_id},
                                           http=403)
        return None, (body, status)
    if editing:
        own = _me() in ((lead.assigned_to or ''),
                        (lead.secondary_owner or ''))
        if not own and not _is_admin():
            body, status = guided_help.refusal(
                'NOT_YOUR_LEAD', {'lead_id': lead.id}, http=403)
            return None, (body, status)
    return lead, None


@bp.route('/api/leads/<int:lead_id>/autofill')
@_auth
def api_autofill(lead_id):
    lead, refused = _lead_or_refusal(lead_id, editing=False)
    if refused:
        body, status = refused
        return jsonify(body), status
    from app.services import autofill as af

    return jsonify(ok=True, lead_id=lead.id,
                   auto=af.auto_fields(lead),
                   fields={f: getattr(lead, f, None) for f in af.FIELDS})


@bp.route('/api/leads/<int:lead_id>/autofill/confirm',
          methods=['POST'])
@_auth
def api_autofill_confirm(lead_id):
    """A person agrees with what the CRM read.

    Per field, because agreeing with the origin is not agreeing with
    the weight, and one button that confirms eight fields at once is a
    button people press to make the yellow go away.
    """
    lead, refused = _lead_or_refusal(lead_id)
    if refused:
        body, status = refused
        return jsonify(body), status
    data = request.get_json(silent=True) or {}
    from app.services import audit, autofill as af

    field = (data.get('field') or '').strip() or None
    if field and field not in af.FIELDS:
        return jsonify(ok=False,
                       error=f'{field} is not a field the CRM fills.',
                       fields=list(af.FIELDS)), 400
    left = af.confirm(lead, field)
    audit.record('lead.autofill_confirmed', 'lead', lead.id,
                 new={'confirmed': field or 'all', 'left': left},
                 actor=_me(),
                 reason='the owner agreed with what the CRM read',
                 commit=False)
    db.session.commit()
    return jsonify(ok=True, auto=left)


#: §5.5 — the outcomes, and what each one means for the lead. The
#: dropdown is the whole of the call log: a free-text box is how 915
#: leads came to have no next step recorded.
CALL_OUTCOMES = (
    ('interested_rfq', 'Interested – RFQ expected',
     'Business Discussion', 'Wait for the RFQ and chase it'),
    ('call_back', 'Call back', None, 'Call back'),
    ('not_now', 'Not now', None, 'Call again when they are ready'),
    ('wrong_contact', 'Wrong contact', None,
     'Find the right person in this company'),
    ('not_interested', 'Not interested', 'Lost', 'Close as Lost'),
)
CALL_BY_CODE = {code: (label, stage, next_action)
                for code, label, stage, next_action in CALL_OUTCOMES}


@bp.route('/api/leads/<int:lead_id>/call-log', methods=['POST'])
@_auth
def api_call_log(lead_id):
    """One tap: the outcome sets the call date, the next step and the
    follow-up, and moves the stage where the outcome implies one.

    Not Interested is the one that does NOT move the stage here. A
    lead is closed through the close-lost endpoint, which asks for the
    reason the reports are built on — closing it quietly from a call
    log would lose that, and CRM-NO-DELETE-01's whole point is that a
    lead's ending is recorded.
    """
    lead, refused = _lead_or_refusal(lead_id)
    if refused:
        body, status = refused
        return jsonify(body), status
    data = request.get_json(silent=True) or {}
    code = (data.get('outcome') or '').strip()
    if code not in CALL_BY_CODE:
        return jsonify(ok=False,
                       error='Pick what happened on the call.',
                       options=[{'code': c, 'label': l}
                                for c, l, _s, _n in CALL_OUTCOMES]), 400
    label, stage, next_action = CALL_BY_CODE[code]

    from app import LeadActivity
    from app.services import audit, followup, last_activity as la

    from app.services import quote_due as qd

    when = datetime.utcnow()
    # The moment is stored UTC like every other datetime here; the
    # *date* a person is held to is the IST one. `when.date()` stamped
    # a call made at 01:00 IST on Tuesday as Monday's.
    lead.phone_call_date = qd.today_ist()
    lead.next_action = next_action
    db.session.add(LeadActivity(
        lead_id=lead.id, kind='call',
        subject=label,
        body=(data.get('note') or '').strip() or None,
        occurred_at=when, performed_by=_me()))

    # §3 — the follow-up, proposed from the stage the lead will be on.
    proposed = None
    if (data.get('followup') or '').strip():
        try:
            lead.followup_date = datetime.strptime(
                str(data['followup'])[:10], '%Y-%m-%d').date()
        except ValueError:
            return jsonify(ok=False,
                           error='I could not read that follow-up '
                                 'date.', needs='followup'), 400
    else:
        proposed = followup.propose(lead, stage=stage or lead.stage)
        if proposed is not None:
            lead.followup_date = proposed

    moved = None
    if stage and stage not in ('Lost',):
        from app.services import cockpit02_gate as c2
        refusal = c2.refusal(lead, stage=stage, payload=data)
        if refusal:
            db.session.rollback()
            body, status = refusal
            return jsonify(body), status
        was, lead.stage = lead.stage, stage
        moved = {'from': was, 'to': stage}

    la.touch(lead, la.CALL, when=when)
    audit.record('lead.call_logged', 'lead', lead.id,
                 new={'outcome': code, 'stage': lead.stage,
                      'followup': str(lead.followup_date or '')},
                 actor=_me(), reason=label, commit=False)
    db.session.commit()
    return jsonify(ok=True, outcome=code, label=label, moved=moved,
                   followup=str(lead.followup_date or ''),
                   next_action=lead.next_action or '',
                   closes_as_lost=(stage == 'Lost'))


@bp.route('/api/accounts/missing-details')
@_auth
def api_accounts_missing():
    """§5.2 — accounts whose industry, state or city is blank.

    With what the CRM would put there, and where it read it: the GSTIN
    state code, the address, or the account's own leads where they all
    agree. The owner accepts or corrects; nothing here writes.
    """
    from app import Company
    from app.services import autofill as af

    rows = af.accounts_missing_details(limit=500)
    if (request.args.get('with_guesses') or '').lower() in ('1', 'true'):
        for row in rows[:100]:
            account = db.session.get(Company, row['id'])
            guesses = af.guess_account_details(account) if account else {}
            row['guesses'] = {f: {'value': v, 'how': how}
                              for f, (v, how) in guesses.items()}
    return jsonify(ok=True, count=len(rows), accounts=rows)
