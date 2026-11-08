"""Two tasks that could never close, and the sweep that clears them.

CRM-FINAL-CLOSEOUT-01 §10 — fault 3 of
docs/operations/RELEASE_2026_10_TASKS.md.

The fault is one shape twice over: a task that closes on exactly one
stage name, and a lead under no obligation to pass through it.
`lead.qualify` waits on `Profile Sent`, which `stage_map.ALIASES`
retired out of existence; `lead.rfq_review` waits on `Quoted`, which
a lead skips whenever it negotiates off a verbal or goes elsewhere.

So the tests that matter are the ones about leads that take the
unusual route — straight to Under Negotiation, straight to Lost —
because those are the leads whose tasks were immortal.
"""
import importlib
import importlib.util
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'closeevt-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'CloseEvt-123456789')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'closeevt.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db

from app import Lead                                        # noqa: E402
from app.models.audit import AuditEvent                     # noqa: E402
from app.models.task_engine import (TaskDefinition,         # noqa: E402
                                    TaskInstance,
                                    TaskInstanceStatus)
from app.services import stage_map                          # noqa: E402
from app.services import task_close_rules as rules          # noqa: E402
from app.services import task_engine                        # noqa: E402

SCRIPT = os.path.join(_ROOT, 'scripts',
                      '2026_11_08_task_close_events.py')
TAG = 'ZCloseEvt'


def _script():
    spec = importlib.util.spec_from_file_location('closeevt', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        yield
        _purge()


def _purge():
    lead_ids = [l.id for l in Lead.query.filter(
        Lead.company.like(TAG + '%')).all()]
    if lead_ids:
        TaskInstance.query.filter(
            TaskInstance.entity_type == 'Lead',
            TaskInstance.entity_id.in_(lead_ids)).delete(
                synchronize_session=False)
        Lead.query.filter(Lead.id.in_(lead_ids)).delete(
            synchronize_session=False)
    TaskDefinition.query.filter(
        TaskDefinition.task_key.in_(sorted(rules.AFTER))).delete(
            synchronize_session=False)
    AuditEvent.query.filter(
        AuditEvent.actor == 'FINAL-CLOSEOUT-01').delete(
            synchronize_session=False)
    db.session.commit()


def _definition(key, completion):
    row = TaskDefinition(task_key=key, title=key, module='leads',
                         allowed_roles=['Lead_Driver'],
                         start_state={'entity': 'Lead',
                                      'state': 'New'},
                         completion_state=completion,
                         is_active=True)
    db.session.add(row)
    db.session.commit()
    return row


def _lead(stage):
    lead = Lead(company=f'{TAG} Client', stage=stage, source='email',
                created_at=datetime.utcnow() - timedelta(days=30))
    db.session.add(lead)
    db.session.commit()
    return lead


def _task(key, lead):
    row = TaskInstance(task_key=key, entity_type='Lead',
                       entity_id=lead.id, owner_user_id='ZC_OWN',
                       status=TaskInstanceStatus.OPEN[0],
                       created_at=datetime.utcnow() - timedelta(days=25))
    db.session.add(row)
    db.session.commit()
    return row


# ═══════════════════════════════════════════════════════════════════
# the rules
# ═══════════════════════════════════════════════════════════════════
def test_the_stage_qualify_waited_on_no_longer_exists():
    """The whole reason it was immortal, asserted so the fix cannot
    be explained away."""
    assert rules.WAS['lead.qualify'] == 'Profile Sent'
    assert 'Profile Sent' not in stage_map.JOURNEY
    assert 'Profile Sent' in stage_map.ALIASES


def test_qualify_closes_on_anything_past_new():
    for stage in ('Call Done', 'Business Discussion', 'New Opportunity',
                  'RFQ Generated', 'Quoted', 'Under Negotiation',
                  'Won', 'Lost', 'On Hold', 'Not Interested'):
        assert rules.closes_now('lead.qualify', stage), stage


def test_qualify_stays_open_on_a_lead_still_on_new():
    assert not rules.closes_now('lead.qualify', 'New')


def test_rfq_review_closes_once_the_lead_moves_past_rfq():
    for stage in ('Quoted', 'Under Negotiation', 'Won', 'Lost',
                  'On Hold', 'Not Interested'):
        assert rules.closes_now('lead.rfq_review', stage), stage


def test_rfq_review_stays_open_at_and_before_rfq():
    for stage in ('New', 'Call Done', 'Business Discussion',
                  'New Opportunity', 'RFQ Generated'):
        assert not rules.closes_now('lead.rfq_review', stage), stage


def test_a_retired_stage_name_still_closes_it():
    """A lead nobody has saved since the rename. This is the exact
    failure: the stage moved and the task went on naming the old
    one."""
    assert rules.closes_now('lead.qualify', 'Visit Done')
    assert rules.closes_now('lead.qualify', 'Appointment')


def test_the_lists_cover_every_stage_so_a_new_one_cannot_be_missed():
    """A typed list is how this broke the first time. If a stage is
    added to the journey, this fails here rather than quietly making
    a task immortal again."""
    for key in rules.AFTER:
        covered = set(rules.closing_states(key))
        anchor = stage_map.JOURNEY.index(rules.AFTER[key])
        expected = set(stage_map.JOURNEY[anchor + 1:]) \
            | set(stage_map.TERMINAL)
        assert covered == expected, key


def test_no_stage_name_is_spelled_out_as_a_closing_state():
    """The lists are computed from stage_map, not typed.

    Asked of `closing_states` itself rather than of the whole module:
    `WAS` names 'Profile Sent' and 'Quoted' on purpose, because those
    are the rules being replaced and a check run shows the before as
    well as the after.
    """
    import inspect

    body = inspect.getsource(rules.closing_states)
    for stage in ('Quoted', 'Under Negotiation', 'Call Done',
                  'Business Discussion', 'New Opportunity', 'Lost',
                  'Won', 'On Hold', 'Not Interested'):
        assert f"'{stage}'" not in body, stage
    assert 'JOURNEY' in body and 'TERMINAL' in body


def test_the_anchors_are_real_journey_stages():
    """A typo in an anchor would silently produce an empty list, and
    an empty completion rule is an immortal task again."""
    for key, anchor in rules.AFTER.items():
        assert anchor in stage_map.JOURNEY, (key, anchor)
        assert rules.closing_states(key), key
        assert rules.completion_rule(key)['states']


def test_an_unknown_task_key_gets_no_rule_rather_than_a_wrong_one():
    assert rules.closing_states('lead.nonesuch') == ()
    assert rules.completion_rule('lead.nonesuch') is None
    assert rules.closes_now('lead.nonesuch', 'Won') is False


def test_the_outcome_is_completed_when_the_lead_advanced():
    status, why = rules.outcome_for('Quoted')
    assert status == TaskInstanceStatus.COMPLETED
    assert 'could not have' in why


def test_the_outcome_is_cancelled_when_the_lead_died():
    """Marking these Completed would record work nobody did."""
    for stage in ('Lost', 'On Hold', 'Not Interested'):
        status, why = rules.outcome_for(stage)
        assert status == TaskInstanceStatus.CANCELLED, stage
        assert 'never done' in why


def test_a_won_lead_counts_as_the_work_having_happened():
    status, _why = rules.outcome_for('Won')
    assert status == TaskInstanceStatus.COMPLETED


# ═══════════════════════════════════════════════════════════════════
# the engine
# ═══════════════════════════════════════════════════════════════════
def test_a_list_of_states_closes_the_task():
    with flask_app.app_context():
        _definition('lead.rfq_review',
                    rules.completion_rule('lead.rfq_review'))
        lead = _lead('RFQ Generated')
        row = _task('lead.rfq_review', lead)

        # The route a lead takes when the customer negotiates off a
        # verbal — it never visits Quoted, which is why this task
        # used to be immortal.
        lead.stage = 'Under Negotiation'
        db.session.commit()
        task_engine.on_state_change(lead, 'Lead', 'RFQ Generated',
                                    'Under Negotiation')
        db.session.commit()
        db.session.expire_all()
        assert TaskInstance.query.get(row.id).status == \
            TaskInstanceStatus.COMPLETED


def test_a_lost_lead_closes_it_too():
    with flask_app.app_context():
        _definition('lead.rfq_review',
                    rules.completion_rule('lead.rfq_review'))
        lead = _lead('RFQ Generated')
        row = _task('lead.rfq_review', lead)
        lead.stage = 'Lost'
        db.session.commit()
        task_engine.on_state_change(lead, 'Lead', 'RFQ Generated',
                                    'Lost')
        db.session.commit()
        db.session.expire_all()
        assert TaskInstance.query.get(row.id).status not in \
            TaskInstanceStatus.OPEN


def test_the_old_single_state_rule_is_what_left_it_open():
    """The bug, asserted, so the fix cannot be quietly reverted."""
    with flask_app.app_context():
        _definition('lead.rfq_review',
                    {'entity': 'Lead', 'state': 'Quoted'})
        lead = _lead('RFQ Generated')
        row = _task('lead.rfq_review', lead)
        task_engine.on_state_change(lead, 'Lead', 'RFQ Generated',
                                    'Under Negotiation')
        db.session.commit()
        db.session.expire_all()
        assert TaskInstance.query.get(row.id).status in \
            TaskInstanceStatus.OPEN


def test_a_retired_stage_name_closes_it_through_the_engine():
    with flask_app.app_context():
        _definition('lead.qualify',
                    rules.completion_rule('lead.qualify'))
        lead = _lead('New')
        row = _task('lead.qualify', lead)
        task_engine.on_state_change(lead, 'Lead', 'New', 'Visit Done')
        db.session.commit()
        db.session.expire_all()
        assert TaskInstance.query.get(row.id).status == \
            TaskInstanceStatus.COMPLETED


def test_aliases_are_not_folded_when_creating_a_task():
    """Folding on the closing side can only close more tasks. Folding
    on the creating side would make more, which nobody asked for."""
    assert task_engine._state_matches(
        {'entity': 'Lead', 'state': 'Business Discussion'},
        'Lead', 'Visit Done') is False
    assert task_engine._state_matches(
        {'entity': 'Lead', 'state': 'Business Discussion'},
        'Lead', 'Visit Done', fold_aliases=True) is True


def test_the_wildcard_and_created_rules_still_work():
    assert task_engine._state_matches(
        {'entity': 'Lead', 'state': '*'}, 'Lead', 'anything')
    assert task_engine._state_matches(
        {'entity': 'Lead', 'state': '__created__'},
        'Lead', '__created__')
    assert not task_engine._state_matches(
        {'entity': 'Opportunity', 'state': '*'}, 'Lead', 'New')


def test_an_empty_rule_matches_nothing():
    assert not task_engine._state_matches(None, 'Lead', 'New')
    assert not task_engine._state_matches({}, 'Lead', 'New')
    assert not task_engine._state_matches(
        {'entity': 'Lead'}, 'Lead', 'New')


# ═══════════════════════════════════════════════════════════════════
# the script
# ═══════════════════════════════════════════════════════════════════
def test_the_check_run_changes_nothing():
    with flask_app.app_context():
        mod = _script()
        _definition('lead.qualify',
                    {'entity': 'Lead', 'state': 'Profile Sent'})
        lead = _lead('Won')
        row = _task('lead.qualify', lead)
        assert mod.main(['--check']) == 0
        db.session.expire_all()
        assert TaskInstance.query.get(row.id).status in \
            TaskInstanceStatus.OPEN
        assert TaskDefinition.query.filter_by(
            task_key='lead.qualify').first().completion_state == \
            {'entity': 'Lead', 'state': 'Profile Sent'}


def test_the_apply_rewrites_the_rule_and_closes_the_backlog():
    """Both halves in one run. The rule alone leaves the existing
    backlog open forever — on_state_change only fires on the next
    transition, and these leads have had theirs."""
    with flask_app.app_context():
        mod = _script()
        _definition('lead.qualify',
                    {'entity': 'Lead', 'state': 'Profile Sent'})
        _definition('lead.rfq_review',
                    {'entity': 'Lead', 'state': 'Quoted'})
        done = _task('lead.qualify', _lead('Won'))
        dead = _task('lead.rfq_review', _lead('Lost'))
        still = _task('lead.rfq_review', _lead('RFQ Generated'))

        assert mod.main(['--apply']) == 0
        db.session.expire_all()

        assert TaskInstance.query.get(done.id).status == \
            TaskInstanceStatus.COMPLETED
        assert TaskInstance.query.get(dead.id).status == \
            TaskInstanceStatus.CANCELLED
        assert TaskInstance.query.get(still.id).status in \
            TaskInstanceStatus.OPEN, (
                'a lead still on RFQ Generated has real open work')

        for key in ('lead.qualify', 'lead.rfq_review'):
            row = TaskDefinition.query.filter_by(task_key=key).first()
            assert 'states' in row.completion_state
            assert row.completion_state == rules.completion_rule(key)


def test_each_closed_row_says_why_in_its_notes_and_the_audit():
    with flask_app.app_context():
        mod = _script()
        _definition('lead.qualify',
                    {'entity': 'Lead', 'state': 'Profile Sent'})
        row = _task('lead.qualify', _lead('Lost'))
        rid = row.id
        mod.main(['--apply'])
        db.session.expire_all()
        closed = TaskInstance.query.get(rid)
        assert 'FINAL-CLOSEOUT-01' in (closed.notes or '')
        assert 'never done' in (closed.notes or '')
        events = AuditEvent.query.filter_by(
            action='task.close_event_backfilled',
            entity_id=str(rid)).all()
        assert len(events) == 1
        assert events[0].new_value['lead_stage'] == 'Lost'


def test_the_definition_change_is_audited():
    with flask_app.app_context():
        mod = _script()
        row = _definition('lead.qualify',
                          {'entity': 'Lead', 'state': 'Profile Sent'})
        rid = row.id
        mod.main(['--apply'])
        events = AuditEvent.query.filter_by(
            action='task_definition.completion_changed',
            entity_id=str(rid)).all()
        assert len(events) == 1
        assert events[0].old_value['completion_state'] == \
            {'entity': 'Lead', 'state': 'Profile Sent'}


def test_running_it_twice_changes_nothing_the_second_time():
    with flask_app.app_context():
        mod = _script()
        _definition('lead.qualify',
                    {'entity': 'Lead', 'state': 'Profile Sent'})
        _task('lead.qualify', _lead('Won'))
        mod.main(['--apply'])
        db.session.expire_all()
        defs = mod.definition_plan()
        assert not any(d['changes'] for d in defs)
        plan, _staying = mod.instance_plan()
        assert plan == []


def test_no_other_task_definition_is_touched():
    with flask_app.app_context():
        mod = _script()
        _definition('lead.qualify',
                    {'entity': 'Lead', 'state': 'Profile Sent'})
        other = TaskDefinition.query.filter(
            TaskDefinition.task_key.notin_(sorted(rules.AFTER))).all()
        before = {d.task_key: d.completion_state for d in other}
        mod.main(['--apply'])
        db.session.expire_all()
        after = {d.task_key: d.completion_state
                 for d in TaskDefinition.query.filter(
                     TaskDefinition.task_key.notin_(
                         sorted(rules.AFTER))).all()}
        assert after == before


def test_the_other_two_immortals_are_named_and_not_touched():
    """`deal.lost.debrief` and `competitor.log` need a decision about
    what "done" means, not a longer list of stages."""
    mod = _script()
    assert set(mod.LEFT_FOR_A_DECISION) == {'deal.lost.debrief',
                                            'competitor.log'}
    assert set(mod.LEFT_FOR_A_DECISION) & set(rules.AFTER) == set()
