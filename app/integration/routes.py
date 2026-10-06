"""
The CRM's integration API — what the TMS calls.

    GET  /api/integration/v1/ping
    GET  /api/integration/v1/accounts?name=&gstin=&domain=
    POST /api/integration/v1/accounts
    GET  /api/integration/v1/leads?crm_lead_id=&tms_project_id=&company=
    POST /api/integration/v1/leads
    POST /api/integration/v1/leads/<crm_lead_id>/link-tms
    GET  /api/integration/v1/leads/<crm_lead_id>

Separate from the browser API on purpose. These are called by another
application with a token, not by a person with a session, so they
share none of the session handling, none of the CSRF, and none of the
data scoping — a server-to-server caller has no employee whose scope
could narrow the answer. What they have instead is a token, an
idempotency key and a log.

Every response carries `request_id`. Every error is the same shape:

    {"ok": false, "error": {"code": "...", "message": "..."},
     "request_id": "..."}

so the TMS can branch on `code` rather than parsing prose that may be
reworded.

Nothing here invents business behaviour. It finds accounts and leads,
creates them when the TMS says to, and records which CRM record is
which TMS record. What a TMS project means is the TMS's business, and
this file does not guess at it.
"""
from __future__ import annotations

import time
from functools import wraps

from flask import Blueprint, g, jsonify, request

from app.services import integration as integ

bp = Blueprint('integration', __name__)

PREFIX = integ.API_PREFIX


def _fail(code, message, status=400, request_id=None):
    return jsonify({'ok': False,
                    'error': {'code': code, 'message': message},
                    'request_id': request_id or getattr(g, 'request_id', '')
                    }), status


def _authenticated(f):
    """A token, or nothing. No session fallback — a browser session
    reaching these routes would mean a person's cookie could act as a
    system credential."""
    @wraps(f)
    def wrap(*a, **kw):
        g.started = time.monotonic()
        g.request_id = (request.headers.get('X-Request-Id')
                        or integ.new_request_id())
        g.idempotency_key = request.headers.get('Idempotency-Key') or ''

        if not integ.configured():
            return _fail('integration_disabled',
                         'No integration tokens are configured on this '
                         'CRM.', 503)
        caller = integ.authenticate(request.headers.get('Authorization'))
        if not caller:
            integ.record(direction='inbound', endpoint=request.path,
                         method=request.method, status='refused',
                         status_code=401, request_id=g.request_id,
                         error='bad or missing token')
            return _fail('unauthorized', 'A valid bearer token is '
                                         'required.', 401)
        g.caller = caller
        return f(*a, **kw)
    return wrap


def _ok(payload, *, crm_type=None, crm_id=None, tms_id=None, status=200):
    body = dict(payload or {})
    body.setdefault('ok', True)
    body['request_id'] = getattr(g, 'request_id', '')
    integ.record(direction='inbound', endpoint=request.path,
                 method=request.method, status='ok', status_code=status,
                 request_id=g.request_id,
                 idempotency_key=getattr(g, 'idempotency_key', ''),
                 crm_object_type=crm_type, crm_object_id=crm_id,
                 tms_object_id=tms_id,
                 request_summary=_safe_body(), response_summary=body,
                 caller=getattr(g, 'caller', ''),
                 started=getattr(g, 'started', None))
    return jsonify(body), status


def _safe_body():
    try:
        return request.get_json(silent=True) or dict(request.args)
    except Exception:
        return None


def _replayed(endpoint):
    """The earlier answer to this exact request, if there was one."""
    key = getattr(g, 'idempotency_key', '')
    if not key:
        return None
    earlier = integ.remembered(key, endpoint)
    if earlier is None:
        return None
    body = dict(earlier)
    body['replayed'] = True
    body['request_id'] = g.request_id
    integ.record(direction='inbound', endpoint=endpoint,
                 method=request.method, status='replayed', status_code=200,
                 request_id=g.request_id, idempotency_key=key,
                 caller=getattr(g, 'caller', ''),
                 response_summary=body, started=getattr(g, 'started', None))
    return jsonify(body), 200


# ── health ───────────────────────────────────────────────────────────
@bp.route(f'{PREFIX}/ping')
@_authenticated
def ping():
    """Proves the token works, before anything is built on it."""
    return _ok({'service': 'procam-crm', 'caller': g.caller})


# ── accounts ─────────────────────────────────────────────────────────
@bp.route(f'{PREFIX}/accounts', methods=['GET'])
@_authenticated
def find_account():
    from app import Company
    from app.services.company_match import build_index, match

    name = (request.args.get('name') or '').strip()
    gstin = (request.args.get('gstin') or '').strip().upper()
    domain = (request.args.get('domain') or '').strip().lower()
    if not (name or gstin or domain):
        return _fail('missing_query', 'Give a name, a gstin or a domain.')

    found = None
    if gstin and hasattr(Company, 'gstin'):
        found = Company.query.filter(Company.gstin == gstin).first()
    if found is None and domain:
        found = Company.query.filter(
            Company.website.ilike(f'%{domain}%')).first()
    if found is None and name:
        index = build_index(Company.query.filter(
            Company.is_active.is_(True)).all())
        found, _why, _cands = match(name, index)

    if found is None:
        return _ok({'found': False, 'account': None})
    return _ok({'found': True, 'account': _account(found)},
               crm_type='Company', crm_id=found.id)


@bp.route(f'{PREFIX}/accounts', methods=['POST'])
@_authenticated
def create_account():
    """Find or create. Never a duplicate for a name we already hold."""
    from app import Company, db
    from app.services.company_match import build_index, match

    replay = _replayed(request.path)
    if replay is not None:
        return replay

    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return _fail('missing_name', 'An account needs a name.')

    index = build_index(Company.query.filter(
        Company.is_active.is_(True)).all())
    found, _why, _cands = match(name, index)
    if found is not None:
        return _ok({'created': False, 'account': _account(found)},
                   crm_type='Company', crm_id=found.id)

    # The block register applies to the TMS as much as to the CRM's
    # own screens: if management has stopped business with a client,
    # a job being raised in another system does not change that.
    from app.services import client_restrictions as restrictions
    verdict = restrictions.check(company_name=name,
                                 email=data.get('email'),
                                 gstin=data.get('gstin'))
    if verdict.blocked:
        restrictions.record_attempt(verdict, what='TMS account create',
                                    user_id=f'tms:{g.caller}')
        return _fail('client_blocked', verdict.message(), 403)

    row = Company(name=name[:200],
                  website=(data.get('website') or '').strip() or None,
                  country=(data.get('country') or '').strip() or None,
                  city=(data.get('city') or '').strip() or None,
                  email=(data.get('email') or '').strip() or None,
                  phone=(data.get('phone') or '').strip() or None,
                  is_active=True,
                  created_by=f'tms:{g.caller}')
    db.session.add(row)
    db.session.commit()
    return _ok({'created': True, 'account': _account(row)},
               crm_type='Company', crm_id=row.id, status=201)


def _account(row):
    return {'crm_account_id': row.id, 'name': row.name,
            'website': row.website or '', 'city': row.city or '',
            'country': row.country or '',
            'pic_emp_code': getattr(row, 'pic_emp_code', '') or ''}


# ── leads ────────────────────────────────────────────────────────────
@bp.route(f'{PREFIX}/leads', methods=['GET'])
@_authenticated
def find_lead():
    from app import Lead
    from app.models.integration import CrmTmsLink

    crm_lead_id = request.args.get('crm_lead_id')
    tms_project_id = (request.args.get('tms_project_id') or '').strip()
    company = (request.args.get('company') or '').strip()

    lead = None
    if crm_lead_id and str(crm_lead_id).isdigit():
        lead = Lead.query.get(int(crm_lead_id))
    elif tms_project_id:
        row = CrmTmsLink.query.filter_by(
            tms_project_id=tms_project_id).first()
        lead = Lead.query.get(row.crm_lead_id) if row else None
    elif company:
        lead = (Lead.query.filter(Lead.company.ilike(f'%{company}%'))
                .order_by(Lead.id.desc()).first())
    else:
        return _fail('missing_query',
                     'Give crm_lead_id, tms_project_id or company.')

    if lead is None:
        return _ok({'found': False, 'lead': None})
    return _ok({'found': True, 'lead': _lead(lead)},
               crm_type='Lead', crm_id=lead.id)


@bp.route(f'{PREFIX}/leads/<int:crm_lead_id>', methods=['GET'])
@_authenticated
def get_lead(crm_lead_id):
    from app import Lead

    lead = Lead.query.get(crm_lead_id)
    if lead is None:
        return _fail('not_found', f'No CRM lead {crm_lead_id}.', 404)
    return _ok({'found': True, 'lead': _lead(lead)},
               crm_type='Lead', crm_id=lead.id)


@bp.route(f'{PREFIX}/leads', methods=['POST'])
@_authenticated
def create_lead():
    """Create a CRM lead the TMS needs to reference.

    For work that reached the TMS first — a customer who called
    operations directly. The lead is an ordinary CRM lead, visible on
    the board and owned by somebody, not a shadow record.
    """
    from app import Lead, db

    replay = _replayed(request.path)
    if replay is not None:
        return replay

    data = request.get_json(silent=True) or {}
    company = (data.get('company') or '').strip()
    if not company:
        return _fail('missing_company', 'A lead needs a company.')

    from app.services import client_restrictions as restrictions
    verdict = restrictions.check(company_name=company,
                                 email=data.get('email'))
    if verdict.blocked:
        restrictions.record_attempt(verdict, what='TMS lead create',
                                    user_id=f'tms:{g.caller}')
        return _fail('client_blocked', verdict.message(), 403)

    from datetime import datetime
    now = datetime.utcnow()
    lead = Lead(company=company[:200],
                source=(data.get('source') or 'tms')[:40],
                stage=(data.get('stage') or 'New'),
                project=(data.get('project') or '')[:200] or None,
                pic=(data.get('contact_name') or '')[:150] or None,
                email=(data.get('email') or '')[:150] or None,
                phone=(data.get('phone') or '')[:50] or None,
                city=(data.get('city') or '')[:100] or None,
                country=(data.get('country') or 'India')[:80],
                assigned_to=(data.get('assigned_to') or '').strip() or None,
                created_at=now, received_at=now,
                onboarded_date=now.date(),
                notes=(data.get('notes') or '')[:4000] or None)
    db.session.add(lead)
    db.session.commit()

    linked = None
    if data.get('tms_project_id') or data.get('tms_job_id'):
        try:
            row, _new = integ.link(
                crm_lead_id=lead.id, tms_project_id=data.get('tms_project_id'),
                tms_job_id=data.get('tms_job_id'),
                link_type=(data.get('link_type') or 'project'),
                created_by=f'tms:{g.caller}')
            linked = row.to_dict()
        except integ.IntegrationError as exc:
            return _fail(exc.code, exc.message, exc.status)

    return _ok({'created': True, 'lead': _lead(lead), 'link': linked},
               crm_type='Lead', crm_id=lead.id,
               tms_id=data.get('tms_project_id'), status=201)


@bp.route(f'{PREFIX}/leads/<int:crm_lead_id>/link-tms', methods=['POST'])
@_authenticated
def link_tms(crm_lead_id):
    """Record that this CRM lead is that TMS project.

    The identifiers are the stable ones. Not the subject line and not
    the company name — both get edited, and a cross-reference that
    breaks when somebody fixes a typo is not a cross-reference.
    """
    from app import Lead

    replay = _replayed(request.path)
    if replay is not None:
        return replay

    lead = Lead.query.get(crm_lead_id)
    if lead is None:
        return _fail('not_found', f'No CRM lead {crm_lead_id}.', 404)

    data = request.get_json(silent=True) or {}
    try:
        row, created = integ.link(
            crm_lead_id=lead.id, crm_account_id=lead.company_id,
            tms_project_id=(data.get('tms_project_id') or '').strip() or None,
            tms_job_id=(data.get('tms_job_id') or '').strip() or None,
            link_type=(data.get('link_type') or 'project'),
            created_by=f'tms:{g.caller}', note=data.get('note'))
    except integ.IntegrationError as exc:
        return _fail(exc.code, exc.message, exc.status)

    return _ok({'created': created, 'link': row.to_dict(),
                'lead': _lead(lead)},
               crm_type='Lead', crm_id=lead.id,
               tms_id=row.tms_project_id, status=201 if created else 200)


def _lead(lead):
    """What the TMS is told about a lead. Deliberately narrow: the
    fields it needs to identify and display one, not the CRM's whole
    record."""
    links = [l.to_dict() for l in integ.links_for_lead(lead.id)]
    return {
        'crm_lead_id': lead.id,
        'company': lead.company or '',
        'crm_account_id': lead.company_id,
        'project': lead.project or '',
        'stage': lead.stage or '',
        'contact_name': lead.pic or '',
        'email': lead.email or '',
        'phone': lead.phone or '',
        'assigned_to': lead.assigned_to or '',
        'received_at': (lead.received_at or lead.created_at).isoformat()
                       if (lead.received_at or lead.created_at) else None,
        'links': links,
    }
