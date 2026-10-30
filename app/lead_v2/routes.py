"""
CRM-LEAD-FLOW-01 §3, §4 — the lead screen and the two-step new lead.

    GET  /leads/<id>/panel          the screen
    GET  /api/leads/<id>/panel      what it draws, as JSON
    POST /api/leads/<id>/stage      move along the strip
    POST /api/leads/<id>/substatus  Contacted's detail
    POST /api/leads/<id>/seen       dismiss the "what has changed" note
    GET  /leads/new                 step 1 and step 2
    GET  /api/leads/new/accounts    the account search
    POST /api/leads/new             create it

Every route 404s while FEATURE_LEAD_POPUP_V2 is off, and nothing in
`templates/app.html` is touched, so a person without the flag gets
exactly the screen they had yesterday. That is the whole reason this
is a separate template rather than a rewrite in place.

Every refusal answers through the guided-help catalogue (§5), so the
screen never shows only "no".
"""
from __future__ import annotations

from functools import wraps

from flask import (Blueprint, jsonify, render_template, request,
                   session)

from app import db
from app.services import guided_help, lead_flow

bp = Blueprint('lead_v2', __name__)


def _me():
    return session.get('emp_code')


def _gate(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not _me():
            body, status = guided_help.refusal('SESSION_EXPIRED',
                                               http=401)
            return jsonify(body), status
        if not lead_flow.enabled():
            return jsonify(ok=False, error='Not found'), 404
        return f(*a, **kw)
    return wrap


def _is_admin():
    from app import Employee
    emp = Employee.query.filter_by(emp_code=_me()).first()
    role = (getattr(emp, 'role', '') or '').lower()
    return role in ('admin', 'super admin', 'superadmin', 'director')


def _lead_or_refusal(lead_id):
    """(lead, None) or (None, (body, status)) with a guide in it."""
    from app import Lead
    from app.access import scope as sc_mod

    lead = db.session.get(Lead, lead_id)
    if lead is None:
        body, status = guided_help.refusal(
            'NOT_YOUR_LEAD', {'lead_id': lead_id}, http=404)
        return None, (body, status)
    if not sc_mod.may_view(lead):
        body, status = guided_help.refusal(
            'NOT_YOUR_LEAD',
            {'lead_id': lead_id, 'owner': lead.assigned_to or '',
             'owner_name': lead.assigned_name or ''}, http=403)
        return None, (body, status)
    return lead, None


def _may_edit(lead):
    """§5's NOT_YOUR_LEAD: reading is wide, changing is not."""
    me = _me()
    if not me:
        return False
    if me in ((lead.assigned_to or ''), (lead.secondary_owner or '')):
        return True
    return _is_admin()


# ── the screen ───────────────────────────────────────────────────────
@bp.route('/leads/<int:lead_id>/panel')
@_gate
def panel_page(lead_id):
    lead, refused = _lead_or_refusal(lead_id)
    if refused:
        body, status = refused
        return render_template('lead_v2/refused.html',
                               guide=body['guide']), status
    data = lead_flow.panel(lead, is_admin=_is_admin())
    return render_template('lead_v2/panel.html', lead=lead, p=data,
                           may_edit=_may_edit(lead),
                           seen=bool(getattr(lead, 'popup_seen_by',
                                             False)))


@bp.route('/api/leads/<int:lead_id>/panel')
@_gate
def api_panel(lead_id):
    lead, refused = _lead_or_refusal(lead_id)
    if refused:
        body, status = refused
        return jsonify(body), status
    return jsonify(ok=True,
                   panel=lead_flow.panel(lead, is_admin=_is_admin()),
                   may_edit=_may_edit(lead))


@bp.route('/api/leads/<int:lead_id>/stage', methods=['POST'])
@_gate
def api_stage(lead_id):
    lead, refused = _lead_or_refusal(lead_id)
    if refused:
        body, status = refused
        return jsonify(body), status
    if not _may_edit(lead):
        body, status = guided_help.refusal(
            'NOT_YOUR_LEAD',
            {'lead_id': lead.id, 'owner': lead.assigned_to or '',
             'owner_name': lead.assigned_name or ''}, http=403)
        return jsonify(body), status

    data = request.get_json(silent=True) or {}
    to = (data.get('to') or '').strip()

    # On Hold is a management decision about the deal, not a way to
    # clear your own list (§5, HOLD_NOT_ALLOWED).
    if to == 'On Hold' and not _hold_allowed(lead):
        body, status = guided_help.refusal(
            'HOLD_NOT_ALLOWED',
            {'lead_id': lead.id, 'vertical': lead.procam_vertical or '',
             'head': _head_of(lead)}, http=403)
        return jsonify(body), status

    ok, detail = lead_flow.move(lead, to, actor=_me(),
                                sub_status=data.get('sub_status'),
                                note=(data.get('note') or '').strip())
    if not ok:
        if detail.get('missing'):
            body, status = guided_help.refusal(
                'STAGE_FIELDS_MISSING',
                {'lead_id': lead.id, 'to': to,
                 'missing': [m['field'] for m in detail['missing']],
                 'fields': detail['missing']})
            return jsonify(body), status
        db.session.rollback()
        return jsonify(ok=False, error=detail.get('error',
                                                  'Refused.')), 400
    return jsonify(ok=True, **detail)


def _hold_allowed(lead):
    from app.services import vertical_heads
    if _is_admin():
        return True
    try:
        heads = vertical_heads.heads_for_vertical(
            lead.procam_vertical or '') or []
    except Exception:                                     # noqa: BLE001
        heads = []
    return _me() in {getattr(h, 'emp_code', h) for h in heads}


def _head_of(lead):
    from app.services import vertical_heads
    try:
        heads = vertical_heads.heads_for_vertical(
            lead.procam_vertical or '') or []
    except Exception:                                     # noqa: BLE001
        return ''
    for head in heads:
        return getattr(head, 'emp_code', head) or ''
    return ''


@bp.route('/api/leads/<int:lead_id>/substatus', methods=['POST'])
@_gate
def api_sub_status(lead_id):
    lead, refused = _lead_or_refusal(lead_id)
    if refused:
        body, status = refused
        return jsonify(body), status
    if not _may_edit(lead):
        body, status = guided_help.refusal('NOT_YOUR_LEAD',
                                           {'lead_id': lead.id},
                                           http=403)
        return jsonify(body), status
    value = ((request.get_json(silent=True) or {}).get('sub_status')
             or '').strip()
    if value and value not in lead_flow.SUB_STATUSES:
        return jsonify(ok=False,
                       error=f'{value} is not one of the Contacted '
                             f'sub-statuses.'), 400
    lead.sub_status = value or None
    db.session.commit()
    return jsonify(ok=True, sub_status=lead.sub_status or '')


@bp.route('/api/leads/<int:lead_id>/seen', methods=['POST'])
@_gate
def api_seen(lead_id):
    """The "what has changed" note, dismissed once per person.

    In the database rather than in the browser: a person uses two
    machines and would be told twice.
    """
    lead, refused = _lead_or_refusal(lead_id)
    if refused:
        body, status = refused
        return jsonify(body), status
    lead.popup_seen_by = True
    db.session.commit()
    return jsonify(ok=True)


# ── §4 — making one, in two steps ────────────────────────────────────
@bp.route('/leads/new')
@_gate
def new_lead_page():
    from app.services import vertical_heads
    try:
        verticals = vertical_heads.acceptable_verticals(for_choice=True)
    except Exception:                                     # noqa: BLE001
        verticals = []
    return render_template('lead_v2/new_lead.html',
                           verticals=verticals)


@bp.route('/api/leads/new/accounts')
@_gate
def api_new_accounts():
    """Step 1's search. Three letters is enough.

    A blocked account is returned, marked and unpickable, rather
    than hidden: somebody searching for it needs to be told why
    they cannot have it, which is the whole point of §5.
    """
    from app import Company
    from app.services import account_match, client_restrictions

    query = (request.args.get('q') or '').strip()
    if len(query) < 3:
        return jsonify(ok=True, rows=[])
    like = f'%{query}%'
    rows = (Company.query
            .filter(Company.is_active.is_(True),
                    Company.name.ilike(like))
            .order_by(Company.name.asc()).limit(12).all())
    out = []
    for account in rows:
        verdict = client_restrictions.check(company_name=account.name,
                                            account_id=account.id)
        out.append({
            'id': account.id, 'name': account.name or '',
            'owner': (account.pic_emp_code or ''),
            'vertical': account.vertical or '',
            'blocked': bool(getattr(verdict, 'blocked', False)),
            'why': getattr(verdict, 'reason', '') or '',
            'not_a_customer': account_match.not_a_customer(account),
        })
    return jsonify(ok=True, rows=out)


@bp.route('/api/leads/new/open')
@_gate
def api_open_for_account():
    """Step 1's warning: an open lead for this customer already."""
    from app import Lead
    from app.services import lead_merge, sales_rules

    account_id = request.args.get('account_id', type=int)
    if not account_id:
        return jsonify(ok=True, rows=[])
    from datetime import datetime, timedelta
    since = datetime.utcnow() - timedelta(days=lead_merge.WINDOW_DAYS)
    rows = (Lead.query
            .filter(Lead.company_id == account_id,
                    Lead.is_archived.isnot(True),
                    Lead.junk_deleted.isnot(True),
                    Lead.created_at >= since,
                    Lead.stage.in_(sales_rules.open_stages()))
            .order_by(Lead.id.desc()).limit(5).all())
    return jsonify(ok=True, rows=[
        {'lead_id': r.id, 'stage': r.stage or '',
         'owner': r.assigned_to or '',
         'created': str(r.created_at)[:10],
         'what': (r.project or r.original_email_subject or '')[:80]}
        for r in rows])


@bp.route('/api/leads/new', methods=['POST'])
@_gate
def api_create():
    from app import Company, Lead
    from app.services import (auto_assign, client_restrictions,
                              lead_assignment)

    data = request.get_json(silent=True) or {}
    account_id = data.get('account_id')
    account = (db.session.get(Company, int(account_id))
               if account_id else None)
    if account is None:
        body, status = guided_help.refusal('NO_ACCOUNT')
        return jsonify(body), status

    verdict = client_restrictions.check(company_name=account.name,
                                        account_id=account.id)
    if getattr(verdict, 'blocked', False):
        body, status = guided_help.refusal(
            'CLIENT_BLOCKED',
            {'account': account.name, 'account_id': account.id,
             'why': getattr(verdict, 'reason', '') or ''}, http=403)
        return jsonify(body), status

    vertical = (data.get('vertical') or account.vertical or '').strip()
    requirement = (data.get('requirement') or '').strip()
    if not requirement:
        return jsonify(ok=False,
                       error='Say in a line what the client wants. '
                             'It is what the next person reads '
                             'first.'), 400

    lead = Lead(company=account.name, company_id=account.id,
                stage='New', source='manual',
                procam_vertical=vertical or None,
                project=requirement[:300],
                pic=(data.get('contact_name') or '').strip() or None,
                email=(data.get('contact_email') or '').strip() or None,
                phone=(data.get('contact_phone') or '').strip() or None)
    if data.get('target_date'):
        from datetime import datetime as _dt
        try:
            lead.followup_date = _dt.strptime(
                data['target_date'], '%Y-%m-%d').date()
        except Exception:                                 # noqa: BLE001
            pass
    if data.get('value'):
        try:
            lead.opportunity_value_num = float(data['value'])
            lead.value_basis = 'Entered by hand at creation'
        except Exception:                                 # noqa: BLE001
            pass
    db.session.add(lead)
    db.session.flush()

    owner = (data.get('owner') or '').strip()
    if not owner:
        code, _rule, _confirm = auto_assign.decide(
            account=account, vertical=vertical)
        owner = code or ''
    if owner:
        ok, err = lead_assignment.assign(
            lead, primary_code=owner, actor=_me(),
            note=(data.get('owner_reason') or '').strip() or None)
        if not ok:
            db.session.rollback()
            return jsonify(ok=False, error=err), 400
    db.session.commit()
    return jsonify(ok=True, lead_id=lead.id,
                   owner=lead.assigned_to or '',
                   stage=lead.stage)
