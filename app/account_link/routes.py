"""The leads on nobody's account, with Accept and Reject per row.

CRM-MAIL-2026-11-11 10, the screen half: "an admin screen listing the
same proposals with Accept / Reject per row".

    GET  /admin/account-link                   the screen
    GET  /api/admin/account-link               the proposals, as JSON
    POST /api/admin/account-link/accept        {lead_id, account_id}
    POST /api/admin/account-link/reject        {lead_id, why}

WHY A REJECT AT ALL

Because the strongest thing this screen can learn is that a proposal
is wrong. Without it, the same wrong suggestion is at the top of the
list every morning and the list stops being read. A rejection is an
audit row against the lead naming the account that was refused, and
the proposal is not offered again.

It does not archive, close or change the lead. "This is not Andritz"
and "this is not an enquiry" are different statements, and the second
one has its own button on the lead (Informational - not a lead).

THE SAME PROPOSALS AS THE SCRIPT

Both read `scripts/2026_11_11_link_accounts.py`'s `propose()`, which
reads `account_match` and `lead_account.suggest`. Three things that
disagreed about which account a lead belongs to would be worse than
none.
"""
from __future__ import annotations

import os
import sys
from functools import wraps

from flask import Blueprint, jsonify, render_template, request, session

bp = Blueprint('account_link', __name__)

#: Rejections live in the audit log rather than in a table of their
#: own: there are a few hundred of them at most, they are read once
#: per screen load, and an audit row is already the thing that says
#: who decided what and when.
REJECTED = 'lead.account_proposal_rejected'

#: A page of proposals. The survey asks the matcher about every open
#: lead with no account, which is a second or two for 384 of them —
#: fine for a screen somebody opens, too slow to do twice per click.
PAGE = 60


def _me():
    return session.get('emp_code')


def _link_admin(f):
    """Admin, through the Access Matrix.

    Linking a lead to an account moves it onto somebody else's
    customer record and into their reports, which is why this is not
    the lead owner's screen. The lead sheet's own account picker is
    theirs.
    """
    @wraps(f)
    def wrap(*a, **kw):
        if not _me():
            return jsonify(ok=False, error='Sign in first.'), 401
        try:
            from app.access.service import can
            allowed = bool(can('admin.triage') or can('admin.master'))
        except Exception:                                 # noqa: BLE001
            allowed = False
        if not allowed:
            return jsonify(ok=False,
                           error='Only an administrator can link leads '
                                 'to accounts in bulk.'), 403
        return f(*a, **kw)
    return wrap


def _script():
    """The one-off script, imported as a module.

    It holds `propose()`, and importing it is how the screen and the
    CSV cannot drift apart. A path import rather than a package one
    because scripts/ is not a package — and it is read-only at
    import: everything it does lives behind `main()`.
    """
    import importlib.util

    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    path = os.path.join(root, 'scripts', '2026_11_11_link_accounts.py')
    name = 'crm_link_accounts_script'
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _rejected_pairs():
    """{(lead_id, account_id)} already refused by a person."""
    out = set()
    try:
        from app.models.audit import AuditEvent
        rows = (AuditEvent.query
                .filter(AuditEvent.action == REJECTED)
                .order_by(AuditEvent.id.desc()).limit(2000).all())
    except Exception:                                     # noqa: BLE001
        return out
    for row in rows:
        payload = row.new_value if isinstance(row.new_value, dict) else {}
        try:
            out.add((int(row.entity_id), int(payload.get('account_id') or 0)))
        except (TypeError, ValueError):
            continue
    return out


def proposals(limit=PAGE):
    """[{lead, account, confidence, rule}] for the screen."""
    from app.access import scope as sc_mod

    script = _script()
    sc = sc_mod.current()
    refused = _rejected_pairs()
    rows = []
    for lead, code, why in script.stuck_leads():
        if not sc_mod.may_view(lead):
            continue
        confidence, best, others = script.propose(lead)
        account_id = best['account'].id if best else 0
        if best and (lead.id, account_id) in refused:
            # Offer the next candidate instead of the refused one.
            others = [r for r in others
                      if (lead.id, r['account_id']) not in refused]
            if others:
                from app import Company, db
                account = db.session.get(Company, others[0]['account_id'])
                best = ({'account': account,
                         'rule': others[0].get('rule') or 'suggested'}
                        if account is not None else None)
                others = others[1:]
            else:
                best, confidence = None, script.NONE
        rows.append({
            'lead_id': lead.id,
            'company': (lead.company or '')[:70],
            'placeholder': code == 'placeholder_account',
            'why': why,
            'sender': (lead.email or lead.original_email_from or '')[:70],
            'subject': (lead.original_email_subject
                        or lead.forwarded_subject or '')[:90],
            'stage': lead.stage or '',
            'owner': lead.assigned_to or '',
            'vertical': script._vertical_of(lead),
            'created': str(lead.created_at or '')[:10],
            'confidence': confidence,
            'account_id': account_id or None,
            'account_name': (best['account'].name if best else ''),
            'rule': (best['rule'] if best else ''),
            'others': [{'account_id': r['account_id'],
                        'name': r.get('name') or '',
                        'rule': r.get('rule') or ''} for r in others[:4]],
        })
        if len(rows) >= limit:
            break
    order = {script.CERTAIN: 0, script.LIKELY: 1,
             script.POSSIBLE: 2, script.NONE: 3}
    rows.sort(key=lambda r: (order.get(r['confidence'], 9), r['lead_id']))
    return rows


@bp.route('/admin/account-link')
@_link_admin
def page():
    return render_template('admin/account_link.html')


@bp.route('/api/admin/account-link')
@_link_admin
def api_proposals():
    limit = request.args.get('limit', type=int) or PAGE
    rows = proposals(limit=min(max(limit, 1), 200))
    return jsonify(ok=True, rows=rows, count=len(rows),
                   note=('The strongest proposals first. "certain" is a '
                         'domain, a GSTIN or a sender somebody already '
                         'mapped; "likely" is an exact name and nothing '
                         'disagreeing.'))


@bp.route('/api/admin/account-link/accept', methods=['POST'])
@_link_admin
def api_accept():
    from app import Company, Lead, db
    from app.services import lead_account

    d = request.get_json(silent=True) or {}
    lead = db.session.get(Lead, d.get('lead_id') or 0)
    account = db.session.get(Company, d.get('account_id') or 0)
    if lead is None:
        return jsonify(ok=False, error='No such lead'), 404
    if account is None:
        return jsonify(ok=False, error='Choose an account'), 400
    if lead.company_id:
        return jsonify(ok=False,
                       error=f'That lead is already on account '
                             f'#{lead.company_id}'), 409
    ok, detail = lead_account.link(
        lead, account, actor=_me(), learn=False,
        reason='CRM-MAIL-2026-11-11 §10: accepted on Admin → '
               'Account link')
    if not ok:
        db.session.rollback()
        return jsonify(ok=False, error=detail), 400
    db.session.commit()
    return jsonify(ok=True, lead_id=lead.id, account=account.name)


@bp.route('/api/admin/account-link/reject', methods=['POST'])
@_link_admin
def api_reject():
    from app import Company, Lead, db
    from app.services import audit

    d = request.get_json(silent=True) or {}
    lead = db.session.get(Lead, d.get('lead_id') or 0)
    account_id = d.get('account_id')
    if lead is None:
        return jsonify(ok=False, error='No such lead'), 404
    if not account_id:
        return jsonify(ok=False,
                       error='Say which account is being refused'), 400
    account = db.session.get(Company, account_id)
    audit.record(REJECTED, 'lead', lead.id,
                 new={'account_id': int(account_id),
                      'account': (account.name if account else ''),
                      'why': (d.get('why') or '')[:200]},
                 actor=_me(),
                 reason=('CRM-MAIL-2026-11-11 §10: not this customer. '
                         'The lead is unchanged.'))
    db.session.commit()
    return jsonify(ok=True, lead_id=lead.id,
                   note='Not offered again. The lead is unchanged.')
