"""
Separate the original email from the Notes / call summary.  Bug fix.

The problem
    email_ingest wrote the customer's enquiry into Lead.notes, and the
    Notes / call summary box on the lead screen read, edited and saved
    that same field.  Writing a note therefore destroyed the enquiry —
    scope, payment terms, deadlines, contacts, all of it.

What this does
    1. Adds original_email_* to leads, plus lead_emails and lead_notes.
    2. Copies every Lead.notes into original_email_body for leads that
       came from email — preserving what is there NOW, before anyone
       overwrites more of it.
    3. Copies the same text into lead_notes as well, flagged
       migrated_from_legacy.  Whichever the text actually is — the
       enquiry or somebody's note — it survives in the right place, and
       the flag says it has not been verified.
    4. Seeds the email trail from the preserved body.
    5. Reports leads whose enquiry looks like it was already destroyed,
       so they can be re-imported from the mailbox.

    Lead.notes itself is left untouched.  Nothing reads it any more, and
    clearing it would be a second destructive act on top of the first.

Usage
    python scripts/2026_09_21_split_notes_from_email.py --check
    python scripts/2026_09_21_split_notes_from_email.py
    python scripts/2026_09_21_split_notes_from_email.py --down --yes

Back up first:
    cp procam_crm.db procam_crm.db.bak-$(date +%F-%H%M)
"""
import argparse
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


COLUMNS = [
    ('original_email_subject', 'VARCHAR(500)'),
    ('original_email_from', 'VARCHAR(320)'),
    ('original_email_received_at', 'DATETIME'),
    ('original_email_body', 'TEXT'),
    ('original_email_source', 'VARCHAR(32)'),
]

LEAD_EMAILS_DDL = """
CREATE TABLE IF NOT EXISTS lead_emails (
    id        INTEGER PRIMARY KEY,
    lead_id   INTEGER NOT NULL REFERENCES leads(id),
    direction VARCHAR(10) NOT NULL,
    from_addr VARCHAR(320),
    to_addr   TEXT,
    cc        TEXT,
    subject   VARCHAR(500),
    body      TEXT,
    sent_or_received_at DATETIME,
    created_by VARCHAR(20),
    source    VARCHAR(32),
    status    VARCHAR(16),
    message_id VARCHAR(400),
    created_at DATETIME
)
"""

LEAD_NOTES_DDL = """
CREATE TABLE IF NOT EXISTS lead_notes (
    id         INTEGER PRIMARY KEY,
    lead_id    INTEGER NOT NULL REFERENCES leads(id),
    note_text  TEXT NOT NULL,
    note_type  VARCHAR(24),
    author     VARCHAR(20),
    author_name VARCHAR(100),
    migrated_from_legacy BOOLEAN DEFAULT 0,
    is_deleted BOOLEAN DEFAULT 0,
    created_at DATETIME,
    updated_at DATETIME
)
"""

#: Text that reads like an email rather than a call note.
_EMAIL_MARKERS = re.compile(
    r'(^|\n)\s*(from|to|sent|subject|cc)\s*:|dear\s|regards|thanks\s*&|'
    r'sincerely|unsubscribe|@[\w.-]+\.\w+', re.I)


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


def _lead_columns(conn):
    rows = conn.execute(text('PRAGMA table_info(leads)')).fetchall()
    if not rows:
        raise SystemExit('Refusing to run: no leads table.')
    return {r[1] for r in rows}


def looks_like_an_email(body):
    """Whether this text reads like a received email.

    Only a signal, never a decision: everything is preserved either way,
    and this decides what gets *reported* as probably damaged.
    """
    if not body:
        return False
    if len(body) > 400:
        return True
    return bool(_EMAIL_MARKERS.search(body))


def survey(conn):
    rows = conn.execute(text(
        "SELECT id, company, notes, email_message_id "
        "FROM leads WHERE notes IS NOT NULL AND notes != ''")).fetchall()
    from_email, looks_lost, plain = [], [], []
    for lid, company, notes, mid in rows:
        if not mid:
            plain.append((lid, company))
            continue
        from_email.append((lid, company, notes, mid))
        if not looks_like_an_email(notes):
            looks_lost.append((lid, company, (notes or '')[:60], mid))
    return from_email, looks_lost, plain


def up(conn, dry):
    have = _lead_columns(conn)
    to_add = [(c, t) for c, t in COLUMNS if c not in have]
    from_email, looks_lost, plain = survey(conn)

    if dry:
        print('== DRY-RUN — nothing written ==')
        print(f'  WOULD add columns: '
              f'{", ".join(c for c, _ in to_add) or "(none — already there)"}')
        print('  WOULD create lead_emails, lead_notes')
        print(f'  WOULD preserve {len(from_email)} email-sourced lead(s) '
              f'into original_email_body')
        print(f'  WOULD copy the same text into lead_notes (flagged)')
        print(f'  leads with notes but no source email: {len(plain)} '
              f'(left alone — these are genuine notes)')
        print(f'\n  Probably already destroyed by a note: {len(looks_lost)}')
        for lid, company, snippet, _mid in looks_lost[:15]:
            print(f'    lead #{lid:<6} {(company or "")[:28]:<30} '
                  f'{snippet!r}')
        if len(looks_lost) > 15:
            print(f'    … and {len(looks_lost) - 15} more')
        if looks_lost:
            print('\n  These still have email_message_id, so the message can '
                  'be re-fetched\n  from the mailbox — see --report-lost.')
        return

    for col, coltype in to_add:
        conn.execute(text(f'ALTER TABLE leads ADD COLUMN {col} {coltype}'))
        print(f'  + leads.{col}')
    conn.execute(text(LEAD_EMAILS_DDL))
    conn.execute(text('CREATE INDEX IF NOT EXISTS ix_lead_emails_lead_id '
                      'ON lead_emails (lead_id)'))
    conn.execute(text(LEAD_NOTES_DDL))
    conn.execute(text('CREATE INDEX IF NOT EXISTS ix_lead_notes_lead_id '
                      'ON lead_notes (lead_id)'))
    print('  + lead_emails, lead_notes')

    # Preserve, twice: as the email (what it almost always is) and as a
    # flagged note (in case this lead was already overwritten). Cheap
    # insurance against a judgement call made on a heuristic.
    preserved = conn.execute(text(
        "UPDATE leads SET original_email_body = notes, "
        "original_email_source = 'migrated_from_notes', "
        "original_email_received_at = COALESCE(original_email_received_at, "
        "                                      created_at) "
        "WHERE notes IS NOT NULL AND notes != '' "
        "AND email_message_id IS NOT NULL "
        "AND (original_email_body IS NULL OR original_email_body = '')"
    )).rowcount
    print(f'  preserved into original_email_body: {preserved}')

    noted = conn.execute(text(
        "INSERT INTO lead_notes (lead_id, note_text, note_type, author, "
        "  author_name, migrated_from_legacy, is_deleted, created_at, "
        "  updated_at) "
        "SELECT id, notes, 'general', NULL, 'migrated', 1, 0, "
        "       created_at, created_at "
        "FROM leads WHERE notes IS NOT NULL AND notes != '' "
        "AND email_message_id IS NOT NULL "
        "AND id NOT IN (SELECT lead_id FROM lead_notes "
        "               WHERE migrated_from_legacy = 1)"
    )).rowcount
    print(f'  copied into lead_notes (flagged migrated): {noted}')

    seeded = conn.execute(text(
        "INSERT INTO lead_emails (lead_id, direction, from_addr, subject, "
        "  body, sent_or_received_at, source, status, message_id, created_at) "
        "SELECT id, 'inbound', email, NULL, original_email_body, "
        "       COALESCE(original_email_received_at, created_at), "
        "       'migrated_from_notes', 'received', email_message_id, "
        "       created_at "
        "FROM leads WHERE original_email_body IS NOT NULL "
        "AND original_email_body != '' "
        "AND id NOT IN (SELECT lead_id FROM lead_emails WHERE direction='inbound')"
    )).rowcount
    print(f'  seeded into the email trail: {seeded}')
    print(f'\n  leads.notes is left untouched — nothing reads it now, and '
          f'clearing it\n  would be a second destructive act on top of the '
          f'first.')
    if looks_lost:
        print(f'\n  !! {len(looks_lost)} lead(s) look like the enquiry was '
              f'already destroyed.')
        print('     Their text is preserved as a flagged note. Re-fetch the '
              'message with\n     --report-lost to get the id list.')


def report_lost(conn, out_path):
    import csv
    _fe, looks_lost, _p = survey(conn)
    if not looks_lost:
        print('  no leads look damaged.')
        return
    with open(out_path, 'w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(['lead_id', 'company', 'current_text_snippet',
                    'email_message_id'])
        w.writerows(looks_lost)
    print(f'  {len(looks_lost)} lead(s) written to {out_path}')
    print('  Each still has its email_message_id, so the original can be '
          're-fetched from\n  the leads mailbox via Graph.')


def down(conn, dry, confirmed):
    have = _lead_columns(conn)
    present = [c for c, _ in COLUMNS if c in have]
    notes_n = emails_n = 0
    for tbl, var in (('lead_notes', 'notes_n'), ('lead_emails', 'emails_n')):
        try:
            v = conn.execute(text(f'SELECT COUNT(*) FROM {tbl}')).scalar() or 0
        except Exception:
            v = 0
        if var == 'notes_n':
            notes_n = v
        else:
            emails_n = v

    print('  This DISCARDS:')
    print(f'    {notes_n} note(s)')
    print(f'    {emails_n} email trail row(s)')
    print(f'    the preserved original_email_* columns')
    print('  leads.notes is untouched, so the pre-migration state remains.')
    if dry:
        print('== DRY-RUN — nothing written ==')
        print(f'  WOULD drop columns: {", ".join(present) or "(none)"}')
        return
    if not confirmed:
        raise SystemExit('\n  Refusing to drop data without --yes.')
    for col in present:
        conn.execute(text(f'ALTER TABLE leads DROP COLUMN {col}'))
        print(f'  - leads.{col}')
    conn.execute(text('DROP TABLE IF EXISTS lead_notes'))
    conn.execute(text('DROP TABLE IF EXISTS lead_emails'))
    print('  - lead_notes, lead_emails')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--report-lost', metavar='CSV',
                    help='write the probably-damaged leads to a CSV')
    args = ap.parse_args()

    url = _db_url()
    print(f'database: {url}')
    engine = create_engine(url)
    with engine.begin() as conn:
        print(f'  employees: {_guard(conn)}')
        if args.report_lost:
            report_lost(conn, args.report_lost)
        elif args.down:
            down(conn, args.check, args.yes)
        else:
            up(conn, args.check)
    print('  done.' if not args.check else '== end dry-run ==')


if __name__ == '__main__':
    main()
