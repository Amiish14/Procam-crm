"""
CRM-COCKPIT-02 §6 — the pending items, and the sweeps around them.

These tests hold the rules the §6 script must not break under me:
nothing is deleted, a named lead that is not in the database is
reported rather than skipped silently, an owner name that matches
nobody refuses rather than writing a wrong code, and every write can
be undone.

The one I care most about is `test_the_two_unflagged_leads_are_reported_not_just_closed`:
§6.1 asks for two leads to be marked blocked, and closing them without
fixing why the register did not match them would leave the next
enquiry from the same client coming straight through.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'close-pending-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'ClosePending12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'closepending.db'))

import importlib                                          # noqa: E402
import importlib.util                                     # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead

from app.models.restriction import (BLOCKED,              # noqa: E402
                                    ClientRestriction)
from app.services import client_restrictions as restrictions  # noqa: E402


def _load(name):
    """A script, by path. scripts/ holds an `email_ingest.py` that
    shadows the package of the same name if the directory goes on
    sys.path."""
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(_ROOT, 'scripts', f'{name}.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


closing = _load('2026_11_01_close_pending')
unowned = _load('unowned_sweep')

TAG = 'CP-'
ACTOR = 'CPADM'


def _emp(code, name, **kw):
    e = Employee.query.filter_by(emp_code=code).first() or \
        Employee(emp_code=code)
    e.name, e.is_active, e.must_change_pw = name, True, False
    e.role = kw.get('role', 'user')
    e.session_version, e.is_super_admin = 0, kw.get('admin', False)
    e.vertical = kw.get('vertical', 'Heavy Transport')
    db.session.add(e)
    db.session.flush()
    return e


def _wipe():
    ids = [l.id for l in Lead.query.filter(
        db.or_(Lead.company.like(TAG + '%'),
               Lead.company.like('%Walchand%'))).all()]
    if ids:
        Lead.query.filter(Lead.id.in_(ids)).delete(
            synchronize_session=False)
    rows = [r.id for r in ClientRestriction.query.filter(
        db.or_(ClientRestriction.company_name.like('%Walchand%'),
               ClientRestriction.company_name.like(TAG + '%'))).all()]
    if rows:
        ClientRestriction.query.filter(
            ClientRestriction.id.in_(rows)).delete(
                synchronize_session=False)
    Company.query.filter(Company.name.like(TAG + '%')).delete(
        synchronize_session=False)
    db.session.commit()
    restrictions.cache_clear()


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        _wipe()
        _emp(ACTOR, 'CP Admin', role='admin', admin=True)
        db.session.commit()
        yield
        _wipe()


def _walchand_block():
    row = ClientRestriction(
        status=BLOCKED, company_name='Walchandnagar Industries Limited',
        name_key=restrictions.normalise(
            'Walchandnagar Industries Limited'),
        reason_category='Payment default / outstanding',
        reason_detail='Outstanding, unresolved.',
        recommended_by=ACTOR, recommended_at=datetime.utcnow(),
        approved_by=ACTOR, approved_at=datetime.utcnow(),
        scope='entity')
    row.aliases = ['WIL']
    row.domains, row.emails = [], []
    db.session.add(row)
    db.session.commit()
    restrictions.cache_clear()
    return row


def _lead_with_id(lead_id, company, **kw):
    """A lead at a chosen id — §6 names leads by number."""
    lead = Lead(id=lead_id, company=company,
                stage=kw.pop('stage', 'RFQ Generated'),
                source='manual', assigned_to=kw.pop('owner', ACTOR),
                created_at=datetime(2026, 10, 25), **kw)
    db.session.add(lead)
    db.session.commit()
    return lead


# ── §6.1 ────────────────────────────────────────────────────────────
def test_the_named_leads_are_closed_as_lost_others_blocked_client(world):
    _walchand_block()
    _lead_with_id(12447, 'Walchandnagar Industries Ltd')
    found = closing.plan()
    mine = [r for r in found['walchand'] if r['id'] == 12447]
    assert mine and mine[0]['already'] is False


def test_a_named_lead_that_is_not_here_is_reported(world):
    """§6 names leads by number from the production database. A
    number that is not here is said out loud, not skipped."""
    _walchand_block()
    found = closing.plan()
    assert 12447 in found['walchand_missing']


def test_the_two_unflagged_leads_are_reported_not_just_closed(world):
    """§6.1 asks for `restriction = blocked` on #12441 and #12355.

    A lead the register does not match is not fixed by closing it:
    the next enquiry from the same client comes straight through. So
    the script reports why the register missed it and says what to
    change, rather than quietly closing the lead and reporting
    success.
    """
    _walchand_block()
    # A name the register cannot match — a different trading name.
    _lead_with_id(12441, TAG + 'Walchand Nagar Engineering Works')
    found = closing.plan()
    rows = [r for r in found['unflagged'] if r['id'] == 12441]
    assert rows, 'the lead has to be examined at all'
    assert rows[0]['now_blocked'] is False
    assert rows[0]['matched_on']


def test_a_lead_the_register_does_match_is_reported_as_blocked(world):
    _walchand_block()
    _lead_with_id(12355, 'Walchandnagar Industries Ltd')
    found = closing.plan()
    rows = [r for r in found['unflagged'] if r['id'] == 12355]
    assert rows and rows[0]['now_blocked'] is True


def test_closing_them_deletes_nothing(world):
    _walchand_block()
    lead = _lead_with_id(12447, 'Walchandnagar Industries Ltd')
    before = Lead.query.count()
    from app.services import bulk_log

    with bulk_log.batch('test.close', actor=ACTOR) as log:
        log.set(lead, 'stage', 'Lost')
        log.set(lead, 'lost_reason', closing.BLOCKED_LOST_REASON)
        name = log.name
    db.session.commit()
    assert Lead.query.count() == before
    assert lead.lost_reason == 'Others: blocked client'
    bulk_log.undo(name)
    db.session.commit()
    assert lead.stage == 'RFQ Generated'


# ── §6.2 ────────────────────────────────────────────────────────────
def test_godrej_says_so_when_nothing_is_blocked(world):
    found = closing.plan()
    assert any('Godrej' in p for p in found['problems'])


def test_an_open_godrej_lead_is_listed_with_whether_it_is_blocked(world):
    _lead_with_id(11851, TAG + 'Godrej and Boyce')
    found = closing.plan()
    rows = [r for r in found['godrej'] if r['id'] == 11851]
    assert rows and rows[0]['blocked'] is False


# ── §6.5 ────────────────────────────────────────────────────────────
def test_a_vertical_correction_knows_when_it_is_already_done(world):
    _lead_with_id(10110, TAG + 'RVNL', procam_vertical='Heavy Transport')
    found = closing.plan()
    rows = [r for r in found['verticals'] if r['id'] == 10110]
    assert rows and rows[0]['already'] is True


def test_a_vertical_correction_reports_what_it_would_change(world):
    _lead_with_id(12218, TAG + 'Prysmian',
                  procam_vertical='General Transport')
    found = closing.plan()
    rows = [r for r in found['verticals'] if r['id'] == 12218]
    assert rows[0]['from'] == 'General Transport'
    assert rows[0]['to'] == 'Project Freight'


def test_every_vertical_correction_targets_one_of_the_four(world):
    """The 31 October decision: four master verticals only."""
    four = ('Heavy Transport', 'Project Freight', 'Installation',
            'Warehousing')
    for _id, _who, vertical in closing.VERTICAL_FIXES:
        assert vertical in four, vertical


# ── §6.6 ────────────────────────────────────────────────────────────
def test_an_owner_name_that_matches_nobody_refuses(world):
    """A wrong employee code in an owner column is silent: the lead
    simply stops appearing on anybody's list."""
    found = closing.plan()
    assert any('no active employee called' in p
               for p in found['problems'])


def test_an_owner_name_is_resolved_against_the_employee_master(world):
    _emp('CPJFX', 'James Francis Xavier')
    db.session.commit()
    emp, problem = closing._employee_by_name('James Francis Xavier')
    assert problem is None and emp.emp_code == 'CPJFX'


def test_two_people_with_the_same_name_refuse_rather_than_guess(world):
    _emp('CPTD1', 'Tamal Das')
    _emp('CPTD2', 'Tamal Das Roy')
    db.session.commit()
    emp, problem = closing._employee_by_name('Tamal Das')
    assert emp is not None and emp.emp_code == 'CPTD1', \
        'an exact match wins over a contains match'
    emp2, problem2 = closing._employee_by_name('Tamal')
    assert emp2 is None and '2 people match' in problem2


# ── §6.7 ────────────────────────────────────────────────────────────
def test_an_auction_with_no_deadline_says_so_rather_than_flagging(world):
    _lead_with_id(12490, TAG + 'Indo Tech', portal_key='RA-57103')
    found = closing.plan()
    rows = [r for r in found['auctions'] if r['id'] == 12490]
    assert rows and rows[0]['no_deadline'] is True
    assert rows[0]['would_flag'] is False


def test_an_auction_with_a_bid_recorded_is_left_alone(world):
    _lead_with_id(12491, TAG + 'Indo Tech Bid', portal_key='RA-56609',
                  quote_due_at=datetime(2026, 10, 20),
                  bid_outcome='not_bid')
    found = closing.plan()
    rows = [r for r in found['auctions'] if r['id'] == 12491]
    assert rows and rows[0]['would_flag'] is False


def test_a_closed_auction_with_nothing_recorded_would_be_flagged(world):
    _lead_with_id(12492, TAG + 'Indo Tech Open', portal_key='RA-56715',
                  quote_due_at=datetime(2026, 10, 20))
    found = closing.plan()
    rows = [r for r in found['auctions'] if r['id'] == 12492]
    assert rows and rows[0]['would_flag'] is True


def test_the_five_auction_numbers_are_the_ones_the_prompt_names(world):
    assert closing.INDO_TECH_AUCTIONS == (
        'RA-56609', 'RA-56715', 'RA-56719', 'RA-56824', 'RA-57103')


# ── §4.5 the 08:30 unowned check ────────────────────────────────────
def test_an_unowned_open_lead_is_found(world):
    _lead_with_id(13001, TAG + 'No Owner', owner=None)
    assert 13001 in [l.id for l in unowned.unowned()]


def test_an_owned_lead_is_not(world):
    _lead_with_id(13002, TAG + 'Owned', owner=ACTOR)
    assert 13002 not in [l.id for l in unowned.unowned()]


def test_a_closed_lead_with_no_owner_is_not_chased(world):
    _lead_with_id(13003, TAG + 'Closed No Owner', owner=None,
                  stage='Lost')
    assert 13003 not in [l.id for l in unowned.unowned()]


def test_every_unowned_lead_gets_a_rule_and_a_name(world):
    _lead_with_id(13004, TAG + 'To Assign', owner=None)
    rows, why = unowned.plan()
    mine = [r for r in rows if r[0].id == 13004]
    assert mine, 'it has to be in the plan'
    _lead, code, rule, unsure = mine[0]
    assert code, 'the backstop is the floor — never nobody'
    assert rule, 'and the rule that chose them is recorded'
