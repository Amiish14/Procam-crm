"""
A lead belongs to somebody's account before anybody works it.

CRM-ACCOUNT-FIRST-01 (R1).

Why
    The mail intake resolves an account on the way in — seven rules,
    free mail excluded from the domain ones — and then throws the
    answer away. It uses the account only to pick an owner, and
    never writes `lead.company_id`. 352 of 1,029 open leads have no
    account on them, and 659 of 1,053 accounts have nobody named for
    any vertical. So "who is the customer" and "whose customer are
    they" are both unanswerable for a third of the open pipeline,
    and every account-level view — history, credit, the 24-month
    record — silently skips those leads.

Where the line is
    Before work starts, not after. A lead with no account, or an
    account with nobody named for the lead's vertical, cannot be
    accepted, moved past New, registered as an RFQ or quoted. Those
    are the four doors; a rule enforced at three of them is a rule
    people route around, which is why `value_gate` exists in one
    place and is called from five.

Where the line is not
    Lost, On Hold and Not Interested stay open, exactly as in
    `value_gate`. A lead that turns out to be nothing must be
    closeable without first inventing an account for it — otherwise
    the queue fills with records nobody can clear.

    Nothing here reads or writes data when the flag is off. With
    `FEATURE_ACCOUNT_FIRST` off, `check()` returns None for
    everything and the CRM behaves exactly as it does today.
"""
from __future__ import annotations

FLAG = 'FEATURE_ACCOUNT_FIRST'

#: The state a lead is in while it waits. Not a stage: putting it in
#: STAGES_PIPELINE would mean every report, funnel and stage chart
#: grew a column overnight, and a lead waiting for an account has
#: not moved anywhere — it is at New with a question attached.
STATE = 'Account to confirm'

#: Stage moves the gate refuses. Everything in the pipeline past New,
#: plus Won. Spelt out rather than sliced out of the pipeline list,
#: for the reason value_gate spells its own out: a stage inserted in
#: the middle should not silently change what the business requires.
BLOCKED_STAGES = ('Call Done', 'Business Discussion', 'New Opportunity',
                  'RFQ Generated', 'Quoted', 'Under Negotiation', 'Won',
                  'Profile Sent', 'Appointment', 'Visit Done')

MESSAGE = ('This lead has no confirmed account{why}. Confirm the '
           'customer on Accounts → Account check first — the owner, '
           'the history and the credit position all hang off it.')


def enabled(emp_code=None):
    """Is the gate live for the person making this request?

    Person-aware, not just flag-aware. During a pilot the flag is
    limited to a few people and one vertical, and a gate that read
    only `flags.on()` would hold *everybody's* leads the moment the
    switch went on for two of them — which is the opposite of a
    pilot.

    Outside a request there is nobody to ask, so the flag itself is
    the answer: a batch script sees what the flag says.
    """
    from flask import has_request_context, session

    from app.services import flags

    if emp_code is None and has_request_context():
        # Inside a request, nobody signed in means nobody the limit
        # can admit — so the feature is not there. Falling back to
        # the global flag here told an anonymous visitor the route
        # existed, and the pilot's 404 became a 401.
        return flags.on_for(FLAG, session.get('emp_code'))
    if emp_code is None:
        return flags.on(FLAG)
    return flags.on_for(FLAG, emp_code)


def account_of(lead):
    from app import Company, db
    cid = getattr(lead, 'company_id', None)
    return db.session.get(Company, cid) if cid else None


def vertical_of(lead):
    """The vertical this lead belongs to.

    A lead carries `procam_vertical`, an account carries `vertical`,
    and the names have aliases — "PFM" is Project Freight. All three
    problems are already solved in `vertical_heads`, so this asks it
    rather than reading one attribute and getting '' for every lead,
    which is what the first draft of this module did.
    """
    try:
        from app.services import vertical_heads
        return vertical_heads.master_vertical(
            vertical_heads.vertical_of(lead)) or ''
    except Exception:                                     # noqa: BLE001
        return (getattr(lead, 'procam_vertical', '') or '').strip()


def owner_for(account, vertical):
    """Who serves this account for this vertical, or ''.

    Tried under the master name and the name as written: owner rows
    predate the vertical master and some carry the old spelling.
    """
    if account is None or not (vertical or '').strip():
        return ''
    try:
        from app.services import auto_assign, vertical_heads
        names = [vertical.strip()]
        master = vertical_heads.master_vertical(vertical)
        if master and master not in names:
            names.append(master)
        for name in names:
            found = auto_assign.owner_for_vertical(account.id, name)
            if found:
                return found
        return ''
    except Exception:                                     # noqa: BLE001
        return ''


def why_pending(lead):
    """Why this lead is waiting, in words, or '' if it is not.

    Two different problems wear the same badge and need different
    fixes: no account at all is a matching question, an account with
    nobody named for the vertical is a staffing one.
    """
    account = account_of(lead)
    if account is None:
        return 'no account'
    # An account named "Unknown", or one holding our own domain, is
    # not a customer. A lead filed under one *looks* placed, which
    # is worse than being unplaced: it never reaches this queue, and
    # the histories of several real customers pool under one
    # meaningless row. #983 "Unknown" held 4,435 leads.
    from app.services import account_match
    placeholder = account_match.not_a_customer(account)
    if placeholder:
        return f'no account — {placeholder}'
    vertical = vertical_of(lead)
    if not vertical:
        # No vertical means no owner question can even be asked. The
        # lead still needs a person to look at it, and saying why is
        # more use than "no owner for ".
        return 'no vertical on the lead'
    if not owner_for(account, vertical):
        return f'no owner for {vertical}'
    return ''


def pending(lead):
    """Is this lead waiting for an account? False when the flag is off."""
    if not enabled() or lead is None:
        return False
    return bool(why_pending(lead))


def check(lead, new_stage=None, *, action='work on'):
    """None if allowed, else the sentence to show.

    `new_stage` None means "not a stage move" — accepting, raising an
    RFQ, building a quote. Those are refused whatever the stage,
    because each of them starts work.
    """
    if not enabled() or lead is None:
        return None
    from app.services import sales_rules
    # A record being closed is not work starting. Closing must stay
    # possible or the queue becomes a trap.
    if new_stage is not None and not sales_rules.is_open(new_stage) \
            and new_stage != 'Won':
        return None
    if new_stage is not None and new_stage not in BLOCKED_STAGES:
        return None
    why = why_pending(lead)
    if not why:
        return None
    return MESSAGE.format(why=f' ({why})')


def pending_leads(sc=None):
    """Open leads waiting for an account, through the Access Matrix.

    The population behind the tile, the badge and the queue, so all
    three count the same thing. Returns [] with the flag off rather
    than a query nobody asked for.
    """
    from app import Lead
    from app.access import scope as sc_mod
    from app.services import sales_rules

    if not enabled():
        return []
    q = (Lead.query
         .filter(Lead.is_archived.isnot(True),
                 Lead.stage.in_(sales_rules.open_stages())))
    q = sc_mod.leads(q, sc=sc_mod.current() if sc is None else sc)
    # The owner test is per account and per vertical, so it cannot be
    # a SQL filter without duplicating owner_for_vertical in SQL.
    # The population is small — hundreds, not millions.
    return [lead for lead in q.all() if why_pending(lead)]
