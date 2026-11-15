"""Reading the card — two passes, and the switch between them.

CRM-SEARCH-SCAN-01 Part 2.

    PASS 1  the image service, as decided on 15 Nov: it stays the
            primary reader. It reads a card at any angle, in two
            languages, and off a photograph of a glossy card under an
            office light, which nothing on the server does.

    PASS 2  the on-server text reader, BEHIND A SWITCH
            (`CARD_TEXT_READER=on`, needs the OCR binary installed).
            Off by default because it is not installed on the CRM box
            today and a scanner that fails when an optional reader is
            missing is worse than one that never had it.

Either pass alone produces a review form. Both passes produce a
review form AND an agreement, which is what the confidence score is
made of: a field two independent readers spelled identically is as
sure as this gets without a person.

NO VENDOR NAMES REACH A SCREEN

Everything user-facing here says "the image service" or "the
on-server reader". The names of what provides them are configuration,
not something a salesperson scanning a card should be shown — the
same rule as CRM-SEARCH-SCAN-01's "no AI/vendor names on any screen".

WHERE THE CARD GOES

The image service is the one already in the CRM and already approved
for this: the picture leaves the server to be read. Nothing else
here sends a card anywhere — the on-server reader runs on the box,
and the deterministic parser is pure text processing.
"""
from __future__ import annotations

import os

#: The environment switch, and what counts as on.
SWITCH = 'CARD_TEXT_READER'
_ON = ('1', 'on', 'yes', 'true', 'enabled')


def text_reader_wanted():
    return str(os.environ.get(SWITCH, '')).strip().lower() in _ON


def text_reader():
    """The on-server reader, or (None, why it is not there)."""
    if not text_reader_wanted():
        return None, f'the on-server reader is switched off ({SWITCH})'
    try:
        import pytesseract
    except Exception as exc:                              # noqa: BLE001
        return None, f'the on-server reader is not installed ({exc})'
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:                              # noqa: BLE001
        return None, f'the on-server reader cannot run ({exc})'
    return pytesseract, ''


def _card_likeness(text):
    """How much this reading looks like a business card.

    Used only to choose which way up the picture is, so it counts the
    things a card has and a desk does not: an @, a run of digits, a
    dot-something domain, and lines of a card's length.
    """
    body = str(text or '')
    if not body.strip():
        return 0
    score = 0
    score += 40 * body.count('@')
    score += 10 * sum(1 for ch in body if ch.isdigit()) // 4
    score += 25 * body.lower().count('.com')
    score += 15 * body.lower().count('www.')
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    score += sum(3 for l in lines if 3 <= len(l) <= 60)
    letters = sum(1 for ch in body if ch.isalpha())
    score += letters // 10
    return score


def read_image(png_bytes, *, try_orientations=True):
    """One picture → the text on it, and which way up it was.

    "Any orientation" is decided by READING, not by guessing from
    pixels: each quarter turn is read and the one that comes back
    looking most like a business card wins. A wrong guess here is a
    blank form; four reads of a cropped card are cheap.
    """
    reader, why = text_reader()
    if reader is None:
        return {'text': '', 'rotation': 0, 'error': why}
    try:
        import io
        from PIL import Image
        from app.services import card_image

        img = Image.open(io.BytesIO(png_bytes))
        img.load()
        tries = card_image.quarter_turns(img) if try_orientations \
            else [(0, img)]
        best, best_text, best_score = 0, '', -1
        for angle, turned in tries:
            try:
                text = reader.image_to_string(turned)
            except Exception:                             # noqa: BLE001
                continue
            found = _card_likeness(text)
            if found > best_score:
                best, best_text, best_score = angle, text, found
        return {'text': best_text, 'rotation': best, 'error': ''}
    except Exception as exc:                              # noqa: BLE001
        return {'text': '', 'rotation': 0,
                'error': f'the on-server reader failed ({exc})'}


def read_pages(pages):
    """Every prepared picture, read and joined front-first."""
    out, notes = [], []
    for n, png in enumerate(pages or []):
        found = read_image(png)
        if found.get('error'):
            notes.append(found['error'])
            break                 # the same reason will hold for page 2
        if found.get('text', '').strip():
            where = 'front' if n == 0 else ('back' if n == 1
                                            else f'page {n + 1}')
            if found.get('rotation'):
                notes.append(f'{where}: read it turned '
                             f'{found["rotation"]}°')
            out.append(found['text'])
    return '\n'.join(out), notes
