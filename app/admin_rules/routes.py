"""
Admin → CRM rules: what the pilot still needs.

    GET /admin/crm-rules       the panel
    GET /api/admin/crm-rules   the same thing as JSON

The spec points at `/admin/crm-rules` in four places and the page
had never been built, so every setting it describes had to be set
from a shell. This is that page — read-only for now, because the
settings behind it are written by scripts that validate names,
and a text box that writes an unchecked employee code into
`rfq_coordinator` would undo that.
"""
from __future__ import annotations

from functools import wraps

from flask import Blueprint, jsonify, render_template, session

bp = Blueprint('admin_rules', __name__)


def _admin_only(f):
    @wraps(f)
    def wrap(*a, **kw):
        from app import Employee

        code = session.get('emp_code')
        if not code:
            from flask import redirect, url_for
            return redirect(url_for('login'))
        emp = Employee.query.filter_by(emp_code=code).first()
        role = (getattr(emp, 'role', '') or '').lower()
        if role not in ('admin', 'super admin', 'superadmin',
                        'director'):
            return jsonify(ok=False,
                           error='Admins and directors only.'), 403
        return f(*a, **kw)
    return wrap


@bp.route('/admin/crm-rules')
@_admin_only
def page():
    from app.services import pilot_readiness as pr
    return render_template('admin/crm_rules.html', data=pr.report())


@bp.route('/api/admin/crm-rules')
@_admin_only
def api():
    from app.services import pilot_readiness as pr
    return jsonify(ok=True, **pr.report())


#: Who each step routes to, in the words an administrator uses.
#: `OWNER_RULE` says the same thing in the engine's terms; this is
#: the sentence that goes on the page, so no person's code appears
#: in the catalogue.
DESKS = {
    'sales': ('Sales / BD',
              'the lead driver on the RFQ'),
    'coordinator': ('RFQ Coordinator',
                    'the rfq_coordinator setting'),
    'scm': ('SCM',
            'the rfq_scm_by_vertical table, per vertical'),
    'ops': ('Operations',
            'the rfq_ops_by_vertical table, per vertical'),
    'head': ('Vertical Head',
             'the head of the RFQ’s vertical'),
}

#: What closes a step, in a sentence rather than an event name.
CLOSES = {
    'rfq_mail_linked': 'the RFQ mail arrives and is linked',
    'registered': 'the coordinator confirms the priority',
    'confirm': 'a person confirms it',
    'cost_sheet_saved': 'the cost sheet is saved',
    'quote_approved': 'the quotation is approved',
    'quote_sent': 'the quotation is sent',
    'customer_replied': 'the customer replies',
    'award_date_recorded': 'the award date is recorded',
    'po_recorded': 'the PO or LOI is recorded',
    'handover_sent': 'the handover pack is sent',
}


def _steps():
    """The twelve, with their catalogue row where one exists."""
    from app.services import rfq_chain as rc

    out = []
    for step in rc.STEPS:
        desk, rule = DESKS.get(step.get('who'), ('—', ''))
        row = rc.definition_for(step['no'])
        share = step.get('share')
        if share:
            standard = f'{int(share * 100)}% of the priority window'
        elif step.get('fixed_hours'):
            standard = f"{int(step['fixed_hours'])} hours, fixed"
        elif step.get('response_clock'):
            standard = 'our response clock, per revision'
        elif step.get('customer_clock'):
            standard = "the customer's own clock — never ours"
        else:
            standard = '—'
        out.append({
            'no': step['no'],
            'key': step['key'],
            'title': step['title'],
            'short': step.get('short', step['title']),
            'desk': desk,
            'routing': rule,
            'standard': standard,
            'closes': CLOSES.get(step['event'], step['event']),
            'confirmable': step['no'] in rc.CONFIRMABLE,
            'optional': step.get('optional', ''),
            'catalogued': row is not None,
            'sla_hours': getattr(row, 'sla_hours', None),
            'active': bool(getattr(row, 'is_active', True)),
        })
    return out


@bp.route('/admin/rfq-steps')
@_admin_only
def steps_page():
    """The twelve steps of the RFQ chain, as rows to read.

    RFQ-DEMO-PARITY. The chain's shape lived in one tuple and one
    catalogue table, and neither had a screen: an administrator
    asking "who does step 5, and what closes it?" had to read
    Python. The demo answers that question on a page, so this is
    that page — read-only, because the settings behind it are
    written by scripts that validate employee codes.
    """
    from app.services import rfq_chain as rc
    return render_template('admin/rfq_steps.html', steps=_steps(),
                           priorities=rc.PRIORITY_DAYS,
                           response=rc.RESPONSE_HOURS,
                           target=int(rc.ON_TIME_TARGET * 100),
                           early=int(rc.EARLY_FRACTION * 100))


@bp.route('/api/admin/rfq-steps')
@_admin_only
def steps_api():
    from app.services import rfq_chain as rc
    return jsonify(ok=True, steps=_steps(),
                   priorities=rc.PRIORITY_DAYS,
                   response=rc.RESPONSE_HOURS)
