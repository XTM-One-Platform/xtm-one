# SPDX-License-Identifier: Apache-2.0

"""The sign-in client of a registered platform.

A product signs its users in through XTM One with OpenID Connect. Its
OAuth client comes from platform registration (``POST /api/v1/platform/register``,
authenticated by the registration token), not from dynamic client registration,
which anyone can call: only a platform registered with the configured token
gets the OpenID Connect scopes, ``xtm_role`` included.

The product declares its callback URLs in ``sign_in_redirect_uris``; each
registration (the heartbeat included) keeps the client in step, and
unregistering removes it. The client is public (PKCE), keyed by
:func:`client_id_for`, and never evicted by the bound on dynamic clients.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.oauth_client import OAuthClient
from app.models.platform_registration import PlatformRegistration
from app.services.mcp_oauth_clients import redirect_uri_allowed

#: Every platform sign-in client id starts with it; dynamic registration only
#: ever mints ``mcp-...`` ids, so the two never collide.
PLATFORM_CLIENT_PREFIX = "xtm-platform-"
PLATFORM_SIGN_IN_SCOPE = "openid email profile xtm_role"
# The ID token only: no refresh token for a sign-in.
PLATFORM_SIGN_IN_GRANT_TYPES = ("authorization_code",)
#: Public client: the code is bound by PKCE, no secret to store or rotate.
PUBLIC_CLIENT = "none"


#: What a platform identifier may be when it names a sign-in client: the
#: ``platform_registrations.platform_identifier`` column holds 50 characters.
_IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9_-]{0,49}")
_DEFAULT_PORTS = {"https": 443, "http": 80}


def client_id_for(platform_identifier: str) -> str:
    return f"{PLATFORM_CLIENT_PREFIX}{platform_identifier}"


def check_identifier(platform_identifier: str) -> None:
    if not _IDENTIFIER.fullmatch(platform_identifier):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="platform_identifier is not valid for a sign-in client (a-z, 0-9, _ and -, 50 at most)",
        )


def _origin(url: str) -> tuple[str, str, int | None] | None:
    """Scheme, host and port, the default port spelled or not; ``None`` for a URL
    carrying credentials or a port that does not parse."""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return None
    if parts.username is not None or parts.password is not None:
        return None
    scheme = parts.scheme.lower()
    return scheme, (parts.hostname or "").lower(), port or _DEFAULT_PORTS.get(scheme)


def check_redirect_uris(platform_url: str, redirect_uris: list[str]) -> None:
    """A platform's callbacks are on its own origin, so the registration token
    cannot point sign-in codes at another site."""
    expected = _origin(platform_url)
    for uri in redirect_uris:
        origin = _origin(uri)
        if expected is None or origin != expected or urlsplit(uri).fragment or not redirect_uri_allowed(uri):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"sign_in_redirect_uris must be on the platform URL's origin (HTTPS or loopback): {uri}",
            )


async def sync_platform_sign_in_client(
    db: AsyncSession,
    *,
    platform_identifier: str,
    platform_title: str,
    redirect_uris: list[str],
) -> str:
    """Create or update the platform's sign-in client; return its ``client_id``.

    Writes only what changed, since every heartbeat calls it.
    """
    client_id = client_id_for(platform_identifier)
    client = (await db.execute(select(OAuthClient).where(OAuthClient.client_id == client_id))).scalar_one_or_none()
    if client is None:
        db.add(
            OAuthClient(
                client_id=client_id,
                client_secret_hash=None,
                client_name=platform_title,
                redirect_uris=list(redirect_uris),
                grant_types=list(PLATFORM_SIGN_IN_GRANT_TYPES),
                response_types=["code"],
                token_endpoint_auth_method=PUBLIC_CLIENT,
                scope=PLATFORM_SIGN_IN_SCOPE,
            )
        )
        return client_id
    if client.redirect_uris != list(redirect_uris):
        client.redirect_uris = list(redirect_uris)
    if client.client_name != platform_title:
        client.client_name = platform_title
    if client.scope != PLATFORM_SIGN_IN_SCOPE:
        client.scope = PLATFORM_SIGN_IN_SCOPE
    if client.grant_types != list(PLATFORM_SIGN_IN_GRANT_TYPES):
        client.grant_types = list(PLATFORM_SIGN_IN_GRANT_TYPES)
    return client_id


async def is_active_platform_client(db: AsyncSession, client_id: str) -> bool:
    """Whether ``client_id`` is the sign-in client of a platform whose
    registration is active: the grant follows the registration's lifecycle,
    not only the id's prefix."""
    if not client_id.startswith(PLATFORM_CLIENT_PREFIX):
        return False
    identifier = client_id.removeprefix(PLATFORM_CLIENT_PREFIX)
    registration_status = (
        await db.execute(
            select(PlatformRegistration.status).where(PlatformRegistration.platform_identifier == identifier)
        )
    ).scalar_one_or_none()
    return registration_status == "active"


async def remove_platform_sign_in_client(db: AsyncSession, platform_identifier: str) -> None:
    await db.execute(delete(OAuthClient).where(OAuthClient.client_id == client_id_for(platform_identifier)))
