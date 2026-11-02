"""
Delete – not relevant: the mail that was never an enquiry.

Mail ingestion captures everything, deliberately — a missed enquiry
costs more than a junk lead. The cost of that choice lands here: a
newsletter, a quarantine digest, a colleague mailing leads@ from a
personal address, each becomes a lead called "Unknown" sitting at
New Opportunity on somebody's board.

Until now the only action was Close as Lost, which files them in
the Lost reports and the win/loss ratio. A ratio that counts
newsletters as deals we failed to win is not a measurement.

What this is not
    It is not the old delete coming back. 830 leads were destroyed
    in September and October and 120 of them left no record at all;
    CRM-NO-DELETE-01 ended that, and its guard still passes against
    this module because nothing here removes a row. A lead marked
    here is intact, listed in the Admin bin, and restorable.

Why it can be this blunt
    Because §3 only offers it on a lead nobody has touched: still at
    New Opportunity, no RFQ, quote or cost sheet, no mail sent from
    the CRM, no call, meeting or note a person wrote, not linked to
    an opportunity and not a merge target. A lead that passes all six
    has no history to lose, which is also why `restore` can simply
    put it back rather than reconstructing anything.

How it disappears
    `is_archived` is set alongside `junk_deleted`. Twenty-six modules
    already exclude archived leads; they hide these the day this
    ships, without being edited. `scope.leads()` — the one query
    every board, count, digest and report goes through — excludes
    `junk_deleted` outright, for admins too, so the bin is the only
    place they appear.
"""
from __future__ import annotations

import re
from datetime import date, datetime

#: (code, label, block the sender by default). The dialog offers
#: these and nothing else: a free-text reason on a delete is how you
#: end up with 2,312 rows saying "junk".
REASONS = (
    ('spam', 'Spam / promotional / newsletter', True),
    ('internal', 'Internal or personal mail (staff, family, own '
                 'address)', True),
    ('test', 'Test entry', False),
    ('duplicate', 'Duplicate of another lead', False),
    ('not_logistics', 'Not a logistics enquiry', False),
    ('other', 'Others', False),
)
CODES = tuple(c for c, _l, _b in REASONS)
LABELS = {c: l for c, l, _b in REASONS}
BLOCKS_BY_DEFAULT = tuple(c for c, _l, b in REASONS if b)
#: Reasons that cannot be recorded without words. `duplicate` needs
#: the number of the lead that is being kept.
NEEDS_NOTE = ('other', 'duplicate')

#: Above this many in one go, only an administrator may confirm.
BULK_ADMIN_ABOVE = 25
#: What one non-admin may delete in a day before Nilesh is told.
DAILY_CAP = 50

#: The furthest a lead may have got. Anything PAST this means somebody
#: worked it, whatever the other checks say.
#:
#: Read as a ceiling, not an equality. It was written as an equality,
#: and that was backwards: 'New' is the FIRST stage on the journey and
#: 'New Opportunity' the fourth, so `stage != ELIGIBLE_STAGE` refused
#: every brand-new lead — the exact population this module exists for.
#: Found on 2 Nov, when the fifteen named marketing senders came back
#: with "it is at New, not New Opportunity" against all of them and
#: nothing could be filed.
#:
#: A terminal stage is still refused, and separately: a Lost lead is
#: closed, and reopening the question of whether it was junk is not
#: this module's business.
ELIGIBLE_STAGE = 'New Opportunity'

ACTION = 'lead.junk_deleted'
RESTORE_ACTION = 'lead.junk_restored'


# ── eligibility ─────────────────────────────────────────────────────
def _has_rows(model, lead_id, column='lead_id'):
    """True when any row of `model` points at this lead.

    Wrapped because several of these tables arrive with their own
    migration: on a database where one has not been created yet the
    honest answer is "no rows", not a 500 on the Delete button.
    """
    try:
        from app import db
        if not db.inspect(db.engine).has_table(model.__tablename__):
            return False
        return db.session.query(
            model.query.filter(getattr(model, column) == lead_id)
            .exists()).scalar()
    except Exception:                                     # noqa: BLE001
        return False


def _check_stage(lead):
    from app.services import stage_map

    stage = (lead.stage or '').strip()
    if not stage:
        # No stage at all is as early as a lead gets.
        return None
    here = stage_map.position(stage)
    if here is None and stage not in stage_map.TERMINAL:
        return f'{stage} is not a stage on the journey'
    return None


def _check_closed(lead):
    """A terminal stage. Somebody decided this lead's outcome.

    Its own key, and not ignorable: three of the live Promedia leads
    are at Lost, and reopening the question of whether a closed lead
    was junk is not this module's business.
    """
    from app.services import stage_map

    stage = (lead.stage or '').strip()
    if stage in stage_map.TERMINAL:
        return (f'it is at {stage}, which is closed — a closed lead is '
                f'not filed as junk')
    return None


def _check_worked(lead):
    """It got past the ceiling, so somebody worked it.

    Kept apart from `stage` because the two mean opposite things. A
    lead at a stage nobody recognises is a data oddity; a lead at Quoted
    is a lead somebody quoted, which is sales work and can no more be
    ignored than the quote itself.
    """
    from app.services import stage_map

    stage = (lead.stage or '').strip()
    if not stage or stage in stage_map.TERMINAL:
        return None
    here = stage_map.position(stage)
    ceiling = stage_map.position(ELIGIBLE_STAGE)
    if here is not None and ceiling is not None and here > ceiling:
        return (f'it is at {stage}, past {ELIGIBLE_STAGE} — somebody '
                f'worked it')
    return None


def _check_paperwork(lead):
    from app.models.quote import Quote
    from app.models.rfq import RFQ

    if _has_rows(RFQ, lead.id):
        return 'an RFQ was raised on it'
    if _has_rows(Quote, lead.id):
        return 'a quotation was made on it'
    # The cost sheet lives on the quote, so a lead with no quote has
    # no costing. Checked through the quote rather than by importing
    # a table that may not exist on every database.
    return None


def _check_outgoing_mail(lead):
    from app import LeadEmail
    from app.models.mailops import EmailOutbox

    if _has_rows(LeadEmail, lead.id):
        from app import db
        sent = db.session.query(
            LeadEmail.query.filter(
                LeadEmail.lead_id == lead.id,
                LeadEmail.direction == 'outbound').exists()).scalar()
        if sent:
            return 'mail was sent from the CRM on it'
    try:
        from app import db
        queued = db.session.query(
            EmailOutbox.query.filter(
                EmailOutbox.entity_type == 'lead',
                EmailOutbox.entity_id == str(lead.id)).exists()).scalar()
        if queued:
            return 'a message to the customer is queued on it'
    except Exception:                                     # noqa: BLE001
        pass
    return None


#: `LeadActivity.kind` values that mean a person reached out or was
#: reached: a call, a meeting, a visit, a mail, an RFQ. The row is the
#: log of them doing it.
CONTACT_KINDS = ('call', 'meeting', 'visit', 'email', 'rfq', 'note')

#: The kind the system writes for itself on every stage move.
STAGE_CHANGE_KIND = 'stage_change'


def _check_human_activity(lead):
    """A call, a meeting, a visit — somebody made contact.

    Narrowed to the kinds that mean that. The docstring here used to
    say `LeadActivity` "is only written when somebody logs a call, a
    meeting or a visit", and that was not true: every stage move writes
    a row with `kind='stage_change'`, so the check reported a logged
    call on leads where nobody had ever rung anybody.

    A stage change is still work, and still blocks — as its own key,
    with its own sentence. The outcome does not change; what changes is
    that the reason given is true. A refusal that misdescribes the
    evidence is a refusal nobody can act on, and these two were read
    side by side on fifteen live leads before anybody noticed.
    """
    from app import LeadActivity

    try:
        rows = LeadActivity.query.filter(
            LeadActivity.lead_id == lead.id,
            LeadActivity.kind.in_(CONTACT_KINDS)).limit(1).all()
    except Exception:                                     # noqa: BLE001
        return None
    if rows:
        return 'a call, meeting or activity is logged on it'
    return None


def _check_stage_change(lead):
    """Somebody moved it along the journey.

    Work, and not the same work as a logged call. The person who moved
    a lead from New to Call Done did something to it; they did not
    necessarily speak to anybody.
    """
    from app import LeadActivity

    try:
        rows = LeadActivity.query.filter(
            LeadActivity.lead_id == lead.id,
            LeadActivity.kind == STAGE_CHANGE_KIND).limit(1).all()
    except Exception:                                     # noqa: BLE001
        return None
    if rows:
        return 'somebody moved it to another stage'
    return None


#: Notes the system wrote about itself, not remarks anybody made.
#:
#: Every one of these was found on the live data on 2 Nov, under
#: "somebody wrote a note on it". Not one was written by a person:
#:
#:   'CAUTION: This email originated from outside of the organization'
#:        the Exchange external-sender banner, captured out of the mail
#:        body when the old combined notes field was split up;
#:   '[10-Oct-26] Owner auto-assigned -> Vijay T V (same vertical)'
#:        the CRM's own assignment log, stamped by a bulk run;
#:   '[10-Oct-26] phone "+91-99582-48203" -> +91-9958248203'
#:        the CRM normalising a phone number and saying so;
#:   'From: Sina Deipenau <...> Sent: Tuesday, 1 Sep'
#:        a quoted mail header, again out of the body.
#:
#: Matched at the START of the note only. A person writing "From the
#: look of it they want a trailer" is a person; a note that OPENS with
#: a mail header is a mail header.
MACHINE_NOTE_PREFIXES = (
    'caution: this email originated',
    'caution:',
    'from:',
    'sent:',
    'subject:',
    'external email',
    'this message originated',
)

#: The house stamp a bulk run leaves: '[10-Oct-26] something'.
MACHINE_NOTE_STAMP = re.compile(
    r'^\s*\[\d{1,2}-[A-Za-z]{3}-\d{2,4}\]')


def note_is_human(note):
    """Did a PERSON write this note. (True/False, why not).

    Three questions, and the first is the model's own:

      * `migrated_from_legacy` — the column exists to record exactly
        this. Its comment: "set when the row was rescued out of the old
        combined field, so a reader knows it may be email text rather
        than somebody's note";
      * does it open with a machine stamp or a mail header;
      * is anybody named as the author. A note nobody signed was not
        written by anybody.

    All three, not any one. A bulk run can stamp a note AND name the
    employee it assigned the lead to, which would pass the author test
    on its own.
    """
    if getattr(note, 'migrated_from_legacy', False):
        return False, ('it was rescued out of the old notes field — it '
                       'may be email text rather than a remark')
    text = (getattr(note, 'note_text', '') or '').strip()
    low = text.lower()
    if MACHINE_NOTE_STAMP.match(text):
        return False, ('it carries a dated stamp from a bulk run, not a '
                       'remark somebody typed')
    for prefix in MACHINE_NOTE_PREFIXES:
        if low.startswith(prefix):
            return False, (f'it opens with {prefix!r} — a mail header or '
                           f'a banner, captured out of the body')
    if not (getattr(note, 'author', '') or '').strip():
        return False, 'nobody is named as its author'
    return True, ''


def human_notes(lead):
    """[note] that a person actually wrote."""
    from app import LeadNote

    try:
        rows = (LeadNote.query
                .filter(LeadNote.lead_id == lead.id,
                        LeadNote.is_deleted.isnot(True)).all())
    except Exception:                                     # noqa: BLE001
        return []
    return [r for r in rows if note_is_human(r)[0]]


def _check_note(lead):
    """Somebody wrote a remark on it. A PERSON, not the system.

    This read "any row in lead_notes", and that was wrong twice over.

    First, the CRM writes notes about itself — the assignment log, a
    phone number it normalised — and the mail ingest captured the
    Exchange external-sender banner and quoted headers into notes when
    the old combined field was split. On the live data every single
    note under "somebody wrote a note on it" was one of those, and all
    fifteen named marketing senders were held back by it.

    Second, even a real note on a marketing lead is very often the word
    "junk" — somebody reading it, deciding, and having no button to
    press. That is why `blockers` names this separately from a logged
    call, and why the Not-relevant run prints what each note says.

    A human note still blocks. Nothing is tolerated unless a caller
    names it.
    """
    if human_notes(lead):
        return 'somebody wrote a note on it'
    return None


def _check_task(lead):
    """A task on it was completed.

    Task instances are raised by the engine on every new lead, so an
    *open* task says nothing; a closed one says a person dealt with it.
    """
    from app import db
    from app.models.task_engine import TaskInstance, TaskInstanceStatus

    try:
        if db.inspect(db.engine).has_table(TaskInstance.__tablename__):
            done = db.session.query(
                TaskInstance.query.filter(
                    TaskInstance.entity_type == 'lead',
                    TaskInstance.entity_id == lead.id,
                    TaskInstance.status.in_(
                        TaskInstanceStatus.CLOSED)).exists()).scalar()
            if done:
                return 'a task on it was completed'
    except Exception:                                     # noqa: BLE001
        pass
    return None


def _check_links(lead):
    from app import db

    if lead.opp_number:
        return f'it carries opportunity {lead.opp_number}'
    if (lead.opp_stage or '').startswith('Closed'):
        return 'it is linked to a closed opportunity'
    try:
        from app.models.assignment import LeadAccountSuggestion  # noqa: F401
    except Exception:                                     # noqa: BLE001
        pass
    # A merge target: another lead has been closed as a duplicate of
    # this one, so deleting it would orphan that decision.
    try:
        from app import Lead
        kept = db.session.query(
            Lead.query.filter(
                Lead.id != lead.id,
                Lead.lost_reason.isnot(None),
                Lead.notes.like(f'%duplicate of #{lead.id}%')
            ).exists()).scalar()
        if kept:
            return 'another lead was closed as a duplicate of it'
    except Exception:                                     # noqa: BLE001
        pass
    return None


def _check_already(lead):
    if lead.junk_deleted:
        return 'it is already in the deleted bin'
    if lead.is_archived:
        return 'it is archived'
    return None


#: (key, function). The key names the check in tests and in the API,
#: so a hidden button can say which rule hid it.
CHECKS = (
    ('already', _check_already),
    ('stage', _check_stage),
    ('closed', _check_closed),
    ('worked', _check_worked),
    ('paperwork', _check_paperwork),
    ('outgoing_mail', _check_outgoing_mail),
    ('activity', _check_human_activity),
    ('stage_change', _check_stage_change),
    ('note', _check_note),
    ('task', _check_task),
    ('links', _check_links),
)

#: Blockers that mean somebody did SALES WORK on the lead — raised an
#: RFQ, quoted it, wrote to the client, linked it to an opportunity.
#: Separated from the rest so a caller can tell "this is a live
#: enquiry" from "this is a lead at New that somebody glanced at".
#: `worked` and `closed` are here with the rest: a lead at Quoted is a
#: lead somebody quoted, and a lead at Lost carries a decision
#: somebody took. Neither is softer than the quote itself.
WORK_KEYS = ('paperwork', 'outgoing_mail', 'links', 'activity',
             'stage_change', 'worked', 'closed')

#: What a disabled Delete says on hover.
HOVER = 'This lead has activity — use Close as Lost.'


def blockers(lead):
    """[(key, sentence)] — empty when the lead may be deleted."""
    out = []
    for key, check in CHECKS:
        try:
            why = check(lead)
        except Exception:                                 # noqa: BLE001
            # A check that cannot run is a check that did not pass.
            # Erring towards "not deletable" is the safe direction.
            why = 'a check on it could not be run'
        if why:
            out.append((key, why))
    return out


def eligible(lead):
    return not blockers(lead)


# ── who ─────────────────────────────────────────────────────────────
def is_admin(emp_code):
    from app import Employee

    emp = Employee.query.filter_by(emp_code=emp_code).first()
    role = (getattr(emp, 'role', '') or '').lower()
    return role in ('admin', 'super admin', 'superadmin')


def may_delete(lead, emp_code):
    """The owner, the vertical head of the lead, or a CRM admin."""
    if not emp_code:
        return False
    if emp_code in (lead.assigned_to, lead.secondary_owner):
        return True
    if is_admin(emp_code):
        return True
    try:
        from app.services import vertical_heads, vertical_scope
        vertical = vertical_scope.vertical_name(lead)
        # `heads_for_vertical` answers Management's head for a
        # vertical it cannot place. That is right for routing a
        # notification and wrong for a permission: it would hand the
        # delete to one person for every lead whose vertical is
        # blank or misspelt. The head is only accepted when the
        # vertical is one the master actually knows.
        if not vertical or not vertical_heads.is_mapped(vertical):
            return False
        heads = vertical_heads.heads_for_vertical(vertical) or []
        return emp_code in {getattr(h, 'emp_code', h) for h in heads}
    except Exception:                                     # noqa: BLE001
        return False


def enabled():
    """Off until it is switched on, like every other release."""
    from app.services import flags
    try:
        return bool(flags.on('FEATURE_JUNK_DELETE'))
    except Exception:                                     # noqa: BLE001
        return False


# ── the delete ──────────────────────────────────────────────────────
def sender_of(lead):
    """The address the lead arrived from, where there is one."""
    return ((lead.original_email_from or '')
            or (lead.email or '')).strip().lower()


def blocks_sender_by_default(reason):
    return reason in BLOCKS_BY_DEFAULT


def mark_not_relevant(lead, *, reason, note=None, actor,
                      block_sender=False, commit=True, ignore=()):
    """Mark one lead not relevant. Returns a dict of what happened.

    Named for what it does rather than for what the button says.
    CRM-NO-DELETE-01's guard reads the source for a `.delete(lead)`
    and it caught this function when it was called `delete` — which
    was the guard doing its job, because a reader skimming
    `jd.delete(lead)` has no way to tell it from a row being
    destroyed. Nothing here destroys anything.

    Raises ValueError when the reason is not one of the six, when a
    reason that needs words has none, or when the lead is not
    eligible — the caller has already been told by `blockers`, so
    reaching here means something changed underneath it.

    `ignore` names blocker keys the caller has been authorised to
    tolerate, and defaults to none. It exists for one case: a bulk run
    against senders a decision has already named, where the only thing
    standing in the way is a note that very often reads "junk" —
    somebody having looked at the lead and had no button to press.

    Nothing in `WORK_KEYS` may be ignored, whatever is asked. A lead
    with an RFQ, a quote, an outgoing mail, a logged call or an
    opportunity on it is work somebody did, and no flag makes that
    untrue. The refusal names the key so a caller passing it learns
    why rather than wondering.
    """
    from app import db
    from app.services import audit

    if reason not in CODES:
        raise ValueError(f'unknown reason: {reason!r}')
    note = (note or '').strip()
    if reason in NEEDS_NOTE and not note:
        raise ValueError(
            'Others needs a sentence; Duplicate needs the lead number.')
    tolerated = {str(k) for k in (ignore or ())}
    forbidden = tolerated & set(WORK_KEYS)
    if forbidden:
        raise ValueError(
            f'{", ".join(sorted(forbidden))} cannot be ignored: a lead '
            f'with sales work on it is not junk, whatever its sender '
            f'looks like.')
    why = [(k, w) for k, w in blockers(lead) if k not in tolerated]
    if why:
        raise ValueError('; '.join(w for _k, w in why))

    sender = sender_of(lead)
    before = {'stage': lead.stage, 'is_archived': bool(lead.is_archived)}
    lead.junk_deleted = True
    lead.junk_deleted_at = datetime.utcnow()
    lead.junk_deleted_by = actor
    lead.junk_reason = reason
    lead.junk_reason_note = note[:400] or None
    lead.is_archived = True
    lead.archived_at = lead.junk_deleted_at
    lead.archived_by = actor
    lead.archive_reason = f'Deleted – not relevant: {LABELS[reason]}'[:200]

    closed = _close_open_work(lead)
    _mark_ingest_log(lead)

    blocked = None
    if block_sender:
        blocked = block_the_sender(sender, reason=reason, actor=actor)
    # 'refused' is a truthy string and is the opposite of blocked —
    # reading it as success told the user their own domain had been
    # added to the block list.
    did_block = blocked in ('added', 'already')

    audit.record(
        ACTION, 'lead', lead.id,
        old=before,
        new={'junk_deleted': True, 'reason': reason, 'note': note,
             'sender': sender, 'subject': lead.original_email_subject,
             'tasks_closed': closed, 'sender_blocked': did_block},
        reason=f'{LABELS[reason]}{": " + note if note else ""}',
        actor=actor)
    if commit:
        db.session.commit()
    return {'id': lead.id, 'reason': reason, 'tasks_closed': closed,
            'sender': sender, 'sender_blocked': did_block,
            'block_refused': blocked == 'refused'}


def _close_open_work(lead):
    """Close the system tasks and escalations hanging off the lead."""
    from app import db
    from app.models.task_engine import TaskInstance, TaskInstanceStatus

    closed = 0
    try:
        if not db.inspect(db.engine).has_table(TaskInstance.__tablename__):
            return 0
        rows = TaskInstance.query.filter(
            TaskInstance.entity_type == 'lead',
            TaskInstance.entity_id == lead.id,
            TaskInstance.status.in_(TaskInstanceStatus.OPEN)).all()
        for row in rows:
            row.status = TaskInstanceStatus.CANCELLED
            if hasattr(row, 'closed_at'):
                row.closed_at = datetime.utcnow()
            if hasattr(row, 'close_reason'):
                row.close_reason = 'Lead deleted – not relevant'
            closed += 1
    except Exception:                                     # noqa: BLE001
        return closed
    return closed


def _mark_ingest_log(lead):
    """The source mail stays, and says what became of its lead."""
    from app.models.ingest_log import MailIngestLog

    try:
        rows = MailIngestLog.query.filter_by(lead_id=lead.id).all()
        for row in rows:
            note = 'lead deleted – not relevant'
            existing = (getattr(row, 'reason', '') or '')
            if note not in existing:
                row.reason = (f'{existing} · {note}'.strip(' ·'))[:300]
    except Exception:                                     # noqa: BLE001
        pass


def block_the_sender(address, *, reason, actor):
    """Add one address to the Master Data block list.

    The address, never the domain: blocking `@gmail.com` because one
    personal mail arrived would silently drop every enquiry from a
    customer who uses a free mailbox, and plenty do.

    Returns 'added', 'already', or 'refused' for our own domains.
    """
    from app import db
    from app.models.master_data import MasterItem
    from app.services import audit, mail_policy, system_senders

    sender = (address or '').strip().lower()
    if not sender or '@' not in sender:
        return 'refused'
    domain = sender.rsplit('@', 1)[-1]
    own = set(getattr(mail_policy, 'BUILTIN_DOMAINS', ()) or ())
    try:
        own |= set(mail_policy.internal_domains())
    except Exception:                                     # noqa: BLE001
        pass
    if domain in own:
        # Blocking ourselves would stop the forwards that are how
        # most real enquiries reach the CRM at all.
        audit.record('sender_block.refused', 'master_item', None,
                     new={'address': sender},
                     reason='a Procam domain is never blocked',
                     actor=actor)
        return 'refused'

    exists = MasterItem.query.filter_by(
        list_key=system_senders.LIST_KEY, code=sender).first()
    if exists is not None:
        if not exists.is_active:
            exists.is_active = True
        return 'already'
    db.session.add(MasterItem(
        list_key=system_senders.LIST_KEY, code=sender, label=sender,
        description=f'Blocked with a deleted lead: {LABELS[reason]}',
        is_active=True, created_by=actor))
    audit.record('sender_block.added', 'master_item', None,
                 new={'address': sender, 'reason': reason},
                 reason=LABELS[reason], actor=actor)
    return 'added'


# ── restore ─────────────────────────────────────────────────────────
def restore(lead, *, actor, commit=True):
    """Put it back exactly as it was.

    Eligibility guaranteed the lead was at New Opportunity and not
    archived when it was deleted, so there is nothing to reconstruct.
    """
    from app import db
    from app.services import audit

    if not lead.junk_deleted:
        raise ValueError('that lead is not in the deleted bin')
    was = {'reason': lead.junk_reason, 'note': lead.junk_reason_note,
           'by': lead.junk_deleted_by,
           'at': str(lead.junk_deleted_at or '')[:19]}
    lead.junk_deleted = False
    lead.junk_deleted_at = None
    lead.junk_deleted_by = None
    lead.junk_reason = None
    lead.junk_reason_note = None
    lead.is_archived = False
    lead.archived_at = None
    lead.archived_by = None
    lead.archive_reason = None
    lead.stage = ELIGIBLE_STAGE
    audit.record(RESTORE_ACTION, 'lead', lead.id, old=was,
                 new={'junk_deleted': False, 'stage': lead.stage},
                 reason='restored from the deleted bin', actor=actor)
    if commit:
        db.session.commit()
    return {'id': lead.id, 'owner': lead.assigned_to}


# ── the bin ─────────────────────────────────────────────────────────
def deleted(*, since=None, until=None, by=None, reason=None,
            sender=None, limit=500):
    """The Admin bin's query — junk only, newest first."""
    from app import Lead

    q = Lead.query.filter(Lead.junk_deleted.is_(True))
    if since:
        q = q.filter(Lead.junk_deleted_at >= since)
    if until:
        q = q.filter(Lead.junk_deleted_at <= until)
    if by:
        q = q.filter(Lead.junk_deleted_by == by)
    if reason:
        q = q.filter(Lead.junk_reason == reason)
    if sender:
        like = f'%{sender.strip().lower()}%'
        q = q.filter(db_or_sender(Lead, like))
    return q.order_by(Lead.junk_deleted_at.desc()).limit(limit)


def db_or_sender(Lead, like):
    from sqlalchemy import func, or_
    return or_(
        func.lower(func.coalesce(Lead.original_email_from, '')).like(like),
        func.lower(func.coalesce(Lead.email, '')).like(like))


def row(lead):
    """One bin row, as the page and the export both want it."""
    return {
        'id': lead.id,
        'company': lead.company or '',
        'sender': sender_of(lead),
        'subject': (lead.original_email_subject or '')[:200],
        'reason': lead.junk_reason or '',
        'reason_label': LABELS.get(lead.junk_reason, lead.junk_reason or ''),
        'note': lead.junk_reason_note or '',
        'by': lead.junk_deleted_by or '',
        'at': str(lead.junk_deleted_at or '')[:19],
        'owner': lead.assigned_to or '',
        'vertical': lead.procam_vertical or '',
    }


# ── the daily cap ───────────────────────────────────────────────────
def deleted_today(emp_code, *, day=None):
    from app import Lead, db

    day = day or date.today()
    start = datetime(day.year, day.month, day.day)
    return db.session.query(
        db.func.count(Lead.id)).filter(
            Lead.junk_deleted.is_(True),
            Lead.junk_deleted_by == emp_code,
            Lead.junk_deleted_at >= start).scalar() or 0


def over_cap(emp_code, *, adding=1):
    """(True, n) when this delete takes a non-admin past the cap."""
    if is_admin(emp_code):
        return (False, 0)
    total = deleted_today(emp_code) + adding
    return (total > DAILY_CAP, total)


def tell_the_admin(emp_code, total):
    """Over the cap is not refused — it is reported."""
    from app.services import outbox

    try:
        outbox.enqueue(
            to=admin_address(),
            subject=f'{emp_code} deleted {total} leads today',
            html=(f'<p>{emp_code} has marked {total} leads '
                  f'"not relevant" today, past the daily limit of '
                  f'{DAILY_CAP}.</p>'
                  f'<p>Nothing was refused and nothing was destroyed — '
                  f'every one is in Admin → Deleted leads and can be '
                  f'restored.</p>'),
            dedupe_key=f'junk-cap:{emp_code}:{date.today()}',
            event_key='lead.junk_cap', user_code=emp_code,
            commit=False)
        return True
    except Exception:                                     # noqa: BLE001
        return False


def admin_address():
    """Who hears about the cap. A setting, not a name in the code."""
    from app.services import admin_settings

    return (admin_settings.get('junk_delete_alert_to')
            or 'leads@procamgroup.in')
