"""What a later notice does to the lead its portal event already has.

13 Nov. One lead per portal event (`portal_rfq.event_key`), and every
mail after the first one is a notice about that event: a reminder, a
corrigendum, "technical bid live", "about to end", "withdrawn by the
buyer". Each is filed on the lead's mail trail -- that part is
`_file_against_lead` and was already there -- and two of them say
something the trail alone cannot act on:

  * **withdrawn** — the buyer called the event off. The lead is closed
    as Lost with the reason "withdrawn by buyer", which is deliberately
    not one of the six reasons the Lost dialog offers: an auction the
    buyer cancelled is not a deal we lost on price, and folding it into
    "Client dropped project" would say we were beaten when we were not.

  * **about to end** — it states the minute the event closes, which is
    the submission deadline. Read, and handed to `quote_due_flow`,
    which proposes rather than applies (§2.5) and leaves it
    unconfirmed for its owner to tap.

NOTHING HERE IS A HARD CHANGE

A close writes an audit row carrying the stage it moved from, so it is
reversible from the record. A withdrawal arriving on a lead somebody
already closed does nothing at all: a notice cannot un-close, re-open
or re-close a lead, and `was` in the audit row is what makes "it was
at RFQ Generated when the buyer pulled the event" answerable later.
"""
from __future__ import annotations

import logging
from datetime import datetime

log = logging.getLogger(__name__)

#: Stages that mean somebody already decided this was over. Shared
#: with `mail_ingest` deliberately -- the same list, one meaning.
from app.services.mail_ingest import CLOSED_STAGES   # noqa: E402

#: Who the audit row names. Not an employee code: no person did this.
ACTOR = 'portal'


def apply_notice(lead, kind, *, subject='', body='',
                 email_message_id=None, received_at=None):
    """Act on one notice. Returns a sentence, or '' for nothing done.

    Never raises and never commits: the caller owns the transaction,
    because the notice and the mail trail row have to land together
    or not at all.
    """
    from app.services import portal_rfq

    if lead is None:
        return ''
    try:
        if kind == portal_rfq.WITHDRAWN:
            return close_as_withdrawn(lead)
        if kind in (portal_rfq.ENDING, portal_rfq.REMINDER,
                    portal_rfq.TECHNICAL):
            return _read_the_deadline(
                lead, subject=subject, body=body,
                email_message_id=email_message_id,
                received_at=received_at)
    except Exception:                                     # noqa: BLE001
        log.exception('could not apply a %s notice to lead %s', kind,
                      getattr(lead, 'id', None))
    return ''


def close_as_withdrawn(lead):
    """Lost, "withdrawn by buyer", with the old stage in the audit row."""
    from app.services import audit, portal_rfq

    was = (getattr(lead, 'stage', '') or '')
    if was in CLOSED_STAGES:
        # Already over. Saying so again would overwrite whatever
        # reason a person wrote with a machine's.
        return (f'the event was withdrawn; the lead was already '
                f'{was} and was left alone')
    lead.stage = 'Lost'
    lead.lost_reason = portal_rfq.LOST_REASON[:60]
    lead.updated_at = datetime.utcnow()
    audit.record('lead.closed_lost', 'lead', lead.id,
                 old={'stage': was},
                 new={'stage': lead.stage,
                      'lost_reason': lead.lost_reason},
                 actor=ACTOR,
                 reason='the buyer withdrew the portal event',
                 commit=False)
    try:
        from app.services import task_autoclose
        task_autoclose.close_for_lead(lead, ACTOR)
    except Exception:                                     # noqa: BLE001
        log.exception('could not close the tasks on lead %s', lead.id)
    return f'closed as Lost ({portal_rfq.LOST_REASON}), was {was}'


def _read_the_deadline(lead, *, subject, body, email_message_id,
                       received_at):
    from app.services import quote_due_flow

    what = quote_due_flow.read_from_message(
        lead, subject=subject, body=body,
        email_message_id=email_message_id, received_at=received_at)
    if what == 'set':
        return 'the submission deadline was read off the notice ' \
               '(unconfirmed)'
    if what == 'proposed':
        return 'a new submission deadline was proposed'
    return ''
