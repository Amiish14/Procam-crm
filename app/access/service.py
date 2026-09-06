"""Who can open what, and how much of it they see.

Everything in the portal asks this module, so access is decided in one
place and changed from one screen (Team & Admin → Access Control).

    from app.access.service import can, data_scope, require

    @bp.route('/quotes')
    @require('module.quotes')
    def quotes_page(): ...

An employee with no AccessProfile row falls back to ``default_for()``,
derived from their role — so the portal behaves exactly as it did before
this screen existed, and an admin can start granting from a sane baseline.
"""
from functools import wraps

from flask import session, jsonify, render_template, request

from app import db
from app.models.access import AccessProfile, DataScope


# ── the catalogue ────────────────────────────────────────────────────
# (key, label, help).  Groups are the column headings in the matrix.
PERMISSION_GROUPS = [
    ('Reports', [
        ('reports.action',      'Action Management',
         'Open and overdue tasks, SLA performance, follow-ups due'),
        ('reports.competitor',  'Competitor',
         'Register, intelligence log, win/loss, price comparison'),
        ('reports.accounts',    'Account Development',
         'Accounts, activities, RFQ / quoted / won value by account'),
    ]),
    ('Deals', [
        ('module.rfq',          'RFQs',          'Raise and work RFQs'),
        ('module.quotes',       'Quotes',        'Prepare, submit and track quotes'),
        ('module.handovers',    'Handovers',     'Won deals handed to operations'),
        ('module.funnels',      'Funnels',       'Account and project funnels'),
    ]),
    ('Intelligence', [
        ('module.competitors',  'Competitors',   'Competitor register and intelligence'),
        ('module.business_cards', 'Business Cards', 'Scan and file business cards'),
    ]),
    ('Administration', [
        ('admin.access',        'Access Control', 'Change this matrix'),
        ('admin.employees',     'Employee Master', 'Add and edit employees'),
        ('admin.email',         'Email Ingestion', 'Mailbox, subscriptions, inbox log'),
    ]),
]

ALL_PERMS = [k for _g, items in PERMISSION_GROUPS for k, _l, _h in items]

# Deliberately NOT in PERMISSION_GROUPS: it is not a checkbox.  It comes
# only from Employee.is_super_admin, so it cannot be granted through the
# matrix and the owner of the matrix cannot be edited out of it.
SUPER_PERM = 'admin.super'

REPORT_PERMS = ['reports.action', 'reports.competitor', 'reports.accounts']

# Everything a person needs to do ordinary sales work.
_TEAM_BASELINE = ['module.rfq', 'module.quotes', 'module.handovers',
                  'module.funnels', 'module.competitors',
                  'module.business_cards']

_ADMIN_ROLES = ('admin', 'procam_admin')


def default_for(emp):
    """The profile an employee gets when nobody has configured them yet."""
    if emp is None:
        return DataScope.OWN, []
    if getattr(emp, 'is_super_admin', False):
        return DataScope.ALL, list(ALL_PERMS)
    if (emp.role or '') in _ADMIN_ROLES:
        return DataScope.ALL, list(ALL_PERMS)
    if getattr(emp, 'is_vertical_head', False):
        return DataScope.VERTICAL, REPORT_PERMS + _TEAM_BASELINE
    return DataScope.OWN, list(_TEAM_BASELINE)


def _employee(emp_code):
    from app import Employee
    if not emp_code:
        return None
    return Employee.query.filter_by(emp_code=emp_code).first()


def is_super(emp_code=None):
    """True for the super admin — the one account that owns access itself."""
    emp = _employee(emp_code if emp_code is not None
                    else session.get('emp_code'))
    return bool(emp is not None and getattr(emp, 'is_super_admin', False))


def effective(emp_code):
    """Return ``(data_scope, perms:set)`` actually in force for someone.

    A stored profile wins, with two exceptions that exist so the portal
    cannot be locked shut: the super admin always holds everything, and an
    admin always keeps admin.access.
    """
    emp = _employee(emp_code)

    if emp is not None and getattr(emp, 'is_super_admin', False):
        return DataScope.ALL, set(ALL_PERMS) | {SUPER_PERM}

    prof = AccessProfile.query.filter_by(emp_code=emp_code).first() \
        if emp_code else None

    if prof is not None:
        scope, perms = (prof.data_scope or DataScope.OWN), prof.perm_set()
    else:
        scope, plist = default_for(emp)
        perms = set(plist)

    if emp is not None and (emp.role or '') in _ADMIN_ROLES:
        perms.add('admin.access')
        scope = scope or DataScope.ALL
    return scope, perms


def current():
    return effective(session.get('emp_code'))


def can(perm):
    """True when the signed-in user holds ``perm``."""
    if not session.get('emp_code'):
        return False
    _scope, perms = current()
    return perm in perms


def data_scope():
    """'all' | 'vertical' | 'own' for the signed-in user."""
    scope, _perms = current()
    return scope or DataScope.OWN


def is_admin():
    return can('admin.access')


def require_super(f):
    """Only the super admin may open this."""
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            if request.path.startswith('/api/'):
                return jsonify(ok=False, error='Not authenticated'), 401
            return ('', 302, {'Location': '/login'})
        if not is_super():
            if request.path.startswith('/api/'):
                return jsonify(ok=False, error='Only the super admin can '
                               'change access.', need=SUPER_PERM), 403
            return render_template('access_denied.html', need=SUPER_PERM), 403
        return f(*a, **kw)
    return wrap


def require(perm, template=None):
    """Guard a view with a permission.

    Redirects anonymous users to /login, answers API and ?format=json
    requests with 403 JSON, and otherwise renders a short explanation
    instead of a bare error.
    """
    def deco(f):
        @wraps(f)
        def wrap(*a, **kw):
            if not session.get('emp_code'):
                # API callers expect 401, not a redirect to an HTML login.
                if request.path.startswith('/api/'):
                    return jsonify(ok=False, error='Not authenticated'), 401
                return ('', 302, {'Location': '/login'})
            if not can(perm):
                wants_json = (request.path.startswith('/api/')
                              or (request.args.get('format') or '') == 'json')
                if wants_json:
                    return jsonify(ok=False, error='You do not have access to '
                                   'this. Ask an administrator.',
                                   need=perm), 403
                return render_template(template or 'access_denied.html',
                                       need=perm), 403
            return f(*a, **kw)
        return wrap
    return deco


# ── writing ──────────────────────────────────────────────────────────
def set_profile(emp_code, data_scope_value, perms, actor=None):
    """Create or update one employee's profile.  Caller checks authority."""
    if data_scope_value not in DataScope.CHOICES:
        raise ValueError(f'bad data_scope: {data_scope_value!r}')
    clean = sorted({p for p in (perms or []) if p in ALL_PERMS})

    prof = AccessProfile.query.filter_by(emp_code=emp_code).first()
    if prof is None:
        prof = AccessProfile(emp_code=emp_code)
        db.session.add(prof)
    prof.data_scope = data_scope_value
    prof.perms = clean
    prof.updated_by = actor or session.get('emp_code') or ''
    db.session.commit()
    return prof
