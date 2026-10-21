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

    __slots__ = ('emp_code', 'vertical', 'codes', 'perms', 'data_scope',
                 'lead_allowances')

    def __init__(self, emp_code, vertical, codes, perms, data_scope,
                 lead_allowances=False):
        self.emp_code = emp_code or ''
        self.vertical = (vertical or '').strip()
        #: None means "no restriction". A set — even an empty one — is a
        #: restriction, and an empty set correctly shows nothing.
        self.codes = codes
        self.perms = set(perms or ())
        self.data_scope = data_scope or DataScope.OWN
        #: The two lead allowances of 21 Oct 2026 (own vertical's leads,
        #: leads with a task) belong to a person's own boundary. A scope
        #: narrowed to show *someone's* records — a review, a workbench
        #: "for=" view — is built without them, so it stays exactly that
        #: person's records.
        self.lead_allowances = bool(lead_allowances)

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
        return Scope(emp_code, vertical, {emp_code}, perms, data_scope,
                     lead_allowances=True)

    return Scope(emp_code, vertical, _vertical_codes(emp), perms, data_scope,
                 lead_allowances=True)


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


#: Decision A2 of 21 October 2026 widens who may open a lead. It
#: is a visibility change over the whole book, so it ships like
#: every other new behaviour here: behind a flag, off, until a
#: pilot has looked at what it reveals.
ALLOWANCE_FLAG = 'FEATURE_LEAD_ALLOWANCES'


def allowances_on():
    try:
        from app.services import flags
        return bool(flags.on(ALLOWANCE_FLAG))
    except Exception:                                     # noqa: BLE001
        return False


def _lead_vertical_names(sc):
    """The lead vertical values (normalised) that belong to a
    vertical-scoped viewer's own vertical.

    `Employee.vertical` holds the four managed names; leads carry more
    (Freight Forwarding, General Transport…). The alias table maps one
    to the other. Only a VERTICAL scope gets this: an own-scope user
    must not see a lead just because it is in their vertical.
    """
    if sc.data_scope != DataScope.VERTICAL or not sc.vertical:
        return set()
    from app.services import vertical_heads
    heads, aliases = vertical_heads._load()
    master = vertical_heads.master_vertical(sc.vertical)
    if master == vertical_heads.FALLBACK_VERTICAL and sc.vertical not in heads:
        master = sc.vertical
    names = {vertical_heads.normalise(master),
             vertical_heads.normalise(sc.vertical)}
    names |= {alias for alias, target in aliases.items() if target == master}
    names.discard('')
    return names


def _lead_task_ids(sc):
    """Ids of leads on which someone in scope holds a task.

    Decision 21 Oct 2026: a head (or user) asked by another vertical to
    act on a lead must be able to open it. Any task, open or closed —
    the history of what they were asked to do stays readable.
    """
    from app.models.task_engine import TaskInstance
    return (TaskInstance.query
            .filter(TaskInstance.entity_type == 'Lead',
                    TaskInstance.owner_user_id.in_(sc.codes))
            .with_entities(TaskInstance.entity_id))


def leads(q=None, sc=None, *, include_deleted=False):
    """Leads the viewer may see — primary or secondary PIC counts.

    Secondary is included deliberately: WP5 made the secondary PIC a
    monitor, and a monitor who cannot open the lead cannot monitor it.

    Leads marked "Delete – not relevant" (CRM-JUNK-DELETE-01) are
    excluded here rather than at each caller, and for everybody
    including an unrestricted scope. This one function is what every
    board, count, digest and report goes through, so excluding them
    here is what makes "it disappears from everywhere" true instead
    of true in the places somebody remembered. The Admin bin passes
    `include_deleted=True`; it is the only caller that does.

    Two allowances on top of ownership (decision 21 Oct 2026,
    behind FEATURE_LEAD_ALLOWANCES): a vertical-scoped viewer sees
    every lead whose own vertical is theirs, whoever owns it; and
    anyone sees a lead on which they, or someone in their scope,
    were given a task. With the flag off this is exactly the
    ownership test it was.
    """
    from sqlalchemy import func, or_

    from app import Lead
    sc = sc or current()
    q = q if q is not None else Lead.query
    if not include_deleted:
        q = q.filter(Lead.junk_deleted.isnot(True))
    if not (sc.lead_allowances and allowances_on()):
        return _narrow(q, Lead.assigned_to, sc,
                       extra_columns=(Lead.secondary_owner,))
    if sc.codes is None:
        return q
    if not sc.codes:
        return q.filter(False)
    clauses = [Lead.assigned_to.in_(sc.codes),
               Lead.secondary_owner.in_(sc.codes),
               Lead.id.in_(_lead_task_ids(sc))]
    names = _lead_vertical_names(sc)
    if names:
        clauses.append(func.lower(func.trim(Lead.procam_vertical))
                       .in_(sorted(names)))
    return q.filter(or_(*clauses))


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
    if (sc.lead_allowances and allowances_on()
            and sc.codes and _is_lead(record)):
        return _lead_allowance(record, sc)
    return False


def _is_lead(record):
    from app import Lead
    return isinstance(record, Lead)


def _lead_allowance(lead, sc):
    """The two lead allowances in `leads()`, for one loaded lead."""
    from app.services import vertical_heads
    names = _lead_vertical_names(sc)
    if names and vertical_heads.normalise(lead.procam_vertical) in names:
        return True
    if lead.id is None:
        return False
    from app.models.task_engine import TaskInstance
    return TaskInstance.query.filter(
        TaskInstance.entity_type == 'Lead',
        TaskInstance.entity_id == lead.id,
        TaskInstance.owner_user_id.in_(sc.codes)).first() is not None


def require_view(record, sc=None):
    """`may_view` that aborts 403 instead of returning False."""
    from flask import abort
    if not may_view(record, sc=sc):
        abort(403, 'Outside your access')
    return record
