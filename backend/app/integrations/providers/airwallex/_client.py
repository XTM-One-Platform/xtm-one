# SPDX-License-Identifier: Apache-2.0

"""Airwallex REST client: credential, environment host, bearer, pacing.

Airwallex exposes one REST API per environment (``https://api.airwallex.com``
in production, ``https://api-demo.airwallex.com`` in the sandbox). A client
id and an API key, created by an account admin in Settings > Developer >
API keys, are exchanged at ``POST /api/v1/authentication/login`` (headers
``x-client-id`` and ``x-api-key``) for a bearer token valid 30 minutes. The
token is cached under a hash of the credential (``app_token_cache_key``) and
reused until shortly before its ``expires_at``; a 401 mints a new one once.

Several Airwallex accounts (one per legal entity) are reached two ways:

* one integration per account, the usual setup: each holds its own key;
* an organisation-level key scoped to several accounts: ``login_as`` (the
  ``x-login-as`` header of the login call) picks the account the token acts
  on, and a platform key reaches a connected account per call with
  ``x-on-behalf-of`` (the ``on_behalf_of`` argument every tool takes).

Reads retry a transient 5xx; every call is paced by one shared bucket per
credential and a 429 is retried after its ``Retry-After`` by
``safe_request``. Writes are never retried: they carry an idempotency
``request_id`` instead.

The dispatcher (``integration_tools._get_token``) hands the builder the
compound ``environment||client_id||login_as||api_key``, the
key last and parsed greedily so a key holding ``||`` still round-trips.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

from app.core.egress import egress_client
from app.integrations.http import (
    PLATFORM_USER_AGENT,
    TRANSIENT_RETRY_STATUSES,
    RateLimiter,
    app_token_cache_key,
    get_cached_app_token,
    get_shared_rate_limiter,
    parse_compound_token,
    safe_request,
    set_cached_app_token,
)

PROVIDER = "Airwallex"
SOURCE = "integration:airwallex"

#: API host of each Airwallex environment.
ENVIRONMENTS: dict[str, str] = {
    "production": "api.airwallex.com",
    "demo": "api-demo.airwallex.com",
}
DEFAULT_ENVIRONMENT = "production"

_TIMEOUT = 60
#: Airwallex documents a per-account limit well above an agent's pace; a
#: bucket of 10 calls a second (burst 20, 5 in flight) keeps a report loop
#: from tripping it.
_RATE_PER_SECOND = 10.0
_BURST = 20.0
_MAX_CONCURRENCY = 5
#: Token lifetime when ``expires_at`` cannot be read (Airwallex: 30 minutes).
_DEFAULT_TOKEN_SECONDS = 1800


@dataclass(frozen=True)
class AirwallexCredentials:
    """One integration's credential, as the compound token carries it."""

    environment: str
    client_id: str
    api_key: str
    login_as: str = ""

    @property
    def host(self) -> str:
        return ENVIRONMENTS.get(self.environment, ENVIRONMENTS[DEFAULT_ENVIRONMENT])

    @property
    def base_url(self) -> str:
        return f"https://{self.host}"

    def missing(self) -> list[str]:
        """The parts the credential lacks, named for the integration form."""
        parts = (("client ID", self.client_id), ("API key", self.api_key))
        return [label for label, value in parts if not value]


def normalize_environment(value: str | None) -> str:
    """A key of :data:`ENVIRONMENTS`; an unknown value is production."""
    key = (value or "").strip().lower()
    if key in ("sandbox", "test"):
        return "demo"
    return key if key in ENVIRONMENTS else DEFAULT_ENVIRONMENT


def build_airwallex_token(
    environment: str,
    client_id: str,
    api_key: str,
    *,
    login_as: str = "",
) -> str:
    """The compound token, the one shape ``_get_token`` and the tests build."""
    return "||".join(
        (
            normalize_environment(environment),
            (client_id or "").strip(),
            (login_as or "").strip(),
            (api_key or "").strip(),
        )
    )


def parse_airwallex_token(token: str) -> AirwallexCredentials:
    """Split ``environment||client_id||login_as||api_key``."""
    environment, client_id, login_as, api_key = parse_compound_token(token or "", 4, greedy_last=True)
    return AirwallexCredentials(
        environment=normalize_environment(environment),
        client_id=client_id.strip(),
        api_key=api_key.strip(),
        login_as=login_as.strip(),
    )


def token_from_config(secret: str | None, extra: dict[str, Any] | None) -> str:
    """The compound token from what the integration stores.

    The API key is the API key column; the environment, the client id, the
    account to log in as live in extra_config.
    Shared by ``_get_token`` and Test Connection.
    """
    values = extra if isinstance(extra, dict) else {}

    def _text(key: str) -> str:
        value = values.get(key)
        return value.strip() if isinstance(value, str) else ""

    return build_airwallex_token(
        _text("environment"),
        _text("client_id"),
        secret or "",
        login_as=_text("login_as"),
    )


def seg(value: Any) -> str:
    """One path segment, encoded."""
    return quote(str(value).strip(), safe="")


def _token_lifetime(expires_at: Any) -> int:
    """Seconds until ``expires_at`` (``2026-11-21T23:10:00+0000``), else 30 minutes."""
    if not isinstance(expires_at, str) or not expires_at.strip():
        return _DEFAULT_TOKEN_SECONDS
    text = expires_at.strip().replace("Z", "+00:00")
    if len(text) > 5 and text[-5] in "+-" and text[-3] != ":":
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return _DEFAULT_TOKEN_SECONDS
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return int((when - datetime.now(UTC)).total_seconds())


@dataclass
class AirwallexResponse:
    """A successful answer: its parsed body, or its raw bytes for a file."""

    data: Any
    content: bytes
    content_type: str


class AirwallexClient:
    """Bearer-authenticated, paced requests against one integration's account."""

    def __init__(self, creds: AirwallexCredentials) -> None:
        self.creds = creds

    def _cache_key(self) -> str:
        # Hashed: neither the cache nor the limiter registry holds the key.
        return app_token_cache_key(
            "airwallex", self.creds.environment, self.creds.client_id, self.creds.api_key, self.creds.login_as
        )

    def _limiter(self) -> RateLimiter:
        return get_shared_rate_limiter(
            f"airwallex:{self._cache_key()}",
            rate=_RATE_PER_SECOND,
            capacity=_BURST,
            max_concurrency=_MAX_CONCURRENCY,
        )

    async def _bearer(self, *, force_refresh: bool) -> tuple[str, str | None]:
        key = self._cache_key()
        if not force_refresh:
            cached = get_cached_app_token(key)
            if cached:
                return cached, None
        headers = {
            "Accept": "application/json",
            "User-Agent": PLATFORM_USER_AGENT,
            "x-client-id": self.creds.client_id,
            "x-api-key": self.creds.api_key,
        }
        if self.creds.login_as:
            headers["x-login-as"] = self.creds.login_as
        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{self.creds.base_url}/api/v1/authentication/login",
                PROVIDER,
                "sign in with the client ID and API key",
                headers=headers,
            )
        if err or resp is None:
            # safe_request never echoes request headers, so the key stays out.
            return "", f"Error: {PROVIDER} sign-in failed: {(err or 'no answer').removeprefix('Error: ')}"
        try:
            body = resp.json()
        except ValueError:
            return "", f"Error: {PROVIDER} sign-in answered unreadable JSON."
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            return "", f"Error: {PROVIDER} sign-in answered no token."
        set_cached_app_token(key, token, _token_lifetime(body.get("expires_at")))
        return token, None

    async def request(
        self,
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        on_behalf_of: str | None = None,
        accept: str = "application/json",
    ) -> tuple[AirwallexResponse | None, str | None]:
        """``(response, None)`` or ``(None, "Error: ...")``.

        A 401 mints a new token once (a rotated key or an expired bearer); a
        429 is retried after ``Retry-After``; a GET retries a transient 5xx.
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
            if on_behalf_of:
                headers["x-on-behalf-of"] = str(on_behalf_of).strip()
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
                return AirwallexResponse(None, b"", ""), None
            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            if not resp.content:
                return AirwallexResponse(None, b"", content_type), None
            if content_type == "application/json" or content_type.endswith("+json"):
                try:
                    return AirwallexResponse(resp.json(), resp.content, content_type), None
                except ValueError:
                    return None, f"Error: {PROVIDER} answered unreadable JSON while trying to {action}."
            return AirwallexResponse(None, resp.content, content_type), None
        return None, f"Error: {PROVIDER} refused the credential while trying to {action}."


def _query_values(query: dict[str, Any]) -> dict[str, Any]:
    """Booleans as ``true``/``false``; lists as comma-separated values."""
    out: dict[str, Any] = {}
    for key, value in query.items():
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            out[key] = ",".join(str(v) for v in value)
        else:
            out[key] = value
    return out
