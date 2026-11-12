"""Account ownership must not block lead work.

CRM-WA-01b. A Freight Forwarding lead was refused at Quoted because
the account it is linked to is owned by an Air Freight colleague.
Nobody disputed the account or the owner — the rule simply asked the
wrong person to settle a staffing question.

What these tests hold in place

    The exemption is narrow. It applies to one reason only — nobody
    named on the account for the lead's vertical — and only for
    somebody with a claim on the lead. A lead with no account at all
    is still refused, for everybody, because nobody can quote a
    customer we have not identified. That is the line the original
    rule was drawn for and it stays.

    The refusal names the rule and who can clear it. The sentence the
    sales manager saw named neither, which is why a one-line staffing
    gap took a day to identify.
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
os.environ.setdefault('SECRET_KEY', 'wa01b-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Wa01b-123456789')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'wa01b.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.assignment')
flask_app = _main.app
db = _main.db
Lead, Company, Employee = _main.Lead, _main.Company, _main.Employee

from app.models.assignment import AccountVerticalOwner       # noqa: E402
from app.models.email_admin import VerticalHead               # noqa: E402
from app.services import (account_gate, flags,                # noqa: E402
                          vertical_heads)

#: The lead's vertical, and a different one for the account's owner —
#: which is the whole shape of the complaint. Role words, no names.
FF = 'Freight Forwarding'
AIR = 'Air Freight'

OWNER, SECOND, COLLEAGUE = 'ZB_OWNER', 'ZB_SECOND', 'ZB_COLL'
AIR_OWNER, STRANGER = 'ZB_AIR', 'ZB_STRANGER'


def _purge():
    Lead.query.filter(Lead.company.like('ZB%')).delete(
        synchronize_session=False)
    ids = [c.id for c in Company.query.filter(Company.name.like('ZB%')).all()]
    if ids:
        AccountVerticalOwner.query.filter(
            AccountVerticalOwner.account_id.in_(ids)).delete(
                synchronize_session=False)
    Company.query.filter(Company.name.like('ZB%')).delete(
        synchronize_session=False)
    Employee.query.filter(Employee.emp_code.like('ZB_%')).delete(
        synchronize_session=False)
    # Both verticals exist in the master, as they do in production.
    # Without that the alias resolver collapses every unmapped name
    # onto Management, and Air Freight and Freight Forwarding read as
    # one vertical — which is a real failure mode, so it has a test of
    # its own rather than being left as the fixture's accident.
    VerticalHead.query.filter(VerticalHead.vertical.in_((FF, AIR))).delete(
        synchronize_session=False)
    # The flag row. One database is shared by the whole suite, so a
    # flag left on here arms the gate for every module after this one.
    from app.models.integration import AppSetting
    AppSetting.query.filter(AppSetting.key.like(
        'flag.FEATURE_ACCOUNT_FIRST%')).delete(synchronize_session=False)
    db.session.commit()
    flags.cache_clear()
    # And the vertical master's own cache. It holds the heads table
    # for thirty seconds, so a module that writes VerticalHead rows
    # and does not clear it leaves the *next* module resolving its
    # verticals against this one's rows — every unlisted name folds
    # onto Management, and a gate asking "who owns this for Heavy
    # Transport" asks about Management instead. That is how
    # test_account_first failed while passing on its own.
    vertical_heads.cache_clear()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code, vertical in ((OWNER, FF), (SECOND, FF), (COLLEAGUE, FF),
                               (AIR_OWNER, AIR), (STRANGER, AIR)):
            db.session.add(Employee(
                emp_code=code, name=code.title(), password_hash='x',
                email=f'{code.lower()}@procamlogistics.com',
                role='user', vertical=vertical, is_active=True))
        db.session.add(VerticalHead(vertical=FF, head_emp_code=OWNER,
                                    active=True))
        db.session.add(VerticalHead(vertical=AIR, head_emp_code=AIR_OWNER,
                                    active=True))
        db.session.commit()
        vertical_heads.cache_clear()
        _on()
        yield
        _purge()


def _on(state=True):
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


def _account(name='ZB Freight Account', *, owner_vertical=None,
             owner=None):
    account = Company(name=name, is_active=True, email_domains=[])
    db.session.add(account)
    db.session.flush()
    if owner:
        db.session.add(AccountVerticalOwner(
            account_id=account.id, vertical=owner_vertical,
            emp_code=owner, source='manual'))
    db.session.commit()
    return account


def _lead(*, account=None, company='ZB Sinocan Freight Logistics',
          vertical=FF, stage='RFQ Generated', owner=OWNER,
          second=None):
    lead = Lead(company=company, stage=stage, procam_vertical=vertical,
                source='email', assigned_to=owner, secondary_owner=second,
                email='buyer@zbfreight.example',
                company_id=account.id if account else None,
                created_at=datetime.utcnow())
    db.session.add(lead)
    db.session.commit()
    return lead


def _the_case():
    """The lead as raised: account owned for another vertical."""
    account = _account(owner_vertical=AIR, owner=AIR_OWNER)
    return _lead(account=account)


# ── the complaint ────────────────────────────────────────────────────
def test_the_reported_lead_was_refused_for_a_staffing_reason():
    """Not a name mismatch and not a missing account — no owner."""
    with flask_app.app_context():
        lead = _the_case()
        code, why = account_gate.reason_for(lead)
        assert code == account_gate.WHY_NO_OWNER
        assert FF in why
        assert code in account_gate.OWNERSHIP_REASONS


def test_an_account_owned_for_another_vertical_counts_as_no_owner():
    with flask_app.app_context():
        lead = _the_case()
        account = account_gate.account_of(lead)
        # Owned, plainly — just not for this lead's vertical.
        assert account_gate.owner_for(account, AIR) == AIR_OWNER
        assert account_gate.owner_for(account, FF) == ''


# ── the fix ──────────────────────────────────────────────────────────
def test_the_lead_owner_can_now_mark_it_quoted():
    with flask_app.app_context():
        lead = _the_case()
        assert account_gate.check(lead, 'Quoted', emp_code=OWNER) is None


def test_the_secondary_pic_can_too():
    with flask_app.app_context():
        account = _account(owner_vertical=AIR, owner=AIR_OWNER)
        lead = _lead(account=account, second=SECOND)
        assert account_gate.check(lead, 'Quoted', emp_code=SECOND) is None


def test_anybody_in_the_leads_own_vertical_can_too():
    """The manager who raised it need not be the named owner."""
    with flask_app.app_context():
        lead = _the_case()
        assert account_gate.check(lead, 'Quoted',
                                  emp_code=COLLEAGUE) is None


def test_the_account_owner_in_another_vertical_is_still_not_blocked():
    """They own the account; nothing about that was ever the problem."""
    with flask_app.app_context():
        lead = _the_case()
        # No claim on the *lead*, so the staffing gap still stops them
        # — and the message tells them who to ask.
        refusal = account_gate.check(lead, 'Quoted', emp_code=AIR_OWNER)
        assert refusal is not None


def test_an_unrelated_user_is_still_refused():
    with flask_app.app_context():
        lead = _the_case()
        refusal = account_gate.check(lead, 'Quoted', emp_code=STRANGER)
        assert refusal is not None
        assert account_gate.worker_claim(lead, STRANGER) == ''


@pytest.mark.parametrize('stage', [
    'Call Done', 'Business Discussion', 'New Opportunity',
    'RFQ Generated', 'Quoted', 'Under Negotiation', 'Won'])
def test_the_exemption_covers_every_blocked_stage_not_just_quoted(stage):
    """A rule fixed at one door is a rule people route around."""
    with flask_app.app_context():
        lead = _the_case()
        assert account_gate.check(lead, stage, emp_code=OWNER) is None
        assert account_gate.check(lead, stage, emp_code=STRANGER) is not None


# ── the line that stays ──────────────────────────────────────────────
def test_a_lead_with_no_account_is_still_refused_for_its_own_owner():
    """The original rule. Nobody can quote an unidentified customer."""
    with flask_app.app_context():
        lead = _lead(account=None)
        refusal = account_gate.check(lead, 'Quoted', emp_code=OWNER)
        assert refusal is not None
        assert account_gate.reason_for(lead)[0] == \
            account_gate.WHY_NO_ACCOUNT


def test_a_placeholder_account_is_still_refused_for_its_own_owner():
    with flask_app.app_context():
        account = _account(name='ZB Unknown')
        account.name = 'Unknown'
        db.session.commit()
        lead = _lead(account=account)
        try:
            assert account_gate.reason_for(lead)[0] == \
                account_gate.WHY_PLACEHOLDER
            assert account_gate.check(lead, 'Quoted',
                                      emp_code=OWNER) is not None
        finally:
            account.name = 'ZB Unknown'
            db.session.commit()


def test_a_lead_with_no_vertical_still_has_a_question_to_answer():
    """And nobody without a claim gets past it.

    Not asserted as `no_vertical`: `vertical_heads.master_vertical`
    puts every unmapped or blank value in the Management bucket by
    deliberate decision, so a lead with no vertical of its own is
    asked the owner question about that bucket rather than being told
    it has no vertical. That is pre-existing and not CRM-WA-01b's to
    change — what matters here is that the reason is still raised and
    still refuses a passer-by.
    """
    with flask_app.app_context():
        account = _account(owner_vertical=AIR, owner=AIR_OWNER)
        lead = _lead(account=account, vertical='')
        assert account_gate.reason_for(lead)[0] != ''
        assert account_gate.check(lead, 'Quoted',
                                  emp_code=STRANGER) is not None


def test_closing_a_lead_is_still_never_blocked():
    with flask_app.app_context():
        lead = _lead(account=None)
        for stage in ('Lost', 'On Hold', 'Not Interested'):
            assert account_gate.check(lead, stage,
                                      emp_code=STRANGER) is None


def test_with_the_flag_off_nothing_changes():
    with flask_app.app_context():
        _on(False)
        lead = _lead(account=None)
        assert account_gate.check(lead, 'Quoted', emp_code=STRANGER) is None


# ── the message ──────────────────────────────────────────────────────
def test_the_refusal_names_the_rule():
    with flask_app.app_context():
        lead = _the_case()
        refusal = account_gate.check(lead, 'Quoted', emp_code=STRANGER)
        assert account_gate.RULE in refusal
        assert 'FEATURE_ACCOUNT_FIRST' in refusal


def test_the_refusal_names_who_can_resolve_it():
    with flask_app.app_context():
        lead = _the_case()
        refusal = account_gate.check(lead, 'Quoted', emp_code=STRANGER)
        assert 'Account check' in refusal
        assert FF in refusal          # the head of *this* vertical
        assert 'admin' in refusal


def test_the_staffing_refusal_says_account_ownership_is_not_the_bar():
    """So the next person reads it as a staffing gap, not a permission."""
    with flask_app.app_context():
        lead = _the_case()
        refusal = account_gate.check(lead, 'Quoted', emp_code=STRANGER)
        assert 'Account ownership does not stop' in refusal


def test_the_no_account_refusal_still_points_at_account_check():
    with flask_app.app_context():
        lead = _lead(account=None)
        refusal = account_gate.check(lead, 'Quoted', emp_code=OWNER)
        assert 'Account check' in refusal
        assert account_gate.RULE in refusal


def test_no_employee_code_or_name_is_written_into_the_gate():
    """The standing rule, checked where a resolver is now named."""
    import inspect
    source = inspect.getsource(account_gate)
    for forbidden in ('EMP', 'emp_code='):
        assert f'{forbidden}0' not in source
    # Roles, not people.
    assert 'vertical head' in source


# ── the name mismatch: a question, never a refusal ───────────────────
def test_a_name_mismatch_is_reported_and_does_not_refuse():
    with flask_app.app_context():
        account = _account(name='ZB Sinofreight', owner_vertical=FF,
                           owner=OWNER)
        lead = _lead(account=account,
                     company='ZB Sinocan Freight Logistics Co Ltd')
        assert account_gate.name_mismatch(lead)
        # Fully staffed and linked: the rule has nothing to say.
        assert account_gate.check(lead, 'Quoted', emp_code=OWNER) is None
        assert account_gate.reason_for(lead)[0] == ''


def test_matching_names_raise_no_question():
    with flask_app.app_context():
        account = _account(name='ZB Sinofreight', owner_vertical=FF,
                           owner=OWNER)
        lead = _lead(account=account, company='ZB Sinofreight Pvt Ltd')
        assert account_gate.name_mismatch(lead) == ()


def test_a_lead_with_no_account_has_no_name_question():
    with flask_app.app_context():
        assert account_gate.name_mismatch(_lead(account=None)) == ()


# ── claims ───────────────────────────────────────────────────────────
def test_the_claim_is_named_in_words_not_a_boolean():
    """The banner tells the user why they may proceed."""
    with flask_app.app_context():
        lead = _the_case()
        assert account_gate.worker_claim(lead, OWNER) == \
            account_gate.CLAIM_OWNER
        assert account_gate.worker_claim(lead, COLLEAGUE) == \
            account_gate.CLAIM_VERTICAL


def test_an_employee_with_no_vertical_claims_nothing():
    """Blank must not match blank, or everybody is in every vertical."""
    with flask_app.app_context():
        db.session.add(Employee(emp_code='ZB_NOVERT', name='Nv',
                                password_hash='x', role='user',
                                vertical='', is_active=True))
        db.session.commit()
        lead = _the_case()
        assert account_gate.worker_claim(lead, 'ZB_NOVERT') == ''


def test_an_unknown_code_claims_nothing():
    with flask_app.app_context():
        assert account_gate.worker_claim(_the_case(), 'ZB_GHOST') == ''


def test_seeing_every_lead_is_not_a_claim_on_one():
    """Whole-company data scope is not a claim.

    The Access Matrix answers "may you open this record", which a
    whole-company viewer passes for every lead in the book. The
    question here is narrower: is this lead yours to move.

    A *CRM administrator* is a separate case and is deliberately
    exempt — see the test below. CRM-MAIL-02 §2: the refusal names
    who can clear the account question, and refusing that same
    person for it is a loop.
    """
    from app.models.access import AccessProfile, DataScope

    with flask_app.app_context():
        db.session.add(Employee(emp_code='ZB_VIEWER', name='Viewer',
                                password_hash='x', role='user',
                                vertical=AIR, is_active=True))
        db.session.add(AccessProfile(
            emp_code='ZB_VIEWER', data_scope=DataScope.ALL,
            perms=['reports.action']))
        db.session.commit()
        lead = _the_case()
        assert account_gate.worker_claim(lead, 'ZB_VIEWER') == ''
        assert account_gate.check(lead, 'Quoted',
                                  emp_code='ZB_VIEWER') is not None


def test_whoever_can_clear_the_question_is_not_refused_for_it():
    """CRM-MAIL-02 §2, and it is the way out of an unowned lead.

    The old rule read ownership, and an unowned lead has no owner to
    read — so the people who could fix it were refused by the very
    sentence telling them to fix it. An administrator, the head of
    the lead's vertical and the RFQ desk can all settle who owns the
    account, so none of them is held up by the question.
    """
    from app.models.access import AccessProfile, DataScope

    with flask_app.app_context():
        db.session.add(Employee(emp_code='ZB_TRIAGE', name='Triage',
                                password_hash='x', role='user',
                                vertical=AIR, is_active=True))
        db.session.add(AccessProfile(
            emp_code='ZB_TRIAGE', data_scope=DataScope.ALL,
            perms=['admin.triage']))
        db.session.commit()
        lead = _the_case()
        assert account_gate.worker_claim(lead, 'ZB_TRIAGE') == \
            account_gate.CLAIM_RESOLVER
        assert account_gate.check(lead, 'Quoted',
                                  emp_code='ZB_TRIAGE') is None


def test_the_head_of_another_vertical_is_still_refused():
    with flask_app.app_context():
        lead = _the_case()
        # OWNER is the head of Freight Forwarding in this fixture, and
        # is also the lead owner; the head of the *account's* other
        # vertical is not.
        assert account_gate.check(lead, 'Quoted',
                                  emp_code=AIR_OWNER) is not None


def test_the_code_is_matched_case_insensitively():
    with flask_app.app_context():
        lead = _the_case()
        assert account_gate.worker_claim(lead, OWNER.lower()) == \
            account_gate.CLAIM_OWNER


# ── the stage move, through the route ────────────────────────────────
def _client(code):
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = code
        s['role'] = 'user'
    return client


def _token(client):
    """The token a browser reads out of the cookie.

    Sent on the POSTs below. Without it CSRF answers 400 before the
    gate runs, and a test meaning to assert "the owner may advance"
    would fail on the wrong refusal — or, worse, a test meaning to
    assert a refusal would pass on it.
    """
    r = client.get('/my-work')
    for header in r.headers.getlist('Set-Cookie'):
        if header.startswith('csrf_token='):
            return header.split('=', 1)[1].split(';')[0]
    return ''


def test_the_owner_can_advance_the_real_lead_over_http():
    """End to end, which is where the complaint was made.

    The estimated value goes in the payload because CRM-VALUE-01 asks
    for one at Quoted and always did. That rule is not this one: it
    names what is missing and the person can supply it, which is the
    whole difference from the refusal this release is about.
    """
    with flask_app.app_context():
        lead_id = _the_case().id
    client = _client(OWNER)
    r = client.post(f'/api/leads/{lead_id}/advance',
                    json={'to_stage': 'Quoted', 'estimated_value': 250000},
                    headers={'X-CSRFToken': _token(client)})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).stage == 'Quoted'


def test_an_unrelated_user_still_cannot_over_http():
    """403 or 400 — refused either way, and never moved."""
    with flask_app.app_context():
        lead_id = _the_case().id
    client = _client(STRANGER)
    r = client.post(f'/api/leads/{lead_id}/advance',
                    json={'to_stage': 'Quoted'},
                    headers={'X-CSRFToken': _token(client)})
    assert r.status_code in (400, 403)
    if r.status_code == 400:
        assert r.get_json().get('needs') == 'account'
        assert account_gate.RULE in r.get_json().get('error', '')
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).stage != 'Quoted'


# ── what the lead screen is told ─────────────────────────────────────
def test_the_lead_screen_is_told_the_move_is_allowed_and_why():
    with flask_app.app_context():
        lead_id = _the_case().id
    r = _client(OWNER).get(f'/api/leads/{lead_id}/account-state')
    assert r.status_code == 200
    body = r.get_json()
    assert body['live'] is True
    assert body['blocked'] is False
    assert body['exempt'] is True
    assert body['claim'] == account_gate.CLAIM_OWNER
    assert body['reason'] == account_gate.WHY_NO_OWNER
    assert 'Account check' in body['resolver']
    assert body['message'] == ''


def test_the_lead_screen_tells_an_admin_they_can_settle_it():
    """An administrator may open anybody's lead, and after CRM-MAIL-02
    is not refused for a question they can answer themselves. The
    banner must then say that rather than nothing.

    This test asserted the refusal until 12 November. The refusal is
    kept for somebody with no claim and no power to clear it —
    `test_an_unrelated_user_is_still_refused` — and the message
    itself is asserted directly above.
    """
    with flask_app.app_context():
        db.session.add(Employee(emp_code='ZB_ADM2', name='Adm2',
                                password_hash='x', role='admin',
                                vertical=AIR, is_active=True))
        db.session.commit()
        lead_id = _the_case().id
    client = _client('ZB_ADM2')
    with client.session_transaction() as sess:
        sess['role'] = 'admin'
    r = client.get(f'/api/leads/{lead_id}/account-state')
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body['blocked'] is False
    assert body['exempt'] is True
    assert body['claim'] == account_gate.CLAIM_RESOLVER
    assert body['reason'] == account_gate.WHY_NO_OWNER
    assert 'Account check' in body['resolver']


def test_the_lead_screen_offers_confirm_account_on_a_name_mismatch():
    with flask_app.app_context():
        account = _account(name='ZB Sinofreight', owner_vertical=FF,
                           owner=OWNER)
        lead_id = _lead(account=account,
                        company='ZB Sinocan Freight Logistics Co Ltd').id
    r = _client(OWNER).get(f'/api/leads/{lead_id}/account-state')
    body = r.get_json()
    assert body['blocked'] is False
    assert body['mismatch']['account_name'] == 'ZB Sinofreight'
    assert body['confirm_url'] == '/accounts/check'


def test_the_lead_screen_says_nothing_with_the_flag_off():
    with flask_app.app_context():
        _on(False)
        lead_id = _lead(account=None).id
    r = _client(OWNER).get(f'/api/leads/{lead_id}/account-state')
    body = r.get_json()
    assert body['live'] is False
    assert body['blocked'] is False


def test_the_account_state_route_needs_a_signed_in_user():
    with flask_app.app_context():
        lead_id = _the_case().id
    assert flask_app.test_client().get(
        f'/api/leads/{lead_id}/account-state').status_code == 401


# ── the diagnosis script ─────────────────────────────────────────────
def test_the_script_reports_and_writes_nothing():
    import importlib.util

    path = os.path.join(_ROOT, 'scripts', '2026_11_11_account_block.py')
    spec = importlib.util.spec_from_file_location('acctblock', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    source = open(path).read()
    assert not hasattr(mod, 'apply')
    assert "add_argument('--apply'" not in source
    assert 'db.session.commit' not in source

    with flask_app.app_context():
        lead = _the_case()
        lead.original_email_from = 'overseas@zbsinofreight.example'
        db.session.commit()
        found = mod.find_by_sender('overseas@zbsinofreight.example')
        assert lead.id in [l.id for l in found]
        claims = dict(mod.claimants(lead))
        assert OWNER in claims and COLLEAGUE in claims
        assert STRANGER not in claims
        mod.report_lead(lead)          # must not raise
        assert db.session.get(Lead, lead.id).stage == 'RFQ Generated'
