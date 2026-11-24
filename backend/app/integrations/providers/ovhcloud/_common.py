"""Shared helpers for the OVHcloud provider package.

OVHcloud exposes one REST API per region (``/1.0``), authenticated in one of
two ways, both supported here:

* ``application_key`` - the classic application key + application secret +
  consumer key. Every request carries ``X-Ovh-Application``,
  ``X-Ovh-Consumer``, ``X-Ovh-Timestamp`` and ``X-Ovh-Signature`` =
  ``$1$`` + SHA1 hex of ``secret+consumer+METHOD+URL+BODY+TIMESTAMP``; the
  timestamp is the API server's clock, read once from ``/auth/time`` and kept
  as an offset per endpoint.
* ``oauth2`` - a service account (client id + client secret) exchanged for a
  bearer token through the client-credentials grant
  (``oauth_token_grants.get_grant_token``, cached and shared).

The dispatcher (``integration_tools._get_token``) hands the builder the
compound ``endpoint||auth_mode||application_key_or_client_id||consumer_key||secret``,
the secret last and parsed greedily.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from app.core.egress import egress_client
from app.integrations.http import (
    PLATFORM_USER_AGENT,
    TRANSIENT_RETRY_STATUSES,
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
from app.tools.base import ToolDefinition

PROVIDER = "OVHcloud"
SOURCE = "integration:ovhcloud"

MODE_APPLICATION_KEY = "application_key"
MODE_OAUTH2 = "oauth2"

#: API base URL per endpoint (the region of the OVHcloud account).
ENDPOINTS: dict[str, str] = {
    "ovh-eu": "https://eu.api.ovh.com/1.0",
    "ovh-ca": "https://ca.api.ovh.com/1.0",
    "ovh-us": "https://api.us.ovhcloud.com/1.0",
}
#: The OAuth2 token endpoint of each region.
TOKEN_URLS: dict[str, str] = {
    "ovh-eu": "https://www.ovh.com/auth/oauth2/token",
    "ovh-ca": "https://ca.ovh.com/auth/oauth2/token",
    "ovh-us": "https://us.ovhcloud.com/auth/oauth2/token",
}
DEFAULT_ENDPOINT = "ovh-eu"

_TIMEOUT = 30
#: How long the server clock offset read from ``/auth/time`` is trusted.
_TIME_DELTA_TTL_SECONDS = 3600.0
#: The longest text a tool answers; the rest is cut with a note.
MAX_ANSWER_CHARS = 24_000
#: Detail reads run in parallel up to this many at a time.
_DETAIL_CONCURRENCY = 5

#: Keys whose value is never answered, whatever the endpoint returns.
_SECRET_KEYS = frozenset(
    {
        "password",
        "secret",
        "privatekey",
        "consumerkey",
        "applicationsecret",
        "clientsecret",
        "token",
        "accesstoken",
        "refreshtoken",
        "kubeconfig",
        "secretkey",
    }
)
REDACTED = "[redacted]"

# endpoint base URL -> (offset in seconds, monotonic time it was read)
_TIME_DELTAS: dict[str, tuple[int, float]] = {}


@dataclass(frozen=True)
class OvhCredentials:
    """One integration's credential, as the compound token carries it."""

    endpoint: str
    mode: str
    key_or_client_id: str
    consumer_key: str
    secret: str

    @property
    def base_url(self) -> str:
        return ENDPOINTS.get(self.endpoint, ENDPOINTS[DEFAULT_ENDPOINT])

    @property
    def token_url(self) -> str:
        return TOKEN_URLS.get(self.endpoint, TOKEN_URLS[DEFAULT_ENDPOINT])

    def missing(self) -> list[str]:
        """The parts this mode needs and the token lacks."""
        if self.mode == MODE_OAUTH2:
            parts = (("client ID", self.key_or_client_id), ("client secret", self.secret))
        else:
            parts = (
                ("application key", self.key_or_client_id),
                ("application secret", self.secret),
                ("consumer key", self.consumer_key),
            )
        return [label for label, value in parts if not value]


def normalize_endpoint(value: str | None) -> str:
    """An endpoint key of :data:`ENDPOINTS`; an unknown value falls back to ``ovh-eu``."""
    key = (value or "").strip().lower()
    return key if key in ENDPOINTS else DEFAULT_ENDPOINT


def normalize_mode(value: str | None) -> str:
    key = (value or "").strip().lower()
    return MODE_OAUTH2 if key == MODE_OAUTH2 else MODE_APPLICATION_KEY


def parse_ovhcloud_token(token: str) -> OvhCredentials:
    """Split ``endpoint||auth_mode||key_or_client_id||consumer_key||secret``."""
    endpoint, mode, key_or_client_id, consumer_key, secret = parse_compound_token(token or "", 5, greedy_last=True)
    return OvhCredentials(
        endpoint=normalize_endpoint(endpoint),
        mode=normalize_mode(mode),
        key_or_client_id=key_or_client_id.strip(),
        consumer_key=consumer_key.strip(),
        secret=secret.strip(),
    )


def build_ovhcloud_token(endpoint: str, mode: str, key_or_client_id: str, consumer_key: str, secret: str) -> str:
    """The compound token, the one shape ``_get_token`` and the tests build."""
    return "||".join(
        (
            normalize_endpoint(endpoint),
            normalize_mode(mode),
            (key_or_client_id or "").strip(),
            (consumer_key or "").strip(),
            (secret or "").strip(),
        )
    )


def token_from_config(secret: str | None, extra: dict[str, Any] | None) -> str:
    """The compound token from what the integration stores.

    The secret (application secret or OAuth2 client secret) is the API key
    column; the endpoint, the mode, the application key or client id and the
    consumer key live in extra_config. Shared by ``_get_token`` and Test
    Connection so both read one shape.
    """
    values = extra if isinstance(extra, dict) else {}

    def _text(key: str) -> str:
        value = values.get(key)
        return value.strip() if isinstance(value, str) else ""

    mode = normalize_mode(_text("auth_mode"))
    key_or_client_id = _text("client_id") if mode == MODE_OAUTH2 else _text("application_key")
    consumer_key = "" if mode == MODE_OAUTH2 else _text("consumer_key")
    return build_ovhcloud_token(_text("endpoint"), mode, key_or_client_id, consumer_key, secret or "")


def compute_signature(
    application_secret: str,
    consumer_key: str,
    method: str,
    url: str,
    body: str,
    timestamp: int,
) -> str:
    """The ``X-Ovh-Signature`` of one request."""
    raw = "+".join((application_secret, consumer_key, method.upper(), url, body, str(timestamp)))
    return "$1$" + hashlib.sha1(raw.encode("utf-8")).hexdigest()  # noqa: S324 - the API's own scheme


def seg(value: Any) -> str:
    """One path segment, encoded (an IP block's ``/`` included)."""
    return quote(str(value).strip(), safe="")


def clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def redact(data: Any) -> Any:
    """*data* with every secret-named value replaced."""
    if isinstance(data, dict):
        return {
            key: (REDACTED if str(key).replace("_", "").lower() in _SECRET_KEYS else redact(value))
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [redact(item) for item in data]
    return data


def render(title: str, data: Any) -> str:
    """A tool answer: a title line, then the redacted data as compact JSON, bounded."""
    body = json.dumps(redact(data), ensure_ascii=False, default=str, separators=(",", ":"))
    if len(body) > MAX_ANSWER_CHARS:
        body = (
            body[:MAX_ANSWER_CHARS] + f"... [cut at {MAX_ANSWER_CHARS} of {len(body)} characters: narrow the call "
            "with its filters, limit or offset]"
        )
    return f"{title}\n{body}"


def page_note(total: int, offset: int, shown: int) -> str:
    if offset + shown < total:
        return f" ({shown} shown from offset {offset}; next offset {offset + shown})"
    return f" ({shown} shown from offset {offset})" if offset else ""


def clear_time_deltas() -> None:
    """Forget every cached server clock offset (tests)."""
    _TIME_DELTAS.clear()


Clock = Callable[[], float]


class OvhClient:
    """Signed or bearer requests against one integration's OVHcloud API."""

    def __init__(self, creds: OvhCredentials, *, clock: Clock = time.time) -> None:
        self.creds = creds
        self._clock = clock

    def _limiter(self):
        # One bucket per account credential; the key is hashed so the
        # registry never holds the secret.
        key = app_token_cache_key("ovhcloud", self.creds.endpoint, self.creds.key_or_client_id, self.creds.consumer_key)
        return get_shared_rate_limiter(f"ovhcloud:{key}", rate=10.0, capacity=20.0, max_concurrency=8)

    async def time_delta(self) -> tuple[int | None, str | None]:
        """Server time minus local time, read from ``/auth/time`` and cached."""
        base = self.creds.base_url
        cached = _TIME_DELTAS.get(base)
        if cached is not None and time.monotonic() - cached[1] < _TIME_DELTA_TTL_SECONDS:
            return cached[0], None
        async with egress_client(timeout=_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{base}/auth/time",
                PROVIDER,
                "read the API server time",
                headers={"Accept": "application/json", "User-Agent": PLATFORM_USER_AGENT},
                retry_statuses=TRANSIENT_RETRY_STATUSES,
            )
        if err or resp is None:
            return None, err or f"Error: {PROVIDER} did not answer its server time."
        try:
            server_time = int(resp.json())
        except (TypeError, ValueError):
            return None, f"Error: {PROVIDER} answered an unreadable server time."
        delta = server_time - int(self._clock())
        _TIME_DELTAS[base] = (delta, time.monotonic())
        return delta, None

    async def _auth_headers(
        self, method: str, url: str, body: str, *, force_refresh: bool = False
    ) -> tuple[dict[str, str], str | None]:
        creds = self.creds
        if creds.mode == MODE_OAUTH2:
            try:
                config = TokenGrantConfig.client_credentials(
                    creds.token_url, creds.key_or_client_id, creds.secret, "all"
                )
                bearer = await get_grant_token(config, force_refresh=force_refresh)
            except TokenGrantError as exc:
                return {}, f"Error: {PROVIDER} OAuth2 sign-in failed: {exc}"
            return {"Authorization": f"Bearer {bearer}"}, None
        delta, err = await self.time_delta()
        if err or delta is None:
            return {}, err
        timestamp = int(self._clock()) + delta
        return {
            "X-Ovh-Application": creds.key_or_client_id,
            "X-Ovh-Consumer": creds.consumer_key,
            "X-Ovh-Timestamp": str(timestamp),
            "X-Ovh-Signature": compute_signature(creds.secret, creds.consumer_key, method, url, body, timestamp),
        }, None

    async def request(
        self,
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        body: Any = None,
    ) -> tuple[Any, str | None]:
        """``(json, None)`` or ``(None, "Error: ...")``; GETs retry transient 5xx."""
        missing = self.creds.missing()
        if missing:
            return None, (
                f"Error: the {PROVIDER} integration is missing its {', '.join(missing)}. "
                "Edit the integration and set them."
            )
        method = method.upper()
        query = {k: v for k, v in (params or {}).items() if v not in (None, "")}
        url = f"{self.creds.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query, doseq=True)}"
        body_text = "" if body is None else json.dumps(body, separators=(",", ":"))
        is_read = method == "GET"
        oauth = self.creds.mode == MODE_OAUTH2
        for attempt in range(2 if oauth else 1):
            auth, err = await self._auth_headers(method, url, body_text, force_refresh=attempt > 0)
            if err:
                return None, err
            headers = {"Accept": "application/json", "User-Agent": PLATFORM_USER_AGENT, **auth}
            extra: dict[str, Any] = {}
            if body_text:
                headers["Content-Type"] = "application/json"
                extra["content"] = body_text
            if oauth and attempt == 0:
                # A revoked or rotated bearer answers 401: mint a new one once.
                extra["expected_statuses"] = {401}
            async with self._limiter().slot(), egress_client(timeout=_TIMEOUT) as client:
                resp, err = await safe_request(
                    client,
                    method.lower(),
                    url,
                    PROVIDER,
                    action,
                    headers=headers,
                    retry_statuses=TRANSIENT_RETRY_STATUSES if is_read else None,
                    **extra,
                )
            if resp is not None and resp.status_code == 401 and oauth and attempt == 0:
                continue
            if err:
                return None, err
            if resp is None or not resp.content:
                return None, None
            try:
                return resp.json(), None
            except ValueError:
                return None, f"Error: {PROVIDER} answered a non-JSON response while trying to {action}."
        return None, f"Error: {PROVIDER} refused the credential while trying to {action}."

    async def get(self, path: str, action: str, params: dict[str, Any] | None = None):
        return await self.request("GET", path, action, params=params)

    async def details(
        self,
        ids: Iterable[Any],
        path_for: Callable[[Any], str],
        action: str,
    ) -> list[dict[str, Any]]:
        """The detail of each id, read in parallel; a failed read keeps its error."""
        semaphore = asyncio.Semaphore(_DETAIL_CONCURRENCY)

        async def _one(item: Any) -> dict[str, Any]:
            async with semaphore:
                data, err = await self.get(path_for(item), f"{action} {item}")
            if err:
                return {"id": item, "error": err}
            return data if isinstance(data, dict) else {"id": item, "value": data}

        return list(await asyncio.gather(*(_one(item) for item in ids)))


async def list_with_details(
    client: OvhClient,
    list_path: str,
    path_for: Callable[[Any], str],
    action: str,
    *,
    limit: int,
    offset: int,
    params: dict[str, Any] | None = None,
) -> tuple[list[Any] | None, list[dict[str, Any]], str | None]:
    """``(all ids, details of one page, error)`` for an id-listing endpoint."""
    ids, err = await client.get(list_path, f"list {action}", params)
    if err:
        return None, [], err
    ids = list(ids or [])
    page = ids[offset : offset + limit]
    return ids, await client.details(page, path_for, f"read {action}"), None


Handler = Callable[..., Awaitable[str]]


def tool(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
    *,
    priority: int = 1,
    always_ask: bool = False,
) -> ToolDefinition:
    schema: dict[str, Any] = {"type": "object", "properties": properties or {}}
    if required:
        schema["required"] = required
    return ToolDefinition(
        name=name,
        description=description,
        input_schema=schema,
        source=SOURCE,
        priority=priority,
        always_ask=always_ask,
    )


LIMIT = {"type": "integer", "description": "Items to read in detail (default 25, max 100)."}
OFFSET = {"type": "integer", "description": "First item of the page (default 0)."}
PROJECT = {"type": "string", "description": "Public Cloud project id (from list_ovhcloud_cloud_projects)."}
REGION = {"type": "string", "description": "Region code, e.g. GRA11 or BHS5 (optional filter)."}
