"""The RFQ desk could not put a name on a lead.

CRM-MAIL-02. An air-freight RFQ from Fox Brasil, on an account whose
default vertical is Heavy Transport. The desk set the owner and a
secondary PIC; the save was refused, the lead kept reading "nobody is
accountable", and the refusal talked about a vertical nobody had
mentioned.

Three faults, and they compound:

  1 the save. A stage posted back unchanged read as a stage move,
    because the comparison was a string one and the form posts
    whatever the pipeline widget was rendered with. Every gate then
    ran on a save that moved nothing.

  2 the people. `assigned_to` and `secondary_owner` were read only
    `if admin.triage`, and dropped in SILENCE otherwise — so the one
    action that gets a lead out of "nobody is accountable" failed
    without saying anything, for everybody but an administrator.

  3 the vertical. The gate asked `vertical_heads.vertical_of`, which
    falls back to the ACCOUNT's vertical, so an Air lead was refused
    for having no owner in Heavy Transport. And a vertical being set
    in the same save was not considered at all.
"""
import importlib
import os
import sys
import tempfile
from datetime import datetime

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'mail02-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Mail02-123456789')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'mail02.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.assignment')
flask_app = _main.app
db = _main.db
Lead, Company, Employee = _main.Lead, _main.Company, _main.Employee

from app.models.access import AccessProfile, DataScope        # noqa: E402
from app.models.assignment import AccountVerticalOwner        # noqa: E402
from app.models.email_admin import VerticalHead               # noqa: E402
from app.services import (account_gate, admin_settings,       # noqa: E402
                          flags, lead_people, vertical_heads)

#: The two verticals in the complaint: the lead is Air, the account's
#: own default is Heavy Transport.
AIR = 'Air Freight'
HEAVY = 'Heavy Transport'
SEA = 'Sea Freight'

DESK = 'ZF_DESK'          # the RFQ desk
AIR_HEAD = 'ZF_AIRHEAD'   # head of Air Freight
SEA_HEAD = 'ZF_SEAHEAD'   # the sea freight head, who is to own the lead
AIR_USER = 'ZF_AIRUSER'   # the air freight user, who is to be second PIC
HEAVY_HEAD = 'ZF_HVYHEAD'
STRANGER = 'ZF_STRANGER'
TRIAGE = 'ZF_TRIAGE'


def _purge():
    from app.models.audit import AuditEvent

    ids = [l.id for l in Lead.query.filter(Lead.company.like('ZF%')).all()]
    if ids:
        AuditEvent.query.filter(
            AuditEvent.entity_type == 'lead',
            AuditEvent.entity_id.in_([str(i) for i in ids])).delete(
                synchronize_session=False)
        try:
            from app import LeadAssignmentHistory
            LeadAssignmentHistory.query.filter(
                LeadAssignmentHistory.lead_id.in_(ids)).delete(
                    synchronize_session=False)
        except Exception:                                 # noqa: BLE001
            db.session.rollback()
    Lead.query.filter(Lead.company.like('ZF%')).delete(
        synchronize_session=False)
    account_ids = [c.id for c in
                   Company.query.filter(Company.name.like('ZF%')).all()]
    if account_ids:
        AccountVerticalOwner.query.filter(
            AccountVerticalOwner.account_id.in_(account_ids)).delete(
                synchronize_session=False)
    Company.query.filter(Company.name.like('ZF%')).delete(
        synchronize_session=False)
    AccessProfile.query.filter(AccessProfile.emp_code.like('ZF_%')).delete(
        synchronize_session=False)
    Employee.query.filter(Employee.emp_code.like('ZF_%')).delete(
        synchronize_session=False)
    VerticalHead.query.filter(VerticalHead.vertical.in_(
        (AIR, HEAVY, SEA))).delete(synchronize_session=False)
    from app.models.integration import AppSetting
    AppSetting.query.filter(AppSetting.key.in_((
        'flag.FEATURE_ACCOUNT_FIRST', 'rfq_coordinator'))).delete(
            synchronize_session=False)
    db.session.commit()
    flags.cache_clear()
    vertical_heads.cache_clear()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code, vertical in ((DESK, HEAVY), (AIR_HEAD, AIR),
                               (SEA_HEAD, SEA), (AIR_USER, AIR),
                               (HEAVY_HEAD, HEAVY), (STRANGER, HEAVY),
                               (TRIAGE, HEAVY)):
            db.session.add(Employee(
                emp_code=code, name=code.title(), password_hash='x',
                email=f'{code.lower()}@procamlogistics.com', role='user',
                vertical=vertical, is_active=True, must_change_pw=False))
        db.session.add(VerticalHead(vertical=AIR, head_emp_code=AIR_HEAD,
                                    active=True))
        db.session.add(VerticalHead(vertical=HEAVY,
                                    head_emp_code=HEAVY_HEAD, active=True))
        db.session.add(AccessProfile(emp_code=TRIAGE,
                                     data_scope=DataScope.ALL,
                                     perms=['admin.triage']))
        db.session.commit()
        admin_settings.save('rfq_coordinator', DESK, actor='test')
        vertical_heads.cache_clear()
        _flag(True)
        yield
        _purge()


def _flag(state=True):
    import json

    from app.models.integration import AppSetting
    row = AppSetting.query.filter_by(
        key='flag.FEATURE_ACCOUNT_FIRST').first()
    if row is None:
        row = AppSetting(key='flag.FEATURE_ACCOUNT_FIRST')
        db.session.add(row)
    row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


def _account(name='ZF Fox Brasil Ltda', *, vertical=HEAVY,
             owner_vertical=HEAVY, owner=HEAVY_HEAD):
    account = Company(name=name, is_active=True, vertical=vertical,
                      email_domains=['zffoxbrasil.example'])
    db.session.add(account)
    db.session.flush()
    if owner:
        db.session.add(AccountVerticalOwner(
            account_id=account.id, vertical=owner_vertical,
            emp_code=owner, source='manual'))
    db.session.commit()
    return account


def _lead(**kw):
    kw.setdefault('company', 'ZF Fox Brasil')
    kw.setdefault('stage', 'New Opportunity')
    kw.setdefault('procam_vertical', AIR)
    kw.setdefault('assigned_to', None)
    kw.setdefault('secondary_owner', None)
    kw.setdefault('email', 'exports@zffoxbrasil.example')
    kw.setdefault('created_at', datetime.utcnow())
    lead = Lead(**kw)
    db.session.add(lead)
    db.session.commit()
    return lead


def _the_case():
    """The lead as reported: Air enquiry, Heavy Transport account,
    nobody accountable."""
    return _lead(company_id=_account().id)


def _client(code):
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = code
        s['role'] = 'user'
    return client


def _token(client):
    r = client.get('/my-work')
    for header in r.headers.getlist('Set-Cookie'):
        if header.startswith('csrf_token='):
            return header.split('=', 1)[1].split(';')[0]
    return ''


# ═══════════════════════════════════════════════════════════════════
# 3 — the gate reads the lead's vertical, not the account's
# ═══════════════════════════════════════════════════════════════════
def test_the_gate_reads_the_leads_own_vertical():
    with flask_app.app_context():
        lead = _the_case()
        assert account_gate.vertical_of(lead) == AIR
        code, why = account_gate.reason_for(lead)
        assert code == account_gate.WHY_NO_OWNER
        # The sentence names Air Freight, not the account's default.
        assert AIR in why
        assert HEAVY not in why


def test_the_refusal_no_longer_names_the_accounts_vertical():
    with flask_app.app_context():
        lead = _the_case()
        refusal = account_gate.check(lead, 'Quoted', emp_code=STRANGER)
        assert refusal is not None
        assert AIR in refusal
        assert HEAVY not in refusal


def test_an_owner_for_the_leads_vertical_is_what_counts():
    with flask_app.app_context():
        lead = _lead(company_id=_account(owner_vertical=AIR,
                                         owner=AIR_USER).id)
        assert account_gate.reason_for(lead)[0] == ''
        assert account_gate.check(lead, 'Quoted',
                                  emp_code=STRANGER) is None


def test_a_lead_with_no_vertical_says_so_rather_than_guessing():
    """It used to borrow the account's, then fold a blank onto
    Management, so a missing vertical became an owner question about a
    vertical the lead is not in."""
    with flask_app.app_context():
        lead = _lead(procam_vertical=None, company_id=_account().id)
        assert account_gate.vertical_of(lead) == ''
        code, why = account_gate.reason_for(lead)
        assert code == account_gate.WHY_NO_VERTICAL
        assert 'no vertical on the lead' == why


def test_the_vertical_being_set_in_the_same_save_is_the_one_checked():
    with flask_app.app_context():
        # The account is owned for Air and not for Heavy Transport.
        lead = _lead(procam_vertical=HEAVY,
                     company_id=_account(owner_vertical=AIR,
                                         owner=AIR_USER).id)
        # As it stands: no owner for Heavy Transport.
        assert account_gate.reason_for(lead)[0] == \
            account_gate.WHY_NO_OWNER
        # Being moved to Air in this save: there is an owner.
        assert account_gate.reason_for(lead, vertical=AIR)[0] == ''
        assert account_gate.check(lead, 'Quoted', emp_code=STRANGER,
                                  vertical=AIR) is None


def test_the_old_vertical_is_not_held_against_the_move_over_http():
    with flask_app.app_context():
        lead = _lead(procam_vertical=HEAVY, assigned_to=SEA_HEAD,
                     company_id=_account(owner_vertical=AIR,
                                         owner=AIR_USER).id)
        lead_id = lead.id
    client = _client(TRIAGE)
    r = client.put(f'/api/leads/{lead_id}',
                   json={'procam_vertical': AIR,
                         'procam_vertical_was': HEAVY,
                         'stage': 'Quoted',
                         'estimated_value': 250000,
                         'quote_due_at': '2026-11-30',
                         'followup': '2026-11-20'},
                   headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert 'stage_refused' not in body, body
    with flask_app.app_context():
        lead = db.session.get(Lead, lead_id)
        assert lead.procam_vertical == AIR
        assert lead.stage == 'Quoted'


# ═══════════════════════════════════════════════════════════════════
# 2 — the RFQ desk, the vertical head and the admins may set people
# ═══════════════════════════════════════════════════════════════════
def test_the_rfq_desk_may_set_the_people():
    with flask_app.app_context():
        lead = _the_case()
        allowed, why = lead_people.may_set_people(lead, DESK)
        if DESK not in lead_people.desk_codes(AIR):
            pytest.skip('the rfq_coordinator setting is not writable '
                        'in this database')
        assert allowed, why
        assert why == lead_people.AS_DESK


def test_the_head_of_the_leads_vertical_may():
    with flask_app.app_context():
        lead = _the_case()
        allowed, why = lead_people.may_set_people(lead, AIR_HEAD)
        assert allowed, why
        assert why == lead_people.AS_HEAD


def test_the_head_of_the_vertical_it_is_moving_to_may():
    """On this lead those were two different people."""
    with flask_app.app_context():
        lead = _lead(procam_vertical=HEAVY, company_id=_account().id)
        allowed, _why = lead_people.may_set_people(lead, AIR_HEAD,
                                                   vertical=AIR)
        assert allowed


def test_an_administrator_still_may():
    with flask_app.app_context():
        lead = _the_case()
        allowed, why = lead_people.may_set_people(lead, TRIAGE)
        assert allowed
        assert why == lead_people.AS_ADMIN


def test_the_leads_own_owner_may_hand_it_on():
    with flask_app.app_context():
        lead = _lead(assigned_to=SEA_HEAD, company_id=_account().id)
        allowed, why = lead_people.may_set_people(lead, SEA_HEAD)
        assert allowed
        assert why == lead_people.AS_OWNER


def test_the_secondary_pic_may_not():
    """A monitor who can reassign the lead they monitor is not a
    monitor."""
    with flask_app.app_context():
        lead = _lead(assigned_to=SEA_HEAD, secondary_owner=AIR_USER,
                     company_id=_account().id)
        allowed, _why = lead_people.may_set_people(lead, AIR_USER)
        assert not allowed


def test_an_unrelated_user_may_not_and_is_told_why():
    with flask_app.app_context():
        lead = _the_case()
        allowed, why = lead_people.may_set_people(lead, STRANGER)
        assert not allowed
        assert 'RFQ desk' in why
        # It says what DID save, because the rest of the form does.
        assert 'everything else in this save' in why


def test_a_refused_assignment_is_reported_not_swallowed():
    """The whole of \u00a72: it used to be dropped in silence.

    The secondary PIC is used because they can *open* the lead \u2014 a
    monitor who could not would be no monitor \u2014 and may not change
    who it belongs to. Somebody who cannot open it at all is refused
    by the Access Matrix first and proves nothing about this rule.
    """
    with flask_app.app_context():
        lead_id = _lead(assigned_to=SEA_HEAD, secondary_owner=AIR_USER,
                        company_id=_account().id).id
    client = _client(AIR_USER)
    r = client.put(f'/api/leads/{lead_id}',
                   json={'assigned_to': AIR_USER, 'pic': 'Someone'},
                   headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body['ok'] is True
    assert any('RFQ desk' in w for w in (body.get('warnings') or [])), body
    with flask_app.app_context():
        lead = db.session.get(Lead, lead_id)
        # The owner was NOT changed \u2026
        assert lead.assigned_to == SEA_HEAD
        # \u2026 and the rest of the form saved.
        assert lead.pic == 'Someone'


def test_the_desk_assignment_actually_sticks():
    with flask_app.app_context():
        lead_id = _the_case().id
        if DESK not in lead_people.desk_codes(AIR):
            pytest.skip('the rfq_coordinator setting is not writable '
                        'in this database')
    client = _client(DESK)
    r = client.put(f'/api/leads/{lead_id}',
                   json={'assigned_to': SEA_HEAD,
                         'secondary_owner': AIR_USER,
                         'reassignment_reason': 'Specialist required'},
                   headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        lead = db.session.get(Lead, lead_id)
        assert lead.assigned_to == SEA_HEAD
        assert lead.secondary_owner == AIR_USER


def test_the_head_can_set_the_owner_over_http():
    with flask_app.app_context():
        lead_id = _the_case().id
    client = _client(AIR_HEAD)
    r = client.put(f'/api/leads/{lead_id}',
                   json={'assigned_to': SEA_HEAD},
                   headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).assigned_to == SEA_HEAD


def test_the_lead_screen_is_told_who_may():
    with flask_app.app_context():
        lead_id = _the_case().id
    for code, expected in ((AIR_HEAD, True), (STRANGER, False)):
        r = _client(code).get(f'/api/leads/{lead_id}/account-state')
        if r.status_code != 200:
            continue          # the Access Matrix refuses them the lead
        body = r.get_json()
        assert body['can_set_people'] is expected, code
        if not expected:
            assert body['why_set']


# ═══════════════════════════════════════════════════════════════════
# 2 — and whoever can clear the question is not refused by it
# ═══════════════════════════════════════════════════════════════════
def test_the_desk_is_not_refused_by_the_question_it_can_answer():
    with flask_app.app_context():
        lead = _the_case()
        if DESK not in lead_people.desk_codes(AIR):
            pytest.skip('the rfq_coordinator setting is not writable '
                        'in this database')
        assert account_gate.worker_claim(lead, DESK) == \
            account_gate.CLAIM_RESOLVER
        assert account_gate.check(lead, 'Quoted', emp_code=DESK) is None


def test_the_head_of_the_leads_vertical_is_not_refused():
    with flask_app.app_context():
        lead = _the_case()
        assert account_gate.check(lead, 'Quoted',
                                  emp_code=AIR_HEAD) is None


def test_the_head_of_another_vertical_is_refused():
    with flask_app.app_context():
        lead = _the_case()
        assert account_gate.check(lead, 'Quoted',
                                  emp_code=HEAVY_HEAD) is not None


def test_no_account_at_all_is_still_refused_for_everybody():
    """The line the rule exists for. A resolver can clear it; they
    cannot quote through it."""
    with flask_app.app_context():
        lead = _lead(company_id=None)
        for code in (DESK, AIR_HEAD, TRIAGE, STRANGER):
            assert account_gate.check(lead, 'Quoted',
                                      emp_code=code) is not None, code


# ═══════════════════════════════════════════════════════════════════
# 1 — saving is never gated
# ═══════════════════════════════════════════════════════════════════
def test_a_stage_posted_back_unchanged_is_not_a_move():
    """The form posts whatever the pipeline widget was rendered with.
    An unchanged stage pulled every gate into the save."""
    with flask_app.app_context():
        lead_id = _lead(stage='New Opportunity',
                        company_id=None).id
    client = _client(TRIAGE)
    r = client.put(f'/api/leads/{lead_id}',
                   json={'stage': 'New Opportunity', 'pic': 'Nicole'},
                   headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert 'stage_refused' not in body
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).pic == 'Nicole'


def test_a_retired_stage_name_is_not_a_move_either():
    """'Profile Sent' is stored; the widget posts 'Call Done', which
    is the same stage under the name it was retired into."""
    from app.services import stage_map

    with flask_app.app_context():
        assert stage_map.canonical('Profile Sent') == 'Call Done'
        lead_id = _lead(stage='Profile Sent', company_id=None).id
    client = _client(TRIAGE)
    r = client.put(f'/api/leads/{lead_id}',
                   json={'stage': 'Call Done', 'pic': 'Nicole'},
                   headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert 'stage_refused' not in r.get_json()
    with flask_app.app_context():
        lead = db.session.get(Lead, lead_id)
        assert lead.pic == 'Nicole'
        # And the stored name is left exactly as it was: nothing
        # moved, so nothing is rewritten.
        assert lead.stage == 'Profile Sent'


def test_everything_but_the_stage_saves_on_a_no_account_lead():
    with flask_app.app_context():
        lead_id = _lead(company_id=None, assigned_to=SEA_HEAD).id
    client = _client(TRIAGE)
    r = client.put(f'/api/leads/{lead_id}',
                   json={'stage': 'Quoted',
                         'procam_vertical': AIR,
                         'procam_vertical_was': AIR,
                         'assigned_to': SEA_HEAD,
                         'secondary_owner': AIR_USER,
                         'pic': 'Nicole',
                         'phone': '+55 11 5555 0000',
                         'rfq_date': '2026-11-10',
                         'estimated_value': 300000},
                   headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body['needs'] == 'account'
    with flask_app.app_context():
        lead = db.session.get(Lead, lead_id)
        assert lead.pic == 'Nicole'
        assert lead.phone == '+55 11 5555 0000'
        assert str(lead.rfq_date) == '2026-11-10'
        assert lead.secondary_owner == AIR_USER
        assert lead.stage != 'Quoted'


def test_the_fox_brasil_script_is_read_only_until_apply():
    import importlib.util

    path = os.path.join(_ROOT, 'scripts', '2026_11_12_fox_brasil.py')
    source = open(path).read()
    spec = importlib.util.spec_from_file_location('zf_fox', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # No employee code or name is written down in it.
    for forbidden in ('ZF_', 'EMP', 'DIR1'):
        assert forbidden not in source.replace('EMPTY', '')
    with flask_app.app_context():
        lead = _the_case()
        mod.show_lead(lead)               # must not raise
        mod.show_accounts('zf fox brasil')
        counts = mod.states()
        assert counts['open'] >= 1
        assert 'inactive_owner' in counts
        assert db.session.get(Lead, lead.id).assigned_to in (None, '')


def test_the_script_refuses_an_inactive_person():
    import importlib.util

    path = os.path.join(_ROOT, 'scripts', '2026_11_12_fox_brasil.py')
    spec = importlib.util.spec_from_file_location('zf_fox2', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with flask_app.app_context():
        emp = Employee.query.filter_by(emp_code=SEA_HEAD).first()
        emp.is_active = False
        db.session.commit()
        lead = _the_case()
        with pytest.raises(SystemExit):
            mod.apply(lead, owner=SEA_HEAD)
        assert db.session.get(Lead, lead.id).assigned_to in (None, '')


def test_the_script_applies_the_four_changes_together():
    import importlib.util

    path = os.path.join(_ROOT, 'scripts', '2026_11_12_fox_brasil.py')
    spec = importlib.util.spec_from_file_location('zf_fox3', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with flask_app.app_context():
        right = _account(name='ZF Fox Brasil Transportes',
                         vertical=AIR, owner_vertical=AIR,
                         owner=AIR_USER)
        lead = _lead(procam_vertical=HEAVY, company_id=_account().id)
        mod.apply(lead, account_id=right.id, vertical=AIR,
                  owner=SEA_HEAD, second=AIR_USER)
        db.session.expire_all()
        lead = db.session.get(Lead, lead.id)
        assert lead.company_id == right.id
        assert lead.procam_vertical == AIR
        assert lead.assigned_to == SEA_HEAD
        assert lead.secondary_owner == AIR_USER
        # and the gate is satisfied afterwards
        assert account_gate.reason_for(lead)[0] == ''
