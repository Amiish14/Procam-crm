"""Likely duplicate leads, for the PFM team to review. READ ONLY.

CRM-WA-01 §2: "A one-off report of current likely duplicates (dry
run, CSV) for the PFM team to review before any merge."

    .venv/bin/python scripts/2026_11_10_lead_duplicates.py --check
    .venv/bin/python scripts/2026_11_10_lead_duplicates.py --check --csv
    .venv/bin/python scripts/2026_11_10_lead_duplicates.py --check \
          --within-days 180 --limit 500

FOUR SIGNALS, FOUR STRENGTHS

A shared GSTIN is one legal entity, exactly. A shared email domain
can be two — `renew.com` carries RENEW POWER and RENEW PHOTOVOLTAICS,
and this CRM has an account called "Mail" created from
`mail.oyaso.com`. So each pair says what matched, and a domain match
on its own does not reach the threshold at all.

Free mail is never a domain match: two unrelated customers who both
used Gmail are not duplicates, and counting them would fire the
warning on half the board.

IT MERGES NOTHING, EVER

There is no `--apply` and no `--merge`. §2 says the PFM team reviews
this before any merge, and a report that acted on its own findings
would make the review a formality. Merging is done on
/admin/lead-duplicates, where whoever decides can see both leads —
and where it can be put back.

THE SURVIVOR IS A SUGGESTION

Most advanced live status, then newest — the same rule as the
thread-duplicate report (QUESTIONS 71), because in this data the
older copy is usually the empty one somebody marked Lost, and
suggesting the oldest would hand somebody a list that discards the
work and keeps the husk.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app, db                                   # noqa: E402

DEFAULT_CSV = os.path.join(_ROOT, 'data', 'lead_duplicates_10NOV.csv')


def report(pairs, show):
    from app.services import lead_duplicates as ld

    print(f'\n{len(pairs)} likely duplicate pair(s).\n')
    if not pairs:
        print('  Nothing on the live board matches on any of the four '
              'signals strongly enough to be worth a person\'s time.')
        return

    by_signal = Counter()
    for pair in pairs:
        for key in pair['matched']:
            by_signal[key] += 1
    print('  pairs by what matched (a pair can match on more than one):')
    for key, _score, _what in ld.SIGNALS:
        if by_signal.get(key):
            print(f'    {by_signal[key]:>5}  {ld.MEANING[key]}')

    print()
    for pair in pairs[:show]:
        keep, drop = pair['keep'], pair['drop']
        print(f'── {pair["score"]}  {pair["why"]}')
        print(f'   keep?  #{keep["lead_id"]:<7} '
              f'{keep["company"][:34]:<34} {keep["stage"][:16]:<16} '
              f'{keep["owner"] or "unowned"}')
        print(f'          {keep["contact"][:28]:<28} '
              f'{keep["email"][:34]:<34} {keep["created"]}')
        print(f'   fold   #{drop["lead_id"]:<7} '
              f'{drop["company"][:34]:<34} {drop["stage"][:16]:<16} '
              f'{drop["owner"] or "unowned"}')
        print(f'          {drop["contact"][:28]:<28} '
              f'{drop["email"][:34]:<34} {drop["created"]}')
    if len(pairs) > show:
        print(f'\n… and {len(pairs) - show} more. --show {len(pairs)} '
              f'for all of them, or --csv for the lot.')

    print('\nNothing is merged by this script. "keep?" is the most '
          'advanced live status, then the newest — a suggestion only. '
          'Merging is done on /admin/lead-duplicates, where whoever '
          'decides can see both leads and can put the merge back.')


def write_csv(pairs, path):
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(['score', 'matched_on', 'why',
                  'keep_lead', 'keep_company', 'keep_contact',
                  'keep_email', 'keep_phone', 'keep_stage',
                  'keep_owner', 'keep_created',
                  'fold_lead', 'fold_company', 'fold_contact',
                  'fold_email', 'fold_phone', 'fold_stage',
                  'fold_owner', 'fold_created'])
    for pair in pairs:
        k, d = pair['keep'], pair['drop']
        out.writerow([pair['score'], '|'.join(pair['matched']),
                      pair['why'],
                      k['lead_id'], k['company'], k['contact'],
                      k['email'], k['phone'], k['stage'], k['owner'],
                      k['created'],
                      d['lead_id'], d['company'], d['contact'],
                      d['email'], d['phone'], d['stage'], d['owner'],
                      d['created']])
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as fh:
        fh.write(buf.getvalue())
    print(f'\nCSV: {path}')
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true', required=True,
                    help='required: this script only ever reads')
    ap.add_argument('--show', type=int, default=25,
                    help='how many pairs to print (default 25)')
    ap.add_argument('--limit', type=int, default=500,
                    help='how many pairs to find (default 500)')
    ap.add_argument('--within-days', dest='within_days', type=int,
                    default=None,
                    help='only leads created in this window')
    ap.add_argument('--csv', nargs='?', const=DEFAULT_CSV, default=None,
                    metavar='PATH',
                    help='also write the full list as a CSV')
    args = ap.parse_args(argv)

    with app.app_context():
        from app.services import lead_dupe_report as rep

        print(f'database: {db.engine.url}')
        print('CRM-WA-01 §2 — likely duplicate leads. REPORT ONLY; '
              'there is no --apply and no --merge.')

        pairs = rep.pairs(limit=args.limit,
                          within_days=args.within_days)
        report(pairs, args.show)
        if args.csv:
            write_csv(pairs, args.csv)

        db.session.rollback()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
