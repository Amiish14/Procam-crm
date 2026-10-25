"""
No work on a lead until somebody's account owns it.

CRM-ACCOUNT-FIRST-01 (R1).

Two things these tests are really guarding.

The flag off must be invisible. Every existing screen, route and
count has to behave exactly as it did, because that is what makes
the rollback a switch rather than a revert. Most of the file is
therefore about what does *not* happen.

And the gate must not become a trap. A lead that turns out to be
nothing has to be closeable without first inventing a customer for
it — otherwise the queue fills with records nobody can clear, and
the way out of that is always to click something untrue.
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
os.environ.setdefault('SECRET_KEY', 'acctfirst-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'AcctFirst-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'acctfirst.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.assignment')
flask_app = _main.app
# As every other module does. The real CSRF behaviour of these new
# write paths is covered with the protection armed in
# test_csrf_live.py, which is where that question belongs.
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Lead, Company, Employee = _main.Lead, _main.Company, _main.Employee

from app.models.assignment import (AccountOwnerConfirmation,   # noqa: E402
                                   AccountVerticalOwner,
                                   SenderAccountLink)
from app.models.email_admin import VerticalHead              # noqa: E402
from app.services import account_gate, account_match, flags   # noqa: E402

VERTICAL = 'Heavy Transport'
HEAD, REP, OTHER = 'EMP_HEAD', 'EMP_REP', 'EMP_OTH'


def _purge():
    # SenderAccountLink too: confirming an account teaches the
    # sender, and a link left behind makes the next module's matcher
    # answer "learned email" where it should answer by domain.
    for model in (Lead, AccountVerticalOwner, AccountOwnerConfirmation,
                  SenderAccountLink):
        model.query.delete()
    Company.query.filter(Company.name.like('ZZ%')).delete(
        synchronize_session=False)
    Employee.query.filter(Employee.emp_code.in_(
        (HEAD, REP, OTHER))).delete(synchronize_session=False)
    VerticalHead.query.filter_by(vertical=VERTICAL).delete(
        synchronize_session=False)
    # The flag row, too. The suite shares one database, so a flag
    # left on here arms the gate for every module that runs after
    # this one — which is exactly how test_audit_trail and
    # test_deep_links failed while passing in isolation.
    from app.models.integration import AppSetting
    AppSetting.query.filter_by(
        key='flag.FEATURE_ACCOUNT_FIRST').delete(
            synchronize_session=False)
    db.session.commit()
    flags.cache_clear()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code, name in ((HEAD, 'Head Person'), (REP, 'Rep Person'),
                           (OTHER, 'Other Person')):
            db.session.add(Employee(
                emp_code=code, name=name, password_hash='x',
                email=f'{code.lower()}@procamlogistics.com',
                role='user', vertical=VERTICAL))
        db.session.add(VerticalHead(vertical=VERTICAL,
                                    head_emp_code=HEAD, active=True))
        db.session.commit()
        yield
        _purge()


def _on(state=True):
    from app.models.integration import AppSetting
    row = AppSetting.query.filter_by(
        key='flag.FEATURE_ACCOUNT_FIRST').first()
    if row is None:
        row = AppSetting(key='flag.FEATURE_ACCOUNT_FIRST')
        db.session.add(row)
    import json
    row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


def _account(name='ZZ Steel', *, owner=None, domain=None):
    account = Company(name=name, is_active=True,
                      email_domains=[domain] if domain else [])
    db.session.add(account)
    db.session.flush()
    if owner:
        db.session.add(AccountVerticalOwner(
            account_id=account.id, vertical=VERTICAL, emp_code=owner,
            source='manual'))
    db.session.commit()
    return account


def _lead(*, account=None, stage='New', vertical=VERTICAL):
    # A lead's vertical is `procam_vertical`; `vertical` is the
    # account's field. Getting this wrong made every lead look like
    # it had no vertical at all.
    lead = Lead(company='ZZ Steel', stage=stage,
                procam_vertical=vertical,
                source='email', email='buyer@zzsteel.com',
                company_id=account.id if account else None,
                created_at=datetime.utcnow())
    db.session.add(lead)
    db.session.commit()
    return lead


# ── the flag off changes nothing ─────────────────────────────────────
def test_with_the_flag_off_the_gate_is_silent():
    with flask_app.app_context():
        _on(False)
        lead = _lead()                     # no account at all
        assert account_gate.check(lead, 'Quoted') is None
        assert account_gate.pending(lead) is False
        assert account_gate.pending_leads() == []


def test_with_the_flag_off_the_queue_route_is_not_there():
    with flask_app.app_context():
        _on(False)
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = HEAD
    assert client.get('/api/account-check/queue').status_code == 404


# ── the gate ─────────────────────────────────────────────────────────
@pytest.mark.parametrize('stage', [
    'Call Done', 'Business Discussion', 'New Opportunity',
    'RFQ Generated', 'Quoted', 'Under Negotiation', 'Won'])
def test_a_lead_with_no_account_cannot_move_forward(stage):
    with flask_app.app_context():
        _on()
        assert account_gate.check(_lead(), stage) is not None


@pytest.mark.parametrize('stage', ['Lost', 'On Hold', 'Not Interested'])
def test_a_lead_with_no_account_can_still_be_closed(stage):
    """Otherwise the queue is a trap and people click something untrue."""
    with flask_app.app_context():
        _on()
        assert account_gate.check(_lead(), stage) is None


def test_an_account_with_no_owner_for_this_vertical_is_not_enough():
    with flask_app.app_context():
        _on()
        lead = _lead(account=_account())          # account, no owner
        assert account_gate.check(lead, 'Quoted') is not None
        assert 'no owner' in account_gate.why_pending(lead)


def test_an_account_with_an_owner_passes():
    with flask_app.app_context():
        _on()
        lead = _lead(account=_account(owner=REP))
        assert account_gate.check(lead, 'Quoted') is None
        assert account_gate.pending(lead) is False


def test_the_two_reasons_are_told_apart():
    """They need different fixes: matching vs staffing."""
    with flask_app.app_context():
        _on()
        assert account_gate.why_pending(_lead()) == 'no account'
        assert account_gate.why_pending(
            _lead(account=_account())).startswith('no owner')


# ── every door, not three of four ────────────────────────────────────
def test_the_stage_stepper_refuses():
    with flask_app.app_context():
        _on()
        lead = _lead()
        # Owned by the person pushing it, or access control refuses
        # first and the test proves nothing about this gate.
        lead.assigned_to = REP
        db.session.commit()
        lead_id = lead.id
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = REP
    r = client.post(f'/api/leads/{lead_id}/advance', json={})
    assert r.status_code == 400
    assert r.get_json().get('needs') == 'account'


def test_raising_an_rfq_refuses():
    with flask_app.app_context():
        _on()
        lead_id = _lead().id
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = REP
    r = client.post('/api/rfqs', json={'subject': 'Test',
                                       'lead_id': lead_id})
    assert r.status_code == 400
    assert r.get_json().get('needs') == 'account'


def test_building_a_quote_refuses():
    with flask_app.app_context():
        _on()
        lead_id = _lead().id
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = REP
    r = client.post('/api/quotes', json={'lead_id': lead_id})
    assert r.status_code == 400
    assert r.get_json().get('needs') == 'account'


# ── who may clear it (D6) ────────────────────────────────────────────
def test_a_salesperson_cannot_confirm_an_account():
    with flask_app.app_context():
        _on()
        lead_id = _lead().id
        account_id = _account(name='ZZ Buyer').id
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = REP
    r = client.post(f'/api/account-check/{lead_id}/confirm',
                    json={'account_id': account_id})
    assert r.status_code == 403


def test_the_vertical_head_can_confirm_and_it_sticks():
    with flask_app.app_context():
        _on()
        lead_id = _lead().id
        account_id = _account(name='ZZ Buyer').id
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = HEAD
    r = client.post(f'/api/account-check/{lead_id}/confirm',
                    json={'account_id': account_id, 'owner': REP})
    assert r.status_code == 200, r.get_data(as_text=True)
    with flask_app.app_context():
        lead = db.session.get(Lead, lead_id)
        assert lead.company_id == account_id
        # and the vertical now has a named owner
        assert AccountVerticalOwner.query.filter_by(
            account_id=account_id, vertical=VERTICAL).count() == 1
        assert account_gate.check(lead, 'Quoted') is None


def test_head_ness_is_read_from_the_table_not_the_role():
    """D12: a head whose role was never updated is still the head."""
    with flask_app.app_context():
        _on()
        from app.account_check.routes import heads_verticals
        emp = Employee.query.filter_by(emp_code=HEAD).first()
        assert (emp.role or '').lower() not in ('admin', 'director')
        assert VERTICAL in heads_verticals(HEAD)
        assert VERTICAL not in heads_verticals(REP)


def test_a_blocked_account_cannot_be_chosen():
    with flask_app.app_context():
        _on()
        lead_id = _lead().id
        account_id = _account(name='ZZ Blocked').id
    import app.account_check.routes as ac
    original = ac._blocked
    ac._blocked = lambda account: True
    try:
        client = flask_app.test_client()
        with client.session_transaction() as s:
            s['emp_code'] = HEAD
        r = client.post(f'/api/account-check/{lead_id}/confirm',
                        json={'account_id': account_id})
        assert r.status_code == 400
        assert 'block register' in r.get_json()['error']
    finally:
        ac._blocked = original


# ── matching: every hit, and when to link without asking ─────────────
def test_candidates_returns_the_rule_that_matched():
    with flask_app.app_context():
        _on()
        _account(name='ZZ Domain Co', domain='zzsteel.com')
        rows = account_match.candidates('buyer@zzsteel.com')
        assert rows and rows[0]['rule'] == 'account email domain'
        assert rows[0]['exact'] is True


def test_free_mail_never_matches_on_its_domain():
    """Two customers on gmail.com are two customers."""
    with flask_app.app_context():
        _on()
        _account(name='ZZ Gmail Co', domain='gmail.com')
        assert account_match.candidates('someone@gmail.com') == []


def test_one_exact_hit_links_itself():
    with flask_app.app_context():
        _on()
        account = _account(name='ZZ Domain Co', domain='zzsteel.com')
        linked, how = account_match.auto_link('buyer@zzsteel.com')
        assert linked is not None and linked.id == account.id
        assert how == 'account email domain'


def test_a_disagreement_is_never_auto_linked():
    """A domain saying one account and the name another is the
    'likely duplicate' case the queue exists for."""
    with flask_app.app_context():
        _on()
        _account(name='ZZ Domain Co', domain='zzsteel.com')
        _account(name='ZZ Other Name')
        linked, how = account_match.auto_link(
            'buyer@zzsteel.com', company_name='ZZ Other Name')
        assert linked is None
        assert how == account_match.UNMAPPED


def test_a_name_only_hit_is_never_auto_linked():
    with flask_app.app_context():
        _on()
        _account(name='ZZ Only By Name')
        linked, _how = account_match.auto_link(
            'stranger@unknown-domain-xyz.com',
            company_name='ZZ Only By Name')
        assert linked is None


# ── the one stage list (decision 5) ──────────────────────────────────
def test_there_is_one_stage_list_and_new_opportunity_is_open():
    from app.services import sales_rules, stage_map

    assert _main.STAGES_PIPELINE == [
        s for s in stage_map.JOURNEY if s not in stage_map.TERMINAL]
    # The 156-lead bug: a journey stage the open-lead query excluded.
    assert 'New Opportunity' in sales_rules.open_stages()
    for alias in stage_map.ALIASES:
        assert alias not in _main.STAGES_PIPELINE
        assert _main.LEGACY_STAGE_MAP[alias] == stage_map.ALIASES[alias]


def test_every_stage_steps_somewhere_on_the_journey():
    from app.services import stage_map
    for stage, nxt in _main.STAGE_NEXT.items():
        assert stage in stage_map.JOURNEY
        assert nxt in stage_map.JOURNEY


# ── the per-account confirmation is its own table (D4) ───────────────
def test_owner_confirmation_is_left_alone():
    """Its lead_id is unique and its rows still mean what they meant."""
    from app.models.assignment import OwnerConfirmation
    assert OwnerConfirmation.__table__.c.lead_id.unique is True
    assert AccountOwnerConfirmation.__tablename__ == \
        'account_owner_confirmation'


def test_the_account_question_is_asked_once_per_vertical():
    with flask_app.app_context():
        account = _account(name='ZZ Once')
        db.session.add(AccountOwnerConfirmation(
            account_id=account.id, vertical=VERTICAL, emp_code=REP))
        db.session.commit()
        from sqlalchemy.exc import IntegrityError
        db.session.add(AccountOwnerConfirmation(
            account_id=account.id, vertical=VERTICAL, emp_code=OTHER))
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()


# ── §3.4: asked once per account × vertical ──────────────────────────
def test_confirming_answers_the_account_level_question():
    from app.services import account_owner_confirm as aoc

    with flask_app.app_context():
        _on()
        lead_id = _lead().id
        account_id = _account(name='ZZ Once Asked').id
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = HEAD
    r = client.post(f'/api/account-check/{lead_id}/confirm',
                    json={'account_id': account_id, 'owner': REP})
    assert r.status_code == 200
    with flask_app.app_context():
        assert aoc.answered(account_id, VERTICAL) == REP


def test_the_next_lead_from_that_customer_asks_nobody():
    from app.services import account_owner_confirm as aoc

    with flask_app.app_context():
        _on()
        account = _account(name='ZZ Inherits', owner=REP)
        aoc.ask(account.id, VERTICAL, REP, rule='test')
        aoc.answer(account.id, VERTICAL, outcome='confirmed', by=HEAD)
        # A later lead is not held and raises no second question.
        later = _lead(account=account)
        assert account_gate.check(later, 'Quoted') is None
        assert aoc.ask(account.id, VERTICAL, REP) is None


def test_an_answered_question_is_never_reopened():
    from app.services import account_owner_confirm as aoc

    with flask_app.app_context():
        account = _account(name='ZZ Settled')
        aoc.ask(account.id, VERTICAL, REP, rule='test')
        aoc.answer(account.id, VERTICAL, outcome='confirmed', by=HEAD)
        ok, why = aoc.answer(account.id, VERTICAL,
                             outcome='reassigned', reassign_to=OTHER,
                             by=HEAD)
        assert ok is False and why == 'already answered'


def test_reassigning_writes_the_owner_not_just_the_answer():
    """Otherwise the next lead asks the same question again."""
    from app.services import account_owner_confirm as aoc

    with flask_app.app_context():
        account = _account(name='ZZ Moved')
        aoc.ask(account.id, VERTICAL, REP, rule='test')
        ok, _ = aoc.answer(account.id, VERTICAL, outcome='reassigned',
                           reassign_to=OTHER, by=HEAD)
        assert ok is True
        assert aoc.answered(account.id, VERTICAL) == OTHER
        assert AccountVerticalOwner.query.filter_by(
            account_id=account.id, vertical=VERTICAL
        ).first().emp_code == OTHER
