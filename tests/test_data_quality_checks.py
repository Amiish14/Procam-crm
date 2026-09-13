"""Every Data Quality check finds its faulty record, skips the clean one,
and stays inside the viewer's scope.

Seeded with one faulty and one clean record per check. The test database
is shared with the rest of the run and SQLite reuses ids, so assertions
look only at the records created here, and everything is removed after.

Two administrators with the Data Quality permission work the same data:
one sees the whole company, the other has Own scope. Own must see the
problems in their own records and nothing about anybody else's — a count
is a leak as much as a row is.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'DqChecksTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'dqchecks.db'))

from app import (app as flask_app, db, Company, Contact,          # noqa: E402
                 EmailClassification, Employee, Lead, LeadActivity,
                 LeadEmail, LeadNote, Opportunity)
import app.models                                                  # noqa: E402,F401
import app.models.data_quality                                     # noqa: E402,F401
from app.access import scope as scope_mod                          # noqa: E402
from app.access.service import set_profile                         # noqa: E402
from app.data_quality import definitions as defs                   # noqa: E402
from app.data_quality import service as dq                         # noqa: E402
from app.models.access import AccessProfile, DataScope             # noqa: E402
from app.models.master_data import MasterItem                      # noqa: E402
from app.models.quote import Quote                                 # noqa: E402
from app.models.rfq import RFQ                                     # noqa: E402
from app.models.tms_handover import WonHandover                    # noqa: E402

flask_app.config['WTF_CSRF_ENABLED'] = False

ME, OTHER, GONE, BOSS = 'DQCME', 'DQCOTHER', 'DQCGONE', 'DQCBOSS'
OLD = datetime.utcnow() - timedelta(days=800)
MONTHS_AGO = datetime.utcnow() - timedelta(days=90)
PAST = date.today() - timedelta(days=5)
FUTURE = date.today() + timedelta(days=20)
MISSING = 7_000_000          # an id no table here will reach


def _add(made, kind, obj):
    db.session.add(obj)
    db.session.flush()
    made.setdefault(kind, []).append(obj.id)
    return obj


@pytest.fixture()
def world():
    made = {}
    with flask_app.app_context():
        db.create_all()
        for code, active in ((ME, True), (OTHER, True), (GONE, False),
                             (BOSS, True)):
            e = Employee.query.filter_by(emp_code=code).first()
            if e is None:
                e = Employee(emp_code=code, name=f'Placeholder {code}')
                db.session.add(e)
            e.role, e.is_active, e.must_change_pw = 'user', active, False
            e.vertical, e.is_super_admin, e.session_version = '', False, 0
        db.session.commit()
        set_profile(ME, DataScope.OWN, ['admin.master'], actor=BOSS)
        set_profile(OTHER, DataScope.OWN, [], actor=BOSS)
        set_profile(BOSS, DataScope.ALL, ['admin.master'], actor=BOSS)

        vert = MasterItem.query.filter_by(list_key='vertical',
                                          code='DQC_V').first()
        if vert is None:
            vert = MasterItem(list_key='vertical', code='DQC_V',
                              label='DQC Vertical', is_active=True)
            db.session.add(vert)
            db.session.flush()
        made['vertical'] = vert.id

        # accounts
        mine = _add(made, 'company', Company(
            name='Dqc Mine Works', pic_emp_code=ME, is_active=True,
            vertical='DQC Vertical'))
        theirs = _add(made, 'company', Company(
            name='Dqc Other Works', pic_emp_code=OTHER, is_active=True,
            vertical='DQC Vertical'))
        nopic = _add(made, 'company', Company(name='Dqc Nobody Works',
                                              is_active=True))
        leaver_acct = _add(made, 'company', Company(
            name='Dqc Leaver Works', pic_emp_code=GONE, is_active=True))
        bad_vert = _add(made, 'company', Company(
            name='Dqc Odd Vertical', pic_emp_code=ME, is_active=True,
            vertical='Not A Real Desk'))
        dupe_a = _add(made, 'company', Company(
            name='Dqc Twin A', pic_emp_code=ME, gstin='27DQCAA0000A1Z5',
            is_active=True))
        dupe_b = _add(made, 'company', Company(
            name='Dqc Twin B', pic_emp_code=ME, gstin='27dqcaa0000a1z5 ',
            is_active=True))
        dupe_other = _add(made, 'company', Company(
            name='Dqc Twin C', pic_emp_code=OTHER, gstin='29DQCOT0000A1Z5',
            is_active=True))
        dupe_other2 = _add(made, 'company', Company(
            name='Dqc Twin D', pic_emp_code=OTHER, gstin='29DQCOT0000A1Z5',
            is_active=True))
        sleepy = _add(made, 'company', Company(
            name='Dqc Sleepy Works', pic_emp_code=ME, is_active=True,
            created_at=OLD))
        noname = _add(made, 'company', Company(name=' ', pic_emp_code=ME,
                                               is_active=True))

        def lead(key, **kw):
            base = dict(company='Dqc Lead', stage='New', source='manual',
                        created_at=MONTHS_AGO, followup_date=FUTURE,
                        procam_vertical='DQC Vertical')
            base.update(kw)
            obj = _add(made, 'lead', Lead(**base))
            made[key] = obj.id
            return obj

        lead('l_unowned', assigned_to='')
        lead('l_unowned_archived', assigned_to='', is_archived=True)
        lead('l_leaver', assigned_to=GONE)
        lead('l_mine_bad', assigned_to=ME, followup_date=None,
             company='Completely Different Name', company_id=mine.id,
             procam_vertical='Not A Real Desk', source='email',
             classification='I_non_business')
        ok = lead('l_mine_ok', assigned_to=ME, company='Dqc Mine Works Pvt Ltd',
                  company_id=mine.id, source='email',
                  classification='A_new_lead')
        lead('l_other_bad', assigned_to=OTHER, followup_date=PAST,
             procam_vertical='Not A Real Desk')
        lead('l_new_unlinked', assigned_to=ME, created_at=datetime.utcnow())
        lead('l_blank_stage', assigned_to=ME, stage='')
        lead('l_orphan_acct', assigned_to=ME, company_id=MISSING)
        lead('l_review', assigned_to=ME, classification='J_needs_review')
        lead('l_closed', assigned_to=ME, stage='Lost', followup_date=None)
        db.session.add(LeadActivity(lead_id=ok.id, kind='call',
                                    occurred_at=datetime.utcnow()))
        db.session.flush()
        made['activity'] = [LeadActivity.query.filter_by(lead_id=ok.id)
                            .first().id]

        # opportunities
        def opp(key, **kw):
            base = dict(stage='Proposal', company_id=mine.id,
                        expected_close_date=FUTURE, value_inr=100)
            base.update(kw)
            base.setdefault('opp_number', f'DQC-{key}')
            obj = _add(made, 'opp', Opportunity(**base))
            made[key] = obj.id
            return obj

        opp('o_unowned', owner_emp_code='')
        opp('o_leaver', owner_emp_code=GONE)
        opp('o_mine_late', owner_emp_code=ME, expected_close_date=PAST)
        opp('o_mine_noclose', owner_emp_code=ME, expected_close_date=None)
        opp('o_mine_ok', owner_emp_code=ME, lead_id=ok.id)
        opp('o_other_late', owner_emp_code=OTHER, company_id=theirs.id,
            expected_close_date=PAST)
        opp('o_won_bare', owner_emp_code=ME, stage='Won', value_inr=None,
            won_at=datetime.utcnow())
        handed = opp('o_won_handed', owner_emp_code=ME, stage='Won',
                     won_at=datetime.utcnow())
        opp('o_wrong_lead', owner_emp_code=ME, company_id=theirs.id,
            lead_id=ok.id)
        opp('o_missing_lead', owner_emp_code=ME, lead_id=MISSING)
        opp('o_no_stage', owner_emp_code=ME, stage='')
        made['handover'] = [_add(made, 'handover_', WonHandover(
            opportunity_id=handed.id, account_id=mine.id,
            status='Awaiting PO')).id]

        # contacts
        for key, kw in (
                ('c_twin1', dict(email='twin@dqc.example')),
                ('c_twin2', dict(email='TWIN@dqc.example ')),
                ('c_lost_acct', dict(email='alone@dqc.example',
                                     company_id=MISSING))):
            made[key] = _add(made, 'contact', Contact(
                name=f'Dqc {key}', assigned_to=ME, is_active=True,
                **kw)).id

        # RFQs
        late = _add(made, 'rfq', RFQ(rfq_number='RFQ-DQC-LATE',
                                     subject='late', lead_driver=ME,
                                     quote_by_date=PAST))
        quoted = _add(made, 'rfq', RFQ(rfq_number='RFQ-DQC-DONE',
                                       subject='done', lead_driver=ME,
                                       quote_by_date=PAST))
        made['r_late'], made['r_quoted'] = late.id, quoted.id
        made['quote'] = [_add(made, 'quote_', Quote(
            quote_number='Q-DQC-1', rfq_id=quoted.id,
            prepared_by_id=ME)).id]

        # orphans and intake
        made['note'] = [_add(made, 'note_', LeadNote(
            lead_id=MISSING, note_text='orphan')).id]
        made['email'] = [_add(made, 'email_', LeadEmail(
            lead_id=made['l_mine_bad'], direction='outbound',
            intake_class='H_quote_submission',
            from_addr='rates@agent.example')).id,
            _add(made, 'email_', LeadEmail(
                lead_id=made['l_other_bad'], direction='outbound',
                intake_class='H_quote_submission',
                from_addr='rates@agent.example')).id]
        made['cls'] = [_add(made, 'cls_', EmailClassification(
            classification='A_new_lead', created_lead_id=MISSING,
            review_state='accepted')).id,
            _add(made, 'cls_', EmailClassification(
                classification='J_needs_review', review_state='pending',
                created_at=datetime.utcnow() - timedelta(days=30))).id]
        db.session.commit()

        made.update(mine=mine.id, theirs=theirs.id, nopic=nopic.id,
                    leaver_acct=leaver_acct.id, bad_vert=bad_vert.id,
                    dupe_a=dupe_a.id, dupe_b=dupe_b.id,
                    dupe_other=dupe_other.id, dupe_other2=dupe_other2.id,
                    sleepy=sleepy.id, noname=noname.id)
    yield made
    with flask_app.app_context():
        for model, key in ((WonHandover, 'handover'), (Quote, 'quote'),
                           (RFQ, 'rfq'), (LeadNote, 'note'),
                           (LeadEmail, 'email'), (EmailClassification, 'cls'),
                           (LeadActivity, 'activity'), (Contact, 'contact'),
                           (Opportunity, 'opp'), (Lead, 'lead'),
                           (Company, 'company')):
            ids = made.get(key) or []
            if ids:
                model.query.filter(model.id.in_(ids)).delete(
                    synchronize_session=False)
        MasterItem.query.filter_by(id=made['vertical']).delete()
        AccessProfile.query.filter(AccessProfile.emp_code.in_(
            [ME, OTHER, BOSS])).delete(synchronize_session=False)
        db.session.commit()


def everyone():
    return dq.system_scope()


def own():
    return scope_mod.for_employee(ME)


def flagged(key, sc):
    data = dq.records_for(key, sc=sc, per_page=100000)
    recs = ([r for g in data['groups'] for r in g['records']]
            if data['grouped'] else data['records'])
    return {(r['kind'], r['id']) for r in recs}


def test_every_definition_has_a_measurement_and_complete_metadata():
    assert [c.key for c in dq.CHECKS] == [d['key'] for d in defs.CHECKS]
    assert len({c.key for c in dq.CHECKS}) == len(dq.CHECKS)
    for c in dq.CHECKS:
        p = c.public()
        assert p['severity'] in defs.SEVERITIES
        assert p['title'] and p['cost'] and p['suggestion'] and p['route']
        assert p['cost'].endswith('.'), c.key


def test_ownership_checks(world):
    with flask_app.app_context():
        sc = everyone()
        unowned = flagged('unowned_leads', sc)
        assert ('Lead', world['l_unowned']) in unowned
        assert ('Lead', world['l_unowned_archived']) not in unowned, \
            'an archived lead is not work anybody should pick up'
        assert ('Lead', world['l_leaver']) in flagged('leads_of_leavers', sc)
        assert ('Lead', world['l_mine_ok']) not in flagged(
            'leads_of_leavers', sc)
        assert ('Opportunity', world['o_unowned']) in flagged(
            'unowned_opps', sc)
        assert ('Opportunity', world['o_leaver']) in flagged(
            'opps_of_leavers', sc)
        assert ('Account', world['nopic']) in flagged('no_pic', sc)
        leavers = flagged('accounts_of_leavers', sc)
        assert ('Account', world['leaver_acct']) in leavers
        assert ('Account', world['mine']) not in leavers


def test_activity_and_pipeline_checks(world):
    with flask_app.app_context():
        sc = everyone()
        stale = flagged('stale_leads', sc)
        assert ('Lead', world['l_mine_bad']) in stale
        assert ('Lead', world['l_mine_ok']) not in stale, \
            'a call today is contact'
        assert ('Lead', world['l_new_unlinked']) not in stale, \
            'a lead created today has not had the chance to be neglected'
        assert ('Lead', world['l_closed']) not in stale

        follow = flagged('leads_no_followup', sc)
        assert ('Lead', world['l_mine_bad']) in follow
        assert ('Lead', world['l_other_bad']) in follow, 'overdue counts'
        assert ('Lead', world['l_mine_ok']) not in follow
        assert ('Lead', world['l_closed']) not in follow

        stale_o = flagged('stale_opps', sc)
        assert ('Opportunity', world['o_mine_late']) in stale_o
        assert ('Opportunity', world['o_mine_ok']) not in stale_o
        assert ('Opportunity', world['o_won_bare']) not in stale_o

        noclose = flagged('opps_no_close_date', sc)
        assert ('Opportunity', world['o_mine_noclose']) in noclose
        assert ('Opportunity', world['o_mine_ok']) not in noclose

        rfq = flagged('rfq_no_quote', sc)
        assert ('RFQ', world['r_late']) in rfq
        assert ('RFQ', world['r_quoted']) not in rfq


def test_won_deal_checks(world):
    with flask_app.app_context():
        sc = everyone()
        assert ('Opportunity', world['o_won_bare']) in flagged(
            'won_no_value', sc)
        no_handover = flagged('won_no_handover', sc)
        assert ('Opportunity', world['o_won_bare']) in no_handover
        assert ('Opportunity', world['o_won_handed']) not in no_handover
        assert ('Handover', world['handover'][0]) in flagged('won_no_po', sc)


def test_duplicate_checks(world):
    with flask_app.app_context():
        data = dq.records_for('dupe_companies', sc=everyone(),
                              per_page=100000)
        gst = [g for g in data['groups']
               if g['name'] == 'Same GSTIN — 27DQCAA0000A1Z5']
        assert gst and {r['id'] for r in gst[0]['records']} == {
            world['dupe_a'], world['dupe_b']}
        assert ('Contact', world['c_twin1']) in flagged('dupe_contacts',
                                                        everyone())
        assert ('Contact', world['c_twin2']) in flagged('dupe_contacts',
                                                        everyone())
        assert ('Contact', world['c_lost_acct']) not in flagged(
            'dupe_contacts', everyone())


def test_integrity_checks(world):
    with flask_app.app_context():
        sc = everyone()
        orphans = flagged('orphan_records', sc)
        assert ('Note', world['note'][0]) in orphans
        assert ('Opportunity', world['o_missing_lead']) in orphans
        assert ('Lead', world['l_orphan_acct']) in orphans
        assert ('Opportunity', world['o_mine_ok']) not in orphans

        broken = flagged('broken_relationships', sc)
        assert ('Opportunity', world['o_wrong_lead']) in broken
        assert ('Opportunity', world['o_mine_ok']) not in broken
        assert ('Contact', world['c_lost_acct']) in broken

        empty = flagged('empty_mandatory', sc)
        assert ('Lead', world['l_blank_stage']) in empty
        assert ('Opportunity', world['o_no_stage']) in empty
        assert ('Account', world['noname']) in empty
        assert ('Lead', world['l_mine_ok']) not in empty


def test_mapping_checks(world):
    with flask_app.app_context():
        sc = everyone()
        mismatch = flagged('lead_account_mismatch', sc)
        assert ('Lead', world['l_mine_bad']) in mismatch
        assert ('Lead', world['l_mine_ok']) not in mismatch, \
            '"Pvt Ltd" is the same company'
        verts = flagged('lead_unknown_vertical', sc)
        assert ('Lead', world['l_mine_bad']) in verts
        assert ('Lead', world['l_mine_ok']) not in verts
        averts = flagged('account_unknown_vertical', sc)
        assert ('Account', world['bad_vert']) in averts
        assert ('Account', world['mine']) not in averts


def test_intake_checks(world):
    with flask_app.app_context():
        sc = everyone()
        non_lead = flagged('email_leads_non_lead', sc)
        assert ('Lead', world['l_mine_bad']) in non_lead
        assert ('Lead', world['l_mine_ok']) not in non_lead
        review = flagged('review_backlog', sc)
        assert ('Classification', world['cls'][1]) in review
        assert ('Lead', world['l_review']) in review
        assert ('Classification', world['cls'][0]) in flagged(
            'classification_orphans', sc)
        sent = flagged('quotes_filed_as_sent', sc)
        assert ('Email', world['email'][0]) in sent


def test_inactive_customers(world):
    with flask_app.app_context():
        inactive = flagged('inactive_customers', everyone())
        assert ('Account', world['sleepy']) in inactive
        assert ('Account', world['mine']) not in inactive


# ── scope ────────────────────────────────────────────────────────────
def test_own_scope_sees_only_its_own_problems(world):
    with flask_app.app_context():
        sc = own()
        follow = flagged('leads_no_followup', sc)
        assert ('Lead', world['l_mine_bad']) in follow
        assert ('Lead', world['l_other_bad']) not in follow
        stale_o = flagged('stale_opps', sc)
        assert ('Opportunity', world['o_mine_late']) in stale_o
        assert ('Opportunity', world['o_other_late']) not in stale_o
        assert ('Email', world['email'][1]) not in flagged(
            'quotes_filed_as_sent', sc)
        groups = dq.records_for('dupe_companies', sc=sc,
                                per_page=100000)['groups']
        seen = {r['id'] for g in groups for r in g['records']}
        assert world['dupe_a'] in seen
        assert world['dupe_other'] not in seen and \
            world['dupe_other2'] not in seen


def test_own_scope_counts_are_its_own_counts(world):
    """A count is a leak as much as a row: every number Own sees must be
    made only of records Own can open."""
    with flask_app.app_context():
        counts = {r['key']: r['count'] for r in dq.summary(own())}
        # Nothing unowned or owned by a leaver can be inside Own's scope.
        for key in ('unowned_leads', 'leads_of_leavers', 'unowned_opps',
                    'opps_of_leavers', 'no_pic', 'accounts_of_leavers',
                    'tasks_no_owner', 'pending_mappings',
                    'classification_orphans'):
            assert counts[key] == 0, key
        mine = Lead.query.filter(Lead.assigned_to == ME).all()
        expected = {l.id for l in mine if not l.is_archived
                    and (l.stage or '') not in defs.LEAD_TERMINAL_STAGES
                    and (l.followup_date is None
                         or l.followup_date < date.today())}
        assert counts['leads_no_followup'] == len(expected)
        orphan_kinds = {k for k, _ in flagged('orphan_records', own())}
        assert 'Note' not in orphan_kinds, \
            'a note on a missing lead has no owner to scope it by'


def test_outside_a_request_the_checks_see_everything(world):
    with flask_app.app_context():
        assert dq.resolve_scope().unrestricted


def test_an_anonymous_request_sees_nothing(world):
    with flask_app.test_request_context('/'):
        sc = dq.resolve_scope()
        assert not sc.unrestricted and not sc.codes
        assert dq.check_leads_without_followup()[0] == 0


def test_affected_ids_answer_within_scope(world):
    with flask_app.app_context():
        ids = [world['l_mine_bad'], world['l_other_bad'], world['l_mine_ok']]
        assert dq.affected_ids('leads_no_followup', ids, everyone()) == {
            world['l_mine_bad'], world['l_other_bad']}
        assert dq.affected_ids('leads_no_followup', ids, own()) == {
            world['l_mine_bad']}


def test_paging_walks_every_record_once(world):
    with flask_app.app_context():
        total = dq.records_for('leads_no_followup', sc=everyone(),
                               per_page=100000)
        seen, page = [], 1
        while True:
            data = dq.records_for('leads_no_followup', sc=everyone(),
                                  page=page, per_page=2)
            seen += [r['id'] for r in data['records']]
            if not data['truncated']:
                break
            page += 1
        assert sorted(seen) == sorted(r['id'] for r in total['records'])
        assert data['pages'] == page


def test_a_threshold_is_read_from_the_definitions(world, monkeypatch):
    """Thresholds live in one place; moving one moves the check."""
    with flask_app.app_context():
        assert ('Lead', world['l_mine_bad']) in flagged('stale_leads',
                                                        everyone())
        monkeypatch.setattr(defs, 'NO_CONTACT_DAYS', 365)
        assert ('Lead', world['l_mine_bad']) not in flagged('stale_leads',
                                                            everyone())


def test_only_a_crm_path_becomes_a_link():
    """A route is joined to the prefix and opened by the page; a stored
    "javascript:" or another site's address must not survive."""
    from app.models.task_engine import TaskInstance
    with flask_app.app_context():
        db.create_all()
        rows = [TaskInstance(task_key='dq.test', entity_type='Lead',
                             entity_id=MISSING, owner_user_id='',
                             status='Pending', priority=3, action_route=r)
                for r in ('javascript:alert(1)', '//evil.example/x',
                          'https://evil.example', '/app?lead=1')]
        db.session.add_all(rows)
        db.session.commit()
        ids = [r.id for r in rows]
        try:
            recs = {r['id']: r['route'] for r in dq.records_for(
                'tasks_no_owner', sc=everyone(), per_page=100000)['records']}
            assert [recs[i] for i in ids] == ['', '', '', '/app?lead=1']
        finally:
            TaskInstance.query.filter(TaskInstance.id.in_(ids)).delete(
                synchronize_session=False)
            db.session.commit()
