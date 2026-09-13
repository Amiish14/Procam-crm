"""Phase 0 — one scope service, proved per persona.

These are the tests the Procam AI RBAC suite (§16) is built on. They
assert the boundary itself, so that every later layer — query templates,
RAG retrieval, the Copilot's answers — inherits something already
proven rather than re-deriving it.

The important assertions here are the negative ones. A scope test that
only checks "the admin sees more than the salesperson" passes while
leaking, because it never checks that the salesperson sees *exactly*
their own rows and no others.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'ScopeTestOnly12345')
# A schema built from the models, not whatever the dev database happens
# to be. These tests assert a boundary; running them against a stale
# schema would prove something about the wrong columns.
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'scope.db')

from app import (app as flask_app, db, Employee, Lead, Company,  # noqa: E402
                 Opportunity)
from app.access import scope                                     # noqa: E402
from app.access.service import set_profile                       # noqa: E402
from app.models.access import DataScope                          # noqa: E402


@pytest.fixture(scope='module')
def world():
    """Four people, one company, and a lead each.

    HEAD9 and REP9 share a vertical; OUT9 is in another one entirely.
    That third person is what makes the vertical tests mean anything —
    without someone outside the vertical, "sees their vertical" and
    "sees everything" are indistinguishable.
    """
    with flask_app.app_context():
        db.create_all()

        people = [
            ('SCADM9', 'Scope Admin',  'admin', 'All',            False),
            ('HEAD9',  'Scope Head',   'user',  'Heavy Transport', True),
            ('REP9',   'Scope Rep',    'user',  'Heavy Transport', False),
            ('OUT9',   'Other Vert',   'user',  'Warehousing',     False),
        ]
        made = {}
        for code, name, role, vertical, is_head in people:
            e = Employee.query.filter_by(emp_code=code).first()
            if e is None:
                e = Employee(emp_code=code, name=name)
                db.session.add(e)
            e.role, e.vertical, e.is_active = role, vertical, True
            e.is_vertical_head, e.must_change_pw = is_head, False
            e.is_super_admin = False
            made[code] = e
        db.session.flush()
        for code in ('REP9',):
            made[code].vertical_head_id = made['HEAD9'].id

        acct = Company.query.filter_by(name='Scope Test Ltd').first()
        if acct is None:
            acct = Company(name='Scope Test Ltd', is_active=True)
            db.session.add(acct)
        acct.pic_emp_code = 'REP9'
        db.session.flush()

        ids = {'account': acct.id, 'lead': {}, 'opp': {}}
        for code in ('SCADM9', 'HEAD9', 'REP9', 'OUT9'):
            lead = Lead.query.filter_by(company=f'Scope {code} Ltd').first()
            if lead is None:
                lead = Lead(company=f'Scope {code} Ltd', source='manual',
                            stage='New')
                db.session.add(lead)
            lead.assigned_to = code
            lead.secondary_owner = None
            db.session.flush()
            ids['lead'][code] = lead.id

            opp = Opportunity.query.filter_by(
                opp_number=f'OPP-SCOPE-{code}').first()
            if opp is None:
                opp = Opportunity(opp_number=f'OPP-SCOPE-{code}')
                db.session.add(opp)
            opp.owner_emp_code = code
            opp.stage = 'RFQ'
            db.session.flush()
            ids['opp'][code] = opp.id

        db.session.commit()
        return ids


def _as(emp_code, data_scope, perms=()):
    """Set someone's matrix row and return their resolved scope."""
    set_profile(emp_code, data_scope, list(perms), actor='SCADM9')
    return scope.for_employee(emp_code)


def _lead_ids(sc):
    return {l.id for l in scope.leads(sc=sc).all()}


# ── the three scopes ─────────────────────────────────────────────────
def test_own_scope_sees_exactly_its_own_lead(world):
    """Not 'fewer'. Exactly one, and it is theirs."""
    with flask_app.app_context():
        sc = _as('REP9', DataScope.OWN)
        assert _lead_ids(sc) == {world['lead']['REP9']}


def test_vertical_scope_sees_the_vertical_and_nothing_beyond_it(world):
    with flask_app.app_context():
        sc = _as('HEAD9', DataScope.VERTICAL)
        seen = _lead_ids(sc)
        assert world['lead']['HEAD9'] in seen
        assert world['lead']['REP9'] in seen
        # the whole point: someone in another vertical is not included
        assert world['lead']['OUT9'] not in seen


def test_all_scope_is_unrestricted(world):
    with flask_app.app_context():
        sc = _as('SCADM9', DataScope.ALL)
        assert sc.unrestricted is True
        seen = _lead_ids(sc)
        for code in ('SCADM9', 'HEAD9', 'REP9', 'OUT9'):
            assert world['lead'][code] in seen


def test_the_matrix_beats_the_role(world):
    """The decision Phase 0 had to make, asserted.

    SCADM9 has role='admin'. Narrowed to own-records in the matrix, the
    matrix wins — otherwise the Access screen would be showing a setting
    that is not enforced, which is the defect this module exists to end.
    """
    with flask_app.app_context():
        sc = _as('SCADM9', DataScope.OWN)
        assert sc.unrestricted is False
        assert _lead_ids(sc) == {world['lead']['SCADM9']}
        # restore, so later tests are not reading a narrowed admin
        _as('SCADM9', DataScope.ALL)


def test_role_still_seeds_someone_nobody_configured(world):
    """Matrix authoritative does not mean matrix mandatory. An employee
    with no profile row keeps the sensible role-derived default."""
    with flask_app.app_context():
        from app.models.access import AccessProfile
        AccessProfile.query.filter_by(emp_code='OUT9').delete()
        db.session.commit()
        sc = scope.for_employee('OUT9')
        assert sc.data_scope == DataScope.OWN
        assert _lead_ids(sc) == {world['lead']['OUT9']}


# ── failing closed ───────────────────────────────────────────────────
def test_an_unknown_caller_sees_nothing(world):
    with flask_app.app_context():
        sc = scope.for_employee(None)
        assert sc.unrestricted is False
        assert _lead_ids(sc) == set()


def test_an_authenticated_stranger_sees_nothing(world):
    """Signed in, but not in the employee master."""
    with flask_app.app_context():
        sc = scope.for_employee('NOSUCHPERSON')
        assert _lead_ids(sc) == set()


def test_an_empty_code_set_shows_nothing_not_everything(world):
    """The bug that would matter most: an empty restriction read as
    'no restriction'. `filter(False)`, never a skipped filter."""
    with flask_app.app_context():
        sc = scope.Scope('X', '', set(), set(), DataScope.OWN)
        assert scope.leads(sc=sc).count() == 0
        assert scope.opportunities(sc=sc).count() == 0
        assert scope.companies(sc=sc).count() == 0


# ── every entity obeys the same boundary ─────────────────────────────
def test_opportunities_follow_the_same_scope(world):
    with flask_app.app_context():
        sc = _as('REP9', DataScope.OWN)
        ids = {o.id for o in scope.opportunities(sc=sc).all()}
        assert ids == {world['opp']['REP9']}


def test_activities_inherit_the_lead_boundary(world):
    """Child rows must not be a way around the parent's scope."""
    from app import LeadActivity

    with flask_app.app_context():
        act = LeadActivity(lead_id=world['lead']['OUT9'], kind='call',
                           subject='outside the vertical')
        db.session.add(act)
        db.session.commit()
        try:
            sc = _as('REP9', DataScope.OWN)
            assert act.id not in {a.id for a in scope.activities(sc=sc).all()}
        finally:
            db.session.delete(act)
            db.session.commit()


def test_a_secondary_pic_can_see_the_lead_they_monitor(world):
    """WP5 made the secondary a monitor. A monitor who cannot open the
    record cannot monitor it."""
    with flask_app.app_context():
        lead = db.session.get(Lead, world['lead']['OUT9'])
        lead.secondary_owner = 'REP9'
        db.session.commit()
        try:
            sc = _as('REP9', DataScope.OWN)
            assert world['lead']['OUT9'] in _lead_ids(sc)
        finally:
            lead.secondary_owner = None
            db.session.commit()


# ── per-record checks agree with the queries ─────────────────────────
def test_may_view_agrees_with_the_query(world):
    """Two ways to ask the same question must not disagree — that
    disagreement is exactly what Phase 0 exists to remove."""
    with flask_app.app_context():
        sc = _as('REP9', DataScope.OWN)
        visible = _lead_ids(sc)
        for code in ('SCADM9', 'HEAD9', 'REP9', 'OUT9'):
            lead = db.session.get(Lead, world['lead'][code])
            assert scope.may_view(lead, sc=sc) == (lead.id in visible), code


def test_may_view_refuses_an_object_with_no_owner_column(world):
    """Unknown shape, restricted viewer: no. The safe direction."""
    with flask_app.app_context():
        sc = _as('REP9', DataScope.OWN)
        assert scope.may_view(object(), sc=sc) is False
        assert scope.may_view(None, sc=sc) is False


# ── permissions travel with the scope ────────────────────────────────
def test_permissions_come_from_the_matrix(world):
    with flask_app.app_context():
        sc = _as('REP9', DataScope.OWN, ['module.quotes'])
        assert sc.can('module.quotes') is True
        assert sc.can('reports.accounts') is False


def test_scope_and_entitlement_are_independent(world):
    """§2.3's two settings. A vertical head may hold every report and
    still be scoped to their own vertical — the combination the brief
    calls out, and the one a single 'role' cannot express."""
    with flask_app.app_context():
        sc = _as('HEAD9', DataScope.VERTICAL,
                 ['reports.action', 'reports.accounts', 'reports.competitor'])
        assert sc.can('reports.accounts') is True
        assert sc.unrestricted is False
        assert world['lead']['OUT9'] not in _lead_ids(sc)
