"""
Take settled decisions back out of the review queue — one-off.

The first run of 2026_09_27 marked every classification with no lead as
'pending'. That swept in thread matches, internal mail and quote
submissions — decisions the classifier made correctly and which need
nobody. The queue would open with a backlog of nothing to do, which is
the fastest way to make a review screen ignored.

Only J_needs_review, anything a person has already touched, and
anything the classifier itself asked for a person on, belongs in the
queue.

That last clause was missing from the first version of this script. A
step-10 lead scoring between 50 and 79 is recorded as A_new_lead with
needs_review set — the classification says "lead", the flag says "but
look at it". Matching on classification alone swept those out, and lead
11670 (an empty message that scored 62 on our own tagline in the
subject) left the queue without anyone seeing it.
scripts/restore_unreviewed_leads.py puts them back.

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


#: What "settled" means, in one place. A row is settled when the
#: classifier was sure and nobody has touched it. The confidence clause
#: is the part the first version was missing: below 80 at step 10 the
#: classifier set needs_review itself, and that is a request, not noise.
SETTLED = (
    "review_state = 'pending' "
    'AND corrected_at IS NULL '
    "AND classification != 'J_needs_review' "
    'AND NOT (confidence IS NOT NULL AND confidence < 80)'
)


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
            f'WHERE {SETTLED} '
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
            f'WHERE {SETTLED}')).rowcount
        print(f'\n  moved {moved} out of the queue. The classifications '
              f'themselves are unchanged.')


if __name__ == '__main__':
    main()
