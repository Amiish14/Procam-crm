"""
CRM-AUTO-ASSIGN-01 — matching first, then who owns what.

The measured rate on 30 days of real mail was 48 per cent: two rules
did all the work and three more existed and never fired. Everything
a mail cannot be matched to is a lead whose owner is a fallback
rather than somebody who knows the customer, so the match rate is
the number that matters.

The assignment rules then guarantee the other half of it: no lead
without an owner, no lead owned by somebody who has left, and a
record of which rule chose and what it matched.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'autoassign-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'AutoAssignTest12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'autoassign.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Contact, Employee, Lead = (_main.Company, _main.Contact,
                                    _main.Employee, _main.Lead)
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.assignment import (AccountVerticalOwner,  # noqa: E402
                                   OwnerConfirmation, OwnerReplacement,
                                   SenderAccountLink)
from app.models.email_admin import VerticalHead           # noqa: E402
from app.services import account_match, auto_assign, flags  # noqa: E402

TAG = 'AA '
HEAD_HT, HEAD_FF, BACKSTOP = 'AAHEADHT', 'AAHEADFF', 'DIR12010'
REP, LEAVER, SUCCESSOR = 'AAREP', 'AALEAVER', 'AASUCC'


def _emp(code, name, *, active=True):
    e = Employee.query.filter_by(emp_code=code).first() or Employee(emp_code=code)
    e.name, e.is_active, e.must_change_pw = name, active, False
    e.email = f'{code.lower()}@procamgroup.in'
    e.role, e.session_version = 'user', 0
    e.is_super_admin, e.is_vertical_head = False, False
    db.session.add(e)
    db.session.flush()
    return e


def _wipe():
    for model in (SenderAccountLink, AccountVerticalOwner,
                  OwnerReplacement, OwnerConfirmation):
        model.query.delete(synchronize_session=False)
    ids = [l.id for l in Lead.query.filter(Lead.company.like(TAG + '%'))]
    if ids:
        Lead.query.filter(Lead.id.in_(ids)).delete(synchronize_session=False)
    Contact.query.filter(Contact.email.like('%@aaclient.com')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like(TAG + '%')).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture()
def world(monkeypatch):
    monkeypatch.setenv('FEATURE_AUTO_ASSIGN', 'true')
    flags.cache_clear()
    with flask_app.app_context():
        db.create_all()
        _wipe()
        _emp(HEAD_HT, 'Head Heavy')
        _emp(HEAD_FF, 'Head Forwarding')
        _emp(REP, 'A Rep')
        _emp(LEAVER, 'Departed Person', active=False)
        _emp(SUCCESSOR, 'The Successor')
        _emp(BACKSTOP, 'The Backstop')
        for vertical, head in (('Heavy Transport', HEAD_HT),
                               ('Freight Forwarding', HEAD_FF)):
            row = VerticalHead.query.filter_by(vertical=vertical).first()
            if row is None:
                row = VerticalHead(vertical=vertical)
                db.session.add(row)
            row.head_emp_code, row.active = head, True

        acct = Company(name=TAG + 'Adani Ports Pvt Ltd', is_active=True,
                       pic_emp_code=REP, website='https://www.aaclient.com',
                       email_domains=['listed.example'])
        db.session.add(acct)
        db.session.flush()
        db.session.commit()
        from app.services import vertical_heads as vh
        vh.cache_clear()
        yield {'account': acct.id}
        _wipe()
        flags.cache_clear()


def _lead(**kw):
    lead = Lead(company=kw.pop('company', TAG + 'Some Lead'),
                stage=kw.pop('stage', 'New Opportunity'),
                created_at=datetime.utcnow(), **kw)
    db.session.add(lead)
    db.session.flush()
    return lead


# ── matching ─────────────────────────────────────────────────────────
def test_free_mail_never_matches_on_the_domain(world):
    """Two customers on gmail.com are two customers. A domain rule
    firing there would collect the entire internet onto one account."""
    with flask_app.app_context():
        acct = db.session.get(Company, world['account'])
        acct.email_domains = ['gmail.com']
        db.session.commit()
        found, why = account_match.resolve('someone@gmail.com')
    assert found is None and why == 'unmapped'


def test_free_mail_still_matches_on_the_exact_contact(world):
    with flask_app.app_context():
        db.session.add(Contact(name='A Buyer', email='buyer@aaclient.com',
                               company_id=world['account']))
        db.session.commit()
        # a free-mail contact is matched by address, which is exact
        db.session.add(Contact(name='G Buyer', email='gbuyer@gmail.com',
                               company_id=world['account']))
        db.session.commit()
        found, why = account_match.resolve('gbuyer@gmail.com')
    assert found is not None and why == 'contact email'


def test_the_website_rule_matches_on_the_domain_not_a_substring(world):
    """`%adani%` matched adani.com, adaniports.com and notadani.com
    alike, which is why the rule could never be trusted."""
    with flask_app.app_context():
        found, why = account_match.resolve('buyer@aaclient.com')
        assert found is not None and why == 'account website domain'
        # a substring is not a match
        other, why2 = account_match.resolve('buyer@notaaclient.com')
    assert other is None, why2


def test_a_subdomain_of_the_website_matches(world):
    with flask_app.app_context():
        found, why = account_match.resolve('buyer@mail.aaclient.com')
    assert found is not None and why == 'account website domain'


def test_the_company_name_matches_once_normalised(world):
    with flask_app.app_context():
        # The fixture's account is "AA Adani Ports Pvt Ltd"; the mail
        # says "Limited, India". Pvt/Ltd/Limited/India and the comma
        # all have to come off for these to meet.
        found, why = account_match.resolve(
            'nobody@unknown-domain.example',
            company_name='AA Adani Ports Limited, India')
    assert found is not None and why == 'company name'


def test_two_accounts_that_normalise_alike_match_neither(world):
    """A merge is a decision somebody makes, not a match to guess."""
    with flask_app.app_context():
        db.session.add(Company(name=TAG + 'Adani Ports Ltd',
                               is_active=True))
        db.session.commit()
        found, why = account_match.resolve(
            'nobody@unknown-domain.example',
            company_name='AA Adani Ports Pvt Ltd')
    assert found is None and why == 'unmapped'


def test_a_correction_teaches_the_matcher(world):
    with flask_app.app_context():
        before, _ = account_match.resolve('buyer@brandnew.example')
        assert before is None

        account_match.learn('buyer@brandnew.example', world['account'],
                            actor='AAREP', reason='linked by hand')
        found, why = account_match.resolve('buyer@brandnew.example')
        assert found is not None and why == 'learned email'

        # and the domain, so the next person at that company matches
        other, why2 = account_match.resolve('colleague@brandnew.example')
    assert other is not None and why2 == 'learned domain'


def test_a_correction_on_free_mail_teaches_the_address_only(world):
    with flask_app.app_context():
        account_match.learn('someone@gmail.com', world['account'],
                            actor='AAREP')
        found, _why = account_match.resolve('someone@gmail.com')
        assert found is not None
        other, why = account_match.resolve('different@gmail.com')
    assert other is None, why


def test_the_learned_link_is_audited(world):
    from app.models.audit import AuditEvent
    with flask_app.app_context():
        account_match.learn('buyer@audited.example', world['account'],
                            actor='AAREP', reason='linked by hand')
        db.session.commit()
        ev = (AuditEvent.query
              .filter(AuditEvent.action == 'assignment.link_learned')
              .order_by(AuditEvent.id.desc()).first())
    assert ev is not None
    assert ev.actor == 'AAREP'


# ── rule (a): the account's owner ────────────────────────────────────
def test_the_vertical_owner_beats_the_account_owner(world):
    with flask_app.app_context():
        db.session.add(AccountVerticalOwner(
            account_id=world['account'], vertical='Freight Forwarding',
            emp_code=HEAD_FF))
        db.session.commit()
        acct = db.session.get(Company, world['account'])
        code, rule, confirm = auto_assign.decide(
            account=acct, vertical='Freight Forwarding')
    assert code == HEAD_FF
    assert rule == auto_assign.R_ACCOUNT_VERTICAL
    assert confirm is False


def test_the_account_owner_is_used_when_the_vertical_has_nobody(world):
    with flask_app.app_context():
        acct = db.session.get(Company, world['account'])
        code, rule, confirm = auto_assign.decide(
            account=acct, vertical='Heavy Transport')
    assert code == REP and rule == auto_assign.R_ACCOUNT
    assert confirm is False


# ── rule (f): the replacement map ────────────────────────────────────
def test_a_departed_owner_is_replaced(world):
    with flask_app.app_context():
        acct = db.session.get(Company, world['account'])
        acct.pic_emp_code = LEAVER
        db.session.add(OwnerReplacement(from_emp_code=LEAVER,
                                        to_emp_code=SUCCESSOR, active=True))
        db.session.commit()
        code, rule, _c = auto_assign.decide(account=acct,
                                            vertical='Heavy Transport')
    assert code == SUCCESSOR
    assert auto_assign.R_REPLACED in rule


def test_a_replacement_who_has_also_left_is_followed_on(world):
    with flask_app.app_context():
        _emp('AAGONE2', 'Also Gone', active=False)
        db.session.add_all([
            OwnerReplacement(from_emp_code=LEAVER, to_emp_code='AAGONE2',
                             active=True),
            OwnerReplacement(from_emp_code='AAGONE2',
                             to_emp_code=SUCCESSOR, active=True)])
        db.session.commit()
        assert auto_assign.replacement_for(LEAVER) == SUCCESSOR


def test_a_replacement_cycle_does_not_hang(world):
    with flask_app.app_context():
        _emp('AACYC', 'Cycle', active=False)
        db.session.add_all([
            OwnerReplacement(from_emp_code=LEAVER, to_emp_code='AACYC',
                             active=True),
            OwnerReplacement(from_emp_code='AACYC', to_emp_code=LEAVER,
                             active=True)])
        db.session.commit()
        assert auto_assign.replacement_for(LEAVER) is None


# ── rule (b): no account, a vertical ─────────────────────────────────
def test_an_unmatched_lead_goes_to_the_head_and_needs_confirming(world):
    with flask_app.app_context():
        code, rule, confirm = auto_assign.decide(
            account=None, vertical='Freight Forwarding')
    assert code == HEAD_FF
    assert rule == auto_assign.R_VERTICAL_HEAD
    assert confirm is True, 'a guess, and the head should be asked'


# ── rule (c): no vertical either ─────────────────────────────────────
def test_with_no_vertical_and_no_account_it_goes_to_the_backstop(world):
    with flask_app.app_context():
        code, rule, confirm = auto_assign.decide(account=None, vertical='')
    assert code == BACKSTOP
    assert rule == auto_assign.R_BACKSTOP
    assert confirm is True


# ── rule (d): never nobody, never somebody who has left ──────────────
def test_a_lead_is_never_left_without_an_owner(world):
    with flask_app.app_context():
        for vertical in ('Heavy Transport', 'Freight Forwarding',
                         'Nonexistent Vertical', ''):
            code, _rule, _c = auto_assign.decide(account=None,
                                                 vertical=vertical)
            assert code, f'{vertical!r} produced no owner'


def test_an_inactive_head_is_not_chosen(world):
    with flask_app.app_context():
        emp = Employee.query.filter_by(emp_code=HEAD_FF).first()
        emp.is_active = False
        db.session.commit()
        from app.services import vertical_heads as vh
        vh.cache_clear()
        code, _rule, _c = auto_assign.decide(account=None,
                                             vertical='Freight Forwarding')
    assert code != HEAD_FF
    assert code == BACKSTOP


# ── rule (e): the audit row ──────────────────────────────────────────
def test_every_automatic_assignment_says_what_it_matched(world):
    from app.models.audit import AuditEvent
    with flask_app.app_context():
        acct = db.session.get(Company, world['account'])
        lead = _lead(procam_vertical='Heavy Transport', company_id=acct.id)
        db.session.commit()

        out = auto_assign.assign_for(lead, account=acct,
                                     sender_email='buyer@aaclient.com',
                                     commit=True)
        assert out and out['emp_code'] == REP

        ev = (AuditEvent.query
              .filter(AuditEvent.action == 'lead.auto_assigned',
                      AuditEvent.entity_id == str(lead.id))
              .order_by(AuditEvent.id.desc()).first())
    assert ev is not None
    assert ev.new_value['rule'] == auto_assign.R_ACCOUNT
    assert ev.new_value['matched_account_id'] == world['account']
    assert ev.new_value['sender'] == 'buyer@aaclient.com'
    assert ev.old_value == {'assigned_to': None}


def test_a_lead_that_already_has_an_active_owner_is_left_alone(world):
    with flask_app.app_context():
        lead = _lead(procam_vertical='Heavy Transport', assigned_to=REP)
        db.session.commit()
        assert auto_assign.assign_for(lead) is None


def test_nothing_happens_with_the_flag_off(world, monkeypatch):
    monkeypatch.setenv('FEATURE_AUTO_ASSIGN', 'false')
    flags.cache_clear()
    with flask_app.app_context():
        lead = _lead(procam_vertical='Heavy Transport')
        db.session.commit()
        assert auto_assign.assign_for(lead) is None
        assert not (lead.assigned_to or '')


# ── confirmations ────────────────────────────────────────────────────
def test_a_guessed_owner_opens_a_confirmation(world):
    with flask_app.app_context():
        lead = _lead(procam_vertical='Freight Forwarding')
        db.session.commit()
        out = auto_assign.assign_for(lead, commit=True)
        assert out['needs_confirmation'] is True
        lead_id = lead.id
        pending = [p.lead_id for p in auto_assign.pending_for(HEAD_FF)]
    assert pending == [lead_id]


def test_a_known_owner_opens_no_confirmation(world):
    with flask_app.app_context():
        acct = db.session.get(Company, world['account'])
        lead = _lead(procam_vertical='Heavy Transport', company_id=acct.id)
        db.session.commit()
        out = auto_assign.assign_for(lead, account=acct, commit=True)
        assert out['needs_confirmation'] is False
        assert auto_assign.pending_for(REP) == []


def test_confirming_closes_it_and_reassigning_moves_the_lead(world):
    with flask_app.app_context():
        lead = _lead(procam_vertical='Freight Forwarding')
        db.session.commit()
        auto_assign.assign_for(lead, commit=True)

        ok, err = auto_assign.answer(lead.id, outcome='reassigned',
                                     by=HEAD_FF, reassign_to=REP)
        assert ok, err
        assert db.session.get(Lead, lead.id).assigned_to == REP
        assert auto_assign.pending_for(HEAD_FF) == []

        again, why = auto_assign.answer(lead.id, outcome='confirmed',
                                        by=HEAD_FF)
    assert again is False and 'already' in why


def test_reassigning_to_somebody_inactive_is_refused(world):
    with flask_app.app_context():
        lead = _lead(procam_vertical='Freight Forwarding')
        db.session.commit()
        auto_assign.assign_for(lead, commit=True)
        ok, why = auto_assign.answer(lead.id, outcome='reassigned',
                                     by=HEAD_FF, reassign_to=LEAVER)
    assert ok is False and 'not active' in why


def test_the_workbench_shows_what_is_waiting_to_be_confirmed(world):
    from app.access import scope as sc_mod
    from app.workbench import service as wb

    with flask_app.app_context():
        lead = _lead(procam_vertical='Freight Forwarding')
        db.session.commit()
        auto_assign.assign_for(lead, commit=True)

        sc = sc_mod.for_employee(HEAD_FF)
        items = wb.owner_confirm_items(sc)
        assert [i['id'] for i in items] == [lead.id]
        assert 'owner_to_confirm' in items[0]['conditions']

        board = wb.board(sc, per_page=200)
        assert board['counts']['owner_to_confirm'] == 1


# ── the morning digest ───────────────────────────────────────────────
def _pending_lead(vertical='Freight Forwarding', *, days_ago=0):
    lead = _lead(procam_vertical=vertical)
    db.session.commit()
    auto_assign.assign_for(lead, commit=True)
    if days_ago:
        from app.models.assignment import OwnerConfirmation
        row = OwnerConfirmation.query.filter_by(lead_id=lead.id).first()
        row.created_at = datetime.utcnow() - timedelta(days=days_ago)
        db.session.commit()
    return lead


def test_one_digest_per_head_not_one_mail_per_lead(world):
    """380 messages in 30 days to two people is a folder nobody
    opens."""
    from app.models.mailops import EmailOutbox
    from app.services import owner_digest

    with flask_app.app_context():
        for _i in range(5):
            _pending_lead()
        before = EmailOutbox.query.count()
        report = owner_digest.run()
        after = EmailOutbox.query.count()

    assert report['sent'] == 1, 'one head, one mail'
    assert after - before == 1
    with flask_app.app_context():
        row = EmailOutbox.query.order_by(EmailOutbox.id.desc()).first()
    assert '5 new client' in row.subject


def test_the_digest_lists_every_lead_with_both_buttons(world):
    from app.models.mailops import EmailOutbox
    from app.services import owner_digest

    with flask_app.app_context():
        lead = _pending_lead()
        owner_digest.run()
        row = EmailOutbox.query.order_by(EmailOutbox.id.desc()).first()
        lead_id = lead.id
    assert f'/owner-confirm?lead={lead_id}&amp;do=confirm' in row.html \
        or f'/owner-confirm?lead={lead_id}&do=confirm' in row.html
    assert 'Yes, mine' in row.html
    assert 'Give it to somebody else' in row.html


def test_a_head_with_nothing_waiting_gets_no_mail(world):
    from app.services import owner_digest
    with flask_app.app_context():
        report = owner_digest.run()
    assert report['sent'] == 0


def test_nothing_is_sent_with_the_flag_off(world, monkeypatch):
    from app.services import owner_digest
    with flask_app.app_context():
        _pending_lead()
    monkeypatch.setenv('FEATURE_AUTO_ASSIGN', 'false')
    flags.cache_clear()
    with flask_app.app_context():
        report = owner_digest.run()
    assert report['sent'] == 0
    assert 'off' in report['skipped']


def test_anything_unanswered_for_two_days_reaches_the_backstop(world):
    from app.models.mailops import EmailOutbox
    from app.services import owner_digest

    with flask_app.app_context():
        _pending_lead(days_ago=3)
        report = owner_digest.run()
        rows = EmailOutbox.query.order_by(EmailOutbox.id.desc()).all()

    assert report['escalated'] == 1
    assert any(r.user_code == BACKSTOP
               and 'waiting for an owner' in r.subject for r in rows)


def test_escalating_does_not_take_the_lead_away(world):
    """A head on leave should come back to their work, not to
    somebody else having taken it."""
    from app.services import owner_digest
    with flask_app.app_context():
        lead = _pending_lead(days_ago=3)
        owner_digest.run()
        assert db.session.get(Lead, lead.id).assigned_to == HEAD_FF


def test_a_fresh_confirmation_is_not_escalated(world):
    from app.services import owner_digest
    with flask_app.app_context():
        _pending_lead(days_ago=1)
        report = owner_digest.run()
    assert report['escalated'] == 0


def test_answering_takes_it_off_the_next_digest(world):
    from app.services import owner_digest
    with flask_app.app_context():
        lead = _pending_lead()
        auto_assign.answer(lead.id, outcome='confirmed', by=HEAD_FF)
        report = owner_digest.run()
    assert report['sent'] == 0


# ── the screens ──────────────────────────────────────────────────────
def _client(code):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        emp = Employee.query.filter_by(emp_code=code).first()
        s.update(emp_code=code, name=code,
                 role=(emp.role if emp else 'user'), sv=0)
    return c


def test_the_confirm_page_shows_me_only_my_own(world):
    with flask_app.app_context():
        mine = _pending_lead()
        other = _lead(procam_vertical='Heavy Transport')
        db.session.commit()
        auto_assign.assign_for(other, commit=True)
        mine_id, other_id = mine.id, other.id

    page = _client(HEAD_FF).get('/owner-confirm').data.decode()
    # The lead link, not "#id": a small id matches any CSS hex colour
    # on the page, and this one found #2 inside #2c3b4a.
    assert f'?lead={mine_id}"' in page
    assert f'?lead={other_id}"' not in page


def test_confirming_from_the_page_closes_it(world):
    with flask_app.app_context():
        lead = _pending_lead()
        lead_id = lead.id

    res = _client(HEAD_FF).post('/owner-confirm',
                                data={'lead': lead_id,
                                      'outcome': 'confirmed'})
    assert res.status_code == 200
    with flask_app.app_context():
        assert auto_assign.pending_for(HEAD_FF) == []


def test_i_cannot_answer_somebody_elses(world):
    with flask_app.app_context():
        lead = _pending_lead()
        lead_id = lead.id
    res = _client(REP).post('/owner-confirm',
                            data={'lead': lead_id, 'outcome': 'confirmed'})
    assert res.status_code == 403
    with flask_app.app_context():
        assert len(auto_assign.pending_for(HEAD_FF)) == 1


def test_the_replacement_screen_refuses_an_inactive_successor(world):
    with flask_app.app_context():
        emp = Employee.query.filter_by(emp_code=BACKSTOP).first()
        emp.role, emp.is_super_admin = 'admin', True
        db.session.commit()

    c = _client(BACKSTOP)
    # CRM-VERTICAL-SAVE-500-01: the screen is Post/Redirect/Get now,
    # so the refusal is read off the page the redirect lands on.
    res = c.post('/admin/assignment/replacements',
                 data={'from': LEAVER, 'to': 'AAGONE3'},
                 follow_redirects=True)
    assert res.status_code in (200, 403)
    if res.status_code == 200:
        assert b'not an employee on file' in res.data

    with flask_app.app_context():
        _emp('AAGONE3', 'Also Departed', active=False)
        db.session.commit()
    res = c.post('/admin/assignment/replacements',
                 data={'from': LEAVER, 'to': 'AAGONE3'},
                 follow_redirects=True)
    assert b'not active either' in res.data
