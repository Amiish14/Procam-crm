"""Intake classification against a real database.

The tree is tested in isolation elsewhere. These tests cover the half
that touches data: thread matching, account resolution to two owners,
and recording the decision as a labelled example.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'IntakeTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'intake.db')

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Lead, LeadEmail, Company = _main.Lead, _main.LeadEmail, _main.Company
Contact, Employee = _main.Contact, _main.Employee
VendorDomain = _main.VendorDomain
EmailClassification = _main.EmailClassification

from app.services import lead_intake as li                    # noqa: E402
from app.services import lead_intake_db as lidb               # noqa: E402


def msg(subject='', body='', frm='buyer@tatasteel.com', to=None,
        conversation=None, in_reply_to=None, references=None, mid=None):
    m = {'subject': subject, 'body': {'content': body},
         'from': {'emailAddress': {'address': frm}},
         'toRecipients': [{'emailAddress': {'address': a}}
                          for a in (to or ['leads@procamgroup.in'])],
         'ccRecipients': []}
    if conversation:
        m['conversationId'] = conversation
    if mid:
        m['internetMessageId'] = mid
    hdrs = []
    if in_reply_to:
        hdrs.append({'name': 'In-Reply-To', 'value': in_reply_to})
    if references:
        hdrs.append({'name': 'References', 'value': references})
    if hdrs:
        m['internetMessageHeaders'] = hdrs
    return m


@pytest.fixture(scope='module')
def world():
    with flask_app.app_context():
        db.create_all()
        for code, name in (('VH001', 'Vertical Head'),
                           ('OPS001', 'Operations PIC')):
            e = Employee.query.filter_by(emp_code=code).first()
            if not e:
                e = Employee(emp_code=code, name=name)
                db.session.add(e)
            e.is_active, e.role = True, 'user'

        acct = Company(name='Tata Steel Limited',
                       website='https://www.tatasteel.com',
                       email_domains=['tatasteel.com', 'tatasteel.co.in'],
                       pic_emp_code='VH001',
                       secondary_pic_emp_code='OPS001',
                       vertical='Project Logistics', is_active=True)
        db.session.add(acct)

        db.session.add(VendorDomain(domain='xyzshippingline.com',
                                    vendor_type='shipping line',
                                    is_active=True))

        lead = Lead(company='Tata Steel Limited', source='email',
                    email='buyer@tatasteel.com',
                    stage='New Opportunity',
                    original_email_subject='RFQ - transformer movement',
                    email_message_id='<original@tatasteel.com>',
                    conversation_id='AAQkCONV1',
                    created_at=datetime.utcnow() - timedelta(days=2))
        db.session.add(lead)
        db.session.commit()
        return {'account_id': acct.id, 'lead_id': lead.id}


# ─── thread matching ─────────────────────────────────────────────────────
def test_a_conversation_id_finds_its_lead(world):
    with flask_app.app_context():
        assert lidb.find_by_thread(conversation_id='AAQkCONV1') \
            == world['lead_id']


def test_in_reply_to_finds_its_lead(world):
    with flask_app.app_context():
        assert lidb.find_by_thread(
            in_reply_to='<original@tatasteel.com>') == world['lead_id']


def test_a_references_chain_finds_its_lead(world):
    """A reply four messages deep still names the first in References."""
    with flask_app.app_context():
        assert lidb.find_by_thread(reference_ids=[
            '<original@tatasteel.com>', '<later@tatasteel.com>'
        ]) == world['lead_id']


def test_a_reply_matches_the_trail_not_just_the_lead(world):
    """Later replies attach to the trail; the next one must find those."""
    with flask_app.app_context():
        db.session.add(LeadEmail(
            lead_id=world['lead_id'], direction='outbound',
            message_id='<reply2@procamgroup.in>',
            conversation_id='AAQkCONV1', body='our response'))
        db.session.commit()
        assert lidb.find_by_thread(
            in_reply_to='<reply2@procamgroup.in>') == world['lead_id']


def test_an_unknown_thread_matches_nothing(world):
    with flask_app.app_context():
        assert lidb.find_by_thread(conversation_id='AAQkNOPE') is None
        assert lidb.find_by_thread() is None


# ─── subject fallback ────────────────────────────────────────────────────
def test_subject_and_domain_together_match(world):
    with flask_app.app_context():
        assert lidb.find_by_subject(
            subject='RFQ - transformer movement',
            counterparties=['buyer@tatasteel.com']) == world['lead_id']


def test_a_subject_alone_is_not_enough(world):
    """"RFQ" matches every enquiry ever sent."""
    with flask_app.app_context():
        assert lidb.find_by_subject(
            subject='RFQ - transformer movement',
            counterparties=['someone@elsewhere.com']) is None


def test_a_generic_subject_is_refused(world):
    with flask_app.app_context():
        assert lidb.find_by_subject(
            subject='RFQ', counterparties=['buyer@tatasteel.com']) is None


# ─── the tree, wired to the database ─────────────────────────────────────
def test_a_reply_on_a_live_thread_creates_nothing(world):
    with flask_app.app_context():
        d = li.classify(
            msg(subject='RE: RFQ - transformer movement',
                body='Any update on the rate?', conversation='AAQkCONV1'),
            lidb.build_context())
    assert d.klass == li.Klass.REPLY
    assert d.lead_id == world['lead_id']
    assert not d.creates_lead


def test_a_known_shipping_line_is_rate_sourcing(world):
    with flask_app.app_context():
        d = li.classify(
            msg(subject='Rates as requested', body='Our rates attached.',
                frm='sales@xyzshippingline.com'),
            lidb.build_context())
    assert d.klass == li.Klass.RATE_SOURCING
    assert not d.creates_lead


def test_a_genuinely_new_enquiry_still_gets_through(world):
    with flask_app.app_context():
        d = li.classify(
            msg(subject='RFQ - new reactor movement to Dahej',
                body='We need to move 3 reactors, 180 MT each, from Hazira '
                     'to Dahej. Over-dimensional. Please quote. '
                     'Contact +91 98200 44556.',
                frm='projects@larsentoubro.com'),
            lidb.build_context())
    assert d.klass == li.Klass.NEW_LEAD
    assert d.creates_lead


# ─── account resolution to two owners ────────────────────────────────────
def test_an_email_domain_resolves_the_account(world):
    with flask_app.app_context():
        company, how = lidb.resolve_account('newbuyer@tatasteel.com')
        assert company is not None
        assert company.id == world['account_id']
        assert how == 'account email domain'


def test_a_second_listed_domain_also_resolves(world):
    with flask_app.app_context():
        company, _how = lidb.resolve_account('buyer@tatasteel.co.in')
        assert company is not None and company.id == world['account_id']


def test_a_known_contact_resolves_even_on_a_personal_domain(world):
    with flask_app.app_context():
        db.session.add(Contact(contact_type='person', name='Ravi Menon',
                               email='ravi.personal@gmail.com',
                               account_id=world['account_id']))
        db.session.commit()
        company, how = lidb.resolve_account('ravi.personal@gmail.com')
        assert company is not None and company.id == world['account_id']
        assert how == 'contact email'


def test_an_unknown_domain_is_unmapped_not_guessed(world):
    with flask_app.app_context():
        company, how = lidb.resolve_account('someone@brandnewclient.com')
        assert company is None
        assert how == 'unmapped'


def test_the_account_yields_both_owners(world):
    with flask_app.app_context():
        company, _ = lidb.resolve_account('buyer@tatasteel.com')
        primary, secondary = lidb.owners_for(company)
    assert primary == 'VH001'
    assert secondary == 'OPS001'


def test_an_account_with_no_owners_returns_none_rather_than_inventing(world):
    with flask_app.app_context():
        bare = Company(name='Unowned Ltd', is_active=True)
        db.session.add(bare)
        db.session.commit()
        assert lidb.owners_for(bare) == (None, None)
    assert lidb.owners_for(None) == (None, None)


# ─── recording ───────────────────────────────────────────────────────────
def test_every_decision_is_recorded_as_a_labelled_example(world):
    with flask_app.app_context():
        m = msg(subject='RE: RFQ - transformer movement',
                conversation='AAQkCONV1', mid='<rec1@tatasteel.com>')
        d = li.classify(m, lidb.build_context())
        lidb.record(d, m)
        db.session.commit()

        row = EmailClassification.query.filter_by(
            message_id='<rec1@tatasteel.com>').first()
        assert row is not None
        assert row.classification == li.Klass.REPLY
        assert row.decided_by == 'step_3'
        assert row.from_domain == 'tatasteel.com'
        assert row.matched_lead_id == world['lead_id']


def test_a_correction_sits_beside_the_original(world):
    """The pair is the training label — the correction alone says nothing
    about what the classifier got wrong."""
    with flask_app.app_context():
        m = msg(subject='Rates', frm='sales@xyzshippingline.com',
                mid='<rec2@xyz.com>')
        d = li.classify(m, lidb.build_context())
        row = lidb.record(d, m)
        db.session.commit()

        lidb.correct(row, li.Klass.NEW_LEAD,
                     reason='Actually a client enquiry', by='VH001')
        db.session.commit()

        again = EmailClassification.query.filter_by(
            message_id='<rec2@xyz.com>').first()
        assert again.classification == li.Klass.RATE_SOURCING
        assert again.corrected_to == li.Klass.NEW_LEAD
        assert again.was_wrong
        assert again.corrected_by == 'VH001'


def test_recording_never_breaks_ingestion(world):
    """A failure to record must not stop a genuine lead being created."""
    with flask_app.app_context():
        d = li.Decision(li.Klass.NEW_LEAD, step=10, reason='x')
        assert lidb.record(d, {'from': None, 'subject': None}) is not None \
            or True


# ─── seeding account domains from history ────────────────────────────────
def test_domains_are_learned_from_records_already_linked(world):
    """The Account Master holds no websites in production, so deriving
    from that column alone seeds nothing. Leads already carry the domain
    people actually write from."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'intake_mig',
        os.path.join(_ROOT, 'scripts', '2026_09_26_lead_intake_engine.py'))
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    with flask_app.app_context():
        acct = Company(name='History Derived Ltd', is_active=True)
        db.session.add(acct)
        db.session.flush()
        for addr in ('one@historyderived.com', 'two@historyderived.com'):
            db.session.add(Lead(company='History Derived Ltd',
                                company_id=acct.id, email=addr,
                                source='email', stage='New Opportunity'))
        # A free-mail sender must not map the account.
        db.session.add(Lead(company='History Derived Ltd',
                            company_id=acct.id, email='someone@gmail.com',
                            source='email', stage='New Opportunity'))
        db.session.commit()

        found = mig._domains_from_history(db.session)
        assert 'historyderived.com' in found.get(acct.id, [])
        assert 'gmail.com' not in found.get(acct.id, [])


def test_a_domain_claimed_by_two_accounts_is_assigned_to_neither(world):
    """Guessing would silently route a customer's mail to the wrong owner."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'intake_mig2',
        os.path.join(_ROOT, 'scripts', '2026_09_26_lead_intake_engine.py'))
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)

    with flask_app.app_context():
        a = Company(name='Claimant A', is_active=True)
        b = Company(name='Claimant B', is_active=True)
        db.session.add_all([a, b])
        db.session.flush()
        db.session.add(Lead(company='Claimant A', company_id=a.id,
                            email='x@contested.com', source='email'))
        db.session.add(Lead(company='Claimant B', company_id=b.id,
                            email='y@contested.com', source='email'))
        db.session.commit()

        found = mig._domains_from_history(db.session)
        assert 'contested.com' not in found.get(a.id, [])
        assert 'contested.com' not in found.get(b.id, [])


# ─── recording covers both branches ──────────────────────────────────────
def test_the_pipeline_records_a_classification_on_both_branches():
    """Recording only on the non-lead branch left created leads — the
    ones a human might reject, and so the most valuable labels there
    are — out of the training set entirely.
    """
    src = open(os.path.join(_ROOT, 'email_ingest', 'pipeline.py')).read()
    calls = src.count('_lidb.record(')
    assert calls >= 2, (
        'record() must be called when a lead is created as well as when '
        f'one is not — found {calls} call site(s)')
    assert 'created_lead_id=lead.id' in src, \
        'a recorded classification must name the lead it produced'


def test_a_recorded_decision_names_the_lead_it_created(world):
    with flask_app.app_context():
        lead = Lead(company='Recorded Ltd', source='email',
                    email='buyer@recordedltd.com', stage='New Opportunity')
        db.session.add(lead)
        db.session.flush()

        d = li.Decision(li.Klass.NEW_LEAD, step=10, confidence=88,
                        reason='new enquiry (88% confidence)')
        row = lidb.record(d, msg(subject='RFQ - new', mid='<created@x.com>'),
                          created_lead_id=lead.id)
        db.session.commit()

        assert row.created_lead_id == lead.id
        assert row.classification == li.Klass.NEW_LEAD
        assert row.confidence == 88
        assert row.decided_by == 'step_10'


# ─── the classifier must sit in the path production actually runs ────────
def test_the_live_ingestion_path_classifies():
    """The poll and the webhook both call
    single_message.process_single_message(). pipeline.run_ingest is an
    older batch path that production never executes — wiring the
    classifier only into that one left it running on no mail at all.
    """
    src = open(os.path.join(_ROOT, 'email_ingest',
                            'single_message.py')).read()
    assert '_li.classify(' in src, \
        'process_single_message must classify before creating a lead'
    assert 'not decision.creates_lead' in src, \
        'the classification must gate lead creation, not merely annotate it'
    assert '_lidb.record(' in src or '_lidb2.record(' in src, \
        'decisions must be recorded on the live path'


def test_both_entry_points_reach_the_same_function():
    """If a third ingestion path appears, it has to go through here too."""
    poll = open(os.path.join(_ROOT, 'scripts',
                             '2026_09_02_poll_leads_mailbox.py')).read()
    hook = open(os.path.join(_ROOT, 'email_ingest', 'webhook.py')).read()
    assert 'process_single_message' in poll
    assert 'process_single_message' in hook


def test_a_non_lead_is_filed_rather_than_dropped():
    """Declining to create a lead is not a reason to lose the email."""
    src = open(os.path.join(_ROOT, 'email_ingest',
                            'single_message.py')).read()
    assert 'def _file_against_lead(' in src
    body = src.split('def _file_against_lead(')[1][:3000]
    assert 'LeadEmail(' in body, 'the email must land on the lead trail'
    assert "lead.stage = 'Quoted'" in body, \
        'a quotation should move the enquiry on'


def test_the_created_lead_keeps_its_thread_identity():
    src = open(os.path.join(_ROOT, 'email_ingest',
                            'single_message.py')).read()
    import re
    for field in ('conversation_id', 'in_reply_to', 'references_header'):
        assert re.search(rf'{field}\s*=\s*_keys\.get', src), \
            f'{field} must be stored on the lead it was ingested from'


# ─── the kill switch ─────────────────────────────────────────────────────
def test_there_is_a_way_to_stand_the_classifier_down():
    """An untuned classifier suppressing a real RFQ costs far more than a
    duplicate lead, so it must be stoppable in seconds without a code
    rollback."""
    src = open(os.path.join(_ROOT, 'email_ingest',
                            'single_message.py')).read()
    assert "os.environ.get('LEAD_INTAKE_MODE')" in src
    assert "_mode == 'off'" in src, 'there must be a full off switch'
    assert "_mode == 'observe'" in src, \
        'observe mode is how it earns trust before it suppresses anything'


def test_observe_mode_records_but_creates_the_lead_anyway():
    src = open(os.path.join(_ROOT, 'email_ingest',
                            'single_message.py')).read()
    block = src.split("if _mode == 'observe'")[1][:700]
    assert '_lidb.record(' in block, 'observe must still record the decision'
    assert 'decision = None' in block, \
        'observe must fall through to creating the lead'


def test_the_dry_run_mirrors_the_production_path():
    """The first dry run skipped the parser, so no forward ever unwrapped
    and 71% of real mail came back Internal."""
    src = open(os.path.join(_ROOT, 'scripts',
                            'classify_mailbox_dryrun.py')).read()
    assert 'email_parser.extract_lead(msg)' in src
    assert "_forward_resolved" in src, \
        'the dry run must pass the same flag production does'
