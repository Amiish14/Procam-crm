"""
Read the deadline off every enquiry we already have.

    .venv/bin/python scripts/quote_due_backfill.py --check
    .venv/bin/python scripts/quote_due_backfill.py --apply
    .venv/bin/python scripts/undo_batch.py --batch cockpit02.quote_due.<date> --apply

CRM-COCKPIT-02 §2.8. Every open lead at RFQ Generated or later gets
its submission deadline read out of the mail it arrived on — the
portal's own subject line first, then a GeM or tender bid end, then a
sentence in the body. 137 leads sit at those stages with 28 quote
dates between them; the deadline was in the subject line all along and
there was no field to put it in.

Two rules this keeps, from §2.8 and §0.3:

  * **no invented dates.** A lead whose mail says nothing goes to the
    "Due date missing" tile for its owner to fill. It is not given a
    date derived from `created_at`, from the target date, or from the
    stage — a guessed deadline is worse than a blank one, because a
    blank one is visibly blank and a guess looks like a fact.
  * **every change is reversible.** Each date written goes through
    `bulk_log`, so `scripts/undo_batch.py` puts every one of them back
    without touching a date an owner has since typed.

Nothing is confirmed by this script. Each date arrives as
"auto — please confirm", which is what the Due date to confirm tile
counts: the CRM reading a line out of a mail is a proposal, and 2,391
leads' worth of proposals is not a decision anybody made.
"""
import argparse
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app import Lead, app, db                             # noqa: E402

BATCH = 'cockpit02.quote_due'
NOTE = ('01-Nov-2026 CRM-COCKPIT-02: quote submission due date read '
        'from the enquiry mail (rule: §2.8, portal subject then mail '
        'body; unconfirmed)')

#: §2.8 — the population. RFQ Generated or later, still open.
STAGES = ('RFQ Generated', 'Quoted', 'Under Negotiation')


def population():
    return (Lead.query
            .filter(Lead.is_archived.isnot(True),
                    Lead.stage.in_(STAGES),
                    Lead.quote_due_at.is_(None))
            .order_by(Lead.id).all())


def plan():
    """[(lead, reading)] and why the rest were left alone."""
    from app.services import quote_due as qd

    out, why = [], Counter()
    for lead in population():
        reading = qd.read_lead(lead)
        if reading is None:
            why['no date in the mail — owner to fill'] += 1
            continue
        why[f'read from the {reading.source.replace("_", " ")}'] += 1
        out.append((lead, reading))
    return out, why


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--actor', default='CRM-COCKPIT-02')
    args = ap.parse_args()
    if not (args.check or args.apply):
        args.check = True

    with app.app_context():
        from app.services import bulk_log, quote_due as qd

        print(f'database: {db.engine.url}')
        print('MODE:     ' + ('APPLY' if args.apply
                              else 'CHECK — nothing written') + '\n')

        everyone = Lead.query.filter(
            Lead.is_archived.isnot(True),
            Lead.stage.in_(STAGES)).count()
        rows, why = plan()
        print(f'  open leads at RFQ Generated or later   {everyone}')
        print(f'    already carry a deadline             '
              f'{everyone - len(population())}')
        print(f'    would be read from the mail          {len(rows)}')
        print()
        for label, n in why.most_common():
            print(f'    {label:<40} {n}')

        if rows:
            print('\n  a sample of what would be written:')
            for lead, reading in rows[:10]:
                print(f'    #{lead.id:<7} {(lead.company or "")[:26]:<26} '
                      f'{qd.shown(reading.at)}  '
                      f'({reading.source})')

        # Also the portal key, which comes off the same subject line
        # and is what §5.6 keys a portal's three mails on.
        keys = [(l, qd.portal_key(l.original_email_subject or '',
                                  l.original_email_body or ''))
                for l in population()]
        keys = [(l, k) for l, k in keys if k and not l.portal_key]
        print(f'\n  portal numbers that would also be filled: '
              f'{len(keys)}')

        if args.check:
            print('\nCHECK ONLY — nothing written. --apply writes '
                  'these;\nscripts/undo_batch.py puts them back.')
            return 0

        written = 0
        with bulk_log.batch(BATCH, note=NOTE, actor=args.actor) as log:
            for lead, reading in rows:
                # Through `bulk_log.set` for the reversible log, and
                # then the history row and the audit row by hand: the
                # due date has three records rather than one, and a
                # back-fill that writes the column without the
                # history would leave §2.4's "Extended n×" counting
                # from a date nobody can see the origin of.
                if not log.set(lead, 'quote_due_at', reading.at):
                    continue
                log.set(lead, 'quote_due_source', reading.source)
                log.set(lead, 'quote_due_confirmed', False)
                from app.models.cockpit02 import LeadQuoteDueHistory
                from app.services import audit
                db.session.add(LeadQuoteDueHistory(
                    lead_id=lead.id, old_due_at=None,
                    new_due_at=reading.at, reason_code='set',
                    reason_text=reading.evidence,
                    source=reading.source, actor=args.actor))
                audit.record('lead.quote_due_backfilled', 'lead',
                             lead.id,
                             new={'quote_due_at': str(reading.at),
                                  'source': reading.source},
                             actor=args.actor, reason=NOTE,
                             commit=False)
                written += 1
            for lead, key in keys:
                log.set(lead, 'portal_key', key)
            batch_name = log.name
            fields = log.fields

        try:
            db.session.commit()
        except Exception as exc:                          # noqa: BLE001
            db.session.rollback()
            print(f'\n  !! ROLLED BACK — nothing was written: '
                  f'{type(exc).__name__}: {str(exc)[:200]}')
            return 1

        print(f'\n  {written} deadline(s) written, each unconfirmed.')
        for field, n in sorted(fields.items()):
            print(f'    {field:<24} {n}')
        print(f'\n  reversible log batch: {batch_name}')
        print(f'  undo: .venv/bin/python scripts/undo_batch.py '
              f'--batch {batch_name} --apply')
        print(f'\n  {why["no date in the mail — owner to fill"]} lead(s) '
              f'have no date in their mail. These are the\n  "Due date '
              f'missing" tile — no date was invented for them.')
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
