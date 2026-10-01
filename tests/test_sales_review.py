"""The sales review: what each figure means, who may see whose, and why
an action agreed at one meeting is still on the agenda at the next.

These tests are also the record of the review's definitions — and of the
fact that it has none of its own: "open", "stale", "high value" and the
quote deadline all come from app/services/sales_rules.py.
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
os.environ.setdefault('SECRET_KEY', 'sales-review-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'SalesReviewTest12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'review.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Lead, Employee, Company = _main.Lead, _main.Employee, _main.Company
LeadActivity = _main.LeadActivity
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.access import scope as sc_mod                    # noqa: E402
from app.models.access import AccessProfile, DataScope    # noqa: E402
from app.models.review import ReviewAction                # noqa: E402
from app.review import service as rv                      # noqa: E402
from app.services import sales_rules as rules             # noqa: E402

# The blueprints are registered by app.py in production; this module
# registers them itself so the routes can be exercised before that edit
# lands (registering twice is a no-op).
for _mod_path in ('app.review.routes', 'app.management.routes'):
    _bp = importlib.import_module(_mod_path).bp
    if _bp.name not in flask_app.blueprints:
        flask_app.register_blueprint(_bp)

REP, OTHER, HEAD = 'RVREP', 'RVOTHER', 'RVHEAD'
VERTICAL = 'Project Freight'
TODAY = rules.business_today()
START = TODAY - timedelta(days=20)
END = TODAY


def _emp(code, name, vertical=VERTICAL, head=False, scope=None, perms=()):
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
             procam_vertical=kw.pop('vertical', VERTICAL),
             created_at=kw.pop('created_at',
                               datetime.utcnow() - timedelta(days=10)))
    for k, v in kw.items():
        setattr(l, k, v)
    db.session.add(l)
    return l


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        # Only this module's rows: the suite shares one database.
        mine = [l.id for l in Lead.query.filter(Lead.company.like('RV %'))]
        if mine:
            LeadActivity.query.filter(LeadActivity.lead_id.in_(mine)).delete(
                synchronize_session=False)
            Lead.query.filter(Lead.id.in_(mine)).delete(
                synchronize_session=False)
        ReviewAction.query.filter(ReviewAction.review_scope.in_(
            [f'emp:{REP}', f'emp:{OTHER}', f'emp:{HEAD}',
             f'vertical:{VERTICAL}'])).delete(synchronize_session=False)
        Company.query.filter(Company.name.like('RV %')).delete(
            synchronize_session=False)
        _emp(REP, 'Review Rep', scope=DataScope.OWN)
        _emp(OTHER, 'Another Rep', scope=DataScope.OWN)
        _emp(HEAD, 'Vertical Head', head=True, scope=DataScope.VERTICAL)
        db.session.flush()

        quoted_on = TODAY - timedelta(days=5)
        rows = {
            # quoted five days ago, three days after the RFQ arrived
            'quoted': _lead('RV Quoted Ltd', REP, stage='Quoted',
                            followup_date=TODAY,
                            rfq_date=quoted_on - timedelta(days=3),
                            quote_date=quoted_on,
                            quoted_amount_inr=Decimal('800000')),
            # open, but nobody has said what happens next
            'no_action': _lead('RV No Next Action Ltd', REP,
                               stage='Business Discussion',
                               estimated_value_inr=Decimal('300000')),
            # nothing said to the customer since it was created
            'stale': _lead('RV Gone Cold Ltd', REP,
                           stage='Business Discussion',
                           followup_date=TODAY + timedelta(days=5),
                           created_at=datetime.utcnow() - timedelta(days=60),
                           estimated_value_inr=Decimal('400000')),
            # a crore-plus deal nobody has touched for two months
            'big_quiet': _lead('RV Big And Quiet Ltd', REP,
                               stage='Under Negotiation',
                               created_at=datetime.utcnow() - timedelta(days=60),
                               followup_date=TODAY + timedelta(days=12),
                               opp_close_date=TODAY + timedelta(days=10),
                               estimated_value_inr=Decimal('25000000')),
            'won': _lead('RV Closed Won Ltd', REP, stage='Won',
                         estimated_value_inr=Decimal('2000000'),
                         stage_entered_at=datetime.utcnow() - timedelta(days=3)),
            'lost': _lead('RV Closed Lost Ltd', REP, stage='Lost',
                          estimated_value_inr=Decimal('900000'),
                          lost_reason='Price too high',
                          stage_entered_at=datetime.utcnow() - timedelta(days=2)),
            'theirs': _lead('RV Someone Elses Ltd', OTHER,
                            stage='Business Discussion',
                            followup_date=TODAY - timedelta(days=4)),
        }
        db.session.flush()
        # Contact today on everything except the two deliberately quiet
        # ones, so the idle rules are exercised rather than blanket-applied.
        for key, l in rows.items():
            if key in ('stale', 'big_quiet'):
                continue
            db.session.add(LeadActivity(
                lead_id=l.id, kind='call', subject='spoke',
                occurred_at=datetime.utcnow(), performed_by=l.assigned_to))
        # one customer visit this period, so "visits" is not zero by
        # accident
        db.session.add(LeadActivity(
            lead_id=rows['quoted'].id, kind='visit', subject='site visit',
            occurred_at=datetime.utcnow() - timedelta(days=2),
            performed_by=REP))

        quiet = Company(name='RV Quiet Account Ltd', pic_emp_code=REP,
                        vertical=VERTICAL, is_active=True,
                        last_activity_at=datetime.utcnow() - timedelta(days=200))
        fresh = Company(name='RV Busy Account Ltd', pic_emp_code=REP,
                        vertical=VERTICAL, is_active=True,
                        last_activity_at=datetime.utcnow() - timedelta(days=1))
        db.session.add_all([quiet, fresh])
        db.session.commit()
        out = {k: v.id for k, v in rows.items()}
        out['quiet_account'] = quiet.id
        out['busy_account'] = fresh.id
        return out


def _client(code):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role='user', vertical=VERTICAL,
                 sv=0)
    return c


def _review(code=REP, viewer=None, **kw):
    with flask_app.app_context():
        return rv.individual(code, str(START), str(END),
                             sc=sc_mod.for_employee(viewer or code), **kw)


def _ids(rows):
    return {r['id'] for r in rows}


# ── the figures ──────────────────────────────────────────────────────
def test_the_funnel_counts_what_is_open_what_was_won_and_what_was_lost(world):
    r = _review()['funnel']
    assert r['open_count'] == 4            # quoted, no_action, stale, big_quiet
    assert _ids(r['won']) == {world['won']}
    assert r['won_value_inr'] == 2000000.0
    assert _ids(r['lost']) == {world['lost']}
    assert r['conversion_pct'] == 50.0     # one won, one lost
    assert world['theirs'] not in _ids(r['open_rows'])


def test_open_value_is_the_quote_where_there_is_one(world):
    """app/services/lead_value.py decides, not the review."""
    r = _review()['funnel']
    quoted = next(x for x in r['open_rows'] if x['id'] == world['quoted'])
    assert quoted['value_inr'] == 800000.0
    assert r['open_value_inr'] == 800000.0 + 300000 + 400000 + 25000000


def test_commercial_counts_the_quote_and_how_long_the_customer_waited(world):
    c = _review()['commercial']
    assert c['quotes_sent'] == 1
    assert c['quote_value_inr'] == 800000.0
    assert c['quote_turnaround_days'] == 3.0
    assert c['quote_sla_days'] == rules.QUOTE_SLA_DAYS


def test_activity_counts_visits_and_touches(world):
    a = _review()['activity']
    assert a['visits'] == 1
    # four calls by this person; the fifth was logged by a colleague on
    # their own lead and belongs to their review, not this one
    assert a['by_kind'].get('call') == 4
    assert a['leads_touched'] == 4


def test_discipline_uses_the_shared_idle_and_stale_thresholds(world):
    d = _review()['discipline']
    assert d['stale_days'] == rules.STALE_DAYS
    assert d['idle_days'] == rules.IDLE_DAYS
    assert _ids(d['stale_rows']) == {world['stale'], world['big_quiet']}
    assert _ids(d['no_next_action_rows']) == {world['no_action']}
    assert d['open_leads'] == 4 and d['with_next_action'] == 3
    assert d['followup_compliance_pct'] == 75.0


def test_accounts_separate_the_quiet_from_the_active(world):
    a = _review()['accounts']
    assert a['quiet_days_threshold'] == rules.ACCOUNT_QUIET_DAYS
    assert {r['id'] for r in a['quiet']} == {world['quiet_account']}
    assert {r['id'] for r in a['active']} == {world['busy_account']}


def test_every_figure_can_be_opened(world):
    r = _review()
    for row in r['funnel']['open_rows'] + r['top_opportunities']:
        assert row['route'].startswith('/app?lead=')
    for row in r['accounts']['quiet']:
        assert row['route'].startswith('/companies/')


def test_trends_compare_this_week_and_this_month_with_the_last(world):
    t = _review()['trends']
    assert set(t) == {'wow', 'mom'}
    assert t['wow']['days'] == rv.WOW_DAYS and t['mom']['days'] == rv.MOM_DAYS
    won = t['mom']['measures']['won']
    assert won['current'] == 1 and won['previous'] == 0
    assert won['direction'] == 'up' and won['change'] == 1


# ── scope ────────────────────────────────────────────────────────────
def test_a_rep_cannot_review_a_colleague(world):
    with flask_app.app_context():
        with pytest.raises(rv.ReviewRefused) as exc:
            rv.individual(OTHER, sc=sc_mod.for_employee(REP))
        assert exc.value.status == 403
    assert _client(REP).get(f'/api/review/individual?emp={OTHER}'
                            ).status_code == 403
    assert _client(REP).get(f'/review/individual?emp={OTHER}'
                            ).status_code == 403


def test_a_refusal_is_not_an_empty_review(world):
    """The difference between "did nothing" and "not yours to see" has to
    survive to the screen."""
    body = _client(REP).get(f'/api/review/individual?emp={OTHER}').get_json()
    assert body['ok'] is False and 'access' in body['error'].lower()


def test_a_head_may_review_the_people_their_scope_reaches(world):
    r = _review(OTHER, viewer=HEAD)
    assert r['subject']['emp_code'] == OTHER
    assert _client(HEAD).get(f'/api/review/individual?emp={OTHER}'
                             ).status_code == 200


def test_a_review_only_ever_shows_the_subjects_own_records(world):
    r = _review(REP, viewer=HEAD)
    assert world['theirs'] not in _ids(r['funnel']['open_rows'])


def test_a_rep_cannot_open_a_vertical_review(world):
    with flask_app.app_context():
        assert rv.may_review_vertical(VERTICAL, sc_mod.for_employee(REP)) \
            is False
        assert rv.may_review_vertical(VERTICAL, sc_mod.for_employee(HEAD)) \
            is True
    assert _client(REP).get(f'/api/review/vertical?vertical={VERTICAL}'
                            ).status_code == 403


def test_the_vertical_review_drills_through_to_people_and_accounts(world):
    with flask_app.app_context():
        v = rv.vertical(VERTICAL, str(START), str(END),
                        sc=sc_mod.for_employee(HEAD))
    assert v['vertical'] == VERTICAL
    codes = {p['emp_code'] for p in v['people']}
    assert REP in codes and OTHER in codes
    for person in v['people']:
        if person['emp_code']:
            assert person['route'] == \
                f"/review/individual?emp={person['emp_code']}"
    assert v['results']['won'] == 1
    assert v['losses'][0]['reason'] == 'Price too high'
    assert v['followup_compliance']['compliance_pct'] is not None
    assert any(a['route'].startswith('/companies/')
               for a in v['accounts']['inactive'])


def test_the_api_needs_a_session(world):
    anon = flask_app.test_client()
    assert anon.get('/api/review/individual').status_code == 401
    assert anon.get('/api/review/meeting').status_code == 401
    assert anon.post('/api/review/actions',
                     data=json.dumps({'description': 'x'}),
                     content_type='application/json').status_code == 401
    assert anon.get('/api/management').status_code == 401


# ── the meeting ──────────────────────────────────────────────────────
def _meeting(code=REP, viewer=None, **kw):
    with flask_app.app_context():
        return rv.meeting(f'emp:{code}', sc=sc_mod.for_employee(viewer or code),
                          **kw)


def test_the_agenda_runs_in_the_fixed_order(world):
    m = _meeting(start=str(START), end=str(END))
    assert [s['key'] for s in m['agenda']] == list(rv.AGENDA_KEYS)
    assert [s['key'] for s in m['agenda']][0] == 'previous_actions'
    assert [s['key'] for s in m['agenda']][-1] == 'actions_agreed'


def test_every_agenda_section_is_present_even_when_empty(world):
    """A section with nothing in it still appears, so the meeting runs
    the same agenda every time."""
    m = _meeting(start=str(START), end=str(END))
    intel = m['sections']['external_intelligence']
    assert intel['title'] == 'External intelligence'
    # The count agrees with the rows shown. Not asserted as zero: the
    # suite shares one database and the intelligence module's own tests
    # leave sightings behind.
    assert intel['count'] == len(intel['rows'])
    for key in ('previous_actions', 'new_leads', 'rfqs', 'quotes_pending',
                'negotiations', 'expected_closures', 'won', 'lost', 'stale',
                'account_development', 'external_intelligence',
                'actions_agreed'):
        assert key in m['sections'], key
        assert m['sections'][key].get('title')


def test_the_agenda_sorts_the_records_into_the_right_sections(world):
    m = _meeting(start=str(START), end=str(END))
    s = m['sections']
    assert _ids(s['won']['rows']) == {world['won']}
    assert _ids(s['lost']['rows']) == {world['lost']}
    assert _ids(s['negotiations']['rows']) == {world['big_quiet']}
    assert _ids(s['stale']['rows']) == {world['stale'], world['big_quiet']}
    assert world['big_quiet'] in _ids(s['expected_closures']['rows'])
    assert world['theirs'] not in _ids(s['new_leads']['rows'])


# ── review actions ───────────────────────────────────────────────────
def _create(client, **body):
    body.setdefault('review_scope', f'emp:{REP}')
    body.setdefault('description', 'Call the customer back')
    return client.post('/api/review/actions', data=json.dumps(body),
                       content_type='application/json')


def test_an_action_is_written_down_against_the_review_scope(world):
    r = _create(_client(HEAD), due_date=str(TODAY + timedelta(days=3)),
                linked_entity_type='lead', linked_entity_id=world['stale'])
    assert r.status_code == 201
    action = r.get_json()['action']
    assert action['review_scope'] == f'emp:{REP}'
    assert action['owner_emp_code'] == REP and action['status'] == 'open'
    assert action['route'] == f"/app?lead={world['stale']}"


def test_an_unresolved_action_is_on_the_next_meetings_agenda(world):
    _create(_client(HEAD), description='Re-quote the cold one')
    # this meeting: the action was agreed here, and is also outstanding
    this_one = _meeting(viewer=HEAD, start=str(START), end=str(END))
    assert this_one['sections']['actions_agreed']['count'] == 1
    assert this_one['sections']['previous_actions']['count'] == 1

    # the next meeting, a period later: nothing was agreed in it, and the
    # action is still there because nobody resolved it
    later_start, later_end = TODAY + timedelta(days=1), TODAY + timedelta(days=8)
    nxt = _meeting(viewer=HEAD, start=str(later_start), end=str(later_end))
    assert nxt['sections']['actions_agreed']['count'] == 0
    rows = nxt['sections']['previous_actions']['rows']
    assert [r['description'] for r in rows] == ['Re-quote the cold one']


def test_closing_an_action_takes_it_off_the_agenda(world):
    c = _client(HEAD)
    action_id = _create(c, description='Send the revised quote'
                        ).get_json()['action']['id']
    assert c.post(f'/api/review/actions/{action_id}/close',
                  data=json.dumps({'outcome': 'done'}),
                  content_type='application/json').status_code == 200
    m = _meeting(viewer=HEAD, start=str(START), end=str(END))
    assert m['sections']['previous_actions']['count'] == 0


def test_carrying_an_action_forward_keeps_the_history(world):
    c = _client(HEAD)
    first = _create(c, description='Chase the purchase order',
                    due_date=str(TODAY)).get_json()['action']['id']
    out = c.post(f'/api/review/actions/{first}/close',
                 data=json.dumps({'outcome': 'carried',
                                  'due_date': str(TODAY + timedelta(days=7))}),
                 content_type='application/json').get_json()
    assert out['action']['status'] == 'carried'
    successor = out['carried_to']
    assert successor['carried_from_id'] == first
    assert successor['due_date'] == str(TODAY + timedelta(days=7))
    # the slipped commitment is one row closed and one row open, not one
    # date that quietly moved
    rows = _meeting(viewer=HEAD, start=str(START),
                    end=str(END))['sections']['previous_actions']['rows']
    assert [r['id'] for r in rows] == [successor['id']]


def test_an_action_cannot_be_given_to_somebody_outside_your_access(world):
    r = _create(_client(REP), review_scope=f'emp:{OTHER}')
    assert r.status_code == 403
    r = _create(_client(REP), owner_emp_code=OTHER)
    assert r.status_code == 403
    with flask_app.app_context():
        assert ReviewAction.query.filter_by(
            review_scope=f'emp:{OTHER}').count() == 0


def test_an_action_cannot_point_at_a_record_you_may_not_open(world):
    r = _create(_client(REP), linked_entity_type='lead',
                linked_entity_id=world['theirs'])
    assert r.status_code == 403


def test_an_action_needs_words(world):
    assert _create(_client(REP), description='  ').status_code == 400


def test_actions_are_listed_for_the_review_scope(world):
    c = _client(HEAD)
    _create(c, description='One thing')
    body = c.get(f'/api/review/actions?scope=emp:{REP}').get_json()
    assert body['ok'] and len(body['actions']) == 1
    assert _client(REP).get(f'/api/review/actions?scope=emp:{OTHER}'
                            ).status_code == 403


# ── the management command view ──────────────────────────────────────
def test_the_command_view_shows_what_is_late_stuck_and_quiet(world):
    with flask_app.app_context():
        cv = rv.command_view(sc=sc_mod.for_employee(REP))
    assert _ids(cv['tiles']['stuck_large']['rows']) == {world['big_quiet']}
    assert _ids(cv['tiles']['actions_today']['rows']) == {world['quoted']}
    assert {r['id'] for r in cv['tiles']['inactive_customers']['rows']} == \
        {world['quiet_account']}
    assert world['big_quiet'] in _ids(cv['tiles']['expected_closures']['rows'])
    verticals = {v['vertical'] for v in cv['ageing_pipeline']}
    assert VERTICAL in verticals
    assert cv['loss_reasons'][0]['reason'] == 'Price too high'


def test_every_command_view_figure_links_to_its_records(world):
    with flask_app.app_context():
        cv = rv.command_view(sc=sc_mod.for_employee(HEAD))
    for section in cv['tiles'].values():
        for row in section['rows']:
            assert row.get('route'), f"{section['key']} row has nowhere to go"
    for v in cv['ageing_pipeline']:
        assert v['route'].startswith('/review/vertical?vertical=')


def test_the_command_view_page_renders_inside_the_viewers_boundary(world):
    """A manager sees their people's work and nobody else's. (A viewer
    with only their own records is refused — see the gate tests below.)"""
    r = _client(HEAD).get('/management')
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    # The head's own desk and their team member's work both appear —
    # that is the point of the view. What must not appear is anything
    # outside the boundary, which the API test below pins down.
    assert 'RV Big And Quiet Ltd' in html
    assert 'RV Someone Elses Ltd' in html


def test_the_pages_render(world):
    c = _client(HEAD)
    assert c.get(f'/review/individual?emp={REP}').status_code == 200
    assert c.get(f'/review/vertical?vertical={VERTICAL}').status_code == 200
    assert c.get(f'/review/meeting?scope=emp:{REP}').status_code == 200


# ── the rules are shared, not copied ─────────────────────────────────
def test_the_review_reads_the_shared_rules_rather_than_its_own():
    src = open(os.path.join(_ROOT, 'app', 'review', 'service.py')).read()
    assert 'from app.services import lead_value, sales_rules as rules' in src
    assert 'from app.access import scope as sc_mod' in src
    assert 'from app.workbench import service as wb' in src
    # no second definition of a threshold the rules already own
    for name in ('IDLE_DAYS', 'STALE_DAYS', 'HIGH_VALUE_INR',
                 'QUOTE_SLA_DAYS', 'ACCOUNT_QUIET_DAYS', 'AGEING_BUCKETS'):
        assert f'{name} =' not in src, f'{name} is redefined in the review'
        assert f'rules.{name}' in src, f'{name} is not read from the rules'
    # and no second definition of what a deal is worth
    assert 'quoted_amount_inr or' not in src
    # every read of a record table goes through the Access Matrix: an
    # unscoped Lead.query or Company.query here is a leak
    assert src.count('Lead.query') == src.count('sc_mod.leads(Lead.query')
    assert src.count('Company.query') == \
        src.count('sc_mod.companies(Company.query')


def test_the_review_asks_the_workbench_what_needs_attention():
    src = open(os.path.join(_ROOT, 'app', 'review', 'service.py')).read()
    assert 'wb.board(' in src


def test_the_management_view_does_not_do_its_own_arithmetic():
    src = open(os.path.join(_ROOT, 'app', 'management', 'routes.py')).read()
    assert 'rv.command_view' in src
    assert 'Lead.query' not in src and 'func.' not in src


def test_the_meeting_page_sends_a_csrf_token_with_every_write():
    """Served outside the app shell, which carries its own fetch
    wrapper — without this every action answers "session expired"."""
    html = open(os.path.join(_ROOT, 'templates', 'review',
                             'meeting.html')).read()
    assert "'X-CSRFToken': csrfToken()" in html
    body = html[html.index('<script>'):]
    for call in ('/api/review/actions', '/close'):
        at = body.index(call)
        assert 'postJson(' in body[max(0, at - 400):at + 200], call


def test_a_write_without_the_token_is_refused(world):
    app_csrf = flask_app.config.get('WTF_CSRF_ENABLED')
    flask_app.config['WTF_CSRF_ENABLED'] = True
    try:
        r = _client(REP).post('/api/review/actions',
                              data=json.dumps({'description': 'no token'}),
                              content_type='application/json')
        assert r.status_code in (400, 403)
    finally:
        flask_app.config['WTF_CSRF_ENABLED'] = app_csrf


# ── the command view is for people who manage other people ───────────
def test_the_command_view_is_refused_to_someone_with_only_their_own_work():
    """A page of your own figures labelled "management" tells you the
    wrong thing about what you are looking at."""
    from app.management import routes as mgmt
    c = _client(REP)
    assert c.get('/management').status_code == 403
    r = c.get('/api/management')
    assert r.status_code == 403 and 'Daily Workbench' in r.get_json()['error']


def test_the_command_view_opens_for_a_vertical_head():
    c = _client(HEAD)
    assert c.get('/management').status_code == 200
    assert c.get('/api/management').get_json()['ok'] is True
