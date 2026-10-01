"""scripts/access_review.py — "should this person see everything?",
answered with evidence.

The report is only worth running if it tells a configured profile apart
from a role default (the second is the one nobody remembers granting),
counts what each person actually owns, and can narrow to the short list
of people who see the whole company.
"""
import csv
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'access-review-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'AccessReviewTest12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'accessreview.db'))

import importlib                                            # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead
LeadActivity = _main.LeadActivity

# Appended after the app import: scripts/ holds an email_ingest.py that
# would otherwise shadow the package of the same name.
sys.path.append(os.path.join(_ROOT, 'scripts'))
import access_review as review                              # noqa: E402

from app.models.access import AccessProfile, DataScope      # noqa: E402
from app.models.audit import AuditEvent                     # noqa: E402

REP, HEAD, ADMIN = 'ARVREP', 'ARVHEAD', 'ARVADMIN'
WIDE, NARROW, ODD = 'ARVWIDE', 'ARVNARROW', 'ARVODD'
EVERYONE = (REP, HEAD, ADMIN, WIDE, NARROW, ODD)

SOURCE = open(os.path.join(_ROOT, 'scripts', 'access_review.py')).read()
ACTIVITY_AT = datetime.utcnow() - timedelta(days=4)


def _emp(code, name, role='user', head=False, scope=None, perms=()):
    e = (Employee.query.filter_by(emp_code=code).first()
         or Employee(emp_code=code))
    e.name, e.role, e.vertical = name, role, 'Project Freight'
    e.is_active, e.must_change_pw = True, False
    e.is_super_admin, e.is_vertical_head = False, head
    e.session_version = 0
    db.session.add(e)
    AccessProfile.query.filter_by(emp_code=code).delete()
    if scope:
        db.session.add(AccessProfile(emp_code=code, data_scope=scope,
                                     perms=list(perms)))
    return e


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        # Only ever clear this module's own rows: the suite shares one
        # database, and deleting everything wipes other modules' worlds.
        mine = [l.id for l in Lead.query.filter(Lead.company.like('ARV %'))]
        if mine:
            LeadActivity.query.filter(
                LeadActivity.lead_id.in_(mine)).delete(
                    synchronize_session=False)
            Lead.query.filter(Lead.id.in_(mine)).delete(
                synchronize_session=False)
        Company.query.filter(Company.name.like('ARV %')).delete(
            synchronize_session=False)
        AuditEvent.query.filter(AuditEvent.actor.in_(EVERYONE)).delete(
            synchronize_session=False)

        # Nobody has configured these three: they run on the role default.
        _emp(REP, 'Review Rep')
        _emp(HEAD, 'Review Head', head=True)
        _emp(ADMIN, 'Review Admin', role='admin')
        # These two have a stored Access Matrix profile.
        _emp(WIDE, 'Review Wide', scope=DataScope.ALL,
             perms=['module.quotes', 'reports.action', 'admin.employees'])
        _emp(NARROW, 'Review Narrow', scope=DataScope.OWN,
             perms=['module.quotes'])
        # A name a spreadsheet would run as a formula.
        _emp(ODD, '=HYPERLINK("http://evil.example","Review")')
        db.session.flush()

        for i in range(3):
            db.session.add(Lead(company=f'ARV Lead {i} Ltd', assigned_to=REP,
                                assigned_name=REP, stage='Quoted',
                                procam_vertical='Project Freight'))
        db.session.add(Lead(company='ARV Other Ltd', assigned_to=HEAD,
                            assigned_name=HEAD, stage='Quoted',
                            procam_vertical='Project Freight'))
        for i in range(2):
            db.session.add(Company(name=f'ARV Account {i}', pic_emp_code=REP))
        db.session.flush()

        lead = Lead.query.filter_by(company='ARV Lead 0 Ltd').first()
        db.session.add(LeadActivity(lead_id=lead.id, kind='call',
                                    subject='spoke', occurred_at=ACTIVITY_AT,
                                    performed_by=REP))
        db.session.add(AuditEvent(
            actor=WIDE, action='lead.update', entity_type='lead',
            entity_id='1', occurred_at=datetime.utcnow() - timedelta(days=1)))
        db.session.commit()
        yield


def _rows(**kw):
    with flask_app.app_context():
        return {r['emp_code']: r for r in review.review(**kw)
                if r['emp_code'] in EVERYONE}


# ── where the scope comes from ───────────────────────────────────────
def test_a_stored_profile_is_told_apart_from_a_role_default(world):
    rows = _rows()
    assert rows[WIDE]['scope_from'] == review.FROM_PROFILE
    assert rows[NARROW]['scope_from'] == review.FROM_PROFILE
    # Nobody configured these: their scope is whatever their role implies,
    # which is the case an access review exists to surface.
    for code in (REP, HEAD, ADMIN):
        assert rows[code]['scope_from'] == review.FROM_DEFAULT, code


def test_the_effective_scope_is_reported_not_the_stored_one(world):
    rows = _rows()
    assert rows[REP]['data_scope'] == DataScope.OWN
    assert rows[HEAD]['data_scope'] == DataScope.VERTICAL
    assert rows[HEAD]['is_vertical_head'] is True
    # An admin role resolves to the whole company with no profile at all.
    assert rows[ADMIN]['data_scope'] == DataScope.ALL
    assert rows[WIDE]['data_scope'] == DataScope.ALL
    assert rows[NARROW]['data_scope'] == DataScope.OWN


def test_the_super_admin_flag_outranks_a_stored_profile(world):
    """A profile row on the super admin is not what is in force, so the
    report must not claim the profile is the reason."""
    with flask_app.app_context():
        emp = Employee.query.filter_by(emp_code=NARROW).first()
        emp.is_super_admin = True
        db.session.commit()
    try:
        row = _rows()[NARROW]
        assert row['scope_from'] == review.FROM_SUPER
        assert row['data_scope'] == DataScope.ALL
    finally:
        with flask_app.app_context():
            emp = Employee.query.filter_by(emp_code=NARROW).first()
            emp.is_super_admin = False
            db.session.commit()


# ── the evidence ─────────────────────────────────────────────────────
def test_owned_records_are_counted_per_person(world):
    rows = _rows()
    assert rows[REP]['leads_owned'] == 3
    assert rows[REP]['accounts_owned'] == 2
    assert rows[HEAD]['leads_owned'] == 1
    assert rows[HEAD]['accounts_owned'] == 0
    assert rows[WIDE]['leads_owned'] == 0 and rows[WIDE]['accounts_owned'] == 0


def test_the_last_thing_each_person_did_is_reported(world):
    rows = _rows()
    assert rows[REP]['last_activity'][:10] == str(ACTIVITY_AT)[:10]
    assert rows[REP]['last_activity_days'] in (3, 4)
    # An audited action counts as activity even with no sales log at all.
    assert rows[WIDE]['last_activity']
    assert rows[WIDE]['last_activity_days'] <= 1
    # Somebody who has never done anything is reported as such, not as 0.
    assert rows[ODD]['last_activity'] == ''
    assert rows[ODD]['last_activity_days'] is None


def test_only_permissions_beyond_the_team_baseline_are_listed(world):
    rows = _rows()
    assert rows[WIDE]['extra_perms'] == ['admin.employees', 'reports.action']
    # The baseline itself is not a grant worth reviewing.
    assert rows[NARROW]['extra_perms'] == []
    assert rows[REP]['extra_perms'] == []
    assert 'admin.access' in rows[ADMIN]['extra_perms']


# ── the whole-company filter ─────────────────────────────────────────
def test_scope_all_returns_exactly_the_company_wide_people(world):
    with flask_app.app_context():
        rows = review.review()
    wide = {r['emp_code'] for r in review.filtered(rows, DataScope.ALL)}
    assert wide & set(EVERYONE) == {ADMIN, WIDE}
    vertical = {r['emp_code'] for r in review.filtered(rows,
                                                       DataScope.VERTICAL)}
    assert vertical & set(EVERYONE) == {HEAD}
    own = {r['emp_code'] for r in review.filtered(rows, DataScope.OWN)}
    assert own & set(EVERYONE) == {REP, NARROW, ODD}
    # No filter means everybody.
    assert len(review.filtered(rows, None)) == len(rows)


def test_the_command_line_prints_the_whole_company_review(world, capsys):
    assert review.main(['--scope', 'all']) == 0
    text = capsys.readouterr().out
    assert 'Whole-company access' in text
    assert WIDE in text and ADMIN in text
    assert REP not in text
    # the evidence columns, and the grants spelled out underneath
    assert 'LAST ACTIVITY' in text and 'grants: ' in text


def test_inactive_people_are_left_out_unless_asked_for(world):
    with flask_app.app_context():
        emp = Employee.query.filter_by(emp_code=NARROW).first()
        emp.is_active = False
        db.session.commit()
    try:
        assert NARROW not in _rows()
        assert NARROW in _rows(include_inactive=True)
    finally:
        with flask_app.app_context():
            emp = Employee.query.filter_by(emp_code=NARROW).first()
            emp.is_active = True
            db.session.commit()


# ── the export ───────────────────────────────────────────────────────
def _col(name):
    return review.CSV_COLUMNS.index(name)


def test_the_csv_export_is_safe_to_open_in_a_spreadsheet(world, tmp_path):
    out = tmp_path / 'access.csv'
    with flask_app.app_context():
        rows = [r for r in review.review() if r['emp_code'] in EVERYONE]
    review.write_csv(rows, str(out))
    with open(out, newline='', encoding='utf-8') as fh:
        table = list(csv.reader(fh))
    assert table[0] == list(review.CSV_COLUMNS)
    by_code = {r[0]: r for r in table[1:]}
    assert set(by_code) == set(EVERYONE)
    # A name that Excel would run as a formula is neutralised.
    assert by_code[ODD][1].startswith("'=HYPERLINK")
    assert by_code[REP][_col('leads_owned')] == '3'
    assert by_code[WIDE][_col('extra_perms')] == \
        'admin.employees; reports.action'


# ── read-only ────────────────────────────────────────────────────────
def test_the_report_writes_nothing_to_the_database(world):
    def census():
        with flask_app.app_context():
            return (Employee.query.count(), Lead.query.count(),
                    Company.query.count(), AccessProfile.query.count(),
                    AuditEvent.query.count(), LeadActivity.query.count())

    before = census()
    with flask_app.app_context():
        review.review()
        review.review(include_inactive=True)
    assert census() == before


def test_the_script_never_writes_a_row():
    """Textual, because the guarantee is about the whole file and not
    only the paths a test happens to walk."""
    for forbidden in ('db.session.add', 'db.session.commit',
                      'db.session.delete', 'db.session.merge',
                      '.delete()', 'set_profile('):
        assert forbidden not in SOURCE, forbidden
