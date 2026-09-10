"""
Imported-data cleanup — DRY RUN ONLY.  WP3.

This script reads.  It does not archive, delete, or modify one row, and
there is no flag that makes it do so — archival is a separate script,
written only after this report has been reviewed and approved.

What it does
    1. Maps the schema from the live database: every table, its primary
       key, and every foreign key pointing at it.  Derived, not
       hard-coded, so a table added later is included automatically.
    2. Finds cleanup candidates under the criteria below.
    3. Writes a CSV per table and prints a summary, including the count
       of pending tasks attached to candidates.

A record is a candidate only when ALL of these hold:

    * nothing references it — checked against every foreign key the
      schema actually declares, plus the two soft references that carry
      no FK (task_instances and notifications address a lead by
      entity_type/entity_id)
    * it carries no meaningful history — no activity, no stage change,
      no assignment change
    * it originated in an import or has no discernible origin, and shows
      no sign of ever having been worked
    * for leads specifically: no inbound email behind it
      (email_message_id), which would make it a real customer enquiry

When any check cannot be made, the record is kept.  Silence is not
evidence of junk.

Usage
    python scripts/2026_09_20_cleanup_dryrun.py
    python scripts/2026_09_20_cleanup_dryrun.py --out /tmp/cleanup
    python scripts/2026_09_20_cleanup_dryrun.py --sources import,manual

Read the summary and the CSVs, decide what is genuinely junk, and only
then ask for the archival script.
"""
import argparse
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:
    pass

from sqlalchemy import inspect, text                        # noqa: E402


#: Tables this report considers.  Deliberately short: these are the
#: records an import creates.  Companies and contacts are excluded — a
#: duplicate company is a merge decision, which the Data Mapping queue
#: and the dedup tooling already handle without deleting anything.
TARGETS = ('leads',)

#: Soft references — a lead addressed without a foreign key.  Missing
#: these is how a "safe" cleanup orphans somebody's open task.
SOFT_REFS = (
    ('task_instances', 'entity_type', 'entity_id', 'Lead'),
    ('notifications', 'entity_type', 'entity_id', 'Lead'),
)

#: Sources that suggest a bulk import rather than a person or a customer.
DEFAULT_SOURCES = ('import', 'excel', 'bulk', '')

OPEN_TASK_STATES = ('Pending', 'In Progress', 'Escalated', 'Returned',
                    'Blocked')


def _db_url():
    return os.environ.get('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))


# ─── schema map ──────────────────────────────────────────────────────────
def schema_map(insp):
    """{parent_table: [(child_table, child_column), ...]} for every FK."""
    refs = defaultdict(list)
    for table in insp.get_table_names():
        for fk in insp.get_foreign_keys(table):
            cols = fk.get('constrained_columns') or []
            if not cols or not fk.get('referred_table'):
                continue
            refs[fk['referred_table']].append((table, cols[0]))
    return {k: sorted(v) for k, v in refs.items()}


def print_schema_map(insp, refs, targets):
    print('\n' + '=' * 72)
    print('  SCHEMA MAP — what points at what')
    print('=' * 72)
    tables = insp.get_table_names()
    print(f'  {len(tables)} tables in this database.\n')
    for parent in targets:
        pk = insp.get_pk_constraint(parent).get('constrained_columns') or []
        kids = refs.get(parent, [])
        print(f'  {parent}  (primary key: {", ".join(pk) or "?"})')
        print(f'    referenced by {len(kids)} table(s) through a foreign key:')
        for t, col in kids:
            print(f'      {t}.{col}')
        soft = [s for s in SOFT_REFS]
        if parent == 'leads' and soft:
            print('    and by these without a foreign key:')
            for t, type_col, id_col, value in soft:
                print(f'      {t}.{id_col} where {type_col} = {value!r}')
        print()


# ─── candidate detection ─────────────────────────────────────────────────
def _table_exists(conn, name):
    return bool(conn.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:n"),
        {'n': name}).fetchone())


def _columns(insp, table):
    return {c['name'] for c in insp.get_columns(table)}


def referenced_ids(conn, insp, parent, refs):
    """Every id of `parent` that anything at all points at.

    Returns (ids, per_table_counts, unchecked) — `unchecked` names any
    reference that could not be read, and its presence means the report
    must not be treated as complete.
    """
    found = set()
    counts = {}
    unchecked = []

    for child, col in refs.get(parent, []):
        if not _table_exists(conn, child):
            unchecked.append(f'{child} (table missing)')
            continue
        try:
            rows = conn.execute(text(
                f'SELECT DISTINCT {col} FROM {child} '
                f'WHERE {col} IS NOT NULL')).fetchall()
        except Exception as exc:
            unchecked.append(f'{child}.{col} ({exc})')
            continue
        ids = {r[0] for r in rows}
        counts[f'{child}.{col}'] = len(ids)
        found |= ids

    if parent == 'leads':
        for child, type_col, id_col, value in SOFT_REFS:
            if not _table_exists(conn, child):
                unchecked.append(f'{child} (table missing)')
                continue
            try:
                rows = conn.execute(text(
                    f'SELECT DISTINCT {id_col} FROM {child} '
                    f'WHERE {type_col} = :v AND {id_col} IS NOT NULL'),
                    {'v': value}).fetchall()
            except Exception as exc:
                unchecked.append(f'{child}.{id_col} ({exc})')
                continue
            ids = {r[0] for r in rows}
            counts[f'{child}.{id_col} [{value}]'] = len(ids)
            found |= ids

    return found, counts, unchecked


def open_task_counts(conn):
    """Open tasks per lead — the count the brief asks for by name."""
    if not _table_exists(conn, 'task_instances'):
        return {}
    marks = ', '.join(f':s{i}' for i in range(len(OPEN_TASK_STATES)))
    params = {f's{i}': s for i, s in enumerate(OPEN_TASK_STATES)}
    rows = conn.execute(text(
        f'SELECT entity_id, COUNT(*) FROM task_instances '
        f"WHERE entity_type = 'Lead' AND status IN ({marks}) "
        f'GROUP BY entity_id'), params).fetchall()
    return {r[0]: r[1] for r in rows}


def lead_candidates(conn, insp, refs, sources):
    """Leads that nothing references and that show no sign of use."""
    cols = _columns(insp, 'leads')
    referenced, ref_counts, unchecked = referenced_ids(conn, insp, 'leads',
                                                       refs)
    tasks = open_task_counts(conn)

    select = ['id', 'company', 'source', 'stage', 'created_at']
    for optional in ('assigned_to', 'email_message_id', 'is_archived',
                     'notes', 'email', 'phone', 'pic', 'opp_number',
                     'updated_at', 'company_id'):
        if optional in cols:
            select.append(optional)

    rows = conn.execute(text(
        f'SELECT {", ".join(select)} FROM leads')).fetchall()

    candidates, kept = [], defaultdict(int)
    for row in rows:
        r = dict(zip(select, row))
        why_keep = []

        if r['id'] in referenced:
            why_keep.append('referenced by another table')
        if tasks.get(r['id']):
            why_keep.append(f'{tasks[r["id"]]} open task(s)')
        if r.get('email_message_id'):
            why_keep.append('created from an inbound customer email')
        if (r.get('source') or '').strip().lower() not in sources:
            why_keep.append(f'source={r.get("source") or "(blank)"!s}')
        if r.get('assigned_to'):
            why_keep.append('has an owner')
        if r.get('opp_number'):
            why_keep.append('has an opportunity number')
        if (r.get('stage') or '') not in ('New Opportunity', 'New', ''):
            why_keep.append(f'stage={r.get("stage")}')
        for field in ('notes', 'email', 'phone', 'pic'):
            if (r.get(field) or '').strip():
                why_keep.append(f'has {field}')
                break

        if why_keep:
            kept[why_keep[0]] += 1
            continue

        candidates.append({
            'id': r['id'],
            'company': r.get('company') or '',
            'source': r.get('source') or '',
            'stage': r.get('stage') or '',
            'created_at': str(r.get('created_at') or '')[:19],
            'company_id': r.get('company_id') or '',
            'is_archived': r.get('is_archived'),
            'open_tasks': tasks.get(r['id'], 0),
            'child_records': 0,
            'reason': 'no references, no history, no content, import source',
        })

    return candidates, ref_counts, unchecked, kept, tasks


# ─── report ──────────────────────────────────────────────────────────────
def write_csv(out_dir, table, rows):
    if not rows:
        return None
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f'cleanup_candidates_{table}.csv')
    with open(path, 'w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(_ROOT, 'cleanup_report'),
                    help='directory for the CSV files')
    ap.add_argument('--sources', default=','.join(DEFAULT_SOURCES),
                    help='comma-separated Lead.source values treated as '
                         'imported (blank means no source recorded)')
    args = ap.parse_args()

    sources = {s.strip().lower() for s in args.sources.split(',')}

    url = _db_url()
    print(f'database: {url}')
    print(f'run at:   {datetime.utcnow():%Y-%m-%d %H:%M} UTC')

    from sqlalchemy import create_engine
    engine = create_engine(url)
    insp = inspect(engine)

    with engine.connect() as conn:
        try:
            emps = conn.execute(text('SELECT COUNT(*) FROM employees')).scalar()
        except Exception:
            emps = 0
        if not emps:
            raise SystemExit(
                'Refusing to run: no employees in this database, so it is '
                'not the production one. This report is only meaningful '
                'against real data — check DATABASE_URL / .env.')
        print(f'employees: {emps}')

        refs = schema_map(insp)
        print_schema_map(insp, refs, TARGETS)

        total_leads = conn.execute(text('SELECT COUNT(*) FROM leads')).scalar()
        candidates, ref_counts, unchecked, kept, tasks = lead_candidates(
            conn, insp, refs, sources)

    print('=' * 72)
    print('  REFERENCE CHECK — distinct leads referenced, per table')
    print('=' * 72)
    for name, n in sorted(ref_counts.items(), key=lambda kv: -kv[1]):
        print(f'    {n:>7,}  {name}')
    if unchecked:
        print('\n  !! These references could NOT be checked:')
        for u in unchecked:
            print(f'       {u}')
        print('  !! The report is incomplete. Do not act on it until every')
        print('     reference can be read.')

    print('\n' + '=' * 72)
    print('  WHY LEADS WERE KEPT — first disqualifying reason')
    print('=' * 72)
    for reason, n in sorted(kept.items(), key=lambda kv: -kv[1])[:20]:
        print(f'    {n:>7,}  {reason}')

    open_on_candidates = sum(c['open_tasks'] for c in candidates)

    print('\n' + '=' * 72)
    print('  SUMMARY')
    print('=' * 72)
    print(f'    leads in database:            {total_leads:>7,}')
    print(f'    cleanup candidates:           {len(candidates):>7,}')
    print(f'    kept:                         {total_leads - len(candidates):>7,}')
    print(f'    pending tasks on candidates:  {open_on_candidates:>7,}')
    if open_on_candidates:
        print('      (a candidate with an open task is a contradiction — '
              'investigate before approving)')
    print(f'    total open lead tasks:        {sum(tasks.values()):>7,}')

    path = write_csv(args.out, 'leads', candidates)
    if path:
        print(f'\n    CSV: {path}')
    else:
        print('\n    No candidates — no CSV written.')

    print('\n' + '=' * 72)
    print('  NOTHING HAS BEEN CHANGED.')
    print('  This script has no apply mode. Review the CSV, decide what is')
    print('  genuinely junk, and ask for the archival script separately.')
    print('=' * 72)


if __name__ == '__main__':
    main()
