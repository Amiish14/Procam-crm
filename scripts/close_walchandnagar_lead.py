"""
Close the one open Walchandnagar lead, now that the client is blocked.

    .venv/bin/python scripts/close_walchandnagar_lead.py --check
    .venv/bin/python scripts/close_walchandnagar_lead.py --apply

93 leads carry the name. 92 are already closed and are **not
touched** — they are history, and rewriting the reason somebody
recorded at the time would destroy the only account of why each one
ended. One is open, and an open lead for a blocked client is work
the CRM is telling somebody to do and the register says they must
not.

It is marked Lost with the reason Nilesh gave. That exact string is
not one of the ten in `LOSS_REASONS`; the convention already in the
data is an "Others" prefix carrying the detail — `Others – Duplicate
of lead #12115` and so on — and this follows it, because "Other"
alone would lose the one fact worth keeping.

Closed the way the Decide dialog closes a lead: a stage-history row,
an activity, the structured `lost_reason`, `stage_entered_at`, and
the open tasks cancelled with it. An audit row names the block.
"""
import argparse
import os
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import (Lead, LeadActivity, LeadStageHistory,    # noqa: E402
                 STAGES_TERMINAL, app, db)

MATCH = '%walchand%'
REASON = 'Others: client blocked'
ACTOR = 'CRM-UAT-CLOSEOUT-01'
NOTE = ('Closed on 19 October: Walchandnagar is on the client block '
        'register as blocked, so no work may continue on it.')


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

        everything = Lead.query.filter(Lead.company.ilike(MATCH)).all()
        openers = [l for l in everything
                   if (l.stage or '') not in STAGES_TERMINAL]
        print(f'  leads matching {MATCH!r}   {len(everything):>4}')
        print(f'  already closed, untouched  {len(everything) - len(openers):>4}')
        print(f'  open, to close             {len(openers):>4}\n')
        for l in openers:
            print(f'    #{l.id}  {(l.company or "")[:44]:<44} '
                  f'stage={l.stage}  owner={l.assigned_to or "—"}')
            print(f'          -> Lost, reason {REASON!r}')

        if args.check:
            print('\nDRY RUN — nothing written. Re-run with --apply.')
            return
        if not openers:
            print('Nothing open. Nothing to do.')
            return

        from app.services import audit, task_autoclose
        now = datetime.utcnow()
        try:
            for lead in openers:
                was = lead.stage
                db.session.add(LeadStageHistory(
                    lead_id=lead.id, from_stage=was, to_stage='Lost',
                    changed_by=ACTOR, note=NOTE))
                db.session.add(LeadActivity(
                    lead_id=lead.id, kind='stage_change',
                    subject=f'{was or "—"} → Lost', body=NOTE,
                    performed_by=ACTOR))
                lead.stage = 'Lost'
                lead.lost_reason = REASON
                lead.stage_entered_at = now
                lead.updated_at = now
                # The lead is closed, so its open tasks close with it —
                # the same rule the Decide dialog applies.
                cancelled = task_autoclose.close_for_lead(lead, actor=ACTOR)
                audit.record('lead.closed_blocked_client', 'lead',
                             str(lead.id),
                             old={'stage': was, 'lost_reason': None},
                             new={'stage': 'Lost', 'lost_reason': REASON,
                                  'tasks_cancelled': cancelled},
                             actor=ACTOR, reason=NOTE[:200])
                print(f'\n    #{lead.id}: {was} -> Lost, '
                      f'{cancelled} task(s) cancelled with it')
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            raise SystemExit(1)

        print(f'\n  {len(openers)} lead(s) closed, in one transaction. '
              f'The {len(everything) - len(openers)} already-closed '
              f'leads were not touched.')


if __name__ == '__main__':
    main()
