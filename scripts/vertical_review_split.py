"""
CRM-VERTICAL-FIX-01 — the Review sheet, split into workable pieces.

296 rows in one sheet is a job nobody starts. One file per suggested
vertical is five jobs somebody can finish, and the biggest of them
can be worked in a block: 145 leads the words say are Sea Freight
read much faster together than scattered among Customs and
Warehousing.

Within each file the high-confidence rows come first, so the clearest
decisions are made while the reader is fresh and the doubtful ones
are left where they can be skipped.

    .venv/bin/python scripts/vertical_review_split.py
    .venv/bin/python scripts/vertical_review_split.py \\
        --from-file docs/exports/vertical_repair_2026_10_18.xlsx

By default the population is read from the database, so it reflects
what is true now — leads repaired or reclassified since the workbook
was made drop out of their own accord. `--from-file` splits an
existing workbook instead and carries over anything already filled
in, for when the work has started.

Each file carries a dropdown on the "correct vertical" column, so the
value cannot be mistyped and refused later. Blank stays a valid
answer, and the commonest one.
"""
import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402
from app.services import vertical_audit as va             # noqa: E402

#: The columns the reader needs and nothing else.
COLUMNS = ('lead', 'client', 'lead title', 'owner', 'confidence',
           'evidence', 'correct vertical')

#: Highest first.
RANK = {'high': 0, 'medium': 1, 'low': 2}


def valid_verticals():
    """What a reviewer may pick: the list the applier accepts, less
    the dead labels nobody should choose."""
    return va.acceptable_verticals(for_choice=True)


def slug(value):
    return re.sub(r'[^a-z0-9]+', '_', (value or 'unsorted').lower()).strip('_')


def from_database():
    data = va.survey(since='2026-09-01')
    out = []
    for r in data['review']:
        out.append({'lead': r['lead_id'], 'client': r['company'],
                    'lead title': r['title'], 'owner': r['pic'],
                    'confidence': r['confidence'],
                    'evidence': r['evidence'],
                    'correct vertical': '',
                    '_group': r['looks_like'] or 'unsorted'})
    return out


def from_file(path):
    from openpyxl import load_workbook

    wb = load_workbook(path, data_only=True)
    if 'Review' not in wb.sheetnames:
        raise SystemExit(f'No Review sheet in {path}')
    ws = wb['Review']
    header = [c.value for c in ws[2]]
    out = []
    for row in ws.iter_rows(min_row=3):
        item = dict(zip(header, [c.value for c in row]))
        if not item.get('lead_id'):
            continue
        out.append({'lead': item['lead_id'],
                    'client': item.get('company') or '',
                    'lead title': item.get('title') or '',
                    'owner': item.get('pic') or '',
                    'confidence': item.get('confidence') or '',
                    'evidence': item.get('evidence') or '',
                    'correct vertical': (str(item.get('correct vertical')
                                             or '').strip()),
                    '_group': item.get('looks_like') or 'unsorted'})
    return out


def write_one(path, group, rows, verticals):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = Workbook()
    ws = wb.active
    ws.title = 'Review'

    note = (f'{len(rows)} leads whose own words suggest {group}. They are '
            f'on Heavy Transport now, put there by a form defect that is '
            f'fixed. Fill in "correct vertical" where you are sure and '
            f'leave it blank where you are not — blanks are skipped and '
            f'can be done later. High-confidence rows come first.')
    ws['A1'] = note
    ws['A1'].font = Font(italic=True, size=9, color='6B6762')
    ws['A1'].alignment = Alignment(wrap_text=True, vertical='top')
    ws.merge_cells(start_row=1, start_column=1, end_row=1,
                   end_column=len(COLUMNS))
    ws.row_dimensions[1].height = 46

    head_fill = PatternFill('solid', fgColor='BC1D2F')
    for col, name in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=2, column=col, value=name)
        cell.fill = head_fill
        cell.font = Font(color='FFFFFF', bold=True, size=10)

    for r, row in enumerate(rows, start=3):
        for col, name in enumerate(COLUMNS, start=1):
            ws.cell(row=r, column=col, value=row.get(name, ''))

    # The list of valid values, on its own sheet, referenced by the
    # dropdown. A literal list is capped at 255 characters and would
    # break silently the day somebody adds a vertical.
    lookup = wb.create_sheet('Verticals')
    lookup['A1'] = 'Valid verticals — do not edit'
    for i, v in enumerate(verticals, start=2):
        lookup[f'A{i}'] = v
    lookup.sheet_state = 'hidden'

    answer = get_column_letter(len(COLUMNS))
    # `allow_blank`, not `allowBlank`: openpyxl takes both and the
    # snake_case one wins unconditionally, so passing the camelCase
    # spelling silently produces allowBlank="0" and Excel then
    # refuses an empty cell — which would make "leave it blank if you
    # are not sure" impossible to obey.
    dv = DataValidation(
        type='list',
        formula1=f'=Verticals!$A$2:$A${len(verticals) + 1}',
        allow_blank=True, showDropDown=False)
    dv.showErrorMessage = True
    dv.showInputMessage = True
    dv.error = ('Pick one from the list. A value the CRM does not know '
                'is refused when the sheet is applied.')
    dv.errorTitle = 'Not a vertical'
    dv.prompt = 'Leave blank if you are not sure.'
    dv.promptTitle = 'Correct vertical'
    ws.add_data_validation(dv)
    dv.add(f'{answer}3:{answer}{max(3, len(rows) + 2)}')

    widths = {'lead': 9, 'client': 34, 'lead title': 44, 'owner': 13,
              'confidence': 12, 'evidence': 40, 'correct vertical': 22}
    for col, name in enumerate(COLUMNS, start=1):
        ws.column_dimensions[get_column_letter(col)].width = widths[name]
    ws.freeze_panes = 'A3'
    ws.auto_filter.ref = f'A2:{answer}{len(rows) + 2}'
    wb.save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--from-file', dest='source', default='')
    ap.add_argument('--out-dir', default=os.path.join('docs', 'exports'))
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('MODE:     READ-ONLY — no lead is changed\n')

        rows = from_file(args.source) if args.source else from_database()
        verticals = valid_verticals()
        print(f'  {len(rows)} review rows')
        print(f'  dropdown offers: {", ".join(verticals)}\n')

        groups = defaultdict(list)
        for row in rows:
            groups[row['_group']].append(row)

        os.makedirs(args.out_dir, exist_ok=True)
        stamp = date.today().isoformat().replace('-', '_')
        written = []
        for group, items in sorted(groups.items(),
                                   key=lambda kv: -len(kv[1])):
            items.sort(key=lambda r: (RANK.get(str(r['confidence']).lower(), 3),
                                      str(r['client']).lower()))
            name = f'vertical_review_{slug(group)}_{stamp}.xlsx'
            path = os.path.join(args.out_dir, name)
            write_one(path, group, items, verticals)
            done = sum(1 for r in items if r['correct vertical'])
            conf = Counter(str(r['confidence']).lower() for r in items)
            written.append(name)
            print(f'  {name:<48} {len(items):>4} rows   '
                  f'high {conf.get("high", 0):>3}  '
                  f'medium {conf.get("medium", 0):>3}'
                  + (f'   ({done} already filled in)' if done else ''))

        print(f'\n  {len(written)} file(s) in {args.out_dir}')
        print('\nREAD-ONLY. Nothing was written to any lead.')


if __name__ == '__main__':
    main()
