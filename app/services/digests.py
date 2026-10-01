"""
What each scheduled report says, worked out once.

The Daily Workbench already decides what a person has to act on
(`app/workbench/service.board`), and `app/services/sales_rules.py`
already decides what "overdue", "stale", "due today" and the quote
ageing buckets mean. A report that re-derived any of that would drift
away from the screen it claims to summarise, and people would stop
trusting both. So every builder here resolves that person's Scope, asks
the Workbench for their board, and does nothing but arrange the answer
for reading.

The builders are pure: they read, they return a dict, they send
nothing. `scripts/send_reports.py` delivers; `render()` turns a dict
into the HTML of one of `templates/email/*.html`.

Prior-period comparison
    There is no history table behind the Workbench, so "a week ago" is
    the same rules run with last week's date against today's records.
    It answers "how much of this was already late a week ago", not
    "what did the board look like then". The templates say so, because
    a reader who assumed the stronger meaning would draw the wrong
    conclusion about the week's work.

Cost
    A board is not cheap — it runs the hygiene checks and the ageing
    service. The rollup reports need every item, not the first page, so
    they ask the Workbench's own item builders for the rest rather than
    counting a second way here. Weekly and monthly jobs can afford it;
    a second definition of "overdue" could not be afforded at all.
"""
from __future__ import annotations

from datetime import timedelta

from app.services import sales_rules as rules

#: Rows one section of an email may list. A digest that runs to five
#: pages is deleted unread, so the overflow is a count and a button.
MAX_PER_SECTION = 15

#: `board()` caps a page at 200, which is more than any email shows and
#: enough that most people's whole board arrives in one read.
_BOARD_PAGE = 200

#: The end-of-day report raises missed and imminent deadlines, and
#: nothing else. "Stale", "newly assigned" and a missing quote value
#: are real, but they are work for the morning list; a report somebody
#: reads on the way home has to be a list they could still clear.
EXCEPTION_CONDITIONS = (
    'followup_overdue', 'followup_today', 'quote_overdue', 'quote_lapsed',
    'account_next_action',
)

#: report key → (builder name, template, subject). `scripts/send_reports.py`
#: and the tests read this rather than hard-coding either.
REPORTS = {
    'daily': ('daily_action_report', 'email/daily_action.html',
              'Your CRM actions for {date}'),
    'exceptions': ('end_of_day_exceptions', 'email/exceptions.html',
                   'End of day: {count} open item(s) — {date}'),
    'weekly_user': ('weekly_user', 'email/weekly_user.html',
                    'Your week on the CRM — week ending {date}'),
    'weekly_head': ('weekly_vertical_head', 'email/weekly_head.html',
                    'Team review — week ending {date}'),
    'monthly': ('monthly_management', 'email/monthly_management.html',
                'CRM management report — {date}'),
}


# ── the pieces every report is built from ────────────────────────────
def _scope(emp_code):
    from app.access import scope as sc_mod
    return sc_mod.for_employee(emp_code)


def company_scope():
    """Every record there is — the management report, and nothing else.

    Built explicitly rather than by borrowing an administrator's code,
    so the report does not quietly change shape when somebody's access
    profile is edited.
    """
    from app.access.scope import Scope
    from app.models.access import DataScope
    return Scope('', '', None, set(), DataScope.ALL)


def _person(emp_code):
    from app import Employee
    e = Employee.query.filter_by(emp_code=emp_code).first()
    return {
        'emp_code': (emp_code or '').upper(),
        'name': (getattr(e, 'name', '') or '') or (emp_code or ''),
        'email': (getattr(e, 'email', '') or '').strip(),
        'vertical': (getattr(e, 'vertical', '') or ''),
        'is_vertical_head': bool(getattr(e, 'is_vertical_head', False)),
    }


def _board(sc, today=None):
    from app.workbench import service as wb
    return wb.board(sc, today=today, per_page=_BOARD_PAGE)


def _all_items(sc, board, today=None):
    """Every item on this board, not only the page the email shows.

    When the board fits in one page — which it does for most people —
    the page already is everything, and nothing further is read.
    """
    if board['total'] <= len(board['items']):
        return list(board['items'])
    from app.workbench import service as wb
    items, _ageing = wb.lead_items(sc, today=today)
    return items + wb.account_items(sc, today=today) + wb.task_items(
        sc, today=today)


def _money(value_inr):
    """Money as the CRM writes it everywhere else: lakh, then crore."""
    from app.services import lead_value
    return lead_value.format_inr(value_inr) if value_inr is not None else ''


def _row(item):
    """One line of an email table, from one board item."""
    return {
        'kind': item['kind'],
        'id': item['id'],
        'title': item['title'],
        'account': item['account'],
        'stage': item['stage'],
        'vertical': item['vertical'],
        'due': item['due'],
        'value': _money(item['value_inr']),
        'value_inr': item['value_inr'],
        'owner': item['assigned_name'] or item['assigned_to'],
        'owner_code': item['assigned_to'],
        'quiet_days': item.get('quiet_days'),
        'ageing_days': item.get('ageing_days'),
        'reasons': list(item['reasons']),
        'conditions': list(item['conditions']),
        'route': item['route'],
    }


def _section(key, items, *, label=None, limit=MAX_PER_SECTION):
    """A named block of a report: what it is, how much of it there is,
    and the first `limit` rows."""
    rows = [_row(i) for i in items]
    rows.sort(key=lambda r: (r['due'] or '9999-12-31',
                             -(r['value_inr'] or 0)))
    value = sum(r['value_inr'] or 0 for r in rows)
    return {
        'key': key,
        'label': label or rules.GROUP_LABELS.get(key, key),
        'count': len(rows),
        'value': _money(value) if value else '',
        'items': rows[:limit],
        'more': max(0, len(rows) - limit),
        'link': f'/my-work?group={key}' if key in rules.GROUP_LABELS
                else '/my-work',
    }


def _sections(items, keys=None, *, limit=MAX_PER_SECTION):
    """Board items split into the Workbench's own groups, in its order."""
    out = []
    for key, label in rules.GROUPS:
        if keys is not None and key not in keys:
            continue
        rows = [i for i in items if i['group'] == key]
        if rows:
            out.append(_section(key, rows, label=label, limit=limit))
    return out


def _ageing_rows(board):
    a = board['quote_ageing']
    return {
        'overdue': a['overdue_count'],
        'buckets': [b for b in a['buckets']],
        'total': sum(b['count'] for b in a['buckets']),
    }


def _deltas(now, before):
    """This period against the last, for every counter on the board."""
    out = {}
    for key, value in (now or {}).items():
        was = (before or {}).get(key, 0)
        out[key] = {'now': value, 'before': was, 'change': value - was}
    return out


def _by_owner(items):
    """Per-person rollup for the reports a manager reads."""
    people = {}
    for it in items:
        code = it['assigned_to'] or '—'
        row = people.setdefault(code, {
            'owner_code': code,
            'owner': it['assigned_name'] or code,
            'total': 0, 'overdue': 0, 'due_today': 0, 'stale': 0,
            'no_next_action': 0, 'high_value': 0, 'value_inr': 0,
        })
        conds = it['conditions']
        row['total'] += 1
        if {'followup_overdue', 'quote_overdue',
                'quote_lapsed'} & set(conds):
            row['overdue'] += 1
        if {'followup_today', 'account_next_action'} & set(conds):
            row['due_today'] += 1
        if 'stale' in conds:
            row['stale'] += 1
        if 'no_next_action' in conds:
            row['no_next_action'] += 1
        if 'high_value' in conds:
            row['high_value'] += 1
            row['value_inr'] += it['value_inr'] or 0
    rows = sorted(people.values(),
                  key=lambda r: (-r['overdue'], -r['total'], r['owner']))
    for r in rows:
        r['value'] = _money(r['value_inr']) if r['value_inr'] else ''
    return rows


def _by_vertical(items):
    verticals = {}
    for it in items:
        key = it['vertical'] or 'Unassigned'
        row = verticals.setdefault(key, {
            'vertical': key, 'total': 0, 'overdue': 0, 'high_value': 0,
            'value_inr': 0})
        row['total'] += 1
        if {'followup_overdue', 'quote_overdue',
                'quote_lapsed'} & set(it['conditions']):
            row['overdue'] += 1
        if 'high_value' in it['conditions']:
            row['high_value'] += 1
            row['value_inr'] += it['value_inr'] or 0
    rows = sorted(verticals.values(), key=lambda r: -r['total'])
    for r in rows:
        r['value'] = _money(r['value_inr']) if r['value_inr'] else ''
    return rows


def _envelope(key, emp_code, today, **rest):
    base = {
        'report': key,
        'template': REPORTS[key][1],
        'person': _person(emp_code) if emp_code else None,
        'date': str(today),
        'generated_for': (emp_code or '').upper(),
        'link': '/my-work',
    }
    base.update(rest)
    return base


# ── the five reports ─────────────────────────────────────────────────
def daily_action_report(emp_code, *, today=None):
    """The morning list: everything this person has to act on today.

    The same board as `/my-work`, in the Workbench's own group order, so
    opening the CRM after reading the email is not a surprise.
    """
    today = today or rules.business_today()
    sc = _scope(emp_code)
    board = _board(sc, today=today)
    # Every item, not the first page: a person with hundreds of open
    # records would otherwise be sent whatever happened to sort onto
    # page one, and never see their stale leads or data gaps at all.
    sections = _sections(_all_items(sc, board, today=today))
    return _envelope(
        'daily', emp_code, today,
        summary=board['summary'],
        total=board['total'],
        group_counts=board['group_counts'],
        sections=sections,
        quote_ageing=_ageing_rows(board),
        empty=board['total'] == 0,
    )


def end_of_day_exceptions(emp_code, *, today=None):
    """What is still open as the day closes.

    Only missed and imminent deadlines, and only where nobody has been
    in touch today: a follow-up that was due today and has a call
    logged against it today is done, and reporting it as an exception
    teaches people to ignore the report.
    """
    today = today or rules.business_today()
    sc = _scope(emp_code)
    board = _board(sc, today=today)
    items = [i for i in _all_items(sc, board, today=today)
             if set(i['conditions']) & set(EXCEPTION_CONDITIONS)
             and i.get('last_activity_days') != 0]
    sections = _sections(items)
    count = len(items)
    return _envelope(
        'exceptions', emp_code, today,
        summary=board['summary'],
        total=count,
        count=count,
        sections=sections,
        quote_ageing=_ageing_rows(board),
        empty=count == 0,
        # Straight to the group the reader has to clear, not the whole
        # board: the point of an evening report is one short list.
        link=f'/my-work?group={rules.G_TODAY}',
    )


def weekly_user(emp_code, *, today=None, days=7):
    """One person's week, against the week before it."""
    today = today or rules.business_today()
    before = today - timedelta(days=days)
    sc = _scope(emp_code)
    board = _board(sc, today=today)
    prior = _board(sc, today=before)
    items = _all_items(sc, board, today=today)
    return _envelope(
        'weekly_user', emp_code, today,
        period={'from': str(before), 'to': str(today), 'days': days},
        summary=board['summary'],
        prior_summary=prior['summary'],
        deltas=_deltas(board['summary'], prior['summary']),
        total=board['total'],
        prior_total=prior['total'],
        sections=_sections(items),
        high_value=_section('high_value',
                            [i for i in items
                             if 'high_value' in i['conditions']]),
        quote_ageing=_ageing_rows(board),
        empty=board['total'] == 0,
    )


def weekly_vertical_head(emp_code, *, today=None, days=7):
    """The team's week: who is carrying what, and what slipped.

    Built from the head's own Scope, so a head sees their vertical and
    their reports and nobody else's — the same boundary as the Team
    Workbench, because it is the same resolver.
    """
    today = today or rules.business_today()
    before = today - timedelta(days=days)
    sc = _scope(emp_code)
    board = _board(sc, today=today)
    prior = _board(sc, today=before)
    items = _all_items(sc, board, today=today)
    return _envelope(
        'weekly_head', emp_code, today,
        period={'from': str(before), 'to': str(today), 'days': days},
        summary=board['summary'],
        prior_summary=prior['summary'],
        deltas=_deltas(board['summary'], prior['summary']),
        total=board['total'],
        prior_total=prior['total'],
        people=_by_owner(items),
        verticals=_by_vertical(items),
        attention=_section('action_today',
                           [i for i in items
                            if 'followup_overdue' in i['conditions']
                            or 'quote_overdue' in i['conditions']],
                           label='Overdue across the team'),
        high_value=_section('high_value',
                            [i for i in items
                             if 'high_value' in i['conditions']]),
        quote_ageing=_ageing_rows(board),
        empty=board['total'] == 0,
        link='/team-workbench',
    )


def monthly_management(*, today=None, days=30):
    """The whole company, for the people who answer for it.

    No `emp_code`: this report is not about one person's work, and
    scoping it to whoever happens to receive it would make two
    recipients disagree about the same month.
    """
    today = today or rules.business_today()
    before = today - timedelta(days=days)
    sc = company_scope()
    board = _board(sc, today=today)
    prior = _board(sc, today=before)
    items = _all_items(sc, board, today=today)
    return _envelope(
        'monthly', '', today,
        period={'from': str(before), 'to': str(today), 'days': days},
        summary=board['summary'],
        prior_summary=prior['summary'],
        deltas=_deltas(board['summary'], prior['summary']),
        total=board['total'],
        prior_total=prior['total'],
        verticals=_by_vertical(items),
        people=_by_owner(items)[:20],
        high_value=_section('high_value',
                            [i for i in items
                             if 'high_value' in i['conditions']]),
        quote_ageing=_ageing_rows(board),
        empty=board['total'] == 0,
        link='/team-workbench',
    )


def build(key, emp_code=None, *, today=None):
    """One report by its key, for `scripts/send_reports.py`."""
    if key not in REPORTS:
        raise KeyError(f'no such report: {key!r}')
    builder = globals()[REPORTS[key][0]]
    if key == 'monthly':
        return builder(today=today)
    return builder(emp_code, today=today)


# ── delivery-side helpers (still no sending) ─────────────────────────
def subject_for(report):
    """The subject line, filled from the report's own numbers."""
    pattern = REPORTS[report['report']][2]
    who = (report.get('person') or {}).get('name') or ''
    return pattern.format(date=report.get('date', ''),
                          count=report.get('count', report.get('total', 0)),
                          name=who)


def base_url():
    from email_ingest import notifier
    return notifier.base_url()


def render(report):
    """The report as the HTML of its template.

    Needs an application context, not a request: these run from a timer.
    """
    from flask import render_template
    return render_template(report['template'], r=report,
                           base_url=base_url(),
                           groups=rules.GROUP_LABELS)
