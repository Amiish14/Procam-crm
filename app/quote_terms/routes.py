"""
Admin → Quote terms, and the terms on one quote.

    GET  /admin/quote-terms                 the screen
    GET  /api/quote-terms/master            the Procam Standard T&C
    POST /api/quote-terms/master            publish a new version
    GET  /api/quote-terms/annexure/<vert>   one vertical's annexure
    POST /api/quote-terms/annexure/<vert>   publish a new version

    GET    /api/quotes/<id>/terms           what this quote prints
    POST   /api/quotes/<id>/terms/document  upload the client's T&C
    DELETE /api/quotes/<id>/terms/document/<doc_id>
    POST   /api/quotes/<id>/terms/condition add an SCC
    DELETE /api/quotes/<id>/terms/condition/<row_id>
    POST   /api/accounts/<id>/standing-condition

PROCAM-DECISIONS-02NOV Module 1. Not behind a feature switch: rule
0.1 of the pack, as of CRM-COCKPIT-02 — deployed is live.

Publishing is administrators only, because a term set is a contract
and a new version changes what every quote printed from now on says.
Reading is wide: anybody who can see a quote can see the terms it
goes out under, and a salesperson who cannot read the payment term
is a salesperson who agrees to a different one on the phone.

Writing an SCC is the quote's own permission — whoever may edit the
quote. An SCC overrides a standard clause (Module 1.4), so this is
the one place a non-administrator changes what a contract says, and
it is deliberate: the alternative is the condition living in the body
of an email.
"""
from __future__ import annotations

import os
import re
from functools import wraps

from flask import (Blueprint, jsonify, render_template, request,
                   session)

from app import db
from app.services import quote_terms as qt

bp = Blueprint('quote_terms_admin', __name__)

#: What the client or the tender may send us as their T&C
#: (Module 1.3: PDF/Word/Excel/GeM). GeM arrives as a PDF; it is
#: named in the decision because it is a different *source*, not a
#: different format, and the note field is where that is recorded.
ALLOWED = {
    '.pdf': 'application/pdf',
    '.doc': 'application/msword',
    '.docx': ('application/vnd.openxmlformats-officedocument'
              '.wordprocessingml.document'),
    '.xls': 'application/vnd.ms-excel',
    '.xlsx': ('application/vnd.openxmlformats-officedocument'
              '.spreadsheetml.sheet'),
}

#: 10 MB. A tender's T&C is a document, not a drawing set.
MAX_BYTES = 10 * 1024 * 1024


def _me():
    return session.get('emp_code')


def _auth(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not _me():
            from app.services import guided_help
            body, status = guided_help.refusal('SESSION_EXPIRED',
                                               http=401)
            return jsonify(body), status
        return f(*a, **kw)
    return wrap


def _is_admin(code=None):
    from app import Employee

    emp = Employee.query.filter_by(emp_code=(code or _me())).first()
    role = (getattr(emp, 'role', '') or '').lower()
    return role in ('admin', 'super admin', 'superadmin', 'director',
                    'procam_admin')


def _admin_only(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not _me():
            from flask import redirect, url_for
            return redirect(url_for('login'))
        if not _is_admin():
            return jsonify(
                ok=False,
                error='Publishing a term set is an administrator\'s '
                      'job: a new version changes what every quote '
                      'from now on says.'), 403
        return f(*a, **kw)
    return wrap


def _quote_or_404(qid, *, editing=False):
    from flask import abort

    from app.access import records
    from app.models.quote import Quote

    quote = Quote.query.get_or_404(qid)
    if not records.may_view_quote(quote):
        abort(404)
    if editing and not (_is_admin()
                        or _me() in (quote.prepared_by_id,
                                     quote.created_by_id)):
        abort(403)
    return quote


# ── the library ─────────────────────────────────────────────────────
@bp.route('/admin/quote-terms')
@_admin_only
def page():
    master = qt.master_current()
    return render_template(
        'admin/quote_terms.html',
        master=(master.to_dict() if master else None),
        master_clauses=(qt.clauses(master.body) if master else []),
        history=[r.to_dict() for r in qt.master_history()],
        verticals=_annexures())


def _annexures():
    from app.models.quote_terms import ANNEXURE, QuoteTermSet

    rows = (QuoteTermSet.query.filter_by(is_current=True)
            .filter(db.or_(QuoteTermSet.kind == ANNEXURE,
                           QuoteTermSet.kind.is_(None)))
            .order_by(QuoteTermSet.vertical).all())
    out = []
    for row in rows:
        d = row.to_dict()
        d['clauses'] = qt.clauses(row.body)
        # Surfaced rather than hidden: a term set whose first line
        # still says DRAFT is one no quote should be going out under,
        # and the screen is where somebody can see that.
        d['draft'] = 'draft' in (row.body or '')[:80].lower()
        out.append(d)
    return out


@bp.route('/api/quote-terms/master')
@_auth
def api_master():
    master = qt.master_current()
    return jsonify(ok=True,
                   master=(master.to_dict() if master else None),
                   clauses=(qt.clauses(master.body) if master else []),
                   history=[r.to_dict() for r in qt.master_history()])


@bp.route('/api/quote-terms/master', methods=['POST'])
@_admin_only
def api_publish_master():
    data = request.get_json(silent=True) or {}
    try:
        row = qt.publish_master((data.get('body') or ''),
                                title=(data.get('title') or None),
                                actor=_me())
    except qt.TermsError as exc:
        return jsonify(ok=False, error=str(exc), needs='body'), 400
    db.session.commit()
    return jsonify(ok=True, master=row.to_dict(),
                   clauses=qt.clauses(row.body))


@bp.route('/api/quote-terms/annexure/<vertical>')
@_auth
def api_annexure(vertical):
    row = qt.current_for(vertical)
    return jsonify(ok=True, vertical=vertical,
                   annexure=(row.to_dict() if row else None),
                   clauses=(qt.clauses(row.body) if row else []),
                   history=[r.to_dict()
                            for r in qt.history_for(vertical)])


@bp.route('/api/quote-terms/annexure/<vertical>', methods=['POST'])
@_admin_only
def api_publish_annexure(vertical):
    data = request.get_json(silent=True) or {}
    try:
        row = qt.publish(vertical, (data.get('body') or ''),
                         title=(data.get('title') or None),
                         actor=_me())
    except qt.TermsError as exc:
        return jsonify(ok=False, error=str(exc), needs='body'), 400
    db.session.commit()
    return jsonify(ok=True, annexure=row.to_dict(),
                   clauses=qt.clauses(row.body))


# ── one quote ───────────────────────────────────────────────────────
@bp.route('/api/quotes/<int:qid>/terms')
@_auth
def api_quote_terms(qid):
    quote = _quote_or_404(qid)
    printed = qt.for_print(quote)
    printed['ok'] = True
    printed['doc_number'] = qt.document_number(quote)
    printed['modes'] = [
        {'code': 'add',
         'label': 'Add to the Procam standard terms'},
        {'code': 'replace',
         'label': 'Replace the Procam standard terms'},
    ]
    return jsonify(printed)


def _safe_name(name):
    name = os.path.basename((name or '').strip())
    return re.sub(r'[^A-Za-z0-9._-]+', '_', name)[:200] or 'terms'


@bp.route('/api/quotes/<int:qid>/terms/document', methods=['POST'])
@_auth
def api_upload_document(qid):
    """The client's or the tender's own T&C, against the number."""
    quote = _quote_or_404(qid, editing=True)
    upload = request.files.get('file')
    if upload is None or not (upload.filename or '').strip():
        return jsonify(ok=False,
                       error='Choose the file with their terms in '
                             'it.', needs='file'), 400
    mode = (request.form.get('mode') or '').strip().lower()
    name = _safe_name(upload.filename)
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED:
        return jsonify(
            ok=False,
            error=f'{ext or "That"} is not a document this can '
                  f'store. PDF, Word or Excel.',
            needs='file',
            allowed=sorted(ALLOWED)), 400

    blob = upload.read()
    if len(blob) > MAX_BYTES:
        return jsonify(
            ok=False,
            error=f'That file is {len(blob) // (1024 * 1024)} MB. '
                  f'The limit is {MAX_BYTES // (1024 * 1024)} MB.',
            needs='file'), 400
    if not blob:
        return jsonify(ok=False, error='That file is empty.',
                       needs='file'), 400

    number = qt.document_number(quote)
    root = os.environ.get('QUOTE_TERMS_STORAGE_ROOT',
                          '/var/www/procam-crm/uploads/quote_terms')
    folder = os.path.join(root, _safe_name(number) or str(quote.id))
    try:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, name)
        # Never overwrite: two revisions can send two files with the
        # same name, and the second silently replacing the first is
        # how a quote prints terms the client never sent.
        stem, n = path, 1
        while os.path.exists(path):
            base, dot_ext = os.path.splitext(stem)
            path = f'{base}_{n}{dot_ext}'
            n += 1
        with open(path, 'wb') as fh:
            fh.write(blob)
    except OSError as exc:
        return jsonify(ok=False,
                       error=f'The file could not be stored: {exc}'), 500

    try:
        row = qt.attach_document(
            quote, filename=name, stored_path=path,
            content_type=(upload.mimetype or ALLOWED[ext]),
            mode=mode, note=(request.form.get('note') or ''),
            actor=_me())
    except qt.TermsError as exc:
        # The file is already on disk. Left there rather than
        # removed: the person's next attempt is the same upload with
        # a mode chosen, and deleting their file between the two
        # attempts is a worse failure than an orphan byte on disk,
        # which the folder name makes findable.
        return jsonify(ok=False, error=str(exc), needs='mode',
                       modes=['add', 'replace']), 400
    db.session.commit()
    return jsonify(ok=True, document=row.to_dict(),
                   doc_number=number)


@bp.route('/api/quotes/<int:qid>/terms/document/<int:doc_id>',
          methods=['DELETE'])
@_auth
def api_withdraw_document(qid, doc_id):
    from app.models.quote_terms import QuoteTermsDocument

    quote = _quote_or_404(qid, editing=True)
    row = db.session.get(QuoteTermsDocument, doc_id)
    if row is None or row.doc_number != qt.document_number(quote):
        return jsonify(ok=False,
                       error='That document is not on this quote.'), 404
    qt.withdraw_document(row, actor=_me())
    db.session.commit()
    return jsonify(ok=True)


@bp.route('/api/quotes/<int:qid>/terms/condition', methods=['POST'])
@_auth
def api_add_condition(qid):
    quote = _quote_or_404(qid, editing=True)
    data = request.get_json(silent=True) or {}
    text = (data.get('text') or '').strip()
    try:
        row = qt.add_condition(quote, text, actor=_me())
    except qt.TermsError as exc:
        return jsonify(ok=False, error=str(exc), needs='text'), 400
    db.session.commit()
    key = qt.clause_key(text)
    overrides = ''
    if key:
        master = qt.master_on(quote)
        for clause in qt.clauses(master.body if master else ''):
            if clause['key'] == key:
                overrides = clause['text']
                break
    return jsonify(ok=True, condition=row.to_dict(),
                   # Told plainly rather than discovered on the PDF:
                   # the person typing it is the one who should know
                   # it has replaced a standard clause.
                   overrides=overrides)


@bp.route('/api/quotes/<int:qid>/terms/condition/<int:row_id>',
          methods=['DELETE'])
@_auth
def api_remove_condition(qid, row_id):
    from app.models.quote_terms import QuoteSpecialCondition

    quote = _quote_or_404(qid, editing=True)
    row = db.session.get(QuoteSpecialCondition, row_id)
    if row is None or row.quote_id != quote.id:
        return jsonify(ok=False,
                       error='That condition is not on this '
                             'quote.'), 404
    qt.remove_condition(row, actor=_me())
    db.session.commit()
    return jsonify(ok=True)


@bp.route('/api/accounts/<int:account_id>/standing-condition',
          methods=['POST'])
@_auth
def api_add_standing(account_id):
    """A condition agreed with this client, on every quote for them."""
    from app import Company

    account = db.session.get(Company, account_id)
    if account is None:
        return jsonify(ok=False, error='No such account.'), 404
    if not _is_admin():
        # Narrower than an SCC on purpose. A standing condition
        # changes quotes nobody has written yet, including other
        # people's.
        return jsonify(
            ok=False,
            error='A condition that applies to every quote for this '
                  'client is set by an administrator. Add it to this '
                  'quote, and ask for it to be made standing.'), 403
    data = request.get_json(silent=True) or {}
    try:
        row = qt.add_standing(account_id, (data.get('text') or ''),
                              source=(data.get('source') or ''),
                              actor=_me())
    except qt.TermsError as exc:
        return jsonify(ok=False, error=str(exc), needs='text'), 400
    db.session.commit()
    return jsonify(ok=True, condition=row.to_dict())
