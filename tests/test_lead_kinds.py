"""
An auction is not an enquiry you can leave until Thursday.

226 SuperProcure load postings were deleted in a bulk review of
unassigned leads, because nothing on the record said they were
time-critical and a load posting looks like a newsletter to
somebody scanning a spreadsheet. And every one had been forwarded
in by a colleague the CRM could not name, so when they stopped
arriving there was nobody to ask.

Both halves are read off the same message at ingest. These tests
are mostly about what must NOT be marked — a rule that catches an
ordinary quotation makes the mark meaningless.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'kinds-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'KindsTest-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'kinds.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.master_data')
flask_app = _main.app
db = _main.db

from app.services import lead_kinds as lk                 # noqa: E402

with flask_app.app_context():
    db.create_all()


# ── what it marks ────────────────────────────────────────────────────
@pytest.mark.parametrize('subject,sender,platform', [
    ('FW: Isgec Heavy Engineering Ltd. : Load Post Notification',
     'noreply@alerts.superprocure.com', 'superprocure'),
    ('FW: Isgec Heavy Engineering Ltd. : Auction Start Notification',
     'noreply@alerts2.superprocure.com', 'superprocure'),
    ('Fw: Isgec : Live Load Post Notification',
     'noreply@alerts.superprocure.com', 'superprocure'),
    ('FW: #24868778 Load has been edited',
     'noreply@alerts.superprocure.com', 'superprocure'),
    ('Jindal Stainless has invited you to participate in an event: '
     'RFQ_Transporter Required',
     'noreply@us.bn.cloud.ariba.com', 'ariba'),
])
def test_a_real_posting_is_marked(subject, sender, platform):
    kind, got, why = lk.classify(subject=subject, sender=sender,
                                 rules=lk.SEEDS)
    assert kind == 'auction'
    assert got == platform
    assert why


@pytest.mark.parametrize('subject', [
    'FW: 🚀 RA-1724 | Auction is now live',
    'FW: RA-1723 | Auction has been withdrawn',
    'RA 1738 closing today',
    'Japanese Reverse Auction || 22/09/2026 @ 4:45 PM',
])
def test_an_auction_on_any_platform_is_marked(subject):
    kind, _p, _w = lk.classify(subject=subject,
                               sender='someone@example.com',
                               rules=lk.SEEDS)
    assert kind == 'auction', subject


# ── what it must not mark ────────────────────────────────────────────
@pytest.mark.parametrize('subject,sender,why', [
    ('Quotation request for Chennai to Hamburg',
     'buyer@acme.com', 'an ordinary enquiry'),
    ('RE: Freight Charges - Kanchipuram to Blore',
     'hk.dhingra@isgec.com', 'the same customer, writing directly'),
    ('Rate revision for October', 'ops@carrier.com', 'a vendor'),
    ('Invoice RA-2 attached', 'accounts@client.com',
     'a number that is not a reverse-auction reference'),
    ('', '', 'nothing at all'),
])
def test_an_ordinary_enquiry_is_left_alone(subject, sender, why):
    kind, _p, _w = lk.classify(subject=subject, sender=sender,
                               rules=lk.SEEDS)
    assert kind is None, f'{why}: {subject!r} was marked'


def test_the_body_is_not_read():
    """A quotation that mentions a reverse auction in a paragraph
    is not an auction notification."""
    kind, _p, _w = lk.classify(
        subject='Our quotation for your enquiry',
        sender='sales@vendor.com',
        body='We also participate in reverse auction events.',
        rules=lk.SEEDS)
    assert kind is None


# ── the list is the admin's ──────────────────────────────────────────
def test_an_admin_list_replaces_the_seeds():
    from app.models.master_data import MasterItem

    with flask_app.app_context():
        MasterItem.query.filter_by(list_key=lk.LIST_KEY).delete()
        db.session.add(MasterItem(
            list_key=lk.LIST_KEY, code='widget tender',
            label='widget tender', description='theirs',
            is_active=True, meta={'platform': 'widgetco'}))
        db.session.commit()
        try:
            rules = lk.patterns()
            assert rules == [('widget tender', 'widgetco', 'theirs')]
            assert lk.classify(subject='FW: widget tender open',
                               rules=rules)[1] == 'widgetco'
            assert lk.classify(
                subject='FW: Load Post Notification',
                rules=rules)[0] is None, 'a seed came back'
        finally:
            MasterItem.query.filter_by(list_key=lk.LIST_KEY).delete()
            db.session.commit()


def test_no_owner_is_named_in_code():
    """The owner is an admin setting. A name in a source file is a
    name nobody can change when that person moves team."""
    src = open(os.path.join(_ROOT, 'app', 'services',
                            'lead_kinds.py')).read()
    import re
    assert not re.search(r'\b(EMP|DIR|CON)\d{4,}\b', src)
    assert lk.OWNER_SETTING == 'auction_lead_owner'
    with flask_app.app_context():
        assert lk.owner() == '', 'nobody is nominated by default'


# ── the forwarder ────────────────────────────────────────────────────
def _msg(subject, sender, name=''):
    return {'subject': subject,
            'from': {'emailAddress': {'address': sender,
                                      'name': name}}}


def test_a_forward_records_who_sent_it_on():
    email, name, subject = lk.forwarder(
        _msg('FW: Isgec : Load Post Notification',
             'suranjan@procamgroup.in', 'Suranjan Das'),
        'From: noreply@alerts.superprocure.com\nLoad posted by...')
    assert email == 'suranjan@procamgroup.in'
    assert name == 'Suranjan Das'
    assert subject.startswith('FW:')


def test_an_ordinary_email_records_no_forwarder():
    """Otherwise every enquiry would show the customer as having
    forwarded their own message."""
    email, name, subject = lk.forwarder(
        _msg('Quotation request', 'buyer@acme.com', 'A Buyer'),
        'Dear sir, please quote for one container.')
    assert (email, name, subject) == ('', '', '')


def test_the_subject_alone_is_enough_to_know_it_was_forwarded():
    """Some forwards arrive with the original headers stripped by
    the client that sent them."""
    email, _n, _s = lk.forwarder(
        _msg('Fw: something', 'colleague@procamgroup.in'),
        'no header block here at all')
    assert email == 'colleague@procamgroup.in'


# ── it cannot be swept ───────────────────────────────────────────────
def test_the_junk_sweep_skips_an_auction():
    src = open(os.path.join(
        _ROOT, 'scripts',
        '2026_09_02_purge_irrelevant_leads.py')).read()
    assert 'lead_kinds.is_auction' in src


def test_the_bulk_close_refuses_an_auction():
    src = open(os.path.join(_ROOT, 'app', 'bulk_admin',
                            'service.py')).read()
    assert 'lead_kinds.is_auction' in src
    assert 'close in hours' in src


def test_closing_one_asks_whether_we_bid():
    src = open(os.path.join(_ROOT, 'app.py')).read()
    body = src.split('def api_close_lost')[1][:1600]
    assert 'bid_outcome' in body
    assert 'lead_kinds.OUTCOMES' in body
    assert lk.OUTCOMES == ('bid', 'not_bid')
