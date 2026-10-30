"""
Two leads that are one enquiry, made one.

CRM-LEAD-FLOW-01 §2.3. The complaint behind it: every reply on an
email thread became a new lead. Indo Tech Transformers made four
leads on 19 October and three on the 22nd; Hitachi Energy made two
on the 29th under two spellings of the account.

Intake already matches a thread — `lead_intake_db.find_by_thread`
has done that since CRM-INTAKE-01 — so new mail mostly attaches
correctly. What was missing is what to do about the ones already
split, and about the uncertain match: the brief asks for a
"possible duplicate of #nnnn" banner with one-click merge, and for
"Mark duplicate" on the list.

What a merge does
    Emails, attachments, notes, tasks, quotes, RFQs and history move
    to the surviving lead. The merged lead is closed as
    "Duplicate of #nnnn" and kept, never deleted (CRM-NO-DELETE-01).
    Every move is audited and the row says where its work went.

What it will not do
    Merge a Won lead, or merge two leads that are both Won: a won
    deal is a reported figure and folding one into another rewrites
    history somebody has already acted on. Merge into itself. Merge
    a lead that has already been merged.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

FLAG = 'FEATURE_THREAD_DEDUPE'

#: §2.3 — how far back a duplicate is looked for.
WINDOW_DAYS = 30

#: Stages that mean the enquiry is finished. A lead in one of these
#: is neither a duplicate candidate nor something to merge away.
CLOSED = ('Won', 'Lost', 'Not Interested')

#: A client's own reference, as it appears in a subject or body:
#: TTL-HL-IN-STD-2026-017, GEM/2026/B/1234567. It has to start with
#: letters, so a bare date is not one, and carry at least two
#: separators and two digits, which is what keeps "RE:", "FW:" and
#: ordinary words out. A single-character group is allowed because
#: GeM uses one.
REFERENCE = re.compile(
    r'\b([A-Z]{2,}[A-Z0-9]*(?:[-/][A-Z0-9]+){2,})\b')

SUBJECT_NOISE = re.compile(
    r'^\s*(?:re|fw|fwd|rpl|aw|tr)\s*[:\-]\s*', re.I)


def enabled():
    try:
        from app.services import flags
        return bool(flags.on(FLAG))
    except Exception:                                     # noqa: BLE001
        return False


# ── reading a message ────────────────────────────────────────────────
def strip_subject(subject):
    """The subject without the reply and forward prefixes.

    A client's own RFQ reference is left in: it is the strongest
    thing in the line and dropping it would make two different
    enquiries from one customer look alike.
    """
    text = (subject or '').strip()
    for _ in range(6):
        trimmed = SUBJECT_NOISE.sub('', text)
        if trimmed == text:
            break
        text = trimmed
    return ' '.join(text.split()).lower()


def references(*texts):
    """Client reference numbers found in a subject or body."""
    found = []
    for text in texts:
        for hit in REFERENCE.findall((text or '').upper()):
            # A date or a plain phone number is not a reference.
            if sum(c.isdigit() for c in hit) < 2:
                continue
            if hit not in found:
                found.append(hit)
    return found


def thread_key(*, conversation_id=None, subject='', domain=''):
    """One string naming the thread a lead belongs to.

    Stored on the lead so the next reply can find it with an index
    rather than by scanning two thousand rows, which is what the
    subject fallback does today.
    """
    conversation_id = (conversation_id or '').strip()
    if conversation_id:
        return 'c:' + conversation_id[:180]
    subject = strip_subject(subject)
    domain = (domain or '').strip().lower()
    if subject and domain:
        return f's:{domain}:{subject}'[:200]
    return ''


# ── finding the duplicate ────────────────────────────────────────────
def _open_leads(account_id=None, since=None):
    from app import Lead

    since = since or (datetime.utcnow() - timedelta(days=WINDOW_DAYS))
    q = (Lead.query
         .filter(Lead.is_archived.isnot(True),
                 Lead.junk_deleted.isnot(True),
                 Lead.merged_into_id.is_(None),
                 Lead.created_at >= since)
         .filter(~Lead.stage.in_(CLOSED)))
    if account_id:
        q = q.filter(Lead.company_id == account_id)
    return q.order_by(Lead.id.asc()).all()


def candidates(lead, *, limit=5):
    """Open leads that look like the same enquiry as this one.

    The three rules of §2.3, each carrying how sure it is. A thread
    match is certain; a reference match is certain; a subject match
    is only a suggestion, which is why it draws a banner rather than
    merging anything.
    """
    from app import Lead

    if lead is None:
        return []
    mine_thread = (getattr(lead, 'thread_key', '') or '').strip()
    mine_subject = strip_subject(
        getattr(lead, 'original_email_subject', '') or '')
    mine_refs = set(references(
        getattr(lead, 'original_email_subject', '') or '',
        (getattr(lead, 'original_email_body', '') or '')[:4000],
        getattr(lead, 'project', '') or ''))
    mine_domain = _domain(lead)

    out = []
    for other in _open_leads(getattr(lead, 'company_id', None) or None):
        if other.id == lead.id:
            continue
        why, sure = None, False
        if mine_thread and (other.thread_key or '') == mine_thread:
            why, sure = 'same email thread', True
        if why is None and mine_refs:
            theirs = set(references(
                other.original_email_subject or '',
                (other.original_email_body or '')[:4000],
                other.project or ''))
            shared = mine_refs & theirs
            if shared and (not lead.company_id
                           or other.company_id == lead.company_id):
                why = f'same reference {sorted(shared)[0]}'
                sure = True
        if why is None and mine_subject and len(mine_subject) >= 8:
            if (strip_subject(other.original_email_subject or '')
                    == mine_subject
                    and _domain(other) == mine_domain and mine_domain):
                why, sure = 'same subject and sender', False
        if why is None:
            continue
        out.append({'lead_id': other.id, 'lead': other,
                    'company': other.company or '',
                    'stage': other.stage or '',
                    'owner': other.assigned_to or '',
                    'created': str(other.created_at)[:10],
                    'why': why, 'certain': sure})
        if len(out) >= limit:
            break
    out.sort(key=lambda r: (not r['certain'], r['lead_id']))
    return out


def _domain(lead):
    address = ((getattr(lead, 'email', '') or '')
               or (getattr(lead, 'original_email_from', '') or ''))
    if '@' not in address:
        return ''
    return address.rsplit('@', 1)[-1].strip().lower().strip('>')


def survivor_of(rows):
    """Which of a group should be kept: the earliest with the most
    work on it. §6.1 words it that way, and it is the right way
    round — the oldest lead is the one the customer's own thread
    points at."""
    def rank(lead):
        return (-_activity(lead), lead.id)
    return sorted(rows, key=rank)[0] if rows else None


def _activity(lead):
    from app import LeadEmail, db
    total = 0
    try:
        total += (db.session.query(LeadEmail.id)
                  .filter(LeadEmail.lead_id == lead.id).count())
    except Exception:                                     # noqa: BLE001
        db.session.rollback()
    for attr in ('quote_no', 'crn'):
        if (getattr(lead, attr, '') or ''):
            total += 2
    return total


# ── the merge ────────────────────────────────────────────────────────
#: Tables whose rows belong to a lead and must follow it.
def _lead_fks():
    from app import db
    out = []
    for table in db.metadata.sorted_tables:
        for column in table.columns:
            for fk in column.foreign_keys:
                if fk.column.table.name == 'leads':
                    out.append((table, column))
    return out


def blockers(lead, into):
    """Why these two cannot be merged, in words, or ''."""
    if lead is None or into is None:
        return 'One of those leads does not exist.'
    if lead.id == into.id:
        return 'That is the same lead.'
    if getattr(lead, 'merged_into_id', None):
        return (f'Lead #{lead.id} was already merged into '
                f'#{lead.merged_into_id}.')
    if getattr(into, 'merged_into_id', None):
        return (f'Lead #{into.id} was itself merged into '
                f'#{into.merged_into_id} — merge into that one.')
    if (lead.stage or '') == 'Won':
        return ('A won lead is a reported figure. Merging it away '
                'would rewrite a number somebody has acted on.')
    return ''


def merge(lead, into, *, actor, reason=''):
    """Fold `lead` into `into`. Returns (ok, detail)."""
    from app import db
    from app.services import audit

    why = blockers(lead, into)
    if why:
        return False, why

    moved = {}
    for table, column in _lead_fks():
        try:
            from sqlalchemy import text
            count = db.session.execute(
                text(f'UPDATE {table.name} SET {column.name} = :into '
                     f'WHERE {column.name} = :lead'),
                {'into': into.id, 'lead': lead.id}).rowcount
        except Exception:                                 # noqa: BLE001
            db.session.rollback()
            log.exception('could not move %s.%s', table.name,
                          column.name)
            return False, (f'Could not move the {table.name} rows. '
                           f'Nothing was changed.')
        if count:
            moved[f'{table.name}.{column.name}'] = count

    note = reason or f'Duplicate of #{into.id}'
    lead.merged_into_id = into.id
    lead.stage = 'Lost'
    lead.lost_reason = 'Duplicate'
    if hasattr(lead, 'junk_reason_note'):
        lead.junk_reason_note = note[:400]
    lead.is_archived = True

    audit.record('lead.merged', 'lead', lead.id,
                 old={'stage': lead.stage, 'merged_into_id': None},
                 new={'merged_into_id': into.id, 'moved': moved},
                 reason=note, actor=actor)
    audit.record('lead.absorbed', 'lead', into.id,
                 new={'absorbed': lead.id, 'moved': moved},
                 reason=f'Absorbed #{lead.id}', actor=actor)
    db.session.commit()
    return True, {'merged': lead.id, 'into': into.id, 'moved': moved,
                  'reason': note}


def groups(limit=500):
    """§6.1 — every set of open leads that look like one enquiry."""
    from app import Lead

    rows = _open_leads(since=datetime.utcnow() - timedelta(days=365))
    seen, out = set(), []
    for lead in rows[:limit]:
        if lead.id in seen:
            continue
        found = [r for r in candidates(lead, limit=8)
                 if r['lead_id'] not in seen]
        if not found:
            continue
        members = [lead] + [r['lead'] for r in found]
        for one in members:
            seen.add(one.id)
        keeper = survivor_of(members)
        out.append({
            'survivor': keeper.id,
            'company': keeper.company or '',
            'certain': all(r['certain'] for r in found),
            'why': sorted({r['why'] for r in found}),
            'members': [{'lead_id': m.id, 'stage': m.stage or '',
                         'owner': m.assigned_to or '',
                         'created': str(m.created_at)[:10],
                         'keep': m.id == keeper.id} for m in members],
        })
    out.sort(key=lambda g: (-len(g['members']), g['survivor']))
    return out
