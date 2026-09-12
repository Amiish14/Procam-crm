"""Primary + secondary lead assignee — WP5.

The secondary PIC is a monitor/backup, not a co-owner: they are notified
in those words, they appear in the assignment history, and they are never
the same person as the primary.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'AsgTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'asg.db')

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee, Lead = _main.Employee, _main.Lead
LeadAssignmentHistory = _main.LeadAssignmentHistory
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.notification import Notification              # noqa: E402


@pytest.fixture(scope='module')
def client():
    with flask_app.app_context():
        db.create_all()
        for code, name in (('ASGADM', 'Assign Admin'),
                           ('PIC001', 'Primary Person'),
                           ('PIC002', 'Secondary Person'),
                           ('PIC003', 'Third Person')):
            e = Employee.query.filter_by(emp_code=code).first()
            if not e:
                e = Employee(emp_code=code, name=name)
                db.session.add(e)
            e.is_active, e.must_change_pw = True, False
            e.role = 'admin' if code == 'ASGADM' else 'user'
        gone = Employee.query.filter_by(emp_code='LEFT01').first()
        if not gone:
            gone = Employee(emp_code='LEFT01', name='Has Left')
            db.session.add(gone)
        gone.is_active = False
        db.session.commit()
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='ASGADM', name='Assign Admin', role='admin',
                 vertical='All')
    return c


def _lead(client, **kw):
    body = {'company': 'Test Co', 'source': 'manual'}
    body.update(kw)
    r = client.post('/api/leads', json=body)
    assert r.status_code in (200, 201), r.get_data(as_text=True)
    return r.get_json()['id']


# ─── the pair ────────────────────────────────────────────────────────────
def test_a_lead_saves_with_a_primary_only(client):
    lid = _lead(client, company='Primary Only Ltd')
    r = client.put(f'/api/leads/{lid}', json={'assigned_to': 'PIC001'})
    assert r.status_code == 200
    with flask_app.app_context():
        lead = db.session.get(Lead, lid)
        assert lead.assigned_to == 'PIC001'
        assert lead.assigned_name == 'Primary Person'
        assert lead.secondary_owner is None


def test_a_lead_takes_an_optional_secondary(client):
    lid = _lead(client, company='Both Ltd')
    r = client.put(f'/api/leads/{lid}', json={'assigned_to': 'PIC001',
                                              'secondary_owner': 'PIC002'})
    assert r.status_code == 200
    with flask_app.app_context():
        lead = db.session.get(Lead, lid)
        assert lead.secondary_owner == 'PIC002'
        assert lead.secondary_owner_name == 'Secondary Person'


def test_one_person_cannot_be_both(client):
    """A monitor who is also the owner monitors nobody."""
    lid = _lead(client, company='Same Person Ltd')
    r = client.put(f'/api/leads/{lid}', json={'assigned_to': 'PIC001',
                                              'secondary_owner': 'PIC001'})
    assert r.status_code == 400
    assert 'cannot be both' in r.get_json()['error']
    with flask_app.app_context():
        assert db.session.get(Lead, lid).assigned_to != 'PIC001'


def test_a_leaver_cannot_be_assigned(client):
    """Leads owned by someone who has left is an existing Data Quality
    finding; the assignment path should stop creating more."""
    lid = _lead(client, company='Leaver Ltd')
    r = client.put(f'/api/leads/{lid}', json={'assigned_to': 'LEFT01'})
    assert r.status_code == 400
    assert 'not an active employee' in r.get_json()['error']


def test_the_secondary_can_be_cleared(client):
    lid = _lead(client, company='Clear Ltd')
    client.put(f'/api/leads/{lid}', json={'assigned_to': 'PIC001',
                                          'secondary_owner': 'PIC002'})
    r = client.put(f'/api/leads/{lid}', json={'secondary_owner': ''})
    assert r.status_code == 200
    with flask_app.app_context():
        lead = db.session.get(Lead, lid)
        assert lead.secondary_owner is None
        assert lead.assigned_to == 'PIC001', 'the primary was untouched'


def test_the_lead_payload_exposes_both(client):
    lid = _lead(client, company='Payload Ltd')
    client.put(f'/api/leads/{lid}', json={'assigned_to': 'PIC001',
                                          'secondary_owner': 'PIC002'})
    d = client.get(f'/api/leads/{lid}').get_json()
    assert d['assigned_to'] == 'PIC001'
    assert d['secondary_owner'] == 'PIC002'
    assert d['secondary_owner_name'] == 'Secondary Person'


# ─── notifications ───────────────────────────────────────────────────────
def test_both_assignees_are_notified(client):
    lid = _lead(client, company='Notify Ltd')
    client.put(f'/api/leads/{lid}', json={'assigned_to': 'PIC001',
                                          'secondary_owner': 'PIC002'})
    with flask_app.app_context():
        notes = Notification.query.filter_by(entity_type='Lead',
                                             entity_id=lid).all()
        who = {n.user_id for n in notes}
        assert who == {'PIC001', 'PIC002'}, who


def test_the_monitor_is_told_they_are_a_monitor(client):
    """If the wording is the same, two people think they own the lead —
    or each assumes the other does."""
    lid = _lead(client, company='Wording Ltd')
    client.put(f'/api/leads/{lid}', json={'assigned_to': 'PIC001',
                                          'secondary_owner': 'PIC002'})
    with flask_app.app_context():
        primary = Notification.query.filter_by(
            entity_id=lid, user_id='PIC001').first()
        secondary = Notification.query.filter_by(
            entity_id=lid, user_id='PIC002').first()
        assert 'primary PIC' in primary.body
        assert 'monitor' in secondary.title.lower() \
            or 'monitor' in secondary.body.lower()
        assert 'secondary PIC' in secondary.body
        assert secondary.title != primary.title


def test_an_unchanged_assignee_is_not_notified_again(client):
    """Adding a secondary must not re-notify a primary who has held the
    lead for a month."""
    lid = _lead(client, company='No Spam Ltd')
    client.put(f'/api/leads/{lid}', json={'assigned_to': 'PIC001'})
    with flask_app.app_context():
        before = Notification.query.filter_by(entity_id=lid,
                                              user_id='PIC001').count()
    client.put(f'/api/leads/{lid}', json={'assigned_to': 'PIC001',
                                          'secondary_owner': 'PIC002'})
    with flask_app.app_context():
        after = Notification.query.filter_by(entity_id=lid,
                                             user_id='PIC001').count()
    assert after == before, 'the primary was notified of a change to someone else'


def test_notifications_link_back_to_the_lead(client):
    lid = _lead(client, company='Link Ltd')
    client.put(f'/api/leads/{lid}', json={'assigned_to': 'PIC001'})
    with flask_app.app_context():
        n = Notification.query.filter_by(entity_id=lid,
                                         user_id='PIC001').first()
        assert n.action_url == f'/app?lead={lid}'


# ─── history ─────────────────────────────────────────────────────────────
def test_every_reassignment_is_recorded(client):
    lid = _lead(client, company='History Ltd')
    client.put(f'/api/leads/{lid}', json={'assigned_to': 'PIC001',
                                          'secondary_owner': 'PIC002'})
    client.put(f'/api/leads/{lid}', json={'assigned_to': 'PIC003'})
    with flask_app.app_context():
        rows = (LeadAssignmentHistory.query.filter_by(lead_id=lid)
                .order_by(LeadAssignmentHistory.id).all())
        # Scoped to this test's own writes: SQLite reuses a deleted
        # lead's id, so a bare filter on lead_id can pick up rows that
        # belonged to a different lead entirely.
        rows = rows[-2:]
        assert len(rows) == 2
        assert rows[0].to_primary == 'PIC001'
        assert rows[0].to_secondary == 'PIC002'
        assert rows[1].from_primary == 'PIC001'
        assert rows[1].to_primary == 'PIC003'
        assert rows[1].to_secondary == 'PIC002', \
            'the untouched secondary must still be recorded'
        assert rows[1].changed_by == 'ASGADM'


def test_a_no_op_assignment_writes_no_history(client):
    lid = _lead(client, company='Noop Ltd')
    client.put(f'/api/leads/{lid}', json={'assigned_to': 'PIC001'})
    with flask_app.app_context():
        before = LeadAssignmentHistory.query.filter_by(lead_id=lid).count()
    client.put(f'/api/leads/{lid}', json={'assigned_to': 'PIC001'})
    with flask_app.app_context():
        assert LeadAssignmentHistory.query.filter_by(
            lead_id=lid).count() == before


def test_assignment_shows_in_the_lead_timeline(client):
    """The timeline answers "why did this stall?", and changing hands is
    usually the answer."""
    lid = _lead(client, company='Timeline Ltd')
    client.put(f'/api/leads/{lid}', json={'assigned_to': 'PIC001',
                                          'secondary_owner': 'PIC002'})
    events = client.get(f'/api/leads/{lid}/history').get_json()
    asg = [e for e in events if e['kind'] == 'assignment']
    assert asg, 'reassignment is missing from the timeline'
    assert asg[0]['to_primary'] == 'PIC001'
    assert asg[0]['to_secondary'] == 'PIC002'


# ─── bulk ────────────────────────────────────────────────────────────────
def test_bulk_assign_sets_both_and_records_each_lead(client):
    ids = [_lead(client, company=f'Bulk {i} Ltd') for i in range(3)]
    r = client.post('/api/leads/bulk-assign',
                    json={'ids': ids, 'emp_code': 'PIC001',
                          'secondary_owner': 'PIC002'})
    assert r.status_code == 200
    assert r.get_json()['count'] == 3
    with flask_app.app_context():
        for lid in ids:
            lead = db.session.get(Lead, lid)
            assert lead.assigned_to == 'PIC001'
            assert lead.secondary_owner == 'PIC002'
            assert LeadAssignmentHistory.query.filter_by(
                lead_id=lid).count() == 1, 'bulk assign skipped the history'


def test_bulk_assign_refuses_the_same_person_twice(client):
    ids = [_lead(client, company='Bulk Same Ltd')]
    r = client.post('/api/leads/bulk-assign',
                    json={'ids': ids, 'emp_code': 'PIC001',
                          'secondary_owner': 'PIC001'})
    assert r.status_code == 400


# ─── existing behaviour is preserved ─────────────────────────────────────
def test_a_lead_that_predates_the_column_still_works(client):
    """Every one of the 11,000 existing leads has NULL here."""
    with flask_app.app_context():
        lead = Lead(company='Legacy Ltd', assigned_to='PIC001',
                    assigned_name='Primary Person', source='manual')
        db.session.add(lead)
        db.session.commit()
        lid = lead.id
    d = client.get(f'/api/leads/{lid}').get_json()
    assert d['assigned_to'] == 'PIC001'
    assert d['secondary_owner'] == ''
    r = client.put(f'/api/leads/{lid}', json={'assigned_to': 'PIC003'})
    assert r.status_code == 200


def test_a_non_admin_cannot_reassign(client):
    """Unchanged from before: reassignment is admin-only."""
    lid = _lead(client, company='Perms Ltd')
    client.put(f'/api/leads/{lid}', json={'assigned_to': 'PIC001'})
    other = flask_app.test_client()
    with other.session_transaction() as s:
        s.update(emp_code='PIC002', name='Secondary Person', role='user',
                 vertical='All')
    other.put(f'/api/leads/{lid}', json={'assigned_to': 'PIC002',
                                         'secondary_owner': 'PIC003'})
    with flask_app.app_context():
        lead = db.session.get(Lead, lid)
        assert lead.assigned_to == 'PIC001'
        assert lead.secondary_owner is None
