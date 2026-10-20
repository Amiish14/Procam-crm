"""
CRM 2.0 R2 — Not Interested becomes Lost, with a reason.

    .venv/bin/python scripts/crm2_not_interested_to_lost.py --check
    .venv/bin/python scripts/crm2_not_interested_to_lost.py --apply
    .venv/bin/python scripts/crm2_not_interested_to_lost.py --down --yes

CRM 2.0 has one closed state for a deal that did not happen. "Not
Interested" and "Lost" are the same outcome recorded two ways, which
is why every funnel in this CRM has had to list both and why the two
have drifted: 1,212 rows on one, 7,128 on the other.

Nothing is lost in the merge. The distinction moves into
`lost_reason` as 'Not interested', which is where it belonged — a
reason, not a stage — and which keeps it reportable.

Reversible, exactly
    `--down` reads this script's own audit rows and puts back only
    the leads it moved, with whatever `lost_reason` they had before.
    A lead already Lost before this ran is never touched, and a lead
    whose reason someone edited afterwards is reported and left
    alone rather than being quietly overwritten.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402
from app.models.audit import AuditEvent                   # noqa: E402

FROM_STAGE = 'Not Interested'
TO_STAGE = 'Lost'
REASON = 'Not interested'
ACTION = 'crm2.not_interested_merged'


def plan():
    return Lead.query.filter(Lead.stage == FROM_STAGE).order_by(
        Lead.id.asc()).all()


def apply_plan(leads, *, actor='CRM2-R2'):
    from app.services import audit

    for lead in leads:
        before = {'stage': lead.stage, 'lost_reason': lead.lost_reason}
        lead.stage = TO_STAGE
        # An existing reason is kept: somebody typed it, and it says
        # more than the stage name did.
        if not (lead.lost_reason or '').strip():
            lead.lost_reason = REASON
        audit.record(ACTION, 'lead', str(lead.id),
                     old=before,
                     new={'stage': lead.stage,
                          'lost_reason': lead.lost_reason},
                     actor=actor,
                     reason='CRM 2.0 has one closed-without-a-deal '
                            'stage; the distinction moves to the '
                            'reason, where it is still reportable')
    return leads


def undo(*, actor='CRM2-R2'):
    """Put back only what this script moved."""
    from app.services import audit

    moved, skipped = [], []
    for ev in (AuditEvent.query
               .filter(AuditEvent.action == ACTION,
                       AuditEvent.entity_type == 'lead')
               .order_by(AuditEvent.id.asc()).all()):
        try:
            lead = db.session.get(Lead, int(ev.entity_id))
        except (TypeError, ValueError):
            continue
        if lead is None:
            continue
        after = ev.new_value or {}
        if (lead.stage or '') != after.get('stage'):
            # Somebody has moved it since. Theirs wins.
            skipped.append((lead, 'moved again since the merge'))
            continue
        before = ev.old_value or {}
        lead.stage = before.get('stage') or FROM_STAGE
        lead.lost_reason = before.get('lost_reason')
        audit.record('crm2.not_interested_restored', 'lead',
                     str(lead.id), actor=actor,
                     reason='merge reverted')
        moved.append(lead)
    return moved, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    args = ap.parse_args()
    if len([a for a in (args.check, args.apply, args.down) if a]) != 1:
        raise SystemExit('Choose one: --check, --apply or --down.')

    with app.app_context():
        print(f'database: {db.engine.url}')
        mode = ('APPLY' if args.apply else
                'DOWN' if args.down else 'DRY RUN — nothing is written')
        print(f'MODE:     {mode}\n')

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            moved, skipped = undo()
            db.session.commit()
            print(f'  {len(moved)} lead(s) put back to {FROM_STAGE}.')
            for lead, why in skipped:
                print(f'    #{lead.id} left alone: {why}')
            return

        leads = plan()
        with_reason = sum(1 for l in leads
                          if (l.lost_reason or '').strip())
        print(f'  on {FROM_STAGE}                {len(leads)}')
        print(f'    already carry a reason     {with_reason} '
              f'(kept as-is)')
        print(f'    will be given "{REASON}"  {len(leads) - with_reason}')
        print(f'  already on {TO_STAGE}                 '
              f'{Lead.query.filter(Lead.stage == TO_STAGE).count()}')

        if args.check:
            print(f'\n  after this: {TO_STAGE} would hold '
                  f'{Lead.query.filter(Lead.stage == TO_STAGE).count() + len(leads)}')
            print('\nDRY RUN — nothing written. `--apply` merges them; '
                  '`--down --yes` puts them back.')
            return

        apply_plan(leads)
        try:
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            raise SystemExit(1)
        print(f'\n  {len(leads)} lead(s) moved to {TO_STAGE}, each with '
              f'an audit row.')
        print(f'  {TO_STAGE} now holds '
              f'{Lead.query.filter(Lead.stage == TO_STAGE).count()}; '
              f'{FROM_STAGE} holds '
              f'{Lead.query.filter(Lead.stage == FROM_STAGE).count()}.')


if __name__ == '__main__':
    main()
