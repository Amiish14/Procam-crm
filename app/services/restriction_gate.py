"""
The one line a route adds to respect the block register.

    from app.services import restriction_gate as gate

    refusal = gate.guard(d, what='lead')
    if refusal:
        return refusal

That is the whole contract. `guard` reads the payload, asks the central
check, records the attempt, and returns a ready response when the
request must not go ahead — or None when it may.

Three outcomes, and the middle one is the interesting one:

  * **blocked** → HTTP 403 and nothing is written. The attempt is
    logged against the register, because "who keeps trying" is a
    question administrators ask.
  * **caution, not yet acknowledged** → HTTP 409 carrying the reason.
    409 and not 403 on purpose: the request is not forbidden, it is
    *unfinished*. The browser shows the warning, the person ticks the
    box, and the same request is sent again with
    `restriction_ack: <id>`.
  * **caution, acknowledged** → None. The acknowledgement is written to
    the register with who did it and on what.

Kept apart from `client_restrictions` so that module stays pure and
testable without a request context; everything Flask-shaped is here.
"""
from __future__ import annotations

from app.services import client_restrictions as restrictions


def _actor():
    try:
        from flask import session
        return session.get('emp_code')
    except Exception:
        return None


def verdict_for(payload=None, *, company_name=None, email=None, domain=None,
                gstin=None, pan=None, account_id=None, business_unit=None):
    """The verdict for a request body, or for named parts of one."""
    data = payload if isinstance(payload, dict) else {}
    return restrictions.check(
        company_name=company_name if company_name is not None
        else data.get('company') or data.get('company_name')
        or data.get('name') or data.get('client'),
        email=email if email is not None
        else (data.get('email') or data.get('email2')),
        domain=domain,
        gstin=gstin if gstin is not None else data.get('gstin'),
        pan=pan if pan is not None else data.get('pan'),
        account_id=(account_id if account_id is not None
                    else data.get('company_id') or data.get('account_id')),
        business_unit=business_unit)


def acknowledged(payload, verdict):
    """Did the caller say they have read the warning, for this entry?

    The acknowledgement names the restriction it is for, so a stale tick
    from a different warning cannot wave this one through.
    """
    data = payload if isinstance(payload, dict) else {}
    ack = data.get('restriction_ack')
    if ack in (True, 'true', '1', 1) and verdict.restriction_id:
        # A bare `true` is accepted from form posts that cannot easily
        # carry the id, but the id form is what the JSON screens send.
        return True
    try:
        return int(ack) == int(verdict.restriction_id)
    except (TypeError, ValueError):
        return False


def guard(payload=None, *, what='record', entity_id=None, allow_caution=True,
          business_unit=None, **parts):
    """None if the request may proceed, otherwise the response to return."""
    from flask import jsonify

    verdict = verdict_for(payload, business_unit=business_unit, **parts)
    if verdict.clear:
        return None

    actor = _actor()
    if verdict.blocked:
        restrictions.record_attempt(verdict, what=what, user_id=actor,
                                    detail={'entity_id': entity_id})
        return jsonify({
            'error': verdict.message(),
            'blocked': True,
            'restriction': verdict.to_dict(),
        }), 403

    if not allow_caution:
        restrictions.record_attempt(verdict, what=what, user_id=actor,
                                    detail={'entity_id': entity_id})
        return jsonify({'error': verdict.message(),
                        'restriction': verdict.to_dict()}), 403

    if acknowledged(payload, verdict):
        restrictions.record_acknowledgement(verdict, what=what,
                                            user_id=actor,
                                            entity_id=entity_id)
        return None

    restrictions.record_attempt(verdict, what=what, user_id=actor,
                                detail={'entity_id': entity_id,
                                        'outcome': 'warned'})
    return jsonify({
        'error': verdict.message(),
        'needs_acknowledgement': True,
        'restriction': verdict.to_dict(),
    }), 409


def badge(company_name=None, **parts):
    """'blocked' | 'caution' | '' — for a list or a detail page.

    Cheap enough to call per row: the register is cached in memory and
    is a handful of entries.
    """
    verdict = restrictions.check(company_name=company_name, **parts)
    return '' if verdict.clear else verdict.level


def badges_for(names):
    """{name: level} for a page full of rows, computed once each."""
    out = {}
    for name in names:
        if not name or name in out:
            continue
        level = badge(company_name=name)
        if level:
            out[name] = level
    return out
