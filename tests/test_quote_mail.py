"""
Which quotation a mail is, and what happens when we cannot tell.

CRM-QUOTE-REV-02 §5.4.

The failure this guards is not a missed match. It is a wrong one:
a quotation filed on the wrong lead changes that lead's stage,
its amount and its reported pipeline, and nobody looks at it
again until the figures are wrong. So an uncertain mail goes in a
queue, and the tests below spend most of their effort on the
cases that must NOT match.
"""
import importlib
import os
import sys
import tempfile
from datetime import date, datetime

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'qmail-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'QMail-1234567')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'qmail.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.quote')
importlib.import_module('app.models.rfq')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Lead, Company, Employee = _main.Lead, _main.Company, _main.Employee

from app.models.quote import Quote, UnmatchedQuoteMail      # noqa: E402
from app.models.rfq import RFQ                              # noqa: E402
from app.services import quote_mail as qm                   # noqa: E402

COORD = 'QM_COORD'
SALES = 'QM_SALES'


def _purge():
    UnmatchedQuoteMail.query.delete()
    Quote.query.filter(Quote.quote_number.like('QUO-2026-%')).delete(
        synchronize_session=False)
    RFQ.query.filter(RFQ.rfq_number.like('RFQ-2026-%')).delete(
        synchronize_session=False)
    Lead.query.filter(Lead.company.like('QM %')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like('QM %')).delete(
        synchronize_session=False)
    Employee.query.filter(Employee.emp_code.like('QM_%')).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code, role in ((COORD, 'admin'), (SALES, 'user')):
            db.session.add(Employee(
                emp_code=code, name=code, password_hash='x',
                role=role,
                email=f'{code.lower()}@procamlogistics.com'))
        db.session.commit()
        yield
        _purge()


def _world(quote_number='QUO-2026-0045', rev_no=0):
    account = Company(name='QM Steel', is_active=True,
                      email_domains=['qmsteel.com'])
    db.session.add(account)
    db.session.flush()
    lead = Lead(company='QM Steel', stage='Quoted', source='email',
                company_id=account.id, procam_vertical='Project Freight')
    db.session.add(lead)
    db.session.flush()
    rfq = RFQ(rfq_number='RFQ-2026-0007', subject='QM rfq',
              lead_id=lead.id, account_id=account.id)
    db.session.add(rfq)
    db.session.flush()
    quote = Quote(quote_number=quote_number, rfq_id=rfq.id,
                  lead_id=lead.id, account_id=account.id,
                  subject='QM quote', total_amount=99000,
                  rev_no=rev_no, root_quote_id=None)
    db.session.add(quote)
    db.session.flush()
    quote.root_quote_id = quote.id
    db.session.commit()
    return account, lead, rfq, quote


# ── reading references out of text ───────────────────────────────────
@pytest.mark.parametrize('text,expected', [
    ('Re: QUO-2026-0045 / Rev-01', ('QUO-2026-0045', 1, None)),
    ('our quote quo/2026/0045', ('QUO-2026-0045', None, None)),
    ('Against RFQ-2026-0007 please', (None, None, 'RFQ-2026-0007')),
    ('QUO 2026 0045 Rev 02', ('QUO-2026-0045', 2, None)),
    ('nothing useful here', (None, None, None)),
])
def test_references_are_read_however_they_are_written(text, expected):
    assert qm.refs_in(text) == expected


# ── matching, surest rule first ──────────────────────────────────────
def test_the_quotation_number_matches():
    with flask_app.app_context():
        _a, lead, _r, quote = _world()
        got, got_lead, how = qm.match(
            subject='Re: our offer QUO-2026-0045')
        assert got.id == quote.id
        assert got_lead.id == lead.id
        assert 'QUO-2026-0045' in how


def test_the_revision_picks_the_right_row():
    with flask_app.app_context():
        _a, _l, rfq, first = _world()
        second = Quote(quote_number='QUO-2026-0061',
                       rfq_id=rfq.id, lead_id=first.lead_id,
                       subject='rev', total_amount=88000,
                       parent_quote_id=first.id,
                       root_quote_id=first.id, rev_no=1)
        db.session.add(second)
        db.session.commit()
        got, _lead, how = qm.match(
            subject='Re: QUO-2026-0045 / Rev-01')
        assert got.id == second.id
        assert 'Rev-01' in how


def test_the_rfq_number_matches_when_there_is_no_quote_number():
    with flask_app.app_context():
        _a, lead, _r, _q = _world()
        got, got_lead, how = qm.match(
            subject='Pricing against RFQ-2026-0007')
        assert got_lead.id == lead.id
        assert 'RFQ-2026-0007' in how


# ── what must NOT match ──────────────────────────────────────────────
def test_a_number_we_never_issued_is_not_a_match():
    with flask_app.app_context():
        _world()
        got, lead, why = qm.match(subject='Your QUO-2026-9999')
        assert got is None and lead is None
        assert 'not a quotation this CRM issued' in why


def test_a_mail_with_no_reference_is_not_guessed_at():
    """Attaching on a guess changes a lead's stage and amount."""
    with flask_app.app_context():
        _world()
        got, lead, why = qm.match(
            subject='Rates please', body='Can you quote us?')
        assert got is None and lead is None
        assert 'no quotation or RFQ number' in why


# ── the queue ────────────────────────────────────────────────────────
def test_an_unplaceable_mail_is_parked_with_what_we_know():
    with flask_app.app_context():
        _a, lead, _r, _q = _world()
        outcome, why = qm.on_quote_mail(
            message_id='<m1@x>', subject='Rates please',
            from_addr='buyer@qmsteel.com', to_addr='leads@procam',
            body='no numbers at all')
        db.session.commit()
        assert outcome == 'parked'
        row = UnmatchedQuoteMail.query.filter_by(
            message_id='<m1@x>').first()
        assert row is not None
        # The person starts from what the CRM knows.
        assert row.suggested_lead_id == lead.id
        assert row.why


def test_parking_the_same_mail_twice_makes_one_row():
    with flask_app.app_context():
        _world()
        for _ in range(2):
            qm.on_quote_mail(message_id='<m2@x>', subject='Rates',
                             from_addr='buyer@qmsteel.com')
            db.session.commit()
        assert UnmatchedQuoteMail.query.filter_by(
            message_id='<m2@x>').count() == 1


def test_a_thread_match_without_a_number_is_still_parked():
    """It places the mail but not the quotation, and step 8 asks
    which quotation went out."""
    with flask_app.app_context():
        _a, lead, _r, _q = _world()
        from app import LeadEmail
        db.session.add(LeadEmail(lead_id=lead.id, direction='outbound',
                                 subject='earlier',
                                 conversation_id='CONV-1',
                                 source='ingested', status='sent'))
        db.session.commit()
        outcome, _why = qm.on_quote_mail(
            message_id='<m3@x>', subject='Re: earlier',
            from_addr='buyer@qmsteel.com',
            headers={'conversation_id': 'CONV-1'})
        db.session.commit()
        assert outcome == 'parked'
        row = UnmatchedQuoteMail.query.filter_by(
            message_id='<m3@x>').first()
        assert 'which revision' in (row.why or '')


# ── a matched mail closes step 8 ─────────────────────────────────────
def _chain_on(rfq, state=True):
    import json
    from app.models.integration import AppSetting
    from app.services import flags
    for key in ('flag.FEATURE_RFQ_CHAIN',):
        row = AppSetting.query.filter_by(key=key).first()
        if row is None:
            row = AppSetting(key=key)
            db.session.add(row)
        row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


def test_a_matched_quote_mail_closes_step_8():
    from app.services import rfq_chain as rc

    with flask_app.app_context():
        _a, _l, rfq, quote = _world()
        rfq.priority = 'Medium'
        db.session.commit()
        _chain_on(rfq, True)
        try:
            task = rc.open_step(rfq, 8, owner=SALES, actor='test')
            from datetime import timedelta
            task.started_at = datetime.utcnow() - timedelta(hours=20)
            db.session.commit()

            outcome, detail = qm.on_quote_mail(
                message_id='<m4@x>',
                subject='Our offer QUO-2026-0045',
                from_addr='sales@procamgroup.in',
                to_addr='buyer@qmsteel.com', actor=SALES)
            db.session.commit()
            assert outcome == 'closed', detail

            from app.models.task_engine import TaskInstanceStatus
            closed = rc.steps_for(rfq)[8]
            assert closed.status == TaskInstanceStatus.COMPLETED
            ev = closed.closed_evidence or {}
            assert ev['mail_reference'] == '<m4@x>'
            assert ev['recipient'] == 'buyer@qmsteel.com'
            assert 'QUO-2026-0045' in ev['matched_by']
        finally:
            _chain_on(rfq, False)


def test_a_matched_mail_with_no_open_step_is_not_an_error():
    with flask_app.app_context():
        _a, _l, rfq, _q = _world()
        _chain_on(rfq, True)
        try:
            outcome, _d = qm.on_quote_mail(
                message_id='<m5@x>',
                subject='Our offer QUO-2026-0045',
                from_addr='sales@procamgroup.in', actor=SALES)
            db.session.commit()
            assert outcome == 'matched'
            assert UnmatchedQuoteMail.query.filter_by(
                message_id='<m5@x>').first() is None
        finally:
            _chain_on(rfq, False)


# ── clearing the queue ───────────────────────────────────────────────
def _client(emp):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['emp_code'] = emp
    return c


def test_a_salesperson_cannot_clear_the_queue():
    with flask_app.app_context():
        _a, _l, rfq, _q = _world()
        _chain_on(rfq, True)
        qm.park(message_id='<m6@x>', subject='x',
                from_addr='buyer@qmsteel.com', why='no number')
        db.session.commit()
        row_id = UnmatchedQuoteMail.query.filter_by(
            message_id='<m6@x>').first().id
    try:
        r = _client(SALES).post(f'/api/chain/unmatched/{row_id}',
                                json={'action': 'not_a_quote'})
        assert r.status_code == 403
    finally:
        with flask_app.app_context():
            _chain_on(None, False)


def test_the_coordinator_can_link_it_to_a_lead():
    with flask_app.app_context():
        _a, lead, rfq, _q = _world()
        _chain_on(rfq, True)
        qm.park(message_id='<m7@x>', subject='x',
                from_addr='buyer@qmsteel.com', why='no number')
        db.session.commit()
        row_id = UnmatchedQuoteMail.query.filter_by(
            message_id='<m7@x>').first().id
        lead_id = lead.id
    try:
        r = _client(COORD).post(f'/api/chain/unmatched/{row_id}',
                                json={'action': 'link',
                                      'lead_id': lead_id})
        assert r.status_code == 200, r.get_data(as_text=True)
        with flask_app.app_context():
            row = db.session.get(UnmatchedQuoteMail, row_id)
            assert row.status == UnmatchedQuoteMail.LINKED
            assert row.linked_lead_id == lead_id
    finally:
        with flask_app.app_context():
            _chain_on(None, False)
