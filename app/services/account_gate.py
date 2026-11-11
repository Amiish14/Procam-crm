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
    Before work starts, not after. A lead with no account cannot be
    accepted, moved past New, or quoted. A rule enforced at some
    doors and not others is a rule people route around, which is why
    `value_gate` exists in one place and is called from five.

    Registering an RFQ was a fourth door and is not any more
    (CRM-MAIL-2026-11-11 §1). Writing down what the customer asked
    for is not work going out: refusing it lost the enquiry instead
    of protecting it, and because the refusal rolled the save back it
    took the RFQ details typed in the same form with it. The doors
    are now named in `GATED_ACTIONS` and `RECORDING_ACTIONS` rather
    than being whatever a caller passed.

Where the line is not
    Lost, On Hold and Not Interested stay open, exactly as in
    `value_gate`. A lead that turns out to be nothing must be
    closeable without first inventing an account for it — otherwise
    the queue fills with records nobody can clear.

    Nothing here reads or writes data when the flag is off. With
    `FEATURE_ACCOUNT_FIRST` off, `check()` returns None for
    everything and the CRM behaves exactly as it does today.

Who the line is not drawn against (CRM-WA-01b)
    Two very different facts wore the same refusal. "No account"
    means nobody knows who the customer is, which has to be settled
    before the lead is worked. "No owner for this vertical" means we
    know the customer perfectly well and have not written down who
    serves them *for this vertical* — a staffing question, usually
    about a vertical the person being refused does not work in.

    A Freight Forwarding lead whose account is owned by an Air
    Freight colleague was refused on the second fact, which is the
    one the lead's own owner cannot fix and should never have been
    asked to. So account ownership no longer stops the people with a
    claim on the lead — its owner (Primary PIC), its secondary PIC,
    or anybody in the lead's own vertical. It still stops a passer-by,
    and the refusal now names the rule and who can clear it.
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

#: The name this rule answers to, so a refusal can be traced to it
#: without grepping. Shown in every message.
RULE = 'Account first (FEATURE_ACCOUNT_FIRST)'

#: Why a lead is waiting, as a code rather than a sentence. The
#: sentences are for people; the code is so `check` can tell the
#: matching question from the staffing one, which is the whole of
#: CRM-WA-01b.
WHY_NO_ACCOUNT = 'no_account'
WHY_PLACEHOLDER = 'placeholder_account'
WHY_NO_VERTICAL = 'no_vertical'
WHY_NO_OWNER = 'no_owner'

#: Reasons that describe who owns the account rather than whether we
#: know who the customer is. These never refuse somebody with a claim
#: on the lead: account ownership must not block lead work.
OWNERSHIP_REASONS = (WHY_NO_OWNER,)

#: What a person's claim on the lead is called. Roles, never names:
#: the words go into a refusal somebody reads, and the codes are read
#: from the lead at the time.
CLAIM_OWNER = 'the lead owner (Primary PIC)'
CLAIM_SECOND = 'the secondary PIC'
CLAIM_VERTICAL = "in the lead's own vertical"

#: Actions that are not a stage move, and which door each one is.
#:
#: CRM-MAIL-2026-11-11 §1: the gate was refusing a *save*. A lead with
#: no confirmed account cannot be quoted — that is the line the rule
#: exists for — but refusing to record what the customer asked for
#: loses the enquiry instead of protecting it, and the refusal rolled
#: the whole save back, so the RFQ details typed in the same form were
#: discarded with it.
#:
#: So the actions are named rather than lumped under "work on". An
#: action not in either list is refused, deliberately: a new call site
#: has to say which kind it is, and the safe default for an unnamed
#: one is the old behaviour.
GATED_ACTIONS = ('build a quote', 'work on')
RECORDING_ACTIONS = ('register an RFQ', 'save the lead', 'add a note',
                     'attach a file', 'add a contact', 'log an activity')

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


def reason_for(lead):
    """(code, words) for why this lead is waiting, or ('', '').

    One pass, two answers: the code so the gate can reason about the
    kind of problem, the words so a person is told which one it is.
    `why_pending` is the words on their own, which is what the
    Account check screen has always read.
    """
    account = account_of(lead)
    if account is None:
        return WHY_NO_ACCOUNT, 'no account'
    # An account named "Unknown", or one holding our own domain, is
    # not a customer. A lead filed under one *looks* placed, which
    # is worse than being unplaced: it never reaches this queue, and
    # the histories of several real customers pool under one
    # meaningless row. #983 "Unknown" held 4,435 leads.
    from app.services import account_match
    placeholder = account_match.not_a_customer(account)
    if placeholder:
        return WHY_PLACEHOLDER, f'no account — {placeholder}'
    vertical = vertical_of(lead)
    if not vertical:
        # No vertical means no owner question can even be asked. The
        # lead still needs a person to look at it, and saying why is
        # more use than "no owner for ".
        return WHY_NO_VERTICAL, 'no vertical on the lead'
    if not owner_for(account, vertical):
        return WHY_NO_OWNER, f'no owner for {vertical}'
    return '', ''


def why_pending(lead):
    """Why this lead is waiting, in words, or '' if it is not.

    Two different problems wear the same badge and need different
    fixes: no account at all is a matching question, an account with
    nobody named for the vertical is a staffing one.
    """
    return reason_for(lead)[1]


def _master(vertical):
    """A vertical's managed name, or the name as written.

    Deliberately not `master_vertical` on its own: that collapses
    every value nobody has told the CRM about onto Management, which
    is right for routing a notification and wrong here — it would
    make an Air Freight person and a Freight Forwarding person
    "the same vertical" the moment either name was missing from the
    master, and hand out the CRM-WA-01b exemption to both.
    """
    value = (vertical or '').strip()
    if not value:
        return ''
    try:
        from app.services import vertical_heads
        if vertical_heads.is_mapped(value):
            return (vertical_heads.master_vertical(value) or value).strip()
    except Exception:                                     # noqa: BLE001
        pass
    return value


def _same_vertical(lead, emp_code):
    """Is this person in the lead's own vertical?

    Both sides go through the vertical master, because the employee
    row says "Project Freight" and the lead says "PFM" and they are
    the same vertical. An employee with no vertical matches nothing
    rather than everything.
    """
    from app import Employee

    mine = ''
    try:
        emp = Employee.query.filter_by(emp_code=emp_code).first()
        mine = _master(getattr(emp, 'vertical', '') or '')
    except Exception:                                     # noqa: BLE001
        return False
    theirs = _master(getattr(lead, 'procam_vertical', '') or '')
    if not theirs:
        theirs = _master(vertical_of(lead))
    return bool(mine) and bool(theirs) and mine.lower() == theirs.lower()


def worker_claim(lead, emp_code=None):
    """This person's claim on the lead, in words, or ''.

    CRM-WA-01b §3: a user may work a lead they own, are secondary PIC
    on, or that is in their vertical — whoever owns the linked
    account. Deliberately *not* the Access Matrix: the matrix answers
    "may you open this record", which an admin or a whole-company
    viewer passes for every lead in the book. The question here is
    narrower — is this lead yours to move — and an administrator
    looking at somebody else's lead should see the staffing problem
    rather than walk past it.
    """
    from flask import has_request_context, session

    if lead is None:
        return ''
    code = emp_code
    if code is None and has_request_context():
        code = session.get('emp_code')
    code = (code or '').strip().upper()
    if not code:
        return ''
    if (getattr(lead, 'assigned_to', '') or '').strip().upper() == code:
        return CLAIM_OWNER
    if (getattr(lead, 'secondary_owner', '') or '').strip().upper() == code:
        return CLAIM_SECOND
    if _same_vertical(lead, code):
        return CLAIM_VERTICAL
    return ''


def resolver_for(lead):
    """Who can clear this lead's account question, by role.

    The same rule `account_check.may_clear` enforces (D6): the head of
    the lead's vertical, or a CRM admin. Named by role and read from
    the lead — no codes or names live in this file.
    """
    vertical = vertical_of(lead)
    who = (f'the head of {vertical}' if vertical
           else "the lead's vertical head")
    return f'{who}, or a CRM admin, on Accounts → Account check'


def name_mismatch(lead):
    """The two names, when the lead's and its account's disagree.

    Returns (lead_name, account_name) or (). Not a refusal — CRM-WA-01b
    §3 is explicit that a name that does not match is a question to
    ask on the lead ("Confirm account"), not a reason to stop work.
    The loose normaliser from the data-quality checks, so the two
    screens flag the same pairs: a false prompt costs a glance, and
    collapsing real differences would cost the prompt its point.
    """
    account = account_of(lead)
    if account is None:
        return ()
    try:
        from app.data_quality import definitions as defs
        norm = defs.norm_name
    except Exception:                                     # noqa: BLE001
        def norm(text):
            return (text or '').strip().lower()
    mine = norm(getattr(lead, 'company', '') or '')
    theirs = norm(account.name or '')
    if mine and theirs and mine != theirs:
        return (getattr(lead, 'company', '') or '', account.name or '')
    return ()


def message_for(lead, code, why):
    """The refusal, naming the rule and who can clear it."""
    where = resolver_for(lead)
    if code == WHY_NO_OWNER:
        return (f'{RULE}: the account on this lead has {why}, so nobody '
                f'is named to serve this customer for it. Account '
                f'ownership does not stop the lead\'s own owner, its '
                f'secondary PIC or anybody in its vertical — you are '
                f'none of those on this lead, so ask {where}.')
    if code == WHY_NO_VERTICAL:
        return (f'{RULE}: this lead has no vertical, so there is no '
                f'owner question to ask yet. Set the vertical on the '
                f'lead, or ask {where}.')
    return (f'{RULE}: this lead has no confirmed account ({why}). '
            f'Confirm the customer on Accounts → Account check first '
            f'— the owner, the history and the credit position all '
            f'hang off it. {where[0].upper()}{where[1:]} can do it.')


def pending(lead):
    """Is this lead waiting for an account? False when the flag is off."""
    if not enabled() or lead is None:
        return False
    return bool(why_pending(lead))


def check(lead, new_stage=None, *, action='work on', emp_code=None):
    """None if allowed, else the sentence to show.

    `new_stage` None means "not a stage move" — accepting, raising an
    RFQ, building a quote. Those are refused whatever the stage,
    because each of them starts work.

    `emp_code` is who is asking; it defaults to the signed-in user.
    It decides only the ownership half: a lead with no account at all
    is refused for everybody, including its owner, because nobody can
    work a customer we have not identified.
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
    # Not a stage move: only the doors that start work outward are
    # gated. Writing down what the customer asked for is not one.
    if new_stage is None and action in RECORDING_ACTIONS:
        return None
    code, why = reason_for(lead)
    if not code:
        return None
    if code in OWNERSHIP_REASONS and worker_claim(lead, emp_code):
        # CRM-WA-01b: the account's staffing is not this person's
        # question, and the lead is theirs to move.
        return None
    return message_for(lead, code, why)


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


def help_for(lead, *, emp_code=None, limit=6):
    """What the screen needs to offer the account picker instead of a
    dead end.

    CRM-MAIL-2026-11-11 §6: "can the account be assigned while
    assigning the RFQ?" — yes, and the answer has to travel with the
    refusal, or the user is sent to another screen and loses the
    dialog they were in. So a refused write returns the candidates
    the picker would have shown, the reason, and who can clear it.

    Suggestions come from `lead_account.suggest`, which is the
    matcher the intake itself uses. One matcher, three screens.
    """
    code, why = reason_for(lead)
    out = {
        'reason': code,
        'reason_words': why,
        'resolver': resolver_for(lead) if code else '',
        'vertical': vertical_of(lead),
        'claim': worker_claim(lead, emp_code),
        'can_pick': False,
        'suggestions': [],
    }
    if not code:
        return out
    try:
        from app.services import lead_account
        out['can_pick'] = bool(lead_account.enabled())
        if out['can_pick'] and code in (WHY_NO_ACCOUNT, WHY_PLACEHOLDER):
            rows = lead_account.suggest(lead, limit=limit) or []
            out['suggestions'] = [
                {'account_id': row.get('account_id'),
                 'name': row.get('name') or '',
                 'rule': row.get('rule') or '',
                 'owner_name': row.get('owner_name') or ''}
                for row in rows]
    except Exception:                                     # noqa: BLE001
        pass
    return out
