"""Deep links from Company 360 into the single-page app.

Company 360 links to /CRM/app?opp=<Opportunity.id> and ?lead=<Lead.id>.
The app had no dispatcher for ?opp= at all, and the lead one it did have
called a handler that reads only the first 500 leads — so most lead links
silently did nothing.

These cover the server side: the fetch-by-id endpoints the dispatcher
depends on, and the fact that an Opportunity id has to be resolved to a
lead before the app's (lead-centric) opportunity view can open.
"""
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'test')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'DeepTestOnly12345')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'deep.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app, db = _main.app, _main.db
Company, Lead, Opportunity, Employee = (_main.Company, _main.Lead,
                                        _main.Opportunity, _main.Employee)

_TEMPLATE = os.path.join(_ROOT, 'templates', 'app.html')


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        Opportunity.query.delete()
        Lead.query.delete()
        Company.query.delete()

        for code, role in (('DLADM', 'admin'), ('DLREP', 'user')):
            e = Employee.query.filter_by(emp_code=code).first()
            if not e:
                e = Employee(emp_code=code, name=code)
                db.session.add(e)
            e.role, e.is_active, e.must_change_pw = role, True, False
            e.is_super_admin, e.vertical = (role == 'admin'), 'All'
        db.session.commit()

        acme = Company(name='Acme', is_active=True)
        db.session.add(acme)
        db.session.flush()

        mine = Lead(company='Acme', company_id=acme.id, stage='New',
                    assigned_to='DLREP')
        theirs = Lead(company='Acme', company_id=acme.id, stage='New',
                      assigned_to='DLADM')
        db.session.add_all([mine, theirs])
        db.session.flush()

        linked = Opportunity(opp_number='D-1', company_id=acme.id,
                             lead_id=mine.id, stage='Won', value_inr=1)
        orphan = Opportunity(opp_number='D-2', company_id=acme.id,
                             stage='Won', value_inr=1)
        db.session.add_all([linked, orphan])
        db.session.commit()
        return {'mine': mine.id, 'theirs': theirs.id,
                'linked': linked.id, 'orphan': orphan.id}


def _c(code='DLADM'):
    c = flask_app.test_client()
    with flask_app.app_context():
        role = Employee.query.filter_by(emp_code=code).first().role
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role=role, vertical='All')
    return c


# ── fetch-by-id, which the list endpoint's 500 cap made necessary ────
def test_lead_by_id_returns_the_record(world):
    body = _c().get(f'/api/leads/{world["mine"]}').get_json()
    assert body['id'] == world['mine']


def test_unknown_lead_is_404_not_an_empty_success(world):
    r = _c().get('/api/leads/999999999')
    assert r.status_code == 404
    assert 'not found' in r.get_json()['error'].lower()


def test_lead_by_id_respects_the_same_scope_as_the_list(world):
    """It must not become a way around the list's visibility rules."""
    r = _c('DLREP').get(f'/api/leads/{world["theirs"]}')
    assert r.status_code == 404, \
        'a lead the list hides was reachable by id'


# ── an Opportunity id is not a Lead id ───────────────────────────────
def test_opportunity_by_id_reports_its_lead(world):
    body = _c().get(f'/api/opportunities/{world["linked"]}').get_json()
    assert body['id'] == world['linked']
    assert body['lead_id'] == world['mine']
    assert body['lead_visible'] is True


def test_an_opportunity_with_no_lead_says_so(world):
    """4,264 opportunities imported in 2023 have no lead. The app's
    opportunity view edits a LEAD, so those cannot open — and the link
    must say that rather than appear broken."""
    body = _c().get(f'/api/opportunities/{world["orphan"]}').get_json()
    assert body['lead_id'] is None
    assert body['lead_visible'] is False


def test_lead_visibility_is_reported_per_user(world):
    body = _c('DLREP').get(
        f'/api/opportunities/{world["linked"]}').get_json()
    assert body['lead_visible'] is True

    with flask_app.app_context():
        other = Lead(company='Acme', stage='New', assigned_to='DLADM')
        db.session.add(other)
        db.session.flush()
        opp = Opportunity(opp_number='D-3', lead_id=other.id, stage='Won')
        db.session.add(opp)
        db.session.commit()
        opp_id = opp.id

    body = _c('DLREP').get(f'/api/opportunities/{opp_id}').get_json()
    assert body['lead_visible'] is False, \
        'the deep link must not offer to open a lead the user cannot see'


def test_unknown_opportunity_is_404(world):
    assert _c().get('/api/opportunities/999999999').status_code == 404


# ── the dispatcher exists and behaves ────────────────────────────────
def test_the_app_dispatches_deep_links_on_startup():
    html = open(_TEMPLATE).read()
    assert 'async function openDeepLink()' in html
    assert 'await openDeepLink();' in html, \
        'the dispatcher must be invoked from the init sequence'
    # after the default dashboard render, per the view-swap constraint
    assert html.index("nav('dash');") < html.index('await openDeepLink();')


def test_the_dispatcher_handles_both_parameters():
    """Part A's two, still routed after Part B widened the loop to five."""
    html = open(_TEMPLATE).read()
    assert "'opp', 'lead'" in html
    assert 'openOppDeepLink' in html and 'openLeadDeepLink' in html


def test_a_bad_id_never_falls_back_to_the_dashboard():
    html = open(_TEMPLATE).read()
    assert 'function deepLinkNotFound(' in html
    assert 'Record not found' in html


def test_the_query_parameter_is_not_stripped():
    """Refresh and Back must keep showing the record."""
    html = open(_TEMPLATE).read()
    assert "history.replaceState(null, '', window.location.pathname)" \
        not in html, 'stripping the parameter breaks refresh and Back'


def test_the_handlers_fall_back_to_fetch_by_id():
    """openLd read only the first 500 leads and returned silently."""
    html = open(_TEMPLATE).read()
    assert html.count("const one=await api('/api/leads/'+id);") == 2, \
        'both openLd and openOpp need the single-record fallback'


def test_home_links_are_untouched():
    """The brand and Back to CRM go to app home with no parameter."""
    html = open(_TEMPLATE).read()
    assert 'crm-brand' not in html or '/app?' not in html.split(
        'crm-brand')[1][:200]


# ── Part B: accounts, companies and contacts ─────────────────────────
def test_contact_by_id_returns_the_record(world):
    with flask_app.app_context():
        acme_id = Company.query.filter_by(name='Acme').first().id
        ct = _main.Contact(name='Rita Shah', company='Acme',
                           company_id=acme_id, assigned_to='DLADM',
                           email='rita@acme.com')
        db.session.add(ct)
        db.session.commit()
        cid = ct.id

    body = _c().get(f'/api/contacts/{cid}').get_json()
    assert body['id'] == cid
    assert body['name'] == 'Rita Shah'
    assert body['company_id'] == acme_id
    assert body['company_name'] == 'Acme', \
        'the detail needs a route through to the organisation'


def test_unknown_contact_is_404(world):
    assert _c().get('/api/contacts/999999999').status_code == 404


def test_contact_by_id_respects_the_list_visibility_rule(world):
    """It must not become a way past the owner filter the list applies."""
    with flask_app.app_context():
        theirs = _main.Contact(name='Someone Else', assigned_to='DLADM')
        db.session.add(theirs)
        db.session.commit()
        cid = theirs.id
    assert _c('DLREP').get(f'/api/contacts/{cid}').status_code == 404


def test_the_dispatcher_covers_all_five_parameters():
    html = open(_TEMPLATE).read()
    assert "['opp', 'lead', 'account', 'company', 'contact']" in html
    for fn in ('openAccountDeepLink', 'openCompanyDeepLink',
               'openContactDeepLink'):
        assert f'async function {fn}(' in html


def test_there_is_an_id_based_contact_handler():
    """openCt() takes no argument and opens the blank new-contact modal,
    so it cannot be reused for this."""
    html = open(_TEMPLATE).read()
    assert 'async function openContact(id, preloaded)' in html
    assert 'function openCt(){' in html, 'the zero-arg handler must remain'
    assert 'openCt(id)' not in html, 'openCt must not be repurposed'


def test_not_found_names_the_right_view_for_each_kind():
    html = open(_TEMPLATE).read()
    assert "account: 'accounts'" in html
    assert "company: 'projects'" in html
    assert "contact: 'contacts'" in html


def test_company_360_deep_links_contact_names():
    tpl = os.path.join(_ROOT, 'templates', 'company', 'detail.html')
    src = open(tpl).read()
    assert '/app?contact={{ p.id }}' in src, \
        'contact names must link into the app'
    assert 'mailto:{{ p.email }}' in src, \
        'the mailto on the email is a different action and stays'


def test_company_360_link_style_is_consistent():
    """Same tab, same markup as the working ?opp= / ?lead= links."""
    src = open(os.path.join(_ROOT, 'templates', 'company',
                            'detail.html')).read()
    for param in ('opp', 'lead', 'contact'):
        assert f'href="{{{{ url_prefix }}}}/app?{param}=' in src
    assert 'target="_blank"' not in src.split('?contact=')[0][-300:], \
        'contact links should open in the same tab, like the others'
