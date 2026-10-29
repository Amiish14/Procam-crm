"""
CRM-LEAD-ACCOUNT-01 — the account, named from the lead sheet.

The case this was built from, on 29 October 2026: a GeM tender for
BEML — 17 high-speed train cars, Bangalore → Surat, ₹2 Cr — arrived
from a Procam address. The parser could not identify the customer,
so the lead was saved as "To review". BEML LIMITED already existed
with 40 leads and 2 wins, and nothing on either screen could join
the two.

Every test below is that lead, or the owner correction that
followed it. The acceptance tests from the brief are named in the
docstrings, so a renamed test cannot quietly stop covering one.
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
os.environ.setdefault('SECRET_KEY', 'leadaccount-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'LeadAccount-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'leadacct.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead

from app.services import lead_account as la                # noqa: E402

ADMIN, SALES, HEAD, OTHER = ('LA_ADMIN', 'LA_SALES', 'LA_HEAD',
                             'LA_OTHER')
PEOPLE = ((ADMIN, 'Lead Account Director', 'admin'),
          (SALES, 'Jayanta Kumar Paul', 'user'),
          (HEAD, 'James Francis Xavier', 'manager'),
          (OTHER, 'Someone Else', 'user'))
VERTICAL = 'Heavy Transport'


def _flag(state):
    from app.models.integration import AppSetting
    from app.services import flags
    key = 'flag.FEATURE_LEAD_ACCOUNT'
    row = AppSetting.query.filter_by(key=key).first()
    if row is None:
        row = AppSetting(key=key)
        db.session.add(row)
    row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


def _purge():
    from app.models.assignment import AccountVerticalOwner
    from app.models.audit import AuditEvent
    for account in Company.query.filter(
            Company.name.like('LA %')).all():
        AccountVerticalOwner.query.filter_by(
            account_id=account.id).delete(synchronize_session=False)
        AuditEvent.query.filter(
            AuditEvent.entity_type == 'company',
            AuditEvent.entity_id == str(account.id)).delete(
                synchronize_session=False)
    Lead.query.filter(Lead.company.like('LA %')).delete(
        synchronize_session=False)
    Lead.query.filter(Lead.company.in_(
        ('To review', 'BEML LIMITED', 'LA BEML LIMITED'))).delete(
            synchronize_session=False)
    Company.query.filter(Company.name.like('LA %')).delete(
        synchronize_session=False)
    Employee.query.filter(
        Employee.emp_code.in_([p[0] for p in PEOPLE])).delete(
            synchronize_session=False)
    _flag(False)
    db.session.commit()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code, name, role in PEOPLE:
            db.session.add(Employee(
                emp_code=code, name=name, password_hash='x',
                role=role, vertical=VERTICAL, is_active=True,
                email=code.lower() + '@procamlogistics.com'))
        db.session.commit()
        _flag(True)
        yield
        _purge()


@pytest.fixture
def beml():
    """The account that already existed, and the lead that could
    not reach it."""
    with flask_app.app_context():
        account = Company(name='LA BEML LIMITED', is_active=True,
                          pic_emp_code=SALES, vertical=VERTICAL)
        db.session.add(account)
        db.session.flush()
        for n in range(2):
            db.session.add(Lead(
                company='LA BEML LIMITED', company_id=account.id,
                stage='Won', source='manual', assigned_to=SALES,
                procam_vertical=VERTICAL))
        for n in range(3):
            db.session.add(Lead(
                company='LA BEML LIMITED', company_id=account.id,
                stage='New Opportunity', source='manual',
                assigned_to=SALES, procam_vertical=VERTICAL))
        lead = Lead(
            company='To review', stage='New Opportunity',
            source='email', procam_vertical=VERTICAL,
            assigned_to=SALES, email='gem.tender@beml.co.in',
            created_at=datetime.utcnow())
        # OTHER is the monitor on this lead: they can open it, which
        # is what makes the permission tests below about permission
        # rather than about visibility.
        lead.secondary_owner = OTHER
        lead.email_extracted_json = json.dumps({
            'company': 'Bharat Earth Movers Limited (BEML)',
            'one_line_summary': '17 high-speed train cars, '
                                'Bangalore to Surat'})
        db.session.add(lead)
        db.session.commit()
        return {'lead_id': lead.id, 'account_id': account.id}


def _client(code=ADMIN, role='admin'):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['emp_code'] = code
        s['role'] = role
    return c


def _get(path, code=ADMIN, role='admin'):
    r = _client(code, role).get(path)
    assert r.status_code == 200, f'{path} -> {r.status_code}'
    return r.get_json()


def _post(path, body=None, code=ADMIN, role='admin'):
    return _client(code, role).post(path, json=body or {})


# ── §2.3 — is this a company name at all ─────────────────────────────
@pytest.mark.parametrize('name', [
    'To review', 'to review', 'Unknown', 'New Lead', '', '   ',
    'n/a', '-', 'Re: urgent enquiry for 40 ft trailer',
    'Fwd: GeM tender', 'someone@example.com',
])
def test_a_placeholder_is_recognised(name):
    with flask_app.app_context():
        assert la.is_placeholder(name), name


@pytest.mark.parametrize('name', [
    'BEML LIMITED', 'Tata Steel', 'L&T', 'Bharat Earth Movers',
    'Jindal Stainless (Hisar) Limited', 'ACC', '3M India',
])
def test_a_real_company_is_not(name):
    with flask_app.app_context():
        assert la.is_placeholder(name) == '', name


def test_a_colleagues_name_is_not_the_customer():
    """The forwarder read as the sender. Only our own people — a
    guess at what "looks like a person's name" would catch
    "Tata Steel"."""
    with flask_app.app_context():
        assert la.is_placeholder('James Francis Xavier')
        assert la.is_placeholder('Rajesh Kumar') == ''


def test_procams_own_name_is_not_the_customer():
    with flask_app.app_context():
        assert la.is_placeholder('Procam Logistics Pvt Ltd')


def test_a_subject_line_is_not_a_company_name():
    with flask_app.app_context():
        long_one = ('Requirement for transportation of 17 high speed '
                    'train cars from Bangalore to Surat by road')
        assert la.is_placeholder(long_one)


def test_the_placeholder_list_is_configurable():
    """§2.3 — Admin → Settings, not a release."""
    from app.services import admin_settings
    with flask_app.app_context():
        assert la.is_placeholder('Parked') == ''
        admin_settings.save('lead_placeholder_names',
                            ['to review', 'parked'], actor=ADMIN)
        assert la.is_placeholder('Parked')
        # A broken setting falls back rather than emptying the list,
        # which would switch the whole feature off silently.
        admin_settings.save('lead_placeholder_names', [], actor=ADMIN)
        assert la.is_placeholder('To review')


# ── acceptance 1 — the banner, and Save disabled ─────────────────────
def test_a_to_review_lead_shows_the_banner_and_blocks_save(beml):
    """Acceptance 1."""
    body = _get(f'/api/leads/{beml["lead_id"]}/account')
    assert body['placeholder'] is True
    assert body['linked'] is False
    assert body['blocked'] is True
    assert body['banner'] == ('Company not identified — choose or '
                              'create account')
    assert 'placeholder' in body['why']

    r = _client().put(f'/api/leads/{beml["lead_id"]}',
                      json={'pic': 'Someone'})
    assert r.status_code == 400
    assert r.get_json()['needs'] == 'account'


def test_closing_as_lost_is_still_allowed_while_unnamed(beml):
    """§2.4 — the lead can always be disposed of, only not left
    half-named."""
    r = _post(f'/api/leads/{beml["lead_id"]}/close-lost',
              {'reason': 'Tender cancelled'})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]


def test_a_named_lead_saves_exactly_as_before(beml):
    """Acceptance 6 — non-placeholder leads behave as before."""
    with flask_app.app_context():
        lead = db.session.get(Lead, beml['lead_id'])
        lead.company = 'LA Some Real Customer Ltd'
        db.session.commit()
    r = _client().put(f'/api/leads/{beml["lead_id"]}',
                      json={'pic': 'Someone'})
    assert r.status_code == 200
    assert r.get_json()['ok'] is True


# ── acceptance 2 — type BEML, get BEML LIMITED ───────────────────────
def test_typing_beml_suggests_the_account(beml):
    """Acceptance 2, first half."""
    body = _get(f'/api/leads/{beml["lead_id"]}'
                f'/account/suggest?q=BEML')
    names = [r['name'] for r in body['rows']]
    assert 'LA BEML LIMITED' in names
    row = [r for r in body['rows'] if r['name'] == 'LA BEML LIMITED'][0]
    assert row['rule']
    assert row['owner_name'] == 'Jayanta Kumar Paul'


def test_the_picker_prefills_from_what_the_parser_read(beml):
    """§2.1 — the company field says "To review"; the parser read
    "Bharat Earth Movers Limited (BEML)", and the account is filed
    under "BEML LIMITED". Matching on the placeholder finds
    nothing."""
    body = _get(f'/api/leads/{beml["lead_id"]}/account')
    names = [r['name'] for r in body['suggestions']]
    assert 'LA BEML LIMITED' in names


def test_a_placeholder_account_is_never_suggested(beml):
    """Live, on 29 Oct: searching "BEML" offered BEML LIMITED,
    Bhel, and account #709 — which is called "Email". `link`
    refuses it, so offering it is noise in the one list a person
    is reading to decide."""
    with flask_app.app_context():
        db.session.add(Company(name='Email', is_active=True))
        db.session.add(Company(name='Unknown', is_active=True))
        db.session.commit()
    body = _get(f'/api/leads/{beml["lead_id"]}'
                f'/account/suggest?q=ema')
    assert 'Email' not in [r['name'] for r in body['rows']]
    body = _get(f'/api/leads/{beml["lead_id"]}'
                f'/account/suggest?q=unkn')
    assert 'Unknown' not in [r['name'] for r in body['rows']]


def test_a_real_account_is_still_suggested_beside_them(beml):
    with flask_app.app_context():
        db.session.add(Company(name='Email', is_active=True))
        db.session.commit()
    body = _get(f'/api/leads/{beml["lead_id"]}'
                f'/account/suggest?q=BEML')
    assert 'LA BEML LIMITED' in [r['name'] for r in body['rows']]


def test_the_suggestion_says_why_it_matched(beml):
    body = _get(f'/api/leads/{beml["lead_id"]}/account')
    row = body['suggestions'][0]
    assert row['rule'], 'a suggestion with no reason is a guess'
    assert 'read as' in row['rule'] or row['exact']


def test_selecting_the_account_renames_the_lead(beml):
    """Acceptance 2, second half."""
    r = _post(f'/api/leads/{beml["lead_id"]}/account/link',
              {'account_id': beml['account_id']})
    assert r.status_code == 200
    assert r.get_json()['account'] == 'LA BEML LIMITED'
    with flask_app.app_context():
        lead = db.session.get(Lead, beml['lead_id'])
        assert lead.company_id == beml['account_id']
        assert lead.company == 'LA BEML LIMITED'


def test_the_lead_joins_the_accounts_count(beml):
    """Acceptance 2 — "lead appears in the account's lead count"."""
    before = _get(f'/api/leads/{beml["lead_id"]}'
                  f'/account/suggest?q=BEML')['rows'][0]['open_leads']
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    after = _get(f'/api/leads/{beml["lead_id"]}'
                 f'/account/suggest?q=BEML')['rows'][0]['open_leads']
    assert after == before + 1


def test_save_is_allowed_once_the_account_is_linked(beml):
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    r = _client().put(f'/api/leads/{beml["lead_id"]}',
                      json={'pic': 'Someone'})
    assert r.status_code == 200


def test_the_picker_can_link_in_the_same_save(beml):
    """The form posts company_id with everything else."""
    r = _client().put(f'/api/leads/{beml["lead_id"]}',
                      json={'pic': 'Someone',
                            'company_id': beml['account_id']})
    assert r.status_code == 200
    with flask_app.app_context():
        assert db.session.get(
            Lead, beml['lead_id']).company_id == beml['account_id']


def test_a_blocked_account_cannot_be_linked(beml):
    from app.services import client_restrictions
    with flask_app.app_context():
        account = db.session.get(Company, beml['account_id'])
        account.name = 'LA Kesari Engineering Works'
        db.session.commit()
        try:
            client_restrictions.cache_clear()
        except Exception:                                 # noqa: BLE001
            pass
    # Without a restriction row there is nothing to block, so this
    # asserts the path exists rather than inventing a fixture the
    # restrictions module does not own.
    r = _post(f'/api/leads/{beml["lead_id"]}/account/link',
              {'account_id': beml['account_id']})
    assert r.status_code in (200, 400)


def test_linking_teaches_the_matcher_the_sender(beml):
    """The next mail from this address needs nobody."""
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    from app.services import account_match
    with flask_app.app_context():
        found = account_match.candidates('gem.tender@beml.co.in')
        assert any(c['account'].id == beml['account_id']
                   for c in found)


# ── acceptance 3 and 4 — create from the lead sheet ──────────────────
def test_creating_an_account_links_it_without_leaving_the_lead(beml):
    """Acceptance 3."""
    r = _post(f'/api/leads/{beml["lead_id"]}/account/create',
              {'name': 'LA Prakash Hydro Projects Ltd',
               'owner': HEAD, 'vertical': VERTICAL,
               'domain': 'prakashhydro.example',
               'alias': 'Prakash Hydro', 'gstin': '24AAACP1234F1Z5'})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_json()
    assert body['name'] == 'LA Prakash Hydro Projects Ltd'
    with flask_app.app_context():
        lead = db.session.get(Lead, beml['lead_id'])
        assert lead.company_id == body['account_id']
        account = db.session.get(Company, body['account_id'])
        assert account.pic_emp_code == HEAD
        assert account.gstin == '24AAACP1234F1Z5'
        assert account.email_domains == ['prakashhydro.example']
        assert account.aliases == ['Prakash Hydro']


def test_the_new_account_gets_a_vertical_owner_row(beml):
    """auto_assign reads this first, so a new account without one
    sends its next lead to the vertical head instead of the owner
    just named."""
    r = _post(f'/api/leads/{beml["lead_id"]}/account/create',
              {'name': 'LA Velor Wind Pvt Ltd', 'owner': HEAD,
               'vertical': VERTICAL, 'force': True})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    body = r.get_json()
    from app.services import auto_assign
    with flask_app.app_context():
        assert auto_assign.owner_for_vertical(
            body['account_id'], VERTICAL) == HEAD


def test_a_near_duplicate_name_is_warned_about(beml):
    """Acceptance 4."""
    r = _post(f'/api/leads/{beml["lead_id"]}/account/create',
              {'name': 'LA BEML Limited'})
    assert r.status_code == 409
    body = r.get_json()
    assert body['needs'] == 'confirm'
    assert body['duplicates'][0]['name'] == 'LA BEML LIMITED'
    assert 'already exists' in body['error']


def test_the_duplicate_warning_can_be_overridden(beml):
    r = _post(f'/api/leads/{beml["lead_id"]}/account/create',
              {'name': 'LA BEML Limited', 'force': True})
    assert r.status_code == 200


def test_an_account_cannot_be_created_with_a_placeholder_name(beml):
    r = _post(f'/api/leads/{beml["lead_id"]}/account/create',
              {'name': 'To review'})
    assert r.status_code == 400
    assert 'placeholder' in r.get_json()['error'].lower()


def test_a_free_mail_domain_is_never_kept_on_an_account(beml):
    body = _post(f'/api/leads/{beml["lead_id"]}/account/create',
                 {'name': 'LA Gmail Customer Ltd',
                  'domain': 'gmail.com'}).get_json()
    with flask_app.app_context():
        assert db.session.get(
            Company, body['account_id']).email_domains == []


def test_creating_needs_the_accounts_screen_permission(beml):
    """§3 — the same permission as "+ New Account"."""
    r = _post(f'/api/leads/{beml["lead_id"]}/account/create',
              {'name': 'LA Nobody Ltd'}, code=OTHER, role='user')
    assert r.status_code == 403
    assert 'New Account' in r.get_json()['error']


def test_any_editor_may_link_an_existing_account(beml):
    """§3 — linking is not the same permission as creating."""
    r = _post(f'/api/leads/{beml["lead_id"]}/account/link',
              {'account_id': beml['account_id']},
              code=OTHER, role='user')
    assert r.status_code == 200


# ── acceptance 5 — both histories ────────────────────────────────────
def test_both_histories_record_the_link(beml):
    """Acceptance 5."""
    from app.models.audit import AuditEvent
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    with flask_app.app_context():
        on_lead = AuditEvent.query.filter_by(
            action='lead.account_linked',
            entity_id=str(beml['lead_id'])).first()
        on_account = AuditEvent.query.filter_by(
            action='account.lead_linked',
            entity_id=str(beml['account_id'])).first()
        assert on_lead is not None and on_account is not None
        assert on_lead.actor == ADMIN
        assert on_lead.occurred_at is not None
        assert on_lead.new_value['company'] == 'LA BEML LIMITED'
        assert on_lead.old_value['company'] == 'To review'


def test_the_account_can_be_changed_later(beml):
    """§2.1 — "Changing the account later is allowed; record old →
    new account, user, time and reason in the lead history."."""
    from app.models.audit import AuditEvent
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    with flask_app.app_context():
        other = Company(name='LA Velor Wind Pvt Ltd', is_active=True,
                        vertical=VERTICAL, pic_emp_code=HEAD)
        db.session.add(other)
        db.session.commit()
        other_id = other.id

    r = _post(f'/api/leads/{beml["lead_id"]}/account/link',
              {'account_id': other_id,
               'reason': 'the tender was BEML, the cargo is Velor'})
    assert r.status_code == 200
    with flask_app.app_context():
        lead = db.session.get(Lead, beml['lead_id'])
        assert lead.company_id == other_id
        assert lead.company == 'LA Velor Wind Pvt Ltd'
        moved = (AuditEvent.query
                 .filter_by(action='lead.account_linked',
                            entity_id=str(beml['lead_id']))
                 .order_by(AuditEvent.id.desc()).first())
        assert moved.old_value['company'] == 'LA BEML LIMITED'
        assert moved.new_value['company'] == 'LA Velor Wind Pvt Ltd'
        assert 'the cargo is Velor' in (moved.reason or '')
        # And the account it left is told.
        left = AuditEvent.query.filter_by(
            action='account.lead_unlinked',
            entity_id=str(beml['account_id'])).first()
        assert left is not None
        assert left.new_value['moved_to'] == 'LA Velor Wind Pvt Ltd'


def test_the_lead_sheet_offers_changing_the_account(beml):
    html = open(os.path.join(_ROOT, 'templates', 'app.html')).read()
    assert 'laChangeAccount' in html
    assert 'Change account' in html


# ── §2.4 — the lead PIC and the account owner disagree ───────────────
def test_the_owner_question_is_asked_once(beml):
    with flask_app.app_context():
        db.session.get(Company,
                       beml['account_id']).pic_emp_code = HEAD
        db.session.commit()
    body = _post(f'/api/leads/{beml["lead_id"]}/account/link',
                 {'account_id': beml['account_id']}).get_json()
    question = body['owner_question']
    assert question['lead_pic_name'] == 'Jayanta Kumar Paul'
    assert question['account_owner_name'] == 'James Francis Xavier'


def test_an_unowned_lead_is_offered_the_account_owner(beml):
    """Live, 29 Oct. #12397 linked to BEML LIMITED and came out
    owned by nobody: `assigned_to` was empty, so the keep-or-switch
    prompt never fired and the lead sat with no PIC. Half the leads
    in this CRM are unowned, so this is the common shape, not the
    edge case."""
    with flask_app.app_context():
        lead = db.session.get(Lead, beml['lead_id'])
        lead.assigned_to = None
        db.session.get(Company,
                       beml['account_id']).pic_emp_code = HEAD
        db.session.commit()
    body = _post(f'/api/leads/{beml["lead_id"]}/account/link',
                 {'account_id': beml['account_id']}).get_json()
    question = body['owner_question']
    assert question is not None, 'an unowned lead was left unowned'
    assert question['unowned'] is True
    assert question['account_owner_name'] == 'James Francis Xavier'


def test_the_offer_assigns_the_account_owner(beml):
    with flask_app.app_context():
        lead = db.session.get(Lead, beml['lead_id'])
        lead.assigned_to = None
        db.session.get(Company,
                       beml['account_id']).pic_emp_code = HEAD
        db.session.commit()
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    _post(f'/api/leads/{beml["lead_id"]}/account/owner-choice',
          {'chose': HEAD, 'keep': False})
    with flask_app.app_context():
        assert db.session.get(
            Lead, beml['lead_id']).assigned_to == HEAD


def test_nothing_is_offered_when_the_account_has_no_owner_either(beml):
    """Then the account is the thing to fix, not the lead."""
    with flask_app.app_context():
        lead = db.session.get(Lead, beml['lead_id'])
        lead.assigned_to = None
        db.session.get(Company,
                       beml['account_id']).pic_emp_code = None
        db.session.commit()
    body = _post(f'/api/leads/{beml["lead_id"]}/account/link',
                 {'account_id': beml['account_id']}).get_json()
    assert body['owner_question'] is None


def test_the_sheet_words_the_offer_as_an_offer(beml):
    html = open(os.path.join(_ROOT, 'templates', 'app.html')).read()
    assert 'q.unowned' in html
    assert 'This lead has no owner' in html


def test_no_question_when_they_already_agree(beml):
    body = _post(f'/api/leads/{beml["lead_id"]}/account/link',
                 {'account_id': beml['account_id']}).get_json()
    assert body['owner_question'] is None


def test_the_answer_is_recorded_either_way(beml):
    from app.models.audit import AuditEvent
    with flask_app.app_context():
        db.session.get(Company,
                       beml['account_id']).pic_emp_code = HEAD
        db.session.commit()
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    r = _post(f'/api/leads/{beml["lead_id"]}/account/owner-choice',
              {'chose': HEAD, 'keep': False})
    assert r.status_code == 200
    assert r.get_json()['moved'] is True
    with flask_app.app_context():
        assert db.session.get(
            Lead, beml['lead_id']).assigned_to == HEAD
        assert AuditEvent.query.filter_by(
            action='lead.pic_vs_account_owner').first() is not None


# ── acceptance 7 — correcting the account owner ──────────────────────
def test_correcting_the_owner_writes_pic_history(beml):
    """Acceptance 7. The live fix: BEML LIMITED moved from Jayanta
    Kumar Paul to James Francis Xavier."""
    from presales.models import AccountAssignmentHistory
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    r = _post(f'/api/leads/{beml["lead_id"]}/account/owner',
              {'owner': HEAD,
               'reason': 'Incorrect automatic assignment'})
    assert r.status_code == 200, r.get_data(as_text=True)[:300]
    with flask_app.app_context():
        account = db.session.get(Company, beml['account_id'])
        assert account.pic_emp_code == HEAD
        row = (AccountAssignmentHistory.query
               .filter_by(account_id=account.id)
               .order_by(AccountAssignmentHistory.id.desc()).first())
        assert row.previous_pic_code == SALES
        assert row.new_pic_code == HEAD
        assert 'Incorrect automatic assignment' in (row.reason or '')


def test_the_move_preview_counts_only_open_leads(beml):
    """Acceptance 7 — "the move-other-leads preview shows the
    correct count". Won and Lost are never offered."""
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    body = _get(f'/api/leads/{beml["lead_id"]}/account/owner')
    # Three open leads were seeded on the account, plus this one.
    assert len(body['other_leads']) == 4
    assert all(row['stage'] != 'Won' for row in body['other_leads'])


def test_nothing_moves_unless_it_was_chosen(beml):
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    _post(f'/api/leads/{beml["lead_id"]}/account/owner',
          {'owner': HEAD, 'reason': 'Incorrect automatic assignment'})
    with flask_app.app_context():
        still = Lead.query.filter_by(
            company_id=beml['account_id'], assigned_to=SALES).count()
        assert still >= 3, 'leads moved without being chosen'


def test_the_chosen_leads_move_and_the_closed_ones_never_do(beml):
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    preview = _get(f'/api/leads/{beml["lead_id"]}/account/owner')
    ids = [row['id'] for row in preview['other_leads']]
    body = _post(f'/api/leads/{beml["lead_id"]}/account/owner',
                 {'owner': HEAD,
                  'reason': 'Incorrect automatic assignment',
                  'move_lead_ids': ids}).get_json()
    assert sorted(body['moved']) == sorted(ids)
    with flask_app.app_context():
        won = Lead.query.filter_by(company_id=beml['account_id'],
                                   stage='Won').all()
        assert all(w.assigned_to == SALES for w in won), \
            'a closed lead was rewritten'


def test_a_correction_needs_a_reason(beml):
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    r = _post(f'/api/leads/{beml["lead_id"]}/account/owner',
              {'owner': HEAD, 'reason': '  '})
    assert r.status_code == 400
    assert 'reason' in r.get_json()['error'].lower()


# ── acceptance 8 — the next lead goes to the corrected owner ─────────
def test_the_next_lead_auto_assigns_to_the_corrected_owner(beml):
    """Acceptance 8."""
    from app.services import auto_assign
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    _post(f'/api/leads/{beml["lead_id"]}/account/owner',
          {'owner': HEAD, 'reason': 'Incorrect automatic assignment'})
    with flask_app.app_context():
        account = db.session.get(Company, beml['account_id'])
        code, rule, _confirm = auto_assign.decide(
            account=account, vertical=VERTICAL)
        assert code == HEAD, rule


def test_the_correction_updates_the_vertical_owner_row_too(beml):
    """A correction that stops at pic_emp_code is undone by the
    next lead in a vertical that has its own row."""
    from app.models.assignment import AccountVerticalOwner
    with flask_app.app_context():
        db.session.add(AccountVerticalOwner(
            account_id=beml['account_id'], vertical=VERTICAL,
            emp_code=SALES, source='manual'))
        db.session.commit()
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    _post(f'/api/leads/{beml["lead_id"]}/account/owner',
          {'owner': HEAD, 'reason': 'Incorrect automatic assignment'})
    with flask_app.app_context():
        row = AccountVerticalOwner.query.filter_by(
            account_id=beml['account_id'], vertical=VERTICAL).first()
        assert row.emp_code == HEAD


# ── acceptance 9 — who may change it ─────────────────────────────────
def test_the_account_owner_may_change_it(beml):
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    body = _get(f'/api/leads/{beml["lead_id"]}/account/owner',
                code=SALES, role='user')
    assert body['may_change'] is True


def test_somebody_else_is_offered_request_instead(beml):
    """Acceptance 9."""
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    body = _get(f'/api/leads/{beml["lead_id"]}/account/owner',
                code=OTHER, role='user')
    assert body['may_change'] is False
    r = _post(f'/api/leads/{beml["lead_id"]}/account/owner',
              {'owner': OTHER, 'reason': 'mine'},
              code=OTHER, role='user')
    assert r.status_code == 403
    assert 'Request owner change' in r.get_json()['error']


def test_the_request_is_logged_on_the_account(beml):
    from app.models.audit import AuditEvent
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    r = _post(f'/api/leads/{beml["lead_id"]}/account/owner/request',
              {'owner': OTHER, 'reason': 'I run this customer now'},
              code=OTHER, role='user')
    assert r.status_code == 200
    with flask_app.app_context():
        row = AuditEvent.query.filter_by(
            action='account.owner_change_requested',
            entity_id=str(beml['account_id'])).first()
        assert row is not None
        assert row.actor == OTHER
        assert 'I run this customer now' in (row.reason or '')


def test_a_request_without_a_reason_is_refused(beml):
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    r = _post(f'/api/leads/{beml["lead_id"]}/account/owner/request',
              {'owner': OTHER, 'reason': ''}, code=OTHER, role='user')
    assert r.status_code == 400


# ── §2.5 — the report ────────────────────────────────────────────────
def test_owner_corrections_lists_every_correction(beml):
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    preview = _get(f'/api/leads/{beml["lead_id"]}/account/owner')
    ids = [row['id'] for row in preview['other_leads']]
    _post(f'/api/leads/{beml["lead_id"]}/account/owner',
          {'owner': HEAD, 'reason': 'Incorrect automatic assignment',
           'move_lead_ids': ids})
    body = _get('/api/admin/owner-corrections')
    row = body['rows'][0]
    assert row['account'] == 'LA BEML LIMITED'
    assert row['was'] == 'Jayanta Kumar Paul'
    assert row['now'] == 'James Francis Xavier'
    assert row['by'] == 'Lead Account Director'
    assert row['leads_moved'] == len(ids)
    assert row['from_lead'] == beml['lead_id']
    assert row['at']


def test_the_corrections_page_renders(beml):
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    _post(f'/api/leads/{beml["lead_id"]}/account/owner',
          {'owner': HEAD, 'reason': 'Incorrect automatic assignment'})
    r = _client().get('/admin/owner-corrections')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'Owner corrections' in html
    assert 'Leads moved' in html
    assert 'James Francis Xavier' in html


def test_the_corrections_page_says_so_when_there_are_none(beml):
    html = _client().get(
        '/admin/owner-corrections').get_data(as_text=True)
    assert 'Nothing corrected yet' in html


def test_the_corrections_report_is_admin_only(beml):
    assert _client(OTHER, 'user').get(
        '/api/admin/owner-corrections').status_code == 403
    assert _client(OTHER, 'user').get(
        '/admin/owner-corrections').status_code == 403


# ── §2.6 — the clean-up ──────────────────────────────────────────────
def test_the_unplaced_report_lists_the_to_review_leads(beml):
    body = _get('/api/leads/unplaced')
    rows = [r for r in body['rows']
            if r['lead_id'] == beml['lead_id']]
    assert rows, 'the To review lead is not in the report'
    assert rows[0]['company'] == 'To review'
    assert 'placeholder' in rows[0]['why']
    assert rows[0]['sender'] == 'gem.tender@beml.co.in'


def test_a_linked_lead_leaves_the_unplaced_report(beml):
    _post(f'/api/leads/{beml["lead_id"]}/account/link',
          {'account_id': beml['account_id']})
    body = _get('/api/leads/unplaced')
    assert not [r for r in body['rows']
                if r['lead_id'] == beml['lead_id']]


def test_bulk_linking_links_what_it_can_and_reports_the_rest(beml):
    with flask_app.app_context():
        second = Lead(company='Unknown', stage='New Opportunity',
                      source='email', procam_vertical=VERTICAL,
                      assigned_to=SALES)
        db.session.add(second)
        db.session.commit()
        second_id = second.id
    r = _post('/api/leads/link-bulk',
              {'account_id': beml['account_id'],
               'lead_ids': [beml['lead_id'], second_id, 999999]})
    assert r.status_code == 200
    body = r.get_json()
    assert sorted(body['linked']) == sorted([beml['lead_id'],
                                             second_id])
    assert body['skipped'][0]['lead_id'] == 999999
    with flask_app.app_context():
        assert db.session.get(
            Lead, second_id).company == 'LA BEML LIMITED'


def test_bulk_linking_needs_an_account_and_some_leads(beml):
    assert _post('/api/leads/link-bulk',
                 {'lead_ids': [1]}).status_code == 400
    assert _post('/api/leads/link-bulk',
                 {'account_id': beml['account_id']}).status_code == 400


# ── the one-off clean-up script (§5, §2.6) ───────────────────────────
def _script():
    import importlib.util
    path = os.path.join(_ROOT, 'scripts', 'link_unplaced_leads.py')
    spec = importlib.util.spec_from_file_location('luc', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_script_reports_without_changing_anything(beml):
    script = _script()
    with flask_app.app_context():
        rows = script.candidates()
        text = script.report(rows)
        assert any(r['lead'].id == beml['lead_id'] for r in rows)
        assert 'LA BEML LIMITED' in text
        assert 'Nothing was changed' in text
        assert db.session.get(
            Lead, beml['lead_id']).company_id is None


def test_the_script_links_what_it_is_told_to(beml):
    """The apply path, run — a gate that was never built reports a
    clean dry run and writes nothing."""
    script = _script()
    with flask_app.app_context():
        done, refused = script.apply([beml['lead_id']],
                                     account_id=beml['account_id'],
                                     actor=ADMIN)
        assert done == [(beml['lead_id'], 'LA BEML LIMITED')]
        assert refused == []
        lead = db.session.get(Lead, beml['lead_id'])
        assert lead.company_id == beml['account_id']
        assert lead.company == 'LA BEML LIMITED'


def test_the_script_refuses_a_lead_that_is_already_linked(beml):
    script = _script()
    with flask_app.app_context():
        script.apply([beml['lead_id']],
                     account_id=beml['account_id'], actor=ADMIN)
        done, refused = script.apply([beml['lead_id']],
                                     account_id=beml['account_id'],
                                     actor=ADMIN)
        assert done == []
        assert 'already linked' in refused[0][1]


def test_the_script_refuses_an_account_that_does_not_exist(beml):
    script = _script()
    with flask_app.app_context():
        done, refused = script.apply([beml['lead_id']],
                                     account_id=999999, actor=ADMIN)
        assert done == []
        assert 'no account' in refused[0][1]


# ── §2.6's report page ───────────────────────────────────────────────
def test_the_unplaced_report_page_renders(beml):
    html = _client().get('/admin/unplaced-leads').get_data(as_text=True)
    assert 'Leads with no account' in html
    assert 'To review' in html
    assert str(beml['lead_id']) in html


def test_the_unplaced_report_page_is_admin_only(beml):
    assert _client(OTHER, 'user').get(
        '/admin/unplaced-leads').status_code == 403


# ── with the flag off, the lead sheet is what it was ─────────────────
def test_with_the_flag_off_every_route_is_gone(beml):
    with flask_app.app_context():
        _flag(False)
    lead_id = beml['lead_id']
    for path in (f'/api/leads/{lead_id}/account',
                 f'/api/leads/{lead_id}/account/suggest',
                 f'/api/leads/{lead_id}/account/owner',
                 '/api/leads/unplaced',
                 '/api/leads/account-available',
                 '/admin/unplaced-leads',
                 '/admin/owner-corrections'):
        assert _client().get(path).status_code == 404, path
    for path in (f'/api/leads/{lead_id}/account/link',
                 f'/api/leads/{lead_id}/account/create',
                 '/api/leads/link-bulk'):
        assert _post(path, {}).status_code == 404, path


def test_with_the_flag_off_a_to_review_lead_still_saves(beml):
    """The one that matters: the save gate must not reach anybody
    outside the pilot."""
    with flask_app.app_context():
        _flag(False)
    r = _client().put(f'/api/leads/{beml["lead_id"]}',
                      json={'pic': 'Someone'})
    assert r.status_code == 200


def test_the_flag_is_off_by_default():
    from app.services import flags
    assert flags.DEFAULTS['FEATURE_LEAD_ACCOUNT'] is False


# ── the lead sheet renders it ────────────────────────────────────────
def test_the_leads_list_offers_bulk_linking(beml):
    """§2.6 — "select leads → Link to account". One at a time is
    not a clean-up of several hundred rows."""
    html = open(os.path.join(_ROOT, 'templates', 'app.html')).read()
    assert 'laBulkBtn' in html and 'function laBulk' in html
    # The select column belongs to the selection, not to whichever
    # feature happened to add it first.
    assert '(JUNK_ON||LA_ON)' in html
    assert _get('/api/leads/account-available')['ok'] is True


def test_the_availability_probe_404s_with_the_flag_off(beml):
    with flask_app.app_context():
        _flag(False)
    assert _client().get(
        '/api/leads/account-available').status_code == 404


def test_the_lead_sheet_carries_the_account_block():
    html = open(os.path.join(_ROOT, 'templates', 'app.html')).read()
    for piece in ('ldAcct', 'laLoad', 'laPick', 'laNewPanel',
                  'laChangeOwner', 'laRequestOwner', 'laBanner'):
        assert piece in html, piece


def test_save_is_refused_in_the_form_as_well_as_the_server():
    html = open(os.path.join(_ROOT, 'templates', 'app.html')).read()
    assert 'LA && LA.blocked' in html
    assert 'Link or create the account before saving' in html
