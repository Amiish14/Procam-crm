"""
Take settled decisions back out of the review queue — one-off.

The first run of 2026_09_27 marked every classification with no lead as
'pending'. That swept in thread matches, internal mail and quote
submissions — decisions the classifier made correctly and which need
nobody. The queue would open with a backlog of nothing to do, which is
the fastest way to make a review screen ignored.

Only J_needs_review, and anything a person has already touched, belongs
in the queue.

Usage
    python scripts/fix_review_backfill.py --check
    python scripts/fix_review_backfill.py
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--check', action='store_true')
    args = ap.parse_args()

    url = os.environ.get('DATABASE_URL') or (
        'sqlite:///' + os.path.join(_ROOT, 'procam_crm.db'))
    print(f'database: {url}')

    with create_engine(url).begin() as conn:
        rows = conn.execute(text(
            "SELECT classification, COUNT(*) FROM email_classifications "
            "WHERE review_state = 'pending' "
            'AND corrected_at IS NULL '
            "AND classification != 'J_needs_review' "
            'GROUP BY classification ORDER BY COUNT(*) DESC')).fetchall()
        total = sum(n for _c, n in rows)

        if not total:
            print('  nothing to move — the queue only holds items that '
                  'asked for a person.')
            return

        print(f'  {total} settled decision(s) sitting in the review queue:')
        for klass, n in rows:
            print(f'    {n:>5}  {klass}')

        if args.check:
            print('\n== DRY-RUN — nothing written ==')
            print('  These would be marked settled. Nothing is deleted and '
                  'no\n  classification changes — only whether the queue '
                  'shows them.')
            return

        moved = conn.execute(text(
            "UPDATE email_classifications SET review_state = 'accepted' "
            "WHERE review_state = 'pending' AND corrected_at IS NULL "
            "AND classification != 'J_needs_review'")).rowcount
        print(f'\n  moved {moved} out of the queue. The classifications '
              f'themselves are unchanged.')


if __name__ == '__main__':
    main()
