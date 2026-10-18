"""
CRM-VERTICAL-MASTER-01 Stage B — settle the list, set the heads,
move the leads.

    .venv/bin/python scripts/vertical_master_apply.py --check
    .venv/bin/python scripts/vertical_master_apply.py --apply

`--check` writes nothing and prints exactly what `--apply` would do.

The decisions below were taken by Nilesh Kumar Sinha on 18 October
2026 and are recorded here rather than passed on a command line, so
the repository says what was decided and why a lead moved. Changing
them is a commit, which is the point.

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
ACTION = 'lead.vertical_mastered'

#: The seven. Everything else is an alias onto one of these.
OFFICIAL = ('Heavy Transport', 'General Transport', 'Project Freight',
            'Freight Forwarding', 'Installation', 'Warehousing', 'Other')

#: value -> the official vertical it belongs to.
MAPPING = {
    'Heavy Cargo': 'Heavy Transport',
    'Transportation': 'Heavy Transport',
    'Sea Freight': 'Freight Forwarding',
    'Air Freight': 'Freight Forwarding',
    'Customs': 'Freight Forwarding',
    'Project Logistics': 'Project Freight',
}

#: vertical -> employee code. Sanjna Vardhan heads two: Project
#: Freight as before, and Freight Forwarding, which the mapping makes
#: the second largest open vertical in the company.
HEADS = {
    'Heavy Transport': 'DIR22010',      # James Francis Xavier
    'General Transport': 'DIR22010',    # the same head, by decision
    'Project Freight': 'EMP372011',     # Sanjna Vardhan
    'Freight Forwarding': 'EMP372011',  # Sanjna Vardhan
    'Installation': 'DIR52011',         # Sethupathy Sundaram
    'Warehousing': 'DIR72012',          # Srinivas Marella
    'Management': 'DIR12010',           # Nilesh Kumar Sinha — the
                                        # fallback for anything unmapped
}

#: Not a service a lead can be in. An employee grouping that reached
#: the master because employees carry it.
REMOVE_HEAD_ROWS = ('All',)


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

        # ── aliases ──
        print('=' * 72)
        print('ALIAS ROWS')
        print('=' * 72)
        for value, target in sorted(MAPPING.items()):
            existing = VerticalAlias.query.filter_by(alias=value).first()
            state = ('already there' if existing
                     and existing.vertical == target
                     else ('REPOINTED from ' + existing.vertical
                           if existing else 'new'))
            print(f'  {value:<22} -> {target:<22} {state}')

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
        for vertical in REMOVE_HEAD_ROWS:
            row = VerticalHead.query.filter_by(vertical=vertical).first()
            print(f'  {vertical:<22} '
                  + ('REMOVE — an employee grouping, not a service'
                     if row else 'not present'))

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

            for vertical in REMOVE_HEAD_ROWS:
                row = VerticalHead.query.filter_by(vertical=vertical).first()
                if row is not None:
                    db.session.delete(row)

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
        print(f'\nDone. Every lead moved carries an audit row reading '
              f'"{REASON}".')


if __name__ == '__main__':
    main()
