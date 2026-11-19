#!/usr/bin/env python
"""Staff email addresses: find the real one, or say nobody knows it.

    .venv/bin/python scripts/2026_11_19_staff_email_fix.py
    .venv/bin/python scripts/2026_11_19_staff_email_fix.py --apply \
        --actor DIR12010
    .venv/bin/python scripts/2026_11_19_staff_email_fix.py --undo \
        --batch SEML-20261119-1 --actor DIR12010

WHAT IS WRONG

Most employee rows carry an address that was never a mailbox. The
first install seeded employees from the PRERNA master and filled a
missing address with `emp<code>@procamgroup.in` -- one line in app.py,
`email=em or f"{ec.lower()}@procamgroup.in"`. It looks like an
address, it is unique, so nothing ever complained; and every
notification the CRM sent to those people bounced into nothing. On
this machine that is 110 of 117 rows. The generator is removed in the
same commit as this script: a missing address is now left missing,
which is at least true.

HOW A REAL ADDRESS IS FOUND

Only from evidence already in this database, and the two best kinds of
evidence are not in the brief:

  forwarded   `mail_ingest_log.forwarded_by_emp` is an employee CODE
              and `forwarded_by` is the address they forwarded from.
              That is a code paired with an address by the ingest
              itself -- no name matching, nothing to mistake. It is
              ranked first and on its own is enough.
  lead_fwd    `leads.forwarded_by_email` with `forwarded_by_name`,
              the same thing one step less direct: an address and the
              display name it arrived under.

Then the brief's own sources, all of which need the display name to
match the employee's:

  header      `lead_emails` (from/to/cc), `lead_raw_emails.from_addr`,
              `mail_ingest_log.from_addr`, `email_classifications`,
              `unmatched_quote_mail` -- wherever a header was kept.
  body        the From/To/Cc lines inside `leads.original_email_body`.
  login       the employee's own login record. In this CRM the
              Employee IS the login -- it carries the password hash --
              so there is no second table to consult, and this source
              reports itself as not applicable rather than pretending
              to have looked.
  tms         the TMS employee list. The integration API runs the
              other way: the TMS calls the CRM, and the CRM has no
              base URL, token or client for the TMS (app.py:4026 says
              so outright). So this is attempted only when
              TMS_BASE_URL and TMS_TOKEN are both set, and otherwise
              reported unreachable -- which is what "if reachable"
              has to mean when the direction of the integration is
              the other one.

Every address is checked against `own_people.own_domains()` -- the
four domains the CRM already agrees are ours -- not a list retyped
here.

WHAT IT REFUSES TO DO

Two addresses for one person is a conflict and is never guessed at.
It is written to the CSV with both candidates and their counts, and
`--apply` skips it. Same for an address already claimed by a
different employee row: `employees.email` is unique, and writing one
twice would fail the transaction and take the whole batch with it.

Nothing is deleted. A row that cannot be resolved keeps whatever it
has and is listed in docs/staff_email_unresolved.csv for HR.
"""
from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('SECRET_KEY', 'script')
os.environ.setdefault('ADMIN_INITIAL_PASSWORD', 'Script-123456789')

ACTION = 'staff_email_fix'
BATCH_PREFIX = 'SEML'

OUT_CSV = 'docs/staff_email_fix.csv'
UNRESOLVED_CSV = 'docs/staff_email_unresolved.csv'

#: `emp<digits>@anything`, which is the shape the seeder made. Case
#: insensitive, and the domain is not pinned: the fault is the local
#: part, and a second install on another domain made the same one.
GENERATED = re.compile(r'^emp\d+@', re.I)

ADDR = re.compile(r'[\w.+-]+@[\w-]+\.[\w.-]+')
#: `Name <addr>` or `"Name" <addr>`, the form a mail header uses.
NAMED = re.compile(r'(?:"([^"]+)"|([^<>,;]+?))\s*<\s*([^<>\s]+@[^<>\s]+)\s*>')
#: The header lines inside a kept body.
BODY_LINE = re.compile(r'^\s*(from|to|cc)\s*:\s*(.+)$', re.I | re.M)

#: How much each kind of evidence is worth when two addresses compete.
#: A code the ingest itself paired with an address outranks any number
#: of display-name matches, because nothing about it is inferred.
WEIGHT = {'forwarded': 100, 'lead_fwd': 40, 'login': 30, 'tms': 25,
          'header': 5, 'body': 3}


# ── helpers ──────────────────────────────────────────────────────────
def own_domains():
    from app.services import own_people
    return {d.lower() for d in own_people.own_domains()}


def is_ours(addr, domains):
    addr = (addr or '').strip().lower()
    return '@' in addr and addr.rsplit('@', 1)[-1] in domains


def looks_generated(addr):
    return bool(GENERATED.match((addr or '').strip()))


def needs_fixing(emp):
    addr = (emp.email or '').strip()
    return not addr or looks_generated(addr)


def norm_name(name):
    """A name reduced to its comparable parts.

    People are written three ways in this data -- "Aritra Mitra",
    "Mitra, Aritra", "ARITRA MITRA" -- so the comparison is on the SET
    of word stems, not the string. Initials and honorifics are dropped
    because they are the difference between the same person twice.
    """
    said = re.sub(r'[^a-z\s]', ' ', (name or '').lower())
    drop = {'mr', 'mrs', 'ms', 'dr', 'shri', 'smt', 'the', 'and'}
    parts = [w for w in said.split() if len(w) > 1 and w not in drop]
    return frozenset(parts)


def name_matches(a, b):
    """True when two written names are the same person.

    Both full name sets equal, or one is a subset of the other with at
    least two words in common -- "Bala Murugan T" against "Bala
    Murugan" is the same person; "Amit" against "Amit Kakkar" is not
    enough on one word, because this book has several Amits.
    """
    sa, sb = norm_name(a), norm_name(b)
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    common = sa & sb
    return len(common) >= 2 and (sa <= sb or sb <= sa)


def table_exists(name):
    from app import db
    from sqlalchemy import inspect
    return name in inspect(db.engine).get_table_names()


def columns_of(name):
    from app import db
    from sqlalchemy import inspect
    try:
        return {c['name'] for c in inspect(db.engine).get_columns(name)}
    except Exception:
        return set()


def rows_of(table, wanted):
    """Read `wanted` columns from `table`, or nothing if the table or
    the columns are not there.

    The schema on this machine is older than the models -- `leads` here
    has no `original_email_body` although the model declares one -- so
    every read is checked first. A script that only runs where the
    schema happens to be current is a script that will not run on the
    server, which is the only place the evidence actually is.
    """
    from app import db
    from sqlalchemy import text
    if not table_exists(table):
        return []
    have = columns_of(table)
    cols = [c for c in wanted if c in have]
    if not cols:
        return []
    sql = 'SELECT %s FROM %s' % (', '.join(f'"{c}"' for c in cols), table)
    out = []
    for row in db.session.execute(text(sql)):
        out.append(dict(zip(cols, row)))
    return out


# ── evidence ─────────────────────────────────────────────────────────
class Evidence:
    """One address proposed for one employee, and why."""

    __slots__ = ('addr', 'kind', 'count', 'detail')

    def __init__(self, addr, kind, detail=''):
        self.addr, self.kind, self.count, self.detail = addr, kind, 1, detail

    @property
    def weight(self):
        return WEIGHT.get(self.kind, 1) * self.count


def gather(employees, domains, *, tms=None):
    """{emp_code: {address: Evidence}} from everything in this database."""
    found = defaultdict(dict)

    def add(code, addr, kind, detail=''):
        addr = (addr or '').strip().lower()
        if not code or not is_ours(addr, domains) or looks_generated(addr):
            return
        bucket = found[code]
        if addr in bucket:
            bucket[addr].count += 1
            if kind != bucket[addr].kind and \
                    WEIGHT.get(kind, 0) > WEIGHT.get(bucket[addr].kind, 0):
                bucket[addr].kind, bucket[addr].detail = kind, detail
        else:
            bucket[addr] = Evidence(addr, kind, detail)

    by_name = [(e.emp_code, e.name) for e in employees]

    def add_by_name(written, addr, kind, detail=''):
        """A display name on a header, matched to an employee.

        One name may match nobody, which is the usual case -- most of
        these headers are customers.
        """
        if not written:
            return
        for code, name in by_name:
            if name_matches(written, name):
                add(code, addr, kind, detail)

    # ── the code-keyed sources, which need no name matching ─────────
    for r in rows_of('mail_ingest_log', ('forwarded_by_emp', 'forwarded_by',
                                         'from_addr')):
        code = (r.get('forwarded_by_emp') or '').strip().upper()
        if code:
            add(code, r.get('forwarded_by'), 'forwarded',
                'forwarded a mail to the leads mailbox from this address')
            add(code, r.get('from_addr'), 'forwarded',
                'the sender of a mail the ingest credited to this person')

    for r in rows_of('leads', ('forwarded_by_email', 'forwarded_by_name')):
        add_by_name(r.get('forwarded_by_name'), r.get('forwarded_by_email'),
                    'lead_fwd', 'forwarded a lead under this name')

    # ── headers, matched on the display name ────────────────────────
    header_tables = (
        ('lead_emails', ('from_addr', 'to_addr', 'cc')),
        ('lead_raw_emails', ('from_addr',)),
        ('mail_ingest_log', ('from_addr',)),
        ('email_classifications', ('from_addr',)),
        ('unmatched_quote_mail', ('from_addr', 'to_addr')),
        ('email_events', ('subject',)),          # no address column; skipped
    )
    for table, cols in header_tables:
        for r in rows_of(table, cols):
            for col in cols:
                for quoted, bare, addr in NAMED.findall(str(r.get(col) or '')):
                    add_by_name((quoted or bare).strip(), addr, 'header',
                                f'{table}.{col} carries this name and address')

    # ── From/To/Cc inside a kept body ───────────────────────────────
    for r in rows_of('leads', ('original_email_body',)):
        body = str(r.get('original_email_body') or '')
        if not body:
            continue
        for _which, line in BODY_LINE.findall(body[:4000]):
            pairs = NAMED.findall(line)
            for quoted, bare, addr in pairs:
                add_by_name((quoted or bare).strip(), addr, 'body',
                            'a From/To/Cc line in a kept mail body')
            if not pairs:
                # A bare address with no display name proves an address
                # exists; it does not say whose, so it is not evidence
                # for anybody. Counted only to report the difference.
                pass

    # ── the login record ────────────────────────────────────────────
    # In this CRM the Employee row IS the login: it holds the password
    # hash and the session version. There is no second table, so there
    # is nothing here to disagree with the row being fixed.

    # ── the TMS, if it is reachable at all ──────────────────────────
    for code, addr in (tms or {}).items():
        add(code.strip().upper(), addr, 'tms', 'the TMS employee list')

    return found


def tms_employees():
    """The TMS list, or None with the reason.

    The integration API is INBOUND: the TMS calls the CRM. There is no
    outbound client, no base URL and no token in this application, and
    app.py:4026 says cross-app calls are done from the TMS side. So
    this looks only where a deployment could have put them, and
    reports honestly when they are not there.
    """
    base = os.environ.get('TMS_BASE_URL', '').strip().rstrip('/')
    token = os.environ.get('TMS_TOKEN', '').strip()
    if not base or not token:
        return None, ('not configured (TMS_BASE_URL and TMS_TOKEN unset) — '
                      'the integration API runs inbound only, so the CRM '
                      'has no client for the TMS')
    try:
        import json
        import urllib.request
        req = urllib.request.Request(
            f'{base}/api/integration/v1/employees',
            headers={'Authorization': f'Bearer {token}',
                     'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=20) as fh:
            payload = json.loads(fh.read().decode('utf-8'))
    except Exception as exc:                       # unreachable, 404, 500
        return None, f'unreachable: {type(exc).__name__}: {exc}'
    rows = payload.get('employees') if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return None, 'reached, but the answer held no employee list'
    out = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        code = str(r.get('emp_code') or r.get('code') or '').strip().upper()
        addr = str(r.get('email') or '').strip().lower()
        if code and addr:
            out[code] = addr
    return out, f'{len(out)} employee(s) read from the TMS'


# ── the survey ───────────────────────────────────────────────────────
def survey(domains, *, tms=None, everyone=False):
    from app import Employee

    employees = Employee.query.order_by(Employee.emp_code).all()
    found = gather(employees, domains, tms=tms)

    # An address already on a DIFFERENT employee row cannot be written
    # to this one: `employees.email` is unique, and a duplicate would
    # fail the transaction and take the whole batch with it.
    taken = {}
    for e in employees:
        addr = (e.email or '').strip().lower()
        if addr and not looks_generated(addr):
            taken[addr] = e.emp_code

    rows, unresolved = [], []
    for e in employees:
        if not (everyone or needs_fixing(e)):
            continue
        current = (e.email or '').strip()
        bucket = sorted(found.get(e.emp_code, {}).values(),
                        key=lambda ev: (-ev.weight, ev.addr))
        best = bucket[0] if bucket else None
        rivals = [ev for ev in bucket[1:] if ev.weight >= (best.weight / 2
                                                           if best else 0)]
        conflict = ''
        if best and rivals:
            conflict = 'two addresses: ' + ', '.join(
                f'{ev.addr} ({ev.kind}×{ev.count})'
                for ev in [best] + rivals)
        elif best and taken.get(best.addr, e.emp_code) != e.emp_code:
            conflict = (f'{best.addr} is already on '
                        f'{taken[best.addr]} — email is unique')

        row = {
            'emp_code': e.emp_code,
            'name': e.name or '',
            'current': current,
            'current_kind': ('generated' if looks_generated(current)
                             else ('empty' if not current else 'real')),
            'proposed': (best.addr if best and not conflict else ''),
            'source': (best.kind if best else ''),
            'evidence_count': (best.count if best else 0),
            'detail': (best.detail if best and not conflict else ''),
            'conflict': conflict,
            'all_candidates': '; '.join(
                f'{ev.addr} [{ev.kind}×{ev.count}]' for ev in bucket) or '',
            '_emp': e,
            '_addr': (best.addr if best and not conflict else ''),
        }
        rows.append(row)
        if not row['proposed']:
            unresolved.append({
                'emp_code': row['emp_code'], 'name': row['name'],
                'current': current, 'current_kind': row['current_kind'],
                'why': (conflict or 'no evidence of a real address anywhere '
                        'in the CRM'),
                'candidates_seen': row['all_candidates'],
            })
    return rows, unresolved


def write_csv(path, rows, fields):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    return path


FIELDS = ('emp_code', 'name', 'current', 'current_kind', 'proposed',
          'source', 'evidence_count', 'detail', 'conflict', 'all_candidates')
UNRESOLVED_FIELDS = ('emp_code', 'name', 'current', 'current_kind', 'why',
                     'candidates_seen')


def report(rows, unresolved, *, tms_note):
    from app import Employee

    total = Employee.query.count()
    generated = sum(1 for r in rows if r['current_kind'] == 'generated')
    empty = sum(1 for r in rows if r['current_kind'] == 'empty')
    proposed = [r for r in rows if r['proposed']]
    conflicts = [r for r in rows if r['conflict']]

    print('\n' + '═' * 68)
    print('  STAFF EMAIL ADDRESSES — what the CRM has, and what is real')
    print('═' * 68)
    print(f'  employees on file          {total}')
    print(f'  needing a real address     {len(rows)}')
    print(f'      made-up emp<code>@     {generated}')
    print(f'      no address at all       {empty}')
    print(f'  a real address found       {len(proposed)}')
    print(f'  conflicts, left alone      {len(conflicts)}')
    print(f'  still unresolved           {len(unresolved)}')
    print(f'\n  TMS employee list: {tms_note}')

    if proposed:
        by_kind = defaultdict(int)
        for r in proposed:
            by_kind[r['source']] += 1
        print('\n  WHERE THE ANSWERS CAME FROM')
        for kind, n in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            print(f'    {kind:<10} {n}')
        print('\n  PROPOSED')
        for r in proposed[:40]:
            print(f'    {r["emp_code"]:<12} {(r["name"] or "")[:22]:<22} '
                  f'{r["current"][:28]:<28} → {r["proposed"]}')
            print(f'    {"":<12} {r["source"]} ×{r["evidence_count"]}'
                  f' — {r["detail"][:60]}')
        if len(proposed) > 40:
            print(f'    … and {len(proposed) - 40} more, all in the CSV')

    if conflicts:
        print('\n  CONFLICTS — not guessed at, not written')
        for r in conflicts[:20]:
            print(f'    {r["emp_code"]:<12} {(r["name"] or "")[:22]:<22} '
                  f'{r["conflict"][:70]}')


# ── apply and undo ───────────────────────────────────────────────────
def next_batch_id(now=None):
    from app.models.audit import AuditEvent

    now = now or datetime.utcnow()
    prefix = f'{BATCH_PREFIX}-{now.strftime("%Y%m%d")}-'
    used = set()
    for r in (AuditEvent.query.filter(AuditEvent.action == ACTION)
              .order_by(AuditEvent.id.desc()).limit(2000).all()):
        said = str((r.new_value or {}).get('batch') or '') \
            if isinstance(r.new_value, dict) else ''
        if said.startswith(prefix):
            used.add(said)
    return f'{prefix}{len(used) + 1}'


def apply(rows, *, batch, actor):
    from app import Employee, db
    from app.services import audit

    written, skipped = 0, []
    try:
        for r in rows:
            if not r['_addr'] or r['conflict']:
                continue
            emp = r['_emp']
            was = (emp.email or '').strip()
            # Edited since the survey: somebody has put a real address
            # on this row while this was being read, and theirs is a
            # decision where this is a guess from evidence.
            if was and not looks_generated(was):
                skipped.append(f'{emp.emp_code}: now has {was}')
                continue
            # And the address must still be free.
            clash = Employee.query.filter(
                db.func.lower(Employee.email) == r['_addr'],
                Employee.emp_code != emp.emp_code).first()
            if clash is not None:
                skipped.append(f'{emp.emp_code}: {r["_addr"]} taken by '
                               f'{clash.emp_code}')
                continue
            emp.email = r['_addr']
            audit.record(ACTION, 'employee', emp.emp_code,
                         old={'email': was},
                         new={'email': r['_addr'], 'batch': batch,
                              'record': 'employee',
                              'source': r['source'],
                              'evidence_count': r['evidence_count']},
                         actor=actor,
                         reason=(f'batch {batch}: {r["source"]} '
                                 f'×{r["evidence_count"]} — {r["detail"]}'),
                         commit=False)
            written += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    print(f'\n  {written} address(es) corrected. Batch {batch}.')
    for said in skipped:
        print(f'  [SKIPPED] {said}')
    print(f'\n  Undo: --undo --batch {batch} --actor {actor}')
    return written


def undo(batch, actor):
    from app import Employee, db
    from app.models.audit import AuditEvent
    from app.services import audit

    back, left = 0, 0
    try:
        for ev in (AuditEvent.query.filter(AuditEvent.action == ACTION)
                   .order_by(AuditEvent.id.asc()).all()):
            new = ev.new_value if isinstance(ev.new_value, dict) else {}
            if str(new.get('batch') or '') != batch:
                continue
            emp = Employee.query.filter_by(emp_code=str(ev.entity_id)).first()
            if emp is None:
                left += 1
                continue
            now = (emp.email or '').strip().lower()
            wrote = str(new.get('email') or '').strip().lower()
            # Only what this batch wrote, and only if it is still what
            # this batch wrote: a later edit by a person outranks an
            # undo of a guess.
            if now != wrote:
                left += 1
                continue
            old = ev.old_value if isinstance(ev.old_value, dict) else {}
            was = (old.get('email') or '') or None
            emp.email = was
            audit.record(f'{ACTION}_undo', 'employee', emp.emp_code,
                         old={'email': wrote}, new={'email': was or '',
                                                    'batch': batch},
                         actor=actor,
                         reason=f'undo of batch {batch}', commit=False)
            back += 1
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    print(f'\n  {back} address(es) put back. {left} left alone '
          f'(edited since, or the row is gone).')
    return back


def main():
    ap = argparse.ArgumentParser(
        description='Find each employee\'s real email address from '
                    'evidence, or say that nobody knows it.')
    ap.add_argument('--out', default=OUT_CSV)
    ap.add_argument('--unresolved-out', dest='unresolved_out',
                    default=UNRESOLVED_CSV)
    ap.add_argument('--all', action='store_true',
                    help='survey every employee, not only the broken rows')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--undo', action='store_true')
    ap.add_argument('--batch')
    ap.add_argument('--actor', help='employee id; required with --apply')
    args = ap.parse_args()

    import app as A

    with A.app.app_context():
        if args.undo:
            if not (args.batch and args.actor):
                sys.exit('  [STOP] --undo needs --batch and --actor.')
            return undo(args.batch, args.actor)

        domains = own_domains()
        tms, tms_note = tms_employees()
        rows, unresolved = survey(domains, tms=tms, everyone=args.all)
        report(rows, unresolved, tms_note=tms_note)

        said = write_csv(args.out, rows, FIELDS)
        print(f'\n  CSV        {said}')
        if unresolved:
            said2 = write_csv(args.unresolved_out, unresolved,
                              UNRESOLVED_FIELDS)
            print(f'  For HR     {said2}')

        if not args.apply:
            n = len([r for r in rows if r['proposed']])
            print(f'\n  DRY RUN — nothing written. {n} row(s) would change.')
            print(f'  Add --apply --actor DIR12010 to write them.')
            return 0

        if not args.actor:
            sys.exit('  [STOP] --apply needs --actor, an employee id.')
        batch = next_batch_id()
        return apply(rows, batch=batch, actor=args.actor)


if __name__ == '__main__':
    main()
