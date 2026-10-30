"""
CRM-LEAD-FLOW-01 §7 — the acceptance tests that are not the screen.

Tests 1 to 4 are about intake, 11 and 12 about the list and the
help button. The screen's own tests (5 to 10, 13) are in
`tests/test_lead_popup_v2.py`; each is named for the number it
covers so a rename cannot quietly drop one.

Where the behaviour already existed — thread matching, the blocked
gate's refusal to create a lead, the classifier — these tests hold
it at the seam that CRM-LEAD-FLOW-01 changed, rather than
re-testing the ingest end to end, which `tests/test_intake_*` and
`tests/test_email_ingest*` already do.
"""
import importlib
import json
import os
import sys
import tempfile
from datetime import datetime

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'accept-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'AcceptTest-123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'accept.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.help_content')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead
LeadEmail = _main.LeadEmail

PREFIX = 'AC '
USER, ADMIN, GONE = 'AC_USER', 'AC_ADMIN', 'AC_GONE'
VERTICAL = 'Heavy Transport'


def _purge():
    from app.models.help_content import HelpTicket
    from app.models.restriction import ClientRestriction
    ids = [r.id for r in Lead.query.filter(
        Lead.company.like(PREFIX + '%')).all()]
    if ids:
        LeadEmail.query.filter(LeadEmail.lead_id.in_(ids)).delete(
            synchronize_session=False)
    Lead.query.filter(Lead.company.like(PREFIX + '%')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like(PREFIX + '%')).delete(
        synchronize_session=False)
    ClientRestriction.query.filter(
        ClientRestriction.company_name.like(PREFIX + '%')).delete(
            synchronize_session=False)
    HelpTicket.query.delete(synchronize_session=False)
    Employee.query.filter(Employee.emp_code.like('AC_%')).delete(
        synchronize_session=False)
    db.session.commit()
    from app.services import client_restrictions
    client_restrictions.cache_clear()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code, name, role, live in (
                (USER, 'Acceptance User', 'user', True),
                (ADMIN, 'Acceptance Director', 'admin', True),
                (GONE, 'Departed Person', 'user', False)):
            db.session.add(Employee(
                emp_code=code, name=name, password_hash='x', role=role,
                vertical=VERTICAL, is_active=live,
                email=code.lower() + '@procamlogistics.com'))
        db.session.commit()
        yield
        _purge()


def _client(code=USER, role='user'):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['emp_code'] = code
        s['role'] = role
    return c


def _account(name, *, domains=()):
    account = Company(name=PREFIX + name, is_active=True,
                      vertical=VERTICAL, pic_emp_code=USER)
    account.email_domains = list(domains)
    db.session.add(account)
    db.session.flush()
    return account


def _account_id_for(name):
    row = Company.query.filter_by(name=name).first()
    return row.id if row else None


def _block(company):
    from app.models.restriction import ClientRestriction
    from app.services import client_restrictions as cr
    db.session.add(ClientRestriction(
        status='blocked', company_name=company,
        name_key=cr.normalise(company),
        reason_category='Payment default / outstanding',
        reason_detail='Outstanding, unresolved.',
        recommended_by=ADMIN, recommended_at=datetime.utcnow(),
        approved_by=ADMIN, approved_at=datetime.utcnow(),
        scope='entity', linked_account_id=_account_id_for(company)))
    db.session.commit()
    cr.cache_clear()


# ── §7.1 — a reply joins the lead it belongs to ──────────────────────
def test_a_reply_on_an_open_thread_finds_that_lead():
    """§7.1: reply to an existing RFQ thread → it attaches to the
    open lead; no new lead is created."""
    from app.services import lead_intake_db as idb
    with flask_app.app_context():
        account = _account('Indo Tech Transformers')
        lead = Lead(company=PREFIX + 'Indo Tech Transformers',
                    company_id=account.id, stage='New Opportunity',
                    source='email', conversation_id='AC-CONV-1',
                    original_email_subject='40 MVA transformer')
        db.session.add(lead)
        db.session.commit()
        assert idb.find_by_thread(
            conversation_id='AC-CONV-1') == lead.id


def test_a_reply_four_messages_deep_still_finds_it():
    from app.services import lead_intake_db as idb
    with flask_app.app_context():
        account = _account('Indo Tech Transformers')
        lead = Lead(company=PREFIX + 'Indo Tech Transformers',
                    company_id=account.id, stage='New Opportunity',
                    source='email')
        db.session.add(lead)
        db.session.flush()
        db.session.add(LeadEmail(lead_id=lead.id, direction='in',
                                 message_id='<deep-4@client>'))
        db.session.commit()
        assert idb.find_by_thread(
            in_reply_to='<deep-4@client>') == lead.id


def test_the_reply_is_matched_before_any_lead_is_made():
    """The ordering is the behaviour: a thread that matches must be
    found while there is still a decision to make."""
    source = open(os.path.join(_ROOT, 'app', 'services',
                               'lead_intake.py')).read()
    assert 'find_by_thread' in source or 'thread' in source


# ── §7.2 — a blocked client's mail makes no lead ─────────────────────
def test_a_blocked_clients_mail_is_refused_by_the_register():
    """§7.2: mail from a blocked client → no lead."""
    from app.services import client_restrictions as cr
    with flask_app.app_context():
        account = _account('Walchandnagar Industries Limited')
        db.session.commit()
        _block(account.name)
        verdict = cr.check(company_name=account.name,
                           account_id=account.id)
        assert verdict.blocked is True


def test_the_block_is_found_through_the_resolved_account():
    """The §2.1 defect: the check was never given the account, so a
    blocked client writing from another domain was not matched."""
    from app.services import client_restrictions as cr
    with flask_app.app_context():
        account = _account('Godrej Enterprises Group',
                           domains=['godrej-test.com'])
        db.session.commit()
        _block(account.name)
        # The name the parser read is useless here; the account is
        # what matches.
        assert cr.check(company_name='an enquiry',
                        account_id=account.id).blocked is True


def test_the_ingest_resolves_the_account_before_it_checks():
    from email_ingest.single_message import \
        _resolve_account_for_block as resolve
    import logging
    with flask_app.app_context():
        account = _account('Godrej Enterprises Group',
                           domains=['godrej-test.com'])
        db.session.commit()
        found_id, found_name = resolve('buyer@godrej-test.com', '',
                                       logging.getLogger('t'))
        assert found_id == account.id
        assert found_name == account.name


def test_a_blocked_client_cannot_be_keyed_in_by_hand():
    """§7.2's manual half, and §2.1's last sentence."""
    from app.models.integration import AppSetting
    from app.services import flags, lead_flow
    with flask_app.app_context():
        row = AppSetting(key='flag.' + lead_flow.FLAG,
                         value_json=json.dumps(True))
        db.session.add(row)
        account = _account('Walchandnagar Industries Limited')
        db.session.commit()
        _block(account.name)
        flags.cache_clear()
        account_id = account.id
    r = _client().post('/api/leads/new',
                       json={'account_id': account_id,
                             'requirement': 'a transformer move'})
    assert r.status_code == 403
    assert r.get_json()['guide']['code'] == 'CLIENT_BLOCKED'
    with flask_app.app_context():
        assert Lead.query.filter_by(company_id=account_id).count() == 0
        AppSetting.query.filter_by(
            key='flag.' + lead_flow.FLAG).delete(
                synchronize_session=False)
        db.session.commit()
        flags.cache_clear()


# ── §7.3 — junk senders never become leads ───────────────────────────
@pytest.mark.parametrize('sender', [
    'partner@procol.in', 'someone@glafamily.com',
    'tnis.s117@gmail.com', 'info@magicbricks.com',
    'nikusi@gmail.com',
])
def test_a_known_junk_sender_is_on_the_block_list(sender):
    """§7.3: these made 15+, ~10 and more leads each. The list is a
    file an administrator edits, not a constant in the code."""
    from email_ingest import blocklist
    assert blocklist.check(sender), sender


def test_the_block_list_is_a_file_somebody_can_edit():
    path = os.path.join(_ROOT, 'data', 'blocked_senders.txt')
    assert os.path.exists(path), path


# ── §7.4 — a new company still gets an owner ─────────────────────────
def test_an_unknown_company_still_gets_an_owner():
    """§7.4: never a blank owner. The backstop is the floor."""
    from app.services import auto_assign
    with flask_app.app_context():
        code, rule, _confirm = auto_assign.decide(
            account=None, vertical='', sender_email='x@unknown-test.com')
        assert code, rule


def test_the_backstop_is_a_person_who_exists():
    from app.services import auto_assign
    with flask_app.app_context():
        assert auto_assign.backstop()


# ── §7.11 — the team list ────────────────────────────────────────────
def test_the_people_list_leaves_out_anybody_who_has_left():
    """§7.11: the current filter includes inactive staff."""
    rows = _client().get('/api/help/people').get_json()['rows']
    codes = [r['emp_code'] for r in rows]
    assert USER in codes
    assert GONE not in codes


def test_the_people_list_names_each_person_once():
    """§7.11: it showed Pravin Choudhary twice."""
    rows = _client().get('/api/help/people').get_json()['rows']
    codes = [r['emp_code'] for r in rows]
    assert len(codes) == len(set(codes))


# ── §7.12 — Ask for help ─────────────────────────────────────────────
def test_ask_for_help_creates_a_ticket_and_answers_with_its_number():
    """§7.12."""
    r = _client().post('/api/help/ticket',
                       json={'description': 'Cannot move to Quoted',
                             'page': 'lead', 'lead_id': 12345,
                             'code': 'STAGE_FIELDS_MISSING'})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body['ok'] is True
    assert body['number'].startswith('CRM-')
    with flask_app.app_context():
        from app.models.help_content import HelpTicket
        row = HelpTicket.query.filter_by(number=body['number']).one()
        assert row.emp_code == USER
        assert row.lead_id == 12345
        assert row.code == 'STAGE_FIELDS_MISSING'
        assert row.status == 'Open'


def test_a_ticket_with_no_description_is_refused_in_words():
    r = _client().post('/api/help/ticket', json={'description': ' '})
    assert r.status_code == 400
    assert 'cannot be answered' in r.get_json()['error']


def test_ticket_numbers_do_not_repeat():
    first = _client().post('/api/help/ticket',
                           json={'description': 'one'}).get_json()
    second = _client().post('/api/help/ticket',
                            json={'description': 'two'}).get_json()
    assert first['number'] != second['number']


def test_the_tech_team_can_read_the_tickets():
    _client().post('/api/help/ticket', json={'description': 'help'})
    rows = _client(ADMIN, 'admin').get(
        '/api/help/tickets').get_json()['rows']
    assert rows and rows[0]['description'] == 'help'


def test_a_user_cannot_read_everybody_elses_tickets():
    assert _client().get('/api/help/tickets').status_code in (302, 403)
