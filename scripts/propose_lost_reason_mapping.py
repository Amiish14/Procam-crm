"""
Twenty-three lost reasons onto five, plus Others.

    .venv/bin/python scripts/propose_lost_reason_mapping.py

READ-ONLY. A proposal. No lead is changed, no list is edited, and
there is no --apply: the mapping is a commercial judgement about
what the business wants to measure, so it is approved before it is
built, not after.

Why five
    Twenty-three options is a dropdown people scroll past, and the
    reports it feeds split the same fact four ways — "No Response"
    and "No Response from Client" are 897 and 1,134 leads saying
    the same thing. Five reasons a sales head can act on beats
    twenty-three nobody picks consistently.

Why "Others" keeps free text
    The one reason that must never be forced into a bucket is the
    one nobody anticipated. Without a free-text escape, people
    pick the nearest wrong option and the other five stop meaning
    anything.
"""
import os
import sys
from collections import Counter, defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from sqlalchemy import text                               # noqa: E402

from app import app, db                                   # noqa: E402

#: The proposed five, and what each is for. Each answers a
#: different question a head would ask about a lost deal.
TARGETS = {
    'Price': 'we were too expensive, or the terms did not suit',
    'Lost to competitor': 'somebody else won it',
    'No response': 'the customer stopped answering',
    'Customer cancelled or postponed': 'the work is not happening, '
                                       'or not yet',
    'We could not serve it': 'capability, capacity, geography or '
                             'compliance on our side',
    'Others': 'free text — anything the five do not cover',
}

#: Existing reason -> proposed bucket.
MAPPING = {
    'Price':                            'Price',
    'Price too high':                   'Price',
    'Commercial Terms':                 'Price',
    'Payment terms mismatch':           'Price',
    'Not commercially viable':          'Price',

    'Competition':                      'Lost to competitor',
    'Lost to competitor':               'Lost to competitor',
    'Client selected in-house transport': 'Lost to competitor',

    'No Response':                      'No response',
    'No Response from Client':          'No response',
    'Not interested':                   'No response',
    'Client Decision':                  'No response',

    'Project Cancelled':                'Customer cancelled or postponed',
    'Project Postponed':                'Customer cancelled or postponed',
    'Customer postponed / on hold':     'Customer cancelled or postponed',
    'Timeline mismatch':                'Customer cancelled or postponed',

    'Technical / Capability':           'We could not serve it',
    'Vehicle/capacity unavailable':     'We could not serve it',
    'Service Constraint':               'We could not serve it',
    'Geography Constraint':             'We could not serve it',
    'Documentation / compliance issue': 'We could not serve it',

    'Duplicate / Invalid':              'Others',
    'Other':                            'Others',
}

#: Mappings a person should look at twice. Said out loud rather
#: than buried, because each loses something real.
ARGUABLE = {
    'Not interested': 'A customer who says "not interested" did '
                      'answer. Grouping it with silence loses the '
                      'difference between a no and a non-reply — '
                      'and 1,213 leads carry it.',
    'Client Decision': 'Vague today. It may mean they chose a rival, '
                       'which belongs under Lost to competitor.',
    'Timeline mismatch': 'Could be ours (we could not meet the date) '
                         'or theirs (they moved it).',
    'Client selected in-house transport': 'Not strictly a competitor '
                                          '— they became their own.',
    'Duplicate / Invalid': 'Not a commercial outcome at all. Arguably '
                           'these leads should be marked duplicate '
                           'rather than lost.',
}


def counts():
    rows = db.session.execute(text(
        "SELECT lost_reason, COUNT(*) FROM leads "
        "WHERE lost_reason IS NOT NULL AND TRIM(lost_reason) <> '' "
        "GROUP BY lost_reason")).fetchall()
    return {(r or '').strip(): n for r, n in rows}


def main():
    with app.app_context():
        print(f'database: {db.engine.url}')
        print('READ-ONLY — a proposal. No lead is changed.\n')

        used = counts()
        from app.models.master_data import MasterItem
        listed = [i.code for i in MasterItem.query.filter_by(
            list_key='lost_reason').all()]

        buckets = defaultdict(list)
        for code in sorted(set(listed) | set(MAPPING)):
            buckets[MAPPING.get(code, 'Others')].append(
                (code, used.get(code, 0)))

        print('  PROPOSED FIVE + Others\n')
        total = 0
        for target, purpose in TARGETS.items():
            members = buckets.get(target, [])
            n = sum(c for _code, c in members)
            total += n
            print(f'  {target}  —  {n} lead(s)')
            print(f'      {purpose}')
            for code, c in sorted(members, key=lambda kv: -kv[1]):
                flag = '  <-- look at this one' if code in ARGUABLE \
                    else ''
                print(f'        {code:<36}{c:>6}{flag}')
            print()

        free = sum(n for r, n in used.items() if r not in MAPPING)
        print(f'  mapped from the list      {total}')
        print(f'  free-text reasons on leads{free:>6}  '
              f'(stay as they are, under Others)')

        print('\n  WORTH ARGUING ABOUT before this is applied:')
        for code, why in ARGUABLE.items():
            print(f'\n    {code}  ({used.get(code, 0)} leads)')
            for line in _wrap(why, 64):
                print(f'        {line}')

        print('\n  Nothing has been changed. Approve the mapping and '
              'I will build the migration, with a --down that puts '
              'every original reason back from the audit trail.')


def _wrap(text, width):
    words, line, out = text.split(), '', []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = f'{line} {w}'.strip()
    if line:
        out.append(line)
    return out


if __name__ == '__main__':
    main()
