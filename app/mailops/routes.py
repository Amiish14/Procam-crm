"""
The screens for outbound email.

    /me/notifications             what one person wants to be told
    /admin/email/rules            the matrix, as the software holds it
    /admin/email/schedules        which report runs when, and to whom
    /admin/email/health           the queue: waiting, failed, refused
    /admin/email/settings         who is copied, and on what
    /admin/mail-ingest            every message the leads mailbox saw

The three admin screens need `admin.email`, the permission that already
governs the ingest settings — the person who looks after what comes in
is the person who looks after what goes out. The preferences page needs
nothing but a session: it only ever edits the signed-in person's own
row, and the emp_code comes from the session rather than from the form,
so one person cannot silence another.
"""
from __future__ import annotations

from functools import wraps

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   session, url_for)

from app.access import service as access

bp = Blueprint('mailops', __name__)


def _signed_in(f):
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            if request.path.startswith('/api/'):
                return jsonify(ok=False, error='Not authenticated'), 401
            return redirect(url_for('login'))
        return f(*a, **kw)
    return wrap


def _me():
    return (session.get('emp_code') or '').upper()


# ── one person's preferences ─────────────────────────────────────────
@bp.route('/me/notifications', methods=['GET', 'POST'])
@_signed_in
def preferences():
    from app.services import notification_rules as rules, notify_prefs

    me = _me()
    saved = False
    if request.method == 'POST':
        muted = [key for key in request.form.getlist('muted') if key]
        notify_prefs.save(
            me,
            email_enabled=bool(request.form.get('email_enabled')),
            mode=('batched' if request.form.get('mode') == 'batched'
                  else 'immediate'),
            batch_minutes=request.form.get('batch_minutes') or 15,
            quiet_enabled=bool(request.form.get('quiet_enabled')),
            quiet_start_hour=request.form.get('quiet_start_hour') or 21,
            quiet_end_hour=request.form.get('quiet_end_hour') or 7,
            daily_brief=bool(request.form.get('daily_brief')),
            weekly_pack=bool(request.form.get('weekly_pack')),
            muted_events=muted)
        saved = True

    prefs = notify_prefs.for_user(me)
    # `mode` is stored as text; save() only writes the two we offer, but
    # a row edited by hand should not make the form lie.
    events = [row for row in rules.matrix_rows() if row['email']]
    return render_template('mailops/preferences.html',
                           prefs=prefs, events=events, saved=saved,
                           urgent=notify_prefs.URGENT_EVENTS, me=me)


# ── the matrix ───────────────────────────────────────────────────────
@bp.route('/admin/email/rules')
@access.require('admin.email')
def rules_screen():
    from app.services import flags, notification_rules as rules

    rows = rules.matrix_rows()
    return render_template(
        'mailops/rules.html', rows=rows,
        implemented=sum(1 for r in rows if r['implemented']),
        flags=flags.all_flags())


# ── report schedules ─────────────────────────────────────────────────
#: What the timers in docs/operations/deploy/ do, written down where an
#: administrator can read it without a shell. The times are IST; the
#: unit files are UTC, and the conversion is here so the screen and the
#: unit cannot be read as saying different things.
SCHEDULES = (
    ('Daily action brief', 'procam-crm-daily-report',
     'Mon–Sat 08:30 IST', '03:00 UTC',
     'Everyone who owns a lead or an account, if their board is not empty',
     'FEATURE_DAILY_BRIEF'),
    ('End-of-day exceptions', 'procam-crm-exceptions-report',
     'Mon–Sat 19:00 IST', '13:30 UTC',
     'The same people — only what is still open', ''),
    ('Weekly, one person', 'procam-crm-weekly-report',
     'Mon 09:10 IST', '03:40 UTC', 'Everyone who owns work', ''),
    ('Weekly, vertical head', 'procam-crm-weekly-report',
     'Mon 09:10 IST', '03:40 UTC', 'Vertical heads', ''),
    ('Review pack', 'procam-crm-weekly-pack',
     'Thu 14:00 IST', '08:30 UTC',
     'Vertical heads and management, before the review',
     'FEATURE_WEEKLY_PACK'),
    ('Friday freeze', 'procam-crm-weekly-freeze',
     'Fri 09:00 IST', '03:30 UTC',
     'The week written down, then sent to heads and management',
     'FEATURE_WEEKLY_PACK'),
    ('Management report', 'procam-crm-monthly-report',
     '1st of the month, 09:00 IST', '03:30 UTC', 'Management', ''),
    ('Outbox worker', 'procam-crm-outbox',
     'every 2 minutes', '—', 'Sends whatever is queued',
     'FEATURE_EMAIL_NOTIFY'),
    ('Escalation sweep', 'procam-crm-escalation',
     'every 15 minutes', '—', 'The RFQ and quote ladder', ''),
)


@bp.route('/admin/email/schedules')
@access.require('admin.email')
def schedules_screen():
    from app.services import flags, weekly_pack

    try:
        latest = weekly_pack.frozen(weekly_pack.week_label())
    except Exception:
        latest = []
    return render_template('mailops/schedules.html', schedules=SCHEDULES,
                           flags=dict((n, s) for n, _d, s, _src in flags.all_flags()),
                           frozen=latest,
                           week=weekly_pack.week_label())


# ── health ───────────────────────────────────────────────────────────
@bp.route('/admin/email/health')
@access.require('admin.email')
def health_screen():
    from app.services import flags, leases, mail_policy, outbox

    try:
        report = outbox.health()
    except Exception as exc:                                  # noqa: BLE001
        report = {'totals': {}, 'recent': {}, 'queued': 0, 'failed': 0,
                  'oldest_queued_minutes': 0, 'failures': [],
                  'error': str(exc)[:200], 'last_hours': 24}
    return render_template('mailops/health.html', h=report,
                           policy=mail_policy.describe(),
                           leases=leases.status(),
                           flags=flags.all_flags())


@bp.route('/api/admin/email/health')
@access.require('admin.email')
def health_api():
    from app.services import outbox
    return jsonify(outbox.health())


@bp.route('/admin/email/health/retry', methods=['POST'])
@access.require('admin.email')
def health_retry():
    from app.services import audit, outbox

    ids = [int(i) for i in request.form.getlist('id') if str(i).isdigit()]
    moved = outbox.retry(ids) if ids else 0
    if moved:
        audit.record('email.outbox_retry', 'email_outbox', None,
                     new={'ids': ids[:50], 'count': moved},
                     reason='retried from the Email Health screen')
    return redirect(url_for('mailops.health_screen'))


@bp.route('/admin/email/health/cancel', methods=['POST'])
@access.require('admin.email')
def health_cancel():
    from app.services import audit, outbox

    ids = [int(i) for i in request.form.getlist('id') if str(i).isdigit()]
    stopped = outbox.cancel(ids) if ids else 0
    if stopped:
        audit.record('email.outbox_cancel', 'email_outbox', None,
                     new={'ids': ids[:50], 'count': stopped},
                     reason='cancelled from the Email Health screen')
    return redirect(url_for('mailops.health_screen'))


# ── notification settings ────────────────────────────────────────────
@bp.route('/admin/email/settings', methods=['GET', 'POST'])
@access.require('admin.email')
def settings_screen():
    """Who is copied on what, as a setting rather than as code."""
    from app.services import admin_settings, notification_rules as rules

    saved = False
    if request.method == 'POST':
        admin_settings.save(
            'admin_recipients',
            [c.strip().upper() for c in request.form.getlist('recipient')
             if c.strip()], actor=_me())
        admin_settings.save(
            'admin_copy_events',
            [e for e in request.form.getlist('copy_event') if e],
            actor=_me())
        admin_settings.save('admin_copies_enabled',
                            bool(request.form.get('admin_copies_enabled')),
                            actor=_me())
        admin_settings.save('admin_daily_report',
                            bool(request.form.get('admin_daily_report')),
                            actor=_me())
        stage = (request.form.get('reopen_stage') or '').strip()
        if stage:
            admin_settings.save('reopen_stage', stage, actor=_me())
        saved = True

    from app import Employee, STAGES_ALL
    people = (Employee.query
              .filter(Employee.is_active.isnot(False))
              .order_by(Employee.name).all())
    chosen = set(admin_settings.get('admin_recipients') or [])
    return render_template(
        'mailops/settings.html', saved=saved,
        people=[p for p in people if (p.email or '').strip()],
        chosen=chosen,
        events=rules.matrix_rows(),
        copy_events=set(admin_settings.get('admin_copy_events') or []),
        copies_on=admin_settings.get('admin_copies_enabled'),
        daily_report=admin_settings.get('admin_daily_report'),
        reopen_stage=admin_settings.get('reopen_stage'),
        stages=list(STAGES_ALL),
        descriptions=dict((k, d) for k, (_v, d)
                          in admin_settings.DEFAULTS.items()))


# ── the email log ────────────────────────────────────────────────────
@bp.route('/admin/email/log')
@access.require('admin.email')
def email_log():
    """Every outbound message, with what happened to it."""
    from app.models.mailops import EmailOutbox
    from app.services import mailer

    status = (request.args.get('status') or '').strip()
    search = (request.args.get('q') or '').strip()
    page = max(1, int(request.args.get('page') or 1))
    size = min(200, max(10, int(request.args.get('size') or 50)))

    query = EmailOutbox.query
    if status:
        query = query.filter(EmailOutbox.status == status)
    if search:
        like = f'%{search.lower()}%'
        from app import db
        query = query.filter(db.or_(
            db.func.lower(EmailOutbox.to_addr).like(like),
            db.func.lower(EmailOutbox.subject).like(like),
            db.func.lower(db.func.coalesce(EmailOutbox.event_key, ''))
            .like(like)))
    total = query.order_by(None).count()
    rows = (query.order_by(EmailOutbox.id.desc())
            .limit(size).offset((page - 1) * size).all())

    ok, why = mailer.ready()
    return render_template(
        'mailops/log.html', rows=rows, total=total, page=page, size=size,
        pages=max(1, (total + size - 1) // size), status=status, q=search,
        transport=mailer.describe(), transport_ok=ok, transport_why=why)


@bp.route('/admin/email/log/resend', methods=['POST'])
@access.require('admin.email')
def email_log_resend():
    """Put chosen messages back in the queue.

    Idempotent by construction: the row is re-queued rather than
    copied, so pressing this twice schedules one send, not two. A row
    already sent is left alone — re-sending a delivered message is a
    different decision and needs a different button.
    """
    from app.services import audit, outbox

    ids = [int(i) for i in request.form.getlist('id') if str(i).isdigit()]
    moved = outbox.retry(ids) if ids else 0
    if moved:
        audit.record('email.resend', 'email_outbox', None,
                     new={'ids': ids[:50], 'count': moved},
                     reason='resent from the Email Log')
    return redirect(url_for('mailops.email_log'))


@bp.route('/admin/email/log/test', methods=['POST'])
@access.require('admin.email')
def email_log_test():
    """Send a test to the signed-in person, now, bypassing the queue.

    Deliberately direct: the question this answers is "does the
    transport work", and routing it through the queue would answer
    "does the queue work" a minute later instead.
    """
    from app import Employee
    from app.services import mailer

    emp = Employee.query.filter_by(emp_code=_me()).first()
    address = (getattr(emp, 'email', '') or '').strip()
    if not address:
        return jsonify(ok=False, error='You have no email address on '
                                       'file'), 400
    sent = mailer.send_test(address)
    return jsonify(ok=bool(sent), to=address,
                   transport=mailer.transport(),
                   error='' if sent else 'The transport refused it — see '
                                         'the journal for why')


# ── the mail ingestion log ───────────────────────────────────────────
@bp.route('/admin/mail-ingest')
@access.require('admin.email')
def ingest_log():
    """What happened to every message the leads mailbox saw."""
    from app import db
    from app.models.ingest_log import MailIngestLog, OUTCOMES

    outcome = (request.args.get('outcome') or '').strip()
    search = (request.args.get('q') or '').strip()
    since = (request.args.get('from') or '').strip()
    until = (request.args.get('to') or '').strip()
    page = max(1, int(request.args.get('page') or 1))
    size = min(200, max(10, int(request.args.get('size') or 50)))

    query = MailIngestLog.query
    if outcome:
        query = query.filter(MailIngestLog.outcome == outcome)
    if search:
        like = f'%{search.lower()}%'
        query = query.filter(db.or_(
            db.func.lower(db.func.coalesce(MailIngestLog.subject, ''))
            .like(like),
            db.func.lower(db.func.coalesce(MailIngestLog.from_addr, ''))
            .like(like)))
    for raw, column, op in ((since, MailIngestLog.received_at, 'ge'),
                            (until, MailIngestLog.received_at, 'le')):
        if not raw:
            continue
        try:
            from datetime import datetime
            when = datetime.strptime(raw[:10], '%Y-%m-%d')
            query = (query.filter(column >= when) if op == 'ge'
                     else query.filter(column <= when.replace(
                         hour=23, minute=59, second=59)))
        except ValueError:
            pass

    total = query.order_by(None).count()
    rows = (query.order_by(MailIngestLog.received_at.desc(),
                           MailIngestLog.id.desc())
            .limit(size).offset((page - 1) * size).all())

    counts = dict(db.session.query(MailIngestLog.outcome,
                                   db.func.count(MailIngestLog.id))
                  .group_by(MailIngestLog.outcome).all())
    return render_template(
        'mailops/ingest_log.html', rows=rows, total=total, page=page,
        size=size, pages=max(1, (total + size - 1) // size),
        outcome=outcome, q=search, since=since, until=until,
        outcomes=OUTCOMES, counts=counts)


@bp.route('/admin/mail-ingest/<int:rid>/create-lead', methods=['POST'])
@access.require('admin.email')
def ingest_create_lead(rid):
    """Turn a message the classifier declined into a lead.

    The reason the skipped rows are kept at all. The message is
    re-fetched from the mailbox and put through the ordinary ingest
    with the classifier stood down, so the lead that results is the
    same shape as any other — not a hand-built row that behaves
    differently a month later.
    """
    from app import db
    from app.models.ingest_log import MailIngestLog
    from app.services import audit

    row = MailIngestLog.query.get(rid)
    if row is None:
        return jsonify(ok=False, error='Not found'), 404
    if row.lead_id:
        return jsonify(ok=False, error=f'Already lead #{row.lead_id}'), 400
    if not row.internet_message_id:
        return jsonify(ok=False, error='No message id — nothing to '
                                       'fetch'), 400

    try:
        from email_ingest import raw_mime, service as mail_service
        from email_ingest.graph_client import GraphClient
        from email_ingest.single_message import process_single_message
        from email_ingest.webhook import _get_message_by_internet_id

        mailbox = mail_service.crm_inbox_email()
        graph = GraphClient()
        msg = _get_message_by_internet_id(graph, mailbox,
                                          row.internet_message_id)
        if not msg:
            return jsonify(ok=False,
                           error='The mailbox no longer has that '
                                 'message'), 404
        result = process_single_message(graph, mailbox, msg, force=True,
                                        override_classifier=True)
    except Exception as exc:                                  # noqa: BLE001
        return jsonify(ok=False, error=str(exc)[:200]), 500

    lead_id = (result or {}).get('lead_id')
    row.lead_id = lead_id
    row.outcome = 'created' if lead_id else row.outcome
    row.overridden_by = _me()
    from datetime import datetime
    row.overridden_at = datetime.utcnow()
    row.reason = (f'Created by {_me()} from the ingestion log '
                  f'(was: {row.reason or "skipped"})')[:500]
    db.session.commit()
    audit.record('mail_ingest.override', 'MailIngestLog', row.id,
                 new={'lead_id': lead_id}, actor=_me(),
                 reason='administrator created a lead from a skipped email')
    return jsonify(ok=bool(lead_id), lead_id=lead_id,
                   error='' if lead_id else
                   (result or {}).get('reason', 'nothing was created'))


# ── the vertical head master ─────────────────────────────────────────
def _may_edit_heads():
    """Editing the master is Super Admin and Admin only.

    Vertical heads can read it — they are the people most likely to
    notice that the master names the wrong person for their own
    vertical — and that is the whole of their access. See `_head_view`.
    """
    return access.is_super() or access.can('admin.email')


def _head_view(f):
    """Readable by an administrator or by a vertical head."""
    @wraps(f)
    def wrap(*a, **kw):
        if not session.get('emp_code'):
            return redirect(url_for('login'))
        if _may_edit_heads():
            return f(*a, **kw)
        from app import Employee
        emp = Employee.query.filter_by(emp_code=_me()).first()
        if emp is not None and getattr(emp, 'is_vertical_head', False):
            return f(*a, **kw)
        return render_template('access_denied.html', need='admin.email'), 403
    return wrap


@bp.route('/admin/email/verticals')
@_head_view
def verticals_screen():
    from app.models.email_admin import VerticalAlias, VerticalHead
    from app.services import vertical_heads

    from app import Employee
    rows = VerticalHead.query.order_by(VerticalHead.vertical).all()
    people = (Employee.query
              .filter(Employee.is_active.isnot(False))
              .order_by(Employee.name).all())
    by_code = {p.emp_code: p for p in people}
    return render_template(
        'mailops/verticals.html',
        rows=rows,
        aliases=VerticalAlias.query.order_by(VerticalAlias.vertical,
                                             VerticalAlias.alias).all(),
        people=[p for p in people if (p.email or '').strip()],
        by_code=by_code,
        unmapped=vertical_heads.unmapped_verticals(),
        gaps=vertical_heads.gaps(),
        can_edit=_may_edit_heads(),
        fallback=vertical_heads.FALLBACK_VERTICAL)


@bp.route('/admin/email/verticals/save', methods=['POST'])
@access.require('admin.email')
def verticals_save():
    from app import db
    from app.models.email_admin import VerticalHead
    from app.services import audit, vertical_heads

    vertical = (request.form.get('vertical') or '').strip()
    if not vertical:
        return redirect(url_for('mailops.verticals_screen'))

    row = VerticalHead.query.filter_by(vertical=vertical).first()
    if row is None:
        row = VerticalHead(vertical=vertical)
        db.session.add(row)
    was = {'head': row.head_emp_code, 'deputy': row.deputy_emp_code,
           'active': row.active}
    row.head_emp_code = (request.form.get('head') or '').strip().upper() or None
    row.deputy_emp_code = ((request.form.get('deputy') or '').strip().upper()
                           or None)
    row.active = bool(request.form.get('active'))
    row.note = (request.form.get('note') or '').strip()[:300] or None
    row.updated_by = _me()
    db.session.commit()
    vertical_heads.cache_clear()
    audit.record('vertical_head.changed', 'VerticalHead', vertical,
                 old=was,
                 new={'head': row.head_emp_code,
                      'deputy': row.deputy_emp_code, 'active': row.active},
                 actor=_me(), reason='changed in Admin → Email → Verticals')
    # The old flag and the new master must not disagree: code written
    # before this table reads `employees.is_vertical_head`.
    vertical_heads.sync_employee_flags(actor=_me())
    return redirect(url_for('mailops.verticals_screen'))


@bp.route('/admin/email/verticals/alias', methods=['POST'])
@access.require('admin.email')
def verticals_alias():
    from app import db
    from app.models.email_admin import VerticalAlias
    from app.services import audit, vertical_heads

    alias = (request.form.get('alias') or '').strip()
    vertical = (request.form.get('maps_to') or '').strip()
    remove = request.form.get('remove')

    if alias and remove:
        row = VerticalAlias.query.filter_by(alias=alias).first()
        if row is not None:
            db.session.delete(row)
            db.session.commit()
            audit.record('vertical_alias.removed', 'VerticalAlias', alias,
                         old={'vertical': vertical}, actor=_me(),
                         reason='removed in Admin → Email → Verticals')
    elif alias and vertical:
        row = VerticalAlias.query.filter_by(alias=alias).first()
        if row is None:
            row = VerticalAlias(alias=alias)
            db.session.add(row)
        row.vertical = vertical
        row.updated_by = _me()
        db.session.commit()
        audit.record('vertical_alias.changed', 'VerticalAlias', alias,
                     new={'vertical': vertical}, actor=_me(),
                     reason='mapped in Admin → Email → Verticals')
    vertical_heads.cache_clear()
    return redirect(url_for('mailops.verticals_screen'))


# ── the recipient policy ─────────────────────────────────────────────
@bp.route('/admin/email/recipients', methods=['GET', 'POST'])
@access.require('admin.email')
def recipients_screen():
    """To and CC per event, and the standing copy list."""
    from app.services import (admin_settings, notification_policy,
                              notification_rules as rules)

    saved = error = ''
    if request.method == 'POST':
        what = request.form.get('what') or 'event'
        try:
            if what == 'always_copy':
                admin_settings.save(
                    'always_copy',
                    [c.strip().upper()
                     for c in request.form.getlist('always') if c.strip()],
                    actor=_me())
                admin_settings.save(
                    'always_copy_enabled',
                    bool(request.form.get('always_copy_enabled')),
                    actor=_me())
                saved = 'The standing copy list is saved.'
            else:
                event = (request.form.get('event') or '').strip()
                if request.form.get('reset'):
                    notification_policy.reset(event, actor=_me())
                    saved = f'{event} is back to the built-in list.'
                else:
                    version = notification_policy.save(
                        event,
                        to_roles=[r for r in request.form.getlist('to') if r],
                        cc_roles=[r for r in request.form.getlist('cc') if r],
                        email_enabled=bool(request.form.get('email_enabled')),
                        bell_enabled=bool(request.form.get('bell_enabled')),
                        actor=_me())
                    saved = f'{event} saved — policy version {version}.'
        except ValueError as exc:
            error = str(exc)

    from app import Employee
    people = (Employee.query
              .filter(Employee.is_active.isnot(False))
              .order_by(Employee.name).all())
    return render_template(
        'mailops/recipients.html',
        rows=notification_policy.rows(),
        roles=rules.ROLES,
        titles=dict((r['event'], r['title']) for r in rules.matrix_rows()),
        people=[p for p in people if (p.email or '').strip()],
        always=set(notification_policy.always_copy()),
        always_on=admin_settings.get('always_copy_enabled'),
        saved=saved, error=error,
        focus=(request.args.get('event') or '').strip())


# ── CRM settings (decisions of 21 Oct 2026) ──────────────────────────
#
# Served at /admin/crm-settings, not /admin/crm-rules. That URL was
# taken while this branch sat unmerged: `app/admin_rules` is the
# pilot-readiness panel, which reads what the pilot still needs and
# is linked from four places in its own spec. Two blueprints on one
# rule means whichever registers first wins and the other silently
# disappears, which is how a screen goes missing without an error.
# This one is an editor and that one is a report; they are different
# screens and now have different addresses.
@bp.route('/admin/crm-settings', methods=['GET', 'POST'])
@access.require('admin.email')
def crm_rules_screen():
    """Tender desk, margin minimums, approvers, viewers, validity."""
    from app.services import admin_settings

    saved = error = ''
    if request.method == 'POST':
        parsed = {}
        try:
            for key in admin_settings.RULE_KEYS:
                if key in request.form:
                    parsed[key] = admin_settings.parse_rule(
                        key, request.form.get(key))
        except ValueError as exc:
            error = str(exc)
        if not error:
            for key, value in parsed.items():
                admin_settings.save(key, value, actor=_me())
            saved = 'The rules are saved.'
    rows = [(key, admin_settings.show_rule(key),
             admin_settings.DEFAULTS[key][1],
             isinstance(admin_settings.DEFAULTS[key][0], dict))
            for key in admin_settings.RULE_KEYS]
    return render_template('mailops/crm_rules.html', rows=rows,
                           saved=saved, error=error)


# ── the switches ─────────────────────────────────────────────────────
@bp.route('/admin/email/switches', methods=['GET', 'POST'])
@access.require('admin.email')
def switches_screen():
    """Feature flags and the throttle, changeable without a restart."""
    from app.services import admin_settings, flags

    saved = ''
    if request.method == 'POST':
        what = request.form.get('what') or 'flags'
        if what == 'throttle':
            raw = (request.form.get('email_max_per_minute') or '').strip()
            try:
                admin_settings.save('email_max_per_minute', int(raw),
                                    actor=_me())
                saved = 'The throttle is saved.'
            except ValueError:
                saved = 'That is not a number — the throttle is unchanged.'
        else:
            wanted = set(request.form.getlist('flag'))
            for name in flags.NAMES:
                if request.form.get('clear_' + name):
                    flags.clear(name, actor=_me())
                    continue
                flags.set(name, name in wanted, actor=_me())
            saved = 'The switches are saved — no restart needed.'

    from app.services import outbox
    return render_template(
        'mailops/switches.html', flags=flags.all_flags(), saved=saved,
        kill_switch=flags.KILL_SWITCH,
        throttle=admin_settings.get('email_max_per_minute'),
        sent_last_minute=outbox.sent_in_last_minute(),
        descriptions=dict((k, d) for k, (_v, d)
                          in admin_settings.DEFAULTS.items()))


# ── employee addresses ───────────────────────────────────────────────
@bp.route('/admin/email/people')
@access.require('admin.email')
def people_screen():
    """Who cannot receive CRM mail, and why."""
    from app.services import employee_email

    return render_template(
        'mailops/people.html',
        bad=employee_email.unreachable(),
        domains=employee_email.allowed_domains())


@bp.route('/admin/email/people/import', methods=['GET', 'POST'])
@access.require('admin.email')
def people_import():
    """Preview an HRMS export, then apply only what it changes."""
    import json
    import os
    import tempfile

    from app.services import employee_email

    plan, problem, report = [], '', None
    if request.method == 'POST' and request.form.get('confirm'):
        # The plan the person looked at, returned by the form, so what
        # is applied is what was shown — not a second read of a file
        # that may have been replaced in between.
        try:
            plan = json.loads(request.form.get('plan') or '[]')
        except Exception:
            plan = []
        report = employee_email.apply_plan(plan, actor=_me())
        plan = []
    elif request.method == 'POST':
        upload = request.files.get('file')
        if upload is None or not (upload.filename or '').strip():
            problem = 'Choose a CSV or XLSX file first.'
        else:
            suffix = os.path.splitext(upload.filename)[1][:10] or '.csv'
            handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
            try:
                upload.save(handle.name)
                handle.close()
                plan, problem = employee_email.preview(handle.name)
            finally:
                try:
                    os.unlink(handle.name)
                except OSError:
                    pass

    import json as _json
    return render_template('mailops/people_import.html', plan=plan,
                           plan_json=_json.dumps(plan), problem=problem,
                           report=report,
                           counts=_count_actions(plan))


def _count_actions(plan):
    out = {}
    for item in (plan or []):
        out[item.get('action')] = out.get(item.get('action'), 0) + 1
    return out


# ── My Mail ──────────────────────────────────────────────────────────
def _is_admin():
    return access.is_super() or access.can('admin.email')


@bp.route('/my-mail')
@_signed_in
def my_mail():
    """What happened to the mail you forwarded to leads@.

    Every user, not administrators only. The whole complaint was that
    the record existed and the person who needed it could not reach
    it.
    """
    from app.services import flags, my_mail as svc

    if not flags.on('FEATURE_MY_MAIL'):
        return render_template('mailops/my_mail_off.html'), 200

    me, admin = _me(), _is_admin()
    rows = svc.rows(me, is_admin=admin,
                    outcome=(request.args.get('outcome') or '').strip(),
                    search=(request.args.get('q') or '').strip(),
                    forwarded_by=(request.args.get('by') or '').strip(),
                    days=min(365, max(1, int(request.args.get('days')
                                             or svc.WINDOW_DAYS))))
    forwarders = []
    if admin:
        from app import Employee, db
        from app.models.ingest_log import MailIngestLog
        codes = [c for (c,) in db.session.query(
            MailIngestLog.forwarded_by_emp).distinct() if c]
        if codes:
            forwarders = (Employee.query
                          .filter(Employee.emp_code.in_(codes))
                          .order_by(Employee.name).all())
    return render_template(
        'mailops/my_mail.html', rows=rows, me=me, is_admin=admin,
        counts=svc.counts(me, is_admin=admin),
        outcomes=svc.OUTCOME_LABELS,
        forwarders=forwarders,
        focus=(request.args.get('msg') or '').strip(),
        outcome=(request.args.get('outcome') or '').strip(),
        search=(request.args.get('q') or '').strip(),
        by=(request.args.get('by') or '').strip(),
        days=(request.args.get('days') or svc.WINDOW_DAYS))


@bp.route('/api/my-mail/badge')
@_signed_in
def my_mail_badge():
    """How many of my forwards were skipped this week, for the nav."""
    from app.services import flags, my_mail as svc

    if not flags.on('FEATURE_MY_MAIL'):
        return jsonify(ok=True, skipped=0)
    return jsonify(ok=True, skipped=svc.badge(_me(), is_admin=_is_admin()))


@bp.route('/my-mail/<int:row_id>/original')
@_signed_in
def my_mail_original(row_id):
    """The message as it arrived, as a .eml.

    Access is checked against the row, not against the URL: a row id
    is not proof that it is yours.
    """
    import os

    from flask import abort, send_file

    from app.models.ingest_log import MailIngestLog
    from app.services import my_mail as svc

    row = MailIngestLog.query.get_or_404(row_id)
    if not svc.may_see(_me(), row, is_admin=_is_admin()):
        return render_template('access_denied.html',
                               need='your own forwarded mail'), 403
    path = row.raw_eml_path or ''
    if not path or not os.path.exists(path):
        abort(404)
    name = (row.subject or 'original')[:80].strip() or 'original'
    name = ''.join(c for c in name if c.isalnum() or c in ' -_()') + '.eml'
    return send_file(path, as_attachment=True, download_name=name,
                     mimetype='message/rfc822')


# ── owner confirmation, and the replacement map ──────────────────────
@bp.route('/owner-confirm', methods=['GET', 'POST'])
@_signed_in
def owner_confirm():
    """Leads a rule assigned to me, for me to keep or pass on.

    The buttons in the morning digest land here. A GET with
    `do=confirm` is the one-click path; everything else shows the
    list. Confirming is a POST, because a link that changes data on
    GET is a link a mail scanner can press.
    """
    from app.services import auto_assign, flags, owner_digest

    if not flags.on('FEATURE_AUTO_ASSIGN'):
        return render_template('mailops/my_mail_off.html'), 200

    me, admin = _me(), _is_admin()
    message = ''

    if request.method == 'POST':
        lead_id = int(request.form.get('lead') or 0)
        outcome = (request.form.get('outcome') or '').strip()
        to_whom = (request.form.get('to') or '').strip().upper()

        row = _confirmation(lead_id)
        if row is None:
            message = 'Nothing is waiting on that lead.'
        elif row.emp_code != me and not admin:
            return render_template('access_denied.html',
                                   need='this lead to be yours'), 403
        else:
            ok, why = auto_assign.answer(
                lead_id, outcome=outcome, by=me,
                reassign_to=to_whom or None)
            message = ('Done.' if ok else f'Not done: {why}')

    mine = auto_assign.pending_for(me if not admin else None) \
        if not admin else owner_digest.pending()
    rows = owner_digest.rows_for(mine)

    from app import Employee
    people = (Employee.query
              .filter(Employee.is_active.is_(True))
              .order_by(Employee.name).all())
    return render_template(
        'mailops/owner_confirm.html', rows=rows, me=me, is_admin=admin,
        people=people, message=message,
        focus=(request.args.get('lead') or '').strip(),
        quick=(request.args.get('do') or '').strip())


def _confirmation(lead_id):
    from app.models.assignment import OwnerConfirmation
    try:
        return OwnerConfirmation.query.filter_by(
            lead_id=lead_id, confirmed_at=None).first()
    except Exception:
        from app import db
        db.session.rollback()
        return None


@bp.route('/admin/assignment/replacements', methods=['GET', 'POST'])
@access.require('admin.employees')
def replacements_screen():
    """Where a departed person's leads go. A table, not code.

    `admin.employees` rather than `admin.email`: this is about who
    works here, and the person who maintains the employee master is
    the person who knows somebody has left.
    """
    from app import Employee, db
    from app.models.assignment import OwnerReplacement
    from app.services import audit

    saved = error = ''
    if request.method == 'POST':
        from_code = (request.form.get('from') or '').strip().upper()
        to_code = (request.form.get('to') or '').strip().upper()
        remove = request.form.get('remove')

        row = (OwnerReplacement.query.filter_by(from_emp_code=from_code)
               .first() if from_code else None)
        if remove and row is not None:
            db.session.delete(row)
            db.session.commit()
            audit.record('assignment.replacement_removed', 'OwnerReplacement',
                         from_code, actor=_me(),
                         reason='removed in Admin → Assignment')
            saved = f'{from_code} removed.'
        elif not from_code or not to_code:
            error = 'Both people are needed.'
        elif from_code == to_code:
            error = 'Somebody cannot replace themselves.'
        else:
            successor = Employee.query.filter_by(emp_code=to_code).first()
            if successor is None:
                error = f'{to_code} is not an employee on file.'
            elif not successor.is_active:
                error = (f'{successor.name} is not active either — their '
                         f'leads would need replacing too.')
            else:
                was = row.to_emp_code if row is not None else None
                if row is None:
                    row = OwnerReplacement(from_emp_code=from_code)
                    db.session.add(row)
                row.to_emp_code = to_code
                row.active = bool(request.form.get('active'))
                row.note = (request.form.get('note') or '').strip()[:200]
                row.updated_by = _me()
                db.session.commit()
                audit.record('assignment.replacement_changed',
                             'OwnerReplacement', from_code,
                             old={'to': was}, new={'to': to_code},
                             actor=_me(),
                             reason='changed in Admin → Assignment')
                saved = f'{from_code} → {to_code} saved.'

    rows = OwnerReplacement.query.order_by(
        OwnerReplacement.from_emp_code).all()
    by_code = {e.emp_code: e for e in Employee.query.all()}
    return render_template(
        'mailops/replacements.html', rows=rows, by_code=by_code,
        inactive=[e for e in by_code.values() if not e.is_active],
        active=sorted([e for e in by_code.values() if e.is_active],
                      key=lambda e: (e.name or '')),
        saved=saved, error=error)
