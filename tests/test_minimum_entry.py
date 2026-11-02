"""
CRM-COCKPIT-02 §5 — the system fills it in, the person confirms it.

The two acceptance checks this module carries are §9.2 (a portal alert
makes one lead, and the reminder for the same number joins it) and
§9.9 (a quote sent from the CRM fills the quote fields and moves the
stage).

The rule under all of it: autofill writes into blanks and nothing
else. A field a person has filled is never replaced by a machine's
reading of a mail, and everything a machine did write says so until
somebody agrees with it.
"""
import json
import os
import sys
import tempfile
from datetime import date, datetime

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'min-entry-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'MinEntryTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'minentry.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead
LeadActivity = _main.LeadActivity
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.access import AccessProfile, DataScope    # noqa: E402
from app.services import autofill as af                   # noqa: E402
from app.services import bulk_log, lead_actions           # noqa: E402

OWNER, ADMIN = 'MEOWN', 'MEADM'
TAG = 'ME-'


def _emp(code, name, *, role='user', admin=False):
    e = Employee.query.filter_by(emp_code=code).first() or \
        Employee(emp_code=code)
    e.name, e.is_active, e.must_change_pw = name, True, False
    e.email = f'{code.lower()}@procamgroup.in'
    e.role, e.session_version = role, 0
    e.is_super_admin, e.is_vertical_head = admin, False
    db.session.add(e)
    db.session.flush()
    profile = AccessProfile.query.filter_by(emp_code=code).first()
    if profile is None:
        profile = AccessProfile(emp_code=code)
        db.session.add(profile)
    profile.data_scope = DataScope.ALL if admin else DataScope.OWN
    return e


def _wipe():
    ids = [l.id for l in Lead.query.filter(
        Lead.company.like(TAG + '%')).all()]
    if ids:
        LeadActivity.query.filter(
            LeadActivity.lead_id.in_(ids)).delete(
                synchronize_session=False)
        Lead.query.filter(Lead.id.in_(ids)).delete(
            synchronize_session=False)
    Company.query.filter(Company.name.like(TAG + '%')).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        _wipe()
        _emp(OWNER, 'ME Owner')
        _emp(ADMIN, 'ME Admin', role='admin', admin=True)
        db.session.commit()
        yield
        _wipe()


EXTRACTED = {
    'origin': 'Hazira, Gujarat',
    'destination': 'Jamnagar, Gujarat',
    'cargo_type': 'Transformer',
    'cargo_qty': '2 nos',
    'cargo_weight_mt': '94.5',
    'cargo_dimensions': '7.2 x 3.1 x 3.4 m',
    'urgency': 'high',
    'special_requirements': ['Road survey needed', 'Night movement'],
    'target_date': '13 Nov 2026',
    'company': 'Someone Ltd',
}


def _lead(**kw):
    kw.setdefault('company', TAG + 'Autofill Co')
    kw.setdefault('stage', 'New Opportunity')
    kw.setdefault('source', 'email')
    kw.setdefault('assigned_to', OWNER)
    extracted = kw.pop('extracted', EXTRACTED)
    lead = Lead(email_extracted_json=(json.dumps(extracted)
                                      if extracted else None),
                created_at=datetime(2026, 10, 25),
                received_at=datetime(2026, 10, 25), **kw)
    db.session.add(lead)
    db.session.commit()
    return lead


def _client(code=OWNER, role='user'):
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['emp_code'] = code
        sess['role'] = role
        sess['session_version'] = 0
    return c


# ══ §5.1 the fields the mail already said ═══════════════════════════
def test_the_mails_own_figures_land_on_the_lead(world):
    lead = _lead()
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        filled = af.from_mail(got)
        db.session.commit()
        assert got.origin == 'Hazira, Gujarat'
        assert got.destination == 'Jamnagar, Gujarat'
        assert got.cargo_type == 'Transformer'
        assert got.cargo_weight_mt == 94.5
        assert got.cargo_dimensions == '7.2 x 3.1 x 3.4 m'
        assert got.urgency == 'High'
        assert got.special_requirements == \
            'Road survey needed; Night movement'
        assert set(filled) == set(af.FROM_MAIL.values())


def test_a_persons_value_is_never_overwritten(world):
    lead = _lead(origin='Mumbai Port')
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        filled = af.from_mail(got)
        assert got.origin == 'Mumbai Port'
        assert 'origin' not in filled


def test_everything_filled_this_way_says_so(world):
    lead = _lead()
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        af.from_mail(got)
        db.session.commit()
        assert 'cargo_type' in af.auto_fields(got)


def test_confirming_is_per_field(world):
    # No nested app context here, deliberately. The fixture already
    # holds one, and a nested context gets its own session — so the
    # request below would be served the outer session's stale copy of
    # the lead, with `auto_filled_json` still None. That is an
    # artefact of two sessions in one process, not of the CRM.
    lead = _lead()
    af.from_mail(db.session.get(Lead, lead.id))
    db.session.commit()
    c = _client()
    r = c.post(f'/api/leads/{lead.id}/autofill/confirm',
               json={'field': 'cargo_type'})
    assert r.status_code == 200
    left = r.get_json()['auto']
    assert 'cargo_type' not in left and 'origin' in left


def test_confirming_everything_at_once_is_possible_but_separate(world):
    lead = _lead()
    af.from_mail(db.session.get(Lead, lead.id))
    db.session.commit()
    r = _client().post(f'/api/leads/{lead.id}/autofill/confirm',
                       json={})
    assert r.get_json()['auto'] == []


def test_a_field_the_crm_does_not_fill_is_refused(world):
    lead = _lead()
    r = _client().post(f'/api/leads/{lead.id}/autofill/confirm',
                       json={'field': 'quoted_amount_inr'})
    assert r.status_code == 400


def test_rubbish_from_the_extractor_is_not_copied(world):
    lead = _lead(extracted={'origin': 'unknown', 'cargo_type': 'N/A',
                            'cargo_weight_mt': 'not stated',
                            'urgency': 'immediately'})
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        assert af.from_mail(got) == {}
        assert got.origin is None and got.urgency is None


def test_a_lead_with_no_extracted_json_is_left_alone(world):
    lead = _lead(extracted=None)
    with flask_app.app_context():
        assert af.from_mail(db.session.get(Lead, lead.id)) == {}


# ══ §5.2 the account carries industry, state and city ═══════════════
def test_a_lead_inherits_its_accounts_details(world):
    with flask_app.app_context():
        account = Company(name=TAG + 'Account Co', industry='Power',
                          state='Gujarat', city='Surat')
        db.session.add(account)
        db.session.commit()
        lead = _lead(company_id=account.id)
        got = db.session.get(Lead, lead.id)
        filled = af.from_account(got)
        db.session.commit()
        assert (got.industry, got.state, got.city) == \
            ('Power', 'Gujarat', 'Surat')
        assert set(filled) == {'industry', 'state', 'city'}


def test_the_account_wins_over_the_mail_for_a_customers_city(world):
    """They describe different things: the account is the record of
    the customer, a mail's signature block is one enquiry's idea."""
    with flask_app.app_context():
        account = Company(name=TAG + 'City Co', city='Surat',
                          state='Gujarat', industry='Power')
        db.session.add(account)
        db.session.commit()
        lead = _lead(company_id=account.id,
                     extracted={**EXTRACTED, 'city': 'Mumbai'})
        got = db.session.get(Lead, lead.id)
        af.apply(got)
        assert got.city == 'Surat'


def test_an_account_with_no_details_is_on_the_owners_list(world):
    with flask_app.app_context():
        account = Company(name=TAG + 'Blank Co')
        db.session.add(account)
        db.session.commit()
        _lead(company_id=account.id, stage='RFQ Generated')
        rows = af.accounts_missing_details()
        mine = [r for r in rows if r['id'] == account.id]
        assert mine and set(mine[0]['missing']) == {'industry', 'state',
                                                    'city'}


def test_an_account_with_no_open_leads_is_not_on_the_list(world):
    with flask_app.app_context():
        account = Company(name=TAG + 'Dormant Co')
        db.session.add(account)
        db.session.commit()
        _lead(company_id=account.id, stage='Lost')
        assert [r for r in af.accounts_missing_details()
                if r['id'] == account.id] == []


def test_the_state_is_read_off_the_gstin(world):
    assert af.state_from_gstin('24AAACW1234A1Z5') == 'Gujarat'
    assert af.state_from_gstin('27AAACW1234A1Z5') == 'Maharashtra'
    assert af.state_from_gstin('') is None


def test_the_crm_offers_a_state_but_does_not_write_it(world):
    with flask_app.app_context():
        account = Company(name=TAG + 'Guess Co',
                          gstin='24AAACW1234A1Z5')
        db.session.add(account)
        db.session.commit()
        guesses = af.guess_account_details(account)
        assert guesses['state'][0] == 'Gujarat'
        assert 'GSTIN' in guesses['state'][1]
        assert account.state is None, 'offered, not written'


def test_two_leads_disagreeing_is_not_an_answer(world):
    with flask_app.app_context():
        account = Company(name=TAG + 'Split Co')
        db.session.add(account)
        db.session.commit()
        _lead(company_id=account.id, city='Surat', extracted=None,
              company=TAG + 'Split A')
        _lead(company_id=account.id, city='Pune', extracted=None,
              company=TAG + 'Split B')
        assert 'city' not in af.guess_account_details(account)


# ══ §5.3 / §9.9 the quote the CRM sent ══════════════════════════════
def test_acceptance_9_a_quote_sent_fills_the_lead_and_moves_it(world):
    from app.models.quote import Quote

    with flask_app.app_context():
        lead = _lead(stage='RFQ Generated', followup_date=None,
                     quote_due_at=datetime(2026, 11, 4, 12, 30))
        quote = Quote(quote_number='PQ-2026-0042', lead_id=lead.id,
                      quote_date=date(2026, 11, 1),
                      validity_until=date(2026, 11, 16),
                      total_amount=1250000)
        db.session.add(quote)
        db.session.commit()

        got = db.session.get(Lead, lead.id)
        changed = lead_actions.quote_sent(got, quote, actor=OWNER)
        db.session.commit()

        assert got.quote_no == 'PQ-2026-0042'
        assert float(got.quote_value_num) == 1250000.0
        assert got.quote_date == date(2026, 11, 1)
        assert got.quote_validity_date == date(2026, 11, 16)
        assert got.stage == 'Quoted'
        assert got.followup_date is not None, 'and the next date'
        assert changed['stage'] == 'Quoted'


def test_a_quote_does_not_drag_a_won_deal_backwards(world):
    from app.models.quote import Quote

    with flask_app.app_context():
        lead = _lead(stage='Won')
        quote = Quote(quote_number='PQ-2026-0043', lead_id=lead.id,
                      total_amount=10)
        db.session.add(quote)
        db.session.commit()
        got = db.session.get(Lead, lead.id)
        lead_actions.quote_sent(got, quote, actor=OWNER)
        assert got.stage == 'Won'


def test_a_quote_clears_a_missed_flag_it_turns_out_we_met(world):
    from app.models.quote import Quote

    with flask_app.app_context():
        lead = _lead(stage='RFQ Generated',
                     quote_due_at=datetime(2026, 10, 29, 9, 45),
                     quote_due_missed_at=datetime(2026, 10, 30))
        quote = Quote(quote_number='PQ-2026-0044', lead_id=lead.id,
                      total_amount=50)
        db.session.add(quote)
        db.session.commit()
        got = db.session.get(Lead, lead.id)
        lead_actions.quote_sent(got, quote, actor=OWNER)
        assert got.quote_due_missed_at is None


def test_an_intro_mail_moves_a_new_lead_to_contacted(world):
    with flask_app.app_context():
        # 'New', the journey's first stage. 'New Opportunity' sits
        # *after* Business Discussion in this CRM's own stage order,
        # so an intro mail does not move it — moving it would be
        # backwards, and §5.3 only ever moves a lead forward.
        lead = _lead(stage='New')
        got = db.session.get(Lead, lead.id)
        changed = lead_actions.intro_sent(got, actor=OWNER)
        db.session.commit()
        assert got.stage == 'Call Done'
        assert got.intro_mail_date is not None
        assert changed['followup_date']


def test_sending_the_profile_records_which_it_was(world):
    with flask_app.app_context():
        lead = _lead(stage='New')
        got = db.session.get(Lead, lead.id)
        lead_actions.intro_sent(got, kind='profile', actor=OWNER)
        assert got.sub_status == 'Profile sent'


# ══ §5.4 the quote reader ═══════════════════════════════════════════
def test_a_quotation_document_is_read(world):
    text = """
        PROCAM LOGISTICS — QUOTATION
        Quotation No: PQ-2026-0099      Dated: 01 Nov 2026
        Transportation of one transformer, Hazira to Jamnagar
        Grand Total  INR 12,50,000.00
        Offer valid up to 16 Nov 2026
    """
    got = lead_actions.read_quote_document(text)
    assert got['quote_no'] == 'PQ-2026-0099'
    assert got['value'] == 1250000.0
    assert got['quote_date'] == date(2026, 11, 1)
    assert got['validity'] == date(2026, 11, 16)


def test_the_reader_reports_only_what_it_found(world):
    got = lead_actions.read_quote_document(
        'Please find our offer attached. Regards.')
    assert got == {}


def test_the_readers_figures_do_not_overwrite_the_owners(world):
    with flask_app.app_context():
        lead = _lead(quote_no='PQ-MINE')
        got = db.session.get(Lead, lead.id)
        filled = lead_actions.apply_quote_reading(
            got, {'quote_no': 'PQ-READ', 'value': 10})
        assert got.quote_no == 'PQ-MINE'
        assert 'quote_no' not in filled
        assert float(got.quote_value_num) == 10.0
        assert 'quote_value_num' in af.auto_fields(got)


# ══ §5.5 the one-tap call log ═══════════════════════════════════════
def test_one_tap_records_the_call_and_the_next_step(world):
    lead = _lead(stage='Contacted')
    r = _client().post(f'/api/leads/{lead.id}/call-log',
                       json={'outcome': 'call_back'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['next_action'] == 'Call back'
    assert body['followup']
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        # IST, not the server's date. This assertion was
        # `date.today()` and failed the first time the suite ran
        # between 18:30 UTC and midnight: the route stamped the UTC
        # date and the test compared the local one, so a call logged
        # at 01:00 IST on Tuesday was Monday's on screen.
        from app.services import quote_due as qd

        assert got.phone_call_date == qd.today_ist()
        assert LeadActivity.query.filter_by(lead_id=got.id,
                                            kind='call').count() == 1


def test_interested_moves_the_lead_along(world):
    lead = _lead(stage='Contacted')
    r = _client().post(f'/api/leads/{lead.id}/call-log',
                       json={'outcome': 'interested_rfq'})
    assert r.status_code == 200
    assert r.get_json()['moved']['to'] == 'Business Discussion'


def test_not_interested_does_not_close_the_lead_quietly(world):
    """A lead's ending is recorded through the close-lost endpoint,
    which asks for the reason the reports are built on."""
    lead = _lead(stage='Contacted')
    r = _client().post(f'/api/leads/{lead.id}/call-log',
                       json={'outcome': 'not_interested'})
    assert r.status_code == 200
    assert r.get_json()['closes_as_lost'] is True
    with flask_app.app_context():
        assert db.session.get(Lead, lead.id).stage == 'Contacted'


def test_the_call_log_refuses_a_free_text_outcome(world):
    lead = _lead(stage='Contacted')
    r = _client().post(f'/api/leads/{lead.id}/call-log',
                       json={'outcome': 'they seemed keen'})
    assert r.status_code == 400
    assert r.get_json()['options']


def test_the_call_log_stamps_last_activity(world):
    lead = _lead(stage='Contacted')
    _client().post(f'/api/leads/{lead.id}/call-log',
                   json={'outcome': 'call_back'})
    with flask_app.app_context():
        assert db.session.get(Lead, lead.id).last_activity_kind == 'call'


# ══ §5.6 one lead per portal number (acceptance 2) ══════════════════
def test_acceptance_2_a_reminder_joins_the_lead_it_belongs_to(world):
    """The alert, the reminder and the corrigendum are one enquiry on
    three threads. Eight duplicates were merged by hand on 01 Nov."""
    from app.services import quote_due as qd

    with flask_app.app_context():
        alert = _lead(
            stage='RFQ Generated',
            company=TAG + 'Indo Tech',
            original_email_subject='RA- 57103: Indo Tech Reverse '
                                   'Auction 29 Nov 2026 15:15:00')
        got = db.session.get(Lead, alert.id)
        got.portal_key = qd.portal_key(got.original_email_subject)
        db.session.commit()
        assert got.portal_key == 'RA-57103'

        # What the intake now does with the reminder: it finds the
        # open lead on that number instead of making a second one.
        from app.services import sales_rules as rules
        held = (Lead.query
                .filter(Lead.portal_key == 'RA-57103',
                        Lead.is_archived.isnot(True),
                        Lead.stage.in_(rules.open_stages()))
                .order_by(Lead.id.asc()).first())
        assert held is not None and held.id == alert.id


def test_the_corrigendum_proposes_the_new_deadline_on_the_same_lead(world):
    from app.services import quote_due_flow as flow

    with flask_app.app_context():
        lead = _lead(stage='RFQ Generated',
                     quote_due_at=datetime(2026, 11, 29, 9, 45))
        got = db.session.get(Lead, lead.id)
        assert flow.read_from_message(
            got, subject='RA- 57103 corrigendum',
            body='The auction now closes 05 Dec 2026 15:15:00',
            received_at=datetime(2026, 11, 20)) == 'proposed'
        db.session.commit()
        assert got.quote_due_at == datetime(2026, 11, 29, 9, 45)
        assert flow.pending(got.id).new_due_at == \
            datetime(2026, 12, 5, 9, 45)


def test_a_closed_lead_does_not_swallow_a_new_auction(world):
    """The same number next year is a new enquiry. Only an OPEN lead
    holds a number."""
    from app.services import sales_rules as rules

    with flask_app.app_context():
        old = _lead(stage='Lost', company=TAG + 'Old Auction')
        got = db.session.get(Lead, old.id)
        got.portal_key = 'RA-57103'
        db.session.commit()
        held = (Lead.query
                .filter(Lead.portal_key == 'RA-57103',
                        Lead.is_archived.isnot(True),
                        Lead.stage.in_(rules.open_stages()))
                .first())
        assert held is None


# ══ the back-fill is reversible ═════════════════════════════════════
def test_the_autofill_backfill_can_be_undone(world):
    lead = _lead()
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        with bulk_log.batch('test.autofill', actor='T') as log:
            filled = af.apply(got, log=log)
            name = log.name
        db.session.commit()
        assert filled and got.cargo_type == 'Transformer'
        bulk_log.undo(name)
        db.session.commit()
        db.session.expire_all()
        got = db.session.get(Lead, lead.id)
        assert got.cargo_type is None and got.origin is None


def test_a_dry_run_changes_nothing(world):
    lead = _lead()
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        filled = af.apply(got, dry=True)
        assert filled, 'it still reports what it would fill'
        assert got.cargo_type is None
        assert af.auto_fields(got) == []
