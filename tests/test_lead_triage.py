"""Admin Lead Triage — WP4.

Inbound leads arrive with no owner, so until someone assigns them they
appear in nobody's My Work. These tests hold the numbers on that queue,
and the rule that only an admin may see it.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'TriageTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'triage.db')

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee, Lead = _main.Employee, _main.Lead
LeadActivity, LeadStageHistory = _main.LeadActivity, _main.LeadStageHistory
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.triage import service as triage                      # noqa: E402

NOW = datetime(2026, 9, 18, 12, 0, 0)

# The Flask app is a singleton, so every test module in a full run shares
# one database and other modules' leads are visible here. These tests
# therefore assert on their own fixture rows by name and on differences
# in the counts, never on absolute totals.
MINE = ('Fresh Co', 'Also Fresh Co', 'Six Hours Co', 'Half Day Co',
        'Stale Co', 'Very Stale Co')


def _ago(hours):
    return NOW - timedelta(hours=hours)


@pytest.fixture(scope='module')
def world():
    with flask_app.app_context():
        db.create_all()
        for code, name, role, admin in (
                ('TRIADM', 'Triage Admin', 'admin', True),
                ('SALES1', 'Sales One', 'user', False),
                ('SALES2', 'Sales Two', 'user', False)):
            e = Employee.query.filter_by(emp_code=code).first()
            if not e:
                e = Employee(emp_code=code, name=name)
                db.session.add(e)
            e.role, e.is_active, e.must_change_pw = role, True, False
            e.is_super_admin, e.vertical = admin, 'All'

        # Unassigned, spread across every bucket.
        for hours, company in ((1, 'Fresh Co'), (2, 'Also Fresh Co'),
                               (6, 'Six Hours Co'), (12, 'Half Day Co'),
                               (40, 'Stale Co'), (100, 'Very Stale Co')):
            db.session.add(Lead(company=company, source='email',
                                stage='New Opportunity',
                                created_at=_ago(hours)))
        # Assigned and worked — must not appear anywhere.
        worked = Lead(company='Worked Co', source='email',
                      stage='New Opportunity', assigned_to='SALES1',
                      assigned_name='Sales One', created_at=_ago(60))
        db.session.add(worked)
        db.session.flush()
        db.session.add(LeadActivity(lead_id=worked.id, kind='call',
                                    subject='Called them',
                                    occurred_at=_ago(50)))
        # Assigned, past SLA, nothing logged — the monitor's list.
        db.session.add(Lead(company='Ignored Co', source='email',
                            stage='New Opportunity', assigned_to='SALES1',
                            assigned_name='Sales One',
                            secondary_owner='SALES2',
                            secondary_owner_name='Sales Two',
                            created_at=_ago(72)))
        # Assigned, past SLA, but the stage moved on.
        moved = Lead(company='Moved On Co', source='email', stage='Qualified',
                     assigned_to='SALES1', assigned_name='Sales One',
                     created_at=_ago(72))
        db.session.add(moved)
        db.session.flush()
        db.session.add(LeadStageHistory(lead_id=moved.id,
                                        from_stage='New Opportunity',
                                        to_stage='Qualified',
                                        changed_at=_ago(70)))
        db.session.commit()
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='TRIADM', name='Triage Admin', role='admin',
                 vertical='All')
    return c


def _plain_client():
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='SALES1', name='Sales One', role='user',
                 vertical='All')
    return c


# ─── the numbers ─────────────────────────────────────────────────────────
def test_the_unassigned_count_is_the_live_one(world):
    with flask_app.app_context():
        h = triage.headline(NOW)
        rows = triage.unassigned_rows(NOW)
    # Compared on ids, not company names: two leads from the same company
    # are two rows in the queue and one name in a set.
    assert h['unassigned_total'] == len({r['id'] for r in rows})
    queued = {r['company'] for r in rows}
    for company in MINE:
        assert company in queued, f'{company} is unassigned and not queued'


def test_the_oldest_unassigned_lead_is_correct(world):
    with flask_app.app_context():
        h = triage.headline(NOW)
        rows = triage.unassigned_rows(NOW)
    assert h['oldest_lead_id'] == rows[0]['id'], \
        'the headline and the top of the queue must name the same lead'
    assert h['oldest_hours'] == max(r['age_hours'] for r in rows
                                    if r['age_hours'] is not None)


def test_the_buckets_match_the_arrival_timestamps(world):
    """Each fixture lead must land in the bucket its arrival time says."""
    with flask_app.app_context():
        rows = {r['company']: r for r in triage.unassigned_rows(NOW)}
    expected = {'Fresh Co': '< 4h', 'Also Fresh Co': '< 4h',
                'Six Hours Co': '4–8h', 'Half Day Co': '8–24h',
                'Stale Co': '> 24h', 'Very Stale Co': '> 24h'}
    for company, want in expected.items():
        age = rows[company]['age_hours']
        got = next(triage.bucket_label(lo, hi)
                   for lo, hi in triage.AGE_BUCKETS
                   if age >= lo and (hi is None or age < hi))
        assert got == want, f'{company} ({age}h) fell in {got}, not {want}'


def test_the_buckets_reconcile_with_the_headline(world):
    """A dashboard whose parts do not add up to its total is lying."""
    with flask_app.app_context():
        h = triage.headline(NOW)
        total = sum(b['count'] for b in triage.buckets(NOW))
    assert total == h['unassigned_total']


def test_a_lead_with_no_arrival_time_is_still_counted(world):
    """Dropping it would make the buckets disagree with the headline."""
    with flask_app.app_context():
        db.session.add(Lead(company='No Date Co', source='manual',
                            stage='New Opportunity'))
        db.session.commit()
        # created_at carries a column default, so passing None still
        # writes a timestamp — the NULL has to be set directly, which is
        # how legacy imported rows actually look.
        db.session.execute(db.text(
            "UPDATE leads SET created_at = NULL WHERE company = 'No Date Co'"))
        db.session.commit()
        h = triage.headline(NOW)
        bs = triage.buckets(NOW)
        assert sum(b['count'] for b in bs) == h['unassigned_total']
        assert any(b['label'] == 'no arrival time' and b['count'] == 1
                   for b in bs)
        db.session.delete(Lead.query.filter_by(company='No Date Co').first())
        db.session.commit()


def test_an_assigned_lead_is_not_in_the_unassigned_queue(world):
    with flask_app.app_context():
        names = {r['company'] for r in triage.unassigned_rows(NOW)}
    assert 'Worked Co' not in names
    assert 'Ignored Co' not in names


def test_the_queue_is_oldest_first(world):
    with flask_app.app_context():
        rows = triage.unassigned_rows(NOW)
    ages = [r['age_hours'] for r in rows if r['age_hours'] is not None]
    assert ages == sorted(ages, reverse=True), 'the oldest must be worked first'


# ─── assigned but not actioned ───────────────────────────────────────────
def test_a_lead_with_no_activity_past_the_sla_is_flagged(world):
    with flask_app.app_context():
        rows = {r['company']: r for r in triage.not_actioned(24, NOW)}
    assert 'Ignored Co' in rows
    assert rows['Ignored Co']['secondary_owner_name'] == 'Sales Two'
    assert rows['Ignored Co']['overdue_by'] == pytest.approx(48, abs=1)


def test_a_lead_with_a_logged_activity_is_not_flagged(world):
    """The CRM's own activity model decides this, not a new definition."""
    with flask_app.app_context():
        assert 'Worked Co' not in {r['company']
                                   for r in triage.not_actioned(24, NOW)}


def test_a_lead_whose_stage_moved_on_is_not_flagged(world):
    with flask_app.app_context():
        assert 'Moved On Co' not in {r['company']
                                     for r in triage.not_actioned(24, NOW)}


def test_the_sla_window_is_configurable(world):
    """Ignored Co is 72h old, so a 200h SLA must not flag it."""
    with flask_app.app_context():
        wide = {r['company'] for r in triage.not_actioned(200, NOW)}
        narrow = {r['company'] for r in triage.not_actioned(24, NOW)}
    assert 'Ignored Co' in narrow
    assert 'Ignored Co' not in wide


# ─── access control, enforced on the server ──────────────────────────────
def test_an_admin_can_open_the_dashboard(world):
    assert world.get('/lead-triage').status_code == 200


def test_a_non_admin_cannot_open_the_dashboard(world):
    r = _plain_client().get('/lead-triage')
    assert r.status_code in (302, 403)
    assert r.status_code != 200


def test_a_non_admin_cannot_call_the_api_directly(world):
    """Hiding the link is not access control."""
    c = _plain_client()
    assert c.get('/api/triage/summary').status_code == 403
    r = c.post('/api/triage/assign', json={'lead_id': 1, 'primary': 'SALES1'})
    assert r.status_code == 403


def test_an_anonymous_user_is_sent_to_login(world):
    c = flask_app.test_client()
    r = c.get('/lead-triage')
    assert r.status_code in (302, 401, 403)
    assert r.status_code != 200


# ─── assigning from the dashboard ────────────────────────────────────────
def test_assigning_from_the_dashboard_uses_the_wp5_path(world):
    """Same service as the lead form, so the history row and both
    notifications happen here too — not a second implementation."""
    from app import LeadAssignmentHistory
    from app.models.notification import Notification
    with flask_app.app_context():
        lid = Lead.query.filter_by(company='Six Hours Co').first().id

    r = world.post('/api/triage/assign',
                   json={'lead_id': lid, 'primary': 'SALES1',
                         'secondary': 'SALES2'})
    assert r.status_code == 200, r.get_data(as_text=True)

    with flask_app.app_context():
        lead = db.session.get(Lead, lid)
        assert lead.assigned_to == 'SALES1'
        assert lead.secondary_owner == 'SALES2'
        h = LeadAssignmentHistory.query.filter_by(lead_id=lid).all()
        assert len(h) == 1 and h[0].note == 'lead triage'
        assert {n.user_id for n in Notification.query.filter_by(
            entity_id=lid, entity_type='Lead').all()} == {'SALES1', 'SALES2'}


def test_the_counts_come_back_with_the_assignment(world):
    """So the dashboard updates without a full reload."""
    with flask_app.app_context():
        lid = Lead.query.filter_by(company='Half Day Co').first().id
        before = triage.headline()['unassigned_total']
    body = world.post('/api/triage/assign',
                      json={'lead_id': lid, 'primary': 'SALES1'}).get_json()
    assert body['headline']['unassigned_total'] == before - 1
    assert 'buckets' in body


def test_an_assigned_lead_leaves_the_queue(world):
    with flask_app.app_context():
        assert 'Half Day Co' not in {r['company']
                                     for r in triage.unassigned_rows()}


def test_the_dashboard_refuses_the_same_person_twice(world):
    with flask_app.app_context():
        lid = Lead.query.filter_by(company='Stale Co').first().id
    r = world.post('/api/triage/assign',
                   json={'lead_id': lid, 'primary': 'SALES1',
                         'secondary': 'SALES1'})
    assert r.status_code == 400


def test_assigning_a_lead_that_does_not_exist_is_404(world):
    r = world.post('/api/triage/assign',
                   json={'lead_id': 999999999, 'primary': 'SALES1'})
    assert r.status_code == 404


def test_assigned_today_counts_triage_decisions_not_edits(world):
    """Lead.updated_at moves for any edit, so counting from it would
    report a corrected phone number as a triage decision."""
    with flask_app.app_context():
        # A lead assigned long ago and never touched since: it must not
        # count today under either implementation.
        lead = Lead(company='Old Assignment Co', source='email',
                    stage='New Opportunity', assigned_to='SALES1',
                    assigned_name='Sales One', created_at=_ago(500))
        db.session.add(lead)
        db.session.commit()
        db.session.execute(db.text(
            "UPDATE leads SET updated_at = '2026-01-01 09:00:00' "
            "WHERE company = 'Old Assignment Co'"))
        db.session.commit()

        before = triage.headline()['assigned_today']

        # Now edit it — a note, not a reassignment.
        db.session.execute(db.text(
            "UPDATE leads SET notes = 'corrected the phone number', "
            "updated_at = CURRENT_TIMESTAMP "
            "WHERE company = 'Old Assignment Co'"))
        db.session.commit()

        after = triage.headline()['assigned_today']
        assert after == before, (
            'an edit was counted as a triage decision — "assigned today" '
            'must come from the assignment history, not Lead.updated_at')


# ─── the page itself ─────────────────────────────────────────────────────
def test_the_dashboard_reuses_the_existing_lead_view(world):
    html = world.get('/lead-triage').get_data(as_text=True)
    assert "'/app?lead=' + id" in html, \
        'rows must open the existing lead detail, not a second one'


def test_the_dashboard_calls_its_api_under_the_prefix(world):
    """The class of bug that made the funnels load and show nothing."""
    html = open(os.path.join(_ROOT, 'templates', 'triage',
                             'dashboard.html')).read()
    assert "u.indexOf('{{ url_prefix }}') !== 0" in html
