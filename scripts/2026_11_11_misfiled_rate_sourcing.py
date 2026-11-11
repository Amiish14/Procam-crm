"""Customer mail filed as our own rate sourcing. Put it back.

CRM-MAIL-2026-11-11 11: on three leads the customer's own RFQ showed
under "Rate sourcing - what we asked suppliers, kept out of the
customer thread".

    .venv/bin/python scripts/2026_11_11_misfiled_rate_sourcing.py --check
    .venv/bin/python scripts/2026_11_11_misfiled_rate_sourcing.py --apply

TWO FAULTS WITH ONE SYMPTOM, AND ONLY ONE OF THEM IS DATA

The first was the route: `/api/leads/<id>/emails` falls back to the
`original_email_*` columns when a lead has no trail rows yet, and the
fallback ran for every `kind` - so the enquiry that started the lead
appeared in the supplier list as well as the customer one. That is
fixed in code and needs no backfill; it accounts for every lead whose
trail is still empty, which is most new ones.

The second is this script. `intake_class` is written once from the
classifier's verdict, and step 6 of the classifier says "rate
sourcing" for anything whose sender domain is in the Vendor Master. A
company that supplies us and also buys from us is both, and their
enquiry was then filed as our own rate request.

WHAT IT MOVES, AND WHAT IT WILL NOT TOUCH

Only a row that is all three of: classed `G_rate_sourcing`, inbound,
and from the lead's own customer - the address or the domain the lead
itself holds, never free mail and never one of ours.
`app.services.mail_filing` is the one place that rule lives, so the
screen and this script cannot disagree.

It does not reclassify by guesswork. A supplier's quotation, a rate
request we sent, a reply from a shipping line: all left exactly where
they are, whatever the wording. The only thing it knows better than
the classifier is who the customer is, because the lead says so.

The new class is `B_existing_lead_comm` - "existing lead
communication", the class the classifier itself uses for a customer
writing about an enquiry we already hold. Not blank: blank means "we
never classified this", and these were classified, wrongly.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import Lead, LeadEmail, app, db                    # noqa: E402

ACTOR = 'CRM-MAIL-2026-11-11'
#: What a customer writing about an enquiry we already hold is
#: called. `lead_intake.Klass.EXISTING`.
NEW_CLASS = 'B_existing_lead_comm'
DEFAULT_CSV = os.path.join(_ROOT, 'data', 'misfiled_rate_sourcing_11NOV.csv')


def survey():
    """Rows called rate sourcing that are plainly the customer's."""
    from app.services import mail_filing

    rows = (LeadEmail.query
            .filter(LeadEmail.intake_class == mail_filing.RATE_SOURCING)
            .order_by(LeadEmail.id.asc())
            .all())
    leads = {}
    found, kept = [], []
    for row in rows:
        lead = leads.get(row.lead_id)
        if lead is None and row.lead_id:
            lead = db.session.get(Lead, row.lead_id)
            leads[row.lead_id] = lead
        if lead is None:
            kept.append((row, 'no lead on the row'))
            continue
        if mail_filing.misfiled(lead, row):
            found.append((lead, row))
        else:
            kept.append((row, 'supplier-facing, or not from the '
                              'lead\'s own customer'))
    return found, kept, len(rows)


def report(found, kept, total):
    print(f'\n── {total} row(s) are classed rate sourcing ──')
    print(f'  {len(found)} of them arrived from the lead\'s own '
          f'customer, which makes them customer mail.')
    print(f'  {len(kept)} are left exactly where they are.')
    if found:
        print('\n  what would move:')
        for lead, row in found[:30]:
            print(f'    lead #{lead.id:<7} {(lead.company or "")[:24]:<24} '
                  f'from {(row.from_addr or "")[:34]:<34} '
                  f'{(row.subject or "")[:34]!r}')
        if len(found) > 30:
            print(f'    … and {len(found) - 30} more')
        by_domain = Counter(
            (row.from_addr or '').rsplit('@', 1)[-1].lower()
            for _lead, row in found)
        print('\n  by sender domain — these are the companies that are '
              'both a supplier and a customer:')
        for domain, n in by_domain.most_common(12):
            print(f'    {n:>5}  {domain}')
    print('\n  The display no longer depends on this: the Rate '
          'sourcing panel asks who the message is between, not what '
          'it was called. This corrects the stored class, which the '
          'data-quality checks and the reports read.')


def write_csv(found, path):
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(['lead_id', 'company', 'email_row_id', 'direction',
                  'from_addr', 'lead_email', 'subject', 'at'])
    for lead, row in found:
        out.writerow([lead.id, (lead.company or '')[:60], row.id,
                      row.direction or '', (row.from_addr or '')[:70],
                      (lead.email or lead.original_email_from or '')[:70],
                      (row.subject or '')[:90],
                      str(row.sent_or_received_at or '')[:19]])
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as fh:
        fh.write(buf.getvalue())
    print(f'\nCSV: {path}')
    return path


def apply(found):
    from app.services import audit, mail_filing

    moved = 0
    try:
        for lead, row in found:
            if not mail_filing.misfiled(lead, row):
                continue                  # changed under us
            audit.record('lead_email.refiled', 'lead', lead.id,
                         old={'intake_class': row.intake_class,
                              'email_row': row.id},
                         new={'intake_class': NEW_CLASS,
                              'email_row': row.id},
                         actor=ACTOR,
                         reason=(f'CRM-MAIL-2026-11-11 §11: it arrived '
                                 f'from {row.from_addr}, which is this '
                                 f'lead\'s own customer, so it is not '
                                 f'rate sourcing')[:400])
            row.intake_class = NEW_CLASS
            moved += 1
        db.session.commit()
    except Exception as exc:                              # noqa: BLE001
        db.session.rollback()
        print(f'\n  !! ROLLED BACK: {type(exc).__name__}: {str(exc)[:200]}')
        raise SystemExit(1)
    print(f'\n{moved} row(s) refiled as {NEW_CLASS}, each with an audit '
          f'row naming the address it came from.')
    print('Nothing was deleted and no body was touched: one column.')
    return moved


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='report only. The default.')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--csv', nargs='?', const=DEFAULT_CSV, default=None,
                    metavar='PATH')
    args = ap.parse_args(argv)

    with app.app_context():
        print(f'database: {db.engine.url}')
        print('CRM-MAIL-2026-11-11 §11 — customer mail in the supplier '
              'panel.' + ('' if args.apply else ' REPORT ONLY.'))
        print('─' * 70)
        found, kept, total = survey()
        report(found, kept, total)
        if args.csv:
            write_csv(found, args.csv)
        if not args.apply:
            print(f'\nDRY RUN — nothing written. --apply refiles the '
                  f'{len(found)} row(s) above.')
            db.session.rollback()
            return 0
        apply(found)
        return 0


if __name__ == '__main__':
    raise SystemExit(main())
