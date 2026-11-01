"""
One definition of each Workbench view, used by the tile, the chip and
the list.

The bug this closes
    The board returned two different sets of numbers for the same
    words. `summary` counted *conditions* — an item carries several,
    so "High-value priorities" was 10. `groups[].count` counted the
    *primary group* — an item has exactly one, chosen by priority, and
    action_today beats high_value, so the High-value chip was 0. The
    tile said 10, the chip said 0, and clicking the tile sent a
    `?group=` that listed 0 rows. Three answers to one question, none
    of them wrong on its own terms, and no way for a reader to tell
    which to believe.

    Quote ageing was worse: it bucketed RFQ records, and the business
    tracks quotes by lead *stage*. One item in 1,984 had a quote
    number. So every bucket read 0 while 130 leads sat in Quoted or
    Under Negotiation.

The rule now
    Each key here has exactly one predicate. The tile number, the chip
    number and the rows behind `?filter=<key>` are all
    `[i for i in items if predicate(i, ctx)]` — the same call. They
    cannot disagree, because there is nothing left to disagree with.

Multi-membership is the point, not a defect
    One lead can be overdue *and* stale *and* missing data. It is
    counted in all three. The chips therefore add up to more than
    "Everything", and the screen says so under them. The alternative —
    filing each item under one bucket — is what produced a High-value
    tile of 10 and a High-value chip of 0.

`group` survives, with one job
    It still decides the row's colour and where it sorts. It no longer
    counts anything and no longer filters anything.
"""
from __future__ import annotations

from app.services import sales_rules as rules

#: Stages that mean "a quote is in play", for the Quotations view and
#: the ageing population. Taken from the stage vocabulary rather than
#: from quote records, because that is how the business actually
#: tracks a quote — see the module docstring.
QUOTE_STAGES = ('RFQ Generated', 'Quoted', 'Under Negotiation')

#: The population quote ageing is measured over.
AGEING_STAGES = ('Quoted', 'Under Negotiation')

#: A lead is waiting to be quoted when it is here.
AWAITING_QUOTE_STAGE = 'RFQ Generated'

#: A quote is "due" this many days out.
QUOTE_DUE_WINDOW_DAYS = 1

#: Stages that mean a quote is on the table and the customer is talking.
#: Narrower than QUOTE_STAGES, which includes leads not yet quoted.
NEGOTIATION_STAGES = ('Quoted', 'Under Negotiation')

#: "Idle" on the Overview panel has always meant three days, while the
#: Workbench's own `idle` condition means seven and `stale` means
#: thirty. Three thresholds for one idea is the smaller version of the
#: disagreement this registry exists to end, so the Overview's three
#: days is kept — it is the number people have been reading — and it
#: now lives here with the other two instead of in a route.
QUIET_DAYS = 3


def _has(item, *conditions):
    got = item.get('conditions') or ()
    return any(c in got for c in conditions)


def _date(value):
    """A 'YYYY-MM-DD' string as a date, or None. Never raises — these
    come off records people type into."""
    from datetime import date as _d
    if not value:
        return None
    if isinstance(value, _d):
        return value
    try:
        return _d.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


# ── the predicates ───────────────────────────────────────────────────
def _due_today(item, ctx):
    """A date somebody set for today: a follow-up, a task, or an
    account action."""
    if _has(item, 'followup_today'):
        return True
    if _has(item, 'account_next_action'):
        return _date(item.get('next_action_date')) == ctx['today']
    return False


def _overdue(item, ctx):
    """Any date that has passed on something still open.

    Wider than `followup_overdue`: a quote the customer is holding
    past its validity is overdue too, and so is an account action
    whose date went by last month. Those were previously invisible on
    the Overdue tile, which is why Overdue and Follow-ups due both
    read 643 — they were measuring the same thing twice.
    """
    if _has(item, 'followup_overdue', 'quote_lapsed', 'quote_overdue'):
        return True
    if _has(item, 'account_next_action'):
        when = _date(item.get('next_action_date'))
        return when is not None and when < ctx['today']
    return False


def _overdue_leads(item, ctx):
    """Overdue, and about a lead or the account behind one.

    Split from `overdue` on 19 October. The combined number was 7,475
    for an admin against 146 overdue leads: 97% of it was the task
    engine, which had 7,318 open tasks, none ever closed. One tile
    cannot answer both "how far behind is my pipeline" and "how big
    is the task backlog", and while it tried, it answered neither.
    """
    return item.get('kind') != 'task' and _overdue(item, ctx)


def _overdue_tasks(item, ctx):
    """Overdue, and a task-engine task."""
    return item.get('kind') == 'task' and _overdue(item, ctx)


def _action_today(item, ctx):
    return _due_today(item, ctx) or _overdue(item, ctx)


def _rfqs_awaiting_quote(item, _ctx):
    """An open lead sitting in RFQ Generated with no quote sent yet."""
    return (item.get('kind') == 'lead'
            and item.get('stage') == AWAITING_QUOTE_STAGE
            and not item.get('quote_sent_at'))


def _quotes_due(item, ctx):
    """Awaiting a quote, with the deadline today or tomorrow.

    A blank deadline is not "due" and is not "not due" — it is a
    record nobody can work from, so it is counted under Data updates
    with that reason rather than quietly dropped here.
    """
    if not _rfqs_awaiting_quote(item, ctx):
        return False
    due = _date(item.get('quote_due'))
    if due is None:
        return False
    return (due - ctx['today']).days <= QUOTE_DUE_WINDOW_DAYS


def _quotations(item, _ctx):
    if item.get('kind') == 'lead':
        return item.get('stage') in QUOTE_STAGES
    if item.get('kind') == 'task':
        return item.get('group') == rules.G_QUOTES
    return False


def _followups_due(item, _ctx):
    return _has(item, 'followup_today', 'followup_overdue')


def _stale(item, _ctx):
    return _has(item, 'stale')


def _no_next_action(item, _ctx):
    return _has(item, 'no_next_action')


def _data_updates(item, _ctx):
    """A data-quality check is flagging it, or its quote deadline is
    missing — see `_quotes_due`."""
    return _has(item, 'data_issue', 'quote_due_missing')


def _newly_assigned(item, _ctx):
    return _has(item, 'newly_assigned')


def _high_value(item, _ctx):
    return _has(item, 'high_value')


def _owner_to_confirm(item, _ctx):
    return _has(item, 'owner_to_confirm')


def _new_mail_on_closed(item, _ctx):
    return _has(item, 'mail_on_closed')


def _account_development(item, _ctx):
    return _has(item, 'account_quiet', 'account_next_action')


def _quiet_3d(item, _ctx):
    """Nothing said to this customer for three days or more.

    Reads `last_activity_days`, which `app.services.contact` works out
    from calls, mail and meetings — not `updated_at`, which counts a
    typo fix as contact with a customer.
    """
    if item.get('kind') != 'lead':
        return False
    days = item.get('last_activity_days')
    return days is None or days >= QUIET_DAYS


def _never_actioned(item, _ctx):
    """No call, no introduction mail, no meeting — ever."""
    return item.get('kind') == 'lead' and bool(item.get('never_actioned'))


def _in_negotiation(item, _ctx):
    """Quoted or under negotiation: money already on the table."""
    return (item.get('kind') == 'lead'
            and item.get('stage') in NEGOTIATION_STAGES)


def _value_missing(item, _ctx):
    """Past RFQ with no number on it.

    912 of 945 open leads carry no value of any kind, so every
    forecast built on this CRM is a floor. This is the subset that
    can actually be fixed: somebody has asked us for a price, so a
    value exists in the world and only needs typing in.

    118 of them were answered by copying the figure from the
    opportunity row beside the lead. Those leave the tile only when
    an owner confirms them: a copied figure is enough for the lead
    to move on and not enough to forecast with, and if confirming
    were optional the distinction would last about a week.
    """
    from app.services import value_gate
    if item.get('kind') != 'lead':
        return False
    if item.get('stage') not in value_gate.OPEN_REQUIRES_VALUE:
        return False
    return (not item.get('value_inr')
            or item.get('value_confirmed') is False)


def needs_confirming(item):
    """On the tile because nobody has agreed with the number, rather
    than because there is no number."""
    return bool(item.get('value_inr')) and \
        item.get('value_confirmed') is False


def _everything(_item, _ctx):
    return True


# ── CRM-COCKPIT-02 ───────────────────────────────────────────────────
#: §4.2 — the three silence windows the cockpit shows. Open leads
#: only, measured on `last_activity_at`, which is why these read zero
#: before this release: there was nothing to measure.
NO_ACTIVITY_WINDOWS = (7, 15, 30)


def _is_open_lead(item, ctx):
    return item.get('kind') == 'lead' and \
        item.get('stage') in rules.open_stages()


def _idle_at_least(item, days):
    idle = item.get('idle_days')
    return idle is not None and idle >= days


def _no_activity_7d(item, ctx):
    return _is_open_lead(item, ctx) and _idle_at_least(item, 7)


def _no_activity_15d(item, ctx):
    return _is_open_lead(item, ctx) and _idle_at_least(item, 15)


def _no_activity_30d(item, ctx):
    return _is_open_lead(item, ctx) and _idle_at_least(item, 30)


def _submission_due_today(item, ctx):
    """§2.9 — our offer is due before the end of today (IST)."""
    if not _is_open_lead(item, ctx) or not item.get('submission_due'):
        return False
    if item.get('submission_missed'):
        return False
    left = item.get('submission_hours_left')
    # Up to the end of the IST day, not a 24-hour window: "due today"
    # has to mean the same thing as the date a person reads on screen.
    return left is not None and 0 <= left <= _hours_left_in_ist_day(ctx)


def _hours_left_in_ist_day(ctx):
    from datetime import datetime, timedelta

    from app.services import quote_due as qd

    now = (ctx or {}).get('now') or datetime.utcnow()
    local = now + qd.IST
    end = datetime.combine(local.date(), datetime.max.time())
    return max(0.0, (end - local).total_seconds() / 3600.0)


def _submission_due_48h(item, ctx):
    """§2.9 — due inside two days and not yet answered."""
    if not _is_open_lead(item, ctx) or not item.get('submission_due'):
        return False
    if item.get('submission_missed'):
        return False
    left = item.get('submission_hours_left')
    return left is not None and 0 <= left <= 48


def _submission_missed(item, ctx):
    """§2.7 — the clock passed with no quote and no Bid/Not bid."""
    return _is_open_lead(item, ctx) and bool(item.get('submission_missed'))


def _submission_due_missing(item, ctx):
    """§2.8 — at RFQ Generated or later with no deadline at all.

    The tile owners fill. A lead here is not a mistake by the person
    holding it: 109 of the 137 leads at these stages had no date
    because there was no field to put one in until today.
    """
    if not _is_open_lead(item, ctx):
        return False
    if item.get('submission_due'):
        return False
    if item.get('stage') not in QUOTE_STAGES:
        return False
    # §2.2 — the deadline is mandatory *until a quote is recorded*.
    # Once our offer is in, when the client stopped accepting offers
    # is history, and a tile asking the owner to go and find it is
    # asking for a number that changes nothing.
    return not (item.get('quote_number')
                or item.get('quote_sent_at')
                or (item.get('value_inr') and item.get('stage')
                    == 'Won'))


def _submission_unconfirmed(item, ctx):
    """§2.3 — a date the CRM read that nobody has agreed with yet."""
    return (_is_open_lead(item, ctx) and bool(item.get('submission_due'))
            and not item.get('submission_confirmed'))


def _followup_missing(item, ctx):
    """§3.5 — must read zero after the back-fill, and stay zero."""
    if not _is_open_lead(item, ctx):
        return False
    from app.services import cockpit02_gate as c2

    if item.get('stage') in c2.FOLLOWUP_EXEMPT:
        return False
    return not item.get('next_action_date')


# ── the registry ─────────────────────────────────────────────────────
class Filter:
    """One view of the board.

    `tile` and `chip` carry the label each surface shows. Where both
    are set they are deliberately the same words: a tile called
    "Stale leads" over a chip called "Stale opportunities" reads as
    two different measurements of two different things.
    """

    __slots__ = ('key', 'tile', 'chip', 'overview', 'predicate', 'note')

    def __init__(self, key, predicate, *, tile=None, chip=None,
                 overview=None, note=''):
        self.key, self.predicate = key, predicate
        self.tile, self.chip, self.note = tile, chip, note
        #: The label the Overview / Sales Intelligence panel shows, when
        #: that panel has a tile for this view. Three of them have no
        #: Workbench tile and exist only there; the rest carry both
        #: labels so the two screens cannot drift apart again.
        self.overview = overview

    def matches(self, item, ctx):
        try:
            return bool(self.predicate(item, ctx))
        except Exception:                                 # noqa: BLE001
            # A predicate that trips on one odd row must not empty the
            # whole board. The row is left out of that one view.
            return False


WORKBENCH_FILTERS = (
    Filter('due_today', _due_today, tile='Due today', overview='Due today',
           note='A follow-up, task or account action dated today.'),
    # `overdue` keeps no tile and no chip. It is still a filter, so a
    # bookmark or a link saved before the split lands somewhere
    # sensible rather than on an error — see LEGACY_KEYS.
    Filter('overdue', _overdue,
           note='Everything overdue, leads and tasks together. Split '
                'into two tiles on 19 October; kept so older links '
                'still work.'),
    Filter('overdue_leads', _overdue_leads,
           tile='Overdue leads', overview='Overdue leads',
           note='Any date that has passed on a lead or account still '
                'open — a follow-up, a quote past its validity, an '
                'account action.'),
    Filter('overdue_tasks', _overdue_tasks,
           tile='Overdue tasks', overview='Overdue tasks',
           note='Task-engine tasks past their SLA.'),
    Filter('action_today', _action_today, chip='Action required today',
           note='Everything with a date of today or earlier.'),
    Filter('quotations', _quotations, chip='Quotations'),
    Filter('rfqs_awaiting_quote', _rfqs_awaiting_quote,
           tile='RFQs awaiting quote', overview='Awaiting quote',
           note='RFQ generated and no quote sent yet.'),
    Filter('quotes_due', _quotes_due, tile='Quotes due'),
    Filter('followups_due', _followups_due,
           tile='Customer follow-up', chip='Customer follow-up'),
    Filter('stale', _stale,
           tile='Stale opportunities', chip='Stale opportunities'),
    Filter('no_next_action', _no_next_action, tile='No next action'),
    # The spec's table spells these two differently on the tile and
    # the chip ("Data updates required" / "Data update required",
    # "New assignments" / "Newly assigned"). They are one number, and
    # two names for one number is the smaller version of exactly the
    # confusion this release exists to remove — so both read the
    # tile's wording.
    Filter('data_updates', _data_updates,
           tile='Data updates required', chip='Data updates required'),
    Filter('newly_assigned', _newly_assigned,
           tile='New assignments', chip='New assignments'),
    Filter('high_value', _high_value,
           tile='High-value priorities', chip='High-value priorities'),
    Filter('owner_to_confirm', _owner_to_confirm,
           tile='Owner to confirm', chip='Owner to confirm',
           note='A rule chose the owner and nobody has checked it.'),
    Filter('new_mail_on_closed', _new_mail_on_closed,
           tile='New mail on closed lead', chip='New mail on closed lead',
           note='A message arrived on a lead nobody is working.'),
    Filter('account_development', _account_development,
           chip='Account development'),
    # Three views the Overview panel has always shown and the Workbench
    # never had. They were five ad-hoc queries in `/api/my-work`, which
    # is how "Idle 3+ days" came to mean something the Workbench had no
    # word for. They carry no tile and no chip: the Workbench is not
    # getting three new tiles, it is getting three filters its sibling
    # screen can link to.
    Filter('quiet_3d', _quiet_3d, overview='Idle 3+ days',
           note='Nothing said to the customer for three days or more.'),
    Filter('never_actioned', _never_actioned, overview='Never actioned',
           note='No call, no introduction mail and no meeting, ever.'),
    Filter('in_negotiation', _in_negotiation, overview='In negotiation',
           note='Quoted or under negotiation.'),
    # ── CRM-COCKPIT-02 §2.9, §3.5, §4.2 ─────────────────────────────
    # Each one is a Filter and nothing else, so the tile, the chip and
    # the rows behind ?filter=<key> are the same call. That is the
    # whole of §4.6, and the reason these are not four more ad-hoc
    # counts in the cockpit endpoint.
    Filter('quote_due_today', _submission_due_today,
           tile='Quotes due today', chip='Quotes due today',
           overview='Quotes due today',
           note='Our offer has to be with the client before the end '
                'of today, and no quote is recorded yet.'),
    Filter('quote_due_48h', _submission_due_48h,
           tile='Due in 48 h', chip='Due in 48 h',
           note='The submission deadline is inside two days.'),
    Filter('missed_submission', _submission_missed,
           tile='Missed submission', chip='Missed submission',
           overview='Missed submission',
           note='The deadline passed with no quote recorded and no '
                'Bid / Not bid decision. Somebody has to say which '
                'it was.'),
    Filter('due_date_missing', _submission_due_missing,
           tile='Due date missing', chip='Due date missing',
           note='At RFQ Generated or later with no submission '
                'deadline. The owner fills it from the enquiry mail.'),
    Filter('due_date_unconfirmed', _submission_unconfirmed,
           tile='Due date to confirm', chip='Due date to confirm',
           note='The CRM read this date off the mail and nobody has '
                'checked it yet.'),
    Filter('followup_missing', _followup_missing,
           tile='Follow-up missing', chip='Follow-up missing',
           note='An open lead with no next date. Nobody is working a '
                'lead that is on no list.'),
    Filter('no_activity_7d', _no_activity_7d,
           tile='No activity 7D', chip='No activity 7D',
           note='Nothing at all has happened on this lead for a '
                'week — no mail, note, call, quote or stage change.'),
    Filter('no_activity_15d', _no_activity_15d,
           tile='No activity 15D', chip='No activity 15D',
           note='Nothing for a fortnight.'),
    Filter('no_activity_30d', _no_activity_30d,
           tile='No activity 30D', chip='No activity 30D',
           note='Nothing for a month.'),
    Filter('value_missing', _value_missing,
           tile='Value missing', chip='Value missing',
           note='Open and at RFQ Generated or later with no quoted '
                'amount and no estimate — or with a figure copied '
                'from the opportunity that nobody has confirmed. '
                'Confirm it, or type a better one.'),
    Filter('everything', _everything, chip='Everything'),
)

BY_KEY = {f.key: f for f in WORKBENCH_FILTERS}
KEYS = tuple(f.key for f in WORKBENCH_FILTERS)

#: The order the tiles appear in, top of the screen.
TILE_KEYS = tuple(f.key for f in WORKBENCH_FILTERS if f.tile)
#: The order the chips appear in. "Everything" leads, as it does today.
CHIP_KEYS = ('everything',) + tuple(
    f.key for f in WORKBENCH_FILTERS if f.chip and f.key != 'everything')
#: The order the Overview / Sales Intelligence tiles appear in. Spelt
#: out rather than taken from registry order, because this is the
#: order people have been reading since September and a tile moving
#: because a filter was inserted above it is a change nobody asked
#: for. A key here without an `overview` label is a mistake, and the
#: test suite says so.
OVERVIEW_KEYS = ('overdue_leads', 'overdue_tasks', 'due_today',
                 'quiet_3d', 'never_actioned', 'rfqs_awaiting_quote',
                 'in_negotiation')

#: Keys that deliberately carry no tile, chip or Overview label. They
#: stay in the registry so a link saved before a split still resolves;
#: nothing on any screen offers them. Anything else without a surface
#: is unreachable by accident, and the suite says so.
LEGACY_KEYS = ('overdue',)


def get(key):
    return BY_KEY.get((key or '').strip())


def counts(items, ctx):
    """{key: number of distinct items matching} for every key.

    One pass over the items, every predicate evaluated per item. The
    number here is the number the chip shows, the number the tile
    shows, and the number of rows `?filter=<key>` returns — because
    `select` below runs the same predicate.
    """
    out = {f.key: 0 for f in WORKBENCH_FILTERS}
    for item in items:
        for f in WORKBENCH_FILTERS:
            if f.matches(item, ctx):
                out[f.key] += 1
    return out


def select(items, key, ctx):
    """The items a filter key matches, in the order given."""
    f = get(key)
    if f is None:
        return list(items)
    return [i for i in items if f.matches(i, ctx)]


def describe():
    """[(key, tile label, chip label, note)] for the docs generator."""
    return [(f.key, f.tile or '', f.chip or '', f.note)
            for f in WORKBENCH_FILTERS]


def overview_tiles(counts):
    """The Overview panel's tiles, labelled and counted from here.

    The panel used to hold its own five SQL queries. Two of them
    answered a question the Workbench also answered, differently:
    Overdue was `followup_date < today` there and "any date that has
    passed on something still open" here, so one screen said 643 and
    the other said rather more. They are now one number with one name.
    """
    return [{'key': k, 'label': BY_KEY[k].overview,
             'count': counts.get(k, 0), 'note': BY_KEY[k].note}
            for k in OVERVIEW_KEYS]
