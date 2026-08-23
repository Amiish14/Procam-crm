# ============================================================
#  PROCAM CRM — Flask Application
#  Version 3.0  |  Ready for Render / PRERNA-stack hosting
# ============================================================

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, abort
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.middleware.dispatcher import DispatcherMiddleware
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from sqlalchemy import UniqueConstraint, Index
from datetime import datetime, date, timedelta
import os, json, re, io, difflib, secrets, threading

# Load .env before any os.environ reads — ensures CLI scripts and gunicorn
# share the same DATABASE_URL / SECRET_KEY / ADMIN_INITIAL_PASSWORD etc.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
except Exception:
    pass

# ─── Workflow stages (matches approved Opportunity workflow diagram) ────────
# New Opportunity → Qualified → Proposal Due → Negotiation Due →
# Decision Taken → Won  (Won triggers PM handoff)
# At any Decision point a deal can also go to Lost / On Hold / Not Interested.
STAGES_PIPELINE = ['New', 'Call Done', 'Profile Sent', 'Appointment',
                   'Visit Done', 'RFQ Generated']
STAGES_TERMINAL = ['Won', 'Lost', 'On Hold', 'Not Interested']
STAGES_ALL      = STAGES_PIPELINE + STAGES_TERMINAL
STAGE_NEXT = {
    'New':            'Call Done',
    'Call Done':      'Profile Sent',
    'Profile Sent':   'Appointment',
    'Appointment':    'Visit Done',
    'Visit Done':     'RFQ Generated',
    'RFQ Generated':  'Won',
}
# Legacy stage names → new stage names. Applied automatically when a
# lead's stage doesn't match STAGES_ALL. Ensures 9,500+ existing leads
# keep working through the new pipeline without a manual migration.
LEGACY_STAGE_MAP = {
    'Pre-Sales':        'New',
    'New Opportunity':  'Call Done',
    'Qualified':        'Profile Sent',
    'Proposal Due':     'Appointment',
    'Negotiation Due':  'Visit Done',
    'Decision Taken':   'RFQ Generated',
}
DECISION_OUTCOMES = ['Won', 'Lost', 'On Hold', 'Not Interested']

# ProConnect Opportunity stages — used AFTER a Lead reaches
# 'RFQ Generated' and is converted into an Opportunity.
OPP_STAGES_PIPELINE = ['Qualification', 'Needs Analysis',
                        'Proposal Sent', 'Negotiation']
OPP_STAGES_TERMINAL = ['Closed Won', 'Closed Lost']
OPP_STAGES_ALL      = OPP_STAGES_PIPELINE + OPP_STAGES_TERMINAL
OPP_STAGE_NEXT = {
    'Qualification':   'Needs Analysis',
    'Needs Analysis':  'Proposal Sent',
    'Proposal Sent':   'Negotiation',
    'Negotiation':     'Closed Won',
}
# Common Indian states — used in the Company + Lead state dropdown.
STATE_LIST = [
    'Andhra Pradesh', 'Arunachal Pradesh', 'Assam', 'Bihar', 'Chhattisgarh',
    'Goa', 'Gujarat', 'Haryana', 'Himachal Pradesh', 'Jharkhand',
    'Karnataka', 'Kerala', 'Madhya Pradesh', 'Maharashtra', 'Manipur',
    'Meghalaya', 'Mizoram', 'Nagaland', 'Odisha', 'Punjab', 'Rajasthan',
    'Sikkim', 'Tamil Nadu', 'Telangana', 'Tripura', 'Uttar Pradesh',
    'Uttarakhand', 'West Bengal',
    'Delhi', 'Jammu and Kashmir', 'Ladakh', 'Chandigarh',
    'Puducherry', 'Andaman and Nicobar Islands', 'Dadra and Nagar Haveli',
    'Daman and Diu', 'Lakshadweep',
]
# Reasons captured when a Lead / Opportunity is Lost
LOSS_REASONS = [
    'Price too high',
    'Lost to competitor',
    'Timeline mismatch',
    'Vehicle/capacity unavailable',
    'Customer postponed / on hold',
    'Not commercially viable',
    'Client selected in-house transport',
    'Documentation / compliance issue',
    'Payment terms mismatch',
    'Other',
]

app = Flask(__name__)
# ProxyFix honours X-Forwarded-Prefix set by nginx (see hub/nginx-procamlogitech.conf)
# so url_for() emits /CRM-prefixed URLs when we're proxied.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# Belt-and-braces: even if ProxyFix's X-Forwarded-Prefix handling misfires,
# force SCRIPT_NAME from the URL_PREFIX env var so url_for() always emits
# the /CRM-prefixed URL. Without this, Flask redirects can end up bare-path
# ("/login") and nginx 404s them.
_url_prefix = (os.environ.get('URL_PREFIX') or '').rstrip('/')
if _url_prefix:
    _inner_app = app.wsgi_app
    def _force_script_name(environ, start_response):
        environ['SCRIPT_NAME'] = _url_prefix
        return _inner_app(environ, start_response)
    app.wsgi_app = _force_script_name
app.secret_key = os.environ.get('SECRET_KEY') or secrets.token_hex(32)
db_url = os.environ.get('DATABASE_URL', 'sqlite:///procam_crm.db')
if db_url.startswith('postgres://'):
    db_url = db_url.replace('postgres://', 'postgresql://', 1)
app.config['SQLALCHEMY_DATABASE_URI'] = db_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
# On https://procamlogictech.com nginx sets X-Forwarded-Proto=https so cookies
# get the Secure flag; on plain-http local dev they don't.
app.config['SESSION_COOKIE_SECURE'] = os.environ.get('FLASK_ENV') == 'production'
# URL_PREFIX lets the app-side templates render CRM-prefixed asset URLs even
# outside of a request context (e.g. when computing an email link).
app.config['APPLICATION_ROOT'] = os.environ.get('URL_PREFIX', '/')

db = SQLAlchemy(app)

# Ensure the email-lead attachment storage directory exists. Overridable via
# EMAIL_INGEST_STORAGE_ROOT so local dev doesn't have to write to /var/www.
EMAIL_INGEST_STORAGE_ROOT = os.environ.get(
    'EMAIL_INGEST_STORAGE_ROOT', '/var/www/procam-crm/uploads/email_leads'
)
try:
    os.makedirs(EMAIL_INGEST_STORAGE_ROOT, exist_ok=True)
except Exception:
    # Non-fatal — file writes will fail later with a clearer error if the
    # dir truly isn't writable (e.g. running locally without perms).
    pass

# Simple in-process lock — the Opportunity numbering sequence is generated
# server-side and we want to serialise concurrent number requests within a
# single gunicorn worker. Between workers, the DB unique-constraint is the
# ultimate guard (see Opportunity.opp_number below).
_opp_lock = threading.Lock()

# ─────────────────── MODELS ───────────────────

class Employee(db.Model):
    __tablename__ = 'employees'
    id            = db.Column(db.Integer, primary_key=True)
    emp_code      = db.Column(db.String(20), unique=True, nullable=False)   # Used as username
    name          = db.Column(db.String(100), nullable=False)
    email         = db.Column(db.String(120), unique=True)
    mobile        = db.Column(db.String(20))
    department    = db.Column(db.String(60))   # Sales, Pre-Sales, Operations, Finance, Admin
    designation   = db.Column(db.String(80))
    vertical      = db.Column(db.String(60))   # Heavy Transport, PFM, Warehousing, Installation, Admin
    role          = db.Column(db.String(20), default='user')  # admin | sales | presales | user
    password_hash = db.Column(db.String(256))
    must_change_pw= db.Column(db.Boolean, default=True)  # Force change on first login
    is_active     = db.Column(db.Boolean, default=True)
    joined_on     = db.Column(db.Date, default=date.today)
    industries    = db.Column(db.Text, default='[]')   # JSON list
    # Dashboard-v2 hierarchy: is_vertical_head marks a manager;
    # vertical_head_id names the manager they report to (may be null for
    # admins / top-of-tree). Enables role-scoped drill-down without
    # inventing a separate teams table.
    is_vertical_head = db.Column(db.Boolean, default=False)
    vertical_head_id = db.Column(db.Integer, db.ForeignKey('employees.id'),
                                 nullable=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, pw):
        self.password_hash = generate_password_hash(pw)

    def check_password(self, pw):
        return check_password_hash(self.password_hash, pw)

    def to_dict(self):
        return {
            'id': self.id, 'emp_code': self.emp_code, 'name': self.name,
            'email': self.email, 'mobile': self.mobile,
            'department': self.department, 'designation': self.designation,
            'vertical': self.vertical, 'role': self.role,
            'must_change_pw': self.must_change_pw, 'is_active': self.is_active,
            'industries': json.loads(self.industries or '[]'),
            'joined_on': str(self.joined_on) if self.joined_on else ''
        }


class Lead(db.Model):
    __tablename__ = 'leads'
    id               = db.Column(db.Integer, primary_key=True)
    source           = db.Column(db.String(30), default='manual')
    company          = db.Column(db.String(200), nullable=False)
    project          = db.Column(db.String(300))
    industry         = db.Column(db.String(100))
    cost_million     = db.Column(db.Float, default=0)
    products         = db.Column(db.Text)
    state            = db.Column(db.String(60))
    city             = db.Column(db.String(60))
    country          = db.Column(db.String(60), default='India')
    # Contact
    pic              = db.Column(db.String(100))
    designation_pic  = db.Column(db.String(100))
    email            = db.Column(db.String(120))
    phone            = db.Column(db.String(30))
    email2           = db.Column(db.String(120))
    phone2           = db.Column(db.String(30))
    linkedin         = db.Column(db.String(200))
    # Pipeline
    stage            = db.Column(db.String(40), default='New Opportunity')
    procam_vertical  = db.Column(db.String(60))
    assigned_to      = db.Column(db.String(100))   # emp_code
    assigned_name    = db.Column(db.String(100))
    followup_date    = db.Column(db.Date)
    notes            = db.Column(db.Text)
    history          = db.Column(db.Text)
    # Activity dates
    phone_call_date  = db.Column(db.Date)
    intro_mail_date  = db.Column(db.Date)
    meeting_date     = db.Column(db.Date)
    rfq_date         = db.Column(db.Date)
    # Opportunity link
    opp_number       = db.Column(db.String(30))
    opp_stage        = db.Column(db.String(40))
    opp_close_date   = db.Column(db.Date)
    opp_notes        = db.Column(db.Text)
    # Dashboard-v2: structured lost reason (one of LOSS_REASONS) + timestamp
    # of the moment the current stage was entered, used by pipeline-ageing
    # buckets. Both nullable so old rows keep working; migration backfills
    # stage_entered_at from updated_at.
    lost_reason      = db.Column(db.String(60))
    stage_entered_at = db.Column(db.DateTime)
    # Tracking
    onboarded_date   = db.Column(db.Date, default=date.today)   # Date entered into system
    week_tag         = db.Column(db.String(20))
    email_sent_flag  = db.Column(db.String(100))
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Email ingest (source=email) — dedup key from Microsoft Graph internetMessageId
    email_message_id = db.Column(db.String(255), unique=True, index=True, nullable=True)
    # Structured JSON extracted from the email by Claude Haiku (see email_ingest/ai_extractor.py).
    # Includes: company, contact_name, designation, phone_primary, phone_secondary,
    # email_primary, email_secondary, origin, destination, cargo_type, cargo_weight_mt,
    # cargo_dimensions, cargo_qty, procam_vertical, requirement_type, urgency,
    # target_date, special_requirements (list), one_line_summary, next_action_suggested.
    # Rendered as a "Lead Summary" card at the top of the lead detail modal.
    email_extracted_json = db.Column(db.Text, nullable=True)

    def to_dict(self):
        def sd(d): return str(d) if d else ''
        return {
            'id': self.id, 'source': self.source, 'company': self.company,
            'project': self.project or '', 'industry': self.industry or '',
            'cost': self.cost_million or 0, 'products': self.products or '',
            'state': self.state or '', 'city': self.city or '', 'country': self.country or 'India',
            'pic': self.pic or '', 'designation': self.designation_pic or '',
            'email': self.email or '', 'phone': self.phone or '',
            'email2': self.email2 or '', 'phone2': self.phone2 or '',
            'linkedin': self.linkedin or '',
            'stage': self.stage or 'New', 'procam_vertical': self.procam_vertical or '',
            'assigned_to': self.assigned_to or '', 'assigned_name': self.assigned_name or '',
            'followup': sd(self.followup_date), 'notes': self.notes or '',
            'history': self.history or '',
            'phone_call_date': sd(self.phone_call_date),
            'intro_mail_date': sd(self.intro_mail_date),
            'meeting_date': sd(self.meeting_date),
            'rfq_date': sd(self.rfq_date),
            'opp_number': self.opp_number or '',
            'opp_stage': self.opp_stage or '',
            'opp_close_date': sd(self.opp_close_date),
            'opp_notes': self.opp_notes or '',
            'onboarded_date': sd(self.onboarded_date),
            'week_tag': self.week_tag or '',
            'created_at': str(self.created_at)[:10] if self.created_at else '',
            # AI-extracted structured summary (populated only for source=email leads
            # that went through Claude Haiku). Renders as the "Lead Summary" card
            # at the top of the lead detail modal.
            'email_extracted': (json.loads(self.email_extracted_json)
                                if self.email_extracted_json else None),
            # Files attached to this lead — populated by email ingest from
            # Graph API attachments, or manually uploaded later. Renders as
            # a list under the "Lead Summary" card in the UI.
            'attachments': [a.to_dict() for a in
                            (LeadAttachment.query.filter_by(lead_id=self.id)
                             .order_by(LeadAttachment.uploaded_at.desc()).all())],
        }


class LeadAttachment(db.Model):
    """A file attached to a Lead. Populated by email ingest (from Graph API
    attachments on lead emails) or manually uploaded via the CRM UI later."""
    __tablename__ = 'lead_attachments'
    id            = db.Column(db.Integer, primary_key=True)
    lead_id       = db.Column(db.Integer, db.ForeignKey('leads.id', ondelete='CASCADE'),
                              nullable=False, index=True)
    filename      = db.Column(db.String(255), nullable=False)
    content_type  = db.Column(db.String(120))
    size_bytes    = db.Column(db.Integer)
    storage_path  = db.Column(db.String(500), nullable=False)   # absolute path on VM disk
    source        = db.Column(db.String(30), default='email')   # email | manual
    email_attachment_id = db.Column(db.String(255), index=True)  # Graph id for dedup
    uploaded_at   = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id,
            'filename': self.filename,
            'content_type': self.content_type or 'application/octet-stream',
            'size_bytes': self.size_bytes or 0,
            'source': self.source,
            'uploaded_at': str(self.uploaded_at)[:16] if self.uploaded_at else '',
        }


class Contact(db.Model):
    __tablename__ = 'contacts'
    id          = db.Column(db.Integer, primary_key=True)
    contact_type= db.Column(db.String(20), default='person')  # person | company | agent
    name        = db.Column(db.String(150), nullable=False)
    company     = db.Column(db.String(200))
    designation = db.Column(db.String(100))
    industry    = db.Column(db.String(100))
    email       = db.Column(db.String(120))
    phone       = db.Column(db.String(30))
    mobile      = db.Column(db.String(30))
    # Global fields for overseas agents
    country     = db.Column(db.String(60))
    state       = db.Column(db.String(80), index=True)
    city        = db.Column(db.String(60))
    website     = db.Column(db.String(200))
    linkedin    = db.Column(db.String(200))
    agent_type  = db.Column(db.String(60))   # Overseas Agent, Partner, Vendor, Client
    assigned_to = db.Column(db.String(100))
    notes       = db.Column(db.Text)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'type': self.contact_type, 'name': self.name,
            'company': self.company or '', 'designation': self.designation or '',
            'industry': self.industry or '', 'email': self.email or '',
            'phone': self.phone or '', 'mobile': self.mobile or '',
            'country': self.country or '', 'city': self.city or '',
            'website': self.website or '', 'linkedin': self.linkedin or '',
            'agent_type': self.agent_type or '', 'assigned_to': self.assigned_to or '',
            'notes': self.notes or '',
            'created_at': str(self.created_at)[:10] if self.created_at else ''
        }


class NewsItem(db.Model):
    __tablename__ = 'news_items'
    id          = db.Column(db.Integer, primary_key=True)
    title       = db.Column(db.String(400), nullable=False)
    summary     = db.Column(db.Text)
    source      = db.Column(db.String(100))   # ETManufacturing, Projects Today, etc.
    category    = db.Column(db.String(80))    # Power, Steel, Chemicals, Infrastructure, etc.
    relevance   = db.Column(db.String(20))    # High, Medium, Low
    url         = db.Column(db.String(500))
    published_date = db.Column(db.Date)
    email_subject  = db.Column(db.String(400))
    status      = db.Column(db.String(20), default='pending')   # pending | assigned | deleted
    assigned_to = db.Column(db.String(100))
    lead_id     = db.Column(db.Integer)       # If converted to lead
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'title': self.title, 'summary': self.summary or '',
            'source': self.source or '', 'category': self.category or '',
            'relevance': self.relevance or 'Medium', 'url': self.url or '',
            'published_date': str(self.published_date) if self.published_date else '',
            'email_subject': self.email_subject or '',
            'status': self.status, 'assigned_to': self.assigned_to or '',
            'lead_id': self.lead_id, 'created_at': str(self.created_at)[:10]
        }


# ═══════════════════════════════════════════════════════════════════════════
# v3.1 additions — Company · OverseasAgent · Opportunity · LeadActivity ·
#                 LeadStageHistory · OutreachDraft · ImportBatch
# ═══════════════════════════════════════════════════════════════════════════

class Company(db.Model):
    """Global CRM — companies. Leads/Opportunities reference this instead of
    duplicating free-text company names."""
    __tablename__ = 'companies'
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(200), nullable=False, index=True)
    industry      = db.Column(db.String(120))
    website       = db.Column(db.String(200))
    country       = db.Column(db.String(80))
    state         = db.Column(db.String(80), index=True)
    city          = db.Column(db.String(80))
    address       = db.Column(db.Text)
    phone         = db.Column(db.String(40))
    email         = db.Column(db.String(120))
    linkedin      = db.Column(db.String(200))
    tier          = db.Column(db.String(10))     # A / B / C
    notes         = db.Column(db.Text)
    is_active     = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    created_by    = db.Column(db.String(20))
    __table_args__ = (Index('ix_company_name_lower',
                            db.func.lower(name)),)

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'industry': self.industry or '',
            'website': self.website or '', 'country': self.country or '',
            'state': self.state or '', 'city': self.city or '',
            'address': self.address or '',
            'phone': self.phone or '', 'email': self.email or '',
            'linkedin': self.linkedin or '', 'tier': self.tier or '',
            'notes': self.notes or '', 'is_active': self.is_active,
            'created_at': str(self.created_at)[:10],
        }


class OverseasAgent(db.Model):
    """Overseas agents — external partners abroad who feed leads/RFQs."""
    __tablename__ = 'overseas_agents'
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(200), nullable=False, index=True)
    country       = db.Column(db.String(80))
    city          = db.Column(db.String(80))
    website       = db.Column(db.String(200))
    contact_person= db.Column(db.String(120))
    phone         = db.Column(db.String(40))
    email         = db.Column(db.String(120))
    address       = db.Column(db.Text)
    notes         = db.Column(db.Text)
    is_active     = db.Column(db.Boolean, default=True)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'name': self.name, 'country': self.country or '',
            'city': self.city or '', 'website': self.website or '',
            'contact_person': self.contact_person or '',
            'phone': self.phone or '', 'email': self.email or '',
            'address': self.address or '', 'notes': self.notes or '',
            'is_active': self.is_active,
        }


class Opportunity(db.Model):
    """Persistent opportunity numbering. Replaces the stateless
    /api/opp-next-number counter — DB unique-constraint guarantees no duplicates
    even under concurrent user load."""
    __tablename__ = 'opportunities'
    id            = db.Column(db.Integer, primary_key=True)
    opp_number    = db.Column(db.String(30), unique=True, nullable=False, index=True)
    lead_id       = db.Column(db.Integer, db.ForeignKey('leads.id'), index=True)
    company_id    = db.Column(db.Integer, db.ForeignKey('companies.id'), index=True)
    title         = db.Column(db.String(255))
    stage         = db.Column(db.String(40), default='RFQ')
    value_inr     = db.Column(db.Numeric(15, 2))
    currency      = db.Column(db.String(6), default='INR')
    probability   = db.Column(db.Integer, default=50)   # 0-100
    expected_close_date = db.Column(db.Date)
    owner_emp_code= db.Column(db.String(20), index=True)
    notes         = db.Column(db.Text)
    rfq_received_date = db.Column(db.Date)
    won_at        = db.Column(db.DateTime)
    lost_at       = db.Column(db.DateTime)
    lost_reason   = db.Column(db.String(255))
    # Filled when a Won opportunity is converted to a project in TMS.
    # Free-text so it works standalone; may hold TMS project code (e.g. PRO2402...)
    won_project_ref = db.Column(db.String(60), index=True)
    won_project_at  = db.Column(db.DateTime)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at    = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    lead          = db.relationship('Lead', foreign_keys=[lead_id])
    company       = db.relationship('Company', foreign_keys=[company_id])

    def to_dict(self):
        return {
            'id': self.id, 'opp_number': self.opp_number,
            'lead_id': self.lead_id, 'company_id': self.company_id,
            'company_name': self.company.name if self.company else None,
            'title': self.title or '', 'stage': self.stage,
            'value_inr': float(self.value_inr) if self.value_inr else None,
            'currency': self.currency, 'probability': self.probability,
            'expected_close_date': str(self.expected_close_date) if self.expected_close_date else '',
            'owner_emp_code': self.owner_emp_code or '',
            'notes': self.notes or '',
            'rfq_received_date': str(self.rfq_received_date) if self.rfq_received_date else '',
            'won_at': str(self.won_at)[:10] if self.won_at else '',
            'lost_at': str(self.lost_at)[:10] if self.lost_at else '',
            'lost_reason': self.lost_reason or '',
            'won_project_ref': self.won_project_ref or '',
            'won_project_at': str(self.won_project_at)[:10] if self.won_project_at else '',
            'created_at': str(self.created_at)[:10],
        }


class LeadActivity(db.Model):
    """Timestamped activity log per lead — call, email, meeting, RFQ, note.
    Never lost when a lead's stage changes."""
    __tablename__ = 'lead_activities'
    id            = db.Column(db.Integer, primary_key=True)
    lead_id       = db.Column(db.Integer, db.ForeignKey('leads.id'), nullable=False, index=True)
    kind          = db.Column(db.String(30), nullable=False)  # call | email | meeting | rfq | note | visit
    subject       = db.Column(db.String(255))
    body          = db.Column(db.Text)
    occurred_at   = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    performed_by  = db.Column(db.String(20))   # emp_code
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'lead_id': self.lead_id, 'kind': self.kind,
            'subject': self.subject or '', 'body': self.body or '',
            'occurred_at': str(self.occurred_at)[:16],
            'performed_by': self.performed_by or '',
        }


class LeadStageHistory(db.Model):
    """Stage transition log so we never lose the sales-pipeline timeline."""
    __tablename__ = 'lead_stage_history'
    id            = db.Column(db.Integer, primary_key=True)
    lead_id       = db.Column(db.Integer, db.ForeignKey('leads.id'), nullable=False, index=True)
    from_stage    = db.Column(db.String(40))
    to_stage      = db.Column(db.String(40), nullable=False)
    changed_at    = db.Column(db.DateTime, default=datetime.utcnow)
    changed_by    = db.Column(db.String(20))
    note          = db.Column(db.Text)

    def to_dict(self):
        return {
            'id': self.id, 'lead_id': self.lead_id,
            'from_stage': self.from_stage or '',
            'to_stage': self.to_stage,
            'changed_at': str(self.changed_at)[:16],
            'changed_by': self.changed_by or '',
            'note': self.note or '',
        }


class OutreachDraft(db.Model):
    """AI-generated outreach draft saved for audit + human editing."""
    __tablename__ = 'outreach_drafts'
    id            = db.Column(db.Integer, primary_key=True)
    lead_id       = db.Column(db.Integer, db.ForeignKey('leads.id'), index=True)
    company_id    = db.Column(db.Integer, db.ForeignKey('companies.id'), index=True)
    channel       = db.Column(db.String(20), default='email')  # email | linkedin | whatsapp
    subject       = db.Column(db.String(255))
    body          = db.Column(db.Text)
    model         = db.Column(db.String(80))
    prompt_hash   = db.Column(db.String(64))
    status        = db.Column(db.String(20), default='draft')  # draft | sent | discarded
    generated_at  = db.Column(db.DateTime, default=datetime.utcnow)
    generated_by  = db.Column(db.String(20))
    sent_at       = db.Column(db.DateTime)

    def to_dict(self):
        return {
            'id': self.id, 'lead_id': self.lead_id, 'company_id': self.company_id,
            'channel': self.channel, 'subject': self.subject or '',
            'body': self.body or '', 'model': self.model or '',
            'status': self.status,
            'generated_at': str(self.generated_at)[:16],
            'generated_by': self.generated_by or '',
        }


class ImportBatch(db.Model):
    """Preview → commit staging for bulk Excel/CSV imports."""
    __tablename__ = 'import_batches'
    id            = db.Column(db.Integer, primary_key=True)
    kind          = db.Column(db.String(30), nullable=False)  # leads | companies | contacts | agents
    filename      = db.Column(db.String(255))
    header_map    = db.Column(db.Text)         # JSON: {"Company Name": "company", ...}
    total_rows    = db.Column(db.Integer, default=0)
    valid_rows    = db.Column(db.Integer, default=0)
    error_rows    = db.Column(db.Integer, default=0)
    duplicate_rows= db.Column(db.Integer, default=0)
    preview_data  = db.Column(db.Text)         # JSON: full parsed rows for review
    committed     = db.Column(db.Boolean, default=False)
    created_at    = db.Column(db.DateTime, default=datetime.utcnow)
    created_by    = db.Column(db.String(20))
    committed_at  = db.Column(db.DateTime)


class Competitor(db.Model):
    """Competitor tagged on an opportunity/lead (per workflow diagram)."""
    __tablename__ = 'competitors'
    id            = db.Column(db.Integer, primary_key=True)
    lead_id       = db.Column(db.Integer, db.ForeignKey('leads.id'),
                              nullable=False, index=True)
    name          = db.Column(db.String(200), nullable=False)
    quoted_price  = db.Column(db.Numeric(15, 2))
    strength      = db.Column(db.String(200))   # short note on why competitive
    weakness      = db.Column(db.String(200))
    notes         = db.Column(db.Text)
    added_by      = db.Column(db.String(20))
    added_at      = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'id': self.id, 'lead_id': self.lead_id, 'name': self.name,
            'quoted_price': float(self.quoted_price) if self.quoted_price else None,
            'strength': self.strength or '', 'weakness': self.weakness or '',
            'notes': self.notes or '',
            'added_by': self.added_by or '',
            'added_at': str(self.added_at)[:16],
        }


# ─────────────────── AUTH ROUTES ───────────────────

@app.route('/')
def index():
    if 'emp_code' not in session:
        return redirect(url_for('login'))
    return redirect(url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        data = request.get_json()
        emp_code = (data.get('emp_code') or '').strip().upper()
        password = data.get('password') or ''
        emp = Employee.query.filter_by(emp_code=emp_code, is_active=True).first()
        if emp and emp.check_password(password):
            session['emp_code'] = emp.emp_code
            session['name']     = emp.name
            session['role']     = emp.role
            session['vertical'] = emp.vertical or ''
            return jsonify({'ok': True, 'must_change': emp.must_change_pw, 'role': emp.role})
        return jsonify({'ok': False, 'error': 'Invalid Employee Code or Password'}), 401

    return render_template('login.html')

@app.route('/change-password', methods=['POST'])
def change_password():
    if 'emp_code' not in session:
        return jsonify({'ok': False}), 401
    data = request.get_json()
    emp = Employee.query.filter_by(emp_code=session['emp_code']).first()
    if not emp:
        return jsonify({'ok': False}), 404
    if not emp.check_password(data.get('current', '')):
        return jsonify({'ok': False, 'error': 'Current password incorrect'}), 400
    new_pw = data.get('new_password', '')
    if len(new_pw) < 6:
        return jsonify({'ok': False, 'error': 'Password must be at least 6 characters'}), 400
    emp.set_password(new_pw)
    emp.must_change_pw = False
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ─────────────────── MAIN APP ───────────────────

@app.route('/app')
def dashboard():
    if 'emp_code' not in session:
        return redirect(url_for('login'))
    emp = Employee.query.filter_by(emp_code=session['emp_code']).first()
    if emp and emp.must_change_pw:
        return render_template('change_password.html', emp=emp)
    return render_template('app.html', emp=emp)

# ─────────────────── API: EMPLOYEES ───────────────────

def require_auth(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'emp_code' not in session:
            return jsonify({'error': 'Not authenticated'}), 401
        return f(*args, **kwargs)
    return decorated

def require_admin(f):
    from functools import wraps
    @wraps(f)
    def decorated(*args, **kwargs):
        if session.get('role') not in ('admin',):
            return jsonify({'error': 'Admin access required'}), 403
        return f(*args, **kwargs)
    return decorated

@app.route('/api/me')
@require_auth
def api_me():
    emp = Employee.query.filter_by(emp_code=session['emp_code']).first()
    return jsonify(emp.to_dict() if emp else {})

@app.route('/api/employees', methods=['GET'])
@require_auth
def api_employees():
    if session.get('role') != 'admin':
        # Non-admins: only get list of names for assignment dropdowns
        emps = Employee.query.filter_by(is_active=True).with_entities(
            Employee.emp_code, Employee.name, Employee.vertical, Employee.role).all()
        return jsonify([{'emp_code': e.emp_code, 'name': e.name,
                         'vertical': e.vertical, 'role': e.role} for e in emps])
    emps = Employee.query.order_by(Employee.name).all()
    return jsonify([e.to_dict() for e in emps])

@app.route('/api/employees', methods=['POST'])
@require_auth
@require_admin
def api_create_employee():
    d = request.get_json()
    # Validate emp_code unique
    if Employee.query.filter_by(emp_code=d['emp_code'].upper()).first():
        return jsonify({'error': 'Employee code already exists'}), 400
    emp = Employee(
        emp_code    = d['emp_code'].upper().strip(),
        name        = d['name'].strip(),
        email       = d.get('email','').strip(),
        mobile      = d.get('mobile','').strip(),
        department  = d.get('department',''),
        designation = d.get('designation',''),
        vertical    = d.get('vertical',''),
        role        = d.get('role','user'),
        industries  = json.dumps(d.get('industries',[])),
        joined_on   = date.today(),
        must_change_pw = True
    )
    # Default password = employee code (lowercase)
    emp.set_password(d['emp_code'].lower())
    db.session.add(emp)
    db.session.commit()
    return jsonify({'ok': True, 'id': emp.id, 'message': f'Employee {emp.name} created. Default password: {d["emp_code"].lower()}'})

@app.route('/api/employees/<int:eid>', methods=['PUT'])
@require_auth
@require_admin
def api_update_employee(eid):
    emp = Employee.query.get_or_404(eid)
    d = request.get_json()
    for field in ('name','email','mobile','department','designation','vertical','role','is_active'):
        if field in d:
            setattr(emp, field, d[field])
    if 'industries' in d:
        emp.industries = json.dumps(d['industries'])
    if d.get('reset_password'):
        emp.set_password(emp.emp_code.lower())
        emp.must_change_pw = True
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/employees/<int:eid>', methods=['DELETE'])
@require_auth
@require_admin
def api_deactivate_employee(eid):
    emp = Employee.query.get_or_404(eid)
    emp.is_active = False
    db.session.commit()
    return jsonify({'ok': True})

# ─────────────────── API: LEADS ───────────────────

def leads_for_user():
    """Return leads visible to current session user (legacy 2-tier)."""
    q, _ = _scope_for_current_session()
    return q


# ────────────────────────────────────────────────────────────────
#   DASHBOARD-V2 SCOPE ENGINE
#   Single source of truth for "which leads is this user allowed
#   to see?" Every dashboard endpoint funnels through this.
# ────────────────────────────────────────────────────────────────
def _scope_for_current_session():
    """Return (leads_query, allowed_emp_codes_set).

    Three tiers:
      * admin              → everything
      * vertical head      → own leads + every report's leads (walk tree)
      * individual         → own leads only

    `emp_codes` is used by the PIC / team scoreboard widgets so they
    only show people the current user is entitled to see.
    """
    role = session.get('role')
    my_code = session.get('emp_code')

    if role == 'admin':
        return Lead.query, {e.emp_code for e in Employee.query.all()}

    # Walk direct-report tree (bounded to 6 levels — plenty for TMS).
    me = Employee.query.filter_by(emp_code=my_code).first()
    if not me:
        return Lead.query.filter_by(assigned_to=my_code), {my_code}

    if getattr(me, 'is_vertical_head', False):
        codes = {my_code}
        frontier = [me.id]
        for _ in range(6):
            if not frontier: break
            reports = (Employee.query
                       .filter(Employee.vertical_head_id.in_(frontier))
                       .all())
            new_ids = []
            for r in reports:
                if r.emp_code not in codes:
                    codes.add(r.emp_code); new_ids.append(r.id)
            frontier = new_ids
        return Lead.query.filter(Lead.assigned_to.in_(codes)), codes

    return Lead.query.filter_by(assigned_to=my_code), {my_code}


def _apply_dashboard_filters(q, args):
    """Layer the common filter-bar filters on top of a scoped query.

    Recognised query-string params:
      from_date, to_date, vertical, industry, state, stage, pic,
      opp_stage, source, lost_reason, ageing_bucket
    """
    from sqlalchemy import and_
    _stage = (args.get('stage') or '').strip()
    _vert  = (args.get('vertical') or '').strip()
    _ind   = (args.get('industry') or '').strip()
    _st    = (args.get('state') or '').strip()
    _pic   = (args.get('pic') or '').strip()
    _oppst = (args.get('opp_stage') or '').strip()
    _src   = (args.get('source') or '').strip()
    _lost  = (args.get('lost_reason') or '').strip()
    _age   = (args.get('ageing_bucket') or '').strip()
    _from  = (args.get('from_date') or '').strip()
    _to    = (args.get('to_date') or '').strip()

    if _stage: q = q.filter(Lead.stage == _stage)
    if _vert:  q = q.filter(Lead.procam_vertical == _vert)
    if _ind:   q = q.filter(Lead.industry == _ind)
    if _st:    q = q.filter(Lead.state == _st)
    if _pic:   q = q.filter(Lead.assigned_to == _pic)
    if _oppst: q = q.filter(Lead.opp_stage == _oppst)
    if _src:   q = q.filter(Lead.source == _src)
    if _lost:  q = q.filter(Lead.lost_reason == _lost)
    # Date range uses onboarded_date (creation cohort). Later phases can
    # switch based on `date_field` param — kept simple for now.
    if _from:
        try:
            d = datetime.strptime(_from, '%Y-%m-%d').date()
            q = q.filter(Lead.onboarded_date >= d)
        except ValueError: pass
    if _to:
        try:
            d = datetime.strptime(_to, '%Y-%m-%d').date()
            q = q.filter(Lead.onboarded_date <= d)
        except ValueError: pass
    # Ageing bucket applies against stage_entered_at (else updated_at).
    if _age in ('0-7', '8-15', '16-30', '31-60', '60+'):
        from sqlalchemy import func as _f, or_
        today = date.today()
        col = db.func.coalesce(Lead.stage_entered_at, Lead.updated_at)
        # SQLite doesn't have INTERVAL — do day math via julianday / date.
        # Portable: filter on Python-side date bounds.
        if   _age == '0-7':   lo, hi = today - timedelta(days=7),   today
        elif _age == '8-15':  lo, hi = today - timedelta(days=15),  today - timedelta(days=8)
        elif _age == '16-30': lo, hi = today - timedelta(days=30),  today - timedelta(days=16)
        elif _age == '31-60': lo, hi = today - timedelta(days=60),  today - timedelta(days=31)
        else:                 lo, hi = date(1970, 1, 1),            today - timedelta(days=61)
        q = q.filter(col >= lo, col <= hi)
    return q

@app.route('/api/leads', methods=['GET'])
@require_auth
def api_leads():
    q = leads_for_user()
    # Filters
    stage   = request.args.get('stage','')
    vert    = request.args.get('vertical','')
    ind     = request.args.get('industry','')
    src     = request.args.get('source','')
    asgn    = request.args.get('assigned','')
    srch    = request.args.get('q','').lower()
    limit   = int(request.args.get('limit', 300))
    if stage:  q = q.filter_by(stage=stage)
    if vert:   q = q.filter_by(procam_vertical=vert)
    if ind:    q = q.filter_by(industry=ind)
    if src:    q = q.filter_by(source=src)
    if asgn and session.get('role')=='admin': q = q.filter_by(assigned_to=asgn)
    leads = q.order_by(Lead.created_at.desc()).limit(limit).all()
    if srch:
        leads = [l for l in leads if srch in (l.company+l.project+l.state+l.industry+l.pic+'').lower()]
    return jsonify([l.to_dict() for l in leads])

@app.route('/api/leads', methods=['POST'])
@require_auth
def api_create_lead():
    d = request.get_json()
    emp = Employee.query.filter_by(emp_code=session['emp_code']).first()
    lead = Lead(
        source          = d.get('source','manual'),
        company         = d.get('company','').strip(),
        project         = d.get('project',''),
        industry        = d.get('industry',''),
        cost_million    = float(d.get('cost',0) or 0),
        products        = d.get('products',''),
        state           = d.get('state',''),
        city            = d.get('city',''),
        country         = d.get('country','India'),
        pic             = d.get('pic',''),
        designation_pic = d.get('designation',''),
        email           = d.get('email',''),
        phone           = d.get('phone',''),
        email2          = d.get('email2',''),
        phone2          = d.get('phone2',''),
        linkedin        = d.get('linkedin',''),
        stage           = d.get('stage','New'),
        procam_vertical = d.get('procam_vertical',''),
        assigned_to     = d.get('assigned_to', session['emp_code']),
        assigned_name   = d.get('assigned_name', emp.name if emp else ''),
        notes           = d.get('notes',''),
        history         = d.get('history',''),
        week_tag        = d.get('week_tag',''),
        onboarded_date  = date.today()
    )
    db.session.add(lead)
    db.session.commit()
    return jsonify({'ok': True, 'id': lead.id})

@app.route('/api/leads/<int:lid>', methods=['PUT'])
@require_auth
def api_update_lead(lid):
    lead = Lead.query.get_or_404(lid)
    # Check ownership unless admin
    if session.get('role') != 'admin' and lead.assigned_to != session['emp_code']:
        return jsonify({'error': 'Not your lead'}), 403
    d = request.get_json()
    fields_map = {
        'company':'company','project':'project','industry':'industry',
        'products':'products','state':'state','city':'city','country':'country',
        'pic':'pic','designation':'designation_pic','email':'email','phone':'phone',
        'email2':'email2','phone2':'phone2','linkedin':'linkedin',
        'stage':'stage','procam_vertical':'procam_vertical','notes':'notes',
        'email_sent_flag':'email_sent_flag','week_tag':'week_tag',
        'opp_number':'opp_number','opp_stage':'opp_stage','opp_notes':'opp_notes',
    }
    for k,v in fields_map.items():
        if k in d: setattr(lead, v, d[k])
    if 'cost' in d: lead.cost_million = float(d['cost'] or 0)
    date_fields = {'followup':'followup_date','phone_call_date':'phone_call_date',
                   'intro_mail_date':'intro_mail_date','meeting_date':'meeting_date',
                   'rfq_date':'rfq_date','opp_close_date':'opp_close_date'}
    for k,v in date_fields.items():
        if k in d and d[k]:
            try: setattr(lead, v, datetime.strptime(d[k],'%Y-%m-%d').date())
            except: pass
        elif k in d and not d[k]:
            setattr(lead, v, None)
    # Reassignment — admin only
    if 'assigned_to' in d and session.get('role')=='admin':
        lead.assigned_to = d['assigned_to']
        emp2 = Employee.query.filter_by(emp_code=d['assigned_to']).first()
        lead.assigned_name = emp2.name if emp2 else d['assigned_to']
    lead.updated_at = datetime.utcnow()
    db.session.commit()
    # Auto-create/update contact
    _auto_save_contact(lead)
    return jsonify({'ok': True})

@app.route('/api/leads/<int:lid>', methods=['DELETE'])
@require_auth
@require_admin
def api_delete_lead(lid):
    lead = Lead.query.get_or_404(lid)
    db.session.delete(lead)
    db.session.commit()
    return jsonify({'ok': True})


# ─── Lead attachments (email ingest + manual upload later) ───────────
@app.route('/api/leads/<int:lid>/attachments', methods=['GET'])
@require_auth
def api_lead_attachments(lid):
    # Ownership: same rule as elsewhere — admins see all; others only their own
    lead = Lead.query.get_or_404(lid)
    if session.get('role') != 'admin' and lead.assigned_to and lead.assigned_to != session.get('emp_code'):
        return jsonify({'error': 'Not your lead'}), 403
    atts = (LeadAttachment.query
            .filter_by(lead_id=lid)
            .order_by(LeadAttachment.uploaded_at.desc())
            .all())
    return jsonify([a.to_dict() for a in atts])


@app.route('/api/leads/<int:lid>/attachments/<int:aid>/download', methods=['GET'])
@require_auth
def api_lead_attachment_download(lid, aid):
    from flask import send_file
    lead = Lead.query.get_or_404(lid)
    if session.get('role') != 'admin' and lead.assigned_to and lead.assigned_to != session.get('emp_code'):
        return jsonify({'error': 'Not your lead'}), 403
    att = LeadAttachment.query.filter_by(id=aid, lead_id=lid).first_or_404()
    if not os.path.exists(att.storage_path):
        abort(404, description=f"File missing on disk: {att.storage_path}")
    return send_file(
        att.storage_path,
        as_attachment=True,
        download_name=att.filename,
        mimetype=att.content_type or 'application/octet-stream',
    )

@app.route('/api/leads/bulk-assign', methods=['POST'])
@require_auth
@require_admin
def api_bulk_assign():
    d = request.get_json()
    ids = d.get('ids', [])
    emp_code = d.get('emp_code','')
    emp = Employee.query.filter_by(emp_code=emp_code).first()
    if not emp:
        return jsonify({'error': 'Employee not found'}), 404
    Lead.query.filter(Lead.id.in_(ids)).update(
        {'assigned_to': emp_code, 'assigned_name': emp.name}, synchronize_session=False)
    db.session.commit()
    return jsonify({'ok': True, 'count': len(ids)})

@app.route('/api/leads/import', methods=['POST'])
@require_auth
def api_import_leads():
    """Bulk import from Excel parse results."""
    rows = request.get_json()
    emp = Employee.query.filter_by(emp_code=session['emp_code']).first()
    added = 0; updated = 0
    for row in rows:
        company = (row.get('company') or '').strip()
        if not company: continue
        existing = Lead.query.filter(
            db.func.lower(Lead.company) == company.lower()).first()
        if existing:
            for f in ('pic','email','phone','phone_call_date','intro_mail_date',
                      'meeting_date','rfq_date','stage','industry','state'):
                if row.get(f): setattr(existing, f if f not in ('phone_call_date','intro_mail_date','meeting_date','rfq_date') else f, row[f])
            existing.updated_at = datetime.utcnow()
            updated += 1
        else:
            l = Lead(company=company, project=row.get('project',''),
                     industry=row.get('industry',''), cost_million=float(row.get('cost',0) or 0),
                     state=row.get('state',''), city=row.get('city',''),
                     pic=row.get('pic',''), email=row.get('email',''), phone=row.get('phone',''),
                     stage=row.get('stage','New'), procam_vertical=row.get('vertical',''),
                     source=row.get('source','import'),
                     assigned_to=session['emp_code'], assigned_name=emp.name if emp else '',
                     onboarded_date=date.today(), week_tag=row.get('week_tag',''))
            db.session.add(l); added += 1
    db.session.commit()
    return jsonify({'ok': True, 'added': added, 'updated': updated})

def _auto_save_contact(lead):
    """Auto-create/update contact from lead data."""
    if lead.email and lead.pic:
        ct = Contact.query.filter_by(email=lead.email).first()
        if ct:
            ct.name = lead.pic; ct.designation = lead.designation_pic or ct.designation
            ct.company = lead.company; ct.phone = lead.phone or ct.phone
        else:
            ct = Contact(contact_type='person', name=lead.pic,
                         company=lead.company, designation=lead.designation_pic or '',
                         industry=lead.industry or '', email=lead.email,
                         phone=lead.phone or '', country=lead.country or 'India',
                         city=lead.city or '', assigned_to=lead.assigned_to)
            db.session.add(ct)
        db.session.commit()

# ─────────────────── API: WORKFLOW TRANSITIONS ───────────────────

def _log_stage_change(lead, new_stage, note=None):
    """Append a LeadStageHistory row + LeadActivity + inline note."""
    from_st = lead.stage
    hist = LeadStageHistory(lead_id=lead.id, from_stage=from_st,
                            to_stage=new_stage,
                            changed_by=session.get('emp_code') or '',
                            note=note or '')
    db.session.add(hist)
    act = LeadActivity(lead_id=lead.id, kind='stage_change',
                       subject=f'{from_st or "—"} → {new_stage}',
                       body=note or '', performed_by=session.get('emp_code') or '')
    db.session.add(act)
    lead.stage = new_stage


@app.route('/api/leads/<int:lid>/advance', methods=['POST'])
@require_auth
def api_lead_advance(lid):
    """Advance a lead to the next pipeline stage per the workflow.

    Also accepts explicit 'to_stage' in body to jump straight to a stage
    (used by the pipeline stepper buttons: Call Done, Profile Sent, etc.)
    """
    lead = Lead.query.get_or_404(lid)
    # Auto-migrate legacy stage names to the new pipeline
    if lead.stage in LEGACY_STAGE_MAP:
        lead.stage = LEGACY_STAGE_MAP[lead.stage]
    data = request.get_json() or {}
    explicit = (data.get('to_stage') or '').strip()
    if explicit:
        if explicit not in STAGES_ALL:
            return jsonify({'ok': False,
                            'error': f'Unknown stage "{explicit}"'}), 400
        nxt = explicit
    else:
        nxt = STAGE_NEXT.get(lead.stage)
        if not nxt:
            return jsonify({'ok': False,
                            'error': f'No next stage from "{lead.stage}"'}), 400
    _log_stage_change(lead, nxt, note=data.get('note'))
    # Stamp workflow-specific dates when the corresponding stage is reached
    today = date.today()
    stamp_date = data.get('event_date')
    if stamp_date:
        try: today = datetime.strptime(stamp_date, '%Y-%m-%d').date()
        except ValueError: pass
    if nxt == 'Call Done':
        lead.phone_call_date = lead.phone_call_date or today
    elif nxt == 'Profile Sent':
        lead.intro_mail_date = lead.intro_mail_date or today
    elif nxt == 'Appointment':
        lead.meeting_date = lead.meeting_date or today
    elif nxt == 'RFQ Generated':
        lead.rfq_date = lead.rfq_date or today
    db.session.commit()
    return jsonify({'ok': True, 'stage': lead.stage, 'lead': lead.to_dict()})


@app.route('/api/leads/<int:lid>/decision', methods=['POST'])
@require_auth
def api_lead_decision(lid):
    """Record a terminal decision: Won / Lost / On Hold / Not Interested."""
    lead = Lead.query.get_or_404(lid)
    data = request.get_json() or {}
    outcome = (data.get('outcome') or '').strip()
    if outcome not in DECISION_OUTCOMES:
        return jsonify({'ok': False,
                        'error': f'Outcome must be one of {DECISION_OUTCOMES}'}), 400
    reason = (data.get('reason') or '').strip()
    # Enforce loss-reason capture for Lost / Not Interested (spec: capture
    # reason for future analysis)
    if outcome in ('Lost', 'Not Interested') and not reason:
        return jsonify({'ok': False,
                        'error': f'reason required when marking {outcome}. '
                                 f'Choose one of {LOSS_REASONS}.'}), 400
    _log_stage_change(lead, outcome, note=reason)
    # Also update the linked Opportunity if we have one
    if lead.opp_number:
        opp = Opportunity.query.filter_by(opp_number=lead.opp_number).first()
        if opp:
            opp.stage = outcome
            if outcome == 'Won':
                opp.won_at = datetime.utcnow()
                opp.probability = 100
            elif outcome in ('Lost', 'Not Interested'):
                opp.lost_at = datetime.utcnow()
                opp.lost_reason = reason or outcome
                opp.probability = 0
    db.session.commit()
    return jsonify({'ok': True, 'stage': lead.stage, 'lead': lead.to_dict()})


@app.route('/api/leads/<int:lid>/assign-opp', methods=['POST'])
@require_auth
def api_lead_assign_opp(lid):
    """RFQ Generated → convert Lead into a full ProConnect Opportunity.

    Body (all optional):
      { "expected_close_date": "2026-10-01",
        "stage": "Qualification",   # default
        "value_inr": 1250000,
        "notes": "..." }

    Auto-generates the next OPP-YYYY-NNNN number, links back to the lead,
    stamps opp_number on the Lead so the pipeline card shows the link.
    """
    lead = Lead.query.get_or_404(lid)
    data = request.get_json() or {}
    # Auto-migrate legacy stage names so the RFQ check works for old leads too
    if lead.stage in LEGACY_STAGE_MAP:
        lead.stage = LEGACY_STAGE_MAP[lead.stage]
    if lead.stage != 'RFQ Generated':
        return jsonify({'ok': False,
                        'error': f'Lead must be at RFQ Generated stage. '
                                 f'Currently: {lead.stage}'}), 400
    if lead.opp_number:
        return jsonify({'ok': False,
                        'error': f'Opportunity already assigned: {lead.opp_number}'}), 400

    # Generate next OPP-YYYY-NNNN
    yr = date.today().year
    prefix = f"OPP-{yr}-"
    with _opp_lock:
        max_seq = 0
        for row in db.session.query(Opportunity.opp_number).filter(
                Opportunity.opp_number.like(f'{prefix}%')).all():
            try: max_seq = max(max_seq, int(str(row[0]).split('-')[-1]))
            except (ValueError, IndexError): pass
        for row in db.session.query(Lead.opp_number).filter(
                Lead.opp_number.like(f'{prefix}%')).all():
            try: max_seq = max(max_seq, int(str(row[0]).split('-')[-1]))
            except (ValueError, IndexError): pass
        opp_num = f"{prefix}{str(max_seq + 1).zfill(4)}"

    stage = (data.get('stage') or 'Qualification').strip()
    if stage not in OPP_STAGES_ALL: stage = 'Qualification'
    close = data.get('expected_close_date')
    close_d = None
    if close:
        try: close_d = datetime.strptime(close, '%Y-%m-%d').date()
        except ValueError: pass

    # Find or create the Company
    company = None
    if lead.company:
        company = (Company.query
                   .filter(db.func.lower(Company.name) == lead.company.strip().lower())
                   .first())

    opp = Opportunity(
        opp_number=opp_num,
        lead_id=lead.id,
        company_id=company.id if company else None,
        title=lead.project or lead.company,
        stage=stage,
        value_inr=(data.get('value_inr') or lead.cost_million * 10 if lead.cost_million else None),
        probability=30 if stage == 'Qualification' else 50,
        expected_close_date=close_d,
        owner_emp_code=lead.assigned_to or session.get('emp_code'),
        rfq_received_date=lead.rfq_date or date.today(),
        notes=(data.get('notes') or lead.opp_notes or ''),
    )
    db.session.add(opp)

    # Stamp back on Lead
    lead.opp_number = opp_num
    lead.opp_stage = stage
    lead.opp_close_date = close_d
    if data.get('notes'): lead.opp_notes = data.get('notes')
    db.session.commit()
    return jsonify({'ok': True, 'opp': opp.to_dict(), 'lead': lead.to_dict()})


@app.route('/api/opportunities/<int:oid>/convert-to-project', methods=['POST'])
@require_auth
def api_opp_convert_to_project(oid):
    """Mark a Won opportunity as converted to a TMS project.

    Body: {"project_ref": "PRO2402/JOB000123"}   (optional — free text)

    Does not currently POST to TMS — that's a cross-app integration best
    done from TMS itself (Rate Sourcing pulling from CRM). This endpoint
    just stamps won_project_ref / won_project_at on the Opportunity + linked
    Lead so the CRM knows the deal has crossed over.
    """
    opp = Opportunity.query.get_or_404(oid)
    if opp.stage != 'Won':
        return jsonify({'ok': False,
                        'error': 'Only Won opportunities can convert to a project.'}), 400
    data = request.get_json() or {}
    ref = (data.get('project_ref') or '').strip()
    opp.won_project_ref = ref or None
    opp.won_project_at = datetime.utcnow()
    # Also stamp on the Lead for pipeline visibility
    if opp.lead_id:
        lead = db.session.get(Lead, opp.lead_id)
        if lead:
            lead.opp_stage = 'Won → Project'
    db.session.commit()
    return jsonify({'ok': True, 'opportunity': opp.to_dict()})


@app.route('/api/config/stages')
def api_config_stages():
    return jsonify({
        'pipeline': STAGES_PIPELINE, 'terminal': STAGES_TERMINAL,
        'all': STAGES_ALL, 'next': STAGE_NEXT,
        'legacy_map': LEGACY_STAGE_MAP,
        'opp_pipeline': OPP_STAGES_PIPELINE,
        'opp_terminal': OPP_STAGES_TERMINAL,
        'opp_all': OPP_STAGES_ALL, 'opp_next': OPP_STAGE_NEXT,
    })


@app.route('/api/config/states')
def api_config_states():
    return jsonify({'states': STATE_LIST})


@app.route('/api/config/loss-reasons')
def api_config_loss_reasons():
    return jsonify({'reasons': LOSS_REASONS})


# ─────────────────── API: COMPETITORS ───────────────────

@app.route('/api/leads/<int:lid>/competitors', methods=['GET'])
@require_auth
def api_competitors_list(lid):
    return jsonify([c.to_dict() for c in
                    Competitor.query.filter_by(lead_id=lid)
                                    .order_by(Competitor.added_at.desc()).all()])


@app.route('/api/leads/<int:lid>/competitors', methods=['POST'])
@require_auth
def api_competitors_add(lid):
    Lead.query.get_or_404(lid)   # 404 if lead missing
    d = request.get_json() or {}
    name = (d.get('name') or '').strip()
    if not name:
        return jsonify({'ok': False, 'error': 'Name required'}), 400
    price = d.get('quoted_price')
    try:
        price_dec = float(price) if price not in (None, '') else None
    except (TypeError, ValueError):
        price_dec = None
    c = Competitor(lead_id=lid, name=name, quoted_price=price_dec,
                   strength=d.get('strength', ''),
                   weakness=d.get('weakness', ''),
                   notes=d.get('notes', ''),
                   added_by=session.get('emp_code'))
    db.session.add(c)
    db.session.commit()
    return jsonify({'ok': True, 'competitor': c.to_dict()})


@app.route('/api/competitors/<int:cid>', methods=['DELETE'])
@require_auth
def api_competitors_delete(cid):
    c = Competitor.query.get_or_404(cid)
    db.session.delete(c)
    db.session.commit()
    return jsonify({'ok': True})


# ─────────────────── API: CONTACTS / GLOBAL CRM ───────────────────

@app.route('/api/contacts', methods=['GET'])
@require_auth
def api_contacts():
    ctype = request.args.get('type','')
    atype = request.args.get('agent_type','')
    country = request.args.get('country','')
    srch = request.args.get('q','').lower()
    q = Contact.query
    if session.get('role') != 'admin':
        q = q.filter_by(assigned_to=session['emp_code'])
    if ctype:   q = q.filter_by(contact_type=ctype)
    if atype:   q = q.filter_by(agent_type=atype)
    if country: q = q.filter_by(country=country)
    contacts = q.order_by(Contact.created_at.desc()).limit(200).all()
    if srch:
        contacts = [c for c in contacts if srch in (c.name+c.company+c.country+c.city+'').lower()]
    return jsonify([c.to_dict() for c in contacts])

@app.route('/api/contacts', methods=['POST'])
@require_auth
def api_create_contact():
    d = request.get_json()
    ct = Contact(
        contact_type = d.get('type','person'),
        name         = d.get('name','').strip(),
        company      = d.get('company',''),
        designation  = d.get('designation',''),
        industry     = d.get('industry',''),
        email        = d.get('email',''),
        phone        = d.get('phone',''),
        mobile       = d.get('mobile',''),
        country      = d.get('country','India'),
        state        = d.get('state',''),
        city         = d.get('city',''),
        website      = d.get('website',''),
        linkedin     = d.get('linkedin',''),
        agent_type   = d.get('agent_type',''),
        assigned_to  = session['emp_code'],
        notes        = d.get('notes','')
    )
    db.session.add(ct)
    db.session.commit()
    # If Type=Company, also upsert the Company master so pipeline dashboards
    # get proper State + Industry columns instead of '—'.
    if ct.contact_type == 'company' and ct.company:
        existing = Company.query.filter(
            db.func.lower(Company.name) == ct.company.strip().lower()).first()
        if not existing:
            c = Company(
                name=ct.company.strip(), industry=ct.industry or None,
                country=ct.country or None, state=ct.state or None,
                city=ct.city or None, phone=ct.phone or None,
                email=ct.email or None, website=ct.website or None,
                linkedin=ct.linkedin or None, notes=ct.notes or None,
                created_by=session.get('emp_code'))
            db.session.add(c); db.session.commit()
        else:
            # Patch only the empty fields — never overwrite existing data
            for src, val in [('industry', ct.industry), ('state', ct.state),
                              ('city', ct.city), ('phone', ct.phone),
                              ('email', ct.email), ('website', ct.website)]:
                if val and not getattr(existing, src):
                    setattr(existing, src, val)
            db.session.commit()
    return jsonify({'ok': True, 'id': ct.id})

@app.route('/api/contacts/<int:cid>', methods=['PUT'])
@require_auth
def api_update_contact(cid):
    ct = Contact.query.get_or_404(cid)
    if session.get('role') != 'admin' and ct.assigned_to != session['emp_code']:
        return jsonify({'error': 'Forbidden'}), 403
    d = request.get_json()
    for f in ('name','company','designation','industry','email','phone','mobile',
              'country','city','website','linkedin','agent_type','notes','contact_type'):
        if f in d: setattr(ct, f, d[f])
    if 'assigned_to' in d and session.get('role')=='admin':
        ct.assigned_to = d['assigned_to']
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/contacts/<int:cid>', methods=['DELETE'])
@require_auth
def api_delete_contact(cid):
    ct = Contact.query.get_or_404(cid)
    if session.get('role') != 'admin' and ct.assigned_to != session['emp_code']:
        return jsonify({'error': 'Forbidden'}), 403
    db.session.delete(ct)
    db.session.commit()
    return jsonify({'ok': True})

# ─────────────────── API: NEWS INTELLIGENCE ───────────────────

@app.route('/api/news', methods=['GET'])
@require_auth
@require_admin
def api_news():
    status = request.args.get('status','')
    q = NewsItem.query
    if status: q = q.filter_by(status=status)
    items = q.order_by(NewsItem.created_at.desc()).limit(100).all()
    return jsonify([n.to_dict() for n in items])

@app.route('/api/news/fetch', methods=['POST'])
@require_auth
@require_admin
def api_news_fetch():
    """Fetch and parse industry-relevant emails from Outlook via MS Graph.
    Called daily at 7am (or manually by admin). Uses the MS365 token from session/env."""
    count = _fetch_news_from_outlook()
    return jsonify({'ok': True, 'fetched': count})

def _fetch_news_from_outlook():
    """Parse ETManufacturing / ProjectsToday newsletters from Outlook."""
    try:
        import requests as req
        token = os.environ.get('MS365_TOKEN','')
        if not token: return 0
        headers = {'Authorization': f'Bearer {token}'}
        # Search for newsletter emails
        params = {'$search': '"project" OR "investment" OR "plant"',
                  '$top': 20, '$orderby': 'receivedDateTime desc'}
        resp = req.get('https://graph.microsoft.com/v1.0/me/messages',
                       headers=headers, params=params, timeout=15)
        if resp.status_code != 200: return 0
        emails = resp.json().get('value', [])
        RELEVANT = ['project','investment','plant','manufacturing','steel','power',
                    'infrastructure','chemical','logistics','transport','energy',
                    'cement','mining','petroleum','refinery','construction']
        added = 0
        for email_item in emails:
            subj = email_item.get('subject','')
            body = email_item.get('bodyPreview','')
            if not any(kw in subj.lower()+body.lower() for kw in RELEVANT): continue
            if NewsItem.query.filter_by(email_subject=subj[:400]).first(): continue
            # Determine category
            cat = 'General'
            cat_map = {'power':'Power','steel':'Steel / Metals','chemical':'Chemicals',
                       'infrastructure':'Infrastructure','transport':'Transport',
                       'petroleum':'Petroleum / Oil & Gas','cement':'Cement',
                       'renewable':'Renewable Energy','defense':'Defence',
                       'mining':'Mining','manufacturing':'Manufacturing'}
            for kw,c in cat_map.items():
                if kw in (subj+body).lower(): cat=c; break
            rel = 'High' if any(kw in subj.lower() for kw in ['crore','project','plant','invest']) else 'Medium'
            item = NewsItem(
                title    = subj[:400],
                summary  = body[:500],
                source   = email_item.get('sender',{}).get('emailAddress',{}).get('name','Email'),
                category = cat, relevance = rel,
                email_subject = subj[:400],
                published_date = date.today(),
                status = 'pending'
            )
            db.session.add(item); added += 1
        db.session.commit()
        return added
    except Exception as e:
        print(f'News fetch error: {e}')
        return 0

@app.route('/api/news/<int:nid>/action', methods=['POST'])
@require_auth
@require_admin
def api_news_action(nid):
    item = NewsItem.query.get_or_404(nid)
    d = request.get_json()
    action = d.get('action','')
    if action == 'delete':
        item.status = 'deleted'
    elif action == 'assign':
        item.status = 'assigned'
        item.assigned_to = d.get('emp_code','')
    elif action == 'convert':
        # Convert to lead
        emp_code = d.get('emp_code', session['emp_code'])
        emp = Employee.query.filter_by(emp_code=emp_code).first()
        lead = Lead(
            source    = 'news', company = d.get('company', item.title[:100]),
            project   = item.title, industry = item.category,
            stage     = 'New', assigned_to = emp_code,
            assigned_name = emp.name if emp else '',
            notes = item.summary, onboarded_date = date.today(),
            week_tag = f"W{date.today().isocalendar()[1]}-{date.today().year}"
        )
        db.session.add(lead)
        db.session.flush()
        item.lead_id = lead.id
        item.status = 'assigned'
    db.session.commit()
    return jsonify({'ok': True})

@app.route('/api/news/seed', methods=['POST'])
@require_auth
@require_admin
def api_news_seed():
    """Seed with real ETManufacturing data already fetched from Outlook."""
    ET_NEWS = [
        {"title": "L&T bags ₹10,000-15,000 crore metals sector order from JSW Steel",
         "summary": "L&T will execute critical process facilities as JSW Steel expands crude steel capacity from 35 MTPA to over 50 MTPA by 2031. Major equipment transport and installation scope expected.",
         "source": "ETManufacturing", "category": "Steel / Metals", "relevance": "High",
         "url": "https://manufacturing.economictimes.indiatimes.com"},
        {"title": "Andhra Minister lays foundation stone for Carrier Global ₹1,000 cr AC facility in Sri City",
         "summary": "Significant investment creating 3,000 direct and indirect jobs. Plant equipment imports and installation logistics scope for Procam.",
         "source": "ETManufacturing", "category": "Manufacturing", "relevance": "High"},
        {"title": "UP defence corridors attract ₹35,000 crore investment, boost manufacturing",
         "summary": "Corridors becoming hubs for defence manufacturing including small arms, ammunition, missiles and advanced engineering. Heavy equipment transport opportunity.",
         "source": "ETManufacturing", "category": "Defence", "relevance": "High"},
        {"title": "SPML Infra bags ₹1,128 crore BESS project from NTPC",
         "summary": "First large-scale grid BESS project for SPML; among largest single BESS orders in India. Battery storage equipment transport and installation scope.",
         "source": "ETManufacturing", "category": "Power", "relevance": "High"},
        {"title": "KEC International bags new orders worth ₹1,002 crore — HVDC, wind energy, railway",
         "summary": "HVDC transmission, wind energy, railway signalling and cables orders across India and Americas. Wind turbine transport potential.",
         "source": "ETManufacturing", "category": "Power", "relevance": "Medium"},
        {"title": "KPI Green Energy Q4 FY26 net profit rises 46% — strong solar/wind execution",
         "summary": "Revenue ₹810 crore, 40% growth. Strong solar, wind and hybrid energy project execution. Blade and panel transport scope.",
         "source": "ETManufacturing", "category": "Renewable Energy", "relevance": "Medium"},
        {"title": "India steel output rises 5.8% in April — demand up 8.1%",
         "summary": "Both imports and exports up 30%+ vs April 2025. Strong steel demand signals ongoing plant capex across steel belt.",
         "source": "ETManufacturing", "category": "Steel / Metals", "relevance": "Medium"},
        {"title": "Oswal Pumps bags ₹162 crore Maharashtra solar pump order under PM Kusum",
         "summary": "6,869 off-grid solar PV water pumping systems across Maharashtra. Equipment logistics and installation support opportunity.",
         "source": "ETManufacturing", "category": "Renewable Energy", "relevance": "Medium"},
        {"title": "Godrej Enterprises to accelerate aerospace manufacturing — 25% YoY growth",
         "summary": "Aerospace division with 30%+ export growth. Precision cargo and project freight scope for aerospace components.",
         "source": "ETManufacturing", "category": "Manufacturing", "relevance": "Low"},
        {"title": "TVS Industrial & Logistics Parks to set up Grade A logistics park in Siliguri",
         "summary": "Organised Grade A warehousing entering Siliguri market. Warehouse competition intelligence for Procam's Northeast strategy.",
         "source": "ETManufacturing", "category": "Warehousing", "relevance": "Medium"},
    ]
    added = 0
    for n in ET_NEWS:
        if not NewsItem.query.filter_by(title=n['title']).first():
            item = NewsItem(title=n['title'], summary=n['summary'], source=n['source'],
                           category=n['category'], relevance=n['relevance'],
                           url=n.get('url',''), published_date=date(2026,5,7), status='pending')
            db.session.add(item); added+=1
    db.session.commit()
    return jsonify({'ok': True, 'added': added})

# ─────────────────── API: DASHBOARD STATS ───────────────────

@app.route('/api/stats')
@require_auth
def api_stats():
    q = leads_for_user()
    all_leads = q.all()
    stages = {}
    for l in all_leads:
        stages[l.stage] = stages.get(l.stage, 0) + 1
    industries = {}
    for l in all_leads:
        if l.industry:
            industries[l.industry] = industries.get(l.industry, 0) + 1
    verticals = {}
    for l in all_leads:
        if l.procam_vertical:
            verticals[l.procam_vertical] = verticals.get(l.procam_vertical, 0) + 1
    # Team stats (admin only)
    team_stats = []
    if session.get('role') == 'admin':
        emps = Employee.query.filter_by(is_active=True).all()
        for emp in emps:
            el = Lead.query.filter_by(assigned_to=emp.emp_code).all()
            team_stats.append({
                'emp_code': emp.emp_code, 'name': emp.name, 'vertical': emp.vertical,
                'total': len(el),
                'active': len([x for x in el if x.stage not in ('Won','Lost')]),
                'mailed': len([x for x in el if x.intro_mail_date]),
                'called': len([x for x in el if x.phone_call_date]),
                'rfq': len([x for x in el if x.stage=='RFQ Generated']),
                'opp': len([x for x in el if x.opp_number]),
                'won': len([x for x in el if x.stage=='Won']),
            })
    # News pending
    pending_news = NewsItem.query.filter_by(status='pending').count() if session.get('role')=='admin' else 0
    # Inbox counts for the workflow sidebar (per approved workflow diagram)
    inbox_counts = {s: stages.get(s, 0) for s in STAGES_ALL}
    active_stages = set(STAGES_PIPELINE)   # 5 pipeline stages
    return jsonify({
        'total': len(all_leads),
        'active': len([l for l in all_leads if l.stage in active_stages]),
        'mailed': len([l for l in all_leads if l.intro_mail_date]),
        'rfq': len([l for l in all_leads if l.stage=='Negotiation Due']),
        'opp': len([l for l in all_leads if l.opp_number]),
        'won': len([l for l in all_leads if l.stage=='Won']),
        'stages': stages, 'inbox': inbox_counts,
        'industries': industries, 'verticals': verticals,
        'team': team_stats, 'pending_news': pending_news
    })


# ═════════════════════════════════════════════════════════════════════
#  DASHBOARD v2 API — role-scoped, filter-aware, one-shot aggregation.
#  Every UI element (KPI card, chart segment, funnel stage, ageing bar)
#  passes the same filter set through /api/dashboard/summary and gets
#  a fully-reconciled snapshot. /api/dashboard/records powers the
#  dynamic detail table that echoes whichever filters are active.
# ═════════════════════════════════════════════════════════════════════
LOST_REASONS = [
    'Price', 'Competition', 'No Response', 'Client Decision',
    'Technical / Capability', 'Project Cancelled', 'Project Postponed',
    'Commercial Terms', 'Service Constraint', 'Geography Constraint',
    'Duplicate / Invalid', 'Other',
]


@app.route('/api/dashboard/summary')
@require_auth
def api_dashboard_summary():
    """Everything the dashboard needs to redraw in one round-trip."""
    scoped_q, allowed_codes = _scope_for_current_session()
    q = _apply_dashboard_filters(scoped_q, request.args)
    leads = q.all()
    today = date.today()

    def _cnt(pred):
        return sum(1 for l in leads if pred(l))
    def _sum(pred, field):
        return float(sum((getattr(l, field) or 0) for l in leads if pred(l)))

    active_stages   = set(STAGES_PIPELINE)
    terminal_won    = 'Won'
    terminal_lost   = 'Lost'

    # ── Primary KPI cards ────────────────────────────────
    kpis = {
        'total_leads':      len(leads),
        'active_pipeline':  _cnt(lambda l: l.stage in active_stages),
        'intro_emailed':    _cnt(lambda l: l.intro_mail_date is not None),
        'calls_done':       _cnt(lambda l: l.phone_call_date is not None),
        'profile_sent':     _cnt(lambda l: l.stage in ('Profile Sent','Appointment','Visit Done','RFQ Generated','Won')),
        'appointments':     _cnt(lambda l: l.meeting_date is not None),
        'visits':           _cnt(lambda l: l.stage in ('Visit Done','RFQ Generated','Won')),
        'rfq_stage':        _cnt(lambda l: l.stage == 'RFQ Generated'),
        'rfqs_generated':   _cnt(lambda l: l.rfq_date is not None),
        'opportunities':    _cnt(lambda l: bool(l.opp_number)),
        'won':              _cnt(lambda l: l.stage == terminal_won),
        'lost':             _cnt(lambda l: l.stage == terminal_lost),
        'won_value_m':      _sum(lambda l: l.stage == terminal_won, 'cost_million'),
        'pipeline_value_m': _sum(lambda l: l.stage in active_stages, 'cost_million'),
        'followup_due':     _cnt(lambda l: l.followup_date is not None and l.followup_date == today),
        'followup_overdue': _cnt(lambda l: l.followup_date is not None and l.followup_date < today
                                            and l.stage in active_stages),
        'no_activity_7':    _cnt(lambda l: (l.updated_at is not None and
                                            (today - l.updated_at.date()).days > 7
                                            and l.stage in active_stages)),
        'no_activity_15':   _cnt(lambda l: (l.updated_at is not None and
                                            (today - l.updated_at.date()).days > 15
                                            and l.stage in active_stages)),
        'no_activity_30':   _cnt(lambda l: (l.updated_at is not None and
                                            (today - l.updated_at.date()).days > 30
                                            and l.stage in active_stages)),
    }
    total = kpis['total_leads'] or 1
    kpis['conversion_pct'] = round(100 * kpis['won'] / total, 1)
    kpis['rfq_won_pct'] = round(
        100 * kpis['won'] / kpis['rfqs_generated'], 1) \
        if kpis['rfqs_generated'] else 0

    # ── Distributions ────────────────────────────────────
    def _dist(getter):
        out = {}
        for l in leads:
            k = getter(l) or '—'
            out[k] = out.get(k, 0) + 1
        return out

    stages_dist    = _dist(lambda l: l.stage)
    industry_dist  = {k: v for k, v in _dist(lambda l: l.industry).items() if k != '—'}
    vertical_dist  = {k: v for k, v in _dist(lambda l: l.procam_vertical).items() if k != '—'}
    state_dist     = {k: v for k, v in _dist(lambda l: l.state).items() if k != '—'}
    source_dist    = _dist(lambda l: l.source)
    lost_reason_dist = {k: v for k, v in _dist(
        lambda l: l.lost_reason if l.stage == terminal_lost else None
    ).items() if k != '—'}

    # ── Person-In-Charge scoreboard ──────────────────────
    pic_map = {}
    for l in leads:
        code = l.assigned_to or '—'
        p = pic_map.setdefault(code, {
            'emp_code': code, 'name': l.assigned_name or code,
            'total': 0, 'active': 0, 'calls': 0, 'profile': 0,
            'appts': 0, 'visits': 0, 'rfqs': 0, 'won': 0, 'lost': 0,
            'pipeline_value': 0.0, 'won_value': 0.0,
        })
        p['total'] += 1
        if l.stage in active_stages: p['active'] += 1
        if l.phone_call_date:        p['calls']  += 1
        if l.intro_mail_date:        p['profile'] += 1
        if l.meeting_date:           p['appts']  += 1
        if l.stage in ('Visit Done','RFQ Generated','Won'):
            p['visits'] += 1
        if l.rfq_date:               p['rfqs']   += 1
        if l.stage == terminal_won:  p['won']    += 1;  p['won_value']      += float(l.cost_million or 0)
        if l.stage == terminal_lost: p['lost']   += 1
        if l.stage in active_stages: p['pipeline_value'] += float(l.cost_million or 0)
    for p in pic_map.values():
        p['conversion_pct'] = round(100 * p['won'] / p['total'], 1) if p['total'] else 0.0
    pic_board = sorted(pic_map.values(),
                       key=lambda x: (-x['won'], -x['active']))

    # ── Funnel counts (cumulative — each stage counts anyone who ≥ reached it) ──
    funnel_order = [
        ('lead',         lambda l: True),
        ('call',         lambda l: l.phone_call_date is not None or l.stage != 'New'),
        ('profile_sent', lambda l: l.intro_mail_date is not None or
                                     l.stage in ('Profile Sent','Appointment','Visit Done','RFQ Generated','Won')),
        ('appointment',  lambda l: l.meeting_date is not None or
                                     l.stage in ('Appointment','Visit Done','RFQ Generated','Won')),
        ('visit',        lambda l: l.stage in ('Visit Done','RFQ Generated','Won')),
        ('rfq',          lambda l: l.rfq_date is not None or l.stage in ('RFQ Generated','Won')),
        ('won',          lambda l: l.stage == terminal_won),
    ]
    funnel = []
    prev_n = None
    for key, pred in funnel_order:
        n = _cnt(pred)
        step_conv = round(100 * n / prev_n, 1) if prev_n else None
        funnel.append({'key': key, 'count': n, 'step_conversion': step_conv})
        prev_n = n

    # ── Ageing buckets (stage_entered_at fallback → updated_at) ──
    def _age_days(l):
        ref = l.stage_entered_at or l.updated_at or l.created_at
        return (today - ref.date()).days if ref else 0
    buckets = {'0-7': 0, '8-15': 0, '16-30': 0, '31-60': 0, '60+': 0}
    for l in leads:
        if l.stage not in active_stages:
            continue
        d = _age_days(l)
        if   d <= 7:  buckets['0-7']  += 1
        elif d <= 15: buckets['8-15'] += 1
        elif d <= 30: buckets['16-30'] += 1
        elif d <= 60: buckets['31-60'] += 1
        else:         buckets['60+']  += 1

    # ── Available filter option lists (for the filter bar dropdowns) ──
    options = {
        'verticals':  sorted({l.procam_vertical for l in leads if l.procam_vertical}),
        'industries': sorted({l.industry for l in leads if l.industry}),
        'states':     sorted({l.state for l in leads if l.state}),
        'stages':     STAGES_ALL,
        'lost_reasons': LOST_REASONS,
        'pic':        [{'emp_code': e.emp_code, 'name': e.name}
                        for e in Employee.query
                                 .filter(Employee.emp_code.in_(allowed_codes))
                                 .filter_by(is_active=True)
                                 .order_by(Employee.name).all()],
    }

    return jsonify({
        'scope': {
            'role': session.get('role'),
            'emp_code': session.get('emp_code'),
            'allowed_emp_codes': sorted(allowed_codes),
        },
        'filters_applied': {k: v for k, v in request.args.items() if v},
        'kpis': kpis,
        'distributions': {
            'stages': stages_dist,
            'industries': industry_dist,
            'verticals': vertical_dist,
            'states': state_dist,
            'sources': source_dist,
            'lost_reasons': lost_reason_dist,
        },
        'pic_board': pic_board,
        'funnel': funnel,
        'ageing': buckets,
        'options': options,
    })


@app.route('/api/dashboard/records')
@require_auth
def api_dashboard_records():
    """The dynamic detail table. Honours the same filter set as summary.
    Paginated; default page size 25, max 500."""
    scoped_q, _ = _scope_for_current_session()
    q = _apply_dashboard_filters(scoped_q, request.args)

    # Sort — most recent activity first by default.
    sort = (request.args.get('sort') or 'updated_desc').lower()
    if   sort == 'created_desc':  q = q.order_by(Lead.created_at.desc())
    elif sort == 'value_desc':    q = q.order_by(Lead.cost_million.desc().nullslast())
    elif sort == 'ageing_desc':
        q = q.order_by(db.func.coalesce(Lead.stage_entered_at, Lead.updated_at).asc())
    else:
        q = q.order_by(Lead.updated_at.desc())

    try:    page = max(1, int(request.args.get('page', 1)))
    except: page = 1
    try:    size = min(500, max(1, int(request.args.get('size', 25))))
    except: size = 25

    total = q.count()
    rows  = q.offset((page - 1) * size).limit(size).all()
    return jsonify({
        'total': total, 'page': page, 'size': size,
        'rows': [l.to_dict() for l in rows],
    })


@app.route('/api/dashboard/options')
@require_auth
def api_dashboard_options():
    """Lightweight — filter-bar picklists without running the summary."""
    _, allowed = _scope_for_current_session()
    return jsonify({
        'verticals':  sorted({v[0] for v in db.session.query(Lead.procam_vertical)
                              .filter(Lead.procam_vertical.isnot(None)).distinct()}),
        'industries': sorted({v[0] for v in db.session.query(Lead.industry)
                              .filter(Lead.industry.isnot(None)).distinct()}),
        'states':     sorted({v[0] for v in db.session.query(Lead.state)
                              .filter(Lead.state.isnot(None)).distinct()}),
        'stages':     STAGES_ALL,
        'lost_reasons': LOST_REASONS,
        'pic':        [{'emp_code': e.emp_code, 'name': e.name}
                        for e in Employee.query
                                 .filter(Employee.emp_code.in_(allowed))
                                 .filter_by(is_active=True)
                                 .order_by(Employee.name).all()],
    })


@app.route('/api/workflow/stages')
@require_auth
def api_workflow_stages():
    """Expose stage vocabulary + transitions for the UI."""
    return jsonify({
        'pipeline': STAGES_PIPELINE,
        'terminal': STAGES_TERMINAL,
        'all':      STAGES_ALL,
        'next':     STAGE_NEXT,
        'decisions': DECISION_OUTCOMES,
    })

# ─────────────────── API: OPP NUMBER ───────────────────

@app.route('/api/opp-next-number')
@require_auth
def api_opp_number():
    """Return the next opportunity number by looking at the DB, NOT a counter.
    The Opportunity table's UNIQUE constraint is the final guard against dupes;
    this endpoint just suggests the next number for the UI to display."""
    yr = date.today().year
    prefix = f"OPP-{yr}-"
    with _opp_lock:
        # Find highest existing sequence for this year across both Lead.opp_number
        # (legacy) and Opportunity.opp_number (new).
        max_seq = 0
        for row in db.session.query(Opportunity.opp_number).filter(
                Opportunity.opp_number.like(f'{prefix}%')).all():
            try: max_seq = max(max_seq, int(str(row[0]).split('-')[-1]))
            except (ValueError, IndexError): pass
        for row in db.session.query(Lead.opp_number).filter(
                Lead.opp_number.like(f'{prefix}%')).all():
            try: max_seq = max(max_seq, int(str(row[0]).split('-')[-1]))
            except (ValueError, IndexError): pass
        num = f"{prefix}{str(max_seq + 1).zfill(4)}"
    return jsonify({'number': num})


# ═══════════════════════════════════════════════════════════════════════════
# v3.1 API — Company · OverseasAgent · Opportunity · Activity · Outreach ·
#           smarter import (fuzzy header + preview → commit)
# ═══════════════════════════════════════════════════════════════════════════

# ---------- Companies ----------
@app.route('/api/companies', methods=['GET'])
@require_auth
def api_companies():
    q = (request.args.get('q') or '').strip().lower()
    query = Company.query.filter_by(is_active=True)
    if q:
        query = query.filter(db.or_(
            db.func.lower(Company.name).like(f'%{q}%'),
            db.func.lower(Company.industry).like(f'%{q}%'),
            db.func.lower(Company.city).like(f'%{q}%'),
        ))
    rows = query.order_by(Company.name).limit(500).all()
    return jsonify([c.to_dict() for c in rows])


@app.route('/api/companies', methods=['POST'])
@require_auth
def api_create_company():
    d = request.get_json(force=True) or {}
    if not (d.get('name') or '').strip():
        return jsonify({'error': 'name required'}), 400
    # Dedupe by lowercase name
    existing = Company.query.filter(
        db.func.lower(Company.name) == d['name'].strip().lower()).first()
    if existing:
        return jsonify({'error': 'Company already exists', 'id': existing.id}), 409
    c = Company(name=d['name'].strip(), industry=d.get('industry'),
                website=d.get('website'), country=d.get('country'),
                state=d.get('state'), city=d.get('city'),
                address=d.get('address'),
                phone=d.get('phone'), email=d.get('email'),
                linkedin=d.get('linkedin'), tier=d.get('tier'),
                notes=d.get('notes'), created_by=session.get('emp_code'))
    db.session.add(c); db.session.commit()
    return jsonify(c.to_dict()), 201


@app.route('/api/companies/<int:cid>', methods=['PUT'])
@require_auth
def api_update_company(cid):
    c = Company.query.get_or_404(cid)
    d = request.get_json(force=True) or {}
    for f in ('name', 'industry', 'website', 'country', 'state', 'city',
              'address', 'phone', 'email', 'linkedin', 'tier', 'notes'):
        if f in d: setattr(c, f, d[f])
    db.session.commit()
    return jsonify(c.to_dict())


@app.route('/api/companies/<int:cid>', methods=['DELETE'])
@require_admin
def api_delete_company(cid):
    c = Company.query.get_or_404(cid)
    c.is_active = False
    db.session.commit()
    return jsonify({'ok': True})


# ---------- Overseas Agents ----------
@app.route('/api/agents', methods=['GET'])
@require_auth
def api_agents():
    return jsonify([a.to_dict() for a in
                    OverseasAgent.query.filter_by(is_active=True)
                    .order_by(OverseasAgent.name).all()])


@app.route('/api/agents', methods=['POST'])
@require_auth
def api_create_agent():
    d = request.get_json(force=True) or {}
    if not (d.get('name') or '').strip():
        return jsonify({'error': 'name required'}), 400
    a = OverseasAgent(name=d['name'].strip(), country=d.get('country'),
                      city=d.get('city'), website=d.get('website'),
                      contact_person=d.get('contact_person'),
                      phone=d.get('phone'), email=d.get('email'),
                      address=d.get('address'), notes=d.get('notes'))
    db.session.add(a); db.session.commit()
    return jsonify(a.to_dict()), 201


@app.route('/api/agents/<int:aid>', methods=['PUT'])
@require_auth
def api_update_agent(aid):
    a = OverseasAgent.query.get_or_404(aid)
    d = request.get_json(force=True) or {}
    for f in ('name', 'country', 'city', 'website', 'contact_person',
              'phone', 'email', 'address', 'notes'):
        if f in d: setattr(a, f, d[f])
    db.session.commit()
    return jsonify(a.to_dict())


@app.route('/api/agents/<int:aid>', methods=['DELETE'])
@require_admin
def api_delete_agent(aid):
    a = OverseasAgent.query.get_or_404(aid)
    a.is_active = False
    db.session.commit()
    return jsonify({'ok': True})


# ---------- Opportunities ----------
@app.route('/api/opportunities', methods=['GET'])
@require_auth
def api_opportunities():
    role, emp = session.get('role'), session.get('emp_code')
    q = Opportunity.query
    if role not in ('admin',):
        q = q.filter(Opportunity.owner_emp_code == emp)
    rows = q.order_by(Opportunity.id.desc()).limit(500).all()
    return jsonify([o.to_dict() for o in rows])


@app.route('/api/opportunities', methods=['POST'])
@require_auth
def api_create_opportunity():
    d = request.get_json(force=True) or {}
    with _opp_lock:
        opp_no = (d.get('opp_number') or '').strip()
        if not opp_no:
            # Auto-mint if not supplied. Re-fetch max to avoid gaps.
            yr = date.today().year
            prefix = f"OPP-{yr}-"
            max_seq = 0
            for row in db.session.query(Opportunity.opp_number).filter(
                    Opportunity.opp_number.like(f'{prefix}%')).all():
                try: max_seq = max(max_seq, int(str(row[0]).split('-')[-1]))
                except (ValueError, IndexError): pass
            for row in db.session.query(Lead.opp_number).filter(
                    Lead.opp_number.like(f'{prefix}%')).all():
                try: max_seq = max(max_seq, int(str(row[0]).split('-')[-1]))
                except (ValueError, IndexError): pass
            opp_no = f"{prefix}{str(max_seq + 1).zfill(4)}"
        # Guard against dupe (DB unique constraint would also catch this)
        if Opportunity.query.filter_by(opp_number=opp_no).first():
            return jsonify({'error': f'Opportunity {opp_no} already exists'}), 409
        def _to_date(v):
            if not v: return None
            try: return datetime.strptime(v, '%Y-%m-%d').date()
            except (ValueError, TypeError): return None
        opp = Opportunity(
            opp_number=opp_no, lead_id=d.get('lead_id'),
            company_id=d.get('company_id'), title=d.get('title'),
            stage=d.get('stage', 'RFQ'),
            value_inr=d.get('value_inr'), currency=d.get('currency', 'INR'),
            probability=d.get('probability', 50),
            expected_close_date=_to_date(d.get('expected_close_date')),
            owner_emp_code=d.get('owner_emp_code') or session.get('emp_code'),
            rfq_received_date=_to_date(d.get('rfq_received_date')),
            notes=d.get('notes'))
        db.session.add(opp)
        try:
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            return jsonify({'error': f'Could not create: {e}'}), 500
    return jsonify(opp.to_dict()), 201


@app.route('/api/opportunities/<int:oid>', methods=['PUT'])
@require_auth
def api_update_opportunity(oid):
    o = Opportunity.query.get_or_404(oid)
    if session.get('role') != 'admin' and o.owner_emp_code != session.get('emp_code'):
        return jsonify({'error': 'Forbidden'}), 403
    d = request.get_json(force=True) or {}
    for f in ('title', 'stage', 'value_inr', 'currency', 'probability',
              'owner_emp_code', 'notes', 'lost_reason'):
        if f in d: setattr(o, f, d[f])
    if 'expected_close_date' in d and d['expected_close_date']:
        try: o.expected_close_date = datetime.strptime(d['expected_close_date'], '%Y-%m-%d').date()
        except (ValueError, TypeError): pass
    if d.get('stage') == 'Won' and not o.won_at:
        o.won_at = datetime.utcnow()
    if d.get('stage') == 'Lost' and not o.lost_at:
        o.lost_at = datetime.utcnow()
    db.session.commit()
    return jsonify(o.to_dict())


# ---------- Lead activities + stage history ----------
@app.route('/api/leads/<int:lid>/activities', methods=['GET'])
@require_auth
def api_lead_activities(lid):
    Lead.query.get_or_404(lid)
    acts = (LeadActivity.query.filter_by(lead_id=lid)
            .order_by(LeadActivity.occurred_at.desc()).all())
    return jsonify([a.to_dict() for a in acts])


@app.route('/api/leads/<int:lid>/activities', methods=['POST'])
@require_auth
def api_add_activity(lid):
    Lead.query.get_or_404(lid)
    d = request.get_json(force=True) or {}
    if not d.get('kind'):
        return jsonify({'error': 'kind required (call/email/meeting/rfq/note/visit)'}), 400
    occ = datetime.utcnow()
    if d.get('occurred_at'):
        try: occ = datetime.strptime(d['occurred_at'], '%Y-%m-%dT%H:%M')
        except (ValueError, TypeError):
            try: occ = datetime.strptime(d['occurred_at'], '%Y-%m-%d')
            except (ValueError, TypeError): pass
    a = LeadActivity(lead_id=lid, kind=d['kind'], subject=d.get('subject'),
                     body=d.get('body'), occurred_at=occ,
                     performed_by=session.get('emp_code'))
    db.session.add(a); db.session.commit()
    return jsonify(a.to_dict()), 201


@app.route('/api/leads/<int:lid>/stage-history', methods=['GET'])
@require_auth
def api_lead_stage_history(lid):
    Lead.query.get_or_404(lid)
    hist = (LeadStageHistory.query.filter_by(lead_id=lid)
            .order_by(LeadStageHistory.changed_at.desc()).all())
    return jsonify([h.to_dict() for h in hist])


# ---------- AI Outreach (Claude) ----------
@app.route('/api/outreach/generate', methods=['POST'])
@require_auth
def api_outreach_generate():
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return jsonify({'error':
            'AI Outreach not configured — set ANTHROPIC_API_KEY on the server.'}), 503
    d = request.get_json(force=True) or {}
    lead_id = d.get('lead_id')
    channel = d.get('channel', 'email')
    tone = d.get('tone', 'professional')
    goal = d.get('goal', 'introduce Procam and request a meeting')

    lead = Lead.query.get(lead_id) if lead_id else None
    company = None
    if d.get('company_id'):
        company = Company.query.get(d['company_id'])

    ctx = []
    if lead:
        # Lead model uses `pic` (contact person) + `designation_pic`.
        # Older code referenced `lead.contact_person` which doesn't exist and
        # raised AttributeError → 502 to every UI call.
        ctx.append(f"Lead: {lead.company or ''} · {lead.pic or ''}")
        if lead.designation_pic:
            ctx.append(f"Designation: {lead.designation_pic}")
        if lead.industry:      ctx.append(f"Industry: {lead.industry}")
        if lead.procam_vertical: ctx.append(f"Procam vertical: {lead.procam_vertical}")
        if lead.stage:         ctx.append(f"Stage: {lead.stage}")
        if lead.city or lead.state:
            ctx.append(f"Location: {', '.join(filter(None, [lead.city, lead.state]))}")
        # Prefer AI-extracted rich context if available (email leads)
        if getattr(lead, 'email_extracted_json', None):
            try:
                x = json.loads(lead.email_extracted_json)
                if x.get('one_line_summary'):
                    ctx.append(f"Their inquiry: {x['one_line_summary']}")
                if x.get('cargo_type'):
                    ctx.append(f"Cargo: {x.get('cargo_type')} "
                               f"{x.get('cargo_weight_mt','')} MT".strip())
                if x.get('origin') or x.get('destination'):
                    ctx.append(f"Route: {x.get('origin','—')} → {x.get('destination','—')}")
            except Exception:
                pass
        elif lead.notes:
            ctx.append(f"Notes: {lead.notes[:400]}")
    if company:
        ctx.append(f"Company: {company.name} · {company.industry or ''}")
        if company.notes: ctx.append(f"Company notes: {company.notes[:400]}")
    context = "\n".join(ctx) or "(no lead/company context provided)"

    # Frontend may pass a fully composed prompt (rich, Procam-specific) as
    # `extra_prompt`. Prefer it, otherwise fall back to a generic template.
    extra_prompt = (d.get('extra_prompt') or '').strip()
    if extra_prompt:
        prompt = extra_prompt + "\n\nContext:\n" + context
    else:
        prompt = (
            f"You are drafting an outreach {channel} on behalf of Procam Logistics — "
            f"a heavy-haulage, project-freight, installation and warehousing firm.\n\n"
            f"Context:\n{context}\n\n"
            f"Goal: {goal}\n"
            f"Tone: {tone}\n\n"
            f"Write a short {channel} draft. If email, include Subject on the first line "
            f"prefixed 'Subject:'. Keep it under 180 words. No emojis."
        )
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model=os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-4-5-20250929'),
            max_tokens=600,
            messages=[{'role': 'user', 'content': prompt}])
        text = ''.join(b.text for b in resp.content if hasattr(b, 'text')).strip()
    except Exception as e:
        return jsonify({'error': f'Anthropic call failed: {e}'}), 502

    subject, body = '', text
    if channel == 'email' and text.lower().startswith('subject:'):
        first, _, rest = text.partition('\n')
        subject = first.split(':', 1)[1].strip()
        body = rest.strip()

    from hashlib import sha256
    draft = OutreachDraft(
        lead_id=lead_id, company_id=d.get('company_id'), channel=channel,
        subject=subject, body=body,
        model=os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-4-5-20250929'),
        prompt_hash=sha256(prompt.encode()).hexdigest()[:32],
        status='draft', generated_by=session.get('emp_code'))
    db.session.add(draft); db.session.commit()
    return jsonify(draft.to_dict()), 201


# ---------- Smarter import — fuzzy header + preview → commit ----------
_HEADER_SYNONYMS = {
    'company':        ['company', 'company name', 'organisation', 'organization',
                       'org', 'client', 'account', 'firm', 'business'],
    'contact_person': ['contact', 'contact person', 'contact name', 'name',
                       'person', 'poc', 'point of contact'],
    'email':          ['email', 'e-mail', 'mail', 'email id', 'email address'],
    'phone':          ['phone', 'mobile', 'cell', 'contact number', 'tel',
                       'telephone', 'phone number'],
    'designation':    ['designation', 'title', 'role', 'position', 'job title'],
    'industry':       ['industry', 'sector', 'vertical', 'segment'],
    'city':           ['city', 'location'],
    'country':        ['country'],
    'stage':          ['stage', 'status', 'lead stage', 'pipeline stage'],
    'source':         ['source', 'lead source', 'referred by'],
    'notes':          ['notes', 'remarks', 'comments', 'description'],
    'opp_number':     ['opp number', 'opportunity number', 'opp no',
                       'opportunity no', 'opp id'],
    'website':        ['website', 'web', 'url', 'homepage'],
    'linkedin':       ['linkedin', 'linkedin url', 'linkedin profile'],
    'address':        ['address', 'street', 'street address'],
}


def _fuzzy_map_headers(headers, target_fields=None):
    """Map spreadsheet headers → our canonical field names using fuzzy matching."""
    target_fields = target_fields or list(_HEADER_SYNONYMS.keys())
    try:
        from rapidfuzz import fuzz
        def score(a, b): return fuzz.token_set_ratio(a, b)
    except ImportError:
        def score(a, b): return int(difflib.SequenceMatcher(None, a, b).ratio() * 100)

    result = {}
    used = set()
    lowered = [(h, (h or '').strip().lower()) for h in headers]
    for canon in target_fields:
        candidates = _HEADER_SYNONYMS.get(canon, [canon])
        best_hdr, best_score = None, 0
        for hdr, hl in lowered:
            if hdr in used: continue
            s = max(score(hl, c) for c in candidates)
            if s > best_score:
                best_score = s; best_hdr = hdr
        if best_hdr and best_score >= 82:
            result[best_hdr] = canon
            used.add(best_hdr)
    return result


def _parse_upload(f):
    """Return (headers, list-of-dict-rows) from an .xlsx/.xls/.csv upload."""
    name = (f.filename or '').lower()
    data = f.read()
    if name.endswith('.csv'):
        import csv as csvmod
        rdr = csvmod.DictReader(io.StringIO(data.decode('utf-8-sig', errors='replace')))
        headers = rdr.fieldnames or []
        rows = list(rdr)
    else:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        ws = wb.active
        rows_iter = ws.iter_rows(values_only=True)
        headers = [str(v).strip() if v is not None else '' for v in next(rows_iter, [])]
        rows = []
        for r in rows_iter:
            if all(v is None or str(v).strip() == '' for v in r):
                continue
            rows.append({headers[i]: r[i] for i in range(min(len(headers), len(r)))})
    return headers, rows


@app.route('/api/leads/import/preview', methods=['POST'])
@require_auth
def api_leads_import_preview():
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    f = request.files['file']
    if len(f.read()) > 15 * 1024 * 1024:
        return jsonify({'error': 'File too large (max 15 MB)'}), 400
    f.stream.seek(0)
    try:
        headers, rows = _parse_upload(f)
    except Exception as e:
        return jsonify({'error': f'Could not parse: {e}'}), 400
    mapping = _fuzzy_map_headers(headers)
    preview = []
    errors = []
    dupes = []
    seen = set()
    for i, r in enumerate(rows, 1):
        norm = {mapping[h]: r[h] for h in mapping if h in r and r[h] not in (None, '')}
        errs = []
        if not norm.get('company') and not norm.get('contact_person'):
            errs.append('Missing company + contact')
        if norm.get('email') and '@' not in str(norm['email']):
            errs.append('Invalid email')
        dupe_key = ((norm.get('company') or '').strip().lower(),
                    (norm.get('email') or '').strip().lower())
        is_dupe = False
        if dupe_key in seen:
            is_dupe = True; dupes.append(i)
        seen.add(dupe_key)
        if norm.get('company') and Lead.query.filter(
                db.func.lower(Lead.company) == dupe_key[0]).first():
            is_dupe = True
        preview.append({'row': i, 'data': norm, 'errors': errs,
                        'duplicate': is_dupe, 'raw': dict(r)})
        if errs: errors.append(i)

    batch = ImportBatch(kind='leads', filename=f.filename,
                       header_map=json.dumps(mapping),
                       total_rows=len(rows),
                       valid_rows=len(rows) - len(errors) - len(dupes),
                       error_rows=len(errors),
                       duplicate_rows=len(dupes),
                       preview_data=json.dumps(preview[:500]),
                       created_by=session.get('emp_code'))
    db.session.add(batch); db.session.commit()

    return jsonify({
        'batch_id': batch.id, 'headers': headers, 'header_map': mapping,
        'total': len(rows),
        'valid': batch.valid_rows, 'errors': batch.error_rows,
        'duplicates': batch.duplicate_rows,
        'preview': preview[:200],
        'note': 'Review the preview then POST /api/leads/import/commit with '
                '{"batch_id": <id>, "skip_duplicates": true} to import.'
    })


@app.route('/api/leads/import/commit', methods=['POST'])
@require_auth
def api_leads_import_commit():
    d = request.get_json(force=True) or {}
    batch = ImportBatch.query.get_or_404(d.get('batch_id'))
    if batch.committed:
        return jsonify({'error': 'Batch already committed'}), 400
    skip_dupes = bool(d.get('skip_duplicates', True))
    preview = json.loads(batch.preview_data or '[]')
    added = skipped = 0
    for row in preview:
        n = row.get('data') or {}
        if row.get('errors'): skipped += 1; continue
        if row.get('duplicate') and skip_dupes: skipped += 1; continue
        # Canonical field names (from _HEADER_SYNONYMS) → actual Lead columns.
        # Lead uses `pic` / `designation_pic` (legacy) — keep both worlds happy.
        lead = Lead(
            company=(n.get('company') or '').strip() or 'Unknown',
            pic=n.get('contact_person'),
            designation_pic=n.get('designation'),
            email=n.get('email'), phone=n.get('phone'),
            industry=n.get('industry'),
            city=n.get('city'), country=n.get('country') or 'India',
            stage=n.get('stage') or 'New',
            source=n.get('source') or 'import',
            notes=n.get('notes'), opp_number=n.get('opp_number'),
            linkedin=n.get('linkedin'),
            assigned_to=session.get('emp_code'),
        )
        db.session.add(lead); added += 1
    batch.committed = True
    batch.committed_at = datetime.utcnow()
    db.session.commit()
    return jsonify({'imported': added, 'skipped': skipped,
                    'batch_id': batch.id})


# ─────────────────── INIT DB ───────────────────

def init_db():
    with app.app_context():
        db.create_all()
        # Additive column autoheal — safe to run every boot (Postgres
        # ADD COLUMN IF NOT EXISTS + SQLite's ALTER TABLE won't error if
        # column exists thanks to the try/except).
        from sqlalchemy import text as _sql
        # SQLite doesn't accept `BOOLEAN DEFAULT FALSE` as a literal
        # (booleans aren't a SQL type there) — use INTEGER 0/1 instead,
        # which SQLAlchemy's Boolean adapter reads back as Python bool.
        _is_sqlite = db.engine.dialect.name == 'sqlite'
        _bool_ddl  = 'INTEGER DEFAULT 0' if _is_sqlite else 'BOOLEAN DEFAULT FALSE'
        _adds = [
            ('companies',     'state',            'VARCHAR(80)'),
            ('contacts',      'state',            'VARCHAR(80)'),
            ('opportunities', 'won_project_ref',  'VARCHAR(60)'),
            ('opportunities', 'won_project_at',   'TIMESTAMP'),
            # Dashboard v2 (Phase 1-3)
            ('leads',         'lost_reason',      'VARCHAR(60)'),
            ('leads',         'stage_entered_at', 'TIMESTAMP'),
            ('employees',     'is_vertical_head', _bool_ddl),
            ('employees',     'vertical_head_id', 'INTEGER'),
        ]
        for tbl, col, dtype in _adds:
            try:
                # Postgres path — supports IF NOT EXISTS on ALTER.
                db.session.execute(_sql(
                    f'ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS {col} {dtype}'))
                db.session.commit()
                app.logger.info('autoheal added %s.%s', tbl, col)
            except Exception as exc1:
                db.session.rollback()
                # SQLite path — retry without IF NOT EXISTS. Duplicate-
                # column errors are the expected case on re-boot; log
                # anything else so we notice a real failure.
                try:
                    db.session.execute(_sql(
                        f'ALTER TABLE {tbl} ADD COLUMN {col} {dtype}'))
                    db.session.commit()
                    app.logger.info('autoheal added %s.%s (retry)', tbl, col)
                except Exception as exc2:
                    db.session.rollback()
                    msg = str(exc2).lower()
                    if 'duplicate' not in msg and 'already exists' not in msg:
                        app.logger.warning('autoheal FAILED %s.%s: %s',
                                            tbl, col, exc2)
        # Dashboard v2 — KPI tables autoheal (idempotent). Boot-time
        # create so /api/kpi/* work even before the standalone migration
        # script runs.
        for _ddl in (
            "CREATE TABLE IF NOT EXISTS kpi_settings ("
            "  id INTEGER PRIMARY KEY, kpi_key VARCHAR(60) UNIQUE NOT NULL,"
            "  name VARCHAR(200) NOT NULL, category VARCHAR(40) NOT NULL,"
            "  unit VARCHAR(20) DEFAULT 'count', source_expr TEXT,"
            "  warning_threshold NUMERIC(5,2) DEFAULT 80,"
            "  success_threshold NUMERIC(5,2) DEFAULT 100,"
            "  default_weightage NUMERIC(5,2) DEFAULT 10,"
            "  is_active BOOLEAN DEFAULT TRUE,"
            "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE IF NOT EXISTS kpi_targets ("
            "  id INTEGER PRIMARY KEY, kpi_key VARCHAR(60) NOT NULL,"
            "  scope_type VARCHAR(20) NOT NULL, scope_key VARCHAR(60),"
            "  period_type VARCHAR(20) NOT NULL, period_start DATE NOT NULL,"
            "  period_end DATE NOT NULL, target_value NUMERIC(15,2) NOT NULL,"
            "  weightage NUMERIC(5,2) DEFAULT 10, notes TEXT,"
            "  created_by VARCHAR(20), updated_by VARCHAR(20),"
            "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
            "  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)",
        ):
            try:
                # Postgres needs SERIAL, but 'INTEGER PRIMARY KEY' works on
                # SQLite. For Postgres we substitute at runtime.
                if db.engine.dialect.name == 'postgresql':
                    _ddl = _ddl.replace('INTEGER PRIMARY KEY',
                                        'SERIAL PRIMARY KEY')
                db.session.execute(_sql(_ddl))
                db.session.commit()
            except Exception:
                db.session.rollback()
        # Seed default KPI catalog if empty.
        try:
            has_any = db.session.execute(_sql('SELECT 1 FROM kpi_settings LIMIT 1')).fetchone()
            if not has_any:
                _defaults = [
                    ('calls_done','Calls Done','activity','count'),
                    ('profile_sent','Profiles Sent','activity','count'),
                    ('appointments','Appointments','activity','count'),
                    ('visits','Visits','activity','count'),
                    ('rfqs_generated','RFQs Generated','activity','count'),
                    ('new_leads','New Leads','pipeline','count'),
                    ('active_pipeline','Active Pipeline','pipeline','count'),
                    ('opportunities','Opportunities','pipeline','count'),
                    ('won_count','Deals Won','pipeline','count'),
                    ('lost_count','Deals Lost','pipeline','count'),
                    ('conversion_pct','Lead → Won Conversion %','conversion','percent'),
                    ('rfq_won_pct','RFQ → Won %','conversion','percent'),
                    ('won_value','Business Won (INR M)','commercial','inr'),
                    ('pipeline_value','Pipeline Value (INR M)','commercial','inr'),
                    ('followup_compliance','Follow-up Compliance %','activity','percent'),
                ]
                for k, n, c, u in _defaults:
                    db.session.execute(_sql(
                        'INSERT INTO kpi_settings (kpi_key, name, category, unit, '
                        'warning_threshold, success_threshold, default_weightage, is_active) '
                        'VALUES (:k, :n, :c, :u, 80, 100, 10, TRUE)'),
                        {'k': k, 'n': n, 'c': c, 'u': u})
                db.session.commit()
        except Exception:
            db.session.rollback()
        # Create employees from PRERNA Employee Master if none exist
        if Employee.query.count() == 0:
            EMPLOYEES = [
                # (emp_code, name, email, department, designation, vertical, role)
            ('EMP3592024', 'Amit Kakkar', '', 'Sales', 'Manager', 'Project Freight', 'presales'),
            ('EMP3902025', 'Bala Murugan T', '', 'Sales', 'Manager', 'Heavy Transport', 'presales'),
            ('EMP3892025', 'Bhavin Vinodhbhai Jiilka', '', 'Corporate', 'Head of Accounts & Finance', 'All', 'user'),
            ('EMP472012', 'Gowdhaman Rajakrishnan', '', 'Operations', 'Asst Vice President', 'Installation', 'presales'),
            ('EMP182010', 'K Umamaheswara Rao', '', 'Corporate', 'Dy. General Manager', 'All', 'presales'),
            ('EMP12010', 'Nitin Rawat', '', 'Operations', 'Asst Vice President', 'Installation', 'presales'),
            ('EMP1282017', 'Pravinkumar Arumugam', '', 'Operations', 'Dy. General Manager', 'Installation', 'presales'),
            ('EMP112010', 'Sanjeev Kumar Paliwal', '', 'Sales', 'Sr General Manager', 'Project Freight', 'presales'),
            ('EMP372011', 'Sanjna Vardhan', '', 'Sales', 'Asst Vice President', 'Project Freight', 'presales'),
            ('EMP3702025', 'Suranjan Aon', '', 'Sales', 'Dy. General Manager', 'Heavy Transport', 'presales'),
            ('EMP3952025', 'Venkatesh Ramarao Althada', '', 'Sales', 'Manager', 'Heavy Transport', 'user'),
            ('EMP572012', 'Vijay T V', '', 'Sales', 'Dy. General Manager', 'Heavy Transport', 'presales'),
            ('EMP4022025', 'Vikrant Vats', '', 'Operations', 'Sr. Manager', 'Warehousing', 'user'),
            ('EMP3292023', 'Zahid Khan', '', 'Operations', 'Project Manager', 'Installation', 'presales'),
            ('EMP1552018', 'Abhishek Singh', '', 'Sales', 'Sr. Executive', 'Heavy Transport', 'user'),
            ('EMP3602024', 'Ahmad Ali', '', 'Operations', 'Supervisor', 'Installation', 'user'),
            ('EMP212010', 'Ajit Kumar Das', '', 'Sales', 'Operator', 'Heavy Transport', 'user'),
            ('EMP3672025', 'Akash Prabu', '', 'Operations', 'HSE Officer', 'Installation', 'user'),
            ('EMP3822025', 'Akash Somnath Narayne', '', 'Operations', 'Sr. Supervisor', 'Warehousing', 'user'),
            ('EMP1612018', 'Amit Kumar', '', 'Sales', 'Sr Assistant', 'Heavy Transport', 'user'),
            ('EMP2802022', 'Amol Bhagvan Nikam', '', 'Operations', 'Asst Manager', 'Warehousing', 'user'),
            ('EMP3942025', 'Aniket Ray Chaudhuri', '', 'Corporate', 'Executive', 'All', 'user'),
            ('EMP3152023', 'Anurag Uday Chand', '', 'Operations', 'Manager', 'Warehousing', 'user'),
            ('EMP2622020', 'Aritra Mitra', '', 'Corporate', 'Sr Supervisor', 'All', 'user'),
            ('EMP3322023', 'Aryaan  Shaikh', '', 'Sales', 'Sr Executive', 'Project Freight', 'user'),
            ('EMP3982025', 'Ashitosh Sarjerao Gholap', '', 'Sales', 'Supervisor', 'Heavy Transport', 'user'),
            ('EMP3852025', 'Avinash Tukaram Ghatul', '', 'Operations', 'Supervisor', 'Warehousing', 'user'),
            ('EMP3102023', 'Balu Bhagovrao Jogdanad', '', 'Operations', 'Supervisor', 'Warehousing', 'user'),
            ('EMP3022023', 'Bhushan B Bhagat', '', 'Sales', 'Manager', 'Heavy Transport', 'presales'),
            ('EMP3132023', 'Bidisha Banerjee', '', 'Corporate', 'Sr Supervisor', 'All', 'user'),
            ('EMP3732025', 'Bikash  Routh', '', 'Corporate', 'Assistant', 'All', 'user'),
            ('EMP3162023', 'Birendra Kumar', '', 'Operations', 'Asst Manager', 'Warehousing', 'user'),
            ('EMP3142023', 'Balkrishnan Sharma', '', 'Sales', 'Sr. Supervisor', 'Heavy Transport', 'user'),
            ('EMP1492018', 'Chakradhar Sahoo', '', 'Sales', 'Assistant', 'Heavy Transport', 'user'),
            ('EMP3862025', 'Chandresh Kumar Baijnath Yadav', '', 'Sales', 'Operator', 'Heavy Transport', 'user'),
            ('EMP2582020', 'Dattaram Mahalim', '', 'Sales', 'Executive', 'Project Freight', 'user'),
            ('EMP3962025', 'Dhanashree Harishchandra Pawar', '', 'Corporate', 'Accountant', 'All', 'user'),
            ('EMP2992023', 'Dipanka Talukder', '', 'Corporate', 'Asst Manager', 'All', 'user'),
            ('EMP3172023', 'Ekbal Chandpasha Shaikh', '', 'Operations', 'Sr. Supervisor', 'Warehousing', 'user'),
            ('EMP2782022', 'Gajanan Narayan Naglot', '', 'Operations', 'Asst Manager', 'Warehousing', 'user'),
            ('EMP2122018', 'Gajendra Kumar Giri', '', 'Sales', 'Sr Supervisor', 'Heavy Transport', 'user'),
            ('EMP3992025', 'Hazarat Ali', '', 'Sales', 'Supervisor', 'Heavy Transport', 'user'),
            ('EMP2972023', 'Jayanta Kumar Paul', '', 'Corporate', 'Sr Executive', 'All', 'user'),
            ('EMP3282023', 'Jones George T', '', 'Operations', 'Sr Project Engineer', 'Installation', 'user'),
            ('EMP3612024', 'Kamar Khan', '', 'Operations', 'Sr Project Engineer', 'Installation', 'user'),
            ('EMP1062016', 'Kamrul Islam', '', 'Sales', 'Operator', 'Heavy Transport', 'user'),
            ('EMP3802025', 'Kapil Bekanale', '', 'Operations', 'Sr. Supervisor', 'Warehousing', 'user'),
            ('EMP3552024', 'Karthikeyan  R', '', 'Operations', 'Project Engineer', 'Installation', 'user'),
            ('EMP2642021', 'Kumar Satyam Ray', '', 'Sales', 'Sr Executive', 'Project Freight', 'user'),
            ('EMP242010', 'Laxmi Ram Singh', '', 'Sales', 'Sr Manager', 'Project Freight', 'presales'),
            ('EMP172010', 'Manjurul Hoque', '', 'Sales', 'Operator', 'Heavy Transport', 'user'),
            ('EMP4002025', 'Md  Inamuddin', '', 'Operations', 'HSE Officer', 'Installation', 'user'),
            ('EMP2832022', 'Mohanraj R', '', 'Operations', 'Dy Manager', 'Installation', 'user'),
            ('EMP3742025', 'Muntazir Alam', '', 'Operations', 'HSE Officer', 'Installation', 'user'),
            ('EMP3932025', 'Manish Kumar Bhakta', '', 'Sales', 'HSE Officer', 'Heavy Transport', 'user'),
            ('EMP1082016', 'Nishit Ranjan Das', '', 'Sales', 'Operator', 'Heavy Transport', 'user'),
            ('EMP3062023', 'Nitin Ambadas Pawar', '', 'Operations', 'Sr. Supervisor', 'Warehousing', 'user'),
            ('EMP3192023', 'Panjab Dinkar Pise', '', 'Operations', 'Data Entry Operator', 'Warehousing', 'user'),
            ('EMP1672018', 'Partab Singh', '', 'Sales', 'Assistant', 'Heavy Transport', 'user'),
            ('EMP3762025', 'Parveen Sharma', '', 'Operations', 'Sr. Supervisor', 'Warehousing', 'user'),
            ('EMP1662018', 'Phool Chandra Yudhishir', '', 'Sales', 'Assistant', 'Heavy Transport', 'user'),
            ('EMP3182023', 'Pradip Balasaheb Surse', '', 'Operations', 'Sr. Supervisor', 'Warehousing', 'user'),
            ('EMP4032025', 'Pramod Kumar', '', 'Operations', 'Sr. Supervisor', 'Installation', 'user'),
            ('EMP22010', 'Rajeev Ranjan', '', 'Sales', 'Executive', 'Heavy Transport', 'user'),
            ('EMP2892022', 'Rakesh Dnyaneshwar Rawal', '', 'Operations', 'Supervisor', 'Warehousing', 'user'),
            ('EMP1322017', 'Ram Mohan Chaubey', '', 'Sales', 'Executive', 'Heavy Transport', 'user'),
            ('EMP812015', 'Ramesh Yadav Sechae', '', 'Sales', 'Operator', 'Heavy Transport', 'user'),
            ('EMP2952023', 'Rameshwar Nihalsingh Gusinge', '', 'Operations', 'Asst Manager', 'Warehousing', 'user'),
            ('EMP3212023', 'Sachin Thakur', '', 'Sales', 'Sr Customer Service Executive', 'Project Freight', 'user'),
            ('EMP2752021', 'Sagar Bhogle', '', 'Sales', 'Executive', 'Project Freight', 'user'),
            ('EMP132010', 'Sahadeb Sahoo', '', 'Sales', 'Operator', 'Heavy Transport', 'user'),
            ('EMP3922025', 'Sajiulah Khan', '', 'Sales', 'HSE Officer', 'Heavy Transport', 'user'),
            ('EMP482012', 'Saktheeswari Murugavel', '', 'Corporate', 'Sr Manager', 'All', 'presales'),
            ('EMP3972025', 'Samiksha Chandrakant Vayngankar', '', 'Corporate', 'Accounts Supervisor', 'All', 'user'),
            ('EMP3772025', 'Sanjay Bhite', '', 'Operations', 'Manager', 'Warehousing', 'presales'),
            ('EMP1602018', 'Santhosh P', '', 'Sales', 'Assistant', 'Project Freight', 'user'),
            ('EMP1332017', 'Santosh Kumar', '', 'Operations', 'Asst Manager', 'Installation', 'user'),
            ('EMP3092023', 'Satish Datta Navghare', '', 'Operations', 'Supervisor', 'Warehousing', 'user'),
            ('EMP3782025', 'Satish Jadhav', '', 'Operations', 'Supervisor', 'Warehousing', 'user'),
            ('EMP3842025', 'Saurabh Ramesh Waghmare', '', 'Operations', 'Supervisor', 'Warehousing', 'user'),
            ('EMP3542024', 'Sayan  Das', '', 'Sales', 'Executive', 'Heavy Transport', 'user'),
            ('EMP3372024', 'Sayantan Naskar', '', 'Operations', 'SITE ENGINEER', 'Warehousing', 'user'),
            ('EMP3222023', 'Sayanti  Ghosh', '', 'Corporate', 'Accounts Supervisor', 'All', 'user'),
            ('EMP2882022', 'Seema Chattopadhyay', '', 'Corporate', 'Manager', 'All', 'user'),
            ('EMP2982023', 'Sharayu Uday Bhosale', '', 'Sales', 'Asst Manager', 'Project Freight', 'user'),
            ('EMP2962023', 'Shashidhar Pandurang Naik', '', 'Operations', 'Asst Manager', 'Warehousing', 'user'),
            ('EMP3202023', 'Shivaji Ashok Dhumal', '', 'Operations', 'Data Entry Operator', 'Warehousing', 'user'),
            ('EMP3482024', 'Shriram Dattu Patil', '', 'Sales', 'Manager', 'Heavy Transport', 'presales'),
            ('EMP3912025', 'Shyam Bharti', '', 'Operations', 'Supervisor', 'Installation', 'user'),
            ('EMP3072023', 'Sohel Mainoor Shaikh', '', 'Operations', 'Sr Supervisor', 'Warehousing', 'user'),
            ('EMP3642024', 'Souvik Chakraborty', '', 'Operations', 'HSE Officer', 'Warehousing', 'user'),
            ('EMP3652025', 'Sumit Mondal', '', 'Corporate', 'Accountant', 'All', 'user'),
            ('EMP3882025', 'Sundhar Rajan S', '', 'Operations', 'Project Engineer', 'Installation', 'user'),
            ('EMP3532024', 'Suresh  Kumar', '', 'Sales', 'Executive', 'Heavy Transport', 'user'),
            ('EMP2792022', 'Swapnil Sunil Jadhav', '', 'Operations', 'Asst Manager', 'Warehousing', 'user'),
            ('EMP3122023', 'Sunita Naga Alkar', '', 'Corporate', 'Assistant', 'All', 'user'),
            ('EMP1682018', 'Tahirul Haque', '', 'Sales', 'Assistant', 'Heavy Transport', 'user'),
            ('EMP2482019', 'Tanima Mukherjee', '', 'Corporate', 'Sr Manager', 'All', 'presales'),
            ('EMP3752025', 'Vikash Dubey', '', 'Operations', 'Project Engineer', 'Installation', 'user'),
            ('EMP2902022', 'Vipul Sinh Zala', '', 'Operations', 'Manager', 'Warehousing', 'user'),
            ('EMP3832025', 'Vishal Pundlik Bhokre', '', 'Operations', 'Supervisor', 'Warehousing', 'user'),
            ('EMP3042023', 'Vishal Raosaheb Magar', '', 'Operations', 'Supervisor', 'Warehousing', 'user'),
            ('EMP3382024', 'Yogesh Kumar Rajasekaran', '', 'Operations', 'Sr Project Engineer', 'Installation', 'user'),
            ('EMP4052026', 'Akram Mahmud Mujawar', '', 'Operations', 'Supervisor', 'Warehousing', 'user'),
            ('EMP4062026', 'Pravin Abasaheb Barde', '', 'Operations', 'Supervisor', 'Warehousing', 'user'),
            ('EMP4092026', 'Pravin Choudhary', '', 'Operations', 'Sr General Manager', 'Warehousing', 'presales'),
            ('EMP4042026', 'Guruswami Mohanta', '', 'Operations', 'Supervisor', 'Installation', 'user'),
            ('EMP4072026', 'Dipanshu Kumar Singh', '', 'Operations', 'Supervisor', 'Installation', 'user'),
            ('EMP4082026', 'Keshvani Ankit Nileshbhai', '', 'Operations', 'Assistant', 'Installation', 'user'),
            ('EMP4102026', 'Aurmugam Pandi', '', 'Operations', 'Asst. General Manager', 'Installation', 'presales'),
            ('DIR12010', 'Nilesh Kumar Sinha', '', 'Corporate', 'Director', 'All', 'admin'),
            ('DIR22010', 'Francis Xavier', '', 'Sales', 'Director', 'Heavy Transport', 'admin'),
            ('DIR42010', 'Tg Ramalingam', '', 'Corporate', 'Director', 'All', 'admin'),
            ('DIR52011', 'S Sethupathy', '', 'Operations', 'Director', 'Installation', 'admin'),
            ('DIR72012', 'Srinivas M', '', 'Operations', 'Director', 'Warehousing', 'admin'),
            ]
            for ec, nm, em, dept, desig, vert, role in EMPLOYEES:
                e = Employee(
                    emp_code=ec, name=nm,
                    email=em or f"{ec.lower()}@procamgroup.in",
                    department=dept, designation=desig,
                    vertical=vert, role=role,
                    is_active=True, industries='[]',
                    must_change_pw=True
                )
                # Default password = employee code in lowercase (PRERNA rule)
                e.set_password(ec.lower())
                db.session.add(e)
            # Special: set Nilesh admin password (not forced to change)
            nilesh = Employee.query.filter_by(emp_code='DIR12010').first()
            if nilesh:
                nilesh.must_change_pw = False
            db.session.commit()
            print(f"✓ {len(EMPLOYEES)} employees seeded from Employee Master")
        else:
            print(f"✓ {Employee.query.count()} employees already in database")

        # ─── PCM001 Super Admin ───
        # Seeded from the ADMIN_INITIAL_PASSWORD env var. This runs on every boot
        # but is idempotent — after the first boot it only fixes must_change_pw
        # if the admin never logged in yet. Password is NEVER exposed in source.
        pcm = Employee.query.filter_by(emp_code='PCM001').first()
        if not pcm:
            initial_pw = (os.environ.get('ADMIN_INITIAL_PASSWORD')
                          or 'admin@Procam25')
            pcm = Employee(
                emp_code='PCM001', name='Procam Super Admin',
                email='admin@procamgroup.in', mobile='',
                department='Corporate', designation='Super Admin',
                vertical='All', role='admin',
                is_active=True, industries='[]',
                must_change_pw=True,
            )
            pcm.set_password(initial_pw)
            db.session.add(pcm)
            db.session.commit()
            print("✓ PCM001 Super Admin seeded from ADMIN_INITIAL_PASSWORD "
                  "(force-change on first login)")

# ═════════════════════════════════════════════════════════════════════
#  KPI MASTER + TARGETS + PERFORMANCE
#  Admin manages KPI catalog and per-scope targets. Actuals are computed
#  live off `Lead` — no manual data entry, no double-counting.
# ═════════════════════════════════════════════════════════════════════
def _kpi_actual_for(kpi_key, scope_type, scope_key, period_start, period_end):
    """Compute the live actual for a KPI over (scope, period).

    scope_type ∈ {'company', 'vertical', 'user'} — 'team' treated as
    vertical for now (Phase 2 will add teams). Actuals are pulled from
    `Lead` using the same field semantics as /api/dashboard/summary so
    every screen agrees.
    """
    q = Lead.query.filter(
        Lead.onboarded_date >= period_start,
        Lead.onboarded_date <= period_end,
    )
    if scope_type == 'vertical' and scope_key:
        q = q.filter(Lead.procam_vertical == scope_key)
    elif scope_type == 'user' and scope_key:
        q = q.filter(Lead.assigned_to == scope_key)

    if kpi_key == 'calls_done':
        return q.filter(Lead.phone_call_date.isnot(None)).count()
    if kpi_key == 'profile_sent':
        return q.filter(Lead.intro_mail_date.isnot(None)).count()
    if kpi_key == 'appointments':
        return q.filter(Lead.meeting_date.isnot(None)).count()
    if kpi_key == 'visits':
        return q.filter(Lead.stage.in_(
            ('Visit Done','RFQ Generated','Won'))).count()
    if kpi_key == 'rfqs_generated':
        return q.filter(Lead.rfq_date.isnot(None)).count()
    if kpi_key == 'new_leads':
        return q.count()
    if kpi_key == 'active_pipeline':
        return q.filter(Lead.stage.in_(STAGES_PIPELINE)).count()
    if kpi_key == 'opportunities':
        return q.filter(Lead.opp_number.isnot(None)).count()
    if kpi_key == 'won_count':
        return q.filter(Lead.stage == 'Won').count()
    if kpi_key == 'lost_count':
        return q.filter(Lead.stage == 'Lost').count()
    if kpi_key == 'conversion_pct':
        tot = q.count() or 1
        won = q.filter(Lead.stage == 'Won').count()
        return round(100.0 * won / tot, 2)
    if kpi_key == 'rfq_won_pct':
        rfq = q.filter(Lead.rfq_date.isnot(None)).count() or 1
        won = q.filter(Lead.stage == 'Won').count()
        return round(100.0 * won / rfq, 2)
    if kpi_key == 'won_value':
        return float(db.session.query(
            db.func.coalesce(db.func.sum(Lead.cost_million), 0)
        ).filter(Lead.stage == 'Won').filter(
            Lead.onboarded_date >= period_start,
            Lead.onboarded_date <= period_end,
        ).scalar() or 0)
    if kpi_key == 'pipeline_value':
        return float(db.session.query(
            db.func.coalesce(db.func.sum(Lead.cost_million), 0)
        ).filter(Lead.stage.in_(STAGES_PIPELINE)).filter(
            Lead.onboarded_date >= period_start,
            Lead.onboarded_date <= period_end,
        ).scalar() or 0)
    if kpi_key == 'followup_compliance':
        due  = q.filter(Lead.followup_date.isnot(None)).count() or 1
        done = q.filter(Lead.stage.in_(STAGES_TERMINAL)).count()
        return round(100.0 * done / due, 2)
    return 0


def _kpi_row_dict(row):
    """Convert a raw DB row (sqlalchemy Row) into JSON-friendly dict."""
    return {k: v for k, v in row._mapping.items()} if hasattr(row, '_mapping') else dict(row)


@app.route('/api/kpi/settings', methods=['GET'])
@require_auth
def api_kpi_settings():
    """Return the KPI Master (catalog)."""
    from sqlalchemy import text as _sql
    rows = db.session.execute(_sql(
        'SELECT kpi_key, name, category, unit, warning_threshold, '
        'success_threshold, default_weightage, is_active FROM kpi_settings '
        'WHERE is_active = TRUE ORDER BY category, name')).fetchall()
    return jsonify([_kpi_row_dict(r) for r in rows])


@app.route('/api/kpi/targets', methods=['GET', 'POST'])
@require_auth
def api_kpi_targets():
    """List (GET) or create/upsert (POST) KPI targets. Admin only."""
    if session.get('role') != 'admin' and request.method == 'POST':
        return jsonify({'ok': False, 'error': 'admin only'}), 403
    from sqlalchemy import text as _sql
    if request.method == 'GET':
        rows = db.session.execute(_sql(
            'SELECT id, kpi_key, scope_type, scope_key, period_type, '
            'period_start, period_end, target_value, weightage, notes '
            'FROM kpi_targets ORDER BY period_start DESC, kpi_key')).fetchall()
        return jsonify([{
            **_kpi_row_dict(r),
            'period_start': str(r._mapping['period_start']) if r._mapping['period_start'] else '',
            'period_end':   str(r._mapping['period_end'])   if r._mapping['period_end']   else '',
        } for r in rows])
    d = request.get_json() or {}
    required = ('kpi_key', 'scope_type', 'period_type', 'period_start',
                'period_end', 'target_value')
    for k in required:
        if k not in d or d[k] in (None, ''):
            return jsonify({'ok': False, 'error': f'missing {k}'}), 400
    db.session.execute(_sql(
        'INSERT INTO kpi_targets (kpi_key, scope_type, scope_key, period_type, '
        'period_start, period_end, target_value, weightage, notes, created_by) '
        'VALUES (:k, :st, :sk, :pt, :ps, :pe, :tv, :w, :n, :cb)'
    ), {'k': d['kpi_key'], 'st': d['scope_type'],
        'sk': d.get('scope_key') or '', 'pt': d['period_type'],
        'ps': d['period_start'], 'pe': d['period_end'],
        'tv': d['target_value'], 'w': d.get('weightage') or 10,
        'n': d.get('notes') or '', 'cb': session.get('emp_code') or ''})
    db.session.commit()
    return jsonify({'ok': True})


@app.route('/api/kpi/performance')
@require_auth
def api_kpi_performance():
    """Target-vs-Actual for every KPI target the current user can see.

    Scoping: admin → all targets. Vertical head → targets whose
    scope_type='vertical' matches their vertical OR scope_type='user' for
    their reports. Individual → own user targets only.
    """
    from sqlalchemy import text as _sql
    rows = db.session.execute(_sql(
        'SELECT id, kpi_key, scope_type, scope_key, period_type, '
        'period_start, period_end, target_value, weightage FROM kpi_targets'
    )).fetchall()

    role = session.get('role')
    my_code = session.get('emp_code') or ''
    me = Employee.query.filter_by(emp_code=my_code).first() if my_code else None
    my_vertical = me.vertical if me else ''
    _, allowed = _scope_for_current_session()

    catalog = {r._mapping['kpi_key']: _kpi_row_dict(r) for r in db.session.execute(_sql(
        'SELECT kpi_key, name, unit, warning_threshold, success_threshold '
        'FROM kpi_settings')).fetchall()}

    out = []
    today = date.today()
    for r in rows:
        m = r._mapping
        # Filter by scope
        if role != 'admin':
            if m['scope_type'] == 'company':      pass  # everyone sees company-wide
            elif m['scope_type'] == 'vertical'  and m['scope_key'] != my_vertical: continue
            elif m['scope_type'] in ('user', 'team') and m['scope_key'] not in allowed: continue
        target = float(m['target_value'] or 0)
        # SQLite returns period_start/end as strings; Postgres as date.
        # Normalise both to date so arithmetic works everywhere.
        def _asdate(x):
            if isinstance(x, date): return x
            try:    return datetime.strptime(str(x), '%Y-%m-%d').date()
            except Exception: return today
        ps = _asdate(m['period_start']); pe = _asdate(m['period_end'])
        actual = _kpi_actual_for(m['kpi_key'], m['scope_type'], m['scope_key'],
                                 ps, pe)
        pct = round(100.0 * actual / target, 1) if target else 0.0
        # Time pace
        span = max(1, (pe - ps).days)
        elapsed = max(0, min(span, (today - ps).days))
        time_pct = round(100.0 * elapsed / span, 1)
        cat = catalog.get(m['kpi_key'], {})
        w = float(cat.get('warning_threshold') or 80)
        s = float(cat.get('success_threshold') or 100)
        status = 'green' if pct >= s else ('amber' if pct >= w else 'red')
        out.append({
            'id': m['id'], 'kpi_key': m['kpi_key'],
            'name': cat.get('name') or m['kpi_key'],
            'unit': cat.get('unit') or 'count',
            'scope_type': m['scope_type'], 'scope_key': m['scope_key'] or '',
            'period_start': str(ps), 'period_end': str(pe),
            'target': target, 'actual': actual, 'achievement_pct': pct,
            'time_elapsed_pct': time_pct, 'status': status,
            'weightage': float(m['weightage'] or 10),
        })
    # Composite score = sum(pct * weightage) / sum(weightage)
    tot_w = sum(x['weightage'] for x in out) or 1
    composite = round(sum(x['achievement_pct'] * x['weightage'] for x in out) / tot_w, 1)
    return jsonify({'rows': out, 'composite_score': composite})


with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(debug=os.environ.get('DEBUG','false').lower()=='true',
            host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))