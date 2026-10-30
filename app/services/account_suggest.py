"""
The matcher's best guess at a lead's customer, written down.

CRM-ACCOUNT-FIRST-01, before FEATURE_ACCOUNT_FIRST goes on.

`auto_link` decides whether mail can be filed without asking. This
answers a different question for the 352 open leads already in the
CRM with no account: *what would we suggest*, and how sure is it.
The difference matters — auto_link's answer is "link it or don't",
and these need "here is the answer, press the button", because a
wrong link on arrival costs one lead and a wrong link applied to
352 at once reassigns somebody's customer quietly.

Confidence is derived from which rule fired, not from a model.
There is no training data here and inventing a score would make
the number look like evidence when it is a ranking.
"""
from __future__ import annotations

from datetime import datetime

#: How far each rule is to be trusted. An identifier someone
#: already corrected, or one the customer themselves supplied,
#: against a name that merely looks similar.
CONFIDENCE = {
    'learned email': 100,          # a person said so, for this sender
    'GSTIN': 98,                   # the customer's own number
    'contact email': 95,           # this person is on file
    'learned domain': 90,          # a person said so, for the domain
    'account email domain': 85,
    'account website domain': 78,
    'company name': 55,            # the typed name, normalised
}
#: Below this, the screen shows the suggestion greyed and
#: bulk-confirm refuses it outright.
TRUSTED = 75


def confidence_of(rule, *, note=''):
    """0–100 for a rule, and lower for a mere resemblance."""
    base = CONFIDENCE.get(rule, 40)
    if rule == 'company name' and '% like' in (note or ''):
        # "72% like X" is a resemblance, not a match. Keep it well
        # under TRUSTED however close the strings are.
        try:
            pct = int((note or '').split('%')[0].strip())
            return min(base - 5, max(20, pct // 2))
        except (ValueError, IndexError):
            return 25
    return base


def _text_of(lead):
    subject = (lead.forwarded_subject or lead.original_email_subject
               or '')
    body = (lead.original_email_body or lead.notes or '')
    return f'{subject}\n{body}'[:4000]


def sender_of(lead):
    return (lead.email or lead.original_email_from or '').strip()


def suggest_for(lead):
    """What we would suggest for this lead. Never writes."""
    from app.services import account_match

    sender = sender_of(lead)
    rows = account_match.candidates(
        sender, company_name=lead.company, text=_text_of(lead))

    linked, rule = account_match.auto_link(
        sender, company_name=lead.company, text=_text_of(lead))

    best = rows[0] if rows else None
    return {
        'lead': lead,
        'sender': sender,
        'account_id': best['account_id'] if best else None,
        'account': best['name'] if best else '',
        'rule': best['rule'] if best else '',
        'confidence': (confidence_of(best['rule'], note=best['note'])
                       if best else 0),
        'candidate_count': len(rows),
        # Exactly the shape auto_link would have linked on its own.
        # Anything else needs a person to look, however high the
        # confidence of the top row.
        'exact_single': linked is not None,
        'auto_rule': rule,
        'alternatives': [
            {'account_id': r['account_id'], 'name': r['name'],
             'rule': r['rule'], 'note': r['note'],
             'kind': r.get('kind', ''),
             'confidence': confidence_of(r['rule'], note=r['note'])}
            for r in rows[1:6]],
        #: Rival claims, not resemblances. This is what "several
        #: matched" should mean on the report.
        'rival_count': sum(1 for r in rows
                           if r.get('kind') != 'resemblance'),
    }


def store(found, *, commit=False):
    """Write one suggestion, replacing any standing one."""
    from app import db
    from app.models.assignment import LeadAccountSuggestion

    lead = found['lead']
    row = LeadAccountSuggestion.query.filter_by(
        lead_id=lead.id).first()
    if row is None:
        row = LeadAccountSuggestion(lead_id=lead.id)
        db.session.add(row)
    elif row.acted_at is not None:
        # Somebody already decided this one. Re-running the pass
        # must not quietly replace a human answer with a guess.
        return row
    row.account_id = found['account_id']
    row.rule = found['rule']
    row.confidence = found['confidence']
    row.candidate_count = found['candidate_count']
    row.exact_single = found['exact_single']
    row.alternatives = found['alternatives']
    row.sender_seen = found['sender'][:320]
    row.computed_at = datetime.utcnow()
    if commit:
        db.session.commit()
    return row


def stale(row, lead):
    """Is this stored suggestion still worth offering?

    Stale if the sender has changed, or if the matcher now answers
    differently — an account created or an alias learnt since the
    pass ran. Checked before anything is written from a stored row,
    because confirming 300 at once is exactly where a stale answer
    does damage.
    """
    if row is None:
        return True
    if (row.sender_seen or '') != sender_of(lead):
        return True
    fresh = suggest_for(lead)
    return (fresh['account_id'] != row.account_id
            or fresh['exact_single'] != bool(row.exact_single))


def batch_scope():
    """The scope an offline pass runs under: everything.

    `scope.current()` reads the session and cannot answer outside a
    request, so a script calling it fails at the first lead. Said
    out loud here rather than defaulted to, because a function that
    quietly widens its own scope when no user is present is exactly
    the shape of an access leak.
    """
    from app.access.scope import Scope
    return Scope(emp_code='', vertical='', codes=None, perms=(),
                 data_scope='all')


def pending(sc=None):
    """Open leads with no account, through the Access Matrix.

    `sc` is required in a request — pass `scope.current()`. Outside
    one, pass `batch_scope()`; there is no implicit default, so a
    caller cannot get the wide scope by forgetting.
    """
    from flask import has_request_context

    from app import Lead
    from app.access import scope as sc_mod
    from app.services import sales_rules

    if sc is None:
        if not has_request_context():
            raise RuntimeError(
                'account_suggest.pending() needs an explicit scope '
                'outside a request — pass batch_scope() from a '
                'script, or scope.current() from a view.')
        sc = sc_mod.current()

    q = (Lead.query
         .filter(Lead.is_archived.isnot(True),
                 Lead.company_id.is_(None),
                 Lead.stage.in_(sales_rules.open_stages())))
    return sc_mod.leads(q, sc=sc).all()


def bucket(found):
    """'one exact' | 'several' | 'weak' | 'none' — for the report.

    'several' counts rival claims, not resemblances: two accounts
    the evidence actually points at, which is the case a person
    has to settle.
    """
    if not found['candidate_count']:
        return 'none'
    if found['exact_single']:
        return 'one exact'
    if found.get('rival_count', found['candidate_count']) > 1:
        return 'several'
    return 'weak'


def confirm_one(lead, *, actor, account_id=None,
                via='Account check'):
    """Link one lead to its suggested account. (ok, detail).

    The single implementation behind both the Account check button
    and the offline bulk pass. Everything is recomputed here — the
    caller's view of the suggestion is at best seconds old, and at
    worst a stored row from a pass that ran yesterday.

    Returns (False, why) rather than raising: a batch of 129 must
    report what it skipped and carry on, because a batch that stops
    at the first oddity gets re-run with the guard removed.
    """
    from app import Company, db
    from app.models.assignment import LeadAccountSuggestion
    from app.services import account_gate, account_match, audit

    if lead is None:
        return False, 'no such lead'
    if lead.company_id:
        existing = db.session.get(Company, lead.company_id)
        if not account_match.not_a_customer(existing):
            return False, 'already has an account'

    found = suggest_for(lead)
    chosen = account_id or found['account_id']
    account = db.session.get(Company, chosen or 0)
    if account is None:
        return False, 'nothing matched'

    why = account_match.not_a_customer(account)
    if why:
        return False, why
    if account_id is None:
        if not found['exact_single']:
            return False, 'no longer a single exact match'
        if found['confidence'] < TRUSTED:
            return False, (f'confidence {found["confidence"]} is '
                           f'below the floor')

    vertical = account_gate.vertical_of(lead)
    owner = account_gate.owner_for(account, vertical)
    if not owner:
        try:
            from app.services import vertical_heads
            heads = vertical_heads.heads_for_vertical(vertical) or []
            owner = heads[0] if heads else ''
        except Exception:                                 # noqa: BLE001
            owner = ''
    if not owner:
        return False, (f'nobody owns {account.name} for '
                       f'{vertical or "this vertical"}')

    was = lead.company_id
    lead.company_id = account.id
    _ensure_owner(account, vertical, owner, actor=actor)
    _assign(lead, owner)

    row = LeadAccountSuggestion.query.filter_by(lead_id=lead.id).first()
    if row is not None:
        row.acted_at = datetime.utcnow()
        row.acted_by = actor
        row.outcome = 'accepted'

    audit.record('lead.account_confirmed', 'lead', lead.id,
                 old={'company_id': was},
                 new={'company_id': account.id, 'account': account.name,
                      'assigned_to': owner, 'rule': found['rule'],
                      'confidence': found['confidence']},
                 actor=actor,
                 reason=f'{via}: confirmed from a single exact '
                        f'match ({found["rule"]})')
    return True, {'account': account.name, 'owner': owner,
                  'rule': found['rule']}


def _ensure_owner(account, vertical, owner, *, actor):
    from app import db
    from app.models.assignment import AccountVerticalOwner
    from app.services import audit

    if not vertical:
        return
    row = AccountVerticalOwner.query.filter_by(
        account_id=account.id, vertical=vertical).first()
    if row is not None:
        row.active = True
        return
    db.session.add(AccountVerticalOwner(
        account_id=account.id, vertical=vertical, emp_code=owner,
        source='manual', note='set when the account was confirmed'))
    audit.record('account.vertical_owner_set', 'company', account.id,
                 new={'vertical': vertical, 'emp_code': owner},
                 actor=actor, reason='named when a lead was confirmed')


def _assign(lead, owner):
    from app.services import lead_assignment
    try:
        lead_assignment.assign(lead, primary_code=owner, actor=None,
                               note='account confirmed',
                               _defer_commit=True)
    except Exception:                                     # noqa: BLE001
        lead.assigned_to = owner
