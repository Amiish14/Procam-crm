"""
The Daily Workbench: everything one person has to act on, in one list.

Built from `app.services.sales_rules` — the conditions, groups,
thresholds and ageing buckets all live there, so the Workbench, the
Team Workbench and (later) the daily email cannot drift apart.

Shape of the work:
  * one scoped, column-trimmed read of the open leads the viewer may see
  * two grouped reads for "when was this customer last contacted"
  * one read of the RFQs awaiting a quote, one of the quotes against them
  * one read per hygiene check that applies to leads, for the data issues
  * accounts needing development, one read

Then the conditions are evaluated in Python over those rows. The
alternative — a UNION of a dozen filtered queries — costs more reads and
cannot rank a lead that trips four conditions at once, which is exactly
the lead that should be at the top.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy.orm import load_only

from app.services import sales_rules as rules

#: Hygiene checks that describe a lead. Reused from Data Quality rather
#: than re-expressed here, so a threshold is changed in one place.
LEAD_HYGIENE_CHECKS = (
    'unowned_leads', 'leads_of_leavers', 'stale_leads', 'leads_no_followup',
    'unlinked_leads', 'lead_unknown_vertical', 'quoted_without_quote',
    'won_leads_no_value', 'quote_past_validity', 'lead_account_mismatch',
)

#: Columns the Workbench reads off a lead. Loading whole Lead objects
#: pulls every email body and extracted-JSON blob for thousands of rows.
_LEAD_COLUMNS = (
    'id', 'company', 'company_id', 'project', 'stage', 'procam_vertical',
    'assigned_to', 'assigned_name', 'secondary_owner', 'followup_date',
    'next_action', 'estimated_value_inr', 'quoted_amount_inr', 'cost_million',
    'quote_date', 'quote_validity_date', 'quote_no', 'rfq_date', 'opp_number',
    'created_at', 'updated_at', 'stage_entered_at', 'is_archived',
    # For the Overview panel's "Never actioned" and for the Mode
    # column on Freight Forwarding leads. Both are read by filter
    # predicates, so they have to travel with the item.
    'phone_call_date', 'intro_mail_date', 'meeting_date', 'mode',
)


def _today():
    return rules.business_today()


def _lead_rows(sc):
    from app import Lead
    q = rules.open_leads(sc=sc).options(
        load_only(*[getattr(Lead, c) for c in _LEAD_COLUMNS]))
    return q.all()


#: The hygiene checks are ten scoped queries over the whole lead table —
#: two thirds of the board's cost for a manager, and re-run on every
#: filter, sort and page change of the same board. One minute of memory
#: makes paging free; a correction shows up on the next minute, and a
#: bulk update clears it at once (see bulk._write).
_HYGIENE_TTL_SECONDS = 60
_hygiene_cache = {}


def hygiene_cache_clear():
    """Called after anything that fixes records, so the board does not
    keep showing an issue somebody has just resolved."""
    _hygiene_cache.clear()


def _scope_key(sc):
    return (sc.emp_code, None if sc.unrestricted else tuple(sorted(sc.codes or ())))


def _hygiene_ids(sc):
    """{lead_id: [check title, ...]} for the lead-level checks."""
    import time as _time
    key = (_scope_key(sc), _today().isoformat())
    hit = _hygiene_cache.get(key)
    if hit and (_time.monotonic() - hit[0]) < _HYGIENE_TTL_SECONDS:
        return hit[1]
    out = _hygiene_ids_uncached(sc)
    _hygiene_cache[key] = (_time.monotonic(), out)
    return out


def _hygiene_ids_uncached(sc):
    from app.data_quality import service as dq
    out = {}
    for key in LEAD_HYGIENE_CHECKS:
        check = dq.find(key)
        if check is None:
            continue
        try:
            _count, detail = dq.run(check, sc)
        except Exception:
            from app import db
            db.session.rollback()
            continue
        ids = []
        if detail is None:
            continue
        if hasattr(detail, 'with_entities'):
            from app import Lead
            try:
                ids = [r[0] for r in detail.with_entities(Lead.id).limit(2000)]
            except Exception:
                from app import db
                db.session.rollback()
                ids = []
        elif isinstance(detail, (list, tuple)):
            for row in detail:
                row = row[0] if isinstance(row, tuple) else row
                if getattr(row, 'id', None) and type(row).__name__ == 'Lead':
                    ids.append(row.id)
        for lid in ids:
            out.setdefault(lid, []).append(check.label)
    return out


def _rfq_by_lead(sc, today):
    """{lead_id: the most pressing RFQ row} from the ageing service."""
    ageing = rules.rfq_ageing(sc=sc, today=today)
    by_lead = {}
    for row in ageing['buckets'].values():
        for r in row:
            if not r['lead_id']:
                continue
            kept = by_lead.get(r['lead_id'])
            if kept is None or (r['age_days'] or 0) > (kept['age_days'] or 0):
                by_lead[r['lead_id']] = r
    return by_lead, ageing


def _account_rows(sc, today):
    """Accounts the viewer owns that need development attention."""
    from app import Company
    from app.access import scope as sc_mod
    quiet_before = datetime.utcnow() - timedelta(days=rules.ACCOUNT_QUIET_DAYS)
    q = sc_mod.companies(Company.query.filter(Company.is_active.is_(True)),
                         sc=sc)
    rows = []
    for c in q.options(load_only(
            Company.id, Company.name, Company.vertical, Company.pic_emp_code,
            Company.secondary_pic_emp_code, Company.dev_stage,
            Company.last_activity_at, Company.next_action_at)).all():
        due = c.next_action_at is not None and c.next_action_at <= today
        quiet = (c.last_activity_at is None
                 or c.last_activity_at < quiet_before)
        if not (due or quiet):
            continue
        rows.append({'company': c, 'due': due, 'quiet': quiet})
    return rows


def _quote_facts(sc, lead_ids):
    """{lead_id: {'sent_at': date|None, 'validity': date|None}}.

    The date a quote actually went to the customer. `Quote.submitted_at`
    is the real event; `Quote.quote_date` is what somebody typed on the
    document. Only quotes that have left the building count — a draft
    is not a quote the customer is sitting on.
    """
    out = {}
    if not lead_ids:
        return out
    try:
        from app.models.quote import Quote
        rows = (Quote.query
                .filter(Quote.lead_id.in_(list(lead_ids)))
                .filter(Quote.status.notin_(('Draft', 'Withdrawn',
                                             'Superseded')))
                .all())
    except Exception:
        from app import db
        db.session.rollback()
        return out
    for q in rows:
        sent = (q.submitted_at.date() if getattr(q, 'submitted_at', None)
                else q.quote_date)
        kept = out.get(q.lead_id)
        # The earliest send is the one the customer has been waiting
        # since; a revision does not reset the clock.
        if kept is None or (sent and kept['sent_at']
                            and sent < kept['sent_at']) \
                or (sent and kept['sent_at'] is None):
            out[q.lead_id] = {'sent_at': sent,
                              'validity': q.validity_until}
    return out


def _stage_entered(lead_ids, stage):
    """{lead_id: date it entered this stage}, from the stage history.

    Used when there is no quote record to date the quote from. The
    history is the only honest answer to "how long has the customer
    been holding this"; `updated_at` moves every time anybody touches
    the row, so a lead edited yesterday would read as quoted
    yesterday.
    """
    out = {}
    if not lead_ids:
        return out
    try:
        from app import LeadStageHistory
        rows = (LeadStageHistory.query
                .filter(LeadStageHistory.lead_id.in_(list(lead_ids)),
                        LeadStageHistory.to_stage == stage)
                .order_by(LeadStageHistory.changed_at.asc()).all())
    except Exception:
        from app import db
        db.session.rollback()
        return out
    for r in rows:
        if r.lead_id not in out and r.changed_at:
            out[r.lead_id] = r.changed_at.date()
    return out


def _priority_and_due(conditions, lead, today, rfq):
    """Where this record sits in the list, and the date it is judged on."""
    due, priority = None, rules.P_ROUTINE
    if 'followup_overdue' in conditions:
        due, priority = lead.followup_date, rules.P_OVERDUE
    elif 'quote_overdue' in conditions and rfq:
        due, priority = rfq['quote_due'], rules.P_OVERDUE
    elif 'quote_lapsed' in conditions:
        due, priority = lead.quote_validity_date, rules.P_OVERDUE
    elif 'followup_today' in conditions:
        due, priority = lead.followup_date, rules.P_TODAY
    elif 'quote_due_soon' in conditions and rfq:
        due, priority = rfq['quote_due'], rules.P_TODAY
    elif 'stale' in conditions:
        priority = rules.P_SOON
    elif 'idle' in conditions or 'negotiation_idle' in conditions:
        priority = rules.P_SOON
    elif lead.followup_date:
        due = lead.followup_date
    if 'high_value' in conditions and priority > rules.P_SOON:
        priority = rules.P_SOON
    return (str(due) if due else ''), priority


def _primary_group(conditions):
    """The group a record is filed under when it trips several."""
    for key, _label, group, _why in rules.CONDITIONS:
        if key in conditions:
            return group
    return rules.G_DATA


#: The group a lead with nothing outstanding is filed under. It is
#: deliberately not one of `rules.GROUPS`: those are the eight headings
#: the Workbench lists under, and a lead with nothing wrong belongs in
#: none of them. It exists so the two screens count the same leads —
#: see `lead_items(include_quiet=...)`.
G_NOTHING_OUTSTANDING = 'nothing_outstanding'


def lead_items(sc, *, today=None, include_quiet=False):
    """Every lead that needs attention, as action items.

    `include_quiet` keeps the leads that trip no condition at all. The
    Workbench used to drop them, which was right while it was the only
    reader: a lead contacted on Monday with a follow-up booked for
    Friday needs nothing. It became wrong when the Overview panel
    started counting the same leads from its own SQL, because
    "In negotiation" and "Idle 3+ days" both include perfectly healthy
    leads and the board could not see them. Both screens now build
    from the same list.

    A quiet lead matches no chip but `Everything`, so the only count
    it changes on the Workbench is that one — which is the count that
    was wrong to exclude it.
    """
    from app.services import lead_value
    today = today or _today()
    leads = _lead_rows(sc)
    lead_ids = [l.id for l in leads]
    idle = rules.idle_days_map(lead_ids)
    hygiene = _hygiene_ids(sc)
    rfq_by_lead, ageing = _rfq_by_lead(sc, today)
    quotes = _quote_facts(sc, lead_ids)
    entered_quoted = _stage_entered(lead_ids, 'Quoted')
    now = datetime.utcnow()
    items = []
    for l in leads:
        conds, rfq = [], rfq_by_lead.get(l.id)
        contacted_days = idle.get(l.id)
        idle_days = rules.idle_days(l, contacted_days)
        if l.followup_date and l.followup_date < today:
            conds.append('followup_overdue')
        if l.followup_date == today:
            conds.append('followup_today')
        if rfq:
            if rfq['days_late']:
                conds.append('quote_overdue')
            elif rfq['quote_due'] and rules.days_between(
                    today, date.fromisoformat(rfq['quote_due'])) <= 2:
                conds.append('quote_due_soon')
        if l.stage in lead_value.OPEN_QUOTE_STAGES:
            if l.quoted_amount_inr is None or l.quote_date is None:
                conds.append('quote_missing_detail')
            if (l.quote_validity_date is not None
                    and l.quote_validity_date < today):
                conds.append('quote_lapsed')
        # Waiting to be quoted with no deadline anywhere. Nothing can
        # tell anyone this one is late, so it is a data problem rather
        # than a silent omission from the Quotes due tile.
        quote_sent = (quotes.get(l.id) or {}).get('sent_at') or l.quote_date
        if (l.stage == 'RFQ Generated' and not quote_sent
                and not (rfq or {}).get('quote_due')):
            conds.append('quote_due_missing')
        if (l.stage == 'Under Negotiation'
                and idle_days is not None and idle_days >= rules.IDLE_DAYS):
            conds.append('negotiation_idle')
        if idle_days is not None and idle_days >= rules.STALE_DAYS:
            conds.append('stale')
        elif idle_days is not None and idle_days >= rules.IDLE_DAYS:
            conds.append('idle')
        if not l.followup_date:
            conds.append('no_next_action')
        if l.id in hygiene:
            conds.append('data_issue')
        if (l.created_at and
                (now - l.created_at) <= timedelta(hours=rules.NEW_ASSIGNMENT_HOURS)):
            conds.append('newly_assigned')
        if rules.is_high_value(l):
            conds.append('high_value')
        if not conds and not include_quiet:
            continue
        due, priority = _priority_and_due(conds, l, today, rfq)
        value = lead_value.value_inr(l)
        items.append({
            'kind': 'lead',
            'id': l.id,
            'group': (_primary_group(conds) if conds
                      else G_NOTHING_OUTSTANDING),
            'priority': priority,
            'due': due,
            'account': l.company or '',
            'account_id': l.company_id,
            'title': l.project or l.company or f'Lead #{l.id}',
            'vertical': l.procam_vertical or '',
            'stage': l.stage or '',
            'value_inr': float(value) if value is not None else None,
            'last_activity_days': contacted_days,
            'quiet_days': idle_days,
            'next_action': l.next_action or '',
            'next_action_date': str(l.followup_date) if l.followup_date else '',
            'quote_due': (rfq or {}).get('quote_due', ''),
            'quote_number': l.quote_no or (rfq or {}).get('rfq_number', ''),
            #: When the quote actually went to the customer, and how
            #: sure we are of that date. The ageing buckets and the
            #: "awaiting quote" filter both read these, so the answer
            #: is worked out once, here, rather than in each of them.
            'quote_sent_at': str(quote_sent) if quote_sent else '',
            'quote_entered_quoted': (str(entered_quoted[l.id])
                                     if l.id in entered_quoted else ''),
            'quote_validity': (str(l.quote_validity_date)
                               if l.quote_validity_date else ''),
            'ageing_days': rules.days_between(
                (l.stage_entered_at or l.updated_at or l.created_at), today),
            'assigned_to': l.assigned_to or '',
            'assigned_name': l.assigned_name or l.assigned_to or '',
            'mode': getattr(l, 'mode', None) or '',
            'never_actioned': not (l.phone_call_date or l.intro_mail_date
                                   or l.meeting_date),
            'data_issues': hygiene.get(l.id, []),
            'conditions': conds,
            'reasons': [rules.CONDITION_LABELS.get(c, c) for c in conds],
            'route': f'/app?lead={l.id}',
        })
    return items, ageing


def account_items(sc, *, today=None):
    today = today or _today()
    out = []
    for row in _account_rows(sc, today):
        c = row['company']
        conds = []
        if row['due']:
            conds.append('account_next_action')
        if row['quiet']:
            conds.append('account_quiet')
        out.append({
            'kind': 'account',
            'id': c.id,
            'group': rules.G_ACCOUNTS,
            'priority': rules.P_TODAY if row['due'] else rules.P_ROUTINE,
            'due': str(c.next_action_at) if c.next_action_at else '',
            'account': c.name,
            'account_id': c.id,
            'title': c.name,
            'vertical': c.vertical or '',
            'stage': c.dev_stage or '',
            'value_inr': None,
            'last_activity_days': rules.days_between(c.last_activity_at, today),
            'next_action': '',
            'next_action_date': str(c.next_action_at) if c.next_action_at else '',
            'quote_due': '', 'quote_number': '',
            'quote_sent_at': '', 'quote_entered_quoted': '',
            'quote_validity': '',
            'ageing_days': rules.days_between(c.last_activity_at, today),
            'assigned_to': c.pic_emp_code or '',
            'assigned_name': c.pic_emp_code or '',
            'data_issues': [],
            'conditions': conds,
            'reasons': [rules.CONDITION_LABELS.get(c_, c_) for c_ in conds],
            'route': f'/companies/{c.id}',
        })
    return out


#: How long a closed lead stays on the board after client mail lands
#: on it. Long enough that a PIC who was away still sees it.
CLOSED_MAIL_DAYS = 14


def closed_mail_items(sc, *, today=None):
    """Closed leads that have received mail, as action items.

    `lead_items` reads open leads only, which is right for everything
    else and is exactly why lead #11807 was invisible: an enquiry was
    filed against it, the lead was Lost, and the board does not show
    Lost leads. The mail arrived, the record was updated, and nothing
    in the CRM put it in front of the person who owned it.

    One query against the ingest log, so a lead appears here only if
    a message genuinely landed on it.
    """
    from app.services import flags, mail_signals

    today = today or _today()
    if not flags.on('FEATURE_REOPEN_ON_MAIL'):
        return []

    from app import Lead, db
    from app.models.ingest_log import MailIngestLog
    from app.access import scope as sc_mod

    since = datetime.utcnow() - timedelta(days=CLOSED_MAIL_DAYS)
    try:
        recent = (db.session.query(MailIngestLog.lead_id,
                                   db.func.max(MailIngestLog.received_at),
                                   db.func.count(MailIngestLog.id))
                  .filter(MailIngestLog.lead_id.isnot(None),
                          MailIngestLog.received_at >= since)
                  .group_by(MailIngestLog.lead_id).all())
    except Exception:
        db.session.rollback()
        return []
    if not recent:
        return []

    by_lead = {lid: (when, n) for lid, when, n in recent if lid}
    try:
        q = Lead.query.filter(
            Lead.id.in_(list(by_lead)),
            Lead.is_archived.isnot(True),
            Lead.stage.in_(mail_signals.CLOSED_STAGES))
        leads = sc_mod.leads(q, sc=sc).all()
    except Exception:
        db.session.rollback()
        return []

    out = []
    for l in leads:
        when, n = by_lead.get(l.id, (None, 0))
        out.append({
            'kind': 'lead', 'id': l.id,
            'group': rules.G_TODAY,
            'priority': rules.P_TODAY,
            'due': str(when)[:10] if when else '',
            'account': l.company or '',
            'account_id': l.company_id,
            'title': l.project or l.company or f'Lead #{l.id}',
            'vertical': l.procam_vertical or '',
            'stage': l.stage or '',
            'value_inr': None,
            'last_activity_days': rules.days_between(when, today),
            'quiet_days': None,
            'next_action': '',
            'next_action_date': '',
            'quote_due': '', 'quote_number': '',
            'quote_sent_at': '', 'quote_entered_quoted': '',
            'quote_validity': '',
            'ageing_days': rules.days_between(when, today),
            'assigned_to': l.assigned_to or '',
            'assigned_name': l.assigned_name or l.assigned_to or '',
            'mode': getattr(l, 'mode', None) or '',
            'never_actioned': not (l.phone_call_date or l.intro_mail_date
                                   or l.meeting_date),
            'data_issues': [],
            'conditions': ['mail_on_closed'],
            'reasons': [f'{n} message{"" if n == 1 else "s"} received while '
                        f'the lead is {l.stage}'],
            'mail_count': n,
            'route': f'/app?lead={l.id}',
        })
    return out


def owner_confirm_items(sc, *, today=None):
    """Leads a rule assigned, waiting for their head to agree.

    They have a real owner already — the rules never leave a lead
    with nobody — so this is not a queue of unowned work. It is the
    list of guesses nobody has checked, which is a different thing
    and belongs in front of the person who can check them.
    """
    from app.services import flags

    today = today or _today()
    if not flags.on('FEATURE_AUTO_ASSIGN'):
        return []

    from app import Lead, db
    from app.access import scope as sc_mod
    from app.models.assignment import OwnerConfirmation

    try:
        pending = (OwnerConfirmation.query
                   .filter(OwnerConfirmation.confirmed_at.is_(None))
                   .limit(2000).all())
    except Exception:
        db.session.rollback()
        return []
    if not pending:
        return []

    by_lead = {p.lead_id: p for p in pending}
    try:
        leads = sc_mod.leads(
            Lead.query.filter(Lead.id.in_(list(by_lead)),
                              Lead.is_archived.isnot(True)), sc=sc).all()
    except Exception:
        db.session.rollback()
        return []

    out = []
    for l in leads:
        row = by_lead.get(l.id)
        waited = rules.days_between(row.created_at, today) if row else None
        out.append({
            'kind': 'lead', 'id': l.id,
            'group': rules.G_NEW,
            'priority': rules.P_TODAY,
            'due': '',
            'account': l.company or '',
            'account_id': l.company_id,
            'title': l.project or l.company or f'Lead #{l.id}',
            'vertical': l.procam_vertical or '',
            'stage': l.stage or '',
            'value_inr': None,
            'last_activity_days': None, 'quiet_days': None,
            'next_action': 'Confirm the owner, or reassign',
            'next_action_date': '',
            'quote_due': '', 'quote_number': '',
            'quote_sent_at': '', 'quote_entered_quoted': '',
            'quote_validity': '',
            'ageing_days': waited,
            'assigned_to': l.assigned_to or '',
            'assigned_name': l.assigned_name or l.assigned_to or '',
            'mode': getattr(l, 'mode', None) or '',
            'never_actioned': not (l.phone_call_date or l.intro_mail_date
                                   or l.meeting_date),
            'data_issues': [],
            'conditions': ['owner_to_confirm'],
            'reasons': [f'assigned by rule ({row.rule}) and not yet '
                        f'confirmed' if row else 'owner not confirmed'],
            'route': f'/app?lead={l.id}',
        })
    return out


#: The most task rows one board build will read. It is a safety stop,
#: not a page size: the board counts what it reads, so a limit that
#: bites turns every count into an understatement.
#:
#: It used to be 500, which bit hard. Tasks sort due-date first and
#: every open task on this system is overdue, so the Overdue tile read
#: exactly 500 for every viewer — an admin with 7,318 overdue tasks and
#: a rep with 587 both saw 500, and neither number was theirs. A tile
#: that reports a limit as though it were a count is worse than no
#: tile, because it is confidently wrong and nothing on the screen says
#: so. `board()` now reports `truncated` when this is reached.
TASK_CEILING = 20000


def task_items(sc, *, today=None):
    """Open task-engine tasks, on the same board as everything else.

    The CRM had two "my work" screens — the task buckets and a separate
    lead-pendency API — and a person had to read both. One board, one
    answer.
    """
    from app.models.task_engine import TaskInstance, TaskInstanceStatus
    today = today or _today()
    now = datetime.utcnow()
    q = TaskInstance.query.filter(
        TaskInstance.status.in_(TaskInstanceStatus.OPEN))
    if not sc.unrestricted:
        codes = sc.codes or set()
        q = q.filter(TaskInstance.owner_user_id.in_(codes or {''}))
    out = []
    for t in q.order_by(TaskInstance.due_at.asc().nullslast()
                        ).limit(TASK_CEILING):
        overdue = bool(t.due_at and t.due_at < now)
        due_today = bool(t.due_at and not overdue
                         and t.due_at.date() == today)
        key = (t.task_key or '')
        group = (rules.G_QUOTES if key.startswith(('quote.', 'rfq.'))
                 else rules.G_ACCOUNTS if key.startswith('account.')
                 else rules.G_TODAY)
        conds = (['followup_overdue'] if overdue
                 else ['followup_today'] if due_today else [])
        out.append({
            'kind': 'task',
            'id': t.id,
            'group': group,
            'priority': (rules.P_OVERDUE if overdue
                         else rules.P_TODAY if due_today
                         else min(rules.P_ROUTINE, max(1, t.priority or 3))),
            'due': str(t.due_at)[:10] if t.due_at else '',
            'account': t.entity_display or '',
            'account_id': None,
            'title': key.replace('.', ' ').replace('_', ' ').title(),
            'vertical': '', 'stage': t.status or '',
            'value_inr': None,
            'last_activity_days': None,
            'next_action': t.entity_display or '',
            'next_action_date': str(t.due_at)[:10] if t.due_at else '',
            'quote_due': '', 'quote_number': '',
            'quote_sent_at': '', 'quote_entered_quoted': '',
            'quote_validity': '',
            'ageing_days': int((now - t.created_at).days) if t.created_at else None,
            'assigned_to': t.owner_user_id or '',
            'assigned_name': t.owner_user_id or (t.owner_role or ''),
            'data_issues': [],
            'conditions': conds,
            'reasons': [rules.CONDITION_LABELS.get(c, c) for c in conds]
                       or [t.status or 'Open task'],
            'route': t.action_route or '/my-work',
        })
    return out


# ── quote ageing ─────────────────────────────────────────────────────
def quote_ageing(items, *, today=None):
    """How long customers have been holding our quotes.

    Rebuilt on lead **stage**, because that is how the business tracks
    a quote. The old version bucketed RFQ records against a quote-due
    date: one item in 1,984 had a quote number and none had a due
    date, so every bucket read 0 while 130 leads sat in Quoted or
    Under Negotiation. A dashboard of zeroes over a hundred and thirty
    live quotes is worse than no dashboard.

    Population = the open leads in Quoted or Under Negotiation that
    are already in `items`, so the scope, vertical, stage and search
    filters apply here exactly as they do to the list.

    Age is days since the quote went out, in falling order of how much
    we trust the date:

        the quote record's submitted_at  — the real event
        the date the lead entered Quoted — from the stage history
        the lead's own quote_date        — what somebody typed
        updated_at                       — a guess, and flagged as one

    Every item in the population lands in exactly one bucket, so the
    buckets sum to the population. A row dated in the future is
    clamped to Today rather than dropped: a wrong date is a data
    problem to see, not a row to lose.
    """
    today = today or _today()
    pop = [i for i in items
           if i.get('kind') == 'lead'
           and i.get('stage') in ('Quoted', 'Under Negotiation')]

    counts = rules.empty_buckets()
    buckets = {k: [] for k, _l, _lo, _hi in rules.AGEING_BUCKETS}
    estimated, overdue = 0, []

    for item in pop:
        when, sure = _quote_age_from(item)
        if when is None:
            days, sure = 0, False
        else:
            days = rules.days_between(when, today) or 0
        if days < 0:
            days, sure = 0, False
        if not sure:
            estimated += 1
        item['ageing_bucket'] = rules.bucket_for(days) or 'd10_plus'
        item['quote_age_days'] = days
        item['quote_date_estimated'] = not sure
        counts[item['ageing_bucket']] += 1
        buckets[item['ageing_bucket']].append(item['id'])
        if _quote_is_overdue(item, days, today):
            overdue.append(item['id'])

    return {
        'population': len(pop),
        'counts': counts,
        'estimated': estimated,
        'overdue_count': len(overdue),
        'buckets': [{'key': k, 'label': lbl, 'count': counts.get(k, 0)}
                    for k, lbl, _lo, _hi in rules.AGEING_BUCKETS],
        'bucket_ids': buckets,
        'overdue_ids': overdue,
    }


def _quote_age_from(item):
    """(date the clock starts, is that date reliable)."""
    from datetime import date as _d

    def _parse(value):
        if not value:
            return None
        try:
            return _d.fromisoformat(str(value)[:10])
        except (ValueError, TypeError):
            return None

    for field in ('quote_sent_at', 'quote_entered_quoted'):
        when = _parse(item.get(field))
        if when is not None:
            return when, True
    # Nothing recorded the send. `ageing_days` is already days since
    # the lead entered its current stage, falling back to updated_at —
    # the best guess available, and shown as a guess.
    days = item.get('ageing_days')
    if days is not None:
        return (_today() - timedelta(days=int(days))), False
    return None, False


def _quote_is_overdue(item, days, today):
    """Past its validity, or gone quiet with nothing scheduled."""
    from datetime import date as _d
    validity = item.get('quote_validity')
    if validity:
        try:
            if _d.fromisoformat(str(validity)[:10]) < today:
                return True
        except (ValueError, TypeError):
            pass
    quiet = item.get('quiet_days')
    return bool(days > 10 and (quiet is None or quiet > 10)
                and not item.get('next_action_date'))


def _sort_key(item):
    return (item['priority'], item['due'] or '9999-12-31',
            -(item['value_inr'] or 0))


def board(sc, *, today=None, group=None, filter_key=None, age='',
          search='', vertical='', stage='', owner='', mode='', page=1,
          per_page=50, sort='priority'):
    """The whole Workbench: one counts object, a filtered page of
    items, and the quote-ageing buckets.

    `filter_key` selects a view from `app.workbench.filters`. The count
    the tile shows, the count the chip shows and the rows returned here
    all come from that one predicate, so they cannot disagree.

    `group` is still accepted so a bookmarked `?group=` keeps working;
    it is mapped onto the equivalent filter and is no longer used for
    counting anything.
    """
    from app.workbench import filters as wf

    today = today or _today()
    items, _rfq_ageing = lead_items(sc, today=today, include_quiet=True)
    items += account_items(sc, today=today)
    tasks = task_items(sc, today=today)
    truncated = len(tasks) >= TASK_CEILING
    items += tasks
    items += closed_mail_items(sc, today=today)
    items += owner_confirm_items(sc, today=today)

    # The scope-wide filters first: everything downstream — counts,
    # chips, ageing, the list — has to see the same population, or
    # changing the vertical moves the list and leaves the tiles
    # behind. That was acceptance test 7's failure.
    if vertical:
        items = [r for r in items if r['vertical'] == vertical]
    if stage:
        items = [r for r in items if r['stage'] == stage]
    if owner:
        items = [r for r in items if r['assigned_to'] == owner]
    if mode:
        items = [r for r in items if r.get('mode') == mode]
    if search:
        s_ = search.lower()
        items = [r for r in items
                 if s_ in (r['account'] or '').lower()
                 or s_ in (r['title'] or '').lower()]

    ctx = {'today': today}
    counts = wf.counts(items, ctx)
    ageing = quote_ageing(items, today=today)

    # A `?group=` from an old bookmark or an old tab.
    key = (filter_key or '').strip()
    if not key and group:
        key = _GROUP_TO_FILTER.get(group, '')

    rows = wf.select(items, key, ctx) if key and key != 'everything' \
        else list(items)

    # An ageing bucket narrows whatever view is active, so the heading
    # count and the list agree when somebody clicks a bucket.
    if age:
        wanted = set(ageing['overdue_ids'] if age == 'overdue'
                     else ageing['bucket_ids'].get(age, []))
        rows = [r for r in rows
                if r.get('kind') == 'lead' and r['id'] in wanted]

    if sort == 'value':
        rows.sort(key=lambda r: -(r['value_inr'] or 0))
    elif sort == 'ageing':
        rows.sort(key=lambda r: -(r['ageing_days'] or 0))
    elif sort == 'due':
        rows.sort(key=lambda r: r['due'] or '9999-12-31')
    else:
        rows.sort(key=_sort_key)

    total = len(rows)
    per_page = max(1, min(int(per_page or 50), 200))
    page = max(1, int(page or 1))
    start = (page - 1) * per_page

    # `group_counts` is kept for the Team Workbench's older callers and
    # for the reference docs. It is the same numbers as `counts`, by
    # filter key — not the primary-group tally it used to be, which is
    # the tally that disagreed with every tile.
    group_counts = {g: counts.get(_GROUP_TO_FILTER.get(g, ''), 0)
                    for g, _lbl in rules.GROUPS}

    return {
        'counts': counts,
        'filter': key or 'everything',
        'age': age or '',
        'tiles': [{'key': k, 'label': wf.BY_KEY[k].tile,
                   'count': counts.get(k, 0)} for k in wf.TILE_KEYS],
        'chips': [{'key': k, 'label': wf.BY_KEY[k].chip,
                   'count': counts.get(k, 0)} for k in wf.CHIP_KEYS],
        # The old shapes, so nothing that reads this API breaks on the
        # day of the deploy. Both now carry the registry's numbers.
        'summary': dict(counts, new_assignments=counts.get('newly_assigned', 0),
                        quotes_overdue=ageing['overdue_count']),
        'group_counts': group_counts,
        'groups': [{'key': k, 'label': lbl, 'count': group_counts.get(k, 0)}
                   for k, lbl in rules.GROUPS],
        'items': rows[start:start + per_page],
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': max(1, (total + per_page - 1) // per_page),
        'multi_membership': True,
        #: True when the task ceiling was reached, so every count below
        #: is a floor rather than an answer. The screen says so instead
        #: of presenting a limit as a number.
        'truncated': truncated,
        'quote_ageing': {
            'population': ageing['population'],
            'estimated': ageing['estimated'],
            'counts': ageing['counts'],
            'overdue_count': ageing['overdue_count'],
            'buckets': ageing['buckets'],
        },
    }


#: An old `?group=` maps onto the filter that means the same thing, so
#: a bookmark saved before this release still lands somewhere sensible.
_GROUP_TO_FILTER = {
    rules.G_TODAY: 'action_today',
    rules.G_QUOTES: 'quotations',
    rules.G_FOLLOWUP: 'followups_due',
    rules.G_STALE: 'stale',
    rules.G_DATA: 'data_updates',
    rules.G_NEW: 'newly_assigned',
    rules.G_HIGH_VALUE: 'high_value',
    rules.G_ACCOUNTS: 'account_development',
}
