"""
The quote submission due date's own endpoints.

    GET  /api/leads/<id>/quote-due            the date, its history,
                                              and any proposal
    POST /api/leads/<id>/quote-due            set or correct it
    POST /api/leads/<id>/quote-due/confirm    §2.3 — "auto, confirmed"
    POST /api/leads/<id>/quote-due/extend     §2.4 — with its reason
    POST /api/leads/<id>/quote-due/accept     §2.5 — take the proposal
    POST /api/leads/<id>/quote-due/dismiss    §2.5 — refuse it
    POST /api/leads/<id>/bid-outcome          §2.7 — Bid / Not bid

Not behind a feature switch, per §0.1: this code is live for everybody
the moment it is deployed. It is behind the ordinary CRM permission
rules — reading a lead is wide, changing one is the owner, the
secondary PIC or an administrator, exactly as the lead pop-up decides
it (CRM-LEAD-POPUP-02 §5).
"""
from __future__ import annotations

from functools import wraps

from flask import Blueprint, jsonify, request, session

from app import db
from app.services import guided_help

bp = Blueprint('quote_due', __name__)


def _me():
    return session.get('emp_code')


def _auth(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not _me():
            body, status = guided_help.refusal('SESSION_EXPIRED',
                                               http=401)
            return jsonify(body), status
        return f(*a, **kw)
    return wrap


def _is_admin():
    from app import Employee

    emp = Employee.query.filter_by(emp_code=_me()).first()
    role = (getattr(emp, 'role', '') or '').lower()
    return role in ('admin', 'super admin', 'superadmin', 'director',
                    'procam_admin')


def _lead_or_refusal(lead_id, *, editing=True):
    from app import Lead
    from app.access import scope as sc_mod

    lead = db.session.get(Lead, lead_id)
    if lead is None:
        body, status = guided_help.refusal(
            'NOT_YOUR_LEAD', {'lead_id': lead_id}, http=404)
        return None, (body, status)
    try:
        allowed = sc_mod.may_view(lead)
    except Exception:                                     # noqa: BLE001
        allowed = False
    if not allowed:
        body, status = guided_help.refusal(
            'NOT_YOUR_LEAD', {'lead_id': lead_id}, http=403)
        return None, (body, status)
    if editing:
        me = _me()
        own = me in ((lead.assigned_to or ''), (lead.secondary_owner or ''))
        if not own and not _is_admin():
            body, status = guided_help.refusal(
                'NOT_YOUR_LEAD',
                {'lead_id': lead.id, 'owner': lead.assigned_to or '',
                 'owner_name': lead.assigned_name or ''}, http=403)
            return None, (body, status)
    return lead, None


def _state(lead):
    """What every one of these endpoints answers with."""
    from app.models.cockpit02 import EXTENSION_REASONS
    from app.services import quote_due as qd, quote_due_flow as flow

    proposal = flow.pending(lead.id)
    reading = None
    if lead.quote_due_at is None:
        # Nothing on the lead: offer what the enquiry mail says, so
        # the box opens filled rather than empty (§2.3).
        try:
            reading = qd.read_lead(lead)
        except Exception:                                 # noqa: BLE001
            reading = None
    return {
        'lead_id': lead.id,
        # The stored moment, UTC, for anything that computes with it.
        'quote_due_at': (lead.quote_due_at.isoformat(timespec='minutes')
                         if lead.quote_due_at else ''),
        # The same moment in IST, for the <input type="datetime-local">
        # the person types into. These must be different fields: the
        # input showed the UTC string under a heading in IST, so the
        # card read "Quote due 02 Nov 23:04" with 17:34 in the box —
        # and pressing Save unchanged would have moved the deadline
        # five and a half hours earlier. A deadline that walks
        # backwards every time somebody opens the lead is worse than
        # no deadline at all.
        'quote_due_local': (qd.to_ist(lead.quote_due_at).isoformat(
            timespec='minutes') if lead.quote_due_at else ''),
        'shown': qd.shown(lead.quote_due_at),
        'source': lead.quote_due_source or '',
        'confirmed': bool(lead.quote_due_confirmed),
        'extensions': lead.quote_due_extensions or 0,
        'colour': qd.colour(lead.quote_due_at),
        'hours_left': qd.hours_left(lead.quote_due_at),
        'missed': bool(lead.quote_due_missed_at),
        'bid_outcome': lead.bid_outcome or '',
        'bid_reason': lead.bid_reason or '',
        'portal_key': lead.portal_key or '',
        'history': flow.history(lead.id),
        'reasons': [{'code': c, 'label': lbl}
                    for c, lbl in EXTENSION_REASONS],
        'suggested': (qd.shown(reading.at) if reading else ''),
        'suggested_iso': (qd.to_ist(reading.at).isoformat(
            timespec='minutes') if reading else ''),
        'suggested_source': (reading.source if reading else ''),
        'proposal': ({'id': proposal.id,
                      'shown': qd.shown(proposal.new_due_at),
                      'iso': qd.to_ist(proposal.new_due_at).isoformat(
                          timespec='minutes'),
                      'source': proposal.source or '',
                      'evidence': proposal.reason_text or ''}
                     if proposal else None),
    }


@bp.route('/api/leads/<int:lead_id>/quote-due')
@_auth
def api_get(lead_id):
    lead, refused = _lead_or_refusal(lead_id, editing=False)
    if refused:
        body, status = refused
        return jsonify(body), status
    return jsonify(ok=True, **_state(lead))


@bp.route('/api/leads/<int:lead_id>/quote-due', methods=['POST'])
@_auth
def api_set(lead_id):
    lead, refused = _lead_or_refusal(lead_id)
    if refused:
        body, status = refused
        return jsonify(body), status
    data = request.get_json(silent=True) or {}
    from app.services import cockpit02_gate as gate

    try:
        gate.apply_due(lead, data.get('quote_due_at'), actor=_me(),
                       source=(data.get('source') or 'user'),
                       reason_code='set',
                       reason_text=(data.get('note') or ''),
                       confirmed=True)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc),
                       needs='quote_due_at'), 400
    db.session.commit()
    return jsonify(ok=True, **_state(lead))


@bp.route('/api/leads/<int:lead_id>/quote-due/confirm',
          methods=['POST'])
@_auth
def api_confirm(lead_id):
    """§2.3 — the owner agrees with a date the CRM read.

    Its own endpoint rather than a flag on the save, because this is
    the one action with no field to fill: the whole of it is a person
    saying "yes, that is the deadline", and the tile stops asking.
    """
    lead, refused = _lead_or_refusal(lead_id)
    if refused:
        body, status = refused
        return jsonify(body), status
    if lead.quote_due_at is None:
        return jsonify(ok=False,
                       error='There is no date on this lead to '
                             'confirm.', needs='quote_due_at'), 400
    from app.services import audit

    lead.quote_due_confirmed = True
    audit.record('lead.quote_due_confirmed', 'lead', lead.id,
                 new={'quote_due_at': str(lead.quote_due_at)},
                 actor=_me(), reason='owner confirmed the due date',
                 commit=False)
    db.session.commit()
    return jsonify(ok=True, **_state(lead))


@bp.route('/api/leads/<int:lead_id>/quote-due/extend',
          methods=['POST'])
@_auth
def api_extend(lead_id):
    lead, refused = _lead_or_refusal(lead_id)
    if refused:
        body, status = refused
        return jsonify(body), status
    data = request.get_json(silent=True) or {}
    from app.services import quote_due_flow as flow

    ok, detail = flow.extend(
        lead, data.get('quote_due_at'),
        reason_code=(data.get('reason') or '').strip(),
        reason_text=(data.get('reason_text') or '').strip(),
        actor=_me(),
        email_message_id=(data.get('email_message_id') or None))
    if not ok:
        db.session.rollback()
        return jsonify(ok=False, error=detail), 400
    db.session.commit()
    return jsonify(ok=True, **_state(lead))


@bp.route('/api/leads/<int:lead_id>/quote-due/accept',
          methods=['POST'])
@_auth
def api_accept(lead_id):
    lead, refused = _lead_or_refusal(lead_id)
    if refused:
        body, status = refused
        return jsonify(body), status
    data = request.get_json(silent=True) or {}
    from app.services import quote_due_flow as flow

    ok, detail = flow.accept(
        lead, actor=_me(),
        reason_code=(data.get('reason') or 'client_extension'))
    if not ok:
        db.session.rollback()
        return jsonify(ok=False, error=detail), 400
    db.session.commit()
    return jsonify(ok=True, **_state(lead))


@bp.route('/api/leads/<int:lead_id>/quote-due/dismiss',
          methods=['POST'])
@_auth
def api_dismiss(lead_id):
    lead, refused = _lead_or_refusal(lead_id)
    if refused:
        body, status = refused
        return jsonify(body), status
    data = request.get_json(silent=True) or {}
    from app.services import quote_due_flow as flow

    ok, detail = flow.dismiss(lead, actor=_me(),
                              why=(data.get('why') or ''))
    if not ok:
        db.session.rollback()
        return jsonify(ok=False, error=detail), 400
    db.session.commit()
    return jsonify(ok=True, **_state(lead))


#: §2.7 — what the owner answers when a deadline passed.
BID_REASONS = (
    ('bid', 'Bid submitted'),
    ('not_bid_rate', 'Not bid — rate not workable'),
    ('not_bid_capacity', 'Not bid — no equipment or capacity'),
    ('not_bid_terms', 'Not bid — terms or payment unacceptable'),
    ('not_bid_late', 'Not bid — we saw it too late'),
    ('not_bid_other', 'Not bid — other'),
)
BID_CODES = dict(BID_REASONS)


@bp.route('/api/leads/<int:lead_id>/bid-outcome', methods=['POST'])
@_auth
def api_bid_outcome(lead_id):
    """Bid or Not bid, with a reason from the dropdown (§2.7).

    `bid_outcome` and `bid_reason` already existed on the lead and were
    empty on every row in the CRM, because nothing ever asked. This is
    what asks.
    """
    lead, refused = _lead_or_refusal(lead_id)
    if refused:
        body, status = refused
        return jsonify(body), status
    data = request.get_json(silent=True) or {}
    code = (data.get('outcome') or '').strip()
    if code not in BID_CODES:
        return jsonify(ok=False,
                       error='Pick Bid, or one of the reasons for not '
                             'bidding.',
                       options=[{'code': c, 'label': l}
                                for c, l in BID_REASONS]), 400
    note = (data.get('note') or '').strip()
    if code == 'not_bid_other' and not note:
        return jsonify(ok=False,
                       error='Say why, in a line.'), 400
    from app.services import audit

    lead.bid_outcome = 'bid' if code == 'bid' else 'not_bid'
    lead.bid_reason = (f'{BID_CODES[code]}'
                       + (f' — {note}' if note else ''))[:300]
    audit.record('lead.bid_outcome', 'lead', lead.id,
                 new={'outcome': lead.bid_outcome,
                      'reason': lead.bid_reason},
                 actor=_me(), reason='recorded after the deadline',
                 commit=False)
    db.session.commit()
    return jsonify(ok=True, outcome=lead.bid_outcome,
                   reason=lead.bid_reason)
