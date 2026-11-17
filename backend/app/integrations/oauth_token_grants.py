# SPDX-License-Identifier: Apache-2.0

"""OAuth 2.0 token grants the platform requests on its own, with a process-wide cache.

Two grants need no browser: **client credentials** (RFC 6749 §4.4, machine
to machine: a client id and secret) and the **resource owner password**
grant (§4.3: a user's own username and password, for APIs that still offer
it). This module is the one place those exchanges happen for generic
integrations (OpenAPI integrations whose spec declares such a flow): a token
is fetched once, cached until shortly before it expires, and shared by every
concurrent tool call of the same configuration — concurrent callers coalesce
onto ONE token request instead of each hitting the token endpoint.

Client authentication follows RFC 6749 §2.3.1: with a client secret, HTTP
Basic first (the method every authorization server MUST support), falling
back once to credentials in the form body when the server rejects Basic as
``invalid_client`` — some widely deployed servers only accept the body form.
Whichever method worked is remembered for the cached entry, so the fallback
costs one extra round-trip per process at most. A public client (an id and
no secret, possible with the password grant) sends its id in the body.
:func:`post_with_client_auth` is that exchange on its own, for the other
endpoints a client authenticates to (device authorization).

Security: the token URL is user-supplied (it comes from an imported spec or
the integration's configuration), so it goes through the same destination
guard as the API calls — private networks allowed, loopback, link-local and
cloud-metadata endpoints refused, the connection pinned to the checked
address — and redirects are never followed (a token request carries
secrets). A client certificate configured on the integration is presented to
the endpoint (mutual TLS). Neither a secret, a password nor a token is ever
logged or echoed in an error message.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import math
import ssl
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote_plus

import httpx

from app.core.egress import egress_client
from app.services.web_fetch import guarded_client_options, validate_outbound_url_async

logger = logging.getLogger(__name__)

GRANT_CLIENT_CREDENTIALS = "client_credentials"
GRANT_PASSWORD = "password"  # noqa: S105 - OAuth grant type name, not a secret

#: Renew this long before the advertised expiry, so a token never lapses
#: between the cache check and the API call that uses it.
_EXPIRY_SKEW_SECONDS = 60
#: Lifetime assumed when the server omits ``expires_in`` (it is only
#: RECOMMENDED by RFC 6749): short enough that a revoked token is dropped soon.
_DEFAULT_TTL_SECONDS = 300
#: Longest a token is cached, whatever ``expires_in`` says: a revoked token
#: must not be served for the life of the worker.
_MAX_TTL_SECONDS = 24 * 3600
#: Entries past which expired tokens (and their idle locks) are swept.
_MAX_CACHED_TOKENS = 256
_REQUEST_TIMEOUT_SECONDS = 15
_MAX_ERROR_DETAIL_CHARS = 200

_METHOD_BASIC = "client_secret_basic"
_METHOD_POST = "client_secret_post"
_METHOD_NONE = "none"


class TokenGrantError(Exception):
    """The endpoint could not produce an access token (message is user-safe)."""


@dataclass(frozen=True)
class TokenGrantConfig:
    """Everything one grant request needs."""

    token_url: str
    client_id: str
    client_secret: str = field(default="", repr=False)
    scopes: tuple[str, ...] = ()
    grant_type: str = GRANT_CLIENT_CREDENTIALS
    username: str = ""
    password: str = field(default="", repr=False)
    #: The client certificate to present (mutual TLS), and what identifies it
    #: in the cache key.
    ssl_context: ssl.SSLContext | None = field(default=None, compare=False, repr=False)
    certificate_id: str = ""

    @classmethod
    def client_credentials(
        cls,
        token_url: str | None,
        client_id: str | None,
        client_secret: str | None,
        scopes: str | list[str] | tuple[str, ...] | None = None,
        *,
        ssl_context: ssl.SSLContext | None = None,
        certificate_id: str = "",
    ) -> TokenGrantConfig:
        """A client-credentials config from stored values; raises when a part is missing."""
        _require(
            "OAuth client credentials",
            ("token URL", token_url),
            ("client ID", client_id),
            ("client secret", client_secret),
        )
        return cls(
            token_url=(token_url or "").strip(),
            client_id=(client_id or "").strip(),
            client_secret=(client_secret or "").strip(),
            scopes=_scope_tuple(scopes),
            ssl_context=ssl_context,
            certificate_id=certificate_id,
        )

    @classmethod
    def password_grant(
        cls,
        token_url: str | None,
        credentials: str | None,
        client_id: str | None = None,
        client_secret: str | None = None,
        scopes: str | list[str] | tuple[str, ...] | None = None,
        *,
        ssl_context: ssl.SSLContext | None = None,
        certificate_id: str = "",
    ) -> TokenGrantConfig:
        """A password-grant config; *credentials* is ``username:password``.

        The client id is optional (some servers take the grant without one);
        a client secret without an id is refused.
        """
        username, _, password = (credentials or "").partition(":")
        _require("The OAuth password grant", ("token URL", token_url), ("username", username))
        if (client_secret or "").strip() and not (client_id or "").strip():
            raise TokenGrantError("The OAuth password grant has a client secret but no client ID.")
        return cls(
            token_url=(token_url or "").strip(),
            client_id=(client_id or "").strip(),
            client_secret=(client_secret or "").strip(),
            scopes=_scope_tuple(scopes),
            grant_type=GRANT_PASSWORD,
            username=username.strip(),
            password=password,
            ssl_context=ssl_context,
            certificate_id=certificate_id,
        )

    @property
    def cache_key(self) -> str:
        """Stable key; hashed so no secret sits in a dict key in clear."""
        raw = "\x1f".join(
            (
                self.grant_type,
                self.token_url,
                self.client_id,
                self.client_secret,
                " ".join(self.scopes),
                self.username,
                self.password,
                self.certificate_id,
            )
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _require(what: str, *parts: tuple[str, str | None]) -> None:
    missing = [label for label, value in parts if not (value or "").strip()]
    if missing:
        raise TokenGrantError(f"{what} is incomplete: missing {', '.join(missing)}.")


def _scope_tuple(scopes: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if isinstance(scopes, str):
        return tuple(s for s in scopes.split() if s)
    return tuple(s for s in (scopes or ()) if s)


@dataclass
class _CachedToken:
    token: str
    expires_at: float
    auth_method: str
    fetched_at: float
    #: Renewal margin: the standard skew, but never more than half the
    #: lifetime — a 30-second token would otherwise never count as fresh.
    skew: float = _EXPIRY_SKEW_SECONDS


_CACHE: dict[str, _CachedToken] = {}
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(key: str) -> asyncio.Lock:
    lock = _LOCKS.get(key)
    if lock is None:
        lock = _LOCKS[key] = asyncio.Lock()
    return lock


def _fresh(entry: _CachedToken | None, now: float) -> bool:
    return entry is not None and entry.expires_at - entry.skew > now


async def get_grant_token(
    config: TokenGrantConfig,
    *,
    force_refresh: bool = False,
) -> str:
    """Return a valid access token for *config*, fetching one when needed.

    ``force_refresh`` discards the cached token (the API just rejected it),
    unless another caller already replaced it while this one waited for the
    lock — then that newer token is returned rather than minting a third.
    """
    key = config.cache_key
    requested_at = time.monotonic()
    cached = _CACHE.get(key)
    if not force_refresh and _fresh(cached, requested_at):
        return cached.token  # type: ignore[union-attr]

    async with _lock_for(key):
        cached = _CACHE.get(key)
        now = time.monotonic()
        if (
            cached is not None
            and _fresh(cached, now)
            and (not force_refresh or cached.fetched_at > requested_at)
        ):
            return cached.token
        token, ttl, method = await _request_token(
            config, preferred_method=cached.auth_method if cached else None
        )
        now = time.monotonic()
        _CACHE[key] = _CachedToken(
            token=token,
            expires_at=now + ttl,
            auth_method=method,
            fetched_at=now,
            skew=min(_EXPIRY_SKEW_SECONDS, ttl / 2),
        )
        if len(_CACHE) > _MAX_CACHED_TOKENS:
            _sweep(now)
        return token


def _sweep(now: float) -> None:
    """Drop the expired tokens, and the locks nobody holds for a token not cached.

    Every credential rotation leaves a key behind: without a sweep the cache
    and its locks would only grow, each holding an old token in memory.
    """
    for key in [k for k, entry in _CACHE.items() if entry.expires_at <= now]:
        del _CACHE[key]
    for key in [k for k, lock in _LOCKS.items() if k not in _CACHE and not lock.locked()]:
        del _LOCKS[key]


def _forget(key: str) -> None:
    _CACHE.pop(key, None)
    lock = _LOCKS.get(key)
    if lock is not None and not lock.locked():
        del _LOCKS[key]


def clear_token_cache() -> None:
    """Drop every cached token (tests, secret rotation)."""
    for key in list(_CACHE):
        _forget(key)
    _sweep(time.monotonic())


async def _request_token(
    config: TokenGrantConfig,
    *,
    preferred_method: str | None,
) -> tuple[str, float, str]:
    """POST the grant; returns ``(access_token, ttl_seconds, auth_method)``."""
    data: dict[str, str] = {"grant_type": config.grant_type}
    if config.grant_type == GRANT_PASSWORD:
        data["username"] = config.username
        data["password"] = config.password
    if config.scopes:
        data["scope"] = " ".join(config.scopes)
    resp, method = await post_with_client_auth(
        config.token_url,
        data,
        client_id=config.client_id,
        client_secret=config.client_secret,
        ssl_context=config.ssl_context,
        preferred_method=preferred_method,
    )
    if not resp.is_success:
        raise TokenGrantError(describe_failure(resp))
    return (*parse_token_response(resp), method)


async def post_with_client_auth(
    url: str,
    data: dict[str, str],
    *,
    client_id: str,
    client_secret: str,
    ssl_context: ssl.SSLContext | None = None,
    preferred_method: str | None = None,
    endpoint: str = "token endpoint",
) -> tuple[httpx.Response, str]:
    """POST a form to an OAuth endpoint as the client (see the module docstring).

    Returns the last response — successful or not — and the client
    authentication method it used. Raises :class:`TokenGrantError` when the
    URL is refused or the endpoint cannot be reached.
    """
    try:
        await validate_outbound_url_async(url, allow_private=True)
    except ValueError as exc:
        raise TokenGrantError(f"the {endpoint} URL is not allowed ({exc})") from None

    if not client_secret:
        methods = [_METHOD_NONE]
    elif preferred_method in (_METHOD_BASIC, _METHOD_POST):
        methods = [preferred_method]
    else:
        methods = [_METHOD_BASIC, _METHOD_POST]
    async with egress_client(
        timeout=_REQUEST_TIMEOUT_SECONDS,
        follow_redirects=False,
        **guarded_client_options(allow_private=True, verify=ssl_context or True),
    ) as client:
        for index, method in enumerate(methods):
            body = dict(data)
            headers = {"Accept": "application/json"}
            if method == _METHOD_BASIC:
                # RFC 6749 §2.3.1: both parts are form-urlencoded before Base64.
                userpass = f"{quote_plus(client_id)}:{quote_plus(client_secret)}"
                headers["Authorization"] = "Basic " + base64.b64encode(
                    userpass.encode("utf-8")
                ).decode("ascii")
            elif method == _METHOD_POST:
                body["client_id"] = client_id
                body["client_secret"] = client_secret
            elif client_id:
                body["client_id"] = client_id
            try:
                resp = await client.post(url, data=body, headers=headers)
            except httpx.TimeoutException:
                raise TokenGrantError(f"the {endpoint} timed out") from None
            except httpx.HTTPError as exc:
                raise TokenGrantError(transport_failure(exc, endpoint)) from None

            has_fallback = index + 1 < len(methods)
            if resp.is_success or not has_fallback or not _is_client_auth_rejection(resp):
                return resp, method
            logger.info(
                "OAuth %s rejected %s client authentication (HTTP %s) — retrying with %s",
                endpoint,
                method,
                resp.status_code,
                methods[index + 1],
            )
    raise TokenGrantError(f"the {endpoint} rejected the client credentials")  # pragma: no cover


def transport_failure(exc: Exception, endpoint: str) -> str:
    """Why a request to *endpoint* failed at the transport, without secrets."""
    reason = str(exc)
    if "CERTIFICATE" in reason.upper() or "SSL" in reason.upper() or "TLS" in reason.upper():
        detail = reason.replace("\n", " ").strip()[:_MAX_ERROR_DETAIL_CHARS]
        return f"the TLS handshake with the {endpoint} failed ({detail})"
    return f"the {endpoint} is unreachable ({type(exc).__name__})"


def response_payload(resp: httpx.Response) -> dict[str, Any]:
    """An OAuth endpoint's body as a dict (JSON, or legacy form-encoded)."""
    try:
        payload = resp.json()
        return payload if isinstance(payload, dict) else {}
    except ValueError:
        pass
    if "application/x-www-form-urlencoded" in resp.headers.get("content-type", ""):
        return {k: v[0] for k, v in parse_qs(resp.text).items() if v}
    return {}


def _is_client_auth_rejection(resp: httpx.Response) -> bool:
    """401, or 400 with ``invalid_client`` — the RFC's "wrong client auth" answers."""
    if resp.status_code == 401:
        return True
    return resp.status_code == 400 and response_payload(resp).get("error") == "invalid_client"


def describe_failure(resp: httpx.Response, endpoint: str = "token endpoint") -> str:
    payload = response_payload(resp)
    detail = ""
    code = payload.get("error")
    description = payload.get("error_description")
    if isinstance(code, str) and code:
        detail = code
        if isinstance(description, str) and description:
            detail = f"{code}: {description}"
    detail = detail.replace("\n", " ").strip()[:_MAX_ERROR_DETAIL_CHARS]
    suffix = f" ({detail})" if detail else ""
    return f"the {endpoint} answered HTTP {resp.status_code}{suffix}"


def parse_token_response(resp: httpx.Response) -> tuple[str, float]:
    """``(access_token, ttl_seconds)`` of a successful token response."""
    payload = response_payload(resp)
    token = payload.get("access_token")
    if not isinstance(token, str) or not token.strip():
        raise TokenGrantError("the token endpoint response has no access_token")
    check_bearer(payload.get("token_type"))
    ttl: float = _DEFAULT_TTL_SECONDS
    raw_ttl = payload.get("expires_in")
    try:
        if raw_ttl not in (None, ""):
            parsed = float(raw_ttl)
            ttl = min(max(parsed, 0.0), _MAX_TTL_SECONDS) if math.isfinite(parsed) else _DEFAULT_TTL_SECONDS
    except (TypeError, ValueError, OverflowError):
        ttl = _DEFAULT_TTL_SECONDS
    return token.strip(), ttl


def check_bearer(token_type: Any) -> None:
    """Refuse a token that is not a bearer token (the only kind the tools send)."""
    if isinstance(token_type, str) and token_type and token_type.lower() != "bearer":
        raise TokenGrantError(
            f"the token endpoint issued a '{token_type[:40]}' token; only Bearer tokens are supported"
        )
