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

# v2026-08-31 — cut-over: leads@procamgroup.in is the sole source of
# opportunities. Everything scans through the webhook now; the daily
# 09:00 IST poll is retired. Default flipped to 'mailbox' so a fresh
# deploy is safe-by-default even if .env forgets to set it.
DEFAULT_MODE = MAILBOX
DEFAULT_INBOX = 'leads@procamgroup.in'


def current_mode() -> str:
    """The active ingestion mode. Defaults to mailbox (webhook-only)."""
    m = (os.environ.get('EMAIL_INGESTION_MODE') or DEFAULT_MODE).strip().lower()
    return m if m in VALID_MODES else DEFAULT_MODE


def crm_inbox_email() -> str | None:
    """Shared mailbox address to watch. Falls back to the canonical leads
    address so a fresh deploy without a CRM_INBOX_EMAIL env still knows
    where to subscribe."""
    v = (os.environ.get('CRM_INBOX_EMAIL') or '').strip()
    return v or DEFAULT_INBOX


def poll_should_run() -> bool:
    """Legacy 09:00 IST poll — disabled by default now that the mailbox +
    webhook route is authoritative. Set EMAIL_INGESTION_MODE=azure_api on
    the process running scripts/email_ingest.py if a one-off backfill is
    ever needed (e.g. for a historical import)."""
    return current_mode() == AZURE_API


def webhook_should_run() -> bool:
    return current_mode() == MAILBOX
