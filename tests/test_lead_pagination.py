"""The lead list: pagination, search and ordering.

The cap was 300 rows. The worse half of that bug was the search: it
ran in Python *after* the limit, so searching for a company older than
the newest 300 leads returned "nothing found" — a wrong answer rather
than a truncated one.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'pagination-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Pagination12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'pagination.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee, Lead = _main.Employee, _main.Lead
flask_app.config['WTF_CSRF_ENABLED'] = False

TAG = 'PAG-'
ADMIN = 'PAGADM'
#: More than the old cap, so the records that used to be unreachable
#: are actually in the fixture.
HOW_MANY = 340


def _wipe():
    Lead.query.filter(Lead.company.like(TAG + '%')).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture(scope='module')
def world():
    with flask_app.app_context():
        db.create_all()
        _wipe()
        emp = Employee.query.filter_by(emp_code=ADMIN).first() \
            or Employee(emp_code=ADMIN)
        emp.name, emp.is_active, emp.must_change_pw = 'Pager', True, False
        emp.email, emp.role, emp.session_version = \
            'pager@procamgroup.in', 'admin', 0
        emp.is_super_admin = True
        db.session.add(emp)

        base = datetime(2026, 1, 1, 9, 0)
        for i in range(HOW_MANY):
            db.session.add(Lead(
                company=f'{TAG}Customer {i:04d}', stage='New',
                assigned_to=ADMIN,
                created_at=base + timedelta(hours=i),
                received_at=base + timedelta(hours=i)))
        # The needle: oldest, so the old code could never find it.
        db.session.add(Lead(
            company=f'{TAG}Needle Engineering Ltd', stage='New',
            assigned_to=ADMIN,
            created_at=base - timedelta(days=5),
            received_at=base - timedelta(days=5)))
        db.session.commit()
        yield
        _wipe()


@pytest.fixture()
def client(world):
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['emp_code'] = ADMIN
        sess['role'] = 'admin'
        sess['session_version'] = 0
    return c


# ── the cap ──────────────────────────────────────────────────────────
def test_the_total_is_reported_even_on_the_legacy_shape(client):
    resp = client.get('/api/leads')
    assert resp.status_code == 200
    assert isinstance(resp.get_json(), list), (
        'a caller that asked for no page must still get a bare array')
    assert int(resp.headers['X-Total-Count']) >= HOW_MANY + 1


def test_asking_for_a_page_returns_a_count_to_show(client):
    body = client.get('/api/leads?page=1&size=50').get_json()
    assert body['page'] == 1 and body['size'] == 50
    assert len(body['items']) == 50
    assert body['total'] >= HOW_MANY + 1
    assert body['pages'] == max(1, (body['total'] + 49) // 50)


def test_every_record_is_reachable_by_paging(client):
    seen, page = set(), 1
    while True:
        body = client.get(f'/api/leads?page={page}&size=100'
                          '&q=PAG-').get_json()
        for item in body['items']:
            seen.add(item['id'])
        if page >= body['pages']:
            break
        page += 1
    assert len(seen) == HOW_MANY + 1, (
        f'{len(seen)} of {HOW_MANY + 1} reachable — the cap is still there')


def test_one_request_cannot_ask_for_everything(client):
    body = client.get('/api/leads?page=1&size=99999').get_json()
    assert len(body['items']) <= _main.MAX_PAGE_SIZE


# ── the search, which is the real bug ────────────────────────────────
def test_the_oldest_record_is_findable(client):
    """It sorts last, so the old code — which searched the first 300
    rows it had already fetched — could never return it."""
    body = client.get('/api/leads?page=1&size=20&q=Needle').get_json()
    assert body['total'] == 1
    assert body['items'][0]['company'] == TAG + 'Needle Engineering Ltd'


def test_the_search_counts_matches_not_the_page(client):
    body = client.get('/api/leads?page=1&size=10&q=Customer').get_json()
    assert body['total'] == HOW_MANY
    assert len(body['items']) == 10


def test_the_legacy_shape_searches_the_whole_table_too(client):
    rows = client.get('/api/leads?q=Needle').get_json()
    assert [r['company'] for r in rows] == [TAG + 'Needle Engineering Ltd']


# ── ordering ─────────────────────────────────────────────────────────
def test_the_default_order_is_when_the_enquiry_reached_us(client):
    body = client.get('/api/leads?page=1&size=5&q=PAG-').get_json()
    ids = [i['id'] for i in body['items']]
    with flask_app.app_context():
        got = [db.session.get(Lead, i).received_at for i in ids]
    assert got == sorted(got, reverse=True)


def test_a_lead_with_no_received_at_still_sorts_by_its_created_date(client):
    """Before the backfill runs, received_at is null on every existing
    lead. Ordering on it alone would put them all at the end."""
    with flask_app.app_context():
        orphan = Lead(company=TAG + 'Unbackfilled Co', stage='New',
                      assigned_to=ADMIN,
                      created_at=datetime(2026, 12, 31, 9, 0),
                      received_at=None)
        db.session.add(orphan)
        db.session.commit()
        orphan_id = orphan.id
    try:
        body = client.get('/api/leads?page=1&size=3&q=PAG-').get_json()
        assert body['items'][0]['id'] == orphan_id, (
            'a lead with no received_at fell to the bottom of the list')
    finally:
        with flask_app.app_context():
            db.session.delete(db.session.get(Lead, orphan_id))
            db.session.commit()


def test_an_unknown_sort_key_is_ignored_rather_than_obeyed(client):
    """The sort name reaches the ORM, so it is an allow-list: a
    parameter that can name any column is a way to read one."""
    resp = client.get('/api/leads?page=1&size=3&sort=password_hash')
    assert resp.status_code == 200
    assert len(resp.get_json()['items']) == 3


def test_it_can_be_sorted_the_other_way(client):
    body = client.get('/api/leads?page=1&size=5&q=PAG-&dir=asc').get_json()
    ids = [i['id'] for i in body['items']]
    with flask_app.app_context():
        got = [db.session.get(Lead, i).received_at for i in ids]
    assert got == sorted(got)
