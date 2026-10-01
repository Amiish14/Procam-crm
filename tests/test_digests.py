"""What the scheduled reports say, and who they say it to.

The digests are the Workbench board arranged for reading, so these
tests are also the record of what a person may be sent: their own
records and nobody else's, with the same money formatting and the same
definition of "overdue" as the screen.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'digests-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'DigestsTest12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'digests.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Lead, Employee, Company = _main.Lead, _main.Employee, _main.Company
LeadActivity = _main.LeadActivity
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.access import AccessProfile, DataScope    # noqa: E402
from app.services import digests                          # noqa: E402
from app.services import sales_rules as rules             # noqa: E402

REP, OTHER, HEAD, IDLE = 'DGREP', 'DGOTHER', 'DGHEAD', 'DGIDLE'
TODAY = rules.business_today()
VERTICAL = 'Project Freight'


def _emp(code, name, *, vertical=VERTICAL, head=False, scope=None):
    e = Employee.query.filter_by(emp_code=code).first() or Employee(
        emp_code=code)
    e.name, e.vertical, e.is_active, e.must_change_pw = name, vertical, True, False
    e.role, e.is_super_admin, e.is_vertical_head = 'user', False, head
    e.email = f'{code.lower()}@digests.invalid'
    e.session_version = 0
    db.session.add(e)
    db.session.flush()
    AccessProfile.query.filter_by(emp_code=code).delete()
    if scope:
        db.session.add(AccessProfile(emp_code=code, data_scope=scope,
                                     perms=[]))
    return e


def _lead(company, owner, **kw):
    l = Lead(company=company, assigned_to=owner, assigned_name=owner,
             stage=kw.pop('stage', 'Quoted'),
             procam_vertical=kw.pop('vertical', VERTICAL),
             created_at=kw.pop('created_at',
                               datetime.utcnow() - timedelta(days=45)))
    for k, v in kw.items():
        setattr(l, k, v)
    db.session.add(l)
    return l


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        # The suite shares one database, so only this module's rows are
        # ever cleared — deleting everything wipes other modules' worlds.
        mine = [l.id for l in Lead.query.filter(Lead.company.like('DG %'))]
        if mine:
            LeadActivity.query.filter(LeadActivity.lead_id.in_(mine)).delete(
                synchronize_session=False)
            Lead.query.filter(Lead.id.in_(mine)).delete(
                synchronize_session=False)
        Company.query.filter(Company.name.like('DG %')).delete(
            synchronize_session=False)
        _emp(REP, 'Digest Rep', scope=DataScope.OWN)
        _emp(OTHER, 'Another Rep', scope=DataScope.OWN)
        _emp(HEAD, 'Vertical Head', head=True, scope=DataScope.VERTICAL)
        _emp(IDLE, 'Nothing To Do', scope=DataScope.OWN)
        db.session.flush()

        rows = {
            # Overdue, and nobody has been in touch for days: this is
            # what both the morning and the evening report must raise.
            'overdue': _lead('DG Overdue Ltd', REP,
                             followup_date=TODAY - timedelta(days=4)),
            # Due today, but a call was logged today — done, so the
            # end-of-day report must leave it alone.
            'handled': _lead('DG Handled Ltd', REP, followup_date=TODAY),
            'future': _lead('DG Future Ltd', REP,
                            followup_date=TODAY + timedelta(days=12)),
            'big': _lead('DG Big Ticket Ltd', REP,
                         followup_date=TODAY + timedelta(days=12),
                         estimated_value_inr=Decimal('25000000')),
            'theirs': _lead('DG Someone Elses Ltd', OTHER,
                            followup_date=TODAY - timedelta(days=6)),
        }
        db.session.flush()
        contacted = {'handled': 0, 'future': 1, 'big': 1, 'theirs': 1,
                     'overdue': 6}
        for key, l in rows.items():
            db.session.add(LeadActivity(
                lead_id=l.id, kind='call', subject='spoke',
                occurred_at=datetime.utcnow() - timedelta(
                    days=contacted[key]),
                performed_by=l.assigned_to))
        db.session.commit()
        return {k: v.id for k, v in rows.items()}


def _report(fn, *args, **kw):
    with flask_app.app_context():
        return fn(*args, **kw)


def _rows(report):
    """Every record line the reader would see, wherever it sits."""
    out = []
    for section in report.get('sections') or []:
        out.extend(section['items'])
    for key in ('high_value', 'attention'):
        if report.get(key):
            out.extend(report[key]['items'])
    return out


def _titles(report):
    return {r['title'] for r in _rows(report)}


# ── the morning report ───────────────────────────────────────────────
def test_the_daily_report_is_the_persons_own_board(world):
    r = _report(digests.daily_action_report, REP)
    assert r['report'] == 'daily' and r['person']['emp_code'] == REP
    assert r['total'] >= 3 and not r['empty']
    assert 'DG Overdue Ltd' in _titles(r)
    assert r['summary']['overdue'] >= 1


def test_every_line_says_why_it_is_there(world):
    for row in _rows(_report(digests.daily_action_report, REP)):
        assert row['reasons'] and all(row['reasons'])
        assert row['route']


def test_the_report_is_filed_in_the_workbenchs_own_groups(world):
    r = _report(digests.daily_action_report, REP)
    keys = [s['key'] for s in r['sections']]
    assert keys, 'the board produced no sections'
    assert set(keys) <= {k for k, _label in rules.GROUPS}
    # ... and in the Workbench's order, so the email and the screen read
    # the same way round.
    order = [k for k, _label in rules.GROUPS]
    assert keys == sorted(keys, key=order.index)
    for s in r['sections']:
        assert s['link'].startswith('/my-work?group=')


def test_one_persons_report_never_contains_anothers_records(world):
    mine = _titles(_report(digests.daily_action_report, REP))
    theirs = _titles(_report(digests.daily_action_report, OTHER))
    assert 'DG Someone Elses Ltd' not in mine
    assert 'DG Overdue Ltd' not in theirs
    assert 'DG Someone Elses Ltd' in theirs
    assert not (mine & theirs)


def test_a_clear_board_produces_an_empty_report(world):
    r = _report(digests.daily_action_report, IDLE)
    assert r['empty'] and r['total'] == 0 and r['sections'] == []


def test_money_is_shown_in_lakh_and_crore(world):
    rows = {r['title']: r for r in
            _rows(_report(digests.daily_action_report, REP))}
    assert rows['DG Big Ticket Ltd']['value'] == '₹ 2.50 Cr'


# ── the evening report ───────────────────────────────────────────────
def test_the_evening_report_raises_only_missed_deadlines(world):
    r = _report(digests.end_of_day_exceptions, REP)
    assert r['report'] == 'exceptions'
    titles = _titles(r)
    assert 'DG Overdue Ltd' in titles
    # Not yet due is not an exception.
    assert 'DG Future Ltd' not in titles
    for row in _rows(r):
        assert set(row['conditions']) & set(digests.EXCEPTION_CONDITIONS)


def test_something_actioned_today_is_not_an_exception_tonight(world):
    """A follow-up due today with a call logged today is done."""
    assert 'DG Handled Ltd' not in _titles(
        _report(digests.end_of_day_exceptions, REP))
    # ... and it was genuinely on the morning board, so the evening
    # report is filtering rather than simply missing it.
    assert 'DG Handled Ltd' in _titles(
        _report(digests.daily_action_report, REP))


def test_the_evening_report_counts_what_it_shows(world):
    r = _report(digests.end_of_day_exceptions, REP)
    assert r['count'] == sum(s['count'] for s in r['sections'])
    assert r['empty'] == (r['count'] == 0)


def test_the_evening_report_sends_the_reader_to_the_list_to_clear(world):
    r = _report(digests.end_of_day_exceptions, REP)
    assert r['link'] == f'/my-work?group={rules.G_TODAY}'


# ── weekly ───────────────────────────────────────────────────────────
def test_the_weekly_report_compares_with_the_week_before(world):
    r = _report(digests.weekly_user, REP)
    assert r['period']['days'] == 7
    assert r['period']['from'] == str(TODAY - timedelta(days=7))
    assert r['deltas'], 'no comparison was produced'
    for key, d in r['deltas'].items():
        assert set(d) == {'now', 'before', 'change'}
        assert d['change'] == d['now'] - d['before']
        assert d['now'] == r['summary'][key]
        assert d['before'] == r['prior_summary'][key]


def test_a_deadline_that_had_not_passed_last_week_shows_as_a_rise(world):
    """The lead fell due four days ago, so it was not overdue a week
    ago and is now — which is exactly what the comparison must say."""
    r = _report(digests.weekly_user, REP)
    assert r['deltas']['overdue']['change'] >= 1


def test_the_weekly_report_calls_out_the_big_deals(world):
    r = _report(digests.weekly_user, REP)
    assert r['high_value']['count'] >= 1
    assert 'DG Big Ticket Ltd' in {i['title'] for i in r['high_value']['items']}


def test_a_head_gets_the_team_rolled_up_by_person(world):
    r = _report(digests.weekly_vertical_head, HEAD)
    owners = {p['owner_code']: p for p in r['people']}
    assert REP in owners and OTHER in owners
    assert owners[REP]['overdue'] >= 1
    assert owners[REP]['total'] >= 3
    assert sum(p['total'] for p in r['people']) == r['total']
    assert r['link'] == '/team-workbench'


def test_a_head_only_sees_their_own_people(world):
    """A rep's weekly report has no team rollup at all, and a head's
    rollup is bounded by the same Scope as the Team Workbench."""
    r = _report(digests.weekly_vertical_head, REP)
    assert {p['owner_code'] for p in r['people']} == {REP}


# ── monthly ──────────────────────────────────────────────────────────
def test_the_management_report_covers_the_whole_company(world):
    r = _report(digests.monthly_management)
    owners = {p['owner_code'] for p in r['people']}
    assert {REP, OTHER} <= owners
    assert r['person'] is None, 'the monthly report is about nobody in '\
                                'particular'
    assert r['deltas'] and r['verticals']
    assert any(v['vertical'] == VERTICAL for v in r['verticals'])


def test_the_management_report_is_not_somebodys_access_profile(world):
    """Built from an explicit company-wide boundary, so editing an
    administrator's profile cannot quietly change the report."""
    with flask_app.app_context():
        sc = digests.company_scope()
    assert sc.unrestricted and sc.emp_code == ''


# ── subjects, templates and links ────────────────────────────────────
def test_every_report_has_a_subject_with_its_date(world):
    for key in digests.REPORTS:
        r = _report(digests.build, key, REP)
        subject = digests.subject_for(r)
        assert str(TODAY) in subject and len(subject) < 120


def test_each_report_renders_to_plain_self_contained_html(world, monkeypatch):
    monkeypatch.setenv('CRM_BASE_URL', 'https://crm.invalid/CRM')
    for key in digests.REPORTS:
        r = _report(digests.build, key, REP)
        with flask_app.app_context():
            html = digests.render(r)
        assert 'https://crm.invalid/CRM' in html, key
        # Nothing is fetched from anywhere: an email client that blocks
        # remote content must still show the whole report.
        for forbidden in ('<img', '<link', '<script', 'url('):
            assert forbidden not in html, f'{key} pulls in {forbidden}'


def test_the_button_goes_to_the_persons_own_work(world, monkeypatch):
    monkeypatch.setenv('CRM_BASE_URL', 'https://crm.invalid/CRM')
    r = _report(digests.daily_action_report, REP)
    with flask_app.app_context():
        html = digests.render(r)
    assert 'https://crm.invalid/CRM/my-work' in html
    assert f'/my-work?group={rules.G_TODAY}' in html


def test_the_comparison_is_explained_where_it_is_shown(world, monkeypatch):
    """A reader who took 'a week ago' literally would misread the week,
    so every template that compares must say what it compared."""
    monkeypatch.setenv('CRM_BASE_URL', '')
    for key in ('weekly_user', 'weekly_head', 'monthly'):
        r = _report(digests.build, key, HEAD)
        with flask_app.app_context():
            html = digests.render(r)
        low = html.lower()
        assert 'snapshot' in low or 'photograph' in low, key


# ── the rules are shared, not copied ─────────────────────────────────
def test_the_digests_read_the_shared_rules_and_the_workbench():
    src = open(os.path.join(_ROOT, 'app', 'services', 'digests.py')).read()
    assert 'from app.services import sales_rules' in src
    assert 'from app.workbench import service as wb' in src
    assert 'from app.access import scope' in src
    # No second definition of money, overdue-ness or the thresholds.
    assert 'format_inr' in src and 'CRORE' not in src
    assert 'timedelta(days=30)' not in src
    assert 'notifier.send' not in src, 'a builder must not send anything'


def test_the_daily_report_covers_every_group_not_just_the_first_page(
        world, monkeypatch):
    """A board bigger than one page used to be sectioned from page one,
    so somebody with hundreds of open records was sent whatever sorted
    to the top and never saw their stale leads or data gaps."""
    import app.services.digests as d
    from app.workbench import service as wb
    with flask_app.app_context():
        sc = d._scope(REP)
        everything, _ageing = wb.lead_items(sc)
        groups_everywhere = {i['group'] for i in everything}
        assert len(groups_everywhere) > 1, 'fixture must span several groups'
        # One item per page: now the first page cannot contain them all,
        # which is the shape a real user with hundreds of records has.
        monkeypatch.setattr(d, '_BOARD_PAGE', 1)
        report = d.daily_action_report(REP)
        sections = {s['key'] for s in report['sections']}
    assert groups_everywhere <= sections, groups_everywhere - sections
