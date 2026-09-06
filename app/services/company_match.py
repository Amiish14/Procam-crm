"""Matching a free-text company name to the Company Master.

Used by both the de-duplication migration and the Lead/Contact linking, so
the two cannot drift apart — if they normalised differently, a lead could
be linked to a record that de-duplication had already retired.

The rule throughout is §65: link on certainty, queue on doubt.  Nothing
here guesses.
"""
from collections import defaultdict

_SUFFIXES = (' pvt', ' ltd', ' limited', ' private', ' inc', ' llc',
             ' gmbh', ' corporation', ' corp', ' company', ' group',
             ' co', ' plc', ' sa', ' nv', ' bv', ' ag')

_PUNCT = '.,()[]/\\-_&\'"'


def norm(name):
    """Normalise a company name for exact matching.

    Deliberately conservative: punctuation and legal suffixes go, but
    nothing is expanded or abbreviated.  'BHEL' and 'Bharat Heavy
    Electricals' stay distinct, because merging them is a judgement a
    person should make.
    """
    if not name:
        return ''
    s = str(name).lower().strip()
    for ch in _PUNCT:
        s = s.replace(ch, ' ')
    s = ' '.join(s.split())
    changed = True
    while changed:
        changed = False
        for suf in _SUFFIXES:
            if s.endswith(suf):
                s, changed = s[: -len(suf)].strip(), True
    return ' '.join(s.split())


def build_index(companies):
    """normalised name → [company, ...].

    A list, not a single company: after de-duplication there should be one
    per key, but if two survive the caller must be told rather than have
    one silently picked.
    """
    index = defaultdict(list)
    for c in companies:
        key = norm(c.name)
        if key:
            index[key].append(c)
    return index


NO_MATCH  = 'no_match'
AMBIGUOUS = 'ambiguous'


def match(raw_name, index):
    """Resolve one name.

    Returns ``(company, reason, candidates)``:
      * a company and reason None  — certain, safe to link
      * None and NO_MATCH          — nothing resembles it
      * None and AMBIGUOUS         — several equally good, needs a person
    """
    key = norm(raw_name)
    if not key:
        return None, NO_MATCH, []

    hits = index.get(key) or []
    if len(hits) == 1:
        return hits[0], None, []
    if len(hits) > 1:
        return None, AMBIGUOUS, [
            {'id': c.id, 'name': c.name, 'score': 100} for c in hits]

    # Nothing exact.  Offer near neighbours as candidates for the review
    # queue — as suggestions for a human, never as an automatic link.
    suggestions = []
    for other_key, rows in index.items():
        if not other_key:
            continue
        if other_key.startswith(key) or key.startswith(other_key):
            for c in rows:
                suggestions.append({'id': c.id, 'name': c.name, 'score': 80})
        elif len(key) > 6 and (key in other_key or other_key in key):
            for c in rows:
                suggestions.append({'id': c.id, 'name': c.name, 'score': 60})
        if len(suggestions) >= 5:
            break
    return None, NO_MATCH, suggestions[:5]
