"""
v2026-08 — Pre-Sales API · Phase 1.

Endpoints:

    GET  /api/accounts                   list accounts (role-filtered)
    POST /api/accounts                   create
    GET  /api/accounts/<id>              detail (with summary + timeline)
    PUT  /api/accounts/<id>              update basic fields
    POST /api/accounts/<id>/assign       PIC change (append to history)
    POST /api/accounts/<id>/stage        dev-stage change (append to history)
    POST /api/accounts/<id>/activities   log an activity
    GET  /api/accounts/<id>/activities   list activities newest-first
    GET  /api/accounts/<id>/history      combined PIC + stage history
    GET  /api/accounts/vocab             enums the UI needs

All endpoints require login. Row-level filtering respects the calling
user's role (admin/vertical head sees more; user sees their own).
"""
from datetime import datetime, date
from flask import request, jsonify, session
from sqlalchemy import or_

from app import (app, db, Company, Contact, Employee, Lead, Opportunity)
from presales import bp


# ─── Auth helpers (inline; the CRM does session-based checks directly). ─
def _require_login() -> bool:
    return bool(session.get('emp_code'))


def _current_emp():
    code = session.get('emp_code')
    return Employee.query.filter_by(emp_code=code).first() if code else None

from presales.models import (
    AccountRelationshipTag, AccountAssignmentHistory,
    AccountStageHistory, AccountActivity,
    ACCOUNT_TYPES, ACCOUNT_DEV_STAGES, ACTIVITY_KINDS,
)
from presales import services as svc


# ─── access helper ─────────────────────────────────────────────────────
def _visible_account_ids(emp: Employee):
    """Return None (= all visible) for admin/vertical head; a set of ids
    otherwise. Vertical heads see accounts assigned to any emp_code in
    their reporting chain."""
    # The Access Matrix decides, as everywhere else: company-wide scope
    # sees all, vertical and own scope see the accounts their scope reaches.
    # (This used to test the role name and the reporting chain, so a
    # narrowed administrator still saw every account here.)
    from app.access import scope as _scope
    sc = _scope.for_employee(emp.emp_code)
    if sc.codes is None:
        return None
    return {c.id for c in _scope.companies(sc=sc).with_entities(Company.id)}


# ─── list + create ─────────────────────────────────────────────────────
@bp.route('/api/accounts', methods=['GET'])
def api_accounts_list():
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    q = Company.query.filter(Company.is_active.is_(True))
    ids = _visible_account_ids(emp)
    if ids is not None:
        if not ids:
            return jsonify(ok=True, accounts=[])
        q = q.filter(Company.id.in_(ids))
    # Text filter
    s = (request.args.get('q') or '').strip()
    if s:
        q = q.filter(Company.name.ilike(f'%{s}%'))
    stage = (request.args.get('stage') or '').strip()
    if stage:
        q = q.filter(Company.dev_stage == stage)
    pic = (request.args.get('pic') or '').strip()
    if pic:
        q = q.filter(Company.pic_emp_code == pic)
    rows = q.order_by(Company.name.asc()).limit(500).all()
    out = []
    for c in rows:
        d = c.to_dict()
        d.update({
            'dev_stage':     getattr(c, 'dev_stage', None),
            'pic':           getattr(c, 'pic_emp_code', None),
            'secondary_pic': getattr(c, 'secondary_pic_emp_code', None),
            'strategic':     bool(getattr(c, 'strategic_flag', False)),
            'priority':      getattr(c, 'priority', None),
            'last_activity_at': (str(c.last_activity_at)[:16]
                                 if getattr(c, 'last_activity_at', None) else None),
            'next_action_at':   (str(c.next_action_at)
                                 if getattr(c, 'next_action_at', None) else None),
            'tags': [t.tag for t in AccountRelationshipTag.query
                                     .filter_by(account_id=c.id).all()],
        })
        out.append(d)
    return jsonify(ok=True, accounts=out)


@bp.route('/api/accounts', methods=['POST'])
def api_accounts_create():
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    body = request.get_json(silent=True) or {}
    name = (body.get('name') or '').strip()
    if not name:
        return jsonify(ok=False, error='name required'), 400
    # dedup by name (case-insensitive)
    existing = Company.query.filter(db.func.lower(Company.name) == name.lower()).first()
    if existing:
        return jsonify(ok=False, error='account exists', id=existing.id), 409
    c = Company(
        name          = name,
        industry      = (body.get('industry') or '').strip() or None,
        website       = (body.get('website') or '').strip() or None,
        country       = (body.get('country') or 'India').strip() or None,
        state         = (body.get('state') or '').strip() or None,
        city          = (body.get('city') or '').strip() or None,
        phone         = (body.get('phone') or '').strip() or None,
        email         = (body.get('email') or '').strip() or None,
        notes         = (body.get('notes') or '').strip() or None,
        is_active     = True,
        created_by    = emp.emp_code,
    )
    # New extensions
    c.dev_stage       = (body.get('dev_stage') or 'Target Identified')
    c.pic_emp_code    = (body.get('pic') or emp.emp_code)
    c.strategic_flag  = bool(body.get('strategic'))
    c.priority        = (body.get('priority') or 'Medium')
    db.session.add(c); db.session.flush()

    for tag in (body.get('tags') or []):
        tag = (tag or '').strip()
        if tag in ACCOUNT_TYPES:
            db.session.add(AccountRelationshipTag(account_id=c.id, tag=tag))

    # Initial assignment + stage rows
    db.session.add(AccountAssignmentHistory(
        account_id=c.id, previous_pic_code=None,
        new_pic_code=c.pic_emp_code, assigned_by=emp.emp_code,
        reason='Initial assignment on creation'))
    db.session.add(AccountStageHistory(
        account_id=c.id, from_stage=None, to_stage=c.dev_stage,
        changed_by=emp.emp_code, note='Initial stage on creation'))

    db.session.commit()
    return jsonify(ok=True, id=c.id), 201


# ─── detail + update ───────────────────────────────────────────────────
@bp.route('/api/accounts/<int:aid>', methods=['GET'])
def api_accounts_detail(aid):
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    c = Company.query.get_or_404(aid)
    ids = _visible_account_ids(emp)
    if ids is not None and c.id not in ids:
        return jsonify(ok=False, error='forbidden'), 403
    return jsonify(ok=True, account={**c.to_dict(),
                                     **svc.account_summary(c),
                                     'secondary_pic': c.secondary_pic_emp_code,
                                     'can_assign': _may_assign(),
                                     'tags': [t.tag for t in AccountRelationshipTag.query
                                                            .filter_by(account_id=c.id).all()]})


@bp.route('/api/accounts/<int:aid>', methods=['PUT'])
def api_accounts_update(aid):
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    c = Company.query.get_or_404(aid)
    ids = _visible_account_ids(emp)
    if ids is not None and c.id not in ids:
        return jsonify(ok=False, error='forbidden'), 403
    body = request.get_json(silent=True) or {}
    for f in ('industry', 'website', 'country', 'state', 'city',
              'phone', 'email', 'notes'):
        if f in body:
            setattr(c, f, (body.get(f) or '').strip() or None)
    if 'strategic' in body:
        c.strategic_flag = bool(body['strategic'])
    if 'priority' in body:
        c.priority = body['priority']
    # Tags — replace set
    if 'tags' in body:
        AccountRelationshipTag.query.filter_by(account_id=c.id).delete()
        for tag in (body.get('tags') or []):
            tag = (tag or '').strip()
            if tag in ACCOUNT_TYPES:
                db.session.add(AccountRelationshipTag(account_id=c.id, tag=tag))
    db.session.commit()
    return jsonify(ok=True)


# ─── assign / stage / activity ─────────────────────────────────────────
def _may_assign() -> bool:
    """Reassigning an account's PIC is an Access Matrix permission; seeing
    the account is not enough."""
    from app.access.service import can
    return can('accounts.assign')


_NO_ASSIGN = ('You do not have permission to reassign account PICs. '
              'Ask an administrator.')


@bp.route('/api/accounts/<int:aid>/assign', methods=['POST'])
def api_accounts_assign(aid):
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    if not _may_assign():
        return jsonify(ok=False, error=_NO_ASSIGN, need='accounts.assign'), 403
    emp = _current_emp()
    c = Company.query.get_or_404(aid)
    ids = _visible_account_ids(emp)
    if ids is not None and c.id not in ids:
        return jsonify(ok=False, error='forbidden'), 403
    body = request.get_json(silent=True) or {}
    try:
        row = svc.reassign_account(
            account=c, new_pic_code=body.get('new_pic'),
            changed_by_code=emp.emp_code, reason=body.get('reason'),
            new_secondary_code=(body.get('new_secondary')
                                if 'new_secondary' in body else svc.UNCHANGED))
        db.session.commit()
    except svc.PreSalesError as e:
        db.session.rollback()
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, changed=row is not None,
                   pic=c.pic_emp_code, secondary=c.secondary_pic_emp_code)


#: Accounts per bulk request. The page sends larger selections in batches
#: of this size so it can show progress.
BULK_ASSIGN_MAX = 100


@bp.route('/api/accounts/assign-bulk', methods=['POST'])
def api_accounts_assign_bulk():
    """Assign many accounts to one PIC (and optional secondary PIC).

    Each account goes through the same reassign_account() as the single
    endpoint, inside its own savepoint: one account that cannot be saved
    is reported and the others still are. The reason is required — it is
    written into every account's PIC history.
    """
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    if not _may_assign():
        return jsonify(ok=False, error=_NO_ASSIGN, need='accounts.assign'), 403
    emp = _current_emp()
    body = request.get_json(silent=True) or {}

    raw_ids = body.get('account_ids')
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify(ok=False, error='Choose at least one account'), 400
    try:
        account_ids = list(dict.fromkeys(int(i) for i in raw_ids))
    except (TypeError, ValueError):
        return jsonify(ok=False, error='account_ids must be numbers'), 400
    if len(account_ids) > BULK_ASSIGN_MAX:
        return jsonify(ok=False, error=f'At most {BULK_ASSIGN_MAX} accounts '
                       'per request'), 400
    reason = (body.get('reason') or '').strip()
    if not reason:
        return jsonify(ok=False, error='A reason is required; it is recorded '
                       "in each account's history"), 400
    new_pic = (body.get('new_pic') or '').strip()
    secondary = (body.get('new_secondary') if 'new_secondary' in body
                 else svc.UNCHANGED)
    # A bad PIC fails every account the same way: say so once.
    try:
        if not new_pic:
            raise svc.PreSalesError('Choose the PIC')
        svc._active_employee(new_pic, 'PIC')
        if secondary is not svc.UNCHANGED and (secondary or '').strip():
            svc._active_employee(secondary.strip(), 'Secondary PIC')
            if secondary.strip() == new_pic:
                raise svc.PreSalesError('The secondary PIC must be a '
                                        'different person from the PIC')
    except svc.PreSalesError as e:
        return jsonify(ok=False, error=str(e)), 400

    visible = _visible_account_ids(emp)
    found = {c.id: c for c in Company.query.filter(
        Company.id.in_(account_ids), Company.is_active.is_(True))}
    results = []
    for aid in account_ids:
        c = found.get(aid)
        if c is None:
            results.append({'id': aid, 'ok': False,
                            'error': 'account not found or inactive'})
            continue
        if visible is not None and aid not in visible:
            results.append({'id': aid, 'name': c.name, 'ok': False,
                            'error': 'outside your access'})
            continue
        try:
            with db.session.begin_nested():
                row = svc.reassign_account(
                    account=c, new_pic_code=new_pic,
                    changed_by_code=emp.emp_code, reason=reason,
                    new_secondary_code=secondary)
        except svc.PreSalesError as e:
            results.append({'id': aid, 'name': c.name, 'ok': False,
                            'error': str(e)})
            continue
        except Exception:
            app.logger.exception('bulk assign failed for account %s', aid)
            results.append({'id': aid, 'name': c.name, 'ok': False,
                            'error': 'could not be saved'})
            continue
        results.append({'id': aid, 'name': c.name, 'ok': True,
                        'changed': row is not None,
                        'pic': c.pic_emp_code,
                        'secondary': c.secondary_pic_emp_code})
    db.session.commit()
    ok_n = sum(1 for r in results if r['ok'])
    return jsonify(ok=True, results=results, succeeded=ok_n,
                   failed=len(results) - ok_n)


@bp.route('/api/accounts/<int:aid>/stage', methods=['POST'])
def api_accounts_stage(aid):
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    c = Company.query.get_or_404(aid)
    ids = _visible_account_ids(emp)
    if ids is not None and c.id not in ids:
        return jsonify(ok=False, error='forbidden'), 403
    body = request.get_json(silent=True) or {}
    _old_stage_acct = getattr(c, 'dev_stage', None)
    try:
        svc.change_account_stage(account=c,
                                 new_stage=body.get('new_stage'),
                                 changed_by_code=emp.emp_code,
                                 note=body.get('note'))
        db.session.commit()
    except svc.PreSalesError as e:
        return jsonify(ok=False, error=str(e)), 400
    # v2026-09-04 — Task engine hook (Phase 3).  Never crashes the request.
    try:
        from app.services.task_engine import on_state_change as _osc
        _osc(c, 'Company', _old_stage_acct, getattr(c, 'dev_stage', None),
             triggered_by=emp)
        db.session.commit()
    except Exception:
        try:
            db.session.rollback()
        except Exception:
            pass
    return jsonify(ok=True)


@bp.route('/api/accounts/<int:aid>/activities', methods=['POST'])
def api_accounts_activity_create(aid):
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    c = Company.query.get_or_404(aid)
    ids = _visible_account_ids(emp)
    if ids is not None and c.id not in ids:
        return jsonify(ok=False, error='forbidden'), 403
    body = request.get_json(silent=True) or {}
    nxt = body.get('next_action_at')
    try:
        nxt_date = datetime.strptime(nxt, '%Y-%m-%d').date() if nxt else None
    except ValueError:
        nxt_date = None
    row = svc.log_activity(
        account         = c,
        kind            = (body.get('kind') or 'Other').strip(),
        performed_by_code = emp.emp_code,
        subject         = body.get('subject'),
        body            = body.get('body'),
        contact_id      = body.get('contact_id') or None,
        next_action     = body.get('next_action'),
        next_action_at  = nxt_date,
    )
    db.session.commit()
    return jsonify(ok=True, id=row.id), 201


@bp.route('/api/accounts/<int:aid>/activities', methods=['GET'])
def api_accounts_activity_list(aid):
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    c = Company.query.get_or_404(aid)
    ids = _visible_account_ids(emp)
    if ids is not None and c.id not in ids:
        return jsonify(ok=False, error='forbidden'), 403
    rows = (AccountActivity.query
            .filter_by(account_id=c.id)
            .order_by(AccountActivity.occurred_at.desc())
            .limit(200).all())
    return jsonify(ok=True, activities=[r.to_dict() for r in rows])


@bp.route('/api/accounts/<int:aid>/history', methods=['GET'])
def api_accounts_history(aid):
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    emp = _current_emp()
    c = Company.query.get_or_404(aid)
    ids = _visible_account_ids(emp)
    if ids is not None and c.id not in ids:
        return jsonify(ok=False, error='forbidden'), 403
    assigns = AccountAssignmentHistory.query.filter_by(account_id=c.id)\
              .order_by(AccountAssignmentHistory.assigned_at.desc()).all()
    stages  = AccountStageHistory.query.filter_by(account_id=c.id)\
              .order_by(AccountStageHistory.changed_at.desc()).all()
    return jsonify(ok=True,
        pic_history=[{
            'previous_pic': a.previous_pic_code, 'new_pic': a.new_pic_code,
            'assigned_by': a.assigned_by, 'reason': a.reason or '',
            'at': str(a.assigned_at)[:16],
        } for a in assigns],
        stage_history=[{
            'from': s.from_stage, 'to': s.to_stage,
            'changed_by': s.changed_by, 'note': s.note or '',
            'at': str(s.changed_at)[:16],
        } for s in stages],
    )


# ─── vocab (drop-downs for the UI) ────────────────────────────────────
@bp.route('/api/accounts/vocab', methods=['GET'])
def api_accounts_vocab():
    if not _require_login():
        return jsonify(ok=False, error='login required'), 401
    return jsonify(ok=True,
        account_types    = list(ACCOUNT_TYPES),
        dev_stages       = list(ACCOUNT_DEV_STAGES),
        activity_kinds   = list(ACTIVITY_KINDS),
    )
