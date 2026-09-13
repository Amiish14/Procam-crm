"""
§10 / Phase 5 — write actions, which must stay off and stay safe.

Two things to prove. That they are genuinely disabled, so deploying
this cannot let the Copilot change anything. And that when somebody
does enable them, a change still cannot happen in one step, cannot
touch a record outside the caller's scope, and cannot be replayed
against a different one.
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'ActionTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'actions.db')
os.environ.pop('PROCAM_AI_ACTIONS', None)

from app import app as flask_app, db, Employee, Lead        # noqa: E402
from app.access import scope as scope_mod                   # noqa: E402
from app.access.service import set_profile                  # noqa: E402
from app.copilot import actions                             # noqa: E402
from app.models.access import DataScope                     # noqa: E402

flask_app.config['WTF_CSRF_ENABLED'] = False


@pytest.fixture(scope='module')
def world():
    with flask_app.app_context():
        db.create_all()
        for code, role in (('ACADM', 'admin'), ('ACREP', 'user'),
                           ('ACOUT', 'user')):
            e = Employee.query.filter_by(emp_code=code).first()
            if e is None:
                e = Employee(emp_code=code, name=code)
                db.session.add(e)
            e.role, e.is_active, e.must_change_pw = role, True, False
            e.vertical, e.is_super_admin = 'All', False
        db.session.commit()

        ids = {}
        for code, tag in (('ACREP', 'mine'), ('ACOUT', 'theirs')):
            name = f'ActCo {code}'
            l = Lead.query.filter_by(company=name).first()
            if l is None:
                l = Lead(company=name, source='manual')
                db.session.add(l)
            l.assigned_to, l.stage = code, 'New'
            db.session.flush()
            ids[tag] = l.id
        set_profile('ACREP', DataScope.OWN, [], actor='ACADM')
        set_profile('ACADM', DataScope.ALL, ['admin.access'], actor='ACADM')
        db.session.commit()
        return ids


def _sc(code):
    with flask_app.app_context():
        return scope_mod.for_employee(code)


# ── off by default ───────────────────────────────────────────────────
def test_actions_are_off_unless_switched_on(world):
    assert actions.enabled() is False


def test_proposing_is_refused_while_off(world):
    with flask_app.app_context():
        p, err = actions.propose('create_activity', _sc('ACREP'),
                                 {'lead_id': world['mine'], 'note': 'hi'})
        assert p is None
        assert 'read-only' in err


def test_committing_is_refused_while_off(world):
    with flask_app.app_context():
        r, err = actions.commit('create_activity', _sc('ACREP'),
                                {'lead_id': world['mine'], 'note': 'hi'},
                                'anything')
        assert r is None and 'switched off' in err


def test_nothing_is_written_while_off(world):
    from app import LeadActivity

    with flask_app.app_context():
        before = LeadActivity.query.count()
        actions.commit('create_activity', _sc('ACREP'),
                       {'lead_id': world['mine'], 'note': 'hi'}, 'x')
        assert LeadActivity.query.count() == before


# ── with them on ─────────────────────────────────────────────────────
@pytest.fixture()
def on(monkeypatch):
    monkeypatch.setenv('PROCAM_AI_ACTIONS', 'on')
    assert actions.enabled() is True


def test_a_proposal_writes_nothing(world, on):
    from app import LeadActivity

    with flask_app.app_context():
        before = LeadActivity.query.count()
        p, err = actions.propose(
            'create_activity', _sc('ACREP'),
            {'lead_id': world['mine'], 'note': 'called the buyer'})
        assert err is None and p.token
        assert 'called the buyer' in p.summary
        assert LeadActivity.query.count() == before


def test_a_proposal_then_a_commit_applies_it(world, on):
    from app import LeadActivity

    with flask_app.app_context():
        params = {'lead_id': world['mine'], 'note': 'spoke to the buyer'}
        p, err = actions.propose('create_activity', _sc('ACREP'), params)
        assert err is None
        r, err = actions.commit('create_activity', _sc('ACREP'), params,
                                p.token, actor='ACREP')
        assert err is None, err
        assert LeadActivity.query.filter_by(
            lead_id=world['mine']).filter(
            LeadActivity.body == 'spoke to the buyer').count() == 1


def test_a_commit_without_a_token_is_refused(world, on):
    """There is no single-call form. A model that could act in one step
    could act by accident."""
    with flask_app.app_context():
        r, err = actions.commit(
            'create_activity', _sc('ACREP'),
            {'lead_id': world['mine'], 'note': 'sneaky'}, None)
        assert r is None and 'confirmation' in err.lower()


def test_a_token_cannot_be_replayed_against_another_record(world, on):
    """The signature covers the target, so editing the request to point
    at a different lead invalidates it."""
    with flask_app.app_context():
        p, _ = actions.propose(
            'create_activity', _sc('ACADM'),
            {'lead_id': world['mine'], 'note': 'note'})
        r, err = actions.commit(
            'create_activity', _sc('ACADM'),
            {'lead_id': world['theirs'], 'note': 'note'}, p.token)
        assert r is None
        assert 'does not match' in err


def test_a_token_cannot_be_replayed_with_different_content(world, on):
    with flask_app.app_context():
        p, _ = actions.propose(
            'create_activity', _sc('ACREP'),
            {'lead_id': world['mine'], 'note': 'harmless'})
        r, err = actions.commit(
            'create_activity', _sc('ACREP'),
            {'lead_id': world['mine'], 'note': 'something else entirely'},
            p.token)
        assert r is None and 'does not match' in err


def test_an_expired_confirmation_is_refused(world, on, monkeypatch):
    with flask_app.app_context():
        p, _ = actions.propose('create_activity', _sc('ACREP'),
                               {'lead_id': world['mine'], 'note': 'x'})
        monkeypatch.setattr(actions.time, 'time',
                            lambda: p.expires_at + 10)
        r, err = actions.commit('create_activity', _sc('ACREP'),
                                {'lead_id': world['mine'], 'note': 'x'},
                                p.token)
        assert r is None and 'expired' in err


# ── scope still holds ────────────────────────────────────────────────
def test_an_action_cannot_touch_a_lead_outside_the_scope(world, on):
    with flask_app.app_context():
        p, err = actions.propose(
            'create_activity', _sc('ACREP'),
            {'lead_id': world['theirs'], 'note': 'not mine'})
        assert p is None
        assert 'not one you can act on' in err


def test_reassignment_needs_its_permission(world, on):
    with flask_app.app_context():
        p, err = actions.propose(
            'reassign_lead', _sc('ACREP'),
            {'lead_id': world['mine'], 'primary': 'ACADM',
             'reason': 'Specialist required'})
        assert p is None and 'access' in err.lower()


def test_reassignment_requires_a_reason_from_the_list(world, on):
    with flask_app.app_context():
        p, err = actions.propose(
            'reassign_lead', _sc('ACADM'),
            {'lead_id': world['mine'], 'primary': 'ACREP',
             'reason': 'because'})
        assert p is None and 'Pick a reason' in err


def test_a_stage_must_be_a_real_stage(world, on):
    with flask_app.app_context():
        p, err = actions.propose(
            'update_stage', _sc('ACADM'),
            {'lead_id': world['mine'], 'stage': 'Fantastic'})
        assert p is None and 'Not a stage' in err


def test_reassignment_goes_through_the_existing_service(world, on):
    """So the history row, validation and notification cannot be
    skipped by coming in through the Copilot instead of the screen."""
    from app import LeadAssignmentHistory

    with flask_app.app_context():
        params = {'lead_id': world['mine'], 'primary': 'ACADM',
                  'reason': 'Specialist required'}
        p, err = actions.propose('reassign_lead', _sc('ACADM'), params)
        assert err is None, err
        r, err = actions.commit('reassign_lead', _sc('ACADM'), params,
                                p.token, actor='ACADM')
        assert err is None, err
        h = (LeadAssignmentHistory.query
             .filter_by(lead_id=world['mine'])
             .order_by(LeadAssignmentHistory.id.desc()).first())
        assert h is not None
        assert h.note == 'Specialist required'


def test_only_the_four_actions_the_brief_names_exist(world):
    """Adding a fifth should be a code change somebody reviews, not
    something that appears because a model asked for it."""
    assert set(actions.ACTIONS) == {
        'create_activity', 'create_reminder', 'reassign_lead', 'update_stage'}


def test_a_token_forged_with_a_guessable_key_is_refused(world, on,
                                                         monkeypatch):
    """Confirmation tokens used to be signed with the literal 'procam-ai'
    whenever SECRET_KEY was absent from the environment — a key anyone
    reading the source knew. They are now signed with the app's own key,
    which has no known fallback."""
    monkeypatch.delenv('SECRET_KEY', raising=False)
    params = {'lead_id': world['mine'], 'note': 'forged'}
    real = flask_app.secret_key
    try:
        with flask_app.app_context():
            flask_app.secret_key = 'procam-ai'          # attacker's guess
            forged, err = actions.propose('create_activity', _sc('ACADM'),
                                          params)
            assert err is None
            flask_app.secret_key = 'the-real-unguessable-key'
            r, err = actions.commit('create_activity', _sc('ACADM'), params,
                                    forged.token, actor='ACADM')
            assert r is None and 'does not match' in err
    finally:
        flask_app.secret_key = real
