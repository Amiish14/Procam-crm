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
                           flags=dict((n, s) for n, _d, s in flags.all_flags()),
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
