"""Quote / RFQ escalation: when each level fires, who it reaches, and
the guarantee that it fires once.

The timings are configuration, so these tests cover both halves: what
happens when Master Data says nothing (the documented defaults), and
what happens when an administrator changes an hour.
"""
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, time, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'escalation-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'EscalationTest12345')
_DB_PATH = os.path.join(tempfile.mkdtemp(), 'escalation.db')
os.environ['DATABASE_URL'] = 'sqlite:///' + _DB_PATH

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.master_data import service as md                 # noqa: E402
from app.models.escalation import EscalationLog           # noqa: E402
from app.models.master_data import MasterItem, MasterList  # noqa: E402
from app.models.notification import Notification          # noqa: E402
from app.models.rfq import RFQ                            # noqa: E402
from app.services import escalation                       # noqa: E402
from app.services import sales_rules as rules             # noqa: E402

OWNER, HEAD, TOP = 'ESOWNER', 'ESHEAD', 'ESTOP'
TAG = 'ES-'
#: Fixed so the arithmetic in these tests is arithmetic, not "roughly".
RECEIVED = date(2026, 10, 1)
DUE = date(2026, 10, 5)


def _emp(code, name, vertical='Escalation Vertical', head=False,
         reports_to=None, super_admin=False):
    e = Employee.query.filter_by(emp_code=code).first() or Employee(emp_code=code)
    e.name, e.vertical, e.is_active, e.must_change_pw = name, vertical, True, False
    e.role, e.is_vertical_head, e.is_super_admin = 'user', head, super_admin
    e.email, e.session_version = f'{code.lower()}@example.test', 0
    e.vertical_head_id = reports_to
    db.session.add(e)
    db.session.flush()
    return e


def _wipe():
    ids = [r.id for r in RFQ.query.filter(RFQ.rfq_number.like(TAG + '%'))]
    if ids:
        EscalationLog.query.filter(EscalationLog.entity_id.in_(ids)).delete(
            synchronize_session=False)
        RFQ.query.filter(RFQ.id.in_(ids)).delete(synchronize_session=False)
    Notification.query.filter(Notification.user_id.in_(
        [OWNER, HEAD, TOP])).delete(synchronize_session=False)
    Company.query.filter(Company.name.like(TAG + '%')).delete(
        synchronize_session=False)
    MasterItem.query.filter_by(list_key=escalation.LIST_KEY).delete(
        synchronize_session=False)
    MasterList.query.filter_by(key=escalation.LIST_KEY).delete(
        synchronize_session=False)


@pytest.fixture()
def world(monkeypatch):
    """One RFQ, awaiting a quote, owned by somebody with a head above
    them and a level above that. No Master Data: the defaults apply."""
    from email_ingest import notifier
    monkeypatch.setattr(notifier, 'send', lambda *a, **kw: True)
    with flask_app.app_context():
        db.create_all()
        escalation.ensure_table()
        _wipe()
        top = _emp(TOP, 'Company Level', super_admin=True)
        head = _emp(HEAD, 'Vertical Head', head=True, reports_to=top.id)
        _emp(OWNER, 'RFQ Owner', reports_to=head.id)
        acc = Company(name=TAG + 'Waiting Customer Ltd',
                      pic_emp_code=OWNER)
        db.session.add(acc)
        db.session.flush()
        rfq = RFQ(rfq_number=TAG + '0001', subject='Two reactors to site',
                  account_id=acc.id, received_date=RECEIVED,
                  quote_by_date=DUE, lead_driver=OWNER, status='Received',
                  created_at=datetime.combine(RECEIVED, time(0, 0))
                  - rules.BUSINESS_TZ)
        db.session.add(rfq)
        db.session.commit()
        global _OUR_RFQ
        _OUR_RFQ = rfq.id
        yield {'rfq': rfq.id, 'account': acc.id}
        _OUR_RFQ = None
        _wipe()
        db.session.commit()


def _at(day, hour=9):
    """A moment on the server's clock (UTC), from a business date."""
    return datetime.combine(day, time(hour, 0)) - rules.BUSINESS_TZ


#: Set by the fixture: the suite shares one database, so other modules'
#: RFQs are in the sweep too and the assertions here are about this one.
_OUR_RFQ = None


def _pending(now, rfq_id=None, **kw):
    """What the sweep would send for this module's own RFQ."""
    with flask_app.app_context():
        plans = escalation.pending(now=now, **kw)
    wanted = rfq_id or _OUR_RFQ
    if wanted is None:
        return plans
    return [p for p in plans if p.get('entity_id') == wanted]


def _stages(plans):
    return [p['stage'] for p in plans]


def _logs(world):
    return EscalationLog.query.filter_by(
        entity_type='rfq', entity_id=world['rfq']).all()


# ── the configuration ────────────────────────────────────────────────
def test_the_documented_defaults_apply_when_master_data_says_nothing(world):
    with flask_app.app_context():
        assert md.items(escalation.LIST_KEY) == []
        ladder = {s['key']: s for s in escalation.ladder()}
    assert [s for s in ladder] == list(escalation.STAGE_KEYS)
    assert all(s['source'] == 'default' for s in ladder.values())
    assert ladder['rfq_received']['hours'] == 0
    assert ladder['owner_reminder']['hours'] == 24
    assert ladder['approaching_deadline']['hours'] == -24
    assert ladder['overdue']['hours'] == 0
    assert ladder['vertical_head']['hours'] == 24
    assert ladder['next_level']['hours'] == 72
    assert escalation.max_backlog_hours() == escalation.MAX_BACKLOG_HOURS


def test_an_administrator_changes_an_hour_without_a_deploy(world):
    with flask_app.app_context():
        escalation.ensure_rules()
        item = MasterItem.query.filter_by(
            list_key=escalation.LIST_KEY, code='owner_reminder').first()
        md.update_item(item.id, meta={'hours': 6, 'anchor': 'received',
                                      'to': 'owner'})
        stage = {s['key']: s for s in escalation.ladder()}['owner_reminder']
        assert stage['hours'] == 6 and stage['source'] == 'master data'

    # Five hours after it arrived: not yet. Seven: due.
    early = _pending(_at(RECEIVED, 0) + timedelta(hours=5))
    late = _pending(_at(RECEIVED, 0) + timedelta(hours=7))
    assert 'owner_reminder' not in _stages(early)
    assert 'owner_reminder' in _stages(late)


def test_a_nonsense_hour_falls_back_to_the_default_rather_than_guessing(world):
    with flask_app.app_context():
        escalation.ensure_rules()
        item = MasterItem.query.filter_by(
            list_key=escalation.LIST_KEY, code='owner_reminder').first()
        md.update_item(item.id, meta={'hours': 'soonish'})
        stage = {s['key']: s for s in escalation.ladder()}['owner_reminder']
    assert stage['hours'] == escalation.BY_KEY['owner_reminder']['hours']


def test_deactivating_a_level_switches_it_off(world):
    before = _pending(_at(DUE, 23) + timedelta(days=2))
    assert 'vertical_head' in _stages(before)
    with flask_app.app_context():
        escalation.ensure_rules()
        item = MasterItem.query.filter_by(
            list_key=escalation.LIST_KEY, code='vertical_head').first()
        md.update_item(item.id, is_active=False)
    after = _pending(_at(DUE, 23) + timedelta(days=2))
    assert 'vertical_head' not in _stages(after)


def test_seeding_the_configuration_is_idempotent(world):
    with flask_app.app_context():
        first = escalation.ensure_rules()
        second = escalation.ensure_rules()
        codes = {i.code for i in md.items(escalation.LIST_KEY)}
    assert first > 0 and second == 0
    assert set(escalation.STAGE_KEYS) <= codes
    assert escalation.MAX_BACKLOG_KEY in codes


# ── when each level fires ────────────────────────────────────────────
def test_the_ladder_climbs_in_order_as_the_days_pass(world):
    # The day it arrived: only the acknowledgement.
    assert _stages(_pending(_at(RECEIVED, 9))) == ['rfq_received']
    # A day later: the owner's reminder joins it.
    assert set(_stages(_pending(_at(RECEIVED + timedelta(days=1), 9)))) == {
        'rfq_received', 'owner_reminder'}
    # The warning lands a day before the deadline, which — because the
    # deadline is the end of the due day — is the moment that day begins.
    assert 'approaching_deadline' not in _stages(
        _pending(_at(DUE - timedelta(days=1), 12)))
    assert 'approaching_deadline' in _stages(_pending(_at(DUE, 1)))
    # The day after: overdue, and nothing above it yet.
    day_after = _stages(_pending(_at(DUE + timedelta(days=1), 9)))
    assert 'overdue' in day_after and 'vertical_head' not in day_after
    # A day later again: the vertical head.
    assert 'vertical_head' in _stages(
        _pending(_at(DUE + timedelta(days=2), 9)))
    # Three days past the deadline: the level above.
    assert 'next_level' in _stages(_pending(_at(DUE + timedelta(days=4), 9)))


def test_a_quote_is_not_late_until_the_end_of_the_day_it_is_due(world):
    """A quote sent on Friday afternoon against a Friday deadline is on
    time, so the overdue level must not fire that morning."""
    assert 'overdue' not in _stages(_pending(_at(DUE, 9)))
    assert 'overdue' in _stages(_pending(_at(DUE + timedelta(days=1), 1)))


def test_an_rfq_with_no_deadline_uses_the_service_standard(world):
    with flask_app.app_context():
        rfq = db.session.get(RFQ, world['rfq'])
        rfq.quote_by_date = None
        db.session.commit()
        assert not rules.quote_deadline_is_committed(rfq)
    standard = RECEIVED + timedelta(days=rules.QUOTE_SLA_DAYS)
    assert 'overdue' not in _stages(_pending(_at(standard, 9)))
    assert 'overdue' in _stages(_pending(_at(standard + timedelta(days=1), 1)))


def test_an_rfq_that_has_been_quoted_is_not_escalated(world):
    with flask_app.app_context():
        rfq = db.session.get(RFQ, world['rfq'])
        rfq.status = 'Quoted'
        db.session.commit()
    assert _pending(_at(DUE + timedelta(days=4), 9)) == []


def test_an_ancient_rfq_does_not_flood_the_first_sweep(world):
    """The backlog window: an escalation whose moment passed months ago
    is not sent at all, or every deployment would mail everybody."""
    long_after = _at(DUE, 9) + timedelta(
        hours=escalation.MAX_BACKLOG_HOURS + 24 * 10)
    assert _pending(long_after) == []


# ── who it reaches ───────────────────────────────────────────────────
def test_each_level_reaches_the_seat_above_the_last(world):
    plans = {p['stage']: p for p in
             _pending(_at(DUE + timedelta(days=4), 9))}
    assert plans['overdue']['to'] == OWNER
    assert plans['vertical_head']['to'] == HEAD
    assert plans['next_level']['to'] == TOP


def test_the_owner_is_the_driver_then_the_lead_then_the_account(world):
    with flask_app.app_context():
        rfq = db.session.get(RFQ, world['rfq'])
        rfq.lead_driver = None
        db.session.commit()
        # No driver and no lead: the account's owner carries it.
        assert escalation.owner_of(rfq) == OWNER
        lead = Lead(company=TAG + 'Lead Co', assigned_to=HEAD,
                    stage='Business Discussion')
        db.session.add(lead)
        db.session.flush()
        rfq.lead_id = lead.id
        db.session.commit()
        assert escalation.owner_of(rfq) == HEAD
        db.session.delete(lead)
        db.session.commit()


def test_an_unowned_rfq_reaches_the_only_level_that_can_act(world):
    """With no owner there is nobody to remind and no head above them,
    so those levels send nothing. The company level is still told: an
    unowned RFQ past its quote date is exactly what nobody else sees."""
    with flask_app.app_context():
        rfq = db.session.get(RFQ, world['rfq'])
        rfq.lead_driver = ''
        acc = db.session.get(Company, world['account'])
        acc.pic_emp_code = None
        db.session.commit()
    plans = _pending(_at(DUE + timedelta(days=4), 9))
    # One plan per company-level recipient: this database may hold more
    # than one super admin, so assert the level and that our own company
    # level is among those told, not the number of them.
    assert set(_stages(plans)) == {'next_level'}
    assert TOP in {p['to'] for p in plans}


# ── sending, once ────────────────────────────────────────────────────
def test_a_sweep_sends_what_is_due_and_records_it(world):
    with flask_app.app_context():
        report = escalation.run(now=_at(DUE + timedelta(days=2), 9))
        assert report['sent'] and not report['failed']
        logs = _logs(world)
        assert {l.stage for l in logs} >= {'rfq_received', 'owner_reminder',
                                           'overdue', 'vertical_head'}
        notes = Notification.query.filter(
            Notification.user_id.in_([OWNER, HEAD])).all()
        assert notes and all(n.action_url == f'/rfqs/{world["rfq"]}'
                             for n in notes)
        from app.models.audit import AuditEvent
        assert AuditEvent.query.filter_by(action='escalation.sent').first()


def test_the_same_escalation_never_fires_twice(world):
    when = _at(DUE + timedelta(days=2), 9)
    with flask_app.app_context():
        first = escalation.run(now=when)
        after_first = len(_logs(world))
        second = escalation.run(now=when)
        assert len(first['sent']) > 0
        assert second['considered'] == 0 and second['sent'] == []
        assert len(_logs(world)) == after_first
        # ... nor fifteen minutes later, nor the next day.
        escalation.run(now=when + timedelta(minutes=15))
        escalation.run(now=when + timedelta(hours=48))
        keys = [l.dedupe_key for l in _logs(world)]
        assert len(keys) == len(set(keys))
        # The next day does bring the next level, and only that.
        assert {l.stage for l in _logs(world)} >= {'next_level'}


def test_sending_goes_through_the_one_notification_path():
    src = open(os.path.join(_ROOT, 'app', 'services', 'escalation.py')).read()
    assert 'from app.services import notify' in src
    assert 'notify.send(' in src
    # Nothing here talks to the mail transport directly.
    assert 'from email_ingest' not in src
    assert 'smtp' not in src.lower() and 'send_mail' not in src


def test_a_notification_failure_does_not_abort_the_sweep(world, monkeypatch):
    """One recipient's mailbox being broken must not keep every other
    escalation in the queue from going out."""
    from app.services import notify
    real = notify.send

    def flaky(user_code, **kw):
        if user_code == HEAD:
            raise RuntimeError('the notification table said no')
        return real(user_code, **kw)

    monkeypatch.setattr(notify, 'send', flaky)
    with flask_app.app_context():
        report = escalation.run(now=_at(DUE + timedelta(days=2), 9))
        assert report['failed'] and all(p['to'] == HEAD
                                        for p in report['failed'])
        assert report['sent'] and all(p['to'] == OWNER
                                      for p in report['sent'])
        # Nothing was recorded for the one that failed, so it is retried.
        assert 'vertical_head' not in {l.stage for l in _logs(world)}
    monkeypatch.undo()
    with flask_app.app_context():
        again = escalation.run(now=_at(DUE + timedelta(days=2), 9))
        assert [p['stage'] for p in again['sent']] == ['vertical_head']


def test_a_refused_notification_is_retried_not_recorded(world, monkeypatch):
    from app.services import notify
    monkeypatch.setattr(notify, 'send', lambda *a, **kw: {
        'notified': False, 'emailed': False, 'suppressed': False,
        'error': 'the mail server refused it'})
    with flask_app.app_context():
        report = escalation.run(now=_at(RECEIVED, 9))
        assert report['sent'] == [] and report['failed']
        assert _logs(world) == []


# ── the sweep script ─────────────────────────────────────────────────
def _db_url():
    """The database the app is really bound to.

    The suite shares one process, so whichever test module imported the
    app first chose the file. A child process has to be pointed at that
    one, not at the path this module would have liked.
    """
    with flask_app.app_context():
        return str(db.engine.url)


def _sweep(*args):
    env = dict(os.environ)
    env['DATABASE_URL'] = _db_url()
    return subprocess.run(
        [sys.executable, os.path.join(_ROOT, 'scripts', 'escalation_sweep.py'),
         *args], capture_output=True, text=True, env=env, cwd=_ROOT,
        timeout=300)


def test_dry_run_prints_what_would_be_sent_and_sends_nothing(world):
    at = str(_at(DUE + timedelta(days=2), 9))[:16]
    out = _sweep('--dry-run', '--verbose', '--at', at)
    assert out.returncode == 0, out.stderr[-2000:]
    assert 'DRY-RUN' in out.stdout and 'nothing sent' in out.stdout
    assert 'vertical_head' in out.stdout
    with flask_app.app_context():
        db.session.expire_all()
        assert _logs(world) == []
        assert Notification.query.filter(
            Notification.user_id.in_([OWNER, HEAD])).count() == 0
        # A dry run does not seed the configuration either.
        assert md.items(escalation.LIST_KEY) == []


def test_the_sweep_sends_and_is_safe_to_run_again(world):
    at = str(_at(DUE + timedelta(days=2), 9))[:16]
    first = _sweep('--verbose', '--at', at)
    assert first.returncode == 0, first.stderr[-2000:]
    assert '[OK]' in first.stdout and 'sent=' in first.stdout
    with flask_app.app_context():
        db.session.expire_all()
        sent = len(_logs(world))
    assert sent > 0
    second = _sweep('--at', at)
    assert second.returncode == 0
    assert 'due=0' in second.stdout
    with flask_app.app_context():
        db.session.expire_all()
        assert len(_logs(world)) == sent


def test_the_migration_is_additive_and_checkable():
    out = subprocess.run(
        [sys.executable,
         os.path.join(_ROOT, 'scripts', '2026_10_10_escalation.py'),
         '--check'], capture_output=True, text=True,
        env=dict(os.environ, DATABASE_URL=_db_url()),
        cwd=_ROOT, timeout=300)
    assert out.returncode == 0, out.stderr[-2000:]
    assert 'DRY-RUN' in out.stdout and 'Nothing backfilled' in out.stdout
