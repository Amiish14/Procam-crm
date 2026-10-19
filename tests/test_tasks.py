"""
The task engine: why nothing ever closed, and the four fixes.

The investigation (CRM-TASKS-01) found five compounding faults, none
of which was "closing is broken". Closing was simply unreachable:

  1. No route completes a task by hand. /my-work/tasks and
     /my-work.json are both GET.
  2. The ordinary lead edit form sets `stage` and never called the
     task engine at all — only the dedicated advance and decision
     endpoints did, and the form is how most people change a stage.
  3. Two definitions could never close: deal.lost.debrief has no
     completion_state, and competitor.log waits for an Opportunity
     state ('Competitor Logged') that is not one of the six.
  4. Every '.escalated' twin was immortal: its task_key matches no
     TaskDefinition, and tasks close only by matching one.
  5. No definition names Won / Lost / On Hold / Not Interested, so a
     lead reaching the end of its life closed nothing behind it.

Faults 4 and 5 are fixed here. 1, 2 and 3 are reported, because the
instruction was to find the cause before changing anything.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'tasks-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'TasksTest123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'tasks.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Lead, Employee = _main.Lead, _main.Employee
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.access import scope as sc_mod                    # noqa: E402
from app.models.access import AccessProfile, DataScope    # noqa: E402
from app.models.audit import AuditEvent                   # noqa: E402
from app.models.task_engine import (TaskDefinition,       # noqa: E402
                                    TaskInstance,
                                    TaskInstanceStatus)
from app.services import task_autoclose                   # noqa: E402
from app.workbench import filters as wf                   # noqa: E402
from app.workbench import service as wb                   # noqa: E402

import scripts.merge_escalated_tasks as merger            # noqa: E402
import scripts.task_backlog_report as backlog             # noqa: E402

ADMIN, REP = 'TKADMIN', 'TKREP'
TAG = 'TK '
TODAY = date.today()


def _emp(code, name, scope=DataScope.OWN, admin=False):
    e = (Employee.query.filter_by(emp_code=code).first()
         or Employee(emp_code=code))
    e.name, e.is_active, e.must_change_pw = name, True, False
    e.role = 'admin' if admin else 'user'
    e.is_super_admin, e.is_vertical_head, e.session_version = admin, False, 0
    e.vertical = 'Freight Forwarding'
    e.email = f'{code.lower()}@procamgroup.in'
    db.session.add(e)
    db.session.flush()
    AccessProfile.query.filter_by(emp_code=code).delete()
    db.session.add(AccessProfile(emp_code=code, data_scope=scope, perms=[]))
    return e


def _lead(name, stage='New', **kw):
    l = Lead(company=TAG + name, assigned_to=REP, assigned_name=REP,
             stage=stage, procam_vertical='Freight Forwarding',
             created_at=kw.pop('created_at',
                               datetime.utcnow() - timedelta(days=10)))
    for k, v in kw.items():
        setattr(l, k, v)
    db.session.add(l)
    return l


def _task(key, lead, **kw):
    t = TaskInstance(
        task_key=key, entity_type='Lead', entity_id=lead.id,
        entity_display=lead.company,
        owner_user_id=kw.pop('owner', REP),
        owner_role=kw.pop('role', None),
        status=kw.pop('status', TaskInstanceStatus.PENDING),
        priority=kw.pop('priority', 3),
        created_at=kw.pop('created_at',
                          datetime.utcnow() - timedelta(days=10)),
        due_at=kw.pop('due_at', datetime.utcnow() - timedelta(days=5)))
    for k, v in kw.items():
        setattr(t, k, v)
    db.session.add(t)
    return t


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        old = [l.id for l in Lead.query.filter(Lead.company.like(TAG + '%'))]
        if old:
            stale = [str(t.id) for t in TaskInstance.query.filter(
                TaskInstance.entity_id.in_(old)).all()]
            TaskInstance.query.filter(
                TaskInstance.entity_id.in_(old)).delete(
                    synchronize_session=False)
            Lead.query.filter(Lead.id.in_(old)).delete(
                synchronize_session=False)
            # SQLite reuses rowids, so the next fixture's tasks inherit
            # the deleted ones' ids — and their audit rows with them.
            # A test counting "the audit rows for this task" then reads
            # the previous test's as well.
            if stale:
                AuditEvent.query.filter(
                    AuditEvent.entity_type == 'task_instance',
                    AuditEvent.entity_id.in_(stale)).delete(
                        synchronize_session=False)
        _emp(ADMIN, 'Tasks Admin', scope=DataScope.ALL, admin=True)
        _emp(REP, 'Tasks Rep')
        db.session.flush()

        live = _lead('Live Ltd', stage='RFQ Generated')
        won = _lead('Won Ltd', stage='Won')
        ancient = _lead('Ancient Ltd', stage='Quoted',
                        created_at=datetime.utcnow() - timedelta(days=200))
        db.session.flush()

        t = {
            'live': _task('lead.rfq_review', live),
            'won_a': _task('lead.rfq_review', won),
            'won_b': _task('deal.negotiation', won),
            'twin': _task('lead.rfq_review.escalated', won,
                          owner=None, role='Vertical_Head'),
            'old': _task('lead.qualify', ancient,
                         created_at=datetime.utcnow() - timedelta(days=200)),
        }
        db.session.commit()
        wb.hygiene_cache_clear()
        yield {'leads': {'live': live.id, 'won': won.id,
                         'ancient': ancient.id},
               'tasks': {k: v.id for k, v in t.items()}}


def _client(code):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role='user', sv=0)
    return c


# ── the investigation, pinned so it cannot silently come back ────────
def test_no_route_completes_a_task_by_hand():
    """Fault 1. If somebody adds one, this fails and the finding is
    stale — which is the point: the report said there is no way to
    finish a task, and that must stop being true deliberately."""
    posts = [r for r in flask_app.url_map.iter_rules()
             if 'task' in r.rule.lower()
             and {'POST', 'PATCH', 'PUT'} & set(r.methods or ())]
    assert not posts, \
        f'a task-writing route now exists: {[r.rule for r in posts]}'


def test_an_escalated_twin_matches_no_definition(world):
    """Fault 4, stated as the rule that made twins immortal."""
    with flask_app.app_context():
        keys = {d.task_key for d in TaskDefinition.query.all()}
    assert not any(k.endswith('.escalated') for k in keys), \
        'a .escalated definition exists, so the twins were closable'


# ── item 5: closing a lead closes its tasks ──────────────────────────
def test_terminal_stages_are_the_four(world):
    assert task_autoclose.TERMINAL_STAGES == (
        'Won', 'Lost', 'On Hold', 'Not Interested')
    assert task_autoclose.is_terminal('Won')
    assert not task_autoclose.is_terminal('Quoted')
    assert not task_autoclose.is_terminal(None)


def test_closing_a_lead_from_the_edit_form_closes_its_tasks(world):
    lid = world['leads']['live']
    with flask_app.app_context():
        assert len(task_autoclose.open_tasks_for(lid)) == 1
    r = _client(ADMIN).put(f'/api/leads/{lid}', json={'stage': 'Lost'})
    assert r.status_code == 200
    with flask_app.app_context():
        assert task_autoclose.open_tasks_for(lid) == []
        row = db.session.get(TaskInstance, world['tasks']['live'])
        # Cancelled, not Completed: nobody did it.
        assert row.status == TaskInstanceStatus.CANCELLED
        assert row.completed_at is not None
        assert 'Lost' in (row.notes or '')


def test_a_lead_that_is_not_closed_keeps_its_tasks(world):
    lid = world['leads']['live']
    _client(ADMIN).put(f'/api/leads/{lid}', json={'stage': 'Quoted'})
    with flask_app.app_context():
        assert len(task_autoclose.open_tasks_for(lid)) == 1


def test_closing_is_audited_per_task(world):
    lid = world['leads']['live']
    _client(ADMIN).put(f'/api/leads/{lid}', json={'stage': 'Won'})
    with flask_app.app_context():
        rows = AuditEvent.query.filter(
            AuditEvent.action == 'task.closed_with_lead',
            AuditEvent.entity_id == str(world['tasks']['live'])).all()
    assert len(rows) == 1
    assert rows[0].new_value['stage'] == 'Won'


def test_close_for_lead_does_not_commit(world):
    """It runs in the caller's transaction. A lead marked Won whose
    tasks stayed open is the state this exists to prevent, so the two
    have to be able to roll back together."""
    with flask_app.app_context():
        lead = db.session.get(Lead, world['leads']['won'])
        n = task_autoclose.close_for_lead(lead, actor=ADMIN)
        assert n >= 1
        db.session.rollback()
        assert len(task_autoclose.open_tasks_for(lead.id)) == n


def test_closing_twice_closes_nothing_the_second_time(world):
    with flask_app.app_context():
        lead = db.session.get(Lead, world['leads']['won'])
        first = task_autoclose.close_for_lead(lead, actor=ADMIN)
        db.session.commit()
        assert first >= 1
        assert task_autoclose.close_for_lead(lead, actor=ADMIN) == 0


# ── item 2: the twins merge into their originals ─────────────────────
def test_the_merge_finds_each_twin_its_original(world):
    with flask_app.app_context():
        matched, orphans = merger.pairs()
        twin_ids = {t.id for t, _o in matched}
        assert world['tasks']['twin'] in twin_ids
        pair = [(t, o) for t, o in matched
                if t.id == world['tasks']['twin']][0]
        assert pair[1].id == world['tasks']['won_a']
        assert orphans == []


def test_merging_moves_the_facts_and_removes_the_row(world):
    with flask_app.app_context():
        matched, _ = merger.pairs()
        for twin, original in matched:
            original.escalated = True
            original.escalated_to = original.escalated_to or twin.owner_role
            original.escalated_at = original.escalated_at or twin.created_at
            db.session.delete(twin)
        db.session.commit()

        original = db.session.get(TaskInstance, world['tasks']['won_a'])
        assert original is not None, 'the original must survive'
        assert original.escalated is True
        assert original.escalated_to == 'Vertical_Head'
        assert original.escalated_at is not None
        assert db.session.get(TaskInstance, world['tasks']['twin']) is None
        # And nothing else was touched.
        assert db.session.get(TaskInstance, world['tasks']['live']) is not None


def test_an_orphan_twin_is_left_alone(world):
    with flask_app.app_context():
        lead = db.session.get(Lead, world['leads']['live'])
        orphan = _task('quote.followup.escalated', lead,
                       owner=None, role='Vertical_Head')
        db.session.commit()
        matched, orphans = merger.pairs()
        assert orphan.id in {t.id for t in orphans}
        assert orphan.id not in {t.id for t, _o in matched}


def test_the_sweeper_escalates_in_place_and_creates_nothing():
    """The sweeper used to write a second row. Reading the source is
    the only way to assert it does not: running it needs a scheduler
    and a clock."""
    with open(os.path.join(_ROOT, 'scripts',
                           'task_sla_sweep.py')) as fh:
        src = fh.read()
    # The prose explains what it stopped doing, so look at the code:
    # a key built by concatenation, and a row added.
    code = '\n'.join(line for line in src.splitlines()
                     if not line.lstrip().startswith('#'))
    code = code.split('"""', 2)[-1]          # drop the module docstring
    assert "+ '.escalated'" not in code, \
        'the sweeper still builds a .escalated key'
    assert 'db.session.add(TaskInstance(' not in code, \
        'the sweeper still creates a task row'
    assert 'inst.escalated = True' in code


# ── item 3: Overdue split into leads and tasks ───────────────────────
def test_overdue_is_split_into_two_tiles():
    assert wf.get('overdue_leads').tile == 'Overdue leads'
    assert wf.get('overdue_tasks').tile == 'Overdue tasks'
    assert 'overdue_leads' in wf.OVERVIEW_KEYS
    assert 'overdue_tasks' in wf.OVERVIEW_KEYS
    # The combined key survives for older links and shows nowhere.
    combined = wf.get('overdue')
    assert combined is not None
    assert not (combined.tile or combined.chip or combined.overview)
    assert 'overdue' in wf.LEGACY_KEYS


def test_the_two_halves_add_up_to_the_whole(world):
    with flask_app.app_context():
        c = wb.board(sc_mod.for_employee(ADMIN), per_page=1)['counts']
    assert c['overdue_leads'] + c['overdue_tasks'] == c['overdue'], \
        'the split must partition the old number, not reinvent it'


def test_each_half_filters_to_its_own_kind(world):
    with flask_app.app_context():
        sc = sc_mod.for_employee(ADMIN)
        leads = wb.board(sc, filter_key='overdue_leads', per_page=200)
        tasks = wb.board(sc, filter_key='overdue_tasks', per_page=200)
    assert all(i['kind'] != 'task' for i in leads['items'])
    assert all(i['kind'] == 'task' for i in tasks['items'])
    assert leads['total'] == len([i for i in leads['items']])


def test_the_overview_shows_both_halves(world):
    body = _client(ADMIN).get('/api/my-work').get_json()
    keys = [t['key'] for t in body['tiles']]
    assert 'overdue_leads' in keys and 'overdue_tasks' in keys
    assert 'overdue' not in keys
    p = body['pending']
    assert p['overdue'] == p['overdue_leads'] + p['overdue_tasks']


# ── item 4: the backlog report ───────────────────────────────────────
def test_the_backlog_report_classifies_by_the_two_rules(world):
    with flask_app.app_context():
        picked, rows, _leads = backlog.classify()
    # On a Won lead → "lead closed", whatever its age.
    assert picked.get(world['tasks']['won_a']) == 'lead_closed'
    assert picked.get(world['tasks']['won_b']) == 'lead_closed'
    # Old task on an old but still-live lead → "expired".
    assert picked.get(world['tasks']['old']) == 'expired'
    # Young task on a live lead → neither.
    assert world['tasks']['live'] not in picked


def test_lead_closed_wins_where_both_rules_apply(world):
    with flask_app.app_context():
        lead = db.session.get(Lead, world['leads']['won'])
        lead.created_at = datetime.utcnow() - timedelta(days=300)
        aged = _task('lead.qualify', lead,
                     created_at=datetime.utcnow() - timedelta(days=300))
        db.session.commit()
        picked, _rows, _l = backlog.classify()
    assert picked[aged.id] == 'lead_closed', \
        'the truer reason, and the one a reader can check'


def test_the_backlog_report_refuses_to_apply_without_approval(world):
    import subprocess
    env = dict(os.environ, DATABASE_URL=str(db.engine.url),
               PYTHONPATH=_ROOT)
    with flask_app.app_context():
        env['DATABASE_URL'] = str(db.engine.url)
    out = subprocess.run(
        [sys.executable, os.path.join(_ROOT, 'scripts',
                                      'task_backlog_report.py'), '--apply'],
        capture_output=True, text=True, env=env, cwd=_ROOT)
    assert out.returncode == 2, out.stdout[-2000:]
    assert 'refused' in out.stdout
