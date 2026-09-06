"""
Competitor Master + Multiple Competitors per Deal — data model
(Phase 9 of the CRM upgrade).

Five tables:

    competitor_masters       — canonical company record for a competitor.
    opportunity_competitors  — junction: competitor <-> opp/lead/rfq/quote
                                with status (Possible / Likely / Confirmed /
                                Winning / Lost To) and price info.
    competitor_contacts      — public professional contacts at the competitor.
    competitor_intelligence  — dated timeline of intelligence events
                                (public sources only).
    competitor_assessments   — dated 1-5 capability scores across axes
                                (spec §39).

The legacy per-lead `Competitor` model in app.py stays untouched for
backward compat.  New code should treat CompetitorMaster as the canonical
row and OpportunityCompetitor as the many-to-many link.
"""
from datetime import date, datetime

from app import db


# =========================================================================
# Canonical competitor company
# =========================================================================
class CompetitorMaster(db.Model):
    __tablename__ = 'competitor_masters'

    id                    = db.Column(db.Integer, primary_key=True)
    name                  = db.Column(db.String(240), unique=True,
                                      nullable=False, index=True)
    aliases               = db.Column(db.JSON, default=list)   # ['K+N', 'KN']
    website               = db.Column(db.String(240))
    country               = db.Column(db.String(80))
    city                  = db.Column(db.String(120))
    hq_address            = db.Column(db.Text)

    services              = db.Column(db.JSON, default=list)
    verticals             = db.Column(db.JSON, default=list)
    capabilities          = db.Column(db.Text)
    geographic_reach      = db.Column(db.JSON, default=list)
    equipment_notes       = db.Column(db.Text)
    key_customers_public  = db.Column(db.JSON, default=list)
    strengths             = db.Column(db.Text)
    weaknesses            = db.Column(db.Text)
    strategic_notes       = db.Column(db.Text)
    linkedin_url          = db.Column(db.String(240))

    is_active             = db.Column(db.Boolean, default=True)
    created_by_id         = db.Column(db.String(20))
    created_at            = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at            = db.Column(db.DateTime, default=datetime.utcnow,
                                      onupdate=datetime.utcnow)

    def to_dict(self, *, deep=False):
        d = {
            'id':                   self.id,
            'name':                 self.name,
            'aliases':              list(self.aliases or []),
            'website':              self.website or '',
            'country':              self.country or '',
            'city':                 self.city or '',
            'hq_address':           self.hq_address or '',
            'services':             list(self.services or []),
            'verticals':            list(self.verticals or []),
            'capabilities':         self.capabilities or '',
            'geographic_reach':     list(self.geographic_reach or []),
            'equipment_notes':      self.equipment_notes or '',
            'key_customers_public': list(self.key_customers_public or []),
            'strengths':            self.strengths or '',
            'weaknesses':           self.weaknesses or '',
            'strategic_notes':      self.strategic_notes or '',
            'linkedin_url':         self.linkedin_url or '',
            'is_active':            bool(self.is_active),
            'created_by_id':        self.created_by_id or '',
            'created_at':           str(self.created_at)[:19] if self.created_at else '',
            'updated_at':           str(self.updated_at)[:19] if self.updated_at else '',
        }
        if deep:
            d['contacts'] = [c.to_dict() for c in
                             CompetitorContact.query
                             .filter_by(competitor_id=self.id).all()]
            d['intelligence'] = [i.to_dict() for i in
                                 CompetitorIntelligence.query
                                 .filter_by(competitor_id=self.id)
                                 .order_by(CompetitorIntelligence.event_date.desc())
                                 .all()]
            d['assessments'] = [a.to_dict() for a in
                                CompetitorAssessment.query
                                .filter_by(competitor_id=self.id)
                                .order_by(CompetitorAssessment.assessment_date.desc())
                                .all()]
        return d


# =========================================================================
# Junction: multiple competitors per opportunity / lead / rfq / quote
# =========================================================================
class OpportunityCompetitor(db.Model):
    """Multiple competitors per opportunity/lead/rfq/quote with status."""
    __tablename__ = 'opportunity_competitors'

    id                = db.Column(db.Integer, primary_key=True)
    competitor_id     = db.Column(db.Integer,
                                  db.ForeignKey('competitor_masters.id'),
                                  nullable=False, index=True)

    opportunity_id    = db.Column(db.Integer,
                                  db.ForeignKey('opportunities.id'),
                                  nullable=True, index=True)
    lead_id           = db.Column(db.Integer,
                                  db.ForeignKey('leads.id'),
                                  nullable=True, index=True)
    rfq_id            = db.Column(db.Integer,
                                  db.ForeignKey('rfqs.id'),
                                  nullable=True, index=True)
    quote_id          = db.Column(db.Integer,
                                  db.ForeignKey('quotes.id'),
                                  nullable=True, index=True)

    # Possible / Likely / Confirmed / Winning / Lost To
    status            = db.Column(db.String(20), default='Possible',
                                  index=True)
    quoted_price      = db.Column(db.Numeric(15, 2))
    winning_price     = db.Column(db.Numeric(15, 2))
    currency          = db.Column(db.String(6), default='INR')
    # Client Told / Public / Vendor / Estimation / Confirmed
    price_source      = db.Column(db.String(80))
    strengths_here    = db.Column(db.Text)
    weaknesses_here   = db.Column(db.Text)
    notes             = db.Column(db.Text)

    is_active         = db.Column(db.Boolean, default=True, index=True)
    added_by_id       = db.Column(db.String(20))
    added_at          = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at        = db.Column(db.DateTime, default=datetime.utcnow,
                                  onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id':               self.id,
            'competitor_id':    self.competitor_id,
            'opportunity_id':   self.opportunity_id,
            'lead_id':          self.lead_id,
            'rfq_id':           self.rfq_id,
            'quote_id':         self.quote_id,
            'status':           self.status or 'Possible',
            'quoted_price':     float(self.quoted_price) if self.quoted_price else None,
            'winning_price':    float(self.winning_price) if self.winning_price else None,
            'currency':         self.currency or 'INR',
            'price_source':     self.price_source or '',
            'strengths_here':   self.strengths_here or '',
            'weaknesses_here':  self.weaknesses_here or '',
            'notes':            self.notes or '',
            'is_active':        bool(self.is_active),
            'added_by_id':      self.added_by_id or '',
            'added_at':         str(self.added_at)[:19] if self.added_at else '',
            'updated_at':       str(self.updated_at)[:19] if self.updated_at else '',
        }


# =========================================================================
# Public contacts at competitor company
# =========================================================================
class CompetitorContact(db.Model):
    """Public professional contacts.  Lawful public info only."""
    __tablename__ = 'competitor_contacts'

    id                 = db.Column(db.Integer, primary_key=True)
    competitor_id      = db.Column(db.Integer,
                                   db.ForeignKey('competitor_masters.id'),
                                   nullable=False, index=True)
    name               = db.Column(db.String(200), nullable=False)
    designation        = db.Column(db.String(160))
    department         = db.Column(db.String(120))
    country            = db.Column(db.String(80))
    city               = db.Column(db.String(120))
    linkedin_url       = db.Column(db.String(240))
    public_email       = db.Column(db.String(200))
    # Sales / Ops / Management / Technical / Other
    role_category      = db.Column(db.String(60))
    # LinkedIn / Company Website / Public News / Conference
    source             = db.Column(db.String(120))
    remarks            = db.Column(db.Text)
    last_updated_by_id = db.Column(db.String(20))
    last_updated_at    = db.Column(db.DateTime, default=datetime.utcnow,
                                   onupdate=datetime.utcnow)
    created_at         = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id':                 self.id,
            'competitor_id':      self.competitor_id,
            'name':               self.name,
            'designation':        self.designation or '',
            'department':         self.department or '',
            'country':            self.country or '',
            'city':               self.city or '',
            'linkedin_url':       self.linkedin_url or '',
            'public_email':       self.public_email or '',
            'role_category':      self.role_category or '',
            'source':             self.source or '',
            'remarks':            self.remarks or '',
            'last_updated_by_id': self.last_updated_by_id or '',
            'last_updated_at':    str(self.last_updated_at)[:19] if self.last_updated_at else '',
            'created_at':         str(self.created_at)[:19] if self.created_at else '',
        }


# =========================================================================
# Timeline of competitor intelligence
# =========================================================================
class CompetitorIntelligence(db.Model):
    """Timeline of intelligence.  Public sources only."""
    __tablename__ = 'competitor_intelligence'

    id                     = db.Column(db.Integer, primary_key=True)
    competitor_id          = db.Column(db.Integer,
                                       db.ForeignKey('competitor_masters.id'),
                                       nullable=False, index=True)
    event_type             = db.Column(db.String(60), nullable=False,
                                       index=True)
    event_date             = db.Column(db.Date, default=date.today,
                                       nullable=False)
    summary                = db.Column(db.Text, nullable=False)
    source                 = db.Column(db.String(240))
    source_url             = db.Column(db.String(500))
    attachment_path        = db.Column(db.Text)
    related_project_id     = db.Column(db.Integer,
                                       db.ForeignKey('projects.id'),
                                       nullable=True)
    related_account_id     = db.Column(db.Integer,
                                       db.ForeignKey('companies.id'),
                                       nullable=True)
    related_opportunity_id = db.Column(db.Integer,
                                       db.ForeignKey('opportunities.id'),
                                       nullable=True)
    added_by_id            = db.Column(db.String(20))
    added_at               = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id':                     self.id,
            'competitor_id':          self.competitor_id,
            'event_type':             self.event_type or '',
            'event_date':             str(self.event_date) if self.event_date else '',
            'summary':                self.summary or '',
            'source':                 self.source or '',
            'source_url':             self.source_url or '',
            'attachment_path':        self.attachment_path or '',
            'related_project_id':     self.related_project_id,
            'related_account_id':     self.related_account_id,
            'related_opportunity_id': self.related_opportunity_id,
            'added_by_id':            self.added_by_id or '',
            'added_at':               str(self.added_at)[:19] if self.added_at else '',
        }


# =========================================================================
# Dated capability assessment (1-5 scale, spec §39)
# =========================================================================
class CompetitorAssessment(db.Model):
    __tablename__ = 'competitor_assessments'

    id                     = db.Column(db.Integer, primary_key=True)
    competitor_id          = db.Column(db.Integer,
                                       db.ForeignKey('competitor_masters.id'),
                                       nullable=False, index=True)
    assessment_date        = db.Column(db.Date, default=date.today,
                                       nullable=False)
    pricing                = db.Column(db.Integer)
    fleet_assets           = db.Column(db.Integer)
    engineering            = db.Column(db.Integer)
    heavy_transport        = db.Column(db.Integer)
    project_logistics      = db.Column(db.Integer)
    freight_forwarding     = db.Column(db.Integer)
    warehousing            = db.Column(db.Integer)
    global_network         = db.Column(db.Integer)
    local_presence         = db.Column(db.Integer)
    customer_relationships = db.Column(db.Integer)
    response_speed         = db.Column(db.Integer)
    notes                  = db.Column(db.Text)
    assessed_by_id         = db.Column(db.String(20))
    created_at             = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id':                     self.id,
            'competitor_id':          self.competitor_id,
            'assessment_date':        str(self.assessment_date) if self.assessment_date else '',
            'pricing':                self.pricing,
            'fleet_assets':           self.fleet_assets,
            'engineering':            self.engineering,
            'heavy_transport':        self.heavy_transport,
            'project_logistics':      self.project_logistics,
            'freight_forwarding':     self.freight_forwarding,
            'warehousing':            self.warehousing,
            'global_network':         self.global_network,
            'local_presence':         self.local_presence,
            'customer_relationships': self.customer_relationships,
            'response_speed':         self.response_speed,
            'notes':                  self.notes or '',
            'assessed_by_id':         self.assessed_by_id or '',
            'created_at':             str(self.created_at)[:19] if self.created_at else '',
        }
