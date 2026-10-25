"""
Account check: the queue of leads that do not belong to anybody yet.

    GET  /accounts/check                       the screen
    GET  /api/account-check/queue              leads waiting, with candidates
    GET  /api/account-check/candidates/<lead>  one lead's candidates
    POST /api/account-check/<lead>/confirm     link it, own it, learn it
    POST /api/account-check/<lead>/create      make the account, then link
    GET  /api/account-check/duplicate?name=    the live warning while typing

CRM-ACCOUNT-FIRST-01 §3.3. Every route 404s while
FEATURE_ACCOUNT_FIRST is off.

Who may clear it
    The vertical head for their own vertical, and admins for
    everything (D6). Everybody else may look — seeing why a lead is
    stuck is useful to the person waiting on it — but the write
    paths check again on the server. A screen that hides a button is
    a courtesy; the check that matters is here.

    Head-ness is read from the `vertical_head` table, not from a
    role, because that is where the business actually maintains it
    (D12, CRM-VERTICAL-MASTER-01). A head whose role was never
    updated is still the head.
"""
from __future__ import annotations

from functools import wraps

from flask import Blueprint, jsonify, render_template, request, session

bp = Blueprint('account_check', __name__)


def _on():
    from app.services import account_gate
    return account_gate.enabled()


def _gate(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not _on():
            return jsonify(ok=False, error='Not found'), 404
        if not session.get('emp_code'):
            return jsonify(ok=False, error='Not authenticated'), 401
        return f(*a, **kw)
    return wrap


def _is_admin(emp_code=None):
    from app import Employee
    emp = Employee.query.filter_by(
        emp_code=emp_code or session.get('emp_code')).first()
    role = (getattr(emp, 'role', '') or '').lower()
    return role in ('admin', 'super admin', 'superadmin', 'director')


def heads_verticals(emp_code=None):
    """Verticals this person heads, from the vertical_head table."""
    code = (emp_code or session.get('emp_code') or '').strip().upper()
    if not code:
        return set()
    try:
        from app.models.email_admin import VerticalHead
        rows = VerticalHead.query.filter_by(active=True).all()
        return {row.vertical for row in rows
                if code in ((row.head_emp_code or '').strip().upper(),
                            (row.deputy_emp_code or '').strip().upper())}
    except Exception:                                     # noqa: BLE001
        return set()


def may_clear(lead, emp_code=None):
    """May this person confirm the account on this lead? (D6)

    The lead's vertical is matched through the vertical master, so a
    lead filed under "PFM" is cleared by the head of Project Freight
    rather than by nobody.
    """
    if _is_admin(emp_code):
        return True
    from app.services import account_gate
    return account_gate.vertical_of(lead) in heads_verticals(emp_code)


# ── the queue ────────────────────────────────────────────────────────
def _row(lead, *, with_candidates=True):
    from app.services import account_gate, account_match

    why = account_gate.why_pending(lead)
    out = {
        'lead_id': lead.id,
        'company': lead.company or '',
        'sender': lead.email or lead.original_email_from or '',
        'subject': (lead.forwarded_subject
                    or lead.original_email_subject or ''),
        'vertical': account_gate.vertical_of(lead),
        'stage': lead.stage or '',
        'created_at': str(lead.created_at)[:16] if lead.created_at else '',
        'forwarded_by': lead.forwarded_by_name or '',
        'why': why,
        'may_clear': may_clear(lead),
        'candidates': [],
    }
    if not with_candidates:
        return out

    rows = account_match.candidates(
        out['sender'], company_name=lead.company,
        text=f"{out['subject']}\n{lead.original_email_body or ''}"[:4000])
    for row in rows:
        account = row['account']
        out['candidates'].append({
            'account_id': account.id,
            'name': account.name,
            'rule': row['rule'],
            'also': row['also'],
            'note': row['note'],
            'exact': row['exact'],
                'owner': account_gate.owner_for(
                account, account_gate.vertical_of(lead)),
            'blocked': _blocked(account),
        })
    # Say which of the three cases this is, so the person is not
    # left to work it out from the candidate list.
    if why == 'no account':
        if len(out['candidates']) > 1:
            out['why'] = 'several matches — likely duplicate'
        elif out['candidates'] and not out['candidates'][0]['exact']:
            out['why'] = 'name only'
        elif not out['candidates']:
            out['why'] = ('free mail — nothing to match on'
                          if _free(out['sender']) else 'no account')
    return out


def _free(sender):
    from app.services import account_match
    return account_match.is_free_mail(account_match.domain_of(sender))


def _blocked(account):
    """Is this account on the client block register?

    `check()` never raises and answers 'none' when the register
    cannot be read — a register that is briefly unreadable must not
    stop the company trading, and must not silently mark everything
    blocked either.
    """
    if account is None:
        return False
    try:
        from app.services import client_restrictions
        verdict = client_restrictions.check(
            company_name=account.name, gstin=account.gstin,
            account_id=account.id)
        return bool(verdict.blocked)
    except Exception:                                     # noqa: BLE001
        return False


@bp.route('/accounts/check')
def check_page():
    if not _on():
        return jsonify(ok=False, error='Not found'), 404
    if not session.get('emp_code'):
        from flask import redirect, url_for
        return redirect(url_for('login'))
    return render_template('account_check.html')


@bp.route('/api/account-check/queue')
@_gate
def api_queue():
    from app.services import account_gate

    leads = account_gate.pending_leads()
    mine_only = (request.args.get('mine') or '').lower() in ('1', 'true')
    rows = [_row(lead) for lead in leads]
    if mine_only:
        rows = [r for r in rows if r['may_clear']]
    rows.sort(key=lambda r: r['created_at'], reverse=True)
    return jsonify(ok=True, rows=rows, total=len(rows),
                   can_clear=sum(1 for r in rows if r['may_clear']))


@bp.route('/api/account-check/candidates/<int:lead_id>')
@_gate
def api_candidates(lead_id):
    from app import Lead, db
    lead = db.session.get(Lead, lead_id)
    if lead is None:
        return jsonify(ok=False, error='Lead not found'), 404
    return jsonify(ok=True, row=_row(lead))


# ── confirming ───────────────────────────────────────────────────────
@bp.route('/api/account-check/<int:lead_id>/confirm', methods=['POST'])
@_gate
def api_confirm(lead_id):
    """Link the lead to an account, name the owner, learn the sender."""
    from app import Company, Lead, db
    from app.services import account_gate, account_match, audit

    lead = db.session.get(Lead, lead_id)
    if lead is None:
        return jsonify(ok=False, error='Lead not found'), 404
    if not may_clear(lead):
        return jsonify(ok=False, error='Only the vertical head for '
                                       f'{account_gate.vertical_of(lead) or "this vertical"} '
                                       'or an admin can confirm an '
                                       'account.'), 403

    d = request.get_json(silent=True) or {}
    account = db.session.get(Company, d.get('account_id') or 0)
    if account is None:
        return jsonify(ok=False, error='Choose an account'), 400
    if _blocked(account):
        return jsonify(ok=False,
                       error=f'{account.name} is on the block '
                             f'register.'), 400

    owner = (d.get('owner') or '').strip()
    vertical = account_gate.vertical_of(lead)
    if not owner:
        owner = account_gate.owner_for(account, vertical) or _head_of(vertical)
    if not owner:
        return jsonify(ok=False,
                       error=f'Nobody owns {account.name} for '
                             f'{vertical or "this vertical"}, and there '
                             f'is no head to fall back on. Choose an '
                             f'owner.'), 400

    was = {'company_id': lead.company_id, 'assigned_to': lead.assigned_to}
    lead.company_id = account.id
    _ensure_vertical_owner(account, vertical, owner, actor=session['emp_code'])
    _assign(lead, owner)

    # Teach the matcher, so the next mail from this sender needs
    # nobody. This is the step that makes the queue shrink.
    sender = lead.email or lead.original_email_from or ''
    learned = False
    if sender and (d.get('learn') is not False):
        try:
            account_match.learn(sender, account.id, lead_id=lead.id,
                                actor=session['emp_code'])
            learned = True
        except Exception:                                 # noqa: BLE001
            pass

    # §3.4: record the account-level answer, so the next lead from
    # this customer in this vertical inherits it and asks nobody.
    try:
        from app.services import account_owner_confirm as _aoc
        _aoc.ask(account.id, vertical, owner, rule='account check',
                 lead_id=lead.id)
        _aoc.answer(account.id, vertical, outcome='confirmed',
                    by=session['emp_code'], commit=False)
    except Exception:                                     # noqa: BLE001
        pass

    audit.record('lead.account_confirmed', 'lead', lead.id,
                 old=was,
                 new={'company_id': account.id, 'account': account.name,
                      'assigned_to': owner, 'vertical': vertical},
                 actor=session['emp_code'],
                 reason='confirmed on Account check')
    db.session.commit()
    return jsonify(ok=True, account=account.name, owner=owner,
                   learned=learned,
                   remaining=len(account_gate.pending_leads()))


def _head_of(vertical):
    """The head of this vertical, or '' — head first, then deputy."""
    try:
        from app.services import vertical_heads
        found = vertical_heads.heads_for_vertical(vertical) or []
        return found[0] if found else ''
    except Exception:                                     # noqa: BLE001
        return ''


def _ensure_vertical_owner(account, vertical, owner, *, actor):
    """Write AccountVerticalOwner when the account has none."""
    from app import db
    from app.models.assignment import AccountVerticalOwner
    from app.services import audit

    if not vertical:
        return
    row = AccountVerticalOwner.query.filter_by(
        account_id=account.id, vertical=vertical).first()
    if row is not None:
        return
    db.session.add(AccountVerticalOwner(
        account_id=account.id, vertical=vertical, emp_code=owner,
        source='manual', note='set on Account check'))
    audit.record('account.vertical_owner_set', 'company', account.id,
                 new={'vertical': vertical, 'emp_code': owner},
                 actor=actor, reason='named on Account check')


def _assign(lead, owner):
    from app.services import lead_assignment
    try:
        lead_assignment.assign(lead, primary_code=owner, actor=None,
                               note='account confirmed on Account check',
                               _defer_commit=True)
    except Exception:                                     # noqa: BLE001
        lead.assigned_to = owner


# ── creating an account from the mail ────────────────────────────────
@bp.route('/api/account-check/<int:lead_id>/create', methods=['POST'])
@_gate
def api_create(lead_id):
    from app import Company, Lead, db
    from app.services import account_match, audit

    lead = db.session.get(Lead, lead_id)
    if lead is None:
        return jsonify(ok=False, error='Lead not found'), 404
    if not may_clear(lead):
        return jsonify(ok=False, error='Only the vertical head or an '
                                       'admin can create the account.'), 403

    d = request.get_json(silent=True) or {}
    name = (d.get('name') or '').strip()
    if len(name) < 2:
        return jsonify(ok=False, error='Name the account'), 400

    blocked = _blocked_lookalike(name)
    if blocked and not d.get('force'):
        return jsonify(ok=False,
                       error=f'"{name}" looks like {blocked}, which is '
                             f'on the block register.'), 400

    dupes = _duplicates(name)
    if dupes and not d.get('force'):
        return jsonify(ok=False, needs='confirm', duplicates=dupes,
                       error=f'{dupes[0]["name"]} already exists '
                             f'({dupes[0]["similarity"]}% alike). Link '
                             f'to it, or tick "create anyway".'), 409

    domain = (d.get('domain') or '').strip().lower()
    if domain and account_match.is_free_mail(domain):
        domain = ''                      # never a free-mail domain

    account = Company(
        name=name,
        gstin=(d.get('gstin') or '').strip().upper() or None,
        email_domains=[domain] if domain else [],
        vertical=account_gate.vertical_of(lead) or None,
        created_by=session['emp_code'],
        is_active=True)
    db.session.add(account)
    db.session.flush()

    audit.record('account.created', 'company', account.id,
                 new={'name': name, 'domain': domain,
                      'gstin': account.gstin, 'from_lead': lead.id},
                 actor=session['emp_code'],
                 reason='created on Account check from the mail')
    db.session.commit()
    return jsonify(ok=True, account_id=account.id, name=account.name)


@bp.route('/api/account-check/duplicate')
@_gate
def api_duplicate():
    """The live warning while somebody types a new account name."""
    name = (request.args.get('name') or '').strip()
    if len(name) < 3:
        return jsonify(ok=True, duplicates=[], blocked='')
    return jsonify(ok=True, duplicates=_duplicates(name),
                   blocked=_blocked_lookalike(name))


def _duplicates(name, *, floor=0.5):
    from app.services import account_match
    return [{'account_id': account.id, 'name': account.name,
             'similarity': int(ratio * 100)}
            for account, ratio in account_match._similar_names(
                name, floor=floor, limit=5)]


def _blocked_lookalike(name):
    """The name of a blocked client this looks like, or ''."""
    for row in _duplicates(name, floor=0.6):
        from app import Company, db
        account = db.session.get(Company, row['account_id'])
        if account is not None and _blocked(account):
            return account.name
    return ''
