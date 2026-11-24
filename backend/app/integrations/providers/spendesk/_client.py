# SPDX-License-Identifier: Apache-2.0

"""Spendesk Public API client: credential, environment, auth, entity, rate limit.

Spendesk exposes one public REST API (``https://public-api.spendesk.com``,
sandbox ``https://public-api.demo.spendesk.com``), authenticated by a bearer
minted at ``POST /v1/auth/token`` from the client id and secret of a Public API
credential (Basic auth, ``grant_type=client_credentials``; cached and minted
again once on a 401 by ``oauth_token_grants.get_grant_token``).

Multi-entity: an organisation-level credential reaches every company
(entity) of the organisation. ``GET /v2/companies`` lists them; every v1 call
names its company in the ``X-Company-Id`` header and the company-scoped v2
calls carry it in the path. A company-level credential reaches its own company
only and needs no entity.

Limits: 1,000 requests per minute per company and credential; a 429 is retried
after its ``Retry-After`` by ``safe_request``, reads also retry a transient
502/503/504. Writes carry an ``Idempotency-Key`` when the caller gives none.

The dispatcher (``integration_tools._get_token``) hands the builder the
compound ``environment||client_id||client_secret``, the secret last and parsed
greedily so a secret holding ``||`` still round-trips.
"""

from __future__ import annotations

import json
import uuid
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

PROVIDER = "Spendesk"
SOURCE = "integration:spendesk"

#: API host of each Spendesk environment.
ENVIRONMENTS: dict[str, str] = {
    "production": "public-api.spendesk.com",
    "sandbox": "public-api.demo.spendesk.com",
}
DEFAULT_ENVIRONMENT = "production"
COMPANY_HEADER = "X-Company-Id"

_TIMEOUT = 60
#: 1,000 calls per minute per company and credential ("Rate Limiting"): a
#: refill of about 16 calls a second, a burst of 50, 16 calls in flight.
_RATE_PER_SECOND = 1000.0 / 60.0
_BURST = 50.0
_MAX_CONCURRENCY = 16


@dataclass(frozen=True)
class SpendeskCredentials:
    """One integration's credential, as the compound token carries it."""

    environment: str
    client_id: str
    client_secret: str

    @property
    def host(self) -> str:
        return ENVIRONMENTS.get(self.environment, ENVIRONMENTS[DEFAULT_ENVIRONMENT])

    @property
    def base_url(self) -> str:
        return f"https://{self.host}"

    @property
    def token_url(self) -> str:
        return f"https://{self.host}/v1/auth/token"

    def missing(self) -> list[str]:
        """The parts the credential lacks, named for the integration form."""
        parts = (("client ID", self.client_id), ("client secret", self.client_secret))
        return [label for label, value in parts if not value]


def normalize_environment(value: str | None) -> str:
    """A key of :data:`ENVIRONMENTS`; an unknown value falls back to production."""
    key = (value or "").strip().lower()
    return key if key in ENVIRONMENTS else DEFAULT_ENVIRONMENT


def build_spendesk_token(environment: str, client_id: str, client_secret: str) -> str:
    """The compound token, the one shape ``_get_token`` and the tests build."""
    return "||".join((normalize_environment(environment), (client_id or "").strip(), (client_secret or "").strip()))


def parse_spendesk_token(token: str) -> SpendeskCredentials:
    """Split ``environment||client_id||client_secret``."""
    environment, client_id, client_secret = parse_compound_token(token or "", 3, greedy_last=True)
    return SpendeskCredentials(
        environment=normalize_environment(environment),
        client_id=client_id.strip(),
        client_secret=client_secret.strip(),
    )


def token_from_config(secret: str | None, extra: dict[str, Any] | None) -> str:
    """The compound token from what the integration stores.

    The client secret is the API key column; the environment and the client
    id live in extra_config. Shared by ``_get_token`` and Test Connection.
    """
    values = extra if isinstance(extra, dict) else {}

    def _text(key: str) -> str:
        value = values.get(key)
        return value.strip() if isinstance(value, str) else ""

    return build_spendesk_token(_text("environment"), _text("client_id"), secret or "")


def seg(value: Any) -> str:
    """One path segment, encoded."""
    return quote(str(value).strip(), safe="")


@dataclass
class SpendeskResponse:
    """A successful answer: its parsed body (or raw bytes) and content type."""

    data: Any
    content: bytes
    content_type: str


class SpendeskClient:
    """Bearer-authenticated, paced requests against one integration's organisation."""

    def __init__(self, creds: SpendeskCredentials) -> None:
        self.creds = creds

    def _limiter(self, company_id: str | None) -> RateLimiter:
        # One bucket per credential and company (the limit is per both); the
        # key is hashed so the registry never holds the secret.
        key = app_token_cache_key("spendesk", self.creds.environment, self.creds.client_id, company_id or "")
        return get_shared_rate_limiter(
            f"spendesk:{key}", rate=_RATE_PER_SECOND, capacity=_BURST, max_concurrency=_MAX_CONCURRENCY
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
        company_id: str | None = None,
        idempotency_key: str | None = None,
        accept: str = "application/json",
    ) -> tuple[SpendeskResponse | None, str | None]:
        """``(response, None)`` or ``(None, "Error: ...")``.

        A 401 mints a new token once (a revoked or rotated bearer); a 429 is
        retried after ``Retry-After``; a GET retries a transient 5xx. A write
        sends one ``Idempotency-Key`` for both attempts, so a retry never
        applies it twice.
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
        if method != "GET" and not idempotency_key:
            idempotency_key = str(uuid.uuid4())
        for attempt in range(2):
            bearer, err = await self._bearer(force_refresh=attempt > 0)
            if err:
                return None, err
            headers = {"Accept": accept, "User-Agent": PLATFORM_USER_AGENT, "Authorization": f"Bearer {bearer}"}
            if company_id:
                headers[COMPANY_HEADER] = str(company_id).strip()
            if idempotency_key and method != "GET":
                headers["Idempotency-Key"] = idempotency_key
            extra: dict[str, Any] = {}
            if body_text is not None:
                headers["Content-Type"] = "application/json"
                extra["content"] = body_text
            if attempt == 0:
                extra["expected_statuses"] = {401}
            async with self._limiter(company_id).slot(), egress_client(timeout=_TIMEOUT) as client:
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
                return SpendeskResponse(None, b"", ""), None
            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            if not resp.content:
                return SpendeskResponse(None, b"", content_type), None
            if content_type == "application/json" or content_type.endswith("+json"):
                try:
                    return SpendeskResponse(resp.json(), resp.content, content_type), None
                except ValueError:
                    return None, f"Error: {PROVIDER} answered unreadable JSON while trying to {action}."
            return SpendeskResponse(None, resp.content, content_type), None
        return None, f"Error: {PROVIDER} refused the credential while trying to {action}."


def _query_values(query: dict[str, Any]) -> dict[str, Any]:
    """Booleans as ``true``/``false``; lists as repeated values."""
    out: dict[str, Any] = {}
    for key, value in query.items():
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            out[key] = ["true" if v is True else "false" if v is False else v for v in value]
        else:
            out[key] = value
    return out
