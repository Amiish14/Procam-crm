"""
Feature flags for the email-notification release.

Every flag guards *new* behaviour only: with all of them off the CRM
behaves exactly as it did before this release, which is what makes the
rollback a line in the admin screen rather than a revert.

    FEATURE_EMAIL_NOTIFY    outbound notification email goes through the
                            outbox (queued, retried, batched, logged)
                            instead of straight at the transport
    FEATURE_RFQ_CAPTURE     keep the client's original message as a .eml
                            and record its attachments
    FEATURE_DAILY_BRIEF     the 08:30 action brief
    FEATURE_WEEKLY_PACK     the Thursday review pack and the Friday
                            freeze
    FEATURE_REPLY_CAPTURE   file replies on the thread against the lead
    FEATURE_MY_MAIL         the My Mail page
    FEATURE_FORWARD_RECEIPT a receipt to whoever forwarded the mail
    FEATURE_REOPEN_ON_MAIL  reopen a closed lead that receives client mail
    FEATURE_QUOTE_REPLY_CLASS
                            treat a forwarded offer or client reply as
                            business rather than dropping it
    EMAIL_KILL_SWITCH       stop all outbound CRM email, now, without a
                            deploy or a restart. Nothing is lost: the
                            outbox keeps queueing and the worker holds.

Where a flag lives
    In the database, in `app_settings`, under `flag.<NAME>`. Before
    this release they lived only in `.env`, which meant turning the
    daily brief off during an incident was a file edit plus a
    `systemctl restart` plus two gunicorn workers reloading — and the
    person who needed it off was usually not the person who could do
    that.

    The environment is still read, as the fallback, so a server
    configured by the old release keeps its settings and so a flag can
    still be forced from outside the application when the database is
    the thing that is broken. Precedence:

        1. the row in `app_settings`, when one exists
        2. `.env` / the process environment
        3. the built-in default

    One exception, deliberately: `EMAIL_KILL_SWITCH` is ON if *either*
    source says so. A kill switch that a stale database row could
    quietly countermand is not a kill switch.
"""
from __future__ import annotations

import os
import time

KILL_SWITCH = 'EMAIL_KILL_SWITCH'

#: (name, description, default when nothing is configured)
FLAGS = (
    ('FEATURE_EMAIL_NOTIFY', 'Queue outbound email through the outbox', False),
    ('FEATURE_RFQ_CAPTURE', 'Keep the original RFQ email and its files', False),
    ('FEATURE_DAILY_BRIEF', 'Daily action brief, 08:30 IST', True),
    ('FEATURE_WEEKLY_PACK', 'Thursday review pack and Friday freeze', False),
    ('FEATURE_REPLY_CAPTURE', 'File replies against the lead', False),
    ('FEATURE_MY_MAIL', 'The My Mail page: what happened to what you '
                        'forwarded to leads@', False),
    ('FEATURE_FORWARD_RECEIPT', 'Tell the forwarder what the CRM did with '
                                'their mail', False),
    ('FEATURE_REOPEN_ON_MAIL', 'Reopen a closed lead when client mail '
                               'lands on it', False),
    ('FEATURE_QUOTE_REPLY_CLASS', 'Recognise a forwarded Procam offer or a '
                                  'client reply as business', False),
    (KILL_SWITCH, 'Stop all outbound CRM email immediately', False),
)

NAMES = tuple(name for name, _d, _s in FLAGS)
DEFAULTS = {name: state for name, _d, state in FLAGS}
DESCRIPTIONS = {name: desc for name, desc, _s in FLAGS}

_TRUE = ('1', 'true', 'yes', 'on')
_FALSE = ('0', 'false', 'no', 'off')

#: Short, because the point of moving these into the database was that
#: a change takes effect without a restart — and two gunicorn workers
#: each hold their own copy. Ten seconds is the longest anybody should
#: wait after turning the kill switch on.
_CACHE_TTL_SECONDS = 10
_CACHE = {'rows': None, 'at': 0.0}


def cache_clear():
    _CACHE.update({'rows': None, 'at': 0.0})


def _truthy(raw):
    """True / False / None, where None means 'did not say'."""
    text = ('' if raw is None else str(raw)).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    if isinstance(raw, bool):
        return raw
    return None


def _stored(now=None):
    """{NAME: bool} from the database. {} when it cannot be read."""
    now = now if now is not None else time.monotonic()
    if _CACHE['rows'] is not None and (now - _CACHE['at']) < _CACHE_TTL_SECONDS:
        return _CACHE['rows']
    rows = {}
    try:
        import json

        from app.models.integration import AppSetting
        prefix = 'flag.'
        for row in AppSetting.query.filter(
                AppSetting.key.like(prefix + '%')).all():
            name = row.key[len(prefix):]
            if name not in DEFAULTS:
                continue
            try:
                state = _truthy(json.loads(row.value_json))
            except Exception:
                state = _truthy(row.value_json)
            if state is not None:
                rows[name] = state
    except Exception:
        # No table yet (before the migration), or no application
        # context (a script importing this module). The environment
        # answers, exactly as it did before. Not cached, so the first
        # call after the migration reads the real rows.
        return {}
    _CACHE.update({'rows': rows, 'at': now})
    return rows


def _env(name):
    return _truthy(os.environ.get(name))


def on(name, default=None):
    """Is this flag on?

    The kill switch is on if either source says so; see the module
    docstring. Everything else: database, then environment, then the
    flag's own default.
    """
    if default is None:
        default = DEFAULTS.get(name, False)
    if name == KILL_SWITCH:
        return bool(_stored().get(name)) or bool(_env(name))
    stored = _stored().get(name)
    if stored is not None:
        return stored
    from_env = _env(name)
    if from_env is not None:
        return from_env
    return bool(default)


def source_of(name):
    """Where the current answer came from, for the admin screen.

    An administrator looking at a switch that will not turn off needs
    to be told that the environment is holding it, not left to guess.
    """
    if name == KILL_SWITCH and _env(name):
        return 'environment'
    if _stored().get(name) is not None:
        return 'database'
    if _env(name) is not None:
        return 'environment'
    return 'default'


def set(name, state, *, actor=None):          # noqa: A001 — reads right here
    """Turn a flag on or off, now, for every worker.

    Returns the state as stored. Audited, because "who turned the
    daily brief off" is a question that gets asked.
    """
    import json

    from app import db
    from app.models.integration import AppSetting

    if name not in DEFAULTS:
        raise ValueError(f'unknown flag {name!r}')
    state = bool(state)
    was = on(name)

    row = AppSetting.query.filter_by(key='flag.' + name).first()
    if row is None:
        row = AppSetting(key='flag.' + name)
        db.session.add(row)
    row.value_json = json.dumps(state)
    row.updated_by = actor
    db.session.commit()
    cache_clear()

    try:
        from app.services import audit
        audit.record('flag.changed', 'AppSetting', 'flag.' + name,
                     old={'state': was}, new={'state': state}, actor=actor,
                     reason=('turned on' if state else 'turned off')
                     + ' in Admin → Email → Switches')
    except Exception:
        pass
    return state


def clear(name, *, actor=None):
    """Remove the database row so the environment decides again."""
    from app import db
    from app.models.integration import AppSetting

    row = AppSetting.query.filter_by(key='flag.' + name).first()
    if row is not None:
        db.session.delete(row)
        db.session.commit()
    cache_clear()
    try:
        from app.services import audit
        audit.record('flag.cleared', 'AppSetting', 'flag.' + name,
                     actor=actor,
                     reason='fell back to the environment setting')
    except Exception:
        pass
    return on(name)


def mail_stopped():
    """True when nothing may go out. Read by the transport and the
    worker, so there is one answer and not two."""
    return on(KILL_SWITCH)


def all_flags():
    """[(name, description, state, source)] for the admin screen."""
    return [(n, d, on(n), source_of(n)) for n, d, _s in FLAGS]
