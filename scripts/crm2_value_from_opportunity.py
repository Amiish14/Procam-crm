"""
The number was in the next table all along.

    .venv/bin/python scripts/crm2_value_from_opportunity.py --check
    .venv/bin/python scripts/crm2_value_from_opportunity.py --apply
    .venv/bin/python scripts/crm2_value_from_opportunity.py --down --yes

CRM-VALUE-01 found 928 of 945 open leads with no estimate and
treated that as 928 figures nobody had produced. For 118 of them the
figure exists on the opportunity row beside the lead, joined by
`opp_number`, and has existed the whole time.

Copied, not confirmed
    `value_source='opportunity'` and `value_confirmed=False`. The
    lead then has a number, so the stage gate stops blocking it, and
    it stays on the "Value missing" tile as "Confirm value" until its
    owner agrees with it or types a better one.

    That split is the whole point. A figure copied from a row nobody
    has looked at in months is better than nothing and is not the
    same as an owner's estimate. Writing it in silently would make
    the value gate — the one thing that got owners to type a number —
    pass on evidence nobody has checked.

Only open leads, only where the lead has nothing. A lead whose owner
has already given a figure is never touched, whatever the
opportunity says.
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, Opportunity, STAGES_TERMINAL, app, db  # noqa: E402
from app.models.audit import AuditEvent                      # noqa: E402

SOURCE = 'opportunity'
ACTION = 'crm2.value_from_opportunity'


def plan():
    """[(lead, opportunity, amount)] — open, empty, and answerable."""
    by_number = {}
    for o in Opportunity.query.all():
        num = (o.opp_number or '').strip()
        if num and (o.value_inr or 0) > 0:
            by_number.setdefault(num, o)

    out = []
    for lead in Lead.query.filter(
            Lead.is_archived.isnot(True),
            ~Lead.stage.in_(STAGES_TERMINAL)).order_by(
                Lead.id.asc()).all():
        if (lead.estimated_value_inr or 0) > 0:
            continue
        num = (lead.opp_number or '').strip()
        opp = by_number.get(num) if num else None
        if opp is None:
            continue
        if opp.lead_id and opp.lead_id != lead.id:
            # The number belongs to a different lead. Two leads
            # sharing an opp_number is a data problem, not a licence
            # to copy a figure onto the wrong one.
            continue
        out.append((lead, opp, opp.value_inr))
    return out


def apply_plan(rows, *, actor='CRM2-VALUE'):
    from app.services import audit

    for lead, opp, amount in rows:
        lead.estimated_value_inr = amount
        lead.value_source = SOURCE
        lead.value_confirmed = False
        audit.record(ACTION, 'lead', str(lead.id),
                     old={'estimated_value_inr': None,
                          'value_source': None},
                     new={'estimated_value_inr': float(amount),
                          'value_source': SOURCE,
                          'value_confirmed': False,
                          'from_opportunity': opp.opp_number},
                     actor=actor,
                     reason=f'copied from opportunity '
                            f'{opp.opp_number}; unconfirmed until the '
                            f'owner agrees with it')
    return rows


def undo(*, actor='CRM2-VALUE'):
    """Clear only what this script wrote, and only if untouched."""
    from app.services import audit

    cleared, kept = [], []
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
        if lead.value_confirmed:
            kept.append((lead, 'the owner has confirmed it'))
            continue
        written = float((ev.new_value or {}).get(
            'estimated_value_inr') or 0)
        if abs(float(lead.estimated_value_inr or 0) - written) > 0.01:
            kept.append((lead, 'the figure has been edited since'))
            continue
        lead.estimated_value_inr = None
        lead.value_source = None
        lead.value_confirmed = None
        audit.record('crm2.value_from_opportunity_reverted', 'lead',
                     str(lead.id), actor=actor, reason='copy reverted')
        cleared.append(lead)
    return cleared, kept


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
        mode = ('APPLY' if args.apply else 'DOWN' if args.down
                else 'DRY RUN — nothing is written')
        print(f'MODE:     {mode}\n')

        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            cleared, kept = undo()
            db.session.commit()
            print(f'  {len(cleared)} lead(s) put back to no value.')
            for lead, why in kept:
                print(f'    #{lead.id} left alone: {why}')
            return

        rows = plan()
        total = sum(float(a) for _l, _o, a in rows)
        print(f'  open leads with a value waiting   {len(rows)}')
        print(f'  total                             '
              f'INR {total:,.0f}\n')
        print(f'    {"lead":<9}{"stage":<20}{"company":<32}'
              f'{"value":>16}  opportunity')
        for lead, opp, amount in rows:
            print(f'    #{lead.id:<8}{(lead.stage or "")[:19]:<20}'
                  f'{(lead.company or "")[:31]:<32}'
                  f'{float(amount):>16,.0f}  {opp.opp_number}')

        if args.check:
            print('\nDRY RUN — nothing written. `--apply` copies these '
                  'in as unconfirmed; they stay on the "Value missing" '
                  'tile as "Confirm value" until an owner agrees.')
            return

        apply_plan(rows)
        try:
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'  !! ROLLED BACK: {type(exc).__name__}: '
                  f'{str(exc)[:200]}')
            raise SystemExit(1)
        print(f'\n  {len(rows)} lead(s) given a value, each with an '
              f'audit row, each awaiting confirmation.')


if __name__ == '__main__':
    main()
