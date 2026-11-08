"""Closing a task the two rules do not pick.

CRM-FINAL-CLOSEOUT-01 §9: "give me the exact --check and --apply
lines for task_backlog_report.py (tasks 1342/1541/1283/1536
included)".

Those four ids came off a report, not out of `classify()`. So
"included" has to mean included whatever the rules think, which is
what `--task` does — and the thing worth testing is not that the
option parses but that a named id actually closes, with an audit row
that says why.

The other half is the refusal. A named id that is not an open task
stops the run, because closing nine hundred rows while quietly
dropping one of the four somebody asked for reports success for a
job half done.
"""
import importlib.util
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'backlog-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Backlog-123456789')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'backlog.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db

from app import Lead                                        # noqa: E402
from app.models.audit import AuditEvent                     # noqa: E402
from app.models.task_engine import (TaskInstance,            # noqa: E402
                                    TaskInstanceStatus)

SCRIPT = os.path.join(_ROOT, 'scripts', 'task_backlog_report.py')


def _script():
    spec = importlib.util.spec_from_file_location('backlog_rep', SCRIPT)
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
    TaskInstance.query.delete()
    Lead.query.delete()
    AuditEvent.query.filter(
        AuditEvent.action == 'task.backlog_closed').delete(
            synchronize_session=False)
    db.session.commit()


def _lead(stage='New', age_days=2):
    lead = Lead(company='ZBacklog Client', stage=stage, source='email',
                created_at=datetime.utcnow() - timedelta(days=age_days))
    db.session.add(lead)
    db.session.commit()
    return lead


def _task(lead, *, age_days=2, status=None, key='rfq.register'):
    row = TaskInstance(
        task_key=key, entity_type='Lead', entity_id=lead.id,
        owner_user_id='ZB_OWN',
        status=status or TaskInstanceStatus.OPEN[0],
        created_at=datetime.utcnow() - timedelta(days=age_days))
    db.session.add(row)
    db.session.commit()
    return row


# ── the option itself ────────────────────────────────────────────────
def test_ids_are_read_both_ways_round():
    """A list copied out of a report arrives comma-separated, and
    retyping it is how one of them gets dropped."""
    mod = _script()
    assert mod.parse_ids(['1342', '1541']) == [1342, 1541]
    assert mod.parse_ids(['1342,1541']) == [1342, 1541]
    assert mod.parse_ids(['1342, 1541', '1283']) == [1342, 1541, 1283]
    assert mod.parse_ids(['1342', '1342']) == [1342]
    assert mod.parse_ids(None) == []


def test_a_word_is_refused_rather_than_ignored():
    mod = _script()
    with pytest.raises(SystemExit):
        mod.parse_ids(['all'])


# ── a named task closes ──────────────────────────────────────────────
def test_a_live_lead_keeps_its_task_until_it_is_named():
    with flask_app.app_context():
        mod = _script()
        lead = _lead(stage='RFQ Generated')
        task = _task(lead)

        picked, _rows, _leads = mod.classify()
        assert task.id not in picked, (
            'neither rule should pick a fresh task on a live lead')

        picked, _rows, _leads = mod.classify(named=[task.id])
        assert picked[task.id] == 'named'


def test_a_rule_that_already_picked_it_keeps_its_reason():
    """The truer answer, and the one a person reading the audit row
    can check for themselves."""
    with flask_app.app_context():
        mod = _script()
        lead = _lead(stage='Lost')
        task = _task(lead)
        picked, _rows, _leads = mod.classify(named=[task.id])
        assert picked[task.id] == 'lead_closed'


def test_naming_an_id_does_not_disturb_the_rest():
    with flask_app.app_context():
        mod = _script()
        live = _task(_lead(stage='RFQ Generated'))
        closed = _task(_lead(stage='Lost'))
        picked, _rows, _leads = mod.classify(named=[live.id])
        assert set(picked) == {live.id, closed.id}
        assert picked[closed.id] == 'lead_closed'


def test_a_named_reason_has_notes_to_write():
    """Every reason `classify` can return needs a sentence for the
    task's own notes, or the apply path raises a KeyError on the row
    it was asked to close."""
    mod = _script()
    for key in mod.REASONS:
        assert key in mod.NOTES, key
        assert mod.NOTES[key].strip()


# ── the report line ──────────────────────────────────────────────────
def test_the_report_says_what_each_named_id_is():
    with flask_app.app_context():
        mod = _script()
        lead = _lead(stage='RFQ Generated')
        task = _task(lead, key='quote.prepare')
        picked, rows, leads = mod.classify(named=[task.id])
        lines = mod.named_report([task.id], rows, leads, picked)
        assert len(lines) == 1
        line = lines[0]
        assert line['open'] is True
        assert line['task_key'] == 'quote.prepare'
        assert line['lead_id'] == lead.id
        assert line['stage'] == 'RFQ Generated'
        assert line['rule'] == 'named'


def test_an_id_that_is_already_closed_is_reported_not_reopened():
    with flask_app.app_context():
        mod = _script()
        lead = _lead(stage='RFQ Generated')
        task = _task(lead, status=TaskInstanceStatus.CANCELLED)
        picked, rows, leads = mod.classify(named=[task.id])
        assert task.id not in picked, 'a closed task must not reopen'
        line = mod.named_report([task.id], rows, leads, picked)[0]
        assert line['open'] is False
        assert 'already' in line['state']


def test_an_id_that_does_not_exist_says_so():
    with flask_app.app_context():
        mod = _script()
        picked, rows, leads = mod.classify(named=[999999])
        line = mod.named_report([999999], rows, leads, picked)[0]
        assert line['found'] is False
        assert line['state'] == 'no such task'


# ── the apply path, not only the refusal ─────────────────────────────
def test_the_apply_actually_closes_the_named_task():
    """The thing a dry run cannot tell you. A gated --apply that was
    never wired reports a clean plan and writes nothing."""
    with flask_app.app_context():
        mod = _script()
        lead = _lead(stage='RFQ Generated')
        task = _task(lead)
        tid = task.id

        sys.argv = ['task_backlog_report.py', '--apply',
                    '--i-have-approval', '--task', str(tid)]
        mod.main()

        # `main()` opens its own application context, so this session's
        # copy of the row is the one loaded before the write. Expire it
        # rather than assert against a cached object — a test that
        # reads its own stale instance passes whatever the script did.
        db.session.expire_all()
        row = TaskInstance.query.get(tid)
        assert row.status == TaskInstanceStatus.CANCELLED
        assert row.completed_at is not None
        assert mod.NOTES['named'] in (row.notes or '')

        events = AuditEvent.query.filter_by(
            action='task.backlog_closed',
            entity_id=str(tid)).all()
        assert len(events) == 1
        assert events[0].new_value['rule'] == 'named'


def test_the_check_run_closes_nothing():
    with flask_app.app_context():
        mod = _script()
        lead = _lead(stage='Lost')
        task = _task(lead)
        tid = task.id
        sys.argv = ['task_backlog_report.py', '--check',
                    '--task', str(tid)]
        mod.main()
        db.session.expire_all()
        assert TaskInstance.query.get(tid).status \
            in TaskInstanceStatus.OPEN


def test_apply_without_approval_still_refuses():
    with flask_app.app_context():
        mod = _script()
        lead = _lead(stage='Lost')
        task = _task(lead)
        tid = task.id
        sys.argv = ['task_backlog_report.py', '--apply',
                    '--task', str(tid)]
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 2
        db.session.expire_all()
        assert TaskInstance.query.get(tid).status \
            in TaskInstanceStatus.OPEN


def test_a_missing_named_id_stops_the_run_before_anything_closes():
    """The point of stopping: the four ids were asked for by name, so
    closing the rest and leaving one out of the count would report a
    job half done as a job finished."""
    with flask_app.app_context():
        mod = _script()
        lead = _lead(stage='Lost')
        task = _task(lead)
        tid = task.id
        sys.argv = ['task_backlog_report.py', '--apply',
                    '--i-have-approval', '--task', f'{tid},999999']
        with pytest.raises(SystemExit) as exc:
            mod.main()
        assert exc.value.code == 2
        # And the one that *was* valid is untouched.
        db.session.expire_all()
        assert TaskInstance.query.get(tid).status \
            in TaskInstanceStatus.OPEN
