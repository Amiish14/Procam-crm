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


def _new_mail_on_closed(item, _ctx):
    return _has(item, 'mail_on_closed')


def _account_development(item, _ctx):
    return _has(item, 'account_quiet', 'account_next_action')


def _everything(_item, _ctx):
    return True


# ── the registry ─────────────────────────────────────────────────────
class Filter:
    """One view of the board.

    `tile` and `chip` carry the label each surface shows. Where both
    are set they are deliberately the same words: a tile called
    "Stale leads" over a chip called "Stale opportunities" reads as
    two different measurements of two different things.
    """

    __slots__ = ('key', 'tile', 'chip', 'predicate', 'note')

    def __init__(self, key, predicate, *, tile=None, chip=None, note=''):
        self.key, self.predicate = key, predicate
        self.tile, self.chip, self.note = tile, chip, note

    def matches(self, item, ctx):
        try:
            return bool(self.predicate(item, ctx))
        except Exception:                                 # noqa: BLE001
            # A predicate that trips on one odd row must not empty the
            # whole board. The row is left out of that one view.
            return False


WORKBENCH_FILTERS = (
    Filter('due_today', _due_today, tile='Due today'),
    Filter('overdue', _overdue, tile='Overdue'),
    Filter('action_today', _action_today, chip='Action required today',
           note='Everything with a date of today or earlier.'),
    Filter('quotations', _quotations, chip='Quotations'),
    Filter('rfqs_awaiting_quote', _rfqs_awaiting_quote,
           tile='RFQs awaiting quote'),
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
    Filter('new_mail_on_closed', _new_mail_on_closed,
           tile='New mail on closed lead', chip='New mail on closed lead',
           note='A message arrived on a lead nobody is working.'),
    Filter('account_development', _account_development,
           chip='Account development'),
    Filter('everything', _everything, chip='Everything'),
)

BY_KEY = {f.key: f for f in WORKBENCH_FILTERS}
KEYS = tuple(f.key for f in WORKBENCH_FILTERS)

#: The order the tiles appear in, top of the screen.
TILE_KEYS = tuple(f.key for f in WORKBENCH_FILTERS if f.tile)
#: The order the chips appear in. "Everything" leads, as it does today.
CHIP_KEYS = ('everything',) + tuple(
    f.key for f in WORKBENCH_FILTERS if f.chip and f.key != 'everything')


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
