"""
Lead Intake Engine — schema for Phases 0 to 2.

Phase 0 is the part that cannot wait: conversation and RFC-822 threading
headers are what let a reply be recognised as a reply, they are now being
requested from Graph, and they need somewhere to land. They cannot be
backfilled — a message not captured with its headers is a thread we can
never reconstruct.

Adds
    leads               conversation_id, in_reply_to, references_header,
                        classification, lead_confidence, duplicate_score,
                        rejection_reason
    lead_emails         conversation_id, in_reply_to, references_header
    email_events        classification, confidence, duplicate_score,
                        matched_lead_id, decided_by
    companies           secondary_pic_emp_code, backup_pic_emp_code,
                        vertical, email_domains
    vendor_domains      new
    email_classifications  new

Also seeds companies.email_domains from the website column where one is
present, so domain matching works on day one rather than after a data
entry exercise, and seeds an initial vendor list.

Usage
    python scripts/2026_09_26_lead_intake_engine.py --check
    python scripts/2026_09_26_lead_intake_engine.py
    python scripts/2026_09_26_lead_intake_engine.py --down --yes

Back up first:
    cp procam_crm.db procam_crm.db.bak-$(date +%F-%H%M)

Nothing about ingestion behaviour changes here. The classifier reads
these columns, but the migration only creates them.
"""
import argparse
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:
    pass

from sqlalchemy import create_engine, text          # noqa: E402


COLUMNS = {
    'leads': [
        ('conversation_id', 'VARCHAR(200)'),
        ('in_reply_to', 'VARCHAR(400)'),
        ('references_header', 'TEXT'),
        ('classification', 'VARCHAR(32)'),
        ('lead_confidence', 'INTEGER'),
        ('duplicate_score', 'INTEGER'),
        ('rejection_reason', 'VARCHAR(80)'),
    ],
    'lead_emails': [
        ('conversation_id', 'VARCHAR(200)'),
        ('in_reply_to', 'VARCHAR(400)'),
        ('references_header', 'TEXT'),
    ],
    'email_events': [
        ('classification', 'VARCHAR(32)'),
        ('confidence', 'INTEGER'),
        ('duplicate_score', 'INTEGER'),
        ('matched_lead_id', 'INTEGER'),
        ('decided_by', 'VARCHAR(40)'),
    ],
    'companies': [
        ('secondary_pic_emp_code', 'VARCHAR(20)'),
        ('backup_pic_emp_code', 'VARCHAR(20)'),
        ('vertical', 'VARCHAR(80)'),
        ('email_domains', 'JSON'),
    ],
}

INDEXES = [
    ('ix_leads_conversation_id', 'leads', 'conversation_id'),
    ('ix_leads_in_reply_to', 'leads', 'in_reply_to'),
    ('ix_leads_classification', 'leads', 'classification'),
    ('ix_lead_emails_conversation_id', 'lead_emails', 'conversation_id'),
    ('ix_lead_emails_in_reply_to', 'lead_emails', 'in_reply_to'),
    ('ix_companies_secondary_pic', 'companies', 'secondary_pic_emp_code'),
    ('ix_companies_vertical', 'companies', 'vertical'),
    ('ix_email_events_classification', 'email_events', 'classification'),
]

VENDOR_DOMAINS_DDL = """
CREATE TABLE IF NOT EXISTS vendor_domains (
    id              INTEGER PRIMARY KEY,
    domain          VARCHAR(200) NOT NULL UNIQUE,
    vendor_type     VARCHAR(40),
    learned_from    VARCHAR(40),
    rejection_count INTEGER DEFAULT 0,
    is_active       BOOLEAN DEFAULT 1,
    added_by        VARCHAR(20),
    created_at      DATETIME
)
"""

CLASSIFICATIONS_DDL = """
CREATE TABLE IF NOT EXISTS email_classifications (
    id              INTEGER PRIMARY KEY,
    message_id      VARCHAR(400),
    conversation_id VARCHAR(200),
    subject         VARCHAR(500),
    from_addr       VARCHAR(320),
    from_domain     VARCHAR(200),
    classification  VARCHAR(32) NOT NULL,
    decided_by      VARCHAR(40),
    reason          VARCHAR(300),
    confidence      INTEGER,
    duplicate_score INTEGER,
    matched_lead_id INTEGER,
    created_lead_id INTEGER,
    corrected_to      VARCHAR(32),
    correction_reason VARCHAR(80),
    corrected_by      VARCHAR(20),
    corrected_at      DATETIME,
    created_at      DATETIME
)
"""

#: Twenty structured reasons, per §10 of the brief. They go into the
#: existing master_items vocabulary so Admin can add one without a
#: deployment.
REJECTION_REASONS = [
    'Duplicate Lead', 'Existing Lead Communication', 'Reply to Existing RFQ',
    'Forward of Existing RFQ', 'Internal Procam Email',
    'Vendor Rate Sourcing', 'Shipping Line Rate Sourcing',
    'Transporter Rate Sourcing', 'Quote Submission', 'Customer Follow-up',
    'Vendor / Supplier Communication', 'Accounts / Payment Email',
    'Operations Email', 'Documentation Email', 'Spam / Marketing',
    'Job Application / HR Email', 'Tender Update – Not New Tender',
    'Existing Project / Job Communication', 'Test Email', 'Other',
]

#: A conservative starting list. Anything ambiguous is left out — a
#: domain wrongly marked a vendor stops a real customer becoming a lead,
#: which is the expensive direction of this mistake.
SEED_VENDORS = [
    ('maersk.com', 'shipping line'), ('msc.com', 'shipping line'),
    ('cma-cgm.com', 'shipping line'), ('hapag-lloyd.com', 'shipping line'),
    ('oocl.com', 'shipping line'), ('evergreen-line.com', 'shipping line'),
    ('cosco.com', 'shipping line'), ('one-line.com', 'shipping line'),
    ('emirates.com', 'airline'), ('qatarairways.com', 'airline'),
    ('lufthansa-cargo.com', 'airline'),
]


def _db_url():
    return os.environ.get('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))


def _guard(conn):
    try:
        n = conn.execute(text('SELECT COUNT(*) FROM employees')).scalar()
    except Exception:
        n = 0
    if not n:
        raise SystemExit(
            'Refusing to run: no employees in this database, so it is not '
            'the production one. Check DATABASE_URL / .env.')
    return n


def _cols(conn, table):
    rows = conn.execute(text(f'PRAGMA table_info({table})')).fetchall()
    return {r[1] for r in rows}


def _has_table(conn, name):
    return bool(conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"),
        {'n': name}).fetchone())


def _domain_from_website(url):
    s = (url or '').strip().lower()
    if not s:
        return None
    s = re.sub(r'^https?://', '', s).split('/')[0].split('?')[0].strip('.')
    if s.startswith('www.'):
        s = s[4:]
    if '.' not in s or ' ' in s:
        return None
    return s


def up(conn, dry):
    missing = {}
    for table, cols in COLUMNS.items():
        if not _has_table(conn, table):
            continue
        have = _cols(conn, table)
        todo = [(c, t) for c, t in cols if c not in have]
        if todo:
            missing[table] = todo

    seedable = conn.execute(text(
        "SELECT COUNT(*) FROM companies WHERE website IS NOT NULL "
        "AND website != ''")).scalar() or 0

    if dry:
        print('== DRY-RUN — nothing written ==')
        for table, todo in missing.items():
            print(f'  WOULD add to {table}: '
                  f'{", ".join(c for c, _ in todo)}')
        if not missing:
            print('  WOULD add columns: (none — already there)')
        print(f'  WOULD create: vendor_domains, email_classifications')
        print(f'  WOULD seed {len(SEED_VENDORS)} vendor domain(s)')
        print(f'  WOULD seed {len(REJECTION_REASONS)} rejection reason(s)')
        print(f'  WOULD derive email_domains for up to {seedable} '
              f'company/companies that have a website')
        print('\n  No ingestion behaviour changes. Existing leads untouched.')
        return

    for table, todo in missing.items():
        for col, coltype in todo:
            conn.execute(text(
                f'ALTER TABLE {table} ADD COLUMN {col} {coltype}'))
            print(f'  + {table}.{col}')

    conn.execute(text(VENDOR_DOMAINS_DDL))
    conn.execute(text(CLASSIFICATIONS_DDL))
    print('  + vendor_domains, email_classifications')

    for name, table, col in INDEXES:
        if _has_table(conn, table) and col in _cols(conn, table):
            conn.execute(text(
                f'CREATE INDEX IF NOT EXISTS {name} ON {table} ({col})'))
    conn.execute(text('CREATE INDEX IF NOT EXISTS '
                      'ix_email_classifications_class '
                      'ON email_classifications (classification)'))
    conn.execute(text('CREATE INDEX IF NOT EXISTS '
                      'ix_email_classifications_domain '
                      'ON email_classifications (from_domain)'))

    # Derive a mail domain from each website. Domain matching is what
    # makes auto-assignment work on day one; without this every account
    # would need its domains typed in by hand first.
    derived = 0
    rows = conn.execute(text(
        "SELECT id, website, email_domains FROM companies "
        "WHERE website IS NOT NULL AND website != ''")).fetchall()
    for cid, website, existing in rows:
        if existing:
            continue
        domain = _domain_from_website(website)
        if not domain:
            continue
        conn.execute(text(
            'UPDATE companies SET email_domains = :d WHERE id = :i'),
            {'d': json.dumps([domain]), 'i': cid})
        derived += 1
    print(f'  derived email_domains for {derived} company/companies')

    seeded = 0
    for domain, kind in SEED_VENDORS:
        exists = conn.execute(text(
            'SELECT 1 FROM vendor_domains WHERE domain = :d'),
            {'d': domain}).fetchone()
        if exists:
            continue
        conn.execute(text(
            'INSERT INTO vendor_domains (domain, vendor_type, learned_from, '
            'rejection_count, is_active, created_at) '
            "VALUES (:d, :t, 'manual', 0, 1, CURRENT_TIMESTAMP)"),
            {'d': domain, 't': kind})
        seeded += 1
    print(f'  seeded {seeded} vendor domain(s)')
    print('\n  No ingestion behaviour changes. Existing leads untouched.')


def seed_reasons():
    """Rejection reasons go in the existing master_items vocabulary, which
    already refuses to delete a value still in use."""
    import re as _re
    from app import app as flask_app                          # noqa: E402
    from app.master_data import service as md                 # noqa: E402

    with flask_app.app_context():
        # Registers the new lead_rejection_reason list alongside the
        # existing vocabularies.
        md.ensure_lists()
        added = 0
        try:
            existing = {i.label for i in md.items('lead_rejection_reason',
                                                  include_inactive=True)}
        except Exception:
            existing = set()
        for label in REJECTION_REASONS:
            if label in existing:
                continue
            code = _re.sub(r'[^a-z0-9]+', '_', label.lower()).strip('_')
            try:
                md.add_item('lead_rejection_reason', code, label=label)
                added += 1
            except Exception as exc:
                print(f'    !! could not add {label!r}: {exc}')
        print(f'  seeded {added} rejection reason(s) into master data')


def down(conn, dry, confirmed):
    counts = {}
    for tbl in ('vendor_domains', 'email_classifications'):
        if _has_table(conn, tbl):
            counts[tbl] = conn.execute(text(
                f'SELECT COUNT(*) FROM {tbl}')).scalar() or 0

    print('  This DISCARDS:')
    for tbl, n in counts.items():
        print(f'    {n} row(s) in {tbl}')
    print('    the thread headers and classifications on leads')
    print('    the second and backup owners on every account')

    if dry:
        print('== DRY-RUN — nothing written ==')
        return
    if not confirmed:
        raise SystemExit('\n  Refusing to drop data without --yes.')

    for name, _t, _c in INDEXES:
        conn.execute(text(f'DROP INDEX IF EXISTS {name}'))
    for table, cols in COLUMNS.items():
        if not _has_table(conn, table):
            continue
        have = _cols(conn, table)
        for col, _t in cols:
            if col in have:
                conn.execute(text(
                    f'ALTER TABLE {table} DROP COLUMN {col}'))
                print(f'  - {table}.{col}')
    conn.execute(text('DROP TABLE IF EXISTS vendor_domains'))
    conn.execute(text('DROP TABLE IF EXISTS email_classifications'))
    print('  - vendor_domains, email_classifications')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()

    url = _db_url()
    print(f'database: {url}')
    engine = create_engine(url)
    with engine.begin() as conn:
        print(f'  employees: {_guard(conn)}')
        if args.down:
            down(conn, args.check, args.yes)
        else:
            up(conn, args.check)

    if not args.down and not args.check:
        seed_reasons()
    print('  done.' if not args.check else '== end dry-run ==')


if __name__ == '__main__':
    main()
