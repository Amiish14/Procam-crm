"""
The lead screen, rebuilt — and the thirteen acceptance tests.

CRM-LEAD-FLOW-01 §3, §4, §7. The screen showed about 25 fields in
nine sections to everybody at every stage, including the intake
scoring the CRM writes for itself. The new one answers three
questions: where is this lead, what should happen next, and what is
missing.

It is a separate template behind FEATURE_LEAD_POPUP_V2.
`templates/app.html` is not touched, so with the flag off a person
gets exactly the screen they had yesterday — the first test below
holds that.

The §7 acceptance tests are named in the docstrings, so a renamed
test cannot quietly stop covering one.
"""
import importlib
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'popup-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'PopupTest-123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'popup.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead
LeadEmail = _main.LeadEmail

from app.services import lead_flow as lf                  # noqa: E402

PREFIX = 'LF '
OWNER, HEAD, OTHER, ADMIN = 'LF_OWN', 'LF_HEAD', 'LF_OTH', 'LF_ADM'
PEOPLE = ((OWNER, 'Lead Owner', 'user'),
          (HEAD, 'Vertical Head', 'manager'),
          (OTHER, 'Someone Else', 'user'),
          (ADMIN, 'Flow Director', 'admin'))
VERTICAL = 'Heavy Transport'


def _flag(name, state):
    from app.models.integration import AppSetting
    from app.services import flags
    row = AppSetting.query.filter_by(key='flag.' + name).first()
    if row is None:
        row = AppSetting(key='flag.' + name)
        db.session.add(row)
    row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


def _purge():
    from app.models.audit import AuditEvent
    from app.models.email_admin import VerticalHead
    ids = [r.id for r in Lead.query.filter(
        Lead.company.like(PREFIX + '%')).all()]
    if ids:
        LeadEmail.query.filter(LeadEmail.lead_id.in_(ids)).delete(
            synchronize_session=False)
        _main.LeadStageHistory.query.filter(
            _main.LeadStageHistory.lead_id.in_(ids)).delete(
                synchronize_session=False)
        AuditEvent.query.filter(
            AuditEvent.entity_type == 'lead',
            AuditEvent.entity_id.in_([str(i) for i in ids])).delete(
                synchronize_session=False)
    Lead.query.filter(Lead.company.like(PREFIX + '%')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like(PREFIX + '%')).delete(
        synchronize_session=False)
    VerticalHead.query.filter_by(vertical=VERTICAL).delete(
        synchronize_session=False)
    Employee.query.filter(Employee.emp_code.like('LF_%')).delete(
        synchronize_session=False)
    from app.models.restriction import ClientRestriction
    ClientRestriction.query.filter(
        ClientRestriction.company_name.like(PREFIX + '%')).delete(
            synchronize_session=False)
    from app.services import client_restrictions
    client_restrictions.cache_clear()
    _flag(lf.FLAG, False)
    db.session.commit()
    from app.services import vertical_heads
    vertical_heads.cache_clear()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        from app.models.email_admin import VerticalHead
        for code, name, role in PEOPLE:
            db.session.add(Employee(
                emp_code=code, name=name, password_hash='x', role=role,
                vertical=VERTICAL, is_active=True,
                email=code.lower() + '@procamlogistics.com'))
        db.session.add(VerticalHead(vertical=VERTICAL,
                                    head_emp_code=HEAD, active=True))
        db.session.commit()
        from app.services import vertical_heads
        vertical_heads.cache_clear()
        _flag(lf.FLAG, True)
        yield
        _purge()


def _account(name='Hitachi Energy India Limited'):
    account = Company(name=PREFIX + name, is_active=True,
                      pic_emp_code=OWNER, vertical=VERTICAL)
    db.session.add(account)
    db.session.flush()
    return account


def _lead(*, stage='New', account=None, owner=OWNER, **kw):
    lead = Lead(company=PREFIX + 'Hitachi Energy India Limited',
                company_id=account.id if account else None,
                stage=stage, source='email', assigned_to=owner,
                assigned_name='Lead Owner',
                procam_vertical=VERTICAL,
                created_at=datetime.utcnow(), **kw)
    db.session.add(lead)
    db.session.flush()
    return lead


def _ready_for_rfq(account):
    """A lead carrying everything New and Contacted ask for."""
    lead = _lead(account=account, project='2 x 40 MVA transformers',
                 origin='Vadodara', destination='Lapanga',
                 opportunity_value_num=2500000,
                 value_basis='Client budget',
                 followup_date=date.today() + timedelta(days=3))
    db.session.add(LeadEmail(lead_id=lead.id, subject='RFQ',
                             direction='in'))
    db.session.flush()
    return lead


def _block(company):
    """Put a company on the block register, the way the register's
    own tests do."""
    from app.models.restriction import ClientRestriction
    from app.services import client_restrictions as cr

    row = ClientRestriction(
        status='blocked', company_name=company,
        name_key=cr.normalise(company),
        reason_category='Payment default / outstanding',
        reason_detail='Outstanding, unresolved.',
        recommended_by=ADMIN, recommended_at=datetime.utcnow(),
        approved_by=ADMIN, approved_at=datetime.utcnow(),
        scope='entity')
    db.session.add(row)
    db.session.commit()
    cr.cache_clear()
    return row


def _client(code=OWNER, role='user'):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['emp_code'] = code
        s['role'] = role
    return c


# ── §7.13 and the flag ───────────────────────────────────────────────
def test_with_the_flag_off_the_new_screen_does_not_exist():
    """§7: flag-off users get exactly today's screen. The old
    pop-up lives in templates/app.html, which this release does not
    touch at all."""
    with flask_app.app_context():
        account = _account()
        lead = _lead(account=account)
        db.session.commit()
        lead_id = lead.id
        _flag(lf.FLAG, False)
    assert _client().get(f'/leads/{lead_id}/panel').status_code == 404
    assert _client().get('/leads/new').status_code == 404
    assert _client().post(f'/api/leads/{lead_id}/stage',
                          json={'to': 'Contacted'}).status_code == 404


def test_the_new_screen_lives_entirely_in_its_own_templates():
    """The promise the flag makes, held as a test: none of the new
    screen's markup, classes or routes is in the old pop-up, so a
    person without the flag cannot be served a half-converted
    page."""
    old = open(os.path.join(_ROOT, 'templates', 'app.html')).read()
    for marker in ('lf-page', 'lf-strip', 'lf-todo', 'leads/new',
                   'lead_v2', 'guided_help.js'):
        assert marker not in old, marker
    new = open(os.path.join(_ROOT, 'templates', 'lead_v2',
                            'panel.html')).read()
    assert 'lf-strip' in new and 'What to do now' in new


def test_the_flag_is_off_by_default():
    from app.services import flags
    assert flags.DEFAULTS[lf.FLAG] is False


# ── §7.5 — the Quote tab appears from RFQ ────────────────────────────
def test_the_quote_tab_is_hidden_at_new():
    """§7.5: at New stage the Quote tab is hidden."""
    with flask_app.app_context():
        lead = _lead(account=_account(), stage='New')
        db.session.commit()
        assert lf.quote_visible(lead) is False
        assert 'quote' not in lf.tabs_for(lead)


def test_the_quote_tab_is_visible_at_rfq():
    """§7.5: at RFQ stage the Quote tab is visible."""
    with flask_app.app_context():
        lead = _lead(account=_account(), stage='RFQ Generated')
        db.session.commit()
        assert lf.quote_visible(lead) is True
        assert 'quote' in lf.tabs_for(lead)


def test_the_quote_tab_is_drawn_only_when_it_is_visible():
    with flask_app.app_context():
        account = _account()
        early = _lead(account=account, stage='New')
        late = _lead(account=account, stage='Quoted',
                     quote_no='Q-1', quote_value_num=10)
        db.session.commit()
        early_id, late_id = early.id, late.id
    assert 'id="t-quote"' not in _client().get(
        f'/leads/{early_id}/panel').get_data(as_text=True)
    assert 'id="t-quote"' in _client().get(
        f'/leads/{late_id}/panel').get_data(as_text=True)


# ── §7.6 — rate sourcing shows suppliers only ────────────────────────
def test_rate_sourcing_never_shows_the_customers_own_mail():
    """§7.6: the customer's RFQ mail must never appear under Rate
    sourcing. They come from two different tables, which is the
    fix — the old screen read one list for both."""
    with flask_app.app_context():
        account = _account()
        lead = _lead(account=account)
        db.session.add(LeadEmail(lead_id=lead.id,
                                 subject='Our RFQ for 40 MVA',
                                 from_addr='buyer@hitachi-test.com',
                                 direction='in'))
        db.session.commit()
        assert lf.rates_of(lead) == []
        assert len(lf.emails_of(lead)) == 1
        body = _client().get(
            f'/leads/{lead.id}/panel').get_data(as_text=True)
        rates = body.split('id="t-rates"')[1].split('</section>')[0]
        assert 'Our RFQ for 40 MVA' not in rates
        assert 'No rates asked yet' in rates


# ── §7.7 — a stage move that cannot happen explains itself ───────────
def test_moving_to_quoted_without_a_quote_number_is_guided():
    """§7.7: the refusal names the missing fields and offers
    [Take me there]."""
    with flask_app.app_context():
        account = _account()
        lead = _ready_for_rfq(account)
        db.session.commit()
        lead_id = lead.id
    r = _client().post(f'/api/leads/{lead_id}/stage',
                       json={'to': 'Quoted'})
    assert r.status_code == 400
    body = r.get_json()
    assert body['ok'] is False
    guide = body['guide']
    assert guide['code'] == 'STAGE_FIELDS_MISSING'
    assert guide['steps']
    assert any(a['label'] == 'Take me there'
               for a in guide['actions'])
    assert 'quote_no' in guide['context']['missing']


def test_the_missing_field_knows_which_tab_it_lives_on():
    with flask_app.app_context():
        lead = _ready_for_rfq(_account())
        db.session.commit()
        gaps = lf.missing_for(lead, 'Quoted')
        assert {g['tab'] for g in gaps} == {'quote'}
        assert any(g['field'] == 'quote_no' for g in gaps)


def test_a_complete_lead_moves():
    with flask_app.app_context():
        account = _account()
        lead = _ready_for_rfq(account)
        lead.quote_no = 'Q-2026-001'
        lead.quote_value_num = 2400000
        lead.quote_date = date.today()
        lead.quote_validity_date = date.today() + timedelta(days=30)
        db.session.commit()
        lead_id = lead.id
    r = _client().post(f'/api/leads/{lead_id}/stage',
                       json={'to': 'Quoted'})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).stage == 'Quoted'


def test_a_stage_move_writes_history_and_an_audit_row():
    with flask_app.app_context():
        account = _account()
        lead = _lead(account=account)
        db.session.add(LeadEmail(lead_id=lead.id, subject='x',
                                 direction='in'))
        db.session.commit()
        lead_id = lead.id
    _client().post(f'/api/leads/{lead_id}/stage',
                   json={'to': 'Contacted'})
    with flask_app.app_context():
        from app.models.audit import AuditEvent
        rows = _main.LeadStageHistory.query.filter_by(
            lead_id=lead_id).all()
        assert len(rows) == 1 and rows[0].changed_by == OWNER
        assert AuditEvent.query.filter_by(
            action='lead.stage_moved',
            entity_id=str(lead_id)).first() is not None


def test_going_back_a_stage_asks_for_nothing():
    """Correcting a stage somebody overshot must not demand the
    next stage's fields."""
    with flask_app.app_context():
        lead = _lead(account=_account(), stage='Quoted')
        db.session.commit()
        ok, gaps = lf.may_move(lead, 'Contacted')
        assert ok is True and gaps == []


# ── §7.8 — On Hold is a management decision ──────────────────────────
def test_a_user_cannot_put_a_lead_on_hold():
    """§7.8: HOLD_NOT_ALLOWED, with [Request hold from {head}]."""
    with flask_app.app_context():
        lead = _lead(account=_account())
        db.session.commit()
        lead_id = lead.id
    r = _client().post(f'/api/leads/{lead_id}/stage',
                       json={'to': 'On Hold'})
    assert r.status_code == 403
    guide = r.get_json()['guide']
    assert guide['code'] == 'HOLD_NOT_ALLOWED'
    assert guide['context']['head'] == HEAD
    assert any(a['action'] == 'mail' for a in guide['actions'])
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).stage == 'New'


def test_the_vertical_head_is_not_stopped():
    with flask_app.app_context():
        lead = _lead(account=_account(), owner=HEAD)
        db.session.commit()
        lead_id = lead.id
    r = _client(HEAD, 'manager').post(f'/api/leads/{lead_id}/stage',
                                      json={'to': 'On Hold'})
    # On Hold is not on the strip, so the move is refused as a
    # stage — but not by the permission, which is what is tested.
    assert r.status_code != 403


# ── §7.9 — no "New Opportunity", no vendor name, intake hidden ───────
def test_new_opportunity_is_not_a_stage_on_the_strip():
    """§7.9: no "New Opportunity" anywhere the user sees."""
    with flask_app.app_context():
        lead = _lead(account=_account(), stage='New Opportunity')
        db.session.commit()
        lead_id = lead.id
        assert lf.strip_stage(lead) == 'New'
    body = _client().get(f'/leads/{lead_id}/panel').get_data(
        as_text=True)
    assert 'New Opportunity' not in body
    assert 'New Opportunity' not in str(lf.STRIP)


def test_no_ai_vendor_name_is_on_the_screen():
    """§7.9: no AI provider or model name anywhere a user reads."""
    with flask_app.app_context():
        lead = _lead(account=_account())
        db.session.commit()
        lead_id = lead.id
    body = _client().get(
        f'/leads/{lead_id}/panel').get_data(as_text=True).lower()
    for word in ('openai', 'anthropic', 'claude', 'gpt', 'gemini',
                 'llm', 'chatgpt'):
        assert word not in body, word


def test_the_intake_decision_is_admin_only():
    """§7.9: intake scoring visible to Admin only."""
    with flask_app.app_context():
        lead = _lead(account=_account(), classification='A_new_lead',
                     vertical_reason='the words say heavy transport')
        db.session.commit()
        lead_id = lead.id
        assert lf.panel(lead, is_admin=False)['intake'] is None
        assert lf.panel(lead, is_admin=True)['intake'] is not None
    user = _client().get(f'/leads/{lead_id}/panel').get_data(
        as_text=True)
    boss = _client(ADMIN, 'admin').get(
        f'/leads/{lead_id}/panel').get_data(as_text=True)
    assert 'Intake decision' not in user
    assert 'Intake decision' in boss


def test_the_score_text_is_never_shown_to_a_user():
    with flask_app.app_context():
        lead = _lead(account=_account())
        db.session.commit()
        lead_id = lead.id
    body = _client().get(f'/leads/{lead_id}/panel').get_data(
        as_text=True)
    assert 'decided by step_' not in body
    assert 'Score:' not in body


# ── §7.10 — the header subtitle ──────────────────────────────────────
def test_the_header_does_not_say_recommend_a_block():
    """§7.10: that sentence was the subtitle of every lead. It is
    an action, and it lives under More."""
    with flask_app.app_context():
        lead = _lead(account=_account())
        db.session.commit()
        lead_id = lead.id
    body = _client().get(f'/leads/{lead_id}/panel').get_data(
        as_text=True)
    head = body.split('lf-strip')[0]
    assert 'Recommend a block' not in head
    assert 'Recommend a block or caution' in body     # under More


def test_the_closing_actions_are_under_more():
    with flask_app.app_context():
        lead = _lead(account=_account())
        db.session.commit()
        lead_id = lead.id
    body = _client().get(f'/leads/{lead_id}/panel').get_data(
        as_text=True)
    menu = body.split('lf-menu')[1].split('</details>')[0]
    for label in ('Close as lost', 'Mark duplicate',
                  'Delete – not relevant', 'Change account',
                  'Change owner'):
        assert label in menu, label
    strip = body.split('lf-strip')[1].split('</div>')[0]
    for label in ('Lost', 'On Hold', 'Not Interested'):
        assert f'>{label}<' not in strip


# ── the header, the strip and the card ───────────────────────────────
def test_the_header_carries_what_the_brief_asks_for():
    with flask_app.app_context():
        account = _account()
        lead = _lead(account=account,
                     followup_date=date.today() + timedelta(days=3))
        db.session.commit()
        lead_id = lead.id
    body = _client().get(f'/leads/{lead_id}/panel').get_data(
        as_text=True)
    assert PREFIX + 'Hitachi Energy India Limited' in body
    assert f'#{lead_id}' in body
    assert 'Lead Owner' in body
    assert VERTICAL in body
    assert 'lf-green' in body


@pytest.mark.parametrize('days,colour', [(-1, 'red'), (0, 'orange'),
                                         (1, 'orange'), (5, 'green')])
def test_the_due_colour_follows_the_agreed_rule(days, colour):
    with flask_app.app_context():
        lead = _lead(account=_account(),
                     followup_date=date.today() + timedelta(days=days))
        db.session.commit()
        assert lf.due_of(lead)[2] == colour


def test_a_lead_with_no_due_date_gets_no_colour():
    with flask_app.app_context():
        lead = _lead(account=_account())
        db.session.commit()
        assert lf.due_of(lead) == (None, None, '')


def test_the_strip_shows_six_stages_and_marks_where_it_is():
    with flask_app.app_context():
        lead = _lead(account=_account(), stage='Quoted')
        db.session.commit()
        steps = lf.panel(lead)['steps']
        assert [s['name'] for s in steps] == list(lf.STRIP)
        assert [s['state'] for s in steps] == [
            'done', 'done', 'done', 'here', 'ahead', 'ahead']


def test_what_to_do_now_is_one_sentence_and_at_most_two_buttons():
    with flask_app.app_context():
        account = _account()
        for stage in ('New', 'Business Discussion', 'RFQ Generated',
                      'Quoted', 'Under Negotiation', 'Won'):
            lead = _lead(account=account, stage=stage)
            db.session.flush()
            card = lf.what_to_do(lead)
            assert card['text'].strip()
            assert card['text'].count('.') <= 3
            assert len(card['actions']) <= 2


def test_a_closed_lead_is_told_it_is_closed():
    with flask_app.app_context():
        lead = _lead(account=_account(), stage='Lost')
        db.session.commit()
        card = lf.what_to_do(lead)
        assert 'closed as Lost' in card['text']
        assert card['actions'] == []


def test_the_detailed_stages_become_contacteds_sub_status():
    with flask_app.app_context():
        for old in ('Call Done', 'Profile Sent', 'Business Discussion',
                    'Appointment', 'Visit Done'):
            lead = _lead(account=_account(), stage=old)
            db.session.flush()
            assert lf.strip_stage(lead) == 'Contacted'


def test_the_sub_status_can_be_set_from_the_screen():
    with flask_app.app_context():
        lead = _lead(account=_account(), stage='Call Done')
        db.session.commit()
        lead_id = lead.id
    r = _client().post(f'/api/leads/{lead_id}/substatus',
                       json={'sub_status': 'Visit done'})
    assert r.status_code == 200
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).sub_status == 'Visit done'


def test_an_invented_sub_status_is_refused():
    with flask_app.app_context():
        lead = _lead(account=_account(), stage='Call Done')
        db.session.commit()
        lead_id = lead.id
    r = _client().post(f'/api/leads/{lead_id}/substatus',
                       json={'sub_status': 'Had a chat'})
    assert r.status_code == 400


# ── the history timeline (§3.2) ──────────────────────────────────────
def test_the_history_is_filled_from_what_happened():
    """Not four empty date boxes for somebody to type into."""
    with flask_app.app_context():
        account = _account()
        lead = _lead(account=account)
        db.session.add(LeadEmail(lead_id=lead.id, subject='their RFQ',
                                 direction='in',
                                 sent_or_received_at=datetime.utcnow()))
        db.session.add(_main.LeadStageHistory(
            lead_id=lead.id, from_stage='New', to_stage='Contacted',
            changed_at=datetime.utcnow(), changed_by=OWNER))
        db.session.commit()
        rows = lf.history_of(lead)
        kinds = {r['kind'] for r in rows}
        assert 'stage' in kinds
        assert any(k.startswith('mail') for k in kinds)


# ── who may change what ──────────────────────────────────────────────
def test_somebody_elses_lead_is_readable_but_not_changeable():
    """§5's NOT_YOUR_LEAD: who owns it, and how to ask."""
    with flask_app.app_context():
        lead = _lead(account=_account(), owner=OWNER)
        db.session.commit()
        lead_id = lead.id
    r = _client(ADMIN, 'admin').post(f'/api/leads/{lead_id}/stage',
                                     json={'to': 'Contacted'})
    assert r.status_code != 403, 'an admin may'
    r = _client(OTHER, 'user').post(f'/api/leads/{lead_id}/stage',
                                    json={'to': 'Contacted'})
    assert r.status_code in (403, 404)
    if r.status_code == 403:
        assert r.get_json()['guide']['code'] == 'NOT_YOUR_LEAD'


def test_signing_out_is_explained_rather_than_just_refused():
    r = flask_app.test_client().get('/api/leads/1/panel')
    assert r.status_code == 401
    assert r.get_json()['guide']['code'] == 'SESSION_EXPIRED'


# ── the banner, once ─────────────────────────────────────────────────
def test_the_what_has_changed_banner_shows_once_per_lead_owner():
    with flask_app.app_context():
        lead = _lead(account=_account())
        db.session.commit()
        lead_id = lead.id
    body = _client().get(f'/leads/{lead_id}/panel').get_data(
        as_text=True)
    assert 'The lead screen has changed' in body
    _client().post(f'/api/leads/{lead_id}/seen')
    again = _client().get(f'/leads/{lead_id}/panel').get_data(
        as_text=True)
    assert 'The lead screen has changed' not in again


def test_the_dismissal_is_remembered_in_the_database():
    """A person uses two machines; browser storage would tell them
    twice."""
    with flask_app.app_context():
        lead = _lead(account=_account())
        db.session.commit()
        lead_id = lead.id
    _client().post(f'/api/leads/{lead_id}/seen')
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).popup_seen_by is True


# ── §4 — the two-step new lead ───────────────────────────────────────
def test_the_new_lead_page_renders_both_steps():
    r = _client().get('/leads/new')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'Step 1 — Who' in body
    assert 'Step 2 — What' in body


def test_the_account_search_needs_three_letters():
    with flask_app.app_context():
        _account('Hitachi Energy India Limited')
        db.session.commit()
    assert _client().get(
        '/api/leads/new/accounts?q=LF').get_json()['rows'] == []
    rows = _client().get(
        '/api/leads/new/accounts?q=Hitachi').get_json()['rows']
    assert rows and rows[0]['name'].startswith(PREFIX)


def test_a_blocked_account_is_shown_marked_rather_than_hidden():
    """Somebody searching for it needs to be told why they cannot
    have it."""
    with flask_app.app_context():
        account = _account('Walchandnagar Industries')
        db.session.flush()
        _block(account.name)
    rows = _client().get(
        '/api/leads/new/accounts?q=Walchandnagar').get_json()['rows']
    assert rows and rows[0]['blocked'] is True


def test_creating_a_lead_for_a_blocked_client_is_guided():
    """§7.2's manual half: refuse with the reason and the route to
    a review."""
    with flask_app.app_context():
        account = _account('Godrej Enterprises Group')
        db.session.flush()
        _block(account.name)
        account_id = account.id
    r = _client().post('/api/leads/new',
                       json={'account_id': account_id,
                             'requirement': 'a move'})
    assert r.status_code == 403
    guide = r.get_json()['guide']
    assert guide['code'] == 'CLIENT_BLOCKED'
    assert guide['steps']
    with flask_app.app_context():
        assert Lead.query.filter_by(
            company_id=account_id).count() == 0


def test_creating_without_an_account_is_guided():
    r = _client().post('/api/leads/new', json={'requirement': 'x'})
    assert r.status_code == 400
    assert r.get_json()['guide']['code'] == 'NO_ACCOUNT'


def test_creating_without_a_requirement_is_refused_in_words():
    with flask_app.app_context():
        account = _account()
        db.session.commit()
        account_id = account.id
    r = _client().post('/api/leads/new',
                       json={'account_id': account_id})
    assert r.status_code == 400
    assert 'what the client wants' in r.get_json()['error']


def test_a_created_lead_starts_at_new_with_an_owner():
    """§7.4's manual half: never a blank owner."""
    with flask_app.app_context():
        account = _account()
        db.session.commit()
        account_id = account.id
    body = _client().post('/api/leads/new',
                          json={'account_id': account_id,
                                'requirement': '2 x 40 MVA to Lapanga',
                                'vertical': VERTICAL}).get_json()
    assert body['ok'] is True
    assert body['stage'] == 'New'
    assert body['owner']
    with flask_app.app_context():
        lead = db.session.get(Lead, body['lead_id'])
        assert lead.assigned_to
        assert lead.project == '2 x 40 MVA to Lapanga'


def test_an_open_lead_for_the_same_customer_is_shown_first():
    with flask_app.app_context():
        account = _account()
        existing = _lead(account=account)
        db.session.commit()
        account_id, existing_id = account.id, existing.id
    rows = _client().get(
        f'/api/leads/new/open?account_id={account_id}').get_json()['rows']
    assert [r['lead_id'] for r in rows] == [existing_id]


# ── both widths are designed, not one plus a sideways scroll ─────────
def test_the_phone_layout_is_in_the_stylesheet():
    """The RFQ screens shipped at 536px on a 390px phone because
    nobody measured. The rules are asserted here so the suite needs
    no browser; scripts/lead_flow_shots.py measures it for real."""
    css = open(os.path.join(_ROOT, 'static', 'css',
                            'crm.css')).read()
    section = css[css.index('The lead screen (CRM-LEAD-FLOW-01'):]
    assert '@media (max-width:620px)' in section
    flat = section.replace('\n', '').replace('  ', '')
    for rule in ('.lf-kv{flex-direction:column',
                 '.lf-t{display:block;overflow-x:auto',
                 '.lf-foot .btn{width:100%'):
        assert rule in flat, rule


def test_the_shared_top_bar_is_fixed_for_this_page_only():
    """Scoped, like .rfq-page's: the bar is on every screen in the
    CRM and changing it centrally would move all of them."""
    css = open(os.path.join(_ROOT, 'static', 'css',
                            'crm.css')).read()
    assert '.lf-page .crm-top{height:auto' in css.replace('\n', '')
    assert '.crm-top{\n  background' in css, 'the shared rule stands'


def test_every_colour_on_the_lead_screen_is_a_token():
    import re
    css = open(os.path.join(_ROOT, 'static', 'css',
                            'crm.css')).read()
    section = css[css.index('The lead screen (CRM-LEAD-FLOW-01'):]
    literals = [h for h in re.findall(r'#[0-9A-Fa-f]{3,8}\b', section)
                if h.lower() not in ('#fff', '#ffffff')]
    assert not literals, literals
