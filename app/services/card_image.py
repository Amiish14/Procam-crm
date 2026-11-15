"""Make a photograph of a business card readable before anything reads it.

CRM-SEARCH-SCAN-01 Part 2. A card is photographed on a desk, at an
angle, under an office light, often upside down, sometimes as a PDF
somebody exported from their phone. The reader — whichever one — is
given a straight, cropped, evenly lit picture instead of that.

    prepare(data, mime) -> Prepared(pages=[PNG bytes], notes=[...])

WHAT IT DOES, AND WHY EACH STEP IS THERE

  * **EXIF rotation.** A phone stores the picture the way the sensor
    saw it and the orientation in a tag. Everything that is not a
    browser ignores the tag, so the card arrives on its side.
  * **Deskew.** The card is never square to the lens. Text lines at
    three degrees cost a text reader whole lines; they cost a vision
    reader accuracy on the small print, which is where the email and
    the mobile number live.
  * **Crop to the card.** A card is a third of the frame. Cropping to
    it is most of the resolution back, and it stops the reader from
    describing the desk.
  * **Contrast.** A grey photograph of a white card is a grey card.
  * **Downscale.** 12-megapixel phone photographs cost time and money
    and carry no more text than 1,600 pixels across.

NO NEW SYSTEM DEPENDENCIES, ON PURPOSE

Pillow only — already installed and already a dependency of the CRM.
No OpenCV, no numpy, no poppler. The deskew is a projection-profile
search done with `resize((1, h))`, which asks Pillow for the average
of every row and is the same measurement OpenCV would make. The PDF
path prefers PyMuPDF when the server has it, falls back to the images
embedded in the PDF (which is what a phone "export to PDF" actually
contains), and falls back again to the PDF's own text — a card
exported from a signature block has no picture in it at all, and its
text is better than nothing to read.

Every step is a **try**: if a stage fails the picture passes through
unchanged with a note, because a card that was not deskewed still
reads, and a scanner that refuses the upload does not.
"""
from __future__ import annotations

import io
import os

#: The longest edge we keep. A card is 85mm wide; 1600px across it is
#: about 480 dpi, which is far past what any reader uses.
MAX_EDGE = 1600

#: How far off square we will look for. Beyond this it is not a skewed
#: photograph, it is a photograph of something else.
MAX_SKEW = 8.0

#: Below this the crop is refused: it found a logo, not the card.
MIN_CROP_AREA = 0.20

#: Above this there was nothing to crop and the "card" is the frame.
MAX_CROP_AREA = 0.985

#: How many pages of a PDF are a business card. Front and back.
MAX_PDF_PAGES = 4


class Prepared:
    """What the reader is given, and what was done to get there."""

    def __init__(self, pages=None, notes=None, text='', error=''):
        self.pages = pages or []          # [bytes] — PNG, in order
        self.notes = notes or []          # what each step did
        self.text = text or ''            # text found without a picture
        self.error = error or ''

    def __bool__(self):
        return bool(self.pages or self.text)

    def to_dict(self):
        return {'pages': len(self.pages), 'notes': list(self.notes),
                'text': self.text, 'error': self.error}


def _pillow():
    from PIL import Image, ImageChops, ImageEnhance, ImageOps, ImageStat
    return Image, ImageChops, ImageEnhance, ImageOps, ImageStat


# ── the steps ───────────────────────────────────────────────────────
def upright(img, notes):
    """The rotation the camera recorded, applied."""
    _Image, _Chops, _Enh, ImageOps, _Stat = _pillow()
    try:
        out = ImageOps.exif_transpose(img)
        if out is not img and out.size != img.size:
            notes.append('turned the right way up from the camera tag')
        return out
    except Exception:                                     # noqa: BLE001
        return img


def _row_variance(img):
    """How strongly this picture is made of horizontal lines.

    `resize((1, h))` is Pillow averaging every row to one pixel: the
    projection profile, without numpy. Text lines make the profile
    swing between dark and light; the same text at an angle smears it
    flat. The angle with the biggest swing is the straight one.
    """
    small = img.convert('L').resize((160, 160))
    column = small.resize((1, 160))
    getter = getattr(column, 'get_flattened_data', None)
    values = list(getter() if getter else column.getdata())
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    return sum((v - mean) ** 2 for v in values) / len(values)


def deskew(img, notes, limit=MAX_SKEW):
    """Straighten the text. Coarse pass, then fine, around the best."""
    try:
        # The angle is searched on a THUMBNAIL and applied to the
        # picture. Searching on the full image meant thirty-two
        # rotations of twelve megapixels for an answer that is one
        # number — seconds of a phone's upload spent proving what 400
        # pixels already show.
        probe = img.convert('L')
        probe.thumbnail((400, 400))
        best, best_score = 0.0, _row_variance(probe)
        for angle in [a / 2.0 for a in range(-int(limit * 2),
                                             int(limit * 2) + 1)]:
            if angle == 0:
                continue
            turned = probe.rotate(angle, expand=False, fillcolor='white')
            score = _row_variance(turned)
            if score > best_score:
                best, best_score = angle, score
        # A straight card must not be turned. The profile of a washed
        # out photograph wanders, and an unguarded search answers with
        # the limit angle every time — which is a straight card rotated
        # eight degrees. The win has to be a real one.
        if abs(best) < 0.5 or best_score < _row_variance(probe) * 1.15:
            return img
        notes.append(f'straightened by {best:+.1f}°')
        from PIL import Image as _I
        # BICUBIC: `rotate` refuses LANCZOS, and NEAREST would put
        # stair-steps on exactly the small print this exists to save.
        return img.rotate(best, expand=True, fillcolor='white',
                          resample=_I.BICUBIC)
    except Exception as exc:                              # noqa: BLE001
        notes.append(f'could not straighten it ({exc})')
        return img


def crop_to_card(img, notes):
    """Cut the desk away.

    The border colour is the desk; anything that differs from it by
    more than a little is the card. `getbbox` on that difference is
    the card's box. Refused when the box is implausible — a crop that
    is wrong throws away the half of the card with the phone number
    on it, which is worse than not cropping.
    """
    _Image, ImageChops, _Enh, ImageOps, _Stat = _pillow()
    try:
        grey = img.convert('L')
        w, h = grey.size
        edge = [grey.getpixel(p) for p in
                ((1, 1), (w - 2, 1), (1, h - 2), (w - 2, h - 2),
                 (w // 2, 1), (w // 2, h - 2))]
        background = sum(edge) / len(edge)
        from PIL import Image as _I
        flat = _I.new('L', grey.size, int(background))
        diff = ImageChops.difference(grey, flat)
        mask = diff.point(lambda v: 255 if v > 28 else 0)
        box = mask.getbbox()
        if not box:
            return img
        share = ((box[2] - box[0]) * (box[3] - box[1])) / float(w * h or 1)
        if share < MIN_CROP_AREA or share > MAX_CROP_AREA:
            return img
        pad = max(2, int(min(w, h) * 0.01))
        box = (max(0, box[0] - pad), max(0, box[1] - pad),
               min(w, box[2] + pad), min(h, box[3] + pad))
        notes.append(f'cropped to the card ({int(share * 100)}% of the frame)')
        return img.crop(box)
    except Exception as exc:                              # noqa: BLE001
        notes.append(f'could not crop it ({exc})')
        return img


def lift_contrast(img, notes):
    """A grey photograph of a white card made white again."""
    _Image, _Chops, ImageEnhance, ImageOps, ImageStat = _pillow()
    try:
        work = img.convert('RGB')
        before = ImageStat.Stat(work.convert('L')).stddev[0]
        work = ImageOps.autocontrast(work, cutoff=1)
        after = ImageStat.Stat(work.convert('L')).stddev[0]
        if after > before + 1:
            notes.append('evened out the lighting')
        if after < 45:
            work = ImageEnhance.Contrast(work).enhance(1.35)
            notes.append('lifted the contrast')
        return work
    except Exception as exc:                              # noqa: BLE001
        notes.append(f'could not even out the lighting ({exc})')
        return img


def fit(img, notes, max_edge=MAX_EDGE):
    try:
        w, h = img.size
        if max(w, h) <= max_edge:
            return img
        scale = max_edge / float(max(w, h))
        out = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        notes.append(f'scaled down to {out.size[0]}×{out.size[1]}')
        return out
    except Exception:                                     # noqa: BLE001
        return img


def quarter_turns(img):
    """The same card the four ways up it could have been photographed.

    "Any orientation" cannot be decided from pixels alone with any
    confidence, so it is decided by READING: the local text reader is
    given each turn and the one that yields the most card-like text
    wins (`card_read.best_orientation`). The vision reader needs none
    of this — it reads a card upside down — so this is only ever used
    on the fallback path.
    """
    return [(0, img), (90, img.rotate(90, expand=True)),
            (180, img.rotate(180, expand=True)),
            (270, img.rotate(270, expand=True))]


def _png(img):
    buf = io.BytesIO()
    img.convert('RGB').save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def clean(img, notes):
    """Every step, in the order that makes each one easier."""
    img = upright(img, notes)
    # Contrast FIRST: the crop finds the card by how much it differs
    # from the desk, and on a washed out photograph that difference is
    # a few shades. Cropping before evening out the light found the
    # block of text and threw the rest of the card away.
    img = lift_contrast(img, notes)
    img = crop_to_card(img, notes)          # before deskew: less to turn
    img = deskew(img, notes)
    img = crop_to_card(img, notes)          # after: the turn left white
    img = fit(img, notes)
    return img


# ── PDFs ────────────────────────────────────────────────────────────
def _pdf_pages(data, notes):
    """Pictures out of a PDF, by whichever means this server has.

    Three readings, best first. A phone's "export to PDF" is a JPEG in
    a wrapper, so the embedded-image path gets the original photograph
    rather than a re-render of it; a card pasted out of a signature
    block has no picture at all and only its text is worth having.
    """
    pages = []
    try:
        import fitz                                        # PyMuPDF
        doc = fitz.open(stream=data, filetype='pdf')
        for page in list(doc)[:MAX_PDF_PAGES]:
            pix = page.get_pixmap(dpi=300)
            pages.append(pix.tobytes('png'))
        if pages:
            notes.append(f'read {len(pages)} page(s) of the PDF')
            return pages, ''
    except Exception:                                     # noqa: BLE001
        pass
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        for page in reader.pages[:MAX_PDF_PAGES]:
            for image in (getattr(page, 'images', None) or [])[:2]:
                if image.data and len(image.data) > 4096:
                    pages.append(image.data)
        if pages:
            notes.append(f'took {len(pages)} picture(s) out of the PDF')
            return pages, ''
        text = '\n'.join((p.extract_text() or '')
                         for p in reader.pages[:MAX_PDF_PAGES])
        if text.strip():
            notes.append('the PDF holds no picture, only text — read that')
            return [], text
    except Exception as exc:                              # noqa: BLE001
        return [], ''
    return [], ''


# ── the way in ──────────────────────────────────────────────────────
def prepare(data, mime='image/jpeg', *, sides=None):
    """Bytes of a card (or several) → pictures a reader can work with.

    `sides` is an optional list of (bytes, mime) for the back and any
    further pictures of the same card; they are prepared the same way
    and returned in order, front first.
    """
    notes, pages, text = [], [], ''
    lots = [(data, mime)] + list(sides or [])
    for n, (blob, kind) in enumerate(lots):
        if not blob:
            continue
        where = 'front' if n == 0 else ('back' if n == 1 else f'page {n + 1}')
        raw = [blob]
        if (kind or '').lower().endswith('pdf') or \
                (blob[:5] == b'%PDF-'):
            found, found_text = _pdf_pages(blob, notes)
            if found_text and not found:
                text = (text + '\n' + found_text).strip()
                continue
            raw = found or []
        for one in raw:
            try:
                Image, _C, _E, _O, _S = _pillow()
                img = Image.open(io.BytesIO(one))
                img.load()
            except Exception as exc:                      # noqa: BLE001
                notes.append(f'the {where} is not a picture this can open '
                             f'({exc})')
                continue
            step_notes = []
            cleaned = clean(img, step_notes)
            notes.extend(f'{where}: {s}' for s in step_notes)
            try:
                pages.append(_png(cleaned))
            except Exception as exc:                      # noqa: BLE001
                notes.append(f'{where}: could not re-save it ({exc})')
    return Prepared(pages=pages, notes=notes, text=text)


def prepare_file(path, *, sides=None):
    """The same, from paths on disk."""
    import mimetypes

    def read(p):
        with open(p, 'rb') as fh:
            return fh.read(), (mimetypes.guess_type(p)[0] or 'image/jpeg')

    data, mime = read(path)
    more = []
    for other in (sides or []):
        if other and os.path.isfile(other):
            more.append(read(other))
    return prepare(data, mime, sides=more)
