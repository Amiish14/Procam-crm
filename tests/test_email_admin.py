"""
Email administration: vertical heads, recipients, switches, addresses.

The ten acceptance tests from the brief, plus the rules they rest on.

The ones that matter most are the two that were silently wrong before
this release. A lead in six of the CRM's nine verticals resolved to no
vertical head at all, so the CC that was meant to reach a head reached
nobody — and a notification with four recipients went out as four
separate emails, so nobody could see who else knew.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'email-admin-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'EmailAdminTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'email_admin.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee, Lead = _main.Employee, _main.Lead
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.email_admin import (FALLBACK_VERTICAL,     # noqa: E402
                                    NotificationPolicy, VerticalAlias,
                                    VerticalHead)
from app.services import admin_settings, flags             # noqa: E402
from app.services import employee_email                    # noqa: E402
from app.services import notification_policy as policy     # noqa: E402
from app.services import notification_rules as rules       # noqa: E402
from app.services import vertical_heads as vh              # noqa: E402

HEAD, DEPUTY, OWNER, DIRECTOR = 'EATHEAD', 'EATDEP', 'EATOWN', 'EATDIR'
TAG = 'EAT-'


def _emp(code, name, *, vertical='', active=True, email=None, role='user'):
    e = (Employee.query.filter_by(emp_code=code).first()
         or Employee(emp_code=code))
    e.name, e.is_active, e.must_change_pw = name, active, False
    # Blank is stored as NULL, not '': `employees.email` is unique, and
    # two people with '' collide — which is also true in production.
    e.email = (f'{name.lower().replace(" ", ".")}@procamgroup.in'
               if email is None else (email.strip() or None))
    e.vertical, e.role, e.session_version = vertical, role, 0
    e.is_super_admin, e.is_vertical_head = False, False
    db.session.add(e)
    db.session.flush()
    return e


def _wipe():
    # The flag rows matter: the database now beats the environment, so
    # a `flag.*` row this module leaves behind turns a feature off for
    # every test module that runs after it in the shared database.
    from app.models.integration import AppSetting
    AppSetting.query.filter(AppSetting.key.like('flag.%')).delete(
        synchronize_session=False)
    Employee.query.filter(Employee.emp_code.like('EAT%')).delete(
        synchronize_session=False)
    VerticalAlias.query.delete(synchronize_session=False)
    VerticalHead.query.delete(synchronize_session=False)
    NotificationPolicy.query.delete(synchronize_session=False)
    Lead.query.filter(Lead.company.like(TAG + '%')).delete(
        synchronize_session=False)
    db.session.commit()
    vh.cache_clear()
    policy.cache_clear()
    flags.cache_clear()


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        _wipe()
        _emp(HEAD, 'Head Person', vertical='Heavy Transport')
        _emp(DEPUTY, 'Deputy Person', vertical='Heavy Transport')
        _emp(OWNER, 'Owner Person', vertical='Heavy Transport')
        _emp(DIRECTOR, 'Director Person', vertical='Management')

        db.session.add_all([
            VerticalHead(vertical='Heavy Transport', head_emp_code=HEAD,
                         deputy_emp_code=DEPUTY, active=True),
            VerticalHead(vertical='Warehousing', active=True),
            VerticalHead(vertical=FALLBACK_VERTICAL,
                         head_emp_code=DIRECTOR, active=True),
        ])
        db.session.add(VerticalAlias(alias='Heavy Cargo',
                                     vertical='Heavy Transport'))
        db.session.commit()
        vh.cache_clear()
        admin_settings.save('always_copy', [], actor='test')
        admin_settings.save('admin_recipients', [], actor='test')
        yield
        _wipe()


def _lead(vertical, owner=OWNER, company=TAG + 'Client'):
    lead = Lead(company=company, procam_vertical=vertical,
                assigned_to=owner, stage='RFQ Generated')
    db.session.add(lead)
    db.session.flush()
    return lead


# ── A. the vertical head master ──────────────────────────────────────
def test_1_an_alias_reaches_the_head_of_the_vertical_it_means(world):
    """Acceptance 1. 'Heavy Cargo' is not in the employee master, and
    before the alias table a lead carrying it had no head at all."""
    assert vh.master_vertical('Heavy Cargo') == 'Heavy Transport'
    assert vh.resolve_vertical_head(_lead('Heavy Cargo')) == [HEAD, DEPUTY]


def test_2_an_unmapped_vertical_falls_back_rather_than_vanishing(world):
    """Acceptance 2. The old behaviour was no recipient, silently."""
    lead = _lead('Something Nobody Has Mapped')
    assert vh.master_vertical(lead.procam_vertical) == FALLBACK_VERTICAL
    assert vh.resolve_vertical_head(lead) == [DIRECTOR]
    assert ('Something Nobody Has Mapped'
            in dict(vh.unmapped_verticals()))


def test_3_a_vertical_with_no_head_is_reported_not_guessed(world):
    """Acceptance 3. Warehousing has a row and nobody in it."""
    assert 'Warehousing' in vh.gaps()
    # It does not silently borrow the fallback's head as its own: the
    # fallback answers, and the gap is still reported.
    assert vh.resolve_vertical_head(_lead('Warehousing')) == [DIRECTOR]


def test_4_the_master_keeps_the_employee_flag_in_step(world):
    """Acceptance 4. Older screens and the escalation ladder still read
    `employees.is_vertical_head`, so the two must not drift."""
    assert Employee.query.filter_by(emp_code=HEAD).first().is_vertical_head \
        is False
    set_now, cleared = vh.sync_employee_flags(actor='test')
    # A subset check, not equality: the test database is shared across
    # modules and other modules' employees carry the flag too.
    assert {HEAD, DEPUTY, DIRECTOR} <= set(set_now)
    assert not ({HEAD, DEPUTY, DIRECTOR} & set(cleared))
    assert Employee.query.filter_by(emp_code=HEAD).first().is_vertical_head
    assert Employee.query.filter_by(emp_code=OWNER).first().is_vertical_head \
        is False


def test_an_inactive_master_row_is_not_used(world):
    row = VerticalHead.query.filter_by(vertical='Heavy Transport').first()
    row.active = False
    db.session.commit()
    vh.cache_clear()
    assert vh.resolve_vertical_head(_lead('Heavy Transport')) == [DIRECTOR]


def test_the_master_cache_expires(world):
    """Two gunicorn workers. A head changed on one must reach the other,
    and a cache with no expiry is why the block register was wrong for
    half of all requests until a restart."""
    assert vh._CACHE_TTL_SECONDS <= 60


# ── B. the recipient policy ──────────────────────────────────────────
def test_5_four_recipients_are_one_email_with_a_real_cc(world, monkeypatch):
    """Acceptance 5. The whole point of the release: not four emails."""
    policy.save('lead.assigned', to_roles=[rules.R_OWNER],
                cc_roles=[rules.R_HEAD], email_enabled=True, actor='test')
    sent = []
    monkeypatch.setattr('app.services.mailer.send_detail',
                        lambda to, subject, html, **kw: (
                            sent.append((to, kw.get('cc'))) or {'ok': True}))
    monkeypatch.setattr('app.services.flags.on',
                        lambda name, default=None: False)

    from app.services import notify
    plan = policy.resolve('lead.assigned', _lead('Heavy Transport'))
    assert plan['to'] == [OWNER]
    assert plan['cc'] == [HEAD, DEPUTY]

    notify.send_policy(plan['to'], plan['cc'], kind='info',
                       title='A lead is yours', email=True,
                       event_key='lead.assigned', in_app=False)
    assert len(sent) == 1, 'one email, not one per person'
    to, cc = sent[0]
    assert len(to) == 1 and len(cc) == 2


def test_6_nobody_is_on_both_lines_and_nobody_is_on_one_twice(world):
    """Acceptance 6. The owner is also the head of their own vertical."""
    row = VerticalHead.query.filter_by(vertical='Heavy Transport').first()
    row.head_emp_code = OWNER
    db.session.commit()
    vh.cache_clear()
    policy.save('lead.assigned', to_roles=[rules.R_OWNER],
                cc_roles=[rules.R_HEAD, rules.R_HEAD],
                email_enabled=True, actor='test')
    plan = policy.resolve('lead.assigned', _lead('Heavy Transport'))
    assert plan['to'] == [OWNER]
    assert OWNER not in plan['cc'], 'the To line wins'
    assert len(plan['cc']) == len(set(plan['cc']))


def test_7_the_standing_copy_is_a_cc_not_another_email(world):
    """Acceptance 7. Seeded with the director who asked for it."""
    admin_settings.save('always_copy', [DIRECTOR], actor='test')
    admin_settings.save('always_copy_enabled', True, actor='test')
    policy.save('lead.assigned', to_roles=[rules.R_OWNER], cc_roles=[],
                email_enabled=True, actor='test')
    plan = policy.resolve('lead.assigned', _lead('Heavy Transport'))
    assert plan['cc'] == [DIRECTOR]

    admin_settings.save('always_copy_enabled', False, actor='test')
    assert policy.resolve('lead.assigned',
                          _lead('Heavy Transport'))['cc'] == []


def test_8_the_policy_version_goes_up_and_is_recorded(world):
    """Acceptance 8. 'Why did that email go there' has to be answerable
    in March about an email sent in November."""
    first = policy.save('lead.assigned', to_roles=[rules.R_OWNER],
                        actor='test')
    second = policy.save('lead.assigned', to_roles=[rules.R_OWNER],
                         actor='test')
    assert second == first + 1, 'a no-op save is still a point in time'
    assert policy.resolve('lead.assigned')['version'] == second


def test_an_absent_row_means_the_built_in_matrix(world):
    """Nothing is seeded, because seeding would be seeding a change."""
    row = rules.rule('lead.assigned')
    pol = policy.policy_for('lead.assigned')
    assert pol['edited'] is False
    assert pol['to_roles'] == list(row['to'])
    assert pol['cc_roles'] == []


def test_a_bad_role_is_refused_rather_than_stored(world):
    with pytest.raises(ValueError):
        policy.save('lead.assigned', to_roles=['whoever'], actor='test')
    with pytest.raises(ValueError):
        policy.save('no.such.event', to_roles=[rules.R_OWNER], actor='test')


def test_reset_returns_to_the_matrix(world):
    policy.save('lead.assigned', to_roles=[rules.R_ADMIN], actor='test')
    assert policy.policy_for('lead.assigned')['edited'] is True
    policy.reset('lead.assigned', actor='test')
    assert policy.policy_for('lead.assigned')['edited'] is False


def test_bell_only_sends_no_email(world):
    policy.save('lead.assigned', to_roles=[rules.R_OWNER],
                email_enabled=False, bell_enabled=True, actor='test')
    plan = policy.resolve('lead.assigned', _lead('Heavy Transport'))
    assert plan['email'] is False and plan['bell'] is True


def test_the_actor_is_not_emailed_about_their_own_click(world):
    policy.save('lead.assigned', to_roles=[rules.R_OWNER],
                cc_roles=[rules.R_HEAD], email_enabled=True, actor='test')
    plan = policy.resolve('lead.assigned', _lead('Heavy Transport'),
                          actor=OWNER)
    assert OWNER not in plan['to'] and OWNER not in plan['cc']


def test_a_cc_only_recipient_is_never_promoted_to_the_to_line(
        world, monkeypatch):
    """The To line carries a client's own documents on some events. An
    unreachable owner must not turn a CC into a To."""
    monkeypatch.setattr('app.services.flags.on',
                        lambda name, default=None: False)
    _emp('EATNOMAIL', 'No Mail', vertical='Heavy Transport', email='')
    db.session.commit()
    from app.services import notify
    out = notify.send_policy(['EATNOMAIL'], [HEAD], kind='info',
                             title='x', email=True, in_app=False)
    assert out['email']['sent'] is False
    assert 'no reachable recipient on the To line' in out['email']['error']


# ── C. the switches ──────────────────────────────────────────────────
def test_9_a_switch_changes_without_a_restart(world):
    """Acceptance 9. Previously a file edit plus a systemctl restart,
    which the person who needed it was usually not able to do."""
    flags.set('FEATURE_WEEKLY_PACK', True, actor='test')
    assert flags.on('FEATURE_WEEKLY_PACK') is True
    assert flags.source_of('FEATURE_WEEKLY_PACK') == 'database'
    flags.set('FEATURE_WEEKLY_PACK', False, actor='test')
    assert flags.on('FEATURE_WEEKLY_PACK') is False


def test_the_database_beats_the_environment_but_not_for_the_kill_switch(
        world, monkeypatch):
    monkeypatch.setenv('FEATURE_WEEKLY_PACK', 'false')
    flags.set('FEATURE_WEEKLY_PACK', True, actor='test')
    assert flags.on('FEATURE_WEEKLY_PACK') is True

    # The exception, deliberately: a stale row must not be able to
    # countermand a kill switch somebody set in .env.
    monkeypatch.setenv(flags.KILL_SWITCH, 'true')
    flags.set(flags.KILL_SWITCH, False, actor='test')
    assert flags.mail_stopped() is True
    assert flags.source_of(flags.KILL_SWITCH) == 'environment'


def test_clearing_a_switch_returns_it_to_the_environment(world, monkeypatch):
    monkeypatch.setenv('FEATURE_WEEKLY_PACK', 'true')
    flags.set('FEATURE_WEEKLY_PACK', False, actor='test')
    assert flags.on('FEATURE_WEEKLY_PACK') is False
    flags.clear('FEATURE_WEEKLY_PACK', actor='test')
    assert flags.on('FEATURE_WEEKLY_PACK') is True
    assert flags.source_of('FEATURE_WEEKLY_PACK') == 'environment'


def test_the_kill_switch_stops_mail_and_loses_nothing(world, monkeypatch):
    from app.services import outbox
    monkeypatch.setattr('app.services.flags.mail_stopped', lambda: True)
    report = outbox.run_once(use_lease=False)
    assert report['skipped'] == 'EMAIL_KILL_SWITCH is on'
    assert report['claimed'] == 0, 'nothing claimed means nothing to lose'


def test_a_held_message_does_not_spend_an_attempt(world):
    """Turning the switch off has to release the queue intact, not one
    attempt closer to failed."""
    from datetime import datetime

    from app.models.mailops import EmailOutbox
    from app.services import outbox

    row = EmailOutbox(dedupe_key='eat-held', to_addr='a@procamgroup.in',
                      subject='x', html='<p>x</p>', status='sending',
                      attempts=0, not_before=datetime.utcnow())
    db.session.add(row)
    db.session.commit()
    outbox.send_group([row], send=lambda *a, **kw: {
        'ok': False, 'held': True, 'error': 'EMAIL_KILL_SWITCH is on'})
    assert row.status == 'queued'
    assert row.attempts == 0


# ── E. the throttle and the SMTP codes ───────────────────────────────
def test_10_a_5xx_fails_at_once_and_a_4xx_waits(world):
    """Acceptance 10. A dead address used to be retried five times,
    which turned one wrong address into five identical log lines and
    buried the reason."""
    from datetime import datetime

    from app.models.mailops import EmailOutbox
    from app.services import outbox

    def _row(key):
        r = EmailOutbox(dedupe_key=key, to_addr='a@procamgroup.in',
                        subject='x', html='<p>x</p>', status='sending',
                        attempts=0, not_before=datetime.utcnow())
        db.session.add(r)
        db.session.commit()
        return r

    hard = _row('eat-5xx')
    outbox.send_group([hard], send=lambda *a, **kw: {
        'ok': False, 'code': 550, 'retry': False,
        'error': '550 5.1.1 Recipient not found'})
    assert hard.status == 'failed'
    assert hard.attempts == 1, 'not retried four more times'
    assert hard.smtp_code == 550
    assert '5.1.1' in hard.last_error, 'the server\'s own words'

    soft = _row('eat-4xx')
    outbox.send_group([soft], send=lambda *a, **kw: {
        'ok': False, 'code': 452, 'retry': True, 'error': '452 4.3.1 full'})
    assert soft.status == 'queued'
    assert soft.smtp_code == 452
    assert soft.not_before > datetime.utcnow()


def test_the_backoff_is_the_schedule_the_brief_asks_for(world):
    from app.services import outbox
    assert outbox.BACKOFF_MINUTES == (2, 5, 15, 30, 60)
    assert len(outbox.BACKOFF_MINUTES) == 5


def test_the_grader_reads_the_smtp_reply_code(world):
    import smtplib

    from app.services import mailer

    hard = mailer._grade(smtplib.SMTPResponseException(
        550, b'5.1.1 Recipient not found'))
    assert hard['code'] == 550 and hard['retry'] is False
    soft = mailer._grade(smtplib.SMTPResponseException(452, b'4.3.1 full'))
    assert soft['code'] == 452 and soft['retry'] is True

    refused = mailer._grade(smtplib.SMTPRecipientsRefused(
        {'nobody@procamgroup.in': (550, b'5.1.1 no such user')}))
    assert refused['code'] == 550 and refused['retry'] is False
    assert 'nobody@procamgroup.in' in refused['error']

    # A connection that never got a reply has no code. That is a 4xx
    # in spirit: the server was unreachable, not unwilling.
    dropped = mailer._grade(OSError('connection reset'))
    assert dropped['code'] is None and dropped['retry'] is True


def test_the_throttle_is_counted_from_what_was_actually_sent(world,
                                                             monkeypatch):
    """Two gunicorn workers and the queue worker all send. A limit each
    of them counts in memory is three times the limit."""
    from app.services import outbox
    monkeypatch.setattr('app.services.outbox.max_per_minute', lambda: 20)
    monkeypatch.setattr('app.services.outbox.sent_in_last_minute',
                        lambda now=None: 18)
    assert outbox.throttle_room() == 2
    monkeypatch.setattr('app.services.outbox.sent_in_last_minute',
                        lambda now=None: 25)
    assert outbox.throttle_room() == 0
    monkeypatch.setattr('app.services.outbox.max_per_minute', lambda: 0)
    assert outbox.throttle_room() is None, '0 means no limit'


def test_the_throttle_holds_rather_than_failing(world, monkeypatch):
    from app.services import outbox
    monkeypatch.setattr('app.services.flags.mail_stopped', lambda: False)
    monkeypatch.setattr('app.services.outbox.max_per_minute', lambda: 20)
    monkeypatch.setattr('app.services.outbox.sent_in_last_minute',
                        lambda now=None: 20)
    report = outbox.run_once(use_lease=False)
    assert 'holding until the next pass' in report['skipped']
    assert report['failed'] == 0


# ── D. employee addresses ────────────────────────────────────────────
def test_a_placeholder_built_from_the_employee_code_is_refused(world):
    for bad in ('emp101@procamgroup.in', 'DIR12010@procamlogistics.com',
                'con77@procamgroup.in'):
        verdict = employee_email.check(bad)
        assert verdict['ok'] is False, bad
        assert 'placeholder' in ' '.join(verdict['errors'])

    # Not every short local part is a placeholder. A real departmental
    # mailbox must not be refused.
    assert employee_email.check('operations@procamgroup.in')['ok'] is True


def test_an_address_the_crm_may_not_send_to_is_refused(world):
    verdict = employee_email.check('someone@gmail.com')
    assert verdict['ok'] is False
    assert 'not allowed to send' in ' '.join(verdict['errors'])
    # A subdomain of an allowed domain is allowed.
    assert employee_email.check('a@mail.procamgroup.in')['ok'] is True


def test_a_duplicate_address_warns_and_does_not_refuse(world):
    """A shared departmental mailbox is sometimes what a department
    wants, and the CRM is not entitled to overrule it."""
    verdict = employee_email.check(
        Employee.query.filter_by(emp_code=HEAD).first().email,
        emp_code=OWNER)
    assert verdict['ok'] is True
    assert verdict['warnings']
    assert HEAD in verdict['warnings'][0]


def test_unreachable_lists_each_reason_once(world):
    _emp('EATBAD1', 'Bad One', email='emp9001@procamgroup.in')
    _emp('EATBAD2', 'Bad Two', email='')
    _emp('EATBAD3', 'Bad Three', email='x@gmail.com')
    db.session.commit()
    found = {r['emp_code']: r['reason']
             for r in employee_email.unreachable()}
    assert 'placeholder' in found['EATBAD1']
    assert 'no email address' in found['EATBAD2']
    assert 'not an allowed domain' in found['EATBAD3']
    assert HEAD not in found


def test_the_import_matches_on_the_code_and_creates_nobody(world, tmp_path):
    path = tmp_path / 'hrms.csv'
    path.write_text(
        'Emp Code,Employee Name,Email ID\n'
        f'{OWNER},Owner Person,owner.new@procamgroup.in\n'
        f'{HEAD},Head Person,{Employee.query.filter_by(emp_code=HEAD).first().email}\n'
        'EATGHOST,Nobody Here,ghost@procamgroup.in\n'
        f'{DEPUTY},Deputy Person,deputy@gmail.com\n')
    plan, problem = employee_email.preview(str(path))
    assert problem == ''
    by_code = {item['emp_code']: item for item in plan}
    assert by_code[OWNER]['action'] == 'update'
    assert by_code[HEAD]['action'] == 'unchanged'
    assert by_code['EATGHOST']['action'] == 'not_found'
    assert by_code[DEPUTY]['action'] == 'invalid'

    report = employee_email.apply_plan(plan, actor='test')
    assert report['updated'] == 1
    assert Employee.query.filter_by(emp_code=OWNER).first().email == \
        'owner.new@procamgroup.in'
    assert Employee.query.filter_by(emp_code='EATGHOST').first() is None


def test_an_import_with_no_code_column_is_refused(world, tmp_path):
    path = tmp_path / 'bad.csv'
    path.write_text('Name,Email\nSomebody,a@procamgroup.in\n')
    plan, problem = employee_email.preview(str(path))
    assert plan == []
    assert 'employee code' in problem


# ── F. the audit trail ───────────────────────────────────────────────
def test_every_change_is_audited(world):
    from app.models.audit import AuditEvent

    def _count(action):
        return AuditEvent.query.filter(AuditEvent.action == action).count()

    before = (_count('notification_policy.changed'), _count('flag.changed'))
    policy.save('lead.assigned', to_roles=[rules.R_OWNER], actor='test')
    flags.set('FEATURE_WEEKLY_PACK', True, actor='test')
    after = (_count('notification_policy.changed'), _count('flag.changed'))
    assert after[0] == before[0] + 1
    assert after[1] == before[1] + 1


def test_the_admin_report_names_the_three_configuration_gaps(world):
    """Each of these is invisible from every other screen: a message
    that was never sent leaves no row to look at."""
    _emp('EATBAD4', 'Bad Four', email='emp9004@procamgroup.in')
    _lead('Something Nobody Has Mapped')
    db.session.commit()

    from app.services import digests
    report = digests.admin_daily()
    assert 'Warehousing' in report['head_gaps']
    assert 'Something Nobody Has Mapped' in dict(report['unmapped_verticals'])
    # The report lists the first few and counts them all; in the shared
    # test database EATBAD4 is well past the section limit.
    assert report['bad_emails_total'] >= 1
    assert 'EATBAD4' in [r['emp_code']
                         for r in employee_email.unreachable(limit=500)]
    assert 'failed_mail' in report


# ── the screens ──────────────────────────────────────────────────────
def _client(emp_code):
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        emp = Employee.query.filter_by(emp_code=emp_code).first()
        sess['emp_code'] = emp_code
        sess['role'] = emp.role
        sess['session_version'] = 0
    return c


@pytest.fixture()
def admin(world):
    emp = Employee.query.filter_by(emp_code=DIRECTOR).first()
    emp.role, emp.is_super_admin = 'admin', True
    db.session.commit()
    from app.access import service as access
    access.effective.cache_clear() if hasattr(
        access.effective, 'cache_clear') else None
    yield DIRECTOR


def test_every_new_screen_answers(admin):
    c = _client(admin)
    for path in ('/admin/email/verticals', '/admin/email/recipients',
                 '/admin/email/switches', '/admin/email/people',
                 '/admin/email/people/import', '/admin/email/log'):
        res = c.get(path)
        assert res.status_code == 200, f'{path} -> {res.status_code}'


def test_a_vertical_head_may_read_the_master_but_not_change_it(world):
    emp = Employee.query.filter_by(emp_code=HEAD).first()
    emp.is_vertical_head = True
    db.session.commit()
    c = _client(HEAD)
    assert c.get('/admin/email/verticals').status_code == 200
    # Writing needs admin.email, which a head does not have.
    assert c.post('/admin/email/verticals/save',
                  data={'vertical': 'Heavy Transport'}).status_code == 403


def test_somebody_with_neither_cannot_read_the_master(world):
    assert _client(OWNER).get(
        '/admin/email/verticals').status_code == 403


def test_saving_a_head_from_the_screen_takes_effect(admin):
    c = _client(admin)
    res = c.post('/admin/email/verticals/save',
                 data={'vertical': 'Warehousing', 'head': OWNER,
                       'active': 'on'})
    assert res.status_code in (302, 303)
    assert vh.resolve_vertical_head(_lead('Warehousing')) == [OWNER]
    assert 'Warehousing' not in vh.gaps()


def test_mapping_an_alias_from_the_screen_takes_effect(admin):
    c = _client(admin)
    res = c.post('/admin/email/verticals/alias',
                 data={'alias': 'General Transport',
                       'maps_to': 'Heavy Transport'})
    assert res.status_code in (302, 303)
    assert vh.master_vertical('General Transport') == 'Heavy Transport'
    assert vh.resolve_vertical_head(
        _lead('General Transport')) == [HEAD, DEPUTY]


def test_the_switches_screen_turns_a_flag_off(admin):
    flags.set('FEATURE_WEEKLY_PACK', True, actor='test')
    c = _client(admin)
    # Every flag the form does not name is turned off, which is how an
    # unticked checkbox has to be read.
    res = c.post('/admin/email/switches', data={'what': 'flags'})
    assert res.status_code == 200
    assert flags.on('FEATURE_WEEKLY_PACK') is False


def test_the_throttle_can_be_changed_from_the_screen(admin):
    c = _client(admin)
    c.post('/admin/email/switches',
           data={'what': 'throttle', 'email_max_per_minute': '35'})
    from app.services import outbox
    assert outbox.max_per_minute() == 35
    admin_settings.save('email_max_per_minute', 20, actor='test')


# ── quiet hours on a shared email ────────────────────────────────────
def test_quiet_hours_hold_a_shared_email_only_if_everybody_is_quiet(world):
    """One email cannot honour four different sets of quiet hours. It
    waits only if everybody on it is in theirs."""
    from datetime import datetime

    from app.services import notify_prefs

    notify_prefs.save(OWNER, email_enabled=True, quiet_enabled=True,
                      quiet_start_hour=21, quiet_end_hour=7)
    # Quiet hours are on by default, so the head has to be turned off
    # explicitly to be the person who is awake.
    notify_prefs.save(HEAD, email_enabled=True, quiet_enabled=False)
    # 22:00 IST is 16:30 UTC — inside the owner's quiet hours.
    night = datetime(2026, 10, 12, 16, 30)

    alone, why = notify_prefs.release_at_for_all([OWNER], now=night)
    assert alone > night and 'quiet hours' in why

    # The head is awake, so the shared email goes now rather than one
    # person's preference delaying everybody else's work.
    together, why = notify_prefs.release_at_for_all([OWNER, HEAD], now=night)
    assert together == night
    assert HEAD in why

    # Everybody quiet: it waits, and only until the first of them is up.
    notify_prefs.save(HEAD, email_enabled=True, quiet_enabled=True,
                      quiet_start_hour=21, quiet_end_hour=6)
    both, why = notify_prefs.release_at_for_all([OWNER, HEAD], now=night)
    assert both > night
    assert HEAD in why, 'the earliest of the two, which is the 06:00 one'
