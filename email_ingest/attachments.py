"""Fetch attachments from lead emails and save them to disk.

Called from the pipeline once a message has passed all filters and its Lead
row has been flushed to get an id. Small helpers only — the DB row is created
by the caller so we don't take a circular dep on `app`.
"""
from __future__ import annotations

import base64
import logging
import os
import re
from typing import List

log = logging.getLogger(__name__)

# Cap per-file size to protect the VM disk. Skip anything bigger.
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024   # 10 MB

# Skip common inline signature junk (logos, tracking pixels)
SKIP_INLINE_MIN_BYTES = 20 * 1024   # inline images under 20KB

# Root storage dir on VM. Overridable for local dev.
STORAGE_ROOT = os.environ.get(
    "EMAIL_INGEST_STORAGE_ROOT",
    "/var/www/procam-crm/uploads/email_leads",
)

ALLOWED_EXTS = {'.pdf', '.png', '.jpg', '.jpeg', '.gif', '.tiff', '.bmp',
                '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
                '.txt', '.csv', '.zip'}
DENIED_EXTS = {'.svg', '.html', '.htm', '.xhtml', '.js', '.mjs', '.hta',
               '.phtml', '.php', '.pht', '.exe', '.msi', '.bat', '.cmd',
               '.ps1', '.sh', '.jar', '.docm', '.xlsm', '.pptm'}


def _sanitize(name: str) -> str:
    """Sanitize a filename for safe filesystem storage."""
    if not name:
        return "attachment"
    # Strip path components, replace unsafe chars
    name = os.path.basename(name)
    name = re.sub(r"[^\w\-. ()]", "_", name)
    return name[:200] or "attachment"


def save_attachments_for_lead(graph, mailbox: str, message_id: str, lead_id: int) -> list[dict]:
    """Fetch, filter, and persist attachments for one Lead.

    Returns list of dicts describing each saved file, suitable for
    constructing LeadAttachment rows:
        {"filename", "content_type", "size_bytes", "storage_path",
         "email_attachment_id"}
    """
    saved: List[dict] = []
    try:
        atts = graph.list_attachments(mailbox=mailbox, message_id=message_id)
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to list attachments for msg=%s lead=%s: %s",
                    message_id, lead_id, e)
        return saved

    if not atts:
        return saved

    lead_dir = os.path.join(STORAGE_ROOT, str(lead_id))
    try:
        os.makedirs(lead_dir, exist_ok=True)
    except Exception as e:  # noqa: BLE001
        log.error("Cannot create storage dir %s: %s", lead_dir, e)
        return saved

    for a in atts:
        try:
            name = _sanitize(a.get("name") or "attachment")
            size = int(a.get("size") or 0)
            ctype = a.get("contentType") or "application/octet-stream"
            att_id = a.get("id") or ""
            is_inline = bool(a.get("isInline"))

            ext = os.path.splitext(name)[1].lower()
            if ext in DENIED_EXTS or (ext and ext not in ALLOWED_EXTS):
                log.info("Skip disallowed attachment ext=%s name=%s", ext, name)
                continue

            if size > MAX_ATTACHMENT_BYTES:
                log.info("Skip oversize attachment lead=%s name=%s size=%d",
                         lead_id, name, size)
                continue
            if is_inline and size < SKIP_INLINE_MIN_BYTES:
                # Signature logos, tracking pixels — noise, not useful docs.
                continue

            content_b64 = a.get("contentBytes")
            if not content_b64:
                continue

            try:
                content = base64.b64decode(content_b64)
            except Exception as e:  # noqa: BLE001
                log.warning("Bad base64 for attachment lead=%s name=%s: %s",
                            lead_id, name, e)
                continue

            # Dedup within lead dir. Two different cases hide behind one
            # name clash:
            #
            #   * the same file again — the webhook saved it and wrote no
            #     row, and the back-fill is now re-fetching it. Writing a
            #     second copy would orphan the first and double the
            #     directory across ten thousand leads. Same name and same
            #     size: reuse what is there.
            #   * a genuinely different file that happens to share a name.
            #     Suffix with -1, -2, so a second ingest cannot clobber
            #     the original bytes.
            target = os.path.join(lead_dir, name)
            base, ext = os.path.splitext(name)
            reused = False
            i = 1
            while os.path.exists(target):
                try:
                    if os.path.getsize(target) == len(content):
                        reused = True
                        break
                except OSError:
                    pass
                target = os.path.join(lead_dir, f"{base}-{i}{ext}")
                i += 1

            if not reused:
                with open(target, "wb") as f:
                    f.write(content)
            else:
                log.info("attachment already on disk, reusing lead=%s name=%s",
                         lead_id, os.path.basename(target))

            saved.append({
                "filename": os.path.basename(target),
                "content_type": ctype,
                "size_bytes": len(content),
                "storage_path": target,
                "email_attachment_id": att_id,
            })
        except Exception as e:  # noqa: BLE001
            log.exception("Failed to save attachment lead=%s: %s", lead_id, e)
            continue

    return saved
