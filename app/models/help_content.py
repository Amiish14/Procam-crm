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


class HelpGuide(db.Model):
    """"What happened, why, and what to do" — one row per refusal.

    CRM-LEAD-FLOW-01 §5. The rule it exists to enforce: no screen
    may show only an error. Users were told "no" with no reason and
    no next step, so they rang the Tech team — which is a support
    cost and, worse, a reason to stop trusting the screen.

    The wording lives in the database rather than in the code so a
    sentence can be fixed by an administrator in a minute instead of
    waiting for a deploy. `code` is what the code refers to; every
    other column is words a person wrote and may rewrite.
    """
    __tablename__ = 'crm_help_guides'

    id             = db.Column(db.Integer, primary_key=True)
    code           = db.Column(db.String(40), unique=True,
                               nullable=False, index=True)
    title          = db.Column(db.String(200), nullable=False)

    #: The rule, in plain words. No codes, no stack traces.
    why            = db.Column(db.Text)

    #: Numbered steps, as a JSON list of strings.
    steps          = db.Column(db.JSON, default=list)

    #: [{label, action, target}] — the buttons that take the user
    #: there. `action` is what the page does with it: 'tab',
    #: 'focus', 'open', 'mail', 'help'.
    actions        = db.Column(db.JSON, default=list)

    #: '' for everybody, or a role name. A guide written for an
    #: administrator is noise to a salesperson.
    audience       = db.Column(db.String(40), default='')

    active         = db.Column(db.Boolean, default=True, index=True)
    updated_at     = db.Column(db.DateTime, default=datetime.utcnow,
                               onupdate=datetime.utcnow)
    last_edited_by = db.Column(db.String(20))

    def to_dict(self):
        return {
            'id': self.id, 'code': self.code, 'title': self.title,
            'why': self.why or '',
            'steps': list(self.steps or []),
            'actions': list(self.actions or []),
            'audience': self.audience or '',
            'active': bool(self.active),
            'last_edited_by': self.last_edited_by or '',
            'updated_at': str(self.updated_at)[:16]
            if self.updated_at else '',
        }


class HelpGuideShown(db.Model):
    """Every guide a person was shown, and whether it helped.

    §5.3. Without this the catalogue is a guess: the Thursday pack
    shows the ten guides shown most often and how many were
    resolved without a ticket, which is the only evidence of
    whether the wording works.
    """
    __tablename__ = 'crm_help_guide_shown'

    id          = db.Column(db.Integer, primary_key=True)
    code        = db.Column(db.String(40), nullable=False, index=True)
    emp_code    = db.Column(db.String(20), index=True)
    lead_id     = db.Column(db.Integer, index=True)
    page        = db.Column(db.String(80))

    #: Set when the person pressed one of the guide's own buttons.
    #: Null means they read it and did something else, or nothing.
    clicked     = db.Column(db.String(60))
    shown_at    = db.Column(db.DateTime, default=datetime.utcnow,
                            index=True)

    def to_dict(self):
        return {'id': self.id, 'code': self.code,
                'emp_code': self.emp_code or '',
                'lead_id': self.lead_id, 'page': self.page or '',
                'clicked': self.clicked or '',
                'shown_at': str(self.shown_at)[:16]
                if self.shown_at else ''}


class HelpTicket(db.Model):
    """"Ask for help", as a row somebody can answer.

    CRM-LEAD-FLOW-01 §5.2. The point of the button is that a person
    stuck on a screen gets a number back instead of a phone call to
    the Tech team, and that the Tech team gets the screen, the lead
    and the last refusal without having to ask for them.
    """
    __tablename__ = 'crm_help_tickets'

    id          = db.Column(db.Integer, primary_key=True)
    number      = db.Column(db.String(20), unique=True, index=True)
    emp_code    = db.Column(db.String(20), index=True)
    page        = db.Column(db.String(80))
    lead_id     = db.Column(db.Integer, index=True)

    #: The guide the person was looking at when they pressed it.
    #: Usually the fastest route to what actually went wrong.
    code        = db.Column(db.String(40))
    description = db.Column(db.Text)

    status      = db.Column(db.String(20), default='Open', index=True)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow,
                            index=True)
    closed_at   = db.Column(db.DateTime)
    closed_by   = db.Column(db.String(20))

    def to_dict(self):
        return {'id': self.id, 'number': self.number or '',
                'emp_code': self.emp_code or '', 'page': self.page or '',
                'lead_id': self.lead_id, 'code': self.code or '',
                'description': self.description or '',
                'status': self.status or 'Open',
                'created_at': str(self.created_at)[:16]
                if self.created_at else ''}
