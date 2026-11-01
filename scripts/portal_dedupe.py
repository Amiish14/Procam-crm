"""
One lead per portal number — for the duplicates already in the CRM.

    .venv/bin/python scripts/portal_dedupe.py --check
    .venv/bin/python scripts/portal_dedupe.py --apply

CRM-COCKPIT-02 §5.6. A reverse auction sends two or three mails — the
alert, a reminder, often a corrigendum — each on its own thread with
its own message id, so the thread dedupe cannot see that they are one
enquiry. Eight duplicates were merged by hand on 01 November.

From now on the intake joins them by the number (RA-57103,
eRFQ-41688, a GeM bid number, a tender number). This fills in the
number on the leads already here and folds the sets that are left.

Nothing is deleted. `lead_merge.merge` moves the mails, notes, tasks
and activities onto the lead that is kept, and leaves the other one
closed as a Duplicate pointing at it — CRM-NO-DELETE-01, unchanged.

Which lead is kept: the oldest, because it is the one the owner has
been working and the one anything else already references. Where the
newer one has a deadline and the older one does not, the deadline
moves across — the corrigendum is often the only mail that states it.
"""
import argparse
import os
import sys
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402

BATCH = 'cockpit02.portal_key'
NOTE = ('01-Nov-2026 CRM-COCKPIT-02: portal number read from the '
        'enquiry subject (rule: §5.6)')


def read_keys():
    """[(lead, key)] for open leads whose number is not recorded."""
    from app.services import quote_due as qd
    from app.services import sales_rules as rules

    out = []
    for lead in (Lead.query
                 .filter(Lead.is_archived.isnot(True),
                         Lead.stage.in_(rules.open_stages()),
                         Lead.portal_key.is_(None))
                 .order_by(Lead.id).all()):
        key = qd.portal_key(lead.original_email_subject or '',
                            lead.original_email_body or '')
        if key:
            out.append((lead, key))
    return out


def sets_to_merge(extra=None):
    """{key: [leads]} where more than one open lead shares a number."""
    from app.services import sales_rules as rules

    groups = defaultdict(list)
    for lead in (Lead.query
                 .filter(Lead.is_archived.isnot(True),
                         Lead.stage.in_(rules.open_stages()),
                         Lead.portal_key.isnot(None))
                 .order_by(Lead.id).all()):
        groups[lead.portal_key].append(lead)
    for lead, key in (extra or []):
        groups[key].append(lead)
    for key in list(groups):
        rows = sorted({l.id: l for l in groups[key]}.values(),
                      key=lambda l: l.id)
        if len(rows) < 2:
            del groups[key]
        else:
            groups[key] = rows
    return groups


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--keys-only', action='store_true',
                    help='record the numbers, merge nothing')
    ap.add_argument('--actor', default='CRM-COCKPIT-02')
    args = ap.parse_args()
    if not (args.check or args.apply):
        args.check = True

    with app.app_context():
        from app.services import bulk_log, lead_merge, quote_due as qd

        print(f'database: {db.engine.url}')
        print('MODE:     ' + ('APPLY' if args.apply
                              else 'CHECK — nothing written') + '\n')

        found = read_keys()
        by_kind = Counter(k.split('-', 1)[0] for _l, k in found)
        print(f'  open leads carrying a portal number, '
              f'not yet recorded: {len(found)}')
        for kind, n in by_kind.most_common():
            print(f'    {kind:<10} {n}')

        groups = sets_to_merge(found)
        print(f'\n  numbers with more than one open lead: '
              f'{len(groups)}')
        merges = 0
        for key, rows in sorted(groups.items()):
            keep = rows[0]
            others = rows[1:]
            merges += len(others)
            print(f'    {key:<16} keep #{keep.id} '
                  f'({(keep.company or "")[:22]}), fold '
                  + ', '.join(f'#{l.id}' for l in others))
            for other in others:
                if other.quote_due_at and not keep.quote_due_at:
                    print(f'        #{other.id} carries the deadline '
                          f'{qd.shown(other.quote_due_at)} — it moves '
                          f'to #{keep.id}')
        print(f'\n  leads that would be folded: {merges}')

        if args.check:
            print('\nCHECK ONLY — nothing written. --apply records the '
                  'numbers and\nfolds the sets; nothing is deleted.')
            return 0

        with bulk_log.batch(BATCH, note=NOTE, actor=args.actor) as log:
            for lead, key in found:
                log.set(lead, 'portal_key', key)
            batch_name, keys_written = log.name, len(log)
        try:
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            return 1
        print(f'\n  {keys_written} portal number(s) recorded.')
        print(f'  reversible log batch: {batch_name}')

        if args.keys_only:
            print('\n  --keys-only: nothing merged.')
            return 0

        folded, problems = 0, []
        for key, rows in sorted(sets_to_merge().items()):
            keep, others = rows[0], rows[1:]
            for other in others:
                # The deadline before the merge: `merge` closes the
                # folded lead, and a deadline on a closed lead is
                # invisible to every tile.
                if other.quote_due_at and not keep.quote_due_at:
                    from app.services import cockpit02_gate as c2
                    c2.apply_due(keep, other.quote_due_at,
                                 actor=args.actor,
                                 source=other.quote_due_source or 'user',
                                 reason_code='set',
                                 reason_text=f'from folded lead '
                                             f'#{other.id}',
                                 confirmed=bool(
                                     other.quote_due_confirmed))
                ok, detail = lead_merge.merge(
                    other, keep, actor=args.actor,
                    reason=f'Same portal number ({key}) as #{keep.id} '
                           f'— CRM-COCKPIT-02 §5.6')
                if ok:
                    folded += 1
                else:
                    problems.append(f'#{other.id} -> #{keep.id}: '
                                    f'{detail}')
        print(f'  {folded} lead(s) folded, each closed as a Duplicate '
              f'pointing at the one kept.')
        for line in problems[:20]:
            print(f'      !! {line}')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
