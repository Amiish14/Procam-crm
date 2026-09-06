"""Upload helper for CRM (Phase 6+).

Adapted from Procam-lr-main/app/utils/uploads.py.  Provides a single,
hardened `save_upload()` helper for the RFQ / Quote / Handover modules.

Guarantees:
    * Extension deny-list (never accept executable / script types)
    * Optional allow-list per call site
    * Filename sanitisation (basename + secure_filename + length cap)
    * Per-file size cap (default 20 MB)
    * Random-prefixed stored filename so concurrent uploads never collide
    * Safe MIME + nosniff on download via `send_safe_download()`
"""
from __future__ import annotations

import os
import re
import uuid

from flask import abort, current_app, send_file
from werkzeug.utils import secure_filename


_DENIED = {
    '.svg', '.html', '.htm', '.xhtml', '.js', '.mjs', '.hta',
    '.phtml', '.php', '.pht', '.exe', '.msi', '.bat', '.cmd',
    '.ps1', '.sh', '.jar', '.docm', '.xlsm', '.pptm',
    '.xml', '.xsl',
}

_DEFAULT_ALLOWED = {
    '.pdf', '.png', '.jpg', '.jpeg', '.gif', '.tiff', '.bmp',
    '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
    '.txt', '.csv', '.zip', '.msg', '.eml',
}


def _upload_root():
    return (
        current_app.config.get('UPLOAD_ROOT')
        or os.environ.get('CRM_UPLOAD_ROOT')
        or os.path.join(current_app.instance_path, 'uploads')
    )


def save_upload(file_storage, subdir, *,
                allowed_exts=None,
                max_bytes: int = 20 * 1024 * 1024):
    """Persist an uploaded FileStorage to disk. Returns (path, safe_name)."""
    if not file_storage or not file_storage.filename:
        abort(400, 'No file uploaded')

    raw = os.path.basename(file_storage.filename or '')
    safe = secure_filename(raw)[:180] or 'upload'
    ext = os.path.splitext(safe)[1].lower()

    if ext in _DENIED:
        abort(400, f'File type {ext} is not permitted')
    allow = allowed_exts if allowed_exts is not None else _DEFAULT_ALLOWED
    if allow and ext not in allow:
        abort(400, f'File type {ext} not allowed for this upload')

    try:
        file_storage.stream.seek(0, os.SEEK_END)
        size = file_storage.stream.tell()
        file_storage.stream.seek(0)
    except Exception:
        size = 0
    if size and size > max_bytes:
        abort(400, f'File exceeds {max_bytes // (1024 * 1024)} MB limit')

    root = _upload_root()
    dest_dir = os.path.join(root, re.sub(r'[^\w\-]', '_', subdir))
    os.makedirs(dest_dir, exist_ok=True)

    stored_name = f'{uuid.uuid4().hex[:12]}_{safe}'
    dest_path = os.path.join(dest_dir, stored_name)
    file_storage.save(dest_path)
    return dest_path, safe


def send_safe_download(path, display_filename, *, force_download=True):
    resp = send_file(
        path,
        as_attachment=force_download,
        download_name=display_filename,
        mimetype='application/octet-stream',
    )
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Content-Security-Policy'] = "sandbox; default-src 'none'"
    resp.headers['Cache-Control'] = 'no-store'
    return resp
