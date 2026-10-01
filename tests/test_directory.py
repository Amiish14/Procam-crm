"""Global CRM: what a search may return, who may read one person's
relationship, and what it takes to change who looks after them.

These tests are also the record of two decisions that are easy to undo
by accident:

  * a contact outside your scope answers 404, never 403, because a 403
    confirms that a named person at a named company exists;
  * who-handles is NOT scope-filtered. Anyone may learn that an
    organisation is already a Procam account and who owns it — that is
    the whole purpose of the Group D directory — while everything beyond
    the routing answer still goes through company_access().
"""
import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'directory-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'DirectoryTest12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'directory.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Contact, Employee, Lead = (_main.Company, _main.Contact,
                                    _main.Employee, _main.Lead)
LeadActivity, LeadEmail = _main.LeadActivity, _main.LeadEmail
flask_app.config['WTF_CSRF_ENABLED'] = False

from app.access import scope as sc_mod                    # noqa: E402
from app.directory import service as svc                  # noqa: E402
from app.directory.routes import bp as directory_bp       # noqa: E402
from app.models.access import AccessProfile, DataScope     # noqa: E402
from presales.models import AccountActivity                # noqa: E402

# app.py does not register this blueprint yet (that is the one change
# outside this stream's files), so the tests register it themselves.
if 'directory' not in flask_app.blueprints:
    flask_app.register_blueprint(directory_bp)

REP, OTHER, HEAD = 'DIRREP', 'DIROTHER', 'DIRHEAD'
V_MINE, V_THEIRS = 'Project Freight', 'Break Bulk'
NOW = datetime.utcnow()


def _emp(code, name, vertical, scope, perms=(), head=False):
    e = Employee.query.filter_by(emp_code=code).first() or Employee(emp_code=code)
    e.name, e.vertical, e.is_active, e.must_change_pw = name, vertical, True, False
    e.role, e.is_super_admin, e.is_vertical_head = 'user', False, head
    e.session_version, e.email = 0, f'{code.lower()}@example.test'
    db.session.add(e)
    db.session.flush()
    AccessProfile.query.filter_by(emp_code=code).delete()
    db.session.add(AccessProfile(emp_code=code, data_scope=scope,
                                 perms=list(perms)))
    return e


def _company(name, pic, vertical, **kw):
    c = Company(name=name, pic_emp_code=pic, vertical=vertical,
                is_active=True)
    for k, v in kw.items():
        setattr(c, k, v)
    db.session.add(c)
    return c


def _contact(name, account, **kw):
    c = Contact(name=name, account_id=account.id, company_id=account.id,
                is_active=True)
    for k, v in kw.items():
        setattr(c, k, v)
    db.session.add(c)
    return c


def _master(list_key, *codes):
    from app.master_data import service as md
    md.ensure_lists()
    for code in codes:
        md.add_item(list_key, code, label=code, actor='test')


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        svc.ensure_tables()
        from app.directory.models import ContactAssignment, ContactRelationship

        # Only ever clear this module's own rows: the suite shares one
        # database, and deleting everything wipes other modules' worlds.
        mine = [c.id for c in Company.query.filter(Company.name.like('DIR %'))]
        if mine:
            leads = [l.id for l in Lead.query.filter(Lead.company_id.in_(mine))]
            if leads:
                LeadActivity.query.filter(LeadActivity.lead_id.in_(leads)).delete(
                    synchronize_session=False)
                LeadEmail.query.filter(LeadEmail.lead_id.in_(leads)).delete(
                    synchronize_session=False)
                Lead.query.filter(Lead.id.in_(leads)).delete(
                    synchronize_session=False)
            contacts = [c.id for c in Contact.query.filter(
                Contact.company_id.in_(mine))]
            if contacts:
                AccountActivity.query.filter(
                    AccountActivity.contact_id.in_(contacts)).delete(
                        synchronize_session=False)
                ContactRelationship.query.filter(
                    ContactRelationship.contact_id.in_(contacts)).delete(
                        synchronize_session=False)
                ContactAssignment.query.filter(
                    ContactAssignment.contact_id.in_(contacts)).delete(
                        synchronize_session=False)
                Contact.query.filter(Contact.id.in_(contacts)).delete(
                    synchronize_session=False)
            AccountActivity.query.filter(
                AccountActivity.account_id.in_(mine)).delete(
                    synchronize_session=False)
            Company.query.filter(Company.id.in_(mine)).delete(
                synchronize_session=False)
        db.session.commit()

        _master('vertical', V_MINE, V_THEIRS)
        _master('relationship', 'Customer', 'Prospect', 'Vendor', 'Partner')
        _master('service', 'Project Cargo')

        _emp(REP, 'Directory Rep', V_MINE, DataScope.OWN)
        _emp(OTHER, 'Other Desk Rep', V_THEIRS, DataScope.OWN)
        _emp(HEAD, 'Vertical Head', V_MINE, DataScope.VERTICAL, head=True,
             perms=['accounts.assign'])
        db.session.flush()

        group = _company('DIR Holdings Group', REP, V_MINE, city='Mumbai',
                         country='India')
        db.session.flush()
        mine_acct = _company('DIR Steelworks Limited', REP, V_MINE,
                             industry='Steel', city='Chennai', state='Tamil Nadu',
                             country='India', parent_account_id=group.id,
                             email_domains=['dirsteel.com'])
        theirs = _company('DIR Rival Shipping Limited', OTHER, V_THEIRS,
                          industry='Shipping', city='Kolkata',
                          country='India')
        db.session.flush()

        people = {
            # The rep's own account. Everything the filters are tested on.
            'mine': _contact('DIR Priya Procurement', mine_acct,
                             designation='Head of Procurement',
                             email='priya@dirsteel.com', phone='+914400001',
                             industry='Steel', country='India', city='Chennai',
                             company='DIR Steel Trichy', assigned_to=REP,
                             agent_type='Customer'),
            # Same account, never spoken to, missing almost everything.
            'cold': _contact('DIR Quiet Person', mine_acct, assigned_to=REP),
            # Another desk's account: the rep must never see this one.
            'theirs': _contact('DIR Rival Buyer', theirs,
                               designation='Chartering Manager',
                               email='buyer@dirrival.test', city='Kolkata',
                               country='India', assigned_to=OTHER,
                               agent_type='Prospect'),
        }
        db.session.flush()

        lead = Lead(company='DIR Steelworks Limited', company_id=mine_acct.id,
                    assigned_to=REP, assigned_name=REP, stage='Quoted',
                    procam_vertical=V_MINE, products='Project Cargo movement',
                    next_action='Send the revised rate',
                    followup_date=date.today() + timedelta(days=4),
                    email='priya@dirsteel.com')
        db.session.add(lead)
        db.session.flush()
        db.session.add(LeadActivity(lead_id=lead.id, kind='call',
                                    subject='Spoke about the rates',
                                    occurred_at=NOW - timedelta(days=2),
                                    performed_by=REP))
        db.session.add(LeadEmail(lead_id=lead.id, direction='outbound',
                                 subject='Revised rates',
                                 sent_or_received_at=NOW - timedelta(days=3),
                                 created_by=REP))
        db.session.add(AccountActivity(
            account_id=mine_acct.id, contact_id=people['mine'].id,
            kind='Meeting', subject='Plant visit',
            occurred_at=NOW - timedelta(days=5), performed_by=REP,
            next_action='Send the site survey', next_action_at=date.today()))
        # A long-cold person at the other desk, so idle counts have
        # something to find on the head's side of the fence.
        db.session.add(AccountActivity(
            account_id=theirs.id, contact_id=people['theirs'].id,
            kind='Email', subject='Old thread',
            occurred_at=NOW - timedelta(days=400), performed_by=OTHER))
        db.session.commit()

        return {'accounts': {'mine': mine_acct.id, 'theirs': theirs.id,
                             'group': group.id},
                'contacts': {k: v.id for k, v in people.items()},
                'lead': lead.id}


def _sc(code):
    return sc_mod.for_employee(code)


def _client(code, vertical=V_MINE):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role='user', vertical=vertical, sv=0)
    return c


def _search(code, **kw):
    with flask_app.app_context():
        return svc.search_contacts(_sc(code), **kw)


def _ids(result):
    return {i['id'] for i in result['items']}


# ── scope ────────────────────────────────────────────────────────────
def test_a_rep_never_sees_another_verticals_contacts(world):
    found = _ids(_search(REP, per_page=200))
    assert world['contacts']['mine'] in found
    assert world['contacts']['cold'] in found
    assert world['contacts']['theirs'] not in found


def test_a_vertical_head_reaches_their_whole_desk_only(world):
    # The head's scope covers their own vertical's people, which is REP.
    found = _ids(_search(HEAD, per_page=200))
    assert world['contacts']['mine'] in found
    assert world['contacts']['theirs'] not in found


def test_an_unscoped_search_cannot_be_widened_by_a_filter(world):
    """Every filter narrows. None of them can reach outside the boundary."""
    theirs = world['contacts']['theirs']
    for kw in ({'q': 'DIR Rival Buyer'}, {'company': 'Rival'},
               {'city': 'Kolkata'}, {'pic': OTHER},
               {'account_id': world['accounts']['theirs']},
               {'vertical': V_THEIRS}, {'designation': 'Chartering'}):
        assert theirs not in _ids(_search(REP, per_page=200, **kw)), kw


# ── the filters ──────────────────────────────────────────────────────
def test_free_text_searches_name_email_phone_and_company(world):
    mine = world['contacts']['mine']
    for term in ('Priya', 'priya@dirsteel.com', '4400001', 'Steelworks',
                 'Trichy'):
        assert mine in _ids(_search(REP, q=term)), term


def test_each_named_filter_narrows_in_sql(world):
    mine, cold = world['contacts']['mine'], world['contacts']['cold']

    assert _ids(_search(REP, company='Steelworks')) >= {mine, cold}
    assert _ids(_search(REP, designation='Procurement')) == {mine}
    assert _ids(_search(REP, industry='Steel')) >= {mine}
    assert _ids(_search(REP, country='India')) >= {mine}
    assert _ids(_search(REP, city='Chennai')) >= {mine}
    # The contact has no vertical of its own; the account's is inherited.
    assert _ids(_search(REP, vertical=V_MINE)) >= {mine, cold}
    assert _ids(_search(REP, vertical=V_THEIRS)) == set()
    assert _ids(_search(REP, account_id=world['accounts']['mine'])) == {mine, cold}
    assert _ids(_search(REP, pic=REP)) >= {mine, cold}
    assert _ids(_search(REP, pic=OTHER)) == set()
    # relationship falls back to the legacy agent_type until someone sets one
    assert _ids(_search(REP, relationship='Customer')) == {mine}
    # a service is something the account asks us to move, not a column
    # on a person: the lead's products is where it is written down
    assert _ids(_search(REP, service='Project Cargo')) == {mine, cold}
    assert _ids(_search(REP, service='Reefer')) == set()


def test_an_unparseable_account_id_matches_nothing_rather_than_everything(world):
    assert _ids(_search(REP, account_id='; drop table contacts')) == set()


def test_last_interaction_reads_both_ways(world):
    mine, cold = world['contacts']['mine'], world['contacts']['cold']
    # the lead call was two days ago, and it rolls up to the account
    assert mine in _ids(_search(REP, last_interaction_days=7))
    assert _ids(_search(REP, last_interaction_days=1)) == set()
    # negative asks the opposite question: who has gone quiet
    stale = _ids(_search(REP, last_interaction_days=-90))
    assert cold in stale and mine not in stale


def test_the_last_interaction_column_is_the_shared_definition(world):
    row = next(i for i in _search(REP, per_page=200)['items']
               if i['id'] == world['contacts']['mine'])
    # the newest of the lead call (2d), the lead email (3d) and the
    # meeting logged against this person (5d)
    assert row['last_interaction_days'] == 2
    cold = next(i for i in _search(REP, per_page=200)['items']
                if i['id'] == world['contacts']['cold'])
    # a colleague at the same employer being rung is not contact with
    # this person, so they are still "never contacted"
    assert cold['last_interaction'] == '' and cold['last_interaction_days'] is None


def test_paging_reports_the_full_total_and_slices_it(world):
    everything = _search(REP, per_page=200)
    assert everything['total'] == 2
    first = _search(REP, per_page=1, page=1)
    second = _search(REP, per_page=1, page=2)
    assert first['total'] == second['total'] == 2
    assert first['pages'] == 2
    assert len(first['items']) == len(second['items']) == 1
    assert _ids(first).isdisjoint(_ids(second))
    assert _ids(first) | _ids(second) == _ids(everything)


def test_an_oversized_page_is_capped_not_refused(world):
    assert _search(REP, per_page=99999)['per_page'] == svc.MAX_PER_PAGE


# ── dashboard ────────────────────────────────────────────────────────
def test_the_dashboard_counts_only_what_the_viewer_may_see(world):
    with flask_app.app_context():
        rep = svc.dashboard(_sc(REP))
        other = svc.dashboard(_sc(OTHER))
    assert rep['total'] == 2 and other['total'] == 1
    assert rep['customers'] == 1
    assert other['prospects'] == 1 and other['customers'] == 0
    assert {b['key'] for b in rep['by_vertical']} == {V_MINE}
    # the quiet contact has no country of its own; the account's shows through
    assert {b['key'] for b in rep['by_country']} == {'India'}
    assert rep['never_contacted']['count'] == 1
    assert world['contacts']['cold'] in {r['id'] for r
                                         in rep['never_contacted']['rows']}
    assert rep['recently_added']['count'] == 2
    assert other['idle_90']['count'] == 1


def test_missing_information_names_what_is_missing(world):
    with flask_app.app_context():
        miss = svc.dashboard(_sc(REP))['missing_information']
    assert miss['total'] >= 1
    assert miss['breakdown']['no_email'] == 1
    assert miss['breakdown']['no_designation'] == 1
    assert miss['breakdown']['no_relationship'] == 1
    # both contacts have an account and a PIC
    assert miss['breakdown']['no_account'] == 0
    assert miss['breakdown']['no_pic'] == 0


# ── one person ───────────────────────────────────────────────────────
def test_the_relationship_view_gathers_the_whole_relationship(world):
    with flask_app.app_context():
        view = svc.relationship_view(_sc(REP), world['contacts']['mine'])
    assert view['contact']['name'] == 'DIR Priya Procurement'
    assert view['contact']['designation'] == 'Head of Procurement'
    assert view['contact']['email'] == 'priya@dirsteel.com'
    assert view['account']['name'] == 'DIR Steelworks Limited'
    assert view['account']['access'] == 'full'
    assert view['ownership']['pic'] == REP
    assert view['ownership']['vertical'] == V_MINE
    assert [l['id'] for l in view['leads']] == [world['lead']]
    assert view['leads'][0]['value'] == '—'          # nothing quoted yet
    assert [m['subject'] for m in view['meetings']] == ['Plant visit']
    assert view['last_interaction_days'] == 2
    # the timeline is the three sources behind that one figure
    assert {i['source'] for i in view['interactions']} == {'account', 'lead',
                                                           'email'}
    assert view['next_action']['what'] in ('Send the site survey',
                                           'Send the revised rate')


def test_a_contact_outside_scope_is_absent_not_forbidden(world):
    with flask_app.app_context():
        assert svc.relationship_view(_sc(REP), world['contacts']['theirs']) is None
        assert svc.relationship_view(_sc(REP), 10 ** 9) is None
    r = _client(REP).get(f"/api/directory/contact/{world['contacts']['theirs']}")
    assert r.status_code == 404
    assert _client(REP).get('/api/directory/contact/999999999').status_code == 404


def test_a_partial_reader_gets_the_account_name_and_not_its_detail(world):
    """OTHER has a lead on the rep's account, so company_access() says
    partial: their own work and this person, never the account's diary."""
    with flask_app.app_context():
        lead = Lead(company='DIR Steelworks Limited',
                    company_id=world['accounts']['mine'], assigned_to=OTHER,
                    assigned_name=OTHER, stage='New Opportunity',
                    procam_vertical=V_THEIRS)
        db.session.add(lead)
        db.session.commit()
        view = svc.relationship_view(_sc(OTHER), world['contacts']['mine'])
        assert view is not None
        assert view['account']['access'] == 'partial'
        assert view['account']['name'] == 'DIR Steelworks Limited'
        assert view['account']['stage'] == ''
        # the rep's lead is not theirs to see; their own is
        assert [l['owner'] for l in view['leads']] == [OTHER]
        # the plant visit was logged against this person, so it stays
        assert [m['subject'] for m in view['meetings']] == ['Plant visit']
        db.session.delete(lead)
        db.session.commit()


# ── assignment ───────────────────────────────────────────────────────
def _assign(client, cid, **body):
    return client.post(f'/api/directory/contact/{cid}/assign',
                       data=json.dumps(body),
                       content_type='application/json')


def test_assignment_needs_the_accounts_assign_permission(world):
    cid = world['contacts']['mine']
    r = _assign(_client(REP), cid, primary_pic=HEAD, reason='Handover')
    assert r.status_code == 403
    assert r.get_json()['need'] == 'accounts.assign'
    with flask_app.app_context():
        assert db.session.get(Contact, cid).assigned_to == REP
        from app.directory.models import ContactAssignment
        assert ContactAssignment.query.filter_by(contact_id=cid).count() == 0


def test_assignment_writes_the_history_and_the_audit_trail(world):
    cid = world['contacts']['mine']
    r = _assign(_client(HEAD), cid, primary_pic=HEAD, secondary_pic=REP,
                vertical=V_MINE, relationship_type='Customer',
                reason='Rep is on leave for a month')
    assert r.status_code == 200 and r.get_json()['changed']
    with flask_app.app_context():
        from app.directory.models import ContactAssignment, ContactRelationship
        from app.models.audit import AuditEvent
        contact = db.session.get(Contact, cid)
        assert contact.assigned_to == HEAD
        rel = ContactRelationship.query.filter_by(contact_id=cid).first()
        assert rel.secondary_pic == REP and rel.relationship_type == 'Customer'
        hist = ContactAssignment.query.filter_by(contact_id=cid).first()
        assert hist.previous_pic_code == REP and hist.new_pic_code == HEAD
        assert hist.reason == 'Rep is on leave for a month'
        assert hist.changes['new']['primary_pic'] == HEAD
        ev = (AuditEvent.query.filter_by(action='directory.contact.assign',
                                         entity_id=str(cid))
              .order_by(AuditEvent.id.desc()).first())
        assert ev is not None and ev.reason == 'Rep is on leave for a month'
        assert ev.new_value['primary_pic'] == HEAD


def test_assignment_refuses_a_reasonless_or_invalid_change(world):
    cid = world['contacts']['mine']
    c = _client(HEAD)
    assert _assign(c, cid, primary_pic=HEAD).status_code == 400
    assert _assign(c, cid, primary_pic='NOBODY',
                   reason='x').status_code == 400
    # the vocabularies come from Master Data, so an unlisted value is refused
    assert _assign(c, cid, vertical='Made Up Desk',
                   reason='x').status_code == 400
    assert _assign(c, cid, relationship_type='Frenemy',
                   reason='x').status_code == 400
    assert _assign(c, cid, primary_pic=HEAD, secondary_pic=HEAD,
                   reason='x').status_code == 400
    with flask_app.app_context():
        assert db.session.get(Contact, cid).assigned_to == REP


def test_assignment_cannot_reach_a_contact_outside_your_scope(world):
    r = _assign(_client(HEAD), world['contacts']['theirs'],
                primary_pic=HEAD, reason='Taking it over')
    assert r.status_code == 404
    with flask_app.app_context():
        assert db.session.get(Contact,
                              world['contacts']['theirs']).assigned_to == OTHER


# ── Group D: who handles this account? ───────────────────────────────
def test_who_handles_answers_by_name_domain_alias_city_and_group(world):
    with flask_app.app_context():
        sc = _sc(REP)
        for term in ('Steelworks', '@dirsteel.com', 'dirsteel.com',
                     'Trichy', 'Chennai', 'Tamil Nadu', 'Steel',
                     'DIR Holdings', 'Directory Rep', V_MINE):
            names = [m['name'] for m in svc.who_handles(sc, term)['matches']]
            assert 'DIR Steelworks Limited' in names, term


def test_who_handles_names_the_owner_and_counts_the_live_work(world):
    with flask_app.app_context():
        from app import Opportunity
        db.session.add(Opportunity(opp_number='DIR-OPP-1',
                                   company_id=world['accounts']['mine'],
                                   title='Reactor move', stage='RFQ',
                                   owner_emp_code=REP))
        db.session.commit()
        match = next(m for m in svc.who_handles(_sc(REP), 'Steelworks')['matches']
                     if m['account_id'] == world['accounts']['mine'])
    assert [o['emp_code'] for o in match['owners']] == [REP]
    assert match['owners'][0]['name'] == 'Directory Rep'
    assert match['group'] == 'DIR Holdings Group'
    assert match['active_opportunities'] == 1
    assert match['last_interaction']


def test_who_handles_routes_everyone_but_opens_detail_only_to_a_claim(world):
    """§5.1: who owns an account is not a secret. What they are doing on
    it is, until company_access() says otherwise."""
    with flask_app.app_context():
        match = next(m for m in svc.who_handles(_sc(OTHER), 'Steelworks')['matches']
                     if m['account_id'] == world['accounts']['mine'])
    assert match['access'] == 'routing'
    assert [o['emp_code'] for o in match['owners']] == [REP]
    assert 'active_opportunities' in match
    assert 'opportunities' not in match
    with flask_app.app_context():
        mine = next(m for m in svc.who_handles(_sc(REP), 'Steelworks')['matches']
                    if m['account_id'] == world['accounts']['mine'])
    assert mine['access'] == 'full' and 'opportunities' in mine


def test_who_handles_says_nothing_when_asked_nothing(world):
    with flask_app.app_context():
        assert svc.who_handles(_sc(REP), '  ')['matches'] == []


# ── the routes ───────────────────────────────────────────────────────
def test_every_route_needs_a_session(world):
    anon = flask_app.test_client()
    for path in ('/api/directory/contacts', '/api/directory/dashboard',
                 f"/api/directory/contact/{world['contacts']['mine']}",
                 '/api/accounts/who-handles?q=steel'):
        assert anon.get(path).status_code == 401, path
    assert anon.post(f"/api/directory/contact/{world['contacts']['mine']}/assign",
                     data='{}', content_type='application/json'
                     ).status_code == 401
    assert anon.get('/global-crm').status_code in (302, 308)


def test_the_api_returns_the_same_rows_as_the_service(world):
    d = _client(REP).get('/api/directory/contacts?per_page=200').get_json()
    assert d['ok'] and d['scope'] == DataScope.OWN
    assert {i['id'] for i in d['items']} == _ids(_search(REP, per_page=200))
    assert _client(REP).get('/api/directory/dashboard').get_json()['total'] == 2
    who = _client(REP).get('/api/accounts/who-handles?q=Steelworks').get_json()
    assert who['ok'] and who['matches']


def test_the_page_renders_for_a_signed_in_user(world):
    r = _client(REP).get('/global-crm')
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert 'DIR Priya Procurement' in body
    assert 'DIR Rival Buyer' not in body
    assert 'Who handles this account?' in body


def test_the_page_sends_a_csrf_token_with_every_write():
    """Served outside the app shell, which carries its own fetch wrapper.
    Without this the assignment answers "your session expired" for
    everybody."""
    html = open(os.path.join(_ROOT, 'templates', 'directory',
                             'home.html')).read()
    assert "'X-CSRFToken': csrfToken()" in html
    body = html[html.index('<script>'):]
    at = body.index('/assign')
    assert 'postJson(' in body[max(0, at - 400):at + 200]


def test_a_write_without_the_token_is_refused(world):
    """Proof the protection is real, not only present in the markup."""
    was = flask_app.config.get('WTF_CSRF_ENABLED')
    flask_app.config['WTF_CSRF_ENABLED'] = True
    try:
        r = _assign(_client(HEAD), world['contacts']['mine'],
                    primary_pic=HEAD, reason='no token here')
        assert r.status_code in (400, 403)
    finally:
        flask_app.config['WTF_CSRF_ENABLED'] = was


# ── the rules are shared, not copied ─────────────────────────────────
def test_the_directory_reads_the_shared_rules_and_vocabularies():
    src = open(os.path.join(_ROOT, 'app', 'directory', 'service.py')).read()
    assert 'from app.access import scope as scope_mod' in src
    assert 'from app.access import records as rec' in src
    assert 'from app.master_data import service as md' in src
    # no second definition of contact, and no vocabulary written in Python
    assert 'updated_at <' not in src
    for hard_coded in ("'Project Freight'", "'Break Bulk'"):
        assert hard_coded not in src
    routes = open(os.path.join(_ROOT, 'app', 'directory', 'routes.py')).read()
    # the gate scanner recognises this decorator by name
    assert routes.count('@_signed_in') == 6
