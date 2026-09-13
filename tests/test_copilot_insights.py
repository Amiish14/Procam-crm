"""
The second shelf of intents — health, risk, relationships, the document
queues, performance and admin — checked two ways.

Leakage first, as §16 insists: a rep whose Access Matrix scope is Own
runs every new intent with every way of naming the other owner's
records (by name, by id, as the panel's context, as a lead or an
opportunity) and must never see one of that owner's distinctive values.
Each has a control — the owner and an admin DO see them — or the test
would pass on an empty CRM.

Then the scores: each health and risk answer is a documented sum, so the
tests seed a known account and require the exact number, and require
the answer to state its factors.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'InsightsTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'insights.db'))
os.environ.pop('PROCAM_AI_BASE_URL', None)

from app import (app as flask_app, db, Company, Contact,          # noqa: E402
                 EmailClassification, Employee, Lead, LeadEmail,
                 Opportunity)
from app.access import scope as scope_mod                        # noqa: E402
from app.access.service import set_profile                       # noqa: E402
from app.copilot import insights                                 # noqa: E402
from app.copilot import intents as catalogue                     # noqa: E402
from app.copilot import service as svc                           # noqa: E402
from app.models.access import DataScope                          # noqa: E402

ALL_PERMS = ['module.rfq', 'module.quotes', 'module.handovers',
             'module.funnels', 'reports.action', 'reports.accounts',
             'reports.competitor', 'admin.master', 'admin.access']

#: Every intent this suite covers — the new shelf.
NEW_INTENTS = [
    'quotes_pending_approval', 'quotes_expiring', 'quotes_recent',
    'quotes_by_status', 'rfqs_recent', 'rfqs_by_status',
    'handovers_awaiting_po', 'account_health', 'pipeline_health',
    'opportunity_risk', 'opps_overdue_close', 'account_pipeline',
    'cross_sell_account', 'relationship_map', 'key_contacts',
    'top_accounts', 'leads_new', 'leads_by_stage', 'lead_sources',
    'leads_unassigned', 'deals_won_recent', 'deals_lost_recent',
    'win_rate_by_vertical', 'my_win_rate', 'team_workload',
    'dq_accounts_no_owner', 'intake_review_pending', 'my_tasks',
]

#: Values that exist only on the other owner's records.
THEIRS_MARKERS = ('9.10 cr', '7.30 cr', '8.80 cr', 'OPP-CI-THEIRS',
                  'OPP-CI-THEIRWON', 'Q-CI-2', 'R-CI-2',
                  'Plant Head Theirs', 'Theirs secret', 'theirs@example')


@pytest.fixture(scope='module')
def world():
    with flask_app.app_context():
        db.create_all()
        people = (('CIADM', 'admin', 'All'),
                  ('CIHEAD', 'user', 'Heavy Transport'),
                  ('CIREP', 'user', 'Heavy Transport'),
                  ('CIOUT', 'user', 'Warehousing'))
        for code, role, vertical in people:
            e = Employee.query.filter_by(emp_code=code).first()
            if e is None:
                e = Employee(emp_code=code, name=f'{code} name')
                db.session.add(e)
            e.role, e.vertical, e.is_active = role, vertical, True
            e.is_vertical_head = code == 'CIHEAD'
            e.must_change_pw, e.is_super_admin = False, False
            e.department = None
        db.session.commit()

        today = date.today()
        now = datetime.utcnow()
        mine = Company(name='Insight Mine Co', is_active=True,
                       pic_emp_code='CIREP', vertical='Heavy Transport')
        theirs = Company(name='Insight Theirs Co', is_active=True,
                         pic_emp_code='CIOUT', vertical='Warehousing')
        orphan = Company(name='Insight Orphan Co', is_active=True)
        twin_a = Company(name='Insight Twin Alpha', is_active=True,
                         pic_emp_code='CIREP')
        twin_b = Company(name='Insight Twin Beta', is_active=True,
                         pic_emp_code='CIREP')
        db.session.add_all([mine, theirs, orphan, twin_a, twin_b])
        db.session.flush()

        lead_mine = Lead(company='Insight Mine Co', source='manual',
                         company_id=mine.id, assigned_to='CIREP',
                         stage='Quoted', estimated_value_inr=5_000_000,
                         procam_vertical='Transportation',
                         followup_date=today + timedelta(days=3),
                         pic='Plant Head Mine', email='mine@example.com')
        lead_theirs = Lead(company='Insight Theirs Co', source='manual',
                           company_id=theirs.id, assigned_to='CIOUT',
                           stage='Under Negotiation',
                           estimated_value_inr=91_000_000,
                           procam_vertical='Warehousing',
                           pic='Plant Head Theirs',
                           email='theirs@example.com')
        lead_orphan = Lead(company='Insight Orphan Co', source='email',
                           company_id=orphan.id, stage='New')
        lead_rep_orphan = Lead(company='Insight Orphan Co', source='manual',
                               company_id=orphan.id, assigned_to='CIREP',
                               stage='New')
        lead_lost = Lead(company='Insight Mine Co', source='manual',
                         company_id=mine.id, assigned_to='CIREP',
                         stage='Lost', lost_reason='Price')
        db.session.add_all([lead_mine, lead_theirs, lead_orphan,
                            lead_rep_orphan, lead_lost])
        db.session.flush()

        opp_mine = Opportunity(opp_number='OPP-CI-MINE', owner_emp_code='CIREP',
                               company_id=mine.id, lead_id=lead_mine.id,
                               stage='Quoted', value_inr=5_000_000,
                               probability=20,
                               expected_close_date=today - timedelta(days=4))
        opp_theirs = Opportunity(opp_number='OPP-CI-THEIRS',
                                 owner_emp_code='CIOUT', company_id=theirs.id,
                                 lead_id=lead_theirs.id, stage='Quoted',
                                 value_inr=91_000_000, probability=60,
                                 expected_close_date=today - timedelta(days=9))
        won_theirs = Opportunity(opp_number='OPP-CI-THEIRWON',
                                 owner_emp_code='CIOUT', company_id=theirs.id,
                                 stage='Won', value_inr=73_000_000,
                                 won_at=now - timedelta(days=5))
        won_mine = Opportunity(opp_number='OPP-CI-MINEWON',
                               owner_emp_code='CIREP', company_id=mine.id,
                               lead_id=lead_mine.id, stage='Won',
                               value_inr=2_000_000,
                               won_at=now - timedelta(days=6))
        db.session.add_all([opp_mine, opp_theirs, won_theirs, won_mine])
        db.session.flush()

        from app.models.quote import Quote
        from app.models.rfq import RFQ
        from app.models.task_engine import TaskInstance
        from app.models.tms_handover import WonHandover
        db.session.add_all([
            Quote(quote_number='Q-CI-1', subject='Mine approval',
                  account_id=mine.id, lead_id=lead_mine.id,
                  prepared_by_id='CIREP', status='Awaiting Approval',
                  total_amount=1_500_000, quote_date=today),
            Quote(quote_number='Q-CI-2', subject='Theirs approval',
                  account_id=theirs.id, lead_id=lead_theirs.id,
                  prepared_by_id='CIOUT', status='Awaiting Approval',
                  total_amount=88_000_000, quote_date=today,
                  validity_until=today + timedelta(days=2)),
            Quote(quote_number='Q-CI-3', subject='Mine live',
                  account_id=mine.id, lead_id=lead_mine.id,
                  prepared_by_id='CIREP', status='Submitted',
                  total_amount=1_200_000, quote_date=today,
                  validity_until=today + timedelta(days=3)),
            RFQ(rfq_number='R-CI-1', subject='Mine rfq', account_id=mine.id,
                lead_id=lead_mine.id, lead_driver='CIREP',
                received_date=today),
            RFQ(rfq_number='R-CI-2', subject='Theirs secret rfq',
                account_id=theirs.id, lead_id=lead_theirs.id,
                lead_driver='CIOUT', received_date=today),
            WonHandover(opportunity_id=won_theirs.id, account_id=theirs.id,
                        account_name='Insight Theirs Co',
                        won_value=73_000_000, pic_emp_code='CIOUT',
                        status='Awaiting PO'),
            Contact(name='Plant Head Mine', company_id=mine.id,
                    assigned_to='CIREP', decision_role='Decision Maker'),
            Contact(name='Plant Head Theirs', company_id=theirs.id,
                    assigned_to='CIOUT', decision_role='Decision Maker'),
            LeadEmail(lead_id=lead_mine.id, direction='inbound',
                      subject='Mine enquiry', body='please quote',
                      sent_or_received_at=now - timedelta(days=2)),
            LeadEmail(lead_id=lead_theirs.id, direction='outbound',
                      subject='Theirs secret mail', body='x',
                      created_by='CIOUT',
                      sent_or_received_at=now - timedelta(days=1)),
            EmailClassification(classification='lead',
                                subject='Theirs secret intake',
                                review_state='pending'),
            TaskInstance(task_key='prepare_quote', entity_type='lead',
                         entity_id=lead_mine.id,
                         entity_display='Insight Mine Co',
                         owner_user_id='CIREP',
                         due_at=now - timedelta(days=1)),
        ])
        db.session.commit()
        ids = {'mine': mine.id, 'theirs': theirs.id, 'orphan': orphan.id,
               'lead_mine': lead_mine.id, 'lead_theirs': lead_theirs.id,
               'lead_orphan': lead_orphan.id, 'opp_mine': opp_mine.id,
               'opp_theirs': opp_theirs.id, 'twins': (twin_a.id, twin_b.id)}
        all_leads = [lead_mine.id, lead_theirs.id, lead_orphan.id,
                     lead_rep_orphan.id, lead_lost.id]
        all_opps = [opp_mine.id, opp_theirs.id, won_theirs.id, won_mine.id]
        all_companies = [mine.id, theirs.id, orphan.id, twin_a.id, twin_b.id]

    yield ids

    with flask_app.app_context():
        from app.models.quote import Quote
        from app.models.rfq import RFQ
        from app.models.task_engine import TaskInstance
        from app.models.tms_handover import WonHandover
        TaskInstance.query.filter_by(task_key='prepare_quote',
                                     owner_user_id='CIREP').delete()
        EmailClassification.query.filter_by(
            subject='Theirs secret intake').delete()
        LeadEmail.query.filter(LeadEmail.lead_id.in_(all_leads)).delete(
            synchronize_session=False)
        Contact.query.filter(Contact.company_id.in_(all_companies)).delete(
            synchronize_session=False)
        WonHandover.query.filter(
            WonHandover.opportunity_id.in_(all_opps)).delete(
            synchronize_session=False)
        Quote.query.filter(Quote.quote_number.like('Q-CI-%')).delete(
            synchronize_session=False)
        RFQ.query.filter(RFQ.rfq_number.like('R-CI-%')).delete(
            synchronize_session=False)
        Opportunity.query.filter(Opportunity.id.in_(all_opps)).delete(
            synchronize_session=False)
        Lead.query.filter(Lead.id.in_(all_leads)).delete(
            synchronize_session=False)
        Company.query.filter(Company.id.in_(all_companies)).delete(
            synchronize_session=False)
        db.session.commit()


def _scope(code, data_scope, perms=ALL_PERMS):
    set_profile(code, data_scope, list(perms), actor='CIADM')
    return scope_mod.for_employee(code)


def _blob(result):
    parts = [result.headline or ''] + list(result.notes or [])
    for r in result.rows or []:
        parts.extend(str(v) for k, v in r.items() if not k.startswith('_'))
    parts.extend(str(v) for v in (result.figures or {}).values())
    parts.extend(str(c.get('label')) for c in result.citations or [])
    if result.clarification:
        parts.extend(o['label'] for o in result.clarification['options'])
    return ' | '.join(parts)


def _probes(world):
    theirs = world['theirs']
    return [{}, {'account': 'Insight Theirs Co'}, {'account_id': theirs},
            {'context': {'type': 'company', 'id': theirs}},
            {'lead_id': world['lead_theirs']},
            {'context': {'type': 'lead', 'id': world['lead_theirs']}},
            {'opportunity_id': world['opp_theirs']},
            {'context': {'type': 'opportunity', 'id': world['opp_theirs']}},
            {'opportunity': 'OPP-CI-THEIRS'}, {'segment': 'customers'},
            {'days': 3650}, {'stage': 'po'}]


# ══════════════════════════════════════════════════════════════════
#  1 · leakage, per intent, every way of naming the other record
# ══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize('key', NEW_INTENTS)
def test_an_own_scope_rep_never_sees_another_owners_rows(world, key):
    with flask_app.app_context():
        sc = _scope('CIREP', DataScope.OWN)
        intent = catalogue.get(key)
        assert intent is not None, key
        for params in _probes(world):
            result = intent.handler(sc, dict(params))
            blob = _blob(result)
            for marker in THEIRS_MARKERS:
                assert marker not in blob, (key, params, marker, blob)


@pytest.mark.parametrize('key', NEW_INTENTS)
def test_a_vertical_head_never_sees_the_other_verticals_rows(world, key):
    with flask_app.app_context():
        sc = _scope('CIHEAD', DataScope.VERTICAL)
        for params in _probes(world):
            blob = _blob(catalogue.get(key).handler(sc, dict(params)))
            for marker in THEIRS_MARKERS:
                assert marker not in blob, (key, params, marker)


def test_naming_the_other_owners_account_gives_routing_only(world):
    """§6.6 — existence and owner, never detail."""
    with flask_app.app_context():
        sc = _scope('CIREP', DataScope.OWN)
        for key in ('account_health', 'cross_sell_account',
                    'relationship_map', 'key_contacts', 'account_pipeline'):
            r = catalogue.get(key).handler(sc, {'account': 'Insight Theirs Co'})
            assert r.restricted is True, key
            assert 'CIOUT name' in r.headline, key
            assert not r.rows, key


def test_the_admin_does_see_the_other_owners_rows(world):
    """The control: without it every leak test passes on an empty CRM."""
    with flask_app.app_context():
        sc = _scope('CIADM', DataScope.ALL)
        checks = {
            'quotes_pending_approval': ({}, 'Q-CI-2'),
            'quotes_recent': ({}, 'Q-CI-2'),
            'opportunity_risk': ({}, 'OPP-CI-THEIRS'),
            'opps_overdue_close': ({}, 'OPP-CI-THEIRS'),
            'deals_won_recent': ({}, '7.30 cr'),
            'handovers_awaiting_po': ({}, '7.30 cr'),
            'key_contacts': ({'account': 'Insight Theirs Co'},
                             'Plant Head Theirs'),
            'account_pipeline': ({'account': 'Insight Theirs Co'},
                                 'OPP-CI-THEIRS'),
            'rfqs_recent': ({}, 'R-CI-2'),
            'top_accounts': ({}, '7.30 cr'),
        }
        for key, (params, marker) in checks.items():
            blob = _blob(catalogue.get(key).handler(sc, params))
            assert marker in blob, (key, blob)


def test_the_owner_sees_their_own(world):
    with flask_app.app_context():
        sc = _scope('CIOUT', DataScope.OWN)
        r = catalogue.get('account_health').handler(
            sc, {'account': 'Insight Theirs Co'})
        assert r.restricted is False
        assert r.figures['score'] > 0
        blob = _blob(catalogue.get('quotes_pending_approval').handler(sc, {}))
        assert 'Q-CI-2' in blob and 'Q-CI-1' not in blob


def test_the_permission_on_each_gated_intent_is_enforced(world):
    """Through the pipeline, an intent's own permission bites."""
    with flask_app.app_context():
        bare = _scope('CIREP', DataScope.OWN, perms=[])
        for question in ('quotes pending approval', 'pipeline health',
                         'which deals are at risk', 'top accounts',
                         'win rate by vertical', 'team workload',
                         'intake review queue', 'pending handovers'):
            answer = svc.ask(question, sc=bare)
            assert answer.result.restricted is True, question
            assert 'access' in (answer.prose or '').lower(), question


def test_company_wide_only_answers_say_so_to_everyone_else(world):
    with flask_app.app_context():
        sc = _scope('CIREP', DataScope.OWN)
        for key in ('leads_unassigned', 'intake_review_pending'):
            r = catalogue.get(key).handler(sc, {})
            assert r.restricted is True and r.empty is True, key
            assert not any(ch.isdigit() for ch in r.headline), key
        admin = _scope('CIADM', DataScope.ALL)
        r = catalogue.get('leads_unassigned').handler(admin, {})
        assert 'Insight Orphan Co' in _blob(r)
        r = catalogue.get('intake_review_pending').handler(admin, {})
        assert r.figures['count'] >= 1
        assert 'Theirs secret intake' not in _blob(r)   # counts, not mail


# ══════════════════════════════════════════════════════════════════
#  2 · transparent scores
# ══════════════════════════════════════════════════════════════════
def test_account_health_is_the_documented_sum(world):
    with flask_app.app_context():
        sc = _scope('CIREP', DataScope.OWN)
        r = catalogue.get('account_health').handler(
            sc, {'account': 'Insight Mine Co'})
        points = {row['Factor']: row['Points'] for row in r.rows}
        # contact 2 days ago · open opp · won 6 days ago · follow-up in
        # 3 days · owner · one contact
        assert points == {'Recent contact': 30, 'Open pipeline': 20,
                          'Won business': 20, 'Next action': 15,
                          'Owner': 10, 'Contacts': 5}
        assert r.figures['score'] == 100 == sum(points.values())
        assert r.figures['band'] == 'healthy'
        assert any('Health = recent contact' in n for n in r.notes)


def test_account_health_drops_by_exactly_the_missing_factor(world):
    with flask_app.app_context():
        lead = db.session.get(Lead, world['lead_mine'])
        lead.followup_date = None
        db.session.commit()
        try:
            sc = _scope('CIREP', DataScope.OWN)
            r = catalogue.get('account_health').handler(
                sc, {'account': 'Insight Mine Co'})
            assert r.figures['score'] == 85
            assert 'next action' in r.headline.lower()
        finally:
            lead.followup_date = date.today() + timedelta(days=3)
            db.session.commit()


def test_account_health_list_ranks_weakest_first(world):
    with flask_app.app_context():
        sc = _scope('CIREP', DataScope.OWN)
        r = catalogue.get('account_health').handler(sc, {})
        scores = [row['Score'] for row in r.rows]
        assert scores == sorted(scores)
        assert 'Insight Mine Co' in [row['Account'] for row in r.rows]
        customers = catalogue.get('account_health').handler(
            sc, {'segment': 'customers'})
        assert [row['Account'] for row in customers.rows] == \
            ['Insight Mine Co']


def test_pipeline_health_counts_each_check(world):
    """The rep's one open opportunity: close date passed (risk) and it is
    100% of open value (risk); everything else ok — 100 − 20 − 20."""
    with flask_app.app_context():
        sc = _scope('CIREP', DataScope.OWN)
        r = catalogue.get('pipeline_health').handler(sc, {})
        status = {row['Check']: row['Status'] for row in r.rows}
        assert status['Close date already passed'] == 'risk'
        assert status['Largest deal share of open value'] == 'risk'
        assert status['No expected close date'] == 'ok'
        assert r.figures['score'] == 60
        assert r.figures['band'] == 'watch'
        assert any('loses 20' in n for n in r.notes)


def test_opportunity_risk_for_one_opportunity(world):
    """Close date passed 30 + probability 20% 10 = 40, medium."""
    with flask_app.app_context():
        sc = _scope('CIREP', DataScope.OWN)
        r = catalogue.get('opportunity_risk').handler(
            sc, {'opportunity_id': world['opp_mine']})
        assert r.figures['score'] == 40
        assert r.figures['band'] == 'medium'
        assert sum(row['Points'] for row in r.rows) == 40
        assert any('Risk = close date passed 30' in n for n in r.notes)
        other = catalogue.get('opportunity_risk').handler(
            sc, {'opportunity_id': world['opp_theirs']})
        assert other.restricted is True


def test_every_health_and_risk_answer_states_its_factors(world):
    with flask_app.app_context():
        sc = _scope('CIADM', DataScope.ALL)
        for key, params, phrase in (
                ('account_health', {'account': 'Insight Mine Co'},
                 'Health ='),
                ('account_health', {}, 'Health ='),
                ('pipeline_health', {}, 'Health starts at 100'),
                ('opportunity_risk', {}, 'Risk ='),
                ('next_best_action', {}, 'Score =')):
            r = catalogue.get(key).handler(sc, params)
            assert any(phrase in n for n in r.notes), (key, r.notes)


# ══════════════════════════════════════════════════════════════════
#  3 · what each answers
# ══════════════════════════════════════════════════════════════════
def test_cross_sell_reads_bought_services_from_won_business(world):
    with flask_app.app_context():
        sc = _scope('CIREP', DataScope.OWN)
        r = catalogue.get('cross_sell_account').handler(
            sc, {'account': 'Insight Mine Co'})
        status = {row['Service']: row['Status'] for row in r.rows}
        assert status['Heavy Transport'] == 'Buys'
        assert status['Warehousing'] == 'Never'
        assert r.recommendation is True


def test_relationship_map_and_contacts(world):
    with flask_app.app_context():
        sc = _scope('CIREP', DataScope.OWN)
        rel = catalogue.get('relationship_map').handler(
            sc, {'account': 'Insight Mine Co'})
        people = {row['Person']: row['Role'] for row in rel.rows}
        assert 'account owner' in people['CIREP name']
        contacts = catalogue.get('key_contacts').handler(
            sc, {'account': 'Insight Mine Co'})
        assert [row['Name'] for row in contacts.rows] == ['Plant Head Mine']
        from_lead = catalogue.get('key_contacts').handler(
            sc, {'lead_id': world['lead_mine']})
        assert from_lead.rows[0]['Name'] == 'Plant Head Mine'


def test_documents_queues(world):
    with flask_app.app_context():
        sc = _scope('CIREP', DataScope.OWN)
        approval = catalogue.get('quotes_pending_approval').handler(sc, {})
        assert [row['Quote'] for row in approval.rows] == ['Q-CI-1']
        expiring = catalogue.get('quotes_expiring').handler(sc, {})
        assert [(row['Quote'], row['Days left'])
                for row in expiring.rows] == [('Q-CI-3', 3)]
        recent = catalogue.get('rfqs_recent').handler(sc, {})
        assert [row['RFQ'] for row in recent.rows] == ['R-CI-1']


def test_overdue_rfqs_are_the_unquoted_list_cut_to_late_ones(world):
    from app.models.rfq import RFQ
    with flask_app.app_context():
        rfq = RFQ.query.filter_by(rfq_number='R-CI-1').first()
        rfq.quote_by_date = date.today() - timedelta(days=2)
        db.session.commit()
        try:
            sc = _scope('CIREP', DataScope.OWN)
            everything = catalogue.get('rfqs_unquoted').handler(sc, {})
            late = catalogue.get('rfqs_unquoted').handler(
                sc, {'overdue': 'true'})
            assert 'R-CI-1' in [row['RFQ'] for row in late.rows]
            assert late.figures['count'] <= everything.figures['count']
            assert 'Overdue' not in late.columns
        finally:
            rfq.quote_by_date = None
            db.session.commit()


def test_ownerless_accounts_reach_a_rep_through_their_own_lead(world):
    with flask_app.app_context():
        sc = _scope('CIREP', DataScope.OWN)
        r = catalogue.get('dq_accounts_no_owner').handler(sc, {})
        assert [row['Account'] for row in r.rows] == ['Insight Orphan Co']
        other = catalogue.get('dq_accounts_no_owner').handler(
            _scope('CIOUT', DataScope.OWN), {})
        assert 'Insight Orphan Co' not in _blob(other)


def test_my_win_rate_is_only_mine(world):
    with flask_app.app_context():
        sc = _scope('CIHEAD', DataScope.VERTICAL)
        r = catalogue.get('my_win_rate').handler(sc, {})
        assert r.figures['won'] == 0          # the head owns nothing
        rep = catalogue.get('my_win_rate').handler(
            _scope('CIREP', DataScope.OWN), {})
        assert rep.figures['won'] == 1
        assert 'too few' in rep.headline


def test_loss_reasons_need_the_permission_loss_analysis_needs(world):
    """The reason is the competitive half of a loss. Listing recent
    losses must not become a way round reports.competitor."""
    with flask_app.app_context():
        without = _scope('CIREP', DataScope.OWN, perms=['module.funnels'])
        r = catalogue.get('deals_lost_recent').handler(without, {})
        assert 'Reason' not in r.columns
        assert 'Price' not in _blob(r)
        with_perm = _scope('CIREP', DataScope.OWN)
        r = catalogue.get('deals_lost_recent').handler(with_perm, {})
        assert 'Reason' in r.columns
        assert 'Price' in _blob(r)


def test_my_tasks_reads_the_task_engine(world):
    with flask_app.app_context():
        sc = _scope('CIREP', DataScope.OWN)
        r = catalogue.get('my_tasks').handler(sc, {})
        assert any(row['Record'] == 'Insight Mine Co' for row in r.rows)
        assert r.figures['overdue'] >= 1


def test_a_service_filter_matches_every_spelling_of_the_service(world):
    """The lead says "Transportation"; the question says "Heavy
    Transport". One service, so one answer."""
    with flask_app.app_context():
        # The account's own vertical is cleared, so only the lead's
        # "Transportation" can match.
        company = db.session.get(Company, world['mine'])
        company.vertical = None
        db.session.commit()
        try:
            sc = _scope('CIREP', DataScope.OWN)
            heavy = catalogue.get('leads_open').handler(
                sc, {'vertical': 'Heavy Transport'})
            assert 'Insight Mine Co' in _blob(heavy)
            assert heavy.filters == {'vertical': 'Heavy Transport'}
            other = catalogue.get('leads_open').handler(
                sc, {'vertical': 'Warehousing'})
            assert 'Insight Mine Co' not in _blob(other)
        finally:
            company.vertical = 'Heavy Transport'
            db.session.commit()


def test_two_matching_accounts_come_back_as_a_clarification(world):
    with flask_app.app_context():
        sc = _scope('CIREP', DataScope.OWN)
        r = catalogue.get('account_health').handler(
            sc, {'account': 'Insight Twin'})
        assert r.clarification is not None
        options = r.clarification['options']
        assert [o['label'] for o in options] == ['Insight Twin Alpha',
                                                 'Insight Twin Beta']
        for o in options:
            key, params = svc._match_patterns(o['question'])
            assert key == 'account_health'
            assert params['account'] in ('Insight Twin Alpha',
                                         'Insight Twin Beta')


def test_next_best_action_puts_a_waiting_customer_first(world):
    """The rep's lead has an unanswered inbound email (50, plus 5 for ₹50
    lakh). A second lead with an overdue follow-up and no value scores
    40 plus two idle points — so only the waiting-customer weight puts
    the first one on top."""
    from app.copilot import queries

    with flask_app.app_context():
        rival = Lead(company='Insight Rival Co', source='manual',
                     assigned_to='CIREP', stage='Visit Done',
                     estimated_value_inr=0,
                     followup_date=date.today() - timedelta(days=2),
                     created_at=datetime.utcnow() - timedelta(days=6))
        db.session.add(rival)
        db.session.commit()
        try:
            sc = _scope('CIREP', DataScope.OWN)
            r = catalogue.get('next_best_action').handler(sc, {})
            names = [row['Company'] for row in r.rows]
            assert names.index('Insight Mine Co') < \
                names.index('Insight Rival Co')
            first = r.rows[0]
            assert 'no reply since' in first['Why']
            assert first['Score'] >= \
                queries.NBA_REASON_WEIGHT['customer_waiting']
        finally:
            db.session.delete(rival)
            db.session.commit()


def test_next_best_action_for_one_lead(world):
    with flask_app.app_context():
        sc = _scope('CIREP', DataScope.OWN)
        r = catalogue.get('next_best_action').handler(
            sc, {'lead_id': world['lead_mine']})
        actions = [row['Action'] for row in r.rows]
        assert actions[0] == 'Reply to the customer'
        assert any(a.startswith('Confirm or revise Q-CI-3') for a in actions)
        assert r.recommendation is True
        hidden = catalogue.get('next_best_action').handler(
            sc, {'lead_id': world['lead_theirs']})
        assert hidden.restricted is True
        assert 'Insight Theirs Co' not in _blob(hidden)
