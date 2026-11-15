"""How much of a business card the scanner gets right — before, after.

CRM-SEARCH-SCAN-01 Part 2: "a test set of at least 10 varied cards
with field accuracy before and after".

    .venv/bin/python scripts/2026_11_16_card_accuracy.py
    .venv/bin/python scripts/2026_11_16_card_accuracy.py --csv
    .venv/bin/python scripts/2026_11_16_card_accuracy.py --cards /tmp/cards
    CARD_TEXT_READER=on .venv/bin/python scripts/2026_11_16_card_accuracy.py

READ-ONLY. It writes nothing to the database, ever.

WHAT "BEFORE" AND "AFTER" MEAN HERE

    before   the scanner as it stood this morning: the text, the
             deterministic parser, `to_contact_fields`. No picture
             preparation, no E.164, no name split, no address split.
    after    the same cards through Part 2.

Both halves read the SAME input, so the difference is the change and
nothing else.

THE PART THIS MACHINE CANNOT MEASURE, AND SAYS SO

Accuracy has two halves: **reading** the card (pixels → text) and
**understanding** it (text → fields). This measures the second half
exactly, because the specimens' text is known.

The first half needs a reader. With `ANTHROPIC_API_KEY` set, or the
on-server reader switched on with the binary installed, the pictures
are read and the report says so at the top. With neither — which is
this laptop, and the CRM box today — the printed text stands in for
the reading and the report says **READING NOT MEASURED** in as many
words. A number that silently assumed a perfect reader would be a
lie, and the ten real cards after this ships are what settles it.

The picture preparation is measured either way, because what it does
is visible without reading anything: how much skew it took out, how
much of the frame it cropped away, what it did to the contrast.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from app import app                                       # noqa: E402

DEFAULT_CSV = os.path.join(_ROOT, 'data', 'card_accuracy_16NOV.csv')


def _rule(n=76):
    print('─' * n)


def _flat(value):
    return re.sub(r'[^a-z0-9]', '', str(value or '').lower())


def _same(field, got, want):
    """Right, for this field.

    A phone is right when its E.164 is right, whatever it was printed
    as. Everything else is compared flattened, because "Pvt. Ltd." and
    "Pvt Ltd" are the same company and a scanner marked wrong for a
    full stop tells you nothing.
    """
    if field in ('mobile', 'telephone'):
        from app.services import card_fields
        a = card_fields.to_e164(got)[0] or _flat(got)
        b = card_fields.to_e164(want)[0] or _flat(want)
        return a == b
    if field == 'linkedin_url':
        return _flat(got).endswith(_flat(want)) if want else not got
    return _flat(got) == _flat(want)


#: What the scanner had BEFORE Part 2 — the fields the old
#: deterministic path produced. Scored separately from the ones Part 2
#: added, because "the old scanner got 0/11 on postal_code" is not a
#: comparison, it is a field that did not exist. The honest number is
#: the first block; the second says what is new.
OLD_FIELDS = ('name', 'designation', 'company', 'email', 'mobile',
              'telephone', 'website')


# ── the two pipelines ───────────────────────────────────────────────
def before(text):
    """The scanner as it stood before Part 2."""
    from app.services import card_parse

    return card_parse.to_contact_fields(card_parse.parse(text))


def after(specimen, text, *, read_pictures):
    """Part 2: the picture prepared, then read, then understood.

    Returns (fields, why it failed). The second value is the whole
    point of the 15 Nov fix: this returned only the fields, so a run
    where the reader failed on all eleven cards printed eleven rows
    of zeros and no reason at all.
    """
    from app.services import card_samples, business_card_ocr

    front, front_mime = card_samples.as_bytes(specimen)
    sides = []
    if specimen.get('back'):
        sides.append(card_samples.as_bytes(specimen, side='back'))
    if read_pictures:
        found = business_card_ocr.extract_business_card(
            front, mime=front_mime, sides=sides)
        if found.get('error'):
            # The reading failed. Rather than eleven rows of zeros,
            # fall back to the card's text so the UNDERSTANDING half
            # is still measured — and keep the reason, which is what
            # the row prints.
            again = business_card_ocr.extract_business_card(
                b'', mime='image/png', card_text=text, preprocess=False)
            return ((again.get('extracted') or {}), found['error'])
    else:
        # No reader on this machine: the card's own text stands in for
        # the reading, and the report says so.
        found = business_card_ocr.extract_business_card(
            b'', mime='image/png', card_text=text, preprocess=False)
    return (found.get('extracted') or {}), (found.get('error') or '')


def prepared(specimen):
    """What the picture preparation did, in its own words."""
    from app.services import card_image, card_samples

    front, front_mime = card_samples.as_bytes(specimen)
    sides = []
    if specimen.get('back'):
        sides.append(card_samples.as_bytes(specimen, side='back'))
    try:
        ready = card_image.prepare(front, front_mime, sides=sides)
        return ready.pages, ready.notes
    except Exception as exc:                              # noqa: BLE001
        return [], [f'preparation failed: {exc}']


# ── the report ──────────────────────────────────────────────────────
def readers():
    """Which readers this machine has, in words for the report."""
    from app.services import card_read

    found = []
    if os.environ.get('ANTHROPIC_API_KEY'):
        found.append('the image service')
    reader, why = card_read.text_reader()
    if reader is not None:
        found.append('the on-server reader')
    else:
        found.append(f'no on-server reader — {why}')
    return found


def run(*, write_cards=None, as_csv=None):
    from app.services import card_samples

    have_service = bool(os.environ.get('ANTHROPIC_API_KEY'))
    from app.services import card_read
    have_local = card_read.text_reader()[0] is not None
    read_pictures = have_service or have_local

    print('\nbusiness card accuracy — READ ONLY, nothing is written.')
    _rule()
    for line in readers():
        print(f'  · {line}')
    if read_pictures:
        print('\n  A reader is configured, so the pictures are sent to '
              'it. Whether')
        print('  it ANSWERED is reported per card below and again at '
              'the end —')
        print('  a configured reader and a working one are not the '
              'same thing.')
    else:
        print('\n  !! READING NOT MEASURED. No reader on this machine, so '
              'the card\'s')
        print('     printed text stands in for what a reader would '
              'return. What is')
        print('     measured below is everything AFTER the text: the '
              'parsing, the')
        print('     E.164, the name and address split. The reading half '
              'is settled')
        print('     by scanning ten real cards on a box that has a '
              'reader.')

    rows, failures = [], []
    totals = {'before': 0, 'after': 0, 'fields': 0,
              'added': 0, 'added_right': 0}
    print(f'\n══ {len(card_samples.SPECIMENS)} specimen card(s) ══')
    for specimen in card_samples.SPECIMENS:
        text = card_samples.both_sides_text(specimen)
        was = before(text)
        now, failed = after(specimen, text, read_pictures=read_pictures)
        # Only when a picture was actually sent. Without a reader no
        # picture goes anywhere on purpose, and the top of the report
        # already says so — repeating it eleven times as a failure
        # would bury the case this block exists for.
        if failed and read_pictures:
            failures.append((specimen['key'], failed))
        pages, notes = prepared(specimen)

        scored = [f for f in card_samples.SCORED_FIELDS
                  if specimen['truth'].get(f)]
        shared = [f for f in scored if f in OLD_FIELDS]
        added = [f for f in scored if f not in OLD_FIELDS]
        right_before = [f for f in shared
                        if _same(f, was.get(f), specimen['truth'][f])]
        right_after = [f for f in shared
                       if _same(f, now.get(f), specimen['truth'][f])]
        right_added = [f for f in added
                       if _same(f, now.get(f), specimen['truth'][f])]
        totals['before'] += len(right_before)
        totals['after'] += len(right_after)
        totals['fields'] += len(shared)
        totals['added_right'] += len(right_added)
        totals['added'] += len(added)
        missed = [f for f in scored
                  if not _same(f, now.get(f), specimen['truth'][f])]

        print(f'\n  {specimen["key"]} — {specimen["what"]}')
        if failed and read_pictures:
            print(f'    !! THE READER FAILED: {failed}')
        print(f'    the fields it always had: before '
              f'{len(right_before)}/{len(shared)}, after '
              f'{len(right_after)}/{len(shared)}')
        print(f'    the fields Part 2 added:  {len(right_added)}/'
              f'{len(added)}   ({len(pages)} picture(s) prepared)')
        for note in notes[:4]:
            print(f'      · {note}')
        if missed:
            for field in missed:
                print(f'      ✗ {field}: read {now.get(field)!r}, '
                      f'card says {specimen["truth"][field]!r}')
        rows.append({
            'card': specimen['key'], 'what': specimen['what'],
            'fields': len(shared), 'before': len(right_before),
            'after': len(right_after),
            'new_fields': len(added), 'new_right': len(right_added),
            'missed': ' '.join(missed),
            'preparation': '; '.join(notes),
        })
        if write_cards:
            _write_card(specimen, write_cards)

    span = totals['fields'] or 1
    added_span = totals['added'] or 1
    print('\n══ all cards ══')
    print('  the fields the scanner always had:')
    print(f'    before  {totals["before"]:>3}/{span}  '
          f'({100.0 * totals["before"] / span:.1f}%)')
    print(f'    after   {totals["after"]:>3}/{span}  '
          f'({100.0 * totals["after"] / span:.1f}%)')
    gain = totals['after'] - totals['before']
    print(f'    {gain:+d} field(s)')
    print('\n  the fields Part 2 added (first name, last name, '
          'department,')
    print('  city, state, country, postal code, LinkedIn): '
          f'{totals["added_right"]}/{added_span}'
          f'  ({100.0 * totals["added_right"] / added_span:.1f}%)')
    print('    There is no "before" for these: the old scanner had '
          'nowhere to put them.')
    if not read_pictures:
        print('\n  Reading not measured — see the top of this report.')
    if failures:
        # The 15 Nov fault said "READING IS MEASURED" at the top and
        # printed zeros underneath. A report that measured nothing has
        # to say so at the BOTTOM too, where the number is.
        print(f'\n  !! THE READER FAILED ON {len(failures)} OF '
              f'{len(card_samples.SPECIMENS)} CARD(S).')
        # Grouped by the KIND of failure: every answer carries its own
        # request id, so the whole string is unique and printing one
        # per card buries the one fact that matters.
        seen = {}
        for key, why in failures:
            head = why.split(' - ')[0].split(':')[0].strip()
            seen.setdefault(head, [why, []])[1].append(key)
        for head, (why, keys) in seen.items():
            print(f'     {len(keys)} card(s): {why}')
        print('\n     The numbers above ARE still a measurement of the '
              'parsing —')
        print('     the card\'s text stood in for the reading — but NOT '
              'of the')
        print('     reader, which answered nothing.')
        print('     These numbers are NOT a measurement of the reader. '
              'Run:')
        print('       .venv/bin/python '
              'scripts/2026_11_16_card_debug.py')
    if as_csv:
        _write_csv(rows, as_csv)
    return rows, totals


def _write_card(specimen, where):
    from app.services import card_samples

    os.makedirs(where, exist_ok=True)
    for side in ('front', 'back'):
        if side == 'back' and not specimen.get('back'):
            continue
        data, mime = card_samples.as_bytes(specimen, side=side)
        ext = '.pdf' if mime.endswith('pdf') else '.png'
        path = os.path.join(where, f'{specimen["key"]}_{side}{ext}')
        with open(path, 'wb') as fh:
            fh.write(data)


def _write_csv(rows, path):
    buf = io.StringIO()
    out = csv.DictWriter(buf, fieldnames=['card', 'what', 'fields',
                                          'before', 'after',
                                          'new_fields', 'new_right',
                                          'missed', 'preparation'])
    out.writeheader()
    for row in rows:
        out.writerow(row)
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', newline='') as fh:
        fh.write(buf.getvalue())
    print(f'\nCSV: {path}')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--csv', nargs='?', const=DEFAULT_CSV, default=None,
                    metavar='PATH')
    ap.add_argument('--cards', default=None, metavar='DIR',
                    help='also write the specimen cards out as files')
    args = ap.parse_args(argv)
    with app.app_context():
        run(write_cards=args.cards, as_csv=args.csv)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
