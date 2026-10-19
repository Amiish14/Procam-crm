"""
CRM-FIGURES-01 — the six pictures, read from production.

    .venv/bin/python scripts/crm_figures.py

Writes docs/exports/crm_figures_<date>.xlsx and prints a summary with
client names masked. Nothing is written to the database; every query
here is a read, and it is safe to run during the working day.

The masking is only on the printed summary. The workbook carries the
real names, because a figure you cannot act on is not worth
producing — the summary is the thing that gets pasted into a chat
window or an email and the file is the thing that stays in
docs/exports.

Where a number cannot be produced honestly it says so rather than
printing a zero. Several of these tables are empty on this system —
`rfqs` and `quotes` have been built and never used — and a report
that answers "0 quotes sent" without saying the table is empty is
worse than no report, because it looks like an answer.
"""
import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from sqlalchemy import func, inspect                      # noqa: E402

from app import (Company, Employee, Lead, STAGES_TERMINAL,  # noqa: E402
                 app, db)

WINDOW_30 = 30
WINDOW_90 = 90

#: A day on which this many leads reached a terminal stage was not a
#: day of trading. On 10 October one person closed 2,381 leads as "No
#: Response" in a single clean-up; counted as trading it drags the
#: win rate to 0% and says nothing true about the business. Bulk days
#: are reported separately rather than dropped silently.
BULK_DAY = 100

#: Tables the CRM 2.0 brief expects. Taken from
#: `scripts/stage_a_review.py`, which encodes the parts of that brief
#: that touch the schema, plus the quote tables named in the request.
#: The brief itself is not in this repository, so this list is what
#: can be checked rather than everything the brief asks for — said
#: out loud in the sheet, because a readiness report that looks
#: complete and is not is the worst kind.
CRM2_TABLES = ('crm_parameter', 'holiday_calendar', 'quotes',
               'quote_lines', 'quote_versions', 'rfqs',
               'rate_sourcing_lines', 'won_handovers')

#: The 12 October Freight Forwarding decisions, as given in the
#: request. Each is checked against what is actually in the system.
FF_DECISIONS = (
    ('crm@ / tms@ mailboxes',
     'Two mailboxes, one per application.'),
    ('CRN scheme <BU>-<C|T><FY>-<NNNNNN>',
     'A client reference number in that shape.'),
    ('FF Client PO created in TMS',
     'The purchase order belongs to the TMS, not the CRM.'),
    ('Incoterm billing',
     'Billing driven by the Incoterm on the shipment.'),
    ('Own CHA only Chennai and Mumbai',
     'Customs house agency in-house at two ports only.'),
)


def _mask(name):
    """A client name reduced to something safe to paste.

    First word kept, the rest initialled: "Alstom Transport India"
    becomes "Alstom T. I.". Enough to recognise a deal you already
    know about, not enough to be a client list.
    """
    parts = [p for p in re.split(r'\s+', (name or '').strip()) if p]
    if not parts:
        return '(no name)'
    if len(parts) == 1:
        return parts[0][:18]
    return parts[0][:18] + ' ' + ' '.join(p[0].upper() + '.'
                                          for p in parts[1:4])


def _age_days(lead, today):
    base = lead.stage_entered_at or lead.updated_at or lead.created_at
    if base is None:
        return None
    return (today - base.date()).days if hasattr(base, 'date') else None


def _value(lead):
    for attr in ('quoted_amount_inr', 'estimated_value_inr'):
        v = getattr(lead, attr, None)
        if v:
            return float(v)
    return 0.0


class Sheet:
    def __init__(self, num, title, note=''):
        self.num, self.title, self.note = num, title, note
        self.rows, self.caveats = [], []

    def add(self, **row):
        self.rows.append(row)

    def caveat(self, text):
        self.caveats.append(text)


# ── 1. pipeline ──────────────────────────────────────────────────────
def pipeline(now, today):
    s = Sheet(1, 'Pipeline by vertical, stage and owner')
    names = {e.emp_code: e.name for e in Employee.query.all()}
    leads = Lead.query.filter(Lead.is_archived.isnot(True)).all()
    live = [l for l in leads if (l.stage or '') not in STAGES_TERMINAL]

    buckets = defaultdict(lambda: {'n': 0, 'value': 0.0, 'ages': []})
    for l in live:
        key = (l.procam_vertical or '(none)', l.stage or '(none)',
               l.assigned_to or '(unowned)')
        b = buckets[key]
        b['n'] += 1
        b['value'] += _value(l)
        age = _age_days(l, today)
        if age is not None:
            b['ages'].append(age)
    for (vert, stage, owner), b in sorted(buckets.items()):
        ages = sorted(b['ages'])
        s.add(section='open pipeline', vertical=vert, stage=stage,
              owner=owner, owner_name=names.get(owner, ''),
              open_leads=b['n'], value_inr=round(b['value']),
              median_age_days=(ages[len(ages) // 2] if ages else ''),
              oldest_days=(ages[-1] if ages else ''))

    # `stage_entered_at` only. Falling back to `updated_at` made this
    # read 9,885 decisions in ninety days — every terminal lead on the
    # system — because a bulk script that corrects a vertical touches
    # `updated_at` on thousands of rows and that is not a decision.
    # The stage-history trail agrees with `stage_entered_at` to within
    # a dozen rows, which is the cross-check reported below.
    since = now - timedelta(days=WINDOW_90)
    decided = [l for l in leads
               if (l.stage or '') in STAGES_TERMINAL
               and l.stage_entered_at is not None
               and l.stage_entered_at >= since]
    by_day = Counter(str(l.stage_entered_at)[:10] for l in decided)
    bulk_days = {d for d, k in by_day.items() if k >= BULK_DAY}
    traded = [l for l in decided
              if str(l.stage_entered_at)[:10] not in bulk_days]
    for day in sorted(bulk_days):
        same = [l for l in decided if str(l.stage_entered_at)[:10] == day]
        actors = Counter(l.assigned_to or '(unowned)' for l in same)
        s.add(section='bulk day, excluded from the win rate',
              vertical='', stage='', owner='', owner_name='',
              open_leads=len(same), value_inr='', median_age_days=day,
              oldest_days='most common reason: '
                          + (Counter((l.lost_reason or '(none)')
                                     for l in same).most_common(1)
                             [0][0] if same else ''))

    won = [l for l in traded if l.stage == 'Won']
    lost = [l for l in traded if l.stage != 'Won']
    for stage, group in (('Won', won), ('Lost / other', lost)):
        s.add(section=f'last {WINDOW_90} days', vertical='(all)',
              stage=stage, owner='', owner_name='',
              open_leads=len(group),
              value_inr=round(sum(_value(l) for l in group)),
              median_age_days='', oldest_days='')
    for reason, n in Counter((l.lost_reason or '(none recorded)')
                             for l in lost).most_common(25):
        s.add(section='lost reasons, 90 days', vertical='', stage='',
              owner='', owner_name='', open_leads=n, value_inr='',
              median_age_days='', oldest_days=reason)

    from app import LeadStageHistory
    hist = LeadStageHistory.query.filter(
        LeadStageHistory.to_stage.in_(STAGES_TERMINAL),
        LeadStageHistory.changed_at >= since).count()
    total = len(won) + len(lost)
    bulk_n = len(decided) - len(traded)
    s.note = (f'{len(live):,} open leads. Last {WINDOW_90} days, '
              f'excluding bulk clean-up days: {len(won)} won, '
              f'{len(lost)} lost'
              + (f' ({100 * len(won) / total:.0f}% win rate)'
                 if total else '')
              + (f'. A further {bulk_n} lead(s) were closed on '
                 f'{len(bulk_days)} bulk day(s) '
                 f'({", ".join(sorted(bulk_days))}) and are left out of '
                 f'that rate — they are a data clean-up, not trading'
                 if bulk_n else '')
              + f'. Stage-history records {hist} terminal move(s) in the '
                f'window against {len(decided)} by stage_entered_at.')
    s.add(section='cross-check', vertical='', stage='', owner='',
          owner_name='', open_leads=hist, value_inr='',
          median_age_days='',
          oldest_days=f'stage-history moves to a terminal stage in '
                      f'{WINDOW_90} days (vs {total} by '
                      f'stage_entered_at)')
    if total and total < 20:
        s.caveat(f'Only {total} decision(s) remain once the bulk days '
                 f'are set aside, so the win rate is a ratio of very '
                 f'small numbers and should not be read as a trend.')
    blank = sum(1 for l in lost if not l.lost_reason)
    if blank:
        s.caveat(f'{blank} of {len(lost)} lost leads carry no reason, so '
                 f'the reason breakdown describes '
                 f'{len(lost) - blank} of them.')
    novalue = sum(1 for l in live if not _value(l))
    if novalue:
        s.caveat(f'{novalue} of {len(live)} open leads have no value of '
                 f'any kind, so every value total here is a floor, not '
                 f'a pipeline figure.')
    return s


# ── 2. activity per salesperson ──────────────────────────────────────
def activity(now, today):
    s = Sheet(2, f'Activity per salesperson, last {WINDOW_30} days')
    from app import LeadActivity
    from app.models.audit import AuditEvent
    from app.models.task_engine import TaskInstance, TaskInstanceStatus

    since = now - timedelta(days=WINDOW_30)
    emps = {e.emp_code: e for e in Employee.query.filter(
        Employee.is_active.is_(True)).all()}

    touched = defaultdict(set)
    logged = defaultdict(Counter)
    for a in LeadActivity.query.filter(
            LeadActivity.occurred_at >= since).all():
        who = a.performed_by or '(none)'
        touched[who].add(a.lead_id)
        logged[who][(a.kind or 'other').lower()] += 1

    edited = defaultdict(set)
    for ev in AuditEvent.query.filter(
            AuditEvent.entity_type == 'lead',
            AuditEvent.occurred_at >= since).all():
        if ev.actor and ev.actor in emps:
            edited[ev.actor].add(ev.entity_id)

    done = Counter()
    for t in TaskInstance.query.filter(
            TaskInstance.status == TaskInstanceStatus.COMPLETED,
            TaskInstance.completed_at >= since).all():
        done[t.completed_by_id or '(none)'] += 1

    overdue = Counter()
    for l in Lead.query.filter(
            Lead.stage.notin_(STAGES_TERMINAL),
            Lead.followup_date.isnot(None),
            Lead.followup_date < today).all():
        overdue[l.assigned_to or '(unowned)'] += 1

    quoted = Counter()
    for l in Lead.query.filter(Lead.quote_date.isnot(None)).all():
        if l.quote_date >= since.date():
            quoted[l.assigned_to or '(unowned)'] += 1

    who = set(touched) | set(edited) | set(done) | set(overdue) | set(emps)
    for code in sorted(who):
        emp = emps.get(code)
        s.add(emp_code=code, name=(emp.name if emp else '(not active)'),
              vertical=(emp.vertical if emp else ''),
              leads_touched=len(touched.get(code, ())),
              leads_edited=len(edited.get(code, ())),
              calls=logged[code].get('call', 0),
              mails=logged[code].get('email', 0)
                    + logged[code].get('mail', 0),
              meetings=logged[code].get('meeting', 0),
              quotes_sent=quoted.get(code, 0),
              tasks_done=done.get(code, 0),
              overdue_now=overdue.get(code, 0))

    active_people = sum(1 for r in s.rows
                        if r['leads_touched'] or r['leads_edited']
                        or r['tasks_done'])
    s.note = (f'{active_people} of {len(emps)} active employees did '
              f'anything the CRM recorded in {WINDOW_30} days.')
    s.caveat('`leads_touched` counts lead_activities, which this system '
             'barely writes — `leads_edited` from the audit trail is the '
             'more truthful column.')
    return s


# ── 3. data quality ──────────────────────────────────────────────────
def data_quality(now, today):
    s = Sheet(3, 'Data quality')
    live = Lead.query.filter(Lead.stage.notin_(STAGES_TERMINAL),
                             Lead.is_archived.isnot(True)).all()
    n = len(live) or 1

    def pct(k):
        return f'{100 * k / n:.1f}%'

    checks = (
        ('no contact email', lambda l: not (l.email or '').strip()),
        ('no phone', lambda l: not (l.phone or '').strip()),
        ('no email and no phone',
         lambda l: not (l.email or '').strip() and not (l.phone or '').strip()),
        ('no value of any kind', lambda l: not _value(l)),
        ('no next action date', lambda l: l.followup_date is None),
        ('no next action text', lambda l: not (l.next_action or '').strip()),
        ('no owner', lambda l: not (l.assigned_to or '').strip()),
        ('no vertical', lambda l: not (l.procam_vertical or '').strip()),
        ('not linked to an account', lambda l: l.company_id is None),
    )
    for label, test in checks:
        k = sum(1 for l in live if test(l))
        s.add(scope=f'open leads ({n:,})', missing=label, count=k,
              share=pct(k))

    quoting = [l for l in live if l.stage in ('Quoted', 'Under Negotiation')]
    if quoting:
        k = sum(1 for l in quoting if l.quote_validity_date is None)
        s.add(scope=f'leads with a quote out ({len(quoting)})',
              missing='no quote validity date', count=k,
              share=f'{100 * k / len(quoting):.1f}%')
        k = sum(1 for l in quoting if l.quote_date is None)
        s.add(scope=f'leads with a quote out ({len(quoting)})',
              missing='no quote date', count=k,
              share=f'{100 * k / len(quoting):.1f}%')

    accounts = Company.query.filter(Company.is_active.isnot(False)).all()
    a = len(accounts) or 1
    acc_checks = (
        ('no vertical', lambda c: not (c.vertical or '').strip()),
        ('no owner (pic_emp_code)',
         lambda c: not (c.pic_emp_code or '').strip()),
        ('no email domain', lambda c: not (c.email_domains or [])),
        ('no GSTIN', lambda c: not (c.gstin or '').strip()),
        ('no website', lambda c: not (c.website or '').strip()),
    )
    for label, test in acc_checks:
        k = sum(1 for c in accounts if test(c))
        s.add(scope=f'accounts ({a:,})', missing=label, count=k,
              share=f'{100 * k / a:.1f}%')

    s.note = (f'{n:,} open leads, {a:,} accounts.')
    return s


# ── 4. mail ──────────────────────────────────────────────────────────
def mail(now, today):
    s = Sheet(4, f'Mail — ingest and outbox, last {WINDOW_30} days')
    from app.models.mailops import EmailOutbox

    since = now - timedelta(days=WINDOW_30)
    tables = set(inspect(db.engine).get_table_names())

    if 'mail_ingest_log' in tables:
        from app.models.ingest_log import MailIngestLog
        rows = MailIngestLog.query.filter(
            MailIngestLog.received_at >= since).all()
        #: `attached` is a message matched onto a lead that already
        #: existed; `created` raised a new one; `skipped` and `error`
        #: are the unfiled — nothing in the CRM answers them.
        label = {'attached': 'matched to an existing lead',
                 'created': 'raised a new lead',
                 'reopened': 'reopened a closed lead',
                 'skipped': 'unfiled — skipped',
                 'error': 'unfiled — errored'}
        by_outcome = Counter((r.outcome or 'unknown') for r in rows)
        for outcome, k in by_outcome.most_common():
            s.add(section='leads@ ingest', item=label.get(outcome,
                                                          outcome),
                  count=k, detail=outcome)
        unfiled = sum(k for o, k in by_outcome.items()
                      if o in ('skipped', 'error'))
        s.add(section='leads@ ingest', item='TOTAL', count=len(rows),
              detail=f'last {WINDOW_30} days; {unfiled} unfiled')
        for reason, k in Counter(
                (r.reason or '(none)')[:70] for r in rows
                if r.outcome in ('skipped', 'error')).most_common(12):
            s.add(section='why a message was unfiled', item=reason,
                  count=k, detail='')
    else:
        s.caveat('There is no mail_ingest_log table on this database, '
                 'so the ingest breakdown cannot be produced.')

    created = Lead.query.filter(Lead.source == 'email',
                                Lead.created_at >= since).count()
    linked = Lead.query.filter(Lead.source == 'email',
                               Lead.created_at >= since,
                               Lead.company_id.isnot(None)).count()
    s.add(section='leads from mail', item='created in window',
          count=created, detail='source = email')
    s.add(section='leads from mail', item='matched to an account',
          count=linked,
          detail=f'{created - linked} unfiled')

    by_day = defaultdict(Counter)
    for r in EmailOutbox.query.filter(
            EmailOutbox.created_at >= since).all():
        by_day[str(r.created_at)[:10]][r.status] += 1
    for day in sorted(by_day):
        counts = by_day[day]
        s.add(section='outbox by day', item=day,
              count=sum(counts.values()),
              detail=', '.join(f'{k} {v}' for k, v in
                               sorted(counts.items())))
    totals = Counter()
    for counts in by_day.values():
        totals.update(counts)
    s.note = ('outbox in window: '
              + ', '.join(f'{k} {v}' for k, v in sorted(totals.items())))
    return s


# ── 5. freight forwarding ────────────────────────────────────────────
def freight_forwarding(now, today):
    from app.services import lead_mode
    s = Sheet(5, 'Freight Forwarding')
    names = {e.emp_code: e.name for e in Employee.query.all()}
    ff = Lead.query.filter(Lead.procam_vertical == lead_mode.VERTICAL).all()
    live = [l for l in ff if (l.stage or '') not in STAGES_TERMINAL]

    for mode in list(lead_mode.MODES) + ['(not stated)']:
        group = [l for l in live
                 if (l.mode or '(not stated)') == mode]
        s.add(section='open FF by mode', key=mode, count=len(group),
              value_inr=round(sum(_value(l) for l in group)), detail='')

    for owner, group in sorted(
            ((o, [l for l in live if (l.assigned_to or '(unowned)') == o])
             for o in {l.assigned_to or '(unowned)' for l in live}),
            key=lambda kv: -len(kv[1])):
        s.add(section='open FF by owner', key=owner, count=len(group),
              value_inr=round(sum(_value(l) for l in group)),
              detail=names.get(owner, ''))

    for stage, k in Counter((l.stage or '(none)')
                            for l in live).most_common():
        s.add(section='open FF by stage', key=stage, count=k,
              value_inr='', detail='')

    quoted = [l for l in ff if l.quote_date is not None]
    won = [l for l in ff if l.stage == 'Won']
    won_quoted = [l for l in won if l.quote_date is not None]
    s.add(section='FF quotes', key='leads with a quote date',
          count=len(quoted), value_inr=round(sum(_value(l)
                                                 for l in quoted)),
          detail='')
    s.add(section='FF quotes', key='won', count=len(won),
          value_inr=round(sum(_value(l) for l in won)),
          detail=f'{len(won_quoted)} of them had a quote date')

    s.note = (f'{len(live)} open Freight Forwarding leads of {len(ff)} '
              f'in all; {sum(1 for l in live if l.mode)} carry a mode.')
    if not any(l.mode for l in ff):
        s.caveat('No FF lead carries a mode, so the mode breakdown is '
                 'entirely "(not stated)".')
    s.caveat('Quotes are counted from `leads.quote_date`, not the '
             '`quotes` table, which has no rows on this system.')
    return s


# ── 6. CRM 2.0 readiness ─────────────────────────────────────────────
def readiness(now, today):
    s = Sheet(6, 'CRM 2.0 readiness and the 12 Oct FF decisions')
    tables = set(inspect(db.engine).get_table_names())

    for name in CRM2_TABLES:
        present = name in tables
        rows = ''
        if present:
            try:
                rows = db.session.execute(
                    db.text(f'SELECT COUNT(*) FROM "{name}"')).scalar()
            except Exception:                             # noqa: BLE001
                rows = '?'
        s.add(area='expected table', item=name,
              state='present' if present else 'ABSENT',
              detail=(f'{rows} row(s)' if present
                      else 'the spec expects it; it does not exist'))

    from app.services import mailer
    sender = ''
    try:
        sender = mailer.sender() or ''
    except Exception:                                     # noqa: BLE001
        sender = '(unreadable)'
    s.add(area='FF decision 12 Oct', item='crm@ / tms@ mailboxes',
          state='CONFLICT' if 'tms@' in sender else 'check',
          detail=f'the CRM sends as {sender!r}; the decision gives the '
                 f'CRM its own crm@ mailbox')

    crn_cols = [c for c in (col['name'] for col in
                            inspect(db.engine).get_columns('leads'))
                if 'crn' in c.lower()]
    s.add(area='FF decision 12 Oct',
          item='CRN <BU>-<C|T><FY>-<NNNNNN>',
          state='ABSENT' if not crn_cols else 'present',
          detail=('no column on `leads` holds a CRN; the CRM numbers '
                  'leads by integer id and `opp_number`'
                  if not crn_cols else ', '.join(crn_cols)))

    po_tables = [t for t in tables if 'purchase_order' in t or t == 'pos']
    s.add(area='FF decision 12 Oct', item='FF Client PO created in TMS',
          state='consistent' if not po_tables else 'CONFLICT',
          detail=('the CRM holds no purchase-order table, which is what '
                  'the decision requires' if not po_tables
                  else f'the CRM has {", ".join(po_tables)}'))

    inco = [c for c in (col['name'] for col in
                        inspect(db.engine).get_columns('leads'))
            if 'incoterm' in c.lower()]
    s.add(area='FF decision 12 Oct', item='Incoterm billing',
          state='ABSENT' if not inco else 'present',
          detail=('nothing in the CRM records an Incoterm, so billing '
                  'cannot be driven by one from here'
                  if not inco else ', '.join(inco)))

    s.add(area='FF decision 12 Oct', item='Own CHA, Chennai and Mumbai',
          state='not modelled',
          detail='no table or field distinguishes in-house CHA from an '
                 'agent, or records the port it applies to')

    absent = [r['item'] for r in s.rows if r['state'] == 'ABSENT']
    s.note = (f'{len(absent)} of the checked items do not exist yet: '
              f'{", ".join(absent)}')
    s.caveat('The CRM 2.0 brief is not in this repository. This sheet '
             'checks the schema items `scripts/stage_a_review.py` '
             'encodes from it, plus the five FF decisions named in the '
             'request — not everything the brief asks for.')
    return s


SHEETS = (pipeline, activity, data_quality, mail, freight_forwarding,
          readiness)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out')
    args = ap.parse_args()

    with app.app_context():
        now, today = datetime.utcnow(), date.today()
        print(f'database: {db.engine.url}')
        print(f'as of:    {now} UTC  (read-only)\n')
        made = []
        for fn in SHEETS:
            try:
                made.append(fn(now, today))
            except Exception as exc:                      # noqa: BLE001
                db.session.rollback()
                sh = Sheet(0, fn.__name__)
                sh.note = (f'this section failed: '
                           f'{type(exc).__name__}: {str(exc)[:200]}')
                made.append(sh)

        out = args.out or os.path.join(
            _ROOT, 'docs', 'exports',
            f'crm_figures_{today.isoformat()}.xlsx')
        _write(made, out, now)
        _summary(made)
        print(f'\nworkbook: {out}')
        print('Client names are masked above and real in the file.')
        print('Nothing was written to the database.')


def _summary(sheets):
    width = 72
    print('=' * width)
    print('CRM-FIGURES-01'.center(width))
    print('=' * width)
    for s in sheets:
        print(f'\n{s.num}. {s.title}')
        print(f'   {s.note}')
        for c in s.caveats:
            print(f'   ! {c}')
    print('\n' + '=' * width)


def _write(sheets, path, now):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    os.makedirs(os.path.dirname(path), exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = 'Contents'
    ws.append(['CRM-FIGURES-01'])
    ws['A1'].font = Font(bold=True, size=14)
    ws.append([f'Read-only, as of {now} UTC. Client names are real in '
               f'this file and masked in the printed summary.'])
    ws.append([])
    ws.append(['#', 'Sheet', 'Headline', 'Read this with'])
    for cell in ws[4]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='1F2937')
    for s in sheets:
        ws.append([s.num, s.title, s.note, ' · '.join(s.caveats)])
        ws.cell(ws.max_row, 3).alignment = Alignment(wrap_text=True,
                                                     vertical='top')
        ws.cell(ws.max_row, 4).alignment = Alignment(wrap_text=True,
                                                     vertical='top')
    for col, w in (('A', 5), ('B', 44), ('C', 62), ('D', 62)):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = 'A5'

    for s in sheets:
        name = f'{s.num}. ' + re.sub(r'[\\/*?:\[\]]', '', s.title)[:26]
        sheet = wb.create_sheet(name[:31])
        sheet.append([s.title])
        sheet['A1'].font = Font(bold=True, size=12)
        sheet.append([s.note])
        for c in s.caveats:
            sheet.append([f'! {c}'])
        sheet.append([])
        if not s.rows:
            sheet.append(['(no rows)'])
            continue
        cols = list(s.rows[0].keys())
        head_at = sheet.max_row + 1
        sheet.append([c.replace('_', ' ') for c in cols])
        for cell in sheet[head_at]:
            cell.font = Font(bold=True, color='FFFFFF')
            cell.fill = PatternFill('solid', fgColor='1F2937')
        for row in s.rows:
            sheet.append([row.get(c, '') for c in cols])
        for i, col in enumerate(cols, start=1):
            widest = max([len(str(col))]
                         + [len(str(r.get(col, ''))) for r in s.rows])
            sheet.column_dimensions[
                sheet.cell(1, i).column_letter].width = min(52, widest + 3)
        sheet.freeze_panes = f'A{head_at + 1}'
    wb.save(path)


if __name__ == '__main__':
    main()
