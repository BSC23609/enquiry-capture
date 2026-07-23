"""Microsoft Graph mail reader.

Uses the client-credentials (app-only) flow and a *delta query*, so each run
fetches only what changed since the last one. The delta cursor lives in
Postgres, which means a restart resumes rather than re-reading the mailbox.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Iterator

import httpx

from .config import settings

log = logging.getLogger(__name__)

GRAPH = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

# Fields we actually need. Asking for fewer keeps responses small and fast.
SELECT = ",".join([
    "id", "internetMessageId", "receivedDateTime", "subject",
    "from", "sender", "body", "bodyPreview", "isDraft",
])


class GraphError(RuntimeError):
    pass


class GraphClient:
    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expires: float = 0.0
        self._http = httpx.Client(timeout=60.0)

    # ------------------------------------------------------------------ auth
    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires - 120:
            return self._token

        resp = self._http.post(
            TOKEN_URL.format(tenant=settings.tenant_id),
            data={
                "client_id": settings.client_id,
                "client_secret": settings.client_secret,
                "scope": "https://graph.microsoft.com/.default",
                "grant_type": "client_credentials",
            },
        )
        if resp.status_code != 200:
            raise GraphError(f"Token request failed [{resp.status_code}]: {resp.text[:400]}")

        data = resp.json()
        self._token = data["access_token"]
        self._token_expires = time.time() + int(data.get("expires_in", 3600))
        return self._token

    def _get(self, url: str) -> dict:
        """GET with token refresh and 429/5xx retry."""
        for attempt in range(4):
            resp = self._http.get(
                url, headers={"Authorization": f"Bearer {self._access_token()}"}
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 401 and attempt == 0:
                self._token = None          # force refresh, retry once
                continue
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 2 ** attempt))
                log.warning("Graph throttled, sleeping %ss", wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            raise GraphError(f"Graph GET failed [{resp.status_code}]: {resp.text[:400]}")
        raise GraphError(f"Graph GET gave up after retries: {url}")

    # ------------------------------------------------------------------ read
    def _initial_delta_url(self) -> str:
        since = datetime.now(timezone.utc) - timedelta(
            days=settings.lookback_days_on_first_run
        )
        return (
            f"{GRAPH}/users/{settings.mailbox}/mailFolders/{settings.folder}"
            f"/messages/delta"
            f"?$select={SELECT}"
            f"&$filter=receivedDateTime ge {since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        )

    def fetch_messages(self, delta_link: str | None) -> tuple[list[dict], str | None]:
        """Return (messages, new_delta_link).

        Follows @odata.nextLink pages until Graph hands back a @odata.deltaLink,
        which is the cursor to store for next time.
        """
        url = delta_link or self._initial_delta_url()
        messages: list[dict] = []
        new_delta: str | None = None

        while url:
            page = self._get(url)

            for item in page.get("value", []):
                # Delta feeds include deletions and drafts; skip both.
                if "@removed" in item:
                    continue
                if item.get("isDraft"):
                    continue
                if not item.get("id"):
                    continue
                messages.append(item)

            if len(messages) >= settings.max_messages_per_run:
                # Stop early but DON'T store a delta link — we'll resume from
                # the same cursor next run and pick up the rest.
                log.info("Hit max_messages_per_run (%s), pausing", settings.max_messages_per_run)
                return messages[: settings.max_messages_per_run], None

            if "@odata.nextLink" in page:
                url = page["@odata.nextLink"]
            else:
                new_delta = page.get("@odata.deltaLink")
                url = None

        return messages, new_delta

    def close(self) -> None:
        self._http.close()


# ---------------------------------------------------------------- helpers
def address_of(msg: dict, key: str = "from") -> tuple[str, str]:
    """Return (email, display_name) for the from/sender field."""
    node = (msg.get(key) or {}).get("emailAddress") or {}
    return (node.get("address") or "").strip().lower(), (node.get("name") or "").strip()


def body_of(msg: dict) -> tuple[str, str]:
    """Return (content, contentType) — contentType is 'html' or 'text'."""
    body = msg.get("body") or {}
    return body.get("content") or msg.get("bodyPreview") or "", (body.get("contentType") or "text").lower()
