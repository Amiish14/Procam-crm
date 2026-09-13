"""Data Quality screens and batch correction: scoped, previewed, audited.

The rules under test:
  * The dashboard, its API, the detail page and the CSV all stay inside
    the viewer's Access Matrix scope — rows and counts both.
  * A batch correction changes nothing until it is previewed, and applies
    only what was previewed: a different value, a different person, an
    expired preview or a record edited in between is refused whole.
  * A record outside the viewer's scope refuses the batch, with the same
    answer as a record that does not exist.
  * Every correction leaves a data_quality.batch_fix event with its reason,
    and nothing is ever deleted.
  * CSV cells cannot run as spreadsheet formulas.

The database is shared with the whole run; only records made here are
asserted on, and they are removed afterwards.
"""
import csv
import io
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'DqBatchTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'dqbatch.db'))

from app import (app as flask_app, db, Company, Employee, Lead,    # noqa: E402
                 LeadAssignmentHistory, Opportunity)
import app.models                                                   # noqa: E402,F401
from app.access.service import set_profile                          # noqa: E402
from app.data_quality import batch                                  # noqa: E402
from app.data_quality import definitions as defs                    # noqa: E402
from app.data_quality import service as dq                          # noqa: E402
from app.models.access import AccessProfile, DataScope              # noqa: E402
from app.models.audit import AuditEvent, DeletionAudit              # noqa: E402
from app.models.data_quality import DataQualitySnapshot             # noqa: E402

flask_app.config['WTF_CSRF_ENABLED'] = False

BOSS, ME, OTHER, GONE, NOPE = 'DQBBOSS', 'DQBME', 'DQBOTHER', 'DQBGONE', \
    'DQBNOPE'
PAST = date.today() - timedelta(days=3)
SOON = (date.today() + timedelta(days=7)).isoformat()
LATER = (date.today() + timedelta(days=14)).isoformat()


@pytest.fixture()
def world():
    made = {'lead': [], 'opp': [], 'company': []}
    started = datetime.utcnow() - timedelta(seconds=1)
    with flask_app.app_context():
        db.create_all()
        for code, active in ((BOSS, True), (ME, True), (OTHER, True),
                             (GONE, False), (NOPE, True)):
            e = Employee.query.filter_by(emp_code=code).first()
            if e is None:
                e = Employee(emp_code=code, name=f'Placeholder {code}')
                db.session.add(e)
            e.role, e.is_active, e.must_change_pw = 'user', active, False
            e.vertical, e.is_super_admin, e.session_version = '', False, 0
        db.session.commit()
        set_profile(BOSS, DataScope.ALL, ['admin.master'], actor=BOSS)
        set_profile(ME, DataScope.OWN, ['admin.master'], actor=BOSS)
        set_profile(OTHER, DataScope.OWN, [], actor=BOSS)
        set_profile(NOPE, DataScope.OWN, ['module.rfq'], actor=BOSS)

        def add(kind, obj):
            db.session.add(obj)
            db.session.flush()
            made[kind].append(obj.id)
            return obj.id

        made['exact'] = add('company', Company(name='Dqb Exact Name Works',
                                               pic_emp_code=ME, is_active=True))
        for _ in range(2):
            add('company', Company(name='Dqb Twice Works', pic_emp_code=ME,
                                   is_active=True))
        made['nopic'] = add('company', Company(name='Dqb Nobody Works',
                                               is_active=True))
        base = dict(stage='New', source='manual')
        made['l_mine'] = add('lead', Lead(company='Dqb Mine One',
                                          assigned_to=ME, **base))
        made['l_mine2'] = add('lead', Lead(company='Dqb Mine Two',
                                           assigned_to=ME,
                                           followup_date=PAST, **base))
        made['l_other'] = add('lead', Lead(company='Dqb Secret Other',
                                           assigned_to=OTHER, **base))
        made['l_formula'] = add('lead', Lead(
            company='=HYPERLINK("http://evil.example","x")',
            assigned_to=ME, **base))
        made['l_unlinked'] = add('lead', Lead(
            company='  dqb exact name works ', assigned_to=ME,
            followup_date=date.today() + timedelta(days=30), **base))
        made['l_twice'] = add('lead', Lead(
            company='Dqb Twice Works', assigned_to=ME,
            followup_date=date.today() + timedelta(days=30), **base))
        made['l_leaver'] = add('lead', Lead(company='Dqb Leaver Lead',
                                            assigned_to=GONE, **base))
        made['o_mine'] = add('opp', Opportunity(opp_number='DQB-MINE',
                                                stage='Proposal',
                                                owner_emp_code=ME))
        made['o_other'] = add('opp', Opportunity(opp_number='DQB-OTHER',
                                                 stage='Proposal',
                                                 owner_emp_code=OTHER))
        db.session.commit()
    yield made
    with flask_app.app_context():
        lead_ids = [str(i) for i in made['lead']]
        LeadAssignmentHistory.query.filter(
            LeadAssignmentHistory.lead_id.in_(made['lead'])).delete(
            synchronize_session=False)
        DeletionAudit.query.filter(
            DeletionAudit.entity_type == 'Lead',
            DeletionAudit.entity_id.in_(made['lead']),
            DeletionAudit.performed_at >= started).delete(
            synchronize_session=False)
        AuditEvent.query.filter(AuditEvent.occurred_at >= started,
                                AuditEvent.entity_type.in_(
                                    ('lead', 'opportunity', 'company',
                                     'data_quality'))).filter(
            (AuditEvent.entity_type == 'data_quality')
            | AuditEvent.entity_id.in_(
                lead_ids + [str(i) for i in made['opp'] + made['company']])
        ).delete(synchronize_session=False)
        Opportunity.query.filter(Opportunity.id.in_(made['opp'])).delete(
            synchronize_session=False)
        Lead.query.filter(Lead.id.in_(made['lead'])).delete(
            synchronize_session=False)
        Company.query.filter(Company.id.in_(made['company'])).delete(
            synchronize_session=False)
        AccessProfile.query.filter(AccessProfile.emp_code.in_(
            [BOSS, ME, OTHER, NOPE])).delete(synchronize_session=False)
        db.session.commit()


def _c(code):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role='user', vertical='', sv=0)
    return c


def _preview(client, key, ids, action, value=None):
    return client.post(f'/api/data-quality/{key}/preview',
                       json={'ids': ids, 'action': action, 'value': value})


def _apply(client, key, ids, action, value, token, reason='Tidy-up agreed'):
    return client.post(f'/api/data-quality/{key}/apply',
                       json={'ids': ids, 'action': action, 'value': value,
                             'reason': reason, 'preview_token': token})


def _followups(world, *keys):
    with flask_app.app_context():
        return [db.session.get(Lead, world[k]).followup_date for k in keys]


# ── the screens ──────────────────────────────────────────────────────
def test_the_screens_need_the_permission(world):
    assert _c(BOSS).get('/admin/data-quality').status_code == 200
    assert _c(ME).get('/admin/data-quality').status_code == 200
    assert _c(NOPE).get('/admin/data-quality').status_code == 403
    assert _c(NOPE).get('/api/data-quality').status_code == 403
    assert _c(NOPE).get(
        '/admin/data-quality/leads_no_followup/export.csv').status_code == 403
    r = _preview(_c(NOPE), 'leads_no_followup', [world['l_mine']],
                 'set_followup', SOON)
    assert r.status_code == 403
    anon = flask_app.test_client()
    assert anon.get('/api/data-quality').status_code == 401
    assert anon.post('/api/data-quality/leads_no_followup/preview',
                     json={}).status_code == 401


def test_the_api_keeps_its_fields_and_adds_the_new_ones(world):
    body = _c(BOSS).get('/api/data-quality').get_json()
    assert body['ok'] is True
    row = [c for c in body['checks'] if c['key'] == 'unowned_leads'][0]
    for field in ('key', 'label', 'why', 'severity', 'route', 'count'):
        assert field in row, field
    for field in ('title', 'cost', 'suggestion', 'batch_fix', 'actions',
                  'trend'):
        assert field in row, field
    assert row['severity'] in defs.SEVERITIES
    detail = _c(BOSS).get('/api/data-quality/leads_no_followup').get_json()
    for field in ('key', 'label', 'why', 'severity', 'count', 'grouped',
                  'records', 'truncated', 'page', 'pages'):
        assert field in detail, field


def test_the_dashboard_shows_severity_suggestion_and_batch(world):
    html = _c(BOSS).get('/admin/data-quality').get_data(as_text=True)
    assert 'batch fix available' in html
    assert defs.BY_KEY['leads_no_followup']['suggestion'] in html
    page = _c(BOSS).get('/admin/data-quality/leads_no_followup') \
        .get_data(as_text=True)
    assert 'id="dqBatch"' in page and 'export.csv' in page
    assert "headers.get('content-type')" in page
    assert "'X-CSRFToken'" in page


def test_own_scope_sees_only_its_own_rows_and_counts(world):
    me = _c(ME)
    detail = me.get('/api/data-quality/leads_no_followup?per_page=500') \
        .get_json()
    ids = {r['id'] for r in detail['records']}
    assert world['l_mine'] in ids and world['l_other'] not in ids
    with flask_app.app_context():
        own_open = Lead.query.filter(
            Lead.assigned_to == ME, Lead.is_archived.isnot(True),
            (Lead.followup_date.is_(None)) |
            (Lead.followup_date < date.today())).count()
    counts = {c['key']: c['count']
              for c in me.get('/api/data-quality').get_json()['checks']}
    assert counts['leads_no_followup'] == own_open
    assert counts['leads_of_leavers'] == 0, \
        'a leaver\'s lead is outside Own scope, so it is not Own\'s count'

    page = me.get('/admin/data-quality/leads_no_followup') \
        .get_data(as_text=True)
    assert 'Dqb Secret Other' not in page
    csv_text = me.get('/admin/data-quality/leads_no_followup/export.csv') \
        .get_data(as_text=True)
    assert 'Dqb Mine One' in csv_text
    assert 'Dqb Secret Other' not in csv_text
    whole = _c(BOSS).get('/admin/data-quality/leads_no_followup/export.csv') \
        .get_data(as_text=True)
    assert 'Dqb Secret Other' in whole


def test_trends_are_withheld_from_a_narrower_scope(world):
    with flask_app.app_context():
        for days in (2, 1):
            db.session.add(DataQualitySnapshot(
                snapshot_date=date.today() - timedelta(days=days),
                check_key='leads_no_followup', scope='company',
                count=40 + days))
        db.session.commit()
    try:
        boss = _c(BOSS).get('/api/data-quality').get_json()['checks']
        me = _c(ME).get('/api/data-quality').get_json()['checks']
        trend = [c['trend'] for c in boss if c['key'] == 'leads_no_followup']
        assert trend and len(trend[0]) >= 2
        assert all(c['trend'] == [] for c in me)
        assert '<polyline' in _c(BOSS).get('/admin/data-quality') \
            .get_data(as_text=True)
        assert '<polyline' not in _c(ME).get('/admin/data-quality') \
            .get_data(as_text=True)
    finally:
        with flask_app.app_context():
            DataQualitySnapshot.query.filter(
                DataQualitySnapshot.count.in_((41, 42)),
                DataQualitySnapshot.check_key == 'leads_no_followup').delete(
                synchronize_session=False)
            db.session.commit()


def test_csv_cells_cannot_run_as_formulas(world):
    text = _c(ME).get('/admin/data-quality/leads_no_followup/export.csv') \
        .get_data(as_text=True)
    cells = [cell for row in csv.reader(io.StringIO(text)) for cell in row]
    assert "'=HYPERLINK(\"http://evil.example\",\"x\")" in cells
    assert not any(c.startswith('=') for c in cells)


def test_an_unknown_check_is_404_everywhere(world):
    boss = _c(BOSS)
    assert boss.get('/admin/data-quality/nope').status_code == 404
    assert boss.get('/api/data-quality/nope').status_code == 404
    assert boss.get('/admin/data-quality/nope/export.csv').status_code == 404
    assert _preview(boss, 'nope', [1], 'archive').status_code == 404


# ── batch correction ─────────────────────────────────────────────────
def test_preview_changes_nothing_and_shows_before_and_after(world):
    r = _preview(_c(ME), 'leads_no_followup',
                 [world['l_mine'], world['l_mine2']], 'set_followup', SOON)
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d['count'] == 2 and d['preview_token']
    by_id = {c['id']: c for c in d['changes']}
    assert by_id[world['l_mine']]['before'] is None
    assert by_id[world['l_mine2']]['before'] == PAST.isoformat()
    assert {c['after'] for c in d['changes']} == {SOON}
    assert _followups(world, 'l_mine', 'l_mine2') == [None, PAST]


def test_apply_writes_what_was_previewed_and_audits_it(world):
    me = _c(ME)
    ids = [world['l_mine'], world['l_mine2']]
    token = _preview(me, 'leads_no_followup', ids, 'set_followup',
                     SOON).get_json()['preview_token']
    r = _apply(me, 'leads_no_followup', ids, 'set_followup', SOON, token,
               reason='Weekly pipeline review')
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['applied'] == 2
    assert _followups(world, 'l_mine', 'l_mine2') == [
        date.fromisoformat(SOON)] * 2
    with flask_app.app_context():
        ev = AuditEvent.query.filter_by(
            action='data_quality.batch_fix',
            entity_id=r.get_json()['batch_ref']).one()
        assert ev.actor == ME and ev.reason == 'Weekly pipeline review'
        assert ev.new_value['count'] == 2
        assert ev.new_value['action'] == 'set_followup'
        assert sorted(ev.new_value['ids']) == sorted(ids)
        per_record = AuditEvent.query.filter(
            AuditEvent.action == 'lead.update',
            AuditEvent.entity_id == str(world['l_mine2']),
            AuditEvent.reason == 'Weekly pipeline review').one()
        assert per_record.old_value == {'followup_date': PAST.isoformat()}

    # The same token again: the records no longer have the problem.
    again = _apply(me, 'leads_no_followup', ids, 'set_followup', SOON, token)
    assert again.status_code == 409


def test_a_record_outside_scope_refuses_the_whole_batch(world):
    me = _c(ME)
    r = _preview(me, 'leads_no_followup',
                 [world['l_mine'], world['l_other']], 'set_followup', SOON)
    assert r.status_code == 403
    missing = _preview(me, 'leads_no_followup', [world['l_mine'], 9_900_001],
                       'set_followup', SOON)
    assert missing.status_code == 403
    assert r.get_json()['error'] == missing.get_json()['error'], \
        'outside scope and not existing must read the same'
    # A token for Own's own records cannot be stretched to another's.
    token = _preview(me, 'leads_no_followup', [world['l_mine']],
                     'set_followup', SOON).get_json()['preview_token']
    stretched = _apply(me, 'leads_no_followup',
                       [world['l_mine'], world['l_other']], 'set_followup',
                       SOON, token)
    assert stretched.status_code == 403
    assert _followups(world, 'l_mine', 'l_other') == [None, None]


def test_apply_refuses_anything_other_than_the_preview(world):
    me = _c(ME)
    ids = [world['l_mine']]
    token = _preview(me, 'leads_no_followup', ids, 'set_followup',
                     SOON).get_json()['preview_token']
    for tampered in (
            dict(value=LATER),                                 # other value
            dict(ids=[world['l_mine'], world['l_mine2']]),     # more records
            dict(token=token[:-1] + ('0' if token[-1] != '0' else '1')),
            dict(token=None)):
        args = dict(ids=ids, value=SOON, token=token)
        args.update(tampered)
        r = _apply(me, 'leads_no_followup', args['ids'], 'set_followup',
                   args['value'], args['token'])
        assert r.status_code in (400, 409), (tampered, r.get_json())
    # Another administrator cannot replay it, even with a wider scope.
    r = _apply(_c(BOSS), 'leads_no_followup', ids, 'set_followup', SOON,
               token)
    assert r.status_code == 409
    assert _followups(world, 'l_mine', 'l_mine2') == [None, PAST]


def test_apply_refuses_when_a_record_changed_after_the_preview(world):
    me = _c(ME)
    ids = [world['l_mine2']]
    token = _preview(me, 'leads_no_followup', ids, 'set_followup',
                     SOON).get_json()['preview_token']
    with flask_app.app_context():
        # Someone else moves it — still overdue, so still flagged, but no
        # longer what the administrator looked at.
        db.session.get(Lead, world['l_mine2']).followup_date = \
            PAST - timedelta(days=1)
        db.session.commit()
    r = _apply(me, 'leads_no_followup', ids, 'set_followup', SOON, token)
    assert r.status_code == 409
    assert 'Preview again' in r.get_json()['error']
    assert _followups(world, 'l_mine2') == [PAST - timedelta(days=1)]


def test_an_expired_preview_is_refused(world):
    with flask_app.test_request_context('/'):
        from app.access import scope
        sc = scope.for_employee(ME)
        ids = [world['l_mine']]
        then = 1_000_000
        p = batch.preview('leads_no_followup', ids, 'set_followup', SOON,
                          sc=sc, actor=ME, now=then)
        with pytest.raises(batch.BatchRefused) as e:
            batch.apply('leads_no_followup', ids, 'set_followup', SOON,
                        'Late apply', p['preview_token'], sc=sc, actor=ME,
                        now=then + defs.PREVIEW_TTL_SECONDS + 1)
        assert e.value.status == 409 and 'expired' in e.value.message
        # Just inside the window it would have gone through.
        out = batch.apply('leads_no_followup', ids, 'set_followup', SOON,
                          'Prompt apply', p['preview_token'], sc=sc,
                          actor=ME, now=then + defs.PREVIEW_TTL_SECONDS)
        assert out['applied'] == 1


def test_a_reason_is_required(world):
    me = _c(ME)
    ids = [world['l_mine']]
    token = _preview(me, 'leads_no_followup', ids, 'set_followup',
                     SOON).get_json()['preview_token']
    r = _apply(me, 'leads_no_followup', ids, 'set_followup', SOON, token,
               reason='  ')
    assert r.status_code == 400
    assert _followups(world, 'l_mine') == [None]


def test_batch_limits_and_bad_requests(world):
    boss = _c(BOSS)
    r = _preview(boss, 'leads_no_followup',
                 list(range(1, defs.BATCH_MAX + 2)), 'set_followup', SOON)
    assert r.status_code == 413
    assert _preview(boss, 'leads_no_followup', [world['l_mine']],
                    'archive').status_code == 400, 'not offered here'
    assert _preview(boss, 'leads_no_followup', [world['l_mine']],
                    'set_followup',
                    PAST.isoformat()).status_code == 400, 'a past date'
    assert _preview(boss, 'leads_no_followup', [world['l_mine']],
                    'set_followup', 'soon').status_code == 400
    assert _preview(boss, 'leads_no_followup', [], 'set_followup',
                    SOON).status_code == 400
    assert _preview(boss, 'leads_no_followup', ['1; DROP'], 'set_followup',
                    SOON).status_code == 400
    assert boss.post('/api/data-quality/leads_no_followup/preview',
                     data='ids=1').status_code == 400
    assert _preview(boss, 'leads_of_leavers', [world['l_leaver']],
                    'assign_owner', GONE).status_code == 400, \
        'an inactive employee cannot be the new owner'
    # A record that does not have the problem cannot be "fixed" by it.
    r = _preview(boss, 'leads_of_leavers', [world['l_mine']],
                 'assign_owner', BOSS)
    assert r.status_code == 409


def test_assigning_an_owner_uses_the_bulk_service(world):
    boss = _c(BOSS)
    ids = [world['l_leaver']]
    d = _preview(boss, 'leads_of_leavers', ids, 'assign_owner',
                 ME).get_json()
    assert d['changes'][0]['before'] == GONE
    assert d['changes'][0]['after'] == ME
    r = _apply(boss, 'leads_of_leavers', ids, 'assign_owner', ME,
               d['preview_token'], reason='Owner left the company')
    assert r.status_code == 200, r.get_json()
    with flask_app.app_context():
        lead = db.session.get(Lead, world['l_leaver'])
        assert lead.assigned_to == ME and lead.assigned_name
        assert LeadAssignmentHistory.query.filter_by(
            lead_id=lead.id, from_primary=GONE, to_primary=ME).count() == 1
        # Newest first: SQLite reuses ids, so an older run's event can
        # carry the same entity id.
        ev = AuditEvent.query.filter_by(action='lead.ownership_change',
                                        entity_id=str(lead.id)) \
            .order_by(AuditEvent.id.desc()).first()
        assert ev is not None and ev.reason == 'Owner left the company'


def test_archive_is_reversible_and_nothing_is_deleted(world):
    with flask_app.app_context():
        lead = db.session.get(Lead, world['l_leaver'])
        lead.source, lead.classification = 'email', 'I_non_business'
        db.session.commit()
    boss = _c(BOSS)
    ids = [world['l_leaver']]
    d = _preview(boss, 'email_leads_non_lead', ids, 'archive').get_json()
    assert d['changes'][0]['after'] is True
    r = _apply(boss, 'email_leads_non_lead', ids, 'archive', None,
               d['preview_token'], reason='Newsletter, not an enquiry')
    assert r.status_code == 200, r.get_json()
    with flask_app.app_context():
        lead = db.session.get(Lead, world['l_leaver'])
        assert lead is not None and lead.is_archived
        assert lead.archive_reason == 'Newsletter, not an enquiry'
        assert DeletionAudit.query.filter_by(entity_id=lead.id,
                                             action='archive').count() == 1


def test_link_account_only_where_one_account_matches(world):
    boss = _c(BOSS)
    ids = [world['l_unlinked'], world['l_twice']]
    d = _preview(boss, 'unlinked_leads', ids, 'link_account').get_json()
    assert [c['id'] for c in d['changes']] == [world['l_unlinked']]
    assert d['changes'][0]['after'] == world['exact']
    assert [s['id'] for s in d['skipped']] == [world['l_twice']]
    assert 'more than one' in d['skipped'][0]['reason']
    r = _apply(boss, 'unlinked_leads', ids, 'link_account', None,
               d['preview_token'], reason='Exact name match')
    assert r.status_code == 200 and r.get_json()['applied'] == 1
    with flask_app.app_context():
        assert db.session.get(Lead, world['l_unlinked']).company_id == \
            world['exact']
        assert db.session.get(Lead, world['l_twice']).company_id is None


def test_opportunity_and_account_corrections(world):
    me = _c(ME)
    ids = [world['o_mine']]
    d = _preview(me, 'opps_no_close_date', ids, 'set_close_date',
                 SOON).get_json()
    assert _preview(me, 'opps_no_close_date', [world['o_other']],
                    'set_close_date', SOON).status_code == 403
    r = _apply(me, 'opps_no_close_date', ids, 'set_close_date', SOON,
               d['preview_token'], reason='Customer confirmed timing')
    assert r.status_code == 200, r.get_json()

    boss = _c(BOSS)
    d = _preview(boss, 'no_pic', [world['nopic']], 'assign_owner',
                 ME).get_json()
    r = _apply(boss, 'no_pic', [world['nopic']], 'assign_owner', ME,
               d['preview_token'], reason='Account review')
    assert r.status_code == 200, r.get_json()
    with flask_app.app_context():
        assert db.session.get(Opportunity, world['o_mine']) \
            .expected_close_date == date.fromisoformat(SOON)
        assert db.session.get(Company, world['nopic']).pic_emp_code == ME
        ev = AuditEvent.query.filter_by(action='opportunity.update',
                                        entity_id=str(world['o_mine'])) \
            .order_by(AuditEvent.id.desc()).first()
        assert ev is not None and ev.reason == 'Customer confirmed timing'


def test_writes_need_the_csrf_token(world):
    cfg = flask_app.config
    before = (cfg.get('WTF_CSRF_ENABLED'), cfg.get('WTF_CSRF_SSL_STRICT'))
    cfg['WTF_CSRF_ENABLED'], cfg['WTF_CSRF_SSL_STRICT'] = True, False
    try:
        r = _preview(_c(BOSS), 'leads_no_followup', [world['l_mine']],
                     'set_followup', SOON)
        assert r.status_code == 400 and r.get_json()['code'] == 'csrf'
    finally:
        cfg['WTF_CSRF_ENABLED'], cfg['WTF_CSRF_SSL_STRICT'] = before
