"""
What "contact" means, in one place.

There is exactly one definition of when somebody last spoke to a
customer, and everything that reports on it reads this module: the
Sales Intelligence tiles, the Copilot's My Day, its stale list, and
next-best-action.

That is not tidiness. The tile and the Copilot answer the same question
on the same screen, and a user who sees "12 idle" in one and "8 idle" in
the other stops believing both.

Contact is an activity OR an email
    lead_activities holds seven rows against eleven thousand leads —
    nobody logs calls. The email trail holds fifteen hundred, with real
    inbound and outbound timestamps. Reading only the activity log
    reported every open lead as neglected, which would have sent a
    salesperson to chase people they emailed that morning.

Contact is NOT `updated_at`
    That column moves when somebody fixes a typo. Reporting a field edit
    as contact with a customer is a lie the CRM tells itself, and the
    tiles told it for as long as they existed.
"""
from __future__ import annotations

from datetime import datetime, timedelta


def contacted_since(cutoff):
    """The set of lead ids touched since `cutoff`.

    One set rather than a per-lead maximum: the question is always "has
    anything happened since", and two grouped reads answer it for the
    whole table at once.
    """
    from app import LeadActivity, LeadEmail

    acted = {r[0] for r in LeadActivity.query
             .with_entities(LeadActivity.lead_id)
             .filter(LeadActivity.occurred_at >= cutoff).all() if r[0]}
    mailed = {r[0] for r in LeadEmail.query
              .with_entities(LeadEmail.lead_id)
              .filter(LeadEmail.sent_or_received_at >= cutoff).all() if r[0]}
    return acted | mailed


def ever_contacted():
    """Lead ids with any recorded contact at all.

    Used to tell "nobody has called them" apart from "nobody records
    calls" — the distinction §6.8 asks answers to make.
    """
    return contacted_since(datetime(1970, 1, 1))


def last_contact(lead):
    """The most recent real contact on one lead, or None."""
    from app import LeadActivity, LeadEmail

    stamps = []
    act = (LeadActivity.query.with_entities(LeadActivity.occurred_at)
           .filter(LeadActivity.lead_id == lead.id)
           .order_by(LeadActivity.occurred_at.desc()).first())
    if act and act[0]:
        stamps.append(act[0])
    mail = (LeadEmail.query.with_entities(LeadEmail.sent_or_received_at)
            .filter(LeadEmail.lead_id == lead.id)
            .order_by(LeadEmail.sent_or_received_at.desc()).first())
    if mail and mail[0]:
        stamps.append(mail[0])
    return max(stamps) if stamps else None


def days_since_contact(lead):
    last = last_contact(lead)
    if last is None:
        return None
    return (datetime.utcnow() - last).days


def untouched_ids(lead_ids, *, days=3):
    """Which of these leads have had no contact in `days`.

    Takes ids so a caller that has already scoped its query does not
    have to hand over objects, and so the two grouped reads happen once
    however many leads are being judged.
    """
    touched = contacted_since(datetime.utcnow() - timedelta(days=days))
    return [lid for lid in lead_ids if lid not in touched]
