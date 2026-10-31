"""
CRM 2.0 R2 — when did this lead reach the stage it is on?

    .venv/bin/python scripts/crm2_backfill_stage_entered.py --check
    .venv/bin/python scripts/crm2_backfill_stage_entered.py --apply
    .venv/bin/python scripts/crm2_backfill_stage_entered.py --down --yes

`leads.stage_entered_at` was added after the fact and is null on most
rows, so the stage map cannot say how long anything has been stuck —
which is the only part of a stage map anybody reads.

Where the date comes from
    The newest `lead.stage_change` audit row whose `new_value.stage`
    equals the stage the lead is on now. That is the moment it
    arrived where it is. 4,222 such rows exist.

Why not `updated_at`
    Because it is wrong, and wrong in a way that looks right. A bulk
    edit touched 2,381 leads on a single day in the last 90, so
    `updated_at` would report most of the book as freshly moved and
    the whole map would read green. A lead whose audit trail does not
    reach back far enough is left null on purpose: the map draws it
    grey and says the date is unknown, which is true, rather than
    drawing it green, which is a guess that would be acted on.

Reversible
    `--down` nulls only the rows this script set, which it knows
    because each one carries an audit row naming it. A lead that
    already had a date keeps it.
"""
import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402
from app.models.audit import AuditEvent                   # noqa: E402

ACTION = 'crm2.stage_entered_backfilled'
SOURCE_ACTIONS = ('lead.stage_change', 'lead.stage_changed')


def _stage_of(payload):
    if isinstance(payload, dict):
        value = payload.get('stage')
        if isinstance(value, str):
            return value.strip()
    return ''


def plan():
    """[(lead, when, audit_id)] for every lead we can date."""
    want = {l.id: l for l in Lead.query.filter(
        Lead.stage_entered_at.is_(None)).all()}
    if not want:
        return [], Counter()

    # One pass, newest first: the first row that matches a lead's
    # current stage is the moment it arrived there.
    seen, out = set(), []
    rows = (AuditEvent.query
            .filter(AuditEvent.entity_type == 'lead',
                    AuditEvent.action.in_(SOURCE_ACTIONS))
            .order_by(AuditEvent.id.desc()).all())
    for ev in rows:
        try:
            lid = int(ev.entity_id)
        except (TypeError, ValueError):
            continue
        if lid in seen:
            continue
        lead = want.get(lid)
        if lead is None:
            continue
        if _stage_of(ev.new_value) != (lead.stage or '').strip():
            # The newest change for this lead moved it somewhere it
            # no longer is — the stage was changed again without an
            # audit row, so we cannot date it. Skip rather than take
            # an older row that would be the wrong arrival.
            seen.add(lid)
            continue
        seen.add(lid)
        out.append((lead, ev.occurred_at, ev.id))

    why = Counter()
    why['dated from the audit trail'] = len(out)
    why['no usable audit row'] = len(want) - len(out)
    return out, why


def apply_plan(rows, *, actor='CRM2-R2'):
    from app.services import audit

    for lead, when, ev_id in rows:
        lead.stage_entered_at = when
        audit.record(ACTION, 'lead', str(lead.id),
                     old={'stage_entered_at': None},
                     new={'stage_entered_at': when.isoformat(
                         timespec='seconds'),
                         'stage': lead.stage,
                         'from_audit_event': ev_id},
                     actor=actor,
                     reason=f'dated from audit event #{ev_id}, the last '
                            f'time this lead moved to {lead.stage}')
    return rows


def undo(*, actor='CRM2-R2'):
    """Null exactly the rows this script set, and nothing else."""
    from app.services import audit

    done = (AuditEvent.query
            .filter(AuditEvent.action == ACTION,
                    AuditEvent.entity_type == 'lead')
            .order_by(AuditEvent.id.asc()).all())
    cleared = []
    for ev in done:
        try:
            lead = db.session.get(Lead, int(ev.entity_id))
        except (TypeError, ValueError):
            continue
        if lead is None or lead.stage_entered_at is None:
            continue
        lead.stage_entered_at = None
        audit.record('crm2.stage_entered_reverted', 'lead',
                     str(lead.id), actor=actor,
                     reason='backfill reverted')
        cleared.append(lead)
    return cleared


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
            cleared = undo()
            db.session.commit()
            print(f'  {len(cleared)} lead(s) put back to no stage date.')
            return

        total = Lead.query.count()
        have = Lead.query.filter(
            Lead.stage_entered_at.isnot(None)).count()
        print(f'  leads                        {total}')
        print(f'    already have a stage date  {have}')
        print(f'    missing one                {total - have}')

        # The cockpit's note counts *open* leads, so the two figures
        # read differently and somebody has to reconcile them by
        # hand. Both are printed here instead.
        from app.services import sales_rules as rules
        open_q = Lead.query.filter(
            Lead.is_archived.isnot(True),
            Lead.stage.in_(rules.open_stages()))
        open_total = open_q.count()
        open_missing = open_q.filter(
            Lead.stage_entered_at.is_(None)).count()
        print(f'  open leads                   {open_total}')
        print(f'    missing a stage date       {open_missing}'
              f'   <- the number the cockpit shows')

        rows, why = plan()
        print()
        for label, n in why.most_common():
            print(f'    {label:<28} {n}')

        if rows:
            print('\n  a sample of what would be written:')
            for lead, when, ev_id in rows[:8]:
                print(f'    #{lead.id:<7} {(lead.stage or ""):<20} '
                      f'{when:%Y-%m-%d}  (audit #{ev_id})')

        if args.check:
            print('\nDRY RUN — nothing written. `--apply` writes these '
                  'dates; `--down --yes` puts them back.')
            return

        apply_plan(rows)
        try:
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            raise SystemExit(1)
        print(f'\n  {len(rows)} lead(s) dated, each with an audit row.')
        print(f'  {why["no usable audit row"]} left without a date — '
              f'the stage map draws these grey and says so.')


if __name__ == '__main__':
    main()
