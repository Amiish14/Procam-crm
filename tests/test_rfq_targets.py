"""
PROCAM-DECISIONS-02NOV Module 3 — the monthly RFQ target.

Two of these matter more than the rest.

`test_no_history_proposes_nothing_rather_than_zero`: the RFQ Chain
is barely used, so a seed that reads `rfqs` would give almost
everybody a target of 0 — which passes every review automatically
and looks like a number somebody chose.

`test_the_seed_does_not_overrule_a_person`: the seed is a starting
point (Module 3.1). A re-run that resets an administrator's edit is
the CRM overruling a decision.
"""
import importlib
import importlib.util
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'rfq-targets-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'RfqTargets12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'targets.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
flask_app.config['WTF_CSRF_ENABLED'] = False
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead

from app.models.access import AccessProfile, DataScope     # noqa: E402
from app.models.rfq_target import (RfqTarget,              # noqa: E402
                                   RfqTargetHistory)
from app.services import rfq_targets as rt                 # noqa: E402

ADMIN = 'RTADM'
SELLER = 'RTSEL'
OTHER = 'RTOTH'
VERTICAL = 'Heavy Transport'

#: A fixed "today", so the four-month window is the same every run.
TODAY = date(2026, 11, 2)


def _emp(code, name, *, vertical=VERTICAL, role='user', admin=False):
    e = Employee.query.filter_by(emp_code=code).first() or \
        Employee(emp_code=code)
    e.name, e.is_active, e.must_change_pw = name, True, False
    e.role, e.session_version, e.is_super_admin = role, 0, admin
    e.vertical = vertical
    db.session.add(e)
    db.session.flush()
    profile = AccessProfile.query.filter_by(emp_code=code).first()
    if profile is None:
        profile = AccessProfile(emp_code=code)
        db.session.add(profile)
    profile.data_scope = DataScope.ALL if admin else DataScope.OWN
    return e


def _client(code=ADMIN, role='admin'):
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['emp_code'] = code
        sess['role'] = role
        sess['session_version'] = 0
    return c


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        RfqTargetHistory.query.delete()
        RfqTarget.query.delete()
        Lead.query.filter(Lead.company.like('RT~%')).delete(
            synchronize_session=False)
        _emp(ADMIN, 'Targets Admin', role='admin', admin=True)
        _emp(SELLER, 'Targets Seller')
        _emp(OTHER, 'Head Office Person', vertical='Management')
        db.session.commit()
        yield


def _rfq_lead(owner, when, vertical=VERTICAL):
    lead = Lead(company='RT~Customer', stage='RFQ Generated',
                source='email', assigned_to=owner,
                procam_vertical=vertical, rfq_date=when,
                created_at=datetime(when.year, when.month, when.day))
    db.session.add(lead)
    db.session.flush()
    return lead


# ══ 3.1 the window and the arithmetic ═══════════════════════════════
def test_the_window_is_four_whole_months_before_this_one(world):
    """A target seeded on the 3rd must not average three days of
    work into a monthly figure."""
    start, end = rt.window(when=TODAY)
    assert (start, end) == (date(2026, 7, 1), date(2026, 11, 1))


def test_the_window_crosses_a_year_correctly(world):
    start, end = rt.window(when=date(2027, 2, 15))
    assert (start, end) == (date(2026, 10, 1), date(2027, 2, 1))


def test_the_month_is_the_ist_month(world):
    """On the last of the month after 18:30 UTC it is already the
    first in Mumbai, and a target seeded then would land in the
    month that had just ended."""
    from app.services import quote_due as qd

    assert rt.month_start() == date(qd.today_ist().year,
                                    qd.today_ist().month, 1)


def test_the_target_is_the_average_plus_twenty_per_cent(world):
    with flask_app.app_context():
        for n in range(8):
            _rfq_lead(SELLER, date(2026, 8, 1) + timedelta(days=n * 7))
        db.session.commit()
        found = rt.proposals(when=TODAY)
        mine = [r for r in found['rows'] if r['emp_code'] == SELLER]
        assert mine, 'the seller has history'
        assert mine[0]['counted'] == 8
        assert mine[0]['average'] == 2.0
        # 2.0 * 1.2 = 2.4, rounded up.
        assert mine[0]['target'] == 3


def test_the_target_rounds_up_not_down(world):
    """Rounding down sets the bar below what they already did."""
    with flask_app.app_context():
        for n in range(4):
            _rfq_lead(SELLER, date(2026, 9, 1) + timedelta(days=n))
        db.session.commit()
        found = rt.proposals(when=TODAY)
        mine = [r for r in found['rows'] if r['emp_code'] == SELLER][0]
        # 4/4 = 1.0/month, +20% = 1.2 -> 2, never 1.
        assert mine['target'] == 2


def test_work_outside_the_window_does_not_count(world):
    with flask_app.app_context():
        _rfq_lead(SELLER, date(2026, 3, 1))     # too old
        _rfq_lead(SELLER, date(2026, 11, 1))    # this month
        db.session.commit()
        found = rt.proposals(when=TODAY)
        assert not [r for r in found['rows']
                    if r['emp_code'] == SELLER]


def test_the_source_of_the_count_is_named(world):
    """A number nobody can trace to a source is a number nobody will
    accept being measured against."""
    with flask_app.app_context():
        _rfq_lead(SELLER, date(2026, 9, 9))
        db.session.commit()
        found = rt.proposals(when=TODAY)
        assert found['source'] in (rt.FROM_RFQS, rt.FROM_STAGE,
                                   rt.FROM_RFQ_DATE)
        assert found['source'] in found['rows'][0]['basis']


def test_no_history_proposes_nothing_rather_than_zero(world):
    """A target of 0 + 20% is 0: it passes every review
    automatically and looks like a number somebody chose."""
    with flask_app.app_context():
        found = rt.proposals(when=TODAY)
        assert not [r for r in found['rows'] if r['emp_code'] == SELLER]
        assert SELLER in [p['emp_code'] for p in found['no_history']]
        assert all(r['target'] > 0 for r in found['rows'])


def test_head_office_is_not_given_a_selling_target(world):
    """Management is where a record goes when nothing else fits, and
    `master_vertical` falls back to it by name — so without the
    selling list every head-office employee gets an RFQ target."""
    with flask_app.app_context():
        found = rt.proposals(when=TODAY)
        everyone = ([r['emp_code'] for r in found['rows']]
                    + [p['emp_code'] for p in found['no_history']])
        assert OTHER not in everyone


def test_somebody_who_has_left_gets_no_target(world):
    """A target for somebody who has gone shows in every review as a
    gap."""
    with flask_app.app_context():
        _rfq_lead('RTGONE', date(2026, 9, 9))
        db.session.commit()
        found = rt.proposals(when=TODAY)
        assert not [r for r in found['rows']
                    if r['emp_code'] == 'RTGONE']


# ══ 3.2 one screen, with history kept ═══════════════════════════════
def test_the_first_write_is_in_the_history_too(world):
    """Otherwise the first edit looks like the only decision anybody
    made."""
    with flask_app.app_context():
        rt.set_target(SELLER, VERTICAL, 5, period=date(2026, 11, 1),
                      actor=ADMIN, reason='seed')
        db.session.commit()
        rows = rt.history(emp_code=SELLER)
        assert len(rows) == 1
        assert rows[0].old_target is None
        assert rows[0].new_target == 5


def test_every_change_keeps_what_it_was(world):
    with flask_app.app_context():
        rt.set_target(SELLER, VERTICAL, 5, period=date(2026, 11, 1),
                      actor=ADMIN)
        db.session.commit()
        rt.set_target(SELLER, VERTICAL, 9, period=date(2026, 11, 1),
                      actor=ADMIN, reason='took over Siemens')
        db.session.commit()
        rows = rt.history(emp_code=SELLER)
        assert [(r.old_target, r.new_target) for r in rows] == \
            [(5, 9), (None, 5)]
        assert rows[0].reason == 'took over Siemens'
        assert rows[0].actor == ADMIN


def test_a_target_is_one_row_per_person_vertical_and_month(world):
    with flask_app.app_context():
        rt.set_target(SELLER, VERTICAL, 5, period=date(2026, 11, 1),
                      actor=ADMIN)
        rt.set_target(SELLER, VERTICAL, 7, period=date(2026, 11, 1),
                      actor=ADMIN)
        db.session.commit()
        assert RfqTarget.query.filter_by(emp_code=SELLER).count() == 1
        assert rt.get(SELLER, VERTICAL, date(2026, 11, 1)).target_rfqs == 7


def test_two_verticals_are_two_targets(world):
    """A person who sells in two verticals has two targets, because
    that is how they are reviewed."""
    with flask_app.app_context():
        rt.set_target(SELLER, 'Heavy Transport', 5,
                      period=date(2026, 11, 1), actor=ADMIN)
        rt.set_target(SELLER, 'Warehousing', 2,
                      period=date(2026, 11, 1), actor=ADMIN)
        db.session.commit()
        assert RfqTarget.query.filter_by(emp_code=SELLER).count() == 2


def test_a_negative_target_is_refused(world):
    with flask_app.app_context():
        with pytest.raises(ValueError):
            rt.set_target(SELLER, VERTICAL, -1, actor=ADMIN)
        with pytest.raises(ValueError):
            rt.set_target(SELLER, VERTICAL, 'lots', actor=ADMIN)
        with pytest.raises(ValueError):
            rt.set_target('', VERTICAL, 3, actor=ADMIN)


def test_an_admin_can_set_a_target_on_the_screen(world):
    r = _client().post('/api/rfq-targets',
                       json={'emp_code': SELLER,
                             'vertical': VERTICAL,
                             'target': 6, 'reason': 'agreed in the '
                                                    'Friday review'})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['target']['target_rfqs'] == 6
    assert r.get_json()['target']['source'] == 'manual'


def test_a_salesperson_cannot_set_their_own_target(world):
    r = _client(SELLER, 'user').post('/api/rfq-targets',
                                     json={'emp_code': SELLER,
                                           'vertical': VERTICAL,
                                           'target': 1})
    assert r.status_code == 403
    assert 'reviewed against' in r.get_json()['error']


def test_a_salesperson_can_read_their_target(world):
    """Somebody who cannot see their target cannot work towards it."""
    with flask_app.app_context():
        rt.set_target(SELLER, VERTICAL, 4, actor=ADMIN)
        db.session.commit()
    r = _client(SELLER, 'user').get('/api/rfq-targets')
    assert r.status_code == 200
    assert any(row['emp_code'] == SELLER
               for row in r.get_json()['rows'])


def test_the_screen_names_the_people_with_no_history(world):
    r = _client().get('/admin/rfq-targets')
    assert r.status_code == 200
    page = r.get_data(as_text=True)
    assert 'no RFQ history' in page
    assert 'A target of 0 plus 20% is 0' in page


# ══ the script ══════════════════════════════════════════════════════
def test_the_seed_does_not_overrule_a_person(world):
    """The seed is a starting point. A re-run that resets an
    administrator's edit is the CRM overruling a decision."""
    with flask_app.app_context():
        for n in range(8):
            _rfq_lead(SELLER, date(2026, 8, 1) + timedelta(days=n * 7))
        db.session.commit()
        period = rt.month_start()
        rt.set_target(SELLER, VERTICAL, 20, period=period,
                      actor=ADMIN, source='manual',
                      reason='stretch target agreed')
        db.session.commit()

        # What --apply does: skip anything already set.
        found = rt.proposals()
        for row in found['rows']:
            if rt.get(row['emp_code'], row['vertical'],
                      found['period']) is not None:
                continue
            rt.set_target(row['emp_code'], row['vertical'],
                          row['target'], period=found['period'],
                          actor='SEED', source='seeded')
        db.session.commit()
        assert rt.get(SELLER, VERTICAL, period).target_rfqs == 20
        assert rt.get(SELLER, VERTICAL, period).source == 'manual'
