"""
Who is on the To line and who is on the CC line, per event, editable.

`notification_rules.MATRIX` decides this today, and it decides it well
— roles rather than names, one table, readable. What it cannot do is
change without a deploy, and it has no notion of a CC: every recipient
is addressed individually, which is why a lead assignment produces
four separate emails rather than one email with three people copied.
Four emails is not a cosmetic problem. Nobody can see who else knows,
so two people chase the same client, and a reply goes to one person
instead of the thread.

This module adds a layer the administrators own:

    notification_policy.resolve(event, record, actor=...)
        → {'to': [codes], 'cc': [codes], 'email': bool, 'bell': bool,
           'version': int}

The matrix is still the default. A `notification_policy` row overrides
it, and only the parts it sets. Nothing is hard-coded here either —
the standing CC comes from `app_settings`, so adding a director to
everything is a screen, not a release.

Two rules the resolver enforces, because they are the ones that get
broken by hand:

    Nobody appears twice. Not twice in To, not twice in CC, and never
    in both — a CC is "for your awareness", and a person on the To
    line is being asked to act, so the CC is dropped for them.

    The version is recorded on the email. When somebody asks in
    December why the November emails went where they did, the answer
    is the policy version stamped on the row, not a guess about what
    the table said at the time.
"""
from __future__ import annotations

import logging
import time
from typing import NamedTuple

from app.services import notification_rules as rules

log = logging.getLogger(__name__)

#: Same reason as everywhere else: two workers, each with its own
#: copy, and a change made on one has to reach the other.
_CACHE_TTL_SECONDS = 30
_CACHE = {'rows': None, 'at': 0.0}

#: A CC-only role: the vertical head, resolved through the new master
#: rather than through `employees.vertical` alone.
R_HEAD = rules.R_HEAD


def cache_clear():
    _CACHE.update({'rows': None, 'at': 0.0})


class Stored(NamedTuple):
    """One saved override, as plain values.

    Not the ORM row. A module-level cache that holds ORM instances
    hands them out after the request that loaded them is over: the
    session is gone, so they are detached, and a commit in that
    session expired their attributes on the way out. Reading one
    then raises DetachedInstanceError, which is how Save on
    Admin → Email → Verticals came to answer 500 while saving
    perfectly well (CRM-VERTICAL-SAVE-500-01). This cache had the
    same shape and the same latent failure.

    The names are the ORM's, so every reader is unchanged.
    """
    event: str
    version: int
    email_enabled: bool
    bell_enabled: bool
    #: None means "not overridden" — the matrix answers. Kept apart
    #: from an empty list, which means "overridden to nobody".
    to_roles_json: object
    cc_roles_json: object
    to_roles: tuple
    cc_roles: tuple


def _stored():
    """{event: Stored}. {} if the table is not there yet."""
    now = time.monotonic()
    if _CACHE['rows'] is not None and (now - _CACHE['at']) < _CACHE_TTL_SECONDS:
        return _CACHE['rows']
    try:
        from app.models.email_admin import NotificationPolicy
        rows = {r.event: Stored(
            event=r.event,
            version=int(r.version or 1),
            email_enabled=bool(r.email_enabled),
            bell_enabled=bool(r.bell_enabled),
            to_roles_json=r.to_roles_json,
            cc_roles_json=r.cc_roles_json,
            to_roles=tuple(r.to_roles or ()),
            cc_roles=tuple(r.cc_roles or ()),
        ) for r in NotificationPolicy.query.all()}
    except Exception:
        return {}
    _CACHE.update({'rows': rows, 'at': now})
    return rows


def policy_for(event):
    """The effective policy for one event, matrix plus overrides.

    Returns a plain dict so nothing downstream can edit the cached
    row in place. `source` says which parts an administrator has
    changed, for the screen.
    """
    row = rules.rule(event)
    out = {
        'event': event,
        'title': (row or {}).get('title', event),
        'kind': (row or {}).get('kind', 'info'),
        'to_roles': list((row or {}).get('to', ())),
        'cc_roles': [],
        'email': bool((row or {}).get('email', False)),
        'bell': bool((row or {}).get('in_app', True)),
        'version': 1,
        'edited': False,
    }
    stored = _stored().get(event)
    if stored is not None:
        out['edited'] = True
        out['version'] = int(stored.version or 1)
        out['email'] = bool(stored.email_enabled)
        out['bell'] = bool(stored.bell_enabled)
        if stored.to_roles_json is not None:
            out['to_roles'] = list(stored.to_roles)
        if stored.cc_roles_json is not None:
            out['cc_roles'] = list(stored.cc_roles)
    return out


# ── the standing CC ──────────────────────────────────────────────────
def always_copy():
    """Employee codes copied on every notification email.

    Seeded with the director who asked for it, held in `app_settings`
    so changing it is a screen. Returns [] when the standing CC is
    switched off, which is a real answer — the CRM does not fall back
    to "whoever looks senior".
    """
    try:
        from app.services import admin_settings
        if not admin_settings.get('always_copy_enabled'):
            return []
        raw = admin_settings.get('always_copy') or []
    except Exception:
        return []
    return [str(c).strip().upper() for c in raw if str(c).strip()]


# ── resolving ────────────────────────────────────────────────────────
def _codes_for_role(role, event, record, actor=None):
    """Employee codes for one role, in order, or [].

    One role at a time rather than handing a list to
    `notification_rules.recipients_for`: that function de-duplicates
    and drops the actor as it goes, which is right for a single To
    line and wrong here, where the same resolution feeds two lines and
    the dropping has to happen once, at the end, across both.
    """
    owner = rules.owner_of(record) if record is not None else ''
    if role == rules.R_OWNER:
        return [owner]
    if role == rules.R_SECONDARY:
        return [rules.secondary_of(record) if record is not None else '']
    if role == rules.R_PREPARER:
        return [rules._first_attr(record, ('prepared_by_id',))
                if record is not None else '']
    if role == rules.R_APPROVER:
        return [rules._first_attr(record, ('approved_by_id',))
                if record is not None else '']
    if role == R_HEAD:
        from app.services import vertical_heads
        heads = vertical_heads.resolve_vertical_head(record)
        if heads:
            return list(heads)
        # Nothing in the master for this vertical. Fall back to the
        # reporting line on the employee record, which is what the
        # CRM used before the master existed — a gap in new
        # configuration must not remove a recipient who was being
        # told yesterday.
        legacy = rules.vertical_head_for(owner)
        return [legacy] if legacy else []
    if role == rules.R_ADMIN:
        return list(rules.administrators())
    if role == rules.R_MANAGEMENT:
        return list(rules.management())
    if role == rules.R_ACTOR:
        return [(actor or '').strip().upper()]
    log.warning('notification_policy: unknown role %r on %r', role, event)
    return []


def _roles_to_codes(roles, event, record, actor=None):
    """Employee codes for a role list, in the order the roles are in."""
    out = []
    for role in roles:
        if not role:
            continue
        out.extend(_codes_for_role(role, event, record, actor=actor))
    return out


def _dedupe(codes, *, exclude=()):
    seen = set(c for c in exclude)
    out = []
    for code in codes:
        code = (code or '').strip().upper()
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def resolve(event, record=None, *, actor=None, escalate=False,
            roles=None, with_always_copy=True):
    """Who hears about this, split into To and CC.

    `roles` overrides the To list, which is how the scheduled jobs
    address a report to one person without inventing a new event; the
    CC list is left as the policy has it.
    """
    pol = policy_for(event)
    to_roles = list(roles) if roles is not None else list(pol['to_roles'])
    row = rules.rule(event)
    if escalate and row is not None and row.get('escalate_to'):
        to_roles = to_roles + [row['escalate_to']]

    to_raw = _roles_to_codes(to_roles, event, record, actor=actor)
    cc_raw = _roles_to_codes(pol['cc_roles'], event, record, actor=actor)

    # Nobody is told about their own action, unless a role names the
    # actor on purpose — somebody who just pressed the button does not
    # need an email about having pressed it. This is how the matrix
    # has always behaved; it is applied once here, across both lines,
    # instead of once per line.
    actor_code = (actor or '').strip().upper()
    named = rules.R_ACTOR in to_roles or rules.R_ACTOR in pol['cc_roles']
    if actor_code and not named:
        to_raw = [c for c in to_raw if (c or '').strip().upper() != actor_code]
        cc_raw = [c for c in cc_raw if (c or '').strip().upper() != actor_code]

    to = _dedupe(to_raw)

    if with_always_copy and pol['email']:
        cc_raw = list(cc_raw) + always_copy()

    # The administrators' own copy list, which predates this module
    # and is still theirs to set. It becomes a CC rather than N more
    # emails, which is the whole point of this release.
    try:
        from app.services import admin_settings
        cc_raw = list(cc_raw) + list(admin_settings.admin_recipients(event))
    except Exception:
        pass

    # A person being asked to act is not also told "for your
    # awareness"; the To line wins.
    cc = _dedupe(cc_raw, exclude=to)

    return {'to': to, 'cc': cc, 'email': pol['email'], 'bell': pol['bell'],
            'version': pol['version']}


# ── editing ──────────────────────────────────────────────────────────
def save(event, *, to_roles=None, cc_roles=None, email_enabled=None,
         bell_enabled=None, actor=None):
    """Change one event's policy. Returns the new version.

    The version goes up on every save, including a save that changes
    nothing, because the question it answers is "which policy was in
    force when that email went out" and a no-op save is still a point
    in time somebody chose to touch it.
    """
    import json

    from app import db
    from app.models.email_admin import NotificationPolicy

    if rules.rule(event) is None:
        raise ValueError(f'no such event: {event!r}')
    bad = [r for r in list(to_roles or []) + list(cc_roles or [])
           if r not in rules.ROLES]
    if bad:
        raise ValueError('not a role: ' + ', '.join(sorted(set(bad))))

    before = policy_for(event)
    row = NotificationPolicy.query.filter_by(event=event).first()
    if row is None:
        row = NotificationPolicy(event=event, version=0)
        db.session.add(row)
    if to_roles is not None:
        row.to_roles_json = json.dumps(list(to_roles))
    if cc_roles is not None:
        row.cc_roles_json = json.dumps(list(cc_roles))
    if email_enabled is not None:
        row.email_enabled = bool(email_enabled)
    if bell_enabled is not None:
        row.bell_enabled = bool(bell_enabled)
    row.version = int(row.version or 0) + 1
    row.updated_by = actor
    db.session.commit()
    cache_clear()

    after = policy_for(event)
    try:
        from app.services import audit
        audit.record('notification_policy.changed', 'NotificationPolicy',
                     event,
                     old={k: before[k] for k in
                          ('to_roles', 'cc_roles', 'email', 'bell')},
                     new={k: after[k] for k in
                          ('to_roles', 'cc_roles', 'email', 'bell')},
                     actor=actor,
                     reason='changed in Admin → Email → Recipients')
    except Exception:
        pass
    return after['version']


def reset(event, *, actor=None):
    """Drop the override so the matrix decides again."""
    from app import db
    from app.models.email_admin import NotificationPolicy

    row = NotificationPolicy.query.filter_by(event=event).first()
    if row is not None:
        db.session.delete(row)
        db.session.commit()
    cache_clear()
    try:
        from app.services import audit
        audit.record('notification_policy.reset', 'NotificationPolicy',
                     event, actor=actor,
                     reason='returned to the built-in recipient list')
    except Exception:
        pass
    return policy_for(event)


def rows():
    """Every event with its effective policy, for the screen."""
    return [policy_for(event) for event in rules.EVENTS]
