"""The daily Data Quality snapshot: one row per check per day, safe to rerun.

Snapshots are written for dates far in the future, so they are the most
recent in the shared test database whatever else has run, and removed
afterwards.
"""
import os
import sys
import tempfile
from datetime import date, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'DqSnapTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'dqsnap.db'))

from app import app as flask_app, db                            # noqa: E402
import app.models                                               # noqa: E402,F401
from app.data_quality import definitions as defs                # noqa: E402
from app.data_quality import service as dq                      # noqa: E402
from app.models.data_quality import DataQualitySnapshot as Snap  # noqa: E402

# Appended after the app import: scripts/ holds an email_ingest.py that
# would shadow the package of the same name.
sys.path.append(os.path.join(_ROOT, 'scripts'))
import data_quality_snapshot as job                             # noqa: E402

DAY = date(2099, 1, 1)


@pytest.fixture()
def clean():
    with flask_app.app_context():
        db.create_all()
    yield
    with flask_app.app_context():
        Snap.query.filter(Snap.snapshot_date >= date(2098, 1, 1)).delete(
            synchronize_session=False)
        db.session.commit()


def test_one_row_per_check_and_a_rerun_replaces_it(clean):
    with flask_app.app_context():
        first = dq.snapshot_all(today=DAY)
        assert [r['key'] for r in first] == [c.key for c in dq.CHECKS]
        rows = Snap.query.filter_by(snapshot_date=DAY).all()
        assert len(rows) == len(dq.CHECKS)
        assert {r.scope for r in rows} == {'company'}

        dq.snapshot_all(today=DAY)
        assert Snap.query.filter_by(snapshot_date=DAY).count() == \
            len(dq.CHECKS), 'a retried timer must not add a second day'


def test_a_rerun_takes_the_latest_count(clean):
    with flask_app.app_context():
        dq.snapshot_all(today=DAY)
        row = Snap.query.filter_by(snapshot_date=DAY,
                                   check_key='unowned_leads').one()
        row.count = 987654
        db.session.commit()
        dq.snapshot_all(today=DAY)
        assert Snap.query.filter_by(snapshot_date=DAY,
                                    check_key='unowned_leads').one() \
            .count != 987654


def test_a_failing_check_is_a_gap_not_a_zero(clean, monkeypatch):
    def boom(sc=None):
        raise RuntimeError('boom')
    broken = dq.Check('unowned_leads', 'x', 'x', 'low', '/', boom)
    monkeypatch.setattr(dq, 'CHECKS', [broken] + dq.CHECKS[1:])
    with flask_app.app_context():
        dq.snapshot_all(today=DAY)
        rows = {r.check_key: r.count for r in
                Snap.query.filter_by(snapshot_date=DAY).all()}
    assert rows['unowned_leads'] is None
    assert all(v is not None for k, v in rows.items()
               if k != 'unowned_leads'), 'the others are still written'


def test_trends_are_oldest_first_and_limited(clean):
    with flask_app.app_context():
        for i in range(defs.TREND_POINTS + 3):
            db.session.add(Snap(snapshot_date=DAY + timedelta(days=i),
                                check_key='unowned_leads', scope='company',
                                count=i))
        db.session.commit()
        series = dq.trends()['unowned_leads']
    assert len(series) == defs.TREND_POINTS
    assert [c for _d, c in series] == list(range(3, defs.TREND_POINTS + 3))


def test_trends_survive_a_missing_table(monkeypatch):
    class Missing:
        def __getattr__(self, name):
            raise RuntimeError('no such table: data_quality_snapshots')
    import app.models.data_quality as mod
    monkeypatch.setattr(mod, 'DataQualitySnapshot', Missing())
    with flask_app.app_context():
        assert dq.trends() == {}


def test_sparkline_points():
    assert dq.sparkline([]) == ''
    assert dq.sparkline([(DAY, 5)]) == ''
    pts = [tuple(map(float, p.split(','))) for p in
           dq.sparkline([(DAY, 0), (DAY, None), (DAY, 10), (DAY, 5)])
           .split()]
    assert len(pts) == 3
    assert pts[0][0] == 0 and pts[-1][0] == 120
    assert all(0 <= y <= 28 for _x, y in pts)
    assert pts[1][1] < pts[0][1], 'a higher count is drawn higher'


def test_the_job_writes_the_day_and_reports(clean, capsys):
    assert job.main(['--date', '2099-02-01', '--quiet']) == 0
    with flask_app.app_context():
        assert Snap.query.filter_by(snapshot_date=date(2099, 2, 1)).count() \
            == len(dq.CHECKS)
    assert '2099-02-01' in capsys.readouterr().out


def test_the_job_refuses_a_bad_date_and_a_missing_table(clean, monkeypatch):
    assert job.main(['--date', 'tomorrow']) == 2
    monkeypatch.setattr(job, '_table_exists', lambda: False)
    assert job.main(['--date', '2099-03-01']) == 2
    with flask_app.app_context():
        assert Snap.query.filter_by(snapshot_date=date(2099, 3, 1)).count() \
            == 0


def test_the_timer_templates_exist_and_run_the_job():
    base = os.path.join(_ROOT, 'docs', 'operations', 'deploy',
                        'procam-crm-dq-snapshot')
    service = open(base + '.service').read()
    timer = open(base + '.timer').read()
    assert 'scripts/data_quality_snapshot.py' in service
    assert 'Type=oneshot' in service and 'User=procamapp' in service
    assert 'OnCalendar=' in timer and 'Persistent=true' in timer
