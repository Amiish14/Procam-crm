#!/usr/bin/env python3
"""
Who sees what, with the evidence needed to decide whether they should.
Read-only: this script reads the database and writes nothing to it.

    .venv/bin/python scripts/access_review.py
    .venv/bin/python scripts/access_review.py --scope all
    .venv/bin/python scripts/access_review.py --csv /tmp/access.csv
    .venv/bin/python scripts/access_review.py --include-inactive

Why it exists
    "Should this person see the whole company?" is a question an
    administrator can only answer with two further facts: what the person
    actually owns, and whether they are still using the CRM at all. The
    Access Control screen shows the setting; it does not show the
    evidence, and it does not show the people who were never configured
    and are running on their role's default.

    `--scope all` is the review that matters: the short list of people
    who can see everything, each with their footprint beside them, so a
    decision takes a minute rather than an afternoon.

What each column means
    scope       all / vertical / own, exactly as app/access/service.py
                resolves it today — not the stored value, the effective
                one
    from        profile    there is an Access Matrix row for them
                default    nobody has configured them; this comes from
                           their role
                super      Employee.is_super_admin, which overrides both
                           and cannot be granted through the matrix
    head        they are marked a vertical head, which widens the role
                default to their whole vertical
    leads /     rows they personally own: Lead.assigned_to and
    accounts    Company.pic_emp_code. Archived leads are counted too —
                this is a footprint, not a pipeline figure
    last        the most recent thing recorded against them anywhere:
                a logged sales activity or an audited action
    grants      the permissions they hold beyond the ordinary sales
                baseline, listed under the row when there are any

Exit status is 0; this is a report, not a check. Argument errors exit 2.

One honest caveat: the report imports the application, which runs the
ordinary boot — the same boot gunicorn runs. That is the application
starting, not this script writing; it adds, changes and deletes nothing.
"""
import argparse
import csv
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app as flask_app, db                       # noqa: E402
from app import Company, Employee, Lead, LeadActivity      # noqa: E402
from app.access.service import effective                   # noqa: E402
# The ordinary sales baseline is defined once, in the access service.
# Reading its private name is deliberate: a copy of the list here would
# drift the day somebody adds a module to the baseline, and this report
# would start calling an ordinary permission a special grant.
from app.access.service import _TEAM_BASELINE as TEAM_BASELINE  # noqa: E402
from app.models.access import AccessProfile, DataScope      # noqa: E402
from app.models.audit import AuditEvent                     # noqa: E402

try:
    # Cells that begin with = + - @ are run as formulas by Excel, and a
    # company name reaches this export. The shared helper is preferred so
    # every export in the portal is escaped the same way.
    from app.utils.spreadsheet_safe import safe_row
except ImportError:                                         # pragma: no cover
    def safe_row(values):
        return list(values)

FROM_PROFILE, FROM_DEFAULT, FROM_SUPER = 'profile', 'default', 'super'

CSV_COLUMNS = ('emp_code', 'name', 'role', 'vertical', 'data_scope',
               'scope_from', 'is_vertical_head', 'is_active', 'leads_owned',
               'accounts_owned', 'last_activity', 'last_activity_days',
               'extra_perms', 'perm_count')


# ── the facts, one grouped query each ────────────────────────────────
# One query per fact rather than four per employee: the per-employee
# version is fine for forty people and unusable for four hundred.

def _owned_leads():
    return dict(db.session.query(Lead.assigned_to, db.func.count(Lead.id))
                .group_by(Lead.assigned_to).all())


def _owned_accounts():
    return dict(db.session.query(Company.pic_emp_code,
                                 db.func.count(Company.id))
                .group_by(Company.pic_emp_code).all())


def _last_activity():
    """{emp_code: datetime} — the most recent thing each person did.

    Two sources, because either on its own understates it. LeadActivity
    is the sales record (calls, meetings, visits); AuditEvent is
    everything else the portal records against a person. Somebody who
    only ever reassigns accounts appears in the second and not the first.
    """
    seen = {}
    rows = (db.session.query(LeadActivity.performed_by,
                             db.func.max(LeadActivity.occurred_at))
            .group_by(LeadActivity.performed_by).all())
    rows += (db.session.query(AuditEvent.actor,
                              db.func.max(AuditEvent.occurred_at))
             .group_by(AuditEvent.actor).all())
    for code, when in rows:
        if not code or when is None:
            continue
        if seen.get(code) is None or when > seen[code]:
            seen[code] = when
    return seen


def _scope_source(emp, has_profile):
    """Where this person's data scope actually comes from.

    The order matters and mirrors `effective()`: the super admin flag
    overrides a stored profile, so a profile row on that account is not
    what is in force.
    """
    if getattr(emp, 'is_super_admin', False):
        return FROM_SUPER
    return FROM_PROFILE if has_profile else FROM_DEFAULT


def _days_since(when, now=None):
    if when is None:
        return None
    return max(0, ((now or datetime.utcnow()) - when).days)


def review(include_inactive=False, now=None):
    """One row per employee. Needs an application context."""
    q = Employee.query
    if not include_inactive:
        q = q.filter(Employee.is_active.is_(True))
    people = q.order_by(Employee.emp_code).all()

    profiles = {p.emp_code for p in AccessProfile.query.all()}
    leads, accounts = _owned_leads(), _owned_accounts()
    activity = _last_activity()
    baseline = set(TEAM_BASELINE)

    rows = []
    for emp in people:
        data_scope, perms = effective(emp.emp_code)
        when = activity.get(emp.emp_code)
        rows.append({
            'emp_code': emp.emp_code,
            'name': emp.name or '',
            'role': emp.role or '',
            'vertical': emp.vertical or '',
            'data_scope': data_scope or DataScope.OWN,
            'scope_from': _scope_source(emp, emp.emp_code in profiles),
            'is_vertical_head': bool(emp.is_vertical_head),
            'is_super_admin': bool(emp.is_super_admin),
            'is_active': bool(emp.is_active),
            'leads_owned': int(leads.get(emp.emp_code) or 0),
            'accounts_owned': int(accounts.get(emp.emp_code) or 0),
            'last_activity': str(when)[:16] if when else '',
            'last_activity_days': _days_since(when, now),
            'extra_perms': sorted(set(perms) - baseline),
            'perm_count': len(perms),
        })
    return rows


def filtered(rows, scope=None):
    return [r for r in rows if not scope or r['data_scope'] == scope]


# ── reporting ────────────────────────────────────────────────────────
def _last_cell(row):
    if not row['last_activity']:
        return 'never'
    days = row['last_activity_days']
    return f'{row["last_activity"][:10]} ({days}d)'


def report(rows, *, scope=None, population=None):
    """`population` is everybody the run looked at; `rows` is what passed
    the filter. The tallies at the foot are of the population, because a
    filtered list tallying itself tells the reader nothing."""
    everyone = rows if population is None else population
    out = [f'\n  Procam CRM access review — {datetime.now():%Y-%m-%d %H:%M}']
    if scope == DataScope.ALL:
        out.append(f'  Whole-company access: {len(rows)} of '
                   f'{len(everyone)} people.')
        out.append('  What each owns and when they last did anything is the '
                   'evidence for\n  whether they still need it.')
    else:
        out.append(f'  {len(rows)} '
                   + ('people' if scope is None else
                      f'people scoped to {DataScope.LABELS.get(scope, scope)}')
                   + '.')
    out.append('')
    out.append(f'    {"CODE":<11}{"NAME":<24}{"ROLE":<11}{"SCOPE":<10}'
               f'{"FROM":<9}{"HEAD":<6}{"LEADS":>7}{"ACCTS":>7}  '
               f'{"LAST ACTIVITY"}')
    for r in rows:
        out.append(f'    {r["emp_code"]:<11}{r["name"][:23]:<24}'
                   f'{r["role"][:10]:<11}{r["data_scope"]:<10}'
                   f'{r["scope_from"]:<9}'
                   f'{("yes" if r["is_vertical_head"] else "-"):<6}'
                   f'{r["leads_owned"]:>7}{r["accounts_owned"]:>7}  '
                   f'{_last_cell(r)}')
        if r['extra_perms']:
            out.append(f'    {"":<11}grants: ' + ', '.join(r['extra_perms']))

    by_scope = {s: sum(1 for r in everyone if r['data_scope'] == s)
                for s in DataScope.CHOICES}
    by_source = {s: sum(1 for r in everyone if r['scope_from'] == s)
                 for s in (FROM_PROFILE, FROM_DEFAULT, FROM_SUPER)}
    out.append(f'\n  of {len(everyone)} people: '
               f'{by_scope[DataScope.ALL]} whole company · '
               f'{by_scope[DataScope.VERTICAL]} own vertical · '
               f'{by_scope[DataScope.OWN]} own records')
    out.append(f'  {by_source[FROM_PROFILE]} from a stored profile · '
               f'{by_source[FROM_DEFAULT]} from the role default · '
               f'{by_source[FROM_SUPER]} super admin')
    return '\n'.join(out)


def write_csv(rows, path):
    with open(path, 'w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for r in rows:
            w.writerow(safe_row([
                r['emp_code'], r['name'], r['role'], r['vertical'],
                r['data_scope'], r['scope_from'],
                'yes' if r['is_vertical_head'] else 'no',
                'yes' if r['is_active'] else 'no',
                r['leads_owned'], r['accounts_owned'], r['last_activity'],
                '' if r['last_activity_days'] is None
                else r['last_activity_days'],
                '; '.join(r['extra_perms']), r['perm_count'],
            ]))
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(
        description='Who sees what, with the evidence to review it '
                    '(read-only).')
    ap.add_argument('--scope', choices=list(DataScope.CHOICES),
                    help="show only people on this data scope; 'all' is "
                         'the whole-company review')
    ap.add_argument('--csv', dest='csv_path', metavar='PATH',
                    help='also write the rows here as CSV')
    ap.add_argument('--include-inactive', action='store_true',
                    help='include deactivated accounts — a leaver who '
                         'still holds a profile is worth seeing')
    args = ap.parse_args(argv)

    with flask_app.app_context():
        rows = review(include_inactive=args.include_inactive)
    shown = filtered(rows, args.scope)
    print(report(shown, scope=args.scope, population=rows))
    if args.csv_path:
        write_csv(shown, args.csv_path)
        print(f'\n  written to {args.csv_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
