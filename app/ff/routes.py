"""
Freight forwarding: rates in, quote out. All behind FEATURE_FF_CRM.

    GET  /api/ff/charge-groups              the vocabulary and the matrix
    GET  /api/ff/lines/<id>/rates           every request for a leg
    POST /api/ff/lines/<id>/rates/request   ask carriers or agents
    POST /api/ff/rates/<id>/reply           capture what one sent back
    POST /api/ff/rates/<id>/status          declined / no response
    GET  /api/ff/lines/<id>/compare         the replies, ranked
    POST /api/ff/rates/<id>/choose          pick one, with a reason
    POST /api/ff/quotes/<id>/price-line     a sell line from the choice
    GET  /api/ff/quotes/<id>/margin         sell, cost, margin

With the flag off every one of these answers 404, which is what an
unbuilt feature should look like from outside — not a 500 and not a
half-working screen.
"""
from __future__ import annotations

from datetime import datetime
from functools import wraps

from flask import Blueprint, jsonify, request, session

from app import db
from app.services import ff_charges, ff_rates

bp = Blueprint('ff', __name__)


def _on():
    from app.services import flags
    try:
        return bool(flags.on('FEATURE_FF_CRM'))
    except Exception:                                     # noqa: BLE001
        return False


def _gated(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not _on():
            # 404, not 403: the feature does not exist yet as far as
            # anything outside is concerned.
            return jsonify(ok=False, error='Not found'), 404
        if not session.get('emp_code'):
            return jsonify(ok=False, error='Not authenticated'), 401
        return f(*a, **kw)
    return wrap


def _me():
    return session.get('emp_code')


def _line(line_id):
    """A sourcing line the viewer may see, or None.

    Scoped through the lead behind the RFQ, so the Access Matrix
    governs this the way it governs everything else.
    """
    from app.access import scope as sc_mod
    from app.models.rfq import RFQ, RateSourcingLine
    from app import Lead

    line = db.session.get(RateSourcingLine, line_id)
    if line is None:
        return None
    rfq = db.session.get(RFQ, line.rfq_id)
    if rfq is None:
        return None
    if rfq.lead_id:
        seen = sc_mod.leads().filter(Lead.id == rfq.lead_id).first()
        if seen is None:
            return None
    return line


def _refuse(exc):
    return jsonify(ok=False, error=str(exc)), 400


@bp.route('/api/ff/charge-groups')
@_gated
def api_charge_groups():
    incoterm = (request.args.get('incoterm') or '').strip().upper()
    party = (request.args.get('party') or '').strip().title()
    out = {'groups': [{'key': k, 'label': l, 'covers': d}
                      for k, l, d in ff_charges.CHARGE_GROUPS],
           'incoterms': list(ff_charges.INCOTERMS),
           'parties': list(ff_charges.PARTIES)}
    if incoterm and party:
        try:
            billable = ff_charges.billable_groups(incoterm, party)
        except ff_charges.ChargeError as exc:
            return _refuse(exc)
        out['billable'] = sorted(billable)
        out['not_billable'] = sorted(set(ff_charges.GROUP_KEYS) - billable)
    return jsonify(ok=True, **out)


@bp.route('/api/ff/lines/<int:line_id>/rates')
@_gated
def api_line_rates(line_id):
    from app.models.ff import RateRequest
    line = _line(line_id)
    if line is None:
        return jsonify(ok=False, error='Not found'), 404
    rows = (RateRequest.query.filter_by(line_id=line_id)
            .order_by(RateRequest.id.asc()).all())
    return jsonify(ok=True, line=line.to_dict(),
                   rates=[r.to_dict() for r in rows])


@bp.route('/api/ff/lines/<int:line_id>/rates/request', methods=['POST'])
@_gated
def api_request_rates(line_id):
    line = _line(line_id)
    if line is None:
        return jsonify(ok=False, error='Not found'), 404
    body = request.get_json(silent=True) or {}
    due = (body.get('due_by') or '').strip()
    try:
        due_by = (datetime.strptime(due, '%Y-%m-%d').date()
                  if due else None)
    except ValueError:
        return jsonify(ok=False, error='due_by must be YYYY-MM-DD'), 400
    try:
        made, skipped = ff_rates.request_rates(
            line, body.get('vendors') or [], actor=_me(),
            due_by=due_by, note=(body.get('note') or '').strip() or None)
    except ff_rates.RateError as exc:
        db.session.rollback()
        return _refuse(exc)
    db.session.commit()
    return jsonify(ok=True, requested=[r.to_dict() for r in made],
                   already_asked=skipped)


@bp.route('/api/ff/rates/<int:rate_id>/reply', methods=['POST'])
@_gated
def api_record_reply(rate_id):
    from app.models.ff import RateRequest
    req = db.session.get(RateRequest, rate_id)
    if req is None or _line(req.line_id) is None:
        return jsonify(ok=False, error='Not found'), 404
    body = request.get_json(silent=True) or {}
    valid = (body.get('validity_until') or '').strip()
    try:
        until = (datetime.strptime(valid, '%Y-%m-%d').date()
                 if valid else None)
    except ValueError:
        return jsonify(ok=False,
                       error='validity_until must be YYYY-MM-DD'), 400
    try:
        ff_rates.record_reply(
            req, amount=body.get('amount'),
            currency=(body.get('currency') or 'INR'),
            validity_until=until, transit_days=body.get('transit_days'),
            remarks=(body.get('remarks') or '').strip() or None,
            actor=_me())
    except ff_rates.RateError as exc:
        db.session.rollback()
        return _refuse(exc)
    db.session.commit()
    return jsonify(ok=True, rate=req.to_dict())


@bp.route('/api/ff/rates/<int:rate_id>/status', methods=['POST'])
@_gated
def api_rate_status(rate_id):
    from app.models.ff import RateRequest
    req = db.session.get(RateRequest, rate_id)
    if req is None or _line(req.line_id) is None:
        return jsonify(ok=False, error='Not found'), 404
    body = request.get_json(silent=True) or {}
    try:
        ff_rates.mark(req, (body.get('status') or '').strip(), actor=_me())
    except ff_rates.RateError as exc:
        db.session.rollback()
        return _refuse(exc)
    db.session.commit()
    return jsonify(ok=True, rate=req.to_dict())


@bp.route('/api/ff/lines/<int:line_id>/compare')
@_gated
def api_compare(line_id):
    line = _line(line_id)
    if line is None:
        return jsonify(ok=False, error='Not found'), 404
    data = ff_rates.compare(line)
    return jsonify(ok=True, line=line.to_dict(),
                   replied=data['replied'], waiting=data['waiting'],
                   cheapest_inr=data['cheapest_inr'],
                   ranked=[{**{k: v for k, v in row.items()
                              if k != 'request'},
                            'rate': row['request'].to_dict()}
                           for row in data['ranked']],
                   no_reply=[r.to_dict() for r in data['no_reply']])


@bp.route('/api/ff/rates/<int:rate_id>/choose', methods=['POST'])
@_gated
def api_choose(rate_id):
    from app.models.ff import RateRequest
    req = db.session.get(RateRequest, rate_id)
    if req is None or _line(req.line_id) is None:
        return jsonify(ok=False, error='Not found'), 404
    body = request.get_json(silent=True) or {}
    try:
        ff_rates.choose(req, actor=_me(),
                        reason=(body.get('reason') or '').strip() or None)
    except ff_rates.RateError as exc:
        db.session.rollback()
        return _refuse(exc)
    db.session.commit()
    return jsonify(ok=True, rate=req.to_dict())


@bp.route('/api/ff/quotes/<int:quote_id>/price-line', methods=['POST'])
@_gated
def api_price_line(quote_id):
    from app.models.quote import Quote
    quote = db.session.get(Quote, quote_id)
    if quote is None:
        return jsonify(ok=False, error='Not found'), 404
    body = request.get_json(silent=True) or {}
    line = _line(body.get('line_id') or 0)
    if line is None:
        return jsonify(ok=False, error='No such sourcing line'), 404
    try:
        ql = ff_rates.quote_line_from(
            line, quote=quote, sell_amount=body.get('sell_amount'),
            markup_pct=body.get('markup_pct'))
    except ff_rates.RateError as exc:
        db.session.rollback()
        return _refuse(exc)
    quote.recompute_totals()
    db.session.commit()
    return jsonify(ok=True, line=ql.to_dict(),
                   margin=ff_rates.margin_summary(quote))


@bp.route('/api/ff/quotes/<int:quote_id>/margin')
@_gated
def api_margin(quote_id):
    from app.models.quote import Quote, QuoteLine
    quote = db.session.get(Quote, quote_id)
    if quote is None:
        return jsonify(ok=False, error='Not found'), 404
    summary = ff_rates.margin_summary(quote)
    out = {'ok': True, 'margin': summary}
    if quote.incoterm and quote.client_party:
        lines = QuoteLine.query.filter_by(quote_id=quote.id).all()
        try:
            billable, not_billable = ff_charges.split_lines(
                lines, quote.incoterm, quote.client_party)
        except ff_charges.ChargeError as exc:
            return _refuse(exc)
        out['incoterm'] = quote.incoterm
        out['client_party'] = quote.client_party
        out['billable_lines'] = [l.to_dict() for l in billable]
        out['not_billable_lines'] = [l.to_dict() for l in not_billable]
    return jsonify(**out)
