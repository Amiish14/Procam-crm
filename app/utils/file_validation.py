"""Check that an uploaded file is what its name says, and safe to open.

An extension is a claim the uploader makes. A renamed executable called
quote.pdf passes an extension check; so does a 40 KB .xlsx that expands to
40 GB when a server library opens it. So before an upload is stored or
parsed:

  * its first bytes must match the family its extension belongs to
    (PDF, PNG, JPEG, GIF, TIFF, BMP, WebP, HEIC, Office Open XML / ZIP,
    legacy Office / Outlook .msg, or plain text);
  * a ZIP-based file (xlsx, docx, pptx, zip) is inspected without being
    extracted: entry count, total uncompressed size, per-entry compression
    ratio, and path traversal in entry names; Office files must look like
    Office files, and may not carry macros;
  * a text file (csv, txt, eml) may not contain NUL bytes.

validate(stream, ext) raises UploadRejected with a sentence the user can
act on; the stream is returned to position 0 either way.
"""
import io
import os
import zipfile


class UploadRejected(ValueError):
    pass


#: Limits for ZIP-based files. An honest spreadsheet of 15 MB expands to a
#: few hundred MB at most; a zip bomb is thousands of times its size.
MAX_ZIP_ENTRIES = 10_000
MAX_ZIP_UNCOMPRESSED = 300 * 1024 * 1024
MAX_ZIP_RATIO = 150

_SIGNATURES = {
    'pdf': (b'%PDF-',),
    'png': (b'\x89PNG\r\n\x1a\n',),
    'jpeg': (b'\xff\xd8\xff',),
    'gif': (b'GIF87a', b'GIF89a'),
    'tiff': (b'II*\x00', b'MM\x00*'),
    'bmp': (b'BM',),
    'zip': (b'PK\x03\x04', b'PK\x05\x06'),
    'ole': (b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1',),
}

#: extension → the byte families it may be
EXPECTED = {
    '.pdf': {'pdf'},
    '.png': {'png'},
    '.jpg': {'jpeg'}, '.jpeg': {'jpeg'},
    '.gif': {'gif'},
    '.tif': {'tiff'}, '.tiff': {'tiff'},
    '.bmp': {'bmp'},
    '.webp': {'webp'},
    '.heic': {'heic'}, '.heif': {'heic'},
    '.xlsx': {'ooxml'}, '.xlsm': {'ooxml'}, '.docx': {'ooxml'},
    '.pptx': {'ooxml'},
    '.zip': {'zip'},
    '.xls': {'ole'}, '.doc': {'ole'}, '.ppt': {'ole'}, '.msg': {'ole'},
    '.csv': {'text'}, '.txt': {'text'}, '.eml': {'text'},
}

_OOXML_MARKER = '[Content_Types].xml'
_MACRO_PARTS = ('vbaproject.bin', 'vbadata.xml')


def _family(head):
    for name, sigs in _SIGNATURES.items():
        if any(head.startswith(s) for s in sigs):
            return name
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    if head[4:8] == b'ftyp' and head[8:12] in (b'heic', b'heix', b'mif1',
                                                  b'msf1', b'hevc'):
        return 'heic'
    return None


def _check_zip(stream, *, office, allow_macros=False):
    try:
        zf = zipfile.ZipFile(stream)
    except zipfile.BadZipFile:
        raise UploadRejected('The file is damaged or not really a ZIP / '
                             'Office file.')
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_ZIP_ENTRIES:
            raise UploadRejected('The file contains too many parts to '
                                 'open safely.')
        total = 0
        names = set()
        for info in infos:
            name = info.filename.replace('\\', '/')
            names.add(name.lower())
            if name.startswith('/') or '..' in name.split('/'):
                raise UploadRejected('The archive contains unsafe file '
                                     'paths.')
            total += info.file_size
            if total > MAX_ZIP_UNCOMPRESSED:
                raise UploadRejected('The file expands to more than '
                                     f'{MAX_ZIP_UNCOMPRESSED // 2**20} MB '
                                     'when opened — refused.')
            if info.compress_size and \
                    info.file_size / info.compress_size > MAX_ZIP_RATIO \
                    and info.file_size > 1024 * 1024:
                raise UploadRejected('The file is compressed in a way used '
                                     'to crash servers — refused.')
        if office:
            if _OOXML_MARKER.lower() not in names:
                raise UploadRejected('This is not a valid Office file.')
            if not allow_macros and any(n.rsplit('/', 1)[-1] in _MACRO_PARTS
                                        for n in names):
                raise UploadRejected('Files with macros are not accepted. '
                                     'Save it as a plain .xlsx / .docx.')


def validate(stream, ext, *, allow_macros=False):
    """Raise UploadRejected unless the bytes match `ext` and are safe."""
    ext = (ext or '').lower()
    expected = EXPECTED.get(ext)
    try:
        stream.seek(0)
        head = stream.read(8192)
        stream.seek(0)
        if expected is None:
            # An extension this module does not know is the caller's
            # allow-list's business; nothing to compare against.
            return
        if not head:
            raise UploadRejected('The file is empty.')
        if expected == {'text'}:
            if b'\x00' in head:
                raise UploadRejected(f'This does not look like a {ext} '
                                     'text file.')
            return
        fam = _family(head)
        if expected == {'ooxml'}:
            if fam != 'zip':
                raise UploadRejected(f'This is not a real {ext} file — its '
                                     'contents do not match the name.')
            _check_zip(stream, office=True,
                       allow_macros=allow_macros or ext == '.xlsm')
            return
        if fam not in expected:
            raise UploadRejected(f'This is not a real {ext} file — its '
                                 'contents do not match the name.')
        if fam == 'zip':
            _check_zip(stream, office=False)
    finally:
        try:
            stream.seek(0)
        except Exception:
            pass


def validate_bytes(data, ext, **kw):
    return validate(io.BytesIO(data), ext, **kw)


def ext_of(filename):
    return os.path.splitext(filename or '')[1].lower()
