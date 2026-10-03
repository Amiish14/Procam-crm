"""
Feature flags for the email-notification release.

Every flag is off unless the environment says otherwise, and every flag
guards *new* behaviour only: with all of them unset the CRM behaves
exactly as it did before this release, which is what makes the rollback
a single line in `.env` and a restart rather than a revert.

    FEATURE_EMAIL_NOTIFY    outbound notification email goes through the
                            outbox (queued, retried, batched, logged)
                            instead of straight at Graph
    FEATURE_RFQ_CAPTURE     keep the client's original message as a .eml
                            and record its attachments
    FEATURE_DAILY_BRIEF     the 08:30 action brief
    FEATURE_WEEKLY_PACK     the Thursday review pack and the Friday
                            freeze
    FEATURE_REPLY_CAPTURE   file replies on the thread against the lead
"""
from __future__ import annotations

import os

FLAGS = (
    ('FEATURE_EMAIL_NOTIFY', 'Queue outbound email through the outbox'),
    ('FEATURE_RFQ_CAPTURE', 'Keep the original RFQ email and its files'),
    ('FEATURE_DAILY_BRIEF', 'Daily action brief, 08:30 IST'),
    ('FEATURE_WEEKLY_PACK', 'Thursday review pack and Friday freeze'),
    ('FEATURE_REPLY_CAPTURE', 'File replies against the lead'),
)

_TRUE = ('1', 'true', 'yes', 'on')


def on(name, default=False):
    """Is this flag on? Read every call — no caching, so turning a flag
    off in an incident takes a restart and not a deploy."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == '':
        return bool(default)
    return raw.strip().lower() in _TRUE


def all_flags():
    """[(name, description, state)] for the admin screen."""
    return [(n, d, on(n)) for n, d in FLAGS]
