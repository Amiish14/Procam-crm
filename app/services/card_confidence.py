"""How sure the scanner is, field by field, and why.

CRM-SEARCH-SCAN-01 Part 2. The review form is always editable and
nothing is ever saved without a person looking at it — so the job of
a confidence score is not to decide anything. It is to tell the
reviewer **where to look first**, on a screen with sixteen fields on
it, before they press Save.

So every score carries its reason in words, and the screen prints the
reason rather than the number. "read the same by both passes" and
"worked out from the email domain" are different kinds of fact, and
the second is the one worth checking.

THE LADDER

    95  both readings agree
    90  validated by its own shape — an address that parses, a number
        the phone library accepts for its region, a state on the list
    80  one reading, and it looks like what it claims to be
    55  the two readings DISAGREE — the screen shows both
    45  not read off the card at all: worked out from something else
        (a company from the email domain, a city implied by a state,
        an industry taken from the account it matched)
     0  nothing there

LOW_AT is 70: anything below it is highlighted. That puts "disagreed"
and "worked out" in front of the reviewer and leaves the fields that
two passes read identically alone.
"""
from __future__ import annotations

import re

LOW_AT = 70

AGREED = 95
VALIDATED = 90
READ_ONCE = 80
DISAGREED = 55
DERIVED = 45
EMPTY = 0

_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$')
_URL_RE = re.compile(r'^(?:https?://)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/.*)?$')

#: The fields the review screen shows, in the order it shows them.
FIELDS = ('name', 'first_name', 'last_name', 'designation', 'department',
          'company', 'email', 'mobile', 'telephone', 'website',
          'street', 'city', 'state', 'country', 'postal_code',
          'linkedin_url', 'industry')


def _flat(value):
    return re.sub(r'[^a-z0-9]', '', str(value or '').lower())


def _shape_ok(field, value):
    """Whether the value is the kind of thing the field is for."""
    text = str(value or '').strip()
    if not text:
        return False
    if field == 'email':
        return bool(_EMAIL_RE.match(text))
    if field in ('website', 'linkedin_url'):
        return bool(_URL_RE.match(text))
    if field in ('mobile', 'telephone'):
        return text.startswith('+') and 8 <= len(re.sub(r'\D', '', text)) <= 15
    if field == 'postal_code':
        return bool(re.fullmatch(r'\d{5,6}(?:-\d{4})?', text))
    if field == 'state':
        from app.services.card_fields import INDIAN_STATES
        return text in INDIAN_STATES
    if field == 'country':
        from app.services.card_fields import COUNTRY_WORDS
        return text in set(COUNTRY_WORDS.values())
    if field in ('name', 'first_name', 'last_name', 'company'):
        return len(text) > 1 and not text.isdigit()
    return True


def score(fields, *, readings=(), conflicts=(), derived=(), notes=None):
    """{field: {'score', 'why', 'low'}} for everything on the form.

    `readings` is what each pass produced — one dict per pass, in the
    order they ran. Two passes that agree is the strongest thing the
    scanner can say about a field without a person; two that disagree
    is the most useful thing it can say TO a person.

    `derived` names the fields that were worked out rather than read.
    `conflicts` is what the cross-check already flagged.
    """
    fields = fields or {}
    conflicts = set(conflicts or ())
    derived = set(derived or ())
    out = {}
    for field in FIELDS:
        value = fields.get(field)
        text = str(value or '').strip()
        if not text:
            out[field] = {'score': EMPTY, 'why': 'nothing on the card',
                          'low': False, 'empty': True}
            continue
        seen = [str((r or {}).get(field) or '').strip()
                for r in readings or ()]
        seen = [v for v in seen if v]
        agreed = len(seen) > 1 and len({_flat(v) for v in seen}) == 1
        if field in conflicts:
            others = sorted({v for v in seen if _flat(v) != _flat(text)})
            out[field] = {
                'score': DISAGREED,
                'why': ('the two passes read this differently'
                        + (f' — also read as "{others[0]}"' if others else '')),
                'low': True, 'empty': False,
                'alternatives': others}
            continue
        if field in derived:
            out[field] = {'score': DERIVED,
                          'why': 'worked out, not read off the card',
                          'low': True, 'empty': False}
            continue
        if agreed:
            out[field] = {'score': AGREED, 'why': 'both passes read it the '
                                                  'same', 'low': False,
                          'empty': False}
            continue
        if _shape_ok(field, text):
            out[field] = {'score': VALIDATED,
                          'why': 'checked against what this field must '
                                 'look like',
                          'low': False, 'empty': False}
            continue
        out[field] = {'score': READ_ONCE, 'why': 'read once, unchecked',
                      'low': READ_ONCE < LOW_AT, 'empty': False}
    if notes:
        out['_notes'] = list(notes)
    return out


def low_fields(scores):
    """What the reviewer should look at first."""
    return [f for f, s in (scores or {}).items()
            if isinstance(s, dict) and s.get('low')]


def summary(scores):
    """One line for the top of the review form."""
    low = low_fields(scores)
    filled = [f for f, s in (scores or {}).items()
              if isinstance(s, dict) and not s.get('empty')]
    if not filled:
        return 'Nothing could be read off this card. Type what it says.'
    if not low:
        return (f'{len(filled)} field(s) read. Check them and save — '
                f'nothing is stored until you do.')
    return (f'{len(filled)} field(s) read, {len(low)} worth a look: '
            f'{", ".join(low[:4])}'
            + ('…' if len(low) > 4 else '') + '.')
