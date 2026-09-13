"""
One answer to "what may this person see?" — Phase 0 of Procam AI.

Before this, the portal had three. `app.py::_scope_for_current_session()`
read `session['role']` and walked the reporting tree; reports_v2 and
pic360 each read the Access Matrix through their own near-identical
copy; and `_require_lead_access()` checked the role again, per record.
They disagreed: somebody set to *whole company* in the matrix but
without `role='admin'` saw the whole company in Reports and only their
own leads in Pipeline, and both answers were correct according to the
code that produced them.

A Copilot spans every screen, so it has to pick one, and any pick makes
some existing screen look wrong. That is why this module exists before
any Copilot code.

    from app.access import scope

    sc = scope.current()             # the signed-in user
    leads = scope.leads()            # a Lead query they may see
    if scope.may_view(lead): ...

The Access Matrix is authoritative
    `access.service.effective()` already resolves the matrix correctly,
    including falling back to a role-derived default for anyone nobody
    has configured. This module adds the missing half: turning that
    answer into a query filter, in one place, for every entity.

Widening a query is impossible here by construction: every helper starts
from the scope and narrows. There is no "unscoped" variant to reach for
by mistake — `Lead.query` is still there for code that genuinely needs
it, but nothing in the Copilot may use it.
"""
from __future__ import annotations

from flask import session

from app.access.service import effective
from app.models.access import DataScope


class Scope:
    """A resolved data boundary. Immutable by intent — build a new one
    rather than editing this, so nothing downstream can widen it."""

    __slots__ = ('emp_code', 'vertical', 'codes', 'perms', 'data_scope')

    def __init__(self, emp_code, vertical, codes, perms, data_scope):
        self.emp_code = emp_code or ''
        self.vertical = (vertical or '').strip()
        #: None means "no restriction". A set — even an empty one — is a
        #: restriction, and an empty set correctly shows nothing.
        self.codes = codes
        self.perms = set(perms or ())
        self.data_scope = data_scope or DataScope.OWN

    # ── questions ────────────────────────────────────────────────────
    @property
    def unrestricted(self):
        return self.codes is None

    def can(self, perm):
        return perm in self.perms

    def reaches(self, emp_code):
        """Whether this scope covers a given employee's records."""
        if self.codes is None:
            return True
        return bool(emp_code) and emp_code in self.codes

    def __repr__(self):
        n = 'all' if self.codes is None else len(self.codes)
        return (f'<Scope {self.emp_code} {self.data_scope} '
                f'codes={n} perms={len(self.perms)}>')


#: What an unknown or signed-out caller gets: nothing at all. Deliberately
#: not "everything" — a scope that fails open is the one bug in this
#: module that could not be caught by reading a query.
def _nobody():
    return Scope('', '', set(), set(), DataScope.OWN)


def for_employee(emp_code):
    """The boundary in force for one employee."""
    from app import Employee

    if not emp_code:
        return _nobody()

    data_scope, perms = effective(emp_code)
    emp = Employee.query.filter_by(emp_code=emp_code).first()
    if emp is None:
        # Authenticated as somebody the employee master does not know.
        # No records, but keep their permissions so the UI can explain
        # itself rather than 500.
        return Scope(emp_code, '', set(), perms, data_scope)

    vertical = (emp.vertical or '').strip()

    if data_scope == DataScope.ALL:
        return Scope(emp_code, vertical, None, perms, data_scope)

    if data_scope == DataScope.OWN:
        return Scope(emp_code, vertical, {emp_code}, perms, data_scope)

    return Scope(emp_code, vertical, _vertical_codes(emp), perms, data_scope)


def _vertical_codes(emp):
    """Everyone whose records a vertical-scoped viewer may see.

    Their own vertical, plus anyone reporting to them — the definition
    reports_v2 and pic360 already agreed on. It is broader than the
    legacy leads resolver, which walked only the reporting tree: a
    vertical head who was never set as anyone's `vertical_head_id` used
    to see nothing but their own leads. Matching the matrix is the point
    of this module, so the broader, documented definition wins.
    """
    from app import Employee
    from sqlalchemy import or_

    codes = {emp.emp_code}
    vertical = (emp.vertical or '').strip()

    clauses = [Employee.vertical_head_id == emp.id]
    if vertical:
        clauses.append(Employee.vertical == vertical)
    for other in Employee.query.filter(or_(*clauses)).all():
        if other.emp_code:
            codes.add(other.emp_code)
    return codes


def current():
    """The boundary for the signed-in user."""
    return for_employee(session.get('emp_code'))


# ── query helpers, one per entity family ─────────────────────────────
#
# Each takes an optional base query so a caller can pre-filter, and
# returns it narrowed to the scope. Passing no query starts from the
# whole table — which is safe, because the narrowing is what this
# function is for.

def _narrow(q, column, sc, *, extra_columns=()):
    """Confine a query to a scope on one or more owner columns."""
    if sc.codes is None:
        return q
    if not sc.codes:
        # Belt and braces: SQLAlchemy already renders IN () as an
        # always-false expression, so this line changes no behaviour
        # today. It is here so that an empty scope reads as "nothing"
        # to the next person, and survives a refactor that stops going
        # through in_().
        return q.filter(False)
    from sqlalchemy import or_

    cols = [column, *extra_columns]
    return q.filter(or_(*[c.in_(sc.codes) for c in cols]))


def leads(q=None, sc=None):
    """Leads the viewer may see — primary or secondary PIC counts.

    Secondary is included deliberately: WP5 made the secondary PIC a
    monitor, and a monitor who cannot open the lead cannot monitor it.
    """
    from app import Lead
    sc = sc or current()
    return _narrow(q if q is not None else Lead.query,
                   Lead.assigned_to, sc,
                   extra_columns=(Lead.secondary_owner,))


def opportunities(q=None, sc=None):
    from app import Opportunity
    sc = sc or current()
    return _narrow(q if q is not None else Opportunity.query,
                   Opportunity.owner_emp_code, sc)


def rfqs(q=None, sc=None):
    """RFQs the viewer may reach — the rule in app/access/records.py.

    These helpers once applied only the RFQ's own owner column, so the
    older Copilot answers and data-quality checks hid RFQs that the RFQ
    screens show (a sourcing owner's, a team member's). One rule now.
    """
    from app.access import records
    return records.rfqs(q, sc=sc)


def quotes(q=None, sc=None):
    """Quotes the viewer may reach — app/access/records.py."""
    from app.access import records
    return records.quotes(q, sc=sc)


def handovers(q=None, sc=None):
    """Handovers the viewer may reach — app/access/records.py, which
    includes the operations queue."""
    from app.access import records
    return records.handovers(q, sc=sc)


def companies(q=None, sc=None):
    """Accounts whose PIC the viewer reaches.

    Existence of an account is not secret — §5.1 and §6.6 both turn on
    being able to say "this is already a Procam account, talk to its
    owner". Callers that answer that question use `Company.query`
    directly and return only routing; this helper is for the detail.
    """
    from app import Company
    sc = sc or current()
    return _narrow(q if q is not None else Company.query,
                   Company.pic_emp_code, sc,
                   extra_columns=(Company.secondary_pic_emp_code,))


def contacts(q=None, sc=None):
    """Contacts the viewer may see: their own, those of accounts they
    own, and those at accounts where they are working a lead — you cannot
    work an enquiry without its people."""
    from app import Contact, Lead
    sc = sc or current()
    q = q if q is not None else Contact.query
    if sc.codes is None:
        return q
    if not sc.codes:
        return q.filter(False)
    from sqlalchemy import or_
    from app import Company
    return q.filter(or_(
        Contact.assigned_to.in_(sc.codes),
        Contact.company_id.in_(companies(sc=sc).with_entities(Company.id)),
        Contact.company_id.in_(leads(sc=sc).with_entities(Lead.company_id))))


def activities(q=None, sc=None):
    from app import Lead, LeadActivity
    sc = sc or current()
    q = q if q is not None else LeadActivity.query
    if sc.codes is None:
        return q
    if not sc.codes:
        return q.filter(False)
    return q.filter(LeadActivity.lead_id.in_(
        leads(sc=sc).with_entities(Lead.id)))


def emails(q=None, sc=None):
    from app import Lead, LeadEmail
    sc = sc or current()
    q = q if q is not None else LeadEmail.query
    if sc.codes is None:
        return q
    if not sc.codes:
        return q.filter(False)
    return q.filter(LeadEmail.lead_id.in_(
        leads(sc=sc).with_entities(Lead.id)))


def notes(q=None, sc=None):
    from app import Lead, LeadNote
    sc = sc or current()
    q = q if q is not None else LeadNote.query
    if sc.codes is None:
        return q
    if not sc.codes:
        return q.filter(False)
    return q.filter(LeadNote.lead_id.in_(
        leads(sc=sc).with_entities(Lead.id)))


def employees(q=None, sc=None):
    """People the viewer may see figures about."""
    from app import Employee
    sc = sc or current()
    q = q if q is not None else Employee.query
    if sc.codes is None:
        return q
    if not sc.codes:
        return q.filter(False)
    return q.filter(Employee.emp_code.in_(sc.codes))


# ── per-record checks ────────────────────────────────────────────────
def may_view(record, sc=None):
    """Whether one already-loaded record is inside the boundary.

    Reads whichever owner column the record actually has, so callers do
    not have to know. An object with no recognised owner column is
    visible only to an unrestricted viewer — the safe direction.
    """
    sc = sc or current()
    if record is None:
        return False
    if sc.codes is None:
        return True

    for attr in ('assigned_to', 'owner_emp_code', 'lead_driver',
                 'prepared_by_id', 'pic_emp_code'):
        if hasattr(record, attr):
            if sc.reaches(getattr(record, attr) or ''):
                return True
    for attr in ('secondary_owner', 'secondary_pic_emp_code'):
        if hasattr(record, attr) and sc.reaches(getattr(record, attr) or ''):
            return True
    return False


def require_view(record, sc=None):
    """`may_view` that aborts 403 instead of returning False."""
    from flask import abort
    if not may_view(record, sc=sc):
        abort(403, 'Outside your access')
    return record
