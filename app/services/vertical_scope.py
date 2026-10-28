"""
One reading of "which vertical is this".

Split out because three callers needed it and each would otherwise carry its own copy of the
Management-fallback trap.
"""
from __future__ import annotations


def vertical_name(record):
    """The record's vertical, without the Management fallback.

    `vertical_heads.master_vertical` answers Management for
    anything it cannot place, which is right for routing a stray
    notification and quite wrong here: it turned "Heavy
    Transport" into "Management" wherever the vertical master was
    not loaded, so the margin floor became 10% instead of 38% and
    every step went to Management's desk. The master name is used
    when it resolves to something real; otherwise the record's
    own word stands.
    """
    try:
        from app.services import vertical_heads as vh
        raw = (vh.vertical_of(record) or '').strip()
        if not raw:
            return ''
        return master_name(raw)
    except Exception:                                     # noqa: BLE001
        return (getattr(record, 'procam_vertical', '')
                or getattr(record, 'vertical', '') or '').strip()


def master_name(raw):
    """The master spelling of a vertical name, without the fallback.

    `vertical_heads.master_vertical` answers Management for
    anything it cannot place, which is right for routing a stray
    notification and wrong wherever a real vertical is being
    named. This has now caught three callers — the margin floor,
    the chain's step owners and the readiness check — so it lives
    in one place.
    """
    raw = (raw or '').strip()
    if not raw:
        return ''
    try:
        from app.services import vertical_heads as vh
        master = (vh.master_vertical(raw) or '').strip()
        fallback = getattr(vh, 'FALLBACK_VERTICAL', 'Management')
        if master and master != raw and master == fallback:
            return raw
        return master or raw
    except Exception:                                     # noqa: BLE001
        return raw
