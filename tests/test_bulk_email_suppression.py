"""A bulk operation does not send one email per record.

Four hundred leads reassigned to one person is one message about four
hundred leads. The failure this prevents is not inconvenience: a
person who receives four hundred notifications turns notifications
off, and the CRM then has no way to reach them about anything.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'bulk-notify-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'BulkNotify12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'bulknotify.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee = _main.Employee

from app.services import notify                           # noqa: E402

WHO = 'BULKONE'
OTHER = 'BULKTWO'


@pytest.fixture()
def world(monkeypatch):
    sent = []
    monkeypatch.setattr('app.services.mailer.send',
                        lambda to, subject, html, **kw:
                        sent.append((to, subject)) or True)
    with flask_app.app_context():
        db.create_all()
        for code, name in ((WHO, 'Bulk One'), (OTHER, 'Bulk Two')):
            e = Employee.query.filter_by(emp_code=code).first() \
                or Employee(emp_code=code)
            e.name, e.is_active, e.must_change_pw = name, True, False
            e.email = f'{code.lower()}@procamgroup.in'
            e.role, e.session_version = 'user', 0
            e.is_super_admin = e.is_vertical_head = False
            db.session.add(e)
        db.session.commit()
        from app.services import notify_prefs
        notify_prefs.save(WHO, quiet_enabled=False)
        notify_prefs.save(OTHER, quiet_enabled=False)
        yield sent


def _forty(code=WHO):
    for i in range(40):
        notify.send(code, kind='lead_assigned', event_key='lead.assigned',
                    title=f'Lead assigned — Customer {i}',
                    body='yours now', email=True, in_app=False,
                    dedupe=False)


def test_without_the_wrapper_every_record_is_its_own_email(world):
    _forty()
    assert len(world) == 40, (
        'the baseline this exists to improve on')


def test_a_bulk_run_sends_one_summary_per_person(world):
    with notify.bulk(what='leads'):
        _forty()
    assert len(world) == 1
    _to, subject = world[0]
    assert subject == '40 leads were updated'


def test_each_person_gets_their_own_summary(world):
    with notify.bulk(what='leads'):
        _forty(WHO)
        for i in range(3):
            notify.send(OTHER, kind='lead_assigned',
                        event_key='lead.assigned',
                        title=f'Lead assigned — Other {i}', email=True,
                        in_app=False, dedupe=False)
    assert len(world) == 2
    subjects = sorted(s for _t, s in world)
    assert subjects == ['3 leads were updated', '40 leads were updated']


def test_one_record_is_still_sent_as_itself(world):
    """A "bulk" run that touched one lead should read like the
    ordinary notification, not like a summary of one thing."""
    with notify.bulk(what='leads'):
        notify.send(WHO, kind='lead_assigned', event_key='lead.assigned',
                    title='Lead assigned — Only Customer', email=True,
                    in_app=False, dedupe=False)
    assert len(world) == 1
    assert world[0][1] == 'Lead assigned — Only Customer'


def test_the_summary_lists_the_records_one_per_line(world, monkeypatch):
    """Forty titles joined by newlines and dropped into a <p> render as
    one unreadable run-on line in an HTML mail client."""
    captured = []
    monkeypatch.setattr('app.services.mailer.send',
                        lambda to, subject, html, **kw:
                        captured.append(html) or True)
    with notify.bulk(what='leads'):
        _forty()
    body = captured[0]
    assert body.count('<li') == 40, (
        'each record needs its own line, not a newline inside a <p>')
    assert 'Customer 0' in body and 'Customer 39' in body


def test_a_very_long_summary_counts_the_rest(world, monkeypatch):
    captured = []
    monkeypatch.setattr('app.services.mailer.send',
                        lambda to, subject, html, **kw:
                        captured.append(html) or True)
    with notify.bulk(what='leads'):
        for i in range(60):
            notify.send(WHO, kind='lead_assigned',
                        event_key='lead.assigned',
                        title=f'Lead assigned - Customer {i}', email=True,
                        in_app=False, dedupe=False)
    body = captured[0]
    assert body.count('<li') == 50
    assert 'and 10 more' in body


# ── a backfill sends nothing at all ──────────────────────────────────
def test_a_silent_run_sends_nothing(world):
    with notify.silent():
        _forty()
    assert world == []


def test_a_silent_run_still_reports_who_would_have_been_told(world):
    with notify.silent() as would_have:
        _forty()
    assert list(would_have) == [WHO]
    assert len(would_have[WHO]) == 40


def test_the_mode_is_put_back_afterwards(world):
    with notify.silent():
        pass
    notify.send(WHO, kind='lead_assigned', event_key='lead.assigned',
                title='After the backfill', email=True, in_app=False,
                dedupe=False)
    assert len(world) == 1, 'suppression leaked past the block it was in'


def test_a_failure_inside_the_block_does_not_leave_it_suppressed(world):
    with pytest.raises(ValueError):
        with notify.bulk(what='leads'):
            notify.send(WHO, kind='lead_assigned',
                        event_key='lead.assigned', title='Before the error',
                        email=True, in_app=False, dedupe=False)
            raise ValueError('something went wrong mid-batch')
    # The summary for what did happen still goes out…
    assert len(world) == 1
    # …and the next ordinary notification is not swallowed.
    notify.send(WHO, kind='lead_assigned', event_key='lead.assigned',
                title='After the error', email=True, in_app=False,
                dedupe=False)
    assert len(world) == 2
