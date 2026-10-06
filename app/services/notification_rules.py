"""
Who hears about what, as data rather than as code.

Notifications used to be decided at the call site: each route chose a
recipient, a wording and a channel, and the answer to "does the
secondary PIC get told when a quote is rejected?" could only be found by
reading every route. This module holds that decision in one table —
`MATRIX` — so the question is answered by reading one list, and changing
the answer is editing one row.

    from app.services import notification_rules as rules
    rules.dispatch('quote.approved', quote, actor=session['emp_code'])

Delivery is `app/services/notify.py` and nothing else: it writes the
in-app row, sends the email through the Graph transport, suppresses
duplicates and never raises. This module only decides *who* and *what
it says*; `dispatch` returns what happened per recipient so a caller
that wants to log it can, and ignores it otherwise.

Roles, not names
    A row names roles — owner, secondary, vertical head, administrator —
    and the role is resolved against the record and the employee master
    at send time. Nobody's code appears in this file, so a person
    changing jobs changes nothing here.

Escalation
    `escalate_to` and `escalate_days` say who hears about it when it has
    gone unattended. Escalation is never automatic inside `dispatch`:
    the caller that knows the record is late (the end-of-day job) passes
    `escalate=True`. A route firing an event the moment it happens must
    not also escalate it.
"""
from __future__ import annotations

# ── roles a row may address ──────────────────────────────────────────
R_OWNER = 'owner'                 #: the record's primary PIC
R_SECONDARY = 'secondary'         #: the monitor/backup PIC
R_HEAD = 'vertical_head'          #: the owner's vertical head
R_ADMIN = 'admin'                 #: CRM administrators
R_MANAGEMENT = 'management'       #: administrators, for company-wide news
R_ACTOR = 'actor'                 #: whoever performed the action
R_PREPARER = 'preparer'           #: who prepared a quote
R_APPROVER = 'approver'           #: who approved a quote

ROLES = (R_OWNER, R_SECONDARY, R_HEAD, R_ADMIN, R_MANAGEMENT, R_ACTOR,
         R_PREPARER, R_APPROVER)

#: How a row arrives: at the moment it happens, or on a timer.
S_EVENT = 'event'
S_SCHEDULED = 'scheduled'

#: Keys every row must carry. Enforced by the tests so a half-written
#: row cannot reach production and fail at send time.
REQUIRED = ('event', 'kind', 'title', 'to', 'in_app', 'email', 'source',
            'entity_type', 'implemented')


def _rule(event, kind, title, *, body='', to=(), in_app=True, email=False,
          escalate_to=None, escalate_days=None, entity_type=None, url=None,
          source=S_EVENT, implemented=False, call_site='', note='',
          attach_original=False):
    """One row of the matrix. A function only so that the defaults are
    written once and every row reads as data."""
    return {
        'event': event,
        'kind': kind,
        'title': title,              # may use {what}
        'body': body,                # may use {what}, {detail}, {actor}
        'to': tuple(to),
        'in_app': in_app,
        'email': email,
        'escalate_to': escalate_to,
        'escalate_days': escalate_days,
        'entity_type': entity_type,
        'url': url,                  # may use {id}
        'source': source,
        'implemented': implemented,
        'call_site': call_site,
        'note': note,
        #: Carry the client's original message and its files on the
        #: email. Only on the rows where the recipient's next action is
        #: to read the request — being handed a lead, or an RFQ raised
        #: against one. Everywhere else it is a client's document sent
        #: for no reason.
        'attach_original': attach_original,
    }


# ── the matrix ───────────────────────────────────────────────────────
# Read top to bottom: it is the whole answer to "who gets told what".
# `implemented=False` means the row is agreed but no call site fires it
# yet; docs/operations/NOTIFICATIONS.md carries the same flags.
MATRIX = (
    # ── leads ────────────────────────────────────────────────────────
    _rule('lead.assigned', 'lead_assigned',
          'Lead assigned to you — {what}',
          body='This lead is yours to progress. {actor}',
          to=(R_OWNER,), email=True, attach_original=True,
          entity_type='Lead', url='/app?lead={id}',
          implemented=True,
          call_site='app/services/lead_assignment.py::_notify',
          note='Already delivered by the assignment service\'s own code; '
               'the row is here so the matrix is complete. Moving that '
               'code onto dispatch() is a tidy-up, not a behaviour change.'),
    _rule('lead.assigned_secondary', 'lead_assigned_secondary',
          'You are monitoring a lead — {what}',
          body='You are the secondary PIC. The primary owns this lead; '
               'you are here to pick it up if it stalls.',
          to=(R_SECONDARY,), email=True, attach_original=True,
          entity_type='Lead', url='/app?lead={id}',
          implemented=True,
          call_site='app/services/lead_assignment.py::_notify'),
    _rule('lead.stage_changed', 'lead_stage_changed',
          'Stage changed — {what}',
          body='{detail} {actor}',
          to=(R_OWNER, R_SECONDARY), email=False,
          entity_type='Lead', url='/app?lead={id}',
          call_site='app.py::_announce_lead_change',
          implemented=True),
    _rule('lead.high_value', 'lead_high_value',
          'High-value deal — {what}',
          body='{detail} A deal at this size is reported to the vertical '
               'head as soon as it is recorded.',
          to=(R_OWNER, R_HEAD), email=True,
          entity_type='Lead', url='/app?lead={id}',
          call_site='app.py::_announce_lead_change'
                    'raised past the high-value threshold',
          implemented=True,
          note='Fires only when the value crosses the threshold, not on every edit of a lead that is already large.'),
    _rule('lead.followup_overdue', 'lead_followup_overdue',
          'Follow-up overdue — {what}',
          body='{detail}',
          to=(R_OWNER,), email=False,
          escalate_to=R_HEAD, escalate_days=7,
          entity_type='Lead', url='/app?lead={id}',
          source=S_SCHEDULED, implemented=True,
          call_site='scripts/send_reports.py --exceptions',
          note='Delivered as one end-of-day email, not one message per '
               'lead: a person with forty overdue leads would stop '
               'reading at the third notification.'),
    _rule('lead.going_cold', 'lead_going_cold',
          'Going cold — {what}',
          body='{detail}',
          to=(R_OWNER,), email=False,
          escalate_to=R_HEAD, escalate_days=14,
          entity_type='Lead', url='/app?lead={id}',
          source=S_SCHEDULED, implemented=True,
          call_site='scripts/send_reports.py --daily'),

    # ── RFQs ─────────────────────────────────────────────────────────
    _rule('rfq.created', 'rfq_created',
          'RFQ raised — {what}',
          body='{detail} A quote is expected by the date on the RFQ.',
          to=(R_OWNER,), email=True,
          entity_type='RFQ', url='/rfqs/{id}',
          call_site='app/rfq/routes.py::api_create_rfq',
          implemented=True),
    _rule('rfq.owner_changed', 'rfq_owner_changed',
          'RFQ is now yours — {what}',
          body='{detail} {actor}',
          to=(R_OWNER,), email=True,
          entity_type='RFQ', url='/rfqs/{id}',
          call_site='app/rfq/routes.py::api_patch_rfq'
                    'changes',
          implemented=True,
          note="The RFQ's owner is its lead_driver."),
    _rule('rfq.rate_submitted', 'rfq_rate_submitted',
          'A rate came back — {what}',
          body='{detail}',
          to=(R_OWNER,), email=False,
          entity_type='RFQ', url='/rfqs/{id}',
          call_site='app/rfq/routes.py::api_submit_rate',
          implemented=True),
    _rule('rfq.quote_due_soon', 'rfq_quote_due_soon',
          'Quote due — {what}',
          body='{detail}',
          to=(R_OWNER,), email=False,
          entity_type='RFQ', url='/rfqs/{id}',
          source=S_SCHEDULED, implemented=True,
          call_site='scripts/send_reports.py --daily'),
    _rule('rfq.quote_overdue', 'rfq_quote_overdue',
          'Quote past its deadline — {what}',
          body='{detail} The customer is waiting beyond the date we '
               'committed to.',
          to=(R_OWNER,), email=True,
          escalate_to=R_HEAD, escalate_days=2,
          entity_type='RFQ', url='/rfqs/{id}',
          source=S_SCHEDULED, implemented=True,
          call_site='scripts/send_reports.py --exceptions'),

    # ── quotes ───────────────────────────────────────────────────────
    _rule('quote.submitted_for_approval', 'quote_awaiting_approval',
          'Quote waiting for your approval — {what}',
          body='{detail} {actor}',
          to=(R_HEAD,), email=True,
          escalate_to=R_ADMIN, escalate_days=2,
          entity_type='Quote', url='/quotes/{id}',
          call_site='app/quote/routes.py::api_submit_for_approval'
                    'the commit',
          implemented=True),
    _rule('quote.approved', 'quote_approved',
          'Quote approved — {what}',
          body='{detail} It may now go to the customer.',
          to=(R_PREPARER, R_OWNER), email=True,
          entity_type='Quote', url='/quotes/{id}',
          call_site='app/quote/routes.py, the approve route',
          implemented=True),
    _rule('quote.rejected', 'quote_rejected',
          'Quote returned for rework — {what}',
          body='{detail}',
          to=(R_PREPARER,), email=True,
          entity_type='Quote', url='/quotes/{id}',
          call_site='app/quote/routes.py, the reject route',
          implemented=True),
    _rule('quote.submitted_to_client', 'quote_submitted',
          'Quote sent to the customer — {what}',
          body='{detail}',
          to=(R_OWNER, R_SECONDARY), email=False,
          entity_type='Quote', url='/quotes/{id}',
          call_site='app/quote/routes.py, the submit-to-client route'
                    'commit',
          implemented=True),
    _rule('quote.won', 'quote_won',
          'Won — {what}',
          body='{detail}',
          to=(R_OWNER, R_HEAD, R_MANAGEMENT), email=True,
          entity_type='Quote', url='/quotes/{id}',
          call_site='app/quote/routes.py, the won route',
          implemented=True),
    _rule('quote.lost', 'quote_lost',
          'Lost — {what}',
          body='{detail}',
          to=(R_OWNER, R_HEAD), email=True,
          entity_type='Quote', url='/quotes/{id}',
          call_site='app/quote/routes.py, the lost route',
          implemented=True),
    _rule('quote.validity_lapsed', 'quote_validity_lapsed',
          'Quote validity has passed — {what}',
          body='{detail} The customer is holding a price we no longer '
               'stand behind.',
          to=(R_OWNER,), email=False,
          escalate_to=R_HEAD, escalate_days=7,
          entity_type='Lead', url='/app?lead={id}',
          source=S_SCHEDULED, implemented=True,
          call_site='scripts/send_reports.py --exceptions'),

    # ── accounts ─────────────────────────────────────────────────────
    _rule('account.pic_changed', 'account_pic_changed',
          'Account assigned to you — {what}',
          body='{detail} {actor}',
          to=(R_OWNER,), email=True,
          entity_type='Company', url='/companies/{id}',
          call_site='presales/routes.py::_announce_pic_change'
                    '(the bulk assign already audits; add the dispatch '
                    'beside it)',
          implemented=True,
          note='Single and bulk both go through it; a bulk run shares one batch key so forty accounts arrive as one email.'),
    _rule('account.next_action_due', 'account_next_action_due',
          'Account action due — {what}',
          body='{detail}',
          to=(R_OWNER, R_SECONDARY), email=False,
          entity_type='Company', url='/companies/{id}',
          source=S_SCHEDULED, implemented=True,
          call_site='scripts/send_reports.py --daily'),

    # ── housekeeping ─────────────────────────────────────────────────
    _rule('data.quality_breach', 'data_quality_breach',
          'Data quality needs attention — {what}',
          body='{detail}',
          to=(R_ADMIN,), email=True,
          entity_type='data_quality', url='/data-quality',
          source=S_SCHEDULED,
          call_site='scripts/data_quality_snapshot.py::_tell_the_administrators'
                    'for "a check got worse" is agreed',
          implemented=True),
    _rule('report.daily', 'report_daily',
          'Your CRM actions for today',
          to=(R_OWNER,), in_app=False, email=True,
          entity_type='report', url='/my-work',
          source=S_SCHEDULED, implemented=True,
          call_site='scripts/send_reports.py --daily'),
    _rule('report.exceptions', 'report_exceptions',
          'Still open at the end of the day',
          to=(R_OWNER,), in_app=False, email=True,
          entity_type='report', url='/my-work',
          source=S_SCHEDULED, implemented=True,
          call_site='scripts/send_reports.py --exceptions'),
    _rule('report.weekly_user', 'report_weekly_user',
          'Your week on the CRM',
          to=(R_OWNER,), in_app=False, email=True,
          entity_type='report', url='/my-work',
          source=S_SCHEDULED, implemented=True,
          call_site='scripts/send_reports.py --weekly-user'),
    _rule('report.weekly_head', 'report_weekly_head',
          'Team review for the week',
          to=(R_HEAD,), in_app=False, email=True,
          entity_type='report', url='/team-workbench',
          source=S_SCHEDULED, implemented=True,
          call_site='scripts/send_reports.py --weekly-head'),
    # ── the client block register ────────────────────────────────────
    _rule('restriction.recommended', 'restriction_recommended',
          'Approval needed — {what}',
          body='{detail}',
          to=(R_ADMIN,), email=True,
          entity_type='ClientRestriction', url='/admin/restrictions/{id}',
          implemented=True,
          call_site='app/services/restriction_notify.py::recommendation_raised',
          note='Sent to whoever holds admin.restrictions, resolved at send '
               'time rather than from this row, because who may approve a '
               'block is configuration.'),
    _rule('restriction.blocked', 'client_blocked',
          'Do not do business with {what}',
          body='{detail}',
          to=(R_ADMIN, R_MANAGEMENT), email=True,
          entity_type='ClientRestriction', url='/admin/restrictions/{id}',
          implemented=True,
          call_site='app/services/restriction_notify.py::announce_block',
          note='Goes to everyone who sells, not only the roles named here, '
               'and is marked urgent: it exists to stop the next enquiry, '
               'so it does not wait for quiet hours to end.'),
    _rule('restriction.records_closed', 'client_blocked_yours',
          '{what} blocked — your records were closed',
          body='{detail}',
          to=(R_OWNER,), email=True,
          entity_type='ClientRestriction', url='/admin/restrictions/{id}',
          implemented=True,
          call_site='app/services/restriction_notify.py::owners_told'),
    _rule('restriction.review_due', 'restriction_review',
          'Review due — {what}',
          body='{detail}',
          to=(R_APPROVER,), email=True, source=S_SCHEDULED,
          entity_type='ClientRestriction', url='/admin/restrictions/{id}',
          implemented=True,
          call_site='scripts/restriction_sweep.py'),
    _rule('restriction.attempt_digest', 'restriction_attempts',
          'Attempts against the block register',
          body='{detail}',
          to=(R_ADMIN,), email=True, in_app=False, source=S_SCHEDULED,
          entity_type='ClientRestriction', url='/admin/restrictions',
          implemented=True,
          call_site='scripts/restriction_sweep.py'),
    _rule('report.monthly', 'report_monthly',
          'CRM management report',
          to=(R_MANAGEMENT,), in_app=False, email=True,
          entity_type='report', url='/team-workbench',
          source=S_SCHEDULED, implemented=True,
          call_site='scripts/send_reports.py --monthly'),
)

BY_EVENT = {row['event']: row for row in MATRIX}
EVENTS = tuple(BY_EVENT)


def rule(event):
    """The row for an event, or None. Never raises on an unknown key —
    an event nobody has written a row for must not break the save that
    produced it."""
    return BY_EVENT.get(event)


# ── resolving roles against a record ─────────────────────────────────
#: Where a record keeps its primary owner, in the order the access
#: layer reads them (`app/access/scope.may_view`), so "owner" means the
#: same person to a notification as it does to a permission check.
_OWNER_ATTRS = ('assigned_to', 'owner_emp_code', 'lead_driver',
                'pic_emp_code', 'prepared_by_id')
_SECONDARY_ATTRS = ('secondary_owner', 'secondary_pic_emp_code')


def _logger():
    try:
        from flask import current_app
        return current_app.logger
    except Exception:                                   # pragma: no cover
        import logging
        return logging.getLogger(__name__)


def _first_attr(record, attrs):
    for attr in attrs:
        value = (getattr(record, attr, None) or '')
        if value:
            return str(value).strip().upper()
    return ''


def owner_of(record):
    return _first_attr(record, _OWNER_ATTRS)


def secondary_of(record):
    return _first_attr(record, _SECONDARY_ATTRS)


def vertical_head_for(emp_code):
    """The head a person reports to.

    The explicit `vertical_head_id` link first, because somebody has
    said so; otherwise the active head of their vertical. Returns '' if
    neither exists, which is a configuration gap, not an error — the
    message simply has one fewer recipient.
    """
    from app import Employee
    if not emp_code:
        return ''
    emp = Employee.query.filter_by(emp_code=emp_code).first()
    if emp is None:
        return ''
    if getattr(emp, 'vertical_head_id', None):
        head = Employee.query.filter_by(id=emp.vertical_head_id).first()
        if head is not None and head.emp_code and head.is_active:
            return head.emp_code
    vertical = (emp.vertical or '').strip()
    if not vertical:
        return ''
    head = (Employee.query
            .filter_by(vertical=vertical, is_vertical_head=True,
                       is_active=True)
            .filter(Employee.emp_code != emp_code)
            .order_by(Employee.id.asc()).first())
    return head.emp_code if head is not None else ''


def administrators():
    """Everyone who answers for the CRM itself."""
    from app import Employee
    from sqlalchemy import or_
    rows = (Employee.query
            .filter(Employee.is_active.is_(True))
            .filter(or_(Employee.role == 'admin',
                        Employee.is_super_admin.is_(True)))
            .order_by(Employee.id.asc()).all())
    return [e.emp_code for e in rows if e.emp_code]


def management():
    """Who the company-wide messages go to.

    Administrators by default. `REPORT_MANAGEMENT_CODES` overrides it
    with a comma-separated list of employee codes, for the common case
    where the people who want the monthly report are not the people who
    administer the system. Optional: unset, nothing changes.
    """
    import os
    raw = (os.environ.get('REPORT_MANAGEMENT_CODES') or '').strip()
    if raw:
        return [c.strip().upper() for c in raw.split(',') if c.strip()]
    return administrators()


def recipients_for(event, record, *, actor=None, escalate=False,
                   roles=None):
    """Who hears about this, in order and without repeats.

    `roles` overrides the row's own list, which is how the scheduled
    jobs address a report to one person without inventing a new event.
    """
    row = rule(event)
    if row is None:
        return []
    wanted = tuple(roles) if roles is not None else row['to']
    if escalate and row['escalate_to']:
        wanted = wanted + (row['escalate_to'],)

    owner = owner_of(record) if record is not None else ''
    out = []
    for role in wanted:
        if role == R_OWNER:
            out.append(owner)
        elif role == R_SECONDARY:
            out.append(secondary_of(record) if record is not None else '')
        elif role == R_PREPARER:
            out.append(_first_attr(record, ('prepared_by_id',))
                       if record is not None else '')
        elif role == R_APPROVER:
            out.append(_first_attr(record, ('approved_by_id',))
                       if record is not None else '')
        elif role == R_HEAD:
            out.append(vertical_head_for(owner))
        elif role == R_ADMIN:
            out.extend(administrators())
        elif role == R_MANAGEMENT:
            out.extend(management())
        elif role == R_ACTOR:
            out.append((actor or '').strip().upper())
        else:
            _logger().warning('notification_rules: unknown role %r on %r',
                              role, event)
    # Nobody is told twice, and nobody is told about their own action —
    # a person who just pressed the button does not need an email about
    # having pressed it.
    seen, ordered = set(), []
    actor_code = (actor or '').strip().upper()
    for code in out:
        code = (code or '').strip().upper()
        if not code or code in seen:
            continue
        if code == actor_code and R_ACTOR not in wanted:
            continue
        seen.add(code)
        ordered.append(code)
    return ordered


# ── wording ──────────────────────────────────────────────────────────
def describe(record):
    """A short, human name for whatever this record is.

    Reads the display fields the CRM already shows on screen, so the
    notification and the record agree about what the thing is called.
    """
    if record is None:
        return ''
    for attr in ('quote_number', 'rfq_number', 'opp_number'):
        value = getattr(record, attr, None)
        if value:
            subject = (getattr(record, 'subject', '') or '').strip()
            return f'{value}{" · " + subject if subject else ""}'[:160]
    for attr in ('project', 'name', 'company', 'subject'):
        value = (getattr(record, attr, '') or '').strip()
        if value:
            return value[:160]
    rid = getattr(record, 'id', None)
    return f'{type(record).__name__} #{rid}' if rid else ''


def _original_of(record):
    """The client's message and its files, as outbox attachment refs.

    Returns None when there is nothing to attach, which is the normal
    case for a lead that did not arrive by email.
    """
    lead_id = getattr(record, 'id', None)
    if lead_id is None or record.__class__.__name__ != 'Lead':
        return None
    try:
        from app.services import rfq_capture
        found = rfq_capture.summary_for_lead(lead_id)
    except Exception:
        return None
    out = []
    if found.get('original_id'):
        out.append({'kind': 'raw_email', 'id': found['original_id']})
    for att in found.get('attachments') or []:
        out.append({'kind': 'lead_attachment', 'id': att['id']})
    return out or None


def _fill(pattern, *, what, detail, actor):
    try:
        return (pattern or '').format(what=what, detail=detail, actor=actor)
    except Exception:
        # A row with a stray brace must not stop the notification: send
        # the pattern as written rather than nothing at all.
        return pattern or ''


# ── dispatch ─────────────────────────────────────────────────────────
def dispatch(event, record=None, *, actor=None, detail='', escalate=False,
             roles=None, url=None, title=None, body=None, email=None,
             entity_id=None, dedupe=True, email_html=None, attachments=None,
             batch_key=None, **ctx):
    """Tell everyone the matrix says should hear about this.

    Returns {emp_code: notify.send result}. Never raises: a caller's
    save has already happened by the time this runs, and a notification
    that cannot be delivered must not undo it.
    """
    try:
        row = rule(event)
        if row is None:
            _logger().warning('no notification rule for %r — nothing sent',
                              event)
            return {}
        from app.services import notify

        what = describe(record)
        actor_name = f'Actioned by {actor}.' if actor else ''
        subject = title or _fill(row['title'], what=what, detail=detail,
                                 actor=actor_name)
        text = body if body is not None else _fill(
            row['body'], what=what, detail=detail, actor=actor_name)
        rid = entity_id if entity_id is not None else getattr(
            record, 'id', None)
        link = url
        if link is None and row['url']:
            link = row['url'].format(id=rid) if rid is not None else None
        if escalate and row['escalate_to']:
            subject = f'Escalation: {subject}'

        if attachments is None and row.get('attach_original'):
            attachments = _original_of(record)

        people = recipients_for(event, record, actor=actor,
                                escalate=escalate, roles=roles)

        # Administrators copied on the events they chose, in the
        # settings screen rather than in this file. Appended to the
        # matrix's own recipients and de-duplicated below, so an
        # administrator who is also the owner is told once.
        try:
            from app.services import admin_settings
            people = list(people) + [
                code for code in admin_settings.admin_recipients(event)
                if code not in people]
        except Exception:
            pass
        out = {}
        for code in people:
            out[code] = notify.send(
                code,
                kind=row['kind'],
                title=subject,
                body=text,
                url=link,
                entity_type=row['entity_type'],
                entity_id=rid,
                email=(row['email'] if email is None else email),
                email_html=email_html,
                actor=actor,
                in_app=row['in_app'],
                dedupe=dedupe,
                event_key=event,
                attachments=attachments,
                batch_key=batch_key)
        return out
    except Exception:
        _logger().exception('notification dispatch failed for %r', event)
        return {}


def matrix_rows():
    """The matrix as plain rows, for the documentation generator and
    the tests. Deliberately not the dicts themselves — nothing outside
    should be able to edit the table in place."""
    return [dict(row) for row in MATRIX]
