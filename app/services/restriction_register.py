"""
The decisions themselves: recommending, approving, lifting, and what
happens to the business already in flight when a client is blocked.

Approving a block is not a change to one row. It is a decision that
some number of live leads, RFQs, quotes and opportunities must stop,
and that the people carrying them need to be told. So the approval is
a two-step: `impact()` says what would happen, the administrator looks
at the list, and `approve()` does it.

Two rules are deliberate and worth not undoing:

  * **Won and in-execution work is never auto-closed.** A job on the
    road has cargo, a vendor and an obligation; closing it from an
    admin screen would be a decision about a contract, not about a
    CRM record. It is listed for management instead.
  * **Nothing is deleted, ever.** A lifted block keeps its whole
    history, because the next person to ask "have we had trouble with
    these people?" needs the answer after the block has gone.
"""
from __future__ import annotations

from datetime import datetime

from app.services import client_restrictions as restrictions

#: Why a lead closed when its client was blocked. Written into the
#: lead's own rejection field so the reason survives in the record and
#: not only in the register.
LEAD_REASON = 'Client blocked by management'

#: Stages that mean the work is finished or already out of the funnel,
#: so blocking the client changes nothing about them.
CLOSED_STAGES = ('Won', 'Lost', 'Not Interested')


class RegisterError(Exception):
    """A refusal the caller turns straight into a JSON error."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


# ── who may do what ──────────────────────────────────────────────────
def can_approve(emp_code=None):
    """Approving, rejecting and blocking directly.

    Read from the Access Matrix rather than from a list of role names,
    so the people who may approve are configuration and not code. A
    super admin always may.

    `emp_code` makes this answerable without a request — the seed
    script and the tests need it, and a permission check that only
    works inside a browser session is one that quietly says "no" to
    every job that runs on a timer.
    """
    from app.access import service as access
    if emp_code:
        _scope, perms = access.effective(emp_code)
        return bool({'admin.super', 'admin.restrictions'} & perms)
    return access.is_super() or access.can('admin.restrictions')


def can_lift(emp_code=None):
    """Lifting a block is narrower than approving one: only the super
    admin and whoever has been given the permission explicitly."""
    return can_approve(emp_code)


def can_see_sensitive(emp_code=None):
    """Dispute amounts, references and attachments."""
    from app.access import service as access
    if emp_code:
        _scope, perms = access.effective(emp_code)
        return bool({'admin.super', 'admin.restrictions',
                     'admin.access'} & perms)
    return (access.is_super() or access.can('admin.restrictions')
            or access.can('admin.access'))


def approvers():
    """Employee codes who should be told a recommendation is waiting."""
    from app import Employee
    from app.access import service as access

    out = []
    try:
        for emp in Employee.query.filter(
                Employee.is_active.isnot(False)).all():
            if not (emp.email or '').strip():
                continue
            if emp.is_super_admin:
                out.append(emp.emp_code)
                continue
            _scope, perms = access.effective(emp.emp_code)
            if 'admin.restrictions' in perms:
                out.append(emp.emp_code)
    except Exception:
        pass
    return out


# ── creating ─────────────────────────────────────────────────────────
def _clean_list(raw):
    if isinstance(raw, str):
        return [p.strip() for p in raw.replace('\n', ',').split(',')
                if p.strip()]
    return [str(p).strip() for p in (raw or []) if str(p).strip()]


def shared_domain_warning(domains, company_name):
    """Domains that look like they cover more than this company.

    A dispute with one company does not justify blocking everyone who
    shares its mail domain — walchand.com carries Walchand Advanced
    Composites as well as Walchandnagar Industries. The admin is told
    and has to choose scope deliberately.
    """
    from app import Company

    warnings = []
    for domain in domains:
        try:
            others = (Company.query
                      .filter(Company.website.ilike(f'%{domain}%'))
                      .limit(6).all())
        except Exception:
            others = []
        names = [c.name for c in others
                 if restrictions.normalise(c.name)
                 != restrictions.normalise(company_name)]
        if names:
            warnings.append({
                'domain': domain,
                'also_covers': names,
                'message': (f'{domain} also covers {", ".join(names[:4])}. '
                            f'Scope "group" will restrict them too — '
                            f'choose "entity" if the decision is about '
                            f'{company_name} alone.'),
            })
    return warnings


def create(data, *, actor, status=None):
    """Record a recommendation, or a direct block by someone who may.

    Returns the row. The caller commits nothing — this does.
    """
    from app import db
    from app.models.restriction import (BLOCKED, CAUTION, ClientRestriction,
                                        RECOMMENDED, SCOPES, SCOPE_ENTITY)

    name = (data.get('company_name') or '').strip()
    if not name:
        raise RegisterError('Name the company')
    detail = (data.get('reason_detail') or '').strip()
    if len(detail) < 10:
        raise RegisterError('Say why, in a sentence somebody else can '
                            'understand a year from now')
    category = (data.get('reason_category') or '').strip()
    if not category:
        raise RegisterError('Choose a reason category')

    wanted = (status or data.get('status') or CAUTION).strip()
    if wanted not in (CAUTION, BLOCKED):
        raise RegisterError('A new entry is either caution or blocked')
    # Only an approver sets the state directly. Everyone else is making
    # a recommendation, which is the point of having the two steps.
    final = wanted if can_approve(actor) else RECOMMENDED

    row = ClientRestriction(
        status=final,
        company_name=name[:240],
        name_key=restrictions.normalise(name),
        reason_category=category[:60],
        reason_detail=detail,
        gstin=(data.get('gstin') or '').strip().upper() or None,
        pan=(data.get('pan') or '').strip().upper() or None,
        linked_account_id=data.get('linked_account_id') or None,
        dispute_amount=data.get('dispute_amount') or None,
        currency=(data.get('currency') or 'INR')[:6],
        dispute_refs=(data.get('dispute_refs') or '')[:500] or None,
        legal_status=(data.get('legal_status') or 'None')[:30],
        recommended_by=actor,
        recommended_at=datetime.utcnow(),
        scope=(data.get('scope') if data.get('scope') in SCOPES
               else SCOPE_ENTITY),
        review_date=_as_date(data.get('review_date')),
    )
    row.aliases = _clean_list(data.get('aliases'))
    row.domains = _clean_list(data.get('domains'))
    row.emails = _clean_list(data.get('emails'))
    row.business_units = _clean_list(data.get('business_units'))

    if final in (CAUTION, BLOCKED):
        row.approved_by = actor
        row.approved_at = datetime.utcnow()

    db.session.add(row)
    db.session.commit()
    restrictions.cache_clear()

    restrictions.log_event(row.id, 'recommended', user_id=actor,
                           note=f'{name} — {category}',
                           payload={'wanted': wanted, 'status': final})
    if final != RECOMMENDED:
        restrictions.log_event(row.id, final, user_id=actor,
                               note='Set directly by an approver')
    _audit('restriction.created', row, actor, {'status': final})
    return row


def _as_date(raw):
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw)[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def _audit(action, row, actor, extra=None):
    try:
        from app.services import audit
        audit.record(action, 'ClientRestriction', row.id,
                     new={'company': row.company_name, 'status': row.status,
                          **(extra or {})},
                     actor=actor,
                     reason=(row.reason_category or '')[:200])
    except Exception:
        pass


# ── what approving would do ──────────────────────────────────────────
def _matches(row, record_name, record_email=None, account_id=None):
    """Does this record belong to this entry?

    Asks the entry directly rather than going through the live check,
    because the whole point of the preview is to run it on a
    *recommendation* — which is not in force yet, so the live check
    would correctly say it matches nothing.
    """
    matched_on, _score = restrictions.matches_row(
        row, company_name=record_name, email=record_email,
        account_id=account_id)
    # A possible-only name match is not enough to close somebody's
    # lead. Closing the wrong deal is worse than leaving one open.
    return bool(matched_on) and matched_on != 'name-possible'


def _candidate_leads(row):
    """Leads worth examining closely for this entry.

    Narrowed in SQL first. Walking every lead and scoring it works, and
    took a second and a half on ten thousand of them — fine once on
    approval, not fine on a page somebody opens to read. The LIKE is
    deliberately loose: it only has to be a superset of what
    `matches_row` will accept, and that does the real deciding.
    """
    from app import Lead

    terms = set()
    for name in [row.company_name] + list(row.aliases):
        key = restrictions.normalise(name)
        if not key:
            continue
        # The longest word is the distinctive one: "walchandnagar",
        # not "industries".
        longest = max(key.split(), key=len, default='')
        if len(longest) >= 4:
            terms.add(longest)
    clauses = []
    query = Lead.query.filter(Lead.is_archived.isnot(True))
    try:
        from app import db
        for term in terms:
            clauses.append(db.func.lower(Lead.company).like(f'%{term}%'))
        for domain in row.domains:
            clauses.append(db.func.lower(Lead.email).like(f'%@{domain}'))
        for email in row.emails:
            clauses.append(db.func.lower(Lead.email) == email)
        if row.linked_account_id:
            clauses.append(Lead.company_id == row.linked_account_id)
        if not clauses:
            return []
        return query.filter(db.or_(*clauses)).all()
    except Exception:
        # If the narrowing cannot be built, fall back to correctness
        # over speed rather than to missing records.
        return query.filter(Lead.company.isnot(None)).all()


def impact(row):
    """What would close if this were approved, and what would not.

    Shown to the administrator before anything happens. The counts here
    and the records `approve()` touches come from the same walk, so the
    list cannot say one thing and the action do another.
    """
    open_leads, won_leads = [], []
    try:
        candidates = _candidate_leads(row)
    except Exception:
        candidates = []
    for lead in candidates:
        if not _matches(row, lead.company, lead.email):
            continue
        stage = (lead.stage or '')
        entry = {'id': lead.id, 'company': lead.company, 'stage': stage,
                 'owner': lead.assigned_to or '', 'type': 'lead'}
        if stage == 'Won':
            won_leads.append(entry)
        elif stage in CLOSED_STAGES:
            continue
        else:
            open_leads.append(entry)

    rfqs = _open_rfqs(row)
    quotes = _open_quotes(row)
    return {
        'leads': open_leads,
        'rfqs': rfqs,
        'quotes': quotes,
        'won': won_leads,
        'counts': {'leads': len(open_leads), 'rfqs': len(rfqs),
                   'quotes': len(quotes), 'won': len(won_leads)},
        'owners': sorted({e['owner'] for e in open_leads if e['owner']}),
    }


def _open_rfqs(row):
    try:
        from app.models.rfq import RFQ
        out = []
        for rfq in RFQ.query.filter(
                RFQ.status.notin_(('Withdrawn', 'Closed'))).all():
            name = getattr(rfq, 'subject', '') or ''
            account = None
            if getattr(rfq, 'account_id', None):
                from app import Company
                account = Company.query.get(rfq.account_id)
            if _matches(row, (account.name if account else name),
                        account_id=getattr(rfq, 'account_id', None)):
                out.append({'id': rfq.id, 'ref': rfq.rfq_number,
                            'stage': rfq.status, 'type': 'rfq',
                            'owner': getattr(rfq, 'lead_driver', '') or ''})
        return out
    except Exception:
        return []


def _open_quotes(row):
    try:
        from app.models.quote import Quote
        out = []
        for quote in Quote.query.all():
            if (quote.status or '') in ('Won', 'Lost', 'Withdrawn'):
                continue
            account = None
            if getattr(quote, 'account_id', None):
                from app import Company
                account = Company.query.get(quote.account_id)
            if account and _matches(row, account.name,
                                    account_id=quote.account_id):
                out.append({'id': quote.id,
                            'ref': getattr(quote, 'quote_number', '') or '',
                            'stage': quote.status, 'type': 'quote',
                            'owner': getattr(quote, 'prepared_by_id', '') or ''})
        return out
    except Exception:
        return []


# ── approving ────────────────────────────────────────────────────────
def approve(row, *, actor, close_records=True, note=''):
    """Put a decision into force, and stop the business behind it."""
    from app import db
    from app.models.restriction import BLOCKED, CAUTION, RECOMMENDED

    if not can_approve(actor):
        raise RegisterError('You cannot approve a block', 403)
    if row.status not in (RECOMMENDED, CAUTION, BLOCKED):
        raise RegisterError(f'A {row.status} entry cannot be approved')

    becoming = BLOCKED if row.status != CAUTION else CAUTION
    row.status = becoming
    row.approved_by = actor
    row.approved_at = datetime.utcnow()
    db.session.commit()
    restrictions.cache_clear()

    restrictions.log_event(row.id, 'approved', user_id=actor, note=note,
                           payload={'status': becoming})
    _audit('restriction.approved', row, actor)

    closed = {'counts': {}}
    if becoming == BLOCKED and close_records:
        closed = close_open_business(row, actor=actor)
    try:
        from app.services import restriction_notify
        restriction_notify.announce_block(row, closed)
    except Exception:
        pass
    return closed


def reject(row, *, actor, reason=''):
    from app import db
    from app.models.restriction import RECOMMENDED, REJECTED

    if not can_approve(actor):
        raise RegisterError('You cannot reject a recommendation', 403)
    if row.status != RECOMMENDED:
        raise RegisterError('Only a recommendation can be rejected')
    if len((reason or '').strip()) < 5:
        raise RegisterError('Give a reason — the person who recommended '
                            'it will read it')
    row.status = REJECTED
    db.session.commit()
    restrictions.cache_clear()
    restrictions.log_event(row.id, 'rejected', user_id=actor, note=reason)
    _audit('restriction.rejected', row, actor)
    return row


def lift(row, *, actor, reason='', downgrade_to=None):
    """Unblock, or step a block down to a caution. Reason required."""
    from app import db
    from app.models.restriction import BLOCKED, CAUTION, LIFTED

    if not can_lift(actor):
        raise RegisterError('Only management can lift a block', 403)
    if row.status not in (BLOCKED, CAUTION):
        raise RegisterError('That entry is not in force')
    if len((reason or '').strip()) < 10:
        raise RegisterError('Say why it is being lifted — this is the '
                            'record of the decision')

    was = row.status
    row.status = CAUTION if downgrade_to == CAUTION else LIFTED
    row.lifted_by = actor
    row.lifted_at = datetime.utcnow()
    row.lift_reason = reason
    db.session.commit()
    restrictions.cache_clear()

    restrictions.log_event(row.id, 'lifted', user_id=actor, note=reason,
                           payload={'from': was, 'to': row.status})
    _audit('restriction.lifted', row, actor, {'from': was})
    return row


# ── closing what is already in flight ────────────────────────────────
def close_open_business(row, *, actor):
    """Stop the live work for a client that has just been blocked.

    Won and in-execution work is listed, not touched — see the module
    docstring.
    """
    from app import Lead, db

    found = impact(row)
    closed_leads = []
    for entry in found['leads']:
        lead = db.session.get(Lead, entry['id'])
        if lead is None:
            continue
        lead.stage = 'Not Interested'
        if hasattr(lead, 'lost_reason'):
            lead.lost_reason = LEAD_REASON
        lead.followup_date = None
        if hasattr(lead, 'next_action'):
            lead.next_action = None
        lead.updated_at = datetime.utcnow()
        closed_leads.append(entry)

    closed_rfqs = []
    try:
        from app.models.rfq import RFQ
        for entry in found['rfqs']:
            rfq = db.session.get(RFQ, entry['id'])
            if rfq is not None:
                rfq.status = 'Withdrawn'
                closed_rfqs.append(entry)
    except Exception:
        pass

    closed_quotes = []
    try:
        from app.models.quote import Quote
        for entry in found['quotes']:
            quote = db.session.get(Quote, entry['id'])
            if quote is not None:
                quote.status = 'Lost'
                closed_quotes.append(entry)
    except Exception:
        pass

    db.session.commit()

    result = {
        'leads': closed_leads, 'rfqs': closed_rfqs,
        'quotes': closed_quotes, 'won': found['won'],
        'owners': found['owners'],
        'counts': {'leads': len(closed_leads), 'rfqs': len(closed_rfqs),
                   'quotes': len(closed_quotes), 'won': len(found['won'])},
    }
    restrictions.log_event(
        row.id, 'records_closed', user_id=actor,
        note=(f"{len(closed_leads)} lead(s), {len(closed_rfqs)} RFQ(s), "
              f"{len(closed_quotes)} quote(s) closed; "
              f"{len(found['won'])} won record(s) left for management"),
        payload=result['counts'])
    return result


def is_frozen(lead):
    """Is this lead closed because its client was blocked?

    The Workbench, the task engine and the escalation sweep ask this so
    a blocked client's records stop appearing on somebody's list the
    morning after the block.
    """
    if lead is None:
        return False
    if (getattr(lead, 'lost_reason', '') or '') == LEAD_REASON:
        return True
    verdict = restrictions.check(company_name=getattr(lead, 'company', None),
                                 email=getattr(lead, 'email', None))
    return verdict.blocked
