"""Lead visibility allowances decided on 21 Oct 2026.

1. A vertical head sees every lead whose own vertical is theirs, even
   when it is owned by someone outside the vertical.
2. Anyone sees a lead on which they were given a task, even in another
   vertical.

The negative assertions are the point: an own-scope user must not gain
the vertical allowance, and a head must not see another vertical's
leads unless a task puts them there.

All of it is behind FEATURE_LEAD_ALLOWANCES, off by default. It
widens who can open a lead across the whole book, so it ships the
way every other new behaviour here does: on for a pilot first. The
fixture turns it on; `test_with_the_flag_off_...` holds that with it
off the scope is exactly the ownership test it has always been.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'VisTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'vis.db')

from app import app as flask_app, db, Employee, Lead          # noqa: E402
from app.access import scope                                  # noqa: E402
from app.access.service import set_profile                    # noqa: E402
from app.models.access import DataScope                       # noqa: E402
from app.models.email_admin import VerticalAlias, VerticalHead  # noqa: E402
from app.models.task_engine import TaskInstance               # noqa: E402
from app.services import vertical_heads                       # noqa: E402


_CREATED = []


def _allowances(state):
    import json

    from app.models.integration import AppSetting
    from app.services import flags
    key = 'flag.' + scope.ALLOWANCE_FLAG
    row = AppSetting.query.filter_by(key=key).first()
    if row is None:
        row = AppSetting(key=key)
        db.session.add(row)
    row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


def _upsert(model, keys, **values):
    """Get-or-create, so the fixture works on the shared suite database.
    Rows this module creates are removed at the end of it."""
    row = model.query.filter_by(**keys).first()
    if row is None:
        row = model(**keys)
        db.session.add(row)
        _CREATED.append(row)
    for k, v in values.items():
        setattr(row, k, v)
    return row


def _cleanup(extra=()):
    for row in list(extra) + list(reversed(_CREATED)):
        try:
            db.session.delete(row)
        except Exception:
            pass
    db.session.commit()
    _CREATED.clear()


@pytest.fixture(scope='module')
def world():
    with flask_app.app_context():
        db.create_all()
        people = [('VADM', 'All', False), ('VHT', 'Heavy Transport', True),
                  ('VPF', 'Project Freight', True),
                  ('VREP', 'Heavy Transport', False),
                  ('VWH', 'Warehousing', False)]
        for code, vertical, head in people:
            _upsert(Employee, {'emp_code': code}, name=code, role='user',
                    vertical=vertical, is_active=True,
                    is_vertical_head=head, must_change_pw=False,
                    is_super_admin=False, vertical_head_id=None)
        _upsert(VerticalHead, {'vertical': 'Project Freight'},
                head_emp_code='VPF', active=True)
        _upsert(VerticalHead, {'vertical': 'Heavy Transport'},
                head_emp_code='VHT', active=True)
        _upsert(VerticalAlias, {'alias': 'Freight Forwarding'},
                vertical='Project Freight')
        db.session.flush()

        def lead(name, owner, vertical):
            l = Lead(company=name, source='manual', stage='New',
                     assigned_to=owner, procam_vertical=vertical)
            db.session.add(l)
            db.session.flush()
            return l.id

        ids = {
            # owned outside HT, but the lead itself is HT
            'ht_by_wh': lead('Vis HT owned by WH', 'VWH', 'Heavy Transport'),
            # FF alias belongs to Project Freight
            'ff_by_wh': lead('Vis FF owned by WH', 'VWH', 'freight forwarding '),
            # a warehousing lead with no task on it
            'wh_plain': lead('Vis WH plain', 'VWH', 'Warehousing'),
            # a warehousing lead where the HT head is asked to act
            'wh_task': lead('Vis WH task', 'VWH', 'Warehousing'),
        }
        db.session.add(TaskInstance(task_key='vis.cost_line',
                                    entity_type='Lead',
                                    entity_id=ids['wh_task'],
                                    owner_user_id='VHT'))
        db.session.commit()
        vertical_heads.cache_clear()
        _allowances(True)
    yield ids
    with flask_app.app_context():
        from app.models.access import AccessProfile
        TaskInstance.query.filter(TaskInstance.task_key.like('vis.%')).delete(
            synchronize_session=False)
        Lead.query.filter(Lead.id.in_(list(ids.values()))).delete(
            synchronize_session=False)
        AccessProfile.query.filter(AccessProfile.emp_code.in_(
            ['VADM', 'VHT', 'VPF', 'VREP', 'VWH'])).delete(
            synchronize_session=False)
        db.session.commit()
        _cleanup()
        _allowances(False)
        vertical_heads.cache_clear()


def _ids(sc):
    return {l.id for l in scope.leads(sc=sc).all()}


def _as(code, data_scope):
    set_profile(code, data_scope, [], actor='VADM')
    vertical_heads.cache_clear()
    return scope.for_employee(code)


def test_head_sees_lead_of_own_vertical_owned_elsewhere(world):
    with flask_app.app_context():
        sc = _as('VHT', DataScope.VERTICAL)
        assert world['ht_by_wh'] in _ids(sc)
        lead = db.session.get(Lead, world['ht_by_wh'])
        assert scope.may_view(lead, sc=sc)


def test_alias_vertical_reaches_its_master_head(world):
    with flask_app.app_context():
        sc = _as('VPF', DataScope.VERTICAL)
        assert world['ff_by_wh'] in _ids(sc)
        assert world['ht_by_wh'] not in _ids(sc)


def test_head_does_not_see_other_vertical_without_task(world):
    with flask_app.app_context():
        sc = _as('VHT', DataScope.VERTICAL)
        assert world['wh_plain'] not in _ids(sc)
        assert not scope.may_view(db.session.get(Lead, world['wh_plain']),
                                  sc=sc)


def test_task_on_other_vertical_lead_makes_it_visible(world):
    with flask_app.app_context():
        sc = _as('VHT', DataScope.VERTICAL)
        assert world['wh_task'] in _ids(sc)
        assert scope.may_view(db.session.get(Lead, world['wh_task']), sc=sc)


def test_team_member_task_reaches_head(world):
    """A task given to someone in the head's scope reaches the head."""
    with flask_app.app_context():
        t = TaskInstance(task_key='vis.rep', entity_type='Lead',
                         entity_id=world['wh_plain'], owner_user_id='VREP')
        db.session.add(t)
        db.session.commit()
        try:
            sc = _as('VHT', DataScope.VERTICAL)
            assert world['wh_plain'] in _ids(sc)
        finally:
            db.session.delete(t)
            db.session.commit()


def test_own_scope_gets_no_vertical_allowance(world):
    """Leak check: an own-scope rep in HT must not see HT leads owned by
    others just because the lead is HT."""
    with flask_app.app_context():
        sc = _as('VREP', DataScope.OWN)
        assert world['ht_by_wh'] not in _ids(sc)
        assert not scope.may_view(db.session.get(Lead, world['ht_by_wh']),
                                  sc=sc)


def test_own_scope_task_holder_sees_only_that_lead(world):
    with flask_app.app_context():
        t = TaskInstance(task_key='vis.own', entity_type='Lead',
                         entity_id=world['wh_plain'], owner_user_id='VREP')
        db.session.add(t)
        db.session.commit()
        try:
            sc = _as('VREP', DataScope.OWN)
            assert _ids(sc) == {world['wh_plain']}
        finally:
            db.session.delete(t)
            db.session.commit()


def test_task_on_other_entity_type_grants_nothing(world):
    """A Quote task with the same numeric id is not a lead task."""
    with flask_app.app_context():
        t = TaskInstance(task_key='vis.q', entity_type='Quote',
                         entity_id=world['wh_plain'], owner_user_id='VREP')
        db.session.add(t)
        db.session.commit()
        try:
            sc = _as('VREP', DataScope.OWN)
            assert world['wh_plain'] not in _ids(sc)
        finally:
            db.session.delete(t)
            db.session.commit()


def test_a_scope_narrowed_to_one_person_gets_no_allowance(world):
    """A head looking at one rep's records (review, workbench for=)
    sees that rep's leads only — not the vertical, not task leads."""
    with flask_app.app_context():
        head = _as('VHT', DataScope.VERTICAL)
        narrowed = scope.Scope(head.emp_code, head.vertical, {'VWH'},
                               head.perms, head.data_scope)
        seen = {l.id for l in scope.leads(sc=narrowed).all()}
        assert {l.assigned_to for l in scope.leads(sc=narrowed).all()} \
            <= {'VWH'}
        assert not scope.may_view(db.session.get(Lead, world['wh_task']),
                                  sc=scope.Scope('VHT', head.vertical,
                                                 {'VHT'}, head.perms,
                                                 head.data_scope))
        assert world['wh_plain'] in seen


def test_with_the_flag_off_the_scope_is_what_it_always_was(world):
    """The whole of decision A2, switched off: a head sees the leads
    they own and the ones they monitor, and nothing else. This is
    what everybody outside the pilot gets."""
    with flask_app.app_context():
        _allowances(False)
        try:
            sc = _as('VHT', DataScope.VERTICAL)
            seen = _ids(sc)
            assert world['ht_by_wh'] not in seen
            assert world['ff_by_wh'] not in seen
            assert world['wh_task'] not in seen
            lead = db.session.get(Lead, world['ht_by_wh'])
            assert not scope.may_view(lead, sc=sc)
        finally:
            _allowances(True)


def test_the_flag_turns_both_allowances_on_together(world):
    """One switch, both allowances — a half-applied visibility rule
    is harder to reason about than either state."""
    with flask_app.app_context():
        _allowances(True)
        sc = _as('VHT', DataScope.VERTICAL)
        seen = _ids(sc)
        assert world['ht_by_wh'] in seen          # the vertical one
        assert world['wh_task'] in seen           # the task one
