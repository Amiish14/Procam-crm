"""Who may reach an RFQ, a quote, a handover, or an account's detail.

The lead, opportunity and account owner rules live in scope.py. The deal
documents hang off those records and are worked by more people than their
owner — the rate-sourcing desk sources an RFQ someone else drives, a team
member helps with a quote, operations works every handover — so each rule
here combines:

  * the viewer's Access Matrix scope (All, Vertical, Own), applied to the
    document's own owner columns;
  * the records it belongs to: a document on a lead, opportunity or
    account the viewer can see is visible with it;
  * team membership on those records (crm_*_members);
  * the working roles that exist in the product: sourcing owners and the
    Rate_Sourcing role for RFQs, and the operations queue for handovers.

Every rule is a query filter, and the per-record checks run the same
filter for one id — so a list and a direct link can never disagree about
what a person may open. An unrestricted scope (All) sees everything.
"""
from sqlalchemy import String, or_

from app.access import scope as scope_mod

#: Employee.department values that work the handover queue.
OPERATIONS_DEPARTMENTS = ('Operations', 'Finance')
#: Grantable in the Access Matrix: every handover, whatever the department.
HANDOVERS_ALL_PERM = 'module.handovers_all'
#: Employee.role that works every RFQ's rate lines.
RATE_SOURCING_ROLE = 'Rate_Sourcing'


def _sc(sc):
    return sc or scope_mod.current()


def _employee(sc):
    from app import Employee
    if not sc.emp_code:
        return None
    return Employee.query.filter_by(emp_code=sc.emp_code).first()


def _member_ids(model_name, fk, codes):
    """Subquery: ids of records on which any of `codes` is an active member."""
    try:
        from app import db
        from app.models import rbac
        model = getattr(rbac, model_name)
        # The team tables arrive with their own migration; before it has
        # run there are no members to count, not an error.
        if not db.inspect(db.engine).has_table(model.__tablename__):
            return None
    except Exception:
        return None
    return (model.query.with_entities(getattr(model, fk))
            .filter(model.user_id.in_(codes), model.is_active.is_(True)))


def _lead_ids(sc):
    from app import Lead
    return scope_mod.leads(sc=sc).with_entities(Lead.id)


def _opp_ids(sc):
    from app import Opportunity
    return scope_mod.opportunities(sc=sc).with_entities(Opportunity.id)


def _company_ids(sc):
    from app import Company
    return scope_mod.companies(sc=sc).with_entities(Company.id)


def _linked(model, sc, *, lead=True, opportunity=True, account=True):
    """Conditions: the document sits on a lead/opportunity/account the
    viewer can see, or on which they are a team member."""
    conds = []
    if lead and hasattr(model, 'lead_id'):
        conds.append(model.lead_id.in_(_lead_ids(sc)))
        members = _member_ids('LeadMember', 'lead_id', sc.codes)
        if members is not None:
            conds.append(model.lead_id.in_(members))
    if opportunity and hasattr(model, 'opportunity_id'):
        conds.append(model.opportunity_id.in_(_opp_ids(sc)))
        members = _member_ids('DealMember', 'opportunity_id', sc.codes)
        if members is not None:
            conds.append(model.opportunity_id.in_(members))
    if account and hasattr(model, 'account_id'):
        conds.append(model.account_id.in_(_company_ids(sc)))
        members = _member_ids('AccountMember', 'account_id', sc.codes)
        if members is not None:
            conds.append(model.account_id.in_(members))
    return conds


# ── RFQs ─────────────────────────────────────────────────────────────
def rfqs(q=None, sc=None):
    from app.models.rfq import RFQ, RateSourcingLine
    sc = _sc(sc)
    q = q if q is not None else RFQ.query
    if sc.codes is None:
        return q
    if not sc.codes:
        return q.filter(False)
    emp = _employee(sc)
    if emp is not None and (emp.role or '') == RATE_SOURCING_ROLE:
        return q
    sourcing = (RateSourcingLine.query.with_entities(RateSourcingLine.rfq_id)
                .filter(RateSourcingLine.sourcing_owner_id.in_(sc.codes)))
    conds = [RFQ.lead_driver.in_(sc.codes), RFQ.created_by_id.in_(sc.codes),
             RFQ.id.in_(sourcing)]
    # A supporting user is held in a JSON list; matched as text so it works
    # on SQLite without JSON functions.
    for code in sorted(sc.codes)[:50]:
        conds.append(RFQ.id.in_(
            RateSourcingLine.query.with_entities(RateSourcingLine.rfq_id)
            .filter(RateSourcingLine.supporting_user_ids.cast(String)
                    .like(f'%"{code}"%'))))
    conds += _linked(RFQ, sc)
    return q.filter(or_(*conds))


def may_view_rfq(rfq, sc=None):
    from app.models.rfq import RFQ
    return rfq is not None and rfqs(sc=sc).filter(RFQ.id == rfq.id) \
        .first() is not None


# ── quotes ───────────────────────────────────────────────────────────
def quotes(q=None, sc=None):
    from app.models.quote import Quote
    sc = _sc(sc)
    q = q if q is not None else Quote.query
    if sc.codes is None:
        return q
    if not sc.codes:
        return q.filter(False)
    from app.models.rfq import RFQ
    conds = [Quote.prepared_by_id.in_(sc.codes),
             Quote.created_by_id.in_(sc.codes),
             Quote.submitted_by_id.in_(sc.codes),
             Quote.approved_by_id.in_(sc.codes),
             Quote.rfq_id.in_(rfqs(sc=sc).with_entities(RFQ.id))]
    conds += _linked(Quote, sc)
    return q.filter(or_(*conds))


def may_view_quote(quote, sc=None):
    from app.models.quote import Quote
    return quote is not None and quotes(sc=sc).filter(
        Quote.id == quote.id).first() is not None


# ── handovers ────────────────────────────────────────────────────────
def sees_all_handovers(sc=None):
    sc = _sc(sc)
    if sc.codes is None or sc.can(HANDOVERS_ALL_PERM):
        return True
    emp = _employee(sc)
    return bool(emp is not None and
                (emp.department or '') in OPERATIONS_DEPARTMENTS)


def handovers(q=None, sc=None):
    from app.models.tms_handover import WonHandover
    sc = _sc(sc)
    q = q if q is not None else WonHandover.query
    if sees_all_handovers(sc):
        return q
    if not sc.codes:
        return q.filter(False)
    from app.models.rfq import RFQ
    from app.models.quote import Quote
    conds = [WonHandover.pic_emp_code.in_(sc.codes),
             WonHandover.created_by_id.in_(sc.codes),
             WonHandover.quote_id.in_(quotes(sc=sc).with_entities(Quote.id)),
             WonHandover.rfq_id.in_(rfqs(sc=sc).with_entities(RFQ.id))]
    conds += _linked(WonHandover, sc, lead=False)
    return q.filter(or_(*conds))


def may_view_handover(row, sc=None):
    from app.models.tms_handover import WonHandover
    return row is not None and handovers(sc=sc).filter(
        WonHandover.id == row.id).first() is not None


# ── accounts ─────────────────────────────────────────────────────────
FULL, PARTIAL, ROUTING = 'full', 'partial', 'routing'


def company_access(company, sc=None):
    """How much of an account's detail the viewer may see.

    full     the account is theirs (owner, secondary, vertical or All
             scope) or they are on its team
    partial  it is someone else's account, but the viewer has leads or
             opportunities on it — they see those, and its contacts
    routing  neither — they see who owns it and how to reach them, which
             §5.1 keeps open to everyone, and nothing else
    """
    from app import Company, Lead, Opportunity
    sc = _sc(sc)
    if company is None:
        return ROUTING
    if sc.codes is None:
        return FULL
    if scope_mod.companies(sc=sc).filter(Company.id == company.id).first():
        return FULL
    members = _member_ids('AccountMember', 'account_id', sc.codes or [''])
    if members is not None and members.filter_by(
            account_id=company.id).first() is not None:
        return FULL
    if scope_mod.leads(sc=sc).filter(Lead.company_id == company.id).first() \
            or scope_mod.opportunities(sc=sc).filter(
                Opportunity.company_id == company.id).first():
        return PARTIAL
    return ROUTING
