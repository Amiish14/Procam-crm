"""
Lead-entry rules decided on 21 Oct 2026 (CRM questionnaire B4, B6).

B6 — what a person must give when they create a lead by hand depends on
the vertical. Transport and freight need a route; warehousing and
installation have no route, they have a site.

B4 — a tender or GeM enquiry goes first to the one person who handles
every tender, who then passes it to the vertical. Who that is lives in
a setting, so a change of person is a change on screen, not in code.

Mail-created and imported leads are not checked here: the intake
pipeline has its own completeness review, and refusing a mail because
a field is missing would lose the enquiry.
"""
from __future__ import annotations

#: The source value for a tender or GeM enquiry. One spelling, so
#: reports group them.
TENDER_SOURCE = 'Tender/GeM'

#: Fields every manual lead needs, whatever the vertical.
_ALWAYS = (('company', 'Company'),
           ('procam_vertical', 'Vertical'))

#: Extra fields by managed vertical (Employee.vertical names; lead
#: aliases such as Freight Forwarding resolve through vertical_heads).
_BY_VERTICAL = {
    'Heavy Transport': (('origin', 'Origin'),
                        ('destination', 'Destination'),
                        ('cargo', 'Cargo summary')),
    'Project Freight': (('origin', 'Origin (port or city)'),
                        ('destination', 'Destination (port or city)'),
                        ('cargo', 'Cargo summary')),
    'Warehousing': (('site_location', 'Site / warehouse location'),
                    ('cargo', 'Storage requirement')),
    'Installation': (('site_location', 'Site location'),
                     ('cargo', 'Equipment / scope summary')),
}

#: Sources this rule applies to: a person typed the lead in.
MANUAL_SOURCES = ('manual', TENDER_SOURCE)


def _managed(vertical):
    from app.services import vertical_heads
    raw = (vertical or '').strip()
    if raw in _BY_VERTICAL:
        return raw
    mapped = vertical_heads.master_vertical(raw)
    return mapped if mapped in _BY_VERTICAL else ''


def required_fields(vertical):
    """[(key, label)] a manual lead in this vertical must carry."""
    return list(_ALWAYS) + list(_BY_VERTICAL.get(_managed(vertical), ()))


def missing(data):
    """Labels of required fields that are blank in `data`.

    `cargo` is satisfied by either the products or the project text,
    because the manual form has always called it one or the other.
    """
    out = []
    for key, label in required_fields(data.get('procam_vertical')):
        if key == 'cargo':
            value = data.get('cargo') or data.get('products') \
                or data.get('project')
        else:
            value = data.get(key)
        if not str(value or '').strip():
            out.append(label)
    return out


def applies(data):
    return (data.get('source') or 'manual') in MANUAL_SOURCES


def tender_owner():
    """emp_code of the tender desk, or None when nobody active is set.

    The setting holds an email address, because that is what people
    know a colleague by; the code is looked up each time so a change of
    code in HRMS does not strand the setting.
    """
    from app import Employee
    from app.services import admin_settings
    address = (admin_settings.get('tender_owner_email') or '').strip().lower()
    if not address:
        return None
    emp = (Employee.query
           .filter(Employee.email.ilike(address))
           .filter(Employee.is_active.isnot(False))
           .first())
    return emp.emp_code if emp else None
