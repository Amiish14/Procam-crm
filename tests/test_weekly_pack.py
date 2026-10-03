"""The Thursday review pack and the Friday freeze.

The freeze exists so a number quoted in a review cannot change while it
is being discussed, so the test that matters is: edit the data after
the freeze, and the frozen figure stays where it was.
"""
import os
import sys
import tempfile
from datetime import date

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'pack-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'PackTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'pack.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead

from app.models.mailops import WeeklyPipelineSnapshot     # noqa: E402
from app.services import weekly_pack as pack              # noqa: E402

TAG = 'WPK-'
OWNER = 'WPKOWN'


def _wipe():
    Lead.query.filter(Lead.company.like(TAG + '%')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like(TAG + '%')).delete(
        synchronize_session=False)
    WeeklyPipelineSnapshot.query.delete(synchronize_session=False)
    db.session.commit()


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        _wipe()
        emp = Employee.query.filter_by(emp_code=OWNER).first() \
            or Employee(emp_code=OWNER)
        emp.name, emp.is_active, emp.must_change_pw = 'Pack Owner', True, False
        emp.email = 'pack.owner@procamgroup.in'
        emp.role, emp.vertical, emp.session_version = 'user', 'Projects', 0
        emp.is_vertical_head, emp.is_super_admin = True, False
        db.session.add(emp)
        for i in range(3):
            db.session.add(Lead(company=f'{TAG}Customer {i}',
                                stage='Quotation', assigned_to=OWNER,
                                estimated_value_inr=100000 * (i + 1)))
        db.session.commit()
        yield
        _wipe()


def test_the_week_label_is_the_iso_week():
    assert pack.week_label(date(2026, 10, 1)) == '2026-W40'
    assert pack.week_label(date(2026, 1, 1)) == '2026-W01'


def test_the_pack_reads_the_same_board_as_the_screen(world):
    report = pack.review_pack(OWNER)
    assert report['report'] == 'weekly_pack'
    assert report['template'] == 'email/weekly_pack.html'
    assert report['figures']['open_count'] >= 3


def test_the_pack_renders(world):
    from app.services import digests
    html = digests.render(pack.review_pack(OWNER))
    assert 'Review pack' in html or 'review' in html.lower()


def test_the_freeze_writes_one_row_per_scope(world):
    out = pack.freeze()
    assert out['rows']
    keys = {r.scope_key for r in pack.frozen(out['week'])}
    assert 'company' in keys


def test_freezing_twice_in_a_week_does_not_duplicate(world):
    first = pack.freeze()
    before = len(pack.frozen(first['week']))
    pack.freeze()
    assert len(pack.frozen(first['week'])) == before


def test_a_frozen_number_does_not_move_when_the_data_does(world):
    out = pack.freeze()
    frozen_before = [r for r in pack.frozen(out['week'])
                     if r.scope_key == 'company'][0].open_count
    live_before = pack.review_pack(OWNER)['figures']['open_count']

    db.session.add(Lead(company=TAG + 'Late Arrival', stage='Quotation',
                        assigned_to=OWNER, estimated_value_inr=999999))
    db.session.commit()

    frozen_after = [r for r in pack.frozen(out['week'])
                    if r.scope_key == 'company'][0].open_count
    assert frozen_after == frozen_before, (
        'the frozen figure moved — the freeze is pointless if it tracks '
        'live data')
    # The live figure, in the same scope as itself, has moved on.
    assert pack.review_pack(OWNER)['figures']['open_count'] == live_before + 1


def test_the_freeze_report_renders_and_names_the_week(world):
    from app.services import digests
    out = pack.freeze()
    report = pack.freeze_report(week=out['week'])
    assert report['week'] == out['week']
    html = digests.render(report)
    assert out['week'] in html
