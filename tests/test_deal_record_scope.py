"""
RFQs, quotes, handovers, accounts and contacts: a direct link reaches
only what the list would show.

Two sales users with Own scope work separate deals. Each probe is a
request that used to succeed for any signed-in user. The allowances are
tested as carefully as the refusals — the rate-sourcing desk, a sourcing
owner, a team member and operations must keep working.
"""
import os
import sys
import tempfile
from datetime import date

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'DealScopeTestOnly12345')
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'dealscope.db'))

from app import (app as flask_app, db, Employee, Lead, Company,  # noqa: E402
                 Contact, Opportunity, OverseasAgent)
import app.models.rfq                                            # noqa: E402,F401
from app.models.rbac import Role, SEED_ROLES                     # noqa: E402
from app.access.service import set_profile                       # noqa: E402
from app.models.access import DataScope                          # noqa: E402
from app.models.quote import Quote                               # noqa: E402
from app.models.rfq import RFQ, RateSourcingLine                 # noqa: E402
from app.models.tms_handover import WonHandover                  # noqa: E402

flask_app.config['WTF_CSRF_ENABLED'] = False
BASE = ['module.rfq', 'module.quotes', 'module.handovers']


@pytest.fixture()
def world():
    made = {}
    with flask_app.app_context():
        db.create_all()
        for key, name, desc in SEED_ROLES:
            if Role.query.filter_by(key=key).first() is None:
                db.session.add(Role(key=key, name=name, description=desc,
                                    is_system=True, is_active=True))
        db.session.commit()
        for code, dept, role in (('DSONE', 'Sales', 'user'),
                                 ('DSTWO', 'Sales', 'user'),
                                 ('DSRATE', 'Pre-Sales', 'Rate_Sourcing'),
                                 ('DSOPS', 'Operations', 'user'),
                                 ('DSADM', 'Admin', 'admin')):
            e = Employee.query.filter_by(emp_code=code).first()
            if e is None:
                e = Employee(emp_code=code, name=code)
                db.session.add(e)
            e.role, e.department, e.is_active = role, dept, True
            e.must_change_pw, e.vertical, e.is_super_admin = False, 'All', False
            e.session_version = 0
        db.session.commit()
        for code in ('DSONE', 'DSTWO', 'DSRATE', 'DSOPS'):
            set_profile(code, DataScope.OWN, BASE, actor='DSADM')
        set_profile('DSADM', DataScope.ALL, BASE + ['admin.master'],
                    actor='DSADM')

        acct = Company(name='Deal Scope Two Ltd', is_active=True,
                       pic_emp_code='DSTWO')
        db.session.add(acct)
        db.session.flush()
        lead = Lead(company=acct.name, company_id=acct.id, source='manual',
                    assigned_to='DSTWO', stage='Quoted')
        db.session.add(lead)
        db.session.flush()
        opp = Opportunity(opp_number=f'DS-{lead.id}', lead_id=lead.id,
                          company_id=acct.id, owner_emp_code='DSTWO',
                          stage='Won', value_inr=5_500_000)
        db.session.add(opp)
        db.session.flush()
        rfq = RFQ(rfq_number=f'RFQ-DS-{lead.id}', subject='Secret move',
                  lead_id=lead.id, opportunity_id=opp.id, account_id=acct.id,
                  lead_driver='DSTWO', received_date=date.today())
        db.session.add(rfq)
        db.session.flush()
        quote = Quote(quote_number=f'Q-DS-{lead.id}', rfq_id=rfq.id,
                      opportunity_id=opp.id, lead_id=lead.id,
                      account_id=acct.id, prepared_by_id='DSTWO',
                      subject='Secret quote', total_amount=5_500_000)
        handover = WonHandover(opportunity_id=opp.id, account_id=acct.id,
                               pic_emp_code='DSTWO', won_value=5_500_000,
                               status='Handover Pending')
        contact = Contact(name='Buyer Contact', company='Deal Scope Two Ltd',
                          company_id=acct.id, assigned_to='DSTWO',
                          email='buyer.contact@dealscope.example')
        agent = OverseasAgent(name='Deal Scope Agent', is_active=True)
        db.session.add_all([quote, handover, contact, agent])
        db.session.commit()
        made = dict(acct=acct.id, lead=lead.id, opp=opp.id, rfq=rfq.id,
                    quote=quote.id, handover=handover.id,
                    contact=contact.id, agent=agent.id)
    yield made
    with flask_app.app_context():
        from app.models.rbac import DealMember
        DealMember.query.filter_by(opportunity_id=made['opp']).delete()
        RateSourcingLine.query.filter_by(rfq_id=made['rfq']).delete()
        WonHandover.query.filter_by(id=made['handover']).delete()
        Quote.query.filter_by(id=made['quote']).delete()
        RFQ.query.filter_by(id=made['rfq']).delete()
        Opportunity.query.filter_by(id=made['opp']).delete()
        Contact.query.filter_by(id=made['contact']).delete()
        Lead.query.filter_by(id=made['lead']).delete()
        Company.query.filter_by(id=made['acct']).delete()
        OverseasAgent.query.filter_by(id=made['agent']).delete()
        db.session.commit()


def _c(code, role='user'):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role=role, vertical='All', sv=0)
    return c


# ── refusals ─────────────────────────────────────────────────────────
@pytest.mark.parametrize('method,path', [
    ('get', '/api/rfqs/{rfq}'),
    ('patch', '/api/rfqs/{rfq}'),
    ('post', '/api/rfqs/{rfq}/advance'),
    ('get', '/rfqs/{rfq}'),
    ('get', '/api/quotes/{quote}'),
    ('patch', '/api/quotes/{quote}'),
    ('post', '/api/quotes/{quote}/won'),
    ('get', '/quotes/{quote}/print'),
    ('get', '/quotes/{quote}/pdf'),
    ('get', '/api/handovers/{handover}'),
    ('patch', '/api/handovers/{handover}'),
    ('post', '/api/handovers/{handover}/cancel'),
    ('get', '/api/contacts/{contact}'),
])
def test_another_owners_record_is_not_reachable_by_id(world, method, path):
    r = getattr(_c('DSONE'), method)(path.format(**world), json={})
    assert r.status_code in (403, 404), (path, r.status_code)
    assert b'Secret' not in r.data and b'5500000' not in r.data


def test_the_lists_agree_with_the_links(world):
    c = _c('DSONE')
    assert world['rfq'] not in {x['id'] for x in
                                c.get('/api/rfqs').get_json()['rfqs']}
    assert world['quote'] not in {x['id'] for x in
                                  c.get('/api/quotes').get_json()['quotes']}
    assert world['handover'] not in {x['id'] for x in c.get(
        '/api/handovers?status=all').get_json()['handovers']}
    assert world['contact'] not in {x['id'] for x in
                                    c.get('/api/contacts').get_json()}


def test_a_quote_cannot_be_raised_on_someone_elses_rfq(world):
    r = _c('DSONE').post('/api/quotes', json={'rfq_id': world['rfq']})
    assert r.status_code == 404


def test_someone_elses_account_shows_only_its_owner(world):
    r = _c('DSONE').get(f'/api/companies/{world["acct"]}/360')
    body = r.get_json()
    assert body['access'] == 'routing'
    assert body['people'] == [] and body['opportunities'] == []
    assert body['kpis'] is None
    assert b'buyer.contact' not in r.data


def test_an_account_edit_needs_ownership(world):
    r = _c('DSONE').put(f'/api/companies/{world["acct"]}',
                        json={'name': 'Hijacked'})
    assert r.status_code == 403


def test_an_overseas_agent_edit_needs_master_data_rights(world):
    r = _c('DSONE').put(f'/api/agents/{world["agent"]}',
                        json={'name': 'Hijacked'})
    assert r.status_code == 403


def test_a_contact_cannot_be_edited_or_deleted_by_another_user(world):
    c = _c('DSONE')
    assert c.put(f'/api/contacts/{world["contact"]}',
                 json={'name': 'x'}).status_code == 403
    assert c.delete(f'/api/contacts/{world["contact"]}').status_code == 403


# ── allowances ───────────────────────────────────────────────────────
def test_the_owner_reaches_everything(world):
    c = _c('DSTWO')
    assert c.get(f'/api/rfqs/{world["rfq"]}').status_code == 200
    assert c.get(f'/api/quotes/{world["quote"]}').status_code == 200
    assert c.get(f'/api/handovers/{world["handover"]}').status_code == 200
    assert c.get(f'/api/companies/{world["acct"]}/360').get_json()[
        'access'] == 'full'


def test_the_rate_sourcing_desk_sees_every_rfq(world):
    assert _c('DSRATE').get(f'/api/rfqs/{world["rfq"]}').status_code == 200


def test_a_sourcing_owner_reaches_the_rfq_they_source(world):
    with flask_app.app_context():
        db.session.add(RateSourcingLine(rfq_id=world['rfq'], line_no=1,
                                        service='Road', created_by_id='DSTWO',
                                        sourcing_owner_id='DSONE'))
        db.session.commit()
    assert _c('DSONE').get(f'/api/rfqs/{world["rfq"]}').status_code == 200


def test_a_team_member_reaches_the_deal_documents(world):
    from app.services.assignment import add_member
    with flask_app.app_context():
        opp = db.session.get(Opportunity, world['opp'])
        assert add_member(opp, 'Technical_Support', 'DSONE',
                          assigned_by='DSADM') is not None
        db.session.commit()
    c = _c('DSONE')
    assert c.get(f'/api/rfqs/{world["rfq"]}').status_code == 200
    assert c.get(f'/api/quotes/{world["quote"]}').status_code == 200


def test_operations_works_the_whole_handover_queue(world):
    r = _c('DSOPS').get(f'/api/handovers/{world["handover"]}')
    assert r.status_code == 200


def test_someone_with_a_lead_on_the_account_sees_their_part(world):
    with flask_app.app_context():
        mine = Lead(company='Deal Scope Two Ltd', company_id=world['acct'],
                    source='manual', assigned_to='DSONE', stage='New')
        db.session.add(mine)
        db.session.commit()
        mine_id = mine.id
    try:
        body = _c('DSONE').get(f'/api/companies/{world["acct"]}/360') \
            .get_json()
        assert body['access'] == 'partial'
        assert [l['id'] for l in body['leads']] == [mine_id]
        assert body['opportunities'] == []
        assert body['people'], 'a lead on the account needs its contacts'
        assert world['contact'] in {x['id'] for x in _c('DSONE').get(
            '/api/contacts').get_json()}
    finally:
        with flask_app.app_context():
            Lead.query.filter_by(id=mine_id).delete()
            db.session.commit()


def test_company_wide_scope_sees_and_edits_everything(world):
    c = _c('DSADM', 'admin')
    assert c.get(f'/api/quotes/{world["quote"]}').status_code == 200
    assert c.put(f'/api/agents/{world["agent"]}',
                 json={'city': 'Rotterdam'}).status_code == 200


def test_quote_approval_needs_company_scope_or_the_vertical_head_role(world):
    """The role name 'admin' in a session no longer approves quotes; the
    Access Matrix (company-wide scope) or the Vertical_Head role does."""
    with flask_app.app_context():
        q = db.session.get(Quote, world['quote'])
        q.status, q.prepared_by_id = 'Awaiting Approval', 'DSRATE'
        # An administrator by role whose matrix profile is Own scope. The
        # session guard reads the role from here, so this is what a
        # role-name check would see.
        Employee.query.filter_by(emp_code='DSTWO').first().role = 'admin'
        db.session.commit()
    r = _c('DSTWO', 'admin').post(f'/api/quotes/{world["quote"]}/approve',
                                  json={})
    assert r.status_code == 403
    r = _c('DSADM', 'admin').post(f'/api/quotes/{world["quote"]}/approve',
                                  json={})
    assert r.status_code == 200, r.get_data(as_text=True)[:200]


def test_every_caller_uses_the_one_rfq_quote_handover_rule(world):
    """scope.rfqs/quotes/handovers used to apply only the document's own
    owner column; the Copilot's older answers and the data-quality
    checks used them and hid what the screens show."""
    from app.access import records, scope as scope_mod
    with flask_app.app_context():
        db.session.add(RateSourcingLine(rfq_id=world['rfq'], line_no=1,
                                        service='Road', created_by_id='DSTWO',
                                        sourcing_owner_id='DSONE'))
        db.session.commit()
        sc = scope_mod.for_employee('DSONE')
        assert {r.id for r in scope_mod.rfqs(sc=sc)} == \
            {r.id for r in records.rfqs(sc=sc)}
        assert world['rfq'] in {r.id for r in scope_mod.rfqs(sc=sc)}
        ops = scope_mod.for_employee('DSOPS')
        assert world['handover'] in {h.id for h in scope_mod.handovers(sc=ops)}


def test_the_lead_screen_tells_the_copilot_which_lead_is_open():
    src = open(os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'templates', 'app.html')).read()
    assert "window.ProcamAI.setContext({type:'lead', id:l.id" in src
    assert 'window.ProcamAI.setContext(null)' in src
