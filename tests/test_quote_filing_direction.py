"""
A quotation Procam sends moves the lead to Quoted; one Procam receives
does not.

The classifier gives the quote class both to our own quotation going out
(rule 4, the sender is one of ours) and to an agent quoting us (rule 7).
Filing treated both as outbound: an agent's quote moved the enquiry to
Quoted and put the agent's buying price in quoted_amount_inr.
"""
import logging
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.modules.setdefault('msal', __import__('types').ModuleType('msal'))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'QuoteDirTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'quotedir.db'))

from app import app as flask_app, db, Lead, LeadEmail          # noqa: E402
from app.services.lead_intake import Decision, Klass           # noqa: E402
from email_ingest import single_message as sm                  # noqa: E402

LOG = logging.getLogger('test')


def _msg(frm, mid, amount='INR 4,50,000'):
    return {'internetMessageId': mid,
            'subject': 'Quotation - transformer movement',
            'body': {'content': f'Please find our quotation. Total {amount}.'},
            'from': {'emailAddress': {'address': frm}},
            'toRecipients': [{'emailAddress': {'address': 'x@y.com'}}],
            'receivedDateTime': '2026-09-01T10:00:00Z'}


@pytest.fixture()
def lead():
    with flask_app.app_context():
        db.create_all()
        l = Lead(company='Quote Direction Co', source='email', stage='New',
                 quoted_amount_inr=None)
        db.session.add(l)
        db.session.commit()
        yield l.id
        LeadEmail.query.filter_by(lead_id=l.id).delete()
        Lead.query.filter_by(id=l.id).delete()
        db.session.commit()


def test_an_agents_quote_is_inbound_and_moves_nothing(lead):
    with flask_app.app_context():
        d = Decision(Klass.QUOTE, step=7, lead_id=lead,
                     reason='quotation against a known enquiry')
        row = sm._file_against_lead(db, d, _msg('rates@agent.example',
                                                '<agent-q@x>'), {}, LOG)
        db.session.commit()
        assert row.direction == 'inbound'
        l = db.session.get(Lead, lead)
        assert l.stage == 'New'
        assert l.quoted_amount_inr is None


def test_a_suppliers_reply_is_inbound(lead):
    with flask_app.app_context():
        d = Decision(Klass.RATE_SOURCING, step=6, lead_id=lead,
                     reason='known supplier')
        row = sm._file_against_lead(db, d, _msg('ops@carrier.example',
                                                '<vendor@x>'), {}, LOG)
        db.session.commit()
        assert row.direction == 'inbound'


def test_our_own_quotation_still_moves_the_lead_to_quoted(lead):
    with flask_app.app_context():
        d = Decision(Klass.QUOTE, step=4, lead_id=lead,
                     reason='quotation sent out')
        row = sm._file_against_lead(db, d, _msg('sales@procamgroup.in',
                                                '<ours-q@x>'), {}, LOG)
        db.session.commit()
        assert row.direction == 'outbound'
        assert db.session.get(Lead, lead).stage == 'Quoted'
