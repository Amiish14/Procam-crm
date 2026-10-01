"""
External intelligence — the pages and the JSON API.

    /intelligence/projects            project intelligence, newest first
    /intelligence/projects/<id>       one project and its timeline
    /intelligence/competitors         competitor activity by axis
    /intelligence/vendors             vendors, vessels, India port calls
    /admin/intelligence/sources       which sources exist and what each
                                      one still needs  (admin.master)

Every route is gated. `@_signed_in` is the gate for the working screens;
what a person then sees is decided per record by `app.access.scope`,
through `app.intel.projects.visible()`. The source-configuration screens
need `admin.master`, the same permission that guards every other
vocabulary and registry in the CRM.

The Sources page is deliberately not admin-only to *read*: a person
looking at an empty project list is entitled to know that the feed which
would have filled it has no subscription, rather than concluding there
are no projects.
"""
from __future__ import annotations

from datetime import datetime
from functools import wraps

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)

from app import db
from app.access import scope as sc_mod
from app.access.service import require
from app.intel import adapters as ad
from app.intel import competitors as comp
from app.intel import projects as proj
from app.intel import vendors as ven
# Imported for its side effect: registering the Release 5 tables on the
# metadata, so db.create_all() builds them on a fresh database. On an
# existing one, scripts/2026_10_09_intelligence.py is what creates them.
from app.models import intel as _intel_models        # noqa: F401
from app.services import audit

bp = Blueprint('intel', __name__)

#: The permission that guards the source-configuration screens — the
#: same one Master Data, Data Quality and Data Mapping already use.
PERM = 'admin.master'


def _signed_in(f):
    """A session is required; the records are then filtered by scope."""
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            if request.path.startswith('/api/'):
                return jsonify(ok=False, error='Not authenticated'), 401
            return redirect(url_for('login'))
        return f(*a, **kw)
    return wrap


def _actor():
    return session.get('emp_code')


def _body():
    return request.get_json(silent=True) or {}


def _refused(exc):
    return jsonify(ok=False, error=exc.message), exc.status


def _project_or_refused(pid):
    """(project, error response). Scope is checked here, once."""
    from presales.models_projects import Project

    project = db.session.get(Project, int(pid))
    if project is None:
        return None, (jsonify(ok=False, error='No such project'), 404)
    if not proj.may_view(project):
        return None, (jsonify(ok=False,
                              error='That project is outside your access'),
                      403)
    return project, None


def _date(value):
    if not value:
        return None
    for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y'):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            continue
    return None


# ═════════════════════════════════════════════════════════════════════
# Sources — the honest answer to "what is actually running?"
# ═════════════════════════════════════════════════════════════════════
@bp.route('/api/intel/sources')
@_signed_in
def api_sources():
    from app.models.intel import IntelSourceConfig, IntelSourceRun
    from app.models.public_source import PublicSource

    configs = []
    for cfg in IntelSourceConfig.query.order_by(
            IntelSourceConfig.id.desc()).limit(200).all():
        source = db.session.get(PublicSource, cfg.source_id)
        adapter = ad.get(cfg.adapter_key, config=cfg.config, source=source)
        row = cfg.to_dict()
        row['source_name'] = source.name if source else '(source removed)'
        row['source_url'] = source.url if source else ''
        if adapter is None:
            row['status'] = 'UNRECOGNISED ADAPTER'
            row['available'] = False
            row['reason'] = (f'No adapter named "{cfg.adapter_key}" is '
                             f'installed; this source cannot run.')
        else:
            ok, reason = adapter.available()
            row['status'] = adapter.status()
            row['available'] = ok
            row['reason'] = reason
            row['dependency'] = adapter.dependency
        configs.append(row)

    runs = [r.to_dict() for r in IntelSourceRun.query.order_by(
        IntelSourceRun.id.desc()).limit(50).all()]
    return jsonify(ok=True, adapters=ad.status_report(), sources=configs,
                   runs=runs)


@bp.route('/admin/intelligence/sources')
@require('admin.master')
def sources_page():
    return render_template('intel/sources.html', report=ad.status_report(),
                           adapter_keys=ad.adapter_keys())


@bp.route('/api/intel/sources', methods=['POST'])
@require('admin.master')
def api_source_save():
    """Wire an adapter to a public source. Secrets are named, not stored."""
    from app.models.intel import IntelSourceConfig
    from app.models.public_source import PublicSource

    body = _body()
    adapter_key = (body.get('adapter_key') or '').strip()
    if ad.get(adapter_key) is None:
        return jsonify(ok=False,
                       error=f'No adapter named "{adapter_key}".'), 400

    source_id = body.get('source_id')
    if source_id:
        source = db.session.get(PublicSource, int(source_id))
        if source is None:
            return jsonify(ok=False, error='No such public source.'), 404
    else:
        name = (body.get('name') or '').strip()
        url = (body.get('url') or '').strip()
        if not name:
            return jsonify(ok=False, error='Name the source.'), 400
        source = PublicSource(name=name[:120], url=url[:500] or '(none)',
                              kind=(body.get('kind') or 'news')[:40],
                              is_active=True)
        db.session.add(source)
        db.session.flush()

    config = dict(body.get('config') or {})
    # A key pasted into the config is a key in the database. Refuse it
    # and say where it belongs instead.
    for key in list(config):
        if key in ('api_key', 'token', 'password', 'secret'):
            return jsonify(ok=False, error=(
                f'Do not store "{key}" here. Put the value in an '
                f'environment variable and name it as "{key}_env".')), 400

    cfg = IntelSourceConfig.query.filter_by(
        source_id=source.id, adapter_key=adapter_key).first()
    creating = cfg is None
    if creating:
        cfg = IntelSourceConfig(source_id=source.id,
                                adapter_key=adapter_key,
                                created_by=_actor())
        db.session.add(cfg)
    cfg.purpose = (body.get('purpose') or 'project')[:20]
    cfg.config = config
    cfg.is_enabled = bool(body.get('is_enabled'))
    cfg.notes = (body.get('notes') or '').strip() or None
    db.session.flush()
    audit.record('intel.source.save', 'intel_source_config', cfg.id,
                 new={'adapter': adapter_key, 'source_id': source.id,
                      'enabled': cfg.is_enabled,
                      'config_keys': sorted(config)}, actor=_actor())
    db.session.commit()

    adapter = ad.get(adapter_key, config=cfg.config, source=source)
    ok, reason = adapter.available()
    return jsonify(ok=True, source=cfg.to_dict(), status=adapter.status(),
                   available=ok, reason=reason)


@bp.route('/api/intel/sources/<int:cid>/run', methods=['POST'])
@require('admin.master')
def api_source_run(cid):
    """Run one source now. An unavailable source records why and stops."""
    from app.models.intel import IntelSourceConfig
    from app.models.public_source import PublicSource

    cfg = db.session.get(IntelSourceConfig, cid)
    if cfg is None:
        return jsonify(ok=False, error='No such source.'), 404
    source = db.session.get(PublicSource, cfg.source_id)
    adapter = ad.get(cfg.adapter_key, config=cfg.config, source=source)
    if adapter is None:
        return jsonify(ok=False,
                       error=f'No adapter named "{cfg.adapter_key}".'), 400
    run = proj.run_source(cfg, adapter, since=_date(_body().get('since')),
                          actor=_actor())
    return jsonify(ok=True, run=run.to_dict())


# ═════════════════════════════════════════════════════════════════════
# Project intelligence
# ═════════════════════════════════════════════════════════════════════
@bp.route('/intelligence/projects')
@_signed_in
def projects_page():
    return render_template('intel/projects.html',
                           stages=[{'code': c, 'label': l}
                                   for c, l, _p, _k in proj.INTEL_STAGES],
                           confidences=ven.confidences(),
                           report=ad.status_report())


@bp.route('/intelligence/projects/<int:pid>')
@_signed_in
def project_page(pid):
    project, refused = _project_or_refused(pid)
    if refused:
        from flask import abort
        abort(403 if refused[1] == 403 else 404)
    fact = proj.fact_for(project.id)
    return render_template(
        'intel/project_detail.html', project=project,
        fact=fact, intel=(fact.to_dict() if fact else {}),
        timeline=proj.timeline(project.id),
        stage_label=proj.STAGE_LABELS.get(
            fact.intel_stage if fact else '', ''),
        stages=[{'code': c, 'label': l} for c, l, _p, _k in proj.INTEL_STAGES],
        followers=proj.followers(project.id))


@bp.route('/api/intel/projects')
@_signed_in
def api_projects():
    data = proj.listing(
        sc_mod.current(),
        q=(request.args.get('q') or '').strip(),
        stage=(request.args.get('stage') or '').strip(),
        vertical=(request.args.get('vertical') or '').strip(),
        industry=(request.args.get('industry') or '').strip(),
        state=(request.args.get('state') or '').strip(),
        owner=(request.args.get('owner') or '').strip(),
        confidence=(request.args.get('confidence') or '').strip(),
        page=request.args.get('page', 1),
        per_page=request.args.get('per_page', 50))
    data['ok'] = True
    data['viewer'] = _actor()
    return jsonify(data)


@bp.route('/api/intel/projects/<int:pid>')
@_signed_in
def api_project(pid):
    project, refused = _project_or_refused(pid)
    if refused:
        return refused
    fact = proj.fact_for(project.id)
    return jsonify(ok=True, project=project.to_dict(),
                   intel=(fact.to_dict() if fact else {}),
                   timeline=proj.timeline(project.id),
                   followers=proj.followers(project.id))


@bp.route('/api/intel/projects/<int:pid>/timeline')
@_signed_in
def api_timeline(pid):
    project, refused = _project_or_refused(pid)
    if refused:
        return refused
    return jsonify(ok=True, timeline=proj.timeline(project.id))


@bp.route('/api/intel/capture', methods=['POST'])
@_signed_in
def api_capture():
    """A person records what they found, with the link they found it at.

    This is the manual adapter, and it goes through exactly the same
    ingest as a feed — so a pasted article about a project the CRM
    already holds appends to that timeline rather than starting a second
    project.
    """
    body = _body()
    title = (body.get('title') or '').strip()
    if not title:
        return jsonify(ok=False, error='Give the item a headline.'), 400
    url_ref = (body.get('url') or '').strip()
    confidence = (body.get('confidence') or 'reported').strip()
    if confidence != 'unverified' and not url_ref:
        return jsonify(ok=False, error=(
            'Give the source link, or record this as unverified.')), 400

    item = ad.ManualAdapter.item(
        title=title, body=body.get('body') or '', url=url_ref,
        publication_date=_date(body.get('publication_date')),
        payload=dict(body.get('payload') or {}), confidence=confidence,
        source_label=(body.get('source_label') or 'Manual entry'))
    out = proj.ingest(item, actor=_actor())
    if out['action'] == 'rejected':
        return jsonify(ok=False, error=out['reason']), 400
    return jsonify(ok=True, **out)


@bp.route('/api/intel/projects/<int:pid>/assign', methods=['POST'])
@_signed_in
def api_assign(pid):
    project, refused = _project_or_refused(pid)
    if refused:
        return refused
    body = _body()
    try:
        code = proj.assign_owner(project, body.get('emp_code'),
                                 actor=_actor(),
                                 reason=(body.get('reason') or '').strip())
    except proj.IntelRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, bd_owner=code)


@bp.route('/api/intel/projects/<int:pid>/follow', methods=['POST'])
@_signed_in
def api_follow(pid):
    project, refused = _project_or_refused(pid)
    if refused:
        return refused
    on = _body().get('follow', True)
    try:
        state = proj.follow(project, _actor(), actor=_actor(), on=bool(on))
    except proj.IntelRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, following=state)


@bp.route('/api/intel/projects/<int:pid>/create-lead', methods=['POST'])
@_signed_in
def api_create_lead(pid):
    project, refused = _project_or_refused(pid)
    if refused:
        return refused
    body = _body()
    try:
        lead = proj.create_lead(project, actor=_actor(),
                                owner=body.get('owner'),
                                note=(body.get('note') or '').strip())
    except proj.IntelRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, lead_id=lead.id, company=lead.company,
                   assigned_to=lead.assigned_to or '')


@bp.route('/api/intel/projects/<int:pid>/accounts', methods=['POST'])
@_signed_in
def api_add_account(pid):
    project, refused = _project_or_refused(pid)
    if refused:
        return refused
    body = _body()
    try:
        link = proj.add_account(project, body.get('account_id'),
                                role=(body.get('role') or 'Other'),
                                actor=_actor(),
                                note=(body.get('note') or '').strip())
    except proj.IntelRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, account_id=link.account_id, role=link.role)


@bp.route('/api/intel/projects/<int:pid>/contacts', methods=['POST'])
@_signed_in
def api_add_contact(pid):
    project, refused = _project_or_refused(pid)
    if refused:
        return refused
    body = _body()
    try:
        link = proj.add_contact(project, body.get('contact_id'),
                                role_on_project=(body.get('role') or ''),
                                actor=_actor())
    except proj.IntelRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, contact_id=link.contact_id)


@bp.route('/api/intel/projects/<int:pid>/not-relevant', methods=['POST'])
@_signed_in
def api_not_relevant(pid):
    project, refused = _project_or_refused(pid)
    if refused:
        return refused
    try:
        fact = proj.mark_not_relevant(project, _body().get('reason'),
                                      actor=_actor())
    except proj.IntelRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, reason=fact.not_relevant_reason)


# ═════════════════════════════════════════════════════════════════════
# Competitor intelligence
# ═════════════════════════════════════════════════════════════════════
@bp.route('/intelligence/competitors')
@_signed_in
def competitors_page():
    return render_template('intel/competitors.html',
                           vocab=comp.vocabularies())


@bp.route('/api/intel/competitor-activity')
@_signed_in
def api_competitor_activity():
    data = comp.search(
        competitor=(request.args.get('competitor') or '').strip(),
        vertical=(request.args.get('vertical') or '').strip(),
        service=(request.args.get('service') or '').strip(),
        industry=(request.args.get('industry') or '').strip(),
        geography=(request.args.get('geography') or '').strip(),
        activity_type=(request.args.get('activity_type') or '').strip(),
        confidence=(request.args.get('confidence') or '').strip(),
        since=_date(request.args.get('since')),
        account_id=request.args.get('account_id'),
        project_id=request.args.get('project_id'),
        page=request.args.get('page', 1),
        per_page=request.args.get('per_page', 50))
    data['ok'] = True
    return jsonify(data)


@bp.route('/api/intel/competitor-activity', methods=['POST'])
@_signed_in
def api_competitor_capture():
    body = _body()
    try:
        row = comp.capture(
            competitor_name=body.get('competitor_name'),
            summary=body.get('summary'), actor=_actor(),
            company_id=body.get('company_id'),
            vertical=body.get('vertical', ''),
            service=body.get('service', ''),
            industry=body.get('industry', ''),
            geography=body.get('geography', ''),
            activity_type=body.get('activity_type', ''),
            event_date=_date(body.get('event_date')),
            confidence=body.get('confidence', 'reported'),
            source=body.get('source', ''),
            source_url=body.get('source_url', ''),
            publication_date=_date(body.get('publication_date')),
            related_account_id=body.get('related_account_id'),
            related_opportunity_id=body.get('related_opportunity_id'),
            related_project_id=body.get('related_project_id'))
    except comp.CompetitorIntelRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, activity=row.to_dict())


@bp.route('/api/intel/competitor-activity/<int:rid>/verify',
          methods=['POST'])
@_signed_in
def api_competitor_verify(rid):
    body = _body()
    try:
        row = comp.verify(rid, actor=_actor(),
                          confidence=body.get('confidence'),
                          note=(body.get('note') or '').strip())
    except comp.CompetitorIntelRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, activity=row.to_dict())


@bp.route('/api/intel/competitor-activity/summary')
@_signed_in
def api_competitor_summary():
    axis = (request.args.get('axis') or 'vertical').strip()
    try:
        rows = comp.by_axis(axis, since=_date(request.args.get('since')))
    except comp.CompetitorIntelRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, axis=axis, rows=rows)


# ═════════════════════════════════════════════════════════════════════
# Vendor, vessel and port-call intelligence
# ═════════════════════════════════════════════════════════════════════
@bp.route('/intelligence/vendors')
@_signed_in
def vendors_page():
    return render_template('intel/vendors.html', vocab=ven.vocabularies(),
                           coverage=ven.coverage(),
                           report=[r for r in ad.status_report()
                                   if 'vendor' in r['purposes']
                                   or 'port_call' in r['purposes']])


@bp.route('/api/intel/vocab')
@_signed_in
def api_vocab():
    """Every vocabulary these screens use, live from Master Data."""
    vocab = ven.vocabularies()
    vocab.update(comp.vocabularies())
    vocab['intel_stage'] = [{'code': c, 'label': l}
                            for c, l, _p, _k in proj.INTEL_STAGES]
    return jsonify(ok=True, vocab=vocab)


@bp.route('/api/intel/vendors')
@_signed_in
def api_vendors():
    data = ven.search_vendors(
        q=(request.args.get('q') or '').strip(),
        category=(request.args.get('category') or '').strip(),
        port=(request.args.get('port') or '').strip(),
        country=(request.args.get('country') or '').strip(),
        service=(request.args.get('service') or '').strip(),
        page=request.args.get('page', 1),
        per_page=request.args.get('per_page', 50))
    data['ok'] = True
    return jsonify(data)


@bp.route('/api/intel/vendors', methods=['POST'])
@_signed_in
def api_vendor_save():
    body = _body()
    try:
        row = ven.save_vendor(name=body.get('name'),
                              category=body.get('category'),
                              actor=_actor(), vendor_id=body.get('id'),
                              **{k: v for k, v in body.items()
                                 if k not in ('name', 'category', 'id')})
    except ven.VendorIntelRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, vendor=row.to_dict())


@bp.route('/api/intel/vessels')
@_signed_in
def api_vessels():
    def _flag(name):
        value = request.args.get(name)
        return None if value in (None, '') else value not in ('0', 'false')

    data = ven.search_vessels(
        q=(request.args.get('q') or '').strip(),
        vessel_type=(request.args.get('vessel_type') or '').strip(),
        operator=(request.args.get('operator') or '').strip(),
        min_lift=request.args.get('min_lift') or None,
        self_geared=_flag('self_geared'), roro=_flag('roro'),
        heavy_lift=_flag('heavy_lift'), calls_india=_flag('calls_india'),
        page=request.args.get('page', 1),
        per_page=request.args.get('per_page', 50))
    data['ok'] = True
    return jsonify(data)


@bp.route('/api/intel/vessels', methods=['POST'])
@_signed_in
def api_vessel_save():
    body = _body()
    try:
        row = ven.save_vessel(name=body.get('name'), actor=_actor(),
                              vessel_id=body.get('id'),
                              **{k: v for k, v in body.items()
                                 if k not in ('name', 'id')})
    except ven.VendorIntelRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, vessel=row.to_dict())


@bp.route('/api/intel/port-calls')
@_signed_in
def api_port_calls():
    data = ven.port_calls(
        port_code=(request.args.get('port') or '').strip(),
        vessel=(request.args.get('vessel') or '').strip(),
        operator=(request.args.get('operator') or '').strip(),
        days_ahead=request.args.get('days_ahead') or None,
        page=request.args.get('page', 1),
        per_page=request.args.get('per_page', 50))
    data['ok'] = True
    return jsonify(data)


@bp.route('/api/intel/port-calls', methods=['POST'])
@_signed_in
def api_port_call_save():
    body = _body()
    try:
        row = ven.save_port_call(vessel_name=body.get('vessel_name'),
                                 port_code=body.get('port_code'),
                                 actor=_actor(), call_id=body.get('id'),
                                 **{k: v for k, v in body.items()
                                    if k not in ('vessel_name', 'port_code',
                                                 'id')})
    except ven.VendorIntelRefused as exc:
        return _refused(exc)
    return jsonify(ok=True, port_call=row.to_dict())
