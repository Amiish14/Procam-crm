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
def test_a_route_now_completes_a_task_by_hand():
    """Fault 1, fixed. This test used to assert the opposite — that
    no such route existed — and it was the record of the finding.
    It is inverted rather than deleted, so the history is legible:
    the absence was deliberate once, and stopped being so on purpose.
    """
    posts = [r.rule for r in flask_app.url_map.iter_rules()
             if 'task' in r.rule.lower()
             and 'POST' in set(r.methods or ())]
    assert '/api/tasks/<int:tid>/done' in posts, \
        f'no way to finish a task; task POST routes are {posts}'


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


def test_apply_with_approval_actually_closes_them(world):
    """The gap this fills: the script reported, refused without
    approval, and — with approval — printed "DRY RUN ONLY" and wrote
    nothing, because the write path was never built. A refusal test
    alone could not see that, because refusing was all it ever did.
    """
    with flask_app.app_context():
        picked, _rows, _l = backlog.classify()
        assert picked, 'fixture must give it something to close'
        targets = dict(picked)

        import subprocess
        out = subprocess.run(
            [sys.executable,
             os.path.join(_ROOT, 'scripts', 'task_backlog_report.py'),
             '--apply', '--i-have-approval'],
            capture_output=True, text=True, cwd=_ROOT,
            env=dict(os.environ, DATABASE_URL=str(db.engine.url),
                     PYTHONPATH=_ROOT))
        assert out.returncode == 0, out.stdout[-3000:] + out.stderr[-2000:]
        assert 'MODE:     APPLY' in out.stdout
        assert 'DRY RUN ONLY' not in out.stdout

        db.session.expire_all()
        for tid, rule in targets.items():
            row = db.session.get(TaskInstance, tid)
            assert row.status == TaskInstanceStatus.CANCELLED, \
                f'#{tid} ({rule}) was reported and not closed'
            assert row.completed_at is not None
            assert backlog.NOTES[rule][:40] in (row.notes or '')
            rows = AuditEvent.query.filter(
                AuditEvent.action == 'task.backlog_closed',
                AuditEvent.entity_id == str(tid)).all()
            assert len(rows) == 1
            assert rows[0].new_value['rule'] == rule

        # And nothing it did not name.
        live = db.session.get(TaskInstance, world['tasks']['live'])
        assert live.status in TaskInstanceStatus.OPEN

        # Twice closes nothing more.
        again, _r, _l2 = backlog.classify()
        assert again == {}


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


# ── fault 1: the Done button ─────────────────────────────────────────
def test_the_owner_can_finish_their_own_task(world):
    tid = world['tasks']['live']
    r = _client(REP).post(f'/api/tasks/{tid}/done',
                          json={'note': 'rang them, quote agreed'})
    assert r.status_code == 200 and r.get_json()['ok']
    with flask_app.app_context():
        row = db.session.get(TaskInstance, tid)
        # Completed, not Cancelled: a person pressing Done did the work.
        assert row.status == TaskInstanceStatus.COMPLETED
        assert row.completed_by_id == REP
        assert row.completed_at is not None
        assert 'quote agreed' in (row.notes or '')


def test_the_note_is_optional(world):
    tid = world['tasks']['live']
    r = _client(REP).post(f'/api/tasks/{tid}/done', json={})
    assert r.status_code == 200
    with flask_app.app_context():
        assert db.session.get(Lead, world['leads']['live']) is not None
        assert (db.session.get(TaskInstance, tid).status
                == TaskInstanceStatus.COMPLETED)


def test_finishing_a_task_is_audited(world):
    tid = world['tasks']['live']
    _client(REP).post(f'/api/tasks/{tid}/done', json={'note': 'done it'})
    with flask_app.app_context():
        rows = AuditEvent.query.filter(
            AuditEvent.action == 'task.completed',
            AuditEvent.entity_id == str(tid)).all()
    assert len(rows) == 1
    assert rows[0].actor == REP
    assert rows[0].new_value['note'] == 'done it'


def test_somebody_elses_task_is_refused(world):
    with flask_app.app_context():
        other = _emp('TKOTHER', 'Someone Else')
        db.session.commit()
    tid = world['tasks']['live']
    r = _client('TKOTHER').post(f'/api/tasks/{tid}/done', json={})
    assert r.status_code == 403
    with flask_app.app_context():
        assert db.session.get(TaskInstance, tid).status \
            in TaskInstanceStatus.OPEN


def test_an_admin_can_finish_anybodys_task(world):
    tid = world['tasks']['live']
    r = _client(ADMIN).post(f'/api/tasks/{tid}/done', json={})
    assert r.status_code == 200
    with flask_app.app_context():
        assert (db.session.get(TaskInstance, tid).status
                == TaskInstanceStatus.COMPLETED)


def test_pressing_done_twice_is_not_an_error(world):
    """Two people on the same row is ordinary. The second should see
    the same outcome as the first, not a failure."""
    tid = world['tasks']['live']
    first = _client(REP).post(f'/api/tasks/{tid}/done', json={})
    second = _client(REP).post(f'/api/tasks/{tid}/done', json={})
    assert first.status_code == 200 and second.status_code == 200
    assert second.get_json()['already'] is True
    with flask_app.app_context():
        rows = AuditEvent.query.filter(
            AuditEvent.action == 'task.completed',
            AuditEvent.entity_id == str(tid)).all()
    assert len(rows) == 1, 'the second press must not write a second row'


def test_a_role_owned_task_can_be_cleared_by_somebody(world):
    """An escalation to Vertical_Head has no named owner. If only its
    owner could close it, it would belong to nobody."""
    from app.access import scope as sc_mod
    from app.tasks.routes import may_close
    with flask_app.app_context():
        twin = db.session.get(TaskInstance, world['tasks']['twin'])
        assert twin.owner_user_id is None and twin.owner_role
        assert may_close(twin, sc_mod.for_employee(ADMIN), ADMIN, False)
        # A rep who only sees their own leads may not.
        assert not may_close(twin, sc_mod.for_employee(REP), REP, False)


def test_the_note_has_a_ceiling(world):
    r = _client(REP).post(f'/api/tasks/{world["tasks"]["live"]}/done',
                          json={'note': 'x' * 2000})
    assert r.status_code == 400


def test_tasks_on_a_lead_are_listed_with_their_permission(world):
    body = _client(REP).get(
        f'/api/tasks?lead={world["leads"]["won"]}').get_json()
    assert body['ok']
    keys = {t['task_key'] for t in body['tasks']}
    assert 'lead.rfq_review' in keys
    for t in body['tasks']:
        assert 'may_close' in t and 'is_open' in t


# ── fault 2: the edit form fires the engine on a stage change ────────
def test_a_stage_change_on_the_edit_form_reaches_the_engine(world):
    """It never did. The advance and decision endpoints fired the
    hook; the form, which is how most people change a stage, did
    not."""
    calls = []
    import app as _app_mod
    real = _app_mod._fire_task_hook

    def spy(entity, entity_type, old_state, new_state):
        calls.append((entity_type, old_state, new_state))
        return real(entity, entity_type, old_state, new_state)

    _app_mod._fire_task_hook = spy
    try:
        _client(ADMIN).put(f'/api/leads/{world["leads"]["live"]}',
                           json={'stage': 'Quoted'})
    finally:
        _app_mod._fire_task_hook = real
    assert calls, 'the edit form did not call the task engine'
    assert calls[0][0] == 'Lead'
    assert calls[0][2] == 'Quoted'


def test_editing_any_other_field_raises_nothing(world):
    """A corrected phone number is not a state transition. Firing on
    every save would re-raise the same task on every edit."""
    calls = []
    import app as _app_mod
    real = _app_mod._fire_task_hook

    def spy(entity, entity_type, old_state, new_state):
        calls.append((entity_type, old_state, new_state))
        return real(entity, entity_type, old_state, new_state)

    _app_mod._fire_task_hook = spy
    try:
        _client(ADMIN).put(f'/api/leads/{world["leads"]["live"]}',
                           json={'city': 'Pune', 'phone': '9999999999'})
    finally:
        _app_mod._fire_task_hook = real
    assert calls == [], f'the engine fired on a non-stage edit: {calls}'


def test_a_save_that_resubmits_the_same_stage_raises_nothing(world):
    calls = []
    import app as _app_mod
    real = _app_mod._fire_task_hook
    _app_mod._fire_task_hook = lambda *a: calls.append(a)
    try:
        with flask_app.app_context():
            stage = db.session.get(Lead, world['leads']['live']).stage
        _client(ADMIN).put(f'/api/leads/{world["leads"]["live"]}',
                           json={'stage': stage})
    finally:
        _app_mod._fire_task_hook = real
    assert calls == []


# ── fault 3: the Lost dialog asks what the tasks used to ─────────────
def test_the_two_retired_keys_are_the_two_that_could_never_close():
    import scripts.retire_lost_tasks as retire
    assert set(retire.RETIRE) == {'deal.lost.debrief', 'competitor.log'}


def test_retiring_deactivates_the_definitions_and_closes_the_rows(world):
    import scripts.retire_lost_tasks as retire
    with flask_app.app_context():
        lead = db.session.get(Lead, world['leads']['live'])
        for key in retire.RETIRE:
            db.session.add(TaskDefinition(
                task_key=key, title=key, module='test',
                allowed_roles=[], is_active=True))
            _task(key, lead)
        db.session.commit()

        defs, rows = retire.targets()
        assert len(defs) == 2 and len(rows) == 2

        for d in defs:
            d.is_active = False
        for r in rows:
            r.status = TaskInstanceStatus.CANCELLED
        db.session.commit()

        defs_after, rows_after = retire.targets()
        assert rows_after == [], 'nothing open should be left'
        assert all(not d.is_active for d in defs_after)
        # The lead's own live task is untouched.
        assert (db.session.get(TaskInstance, world['tasks']['live']).status
                in TaskInstanceStatus.OPEN)


def test_a_new_competitor_name_creates_one_row_not_three(world):
    from app.models.competitor import CompetitorMaster
    from app.services import lost_capture
    with flask_app.app_context():
        CompetitorMaster.query.filter(
            CompetitorMaster.name.ilike('TK Rival%')).delete(
                synchronize_session=False)
        db.session.commit()
        a = lost_capture.resolve_competitor(name='TK Rival Ltd', actor=REP)
        db.session.commit()
        b = lost_capture.resolve_competitor(name='tk rival ltd', actor=REP)
        c = lost_capture.resolve_competitor(name='  TK RIVAL LTD ', actor=REP)
        db.session.commit()
        assert a.id == b.id == c.id, \
            'add-new made a second row for a name that already existed'
        assert CompetitorMaster.query.filter(
            CompetitorMaster.name.ilike('TK Rival%')).count() == 1


def test_resolving_by_id_and_by_nothing(world):
    from app.services import lost_capture
    with flask_app.app_context():
        made = lost_capture.resolve_competitor(name='TK Byid Ltd', actor=REP)
        db.session.commit()
        assert lost_capture.resolve_competitor(
            competitor_id=made.id).id == made.id
        assert lost_capture.resolve_competitor() is None
        assert lost_capture.resolve_competitor(name='   ') is None
        with pytest.raises(lost_capture.LostCaptureError):
            lost_capture.resolve_competitor(competitor_id=10 ** 9)


def test_losing_a_lead_records_the_competitor_and_the_debrief(world):
    lid = world['leads']['live']
    r = _client(ADMIN).post(f'/api/leads/{lid}/decision', json={
        'outcome': 'Lost', 'reason': 'Competition',
        'lost_to_competitor_name': 'TK Winner Logistics',
        'debrief_note': 'They were 8% cheaper on the main haul.'})
    assert r.status_code == 200, r.get_json()
    with flask_app.app_context():
        lead = db.session.get(Lead, lid)
        assert lead.stage == 'Lost'
        assert lead.lost_reason == 'Competition'
        assert lead.debrief_note.startswith('They were 8%')
        from app.models.competitor import CompetitorMaster
        comp = db.session.get(CompetitorMaster, lead.lost_to_competitor_id)
        assert comp is not None and comp.name == 'TK Winner Logistics'


def test_both_new_fields_are_optional(world):
    """A required field people cannot answer is how 4,738 lost leads
    came to have no lost reason at all."""
    lid = world['leads']['live']
    r = _client(ADMIN).post(f'/api/leads/{lid}/decision',
                            json={'outcome': 'Lost', 'reason': 'No Response'})
    assert r.status_code == 200
    with flask_app.app_context():
        lead = db.session.get(Lead, lid)
        assert lead.stage == 'Lost'
        assert lead.lost_to_competitor_id is None
        assert not lead.debrief_note


def test_the_lost_reason_is_still_mandatory(world):
    r = _client(ADMIN).post(f'/api/leads/{world["leads"]["live"]}/decision',
                            json={'outcome': 'Lost',
                                  'debrief_note': 'no reason given'})
    assert r.status_code == 400
    assert 'reason required' in r.get_json()['error']


def test_an_overlong_debrief_is_refused_not_truncated(world):
    from app.services import lost_capture
    r = _client(ADMIN).post(f'/api/leads/{world["leads"]["live"]}/decision',
                            json={'outcome': 'Lost', 'reason': 'Price',
                                  'debrief_note': 'x' * (lost_capture.MAX_NOTE + 1)})
    assert r.status_code == 400
    with flask_app.app_context():
        assert db.session.get(Lead, world['leads']['live']).stage != 'Lost', \
            'the decision must not land when its capture was refused'


def test_winning_a_lead_asks_for_none_of_it(world):
    lid = world['leads']['live']
    r = _client(ADMIN).post(f'/api/leads/{lid}/decision',
                            json={'outcome': 'Won'})
    assert r.status_code == 200
    with flask_app.app_context():
        lead = db.session.get(Lead, lid)
        assert lead.lost_to_competitor_id is None and not lead.debrief_note


def test_the_dialog_sends_what_it_collects():
    """The competitor box was collected and never sent, and `remarks`
    was sent and never read — both silently discarded, like the Won
    value before them. The page must send the two names the server
    actually reads."""
    with open(os.path.join(_ROOT, 'templates', 'app.html')) as fh:
        page = fh.read()
    assert 'body.debrief_note' in page
    assert 'body.lost_to_competitor_name' in page
    assert 'body.lost_to_competitor_id' in page
    # Scoped to the Lost branch, which is what was asked for. The
    # On Hold branch still sends `remarks`, which the server also
    # does not read — reported, not fixed here, because changing the
    # On Hold dialog was not part of this work.
    lost_branch = page.split("_decOutcome === 'Lost'", 1)[-1]
    lost_branch = lost_branch.split("else if (_decOutcome === 'On Hold')", 1)[0]
    assert 'body.remarks' not in lost_branch, \
        'the Lost branch still sends a field nobody reads'


# ── On Hold: the remark that was collected and dropped ───────────────
def test_putting_a_lead_on_hold_stores_the_remark(world):
    lid = world['leads']['live']
    r = _client(ADMIN).post(f'/api/leads/{lid}/decision', json={
        'outcome': 'On Hold', 'reason': 'Awaiting client budget',
        'hold_note': 'Client CFO signs off in April; call them in March.'})
    assert r.status_code == 200, r.get_json()
    with flask_app.app_context():
        lead = db.session.get(Lead, lid)
        assert lead.stage == 'On Hold'
        assert lead.hold_note.startswith('Client CFO')


def test_a_page_cached_before_today_still_sends_remarks(world):
    """`remarks` was the old field name and was silently dropped.
    Accepting it means a stale tab does not lose the typing twice."""
    lid = world['leads']['live']
    r = _client(ADMIN).post(f'/api/leads/{lid}/decision', json={
        'outcome': 'On Hold', 'reason': 'Paused',
        'remarks': 'sent by an old page'})
    assert r.status_code == 200
    with flask_app.app_context():
        assert db.session.get(Lead, lid).hold_note == 'sent by an old page'


def test_the_hold_note_is_audited(world):
    lid = world['leads']['live']
    _client(ADMIN).post(f'/api/leads/{lid}/decision', json={
        'outcome': 'On Hold', 'reason': 'Paused', 'hold_note': 'why it paused'})
    with flask_app.app_context():
        rows = AuditEvent.query.filter(
            AuditEvent.entity_type == 'lead',
            AuditEvent.entity_id == str(lid)).all()
    assert any('hold_note' in (r.new_value or {}) for r in rows), \
        'the hold note changed and left no audit row'


def test_the_hold_note_is_optional(world):
    lid = world['leads']['live']
    r = _client(ADMIN).post(f'/api/leads/{lid}/decision',
                            json={'outcome': 'On Hold', 'reason': 'Paused'})
    assert r.status_code == 200
    with flask_app.app_context():
        assert db.session.get(Lead, lid).stage == 'On Hold'


def test_the_on_hold_dialog_sends_what_it_collects():
    with open(os.path.join(_ROOT, 'templates', 'app.html')) as fh:
        page = fh.read()
    hold = page.split("_decOutcome === 'On Hold'", 1)[-1]
    hold = hold.split("else if (_decOutcome === 'Won')", 1)[0]
    assert 'body.hold_note' in hold
    assert 'body.remarks' not in hold, 'still sending a field nobody reads'


# ── the competitors page ─────────────────────────────────────────────
def _competitor(name):
    from app.models.competitor import CompetitorMaster
    row = CompetitorMaster(name=name, is_active=True)
    db.session.add(row)
    db.session.flush()
    return row


def test_the_competitors_page_is_no_longer_a_dead_redirect(world):
    """It redirected to /companies?relationship=Competitor, and
    Company has no `relationship` column, so that filter could never
    match. It is a page now."""
    from app import Company
    assert 'relationship' not in {c.name for c in Company.__table__.columns}
    r = _client(ADMIN).get('/competitors')
    assert r.status_code == 200
    assert b'Competitors' in r.data


def test_the_summary_counts_deals_lost_to_each(world):
    with flask_app.app_context():
        rival = _competitor('TK Summary Rival')
        quiet = _competitor('TK Never Won Anything')
        for key in ('live', 'won'):
            lead = db.session.get(Lead, world['leads'][key])
            lead.lost_to_competitor_id = rival.id
        db.session.commit()
        rival_id, quiet_id = rival.id, quiet.id

    body = _client(ADMIN).get('/api/competitors/summary').get_json()
    assert body['ok']
    by_id = {c['id']: c for c in body['items']}
    assert by_id[rival_id]['lost_count'] == 2
    assert by_id[rival_id]['last_seen']
    assert by_id[quiet_id]['lost_count'] == 0
    # Worst offender first.
    counts = [c['lost_count'] for c in body['items']]
    assert counts == sorted(counts, reverse=True)


def test_merging_moves_the_deals_before_removing_the_duplicate(world):
    from app.models.competitor import CompetitorMaster
    with flask_app.app_context():
        keep = _competitor('TK Kuehne Nagel')
        dupe = _competitor('TK K+N')
        lead = db.session.get(Lead, world['leads']['live'])
        lead.lost_to_competitor_id = dupe.id
        db.session.commit()
        keep_id, dupe_id, lid = keep.id, dupe.id, lead.id

    r = _client(ADMIN).post(f'/api/competitors/{dupe_id}/merge',
                            json={'into': keep_id})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['leads_moved'] == 1

    with flask_app.app_context():
        assert db.session.get(CompetitorMaster, dupe_id) is None
        keeper = db.session.get(CompetitorMaster, keep_id)
        assert 'TK K+N' in (keeper.aliases or []), \
            'the old spelling must survive as an alias'
        # No lead is left pointing at a competitor that is gone.
        assert db.session.get(Lead, lid).lost_to_competitor_id == keep_id
        orphans = Lead.query.filter(
            Lead.lost_to_competitor_id == dupe_id).count()
        assert orphans == 0


def test_merging_is_audited(world):
    with flask_app.app_context():
        keep, dupe = _competitor('TK Keep Co'), _competitor('TK Dupe Co')
        db.session.commit()
        keep_id, dupe_id = keep.id, dupe.id
    _client(ADMIN).post(f'/api/competitors/{dupe_id}/merge',
                        json={'into': keep_id})
    with flask_app.app_context():
        rows = AuditEvent.query.filter(
            AuditEvent.action == 'competitor.merged',
            AuditEvent.entity_id == str(keep_id)).all()
    assert len(rows) == 1
    assert rows[0].old_value['merged_name'] == 'TK Dupe Co'


def test_merging_into_itself_is_refused(world):
    with flask_app.app_context():
        c = _competitor('TK Self Merge')
        db.session.commit()
        cid = c.id
    r = _client(ADMIN).post(f'/api/competitors/{cid}/merge', json={'into': cid})
    assert r.status_code == 400


def test_only_an_admin_may_add_edit_or_merge(world):
    with flask_app.app_context():
        a, b = _competitor('TK Perm A'), _competitor('TK Perm B')
        db.session.commit()
        a_id, b_id = a.id, b.id
    rep = _client(REP)
    assert rep.post('/api/competitors',
                    json={'name': 'TK Sneaky'}).status_code == 403
    assert rep.patch(f'/api/competitors/{a_id}',
                     json={'name': 'TK Renamed'}).status_code == 403
    assert rep.post(f'/api/competitors/{a_id}/merge',
                    json={'into': b_id}).status_code == 403
    with flask_app.app_context():
        from app.models.competitor import CompetitorMaster
        assert db.session.get(CompetitorMaster, a_id).name == 'TK Perm A'


def test_a_rep_can_still_name_a_competitor_while_losing_a_deal(world):
    """Curating the list is admin work; naming who took your deal is
    not. The Lost dialog does not come through the admin endpoint."""
    from app.models.competitor import CompetitorMaster
    r = _client(REP).post(f'/api/leads/{world["leads"]["live"]}/decision',
                          json={'outcome': 'Lost', 'reason': 'Competition',
                                'lost_to_competitor_name': 'TK Rep Named Co'})
    assert r.status_code == 200, r.get_json()
    with flask_app.app_context():
        assert CompetitorMaster.query.filter_by(
            name='TK Rep Named Co').count() == 1
