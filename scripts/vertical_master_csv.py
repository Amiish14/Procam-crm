"""
The vertical review master, as one CSV.

FINISH-TRACKER-CRM §2. TKT-0078 asked for the 296-lead master file.
`scripts/vertical_review_split.py` already turns that population into
five workbooks, one per suggested vertical, with a dropdown — good for
somebody filling it in, no use at all to anybody who wants to read the
whole list, diff it next week, or load it somewhere else.

This writes the same population as one CSV: every lead still awaiting
a vertical decision, with the evidence and the suggestion, in lead
order. It is a report, not a decision — nothing is written to any
lead and no file is produced unless asked.

Read-only by default. It prints the counts and the first rows:

    .venv/bin/python scripts/vertical_master_csv.py

--write saves it under docs/reports/, named for the day, so two runs
a week apart leave two files and the difference between them is the
work somebody did:

    .venv/bin/python scripts/vertical_master_csv.py --write

The population comes from the database, so leads reclassified since
the last run drop out of their own accord. That is why the count is
no longer exactly 296.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import Counter
from datetime import date

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

#: Where the file lands. Under docs so it travels with the repo and
#: can be diffed; the reader is a person, not a program.
REPORT_DIR = os.path.join('docs', 'reports')

#: The reader needs the lead, who holds it, what the words suggest,
#: and how sure that is. Anything else is noise in a review.
COLUMNS = ('lead_id', 'company', 'title', 'owner', 'current_vertical',
           'looks_like', 'confidence', 'evidence', 'correct_vertical')

SINCE = '2026-09-01'


def rows(since=SINCE):
    """Every lead still awaiting a vertical decision."""
    from app.services import vertical_audit as va

    out = []
    for row in va.survey(since=since)['review']:
        out.append({
            'lead_id': row.get('lead_id'),
            'company': row.get('company') or '',
            'title': row.get('title') or '',
            'owner': row.get('pic') or '',
            'current_vertical': row.get('now') or '',
            'looks_like': row.get('looks_like') or '',
            'confidence': row.get('confidence') or '',
            'evidence': row.get('evidence') or '',
            # Left blank on purpose: this column is the answer, and
            # the file exists to collect it.
            'correct_vertical': '',
        })
    out.sort(key=lambda r: (r['lead_id'] or 0))
    return out


def counts(found):
    """(total, per suggestion, per confidence)."""
    return (len(found),
            Counter(r['looks_like'] or '(none)' for r in found),
            Counter(r['confidence'] or '(none)' for r in found))


def filename(on=None):
    on = on or date.today()
    return os.path.join(REPORT_DIR,
                        f'vertical_master_{on:%Y_%m_%d}.csv')


def write(found, path=None, root=_ROOT):
    """Write the CSV. Returns the path written."""
    path = path or filename()
    full = path if os.path.isabs(path) else os.path.join(root, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS))
        writer.writeheader()
        for row in found:
            writer.writerow({k: row.get(k, '') for k in COLUMNS})
    return full


def report(found, *, path=None):
    total, by_suggestion, by_confidence = counts(found)
    if not total:
        return ('Nothing awaiting a vertical decision. The review '
                'list is empty.')
    out = [f'{total} lead(s) awaiting a vertical decision.', '']
    out.append('By suggestion:')
    for name, n in by_suggestion.most_common():
        out.append(f'  {name:<22} {n:>4}')
    out.append('')
    out.append('By confidence:')
    for name in ('high', 'medium', 'low', '(none)'):
        if by_confidence.get(name):
            out.append(f'  {name:<22} {by_confidence[name]:>4}')
    out.append('')
    out.append('First rows:')
    for row in found[:5]:
        out.append(f'  #{row["lead_id"]}  {row["company"] or "(blank)"}'
                   f'  ·  {row["looks_like"] or "(none)"}'
                   f'  ·  {row["confidence"] or "(none)"}')
    out.append('')
    if path:
        out.append(f'Written: {path}')
    else:
        out.append(f'Nothing was written. --write saves it as '
                   f'{filename()}')
    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true',
                    help='save the CSV under docs/reports/')
    ap.add_argument('--since', default=SINCE,
                    help='the survey window start')
    ap.add_argument('--out', help='write somewhere else')
    args = ap.parse_args()

    import app as main_app
    with main_app.app.app_context():
        found = rows(args.since)
        path = None
        if args.write and found:
            path = write(found, args.out)
        print(report(found, path=path))
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
