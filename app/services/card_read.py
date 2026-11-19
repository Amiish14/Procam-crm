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

#: Which languages. `eng` alone unless the box has more installed;
#: `osd` is the orientation model, which is not a language and is not
#: listed here.
LANGS = 'CARD_TEXT_READER_LANGS'
DEFAULT_LANGS = 'eng'

#: The page-segmentation mode. A business card is NOT a page of
#: running text, which is what the default (psm 3, "fully automatic")
#: assumes: it is a few short lines scattered over a small area, and
#: psm 3 on one is where a reader returns two words or nothing. 6 is
#: "one uniform block" and reads the usual stacked-lines card; 11 is
#: "sparse text", which is the one that saves a card with the name in
#: a corner and the numbers down the side. Both are tried, best kept.
CONFIG = 'CARD_TEXT_READER_CONFIG'
DEFAULT_CONFIG = '--oem 3 --psm 6'
SPARSE_CONFIG = '--oem 3 --psm 11'
#: CRM-SCAN-02: two more segmentations. 4 is "one column of text of
#: varying sizes" — the card with a big name and small lines under it;
#: 3 is the automatic page layout, which finds the columns of a
#: two-column card. Every one is kept as evidence, best one wins.
COLUMN_CONFIG = '--oem 3 --psm 4'
AUTO_CONFIG = '--oem 3 --psm 3'


def configs():
    return (config(), SPARSE_CONFIG, COLUMN_CONFIG, AUTO_CONFIG)


def blocks_from_data(data, width=0):
    """Block/column-aware text from the reader's word boxes.

    `data` is image_to_data(output_type=DICT). Words are grouped into
    the reader's blocks and lines; blocks are ordered COLUMN first
    (left edge clustered at 25% of the width), then top to bottom, so a
    two-column card is read down the left, then down the right, instead
    of across both. A blank line separates blocks.
    """
    words = []
    n = len((data or {}).get('text') or [])
    for i in range(n):
        text = str(data['text'][i] or '').strip()
        try:
            conf = float(data.get('conf', ['0'] * n)[i])
        except (TypeError, ValueError):
            conf = 0.0
        if not text or conf < 0:
            continue
        words.append({'b': data['block_num'][i], 'p': data['par_num'][i],
                      'l': data['line_num'][i], 'x': data['left'][i],
                      'y': data['top'][i], 'w': data['width'][i],
                      't': text})
    if not words:
        return ''
    width = width or max(w['x'] + w['w'] for w in words) or 1
    blocks = {}
    for w in words:
        blocks.setdefault(w['b'], []).append(w)
    ordered = []
    for b, ws in blocks.items():
        left = min(w['x'] for w in ws)
        top = min(w['y'] for w in ws)
        ordered.append((int(left / (width * 0.25)), top, b, ws))
    ordered.sort(key=lambda t: (t[0], t[1]))
    out = []
    for _col, _top, _b, ws in ordered:
        lines = {}
        for w in ws:
            lines.setdefault((w['p'], w['l']), []).append(w)
        for key in sorted(lines, key=lambda k: min(w['y'] for w in lines[k])):
            out.append(' '.join(w['t'] for w in sorted(lines[key],
                                                       key=lambda w: w['x'])))
        out.append('')
    return '\n'.join(out).strip()

#: Where the binary is, when it is not on PATH.
CMD = 'TESSERACT_CMD'

#: Below this, the reading is not worth keeping over another try.
WORTH_KEEPING = 30


def text_reader_wanted():
    return str(os.environ.get(SWITCH, '')).strip().lower() in _ON


def langs():
    return (os.environ.get(LANGS) or DEFAULT_LANGS).strip()


def config():
    return (os.environ.get(CONFIG) or DEFAULT_CONFIG).strip()


def text_reader():
    """The on-server reader, or (None, why it is not there)."""
    if not text_reader_wanted():
        return None, (f'the on-server reader is switched off '
                      f'({SWITCH} is not set to "on")')
    try:
        import pytesseract
    except Exception as exc:                              # noqa: BLE001
        return None, f'the on-server reader is not installed ({exc})'
    wanted = os.environ.get(CMD)
    if wanted:
        # A binary that is not on PATH is the commonest reason this
        # works in a shell and not under systemd, whose PATH is its
        # own.
        pytesseract.pytesseract.tesseract_cmd = wanted
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:                              # noqa: BLE001
        return None, f'the on-server reader cannot run ({exc})'
    return pytesseract, ''


def describe():
    """Everything about the on-server reader, for the debug command."""
    found = {'wanted': text_reader_wanted(), 'switch': SWITCH,
             'cmd': '', 'version': '', 'langs': langs(),
             'available_langs': [], 'config': config(),
             'sparse_config': SPARSE_CONFIG, 'error': ''}
    reader, why = text_reader()
    if reader is None:
        found['error'] = why
        return found
    try:
        found['cmd'] = str(reader.pytesseract.tesseract_cmd)
        found['version'] = str(reader.get_tesseract_version())
    except Exception as exc:                              # noqa: BLE001
        found['error'] = str(exc)
    try:
        found['available_langs'] = list(reader.get_languages(config=''))
    except Exception:                                     # noqa: BLE001
        pass
    return found


def _card_likeness(text):
    """How much this reading looks like a business card.

    Used to choose which way up the picture is and which segmentation
    read it better, so it counts the things a card has and a desk does
    not: an @, a run of digits, a dot-something domain, and lines of a
    card's length.
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


def for_reading(img):
    """The picture in the shape the reader wants it.

    Greyscale, because colour tells a text reader nothing and costs
    it time; and up to at least 1000 pixels across, because the
    reader wants roughly 300 dpi over a card and a picture scaled
    down for sending can fall under it.
    """
    out = img.convert('L')
    if out.size[0] < 1000:
        scale = 1000.0 / max(1, out.size[0])
        out = out.resize((int(out.size[0] * scale),
                          int(out.size[1] * scale)))
    return out


def _osd_rotation(reader, img):
    """Which way up, asked of the orientation model — one cheap call.

    Four reads of every card to find out which way up it is costs
    four times the wait on a phone. The orientation model answers
    that in one. When it cannot (too little text, no `osd` data), the
    caller falls back to reading each turn.
    """
    try:
        found = reader.image_to_osd(img)
    except Exception:                                     # noqa: BLE001
        return None
    for line in str(found or '').splitlines():
        if line.lower().startswith('rotate:'):
            try:
                return int(line.split(':', 1)[1].strip()) % 360
            except Exception:                             # noqa: BLE001
                return None
    return None


def _read_once(reader, img, *, how):
    return reader.image_to_string(img, lang=langs(), config=how)


def read_image(png_bytes, *, try_orientations=True):
    """One picture → the text on it, and which way up it was.

    Orientation from the orientation model where it answers, from
    reading each quarter turn where it does not. Then the two
    segmentations, best kept: a card is a few short lines scattered
    over a small area, and the reader's default assumes a page of
    running text.
    """
    reader, why = text_reader()
    if reader is None:
        return {'text': '', 'rotation': 0, 'error': why, 'ms': 0,
                'how': '', 'size': '', 'mode': ''}
    import time

    started = time.time()
    try:
        import io
        from PIL import Image
        from app.services import card_image

        img = Image.open(io.BytesIO(png_bytes))
        img.load()
        work = for_reading(img)
        shape = {'size': f'{work.size[0]}×{work.size[1]}',
                 'mode': work.mode}

        turn = _osd_rotation(reader, work) if try_orientations else 0
        tries = []
        if turn:
            tries.append((turn, work.rotate(-turn, expand=True,
                                            fillcolor=255)))
        tries.append((0, work))
        if try_orientations and turn is None:
            tries = [(angle, for_reading(one)) for angle, one
                     in card_image.quarter_turns(img)]

        best = {'text': '', 'rotation': 0, 'how': '', 'score': -1}
        readings = []
        for angle, candidate in tries:
            for how in configs():
                try:
                    text = _read_once(reader, candidate, how=how)
                except Exception as exc:                  # noqa: BLE001
                    return {'text': '', 'rotation': 0,
                            'error': f'the on-server reader failed '
                                     f'({type(exc).__name__}: {exc})',
                            'ms': int((time.time() - started) * 1000),
                            'how': how, **shape}
                score = _card_likeness(text)
                readings.append({'rotation': angle, 'how': how,
                                 'score': score, 'text': text})
                if score > best['score']:
                    best = {'text': text, 'rotation': angle, 'how': how,
                            'score': score}
                if score >= WORTH_KEEPING and how == config():
                    break          # the usual card, read the usual way
            if best['score'] >= WORTH_KEEPING:
                break
        # Block/column reading of the winning orientation, as one more
        # candidate and one more piece of evidence.
        try:
            upright = dict(tries).get(best['rotation'], work)
            data = reader.image_to_data(upright, lang=langs(),
                                        config=AUTO_CONFIG,
                                        output_type=reader.Output.DICT)
            text = blocks_from_data(data, upright.size[0])
            if text.strip():
                score = _card_likeness(text)
                readings.append({'rotation': best['rotation'],
                                 'how': 'blocks', 'score': score,
                                 'text': text})
                if score > best['score']:
                    best = {'text': text, 'rotation': best['rotation'],
                            'how': 'blocks', 'score': score}
        except Exception:                                 # noqa: BLE001
            pass
        return {'text': best['text'], 'rotation': best['rotation'],
                'error': '', 'ms': int((time.time() - started) * 1000),
                'how': best['how'], 'score': best['score'],
                'readings': readings, **shape}
    except Exception as exc:                              # noqa: BLE001
        return {'text': '', 'rotation': 0,
                'error': f'the on-server reader failed '
                         f'({type(exc).__name__}: {exc})',
                'ms': int((time.time() - started) * 1000),
                'how': '', 'size': '', 'mode': ''}


def read_pages(pages):
    """Every prepared picture, read and joined front-first."""
    text, notes, _evidence = read_pages_with_evidence(pages)
    return text, notes


def read_pages_with_evidence(pages):
    """read_pages, plus every reading of every side for the accuracy
    page and the candidate scorer: [{side, rotation, how, score, text}]."""
    out, notes, evidence = [], [], []
    for n, png in enumerate(pages or []):
        found = read_image(png)
        where = 'front' if n == 0 else ('back' if n == 1
                                        else f'page {n + 1}')
        for r in found.get('readings') or []:
            evidence.append(dict(r, side=where))
        if found.get('error'):
            notes.append(f'{where}: {found["error"]}')
            break                 # the same reason will hold for page 2
        if found.get('text', '').strip():
            if found.get('rotation'):
                notes.append(f'{where}: read it turned '
                             f'{found["rotation"]}°')
            out.append(found['text'])
        else:
            notes.append(f'{where}: the on-server reader found no text '
                         f'on it ({found.get("ms", 0)} ms)')
    return '\n'.join(out), notes, evidence
