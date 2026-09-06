"""
Quote Management + Revision routes (Phase 7 of the CRM upgrade).

Endpoints:
    HTML:
        GET  /quotes                     list view
        GET  /quotes/<id>                detail view
        GET  /quotes/<id>/print          printable / PDF preview
        GET  /quotes/<id>/pdf            reportlab PDF (no-store)

    JSON:
        POST  /api/quotes                create (from RFQ or from scratch)
        GET   /api/quotes                list, filterable
        GET   /api/quotes/<id>           detail (deep)
        PATCH /api/quotes/<id>           header patch
        POST  /api/quotes/<id>/lines
        PATCH /api/quotes/<id>/lines/<line_id>
        POST  /api/quotes/<id>/submit-for-approval
        POST  /api/quotes/<id>/approve
        POST  /api/quotes/<id>/reject
        POST  /api/quotes/<id>/submit-to-client
        POST  /api/quotes/<id>/revise
        POST  /api/quotes/<id>/won
        POST  /api/quotes/<id>/lost

Every state transition calls `on_state_change` so the task engine picks
it up.  Maker/checker: `approve` refuses if approver == preparer.
"""
from datetime import date, datetime
from decimal import Decimal
from functools import wraps

from flask import (Blueprint, jsonify, render_template, request, session,
                   redirect, url_for, current_app, make_response)

from app import db
from app.models.quote import Quote, QuoteLine, QuoteRevisionLog
from app.models.rfq import RFQ, RateSourcingLine
from app.services.task_engine import on_state_change
from app.services.assignment import add_member, is_member


bp = Blueprint('quote', __name__)


# ─── auth helpers ──────────────────────────────────────────────────────────
def _require_auth(f):
    """Gated by the Access Control matrix (module.quotes).

    Kept under the original name so every existing @_require_auth on this
    blueprint picks up the permission check without being touched.
    """
    from app.access.service import require as _require_perm
    return _require_perm('module.quotes')(f)


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
    if not emp:
        return False
    if _emp_role() == 'admin':
        return True
    return (getattr(emp, 'role', '') or '') == role_key


_ALLOWED_STATUSES = [
    'Draft', 'Awaiting Approval', 'Approved', 'Submitted',
    'Under Negotiation', 'Won', 'Lost', 'Superseded', 'Withdrawn',
]


def _next_quote_number():
    """QUO-YYYY-NNNN, gap-safe within the calendar year."""
    yr = date.today().year
    prefix = f'QUO-{yr}-'
    max_seq = 0
    for (n,) in db.session.query(Quote.quote_number).filter(
            Quote.quote_number.like(f'{prefix}%')).all():
        try:
            max_seq = max(max_seq, int(str(n).split('-')[-1]))
        except (ValueError, IndexError):
            pass
    return f'{prefix}{str(max_seq + 1).zfill(4)}'


def _parse_date(v):
    if not v:
        return None
    try:
        return datetime.strptime(str(v)[:10], '%Y-%m-%d').date()
    except Exception:
        return None


def _fire(entity, entity_type, old_state, new_state):
    try:
        emp = _current_emp()
        on_state_change(entity, entity_type, old_state, new_state,
                        triggered_by=emp)
    except Exception:
        try:
            current_app.logger.exception(
                'quote.on_state_change failed for %s#%s %s→%s',
                entity_type, getattr(entity, 'id', '?'),
                old_state, new_state)
        except Exception:
            pass


# ─── HTML ──────────────────────────────────────────────────────────────────
@bp.route('/quotes')
@_require_auth
def quote_list_page():
    if not session.get('emp_code'):
        return redirect(url_for('login'))
    return render_template('quote/list.html', emp=_current_emp())


@bp.route('/quotes/<int:qid>')
@_require_auth
def quote_detail_page(qid):
    if not session.get('emp_code'):
        return redirect(url_for('login'))
    quote = Quote.query.get_or_404(qid)
    return render_template('quote/detail.html', quote=quote, emp=_current_emp())


@bp.route('/quotes/<int:qid>/print')
@_require_auth
def quote_print_page(qid):
    if not session.get('emp_code'):
        return redirect(url_for('login'))
    quote = Quote.query.get_or_404(qid)
    lines = (QuoteLine.query.filter_by(quote_id=qid)
             .order_by(QuoteLine.line_no.asc(), QuoteLine.id.asc()).all())
    return render_template('quote/print.html', quote=quote, lines=lines)


@bp.route('/quotes/<int:qid>/pdf')
@_require_auth
def quote_pdf(qid):
    if not session.get('emp_code'):
        return redirect(url_for('login'))
    quote = Quote.query.get_or_404(qid)
    lines = (QuoteLine.query.filter_by(quote_id=qid)
             .order_by(QuoteLine.line_no.asc(), QuoteLine.id.asc()).all())
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer,
                                        Table, TableStyle)
        import io
    except Exception:
        # Fall back to the printable HTML if reportlab is unavailable.
        return redirect(url_for('quote.quote_print_page', qid=qid))

    styles = getSampleStyleSheet()
    hdr = ParagraphStyle('hdr', parent=styles['Title'], fontSize=14,
                         alignment=1)
    body = styles['Normal']
    b = styles['BodyText']

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=18 * mm, rightMargin=18 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm,
                            title=f'Quote {quote.quote_number}')
    story = []
    story.append(Paragraph(f'Quote {quote.quote_number}', hdr))
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph(
        f'<b>Subject:</b> {quote.subject or ""}<br/>'
        f'<b>Date:</b> {quote.quote_date or ""} &nbsp;&nbsp; '
        f'<b>Valid Until:</b> {quote.validity_until or ""}<br/>'
        f'<b>Currency:</b> {quote.currency}<br/>'
        f'<b>Status:</b> {quote.status} &nbsp; (rev {quote.revision_number})',
        body))
    story.append(Spacer(1, 4 * mm))

    hdrs = ['#', 'Service', 'Description', 'Qty', 'Unit', 'Rate', 'Total']
    data = [hdrs]
    for i, ln in enumerate(lines, start=1):
        data.append([
            str(i),
            ln.service or '',
            (ln.description or '')[:80],
            f'{ln.quantity or 0}',
            ln.unit or '',
            f'{float(ln.unit_rate or 0):,.2f}',
            f'{float(ln.line_total or 0):,.2f}',
        ])
    tbl = Table(data, hAlign='LEFT', repeatRows=1,
                colWidths=[10 * mm, 30 * mm, 60 * mm, 15 * mm,
                           15 * mm, 25 * mm, 25 * mm])
    tbl.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.lightgrey),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.black),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('ALIGN', (3, 1), (-1, -1), 'RIGHT'),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(
        f'<b>Subtotal:</b> {float(quote.subtotal or 0):,.2f} &nbsp;&nbsp;'
        f'<b>Tax:</b> {float(quote.tax_amount or 0):,.2f} &nbsp;&nbsp;'
        f'<b>Total:</b> {float(quote.total_amount or 0):,.2f}', body))
    if quote.payment_terms:
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph(f'<b>Payment terms:</b> {quote.payment_terms}',
                               b))
    if quote.inclusions:
        story.append(Paragraph(f'<b>Inclusions:</b> {quote.inclusions}', b))
    if quote.exclusions:
        story.append(Paragraph(f'<b>Exclusions:</b> {quote.exclusions}', b))
    if quote.remarks:
        story.append(Paragraph(f'<b>Remarks:</b> {quote.remarks}', b))

    doc.build(story)
    buf.seek(0)
    resp = make_response(buf.getvalue())
    resp.headers['Content-Type'] = 'application/pdf'
    resp.headers['Content-Disposition'] = (
        f'inline; filename="quote_{quote.quote_number}.pdf"')
    resp.headers['Cache-Control'] = 'no-store'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp


# ─── JSON API ──────────────────────────────────────────────────────────────
@bp.route('/api/quotes', methods=['GET'])
@_require_auth
def api_list_quotes():
    q = Quote.query
    status = request.args.get('status')
    owner  = request.args.get('owner')
    rfq_id = request.args.get('rfq_id', type=int)
    opp_id = request.args.get('opportunity_id', type=int)
    if status:
        q = q.filter(Quote.status == status)
    if owner:
        q = q.filter(Quote.prepared_by_id == owner)
    if rfq_id:
        q = q.filter(Quote.rfq_id == rfq_id)
    if opp_id:
        q = q.filter(Quote.opportunity_id == opp_id)
    if _emp_role() != 'admin':
        me = session.get('emp_code')
        q = q.filter((Quote.prepared_by_id == me) |
                     (Quote.created_by_id == me))
    rows = q.order_by(Quote.quote_date.desc(), Quote.id.desc()).limit(500).all()
    return jsonify(ok=True, quotes=[r.to_dict() for r in rows])


@bp.route('/api/quotes', methods=['POST'])
@_require_auth
def api_create_quote():
    d = request.get_json(silent=True) or {}
    actor = session.get('emp_code')

    rfq = None
    rfq_id = d.get('rfq_id')
    if rfq_id:
        rfq = RFQ.query.get(rfq_id)

    quote = Quote(
        quote_number=_next_quote_number(),
        rfq_id=rfq.id if rfq else None,
        opportunity_id=d.get('opportunity_id') or (rfq.opportunity_id if rfq else None),
        lead_id=d.get('lead_id') or (rfq.lead_id if rfq else None),
        account_id=d.get('account_id') or (rfq.account_id if rfq else None),
        subject=(d.get('subject') or (rfq.subject if rfq else '') or '')[:240],
        quote_date=_parse_date(d.get('quote_date')) or date.today(),
        validity_until=_parse_date(d.get('validity_until')),
        currency=(d.get('currency') or (rfq.currency if rfq else 'INR'))[:6],
        payment_terms=(d.get('payment_terms') or '').strip() or None,
        inclusions=(d.get('inclusions') or '').strip() or None,
        exclusions=(d.get('exclusions') or '').strip() or None,
        remarks=(d.get('remarks') or '').strip() or None,
        status='Draft',
        prepared_by_id=(d.get('prepared_by_id') or actor),
        revision_number=1,
        created_by_id=actor,
    )
    db.session.add(quote)
    db.session.flush()

    # Seed lines: either explicit `lines` payload, or copy from RFQ.
    provided = d.get('lines') or []
    if provided:
        for i, ln in enumerate(provided, start=1):
            _append_quote_line(quote, ln, i)
    elif rfq:
        source_lines = (RateSourcingLine.query.filter_by(rfq_id=rfq.id)
                        .order_by(RateSourcingLine.line_no.asc(),
                                  RateSourcingLine.id.asc()).all())
        for i, sl in enumerate(source_lines, start=1):
            _append_quote_line(quote, {
                'service': sl.service,
                'description': sl.scope or sl.cargo or '',
                'quantity': 1,
                'unit': 'lot',
                'unit_rate': float(sl.rate_amount or 0),
            }, i)
    quote.recompute_totals()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500

    _fire(quote, 'Quote', None, 'Draft')
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify(ok=True, quote=quote.to_dict(deep=True))


def _append_quote_line(quote, d, default_line_no):
    ln = QuoteLine(
        quote_id=quote.id,
        line_no=d.get('line_no') or default_line_no,
        service=(d.get('service') or '')[:80] or None,
        description=(d.get('description') or '').strip() or None,
        quantity=Decimal(str(d.get('quantity', 1) or 1)),
        unit=(d.get('unit') or '')[:24] or None,
        unit_rate=Decimal(str(d.get('unit_rate', 0) or 0)),
        remarks=(d.get('remarks') or '').strip() or None,
    )
    ln.recompute_total()
    db.session.add(ln)
    return ln


@bp.route('/api/quotes/<int:qid>', methods=['GET'])
@_require_auth
def api_get_quote(qid):
    q = Quote.query.get_or_404(qid)
    return jsonify(ok=True, quote=q.to_dict(deep=True))


@bp.route('/api/quotes/<int:qid>', methods=['PATCH'])
@_require_auth
def api_patch_quote(qid):
    q = Quote.query.get_or_404(qid)
    if q.status in ('Superseded', 'Won', 'Lost'):
        return jsonify(ok=False, error=f'Quote is {q.status} — immutable'), 400
    d = request.get_json(silent=True) or {}
    for f in ('subject', 'payment_terms', 'inclusions', 'exclusions',
              'remarks', 'currency'):
        if f in d:
            setattr(q, f, (d.get(f) or None))
    for df in ('quote_date', 'validity_until'):
        if df in d:
            setattr(q, df, _parse_date(d.get(df)))
    if 'tax_amount' in d:
        try:
            q.tax_amount = Decimal(str(d.get('tax_amount') or 0))
        except Exception:
            pass
    q.recompute_totals()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500
    return jsonify(ok=True, quote=q.to_dict())


@bp.route('/api/quotes/<int:qid>/lines', methods=['POST'])
@_require_auth
def api_add_line(qid):
    q = Quote.query.get_or_404(qid)
    if q.status in ('Superseded', 'Won', 'Lost'):
        return jsonify(ok=False, error=f'Quote is {q.status} — immutable'), 400
    d = request.get_json(silent=True) or {}
    ln = _append_quote_line(q, d, (db.session.query(db.func.max(QuoteLine.line_no))
                                   .filter_by(quote_id=qid).scalar() or 0) + 1)
    q.recompute_totals()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500
    return jsonify(ok=True, line=ln.to_dict())


@bp.route('/api/quotes/<int:qid>/lines/<int:line_id>', methods=['PATCH'])
@_require_auth
def api_patch_line(qid, line_id):
    q = Quote.query.get_or_404(qid)
    if q.status in ('Superseded', 'Won', 'Lost'):
        return jsonify(ok=False, error=f'Quote is {q.status} — immutable'), 400
    ln = QuoteLine.query.get_or_404(line_id)
    if ln.quote_id != qid:
        return jsonify(ok=False, error='line does not belong to quote'), 400
    d = request.get_json(silent=True) or {}
    for f in ('service', 'description', 'unit', 'remarks'):
        if f in d:
            setattr(ln, f, (d.get(f) or None))
    for numf in ('quantity', 'unit_rate'):
        if numf in d:
            try:
                setattr(ln, numf, Decimal(str(d.get(numf) or 0)))
            except Exception:
                pass
    ln.recompute_total()
    q.recompute_totals()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500
    return jsonify(ok=True, line=ln.to_dict(), quote=q.to_dict())


@bp.route('/api/quotes/<int:qid>/submit-for-approval', methods=['POST'])
@_require_auth
def api_submit_for_approval(qid):
    q = Quote.query.get_or_404(qid)
    if q.status != 'Draft':
        return jsonify(ok=False,
                       error=f'Only Draft quotes may be submitted. Current: {q.status}'), 400
    old = q.status
    q.status = 'Awaiting Approval'
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500
    _fire(q, 'Quote', old, 'Awaiting Approval')
    _fire(q, 'Quote', None, 'Submitted For Approval')   # legacy start_state
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify(ok=True, quote=q.to_dict())


@bp.route('/api/quotes/<int:qid>/approve', methods=['POST'])
@_require_auth
def api_approve(qid):
    q = Quote.query.get_or_404(qid)
    if q.status != 'Awaiting Approval':
        return jsonify(ok=False,
                       error=f'Only Awaiting-Approval quotes may be approved. Current: {q.status}'), 400
    actor = session.get('emp_code')
    emp = _current_emp()
    if _emp_role() != 'admin' and not _has_role_key(emp, 'Vertical_Head'):
        return jsonify(ok=False,
                       error='Only Vertical_Head (or admin) may approve'), 403
    # Maker-checker
    if q.prepared_by_id and q.prepared_by_id == actor \
       and _emp_role() != 'admin':
        return jsonify(ok=False,
                       error='Preparer cannot approve their own quote'), 403
    old = q.status
    q.status = 'Approved'
    q.approved_by_id = actor
    q.approved_at = datetime.utcnow()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500
    _fire(q, 'Quote', old, 'Approved')
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify(ok=True, quote=q.to_dict())


@bp.route('/api/quotes/<int:qid>/reject', methods=['POST'])
@_require_auth
def api_reject(qid):
    q = Quote.query.get_or_404(qid)
    if q.status != 'Awaiting Approval':
        return jsonify(ok=False,
                       error=f'Only Awaiting-Approval quotes may be rejected. Current: {q.status}'), 400
    d = request.get_json(silent=True) or {}
    reason = (d.get('reason') or d.get('remarks') or '').strip()
    old = q.status
    q.status = 'Draft'
    if reason:
        q.remarks = ((q.remarks or '') + f'\n[Rejected {datetime.utcnow():%Y-%m-%d %H:%M}] {reason}').strip()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500
    _fire(q, 'Quote', old, 'Draft')
    # Fire an explicit rework hook so the preparer gets a Returned task.
    try:
        from app.services.task_engine import on_return_for_rework
        on_return_for_rework(q, 'Quote', reason or 'Approval rejected',
                             triggered_by=_current_emp(),
                             return_to_task_key='quote.prepare')
    except Exception:
        pass
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify(ok=True, quote=q.to_dict())


@bp.route('/api/quotes/<int:qid>/submit-to-client', methods=['POST'])
@_require_auth
def api_submit_to_client(qid):
    q = Quote.query.get_or_404(qid)
    if q.status != 'Approved':
        return jsonify(ok=False,
                       error=f'Only Approved quotes may be submitted to client. Current: {q.status}'), 400
    old = q.status
    q.status = 'Submitted'
    q.submitted_by_id = session.get('emp_code')
    q.submitted_at = datetime.utcnow()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500
    _fire(q, 'Quote', old, 'Submitted')
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify(ok=True, quote=q.to_dict())


@bp.route('/api/quotes/<int:qid>/revise', methods=['POST'])
@_require_auth
def api_revise(qid):
    old_q = Quote.query.get_or_404(qid)
    if old_q.status in ('Superseded', 'Won', 'Lost'):
        return jsonify(ok=False,
                       error=f'Quote is {old_q.status} — cannot revise'), 400
    d = request.get_json(silent=True) or {}
    reason = (d.get('reason') or '').strip()
    actor = session.get('emp_code')

    old_status = old_q.status
    new_q = Quote(
        quote_number=_next_quote_number(),
        rfq_id=old_q.rfq_id,
        opportunity_id=old_q.opportunity_id,
        lead_id=old_q.lead_id,
        account_id=old_q.account_id,
        subject=old_q.subject,
        quote_date=date.today(),
        validity_until=old_q.validity_until,
        currency=old_q.currency,
        subtotal=old_q.subtotal or 0,
        tax_amount=old_q.tax_amount or 0,
        total_amount=old_q.total_amount or 0,
        payment_terms=old_q.payment_terms,
        inclusions=old_q.inclusions,
        exclusions=old_q.exclusions,
        remarks=old_q.remarks,
        status='Draft',
        prepared_by_id=old_q.prepared_by_id or actor,
        parent_quote_id=old_q.id,
        revision_number=(old_q.revision_number or 1) + 1,
        created_by_id=actor,
    )
    db.session.add(new_q)
    db.session.flush()

    # Copy lines
    old_lines = QuoteLine.query.filter_by(quote_id=old_q.id).all()
    for src in old_lines:
        db.session.add(QuoteLine(
            quote_id=new_q.id,
            line_no=src.line_no,
            service=src.service,
            description=src.description,
            quantity=src.quantity,
            unit=src.unit,
            unit_rate=src.unit_rate,
            line_total=src.line_total,
            remarks=src.remarks,
        ))
    new_q.recompute_totals()

    # Supersede old
    old_q.status = 'Superseded'
    db.session.add(QuoteRevisionLog(
        quote_id=new_q.id,
        from_revision=old_q.revision_number or 1,
        to_revision=new_q.revision_number,
        changed_by_id=actor,
        diff_summary={'parent_quote_number': [old_q.quote_number,
                                              new_q.quote_number]},
        reason=reason or None,
    ))
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500

    _fire(old_q, 'Quote', old_status, 'Superseded')
    _fire(new_q, 'Quote', None, 'Draft')
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify(ok=True, quote=new_q.to_dict(deep=True),
                   superseded=old_q.to_dict())


@bp.route('/api/quotes/<int:qid>/won', methods=['POST'])
@_require_auth
def api_won(qid):
    q = Quote.query.get_or_404(qid)
    if q.status in ('Superseded', 'Won', 'Lost'):
        return jsonify(ok=False, error=f'Quote is {q.status}'), 400
    old = q.status
    q.status = 'Won'
    q.won_at = datetime.utcnow()
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500
    _fire(q, 'Quote', old, 'Won')
    # Auto-create WonHandover
    try:
        _autocreate_handover(q)
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify(ok=True, quote=q.to_dict())


@bp.route('/api/quotes/<int:qid>/lost', methods=['POST'])
@_require_auth
def api_lost(qid):
    q = Quote.query.get_or_404(qid)
    if q.status in ('Superseded', 'Won', 'Lost'):
        return jsonify(ok=False, error=f'Quote is {q.status}'), 400
    d = request.get_json(silent=True) or {}
    reason = (d.get('lost_reason') or d.get('reason') or '').strip()
    old = q.status
    q.status = 'Lost'
    q.lost_at = datetime.utcnow()
    q.lost_reason = reason[:200] or None
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return jsonify(ok=False, error='commit failed'), 500
    _fire(q, 'Quote', old, 'Lost')
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return jsonify(ok=True, quote=q.to_dict())


# ─── Helpers exposed for the app-level hook ────────────────────────────────
def _autocreate_handover(source_entity):
    """Create a WonHandover row for a Won Quote or Opportunity.

    Deduped by (opportunity_id, quote_id) so re-firing the hook is safe.
    """
    try:
        from app.models.tms_handover import WonHandover
        from app import Opportunity  # noqa: F401
        opp_id = None
        q_id = None
        rfq_id = None
        account_id = None
        subject = None
        currency = 'INR'
        vertical = None
        pic = None
        origin = destination = scope = None
        value = None

        if type(source_entity).__name__ == 'Quote':
            q_id = source_entity.id
            opp_id = source_entity.opportunity_id
            rfq_id = source_entity.rfq_id
            account_id = source_entity.account_id
            subject = source_entity.subject
            value = float(source_entity.total_amount or 0)
            currency = source_entity.currency or 'INR'
        elif type(source_entity).__name__ == 'Opportunity':
            opp_id = source_entity.id
            account_id = getattr(source_entity, 'company_id', None)
            subject = getattr(source_entity, 'title', None)
            value = float(getattr(source_entity, 'value_inr', 0) or 0)
            vertical = getattr(source_entity, 'vertical', None)
            pic = getattr(source_entity, 'owner_emp_code', None)

        # Dedup
        q = WonHandover.query
        if opp_id:
            q = q.filter(WonHandover.opportunity_id == opp_id)
        elif q_id:
            q = q.filter(WonHandover.quote_id == q_id)
        else:
            return None
        existing = q.first()
        if existing:
            return existing

        # Try to enrich from linked Company
        account_name = ''
        if account_id:
            try:
                from app import Company
                acct = Company.query.get(account_id)
                if acct:
                    account_name = getattr(acct, 'name', '') or ''
                    if not vertical:
                        vertical = getattr(acct, 'vertical', None) \
                                   or getattr(acct, 'procam_vertical', None)
                    if not pic:
                        pic = getattr(acct, 'pic_emp_code', None)
            except Exception:
                pass

        row = WonHandover(
            opportunity_id=opp_id, quote_id=q_id, rfq_id=rfq_id,
            account_id=account_id,
            account_name=account_name or subject or '',
            won_value=value or 0,
            services=[],
            origin=origin, destination=destination, scope=scope,
            vertical=vertical, pic_emp_code=pic,
            attachments=[], commercial_refs={},
            status='Handover Pending',
            created_by_id=session.get('emp_code') if session else None,
        )
        db.session.add(row)
        db.session.flush()
        _fire(row, 'WonHandover', None, 'Handover Pending')
        return row
    except Exception:
        try:
            current_app.logger.exception('autocreate_handover failed')
        except Exception:
            pass
        return None
