"""
Delete – not relevant: the routes.

    GET  /api/leads/<id>/junk-check      may I, and if not why not
    POST /api/leads/<id>/junk-delete     one lead
    POST /api/leads/junk-preview         what a bulk delete would do
    POST /api/leads/junk-delete-bulk     the bulk delete
    GET  /admin/deleted-leads            the bin
    GET  /api/admin/deleted-leads        the bin, as JSON
    GET  /api/admin/deleted-leads/export the bin, as a spreadsheet
    POST /api/admin/deleted-leads/<id>/restore
    POST /api/admin/deleted-leads/purge  behind FEATURE_JUNK_PURGE

Everything is behind FEATURE_JUNK_DELETE, and a person without the
flag gets a 404 rather than a 403 — the same shape as every other
release, so a flag that is off looks like a page that is not there.
"""
from __future__ import annotations

import io
from datetime import datetime, timedelta
from functools import wraps

from flask import (Blueprint, abort, jsonify, render_template, request,
                   send_file, session)

bp = Blueprint('junk', __name__)


def _me():
    return session.get('emp_code')


def _junk(f):
    """404 unless the flag is on for this person."""
    @wraps(f)
    def wrap(*a, **kw):
        from app.services import flags

        code = _me()
        if not code:
            abort(404)
        if not flags.on_for('FEATURE_JUNK_DELETE', code):
            abort(404)
        return f(*a, **kw)
    return wrap


def _admin_only(f):
    @wraps(f)
    def wrap(*a, **kw):
        from app.services import junk_delete as jd

        if not jd.is_admin(_me()):
            return jsonify(ok=False,
                           error='Administrators only.'), 403
        return f(*a, **kw)
    return wrap


def _lead(lid):
    from app import Lead

    lead = Lead.query.get(lid)
    if lead is None:
        abort(404)
    return lead


@bp.route('/api/leads/junk-available')
@_junk
def available():
    """Is the feature here for this person?

    The shell asks once on load so it knows whether to draw the
    select boxes and the bulk bar at all. A 404 — the flag off, or
    not on for this person — is the answer "no", and the shell then
    looks exactly as it did before this release.
    """
    from app.services import junk_delete as jd

    return jsonify(ok=True, admin=jd.is_admin(_me()),
                   admin_above=jd.BULK_ADMIN_ABOVE, cap=jd.DAILY_CAP)


# ── one lead ────────────────────────────────────────────────────────
@bp.route('/api/leads/<int:lid>/junk-check')
@_junk
def check(lid):
    """What the button should do before anybody clicks it."""
    from app.services import junk_delete as jd

    lead = _lead(lid)
    why = jd.blockers(lead)
    allowed = jd.may_delete(lead, _me())
    return jsonify(
        ok=True, lead_id=lid,
        show=bool(allowed),
        eligible=not why,
        blockers=[{'check': k, 'why': w} for k, w in why],
        hover=jd.HOVER if why else '',
        sender=jd.sender_of(lead),
        reasons=[{'code': c, 'label': l, 'blocks_sender': b}
                 for c, l, b in jd.REASONS],
        needs_note=list(jd.NEEDS_NOTE))


@bp.route('/api/leads/<int:lid>/junk-delete', methods=['POST'])
@_junk
def delete_one(lid):
    from app.services import junk_delete as jd

    lead = _lead(lid)
    me = _me()
    if not jd.may_delete(lead, me):
        return jsonify(ok=False,
                       error='Only the owner, the vertical head or an '
                             'administrator can do that.'), 403
    body = request.get_json(silent=True) or {}
    reason = (body.get('reason') or '').strip()
    note = (body.get('note') or '').strip()
    block = bool(body.get('block_sender'))

    over, total = jd.over_cap(me, adding=1)
    try:
        out = jd.mark_not_relevant(lead, reason=reason, note=note, actor=me,
                        block_sender=block, commit=False)
    except ValueError as exc:
        from app import db
        db.session.rollback()
        return jsonify(ok=False, error=str(exc)), 400
    if over:
        jd.tell_the_admin(me, total)
    from app import db
    db.session.commit()
    return jsonify(ok=True, deleted=1, **out, over_cap=over)


# ── bulk ────────────────────────────────────────────────────────────
def _ids_from(body):
    out = []
    for raw in (body.get('ids') or []):
        try:
            out.append(int(raw))
        except (TypeError, ValueError):
            continue
    return out


def _split(ids, me):
    """(eligible leads, [{id, company, why}] skipped)."""
    from app.services import junk_delete as jd

    from app import Lead

    good, skipped = [], []
    for lead in Lead.query.filter(Lead.id.in_(ids)).all():
        if not jd.may_delete(lead, me):
            skipped.append({'id': lead.id, 'company': lead.company or '',
                            'why': 'it is not yours to delete'})
            continue
        why = jd.blockers(lead)
        if why:
            skipped.append({'id': lead.id, 'company': lead.company or '',
                            'why': why[0][1]})
            continue
        good.append(lead)
    return good, skipped


@bp.route('/api/leads/junk-preview', methods=['POST'])
@_junk
def preview():
    """Say what would happen before it happens."""
    from app.services import junk_delete as jd

    me = _me()
    ids = _ids_from(request.get_json(silent=True) or {})
    good, skipped = _split(ids, me)
    needs_admin = len(good) > jd.BULK_ADMIN_ABOVE and not jd.is_admin(me)
    over, total = jd.over_cap(me, adding=len(good))
    return jsonify(
        ok=True,
        eligible=len(good), skipped=len(skipped),
        skipped_rows=skipped[:100],
        eligible_rows=[{'id': l.id, 'company': l.company or '',
                        'sender': jd.sender_of(l)} for l in good[:100]],
        needs_admin=needs_admin,
        admin_above=jd.BULK_ADMIN_ABOVE,
        over_cap=over, today=total, cap=jd.DAILY_CAP,
        confirm_label=f'Delete {len(good)} lead(s)')


@bp.route('/api/leads/junk-delete-bulk', methods=['POST'])
@_junk
def delete_bulk():
    from app import db
    from app.services import junk_delete as jd

    me = _me()
    body = request.get_json(silent=True) or {}
    reason = (body.get('reason') or '').strip()
    note = (body.get('note') or '').strip()
    block = bool(body.get('block_sender'))
    good, skipped = _split(_ids_from(body), me)

    if len(good) > jd.BULK_ADMIN_ABOVE and not jd.is_admin(me):
        return jsonify(
            ok=False,
            error=(f'More than {jd.BULK_ADMIN_ABOVE} at once needs an '
                   f'administrator to confirm.')), 403

    over, total = jd.over_cap(me, adding=len(good))
    done, failed = [], []
    for lead in good:
        try:
            done.append(jd.mark_not_relevant(lead, reason=reason, note=note,
                                  actor=me, block_sender=block,
                                  commit=False))
        except ValueError as exc:
            failed.append({'id': lead.id, 'why': str(exc)})
    if not done and failed:
        db.session.rollback()
        return jsonify(ok=False, error=failed[0]['why'],
                       failed=failed), 400
    if over:
        jd.tell_the_admin(me, total)
    db.session.commit()
    return jsonify(ok=True, deleted=len(done),
                   skipped=len(skipped) + len(failed),
                   skipped_rows=skipped, failed=failed,
                   over_cap=over)


# ── the bin ─────────────────────────────────────────────────────────
def _filters():
    args = request.args
    out = {}
    for key, arg in (('by', 'by'), ('reason', 'reason'),
                     ('sender', 'sender')):
        value = (args.get(arg) or '').strip()
        if value:
            out[key] = value
    for key, arg in (('since', 'from'), ('until', 'to')):
        value = (args.get(arg) or '').strip()
        if value:
            try:
                out[key] = datetime.strptime(value, '%Y-%m-%d')
            except ValueError:
                pass
    if 'until' in out:
        out['until'] = out['until'] + timedelta(days=1)
    return out


@bp.route('/admin/deleted-leads')
@_junk
@_admin_only
def bin_page():
    from app.services import junk_delete as jd
    from app.services import flags

    rows = [jd.row(l) for l in jd.deleted(**_filters()).all()]
    return render_template(
        'admin/deleted_leads.html', rows=rows,
        reasons=jd.REASONS, filters=request.args,
        purge_on=flags.on_for('FEATURE_JUNK_PURGE', _me()),
        purge_days=PURGE_AFTER_DAYS)


@bp.route('/api/admin/deleted-leads')
@_junk
@_admin_only
def bin_json():
    from app.services import junk_delete as jd

    rows = [jd.row(l) for l in jd.deleted(**_filters()).all()]
    return jsonify(ok=True, rows=rows, count=len(rows))


@bp.route('/api/admin/deleted-leads/<int:lid>/restore', methods=['POST'])
@_junk
@_admin_only
def restore(lid):
    from app.services import junk_delete as jd

    lead = _lead(lid)
    try:
        out = jd.restore(lead, actor=_me())
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(ok=True, **out)


@bp.route('/api/admin/deleted-leads/export')
@_junk
@_admin_only
def export():
    """The bin as a spreadsheet. Required before a purge, and useful
    on its own — somebody has to be able to read what was removed
    without a database client."""
    from app.services import junk_delete as jd

    rows = [jd.row(l) for l in jd.deleted(**_filters()).all()]
    buf = io.BytesIO()
    try:
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = 'Deleted leads'
        headers = ['Lead', 'Company', 'Sender', 'Subject', 'Reason',
                   'Note', 'Deleted by', 'Deleted at', 'Owner',
                   'Vertical']
        ws.append(headers)
        for r in rows:
            ws.append([r['id'], r['company'], r['sender'], r['subject'],
                       r['reason_label'], r['note'], r['by'], r['at'],
                       r['owner'], r['vertical']])
        wb.save(buf)
        name = 'deleted_leads.xlsx'
        mime = ('application/vnd.openxmlformats-officedocument'
                '.spreadsheetml.sheet')
    except Exception:                                     # noqa: BLE001
        # No openpyxl on this box: CSV still answers the question,
        # and a failed export is not a reason to block a purge that
        # somebody has already thought about.
        import csv

        text = io.StringIO()
        writer = csv.writer(text)
        writer.writerow(['Lead', 'Company', 'Sender', 'Subject',
                         'Reason', 'Note', 'Deleted by', 'Deleted at',
                         'Owner', 'Vertical'])
        for r in rows:
            writer.writerow([r['id'], r['company'], r['sender'],
                             r['subject'], r['reason_label'], r['note'],
                             r['by'], r['at'], r['owner'],
                             r['vertical']])
        buf = io.BytesIO(text.getvalue().encode('utf-8'))
        name, mime = 'deleted_leads.csv', 'text/csv'
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=name,
                     mimetype=mime)


#: Nothing is purged before this. Ninety days is long enough that a
#: quarter-end review still finds it.
PURGE_AFTER_DAYS = 90


@bp.route('/api/admin/deleted-leads/purge', methods=['POST'])
@_junk
@_admin_only
def purge():
    """Two steps, and even then it closes rather than destroys.

    The spec calls this a purge and the word is kept, because that is
    what an administrator is asking for. What it does is take the
    lead out of the bin and leave it closed as Lost with its reason
    — CRM-NO-DELETE-01 has no exceptions, and a bin that could
    destroy rows would be the old delete wearing a different hat.
    """
    from app import Lead, db
    from app.services import audit, flags, junk_delete as jd

    if not flags.on_for('FEATURE_JUNK_PURGE', _me()):
        return jsonify(ok=False,
                       error='Purge is switched off.'), 403
    body = request.get_json(silent=True) or {}
    cutoff = datetime.utcnow() - timedelta(days=PURGE_AFTER_DAYS)
    rows = (Lead.query
            .filter(Lead.junk_deleted.is_(True),
                    Lead.junk_deleted_at < cutoff).all())
    if not body.get('confirm') or not body.get('exported'):
        return jsonify(
            ok=True, staged=True, count=len(rows),
            message=(f'{len(rows)} lead(s) older than '
                     f'{PURGE_AFTER_DAYS} days. Export them first, '
                     f'then confirm.'))
    me = _me()
    for lead in rows:
        lead.junk_deleted = False
        lead.stage = 'Lost'
        lead.lost_reason = 'Others'
        lead.is_archived = True
        lead.archive_reason = (
            f'Purged from the deleted bin: '
            f'{jd.LABELS.get(lead.junk_reason, lead.junk_reason or "")}'
        )[:200]
        audit.record('lead.junk_purged', 'lead', lead.id,
                     new={'stage': 'Lost'},
                     reason=f'purged after {PURGE_AFTER_DAYS} days',
                     actor=me)
    db.session.commit()
    return jsonify(ok=True, purged=len(rows))
