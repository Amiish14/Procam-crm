"""Sending as tms@ with the tag, and releasing what was held.

CRM-FINAL-CLOSEOUT-01 §2.

Two halves. The sending address, where the failure mode is silence —
mail that goes out as the ingest mailbox does not bounce, it just
comes back in as a new enquiry — and the release, where the failure
mode is the opposite: a customer asked twice, or told a question has
been sent when the outbox refused it again.
"""
import importlib
import importlib.util
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'held-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Held-123456789')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'held.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
db = _main.db

from app import Lead                                        # noqa: E402
from app.models.audit import AuditEvent                     # noqa: E402
from app.models.integration import AppSetting               # noqa: E402
from app.models.rfq import RFQ                              # noqa: E402
from app.models.task_engine import (TaskInstance,            # noqa: E402
                                    TaskInstanceStatus)
from app.services import admin_settings, mailer             # noqa: E402

SCRIPT = os.path.join(_ROOT, 'scripts',
                      '2026_11_08_release_held_clarifications.py')

OUTCOME = 'Missing: site address / route'
CUSTOMER = 'buyer@zzheld-client.example'


def _script():
    spec = importlib.util.spec_from_file_location('held_rel', SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        yield
        _purge()
        for key in ('crm_mail_from', 'mail_subject_prefix'):
            AppSetting.query.filter_by(key=key).delete()
        db.session.commit()
        for key in ('CRM_MAIL_FROM', 'MAIL_DEFAULT_SENDER',
                    'CRM_EMAIL_ALLOW_EXTERNAL', 'NOTIFY_FROM'):
            os.environ.pop(key, None)


def _purge():
    TaskInstance.query.delete()
    RFQ.query.delete()
    Lead.query.delete()
    AuditEvent.query.filter(
        AuditEvent.entity_type == 'rfq').delete(synchronize_session=False)
    db.session.commit()


def _setting(key, value):
    admin_settings.save(key, value, actor='ZH_TEST')
    db.session.commit()


# ═══════════════════════════════════════════════════════════════════
# the sending address
# ═══════════════════════════════════════════════════════════════════
def test_the_admin_setting_beats_the_environment():
    """The person who needs the address corrected is rarely the
    person who can edit .env and restart gunicorn."""
    with flask_app.app_context():
        os.environ['CRM_MAIL_FROM'] = 'Old <old@procamgroup.in>'
        _setting('crm_mail_from', 'Procam CRM <tms@procamgroup.in>')
        name, addr = mailer.sender()
        assert addr == 'tms@procamgroup.in'
        assert name == 'Procam CRM'


def test_the_environment_still_answers_when_the_setting_is_empty():
    with flask_app.app_context():
        _setting('crm_mail_from', '')
        os.environ['CRM_MAIL_FROM'] = 'Procam CRM <tms@procamgroup.in>'
        assert mailer.sender()[1] == 'tms@procamgroup.in'


def test_tms_with_a_tag_is_the_configuration_with_no_problem():
    with flask_app.app_context():
        _setting('crm_mail_from', 'Procam CRM <tms@procamgroup.in>')
        _setting('mail_subject_prefix', '[CRM]')
        assert mailer.sender_problem() == ''


def test_a_crm_address_is_reported_because_that_mailbox_does_not_exist():
    with flask_app.app_context():
        _setting('crm_mail_from', 'crm@procamgroup.in')
        _setting('mail_subject_prefix', '[CRM]')
        problem = mailer.sender_problem()
        assert 'does not exist' in problem


def test_sending_as_the_ingest_mailbox_is_reported():
    """The silent one. Nothing bounces; a reply to a notification
    lands back in the poller as a new enquiry."""
    with flask_app.app_context():
        from email_ingest import service as mail_service
        inbox = mail_service.crm_inbox_email()
        if not inbox:
            pytest.skip('no ingest mailbox configured in this checkout')
        _setting('crm_mail_from', inbox)
        _setting('mail_subject_prefix', '[CRM]')
        problem = mailer.sender_problem()
        assert 'ingest reads' in problem
        assert 'new enquiry' in problem


def test_no_tag_is_reported_because_one_address_serves_two_systems():
    with flask_app.app_context():
        _setting('crm_mail_from', 'Procam CRM <tms@procamgroup.in>')
        _setting('mail_subject_prefix', '')
        assert 'nothing to tell them apart' in mailer.sender_problem()


def test_the_tag_goes_on_once_and_only_once():
    with flask_app.app_context():
        _setting('mail_subject_prefix', '[CRM]')
        once = mailer.with_prefix('RFQ-000041: one thing we need')
        assert once.startswith('[CRM] ')
        assert mailer.with_prefix(once) == once


def test_the_health_line_says_the_address_the_tag_and_the_problem():
    with flask_app.app_context():
        _setting('crm_mail_from', 'crm@procamgroup.in')
        _setting('mail_subject_prefix', '[CRM]')
        line = mailer.describe()
        assert 'crm@procamgroup.in' in line
        assert '[CRM]' in line
        assert 'does not exist' in line


def test_there_is_one_reader_for_the_tag():
    """`with_prefix` used to read the setting itself. Two readers
    drift, and the one that drifts is the one that stops tagging."""
    source = open(os.path.join(_ROOT, 'app', 'services',
                               'mailer.py')).read()
    assert source.count("'mail_subject_prefix'") == 1


# ═══════════════════════════════════════════════════════════════════
# releasing what was held
# ═══════════════════════════════════════════════════════════════════
def _world(*, waiting=True, held=True, sent=False, address=CUSTOMER):
    lead = Lead(company='ZHeld Client', stage='RFQ Generated',
                source='email', email=address,
                created_at=datetime.utcnow() - timedelta(days=5))
    db.session.add(lead)
    db.session.commit()
    rfq = RFQ(rfq_number='RFQ-ZH0001', subject='40 MVA transformer',
              lead_id=lead.id)
    db.session.add(rfq)
    db.session.commit()
    db.session.add(TaskInstance(
        task_key='rfq.screening', entity_type='RFQ', entity_id=rfq.id,
        step_no=3, status=TaskInstanceStatus.OPEN[0],
        waiting_for_customer=waiting,
        created_at=datetime.utcnow() - timedelta(days=5)))
    if held:
        db.session.add(AuditEvent(
            occurred_at=datetime.utcnow() - timedelta(days=4),
            actor='ZH_OWN', action='rfq.clarification_held',
            entity_type='rfq', entity_id=str(rfq.id),
            new_value={'to': address, 'outcome': OUTCOME,
                       'subject': 'old subject'}))
    if sent:
        db.session.add(AuditEvent(
            occurred_at=datetime.utcnow() - timedelta(days=3),
            actor='ZH_OWN', action='rfq.clarification_sent',
            entity_type='rfq', entity_id=str(rfq.id),
            new_value={'to': address, 'outcome': OUTCOME}))
    db.session.commit()
    return lead, rfq


def test_a_held_clarification_on_a_waiting_step_is_releasable():
    with flask_app.app_context():
        os.environ['CRM_EMAIL_ALLOW_EXTERNAL'] = 'true'
        mod = _script()
        _lead, rfq = _world()
        records = mod.survey()
        assert len(records) == 1
        assert records[0]['rfq_id'] == rfq.id
        assert records[0]['release'] is True
        assert records[0]['to'] == CUSTOMER


def test_the_subject_is_recomposed_not_replayed():
    """The RFQ may have been corrected since. Sending the old
    question makes Procam look like it is not reading its own file."""
    with flask_app.app_context():
        os.environ['CRM_EMAIL_ALLOW_EXTERNAL'] = 'true'
        mod = _script()
        _world()
        record = mod.survey()[0]
        assert record['subject'] != 'old subject'
        assert 'RFQ-ZH0001' in record['subject']


def test_a_step_that_stopped_waiting_is_not_asked_again():
    """Somebody got the answer by phone. Asking again is worse than
    never having asked."""
    with flask_app.app_context():
        os.environ['CRM_EMAIL_ALLOW_EXTERNAL'] = 'true'
        mod = _script()
        _world(waiting=False)
        record = mod.survey()[0]
        assert record['release'] is False
        assert record['why'] == mod.SKIP_STEP


def test_one_already_sent_is_not_sent_twice():
    with flask_app.app_context():
        os.environ['CRM_EMAIL_ALLOW_EXTERNAL'] = 'true'
        mod = _script()
        _world(sent=True)
        record = mod.survey()[0]
        assert record['release'] is False
        assert record['why'] == mod.SKIP_SENT


def test_external_mail_still_off_reports_still_held():
    """The whole reason for running a check first: it must not claim
    a send the policy will refuse again."""
    with flask_app.app_context():
        os.environ.pop('CRM_EMAIL_ALLOW_EXTERNAL', None)
        mod = _script()
        _world()
        record = mod.survey()[0]
        assert record['release'] is False
        assert 'still switched off' in record['why']


def test_an_internal_only_lead_has_nobody_to_ask():
    with flask_app.app_context():
        os.environ['CRM_EMAIL_ALLOW_EXTERNAL'] = 'true'
        mod = _script()
        _world(address='colleague@procamlogistics.com')
        record = mod.survey()[0]
        assert record['release'] is False
        assert 'no customer address' in record['why']


def test_two_outcomes_on_one_rfq_are_two_questions():
    with flask_app.app_context():
        os.environ['CRM_EMAIL_ALLOW_EXTERNAL'] = 'true'
        mod = _script()
        _lead, rfq = _world()
        db.session.add(AuditEvent(
            occurred_at=datetime.utcnow() - timedelta(days=2),
            actor='ZH_OWN', action='rfq.clarification_held',
            entity_type='rfq', entity_id=str(rfq.id),
            new_value={'to': CUSTOMER,
                       'outcome': 'Missing: required date'}))
        db.session.commit()
        outcomes = {r['outcome'] for r in mod.survey()}
        assert outcomes == {OUTCOME, 'Missing: required date'}


def test_the_latest_attempt_for_one_outcome_wins():
    with flask_app.app_context():
        mod = _script()
        _lead, rfq = _world()
        db.session.add(AuditEvent(
            occurred_at=datetime.utcnow() - timedelta(hours=2),
            actor='ZH_OWN', action='rfq.clarification_held',
            entity_type='rfq', entity_id=str(rfq.id),
            new_value={'to': 'newer@zzheld-client.example',
                       'outcome': OUTCOME}))
        db.session.commit()
        records = [r for r in mod.survey() if r['outcome'] == OUTCOME]
        assert len(records) == 1
        assert records[0]['held_to'] == 'newer@zzheld-client.example'


def test_the_dry_run_queues_nothing():
    with flask_app.app_context():
        os.environ['CRM_EMAIL_ALLOW_EXTERNAL'] = 'true'
        from app.models.mailops import EmailOutbox
        mod = _script()
        _world()
        before = EmailOutbox.query.count()
        mod.release(mod.survey(), apply=False)
        db.session.rollback()
        assert EmailOutbox.query.count() == before


def test_the_apply_queues_it_and_the_outbox_holds_it():
    """The apply path, not only the refusal. And it goes through the
    outbox rather than the transport, so the kill switch and the
    subject tag still apply."""
    with flask_app.app_context():
        os.environ['CRM_EMAIL_ALLOW_EXTERNAL'] = 'true'
        _setting('crm_mail_from', 'Procam CRM <tms@procamgroup.in>')
        _setting('mail_subject_prefix', '[CRM]')
        from app.models.mailops import EmailOutbox
        mod = _script()
        _world()
        done, still_held = mod.release(mod.survey(), apply=True)
        assert len(done) == 1, still_held
        assert still_held == []
        row = (EmailOutbox.query
               .order_by(EmailOutbox.id.desc()).first())
        assert row is not None
        assert CUSTOMER in (row.to_addr or row.to_json or '')


def test_a_release_the_outbox_refuses_is_reported_not_swallowed():
    """Otherwise the step stays waiting and the report claims it was
    sent."""
    with flask_app.app_context():
        os.environ.pop('CRM_EMAIL_ALLOW_EXTERNAL', None)
        mod = _script()
        _world()
        # Force it past the policy check in survey() so release()
        # itself has to deal with the refusal.
        records = mod.survey()
        for r in records:
            r['release'] = True
        done, still_held = mod.release(records, apply=True)
        assert done == []
        assert len(still_held) == 1
        assert 'would not take it' in still_held[0]['why']


def test_it_refuses_to_release_while_the_sending_address_is_wrong():
    """These go to customers. Releasing a hundred of them from the
    wrong mailbox is not a thing to discover afterwards."""
    with flask_app.app_context():
        os.environ['CRM_EMAIL_ALLOW_EXTERNAL'] = 'true'
        _setting('crm_mail_from', 'crm@procamgroup.in')
        _setting('mail_subject_prefix', '[CRM]')
        mod = _script()
        _world()
        with pytest.raises(SystemExit) as exc:
            mod.main(['--apply'])
        assert exc.value.code == 2


def test_the_check_run_prints_and_writes_nothing():
    with flask_app.app_context():
        os.environ['CRM_EMAIL_ALLOW_EXTERNAL'] = 'true'
        from app.models.mailops import EmailOutbox
        mod = _script()
        _world()
        before = EmailOutbox.query.count()
        assert mod.main(['--check']) == 0
        assert EmailOutbox.query.count() == before


def test_one_rfq_can_be_released_on_its_own():
    """How to release one and watch what the customer does before
    releasing the rest."""
    with flask_app.app_context():
        os.environ['CRM_EMAIL_ALLOW_EXTERNAL'] = 'true'
        mod = _script()
        _lead, rfq = _world()
        assert mod.main(['--check', '--rfq', str(rfq.id + 500)]) == 0
        assert mod.main(['--check', '--rfq', str(rfq.id)]) == 0


def test_a_word_given_to_rfq_is_refused():
    mod = _script()
    with pytest.raises(SystemExit):
        mod.main(['--check', '--rfq', 'all'])


def test_the_script_sends_nothing_itself():
    """It enqueues. The worker sends — so the kill switch, the
    preferences, the retries and the tag all still apply."""
    import ast

    tree = ast.parse(open(SCRIPT).read())
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = (getattr(node.func, 'attr', None)
                    or getattr(node.func, 'id', None))
            if name:
                called.add(name)
    for forbidden in ('send_detail', 'send_test', 'sendmail', 'smtp'):
        assert forbidden not in called, forbidden
