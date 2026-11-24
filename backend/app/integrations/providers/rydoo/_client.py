# SPDX-License-Identifier: Apache-2.0

"""Rydoo REST client: credential, bearer, pacing, multipart uploads.

Rydoo exposes one public REST API (``https://api.rydoo.com``). An API
client (client id + client secret) requested from Rydoo for the company is
exchanged at ``https://accounts.rydoo.com/connect/token`` (OAuth2 client
credentials, scope ``api_xpd2``) for a bearer. Rydoo asks integrators to
reuse a token rather than mint one per call (the token endpoint allows 25
requests a minute), so it is cached under a hash of the credential for at
most five minutes and minted again once on a 401.

Rate limits (``rate-limiting`` page): 500 requests a minute per company, 150
on the groups endpoints. One shared bucket per credential paces every call
under that; a 429 is retried after its ``Retry-After`` by ``safe_request``.
Reads retry a transient 5xx; writes are never retried, and the e-invoice
upload carries an ``Idempotency-Key``.

The dispatcher (``integration_tools._get_token``) hands the builder the
compound ``client_id||client_secret``, the secret last and parsed greedily
so a secret holding ``||`` still round-trips.
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
    get_cached_app_token,
    get_shared_rate_limiter,
    parse_compound_token,
    safe_request,
    set_cached_app_token,
)

PROVIDER = "Rydoo"
SOURCE = "integration:rydoo"
API_BASE = "https://api.rydoo.com"
SIGN_IN_URL = "https://accounts.rydoo.com/connect/token"
SCOPE = "api_xpd2"

_TIMEOUT = 60
#: 500 requests a minute per company: a bucket of 6 a second (burst 12, 4 in
#: flight) stays under it with room for a second agent.
_RATE_PER_SECOND = 6.0
_BURST = 12.0
_MAX_CONCURRENCY = 4
#: Rydoo: reuse a token, refresh it at most every five minutes.
_TOKEN_SECONDS = 300


@dataclass(frozen=True)
class RydooCredentials:
    """One integration's credential, as the compound token carries it."""

    client_id: str
    client_secret: str

    def missing(self) -> list[str]:
        """The parts the credential lacks, named for the integration form."""
        parts = (("client ID", self.client_id), ("client secret", self.client_secret))
        return [label for label, value in parts if not value]


def build_rydoo_token(client_id: str, client_secret: str) -> str:
    """The compound token, the one shape ``_get_token`` and the tests build."""
    return "||".join(((client_id or "").strip(), (client_secret or "").strip()))


def parse_rydoo_token(token: str) -> RydooCredentials:
    """Split ``client_id||client_secret``."""
    client_id, client_secret = parse_compound_token(token or "", 2, greedy_last=True)
    return RydooCredentials(client_id=client_id.strip(), client_secret=client_secret.strip())


def token_from_config(secret: str | None, extra: dict[str, Any] | None) -> str:
    """The compound token from what the integration stores.

    The client secret is the API key column; the client id lives in
    extra_config. Shared by ``_get_token`` and Test Connection.
    """
    values = extra if isinstance(extra, dict) else {}
    client_id = values.get("client_id")
    return build_rydoo_token(client_id.strip() if isinstance(client_id, str) else "", secret or "")


def seg(value: Any) -> str:
    """One path segment, encoded."""
    return quote(str(value).strip(), safe="")


@dataclass
class RydooResponse:
    """A successful answer: its parsed body, or its raw bytes for a file."""

    data: Any
    content: bytes
    content_type: str


@dataclass(frozen=True)
class Upload:
    """A multipart part: the form field, the file name, its bytes and type."""

    field: str
    filename: str
    data: bytes
    content_type: str


class RydooClient:
    """Bearer-authenticated, paced requests against one company."""

    def __init__(self, creds: RydooCredentials) -> None:
        self.creds = creds

    def _cache_key(self) -> str:
        # Hashed: neither the cache nor the limiter registry holds the secret.
        return app_token_cache_key("rydoo", self.creds.client_id, self.creds.client_secret)

    def _limiter(self) -> RateLimiter:
        return get_shared_rate_limiter(
            f"rydoo:{self._cache_key()}",
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
        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "post",
                SIGN_IN_URL,
                PROVIDER,
                "sign in with the client ID and secret",
                headers={
                    "Accept": "application/json",
                    "User-Agent": PLATFORM_USER_AGENT,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                content=urlencode(
                    {
                        "grant_type": "client_credentials",
                        "client_id": self.creds.client_id,
                        "client_secret": self.creds.client_secret,
                        "scope": SCOPE,
                    }
                ),
            )
        if err or resp is None:
            # safe_request never echoes the request body, so the secret stays out.
            return "", f"Error: {PROVIDER} sign-in failed: {(err or 'no answer').removeprefix('Error: ')}"
        try:
            body = resp.json()
        except ValueError:
            return "", f"Error: {PROVIDER} sign-in answered unreadable JSON."
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            return "", f"Error: {PROVIDER} sign-in answered no access token."
        lifetime = body.get("expires_in") if isinstance(body, dict) else None
        seconds = lifetime if isinstance(lifetime, int) and not isinstance(lifetime, bool) else _TOKEN_SECONDS
        set_cached_app_token(key, token, max(30, min(seconds - 30, _TOKEN_SECONDS)))
        return token, None

    async def request(
        self,
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
        files: list[Upload] | None = None,
        form: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        accept: str = "application/json",
    ) -> tuple[RydooResponse | None, str | None]:
        """``(response, None)`` or ``(None, "Error: ...")``.

        A 401 mints a new token once (a rotated secret or an expired bearer);
        a 429 is retried after ``Retry-After``; a GET retries a transient 5xx.
        """
        missing = self.creds.missing()
        if missing:
            return None, (
                f"Error: the {PROVIDER} integration is missing its {', '.join(missing)}. "
                "Edit the integration and set them."
            )
        method = method.upper()
        query = {key: value for key, value in (params or {}).items() if value is not None and value != ""}
        url = f"{API_BASE}{path}"
        if query:
            url = f"{url}?{urlencode(_query_values(query), doseq=True)}"
        body_text = None if body is None else json.dumps(body, separators=(",", ":"))
        for attempt in range(2):
            bearer, err = await self._bearer(force_refresh=attempt > 0)
            if err:
                return None, err
            send = {"Accept": accept, "User-Agent": PLATFORM_USER_AGENT, "Authorization": f"Bearer {bearer}"}
            send.update(headers or {})
            extra: dict[str, Any] = {}
            if files:
                extra["files"] = [(f.field, (f.filename, f.data, f.content_type)) for f in files]
                if form:
                    extra["data"] = form
            elif body_text is not None:
                send["Content-Type"] = "application/json"
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
                    headers=send,
                    retry_statuses=TRANSIENT_RETRY_STATUSES if method == "GET" else None,
                    **extra,
                )
            if resp is not None and resp.status_code == 401 and attempt == 0:
                continue
            if err:
                return None, err
            if resp is None:
                return RydooResponse(None, b"", ""), None
            content_type = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            if not resp.content:
                return RydooResponse(None, b"", content_type), None
            if content_type == "application/json" or content_type.endswith("+json"):
                try:
                    return RydooResponse(resp.json(), resp.content, content_type), None
                except ValueError:
                    return None, f"Error: {PROVIDER} answered unreadable JSON while trying to {action}."
            return RydooResponse(None, resp.content, content_type), None
        return None, f"Error: {PROVIDER} refused the credential while trying to {action}."


def _query_values(query: dict[str, Any]) -> dict[str, Any]:
    """Booleans as ``true``/``false``; lists repeated (``branchIds=a&branchIds=b``)."""
    out: dict[str, Any] = {}
    for key, value in query.items():
        if isinstance(value, bool):
            out[key] = "true" if value else "false"
        elif isinstance(value, (list, tuple)):
            out[key] = [str(v) for v in value]
        else:
            out[key] = value
    return out
