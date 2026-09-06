"""Access Control — the admin screen's API.

Read the matrix, write one row, or apply a preset to several people.  Every
endpoint requires ``admin.access``; the service layer guarantees a real
admin always holds it, so this screen cannot lock itself away.
"""
from flask import Blueprint, jsonify, request, session

from app import db
from app.models.access import AccessProfile, DataScope
from app.access.service import (PERMISSION_GROUPS, ALL_PERMS, REPORT_PERMS,
                                effective, set_profile, default_for,
                                require_super, is_super)

bp = Blueprint('access', __name__)


# Ready-made rows, so an admin does not tick twelve boxes per person.
PRESETS = {
    'admin': {
        'label': 'Administrator',
        'help':  'Every screen, whole company',
        'data_scope': DataScope.ALL,
        'perms': list(ALL_PERMS),
    },
    'vertical_head': {
        'label': 'Vertical Head',
        'help':  'Every report, but only their own vertical',
        'data_scope': DataScope.VERTICAL,
        'perms': REPORT_PERMS + ['module.rfq', 'module.quotes',
                                 'module.handovers', 'module.funnels',
                                 'module.competitors',
                                 'module.business_cards'],
    },
    'team_member': {
        'label': 'Team Member',
        'help':  'Day-to-day sales screens, no reports',
        'data_scope': DataScope.OWN,
        'perms': ['module.rfq', 'module.quotes', 'module.handovers',
                  'module.funnels', 'module.competitors',
                  'module.business_cards'],
    },
    'view_only': {
        'label': 'View Only',
        'help':  'My Work only',
        'data_scope': DataScope.OWN,
        'perms': [],
    },
}


@bp.route('/api/access/matrix')
@require_super
def api_matrix():
    """Everyone, with the access actually in force for each of them."""
    from app import Employee

    show_inactive = (request.args.get('inactive') or '') in ('1', 'true', 'yes')
    q = Employee.query
    if not show_inactive:
        q = q.filter(Employee.is_active.is_(True))

    configured = {p.emp_code: p for p in AccessProfile.query.all()}

    people = []
    for emp in q.order_by(Employee.name).all():
        scope, perms = effective(emp.emp_code)
        people.append({
            'is_super':    bool(getattr(emp, 'is_super_admin', False)),
            'id':          emp.id,
            'emp_code':    emp.emp_code,
            'name':        emp.name,
            'designation': emp.designation or '',
            'vertical':    (emp.vertical or '').strip(),
            'role':        emp.role or 'user',
            'is_active':   bool(emp.is_active),
            'data_scope':  scope,
            'perms':       sorted(perms),
            # False → still on the role-derived default, never edited here.
            'configured':  emp.emp_code in configured,
        })

    return jsonify(ok=True,
                   groups=[{'title': t,
                            'items': [{'key': k, 'label': l, 'help': h}
                                      for k, l, h in items]}
                           for t, items in PERMISSION_GROUPS],
                   scopes=[{'value': v, 'label': DataScope.LABELS[v]}
                           for v in DataScope.CHOICES],
                   presets=[{'key': k, **{kk: vv for kk, vv in p.items()}}
                            for k, p in PRESETS.items()],
                   people=people)


@bp.route('/api/access/profile/<emp_code>', methods=['PUT'])
@require_super
def api_set_profile(emp_code):
    from app import Employee

    emp = Employee.query.filter_by(emp_code=emp_code).first()
    if emp is None:
        return jsonify(ok=False, error='No such employee'), 404

    d = request.get_json(silent=True) or {}

    preset = d.get('preset')
    if preset:
        if preset not in PRESETS:
            return jsonify(ok=False, error=f'Unknown preset {preset}'), 400
        scope = PRESETS[preset]['data_scope']
        perms = PRESETS[preset]['perms']
    else:
        scope = d.get('data_scope')
        perms = d.get('perms')
        if scope is None or perms is None:
            return jsonify(ok=False,
                           error='data_scope and perms are required'), 400

    # The super admin's access is a column, not a profile row; editing it
    # here would be silently ignored, so say so rather than pretend.
    if getattr(emp, 'is_super_admin', False):
        return jsonify(ok=False, error='The super admin always has full '
                       'access and cannot be restricted here.'), 400

    try:
        prof = set_profile(emp_code, scope, perms)
    except ValueError as exc:
        return jsonify(ok=False, error=str(exc)), 400

    eff_scope, eff_perms = effective(emp_code)
    return jsonify(ok=True, profile=prof.to_dict(),
                   effective={'data_scope': eff_scope,
                              'perms': sorted(eff_perms)})


@bp.route('/api/access/profile/<emp_code>', methods=['DELETE'])
@require_super
def api_reset_profile(emp_code):
    """Drop the override and fall back to the role-derived default."""
    from app import Employee

    emp = Employee.query.filter_by(emp_code=emp_code).first()
    if emp is None:
        return jsonify(ok=False, error='No such employee'), 404

    prof = AccessProfile.query.filter_by(emp_code=emp_code).first()
    if prof is not None:
        db.session.delete(prof)
        db.session.commit()

    scope, perms = default_for(emp)
    return jsonify(ok=True, data_scope=scope, perms=sorted(perms))


@bp.route('/api/access/summary/<emp_code>')
@require_super
def api_person_summary(emp_code):
    '''Plain-English answer to: what can this person actually see?'''
    from app import Employee

    emp = Employee.query.filter_by(emp_code=emp_code).first()
    if emp is None:
        return jsonify(ok=False, error='No such employee'), 404

    scope, perms = effective(emp_code)
    label = {'all': 'every record in the company',
             'vertical': f'only {emp.vertical or "their vertical"} records',
             'own': 'only records they personally own'}.get(scope, scope)

    opens, closed = [], []
    for title, items in PERMISSION_GROUPS:
        for key, lbl, _help in items:
            (opens if key in perms else closed).append(f'{title} · {lbl}')

    return jsonify(ok=True, emp_code=emp_code, name=emp.name,
                   is_super=bool(getattr(emp, 'is_super_admin', False)),
                   data_scope=scope, scope_label=label,
                   can_open=opens, cannot_open=closed)


@bp.route('/api/access/me')
def api_my_access():
    """What the signed-in user may see — drives the navigation."""
    if not session.get('emp_code'):
        return jsonify(ok=False, error='Not signed in'), 401
    scope, perms = effective(session.get('emp_code'))
    return jsonify(ok=True, data_scope=scope, perms=sorted(perms))
