"""
CRM-VERTICAL-MASTER-01 Stage B — settle the list, set the heads,
move the leads.

    .venv/bin/python scripts/vertical_master_apply.py --check
    .venv/bin/python scripts/vertical_master_apply.py --apply

`--check` writes nothing and prints exactly what `--apply` would do.

The decisions below were taken by Nilesh Kumar Sinha — the master
list on 18 October 2026, narrowed to four on 31 October 2026 — and
are recorded here rather than passed on a command line, so the
repository says what was decided and why a lead moved. Changing them
is a commit, which is the point.

31 October: four master verticals, not seven
    Heavy Transport, Project Freight, Installation, Warehousing.
    Freight Forwarding becomes an alias of Project Freight and
    General Transport an alias of Heavy Transport.

    A master vertical is never deleted. Its head row is marked
    inactive and an alias row is written in the same transaction, so
    the resolver stops offering it and starts folding it onto the
    vertical it belongs to — and the row that recorded the decision
    to have it is still there to read.

    Aliases are flattened, not chained. Sea Freight, Air Freight and
    Customs pointed at Freight Forwarding, which is now an alias
    itself; `master_vertical()` resolves one hop, so an alias of an
    alias returns a name that is not a master at all. They are
    repointed at Project Freight.

    A master vertical this file has no mapping for is reported and
    left alone, because which of the four it belongs to is a
    decision and not something to infer from a name.

Three changes, in one transaction on apply:

  aliases   a row per value that is not official, naming the vertical
            it belongs to. The review dropdown reads this table, so
            an aliased value stops being offered the moment it is
            added.
  heads     every official vertical gets one, including Management —
            the fallback for anything unmapped, which has had none
            since the master was created and so escalated roughly
            1,100 open leads to nobody.
  leads     every lead on a non-official value moves to the vertical
            it belongs to.

What it will not touch
    leads still undecided on the 296-row review files. They sit on
    Heavy Transport awaiting a person's judgement, and no mapping
    here starts from Heavy Transport — but the guard is explicit and
    tested rather than left to that coincidence.

    blank verticals (655 closed leads) and accounts, both left alone
    by decision.
"""
import argparse
import os
import sys
from collections import Counter
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402

REASON = 'vertical master, CRM-VERTICAL-MASTER-01'
#: On the retired head row and its audit row, so the
#: narrowing to four is readable from the row itself.
RETIRE_NOTE = ('four master verticals only, 31 Oct 2026 — kept as '
               'an alias')
ACTION = 'lead.vertical_mastered'

#: The four. Everything else is an alias onto one of these.
#: Decision of 31 October 2026, superseding the seven of 18 October.
OFFICIAL = ('Heavy Transport', 'Project Freight', 'Installation',
            'Warehousing')

#: value -> the official vertical it belongs to. Every target is one
#: of the four: an alias whose target is itself an alias resolves to
#: a name that is not a master, because `master_vertical()` takes
#: one hop and stops.
MAPPING = {
    'Heavy Cargo': 'Heavy Transport',
    'Transportation': 'Heavy Transport',
    'General Transport': 'Heavy Transport',
    'Sea Freight': 'Project Freight',
    'Air Freight': 'Project Freight',
    'Customs': 'Project Freight',
    'Freight Forwarding': 'Project Freight',
    'Project Logistics': 'Project Freight',
}

#: vertical -> employee code. Sanjna Vardhan now heads one vertical
#: and not two: Freight Forwarding folds into Project Freight, which
#: she already held.
HEADS = {
    'Heavy Transport': 'DIR22010',      # James Francis Xavier
    'Project Freight': 'EMP372011',     # Sanjna Vardhan
    'Installation': 'DIR52011',         # Sethupathy Sundaram
    'Warehousing': 'DIR72012',          # Srinivas Marella
    'Management': 'DIR12010',           # Nilesh Kumar Sinha — the
                                        # fallback for anything unmapped
}

#: Master rows that stay even though they are not one of the four.
#: Neither is a service anybody sells; both are where a record goes
#: when nothing else fits, and an alias cannot do that job —
#: `master_vertical()` falls back to Management by name, so making
#: Management an alias of a service vertical would send every
#: unplaceable record to a service head.
NOT_A_SERVICE_KEEP = ('Management', 'Other')

#: Not a service a lead can be in: an employee grouping that reached
#: the master because employees carry it. Retired, not deleted — the
#: 31 October decision is that a master vertical is never deleted,
#: and "we once thought All was a vertical" is worth being able to
#: read a year from now.
RETIRE_HEAD_ROWS = ('All',)


def protected_lead_ids():
    """Leads awaiting a person's judgement on the review files."""
    from app.services import vertical_audit as va
    try:
        return {r['lead_id'] for r in va.survey(since='2026-09-01')['review']}
    except Exception:
        db.session.rollback()
        return set()


def lead_plan(protected):
    """[(lead, from, to)] for every lead that should move."""
    out, skipped = [], []
    for value, target in MAPPING.items():
        rows = (Lead.query
                .filter(Lead.procam_vertical == value,
                        Lead.is_archived.isnot(True)).all())
        for lead in rows:
            if lead.id in protected:
                skipped.append((lead.id, value))
                continue
            out.append((lead, value, target))
    return out, skipped


def master_rows():
    """Every master vertical the database holds, and its verdict.

    Four verdicts, and the last is the one to read: a master this
    file has no mapping for is nobody's decision yet.
    """
    from app.models.email_admin import VerticalHead

    rows = []
    for row in VerticalHead.query.order_by(VerticalHead.vertical).all():
        name = row.vertical
        if name in OFFICIAL:
            verdict, detail = 'official', 'one of the four'
        elif name in NOT_A_SERVICE_KEEP:
            verdict, detail = 'kept', 'not a service — where unplaceable ' \
                                      'records go'
        elif name in MAPPING:
            verdict, detail = 'alias', 'becomes an alias of ' + MAPPING[name]
        elif name in RETIRE_HEAD_ROWS:
            verdict, detail = 'retired', 'an employee grouping, not a service'
        else:
            verdict, detail = 'YOUR DECISION', 'no mapping — which of the ' \
                                               'four does it belong to?'
        rows.append({'vertical': name, 'verdict': verdict,
                     'detail': detail,
                     'head': row.head_emp_code or '—',
                     'active': bool(row.active is not False),
                     'leads': 0})
    return rows


def lead_counts_by_value():
    """Unarchived leads per written vertical, for the master report."""
    rows = (db.session.query(Lead.procam_vertical, db.func.count(Lead.id))
            .filter(Lead.is_archived.isnot(True))
            .group_by(Lead.procam_vertical).all())
    return {(v or ''): n for v, n in rows}


def head_rows_to_retire():
    """Master rows that become aliases, so their head row goes
    inactive. Never deleted."""
    from app.models.email_admin import VerticalHead

    names = [n for n in MAPPING if n not in OFFICIAL] \
        + list(RETIRE_HEAD_ROWS)
    out = []
    for name in names:
        row = VerticalHead.query.filter_by(vertical=name).first()
        if row is not None and row.active is not False:
            out.append(row)
    return out


def aliases_to_flatten():
    """[(alias, old target, new target)] for alias rows pointing at a
    value that is no longer a master.

    `master_vertical()` takes one hop: an alias of an alias answers
    with a name that is in no head table, so the record resolves to
    nothing rather than to the vertical it belongs to.
    """
    from app.models.email_admin import VerticalAlias

    out = []
    for row in VerticalAlias.query.order_by(VerticalAlias.alias).all():
        target = row.vertical
        if target in OFFICIAL or target in NOT_A_SERVICE_KEEP:
            continue
        want = MAPPING.get(target) or MAPPING.get(row.alias)
        if want and want != target:
            out.append((row.alias, target, want))
    return out


def describe_heads():
    from app.models.email_admin import VerticalHead
    from app import Employee

    by_code = {e.emp_code: e for e in Employee.query.all()}
    rows = []
    for vertical, code in HEADS.items():
        existing = VerticalHead.query.filter_by(vertical=vertical).first()
        was = (existing.head_emp_code if existing else None) or '—'
        emp = by_code.get(code)
        rows.append({
            'vertical': vertical, 'was': was, 'now': code,
            'name': emp.name if emp else 'NOT AN EMPLOYEE ON FILE',
            'active': bool(emp and emp.is_active),
            'action': ('unchanged' if was == code
                       else ('new row' if existing is None else 'changed')),
        })
    return rows


def counts_by_vertical():
    rows = (db.session.query(Lead.procam_vertical, db.func.count(Lead.id))
            .filter(Lead.is_archived.isnot(True))
            .group_by(Lead.procam_vertical).all())
    return {(v or '(blank)'): n for v, n in rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if args.apply == args.check:
        raise SystemExit('Choose one: --check or --apply.')

    with app.app_context():
        from app.models.email_admin import VerticalAlias, VerticalHead

        print(f'database: {db.engine.url}')
        print(f'MODE:     {"APPLY — the database will be written" if args.apply else "DRY RUN — nothing is written"}\n')

        before = counts_by_vertical()
        protected = protected_lead_ids()
        moves, skipped = lead_plan(protected)
        head_rows = describe_heads()

        # ── master verticals ──
        masters = master_rows()
        per_value = lead_counts_by_value()
        for row in masters:
            row['leads'] = per_value.get(row['vertical'], 0)
        print('=' * 72)
        print('MASTER VERTICALS — four only, by decision of 31 October')
        print('=' * 72)
        print(f'  {"vertical":<22} {"head":<12} {"leads":>6}  verdict')
        for row in masters:
            mark = '' if row['active'] else '  (already inactive)'
            print(f'  {row["vertical"]:<22} {row["head"]:<12} '
                  f'{row["leads"]:>6}  {row["verdict"]}{mark}')
            print(f'  {"":<22} {"":<12} {"":>6}  {row["detail"]}')
        mine = [r for r in masters if r['verdict'] == 'YOUR DECISION']
        if mine:
            print('\n  WAITING ON YOU — these master verticals have no '
                  'mapping in this file,')
            print('  so nothing happens to them. Tell me which of the '
                  'four each belongs to')
            print('  and it becomes an alias in the same way:')
            for row in mine:
                print(f'      {row["vertical"]:<24} {row["leads"]:>6} '
                      f'lead(s), head {row["head"]}')

        retire = head_rows_to_retire()
        print('\n  head rows to mark inactive (never deleted): '
              f'{len(retire)}')
        for row in retire:
            print(f'      {row.vertical:<24} head {row.head_emp_code or "—"}')

        # ── aliases ──
        print('\n' + '=' * 72)
        print('ALIAS ROWS')
        print('=' * 72)
        for value, target in sorted(MAPPING.items()):
            existing = VerticalAlias.query.filter_by(alias=value).first()
            state = ('already there' if existing
                     and existing.vertical == target
                     else ('REPOINTED from ' + existing.vertical
                           if existing else 'new'))
            print(f'  {value:<22} -> {target:<22} {state}')

        flatten = aliases_to_flatten()
        if flatten:
            print('\n  flattened — these pointed at a value that is no '
                  'longer a master,')
            print('  and master_vertical() takes one hop, so they '
                  'resolved to nothing:')
            for alias, was, now in flatten:
                print(f'      {alias:<22} {was} -> {now}')

        # ── heads ──
        print('\n' + '=' * 72)
        print('HEADS')
        print('=' * 72)
        print(f'  {"vertical":<22} {"was":<12} {"now":<12} '
              f'{"name":<26} {"":<10}')
        for h in head_rows:
            flag = '' if h['active'] else '  <<< NOT ACTIVE'
            print(f'  {h["vertical"]:<22} {h["was"]:<12} {h["now"]:<12} '
                  f'{h["name"][:26]:<26} {h["action"]}{flag}')
        for vertical in RETIRE_HEAD_ROWS:
            row = VerticalHead.query.filter_by(vertical=vertical).first()
            print(f'  {vertical:<22} '
                  + ('RETIRE — an employee grouping, not a service; '
                     'the row stays' if row else 'not present'))

        # ── leads ──
        print('\n' + '=' * 72)
        print('LEADS TO MOVE')
        print('=' * 72)
        tally = Counter((frm, to) for _l, frm, to in moves)
        print(f'  {"from":<22} {"to":<22} {"leads":>7}')
        for (frm, to), n in sorted(tally.items(), key=lambda kv: -kv[1]):
            print(f'  {frm:<22} {to:<22} {n:>7}')
        print(f'  {"":<22} {"TOTAL":<22} {len(moves):>7}')

        print(f'\n  protected (undecided on the review files): '
              f'{len(protected)}')
        print(f'  of those, in the way of a mapping and therefore '
              f'skipped: {len(skipped)}')

        if args.check:
            print('\n  Before, by vertical:')
            for v, n in sorted(before.items(), key=lambda kv: -kv[1]):
                print(f'      {v:<24} {n:>6}')
            print('\nDRY RUN — nothing was written. Re-run with --apply.')
            return

        # ── one transaction ──
        from app.services import audit

        written = 0
        try:
            for value, target in MAPPING.items():
                row = VerticalAlias.query.filter_by(alias=value).first()
                if row is None:
                    row = VerticalAlias(alias=value)
                    db.session.add(row)
                row.vertical = target
                row.updated_by = 'CRM-VERTICAL-MASTER-01'

            for vertical, code in HEADS.items():
                row = VerticalHead.query.filter_by(vertical=vertical).first()
                if row is None:
                    row = VerticalHead(vertical=vertical)
                    db.session.add(row)
                row.head_emp_code = code
                row.active = True
                row.updated_by = 'CRM-VERTICAL-MASTER-01'
                row.note = REASON

            for alias, _was, want in aliases_to_flatten():
                row = VerticalAlias.query.filter_by(alias=alias).first()
                if row is not None:
                    row.vertical = want
                    row.updated_by = 'CRM-VERTICAL-MASTER-01'

            # A master vertical is never deleted. The alias rows above
            # are written in this same transaction, so the moment the
            # head row goes inactive the resolver folds the value onto
            # the vertical it belongs to rather than onto nothing.
            for row in head_rows_to_retire():
                row.active = False
                row.updated_by = 'CRM-VERTICAL-MASTER-01'
                row.note = (RETIRE_NOTE + ' -> '
                            + MAPPING.get(row.vertical, 'not a service'))
                audit.record('vertical.master_retired', 'VerticalHead',
                             row.id, old={'active': True},
                             new={'active': False,
                                  'alias_of': MAPPING.get(row.vertical, '')},
                             actor='CRM-VERTICAL-MASTER-01',
                             reason=RETIRE_NOTE, commit=False)

            for lead, frm, to in moves:
                if lead.procam_vertical != frm:
                    continue        # moved under us since the plan
                lead.procam_vertical = to
                lead.updated_at = datetime.utcnow()
                audit.record(ACTION, 'Lead', lead.id,
                             old={'procam_vertical': frm},
                             new={'procam_vertical': to},
                             actor='CRM-VERTICAL-MASTER-01',
                             reason=REASON, commit=False)
                written += 1
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK — nothing was written: '
                  f'{type(exc).__name__}: {str(exc)[:200]}')
            raise SystemExit(1)

        try:
            from app.services import vertical_heads
            vertical_heads.cache_clear()
            vertical_heads.sync_employee_flags(actor='CRM-VERTICAL-MASTER-01')
        except Exception:
            pass

        after = counts_by_vertical()
        print(f'\n  {written} lead(s) moved, in one transaction.')
        print('\n  Before and after, by vertical:')
        print(f'      {"vertical":<24} {"before":>8} {"after":>8} {"move":>8}')
        for v in sorted(set(before) | set(after),
                        key=lambda k: -(after.get(k, 0))):
            b, a = before.get(v, 0), after.get(v, 0)
            print(f'      {v:<24} {b:>8} {a:>8} '
                  f'{(a - b) if a != b else 0:>+8}')

        from app.services import vertical_audit as va
        print('\n  the review dropdown now offers: '
              + ', '.join(va.acceptable_verticals(for_choice=True)))
        print('  master verticals, active: '
              + ', '.join(sorted(
                  r['vertical'] for r in master_rows() if r['active'])))
        print(f'\nDone. Every lead moved carries an audit row reading '
              f'"{REASON}".')


if __name__ == '__main__':
    main()
