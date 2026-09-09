"""Business Card Scanning — routes (Phase 12).

HTML
    GET  /business-cards/scan

JSON
    POST /api/business-cards/upload           multipart/form-data 'file'
    POST /api/business-cards/<int:id>/save    JSON: corrected fields + choice
    POST /api/business-cards/<int:id>/discard
    GET  /api/business-cards/<int:id>         review payload
"""
from datetime import datetime
from functools import wraps
import mimetypes
import os

from flask import (Blueprint, jsonify, render_template, request, session,
                   url_for,
                   current_app)

from app import db
from app.models.business_card import BusinessCardImport
from app.services.business_card_ocr import extract_business_card, find_duplicates
from app.utils.uploads import save_upload


bp = Blueprint('business_card', __name__)


_ALLOWED_EXTS = {'.png', '.jpg', '.jpeg', '.heic', '.webp', '.pdf'}


# ─── auth helpers ────────────────────────────────────────────────────────
def _require_auth(f):
    """Gated by the Access Control matrix (module.business_cards).

    Kept under the original name so every existing @_require_auth on this
    blueprint picks up the permission check without being touched.
    """
    from app.access.service import require as _require_perm
    return _require_perm('module.business_cards')(f)


def _emp():
    return session.get('emp_code') or ''


# ─── HTML page ───────────────────────────────────────────────────────────
@bp.route('/business-cards/scan')
@_require_auth
def scan_page():
    """§35 — the mobile capture screen."""
    from app.master_data import service as md
    from app import Employee
    return render_template(
        'business_card/scan.html',
        relationships=[i.label for i in md.items('relationship')],
        employees=Employee.query.filter_by(is_active=True)
                          .order_by(Employee.name).all())


@bp.route('/api/business-cards/upload', methods=['POST'])
@_require_auth
def api_upload():
    fs = request.files.get('file') or request.files.get('image')
    if not fs or not fs.filename:
        return jsonify(error='No file uploaded'), 400

    try:
        dest_path, safe_name = save_upload(
            fs, subdir='business_cards',
            allowed_exts=_ALLOWED_EXTS,
            max_bytes=8 * 1024 * 1024,
        )
    except Exception as exc:
        current_app.logger.warning('business card upload failed: %s', exc)
        return jsonify(error=str(exc)), 400

    mime, _ = mimetypes.guess_type(dest_path)
    if not mime:
        mime = 'image/jpeg'

    try:
        with open(dest_path, 'rb') as fh:
            img_bytes = fh.read()
    except Exception as exc:
        return jsonify(error=f'Could not re-read upload: {exc}'), 500

    # Text read off the card by whatever means — pasted by the user, or
    # from an OCR step if one is added later. With no Vision credit this
    # is the only thing that makes the scanner work at all.
    card_text = (request.form.get('card_text') or '').strip() or None
    result = extract_business_card(img_bytes, mime=mime, card_text=card_text)
    extracted = result.get('extracted') or {}
    dup = find_duplicates(extracted, db)

    row = BusinessCardImport(
        uploaded_by_id=_emp(),
        uploaded_at=datetime.utcnow(),
        image_path=dest_path,
        ocr_raw=result.get('raw') or '',
        extracted_json=extracted,
        status='Extracted',
        dup_matches=dup,
        remarks=result.get('error') or '',
    )
    db.session.add(row)
    db.session.commit()

    payload = row.to_dict()
    payload['ocr_error'] = result.get('error')
    payload['image_url'] = url_for('business_card.card_image',
                                   card_id=row.id)
    return jsonify(ok=True, card=payload)


# ─── Review payload ──────────────────────────────────────────────────────
@bp.route('/business-cards/<int:card_id>/image')
@_require_auth
def card_image(card_id):
    """The photographed card, shown beside the fields read off it.

    send_safe_download() forces an attachment download, which cannot be
    rendered in an <img>, so this serves inline instead — with the same
    nosniff and sandbox headers, an image-only mimetype, and the stored
    path confined to the upload root so a tampered row cannot read an
    arbitrary file off the server.
    """
    from flask import send_file, abort
    from app.utils.uploads import _upload_root

    row = BusinessCardImport.query.get_or_404(card_id)
    path = row.image_path or ''
    if not path:
        abort(404)
    root = os.path.realpath(_upload_root())
    real = os.path.realpath(path)
    if not real.startswith(root + os.sep) or not os.path.isfile(real):
        abort(404)

    mime, _ = mimetypes.guess_type(real)
    if mime not in ('image/jpeg', 'image/png', 'image/gif', 'image/webp',
                    'image/tiff', 'image/bmp', 'application/pdf'):
        abort(404)

    resp = send_file(real, mimetype=mime, as_attachment=False)
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Content-Security-Policy'] = "sandbox; default-src 'none'"
    resp.headers['Cache-Control'] = 'private, max-age=300'
    return resp


@bp.route('/api/business-cards/<int:card_id>', methods=['GET'])
@_require_auth
def api_get(card_id):
    row = BusinessCardImport.query.get_or_404(card_id)
    return jsonify(ok=True, card=row.to_dict())


# ─── Save (create new OR link existing) ──────────────────────────────────
@bp.route('/api/business-cards/<int:card_id>/save', methods=['POST'])
@_require_auth
def api_save(card_id):
    from app import Company, Contact

    row = BusinessCardImport.query.get_or_404(card_id)
    if row.status == 'Saved':
        return jsonify(ok=False, error='Already saved'), 400

    data = request.get_json(silent=True) or {}
    fields = data.get('fields') or {}
    choice = (data.get('choice') or 'new').strip().lower()
    link_account_id = data.get('link_account_id')
    link_contact_id = data.get('link_contact_id')

    # Persist the corrected extraction back so audit is complete.
    row.extracted_json = fields or row.extracted_json

    person_name  = (fields.get('name') or '').strip()
    company_name = (fields.get('company') or '').strip()

    if choice == 'new' and not data.get('confirm_duplicate'):
        # Duplicates were shown at upload, but the reviewer edits the
        # fields before saving — an email corrected here can collide with
        # a contact the original extraction never matched. Checked again
        # against what is actually about to be written.
        again = find_duplicates(fields, db)
        if again.get('contacts'):
            row.dup_matches = again
            db.session.commit()
            return jsonify(ok=False, duplicate=True,
                           matches=again,
                           error='This person may already be in the CRM.'
                                 ' Link to the existing record, or confirm'
                                 ' to create a new one.'), 409

    if choice == 'link':
        if link_account_id:
            acct = Company.query.get(int(link_account_id))
            if not acct:
                return jsonify(error='Account not found'), 404
            row.linked_account_id = acct.id
        if link_contact_id:
            cnt = Contact.query.get(int(link_contact_id))
            if not cnt:
                return jsonify(error='Contact not found'), 404
            row.linked_contact_id = cnt.id
        if not row.linked_account_id and not row.linked_contact_id:
            return jsonify(error='Nothing to link'), 400
    else:
        # Create new Account (if a company name is present) + Contact.
        acct = None
        if company_name:
            # Same matcher the imports and linking use, so a card for
            # "Siemens Ltd" attaches to "Siemens Limited" rather than
            # creating the duplicate de-duplication would have to merge.
            from app.services.company_match import build_index, match
            index = build_index(
                Company.query.filter(Company.is_active.is_(True)).all())
            acct, _reason, _cands = match(company_name, index)
            if not acct:
                acct = Company(
                    name=company_name,
                    website=(fields.get('website') or '').strip() or None,
                    country=(fields.get('country') or '').strip() or None,
                    state=(fields.get('state') or '').strip() or None,
                    city=(fields.get('city') or '').strip() or None,
                    address=(fields.get('address') or '').strip() or None,
                    phone=(fields.get('telephone') or '').strip() or None,
                    email=(fields.get('email') or '').strip() or None,
                    linkedin=(fields.get('linkedin_url') or '').strip() or None,
                    created_by=_emp(),
                )
                db.session.add(acct)
                db.session.flush()
            row.created_account_id = acct.id

        if person_name:
            cnt = Contact(
                name=person_name,
                company=company_name or None,
                designation=(fields.get('designation') or '').strip() or None,
                email=(fields.get('email') or '').strip() or None,
                phone=(fields.get('telephone') or '').strip() or None,
                mobile=(fields.get('mobile') or '').strip() or None,
                country=(fields.get('country') or '').strip() or None,
                state=(fields.get('state') or '').strip() or None,
                city=(fields.get('city') or '').strip() or None,
                website=(fields.get('website') or '').strip() or None,
                linkedin=(fields.get('linkedin_url') or '').strip() or None,
                account_id=acct.id if acct else None,
                company_id=acct.id if acct else None,
                assigned_to=_emp(),
            )
            db.session.add(cnt)
            db.session.flush()
            row.created_contact_id = cnt.id
        elif not acct:
            return jsonify(error='Neither person nor company name present'),\
                   400

    # §38 — classify the organisation there and then. A salesperson who
    # has just met a competitor should not have to find another screen to
    # say so.
    target_account = (row.created_account_id or row.linked_account_id)
    applied = []
    wanted = [t.strip() for t in (data.get('classifications') or [])
              if t and t.strip()]
    if target_account and wanted:
        from app.master_data import service as md
        from presales.models import AccountRelationshipTag
        allowed = {i.label for i in md.items('relationship')}
        unknown = [t for t in wanted if t not in allowed]
        if unknown:
            return jsonify(ok=False,
                           error=f'Not a known relationship type: '
                                 f'{", ".join(unknown)}'), 400
        have = {t.tag for t in AccountRelationshipTag.query.filter_by(
            account_id=target_account).all()}
        for tag in wanted:
            if tag not in have:
                db.session.add(AccountRelationshipTag(
                    account_id=target_account, tag=tag))
                applied.append(tag)

    # §40 — turn the scan into work rather than a filed card.
    follow_up = (data.get('follow_up_date') or '').strip()
    note = (data.get('note') or '').strip()
    created_lead = None
    if target_account and (follow_up or note or data.get('create_lead')):
        from app import Lead, Company as _Company
        company = _Company.query.get(target_account)
        lead = Lead(company=company.name if company else company_name,
                    company_id=target_account,
                    project=(data.get('lead_title') or
                             f'Follow-up — {person_name or company_name}'),
                    source='business_card', stage='New',
                    assigned_to=(data.get('owner') or _emp()),
                    pic=person_name or None,
                    email=(fields.get('email') or '').strip() or None,
                    phone=(fields.get('mobile') or '').strip() or None,
                    notes=note or None)
        if follow_up:
            try:
                from datetime import datetime as _dt
                lead.followup_date = _dt.strptime(follow_up, '%Y-%m-%d').date()
            except Exception:
                pass
        db.session.add(lead)
        db.session.flush()
        created_lead = lead.id

    row.status = 'Saved'
    db.session.commit()
    return jsonify(ok=True, card=row.to_dict(),
                   classifications=applied, lead_id=created_lead)


# ─── Discard ─────────────────────────────────────────────────────────────
@bp.route('/api/business-cards/<int:card_id>/discard', methods=['POST'])
@_require_auth
def api_discard(card_id):
    row = BusinessCardImport.query.get_or_404(card_id)
    row.status = 'Discarded'
    reason = (request.get_json(silent=True) or {}).get('reason') or ''
    if reason:
        row.remarks = reason
    db.session.commit()
    return jsonify(ok=True)


# ─── List (admin / audit) ────────────────────────────────────────────────
@bp.route('/api/business-cards', methods=['GET'])
@_require_auth
def api_list():
    status  = (request.args.get('status') or '').strip()
    limit   = min(int(request.args.get('limit') or 100), 500)
    q = BusinessCardImport.query
    if status:
        q = q.filter(BusinessCardImport.status == status)
    rows = q.order_by(BusinessCardImport.uploaded_at.desc()).limit(limit).all()
    return jsonify(ok=True, cards=[r.to_dict() for r in rows])
