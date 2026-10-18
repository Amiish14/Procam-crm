"""
CRM-VERTICAL-FIX-01 Stage B — put the verticals back.

The lead edit form offered four verticals against the eleven leads
carry. A `<select>` with no option matching the record selects option
zero, so saving anything at all rewrote the vertical to Heavy
Transport. The form is fixed. This repairs what it did.

    .venv/bin/python scripts/vertical_restore.py --check
    .venv/bin/python scripts/vertical_restore.py --apply

`--check` writes nothing and prints exactly what `--apply` would do.
Run it first, every time.

What is restored, and what is not
    restore    the audit trail holds the previous value and that
               value is a vertical in its own right. 90 leads.
    remap      the previous value is `Project Logistics`, a dead
               label for Project Freight. Writing it back would
               reintroduce the label; the lead is set to Project
               Freight instead. 5 leads.
    skip       `Transportation` — a dead label for Heavy Transport,
               which is what the lead already carries, so restoring
               is a no-op. 35 leads.
    skip       the previous value was one of the four the broken form
               *did* offer, so the defect cannot explain it: the
               select matched and option zero never fired. Somebody
               chose it. 4 leads, listed with their exact stored
               value so a person can check them.

One transaction. Every lead gets an audit row carrying the reason, so
a year from now the trail says why its vertical moved rather than
leaving another unexplained change.
"""
import argparse
import os
import sys
from collections import Counter
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402
from app.services import vertical_audit as va             # noqa: E402

REASON = 'restored after form defect, CRM-VERTICAL-FIX-01'
ACTION = 'lead.vertical_restored'

#: The four the broken form offered. A lead whose vertical was one of
#: these matched an option, so option zero never fired and the defect
#: cannot be the explanation.
FORM_OFFERED = ('Heavy Transport', 'Project Freight', 'Warehousing',
                'Installation')


def _aliases():
    try:
        from app.models.email_admin import VerticalAlias
        return {a.alias: a.vertical for a in VerticalAlias.query.all()}
    except Exception:
        db.session.rollback()
        return {}


def _counts():
    rows = (db.session.query(Lead.procam_vertical, db.func.count(Lead.id))
            .filter(Lead.is_archived.isnot(True))
            .group_by(Lead.procam_vertical).all())
    return {(v or '(none)'): n for v, n in rows}


def plan(since):
    """(restore, remap, skip_alias, skip_deliberate, gone)."""
    alias = _aliases()
    data = va.survey(since=since)
    restore, remap, skip_alias, skip_deliberate, gone = [], [], [], [], []

    for row in data['recorded']:
        lead = db.session.get(Lead, row['lead_id'])
        if lead is None or (lead.procam_vertical or '') != va.TARGET:
            # Moved since the survey. Never write over that.
            gone.append({**row,
                         'now': (lead.procam_vertical if lead else '(deleted)')})
            continue
        was = row['restore_to']
        if was in FORM_OFFERED:
            skip_deliberate.append({**row, 'stored_exactly': repr(was)})
        elif was in alias and alias[was] == va.TARGET:
            skip_alias.append(row)
        elif was in alias:
            remap.append({**row, 'set_to': alias[was]})
        else:
            restore.append({**row, 'set_to': was})
    return restore, remap, skip_alias, skip_deliberate, gone


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='2026-09-01')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if args.apply == args.check:
        raise SystemExit('Choose one: --check or --apply.')

    with app.app_context():
        print(f'database: {db.engine.url}')
        print(f'MODE:     {"APPLY — leads will be written" if args.apply else "DRY RUN — nothing is written"}\n')

        since = datetime.fromisoformat(args.since)
        restore, remap, skip_alias, skip_deliberate, gone = plan(since)
        before = _counts()

        print('=' * 72)
        print(f'  restore to the recorded value   {len(restore):>4}')
        print(f'  remap a dead label              {len(remap):>4}')
        print(f'  skip — alias of Heavy Transport {len(skip_alias):>4}')
        print(f'  skip — deliberate               {len(skip_deliberate):>4}')
        if gone:
            print(f'  skip — moved since the survey   {len(gone):>4}')
        print(f'  TOTAL TOUCHED                   '
              f'{len(restore) + len(remap):>4}')

        print('\n  restoring, by target vertical:')
        for v, n in Counter(r['set_to'] for r in restore).most_common():
            print(f'      {v:<24} {n:>4}')
        for v, n in Counter(r['set_to'] for r in remap).most_common():
            print(f'      {v:<24} {n:>4}   (from a dead label)')

        if skip_deliberate:
            print('\n  NOT restored — the form offered this value, so the '
                  'defect cannot explain it.')
            print('  Check these by hand; the stored value is shown '
                  'exactly as it is held:')
            print(f'      {"lead":<8} {"company":<34} {"stored old value":<24} '
                  f'{"by":<12} when')
            for r in skip_deliberate:
                print(f'      {r["lead_id"]:<8} {r["company"][:34]:<34} '
                      f'{r["stored_exactly"]:<24} {r["rewritten_by"]:<12} '
                      f'{r["rewritten_at"]}')

        if gone:
            print('\n  NOT restored — moved since the survey:')
            for r in gone[:20]:
                print(f'      {r["lead_id"]:<8} now {r["now"]}')

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
            for row in restore + remap:
                lead = db.session.get(Lead, row['lead_id'])
                if lead is None or (lead.procam_vertical or '') != va.TARGET:
                    continue            # changed under us between plan and write
                was = lead.procam_vertical
                lead.procam_vertical = row['set_to']
                lead.updated_at = datetime.utcnow()
                audit.record(ACTION, 'Lead', lead.id,
                             old={'procam_vertical': was},
                             new={'procam_vertical': row['set_to']},
                             actor='CRM-VERTICAL-FIX-01',
                             reason=REASON, commit=False)
                written += 1
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK — nothing was written: '
                  f'{type(exc).__name__}: {str(exc)[:200]}')
            raise SystemExit(1)

        after = _counts()
        print(f'\n  {written} lead(s) written, in one transaction.')
        print('\n  Before and after, by vertical:')
        keys = sorted(set(before) | set(after),
                      key=lambda k: -(after.get(k, 0)))
        print(f'      {"vertical":<24} {"before":>8} {"after":>8} {"move":>8}')
        for v in keys:
            b, a = before.get(v, 0), after.get(v, 0)
            if b == a:
                continue
            print(f'      {v:<24} {b:>8} {a:>8} {a - b:>+8}')
        print('\nDone. Every lead written carries an audit row reading '
              f'"{REASON}".')


if __name__ == '__main__':
    main()
