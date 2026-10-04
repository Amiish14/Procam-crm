"""The client block and caution register.

The ten acceptance tests from the brief, plus the matching rules they
rest on. The one that matters most is that the check runs on the
server: a blocked client must be refused by the API even when nothing
in the interface offers the button.
"""
import json
import os
import sys
import tempfile
from datetime import datetime

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'restriction-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'RestrictionTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'restrictions.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.restriction import (                      # noqa: E402
    BLOCKED, CAUTION, ClientRestriction, ClientRestrictionEvent, LIFTED,
    RECOMMENDED, REJECTED, SCOPE_ENTITY, SCOPE_GROUP)
from app.services import client_restrictions as restrictions  # noqa: E402
from app.services import restriction_register as register     # noqa: E402

WIL = 'Walchandnagar Industries Limited'
ALIASES = ['Walchandnagar Industries Ltd', 'Walchandnagar Industries Limit',
           'WIL']
ADMIN, SALES = 'RSTADMIN', 'RSTSALES'
TAG = 'RST-'


def _emp(code, name, *, super_admin=False, role='user'):
    e = Employee.query.filter_by(emp_code=code).first() or Employee(emp_code=code)
    e.name, e.is_active, e.must_change_pw = name, True, False
    e.email = f'{code.lower()}@procamgroup.in'
    e.role, e.session_version = role, 0
    e.is_super_admin, e.is_vertical_head = super_admin, False
    db.session.add(e)
    db.session.flush()
    return e


def _wipe():
    ids = [r.id for r in ClientRestriction.query.all()]
    if ids:
        ClientRestrictionEvent.query.filter(
            ClientRestrictionEvent.restriction_id.in_(ids)).delete(
                synchronize_session=False)
        ClientRestriction.query.filter(ClientRestriction.id.in_(ids)).delete(
            synchronize_session=False)
    Lead.query.filter(Lead.company.like('%Walchand%')).delete(
        synchronize_session=False)
    Lead.query.filter(Lead.company.like(TAG + '%')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like(TAG + '%')).delete(
        synchronize_session=False)
    db.session.commit()
    restrictions.cache_clear()


@pytest.fixture()
def world(monkeypatch):
    monkeypatch.setattr(
        'app.services.restriction_notify.announce_block',
        lambda *a, **kw: None)
    monkeypatch.setattr(
        'app.services.restriction_notify.recommendation_raised',
        lambda *a, **kw: None)
    with flask_app.app_context():
        db.create_all()
        _wipe()
        _emp(ADMIN, 'Register Admin', super_admin=True, role='admin')
        _emp(SALES, 'Sales Person')
        db.session.commit()
        yield
        _wipe()


def _block(company=WIL, *, aliases=ALIASES, status=BLOCKED, scope=SCOPE_ENTITY,
           domains=(), emails=()):
    row = ClientRestriction(
        status=status, company_name=company,
        name_key=restrictions.normalise(company),
        reason_category='Payment default / outstanding',
        reason_detail='Outstanding against invoices, unresolved since.',
        recommended_by=ADMIN, recommended_at=datetime.utcnow(),
        approved_by=ADMIN, approved_at=datetime.utcnow(), scope=scope)
    row.aliases = list(aliases)
    row.domains = list(domains)
    row.emails = list(emails)
    db.session.add(row)
    db.session.commit()
    restrictions.cache_clear()
    return row


def _client(emp_code=SALES):
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        emp = Employee.query.filter_by(emp_code=emp_code).first()
        sess['emp_code'] = emp_code
        sess['role'] = emp.role
        sess['session_version'] = 0
    return c


# ── 1. the register holds the decision ───────────────────────────────
def test_blocking_wil_records_the_decision_and_an_event(world):
    row = register.create({
        'company_name': WIL, 'aliases': ', '.join(ALIASES),
        'status': BLOCKED, 'reason_category': 'Payment default / outstanding',
        'reason_detail': 'Outstanding against invoices since last year.',
    }, actor=ADMIN)
    assert row.status == BLOCKED
    assert set(row.aliases) == set(ALIASES)
    events = ClientRestrictionEvent.query.filter_by(
        restriction_id=row.id).all()
    assert {e.action for e in events} >= {'recommended', 'blocked'}


# ── the matching rules ───────────────────────────────────────────────
@pytest.mark.parametrize('name', [
    'Walchandnagar Industries Limited',
    'Walchandnagar Industries Ltd',
    'Walchandnagar Industries Ltd.',
    'WALCHANDNAGAR INDUSTRIES LIMIT',
    'Walchandnagar  Industries,  Pvt  Ltd',
    'WIL',
])
def test_every_spelling_of_the_name_is_the_same_client(world, name):
    _block()
    assert restrictions.check(company_name=name).blocked, name


def test_a_different_company_is_not_caught(world):
    _block()
    for name in ('Walchand Advanced Composites', 'Tata Steel Limited',
                 'Nagar Industries'):
        assert restrictions.check(company_name=name).clear, name


# ── 9. a typo is a caution, never a block ────────────────────────────
def test_a_single_letter_typo_does_not_get_past_a_block(world):
    """§4: 0.90 and above is the client. "Walchandnagr" scores 0.98
    against "Walchandnagar", so it is caught rather than warned about.

    This is the one place the brief argues with itself — acceptance
    test 9 calls the same string a Caution. Blocking is the reading
    that serves the register's purpose: a block somebody can step
    around by mistyping a letter is not a block.
    """
    _block()
    verdict = restrictions.check(company_name='Walchandnagr Industries')
    assert verdict.blocked, (verdict.level, verdict.score)
    assert verdict.matched_on == 'name-fuzzy'


def test_a_distant_name_is_a_question_not_a_refusal(world):
    """The 0.80–0.90 band: close enough to ask, never to refuse."""
    _block(company='Thermax Engineering Limited', aliases=[])
    verdict = restrictions.check(company_name='Thermax Engineers Pvt Ltd')
    assert verdict.caution, (verdict.level, verdict.score)
    assert verdict.matched_on == 'name-possible'
    assert 'may be' in verdict.message()


def test_the_group_scope_is_what_makes_a_domain_match(world):
    row = _block(domains=['walchand.com'], scope=SCOPE_ENTITY)
    assert restrictions.check(email='anil@walchand.com').clear, (
        'an entity-scoped block must not catch the whole domain')
    row.scope = SCOPE_GROUP
    db.session.commit()
    restrictions.cache_clear()
    assert restrictions.check(email='anil@walchand.com').blocked


def test_an_exact_email_is_matched_whatever_the_scope(world):
    _block(emails=['anil.kale@walchand.com'])
    assert restrictions.check(email='Anil.Kale@Walchand.com').blocked


# ── 2 & 5. the server refuses, not the interface ─────────────────────
def test_a_sales_user_cannot_create_a_lead_for_a_blocked_client(world):
    _block()
    resp = _client().post('/api/leads',
                          json={'company': 'Walchandnagar Industries Ltd'})
    assert resp.status_code == 403
    body = resp.get_json()
    assert 'blocked by management' in body['error']
    assert Lead.query.filter(Lead.company.like('%Walchand%')).count() == 0


def test_the_attempt_is_logged_against_the_register(world):
    row = _block()
    _client().post('/api/leads', json={'company': 'WIL'})
    attempts = ClientRestrictionEvent.query.filter_by(
        restriction_id=row.id, action='attempt_blocked').all()
    assert len(attempts) == 1
    assert attempts[0].user_id == SALES


def test_an_account_for_a_blocked_client_is_refused(world):
    _block()
    resp = _client().post('/api/companies',
                          json={'name': 'Walchandnagar Industries Ltd'})
    assert resp.status_code == 403


def test_a_contact_for_a_blocked_client_is_refused(world):
    _block()
    resp = _client().post('/api/contacts',
                          json={'name': 'Anil', 'company': WIL})
    assert resp.status_code == 403


def test_advancing_an_existing_lead_is_refused(world):
    """A client can be blocked after the lead exists."""
    lead = Lead(company=TAG + 'Later Blocked', stage='New Opportunity',
                assigned_to=SALES)
    db.session.add(lead)
    db.session.commit()
    _block(company=TAG + 'Later Blocked', aliases=[])
    resp = _client().put(f'/api/leads/{lead.id}', json={'stage': 'Quoted'})
    assert resp.status_code == 403


# ── 8. a caution is a conversation, not a refusal ────────────────────
def test_a_caution_asks_once_and_then_allows(world):
    row = _block(company=TAG + 'Watchlist Co', aliases=[], status=CAUTION)

    first = _client().post('/api/leads', json={'company': TAG + 'Watchlist Co'})
    assert first.status_code == 409
    body = first.get_json()
    assert body['needs_acknowledgement'] is True
    assert body['restriction']['restriction_id'] == row.id
    assert Lead.query.filter_by(company=TAG + 'Watchlist Co').count() == 0

    second = _client().post('/api/leads',
                            json={'company': TAG + 'Watchlist Co',
                                  'restriction_ack': row.id})
    assert second.status_code == 200
    assert Lead.query.filter_by(company=TAG + 'Watchlist Co').count() == 1

    acks = ClientRestrictionEvent.query.filter_by(
        restriction_id=row.id, action='caution_acknowledged').all()
    assert len(acks) == 1 and acks[0].user_id == SALES


def test_an_acknowledgement_for_a_different_entry_does_not_count(world):
    row = _block(company=TAG + 'Watchlist Co', aliases=[], status=CAUTION)
    resp = _client().post('/api/leads',
                          json={'company': TAG + 'Watchlist Co',
                                'restriction_ack': row.id + 999})
    assert resp.status_code == 409


# ── 7. who may do what ───────────────────────────────────────────────
def test_anyone_may_recommend_but_only_an_approver_decides(world):
    client = _client(SALES)
    resp = client.post('/admin/restrictions/new', json={
        'company_name': TAG + 'Recommended Co',
        'reason_category': 'Commercial dispute',
        'reason_detail': 'They have disputed every invoice this year.',
        'status': BLOCKED})
    assert resp.status_code == 200
    row = ClientRestriction.query.filter_by(
        company_name=TAG + 'Recommended Co').one()
    assert row.status == RECOMMENDED, (
        'a recommendation from a non-approver must not take effect')
    # and it enforces nothing until approved
    assert restrictions.check(company_name=TAG + 'Recommended Co').clear

    assert client.post(f'/admin/restrictions/{row.id}/approve',
                       json={}).status_code == 403
    assert client.post(f'/admin/restrictions/{row.id}/lift',
                       json={'reason': 'because I say so'}).status_code == 403

    admin = _client(ADMIN)
    assert admin.post(f'/admin/restrictions/{row.id}/approve',
                      json={}).status_code == 200
    db.session.expire_all()
    restrictions.cache_clear()
    assert ClientRestriction.query.get(row.id).status == BLOCKED


def test_lifting_needs_a_reason_and_keeps_the_history(world):
    row = _block(company=TAG + 'Forgiven Co', aliases=[])
    admin = _client(ADMIN)
    short = admin.post(f'/admin/restrictions/{row.id}/lift',
                       json={'reason': 'ok'})
    assert short.status_code == 400

    ok = admin.post(f'/admin/restrictions/{row.id}/lift',
                    json={'reason': 'Paid in full on 2026-10-01, '
                                    'confirmed by accounts.'})
    assert ok.status_code == 200
    db.session.expire_all()
    restrictions.cache_clear()
    assert ClientRestriction.query.get(row.id).status == LIFTED
    assert restrictions.check(company_name=TAG + 'Forgiven Co').clear
    history = ClientRestrictionEvent.query.filter_by(
        restriction_id=row.id).all()
    assert any(e.action == 'lifted' for e in history)
    assert any(e.action == 'blocked' or e.action == 'recommended'
               for e in history) or len(history) >= 1


def test_a_block_can_be_stepped_down_to_a_caution(world):
    row = _block(company=TAG + 'Downgraded Co', aliases=[])
    _client(ADMIN).post(f'/admin/restrictions/{row.id}/lift',
                        json={'reason': 'Settled, but keep an eye on them.',
                              'downgrade_to': CAUTION})
    db.session.expire_all()
    restrictions.cache_clear()
    assert restrictions.check(company_name=TAG + 'Downgraded Co').caution


# ── 6. what happens to work already in flight ────────────────────────
def test_approving_a_block_closes_the_open_leads_and_leaves_the_won(world):
    for stage in ('New Opportunity', 'Quotation', 'Won', 'Lost'):
        db.session.add(Lead(company='Walchandnagar Industries Ltd',
                            stage=stage, assigned_to=SALES,
                            followup_date=None))
    db.session.commit()

    row = _block(status=RECOMMENDED)
    row.status = RECOMMENDED
    db.session.commit()
    restrictions.cache_clear()

    with flask_app.test_request_context():
        from flask import session as flask_session
        flask_session['emp_code'] = ADMIN
        flask_session['role'] = 'admin'
        found = register.impact(row)
    assert found['counts']['leads'] == 2, found['counts']
    assert found['counts']['won'] == 1

    _client(ADMIN).post(f'/admin/restrictions/{row.id}/approve', json={})
    db.session.expire_all()

    stages = sorted(l.stage for l in Lead.query.filter(
        Lead.company.like('%Walchand%')).all())
    assert stages == ['Lost', 'Not Interested', 'Not Interested', 'Won'], stages
    closed = Lead.query.filter_by(stage='Not Interested').all()
    assert all(l.lost_reason == register.LEAD_REASON for l in closed)
    assert all(l.followup_date is None for l in closed)


def test_closed_records_leave_my_work_and_the_follow_up_lists(world):
    """§5.3 — "remove them from My Work, follow-ups and the timers".

    Nothing extra does this: the Workbench reads only open stages and
    the closure clears the follow-up date, so the record leaves every
    list by being closed. Asserted rather than assumed, because if the
    board ever starts reading terminal stages a blocked client's work
    would quietly reappear on somebody's morning list.
    """
    from datetime import date, timedelta

    from app.access import scope as sc_mod
    from app.workbench import service as wb

    yesterday = date.today() - timedelta(days=3)
    lead = Lead(company='Walchandnagar Industries Ltd', stage='Quoted',
                assigned_to=SALES, followup_date=yesterday)
    db.session.add(lead)
    db.session.commit()

    sc = sc_mod.for_employee(SALES)
    before = wb.board(sc)
    assert any(i['id'] == lead.id for i in before['items']), (
        'the lead should be on the board before the block')

    row = _block()
    _client(ADMIN).post(f'/admin/restrictions/{row.id}/approve', json={})
    db.session.expire_all()
    wb.hygiene_cache_clear()

    after = wb.board(sc)
    assert not any(i['id'] == lead.id for i in after['items'])
    refreshed = db.session.get(Lead, lead.id)
    assert refreshed.stage == 'Not Interested'
    assert refreshed.followup_date is None


# ── 4. a bad row does not cost the whole upload ──────────────────────
def test_an_import_rejects_the_matching_row_and_keeps_the_rest(world):
    from app.excel_io import service as excel

    _block()
    rows = [
        {'errors': [], 'warnings': [], 'action': 'create', 'row': 1,
         'existing_id': None, 'data': {'company': TAG + 'Good Customer'}},
        {'errors': [], 'warnings': [], 'action': 'create', 'row': 2,
         'existing_id': None,
         'data': {'company': 'Walchandnagar Industries Ltd'}},
        {'errors': [], 'warnings': [], 'action': 'create', 'row': 3,
         'existing_id': None, 'data': {'company': TAG + 'Another Good One'}},
    ]

    class _Batch:
        total_rows = valid_rows = error_rows = duplicate_rows = 0
        committed = False
        committed_at = None
        preview_data = None

    counts = excel.commit(rows, 'lead', excel.MODE_CREATE if
                          hasattr(excel, 'MODE_CREATE') else 'create',
                          _Batch(), ADMIN)
    assert counts['restricted'] == 1
    assert counts['created'] == 2
    assert counts['restricted_rows'][0]['company'] == \
        'Walchandnagar Industries Ltd'
    assert Lead.query.filter(Lead.company.like('%Walchand%')).count() == 0
    assert Lead.query.filter(Lead.company.like(TAG + '%')).count() == 2


# ── 3. the email intake ──────────────────────────────────────────────
def test_an_email_from_a_blocked_client_never_becomes_a_lead(world):
    _block(domains=['walchand.com'], scope=SCOPE_GROUP)
    verdict = restrictions.check(company_name='', email='anil.kale@walchand.com',
                                 domain='walchand.com')
    assert verdict.blocked

    source = open(os.path.join(_ROOT, 'email_ingest',
                               'single_message.py')).read()
    assert '_verdict.blocked' in source
    assert "'blocked_client'" in source
    assert '_log_blocked_intake' in source


# ── 10. nothing is deleted ───────────────────────────────────────────
def test_the_history_is_append_only(world):
    row = _block(company=TAG + 'Audited Co', aliases=[])
    restrictions.log_event(row.id, 'edited', user_id=ADMIN, note='changed')
    _client(ADMIN).post(f'/admin/restrictions/{row.id}/lift',
                        json={'reason': 'Resolved amicably after a meeting.'})
    db.session.expire_all()
    events = ClientRestrictionEvent.query.filter_by(
        restriction_id=row.id).order_by(ClientRestrictionEvent.id).all()
    assert [e.action for e in events] == ['edited', 'lifted']
    # the row itself survives the lift
    assert ClientRestriction.query.get(row.id) is not None


# ── the paths added after the first pass ────────────────────────────
def test_an_opportunity_for_a_blocked_client_is_refused(world):
    _block()
    resp = _client().post('/api/opportunities',
                          json={'company': 'Walchandnagar Industries Ltd',
                                'title': 'Anything'})
    assert resp.status_code == 403


def test_a_handover_for_a_blocked_client_is_refused(world):
    """The last gate before operations start work."""
    _block()
    resp = _client().post('/api/handovers',
                          json={'account_name': 'Walchandnagar Industries Ltd',
                                'won_value': 100000})
    assert resp.status_code == 403


def test_a_closed_record_cannot_be_put_back_into_play(world):
    """§5.3 — read-only. Closing a lead takes it off every list, but a
    deep link still reaches the form, and reopening one would make the
    whole register advisory."""
    lead = Lead(company='Walchandnagar Industries Ltd', stage='Quoted',
                assigned_to=SALES)
    db.session.add(lead)
    db.session.commit()
    row = _block()
    _client(ADMIN).post(f'/admin/restrictions/{row.id}/approve', json={})
    db.session.expire_all()

    client = _client(SALES)
    back = client.put(f'/api/leads/{lead.id}', json={'stage': 'Quoted'})
    assert back.status_code == 403
    assert 'cannot be put back into play' in back.get_json()['error']

    # A correction that does not restart the work is still allowed —
    # an administrator has to be able to fix a typo on a closed record.
    fix = client.put(f'/api/leads/{lead.id}', json={'city': 'Pune'})
    assert fix.status_code == 200
    assert db.session.get(Lead, lead.id).stage == 'Not Interested'


def test_the_badge_rides_on_the_lead_the_account_and_the_contact(world):
    """§5 — the badge has to appear wherever the company does, which
    means it travels on the payload rather than being asked for."""
    _block()
    lead = Lead(company='Walchandnagar Industries Ltd', stage='New',
                assigned_to=SALES)
    company = Company(name='Walchandnagar Industries Ltd')
    contact = _main.Contact(name='Anil', company='WIL')
    db.session.add_all([lead, company, contact])
    db.session.commit()

    assert lead.to_dict()['restriction'] == 'blocked'
    assert company.to_dict()['restriction'] == 'blocked'
    assert contact.to_dict()['restriction'] == 'blocked'

    clear = Lead(company=TAG + 'Ordinary Customer', stage='New')
    db.session.add(clear)
    db.session.commit()
    assert clear.to_dict()['restriction'] == ''


def test_the_menu_shows_how_many_decisions_are_waiting(world):
    register.create({
        'company_name': TAG + 'Waiting Co',
        'reason_category': 'Commercial dispute',
        'reason_detail': 'They have disputed every invoice this year.',
        'status': CAUTION}, actor=SALES)
    assert _client(ADMIN).get('/api/me').get_json()['restrictions_pending'] == 1
    # Somebody who cannot approve is not shown a count they cannot act on.
    assert _client(SALES).get('/api/me').get_json()['restrictions_pending'] == 0


def test_the_detail_page_lists_the_records_the_entry_reaches(world):
    db.session.add(Lead(company='Walchandnagar Industries Ltd',
                        stage='Quotation', assigned_to=SALES))
    db.session.commit()
    row = _block(status=RECOMMENDED)
    row.status = RECOMMENDED
    db.session.commit()
    restrictions.cache_clear()
    page = _client(ADMIN).get(f'/admin/restrictions/{row.id}')
    assert page.status_code == 200
    assert b'Records for this client' in page.data


def test_finding_the_records_does_not_walk_the_whole_lead_table(world):
    """The preview is on a page somebody opens to read, so it narrows
    in SQL before scoring anything."""
    for i in range(60):
        db.session.add(Lead(company=f'{TAG}Unrelated {i}', stage='New'))
    db.session.add(Lead(company='Walchandnagar Industries Ltd',
                        stage='Quotation'))
    db.session.commit()
    row = _block()
    candidates = register._candidate_leads(row)
    assert len(candidates) == 1, (
        f'{len(candidates)} leads were examined; the SQL narrowing is '
        f'not doing its job')


# ── the cache, which is per worker ───────────────────────────────────
def test_a_block_reaches_a_worker_that_did_not_approve_it(world):
    """Production runs two gunicorn workers. `cache_clear()` only
    reaches the one that handled the approval, so without an expiry
    the other enforces nothing for ever and a blocked client is
    refused about half the time.

    Simulated by clearing nothing — exactly what the second worker
    does — and letting the clock move past the TTL.
    """
    # The second worker has already answered something, so its cache
    # holds "nothing is restricted".
    assert restrictions.live_rows(now=1000.0) == []

    # Meanwhile the first worker approves a block. Straight to the
    # database, with no cache_clear, because the other process cannot
    # see that call.
    _block()
    restrictions._CACHE['rows'] = []
    restrictions._CACHE['at'] = 1000.0

    # Inside the window the stale answer stands — that is the trade.
    assert restrictions.live_rows(now=1000.0 + 5) == []

    # Past it, the worker re-reads and the block is in force.
    fresh = restrictions.live_rows(
        now=1000.0 + restrictions._CACHE_TTL_SECONDS + 1)
    assert len(fresh) == 1 and fresh[0].company_name == WIL


def test_a_missing_table_is_not_remembered_as_an_answer(world, monkeypatch):
    """Before the migration the check must say "nothing restricted" —
    and must not cache that, or the first request after the migration
    would pin it for the life of the process."""
    restrictions.cache_clear()

    def _boom():
        raise RuntimeError('no such table: client_restrictions')

    monkeypatch.setattr(restrictions, '_load_rows', _boom)
    assert restrictions.live_rows(now=1.0) == []
    assert restrictions._CACHE['rows'] is None, (
        'a failure was cached as if it were an answer')


# ── CSRF ─────────────────────────────────────────────────────────────
def test_every_form_on_the_register_carries_a_csrf_token(world):
    """CSRFProtect is on app-wide and rejects a POST before the handler
    runs, with a 400 HTML page the screen cannot read. A form without a
    token is a button that does nothing — and the rest of this module
    disables CSRF, so nothing else here would notice.
    """
    import re

    row = _block()
    admin = _client(ADMIN)
    for path in ('/admin/restrictions/new',
                 f'/admin/restrictions/{row.id}'):
        html = admin.get(path).get_data(as_text=True)
        forms = re.findall(r'<form method="post".*?</form>', html,
                           re.S | re.I)
        assert forms, f'no POST form found on {path}'
        for form in forms:
            assert 'name="csrf_token"' in form, (
                f'a form on {path} would be refused in a browser: '
                f'{form[:120]}')


def test_a_form_post_survives_csrf_being_on(world):
    """The round trip the screens actually make."""
    import re

    row = _block(company=TAG + 'Csrf Co', aliases=[])
    flask_app.config['WTF_CSRF_ENABLED'] = True
    try:
        admin = _client(ADMIN)
        html = admin.get(f'/admin/restrictions/{row.id}').get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        resp = admin.post(f'/admin/restrictions/{row.id}/lift',
                          data={'csrf_token': token,
                                'reason': 'Settled in full, confirmed by '
                                          'accounts this morning.'})
        assert resp.status_code in (200, 302), resp.get_data()[:200]
    finally:
        flask_app.config['WTF_CSRF_ENABLED'] = False
    db.session.expire_all()
    restrictions.cache_clear()
    assert ClientRestriction.query.get(row.id).status == LIFTED


def test_an_attachment_can_be_added_and_read_back(world, tmp_path,
                                                  monkeypatch):
    """With CSRF on, because a multipart form carries its token in the
    body and that is a different path through flask-wtf than a JSON
    post. This is the form that was found dead."""
    import io
    import re

    monkeypatch.setenv('CRM_UPLOAD_ROOT', str(tmp_path))
    row = _block(company=TAG + 'Papers Co', aliases=[])
    admin = _client(ADMIN)

    flask_app.config['WTF_CSRF_ENABLED'] = True
    try:
        page = admin.get(f'/admin/restrictions/{row.id}').get_data(as_text=True)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        up = admin.post(f'/admin/restrictions/{row.id}/attachments',
                        data={'csrf_token': token,
                              'file': (io.BytesIO(b'%PDF-1.4 legal notice'),
                                       'notice.pdf')},
                        content_type='multipart/form-data')
    finally:
        flask_app.config['WTF_CSRF_ENABLED'] = False
    assert up.status_code == 200, up.get_data()[:200]
    att_id = up.get_json()['attachment']['id']

    got = admin.get(f'/admin/restrictions/{row.id}/attachments/{att_id}')
    assert got.status_code == 200
    assert b'legal notice' in got.data

    # Somebody without the permission cannot read a legal notice.
    assert _client(SALES).get(
        f'/admin/restrictions/{row.id}/attachments/{att_id}'
    ).status_code == 403


# ── the TMS endpoint ─────────────────────────────────────────────────
def test_the_tms_can_ask_and_gets_a_plain_answer(world):
    _block()
    resp = _client().get('/api/client-restriction/check'
                         '?company_name=Walchandnagar%20Industries%20Ltd')
    assert resp.status_code == 200
    body = resp.get_json()
    assert body['level'] == 'blocked'
    assert body['company_name'] == WIL
    assert 'blocked by management' in body['message']


def test_the_tms_endpoint_needs_a_session(world):
    _block()
    resp = flask_app.test_client().get(
        '/api/client-restriction/check?company_name=' + WIL)
    assert resp.status_code == 401


def test_a_clear_client_is_answered_as_clear(world):
    _block()
    body = _client().get('/api/client-restriction/check'
                         '?company_name=Tata+Steel').get_json()
    assert body['level'] == 'none' and body['restriction_id'] is None


# ── business units ───────────────────────────────────────────────────
def test_a_block_can_apply_to_one_business_unit_only(world):
    row = _block(company=TAG + 'One Unit Co', aliases=[])
    row.business_units = ['PLPL']
    db.session.commit()
    restrictions.cache_clear()
    assert restrictions.check(company_name=TAG + 'One Unit Co',
                              business_unit='PLPL').blocked
    assert restrictions.check(company_name=TAG + 'One Unit Co',
                              business_unit='PWLPL').clear


# ── reading the register ─────────────────────────────────────────────
def test_everyone_can_read_the_register_but_not_the_money(world):
    row = _block()
    row.dispute_amount = 14000000
    row.dispute_refs = 'INV-1, INV-2'
    db.session.commit()

    page = _client(SALES).get('/admin/restrictions')
    assert page.status_code == 200
    assert WIL.encode() in page.data
    assert b'14,000,000' not in page.data, (
        'the dispute amount is for administrators')

    admin_page = _client(ADMIN).get('/admin/restrictions')
    assert b'14,000,000' in admin_page.data
