"""
Close the leads under a placeholder account that nothing can identify.

    .venv/bin/python scripts/close_unidentifiable_leads.py --check
    .venv/bin/python scripts/close_unidentifiable_leads.py --apply
    .venv/bin/python scripts/close_unidentifiable_leads.py --down --yes

Who is in scope, and the three conditions are all required:

    the lead sits under a placeholder account (#983 "Unknown",
    #847 "New Lead", and the rest by the same name rule);
    it carries no sender address, no usable company name and no
    GSTIN — there is nothing to identify it with, now or later;
    it was created more than four months ago.

A lead that meets all three has been sitting open for a third of
a year with nobody able to say whose it is. Closing it as Lost /
"No Response" is the honest record. Newer ones stay in Account
check: four months is the line, and a lead from last month may
simply not have been looked at yet.

What it will not do
    Touch a lead with any identifier at all, however old. Those
    can still be matched, and closing them would destroy the one
    thing that makes that possible.

    Delete anything. Lost is a stage, and `--down --yes` restores
    every stage and reason from the audit trail.

A note on the reason
    "No Response" is in app.LOST_REASONS but not in the
    `lost_reason` Master Data list that fills the dropdown — two
    lists that disagree, like the stage lists did. The value is
    legal and is what the rule asks for; reconciling the two lists
    is a separate decision.
"""
import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Company, Lead, app, db                    # noqa: E402

ACTION = 'lead.closed_unidentifiable'
REASON = 'No Response'
MONTHS = 4
CEILING = 200


def placeholder_ids():
    from app.services import account_match
    return [c.id for c in Company.query.all()
            if account_match.not_a_customer(c)]


def identifiers(lead):
    """What this lead could ever be matched on. Empty means nothing."""
    from app.services import account_match, account_suggest

    found = []
    sender = account_suggest.sender_of(lead)
    if sender and account_match.domain_of(sender):
        found.append(f'sender {sender}')

    # A name like "New Lead" is the placeholder's own name copied
    # onto the lead; it identifies nothing.
    name = (lead.company or '').strip()
    if (name.lower() not in account_match.PLACEHOLDER_NAMES
            and len(account_match.strict_norm(name)) >= 3):
        found.append(f'name "{name}"')

    try:
        from app.services import lead_intake as li
        text = (f'{lead.forwarded_subject or ""}\n'
                f'{lead.original_email_subject or ""}\n'
                f'{lead.original_email_body or lead.notes or ""}')
        for number in li.gstins(text[:4000]):
            found.append(f'GSTIN {number}')
            break
    except Exception:                                     # noqa: BLE001
        pass

    # A link back to the ERP opportunity, or a named contact carried
    # in from one. Lead #9246 had both and was still counted as
    # unidentifiable, which would have closed an active negotiation
    # as "No Response" — it has a customer, we just have not written
    # it into company_id yet.
    notes = (lead.notes or '')
    if 'myprocam.in' in notes.lower():
        found.append('an ERP opportunity link')
    import re as _re
    named = _re.search(r'Owner \(unmatched\):\s*([^|\n]{3,60})',
                       notes)
    if named:
        found.append(f'a named contact ({named.group(1).strip()})')
    return found


def plan(*, months=MONTHS):
    from app.services import account_gate, sales_rules

    cutoff = datetime.utcnow() - timedelta(days=months * 30)
    ids = placeholder_ids()
    if not ids:
        return [], []

    rows = (Lead.query
            .filter(Lead.company_id.in_(ids),
                    Lead.is_archived.isnot(True),
                    Lead.stage.in_(sales_rules.open_stages()))
            .order_by(Lead.id).all())

    close, keep = [], []
    for lead in rows:
        why = identifiers(lead)
        old = lead.created_at is not None and lead.created_at < cutoff
        entry = {'lead': lead, 'identifiers': why,
                 'vertical': account_gate.vertical_of(lead)
                 or '(no vertical)',
                 'age_days': ((datetime.utcnow() - lead.created_at).days
                              if lead.created_at else None)}
        if why:
            entry['reason'] = f'has {len(why)} identifier(s)'
            keep.append(entry)
        elif not old:
            entry['reason'] = (f'only {entry["age_days"]} days old — '
                               f'stays in Account check')
            keep.append(entry)
        else:
            close.append(entry)
    return close, keep


def apply_plan(close, *, actor='DIR12010'):
    from app.services import audit

    today = datetime.utcnow().date()
    for entry in close:
        lead = entry['lead']
        was = {'stage': lead.stage, 'lost_reason': lead.lost_reason}
        lead.stage = 'Lost'
        lead.lost_reason = REASON
        audit.record(ACTION, 'lead', lead.id, old=was,
                     new={'stage': 'Lost', 'lost_reason': REASON},
                     actor=actor,
                     reason=(f'{today:%Y-%m-%d}: open {entry["age_days"]} '
                             f'days under a placeholder account with '
                             f'no sender, name or GSTIN — nothing to '
                             f'identify it with'))
    return len(close)


def undo(*, actor='DIR12010'):
    from app.models.audit import AuditEvent
    from app.services import audit

    back = 0
    for ev in AuditEvent.query.filter_by(action=ACTION).all():
        try:
            lead = db.session.get(Lead, int(ev.entity_id))
        except (TypeError, ValueError):
            continue
        if lead is None:
            continue
        was = ev.old_value or {}
        lead.stage = was.get('stage') or lead.stage
        lead.lost_reason = was.get('lost_reason')
        audit.record('lead.closed_unidentifiable_reverted', 'lead',
                     lead.id, new={'stage': lead.stage}, actor=actor,
                     reason='sweep reverted')
        back += 1
    return back


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--down', action='store_true')
    ap.add_argument('--yes', action='store_true')
    ap.add_argument('--months', type=int, default=MONTHS)
    ap.add_argument('--actor', default='DIR12010')
    ap.add_argument('--ceiling', type=int, default=CEILING)
    args = ap.parse_args()
    if len([a for a in (args.check, args.apply, args.down) if a]) != 1:
        raise SystemExit('Choose one: --check, --apply or --down.')

    with app.app_context():
        print(f'database: {db.engine.url}')
        if args.down:
            if not args.yes:
                raise SystemExit('--down needs --yes.')
            n = undo(actor=args.actor)
            db.session.commit()
            print(f'  {n} lead(s) put back to their old stage '
                  f'and reason')
            return

        print(f'MODE:     {"APPLY" if args.apply else "DRY RUN"}')
        print(f'cutoff:   older than {args.months} months\n')
        close, keep = plan(months=args.months)

        print(f'  open leads under placeholder accounts  '
              f'{len(close) + len(keep)}')
        print(f'    would be closed as Lost / {REASON}     '
              f'{len(close)}')
        print(f'    stay in Account check                {len(keep)}')

        print('\n  to close, per vertical:')
        for vertical, n in Counter(
                e['vertical'] for e in close).most_common():
            print(f'    {vertical:<24}{n:>6}')

        print('\n  staying, and why:')
        for reason, n in Counter(
                ('has identifiers' if e['identifiers']
                 else 'too new') for e in keep).most_common():
            print(f'    {reason:<24}{n:>6}')

        if close:
            ages = [e['age_days'] for e in close if e['age_days']]
            print(f'\n  age of the ones to close: '
                  f'{min(ages)}–{max(ages)} days '
                  f'(median {sorted(ages)[len(ages) // 2]})')
            print('  a sample:')
            for entry in close[:8]:
                lead = entry['lead']
                print(f'    #{lead.id:<7}{lead.stage:<18}'
                      f'{entry["vertical"]:<20}'
                      f'{entry["age_days"]} days old')

        if args.check:
            print(f'\nDRY RUN — nothing written. The apply refuses '
                  f'above {args.ceiling} rows.')
            return

        if len(close) > args.ceiling:
            raise SystemExit(
                f'\n  REFUSED: {len(close)} rows is over the '
                f'{args.ceiling} ceiling. Closing that many leads in '
                f'one pass is not something to do without looking '
                f'again — re-run --check, and raise --ceiling '
                f'deliberately if the number is right.')

        n = apply_plan(close, actor=args.actor)
        db.session.commit()
        print(f'\n  {n} lead(s) closed as Lost / {REASON}, an audit '
              f'row each carrying today\'s date and the lead\'s age. '
              f'`--down --yes` puts them all back.')


if __name__ == '__main__':
    main()
