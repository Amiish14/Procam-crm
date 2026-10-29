"""
CRM-LEAD-ACCOUNT-01 — link or create the account without leaving the lead.

    GET  /api/leads/<id>/account              what the lead sheet draws
    GET  /api/leads/<id>/account/suggest?q=   the picker
    POST /api/leads/<id>/account/link         link an existing account
    POST /api/leads/<id>/account/create       create one, and link it
    GET  /api/leads/<id>/account/owner        who owns it, and may I change it
    POST /api/leads/<id>/account/owner        correct the owner (§2.5)
    POST /api/leads/<id>/account/owner/request   ask the head to
    GET  /api/leads/unplaced                  the clean-up report (§2.6)
    POST /api/leads/link-bulk                 link many at once
    GET  /admin/owner-corrections             the report
    GET  /api/admin/owner-corrections         the same, as JSON

Every route 404s with the flag off, so the lead sheet is exactly
what it was for anybody not in the pilot.
"""
from __future__ import annotations

from functools import wraps

from flask import Blueprint, jsonify, render_template, request, session

from app.services import lead_account as la

bp = Blueprint('lead_account', __name__)


def _me():
    return session.get('emp_code')


def _gate(f):
    """404 unless the flag is on for this person."""
    @wraps(f)
    def wrap(*a, **kw):
        if not _me():
            return jsonify(ok=False, error='Not authenticated'), 401
        if not la.enabled():
            return jsonify(ok=False, error='Not found'), 404
        return f(*a, **kw)
    return wrap


def _admin_only(f):
    @wraps(f)
    def wrap(*a, **kw):
        from app import Employee

        if not _me():
            from flask import redirect, url_for
            return redirect(url_for('login'))
        emp = Employee.query.filter_by(emp_code=_me()).first()
        role = (getattr(emp, 'role', '') or '').lower()
        if role not in ('admin', 'super admin', 'superadmin',
                        'director'):
            return jsonify(ok=False,
                           error='Admins and directors only.'), 403
        return f(*a, **kw)
    return wrap


def _lead(lead_id):
    """The lead, if this person may edit it. (lead, refusal)."""
    from app import Lead, db
    from app.access import scope

    lead = db.session.get(Lead, lead_id)
    if lead is None:
        return None, (jsonify(ok=False, error='Lead not found'), 404)
    if not scope.may_view(lead):
        return None, (jsonify(ok=False, error='Lead not found'), 404)
    return lead, None


@bp.route('/api/leads/account-available')
@_gate
def api_available():
    """Is the feature here for this person?

    The Leads list asks before it draws the select column, the way
    the junk probe does: a 404 and the page is what it always was.
    """
    return jsonify(ok=True, can_create=_may_create())


# ── what the lead sheet draws ────────────────────────────────────────
@bp.route('/api/leads/<int:lead_id>/account')
@_gate
def api_state(lead_id):
    lead, refusal = _lead(lead_id)
    if refusal:
        return refusal
    found = la.state(lead)
    found['can_create'] = _may_create()
    if found['linked']:
        from app import Company, db
        account = db.session.get(Company, found['account_id'])
        found['owner'] = _owner_block(account, lead)
    else:
        found['suggestions'] = la.suggest(lead, limit=3)
    return jsonify(ok=True, **found)


@bp.route('/api/leads/<int:lead_id>/account/suggest')
@_gate
def api_suggest(lead_id):
    lead, refusal = _lead(lead_id)
    if refusal:
        return refusal
    query = (request.args.get('q') or '').strip()
    rows = la.suggest(lead, query)
    return jsonify(ok=True, rows=rows, query=query,
                   can_create=_may_create())


def _may_create():
    """The same permission as "+ New Account" on the Accounts screen."""
    try:
        from app.access.service import can
        return bool(can('accounts.create') or can('accounts.assign'))
    except Exception:                                     # noqa: BLE001
        return False


# ── linking ──────────────────────────────────────────────────────────
@bp.route('/api/leads/<int:lead_id>/account/link', methods=['POST'])
@_gate
def api_link(lead_id):
    from app import Company, db

    lead, refusal = _lead(lead_id)
    if refusal:
        return refusal
    d = request.get_json(silent=True) or {}
    account = db.session.get(Company, d.get('account_id') or 0)
    ok, detail = la.link(lead, account, actor=_me(),
                         reason=(d.get('reason') or '').strip())
    if not ok:
        db.session.rollback()
        return jsonify(ok=False, error=detail), 400
    db.session.commit()
    return jsonify(ok=True, **detail)


@bp.route('/api/leads/<int:lead_id>/account/create', methods=['POST'])
@_gate
def api_create(lead_id):
    """Create the account from the lead sheet, and link it.

    The duplicate check is the one on Account check, so a name that
    is refused there is refused here.
    """
    from app import Company, db
    from app.services import account_match, audit

    lead, refusal = _lead(lead_id)
    if refusal:
        return refusal
    if not _may_create():
        return jsonify(ok=False,
                       error='Creating an account needs the same '
                             'permission as "+ New Account" on the '
                             'Accounts screen.'), 403

    d = request.get_json(silent=True) or {}
    name = (d.get('name') or '').strip()
    if len(name) < 2:
        return jsonify(ok=False, error='Name the account'), 400
    why = la.is_placeholder(name)
    if why:
        return jsonify(ok=False, error=why.capitalize()), 400

    from app.account_check.routes import (_blocked_lookalike,
                                          _duplicates)
    blocked = _blocked_lookalike(name)
    if blocked and not d.get('force'):
        return jsonify(ok=False,
                       error=f'"{name}" looks like {blocked}, which '
                             f'is on the block register.'), 400
    dupes = _duplicates(name)
    if dupes and not d.get('force'):
        return jsonify(ok=False, needs='confirm', duplicates=dupes,
                       error=f'{dupes[0]["name"]} already exists '
                             f'({dupes[0]["similarity"]}% alike). '
                             f'Use it instead, or create anyway.'), 409

    domain = (d.get('domain') or '').strip().lower()
    if domain.startswith('http'):
        domain = domain.split('//', 1)[-1].split('/')[0]
    if domain and account_match.is_free_mail(domain):
        domain = ''
    owner = (d.get('owner') or '').strip()
    vertical = ((d.get('vertical') or '').strip()
                or (lead.procam_vertical or ''))
    if not owner:
        owner = _default_owner(vertical)

    account = Company(
        name=name,
        gstin=(d.get('gstin') or '').strip().upper() or None,
        email_domains=[domain] if domain else [],
        vertical=vertical or None,
        created_by=_me(),
        is_active=True)
    for column, value in (('pic_emp_code', owner),
                          ('secondary_pic_emp_code',
                           (d.get('secondary') or '').strip() or None),
                          ('city', (d.get('city') or '').strip() or None),
                          ('state', (d.get('state') or '').strip() or None),
                          ('website', (d.get('website') or '').strip()
                           or None)):
        if hasattr(account, column) and value:
            setattr(account, column, value)
    # The short name is one of `aliases`, not a column of its own —
    # the vendor masking redacts every alias, which is why the list
    # exists at all.
    alias = (d.get('alias') or '').strip()
    if alias:
        account.aliases = [alias]
    db.session.add(account)
    db.session.flush()

    if owner and vertical:
        from app.models.assignment import AccountVerticalOwner
        db.session.add(AccountVerticalOwner(
            account_id=account.id, vertical=vertical, emp_code=owner,
            source='manual', note='created from the lead sheet'))

    audit.record('account.created', 'company', account.id,
                 new={'name': name, 'domain': domain,
                      'gstin': account.gstin, 'owner': owner,
                      'vertical': vertical, 'from_lead': lead.id},
                 actor=_me(),
                 reason='created from the lead sheet')

    ok, detail = la.link(lead, account, actor=_me(),
                         reason='created from the lead sheet')
    if not ok:
        db.session.rollback()
        return jsonify(ok=False, error=detail), 400
    db.session.commit()
    # `detail` already carries account_id and the owner question.
    return jsonify(ok=True, name=account.name, **detail)


def _default_owner(vertical):
    """The vertical head, as the auto-assign rules would choose."""
    try:
        from app.services import auto_assign
        code, _rule, _confirm = auto_assign.decide(vertical=vertical)
        return code or ''
    except Exception:                                     # noqa: BLE001
        return ''


# ── who owns the account (§2.5) ──────────────────────────────────────
def _owner_block(account, lead):
    from app.services import rfq_board

    owner = (getattr(account, 'pic_emp_code', '') or '')
    return {
        'emp_code': owner,
        'name': rfq_board.name_of(owner),
        'secondary': getattr(account, 'secondary_pic_emp_code', '') or '',
        'may_change': la.may_change_owner(account, _me()),
        'question': la.owner_question(lead, account),
    }


@bp.route('/api/leads/<int:lead_id>/account/owner')
@_gate
def api_owner(lead_id):
    from app import Company, db

    lead, refusal = _lead(lead_id)
    if refusal:
        return refusal
    account = (db.session.get(Company, lead.company_id)
               if lead.company_id else None)
    if account is None:
        return jsonify(ok=False, error='No account on this lead'), 400
    block = _owner_block(account, lead)
    block['other_leads'] = la.other_open_leads(
        account, block['emp_code'])
    return jsonify(ok=True, account=account.name,
                   account_id=account.id, **block)


@bp.route('/api/leads/<int:lead_id>/account/owner', methods=['POST'])
@_gate
def api_set_owner(lead_id):
    from app import Company, db

    lead, refusal = _lead(lead_id)
    if refusal:
        return refusal
    account = (db.session.get(Company, lead.company_id)
               if lead.company_id else None)
    if account is None:
        return jsonify(ok=False, error='No account on this lead'), 400
    if not la.may_change_owner(account, _me()):
        return jsonify(ok=False,
                       error='Only the account owner, the vertical '
                             'head or an admin can change this. Use '
                             '"Request owner change" instead.'), 403

    d = request.get_json(silent=True) or {}
    ok, detail = la.correct_owner(
        account,
        new_owner=(d.get('owner') or '').strip(),
        secondary=(d.get('secondary')
                   if 'secondary' in d else None),
        actor=_me(),
        reason=(d.get('reason') or '').strip(),
        move_lead_ids=d.get('move_lead_ids') or (),
        from_lead=lead)
    if not ok:
        db.session.rollback()
        return jsonify(ok=False, error=detail), 400
    db.session.commit()
    _tell_them(account, detail, lead)
    return jsonify(ok=True, **detail)


def _tell_them(account, detail, lead):
    """The old owner, the new owner and the vertical head, per the
    standing mail rule. Best effort, after the commit: the
    correction stands whether or not the mail goes."""
    try:
        from app.services import notification_rules as nrules
        nrules.dispatch(
            'account.pic_changed', account, actor=session.get('emp_code'),
            detail=(f'Owner corrected from lead #{lead.id}: '
                    f'{detail.get("was") or "nobody"} → '
                    f'{detail.get("owner")}. '
                    f'{len(detail.get("moved") or [])} open lead(s) '
                    f'moved with it.'))
    except Exception:                                     # noqa: BLE001
        from flask import current_app
        current_app.logger.exception(
            'could not announce the owner correction on account %s',
            getattr(account, 'id', '?'))


@bp.route('/api/leads/<int:lead_id>/account/owner/request',
          methods=['POST'])
@_gate
def api_request_owner(lead_id):
    """For everybody else: ask the vertical head, and log it."""
    from app import Company, db
    from app.services import audit, rfq_board

    lead, refusal = _lead(lead_id)
    if refusal:
        return refusal
    account = (db.session.get(Company, lead.company_id)
               if lead.company_id else None)
    if account is None:
        return jsonify(ok=False, error='No account on this lead'), 400

    d = request.get_json(silent=True) or {}
    wanted = (d.get('owner') or '').strip()
    reason = (d.get('reason') or '').strip()
    if not reason:
        return jsonify(ok=False,
                       error='Say why — the head reads this'), 400

    audit.record('account.owner_change_requested', 'company',
                 account.id,
                 new={'wanted': wanted, 'from_lead': lead.id,
                      'account': account.name},
                 actor=_me(), reason=reason)
    db.session.commit()
    try:
        from app.services import notification_rules as nrules
        nrules.dispatch(
            'account.pic_changed', account, actor=_me(),
            detail=(f'{rfq_board.name_of(_me())} asks for the owner '
                    f'of {account.name} to become '
                    f'{rfq_board.name_of(wanted) or "somebody else"} '
                    f'— from lead #{lead.id}. Reason: {reason}'))
    except Exception:                                     # noqa: BLE001
        pass
    return jsonify(ok=True,
                   sent_to=rfq_board.name_of(
                       _head_of(account)) or 'the vertical head')


def _head_of(account):
    try:
        from app.services import vertical_heads
        found = vertical_heads.heads_for_vertical(
            getattr(account, 'vertical', '') or '') or []
        return found[0] if found else ''
    except Exception:                                     # noqa: BLE001
        return ''


@bp.route('/api/leads/<int:lead_id>/account/owner-choice',
          methods=['POST'])
@_gate
def api_owner_choice(lead_id):
    """§2.4's one question, answered: keep the lead's PIC or switch.

    Recorded rather than acted on silently. The choice is the
    evidence for whether the auto-assign rule or the person was
    right, and a prompt nobody can score is a prompt nobody can
    improve.
    """
    from app import Company, db
    from app.services import audit, lead_assignment

    lead, refusal = _lead(lead_id)
    if refusal:
        return refusal
    d = request.get_json(silent=True) or {}
    chose = (d.get('chose') or '').strip()
    if not chose:
        return jsonify(ok=False, error='Nothing chosen'), 400
    account = (db.session.get(Company, lead.company_id)
               if lead.company_id else None)
    was = lead.assigned_to or ''
    moved = False
    if chose != was:
        ok, why = lead_assignment.assign(
            lead, primary_code=chose, actor=_me(),
            note=('Account owner kept on the lead — '
                  f'{getattr(account, "name", "the account")}'),
            _defer_commit=True)
        if not ok:
            db.session.rollback()
            return jsonify(ok=False, error=why), 400
        moved = True
    audit.record('lead.pic_vs_account_owner', 'lead', lead.id,
                 old={'assigned_to': was},
                 new={'assigned_to': chose, 'kept': bool(d.get('keep')),
                      'account_id': getattr(account, 'id', None)},
                 actor=_me(),
                 reason=('kept the lead PIC' if d.get('keep')
                         else 'switched the lead to the account owner'))
    db.session.commit()
    return jsonify(ok=True, assigned_to=chose, moved=moved)


# ── the clean-up (§2.6) ──────────────────────────────────────────────
@bp.route('/api/leads/unplaced')
@_gate
def api_unplaced():
    rows = la.unplaced()
    return jsonify(ok=True, rows=rows, total=len(rows))


@bp.route('/api/leads/link-bulk', methods=['POST'])
@_gate
def api_link_bulk():
    """Link several leads to one account from the Leads list.

    Each lead is re-checked and linked in its own savepoint: one
    that cannot be linked is reported and the rest still are.
    """
    from app import Company, Lead, db

    d = request.get_json(silent=True) or {}
    account = db.session.get(Company, d.get('account_id') or 0)
    if account is None:
        return jsonify(ok=False, error='Choose an account'), 400
    ids = [int(x) for x in (d.get('lead_ids') or [])
           if str(x).isdigit()]
    if not ids:
        return jsonify(ok=False, error='Choose the leads'), 400

    linked, skipped = [], []
    for lead_id in ids[:200]:
        lead, refusal = _lead(lead_id)
        if refusal:
            skipped.append({'lead_id': lead_id,
                            'why': 'not yours to edit'})
            continue
        try:
            with db.session.begin_nested():
                ok, detail = la.link(
                    lead, account, actor=_me(),
                    reason='linked in bulk from the Leads list')
                if not ok:
                    raise ValueError(detail)
            linked.append(lead_id)
        except Exception as exc:                          # noqa: BLE001
            skipped.append({'lead_id': lead_id, 'why': str(exc)})
    db.session.commit()
    return jsonify(ok=True, linked=linked, skipped=skipped,
                   account=account.name)


@bp.route('/admin/unplaced-leads')
@_admin_only
def unplaced_page():
    """§2.6's report: what a bulk clean-up would touch, before it
    touches anything."""
    if not la.enabled():
        return jsonify(ok=False, error='Not found'), 404
    rows = la.unplaced()
    counted = {}
    for row in rows:
        counted[row['why']] = counted.get(row['why'], 0) + 1
    return render_template('admin/unplaced_leads.html', rows=rows,
                           counted=sorted(counted.items(),
                                          key=lambda x: -x[1]))


# ── the report ───────────────────────────────────────────────────────
@bp.route('/admin/owner-corrections')
@_admin_only
def corrections_page():
    if not la.enabled():
        return jsonify(ok=False, error='Not found'), 404
    return render_template('admin/owner_corrections.html',
                           rows=la.corrections())


@bp.route('/api/admin/owner-corrections')
@_gate
@_admin_only
def corrections_api():
    rows = la.corrections()
    return jsonify(ok=True, rows=rows, total=len(rows))
