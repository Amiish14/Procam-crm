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
        master = (vh.master_vertical(raw) or '').strip()
        if master and master.lower() != raw.lower() \
                and master == getattr(vh, 'FALLBACK_VERTICAL',
                                      'Management'):
            return raw
        return master or raw
    except Exception:                                     # noqa: BLE001
        return (getattr(record, 'procam_vertical', '')
                or getattr(record, 'vertical', '') or '').strip()
