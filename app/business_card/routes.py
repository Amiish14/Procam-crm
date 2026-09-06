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
    if not session.get('emp_code'):
        return render_template('login.html') if False else ('', 302, {'Location': '/login'})
    return render_template('business_card/scan.html')


# ─── Upload + extract ────────────────────────────────────────────────────
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

    result = extract_business_card(img_bytes, mime=mime)
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
    return jsonify(ok=True, card=payload)


# ─── Review payload ──────────────────────────────────────────────────────
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
            acct = Company.query.filter(
                db.func.lower(Company.name) == company_name.lower()
            ).first()
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
                assigned_to=_emp(),
            )
            db.session.add(cnt)
            db.session.flush()
            row.created_contact_id = cnt.id
        elif not acct:
            return jsonify(error='Neither person nor company name present'),\
                   400

    row.status = 'Saved'
    db.session.commit()
    return jsonify(ok=True, card=row.to_dict())


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
