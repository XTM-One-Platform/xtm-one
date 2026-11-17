# SPDX-License-Identifier: Apache-2.0

"""The platform's OAuth 2.1 authorization server.

Named for MCP, which was its first and is still its main consumer, but it is
resource-neutral: it owns the endpoints, client authentication and the token
response envelope, and nothing about any particular resource.

A feature that needs its own grant registers one through
:func:`register_grant_handler` rather than being special-cased in the dispatch,
and keeps its own token vocabulary in its own package — the A2A partner grant
lives in ``app.a2a.grants``, its claims in ``app.a2a.tokens``. That is why this
module imports nothing from the features it serves: the dependency points one
way, and a second resource costs no edit here.

Implements, for the MCP specification (revision 2025-06-18 and later):

- **RFC 9728** – Protected Resource Metadata (``/.well-known/oauth-protected-resource``)
- **RFC 8414** – Authorization Server Metadata (``/.well-known/oauth-authorization-server``)
- **RFC 7591** – Dynamic Client Registration (``POST /mcp/oauth/register``)
- **Client ID Metadata Documents** (MCP 2025-11-25) – an HTTPS ``client_id`` names
  the document describing the client (``app/services/mcp_oauth_clients.py``)
- **OAuth 2.1** – Authorization Code + PKCE (authorize / token / revoke)
- **RFC 9207** – the issuer (``iss``) on every authorization response (MCP
  2026-07-28), against mix-up attacks

Registered grants are advertised in the server metadata alongside these.

Auth codes are ephemeral (Redis, 5-min TTL).  Access tokens are JWTs.
Refresh tokens are opaque and stored in Redis (30-day TTL).
"""

from __future__ import annotations

import hashlib
import html as html_module
import json
import logging
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import quote, urlencode, urlparse, urlunparse

import jwt
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    INSECURE_TRANSPORT_DETAIL,
    credential_transport_route,
    refuses_credential_transport,
)
from app.core.config import settings
from app.core.csp import (
    GOOGLE_FONTS_SOURCES,
    PagePolicy,
    add_response_sources,
    origin_source,
    register_page_policy,
    script_hash_source,
)
from app.core.database import get_db
from app.core.rate_limit import enforce_rate_limit
from app.core.redis import redis_client
from app.core.security import bcrypt_checkpw_async, bcrypt_hashpw_async
from app.core.xtm_auth import XTM_JWKS_PATH, sign_platform_jwt
from app.models.oauth_client import OAuthClient
from app.models.user import User, is_platform_admin
from app.services.audit import log_audit
from app.services.branding import get_branding, get_cached_branding
from app.services.mcp_oauth_clients import (
    cached_refusal,
    document_host,
    is_metadata_document_url,
    redirect_uri_allowed,
    redirect_uri_matches,
    resolve_client_metadata,
)
from app.services.platform_sign_in_clients import PLATFORM_CLIENT_PREFIX, is_active_platform_client

logger = logging.getLogger(__name__)

router = APIRouter()
well_known_router = APIRouter()

# ── Constants ────────────────────────────────────────────────────────────────

ACCESS_TOKEN_EXPIRE_MINUTES = 60
REFRESH_TOKEN_EXPIRE_DAYS = 30
#: How long the refresh tokens of one authorization of a metadata-document
#: client may keep rotating. Such a client has no row to delete, so
#: its token family ends here at the latest; the user authorizes again.
DOCUMENT_CLIENT_REFRESH_FAMILY_DAYS = 90
AUTH_CODE_TTL_SECONDS = 300  # 5 minutes
REDIS_PREFIX_CODE = "mcp_oauth_code:"
REDIS_PREFIX_REFRESH = "mcp_oauth_refresh:"

MCP_OAUTH_TOKEN_TYPE = "mcp_oauth"  # noqa: S105
# Internal token kind identifier, not a credential. It must stay distinct from
# every other token type the platform mints, because the surfaces that consume
# tokens tell them apart by this value alone -- a registered grant contributing
# a colliding type would make its tokens usable on this resource server.

# PKCE method the token endpoint verifies (see ``_handle_authorization_code``).
# OAuth 2.1 and the MCP spec mandate S256 and drop ``plain``, and the
# authorization-server metadata advertises ``["S256"]`` only — so we accept
# S256 exclusively, keeping the constant, the grant guard, the token verifier,
# and the discovery metadata consistent. Used to reject a grant for an
# unsupported method *before* a code is minted, so we never issue a code that
# can never be exchanged at ``/mcp/oauth/token``.
SUPPORTED_CODE_CHALLENGE_METHODS = ("S256",)

# OpenID Connect on the same server: a client asking for the ``openid`` scope
# also gets an ID token (EdDSA, the platform key of ``/xtm/auth/jwks``) beside
# the access token, so another product can sign its users in through this
# platform. Without ``openid`` nothing changes.
#: ``email`` and ``profile`` are OpenID Connect Core 5.4 (``profile`` also carries
#: ``xtm_title``, the person's title); ``xtm_role`` asks for the platform role
#: (``admin`` / ``user``) and ``xtm_super_admin`` (the ``ADMIN_EMAIL`` account),
#: claims of this platform.
OIDC_SCOPES_SUPPORTED = ("openid", "email", "profile", "xtm_role")
#: Not for a self-registered client: dynamic registration is open to anyone, so
#: a client registered that way gets no OpenID Connect scope at all. A product's
#: sign-in client comes from platform registration instead.
TRUSTED_CLIENT_SCOPES = OIDC_SCOPES_SUPPORTED
ID_TOKEN_EXPIRE_MINUTES = 10
MAX_SCOPE_LENGTH = 500
MAX_NONCE_LENGTH = 255

# RFC 7591 / the MCP spec require Dynamic Client Registration to stay
# unauthenticated (clients self-register before any user session exists),
# so abuse resistance comes from per-IP throttling
# (``settings.mcp_dcr_rate_limit`` per hour) + a bounded client table
# (``settings.mcp_dcr_max_clients``) instead of a login gate.
DCR_RATE_WINDOW_SECONDS = 3600  # per hour, per client IP

# Stable bigint key for the transaction-scoped Postgres advisory lock that
# serializes DCR registrations (``b"mcp_dcr"`` big-endian). Any constant
# would do; deriving it from the feature name keeps it collision-resistant
# against other advisory-lock users and self-documenting in ``pg_locks``.
DCR_REGISTRATION_LOCK_KEY = int.from_bytes(b"mcp_dcr", "big")


# ── Helpers ──────────────────────────────────────────────────────────────────


def _base_url() -> str:
    """Canonical base URL (no trailing slash)."""
    return settings.base_url.rstrip("/")


def _issuer_path_segments() -> str:
    """The issuer's path component without its slashes (``xtm-one``), or ``""``.

    Under a URL path prefix (``BASE_PATH``) the issuer is
    ``https://host/xtm-one`` and RFC 8414 / RFC 9728 put the well-known segment
    BEFORE that path: a spec-following client asks the HOST ROOT for
    ``/.well-known/oauth-authorization-server/xtm-one`` and
    ``/.well-known/oauth-protected-resource/xtm-one/mcp/user``. The reverse
    proxy only routes ``/xtm-one/...`` to the platform, so the operator adds
    one rule forwarding ``/.well-known/`` as well - and the two handlers below
    then read the request whichever of the two RFC shapes it took.
    """
    return settings.base_path.strip("/")


def _strip_issuer_path(path: str) -> str:
    """``path`` with the issuer's path component removed when it opens with it.

    ``xtm-one/mcp/user`` -> ``mcp/user`` and ``xtm-one`` -> ``""`` under
    ``BASE_PATH=/xtm-one`` (the RFC 9728 path-insertion form); ``mcp/user`` is
    returned as is (the form the ``WWW-Authenticate`` challenge names, under
    the prefix). At the root nothing is stripped.
    """
    issuer_path = _issuer_path_segments()
    if not issuer_path:
        return path
    if path == issuer_path:
        return ""
    if path.startswith(issuer_path + "/"):
        return path[len(issuer_path) + 1 :]
    return path


def _base_path_js_literal() -> str:
    """``BASE_PATH`` as a JavaScript string literal safe inside ``<script>``.

    The consent page's script addresses the platform with same-origin paths
    (``/api/v1/auth/config``, ``/mcp/oauth/authorize/session``, the SSO
    routes); under a URL path prefix those must carry it. ``<``, ``>``
    and ``&`` are escaped so a prefix can never close the script element.
    """
    return (
        json.dumps(settings.base_path)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _hash_code_verifier(verifier: str) -> str | None:
    """S256 hash of PKCE code_verifier (base64url-encoded SHA-256).

    ``None`` for a verifier that is not ASCII (RFC 7636): no challenge was made
    from it, and ``None`` equals no stored challenge, an empty one included.
    """
    import base64

    try:
        raw = verifier.encode("ascii")
    except UnicodeEncodeError:
        return None
    digest = hashlib.sha256(raw).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


async def _read_oauth_params(request: Request) -> dict[str, str] | None:
    """The parameters of a token or revocation request, or ``None`` if unreadable.

    OAuth parameters are strings (RFC 6749 3.2 / RFC 7009 2.1). A JSON body can
    carry any type, and a string UTF-8 cannot encode (a lone surrogate), so
    only encodable strings are kept: anything else reads as a missing
    parameter - ``invalid_request`` / ``invalid_client`` - never a 500.
    """
    content_type = request.headers.get("content-type", "")
    try:
        if "application/json" in content_type:
            data = await request.json()
        else:
            data = dict(await request.form())
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    params: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(key, str) and isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                continue
            params[key] = value
    return params


#: Grants contributed by feature modules, keyed by ``grant_type``. The
#: authorization server owns the endpoint and client authentication; a feature
#: that needs its own grant registers here rather than being special-cased in
#: the dispatch below. Populated at startup (see ``register_grant_handler``),
#: which is what keeps this module free of any import from the features it
#: serves.
_GRANT_HANDLERS: dict[str, Any] = {}

#: Token ``type`` values this server mints, its own plus those contributed by
#: registered grants. Not consulted per-request - the session guard keys on the
#: presence of ``type``, not on membership here - but a grant claiming a type
#: another already mints would make its tokens interchangeable on that
#: resource, so registration refuses it.
_ACCESS_TOKEN_TYPES: set[str] = {MCP_OAUTH_TOKEN_TYPE}


def register_grant_handler(
    grant_type: str, handler: Any, *, token_type: str | None = None
) -> None:
    """Register a feature's grant handler. Idempotent.

    Raises when *token_type* collides with a type already minted here: two
    grants sharing one ``type`` means each one's tokens are accepted wherever
    the other's are, which is precisely the confusion the claim exists to
    prevent.
    """
    if (
        token_type
        and token_type in _ACCESS_TOKEN_TYPES
        and _GRANT_HANDLERS.get(grant_type) is not handler
    ):
        raise ValueError(
            f"token_type {token_type!r} is already minted by this server; "
            "each grant needs a distinct type or its tokens become "
            "interchangeable with another resource's"
        )
    _GRANT_HANDLERS[grant_type] = handler
    if token_type:
        _ACCESS_TOKEN_TYPES.add(token_type)


async def authenticate_client(
    db: AsyncSession, client_id: str, client_secret: str, *, grant_type: str
) -> OAuthClient | None:
    """Verify a confidential client's credentials for *grant_type*.

    Resource-neutral plumbing offered to registered grants so each does not
    re-implement secret verification. Returns ``None`` on every failure rather
    than distinguishing them: the caller turns that into one uniform error, and
    a caller that could tell the failures apart could enumerate registrations.
    """
    client = (
        await db.execute(
            select(OAuthClient).where(OAuthClient.client_id == client_id)
        )
    ).scalar_one_or_none()
    if client is None or not client.client_secret_hash:
        return None
    if not await bcrypt_checkpw_async(
        client_secret, client.client_secret_hash.encode()
    ):
        return None
    if grant_type not in (client.grant_types or []):
        return None
    return client


def _create_mcp_access_token(
    user_id: str,
    *,
    client_id: str,
    resource: str | None = None,
) -> str:
    """Issue a JWT access token scoped to the MCP resource server."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": user_id,
        "iss": _base_url(),
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        "type": MCP_OAUTH_TOKEN_TYPE,
        "client_id": client_id,
    }
    if resource:
        payload["aud"] = resource
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def _granted_oidc_scope(requested: str, client: OAuthClient) -> str:
    """The OpenID Connect scope granted on a request: the supported values the
    client asked for AND registered. Empty unless that includes ``openid``, so
    a client that never registered for sign-in (an MCP client sending
    ``scope=openid`` today) gets no ID token.

    Only a platform's sign-in client is granted any: a dynamically
    registered row that kept OpenID Connect scopes (registered before they were
    stripped at registration) gets none either."""
    if not (getattr(client, "client_id", None) or "").startswith(PLATFORM_CLIENT_PREFIX):
        return ""
    asked = set(requested.split())
    registered = set((getattr(client, "scope", None) or "").split())
    granted = [value for value in OIDC_SCOPES_SUPPORTED if value in asked and value in registered]
    return " ".join(granted) if "openid" in granted else ""


async def _granted_oidc_scope_for(db: AsyncSession, requested: str, client: OAuthClient) -> str:
    """:func:`_granted_oidc_scope`, and only while the client's platform
    registration is active: an unregistered or purged platform signs
    nobody in. Queries only for a sign-in request of a platform client."""
    granted = _granted_oidc_scope(requested, client)
    if granted and not await is_active_platform_client(db, client.client_id):
        return ""
    return granted


def _self_registered_scope(scope: str | None) -> str | None:
    """The scope a dynamically registered client may keep: what it declared,
    minus :data:`TRUSTED_CLIENT_SCOPES`."""
    kept = [value for value in (scope or "").split() if value not in TRUSTED_CLIENT_SCOPES]
    return " ".join(kept) or None


def _check_oidc_params(scope: str, nonce: str) -> None:
    """Both are carried with the code and echoed into the ID token: bound them,
    before the person is asked to sign in. Only for an ``openid`` request: an
    MCP client's ``scope`` was never read and stays unbounded."""
    if "openid" not in scope.split():
        return
    if len(scope) > MAX_SCOPE_LENGTH or len(nonce) > MAX_NONCE_LENGTH:
        raise HTTPException(status_code=400, detail="scope or nonce too long")


def _create_id_token(user: User, *, client_id: str, nonce: str | None, scope: str) -> str:
    """Issue an OpenID Connect ID token for ``user`` to ``client_id``.

    Signed with the platform's Ed25519 key, so the client verifies it against
    ``/xtm/auth/jwks``. ``aud`` is the client, never a platform URL: a
    cross-platform JWT (``aud`` = the receiving platform) and an ID token can
    never pass for one another.
    """
    now = datetime.now(UTC)
    granted = scope.split()
    payload: dict[str, Any] = {
        "iss": _base_url(),
        "sub": str(user.id),
        "aud": client_id,
        "iat": now,
        "exp": now + timedelta(minutes=ID_TOKEN_EXPIRE_MINUTES),
    }
    if "email" in granted:
        payload["email"] = user.email
    if "profile" in granted:
        payload["name"] = user.full_name
        # Always present, null without a title: a client tells "no title" from
        # a server that does not emit the claim. OpenID Connect has no title
        # claim, hence the platform prefix, as for ``xtm_role``.
        payload["xtm_title"] = user.title or None
    if "xtm_role" in granted:
        payload["xtm_role"] = user.role
        payload["xtm_super_admin"] = is_platform_admin(user)
    if nonce:
        payload["nonce"] = nonce
    return sign_platform_jwt(payload)


def _generate_refresh_token() -> str:
    return secrets.token_urlsafe(48)


async def _store_auth_code(
    code: str,
    *,
    client_id: str,
    redirect_uri: str,
    user_id: str,
    code_challenge: str,
    code_challenge_method: str,
    resource: str | None,
    scope: str | None = None,
    nonce: str | None = None,
) -> None:
    redis = await redis_client()
    data = json.dumps(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "user_id": user_id,
            "code_challenge": code_challenge,
            "code_challenge_method": code_challenge_method,
            "resource": resource,
            "scope": scope,
            "nonce": nonce,
        }
    )
    await redis.set(f"{REDIS_PREFIX_CODE}{code}", data, ex=AUTH_CODE_TTL_SECONDS)


async def _consume_auth_code(code: str) -> dict | None:
    redis = await redis_client()
    key = f"{REDIS_PREFIX_CODE}{code}"
    raw = await redis.get(key)
    if not raw:
        return None
    await redis.delete(key)
    return json.loads(raw)


def _refresh_family_deadline(client_id: str, family_started_at: float) -> float | None:
    """When the refresh tokens of one authorization stop rotating, if they do."""
    if not is_metadata_document_url(client_id):
        return None
    return family_started_at + DOCUMENT_CLIENT_REFRESH_FAMILY_DAYS * 86400


async def _store_refresh_token(
    token_hash: str,
    *,
    user_id: str,
    client_id: str,
    resource: str | None,
    family_started_at: float,
) -> None:
    """Store a refresh token; *family_started_at* is when its authorization was granted.

    Carried across rotations, so the family of a metadata-document client ends
    :data:`DOCUMENT_CLIENT_REFRESH_FAMILY_DAYS` after its authorization.
    """
    redis = await redis_client()
    data = json.dumps(
        {
            "user_id": user_id,
            "client_id": client_id,
            "resource": resource,
            "family_started_at": family_started_at,
        }
    )
    ttl = REFRESH_TOKEN_EXPIRE_DAYS * 86400
    deadline = _refresh_family_deadline(client_id, family_started_at)
    if deadline is not None:
        ttl = max(1, min(ttl, int(deadline - time.time())))
    await redis.set(f"{REDIS_PREFIX_REFRESH}{token_hash}", data, ex=ttl)


async def _consume_refresh_token(token: str) -> dict | None:
    redis = await redis_client()
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    key = f"{REDIS_PREFIX_REFRESH}{token_hash}"
    raw = await redis.get(key)
    if not raw:
        return None
    await redis.delete(key)
    return json.loads(raw)


#: The redirect transport rule (loopback host or HTTPS), shared with the
#: Client ID Metadata Documents (``mcp_oauth_clients.redirect_uri_allowed``).
_validate_redirect_uri = redirect_uri_allowed


async def _load_client(
    db: AsyncSession,
    client_id: str,
    request: Request,
    *,
    redirect_uri: str,
    code_challenge_method: str,
) -> OAuthClient | None:
    """The client *client_id* names: a registered row, or the client its
    metadata document describes.

    A document ``client_id`` while documents are switched off is refused
    outright, rather than looked up as a registration it can never be. Before
    a document is fetched - which spends a budget - the request must be one
    that could succeed: a redirect URI that may receive a code and a supported
    PKCE method.
    """
    if is_metadata_document_url(client_id):
        from app.core.security_options import security_option

        if not security_option("mcp_cimd_enabled"):
            raise HTTPException(
                status_code=400,
                detail="Client ID Metadata Documents are disabled on this platform",
            )
        if code_challenge_method not in SUPPORTED_CODE_CHALLENGE_METHODS:
            raise HTTPException(status_code=400, detail="Unsupported code_challenge_method")
        if not redirect_uri_allowed(redirect_uri):
            raise HTTPException(status_code=400, detail="redirect_uri not registered")
        return await resolve_client_metadata(client_id, request=request)
    result = await db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
    return result.scalar_one_or_none()


# ── Well-Known Metadata (RFC 9728 + RFC 8414) ───────────────────────────────


@well_known_router.get("/oauth-protected-resource")
@well_known_router.get("/oauth-protected-resource/{path:path}")
async def protected_resource_metadata(path: str = ""):
    """OAuth 2.0 Protected Resource Metadata (RFC 9728).

    Advertises the authorization server(s) for this resource server.
    Path-aware: ``/.well-known/oauth-protected-resource/mcp/global``
    returns metadata for the ``/mcp/global`` resource. Under a URL path
    prefix the RFC's path-insertion form
    (``/.well-known/oauth-protected-resource/xtm-one/mcp/global`` at the host
    root) names the same resource.
    """
    base = _base_url()
    path = _strip_issuer_path(path.strip("/"))
    resource = f"{base}/{path}" if path else base
    return JSONResponse(
        content={
            "resource": resource,
            "authorization_servers": [base],
            "bearer_methods_supported": ["header"],
        },
        headers={
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
    )


@well_known_router.get("/oauth-authorization-server")
@well_known_router.get("/oauth-authorization-server/{path:path}")
async def authorization_server_metadata(path: str = ""):
    """OAuth 2.0 Authorization Server Metadata (RFC 8414).

    Provides all endpoint URLs and supported capabilities for automated
    client discovery. Under a URL path prefix the issuer is
    ``https://host/xtm-one`` and RFC 8414 puts the well-known segment before
    its path, so ``/.well-known/oauth-authorization-server/xtm-one`` (at the
    host root) is this same document; any other suffix is not a
    document this server publishes.
    """
    return JSONResponse(
        content=_authorization_server_document(path),
        headers={
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
    )


def _authorization_server_document(path: str) -> dict[str, Any]:
    """The RFC 8414 document, shared by the OAuth and OpenID Connect discovery routes."""
    suffix = path.strip("/")
    if suffix and suffix != _issuer_path_segments():
        raise HTTPException(status_code=404, detail="Not Found")
    base = _base_url()

    def _cimd_enabled() -> bool:
        from app.core.security_options import security_option

        return bool(security_option("mcp_cimd_enabled"))

    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/mcp/oauth/authorize",
        "token_endpoint": f"{base}/mcp/oauth/token",
        "registration_endpoint": f"{base}/mcp/oauth/register",
        "revocation_endpoint": f"{base}/mcp/oauth/revoke",
        "response_types_supported": ["code"],
        # Registered grants are advertised alongside the built-ins, so a
        # feature contributing one is discoverable without editing this.
        "grant_types_supported": [
            "authorization_code",
            "refresh_token",
            *sorted(_GRANT_HANDLERS),
        ],
        "token_endpoint_auth_methods_supported": [
            "none",
            "client_secret_post",
        ],
        "code_challenge_methods_supported": ["S256"],
        # RFC 9207: every authorization response names its issuer.
        "authorization_response_iss_parameter_supported": True,
        # Client ID Metadata Documents (MCP 2025-11-25): a client may name
        # its own HTTPS metadata document instead of registering.
        "client_id_metadata_document_supported": _cimd_enabled(),
        "service_documentation": f"{base}/docs",
    }


@well_known_router.get("/openid-configuration")
@well_known_router.get("/openid-configuration/{path:path}")
async def openid_configuration(path: str = ""):
    """OpenID Connect Discovery 1.0 for the same server.

    The RFC 8414 document plus what an OpenID Connect relying party needs: the
    JWKS of the platform key that signs ID tokens, the scopes and the signing
    algorithm. Same issuer and the same path-prefix handling as
    :func:`authorization_server_metadata`.
    """
    base = _base_url()
    metadata = {
        **_authorization_server_document(path),
        "jwks_uri": f"{base}{XTM_JWKS_PATH}",
        "scopes_supported": list(OIDC_SCOPES_SUPPORTED),
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["EdDSA"],
        "claims_supported": [
            "iss", "sub", "aud", "iat", "exp", "nonce", "email", "name", "xtm_title", "xtm_role", "xtm_super_admin",
        ],
    }
    return JSONResponse(content=metadata, headers={"Access-Control-Allow-Origin": "*"})


# ── Dynamic Client Registration (RFC 7591) ──────────────────────────────────


class ClientRegistrationRequest(BaseModel):
    client_name: str | None = None
    redirect_uris: list[str] = []
    #: OpenID Connect Registration's ``web`` / ``native``, which MCP 2026-07-28
    #: asks clients to send. Accepted and returned: this server applies one
    #: redirect rule to both (HTTPS, or loopback on any port).
    application_type: str | None = None
    grant_types: list[str] | None = None
    response_types: list[str] | None = None
    token_endpoint_auth_method: str | None = None
    client_uri: str | None = None
    scope: str | None = None


async def _evict_oldest_clients_if_full(db: AsyncSession) -> None:
    """Keep the dynamic client table bounded at ``settings.mcp_dcr_max_clients``.

    When the cap is reached, the oldest registrations (by ``created_at``)
    are evicted to make room for the new one. Eviction is preferred over a
    hard rejection because:

    - rejecting would let an attacker who filled the table permanently DoS
      registration for legitimate clients, while eviction only churns the
      oldest rows;
    - an evicted-but-still-active client self-heals — RFC 7591 clients
      (and every MCP client) handle ``invalid_client`` by re-registering
      and re-running the authorization flow;
    - the ``OAuthClient`` model tracks ``created_at`` only (no last-used
      column), so age is the best available staleness signal without a
      schema migration.

    A misconfigured cap (0 or negative ``MCP_DCR_MAX_CLIENTS``) is clamped
    to 1: the overflow math (``count - cap + 1``) would otherwise evict
    essentially the whole table on every registration, which defeats the
    point of a *bounded* table. Operators who want to turn registration
    off entirely have the explicit ``MCP_DCR_ENABLED`` kill switch.
    """
    # Serialize the count → evict → insert critical section across
    # concurrent registrations. Without a lock, N concurrent requests can
    # all observe ``count < cap``, skip eviction, and each insert — letting
    # the table exceed the cap by the burst size, so the "bounded table"
    # guarantee would not hold exactly when it matters (an abuse burst).
    # ``pg_advisory_xact_lock`` blocks until the lock is granted and is
    # released automatically when the surrounding transaction ends (the
    # ``commit()`` in ``register_client``), so exactly one registration at
    # a time evaluates the cap, evicts, and inserts. Registrations are rare
    # and per-IP throttled, so the serialization cost is negligible.
    # The key is bound as a parameter (not interpolated into the SQL string)
    # to match the repo-wide advisory-lock convention (``kb_sync.py``,
    # ``sub_agent.py``) and keep static analyzers (Bandit S608) quiet; the
    # explicit bigint cast pins the single-key
    # ``pg_advisory_xact_lock(int8)`` overload.
    await db.execute(
        text("SELECT pg_advisory_xact_lock(CAST(:lock_key AS bigint))"),
        {"lock_key": DCR_REGISTRATION_LOCK_KEY},
    )

    cap = max(1, settings.mcp_dcr_max_clients)
    # DCR clients only. A2A partner credentials share this table but are not
    # dynamically registered: they are minted when an agent is exposed and
    # revoked with it, and an evicted one cannot self-heal by re-registering
    # the way an RFC 7591 client can - it just returns invalid_client forever,
    # silently breaking a live partner integration. Ordinary MCP churn past
    # the cap (or the unauthenticated register endpoint) must not reach them,
    # and they must not count toward a cap that exists to bound *dynamic*
    # registration.
    count = (
        await db.scalar(
            select(func.count())
            .select_from(OAuthClient)
            .where(OAuthClient.a2a_exposure_id.is_(None))
            .where(~OAuthClient.client_id.startswith(PLATFORM_CLIENT_PREFIX))
        )
    ) or 0
    if count < cap:
        return

    # Evict in a single bulk DELETE with an IN-subquery rather than
    # loading + deleting row by row: the table may be far above the cap
    # on first deploy (the endpoint used to be unbounded), and a per-row
    # loop would materialize thousands of ORM objects and hold the
    # advisory lock / transaction open long enough to block legitimate
    # registrations. ``synchronize_session=False`` is safe — no
    # ``OAuthClient`` instance is in the session at this point.
    overflow = count - cap + 1
    oldest_ids = (
        select(OAuthClient.id)
        .where(OAuthClient.a2a_exposure_id.is_(None))
        .where(~OAuthClient.client_id.startswith(PLATFORM_CLIENT_PREFIX))
        .order_by(OAuthClient.created_at.asc())
        .limit(overflow)
        .scalar_subquery()
    )
    result = await db.execute(
        delete(OAuthClient)
        .where(OAuthClient.id.in_(oldest_ids))
        .execution_options(synchronize_session=False)
    )
    logger.warning(
        "MCP DCR client cap reached (%d/%d) — evicted %d oldest registration(s)",
        count,
        cap,
        result.rowcount,
    )


def _registration_transport_refusal() -> JSONResponse:
    """The transport refusal in RFC 7591 section 3.2.2's registration error shape.

    Of the four codes that section defines, ``invalid_client_metadata`` is the
    one naming no field the client could change (``invalid_redirect_uri`` and
    the two software-statement codes do), so a client does not retry with other
    metadata.
    """
    return JSONResponse(
        content={"error": "invalid_client_metadata", "error_description": INSECURE_TRANSPORT_DETAIL},
        status_code=status.HTTP_400_BAD_REQUEST,
    )


_TransportFirstRoute = credential_transport_route(_registration_transport_refusal)


async def register_client(
    request: Request,
    body: ClientRegistrationRequest,
    db: AsyncSession = Depends(get_db),
):
    """RFC 7591 Dynamic Client Registration.

    MCP clients call this to obtain a ``client_id`` (and optionally a
    ``client_secret`` for confidential clients) before starting the
    authorization flow.

    The endpoint is intentionally unauthenticated (the MCP spec relies on
    open DCR for client onboarding), so it is hardened against abuse
    instead: an operator kill switch (Settings > Access and security, or
    ``MCP_DCR_ENABLED``, which wins), a per-IP rate limit, and a bounded
    client table with oldest-first eviction (see
    ``_evict_oldest_clients_if_full``). Under ``CREDENTIAL_TRANSPORT_STRICT``
    its route (``_TransportFirstRoute``) refuses a transport the platform
    cannot confirm encrypted before any of them runs.
    """
    from app.core.security_options import security_option

    if not security_option("mcp_dcr_enabled"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Dynamic client registration is disabled on this platform",
        )

    # Throttle before any DB work so a registration flood is rejected with
    # 429 without touching Postgres.
    await enforce_rate_limit(
        request,
        "mcp-oauth-register",
        limit=max(1, settings.mcp_dcr_rate_limit),
        window_seconds=DCR_RATE_WINDOW_SECONDS,
    )

    for uri in body.redirect_uris:
        if not _validate_redirect_uri(uri):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid redirect_uri (must be localhost or HTTPS): {uri}",
            )

    if body.application_type is not None and body.application_type not in ("web", "native"):
        return JSONResponse(
            content={
                "error": "invalid_client_metadata",
                "error_description": "application_type must be web or native",
            },
            status_code=status.HTTP_400_BAD_REQUEST,
        )

    grant_types = body.grant_types or ["authorization_code"]
    response_types = body.response_types or ["code"]
    auth_method = body.token_endpoint_auth_method or "none"

    client_id = f"mcp-{uuid.uuid4().hex[:24]}"

    client_secret: str | None = None
    client_secret_hash: str | None = None
    if auth_method != "none":
        client_secret = secrets.token_urlsafe(32)
        client_secret_hash = (
            await bcrypt_hashpw_async(client_secret.encode())
        ).decode()

    # Acquire the advisory lock (inside the helper) only now — after the
    # CPU-only work above (client_id generation, bcrypt secret hashing,
    # ~100-300ms for confidential clients) — so the globally serialized
    # critical section covers nothing but the DB work: count → evict →
    # insert → commit. Taking the lock any earlier would make every
    # concurrent registration queue behind another request's bcrypt run.
    await _evict_oldest_clients_if_full(db)

    client = OAuthClient(
        client_id=client_id,
        client_secret_hash=client_secret_hash,
        client_name=body.client_name,
        client_uri=body.client_uri,
        redirect_uris=body.redirect_uris,
        grant_types=grant_types,
        response_types=response_types,
        token_endpoint_auth_method=auth_method,
        scope=_self_registered_scope(body.scope),
    )
    db.add(client)
    await db.commit()

    result: dict[str, Any] = {
        "client_id": client_id,
        "client_name": body.client_name,
        "redirect_uris": body.redirect_uris,
        "grant_types": grant_types,
        "response_types": response_types,
        "token_endpoint_auth_method": auth_method,
    }
    if body.application_type is not None:
        result["application_type"] = body.application_type
    if client_secret:
        result["client_secret"] = client_secret

    return JSONResponse(content=result, status_code=status.HTTP_201_CREATED)


router.add_api_route(
    "/register", register_client, methods=["POST"], route_class_override=_TransportFirstRoute
)


# ── Authorization Endpoint ───────────────────────────────────────────────────


# Client-side controller for the consent page.  Kept as a static (non-f)
# string so its JavaScript braces never collide with the ``_consent_html``
# f-string and so the page needs no per-request templating of script logic.
# It reads OAuth params from the hidden form fields and uses same-origin
# root-relative URLs (the page is served by the backend, so these resolve to the
# backend regardless of how BASE_URL is configured), each prefixed with the
# ``BASE_PATH`` literal the page declares just above this script.
#
# Three runtime paths (SSO users could not complete the
# flow because the old page only offered a local email/password form):
#   1. Existing session  → JWT in localStorage['copilot_token'] → show only an
#      "Authorize" button that POSTs the token to /mcp/oauth/authorize/session.
#   2. No session + SSO   → discover providers via /api/v1/auth/config, render
#      OIDC/SAML buttons that stash the return URL and redirect to the IdP.
#   3. No session + local → the original email/password form (fallback).
#   4. A product sign-in (``openid`` granted to the sign-in client of an
#      active platform registration, decided server-side in ``product_sign_in``):
#      no consent, the products and XTM One are one platform. With a
#      session the page authorizes at once; without one it sends the person to
#      the platform's own sign-in page, which opens an XTM One session and comes
#      back here: the person is then signed in to XTM One and to the next
#      product too. The in-page form would check the password without opening
#      a session. The card stays hidden unless the authorization fails.
#
# The page's controls are bound here, never through ``on*`` attributes: the
# page runs under a Content-Security-Policy that allows this script by its
# hash, and no hash or nonce can allow an inline event handler.
_CONSENT_SCRIPT = """
var _loginSectionReady = null;
// Product sign-in round trips to the platform sign-in page (signInToPlatform).
var SIGN_IN_ATTEMPTS = 'mcp_oauth_sign_in_attempts';

document.querySelectorAll('[data-consent-action="cancel"]').forEach(function (btn) {
  btn.addEventListener('click', function () {
    // A product's full-page sign-in has no window to close: tell the product
    // (RFC 6749 section 4.1.2.1), which checks the state as for a code.
    if (isProductSignIn()) { returnToProduct('access_denied'); } else { window.close(); }
  });
});
document.getElementById('session-authorize-btn').addEventListener('click', function () {
  authorizeWithSession();
});
document.getElementById('local-auth-form').addEventListener('submit', function (event) {
  if (!validateLocalAuth()) event.preventDefault();
});

async function setupLoginSection() {
  // Discover configured SSO providers and render their buttons; on an SSO-only
  // platform (local auth disabled) hide the local email/password form. This is
  // run unconditionally — even when a session token is present — so that if the
  // stored session later turns out to be expired and showLoginFallback()
  // reveals the login section, the SSO buttons are already populated. Without
  // this, an SSO-only user whose token had expired would be stranded on a login
  // section showing only an unusable local form and no SSO buttons.
  try {
    var resp = await fetch(BASE_PATH + '/api/v1/auth/config');
    if (!resp.ok) return;
    var config = await resp.json();
    var ssoContainer = document.getElementById('sso-buttons');
    var hasSso = false;

    function addSsoButton(label, href) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn-sso';
      btn.textContent = label;
      btn.onclick = function () {
        try { sessionStorage.setItem('mcp_oauth_return', window.location.href); } catch (e) {}
        window.location.href = href;
      };
      ssoContainer.appendChild(btn);
    }

    if (config.oidc_enabled) {
      hasSso = true;
      addSsoButton('Continue with ' + (config.oidc_display_name || 'SSO'), BASE_PATH + '/api/auth/oidc/login');
    }
    if (config.saml_enabled) {
      hasSso = true;
      addSsoButton('Continue with ' + (config.saml_display_name || 'Corporate SSO'), BASE_PATH + '/api/auth/saml/login');
    }

    if (hasSso) {
      if (config.local_enabled) {
        document.getElementById('sso-divider').style.display = '';
      } else {
        // SSO-only platform — hide the local form entirely.
        document.getElementById('local-auth-fields').style.display = 'none';
        document.getElementById('local-auth-actions').style.display = 'none';
      }
    }
  } catch (e) {
    // Network/parse error — silently fall back to the local-auth form.
  }
}

(async function () {
  // Second step of a password sign-in: the page asks for the code
  // alone, so neither a stored session nor the SSO buttons apply.
  if (document.getElementById('code')) return;

  var token = null;
  try { token = localStorage.getItem('copilot_token'); } catch (e) {}

  if (isProductSignIn()) {
    if (token) { authorizeWithSession(); } else { signInToPlatform(); }
    return;
  }

  // Existing session — show the consent button and hide the login form up
  // front (synchronously, to avoid a flash of the login form before SSO
  // discovery completes).
  if (token) {
    document.getElementById('login-section').style.display = 'none';
    document.getElementById('session-section').style.display = 'block';
  }

  // Configure the login section regardless of whether a session exists, so the
  // SSO buttons / local-form visibility are already correct if an expired
  // session forces showLoginFallback() to reveal the login section later.
  _loginSectionReady = setupLoginSection();
  await _loginSectionReady;
})();

function validateLocalAuth() {
  var errDiv = document.getElementById('js-error');
  var code = document.getElementById('code');
  if (code) {
    if (!(code.value || '').trim()) {
      errDiv.textContent = 'Please enter your verification code.';
      errDiv.style.display = 'block';
      return false;
    }
    return true;
  }
  var email = (document.getElementById('email').value || '').trim();
  var password = document.getElementById('password').value || '';
  if (!email || !password) {
    errDiv.textContent = 'Please enter your email and password.';
    errDiv.style.display = 'block';
    return false;
  }
  return true;
}

function isProductSignIn() {
  // Decided by the server from the granted scope, not read from the request.
  var field = document.querySelector('#local-auth-form [name=product_sign_in]');
  return !!field && field.value === '1';
}

function signInToPlatform() {
  // A session endpoint that keeps refusing a fresh session would bounce the
  // person between this page and the sign-in page: one retry, then the error.
  // Counted per authorization request (its PKCE challenge), so an abandoned
  // sign-in never weighs on the next one.
  var request = formValue('code_challenge');
  var attempts = 0;
  try {
    var stored = JSON.parse(sessionStorage.getItem(SIGN_IN_ATTEMPTS) || 'null');
    if (stored && stored.request === request) { attempts = Number(stored.count) || 0; }
  } catch (e) {}
  if (attempts >= 2) { showProductSignInError(); return; }
  try { sessionStorage.setItem(SIGN_IN_ATTEMPTS, JSON.stringify({ request: request, count: attempts + 1 })); } catch (e) {}
  try { sessionStorage.setItem('mcp_oauth_return', window.location.href); } catch (e) {}
  window.location.replace(BASE_PATH + '/login');
}

function formValue(name) {
  return document.querySelector('#local-auth-form [name=' + name + ']').value;
}

function returnToProduct(error) {
  // The redirect URI was matched against the client's registration before this
  // page was served.
  var target = new URL(formValue('redirect_uri'));
  target.searchParams.set('error', error);
  if (formValue('state')) { target.searchParams.set('state', formValue('state')); }
  target.searchParams.set('iss', document.getElementById('local-auth-form').getAttribute('data-issuer'));
  window.location.replace(target.href);
}

function showProductSignInError(status) {
  // Not the MCP consent card: a product signs in with no consent, so the page
  // says the sign-in failed and offers to try again.
  try { sessionStorage.removeItem(SIGN_IN_ATTEMPTS); } catch (e) {}
  document.getElementById('signing-in').style.display = 'none';
  document.querySelector('#consent-card h2').textContent = 'Sign-in failed';
  document.querySelector('#consent-card .scope-info').style.display = 'none';
  document.querySelector('#session-section .session-info').style.display = 'none';
  document.getElementById('login-section').style.display = 'none';
  document.getElementById('session-section').style.display = 'block';
  document.getElementById('consent-card').style.display = '';
  var errDiv = document.getElementById('js-error');
  var btn = document.getElementById('session-authorize-btn');
  if (status === 403) {
    errDiv.textContent = 'You cannot sign in to a product while impersonating another user.';
  } else if (status === 400) {
    errDiv.textContent = 'Signing in to this product through XTM One is not available.';
  } else {
    errDiv.textContent = 'Could not sign you in. Please try again.';
  }
  errDiv.style.display = 'block';
  // Cancel goes back to the product; Try again only where trying again can work.
  btn.style.display = status === 403 || status === 400 ? 'none' : '';
  btn.disabled = false;
  btn.textContent = 'Try again';
}

async function showLoginFallback(btn) {
  try { localStorage.removeItem('copilot_token'); } catch (e) {}
  if (isProductSignIn()) { signInToPlatform(); return; }
  // Wait for SSO discovery to finish so the revealed login section already has
  // its SSO buttons and correct local-form visibility (covers the case where a
  // stored session was expired and the user clicked Authorize before discovery
  // completed).
  try { await _loginSectionReady; } catch (e) {}
  document.getElementById('session-section').style.display = 'none';
  document.getElementById('login-section').style.display = 'block';
  if (btn) { btn.disabled = false; btn.textContent = 'Authorize'; }
}

async function authorizeWithSession() {
  var token = null;
  try { token = localStorage.getItem('copilot_token'); } catch (e) {}
  if (!token) { showLoginFallback(null); return; }

  var btn = document.getElementById('session-authorize-btn');
  btn.disabled = true;
  btn.textContent = 'Authorizing\\u2026';

  var form = document.getElementById('local-auth-form');
  function val(name) { return form.querySelector('[name=' + name + ']').value; }
  var body = {
    client_id: val('client_id'),
    redirect_uri: val('redirect_uri'),
    state: val('state'),
    code_challenge: val('code_challenge'),
    code_challenge_method: val('code_challenge_method'),
    response_type: val('response_type'),
    resource: val('resource'),
    scope: val('scope'),
    nonce: val('nonce'),
  };

  try {
    var resp = await fetch(BASE_PATH + '/mcp/oauth/authorize/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token },
      body: JSON.stringify(body),
    });
    if (resp.status === 401) {
      // Session expired/invalid — drop it and let the user sign in again.
      showLoginFallback(btn);
      return;
    }
    if (resp.status === 423) {
      // The companion is locked behind its PIN on this phone.
      var lockedDiv = document.getElementById('js-error');
      lockedDiv.textContent = 'Unlock the app with your PIN first, then try again.';
      lockedDiv.style.display = 'block';
      btn.disabled = false;
      btn.textContent = 'Authorize';
      return;
    }
    if (!resp.ok) {
      // A product sign-in refused for good (impersonating, sign-in withdrawn): no retry.
      if (isProductSignIn() && (resp.status === 403 || resp.status === 400)) { showProductSignInError(resp.status); return; }
      throw new Error('Authorization failed (' + resp.status + ')');
    }
    var data = await resp.json();
    // A product sign-in leaves no page behind: Back returns to where it started.
    if (isProductSignIn()) {
      try { sessionStorage.removeItem(SIGN_IN_ATTEMPTS); } catch (e) {}
      window.location.replace(data.redirect_url);
    } else {
      window.location.href = data.redirect_url;
    }
  } catch (e) {
    if (isProductSignIn()) { showProductSignInError(); return; }
    var errDiv = document.getElementById('js-error');
    errDiv.textContent = 'Could not complete authorization. Please try again.';
    errDiv.style.display = 'block';
    btn.disabled = false;
    btn.textContent = 'Authorize';
  }
}
"""


def _client_presentation(client: OAuthClient, client_id: str, redirect_uri: str) -> dict[str, str]:
    """What the consent page says of the client, by kind.

    A registered client is named by its registration. A metadata-document
    client is named by the host its document is served from - the one thing
    no other party can claim - with its ``client_name`` shown as what it says
    it is, and the host the code goes to, flagged when it is this computer or
    another site than the document's (MCP 2025-11-25, CIMD security
    considerations: the redirect host must be shown).
    """
    if not is_metadata_document_url(client_id):
        return {"client_name": client.client_name or client_id}
    host = document_host(client_id)
    try:
        target = urlparse(redirect_uri).hostname or ""
    except ValueError:
        target = ""
    if target in ("localhost", "127.0.0.1", "::1"):
        note = "an application on this computer. Authorize only if you started this sign-in from it."
    elif target.lower() != host.lower():
        note = "a site other than the one the application is served from."
    else:
        note = ""
    return {
        "client_name": client.client_name or "",
        "client_host": host,
        "redirect_host": target,
        "redirect_note": note,
    }


def _consent_html(
    *,
    client_name: str,
    client_host: str = "",
    redirect_host: str = "",
    redirect_note: str = "",
    error: str = "",
    client_id: str = "",
    redirect_uri: str = "",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    response_type: str = "",
    resource: str = "",
    scope: str = "",
    nonce: str = "",
    product_sign_in: bool = False,
    app_name: str | None = None,
    mfa_token: str = "",
) -> str:
    """Self-contained HTML consent + login page matching the platform design.

    With ``mfa_token`` the page is the second step of a password sign-in for an
    account with two-factor enabled: the form asks for the code and
    carries the challenge back instead of asking for the email and password.
    """
    app_name = html_module.escape(app_name or get_cached_branding().name)
    if client_host:
        # A metadata-document client: the host is the headline, its name a claim.
        headline = f"<strong>{html_module.escape(client_host)}</strong>"
        details = ""
        if client_name:
            details += f'<div class="client-claim">It calls itself &ldquo;{html_module.escape(client_name)}&rdquo;.</div>'
        if redirect_host:
            note = f" &mdash; {html_module.escape(redirect_note)}" if redirect_note else "."
            details += (
                f'<div class="client-claim">The authorization will be sent to '
                f"<strong>{html_module.escape(redirect_host)}</strong>{note}</div>"
            )
    else:
        headline = f"<strong>{html_module.escape(client_name) or 'An application'}</strong>"
        details = ""
    error = html_module.escape(error)
    client_id = html_module.escape(client_id)
    redirect_uri = html_module.escape(redirect_uri)
    state = html_module.escape(state)
    code_challenge = html_module.escape(code_challenge)
    code_challenge_method = html_module.escape(code_challenge_method)
    response_type = html_module.escape(response_type)
    resource = html_module.escape(resource)
    scope = html_module.escape(scope)
    nonce = html_module.escape(nonce)
    if mfa_token:
        local_auth_fields = f"""<p class="session-info">Enter the 6-digit code from your authenticator app, or one of your recovery codes.</p>
        <label for="code">Verification code</label>
        <input type="text" id="code" name="code" required autocomplete="one-time-code" autofocus>
        <input type="hidden" name="mfa_token" value="{html_module.escape(mfa_token)}">"""
    else:
        local_auth_fields = """<label for="email">Email</label>
        <input type="email" id="email" name="email" required autocomplete="email">
        <label for="password">Password</label>
        <input type="password" id="password" name="password" required>"""
    error_block = ""
    if error:
        error_block = f'<div class="error">{error}</div>'
    # A product sign-in has no consent: nothing to show unless it fails.
    card_style = ' style="display:none"' if product_sign_in and not error and not mfa_token else ""
    signing_in_style = "" if card_style else ' style="display:none"'
    # A product sign-in needs the script (the session is in ``localStorage``).
    noscript = (
        '<noscript><style>#signing-in { display: none; }</style>'
        '<p class="session-info">JavaScript is required to sign in.</p></noscript>'
        if card_style
        else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Authorize – {app_name}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap');
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: 'IBM Plex Sans', sans-serif;
    background: hsl(220, 56%, 6%);
    color: #fff;
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 100vh;
  }}
  .card {{
    background: rgba(255,255,255,0.04);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(255,255,255,0.08);
    border-radius: 16px;
    padding: 40px;
    width: 420px;
    max-width: 90vw;
  }}
  .card:hover {{
    box-shadow: 0 0 30px hsl(197 100% 53% / 0.12);
  }}
  .logo {{
    text-align: center;
    margin-bottom: 24px;
    font-size: 20px;
    font-weight: 600;
    color: hsl(197, 100%, 53%);
    letter-spacing: 0.5px;
  }}
  h2 {{
    text-align: center;
    font-size: 16px;
    font-weight: 500;
    margin-bottom: 8px;
    color: rgba(255,255,255,0.9);
  }}
  .scope-info {{
    text-align: center;
    font-size: 13px;
    color: rgba(255,255,255,0.5);
    margin-bottom: 28px;
  }}
  .scope-info strong {{
    color: hsl(197, 100%, 53%);
  }}
  .client-claim {{
    margin-top: 8px;
    font-size: 12px;
  }}
  label {{
    display: block;
    font-size: 13px;
    color: rgba(255,255,255,0.6);
    margin-bottom: 6px;
    font-weight: 500;
  }}
  input[type="email"],
  input[type="password"],
  input[type="text"] {{
    width: 100%;
    padding: 10px 14px;
    border: 1px solid rgba(255,255,255,0.12);
    border-radius: 8px;
    background: rgba(255,255,255,0.05);
    color: #fff;
    font-size: 14px;
    font-family: inherit;
    margin-bottom: 16px;
    outline: none;
    transition: border-color 0.2s;
  }}
  input:focus {{
    border-color: hsl(197, 100%, 53%);
    box-shadow: 0 0 0 2px hsl(197 100% 53% / 0.15);
  }}
  .actions {{
    display: flex;
    gap: 12px;
    margin-top: 8px;
  }}
  button {{
    flex: 1;
    padding: 10px 20px;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 500;
    font-family: inherit;
    cursor: pointer;
    border: none;
    transition: all 0.2s;
  }}
  button:disabled {{
    opacity: 0.6;
    cursor: not-allowed;
  }}
  .btn-primary {{
    background: hsl(197, 100%, 53%);
    color: hsl(220, 56%, 6%);
  }}
  .btn-primary:hover:not(:disabled) {{
    background: hsl(197, 100%, 60%);
    box-shadow: 0 0 20px hsl(197 100% 53% / 0.3);
  }}
  .btn-secondary {{
    background: rgba(255,255,255,0.08);
    color: rgba(255,255,255,0.7);
    border: 1px solid rgba(255,255,255,0.12);
  }}
  .btn-secondary:hover:not(:disabled) {{
    background: rgba(255,255,255,0.12);
  }}
  .btn-sso {{
    width: 100%;
    margin-bottom: 10px;
    background: rgba(255,255,255,0.08);
    color: rgba(255,255,255,0.85);
    border: 1px solid rgba(255,255,255,0.16);
  }}
  .btn-sso:hover:not(:disabled) {{
    background: rgba(255,255,255,0.14);
  }}
  .divider {{
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 20px 0 16px;
    color: rgba(255,255,255,0.3);
    font-size: 12px;
  }}
  .divider::before,
  .divider::after {{
    content: '';
    flex: 1;
    height: 1px;
    background: rgba(255,255,255,0.1);
  }}
  .session-info {{
    text-align: center;
    font-size: 13px;
    color: rgba(255,255,255,0.6);
    margin-bottom: 24px;
    padding: 12px 16px;
    background: rgba(255,255,255,0.04);
    border-radius: 8px;
    border: 1px solid rgba(255,255,255,0.08);
  }}
  .error {{
    background: rgba(239,68,68,0.12);
    border: 1px solid rgba(239,68,68,0.3);
    color: #f87171;
    border-radius: 8px;
    padding: 10px 14px;
    font-size: 13px;
    margin-bottom: 16px;
  }}
</style>
</head>
<body>
<p class="session-info" id="signing-in"{signing_in_style}>Signing you in&hellip;</p>{noscript}
<div class="card" id="consent-card"{card_style}>
  <div class="logo">{app_name}</div>
  <h2>Authorize Application</h2>
  <div class="scope-info">
    {headline} wants to access
    your agents via MCP.
    {details}
  </div>
  {error_block}
  <div class="error" id="js-error" style="display:none"></div>

  <!-- Session path: revealed by JS when a valid platform session is found. -->
  <div id="session-section" style="display:none">
    <p class="session-info">You are already signed in. Approve access for this application?</p>
    <div class="actions">
      <button type="button" class="btn-secondary" data-consent-action="cancel">Cancel</button>
      <button type="button" class="btn-primary" id="session-authorize-btn">Authorize</button>
    </div>
  </div>

  <!-- Login path: SSO buttons (discovered at runtime) + local form. -->
  <div id="login-section">
    <div id="sso-buttons"></div>
    <div id="sso-divider" class="divider" style="display:none">or sign in with email</div>
    <form id="local-auth-form" method="POST" action="{_base_url()}/mcp/oauth/authorize" data-issuer="{html_module.escape(_base_url())}">
      <div id="local-auth-fields">
        {local_auth_fields}
      </div>
      <input type="hidden" name="client_id" value="{client_id}">
      <input type="hidden" name="redirect_uri" value="{redirect_uri}">
      <input type="hidden" name="state" value="{state}">
      <input type="hidden" name="code_challenge" value="{code_challenge}">
      <input type="hidden" name="code_challenge_method" value="{code_challenge_method}">
      <input type="hidden" name="response_type" value="{response_type}">
      <input type="hidden" name="resource" value="{resource}">
      <input type="hidden" name="scope" value="{scope}">
      <input type="hidden" name="nonce" value="{nonce}">
      <input type="hidden" name="product_sign_in" value="{"1" if product_sign_in else ""}">
      <div class="actions" id="local-auth-actions">
        <button type="button" class="btn-secondary" data-consent-action="cancel">Cancel</button>
        <button type="submit" class="btn-primary">Authorize</button>
      </div>
    </form>
  </div>
</div>
<script>{_consent_script()}</script>
</body>
</html>"""


def _consent_script() -> str:
    """The consent page's one inline script, exactly as written into the page.

    The page's Content-Security-Policy allows it by the hash of this text, so
    the template and the policy read the same function.
    """
    return f"\nvar BASE_PATH = {_base_path_js_literal()};\n{_CONSENT_SCRIPT}\n"


# Loaded in a browser window by an MCP client, never framed: framing is what a
# clickjacking attempt on its Authorize button would need. Its ``form-action``
# is widened per response by :func:`_allow_consent_form_target`. The page a
# successful sign-in answers with (:func:`_grant_response`) is served on the
# same route and carries the same policy.
register_page_policy(
    "/mcp/oauth/authorize",
    PagePolicy(
        sources=GOOGLE_FONTS_SOURCES,
        script_hashes=lambda: (script_hash_source(_consent_script()),),
        frame_ancestors="self",
    ),
)


def _allow_consent_form_target(request: Request) -> None:
    """The sign-in form posts to ``BASE_URL``, which may name another origin
    than the one the page is served on: that origin is allowed on this response
    and nothing else is.

    The client's ``redirect_uri`` is not: a successful sign-in answers with a
    page that navigates there (:func:`_grant_response`), not with a redirect
    the form submission would follow."""
    add_response_sources(request, "form-action", origin_source(_base_url()))


# The escaping Starlette's ``RedirectResponse`` applies to ``Location``, so the
# browser is sent to the URL a ``302`` would have named.
_REDIRECT_URL_SAFE = ":/%#?=@[]!$&'()*+,;"


def _return_to_client_html(*, target: str, client_name: str, app_name: str) -> str:
    """The page a successful sign-in answers with: it sends the browser on to
    ``target`` (the client's validated ``redirect_uri``, the code appended) by a
    declarative refresh, with a link for a browser that does not follow one.

    Not a ``302``: browsers check ``form-action`` on every redirect of the
    navigation a form submission starts, and an MCP client's callback often
    redirects again - to its own front end, an origin the consent page cannot
    know - so the sign-in stopped there once the policy was enforced.
    The navigation this page starts is not a form submission, so none of its
    hops meets ``form-action``, the footing the session path (a script
    navigation, see ``authorize_session``) is already on. The form itself stays
    held to ``form-action``, which is what stops a form the page did not write
    from posting the password typed on it elsewhere. No script, so the page
    needs nothing more from its policy; a refresh replaces the page in the
    session history, so Back does not land on it.
    """
    url = html_module.escape(quote(target, safe=_REDIRECT_URL_SAFE), quote=True)
    client_name = html_module.escape(client_name)
    app_name = html_module.escape(app_name)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="0; url={url}">
<title>Returning to {client_name} – {app_name}</title>
<style>
  body {{
    margin: 0;
    font-family: 'IBM Plex Sans', sans-serif;
    background: hsl(220, 56%, 6%);
    color: rgba(255,255,255,0.8);
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 100vh;
  }}
  p {{ font-size: 14px; line-height: 1.6; text-align: center; padding: 0 24px; }}
  strong {{ color: hsl(197, 100%, 53%); font-weight: 500; }}
  a {{ color: hsl(197, 100%, 53%); }}
</style>
</head>
<body>
<p>Signed in. Returning to <strong>{client_name}</strong>&hellip;<br>
If nothing happens, <a id="return-to-client" href="{url}">continue to {client_name}</a>.</p>
</body>
</html>"""


def _grant_response(target: str, *, client_name: str, app_name: str) -> HTMLResponse:
    """How every successful grant of ``/mcp/oauth/authorize`` answers a form:
    the page of :func:`_return_to_client_html`, never a ``302``. A new
    grant path (a second factor, another sign-in method) answers through this
    too; ``tests/test_mcp_oauth_authorize.py`` fails if the module builds a
    ``RedirectResponse`` again.

    ``no-store`` and ``Pragma: no-cache``, as the token endpoint: the page
    carries a single-use authorization code."""
    return HTMLResponse(
        _return_to_client_html(target=target, client_name=client_name, app_name=app_name),
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.get("/authorize")
async def authorize_get(
    request: Request,
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    code_challenge: str = Query(...),
    code_challenge_method: str = Query("S256"),
    state: str = Query(""),
    resource: str = Query(""),
    scope: Annotated[str, Query()] = "",
    nonce: Annotated[str, Query()] = "",
    db: AsyncSession = Depends(get_db),
):
    """OAuth 2.1 Authorization Endpoint (GET).

    Serves a login + consent page.  After the user authenticates and
    consents, an authorization code is issued and the user-agent is
    redirected to ``redirect_uri``.
    """
    if response_type != "code":
        raise HTTPException(
            status_code=400, detail="Only response_type=code is supported"
        )

    client = await _load_client(
        db, client_id, request, redirect_uri=redirect_uri, code_challenge_method=code_challenge_method
    )
    if not client:
        raise HTTPException(status_code=400, detail="Unknown client_id")

    # The redirect_uri must always match a registered one. An empty
    # registration list means "no URIs registered" → reject (never "allow
    # any"), so a client registered without redirect_uris can't have a code
    # sent to an arbitrary callback. A loopback one matches whatever its port.
    if not redirect_uri_matches(redirect_uri, client.redirect_uris):
        raise HTTPException(status_code=400, detail="redirect_uri not registered")
    _check_oidc_params(scope, nonce)

    _app_name = (await get_branding(db)).name

    _allow_consent_form_target(request)
    return HTMLResponse(
        _consent_html(
            **_client_presentation(client, client_id, redirect_uri),
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            response_type=response_type,
            resource=resource,
            scope=scope,
            nonce=nonce,
            product_sign_in=bool(await _granted_oidc_scope_for(db, scope, client)),
            app_name=_app_name,
        )
    )


async def _grant_authorization_code(
    db: AsyncSession,
    *,
    user: User,
    client_row: OAuthClient,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    code_challenge_method: str,
    resource: str,
    request: Request,
    login_method: str,
    mfa: bool = False,
    scope: str = "",
    nonce: str = "",
) -> str:
    """Mint an authorization code for ``user`` and return the redirect target
    (``redirect_uri`` + ``?code=...&state=...``).

    Single source of truth shared by the local-form path (``authorize_post``)
    and the session-consent path (``authorize_session``) so the code-issuing,
    audit, and redirect-construction can never drift between them. The
    redirect URI is validated *before* the code is stored so a bad one never
    leaves an orphaned code in Redis.
    """
    parsed = urlparse(redirect_uri)
    # A fragment must be rejected: the redirect is built by appending
    # ``?code=...`` to the URI, so a registered ``redirect_uri`` carrying a
    # ``#fragment`` would yield ``...#fragment?code=...`` — the code lands in
    # the fragment, breaking the exchange and violating OAuth redirect rules.
    if parsed.fragment:
        raise HTTPException(
            status_code=400, detail="redirect_uri must not contain a fragment"
        )
    # Enforce the OAuth 2.1 transport rule (loopback host or HTTPS), not just a
    # bare http/https scheme — the same contract registration applies.
    if not _validate_redirect_uri(redirect_uri):
        raise HTTPException(
            status_code=400,
            detail="Invalid redirect_uri (must be a loopback host or HTTPS)",
        )

    # Reject an unsupported PKCE method up front — the token endpoint only
    # verifies S256 (OAuth 2.1; matches SUPPORTED_CODE_CHALLENGE_METHODS and the
    # advertised metadata), so minting a code for anything else would store an
    # un-exchangeable grant in Redis (and churn it for malformed requests).
    if code_challenge_method not in SUPPORTED_CODE_CHALLENGE_METHODS:
        raise HTTPException(
            status_code=400, detail="Unsupported code_challenge_method"
        )

    _check_oidc_params(scope, nonce)
    requested_openid = "openid" in scope.split()
    scope = await _granted_oidc_scope_for(db, scope, client_row)
    # A product's sign-in client is for signing in only. Its sign-in no longer
    # grantable here (registration gone or inactive, whatever the page saw) is
    # refused, never turned into an MCP grant authorized with no consent.
    product_sign_in = client_id.startswith(PLATFORM_CLIENT_PREFIX) and requested_openid
    if product_sign_in and not scope:
        raise HTTPException(status_code=400, detail="Product sign-in is not available for this client")

    code = secrets.token_urlsafe(32)
    await _store_auth_code(
        code,
        client_id=client_id,
        redirect_uri=redirect_uri,
        user_id=str(user.id),
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        resource=resource or None,
        scope=scope or None,
        nonce=(nonce or None) if scope else None,
    )

    # Audit the grant — same visibility as a local ``auth.login``, so an admin
    # can trace which MCP client an account authorized, how, and from where.
    await log_audit(
        db,
        user=user,
        action="login",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.full_name,
        summary=(
            f"Signed in to {client_row.client_name or client_id} through XTM One ({user.email})"
            if product_sign_in
            else f"Authorized MCP OAuth client ({user.email})"
        ),
        details={
            "login_method": login_method,
            "client_id": client_id,
            "client_name": client_row.client_name,
            **({"mfa": True} if mfa else {}),
            # A product sign-in through a session is authorized with no consent
            # page. ``login_method`` stays, the login telemetry keys on it.
            **({"consent": False} if login_method == "mcp_oauth_session" and product_sign_in else {}),
        },
        request=request,
    )

    # RFC 9207 (MCP 2026-07-28): the response names the issuer, so a client
    # talking to several authorization servers cannot be handed this code by
    # another one (mix-up).
    params: dict[str, str] = {"code": code}
    if state:
        params["state"] = state
    params["iss"] = _base_url()

    safe_base = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )
    sep = "&" if "?" in safe_base else "?"
    return f"{safe_base}{sep}{urlencode(params)}"


async def _resolve_session_user(request: Request, db: AsyncSession) -> User:
    """Resolve the platform session user from the ``Authorization: Bearer``
    header for the session-consent path.

    Mirrors the local-JWT branch of ``app.api.deps.get_current_user``
    (decode -> load -> active -> ``password_changed_at`` staleness) and adds a
    token-type-confusion guard: an MCP OAuth *access* token
    (``type == mcp_oauth``) is a resource credential minted for ``/mcp/user``
    — it must never be replayable here to authorize further OAuth grants.
    """
    from app.core.security import decode_access_token, is_confined_token

    # HTTP auth schemes are case-insensitive (RFC 7235), and FastAPI's
    # ``HTTPBearer`` accepts e.g. ``bearer`` — parse the scheme the same way so
    # a valid client doesn't get a surprising 401 over casing.
    auth_header = request.headers.get("Authorization", "")
    scheme, _, credentials = auth_header.partition(" ")
    token = credentials.strip()
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    try:
        payload = decode_access_token(token)
        user_id = uuid.UUID(payload["sub"])
    except Exception as exc:
        raise HTTPException(
            status_code=401, detail="Invalid or expired token"
        ) from exc

    # Reject any confined credential replayed as a platform session — the
    # shared predicate covers both marker claims, so the MFA challenge token
    # (``scope``, no ``type``) is refused here exactly as a resource token
    # (``type``) is; see ``is_confined_token`` for why one predicate serves
    # every session surface.
    if is_confined_token(payload):
        raise HTTPException(status_code=401, detail="Invalid token type")

    # A session past its absolute lifetime or idle too long authorizes
    # nothing more; the consent page sends the person to sign in.
    from app.core.sessions import SessionEndedError, verify_session

    try:
        web_session = await verify_session(payload, token)
    except SessionEndedError as exc:
        raise HTTPException(status_code=401, detail="Session expired, please sign in again") from exc

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or inactive")
    # As ``deps`` marks it: a product sign-in refuses an impersonation (authorize_session).
    if payload.get("impersonated_by") is not None:
        user.impersonated_by = payload["impersonated_by"]

    # Reject tokens issued before the user's last password change, so a
    # password change/reset invalidates older sessions (matches get_current_user).
    if user.password_changed_at is not None:
        iat = payload.get("iat")
        if iat is None or iat < int(user.password_changed_at.timestamp()):
            raise HTTPException(
                status_code=401, detail="Session expired, please sign in again"
            )

    # A phone's session locked behind its PIN grants nothing either, an MCP
    # client least of all: its token would outlive the lock (423, or 401 for a
    # session its PIN ended), as on every route that takes a web session.
    from app.services.app_lock import enforce as enforce_app_lock

    user.web_session = web_session
    await enforce_app_lock(db, user)
    return user


@router.post("/authorize")
async def authorize_post(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    state: str = Form(""),
    code_challenge: str = Form(...),
    code_challenge_method: str = Form("S256"),
    response_type: str = Form("code"),
    resource: str = Form(""),
    scope: Annotated[str, Form()] = "",
    nonce: Annotated[str, Form()] = "",
    mfa_token: str = Form(""),
    code: str = Form(""),
    db: AsyncSession = Depends(get_db),
):
    """OAuth 2.1 Authorization Endpoint (POST).

    Validates user credentials, generates an authorization code, and sends
    the browser on to the client's ``redirect_uri`` - by a page that navigates
    there, not a ``302`` (:func:`_grant_response`).

    An account with two-factor enabled gets no code for its password alone:
    the password step answers with the page's code step, carrying the
    scoped challenge ``POST /auth/login`` hands out, and the code is granted
    once ``mfa_token`` + ``code`` pass ``auth.complete_mfa_challenge``, the
    check behind ``POST /auth/mfa/verify``.

    The credential check is intentionally kept in lockstep with the
    canonical local-login contract in ``app.api.v1.auth.login`` — this is
    a public, browser-facing, password-checking endpoint and must not
    drift from it. Concretely that means: per-IP rate limiting, the
    ``local_auth_enabled`` gate (SSO-only platforms reject local
    passwords here), case-insensitive email resolution, and the
    ``hashed_password`` + ``verify_password`` checks. An SSO-only account
    has ``hashed_password is None`` and is rejected as invalid
    credentials rather than crashing (regression guard: the old
    code read a non-existent ``user.password_hash`` and 500'd).
    """
    # Throttle this unauthenticated, credential-checking endpoint per
    # client IP — same envelope as ``auth.login`` — so it cannot be used
    # for password brute-force / credential stuffing.
    await enforce_rate_limit(
        request, "mcp-oauth-authorize", limit=10, window_seconds=60
    )

    client_row = await _load_client(
        db, client_id, request, redirect_uri=redirect_uri, code_challenge_method=code_challenge_method
    )
    if not client_row:
        raise HTTPException(status_code=400, detail="Unknown client_id")

    # Empty registration list means "no URIs registered" → reject (never "allow
    # any") so a code can't be sent to an arbitrary callback (matches the GET and
    # session paths).
    if not redirect_uri_matches(redirect_uri, client_row.redirect_uris):
        raise HTTPException(status_code=400, detail="redirect_uri not registered")
    _check_oidc_params(scope, nonce)
    product_sign_in = bool(await _granted_oidc_scope_for(db, scope, client_row))

    _app_name = (await get_branding(db)).name

    def _consent_error(error: str, *, mfa_token: str = "") -> HTMLResponse:
        """Re-render the consent/login page with an inline error (HTTP 200).

        Keeps the browser on the consent page so the user can retry, with
        all OAuth params and styling preserved — the same UX the GET
        handler serves. Centralised here so every non-fatal failure
        (disabled local auth, invalid credentials, disabled account, a
        wrong code) renders identically. With ``mfa_token`` it renders the
        code step instead of the email and password form, kept out of every
        cache since the page carries the challenge.
        """
        _allow_consent_form_target(request)
        return HTMLResponse(
            _consent_html(
                **_client_presentation(client_row, client_id, redirect_uri),
                error=error,
                client_id=client_id,
                redirect_uri=redirect_uri,
                state=state,
                code_challenge=code_challenge,
                code_challenge_method=code_challenge_method,
                response_type=response_type,
                resource=resource,
                scope=scope,
                nonce=nonce,
                product_sign_in=product_sign_in,
                app_name=_app_name,
                mfa_token=mfa_token,
            ),
            status_code=200,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"} if mfa_token else None,
        )

    # SSO-only platforms (local auth disabled) must not authenticate a
    # local password here — mirrors the 403 gate in ``auth.login``. Refusing cleanly
    # (instead of probing a password the account doesn't have) is the
    # consistent behaviour.
    from app.core.sso_helpers import get_sso_config

    cfg = await get_sso_config(db)
    if not cfg.local_auth_enabled:
        return _consent_error(
            "Local authentication is disabled. Please sign in through your "
            "identity provider."
        )

    if mfa_token:
        # Second step of a password sign-in for an account with two-factor
        # enabled. The code is checked by the very function behind
        # ``POST /auth/mfa/verify``: one check, so this page can never grant on
        # a weaker one. Never falls back to the email and password.
        from app.api.v1.auth import (
            MFA_INVALID_CODE_DETAIL,
            MfaInvalidCodeError,
            complete_mfa_challenge,
        )

        # A 429 or 503 (the shared ``mfa-verify`` budget) is raised as it is,
        # like this route's own rate limit at the top.
        try:
            user, _challenge, factor = await complete_mfa_challenge(
                db, mfa_token, code, request=request, context="mcp-oauth"
            )
        except MfaInvalidCodeError:
            # The failure audit is already committed; this keeps it that way
            # for any other write the check made before answering.
            await db.commit()
            return _consent_error(MFA_INVALID_CODE_DETAIL, mfa_token=mfa_token)
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            await db.commit()
            return _consent_error(
                "Your verification session has expired. Please sign in again."
            )
        target = await _grant_authorization_code(
            db,
            user=user,
            client_row=client_row,
            client_id=client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
            code_challenge_method=code_challenge_method,
            resource=resource,
            scope=scope,
            nonce=nonce,
            request=request,
            login_method="mcp_oauth",
            # No factor when two-factor was turned off while the challenge was
            # in flight: the audit claims only what was checked.
            mfa=factor is not None,
        )
        # Commit before answering: the consumed step or recovery code must be
        # on record before the code leaves, as in ``auth.mfa_verify``.
        await db.commit()
        return _grant_response(target, client_name=client_row.client_name or client_id, app_name=_app_name)

    # Case-insensitive lookup so accounts stored with a mixed-case email
    # (legacy / SSO-provisioned rows) still resolve — matches every other
    # auth path (``auth.login`` / ``register`` / ``forgot_password``). The
    # prior ``User.email == email.lower().strip()`` was case-sensitive in
    # Postgres and silently missed those rows.
    result = await db.execute(
        select(User).where(func.lower(User.email) == email.strip().lower())
    )
    user = result.scalar_one_or_none()

    from app.core.security import verify_password_async

    # ``hashed_password`` is the actual model field. The old code read a
    # non-existent ``user.password_hash`` (→ AttributeError → 500).
    # SSO-only accounts have it as ``None``; the short-circuit guarantees
    # ``verify_password_async`` is never called with a ``None`` hash.
    if (
        not user
        or not user.hashed_password
        or not await verify_password_async(password, user.hashed_password)
    ):
        return _consent_error("Invalid email or password.")

    if not user.is_active:
        return _consent_error("Your account is disabled.")

    # Second factor: the same predicate and the same challenge as
    # ``auth.login``, so an enrolled account gets no code for its password.
    from app.services.mfa import challenge_required

    if challenge_required(user):
        from app.api.v1.auth import begin_mfa_challenge

        challenge = await begin_mfa_challenge(
            db, user, request=request, login_method="mcp_oauth"
        )
        return _consent_error("", mfa_token=challenge)

    target = await _grant_authorization_code(
        db,
        user=user,
        client_row=client_row,
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        resource=resource,
        scope=scope,
        nonce=nonce,
        request=request,
        login_method="mcp_oauth",
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return _grant_response(target, client_name=client_row.client_name or client_id, app_name=_app_name)


@router.post("/authorize/session")
async def authorize_session(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """OAuth 2.1 Authorization Endpoint — session-based consent (JSON).

    Lets a user who already holds a valid platform session approve an MCP
    client without re-entering a local password.  The SPA stores its JWT in
    ``localStorage``; the consent page reads it and POSTs it here (as a
    Bearer token) together with the OAuth params, and gets back the
    ``redirect_url`` to send the browser to.

    This is what unblocks SSO-only users: the standalone consent
    *form* only ever supported local ``email`` + ``password`` and rejected
    SSO accounts (which have no local ``hashed_password``).  With this
    endpoint the consent page can instead reuse the user's existing session —
    obtained through the normal SSO login flow — so SSO config never has to
    be duplicated onto a second auth surface.
    """
    user = await _resolve_session_user(request, db)

    try:
        body = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    # This endpoint is public-facing: validate that each OAuth param is a
    # string before normalizing, so a malformed payload (e.g.
    # ``{"client_id": {}}``) is a clean 400 rather than an AttributeError 500.
    def _field(name: str) -> str:
        value = body.get(name, "")
        if value is None:
            return ""
        if not isinstance(value, str):
            raise HTTPException(
                status_code=400, detail=f"Invalid '{name}' parameter"
            )
        return value

    client_id = _field("client_id").strip()
    redirect_uri = _field("redirect_uri").strip()
    state = _field("state")
    code_challenge = _field("code_challenge").strip()
    code_challenge_method = _field("code_challenge_method") or "S256"
    response_type = _field("response_type") or "code"
    resource = _field("resource")
    scope = _field("scope")
    nonce = _field("nonce")

    if response_type != "code":
        raise HTTPException(
            status_code=400, detail="Only response_type=code is supported"
        )
    if not client_id or not redirect_uri or not code_challenge:
        raise HTTPException(
            status_code=400, detail="Missing required OAuth parameters"
        )

    client_row = await _load_client(
        db, client_id, request, redirect_uri=redirect_uri, code_challenge_method=code_challenge_method
    )
    if not client_row:
        raise HTTPException(status_code=400, detail="Unknown client_id")
    # Empty registration list means "no URIs registered" → reject (never "allow
    # any") so a code can't be sent to an arbitrary callback (matches the GET and
    # form paths).
    if not redirect_uri_matches(redirect_uri, client_row.redirect_uris):
        raise HTTPException(status_code=400, detail="redirect_uri not registered")

    # A product sign-in is authorized with no consent page, so only on
    # the person's own first-hand session: an impersonating admin would land in
    # the product as the user, with no banner and no trail (the rule of the
    # Desktop handoff). Whether it can still be granted is the grant's decision.
    if (
        client_id.startswith(PLATFORM_CLIENT_PREFIX)
        and "openid" in scope.split()
        and getattr(user, "impersonated_by", None) is not None
    ):
        raise HTTPException(status_code=403, detail="A product cannot be signed in to while impersonating another user")

    target = await _grant_authorization_code(
        db,
        user=user,
        client_row=client_row,
        client_id=client_id,
        redirect_uri=redirect_uri,
        state=state,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        resource=resource,
        scope=scope,
        nonce=nonce,
        request=request,
        login_method="mcp_oauth_session",
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return JSONResponse(content={"redirect_url": target})


# ── Token Endpoint ───────────────────────────────────────────────────────────


@router.post("/token")
async def token_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """OAuth 2.1 Token Endpoint.

    Supports two grant types:

    - ``authorization_code`` — exchanges an auth code (with PKCE verifier)
      for access + refresh tokens.
    - ``refresh_token`` — exchanges a refresh token for a new access token
      (with refresh token rotation for public clients).
    """
    # Transport first, in the RFC 6749 section 5.2 envelope every other refusal
    # here uses: an OAuth client parses ``error``, never a ``detail`` body.
    if refuses_credential_transport(request):
        return JSONResponse(
            content={"error": "invalid_request", "error_description": INSECURE_TRANSPORT_DETAIL},
            status_code=400,
        )
    data = await _read_oauth_params(request)
    if data is None:
        return JSONResponse(content={"error": "invalid_request"}, status_code=400)

    grant_type = data.get("grant_type")

    # Throttled like ``register_client`` and ``authorize_post``. This endpoint
    # was unthrottled while it only served the authorization-code exchange;
    # client_credentials made it externally reachable by machine callers doing
    # a bcrypt verify per request, which is a cheap way to burn CPU.
    # A ceiling on bcrypt work per source IP, configurable because behind a
    # proxy the bucket collapses to the proxy address unless ``FORWARDED_ALLOW_IPS``
    # names it or ``trust_proxy_headers`` keys on the leftmost
    # hop - see the setting's comment for how it is sized.
    await enforce_rate_limit(
        request,
        "mcp_oauth_token",
        limit=settings.mcp_oauth_token_rate_limit,
        window_seconds=3600,
        # Fail CLOSED, unlike the platform default: this endpoint is
        # unauthenticated, reachable from outside, advertised to partners on
        # every agent card, and spends a bcrypt verify per call. If the limiter
        # backend is down, admitting unbounded bcrypt work is worse than
        # refusing — and the A2A surface this endpoint serves credentials for
        # already fails closed, so failing open here just moves the hole.
        fail_closed=True,
    )

    if grant_type == "authorization_code":
        return await _handle_authorization_code(data, db)
    elif grant_type == "refresh_token":
        return await _handle_refresh_token(data, db)

    handler = _GRANT_HANDLERS.get(grant_type)
    if handler is not None:
        return await handler(data, db)

    return JSONResponse(
        content={"error": "unsupported_grant_type"},
        status_code=400,
    )


async def _authenticate_token_client(
    db: AsyncSession, client_id: str, data: dict
) -> JSONResponse | None:
    """The ``invalid_client`` answer for a client the token endpoint refuses.

    A metadata-document client is public (its document says so, or it was
    refused): the code or refresh token it presents was bound to its
    ``client_id`` at the authorization endpoint, so there is nothing to fetch
    here. The switch is read, so turning documents off ends their refreshes
    too, and the document cache: a client whose document was refused since -
    removed, changed, its domain handed over - is refused too.
    A registered confidential client proves its secret.
    """
    invalid = JSONResponse(content={"error": "invalid_client"}, status_code=401)
    if is_metadata_document_url(client_id):
        from app.core.security_options import security_option

        if not security_option("mcp_cimd_enabled") or await cached_refusal(client_id):
            return invalid
        return None

    result = await db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))
    client_row = result.scalar_one_or_none()
    if not client_row:
        return invalid
    if client_row.client_secret_hash:
        client_secret = data.get("client_secret")
        if not client_secret or not await bcrypt_checkpw_async(
            client_secret, client_row.client_secret_hash.encode()
        ):
            return invalid
    return None


async def _handle_authorization_code(data: dict, db: AsyncSession) -> JSONResponse:
    code = data.get("code")
    code_verifier = data.get("code_verifier")
    redirect_uri = data.get("redirect_uri")
    client_id = data.get("client_id")

    if not code or not code_verifier or not client_id:
        return JSONResponse(
            content={
                "error": "invalid_request",
                "error_description": "Missing required parameters",
            },
            status_code=400,
        )

    code_data = await _consume_auth_code(code)
    if not code_data:
        return JSONResponse(
            content={
                "error": "invalid_grant",
                "error_description": "Authorization code expired or invalid",
            },
            status_code=400,
        )

    if code_data["client_id"] != client_id:
        return JSONResponse(
            content={
                "error": "invalid_grant",
                "error_description": "client_id mismatch",
            },
            status_code=400,
        )

    if redirect_uri and code_data["redirect_uri"] != redirect_uri:
        return JSONResponse(
            content={
                "error": "invalid_grant",
                "error_description": "redirect_uri mismatch",
            },
            status_code=400,
        )

    # PKCE verification — S256 only (OAuth 2.1; matches the advertised
    # ``code_challenge_methods_supported`` metadata and the grant-time guard).
    if code_data["code_challenge_method"] == "S256":
        computed = _hash_code_verifier(code_verifier)
        if computed != code_data["code_challenge"]:
            return JSONResponse(
                content={
                    "error": "invalid_grant",
                    "error_description": "PKCE verification failed",
                },
                status_code=400,
            )
    else:
        return JSONResponse(
            content={
                "error": "invalid_request",
                "error_description": "Unsupported code_challenge_method",
            },
            status_code=400,
        )

    refused = await _authenticate_token_client(db, client_id, data)
    if refused is not None:
        return refused

    user_id = code_data["user_id"]
    resource = code_data.get("resource")
    scope = code_data.get("scope") or ""

    # A sign-in resolves the account before any token is minted, so a refusal
    # leaves no refresh token behind.
    user = None
    if "openid" in scope.split():
        if not await is_active_platform_client(db, client_id):
            return JSONResponse(
                content={"error": "invalid_grant", "error_description": "Platform not registered"},
                status_code=400,
            )
        user = await db.get(User, uuid.UUID(user_id))
        if user is None or not user.is_active:
            return JSONResponse(
                content={"error": "invalid_grant", "error_description": "User not available"},
                status_code=400,
            )

    # A product sign-in: ``openid`` granted to an active platform client (both
    # checked above), authorized without consent. It gets the ID token
    # and nothing usable elsewhere: the access token the response requires is
    # bound to the client itself, whatever ``resource`` it sent, so no MCP
    # resource server accepts it (``mcp_auth._audience_matches``), and no
    # refresh token.
    sign_in = user is not None
    access_token = _create_mcp_access_token(
        user_id, client_id=client_id, resource=client_id if sign_in else resource
    )
    content: dict[str, Any] = {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    }
    if not sign_in:
        refresh_token = _generate_refresh_token()
        refresh_hash = hashlib.sha256(refresh_token.encode()).hexdigest()
        await _store_refresh_token(
            refresh_hash,
            user_id=user_id,
            client_id=client_id,
            resource=resource,
            family_started_at=time.time(),
        )
        content["refresh_token"] = refresh_token
    if user is not None:
        content["scope"] = scope
        content["id_token"] = _create_id_token(user, client_id=client_id, nonce=code_data.get("nonce"), scope=scope)

    return JSONResponse(
        content=content,
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


async def _handle_refresh_token(data: dict, db: AsyncSession) -> JSONResponse:
    refresh_token = data.get("refresh_token")
    client_id = data.get("client_id")

    if not refresh_token or not client_id:
        return JSONResponse(
            content={"error": "invalid_request"},
            status_code=400,
        )

    token_data = await _consume_refresh_token(refresh_token)
    if not token_data:
        return JSONResponse(
            content={
                "error": "invalid_grant",
                "error_description": "Refresh token expired or invalid",
            },
            status_code=400,
        )

    if token_data["client_id"] != client_id:
        return JSONResponse(
            content={
                "error": "invalid_grant",
                "error_description": "client_id mismatch",
            },
            status_code=400,
        )

    refused = await _authenticate_token_client(db, client_id, data)
    if refused is not None:
        return refused

    # A token stored before families were recorded starts its family now.
    family_started_at = token_data.get("family_started_at") or time.time()
    deadline = _refresh_family_deadline(client_id, family_started_at)
    if deadline is not None and time.time() >= deadline:
        return JSONResponse(
            content={
                "error": "invalid_grant",
                "error_description": "Refresh token expired or invalid",
            },
            status_code=400,
        )

    user_id = token_data["user_id"]
    resource = token_data.get("resource")

    new_access = _create_mcp_access_token(
        user_id, client_id=client_id, resource=resource
    )

    # Rotate refresh token (required for public clients per OAuth 2.1)
    new_refresh = _generate_refresh_token()
    new_refresh_hash = hashlib.sha256(new_refresh.encode()).hexdigest()
    await _store_refresh_token(
        new_refresh_hash,
        user_id=user_id,
        client_id=client_id,
        resource=resource,
        family_started_at=family_started_at,
    )

    return JSONResponse(
        content={
            "access_token": new_access,
            "token_type": "bearer",
            "expires_in": ACCESS_TOKEN_EXPIRE_MINUTES * 60,
            "refresh_token": new_refresh,
        },
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


# ── Token Revocation ─────────────────────────────────────────────────────────


@router.post("/revoke")
async def revoke_token(request: Request):
    """OAuth 2.0 Token Revocation (RFC 7009).

    Accepts a refresh token and removes it from Redis.  Access tokens
    are JWTs and cannot be revoked (they expire naturally).
    """
    data = await _read_oauth_params(request)
    if data is None:
        return JSONResponse(content={"error": "invalid_request"}, status_code=400)

    token = data.get("token")
    if not token:
        return JSONResponse(content={}, status_code=200)

    redis = await redis_client()
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    await redis.delete(f"{REDIS_PREFIX_REFRESH}{token_hash}")

    return JSONResponse(content={}, status_code=200)
