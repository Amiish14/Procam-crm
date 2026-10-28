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

# Module level: several handlers below ask it for a lead's vertical
# and its owner, and a per-function import in each is how one of
# them ends up without it. (It did: bulk-confirm raised NameError
# on its first real call.)
from app.services import account_gate                     # noqa: E402


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
    from app.services import account_gate, account_match, rfq_board

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
        'suggestion': _suggestion(lead),
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
            # A name beside the code: the person reading this row
            # is deciding whether to hand the lead to them, and
            # callers that match on the code keep working.
            'owner_name': rfq_board.name_of(account_gate.owner_for(
                account, account_gate.vertical_of(lead))),
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


def _suggestion(lead):
    """The recorded suggestion for this lead, if it still holds.

    Re-checked rather than trusted: an account created or an alias
    learnt since the pass ran makes a stored answer wrong, and a
    wrong answer behind a one-click button is worse than none.
    """
    from app.models.assignment import LeadAccountSuggestion
    from app.services import account_suggest

    row = LeadAccountSuggestion.query.filter_by(
        lead_id=lead.id).first()
    if row is None or row.acted_at is not None:
        return None
    if account_suggest.stale(row, lead):
        fresh = account_suggest.suggest_for(lead)
        account_suggest.store(fresh)
        from app import db
        db.session.commit()
        row = LeadAccountSuggestion.query.filter_by(
            lead_id=lead.id).first()
    if row is None or not row.account_id:
        return None
    from app import Company, db as _db
    account = _db.session.get(Company, row.account_id)
    if account is None:
        return None
    out = row.to_dict()
    out['name'] = account.name
    out['blocked'] = _blocked(account)
    out['trusted'] = (row.confidence or 0) >= account_suggest.TRUSTED
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


# ── acting on the suggestions ────────────────────────────────────────
@bp.route('/api/account-check/suggestions')
@_gate
def api_suggestions():
    """What could be confirmed in one go, for this person."""
    from app.models.assignment import LeadAccountSuggestion
    from app.services import account_suggest

    rows, blocked_out = [], 0
    for lead in account_suggest.pending():
        if not may_clear(lead):
            continue
        row = LeadAccountSuggestion.query.filter_by(
            lead_id=lead.id).first()
        if row is None or row.acted_at is not None \
                or not row.exact_single or not row.account_id:
            continue
        found = _suggestion(lead)
        if found is None:
            continue
        if found['blocked']:
            blocked_out += 1
            continue
        rows.append({'lead_id': lead.id,
                     'company': lead.company or '',
                     'vertical': account_gate.vertical_of(lead),
                     'account_id': found['account_id'],
                     'account': found['name'],
                     'rule': found['rule'],
                     'confidence': found['confidence']})
    return jsonify(ok=True, rows=rows, total=len(rows),
                   blocked=blocked_out)


@bp.route('/api/account-check/bulk-confirm', methods=['POST'])
@_gate
def api_bulk_confirm():
    """Confirm several suggestions at once.

    Only the shape `auto_link` would have linked unasked: exactly
    one exact-rule match, nothing disagreeing, above the confidence
    floor, not blocked. Every row is re-checked here even though
    the screen checked it — the screen's copy is seconds old and
    this writes to live customer records.

    A row that no longer qualifies is skipped and named, not
    silently dropped: 'confirmed 300' with 4 quietly missing is how
    somebody later finds a lead nobody owns.
    """
    from datetime import datetime

    from app import Company, Lead, db
    from app.services import account_suggest

    d = request.get_json(silent=True) or {}
    ids = [int(i) for i in (d.get('lead_ids') or [])
           if str(i).strip().isdigit()]
    if not ids:
        return jsonify(ok=False, error='Nothing selected'), 400
    if len(ids) > 500:
        return jsonify(ok=False,
                       error='500 at a time at most'), 400

    done, skipped = [], []
    for lead_id in ids:
        lead = db.session.get(Lead, lead_id)
        if lead is None:
            skipped.append({'lead_id': lead_id, 'why': 'no such lead'})
            continue
        if not may_clear(lead):
            skipped.append({'lead_id': lead_id,
                            'why': 'not yours to confirm'})
            continue
        if _blocked(db.session.get(Company, lead.company_id or 0)
                    if lead.company_id else None):
            skipped.append({'lead_id': lead_id,
                            'why': 'account is blocked'})
            continue
        # One implementation, shared with the offline pass. It
        # recomputes everything itself.
        ok, detail = account_suggest.confirm_one(
            lead, actor=session['emp_code'], via='bulk-confirmed')
        if not ok:
            skipped.append({'lead_id': lead_id, 'why': detail})
            continue
        done.append({'lead_id': lead.id, 'account': detail['account'],
                     'owner': detail['owner']})

    db.session.commit()
    return jsonify(ok=True, confirmed=len(done), done=done,
                   skipped=skipped,
                   remaining=len(account_gate.pending_leads()))


@bp.route('/api/account-check/<int:lead_id>/dismiss', methods=['POST'])
@_gate
def api_dismiss(lead_id):
    """'Pick another' — the suggestion was looked at and rejected.

    Recorded rather than deleted, so the hit rate can be measured
    later: a suggestion engine nobody can score is one nobody can
    improve.
    """
    from datetime import datetime

    from app import Lead, db
    from app.models.assignment import LeadAccountSuggestion

    lead = db.session.get(Lead, lead_id)
    if lead is None:
        return jsonify(ok=False, error='Lead not found'), 404
    if not may_clear(lead):
        return jsonify(ok=False, error='Not yours to confirm'), 403
    row = LeadAccountSuggestion.query.filter_by(
        lead_id=lead_id).first()
    if row is not None:
        row.acted_at = datetime.utcnow()
        row.acted_by = session['emp_code']
        row.outcome = 'replaced'
        db.session.commit()
    return jsonify(ok=True)


# ── the two panels beside the queue ──────────────────────────────────
@bp.route('/api/account-check/settled')
@_gate
def api_settled():
    """What the CRM and the heads placed today, and how.

    RFQ-DEMO-PARITY. The queue showed only what is still waiting,
    so the work of clearing it was invisible: a head who confirmed
    nine accounts this morning saw the same empty page as one who
    confirmed none, and nobody could tell an account the matcher
    placed by itself from one a person had to settle.
    """
    from datetime import datetime, timedelta

    from app import Company, Lead, db
    from app.access import scope as sc_mod
    from app.models.audit import AuditEvent
    from app.services import rfq_board

    since = datetime.utcnow() - timedelta(days=1)
    rows = (AuditEvent.query
            .filter(AuditEvent.entity_type == 'lead',
                    AuditEvent.action.in_(
                        ('lead.account_linked', 'lead.account_created',
                         'account.linked', 'account.created')),
                    AuditEvent.occurred_at >= since)
            .order_by(AuditEvent.id.desc()).limit(60).all())
    reachable = {lead.id for lead in
                 sc_mod.leads().with_entities(Lead.id).all()}
    out = []
    for row in rows:
        try:
            lead_id = int(row.entity_id)
        except (TypeError, ValueError):
            continue
        if reachable and lead_id not in reachable:
            continue
        lead = db.session.get(Lead, lead_id)
        if lead is None:
            continue
        account = (db.session.get(Company, lead.company_id)
                   if lead.company_id else None)
        by = (row.actor or '').strip()
        out.append({
            'lead_id': lead_id,
            'at': str(getattr(row, 'occurred_at', '') or '')[:16],
            'company': getattr(account, 'name', '') or lead.company or '',
            'subject': (lead.notes or '')[:120],
            'owner': rfq_board.name_of(lead.assigned_to),
            'how': ('Linked automatically' if by in ('', 'system',
                                                     'System')
                    else 'Confirmed'),
            'by': rfq_board.name_of(by) if by else 'the matcher',
            'why': (row.reason or '')[:160],
            'journey': f'/leads/{lead_id}/journey',
        })
    return jsonify(ok=True, rows=out, total=len(out))


@bp.route('/api/account-check/owners')
@_gate
def api_owners():
    """Account owners per vertical, newest first.

    The demo's third card. "New this week" is the Thursday review
    list — accounts are not approved, they are read.
    """
    from datetime import datetime, timedelta

    from app import Company, Lead, db
    from app.models.assignment import AccountVerticalOwner
    from app.services import rfq_board

    week = datetime.utcnow() - timedelta(days=7)
    accounts = (Company.query
                .order_by(Company.id.desc()).limit(120).all())
    ids = [a.id for a in accounts]
    owners = {}
    if ids:
        for row in (AccountVerticalOwner.query
                    .filter(AccountVerticalOwner.account_id.in_(ids))
                    .all()):
            owners.setdefault(row.account_id, []).append(
                (row.vertical, row.emp_code))
    rfq_board.names_for([code for pairs in owners.values()
                         for _v, code in pairs])

    open_leads = {}
    if ids:
        from app.services import sales_rules
        for lead in (db.session.query(Lead.company_id)
                     .filter(Lead.company_id.in_(ids),
                             Lead.is_archived.isnot(True),
                             Lead.stage.in_(sales_rules.open_stages()))
                     .all()):
            open_leads[lead[0]] = open_leads.get(lead[0], 0) + 1

    out = []
    for account in accounts:
        created = getattr(account, 'created_at', None)
        out.append({
            'id': account.id,
            'name': account.name or '',
            # `_blocked` already answers this safely, including
            # when the register cannot be read.
            'blocked': _blocked(account),
            'new_this_week': bool(created and created >= week),
            'owners': [{'vertical': v,
                        'owner': rfq_board.name_of(code)}
                       for v, code in sorted(owners.get(account.id, []))],
            'open_leads': open_leads.get(account.id, 0),
            'created': str(created or '')[:10],
        })
    out.sort(key=lambda r: (not r['new_this_week'], r['name'].lower()))
    return jsonify(ok=True, rows=out, total=len(out),
                   new_this_week=sum(1 for r in out
                                     if r['new_this_week']))
