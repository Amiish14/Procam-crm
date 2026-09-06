"""Master Data Import / Export Center — §42-50."""
import json
import os

from flask import (Blueprint, jsonify, render_template, request, session,
                   send_file)

from app import db
from app.access.service import require
from app.excel_io import service as xl
from app.excel_io.templates import TEMPLATES

bp = Blueprint('excel_io', __name__)

PERM = 'admin.master'
MAX_BYTES = 12 * 1024 * 1024


@bp.route('/admin/import')
@require(PERM)
def page():
    from app import ImportBatch
    recent = (ImportBatch.query.order_by(ImportBatch.id.desc())
              .limit(20).all())
    for b in recent:
        try:
            b.counts = json.loads(b.preview_data or '{}')
        except Exception:
            b.counts = {}
    return render_template('excel_io/index.html',
                           templates=TEMPLATES.values(), recent=recent,
                           modes=xl.MODES)


@bp.route('/admin/import/template/<kind>')
@require(PERM)
def download_template(kind):
    if kind not in TEMPLATES:
        return jsonify(ok=False, error='No such template'), 404
    stream = xl.build_template(kind)
    return send_file(
        stream, as_attachment=True,
        download_name=f'procam_{kind}_template.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.'
                 'spreadsheetml.sheet')


@bp.route('/admin/import/preview', methods=['POST'])
@require(PERM)
def preview():
    """§47 — parse and validate, write nothing."""
    kind = (request.form.get('kind') or '').strip()
    mode = (request.form.get('mode') or xl.MODE_UPSERT).strip()
    if kind not in TEMPLATES:
        return jsonify(ok=False, error='Pick a template'), 400
    if mode not in xl.MODES:
        return jsonify(ok=False, error='Unknown import mode'), 400

    upload = request.files.get('file')
    if upload is None or not upload.filename:
        return jsonify(ok=False, error='Choose a file'), 400
    if not upload.filename.lower().endswith(('.xlsx', '.xlsm')):
        return jsonify(ok=False,
                       error='Upload an .xlsx file exported from the '
                             'template'), 400

    upload.stream.seek(0, os.SEEK_END)
    if upload.stream.tell() > MAX_BYTES:
        return jsonify(ok=False, error='File is larger than 12 MB'), 400
    upload.stream.seek(0)

    batch = xl.stage(upload, kind, session.get('emp_code'))
    upload.stream.seek(0)
    records, problems = xl.parse(upload, kind)
    if problems:
        return jsonify(ok=False, error=problems[0], batch_id=batch.id), 400

    results = xl.validate(records, kind, mode)
    summary = xl.summarise(results)
    batch.total_rows = summary['total']
    batch.valid_rows = summary['valid']
    batch.error_rows = summary['errors']
    db.session.commit()

    return jsonify(ok=True, batch_id=batch.id, kind=kind, mode=mode,
                   summary=summary,
                   sample=[{'row': r['row'], 'action': r['action'],
                            'errors': r['errors'], 'warnings': r['warnings'],
                            'name': (r['data'].get('name')
                                     or r['data'].get('company')
                                     or r['data'].get('_person_name') or '')}
                           for r in results[:60]])


@bp.route('/admin/import/commit', methods=['POST'])
@require(PERM)
def commit():
    from app import ImportBatch
    d = request.get_json(silent=True) or {}
    batch = ImportBatch.query.get(d.get('batch_id') or 0)
    if batch is None:
        return jsonify(ok=False, error='That upload has expired — upload '
                                       'the file again'), 404
    if batch.committed:
        return jsonify(ok=False, error='This import was already applied'), 400

    path = xl.staged_path(batch.id)
    if not os.path.exists(path):
        return jsonify(ok=False, error='The staged file is no longer '
                                       'available — upload it again'), 410

    mode = (d.get('mode') or xl.MODE_UPSERT).strip()
    with open(path, 'rb') as fh:
        records, problems = xl.parse(fh, batch.kind)
    if problems:
        return jsonify(ok=False, error=problems[0]), 400

    results = xl.validate(records, batch.kind, mode)
    counts = xl.commit(results, batch.kind, mode, batch,
                       session.get('emp_code'))
    return jsonify(ok=True, counts=counts, batch_id=batch.id,
                   errors=xl.summarise(results)['errors'])


@bp.route('/admin/import/<int:batch_id>/errors')
@require(PERM)
def error_report(batch_id):
    """§49 — the rows that failed, as a file to fix and re-upload."""
    from app import ImportBatch
    batch = ImportBatch.query.get(batch_id)
    if batch is None:
        return jsonify(ok=False, error='No such import'), 404
    path = xl.staged_path(batch.id)
    if not os.path.exists(path):
        return jsonify(ok=False, error='The staged file is no longer '
                                       'available'), 410

    with open(path, 'rb') as fh:
        records, problems = xl.parse(fh, batch.kind)
    if problems:
        return jsonify(ok=False, error=problems[0]), 400
    results = xl.validate(records, batch.kind, xl.MODE_UPSERT)

    stream = xl.build_error_report(results, batch.kind)
    return send_file(
        stream, as_attachment=True,
        download_name=f'procam_import_{batch.id}_errors.xlsx',
        mimetype='application/vnd.openxmlformats-officedocument.'
                 'spreadsheetml.sheet')
