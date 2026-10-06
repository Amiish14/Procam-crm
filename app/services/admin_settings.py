"""
Administrator-editable settings that are not vocabulary.

Master Data holds lists people pick from. This holds single values an
administrator sets once: who gets a copy of notifications, which
events those copies cover, whether the admin daily report goes out.

Why a table rather than the environment: the brief is explicit that
the admin recipient must not be hard-coded into application logic,
and an environment variable is still a deploy and still a developer.
An administrator adding a second person to the copy list should be a
checkbox.

Every value is read through `get`, which falls back to a stated
default, so a setting that has never been saved behaves predictably
and the screen can show what the default is. The writer is `save`,
not `set`, because a module-level `set` shadows the builtin for
everything below it.
"""
from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

#: key → (default, one-line description for the screen)
DEFAULTS = {
    'admin_recipients': (
        [], 'Employees who receive a copy of the events ticked below. '
            'Empty means nobody — the CRM will not invent a recipient.'),
    'admin_copy_events': (
        ['lead.created', 'lead.unassigned', 'lead.needs_review'],
        'Which events the administrators are copied on.'),
    'admin_copies_enabled': (
        True, 'Turn every admin copy off without losing the list.'),
    'admin_daily_report': (
        True, 'The 08:30 administrator report: new leads by vertical and '
              'by PIC, unassigned leads, skipped and errored email.'),
    'reopen_stage': (
        'RFQ Generated', 'What a lead becomes when an enquiry arrives '
                         'against it after it was closed.'),
    'weekly_pack_recipients': (
        [], 'Extra people on the Thursday review pack, beyond the '
            'vertical heads.'),
}


def get(key, default=None):
    """One setting. Never raises; falls back to the stated default."""
    from app.models.integration import AppSetting

    fallback = default if default is not None else DEFAULTS.get(key, (None,))[0]
    try:
        row = AppSetting.query.filter_by(key=key).first()
        if row is None or row.value_json is None:
            return fallback
        return json.loads(row.value_json)
    except Exception:
        return fallback


def save(key, value, *, actor=None, commit=True):
    """Save one setting. Returns the value as stored."""
    from app import db
    from app.models.integration import AppSetting

    row = AppSetting.query.filter_by(key=key).first()
    if row is None:
        row = AppSetting(key=key)
        db.session.add(row)
    row.value_json = json.dumps(value)
    row.updated_by = actor
    if commit:
        db.session.commit()
    try:
        from app.services import audit
        audit.record('settings.changed', 'AppSetting', key,
                     new={'key': key, 'value': value}, actor=actor,
                     reason='changed in Notification Settings')
    except Exception:
        pass
    return value


def all_settings():
    """[(key, value, description)] for the screen."""
    return [(key, get(key), DEFAULTS[key][1]) for key in DEFAULTS]


# ── who gets a copy ──────────────────────────────────────────────────
def admin_recipients(event=None):
    """Employee codes to copy on this event.

    Three gates, all of which have to pass: copies are on at all, the
    event is on the list, and the person is an active employee with an
    address. An empty list is a real answer — the CRM does not fall
    back to "whoever looks like an admin", because a recipient nobody
    chose is a recipient nobody maintains.
    """
    if not get('admin_copies_enabled'):
        return []
    if event is not None and event not in (get('admin_copy_events') or []):
        return []

    wanted = [str(c).strip().upper() for c in (get('admin_recipients') or [])
              if str(c).strip()]
    if not wanted:
        return []
    try:
        from app import Employee
        rows = (Employee.query
                .filter(Employee.emp_code.in_(wanted),
                        Employee.is_active.isnot(False)).all())
        return [e.emp_code for e in rows if (e.email or '').strip()]
    except Exception:
        return []
