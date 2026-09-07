"""
Competitor Master + Multiple Competitors per Deal + Intelligence
(Phases 9 & 10 of the CRM upgrade).

HTML views
    GET  /competitors                        list / search page
    GET  /competitors/<id>                   detail page (tabs)
    GET  /competitors/dashboard              interactive dashboard

JSON — competitor master
    GET  /api/competitors                    list / search (q, active_only)
    POST /api/competitors                    create (dedup by name.lower())
    GET  /api/competitors/<id>               full detail (deep)
    PATCH /api/competitors/<id>              update master fields
    GET  /api/competitors/<id>/performance   auto-computed stats

JSON — opportunity_competitors (junction)
    POST /api/opportunity-competitors                            attach
    PATCH /api/opportunity-competitors/<id>                      update
    DELETE /api/opportunity-competitors/<id>                     soft-delete
    GET  /api/opportunity-competitors?opportunity_id= | lead_id= | rfq_id= | quote_id=

JSON — contacts
    POST /api/competitors/<id>/contacts                          add
    PATCH /api/competitor-contacts/<id>                          update
    DELETE /api/competitor-contacts/<id>                         delete

JSON — intelligence
    GET  /api/competitors/<id>/intelligence?type=&from_date=&to_date=
    POST /api/competitors/<id>/intelligence                      log event

JSON — assessments
    POST /api/competitors/<id>/assessments                       save
    GET  /api/competitors/<id>/assessments                       list all

JSON — public sources (Phase 10)
    GET  /api/public-sources                                     list
    POST /api/public-sources                                     add
    GET  /api/public-source-items?competitor_id=                 list items
    POST /api/public-source-items                                add item
    POST /api/public-source-items/<id>/promote                   promote → intel
    POST /api/public-source-items/<id>/dismiss                   dismiss
"""
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Blueprint, jsonify, render_template, request, session,
                   redirect, url_for, current_app)
from sqlalchemy import or_, func

from app import db
from app.models.competitor import (
    CompetitorMaster, OpportunityCompetitor, CompetitorContact,
    CompetitorIntelligence, CompetitorAssessment,
)
from app.models.public_source import PublicSource, PublicSourceItem
from app.services.task_engine import on_state_change
from app.services.urls import prefixed as _prefixed, login_url as _login_url


bp = Blueprint('competitor', __name__)


# ─── auth helpers ────────────────────────────────────────────────────────
def _require_auth(f):
    """Gated by the Access Control matrix (module.competitors).

    Kept under the original name so every existing @_require_auth on this
    blueprint picks up the permission check without being touched.
    """
    from app.access.service import require as _require_perm
    return _require_perm('module.competitors')(f)


def _current_emp_code():
    return session.get('emp_code') or None


def _parse_date(v):
    if not v:
        return None
    try:
        return datetime.strptime(str(v)[:10], '%Y-%m-%d').date()
    except Exception:
        return None


def _num(v):
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    if v is None or v == '':
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _list_field(v):
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    # comma-separated fallback
    return [x.strip() for x in str(v).split(',') if x.strip()]


# ─── HTML views ──────────────────────────────────────────────────────────
@bp.route('/competitors')
@_require_auth
def competitor_list_page():
    """§6/§19 — Competitors are a filtered view of Company Master.

    competitor_masters was a second company master, which §4 forbids. The
    list now shows companies classified as Competitor, and each opens the
    same Company 360 any other route would reach (§87).
    """
    from flask import redirect
    return redirect(_prefixed('/companies?relationship=Competitor'))


@bp.route('/competitors/<int:cid>')
@_require_auth
def competitor_detail_page(cid):
    if not session.get('emp_code'):
        return redirect(url_for('login'))
    comp = CompetitorMaster.query.get_or_404(cid)
    return render_template('competitor/detail.html', competitor=comp)


@bp.route('/competitors/dashboard')
@_require_auth
def competitor_dashboard_page():
    if not session.get('emp_code'):
        return redirect(url_for('login'))
    return render_template('competitor/dashboard.html')


# ═════════════════════════════════════════════════════════════════════════
# Competitor Master
# ═════════════════════════════════════════════════════════════════════════
@bp.route('/api/competitors', methods=['GET'])
@_require_auth
def api_list_competitors():
    q = (request.args.get('q') or '').strip()
    active_only = request.args.get('active_only', '1') in ('1', 'true', 'yes')

    query = CompetitorMaster.query
    if active_only:
        query = query.filter(CompetitorMaster.is_active.is_(True))
    if q:
        like = f'%{q.lower()}%'
        query = query.filter(func.lower(CompetitorMaster.name).like(like))
    rows = query.order_by(CompetitorMaster.name.asc()).limit(500).all()
    return jsonify(items=[r.to_dict() for r in rows])


@bp.route('/api/competitors', methods=['POST'])
@_require_auth
def api_create_competitor():
    payload = request.get_json(silent=True) or {}
    name = (payload.get('name') or '').strip()
    if not name:
        return jsonify(error='name required'), 400

    # Dedup by lower-cased name
    existing = CompetitorMaster.query.filter(
        func.lower(CompetitorMaster.name) == name.lower()).first()
    if existing:
        return jsonify(item=existing.to_dict(), created=False), 200

    row = CompetitorMaster(
        name=name,
        aliases=_list_field(payload.get('aliases')),
        website=(payload.get('website') or '').strip() or None,
        country=(payload.get('country') or '').strip() or None,
        city=(payload.get('city') or '').strip() or None,
        hq_address=(payload.get('hq_address') or '').strip() or None,
        services=_list_field(payload.get('services')),
        verticals=_list_field(payload.get('verticals')),
        capabilities=(payload.get('capabilities') or '').strip() or None,
        geographic_reach=_list_field(payload.get('geographic_reach')),
        equipment_notes=(payload.get('equipment_notes') or '').strip() or None,
        key_customers_public=_list_field(payload.get('key_customers_public')),
        strengths=(payload.get('strengths') or '').strip() or None,
        weaknesses=(payload.get('weaknesses') or '').strip() or None,
        strategic_notes=(payload.get('strategic_notes') or '').strip() or None,
        linkedin_url=(payload.get('linkedin_url') or '').strip() or None,
        is_active=True,
        created_by_id=_current_emp_code(),
    )
    db.session.add(row)
    db.session.commit()
    return jsonify(item=row.to_dict(), created=True), 201


@bp.route('/api/competitors/<int:cid>', methods=['GET'])
@_require_auth
def api_get_competitor(cid):
    row = CompetitorMaster.query.get_or_404(cid)
    return jsonify(item=row.to_dict(deep=True))


@bp.route('/api/competitors/<int:cid>', methods=['PATCH'])
@_require_auth
def api_patch_competitor(cid):
    row = CompetitorMaster.query.get_or_404(cid)
    payload = request.get_json(silent=True) or {}

    _scalar = {'name', 'website', 'country', 'city', 'hq_address',
               'capabilities', 'equipment_notes', 'strengths',
               'weaknesses', 'strategic_notes', 'linkedin_url'}
    _list = {'aliases', 'services', 'verticals', 'geographic_reach',
             'key_customers_public'}
    for k, v in payload.items():
        if k in _scalar:
            setattr(row, k, (v or '').strip() if isinstance(v, str) else v)
        elif k in _list:
            setattr(row, k, _list_field(v))
        elif k == 'is_active':
            row.is_active = bool(v)
    db.session.commit()
    return jsonify(item=row.to_dict())


# ═════════════════════════════════════════════════════════════════════════
# Performance stats (spec §38)
# ═════════════════════════════════════════════════════════════════════════
@bp.route('/api/competitors/<int:cid>/performance', methods=['GET'])
@_require_auth
def api_competitor_performance(cid):
    CompetitorMaster.query.get_or_404(cid)

    junctions = OpportunityCompetitor.query.filter_by(
        competitor_id=cid, is_active=True).all()

    encounters = len(junctions)
    won_by_us = 0
    lost_to_them = 0
    price_deltas = []
    accounts, industries, verticals, services, geographies = (
        set(), set(), set(), set(), set())

    try:
        from app import Opportunity, Company, Lead
    except Exception:
        Opportunity = Company = Lead = None

    for j in junctions:
        # win/loss inference — 'Lost To' means we lost, else look at
        # the parent opportunity/quote outcome
        if (j.status or '') == 'Lost To':
            lost_to_them += 1

        opp = None
        if Opportunity and j.opportunity_id:
            opp = Opportunity.query.get(j.opportunity_id)
        if opp and (opp.stage or '').lower() in ('won',) and \
                (j.status or '') != 'Lost To':
            won_by_us += 1

        # Price delta = winning_price - our_quoted (if both known)
        try:
            if j.winning_price and opp and opp.value_inr:
                price_deltas.append(
                    float(j.winning_price) - float(opp.value_inr))
        except Exception:
            pass

        # Account / industry / vertical / service / geography attribution
        if Company and opp and opp.company_id:
            co = Company.query.get(opp.company_id)
            if co:
                accounts.add(getattr(co, 'name', None) or f'#{co.id}')
                industries.add(getattr(co, 'industry', None) or '')
        if Lead and j.lead_id:
            ld = Lead.query.get(j.lead_id)
            if ld:
                if ld.company:
                    accounts.add(ld.company)
                if ld.industry:
                    industries.add(ld.industry)
                if ld.procam_vertical:
                    verticals.add(ld.procam_vertical)
                if ld.country:
                    geographies.add(ld.country)
                if ld.city:
                    geographies.add(ld.city)

    win_pct = round(100.0 * won_by_us / encounters, 1) if encounters else 0.0
    loss_pct = round(100.0 * lost_to_them / encounters, 1) if encounters else 0.0
    avg_delta = round(sum(price_deltas) / len(price_deltas), 2) \
        if price_deltas else None

    return jsonify(
        competitor_id=cid,
        encounters=encounters,
        won_by_us=won_by_us,
        lost_to_them=lost_to_them,
        win_pct=win_pct,
        loss_pct=loss_pct,
        avg_price_delta=avg_delta,
        accounts=sorted(x for x in accounts if x),
        industries=sorted(x for x in industries if x),
        verticals=sorted(x for x in verticals if x),
        services=sorted(x for x in services if x),
        geographies=sorted(x for x in geographies if x),
    )


# ═════════════════════════════════════════════════════════════════════════
# OpportunityCompetitor junction
# ═════════════════════════════════════════════════════════════════════════
@bp.route('/api/opportunity-competitors', methods=['GET'])
@_require_auth
def api_list_junction():
    q = OpportunityCompetitor.query.filter_by(is_active=True)
    for key in ('opportunity_id', 'lead_id', 'rfq_id', 'quote_id',
                'competitor_id'):
        v = request.args.get(key)
        if v:
            try:
                q = q.filter(getattr(OpportunityCompetitor, key) == int(v))
            except (TypeError, ValueError):
                pass
    rows = q.order_by(OpportunityCompetitor.added_at.desc()).limit(500).all()
    return jsonify(items=[r.to_dict() for r in rows])


@bp.route('/api/opportunity-competitors', methods=['POST'])
@_require_auth
def api_create_junction():
    payload = request.get_json(silent=True) or {}
    cid = payload.get('competitor_id')
    if not cid:
        return jsonify(error='competitor_id required'), 400
    if not any(payload.get(k) for k in
               ('opportunity_id', 'lead_id', 'rfq_id', 'quote_id')):
        return jsonify(error='need one of opportunity_id/lead_id/rfq_id/quote_id'), 400

    row = OpportunityCompetitor(
        competitor_id=int(cid),
        opportunity_id=_int(payload.get('opportunity_id')),
        lead_id=_int(payload.get('lead_id')),
        rfq_id=_int(payload.get('rfq_id')),
        quote_id=_int(payload.get('quote_id')),
        status=(payload.get('status') or 'Possible'),
        quoted_price=_num(payload.get('quoted_price')),
        winning_price=_num(payload.get('winning_price')),
        currency=(payload.get('currency') or 'INR'),
        price_source=(payload.get('price_source') or '').strip() or None,
        strengths_here=(payload.get('strengths_here') or '').strip() or None,
        weaknesses_here=(payload.get('weaknesses_here') or '').strip() or None,
        notes=(payload.get('notes') or '').strip() or None,
        added_by_id=_current_emp_code(),
    )
    db.session.add(row)
    db.session.commit()
    return jsonify(item=row.to_dict()), 201


@bp.route('/api/opportunity-competitors/<int:jid>', methods=['PATCH'])
@_require_auth
def api_patch_junction(jid):
    row = OpportunityCompetitor.query.get_or_404(jid)
    payload = request.get_json(silent=True) or {}
    old_status = row.status
    for k in ('status', 'currency', 'price_source', 'strengths_here',
              'weaknesses_here', 'notes'):
        if k in payload:
            v = payload.get(k)
            setattr(row, k,
                    (v or '').strip() if isinstance(v, str) else v)
    for k in ('quoted_price', 'winning_price'):
        if k in payload:
            setattr(row, k, _num(payload.get(k)))
    if 'is_active' in payload:
        row.is_active = bool(payload['is_active'])
    db.session.commit()

    # Task-engine hook — status change on junction
    if payload.get('status') and payload.get('status') != old_status:
        try:
            on_state_change(row, 'OpportunityCompetitor',
                            old_status, row.status)
        except Exception:
            current_app.logger.exception(
                'OpportunityCompetitor state-change hook failed')
    return jsonify(item=row.to_dict())


@bp.route('/api/opportunity-competitors/<int:jid>', methods=['DELETE'])
@_require_auth
def api_delete_junction(jid):
    row = OpportunityCompetitor.query.get_or_404(jid)
    row.is_active = False
    db.session.commit()
    return jsonify(ok=True)


# ═════════════════════════════════════════════════════════════════════════
# Contacts
# ═════════════════════════════════════════════════════════════════════════
@bp.route('/api/competitors/<int:cid>/contacts', methods=['POST'])
@_require_auth
def api_add_contact(cid):
    CompetitorMaster.query.get_or_404(cid)
    payload = request.get_json(silent=True) or {}
    name = (payload.get('name') or '').strip()
    if not name:
        return jsonify(error='name required'), 400

    linkedin = (payload.get('linkedin_url') or '').strip()
    if linkedin and not (linkedin.startswith('http://') or
                         linkedin.startswith('https://')):
        return jsonify(error='linkedin_url must start with http(s)://'), 400

    row = CompetitorContact(
        competitor_id=cid,
        name=name,
        designation=(payload.get('designation') or '').strip() or None,
        department=(payload.get('department') or '').strip() or None,
        country=(payload.get('country') or '').strip() or None,
        city=(payload.get('city') or '').strip() or None,
        linkedin_url=linkedin or None,
        public_email=(payload.get('public_email') or '').strip() or None,
        role_category=(payload.get('role_category') or '').strip() or None,
        source=(payload.get('source') or '').strip() or None,
        remarks=(payload.get('remarks') or '').strip() or None,
        last_updated_by_id=_current_emp_code(),
    )
    db.session.add(row)
    db.session.commit()
    return jsonify(item=row.to_dict()), 201


@bp.route('/api/competitor-contacts/<int:cid>', methods=['PATCH'])
@_require_auth
def api_patch_contact(cid):
    row = CompetitorContact.query.get_or_404(cid)
    payload = request.get_json(silent=True) or {}
    for k in ('name', 'designation', 'department', 'country', 'city',
              'linkedin_url', 'public_email', 'role_category', 'source',
              'remarks'):
        if k in payload:
            v = payload.get(k)
            setattr(row, k, (v or '').strip() if isinstance(v, str) else v)
    if row.linkedin_url and not (row.linkedin_url.startswith('http://') or
                                 row.linkedin_url.startswith('https://')):
        return jsonify(error='linkedin_url must start with http(s)://'), 400
    row.last_updated_by_id = _current_emp_code()
    db.session.commit()
    return jsonify(item=row.to_dict())


@bp.route('/api/competitor-contacts/<int:cid>', methods=['DELETE'])
@_require_auth
def api_delete_contact(cid):
    row = CompetitorContact.query.get_or_404(cid)
    db.session.delete(row)
    db.session.commit()
    return jsonify(ok=True)


# ═════════════════════════════════════════════════════════════════════════
# Intelligence
# ═════════════════════════════════════════════════════════════════════════
@bp.route('/api/competitors/<int:cid>/intelligence', methods=['GET'])
@_require_auth
def api_list_intel(cid):
    CompetitorMaster.query.get_or_404(cid)
    q = CompetitorIntelligence.query.filter_by(competitor_id=cid)

    typ = (request.args.get('type') or '').strip()
    if typ:
        q = q.filter(CompetitorIntelligence.event_type == typ)
    from_d = _parse_date(request.args.get('from_date'))
    to_d = _parse_date(request.args.get('to_date'))
    if from_d:
        q = q.filter(CompetitorIntelligence.event_date >= from_d)
    if to_d:
        q = q.filter(CompetitorIntelligence.event_date <= to_d)

    rows = q.order_by(CompetitorIntelligence.event_date.desc(),
                      CompetitorIntelligence.added_at.desc()).limit(500).all()
    return jsonify(items=[r.to_dict() for r in rows])


@bp.route('/api/competitors/<int:cid>/intelligence', methods=['POST'])
@_require_auth
def api_add_intel(cid):
    CompetitorMaster.query.get_or_404(cid)
    payload = request.get_json(silent=True) or {}
    event_type = (payload.get('event_type') or '').strip()
    summary = (payload.get('summary') or '').strip()
    if not event_type or not summary:
        return jsonify(error='event_type and summary required'), 400

    row = CompetitorIntelligence(
        competitor_id=cid,
        event_type=event_type,
        event_date=_parse_date(payload.get('event_date')) or date.today(),
        summary=summary,
        source=(payload.get('source') or '').strip() or None,
        source_url=(payload.get('source_url') or '').strip() or None,
        attachment_path=(payload.get('attachment_path') or '').strip() or None,
        related_project_id=_int(payload.get('related_project_id')),
        related_account_id=_int(payload.get('related_account_id')),
        related_opportunity_id=_int(payload.get('related_opportunity_id')),
        added_by_id=_current_emp_code(),
    )
    db.session.add(row)
    db.session.commit()
    return jsonify(item=row.to_dict()), 201


# ═════════════════════════════════════════════════════════════════════════
# Assessments
# ═════════════════════════════════════════════════════════════════════════
_ASSESSMENT_AXES = (
    'pricing', 'fleet_assets', 'engineering', 'heavy_transport',
    'project_logistics', 'freight_forwarding', 'warehousing',
    'global_network', 'local_presence', 'customer_relationships',
    'response_speed',
)


@bp.route('/api/competitors/<int:cid>/assessments', methods=['POST'])
@_require_auth
def api_add_assessment(cid):
    CompetitorMaster.query.get_or_404(cid)
    payload = request.get_json(silent=True) or {}
    kwargs = {'competitor_id': cid,
              'assessment_date': (_parse_date(payload.get('assessment_date'))
                                  or date.today()),
              'notes': (payload.get('notes') or '').strip() or None,
              'assessed_by_id': _current_emp_code()}
    for axis in _ASSESSMENT_AXES:
        v = _int(payload.get(axis))
        if v is not None and (v < 1 or v > 5):
            return jsonify(error=f'{axis} must be 1..5'), 400
        kwargs[axis] = v
    row = CompetitorAssessment(**kwargs)
    db.session.add(row)
    db.session.commit()
    return jsonify(item=row.to_dict()), 201


@bp.route('/api/competitors/<int:cid>/assessments', methods=['GET'])
@_require_auth
def api_list_assessments(cid):
    CompetitorMaster.query.get_or_404(cid)
    rows = (CompetitorAssessment.query
            .filter_by(competitor_id=cid)
            .order_by(CompetitorAssessment.assessment_date.desc()).all())
    return jsonify(items=[r.to_dict() for r in rows])


# ═════════════════════════════════════════════════════════════════════════
# Public sources (Phase 10 — Review UI only, no polling worker)
# ═════════════════════════════════════════════════════════════════════════
@bp.route('/api/public-sources', methods=['GET'])
@_require_auth
def api_list_public_sources():
    q = PublicSource.query
    cid = _int(request.args.get('competitor_id'))
    if cid:
        q = q.filter_by(competitor_id=cid)
    rows = q.order_by(PublicSource.created_at.desc()).limit(500).all()
    return jsonify(items=[r.to_dict() for r in rows])


@bp.route('/api/public-sources', methods=['POST'])
@_require_auth
def api_create_public_source():
    payload = request.get_json(silent=True) or {}
    name = (payload.get('name') or '').strip()
    url = (payload.get('url') or '').strip()
    if not name or not url:
        return jsonify(error='name and url required'), 400
    row = PublicSource(
        name=name,
        kind=(payload.get('kind') or '').strip() or None,
        url=url,
        competitor_id=_int(payload.get('competitor_id')),
        poll_interval_hours=(_int(payload.get('poll_interval_hours')) or 24),
        is_active=True,
    )
    db.session.add(row)
    db.session.commit()
    return jsonify(item=row.to_dict()), 201


@bp.route('/api/public-source-items', methods=['GET'])
@_require_auth
def api_list_public_source_items():
    q = PublicSourceItem.query
    src_id = _int(request.args.get('source_id'))
    if src_id:
        q = q.filter_by(source_id=src_id)
    cid = _int(request.args.get('competitor_id'))
    if cid:
        # Items belong to sources tied to a competitor
        src_ids = [r.id for r in
                   PublicSource.query.filter_by(competitor_id=cid).all()]
        if not src_ids:
            return jsonify(items=[])
        q = q.filter(PublicSourceItem.source_id.in_(src_ids))
    only_pending = request.args.get('only_pending')
    if only_pending in ('1', 'true', 'yes'):
        q = q.filter(PublicSourceItem.reviewed_at.is_(None),
                     PublicSourceItem.dismissed.is_(False))
    rows = q.order_by(PublicSourceItem.captured_at.desc()).limit(500).all()
    return jsonify(items=[r.to_dict() for r in rows])


@bp.route('/api/public-source-items', methods=['POST'])
@_require_auth
def api_create_public_source_item():
    payload = request.get_json(silent=True) or {}
    src_id = _int(payload.get('source_id'))
    if not src_id:
        return jsonify(error='source_id required'), 400
    PublicSource.query.get_or_404(src_id)
    row = PublicSourceItem(
        source_id=src_id,
        publication_date=_parse_date(payload.get('publication_date')),
        url=(payload.get('url') or '').strip() or None,
        title=(payload.get('title') or '').strip() or None,
        summary=(payload.get('summary') or '').strip() or None,
    )
    db.session.add(row)
    db.session.commit()
    return jsonify(item=row.to_dict()), 201


@bp.route('/api/public-source-items/<int:iid>/promote', methods=['POST'])
@_require_auth
def api_promote_public_source_item(iid):
    item = PublicSourceItem.query.get_or_404(iid)
    payload = request.get_json(silent=True) or {}

    src = PublicSource.query.get(item.source_id)
    cid = _int(payload.get('competitor_id')) or (src.competitor_id if src else None)
    if not cid:
        return jsonify(error='competitor_id required (source has no competitor_id)'), 400
    event_type = (payload.get('event_type') or 'Public News').strip()
    summary = (payload.get('summary') or item.summary or item.title or '').strip()
    if not summary:
        return jsonify(error='summary required (item has none — supply one)'), 400

    intel = CompetitorIntelligence(
        competitor_id=cid,
        event_type=event_type,
        event_date=item.publication_date or date.today(),
        summary=summary,
        source=(src.name if src else None),
        source_url=item.url,
        added_by_id=_current_emp_code(),
    )
    db.session.add(intel)
    db.session.flush()

    item.reviewed_at = datetime.utcnow()
    item.reviewed_by_id = _current_emp_code()
    item.promoted_to_intelligence_id = intel.id
    db.session.commit()
    return jsonify(item=item.to_dict(), intelligence=intel.to_dict()), 201


@bp.route('/api/public-source-items/<int:iid>/dismiss', methods=['POST'])
@_require_auth
def api_dismiss_public_source_item(iid):
    item = PublicSourceItem.query.get_or_404(iid)
    item.dismissed = True
    item.reviewed_at = datetime.utcnow()
    item.reviewed_by_id = _current_emp_code()
    db.session.commit()
    return jsonify(item=item.to_dict())
