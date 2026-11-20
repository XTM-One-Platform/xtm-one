# SPDX-License-Identifier: Apache-2.0

"""Aikido Security REST client: credential, regional host, auth, rate limit.

Aikido exposes one public REST API per data region
(``https://<host>/api/public/v1``), authenticated by an OAuth2
client-credentials grant: the client id and secret of an API client created
in the workspace settings are exchanged at ``https://<host>/api/oauth/token``
for a bearer token (``oauth_token_grants.get_grant_token``, cached, shared
and minted again once on a 401).

The workspace is limited to 20 calls per minute: one shared bucket per
credential paces the calls, and a 429 is retried after its ``Retry-After``
by ``safe_request``. Reads also retry a transient 502/503/504.

The dispatcher (``integration_tools._get_token``) hands the builder the
compound ``region||client_id||client_secret``, the secret last and parsed
greedily so a secret holding ``||`` still round-trips.
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
    parse_compound_token,
    safe_request,
)
from app.integrations.oauth_token_grants import (
    TokenGrantConfig,
    TokenGrantError,
    get_grant_token,
)

PROVIDER = "Aikido Security"
SOURCE = "integration:aikido"

#: Host of each Aikido data region.
REGIONS: dict[str, str] = {
    "eu": "app.aikido.dev",
    "us": "app.us.aikido.dev",
    "au": "app.au.aikido.dev",
    "me": "app.me.aikido.dev",
}
DEFAULT_REGION = "eu"

_TIMEOUT = 60
#: 20 calls per minute per workspace (Aikido "Rate Limiting"): a refill of one
#: call every three seconds, a burst of ten, four calls in flight at most.
_RATE_PER_SECOND = 20.0 / 60.0
_BURST = 10.0
_MAX_CONCURRENCY = 4


@dataclass(frozen=True)
class AikidoCredentials:
    """One integration's credential, as the compound token carries it."""

    region: str
    client_id: str
    client_secret: str

    @property
    def host(self) -> str:
        return REGIONS.get(self.region, REGIONS[DEFAULT_REGION])

    @property
    def base_url(self) -> str:
        return f"https://{self.host}/api/public/v1"

    @property
    def token_url(self) -> str:
        return f"https://{self.host}/api/oauth/token"

    def missing(self) -> list[str]:
        """The parts the credential lacks, named for the integration form."""
        parts = (("client ID", self.client_id), ("client secret", self.client_secret))
        return [label for label, value in parts if not value]


def normalize_region(value: str | None) -> str:
    """A key of :data:`REGIONS`; an unknown value falls back to ``eu``."""
    key = (value or "").strip().lower()
    return key if key in REGIONS else DEFAULT_REGION


def build_aikido_token(region: str, client_id: str, client_secret: str) -> str:
    """The compound token, the one shape ``_get_token`` and the tests build."""
    return "||".join((normalize_region(region), (client_id or "").strip(), (client_secret or "").strip()))


def parse_aikido_token(token: str) -> AikidoCredentials:
    """Split ``region||client_id||client_secret``."""
    region, client_id, client_secret = parse_compound_token(token or "", 3, greedy_last=True)
    return AikidoCredentials(
        region=normalize_region(region),
        client_id=client_id.strip(),
        client_secret=client_secret.strip(),
    )


def token_from_config(secret: str | None, extra: dict[str, Any] | None) -> str:
    """The compound token from what the integration stores.

    The client secret is the API key column; the region and the client id
    live in extra_config. Shared by ``_get_token`` and Test Connection.
    """
    values = extra if isinstance(extra, dict) else {}

    def _text(key: str) -> str:
        value = values.get(key)
        return value.strip() if isinstance(value, str) else ""

    return build_aikido_token(_text("region"), _text("client_id"), secret or "")


def seg(value: Any) -> str:
    """One path segment, encoded."""
    return quote(str(value).strip(), safe="")


@dataclass
class AikidoResponse:
    """A successful answer: its parsed body (or raw bytes) and paging header."""

    data: Any
    content: bytes
    content_type: str
    has_next_page: bool | None


class AikidoClient:
    """Bearer-authenticated, paced requests against one integration's workspace."""

    def __init__(self, creds: AikidoCredentials) -> None:
        self.creds = creds

    def _limiter(self) -> RateLimiter:
        # One bucket per API client; the key is hashed so the registry never
        # holds the secret.
        key = app_token_cache_key("aikido", self.creds.region, self.creds.client_id)
        return get_shared_rate_limiter(
            f"aikido:{key}", rate=_RATE_PER_SECOND, capacity=_BURST, max_concurrency=_MAX_CONCURRENCY
        )

    async def _bearer(self, *, force_refresh: bool) -> tuple[str, str | None]:
        try:
            config = TokenGrantConfig.client_credentials(
                self.creds.token_url, self.creds.client_id, self.creds.client_secret
            )
            return await get_grant_token(config, force_refresh=force_refresh), None
        except TokenGrantError as exc:
            return "", f"Error: {PROVIDER} sign-in failed (client credentials): {exc}"

    async def request(
        self,
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        accept: str = "application/json",
    ) -> tuple[AikidoResponse | None, str | None]:
        """``(response, None)`` or ``(None, "Error: ...")``.

        A 401 mints a new token once (a revoked or rotated bearer); a 429 is
        retried after ``Retry-After``; a GET retries a transient 5xx.
        """
        missing = self.creds.missing()
        if missing:
            return None, (
                f"Error: the {PROVIDER} integration is missing its {', '.join(missing)}. "
                "Edit the integration and set them."
            )
        method = method.upper()
        query = {key: value for key, value in (params or {}).items() if value is not None and value != ""}
        url = f"{self.creds.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(_query_values(query), doseq=True)}"
        body_text = None if body is None else json.dumps(body, separators=(",", ":"))
        for attempt in range(2):
            bearer, err = await self._bearer(force_refresh=attempt > 0)
            if err:
                return None, err
            headers = {"Accept": accept, "User-Agent": PLATFORM_USER_AGENT, "Authorization": f"Bearer {bearer}"}
            extra: dict[str, Any] = {}
            if body_text is not None:
                headers["Content-Type"] = "application/json"
                extra["content"] = body_text
            if attempt == 0:
                extra["expected_statuses"] = {401}
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
            if resp is not None and resp.status_code == 401 and attempt == 0:
                continue
            if err:
                return None, err
            if resp is None:
                return AikidoResponse(None, b"", "", None), None
            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            has_next = _has_next(resp.headers.get("x-has-next-page"))
            if not resp.content:
                return AikidoResponse(None, b"", content_type, has_next), None
            if content_type == "application/json" or content_type.endswith("+json"):
                try:
                    return AikidoResponse(resp.json(), resp.content, content_type, has_next), None
                except ValueError:
                    return None, f"Error: {PROVIDER} answered unreadable JSON while trying to {action}."
            return AikidoResponse(None, resp.content, content_type, has_next), None
        return None, f"Error: {PROVIDER} refused the credential while trying to {action}."


def _has_next(value: str | None) -> bool | None:
    if value is None:
        return None
    return value.strip().lower() in ("true", "1", "yes")


def _query_values(query: dict[str, Any]) -> dict[str, Any]:
    """Booleans as ``true``/``false``; lists as repeated ``key[]`` values."""
    out: dict[str, Any] = {}
    for key, value in query.items():
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            out[f"{key}[]"] = ["true" if v is True else "false" if v is False else v for v in value]
        else:
            out[key] = value
    return out
