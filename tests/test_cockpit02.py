"""
CRM-COCKPIT-02 — the two mandatory dates, the cockpit numbers, and the
reversible log.

The acceptance checks from §9 are named in the tests that cover them,
so a failure says which check it is rather than which function.

One rule this module holds hardest: no invented dates. A deadline the
CRM could not read must stay empty and appear on a tile, because a
guessed deadline looks like a fact and is acted on as one.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'cockpit02-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Cockpit02Test12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'cockpit02.db'))

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead
LeadActivity, LeadNote = _main.LeadActivity, _main.LeadNote
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.models.access import AccessProfile, DataScope    # noqa: E402
from app.models.cockpit02 import (BulkChangeLog,          # noqa: E402
                                  LeadQuoteDueHistory)
from app.services import bulk_log, cockpit02_gate as gate  # noqa: E402
from app.services import followup, last_activity as la    # noqa: E402
from app.services import quote_due as qd                  # noqa: E402
from app.services import quote_due_flow as flow           # noqa: E402
from app.services import screen_scope as ss               # noqa: E402

OWNER, ADMIN, OTHER = 'C02OWN', 'C02ADM', 'C02OTH'
TAG = 'C02-'

#: 04 Nov 2026 18:00 IST, as stored.
DUE = datetime(2026, 11, 4, 12, 30)


def _emp(code, name, *, role='user', admin=False):
    e = Employee.query.filter_by(emp_code=code).first() or \
        Employee(emp_code=code)
    e.name, e.is_active, e.must_change_pw = name, True, False
    e.email = f'{code.lower()}@procamgroup.in'
    e.role, e.session_version = role, 0
    e.is_super_admin, e.is_vertical_head = admin, False
    e.vertical = 'Heavy Transport'
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
        LeadQuoteDueHistory.query.filter(
            LeadQuoteDueHistory.lead_id.in_(ids)).delete(
                synchronize_session=False)
        LeadActivity.query.filter(
            LeadActivity.lead_id.in_(ids)).delete(
                synchronize_session=False)
        LeadNote.query.filter(LeadNote.lead_id.in_(ids)).delete(
            synchronize_session=False)
        Lead.query.filter(Lead.id.in_(ids)).delete(
            synchronize_session=False)
    BulkChangeLog.query.filter(
        BulkChangeLog.batch.like('test.%')).delete(
            synchronize_session=False)
    Company.query.filter(Company.name.like(TAG + '%')).delete(
        synchronize_session=False)
    db.session.commit()


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        _wipe()
        _emp(OWNER, 'C02 Owner')
        _emp(ADMIN, 'C02 Admin', role='admin', admin=True)
        _emp(OTHER, 'C02 Other')
        db.session.commit()
        yield
        _wipe()


def _lead(stage='RFQ Generated', *, company=TAG + 'Portal Co',
          due=None, subject='', body='', followup_date=None,
          owner=OWNER, created=None, source='email', quote_no=None,
          mid=None):
    lead = Lead(company=company, stage=stage, source=source,
                email_message_id=(mid or f'<{company}-{stage}@t>'),
                original_email_subject=subject,
                original_email_body=body,
                quote_due_at=due, followup_date=followup_date,
                assigned_to=owner, procam_vertical='Heavy Transport',
                created_at=created or datetime(2026, 10, 25),
                received_at=created or datetime(2026, 10, 25),
                # CRM-VALUE-01's gate runs before this release's and
                # refuses a move to RFQ Generated without a figure.
                # These tests are about the two dates, so the value is
                # already there — otherwise every one of them would be
                # testing the older gate.
                estimated_value_inr=500000,
                opportunity_value_num=500000, value_basis='estimate',
                quote_no=quote_no)
    db.session.add(lead)
    db.session.commit()
    return lead


def _client(code=OWNER, role='user'):
    c = flask_app.test_client()
    with c.session_transaction() as sess:
        sess['emp_code'] = code
        sess['role'] = role
        sess['session_version'] = 0
        sess['vertical'] = 'Heavy Transport'
    return c


# ══ §2 the quote submission due date ════════════════════════════════
def test_a_portal_subject_gives_the_deadline_to_the_minute():
    r = qd.read('RA- 57103: Indo Tech Transformers Reverse Auction '
                '29 Oct 2026 15:15:00',
                received_at=datetime(2026, 10, 27))
    assert r is not None
    assert qd.shown(r.at) == '29 Oct 2026 15:15'
    assert r.source == qd.PORTAL_SUBJECT


def test_the_stored_moment_is_utc_and_the_shown_one_is_ist():
    """IST in, UTC stored, IST shown.

    A column holding IST would be compared against utcnow() by the
    first reader who did not know — five and a half hours wrong, in
    the direction that says a closed auction is still open.
    """
    r = qd.read('eRFQ- 41688: Sterlite 04 Nov 2026 18:00:00',
                received_at=datetime(2026, 11, 1))
    assert r.at == datetime(2026, 11, 4, 12, 30)
    assert qd.shown(r.at) == '04 Nov 2026 18:00'


@pytest.mark.parametrize('subject,key', [
    ('RA- 57103: Indo Tech', 'RA-57103'),
    ('RA-57103 reminder', 'RA-57103'),
    ('RA 57103', 'RA-57103'),
    ('eRFQ- 41688: Sterlite Power', 'ERFQ-41688'),
    ('e-RFQ 41688 corrigendum', 'ERFQ-41688'),
    ('GEM/2026/B/7654321 bid', 'GEM-7654321'),
])
def test_one_portal_number_however_it_is_written(subject, key):
    """§5.6 — the alert, the reminder and the corrigendum carry the
    same number on three different threads."""
    assert qd.portal_key(subject) == key


def test_a_target_date_is_not_a_deadline():
    """Indo Tech RA-57103 closed on 29 October with a target_date of
    13 November. Reading that as the deadline would have set every
    auction two weeks late and the tile would have been green on the
    morning it closed."""
    assert qd.read('Enquiry',
                   'Target date 13 Nov 2026 for delivery at site.',
                   received_at=datetime(2026, 10, 25)) is None


def test_our_own_validity_is_not_a_deadline():
    assert qd.read('Quotation',
                   'Our offer is valid till 31 Dec 2027.',
                   received_at=datetime(2026, 10, 25)) is None


def test_hours_written_without_a_colon_are_read():
    r = qd.read('RFQ', 'Please submit before 10/11/2026 by 1700 hrs',
                received_at=datetime(2026, 11, 1))
    assert qd.shown(r.at) == '10 Nov 2026 17:00'


def test_a_reminder_mails_sentence_is_read_when_the_subject_has_no_date():
    r = qd.read('Re: RA- 57103 reminder',
                'Auction closes 29 Oct 2026 15:15:00',
                received_at=datetime(2026, 10, 27))
    assert qd.shown(r.at) == '29 Oct 2026 15:15'


def test_a_date_two_years_out_is_not_a_submission_deadline():
    assert qd.read('RFQ', 'Rate contract valid until 31 Dec 2029.',
                   received_at=datetime(2026, 11, 1)) is None


@pytest.mark.parametrize('hours,colour', [
    (0.5, 'red'), (23, 'red'), (30, 'orange'), (47, 'orange'),
    (49, 'green'), (240, 'green'),
])
def test_one_colour_rule_everywhere(hours, colour):
    """§2.6 — red under a day, orange under two, green beyond. One
    function, so the cockpit, the strip and the lists cannot drift."""
    now = datetime(2026, 11, 1, 9, 0)
    assert qd.colour(now + timedelta(hours=hours), now=now) == colour


def test_a_lead_with_no_deadline_is_grey_not_green():
    assert qd.colour(None) == 'grey'


# ── §2.2 mandatory, with guided help (acceptance 1) ─────────────────
def test_acceptance_1_a_lead_cannot_reach_rfq_without_a_due_date(world):
    lead = _lead(stage='New Opportunity', followup_date=date(2026, 12, 1))
    r = _client().put(f'/api/leads/{lead.id}',
                      json={'stage': 'RFQ Generated'})
    assert r.status_code == 400
    body = r.get_json()
    assert body['needs'] == 'quote_due_at'
    assert body['guide']['code'] == 'QUOTE_DUE_MISSING'
    assert body['guide']['steps'], 'guided help must say what to do'
    with flask_app.app_context():
        assert db.session.get(Lead, lead.id).stage == 'New Opportunity'


def test_the_refusal_offers_the_date_the_mail_states(world):
    """[Take me there] has to land on a filled box, or the person is
    asked to go and find something the CRM already read."""
    lead = _lead(stage='New Opportunity',
                 followup_date=date(2026, 12, 1),
                 subject='RA- 57103: Indo Tech 29 Nov 2026 15:15:00')
    r = _client().put(f'/api/leads/{lead.id}',
                      json={'stage': 'RFQ Generated'})
    assert r.get_json()['suggested'] == '29 Nov 2026 15:15'


def test_the_same_save_may_carry_the_date(world):
    lead = _lead(stage='New Opportunity', followup_date=date(2026, 12, 1))
    r = _client().put(f'/api/leads/{lead.id}',
                      json={'stage': 'RFQ Generated',
                            'quote_due_at': '2026-11-04T18:00'})
    assert r.status_code == 200
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        assert got.stage == 'RFQ Generated'
        assert got.quote_due_at == DUE
        assert got.quote_due_confirmed is True


def test_once_a_quote_is_recorded_the_deadline_is_no_longer_demanded(world):
    """§2.2 — mandatory *until a quote is recorded*. After that, when
    the client stopped accepting offers is history."""
    lead = _lead(stage='RFQ Generated', followup_date=date(2026, 12, 1),
                 quote_no='Q-1')
    assert not gate.quote_due_needed(lead, 'Quoted')


def test_closing_a_lead_never_asks_for_either_date(world):
    """A lead must always be disposable, or people work around the
    CRM instead of in it."""
    lead = _lead(stage='RFQ Generated')
    assert not gate.quote_due_needed(lead, 'Lost')
    assert not gate.followup_needed(lead, 'Lost')
    assert not gate.followup_needed(lead, 'Won')
    assert not gate.followup_needed(lead, 'On Hold')


def test_a_retired_stage_name_does_not_break_the_save(world):
    """`stage_map.position()` answers None for a stage it does not
    know, and this CRM still carries old values. Comparing None with
    an int made the gate 500 on an ordinary save."""
    lead = _lead(stage='New Opportunity', followup_date=date(2026, 12, 1))
    r = _client().put(f'/api/leads/{lead.id}',
                      json={'stage': 'Meeting',
                            'followup': '2026-12-02'})
    assert r.status_code == 200


# ── §2.3 confirm ────────────────────────────────────────────────────
def test_a_date_read_from_the_mail_arrives_unconfirmed(world):
    lead = _lead(stage='New Opportunity',
                 subject='RA- 57103: Indo Tech 29 Nov 2026 15:15:00')
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        flow.read_from_message(
            got, subject=got.original_email_subject,
            received_at=got.received_at)
        db.session.commit()
        assert got.quote_due_at is not None
        assert got.quote_due_confirmed is False


def test_confirming_takes_the_lead_off_the_tile(world):
    lead = _lead(due=DUE)
    with flask_app.app_context():
        db.session.get(Lead, lead.id).quote_due_confirmed = False
        db.session.commit()
    r = _client().post(f'/api/leads/{lead.id}/quote-due/confirm')
    assert r.status_code == 200 and r.get_json()['confirmed'] is True


def test_confirming_a_lead_with_no_date_is_refused(world):
    lead = _lead()
    r = _client().post(f'/api/leads/{lead.id}/quote-due/confirm')
    assert r.status_code == 400
    assert r.get_json()['needs'] == 'quote_due_at'


# ── §2.4 the extension (acceptance 3) ───────────────────────────────
def test_acceptance_3_an_extension_keeps_its_history_and_its_reason(world):
    lead = _lead(due=DUE, followup_date=date(2026, 11, 3))
    r = _client().post(f'/api/leads/{lead.id}/quote-due/extend',
                       json={'quote_due_at': '2026-11-10T18:00',
                             'reason': 'client_extension'})
    assert r.status_code == 200
    body = r.get_json()
    assert body['extensions'] == 1, 'the lead shows "Extended 1x"'
    assert body['shown'] == '10 Nov 2026 18:00'
    rows = [h for h in body['history'] if h['reason_code'] != 'set']
    assert rows and rows[0]['old_due_at'].startswith('2026-11-04')
    assert rows[0]['reason'] == 'Client extension'


def test_an_extension_needs_a_reason_from_the_list(world):
    lead = _lead(due=DUE)
    r = _client().post(f'/api/leads/{lead.id}/quote-due/extend',
                       json={'quote_due_at': '2026-11-10T18:00',
                             'reason': 'because'})
    assert r.status_code == 400
    with flask_app.app_context():
        assert db.session.get(Lead, lead.id).quote_due_at == DUE


def test_other_as_a_reason_has_to_say_what(world):
    lead = _lead(due=DUE)
    r = _client().post(f'/api/leads/{lead.id}/quote-due/extend',
                       json={'quote_due_at': '2026-11-10T18:00',
                             'reason': 'other'})
    assert r.status_code == 400


def test_an_extension_backwards_is_refused(world):
    """"Extended" has a direction. A portal mail restating an earlier
    deadline is a correction, which is an edit, not an extension."""
    lead = _lead(due=DUE)
    r = _client().post(f'/api/leads/{lead.id}/quote-due/extend',
                       json={'quote_due_at': '2026-11-02T18:00',
                             'reason': 'corrigendum'})
    assert r.status_code == 400
    assert 'not later' in r.get_json()['error']


def test_two_extensions_count_two(world):
    lead = _lead(due=DUE, followup_date=date(2026, 11, 3))
    c = _client()
    for day in ('2026-11-10T18:00', '2026-11-17T18:00'):
        assert c.post(f'/api/leads/{lead.id}/quote-due/extend',
                      json={'quote_due_at': day,
                            'reason': 'corrigendum'}).status_code == 200
    assert c.get(f'/api/leads/{lead.id}/quote-due'
                 ).get_json()['extensions'] == 2


def test_an_extension_moves_the_follow_up_with_it(world):
    """§3.2 — at RFQ Generated the follow-up is a day before the
    deadline, so a deadline that moves takes it along."""
    lead = _lead(due=DUE, followup_date=date(2026, 11, 3))
    _client().post(f'/api/leads/{lead.id}/quote-due/extend',
                   json={'quote_due_at': '2026-11-20T18:00',
                         'reason': 'client_extension'})
    with flask_app.app_context():
        assert db.session.get(Lead, lead.id).followup_date == \
            date(2026, 11, 19)


def test_a_date_chosen_by_a_person_is_not_moved(world):
    """A person's date is a decision; the rule's date is a default."""
    lead = _lead(due=DUE, followup_date=date(2026, 11, 2))
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        followup.after_due_change(got)
        assert got.followup_date == date(2026, 11, 2)


# ── §2.5 the proposal (acceptance 2) ────────────────────────────────
def test_acceptance_2_a_later_mail_proposes_rather_than_applies(world):
    lead = _lead(due=DUE)
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        assert flow.read_from_message(
            got, subject='Re: RA- 57103 corrigendum',
            body='The auction now closes 12 Nov 2026 15:15:00',
            received_at=datetime(2026, 11, 2)) == 'proposed'
        db.session.commit()
        # Nothing moved on the lead.
        assert got.quote_due_at == DUE
        assert flow.pending(got.id) is not None


def test_a_reminder_restating_the_same_date_proposes_nothing(world):
    lead = _lead(due=DUE)
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        flow.read_from_message(
            got, subject='Re: RA- 57103 reminder',
            body='Reminder: closes 04 Nov 2026 18:00:00',
            received_at=datetime(2026, 11, 2))
        db.session.commit()
        assert flow.pending(got.id) is None


def test_one_tap_accepts_the_proposal(world):
    lead = _lead(due=DUE, followup_date=date(2026, 11, 3))
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        flow.propose(got, datetime(2026, 11, 12, 9, 45),
                     source=qd.MAIL_BODY, evidence='closes 12 Nov')
        db.session.commit()
    r = _client().post(f'/api/leads/{lead.id}/quote-due/accept')
    assert r.status_code == 200
    body = r.get_json()
    assert body['shown'] == '12 Nov 2026 15:15'
    assert body['extensions'] == 1
    assert body['proposal'] is None


def test_a_proposal_can_be_refused_and_the_record_of_it_stays(world):
    lead = _lead(due=DUE)
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        flow.propose(got, datetime(2026, 11, 12, 9, 45),
                     source=qd.MAIL_BODY)
        db.session.commit()
    assert _client().post(f'/api/leads/{lead.id}/quote-due/dismiss',
                          json={'why': 'not our lane'}
                          ).status_code == 200
    with flask_app.app_context():
        assert flow.pending(lead.id) is None
        rows = LeadQuoteDueHistory.query.filter_by(
            lead_id=lead.id).all()
        assert any(r.reason_code == 'dismissed' for r in rows), \
            'the offer is spent, not deleted'


# ── §2.7 the miss (acceptance 4) ────────────────────────────────────
def test_acceptance_4_a_passed_deadline_with_no_quote_is_missed(world):
    lead = _lead(due=datetime(2026, 10, 29, 9, 45))
    now = datetime(2026, 11, 1)
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        assert qd.is_missed(got, now=now)
        assert flow.mark_missed(got, now=now) is True
        db.session.commit()
        assert got.quote_due_missed_at is not None
        # Once only, so the mail goes once.
        assert flow.mark_missed(got, now=now) is False


def test_a_recorded_quote_is_not_a_miss(world):
    lead = _lead(due=datetime(2026, 10, 29, 9, 45), quote_no='Q-9')
    assert not qd.is_missed(lead, now=datetime(2026, 11, 1))


def test_a_recorded_bid_outcome_is_not_a_miss(world):
    lead = _lead(due=datetime(2026, 10, 29, 9, 45))
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        got.bid_outcome = 'not_bid'
        db.session.commit()
        assert not qd.is_missed(got, now=datetime(2026, 11, 1))


def test_the_miss_mail_names_the_owner_and_the_head(world):
    lead = _lead(due=datetime(2026, 10, 29, 9, 45))
    with flask_app.app_context():
        told = flow.tell_about_miss(db.session.get(Lead, lead.id),
                                    dry_run=True)
    assert OWNER in told['would_tell']
    assert 'Submission missed' in told['title']


def test_bid_or_not_bid_is_recorded_with_a_reason(world):
    lead = _lead(due=datetime(2026, 10, 29, 9, 45))
    r = _client().post(f'/api/leads/{lead.id}/bid-outcome',
                       json={'outcome': 'not_bid_rate'})
    assert r.status_code == 200
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        assert got.bid_outcome == 'not_bid'
        assert 'rate not workable' in got.bid_reason


def test_bid_outcome_refuses_free_text(world):
    lead = _lead(due=DUE)
    r = _client().post(f'/api/leads/{lead.id}/bid-outcome',
                       json={'outcome': 'we did not fancy it'})
    assert r.status_code == 400
    assert r.get_json()['options']


# ── who may change it ───────────────────────────────────────────────
def test_somebody_elses_lead_is_read_only(world):
    lead = _lead(due=DUE)
    r = _client(OTHER).post(f'/api/leads/{lead.id}/quote-due/confirm')
    assert r.status_code in (403, 404)


def test_an_admin_may_change_any_lead(world):
    lead = _lead(due=DUE)
    with flask_app.app_context():
        db.session.get(Lead, lead.id).quote_due_confirmed = False
        db.session.commit()
    assert _client(ADMIN, 'admin').post(
        f'/api/leads/{lead.id}/quote-due/confirm').status_code == 200


def test_signing_out_is_not_a_500(world):
    lead = _lead(due=DUE)
    assert flask_app.test_client().get(
        f'/api/leads/{lead.id}/quote-due').status_code == 401


# ══ §3 the next follow-up ═══════════════════════════════════════════
@pytest.mark.parametrize('stage,days', [
    ('New Opportunity', 1), ('Call Done', 3),
    ('Business Discussion', 3), ('Quoted', 2),
    ('Under Negotiation', 2),
])
def test_the_proposal_follows_the_stage(world, stage, days):
    lead = _lead(stage=stage)
    today = date(2026, 11, 2)
    assert followup.propose(lead, today=today) == \
        today + timedelta(days=days)


def test_at_rfq_the_proposal_is_a_day_before_the_deadline(world):
    lead = _lead(stage='RFQ Generated', due=datetime(2026, 11, 20, 12, 30))
    assert followup.propose(lead, today=date(2026, 11, 2)) == \
        date(2026, 11, 19)


def test_the_proposal_is_never_today_or_in_the_past(world):
    lead = _lead(stage='RFQ Generated', due=datetime(2026, 10, 1, 12, 30))
    today = date(2026, 11, 2)
    assert followup.propose(lead, today=today) > today


def test_won_lost_and_on_hold_are_proposed_nothing(world):
    for stage in ('Won', 'Lost', 'On Hold'):
        assert followup.propose(_lead(stage=stage,
                                      company=TAG + stage)) is None


def test_the_quick_picks_include_after_the_deadline(world):
    lead = _lead(due=DUE)
    picks = dict((k, v) for k, _l, v in
                 followup.quick_picks(lead, today=date(2026, 11, 2)))
    assert picks['tomorrow'] == date(2026, 11, 3)
    assert picks['plus7'] == date(2026, 11, 9)
    assert picks['after_due'] == date(2026, 11, 5)


def test_acceptance_5_a_stage_change_asks_for_a_follow_up(world):
    lead = _lead(stage='New Opportunity')
    r = _client().put(f'/api/leads/{lead.id}',
                      json={'stage': 'Call Done'})
    assert r.status_code == 400
    body = r.get_json()
    assert body['needs'] == 'followup'
    assert body['guide']['code'] == 'FOLLOWUP_MISSING'
    assert body['suggested'], 'the refusal carries the proposed date'


def test_an_activity_save_asks_for_a_follow_up_too(world):
    lead = _lead(stage='Call Done')
    r = _client().post(f'/api/leads/{lead.id}/activities',
                       json={'kind': 'call', 'subject': 'rang them'})
    assert r.status_code == 400
    assert r.get_json()['needs'] == 'followup'


def test_an_activity_save_that_carries_the_date_is_accepted(world):
    lead = _lead(stage='Call Done')
    r = _client().post(f'/api/leads/{lead.id}/activities',
                       json={'kind': 'call', 'subject': 'rang them',
                             'followup': '2026-11-09'})
    assert r.status_code == 201
    with flask_app.app_context():
        assert db.session.get(Lead, lead.id).followup_date == \
            date(2026, 11, 9)


def test_a_lead_that_already_has_a_follow_up_is_not_asked_again(world):
    lead = _lead(stage='Call Done', followup_date=date(2026, 12, 1))
    assert _client().post(
        f'/api/leads/{lead.id}/activities',
        json={'kind': 'note', 'body': 'x'}).status_code == 201


# ══ §4 the cockpit ══════════════════════════════════════════════════
def test_the_listener_stamps_every_kind_of_activity(world):
    lead = _lead(stage='Call Done', followup_date=date(2026, 12, 1))
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        # Created is the floor (§4.1).
        assert got.last_activity_kind == 'created'
        db.session.add(LeadNote(lead_id=got.id, note_text='called',
                                note_type='general'))
        db.session.commit()
        assert got.last_activity_kind == 'note'
        got.stage = 'Quoted'
        db.session.commit()
        assert got.last_activity_kind == 'stage'


def test_filing_an_old_mail_does_not_make_a_lead_look_worked(world):
    lead = _lead(created=datetime(2026, 10, 1))
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        was = got.last_activity_at
        db.session.add(LeadActivity(lead_id=got.id, kind='email',
                                    occurred_at=datetime(2026, 9, 1)))
        db.session.commit()
        assert got.last_activity_at == was


def test_acceptance_6_no_activity_counts_are_not_zero(world):
    """They read zero for every lead in the CRM while the ageing chart
    showed 584 at 16–30 days. Both came off the same records."""
    from app.workbench import filters as wf

    quiet = _lead(company=TAG + 'Quiet Co', followup_date=date(2026, 12, 1))
    with flask_app.app_context():
        db.session.get(Lead, quiet.id).last_activity_at = \
            datetime.utcnow() - timedelta(days=20)
        db.session.commit()
        from app.access import scope as sc_mod
        from app.workbench import service as wb
        items, _ = wb.lead_items(sc_mod.for_employee(ADMIN),
                                 include_quiet=True)
        ctx = {'today': date.today()}
        mine = [i for i in items if i['id'] == quiet.id]
        assert mine, 'the lead has to be on the board at all'
        assert wf.BY_KEY['no_activity_7d'].matches(mine[0], ctx)
        assert wf.BY_KEY['no_activity_15d'].matches(mine[0], ctx)
        assert not wf.BY_KEY['no_activity_30d'].matches(mine[0], ctx)


def test_quiet_days_falls_back_to_the_created_date(world):
    lead = _lead(created=datetime(2026, 10, 1))
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        got.last_activity_at = None
        assert la.quiet_days(got, now=datetime(2026, 10, 21)) == 20


def test_acceptance_7_the_strip_and_the_kpi_row_agree_on_one_scope(world):
    """154 against 128, with nothing on screen to say why. Both
    endpoints now take the scope from the request and report it."""
    typed = _lead(company=TAG + 'Typed In', source='manual',
                  followup_date=date(2026, 10, 1), mid=None)
    with flask_app.app_context():
        db.session.get(Lead, typed.id).email_message_id = None
        db.session.commit()
    c = _client(ADMIN, 'admin')
    team = c.get('/api/my-work?scope=all').get_json()
    mailbox = c.get('/api/my-work?scope=mailbox').get_json()
    assert team['data_scope']['label'] == 'Team'
    assert mailbox['data_scope']['label'] == 'Mailbox'
    # The manually typed lead is in one population and not the other,
    # which is the whole point of saying which.
    assert team['pending']['open_leads'] > mailbox['pending']['open_leads']


def test_a_scope_narrows_and_never_widens(world):
    lead = _lead(company=TAG + 'Someone Elses', owner=OTHER,
                 followup_date=date(2026, 12, 1))
    c = _client(OWNER)
    got = c.get('/api/my-work?scope=all').get_json()
    assert lead.id not in [a.get('id') for a in got.get('actions', [])]


def test_the_scoreboard_counts_open_leads_and_names_the_total(world):
    """The "—" row read 883 leads, of which 822 were already Lost."""
    _lead(company=TAG + 'Open One', followup_date=date(2026, 12, 1))
    _lead(company=TAG + 'Closed One', stage='Lost')
    c = _client(ADMIN, 'admin')
    with flask_app.app_context():
        # The cockpit route is behind FEATURE_CRM2, which predates
        # this release. Switched on here rather than skipping: a test
        # that skips when a flag is off is a test that passes while
        # the screen is broken.
        from app.services import flags
        flags.set('FEATURE_CRM2', True, actor='test')
        flags.cache_clear()
    body = c.get('/api/crm2/cockpit?scope=mailbox').get_json()
    assert body.get('ok'), body
    board = body['cockpit']
    rows = board['scoreboard']['rows']
    assert rows and all(r['open_leads'] >= 1 for r in rows)
    labels = [col['label'] for col in board['scoreboard']['columns']]
    assert 'incl. closed' in labels
    # §4.3 — and the page says which leads these are.
    assert board['data_scope']['label'] == 'Mailbox'
    # §2.9 / §4.2 — the tiles arrive with the page.
    assert {t['key'] for t in board['tiles']} >= {
        'quote_due_today', 'missed_submission', 'followup_missing',
        'no_activity_7d'}


def test_every_cockpit_tile_opens_its_own_filter(world):
    """§4.6 — tile = chip = list. The count a tile shows is the count
    of the filter its link opens."""
    from app.crm2.routes import C02_TILES
    from app.workbench import filters as wf

    for key, _label in C02_TILES:
        assert key in wf.BY_KEY, key
        assert wf.BY_KEY[key].tile, f'{key} needs a tile label'


# ══ §8 the reversible log ═══════════════════════════════════════════
def test_a_bulk_change_can_be_put_back(world):
    lead = _lead(followup_date=None)
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        with bulk_log.batch('test.followup', note='t',
                            actor='T') as log:
            log.set(got, 'followup_date', date(2026, 12, 25))
            name = log.name
        db.session.commit()
        assert got.followup_date == date(2026, 12, 25)
        restored, skipped, problems = bulk_log.undo(name, actor='T')
        db.session.commit()
        assert (restored, skipped, problems) == (1, 0, [])
        assert got.followup_date is None


def test_an_undo_refuses_to_overwrite_a_later_edit(world):
    lead = _lead(followup_date=None)
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        with bulk_log.batch('test.later', actor='T') as log:
            log.set(got, 'followup_date', date(2026, 12, 25))
            name = log.name
        db.session.commit()
        # The owner picks their own date afterwards.
        got.followup_date = date(2027, 1, 5)
        db.session.commit()
        restored, skipped, problems = bulk_log.undo(name)
        db.session.commit()
        assert restored == 0 and skipped == 1
        assert 'changed since' in problems[0]
        assert got.followup_date == date(2027, 1, 5)


def test_undoing_twice_does_nothing_the_second_time(world):
    lead = _lead(followup_date=None)
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        with bulk_log.batch('test.twice', actor='T') as log:
            log.set(got, 'followup_date', date(2026, 12, 25))
            name = log.name
        db.session.commit()
        bulk_log.undo(name)
        db.session.commit()
        assert bulk_log.undo(name) == (0, 0, [])


def test_the_log_tells_an_empty_field_from_the_word_none(world):
    lead = _lead(followup_date=None)
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        with bulk_log.batch('test.null', actor='T') as log:
            log.set(got, 'next_action', 'call them')
            name = log.name
        db.session.commit()
        row = BulkChangeLog.query.filter_by(batch=name).first()
        assert row.was_null is True and row.old_value is None
        bulk_log.undo(name)
        db.session.commit()
        assert got.next_action is None


def test_a_no_op_write_is_not_logged(world):
    lead = _lead(followup_date=date(2026, 12, 1))
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        with bulk_log.batch('test.noop', actor='T') as log:
            assert log.set(got, 'followup_date',
                           date(2026, 12, 1)) is False
            assert len(log) == 0


def test_a_date_comes_back_as_a_date_not_a_string(world):
    """SQLite stores '2026-12-25' in a DATE column happily and hands
    back a string that every date comparison then skips."""
    lead = _lead(followup_date=date(2026, 11, 20))
    with flask_app.app_context():
        got = db.session.get(Lead, lead.id)
        with bulk_log.batch('test.types', actor='T') as log:
            log.set(got, 'followup_date', date(2026, 12, 25))
            name = log.name
        db.session.commit()
        bulk_log.undo(name)
        db.session.commit()
        db.session.expire_all()
        assert db.session.get(Lead, lead.id).followup_date == \
            date(2026, 11, 20)
        assert isinstance(db.session.get(Lead, lead.id).followup_date,
                          date)


# ── the box a person types into is in the time they read ────────────
def test_the_form_value_is_ist_not_the_stored_utc(world):
    """A screenshot caught this one, and it was the worst bug in the
    release: the card's heading read "Quote due 02 Nov 23:04" with
    17:34 in the box under it, because the heading was IST and the
    input was handed the stored UTC string. Pressing Save without
    touching it moved the deadline five and a half hours earlier — a
    deadline that walks backwards every time somebody opens the lead.
    """
    lead = _lead(due=DUE)
    body = _client().get(f'/api/leads/{lead.id}/quote-due').get_json()
    assert body['shown'] == '04 Nov 2026 18:00'
    assert body['quote_due_local'] == '2026-11-04T18:00'
    assert body['quote_due_at'] == '2026-11-04T12:30'


def test_saving_the_form_value_back_unchanged_moves_nothing(world):
    """The round trip the bug broke: read the card, press Save
    without editing, and the deadline must be where it was."""
    lead = _lead(due=DUE)
    c = _client()
    body = c.get(f'/api/leads/{lead.id}/quote-due').get_json()
    again = c.post(f'/api/leads/{lead.id}/quote-due',
                   json={'quote_due_at': body['quote_due_local']})
    assert again.status_code == 200
    with flask_app.app_context():
        assert db.session.get(Lead, lead.id).quote_due_at == DUE


def test_a_suggested_date_is_offered_in_ist_too(world):
    lead = _lead(stage='New Opportunity',
                 subject='RA- 57103: Indo Tech 29 Nov 2026 15:15:00')
    body = _client().get(f'/api/leads/{lead.id}/quote-due').get_json()
    assert body['suggested'] == '29 Nov 2026 15:15'
    assert body['suggested_iso'] == '2026-11-29T15:15'


def test_a_proposed_date_is_offered_in_ist_too(world):
    lead = _lead(due=DUE)
    with flask_app.app_context():
        flow.propose(db.session.get(Lead, lead.id),
                     datetime(2026, 11, 12, 9, 45),
                     source=qd.MAIL_BODY)
        db.session.commit()
    body = _client().get(f'/api/leads/{lead.id}/quote-due').get_json()
    assert body['proposal']['shown'] == '12 Nov 2026 15:15'
    assert body['proposal']['iso'] == '2026-11-12T15:15'


# ── §4.4 the scoreboard ─────────────────────────────────────────────
def test_the_no_owner_row_is_named_for_what_it_is(world):
    """It read 883 leads, 822 of them already Lost, under a heading
    of "—" that looked like a person's name."""
    _lead(company=TAG + 'Ownerless', owner=None,
          followup_date=date(2026, 12, 1))
    body = _client(ADMIN, 'admin').get(
        '/api/dashboard/summary').get_json() or {}
    rows = [r for r in (body.get('pic_board') or [])
            if r['emp_code'] == '—']
    assert rows, 'the ownerless row has to exist at all'
    assert rows[0]['name'] == 'No owner'


def test_the_scoreboard_separates_open_from_closed(world):
    _lead(company=TAG + 'Board Open', owner=OWNER,
          followup_date=date(2026, 12, 1))
    _lead(company=TAG + 'Board Lost', owner=OWNER, stage='Lost')
    body = _client(ADMIN, 'admin').get(
        '/api/dashboard/summary').get_json() or {}
    rows = [r for r in (body.get('pic_board') or [])
            if r['emp_code'] == OWNER]
    assert rows, 'the owner has to be on the board'
    row = rows[0]
    assert row['total'] > row['active'], \
        'total includes the closed lead, active does not'
    assert row['active'] >= 1


def test_the_board_leads_with_the_people_carrying_live_work(world):
    """Sorting on wins put somebody with two won deals and no
    pipeline above somebody carrying forty live ones."""
    body = _client(ADMIN, 'admin').get(
        '/api/dashboard/summary').get_json() or {}
    rows = body.get('pic_board') or []
    actives = [r['active'] for r in rows]
    assert actives == sorted(actives, reverse=True)


# ── the date it is where the users are ───────────────────────────────
def test_today_is_the_ist_date_not_the_servers(world):
    """Caught by the suite running after 18:30 UTC.

    The server keeps naive UTC, so between 18:30 and midnight UTC it
    is already tomorrow in IST. A call logged at 01:00 IST on Tuesday
    was being stamped Monday, and a follow-up proposed "today + 2"
    landed a day early for five and a half hours of every day.
    """
    from datetime import datetime, timedelta

    from app.services import quote_due as qd

    assert qd.today_ist() == (datetime.utcnow() + timedelta(
        hours=5, minutes=30)).date()


def test_the_ist_date_turns_over_at_1830_utc(world):
    from datetime import datetime

    from app.services import quote_due as qd

    # 18:29 UTC on the 1st is still the 1st in IST (23:59);
    # 18:30 is the 2nd (00:00).
    assert qd.to_ist(datetime(2026, 11, 1, 18, 29)).date().day == 1
    assert qd.to_ist(datetime(2026, 11, 1, 18, 30)).date().day == 2


def test_a_follow_up_is_never_proposed_for_today_in_ist(world):
    """The rule §3.2 exists to hold: a follow-up due the moment it is
    set is already overdue."""
    from app.services import followup, quote_due as qd

    lead = _lead(stage='Contacted')
    proposed = followup.propose(lead)
    assert proposed is None or proposed > qd.today_ist()
