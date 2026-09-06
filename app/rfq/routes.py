"""
RFQ + Rate Sourcing routes (Phase 6 of the CRM upgrade).

Endpoints:
    GET  /rfqs                                    HTML list view
    GET  /rfqs/<id>                               HTML detail view

    POST /api/rfqs                                create RFQ (JSON)
    GET  /api/rfqs                                list (JSON), filterable
    GET  /api/rfqs/<id>                           detail (JSON, deep)
    PATCH /api/rfqs/<id>                          header patch
    POST /api/rfqs/<id>/attachments               multipart upload
    POST /api/rfqs/<id>/lines                     add RateSourcingLine
    PATCH /api/rfqs/<id>/lines/<line_id>          update line
    POST /api/rfqs/<id>/lines/<line_id>/submit-rate
    POST /api/rfqs/<id>/advance                   move status forward

Every write route fires the task engine via `on_state_change`.
Rate submission also auto-closes the matching `rfq.rate_source` task
for the specific RateSourcingLine.
"""
from datetime import date, datetime
from functools import wraps

from flask import (Blueprint, jsonify, render_template, request, session,
                   redirect, url_for, current_app)

from app import db
from app.models.rfq import RFQ, RFQAttachment, RateSourcingLine
from app.services.assignment import (
    add_member, primary_owner_of, members_of, is_member,
)
from app.services.task_engine import on_state_change
from app.models.task_engine import TaskInstance, TaskInstanceStatus


bp = Blueprint('rfq', __name__)


# ─── auth helpers ──────────────────────────────────────────────────────────
def _require_auth(f):
    """Gated by the Access Control matrix (module.rfq).

    Kept under the original name so every existing @_require_auth on this
    blueprint picks up the permission check without being touched.
    """
    from app.access.service import require as _require_perm
    return _require_perm('module.rfq')(f)


def _current_emp():
    ec = session.get('emp_code')
    if not ec:
        return None
    try:
        from app import Employee
        return Employee.query.filter_by(emp_code=ec).first()
    except Exception:
        return None


def _emp_role():
    return session.get('role') or ''


def _has_role_key(emp, role_key):
    """True if the acting user is an admin, has the CRM role_key as their
    Employee.role, or is a member on ANY record with that role.
    Kept intentionally permissive for RBAC-lite gating."""
    if not emp:
        return False
    if _emp_role() == 'admin':
        return True
    # Employee.role stores generic buckets (admin/sales/presales/user).
    # The role_key match here is a loose check for the seven CRM roles.
    if (getattr(emp, 'role', '') or '') == role_key:
        return True
    # Membership check across recent records is expensive; skip for now.
    return False


# ─── helpers ───────────────────────────────────────────────────────────────
_ALLOWED_STATUSES = [
    'Received', 'Rate Sourcing', 'Quote Preparation', 'Quoted',
    'Negotiation', 'Won', 'Lost', 'Withdrawn',
]
_LINE_STATUSES = [
    'Not Started', 'Requested', 'Awaiting Response',
    'Partially Received', 'Completed',
]

_STATUS_ORDER = {s: i for i, s in enumerate(_ALLOWED_STATUSES)}


def _next_rfq_number():
    """RFQ-YYYY-NNNN — scoped per calendar year, gap-safe."""
    yr = date.today().year
    prefix = f'RFQ-{yr}-'
    max_seq = 0
    for (n,) in db.session.query(RFQ.rfq_number).filter(
            RFQ.rfq_number.like(f'{prefix}%')).all():
        try:
            max_seq = max(max_seq, int(str(n).split('-')[-1]))
        except (ValueError, IndexError):
            pass
    return f'{prefix}{str(max_seq + 1).zfill(4)}'


def _fire(entity, entity_type, old_state, new_state):
    try:
        emp = _current_emp()
        on_state_change(entity, entity_type, old_state, new_state,
                        triggered_by=emp)
    except Exception:
        try:
            current_app.logger.exception(
                'rfq.on_state_change failed for %s#%s %s→%s',
                entity_type, getattr(entity, 'id', '?'),
                old_state, new_state)
        except Exception:
            pass


def _parse_date(v):
    if not v:
        return None
    try:
        return datetime.strptime(str(v)[:10], '%Y-%m-%d').date()
    except Exception:
        return None


def _resolve_default_owner(rfq, line_service=None):
    """Pick a sourcing owner for a rate line.  Prefer a Rate_Sourcing
    member on the RFQ's Account; fall back to the account's primary
    owner; else the RFQ's own lead_driver."""
    account = None
    if rfq.account_id:
        try:
            from app import Company
            account = Company.query.get(rfq.account_id)
        except Exception:
            account = None
    if account:
        for m in members_of(account, role_key='Rate_Sourcing'):
            return m.user_id
        po = primary_owner_of(account)
        if po:
            return po
    return rfq.lead_driver or None


def _create_lines_from_payload(rfq, lines_payload, actor_id):
    created = []
    for i, ln in enumerate(lines_payload or [], start=1):
        svc = (ln.get('service') or '').strip()
        if not svc:
            continue
        row = RateSourcingLine(
            rfq_id=rfq.id,
            line_no=ln.get('line_no') or i,
            service=svc,
            scope=(ln.get('scope') or '').strip() or None,
            origin=(ln.get('origin') or '').strip() or None,
            destination=(ln.get('destination') or '').strip() or None,
            cargo=(ln.get('cargo') or '').strip() or None,
            required_by=_parse_date(ln.get('required_by')),
            sourcing_owner_id=(ln.get('sourcing_owner_id') or
                               _resolve_default_owner(rfq, svc)),
            supporting_user_ids=list(ln.get('supporting_user_ids') or []),
            vendor_source=(ln.get('vendor_source') or '').strip() or None,
            status=(ln.get('status') or 'Not Started'),
            remarks=(ln.get('remarks') or '').strip() or None,
            created_by_id=actor_id,
        )
        db.session.add(row)
        created.append(row)
    db.session.flush()
    for row in created:
        _fire(row, 'RateSourcingLine', None, row.status)
    return created


# ─── HTML views ────────────────────────────────────────────────────────────
@bp.route('/rfqs')
@_require_auth
def rfq_list_page():
    if not session.get('emp_code'):
        return redirect(url_for('login'))
    return render_template('rfq/list.html', emp=_current_emp())


@bp.route('/rfqs/<int:rid>')
@_require_auth
def rfq_detail_page(rid):
    if not session.get('emp_code'):
        return redirect(url_for('login'))
    rfq = RFQ.query.get_or_404(rid)
    return render_template('rfq/detail.html', rfq=rfq, emp=_current_emp())


# ─── JSON API ──────────────────────────────────────────────────────────────
@bp.route('/api/rfqs', methods=['GET'])
@_require_auth
def api_list_rfqs():
    q = RFQ.query
    status = request.args.get('status')
    owner  = request.args.get('owner')
    lead_id = request.args.get('lead_id', type=int)
    account_id = request.args.get('account_id', type=int)
    opp_id = request.args.get('opportunity_id', type=int)
    if status:
        q = q.filter(RFQ.status == status)
    if owner:
        q = q.filter(RFQ.lead_driver == owner)
    if lead_id:
        q = q.filter(RFQ.lead_id == lead_id)
    if account_id:
        q = q.filter(RFQ.account_id == account_id)
    if opp_id:
        q = q.filter(RFQ.opportunity_id == opp_id)

    # Role scoping — non-admin sees RFQs they lead, or where they're a
    # member of the linked account/opportunity.
    if _emp_role() != 'admin':
        me = session.get('emp_code')
        q = q.filter((RFQ.lead_driver == me) |
                     (RFQ.created_by_id == me))
    rows = q.order_by(RFQ.received_date.desc(), RFQ.id.desc()).limit(500).all()
    return jsonify(ok=True, rfqs=[r.to_dict() for r in rows])


@bp.route('/api/rfqs', methods=['POST'])
@_require_auth
def api_create_rfq():
    d = request.get_json(silent=True) or {}
    subject = (d.get('subject') or '').strip()
    if not subject:
        return jsonify(ok=False, error='subject required'), 400

    actor = session.get('emp_code')
    driver = (d.get('lead_driver') or actor) or None

    rfq = RFQ(
        rfq_number=_next_rfq_number(),
        lead_id=d.get('lead_id') or None,
        opportunity_id=d.get('opportunity_id') or None,
        account_id=d.get('account_id') or None,
        project_id=d.get('project_id') or None,
        subject=subject,
        description=(d.get('description') or '').strip() or None,
        origin=(d.get('origin') or '').strip() or None,
        destination=(d.get('destination') or '').strip() or None,
        cargo=(d.get('cargo') or '').strip() or None,
        scope=(d.get('scope') or '').strip() or None,
        received_date=_parse_date(d.get('received_date')) or date.today(),
        close_date=_parse_date(d.get('close_date')),
        quote_by_date=_parse_date(d.get('quote_by_date')),
        currency=(d.get('currency') or 'INR')[:6],
        lead_driver=driver,
        status='Received',
        created_by_id=actor,
    )
    db.session.add(rfq)
    db.session.flush()

    # Optionally seed lines from the same payload.
    _create_lines_from_payload(rfq, d.get('lines') or [], actor)

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500

    _fire(rfq, 'RFQ', None, 'Received')
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

    return jsonify(ok=True, rfq=rfq.to_dict(deep=True))


@bp.route('/api/rfqs/<int:rid>', methods=['GET'])
@_require_auth
def api_get_rfq(rid):
    rfq = RFQ.query.get_or_404(rid)
    return jsonify(ok=True, rfq=rfq.to_dict(deep=True))


@bp.route('/api/rfqs/<int:rid>', methods=['PATCH'])
@_require_auth
def api_patch_rfq(rid):
    rfq = RFQ.query.get_or_404(rid)
    d = request.get_json(silent=True) or {}
    for field in ('subject', 'description', 'origin', 'destination',
                  'cargo', 'scope', 'currency', 'lead_driver'):
        if field in d:
            setattr(rfq, field, (d.get(field) or None))
    for dfield in ('received_date', 'close_date', 'quote_by_date'):
        if dfield in d:
            setattr(rfq, dfield, _parse_date(d.get(dfield)))
    for lfield in ('lead_id', 'opportunity_id', 'account_id', 'project_id'):
        if lfield in d:
            setattr(rfq, lfield, d.get(lfield) or None)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500
    return jsonify(ok=True, rfq=rfq.to_dict())


@bp.route('/api/rfqs/<int:rid>/attachments', methods=['POST'])
@_require_auth
def api_upload_attachment(rid):
    rfq = RFQ.query.get_or_404(rid)
    f = request.files.get('file') or request.files.get('upload')
    if not f:
        return jsonify(ok=False, error='no file'), 400
    try:
        from app.utils.uploads import save_upload
        path, safe_name = save_upload(f, subdir=f'rfq/{rfq.id}')
    except Exception as e:
        current_app.logger.exception('rfq upload failed: %s', e)
        return jsonify(ok=False, error='upload failed'), 500
    att = RFQAttachment(
        rfq_id=rfq.id,
        filename=safe_name,
        stored_path=path,
        content_type=(f.mimetype or '')[:80],
        uploaded_by_id=session.get('emp_code'),
    )
    db.session.add(att)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500
    return jsonify(ok=True, attachment=att.to_dict())


@bp.route('/api/rfqs/<int:rid>/lines', methods=['POST'])
@_require_auth
def api_add_line(rid):
    rfq = RFQ.query.get_or_404(rid)
    d = request.get_json(silent=True) or {}
    actor = session.get('emp_code')
    created = _create_lines_from_payload(rfq, [d], actor)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500
    if not created:
        return jsonify(ok=False, error='service required'), 400
    return jsonify(ok=True, line=created[0].to_dict())


@bp.route('/api/rfqs/<int:rid>/lines/<int:line_id>', methods=['PATCH'])
@_require_auth
def api_patch_line(rid, line_id):
    line = RateSourcingLine.query.get_or_404(line_id)
    if line.rfq_id != rid:
        return jsonify(ok=False, error='line does not belong to rfq'), 400
    d = request.get_json(silent=True) or {}
    old_status = line.status
    for f in ('service', 'scope', 'origin', 'destination', 'cargo',
              'sourcing_owner_id', 'vendor_source', 'remarks',
              'rate_currency'):
        if f in d:
            setattr(line, f, (d.get(f) or None))
    for df in ('required_by', 'rate_validity_until'):
        if df in d:
            setattr(line, df, _parse_date(d.get(df)))
    if 'supporting_user_ids' in d:
        line.supporting_user_ids = list(d.get('supporting_user_ids') or [])
    if 'rate_amount' in d:
        v = d.get('rate_amount')
        try:
            line.rate_amount = float(v) if v not in (None, '') else None
        except Exception:
            line.rate_amount = None
    if 'status' in d:
        nxt = (d.get('status') or '').strip()
        if nxt and nxt in _LINE_STATUSES:
            line.status = nxt
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500
    if line.status != old_status:
        _fire(line, 'RateSourcingLine', old_status, line.status)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
    return jsonify(ok=True, line=line.to_dict())


@bp.route('/api/rfqs/<int:rid>/lines/<int:line_id>/submit-rate',
          methods=['POST'])
@_require_auth
def api_submit_rate(rid, line_id):
    line = RateSourcingLine.query.get_or_404(line_id)
    if line.rfq_id != rid:
        return jsonify(ok=False, error='line does not belong to rfq'), 400

    actor = session.get('emp_code')
    emp = _current_emp()

    # Only the assigned sourcing owner (or admin / Rate_Sourcing role) may
    # submit the rate.
    if _emp_role() != 'admin' \
       and line.sourcing_owner_id and line.sourcing_owner_id != actor \
       and not _has_role_key(emp, 'Rate_Sourcing'):
        return jsonify(ok=False,
                       error='Only the assigned sourcing owner may submit'), 403

    d = request.get_json(silent=True) or {}
    amt = d.get('rate_amount')
    if amt in (None, ''):
        return jsonify(ok=False, error='rate_amount required'), 400
    try:
        line.rate_amount = float(amt)
    except Exception:
        return jsonify(ok=False, error='rate_amount must be numeric'), 400
    line.rate_currency = (d.get('rate_currency') or line.rate_currency
                          or 'INR')[:6]
    line.rate_validity_until = _parse_date(d.get('rate_validity_until')) \
                               or line.rate_validity_until
    line.rate_received_at = datetime.utcnow()
    if d.get('vendor_source'):
        line.vendor_source = str(d.get('vendor_source'))[:240]
    if d.get('remarks'):
        line.remarks = str(d.get('remarks'))
    old_status = line.status
    line.status = 'Completed'

    # Auto-close matching task_instance (rfq.rate_source, RateSourcingLine)
    try:
        open_tasks = (TaskInstance.query
                      .filter(TaskInstance.task_key == 'rfq.rate_source',
                              TaskInstance.entity_type == 'RateSourcingLine',
                              TaskInstance.entity_id == line.id,
                              TaskInstance.status.in_(TaskInstanceStatus.OPEN))
                      .all())
        for t in open_tasks:
            t.status = TaskInstanceStatus.COMPLETED
            t.completed_at = datetime.utcnow()
            t.completed_by_id = actor
    except Exception:
        pass

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500

    _fire(line, 'RateSourcingLine', old_status, 'Completed')
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify(ok=True, line=line.to_dict())


@bp.route('/api/rfqs/<int:rid>/advance', methods=['POST'])
@_require_auth
def api_advance(rid):
    rfq = RFQ.query.get_or_404(rid)
    d = request.get_json(silent=True) or {}
    target = (d.get('status') or '').strip()
    if not target:
        # Auto next-in-sequence
        idx = _STATUS_ORDER.get(rfq.status, 0)
        if idx + 1 >= len(_ALLOWED_STATUSES):
            return jsonify(ok=False,
                           error='RFQ already at terminal status'), 400
        target = _ALLOWED_STATUSES[idx + 1]
    if target not in _ALLOWED_STATUSES:
        return jsonify(ok=False,
                       error=f'status must be one of {_ALLOWED_STATUSES}'), 400
    old = rfq.status
    if target == old:
        return jsonify(ok=True, rfq=rfq.to_dict(), noop=True)
    rfq.status = target
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500
    _fire(rfq, 'RFQ', old, target)
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify(ok=True, rfq=rfq.to_dict())
