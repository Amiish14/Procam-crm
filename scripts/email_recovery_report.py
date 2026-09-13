"""
Family A — where every lead's original email stands. Read-only.

Four numbers the audit asks for, and the fifth that explains the gap:

    intact         the enquiry is the one ingest stored, untouched
    restored       it was damaged, and read back out of the mailbox
    unrecoverable  it was damaged and is still missing
    preserved      the damaged text survives as a flagged legacy note
    blocked        why the unrecoverable ones are still unrecoverable

Writes nothing, contacts nothing. Recovery itself is
scripts/2026_09_22_recover_lost_emails.py, and it needs Graph access the
tenant has not granted — this report says so rather than implying the
work is finished.

    python scripts/email_recovery_report.py
    python scripts/email_recovery_report.py --list
"""
import argparse
import os
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


def _looks_like_an_email():
    """The recovery script's own test for "this text is an email", so the
    report and the script agree on what counts as damaged. Loaded by
    path (the file name starts with a digit); it imports no app code at
    module level."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'recover_lost_emails',
        os.path.join(_HERE, '2026_09_22_recover_lost_emails.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.looks_like_an_email


def _db_url():
    return os.environ.get('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))


def counts(conn):
    """The report as a dict, so a test can hold it to account."""
    def one(sql):
        return conn.execute(text(sql)).scalar() or 0

    cols = {r[1] for r in conn.execute(text('PRAGMA table_info(leads)'))}
    if 'original_email_source' not in cols:
        return {'error': 'leads.original_email_source is missing — the '
                         'notes/email split migration has not run.'}

    email_leads = one("SELECT COUNT(*) FROM leads WHERE source = 'email'")
    intact = one(
        "SELECT COUNT(*) FROM leads WHERE source = 'email' "
        "AND COALESCE(original_email_body, '') != '' "
        "AND COALESCE(original_email_source, 'ingested') "
        "    NOT IN ('migrated_from_notes', 'recovered_from_mailbox')")
    restored = one(
        "SELECT COUNT(*) FROM leads "
        "WHERE original_email_source = 'recovered_from_mailbox'")
    unrecoverable = one(
        "SELECT COUNT(*) FROM leads "
        "WHERE original_email_source = 'migrated_from_notes'")
    no_email = one(
        "SELECT COUNT(*) FROM leads WHERE source = 'email' "
        "AND COALESCE(original_email_body, '') = ''")

    notes_cols = {r[1] for r in conn.execute(text(
        'PRAGMA table_info(lead_notes)'))}
    preserved = (one("SELECT COUNT(*) FROM lead_notes "
                     "WHERE migrated_from_legacy = 1")
                 if 'migrated_from_legacy' in notes_cols else 0)

    # The split migration flagged every lead whose enquiry field might
    # have been overwritten. Some of those still hold a real email; the
    # recovery script leaves them alone, so the percentage must too —
    # counting them as damaged would understate the recovery.
    looks = _looks_like_an_email()
    flagged = conn.execute(text(
        "SELECT original_email_body, COALESCE(email_message_id, '') "
        "FROM leads WHERE original_email_source = 'migrated_from_notes'"
    )).fetchall()
    still_damaged = [mid for body, mid in flagged if not looks(body)]
    no_mid = sum(1 for mid in still_damaged if not mid)
    damaged = restored + len(still_damaged)
    return {'email_leads': email_leads, 'intact': intact,
            'restored': restored, 'unrecoverable': unrecoverable,
            'still_damaged': len(still_damaged),
            'flagged_but_readable': unrecoverable - len(still_damaged),
            'unrecoverable_no_message_id': no_mid,
            'preserved_as_note': preserved,
            'email_lead_with_no_body': no_email,
            'damaged': damaged,
            'recovery_pct': (round(100.0 * restored / damaged, 1)
                             if damaged else None)}


def graph_blocker():
    """Why mailbox recovery has not run, in words an admin can act on."""
    # The names email_ingest/graph_client.py reads. This used to check
    # GRAPH_*, which nothing sets, so a configured server was always
    # reported as unconfigured.
    need = [k for k in ('MS_TENANT_ID', 'MS_CLIENT_ID', 'MS_CLIENT_SECRET')
            if not os.environ.get(k)]
    if need:
        return ('Graph credentials are not configured on this host: '
                + ', '.join(need) + '.')
    return ('Credentials are present. The last recovery attempt was '
            'refused by Microsoft Graph with HTTP 403 — the app registration '
            'needs Mail.Read (Application) on leads@procamgroup.in with admin '
            'consent. That is a tenant-admin action, not a code change.')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true',
                    help='list the leads still unrecoverable')
    args = ap.parse_args()

    with create_engine(_db_url()).connect() as conn:
        c = counts(conn)
        if 'error' in c:
            print(f'  {c["error"]}')
            return 1

        print(f'\n  Email-sourced leads            {c["email_leads"]}')
        print(f'    original enquiry intact       {c["intact"]}')
        print(f'    restored from the mailbox     {c["restored"]}')
        print(f'    flagged, not yet restored     {c["unrecoverable"]}')
        print(f'      of which still damaged      {c["still_damaged"]}')
        print(f'      of which text reads as email '
              f'{c["flagged_but_readable"]}')
        print(f'    no body at all                {c["email_lead_with_no_body"]}')
        print(f'\n  Damaged text kept as a flagged note   '
              f'{c["preserved_as_note"]}')
        pct = c['recovery_pct']
        print(f'  Recovery: {c["restored"]} of {c["damaged"]} damaged '
              f'enquiries restored'
              + (f' ({pct}%)' if pct is not None else ' (none damaged)'))
        if c['unrecoverable_no_message_id']:
            print(f'  {c["unrecoverable_no_message_id"]} have no message id '
                  f'and cannot be looked up in the mailbox at all.')

        if c['unrecoverable']:
            print(f'\n  BLOCKED: {graph_blocker()}')
            print('  Nothing on those leads has been overwritten — the text '
                  'that was there is kept as a flagged note.')
            if args.list:
                rows = conn.execute(text(
                    "SELECT id, company, email_message_id FROM leads "
                    "WHERE original_email_source = 'migrated_from_notes' "
                    "ORDER BY id")).fetchall()
                for lid, company, mid in rows:
                    print(f'    #{lid:<6} {(company or "")[:34]:<36} '
                          f'{"has message id" if mid else "NO message id"}')
        else:
            print('\n  Nothing left to recover.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
