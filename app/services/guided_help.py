"""
"What happened, why, and what to do" — never only "no".

CRM-LEAD-FLOW-01 §5. The finding this answers: when the CRM refuses
something, the user is told "no" and nothing else, so they ring the
Tech team. A refusal with no reason and no next step is a support
cost every time it fires, and it teaches people that the screen is
something to get around rather than to read.

Every refusal in the lead, account, quote and RFQ flows is routed
through `guide()`, which returns three things:

    what happened  the title, one plain sentence
    why            the rule, in words a salesperson uses
    what to do     numbered steps, each with a button that takes
                   them there

The wording lives in `crm_help_guides`, not here, so an
administrator can fix a sentence from Admin → Help texts in a
minute. `SEED` below is what the table starts with and what the
catalogue falls back to when a code has no row — a missing row must
degrade to the built-in words, never to a blank box.

Nothing in here decides anything. It turns a decision somebody else
made into something a person can act on.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

FLAG = 'FEATURE_GUIDED_HELP'

#: What an action button does on the page.
#:   tab    open that tab on the lead
#:   focus  open the tab and put the cursor in that field
#:   open   go to that URL
#:   mail   draft a mail to the person named in the context
#:   help   raise a support ticket, pre-filled
ACTIONS = ('tab', 'focus', 'open', 'mail', 'help')


def _row(code, title, why, steps, actions, audience=''):
    return {'code': code, 'title': title, 'why': why, 'steps': steps,
            'actions': actions, 'audience': audience, 'active': True}


#: The catalogue at go-live (§5.1). Every one of these replaced a
#: message that said only that something was not allowed.
SEED = [
    _row('CLIENT_BLOCKED',
         'This client is blocked, so no lead can be created.',
         'Somebody in management stopped new business with this '
         'client — usually payment history. The block is on the '
         'account, not on you, and it applies to mail as well as to '
         'leads keyed in by hand.',
         ['Read the block reason shown above.',
          'If the client has settled, ask for the block to be '
          'reviewed — the request goes to the vertical head and to '
          'Nilesh.',
          'If this is a different company with a similar name, say '
          'so in the request and name the right account.'],
         [{'label': 'Request a review', 'action': 'mail',
           'target': 'block_review'},
          {'label': 'Open the account', 'action': 'open',
           'target': 'account'}]),

    _row('NO_ACCOUNT',
         'This lead needs a customer before it can be saved.',
         'Every lead belongs to an account. The account is what '
         'carries the owner, the history and the credit position, '
         'so a lead without one is invisible in every report about '
         'that customer.',
         ['Type the company name in the account box — three letters '
          'is enough.',
          'Pick the account from the list.',
          'If it is genuinely a new customer, create the account '
          'from the same box.'],
         [{'label': 'Link an account', 'action': 'focus',
           'target': 'account'},
          {'label': 'Create the account', 'action': 'focus',
           'target': 'account_new'}]),

    _row('NO_OWNER',
         'Nobody holds this lead.',
         'A lead with no owner is not being worked and nobody has '
         'been told to work it. It sits in no one’s list.',
         ['Give it to the account owner, which is almost always '
          'right.',
          'If it belongs to somebody else, pick them instead and '
          'say why.'],
         [{'label': 'Assign to the account owner', 'action': 'focus',
           'target': 'owner'}]),

    _row('NOT_YOUR_LEAD',
         'This lead belongs to somebody else.',
         'Only the owner and the secondary PIC can change a lead. '
         'That is what keeps two people from working the same deal '
         'in different directions.',
         ['Read it — you can see everything on it.',
          'If you need to work on it, ask the owner to add you as '
          'the secondary PIC.'],
         [{'label': 'Ask the owner', 'action': 'mail',
           'target': 'owner'}]),

    _row('STAGE_FIELDS_MISSING',
         'This lead is not ready for the next stage yet.',
         'Each stage asks for the few things the next person needs. '
         'Moving without them leaves somebody guessing later, '
         'usually when the customer is on the phone.',
         ['Fill in the fields listed above — each one has a '
          'button that takes you to it.',
          'Save, then move the stage again.'],
         [{'label': 'Take me there', 'action': 'focus',
           'target': 'first_missing'}]),

    _row('HOLD_NOT_ALLOWED',
         'Only a vertical head or a director can put a lead on hold.',
         'On Hold takes a lead out of the pipeline figures. It is a '
         'management decision about the deal, not a way to clear '
         'your own list.',
         ['Ask your vertical head to put it on hold, saying why.',
          'If the client has gone quiet rather than paused, close it '
          'as Lost — "No response" — which keeps the figures '
          'honest.'],
         [{'label': 'Request a hold', 'action': 'mail',
           'target': 'vertical_head'}]),

    _row('POSSIBLE_DUPLICATE',
         'There may already be a lead for this.',
         'Replies on one email thread used to become a new lead '
         'each time, so one enquiry turned into four. The CRM found '
         'an open lead that looks like the same job.',
         ['Open the existing lead and see whether it is the same '
          'job.',
          'If it is, merge this one into it — nothing is lost, '
          'everything moves across.',
          'If it is genuinely different, keep both and say why.'],
         [{'label': 'Open the other lead', 'action': 'open',
           'target': 'duplicate'},
          {'label': 'Merge them', 'action': 'open',
           'target': 'merge'}]),

    _row('DELETE_NOT_ALLOWED',
         'A lead is never deleted.',
         'Somebody has already worked this one — there are mails, '
         'notes or tasks on it. Deleting it would remove the record '
         'of what we told the customer.',
         ['If the deal is dead, close it as Lost and pick a reason.',
          'If it was never a real enquiry, use "Delete – not '
          'relevant", which hides it without destroying it.'],
         [{'label': 'Close as Lost', 'action': 'focus',
           'target': 'lost'}]),

    _row('MAIL_NOT_SENT',
         'That mail has not gone out yet.',
         'Outgoing mail to people outside Procam is held until it is '
         'released. Nothing is lost — it is queued, not refused.',
         ['Check the message under Mail → Outbox.',
          'If it should go now, retry it; if it is held by the '
          'external-mail switch, ask an administrator to release it.'],
         [{'label': 'Open the outbox', 'action': 'open',
           'target': 'outbox'}]),

    _row('RATE_SOURCING_EMPTY',
         'No supplier has been asked for a rate yet.',
         'Rate sourcing is where supplier requests and their replies '
         'live. The customer’s own mail is not a rate, which is '
         'why it does not appear here.',
         ['Start rate sourcing and pick the suppliers to ask.',
          'Their replies land here as they come in.'],
         [{'label': 'Start rate sourcing', 'action': 'tab',
           'target': 'rates'}]),

    _row('QUOTE_EXPIRED',
         'This quote has passed its validity date.',
         'A quote carries a date after which the price is no longer '
         'ours to honour — rates and the rupee move.',
         ['If the price still stands, extend the validity and say '
          'until when.',
          'If it does not, revise the quote; the revision keeps the '
          'same quote number.'],
         [{'label': 'Revise the quote', 'action': 'tab',
           'target': 'quote'}]),

    _row('HOW_THIS_WORKS',
         'How this screen works.',
         'Leads mostly make themselves: mail to leads@ is matched to '
         'the account, given an owner, and replies join the lead '
         'they belong to rather than starting a new one.',
         ['Read the "What to do now" card and press its button.',
          'Move along the stage strip: New, Contacted, RFQ, Quoted, '
          'Negotiation, Won. If something is missing the screen says '
          'what and takes you to it.',
          'Customer mail is under Emails, supplier rates under Rate '
          'sourcing, the quote under Quote from RFQ onwards.',
          'Wrong account, wrong owner, duplicate, not relevant or '
          'lost? They are all under More.'],
         [{'label': 'Show me the card', 'action': 'focus',
           'target': 'lfTodo'}]),

    _row('SESSION_EXPIRED',
         'You have been signed out.',
         'A session ends after a long gap. Nothing you saved is '
         'lost.',
         ['Sign in again.',
          'Open the lead from your list — it is where you left '
          'it.'],
         [{'label': 'Sign in', 'action': 'open', 'target': 'login'}]),

    _row('NO_PERMISSION',
         'Your role does not include this.',
         'Permissions are set per role so that figures and customer '
         'records are changed only by the people answerable for '
         'them.',
         ['If you need this to do your job, ask for the role — '
          'an administrator grants it.',
          'Say which screen and what you were trying to do.'],
         [{'label': 'Request access', 'action': 'help',
           'target': 'access'}]),
]

SEED_BY_CODE = {row['code']: row for row in SEED}
CODES = tuple(row['code'] for row in SEED)


def enabled():
    try:
        from app.services import flags
        return bool(flags.on(FLAG))
    except Exception:                                     # noqa: BLE001
        return False


def catalogue(*, include_inactive=False):
    """Every guide, database first, seed for anything missing."""
    from app.models.help_content import HelpGuide

    rows = {}
    try:
        for row in HelpGuide.query.all():
            rows[row.code] = row.to_dict()
    except Exception:                                     # noqa: BLE001
        from app import db
        db.session.rollback()
    out = []
    for code in CODES:
        out.append(rows.pop(code, None) or dict(SEED_BY_CODE[code]))
    # Codes an administrator added that the code does not know about
    # are still shown; somebody wrote them for a reason.
    out.extend(rows.values())
    if not include_inactive:
        out = [row for row in out if row.get('active', True)]
    return out


def guide(code, context=None):
    """One guide, ready for the screen. Never None.

    An unknown code still answers — with the plain "ask for help"
    guide rather than a blank box, because a refusal that shows
    nothing is the thing this module exists to end.
    """
    from app.models.help_content import HelpGuide

    code = (code or '').strip().upper()
    found = None
    try:
        row = HelpGuide.query.filter_by(code=code).first()
        if row is not None and row.active:
            found = row.to_dict()
    except Exception:                                     # noqa: BLE001
        from app import db
        db.session.rollback()
    if found is None:
        found = dict(SEED_BY_CODE.get(code) or {})
    if not found:
        found = {
            'code': code or 'UNKNOWN',
            'title': 'That did not work.',
            'why': 'The CRM refused this and has no wording written '
                   'for it yet, which is itself something to fix.',
            'steps': ['Tell the Tech team what you were doing — the '
                      'ticket carries the screen and the lead.'],
            'actions': [{'label': 'Ask for help', 'action': 'help',
                         'target': 'generic'}],
            'audience': '', 'active': True,
        }
    found = dict(found)
    found['context'] = dict(context or {})
    return found


def refusal(code, context=None, *, http=400):
    """(json-able body, status) for a route that must refuse.

    Keeps `error` as it always was — every caller and test reads
    that key — and adds the guide beside it.
    """
    found = guide(code, context)
    body = {'ok': False, 'error': found['title'], 'guide': found}
    return body, http


def record(code, *, emp_code=None, lead_id=None, page='',
           clicked=None, commit=False):
    """Log that a guide was shown, or that its button was used.

    Best-effort: a logging failure must never swallow the guide.
    """
    from app.models.help_content import HelpGuideShown

    try:
        from app import db
        db.session.add(HelpGuideShown(
            code=(code or '')[:40], emp_code=(emp_code or '')[:20] or None,
            lead_id=lead_id, page=(page or '')[:80] or None,
            clicked=(clicked or '')[:60] or None))
        if commit:
            db.session.commit()
        return True
    except Exception:                                     # noqa: BLE001
        try:
            from app import db
            db.session.rollback()
        except Exception:                                 # noqa: BLE001
            pass
        log.debug('could not log guide %s', code, exc_info=True)
        return False


def weekly(limit=10, days=7):
    """§5.3 — the ten most-shown guides, and how many were acted on.

    "Resolved by the user" is read as "they pressed one of the
    guide's own buttons". It is a proxy, and the honest reading is
    that a guide nobody clicks is either unnecessary or badly
    worded — both worth knowing.
    """
    from datetime import datetime, timedelta

    from app import db
    from app.models.help_content import HelpGuideShown

    since = datetime.utcnow() - timedelta(days=days)
    rows = (db.session.query(HelpGuideShown.code,
                             db.func.count(HelpGuideShown.id),
                             db.func.sum(
                                 db.case((HelpGuideShown.clicked.isnot(None),
                                          1), else_=0)))
            .filter(HelpGuideShown.shown_at >= since)
            .group_by(HelpGuideShown.code)
            .order_by(db.func.count(HelpGuideShown.id).desc())
            .limit(limit).all())
    out = []
    for code, shown, acted in rows:
        shown = shown or 0
        acted = acted or 0
        out.append({'code': code, 'shown': shown, 'acted': acted,
                    'acted_pct': round(100.0 * acted / shown, 1)
                    if shown else 0.0})
    return out


def raise_ticket(*, emp_code, page='', lead_id=None, code='',
                 description=''):
    """"Ask for help", answered with a number. (ticket, error).

    §5.2. A person who is stuck gets a reference instead of the
    Tech team's phone number, and the Tech team gets the screen,
    the lead and the guide that was on display without having to
    ask for any of them.
    """
    from datetime import datetime

    from app import db
    from app.models.help_content import HelpTicket

    description = (description or '').strip()
    if not description:
        return None, ('Say in a line what you were trying to do. '
                      'Without it the ticket cannot be answered.')
    row = HelpTicket(emp_code=(emp_code or '')[:20] or None,
                     page=(page or '')[:80] or None,
                     lead_id=lead_id, code=(code or '')[:40] or None,
                     description=description[:4000], status='Open')
    db.session.add(row)
    db.session.flush()
    row.number = f'CRM-{datetime.utcnow():%Y%m}-{row.id:05d}'
    db.session.commit()
    return row, ''


def seed(actor='system'):
    """Put the catalogue in the database. Returns how many were added.

    Idempotent, and it never overwrites: a sentence an administrator
    improved is not reverted by running this again.
    """
    from app import db
    from app.models.help_content import HelpGuide

    added = 0
    for row in SEED:
        if HelpGuide.query.filter_by(code=row['code']).first():
            continue
        db.session.add(HelpGuide(
            code=row['code'], title=row['title'], why=row['why'],
            steps=list(row['steps']), actions=list(row['actions']),
            audience=row['audience'], active=True,
            last_edited_by=actor))
        added += 1
    db.session.commit()
    return added
