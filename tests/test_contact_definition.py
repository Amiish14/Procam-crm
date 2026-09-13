"""
One definition of contact, and the two places that must agree on it.

The Sales Intelligence tile and the Copilot's My Day answer the same
question on the same screen. A user who sees "12 idle" in one and "8
idle" in the other stops believing both — so this asserts they cannot
diverge, and that neither of them counts a field edit as contact.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'ContactTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'contact.db')
os.environ.pop('PROCAM_AI_BASE_URL', None)

from app import (app as flask_app, db, Employee, Lead,        # noqa: E402
                 LeadActivity, LeadEmail)
from app.access import scope as scope_mod                     # noqa: E402
from app.access.service import set_profile                    # noqa: E402
from app.copilot import intents as catalogue                  # noqa: E402
from app.models.access import DataScope                       # noqa: E402
from app.services import contact                              # noqa: E402

flask_app.config['WTF_CSRF_ENABLED'] = False

OLD = datetime.utcnow() - timedelta(days=30)
RECENT = datetime.utcnow() - timedelta(hours=6)


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        e = Employee.query.filter_by(emp_code='CTREP').first()
        if e is None:
            e = Employee(emp_code='CTREP', name='Contact Rep')
            db.session.add(e)
        e.role, e.is_active, e.must_change_pw = 'user', True, False
        e.vertical, e.is_super_admin = 'All', False
        db.session.commit()

        LeadActivity.query.delete()
        LeadEmail.query.delete()
        Lead.query.filter(Lead.company.like('Ct %')).delete()
        db.session.commit()

        ids = {}
        for tag in ('emailed', 'called', 'edited', 'silent'):
            lead = Lead(company=f'Ct {tag}', source='manual',
                        stage='Quoted', assigned_to='CTREP')
            db.session.add(lead)
            db.session.flush()
            ids[tag] = lead.id

        db.session.add(LeadEmail(lead_id=ids['emailed'], direction='outbound',
                                 subject='quote', sent_or_received_at=RECENT))
        db.session.add(LeadActivity(lead_id=ids['called'], kind='call',
                                    subject='spoke', occurred_at=RECENT))
        # "edited" gets a fresh updated_at and nothing else — the case
        # the old definition got wrong.
        db.session.get(Lead, ids['edited']).updated_at = datetime.utcnow()
        db.session.add(LeadEmail(lead_id=ids['silent'], direction='inbound',
                                 subject='ancient', sent_or_received_at=OLD))
        set_profile('CTREP', DataScope.OWN, [], actor='CTREP')
        db.session.commit()
        return ids


def _client():
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='CTREP', name='Contact Rep', role='user',
                 vertical='All')
    return c


# ── the definition itself ────────────────────────────────────────────
def test_an_email_is_contact(world):
    with flask_app.app_context():
        assert world['emailed'] in contact.contacted_since(
            datetime.utcnow() - timedelta(days=3))


def test_an_activity_is_contact(world):
    with flask_app.app_context():
        assert world['called'] in contact.contacted_since(
            datetime.utcnow() - timedelta(days=3))


def test_an_edit_is_not_contact(world):
    """updated_at moves when somebody fixes a typo."""
    with flask_app.app_context():
        assert world['edited'] not in contact.contacted_since(
            datetime.utcnow() - timedelta(days=3))


def test_old_contact_does_not_count_as_recent(world):
    with flask_app.app_context():
        recent = contact.contacted_since(
            datetime.utcnow() - timedelta(days=3))
        assert world['silent'] not in recent
        # but it IS contact, just not recent
        assert world['silent'] in contact.ever_contacted()


def test_last_contact_takes_the_later_of_the_two(world):
    with flask_app.app_context():
        lead = db.session.get(Lead, world['emailed'])
        db.session.add(LeadActivity(lead_id=lead.id, kind='call',
                                    subject='older', occurred_at=OLD))
        db.session.commit()
        assert contact.days_since_contact(lead) < 2


# ── the two consumers agree ──────────────────────────────────────────
def test_the_tile_and_the_copilot_report_the_same_untouched_count(world):
    """The claim that made this module necessary.

    Both answer "how many of my open leads have nobody spoken to in
    three days" on the same screen. If they can differ, one of them is
    wrong and the user cannot tell which.
    """
    tile = _client().get('/api/my-work').get_json()
    # Deliberately not a skip. The first version skipped when the
    # endpoint name was wrong, so the one test this module exists for
    # passed without running.
    assert tile and 'pending' in tile, \
        'the My Work endpoint moved — this test must be repointed, not skipped'

    with flask_app.app_context():
        sc = scope_mod.for_employee('CTREP')
        answer = catalogue.get('my_day').handler(sc, {})
    assert tile['pending']['untouched_3d'] == answer.figures['idle_3_days']


def test_neither_counts_the_edited_lead(world):
    """Two open leads have had real contact, one has only been edited,
    one was contacted a month ago. Two should be untouched."""
    with flask_app.app_context():
        sc = scope_mod.for_employee('CTREP')
        answer = catalogue.get('my_day').handler(sc, {})
    assert answer.figures['idle_3_days'] == 2


def test_the_stale_list_names_the_edited_lead(world):
    """Because an edit is not contact, it belongs in the stale list —
    the old definition would have hidden it."""
    with flask_app.app_context():
        sc = scope_mod.for_employee('CTREP')
        result = catalogue.get('leads_stale').handler(sc, {})
        names = {r['Company'] for r in result.rows}
    assert 'Ct edited' in names
    assert 'Ct emailed' not in names
    assert 'Ct called' not in names
