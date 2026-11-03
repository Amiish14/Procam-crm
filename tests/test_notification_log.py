"""Every notification recorded, and the five silences that hid §3.

CRM-USER-ISSUES-02NOV §3: "Some PICs don't get the new-lead mail. Find
how the recipient is resolved and where it fails (no email on the user,
inactive user, wrong field, send error swallowed)."

Each of those is a test here. The point of all of them is the same: a
notification that did not arrive must leave a row saying so. Before
this, every one of these paths returned a dict with an `error` in it
and the caller threw the dict away, so a PIC who got nothing and a PIC
who got their mail were indistinguishable from inside the CRM.
"""
import importlib
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'notifylog-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'NotifyLog-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'notifylog.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Employee = _main.Employee
Lead = _main.Lead

from app.models.notification import Notification            # noqa: E402
from app.models.notification_log import (FAILED, IN_APP_ONLY,  # noqa: E402
                                         QUEUED, SENT, SUPPRESSED,
                                         NotificationAttempt)
from app.services import notify, notify_log                 # noqa: E402

WHO = 'ZN_PIC'
GONE = 'ZN_LEFT'
NOMAIL = 'ZN_NOADDR'
ADMIN = 'ZN_ADM'


def _purge():
    NotificationAttempt.query.delete()
    Notification.query.filter(Notification.user_id.in_(
        (WHO, GONE, NOMAIL, ADMIN))).delete(synchronize_session=False)
    Lead.query.delete()
    Employee.query.filter(Employee.emp_code.in_(
        (WHO, GONE, NOMAIL, ADMIN))).delete(synchronize_session=False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add(Employee(
            emp_code=WHO, name='A Pic', password_hash='x', role='user',
            email='a.pic@procamlogistics.com', is_active=True))
        db.session.add(Employee(
            emp_code=GONE, name='A Leaver', password_hash='x', role='user',
            email='a.leaver@procamlogistics.com', is_active=False))
        db.session.add(Employee(
            emp_code=NOMAIL, name='No Address', password_hash='x',
            role='user', email=None, is_active=True))
        db.session.add(Employee(
            emp_code=ADMIN, name='Admin Person', password_hash='x',
            role='admin', email='zn.adm@procamlogistics.com',
            is_active=True))
        db.session.commit()
        yield
        _purge()


def _rows(code=None, status=None):
    q = NotificationAttempt.query
    if code:
        q = q.filter(NotificationAttempt.user_code == code)
    if status:
        q = q.filter(NotificationAttempt.status == status)
    return q.order_by(NotificationAttempt.id.asc()).all()


def _notify(code, **kw):
    kw.setdefault('kind', 'lead_assigned')
    kw.setdefault('title', 'New lead assigned: Drewes Group')
    kw.setdefault('event_key', 'lead.assigned')
    kw.setdefault('entity_type', 'Lead')
    kw.setdefault('entity_id', 4242)
    return notify.send(code, **kw)


# ── the five silences ────────────────────────────────────────────────
def test_no_address_on_the_employee_is_recorded_not_swallowed():
    with flask_app.app_context():
        out = _notify(NOMAIL, email=True)
        assert out['emailed'] is False
        assert 'no email address' in out['error']
        rows = _rows(NOMAIL, FAILED)
        assert len(rows) == 1
        assert 'no email address' in rows[0].error
        assert rows[0].entity_id == '4242'


def test_an_inactive_employee_is_refused_and_recorded():
    """§3 names "inactive user". It was not checked at all: a leaver's
    address was mailed exactly as if they were still here, and the
    lead looked notified."""
    with flask_app.app_context():
        out = _notify(GONE, email=True)
        assert out['emailed'] is False
        assert 'not active' in out['error']
        rows = _rows(GONE, FAILED)
        assert len(rows) == 1
        # The address is kept even though it was refused, so the
        # reassignment is obvious from the one line.
        assert rows[0].address == 'a.leaver@procamlogistics.com'


def test_a_swallowed_send_error_leaves_a_row(monkeypatch):
    with flask_app.app_context():
        from app.services import mailer

        def _boom(*a, **kw):
            raise RuntimeError('mailbox is full')

        monkeypatch.setattr(mailer, 'send', _boom)
        out = _notify(WHO, email=True)
        assert out['emailed'] is False
        rows = _rows(WHO, FAILED)
        assert len(rows) == 1
        assert 'mailbox is full' in rows[0].error


def test_a_refusal_by_the_transport_leaves_a_row(monkeypatch):
    with flask_app.app_context():
        from app.services import mailer
        monkeypatch.setattr(mailer, 'send', lambda *a, **kw: False)
        out = _notify(WHO, email=True)
        assert out['emailed'] is False
        rows = _rows(WHO, FAILED)
        assert len(rows) == 1
        assert rows[0].address == 'a.pic@procamlogistics.com'


def test_a_suppressed_duplicate_is_recorded_as_suppressed_not_failed():
    """Kept apart from failed, so the failure screen is not buried in
    deliberate silences."""
    with flask_app.app_context():
        _notify(WHO, email=False)
        _notify(WHO, email=False)
        assert len(_rows(WHO, SUPPRESSED)) == 1
        assert len(_rows(WHO, FAILED)) == 0


# ── the happy paths are recorded too ─────────────────────────────────
def test_a_send_that_worked_is_recorded(monkeypatch):
    with flask_app.app_context():
        from app.services import mailer
        monkeypatch.setattr(mailer, 'send', lambda *a, **kw: True)
        out = _notify(WHO, email=True)
        assert out['emailed'] is True
        rows = _rows(WHO, SENT)
        assert len(rows) == 1
        assert rows[0].address == 'a.pic@procamlogistics.com'
        assert rows[0].error is None


def test_a_bell_only_notification_is_recorded_as_in_app():
    """So "was anybody told at all?" is answerable for a lead where no
    mail was ever asked for."""
    with flask_app.app_context():
        _notify(WHO, email=False)
        assert len(_rows(WHO, IN_APP_ONLY)) == 1


def test_a_queued_message_is_recorded_with_its_outbox_row(monkeypatch):
    with flask_app.app_context():
        from app.services import flags, outbox

        monkeypatch.setattr(flags, 'on',
                            lambda name, *a, **kw:
                            name == 'FEATURE_EMAIL_NOTIFY')

        class _Row:
            id = 9191

        monkeypatch.setattr(outbox, 'enqueue', lambda **kw: _Row())
        out = _notify(WHO, email=True)
        assert out['queued'] is True
        rows = _rows(WHO, QUEUED)
        assert len(rows) == 1
        assert rows[0].outbox_id == 9191


def test_an_enqueue_that_was_refused_is_a_failure(monkeypatch):
    with flask_app.app_context():
        from app.services import flags, outbox
        monkeypatch.setattr(flags, 'on',
                            lambda name, *a, **kw:
                            name == 'FEATURE_EMAIL_NOTIFY')
        monkeypatch.setattr(outbox, 'enqueue', lambda **kw: None)
        out = _notify(WHO, email=True)
        assert out['emailed'] is False
        rows = _rows(WHO, FAILED)
        assert len(rows) == 1
        assert 'internal address' in rows[0].error


# ── retry ────────────────────────────────────────────────────────────
def test_the_direct_path_tries_three_times(monkeypatch):
    """It had no retry at all: one throttled response lost the mail."""
    with flask_app.app_context():
        from app.services import mailer
        tries = []

        def _fail(*a, **kw):
            tries.append(1)
            return False

        monkeypatch.setattr(mailer, 'send', _fail)
        _notify(WHO, email=True)
        assert len(tries) == notify_log.DIRECT_ATTEMPTS == 3
        assert _rows(WHO, FAILED)[0].attempts == 3


def test_it_stops_as_soon_as_one_works(monkeypatch):
    with flask_app.app_context():
        from app.services import mailer
        tries = []

        def _second_time(*a, **kw):
            tries.append(1)
            return len(tries) >= 2

        monkeypatch.setattr(mailer, 'send', _second_time)
        _notify(WHO, email=True)
        assert len(tries) == 2
        assert _rows(WHO, SENT)[0].attempts == 2


def test_a_hopeless_error_is_not_tried_three_times():
    """"No address on file" says the same thing every time. Three
    copies of it bury the one fact that matters."""
    assert notify_log.retryable('no email address on file') is False
    assert notify_log.retryable('not an internal address') is False
    assert notify_log.retryable('already queued, muted') is False
    # A transient one is worth retrying, and so is an unknown one: the
    # cost of being wrong is three attempts, not a lost notification.
    assert notify_log.retryable('the mail server refused it') is True
    assert notify_log.retryable('') is True
    assert notify_log.retryable('something nobody has classified') is True


def test_a_person_may_retry_even_a_hopeless_one(monkeypatch):
    """Somebody who has just corrected an address is the case for it."""
    with flask_app.app_context():
        from app.services import mailer
        _notify(NOMAIL, email=True)
        row = _rows(NOMAIL, FAILED)[0]

        # They fix the address, then press the button.
        emp = Employee.query.filter_by(emp_code=NOMAIL).first()
        emp.email = 'fixed@procamlogistics.com'
        db.session.commit()
        monkeypatch.setattr(mailer, 'send', lambda *a, **kw: True)

        out = notify_log.retry(row, actor=ADMIN)
        assert out['ok'] is True
        assert db.session.get(NotificationAttempt,
                              row.id).retried_by == ADMIN


def test_retrying_one_that_still_has_no_address_says_so():
    with flask_app.app_context():
        _notify(NOMAIL, email=True)
        row = _rows(NOMAIL, FAILED)[0]
        out = notify_log.retry(row, actor=ADMIN)
        assert out['ok'] is False
        assert 'no email address' in out['error']


def test_only_a_failed_row_can_be_retried():
    with flask_app.app_context():
        _notify(WHO, email=False)
        row = _rows(WHO, IN_APP_ONLY)[0]
        assert notify_log.retry(row, actor=ADMIN)['ok'] is False


# ── reading it back ──────────────────────────────────────────────────
def test_the_log_answers_which_leads_nobody_heard_about():
    with flask_app.app_context():
        _notify(NOMAIL, email=True, entity_id=777)
        rows = notify_log.for_entity('Lead', 777)
        assert [r.status for r in rows] == [FAILED]


def test_failures_lists_only_failures():
    with flask_app.app_context():
        _notify(WHO, email=False)             # in_app
        _notify(NOMAIL, email=True)           # failed
        assert [r.status for r in notify_log.failures()] == [FAILED]


def test_the_summary_counts_by_status():
    with flask_app.app_context():
        _notify(WHO, email=False)
        _notify(NOMAIL, email=True)
        got = notify_log.summary()
        assert got.get(FAILED) == 1
        assert got.get(IN_APP_ONLY) == 1


# ── it must never break what it is logging ───────────────────────────
def test_a_broken_log_does_not_lose_the_notification(monkeypatch):
    with flask_app.app_context():
        def _boom(**kw):
            raise RuntimeError('the log table is gone')

        monkeypatch.setattr(notify_log, 'record', _boom)
        out = _notify(WHO, email=False)
        # The bell row is what the person actually sees; it survives.
        assert out['notified'] is True
        assert Notification.query.filter_by(user_id=WHO).count() == 1


def test_the_call_site_no_longer_discards_the_result():
    """§3's root cause, as a source guard.

    lead_assignment called notify.send and threw the returned dict
    away, so every documented failure mode became silence.
    """
    import ast

    path = os.path.join(_ROOT, 'app', 'services', 'lead_assignment.py')
    tree = ast.parse(open(path).read())
    bare = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        call = node.value
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if (getattr(func, 'attr', None) == 'send'
                and getattr(getattr(func, 'value', None), 'id', '')
                == 'notify'):
            bare.append(node.lineno)
    assert not bare, (
        f'notify.send result discarded at line(s) {bare} — that is the '
        f'§3 bug: every failure mode it reports becomes silence.')


# ── the screen ───────────────────────────────────────────────────────
def _admin_client():
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess['emp_code'] = ADMIN
        sess['role'] = 'admin'
    return client


def test_the_failure_screen_renders_and_names_the_lead():
    with flask_app.app_context():
        _notify(NOMAIL, email=True, entity_id=5150)
    r = _admin_client().get('/admin/notifications')
    assert r.status_code == 200, r.status_code
    body = r.get_data(as_text=True)
    assert 'no email address on file' in body
    assert 'Lead #5150' in body
    # The code, never the name: people are data.
    assert NOMAIL in body
    assert 'No Address' not in body


def test_the_json_view_answers_too():
    with flask_app.app_context():
        _notify(NOMAIL, email=True)
    body = _admin_client().get('/api/admin/notifications').get_json()
    assert body['ok'] is True
    assert body['rows'] and body['rows'][0]['status'] == FAILED
