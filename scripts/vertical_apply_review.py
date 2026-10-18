"""
CRM-VERTICAL-FIX-01 — apply the filled-in Review sheet.

The Review sheet holds the leads where only keywords disagreed with
Heavy Transport. The CRM does not know what those should be, so a
person fills in the "correct vertical" column and this applies what
they wrote.

    .venv/bin/python scripts/vertical_apply_review.py \\
        --file /var/www/procam-crm/docs/exports/vertical_repair_2026_10_18.xlsx \\
        --check

    .venv/bin/python scripts/vertical_apply_review.py \\
        --file ...xlsx --apply

Rules, all of them refusals rather than guesses:

    a blank cell is skipped — "I am not sure" is a real answer and
    the commonest one;
    a value that is not a vertical the CRM knows is refused, with the
    list printed, rather than written as a new free-text vertical;
    a lead that is no longer on Heavy Transport is skipped — somebody
    has dealt with it since, and this must not overwrite them;
    the same value as the lead already has is skipped as a no-op.

One transaction, and an audit row per lead naming the person who
filled the sheet in.
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

ACTION = 'lead.vertical_corrected'
SHEET = 'Review'
COLUMN = 'correct vertical'


def known_verticals():
    """What a reviewer may choose. The same list the dropdown offers,
    so the sheet cannot suggest a value this then refuses."""
    return set(va.acceptable_verticals()) | set(alias_map())


def alias_map():
    try:
        from app.models.email_admin import VerticalAlias
        return {a.alias: a.vertical for a in VerticalAlias.query.all()}
    except Exception:
        db.session.rollback()
        return {}


def read_sheet(path):
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    if SHEET not in wb.sheetnames:
        raise SystemExit(f'No "{SHEET}" sheet in {path}. Sheets: '
                         f'{", ".join(wb.sheetnames)}')
    ws = wb[SHEET]
    header = [c.value for c in ws[2]]
    if COLUMN not in header:
        raise SystemExit(f'No "{COLUMN}" column on the {SHEET} sheet. '
                         f'Columns: {", ".join(str(h) for h in header)}')
    # The full workbook calls it `lead_id`; the per-vertical split
    # files call it `lead`, because a reader does not need to be told
    # it is an id. Both are the same column and this reads either.
    id_col = next((c for c in ('lead_id', 'lead') if c in header), None)
    if id_col is None:
        raise SystemExit(f'No lead column on the {SHEET} sheet. '
                         f'Columns: {", ".join(str(h) for h in header)}')
    out = []
    for row in ws.iter_rows(min_row=3):
        item = dict(zip(header, [c.value for c in row]))
        if not item.get(id_col):
            continue
        item['lead_id'] = item[id_col]
        out.append(item)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', required=True)
    ap.add_argument('--by', default='', help='who filled the sheet in')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if args.apply == args.check:
        raise SystemExit('Choose one: --check or --apply.')

    with app.app_context():
        print(f'database: {db.engine.url}')
        print(f'workbook: {args.file}')
        print(f'MODE:     {"APPLY — leads will be written" if args.apply else "DRY RUN — nothing is written"}\n')

        rows = read_sheet(args.file)
        known, alias = known_verticals(), alias_map()

        apply_to, blank, unknown, moved, noop = [], 0, [], [], 0
        for item in rows:
            want = str(item.get(COLUMN) or '').strip()
            if not want:
                blank += 1
                continue
            if want not in known:
                unknown.append((item['lead_id'], want))
                continue
            # A dead label is written as the vertical it means.
            target = alias.get(want, want)
            lead = db.session.get(Lead, int(item['lead_id']))
            if lead is None or (lead.procam_vertical or '') != va.TARGET:
                moved.append((item['lead_id'],
                              lead.procam_vertical if lead else '(deleted)'))
                continue
            if (lead.procam_vertical or '') == target:
                noop += 1
                continue
            apply_to.append({'lead_id': lead.id, 'company': lead.company or '',
                             'from': lead.procam_vertical, 'to': target,
                             'written': want})

        print(f'  rows on the sheet            {len(rows):>5}')
        print(f'  blank — left for later       {blank:>5}')
        print(f'  would be written             {len(apply_to):>5}')
        print(f'  already right                {noop:>5}')
        print(f'  moved since — skipped        {len(moved):>5}')
        print(f'  value not recognised         {len(unknown):>5}')

        if apply_to:
            print('\n  by target vertical:')
            for v, n in Counter(r['to'] for r in apply_to).most_common():
                print(f'      {v:<24} {n:>4}')

        if unknown:
            print('\n  REFUSED — not a vertical the CRM knows. Fix the '
                  'spelling or add the vertical first:')
            for lid, want in unknown[:30]:
                print(f'      {lid:<8} {want!r}')
            print(f'\n  Recognised values: {", ".join(sorted(known))}')

        if moved:
            print('\n  Skipped — no longer on Heavy Transport, so somebody '
                  'has dealt with them since:')
            for lid, now in moved[:20]:
                print(f'      {lid:<8} now {now}')

        if args.check:
            print('\nDRY RUN — nothing was written. Re-run with --apply.')
            if unknown:
                print('  Note: the refused rows above will still be '
                      'refused. Correct them in the workbook first.')
            return

        from app.services import audit

        who = args.by.strip() or 'CRM-VERTICAL-FIX-01'
        reason = (f'corrected from the CRM-VERTICAL-FIX-01 review sheet'
                  + (f', filled in by {who}' if args.by.strip() else ''))
        written = 0
        try:
            for row in apply_to:
                lead = db.session.get(Lead, row['lead_id'])
                if lead is None or (lead.procam_vertical or '') != va.TARGET:
                    continue
                was = lead.procam_vertical
                lead.procam_vertical = row['to']
                lead.updated_at = datetime.utcnow()
                audit.record(ACTION, 'Lead', lead.id,
                             old={'procam_vertical': was},
                             new={'procam_vertical': row['to']},
                             actor=who, reason=reason, commit=False)
                written += 1
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK — nothing was written: '
                  f'{type(exc).__name__}: {str(exc)[:200]}')
            raise SystemExit(1)

        print(f'\n  {written} lead(s) written, in one transaction.')
        print(f'  Each carries an audit row reading "{reason}".')


if __name__ == '__main__':
    main()
