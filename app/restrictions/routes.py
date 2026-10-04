"""
The Client Block & Caution Register — screens and API.

    /admin/restrictions              the register
    /admin/restrictions/new          recommend or add
    /admin/restrictions/<id>         one decision and its whole history
    /admin/restrictions/<id>/impact  what approving it would close
    /admin/restrictions/<id>/approve | /reject | /lift
    /admin/restrictions.xlsx         export

    /api/client-restriction/check    read-only, for the TMS and anything
                                     else that starts business

Who may do what is in `restriction_register`, not here: this module
asks and renders. Two things are worth noticing in the access rules.

Everyone may **read** the register — that is the point of the caution
list, which exists so a new employee learns about a client before
dealing with them rather than afterwards. What everyone may not see is
the money: dispute amounts, references and attachments are for
administrators, because "we are in dispute" is something the whole
company needs and "they owe us ₹1.4 crore" is not.

And anyone may **recommend**. A block is usually first noticed by the
person being messed about, who is rarely an administrator.
"""
from __future__ import annotations

from functools import wraps

from flask import (Blueprint, abort, jsonify, redirect, render_template,
                   request, session, url_for)

from app.services import client_restrictions as restrictions
from app.services import restriction_register as register

bp = Blueprint('restrictions', __name__)


def _signed_in(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            if request.path.startswith('/api/'):
                return jsonify(ok=False, error='Not authenticated'), 401
            return redirect(url_for('login'))
        return f(*a, **kw)
    return wrap


def _me():
    return (session.get('emp_code') or '').upper()


def _row_or_404(rid):
    from app.models.restriction import ClientRestriction
    row = ClientRestriction.query.get(rid)
    if row is None:
        abort(404)
    return row


def _refused(exc):
    return jsonify(ok=False, error=exc.message), exc.status


# ── the register ─────────────────────────────────────────────────────
@bp.route('/admin/restrictions')
@_signed_in
def register_list():
    from app.models.restriction import ClientRestriction

    status = (request.args.get('status') or '').strip()
    reason = (request.args.get('reason') or '').strip()
    unit = (request.args.get('bu') or '').strip()

    query = ClientRestriction.query
    if status:
        query = query.filter(ClientRestriction.status == status)
    if reason:
        query = query.filter(ClientRestriction.reason_category == reason)
    rows = query.order_by(ClientRestriction.status,
                          ClientRestriction.company_name).all()
    if unit:
        rows = [r for r in rows if unit in r.business_units]

    from app.master_data import service as md
    sensitive = register.can_see_sensitive()
    return render_template(
        'restrictions/list.html',
        rows=rows, sensitive=sensitive,
        can_approve=register.can_approve(),
        pending=sum(1 for r in rows if r.status == 'recommended'),
        reasons=[i.label for i in md.items('restriction_reason')],
        status=status, reason=reason, bu=unit)


@bp.route('/admin/restrictions.xlsx')
@_signed_in
def export():
    """The register as a spreadsheet. Money only for those who may."""
    import io

    from openpyxl import Workbook

    from app.models.restriction import ClientRestriction

    sensitive = register.can_see_sensitive()
    wb = Workbook()
    ws = wb.active
    ws.title = 'Block register'
    headers = ['Client', 'Status', 'Reason', 'Detail', 'Recommended by',
               'Approved by', 'Approved on', 'Scope', 'Business units',
               'Review date']
    if sensitive:
        headers += ['Dispute amount', 'Currency', 'References',
                    'Legal status']
    ws.append(headers)
    for row in ClientRestriction.query.order_by(
            ClientRestriction.company_name).all():
        line = [row.company_name, row.status, row.reason_category or '',
                (row.reason_detail or '')[:500], row.recommended_by or '',
                row.approved_by or '',
                str(row.approved_at)[:10] if row.approved_at else '',
                row.scope, ', '.join(row.business_units),
                str(row.review_date) if row.review_date else '']
        if sensitive:
            line += [float(row.dispute_amount)
                     if row.dispute_amount is not None else '',
                     row.currency or '', row.dispute_refs or '',
                     row.legal_status or '']
        ws.append(line)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    from flask import send_file
    return send_file(buf, as_attachment=True,
                     download_name='client-block-register.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument'
                              '.spreadsheetml.sheet')


# ── adding ───────────────────────────────────────────────────────────
@bp.route('/admin/restrictions/new', methods=['GET', 'POST'])
@_signed_in
def new():
    from app.master_data import service as md

    if request.method == 'POST':
        data = request.get_json(silent=True) or request.form.to_dict()
        if 'aliases' in request.form:
            data['aliases'] = request.form.get('aliases')
        try:
            row = register.create(data, actor=_me())
        except register.RegisterError as exc:
            if request.is_json:
                return _refused(exc)
            return render_template('restrictions/form.html',
                                   error=exc.message, data=data,
                                   reasons=[i.label for i in
                                            md.items('restriction_reason')],
                                   can_approve=register.can_approve()), 400
        try:
            from app.services import restriction_notify as tell
            if row.status == 'recommended':
                tell.recommendation_raised(row, actor=_me())
            else:
                tell.announce_block(row, None)
        except Exception:
            pass
        if request.is_json:
            return jsonify(ok=True, id=row.id, status=row.status)
        return redirect(url_for('restrictions.detail', rid=row.id))

    from app.master_data import service as md2
    prefill = {
        'company_name': (request.args.get('company') or '').strip(),
        'domains': (request.args.get('domain') or '').strip(),
        'emails': (request.args.get('email') or '').strip(),
        'linked_account_id': request.args.get('account_id') or '',
    }
    return render_template('restrictions/form.html', data=prefill, error=None,
                           reasons=[i.label for i in
                                    md2.items('restriction_reason')],
                           can_approve=register.can_approve())


@bp.route('/api/restrictions/domain-warning')
@_signed_in
def domain_warning():
    """Does this domain reach further than this one company?"""
    domains = [d.strip() for d in
               (request.args.get('domains') or '').replace('\n', ',').split(',')
               if d.strip()]
    name = request.args.get('company') or ''
    return jsonify(warnings=register.shared_domain_warning(domains, name))


# ── one decision ─────────────────────────────────────────────────────
@bp.route('/admin/restrictions/<int:rid>')
@_signed_in
def detail(rid):
    from app.models.restriction import ClientRestrictionEvent, RestrictionAttachment

    row = _row_or_404(rid)
    sensitive = register.can_see_sensitive()
    events = (ClientRestrictionEvent.query
              .filter_by(restriction_id=rid)
              .order_by(ClientRestrictionEvent.created_at.desc(),
                        ClientRestrictionEvent.id.desc()).all())
    files = (RestrictionAttachment.query.filter_by(restriction_id=rid).all()
             if sensitive else [])
    attempts = [e for e in events if e.action == 'attempt_blocked']
    return render_template(
        'restrictions/detail.html', r=row, events=events, files=files,
        attempts=attempts, sensitive=sensitive,
        can_approve=register.can_approve(), can_lift=register.can_lift())


@bp.route('/admin/restrictions/<int:rid>/impact')
@_signed_in
def impact(rid):
    """What approving this would close. Looked at before it happens."""
    row = _row_or_404(rid)
    if not register.can_approve():
        return jsonify(ok=False, error='You cannot approve a block'), 403
    return jsonify(ok=True, impact=register.impact(row))


@bp.route('/admin/restrictions/<int:rid>/approve', methods=['POST'])
@_signed_in
def approve(rid):
    row = _row_or_404(rid)
    body = request.get_json(silent=True) or request.form.to_dict()
    close = str(body.get('close_records', 'true')).lower() not in ('false', '0')
    try:
        closed = register.approve(row, actor=_me(), close_records=close,
                                  note=(body.get('note') or ''))
    except register.RegisterError as exc:
        return _refused(exc)
    try:
        from app.services import restriction_notify as tell
        tell.owners_told(row, closed)
    except Exception:
        pass
    if request.is_json:
        return jsonify(ok=True, closed=closed.get('counts', {}))
    return redirect(url_for('restrictions.detail', rid=rid))


@bp.route('/admin/restrictions/<int:rid>/reject', methods=['POST'])
@_signed_in
def reject(rid):
    row = _row_or_404(rid)
    body = request.get_json(silent=True) or request.form.to_dict()
    try:
        register.reject(row, actor=_me(), reason=body.get('reason') or '')
    except register.RegisterError as exc:
        return _refused(exc)
    if request.is_json:
        return jsonify(ok=True)
    return redirect(url_for('restrictions.detail', rid=rid))


@bp.route('/admin/restrictions/<int:rid>/lift', methods=['POST'])
@_signed_in
def lift(rid):
    row = _row_or_404(rid)
    body = request.get_json(silent=True) or request.form.to_dict()
    try:
        register.lift(row, actor=_me(), reason=body.get('reason') or '',
                      downgrade_to=body.get('downgrade_to'))
    except register.RegisterError as exc:
        return _refused(exc)
    if request.is_json:
        return jsonify(ok=True)
    return redirect(url_for('restrictions.detail', rid=rid))


@bp.route('/admin/restrictions/<int:rid>/attachments', methods=['POST'])
@_signed_in
def upload(rid):
    """A notice, an email, a legal letter."""
    from app import db
    from app.models.restriction import RestrictionAttachment
    from app.utils import uploads

    row = _row_or_404(rid)
    if not register.can_see_sensitive():
        return jsonify(ok=False, error='Not yours to add'), 403
    file = request.files.get('file')
    if file is None or not file.filename:
        return jsonify(ok=False, error='Choose a file'), 400
    import os

    path, safe_name = uploads.save_upload(file, f'restrictions/{row.id}')
    att = RestrictionAttachment(
        restriction_id=row.id, filename=safe_name,
        content_type=file.mimetype,
        size_bytes=(os.path.getsize(path) if os.path.exists(path) else None),
        storage_path=path, uploaded_by=_me())
    db.session.add(att)
    db.session.commit()
    restrictions.log_event(row.id, 'edited', user_id=_me(),
                           note=f'attached {safe_name}')
    return jsonify(ok=True, attachment=att.to_dict())


@bp.route('/admin/restrictions/<int:rid>/attachments/<int:aid>')
@_signed_in
def download(rid, aid):
    from app.models.restriction import RestrictionAttachment
    from app.utils.uploads import send_safe_download

    if not register.can_see_sensitive():
        abort(403)
    att = RestrictionAttachment.query.filter_by(id=aid,
                                                restriction_id=rid).first()
    if att is None:
        abort(404)
    return send_safe_download(att.storage_path, att.filename)


# ── the check, for everything else ───────────────────────────────────
@bp.route('/api/client-restriction/check')
@_signed_in
def api_check():
    """Read-only. The TMS asks this before a Client PO, Job or LR.

    Signed in like everything else — the TMS runs on the same VM and
    can hold a session, and an unauthenticated endpoint that confirms
    which customers are in dispute is not one to leave open.
    """
    verdict = restrictions.check(
        company_name=request.args.get('company_name') or request.args.get('name'),
        email=request.args.get('email'),
        domain=request.args.get('domain'),
        gstin=request.args.get('gstin'),
        pan=request.args.get('pan'),
        account_id=request.args.get('account_id'),
        business_unit=request.args.get('business_unit'))
    out = verdict.to_dict()
    if not register.can_see_sensitive():
        out.pop('score', None)
    return jsonify(out)


@bp.route('/api/restrictions/badges', methods=['POST'])
@_signed_in
def badges():
    """{name: 'blocked'|'caution'} for a list of company names."""
    from app.services import restriction_gate as gate
    names = (request.get_json(silent=True) or {}).get('names') or []
    return jsonify(gate.badges_for([str(n) for n in names][:500]))
