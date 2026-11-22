"""HTTP client of the QuotaPath public API (https://api.quotapath.com/docs/).

QuotaPath authenticates every call with a workspace API key sent as
``Authorization: Token <key>`` (the key an admin generates in QuotaPath,
Settings > Integrations > API). The server is ``https://api.quotapath.com/v1``.
Lists page with ``limit`` / ``offset`` and answer ``{count, next, previous,
results}``. A 429 is retried after ``Retry-After`` by ``safe_request``; reads
also retry a transient 502/503/504. The key is never echoed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from app.core.egress import egress_client
from app.integrations.http import (
    PLATFORM_USER_AGENT,
    TRANSIENT_RETRY_STATUSES,
    RateLimiter,
    app_token_cache_key,
    get_shared_rate_limiter,
    safe_request,
)

PROVIDER = "QuotaPath"
SOURCE = "integration:quotapath"
BASE_URL = "https://api.quotapath.com/v1"

_TIMEOUT = 60
#: QuotaPath publishes no rate limit: a conservative pace of five calls a
#: second, a burst of ten and four in flight, the 429 handling of
#: ``safe_request`` covering the rest.
_RATE_PER_SECOND = 5.0
_BURST = 10.0
_MAX_CONCURRENCY = 4


def seg(value: Any) -> str:
    """One path segment, encoded."""
    return quote(str(value).strip(), safe="")


@dataclass
class QuotaPathResponse:
    """A successful answer: its parsed body (or raw bytes)."""

    data: Any
    content: bytes
    content_type: str


class QuotaPathClient:
    """Token-authenticated, paced requests against one QuotaPath workspace."""

    def __init__(self, api_key: str, base_url: str = BASE_URL) -> None:
        self.api_key = (api_key or "").strip()
        self.base_url = base_url.rstrip("/")

    def _limiter(self) -> RateLimiter:
        # One bucket per key; hashed so the registry never holds the secret.
        key = app_token_cache_key("quotapath", self.api_key)
        return get_shared_rate_limiter(
            f"quotapath:{key}", rate=_RATE_PER_SECOND, capacity=_BURST, max_concurrency=_MAX_CONCURRENCY
        )

    async def request(
        self,
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
    ) -> tuple[QuotaPathResponse | None, str | None]:
        """``(response, None)`` or ``(None, "Error: ...")``."""
        if not self.api_key:
            return None, f"Error: the {PROVIDER} integration has no API key. Edit the integration and set it."
        method = method.upper()
        query = {key: value for key, value in (params or {}).items() if value is not None and value != ""}
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query, doseq=True)}"
        headers = {
            "Accept": "application/json",
            "User-Agent": PLATFORM_USER_AGENT,
            "Authorization": f"Token {self.api_key}",
        }
        extra: dict[str, Any] = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            extra["content"] = json.dumps(body, separators=(",", ":"))
        async with self._limiter().slot(), egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                method.lower(),
                url,
                PROVIDER,
                action,
                headers=headers,
                retry_statuses=TRANSIENT_RETRY_STATUSES if method == "GET" else None,
                **extra,
            )
        if err:
            return None, err
        if resp is None or not resp.content:
            return QuotaPathResponse(None, b"", ""), None
        content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if content_type == "application/json" or content_type.endswith("+json"):
            try:
                return QuotaPathResponse(resp.json(), resp.content, content_type), None
            except ValueError:
                return None, f"Error: {PROVIDER} answered unreadable JSON while trying to {action}."
        return QuotaPathResponse(None, resp.content, content_type), None

    async def list_all(
        self, path: str, action: str, params: dict[str, Any] | None = None, *, max_rows: int = 5000
    ) -> tuple[list[Any], bool, str | None]:
        """Every row of a ``limit``/``offset`` list: ``(rows, truncated, error)``.

        A list answered as a bare array (no ``results``) is one page.
        """
        rows: list[Any] = []
        offset = int((params or {}).get("offset") or 0)
        page = 500
        while True:
            resp, err = await self.request(
                "GET", path, action, params={**(params or {}), "limit": page, "offset": offset}
            )
            if err or resp is None:
                return rows, False, err or f"Error: {PROVIDER} answered nothing while trying to {action}."
            data = resp.data
            if isinstance(data, list):
                rows.extend(data)
                return rows[:max_rows], len(rows) > max_rows, None
            batch = data.get("results") if isinstance(data, dict) else None
            if not isinstance(batch, list):
                return rows, False, None
            rows.extend(batch)
            if len(rows) >= max_rows:
                return rows[:max_rows], True, None
            if not batch or not data.get("next"):
                return rows, False, None
            offset += len(batch)
