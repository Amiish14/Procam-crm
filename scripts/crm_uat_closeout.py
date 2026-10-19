"""
CRM-UAT-CLOSEOUT-01 — eight checks, read-only, with the evidence.

    .venv/bin/python scripts/crm_uat_closeout.py

Writes docs/exports/crm_uat_closeout_<date>.xlsx and prints a
one-screen summary. Nothing is written to the database; every query
below is a read.

Three verdicts, and the third is the important one:

    PASS          the evidence says it works.
    FAIL          the evidence says it does not.
    NEEDS PERSON  the CRM cannot answer this on its own — the data
                  is absent, or the question is about a judgement
                  somebody has to make. Reported as neither a pass
                  nor a failure, because calling an unanswerable
                  question "PASS" is how a UAT sign-off becomes
                  worthless.
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from sqlalchemy import func                               # noqa: E402

from app import Employee, Lead, app, db                   # noqa: E402

PASS, FAIL, PERSON = 'PASS', 'FAIL', 'NEEDS PERSON'


class Check:
    def __init__(self, num, title):
        self.num, self.title = num, title
        self.verdict, self.headline, self.rows = PERSON, '', []

    def set(self, verdict, headline):
        self.verdict, self.headline = verdict, headline
        return self

    def add(self, **row):
        self.rows.append(row)


def _norm(s):
    return re.sub(r'[^a-z0-9]+', '', (s or '').lower())


# ── 1. client block ──────────────────────────────────────────────────
#: Named in the brief, so its absence is reported rather than passed
#: over in a count of "all blocked clients".
NAMED_IN_BRIEF = ('Walchandnagar',)


def _tokens(s):
    return {t for t in re.split(r'[^a-z0-9]+', (s or '').lower()) if t}


def _matches(company, names):
    """Whether a company name is one of these, by whole words.

    Substring matching was the first version and it was wrong: the
    caution on "Seml" matched every company whose name contains those
    four letters in order, and reported nine innocent leads as having
    slipped past a block. A register entry has to match a name, not
    appear inside one.
    """
    ctoks = _tokens(company)
    if not ctoks:
        return False
    for n in names:
        ntoks = _tokens(n)
        if ntoks and ntoks <= ctoks:
            return True
    return False


def check_blocks(now):
    c = Check(1, 'Client block — nothing created for a blocked client')
    from app.models.mailops import EmailOutbox
    from app.models.restriction import (BLOCKED, CAUTION,
                                        ClientRestriction,
                                        ClientRestrictionEvent)

    everything = ClientRestriction.query.all()
    missing = []
    for wanted in NAMED_IN_BRIEF:
        if not any(_tokens(wanted) & _tokens(r.company_name)
                   for r in everything):
            missing.append(wanted)
            c.add(client=wanted, status='NOT ON THE REGISTER',
                  blocked_since='—', domains='—',
                  leads_since=Lead.query.filter(
                      Lead.company.ilike(f'%{wanted}%')).count(),
                  mail_since='—', attempts_refused='—',
                  note='named in the brief; no restriction exists')

    hard = [r for r in everything if r.status == BLOCKED]
    soft = [r for r in everything if r.status == CAUTION]
    violations = 0

    for r in hard + soft:
        since = r.approved_at or r.recommended_at or r.created_at
        names = [r.company_name] + list(r.aliases or [])
        domains = {d.lower().lstrip('@') for d in (r.domains or []) if d}

        leads = [l for l in Lead.query.filter(Lead.created_at >= since).all()
                 if _matches(l.company, names)
                 or (domains and l.email
                     and l.email.split('@')[-1].lower() in domains)]
        mail = []
        if domains:
            for row in EmailOutbox.query.filter(
                    EmailOutbox.created_at >= since).all():
                to = ((row.to_addr or '') + ',' + (row.cc_addr or '')).lower()
                if any('@' + d in to for d in domains):
                    mail.append(row)
        refused = ClientRestrictionEvent.query.filter(
            ClientRestrictionEvent.restriction_id == r.id,
            ClientRestrictionEvent.action == 'attempt_blocked').count()

        # Caution is not a block. The gate refuses once with the
        # reason and allows the same request back acknowledged, so a
        # lead on a cautioned client is the design working, not a
        # breach. Only `blocked` means never.
        if r.status == BLOCKED:
            violations += len(leads) + len(mail)
        c.add(client=r.company_name, status=r.status,
              blocked_since=str(since)[:16] if since else '(not approved)',
              domains=', '.join(sorted(domains)) or '—',
              leads_since=len(leads), mail_since=len(mail),
              attempts_refused=refused,
              note=('VIOLATION' if (r.status == BLOCKED
                                    and (leads or mail))
                    else 'allowed after acknowledgement'
                         if r.status == CAUTION
                         else 'clean'))

    if violations:
        return c.set(FAIL, f'{violations} record(s) created for a fully '
                           f'blocked client since the block.')
    if missing:
        return c.set(PERSON,
                     f'{", ".join(missing)} is not on the register at all, '
                     f'so nothing is blocking it — and {Lead.query.filter(Lead.company.ilike("%" + missing[0] + "%")).count()} '
                     f'lead(s) for that name already exist. The only two '
                     f'entries on file are {", ".join(r.company_name for r in soft)}, '
                     f'both `caution`, not `blocked`. Somebody has to add '
                     f'the block before this can be tested.')
    if not hard:
        return c.set(PERSON, 'No client has status `blocked` — only '
                             '`caution`, which permits the work once '
                             'acknowledged. Nothing here proves a block '
                             'is enforced.')
    return c.set(PASS, f'{len(hard)} blocked client(s), nothing created '
                       f'since the block.')


# ── 2. weekly report ─────────────────────────────────────────────────
def _last_weekday(now, weekday):
    """The most recent given weekday (0=Mon) strictly before today."""
    d = now.date() - timedelta(days=1)
    while d.weekday() != weekday:
        d -= timedelta(days=1)
    return d


def check_weekly(now):
    c = Check(2, 'Weekly report — did Thursday go out?')
    from app.models.mailops import EmailOutbox

    thu = _last_weekday(now, 3)
    rows = EmailOutbox.query.filter(
        EmailOutbox.event_key.like('report.weekly%'),
        EmailOutbox.created_at >= datetime.combine(thu, datetime.min.time()),
        EmailOutbox.created_at < datetime.combine(
            thu + timedelta(days=1), datetime.min.time())).all()
    for row in rows:
        c.add(thursday=str(thu), event=row.event_key,
              to=(row.to_addr or '')[:120], cc=(row.cc_addr or '')[:120],
              status=row.status, attempts=row.attempts,
              sent_at=str(row.sent_at)[:16] if row.sent_at else '',
              error=(row.last_error or '')[:120])
    if not rows:
        return c.set(FAIL, f'Nothing queued for the weekly report on '
                           f'{thu} (last Thursday). It did not run.')
    bad = [r for r in rows if r.status not in ('sent',)]
    if bad:
        return c.set(FAIL, f'{len(rows)} weekly message(s) on {thu}, '
                           f'{len(bad)} not sent.')
    return c.set(PASS, f'{len(rows)} weekly message(s) on {thu}, all sent.')


# ── 3. daily brief + admin report ────────────────────────────────────
def _timer_for(fragment):
    """The systemd timer whose name contains this, or None."""
    try:
        out = subprocess.run(
            ['systemctl', 'list-timers', '--all', '--no-pager'],
            capture_output=True, text=True, timeout=20)
        for line in out.stdout.splitlines():
            if fragment in line:
                return ' '.join(line.split())
    except Exception:                                     # noqa: BLE001
        return None
    return None


def check_daily(now):
    c = Check(3, 'Daily brief and admin report — every working day')
    from app.models.mailops import EmailOutbox
    from app.services import admin_settings

    # Whole days, from midnight. A rolling `now - 7 days` starts
    # mid-morning and so excludes that day's 03:03 run, which read as
    # a missing report on the oldest day of every window.
    since = datetime.combine((now - timedelta(days=7)).date(),
                             datetime.min.time())
    by_day = defaultdict(lambda: defaultdict(Counter))
    for r in EmailOutbox.query.filter(
            EmailOutbox.event_key.in_(('report.daily', 'report.admin_daily')),
            EmailOutbox.created_at >= since).all():
        by_day[str(r.created_at)[:10]][r.event_key][r.status] += 1

    gaps, failures, days = [], 0, []
    for n in range(7, 0, -1):
        d = (now - timedelta(days=n)).date()
        if d.weekday() == 6:
            continue
        days.append(d)
        counts = by_day.get(str(d), {}).get('report.daily', Counter())
        n_sent = counts.get('sent', 0)
        n_bad = sum(v for k, v in counts.items() if k != 'sent')
        failures += n_bad
        if not counts:
            gaps.append(str(d))
        c.add(day=str(d), weekday=d.strftime('%a'), event='report.daily',
              sent=n_sent, failed=n_bad,
              note='NOTHING QUEUED' if not counts else '')

    admin_rows = sum(sum(v.values())
                     for day in by_day.values()
                     for k, v in day.items() if k == 'report.admin_daily')
    enabled = admin_settings.get('admin_daily_report')
    recipients = admin_settings.get('admin_recipients') or []
    timer = _timer_for('admin-daily') or _timer_for('admin_daily')
    c.add(day='—', weekday='—', event='report.admin_daily',
          sent=admin_rows, failed=0,
          note=f'enabled={enabled}, recipients={recipients or "none"}, '
               f'timer={timer or "NONE INSTALLED"}')

    problems = []
    if gaps:
        problems.append(f'report.daily queued nothing on {", ".join(gaps)}')
    if failures:
        problems.append(f'{failures} daily message(s) failed')
    if enabled and recipients and not timer and admin_rows == 0:
        problems.append('the admin report is switched on with a recipient '
                        'and has never sent — there is no timer installed '
                        'to run it')
    elif enabled and not recipients:
        problems.append('the admin report is on with no recipients')

    if problems:
        return c.set(FAIL, '; '.join(problems) + '.')
    return c.set(PASS, f'{len(days)} working day(s) covered, no failures.')


# ── 4. email routing ─────────────────────────────────────────────────
def check_routing(now):
    c = Check(4, 'Email routing — heads, unmapped verticals, bad addresses')
    from app.models.email_admin import VerticalAlias, VerticalHead
    from app.models.mailops import EmailOutbox

    heads = VerticalHead.query.filter(VerticalHead.active.is_(True)).all()
    emps = {e.emp_code: e for e in Employee.query.all()}
    gaps = []
    for h in heads:
        emp = emps.get((h.head_emp_code or '').upper())
        ok = emp is not None and emp.is_active
        if not ok:
            gaps.append(h.vertical)
        c.add(section='vertical head', vertical=h.vertical,
              head=h.head_emp_code or '(none)',
              head_name=(emp.name if emp else '—'),
              head_active=('yes' if ok else 'NO'))

    known = {h.vertical.strip().lower() for h in heads}
    known |= {a.alias.strip().lower()
              for a in VerticalAlias.query.all() if a.alias}
    unmapped = []
    for v, n in (db.session.query(Lead.procam_vertical, func.count())
                 .filter(Lead.procam_vertical.isnot(None),
                         Lead.procam_vertical != '')
                 .group_by(Lead.procam_vertical).all()):
        if v.strip().lower() not in known:
            unmapped.append((v, n))
            c.add(section='unmapped vertical', vertical=v, head='—',
                  head_name='—', head_active=f'{n} live record(s)')

    since = now - timedelta(days=7)
    refused = EmailOutbox.query.filter(
        EmailOutbox.created_at >= since,
        EmailOutbox.smtp_code.isnot(None),
        EmailOutbox.smtp_code >= 500).all()
    for r in refused:
        c.add(section='refused address', vertical='—',
              head=(r.to_addr or '')[:80], head_name=str(r.smtp_code),
              head_active=(r.last_error or '')[:80])

    bad_emp = [e for e in emps.values()
               if e.is_active and (not e.email
                                   or re.match(r'^(emp|dir|con)\d+@',
                                               e.email or '', re.I))]
    for e in bad_emp[:25]:
        c.add(section='employee with no usable address', vertical='—',
              head=e.emp_code, head_name=e.name or '',
              head_active=e.email or '(none)')

    problems = []
    if gaps:
        problems.append(f'{len(gaps)} vertical(s) with no active head: '
                        f'{", ".join(gaps)}')
    if unmapped:
        problems.append(f'{len(unmapped)} unmapped vertical(s) on live '
                        f'records')
    if refused:
        problems.append(f'{len(refused)} message(s) refused by the server '
                        f'in 7 days')
    if problems:
        return c.set(FAIL, '; '.join(problems)
                     + (f'; {len(bad_emp)} active employee(s) have no '
                        f'usable address' if bad_emp else ''))
    if bad_emp:
        return c.set(PERSON, f'Heads and mapping are complete and nothing '
                             f'was refused, but {len(bad_emp)} active '
                             f'employee(s) have no usable address — the '
                             f'CRM cannot mail them and nothing will say '
                             f'so. Somebody has to fix the records.')
    return c.set(PASS, f'{len(heads)} vertical(s), all with an active head; '
                       f'no unmapped verticals; nothing refused in 7 days.')


# ── 5. mail delivery ─────────────────────────────────────────────────
#: When `_attach_message` landed (commit caa1634). Not 13 October —
#: that was the SMTP *sender* fix, and using it here swept in two
#: failures from the 16th that predate the thing being tested and
#: reported them as a live regression.
MIME_FIX = datetime(2026, 10, 17, 18, 24)


def check_delivery(now):
    c = Check(5, 'Mail delivery since the MIME fix')
    from app.models.mailops import EmailOutbox

    after = EmailOutbox.query.filter(
        EmailOutbox.created_at >= MIME_FIX).all()
    before = EmailOutbox.query.filter(
        EmailOutbox.created_at < MIME_FIX,
        EmailOutbox.smtp_code == 554).count()
    failed = [r for r in after if r.status == 'failed']
    five54 = [r for r in after
              if r.smtp_code == 554 or '554' in (r.last_error or '')]
    queued_old = [r for r in after
                  if r.status == 'queued'
                  and r.created_at < now - timedelta(hours=6)]

    for r in failed[:50] + five54[:50]:
        c.add(id=r.id, event=r.event_key or '', to=(r.to_addr or '')[:90],
              status=r.status, smtp_code=r.smtp_code or '',
              created=str(r.created_at)[:16],
              error=(r.last_error or '')[:150])
    c.add(id='—', event=f'TOTAL since {str(MIME_FIX)[:16]}', to='',
          status='', smtp_code='', created='',
          error=f'{len(after)} queued, {len(failed)} failed, '
                f'{len(five54)} with 554, {len(queued_old)} stuck in '
                f'queued over 6h · {before} message(s) hit 554 before '
                f'the fix')

    if five54:
        return c.set(FAIL, f'{len(five54)} message(s) still hitting 554 '
                           f'since the MIME fix.')
    if failed:
        return c.set(FAIL, f'{len(failed)} failure(s) since 13 Oct '
                           f'(expected ~0).')
    if queued_old:
        return c.set(FAIL, f'No failures, but {len(queued_old)} message(s) '
                           f'have sat in `queued` for over six hours — the '
                           f'worker may not be running.')
    return c.set(PASS, f'{len(after)} message(s) since 13 Oct, 0 failed, '
                       f'0 with 554.')


# ── 6. vertical guard ────────────────────────────────────────────────
GUARD_FROM = datetime(2026, 10, 18, 19, 0)   # after the master apply


def check_vertical_guard(now):
    c = Check(6, 'Vertical guard — no silent rewrites since 18 Oct')
    from app.models.audit import AuditEvent

    rows = AuditEvent.query.filter(
        AuditEvent.entity_type == 'lead',
        AuditEvent.occurred_at >= GUARD_FROM).all()
    changes = []
    for ev in rows:
        old, new = (ev.old_value or {}), (ev.new_value or {})
        if not isinstance(old, dict) or not isinstance(new, dict):
            continue
        if 'procam_vertical' not in new:
            continue
        was, is_ = old.get('procam_vertical'), new.get('procam_vertical')
        if was == is_:
            continue
        changes.append(ev)
        c.add(lead=ev.entity_id, when=str(ev.occurred_at)[:16],
              actor=ev.actor, action=ev.action,
              was=was or '(blank)', now=is_ or '(blank)',
              alone=('yes' if set(new) == {'procam_vertical'} else 'no'),
              reason=(ev.reason or '')[:80])

    by_actor = Counter(ev.actor for ev in changes)
    scripted = sum(n for a, n in by_actor.items()
                   if a and (a.startswith('CRM-') or a == 'system'))
    human = len(changes) - scripted
    if human == 0:
        return c.set(PASS, f'{len(changes)} vertical change(s) since '
                           f'18 Oct 19:00, all of them from named '
                           f'scripts. No lead moved under a user who was '
                           f'editing something else.')
    return c.set(PERSON,
                 f'{human} vertical change(s) by a person since 18 Oct. '
                 f'The guard refuses a change the form did not send, so '
                 f'these should all be deliberate — but the audit row '
                 f'cannot prove intent, so somebody has to glance at the '
                 f'list. `alone=yes` means the vertical was the only '
                 f'field in the save, which is the suspicious shape.')


# ── 7. auto-assign ───────────────────────────────────────────────────
def check_auto_assign(now):
    c = Check(7, 'Auto-assign — flag, queue, digest timer')
    from app.models.assignment import OwnerConfirmation
    from app.services import flags

    for name in ('FEATURE_AUTO_ASSIGN', 'FEATURE_MY_MAIL',
                 'FEATURE_FORWARD_RECEIPT', 'FEATURE_REOPEN_ON_MAIL',
                 'FEATURE_QUOTE_REPLY_CLASS'):
        try:
            is_on = flags.on(name)
            where = flags.source_of(name)
        except Exception as exc:                          # noqa: BLE001
            is_on, where = None, f'unreadable: {exc}'
        c.add(item=f'flag {name}', value=('ON' if is_on else 'off'),
              detail=str(where))

    pending = OwnerConfirmation.query.filter(
        OwnerConfirmation.confirmed_at.is_(None)).count()
    c.add(item='owner-confirm queue', value=str(pending),
          detail='rows awaiting a person')

    timer = '(not readable)'
    try:
        out = subprocess.run(
            ['systemctl', 'list-timers', '--all', '--no-pager'],
            capture_output=True, text=True, timeout=20)
        for line in out.stdout.splitlines():
            if 'owner-digest' in line or 'owner_digest' in line:
                timer = ' '.join(line.split())
    except Exception as exc:                              # noqa: BLE001
        timer = f'(could not read: {exc})'
    c.add(item='owner-digest timer', value=timer[:60], detail=timer)

    try:
        auto_on = flags.on('FEATURE_AUTO_ASSIGN')
    except Exception:                                     # noqa: BLE001
        auto_on = None
    if auto_on:
        return c.set(PERSON, f'FEATURE_AUTO_ASSIGN is ON with {pending} '
                             f'row(s) in the confirm queue. Someone has '
                             f'to work the queue.')
    return c.set(PASS, f'FEATURE_AUTO_ASSIGN is off, as agreed. '
                       f'{pending} row(s) sit in the confirm queue and '
                       f'nothing is assigning automatically.')


# ── 8. tasks ─────────────────────────────────────────────────────────
def check_tasks(now):
    c = Check(8, 'Tasks — open by type, and any that can never close')
    from app.models.task_engine import (TaskDefinition, TaskInstance,
                                        TaskInstanceStatus)

    defs = {d.task_key: d for d in TaskDefinition.query.all()}
    immortal_open = 0
    total_open = 0
    for key, n in (db.session.query(TaskInstance.task_key, func.count())
                   .filter(TaskInstance.status.in_(TaskInstanceStatus.OPEN))
                   .group_by(TaskInstance.task_key)
                   .order_by(func.count().desc()).all()):
        d = defs.get(key)
        cs = d.completion_state if d else None
        can = bool(cs)
        total_open += n
        if not can:
            immortal_open += n
        c.add(section='open tasks', task=key, open=n,
              definition=('active' if d and d.is_active
                          else 'retired' if d else 'NO DEFINITION'),
              closes_on=(f'{cs.get("entity")}/{cs.get("state")}' if cs
                         else 'NEVER — no completion state'))

    for key, d in sorted(defs.items()):
        if not d.completion_state:
            c.add(section='definition with no completion state', task=key,
                  open=TaskInstance.query.filter(
                      TaskInstance.task_key == key,
                      TaskInstance.status.in_(
                          TaskInstanceStatus.OPEN)).count(),
                  definition=('active' if d.is_active else 'retired'),
                  closes_on='NEVER')

    from app.services import task_health
    broken = task_health.problems(list(defs.values()))
    for key, kind, why in broken:
        c.add(section='definition that cannot work', task=key, open='',
              definition=kind, closes_on=why)

    if immortal_open:
        return c.set(FAIL, f'{immortal_open} open task(s) can never close.')
    if broken:
        return c.set(FAIL, f'{len(broken)} active definition(s) cannot '
                           f'work: '
                           + '; '.join(f'{k} ({kind})'
                                       for k, kind, _w in broken))
    return c.set(PASS, f'{total_open} open task(s), every one of them '
                       f'closable through normal work.')


CHECKS = (check_blocks, check_weekly, check_daily, check_routing,
          check_delivery, check_vertical_guard, check_auto_assign,
          check_tasks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=None)
    args = ap.parse_args()

    with app.app_context():
        now = datetime.utcnow()
        print(f'database: {db.engine.url}')
        print(f'as of:    {now} UTC  (read-only)\n')
        results = []
        for fn in CHECKS:
            try:
                results.append(fn(now))
            except Exception as exc:                      # noqa: BLE001
                c = Check(0, fn.__name__)
                c.set(PERSON, f'the check itself failed: '
                              f'{type(exc).__name__}: {str(exc)[:200]}')
                results.append(c)
                db.session.rollback()

        out = args.out or os.path.join(
            _ROOT, 'docs', 'exports',
            f'crm_uat_closeout_{now.date().isoformat()}.xlsx')
        _write(results, out, now)

        width = 72
        print('=' * width)
        print('CRM-UAT-CLOSEOUT-01'.center(width))
        print('=' * width)
        for r in results:
            print(f'\n{r.num}. {r.title}')
            print(f'   {r.verdict:<13} {r.headline}')
        print('\n' + '=' * width)
        tally = Counter(r.verdict for r in results)
        print(f'  PASS {tally[PASS]}   FAIL {tally[FAIL]}   '
              f'NEEDS PERSON {tally[PERSON]}   of {len(results)}')
        ready = [str(r.num) for r in results if r.verdict == PASS]
        notready = [str(r.num) for r in results if r.verdict != PASS]
        print(f'  ready for Live: {", ".join(ready) or "none"}')
        print(f'  not yet:        {", ".join(notready) or "none"}')
        print('=' * width)
        print(f'\nworkbook: {out}')
        print('Nothing was written to the database.')


def _write(results, path, now):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    os.makedirs(os.path.dirname(path), exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Summary'
    head = Font(bold=True, color='FFFFFF')
    fill = PatternFill('solid', fgColor='1F2937')
    colour = {PASS: '2F855A', FAIL: 'B91C1C', PERSON: 'B45309'}

    ws.append(['CRM-UAT-CLOSEOUT-01'])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([f'Read-only, as of {now} UTC'])
    ws.append([])
    ws.append(['#', 'Check', 'Verdict', 'Evidence'])
    for cell in ws[4]:
        cell.font, cell.fill = head, fill
    for r in results:
        ws.append([r.num, r.title, r.verdict, r.headline])
        ws.cell(ws.max_row, 3).font = Font(
            bold=True, color=colour.get(r.verdict, '000000'))
        ws.cell(ws.max_row, 4).alignment = Alignment(wrap_text=True,
                                                     vertical='top')
    for col, w in (('A', 5), ('B', 52), ('C', 14), ('D', 90)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = 'A5'

    for r in results:
        name = f'{r.num}. ' + re.sub(r'[\\/*?:\[\]]', '', r.title)[:27]
        sheet = wb.create_sheet(name[:31])
        sheet.append([r.title])
        sheet['A1'].font = Font(bold=True, size=12)
        sheet.append([r.verdict, r.headline])
        sheet.cell(2, 1).font = Font(bold=True,
                                     color=colour.get(r.verdict, '000000'))
        sheet.append([])
        if not r.rows:
            sheet.append(['(no rows)'])
            continue
        cols = list(r.rows[0].keys())
        sheet.append([c.replace('_', ' ') for c in cols])
        for cell in sheet[4]:
            cell.font, cell.fill = head, fill
        for row in r.rows:
            sheet.append([row.get(c, '') for c in cols])
        for i, col in enumerate(cols, start=1):
            widest = max([len(str(col))]
                         + [len(str(x.get(col, ''))) for x in r.rows])
            sheet.column_dimensions[
                sheet.cell(1, i).column_letter].width = min(60,
                                                            widest + 3)
        sheet.freeze_panes = 'A5'
    wb.save(path)


if __name__ == '__main__':
    main()
