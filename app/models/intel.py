"""
External intelligence — additive tables (Release 5, Groups H, I and J).

Nothing here alters an existing table. Project intelligence already has
`projects`, `project_updates` and `project_stage_history` in
presales/models_projects.py, and competitor intelligence already has
`competitor_intelligence`; this module adds the columns those tables do
not have, in side-car tables keyed back to them, so both streams keep
working unchanged.

What each table is for
    intel_source_configs   how an adapter is wired to a row in the
                           existing `public_sources` table. Holds the
                           NAME of the environment variable carrying a
                           credential, never the credential itself.
    intel_source_runs      one row per attempt to collect from a source,
                           including the attempts that collected nothing
                           and why. A source that cannot run must leave
                           evidence, not silence.
    intel_raw_items        what arrived, before it was matched to a
                           project. The fingerprint is what stops the
                           same article being ingested twice.
    intel_project_facts    the structured capture of §12.1 against a
                           project: owner/group, land, clearance, EPC,
                           PMC, technology provider, equipment suppliers
                           and the rest, each with its source, dates,
                           confidence and last-verified stamp.
    intel_project_follows  who asked to be told when a project moves.
    intel_competitor_activity
                           competitor activity by vertical, service,
                           geography and industry — the axes
                           competitor_intelligence has no columns for.
    vendor_profiles        shipping lines, operators, agents, hauliers,
                           warehouses, customs partners, airlines and
                           overseas partners.
    vessels                vessel capability.
    vessel_port_calls      India port-call intelligence.

Confidence, source and last-verified appear on every intelligence row on
purpose: an unsourced claim about somebody else's project is a rumour,
and the screens have to be able to say which is which.
"""
from datetime import datetime

from app import db


# The confidence vocabulary is NOT defined here. It is a Master Data
# list (`intel_confidence`), seeded by the migration from
# app/intel/__init__.py and read at run time, so a second copy in the
# models would be a second source of truth that could drift.


class IntelSourceConfig(db.Model):
    """Which adapter runs a `public_sources` row, and with what settings.

    Kept apart from `public_sources` because that table belongs to the
    competitor-monitoring stream and must not grow columns here.
    """
    __tablename__ = 'intel_source_configs'

    id              = db.Column(db.Integer, primary_key=True)
    source_id       = db.Column(db.Integer,
                                db.ForeignKey('public_sources.id'),
                                nullable=False, index=True)
    adapter_key     = db.Column(db.String(40), nullable=False, index=True)
    #: project | competitor | vendor | port_call
    purpose         = db.Column(db.String(20), default='project', index=True)
    #: Adapter settings. Secrets are referenced by environment-variable
    #: name (e.g. {"api_key_env": "INTEL_PROJECTS_API_KEY"}); the value
    #: itself is never written to the database.
    config          = db.Column(db.JSON, default=dict)
    is_enabled      = db.Column(db.Boolean, default=False, index=True)
    notes           = db.Column(db.Text)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    created_by      = db.Column(db.String(20))
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow,
                                onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'source_id': self.source_id,
            'adapter_key': self.adapter_key or '',
            'purpose': self.purpose or '',
            'config': dict(self.config or {}),
            'is_enabled': bool(self.is_enabled),
            'notes': self.notes or '',
        }


class IntelSourceRun(db.Model):
    """One collection attempt. Written whether or not anything arrived."""
    __tablename__ = 'intel_source_runs'

    id              = db.Column(db.Integer, primary_key=True)
    source_id       = db.Column(db.Integer,
                                db.ForeignKey('public_sources.id'),
                                nullable=True, index=True)
    adapter_key     = db.Column(db.String(40), nullable=False, index=True)
    #: ok | unavailable | error
    status          = db.Column(db.String(20), default='ok', index=True)
    #: Why nothing came back, in words an administrator can act on.
    reason          = db.Column(db.Text)
    started_at      = db.Column(db.DateTime, default=datetime.utcnow,
                                index=True)
    finished_at     = db.Column(db.DateTime)
    items_fetched   = db.Column(db.Integer, default=0)
    items_new       = db.Column(db.Integer, default=0)
    items_matched   = db.Column(db.Integer, default=0)
    run_by          = db.Column(db.String(20))

    def to_dict(self):
        return {
            'id': self.id, 'source_id': self.source_id,
            'adapter_key': self.adapter_key or '',
            'status': self.status or '', 'reason': self.reason or '',
            'started_at': str(self.started_at)[:19] if self.started_at else '',
            'finished_at': str(self.finished_at)[:19] if self.finished_at else '',
            'items_fetched': self.items_fetched or 0,
            'items_new': self.items_new or 0,
            'items_matched': self.items_matched or 0,
            'run_by': self.run_by or '',
        }


class IntelRawItem(db.Model):
    """One item as it arrived, kept verbatim alongside what was made of it.

    `fingerprint` is unique: re-reading a feed re-sees the same article,
    and the second sighting must not become a second timeline entry.
    """
    __tablename__ = 'intel_raw_items'

    id               = db.Column(db.Integer, primary_key=True)
    source_id        = db.Column(db.Integer,
                                 db.ForeignKey('public_sources.id'),
                                 nullable=True, index=True)
    adapter_key      = db.Column(db.String(40), nullable=False, index=True)
    external_id      = db.Column(db.String(300))
    fingerprint      = db.Column(db.String(64), unique=True, nullable=False,
                                 index=True)
    url              = db.Column(db.String(500))
    title            = db.Column(db.String(500))
    body             = db.Column(db.Text)
    publication_date = db.Column(db.Date, index=True)
    captured_at      = db.Column(db.DateTime, default=datetime.utcnow,
                                 index=True)
    #: What the adapter or the person entering it said the item contains.
    payload          = db.Column(db.JSON, default=dict)
    confidence       = db.Column(db.String(20), default='reported')
    #: new | matched | created | ignored
    status           = db.Column(db.String(20), default='new', index=True)
    project_id       = db.Column(db.Integer, db.ForeignKey('projects.id'),
                                 nullable=True, index=True)
    project_update_id= db.Column(db.Integer,
                                 db.ForeignKey('project_updates.id'),
                                 nullable=True)
    match_note       = db.Column(db.Text)
    captured_by      = db.Column(db.String(20))

    def to_dict(self):
        return {
            'id': self.id, 'source_id': self.source_id,
            'adapter_key': self.adapter_key or '',
            'url': self.url or '', 'title': self.title or '',
            'publication_date': (str(self.publication_date)
                                 if self.publication_date else ''),
            'captured_at': str(self.captured_at)[:19] if self.captured_at else '',
            'confidence': self.confidence or '',
            'status': self.status or '', 'project_id': self.project_id,
            'match_note': self.match_note or '',
        }


class IntelProjectFact(db.Model):
    """The §12.1 structured capture for one project — one row per project.

    `match_key` is the normalised name + owner + location triple that
    de-duplication compares. It is stored rather than recomputed so that
    changing the normaliser later cannot silently re-split a project's
    timeline without a migration saying so.
    """
    __tablename__ = 'intel_project_facts'

    id                  = db.Column(db.Integer, primary_key=True)
    project_id          = db.Column(db.Integer, db.ForeignKey('projects.id'),
                                    nullable=False, unique=True, index=True)
    match_key           = db.Column(db.String(300), index=True)
    name_key            = db.Column(db.String(200), index=True)
    owner_key           = db.Column(db.String(120), index=True)
    location_key        = db.Column(db.String(120), index=True)

    owner_group         = db.Column(db.String(240))
    industry            = db.Column(db.String(120))
    location            = db.Column(db.String(240))
    value_inr           = db.Column(db.Numeric(16, 2))
    announcement_date   = db.Column(db.Date)
    #: One of app.intel.projects.INTEL_STAGES; the project's own `stage`
    #: column keeps the CRM vocabulary it already used.
    intel_stage         = db.Column(db.String(40), index=True)
    land_status         = db.Column(db.String(240))
    clearance_status    = db.Column(db.String(240))
    epc_contractor      = db.Column(db.String(240))
    pmc                 = db.Column(db.String(240))
    technology_provider = db.Column(db.String(240))
    equipment_suppliers = db.Column(db.JSON, default=list)
    logistics_needs     = db.Column(db.Text)
    timeline_note       = db.Column(db.Text)

    source_ref          = db.Column(db.String(240))
    source_url          = db.Column(db.String(500))
    publication_date    = db.Column(db.Date)
    captured_at         = db.Column(db.DateTime, default=datetime.utcnow)
    confidence          = db.Column(db.String(20), default='reported',
                                    index=True)
    last_verified_at    = db.Column(db.DateTime)
    last_verified_by    = db.Column(db.String(20))
    #: Names, organisations and places pulled out of the source text.
    extracted_entities  = db.Column(db.JSON, default=dict)

    bd_owner_emp_code   = db.Column(db.String(20), index=True)
    vertical            = db.Column(db.String(60), index=True)
    #: not_started | lead_created | opportunity | not_relevant
    opportunity_status  = db.Column(db.String(30), default='not_started',
                                    index=True)
    lead_id             = db.Column(db.Integer, db.ForeignKey('leads.id'),
                                    nullable=True, index=True)
    not_relevant        = db.Column(db.Boolean, default=False, index=True)
    not_relevant_reason = db.Column(db.String(300))
    update_count        = db.Column(db.Integer, default=0)
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)
    created_by          = db.Column(db.String(20))

    def to_dict(self):
        return {
            'project_id': self.project_id,
            'match_key': self.match_key or '',
            'owner_group': self.owner_group or '',
            'industry': self.industry or '',
            'location': self.location or '',
            'value_inr': float(self.value_inr) if self.value_inr else None,
            'announcement_date': (str(self.announcement_date)
                                  if self.announcement_date else ''),
            'intel_stage': self.intel_stage or '',
            'land_status': self.land_status or '',
            'clearance_status': self.clearance_status or '',
            'epc_contractor': self.epc_contractor or '',
            'pmc': self.pmc or '',
            'technology_provider': self.technology_provider or '',
            'equipment_suppliers': list(self.equipment_suppliers or []),
            'logistics_needs': self.logistics_needs or '',
            'timeline_note': self.timeline_note or '',
            'source_ref': self.source_ref or '',
            'source_url': self.source_url or '',
            'publication_date': (str(self.publication_date)
                                 if self.publication_date else ''),
            'captured_at': str(self.captured_at)[:19] if self.captured_at else '',
            'confidence': self.confidence or '',
            'last_verified_at': (str(self.last_verified_at)[:19]
                                 if self.last_verified_at else ''),
            'last_verified_by': self.last_verified_by or '',
            'extracted_entities': dict(self.extracted_entities or {}),
            'bd_owner': self.bd_owner_emp_code or '',
            'vertical': self.vertical or '',
            'opportunity_status': self.opportunity_status or '',
            'lead_id': self.lead_id,
            'not_relevant': bool(self.not_relevant),
            'not_relevant_reason': self.not_relevant_reason or '',
            'update_count': self.update_count or 0,
        }


class IntelProjectFollow(db.Model):
    """Who wants to hear about a project they do not own."""
    __tablename__ = 'intel_project_follows'

    id         = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('projects.id'),
                           nullable=False, index=True)
    emp_code   = db.Column(db.String(20), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('project_id', 'emp_code',
                            name='uq_intel_follow'),
    )


class IntelCompetitorActivity(db.Model):
    """Competitor activity on the axes the CRM can already slice by.

    `competitor_intelligence` records the event; this records where it
    happened — vertical, service, geography, industry — so the question
    "who is winning heavy-lift work in Gujarat?" is a query rather than
    a reading exercise.
    """
    __tablename__ = 'intel_competitor_activity'

    id                     = db.Column(db.Integer, primary_key=True)
    #: The competitor, as a Company (§19) and/or the legacy master row.
    company_id             = db.Column(db.Integer,
                                       db.ForeignKey('companies.id'),
                                       nullable=True, index=True)
    competitor_master_id   = db.Column(db.Integer,
                                       db.ForeignKey('competitor_masters.id'),
                                       nullable=True, index=True)
    competitor_name        = db.Column(db.String(240), nullable=False,
                                       index=True)

    #: Every one of these is a Master Data code, never a hard-coded list.
    vertical               = db.Column(db.String(60), index=True)
    service                = db.Column(db.String(60), index=True)
    industry               = db.Column(db.String(120), index=True)
    geography              = db.Column(db.String(120), index=True)
    activity_type          = db.Column(db.String(60), index=True)

    summary                = db.Column(db.Text, nullable=False)
    event_date             = db.Column(db.Date, index=True)
    confidence             = db.Column(db.String(20), default='reported',
                                       index=True)
    source                 = db.Column(db.String(240))
    source_url             = db.Column(db.String(500))
    publication_date       = db.Column(db.Date)
    captured_at            = db.Column(db.DateTime, default=datetime.utcnow)
    last_verified_at       = db.Column(db.DateTime)
    last_verified_by       = db.Column(db.String(20))

    related_account_id     = db.Column(db.Integer,
                                       db.ForeignKey('companies.id'),
                                       nullable=True, index=True)
    related_opportunity_id = db.Column(db.Integer,
                                       db.ForeignKey('opportunities.id'),
                                       nullable=True, index=True)
    related_project_id     = db.Column(db.Integer,
                                       db.ForeignKey('projects.id'),
                                       nullable=True, index=True)
    #: Where it came from, when it was promoted out of the review queue.
    raw_item_id            = db.Column(db.Integer,
                                       db.ForeignKey('intel_raw_items.id'),
                                       nullable=True)
    added_by               = db.Column(db.String(20))
    added_at               = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'competitor_name': self.competitor_name or '',
            'company_id': self.company_id,
            'vertical': self.vertical or '', 'service': self.service or '',
            'industry': self.industry or '', 'geography': self.geography or '',
            'activity_type': self.activity_type or '',
            'summary': self.summary or '',
            'event_date': str(self.event_date) if self.event_date else '',
            'confidence': self.confidence or '',
            'source': self.source or '', 'source_url': self.source_url or '',
            'publication_date': (str(self.publication_date)
                                 if self.publication_date else ''),
            'captured_at': str(self.captured_at)[:19] if self.captured_at else '',
            'last_verified_at': (str(self.last_verified_at)[:19]
                                 if self.last_verified_at else ''),
            'related_account_id': self.related_account_id,
            'related_opportunity_id': self.related_opportunity_id,
            'related_project_id': self.related_project_id,
            'added_by': self.added_by or '',
        }


class VendorProfile(db.Model):
    """A carrier, operator, agent, haulier, warehouse or partner.

    `category` is a Master Data code (list key `vendor_category`), so
    adding "project forwarder" is an admin action, not a release.
    """
    __tablename__ = 'vendor_profiles'

    id               = db.Column(db.Integer, primary_key=True)
    name             = db.Column(db.String(240), nullable=False, index=True)
    category         = db.Column(db.String(60), nullable=False, index=True)
    subcategory      = db.Column(db.String(60), index=True)
    company_id       = db.Column(db.Integer, db.ForeignKey('companies.id'),
                                 nullable=True, index=True)
    country          = db.Column(db.String(80), index=True)
    city             = db.Column(db.String(120))
    #: Master Data `india_port` codes, plus any free ports typed in.
    ports_served     = db.Column(db.JSON, default=list)
    services         = db.Column(db.JSON, default=list)
    trade_lanes      = db.Column(db.JSON, default=list)
    capabilities     = db.Column(db.Text)
    #: Role title, never a personal name in the column name itself.
    contact_name     = db.Column(db.String(160))
    contact_role     = db.Column(db.String(120))
    contact_email    = db.Column(db.String(160))
    contact_phone    = db.Column(db.String(60))
    website          = db.Column(db.String(240))
    notes            = db.Column(db.Text)

    source           = db.Column(db.String(240))
    source_url       = db.Column(db.String(500))
    confidence       = db.Column(db.String(20), default='reported')
    captured_at      = db.Column(db.DateTime, default=datetime.utcnow)
    last_verified_at = db.Column(db.DateTime)
    last_verified_by = db.Column(db.String(20))
    is_active        = db.Column(db.Boolean, default=True, index=True)
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    created_by       = db.Column(db.String(20))

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'category': self.category or '',
            'subcategory': self.subcategory or '',
            'company_id': self.company_id,
            'country': self.country or '', 'city': self.city or '',
            'ports_served': list(self.ports_served or []),
            'services': list(self.services or []),
            'trade_lanes': list(self.trade_lanes or []),
            'capabilities': self.capabilities or '',
            'contact_name': self.contact_name or '',
            'contact_role': self.contact_role or '',
            'contact_email': self.contact_email or '',
            'contact_phone': self.contact_phone or '',
            'website': self.website or '', 'notes': self.notes or '',
            'source': self.source or '', 'source_url': self.source_url or '',
            'confidence': self.confidence or '',
            'last_verified_at': (str(self.last_verified_at)[:19]
                                 if self.last_verified_at else ''),
            'is_active': bool(self.is_active),
        }


class Vessel(db.Model):
    """Vessel capability — what a ship can actually lift and carry."""
    __tablename__ = 'vessels'

    id                = db.Column(db.Integer, primary_key=True)
    name              = db.Column(db.String(200), nullable=False, index=True)
    imo               = db.Column(db.String(20), index=True)
    owner_operator    = db.Column(db.String(240), index=True)
    vendor_id         = db.Column(db.Integer,
                                  db.ForeignKey('vendor_profiles.id'),
                                  nullable=True, index=True)
    #: Master Data `vessel_type` code — MPV, breakbulk, RoRo, heavy-lift…
    vessel_type       = db.Column(db.String(60), index=True)
    dwt               = db.Column(db.Integer)
    deck_capacity_sqm = db.Column(db.Integer)
    #: Free text because "2 x 400 t" says more than a number would.
    cranes            = db.Column(db.String(160))
    max_lift_tonnes   = db.Column(db.Integer, index=True)
    self_geared       = db.Column(db.Boolean, default=False, index=True)
    roro              = db.Column(db.Boolean, default=False, index=True)
    heavy_lift        = db.Column(db.Boolean, default=False, index=True)
    trading_area      = db.Column(db.String(240))
    calls_india       = db.Column(db.Boolean, default=False, index=True)
    local_agent       = db.Column(db.String(240))
    contact_email     = db.Column(db.String(160))
    contact_phone     = db.Column(db.String(60))
    notes             = db.Column(db.Text)

    source            = db.Column(db.String(240))
    source_url        = db.Column(db.String(500))
    confidence        = db.Column(db.String(20), default='reported')
    captured_at       = db.Column(db.DateTime, default=datetime.utcnow)
    last_verified_at  = db.Column(db.DateTime)
    last_verified_by  = db.Column(db.String(20))
    is_active         = db.Column(db.Boolean, default=True, index=True)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)
    created_by        = db.Column(db.String(20))

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'imo': self.imo or '',
            'owner_operator': self.owner_operator or '',
            'vendor_id': self.vendor_id,
            'vessel_type': self.vessel_type or '',
            'dwt': self.dwt, 'deck_capacity_sqm': self.deck_capacity_sqm,
            'cranes': self.cranes or '',
            'max_lift_tonnes': self.max_lift_tonnes,
            'self_geared': bool(self.self_geared),
            'roro': bool(self.roro), 'heavy_lift': bool(self.heavy_lift),
            'trading_area': self.trading_area or '',
            'calls_india': bool(self.calls_india),
            'local_agent': self.local_agent or '',
            'contact_email': self.contact_email or '',
            'contact_phone': self.contact_phone or '',
            'notes': self.notes or '',
            'source': self.source or '', 'source_url': self.source_url or '',
            'confidence': self.confidence or '',
            'last_verified_at': (str(self.last_verified_at)[:19]
                                 if self.last_verified_at else ''),
            'is_active': bool(self.is_active),
        }


class VesselPortCall(db.Model):
    """An India port call: which ship, which berth window, whose cargo.

    The vessel name is stored as well as the link, because a call can be
    known before the vessel has a capability record.
    """
    __tablename__ = 'vessel_port_calls'

    id               = db.Column(db.Integer, primary_key=True)
    vessel_id        = db.Column(db.Integer, db.ForeignKey('vessels.id'),
                                 nullable=True, index=True)
    vessel_name      = db.Column(db.String(200), nullable=False, index=True)
    #: Master Data `india_port` code — extensible without a release.
    port_code        = db.Column(db.String(40), nullable=False, index=True)
    port_name        = db.Column(db.String(120))
    operator         = db.Column(db.String(240))
    eta              = db.Column(db.DateTime, index=True)
    etd              = db.Column(db.DateTime, index=True)
    previous_port    = db.Column(db.String(120))
    next_port        = db.Column(db.String(120))
    cargo            = db.Column(db.Text)
    agent            = db.Column(db.String(240))
    vendor_id        = db.Column(db.Integer,
                                 db.ForeignKey('vendor_profiles.id'),
                                 nullable=True, index=True)
    notes            = db.Column(db.Text)

    source           = db.Column(db.String(240))
    source_url       = db.Column(db.String(500))
    confidence       = db.Column(db.String(20), default='reported')
    captured_at      = db.Column(db.DateTime, default=datetime.utcnow,
                                 index=True)
    last_verified_at = db.Column(db.DateTime)
    last_verified_by = db.Column(db.String(20))
    created_by       = db.Column(db.String(20))

    def to_dict(self):
        return {
            'id': self.id, 'vessel_id': self.vessel_id,
            'vessel_name': self.vessel_name,
            'port_code': self.port_code or '', 'port_name': self.port_name or '',
            'operator': self.operator or '',
            'eta': str(self.eta)[:16] if self.eta else '',
            'etd': str(self.etd)[:16] if self.etd else '',
            'previous_port': self.previous_port or '',
            'next_port': self.next_port or '',
            'cargo': self.cargo or '', 'agent': self.agent or '',
            'source': self.source or '', 'source_url': self.source_url or '',
            'confidence': self.confidence or '',
            'captured_at': str(self.captured_at)[:19] if self.captured_at else '',
            'last_verified_at': (str(self.last_verified_at)[:19]
                                 if self.last_verified_at else ''),
        }
