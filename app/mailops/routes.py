"""
The screens for outbound email.

    /me/notifications             what one person wants to be told
    /admin/email/rules            the matrix, as the software holds it
    /admin/email/schedules        which report runs when, and to whom
    /admin/email/health           the queue: waiting, failed, refused

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
