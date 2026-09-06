"""
Public source monitoring — data model (Phase 10 of the CRM upgrade).

Two tables:

    public_sources        — whitelisted public source to monitor for
                            competitor intelligence (website / press /
                            news / tender / linkedin).
    public_source_items   — individual items captured from a public
                            source.  Each item is a candidate for
                            promotion into competitor_intelligence.

The scheduled polling worker is OUT OF SCOPE for this phase — the
tables + Review UI let a user paste-in an event manually with source
attribution.  The polling worker slots in later without any schema
changes.
"""
from datetime import datetime

from app import db


class PublicSource(db.Model):
    __tablename__ = 'public_sources'

    id                  = db.Column(db.Integer, primary_key=True)
    name                = db.Column(db.String(120), nullable=False)
    # website / press / news / tender / linkedin
    kind                = db.Column(db.String(40))
    url                 = db.Column(db.String(500), nullable=False)
    competitor_id       = db.Column(db.Integer,
                                    db.ForeignKey('competitor_masters.id'),
                                    nullable=True, index=True)
    is_active           = db.Column(db.Boolean, default=True)
    last_polled_at      = db.Column(db.DateTime)
    poll_interval_hours = db.Column(db.Integer, default=24)
    created_at          = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id':                  self.id,
            'name':                self.name,
            'kind':                self.kind or '',
            'url':                 self.url,
            'competitor_id':       self.competitor_id,
            'is_active':           bool(self.is_active),
            'last_polled_at':      str(self.last_polled_at)[:19] if self.last_polled_at else '',
            'poll_interval_hours': self.poll_interval_hours,
            'created_at':          str(self.created_at)[:19] if self.created_at else '',
        }


class PublicSourceItem(db.Model):
    """One item captured from a public source; candidate intelligence."""
    __tablename__ = 'public_source_items'

    id                          = db.Column(db.Integer, primary_key=True)
    source_id                   = db.Column(db.Integer,
                                            db.ForeignKey('public_sources.id'),
                                            nullable=False, index=True)
    captured_at                 = db.Column(db.DateTime, default=datetime.utcnow)
    publication_date            = db.Column(db.Date)
    url                         = db.Column(db.String(500))
    title                       = db.Column(db.String(500))
    summary                     = db.Column(db.Text)
    reviewed_at                 = db.Column(db.DateTime)
    reviewed_by_id              = db.Column(db.String(20))
    promoted_to_intelligence_id = db.Column(db.Integer,
                                            db.ForeignKey('competitor_intelligence.id'))
    dismissed                   = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {
            'id':                          self.id,
            'source_id':                   self.source_id,
            'captured_at':                 str(self.captured_at)[:19] if self.captured_at else '',
            'publication_date':            str(self.publication_date) if self.publication_date else '',
            'url':                         self.url or '',
            'title':                       self.title or '',
            'summary':                     self.summary or '',
            'reviewed_at':                 str(self.reviewed_at)[:19] if self.reviewed_at else '',
            'reviewed_by_id':              self.reviewed_by_id or '',
            'promoted_to_intelligence_id': self.promoted_to_intelligence_id,
            'dismissed':                   bool(self.dismissed),
        }
