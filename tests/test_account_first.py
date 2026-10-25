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
                                   LeadAccountSuggestion,
                                   SenderAccountLink)
from app.models.email_admin import VerticalHead              # noqa: E402
from app.services import account_gate, account_match, flags   # noqa: E402

VERTICAL = 'Heavy Transport'
HEAD, REP, OTHER = 'EMP_HEAD', 'EMP_REP', 'EMP_OTH'

#: Accounts this module made, by id. Purging on `name LIKE 'ZZ%'`
#: alone leaked: one test renames its account to "Email" to check
#: the placeholder guard, the name stops matching, the row survives
#: and competes for the same domain in the next test.
_MADE = set()


def _purge():
    # SenderAccountLink too: confirming an account teaches the
    # sender, and a link left behind makes the next module's matcher
    # answer "learned email" where it should answer by domain.
    for model in (Lead, AccountVerticalOwner, AccountOwnerConfirmation,
                  SenderAccountLink, LeadAccountSuggestion):
        model.query.delete()
    Company.query.filter(db.or_(Company.name.like('ZZ%'),
                                Company.id.in_(_MADE or {-1}))).delete(
        synchronize_session=False)
    _MADE.clear()
    Employee.query.filter(Employee.emp_code.in_(
        (HEAD, REP, OTHER))).delete(synchronize_session=False)
    VerticalHead.query.filter_by(vertical=VERTICAL).delete(
        synchronize_session=False)
    # And this module's own audit rows. SQLite reuses lead ids
    # after a purge, so lead #1 in the next test inherits the audit
    # trail of lead #1 in the last one — which made a one-row
    # assertion see four.
    from app.models.audit import AuditEvent
    AuditEvent.query.filter(AuditEvent.action.in_((
        'lead.account_confirmed', 'account.vertical_owner_set',
        'account.owner_confirmed', 'account.created',
        'lead.created_for_account'))).delete(
            synchronize_session=False)
    # The flag row, too. The suite shares one database, so a flag
    # left on here arms the gate for every module that runs after
    # this one — which is exactly how test_audit_trail and
    # test_deep_links failed while passing in isolation.
    from app.models.integration import AppSetting
    AppSetting.query.filter(AppSetting.key.like(
        'flag.FEATURE_ACCOUNT_FIRST%')).delete(
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
    _MADE.add(account.id)
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


# ── suggestions: a guess, filed, not a link ──────────────────────────
def test_the_pass_links_nothing():
    """The whole point. auto_link would place some of these; doing
    that to 352 historical leads in a batch is a different act from
    doing it to mail as it arrives."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import account_suggestions as pass_

    with flask_app.app_context():
        _on()
        _account(name='ZZ Domain Co', domain='zzsteel.com')
        lead = _lead()                       # no account
        pass_.run(write=True)
        assert db.session.get(Lead, lead.id).company_id is None

        from app.models.assignment import LeadAccountSuggestion
        row = LeadAccountSuggestion.query.filter_by(
            lead_id=lead.id).first()
        assert row is not None
        assert row.exact_single is True
        assert row.rule == 'account email domain'
        assert row.confidence == 85


def test_confidence_ranks_rules_not_guesses():
    from app.services import account_suggest as sug

    assert sug.confidence_of('learned email') == 100
    assert sug.confidence_of('GSTIN') > sug.confidence_of('company name')
    # A resemblance stays well under the bulk floor however close.
    assert sug.confidence_of('company name',
                             note='98% like ZZ Co') < sug.TRUSTED


def test_a_lead_with_nothing_to_match_on_still_gets_a_row():
    """Otherwise 'not looked at' and 'looked at, found nothing' are
    indistinguishable."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import account_suggestions as pass_

    with flask_app.app_context():
        _on()
        lead = _lead()
        lead.email = 'nobody@nowhere-at-all-xyz.com'
        lead.company = ''
        db.session.commit()
        pass_.run(write=True)
        from app.models.assignment import LeadAccountSuggestion
        row = LeadAccountSuggestion.query.filter_by(
            lead_id=lead.id).first()
        assert row is not None
        assert row.account_id is None
        assert row.exact_single is False


def test_a_rerun_never_overwrites_a_human_answer():
    import sys as _sys
    from datetime import datetime as _dt
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import account_suggestions as pass_
    from app.models.assignment import LeadAccountSuggestion

    with flask_app.app_context():
        _on()
        _account(name='ZZ Domain Co', domain='zzsteel.com')
        lead = _lead()
        pass_.run(write=True)
        row = LeadAccountSuggestion.query.filter_by(
            lead_id=lead.id).first()
        row.acted_at, row.outcome = _dt.utcnow(), 'replaced'
        row.account_id = None
        db.session.commit()

        pass_.run(write=True)
        again = LeadAccountSuggestion.query.filter_by(
            lead_id=lead.id).first()
        assert again.outcome == 'replaced'
        assert again.account_id is None


def test_a_stale_suggestion_is_spotted():
    from app.models.assignment import LeadAccountSuggestion
    from app.services import account_suggest as sug

    with flask_app.app_context():
        _on()
        account = _account(name='ZZ Domain Co', domain='zzsteel.com')
        lead = _lead()
        sug.store(sug.suggest_for(lead), commit=True)
        row = LeadAccountSuggestion.query.filter_by(
            lead_id=lead.id).first()
        assert sug.stale(row, lead) is False

        # A second account on the same domain: the answer is no
        # longer a single exact match.
        _account(name='ZZ Rival Co', domain='zzsteel.com')
        assert sug.stale(row, lead) is True


# ── bulk confirm ─────────────────────────────────────────────────────
def _bulk(client, ids):
    return client.post('/api/account-check/bulk-confirm',
                       json={'lead_ids': ids})


def test_bulk_confirm_links_the_clear_ones():
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import account_suggestions as pass_

    with flask_app.app_context():
        _on()
        account = _account(name='ZZ Domain Co', domain='zzsteel.com',
                           owner=REP)
        lead_id = _lead().id
        account_id = account.id
        pass_.run(write=True)
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = HEAD
    r = _bulk(client, [lead_id])
    assert r.status_code == 200, r.get_data(as_text=True)
    assert r.get_json()['confirmed'] == 1
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).company_id == account_id


def test_bulk_confirm_rechecks_and_names_what_it_skipped():
    """A stale row must not ride through on the screen's copy, and
    a silent drop is how somebody later finds an unowned lead."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import account_suggestions as pass_

    with flask_app.app_context():
        _on()
        _account(name='ZZ Domain Co', domain='zzsteel.com', owner=REP)
        lead_id = _lead().id
        pass_.run(write=True)
        # Something changed after the pass: now two accounts claim
        # the domain, so it is no longer a single exact match.
        _account(name='ZZ Rival Co', domain='zzsteel.com', owner=REP)
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = HEAD
    body = _bulk(client, [lead_id]).get_json()
    assert body['confirmed'] == 0
    assert body['skipped'][0]['lead_id'] == lead_id
    assert 'single exact match' in body['skipped'][0]['why']
    with flask_app.app_context():
        assert db.session.get(Lead, lead_id).company_id is None


def test_a_salesperson_cannot_bulk_confirm():
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import account_suggestions as pass_

    with flask_app.app_context():
        _on()
        _account(name='ZZ Domain Co', domain='zzsteel.com', owner=REP)
        lead_id = _lead().id
        pass_.run(write=True)
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = REP
    r = _bulk(client, [lead_id])
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body['confirmed'] == 0
    assert 'not yours' in body['skipped'][0]['why']


def test_bulk_confirm_refuses_a_blocked_account():
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import account_suggestions as pass_

    with flask_app.app_context():
        _on()
        _account(name='ZZ Domain Co', domain='zzsteel.com', owner=REP)
        lead_id = _lead().id
        pass_.run(write=True)
    import app.account_check.routes as ac
    original = ac._blocked
    ac._blocked = lambda account: True
    try:
        client = flask_app.test_client()
        with client.session_transaction() as s:
            s['emp_code'] = HEAD
        body = _bulk(client, [lead_id]).get_json()
        assert body['confirmed'] == 0
        assert 'blocked' in body['skipped'][0]['why']
    finally:
        ac._blocked = original


def test_bulk_confirm_writes_an_audit_row_each():
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import account_suggestions as pass_
    from app.models.audit import AuditEvent

    with flask_app.app_context():
        _on()
        _account(name='ZZ Domain Co', domain='zzsteel.com', owner=REP)
        ids = [_lead().id]
        pass_.run(write=True)
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = HEAD
    _bulk(client, ids)
    with flask_app.app_context():
        # Scoped to these leads: audit_events is shared across the
        # suite and earlier tests in this module confirm leads too.
        rows = (AuditEvent.query
                .filter(AuditEvent.action == 'lead.account_confirmed',
                        AuditEvent.entity_id.in_([str(i) for i in ids]))
                .all())
        assert len(rows) == len(ids)
        assert 'bulk-confirmed' in (rows[0].reason or '')


def test_bulk_confirm_has_a_ceiling():
    with flask_app.app_context():
        _on()
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = HEAD
    r = _bulk(client, list(range(1, 600)))
    assert r.status_code == 400
    assert '500' in r.get_json()['error']


# ── a resemblance is not a rival claim ───────────────────────────────
def test_a_mere_resemblance_does_not_block_a_domain_match():
    """The bug this caught: of 352 leads with a clear domain hit,
    exactly one survived as unambiguous, because any string 50%
    alike counted as a competing account."""
    with flask_app.app_context():
        _on()
        account = _account(name='ZZ Domain Co', domain='zzsteel.com')
        _account(name='ZZ Domain Cx')         # looks alike, means nothing
        linked, how = account_match.auto_link(
            'buyer@zzsteel.com', company_name='ZZ Domain Co')
        assert linked is not None and linked.id == account.id
        assert how == 'account email domain'


def test_a_real_name_match_still_blocks_it():
    """A second account the name genuinely resolves to is a rival."""
    with flask_app.app_context():
        _on()
        _account(name='ZZ Domain Co', domain='zzsteel.com')
        _account(name='ZZ Other Name')
        linked, _how = account_match.auto_link(
            'buyer@zzsteel.com', company_name='ZZ Other Name')
        assert linked is None


def test_resemblances_are_tagged_so_a_person_still_sees_them():
    with flask_app.app_context():
        _on()
        _account(name='ZZ Domain Co', domain='zzsteel.com')
        _account(name='ZZ Domain Cx')
        rows = account_match.candidates('buyer@zzsteel.com',
                                        company_name='ZZ Domain Co')
        kinds = {r['name']: r['kind'] for r in rows}
        assert kinds['ZZ Domain Co'] in ('exact', 'name')
        assert kinds.get('ZZ Domain Cx') == 'resemblance'


# ── accounts that are not customers ──────────────────────────────────
@pytest.mark.parametrize('name', ['Unknown', 'New Lead', 'Email',
                                  'Info', 'N/A', '-'])
def test_a_placeholder_account_is_never_linked_unasked(name):
    with flask_app.app_context():
        _on()
        _account(name=name, domain='zzsteel.com')
        linked, _how = account_match.auto_link('buyer@zzsteel.com')
        assert linked is None


def test_an_account_holding_our_own_domain_is_refused():
    """#983 'Unknown' lists procamgroup.in. With the flag on it
    would have swallowed every internal forward."""
    from app.services.mail_signals import OUR_DOMAINS

    with flask_app.app_context():
        _on()
        ours = sorted(OUR_DOMAINS)[0]
        account = _account(name='ZZ Looks Real', domain=ours)
        assert account_match.not_a_customer(account)
        linked, _how = account_match.auto_link(f'colleague@{ours}')
        assert linked is None


def test_a_real_account_is_not_caught_by_the_guard():
    with flask_app.app_context():
        _on()
        account = _account(name='ZZ Steel Industries',
                           domain='zzsteel.com')
        assert account_match.not_a_customer(account) == ''


def test_bulk_confirm_refuses_a_placeholder_too():
    """auto_link is one caller, not the gate."""
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import account_suggestions as pass_
    from app.models.assignment import LeadAccountSuggestion

    with flask_app.app_context():
        _on()
        account = _account(name='ZZ Real Co', domain='zzsteel.com',
                           owner=REP)
        lead_id = _lead().id
        pass_.run(write=True)
        # It qualified; now the account turns out to be a
        # placeholder, as #709 "Email" did.
        account.name = 'Email'
        db.session.commit()
        assert LeadAccountSuggestion.query.filter_by(
            lead_id=lead_id).first().exact_single is True
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = HEAD
    body = _bulk(client, [lead_id]).get_json()
    assert body['confirmed'] == 0
    assert 'placeholder' in body['skipped'][0]['why']


# ── a placeholder account is no account ──────────────────────────────
@pytest.mark.parametrize('name', ['Unknown', 'New Lead', 'Info',
                                  'Email'])
def test_a_lead_under_a_placeholder_is_held(name):
    """It *looks* placed, which is worse than being unplaced: it
    never reaches the queue, and several customers' histories pool
    under one meaningless row."""
    with flask_app.app_context():
        _on()
        lead = _lead(account=_account(name=name, owner=REP))
        assert account_gate.check(lead, 'Quoted') is not None
        assert 'placeholder' in account_gate.why_pending(lead)


def test_a_lead_under_an_own_domain_account_is_held():
    from app.services.mail_signals import OUR_DOMAINS

    with flask_app.app_context():
        _on()
        ours = sorted(OUR_DOMAINS)[0]
        account = _account(name='ZZ Looks Fine', owner=REP,
                           domain=ours)
        lead = _lead(account=account)
        assert account_gate.check(lead, 'Quoted') is not None
        assert 'our own domain' in account_gate.why_pending(lead)


def test_a_real_account_with_an_owner_still_passes():
    with flask_app.app_context():
        _on()
        lead = _lead(account=_account(name='ZZ Real Steel',
                                      owner=REP))
        assert account_gate.check(lead, 'Quoted') is None


def test_such_a_lead_appears_in_the_queue():
    with flask_app.app_context():
        _on()
        lead = _lead(account=_account(name='Unknown', owner=REP))
        held = [l.id for l in account_gate.pending_leads(
            sc=__import__('app.access.scope', fromlist=['x'])
            .Scope(emp_code=HEAD, vertical='', codes=None, perms=(),
                   data_scope='all'))]
        assert lead.id in held


# ── confirm_one is the one implementation ────────────────────────────
def test_confirm_one_refuses_to_replace_a_real_account():
    from app.services import account_suggest as sug

    with flask_app.app_context():
        _on()
        real = _account(name='ZZ Already Here', owner=REP)
        _account(name='ZZ Domain Co', domain='zzsteel.com', owner=REP)
        lead = _lead(account=real)
        ok, why = sug.confirm_one(lead, actor=HEAD)
        assert ok is False
        assert why == 'already has an account'
        assert lead.company_id == real.id


def test_confirm_one_will_replace_a_placeholder():
    """That is the whole point of counting one as no account."""
    from app.services import account_suggest as sug

    with flask_app.app_context():
        _on()
        _account(name='ZZ Domain Co', domain='zzsteel.com', owner=REP)
        lead = _lead(account=_account(name='Unknown', owner=REP))
        ok, detail = sug.confirm_one(lead, actor=HEAD)
        assert ok is True, detail
        assert detail['account'] == 'ZZ Domain Co'


# ── closing what nothing can identify ────────────────────────────────
def _sweep():
    import sys as _sys
    _sys.path.insert(0, os.path.join(_ROOT, 'scripts'))
    import close_unidentifiable_leads as mod
    return mod


def _old_lead(account, **kw):
    from datetime import timedelta
    lead = Lead(company=kw.pop('company', ''),
                stage=kw.pop('stage', 'New'),
                procam_vertical=VERTICAL, source='email',
                company_id=account.id,
                created_at=datetime.utcnow() - timedelta(days=200),
                **kw)
    db.session.add(lead)
    db.session.commit()
    return lead


def test_an_old_unidentifiable_lead_is_closed():
    mod = _sweep()
    with flask_app.app_context():
        lead = _old_lead(_account(name='Unknown'))
        close, _keep = mod.plan()
        assert [e['lead'].id for e in close] == [lead.id]
        mod.apply_plan(close)
        db.session.commit()
        fresh = db.session.get(Lead, lead.id)
        assert fresh.stage == 'Lost'
        assert fresh.lost_reason == 'No Response'


@pytest.mark.parametrize('field,value', [
    ('email', 'someone@realcompany.com'),
    ('company', 'Tata Steel Limited'),
])
def test_a_lead_with_any_identifier_is_left_alone(field, value):
    """However old. The identifier is the one thing that makes a
    later match possible; closing it destroys that."""
    mod = _sweep()
    with flask_app.app_context():
        lead = _old_lead(_account(name='Unknown'), **{field: value})
        close, keep = mod.plan()
        assert close == []
        assert keep[0]['lead'].id == lead.id
        assert keep[0]['identifiers']


def test_a_gstin_in_the_body_counts_as_an_identifier():
    mod = _sweep()
    with flask_app.app_context():
        lead = _old_lead(_account(name='Unknown'),
                         notes='Our GSTIN is 27AAFCP6574J1ZW for '
                               'billing.')
        close, keep = mod.plan()
        assert close == []
        assert any('GSTIN' in i for i in keep[0]['identifiers'])


def test_a_recent_lead_stays_in_the_queue():
    """Bare, like the old ones — only its age saves it."""
    mod = _sweep()
    with flask_app.app_context():
        lead = Lead(company='', stage='New', source='email',
                    procam_vertical=VERTICAL,
                    company_id=_account(name='Unknown').id,
                    created_at=datetime.utcnow())
        db.session.add(lead)
        db.session.commit()
        close, keep = mod.plan()
        assert close == []
        assert keep[0]['identifiers'] == []
        assert 'days old' in keep[0]['reason']


def test_a_lead_under_a_real_account_is_out_of_scope():
    mod = _sweep()
    with flask_app.app_context():
        _old_lead(_account(name='ZZ Real Co', owner=REP))
        close, keep = mod.plan()
        assert close == [] and keep == []


def test_the_placeholder_name_copied_onto_the_lead_is_not_an_identifier():
    mod = _sweep()
    with flask_app.app_context():
        lead = _old_lead(_account(name='New Lead'), company='New Lead')
        close, _keep = mod.plan()
        assert [e['lead'].id for e in close] == [lead.id]


def test_down_puts_them_back():
    mod = _sweep()
    with flask_app.app_context():
        lead = _old_lead(_account(name='Unknown'))
        close, _keep = mod.plan()
        mod.apply_plan(close)
        db.session.commit()
        mod.undo()
        db.session.commit()
        fresh = db.session.get(Lead, lead.id)
        assert fresh.stage == 'New'
        assert not fresh.lost_reason


def test_an_erp_link_counts_as_an_identifier():
    """Lead #9246 was at Under Negotiation, had an ERP opportunity
    link and a named contact, and the first version of this sweep
    would have closed it as "No Response"."""
    mod = _sweep()
    with flask_app.app_context():
        lead = _old_lead(
            _account(name='Unknown'), stage='Under Negotiation',
            notes='Owner (unmatched): VENKATESH ALTHADA | ERP: '
                  'http://pws.myprocam.in/OpportunityERPOpportunity/'
                  'Detail/6a2b7c5b')
        close, keep = mod.plan()
        assert close == []
        assert keep[0]['lead'].id == lead.id
        assert any('ERP' in i for i in keep[0]['identifiers'])
        assert any('VENKATESH' in i for i in keep[0]['identifiers'])


# ── the pilot: on for some people, off for the rest ──────────────────
def _limit(codes=(), verticals=()):
    flags.limit_to('FEATURE_ACCOUNT_FIRST', codes, actor='test')
    flags.limit_verticals('FEATURE_ACCOUNT_FIRST', verticals,
                          actor='test')
    flags.cache_clear()


def test_a_pilot_holds_only_the_pilots_leads():
    """A gate that read only flags.on() would hold everybody's
    leads the moment the switch went on for two people."""
    with flask_app.app_context():
        _on()
        _limit(codes=[HEAD], verticals=[])
        try:
            assert account_gate.enabled(HEAD) is True
            assert account_gate.enabled(REP) is False
            # The same lead, two people: held for the pilot, silent
            # for everybody else.
            lead = _lead()
            assert account_gate.check(lead, 'Quoted') is not None
            import app.services.account_gate as ag
            was, ag.enabled = ag.enabled, lambda e=None: False
            try:
                assert ag.check(lead, 'Quoted') is None
            finally:
                ag.enabled = was
        finally:
            _limit()


def test_a_vertical_limit_covers_everybody_in_it():
    with flask_app.app_context():
        _on()
        emp = Employee.query.filter_by(emp_code=REP).first()
        emp.vertical = VERTICAL
        db.session.commit()
        _limit(codes=[], verticals=[VERTICAL])
        try:
            assert flags.on_for('FEATURE_ACCOUNT_FIRST', REP) is True
            assert flags.on_for('FEATURE_ACCOUNT_FIRST',
                                'EMP_NOBODY') is False
        finally:
            _limit()


def test_the_two_limits_are_an_or_not_an_and():
    """A pilot is 'this person, and everybody in that vertical'."""
    with flask_app.app_context():
        _on()
        emp = Employee.query.filter_by(emp_code=OTHER).first()
        emp.vertical = 'Warehousing'
        db.session.commit()
        _limit(codes=[HEAD], verticals=['Warehousing'])
        try:
            assert flags.on_for('FEATURE_ACCOUNT_FIRST', HEAD) is True
            assert flags.on_for('FEATURE_ACCOUNT_FIRST', OTHER) is True
            assert flags.on_for('FEATURE_ACCOUNT_FIRST', REP) is False
        finally:
            _limit()


def test_a_limit_never_turns_a_dead_flag_on():
    with flask_app.app_context():
        _on(False)
        _limit(codes=[HEAD], verticals=[VERTICAL])
        try:
            assert flags.on_for('FEATURE_ACCOUNT_FIRST', HEAD) is False
        finally:
            _limit()


def test_the_queue_404s_for_somebody_outside_the_pilot():
    with flask_app.app_context():
        _on()
        _limit(codes=[HEAD], verticals=[])
    try:
        client = flask_app.test_client()
        with client.session_transaction() as s:
            s['emp_code'] = REP
        assert client.get('/api/account-check/queue').status_code == 404
        with client.session_transaction() as s:
            s['emp_code'] = HEAD
        assert client.get('/api/account-check/queue').status_code == 200
    finally:
        with flask_app.app_context():
            _limit()


def test_an_anonymous_visitor_sees_nothing_during_a_pilot():
    """Falling back to the global flag when nobody is signed in
    told a stranger the route existed: the pilot's 404 became a
    401, which is the one thing a 404 is for."""
    with flask_app.app_context():
        _on()
        _limit(codes=[HEAD], verticals=[])
    try:
        anon = flask_app.test_client()      # no session at all
        assert anon.get('/api/account-check/queue').status_code == 404
        assert anon.get('/accounts/check').status_code == 404
    finally:
        with flask_app.app_context():
            _limit()


def test_somebody_signed_in_but_outside_the_pilot_sees_404():
    with flask_app.app_context():
        _on()
        _limit(codes=[HEAD], verticals=[])
    try:
        client = flask_app.test_client()
        with client.session_transaction() as s:
            s['emp_code'] = REP
        assert client.get('/accounts/check').status_code == 404
        assert client.get(
            '/api/account-check/queue').status_code == 404
    finally:
        with flask_app.app_context():
            _limit()
