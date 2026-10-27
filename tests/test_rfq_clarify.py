"""
Asking the customer, and saying so when we could not.

CRM-RFQ-CHAIN-01 §4.5.

The step used to record "waiting for customer" and send nothing,
so an enquiry could sit for a week with everyone believing the
customer was slow.

The test that matters most here is the one about the installation
we actually have: external mail is switched off, so the outbox
declines the row. The caller must be told plainly, because
"queued" when nothing left the building is the same failure as
before, wearing a success message.
"""
import importlib
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'clarify-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Clarify-1234567')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'clarify.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.rfq')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Company, Contact, Employee, Lead = (_main.Company, _main.Contact,
                                    _main.Employee, _main.Lead)

from app.models.rfq import RFQ                            # noqa: E402
from app.services import rfq_clarify as rcl               # noqa: E402

SCM = 'CL_SCM'


def _purge():
    # SQLite reuses rfq ids after a purge, so last test's queued
    # mail is counted against this test's RFQ unless it goes too.
    from app.models.mailops import EmailOutbox
    from app.models.task_engine import TaskInstance
    EmailOutbox.query.filter(
        EmailOutbox.event_key == 'rfq.clarification').delete(
            synchronize_session=False)
    TaskInstance.query.filter(
        TaskInstance.entity_type == 'RFQ').delete(
            synchronize_session=False)
    RFQ.query.filter(RFQ.rfq_number.like('CL-%')).delete(
        synchronize_session=False)
    Lead.query.filter(Lead.company.like('CL %')).delete(
        synchronize_session=False)
    Contact.query.filter(Contact.name.like('CL %')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like('CL %')).delete(
        synchronize_session=False)
    Employee.query.filter_by(emp_code=SCM).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        db.session.add(Employee(emp_code=SCM, name='CL Scm',
                                password_hash='x', role='admin',
                                email='cl@procamlogistics.com'))
        db.session.commit()
        yield
        _purge()


_N = [0]


def _world(customer='buyer@clsteel.com'):
    _N[0] += 1
    account = Company(name='CL Steel', is_active=True)
    db.session.add(account)
    db.session.flush()
    lead = Lead(company='CL Steel', stage='RFQ Generated',
                source='email', email=customer,
                company_id=account.id, pic='Mr Buyer')
    db.session.add(lead)
    db.session.flush()
    rfq = RFQ(rfq_number=f'CL-{_N[0]:04d}', subject='2 x 40ft to Haldia',
              lead_id=lead.id, account_id=account.id,
              priority='Medium')
    db.session.add(rfq)
    db.session.commit()
    return account, lead, rfq


# ── who it goes to ───────────────────────────────────────────────────
def test_it_goes_to_the_customer_on_the_lead():
    with flask_app.app_context():
        _a, lead, rfq = _world()
        to, name = rcl.recipient(rfq, lead)
        assert to == 'buyer@clsteel.com'
        assert name == 'Mr Buyer'


def test_it_never_goes_to_one_of_us():
    """A clarification mailed to ourselves is a step closing on
    nothing."""
    with flask_app.app_context():
        _a, lead, rfq = _world(customer='colleague@procamgroup.in')
        to, _name = rcl.recipient(rfq, lead)
        assert to == ''


def test_it_falls_back_to_the_account_contact():
    with flask_app.app_context():
        account, lead, rfq = _world(customer='')
        db.session.add(Contact(name='CL Contact',
                               email='ops@clsteel.com',
                               account_id=account.id))
        db.session.commit()
        to, name = rcl.recipient(rfq, lead)
        assert to == 'ops@clsteel.com'
        assert name == 'CL Contact'


# ── what it says ─────────────────────────────────────────────────────
@pytest.mark.parametrize('outcome,wants', [
    ('Missing: drawings / dimensions', 'drawings'),
    ('Missing: site address / route', 'delivery site address'),
    ('Missing: scope of work', 'in our scope'),
    ('Missing: required date', 'date you need this by'),
])
def test_it_asks_for_the_thing_that_is_missing(outcome, wants):
    with flask_app.app_context():
        _a, lead, rfq = _world()
        subject, html = rcl.compose(rfq, lead, outcome)
        assert wants in html
        assert rfq.rfq_number in subject
        # Our own note is not the question we ask them.
        assert 'Missing:' not in html


def test_the_reference_is_on_it_so_the_reply_comes_back_here():
    with flask_app.app_context():
        _a, lead, rfq = _world()
        _subject, html = rcl.compose(
            rfq, lead, 'Missing: scope of work')
        assert rfq.rfq_number in html


# ── the installation we actually have ────────────────────────────────
def test_external_mail_being_off_is_reported_not_hidden():
    """"Queued" when nothing left the building is the old failure
    wearing a success message."""
    with flask_app.app_context():
        _a, lead, rfq = _world()
        status, detail = rcl.send(rfq, lead,
                                  'Missing: scope of work',
                                  actor=SCM, commit=True)
        assert status == 'held'
        assert 'not sent' in detail['why']
        assert 'CRM_EMAIL_ALLOW_EXTERNAL' in detail['why']
        assert detail['to'] == 'buyer@clsteel.com'


def test_being_held_is_audited_as_held():
    from app.models.audit import AuditEvent

    with flask_app.app_context():
        _a, lead, rfq = _world()
        rcl.send(rfq, lead, 'Missing: scope of work', actor=SCM,
                 commit=True)
        row = (AuditEvent.query
               .filter(AuditEvent.action == 'rfq.clarification_held',
                       AuditEvent.entity_id == str(rfq.id)).first())
        assert row is not None
        assert 'not sent' in (row.reason or '')


def test_it_queues_when_external_mail_is_allowed(monkeypatch):
    from app.services import mail_policy

    with flask_app.app_context():
        monkeypatch.setattr(mail_policy, 'external_allowed',
                            lambda: True)
        _a, lead, rfq = _world()
        status, detail = rcl.send(rfq, lead,
                                  'Missing: required date',
                                  actor=SCM, commit=True)
        assert status == 'queued', detail
        assert detail['to'] == 'buyer@clsteel.com'

        from app.models.mailops import EmailOutbox
        row = (EmailOutbox.query
               .filter(EmailOutbox.event_key == 'rfq.clarification')
               .order_by(EmailOutbox.id.desc()).first())
        assert row is not None
        # leads@ is copied, so the reply lands back in the CRM.
        assert 'leads@' in (row.cc_addr or '')


def test_no_customer_address_is_its_own_answer():
    with flask_app.app_context():
        account, lead, rfq = _world(customer='')
        account.email = None
        db.session.commit()
        status, detail = rcl.send(rfq, lead, 'Missing: scope of work',
                                  actor=SCM, commit=True)
        assert status == 'no-recipient'
        assert 'by hand' in detail


def test_asking_twice_queues_once(monkeypatch):
    from app.services import mail_policy

    with flask_app.app_context():
        monkeypatch.setattr(mail_policy, 'external_allowed',
                            lambda: True)
        _a, lead, rfq = _world()
        for _ in range(2):
            rcl.send(rfq, lead, 'Missing: scope of work', actor=SCM,
                     commit=True)
        from app.models.mailops import EmailOutbox
        assert EmailOutbox.query.filter(
            EmailOutbox.event_key == 'rfq.clarification',
            EmailOutbox.entity_id == rfq.id).count() == 1
