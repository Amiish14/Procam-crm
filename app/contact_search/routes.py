"""Find a person — the search screen and its JSON.

CRM-SEARCH-SCAN-01 Part 1.

    GET  /contacts/search                    the screen
    GET  /api/contacts/search                the results
    GET  /api/contacts/search/suggest        type-ahead values
    GET  /api/contacts/search/duplicates     before a new contact is made

Every route is behind the ordinary sign-in and every query runs
through `app/access/scope.py`, so this shows a person exactly what the
rest of the CRM shows them and nothing more. A search box is the
easiest place in a CRM to leak a record by accident.
"""
from __future__ import annotations

from functools import wraps

from flask import (Blueprint, jsonify, render_template, request, session)

bp = Blueprint('contact_search', __name__)

FIELDS = ('name', 'company', 'country', 'location', 'vertical',
          'industry')


def _require_auth(f):
    @wraps(f)
    def wrap(*a, **kw):
        if 'emp_code' not in session:
            return jsonify(ok=False, error='Sign in first.'), 401
        return f(*a, **kw)
    return wrap


def _asked():
    return {name: (request.args.get(name) or '').strip()
            for name in FIELDS}


@bp.route('/contacts/search')
def page():
    from flask import redirect, url_for

    if 'emp_code' not in session:
        return redirect(url_for('login'))
    return render_template('contact_search.html')


@bp.route('/api/contacts/search')
@_require_auth
def api_search():
    from app.services import people_search

    asked = _asked()
    if not any(asked.values()):
        return jsonify(ok=True, rows=[], total=0, page=1, pages=0,
                       asked={}, took_ms=0,
                       note='Fill at least one field. Every field you '
                            'fill narrows the answer — they are ANDed.')
    found = people_search.search(
        page=request.args.get('page', type=int) or 1,
        per_page=request.args.get('per_page', type=int)
        or people_search.PER_PAGE,
        sort=(request.args.get('sort') or 'name'),
        descending=(request.args.get('dir') == 'desc'),
        **asked)
    found['ok'] = True
    return jsonify(found)


@bp.route('/api/contacts/search/suggest')
@_require_auth
def api_suggest():
    from app.services import people_search

    field = (request.args.get('field') or '').strip()
    if field not in ('company', 'country', 'location', 'vertical',
                     'industry'):
        return jsonify(ok=False, error='No such field.'), 400
    return jsonify(ok=True, field=field,
                   values=people_search.suggest(
                       field, request.args.get('q') or ''))


@bp.route('/api/contacts/search/duplicates')
@_require_auth
def api_duplicates():
    """Asked by the new-contact form and by the card review screen."""
    from app.services import people_search

    rows = people_search.duplicates_of(
        email=request.args.get('email') or '',
        phone=request.args.get('phone') or '',
        name=request.args.get('name') or '',
        company=request.args.get('company') or '')
    return jsonify(ok=True, rows=rows, count=len(rows))


@bp.route('/api/contacts/search/country-health')
@_require_auth
def api_country_health():
    """Whether the country filter can be trusted today.

    `api_create_contact` has always defaulted country to 'India', so
    the column is 100% filled and says nothing. A filter that looks
    authoritative and is not is worse than one marked as unreliable,
    so the screen asks and prints what it is told.
    """
    from app import Contact, db

    rows = db.session.query(Contact.country, db.func.count(Contact.id)) \
        .group_by(Contact.country).all()
    total = sum(n for _v, n in rows) or 0
    spread = sorted(((v or '(blank)'), n) for v, n in rows)
    top = max(((v or '(blank)'), n) for v, n in rows) if rows else ('', 0)
    dominant = (100.0 * top[1] / total) if total else 0.0
    return jsonify(
        ok=True, total=total,
        values=[{'value': v, 'count': n} for v, n in
                sorted(spread, key=lambda x: -x[1])[:12]],
        distinct=len(rows),
        dominant={'value': top[0], 'count': top[1],
                  'percent': round(dominant, 1)},
        trustworthy=bool(total and dominant < 90.0),
        note=('Every contact carries a country because the create form '
              'has always defaulted it. Until that is corrected, a '
              'country filter answers about the default, not about the '
              'person.') if dominant >= 90.0 else '')
