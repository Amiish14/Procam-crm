"""
CRM-JUNK-DELETE-01 §10 — the one-time clean-up.

    .venv/bin/python scripts/junk_cleanup.py                  # dry run
    .venv/bin/python scripts/junk_cleanup.py --sender a@b.com
    .venv/bin/python scripts/junk_cleanup.py --mail
    .venv/bin/python scripts/junk_cleanup.py --apply --ids 1,2,3 \
        --reason internal --actor DIR12010

Nothing is applied without `--ids`. That is the point of §10: the
dry run produces a list, the list goes to the person who asked for
this, and only the rows that come back approved are marked — by
number, not by a rule that re-reads the mailbox and decides again.

The last clean-up that ran from a rule took 967 leads.
"""
import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402
from app.services import junk_delete as jd                # noqa: E402
from app.services import junk_suggest as js               # noqa: E402

REMARK = 'Junk clean-up'


def candidates(*, senders=()):
    """Open New Opportunity leads that look like they were never
    enquiries. Eligible ones only — a lead somebody has worked is
    not a candidate however junk its sender looks."""
    rows = (Lead.query
            .filter(Lead.stage == jd.ELIGIBLE_STAGE,
                    Lead.junk_deleted.isnot(True),
                    Lead.is_archived.isnot(True))
            .order_by(Lead.id.asc()).all())
    staff = js._staff_addresses()
    wanted = {s.strip().lower() for s in senders if s.strip()}
    out = []
    for lead in rows:
        sender = jd.sender_of(lead)
        if wanted:
            if sender not in wanted:
                continue
            why = ['named on the command line']
        else:
            why = js.reasons(lead, staff=staff)
            if not why:
                continue
        blockers = jd.blockers(lead)
        out.append({'lead': lead, 'sender': sender, 'why': why,
                    'blockers': blockers})
    return out


def report(rows):
    per_sender = Counter(r['sender'] or '(no sender)' for r in rows)
    eligible = [r for r in rows if not r['blockers']]
    lines = []
    lines.append(f'{len(rows)} lead(s) look like they were never '
                 f'enquiries; {len(eligible)} of them can be deleted '
                 f'(the rest have activity).')
    lines.append('')
    lines.append('Per sender:')
    for sender, n in per_sender.most_common():
        lines.append(f'  {n:>4}  {sender}')
    lines.append('')
    lines.append('The ones that can go:')
    for r in eligible:
        lead = r['lead']
        lines.append(f'  #{lead.id:<7} {(lead.company or "")[:34]:<34} '
                     f'{r["sender"][:38]:<38} {"; ".join(r["why"])[:60]}')
    skipped = [r for r in rows if r['blockers']]
    if skipped:
        lines.append('')
        lines.append('Left alone — somebody worked these:')
        for r in skipped:
            lead = r['lead']
            lines.append(f'  #{lead.id:<7} {(lead.company or "")[:34]:<34} '
                         f'{r["blockers"][0][1]}')
    return '\n'.join(lines)


def _block_live():
    """Is the block list actually in force?

    `system_senders.enabled()` reads FEATURE_SENDER_BLOCK, and the
    ingest only consults the list when it is on. Adding an address
    while the flag is off writes a row that does nothing, which is
    the kind of silence this codebase has been bitten by before.
    """
    from app.services import system_senders
    return system_senders.enabled()


def mail(body, *, to=None):
    from app.services import outbox

    to = to or jd.admin_address()
    html = ('<p>Junk clean-up, for approval. Nothing has been '
            'deleted.</p><pre style="font-size:12px">'
            + body.replace('&', '&amp;').replace('<', '&lt;')
            + '</pre><p>Reply with the lead numbers to delete.</p>')
    outbox.enqueue(to=to, subject='Junk clean-up — for approval',
                   html=html, dedupe_key=None,
                   event_key='lead.junk_cleanup', commit=False)
    db.session.commit()
    return to


def apply(ids, *, reason, note, actor, day, block=False):
    """Mark the approved numbers. Nothing else is touched.

    `block` adds each lead's sender to the Master Data block list —
    the address, never the domain, and never one of ours. It is off
    unless asked for, because deleting twelve messages and silencing
    an address are two decisions and only one of them is reversible
    by clicking Restore.
    """
    done, refused, blocked = [], [], {}
    for lid in ids:
        lead = Lead.query.get(lid)
        if lead is None:
            refused.append((lid, 'no such lead'))
            continue
        try:
            out = jd.mark_not_relevant(
                lead, reason=reason, note=note or f'{REMARK} {day}',
                actor=actor, block_sender=block, commit=False)
            done.append(lid)
            if out['sender_blocked']:
                blocked[out['sender']] = 'blocked'
            elif out['block_refused']:
                blocked[out['sender']] = 'refused — a Procam address'
        except ValueError as exc:
            refused.append((lid, str(exc)))
    db.session.commit()
    return done, refused, blocked


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--sender', action='append', default=[],
                    help='limit to this sender; repeatable')
    ap.add_argument('--mail', action='store_true',
                    help='queue the list for approval')
    ap.add_argument('--to', default=None)
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--ids', default='',
                    help='the approved lead numbers, comma separated')
    ap.add_argument('--reason', default='spam', choices=jd.CODES)
    ap.add_argument('--note', default='')
    ap.add_argument('--actor', default='')
    ap.add_argument('--block', action='store_true',
                    help='also add each sender to the block list')
    ap.add_argument('--day', default='26-Oct-2026',
                    help='the date in the audit remark')
    args = ap.parse_args()

    with app.app_context():
        print(f'database: {db.engine.url}')
        rows = candidates(senders=args.sender)
        text = report(rows)
        print()
        print(text)

        if args.mail:
            where = mail(text, to=args.to)
            print(f'\n  Queued for {where}. Nothing deleted.')

        if not args.apply:
            print('\nDRY RUN — nothing written. To apply, send the list '
                  'for approval and come back with --apply --ids.')
            return

        ids = [int(x) for x in args.ids.replace(' ', '').split(',')
               if x.strip().isdigit()]
        if not ids:
            raise SystemExit(
                '--apply needs --ids: the numbers that came back '
                'approved. A clean-up that re-decides for itself is '
                'the one that took 967 leads.')
        if not args.actor:
            raise SystemExit('--apply needs --actor (an emp_code): '
                             'every delete is signed.')
        done, refused, blocked = apply(
            ids, reason=args.reason, note=args.note, actor=args.actor,
            day=args.day, block=args.block)
        print(f'\n  {len(done)} deleted: '
              f'{", ".join("#" + str(i) for i in done) or "none"}')
        for lid, why in refused:
            print(f'  !! #{lid}: {why}')
        for sender, what in sorted(blocked.items()):
            print(f'  sender {sender}: {what}')
        if blocked and not _block_live():
            print('\n  !! The block list is NOT in force: '
                  'FEATURE_SENDER_BLOCK is off, so nothing is held '
                  'and the next message from that address becomes a '
                  'lead exactly as before. The address is on the '
                  'list and will take effect the moment the flag '
                  'is switched on.')
        print('\n  Every one is in Admin → Deleted leads and can '
              'be restored.')


if __name__ == '__main__':
    main()
