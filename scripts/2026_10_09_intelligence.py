"""
External intelligence (Release 5, Groups H, I and J) — schema and
vocabularies.

Adds nine tables and no columns at all. Project intelligence already has
`projects`, `project_updates` and `project_stage_history`; competitor
intelligence already has `competitor_intelligence`; source monitoring
already has `public_sources`. Every one of those keeps the shape it has,
and the new tables hang off them:

    intel_source_configs        which adapter runs a public source, and
                                the NAME of the environment variable its
                                credential lives in — never the value
    intel_source_runs           one row per collection attempt, including
                                the attempts that could not run and why
    intel_raw_items             what arrived, with the fingerprint that
                                stops one article being ingested twice
    intel_project_facts         the structured §12.1 capture per project,
                                and the normalised key that keeps two
                                articles about one project on ONE timeline
    intel_project_follows       who asked to be told when it moves
    intel_competitor_activity   competitor activity by vertical, service,
                                geography and industry
    vendor_profiles             carriers, operators, agents, hauliers,
                                warehouses, customs partners, airlines
    vessels                     vessel capability
    vessel_port_calls           India port-call intelligence

Why tables and not columns
    A model column the database lacks breaks every query on that table.
    `projects` and `competitor_intelligence` are read by the pre-sales
    screens, the Copilot and the reports; a deploy landing before this
    script ran would take them down. A table nothing has been told about
    is inert, so the intelligence screens degrade to "nothing recorded
    yet" until the script runs, and nothing else notices.

What is seeded, and what is not
    SEEDED: six Master Data vocabularies — vendor categories, vessel
    types, India ports, confidence levels, competitor activity types and
    the project-intelligence stages. These are configuration: the code
    reads them from Master Data at run time, so seeding them is what
    makes the screens usable, and an administrator may rename, reorder
    or retire any of them afterwards.

    NOT SEEDED: a single row of intelligence. No example project, no
    sample vessel, no demonstration port call. An example on an
    intelligence screen is indistinguishable from intelligence, and
    somebody would quote against it. The registers start empty, and the
    screens say that is what they are.

Usage
    python scripts/2026_10_09_intelligence.py --check
    python scripts/2026_10_09_intelligence.py
    python scripts/2026_10_09_intelligence.py --down --yes
"""
import argparse
import json
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:
    pass

from sqlalchemy import create_engine, inspect, text      # noqa: E402

TABLES = ('intel_source_configs', 'intel_source_runs', 'intel_raw_items',
          'intel_project_facts', 'intel_project_follows',
          'intel_competitor_activity', 'vendor_profiles', 'vessels',
          'vessel_port_calls')

#: Written out rather than taken from the models, so this script reads
#: on its own and runs without importing the application.
DDL = {
    'intel_source_configs': """
        CREATE TABLE intel_source_configs (
            id          INTEGER PRIMARY KEY,
            source_id   INTEGER NOT NULL REFERENCES public_sources (id),
            adapter_key VARCHAR(40) NOT NULL,
            purpose     VARCHAR(20),
            config      JSON,
            is_enabled  BOOLEAN,
            notes       TEXT,
            created_at  TIMESTAMP,
            created_by  VARCHAR(20),
            updated_at  TIMESTAMP
        )""",
    'intel_source_runs': """
        CREATE TABLE intel_source_runs (
            id            INTEGER PRIMARY KEY,
            source_id     INTEGER REFERENCES public_sources (id),
            adapter_key   VARCHAR(40) NOT NULL,
            status        VARCHAR(20),
            reason        TEXT,
            started_at    TIMESTAMP,
            finished_at   TIMESTAMP,
            items_fetched INTEGER,
            items_new     INTEGER,
            items_matched INTEGER,
            run_by        VARCHAR(20)
        )""",
    'intel_raw_items': """
        CREATE TABLE intel_raw_items (
            id                INTEGER PRIMARY KEY,
            source_id         INTEGER REFERENCES public_sources (id),
            adapter_key       VARCHAR(40) NOT NULL,
            external_id       VARCHAR(300),
            fingerprint       VARCHAR(64) NOT NULL UNIQUE,
            url               VARCHAR(500),
            title             VARCHAR(500),
            body              TEXT,
            publication_date  DATE,
            captured_at       TIMESTAMP,
            payload           JSON,
            confidence        VARCHAR(20),
            status            VARCHAR(20),
            project_id        INTEGER REFERENCES projects (id),
            project_update_id INTEGER REFERENCES project_updates (id),
            match_note        TEXT,
            captured_by       VARCHAR(20)
        )""",
    'intel_project_facts': """
        CREATE TABLE intel_project_facts (
            id                  INTEGER PRIMARY KEY,
            project_id          INTEGER NOT NULL UNIQUE
                                    REFERENCES projects (id),
            match_key           VARCHAR(300),
            name_key            VARCHAR(200),
            owner_key           VARCHAR(120),
            location_key        VARCHAR(120),
            owner_group         VARCHAR(240),
            industry            VARCHAR(120),
            location            VARCHAR(240),
            value_inr           NUMERIC(16, 2),
            announcement_date   DATE,
            intel_stage         VARCHAR(40),
            land_status         VARCHAR(240),
            clearance_status    VARCHAR(240),
            epc_contractor      VARCHAR(240),
            pmc                 VARCHAR(240),
            technology_provider VARCHAR(240),
            equipment_suppliers JSON,
            logistics_needs     TEXT,
            timeline_note       TEXT,
            source_ref          VARCHAR(240),
            source_url          VARCHAR(500),
            publication_date    DATE,
            captured_at         TIMESTAMP,
            confidence          VARCHAR(20),
            last_verified_at    TIMESTAMP,
            last_verified_by    VARCHAR(20),
            extracted_entities  JSON,
            bd_owner_emp_code   VARCHAR(20),
            vertical            VARCHAR(60),
            opportunity_status  VARCHAR(30),
            lead_id             INTEGER REFERENCES leads (id),
            not_relevant        BOOLEAN,
            not_relevant_reason VARCHAR(300),
            update_count        INTEGER,
            created_at          TIMESTAMP,
            created_by          VARCHAR(20)
        )""",
    'intel_project_follows': """
        CREATE TABLE intel_project_follows (
            id         INTEGER PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects (id),
            emp_code   VARCHAR(20) NOT NULL,
            created_at TIMESTAMP,
            CONSTRAINT uq_intel_follow UNIQUE (project_id, emp_code)
        )""",
    'intel_competitor_activity': """
        CREATE TABLE intel_competitor_activity (
            id                     INTEGER PRIMARY KEY,
            company_id             INTEGER REFERENCES companies (id),
            competitor_master_id   INTEGER
                                       REFERENCES competitor_masters (id),
            competitor_name        VARCHAR(240) NOT NULL,
            vertical               VARCHAR(60),
            service                VARCHAR(60),
            industry               VARCHAR(120),
            geography              VARCHAR(120),
            activity_type          VARCHAR(60),
            summary                TEXT NOT NULL,
            event_date             DATE,
            confidence             VARCHAR(20),
            source                 VARCHAR(240),
            source_url             VARCHAR(500),
            publication_date       DATE,
            captured_at            TIMESTAMP,
            last_verified_at       TIMESTAMP,
            last_verified_by       VARCHAR(20),
            related_account_id     INTEGER REFERENCES companies (id),
            related_opportunity_id INTEGER REFERENCES opportunities (id),
            related_project_id     INTEGER REFERENCES projects (id),
            raw_item_id            INTEGER REFERENCES intel_raw_items (id),
            added_by               VARCHAR(20),
            added_at               TIMESTAMP
        )""",
    'vendor_profiles': """
        CREATE TABLE vendor_profiles (
            id               INTEGER PRIMARY KEY,
            name             VARCHAR(240) NOT NULL,
            category         VARCHAR(60) NOT NULL,
            subcategory      VARCHAR(60),
            company_id       INTEGER REFERENCES companies (id),
            country          VARCHAR(80),
            city             VARCHAR(120),
            ports_served     JSON,
            services         JSON,
            trade_lanes      JSON,
            capabilities     TEXT,
            contact_name     VARCHAR(160),
            contact_role     VARCHAR(120),
            contact_email    VARCHAR(160),
            contact_phone    VARCHAR(60),
            website          VARCHAR(240),
            notes            TEXT,
            source           VARCHAR(240),
            source_url       VARCHAR(500),
            confidence       VARCHAR(20),
            captured_at      TIMESTAMP,
            last_verified_at TIMESTAMP,
            last_verified_by VARCHAR(20),
            is_active        BOOLEAN,
            created_at       TIMESTAMP,
            created_by       VARCHAR(20)
        )""",
    'vessels': """
        CREATE TABLE vessels (
            id                INTEGER PRIMARY KEY,
            name              VARCHAR(200) NOT NULL,
            imo               VARCHAR(20),
            owner_operator    VARCHAR(240),
            vendor_id         INTEGER REFERENCES vendor_profiles (id),
            vessel_type       VARCHAR(60),
            dwt               INTEGER,
            deck_capacity_sqm INTEGER,
            cranes            VARCHAR(160),
            max_lift_tonnes   INTEGER,
            self_geared       BOOLEAN,
            roro              BOOLEAN,
            heavy_lift        BOOLEAN,
            trading_area      VARCHAR(240),
            calls_india       BOOLEAN,
            local_agent       VARCHAR(240),
            contact_email     VARCHAR(160),
            contact_phone     VARCHAR(60),
            notes             TEXT,
            source            VARCHAR(240),
            source_url        VARCHAR(500),
            confidence        VARCHAR(20),
            captured_at       TIMESTAMP,
            last_verified_at  TIMESTAMP,
            last_verified_by  VARCHAR(20),
            is_active         BOOLEAN,
            created_at        TIMESTAMP,
            created_by        VARCHAR(20)
        )""",
    'vessel_port_calls': """
        CREATE TABLE vessel_port_calls (
            id               INTEGER PRIMARY KEY,
            vessel_id        INTEGER REFERENCES vessels (id),
            vessel_name      VARCHAR(200) NOT NULL,
            port_code        VARCHAR(40) NOT NULL,
            port_name        VARCHAR(120),
            operator         VARCHAR(240),
            eta              TIMESTAMP,
            etd              TIMESTAMP,
            previous_port    VARCHAR(120),
            next_port        VARCHAR(120),
            cargo            TEXT,
            agent            VARCHAR(240),
            vendor_id        INTEGER REFERENCES vendor_profiles (id),
            notes            TEXT,
            source           VARCHAR(240),
            source_url       VARCHAR(500),
            confidence       VARCHAR(20),
            captured_at      TIMESTAMP,
            last_verified_at TIMESTAMP,
            last_verified_by VARCHAR(20),
            created_by       VARCHAR(20)
        )""",
}

INDEXES = (
    ('ix_intel_source_configs_source_id', 'intel_source_configs', 'source_id'),
    ('ix_intel_source_configs_adapter_key', 'intel_source_configs',
     'adapter_key'),
    ('ix_intel_source_runs_started_at', 'intel_source_runs', 'started_at'),
    ('ix_intel_source_runs_status', 'intel_source_runs', 'status'),
    ('ix_intel_raw_items_fingerprint', 'intel_raw_items', 'fingerprint'),
    ('ix_intel_raw_items_project_id', 'intel_raw_items', 'project_id'),
    ('ix_intel_raw_items_captured_at', 'intel_raw_items', 'captured_at'),
    ('ix_intel_project_facts_match_key', 'intel_project_facts', 'match_key'),
    ('ix_intel_project_facts_name_key', 'intel_project_facts', 'name_key'),
    ('ix_intel_project_facts_intel_stage', 'intel_project_facts',
     'intel_stage'),
    ('ix_intel_project_facts_bd_owner', 'intel_project_facts',
     'bd_owner_emp_code'),
    ('ix_intel_project_follows_project_id', 'intel_project_follows',
     'project_id'),
    ('ix_intel_project_follows_emp_code', 'intel_project_follows',
     'emp_code'),
    ('ix_intel_competitor_activity_name', 'intel_competitor_activity',
     'competitor_name'),
    ('ix_intel_competitor_activity_vertical', 'intel_competitor_activity',
     'vertical'),
    ('ix_intel_competitor_activity_service', 'intel_competitor_activity',
     'service'),
    ('ix_intel_competitor_activity_geography', 'intel_competitor_activity',
     'geography'),
    ('ix_intel_competitor_activity_event_date', 'intel_competitor_activity',
     'event_date'),
    ('ix_vendor_profiles_name', 'vendor_profiles', 'name'),
    ('ix_vendor_profiles_category', 'vendor_profiles', 'category'),
    ('ix_vessels_name', 'vessels', 'name'),
    ('ix_vessels_max_lift_tonnes', 'vessels', 'max_lift_tonnes'),
    ('ix_vessel_port_calls_port_code', 'vessel_port_calls', 'port_code'),
    ('ix_vessel_port_calls_eta', 'vessel_port_calls', 'eta'),
    ('ix_vessel_port_calls_vessel_name', 'vessel_port_calls', 'vessel_name'),
)


def _intel_vocabulary():
    """The list registrations and their starting values.

    Loaded straight from app/intel/__init__.py by file path, so there is
    one definition of what a vendor category is — and so this script
    still runs without importing the Flask application.
    """
    import importlib.util

    path = os.path.join(_ROOT, 'app', 'intel', '__init__.py')
    spec = importlib.util.spec_from_file_location('_intel_vocab', path)
    intel = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(intel)
    return intel.INTEL_LISTS, {
        intel.LIST_VENDOR_CATEGORY: intel.SEED_VENDOR_CATEGORIES,
        intel.LIST_VESSEL_TYPE: intel.SEED_VESSEL_TYPES,
        intel.LIST_INDIA_PORT: intel.SEED_INDIA_PORTS,
        intel.LIST_CONFIDENCE: intel.SEED_CONFIDENCE,
        intel.LIST_ACTIVITY_TYPE: intel.SEED_ACTIVITY_TYPES,
        intel.LIST_INTEL_STAGE: intel.SEED_INTEL_STAGES,
    }


def seed_master_data(conn, *, dry_run=False):
    """Create the six vocabularies and their values. Idempotent.

    A value an administrator has retired stays retired: this only ever
    inserts codes that are not already in the list.
    """
    lists, values = _intel_vocabulary()
    now = datetime.utcnow()
    added_lists, added_items = [], []

    known = {r[0] for r in conn.execute(text('SELECT key FROM master_lists'))}
    for order, (key, label, description) in enumerate(lists):
        if key in known:
            continue
        added_lists.append(key)
        if dry_run:
            continue
        conn.execute(text(
            'INSERT INTO master_lists (key, label, description, is_system, '
            'sort_order) VALUES (:k, :l, :d, :s, :o)'),
            {'k': key, 'l': label, 'd': description, 's': True,
             'o': 900 + order * 10})

    for list_key, items in values.items():
        have = {r[0] for r in conn.execute(
            text('SELECT code FROM master_items WHERE list_key = :k'),
            {'k': list_key})}
        for order, (code, label) in enumerate(items):
            if code in have:
                continue
            added_items.append(f'{list_key}:{code}')
            if dry_run:
                continue
            conn.execute(text(
                'INSERT INTO master_items (list_key, code, label, '
                'description, sort_order, is_active, meta, created_at, '
                'created_by, updated_at) VALUES (:lk, :c, :l, :d, :o, :a, '
                ':m, :t, :b, :t)'),
                {'lk': list_key, 'c': code, 'l': label, 'd': '',
                 'o': (order + 1) * 10, 'a': True, 'm': json.dumps({}),
                 't': now, 'b': 'migration'})
    return added_lists, added_items


def _db_url():
    return os.environ.get('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    url = _db_url()
    print(f'database: {url}')
    engine = create_engine(url)
    # Postgres spells the serial primary key differently; SQLite is what
    # production runs, and create_all covers a fresh database.
    pg = engine.dialect.name not in ('sqlite',)
    with engine.begin() as conn:
        if not conn.execute(text('SELECT COUNT(*) FROM employees')).scalar():
            raise SystemExit('Refusing to run: not the production database.')
        have = set(inspect(conn).get_table_names())

        if args.down:
            present = [t for t in reversed(TABLES) if t in have]
            if args.check or not present:
                print(f'== DRY-RUN == WOULD drop {present or "nothing"}')
                print('  This loses every captured intelligence item, every '
                      'project fact sheet, the vendor and vessel registers '
                      'and the port-call log. The projects themselves and '
                      'their timelines survive — they live in `projects` '
                      'and `project_updates`, which this script never '
                      'touched.')
                print('  Master Data vocabularies are NOT removed: an '
                      'administrator may have edited them, and dropping a '
                      'list somebody curated is not a rollback.')
                return
            if not args.yes:
                raise SystemExit('  Refusing without --yes.')
            for t in present:
                conn.execute(text(f'DROP TABLE {t}'))
                print(f'  - {t}')
            return

        missing = [t for t in TABLES if t not in have]
        if args.check:
            print('== DRY-RUN — nothing written ==')
            print(f'  WOULD create: {missing or "(already there)"}')
            lists, items = seed_master_data(conn, dry_run=True)
            print(f'  WOULD register Master Data lists: {lists or "(all there)"}')
            print(f'  WOULD seed {len(items)} Master Data value(s).')
            print('  No intelligence is seeded — not one project, vessel '
                  'or port call. The registers start empty on purpose.')
            return

        for t in TABLES:
            if t in have:
                continue
            ddl = DDL[t]
            if pg:                                      # pragma: no cover
                ddl = ddl.replace('INTEGER PRIMARY KEY', 'SERIAL PRIMARY KEY')
            conn.execute(text(ddl))
            print(f'  + {t}')
        for name, table, col in INDEXES:
            conn.execute(text(f'CREATE INDEX IF NOT EXISTS {name} '
                              f'ON {table} ({col})'))
        lists, items = seed_master_data(conn)
        for key in lists:
            print(f'  + master list {key}')
        if items:
            print(f'  + {len(items)} master data value(s)')
    print('  done. No intelligence rows were created; the registers are '
          'empty until somebody captures something or a subscription is '
          'configured.')


if __name__ == '__main__':
    main()
