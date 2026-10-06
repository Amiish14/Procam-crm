"""
Every table the CRM defines, from the CRM itself.

Written because a hand-kept list was wrong. The cleanup of the CRM
tables accidentally created in the TMS database used a list I typed
out from an earlier report; it missed `competitors`,
`project_accounts`, `project_contacts` and `opportunity_source_links`,
which only surfaced because a foreign key refused to drop. A list of
sixty table names maintained by hand will always be missing four.

SQLAlchemy already knows. This asks it.

    .venv/bin/python scripts/crm_table_names.py
    .venv/bin/python scripts/crm_table_names.py --out /tmp/crm_tables.txt

Run with the CRM's own interpreter — it imports the CRM app, which
needs the CRM's dependencies and reads the CRM's own database.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import app, db                                   # noqa: E402


def table_names():
    """Every table in the CRM's metadata, after every model is loaded.

    The import order matters: a model that is never imported is not in
    the metadata, which is the same mistake in a different shape. The
    boot sequence in app.py loads them all, so doing this inside an
    application context gets the complete set.
    """
    with app.app_context():
        import importlib
        for module in ('app.models.audit', 'app.models.data_quality',
                       'presales.models', 'presales.models_projects',
                       'app.models.rfq', 'app.models.quote',
                       'app.models.tms_handover', 'app.models.competitor',
                       'app.models.public_source', 'app.models.task_engine',
                       'app.models.notification', 'app.models.review',
                       'app.models.escalation', 'app.models.intel',
                       'app.models.mailops', 'app.models.restriction',
                       'app.models.ingest_log', 'app.models.integration',
                       'app.models.master_data', 'app.models.access',
                       'app.models.help_content', 'app.directory.models'):
            try:
                importlib.import_module(module)
            except Exception:
                pass
        return sorted(db.metadata.tables.keys())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', help='write the list here, one name per line')
    args = ap.parse_args()

    names = table_names()
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(names) + '\n')
        print(f'{len(names)} table name(s) written to {args.out}')
    else:
        for name in names:
            print(name)
        print(f'\n{len(names)} table(s)', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
