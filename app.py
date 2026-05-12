# ============================================================
#  PROCAM ProConnect CRM — Flask Application
#  Version 3.0  |  Ready for Render / PRERNA-stack hosting
# ============================================================

from flask import Flask, render_template, request, jsonify, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date
import os, json, re

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'procam-crm-secret-2025-change-in-prod')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///procam_crm.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = False  # Required for HTTP (not HTTPS) on local network

db = SQLAlchemy(app)

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
    stage            = db.Column(db.String(40), default='New')
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
    # ProConnect Opportunity
    opp_number       = db.Column(db.String(30))
    opp_stage        = db.Column(db.String(40))
    opp_close_date   = db.Column(db.Date)
    opp_notes        = db.Column(db.Text)
    # Tracking
    onboarded_date   = db.Column(db.Date, default=date.today)   # Date entered into system
    week_tag         = db.Column(db.String(20))
    email_sent_flag  = db.Column(db.String(100))
    created_at       = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at       = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
            'created_at': str(self.created_at)[:10] if self.created_at else ''
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
    """Return leads visible to current session user."""
    role = session.get('role')
    emp_code = session.get('emp_code')
    if role == 'admin':
        return Lead.query
    return Lead.query.filter_by(assigned_to=emp_code)

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
        city         = d.get('city',''),
        website      = d.get('website',''),
        linkedin     = d.get('linkedin',''),
        agent_type   = d.get('agent_type',''),
        assigned_to  = session['emp_code'],
        notes        = d.get('notes','')
    )
    db.session.add(ct)
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
    return jsonify({
        'total': len(all_leads),
        'active': len([l for l in all_leads if l.stage not in ('Won','Lost')]),
        'mailed': len([l for l in all_leads if l.intro_mail_date]),
        'rfq': len([l for l in all_leads if l.stage=='RFQ Generated']),
        'opp': len([l for l in all_leads if l.opp_number]),
        'won': len([l for l in all_leads if l.stage=='Won']),
        'stages': stages, 'industries': industries, 'verticals': verticals,
        'team': team_stats, 'pending_news': pending_news
    })

# ─────────────────── API: OPP NUMBER ───────────────────

@app.route('/api/opp-next-number')
@require_auth
def api_opp_number():
    count = Lead.query.filter(Lead.opp_number != None, Lead.opp_number != '').count()
    num = f"OPP-{date.today().year}-{str(count+1).zfill(4)}"
    return jsonify({'number': num})

# ─────────────────── INIT DB ───────────────────

def init_db():
    with app.app_context():
        db.create_all()
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

with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(debug=os.environ.get('DEBUG','false').lower()=='true',
            host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
