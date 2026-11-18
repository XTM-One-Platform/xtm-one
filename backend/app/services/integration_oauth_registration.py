# SPDX-License-Identifier: Apache-2.0

"""OAuth clients an integration registers for itself (RFC 7591).

Some authorization servers hand no operator a client id: the app registers
itself (dynamic client registration) and the server recognises it by its
redirect URI. A catalog entry says so with ``oauth_registration``::

    "oauth_registration": {
        "registration_url": "https://auth.example.com/oauth/register",
        "resource": "https://api.example.com/v1",  # RFC 8707, optional
    }

An OAuth integration of such a provider is registered when it is created
without a client id, or at its first Authorize when it still has none. The
client is a public one (PKCE S256) unless the server insists on issuing a
secret, which is then stored encrypted like a hand-configured one. The
authorize request and the code exchange add PKCE and the ``resource``
(``app.integrations.oauth``).

The generic layer reads catalog data only: no provider name is written here.
"""

from __future__ import annotations

from typing import Any

from app.core.redis import redis_client
from app.core.security import encrypt_value
from app.services.mcp_oauth_discovery import (
    MCPOAuthDiscoveryError,
    oauth_redirect_uri,
    register_dynamic_client,
)

__all__ = [
    "IntegrationOAuthRegistrationError",
    "oauth_registration",
    "oauth_resource",
    "register_integration_client",
]

#: How long a registration in flight holds its lock: a double-clicked
#: Authorize would otherwise mint two clients, the second overwriting the
#: client id the first authorization request carries.
_LOCK_SECONDS = 30


class IntegrationOAuthRegistrationError(Exception):
    """The authorization server refused or failed the registration (operator-facing)."""


def oauth_registration(provider: str | None) -> dict[str, str] | None:
    """The catalog's ``oauth_registration`` of *provider*, ``None`` when it has none."""
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    entry: Any = PROVIDER_CATALOG.get(provider or "", {})
    registration = entry.get("oauth_registration") if isinstance(entry, dict) else None
    if not isinstance(registration, dict) or not registration.get("registration_url"):
        return None
    return {key: value for key, value in registration.items() if isinstance(value, str) and value}


def oauth_resource(provider: str | None) -> str:
    """The RFC 8707 ``resource`` *provider* asks for, ``""`` when none."""
    return (oauth_registration(provider) or {}).get("resource", "")


async def register_integration_client(db: Any, integration: Any, *, commit: bool = True) -> bool:
    """Register *integration* as an OAuth client of its provider when it needs one.

    ``True`` when a client was registered and stored on the integration,
    ``False`` when nothing was to do (an API key integration, a client id
    already there, a provider without ``oauth_registration``). Raises
    :class:`IntegrationOAuthRegistrationError` with the reason to show.
    """
    registration = oauth_registration(integration.provider)
    if registration is None or integration.auth_type != "oauth" or integration.client_id_enc:
        return False

    from app.services.branding import get_branding

    client_name = (await get_branding(db)).name
    redirect_uri = oauth_redirect_uri()
    holder = integration.id or f"{integration.user_id}:{integration.provider}"
    redis = await redis_client()
    lock_key = f"integration_dcr_register:{holder}"
    if not await redis.set(lock_key, "1", ex=_LOCK_SECONDS, nx=True):
        raise IntegrationOAuthRegistrationError(
            "An OAuth client registration is already in progress for this integration. Try again in a moment."
        )
    try:
        client = await register_dynamic_client(
            registration["registration_url"],
            client_name=client_name,
            redirect_uri=redirect_uri,
            scopes=integration.scopes or None,
        )
    except MCPOAuthDiscoveryError as exc:
        raise IntegrationOAuthRegistrationError(
            f"{str(exc).rstrip('.')}. The authorization server registers only the redirect URIs it allows: "
            f"{redirect_uri} must be on its allowlist before OAuth can be used. "
            "Use API key mode meanwhile."
        ) from exc
    finally:
        await redis.delete(lock_key)

    integration.client_id_enc = encrypt_value(client.client_id)
    integration.client_secret_enc = encrypt_value(client.client_secret) if client.client_secret else None
    if commit:
        await db.commit()
    return True
