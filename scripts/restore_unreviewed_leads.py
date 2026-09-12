"""
Put back the leads that left the review queue unseen — one-off.

fix_review_backfill.py marked a row settled when its classification was
not J_needs_review. That was too broad. A step-10 lead scoring between
50 and 79 is stored as A_new_lead with needs_review set: the
classification says "lead", the flag says "but a person should look".
Those were swept out with the genuinely settled ones.

Lead 11670 is the example that found this — an empty message that
scored 62 on our own company tagline in the subject line. It created a
lead and left the queue without anyone seeing it.

This only changes review_state, and only for rows nobody has touched.
No classification changes, no lead is created, and nothing is deleted.

Usage
    python scripts/restore_unreviewed_leads.py --check     # DRY RUN
    python scripts/restore_unreviewed_leads.py --apply
"""
import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_ROOT, '.env'))
except ImportError:
    pass

from sqlalchemy import create_engine, text          # noqa: E402


#: Swept, but the classifier had asked for a person: it is accepted, no
#: human ever touched it, and the score was below the 80 at which
#: needs_review is cleared. corrected_at is the proof nobody looked —
#: every human action in intake/service.py writes it.
SWEPT = (
    "review_state = 'accepted' "
    'AND corrected_at IS NULL '
    'AND confidence IS NOT NULL AND confidence < 80'
)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true',
                    help='show what would move and write nothing')
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    if not (args.check or args.apply):
        ap.error('pass --check to preview or --apply to write')

    url = os.environ.get('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))
    print(f'database: {url}\n')

    with create_engine(url).begin() as conn:
        rows = conn.execute(text(
            'SELECT id, classification, confidence, created_lead_id, subject '
            f'FROM email_classifications WHERE {SWEPT} '
            'ORDER BY confidence, id')).fetchall()

        if not rows:
            print('  Nothing was swept. The queue holds everything that '
                  'asked for a person.')
            return

        print(f'  {len(rows)} classification(s) left the queue unseen:\n')
        for cid, klass, conf, lead_id, subject in rows:
            made = f'lead {lead_id}' if lead_id else 'no lead'
            print(f'    #{cid:<6} {conf:>3}%  {klass:<20} {made:<10} '
                  f'{(subject or "")[:44]}')

        if not args.apply:
            print('\n== DRY RUN — nothing written ==')
            print('  --apply would set review_state back to pending on '
                  'these.\n  No classification changes. No lead is created '
                  'or deleted.\n  Any lead already created stays exactly as '
                  'it is; this only\n  puts the decision back in front of a '
                  'person.')
            return

        moved = conn.execute(text(
            "UPDATE email_classifications SET review_state = 'pending' "
            f'WHERE {SWEPT}')).rowcount
        print(f'\n  {moved} back in the review queue. Nothing else changed.')


if __name__ == '__main__':
    raise SystemExit(main())
