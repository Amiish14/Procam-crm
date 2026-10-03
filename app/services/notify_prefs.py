"""
What each person wants to be told, and when they want to hear it.

Three anti-noise rules live here, and nowhere else, so that "why did I
not get that email?" has one place to look:

  * **muted events** — a person may turn off an event they do not need.
    Only the ones turned *off* are stored, so an event added to the
    matrix next year arrives switched on rather than silently off.
  * **quiet hours** — nothing lands between 21:00 and 07:00 IST. The
    email is not dropped, it is held until the morning, because a lead
    assigned at midnight still needs working at nine. An email marked
    urgent ignores the window.
  * **batching** — a person in batched mode has their email held for a
    few minutes so that a bulk assignment of twenty leads arrives as
    one message rather than twenty.

Times are IST throughout. The server runs on UTC and the business does
not, which has already caused one dated bug in this codebase; the
conversion happens here and the stored `not_before` is UTC.
"""
from __future__ import annotations

from datetime import datetime, timedelta

#: India Standard Time. No daylight saving, so a fixed offset is right.
IST = timedelta(hours=5, minutes=30)

DEFAULTS = {
    'email_enabled': True,
    'mode': 'immediate',
    'batch_minutes': 15,
    'quiet_enabled': True,
    'quiet_start_hour': 21,
    'quiet_end_hour': 7,
    'daily_brief': True,
    'weekly_pack': True,
    'muted_events': (),
}

#: Events that ignore quiet hours and batching. Deliberately short: if
#: everything is urgent, the quiet window means nothing.
URGENT_EVENTS = (
    'rfq.quote_overdue',
    'quote.validity_lapsed',
    'security.password_reset',
)


def _row(user_code):
    from app.models.mailops import NotificationPref
    code = (user_code or '').strip().upper()
    if not code:
        return None
    try:
        return NotificationPref.query.filter_by(user_code=code).first()
    except Exception:
        # The table may not exist yet on a machine that has not run the
        # migration. Defaults are the old behaviour, so that is safe.
        return None


def for_user(user_code):
    """Preferences with defaults filled in. Never raises."""
    import json

    out = dict(DEFAULTS)
    row = _row(user_code)
    if row is None:
        return out
    for key in ('email_enabled', 'mode', 'batch_minutes', 'quiet_enabled',
                'quiet_start_hour', 'quiet_end_hour', 'daily_brief',
                'weekly_pack'):
        val = getattr(row, key, None)
        if val is not None:
            out[key] = val
    try:
        muted = json.loads(row.muted_events_json or '[]')
        out['muted_events'] = tuple(m for m in muted if isinstance(m, str))
    except Exception:
        out['muted_events'] = ()
    return out


def save(user_code, **changes):
    """Write a person's preferences. Returns the stored row."""
    import json

    from app import db
    from app.models.mailops import NotificationPref

    code = (user_code or '').strip().upper()
    row = NotificationPref.query.filter_by(user_code=code).first()
    if row is None:
        row = NotificationPref(user_code=code)
        db.session.add(row)
    for key in ('email_enabled', 'quiet_enabled', 'daily_brief',
                'weekly_pack'):
        if key in changes:
            setattr(row, key, bool(changes[key]))
    if 'mode' in changes:
        # Text, not a flag. Anything unrecognised means immediate: a
        # typo must not silently hold somebody's email for ever.
        row.mode = ('batched' if str(changes['mode']).strip().lower()
                    == 'batched' else 'immediate')
    if 'batch_minutes' in changes:
        row.batch_minutes = max(1, min(120, int(changes['batch_minutes'] or 15)))
    for key in ('quiet_start_hour', 'quiet_end_hour'):
        if key in changes:
            setattr(row, key, max(0, min(23, int(changes[key] or 0))))
    if 'muted_events' in changes:
        muted = [str(e)[:60] for e in (changes['muted_events'] or [])]
        row.muted_events_json = json.dumps(sorted(set(muted)))
    db.session.commit()
    return row


def wants(user_code, event_key):
    """Should this person be emailed about this event at all?"""
    prefs = for_user(user_code)
    if not prefs['email_enabled']:
        return False
    return event_key not in (prefs.get('muted_events') or ())


def in_quiet_hours(prefs, when_ist):
    if not prefs.get('quiet_enabled'):
        return False
    start = int(prefs.get('quiet_start_hour', 21))
    end = int(prefs.get('quiet_end_hour', 7))
    hour = when_ist.hour
    if start == end:
        return False
    if start < end:                       # e.g. 01:00–06:00
        return start <= hour < end
    return hour >= start or hour < end    # e.g. 21:00–07:00, over midnight


def release_at(user_code, *, event_key='', now=None, urgent=None):
    """When this email may go out, in UTC.

    Returns (when, why) so the health screen and the tests can say
    which rule held a message back.
    """
    now = now or datetime.utcnow()
    if not (user_code or '').strip():
        # Nobody's preferences to consult. This is a message to a
        # mailbox rather than to a colleague — a report to a list, or a
        # health alert — and the caller has already chosen the moment.
        return now, 'no recipient preferences'
    prefs = for_user(user_code)
    if urgent is None:
        urgent = event_key in URGENT_EVENTS
    if urgent:
        return now, 'urgent'

    when = now
    why = 'immediate'
    if prefs.get('mode') == 'batched':
        when = now + timedelta(minutes=int(prefs.get('batch_minutes') or 15))
        why = 'batched'

    ist = when + IST
    if in_quiet_hours(prefs, ist):
        end = int(prefs.get('quiet_end_hour', 7))
        morning = ist.replace(hour=end, minute=0, second=0, microsecond=0)
        if morning <= ist:
            morning = morning + timedelta(days=1)
        when = morning - IST
        why = 'quiet hours'
    return when, why
