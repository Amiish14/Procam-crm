"""Company 360 and the filtered views — §6, §12-19, §87.

§87 requires that clicking a company from Global CRM, an Account, a
Competitor, an Overseas Partner, an Opportunity, an RFQ or a Quote all
resolve to the SAME record.  One route does that: /companies/<id>.

§6 requires Customers, Competitors and Overseas Agents to be filtered
views of Company Master rather than separate lists, so /companies takes a
?relationship= filter and the named routes are thin aliases of it.
"""
from flask import (Blueprint, jsonify, render_template, request, session,
                   redirect)

from app import db
from app.access.service import require
from app.company360 import service as c360
from app.services.urls import prefixed as _prefixed, login_url as _login_url

bp = Blueprint('company360', __name__)


def _login_required(f):
    from functools import wraps

    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            if request.path.startswith('/api/'):
                return jsonify(ok=False, error='Not authenticated'), 401
            return ('', 302, {'Location': _login_url()})
        return f(*a, **kw)
    return wrap


# ── the one company view ─────────────────────────────────────────────
@bp.route('/companies/<int:company_id>')
@_login_required
def company_360(company_id):
    from app import Company
    company = Company.query.get(company_id)
    if company is None:
        return render_template('company/not_found.html',
                               company_id=company_id), 404

    # A company merged by de-duplication redirects to its survivor, so old
    # links and bookmarks keep working (§87).
    if not company.is_active and '[merged into #' in (company.name or ''):
        try:
            survivor = int(company.name.split('[merged into #')[1]
                           .rstrip(']').strip())
            return redirect(f'/companies/{survivor}')
        except Exception:
            pass

    from app.master_data import service as md
    return render_template('company/detail.html', company=company,
                           data=c360.full(company),
                           relationships=[i.label for i in
                                          md.items('relationship')])


@bp.route('/api/companies/<int:company_id>/360')
@_login_required
def api_company_360(company_id):
    from app import Company
    company = Company.query.get(company_id)
    if company is None:
        return jsonify(ok=False, error='No such company'), 404
    return jsonify(ok=True, **c360.full(company))


# ── §6 filtered views of the one master ──────────────────────────────
@bp.route('/companies')
@_login_required
def company_list():
    from app import Company
    from presales.models import AccountRelationshipTag

    rel = (request.args.get('relationship') or '').strip()
    term = (request.args.get('q') or '').strip()

    q = Company.query.filter(Company.is_active.is_(True))
    if rel:
        ids = [t.account_id for t in
               AccountRelationshipTag.query.filter_by(tag=rel).all()]
        q = q.filter(Company.id.in_(ids or [0]))
    if term:
        q = q.filter(Company.name.ilike(f'%{term}%'))

    rows = q.order_by(Company.name).limit(500).all()

    tags = {}
    for t in AccountRelationshipTag.query.filter(
            AccountRelationshipTag.account_id.in_(
                [c.id for c in rows] or [0])).all():
        tags.setdefault(t.account_id, []).append(t.tag)

    from app.master_data import service as md
    return render_template(
        'company/list.html', companies=rows, tags=tags,
        relationship=rel, term=term,
        relationships=[i.label for i in md.items('relationship')],
        total=q.count())


# Named aliases, so existing links and habits keep working while the data
# behind them is one master (§6, "without unnecessarily breaking existing
# URLs").
@bp.route('/customers')
@_login_required
def customers():
    return redirect(_prefixed('/companies?relationship=Customer'))


@bp.route('/overseas-partners')
@_login_required
def overseas_partners():
    return redirect(_prefixed('/companies?relationship=Overseas Partner'))


# ── classifications (§5, §11) ────────────────────────────────────────
@bp.route('/api/companies/<int:company_id>/classifications',
          methods=['POST'])
@_login_required
def api_set_classifications(company_id):
    """Replace the relationship types on a company.

    One company, many classifications — the §4 principle. ABC Global may
    be Overseas Partner AND Vendor AND Competitor without becoming three
    records.
    """
    from app import Company
    from presales.models import AccountRelationshipTag

    company = Company.query.get(company_id)
    if company is None:
        return jsonify(ok=False, error='No such company'), 404

    from app.master_data import service as md
    allowed = {i.label for i in md.items('relationship')}
    wanted = {t.strip() for t in (request.get_json(silent=True) or {})
              .get('classifications', []) if t and t.strip()}
    unknown = wanted - allowed
    if unknown:
        return jsonify(ok=False,
                       error=f'Not a known relationship type: '
                             f'{", ".join(sorted(unknown))}. '
                             f'Add it under Master Data first.'), 400

    existing = {t.tag: t for t in AccountRelationshipTag.query.filter_by(
        account_id=company_id).all()}
    for tag in wanted - set(existing):
        db.session.add(AccountRelationshipTag(account_id=company_id, tag=tag))
    for tag in set(existing) - wanted:
        db.session.delete(existing[tag])
    db.session.commit()

    return jsonify(ok=True, classifications=sorted(wanted))
