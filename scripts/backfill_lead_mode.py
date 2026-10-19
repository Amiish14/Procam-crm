"""
Fill `leads.mode` from what the vertical used to say.

    .venv/bin/python scripts/backfill_lead_mode.py --check
    .venv/bin/python scripts/backfill_lead_mode.py --apply

Until 18 October a forwarding job was filed as Sea Freight or Air
Freight, because those were verticals. CRM-VERTICAL-MASTER-01 folded
both into Freight Forwarding and the audit trail recorded every one of
those moves — old value and new value, per lead. That trail is the only
place the sea/air answer still exists, so this reads it back.

What it writes, and nothing else:

  * the lead is Freight Forwarding **now** — a lead that has since
    moved on to Heavy Transport is not a forwarding job and does not
    get a mode;
  * its `mode` is empty — a value somebody has typed since is theirs,
    not this script's to overwrite;
  * its most recent vertical change away from Sea Freight or Air
    Freight says which. Most recent, because a lead that went
    Sea → Air → Freight Forwarding was last an air job, and the
    earlier row is history rather than an answer.

Everything else is left blank. A blank mode means nobody has said —
it does not mean the job is neither, and guessing one from the lead
title or the account would turn a gap into a wrong answer that looks
like a right one.
"""
import argparse
import os
import sys
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402
from app.models.audit import AuditEvent                   # noqa: E402
from app.services import lead_mode                        # noqa: E402

#: The verb the vertical rewrite was recorded under.
ACTIONS = ('lead.update', 'lead.vertical_change')


def _evidence():
    """{lead_id: (mode, when, actor)} from the most recent move away
    from one of the old verticals."""
    rows = (AuditEvent.query
            .filter(AuditEvent.entity_type == 'lead',
                    AuditEvent.action.in_(ACTIONS))
            .order_by(AuditEvent.occurred_at.asc()).all())
    found = {}
    for ev in rows:
        old = (ev.old_value or {})
        new = (ev.new_value or {})
        if not isinstance(old, dict) or not isinstance(new, dict):
            continue
        if 'procam_vertical' not in old or 'procam_vertical' not in new:
            continue
        was, now = old['procam_vertical'], new['procam_vertical']
        if was == now:
            continue
        mode = lead_mode.from_vertical(was)
        if not mode:
            continue
        try:
            lid = int(ev.entity_id)
        except (TypeError, ValueError):
            continue
        # Ascending order, so a later row simply replaces an earlier
        # one — that is the "most recent move" rule.
        found[lid] = (mode, ev.occurred_at, ev.actor, was, now)
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    if args.apply == args.check:
        raise SystemExit('Choose one: --check or --apply.')

    with app.app_context():
        print(f'database: {db.engine.url}')
        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN — nothing is written"}\n')

        ev = _evidence()
        print(f'audit rows naming an old Sea/Air vertical: {len(ev)} lead(s)')
        if not ev:
            print('\nNothing to go on. No lead is given a mode.')
            return

        leads = {l.id: l for l in Lead.query.filter(Lead.id.in_(ev)).all()}
        write, skip = [], defaultdict(list)
        for lid, (mode, when, actor, was, now) in sorted(ev.items()):
            lead = leads.get(lid)
            if lead is None:
                skip['the lead no longer exists'].append(lid)
                continue
            if not lead_mode.applies_to(lead.procam_vertical):
                skip[f'now {lead.procam_vertical or "(blank)"}, '
                     f'not {lead_mode.VERTICAL}'].append(lid)
                continue
            if (lead.mode or '').strip():
                skip[f'already set to {lead.mode}'].append(lid)
                continue
            write.append((lead, mode, was, when, actor))

        print(f'  to fill                 {len(write):>5}')
        print(f'  left alone              {sum(len(v) for v in skip.values()):>5}')
        for why, ids in sorted(skip.items(), key=lambda kv: -len(kv[1])):
            print(f'      {len(ids):>5}  {why}')

        if write:
            print('\n  by mode:')
            for mode, n in Counter(m for _l, m, _w, _d, _a in write).most_common():
                print(f'      {n:>5}  {mode}')
            print('\n  the first 15, with the evidence:')
            for lead, mode, was, when, actor in write[:15]:
                print(f'      #{lead.id:<6} {mode:<11} was {was:<14} '
                      f'{str(when)[:16]}  by {actor}')

        if args.check:
            print('\nDRY RUN — nothing written. Re-run with --apply.')
            return

        try:
            for lead, mode, _was, _when, _actor in write:
                lead.mode = mode
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK: {type(exc).__name__}: {str(exc)[:200]}')
            raise SystemExit(1)

        print(f'\n  {len(write)} lead(s) given a mode, in one transaction. '
              f'Every one is audited as a lead.update, so `--check` after '
              f'this reports them as already set rather than filling them '
              f'twice.')


if __name__ == '__main__':
    main()
