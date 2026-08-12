"""Thin Microsoft Graph API client for the email → lead ingest.

Uses OAuth 2.0 client-credentials (app-only) flow via `msal`, and `requests`
for the Graph REST calls. No hardcoded secrets — pulls from env at construct
time.

Only the surface the ingest pipeline needs is implemented:
    - list_messages(mailbox, since_utc, ...)
    - mark_as_read(mailbox, message_id)

Everything else (folders, attachments, subscriptions) is out of scope.
"""
from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Iterator, Optional
from urllib.parse import quote

import requests

try:
    import msal  # type: ignore
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "The `msal` package is required for GraphClient. "
        "Add `msal>=1.28.0` to requirements.txt and reinstall."
    ) from e

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]
DEFAULT_TIMEOUT = 30
DEFAULT_SELECT = (
    "id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
    "body,bodyPreview,internetMessageId,isRead,hasAttachments"
)


class GraphClient:
    """Thin wrapper around Microsoft Graph for reading a shared mailbox."""

    def __init__(self) -> None:
        self.tenant_id = os.environ.get("MS_TENANT_ID")
        self.client_id = os.environ.get("MS_CLIENT_ID")
        self.client_secret = os.environ.get("MS_CLIENT_SECRET")

        missing = [
            name for name, val in (
                ("MS_TENANT_ID", self.tenant_id),
                ("MS_CLIENT_ID", self.client_id),
                ("MS_CLIENT_SECRET", self.client_secret),
            )
            if not val
        ]
        if missing:
            raise RuntimeError(
                "GraphClient: missing required env var(s): " + ", ".join(missing)
            )

        self._authority = f"https://login.microsoftonline.com/{self.tenant_id}"
        self._msal_app: Optional[msal.ConfidentialClientApplication] = None
        self._token: Optional[str] = None
        self._token_expiry: float = 0.0  # unix ts
        self._session = requests.Session()

    # ─── auth ────────────────────────────────────────────────────────────
    def _get_msal_app(self) -> msal.ConfidentialClientApplication:
        if self._msal_app is None:
            self._msal_app = msal.ConfidentialClientApplication(
                client_id=self.client_id,
                client_credential=self.client_secret,
                authority=self._authority,
            )
        return self._msal_app

    def get_token(self, force_refresh: bool = False) -> str:
        """Return a valid access token, refreshing if expired or forced."""
        now = time.time()
        if (
            not force_refresh
            and self._token
            and now < self._token_expiry - 60  # 60s safety margin
        ):
            return self._token

        app = self._get_msal_app()
        result = app.acquire_token_for_client(scopes=GRAPH_SCOPE)
        if "access_token" not in result:
            raise RuntimeError(
                "MSAL token acquisition failed: "
                f"{result.get('error')} — {result.get('error_description')}"
            )
        self._token = result["access_token"]
        # `expires_in` is seconds from now
        self._token_expiry = now + int(result.get("expires_in", 3600))
        log.debug("Fetched new Graph token, valid ~%ss", result.get("expires_in"))
        return self._token

    # ─── low-level request with retry ───────────────────────────────────
    def _request(
        self,
        method: str,
        path_or_url: str,
        *,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        _retry_401: bool = True,
        _retry_429: bool = True,
    ) -> requests.Response:
        """Send an HTTP request to Graph.

        `path_or_url` may be either a path fragment like `/users/x/messages`
        or a full URL (used for `@odata.nextLink` pagination).
        Retries once on 401 (with fresh token) and on 429 (respect Retry-After).
        """
        if path_or_url.startswith("http"):
            url = path_or_url
        else:
            if not path_or_url.startswith("/"):
                path_or_url = "/" + path_or_url
            url = GRAPH_BASE + path_or_url

        token = self.get_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        resp = self._session.request(
            method=method,
            url=url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=DEFAULT_TIMEOUT,
        )

        if resp.status_code == 401 and _retry_401:
            log.info("Graph 401 — refreshing token and retrying once")
            self.get_token(force_refresh=True)
            return self._request(
                method, path_or_url,
                params=params, json_body=json_body,
                _retry_401=False, _retry_429=_retry_429,
            )

        if resp.status_code == 429 and _retry_429:
            retry_after = int(resp.headers.get("Retry-After", "5"))
            log.warning("Graph 429 — sleeping %ss then retrying", retry_after)
            time.sleep(max(1, retry_after))
            return self._request(
                method, path_or_url,
                params=params, json_body=json_body,
                _retry_401=_retry_401, _retry_429=False,
            )

        return resp

    # ─── high-level API ─────────────────────────────────────────────────
    def list_messages(
        self,
        mailbox: str,
        since_utc: datetime,
        only_unread: bool = False,
        top: int = 100,
    ) -> Iterator[dict]:
        """Yield messages received on/after `since_utc` from `mailbox`.

        Handles `@odata.nextLink` pagination transparently.
        """
        # Ensure ISO-8601 with Z suffix (Graph is picky)
        if since_utc.tzinfo is None:
            since_utc = since_utc.replace(tzinfo=timezone.utc)
        iso = since_utc.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        filter_parts = [f"receivedDateTime ge {iso}"]
        if only_unread:
            filter_parts.append("isRead eq false")
        filter_clause = " and ".join(filter_parts)

        path = f"/users/{quote(mailbox)}/messages"
        params = {
            "$filter": filter_clause,
            "$top": str(top),
            "$select": DEFAULT_SELECT,
            "$orderby": "receivedDateTime desc",
        }

        url_or_path: str = path
        first_call = True

        while url_or_path:
            resp = self._request(
                "GET",
                url_or_path,
                params=params if first_call else None,
            )
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"Graph list_messages failed: HTTP {resp.status_code} — "
                    f"{resp.text[:500]}"
                )
            data = resp.json()
            for msg in data.get("value", []):
                yield msg
            url_or_path = data.get("@odata.nextLink") or ""
            first_call = False

    def list_attachments(self, mailbox: str, message_id: str) -> list[dict]:
        """GET /users/{mailbox}/messages/{id}/attachments — returns list of attachments.

        Each dict has: id, name, contentType, size, contentBytes (base64),
        isInline, @odata.type. Filters to fileAttachments only (excludes
        itemAttachments and referenceAttachments, which need different handling).

        Called AFTER a message has passed all filters and been accepted as a
        real lead — this keeps us from downloading attachment bytes for the
        thousands of messages we reject each day.
        """
        # NOTE: $select cannot include contentBytes — Graph rejects it as
        # "not a property of microsoft.graph.attachment". contentBytes only
        # exists on the fileAttachment sub-type. Drop $select entirely and
        # take the full payload (small — usually one attachment per email
        # in this workload).
        path = (
            f"/users/{quote(mailbox)}/messages/{quote(message_id, safe='')}"
            "/attachments"
        )
        resp = self._request("GET", path)
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Graph list_attachments failed: HTTP {resp.status_code} — "
                f"{resp.text[:500]}"
            )
        data = resp.json()
        atts = data.get("value") or []
        return [a for a in atts
                if str(a.get("@odata.type", "")).endswith("fileAttachment")]

    def mark_as_read(self, mailbox: str, message_id: str) -> None:
        """Best-effort — PATCH isRead=true. Logs on failure, never raises."""
        try:
            path = f"/users/{quote(mailbox)}/messages/{quote(message_id, safe='')}"
            resp = self._request("PATCH", path, json_body={"isRead": True})
            if resp.status_code >= 400:
                log.warning(
                    "mark_as_read failed for %s: HTTP %s — %s",
                    message_id, resp.status_code, resp.text[:300],
                )
        except Exception as e:  # noqa: BLE001
            log.warning("mark_as_read exception for %s: %s", message_id, e)
