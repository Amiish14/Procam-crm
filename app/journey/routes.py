"""
One lead record, and the funnel behind it.

    GET  /leads/<id>/journey          the record
    GET  /api/journey/<id>            the 8 + 12 flowchart
    POST /api/journey/<id>/activity   Log activity (§6.2)
    POST /api/journey/<id>/channel    set the channel by hand
    POST /api/journey/<id>/po         step 11
    GET  /api/journey/targets         §6.3
    POST /api/journey/targets         set one target

CRM-LEAD-JOURNEY-01 (R4). The flowchart routes answer 404 while
FEATURE_LEAD_JOURNEY is off; the activity and target routes while
FEATURE_PRE_RFQ_FUNNEL is off. They are separate flags because
the flowchart is a way of looking and the activity log changes
data — one can be shown to people a week before the other.
"""
from __future__ import annotations

from functools import wraps

from flask import Blueprint, jsonify, render_template, request, session

from app.services import lead_journey as lj

bp = Blueprint('journey', __name__)


def _need(flag_fn):
    def outer(f):
        @wraps(f)
        def wrap(*a, **kw):
            if not flag_fn():
                return jsonify(ok=False, error='Not found'), 404
            if not session.get('emp_code'):
                return jsonify(ok=False,
                               error='Not authenticated'), 401
            return f(*a, **kw)
        return wrap
    return outer


_journey = _need(lj.journey_on)
_funnel = _need(lj.funnel_on)


def _lead_or_404(lead_id):
    from app import Lead
    from app.access import scope as sc_mod

    lead = sc_mod.leads(Lead.query.filter(Lead.id == lead_id)).first()
    if lead is None:
        return None, (jsonify(ok=False, error='Lead not found'), 404)
    return lead, None


@bp.route('/leads/<int:lead_id>/journey')
def journey_page(lead_id):
    if not lj.journey_on():
        return jsonify(ok=False, error='Not found'), 404
    if not session.get('emp_code'):
        from flask import redirect, url_for
        return redirect(url_for('login'))
    return render_template('lead_journey.html', lead_id=lead_id)


@bp.route('/api/journey/<int:lead_id>')
@_journey
def api_journey(lead_id):
    from app import Company, db
    from app.services import rfq_chain

    lead, refusal = _lead_or_404(lead_id)
    if refusal:
        return refusal
    account = (db.session.get(Company, lead.company_id)
               if lead.company_id else None)
    return jsonify(
        ok=True,
        lead={'id': lead.id, 'company': lead.company or '',
              'stage': lead.stage or '',
              'owner': lead.assigned_to or '',
              'vertical': lead.procam_vertical or '',
              'channel': lead.lead_channel or '',
              'channel_auto': bool(lead.lead_channel_auto),
              'account': getattr(account, 'name', '') or '',
              'account_id': lead.company_id,
              'expected_rfq_timing': lead.expected_rfq_timing or '',
              'capability_fit': lead.capability_fit or '',
              'rfq_expected_date': str(lead.rfq_expected_date or '')},
        journey=rfq_chain.journey(lead),
        kinds=list(lj.KINDS),
        asks={k: v[0] for k, v in lj.ASKS.items()},
        lists={'expected_rfq_timing': list(lj.EXPECTED_TIMING),
               'capability_fit': list(lj.CAPABILITY_FIT),
               'manual_channels': list(lj.MANUAL_CHANNELS)})


@bp.route('/api/journey/<int:lead_id>/activity', methods=['POST'])
@_funnel
def api_activity(lead_id):
    from app import db

    lead, refusal = _lead_or_404(lead_id)
    if refusal:
        return refusal
    d = request.get_json(silent=True) or {}
    ok, detail = lj.log(lead, kind=(d.get('kind') or '').strip(),
                        actor=session['emp_code'],
                        contact_id=d.get('contact_id'),
                        note=(d.get('note') or ''),
                        value=d.get('value'))
    if not ok:
        db.session.rollback()
        return jsonify(ok=False, error=detail), 400
    db.session.commit()
    return jsonify(ok=True, **detail)


@bp.route('/api/journey/<int:lead_id>/channel', methods=['POST'])
@_funnel
def api_channel(lead_id):
    from app import db

    lead, refusal = _lead_or_404(lead_id)
    if refusal:
        return refusal
    d = request.get_json(silent=True) or {}
    chosen = (d.get('channel') or '').strip()
    if chosen and chosen not in lj.MANUAL_CHANNELS:
        return jsonify(ok=False,
                       error=f'the CRM sets {", ".join(lj.AUTOMATIC)} '
                             f'itself — choose one of the '
                             f'rest'), 400
    found, why = lj.set_channel(lead, actor=session['emp_code'],
                                channel=chosen or None)
    db.session.commit()
    return jsonify(ok=bool(found), channel=found, why=why)


@bp.route('/api/journey/<int:lead_id>/po', methods=['POST'])
@_funnel
def api_po(lead_id):
    """Step 11 — the PO or LOI, and Won."""
    from app import db
    from app.services import audit, rfq_chain

    lead, refusal = _lead_or_404(lead_id)
    if refusal:
        return refusal
    d = request.get_json(silent=True) or {}
    po = (d.get('po_number') or '').strip()
    if not po:
        return jsonify(ok=False,
                       error='a PO or LOI reference is required'), 400

    rfq = rfq_chain._rfq_of_lead(lead)
    detail = None
    if rfq is not None:
        ok, detail = rfq_chain.on_po_recorded(
            rfq, actor=session['emp_code'], po_number=po,
            amount=d.get('amount'),
            awarded_on=d.get('awarded_on'))
        if not ok and isinstance(detail, str):
            db.session.rollback()
            return jsonify(ok=False, error=detail), 400

    was = lead.stage
    lead.stage = 'Won'
    audit.record('lead.won_on_po', 'lead', lead.id,
                 old={'stage': was},
                 new={'stage': 'Won', 'po_number': po},
                 actor=session['emp_code'],
                 reason=f'PO {po} recorded')
    db.session.commit()
    return jsonify(ok=True, po_number=po, step11=detail)


# ── §6.3 targets ─────────────────────────────────────────────────────
@bp.route('/api/journey/targets')
@_funnel
def api_targets():
    month = (request.args.get('month') or '').strip() or None
    vertical = (request.args.get('vertical') or '').strip() or None
    rows = lj.targets_for(month=month, vertical=vertical)
    return jsonify(ok=True, month=month or lj.month_key(),
                   rows=rows, total=len(rows))


@bp.route('/api/journey/targets', methods=['POST'])
@_funnel
def api_set_target():
    from app import Employee, db
    from app.models.rfq_target import RfqTarget
    from app.services import audit

    emp = Employee.query.filter_by(
        emp_code=session['emp_code']).first()
    if (getattr(emp, 'role', '') or '').lower() not in (
            'admin', 'super admin', 'superadmin', 'director'):
        return jsonify(ok=False,
                       error='Only an admin or a director sets '
                             'targets.'), 403

    d = request.get_json(silent=True) or {}
    month = (d.get('month') or lj.month_key()).strip()
    vertical = (d.get('vertical') or '').strip()
    code = (d.get('emp_code') or '').strip().upper()
    try:
        target = int(d.get('target_rfqs') or 0)
    except (TypeError, ValueError):
        return jsonify(ok=False, error='target must be a number'), 400
    if not (vertical and code):
        return jsonify(ok=False,
                       error='vertical and emp_code are both '
                             'required'), 400

    row = RfqTarget.query.filter_by(month=month, vertical=vertical,
                                    emp_code=code).first()
    was = getattr(row, 'target_rfqs', None)
    if row is None:
        row = RfqTarget(month=month, vertical=vertical,
                        emp_code=code)
        db.session.add(row)
    row.target_rfqs = target
    row.updated_by = session['emp_code']
    audit.record('rfq_target.set', 'rfq_target',
                 f'{month}/{vertical}/{code}',
                 old={'target_rfqs': was},
                 new={'target_rfqs': target},
                 actor=session['emp_code'],
                 reason='RFQ target set')
    db.session.commit()
    return jsonify(ok=True, **row.to_dict())
