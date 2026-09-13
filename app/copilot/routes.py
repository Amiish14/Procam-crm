"""Procam AI — HTTP surface.

Every endpoint resolves the caller's scope server-side and passes it
down. None of them accepts a scope, an emp_code or a filter that could
widen what is returned: a caller supplies the question, and optionally
the record the panel is open on and a conversation id — the first is
checked against the caller's own scope, the second against the caller's
own identity, and neither can widen anything.
"""
from flask import Blueprint, jsonify, render_template, request, session

from app import db
from app.access import scope as scope_mod
from app.access.service import require
from app.copilot import model as model_mod
from app.copilot import service as svc

bp = Blueprint('copilot', __name__)

#: Everyone who can sign in may ask questions — what they get back is
#: bounded by their own scope, which is the point. Individual intents
#: carry their own permission from the matrix.
_PERM = None


def _actor():
    return session.get('emp_code') or ''


def _authed():
    return bool(session.get('emp_code'))


@bp.route('/api/copilot/ask', methods=['POST'])
def api_ask():
    if not _authed():
        return jsonify(ok=False, error='Not authenticated'), 401
    d = request.get_json(silent=True) or {}
    ctx = d.get('context')
    answer = svc.ask((d.get('q') or '')[:1000],
                     history=d.get('history') or [],
                     context=ctx if isinstance(ctx, dict) else None,
                     conversation_id=_conversation(d),
                     actor=_actor())
    return jsonify(ok=True, **answer.to_dict())


def _conversation(d):
    """The conversation id the panel sent back, as a short string.

    It is not trusted: the service verifies its signature against the
    signed-in user and reads only that user's own log rows, so a pasted
    or guessed id can never reach anyone else's conversation."""
    value = d.get('conversation_id')
    return str(value)[:64] if isinstance(value, str) and value else None


@bp.route('/api/copilot/ask/stream', methods=['POST'])
def api_ask_stream():
    """§6.3 — the same answer, in stages, over server-sent events.

    The scope is resolved before the generator starts. stream_with_context
    would keep the request context alive either way, so this is not
    load-bearing — it is deliberate: the boundary is fixed at the moment
    the question was asked and cannot be re-read part-way through a
    response, whatever a later refactor does to the context handling.
    """
    import json

    from flask import Response, stream_with_context

    if not _authed():
        return jsonify(ok=False, error='Not authenticated'), 401

    d = request.get_json(silent=True) or {}
    ctx = d.get('context')
    sc = scope_mod.current()
    actor = _actor()
    conversation = _conversation(d)

    def events():
        try:
            for chunk in svc.ask_stream(
                    (d.get('q') or '')[:1000], sc=sc,
                    history=d.get('history') or [],
                    context=ctx if isinstance(ctx, dict) else None,
                    conversation_id=conversation, actor=actor):
                yield f'data: {json.dumps(chunk, default=str)}\n\n'
        except Exception:
            yield ('data: ' + json.dumps(
                {'stage': 'done', 'ok': False,
                 'prose': 'Something went wrong. Nothing was changed.'})
                + '\n\n')

    return Response(stream_with_context(events()),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache',
                             'X-Accel-Buffering': 'no'})


@bp.route('/api/copilot/suggestions', methods=['GET'])
def api_suggestions():
    if not _authed():
        return jsonify(ok=False, error='Not authenticated'), 401
    sc = scope_mod.current()
    health = model_mod.health()
    if not sc.can('admin.access'):
        # Every user saw the internal model host here. They need to know
        # whether it is available, not where it lives.
        health = {'available': health.get('available'),
                  'reason': health.get('reason')}
    return jsonify(ok=True, suggestions=svc.suggestions(sc),
                   scope_note=svc._scope_note(sc),
                   model=health)


@bp.route('/api/copilot/feedback', methods=['POST'])
def api_feedback():
    if not _authed():
        return jsonify(ok=False, error='Not authenticated'), 401
    d = request.get_json(silent=True) or {}
    ok, err = svc.record_feedback(
        d.get('log_id'), helpful=bool(d.get('helpful')),
        reason=d.get('reason'), note=d.get('note'), actor=_actor())
    if not ok:
        db.session.rollback()
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, reasons=list(svc.FEEDBACK_REASONS))


@bp.route('/api/copilot/pin', methods=['POST'])
def api_pin():
    """§6.4 — pin a question so it can be re-asked, not a frozen answer."""
    if not _authed():
        return jsonify(ok=False, error='Not authenticated'), 401
    d = request.get_json(silent=True) or {}
    ok, err = svc.pin(d.get('log_id'), pinned=bool(d.get('pinned', True)),
                      actor=_actor())
    if not ok:
        db.session.rollback()
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, pinned=svc.pinned_for(_actor()))


@bp.route('/api/copilot/brief', methods=['GET'])
def api_brief():
    """§6.5 — the morning brief, for the panel's first run and for a
    dashboard tile later."""
    if not _authed():
        return jsonify(ok=False, error='Not authenticated'), 401
    sc = scope_mod.current()
    return jsonify(ok=True, brief=svc.morning_brief(sc),
                   pinned=svc.pinned_for(_actor()))


# ── §10 / Phase 5 — controlled actions, off unless enabled ───────────
@bp.route('/api/copilot/actions', methods=['GET'])
def api_actions():
    if not _authed():
        return jsonify(ok=False, error='Not authenticated'), 401
    from app.copilot import actions
    return jsonify(ok=True, enabled=actions.enabled(),
                   actions=actions.available(scope_mod.current()))


@bp.route('/api/copilot/actions/propose', methods=['POST'])
def api_propose():
    """Describes a change. Writes nothing."""
    if not _authed():
        return jsonify(ok=False, error='Not authenticated'), 401
    from app.copilot import actions

    d = request.get_json(silent=True) or {}
    proposal, err = actions.propose(d.get('action'), scope_mod.current(),
                                    d.get('params') or {})
    if err:
        return jsonify(ok=False, error=err), 400
    return jsonify(ok=True, proposal=proposal.to_dict())


@bp.route('/api/copilot/actions/commit', methods=['POST'])
def api_commit():
    """Applies a change, given the token from its own proposal."""
    if not _authed():
        return jsonify(ok=False, error='Not authenticated'), 401
    from app.copilot import actions

    d = request.get_json(silent=True) or {}
    result, err = actions.commit(
        d.get('action'), scope_mod.current(), d.get('params') or {},
        d.get('token'), actor=_actor())
    if err:
        db.session.rollback()
        return jsonify(ok=False, error=err), 400
    _audit_action(d.get('action'), d.get('params') or {}, result)
    from app.services import audit
    audit.record('copilot.action_commit', 'copilot_action',
                 d.get('action'), new={'params': d.get('params') or {}},
                 commit=True)
    return jsonify(ok=True, **(result or {}))


def _audit_action(action_key, params, result):
    """§10 requires a full audit on every write."""
    from app.models.copilot import CopilotLog
    try:
        db.session.add(CopilotLog(
            emp_code=_actor(),
            question=f'[action] {action_key} {params}'[:1000],
            intent=f'action:{action_key}',
            data_scope=scope_mod.current().data_scope,
            sources=(result or {}).get('message', '')[:500],
            answered=True, model_used=False, latency_ms=0))
        db.session.commit()
    except Exception:
        db.session.rollback()


# ── admin: adoption analytics, §11 ───────────────────────────────────
@bp.route('/copilot-analytics')
@require('admin.access')
def analytics_page():
    return render_template('copilot/analytics.html')


@bp.route('/api/copilot/glossary', methods=['GET'])
def api_glossary():
    """§7 — the words the Copilot knows.

    Readable by anyone signed in, not just an admin: a salesperson
    wondering why "ODC" was read as Project Logistics is entitled to see
    the list that decided it. There is nothing confidential in a
    vocabulary.
    """
    if not _authed():
        return jsonify(ok=False, error='Not authenticated'), 401
    from app.copilot import vocabulary
    return jsonify(ok=True, **vocabulary.glossary())


@bp.route('/api/copilot/analytics', methods=['GET'])
@require('admin.access')
def api_analytics():
    from datetime import datetime, timedelta

    from app.models.copilot import CopilotLog

    days = min(int(request.args.get('days') or 30), 365)
    since = datetime.utcnow() - timedelta(days=days)
    rows = CopilotLog.query.filter(CopilotLog.created_at >= since).all()

    total = len(rows)
    answered = sum(1 for r in rows if r.answered)
    unmatched = [r for r in rows if not r.intent]
    slow = sorted((r for r in rows if (r.latency_ms or 0) > 2000),
                  key=lambda r: -(r.latency_ms or 0))[:20]
    thumbs = [r for r in rows if r.helpful is not None]
    good = sum(1 for r in thumbs if r.helpful)

    by_intent = {}
    for r in rows:
        key = r.intent or '(unrecognised)'
        by_intent[key] = by_intent.get(key, 0) + 1

    return jsonify(
        ok=True, days=days, total=total, answered=answered,
        answer_rate=(round(100 * answered / total) if total else None),
        rated=len(thumbs), helpful=good,
        satisfaction=(round(100 * good / len(thumbs)) if thumbs else None),
        by_intent=sorted(
            ({'intent': k, 'count': v} for k, v in by_intent.items()),
            key=lambda d: -d['count']),
        # The most useful column on the screen: what people asked that
        # the catalogue could not answer is the backlog.
        unmatched=[r.to_dict() for r in unmatched[-40:]],
        slow=[r.to_dict() for r in slow],
        model=model_mod.health(),
        index=_index_health())


def _index_health():
    """§4 — whether text search can answer anything."""
    from app.copilot import retrieval
    return retrieval.stats()
