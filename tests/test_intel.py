"""External intelligence: one project per timeline, honest sources, and
vocabularies that live in Master Data.

These tests are the record of four promises Release 5 makes.

    1. Two articles about one project update ONE timeline. The CRM does
       not end up with three refinery expansions because three
       publications wrote about one.
    2. An adapter without its subscription or key returns nothing and
       says why. It never invents a row, and the attempt is recorded so
       "no news" and "no feed" can be told apart.
    3. Every intelligence record keeps its source, its publication and
       capture dates, its confidence and when it was last verified.
    4. Vendor categories, vessel types and ports come from Master Data.
       Adding one is an administrator's row, not a release.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

os.environ['URL_PREFIX'] = ''
os.environ['SESSION_COOKIE_SECURE'] = 'false'
os.environ.setdefault('SECRET_KEY', 'intel-test-secret')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'IntelTest123456')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(
    tempfile.mkdtemp(), 'intel.db')

import importlib                                          # noqa: E402
_main = importlib.import_module('app')
importlib.import_module('app.models')
importlib.import_module('app.models.intel')
flask_app, db = _main.app, _main.db
Company, Contact, Employee, Lead = (_main.Company, _main.Contact,
                                    _main.Employee, _main.Lead)
flask_app.config['WTF_CSRF_ENABLED'] = False

from app import intel as intel_pkg                        # noqa: E402
from app.access import scope as sc_mod                    # noqa: E402
from app.intel import adapters as ad                      # noqa: E402
from app.intel import competitors as comp                 # noqa: E402
from app.intel import projects as proj                    # noqa: E402
from app.intel import vendors as ven                      # noqa: E402
from app.intel.routes import bp as intel_bp               # noqa: E402
from app.models.access import AccessProfile, DataScope    # noqa: E402
from app.models.intel import (IntelProjectFact, IntelRawItem,  # noqa: E402
                              IntelSourceConfig, IntelSourceRun,
                              Vessel, VesselPortCall, VendorProfile)
from app.models.master_data import MasterItem, MasterList  # noqa: E402
from app.models.public_source import PublicSource         # noqa: E402
from presales.models_projects import (Project,            # noqa: E402
                                      ProjectUpdate)

if 'intel' not in flask_app.blueprints:
    # Registered here because app.py is another stream's file; in
    # production the blueprint is added to the registration loop there.
    flask_app.register_blueprint(intel_bp)

REP, OTHER, HEAD = 'INREP', 'INOTHER', 'INHEAD'
VERTICAL = 'Project Freight'


# ── fixture world ────────────────────────────────────────────────────
def _emp(code, name, head=False, scope=None, perms=()):
    e = Employee.query.filter_by(emp_code=code).first() or Employee(
        emp_code=code)
    e.name, e.vertical, e.is_active, e.must_change_pw = (name, VERTICAL,
                                                         True, False)
    e.role, e.is_super_admin, e.is_vertical_head = 'user', False, head
    e.session_version = 0
    db.session.add(e)
    db.session.flush()
    AccessProfile.query.filter_by(emp_code=code).delete()
    if scope:
        db.session.add(AccessProfile(emp_code=code, data_scope=scope,
                                     perms=list(perms)))
    return e


def _seed_master_data():
    """The vocabularies the migration seeds, through the ORM.

    Done here rather than hard-coded in the modules under test: these
    rows ARE the configuration, and a test that bypassed Master Data
    would not notice if the code stopped reading it.
    """
    for key, label, description in intel_pkg.INTEL_LISTS:
        if MasterList.query.filter_by(key=key).first() is None:
            db.session.add(MasterList(key=key, label=label,
                                      description=description,
                                      is_system=True, sort_order=900))
    seeds = {
        intel_pkg.LIST_VENDOR_CATEGORY: intel_pkg.SEED_VENDOR_CATEGORIES,
        intel_pkg.LIST_VESSEL_TYPE: intel_pkg.SEED_VESSEL_TYPES,
        intel_pkg.LIST_INDIA_PORT: intel_pkg.SEED_INDIA_PORTS,
        intel_pkg.LIST_CONFIDENCE: intel_pkg.SEED_CONFIDENCE,
        intel_pkg.LIST_ACTIVITY_TYPE: intel_pkg.SEED_ACTIVITY_TYPES,
        intel_pkg.LIST_INTEL_STAGE: intel_pkg.SEED_INTEL_STAGES,
    }
    for list_key, items in seeds.items():
        for order, (code, label) in enumerate(items):
            if MasterItem.query.filter_by(list_key=list_key,
                                          code=code).first() is None:
                db.session.add(MasterItem(list_key=list_key, code=code,
                                          label=label, sort_order=order * 10,
                                          is_active=True, meta={}))
    db.session.flush()


def _wipe():
    """Only this module's rows: the suite shares one database."""
    mine = [p.id for p in Project.query.filter(Project.name.like('IT %')).all()]
    if mine:
        IntelRawItem.query.filter(IntelRawItem.project_id.in_(mine)).delete(
            synchronize_session=False)
        IntelProjectFact.query.filter(
            IntelProjectFact.project_id.in_(mine)).delete(
                synchronize_session=False)
        ProjectUpdate.query.filter(ProjectUpdate.project_id.in_(mine)).delete(
            synchronize_session=False)
        Project.query.filter(Project.id.in_(mine)).delete(
            synchronize_session=False)
    IntelRawItem.query.delete(synchronize_session=False)
    IntelSourceRun.query.delete(synchronize_session=False)
    IntelSourceConfig.query.delete(synchronize_session=False)
    VesselPortCall.query.delete(synchronize_session=False)
    Vessel.query.delete(synchronize_session=False)
    VendorProfile.query.delete(synchronize_session=False)
    Lead.query.filter(Lead.source == 'Project Intelligence').delete(
        synchronize_session=False)


@pytest.fixture()
def world():
    with flask_app.app_context():
        db.create_all()
        _wipe()
        _seed_master_data()
        _emp(REP, 'Intel Rep', scope=DataScope.OWN)
        _emp(OTHER, 'Another Rep', scope=DataScope.OWN)
        _emp(HEAD, 'Vertical Head', head=True, scope=DataScope.VERTICAL)
        db.session.commit()
        yield


def _client(code):
    c = flask_app.test_client()
    with c.session_transaction() as s:
        s.update(emp_code=code, name=code, role='user', vertical=VERTICAL,
                 sv=0)
    return c


def _item(title, **kw):
    """A manual sighting, through the same adapter a person's paste uses."""
    payload = kw.pop('payload', {})
    return ad.ManualAdapter.item(title=title, payload=payload, **kw)


# ═════════════════════════════════════════════════════════════════════
# 1 · One project, one timeline
# ═════════════════════════════════════════════════════════════════════
def test_two_articles_about_one_project_update_one_timeline(world):
    with flask_app.app_context():
        first = proj.ingest(_item(
            'IT Dahej Petrochemical Expansion Project announced',
            url='https://example.invalid/a1',
            publication_date=date(2026, 9, 1),
            payload={'project_name': 'IT Dahej Petrochemical Expansion',
                     'owner_group': 'Westcoast Chemicals Limited',
                     'location': 'Dahej, Gujarat',
                     'industry': 'Chemicals'}), actor=REP)
        # A second publication, three weeks later, writing the same
        # project's name differently and omitting the owner entirely.
        second = proj.ingest(_item(
            'IT Dahej petrochemical expansion: EPC contract awarded',
            url='https://example.invalid/a2',
            publication_date=date(2026, 9, 22),
            payload={'project_name': 'IT Dahej Petrochemicals Expansion '
                                     'Project',
                     'location': 'Dahej',
                     'epc_contractor': 'Larkhill Engineering'}), actor=REP)

        assert first['action'] == 'created'
        assert second['action'] == 'appended'
        assert second['project_id'] == first['project_id']

        assert Project.query.filter(
            Project.name.like('IT Dahej%')).count() == 1
        assert ProjectUpdate.query.filter_by(
            project_id=first['project_id']).count() == 2

        fact = proj.fact_for(first['project_id'])
        assert fact.update_count == 2
        # The owner the first article gave survives the second, which
        # did not mention it.
        assert fact.owner_group == 'Westcoast Chemicals Limited'
        # And the second article's new fact was taken.
        assert fact.epc_contractor == 'Larkhill Engineering'


def test_the_stage_advances_and_the_change_is_on_the_project(world):
    with flask_app.app_context():
        out = proj.ingest(_item(
            'IT Koyali Refinery unit announced by the board',
            url='https://example.invalid/b1',
            payload={'project_name': 'IT Koyali Refinery unit',
                     'owner_group': 'Statewide Refining',
                     'location': 'Vadodara'}), actor=REP)
        assert proj.fact_for(out['project_id']).intel_stage == 'announced'

        proj.ingest(_item(
            'IT Koyali Refinery unit: EPC contract awarded to a bidder',
            url='https://example.invalid/b2',
            payload={'project_name': 'IT Koyali Refinery unit',
                     'location': 'Vadodara'}), actor=REP)
        fact = proj.fact_for(out['project_id'])
        project = db.session.get(Project, out['project_id'])
        assert fact.intel_stage == 'epc'
        # Mapped onto the vocabulary the pre-sales screens already use.
        assert project.stage == 'EPC Appointed'
        from presales.models_projects import ProjectStageHistory
        assert ProjectStageHistory.query.filter_by(
            project_id=project.id).count() >= 1


def test_the_same_article_twice_is_not_a_second_update(world):
    with flask_app.app_context():
        item = _item('IT Paradip berth upgrade announced',
                     url='https://example.invalid/c1',
                     payload={'project_name': 'IT Paradip berth upgrade',
                              'owner_group': 'Eastern Port Trust',
                              'location': 'Paradip'})
        first = proj.ingest(item, actor=REP)
        again = proj.ingest(item, actor=REP)
        assert first['action'] == 'created'
        assert again['action'] == 'duplicate'
        assert ProjectUpdate.query.filter_by(
            project_id=first['project_id']).count() == 1


def test_a_different_project_with_a_similar_name_stays_separate(world):
    with flask_app.app_context():
        a = proj.ingest(_item(
            'IT Mundra cement grinding unit announced',
            url='https://example.invalid/d1',
            payload={'project_name': 'IT Mundra cement grinding unit',
                     'owner_group': 'Northern Cement',
                     'location': 'Mundra'}), actor=REP)
        b = proj.ingest(_item(
            'IT Mundra cement grinding unit announced',
            url='https://example.invalid/d2',
            payload={'project_name': 'IT Mundra cement grinding unit',
                     'owner_group': 'Southern Cement',
                     'location': 'Mundra'}), actor=REP)
        # Same name and place, a different owner — two projects.
        assert a['project_id'] != b['project_id']
        assert b['action'] == 'created'


def test_the_normaliser_ignores_noise_but_not_identity():
    # Punctuation, the word "project", a company suffix and a plural are
    # noise; the place, the number of the phase and the owner are not.
    assert proj.name_key('Dahej Cracker Project (Phase-II)') == \
        proj.name_key('dahej crackers, phase 2')
    assert proj.owner_key('Westcoast Chemicals Limited') == \
        proj.owner_key('Westcoast Chemicals Pvt Ltd')
    assert proj.name_key('Dahej unit') != proj.name_key('Mundra unit')
    # Phase I and Phase II are two projects, and stay two.
    assert proj.name_key('Dahej cracker phase I') != \
        proj.name_key('Dahej cracker phase II')
    # A blank part never contradicts a stated one, so a bulletin that
    # omits the owner still matches.
    assert proj._compatible('', 'westcoast')
    assert proj._compatible('dahej', 'dahej gujarat')
    assert not proj._compatible('mundra', 'dahej')


# ═════════════════════════════════════════════════════════════════════
# 2 · Adapters are honest about what they cannot do
# ═════════════════════════════════════════════════════════════════════
def test_an_unconfigured_feed_returns_nothing_and_says_why():
    adapter = ad.RssAdapter(config={})
    ok, reason = adapter.available()
    assert ok is False
    assert adapter.status() == ad.STATUS_SOURCE_REQUIRED
    assert 'feed_url is not configured' in reason
    assert adapter.fetch(None) == []
    assert adapter.last_reason == reason


def test_an_api_adapter_without_its_key_names_the_variable_it_wants():
    adapter = ad.JsonApiAdapter(config={
        'endpoint': 'https://example.invalid/v1/projects',
        'api_key_env': 'INTEL_TEST_KEY_THAT_IS_NOT_SET'})
    ok, reason = adapter.available()
    assert ok is False
    assert adapter.status() == ad.STATUS_CREDENTIAL_REQUIRED
    assert 'INTEL_TEST_KEY_THAT_IS_NOT_SET' in reason
    assert adapter.fetch(None) == []


def test_only_the_dependency_free_adapters_report_operational():
    report = {r['key']: r for r in ad.status_report()}
    assert report['manual']['status'] == ad.STATUS_OPERATIONAL
    assert report['news_items']['status'] == ad.STATUS_OPERATIONAL
    assert report['rss']['status'] == ad.STATUS_SOURCE_REQUIRED
    assert report['json_api']['status'] == ad.STATUS_CREDENTIAL_REQUIRED
    assert report['port_calls']['status'] == ad.STATUS_SOURCE_REQUIRED
    assert report['vendor_directory']['status'] == ad.STATUS_SOURCE_REQUIRED
    # Every one that cannot run says what it would need.
    for row in report.values():
        assert row['dependency']


def test_running_an_unavailable_source_records_why_and_creates_nothing(world):
    with flask_app.app_context():
        before = Project.query.count()
        source = PublicSource(name='IT Paid Projects Feed',
                              url='https://example.invalid/feed.xml',
                              kind='news', is_active=True)
        db.session.add(source)
        db.session.flush()
        cfg = IntelSourceConfig(source_id=source.id, adapter_key='rss',
                                purpose='project', config={},
                                is_enabled=True)
        db.session.add(cfg)
        db.session.commit()

        run = proj.run_source(cfg, ad.get('rss', config=cfg.config,
                                          source=source), actor=REP)
        assert run.status == 'unavailable'
        assert ad.STATUS_SOURCE_REQUIRED in run.reason
        assert run.items_fetched == 0 and run.items_new == 0
        # Nothing was invented to fill the gap.
        assert Project.query.count() == before
        assert IntelRawItem.query.count() == 0


def test_the_sources_api_tells_a_signed_in_person_what_is_not_running(world):
    d = _client(REP).get('/api/intel/sources').get_json()
    assert d['ok']
    labels = {a['key']: a['status'] for a in d['adapters']}
    assert labels['rss'] == ad.STATUS_SOURCE_REQUIRED
    assert labels['manual'] == ad.STATUS_OPERATIONAL


def test_the_news_adapter_reads_the_crm_and_invents_nothing(world):
    with flask_app.app_context():
        adapter = ad.NewsItemAdapter()
        ok, reason = adapter.available()
        assert ok and reason == ''
        # No news rows in this database: an operational adapter with
        # nothing to say returns nothing, not an example.
        assert adapter.fetch(None) == []


# ═════════════════════════════════════════════════════════════════════
# 3 · Source, confidence and last-verified are kept
# ═════════════════════════════════════════════════════════════════════
def test_a_project_keeps_its_source_dates_confidence_and_entities(world):
    with flask_app.app_context():
        out = proj.ingest(_item(
            'IT Haldia tank farm cleared by the regulator',
            url='https://example.invalid/e1',
            body='Environment clearance granted for the tank farm.',
            publication_date=date(2026, 8, 14), confidence='confirmed',
            source_label='Trade Bulletin',
            payload={'project_name': 'IT Haldia tank farm',
                     'owner_group': 'Riverside Terminals',
                     'location': 'Haldia',
                     'technology_provider': 'Tankline Systems'}), actor=REP)
        fact = proj.fact_for(out['project_id'])
        assert fact.source_ref == 'Trade Bulletin'
        assert fact.source_url == 'https://example.invalid/e1'
        assert fact.publication_date == date(2026, 8, 14)
        assert fact.captured_at is not None
        assert fact.confidence == 'confirmed'
        # Confirmed means somebody can check it, so it is stamped.
        assert fact.last_verified_at is not None
        assert fact.last_verified_by == REP
        assert 'Tankline Systems' in fact.extracted_entities['organisations']
        assert fact.extracted_entities['stage_evidence'] == 'clearance'

        row = proj.timeline(out['project_id'])[0]
        assert row['confidence'] == 'confirmed'
        assert row['publication_date'] == '2026-08-14'
        assert row['source_url'] == 'https://example.invalid/e1'


def test_an_unverified_sighting_cannot_overwrite_a_confirmed_fact(world):
    with flask_app.app_context():
        out = proj.ingest(_item(
            'IT Kandla fertiliser line, EPC confirmed',
            url='https://example.invalid/f1', confidence='confirmed',
            payload={'project_name': 'IT Kandla fertiliser line',
                     'owner_group': 'Gulf Fertilisers',
                     'location': 'Kandla',
                     'epc_contractor': 'Correct Engineering'}), actor=REP)
        proj.ingest(_item(
            'IT Kandla fertiliser line, rumour about the contractor',
            url='https://example.invalid/f2', confidence='unverified',
            payload={'project_name': 'IT Kandla fertiliser line',
                     'owner_group': 'Gulf Fertilisers',
                     'location': 'Kandla',
                     'epc_contractor': 'Hearsay Engineering'}), actor=REP)
        fact = proj.fact_for(out['project_id'])
        assert fact.epc_contractor == 'Correct Engineering'
        assert fact.confidence == 'confirmed'
        # The rumour is still on the timeline — rejected, not hidden.
        assert len(proj.timeline(out['project_id'])) == 2


def test_competitor_activity_keeps_its_axes_source_and_verification(world):
    with flask_app.app_context():
        row = comp.capture(
            competitor_name='IT Rival Logistics', actor=REP,
            summary='Took a heavy-lift move out of Hazira.',
            vertical='', service='', geography='Gujarat',
            activity_type='contract_win', confidence='reported',
            source='Trade Bulletin', source_url='https://example.invalid/g1',
            publication_date=date(2026, 9, 3), event_date=date(2026, 9, 1))
        assert row.geography == 'Gujarat'
        assert row.activity_type == 'contract_win'
        assert row.source_url == 'https://example.invalid/g1'
        assert row.captured_at is not None
        assert row.last_verified_at is None       # nobody has checked yet

        comp.verify(row.id, actor=HEAD, confidence='confirmed',
                    note='Confirmed by the account owner')
        again = db.session.get(type(row), row.id)
        assert again.confidence == 'confirmed'
        assert again.last_verified_at is not None
        assert again.last_verified_by == HEAD

        found = comp.search(geography='Gujarat')
        assert found['total'] == 1
        assert comp.by_axis('activity_type')[0]['count'] == 1


def test_anything_stronger_than_a_rumour_must_name_its_source(world):
    with flask_app.app_context():
        with pytest.raises(comp.CompetitorIntelRefused):
            comp.capture(competitor_name='IT Rival Logistics', actor=REP,
                         summary='Heard they are quoting below cost.',
                         confidence='reported', source='')
        # Recorded as unverified, it is allowed — and marked as such.
        row = comp.capture(competitor_name='IT Rival Logistics', actor=REP,
                           summary='Heard they are quoting below cost.',
                           confidence='unverified')
        assert row.confidence == 'unverified' and row.source is None


# ═════════════════════════════════════════════════════════════════════
# 4 · Master Data drives the categories
# ═════════════════════════════════════════════════════════════════════
def test_vendor_categories_vessel_types_and_ports_come_from_master_data(world):
    with flask_app.app_context():
        codes = {c['code'] for c in ven.categories()}
        assert 'shipping_line' in codes and 'spmt_operator' in codes
        assert {p['code'] for p in ven.ports()} >= {'INNSA', 'INPRT'}

        # A category an administrator adds is immediately usable, with
        # no code change and no restart.
        from app.master_data import service as md
        md.add_item(intel_pkg.LIST_VENDOR_CATEGORY, 'project_forwarder',
                    'Project forwarder', actor=HEAD)
        assert 'project_forwarder' in {c['code'] for c in ven.categories()}
        vendor = ven.save_vendor(name='IT Coastal Forwarders',
                                 category='project_forwarder', actor=REP,
                                 country='India', source='Site visit',
                                 confidence='confirmed')
        assert vendor.category == 'project_forwarder'
        assert vendor.last_verified_at is not None

        # One that is NOT in Master Data is refused, rather than becoming
        # free text no filter will ever find again.
        with pytest.raises(ven.VendorIntelRefused):
            ven.save_vendor(name='IT Nowhere Ltd', category='invented',
                            actor=REP, source='x')

        # Retiring a value removes it everywhere at once.
        item = MasterItem.query.filter_by(
            list_key=intel_pkg.LIST_VENDOR_CATEGORY,
            code='project_forwarder').first()
        md.update_item(item.id, is_active=False)
        assert 'project_forwarder' not in {c['code'] for c in ven.categories()}


def test_a_vessel_and_a_port_call_keep_their_provenance(world):
    with flask_app.app_context():
        vessel = ven.save_vessel(
            name='IT Heavy Carrier', actor=REP, vessel_type='heavy_lift',
            owner_operator='Blue Ocean Shipping', dwt=12500,
            deck_capacity_sqm=2100, cranes='2 x 400 t', max_lift_tonnes=800,
            self_geared=True, heavy_lift=True, calls_india=True,
            local_agent='Western Agencies', source='Operator fleet list',
            source_url='https://example.invalid/fleet', confidence='reported')
        assert vessel.max_lift_tonnes == 800 and vessel.self_geared is True
        assert vessel.source == 'Operator fleet list'
        assert vessel.last_verified_at is None

        found = ven.search_vessels(min_lift=600, calls_india=True)
        assert found['total'] == 1
        # A vessel whose lift nobody recorded is not offered as capable.
        ven.save_vessel(name='IT Unknown Gear', actor=REP,
                        vessel_type='mpv', source='x')
        assert ven.search_vessels(min_lift=600)['total'] == 1

        call = ven.save_port_call(
            vessel_name='IT Heavy Carrier', port_code='INNSA', actor=REP,
            operator='Blue Ocean Shipping', eta='2026-10-20 06:00',
            etd='2026-10-22 18:00', previous_port='Singapore',
            next_port='Mundra', cargo='2 reactors, 310 t each',
            agent='Western Agencies', source='Agent advice',
            confidence='reported')
        assert call.port_name == 'JNPT / Nhava Sheva'
        assert call.vessel_id == vessel.id
        assert ven.port_calls(port_code='INNSA')['total'] == 1

        with pytest.raises(ven.VendorIntelRefused):
            ven.save_port_call(vessel_name='IT Heavy Carrier',
                               port_code='INXXX', actor=REP, source='x')


def test_the_registers_ship_empty(world):
    """No sample intelligence is seeded anywhere. An empty register is
    the honest state of a CRM nobody has given data to."""
    with flask_app.app_context():
        assert VendorProfile.query.count() == 0
        assert Vessel.query.count() == 0
        assert VesselPortCall.query.count() == 0
        cov = ven.coverage()
        assert cov['vendors'] == 0 and cov['port_calls'] == 0
        # The vocabularies, by contrast, ARE configured.
        assert cov['categories_configured'] >= 17
        assert cov['ports_configured'] >= 8


# ═════════════════════════════════════════════════════════════════════
# 5 · Access
# ═════════════════════════════════════════════════════════════════════
@pytest.fixture()
def owned(world):
    with flask_app.app_context():
        mine = proj.ingest(_item(
            'IT Owned By Rep plant announced',
            url='https://example.invalid/h1',
            payload={'project_name': 'IT Owned By Rep plant',
                     'owner_group': 'Rep Holdings', 'location': 'Nagpur',
                     'bd_owner': REP}), actor=REP)
        theirs = proj.ingest(_item(
            'IT Owned By Other plant announced',
            url='https://example.invalid/h2',
            payload={'project_name': 'IT Owned By Other plant',
                     'owner_group': 'Other Holdings', 'location': 'Pune',
                     'bd_owner': OTHER}), actor=OTHER)
        unowned = proj.ingest(_item(
            'IT Nobody Owns This plant announced',
            url='https://example.invalid/h3',
            payload={'project_name': 'IT Nobody Owns This plant',
                     'owner_group': 'Unclaimed Holdings',
                     'location': 'Surat'}), actor=REP)
        return {'mine': mine['project_id'], 'theirs': theirs['project_id'],
                'unowned': unowned['project_id']}


def test_the_api_needs_a_session(world):
    assert flask_app.test_client().get(
        '/api/intel/projects').status_code == 401
    assert flask_app.test_client().post(
        '/api/intel/capture', json={'title': 'x'}).status_code == 401


def test_a_rep_sees_their_own_and_the_unowned_but_not_a_colleagues(owned):
    d = _client(REP).get('/api/intel/projects?per_page=100').get_json()
    ids = {row['id'] for row in d['items']}
    assert owned['mine'] in ids
    assert owned['unowned'] in ids          # nobody owns it yet
    assert owned['theirs'] not in ids

    r = _client(REP).get(f"/api/intel/projects/{owned['theirs']}")
    assert r.status_code == 403


def test_a_vertical_head_sees_the_team(owned):
    d = _client(HEAD).get('/api/intel/projects?per_page=100').get_json()
    ids = {row['id'] for row in d['items']}
    assert {owned['mine'], owned['theirs'], owned['unowned']} <= ids


def test_the_source_configuration_screens_are_admin_only(world):
    assert _client(REP).get(
        '/admin/intelligence/sources').status_code == 403
    assert _client(REP).post('/api/intel/sources',
                             json={'adapter_key': 'rss',
                                   'name': 'x'}).status_code == 403


def test_a_key_pasted_into_a_source_config_is_refused(world):
    with flask_app.app_context():
        _emp(HEAD, 'Vertical Head', head=True, scope=DataScope.ALL,
             perms=['admin.master'])
        db.session.commit()
    r = _client(HEAD).post('/api/intel/sources',
                           json={'adapter_key': 'json_api', 'name': 'IT Feed',
                                 'url': 'https://example.invalid/v1',
                                 'config': {'endpoint': 'https://x.invalid',
                                            'api_key': 'sk-secret'}})
    assert r.status_code == 400
    assert 'environment variable' in r.get_json()['error']


# ═════════════════════════════════════════════════════════════════════
# 6 · Actions
# ═════════════════════════════════════════════════════════════════════
def test_create_lead_writes_assignment_history_and_an_audit_event(owned):
    with flask_app.app_context():
        from app import LeadAssignmentHistory
        from app.models.audit import AuditEvent

        project = db.session.get(Project, owned['unowned'])
        lead = proj.create_lead(project, actor=REP, owner=REP,
                                note='Worth chasing')
        assert lead.id and lead.source == 'Project Intelligence'
        assert lead.assigned_to == REP
        assert lead.company == 'Unclaimed Holdings'

        history = LeadAssignmentHistory.query.filter_by(
            lead_id=lead.id).all()
        assert len(history) == 1
        assert history[0].to_primary == REP

        event = (AuditEvent.query
                 .filter_by(action='intel.project.create_lead',
                            entity_id=str(lead.id)).first())
        assert event is not None
        assert event.new_value['project_id'] == project.id

        fact = proj.fact_for(project.id)
        assert fact.lead_id == lead.id
        assert fact.opportunity_status == 'lead_created'

        # A second attempt is refused rather than making a twin.
        with pytest.raises(proj.IntelRefused):
            proj.create_lead(project, actor=REP, owner=REP)


def test_assign_follow_link_and_dismiss_are_all_audited(owned):
    from app.models.audit import AuditEvent

    client = _client(REP)
    pid = owned['unowned']
    assert client.post(f'/api/intel/projects/{pid}/assign',
                       json={'emp_code': REP,
                             'reason': 'Mine to work'}).status_code == 200
    assert client.post(f'/api/intel/projects/{pid}/follow',
                       json={'follow': True}).status_code == 200

    with flask_app.app_context():
        company = Company(name='IT Project Owner Ltd')
        db.session.add(company)
        db.session.commit()
        cid = company.id
    assert client.post(f'/api/intel/projects/{pid}/accounts',
                       json={'account_id': cid,
                             'role': 'Project Owner'}).status_code == 200

    # Without a reason, dismissing is refused: the reason is what stops
    # the next bulletin raising it again.
    assert client.post(f'/api/intel/projects/{pid}/not-relevant',
                       json={'reason': ''}).status_code == 400
    assert client.post(f'/api/intel/projects/{pid}/not-relevant',
                       json={'reason': 'Captive logistics, never tendered'}
                       ).status_code == 200

    with flask_app.app_context():
        actions = {e.action for e in AuditEvent.query.filter(
            AuditEvent.entity_id == str(pid),
            AuditEvent.action.like('intel.project.%')).all()}
        assert {'intel.project.assign', 'intel.project.follow',
                'intel.project.add_account',
                'intel.project.not_relevant'} <= actions
        assert proj.fact_for(pid).not_relevant is True
        assert proj.followers(pid) == [REP]


def test_capturing_through_the_api_folds_into_the_same_timeline(world):
    client = _client(REP)
    body = {'title': 'IT Bhadla solar park, land allotted',
            'url': 'https://example.invalid/i1',
            'confidence': 'reported',
            'payload': {'project_name': 'IT Bhadla solar park',
                        'owner_group': 'Desert Power', 'location': 'Bhadla'}}
    first = client.post('/api/intel/capture', json=body).get_json()
    assert first['ok'] and first['action'] == 'created'

    body['title'] = 'IT Bhadla solar park: supplier selected for modules'
    body['url'] = 'https://example.invalid/i2'
    second = client.post('/api/intel/capture', json=body).get_json()
    assert second['action'] == 'appended'
    assert second['project_id'] == first['project_id']

    timeline = client.get(
        f"/api/intel/projects/{first['project_id']}/timeline").get_json()
    assert len(timeline['timeline']) == 2


def test_a_sourced_capture_needs_a_link_unless_it_is_unverified(world):
    client = _client(REP)
    r = client.post('/api/intel/capture',
                    json={'title': 'IT Something heard', 'url': '',
                          'confidence': 'reported'})
    assert r.status_code == 400
    r = client.post('/api/intel/capture',
                    json={'title': 'IT Something heard', 'url': '',
                          'confidence': 'unverified'})
    assert r.status_code == 200


# ═════════════════════════════════════════════════════════════════════
# 7 · The pages render
# ═════════════════════════════════════════════════════════════════════
def test_the_pages_render_for_a_signed_in_person(owned):
    client = _client(REP)
    for path in ('/intelligence/projects', '/intelligence/competitors',
                 '/intelligence/vendors',
                 f"/intelligence/projects/{owned['mine']}"):
        r = client.get(path)
        assert r.status_code == 200, path
    # A colleague's project is not openable.
    assert client.get(
        f"/intelligence/projects/{owned['theirs']}").status_code == 403
