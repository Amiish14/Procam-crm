"""
CRM 2.0: the cockpit, global search and the stage map.

    GET  /crm2                            the cockpit
    GET  /api/crm2/cockpit                what the cockpit shows
    GET  /api/crm2/search?q=              one box, every record type
    GET  /api/crm2/pipeline               the admin map, stage by stage
    GET  /api/crm2/leads/<id>/stage-map   one lead's line
    GET  /api/crm2/my-work                next action, overdue, escalated
    POST /api/crm2/leads/<id>/confirm-value   the owner agrees

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


def _on(emp_code=None):
    """On, and on for this person.

    While CRM 2.0 is being reviewed the flag is limited to the people
    reviewing it, so the cockpit exists for them and 404s for
    everybody else. The limit narrows and never grants: a flag that
    is off is off for all.
    """
    from app.services import flags
    try:
        return bool(flags.on_for('FEATURE_CRM2', emp_code))
    except Exception:                                     # noqa: BLE001
        return False


def _crm2(f):
    """The feature switch, then a session. Records are then scoped
    per record, exactly as the Workbench scopes them."""
    @wraps(f)
    def wrap(*a, **kw):
        emp = session.get('emp_code')
        if not _on(emp):
            # 404, not 403: from outside, an unbuilt feature should
            # look like it does not exist — and somebody outside the
            # review group should not learn that it exists either.
            return jsonify(ok=False, error='Not found'), 404
        if not emp:
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
        # CRM-ACCOUNT-FIRST-01 §3.6. `unlinked` above counts leads
        # with no company_id; this counts the ones the gate is
        # actually holding, which also includes leads whose account
        # nobody owns for their vertical. Zero while that flag is
        # off, so the tile simply does not appear.
        'accounts_to_confirm': len(_pending_accounts()),
        # §6.5. Empty dict while the chain is off, so the cockpit
        # is exactly the page it was.
        'panels': _chain_panels(),
    })


def _chain_panels():
    """Waiting on me, overdue, unmatched mail, RFQs expected."""
    from flask import session

    try:
        from app.services import rfq_chain, rfq_reports
        if not rfq_chain.enabled():
            return {}
        if not rfq_reports.enabled():
            # The counts come from the reports service; without it
            # the panels would be half-filled, which is worse than
            # absent.
            return {}
        from app.rfq_reports.routes import api_cockpit
        body = api_cockpit()
        data = body.get_json() if hasattr(body, 'get_json') else {}
        return (data or {}).get('panels', {})
    except Exception:                                     # noqa: BLE001
        return {}


def _pending_accounts():
    from app.services import account_gate
    try:
        return account_gate.pending_leads(sc=sc_mod.current())
    except Exception:                                     # noqa: BLE001
        return []


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

    # §6.5 — an RFQ or quotation number is the thing people have in
    # front of them when they search. Behind the chain's own flag,
    # so a search is exactly what it was until that goes on.
    out.extend(_rfq_and_quote_hits(q, like, sc))

    counts = {}
    for r in out:
        counts[r['kind']] = counts.get(r['kind'], 0) + 1
    return jsonify(ok=True, query=q, results=out, counts=counts,
                   capped=PER_KIND)


def _rfq_and_quote_hits(q, like, sc):
    """RFQ numbers, and quotations by their displayed number.

    A customer quotes "QUO-2026-0045 / Rev-01" back at us, and the
    revision's own stored number is a different string — so the
    search matches the root's number and then names the revision,
    which is what the person holding the mail typed.
    """
    out = []
    try:
        from app.services import rfq_chain
        if not rfq_chain.enabled():
            return out
    except Exception:                                     # noqa: BLE001
        return out

    try:
        from app.access import records
        from app.models.quote import Quote
        from app.models.rfq import RFQ

        for rfq in (records.rfqs(sc=sc)
                    .filter(db_or(RFQ.rfq_number.ilike(like),
                                  RFQ.subject.ilike(like)))
                    .order_by(RFQ.id.desc()).limit(PER_KIND).all()):
            out.append({
                'kind': 'rfq', 'id': rfq.id,
                'title': rfq.rfq_number or f'RFQ #{rfq.id}',
                'detail': (rfq.subject or '')[:70]
                          or (rfq.priority or ''),
                'url': (f'/leads/{rfq.lead_id}/journey'
                        if rfq.lead_id else '/rfqs/chain')})

        from app.services import quote_rev
        roots = (records.quotes(sc=sc)
                 .filter(Quote.quote_number.ilike(like))
                 .order_by(Quote.id.desc()).limit(PER_KIND).all())
        seen = set()
        for quote in roots:
            for row in (quote_rev.chain(quote) or [quote]):
                if row.id in seen:
                    continue
                seen.add(row.id)
                out.append({
                    'kind': 'quote', 'id': row.id,
                    'title': quote_rev.display_number(row),
                    'detail': f'{row.status or ""} '
                              f'{row.currency or ""} '
                              f'{float(row.total_amount or 0):,.0f}'
                              .strip(),
                    'url': f'/quotes/{row.id}'})
                if len(seen) >= PER_KIND:
                    break
    except Exception:                                     # noqa: BLE001
        pass
    return out


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


# ── R3: one person's work, from the engines that already decide it ───
@bp.route('/api/crm2/my-work')
@_crm2
def api_my_work():
    """What now, what is late, what has been pushed at me.

    Every number here is fetched from an engine that already exists —
    the Workbench's own item list and the task engine's escalation
    columns. The cockpit adds the arrangement and nothing else. A
    second opinion about "overdue" is exactly how this CRM ended up
    with 7,318 open tasks and a twin row for each escalation.
    """
    from app.services import cockpit

    emp = session.get('emp_code')
    return jsonify(ok=True, work=cockpit.for_person(
        sc_mod.current(), emp))


@bp.route('/api/crm2/leads/<int:lid>/confirm-value', methods=['POST'])
@_crm2
def api_confirm_value(lid):
    """The owner agrees with a figure copied from the opportunity,
    or replaces it.

    118 open leads carry a value copied from the opportunity row
    beside them, marked unconfirmed. Until somebody says the number
    is right it is evidence, not a forecast, so the lead stays on the
    Value missing tile and this is the button that takes it off.
    """
    from app import Lead, db
    from app.services import audit

    emp = session.get('emp_code')
    lead = sc_mod.leads(Lead.query.filter(Lead.id == lid),
                        sc=sc_mod.current()).first()
    if lead is None:
        return jsonify(ok=False, error='Not found'), 404

    body = request.get_json(silent=True) or {}
    was = (float(lead.estimated_value_inr)
           if lead.estimated_value_inr else None)
    amount = body.get('estimated_value')
    if amount not in (None, ''):
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return jsonify(ok=False,
                           error='That is not a number.'), 400
        if amount <= 0:
            return jsonify(
                ok=False,
                error='A value has to be more than nothing.'), 400
        lead.estimated_value_inr = amount
        lead.value_source = 'owner'
    elif was is None:
        return jsonify(
            ok=False,
            error='There is no figure to confirm. Enter one.'), 400

    lead.value_confirmed = True
    audit.record('lead.value_confirmed', 'lead', str(lead.id),
                 old={'estimated_value_inr': was,
                      'value_confirmed': False},
                 new={'estimated_value_inr':
                      float(lead.estimated_value_inr),
                      'value_source': lead.value_source,
                      'value_confirmed': True},
                 actor=emp,
                 reason=('confirmed as it stood' if amount in (None, '')
                         else 'replaced with the owner\'s own figure'))
    db.session.commit()
    try:
        from app.workbench import service as wb
        wb.hygiene_cache_clear()
    except Exception:                                     # noqa: BLE001
        pass
    return jsonify(ok=True, lead_id=lead.id,
                   estimated_value=float(lead.estimated_value_inr),
                   value_source=lead.value_source,
                   value_confirmed=True)
