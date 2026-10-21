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
    'email_max_per_minute': (
        20, 'How many emails a minute the queue may send. Office 365 '
            'throttles a mailbox at thirty. 0 means no limit.'),
    'always_copy': (
        ['DIR12010'], 'Employees copied (CC) on every CRM notification '
                      'email. Kept small — this is a CC on everything.'),
    'always_copy_enabled': (
        True, 'Turn the standing CC off without losing the list.'),
    'allowed_email_domains': (
        [], 'Domains an employee address may use, beyond Procam\'s own. '
            'Empty means Procam domains only.'),
    'auto_assign_backstop': (
        'DIR12010', 'Who owns a lead when there is no account and no '
                    'vertical to go on. The floor, not a preference.'),
    'weekly_pack_recipients': (
        [], 'Extra people on the Thursday review pack, beyond the '
            'vertical heads.'),
    # ── decisions of 21 Oct 2026 (CRM questionnaire) ─────────────────
    'tender_owner_email': (
        '', 'Email address of the tender desk: every Tender / GeM lead '
            'goes to this person first, who passes it to the vertical. '
            'Empty means the person who keys the lead in keeps it.'),
    'new_accounts_reviewer': (
        ['DIR12010'],
        'Who reviews the accounts created during the week, in the '
        'Thursday report before the Friday sales review.'),
    'mail_subject_prefix': (
        '[CRM]', 'Put at the start of every CRM email subject, so CRM '
                 'mail can be told apart from TMS mail sent from the same '
                 'mailbox. Empty turns it off.'),
    'margin_floors': (
        {'PTM_OWN': 38, 'PTM_MARKET': 14, 'MECHANICAL': 12,
         'FREIGHT_FORWARDING': 10, 'WAREHOUSING': 18, 'INSTALLATION': 25},
        'Minimum margin %, on sell, per service line. A quote below it '
        'needs the director\'s approval. PTM lines need Own or Market '
        'on the line.'),
    'head_approval_limit_inr': (
        2500000, 'The largest quote (INR, before GST) a vertical head may '
                 'approve. Larger quotes need the director.'),
    'director_approvers': (
        ['DIR12010'],
        'Who may approve a quote below its minimum margin or above the '
        'head\'s limit — the director, and whoever stands in for him '
        '(add their employee code).'),
    'margin_viewers_ff': (
        ['DIR12010'],
        'The only people who see buy rates and margin on Freight '
        'Forwarding / Project Freight quotes. Add employee codes on this '
        'screen.'),
    'quote_validity_days': (
        {'Heavy Transport': 15, 'Project Freight': 7,
         'Freight Forwarding': 7, 'Installation': 30, 'Warehousing': 30},
        'Default quote validity in days, by vertical. Forwarding quotes '
        'also never outlive the carrier\'s own validity.'),
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


#: The settings on the "CRM rules" screen (decisions of 21 Oct 2026).
RULE_KEYS = ('tender_owner_email', 'new_accounts_reviewer',
             'director_approvers', 'margin_viewers_ff', 'margin_floors',
             'head_approval_limit_inr', 'quote_validity_days',
             'mail_subject_prefix')


def parse_rule(key, raw):
    """A value typed on the rules screen, checked against the default's
    shape. Raises ValueError with a sentence a person can act on."""
    default = DEFAULTS[key][0]
    raw = (raw or '').strip()
    if isinstance(default, str):
        return raw
    if isinstance(default, list):
        return [p.strip().upper() for p in raw.replace('\n', ',').split(',')
                if p.strip()]
    if isinstance(default, (int, float)) and not isinstance(default, bool):
        try:
            return int(raw.replace(',', ''))
        except ValueError:
            raise ValueError(f'{key}: enter a whole number')
    if isinstance(default, dict):
        out = {}
        for line in raw.splitlines():
            if not line.strip():
                continue
            if '=' not in line:
                raise ValueError(f'{key}: write one "name = number" per line')
            name, value = line.split('=', 1)
            try:
                out[name.strip()] = float(value.strip())
            except ValueError:
                raise ValueError(f'{key}: "{value.strip()}" is not a number')
            if out[name.strip()].is_integer():
                out[name.strip()] = int(out[name.strip()])
        return out
    raise ValueError(f'{key}: cannot be edited here')


def show_rule(key):
    value = get(key)
    if isinstance(value, list):
        return ', '.join(value)
    if isinstance(value, dict):
        return '\n'.join(f'{k} = {v}' for k, v in value.items())
    return '' if value is None else str(value)


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
