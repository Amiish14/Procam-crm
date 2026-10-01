"""Who the CRM tells what, and the guarantees around telling them.

The matrix in app/services/notification_rules.py is the whole answer to
"who hears about this", so these tests are the record of it: the roles
resolve the way the access layer resolves them, nobody is told twice,
nothing here can break the save that triggered it, and a report run
serves everybody it can even when one recipient fails.
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
os.environ.setdefault('SECRET_KEY', 'notify-rules-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'NotifyRulesTest12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'notify_rules.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Lead, Employee = _main.Lead, _main.Employee
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.access import AccessProfile, DataScope    # noqa: E402
from app.models.notification import Notification          # noqa: E402
from app.services import notification_rules as nrules     # noqa: E402

REP, MATE, HEAD, BOSS = 'NRREP', 'NRMATE', 'NRHEAD', 'NRBOSS'
VERTICAL = 'Warehousing'


def _emp(code, name, *, head=False, admin=False, scope=DataScope.OWN):
    e = Employee.query.filter_by(emp_code=code).first() or Employee(
        emp_code=code)
    e.name, e.vertical, e.is_active, e.must_change_pw = (
        name, VERTICAL, True, False)
    e.role = 'admin' if admin else 'user'
    e.is_super_admin, e.is_vertical_head = False, head
    e.email = f'{code.lower()}@notify.invalid'
    e.session_version = 0
    db.session.add(e)
    db.session.flush()
    AccessProfile.query.filter_by(emp_code=code).delete()
    db.session.add(AccessProfile(emp_code=code, data_scope=scope, perms=[]))
    return e


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        # Only this module's rows: the suite shares one database.
        mine = [l.id for l in Lead.query.filter(Lead.company.like('NR %'))]
        if mine:
            Lead.query.filter(Lead.id.in_(mine)).delete(
                synchronize_session=False)
        Notification.query.filter(Notification.user_id.in_(
            [REP, MATE, HEAD, BOSS])).delete(synchronize_session=False)
        _emp(REP, 'Notified Rep')
        _emp(MATE, 'Second PIC')
        _emp(HEAD, 'Vertical Head', head=True, scope=DataScope.VERTICAL)
        _emp(BOSS, 'Administrator', admin=True, scope=DataScope.ALL)
        lead = Lead(company='NR Acme Ltd', project='NR Pipeline Move',
                    assigned_to=REP, assigned_name=REP,
                    secondary_owner=MATE, stage='Quoted',
                    procam_vertical=VERTICAL,
                    created_at=datetime.utcnow() - timedelta(days=5))
        db.session.add(lead)
        db.session.commit()
        return {'lead': lead.id}


def _lead(world):
    return db.session.get(Lead, world['lead'])


def _notes(code):
    return (Notification.query.filter_by(user_id=code)
            .order_by(Notification.id.desc()).all())


@pytest.fixture()
def no_mail(monkeypatch):
    """Record every outbound email instead of sending one."""
    from email_ingest import notifier
    sent = []
    monkeypatch.setattr(notifier, 'send',
                        lambda to, subject, html, cc=None: sent.append(
                            (to, subject)) or True)
    return sent


# ── the matrix is well formed ────────────────────────────────────────
def test_every_row_carries_what_dispatch_needs():
    assert nrules.MATRIX, 'the matrix is empty'
    for row in nrules.MATRIX:
        for key in nrules.REQUIRED:
            assert key in row, f'{row.get("event")} is missing {key}'
        assert row['event'] and row['kind'] and row['title']
        assert row['to'], f'{row["event"]} reaches nobody'
        assert set(row['to']) <= set(nrules.ROLES), row['event']
        assert row['escalate_to'] in (None,) + nrules.ROLES, row['event']
        if row['escalate_to']:
            assert row['escalate_days'], \
                f'{row["event"]} escalates but says nothing about when'
        assert row['in_app'] or row['email'], \
            f'{row["event"]} is delivered by no channel at all'
        assert row['call_site'], f'{row["event"]} says nothing about where '\
                                 f'it is fired from'


def test_events_are_unique_and_lookup_is_total():
    assert len(nrules.EVENTS) == len(nrules.MATRIX)
    for event in nrules.EVENTS:
        assert nrules.rule(event)['event'] == event


def test_an_unknown_event_is_refused_quietly(world, no_mail):
    """An event nobody has written a row for must not break the save
    that produced it."""
    with flask_app.app_context():
        assert nrules.dispatch('nothing.like.this', _lead(world)) == {}
        assert nrules.rule('nothing.like.this') is None
        assert not _notes(REP)
    assert no_mail == []


def test_the_matrix_cannot_be_edited_through_the_accessor():
    rows = nrules.matrix_rows()
    rows[0]['to'] = ()
    assert nrules.MATRIX[0]['to'], 'the live matrix was mutated'


# ── roles resolve the way the access layer resolves them ─────────────
def test_the_owner_and_the_secondary_pic_are_read_off_the_record(world):
    with flask_app.app_context():
        lead = _lead(world)
        assert nrules.owner_of(lead) == REP
        assert nrules.secondary_of(lead) == MATE


def test_the_vertical_head_is_found_by_vertical(world):
    with flask_app.app_context():
        assert nrules.vertical_head_for(REP) == HEAD
        # A head is not their own escalation.
        assert nrules.vertical_head_for(HEAD) == ''


def test_administrators_are_whoever_holds_the_role(world):
    with flask_app.app_context():
        assert BOSS in nrules.administrators()
        assert REP not in nrules.administrators()


def test_management_can_be_named_without_touching_the_code(world,
                                                           monkeypatch):
    monkeypatch.setenv('REPORT_MANAGEMENT_CODES', f'{HEAD}, {REP}')
    with flask_app.app_context():
        assert nrules.management() == [HEAD, REP]


def test_nobody_is_told_twice_and_nobody_about_their_own_action(world):
    with flask_app.app_context():
        lead = _lead(world)
        people = nrules.recipients_for('lead.stage_changed', lead)
        assert people == [REP, MATE]
        assert nrules.recipients_for('lead.stage_changed', lead,
                                     actor=REP) == [MATE]


def test_escalation_is_never_automatic(world):
    with flask_app.app_context():
        lead = _lead(world)
        assert nrules.recipients_for('lead.followup_overdue', lead) == [REP]
        assert nrules.recipients_for('lead.followup_overdue', lead,
                                     escalate=True) == [REP, HEAD]


# ── dispatch ─────────────────────────────────────────────────────────
def test_dispatch_notifies_the_owner_with_a_link_to_the_record(world,
                                                               no_mail):
    with flask_app.app_context():
        lead = _lead(world)
        out = nrules.dispatch('lead.high_value', lead,
                              detail='Worth more than a crore.')
        assert out[REP]['notified']
        note = _notes(REP)[0]
        assert 'NR Pipeline Move' in note.title
        assert note.action_url == f'/app?lead={lead.id}'
        assert note.entity_type == 'Lead'
        assert 'crore' in note.body
        # The row says the head hears about a big deal too.
        assert _notes(HEAD)


def test_dispatch_emails_only_where_the_row_says_so(world, no_mail):
    with flask_app.app_context():
        lead = _lead(world)
        nrules.dispatch('lead.stage_changed', lead,
                        detail='Quoted → Under Negotiation.')
        assert no_mail == [], 'a silent row sent an email'
        nrules.dispatch('lead.high_value', lead, detail='Big one.')
    assert [to for to, _s in no_mail] == [f'{REP.lower()}@notify.invalid',
                                          f'{HEAD.lower()}@notify.invalid']


def test_an_escalated_message_says_that_it_is_one(world, no_mail):
    with flask_app.app_context():
        nrules.dispatch('lead.followup_overdue', _lead(world),
                        detail='Four days past the date.', escalate=True)
        assert _notes(HEAD)[0].title.startswith('Escalation:')


def test_the_same_notification_twice_is_written_once(world, no_mail):
    with flask_app.app_context():
        lead = _lead(world)
        first = nrules.dispatch('lead.stage_changed', lead, detail='Moved.')
        second = nrules.dispatch('lead.stage_changed', lead, detail='Moved.')
        assert first[REP]['notified'] and second[REP]['suppressed']
        assert len(_notes(REP)) == 1


def test_a_report_row_emails_without_ringing_the_bell(world, no_mail):
    """A digest summarises things that already have notifications of
    their own; a daily 'your report was sent' row would bury them."""
    with flask_app.app_context():
        before = len(_notes(REP))
        out = nrules.dispatch('report.daily', None, roles=[nrules.R_ACTOR],
                              actor=REP, title='Your actions for today',
                              body='See the board.')
        assert out[REP]['emailed'] and not out[REP]['notified']
        assert len(_notes(REP)) == before


def test_notify_enabled_off_stops_the_email_and_nothing_else(world,
                                                             monkeypatch):
    monkeypatch.setenv('NOTIFY_ENABLED', 'false')
    with flask_app.app_context():
        out = nrules.dispatch('lead.high_value', _lead(world),
                              detail='Big one.')
        assert out[REP]['notified'], 'the bell should still ring'
        assert not out[REP]['emailed']


def test_one_bad_recipient_does_not_cost_the_others_theirs(world,
                                                           monkeypatch):
    from email_ingest import notifier
    sent = []

    def flaky(to, subject, html, cc=None):
        if to.startswith(REP.lower()):
            raise RuntimeError('the mail server fell over')
        sent.append(to)
        return True

    monkeypatch.setattr(notifier, 'send', flaky)
    with flask_app.app_context():
        out = nrules.dispatch('lead.high_value', _lead(world),
                              detail='Big one.')
    assert out[REP]['error'] and not out[REP]['emailed']
    assert out[REP]['notified'], 'the bell must survive a mail failure'
    assert out[HEAD]['emailed'] and sent == [f'{HEAD.lower()}@notify.invalid']


def test_a_dispatch_never_raises_at_the_call_site(world, monkeypatch):
    """Whatever goes wrong below, the caller's save has happened and
    must not be undone by a notification."""
    from app.services import notify
    monkeypatch.setattr(notify, 'send',
                        lambda *a, **kw: (_ for _ in ()).throw(
                            RuntimeError('boom')))
    with flask_app.app_context():
        assert nrules.dispatch('lead.stage_changed', _lead(world)) == {}


# ── the report runner ────────────────────────────────────────────────
@pytest.fixture()
def runner():
    """`scripts/send_reports.py`, loaded by path.

    Not by putting scripts/ on sys.path: there is a
    `scripts/email_ingest.py` there, which would shadow the
    `email_ingest` package the whole application imports.
    """
    import importlib.util
    path = os.path.join(_ROOT, 'scripts', 'send_reports.py')
    spec = importlib.util.spec_from_file_location('send_reports_cli', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dry_run_sends_nothing_and_writes_nothing(world, runner, no_mail):
    with flask_app.app_context():
        before = Notification.query.count()
    code = runner.run('daily', only=REP, dry_run=True, send_empty=True,
                      quiet=True)
    assert code == 0
    assert no_mail == [], 'a dry run sent mail'
    with flask_app.app_context():
        assert Notification.query.count() == before


def test_a_dry_run_still_says_what_each_person_would_get(world, runner,
                                                         no_mail, capsys):
    runner.run('daily', only=REP, send_empty=True, dry_run=True)
    out = capsys.readouterr().out
    assert REP in out and 'dry-run' in out
    assert 'daily:' in out


def test_the_run_sends_and_reports_per_recipient(world, runner, no_mail):
    assert runner.run('daily', only=REP, send_empty=True, quiet=True) == 0
    assert [to for to, _s in no_mail] == [f'{REP.lower()}@notify.invalid']


def test_notify_enabled_off_stops_the_whole_run(world, runner, no_mail,
                                                monkeypatch, capsys):
    monkeypatch.setenv('NOTIFY_ENABLED', 'false')
    assert runner.run('daily', only=REP, send_empty=True) == 0
    assert no_mail == []
    out = capsys.readouterr().out
    assert 'disabled' in out and 'NOTIFY_ENABLED is off' in out


def test_one_failing_recipient_does_not_abort_the_run(world, runner,
                                                      no_mail, monkeypatch,
                                                      capsys):
    monkeypatch.setattr(runner, 'recipients',
                        lambda key, only=None: [REP, MATE])
    from app.services import digests
    real_build = digests.build

    def explode(key, emp_code=None, **kw):
        if emp_code == REP:
            raise RuntimeError('that board would not build')
        return real_build(key, emp_code, **kw)

    monkeypatch.setattr(digests, 'build', explode)
    code = runner.run('daily', send_empty=True)
    out = capsys.readouterr().out
    assert code == 1, 'a failed recipient must be reported in the exit code'
    assert f'{REP}' in out and 'failed' in out
    # ... and the other person was still served.
    assert [to for to, _s in no_mail] == [f'{MATE.lower()}@notify.invalid']


def test_an_empty_board_is_skipped_rather_than_mailed(world, runner,
                                                      no_mail, capsys):
    with flask_app.app_context():
        _emp('NRQUIET', 'Nothing To Do')
        db.session.commit()
    assert runner.run('daily', only='NRQUIET', quiet=True) == 0
    assert no_mail == []


def test_the_runner_needs_to_be_told_which_report(runner):
    with pytest.raises(SystemExit) as bad:
        runner.main([])
    assert bad.value.code == 2
    with pytest.raises(SystemExit) as both:
        runner.main(['--daily', '--monthly'])
    assert both.value.code == 2


# ── the matrix and the documentation agree ───────────────────────────
def test_every_event_appears_in_the_operations_document():
    doc = open(os.path.join(_ROOT, 'docs', 'operations',
                            'NOTIFICATIONS.md')).read()
    for row in nrules.MATRIX:
        assert f'`{row["event"]}`' in doc, row['event']


def test_notifications_go_through_the_one_delivery_path():
    src = open(os.path.join(_ROOT, 'app', 'services',
                            'notification_rules.py')).read()
    assert 'from app.services import notify' in src
    # Not a second transport, and not a second Notification writer.
    assert 'graph_client' not in src
    assert 'Notification(' not in src
