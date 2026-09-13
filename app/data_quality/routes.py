"""Data Quality dashboard — §66.

Every figure and row here is measured inside the viewer's Access Matrix
scope. The permission to open the module is admin.master, as it always
was; seeing the whole company's problems additionally needs a scope that
reaches the whole company.
"""
import csv
import io

from flask import (Blueprint, Response, jsonify, render_template, request,
                   session)

from app.access.service import require
from app.data_quality import batch
from app.data_quality import definitions as defs
from app.data_quality import service as dq

bp = Blueprint('data_quality', __name__)

PERM = 'admin.master'


def _scope():
    from app.access import scope
    return scope.current()


def _page_arg():
    try:
        return max(1, int(request.args.get('page') or 1))
    except ValueError:
        return 1


def _trends_for(sc, checks):
    """Sparklines, only for a viewer who sees the whole company.

    Snapshots are company-wide totals. Drawn for an Own-scope viewer they
    would publish exactly the counts their scope withholds.
    """
    if not sc.unrestricted:
        return {}
    series = dq.trends()
    out = {}
    for c in checks:
        points = series.get(c['key']) or []
        out[c['key']] = {
            'points': dq.sparkline(points),
            'first': points[0][1] if points else None,
            'days': len(points),
        }
    return out


_ORDER = {s: i for i, s in enumerate(defs.SEVERITIES)}


@bp.route('/admin/data-quality')
@require(PERM)
def page():
    sc = _scope()
    checks = dq.summary(sc)
    # Worst first, and within a severity the biggest problem first.
    checks.sort(key=lambda c: (_ORDER.get(c['severity'], 9),
                               -(c['count'] or 0)))
    return render_template(
        'data_quality/index.html', checks=checks,
        critical=[c for c in checks if c['severity'] == 'critical'
                  and (c['count'] or 0) > 0],
        total=sum(c['count'] or 0 for c in checks),
        trends=_trends_for(sc, checks), whole_company=sc.unrestricted)


@bp.route('/api/data-quality')
@require(PERM)
def api():
    sc = _scope()
    checks = dq.summary(sc)
    # Company-wide totals: withheld from a viewer whose scope is narrower.
    series = dq.trends() if sc.unrestricted else {}
    for c in checks:
        c['trend'] = [{'date': str(d), 'count': n}
                      for d, n in series.get(c['key']) or []]
    return jsonify(ok=True, checks=checks, whole_company=sc.unrestricted)


@bp.route('/admin/data-quality/<key>')
@require(PERM)
def detail(key):
    """The records behind one number."""
    sc = _scope()
    data = dq.records_for(key, sc=sc, page=_page_arg(),
                          per_page=defs.PAGE_SIZE)
    if data is None:
        return render_template('data_quality/index.html',
                               checks=dq.summary(sc), critical=[], total=0,
                               trends={}, whole_company=sc.unrestricted), 404
    employees, verticals = [], []
    kinds = {a['value'] for a in data['actions']}
    if 'employee' in kinds:
        from app import Employee
        employees = [{'code': e.emp_code, 'name': e.name} for e in
                     Employee.query.filter_by(is_active=True)
                     .order_by(Employee.name).all()]
    if 'vertical' in kinds:
        from app.master_data import service as md
        verticals = [i.label for i in md.items('vertical')]
    return render_template('data_quality/detail.html', employees=employees,
                           verticals=verticals, batch_max=defs.BATCH_MAX,
                           **data)


@bp.route('/api/data-quality/<key>')
@require(PERM)
def api_detail(key):
    per_page = min(request.args.get('per_page', type=int) or 500, 500)
    data = dq.records_for(key, sc=_scope(), page=_page_arg(),
                          per_page=max(1, per_page))
    if data is None:
        return jsonify(ok=False, error=f'Unknown check "{key}"'), 404
    return jsonify(ok=True, **data)


@bp.route('/admin/data-quality/<key>/export.csv')
@require(PERM)
def export(key):
    """Every record behind one check, as a spreadsheet-safe CSV."""
    from app.services import audit
    from app.utils.spreadsheet_safe import safe_row

    out = dq.export_rows(key, sc=_scope())
    if out is None:
        return jsonify(ok=False, error=f'Unknown check "{key}"'), 404
    header, rows, truncated = out
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(safe_row(header))
    for r in rows:
        # Names, subjects and addresses are typed by outsiders; a cell
        # starting "=" must open as text, not run as a formula.
        w.writerow(safe_row(r))
    audit.record('data_quality.export', 'data_quality', key,
                 new={'rows': len(rows), 'truncated': truncated},
                 commit=True)
    return Response(buf.getvalue(), mimetype='text/csv', headers={
        'Content-Disposition':
            f'attachment; filename=data_quality_{key}.csv'})


def _body():
    d = request.get_json(silent=True)
    return d if isinstance(d, dict) else None


def _refused(e):
    return jsonify(ok=False, error=e.message, **e.extra), e.status


@bp.route('/api/data-quality/<key>/preview', methods=['POST'])
@require(PERM)
def api_preview(key):
    """Exactly what a batch correction would change. Changes nothing."""
    d = _body()
    if d is None:
        return jsonify(ok=False, error='Send the selection as JSON.'), 400
    try:
        out = batch.preview(key, d.get('ids'), d.get('action'),
                            d.get('value'), sc=_scope(),
                            actor=session.get('emp_code'))
    except batch.BatchRefused as e:
        return _refused(e)
    return jsonify(ok=True, **out)


@bp.route('/api/data-quality/<key>/apply', methods=['POST'])
@require(PERM)
def api_apply(key):
    """Apply a previewed batch correction — only if it is still exactly
    what was previewed."""
    d = _body()
    if d is None:
        return jsonify(ok=False, error='Send the selection as JSON.'), 400
    try:
        out = batch.apply(key, d.get('ids'), d.get('action'), d.get('value'),
                          d.get('reason'), d.get('preview_token'),
                          sc=_scope(), actor=session.get('emp_code'))
    except batch.BatchRefused as e:
        return _refused(e)
    return jsonify(ok=True, **out)
