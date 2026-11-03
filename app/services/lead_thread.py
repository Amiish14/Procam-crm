"""The mail thread a lead belongs to: computed, stored, and looked up.

CRM-USER-ISSUES-02NOV §2: "A reply on the same email thread creates a
duplicate lead."

WHY IT STILL HAPPENED

Most of the machinery was already here and correct. `lead_intake`
classifies on the thread headers at step 3 — "deterministic, final,
outranks everything" — and files a reply against the lead it belongs to
instead of creating one. `lead_merge.thread_key()` computes a key.
`leads.thread_key` is a column, indexed, migrated by
`scripts/2026_10_30_lead_flow.py`, and its own comment in app.py says
"filled at intake; a reply whose thread is already here joins the lead
instead of starting another."

It was never filled. Not once, by any code path — only by three tests
that set it by hand. The column has been NULL in live data since the
day it was added. So:

    * `lead_merge.candidates()` asks `other.thread_key == mine_thread`
      for its one *certain* duplicate rule. With both sides NULL the
      rule can never fire, and thread duplicates fell through to the
      subject heuristic, which only ever "draws a banner".
    * step 3 of the classifier still worked, but only through
      `conversation_id` and the stored message ids. A reply that
      arrives with a conversation id we never saw — a forward relayed
      by a colleague, a client replying from a different client that
      rewrote the headers, a thread whose first message reached us
      before this mailbox existed — had nothing to match.

THE TWO KEYS, AND WHY BOTH

`lead_merge.thread_key` prefers Exchange's conversation id and falls
back to the normalised subject plus the sender's domain. That fallback
is the half that answers §2's "then the normalised subject + sender
domain within 30 days": it is a key, so it is answered by an index
rather than by `find_by_subject`'s scan of the newest two thousand
leads, and it keeps matching after a mail client has stripped the
`Re:` that step 5 depends on.

A SUBJECT KEY IS NOT A CERTAINTY, SO IT IS WINDOWED

A conversation id is unique to a thread and matches at any age. A
subject-plus-domain key is not: "RFQ" from a regular customer is the
same key in March and in November and they are different enquiries.
So a `c:` key matches without a window and an `s:` key only inside
WINDOW_DAYS. The window is `lead_merge.WINDOW_DAYS`, which is already
30, rather than a second 30 written here.
"""
from __future__ import annotations

from datetime import datetime, timedelta

#: A subject-and-domain key means "probably the same enquiry". It is
#: only trusted inside this many days. A conversation-id key means
#: "certainly the same thread" and is not windowed at all.
from app.services.lead_merge import WINDOW_DAYS  # noqa: F401  (re-exported)


def key_for_message(msg, *, sender=None):
    """The thread key for an incoming Graph message, or ''.

    `sender` overrides the From when the parser has already resolved a
    forward to the customer's own address — otherwise every enquiry a
    colleague relays keys on the colleague's domain, and two unrelated
    forwards from the same person become one thread.
    """
    from app.services import lead_intake as li
    from app.services import lead_merge as lm

    msg = msg or {}
    keys = li.thread_keys(msg)
    address = (sender or '').strip() or li.effective_sender(msg)
    return lm.thread_key(
        conversation_id=keys.get('conversation_id'),
        subject=(msg.get('subject') or ''),
        domain=li.domain_of(address))


def key_for_lead(lead):
    """The thread key a lead already on file would have.

    Used by the backfill and by the duplicate report. Reads the
    columns the intake stored rather than re-parsing anything.
    """
    from app.services import lead_intake as li
    from app.services import lead_merge as lm

    if lead is None:
        return ''
    return lm.thread_key(
        conversation_id=(getattr(lead, 'conversation_id', '') or ''),
        subject=(getattr(lead, 'original_email_subject', '') or ''),
        domain=li.domain_of(
            (getattr(lead, 'original_email_from', '') or '')
            or (getattr(lead, 'email', '') or '')))


def is_windowed(key):
    """Whether this kind of key may only be trusted recently.

    `s:` keys are a guess about two enquiries looking alike; `c:` keys
    are Exchange telling us it is one conversation.
    """
    return not (key or '').startswith('c:')


def find_by_key(key, *, within_days=None, exclude_id=None):
    """The earliest live lead on this thread, or None.

    Earliest, not latest: the thread belongs to the enquiry that
    started it, and a reply filed against the newest duplicate would
    build the trail on the wrong lead.

    Archived, junk-deleted and already-merged leads are skipped. A
    reply must not resurrect a lead somebody archived, and filing it
    against a merged shell hides it behind a redirect.
    """
    from app import Lead

    key = (key or '').strip()
    if not key:
        return None

    q = Lead.query.filter(Lead.thread_key == key,
                          Lead.is_archived.isnot(True),
                          Lead.junk_deleted.isnot(True),
                          Lead.merged_into_id.is_(None))
    if exclude_id:
        q = q.filter(Lead.id != exclude_id)
    if is_windowed(key):
        days = WINDOW_DAYS if within_days is None else within_days
        q = q.filter(Lead.created_at >= datetime.utcnow()
                     - timedelta(days=days))
    row = q.order_by(Lead.id.asc()).first()
    return row.id if row else None


def fill(lead, msg=None, *, sender=None, key=None):
    """Put the thread key on a lead at intake. Returns what was set.

    Never raises and never overwrites: a key already on the lead is
    the one its first message produced, and the first message is what
    names the thread.
    """
    if lead is None:
        return ''
    try:
        if (getattr(lead, 'thread_key', '') or '').strip():
            return lead.thread_key
        value = (key or '').strip() or key_for_message(msg, sender=sender)
        if value:
            lead.thread_key = value[:200]
        return getattr(lead, 'thread_key', '') or ''
    except Exception:                                     # noqa: BLE001
        # A lead that arrives without its thread key is worse off than
        # one with it, and far better off than no lead at all.
        return ''
