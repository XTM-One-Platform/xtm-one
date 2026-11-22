# SPDX-License-Identifier: Apache-2.0

"""DealHub REST client: credential, instance host, bearer auth, pacing.

Every DealHub API (Quote, Headless, CRM, User, Partner, Pricing, Version)
takes a secret bearer token a CPQ administrator generates, sent as
``Authorization: Bearer <token>``. The host is the account's instance:
``api.dealhub.io`` by default, a regional instance otherwise; it is stored in
the integration's extra config and reaches the builder as the compound token
``host||token`` (``integration_tools._get_token``), the token last and parsed
greedily.

DealHub documents no rate limit: one shared bucket per credential keeps the
calls polite, a 429 is retried after its ``Retry-After`` by ``safe_request``
and a GET also retries a transient 502/503/504.
"""

from __future__ import annotations

import json
import re
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
    parse_compound_token,
    safe_request,
)

PROVIDER = "DealHub"
SOURCE = "integration:dealhub"
DEFAULT_HOST = "api.dealhub.io"

_TIMEOUT = 60
_RATE_PER_SECOND = 5.0
_BURST = 10.0
_MAX_CONCURRENCY = 4
#: A DealHub host: a name under dealhub.io, nothing else (no scheme, path or port).
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*\.dealhub\.io$")


def normalize_host(value: str | None) -> str | None:
    """The instance host from what a person typed, or ``None`` when it is not a DealHub host.

    ``https://service-eu1.dealhub.io/`` and ``service-eu1.dealhub.io`` give
    ``service-eu1.dealhub.io``; an empty value gives the default host. A host
    outside ``dealhub.io`` is refused so the token never leaves for another one.
    """
    text = (value or "").strip().lower()
    if not text:
        return DEFAULT_HOST
    text = re.sub(r"^https://", "", text).rstrip("/")
    return text if _HOST_RE.match(text) else None


@dataclass(frozen=True)
class DealHubCredentials:
    host: str | None
    token: str

    @property
    def base_url(self) -> str:
        return f"https://{self.host}"

    def problem(self) -> str | None:
        if not self.token:
            return f"Error: the {PROVIDER} integration has no API token. Edit the integration and set it."
        if self.host is None:
            return (
                f"Error: the {PROVIDER} instance host is not a dealhub.io host. "
                "Edit the integration and set it (for example api.dealhub.io)."
            )
        return None


def build_dealhub_token(host: str | None, token: str) -> str:
    """The compound token ``host||token`` the dispatcher hands the builder."""
    return f"{(host or '').strip()}||{(token or '').strip()}"


def parse_dealhub_token(token: str) -> DealHubCredentials:
    """``host||token``, or a bare token on the default host."""
    raw = token or ""
    if "||" not in raw:
        return DealHubCredentials(DEFAULT_HOST, raw.strip())
    host, secret = parse_compound_token(raw, 2, greedy_last=True)
    return DealHubCredentials(normalize_host(host), secret.strip())


def seg(value: Any) -> str:
    """One path segment, encoded."""
    return quote(str(value).strip(), safe="")


@dataclass
class DealHubResponse:
    data: Any
    content: bytes
    content_type: str
    filename: str | None


class DealHubClient:
    """Bearer-authenticated, paced requests against one DealHub account."""

    def __init__(self, creds: DealHubCredentials) -> None:
        self.creds = creds

    def _limiter(self) -> RateLimiter:
        key = app_token_cache_key("dealhub", self.creds.host or "", self.creds.token)
        return get_shared_rate_limiter(
            f"dealhub:{key}", rate=_RATE_PER_SECOND, capacity=_BURST, max_concurrency=_MAX_CONCURRENCY
        )

    async def request(
        self,
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        accept: str = "application/json",
    ) -> tuple[DealHubResponse | None, str | None]:
        """``(response, None)`` or ``(None, "Error: ...")``; the token is never in the error."""
        problem = self.creds.problem()
        if problem:
            return None, problem
        method = method.upper()
        query = {k: v for k, v in (params or {}).items() if v is not None and v != "" and v != []}
        url = f"{self.creds.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(_query_values(query), doseq=True)}"
        headers = {
            "Accept": accept,
            "User-Agent": PLATFORM_USER_AGENT,
            "Authorization": f"Bearer {self.creds.token}",
        }
        extra: dict[str, Any] = {}
        if body is not None:
            headers["Content-Type"] = "application/json"
            extra["content"] = json.dumps(body, separators=(",", ":"))
        # A refused token is told apart from any other failure, never quoting it.
        extra["expected_statuses"] = {401, 403}
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
            if resp is not None and resp.status_code in (401, 403):
                return None, (
                    f"Error: {PROVIDER} refused the API token (HTTP {resp.status_code}) while trying to {action}. "
                    "Check the token and that its user may use this API."
                )
            return None, err
        if resp is None:
            return DealHubResponse(None, b"", "", None), None
        content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        filename = _filename(resp.headers.get("content-disposition"))
        if not resp.content:
            return DealHubResponse(None, b"", content_type, filename), None
        if content_type == "application/json" or content_type.endswith("+json"):
            try:
                return DealHubResponse(resp.json(), resp.content, content_type, filename), None
            except ValueError:
                return None, f"Error: {PROVIDER} answered unreadable JSON while trying to {action}."
        return DealHubResponse(None, resp.content, content_type, filename), None


def _filename(disposition: str | None) -> str | None:
    if not disposition:
        return None
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', disposition, re.I)
    if not match:
        return None
    name = match.group(1).strip().replace("/", "_").replace("\\", "_")
    return name or None


def _query_values(query: dict[str, Any]) -> dict[str, Any]:
    """Booleans as ``true``/``false``; lists as repeated values."""
    out: dict[str, Any] = {}
    for key, value in query.items():
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            out[key] = list(value)
        else:
            out[key] = value
    return out


async def probe(creds: DealHubCredentials) -> tuple[bool, str]:
    """Read the account's active users: proves the host and the token."""
    problem = creds.problem()
    if problem:
        return False, problem.removeprefix("Error: ")
    resp, err = await DealHubClient(creds).request(
        "GET", "/api/v1/users", "read the account's users", params={"status": "active"}
    )
    if err:
        return False, err.removeprefix("Error: ")
    data = resp.data if resp else None
    users = data.get("users") if isinstance(data, dict) else data
    count = len(users) if isinstance(users, list) else None
    suffix = f" ({count} active user(s))" if count is not None else ""
    return True, f"Connected to {PROVIDER} at {creds.host}{suffix}."


__all__ = [
    "DEFAULT_HOST",
    "PROVIDER",
    "SOURCE",
    "DealHubClient",
    "DealHubCredentials",
    "build_dealhub_token",
    "egress_client",
    "normalize_host",
    "parse_dealhub_token",
    "probe",
    "seg",
]
