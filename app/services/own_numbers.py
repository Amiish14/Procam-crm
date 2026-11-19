"""Phone numbers that are ours, so the ingest stops filing them as customers'.

UAT 19 Nov, shared-phone follow-up. `+91-7738155449` sits on contacts at
two different companies, and 24 numbers are shared like that. A number
in a Procam signature, captured off a forwarded enquiry, becomes the
"customer's" phone on whatever contact the ingest was building -- and
then it is on two customers, and neither of them answers it.

HOW THIS LIST IS BUILT, AND WHY IT IS A FILE

`data/own_numbers.txt`, beside `blocked_senders.txt` and
`portal_senders.txt` and read the same way: one number per line, `#`
for a comment, re-read when the file changes so adding one needs no
deploy. It is written by
`scripts/2026_11_19_shared_phone_evidence.py --apply`, which puts a
number here only when the evidence says OURS -- in a Procam signature
or on a Procam user's record, and never seen coming from the customer's
own domain.

THE LAST TEN DIGITS ARE THE NUMBER

`+91-7738155449`, `07738155449` and `7738155449` are one phone. Every
comparison is on the last ten digits, which is what
`people_search._digits` already does for the search and the duplicate
check, so the three places agree about what "the same number" means.

WHAT THIS DOES NOT DO

It does not touch a Procam user's own record -- that is where the
number belongs. It only stops a number being written onto a CUSTOMER,
and it only ever answers a question: the removal of what is already
filed is a separate, audited, undoable script.
"""
import os
import re
import threading

PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), 'data', 'own_numbers.txt')

_lock = threading.Lock()
_cache = {'at': None, 'numbers': frozenset(), 'entries': {}}


def digits(value):
    """The last ten digits, which is what makes two numbers one."""
    said = re.sub(r'\D', '', str(value or ''))
    return said[-10:] if len(said) >= 10 else ''


def _read():
    """{number: the note beside it}. The note says who decided."""
    out = {}
    try:
        with open(PATH, encoding='utf-8') as fh:
            for line in fh:
                body, _, note = line.partition('#')
                said = digits(body)
                if said:
                    out[said] = note.strip()
    except FileNotFoundError:
        pass
    except Exception:                                      # noqa: BLE001
        # A list that cannot be read must not stop an ingest. It fails
        # OPEN on purpose: the worst case is the fault this file exists
        # to fix, and the worst case of failing closed is an enquiry
        # with no phone on it at all.
        return {}
    return out


def entries():
    """{number: note}, re-read when the file has changed underneath us.

    The note is what a screen or a report shows as the REASON -- "marked
    by DIR12010", "batch PHONE-20261119-1". A number with no note is
    still ours; it just cannot say who said so.
    """
    try:
        stamp = os.path.getmtime(PATH)
    except OSError:
        stamp = None
    with _lock:
        # `.get`, not `[...]`: a cache dict handed to us without every
        # key -- a test's monkeypatch, an older copy of this module --
        # must re-read the file, not raise. A rights or ingest path
        # that fails on its own cache is worse than a slow one.
        if _cache.get('at') != stamp or 'entries' not in _cache:
            _cache['entries'] = _read()
            _cache['numbers'] = frozenset(_cache['entries'])
            _cache['at'] = stamp
        return dict(_cache.get('entries') or {})


def numbers():
    """Just the numbers."""
    entries()
    with _lock:
        return _cache.get('numbers') or frozenset()


def note_for(value):
    """Why this number is ours, in the words of whoever marked it."""
    said = digits(value)
    return entries().get(said, '') if said else ''


def is_ours(value):
    """Is this one of our own numbers?"""
    said = digits(value)
    return bool(said) and said in numbers()


def add(values, *, note=''):
    """Append numbers to the list, keeping what is already there.

    Returns the ones actually added. Used only by the evidence script's
    `--apply`; nothing in the application writes this file.
    """
    held = set(numbers())
    fresh = []
    for value in values:
        said = digits(value)
        if said and said not in held:
            held.add(said)
            fresh.append(said)
    if not fresh:
        return []
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    new = not os.path.exists(PATH)
    with open(PATH, 'a', encoding='utf-8') as fh:
        if new:
            fh.write('# Procam numbers the mail ingest must not file as a\n'
                     '# customer\'s. Written by\n'
                     '# scripts/2026_11_19_shared_phone_evidence.py '
                     '--apply.\n'
                     '# One number per line; the last ten digits are what\n'
                     '# is compared.\n')
        for said in fresh:
            fh.write(f'{said}' + (f'   # {note}' if note else '') + '\n')
    with _lock:
        _cache['at'] = None            # forget, so the next read is fresh
    return fresh


def remove(values):
    """Take numbers back out of the list. The undo half of `add`."""
    want = {digits(v) for v in values} - {''}
    if not want or not os.path.exists(PATH):
        return []
    kept, dropped = [], []
    with open(PATH, encoding='utf-8') as fh:
        for line in fh:
            said = digits(line.split('#', 1)[0])
            if said and said in want:
                dropped.append(said)
                continue
            kept.append(line)
    with open(PATH, 'w', encoding='utf-8') as fh:
        fh.writelines(kept)
    with _lock:
        _cache['at'] = None
    return dropped
