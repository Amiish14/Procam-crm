"""The decisions the leads mailbox makes.

Each of these is a case where a real enquiry used to vanish: a
forwarded email with no external sender, a second RFQ in a running
thread, an enquiry against a lead somebody had closed, a forward that
says who it is for and was assigned to nobody.
"""
import os
import sys
import tempfile
from datetime import datetime

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'ingest-rules-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'IngestRules12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'ingestrules.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead

from app.models.ingest_log import MailIngestLog           # noqa: E402
from app.services import mail_ingest                      # noqa: E402

TAG = 'MIR-'
PIC = 'MIRSUR'


def _wipe():
    MailIngestLog.query.filter(
        MailIngestLog.subject.like(TAG + '%')).delete(
            synchronize_session=False)
    Lead.query.filter(Lead.company.like(TAG + '%')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like(TAG + '%')).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        _wipe()
        for code, name in ((PIC, 'Suranjan Aon'),
                           ('MIRANB', 'Anubhav Rai'),
                           ('MIRANC', 'Anita Chopra')):
            e = Employee.query.filter_by(emp_code=code).first() \
                or Employee(emp_code=code)
            e.name, e.is_active, e.must_change_pw = name, True, False
            e.email = f'{code.lower()}@procamgroup.in'
            e.role, e.session_version = 'user', 0
            e.is_super_admin = e.is_vertical_head = False
            db.session.add(e)
        db.session.commit()
        yield
        _wipe()


def _msg(**kw):
    base = {'internetMessageId': '<mir-1@customer.test>',
            'conversationId': 'CONV-1',
            'subject': TAG + 'RFQ for 3 transformers',
            'receivedDateTime': '2026-10-01T06:30:00Z',
            'from': {'emailAddress': {'address': 'buyer@customer.test'}}}
    base.update(kw)
    return base


# ── the log ──────────────────────────────────────────────────────────
def test_every_outcome_is_recorded(world):
    for outcome in ('created', 'attached', 'reopened', 'skipped', 'error'):
        mail_ingest.record(outcome,
                           _msg(internetMessageId=f'<{outcome}@x.test>',
                                subject=TAG + outcome),
                           reason=f'because {outcome}', commit=True)
    rows = MailIngestLog.query.filter(
        MailIngestLog.subject.like(TAG + '%')).all()
    assert {r.outcome for r in rows} == {'created', 'attached', 'reopened',
                                         'skipped', 'error'}
    assert all(r.reason for r in rows)
    assert all(r.received_at == datetime(2026, 10, 1, 6, 30) for r in rows)


def test_the_same_message_updates_one_row(world):
    mail_ingest.record('skipped', _msg(), reason='first look', commit=True)
    mail_ingest.record('created', _msg(), lead_id=7, reason='on reflection',
                       commit=True)
    rows = MailIngestLog.query.filter_by(
        internet_message_id='<mir-1@customer.test>').all()
    assert len(rows) == 1
    assert rows[0].outcome == 'created' and rows[0].lead_id == 7


# ── de-duplication ───────────────────────────────────────────────────
def test_the_same_message_id_is_a_duplicate(world):
    mail_ingest.record('created', _msg(), lead_id=1, commit=True)
    assert mail_ingest.already_ingested('<mir-1@customer.test>')


def test_a_second_rfq_in_the_same_thread_is_not_a_duplicate(world):
    """Deduplicating on the conversation is how a real second enquiry
    gets swallowed by the first."""
    mail_ingest.record('created', _msg(), lead_id=1, commit=True)
    assert not mail_ingest.already_ingested('<mir-2@customer.test>')


def test_a_skipped_message_does_not_block_a_later_look(world):
    mail_ingest.record('skipped', _msg(), reason='looked like a newsletter',
                       commit=True)
    assert not mail_ingest.already_ingested('<mir-1@customer.test>'), (
        'a message we declined once must still be convertible by hand')


# ── reopening ────────────────────────────────────────────────────────
@pytest.mark.parametrize('stage', ['Lost', 'Not Interested', 'On Hold', 'Won'])
def test_an_enquiry_against_a_closed_lead_reopens_it(world, stage,
                                                     monkeypatch):
    monkeypatch.setattr('app.services.notification_rules.dispatch',
                        lambda *a, **kw: {})
    lead = Lead(company=TAG + 'Closed Customer', stage=stage,
                assigned_to=PIC, lost_reason='Went quiet')
    db.session.add(lead)
    db.session.commit()

    assert mail_ingest.reopen_for(lead, _msg(), classification='rfq')
    db.session.commit()
    assert lead.stage == 'RFQ Generated'
    assert 'Reopened by inbound email' in (lead.history or '')
    assert lead.lost_reason is None
    assert lead.received_at == datetime(2026, 10, 1, 6, 30)


def test_an_open_lead_is_not_reopened(world):
    lead = Lead(company=TAG + 'Live Customer', stage='Quoted')
    db.session.add(lead)
    db.session.commit()
    assert not mail_ingest.reopen_for(lead, _msg(), classification='rfq')
    assert lead.stage == 'Quoted'


def test_a_thank_you_note_does_not_reopen_a_lost_lead(world):
    lead = Lead(company=TAG + 'Closed Customer', stage='Lost')
    db.session.add(lead)
    db.session.commit()
    assert not mail_ingest.reopen_for(lead, _msg(), classification='other')
    assert lead.stage == 'Lost'


def test_reopening_is_announced(world, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        'app.services.notification_rules.dispatch',
        lambda event, record=None, **kw: seen.update(
            {'event': event, 'lead': getattr(record, 'id', None)}) or {})
    lead = Lead(company=TAG + 'Closed Customer', stage='Lost', assigned_to=PIC)
    db.session.add(lead)
    db.session.commit()
    mail_ingest.reopen_for(lead, _msg(), classification='enquiry')
    assert seen['event'] == 'lead.reopened' and seen['lead'] == lead.id


# ── internal-only forwards ───────────────────────────────────────────
INTERNAL = ('procamgroup.in', 'procamlogistics.com')

FORWARD = """Dear Suranjan,

Please take this up.

-----Original Message-----
From: Anil Kale <anil.kale@steelworks.test>
Sent: Monday, September 1, 2026 10:42 AM
To: rakesh@procamgroup.in
Subject: Transformer movement enquiry

We need 3 transformers moved from Pune to Paradip.

Anil Kale
Steelworks Engineering Pvt Ltd
"""


def test_a_client_is_found_inside_an_internal_forward(world):
    found = mail_ingest.client_in_forward(FORWARD, internal_domains=INTERNAL)
    assert found['email'] == 'anil.kale@steelworks.test'
    assert found['how']


def test_our_own_addresses_are_not_mistaken_for_the_client(world):
    body = ("FYI\n\n-----Original Message-----\n"
            "From: rakesh@procamgroup.in\nSent: 1 September 2026 10:42\n"
            "Subject: internal note\n\nnothing here\n")
    found = mail_ingest.client_in_forward(body, internal_domains=INTERNAL)
    assert found['email'] == ''


def test_a_company_named_in_a_signature_is_picked_up(world):
    body = ("Please handle.\n\n-----Original Message-----\n"
            "From: someone@unknown.test\nSubject: enquiry\n\n"
            "Regards,\nAnil\nSteelworks Engineering Pvt Ltd\n")
    found = mail_ingest.client_in_forward(body, internal_domains=INTERNAL)
    assert 'Steelworks' in found['company']


def test_a_forwarded_client_is_matched_to_an_existing_account(world):
    db.session.add(Company(name=TAG + 'Steelworks Engineering Limited',
                           is_active=True))
    db.session.commit()
    account = mail_ingest.match_account(TAG + 'Steelworks Engineering Pvt Ltd')
    assert account is not None and 'Steelworks' in account.name


# ── the client's own date ────────────────────────────────────────────
def test_the_clients_date_is_read_from_the_forwarded_header(world):
    from email_ingest import parser as email_parser
    assert email_parser.client_sent_datetime({}, FORWARD) == \
        datetime(2026, 9, 1, 10, 42)


def test_a_forward_is_dated_when_it_reached_us_not_when_it_was_written(world):
    """The whole point of keeping both: an enquiry from the 1st that a
    colleague forwards on the 14th is work that arrived on the 14th."""
    from email_ingest import parser as email_parser
    msg = _msg(receivedDateTime='2026-09-14T04:00:00Z')
    received = email_parser.received_datetime(msg)
    client = email_parser.client_sent_datetime(msg, FORWARD)
    assert received == datetime(2026, 9, 14, 4, 0)
    assert client == datetime(2026, 9, 1, 10, 42)
    assert received > client


# ── who it is for ────────────────────────────────────────────────────
@pytest.mark.parametrize('body', [
    'Dear Suranjan,\n\nplease take this up.',
    'Hi Suranjan\nFYI',
    '@Suranjan please quote',
    'Suranjan, please take this up',
])
def test_a_forward_that_names_one_person_assigns_to_them(world, body):
    emp, name = mail_ingest.assignee_from_forward(body)
    assert emp is not None and emp.emp_code == PIC, (body, name)


def test_an_ambiguous_name_assigns_to_nobody(world):
    """Two people called Anu*. Guessing is worse than the triage
    screen, which at least shows the lead to everybody."""
    emp, _name = mail_ingest.assignee_from_forward('Dear Ani, please handle')
    assert emp is None


def test_a_forward_naming_nobody_assigns_to_nobody(world):
    emp, _name = mail_ingest.assignee_from_forward('FYI, see below')
    assert emp is None
