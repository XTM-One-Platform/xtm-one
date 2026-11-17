# SPDX-License-Identifier: Apache-2.0

"""Authentication of the MCP endpoints that take a user's credentials.

One rule for ``/mcp/user``, ``/mcp/xtm`` and the Platform MCP Servers
(``/mcp/opencti``, ``/mcp/openaev``, ``/mcp/opencrq``): an OAuth 2.1 access
token minted by ``/mcp/oauth`` for the resource the request hit, else a
personal ``fcp-`` API key resolved for the MCP surface. A refusal is a 401
whose ``WWW-Authenticate`` header points to the Protected Resource Metadata
(RFC 9728), so an MCP client can discover the authorization server.

It lives apart from the MCP Agent Endpoints (``mcp_endpoint``, Enterprise
Edition), so no Platform MCP Server file imports that module to authenticate
(pinned by ``tests/test_mcp_platform_endpoint.py``). That is a rule on
direct imports only: the modules this one builds on (the MCP OAuth server, the
API key rules, the MCP transport) may still reach Enterprise Edition code.
``/mcp/global`` keeps its own platform-key check in ``mcp_endpoint`` and reuses
the OAuth half from here.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, NoReturn

import jwt
from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.mcp_oauth import MCP_OAUTH_TOKEN_TYPE
from app.api.v1.mcp_transport import is_modern_request
from app.core.config import settings
from app.core.security import JWT_CLOCK_SKEW_LEEWAY
from app.llm.provider_access import bind_acting_user
from app.models.user import User
from app.services.api_key_access import ApiKeySurface, looks_like_api_key, resolve_api_key

logger = logging.getLogger(__name__)


def www_authenticate_header(resource_path: str) -> str:
    """Build the RFC 9728 WWW-Authenticate header value.

    Points the client to the Protected Resource Metadata URL so it can
    discover the authorization server and start the OAuth flow.
    """
    base = settings.base_url.rstrip("/")
    metadata_url = f"{base}/.well-known/oauth-protected-resource/{resource_path}"
    return f'Bearer resource_metadata="{metadata_url}"'


def raise_unauthorized(resource_path: str, detail: str = "Unauthorized") -> NoReturn:
    """Raise 401 with the correct WWW-Authenticate header (RFC 9728 §5.1)."""
    raise HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": www_authenticate_header(resource_path)},
    )


def _audience_matches(claim: Any, resource_path: str) -> bool:
    """Whether an ``aud`` claim names *this* resource server.

    RFC 8707 audience binding is the reason the ``resource`` parameter exists:
    a token minted for ``/mcp/opencti`` must not be replayable against
    ``/mcp/user``.  ``aud`` may be a string or a list (RFC 7519 §4.1.3), and
    trailing slashes are insignificant in the canonical resource identifier.
    """
    expected = f"{settings.base_url.rstrip('/')}/{resource_path}".rstrip("/")
    values = claim if isinstance(claim, list) else [claim]
    return any(isinstance(v, str) and v.rstrip("/") == expected for v in values)


async def try_oauth_token(
    token: str, db: AsyncSession, resource_path: str, *, require_audience: bool = False
) -> User | None:
    """Attempt to validate an MCP OAuth 2.1 JWT access token.

    Returns the ``User`` if valid, or ``None`` if the token is not an
    MCP OAuth token (so the caller can fall back to API key auth).

    ``resource_path`` is required rather than defaulted: it names the resource
    the caller is guarding, and a default would silently authorize one
    endpoint's tokens on another the moment a new call site forgot to pass it.

    The audience is verified here rather than by PyJWT. The MCP spec requires
    clients to send the RFC 8707 ``resource`` parameter, and ``/mcp/oauth``
    copies it into ``aud`` when it does — but PyJWT *rejects* any token
    carrying ``aud`` unless an ``audience`` is passed to ``decode``, so
    verifying it here is what keeps a spec-compliant client from being turned
    away with a blanket 401. Tokens issued without ``aud`` (a client that sent
    no ``resource``, including every token minted before audience binding
    reached this platform's own MCP client) stay valid; a token that names a
    *different* resource is refused, which is the whole point of the claim.

    ``require_audience`` refuses a token without ``aud`` too: a client speaking
    the 2026-07-28 revision MUST send the ``resource`` parameter, and the
    server MUST check the token was issued for it, so only a client of
    a handshake-era revision keeps the old acceptance.
    """
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
            leeway=JWT_CLOCK_SKEW_LEEWAY,
            options={"verify_aud": False},
        )
    except jwt.PyJWTError:
        return None

    if payload.get("type") != MCP_OAUTH_TOKEN_TYPE:
        return None

    audience = payload.get("aud")
    if require_audience and not audience:
        logger.warning(
            "Rejected an MCP OAuth token with no audience on a 2026-07-28 request (endpoint=%s)",
            resource_path,
        )
        return None
    if audience and not _audience_matches(audience, resource_path):
        logger.warning(
            "Rejected an MCP OAuth token minted for another resource (endpoint=%s)",
            resource_path,
        )
        return None

    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        return None

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or not user.is_active:
        return None
    return user


async def authenticate_user(request: Request, db: AsyncSession, resource_path: str) -> User:
    """Authenticate a user-credential MCP request (``fcp-`` key or OAuth JWT).

    Tries OAuth 2.1 JWT first, then falls back to ``fcp-`` API key auth.
    Returns 401 with ``WWW-Authenticate`` header on failure.  ``resource_path``
    is the resource the endpoint guards: the audience an OAuth token must name
    and the Protected Resource Metadata URL advertised in the 401 (RFC 9728).
    It is required, as on ``try_oauth_token``: a default would bind a new
    endpoint that forgot it to another endpoint's tokens.

    An API key is resolved for the MCP surface: scopes, expiry and the
    key's own limits through ``services/api_key_access.py``; each endpoint then
    narrows its tools to what the key may call.
    """
    raw_token: str | None = None

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        raw_token = auth_header[7:]

    if not raw_token:
        raw_token = request.headers.get("x-api-key")

    if not raw_token:
        raise_unauthorized(resource_path)

    # 1) Try OAuth JWT — bound to the resource this request actually hit.
    user = await try_oauth_token(raw_token, db, resource_path, require_audience=is_modern_request(request))
    if not user:
        # 2) Fall back to fcp- API key
        if not looks_like_api_key(raw_token):
            raise_unauthorized(resource_path, "Invalid or expired token")

        user = await resolve_api_key(
            raw_token,
            db,
            surface=ApiKeySurface.MCP,
            request=request,
            unauthorized=lambda detail: raise_unauthorized(resource_path, detail),
        )
    # Every AI call of the request is for that person, the tools that embed a
    # query with no model run before them included (``search_knowledge`` on
    # /mcp/xtm): their LLM provider exclusions hold, as on REST.
    bind_acting_user(user.id)
    return user
