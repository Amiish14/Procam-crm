"""
v2026-08 — Email ingestion service abstraction.

Two modes, chosen at boot by the env var EMAIL_INGESTION_MODE:

    azure_api  →  poll the shared mailbox via Microsoft Graph on a schedule
                  (the existing behaviour — kept intact for rollback).
    mailbox    →  event-driven: Microsoft Graph pushes a webhook when new
                  mail arrives; the webhook fetches the message and
                  reuses the existing parser + Lead-creation code path.

Adding the new mode does NOT remove or alter the existing Azure-API
pipeline — it just installs a webhook handler alongside it. Both share
the same parser, AI extractor, dedup logic, and Lead-creation code, so
Leads created via either route look identical downstream.

Env vars (all documented in .env.example):

    EMAIL_INGESTION_MODE   'azure_api' (default) | 'mailbox'
    CRM_INBOX_EMAIL        Shared-mailbox address to watch. Configured
                           later, once the dedicated address exists.
    EMAIL_WEBHOOK_SECRET   Random string; Microsoft Graph echoes this on
                           every notification so we can verify it.
    EMAIL_WEBHOOK_URL      Public HTTPS URL of /api/email/webhook (used
                           only when re-subscribing).
"""
from __future__ import annotations
import os


AZURE_API   = 'azure_api'
MAILBOX     = 'mailbox'
VALID_MODES = {AZURE_API, MAILBOX}


def current_mode() -> str:
    """The active ingestion mode. Defaults to 'azure_api' for back-compat."""
    m = (os.environ.get('EMAIL_INGESTION_MODE') or AZURE_API).strip().lower()
    return m if m in VALID_MODES else AZURE_API


def crm_inbox_email() -> str | None:
    """Shared mailbox address to watch. Optional until it's provisioned."""
    v = (os.environ.get('CRM_INBOX_EMAIL') or '').strip()
    return v or None


def poll_should_run() -> bool:
    """The scheduled poll job (existing behaviour) is disabled when the
    mailbox / webhook mode is active — otherwise both would compete on the
    same message and dedup would waste cycles."""
    return current_mode() == AZURE_API


def webhook_should_run() -> bool:
    return current_mode() == MAILBOX
