"""
Every RFQ chain screen, rendered the way a pilot user sees it.

RFQ-DEMO-PARITY. `docs/reference/RFQ_Chain_Tracker_Demo.html` was
reviewed and signed off as the way these screens should look, and
the live ones had drifted: the chain board drew grey columns where
the demo stacks the timer colours, the lead record showed numbered
squares where the demo names every box, and two panels — the
leads@ queue and the twelve steps — had no screen at all.

So each screen here is fetched with its flags on, as a pilot user,
against real rows, and checked for the pieces that only the demo's
layout produces. A 200 with the old layout would pass a status
check and fail the review.

Two things these tests deliberately do not do. They do not assert
pixels: the screenshot pairs under `docs/reference/parity/` are
the record of how it looks, taken at 1440px and 390px by
`scripts/parity_shots.py`. And they do not count on a browser —
the phone layout is asserted through the stylesheet's own rules,
because a suite that needs Chromium is a suite that stops running.
"""
import glob
import importlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ.setdefault('SECRET_KEY', 'parity-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'ParityPage-12345')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'parity.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.rfq')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead

from app.models.quote import Quote, UnmatchedQuoteMail      # noqa: E402
from app.models.rfq import RFQ, RfqCondition                # noqa: E402
from app.services import rfq_chain as rc                    # noqa: E402

ADMIN = 'PAR_T_ADMIN'
SALES = 'PAR_T_SALES'
SCM = 'PAR_T_SCM'
PEOPLE = ((ADMIN, 'Parity Test Director', 'admin'),
          (SALES, 'Parity Test Sales', 'user'),
          (SCM, 'Parity Test Costing', 'user'))

FLAGS = ('FEATURE_ACCOUNT_FIRST', 'FEATURE_RFQ_CHAIN',
         'FEATURE_QUOTE_REV_V2', 'FEATURE_PRE_RFQ_FUNNEL',
         'FEATURE_LEAD_JOURNEY', 'FEATURE_RFQ_REPORTS')

_CSS = os.path.join(_ROOT, 'static', 'css', 'crm.css')
_JS = os.path.join(_ROOT, 'static', 'js', 'rfq_ui.js')
_TEMPLATES = os.path.join(_ROOT, 'templates')

#: Every screen RFQ-DEMO-PARITY names, and the path it lives at.
SCREENS = {
    'account-check': '/accounts/check',
    'chain-board': '/rfqs/chain',
    'reports': '/rfqs/reports',
    'department-steps': '/admin/rfq-steps',
    'my-work': '/my-work/tasks',
}


# ── the world ────────────────────────────────────────────────────────
def _purge():
    from app.models.task_engine import TaskInstance
    UnmatchedQuoteMail.query.filter(
        UnmatchedQuoteMail.subject.like('PT %')).delete(
            synchronize_session=False)
    RfqCondition.query.filter(RfqCondition.text.like('PT %')).delete(
        synchronize_session=False)
    for rfq in RFQ.query.filter(RFQ.rfq_number.like('PT-%')).all():
        TaskInstance.query.filter(
            TaskInstance.entity_type == 'RFQ',
            TaskInstance.entity_id == rfq.id).delete(
                synchronize_session=False)
    Quote.query.filter(Quote.quote_number.like('PT-%')).delete(
        synchronize_session=False)
    RFQ.query.filter(RFQ.rfq_number.like('PT-%')).delete(
        synchronize_session=False)
    Lead.query.filter(Lead.company.like('PT %')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like('PT %')).delete(
        synchronize_session=False)
    Employee.query.filter(
        Employee.emp_code.in_([p[0] for p in PEOPLE])).delete(
            synchronize_session=False)
    _flags(False)
    db.session.commit()


def _flags(state):
    from app.models.integration import AppSetting
    from app.services import flags
    for name in FLAGS:
        key = 'flag.' + name
        row = AppSetting.query.filter_by(key=key).first()
        if row is None:
            row = AppSetting(key=key)
            db.session.add(row)
        row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.cache_clear()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code, name, role in PEOPLE:
            db.session.add(Employee(
                emp_code=code, name=name, password_hash='x',
                role=role, vertical='Heavy Transport', is_active=True,
                email=code.lower() + '@procamlogistics.com'))
        db.session.commit()
        _flags(True)
        yield
        _purge()


@pytest.fixture
def world():
    """Two leads: one running the chain, one before any RFQ."""
    with flask_app.app_context():
        account = Company(name='PT Steel', is_active=True)
        _set(account, dev_stage='RFQ Expected')
        db.session.add(account)
        db.session.flush()

        lead = Lead(company='PT Steel', stage='RFQ Generated',
                    source='email', procam_vertical='Heavy Transport',
                    assigned_to=SALES, company_id=account.id,
                    created_at=datetime.utcnow() - timedelta(days=6))
        _set(lead, lead_channel='Inbound Enquiries',
             lead_channel_auto=True)
        db.session.add(lead)
        db.session.flush()

        rfq = RFQ(rfq_number='PT-RFQ-1', subject='PT 140 T reformer',
                  lead_id=lead.id, account_id=account.id,
                  priority='High', lead_driver=SALES,
                  created_by_id=SALES, needs_technical=True,
                  needs_survey=True)
        db.session.add(rfq)
        db.session.flush()

        # Steps 1 and 2 closed, step 3 running and late.
        for step_no in (1, 2):
            task = rc.open_step(rfq, step_no, owner=SALES,
                                actor='test')
            task.started_at = datetime.utcnow() - timedelta(hours=2)
            db.session.flush()
            # A step that finishes early will not close without a
            # reason, by design — so give it one, as a person must.
            ok, why = rc.close_step(
                rfq, step_no, actor=SALES,
                event=rc.BY_NO[step_no]['event'],
                reason='Complete customer information')
            assert ok, why
        running = rc.steps_for(rfq)[3]
        running.owner_user_id = SCM
        running.started_at = datetime.utcnow() - timedelta(days=4)

        db.session.add(RfqCondition(
            rfq_id=rfq.id, step_no=4, type='Assumption',
            text='PT route clear of obstructions above 5.5 m',
            visibility=RfqCondition.CUSTOMER, created_by=SCM))
        db.session.add(RfqCondition(
            rfq_id=rfq.id, step_no=5, type='Condition',
            text='PT bridge at km 42 needs a structural check',
            visibility=RfqCondition.INTERNAL, created_by=SCM))
        # The same term in different words, from two desks.
        db.session.add(RfqCondition(
            rfq_id=rfq.id, step_no=5, type='Assumption',
            text='PT route clear of obstructions above 5.5 m.',
            visibility=RfqCondition.CUSTOMER, created_by=SALES))

        quote = Quote(quote_number='PT-QUO-1', rfq_id=rfq.id,
                      lead_id=lead.id, account_id=account.id,
                      currency='INR', total_amount=2500000,
                      status='Submitted', prepared_by_id=SCM,
                      approved_by_id=ADMIN,
                      approved_at=datetime.utcnow())
        _set(quote, cost_amount=1875000, revision_reason='Initial offer')
        db.session.add(quote)

        db.session.add(UnmatchedQuoteMail(
            subject='PT our offer for the reformer',
            from_addr='pt.sales@procamlogistics.com',
            to_addr='leads@procamgroup.in',
            sent_at=datetime.utcnow(),
            status=UnmatchedQuoteMail.OPEN,
            suggested_lead_id=lead.id,
            why='No RFQ or quote number in the subject'))

        early = Lead(company='PT Early', stage='New Opportunity',
                     source='manual',
                     procam_vertical='Heavy Transport',
                     assigned_to=SALES,
                     created_at=datetime.utcnow())
        db.session.add(early)
        db.session.commit()
        return {'lead_id': lead.id, 'rfq_id': rfq.id,
                'early_id': early.id, 'account_id': account.id}


def _set(obj, **kw):
    for key, value in kw.items():
        if hasattr(obj, key):
            setattr(obj, key, value)


def _client(code=ADMIN, role='admin'):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['emp_code'] = code
        s['role'] = role
    return c


def _body(path, code=ADMIN, role='admin'):
    r = _client(code, role).get(path)
    assert r.status_code == 200, (
        f'{path} -> {r.status_code}: '
        f'{r.get_data(as_text=True)[:200]}')
    return r.get_data(as_text=True)


def _json(path, code=ADMIN, role='admin'):
    r = _client(code, role).get(path)
    assert r.status_code == 200, f'{path} -> {r.status_code}'
    body = r.get_json()
    assert body.get('ok') is True, path
    return body


# ── the shared components exist at all ───────────────────────────────
def test_the_components_live_in_the_shared_stylesheet():
    """A second copy per page is how the screens drifted apart."""
    css = open(_CSS).read()
    for component in ('.rfq-tile', '.rfq-js', '.rfq-bar', '.rfq-box',
                      '.rfq-gantt', '.rfq-chip', '.rfq-ptabs',
                      '.rfq-card', '.rfq-heat'):
        assert component in css, component


def test_every_colour_in_the_rfq_section_is_a_token():
    """A literal hex here is the drift the section exists to stop."""
    css = open(_CSS).read()
    section = css[css.index('RFQ CHAIN — the components'):]
    # The section defines the three tokens the portal did not have
    # (hold, its tint, and a neutral grey); those declarations are
    # where a hex belongs. Everything after them must read a token.
    section = section[section.index('.rfq-ui{'):]
    # #fff on the primary button is the one literal the portal
    # itself uses for text on red, and it is not a theme colour.
    literals = [h for h in re.findall(r'#[0-9A-Fa-f]{3,8}\b', section)
                if h.lower() not in ('#fff', '#ffffff')]
    assert not literals, (
        f'the .rfq-* section must use crm.css tokens: {literals}')


def test_the_phone_layout_is_in_the_stylesheet():
    """Both widths are designed, not one plus a horizontal scroll."""
    css = open(_CSS).read()
    section = css[css.index('RFQ CHAIN — the components'):]
    assert '@media (max-width:620px)' in section
    for rule in ('.rfq-bar{grid-template-columns:96px',
                 '.rfq-gr{grid-template-columns:90px',
                 '.rfq-box{min-width:84px'):
        assert rule in section.replace('\n', '').replace('  ', ''), rule


def test_the_renderers_are_shared_not_copied():
    js = open(_JS).read()
    for fn in ('strip', 'leadsTable', 'sitting', 'priorityChip',
               'statusChip', 'nowCell'):
        assert f'{fn}:' in js or f'function {fn}' in js, fn


@pytest.mark.parametrize('page', ['rfq_chain.html', 'lead_journey.html',
                                  'rfq_reports.html',
                                  'account_check.html'])
def test_each_chain_page_loads_the_shared_renderers(page):
    src = open(os.path.join(_TEMPLATES, page)).read()
    assert 'static/js/rfq_ui.js' in src, page
    assert 'static/css/crm.css' in src, page


# ── screen 1: the Account check page ─────────────────────────────────
def test_the_accounts_page_has_the_demos_three_cards(world):
    html = _body('/accounts/check')
    for card in ('Account check', 'Settled today',
                 'New accounts this week'):
        assert card in html, card


def test_the_account_check_queue_still_answers(world):
    body = _json('/api/account-check/queue')
    assert any(r['company'] == 'PT Early' for r in body['rows'])


def test_a_candidate_row_carries_both_the_code_and_the_name(world):
    """The screen reads the name; callers match on the code."""
    body = _json('/api/account-check/queue')
    rows = [r for r in body['rows'] if r['company'] == 'PT Early']
    assert rows, 'the unplaced lead is not in the queue'
    for candidate in rows[0]['candidates']:
        assert 'owner' in candidate and 'owner_name' in candidate


def test_new_accounts_this_week_marks_what_is_new(world):
    body = _json('/api/account-check/owners')
    mine = [r for r in body['rows'] if r['name'] == 'PT Steel']
    assert mine and mine[0]['new_this_week'] is True
    assert body['new_this_week'] >= 1


def test_the_owners_card_counts_only_open_leads(world):
    body = _json('/api/account-check/owners')
    mine = [r for r in body['rows'] if r['name'] == 'PT Steel'][0]
    assert mine['open_leads'] == 1


def test_settled_today_answers_even_with_nothing_settled(world):
    body = _json('/api/account-check/settled')
    assert isinstance(body['rows'], list)


# ── screen 2: the chain board ────────────────────────────────────────
def test_the_chain_board_renders_the_demos_three_panels(world):
    html = _body('/rfqs/chain')
    for piece in ('Where RFQs are sitting now', 'RFQ chain',
                  'Quote mail copied to leads@'):
        assert piece in html, piece


def test_a_board_row_is_one_lead_on_one_line(world):
    row = _board_row(world)
    for key in ('ref', 'company', 'owner', 'strip', 'now', 'quoted',
                'priority', 'status'):
        assert key in row, key


def test_the_journey_strip_is_twenty_cells_in_two_lanes(world):
    """Eight before the RFQ and twelve after — the demo's reading."""
    row = _board_row(world)
    assert len(row['strip']) == 20
    assert all('cls' in c and 'title' in c for c in row['strip'])
    assert row['strip'][8]['title'].startswith('1. RFQ Received')


def test_the_running_step_is_the_only_live_cell(world):
    row = _board_row(world)
    live = [i for i, c in enumerate(row['strip'])
            if c['cls'].startswith('a ')]
    assert live == [10], f'step 3 should be the live cell, got {live}'


def test_the_board_names_the_person_not_the_code(world):
    row = _board_row(world)
    assert row['now']['with'] == 'Parity Test Costing'
    assert row['owner'] == 'Parity Test Sales'


def test_a_late_step_is_red_in_words_and_in_colour(world):
    row = _board_row(world)
    assert row['now']['tone'] == 'rfq-t-red'
    assert 'overdue' in row['now']['words'].lower()


def test_the_sitting_bars_count_what_the_list_shows(world):
    """A panel that disagrees with the list under it is worse than
    no panel: it is the one number somebody quotes in a meeting."""
    body = _json('/api/chain/board')
    for bar in body['bars']:
        here = [r for r in body['rows'] if r['step_no'] == bar['step_no']]
        assert bar['total'] == len(here), bar['title']
        assert sum(bar['counts'].values()) == bar['total'], bar['title']


def test_the_bottleneck_names_the_step_with_the_most_late(world):
    body = _json('/api/chain/board')
    assert body['bottleneck']['step_no'] == 3
    assert body['bottleneck']['late'] == 1


def test_a_held_rfq_is_purple_not_red(world):
    with flask_app.app_context():
        rfq = db.session.get(RFQ, world['rfq_id'])
        rfq.held_at = datetime.utcnow()
        rfq.hold_reason = 'Postponed by customer'
        db.session.commit()
    row = _board_row(world)
    assert row['held'] is True
    assert row['now']['tone'] == 'rfq-t-hold'
    assert 'Postponed by customer' in row['now']['words']


def test_a_won_lead_reads_as_handed_over(world):
    with flask_app.app_context():
        lead = db.session.get(Lead, world['lead_id'])
        lead.stage = 'Won'
        db.session.commit()
    row = _board_row(world)
    assert row['status'] == 'won'
    assert row['now']['what'] == 'Won · handed over'


def _board_row(world):
    body = _json('/api/chain/board')
    rows = [r for r in body['rows'] if r['lead_id'] == world['lead_id']]
    assert rows, 'the seeded RFQ is not on the board'
    return rows[0]


# ── screen 3: the leads@ queue ───────────────────────────────────────
def test_the_leads_queue_lists_the_unmatched_mail(world):
    body = _json('/api/chain/unmatched')
    assert any(r['subject'].startswith('PT our offer')
               for r in body['rows'])
    assert body['may_clear'] is True


def test_the_queue_suggests_the_lead_it_came_from(world):
    body = _json('/api/chain/unmatched')
    row = [r for r in body['rows']
           if r['subject'].startswith('PT our offer')][0]
    assert row['suggested']['id'] == world['lead_id']


def test_somebody_who_may_not_clear_the_queue_is_told_so(world):
    body = _json('/api/chain/board', code=SALES, role='user')
    assert body['may_clear_mail'] is False


# ── screen 4: the lead record ────────────────────────────────────────
def test_the_lead_record_renders_both_lanes_and_every_tab(world):
    html = _body(f'/leads/{world["lead_id"]}/journey')
    assert 'Before the RFQ' in html and 'RFQ to order' in html
    # The tab strip is built in JS, so the ampersand is raw here.
    for tab in ('Journey', 'Steps & time', 'Activity',
                'Conditions', 'Quotes', 'Spend (IOU)', 'Audit'):
        assert tab in html, tab


def test_the_record_draws_boxes_with_names_not_numbered_squares(world):
    html = _body(f'/leads/{world["lead_id"]}/journey')
    for piece in ('rfq-box', 'rfq-arr', 'rfq-flow', 'afterSub'):
        assert piece in html, piece


def test_every_chain_box_names_its_person_and_its_time(world):
    body = _json(f'/api/journey/{world["lead_id"]}')
    after = body['journey']['after']
    assert len(after) == 12
    done = [b for b in after if b['state'] == 'done']
    assert done, 'nothing is closed in the fixture'
    for box in done:
        assert box['owner_name'], box['title']
        assert box['hours'] is not None, box['title']


def test_the_record_header_carries_the_priority_and_the_state(world):
    body = _json(f'/api/journey/{world["lead_id"]}')
    assert body['priority'] == 'High'
    assert body['state'] == 'rfq'
    assert body['lead']['owner'] == 'Parity Test Sales'


def test_a_lead_with_no_rfq_reads_as_before_the_rfq(world):
    body = _json(f'/api/journey/{world["early_id"]}')
    assert body['state'] == 'pre'
    assert all(b['state'] in ('future', 'current')
               for b in body['journey']['after'])


def test_the_pre_rfq_lane_says_when_the_rfq_came_direct(world):
    """Eight boxes nobody will ever tick is not the same as eight
    boxes that were skipped, and the flowchart says which."""
    from app.services import rfq_board
    with flask_app.app_context():
        lead = db.session.get(Lead, world['lead_id'])
        account = db.session.get(Company, lead.company_id)
        account.dev_stage = ''
        db.session.commit()
        rfq = db.session.get(RFQ, world['rfq_id'])
        cells = rfq_board.strip(lead, rfq)
    assert all(c['cls'] == 's' for c in cells[:8])
    assert 'came direct' in cells[0]['title']


def test_the_step_detail_answers_for_a_box(world):
    body = _json(f'/api/journey/{world["lead_id"]}/step/3')
    assert body['step']['state'] == 'current'
    assert body['step']['owner_name'] == 'Parity Test Costing'
    assert body['step']['standard']


# ── screen 5: conditions, consolidated ───────────────────────────────
def test_conditions_are_listed_by_department(world):
    body = _json(f'/api/journey/{world["lead_id"]}/tab/conditions')
    assert len(body['rows']) == 3
    assert {r['step_title'] for r in body['rows']} == {
        'Technical Evaluation', 'Site / Route Survey'}


def test_the_quotation_list_merges_the_duplicate(world):
    """Two desks wrote the same term; the customer sees it once."""
    body = _json(f'/api/journey/{world["lead_id"]}/tab/conditions')
    texts = [c['text'] for c in body['consolidated']]
    assert len(texts) == 1
    assert body['consolidated'][0]['duplicates'] == 1


def test_an_internal_note_never_reaches_the_quotation(world):
    body = _json(f'/api/journey/{world["lead_id"]}/tab/conditions')
    assert not any('bridge at km 42' in c['text'].lower()
                   for c in body['consolidated'])


# ── screen 6: the quotation versions ─────────────────────────────────
def test_the_quotes_tab_shows_value_margin_and_who_approved(world):
    body = _json(f'/api/journey/{world["lead_id"]}/tab/quotes')
    row = body['rows'][0]
    assert row['margin'] == 25
    assert row['money'] == '₹25.00 L'
    assert row['approved_by'] == 'Parity Test Director'
    assert row['status'] == 'Submitted'


# ── screen 7: spend, which the TMS owns ──────────────────────────────
def test_spend_says_who_fills_it_rather_than_showing_nothing(world):
    body = _json(f'/api/journey/{world["lead_id"]}/tab/spend')
    assert body['linked'] is False
    assert 'Not set yet' in body['missing']
    assert f'crm_lead_no={world["lead_id"]}' in body['form']


def test_the_spend_tab_links_to_the_readiness_panel(world):
    html = _body(f'/leads/{world["lead_id"]}/journey')
    assert '/admin/crm-rules' in html


# ── screen 8: the audit trail ────────────────────────────────────────
def test_the_audit_tab_lists_the_step_closes(world):
    body = _json(f'/api/journey/{world["lead_id"]}/tab/audit')
    assert any('step_closed' in r['action'] for r in body['rows'])


# ── screen 9: waiting on me ──────────────────────────────────────────
def test_waiting_on_me_is_the_demos_table(world):
    html = _body('/my-work/tasks', code=SCM, role='user')
    assert 'Waiting on me — RFQ steps' in html
    for column in ('>Lead<', '>Next action<', '>Since<', '>Timer<'):
        assert column in html, column
    assert 'PT-RFQ-1' in html
    assert '>Act</a>' in html


def test_a_waiting_row_says_since_when_and_what_closes_it(world):
    body = _client(SCM, 'user').get('/my-work.json').get_json()
    step = body['chain_steps'][0]
    assert step['since']
    assert step['event'] == 'confirm'
    assert step['tone'] == 'rfq-t-red'


def test_the_count_matches_the_rows_in_the_table(world):
    """The heading's number and the table under it are one list."""
    body = _client(SCM, 'user').get('/my-work.json').get_json()
    html = _body('/my-work/tasks', code=SCM, role='user')
    shown = html.count('class="rfq-btn p s"')
    assert shown == len(body['chain_steps']) == 1


# ── screen 10: the department steps ──────────────────────────────────
def test_the_department_steps_page_lists_all_twelve(world):
    html = _body('/admin/rfq-steps')
    for step in rc.STEPS:
        assert step['title'] in html, step['title']
    assert 'Steps that close themselves' in html


def test_each_step_says_which_desk_and_what_closes_it(world):
    body = _json('/api/admin/rfq-steps')
    assert len(body['steps']) == 12
    for step in body['steps']:
        assert step['desk'] and step['desk'] != '—', step['title']
        assert step['closes'], step['title']
        assert step['routing'], step['title']


def test_only_the_three_confirm_steps_take_a_click(world):
    body = _json('/api/admin/rfq-steps')
    clicks = [s['no'] for s in body['steps'] if s['confirmable']]
    assert clicks == list(rc.CONFIRMABLE)


def test_no_persons_code_appears_in_the_step_catalogue(world):
    """The routing is a rule, not a name — that is the whole point
    of the setting tables behind it."""
    body = _json('/api/admin/rfq-steps')
    blob = json.dumps(body)
    for code, _name, _role in PEOPLE:
        assert code not in blob, code


def test_the_department_steps_page_is_admin_only(world):
    r = _client(SALES, 'user').get('/admin/rfq-steps')
    assert r.status_code == 403


# ── screen 11: the reports ───────────────────────────────────────────
def test_the_reports_page_has_every_panel_the_demo_has(world):
    html = _body('/rfqs/reports')
    for panel in ('Time per step against standard',
                  'Delay &amp; good-practice reasons',
                  'Workload &amp; completion',
                  'Person × step: average time against standard',
                  'Sales spend tagged to leads (IOU)'):
        assert panel in html, panel


def test_the_reports_tiles_match_the_lists_behind_them(world):
    """Every tile on the demo opens the list it counts."""
    tiles = _json('/api/reports/tiles')['tiles']
    board = _json('/api/chain/board')
    live = [r for r in board['rows'] if r['step_no']]
    assert tiles['active'] == len(live)
    assert tiles['on_hold'] == len([r for r in board['rows']
                                    if r['held']])
    assert tiles['overdue'] == len(
        [r for r in live if r['now']['tone'] == 'rfq-t-red'])


def test_the_workload_table_names_people_and_splits_by_priority(world):
    body = _json('/api/reports/workload')
    rows = {r['emp_code']: r for r in body['rows']}
    assert rows[SALES]['name'] == 'Parity Test Sales'
    assert 'High' in rows[SALES]['by_priority']


def test_the_heat_table_carries_a_short_name_per_column(world):
    body = _json('/api/reports/heatmap')
    assert len(body['steps']) == 12
    for step in body['steps']:
        assert step['short']
        assert len(step['short']) <= len(step['title'])


def test_the_reasons_panel_splits_late_from_early(world):
    body = _json('/api/reports/reasons')
    assert 'reason' in body['delay'] or body['delay'] == {}
    assert isinstance(body['good_practice'], dict)


def test_lateness_is_also_cut_by_the_desk_it_sat_on(world):
    """A person is told; a function is staffed."""
    with flask_app.app_context():
        rfq = db.session.get(RFQ, world['rfq_id'])
        rc.close_step(rfq, 3, actor=SCM, event='confirm',
                      reason='Awaiting vendor rate')
        db.session.commit()
    body = _json('/api/reports/reasons')
    assert body['delay']['function'] == {'SCM': 1}


# ── with the flags off, every screen is what it always was ───────────
def _snapshot(path, code=ADMIN, role='admin'):
    r = _client(code, role).get(path)
    return r.status_code, r.get_data(as_text=True)


def test_with_the_flags_off_the_chain_screens_are_gone(world):
    with flask_app.app_context():
        _flags(False)
    for path in ('/rfqs/chain', '/rfqs/reports', '/accounts/check',
                 f'/leads/{world["lead_id"]}/journey'):
        r = _client().get(path)
        assert r.status_code == 404, f'{path} -> {r.status_code}'


def test_my_work_is_the_page_it_always_was_with_the_flag_off(world):
    """The one screen the chain only adds a block to.

    With the flag off it must be the page it always was — not a
    similar page, the same one. Everything outside the chain block
    is compared character for character, because every other check
    passes while a stray empty table sits on it.
    """
    with flask_app.app_context():
        _flags(False)
    status, off = _snapshot('/my-work/tasks', SCM, 'user')
    assert status == 200
    assert 'Waiting on me — RFQ steps' not in off
    assert '<div class="bucket" id="bucket-chain"' not in off
    with flask_app.app_context():
        _flags(True)
    _status, on = _snapshot('/my-work/tasks', SCM, 'user')
    assert 'Waiting on me — RFQ steps' in on
    assert _without_chain(on) == _without_chain(off)


def _without_chain(html):
    """The page with the chain block, and only it, cut out."""
    marker = '<div class="buckets">'
    head = html[:html.index(marker)]
    # Everything above the buckets differs only by the chain block
    # and the counts it does not touch; drop the block, keep the
    # rest, and compare that to the same slice of the other page.
    start = head.find('<div class="bucket" id="bucket-chain"')
    if start >= 0:
        head = head[:start]
    return head.rstrip() + html[html.index(marker):]


def test_the_department_steps_page_is_not_behind_a_chain_flag(world):
    """It describes the chain; it is not part of running one, and
    an admin must be able to read it while the pilot is off."""
    with flask_app.app_context():
        _flags(False)
    assert _client().get('/admin/rfq-steps').status_code == 200


# ── the guards that must keep holding ────────────────────────────────
def test_every_chain_page_includes_the_csrf_helper(world):
    for path in ('/rfqs/chain', '/rfqs/reports', '/accounts/check',
                 '/admin/rfq-steps',
                 f'/leads/{world["lead_id"]}/journey'):
        assert '__crmCsrfFetch' in _body(path), path


def test_no_chain_template_hardcodes_an_unprefixed_link():
    pattern = re.compile(r'(?:href|src|action)="(/(?!/)[^"]*)"')
    for name in ('rfq_chain.html', 'lead_journey.html',
                 'rfq_reports.html', 'account_check.html',
                 os.path.join('admin', 'rfq_steps.html')):
        src = open(os.path.join(_TEMPLATES, name)).read()
        for url in pattern.findall(src):
            assert url.startswith('{{'), f'{name}: {url}'


def test_the_screenshot_runner_covers_every_screen():
    """A screen nobody photographs is a screen that drifts again."""
    runner = open(os.path.join(_ROOT, 'scripts',
                               'parity_shots.py')).read()
    for path in SCREENS.values():
        root = path.split('#')[0].replace('{lead}', '')
        assert root.split('/')[1] in runner, path


def test_the_parity_folder_holds_a_pair_for_every_screen():
    folder = os.path.join(_ROOT, 'docs', 'reference', 'parity')
    shots = {os.path.basename(p)
             for p in glob.glob(os.path.join(folder, '*.png'))}
    assert shots, 'no screenshots have been taken'
    slugs = {s.split('--')[0] for s in shots}
    for slug in slugs:
        for want in (f'{slug}--demo-desktop.png',
                     f'{slug}--demo-phone.png',
                     f'{slug}--live-desktop-after.png',
                     f'{slug}--live-phone-after.png'):
            assert want in shots, want
