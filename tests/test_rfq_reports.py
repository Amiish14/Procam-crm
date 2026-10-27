"""
The workload table, computed instead of typed.

CRM-RFQ-REPORTS-01 (R5).

The SCM sheet says who was slow, and every figure on it was typed
by the people it measures. These numbers come from started_at,
completed_at and paused_seconds, so the tests that matter are the
ones about what the report refuses to say:

    a step with no standard time is scored nowhere — steps 9-11
    run on a response clock or the customer's own, and putting a
    customer's delay in a Procam person's column is how a league
    table stops being believed;

    a person with two closed steps has no percentage. They are
    unmeasured, not at 50%, and a table that cannot tell those
    apart is one people learn to game;

    held time never counts.
"""
import importlib
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'reports-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Reports-1234567')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'reports.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.rfq')
importlib.import_module('app.models.rfq_target')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Employee = _main.Employee

from app.models.rfq import RFQ                            # noqa: E402
from app.models.task_engine import (TaskInstance,         # noqa: E402
                                    TaskInstanceStatus)
from app.services import rfq_chain as rc                  # noqa: E402
from app.services import rfq_reports as rr                # noqa: E402

A, B = 'RR_A', 'RR_B'


def _purge():
    TaskInstance.query.filter(
        TaskInstance.entity_type == 'RFQ').delete(
            synchronize_session=False)
    RFQ.query.filter(RFQ.rfq_number.like('RR-%')).delete(
        synchronize_session=False)
    Employee.query.filter(Employee.emp_code.like('RR_%')).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code in (A, B):
            db.session.add(Employee(
                emp_code=code, name=code, password_hash='x',
                role='admin',
                email=f'{code.lower()}@procamlogistics.com'))
        db.session.commit()
        _flag(True)
        yield
        _flag(False)
        _purge()


def _flag(state):
    import json
    from app.models.integration import AppSetting
    from app.services import flags
    for key in ('flag.FEATURE_RFQ_REPORTS', 'flag.FEATURE_RFQ_CHAIN'):
        row = AppSetting.query.filter_by(key=key).first()
        if row is None:
            row = AppSetting(key=key)
            db.session.add(row)
        row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


_N = [0]


def _rfq(priority='Medium', **kw):
    _N[0] += 1
    kw.setdefault('needs_technical', True)
    kw.setdefault('needs_survey', True)
    rfq = RFQ(rfq_number=f'RR-{_N[0]:04d}', subject='RR test',
              priority=priority, **kw)
    db.session.add(rfq)
    db.session.commit()
    return rfq


def _done(rfq, step_no, owner, *, hours, result):
    """A closed step that took `hours`."""
    now = datetime.utcnow()
    task = TaskInstance(
        task_key=rc.BY_NO[step_no]['key'], entity_type='RFQ',
        entity_id=rfq.id, step_no=step_no, owner_user_id=owner,
        status=TaskInstanceStatus.COMPLETED,
        started_at=now - timedelta(hours=hours), created_at=now,
        completed_at=now, completed_by_id=owner, result=result,
        paused_seconds=0, priority=3)
    db.session.add(task)
    db.session.commit()
    return task


# ── the tiles ────────────────────────────────────────────────────────
def test_the_tiles_count_what_they_say():
    with flask_app.app_context():
        _rfq(status='Won')
        _rfq(status='Lost')
        _rfq(status='Quoted')
        got = rr.tiles()
        assert got['received'] >= 3
        assert got['won'] == 1 and got['lost'] == 1
        assert got['quoted'] == 1
        assert got['conversion'] == 50       # 1 of 2 closed


def test_conversion_is_none_before_anything_closes():
    """0% would read as "we lose everything"."""
    with flask_app.app_context():
        _rfq(status='Quoted')
        assert rr.tiles()['conversion'] is None


def test_average_response_is_none_when_nothing_has_been_quoted():
    with flask_app.app_context():
        _rfq()
        assert rr.tiles()['avg_response_hours'] is None


# ── what is scored, and what is not ──────────────────────────────────
def test_a_step_with_no_standard_time_is_scored_nowhere():
    """Steps 9-11 run on the customer's clock. Scoring them puts
    the customer's delay in a Procam person's column."""
    with flask_app.app_context():
        rfq = _rfq('Medium')
        _done(rfq, 11, A, hours=900, result=None)
        rows = {r['emp_code']: r for r in rr.workload()}
        assert A not in rows or rows[A]['closed'] == 0


def test_a_scoreable_step_is_counted():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        _done(rfq, 6, A, hours=20, result=rc.ON_TIME)
        rows = {r['emp_code']: r for r in rr.workload()}
        assert rows[A]['closed'] == 1
        assert rows[A]['on_time'] == 1


def test_too_few_steps_means_unmeasured_not_a_percentage():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        for _ in range(2):
            _done(rfq, 6, A, hours=20, result=rc.ON_TIME)
        row = {r['emp_code']: r for r in rr.workload()}[A]
        assert row['closed'] == 2
        assert row['on_time_pct'] is None
        assert row['verdict'] is None
        assert row['measured'] is False


def test_enough_steps_gets_a_percentage_and_a_verdict():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        for _ in range(rr.MIN_FOR_RATE):
            _done(rfq, 6, A, hours=20, result=rc.ON_TIME)
        row = {r['emp_code']: r for r in rr.workload()}[A]
        assert row['on_time_pct'] == 100
        assert row['verdict'] == 'Achieved'
        assert row['measured'] is True


def test_below_the_98_percent_standard_says_so():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        for _ in range(4):
            _done(rfq, 6, A, hours=20, result=rc.ON_TIME)
        _done(rfq, 6, A, hours=99, result=rc.DELAYED)
        row = {r['emp_code']: r for r in rr.workload()}[A]
        assert row['on_time_pct'] == 80
        assert row['verdict'] == 'Below target'
        assert row['gap'] == 18


def test_early_counts_as_on_time_and_is_also_counted_separately():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        for _ in range(rr.MIN_FOR_RATE):
            _done(rfq, 6, A, hours=5, result=rc.EARLY)
        row = {r['emp_code']: r for r in rr.workload()}[A]
        assert row['early'] == rr.MIN_FOR_RATE
        assert row['on_time_pct'] == 100


def test_held_time_is_not_counted_against_anybody():
    with flask_app.app_context():
        rfq = _rfq('Medium')               # costing standard 50.4h
        task = _done(rfq, 6, A, hours=60, result=None)
        task.paused_seconds = 30 * 3600
        db.session.commit()
        rows = rr.per_step()
        step6 = [r for r in rows if r['step_no'] == 6][0]
        assert step6['avg_hours'] == pytest.approx(30, abs=0.2)


# ── the slowest step ─────────────────────────────────────────────────
def test_the_slowest_step_is_named_once_there_is_evidence():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        for _ in range(3):
            _done(rfq, 3, A, hours=60, result=rc.DELAYED)  # std 8.4h
            _done(rfq, 6, B, hours=10, result=rc.EARLY)
        worst = rr.slowest_step()
        assert worst is not None
        assert worst['step_no'] == 3
        assert worst['ratio'] > 1


def test_one_slow_step_is_not_a_bottleneck():
    """Three closes minimum, or one bad week names a step."""
    with flask_app.app_context():
        rfq = _rfq('Medium')
        _done(rfq, 3, A, hours=60, result=rc.DELAYED)
        assert rr.slowest_step() is None


def test_nothing_overdue_means_no_slowest_step():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        for _ in range(3):
            _done(rfq, 6, A, hours=10, result=rc.EARLY)
        assert rr.slowest_step() is None


# ── heatmap and reasons ──────────────────────────────────────────────
def test_the_heatmap_is_a_ratio_with_its_sample_size():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        _done(rfq, 6, A, hours=25, result=rc.ON_TIME)   # std 50.4
        got = rr.heatmap()
        cell = [c for c in got['cells']
                if c['emp_code'] == A and c['step_no'] == 6][0]
        assert cell['ratio'] == pytest.approx(0.5, abs=0.02)
        assert cell['n'] == 1


def test_delay_reasons_are_cut_every_way_the_spec_asks():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        task = _done(rfq, 6, A, hours=99, result=rc.DELAYED)
        task.delay_reason = 'Awaiting vendor rate'
        db.session.commit()
        got = rr.reasons()
        assert got['delay']['reason']['Awaiting vendor rate'] == 1
        for cut in ('step', 'person', 'vertical', 'customer',
                    'month'):
            assert got['delay'][cut]


def test_good_practice_reasons_are_reported_too():
    with flask_app.app_context():
        rfq = _rfq('Medium')
        task = _done(rfq, 6, A, hours=5, result=rc.EARLY)
        task.good_practice_reason = 'Historical rate / database used'
        db.session.commit()
        assert rr.reasons()['good_practice'][
            'Historical rate / database used'] == 1


# ── §6.4 chart ───────────────────────────────────────────────────────
def test_the_chart_has_a_bar_per_applicable_step():
    with flask_app.app_context():
        rfq = _rfq('Medium', needs_survey=False)
        _done(rfq, 6, A, hours=20, result=rc.ON_TIME)
        got = rr.time_chart(rfq)
        steps = [b['step_no'] for b in got['bars']]
        assert 5 not in steps              # survey unticked
        bar = [b for b in got['bars'] if b['step_no'] == 6][0]
        assert bar['hours'] == pytest.approx(20, abs=0.2)
        assert bar['standard'] == pytest.approx(50.4, abs=0.1)


# ── the flag ─────────────────────────────────────────────────────────
def test_the_reports_404_while_the_flag_is_off():
    with flask_app.app_context():
        _flag(False)
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = A
    for path in ('/api/reports/tiles', '/api/reports/workload',
                 '/api/reports/heatmap', '/rfqs/reports'):
        assert client.get(path).status_code == 404, path


def test_every_tile_names_the_list_it_opens():
    """Acceptance test 13: a figure and its list must not drift."""
    with flask_app.app_context():
        _flag(True)
        _rfq(status='Won')
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = A
    body = client.get('/api/reports/tiles').get_json()
    assert body['ok'] is True
    for key in ('received', 'active', 'on_hold', 'quoted', 'won',
                'lost', 'overdue', 'due_today'):
        assert key in body['tiles']
        assert body['opens'].get(key), f'{key} opens nothing'


# ── acceptance 13 and 14: the figure equals the list ─────────────────
def test_the_overdue_tile_agrees_with_the_board():
    """Acceptance 13. Two code paths count overdue — the tile from
    rfq_reports and the strip from rfq_chain.board. A dashboard
    that disagrees with the list it opens is worse than no
    dashboard, because somebody acts on the number."""
    with flask_app.app_context():
        rfq = _rfq('Medium')              # costing standard 50.4h
        now = datetime.utcnow()
        db.session.add(TaskInstance(
            task_key=rc.BY_NO[6]['key'], entity_type='RFQ',
            entity_id=rfq.id, step_no=6, owner_user_id=A,
            status=TaskInstanceStatus.PENDING,
            started_at=now - timedelta(hours=80),
            created_at=now, paused_seconds=0, priority=3))
        db.session.commit()

        from_tile = rr.tiles()['overdue']
        from_board = sum(
            1 for row in rc.board(rfq)
            if row['state'] == 'current' and row['colour'] == rc.RED)
        assert from_tile == from_board == 1


def test_the_won_tile_equals_the_rfqs_it_would_list():
    """Acceptance 13, the other direction: the tile counts exactly
    the rows its filter selects."""
    with flask_app.app_context():
        _rfq(status='Won')
        _rfq(status='Won')
        _rfq(status='Lost')
        tile = rr.tiles()['won']
        listed = [r for r in rr._visible_rfqs(None)
                  if (r.status or '') == 'Won']
        assert tile == len(listed) == 2


def test_targets_received_equals_the_rfqs_registered_that_month():
    """Acceptance 14, counted independently of targets_for()."""
    from app.models.rfq_target import RfqTarget
    from app.services import lead_journey as lj

    with flask_app.app_context():
        month = lj.month_key()
        db.session.add(RfqTarget(month=month, vertical='Warehousing',
                                 emp_code=A, target_rfqs=3))
        now = datetime.utcnow()
        # An RFQ reaches its vertical through its lead, so give it
        # one — otherwise the target row and the RFQ land under
        # different keys and the test proves nothing.
        lead = _main.Lead(company='RR Co', stage='RFQ Generated',
                          procam_vertical='Warehousing',
                          source='manual')
        db.session.add(lead)
        db.session.flush()
        for _ in range(2):
            r = _rfq('Medium', lead_driver=A, lead_id=lead.id)
            r.registered_at = now
        # One registered last month, which must not be counted.
        old = _rfq('Medium', lead_driver=A, lead_id=lead.id)
        old.registered_at = now - timedelta(days=45)
        db.session.commit()
        try:
            rows = {(r['emp_code'], r['vertical']): r
                    for r in lj.targets_for(month=month)}
            mine = rows.get((A, 'Warehousing'))
            assert mine is not None, rows

            independent = sum(
                1 for r in RFQ.query.all()
                if r.registered_at
                and lj.month_key(r.registered_at.date()) == month
                and (r.lead_driver or r.created_by_id) == A)
            assert mine['received'] == independent == 2
            assert mine['target'] == 3
        finally:
            RfqTarget.query.filter_by(month=month,
                                      emp_code=A).delete(
                synchronize_session=False)
            _main.Lead.query.filter(
                _main.Lead.company == 'RR Co').delete(
                    synchronize_session=False)
            db.session.commit()
