"""
Our own mail going out belongs to the customer it was sent to.

On 1-3 September, 167 messages a Procam employee had sent to a
customer became 167 new leads under the placeholder account
"Unknown", all assigned to one person. They are live RFQ threads
with Alstom, GE Vernova, MSC and others.

Every matching rule read the sender, and on outbound mail the
sender is us — so the customer sat in the To: line, unread.

Two things these tests guard, and the second is the one that
would do damage if it broke: that the recipient is read at all,
and that our own domain is never mistaken for the customer.
Account #983 "Unknown" listed procamgroup.in, so an exact rule
matched every one of these messages to the placeholder; if that
guard goes, the bug comes back wearing a fix.
"""
import importlib
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'outbound-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Outbound-123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'outbound.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.assignment')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Lead, Company = _main.Lead, _main.Company

from app.services import outbound_mail as om              # noqa: E402
from app.services.mail_signals import OUR_DOMAINS         # noqa: E402

OURS = sorted(OUR_DOMAINS)[0]
CUSTOMER = 'buyer@alstomgroup.com'
_MADE = set()


def _purge():
    Lead.query.filter(Lead.company.like('OB%')).delete(
        synchronize_session=False)
    Company.query.filter(db.or_(Company.name.like('OB%'),
                                Company.id.in_(_MADE or {-1}))).delete(
        synchronize_session=False)
    _MADE.clear()
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        yield
        _purge()


def _account(name='OB Alstom', domain='alstomgroup.com'):
    account = Company(name=name, is_active=True,
                      email_domains=[domain] if domain else [])
    db.session.add(account)
    db.session.flush()
    _MADE.add(account.id)
    db.session.commit()
    return account


def _lead(account, *, stage='New', subject='', days=1):
    lead = Lead(company='OB Co', stage=stage, source='email',
                company_id=account.id if account else None,
                original_email_subject=subject,
                procam_vertical='Project Freight',
                created_at=datetime.utcnow() - timedelta(days=days),
                updated_at=datetime.utcnow() - timedelta(days=days))
    db.session.add(lead)
    db.session.commit()
    return lead


def _msg(sender, to, subject='RFQ for Mumbai'):
    return {'from': {'emailAddress': {'address': sender}},
            'toRecipients': [{'emailAddress': {'address': a}}
                             for a in to],
            'subject': subject}


HEADER = (f'From: TV Vijay <tv.vijay@{OURS}>\n'
          f'Sent: Thursday, 3 September 2026 12:51:22\n'
          f'To: KUMAR R Vinodh <{CUSTOMER}>; . Prince '
          f'<prince@alstomgroup.com>\n'
          f'Cc: colleague <someone@{OURS}>\n'
          f'Subject: Re: RFQ for Mumbai\n\nDear Vinodh, …')


# ── reading the direction ────────────────────────────────────────────
def test_the_envelope_says_outbound():
    with flask_app.app_context():
        got, sender, ext = om.direction(
            _msg(f'me@{OURS}', [CUSTOMER]))
        assert got == 'outbound'
        assert ext == [CUSTOMER]


def test_the_forwarded_header_is_read_when_the_envelope_is_empty():
    """The 167 carry nothing in original_email_from; their sender
    exists only inside the body."""
    with flask_app.app_context():
        got, sender, ext = om.direction({}, body=HEADER)
        assert got == 'outbound'
        assert sender == f'tv.vijay@{OURS}'
        assert CUSTOMER in ext


def test_internal_to_internal_is_not_outbound():
    """Two colleagues writing to each other is not a customer."""
    with flask_app.app_context():
        got, _s, ext = om.direction(
            _msg(f'me@{OURS}', [f'colleague@{OURS}']))
        assert got == ''
        assert ext == []


def test_inbound_customer_mail_is_untouched():
    with flask_app.app_context():
        got, _s, _e = om.direction(_msg(CUSTOMER, [f'leads@{OURS}']))
        assert got == ''


def test_our_colleagues_on_the_to_line_are_not_the_customer():
    with flask_app.app_context():
        _got, _s, ext = om.direction(
            _msg(f'me@{OURS}', [f'mate@{OURS}', CUSTOMER]))
        assert ext == [CUSTOMER]


# ── naming the customer ──────────────────────────────────────────────
def test_the_recipient_names_the_customer():
    with flask_app.app_context():
        account = _account()
        found, rule, addr = om.customer_of([CUSTOMER])
        assert found is not None and found.id == account.id
        assert addr == CUSTOMER


def test_our_own_domain_is_never_the_customer():
    """#983 "Unknown" listed procamgroup.in and matched every one
    of these messages on an exact rule."""
    with flask_app.app_context():
        _account(name='Unknown', domain=OURS)
        found, _rule, _addr = om.customer_of([f'someone@{OURS}'])
        assert found is None


def test_a_placeholder_account_is_never_the_customer():
    with flask_app.app_context():
        _account(name='New Lead', domain='alstomgroup.com')
        found, _rule, _addr = om.customer_of([CUSTOMER])
        assert found is None


# ── choosing the lead ────────────────────────────────────────────────
def test_it_attaches_to_the_customers_open_lead():
    with flask_app.app_context():
        account = _account()
        lead = _lead(account)
        assert om.open_lead_for(account).id == lead.id


def test_a_matching_subject_wins_over_recency():
    with flask_app.app_context():
        account = _account()
        _newer = _lead(account, subject='Something else', days=0)
        wanted = _lead(account, subject='RFQ for Mumbai', days=5)
        found = om.open_lead_for(account, subject='Re: RFQ for Mumbai')
        assert found.id == wanted.id


def test_a_closed_lead_is_not_a_target():
    """Otherwise outbound mail silently reopens finished business."""
    with flask_app.app_context():
        account = _account()
        _lead(account, stage='Lost')
        assert om.open_lead_for(account) is None


def test_no_open_lead_means_a_new_one_is_right():
    with flask_app.app_context():
        assert om.open_lead_for(_account()) is None


@pytest.mark.parametrize('subject,expected', [
    ('Re: Fw: [EXTERNAL] RFQ for Mumbai', 'rfq for mumbai'),
    ('RE: RE: AW: Budgetary quote', 'budgetary quote'),
    ('Re: Fw:', ''),                      # nothing left to match on
    ('Hi', ''),                           # too generic
])
def test_subject_stripping(subject, expected):
    assert om.strip_subject(subject) == expected
