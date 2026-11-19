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


def _sees_all_cards():
    from app.access import scope
    return session.get('role') == 'admin' or scope.current().codes is None


def _card_or_404(card_id):
    """A scanned card belongs to whoever photographed it.

    Every route here used to load any card by id, so a signed-in user
    could read — or save, or discard — a colleague's scanned contacts.
    404 rather than 403 so ids cannot be probed."""
    from flask import abort
    row = BusinessCardImport.query.get_or_404(card_id)
    if row.uploaded_by_id != _emp() and not _sees_all_cards():
        abort(404)
    return row


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


def _store(fs):
    """One uploaded picture on disk → (path, bytes, mime)."""
    dest_path, _safe = save_upload(
        fs, subdir='business_cards',
        allowed_exts=_ALLOWED_EXTS,
        max_bytes=8 * 1024 * 1024,
    )
    mime, _ = mimetypes.guess_type(dest_path)
    with open(dest_path, 'rb') as fh:
        return dest_path, fh.read(), (mime or 'image/jpeg')


#: The fields the accuracy page scores, read against saved.
ACCURACY_FIELDS = ('name', 'designation', 'company', 'email', 'mobile',
                   'telephone', 'website', 'city', 'state', 'postal_code')


def _same(a, b):
    import re as _re
    f = lambda v: _re.sub(r'[^a-z0-9]', '', str(v or '').lower())
    return f(a) == f(b)


@bp.route('/business-cards/accuracy')
def accuracy_page():
    """CRM-SCAN-02: how often each field was read right, card by card,
    with the reader's evidence — every segmentation it tried, its score,
    and the text it returned. Read-only."""
    from flask import redirect, session, url_for
    if 'emp_code' not in session:
        return redirect(url_for('login'))
    rows = (BusinessCardImport.query
            .order_by(BusinessCardImport.id.desc()).limit(100).all())
    totals = {f: [0, 0] for f in ACCURACY_FIELDS}
    cards = []
    for row in rows:
        ex = row.extracted_json or {}
        as_read = ex.get('_as_read')
        checks = []
        if row.status == 'Saved' and isinstance(as_read, dict):
            for f in ACCURACY_FIELDS:
                saved, read = ex.get(f) or '', as_read.get(f) or ''
                if not saved and not read:
                    continue
                ok = _same(saved, read)
                totals[f][0] += 1 if ok else 0
                totals[f][1] += 1
                checks.append({'field': f, 'read': read, 'saved': saved,
                               'ok': ok})
        cards.append({'id': row.id, 'status': row.status,
                      'at': str(row.uploaded_at or '')[:16],
                      'checks': checks,
                      'evidence': ex.get('ocr_evidence') or [],
                      'candidates': ex.get('candidates') or {},
                      'notes': [n for n in (row.prep_notes or '').split('\n')
                                if n.strip()]})
    per_field = [{'field': f, 'right': r, 'of': n,
                  'pct': round(100.0 * r / n) if n else None}
                 for f, (r, n) in totals.items()]
    return render_template('business_card/accuracy.html', cards=cards,
                           per_field=per_field)


@bp.route('/api/business-cards/upload', methods=['POST'])
@_require_auth
def api_upload():
    """A card — front, and the back if there is one — becomes a FORM.

    Nothing is created here and nothing is saved: the upload produces
    a review form and a row that remembers what was read, which is
    the whole contract of this screen. CRM-SEARCH-SCAN-01 Part 2:
    "ALWAYS an editable review form, never auto-save."
    """
    fs = request.files.get('file') or request.files.get('image')
    if not fs or not fs.filename:
        return jsonify(error='No file uploaded'), 400

    try:
        dest_path, img_bytes, mime = _store(fs)
    except Exception as exc:
        current_app.logger.warning('business card upload failed: %s', exc)
        return jsonify(error=str(exc)), 400

    # The back of the card, when there is one. The address and the
    # second number live there, and two scans of one person is the
    # duplicate this screen exists to avoid.
    back_path, sides = '', []
    back_fs = request.files.get('file_back') or request.files.get('back')
    if back_fs and back_fs.filename:
        try:
            back_path, back_bytes, back_mime = _store(back_fs)
            sides.append((back_bytes, back_mime))
        except Exception as exc:                          # noqa: BLE001
            current_app.logger.warning('card back rejected: %s', exc)

    card_text = (request.form.get('card_text') or '').strip() or None
    result = extract_business_card(img_bytes, mime=mime,
                                   card_text=card_text, sides=sides)
    extracted = result.get('extracted') or {}
    if result.get('error'):
        # The request path says so too. A reader that fails on every
        # card for a fortnight and logs nothing is the fault of
        # 15 Nov; one line per upload is what makes it findable.
        current_app.logger.warning(
            'business card %s: the reader failed — %s',
            fs.filename, result['error'])
    _industry_from_account(extracted, result.get('confidence') or {})
    dup = find_duplicates(extracted, db)

    row = BusinessCardImport(
        uploaded_by_id=_emp(),
        uploaded_at=datetime.utcnow(),
        image_path=dest_path,
        back_image_path=back_path or None,
        ocr_raw=result.get('raw') or '',
        extracted_json=extracted,
        confidence_json=result.get('confidence') or {},
        prep_notes='\n'.join(result.get('notes') or []),
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
    if back_path:
        payload['back_image_url'] = url_for('business_card.card_image',
                                            card_id=row.id, side='back')
    payload['people'] = _people_like(extracted)
    return jsonify(ok=True, card=payload)


def _industry_from_account(fields, confidence):
    """The industry, from the account the card matched.

    A business card almost never prints what the company DOES, so
    reading an industry off one would be inventing it. The account
    knows, when the card matches an account — and the review screen
    marks it as worked out rather than read, which is the difference
    between a fact and a good guess.
    """
    if not fields or str(fields.get('industry') or '').strip():
        return
    try:
        account = _account_for(fields.get('company') or '',
                               fields.get('website') or '',
                               fields.get('email') or '')
    except Exception:                                     # noqa: BLE001
        return
    if account is None or not (account.industry or '').strip():
        return
    fields['industry'] = account.industry.strip()
    fields['industry_from'] = f'the account "{account.name}"'
    if isinstance(confidence, dict):
        from app.services import card_confidence
        confidence['industry'] = {
            'score': card_confidence.DERIVED,
            'why': f'taken from the account "{account.name}", not '
                   f'printed on the card',
            'low': True, 'empty': False}
        fields['confidence'] = confidence


def _people_like(fields):
    """The people already on file who look like this card.

    The Part 1 search, which reads the LEADS as well as the contacts —
    most of the people in this CRM are on a lead and nowhere else, so
    a duplicate check that reads only `contacts` says "new person"
    about somebody we have been quoting for months.
    """
    try:
        from app.services import people_search
        return people_search.duplicates_of(
            email=(fields or {}).get('email') or '',
            phone=((fields or {}).get('mobile')
                   or (fields or {}).get('telephone') or ''),
            name=(fields or {}).get('name') or '',
            company=(fields or {}).get('company') or '')
    except Exception:                                     # noqa: BLE001
        return []


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

    row = _card_or_404(card_id)
    side = (request.args.get('side') or 'front').strip().lower()
    path = (row.back_image_path if side == 'back' else row.image_path) or ''
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
    row = _card_or_404(card_id)
    return jsonify(ok=True, card=row.to_dict())


def _account_for_any(company_name, website='', email=''):
    """The account this card belongs to, aliases and all.

    Three readings, each the one the rest of the CRM already uses:

      * the ALIAS index (`account_match`), so a card printed
        "ArcelorMittal Nippon Steel India" finds the account called
        "Amns" instead of making a second one — the fault of 14 Nov,
        which cost a hand-merge on live;
      * the website or email DOMAIN, which is the strongest signal a
        card carries and the one a name match misses when the card
        prints a trading name;
      * `company_match`, the name matcher the imports use.

    Returns an existing account or None. It never creates one: that
    is the caller's decision, made after the reviewer has looked.
    """
    from app import Company

    name = (company_name or '').strip()
    if not name and not website and not email:
        return None
    domain = ''
    for source in (website, email):
        from app.services.card_parse import domain_of
        domain = domain_of(source or '')
        if domain:
            break
    if domain:
        try:
            found = (Company.query
                     .filter(Company.website.ilike(f'%{domain}%'))
                     .first())
            if found:
                return found
        except Exception:                                 # noqa: BLE001
            pass
    if name:
        try:
            from app.services import account_match
            for account, norms, stricts, _own in account_match.active_index():
                flat = account_match.strict_norm(name)
                if flat and (flat in stricts or flat in norms):
                    return account
        except Exception:                                 # noqa: BLE001
            pass
        try:
            from app.services.company_match import build_index, match
            index = build_index(
                Company.query.filter(Company.is_active.is_(True)).all())
            found, _reason, _cands = match(name, index)
            if found:
                return found
        except Exception:                                 # noqa: BLE001
            pass
    return None



def _account_for(company_name, website='', email=''):
    """`_account_for_any`, never a placeholder or a portal (UAT 19 Nov).

    A card whose domain or name matched #847 "New Lead" filed a real
    person under a row that is not a company. The same refusal the mail
    intake, the account gate and the lead picker use.
    """
    found = _account_for_any(company_name, website, email)
    if found is None:
        return None
    try:
        from app.services import account_match
        if account_match.not_a_customer(found):
            return None
    except Exception:                                     # noqa: BLE001
        pass
    return found

def _card_extras(fields, row):
    """The columns Part 2 added, filled only where the model has them.

    Written this way so the same code runs on a database that has not
    had the additive columns applied yet: a scanner that cannot save
    because one column is missing is a scanner that is down.
    """
    from app import Contact

    wanted = {
        'department': (fields.get('department') or '').strip() or None,
        'address': (fields.get('address')
                    or fields.get('street') or '').strip() or None,
        'postal_code': (fields.get('postal_code') or '').strip() or None,
        'industry': (fields.get('industry') or '').strip() or None,
        # The card itself stays with the person it names.
        'card_image_path': row.image_path or None,
    }
    return {k: v for k, v in wanted.items() if hasattr(Contact, k)}


# ─── Save (create new OR link existing) ──────────────────────────────────
@bp.route('/api/business-cards/<int:card_id>/save', methods=['POST'])
@_require_auth
def api_save(card_id):
    from app import Company, Contact

    row = _card_or_404(card_id)
    if row.status == 'Saved':
        return jsonify(ok=False, error='Already saved'), 400

    data = request.get_json(silent=True) or {}
    fields = data.get('fields') or {}
    choice = (data.get('choice') or 'new').strip().lower()
    link_account_id = data.get('link_account_id')
    link_contact_id = data.get('link_contact_id')

    # What was READ, before the reviewer's corrections overwrite it —
    # CRM-SCAN-02 learns from the difference on a successful save.
    _as_read = dict(row.extracted_json or {})
    # Persist the corrected extraction back so audit is complete.
    row.extracted_json = fields or row.extracted_json
    if fields and isinstance(row.extracted_json, dict):
        # Kept for the accuracy page: what the reader said, beside what
        # the reviewer saved, plus the reader's evidence.
        row.extracted_json = dict(row.extracted_json)
        row.extracted_json['_as_read'] = {
            k: _as_read.get(k) for k in ACCURACY_FIELDS}
        if _as_read.get('ocr_evidence') and not fields.get('ocr_evidence'):
            row.extracted_json['ocr_evidence'] = _as_read['ocr_evidence']

    person_name  = (fields.get('name') or '').strip()
    company_name = (fields.get('company') or '').strip()
    # A card from a blocked client is refused before anything is filed:
    # scanning one is how a relationship starts, which is exactly what
    # the block exists to stop.
    from app.services import restriction_gate as _gate
    _refusal = _gate.guard(
        request.get_json(silent=True) or {}, what='business card',
        company_name=company_name,
        email=(fields.get('email') or '').strip())
    if _refusal:
        return _refusal

    if choice == 'new' and not data.get('confirm_duplicate'):
        # Duplicates were shown at upload, but the reviewer edits the
        # fields before saving — an email corrected here can collide with
        # a contact the original extraction never matched. Checked again
        # against what is actually about to be written.
        again = find_duplicates(fields, db)
        # And against the LEADS, which is where most of the people in
        # this CRM are (Part 1). A check that reads only `contacts`
        # says "new person" about somebody we quoted last week.
        people = _people_like(fields)
        if people and not again.get('contacts'):
            row.dup_matches = again
            db.session.commit()
            return jsonify(ok=False, duplicate=True, matches=again,
                           people=people,
                           error='This person may already be in the CRM. '
                                 'Open one of them, or press Save anyway '
                                 'if this is a different person.'), 409
        if again.get('contacts'):
            row.dup_matches = again
            db.session.commit()
            return jsonify(ok=False, duplicate=True,
                           matches=again, people=_people_like(fields),
                           error='This person may already be in the CRM.'
                                 ' Link to the existing record, or press'
                                 ' Save anyway if this is a different'
                                 ' person.'), 409

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
            acct = _account_for(company_name,
                                (fields.get('website') or '').strip(),
                                (fields.get('email') or '').strip())
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
                **_card_extras(fields, row),
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
    try:
        from app.services import card_smart
        card_smart.learn(_as_read, fields, actor=_emp())
    except Exception as exc:                              # noqa: BLE001
        current_app.logger.warning('card learning skipped: %s', exc)
    db.session.commit()
    return jsonify(ok=True, card=row.to_dict(),
                   classifications=applied, lead_id=created_lead)


# ─── Discard ─────────────────────────────────────────────────────────────
@bp.route('/api/business-cards/<int:card_id>/discard', methods=['POST'])
@_require_auth
def api_discard(card_id):
    row = _card_or_404(card_id)
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
    if not _sees_all_cards():
        q = q.filter(BusinessCardImport.uploaded_by_id == _emp())
    if status:
        q = q.filter(BusinessCardImport.status == status)
    rows = q.order_by(BusinessCardImport.uploaded_at.desc()).limit(limit).all()
    return jsonify(ok=True, cards=[r.to_dict() for r in rows])
