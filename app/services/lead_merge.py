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

    Gateway tags ("[EXT]", "EXT:") and relay prefixes ("DSR //") are
    taken off: they say how the mail reached us, not which enquiry it
    is about, and leaving them in gave a reminder its own thread key.
    """
    text = (subject or '').strip()
    for _ in range(6):
        trimmed = _strip_tags(SUBJECT_NOISE.sub('', text))
        if trimmed == text:
            break
        text = trimmed
    return ' '.join(text.split()).lower()


def _strip_tags(text):
    """Gateway and relay tags off the front — "[EXT]", "DSR //".

    `lead_intake.strip_lead_tags` rather than a second list here: the
    classifier and the thread key must agree about what the subject
    is, and CRM-MAIL-2026-11-11 §3+§5 is what happens when they do
    not. A failure leaves the text alone rather than emptying it.
    """
    try:
        from app.services import lead_intake as li
        return li.strip_lead_tags(text)
    except Exception:                                     # noqa: BLE001
        return text


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


def _pk_column(table):
    """The table's single primary-key column, or None.

    None means the rows this merge moves in that table cannot be
    listed by id, only counted — and a merge recorded that way is
    marked `partial` and cannot be reversed. See
    `app.models.lead_merge`.
    """
    keys = list(table.primary_key.columns)
    return keys[0] if len(keys) == 1 else None


def merge(lead, into, *, actor, reason=''):
    """Fold `lead` into `into`. Returns (ok, detail).

    Records every repointed row **by id** in `lead_merges`, so the
    merge can be reversed. The old version recorded counts, which
    made it auditable and still irreversible: after the merge the
    survivor holds thirty activities and nothing says which fourteen
    arrived with the loser.
    """
    from sqlalchemy import text

    from app import db
    from app.models.lead_merge import LeadMerge
    from app.services import audit

    why = blockers(lead, into)
    if why:
        return False, why

    # The loser's own state, read BEFORE anything is written. The
    # earlier version built its audit row after the assignments, so
    # `old={'stage': lead.stage}` recorded the new stage as the old
    # one and the trail could not say what the lead had been.
    before = {
        'stage': lead.stage,
        'lost_reason': getattr(lead, 'lost_reason', None),
        'is_archived': bool(getattr(lead, 'is_archived', False)),
        'junk_reason_note': getattr(lead, 'junk_reason_note', None),
    }

    moved, counted, partial = {}, {}, False
    for table, column in _lead_fks():
        pk = _pk_column(table)
        try:
            if pk is not None:
                rows = db.session.execute(
                    text(f'SELECT {pk.name} FROM {table.name} '
                         f'WHERE {column.name} = :lead'),
                    {'lead': lead.id}).fetchall()
                ids = [r[0] for r in rows]
            else:
                ids = None
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
        if not count:
            continue
        key = f'{table.name}.{column.name}'
        counted[key] = count
        if ids is None:
            partial = True
        else:
            moved[key] = ids

    note = reason or f'Duplicate of #{into.id}'
    lead.merged_into_id = into.id
    lead.stage = 'Lost'
    lead.lost_reason = 'Duplicate'
    if hasattr(lead, 'junk_reason_note'):
        lead.junk_reason_note = note[:400]
    lead.is_archived = True

    ledger = LeadMerge(lead_id=lead.id, into_id=into.id,
                       moves_json=moved, before_json=before,
                       partial=partial, reason=note[:400],
                       merged_by=actor)
    db.session.add(ledger)
    db.session.flush()

    audit.record('lead.merged', 'lead', lead.id,
                 old=before,
                 new={'merged_into_id': into.id, 'moved': counted,
                      'ledger_id': ledger.id},
                 reason=note, actor=actor)
    audit.record('lead.absorbed', 'lead', into.id,
                 new={'absorbed': lead.id, 'moved': counted,
                      'ledger_id': ledger.id},
                 reason=f'Absorbed #{lead.id}', actor=actor)
    db.session.commit()
    return True, {'merged': lead.id, 'into': into.id, 'moved': counted,
                  'reason': note, 'ledger_id': ledger.id,
                  'reversible': not partial}


def last_merge(lead):
    """The live merge that folded this lead away, or None."""
    from app.models.lead_merge import LeadMerge

    if lead is None or not getattr(lead, 'id', None):
        return None
    return (LeadMerge.query
            .filter(LeadMerge.lead_id == lead.id,
                    LeadMerge.reversed_at.is_(None))
            .order_by(LeadMerge.merged_at.desc())
            .first())


def unmerge_blockers(lead):
    """Why this merge cannot be reversed, in words, or ''."""
    if lead is None:
        return 'That lead does not exist.'
    if not getattr(lead, 'merged_into_id', None):
        return f'Lead #{lead.id} was not merged into anything.'
    ledger = last_merge(lead)
    if ledger is None:
        return (f'Lead #{lead.id} was merged before the CRM recorded '
                f'what a merge moved, so there is no record of which '
                f'rows to put back. It can be restored by hand from '
                f'the archive, but not automatically.')
    if ledger.partial:
        return ('That merge moved rows the CRM could not record by '
                'id, so reversing it would put some of them back and '
                'leave the rest. Reversing by hand is the only safe '
                'way.')
    return ''


def unmerge(lead, *, actor):
    """Put a merged lead and its rows back. Returns (ok, detail).

    The reverse of `merge`, exactly: the rows named in the ledger go
    back to the lead they came from, and the four columns the merge
    overwrote are restored from what was read before it wrote them.

    Only the rows in the ledger move. The survivor's own activities
    and mail — the ones it had all along, and anything added since —
    stay where they are, which is the whole reason the ids are
    recorded rather than the counts.
    """
    from sqlalchemy import text

    from app import db
    from app.services import audit

    why = unmerge_blockers(lead)
    if why:
        return False, why

    ledger = last_merge(lead)
    into_id = ledger.into_id
    moves = ledger.moves_json or {}
    before = ledger.before_json or {}

    put_back = {}
    for key, ids in moves.items():
        if not ids:
            continue
        table_name, _, column_name = key.partition('.')
        table = db.metadata.tables.get(table_name)
        if table is None:
            db.session.rollback()
            return False, (f'The table {table_name} no longer exists, '
                           f'so this merge cannot be reversed '
                           f'automatically. Nothing was changed.')
        pk = _pk_column(table)
        if pk is None:
            db.session.rollback()
            return False, (f'{table_name} has no single primary key, '
                           f'so its rows cannot be put back by id. '
                           f'Nothing was changed.')
        try:
            # Only rows that are still on the survivor. One that has
            # since been moved somewhere else by a person is left
            # alone: their decision is newer than this merge.
            placeholders = ', '.join(f':i{n}' for n in range(len(ids)))
            params = {f'i{n}': value for n, value in enumerate(ids)}
            params['lead'] = lead.id
            params['into'] = into_id
            count = db.session.execute(
                text(f'UPDATE {table_name} SET {column_name} = :lead '
                     f'WHERE {column_name} = :into '
                     f'AND {pk.name} IN ({placeholders})'),
                params).rowcount
        except Exception:                                 # noqa: BLE001
            db.session.rollback()
            log.exception('could not put back %s', key)
            return False, (f'Could not put the {table_name} rows back. '
                           f'Nothing was changed.')
        if count:
            put_back[key] = count

    lead.merged_into_id = None
    lead.stage = before.get('stage') or 'New'
    lead.lost_reason = before.get('lost_reason')
    lead.is_archived = bool(before.get('is_archived'))
    if hasattr(lead, 'junk_reason_note'):
        lead.junk_reason_note = before.get('junk_reason_note')

    from datetime import datetime as _dt
    ledger.reversed_at = _dt.utcnow()
    ledger.reversed_by = actor

    audit.record('lead.unmerged', 'lead', lead.id,
                 old={'merged_into_id': into_id},
                 new={'merged_into_id': None,
                      'restored': before,
                      'put_back': put_back,
                      'ledger_id': ledger.id},
                 reason=(f'Merge into #{into_id} reversed'),
                 actor=actor)
    audit.record('lead.unabsorbed', 'lead', into_id,
                 new={'released': lead.id, 'put_back': put_back,
                      'ledger_id': ledger.id},
                 reason=f'Released #{lead.id}', actor=actor)
    db.session.commit()
    return True, {'unmerged': lead.id, 'from': into_id,
                  'put_back': put_back, 'restored': before,
                  'ledger_id': ledger.id}


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
