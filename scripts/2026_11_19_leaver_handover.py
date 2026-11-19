#!/usr/bin/env python
"""Everything one departing person still holds, and nothing reassigned.

    .venv/bin/python scripts/2026_11_19_leaver_handover.py --emp-code <CODE>
    .venv/bin/python scripts/2026_11_19_leaver_handover.py --emp-code <CODE> \
        --mark-left --apply --actor DIR12010 --note 'left Procam, confirmed'
    .venv/bin/python scripts/2026_11_19_leaver_handover.py --emp-code <CODE> \
        --undo --batch LEFT-20261119-1 --actor DIR12010

WHAT IT DOES AND WHAT IT REFUSES TO DO

It reads. The report and the two CSVs are the whole of the default run:
nothing is reassigned, ever, by this script -- not with --apply, not
with any flag. Reassignment needs a person to say who each account goes
to, and a script that guessed would be worse than the gap it filled.
`--mark-left` writes exactly three columns on ONE row, the employee's
own: `left_on`, `left_note`, `is_active`, plus the session bump that
ends their open sessions. Everything else is left exactly where it is,
so the names in the CSV still point at the work when the new owners are
known.

WHY `left_on` AND NOT JUST `is_active`

`is_active` false means "cannot sign in", and it reads the same for a
suspension, a long leave, an account made by mistake and somebody who
has left. So nothing could tell a leaver from the rest, and Employee
Master's Activate button would put a departed person back into every
assignment list with one click. `left_on` is the durable fact:
reactivation refuses while it is set (app/services/leavers.py), the
assignment refusals say "has left Procam" rather than "is not active",
and the pickers drop them while the "All Team" FILTERS keep them --
because filtering by a departed person is how their work gets found.

WHAT STOPS THEM BEING PICKED, AND WHY IT IS NOT NEW CODE

Every candidate query in this CRM already filters on `is_active`: mail
ingest (`mail_ingest.assignee_from_forward`, `lead_kinds.owner`), the
auto-assign rules (`auto_assign.usable`, which also follows the
`owner_replacement` table), the triage and PIC pickers, the escalation
and notification recipients. The two write paths refuse an inactive
person outright -- `lead_assignment.validate` and
`directory._active_employee`. So marking somebody inactive is the
enforcement; this adds the fact that says WHY, and the guard that stops
it being undone by accident.

ON A DEVELOPMENT MACHINE THIS REPORTS NOTHING. The leads, accounts,
quotes and RFQ tables are empty here. Run it on the server.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import OrderedDict
from datetime import date, datetime

# The repo root, so `import app` works however this is invoked. NOT
# scripts/ -- `scripts/email_ingest.py` shadows the `email_ingest`
# package on the path and the whole app then fails to import.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ACTION = 'employee.left'
BATCH_PREFIX = 'LEFT'

OPEN_CSV = 'docs/leaver_{code}_open_work.csv'
TRACE_CSV = 'docs/leaver_{code}_everything_else.csv'

FIELDS = ('record_type', 'their_role', 'record', 'customer', 'stage',
          'value_inr', 'last_activity', 'open', 'id')
TRACE_FIELDS = ('what', 'table', 'column', 'rows', 'detail')


# ── schema-safe reads ────────────────────────────────────────────────
def _inspector():
    from app import db
    from sqlalchemy import inspect
    return inspect(db.engine)


def columns_of(table):
    try:
        return {c['name'] for c in _inspector().get_columns(table)}
    except Exception:
        return set()


def tables():
    try:
        return set(_inspector().get_table_names())
    except Exception:
        return set()


def have(table, *cols):
    """True when the table exists and carries every column named.

    A report that crashes on a database one column older than this
    script is a report nobody runs.
    """
    if table not in tables():
        return False
    present = columns_of(table)
    return all(c in present for c in cols)


def count_where(table, column, value):
    from app import db
    from sqlalchemy import text
    if not have(table, column):
        return None
    try:
        return db.session.execute(
            text(f'SELECT COUNT(*) FROM "{table}" '
                 f'WHERE UPPER(TRIM(CAST("{column}" AS TEXT))) = :v'),
            {'v': value}).scalar() or 0
    except Exception:
        db.session.rollback()
        return None


def pick(model, table, wanted, *where):
    """Rows carrying only the columns named, so a column this database
    has not got cannot break the query.

    The models here are ahead of any database that has not booted the
    app since the last deploy, and the ORM selects every mapped column:
    one unrelated missing column turned a whole group of this report
    into a stack trace. Asking for the columns we actually print is
    both cheaper and the only version that cannot do that.
    """
    from app import db

    if table not in tables():
        return []
    present = columns_of(table)
    cols = [c for c in wanted if c in present]
    if 'id' not in cols and 'id' in present:
        cols.insert(0, 'id')
    missing = [c for c in wanted if c not in present]
    if missing:
        print(f'  [NOTE] {table}: no column {", ".join(missing)} in this '
              f'database — left out of the report')
    try:
        return (model.query
                .with_entities(*[getattr(model, c) for c in cols])
                .filter(*where).all())
    except Exception as exc:                                # noqa: BLE001
        db.session.rollback()
        print(f'  [WARN] {table}: {exc}')
        return []


def got(row, name, default=None):
    return getattr(row, name, default)


# ── the person ───────────────────────────────────────────────────────
def employee(code):
    from app import Employee
    return Employee.query.filter_by(emp_code=code).first()


def money(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def when(value):
    if value is None:
        return ''
    if isinstance(value, (datetime, date)):
        return str(value)[:19]
    return str(value)[:19]


def _open_lead(stage):
    from app.services import stage_map
    return stage_map.canonical(stage or '') not in stage_map.TERMINAL


# ── the work ─────────────────────────────────────────────────────────
def leads_of(code):
    """Leads they own or second, valued the way the CRM values them."""
    from app import Lead
    from app.services import lead_value
    from sqlalchemy import or_

    cols = columns_of('leads')
    second_col = 'secondary_owner' in cols
    where = (or_(Lead.assigned_to == code, Lead.secondary_owner == code)
             if second_col else Lead.assigned_to == code)
    rows = []
    for r in pick(Lead, 'leads',
                  ('id', 'crn', 'company', 'stage', 'assigned_to',
                   'secondary_owner', 'quoted_amount_inr',
                   'estimated_value_inr', 'last_activity_at', 'updated_at'),
                  where):
        second = (got(r, 'secondary_owner') or '') == code
        value = lead_value.row_value_inr(r)
        rows.append({
            'record_type': 'Lead',
            'their_role': 'secondary PIC' if second else 'PIC',
            'record': got(r, 'crn') or f'lead {r.id}',
            'customer': got(r, 'company') or '',
            'stage': got(r, 'stage') or '',
            'value_inr': value or None,
            'last_activity': when(got(r, 'last_activity_at')
                                  or got(r, 'updated_at')),
            'open': _open_lead(got(r, 'stage')),
            'id': r.id,
        })
    return rows


def followups_of(code):
    """A follow-up in this CRM is a date on a lead, not its own record.

    Listed separately because it is the part with a clock on it: a date
    nobody owns any more passes silently.
    """
    from app import Lead

    if 'followup_date' not in columns_of('leads'):
        return []
    today = date.today()
    rows = []
    for r in pick(Lead, 'leads',
                  ('id', 'crn', 'company', 'stage', 'followup_date',
                   'last_activity_at'),
                  Lead.assigned_to == code, Lead.followup_date.isnot(None)):
        if not _open_lead(got(r, 'stage')):
            continue
        due = got(r, 'followup_date')
        rows.append({
            'record_type': 'Follow-up',
            'their_role': 'OVERDUE' if due and due < today else 'due',
            'record': f'{due} on {got(r, "crn") or "lead " + str(r.id)}',
            'customer': got(r, 'company') or '',
            'stage': got(r, 'stage') or '',
            'value_inr': None,
            'last_activity': when(got(r, 'last_activity_at')),
            'open': True,
            'id': r.id,
        })
    return rows


def accounts_of(code):
    from app import Company

    rows = []
    cols = columns_of('companies')
    for column, label in (('pic_emp_code', 'PIC'),
                          ('secondary_pic_emp_code', 'secondary PIC'),
                          ('backup_pic_emp_code', 'backup PIC')):
        if column not in cols:
            continue
        for r in pick(Company, 'companies',
                      ('id', 'name', 'dev_stage', 'last_activity_at',
                       'is_active'),
                      getattr(Company, column) == code):
            rows.append({
                'record_type': 'Account',
                'their_role': label,
                'record': got(r, 'name') or f'account {r.id}',
                'customer': got(r, 'name') or '',
                'stage': got(r, 'dev_stage') or '',
                'value_inr': None,
                'last_activity': when(got(r, 'last_activity_at')),
                'open': bool(got(r, 'is_active', True)),
                'id': r.id,
            })
    return rows


def opportunities_of(code):
    from app import Company, Opportunity

    if not have('opportunities', 'owner_emp_code'):
        return []
    rows = []
    for r in pick(Opportunity, 'opportunities',
                  ('id', 'opp_number', 'title', 'stage', 'value_inr',
                   'company_id', 'updated_at'),
                  Opportunity.owner_emp_code == code):
        name = ''
        if got(r, 'company_id'):
            account = Company.query.with_entities(Company.name).filter(
                Company.id == r.company_id).first()
            name = account.name if account is not None else ''
        rows.append({
            'record_type': 'Opportunity',
            'their_role': 'owner',
            'record': got(r, 'opp_number') or f'opportunity {r.id}',
            'customer': name or got(r, 'title') or '',
            'stage': got(r, 'stage') or '',
            'value_inr': money(got(r, 'value_inr')),
            'last_activity': when(got(r, 'updated_at')),
            'open': (got(r, 'stage') or '') not in ('Won', 'Lost', 'Closed'),
            'id': r.id,
        })
    return rows


def quotes_of(code):
    from app.models.quote import Quote

    rows, seen = [], set()
    for column, label in (('prepared_by_id', 'prepared'),
                          ('submitted_by_id', 'submitted'),
                          ('approved_by_id', 'approved'),
                          ('created_by_id', 'created')):
        if not have('quotes', column):
            continue
        for r in pick(Quote, 'quotes',
                      ('id', 'quote_number', 'subject', 'status',
                       'total_amount', 'updated_at'),
                      getattr(Quote, column) == code):
            if r.id in seen:
                continue
            seen.add(r.id)
            rows.append({
                'record_type': 'Quote',
                'their_role': label,
                'record': got(r, 'quote_number') or f'quote {r.id}',
                'customer': got(r, 'subject') or '',
                'stage': got(r, 'status') or '',
                'value_inr': money(got(r, 'total_amount')),
                'last_activity': when(got(r, 'updated_at')),
                'open': (got(r, 'status') or '') not in ('Won', 'Lost',
                                                         'Cancelled'),
                'id': r.id,
            })
    return rows


def rfqs_of(code):
    from app.models.rfq import RFQ

    rows, seen = [], set()
    for column, label in (('lead_driver', 'driving'),
                          ('created_by_id', 'raised')):
        if not have('rfqs', column):
            continue
        for r in pick(RFQ, 'rfqs',
                      ('id', 'rfq_number', 'subject', 'status',
                       'shipper_name', 'quote_by_date', 'updated_at'),
                      getattr(RFQ, column) == code):
            if r.id in seen:
                continue
            seen.add(r.id)
            rows.append({
                'record_type': 'RFQ',
                'their_role': label,
                'record': got(r, 'rfq_number') or f'rfq {r.id}',
                'customer': got(r, 'subject') or got(r, 'shipper_name') or '',
                'stage': got(r, 'status') or '',
                'value_inr': None,
                'last_activity': when(got(r, 'quote_by_date')
                                      or got(r, 'updated_at')),
                'open': (got(r, 'status') or '') not in ('Closed',
                                                         'Cancelled'),
                'id': r.id,
            })
    return rows


def tasks_of(code):
    rows = []
    if have('task_instances', 'owner_user_id'):
        from app.models.task_engine import TaskInstance
        for r in pick(TaskInstance, 'task_instances',
                      ('id', 'task_key', 'entity_type', 'entity_id',
                       'entity_display', 'status', 'due_at', 'created_at'),
                      TaskInstance.owner_user_id == code):
            rows.append({
                'record_type': 'Task',
                'their_role': 'owner',
                'record': got(r, 'task_key') or f'task {r.id}',
                'customer': (got(r, 'entity_display')
                             or f'{got(r, "entity_type")} '
                                f'{got(r, "entity_id")}'),
                'stage': got(r, 'status') or '',
                'value_inr': None,
                'last_activity': when(got(r, 'due_at') or got(r,
                                                              'created_at')),
                'open': (got(r, 'status') or '') not in ('done', 'cancelled',
                                                         'complete'),
                'id': r.id,
            })
    if have('review_actions', 'owner_emp_code'):
        from app.models.review import ReviewAction
        from sqlalchemy import or_
        for r in pick(ReviewAction, 'review_actions',
                      ('id', 'description', 'status', 'due_date',
                       'owner_emp_code', 'subject_emp_code', 'created_at'),
                      or_(ReviewAction.owner_emp_code == code,
                          ReviewAction.subject_emp_code == code)):
            rows.append({
                'record_type': 'Review action',
                'their_role': ('owner' if got(r, 'owner_emp_code') == code
                               else 'subject'),
                'record': f'action {r.id}',
                'customer': (got(r, 'description') or '')[:80],
                'stage': got(r, 'status') or '',
                'value_inr': None,
                'last_activity': when(got(r, 'due_date')
                                      or got(r, 'created_at')),
                'open': (got(r, 'status') or '') == 'open',
                'id': r.id,
            })
    return rows


def handovers_of(code):
    """A won deal handed to operations still has their name on it."""
    if not have('won_handovers', 'pic_emp_code'):
        return []
    from app.models.tms_handover import WonHandover

    rows = []
    for r in pick(WonHandover, 'won_handovers',
                  ('id', 'crn', 'account_name', 'status', 'won_value',
                   'updated_at'),
                  WonHandover.pic_emp_code == code):
        rows.append({
            'record_type': 'Won handover',
            'their_role': 'PIC',
            'record': got(r, 'crn') or f'handover {r.id}',
            'customer': got(r, 'account_name') or '',
            'stage': got(r, 'status') or '',
            'value_inr': money(got(r, 'won_value')),
            'last_activity': when(got(r, 'updated_at')),
            'open': (got(r, 'status') or '') not in ('Closed',
                                                     'Acknowledged'),
            'id': r.id,
        })
    return rows


GROUPS = OrderedDict((
    ('Leads', leads_of),
    ('Follow-ups', followups_of),
    ('Accounts', accounts_of),
    ('Opportunities', opportunities_of),
    ('Quotes', quotes_of),
    ('RFQs', rfqs_of),
    ('Tasks', tasks_of),
    ('Won handovers', handovers_of),
))


# ── everything else that still names them ────────────────────────────
#: The config and the history. Not work to reassign -- settings and
#: rows that keep pointing at somebody who is gone, which is how a
#: departed person's name comes back after the handover is finished.
CONFIG = [
    ('Vertical head master', 'vertical_head', 'head_emp_code'),
    ('Vertical head master (deputy)', 'vertical_head', 'deputy_emp_code'),
    ('Account owner per vertical', 'account_vertical_owner', 'emp_code'),
    ('Owner replacement (from)', 'owner_replacement', 'from_emp_code'),
    ('Owner replacement (to)', 'owner_replacement', 'to_emp_code'),
    ('Access Matrix profile', 'access_profiles', 'emp_code'),
    ('Owner confirmation waiting', 'owner_confirmation', 'emp_code'),
    ('Account owner confirmation', 'account_owner_confirmation', 'emp_code'),
    ('Contacts assigned', 'contacts', 'assigned_to'),
    ('Contact secondary PIC', 'contact_relationships', 'secondary_pic'),
    ('Projects (PIC)', 'projects', 'pic_emp_code'),
    ('Intelligence project owner', 'intel_project_facts', 'bd_owner_emp_code'),
    ('Project follows', 'intel_project_follows', 'emp_code'),
    ('News items assigned', 'news_items', 'assigned_to'),
    ('Rate request owner', 'rate_sourcing_lines', 'sourcing_owner_id'),
    ('Unquoted mail waiting', 'unmatched_quote_mail', 'resolved_by'),
    ('Training progress', 'training_progress', 'emp_code'),
    ('Copilot chunks (owner)', 'copilot_chunk', 'owner_emp_code'),
]


def config_rows(code, emp):
    rows = []
    for label, table, column in CONFIG:
        n = count_where(table, column, code)
        if n:
            rows.append({'what': label, 'table': table, 'column': column,
                         'rows': n, 'detail': ''})
    # Settings that name a person as a destination for new work.
    try:
        from app.services import admin_settings
        for key in ('auto_assign_backstop', 'lead_kind_owner',
                    'lead_kinds_owner'):
            said = (admin_settings.get(key) or '').strip().upper()
            if said == code:
                rows.append({'what': 'Admin setting names them',
                             'table': 'app_settings', 'column': key,
                             'rows': 1,
                             'detail': 'new work would still be sent here'})
    except Exception:
        pass
    # Other people reporting to them.
    try:
        from app import Employee
        reports = Employee.query.filter(
            Employee.vertical_head_id == emp.id,
            Employee.is_active.is_(True)).all()
        if reports:
            rows.append({'what': 'Employees reporting to them',
                         'table': 'employees', 'column': 'vertical_head_id',
                         'rows': len(reports),
                         'detail': ', '.join(r.emp_code for r in reports)})
    except Exception:
        pass
    if getattr(emp, 'is_vertical_head', False):
        rows.append({'what': 'Marked as a vertical head', 'table': 'employees',
                     'column': 'is_vertical_head', 'rows': 1,
                     'detail': emp.vertical or ''})
    return rows


# ── the report ───────────────────────────────────────────────────────
def write_csv(path, rows, fields):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    return path


def report(emp, work, config, *, show=12):
    print('\n' + '=' * 70)
    print('  LEAVER HANDOVER — what is still in this person\'s name')
    print('=' * 70)
    print(f'  employee        {emp.emp_code}  {emp.name}')
    print(f'  vertical        {emp.vertical or "—"} · role {emp.role or "—"}'
          f'{" · VERTICAL HEAD" if emp.is_vertical_head else ""}')
    print(f'  can sign in     {"YES" if emp.is_active else "no"}')
    print(f'  recorded left   {emp.left_on or "NOT RECORDED"}')

    total_open = sum(1 for r in work if r['open'])
    print(f'\n  records in their name    {len(work)}'
          f'   (open: {total_open})')
    for group, rows in group_counts(work).items():
        opens = sum(1 for r in rows if r['open'])
        value = sum(r['value_inr'] or 0 for r in rows if r['open'])
        money_said = f'   Rs {value:,.0f} open' if value else ''
        print(f'    {group:<16} {len(rows):>4}  open {opens:>4}{money_said}')

    openers = [r for r in work if r['open']]
    if openers:
        print('\n  THE OPEN ONES')
        for r in openers[:show]:
            value = r['value_inr']
            said = '' if value is None else f'Rs {value:,.0f}'
            print(f'    {r["record_type"]:<14} {str(r["record"])[:18]:<18} '
                  f'{str(r["customer"])[:26]:<26} {str(r["stage"])[:16]:<16} '
                  f'{said}')
        if len(openers) > show:
            print(f'    … and {len(openers) - show} more, all in the CSV')

    if config:
        print('\n  AND THESE STILL NAME THEM — config, not work')
        for row in config:
            print(f'    {row["what"]:<34} {row["rows"]:>4}  '
                  f'{row["detail"][:28]}')
    print('\n  Nothing above has been reassigned. That is deliberate: who '
          'each\n  one goes to is a decision, and this script does not make '
          'it.')


def group_counts(work):
    out = OrderedDict()
    for row in work:
        out.setdefault(row['record_type'], []).append(row)
    return out


# ── marking them as left ─────────────────────────────────────────────
def next_batch_id(now=None):
    from app.models.audit import AuditEvent

    now = now or datetime.utcnow()
    prefix = f'{BATCH_PREFIX}-{now.strftime("%Y%m%d")}-'
    used = set()
    for event in (AuditEvent.query.filter(AuditEvent.action == ACTION)
                  .order_by(AuditEvent.id.desc()).limit(500).all()):
        said = str((event.new_value or {}).get('batch') or '') \
            if isinstance(event.new_value, dict) else ''
        if said.startswith(prefix):
            used.add(said)
    return f'{prefix}{len(used) + 1}'


def mark(emp, *, actor, note, batch):
    from app import db
    from app.services import leavers

    ok, error = leavers.mark_left(emp, actor=actor, note=note, batch=batch)
    if not ok:
        sys.exit(f'  [STOP] {error}')
    db.session.commit()
    print(f'\n  {emp.emp_code} {emp.name} is recorded as having left on '
          f'{emp.left_on}.')
    print('  Their sessions are ended, the account cannot sign in, and '
          'reactivating\n  it now refuses until the leaver mark is cleared.')
    print(f'\n  Undo: --undo --batch {batch} --actor {actor}')


def undo(emp, *, actor, batch):
    from app import db
    from app.models.audit import AuditEvent
    from app.services import leavers

    event = None
    for row in (AuditEvent.query.filter(AuditEvent.action == ACTION,
                                        AuditEvent.entity_id == emp.emp_code)
                .order_by(AuditEvent.id.desc()).all()):
        if isinstance(row.new_value, dict) and \
                str(row.new_value.get('batch') or '') == batch:
            event = row
            break
    if event is None:
        sys.exit(f'  [STOP] no {ACTION} row for {emp.emp_code} in batch '
                 f'{batch}.')
    ok, error = leavers.unmark(emp, actor=actor, batch=batch)
    if not ok:
        sys.exit(f'  [STOP] {error}')
    db.session.commit()
    print(f'\n  The leaver mark is removed. {emp.emp_code} can sign in: '
          f'{"yes" if emp.is_active else "no"} (as it was before).')
    print('  Sessions are NOT un-ended: a revocation is not undone by '
          'pretending\n  it did not happen.')


def main():
    parser = argparse.ArgumentParser(
        description='List everything one employee still holds, and '
                    'optionally record that they have left.')
    parser.add_argument('--emp-code', required=True,
                        help='the employee code; no name or code is '
                             'written into this script')
    parser.add_argument('--out')
    parser.add_argument('--trace-out', dest='trace_out')
    parser.add_argument('--mark-left', action='store_true',
                        help='record that they have left (needs --apply)')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--undo', action='store_true')
    parser.add_argument('--batch')
    parser.add_argument('--actor', help='employee id; required to write')
    parser.add_argument('--note', default='left the company')
    parser.add_argument('--show', type=int, default=12)
    args = parser.parse_args()

    code = (args.emp_code or '').strip().upper()
    import app as A

    with A.app.app_context():
        emp = employee(code)
        if emp is None:
            sys.exit(f'  [STOP] no employee has the code {code}.')

        if args.undo:
            if not (args.batch and args.actor):
                sys.exit('  [STOP] --undo needs --batch and --actor.')
            return undo(emp, actor=args.actor, batch=args.batch)

        work = []
        for _group, finder in GROUPS.items():
            try:
                work.extend(finder(code))
            except Exception as exc:                       # noqa: BLE001
                A.db.session.rollback()
                print(f'  [WARN] {_group}: {exc}')
        config = config_rows(code, emp)
        report(emp, work, config, show=args.show)

        said = write_csv(args.out or OPEN_CSV.format(code=code.lower()),
                         work, FIELDS)
        print(f'\n  CSV             {said}')
        if config:
            said2 = write_csv(
                args.trace_out or TRACE_CSV.format(code=code.lower()),
                config, TRACE_FIELDS)
            print(f'  Config          {said2}')

        if not args.mark_left:
            print('\n  DRY RUN — nothing written, nothing reassigned.')
            print('  Add --mark-left --apply --actor DIR12010 to record '
                  'that they have left.')
            return 0
        if not args.apply:
            print('\n  DRY RUN — --mark-left needs --apply as well.')
            print(f'  It would set left_on={date.today()}, is_active=false '
                  f'and end their sessions.')
            return 0
        if not args.actor:
            sys.exit('  [STOP] --apply needs --actor, an employee id.')
        return mark(emp, actor=args.actor, note=args.note,
                    batch=next_batch_id())


if __name__ == '__main__':
    main()
