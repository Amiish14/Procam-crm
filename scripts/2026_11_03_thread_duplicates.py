"""Leads that are the same mail thread. DRY RUN BY DEFAULT. NOTHING MERGES.

CRM-USER-ISSUES-02NOV §2:

    "Give me a --dry-run script listing existing duplicate pairs
     created this way. Don't merge them; I decide."

    .venv/bin/python scripts/2026_11_03_thread_duplicates.py
    .venv/bin/python scripts/2026_11_03_thread_duplicates.py --show 200
    .venv/bin/python scripts/2026_11_03_thread_duplicates.py --csv
    .venv/bin/python scripts/2026_11_03_thread_duplicates.py --backfill \
          --apply

TWO JOBS, AND ONLY ONE OF THEM WRITES

    (default)     report the pairs. Reads. Writes nothing, ever.
    --backfill    fill `leads.thread_key` where it is NULL, which is
                  everywhere, because no code path has ever written it.
                  Still a dry run without --apply.

There is no --merge and there will not be one. §2 says "I decide", and
a merge is `lead_merge`'s job from the screen, where the person doing
it can see both leads.

WHY THE BACKFILL IS NEEDED AT ALL

`leads.thread_key` has been a column since
`scripts/2026_10_30_lead_flow.py` and nothing has ever filled it. The
intake fills it from now on, but that only groups threads that arrive
from now on. Every duplicate already on file has a NULL key, so the
report computes the key in Python from the columns the intake did
store — `conversation_id`, `original_email_subject`,
`original_email_from` — and the backfill writes that same value.

The report does not need the backfill to have run. They are separate
so that the listing §2 asks for can be read and argued with before
anything at all is written.

WHAT COUNTS AS A PAIR

Two or more live leads sharing a thread key. "Live" excludes archived,
junk-deleted and already-merged leads: a lead somebody has already
dealt with is not a duplicate waiting to be found.

A conversation-id key is reported at any age. A subject-and-domain key
is only reported inside `lead_merge.WINDOW_DAYS` (30), because "RFQ"
from a regular customer is the same key in March and in November and
those are two different enquiries. The two kinds are counted
separately in the summary, because they are not equally certain and a
reader deciding what to merge needs to know which is which.

WHICH ONE IT SUGGESTS KEEPING

It used to say keep the oldest, which in this data is almost always
the wrong one: the pairs on file are an empty first copy somebody
marked Lost and a second copy the owner actually worked. So the
suggestion is now the one that got furthest down the journey, and
where two sit on the same stage, the newer. It is still only a
suggestion, printed next to both leads, and nothing in this script
acts on it.

WHAT IT DELIBERATELY DOES NOT DO

It does not guess at pairs with no key at all. A lead with no
conversation id, no subject and no sender address has nothing to group
on, and inventing a grouping would hand somebody a merge list built
out of nothing. Those are counted and named as unkeyable.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import defaultdict
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                              # noqa: E402

from app.services import lead_merge, lead_thread           # noqa: E402
from app.services import stage_map                         # noqa: E402

#: Where the CSV goes when --csv is passed with no path.
DEFAULT_CSV = os.path.join(_HERE, '..', 'data',
                           'thread_duplicates_03NOV.csv')


#: Closed states, least worth keeping first. `stage_map.TERMINAL`
#: names four and one of them — Won — is the far end of the journey
#: rather than a dead end, so it is ranked with the live stages and
#: not here. On Hold can come back and Lost will not, which is the
#: order. A test asserts this covers TERMINAL, so a new closed state
#: has to be placed deliberately rather than falling somewhere by
#: accident.
CLOSED_ORDER = ('Lost', 'Not Interested', 'On Hold')

#: For leads with no created_at, so the tie-break can sort them.
_NO_DATE = datetime.min


def advancement(lead):
    """How far down the journey this lead got. Bigger is further.

    Read off `stage_map.JOURNEY` rather than written out again here,
    so it cannot drift from the order the funnel, the board and the
    reports all use. Won is the end of that journey and so ranks
    highest: a thread that produced a win keeps the win.
    """
    stage = stage_map.canonical(lead.stage)
    if stage in stage_map.JOURNEY:
        return 100 + stage_map.JOURNEY.index(stage)
    if stage in CLOSED_ORDER:
        return CLOSED_ORDER.index(stage)
    # A stage name nothing recognises. Above the closed states and
    # below every live one: it is probably something live, and letting
    # an unknown word outrank a real Under Negotiation would be worse
    # than this.
    return len(CLOSED_ORDER)


def rank(lead):
    """Sort key for "which of these would you keep". Highest wins.

    Most advanced, then newest. Newest and not oldest for the same
    reason as the whole change: where two copies sit on the same
    stage, the later one is the one carrying the reply that created
    it, and the earlier one is usually the husk.
    """
    return (advancement(lead), lead.created_at or _NO_DATE, lead.id)


def _live():
    """Leads worth grouping: not archived, junked or merged away."""
    return (Lead.query
            .filter(Lead.is_archived.isnot(True),
                    Lead.junk_deleted.isnot(True),
                    Lead.merged_into_id.is_(None))
            .order_by(Lead.id.asc()))


def survey():
    """{key: [lead, ...]} for every live lead, plus the unkeyable."""
    groups = defaultdict(list)
    unkeyable = []
    for lead in _live().all():
        key = lead_thread.key_for_lead(lead)
        if not key:
            unkeyable.append(lead)
            continue
        groups[key].append(lead)
    return groups, unkeyable


def _within_window(leads):
    """Whether a windowed group is close enough together to count.

    The window is between the earliest and the latest of the group,
    not against today: two leads from one thread in March are a
    duplicate pair in March, and still one in November.
    """
    dates = [l.created_at for l in leads if l.created_at]
    if len(dates) < 2:
        return True
    span = max(dates) - min(dates)
    return span.days <= lead_merge.WINDOW_DAYS


def pairs(groups):
    """The groups that are duplicates, with why each one qualified."""
    out = []
    for key, leads in groups.items():
        if len(leads) < 2:
            continue
        windowed = lead_thread.is_windowed(key)
        if windowed and not _within_window(leads):
            continue
        out.append({
            'key': key,
            'kind': ('subject + sender domain' if windowed
                     else 'conversation id'),
            'certain': not windowed,
            # Best first, so the suggestion is the line a reader's eye
            # lands on and the ones below it are the candidates to
            # close.
            'leads': sorted(leads, key=rank, reverse=True),
            'keep': max(leads, key=rank),
        })
    # The certain ones first, then the biggest groups: that is the
    # order somebody working through the list would choose.
    out.sort(key=lambda g: (not g['certain'], -len(g['leads'])))
    return out


def _row(lead):
    return {
        'lead': lead.id,
        'company': (lead.company or '')[:40],
        'stage': lead.stage or '',
        'owner': lead.assigned_to or '',
        'created': str(lead.created_at or '')[:10],
        'sender': (lead.original_email_from or lead.email or '')[:40],
        'subject': (lead.original_email_subject or '')[:50],
    }


def report(found, unkeyable, show):
    certain = [g for g in found if g['certain']]
    likely = [g for g in found if not g['certain']]
    extra = sum(len(g['leads']) - 1 for g in found)

    print(f'\n{len(found)} thread(s) hold more than one lead — '
          f'{extra} lead(s) more than there are threads.')
    print(f'  {len(certain)} on the conversation id (certain)')
    print(f'  {len(likely)} on subject + sender domain, within '
          f'{lead_merge.WINDOW_DAYS} days (likely)')
    if unkeyable:
        print(f'  {len(unkeyable)} lead(s) could not be keyed at all — '
              f'no conversation id, subject or sender. Not grouped, '
              f'because a grouping built out of nothing is worse than '
              f'none.')

    for group in found[:show]:
        mark = '!!' if group['certain'] else '  '
        print(f'\n{mark} {group["kind"]}  ·  {len(group["leads"])} leads')
        print(f'   key: {group["key"][:90]}')
        for lead in group['leads']:
            r = _row(lead)
            keep = '(keep?)' if lead is group['keep'] else '       '
            print(f'   {keep} #{r["lead"]:<7} {r["created"]}  '
                  f'{r["stage"]:<16} {r["owner"]:<10} '
                  f'{r["company"]:<40} {r["sender"]}')
            print(f'           {r["subject"]}')

    if len(found) > show:
        print(f'\n… and {len(found) - show} more. --show {len(found)} '
              f'for all of them.')
    print('\nNothing is merged by this script. "(keep?)" marks the '
          'lead that got furthest down the journey — and where two '
          'are level, the newer one. It is a suggestion only: merging '
          'is done on /admin/account-duplicates, where whoever decides '
          'can see both leads.')
    return certain, likely


def write_csv(found, path):
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(['thread_key', 'match', 'certain', 'leads_in_thread',
                  'lead', 'suggested_keep', 'company', 'stage', 'owner',
                  'created', 'sender', 'subject'])
    for group in found:
        for lead in group['leads']:
            r = _row(lead)
            out.writerow([group['key'], group['kind'],
                          'yes' if group['certain'] else 'no',
                          len(group['leads']), r['lead'],
                          'yes' if lead is group['keep'] else 'no',
                          r['company'], r['stage'], r['owner'],
                          r['created'], r['sender'], r['subject']])
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as fh:
        fh.write(buf.getvalue())
    print(f'\nCSV: {path}')
    return path


def backfill(groups, unkeyable, *, apply):
    """Fill thread_key where it is NULL. Never overwrites one."""
    filled = 0
    already = 0
    for key, leads in groups.items():
        for lead in leads:
            if (lead.thread_key or '').strip():
                already += 1
                continue
            if apply:
                lead.thread_key = key[:200]
            filled += 1
    print(f'\nbackfill: {filled} lead(s) would take a thread key'
          if not apply else
          f'\nbackfill: {filled} lead(s) given a thread key')
    if already:
        print(f'          {already} already had one — left alone')
    if unkeyable:
        print(f'          {len(unkeyable)} cannot be keyed and stay NULL')
    if apply:
        db.session.commit()
        print('          committed.')
    else:
        db.session.rollback()
        print('          DRY RUN — nothing written. --apply to write.')
    return filled


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--show', type=int, default=25,
                    help='how many threads to print (default 25)')
    ap.add_argument('--csv', nargs='?', const=DEFAULT_CSV, default=None,
                    metavar='PATH',
                    help='also write the full list as a CSV')
    ap.add_argument('--backfill', action='store_true',
                    help='fill leads.thread_key where it is NULL')
    ap.add_argument('--apply', action='store_true',
                    help='with --backfill, actually write. Default is '
                         'a dry run.')
    args = ap.parse_args()

    if args.apply and not args.backfill:
        ap.error('--apply only means something with --backfill. The '
                 'report never writes.')

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-USER-ISSUES-02NOV §2 — leads that are the same mail '
              'thread.' + ('' if args.backfill else ' REPORT ONLY.'))

        groups, unkeyable = survey()
        found = pairs(groups)
        report(found, unkeyable, args.show)

        if args.csv:
            write_csv(found, args.csv)

        if args.backfill:
            backfill(groups, unkeyable, apply=args.apply)
        else:
            # The report read rows and nothing else; make that true of
            # the session too.
            db.session.rollback()


if __name__ == '__main__':
    main()
