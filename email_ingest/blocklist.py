"""
v2026-09-02 — Sender denylist for lead ingestion.

The leads inbox receives a lot of promotional mail relayed in by the
auto-forward: consultancy digests, executive-education mailers, trade-press
newsletters, event marketing, bank statements, job boards. None of it is a
lead. This blocks it at ingest so it never reaches the CRM at all, rather
than being captured and purged afterwards.

Deliberately narrow: it matches the SENDER's domain against an explicit
list, plus a small set of unmistakably promotional local parts. No content
heuristics, no AI verdict, nothing that could quietly swallow a real
enquiry. If it is not on the list, it becomes a lead.

The list lives in data/blocked_senders.txt and is re-read when the file
changes, so a domain can be added without a code change or a deploy.

Env:
    LEAD_BLOCKLIST_FILE      override the list location
    LEAD_BLOCK_BULK_LOCALS   'true' (default) — also block newsletter@,
                             marketing@, promo@ etc. on ANY domain. Note
                             this deliberately does NOT include no-reply@,
                             which tender and procurement portals use for
                             genuine RFQs.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Optional, Set

log = logging.getLogger(__name__)

_DEFAULT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'blocked_senders.txt')

# Local parts that only ever send bulk marketing. no-reply/notifications are
# intentionally absent — procurement portals send real RFQs from those.
_BULK_LOCAL_RE = re.compile(
    r'^(newsletter|newsletters|marketing|promo|promotions?|campaign|campaigns|'
    r'digest|digests|offers?|deals?|advert|advertising|subscribe|'
    r'subscriptions?|mailer|mailers|bulk|broadcast|broadcasts)'
    r'(\d*|[._-].*)?$', re.I)

_lock = threading.Lock()
_cache: Set[str] = set()
_cache_mtime: float = -1.0
_cache_path: Optional[str] = None


def list_file() -> str:
    return (os.environ.get('LEAD_BLOCKLIST_FILE') or _DEFAULT_FILE).strip()


def _flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == '':
        return default
    return raw.strip().lower() in ('1', 'true', 'yes', 'on')


def blocked_domains() -> Set[str]:
    """The denylist, reloaded whenever the file changes on disk."""
    global _cache, _cache_mtime, _cache_path
    path = list_file()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        if _cache_path != path:
            log.warning('blocklist file not found: %s — nothing is blocked', path)
            _cache_path = path
        return set()
    with _lock:
        if path != _cache_path or mtime != _cache_mtime:
            entries = set()
            try:
                with open(path) as fh:
                    for line in fh:
                        line = line.split('#', 1)[0].strip().lower()
                        if line:
                            entries.add(line.lstrip('@.'))
            except OSError as e:                                 # noqa: BLE001
                log.warning('could not read blocklist %s: %s', path, e)
                return _cache
            _cache, _cache_mtime, _cache_path = entries, mtime, path
            log.info('blocklist loaded: %d domains from %s', len(entries), path)
        return _cache


def domain_blocked(domain: str) -> Optional[str]:
    """Return the matching denylist entry, or None."""
    d = (domain or '').strip().lower().rstrip('.')
    if not d:
        return None
    for entry in blocked_domains():
        if d == entry or d.endswith('.' + entry):
            return entry
    return None


def check(email: str) -> Optional[str]:
    """Reason this sender is blocked, or None if it may become a lead."""
    addr = (email or '').strip().lower()
    if not addr or '@' not in addr:
        return None
    local, _, domain = addr.partition('@')

    hit = domain_blocked(domain)
    if hit:
        return f'blocked sender domain: {hit}'

    if _flag('LEAD_BLOCK_BULK_LOCALS', True) and _BULK_LOCAL_RE.match(local):
        return f'blocked bulk sender: {local}@'

    return None
