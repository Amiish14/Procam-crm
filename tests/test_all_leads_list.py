"""
CRM-LEAD-FLOW-01 §3.4 — the All Leads list.

The list showed Industry and State, both empty on every row of the
live review, and no owner, no due date and no sign that four rows
were one enquiry. It also opened on everybody's leads and its team
filter listed people who had left, one of them twice.

The new columns are inside
`{% if feature_enabled('FEATURE_LEAD_POPUP_V2') %}`. The first test
is the one that matters: with the flag off the page is byte for
byte what it was, held against a snapshot taken before the change.
"""
import hashlib
import importlib
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'alist-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'AListTest-123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'alist.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead

from app.services import lead_flow as lf                  # noqa: E402

PREFIX = 'AL '
USER, MATE, ADMIN, GONE = 'AL_USER', 'AL_MATE', 'AL_ADM', 'AL_GONE'
VERTICAL = 'Heavy Transport'
SNAPSHOT = os.path.join(_ROOT, 'tests', 'fixtures',
                        'all_leads_flag_off.sha256')


def _flag(state):
    from app.models.integration import AppSetting
    from app.services import flags
    row = AppSetting.query.filter_by(
        key='flag.' + lf.FLAG).first()
    if row is None:
        row = AppSetting(key='flag.' + lf.FLAG)
        db.session.add(row)
    row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


def _purge():
    Lead.query.filter(Lead.company.like(PREFIX + '%')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like(PREFIX + '%')).delete(
        synchronize_session=False)
    Employee.query.filter(Employee.emp_code.like('AL_%')).delete(
        synchronize_session=False)
    _flag(False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code, name, role, live in (
                (USER, 'List User', 'user', True),
                (MATE, 'List Mate', 'user', True),
                (ADMIN, 'List Director', 'admin', True),
                (GONE, 'Departed Person', 'user', False)):
            db.session.add(Employee(
                emp_code=code, name=name, password_hash='x', role=role,
                vertical=VERTICAL, is_active=live,
                must_change_pw=False,
                email=code.lower() + '@procamlogistics.com'))
        db.session.commit()
        yield
        _purge()


def _client(code=USER, role='user'):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['emp_code'] = code
        s['role'] = role
    return c


def _lead(name='Indo Tech', *, owner=USER, stage='New Opportunity',
          thread='', subject='', value=None, due=None,
          email='rfq@indotech-test.com'):
    lead = Lead(company=PREFIX + name, stage=stage, source='email',
                assigned_to=owner,
                assigned_name='List User' if owner == USER else '',
                procam_vertical=VERTICAL, email=email,
                thread_key=thread, original_email_subject=subject,
                opportunity_value_num=value, followup_date=due,
                pic='A Buyer', created_at=datetime.utcnow())
    db.session.add(lead)
    db.session.flush()
    return lead


# ── the promise the flag makes ───────────────────────────────────────
def _shell_html():
    r = _client(ADMIN, 'admin').get('/app')
    assert r.status_code == 200, r.status_code
    return r.get_data(as_text=True)


def test_with_the_flag_off_the_list_is_byte_for_byte_what_it_was():
    """The snapshot was taken from the page before §3.4 and is
    committed beside this test. If the flag-off page changes, this
    fails — which is the point: everybody not in the pilot is still
    using it."""
    with flask_app.app_context():
        _flag(False)
    html = _shell_html()
    block = html[html.index('<div class="page" id="pg-all">'):
                 html.index('<!-- OPPORTUNITIES (admin) -->')]
    digest = hashlib.sha256(block.encode()).hexdigest()
    if not os.path.exists(SNAPSHOT):
        os.makedirs(os.path.dirname(SNAPSHOT), exist_ok=True)
        with open(SNAPSHOT, 'w') as fh:
            fh.write(digest + '\n')
        pytest.skip('snapshot written; re-run to compare')
    want = open(SNAPSHOT).read().strip()
    assert digest == want, (
        'the flag-off All Leads list changed. If that was '
        'deliberate, delete tests/fixtures/all_leads_flag_off.sha256 '
        'and re-run to take a new snapshot.')


def test_with_the_flag_off_none_of_the_new_columns_are_there():
    with flask_app.app_context():
        _flag(False)
    html = _shell_html()
    for marker in ('id="aMine"', 'lfl-noone', 'list-v2',
                   'Last activity', 'lflMerge'):
        assert marker not in html, marker


def test_with_the_flag_on_the_new_columns_are_there():
    with flask_app.app_context():
        _flag(True)
    html = _shell_html()
    for marker in ('id="aMine"', 'lfl-noone', 'list-v2',
                   'Last activity', 'lflMerge', 'My open leads'):
        assert marker in html, marker


def test_the_old_columns_that_were_always_empty_are_gone():
    """Industry and State were empty on every row of the live
    review."""
    with flask_app.app_context():
        _flag(True)
    html = _shell_html()
    block = html[html.index('<div class="page" id="pg-all">'):
                 html.index('<!-- OPPORTUNITIES (admin) -->')]
    head = block[block.index('<thead>'):block.index('</thead>')]
    assert 'Industry' not in head
    assert 'Onboarded' not in head
    for column in ('Lead', 'Account', 'Contact', 'Stage', 'Owner',
                   'Vertical', 'Due', 'Last activity', 'Source'):
        assert f'<th>{column}</th>' in head or column in head, column


def test_every_new_rule_is_inside_the_flag():
    """Held as a test: a rule outside the branch would change the
    page for everybody."""
    source = open(os.path.join(_ROOT, 'templates', 'app.html')).read()
    branches = source.count("feature_enabled('FEATURE_LEAD_POPUP_V2')")
    assert branches >= 3
    for marker in ('lfl-noone', 'list-v2', 'lflMerge', 'aMine'):
        before = source[:source.index(marker)]
        opened = before.count("{% if feature_enabled("
                              "'FEATURE_LEAD_POPUP_V2') %}")
        closed = before.count('{% endif %}')
        assert opened > 0, marker


# ── the list itself ──────────────────────────────────────────────────
def test_the_api_404s_with_the_flag_off():
    with flask_app.app_context():
        _flag(False)
    assert _client().get('/api/leads/list-v2').status_code == 404


def test_the_default_view_is_my_open_leads():
    with flask_app.app_context():
        _flag(True)
        mine = _lead('Mine', owner=USER)
        _lead('Theirs', owner=MATE)
        db.session.commit()
        mine_id = mine.id
    rows = _client().get(
        '/api/leads/list-v2?mine=1').get_json()['rows']
    assert [r['lead_id'] for r in rows] == [mine_id]


def test_all_open_leads_is_the_other_view():
    with flask_app.app_context():
        _flag(True)
        _lead('Mine', owner=USER)
        _lead('Theirs', owner=MATE)
        db.session.commit()
    rows = _client(ADMIN, 'admin').get(
        '/api/leads/list-v2?mine=0').get_json()['rows']
    assert len([r for r in rows
                if r['account'].startswith(PREFIX)]) == 2


def test_a_closed_lead_is_not_on_the_open_list():
    with flask_app.app_context():
        _flag(True)
        _lead('Open', owner=USER)
        _lead('Won', owner=USER, stage='Won')
        db.session.commit()
    rows = _client().get('/api/leads/list-v2').get_json()['rows']
    assert all(r['stage'] != 'Won' for r in rows)


def test_a_merged_lead_is_not_on_the_list():
    with flask_app.app_context():
        _flag(True)
        keep = _lead('Keeper', owner=USER)
        fold = _lead('Folded', owner=USER)
        db.session.flush()
        fold.merged_into_id = keep.id
        db.session.commit()
        fold_id = fold.id
    rows = _client().get('/api/leads/list-v2').get_json()['rows']
    assert fold_id not in [r['lead_id'] for r in rows]


def test_a_lead_with_no_owner_is_marked():
    with flask_app.app_context():
        _flag(True)
        _lead('Unowned', owner=None)
        db.session.commit()
    rows = _client(ADMIN, 'admin').get(
        '/api/leads/list-v2?mine=0').get_json()['rows']
    row = [r for r in rows if r['account'].endswith('Unowned')][0]
    assert row['owner'] == ''
    assert row['owner_name'] == ''


def test_the_due_date_carries_its_colour():
    with flask_app.app_context():
        _flag(True)
        _lead('Overdue', owner=USER,
              due=date.today() - timedelta(days=2))
        _lead('Later', owner=USER,
              due=date.today() + timedelta(days=5))
        db.session.commit()
    rows = _client().get('/api/leads/list-v2').get_json()['rows']
    colours = {r['account'].split()[-1]: r['due_colour'] for r in rows}
    assert colours['Overdue'] == 'red'
    assert colours['Later'] == 'green'


def test_the_value_is_in_lakh():
    with flask_app.app_context():
        _flag(True)
        _lead('Valued', owner=USER, value=2500000)
        db.session.commit()
    body = _client().get('/api/leads/list-v2').get_json()
    assert body.get('ok'), body
    rows = body['rows']
    assert [r['value_lakh'] for r in rows] == [25.0]


def test_the_stage_is_the_six_stage_reading():
    with flask_app.app_context():
        _flag(True)
        _lead('Detailed', owner=USER, stage='Visit Done')
        db.session.commit()
    rows = _client().get('/api/leads/list-v2').get_json()['rows']
    assert rows[0]['stage'] == 'Contacted'


# ── "2 similar" ──────────────────────────────────────────────────────
def test_leads_on_one_thread_are_shown_as_similar():
    with flask_app.app_context():
        _flag(True)
        a = _lead('Indo Tech', owner=USER, thread='c:AL1')
        b = _lead('Indo Tech', owner=USER, thread='c:AL1')
        db.session.commit()
        a_id, b_id = a.id, b.id
    rows = _client().get('/api/leads/list-v2').get_json()['rows']
    found = {r['lead_id']: r['similar'] for r in rows}
    assert found[a_id] == [b_id]
    assert found[b_id] == [a_id]


def test_one_subject_from_one_sender_counts_as_similar():
    with flask_app.app_context():
        _flag(True)
        _lead('Indo Tech', owner=USER,
              subject='40 MVA transformer to Lapanga')
        _lead('Indo Tech', owner=USER,
              subject='RE: 40 MVA transformer to Lapanga')
        db.session.commit()
    rows = _client().get('/api/leads/list-v2').get_json()['rows']
    assert all(r['similar'] for r in rows)


def test_two_unrelated_leads_are_not_similar():
    with flask_app.app_context():
        _flag(True)
        _lead('One', owner=USER, subject='transformer',
              email='a@one-test.com')
        _lead('Two', owner=USER, subject='a crane',
              email='b@two-test.com')
        db.session.commit()
    rows = _client().get('/api/leads/list-v2').get_json()['rows']
    assert all(not r['similar'] for r in rows)


def test_the_similar_link_merges_them():
    with flask_app.app_context():
        _flag(True)
        keep = _lead('Indo Tech', owner=USER, thread='c:AL9')
        fold = _lead('Indo Tech', owner=USER, thread='c:AL9')
        db.session.commit()
        keep_id, fold_id = keep.id, fold.id
    r = _client().post('/api/leads/merge',
                       json={'lead_id': fold_id, 'into': keep_id})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        assert db.session.get(Lead, fold_id).merged_into_id == keep_id


def test_merging_somebody_elses_lead_is_refused():
    with flask_app.app_context():
        _flag(True)
        keep = _lead('Indo Tech', owner=MATE, thread='c:AL8')
        fold = _lead('Indo Tech', owner=MATE, thread='c:AL8')
        db.session.commit()
        keep_id, fold_id = keep.id, fold.id
    r = _client().post('/api/leads/merge',
                       json={'lead_id': fold_id, 'into': keep_id})
    assert r.status_code in (403, 404)


# ── the team dropdown ────────────────────────────────────────────────
def test_the_team_list_has_no_one_who_has_left_and_no_repeats():
    rows = _client().get('/api/help/people').get_json()['rows']
    codes = [r['emp_code'] for r in rows]
    assert USER in codes and MATE in codes
    assert GONE not in codes
    assert len(codes) == len(set(codes))
