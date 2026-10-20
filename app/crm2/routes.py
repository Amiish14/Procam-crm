"""
CRM 2.0: the cockpit, global search and the stage map.

    GET  /crm2                            the cockpit
    GET  /api/crm2/cockpit                what the cockpit shows
    GET  /api/crm2/search?q=              one box, every record type
    GET  /api/crm2/pipeline               the admin map, stage by stage
    GET  /api/crm2/leads/<id>/stage-map   one lead's line

All behind FEATURE_CRM2. With the flag off every route 404s and no
existing screen changes — the cockpit is a new page beside the old
landing pages, not a replacement for them, until it is switched on.

Scope is not relaxed anywhere here. Leads come through
`sales_rules`/`access.scope`, the same path the Workbench uses, so a
salesperson searching finds their own book and an administrator finds
everything. A global search box is exactly where a scope leak would
go unnoticed, so the test suite asserts the narrow case rather than
trusting the query.
"""
from __future__ import annotations

from functools import wraps

from flask import Blueprint, jsonify, render_template, request, session

from app.access import scope as sc_mod
from app.services import sales_rules as rules
from app.services import stage_map

bp = Blueprint('crm2', __name__)

#: A search that returns everything is a search nobody reads, and an
#: unbounded LIKE over 10,858 leads on every keystroke is a cost
#: nobody sees until the box is popular.
PER_KIND = 8
MIN_QUERY = 2


def _on():
    from app.services import flags
    try:
        return bool(flags.on('FEATURE_CRM2'))
    except Exception:                                     # noqa: BLE001
        return False


def _crm2(f):
    """The feature switch, then a session. Records are then scoped
    per record, exactly as the Workbench scopes them."""
    @wraps(f)
    def wrap(*a, **kw):
        if not _on():
            # 404, not 403: from outside, an unbuilt feature should
            # look like it does not exist.
            return jsonify(ok=False, error='Not found'), 404
        if not session.get('emp_code'):
            return jsonify(ok=False, error='Not authenticated'), 401
        return f(*a, **kw)
    return wrap


# ── R1: the cockpit ──────────────────────────────────────────────────
@bp.route('/crm2')
@_crm2
def cockpit_page():
    """One landing page instead of two.

    Today `/` sends administrators to /dashboard and everyone else to
    /my-work (app.py:1554), so two groups of people have never seen
    the same screen and no conversation about "the home page" has
    meant one thing.
    """
    return render_template('crm2/home.html')


@bp.route('/api/crm2/cockpit')
@_crm2
def api_cockpit():
    from app.workbench import service as wb

    sc = sc_mod.current()
    # (items, ageing) — the second half is the RFQ ageing buckets,
    # which the cockpit does not show.
    items, _ageing = wb.lead_items(sc, include_quiet=True)
    leads = rules.open_leads(sc=sc).all()
    rows = stage_map.pipeline(leads)

    value_known = sum(1 for l in leads
                      if (l.estimated_value_inr or 0) > 0)
    unowned = sum(1 for l in leads if not (l.assigned_to or '').strip())
    unlinked = sum(1 for l in leads if not l.company_id)

    return jsonify(ok=True, cockpit={
        'open_leads': len(leads),
        # A quiet lead is grouped as "nothing outstanding"; anything
        # else tripped at least one condition.
        'needing_attention': sum(
            1 for i in items
            if i.get('group') != wb.G_NOTHING_OUTSTANDING),
        'value_known': value_known,
        'value_missing': len(leads) - value_known,
        'unowned': unowned,
        'unlinked': unlinked,
        'pipeline': rows,
        'stage_dates_missing': sum(r['grey'] for r in rows),
    })


# ── R1: one search box ───────────────────────────────────────────────
@bp.route('/api/crm2/search')
@_crm2
def api_search():
    """Leads, accounts, contacts — one box.

    Five separate filter inputs exist today, one per tab
    (templates/app.html:1323 and its siblings), and none of them finds
    an account from a lead screen.
    """
    from app import Company, Contact, Lead

    q = (request.args.get('q') or '').strip()
    if len(q) < MIN_QUERY:
        return jsonify(ok=True, query=q, results=[], counts={},
                       note=f'type at least {MIN_QUERY} characters')

    like = f'%{q}%'
    sc = sc_mod.current()
    out = []

    lead_q = sc_mod.leads(
        Lead.query.filter(Lead.is_archived.isnot(True)).filter(
            db_or(Lead.company.ilike(like), Lead.pic.ilike(like),
                  Lead.email.ilike(like), Lead.crn.ilike(like),
                  Lead.opp_number.ilike(like))), sc=sc)
    for l in lead_q.order_by(Lead.id.desc()).limit(PER_KIND).all():
        out.append({'kind': 'lead', 'id': l.id,
                    'title': l.company or '(no company)',
                    'detail': f'{l.stage or "—"} · '
                              f'{l.pic or l.email or "no contact"}',
                    'url': f'/?lead={l.id}'})

    for c in (Company.query
              .filter(Company.is_active.isnot(False))
              .filter(db_or(Company.name.ilike(like),
                            Company.city.ilike(like)))
              .order_by(Company.name.asc()).limit(PER_KIND).all()):
        out.append({'kind': 'account', 'id': c.id, 'title': c.name,
                    'detail': ' · '.join(
                        x for x in (c.city, c.vertical,
                                    c.dev_stage) if x) or '—',
                    'url': f'/company/{c.id}'})

    for p in (Contact.query
              .filter(db_or(Contact.name.ilike(like),
                            Contact.email.ilike(like)))
              .order_by(Contact.name.asc()).limit(PER_KIND).all()):
        out.append({'kind': 'contact', 'id': p.id,
                    'title': p.name or p.email or f'#{p.id}',
                    'detail': p.email or p.designation or '—',
                    'url': f'/company/{p.company_id}'
                           if p.company_id else ''})

    counts = {}
    for r in out:
        counts[r['kind']] = counts.get(r['kind'], 0) + 1
    return jsonify(ok=True, query=q, results=out, counts=counts,
                   capped=PER_KIND)


def db_or(*clauses):
    from sqlalchemy import or_
    return or_(*clauses)


# ── R2: the stage map ────────────────────────────────────────────────
@bp.route('/api/crm2/leads/<int:lid>/stage-map')
@_crm2
def api_stage_map(lid):
    from app import Lead

    sc = sc_mod.current()
    lead = sc_mod.leads(Lead.query.filter(Lead.id == lid),
                        sc=sc).first()
    if lead is None:
        return jsonify(ok=False, error='Not found'), 404
    return jsonify(ok=True, map=stage_map.for_lead(lead))


@bp.route('/api/crm2/pipeline')
@_crm2
def api_pipeline():
    """The same judgement over the whole book."""
    sc = sc_mod.current()
    leads = rules.open_leads(sc=sc).all()
    rows = stage_map.pipeline(leads)
    return jsonify(ok=True, pipeline=rows,
                   total=sum(r['total'] for r in rows),
                   stuck=sum(r['red'] for r in rows),
                   unknown=sum(r['grey'] for r in rows))
