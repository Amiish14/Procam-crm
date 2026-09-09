"""
PO-before-Project migration — "One PO = One Project and Job".

The process used to be:  Won → create Project + Job → raise the PO.
It is now:               Won → capture Customer PO / Sales Order /
                               Contract → create the Project and Job.

What this script does
    1. Adds the PO capture columns to won_handovers (raw ALTER, so it
       runs before app.py's init_db() gets a chance to fire).
    2. Indexes po_ref.
    3. Moves every handover that is still waiting for TMS but has no PO
       recorded into the new `Awaiting PO` state — they are exactly the
       rows the new rule exists to catch.
    4. Seeds the `deal.won.po` task definition and re-points
       `deal.won.handoff` at the PO state, so the task after a win is
       "capture the customer PO" and the TMS handoff follows it.
    5. Reports any customer that already has one PO reference on two
       handovers — those are the pre-existing violations of the rule and
       need a human decision, so the script never merges them itself.

Usage:
    python scripts/2026_09_17_po_before_project.py --check   # dry-run
    python scripts/2026_09_17_po_before_project.py           # apply

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

# .env before anything reads DATABASE_URL — a previous migration in this
# series silently wrote to an empty instance/ database by skipping this.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:
    pass

from sqlalchemy import create_engine, text     # noqa: E402


NEW_COLUMNS = [
    ('po_type',        'VARCHAR(30)'),
    ('po_date',        'DATE'),
    ('po_value',       'NUMERIC(15, 2)'),
    ('po_currency',    'VARCHAR(6)'),
    ('po_captured_by', 'VARCHAR(20)'),
    ('po_captured_at', 'DATETIME'),
]

AWAITING_PO = 'Awaiting PO'
PENDING = 'Handover Pending'


def _db_url():
    url = os.environ.get('DATABASE_URL')
    if not url:
        url = 'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db')
    return url


def _norm(ref):
    return re.sub(r'[^a-z0-9]', '', (ref or '').lower())


def _existing_columns(conn):
    rows = conn.execute(text('PRAGMA table_info(won_handovers)')).fetchall()
    if not rows:
        raise SystemExit(
            'Refusing to run: won_handovers does not exist. Run '
            'scripts/2026_09_05_crm_rfq_quote_tms.py first.')
    return {r[1] for r in rows}


def _guard(conn):
    """Refuse to touch a database that isn't the real one.

    Learned the hard way: an empty instance/procam_crm.db accepts every
    ALTER happily and leaves production untouched.
    """
    try:
        n = conn.execute(text('SELECT COUNT(*) FROM employees')).scalar()
    except Exception:
        n = 0
    if not n:
        raise SystemExit(
            'Refusing to run: no employees in this database, so it is not '
            'the production one. Check DATABASE_URL / .env — an earlier '
            'migration in this series silently wrote to an empty '
            'instance/procam_crm.db this way.')
    return n


def _duplicate_pos(conn):
    """Customers already holding one PO on two handovers."""
    rows = conn.execute(text(
        'SELECT id, account_id, account_name, po_ref, status '
        'FROM won_handovers WHERE po_ref IS NOT NULL '
        "AND po_ref != '' AND status != 'Cancelled'")).fetchall()
    groups = {}
    for hid, acct_id, acct_name, ref, status in rows:
        key = (acct_id or ('name:' + _norm(acct_name)), _norm(ref))
        groups.setdefault(key, []).append((hid, acct_name, ref, status))
    return {k: v for k, v in groups.items() if len(v) > 1}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true', help='dry-run')
    args = ap.parse_args()

    url = _db_url()
    print(f'database: {url}')
    engine = create_engine(url)

    with engine.begin() as conn:
        emp_n = _guard(conn)
        print(f'  employees: {emp_n}')

        have = _existing_columns(conn)
        to_add = [(c, t) for c, t in NEW_COLUMNS if c not in have]

        stranded = conn.execute(text(
            'SELECT COUNT(*) FROM won_handovers WHERE status = :p '
            "AND (po_ref IS NULL OR po_ref = '')"), {'p': PENDING}).scalar()

        dupes = _duplicate_pos(conn)

        if args.check:
            print('== DRY-RUN — nothing written ==')
            print(f'  WOULD add columns:            '
                  f'{", ".join(c for c, _ in to_add) or "(none — already there)"}')
            print(f'  WOULD move to "Awaiting PO":  {stranded} handover(s)')
            print(f'  WOULD seed/refresh task defs: deal.won.po, '
                  f'deal.won.handoff')
        else:
            for col, coltype in to_add:
                conn.execute(text(
                    f'ALTER TABLE won_handovers ADD COLUMN {col} {coltype}'))
                print(f'  + column {col}')
            conn.execute(text(
                'CREATE INDEX IF NOT EXISTS ix_won_handovers_po_ref '
                'ON won_handovers (po_ref)'))
            if 'po_currency' in {c for c, _ in NEW_COLUMNS}:
                conn.execute(text(
                    "UPDATE won_handovers SET po_currency = 'INR' "
                    'WHERE po_currency IS NULL'))
            moved = conn.execute(text(
                'UPDATE won_handovers SET status = :a WHERE status = :p '
                "AND (po_ref IS NULL OR po_ref = '')"),
                {'a': AWAITING_PO, 'p': PENDING}).rowcount
            print(f'  moved to "Awaiting PO": {moved}')
            # Rows that already carried a PO keep their capture trail
            # honest rather than pretending someone entered it today.
            conn.execute(text(
                "UPDATE won_handovers SET po_type = 'Customer PO' "
                "WHERE po_ref IS NOT NULL AND po_ref != '' "
                'AND po_type IS NULL'))

        if dupes:
            print(f'\n  !! {len(dupes)} PO reference(s) are on more than one '
                  f'handover for the same customer.')
            print('     These predate the rule, so they are left alone — '
                  'one of each pair needs its own PO,')
            print('     or the handovers need merging by hand:')
            for (_, ref), rows in list(dupes.items())[:20]:
                print(f'       {ref}:')
                for hid, name, raw, status in rows:
                    print(f'         handover #{hid}  {raw!r}  '
                          f'{name or "(no account)"}  [{status}]')
            if len(dupes) > 20:
                print(f'       … and {len(dupes) - 20} more')
        else:
            print('\n  no duplicate PO references — the rule already holds.')

    if args.check:
        print('== end dry-run ==')
        return

    # Task definitions need the app, and the schema must be in place first.
    from app import app as flask_app, db                    # noqa: E402
    from app.models.task_engine import TaskDefinition       # noqa: E402
    import app.models.tms_handover                          # noqa: F401,E402

    defs = [
        ('deal.won.po',
         'Capture the Customer PO / Sales Order / Contract',
         'handover',
         ['Lead_Driver', 'Vertical_Head'],
         {'entity': 'WonHandover', 'state': AWAITING_PO},
         {'entity': 'WonHandover', 'state': PENDING},
         'deal.won.handoff',
         {'kind': 'field', 'path': 'pic_emp_code'},
         '/handovers?status=Awaiting%20PO', 48, 'Vertical_Head', 1),

        ('deal.won.handoff',
         'Create the TMS Project and Job against the customer PO',
         'handover',
         ['Lead_Driver', 'Vertical_Head'],
         {'entity': 'WonHandover', 'state': PENDING},
         {'entity': 'WonHandover', 'state': 'TMS Project Created'},
         None,
         {'kind': 'field', 'path': 'pic_emp_code'},
         '/handovers', 24, 'Vertical_Head', 2),
    ]

    with flask_app.app_context():
        for (key, title, module, roles, start, done, nxt, owner, route,
             sla, esc, prio) in defs:
            row = TaskDefinition.query.filter_by(task_key=key).first()
            if row is None:
                row = TaskDefinition(task_key=key)
                db.session.add(row)
                verb = 'seeded'
            else:
                verb = 'refreshed'
            row.title, row.module = title, module
            row.allowed_roles = list(roles)
            row.start_state, row.completion_state = start, done
            row.next_task_key, row.next_owner_rule = nxt, owner
            row.action_route, row.sla_hours = route, sla
            row.escalation_role, row.priority = esc, prio
            row.is_active = True
            print(f'  {verb} task definition {key}')
        db.session.commit()

    print('\n  done.')


if __name__ == '__main__':
    main()
