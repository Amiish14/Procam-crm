"""
Delete – not relevant, and the six things that stop it.

CRM-JUNK-DELETE-01.

Two failures these tests exist to prevent.

The first is the obvious one: this must not become the old delete.
830 leads were destroyed in September and October and 120 of them
left no record of who took them. So the tests check that a marked
lead is still *there* — queryable, restorable, in the bin — and the
CRM-NO-DELETE-01 source guard runs over this module's code too.

The second is quieter and is what the spec is actually about. A
lead with any history at all must be impossible to delete, by
anybody, because "it was only a newsletter" is what every
accidental deletion says afterwards. There is one test per rule in
§3, each one setting up exactly the history that should stop it,
and a test that the rules fire before the write and not after.
"""
import importlib
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'junk-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'JunkDelete-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'junk.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Lead = _main.Lead
Employee = _main.Employee
LeadActivity = _main.LeadActivity
LeadEmail = _main.LeadEmail
LeadNote = _main.LeadNote

from app.models.audit import AuditEvent                   # noqa: E402
from app.models.email_admin import VerticalHead           # noqa: E402
from app.models.integration import AppSetting             # noqa: E402
from app.models.master_data import MasterItem             # noqa: E402
from app.models.quote import Quote                        # noqa: E402
from app.models.rfq import RFQ                            # noqa: E402
from app.services import flags, junk_delete as jd         # noqa: E402
from app.services import junk_suggest as jsg              # noqa: E402

VERTICAL = 'Heavy Transport'
OWNER, HEAD, OTHER, ADMIN = 'ZJ_OWN', 'ZJ_HEAD', 'ZJ_OTH', 'ZJ_ADM'
FLAG = 'flag.FEATURE_JUNK_DELETE'


def _purge():
    for model in (LeadActivity, LeadEmail, LeadNote, RFQ, Quote, Lead):
        model.query.delete()
    # This module's own audit rows. SQLite reuses lead ids after a
    # purge, so lead #1 in the next test inherits the trail of lead
    # #1 in the last one — which is how a one-row assertion sees
    # four and nobody can tell why.
    AuditEvent.query.filter(AuditEvent.action.in_((
        jd.ACTION, jd.RESTORE_ACTION, 'lead.junk_purged',
        'sender_block.added', 'sender_block.refused'))).delete(
            synchronize_session=False)
    MasterItem.query.filter(
        MasterItem.list_key == 'system_sender',
        MasterItem.code.like('%zzjunk%')).delete(
            synchronize_session=False)
    Employee.query.filter(Employee.emp_code.in_(
        (OWNER, HEAD, OTHER, ADMIN))).delete(synchronize_session=False)
    VerticalHead.query.filter_by(vertical=VERTICAL).delete(
        synchronize_session=False)
    AppSetting.query.filter(AppSetting.key.like(
        'flag.FEATURE_JUNK%')).delete(synchronize_session=False)
    db.session.commit()
    flags.cache_clear()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code, name, role in ((OWNER, 'Owner Person', 'user'),
                                 (HEAD, 'Head Person', 'user'),
                                 (OTHER, 'Other Person', 'user'),
                                 (ADMIN, 'Admin Person', 'admin')):
            db.session.add(Employee(
                emp_code=code, name=name, password_hash='x',
                email=f'{code.lower()}@procamlogistics.com',
                role=role, vertical=VERTICAL))
        db.session.add(VerticalHead(vertical=VERTICAL,
                                    head_emp_code=HEAD, active=True))
        db.session.commit()
        yield
        _purge()


def _on(name='FEATURE_JUNK_DELETE', state=True):
    key = f'flag.{name}'
    row = AppSetting.query.filter_by(key=key).first()
    if row is None:
        row = AppSetting(key=key)
        db.session.add(row)
    row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


def _lead(*, stage=None, sender='nikusi@zzjunk-mail.com',
          company='Unknown', owner=OWNER):
    lead = Lead(company=company, stage=stage or jd.ELIGIBLE_STAGE,
                procam_vertical=VERTICAL, source='email',
                assigned_to=owner, original_email_from=sender,
                original_email_subject='Our newsletter for October',
                created_at=datetime.utcnow())
    db.session.add(lead)
    db.session.commit()
    return lead


def _client(code=OWNER):
    client = flask_app.test_client()
    with client.session_transaction() as s:
        s['emp_code'] = code
        s['role'] = 'admin' if code == ADMIN else 'user'
    return client


# ── the flag off changes nothing ─────────────────────────────────────
def test_with_the_flag_off_there_is_no_route():
    with flask_app.app_context():
        _on(state=False)
        lead = _lead()
        lid = lead.id
    client = _client()
    assert client.get(f'/api/leads/{lid}/junk-check').status_code == 404
    assert client.post(f'/api/leads/{lid}/junk-delete',
                       json={'reason': 'spam'}).status_code == 404
    assert client.get('/admin/deleted-leads').status_code == 404
    assert client.get('/api/leads/junk-available').status_code == 404


def test_with_the_flag_off_nothing_is_hidden():
    """The exclusion is keyed on the column, not on the flag — but
    with the flag off nothing ever sets it, so every board reads as
    it did."""
    with flask_app.app_context():
        _on(state=False)
        lead = _lead()
        from app.access import scope as sc_mod
        from app.access.scope import Scope

        sc = Scope(emp_code=OWNER, vertical=VERTICAL, codes=[OWNER],
                   perms=set(), data_scope='Own')
        assert lead.id in {l.id for l in sc_mod.leads(sc=sc).all()}


# ── §3 · one test per eligibility rule ───────────────────────────────
def test_a_fresh_mail_lead_is_eligible():
    with flask_app.app_context():
        assert jd.eligible(_lead()) is True


def test_a_lead_past_new_opportunity_is_not():
    with flask_app.app_context():
        lead = _lead(stage='Quoted')
        keys = dict(jd.blockers(lead))
        assert 'stage' in keys
        assert 'New Opportunity' in keys['stage']


def test_a_lead_with_an_rfq_is_not():
    with flask_app.app_context():
        lead = _lead()
        db.session.add(RFQ(lead_id=lead.id, rfq_number='ZZ-RFQ-1',
                           subject='ZZ enquiry'))
        db.session.commit()
        assert 'paperwork' in dict(jd.blockers(lead))


def test_a_lead_with_a_quotation_is_not():
    with flask_app.app_context():
        lead = _lead()
        db.session.add(Quote(lead_id=lead.id, quote_number='ZZ-Q-1'))
        db.session.commit()
        assert 'paperwork' in dict(jd.blockers(lead))


def test_a_lead_we_have_written_to_is_not():
    with flask_app.app_context():
        lead = _lead()
        db.session.add(LeadEmail(
            lead_id=lead.id, direction='outbound',
            subject='Our quotation', status='sent'))
        db.session.commit()
        why = dict(jd.blockers(lead))
        assert 'outgoing_mail' in why
        assert 'sent' in why['outgoing_mail']


def test_inbound_mail_alone_does_not_stop_it():
    """The lead *is* an inbound mail. If that counted as activity
    nothing would ever be deletable and the feature would be a
    button that never works."""
    with flask_app.app_context():
        lead = _lead()
        db.session.add(LeadEmail(
            lead_id=lead.id, direction='inbound',
            subject='Our newsletter', status='received'))
        db.session.commit()
        assert jd.eligible(lead) is True


def test_a_logged_call_stops_it():
    with flask_app.app_context():
        lead = _lead()
        db.session.add(LeadActivity(lead_id=lead.id, kind='call',
                                    subject='Rang them'))
        db.session.commit()
        assert 'activity' in dict(jd.blockers(lead))


def test_a_note_somebody_wrote_stops_it():
    with flask_app.app_context():
        lead = _lead()
        db.session.add(LeadNote(lead_id=lead.id,
                                note_text='Spoke to the buyer'))
        db.session.commit()
        assert 'activity' in dict(jd.blockers(lead))


def test_a_deleted_note_does_not_stop_it():
    with flask_app.app_context():
        lead = _lead()
        db.session.add(LeadNote(lead_id=lead.id, note_text='oops',
                                is_deleted=True))
        db.session.commit()
        assert jd.eligible(lead) is True


def test_an_opportunity_number_stops_it():
    with flask_app.app_context():
        lead = _lead()
        lead.opp_number = 'OPP-2026-0001'
        db.session.commit()
        assert 'links' in dict(jd.blockers(lead))


def test_being_a_merge_target_stops_it():
    with flask_app.app_context():
        kept = _lead()
        other = _lead(company='ZZ Other')
        other.lost_reason = 'Others'
        other.notes = f'Closed as a duplicate of #{kept.id}'
        db.session.commit()
        assert 'links' in dict(jd.blockers(kept))


def test_an_already_deleted_lead_is_not_deleted_twice():
    with flask_app.app_context():
        lead = _lead()
        jd.mark_not_relevant(lead, reason='spam', actor=OWNER)
        assert 'already' in dict(jd.blockers(lead))


def test_the_hover_says_where_to_go_instead():
    assert 'Close as Lost' in jd.HOVER


# ── §4 · who ─────────────────────────────────────────────────────────
def test_the_owner_may():
    with flask_app.app_context():
        assert jd.may_delete(_lead(), OWNER) is True


def test_the_vertical_head_may():
    with flask_app.app_context():
        assert jd.may_delete(_lead(), HEAD) is True


def test_an_admin_may():
    with flask_app.app_context():
        assert jd.may_delete(_lead(), ADMIN) is True


def test_nobody_else_may():
    with flask_app.app_context():
        assert jd.may_delete(_lead(), OTHER) is False
        assert jd.may_delete(_lead(), None) is False


def test_a_head_of_another_vertical_may_not():
    """`heads_for_vertical` answers Management's head for a vertical
    it cannot place, which would hand one person the delete for
    every lead with a blank or misspelt vertical."""
    with flask_app.app_context():
        lead = _lead()
        lead.procam_vertical = 'Not A Real Vertical'
        db.session.commit()
        assert jd.may_delete(lead, HEAD) is False


def test_the_api_refuses_somebody_elses_lead():
    with flask_app.app_context():
        _on()
        lid = _lead().id
    r = _client(OTHER).post(f'/api/leads/{lid}/junk-delete',
                            json={'reason': 'spam'})
    assert r.status_code == 403
    with flask_app.app_context():
        assert Lead.query.get(lid).junk_deleted is not True


# ── §5–6 · the delete itself ─────────────────────────────────────────
def test_the_reason_must_be_one_of_the_six():
    with flask_app.app_context():
        with pytest.raises(ValueError):
            jd.mark_not_relevant(_lead(), reason='because', actor=OWNER)


def test_others_needs_words():
    with flask_app.app_context():
        with pytest.raises(ValueError):
            jd.mark_not_relevant(_lead(), reason='other', actor=OWNER)


def test_duplicate_needs_the_number():
    with flask_app.app_context():
        with pytest.raises(ValueError):
            jd.mark_not_relevant(_lead(), reason='duplicate', actor=OWNER)
        lead = _lead(company='ZZ Two')
        jd.mark_not_relevant(lead, reason='duplicate',
                             note='#4019', actor=OWNER)
        assert lead.junk_reason_note == '#4019'


def test_the_delete_marks_rather_than_destroys():
    with flask_app.app_context():
        lead = _lead()
        lid = lead.id
        jd.mark_not_relevant(lead, reason='spam', actor=OWNER)
        again = Lead.query.get(lid)
        assert again is not None
        assert again.junk_deleted is True
        assert again.junk_deleted_by == OWNER
        assert again.junk_reason == 'spam'
        assert again.junk_deleted_at is not None
        # archived too, so the modules that already hide archived
        # leads hide this one without being changed
        assert again.is_archived is True


def test_every_delete_is_audited_with_the_sender_and_subject():
    with flask_app.app_context():
        lead = _lead()
        lid = lead.id
        jd.mark_not_relevant(lead, reason='spam', actor=OWNER)
        rows = AuditEvent.query.filter_by(
            action=jd.ACTION, entity_id=str(lid)).all()
        assert len(rows) == 1
        new = rows[0].new_value or {}
        if isinstance(new, str):
            new = json.loads(new)
        assert new['sender'] == 'nikusi@zzjunk-mail.com'
        assert 'newsletter' in (new['subject'] or '').lower()
        assert rows[0].actor == OWNER


# ── §6 · it disappears from everywhere ───────────────────────────────
def test_it_is_gone_from_the_scope_every_board_reads():
    with flask_app.app_context():
        from app.access import scope as sc_mod
        from app.access.scope import Scope

        lead = _lead()
        sc = Scope(emp_code=OWNER, vertical=VERTICAL, codes=[OWNER],
                   perms=set(), data_scope='Own')
        assert lead.id in {l.id for l in sc_mod.leads(sc=sc).all()}
        jd.mark_not_relevant(lead, reason='spam', actor=OWNER)
        assert lead.id not in {l.id for l in sc_mod.leads(sc=sc).all()}


def test_an_admin_does_not_see_it_either():
    """An unrestricted scope is the one that would otherwise still
    show it — and an administrator's board is where it would be most
    confusing."""
    with flask_app.app_context():
        from app.access import scope as sc_mod
        from app.access.scope import Scope

        lead = _lead()
        sc = Scope(emp_code=ADMIN, vertical=None, codes=None,
                   perms=set(), data_scope='All')
        jd.mark_not_relevant(lead, reason='spam', actor=OWNER)
        assert lead.id not in {l.id for l in sc_mod.leads(sc=sc).all()}


def test_the_bin_is_the_one_place_it_does_appear():
    with flask_app.app_context():
        from app.access import scope as sc_mod
        from app.access.scope import Scope

        lead = _lead()
        jd.mark_not_relevant(lead, reason='spam', actor=OWNER)
        sc = Scope(emp_code=ADMIN, vertical=None, codes=None,
                   perms=set(), data_scope='All')
        seen = sc_mod.leads(sc=sc, include_deleted=True).all()
        assert lead.id in {l.id for l in seen}
        assert lead.id in {l.id for l in jd.deleted().all()}


def test_the_api_list_no_longer_returns_it():
    with flask_app.app_context():
        _on()
        lead = _lead()
        lid = lead.id
    client = _client()
    before = client.get('/api/leads?limit=500').get_json()
    assert lid in {l['id'] for l in before}
    r = client.post(f'/api/leads/{lid}/junk-delete',
                    json={'reason': 'spam'})
    assert r.status_code == 200, r.get_json()
    after = client.get('/api/leads?limit=500').get_json()
    assert lid not in {l['id'] for l in after}


def test_notes_search_does_not_resolve_a_deleted_lead():
    """Notes Search scores notes and then looks their leads up. The
    note survives a delete, so without the filter the search would
    still offer a hit that opens nothing."""
    import inspect as _inspect

    import app.notes_search as ns

    src = _inspect.getsource(ns)
    assert 'junk_deleted' in src, \
        'notes_search resolves leads without excluding deleted ones'


def test_a_deleted_lead_is_absent_from_the_module_level_queries():
    """pic360, company360, the owner digest and the funnel all read
    leads without going through scope.leads(). Each was given the
    filter; this is what notices if one is removed."""
    import inspect as _inspect

    import app.company360.service as c360
    import app.pic360.service as p360
    import app.services.owner_digest as digest

    for mod in (c360, p360, digest):
        assert 'junk_deleted' in _inspect.getsource(mod), mod.__name__


def test_its_open_tasks_are_closed():
    with flask_app.app_context():
        from app.models.task_engine import (TaskInstance,
                                            TaskInstanceStatus)

        lead = _lead()
        db.session.add(TaskInstance(
            task_key='zz.junk.check', entity_type='lead',
            entity_id=lead.id, owner_user_id=OWNER,
            status=TaskInstanceStatus.PENDING))
        db.session.commit()
        out = jd.mark_not_relevant(lead, reason='spam', actor=OWNER)
        assert out['tasks_closed'] == 1
        row = TaskInstance.query.filter_by(
            task_key='zz.junk.check', entity_id=lead.id).first()
        assert row.status in TaskInstanceStatus.CLOSED
        TaskInstance.query.filter_by(task_key='zz.junk.check').delete()
        db.session.commit()


def test_an_open_system_task_does_not_block_the_delete():
    """The engine raises a task on every new lead. If an open task
    counted as activity, nothing arriving by mail would ever be
    deletable."""
    with flask_app.app_context():
        from app.models.task_engine import (TaskInstance,
                                            TaskInstanceStatus)

        lead = _lead()
        db.session.add(TaskInstance(
            task_key='zz.junk.auto', entity_type='lead',
            entity_id=lead.id, owner_user_id=OWNER,
            status=TaskInstanceStatus.PENDING))
        db.session.commit()
        assert jd.eligible(lead) is True
        TaskInstance.query.filter_by(task_key='zz.junk.auto').delete()
        db.session.commit()


def test_a_completed_task_does_block_it():
    with flask_app.app_context():
        from app.models.task_engine import (TaskInstance,
                                            TaskInstanceStatus)

        lead = _lead()
        db.session.add(TaskInstance(
            task_key='zz.junk.done', entity_type='lead',
            entity_id=lead.id, owner_user_id=OWNER,
            status=TaskInstanceStatus.COMPLETED))
        db.session.commit()
        assert 'activity' in dict(jd.blockers(lead))
        TaskInstance.query.filter_by(task_key='zz.junk.done').delete()
        db.session.commit()


# ── §5.3 · also block this sender ────────────────────────────────────
def test_blocking_the_sender_writes_the_address_not_the_domain():
    with flask_app.app_context():
        lead = _lead(sender='spammer@zzjunk-mail.com')
        out = jd.mark_not_relevant(lead, reason='spam', actor=OWNER,
                        block_sender=True)
        assert out['sender_blocked'] is True
        row = MasterItem.query.filter_by(
            list_key='system_sender',
            code='spammer@zzjunk-mail.com').first()
        assert row is not None and row.is_active
        # never the whole domain
        assert MasterItem.query.filter_by(
            list_key='system_sender',
            code='*@zzjunk-mail.com').first() is None


def test_a_procam_address_is_never_blocked():
    with flask_app.app_context():
        lead = _lead(sender='colleague@procamgroup.in')
        out = jd.mark_not_relevant(lead, reason='internal', actor=OWNER,
                        block_sender=True)
        assert out['sender_blocked'] is False
        assert out['block_refused'] is True
        assert MasterItem.query.filter_by(
            list_key='system_sender',
            code='colleague@procamgroup.in').first() is None
        # and it is recorded, so nobody wonders why it did nothing
        assert AuditEvent.query.filter_by(
            action='sender_block.refused').count() == 1
        # the lead is still deleted — the block was the optional part
        assert lead.junk_deleted is True


def test_spam_and_personal_are_the_two_that_tick_the_box():
    assert jd.blocks_sender_by_default('spam') is True
    assert jd.blocks_sender_by_default('internal') is True
    assert jd.blocks_sender_by_default('test') is False
    assert jd.blocks_sender_by_default('not_logistics') is False


# ── §7 · the bin and restore ─────────────────────────────────────────
def test_restore_brings_it_back_unchanged():
    with flask_app.app_context():
        lead = _lead()
        lid, owner = lead.id, lead.assigned_to
        jd.mark_not_relevant(lead, reason='spam', actor=OWNER)
        jd.restore(lead, actor=ADMIN)
        back = Lead.query.get(lid)
        assert back.junk_deleted is False
        assert back.is_archived is False
        assert back.stage == jd.ELIGIBLE_STAGE
        assert back.assigned_to == owner
        assert back.junk_reason is None
        assert AuditEvent.query.filter_by(
            action=jd.RESTORE_ACTION, entity_id=str(lid)).count() == 1


def test_a_restored_lead_is_back_on_the_board():
    with flask_app.app_context():
        from app.access import scope as sc_mod
        from app.access.scope import Scope

        lead = _lead()
        sc = Scope(emp_code=OWNER, vertical=VERTICAL, codes=[OWNER],
                   perms=set(), data_scope='Own')
        jd.mark_not_relevant(lead, reason='spam', actor=OWNER)
        jd.restore(lead, actor=ADMIN)
        assert lead.id in {l.id for l in sc_mod.leads(sc=sc).all()}


def test_restoring_something_that_is_not_in_the_bin_says_so():
    with flask_app.app_context():
        with pytest.raises(ValueError):
            jd.restore(_lead(), actor=ADMIN)


def test_the_bin_filters_by_reason_and_sender():
    with flask_app.app_context():
        one = _lead(sender='a@zzjunk-mail.com')
        two = _lead(sender='b@zzother-mail.com', company='ZZ Two')
        jd.mark_not_relevant(one, reason='spam', actor=OWNER)
        jd.mark_not_relevant(two, reason='test', actor=OWNER)
        assert {l.id for l in jd.deleted(reason='spam').all()} == {one.id}
        assert {l.id for l in jd.deleted(sender='zzother').all()} \
            == {two.id}
        assert {l.id for l in jd.deleted(by=OWNER).all()} \
            == {one.id, two.id}


def test_the_bin_page_renders_for_an_admin_with_the_flag_on():
    with flask_app.app_context():
        _on()
        lead = _lead()
        jd.mark_not_relevant(lead, reason='spam', actor=OWNER)
        lid = lead.id
    r = _client(ADMIN).get('/admin/deleted-leads')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'Deleted leads' in body
    assert f'#{lid}' in body
    assert 'Restore' in body


def test_the_bin_is_admins_only():
    with flask_app.app_context():
        _on()
    assert _client(OWNER).get('/admin/deleted-leads').status_code == 403
    assert _client(HEAD).get('/admin/deleted-leads').status_code == 403


def test_restore_over_the_api_is_admins_only():
    with flask_app.app_context():
        _on()
        lead = _lead()
        jd.mark_not_relevant(lead, reason='spam', actor=OWNER)
        lid = lead.id
    assert _client(OWNER).post(
        f'/api/admin/deleted-leads/{lid}/restore').status_code == 403
    r = _client(ADMIN).post(f'/api/admin/deleted-leads/{lid}/restore')
    assert r.status_code == 200
    with flask_app.app_context():
        assert Lead.query.get(lid).junk_deleted is False


def test_the_export_lists_what_was_removed():
    with flask_app.app_context():
        _on()
        lead = _lead()
        jd.mark_not_relevant(lead, reason='spam', actor=OWNER)
    r = _client(ADMIN).get('/api/admin/deleted-leads/export')
    assert r.status_code == 200
    assert len(r.get_data()) > 0


# ── §8 · bulk ────────────────────────────────────────────────────────
def test_the_preview_says_what_will_and_will_not_go():
    with flask_app.app_context():
        _on()
        good = _lead()
        worked = _lead(company='ZZ Worked', stage='Quoted')
        ids = [good.id, worked.id]
    r = _client().post('/api/leads/junk-preview', json={'ids': ids})
    out = r.get_json()
    assert out['eligible'] == 1
    assert out['skipped'] == 1
    assert 'New Opportunity' in out['skipped_rows'][0]['why']
    assert out['confirm_label'] == 'Delete 1 lead(s)'


def test_bulk_deletes_the_eligible_and_leaves_the_rest():
    with flask_app.app_context():
        _on()
        good = _lead()
        worked = _lead(company='ZZ Worked', stage='Quoted')
        ids, gid, wid = [good.id, worked.id], good.id, worked.id
    r = _client().post('/api/leads/junk-delete-bulk',
                       json={'ids': ids, 'reason': 'spam'})
    out = r.get_json()
    assert out['ok'] and out['deleted'] == 1 and out['skipped'] == 1
    with flask_app.app_context():
        assert Lead.query.get(gid).junk_deleted is True
        assert Lead.query.get(wid).junk_deleted is not True


def test_more_than_twenty_five_needs_an_admin():
    with flask_app.app_context():
        _on()
        ids = [_lead(company=f'ZZ {i}').id
               for i in range(jd.BULK_ADMIN_ABOVE + 1)]
    r = _client(OWNER).post('/api/leads/junk-delete-bulk',
                            json={'ids': ids, 'reason': 'spam'})
    assert r.status_code == 403
    assert str(jd.BULK_ADMIN_ABOVE) in r.get_json()['error']
    with flask_app.app_context():
        assert Lead.query.filter(
            Lead.junk_deleted.is_(True)).count() == 0
    # and an admin can
    r = _client(ADMIN).post('/api/leads/junk-delete-bulk',
                            json={'ids': ids, 'reason': 'spam'})
    assert r.status_code == 200
    assert r.get_json()['deleted'] == len(ids)


# ── §8 · the daily cap ───────────────────────────────────────────────
def test_the_cap_reports_rather_than_refuses():
    with flask_app.app_context():
        _on()
        for i in range(jd.DAILY_CAP):
            lead = _lead(company=f'ZZ Cap {i}')
            jd.mark_not_relevant(lead, reason='spam', actor=OWNER)
        assert jd.deleted_today(OWNER) == jd.DAILY_CAP
        over, total = jd.over_cap(OWNER, adding=1)
        assert over is True and total == jd.DAILY_CAP + 1
        lid = _lead(company='ZZ One More').id
    r = _client(OWNER).post(f'/api/leads/{lid}/junk-delete',
                            json={'reason': 'spam'})
    assert r.status_code == 200
    assert r.get_json()['over_cap'] is True
    with flask_app.app_context():
        # nothing refused — the lead did go
        assert Lead.query.get(lid).junk_deleted is True


def test_the_cap_does_not_apply_to_an_admin():
    with flask_app.app_context():
        over, _n = jd.over_cap(ADMIN, adding=10_000)
        assert over is False


# ── §9 · the suggestion only suggests ────────────────────────────────
def test_the_chip_is_silent_while_its_flag_is_off():
    with flask_app.app_context():
        _on('FEATURE_JUNK_SUGGEST', False)
        show, _why = jsg.chip(_lead())
        assert show is False


def test_a_newsletter_header_is_a_reason_to_look():
    with flask_app.app_context():
        _on('FEATURE_JUNK_SUGGEST', True)
        lead = _lead()
        lead.original_email_body = (
            'List-Unsubscribe: <mailto:x@y.com>\nHello')
        db.session.commit()
        show, why = jsg.chip(lead)
        assert show is True
        assert 'list-unsubscribe' in why.lower()
        assert 'Nobody has decided' in why


def test_a_free_mailbox_with_a_real_company_is_not_flagged():
    """226 of the 247 leads the obvious rule would have caught were
    real freight postings. A personal mailbox is not evidence."""
    with flask_app.app_context():
        _on('FEATURE_JUNK_SUGGEST', True)
        lead = _lead(sender='buyer@gmail.com', company='Isgec Heavy')
        lead.original_email_subject = 'Load posted: Pune to Haldia'
        db.session.commit()
        show, why = jsg.chip(lead)
        assert show is False, why


def test_a_free_mailbox_with_no_company_is_flagged():
    with flask_app.app_context():
        _on('FEATURE_JUNK_SUGGEST', True)
        lead = _lead(sender='someone@gmail.com', company='Unknown')
        lead.original_email_subject = 'hi'
        db.session.commit()
        show, why = jsg.chip(lead)
        assert show is True
        assert 'no company name' in why


def test_the_suggestion_never_deletes_anything():
    with flask_app.app_context():
        _on('FEATURE_JUNK_SUGGEST', True)
        lead = _lead(sender='someone@gmail.com')
        jsg.chip(lead)
        assert lead.junk_deleted is not True


# ── §10 · the clean-up script ────────────────────────────────────────
def _cleanup():
    import importlib.util as u
    spec = u.spec_from_file_location(
        'junk_cleanup', os.path.join(_ROOT, 'scripts',
                                     'junk_cleanup.py'))
    mod = u.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_cleanup_lists_per_sender_without_applying():
    with flask_app.app_context():
        _on('FEATURE_JUNK_SUGGEST', True)
        one = _lead(sender='nikusi@zzjunk-mail.com')
        two = _lead(sender='nikusi@zzjunk-mail.com', company='ZZ Two')
        mod = _cleanup()
        rows = mod.candidates(senders=['nikusi@zzjunk-mail.com'])
        assert {r['lead'].id for r in rows} == {one.id, two.id}
        text = mod.report(rows)
        assert '2' in text and 'nikusi@zzjunk-mail.com' in text
        assert Lead.query.filter(
            Lead.junk_deleted.is_(True)).count() == 0


def test_the_cleanup_applies_only_the_numbers_it_is_given():
    with flask_app.app_context():
        one = _lead(sender='nikusi@zzjunk-mail.com')
        two = _lead(sender='nikusi@zzjunk-mail.com', company='ZZ Two')
        mod = _cleanup()
        done, refused = mod.apply([one.id], reason='internal', note='',
                                  actor=ADMIN, day='26-Oct-2026')
        assert done == [one.id] and refused == []
        assert Lead.query.get(one.id).junk_deleted is True
        assert Lead.query.get(two.id).junk_deleted is not True
        assert '26-Oct-2026' in Lead.query.get(one.id).junk_reason_note


def test_the_cleanup_refuses_a_lead_that_was_worked():
    with flask_app.app_context():
        worked = _lead(stage='Quoted')
        mod = _cleanup()
        done, refused = mod.apply([worked.id], reason='spam', note='',
                                  actor=ADMIN, day='26-Oct-2026')
        assert done == []
        assert refused and 'New Opportunity' in refused[0][1]


# ── the rule that has no exceptions ──────────────────────────────────
def test_no_module_here_destroys_a_lead():
    """CRM-NO-DELETE-01 reads the source of every file under app/ and
    scripts/, this release included. Asserted here too so the reason
    is recorded next to the feature that most looks like a delete."""
    import tests.test_no_lead_hard_delete as guard

    guard.test_no_code_path_can_hard_delete_a_lead()
    guard.test_the_allowlist_names_only_what_it_should()


def test_even_the_purge_closes_rather_than_destroys():
    with flask_app.app_context():
        _on()
        _on('FEATURE_JUNK_PURGE', True)
        lead = _lead()
        jd.mark_not_relevant(lead, reason='spam', actor=OWNER)
        lead.junk_deleted_at = datetime.utcnow() - timedelta(days=200)
        db.session.commit()
        lid = lead.id
    client = _client(ADMIN)
    staged = client.post('/api/admin/deleted-leads/purge',
                         json={}).get_json()
    assert staged['staged'] is True and staged['count'] == 1
    with flask_app.app_context():
        assert Lead.query.get(lid).junk_deleted is True   # not yet
    done = client.post('/api/admin/deleted-leads/purge',
                       json={'confirm': True, 'exported': True})
    assert done.get_json()['purged'] == 1
    with flask_app.app_context():
        after = Lead.query.get(lid)
        assert after is not None          # the row is still there
        assert after.stage == 'Lost'
        assert after.junk_deleted is False


def test_the_purge_is_off_by_default():
    with flask_app.app_context():
        _on()
        lead = _lead()
        jd.mark_not_relevant(lead, reason='spam', actor=OWNER)
    r = _client(ADMIN).post('/api/admin/deleted-leads/purge',
                            json={'confirm': True, 'exported': True})
    assert r.status_code == 403


def test_the_three_flags_are_off_by_default():
    for name in ('FEATURE_JUNK_DELETE', 'FEATURE_JUNK_SUGGEST',
                 'FEATURE_JUNK_PURGE'):
        assert flags.DEFAULTS[name] is False
