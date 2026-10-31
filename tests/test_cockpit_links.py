"""
CRM-COCKPIT-LINKS-01 — the pages existed and nothing led to them.

The Cockpit, the lead journey, the RFQ chain board and the RFQ
reports were all built, switched on, and reachable only by typing
the URL. Five things close that:

    the Cockpit is the landing page after login, and first in both
      menus; the logo returns to it
    the lead pop-up carries a Journey button and a read-only stage
      line
    every lead list row carries a journey icon
    a Cockpit row opens that lead's journey — clicking one did
      nothing at all
    the chain board and the RFQ reports are in both menus

Every link is gated on the feature switch *for the person signed
in*, not the global switch, and that is what most of these tests
hold. Each of those routes answers 404 for anybody its pilot does
not cover, so a menu built on the global answer would offer links
that do not open — the same bug this ticket is about, pointing the
other way.
"""
import importlib
import json
import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'cklinks-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'CkLinksTest-123456')
os.environ.setdefault(
    'DATABASE_URL',
    'sqlite:///' + os.path.join(tempfile.mkdtemp(), 'cklinks.db'))

_main = importlib.import_module('app')
importlib.import_module('app.models')
flask_app = _main.app
flask_app.config['WTF_CSRF_ENABLED'] = False
db = _main.db
Company, Employee, Lead = _main.Company, _main.Employee, _main.Lead

from app.services import flags                            # noqa: E402

PREFIX = 'CK '
USER, PILOT, ADMIN = 'CK_USER', 'CK_PILOT', 'CK_ADM'
VERTICAL = 'Heavy Transport'

#: Every switch these links depend on.
LINK_FLAGS = ('FEATURE_CRM2', 'FEATURE_LEAD_JOURNEY',
              'FEATURE_RFQ_CHAIN', 'FEATURE_RFQ_REPORTS')


def _set(name, state, *, only=()):
    from app.models.integration import AppSetting
    row = AppSetting.query.filter_by(key='flag.' + name).first()
    if row is None:
        row = AppSetting(key='flag.' + name)
        db.session.add(row)
    row.value_json = json.dumps(bool(state))
    db.session.commit()
    flags.limit_to(name, only, actor=ADMIN)
    flags.cache_clear()


def _all(state, *, only=()):
    for name in LINK_FLAGS:
        _set(name, state, only=only)


def _purge():
    from app.models.integration import AppSetting
    Lead.query.filter(Lead.company.like(PREFIX + '%')).delete(
        synchronize_session=False)
    Company.query.filter(Company.name.like(PREFIX + '%')).delete(
        synchronize_session=False)
    Employee.query.filter(Employee.emp_code.like('CK_%')).delete(
        synchronize_session=False)
    AppSetting.query.filter(AppSetting.key.like('flag.%')).delete(
        synchronize_session=False)
    db.session.commit()
    flags.cache_clear()


@pytest.fixture(autouse=True)
def _clean():
    with flask_app.app_context():
        db.create_all()
        _purge()
        for code, name, role in ((USER, 'Link User', 'user'),
                                 (PILOT, 'Link Pilot', 'user'),
                                 (ADMIN, 'Link Director', 'admin')):
            db.session.add(Employee(
                emp_code=code, name=name, password_hash='x', role=role,
                vertical=VERTICAL, is_active=True, must_change_pw=False,
                email=code.lower() + '@procamlogistics.com'))
        db.session.commit()
        yield
        _purge()


def _client(code=USER, role='user'):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s['emp_code'] = code
        s['role'] = role
        s['must_change_pw'] = False
    return c


def _lead(name='Indo Tech', *, owner=USER, stage='New Opportunity'):
    lead = Lead(company=PREFIX + name, stage=stage, source='manual',
                assigned_to=owner, procam_vertical=VERTICAL)
    db.session.add(lead)
    db.session.flush()
    return lead


def _html(path, code=USER, role='user'):
    r = _client(code, role).get(path)
    assert r.status_code == 200, (path, r.status_code)
    return r.get_data(as_text=True)


# ── 1. the landing page ──────────────────────────────────────────────
def test_login_lands_on_the_cockpit():
    with flask_app.app_context():
        _all(True)
    r = _client().get('/')
    assert r.status_code == 302
    assert r.headers['Location'].endswith('/crm2')


def test_both_groups_land_on_the_same_screen():
    """The point of the ticket: an administrator went to /dashboard
    and everybody else to /my-work, so no conversation about "the
    home page" meant one thing."""
    with flask_app.app_context():
        _all(True)
    for code, role in ((USER, 'user'), (ADMIN, 'admin')):
        got = _client(code, role).get('/').headers['Location']
        assert got.endswith('/crm2'), (code, got)


def test_with_the_cockpit_off_the_landing_page_is_what_it_was():
    with flask_app.app_context():
        _all(False)
    assert '/my-work' in _client().get('/').headers['Location']
    # `dashboard` is the single-page app, served at /app.
    assert _client(ADMIN, 'admin').get(
        '/').headers['Location'].endswith('/app')


def test_somebody_outside_the_pilot_does_not_land_on_a_404():
    """The cockpit 404s for anybody the pilot does not cover.
    Landing them there would trade one broken front door for
    another."""
    with flask_app.app_context():
        _all(True, only=[PILOT])
    assert _client(PILOT).get('/').headers['Location'].endswith('/crm2')
    assert '/my-work' in _client(USER).get('/').headers['Location']
    assert _client(USER).get('/crm2').status_code == 404


def test_a_signed_out_visitor_still_goes_to_login():
    with flask_app.app_context():
        _all(True)
    r = flask_app.test_client().get('/')
    assert r.status_code == 302
    assert '/login' in r.headers['Location']


# ── 2. both menus ────────────────────────────────────────────────────
def test_the_cockpit_is_first_in_the_page_menu():
    with flask_app.app_context():
        _all(True)
    html = _html('/my-work')
    nav = html[html.index('<nav aria-label="Sections">'):
               html.index('</nav>')]
    assert '/crm2' in nav
    assert nav.index('/crm2') < nav.index('/my-work')


def test_the_chain_board_and_reports_are_in_the_page_menu():
    with flask_app.app_context():
        _all(True)
    nav = _html('/my-work')
    assert '/rfqs/chain' in nav
    assert '/rfqs/reports' in nav


def test_the_page_menu_offers_nothing_that_would_404():
    with flask_app.app_context():
        _all(False)
    html = _html('/my-work')
    nav = html[html.index('<nav aria-label="Sections">'):
               html.index('</nav>')]
    for href in ('/crm2', '/rfqs/chain', '/rfqs/reports'):
        assert href not in nav, href


def test_the_logo_returns_to_the_cockpit():
    with flask_app.app_context():
        _all(True)
    assert 'class="crm-brand" href="/crm2"' in _html(
        '/my-work').replace('\n', ' ').replace('  ', ' ')


def test_the_logo_falls_back_to_the_app_when_the_cockpit_is_off():
    with flask_app.app_context():
        _all(False)
    html = _html('/my-work')
    brand = html[html.index('class="crm-brand"'):]
    assert '/app' in brand[:120]
    assert '/crm2' not in brand[:120]


def test_the_app_menu_knows_which_links_are_live():
    with flask_app.app_context():
        _all(True)
    html = _html('/app', ADMIN, 'admin')
    assert 'window.CRM_LINKS' in html
    for key in ('cockpit', 'journey', 'chain', 'reports'):
        assert key + ':' in html.replace(' ', '')
    assert 'cockpit:true' in html.replace(' ', '')
    assert "label: 'Cockpit'" in html
    assert "label: 'RFQ Chain Board'" in html
    assert "label: 'RFQ Reports'" in html


def test_the_app_menu_marks_the_links_off_when_they_are_off():
    with flask_app.app_context():
        _all(False)
    flat = _html('/app', ADMIN, 'admin').replace(' ', '')
    assert 'cockpit:false' in flat
    assert 'journey:false' in flat
    assert 'chain:false' in flat
    assert 'reports:false' in flat


def test_the_cockpit_item_is_the_first_of_the_first_group():
    with flask_app.app_context():
        _all(True)
    html = _html('/app', ADMIN, 'admin')
    groups = html[html.index('var TOPNAV_GROUPS = ['):]
    assert groups.index("label: 'Cockpit'") < groups.index("label: 'My Work'")


# ── 3. the lead pop-up and the list rows ─────────────────────────────
def test_the_popup_carries_a_journey_button_and_a_stage_line():
    with flask_app.app_context():
        _all(True)
    html = _html('/app', ADMIN, 'admin')
    assert 'id="ldJny"' in html
    assert 'id="ldStrip"' in html
    assert 'function ldJourneyLink' in html
    assert 'function ldStageStrip' in html
    assert '/api/crm2/leads/' in html


def test_every_lead_list_row_carries_a_journey_icon():
    with flask_app.app_context():
        _all(True)
    html = _html('/app', ADMIN, 'admin')
    assert 'function journeyIcon' in html
    # My Leads, All Leads, Sales Pipeline, and the v2 list.
    assert html.count('journeyIcon(') >= 4


def test_the_journey_icon_is_empty_when_the_journey_is_off():
    """The helper is rendered either way — it reads the flag at call
    time — and returns nothing when the feature is off for this
    person, so no row carries a link to a 404."""
    with flask_app.app_context():
        _all(False)
    html = _html('/app', ADMIN, 'admin')
    assert 'function journeyIcon' in html
    assert 'journey:false' in html.replace(' ', '')


def test_the_v2_panel_carries_a_journey_link():
    with flask_app.app_context():
        _all(True)
        _set('FEATURE_LEAD_POPUP_V2', True)
        lead = _lead()
        lid = lead.id
        db.session.commit()
    html = _html(f'/leads/{lid}/panel', ADMIN, 'admin')
    assert f'/leads/{lid}/journey' in html
    assert 'class="lf-jny"' in html


def test_the_v2_panel_has_no_journey_link_when_it_is_off():
    with flask_app.app_context():
        _all(False)
        _set('FEATURE_LEAD_POPUP_V2', True)
        lead = _lead()
        lid = lead.id
        db.session.commit()
    html = _html(f'/leads/{lid}/panel', ADMIN, 'admin')
    assert 'class="lf-jny"' not in html
    assert f'/leads/{lid}/journey' not in html


# ── 4. the cockpit's own rows ────────────────────────────────────────
def test_a_cockpit_row_opens_the_lead_journey():
    with flask_app.app_context():
        _all(True)
    html = _html('/crm2', ADMIN, 'admin')
    assert 'function rowHref' in html
    assert 'function leadHref' in html
    assert "'/leads/' + id + '/journey'" in html
    assert 'const JOURNEY_ON = true' in html


def test_a_cockpit_row_falls_back_to_the_popup_without_the_journey():
    with flask_app.app_context():
        _all(True)
        _set('FEATURE_LEAD_JOURNEY', False)
    html = _html('/crm2', ADMIN, 'admin')
    assert 'const JOURNEY_ON = false' in html
    assert "'/app?lead=' + id" in html


def test_a_cockpit_row_is_a_link_and_not_a_dead_div():
    with flask_app.app_context():
        _all(True)
    html = _html('/crm2', ADMIN, 'admin')
    assert 'class="ck-item ck-open"' in html
    assert 'function openable' in html


# ── 5. the routes the links point at actually answer ─────────────────
def test_every_link_the_menu_offers_opens():
    with flask_app.app_context():
        _all(True)
        lead = _lead()
        lid = lead.id
        db.session.commit()
    c = _client(ADMIN, 'admin')
    for path in ('/crm2', '/rfqs/chain', '/rfqs/reports',
                 f'/leads/{lid}/journey'):
        assert c.get(path).status_code == 200, path


def test_the_feature_for_helper_reads_the_person_not_the_switch():
    with flask_app.app_context():
        _all(True, only=[PILOT])
        with flask_app.test_request_context('/'):
            from flask import session as sess
            sess['emp_code'] = PILOT
            assert _main._feature_for('FEATURE_CRM2') is True
            sess['emp_code'] = USER
            assert _main._feature_for('FEATURE_CRM2') is False
        # The global switch says on for both; only the pilot sees it.
        assert flags.on('FEATURE_CRM2') is True


def test_the_helper_never_raises():
    with flask_app.app_context():
        with flask_app.test_request_context('/'):
            assert _main._feature_for('NOT_A_FLAG_AT_ALL') is False
