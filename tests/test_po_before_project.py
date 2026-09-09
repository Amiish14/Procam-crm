"""One PO = one Project and one Job.

The process changed in Sept 2026: the Customer PO / Sales Order /
Contract is captured immediately after the win, and the TMS Project and
Job are created against it. These tests hold that ordering in place —
the old order (project first, PO afterwards) allowed several projects
under one PO and projects with no PO behind them at all.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'PoTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'po.db')

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db, Employee = _main.app, _main.db, _main.Employee
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.tms_handover import (WonHandover, HandoverStatus,   # noqa: E402
                                     PoType)


@pytest.fixture(scope='module')
def client():
    with flask_app.app_context():
        db.create_all()
        e = Employee.query.filter_by(emp_code='POADM').first()
        if not e:
            e = Employee(emp_code='POADM', name='PO Admin')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'admin', True, False
        e.vertical, e.is_super_admin = 'All', True
        db.session.commit()
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='POADM', name='PO Admin', role='admin',
                 vertical='All')
    return c


def _make(client, **kw):
    body = {'account_name': 'Acme Steel', 'won_value': 100000}
    body.update(kw)
    r = client.post('/api/handovers', json=body)
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.get_json()['handover']


# ─── the sequence ────────────────────────────────────────────────────────
def test_a_won_deal_starts_awaiting_its_po(client):
    h = _make(client, account_name='Sequence Co')
    assert h['status'] == HandoverStatus.AWAITING_PO
    assert h['has_po'] is False


def test_tms_project_is_refused_until_the_po_exists(client):
    """The core of the change — this is what used to be allowed."""
    h = _make(client, account_name='Premature Co')
    r = client.patch(f'/api/handovers/{h["id"]}',
                     json={'tms_project_id': 'TMS-9001',
                           'tms_ack_by': 'Ops'})
    assert r.status_code == 409
    body = r.get_json()
    assert body['needs_po'] is True
    with flask_app.app_context():
        row = db.session.get(WonHandover, h['id'])
        assert row.tms_project_id is None, 'the id was written anyway'
        assert row.status == HandoverStatus.AWAITING_PO


def test_capturing_the_po_unblocks_tms(client):
    h = _make(client, account_name='Happy Path Co')
    r = client.post(f'/api/handovers/{h["id"]}/po',
                    json={'po_ref': 'PO/2026/0001',
                          'po_type': PoType.PO,
                          'po_date': '2026-09-17', 'po_value': 250000})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['handover']['status'] == HandoverStatus.PENDING

    r = client.patch(f'/api/handovers/{h["id"]}',
                     json={'tms_project_id': 'TMS-1001',
                           'tms_ack_by': 'Ops'})
    assert r.status_code == 200
    assert r.get_json()['handover']['status'] == HandoverStatus.PROJECT_MADE


def test_the_po_records_who_captured_it_and_when(client):
    h = _make(client, account_name='Audit Co')
    client.post(f'/api/handovers/{h["id"]}/po',
                json={'po_ref': 'PO/AUDIT/1'})
    with flask_app.app_context():
        row = db.session.get(WonHandover, h['id'])
        assert row.po_captured_by == 'POADM'
        assert row.po_captured_at is not None


# ─── one PO = one project ────────────────────────────────────────────────
def test_the_same_po_cannot_carry_two_projects(client):
    a = _make(client, account_name='Duplicate Co')
    b = _make(client, account_name='Duplicate Co')
    assert a['id'] != b['id']

    r = client.post(f'/api/handovers/{a["id"]}/po',
                    json={'po_ref': 'PO/DUP/77'})
    assert r.status_code == 200

    r = client.post(f'/api/handovers/{b["id"]}/po',
                    json={'po_ref': 'PO/DUP/77'})
    assert r.status_code == 400
    assert f'#{a["id"]}' in r.get_json()['error']

    with flask_app.app_context():
        assert db.session.get(WonHandover, b['id']).po_ref is None


def test_a_reformatted_reference_is_still_the_same_po(client):
    """Customers write one PO number many ways; a duplicate typed as
    `po dup-88` must not slip past `PO/DUP/88`."""
    a = _make(client, account_name='Reformat Co')
    b = _make(client, account_name='Reformat Co')
    assert client.post(f'/api/handovers/{a["id"]}/po',
                       json={'po_ref': 'PO/DUP/88'}).status_code == 200
    r = client.post(f'/api/handovers/{b["id"]}/po',
                    json={'po_ref': 'po dup-88'})
    assert r.status_code == 400


def test_two_customers_may_use_the_same_po_number(client):
    """Uniqueness is per customer — `PO/2026/001` is a common number and
    blocking it globally would stop legitimate work."""
    a = _make(client, account_name='First Customer Ltd')
    b = _make(client, account_name='Second Customer Ltd')
    assert client.post(f'/api/handovers/{a["id"]}/po',
                       json={'po_ref': 'PO/2026/001'}).status_code == 200
    assert client.post(f'/api/handovers/{b["id"]}/po',
                       json={'po_ref': 'PO/2026/001'}).status_code == 200


def test_editing_a_po_does_not_clash_with_itself(client):
    h = _make(client, account_name='Self Edit Co')
    assert client.post(f'/api/handovers/{h["id"]}/po',
                       json={'po_ref': 'PO/SELF/1'}).status_code == 200
    r = client.post(f'/api/handovers/{h["id"]}/po',
                    json={'po_ref': 'PO/SELF/1', 'po_value': 999})
    assert r.status_code == 200


def test_a_cancelled_handover_releases_its_po(client):
    """A cancelled deal must not lock its PO number forever — the same PO
    is usually re-raised against the replacement handover."""
    a = _make(client, account_name='Cancelled Co')
    assert client.post(f'/api/handovers/{a["id"]}/po',
                       json={'po_ref': 'PO/CANC/5'}).status_code == 200
    assert client.post(f'/api/handovers/{a["id"]}/cancel',
                       json={'reason': 'customer withdrew'}).status_code == 200

    b = _make(client, account_name='Cancelled Co')
    assert client.post(f'/api/handovers/{b["id"]}/po',
                       json={'po_ref': 'PO/CANC/5'}).status_code == 200


# ─── validation ──────────────────────────────────────────────────────────
def test_a_po_needs_a_reference(client):
    h = _make(client, account_name='Blank Co')
    r = client.post(f'/api/handovers/{h["id"]}/po', json={'po_ref': '   '})
    assert r.status_code == 400


def test_po_type_is_one_of_the_three(client):
    h = _make(client, account_name='Type Co')
    r = client.post(f'/api/handovers/{h["id"]}/po',
                    json={'po_ref': 'PO/T/1', 'po_type': 'Handshake'})
    assert r.status_code == 400
    assert 'Customer Contract' in r.get_json()['error']


def test_po_ref_is_not_writable_through_patch(client):
    """PATCH carries no uniqueness check, so it must not set the PO —
    otherwise the rule is one endpoint away from being bypassed."""
    a = _make(client, account_name='Bypass Co')
    b = _make(client, account_name='Bypass Co')
    assert client.post(f'/api/handovers/{a["id"]}/po',
                       json={'po_ref': 'PO/BYPASS/1'}).status_code == 200

    client.patch(f'/api/handovers/{b["id"]}', json={'po_ref': 'PO/BYPASS/1'})
    with flask_app.app_context():
        assert db.session.get(WonHandover, b['id']).po_ref is None


def test_a_deal_won_against_an_existing_po_still_checks_uniqueness(client):
    """The PO can be supplied at creation; that path is checked too."""
    a = _make(client, account_name='Upfront Co', po_ref='PO/UP/1')
    assert a['status'] == HandoverStatus.PENDING
    assert a['po_ref'] == 'PO/UP/1'

    r = client.post('/api/handovers',
                    json={'account_name': 'Upfront Co', 'po_ref': 'PO/UP/1'})
    assert r.status_code == 400


def test_status_cannot_be_hand_advanced_past_the_po(client):
    """Marking a row "ready for TMS" by hand is the same shortcut."""
    h = _make(client, account_name='Shortcut Co')
    r = client.patch(f'/api/handovers/{h["id"]}',
                     json={'status': HandoverStatus.PENDING})
    assert r.status_code == 409
    with flask_app.app_context():
        assert (db.session.get(WonHandover, h['id']).status
                == HandoverStatus.AWAITING_PO)


# ─── data quality surfaces both failure modes ────────────────────────────
def test_data_quality_finds_won_deals_with_no_po(client):
    from app.data_quality import service as dq
    with flask_app.app_context():
        row = WonHandover(account_name='Stuck Co',
                          status=HandoverStatus.AWAITING_PO)
        db.session.add(row)
        db.session.commit()

        count, _q = dq.check_won_without_po()
        assert count >= 1

        detail = dq.records_for('won_no_po')
        assert detail['count'] == count
        assert detail['records'], 'counted but drilled into nothing'
        names = [r['name'] for r in detail['records']]
        assert 'Stuck Co' in names
        for r in detail['records']:
            assert r['route'].startswith('/handovers')


def test_data_quality_finds_a_po_on_two_handovers(client):
    """Enforcement stops new ones; this catches what predates the rule
    or was written straight to the database."""
    from app.data_quality import service as dq
    with flask_app.app_context():
        for _ in range(2):
            db.session.add(WonHandover(account_name='Legacy Dupe Ltd',
                                       po_ref='PO/LEGACY/1',
                                       status=HandoverStatus.PENDING))
        db.session.commit()

        count, rows = dq.check_duplicate_po_refs()
        assert count >= 2, 'both sides of the clash should be listed'

        detail = dq.records_for('dupe_po_refs')
        assert detail['records'], 'counted but drilled into nothing'
        assert any('PO/LEGACY/1' in r['meta'] for r in detail['records'])
