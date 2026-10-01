"""
Vendor, vessel and India port-call intelligence.

Three registers that answer the questions a project-logistics desk asks
before it can quote:

    vendors     who can carry, lift, haul, store or clear this — by
                category, port and trade lane
    vessels     what a named ship can actually do — DWT, deck, gear,
                maximum lift, whether it is self-geared, whether it
                calls India, and who its local agent is
    port calls  which ships are coming to Mumbai, JNPT, Chennai,
                Paradip, Mundra, Kandla, Kolkata or Haldia, when, from
                where, with what, and through which agent

Categories, vessel types and ports are Master Data lists
(`vendor_category`, `vessel_type`, `india_port`), seeded once by the
migration and maintained by an administrator from then on. Adding a
ninth port is a row, not a release — which is why no port is named in
this file's code.

Where the data comes from
-------------------------
Vessel particulars and port calls are, in practice, licensed products:
AIS feeds, port-community systems and vessel registers. Those adapters
are implemented in app/intel/adapters.py and report themselves as
requiring an external data source until a subscription exists. Until
then every row in these registers is one a person entered, with the
source they entered it from. Nothing here seeds example vessels or
invented sailings: an empty register is the honest state of a CRM that
has not been given the data.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app import db
from app.intel import (LIST_CONFIDENCE, LIST_INDIA_PORT,
                       LIST_VENDOR_CATEGORY, LIST_VESSEL_TYPE, master_items)
from app.services import audit


class VendorIntelRefused(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


# ── vocabularies ─────────────────────────────────────────────────────
def categories():
    """Vendor categories, live from Master Data."""
    return [{'code': c, 'label': l} for c, l in
            master_items(LIST_VENDOR_CATEGORY)]


def vessel_types():
    return [{'code': c, 'label': l} for c, l in master_items(LIST_VESSEL_TYPE)]


def ports():
    return [{'code': c, 'label': l} for c, l in master_items(LIST_INDIA_PORT)]


def confidences():
    return [{'code': c, 'label': l} for c, l in master_items(LIST_CONFIDENCE)]


def vocabularies():
    return {'vendor_category': categories(), 'vessel_type': vessel_types(),
            'india_port': ports(), 'confidence': confidences()}


def _require_code(options, value, label, allow_blank=False):
    """A code must be one Master Data offers.

    When the list is empty nothing can be valid, and the message says to
    configure it rather than accepting free text that no filter will
    ever find again.
    """
    value = (value or '').strip()
    if not value:
        if allow_blank:
            return ''
        raise VendorIntelRefused(f'{label} is required.')
    codes = {o['code'] for o in options}
    if not codes:
        raise VendorIntelRefused(
            f'No {label.lower()} values are configured in Master Data yet. '
            f'An administrator adds them there first.')
    if value not in codes:
        raise VendorIntelRefused(
            f'{label} "{value}" is not in Master Data. Add it there so '
            f'every screen agrees on the vocabulary.')
    return value


def _provenance(row, *, source, source_url, confidence, actor,
                verified=False):
    """Stamp where a claim came from. Every register row gets this."""
    row.source = (source or '').strip()[:240] or None
    row.source_url = (source_url or '').strip()[:500] or None
    row.confidence = _require_code(confidences(), confidence or 'reported',
                                   'Confidence')
    row.captured_at = datetime.utcnow()
    if verified or row.confidence == 'confirmed':
        row.last_verified_at = datetime.utcnow()
        row.last_verified_by = actor
    return row


# ── vendors ──────────────────────────────────────────────────────────
def save_vendor(*, name, category, actor=None, vendor_id=None, **fields):
    """Create or update a vendor profile. Audited either way."""
    from app.models.intel import VendorProfile

    name = (name or '').strip()
    if not name:
        raise VendorIntelRefused('A vendor needs a name.')
    category = _require_code(categories(), category, 'Vendor category')

    row = (db.session.get(VendorProfile, int(vendor_id))
           if vendor_id else None)
    creating = row is None
    if creating:
        row = VendorProfile(created_by=actor)
        db.session.add(row)
    before = audit.snapshot(row, ('name', 'category', 'country',
                                  'contact_email', 'is_active'))
    row.name, row.category = name[:240], category
    for column, limit in (('subcategory', 60), ('country', 80),
                          ('city', 120), ('contact_name', 160),
                          ('contact_role', 120), ('contact_email', 160),
                          ('contact_phone', 60), ('website', 240)):
        if column in fields:
            row.__setattr__(column,
                            (str(fields[column] or '').strip()[:limit]
                             or None))
    for column in ('capabilities', 'notes'):
        if column in fields:
            setattr(row, column, (fields[column] or '').strip() or None)
    for column in ('ports_served', 'services', 'trade_lanes'):
        if column in fields:
            value = fields[column]
            if isinstance(value, str):
                value = [v.strip() for v in value.split(',') if v.strip()]
            setattr(row, column, list(value or []))
    if fields.get('company_id'):
        row.company_id = int(fields['company_id'])
    if 'is_active' in fields:
        row.is_active = bool(fields['is_active'])

    _provenance(row, source=fields.get('source', ''),
                source_url=fields.get('source_url', ''),
                confidence=fields.get('confidence', 'reported'),
                actor=actor, verified=bool(fields.get('verified')))
    db.session.flush()
    audit.record_change(
        'intel.vendor.create' if creating else 'intel.vendor.update',
        'vendor_profile', row.id, before,
        audit.snapshot(row, ('name', 'category', 'country',
                             'contact_email', 'is_active')), actor=actor)
    db.session.commit()
    return row


def search_vendors(*, q='', category='', port='', country='', service='',
                   page=1, per_page=50, include_inactive=False):
    from app.models.intel import VendorProfile as V

    query = V.query
    if not include_inactive:
        query = query.filter(V.is_active.is_(True))
    if q:
        query = query.filter(V.name.ilike(f'%{q}%'))
    if category:
        query = query.filter(V.category == category)
    if country:
        query = query.filter(V.country == country)
    try:
        page, per_page = max(1, int(page)), min(200, max(1, int(per_page)))
    except (TypeError, ValueError):
        page, per_page = 1, 50
    rows = query.order_by(V.name.asc()).all()
    # ports_served and services are JSON lists; filtering them in SQL
    # would need a JSON1 function that is not guaranteed on every
    # deployment's SQLite build, so it is done here on an already
    # category-narrowed set.
    if port:
        rows = [r for r in rows if port in (r.ports_served or [])]
    if service:
        rows = [r for r in rows if service in (r.services or [])]
    total = len(rows)
    start = (page - 1) * per_page
    return {'items': [r.to_dict() for r in rows[start:start + per_page]],
            'total': total, 'page': page, 'per_page': per_page,
            'pages': max(1, (total + per_page - 1) // per_page)}


# ── vessels ──────────────────────────────────────────────────────────
def save_vessel(*, name, actor=None, vessel_id=None, **fields):
    """Create or update a vessel capability record."""
    from app.models.intel import Vessel

    name = (name or '').strip()
    if not name:
        raise VendorIntelRefused('A vessel needs a name.')
    vessel_type = _require_code(vessel_types(), fields.get('vessel_type', ''),
                                'Vessel type', allow_blank=True)

    row = db.session.get(Vessel, int(vessel_id)) if vessel_id else None
    creating = row is None
    if creating:
        row = Vessel(created_by=actor)
        db.session.add(row)
    before = audit.snapshot(row, ('name', 'owner_operator', 'vessel_type',
                                  'max_lift_tonnes', 'calls_india'))
    row.name, row.vessel_type = name[:200], vessel_type or None
    for column, limit in (('imo', 20), ('owner_operator', 240),
                          ('cranes', 160), ('trading_area', 240),
                          ('local_agent', 240), ('contact_email', 160),
                          ('contact_phone', 60)):
        if column in fields:
            setattr(row, column,
                    (str(fields[column] or '').strip()[:limit] or None))
    for column in ('dwt', 'deck_capacity_sqm', 'max_lift_tonnes'):
        if column in fields and fields[column] not in (None, ''):
            try:
                setattr(row, column, int(float(fields[column])))
            except (TypeError, ValueError):
                raise VendorIntelRefused(f'{column} must be a number.')
    for column in ('self_geared', 'roro', 'heavy_lift', 'calls_india',
                   'is_active'):
        if column in fields:
            setattr(row, column, bool(fields[column]))
    if 'notes' in fields:
        row.notes = (fields['notes'] or '').strip() or None
    if fields.get('vendor_id'):
        row.vendor_id = int(fields['vendor_id'])

    _provenance(row, source=fields.get('source', ''),
                source_url=fields.get('source_url', ''),
                confidence=fields.get('confidence', 'reported'),
                actor=actor, verified=bool(fields.get('verified')))
    db.session.flush()
    audit.record_change(
        'intel.vessel.create' if creating else 'intel.vessel.update',
        'vessel', row.id, before,
        audit.snapshot(row, ('name', 'owner_operator', 'vessel_type',
                             'max_lift_tonnes', 'calls_india')), actor=actor)
    db.session.commit()
    return row


def search_vessels(*, q='', vessel_type='', min_lift=None, self_geared=None,
                   roro=None, heavy_lift=None, calls_india=None,
                   operator='', page=1, per_page=50):
    """Find a ship that can take the piece.

    `min_lift` is the honest filter: a vessel whose maximum lift nobody
    has recorded is excluded, because "we do not know" is not "it can".
    """
    from app.models.intel import Vessel as V

    query = V.query.filter(V.is_active.is_(True))
    if q:
        query = query.filter(V.name.ilike(f'%{q}%'))
    if operator:
        query = query.filter(V.owner_operator.ilike(f'%{operator}%'))
    if vessel_type:
        query = query.filter(V.vessel_type == vessel_type)
    if min_lift not in (None, ''):
        query = query.filter(V.max_lift_tonnes.isnot(None),
                             V.max_lift_tonnes >= int(min_lift))
    for column, value in ((V.self_geared, self_geared), (V.roro, roro),
                          (V.heavy_lift, heavy_lift),
                          (V.calls_india, calls_india)):
        if value is not None:
            query = query.filter(column.is_(bool(value)))
    try:
        page, per_page = max(1, int(page)), min(200, max(1, int(per_page)))
    except (TypeError, ValueError):
        page, per_page = 1, 50
    total = query.count()
    rows = (query.order_by(V.max_lift_tonnes.desc().nullslast(),
                           V.name.asc())
            .offset((page - 1) * per_page).limit(per_page).all())
    return {'items': [r.to_dict() for r in rows], 'total': total,
            'page': page, 'per_page': per_page,
            'pages': max(1, (total + per_page - 1) // per_page)}


# ── port calls ───────────────────────────────────────────────────────
def save_port_call(*, vessel_name, port_code, actor=None, call_id=None,
                   **fields):
    """Record or correct an India port call."""
    from app.models.intel import Vessel, VesselPortCall

    vessel_name = (vessel_name or '').strip()
    if not vessel_name:
        raise VendorIntelRefused('Name the vessel.')
    port_code = _require_code(ports(), port_code, 'Port')

    row = db.session.get(VesselPortCall, int(call_id)) if call_id else None
    creating = row is None
    if creating:
        row = VesselPortCall(created_by=actor)
        db.session.add(row)
    before = audit.snapshot(row, ('vessel_name', 'port_code', 'eta', 'etd'))
    row.vessel_name, row.port_code = vessel_name[:200], port_code
    row.port_name = dict((p['code'], p['label']) for p in ports()).get(
        port_code, '')[:120] or None
    for column, limit in (('operator', 240), ('previous_port', 120),
                          ('next_port', 120), ('agent', 240)):
        if column in fields:
            setattr(row, column,
                    (str(fields[column] or '').strip()[:limit] or None))
    for column in ('cargo', 'notes'):
        if column in fields:
            setattr(row, column, (fields[column] or '').strip() or None)
    for column in ('eta', 'etd'):
        if column in fields:
            setattr(row, column, _as_datetime(fields[column]))
    if fields.get('vendor_id'):
        row.vendor_id = int(fields['vendor_id'])
    # Link the call to a vessel record when we already hold one; the
    # name alone stays authoritative, because a call can be known before
    # the ship's particulars are.
    if row.vessel_id is None:
        match = Vessel.query.filter(Vessel.name.ilike(vessel_name)).first()
        if match is not None:
            row.vessel_id = match.id

    _provenance(row, source=fields.get('source', ''),
                source_url=fields.get('source_url', ''),
                confidence=fields.get('confidence', 'reported'),
                actor=actor, verified=bool(fields.get('verified')))
    db.session.flush()
    audit.record_change(
        'intel.port_call.create' if creating else 'intel.port_call.update',
        'vessel_port_call', row.id, before,
        audit.snapshot(row, ('vessel_name', 'port_code', 'eta', 'etd')),
        actor=actor)
    db.session.commit()
    return row


def _as_datetime(value):
    if isinstance(value, datetime):
        return value
    text = str(value or '').strip()
    if not text:
        return None
    for fmt in ('%Y-%m-%dT%H:%M', '%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M',
                '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise VendorIntelRefused(f'"{text}" is not a date and time we can read.')


def port_calls(*, port_code='', vessel='', operator='', days_ahead=None,
               since=None, page=1, per_page=50):
    from app.models.intel import VesselPortCall as C

    query = C.query
    if port_code:
        query = query.filter(C.port_code == port_code)
    if vessel:
        query = query.filter(C.vessel_name.ilike(f'%{vessel}%'))
    if operator:
        query = query.filter(C.operator.ilike(f'%{operator}%'))
    if since:
        query = query.filter(C.eta >= since)
    if days_ahead:
        query = query.filter(C.eta <= datetime.utcnow()
                             + timedelta(days=int(days_ahead)))
    try:
        page, per_page = max(1, int(page)), min(200, max(1, int(per_page)))
    except (TypeError, ValueError):
        page, per_page = 1, 50
    total = query.count()
    rows = (query.order_by(C.eta.asc().nullslast(), C.id.desc())
            .offset((page - 1) * per_page).limit(per_page).all())
    return {'items': [r.to_dict() for r in rows], 'total': total,
            'page': page, 'per_page': per_page,
            'pages': max(1, (total + per_page - 1) // per_page)}


def coverage():
    """How much of each register actually holds anything.

    Shown at the top of the vendor pages so nobody mistakes an empty
    register for "no vendors call at Paradip".
    """
    from app.models.intel import VendorProfile, Vessel, VesselPortCall

    def _count(model, *filters):
        try:
            return model.query.filter(*filters).count()
        except Exception:
            return 0

    return {
        'vendors': _count(VendorProfile, VendorProfile.is_active.is_(True)),
        'vessels': _count(Vessel, Vessel.is_active.is_(True)),
        'port_calls': _count(VesselPortCall),
        'categories_configured': len(categories()),
        'ports_configured': len(ports()),
    }
