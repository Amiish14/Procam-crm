"""Three things the lead screen could not say.

CRM-FINAL-CLOSEOUT-01 §5, §6, §7.

    GET  /api/leads/<id>/dates          every milestone, with its rung
    GET  /api/leads/<id>/team           who is on it, and by what right
    GET  /api/leads/<id>/quote-files    quotations attached to the lead
    POST /api/leads/<id>/quote-files    attach one
    GET  /api/leads/<id>/account-state  the account question, if any
                                        (CRM-WA-01b)

Not behind a feature switch. All three are reported absences — the
dates are blank, the team is one name, and a quotation made in Excel
has nowhere to live — and a fix for an absence that is itself switched
off leaves the absence in place.

Downloading is not here on purpose: `/api/leads/<id>/attachments/
<aid>/download` already serves a lead's files, with the ownership
check and the `nosniff` headers already written. A second download
route would be a second place for that check to be wrong.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime
from functools import wraps

from flask import Blueprint, abort, jsonify, request, session

bp = Blueprint('lead_facts', __name__)

#: What a quotation may be. PDF first because that is what goes to a
#: customer; the Office formats because that is what it was made in
#: and people attach the working file beside it.
ALLOWED = {'.pdf', '.doc', '.docx', '.xls', '.xlsx'}

MAX_BYTES = 15 * 1024 * 1024

#: `lead_attachments.source` for a file attached as a quotation.
#: Its own value rather than 'manual', so the quote engine can find
#: these later and so the lead screen can show them in their own
#: place instead of among the customer's own attachments.
QUOTE_SOURCE = 'quote'


def _me():
    return session.get('emp_code')


def _facts(f):
    """Signed in, and allowed to see this lead.

    Its own decorator so the route audit in
    scripts/generate_reference_docs.py can name the gate rather than
    reporting these as open. The per-lead check is
    `_require_lead_access`, which asks `app.access.scope` — the same
    chokepoint the boards and the reports go through, so this cannot
    reach a lead the list would hide.
    """
    @wraps(f)
    def wrap(*a, **kw):
        if not _me():
            return jsonify(ok=False, error='Sign in first.'), 401
        return f(*a, **kw)
    return wrap


def _lead(lid):
    from app import _require_lead_access

    return _require_lead_access(lid)


# ── §5 the dates ─────────────────────────────────────────────────────
@bp.route('/api/leads/<int:lid>/dates')
@_facts
def dates(lid):
    """Every milestone, each labelled with how it is known."""
    from app.services import lead_dates

    out = lead_dates.summary(_lead(lid))
    return jsonify(ok=True,
                   dates=[_date_row(r) for r in out['dates']],
                   counts=out['counts'], known=out['known'],
                   total=out['total'], stage=out['stage'],
                   labels=lead_dates.SOURCE_LABELS)


def _date_row(row):
    """One milestone as JSON. The date is a string or empty — never
    the word "None", which is what a template prints when a date is
    passed through unchecked."""
    value = row['date']
    return {**row,
            'date': (value.isoformat()
                     if isinstance(value, (date, datetime)) else '')}


# ── §6 the team ──────────────────────────────────────────────────────
@bp.route('/api/leads/<int:lid>/team')
@_facts
def team(lid):
    """Owner, cover, head, account, task holders and the desks.

    `with_access=1` additionally asks, per member, whether they can
    actually open this lead. It is a query per person, so it is not
    the default — the lead screen asks for it once, on the Team tab,
    and the answer is what makes a misconfigured head visible.
    """
    from app.services import lead_team

    want = (request.args.get('with_access') or '').strip().lower()
    out = lead_team.summary(_lead(lid),
                            check_access=want in ('1', 'true', 'yes'))
    return jsonify(ok=True, **out)


# ── §7 the quotation file ────────────────────────────────────────────
def _safe_name(name):
    name = os.path.basename((name or '').strip())
    return re.sub(r'[^A-Za-z0-9._-]+', '_', name)[:200] or 'quote'


@bp.route('/api/leads/<int:lid>/quote-files')
@_facts
def quote_files(lid):
    from app.services import lead_quote_file

    lead = _lead(lid)
    return jsonify(ok=True, files=lead_quote_file.listing(lead),
                   allowed=sorted(ALLOWED),
                   max_mb=MAX_BYTES // (1024 * 1024))


@bp.route('/api/leads/<int:lid>/quote-files', methods=['POST'])
@_facts
def upload_quote_file(lid):
    """Attach a quotation to the lead it belongs to.

    Multipart: `file`, and optionally `quote_no`, `quote_date` and
    `note`. The numbers go on the lead only where it has none — see
    `lead_quote_file.attach`.
    """
    from app.services import lead_quote_file

    lead = _lead(lid)
    upload = request.files.get('file')
    if upload is None or not (upload.filename or '').strip():
        return jsonify(ok=False, needs='file',
                       error='Choose the quotation file.'), 400

    name = _safe_name(upload.filename)
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED:
        return jsonify(
            ok=False, needs='file', allowed=sorted(ALLOWED),
            error=f'{ext or "That"} is not a file this can store. '
                  f'PDF, Word or Excel.'), 400

    blob = upload.read()
    if not blob:
        return jsonify(ok=False, needs='file',
                       error='That file is empty.'), 400
    if len(blob) > MAX_BYTES:
        return jsonify(
            ok=False, needs='file',
            error=f'That file is {len(blob) // (1024 * 1024)} MB. '
                  f'The limit is {MAX_BYTES // (1024 * 1024)} MB.'), 400

    try:
        out = lead_quote_file.attach(
            lead, filename=name, blob=blob,
            content_type=(upload.mimetype or ''),
            quote_no=request.form.get('quote_no'),
            quote_date=request.form.get('quote_date'),
            note=request.form.get('note'),
            actor=_me())
    except lead_quote_file.Refused as refusal:
        return jsonify(ok=False, error=str(refusal)), 400
    except OSError as exc:
        return jsonify(
            ok=False,
            error=f'The file could not be stored on the server '
                  f'({exc.__class__.__name__}). Nothing was '
                  f'attached.'), 500
    return jsonify(ok=True, **out)


# ── CRM-WA-01b the account question, asked instead of refused ───────
@bp.route('/api/leads/<int:lid>/account-state')
@_facts
def account_state(lid):
    """What the account-first rule thinks of this lead, for this viewer.

    One place for the three answers the lead screen needs and could
    not get: whether a stage move would be refused and why, whether
    *this* viewer is exempt because the lead is theirs, and whether
    the lead's own company name disagrees with the account it is
    linked to.

    The last one is a prompt, never a refusal. CRM-WA-01b §3: "if the
    block is a name mismatch, show Confirm account on the lead instead
    of refusing the stage change". It never did refuse for a mismatch
    — the gate only ever asked for an account and an owner — so what
    was missing was the asking, which is this.
    """
    from app.services import account_gate, lead_people

    lead = _lead(lid)
    # CRM-MAIL-02 §2. The lead sheet disabled the Primary PIC, the
    # Secondary PIC and the vertical for anybody who was not an
    # administrator, so the RFQ desk could not even attempt the one
    # change that gets a lead out of "nobody is accountable". The
    # server decides who may; the screen asks here and enables the
    # three fields to match, so the form and the rule cannot differ.
    may_set, why_set = lead_people.may_set_people(lead)
    if not account_gate.enabled():
        return jsonify(ok=True, live=False, blocked=False, reason='',
                       mismatch=None, claim='', message='',
                       resolver='', confirm_url='',
                       can_set_people=may_set, why_set=why_set)

    code, why = account_gate.reason_for(lead)
    claim = account_gate.worker_claim(lead, _me())
    exempt = bool(code in account_gate.OWNERSHIP_REASONS and claim)
    mismatch = account_gate.name_mismatch(lead)
    account = account_gate.account_of(lead)
    return jsonify(
        ok=True,
        live=True,
        reason=code,
        reason_words=why,
        blocked=bool(code) and not exempt,
        exempt=exempt,
        claim=claim,
        message=(account_gate.message_for(lead, code, why)
                 if code and not exempt else ''),
        resolver=account_gate.resolver_for(lead) if code else '',
        vertical=account_gate.vertical_of(lead),
        account_id=(account.id if account is not None else None),
        account_name=(account.name if account is not None else ''),
        mismatch=({'lead_name': mismatch[0], 'account_name': mismatch[1]}
                  if mismatch else None),
        can_set_people=may_set,
        why_set=why_set,
        confirm_url=('/accounts/check' if (code or mismatch) else ''))
