"""The three intake screens — §14, §15, §21, §22.

The engine decides; these let a person disagree, and record the
disagreement. A correction is only useful next to what it corrected, so
every action here writes the pair.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'ScreenTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'screens.db')

import importlib                                              # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Employee, Company, Lead = _main.Employee, _main.Company, _main.Lead
EmailClassification = _main.EmailClassification
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.intake import service as svc                         # noqa: E402
from app.services import lead_intake as li                    # noqa: E402


@pytest.fixture(scope='module')
def world():
    with flask_app.app_context():
        db.create_all()
        for code, name, role in (('SCRADM', 'Screen Admin', 'admin'),
                                 ('VH9', 'Vertical Head', 'user'),
                                 ('OPS9', 'Ops PIC', 'user'),
                                 ('BAK9', 'Backup Person', 'user')):
            e = Employee.query.filter_by(emp_code=code).first()
            if not e:
                e = Employee(emp_code=code, name=name)
                db.session.add(e)
            e.role, e.is_active, e.must_change_pw = role, True, False
            e.is_super_admin = (code == 'SCRADM')
            e.vertical = 'All'

        acct = Company(name='Screen Steel Ltd', is_active=True,
                       email_domains=['screensteel.com'])
        bare = Company(name='Ownerless Ltd', is_active=True)
        db.session.add_all([acct, bare])
        db.session.commit()
        ids = {'acct': acct.id, 'bare': bare.id}
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='SCRADM', name='Screen Admin', role='admin',
                 vertical='All')
    ids['client'] = c
    return ids


def _pending(subject='Fw: RFQ - screen test', frm='buyer@screensteel.com',
             klass=li.Klass.REVIEW, body='Please quote for 40 MT.'):
    with flask_app.app_context():
        row = EmailClassification(
            message_id=f'<{subject}@x>', subject=subject, from_addr=frm,
            from_domain=frm.split('@')[1], classification=klass,
            decided_by='step_10', reason='low confidence (42%)',
            confidence=42, duplicate_score=0, review_state='pending',
            payload={'body': body, 'resolved_sender': frm,
                     'attachments': ['RFQ.xlsx']})
        db.session.add(row)
        db.session.commit()
        return row.id


# ─── Account Master — §12, §15 ───────────────────────────────────────────
def test_the_summary_counts_what_can_actually_auto_assign(world):
    with flask_app.app_context():
        s = svc.account_summary()
    assert s['total'] >= 2
    assert s['no_owner'] >= 1
    # A domain without an owner cannot assign, and neither can an owner
    # without a domain. Only the pair counts.
    assert s['auto_assignable'] == 0


def test_saving_both_owners_makes_an_account_auto_assignable(world):
    with flask_app.app_context():
        ok, err = svc.save_account_owners(
            world['acct'], primary='VH9', secondary='OPS9', backup='BAK9',
            vertical='Project Logistics', domains='screensteel.com')
        assert ok, err
        db.session.commit()
        s = svc.account_summary()
        assert s['auto_assignable'] == 1
        assert s['with_both'] == 1


def test_the_same_person_cannot_hold_both_roles(world):
    with flask_app.app_context():
        ok, err = svc.save_account_owners(world['acct'], primary='VH9',
                                          secondary='VH9')
        assert not ok
        assert 'cannot be both' in err


def test_a_backup_who_is_already_an_owner_is_refused(world):
    with flask_app.app_context():
        ok, err = svc.save_account_owners(
            world['acct'], primary='VH9', secondary='OPS9', backup='VH9')
        assert not ok
        assert 'backup' in err.lower()


def test_domains_are_cleaned_not_taken_literally(world):
    with flask_app.app_context():
        ok, _ = svc.save_account_owners(
            world['bare'], primary='OPS9',
            domains='https://www.Ownerless.com/about, @ownerless.co.in, junk')
        assert ok
        db.session.commit()
        c = db.session.get(Company, world['bare'])
        assert c.email_domains == ['ownerless.com', 'ownerless.co.in']


def test_a_domain_already_claimed_elsewhere_is_flagged(world):
    """Two accounts on one domain means a lead goes to whichever the
    lookup reached first."""
    with flask_app.app_context():
        clashes = svc.domain_conflicts(world['bare'], 'screensteel.com')
    assert clashes and clashes[0]['name'] == 'Screen Steel Ltd'


def test_the_worst_configured_accounts_come_first(world):
    with flask_app.app_context():
        rows = svc.account_rows(missing_only=False)
    ownership = [r['ownership'] for r in rows]
    assert ownership == sorted(ownership), \
        'accounts with no owner must be at the top, where they get fixed'


def test_the_api_refuses_a_clashing_domain_until_confirmed(world):
    c = world['client']
    r = c.put(f'/api/intake/accounts/{world["bare"]}',
              json={'primary': 'OPS9', 'domains': 'screensteel.com'})
    assert r.status_code == 409
    assert r.get_json()['domain_conflict'] is True

    r = c.put(f'/api/intake/accounts/{world["bare"]}',
              json={'primary': 'OPS9', 'domains': 'screensteel.com',
                    'confirm_domains': True})
    assert r.status_code == 200


# ─── Review queue — §21 ──────────────────────────────────────────────────
def test_the_queue_shows_what_is_waiting(world):
    cid = _pending()
    with flask_app.app_context():
        items = svc.review_queue()
    got = [i for i in items if i['id'] == cid]
    assert got, 'a pending classification should be in the queue'
    assert got[0]['payload']['body']
    assert got[0]['account'] == 'Screen Steel Ltd'


def test_accepting_creates_a_lead_and_assigns_both_owners(world):
    cid = _pending(subject='Fw: RFQ - accept me')
    r = world['client'].post(f'/api/intake/review/{cid}/accept', json={})
    assert r.status_code == 200, r.get_data(as_text=True)
    lead_id = r.get_json()['lead_id']

    with flask_app.app_context():
        lead = db.session.get(Lead, lead_id)
        assert lead is not None
        assert lead.company == 'Screen Steel Ltd'
        assert lead.assigned_to == 'VH9'
        assert lead.secondary_owner == 'OPS9'
        assert lead.original_email_body           # built from the payload
        row = db.session.get(EmailClassification, cid)
        assert row.review_state == 'accepted'
        assert row.corrected_to == li.Klass.NEW_LEAD


def test_rejecting_needs_a_reason_and_deletes_nothing(world):
    cid = _pending(subject='Fw: not a lead')
    c = world['client']

    r = c.post(f'/api/intake/review/{cid}/reject', json={})
    assert r.status_code == 400, 'a reason is what makes it worth recording'

    r = c.post(f'/api/intake/review/{cid}/reject',
               json={'reason': 'Spam / Marketing'})
    assert r.status_code == 200
    with flask_app.app_context():
        row = db.session.get(EmailClassification, cid)
        assert row is not None, 'the row must survive a rejection'
        assert row.review_state == 'rejected'
        assert row.correction_reason == 'Spam / Marketing'
        assert row.corrected_by == 'SCRADM'


def test_reclassifying_records_the_right_answer(world):
    cid = _pending(subject='Fw: rates from a line')
    r = world['client'].post(f'/api/intake/review/{cid}/reclassify',
                             json={'to_class': li.Klass.RATE_SOURCING})
    assert r.status_code == 200
    with flask_app.app_context():
        row = db.session.get(EmailClassification, cid)
        assert row.corrected_to == li.Klass.RATE_SOURCING
        assert row.classification == li.Klass.REVIEW, \
            'the original decision must survive beside the correction'
        assert row.was_wrong


def test_an_unknown_class_is_refused(world):
    cid = _pending(subject='Fw: bad class')
    r = world['client'].post(f'/api/intake/review/{cid}/reclassify',
                             json={'to_class': 'Z_made_up'})
    assert r.status_code == 400


def test_accepting_twice_does_not_create_two_leads(world):
    cid = _pending(subject='Fw: RFQ - twice')
    c = world['client']
    first = c.post(f'/api/intake/review/{cid}/accept', json={}).get_json()
    second = c.post(f'/api/intake/review/{cid}/accept', json={}).get_json()
    assert first['lead_id'] == second['lead_id']


# ─── Intelligence — §22 ──────────────────────────────────────────────────
def test_the_headline_numbers_add_up(world):
    with flask_app.app_context():
        d = svc.intelligence(days=365)
    assert d['total'] == d['leads_created'] + d['noise']
    assert 0 <= d['noise_reduction'] <= 100


def test_accuracy_is_measured_over_what_was_reviewed_not_everything(world):
    """Counting unreviewed decisions as correct would report near-100%
    for a classifier nobody has checked.

    Twenty unreviewed decisions are added so the two denominators —
    reviewed and total — cannot coincide.
    """
    with flask_app.app_context():
        for n in range(20):
            db.session.add(EmailClassification(
                message_id=f'<unreviewed{n}@x>',
                classification=li.Klass.NEW_LEAD,
                decided_by='step_10', review_state='pending'))
        db.session.commit()
        d = svc.intelligence(days=365)

    assert d['reviewed'] < d['total'], 'the fixture should leave a gap'
    assert d['accuracy'] == round(
        100.0 * (d['reviewed'] - d['wrong']) / d['reviewed'], 1), \
        'accuracy must be measured over reviewed decisions, not all of them'


def test_accuracy_is_none_when_nothing_has_been_reviewed():
    """A fresh install must not report a score it cannot have earned."""
    import inspect
    src = inspect.getsource(svc.intelligence)
    assert "if corrected else None" in src


def test_the_breakdowns_are_sorted_and_percentaged(world):
    with flask_app.app_context():
        d = svc.intelligence(days=365)
    counts = [c['count'] for c in d['by_class']]
    assert counts == sorted(counts, reverse=True)
    for c in d['by_class']:
        assert c['label'] and 0 <= c['pct'] <= 100


# ─── access control ──────────────────────────────────────────────────────
@pytest.mark.parametrize('path', ['/accounts/owners', '/lead-review',
                                  '/intake-intelligence'])
def test_an_admin_can_open_each_screen(world, path):
    assert world['client'].get(path).status_code == 200


@pytest.mark.parametrize('path', ['/accounts/owners', '/lead-review',
                                  '/intake-intelligence',
                                  '/api/intake/accounts',
                                  '/api/intake/review',
                                  '/api/intake/intelligence'])
def test_a_non_admin_cannot(world, path):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code='VH9', name='Vertical Head', role='user',
                 vertical='All')
    assert c.get(path).status_code in (302, 403)


def test_the_screens_call_their_api_under_the_prefix():
    """The class of bug that made the funnels load and show nothing."""
    for name in ('accounts', 'review', 'intelligence'):
        src = open(os.path.join(_ROOT, 'templates', 'intake',
                                f'{name}.html')).read()
        assert "u.indexOf('{{ url_prefix }}') !== 0" in src, name


# ─── §21 — merge into an existing lead ───────────────────────────────────
@pytest.fixture
def scratch_lead(world):
    """A lead whose trail rows are cleaned up, and whose id is not.

    Two ways to break other modules, and this avoids both. Leaving trail
    rows behind is unsafe because another module deletes leads directly
    in the session — bypassing the cascade the delete API applies — and
    SQLite hands the freed id to the next insert, so an orphan reappears
    on a stranger's lead. But deleting the lead here is equally unsafe:
    it frees this id, and someone else's orphan lands on whatever takes
    it. So the trail goes and the empty lead row stays, holding its id
    out of circulation.
    """
    from app import LeadEmail
    made = []

    def make(subject='Existing RFQ for 40 MT'):
        with flask_app.app_context():
            lead = Lead(source='email', stage='New Opportunity',
                        company='Screen Steel Ltd', project=subject)
            db.session.add(lead)
            db.session.commit()
            made.append(lead.id)
            return lead.id

    yield make

    with flask_app.app_context():
        for lead_id in made:
            LeadEmail.query.filter_by(lead_id=lead_id).delete()
        db.session.commit()


def test_merging_puts_the_email_on_the_lead_it_belongs_to(world, scratch_lead):
    """Reclassifying records the right answer but leaves the email
    nowhere. The trail is what a salesperson actually reads."""
    from app import LeadEmail

    cid = _pending(subject='RE: Existing RFQ for 40 MT')
    lead_id = scratch_lead()

    with flask_app.app_context():
        row, err = svc.merge(cid, lead_id=lead_id, actor='SCRADM')
        assert err is None, err
        db.session.commit()

        trail = LeadEmail.query.filter_by(lead_id=lead_id).all()
        assert len(trail) == 1
        assert trail[0].subject == 'RE: Existing RFQ for 40 MT'
        assert trail[0].body == 'Please quote for 40 MT.'
        assert trail[0].source == 'merged_at_review'


def test_a_merge_is_recorded_as_a_correction(world, scratch_lead):
    """It is a label too — the classifier said review, a person said
    this belongs to a lead. That pair is the training example."""
    from app import EmailClassification

    cid = _pending(subject='RE: another existing one')
    lead_id = scratch_lead('another existing one')
    with flask_app.app_context():
        svc.merge(cid, lead_id=lead_id, actor='SCRADM')
        db.session.commit()
        row = db.session.get(EmailClassification, cid)
        assert row.review_state == 'merged'
        assert row.matched_lead_id == lead_id
        assert row.corrected_to == li.Klass.EXISTING
        assert row.corrected_at is not None


def test_merging_twice_does_not_duplicate_the_trail_row(world, scratch_lead):
    from app import LeadEmail

    cid = _pending(subject='RE: idempotent merge')
    lead_id = scratch_lead('idempotent merge')
    with flask_app.app_context():
        svc.merge(cid, lead_id=lead_id, actor='SCRADM')
        db.session.commit()
        svc.merge(cid, lead_id=lead_id, actor='SCRADM')
        db.session.commit()
        assert LeadEmail.query.filter_by(lead_id=lead_id).count() == 1


def test_merging_into_a_lead_that_does_not_exist_is_refused(world):
    cid = _pending(subject='RE: nowhere to go')
    with flask_app.app_context():
        row, err = svc.merge(cid, lead_id=999999, actor='SCRADM')
        assert row is None
        assert '999999' in err


def test_merging_without_a_lead_number_is_refused(world):
    cid = _pending(subject='RE: no number given')
    with flask_app.app_context():
        row, err = svc.merge(cid, lead_id=None, actor='SCRADM')
        assert row is None
        assert 'lead number' in err


# ─── §21 — reassign from the review screen ───────────────────────────────
def test_reassigning_moves_the_lead_this_row_created(world, scratch_lead):
    from app import Lead, EmailClassification

    cid = _pending(subject='RFQ to reassign')
    lead_id = scratch_lead('RFQ to reassign')
    with flask_app.app_context():
        db.session.get(EmailClassification, cid).created_lead_id = lead_id
        db.session.commit()

        row, err = svc.reassign(cid, primary_code='VH9',
                                secondary_code='OPS9',
                                reason='Specialist required', actor='SCRADM')
        assert err is None, err
        db.session.commit()
        lead = db.session.get(Lead, lead_id)
        assert lead.assigned_to == 'VH9'
        assert lead.secondary_owner == 'OPS9'


def test_the_reassignment_reason_reaches_the_history(world, scratch_lead):
    from app import Lead, EmailClassification, LeadAssignmentHistory

    cid = _pending(subject='RFQ reassign with reason')
    lead_id = scratch_lead('RFQ reassign with reason')
    with flask_app.app_context():
        db.session.get(EmailClassification, cid).created_lead_id = lead_id
        db.session.commit()
        svc.reassign(cid, primary_code='VH9', reason='Different geography',
                     actor='SCRADM')
        db.session.commit()
        h = (LeadAssignmentHistory.query.filter_by(lead_id=lead_id)
             .order_by(LeadAssignmentHistory.id.desc()).first())
        assert h is not None
        assert h.note == 'Different geography'


def test_reassigning_an_email_that_is_not_a_lead_yet_is_refused(world):
    """There is nothing to reassign. Saying so is better than silently
    doing nothing, which is what an unguarded version would do."""
    cid = _pending(subject='not a lead yet')
    with flask_app.app_context():
        row, err = svc.reassign(cid, primary_code='VH9',
                                reason='Specialist required')
        assert row is None
        assert 'accept it or merge it' in err


def test_a_reassignment_needs_a_primary(world, scratch_lead):
    from app import EmailClassification

    cid = _pending(subject='no primary given')
    lead_id = scratch_lead('no primary given')
    with flask_app.app_context():
        db.session.get(EmailClassification, cid).created_lead_id = lead_id
        db.session.commit()
        row, err = svc.reassign(cid, primary_code='', reason='Other')
        assert row is None
        assert 'primary' in err.lower()


# ─── §12-§15, §26 — the two PICs, without an admin ───────────────────────
def test_the_intake_path_assigns_owners_itself(world, scratch_lead):
    """The whole point of the engine, and it was missing.

    Only the review-accept path assigned anyone. A lead the classifier
    was confident enough to create straight through — the case §26 says
    must need no admin at all — arrived with no owner.
    """
    from app import Lead
    from email_ingest import single_message

    with flask_app.app_context():
        svc.save_account_owners(world['acct'], primary='VH9',
                                secondary='OPS9', backup='',
                                vertical='Project Logistics',
                                domains='screensteel.com')
        db.session.commit()

        lead_id = scratch_lead('auto assign on intake')
        lead = db.session.get(Lead, lead_id)
        primary, secondary = single_message.auto_assign_owners(
            lead, 'buyer@screensteel.com',
            subject='RFQ 40 MT', body='Please quote.')
        db.session.commit()

        assert (primary, secondary) == ('VH9', 'OPS9')
        assert db.session.get(Lead, lead_id).assigned_to == 'VH9'
        assert db.session.get(Lead, lead_id).secondary_owner == 'OPS9'


def test_an_unmapped_account_leaves_the_lead_unassigned_not_lost(
        world, scratch_lead):
    """§14 — no configured owner is an admin queue, not a dropped lead."""
    from app import Lead
    from email_ingest import single_message

    with flask_app.app_context():
        lead_id = scratch_lead('nobody owns this domain')
        lead = db.session.get(Lead, lead_id)
        primary, secondary = single_message.auto_assign_owners(
            lead, 'buyer@nobody-knows-this-domain.example')
        db.session.commit()

        assert (primary, secondary) == (None, None)
        assert db.session.get(Lead, lead_id) is not None


def test_assignment_failing_never_costs_the_lead(world, scratch_lead):
    """A broken lookup must not take the enquiry down with it."""
    from app import Lead
    from email_ingest import single_message
    from app.services import lead_intake_db as lidb

    real = lidb.resolve_account
    lidb.resolve_account = lambda *a, **k: 1 / 0
    try:
        with flask_app.app_context():
            lead_id = scratch_lead('assignment explodes')
            lead = db.session.get(Lead, lead_id)
            assert single_message.auto_assign_owners(
                lead, 'buyer@screensteel.com') == (None, None)
            assert db.session.get(Lead, lead_id) is not None
    finally:
        lidb.resolve_account = real


def test_auto_assigned_is_reported_on_the_intelligence_dashboard(world):
    """§22 lists it as a headline. It was only on the triage screen."""
    with flask_app.app_context():
        data = svc.intelligence(days=30)
    assert 'auto_assigned' in data
    assert isinstance(data['auto_assigned'], int)


def test_auto_assigned_counts_the_engine_not_people(world):
    """An admin reassigning by hand is not automatic assignment.

    Counting both would make the number report success it did not earn.
    """
    from app import Lead, LeadAssignmentHistory
    from app.services import lead_assignment

    with flask_app.app_context():
        before = svc.intelligence(days=30)['auto_assigned']

        lead = Lead(source='email', stage='New Opportunity',
                    company='Screen Steel Ltd')
        db.session.add(lead)
        db.session.flush()
        # by a person
        lead_assignment.assign(lead, primary_code='VH9', actor='SCRADM',
                               note='by hand')
        db.session.commit()
        assert svc.intelligence(days=30)['auto_assigned'] == before

        # by the engine
        lead_assignment.assign(lead, primary_code='OPS9', actor=None,
                               note='assigned automatically on intake')
        db.session.commit()
        assert svc.intelligence(days=30)['auto_assigned'] == before + 1
