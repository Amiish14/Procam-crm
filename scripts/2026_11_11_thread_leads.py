"""One thread, several leads: separate them, then merge them back.

CRM-MAIL-2026-11-11 3+5. A GE Vernova reminder arrived as
"DSR // EXT: Re: RFQ for Sea Freight - FCA Aussafer, Italy to CFR
Kolkata Sea Port : Project Super Trishuli/TC-SuperTrishul-HYDRO-HIN-
3996" and became a second lead instead of joining the first.

    .venv/bin/python scripts/2026_11_11_thread_leads.py --check
    .venv/bin/python scripts/2026_11_11_thread_leads.py --check \
          --sender jaswanti.patel@gevernova.com
    .venv/bin/python scripts/2026_11_11_thread_leads.py --check --csv
    .venv/bin/python scripts/2026_11_11_thread_leads.py --apply \
          --group 3

WHY THE DUPLICATES EXIST AT ALL

The threading was right and the subject was unreadable. A mail
gateway stamps "EXT:" on anything from outside and a colleague
relaying a status report prefixes "DSR //", both in front of the
"Re:". Every anchored reply test in `lead_intake` therefore failed,
the normalised subject was a different subject, and the thread key
was a different key — so a chase looked like a fresh enquiry.

`lead_intake.strip_lead_tags` fixes that for every message from now
on. This is for the leads already made.

HOW IT GROUPS

By thread key, through `lead_thread.key_for_lead`, which is the key
the intake stores and the duplicate report reads. Each lead's key is
recomputed with the tags stripped, which is what brings the reminder
and the enquiry together: before the fix they keyed apart, and the
report they appear in is the proof the fix works on real data.

A group of one is not reported. A group whose leads are on different
accounts is reported and never merged — that is two customers whose
mail looks alike, and merging them would be the worst outcome here.

THE MERGE IS THE ONE FROM CRM-WA-01

`lead_merge.merge` moves activities, mails, contacts, attachments and
quotes by row id, archives the loser as "merged into #N", and records
every move in the `lead_merges` ledger so it can be put back. This
script chooses nothing on its own: `--apply --group N` merges the one
group you name, into the survivor the report proposes, and prints the
undo line.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import Lead, app, db                               # noqa: E402

ACTOR = 'CRM-MAIL-2026-11-11'
DEFAULT_CSV = os.path.join(_ROOT, 'data', 'thread_leads_11NOV.csv')


def _rule(n=70):
    print('─' * n)


def groups(within_days=120, sender=None):
    """[{key, leads, keep, drop, same_account}] — threads with more
    than one lead."""
    from datetime import datetime, timedelta

    from app.services import lead_thread

    since = datetime.utcnow() - timedelta(days=within_days)
    q = (Lead.query
         .filter(Lead.is_archived.isnot(True),
                 Lead.junk_deleted.isnot(True),
                 Lead.merged_into_id.is_(None),
                 Lead.created_at >= since))
    if sender:
        wanted = f'%{sender.strip().lower()}%'
        q = q.filter(db.or_(
            db.func.lower(db.func.coalesce(Lead.email, '')).like(wanted),
            db.func.lower(db.func.coalesce(
                Lead.original_email_from, '')).like(wanted)))
    buckets = defaultdict(list)
    for lead in q.order_by(Lead.id.asc()).all():
        key = lead_thread.key_for_lead(lead)
        if key:
            buckets[key].append(lead)

    out = []
    for key, leads in buckets.items():
        if len(leads) < 2:
            continue
        keep = _survivor(leads)
        accounts = {l.company_id for l in leads if l.company_id}
        out.append({
            'key': key,
            'leads': sorted(leads, key=_rank, reverse=True),
            'keep': keep,
            'drop': [l for l in leads if l.id != keep.id],
            'same_account': len(accounts) <= 1,
        })
    out.sort(key=lambda g: (-len(g['leads']), g['keep'].id))
    return out


def _rank(lead):
    """The QUESTIONS 71 ladder: most advanced live status, then newest.

    The same rule as the thread-duplicate and the duplicate-pair
    reports, read off `stage_map`, so no two reports disagree about
    which copy of a lead is the real one.
    """
    from datetime import datetime

    from app.services import stage_map

    closed = ('Lost', 'Not Interested', 'On Hold')
    stage = stage_map.canonical(lead.stage)
    if stage in stage_map.JOURNEY:
        rank = 100 + stage_map.JOURNEY.index(stage)
    elif stage in closed:
        rank = closed.index(stage)
    else:
        rank = len(closed)
    return (rank, lead.created_at or datetime.min, lead.id)


def _survivor(leads):
    return max(leads, key=_rank)


def report(found):
    from app.services import lead_thread

    print(f'\n── {len(found)} thread(s) carrying more than one lead ──')
    if not found:
        print('  Nothing. Either the book is clean or the window is '
              'too short — --days widens it.')
        return
    print('  The key is how the CRM recognises a thread: `c:` is '
          'Exchange\'s conversation id (certain), `s:` is the '
          'normalised subject and the sender\'s domain (windowed to '
          '30 days).')
    for i, group in enumerate(found, start=1):
        kind = ('conversation id' if group['key'].startswith('c:')
                else 'subject + domain')
        print(f'\n  [{i}] {len(group["leads"])} leads · {kind}')
        print(f'      {group["key"][:90]}')
        if not group['same_account']:
            print('      !! they are on DIFFERENT accounts. Not merged '
                  'by this script: that is two customers whose mail '
                  'looks alike, and merging them is the worst outcome.')
        for lead in group['leads']:
            marker = 'keep ' if lead.id == group['keep'].id else 'fold '
            print(f'      {marker}#{lead.id:<7} '
                  f'{(lead.company or "")[:26]:<26} '
                  f'{(lead.stage or ""):<18} '
                  f'{str(lead.created_at or "")[:10]}  '
                  f'{(lead.original_email_subject or "")[:46]!r}')
        if group['same_account']:
            print(f'      merge: --apply --group {i}')


def write_csv(found, path):
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(['group', 'thread_key', 'role', 'lead_id', 'company',
                  'stage', 'sender', 'subject', 'created',
                  'account_id', 'same_account'])
    for i, group in enumerate(found, start=1):
        for lead in group['leads']:
            out.writerow([
                i, group['key'],
                'keep' if lead.id == group['keep'].id else 'fold',
                lead.id, (lead.company or '')[:60], lead.stage or '',
                (lead.email or lead.original_email_from or '')[:70],
                (lead.original_email_subject or '')[:90],
                str(lead.created_at or '')[:10],
                lead.company_id or '', 'yes' if group['same_account'] else 'no'])
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as fh:
        fh.write(buf.getvalue())
    print(f'\nCSV: {path}')
    return path


def apply(group):
    """Fold every other lead in this group into the survivor."""
    from app.services import lead_merge

    if not group['same_account']:
        print('\n  !! refused: the leads in that group are on '
              'different accounts.')
        raise SystemExit(2)
    keep = group['keep']
    done = []
    try:
        for lead in group['drop']:
            ok, detail = lead_merge.merge(
                lead, keep, actor=ACTOR,
                reason=('CRM-MAIL-2026-11-11 §3+§5: the same mail '
                        'thread, split by a gateway tag in the '
                        'subject')[:400])
            if not ok:
                print(f'  !! #{lead.id}: {detail}')
                continue
            done.append((lead.id, detail))
        db.session.commit()
    except Exception as exc:                              # noqa: BLE001
        db.session.rollback()
        print(f'\n  !! ROLLED BACK: {type(exc).__name__}: {str(exc)[:200]}')
        raise SystemExit(1)
    print(f'\n{len(done)} lead(s) folded into #{keep.id}.')
    for lead_id, detail in done:
        counted = (detail or {}).get('moved') or {}
        moved = sum(v for v in counted.values()
                    if isinstance(v, int)) if isinstance(counted, dict) else 0
        print(f'  #{lead_id} → #{keep.id}, {moved} row(s) moved, '
              f'reversible: {(detail or {}).get("reversible")}')
    print('Put one back on Admin → Duplicate leads, which reads the '
          'same ledger.')
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='report only. The default.')
    ap.add_argument('--apply', action='store_true',
                    help='merge one group, named with --group')
    ap.add_argument('--group', type=int, default=None, metavar='N')
    ap.add_argument('--days', type=int, default=120)
    ap.add_argument('--sender', default=None, metavar='ADDRESS')
    ap.add_argument('--csv', nargs='?', const=DEFAULT_CSV, default=None,
                    metavar='PATH')
    args = ap.parse_args(argv)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-MAIL-2026-11-11 §3+§5 — one thread, several leads.'
              + ('' if args.apply else ' REPORT ONLY.'))
        _rule()
        found = groups(within_days=args.days, sender=args.sender)
        report(found)
        if args.csv:
            write_csv(found, args.csv)

        if not args.apply:
            print('\nDRY RUN — nothing written. --apply --group N '
                  'merges one group.')
            db.session.rollback()
            return 0

        if not args.group or not (1 <= args.group <= len(found)):
            print('\n--apply needs --group N, one of the numbers above.')
            return 2
        apply(found[args.group - 1])
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
