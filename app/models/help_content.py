"""Self-Help / User Manual — data model (Phase 13 of the CRM upgrade).

Two tables:

    help_articles   — long-form section-level manual pages.
    help_tooltips   — per-field / per-button contextual popovers keyed
                      by (page, element_key).
"""
from datetime import datetime

from app import db


class HelpArticle(db.Model):
    __tablename__ = 'help_articles'

    id                = db.Column(db.Integer, primary_key=True)
    slug              = db.Column(db.String(120), unique=True,
                                  nullable=False, index=True)
    section           = db.Column(db.String(60), nullable=False, index=True)
    title             = db.Column(db.String(200), nullable=False)
    what_it_is        = db.Column(db.Text)
    when_to_use       = db.Column(db.Text)
    how_to_use        = db.Column(db.Text)
    required_fields   = db.Column(db.Text)
    what_happens_next = db.Column(db.Text)
    common_mistakes   = db.Column(db.Text)
    role_visibility   = db.Column(db.JSON, default=list)   # empty = all
    display_order     = db.Column(db.Integer, default=0)
    is_active         = db.Column(db.Boolean, default=True)
    updated_at        = db.Column(db.DateTime, default=datetime.utcnow,
                                  onupdate=datetime.utcnow)
    updated_by_id     = db.Column(db.String(20))

    def to_dict(self):
        return {
            'id': self.id, 'slug': self.slug, 'section': self.section,
            'title': self.title,
            'what_it_is': self.what_it_is or '',
            'when_to_use': self.when_to_use or '',
            'how_to_use': self.how_to_use or '',
            'required_fields': self.required_fields or '',
            'what_happens_next': self.what_happens_next or '',
            'common_mistakes': self.common_mistakes or '',
            'role_visibility': list(self.role_visibility or []),
            'display_order': self.display_order or 0,
            'is_active': bool(self.is_active),
            'updated_at': str(self.updated_at) if self.updated_at else '',
            'updated_by_id': self.updated_by_id or '',
        }


class HelpTooltip(db.Model):
    """Per-field / per-button contextual help. Keyed by (page, element_key)."""
    __tablename__ = 'help_tooltips'

    id              = db.Column(db.Integer, primary_key=True)
    page            = db.Column(db.String(80), nullable=False, index=True)
    element_key     = db.Column(db.String(120), nullable=False)
    tooltip_html    = db.Column(db.Text, nullable=False)
    role_visibility = db.Column(db.JSON, default=list)
    is_active       = db.Column(db.Boolean, default=True)
    updated_at      = db.Column(db.DateTime, default=datetime.utcnow,
                                onupdate=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'page': self.page,
            'element_key': self.element_key,
            'tooltip_html': self.tooltip_html,
            'role_visibility': list(self.role_visibility or []),
            'is_active': bool(self.is_active),
            'updated_at': str(self.updated_at) if self.updated_at else '',
        }
