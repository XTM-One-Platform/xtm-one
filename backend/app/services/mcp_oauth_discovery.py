# SPDX-License-Identifier: Apache-2.0

"""Outbound MCP OAuth: metadata discovery + RFC 7591 dynamic client registration.

This is the **client** side of MCP OAuth — how this platform connects to a
*remote* MCP server that requires OAuth.  (Its mirror image, where the platform
*is* the authorization server for inbound MCP clients, lives in
``app.api.v1.mcp_oauth``.)

The MCP authorization spec (2025-06-18) is explicitly designed so a client can
onboard with **no pre-provisioned credentials**: it discovers the authorization
server from the resource, registers itself dynamically to obtain a ``client_id``,
then runs Authorization Code + PKCE in the user's browser.  That is what this
module implements, so configuring a remote MCP server needs only its URL — or,
for servers that publish no metadata, just the Authorize + Token URLs — never a
``client_id`` / ``client_secret`` pair pasted from a provider console.

The discovery chain, in order.  Every step is optional and individually fallible;
whatever an operator configured by hand always wins over what is discovered:

1. **RFC 9728** Protected Resource Metadata — ``/.well-known/oauth-protected-resource``
   on the MCP server.  Yields the canonical ``resource`` identifier (used as the
   RFC 8707 ``resource`` parameter, which the MCP spec requires) and the
   ``authorization_servers`` list.
2. **RFC 8414 / OIDC Discovery** — ``/.well-known/oauth-authorization-server``
   then ``/.well-known/openid-configuration`` on the authorization server.
   Yields ``authorization_endpoint``, ``token_endpoint`` and — the part that
   makes credential-free onboarding possible — ``registration_endpoint``.
3. **RFC 7591** Dynamic Client Registration — ``POST`` to that registration
   endpoint to mint a ``client_id`` for this platform.

Every endpoint that comes out of discovery is scheme-checked before it is used
(:func:`is_safe_endpoint`): HTTPS everywhere, plain HTTP only for loopback hosts.
Every discovery and registration request is held to the MCP server URL policy
on the address it connects to (``app.tools.mcp_destinations.guarded_client``):
the documents come from hosts the server named, which are no more trusted than
the server URL itself.
Metadata is attacker-influenced input — a server that answers the well-known
probe chooses the URLs we then send the user's browser and our token requests to,
so an ``http://`` downgrade must never survive discovery.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from app.core.config import settings
from app.core.utils import format_exception_message
from app.tools.mcp_destinations import guarded_client

logger = logging.getLogger(__name__)

# Discovery probes several well-known URLs in sequence and most of them 404, so
# the per-request timeout is kept short: the whole chain must stay inside the
# patience of a user who just clicked "Connect".
DISCOVERY_TIMEOUT_SECONDS = 8.0
# Ceiling on the whole chain. Up to ~18 candidate URLs can be probed (three URL
# shapes x two metadata suffixes x three candidate bases); against a host that
# black-holes packets rather than refusing them, each one costs the full
# per-request timeout, so without an overall budget a single click could block
# for minutes. Once the budget is spent, remaining probes are skipped and
# whatever was found so far is returned.
DISCOVERY_BUDGET_SECONDS = 20.0
# Registration is a single write request against a live authorization server and
# may legitimately be slower (some issue and persist a client record).
REGISTRATION_TIMEOUT_SECONDS = 20.0

# Metadata documents are small JSON objects. Reading an unbounded body from an
# arbitrary host would let a hostile (or merely broken) server stream the worker
# out of memory, so the response is read in chunks and abandoned past this size.
_MAX_METADATA_BYTES = 256 * 1024

_PROTECTED_RESOURCE_SUFFIX = "oauth-protected-resource"
_AUTHORIZATION_SERVER_SUFFIX = "oauth-authorization-server"
_OPENID_SUFFIX = "openid-configuration"

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class MCPOAuthDiscoveryError(Exception):
    """Discovery or dynamic registration could not produce a usable client.

    Carries an operator-facing message: it is surfaced verbatim in the API
    response for ``GET /mcp-servers/{id}/authorize``, because the person who
    configured the server is the only one who can fix a bad URL.
    """


@dataclass(slots=True)
class OAuthEndpoints:
    """Everything needed to run the authorization-code flow against a server."""

    authorize_url: str | None = None
    token_url: str | None = None
    registration_url: str | None = None
    #: RFC 8707 canonical resource identifier, sent as ``resource`` on the
    #: authorize and token requests. The MCP spec mandates it so an access token
    #: minted for one MCP server cannot be replayed against another.
    resource: str | None = None
    #: Space-separated scopes advertised by the server (``scopes_supported``).
    scopes: str | None = None
    issuer: str | None = None
    #: The authorization server whose token endpoint the flow uses lists
    #: ``offline_access`` in its ``scopes_supported``: the scope Okta,
    #: Entra ID or Auth0 require before they issue a refresh token, which an
    #: MCP server does not advertise itself (MCP 2026-07-28, "Refresh Tokens").
    offline_access_supported: bool = False
    #: True when at least one well-known document answered — i.e. the endpoints
    #: below are the server's own, not a fallback guess.
    discovered: bool = False

    @property
    def is_complete(self) -> bool:
        """Whether the pair required to run the flow is known."""
        return bool(self.authorize_url and self.token_url)


@dataclass(frozen=True, slots=True)
class MCPOAuthClientSetup:
    """What :func:`ensure_mcp_oauth_client` changed, for the audit trail."""

    #: A client was registered dynamically (RFC 7591).
    registered: bool = False
    #: ``offline_access`` was added to the scopes.
    offline_access_added: bool = False


@dataclass(slots=True)
class DynamicClient:
    """The credentials an authorization server issued to this platform."""

    client_id: str
    #: ``None`` for a public client — the common case. Public clients
    #: authenticate the token request with PKCE alone, and sending an empty
    #: ``client_secret`` to a server that issued none is an authentication
    #: attempt that OAuth 2.1 servers reject, so absence must stay absent.
    client_secret: str | None = None
    registration_client_uri: str | None = None
    scope: str | None = None


# ── URL helpers ──────────────────────────────────────────────────────────────


def oauth_redirect_uri() -> str:
    """The single callback URI this platform registers and redirects to."""
    return f"{settings.base_url.rstrip('/')}/api/oauth/callback"


def is_safe_endpoint(url: str) -> bool:
    """Whether ``url`` may be used as an OAuth endpoint.

    HTTPS for anything routable; plain HTTP only for loopback, so a developer
    can point at an MCP server running on their own machine. Anything else —
    ``http://`` to a real host, ``file://``, a scheme-less string — is rejected
    rather than downgraded, because these URLs come from a remote metadata
    document and are used to carry authorization codes and tokens.
    """
    parsed = urlparse(url)
    if parsed.scheme == "https":
        return bool(parsed.netloc)
    if parsed.scheme == "http":
        return (parsed.hostname or "") in _LOOPBACK_HOSTS
    return False


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


def _well_known_candidates(url: str, suffix: str) -> list[str]:
    """Well-known URLs to probe for ``url``, most specific first.

    Three shapes are tried because the ecosystem uses all three:

    * ``https://host/.well-known/<suffix>/tenant`` — RFC 8414 §3.1 and RFC 9728
      §3.1 insert the well-known segment *before* the issuer/resource path, so a
      multi-tenant host can serve one document per tenant.
    * ``https://host/tenant/.well-known/<suffix>`` — OIDC Discovery 1.0 appends
      instead, and many OIDC-derived servers only answer this shape.
    * ``https://host/.well-known/<suffix>`` — the root document, which is what a
      single-tenant server (and every MCP server whose URL is just a path on a
      dedicated host) publishes.
    """
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return []

    origin = urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))
    path = parsed.path.rstrip("/")

    candidates = []
    if path:
        candidates.append(f"{origin}/.well-known/{suffix}{path}")
        candidates.append(f"{origin}{path}/.well-known/{suffix}")
    candidates.append(f"{origin}/.well-known/{suffix}")

    # Preserve order while dropping the duplicates a path-less URL produces.
    seen: set[str] = set()
    return [c for c in candidates if not (c in seen or seen.add(c))]


def _first_str(value: Any) -> str | None:
    """First non-empty string of a metadata value that may be a str or a list."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


#: The OpenID Connect scope that asks for a refresh token.
OFFLINE_ACCESS_SCOPE = "offline_access"


def _join_scopes(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        scopes = [s.strip() for s in value if isinstance(s, str) and s.strip()]
        if scopes:
            return " ".join(scopes)
    return None


# ── Metadata fetching ────────────────────────────────────────────────────────


class _Prober:
    """Bounded well-known probe over a single discovery run.

    Holds the HTTP client, a wall-clock budget, and the set of URLs already
    tried. Both bounds matter: the candidate bases overlap in practice (an MCP
    server that is its own authorization server produces the same URLs twice),
    and a black-holing host makes every probe cost the full timeout.
    """

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._deadline = time.monotonic() + DISCOVERY_BUDGET_SECONDS
        self._tried: set[str] = set()

    async def fetch(self, url: str) -> dict | None:
        """GET a well-known document, returning ``None`` for anything unusable.

        Discovery walks candidate URLs of which most are expected to 404, so
        *every* failure mode here — connection error, non-2xx, non-JSON body,
        oversized body — is a soft "not this one, try the next" rather than an
        error worth propagating.
        """
        if not is_safe_endpoint(url) or url in self._tried:
            return None
        if time.monotonic() >= self._deadline:
            logger.warning("OAuth discovery budget exhausted before probing %s", url)
            return None
        self._tried.add(url)
        try:
            # Streamed and length-checked as it arrives, not after: the body
            # comes from a host the caller named, and a buffered read would let
            # an endless response exhaust the worker's memory before any size
            # check could run.
            body = bytearray()
            async with self._http.stream(
                "GET", url, headers={"Accept": "application/json"}
            ) as resp:
                if resp.status_code != 200:
                    return None
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_METADATA_BYTES:
                        logger.warning(
                            "OAuth metadata at %s exceeds the size limit", url
                        )
                        return None
            data = json.loads(body)
        except Exception as exc:
            logger.debug(
                "OAuth metadata probe failed for %s: %s",
                url,
                format_exception_message(exc),
            )
            return None
        return data if isinstance(data, dict) else None

    async def probe(self, url: str, suffix: str) -> tuple[dict, str] | None:
        """Return the first well-known document that answers, with its URL."""
        for candidate in _well_known_candidates(url, suffix):
            data = await self.fetch(candidate)
            if data is not None:
                return data, candidate
        return None

    async def authorization_server(self, base: str) -> dict | None:
        """Fetch RFC 8414 metadata for ``base``, falling back to OIDC discovery."""
        for suffix in (_AUTHORIZATION_SERVER_SUFFIX, _OPENID_SUFFIX):
            found = await self.probe(base, suffix)
            if found:
                data, url = found
                logger.info(
                    "Discovered OAuth authorization server metadata at %s", url
                )
                return data
        return None


async def discover_oauth_endpoints(
    *,
    server_url: str | None = None,
    authorize_url: str | None = None,
    token_url: str | None = None,
    registration_url: str | None = None,
    resource: str | None = None,
    metadata_url: str | None = None,
    http: httpx.AsyncClient | None = None,
) -> OAuthEndpoints:
    """Resolve the OAuth endpoints for a remote MCP server.

    Runs the RFC 9728 → RFC 8414 chain from ``server_url`` and merges the result
    with whatever the operator configured by hand.  **Operator-configured values
    always win**: someone who typed an Authorize URL did so because the
    discovered one (or the absence of one) was wrong for their deployment.

    ``metadata_url`` is the protected-resource metadata URL a ``401`` challenge
    named (RFC 9728 §5.1). It is fetched *before* any well-known guess because
    the spec lets it live on a host or path the guesses would never reach — for
    such a server, following the challenge is the only discovery that works.

    Never raises for a server that simply publishes nothing — the caller decides
    whether the resulting :class:`OAuthEndpoints` is usable, because "authorize
    and token are known but registration is not" is a perfectly workable state
    for a server whose ``client_id`` was configured by hand.
    """
    result = OAuthEndpoints(
        authorize_url=authorize_url or None,
        token_url=token_url or None,
        registration_url=registration_url or None,
        resource=resource or None,
    )

    own_client = http is None
    client = http or guarded_client(
        timeout=DISCOVERY_TIMEOUT_SECONDS, follow_redirects=True
    )
    prober = _Prober(client)
    try:
        # 1. RFC 9728 — ask the resource who its authorization server is.
        #    The challenge-supplied URL is authoritative, so it is fetched
        #    directly; only if it is absent or unusable do we fall back to
        #    guessing well-known paths from the MCP server URL.
        as_base: str | None = None
        found: tuple[dict, str] | None = None
        if metadata_url:
            data = await prober.fetch(metadata_url)
            if data is not None:
                found = (data, metadata_url)
        if found is None and server_url and is_safe_endpoint(server_url):
            found = await prober.probe(server_url, _PROTECTED_RESOURCE_SUFFIX)
        if found:
            prm, url = found
            logger.info("Discovered MCP protected-resource metadata at %s", url)
            result.discovered = True
            as_base = _first_str(prm.get("authorization_servers"))
            if not result.resource:
                # The document's own ``resource`` value is authoritative for
                # the RFC 8707 parameter; falling back to the URL we probed
                # keeps a token audience-bound even for a terse document.
                #
                # Deliberately set *only* on this branch. A server that
                # publishes protected-resource metadata is following the MCP
                # authorization spec and therefore understands ``resource``;
                # a plain OAuth 2.0 provider reached through the fallback
                # paths below may answer an unknown ``resource`` with
                # ``invalid_target`` and break a flow that works today.
                result.resource = _first_str(prm.get("resource")) or server_url
            if not result.scopes:
                result.scopes = _join_scopes(prm.get("scopes_supported"))

        # 2. RFC 8414 / OIDC — the authorization server's own endpoint list.
        #    Probed at the AS the resource named, else at the MCP server itself
        #    (small deployments are their own AS), else at the origin of a
        #    hand-configured authorize URL — which is the only lead available
        #    when an operator supplied Authorize + Token URLs and nothing else.
        #    The first document found fills the blanks. ``offline_access`` is
        #    said of the authorization server whose token endpoint the flow
        #    uses: when the first document names another one (a token
        #    URL typed by hand overriding the AS the resource named), the
        #    remaining candidates - the typed token URL's origin among them -
        #    are read for that one alone, merging nothing else.
        candidates = list(
            dict.fromkeys(
                base
                for base in (
                    as_base,
                    server_url,
                    _origin(result.authorize_url) if result.authorize_url else None,
                    _origin(result.token_url) if result.token_url else None,
                )
                if base
            )
        )
        merged = False
        for candidate_base in candidates:
            if not is_safe_endpoint(candidate_base):
                continue
            asm = await prober.authorization_server(candidate_base)
            if not asm:
                continue
            result.discovered = True
            if not merged:
                merged = True
                result.issuer = result.issuer or _first_str(asm.get("issuer"))
                result.authorize_url = result.authorize_url or _first_str(
                    asm.get("authorization_endpoint")
                )
                result.token_url = result.token_url or _first_str(
                    asm.get("token_endpoint")
                )
                result.registration_url = result.registration_url or _first_str(
                    asm.get("registration_endpoint")
                )
                if not result.scopes:
                    result.scopes = _join_scopes(asm.get("scopes_supported"))
            if _first_str(asm.get("token_endpoint")) == result.token_url:
                result.offline_access_supported = OFFLINE_ACCESS_SCOPE in (
                    _join_scopes(asm.get("scopes_supported")) or ""
                ).split()
                break
    finally:
        if own_client:
            await client.aclose()

    # Discovery is attacker-influenced: drop anything that came back pointing at
    # a non-HTTPS host rather than carrying a code or token over it.
    for field_name in ("authorize_url", "token_url", "registration_url"):
        value = getattr(result, field_name)
        if value and not is_safe_endpoint(value):
            logger.warning(
                "Discarding unsafe discovered OAuth %s: %s", field_name, value
            )
            setattr(result, field_name, None)

    return result


async def find_protected_resource_metadata(
    server_url: str, *, http: httpx.AsyncClient | None = None
) -> str | None:
    """Return the URL of the server's RFC 9728 metadata, or ``None``.

    A resource that publishes this document is declaring itself OAuth-protected.
    That is the signal behind "connected, but authorization required": many MCP
    servers answer ``initialize`` and ``tools/list`` to anyone and gate only
    ``tools/call``, so a connectivity test alone reports a cheerful success for
    a server that will reject every real call.
    """
    if not server_url or not is_safe_endpoint(server_url):
        return None

    own_client = http is None
    client = http or guarded_client(
        timeout=DISCOVERY_TIMEOUT_SECONDS, follow_redirects=True
    )
    try:
        found = await _Prober(client).probe(server_url, _PROTECTED_RESOURCE_SUFFIX)
    finally:
        if own_client:
            await client.aclose()
    return found[1] if found else None


def fallback_registration_url(endpoints: OAuthEndpoints) -> str | None:
    """Last-resort guess at a registration endpoint, or ``None``.

    Some MCP servers implement ``POST /register`` without publishing any
    metadata at all.  When an operator gave us Authorize + Token URLs and
    discovery found no ``registration_endpoint``, this convention is the only
    remaining way to obtain a ``client_id`` automatically — so it is tried, and
    a 404 simply surfaces as "this server needs a client ID configured by hand".
    Deliberately derived from an endpoint we were *already* going to talk to, so
    it can never widen the set of hosts this platform contacts.
    """
    base = endpoints.authorize_url or endpoints.token_url
    if not base:
        return None
    candidate = f"{_origin(base)}/register"
    return candidate if is_safe_endpoint(candidate) else None


# ── RFC 7591 Dynamic Client Registration ─────────────────────────────────────


async def register_dynamic_client(
    registration_url: str,
    *,
    client_name: str,
    redirect_uri: str,
    scopes: str | None = None,
    http: httpx.AsyncClient | None = None,
) -> DynamicClient:
    """Register this platform as an OAuth client (RFC 7591).

    Registers as a **public** client (``token_endpoint_auth_method: none``):
    the platform runs the flow in the user's browser with PKCE, and a secret
    would add nothing an authorization server could rely on.  Servers that
    refuse a public client are retried once with the auth method omitted so the
    server picks its own default and hands back a ``client_secret`` — which is
    then stored and used, encrypted, like a hand-configured one.

    Raises :class:`MCPOAuthDiscoveryError` with an operator-facing message.
    """
    if not is_safe_endpoint(registration_url):
        raise MCPOAuthDiscoveryError(
            "The OAuth registration endpoint must use HTTPS "
            f"(got: {registration_url})"
        )

    base_body: dict[str, Any] = {
        "client_name": client_name,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "application_type": "web",
    }
    if scopes:
        base_body["scope"] = scopes

    attempts: list[dict[str, Any]] = [
        {**base_body, "token_endpoint_auth_method": "none"},
        # Retry without the field: a server that mandates a confidential client
        # rejects ``none`` outright, and RFC 7591 lets it choose the default.
        base_body,
    ]

    own_client = http is None
    client = http or guarded_client(
        timeout=REGISTRATION_TIMEOUT_SECONDS, follow_redirects=True
    )
    last_error = ""
    try:
        for index, body in enumerate(attempts):
            try:
                resp = await client.post(
                    registration_url,
                    json=body,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
            except Exception as exc:
                raise MCPOAuthDiscoveryError(
                    "Could not reach the OAuth registration endpoint "
                    f"({registration_url}): {format_exception_message(exc)}"
                ) from exc

            if resp.status_code in (200, 201):
                try:
                    data = resp.json()
                except Exception as exc:
                    raise MCPOAuthDiscoveryError(
                        "The OAuth registration endpoint returned a non-JSON "
                        "response"
                    ) from exc
                client_id = _first_str(
                    data.get("client_id") if isinstance(data, dict) else None
                )
                if not client_id:
                    raise MCPOAuthDiscoveryError(
                        "The OAuth registration endpoint returned no client_id"
                    )
                secret = (
                    _first_str(data.get("client_secret"))
                    if isinstance(data, dict)
                    else None
                )
                logger.info(
                    "Dynamically registered OAuth client %s at %s",
                    client_id,
                    registration_url,
                )
                return DynamicClient(
                    client_id=client_id,
                    client_secret=secret,
                    registration_client_uri=_first_str(
                        data.get("registration_client_uri")
                    ),
                    scope=_first_str(data.get("scope")),
                )

            # Truncated: this text reaches the operator in an API error, and an
            # authorization server's error body is not a trusted payload.
            last_error = (resp.text or "").strip()[:200]
            retryable = resp.status_code == 400 and index + 1 < len(attempts)
            if not retryable:
                break
    finally:
        if own_client:
            await client.aclose()

    raise MCPOAuthDiscoveryError(
        "Dynamic client registration was refused by "
        f"{registration_url}: {last_error or 'no details returned'}"
    )


# ── PKCE ─────────────────────────────────────────────────────────────────────


async def ensure_mcp_oauth_client(db, srv) -> MCPOAuthClientSetup:
    """Give ``srv`` everything the authorization-code flow needs, persisting it.

    Called right before the browser is sent to the authorization server. Fills
    in whatever is missing — authorize / token / registration URLs, the RFC 8707
    resource identifier, advertised scopes — and, when no ``client_id`` was ever
    provisioned, registers this platform dynamically to mint one. A server that
    already has a ``client_id`` and both endpoints is left untouched, apart
    from ``offline_access`` (see :func:`_scopes_with_offline_access`).

    The scopes are settled before a client is registered, so the client is
    registered with the scopes the authorization will request (an
    authorization server may refuse a scope its client was not registered
    with), and from the one discovery that fills the endpoints in.

    No database transaction is held across the outbound requests (discovery:
    up to ``DISCOVERY_BUDGET_SECONDS``; registration): the one value read from
    the database, the client name a registration sends, is read first, then
    the connection is handed back before any request leaves. ``srv`` is only
    changed in memory until the final commit, so a failure stores nothing.

    Returns what changed: whether a new client was registered and whether
    ``offline_access`` was added.
    Raises :class:`MCPOAuthDiscoveryError` with an operator-facing message when
    the server cannot be onboarded automatically.
    """
    from datetime import UTC, datetime

    from app.core.database import release_db_connection_before_loop
    from app.core.redis import redis_client
    from app.core.security import encrypt_value
    from app.core.security_options import security_option
    from app.integrations.token_endpoint import (
        TokenEndpointRefusedError,
        assert_token_endpoint_allowed,
    )
    from app.services.branding import get_branding

    # Read before any change to ``srv``: a query would flush those changes and
    # hold the server row locked across the registration request.
    client_name = (
        (await get_branding(db)).name
        if not srv.client_id_enc and security_option("mcp_outbound_dcr_enabled")
        else None
    )
    await release_db_connection_before_loop(db)

    if srv.client_id_enc and srv.authorize_url and srv.token_url:
        scopes = await _scopes_with_offline_access(srv)
        if scopes is None:
            return MCPOAuthClientSetup()
        srv.scopes = scopes
        await db.commit()
        return MCPOAuthClientSetup(offline_access_added=True)

    endpoints = await discover_oauth_endpoints(
        server_url=srv.url,
        authorize_url=srv.authorize_url,
        token_url=srv.token_url,
        registration_url=srv.registration_url,
        resource=srv.oauth_resource,
        # Learned from a 401 challenge on a previous connection attempt, and
        # authoritative when present (RFC 9728 §5.1).
        metadata_url=srv.oauth_metadata_url,
    )

    # Only ever fill blanks — a value an operator typed in stays as typed.
    srv.authorize_url = srv.authorize_url or endpoints.authorize_url
    srv.token_url = srv.token_url or endpoints.token_url
    srv.registration_url = srv.registration_url or endpoints.registration_url
    srv.oauth_resource = srv.oauth_resource or endpoints.resource
    srv.scopes = srv.scopes or endpoints.scopes

    if not srv.authorize_url or not srv.token_url:
        raise MCPOAuthDiscoveryError(
            "Could not determine the OAuth endpoints for this server. It "
            "publishes no OAuth metadata, so enter the Authorize URL and Token "
            "URL in the server configuration."
        )

    # A discovered token URL comes from a remote metadata document. Held to the
    # token endpoint policy here, before a client is registered and the
    # user is sent to consent, not only at the code exchange that follows.
    try:
        await assert_token_endpoint_allowed(srv.token_url)
    except TokenEndpointRefusedError as exc:
        raise MCPOAuthDiscoveryError(str(exc)) from exc

    scopes = await _scopes_with_offline_access(srv, endpoints)
    added = scopes is not None
    if added:
        srv.scopes = scopes

    if srv.client_id_enc:
        await db.commit()
        return MCPOAuthClientSetup(offline_access_added=added)

    if client_name is None or not security_option("mcp_outbound_dcr_enabled"):
        raise MCPOAuthDiscoveryError(
            "Automatic OAuth client registration is disabled on this platform. "
            "Configure a client ID for this server."
        )

    registration_url = srv.registration_url or fallback_registration_url(endpoints)
    if not registration_url:
        raise MCPOAuthDiscoveryError(
            "This server does not advertise a dynamic client registration "
            "endpoint. Configure a client ID, or enter the registration URL."
        )

    # Serialize registration per server: a double-clicked "Connect" would
    # otherwise mint two clients, and the second would overwrite the client_id
    # the first authorization request is already carrying in the browser.
    redis = await redis_client()
    lock_key = f"mcp_dcr_register:{srv.id}"
    if not await redis.set(lock_key, "1", ex=30, nx=True):
        raise MCPOAuthDiscoveryError(
            "An OAuth client registration is already in progress for this "
            "server. Try again in a moment."
        )

    try:
        client = await register_dynamic_client(
            registration_url,
            client_name=client_name,
            redirect_uri=oauth_redirect_uri(),
            scopes=srv.scopes,
        )
    finally:
        await redis.delete(lock_key)

    srv.client_id_enc = encrypt_value(client.client_id)
    srv.client_secret_enc = (
        encrypt_value(client.client_secret) if client.client_secret else None
    )
    srv.registration_url = registration_url
    srv.dcr_registered_at = datetime.now(UTC)
    if client.scope and not srv.scopes:
        srv.scopes = client.scope
    await db.commit()
    return MCPOAuthClientSetup(registered=True, offline_access_added=added)


async def _scopes_with_offline_access(srv, endpoints: OAuthEndpoints | None = None) -> str | None:
    """Ask for a refresh token where the authorization server issues one on request.

    Okta, Entra ID and Auth0 issue a refresh token only to a client that asked
    for ``offline_access``, and an MCP server does not advertise that scope
    itself, so a server whose scopes came from its protected-resource metadata
    (or were typed by hand) never got one: with a 10-minute access token there
    was nothing to refresh. The MCP authorization spec (2026-07-28,
    "Refresh Tokens") lets a client add it to the authorization and token
    requests when the authorization server lists it in ``scopes_supported``.

    Adds ``offline_access`` to the stored scopes - which both requests send -
    when that authorization server lists it and the scopes name others. With no
    scopes at all the request asks for the server's defaults, and a scope
    parameter holding ``offline_access`` alone could narrow them. A client this
    platform registered dynamically without it keeps the scopes it was
    registered with, since an authorization server enforcing the registered
    scope would refuse the authorization; clearing its client ID registers a new
    one that carries it. ``endpoints`` is the discovery the caller already ran;
    without one, a discovery runs here, best effort: a failure leaves the scopes
    as they are. Returns the scopes to store, ``None`` when they stay as they
    are; the caller stores them and commits.
    """
    scopes = (srv.scopes or "").split()
    if not scopes or OFFLINE_ACCESS_SCOPE in scopes:
        return None
    if srv.client_id_enc and srv.dcr_registered_at is not None:
        return None
    if endpoints is None:
        try:
            endpoints = await discover_oauth_endpoints(
                server_url=srv.url,
                authorize_url=srv.authorize_url,
                token_url=srv.token_url,
                registration_url=srv.registration_url,
                resource=srv.oauth_resource,
                metadata_url=srv.oauth_metadata_url,
            )
        except Exception as exc:
            logger.warning(
                "Could not read the authorization server metadata of MCP server %s: %s",
                srv.id,
                format_exception_message(exc),
            )
            return None
    if not endpoints.offline_access_supported:
        return None
    return " ".join([*scopes, OFFLINE_ACCESS_SCOPE])


async def forget_dynamic_client(db, srv) -> bool:
    """Drop a dynamically registered client so the next attempt re-registers.

    Dynamically registered clients are disposable by design, and authorization
    servers do dispose of them — they expire, get garbage-collected, or are
    evicted when a registration table is bounded (this platform's own inbound
    registry does exactly that). The correct response to ``invalid_client`` is
    therefore to register again, not to ask the operator to fix something.

    A hand-configured client is never touched: forgetting it would silently
    discard credentials only the operator can restore.
    """
    if not srv.dcr_registered_at:
        return False
    srv.client_id_enc = None
    srv.client_secret_enc = None
    srv.dcr_registered_at = None
    srv.oauth_status = "disconnected"
    await db.commit()
    logger.info(
        "Discarded the dynamically registered OAuth client for MCP server %s; "
        "it will be re-registered on the next authorization",
        srv.id,
    )
    return True


def generate_pkce_pair() -> tuple[str, str]:
    """Return a ``(code_verifier, code_challenge)`` pair for S256 PKCE.

    OAuth 2.1 (and therefore MCP) makes PKCE mandatory, and S256 is the only
    method it keeps.  The verifier is 43-128 unreserved characters per RFC 7636
    §4.1; ``token_urlsafe(64)`` yields ~86.
    """
    verifier = secrets.token_urlsafe(64)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    return verifier, challenge
