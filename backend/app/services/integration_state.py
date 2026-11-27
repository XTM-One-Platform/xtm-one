# SPDX-License-Identifier: Apache-2.0

"""Connected-integration state helpers (integration seam).

Small shared query used to gate integration-contributed surfaces — a flow node,
an admin sub-page, a scheduler step — on whether the integration is actually
connected. Kept separate so every gated surface reads the same
definition of "connected" rather than re-deriving it. Also the one rule for
what replacing an integration's OAuth app does to its connection
(:func:`replace_oauth_client_id`), shared by the REST update and the agent
builder, and the one rule for whether an integration runs on the admin's OAuth
app (:func:`uses_admin_oauth_app`), shared by the integration API, the
provider-level webhook, the LinkedIn notifications and the Slack signing
secrets. And the one rule binding an API key to the address it was made for
(:func:`api_key_binding_of`, :func:`forget_key_bound_elsewhere`), declared by a
provider's catalog entry and shared by the REST update and the agent builder.
And the one rule putting an OAuth integration created or left without its own
client id on its provider's admin OAuth app (:func:`takes_admin_oauth_app`,
:func:`admin_oauth_app`, :func:`put_on_admin_oauth_app`,
:func:`repair_onto_admin_oauth_app`), shared by the REST create and update
and the agent builder's create and update, so the paths cannot drift.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    decrypt_json,
    decrypt_stored_value,
    decrypt_value,
    encrypt_value,
    timing_safe_str_eq,
)

if TYPE_CHECKING:
    from app.models.integration import Integration

logger = logging.getLogger(__name__)

# The integration status that means "usable" (see app/models/integration.py —
# api_key/bot_token/none/platform_managed land here immediately, oauth after the
# callback completes).
INTEGRATION_CONNECTED = "connected"


async def any_provider_connected(db: AsyncSession, provider: str) -> bool:
    """Whether *any* connected integration exists for *provider* (platform-level).

    For platform guardrails (e.g. Nova) the activation signal is a connected,
    company-managed integration that applies to every agent — a userless check.
    """
    from sqlalchemy import select

    from app.models.integration import Integration

    row = (
        await db.execute(
            select(Integration.id)
            .where(
                Integration.provider == provider,
                Integration.status == INTEGRATION_CONNECTED,
                # access_restricted: bypass - userless platform activation signal, reads no credential.
                Integration.is_company_managed.is_(True),
            )
            .limit(1)
        )
    ).first()
    return row is not None


def replace_oauth_client_id(integration: Integration, client_id: str) -> bool:
    """Store *client_id* as the integration's OAuth app.

    A different client id is a different OAuth app, and the tokens on the row
    were issued to the previous one: tools would keep acting through its
    grant, and its refresh token is refused once it is sent with the new
    app's credentials. So an OAuth integration loses them, and the record of
    whose they were (``oauth_token_owner_id``), and waits for an
    authorization against the new app, like one created with its own
    credentials. The admin's client id is never served, so a replacement is
    the user's own app. Typing the stored client id again changes nothing,
    and neither does rotating the client secret alone: the app and its grant
    stay the same.

    Returns whether the integration now needs an OAuth authorization.
    """
    try:
        stored = (
            decrypt_value(integration.client_id_enc)
            if integration.client_id_enc
            else ""
        )
    except Exception:
        # An unreadable stored id cannot match: storing the new one is asked.
        stored = None
    if client_id == stored:
        return False
    integration.client_id_enc = encrypt_value(client_id) if client_id else None
    integration.uses_admin_oauth_credentials = False
    if integration.auth_type != "oauth":
        return False
    integration.access_token_enc = None
    integration.refresh_token_enc = None
    integration.token_expires_at = None
    integration.oauth_token_owner_id = None
    integration.status = "configured" if client_id else "disconnected"
    return True


#: What :func:`uses_admin_oauth_app` is given for a provider whose OAuth default
#: stores a client id that cannot be decrypted.
UNREADABLE_ADMIN_CLIENT_ID = object()


def uses_admin_oauth_app(
    recorded: bool | None, client_id_enc: str | None, admin_client_id: object
) -> bool | None:
    """Whether an integration runs on its provider's admin OAuth app.

    The recorded ``uses_admin_oauth_credentials`` when there is one. A row created
    before it was recorded (NULL) is judged by its stored client id against the
    provider's current OAuth default client id: *admin_client_id* is that id,
    ``None`` when the provider has no OAuth default, or
    :data:`UNREADABLE_ADMIN_CLIENT_ID`. ``None`` when that comparison cannot be
    made; a caller that must decide reads it as "not on the admin app".
    """
    if recorded is not None:
        return recorded
    if admin_client_id is None or not client_id_enc:
        return False
    if admin_client_id is UNREADABLE_ADMIN_CLIENT_ID or not isinstance(admin_client_id, str):
        return None
    try:
        client_id = decrypt_value(client_id_enc)
    except Exception:
        return None
    return bool(client_id) and timing_safe_str_eq(client_id, admin_client_id)


@dataclass(frozen=True)
class AdminOAuthApp:
    """A provider's admin OAuth app as stored on its OAuth default.

    Decrypted for copying onto an integration only: never served, never logged.
    The secret is the stored text, so a vault reference stays a reference.
    """

    client_id: str
    client_secret: str | None
    extra_config: dict | None


def takes_admin_oauth_app(
    auth_type: str | None, *, client_id_given: bool, is_company_managed: bool
) -> bool:
    """Whether a new integration falls back to its provider's admin OAuth app.

    An OAuth integration created without its own client id, unless it is
    company-managed (its administrator brings the app they want).
    """
    return auth_type == "oauth" and not client_id_given and not is_company_managed


async def admin_oauth_app(db: AsyncSession, provider: str) -> AdminOAuthApp | None:
    """The provider's admin OAuth app, ``None`` without a readable OAuth default."""
    from sqlalchemy import select

    from app.models.oauth_default import OAuthDefault

    row = (
        await db.execute(select(OAuthDefault).where(OAuthDefault.provider == provider))
    ).scalar_one_or_none()
    if not isinstance(row, OAuthDefault) or not row.client_id_enc:
        return None
    try:
        client_id = decrypt_value(row.client_id_enc)
        client_secret = (
            decrypt_stored_value(row.client_secret_enc) if row.client_secret_enc else None
        )
        extra = json.loads(decrypt_value(row.extra_config_enc)) if row.extra_config_enc else None
    except Exception:
        logger.warning("The admin OAuth default of %s cannot be read: not applied", provider)
        return None
    if not client_id:
        return None
    return AdminOAuthApp(
        client_id=client_id,
        client_secret=client_secret or None,
        extra_config=extra if isinstance(extra, dict) and extra else None,
    )


def put_on_admin_oauth_app(integration: Integration, app: AdminOAuthApp) -> None:
    """Store *app* as the integration's OAuth app: ready to connect.

    The client id and secret are the admin's (a secret the row held without a
    client id belonged to no app); the extra config is the default's only when
    the integration has none of its own.
    """
    integration.client_id_enc = encrypt_value(app.client_id)
    integration.client_secret_enc = encrypt_value(app.client_secret) if app.client_secret else None
    if app.extra_config and not getattr(integration, "extra_config_enc", None):
        integration.extra_config_enc = encrypt_value(json.dumps(app.extra_config))
    integration.uses_admin_oauth_credentials = True
    integration.status = "configured"
    logger.info(
        "Auto-filled OAuth credentials from admin defaults for %s", integration.provider
    )


#: The auth types whose stored secret is the credential: an API key or a bot
#: token (a client id/secret pair kept in the client columns for CrowdStrike).
_TYPED_CREDENTIAL_AUTH_TYPES = frozenset({"api_key", "bot_token"})


def status_for_auth_type(integration: Integration) -> str | None:
    """The status the stored credentials give *integration* under its auth type.

    The one rule applied after an auth-type switch, by the REST update and the
    agent builder, so a status set under the previous type never survives it
    (an OAuth integration left ``configured`` and switched to an API key showed
    "Pre-configured" with its key stored and tested, #2553). A typed credential
    is connected once its key is stored, as at creation (a client id left by
    an OAuth past is that app's, not a credential: it does not count); OAuth is
    connected with a token, ``configured`` with an app and no token yet, else
    ``disconnected``.
    ``None`` for the auth types whose status has its own rule (none,
    platform-managed, the token grants): the caller keeps it.
    """
    auth_type = getattr(integration, "auth_type", None)
    if auth_type in _TYPED_CREDENTIAL_AUTH_TYPES:
        return INTEGRATION_CONNECTED if getattr(integration, "api_key_enc", None) else "disconnected"
    if auth_type == "oauth":
        if getattr(integration, "access_token_enc", None):
            return INTEGRATION_CONNECTED
        return "configured" if getattr(integration, "client_id_enc", None) else "disconnected"
    return None


def heal_status_after_test(integration: Integration, test_status: str | None) -> bool:
    """Mark a typed-credential integration connected once its own credential passed a test.

    ``True`` when the status changed. A test that reached the provider with
    the stored key proves it usable whatever status an older write left
    (``configured`` from an OAuth past, #2553); a failed test leaves the status
    alone, its verdict is ``connection_test_status``. OAuth and the other auth
    types read their status from their token or their own rule.
    """
    if (
        test_status in ("ok", "info")
        and getattr(integration, "auth_type", None) in _TYPED_CREDENTIAL_AUTH_TYPES
        and getattr(integration, "status", None) != INTEGRATION_CONNECTED
        and getattr(integration, "api_key_enc", None)
    ):
        integration.status = INTEGRATION_CONNECTED
        return True
    return False


async def repair_onto_admin_oauth_app(db: AsyncSession, integration: Integration) -> bool:
    """Put an existing OAuth integration without an app on the admin's; ``True`` if done.

    The create's rule applied to a row it missed (one created before the
    fallback reached its path, or switched back to OAuth): an OAuth integration
    that is not company-managed, holds no client id and is not connected gets
    its provider's admin OAuth app, when there is one. Its id, name, tool suffix
    and the agents bound to it are untouched.
    """
    if (
        getattr(integration, "auth_type", None) != "oauth"
        or getattr(integration, "is_company_managed", False)
        or getattr(integration, "client_id_enc", None)
        or getattr(integration, "status", None) == INTEGRATION_CONNECTED
    ):
        return False
    app = await admin_oauth_app(db, integration.provider)
    if app is None:
        return False
    put_on_admin_oauth_app(integration, app)
    return True


def api_key_binding_of(integration: Integration) -> str | None:
    """What the integration's API key is bound to, read before an edit.

    A provider whose key acts only at the address it was made for declares, on
    its catalog entry, ``api_key_bound_to``: ``{"field": <the extra field
    holding that address>, "default": <the address an empty field means>}``.
    ``None`` when the provider declares no binding. The value is the field as
    configured, trimmed and without a trailing slash, the default when empty.
    """
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    binding = (PROVIDER_CATALOG.get(integration.provider) or {}).get("api_key_bound_to")
    if not binding:
        return None
    field = binding["field"]
    default = str(binding.get("default") or "").strip().rstrip("/")
    extra: object = None
    if integration.extra_config_enc:
        try:
            extra = decrypt_json(integration.extra_config_enc)
        except Exception:  # an unreadable config reads as no config
            extra = None
    value = extra.get(field) if isinstance(extra, dict) else None
    if value is None:
        return default
    if not isinstance(value, str):
        return repr(value)
    return value.strip().rstrip("/") or default


def forget_key_bound_elsewhere(
    integration: Integration, binding_before: str | None, *, new_key_given: bool
) -> bool:
    """Forget an API key whose bound address changed; ``True`` if it was.

    The key acts as its user at the address it was made for. Someone who may
    edit the integration's address but does not hold the key (a ``config``
    grantee) must not be able to send it to a host of their choosing, so a new
    address - or the switch to no authentication, under which a kept key would
    still be stored - drops it unless the same edit brings a new one. Shared by
    the REST update and the agent builder; *binding_before* is
    :func:`api_key_binding_of` read before the edit (``None``: no binding).
    """
    if binding_before is None or not integration.api_key_enc or new_key_given:
        return False
    if api_key_binding_of(integration) == binding_before and integration.auth_type != "none":
        return False
    integration.api_key_enc = None
    if integration.auth_type == "api_key":
        integration.status = "not_connected"
    return True
