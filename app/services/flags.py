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
    FEATURE_FF_CRM          the freight-forwarding rate, quote and
                            communication hub (FF-COMMS-01). Off: the
                            tables exist and nothing reads them.
    FEATURE_MY_MAIL         the My Mail page
    FEATURE_FORWARD_RECEIPT a receipt to whoever forwarded the mail
    FEATURE_REOPEN_ON_MAIL  reopen a closed lead that receives client mail
    FEATURE_QUOTE_REPLY_CLASS
                            treat a forwarded offer or client reply as
                            business rather than dropping it
    FEATURE_AUTO_ASSIGN     give every lead an owner on arrival, by rule
    FEATURE_CRM2            the CRM 2.0 cockpit, global search and stage
                            map. Off: the routes 404 and nothing on the
                            existing screens changes.
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
    ('FEATURE_AUTO_ASSIGN', 'Give every lead an owner automatically, and '
                            'ask the head to confirm the guesses', False),
    # FEATURE_FF_CRM was documented in this module from the day it was
    # written and never listed here, so `_stored()` skipped its row and
    # the admin screen could not turn it on — only the environment
    # could, which is the thing moving flags into the database was
    # meant to end. Listing it changes no behaviour while it is off.
    ('FEATURE_FF_CRM', 'The freight-forwarding rate, quote and '
                       'communication hub (FF-COMMS-01)', False),
    ('FEATURE_CRM2', 'CRM 2.0: the cockpit, global search and the '
                     'stage map', False),
    ('FEATURE_SENDER_BLOCK',
     'Hold mail from system senders — quarantine digests, bounces, '
     'postmaster notices — instead of making a lead of it', False),
    ('FEATURE_ACCOUNT_FIRST',
     'Every lead belongs to an account with an owner before anyone '
     'works it (CRM-ACCOUNT-FIRST-01)', False),
    ('FEATURE_RFQ_CHAIN',
     'The 12-step RFQ chain: timers from the priority, holds, and '
     'the chain board (CRM-RFQ-CHAIN-01)', False),
    ('FEATURE_QUOTE_REV_V2',
     'A revision keeps the first quote number (Rev-00, Rev-01), '
     'conditions flow in from the RFQ steps, and the PDF carries '
     'the letterhead (CRM-QUOTE-REV-02)', False),
    ('FEATURE_PRE_RFQ_FUNNEL',
     'Log activity moves the account stage, automatic lead '
     'channels, and RFQ targets (CRM-LEAD-JOURNEY-01)', False),
    ('FEATURE_LEAD_JOURNEY',
     'One lead record: the 8 + 12 journey flowchart and the '
     'time-per-step chart', False),
    ('FEATURE_RFQ_REPORTS',
     'RFQ dashboard, the slowest step, the workload table and the '
     'delay reasons — all from timestamps (CRM-RFQ-REPORTS-01)',
     False),
    ('FEATURE_JUNK_DELETE',
     'Delete – not relevant: take a newsletter or a personal mail off '
     'the board without filing it as a lost deal (CRM-JUNK-DELETE-01)',
     False),
    ('FEATURE_JUNK_SUGGEST',
     'Mark a new mail lead "Possible junk" so a person can look — it '
     'only suggests, it never deletes', False),
    ('FEATURE_JUNK_PURGE',
     'Allow an administrator to purge deleted leads older than 90 '
     'days, after an export', False),
    ('FEATURE_LEAD_ACCOUNT',
     'Name the account from the lead sheet: link it, create it, or '
     'correct who owns it (CRM-LEAD-ACCOUNT-01)', False),
    ('FEATURE_ACCOUNT_MERGE',
     'Merge duplicate accounts: one customer, one row, the old '
     'spelling kept as an alias (FINISH-TRACKER-CRM)', False),
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


#: A flag can be limited to named people while it is being looked
#: at. Stored beside the flag as `flag.<NAME>.only`, a list of
#: employee codes.
ONLY_SUFFIX = '.only'
#: The same idea by vertical, so a pilot can be "this
#: person, plus everybody in Warehousing" without
#: listing a department one code at a time — and
#: without the list going stale when somebody joins.
VERTICAL_SUFFIX = '.only_verticals'


def only_for(name):
    """Employee codes this flag is limited to, or () for everybody."""
    import json

    try:
        from app.models.integration import AppSetting
        row = AppSetting.query.filter_by(
            key='flag.' + name + ONLY_SUFFIX).first()
    except Exception:                                     # noqa: BLE001
        return ()
    if row is None:
        return ()
    try:
        codes = json.loads(row.value_json)
    except Exception:                                     # noqa: BLE001
        codes = []
    if isinstance(codes, str):
        codes = [codes]
    return tuple(sorted({str(c).strip().upper()
                         for c in (codes or []) if str(c).strip()}))


def only_verticals(name):
    """Verticals this flag is limited to, or () for everybody."""
    import json

    try:
        from app.models.integration import AppSetting
        row = AppSetting.query.filter_by(
            key='flag.' + name + VERTICAL_SUFFIX).first()
    except Exception:                                     # noqa: BLE001
        return ()
    if row is None:
        return ()
    try:
        names = json.loads(row.value_json)
    except Exception:                                     # noqa: BLE001
        names = []
    if isinstance(names, str):
        names = [names]
    return tuple(sorted({str(v).strip() for v in (names or [])
                         if str(v).strip()}))


def on_for(name, emp_code, default=None, vertical=None):
    """Is this flag on *for this person*?

    A flag that is off is off for everybody — the limit narrows, it
    never grants. That ordering matters: a reviewer list left behind
    on a flag somebody later turned off must not quietly switch it
    back on for them.

    Two limits, and they are an OR, not an AND: a pilot reads "this
    person, and everybody in that vertical". Requiring both would
    mean naming every member of the vertical as well, which is the
    list that goes stale the first time somebody joins.
    """
    if not on(name, default):
        return False
    codes = only_for(name)
    verticals = only_verticals(name)
    if not codes and not verticals:
        return True
    if codes and bool(emp_code) \
            and str(emp_code).strip().upper() in codes:
        return True
    if verticals:
        if vertical is None:
            vertical = _vertical_of(emp_code)
        try:
            from app.services import vertical_heads as vh
            if vertical and vh.master_vertical(vertical) in {
                    vh.master_vertical(v) for v in verticals}:
                return True
        except Exception:                                 # noqa: BLE001
            if vertical and vertical in verticals:
                return True
    return False


def _vertical_of(emp_code):
    """The vertical this person belongs to, or ''."""
    if not emp_code:
        return ''
    try:
        from app import Employee
        emp = Employee.query.filter_by(
            emp_code=str(emp_code).strip().upper()).first()
        return (getattr(emp, 'vertical', '') or '').strip()
    except Exception:                                     # noqa: BLE001
        return ''


def limit_verticals(name, verticals, *, actor=None):
    """Limit a flag to these verticals, or pass nothing to lift it."""
    import json

    from app import db
    from app.models.integration import AppSetting

    if name not in DEFAULTS:
        raise ValueError(f'unknown flag {name!r}')
    clean = sorted({str(v).strip() for v in (verticals or [])
                    if str(v).strip()})
    key = 'flag.' + name + VERTICAL_SUFFIX
    was = only_verticals(name)
    row = AppSetting.query.filter_by(key=key).first()
    if not clean:
        if row is not None:
            db.session.delete(row)
    else:
        if row is None:
            row = AppSetting(key=key)
            db.session.add(row)
        row.value_json = json.dumps(clean)
        row.updated_by = actor
    db.session.commit()
    cache_clear()

    try:
        from app.services import audit
        audit.record('flag.limited_verticals', 'AppSetting', key,
                     old={'verticals': list(was)},
                     new={'verticals': clean}, actor=actor,
                     reason=(f'limited to {", ".join(clean)}' if clean
                             else 'vertical limit lifted'))
    except Exception:                                     # noqa: BLE001
        pass
    return tuple(clean)


def limit_to(name, codes, *, actor=None):
    """Limit a flag to these people, or pass nothing to lift it."""
    import json

    from app import db
    from app.models.integration import AppSetting

    if name not in DEFAULTS:
        raise ValueError(f'unknown flag {name!r}')
    clean = sorted({str(c).strip().upper() for c in (codes or [])
                    if str(c).strip()})
    key = 'flag.' + name + ONLY_SUFFIX
    was = only_for(name)
    row = AppSetting.query.filter_by(key=key).first()
    if not clean:
        if row is not None:
            db.session.delete(row)
    else:
        if row is None:
            row = AppSetting(key=key)
            db.session.add(row)
        row.value_json = json.dumps(clean)
        row.updated_by = actor
    db.session.commit()
    cache_clear()

    try:
        from app.services import audit
        audit.record('flag.limited', 'AppSetting', key,
                     old={'only': list(was)}, new={'only': clean},
                     actor=actor,
                     reason=(f'limited to {", ".join(clean)}' if clean
                             else 'limit lifted — everyone sees it'))
    except Exception:                                     # noqa: BLE001
        pass
    return tuple(clean)


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
    """[(name, description, state, source)] for the admin screen.

    Four items, not five: three call sites and a template unpack
    this, and a flag screen that 500s is a worse outcome than one
    that does not mention the reviewer list. `all_limits()` carries
    that separately.
    """
    return [(n, d, on(n), source_of(n)) for n, d, _s in FLAGS]


def all_limits():
    """{NAME: (codes…)} for the flags that are limited to people."""
    return {n: only_for(n) for n, _d, _s in FLAGS if only_for(n)}
