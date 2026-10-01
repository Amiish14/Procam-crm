"""The Daily Workbench: what it puts in front of you, who may see it,
and what a bulk update is allowed to do.

The board is built from app/services/sales_rules.py, so these tests are
also the record of what "overdue", "stale", "due today" and the quote
ageing buckets mean.
"""
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'workbench-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'WorkbenchTest12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'workbench.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Lead, Employee, Company = _main.Lead, _main.Employee, _main.Company
LeadActivity = _main.LeadActivity
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.access import scope as sc_mod                    # noqa: E402
from app.models.access import AccessProfile, DataScope    # noqa: E402
from app.services import sales_rules as rules            # noqa: E402
from app.workbench import service as wb                   # noqa: E402

REP, OTHER, HEAD = 'WBREP', 'WBOTHER', 'WBHEAD'
TODAY = rules.business_today()


def _emp(code, name, vertical='Project Freight', head=False, scope=None,
         perms=()):
    e = Employee.query.filter_by(emp_code=code).first() or Employee(emp_code=code)
    e.name, e.vertical, e.is_active, e.must_change_pw = name, vertical, True, False
    e.role, e.is_super_admin, e.is_vertical_head = 'user', False, head
    e.session_version = 0
    db.session.add(e)
    db.session.flush()
    AccessProfile.query.filter_by(emp_code=code).delete()
    if scope:
        db.session.add(AccessProfile(emp_code=code, data_scope=scope,
                                     perms=list(perms)))
    return e


def _lead(company, owner, **kw):
    l = Lead(company=company, assigned_to=owner, assigned_name=owner,
             stage=kw.pop('stage', 'Quoted'),
             procam_vertical=kw.pop('vertical', 'Project Freight'),
             created_at=kw.pop('created_at', datetime.utcnow() - timedelta(days=30)))
    for k, v in kw.items():
        setattr(l, k, v)
    db.session.add(l)
    return l


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        # Only ever clear this module's own rows: the suite shares one
        # database, and deleting everything wipes other modules' worlds.
        from app.models.notification import Notification
        mine = [l.id for l in Lead.query.filter(Lead.company.like('WB %'))]
        if mine:
            LeadActivity.query.filter(LeadActivity.lead_id.in_(mine)).delete(
                synchronize_session=False)
            Lead.query.filter(Lead.id.in_(mine)).delete(synchronize_session=False)
        Notification.query.filter(Notification.user_id.in_(
            [REP, OTHER, HEAD])).delete(synchronize_session=False)
        Company.query.filter(Company.name.like('WB %')).delete(
            synchronize_session=False)
        _emp(REP, 'Workbench Rep', scope=DataScope.OWN)
        _emp(OTHER, 'Another Rep', scope=DataScope.OWN)
        _emp(HEAD, 'Vertical Head', head=True, scope=DataScope.VERTICAL)
        db.session.flush()

        rows = {
            'overdue': _lead('WB Overdue Ltd', REP,
                             followup_date=TODAY - timedelta(days=3)),
            'today': _lead('WB Today Ltd', REP, followup_date=TODAY),
            'future': _lead('WB Future Ltd', REP,
                            followup_date=TODAY + timedelta(days=9)),
            'stale': _lead('WB Stale Ltd', REP, stage='Business Discussion',
                           followup_date=TODAY + timedelta(days=9)),
            'lapsed': _lead('WB Lapsed Quote Ltd', REP,
                            followup_date=TODAY + timedelta(days=9),
                            quoted_amount_inr=Decimal('500000'),
                            quote_date=TODAY - timedelta(days=40),
                            quote_validity_date=TODAY - timedelta(days=5)),
            'big': _lead('WB Big Ticket Ltd', REP,
                         followup_date=TODAY + timedelta(days=9),
                         estimated_value_inr=Decimal('25000000')),
            'theirs': _lead('WB Someone Elses Ltd', OTHER,
                            followup_date=TODAY - timedelta(days=4)),
            'won': _lead('WB Closed Ltd', REP, stage='Won',
                         followup_date=TODAY - timedelta(days=4)),
        }
        db.session.flush()
        # "Contacted today" for everything except the stale one, so the
        # idle rules are exercised rather than blanket-applied.
        for key, l in rows.items():
            if key == 'stale':
                continue
            db.session.add(LeadActivity(lead_id=l.id, kind='call',
                                        subject='spoke',
                                        occurred_at=datetime.utcnow(),
                                        performed_by=l.assigned_to))
        db.session.commit()
        return {k: v.id for k, v in rows.items()}


def _client(code):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role='user', vertical='Project Freight',
                 sv=0)
    return c


def _board(code, **params):
    with flask_app.app_context():
        return wb.board(sc_mod.for_employee(code), **params)


def _by_id(board, lead_id):
    return next((i for i in board['items']
                 if i['kind'] == 'lead' and i['id'] == lead_id), None)


# ── what lands on the board ──────────────────────────────────────────
def test_the_board_shows_only_what_the_viewer_may_see(world):
    board = _board(REP, per_page=200)
    assert _by_id(board, world['theirs']) is None
    assert _by_id(board, world['overdue']) is not None
    # a closed lead is not work
    assert _by_id(board, world['won']) is None


def test_overdue_due_today_and_future_are_told_apart(world):
    board = _board(REP, per_page=200)
    overdue = _by_id(board, world['overdue'])
    today = _by_id(board, world['today'])
    assert 'followup_overdue' in overdue['conditions']
    assert overdue['priority'] == rules.P_OVERDUE
    assert overdue['group'] == rules.G_TODAY
    assert 'followup_today' in today['conditions']
    assert today['priority'] == rules.P_TODAY
    assert board['summary']['overdue'] >= 1
    assert board['summary']['due_today'] >= 1


def test_a_lead_with_no_contact_for_a_month_is_stale_not_merely_idle(world):
    item = _by_id(_board(REP, per_page=200), world['stale'])
    assert 'stale' in item['conditions'] and 'idle' not in item['conditions']
    assert item['group'] == rules.G_STALE
    assert item['last_activity_days'] is None


def test_a_quoted_lead_with_no_quote_recorded_is_quotations_work(world):
    item = _by_id(_board(REP, per_page=200), world['today'])
    assert 'quote_missing_detail' in item['conditions']


def test_a_lapsed_quote_is_overdue_work_in_the_quotations_group(world):
    item = _by_id(_board(REP, per_page=200), world['lapsed'])
    assert 'quote_lapsed' in item['conditions']
    assert item['group'] == rules.G_QUOTES
    assert item['priority'] == rules.P_OVERDUE


def test_a_big_deal_is_a_high_value_priority(world):
    item = _by_id(_board(REP, per_page=200), world['big'])
    assert 'high_value' in item['conditions']
    assert _board(REP, per_page=200)['summary']['high_value'] >= 1


def test_every_item_says_why_it_is_there(world):
    for item in _board(REP, per_page=200)['items']:
        assert item['conditions'] and item['reasons']
        assert all(r for r in item['reasons'])


def test_groups_filter_and_paginate(world):
    quotes = _board(REP, group=rules.G_QUOTES, per_page=200)
    assert all(i['group'] == rules.G_QUOTES for i in quotes['items'])
    page = _board(REP, per_page=2)
    assert len(page['items']) == 2 and page['pages'] >= 2
    assert page['total'] == _board(REP, per_page=200)['total']


def test_a_vertical_head_sees_the_team_and_a_rep_does_not(world):
    head = _board(HEAD, per_page=200)
    assert _by_id(head, world['theirs']) is not None
    assert _by_id(head, world['overdue']) is not None


# ── the API ──────────────────────────────────────────────────────────
def test_the_api_needs_a_session_and_honours_scope(world):
    assert flask_app.test_client().get('/api/workbench').status_code == 401
    d = _client(REP).get('/api/workbench?per_page=200').get_json()
    assert d['ok'] and d['viewer'] == REP
    assert all(i['assigned_to'] in ('', REP) for i in d['items']
               if i['kind'] == 'lead')


def test_asking_for_someone_outside_your_scope_is_refused(world):
    r = _client(REP).get(f'/api/workbench?for={OTHER}')
    assert r.status_code == 403
    r = _client(HEAD).get(f'/api/workbench?for={OTHER}&per_page=200')
    assert r.status_code == 200
    assert {i['assigned_to'] for i in r.get_json()['items']
            if i['kind'] == 'lead'} <= {OTHER, ''}


def test_quote_ageing_buckets_are_today_1_3_4_5_6_10_and_over_10():
    assert [k for k, _l, _lo, _hi in rules.AGEING_BUCKETS] == [
        'today', 'd1_3', 'd4_5', 'd6_10', 'd10_plus']
    assert [rules.bucket_for(d) for d in (0, 1, 3, 4, 5, 6, 10, 11, 400)] == [
        'today', 'd1_3', 'd1_3', 'd4_5', 'd4_5', 'd6_10', 'd6_10',
        'd10_plus', 'd10_plus']


def test_quote_ageing_endpoint_answers_for_the_viewer(world):
    d = _client(REP).get('/api/workbench/quote-ageing').get_json()
    assert d['ok'] and 'buckets' in d and len(d['buckets']) == 5


# ── bulk ─────────────────────────────────────────────────────────────
def _preview(client, ids, action, value):
    return client.post('/api/workbench/bulk/preview',
                       data=json.dumps({'ids': ids, 'action': action,
                                        'value': value}),
                       content_type='application/json')


def _apply(client, ids, action, value, reason, token):
    return client.post('/api/workbench/bulk/apply',
                       data=json.dumps({'ids': ids, 'action': action,
                                        'value': value, 'reason': reason,
                                        'preview_token': token}),
                       content_type='application/json')


def test_bulk_previews_then_applies_and_audits(world):
    c = _client(REP)
    ids = [world['overdue'], world['today']]
    when = str(TODAY + timedelta(days=7))
    p = _preview(c, ids, 'set_followup', when).get_json()
    assert p['ok'] and p['count'] == 2 and p['preview_token']
    assert {ch['after'] for ch in p['changes']} == {when}

    with flask_app.app_context():
        before = db.session.get(Lead, ids[0]).followup_date
    a = _apply(c, ids, 'set_followup', when, 'Agreed at the review',
               p['preview_token']).get_json()
    assert a['ok'] and a['applied_count'] == 2 and a['failed_count'] == 0
    with flask_app.app_context():
        assert db.session.get(Lead, ids[0]).followup_date == TODAY + timedelta(days=7)
        assert before != db.session.get(Lead, ids[0]).followup_date
        from app.models.audit import AuditEvent
        ev = (AuditEvent.query.filter_by(action='workbench.bulk')
              .order_by(AuditEvent.id.desc()).first())
        assert ev and ev.reason == 'Agreed at the review'
        assert ev.new_value['count'] == 2


def test_bulk_refuses_records_outside_the_viewers_scope(world):
    c = _client(REP)
    r = _preview(c, [world['overdue'], world['theirs']], 'set_followup',
                 str(TODAY + timedelta(days=3)))
    assert r.status_code == 403
    assert r.get_json()['refused'] == [world['theirs']]
    with flask_app.app_context():
        assert db.session.get(Lead, world['theirs']).followup_date == \
            TODAY - timedelta(days=4)


def test_bulk_needs_a_reason_and_a_matching_preview(world):
    c = _client(REP)
    ids = [world['future']]
    when = str(TODAY + timedelta(days=5))
    p = _preview(c, ids, 'set_followup', when).get_json()
    assert _apply(c, ids, 'set_followup', when, 'x', p['preview_token']).status_code == 400
    # a different value than was previewed cannot ride the same token
    other = str(TODAY + timedelta(days=6))
    r = _apply(c, ids, 'set_followup', other, 'Moved the date',
               p['preview_token'])
    assert r.status_code == 409
    assert _apply(c, ids, 'set_followup', when, 'Moved the date',
                  p['preview_token']).get_json()['applied_count'] == 1


def test_bulk_reports_each_record_and_keeps_the_rest(world, monkeypatch):
    c = _client(REP)
    ids = [world['overdue'], world['today']]
    when = str(TODAY + timedelta(days=4))
    p = _preview(c, ids, 'set_followup', when).get_json()
    from app.workbench import bulk as wb_bulk
    real_get = db.session.get

    def explode(model, pk, *a, **kw):
        row = real_get(model, pk, *a, **kw)
        if pk == ids[0] and model is Lead:
            raise RuntimeError('database said no')
        return row
    monkeypatch.setattr(db.session, 'get', explode)
    out = _apply(c, ids, 'set_followup', when, 'Partial failure test',
                 p['preview_token']).get_json()
    monkeypatch.undo()
    assert out['applied_count'] == 1 and out['failed_count'] == 1
    assert out['failed'][0]['id'] == ids[0]
    assert 'database said no' in out['failed'][0]['error']
    with flask_app.app_context():
        assert db.session.get(Lead, ids[1]).followup_date == TODAY + timedelta(days=4)


def test_bulk_skips_what_is_already_right(world):
    c = _client(REP)
    when = str(TODAY + timedelta(days=9))
    p = _preview(c, [world['future']], 'set_followup', when).get_json()
    assert p['count'] == 0 and p['preview_token'] is None
    assert p['skipped'][0]['reason'] == 'already set to this value'


def test_bulk_validates_the_value(world):
    c = _client(REP)
    past = str(TODAY - timedelta(days=1))
    assert _preview(c, [world['future']], 'set_followup', past).status_code == 400
    assert _preview(c, [world['future']], 'set_stage', 'Nowhere').status_code == 400
    assert _preview(c, [world['future']], 'assign_owner', 'NOBODY').status_code == 400
    assert _preview(c, [world['future']], 'teleport', 'x').status_code == 404


def test_bulk_caps_the_batch(world):
    r = _preview(_client(REP), list(range(1, 400)), 'set_followup',
                 str(TODAY + timedelta(days=2)))
    assert r.status_code == 413


# ── reminders ────────────────────────────────────────────────────────
def test_a_manager_can_remind_their_team_and_it_is_logged(world, monkeypatch):
    from email_ingest import notifier
    monkeypatch.setattr(notifier, 'send', lambda *a, **kw: True)
    r = _client(HEAD).post('/api/workbench/remind',
                           data=json.dumps({'to': REP, 'message': 'Please update',
                                            'email': True}),
                           content_type='application/json')
    assert r.status_code == 200 and r.get_json()['notified']
    with flask_app.app_context():
        from app.models.notification import Notification
        from app.models.audit import AuditEvent
        n = (Notification.query.filter_by(user_id=REP)
             .order_by(Notification.id.desc()).first())
        assert n and 'Please update' in n.body and n.action_url.startswith('/my-work')
        assert AuditEvent.query.filter_by(action='workbench.reminder').first()


def test_a_reminder_cannot_reach_outside_your_scope(world):
    r = _client(REP).post('/api/workbench/remind',
                          data=json.dumps({'to': OTHER, 'message': 'hi'}),
                          content_type='application/json')
    assert r.status_code == 403
    with flask_app.app_context():
        from app.models.notification import Notification
        assert Notification.query.filter_by(user_id=OTHER).count() == 0


def test_the_same_reminder_twice_is_sent_once(world):
    c = _client(HEAD)
    body = json.dumps({'to': REP, 'message': 'Same nudge'})
    first = c.post('/api/workbench/remind', data=body,
                   content_type='application/json').get_json()
    second = c.post('/api/workbench/remind', data=body,
                    content_type='application/json').get_json()
    assert first['notified'] and second['suppressed']


# ── the rules are shared, not copied ─────────────────────────────────
def test_the_workbench_reads_the_shared_rules_and_checks():
    src = open(os.path.join(_ROOT, 'app', 'workbench', 'service.py')).read()
    assert 'from app.services import sales_rules' in src
    assert 'from app.data_quality import service as dq' in src
    # no second definition of contact, value or open-ness
    assert 'updated_at <' not in src
    rules_src = open(os.path.join(_ROOT, 'app', 'services',
                                  'sales_rules.py')).read()
    assert 'from app.services import contact' in rules_src
    assert 'lead_value.value_inr' in rules_src


def test_the_page_sends_a_csrf_token_with_every_write():
    """The Workbench is served outside the app shell, which carries its
    own fetch wrapper. Without this the bulk actions answered "your
    session token has expired" for everybody."""
    html = open(os.path.join(_ROOT, 'templates', 'workbench',
                             'home.html')).read()
    assert "'X-CSRFToken': csrfToken()" in html
    body = html[html.index('<script>'):]
    for call in ('bulk/preview', 'bulk/apply', '/remind'):
        at = body.index(call)
        window = body[max(0, at - 400):at + 200]
        assert 'postJson(' in window, call


def test_a_write_without_the_token_is_refused(world):
    """Proof the protection is real, not only present in the markup."""
    app_csrf = flask_app.config.get('WTF_CSRF_ENABLED')
    flask_app.config['WTF_CSRF_ENABLED'] = True
    try:
        c = _client(REP)
        r = c.post('/api/workbench/bulk/apply',
                   data=json.dumps({'ids': [world['future']],
                                    'action': 'set_followup',
                                    'value': str(TODAY + timedelta(days=3)),
                                    'reason': 'no token here',
                                    'preview_token': 'x.y'}),
                   content_type='application/json')
        assert r.status_code in (400, 403)
    finally:
        flask_app.config['WTF_CSRF_ENABLED'] = app_csrf
