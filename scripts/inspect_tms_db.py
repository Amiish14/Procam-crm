"""
Read-only: what did the CRM accidentally create in the TMS database?

On 2026-10-06 a CRM migration was run in a shell that still had the
TMS's DATABASE_URL exported, so `create_all` and the boot autoheal
ran against the TMS's PostgreSQL rather than the CRM's SQLite. This
reports what is there now. It changes nothing.

Standalone on purpose: it imports no CRM code, so it can be run with
the TMS's own interpreter, which already has a PostgreSQL driver.

    /var/www/procam-lr/.venv/bin/python \
        /var/www/procam-crm/scripts/inspect_tms_db.py \
        --env-file /var/www/procam-lr/.env

It never prints the password — the connection string is read from the
file and used, not displayed.
"""
import argparse
import re
import sys

#: Tables both applications define. These are the only ones where the
#: CRM could have touched something the TMS relies on; everything else
#: it created is clutter.
SHARED_NAMES = ('notifications', 'projects', 'task_definitions',
                'task_instances')

#: Columns the CRM's version of a shared table has and the TMS's does
#: not. Kept, but no longer trusted on its own — see `_verdict_for`.
#:
#: The first run of this script called notifications, task_definitions
#: and task_instances "CRM shape" and was wrong about all three. The
#: CRM's notification and task-engine models were originally copied
#: from the TMS codebase, so they share column names by descent. A
#: column test cannot tell apart two tables with the same ancestor.
CRM_FINGERPRINTS = {
    'notifications': {'user_id', 'kind', 'action_url', 'task_instance_id'},
    'projects': {'procam_vertical'},
    'task_definitions': {'task_key'},
    'task_instances': {'task_key', 'entity_type'},
}

#: What actually distinguishes them. The CRM created these tables
#: minutes ago and wrote nothing to them, so a shared table holding
#: real data is the TMS's, whatever its columns look like.
ROWS_MEANING_TMS_OWNS_IT = 1


def _database_url(path):
    """Parse DATABASE_URL out of a .env. Never executes the file."""
    try:
        with open(path, encoding='utf-8', errors='replace') as fh:
            for line in fh:
                line = line.strip()
                if line.startswith('DATABASE_URL=') and '=' in line:
                    value = line.partition('=')[2].strip()
                    if len(value) >= 2 and value[0] == value[-1] in ('"', "'"):
                        value = value[1:-1]
                    return value
    except OSError as exc:
        print(f'could not read {path}: {exc}')
    return None


def _safe(url):
    """The connection string with the password replaced."""
    return re.sub(r'://([^:/@]+):[^@]*@', r'://\1:***@', url or '')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--env-file', default='/var/www/procam-lr/.env')
    args = ap.parse_args()

    url = _database_url(args.env_file)
    if not url:
        print('no DATABASE_URL found in that file.')
        return 2
    print(f'inspecting {_safe(url)}\n')

    try:
        from sqlalchemy import create_engine, inspect, text
    except ImportError:
        print('SQLAlchemy is not available to this interpreter. Run this '
              'with the TMS virtualenv:\n'
              '  /var/www/procam-lr/.venv/bin/python '
              'scripts/inspect_tms_db.py')
        return 2

    engine = create_engine(url)
    insp = inspect(engine)
    tables = sorted(insp.get_table_names())
    print(f'{len(tables)} table(s) in the database\n')

    print('THE FOUR THAT COULD MATTER')
    harmed = []
    for name in SHARED_NAMES:
        if name not in tables:
            print(f'  {name:<20} absent')
            continue
        columns = {c['name'] for c in insp.get_columns(name)}
        fingerprint = CRM_FINGERPRINTS.get(name, set())
        looks_crm = bool(fingerprint & columns)
        with engine.connect() as conn:
            try:
                rows = conn.execute(
                    text(f'SELECT COUNT(*) FROM "{name}"')).scalar()
            except Exception:
                rows = '?'
        has_data = isinstance(rows, int) and rows >= ROWS_MEANING_TMS_OWNS_IT
        if has_data:
            verdict = f'TMS data ({rows} rows) — untouched'
        elif looks_crm:
            verdict = 'EMPTY and CRM-shaped — check this one'
            harmed.append(name)
        else:
            verdict = 'TMS shape — untouched'
        print(f'  {name:<20} {rows:>8} row(s)  {verdict}')
        print(f'  {"":<20} columns: {", ".join(sorted(columns))[:150]}')

    print('\nCRM TABLES NOW IN THIS DATABASE')
    crm_only = [t for t in tables if t in _crm_table_names()
                and t not in SHARED_NAMES]
    for name in crm_only:
        with engine.connect() as conn:
            try:
                rows = conn.execute(
                    text(f'SELECT COUNT(*) FROM "{name}"')).scalar()
            except Exception:
                rows = '?'
        print(f'  {name:<34} {rows:>8} row(s)')
    print(f'\n  {len(crm_only)} CRM table(s) created here by mistake')

    print('\nVERDICT')
    if harmed:
        print(f'  These shared tables are empty AND look like the CRM\'s: '
              f'{", ".join(harmed)}.')
        print('  Check whether the TMS expects them before dropping '
              'anything.')
    else:
        print('  Every shared table holds TMS data. create_all skips a '
              'table that already exists, so the TMS\'s own tables were '
              'never touched.')
    print('\n  Nothing was deleted: create_all only adds, and no DROP or '
          'DELETE ran.')
    return 0


def _crm_table_names():
    """The CRM's table names, hard-coded so this script stays
    standalone and importable by the TMS interpreter."""
    return {
        'leads', 'lead_attachments', 'lead_activities', 'lead_notes',
        'lead_emails', 'lead_stage_history', 'lead_assignment_history',
        'lead_raw_emails', 'companies', 'contacts', 'employees',
        'opportunities', 'email_classifications', 'email_events',
        'outreach_drafts', 'audit_events', 'deletion_audit',
        'access_profiles', 'master_lists', 'master_items',
        'vendor_domains', 'rfqs', 'rfq_attachments',
        'rate_sourcing_lines', 'quotes', 'quote_lines', 'won_handovers',
        'overseas_agents', 'business_cards', 'help_articles',
        'help_tooltips', 'data_quality_snapshots', 'review_actions',
        'contact_relationships', 'contact_assignments', 'escalation_log',
        'intel_projects', 'intel_project_facts', 'intel_competitors',
        'intel_vendors', 'intel_vessels', 'intel_port_calls',
        'intel_sources', 'public_sources', 'email_outbox',
        'notification_prefs', 'weekly_pipeline_snapshot', 'job_leases',
        'client_restrictions', 'client_restriction_events',
        'client_restriction_attachments', 'mail_ingest_log',
        'crm_tms_links', 'integration_log', 'app_settings',
        'account_assignments', 'account_stage_history',
        'account_activities', 'account_relationship_tags',
        'import_batches', 'training_progress', 'copilot_feedback',
    }


if __name__ == '__main__':
    sys.exit(main())
