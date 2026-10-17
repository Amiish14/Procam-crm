"""
Stale alerts: a different update is not a duplicate, and a message
that waited is re-read before it is sent.

Two bugs, one symptom — "the notification emails don't show the PIC's
latest lead updates".

(a) The duplicate check keyed on (person, kind, entity, 10 minutes)
    and never looked at the words. A second, *different* update to
    the same lead inside that window was dropped completely: no bell
    row, no email. The health check on 18 October found 1,273 records
    in a fortnight with more than one notification in the same hour.

(b) The body was rendered when the event fired and sent unchanged
    however much later. Median wait: 69 minutes. Worst: nine hours.
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
os.environ.setdefault('SECRET_KEY', 'stale-alerts-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'StaleAlertsTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'stale_alerts.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee, Lead = _main.Employee, _main.Lead
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.mailops import EmailOutbox               # noqa: E402
from app.models.notification import Notification         # noqa: E402
from app.services import notification_rules as rules     # noqa: E402
from app.services import notify, outbox                  # noqa: E402

PIC = 'SAPIC'
TAG = 'SA '


def _wipe():
    Notification.query.filter(Notification.user_id == PIC).delete(
        synchronize_session=False)
    EmailOutbox.query.filter(EmailOutbox.dedupe_key.like('sa-%')).delete(
        synchronize_session=False)
    ids = [l.id for l in Lead.query.filter(Lead.company.like(TAG + '%'))]
    if ids:
        Lead.query.filter(Lead.id.in_(ids)).delete(synchronize_session=False)
    db.session.commit()


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        _wipe()
        e = (Employee.query.filter_by(emp_code=PIC).first()
             or Employee(emp_code=PIC))
        e.name, e.is_active, e.must_change_pw = 'Stale Alert PIC', True, False
        e.email, e.role, e.session_version = 'sapic@procamgroup.in', 'user', 0
        e.is_super_admin, e.is_vertical_head = False, False
        db.session.add(e)
        lead = Lead(company=TAG + 'Pandacs Ltd', assigned_to=PIC,
                    assigned_name=PIC, stage='Quoted',
                    procam_vertical='Project Freight',
                    created_at=datetime.utcnow() - timedelta(days=5))
        db.session.add(lead)
        db.session.commit()
        yield {'lead': lead.id}
        _wipe()


def _notes():
    return Notification.query.filter_by(user_id=PIC).order_by(
        Notification.id).all()


# ── (a) the duplicate check ──────────────────────────────────────────
def test_two_different_updates_in_ten_minutes_are_two_notifications(world):
    """The bug: the second was dropped and the person kept the first."""
    with flask_app.app_context():
        notify.send(PIC, kind='lead_update', title='Quote sent',
                    body='₹4,50,000 quoted.', entity_type='Lead',
                    entity_id=world['lead'])
        notify.send(PIC, kind='lead_update', title='Client replied',
                    body='They want it by Friday.', entity_type='Lead',
                    entity_id=world['lead'])
        titles = [n.title for n in _notes()]
    assert titles == ['Quote sent', 'Client replied'], titles


def test_the_same_update_twice_in_ten_minutes_is_one_notification(world):
    with flask_app.app_context():
        first = notify.send(PIC, kind='lead_update', title='Quote sent',
                            body='₹4,50,000 quoted.', entity_type='Lead',
                            entity_id=world['lead'])
        second = notify.send(PIC, kind='lead_update', title='Quote sent',
                             body='₹4,50,000 quoted.', entity_type='Lead',
                             entity_id=world['lead'])
        assert first['notified'] is True
        assert second['suppressed'] is True
        assert len(_notes()) == 1


def test_whitespace_does_not_make_a_new_message(world):
    """A reflowed body is the same message, not a new one."""
    with flask_app.app_context():
        notify.send(PIC, kind='lead_update', title='Quote sent',
                    body='₹4,50,000  quoted.', entity_type='Lead',
                    entity_id=world['lead'])
        out = notify.send(PIC, kind='lead_update', title='Quote sent',
                          body='₹4,50,000 quoted.\n', entity_type='Lead',
                          entity_id=world['lead'])
        assert out['suppressed'] is True
        assert len(_notes()) == 1


def test_the_hash_is_recorded_on_the_row(world):
    with flask_app.app_context():
        notify.send(PIC, kind='lead_update', title='Quote sent',
                    body='x', entity_type='Lead', entity_id=world['lead'])
        row = _notes()[0]
        assert row.content_hash == notify.content_hash('Quote sent', 'x')
        assert len(row.content_hash) == 32


def test_a_row_written_before_the_column_existed_blocks_nothing(world):
    """Rows from before the migration have no hash. They must not
    suppress the first notification after it."""
    with flask_app.app_context():
        db.session.add(Notification(
            user_id=PIC, kind='lead_update', title='Old one', body='old',
            entity_type='Lead', entity_id=world['lead'],
            content_hash=None, created_at=datetime.utcnow()))
        db.session.commit()
        out = notify.send(PIC, kind='lead_update', title='Old one',
                          body='old', entity_type='Lead',
                          entity_id=world['lead'])
        assert out['notified'] is True


def test_the_email_key_carries_the_content_too(world):
    """Otherwise the bell delivers the second update and the email is
    refused by the unique index — worse than either alone."""
    a = notify._dedupe_key(PIC, 'lead.updated', 'Lead', 1,
                           digest=notify.content_hash('A', 'a'))
    b = notify._dedupe_key(PIC, 'lead.updated', 'Lead', 1,
                           digest=notify.content_hash('B', 'b'))
    assert a != b
    same = notify._dedupe_key(PIC, 'lead.updated', 'Lead', 1,
                              digest=notify.content_hash('A', 'a'))
    assert a == same


# ── (b) re-rendering a message that waited ───────────────────────────
def _queued(world, *, event='lead.assigned', minutes_ago=90, detail=''):
    spec = {'event': event, 'entity_type': 'Lead',
            'entity_id': str(world['lead']), 'detail': detail,
            'actor': '', 'url': f'/app?lead={world["lead"]}'}
    import json
    row = EmailOutbox(
        dedupe_key=f'sa-{event}-{minutes_ago}',
        event_key=event, user_code=PIC,
        to_addr='sapic@procamgroup.in',
        subject='Stale subject', html='<p>Stale body</p>',
        text_body='Stale body', status='queued',
        entity_type='Lead', entity_id=str(world['lead']),
        render_json=json.dumps(spec),
        created_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
        not_before=datetime.utcnow() - timedelta(minutes=minutes_ago))
    db.session.add(row)
    db.session.commit()
    return row


def test_a_delayed_email_is_rebuilt_from_the_record_as_it_stands(world):
    with flask_app.app_context():
        row = _queued(world)
        lead = db.session.get(Lead, world['lead'])
        lead.company = TAG + 'Pandacs Logistics Pvt Ltd'
        lead.updated_at = datetime.utcnow()
        db.session.commit()

        assert outbox.refresh(row) is True
        assert 'Pandacs Logistics Pvt Ltd' in row.subject
        assert 'Stale body' not in (row.text_body or '')


def test_a_changed_record_says_so_at_the_top(world):
    with flask_app.app_context():
        row = _queued(world)
        lead = db.session.get(Lead, world['lead'])
        lead.stage = 'Under Negotiation'
        lead.updated_at = datetime.utcnow()
        db.session.commit()

        outbox.refresh(row)
        assert row.text_body.startswith(rules.CHANGED_NOTE)
        assert rules.CHANGED_NOTE in row.html


def test_an_unchanged_record_gets_no_such_line(world):
    """The note has to mean something. A record nobody touched must
    not carry it."""
    with flask_app.app_context():
        row = _queued(world, minutes_ago=90)
        lead = db.session.get(Lead, world['lead'])
        lead.updated_at = datetime.utcnow() - timedelta(days=2)
        db.session.commit()

        outbox.refresh(row)
        assert rules.CHANGED_NOTE not in (row.text_body or '')
        assert rules.CHANGED_NOTE not in (row.html or '')


def test_the_event_the_actor_and_the_detail_are_not_rewritten(world):
    """The mail still says what happened and who did it. Only the
    description of the record is refreshed."""
    with flask_app.app_context():
        row = _queued(world, event='lead.stage_changed',
                      detail='Quoted → Under Negotiation.')
        lead = db.session.get(Lead, world['lead'])
        lead.stage = 'Won'
        lead.updated_at = datetime.utcnow()
        db.session.commit()

        outbox.refresh(row)
        assert 'Quoted → Under Negotiation.' in row.text_body


def test_a_row_with_no_render_spec_is_sent_as_queued(world):
    """Reports, digests and bulk summaries have no single record
    behind them. They must go out exactly as written."""
    with flask_app.app_context():
        row = EmailOutbox(dedupe_key='sa-plain', event_key='report.daily',
                          to_addr='sapic@procamgroup.in',
                          subject='Your actions', html='<p>Board</p>',
                          status='queued')
        db.session.add(row)
        db.session.commit()
        assert outbox.refresh(row) is False
        assert row.subject == 'Your actions'


def test_a_deleted_record_does_not_lose_the_message(world):
    with flask_app.app_context():
        row = _queued(world)
        Lead.query.filter_by(id=world['lead']).delete(
            synchronize_session=False)
        db.session.commit()
        assert outbox.refresh(row) is False
        assert row.subject == 'Stale subject'
        assert row.html == '<p>Stale body</p>'


def test_re_rendering_never_raises(world):
    with flask_app.app_context():
        row = _queued(world)
        row.render_json = '{not json at all'
        assert outbox.refresh(row) is False
        assert rules.rerender(None) is None
        assert rules.rerender({'event': 'no.such.event'}) is None


def test_the_worker_re_renders_before_it_sends(world):
    """End to end: the row the mail server is handed is the rebuilt
    one, not the snapshot."""
    with flask_app.app_context():
        row = _queued(world)
        lead = db.session.get(Lead, world['lead'])
        lead.company = TAG + 'Renamed Client Ltd'
        lead.updated_at = datetime.utcnow()
        db.session.commit()

        seen = {}

        def fake_send(to, subject, html, **kw):
            seen['subject'], seen['html'] = subject, html
            return {'ok': True}

        outbox.send_group([row], send=fake_send)
        assert 'Renamed Client Ltd' in seen['subject']
        assert rules.CHANGED_NOTE in seen['html']
        assert row.status == 'sent'
