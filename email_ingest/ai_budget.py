"""
v2026-09-02 — Self-imposed daily token budget for AI extraction.

Groq's free tier meters tokens over a rolling 24h window. Hitting that wall
is disruptive: extraction stops mid-day and whatever arrives afterwards is
regex-only. Worse, the budget is spent first-come-first-served, so a burst
of machine-generated notifications can consume the day before a real
customer enquiry turns up.

This tracks spend ourselves and stops *before* the provider's limit, keeping
a reserve. State lives in a small JSON file so it is shared across the
5-minutely poller processes, not just one run.

Env:
    AI_DAILY_TOKEN_BUDGET   default 180000 — sits under Groq's 200k so we
                            stop cleanly rather than erroring
    AI_BUDGET_FILE          override the state file location
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

log = logging.getLogger(__name__)

_DEFAULT_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'instance', 'ai_budget.json')

_WINDOW = 24 * 3600.0
_lock = threading.Lock()


def budget() -> int:
    try:
        return max(0, int(os.environ.get('AI_DAILY_TOKEN_BUDGET') or 180000))
    except ValueError:
        return 180000


def _path() -> str:
    return (os.environ.get('AI_BUDGET_FILE') or _DEFAULT_FILE).strip()


def _load() -> list:
    """[[timestamp, tokens], ...] within the rolling window."""
    try:
        with open(_path()) as fh:
            data = json.load(fh)
        cutoff = time.time() - _WINDOW
        return [e for e in data.get('spend', [])
                if isinstance(e, list) and len(e) == 2 and e[0] >= cutoff]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def _save(entries: list) -> None:
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w') as fh:
            json.dump({'spend': entries}, fh)
        os.replace(tmp, path)
    except OSError as e:                                          # noqa: BLE001
        log.debug('could not persist AI budget: %s', e)


def used() -> int:
    """Tokens spent in the trailing 24 hours."""
    return sum(int(e[1]) for e in _load())


def remaining() -> int:
    return max(0, budget() - used())


def can_spend(estimate: int = 3000) -> bool:
    """Is there room for another call of roughly this size?"""
    if budget() <= 0:
        return True                     # budget disabled
    return remaining() >= estimate


def record(tokens: int) -> None:
    if tokens <= 0 or budget() <= 0:
        return
    with _lock:
        entries = _load()
        entries.append([time.time(), int(tokens)])
        _save(entries)


def status() -> dict:
    u = used()
    b = budget()
    return {'used': u, 'budget': b, 'remaining': max(0, b - u),
            'pct': round(100.0 * u / b, 1) if b else 0.0}
