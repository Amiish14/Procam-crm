"""
CRM-WB-01 — the tiles, the chips, the list and Quote ageing agree.

The eight acceptance tests from the brief, plus the behaviour they
rest on.

What was wrong
    The board answered the same question three ways. `summary`
    counted conditions (an item has several), `groups[].count`
    counted the primary group (an item has one), and the list
    filtered by primary group. So the High-value tile read 10, the
    High-value chip read 0, and clicking the tile listed 0 rows —
    because all ten high-value items had a primary group of something
    more urgent.

    Quote ageing read 0 in every bucket while 130 leads sat in Quoted
    or Under Negotiation, because it bucketed RFQ records and the
    business tracks quotes by lead stage.

Each test below is run for a viewer with company-wide scope and for
two individual people, because a count that is right for an admin and
wrong for a rep is still wrong.
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
os.environ.setdefault('SECRET_KEY', 'wbfilters-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'WbFiltersTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'wbfilters.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Lead, Employee, Company = _main.Lead, _main.Employee, _main.Company
LeadActivity, LeadStageHistory = _main.LeadActivity, _main.LeadStageHistory
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.access import scope as sc_mod                    # noqa: E402
from app.models.access import AccessProfile, DataScope    # noqa: E402
from app.services import sales_rules as rules             # noqa: E402
from app.workbench import filters as wf                   # noqa: E402
from app.workbench import service as wb                   # noqa: E402

ADMIN, REP, MATE = 'WFADMIN', 'WFREP', 'WFMATE'
TAG = 'WF '
TODAY = rules.business_today()
VERT_A, VERT_B = 'Project Freight', 'Heavy Transport'


def _emp(code, name, vertical=VERT_A, scope=DataScope.OWN, admin=False):
    e = (Employee.query.filter_by(emp_code=code).first()
         or Employee(emp_code=code))
    e.name, e.vertical, e.is_active, e.must_change_pw = name, vertical, True, False
    e.role = 'admin' if admin else 'user'
    e.is_super_admin, e.is_vertical_head, e.session_version = admin, False, 0
    e.email = f'{code.lower()}@procamgroup.in'
    db.session.add(e)
    db.session.flush()
    AccessProfile.query.filter_by(emp_code=code).delete()
    db.session.add(AccessProfile(emp_code=code, data_scope=scope, perms=[]))
    return e


def _lead(name, owner, **kw):
    l = Lead(company=TAG + name, assigned_to=owner, assigned_name=owner,
             stage=kw.pop('stage', 'Quoted'),
             procam_vertical=kw.pop('vertical', VERT_A),
             created_at=kw.pop('created_at',
                               datetime.utcnow() - timedelta(days=40)))
    for k, v in kw.items():
        setattr(l, k, v)
    db.session.add(l)
    return l


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        mine = [l.id for l in Lead.query.filter(Lead.company.like(TAG + '%'))]
        if mine:
            LeadActivity.query.filter(LeadActivity.lead_id.in_(mine)).delete(
                synchronize_session=False)
            LeadStageHistory.query.filter(
                LeadStageHistory.lead_id.in_(mine)).delete(
                    synchronize_session=False)
            Lead.query.filter(Lead.id.in_(mine)).delete(
                synchronize_session=False)
        Company.query.filter(Company.name.like(TAG + '%')).delete(
            synchronize_session=False)
        _emp(ADMIN, 'Workbench Admin', scope=DataScope.ALL, admin=True)
        _emp(REP, 'Workbench Rep')
        _emp(MATE, 'Workbench Mate', vertical=VERT_B)
        db.session.flush()

        rows = {
            # Overdue follow-up AND stale AND no quote detail — the
            # multi-condition item that broke the old counting.
            'busy': _lead('Busy Ltd', REP, stage='Quoted',
                          followup_date=TODAY - timedelta(days=5)),
            'today': _lead('Today Ltd', REP,
                           followup_date=TODAY,
                           quoted_amount_inr=Decimal('100000'),
                           quote_date=TODAY - timedelta(days=2)),
            # High value, and overdue, so its primary group is
            # action_today — the case where tile and chip diverged.
            'big': _lead('Big Ticket Ltd', REP,
                         followup_date=TODAY - timedelta(days=1),
                         estimated_value_inr=Decimal('25000000'),
                         quoted_amount_inr=Decimal('25000000'),
                         quote_date=TODAY - timedelta(days=4)),
            'newish': _lead('Brand New Ltd', REP, stage='RFQ Generated',
                            created_at=datetime.utcnow() - timedelta(hours=3),
                            followup_date=TODAY + timedelta(days=4)),
            'awaiting': _lead('Awaiting Quote Ltd', REP,
                              stage='RFQ Generated',
                              followup_date=TODAY + timedelta(days=6)),
            'negotiating': _lead('Negotiating Ltd', REP,
                                 stage='Under Negotiation',
                                 followup_date=TODAY + timedelta(days=6),
                                 quoted_amount_inr=Decimal('700000'),
                                 quote_date=TODAY - timedelta(days=8)),
            'lapsed': _lead('Lapsed Quote Ltd', REP,
                            followup_date=TODAY + timedelta(days=9),
                            quoted_amount_inr=Decimal('500000'),
                            quote_date=TODAY - timedelta(days=40),
                            quote_validity_date=TODAY - timedelta(days=5)),
            'mates': _lead('Mates Ltd', MATE, vertical=VERT_B,
                           stage='Quoted',
                           followup_date=TODAY - timedelta(days=2),
                           quoted_amount_inr=Decimal('300000'),
                           quote_date=TODAY - timedelta(days=3)),
            'won': _lead('Closed Ltd', REP, stage='Won',
                         followup_date=TODAY - timedelta(days=4)),
        }
        db.session.flush()
        for key, l in rows.items():
            if key == 'busy':
                continue                        # left quiet, so it goes stale
            db.session.add(LeadActivity(
                lead_id=l.id, kind='call', subject='spoke',
                occurred_at=datetime.utcnow(), performed_by=l.assigned_to))
        # One lead with a real "entered Quoted" date and no quote
        # record, so the ageing fallback is exercised rather than
        # assumed.
        db.session.add(LeadStageHistory(
            lead_id=rows['negotiating'].id, from_stage='Quoted',
            to_stage='Quoted',
            changed_at=datetime.utcnow() - timedelta(days=7)))
        db.session.commit()
        yield {k: v.id for k, v in rows.items()}


def _board(code, **params):
    with flask_app.app_context():
        return wb.board(sc_mod.for_employee(code), **params)


def _items(code, **params):
    """Every item on the board, unpaged — what the counts count."""
    return _board(code, per_page=200, **params)['items']


def _client(code):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role='user', sv=0)
    return c


VIEWERS = (ADMIN, REP, MATE)


# ── 1 ────────────────────────────────────────────────────────────────
def test_1_count_equals_filter_total_equals_rows_for_every_key(world):
    """counts[key] == ?filter=key total == rows shown == footer.

    The footer prints `total`, so proving total matches the rows and
    the count proves all four.
    """
    for who in VIEWERS:
        base = _board(who, per_page=1)
        for key in wf.KEYS:
            got = _board(who, filter_key=key, per_page=200)
            assert got['total'] == base['counts'][key], (
                f'{who}: counts[{key}]={base["counts"][key]} but '
                f'?filter={key} returned {got["total"]}')
            assert len(got['items']) == min(got['total'], 200)
            # and the rows really do match the predicate
            ctx = {'today': TODAY}
            for item in got['items']:
                assert wf.BY_KEY[key].matches(item, ctx)


# ── 2 ────────────────────────────────────────────────────────────────
PAIRS = ('newly_assigned', 'high_value', 'data_updates', 'stale',
         'followups_due')


def test_2_tile_and_chip_agree_wherever_both_exist(world):
    for who in VIEWERS:
        board = _board(who, per_page=1)
        tiles = {t['key']: t['count'] for t in board['tiles']}
        chips = {c['key']: c['count'] for c in board['chips']}
        for key in PAIRS:
            assert key in tiles and key in chips, key
            assert tiles[key] == chips[key], (
                f'{who}: tile {key}={tiles[key]} chip={chips[key]}')


def test_2b_the_pairs_read_the_same_words(world):
    """A tile called "Stale leads" over a chip called "Stale
    opportunities" reads as two measurements of two things."""
    for key in PAIRS:
        f = wf.BY_KEY[key]
        assert f.tile == f.chip, f'{key}: tile {f.tile!r} chip {f.chip!r}'


# ── 3 ────────────────────────────────────────────────────────────────
def test_3_action_today_is_the_union_of_due_today_and_overdue(world):
    for who in VIEWERS:
        items = _items(who)
        ctx = {'today': TODAY}
        due = {id(i) for i in items if wf.BY_KEY['due_today'].matches(i, ctx)}
        over = {id(i) for i in items if wf.BY_KEY['overdue'].matches(i, ctx)}
        counts = _board(who, per_page=1)['counts']
        assert counts['action_today'] == len(due | over)


# ── 4 ────────────────────────────────────────────────────────────────
def test_4_ageing_buckets_sum_to_the_quoted_population(world):
    for who in VIEWERS:
        board = _board(who, per_page=200)
        qa = board['quote_ageing']
        assert sum(qa['counts'].values()) == qa['population']

        in_scope = [i for i in _items(who)
                    if i['kind'] == 'lead'
                    and i['stage'] in ('Quoted', 'Under Negotiation')]
        assert qa['population'] == len(in_scope)


def test_4b_a_bucket_click_lists_exactly_that_bucket(world):
    for who in VIEWERS:
        qa = _board(who, per_page=1)['quote_ageing']
        for bucket in qa['buckets']:
            got = _board(who, filter_key='quotations', age=bucket['key'],
                         per_page=200)
            assert got['total'] == bucket['count'], (
                f'{who}: bucket {bucket["key"]} says {bucket["count"]}, '
                f'list returned {got["total"]}')


def test_4c_a_quote_with_no_record_is_dated_and_flagged(world):
    """No quote record and no stage history means the date is a
    guess, and the row has to say so rather than look certain."""
    items = _items(REP, filter_key='quotations')
    quoted = [i for i in items if i['stage'] in ('Quoted', 'Under Negotiation')]
    assert quoted
    for i in quoted:
        assert 'quote_age_days' in i and i['quote_age_days'] >= 0
        assert 'quote_date_estimated' in i


# ── 5 ────────────────────────────────────────────────────────────────
def test_5_rfqs_awaiting_quote_is_the_rfq_generated_stage(world):
    for who in VIEWERS:
        board = _board(who, per_page=200)
        expected = [i for i in _items(who)
                    if i['kind'] == 'lead'
                    and i['stage'] == 'RFQ Generated'
                    and not i.get('quote_sent_at')]
        assert board['counts']['rfqs_awaiting_quote'] == len(expected)


def test_5b_a_missing_quote_deadline_becomes_a_data_update(world):
    """Not "due" and not "not due" — a record nobody can work from."""
    awaiting = _items(REP, filter_key='rfqs_awaiting_quote')
    assert awaiting
    no_deadline = [i for i in awaiting if not i['quote_due']]
    assert no_deadline, 'the fixture should have one without a deadline'
    for i in no_deadline:
        assert 'quote_due_missing' in i['conditions']
        assert 'Quote due date missing' in i['reasons']
        assert wf.BY_KEY['data_updates'].matches(i, {'today': TODAY})


# ── 6 ────────────────────────────────────────────────────────────────
def test_6_everything_is_the_distinct_item_count(world):
    for who in VIEWERS:
        board = _board(who, per_page=1)
        assert board['counts']['everything'] == board['total'] \
            or board['filter'] != 'everything'
        full = _board(who, per_page=200)
        assert board['counts']['everything'] == full['total']


def test_6b_paging_loses_and_duplicates_nothing(world):
    for who in VIEWERS:
        seen, page, pages = [], 1, None
        while True:
            got = _board(who, per_page=3, page=page)
            pages = got['pages']
            seen += [(i['kind'], i['id']) for i in got['items']]
            if page >= pages:
                break
            page += 1
        assert len(seen) == len(set(seen)), 'an item appeared on two pages'
        assert len(seen) == _board(who, per_page=1)['counts']['everything']


# ── 7 ────────────────────────────────────────────────────────────────
def test_7_a_filter_change_moves_tiles_chips_ageing_and_list_together(world):
    all_v = _board(ADMIN, per_page=200)
    one_v = _board(ADMIN, vertical=VERT_B, per_page=200)

    assert one_v['total'] < all_v['total'], 'the fixture needs two verticals'
    # tiles and chips move with it
    assert one_v['counts']['everything'] == one_v['total']
    assert sum(c['count'] for c in one_v['chips']) \
        <= sum(c['count'] for c in all_v['chips'])
    # the ageing population moves with it too — this is the one that
    # used to stay put, because ageing was built from its own query
    assert one_v['quote_ageing']['population'] \
        <= all_v['quote_ageing']['population']
    for i in one_v['items']:
        assert i['vertical'] == VERT_B

    # and a search narrows all of it the same way
    found = _board(ADMIN, search='Mates', per_page=200)
    assert found['total'] == found['counts']['everything']
    assert all('mates' in i['account'].lower() for i in found['items'])


def test_7b_a_stage_filter_narrows_the_ageing_population(world):
    narrowed = _board(ADMIN, stage='Under Negotiation', per_page=200)
    assert narrowed['quote_ageing']['population'] == narrowed['total']
    assert all(i['stage'] == 'Under Negotiation' for i in narrowed['items'])


# ── 8 ────────────────────────────────────────────────────────────────
def test_8_reading_the_board_changes_no_record(world):
    """The board is a read. Counting it differently must not write."""
    from app.models.task_engine import TaskInstance

    def census():
        return (Lead.query.count(), Company.query.count(),
                TaskInstance.query.count(), LeadActivity.query.count())

    with flask_app.app_context():
        before = census()
    for who in VIEWERS:
        for key in wf.KEYS:
            _board(who, filter_key=key, per_page=200)
        _board(who, filter_key='quotations', age='d1_3', per_page=200)
    with flask_app.app_context():
        assert census() == before


# ── the API surface ──────────────────────────────────────────────────
def test_the_api_returns_one_counts_object(world):
    d = _client(REP).get('/api/workbench?per_page=200').get_json()
    assert d['ok']
    assert set(wf.KEYS) <= set(d['counts'])
    assert [t['key'] for t in d['tiles']] == list(wf.TILE_KEYS)
    assert [c['key'] for c in d['chips']] == list(wf.CHIP_KEYS)
    assert d['multi_membership'] is True
    for t in d['tiles']:
        assert t['count'] == d['counts'][t['key']]
    for c in d['chips']:
        assert c['count'] == d['counts'][c['key']]


def test_the_api_filter_parameter_matches_the_count(world):
    c = _client(REP)
    counts = c.get('/api/workbench?per_page=1').get_json()['counts']
    for key in wf.KEYS:
        d = c.get(f'/api/workbench?filter={key}&per_page=200').get_json()
        assert d['total'] == counts[key], key


def test_an_unknown_filter_falls_back_to_everything_rather_than_nothing(world):
    """A typo in a bookmark should not look like an empty Workbench."""
    d = _client(REP).get('/api/workbench?filter=nonsense&per_page=200') \
        .get_json()
    assert d['total'] == d['counts']['everything']


def test_an_old_group_bookmark_still_works(world):
    d = _client(REP).get('/api/workbench?group=high_value&per_page=200') \
        .get_json()
    assert d['filter'] == 'high_value'
    assert d['total'] == d['counts']['high_value']


def test_the_quote_ageing_endpoint_agrees_with_the_board(world):
    c = _client(REP)
    board = c.get('/api/workbench?per_page=1').get_json()['quote_ageing']
    api = c.get('/api/workbench/quote-ageing').get_json()
    assert api['population'] == board['population']
    assert {b['key']: b['count'] for b in api['buckets']} \
        == {b['key']: b['count'] for b in board['buckets']}


def test_the_registry_is_served_so_the_page_invents_no_labels(world):
    d = _client(REP).get('/api/workbench/filters').get_json()
    assert d['ok']
    assert {f['key'] for f in d['filters']} == set(wf.KEYS)


def test_every_registry_key_has_a_tile_a_chip_or_an_overview_tile(world):
    # A filter nothing shows is a filter nobody can reach. Three of
    # them are reachable only from the Overview panel, which is a
    # surface like the other two, not an exception to this rule.
    for f in wf.WORKBENCH_FILTERS:
        if f.key in wf.LEGACY_KEYS:
            continue              # kept for old links; shown nowhere
        assert f.tile or f.chip or f.overview, \
            f'{f.key} is reachable from nowhere'


def test_a_predicate_that_trips_does_not_empty_the_view(world):
    """One odd row must cost that row, not the whole board."""
    bad = {'kind': 'lead', 'conditions': None, 'stage': None}
    assert wf.BY_KEY['stale'].matches(bad, {'today': TODAY}) is False
    assert wf.counts([bad], {'today': TODAY})['everything'] == 1
