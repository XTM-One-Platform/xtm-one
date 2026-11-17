# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""OAuth app credentials an administrator's agent prepares, with no browser.

Two admin tools served with the ``platform_insights`` suite, next to the OAuth
defaults tools of :mod:`.platform_admin`:

* ``create_integration_app_secret`` adds (and, with ``replace_key_id``,
  rotates) a client secret on an identity provider's app registration with an
  app-only credential the platform already holds - an integration in API key
  mode of a provider that declares an
  :class:`~app.integrations.app_secrets.AppSecretMinter` - never a user's
  portal token. The secret is written straight into its target, a provider's
  admin OAuth default or an integration row, and never returned: the answer
  carries the key id, the expiry and the target's booleans.
* ``apply_oauth_default_to_integration`` puts an OAuth integration on its
  provider's admin OAuth default (``integration_state.put_on_admin_oauth_app``,
  the create path's rule), so a person has one Authorize click left, and reads
  the row back.

Every answer names the platform written (``platform``: its public URL). The
acting user must be an administrator, checked at every call; each change is
audited without a secret value. No provider is named here: the minter comes
from the integration seam (invariant 18).
"""

from __future__ import annotations

import json
from typing import Any

from app.core import database
from app.core.utils import format_exception_message, safe_uuid

from ._shared import logger

CREATE_INTEGRATION_APP_SECRET = "create_integration_app_secret"  # noqa: S105 - a tool name, not a secret
APPLY_OAUTH_DEFAULT_TO_INTEGRATION = "apply_oauth_default_to_integration"

_FORBIDDEN = (
    "Error 403: only an administrator can create app secrets or apply OAuth defaults; "
    "the person this agent acts for is not one."
)


def platform_label() -> str:
    """The public URL of this platform: the one every answer names as written."""
    from app.services.email import platform_url

    return platform_url()


async def _admin(db: Any, user_id: str) -> Any | None:
    from .platform_admin import _admin as admin_user

    return await admin_user(db, user_id)


def _minter(credential_provider: str) -> tuple[Any | None, str | None]:
    """The provider seam that mints app secrets, its integrations loaded first."""
    # Importing the integration tool registry imports every provider package,
    # which registers its minter (app.integrations.app_secrets).
    import app.services.integration_tools  # noqa: F401
    from app.integrations.app_secrets import resolve_minter

    return resolve_minter(credential_provider)


async def _credential_token(db: Any, minter: Any, credential_integration_id: str) -> tuple[str | None, str | None]:
    """The app-only token of the minter's credential integration, or the error sentence."""
    from sqlalchemy import select

    from app.models.integration import Integration

    query = select(Integration).where(
        # access_restricted: bypass - administrator-only tool, checked before this call.
        Integration.provider == minter.provider,
        Integration.auth_type != "oauth",
    )
    if credential_integration_id:
        parsed = safe_uuid(credential_integration_id)
        if parsed is None:
            return None, "Error: credential_integration_id must be a UUID."
        query = query.where(Integration.id == parsed)
    rows = list((await db.execute(query)).scalars().all())
    if not rows:
        if credential_integration_id:
            return None, (
                f"Error 404: integration {credential_integration_id} is not a {minter.display_name} integration "
                "in app-only (API key) mode."
            )
        return None, minter.missing_credential
    if len(rows) > 1:
        names = ", ".join(f"{row.name} ({row.id})" for row in rows)
        return None, (
            f"Error: several app-only {minter.display_name} credentials exist ({names}); "
            "name one as credential_integration_id."
        )
    return await minter.read_credential(rows[0])


def _row_readback(integration: Any) -> dict[str, Any]:
    return {
        "id": str(integration.id),
        "name": integration.name,
        "provider": integration.provider,
        "auth_type": integration.auth_type,
        "status": integration.status,
        "has_client_id": bool(integration.client_id_enc),
        "has_client_secret": bool(integration.client_secret_enc),
        "uses_admin_oauth_credentials": integration.uses_admin_oauth_credentials,
    }


def _default_readback(default: Any) -> dict[str, Any]:
    return {
        "provider": default.provider,
        "has_client_id": bool(default.client_id_enc),
        "has_client_secret": bool(default.client_secret_enc),
        "has_extra_config": bool(default.extra_config_enc),
    }


async def _load_integration(db: Any, integration_id: str) -> tuple[Any | None, str | None]:
    from sqlalchemy import select

    from app.models.integration import Integration

    parsed = safe_uuid(integration_id) if integration_id else None
    if parsed is None:
        return None, "Error: integration_id must be a UUID."
    # access_restricted: bypass - administrator-only tool, checked before this call.
    row = (await db.execute(select(Integration).where(Integration.id == parsed))).scalar_one_or_none()
    if row is None:
        return None, f"Error 404: no integration {integration_id} on {platform_label()}."
    return row, None


async def _store_secret(db: Any, *, app_id: str, value: str, provider: str, integration: Any | None) -> dict[str, Any]:
    """Write *value* as the client secret of the target; the target's readback."""
    from sqlalchemy import select

    from app.core.security import encrypt_value
    from app.models.oauth_default import OAuthDefault
    from app.services.integration_state import replace_oauth_client_id

    if integration is not None:
        # The secret belongs to this app: a row on another app moves to it
        # (its tokens were issued to the other one and are cleared).
        replace_oauth_client_id(integration, app_id)
        integration.client_secret_enc = encrypt_value(value)
        if integration.auth_type == "oauth" and integration.status != "connected":
            integration.status = "configured"
        await db.flush()
        return {"integration": _row_readback(integration)}
    default = (
        await db.execute(select(OAuthDefault).where(OAuthDefault.provider == provider))
    ).scalar_one_or_none()
    if default is None:
        default = OAuthDefault(provider=provider, client_id_enc=encrypt_value(app_id), client_secret_enc=encrypt_value(value))
        db.add(default)
    else:
        default.client_id_enc = encrypt_value(app_id)
        default.client_secret_enc = encrypt_value(value)
    await db.flush()
    return {"oauth_default": _default_readback(default)}


def build_platform_admin_credential_tools(user_id: str) -> tuple[list[Any], dict[str, Any]]:
    """The two credential tools acting for *user_id*: (definitions, {name: handler})."""
    from app.tools.base import ToolDefinition

    async def _create_app_secret(**args: Any) -> str:
        from app.api.v1.integration_catalog import PROVIDER_CATALOG
        from app.services.audit import log_audit

        app_id = str(args.get("app_id") or "").strip()
        object_id = str(args.get("object_id") or "").strip()
        provider = str(args.get("oauth_default_provider") or "").strip()
        integration_id = str(args.get("integration_id") or "").strip()
        replace_key_id = str(args.get("replace_key_id") or "").strip()
        display_name = str(args.get("display_name") or "").strip() or "XTM One"
        try:
            days = int(args.get("days") or 180)
        except (TypeError, ValueError):
            return "Error: days must be a whole number of days."
        if not app_id and not object_id:
            return "Error: app_id or object_id is required."
        if bool(provider) == bool(integration_id):
            return "Error: name exactly one target: oauth_default_provider or integration_id."
        if provider and provider not in PROVIDER_CATALOG:
            return f"Error: unknown provider '{provider}' (use the integration catalog's provider key)."
        minter, problem = _minter(str(args.get("credential_provider") or "").strip())
        if problem or minter is None:
            return problem or "Error: no integration provider on this platform can mint app secrets."
        days = max(1, min(days, minter.max_days))
        try:
            async with database.async_session() as db:
                user = await _admin(db, user_id)
                if user is None:
                    return _FORBIDDEN
                integration = None
                if integration_id:
                    integration, problem = await _load_integration(db, integration_id)
                    if problem:
                        return problem
                token, problem = await _credential_token(
                    db, minter, str(args.get("credential_integration_id") or "").strip()
                )
                if problem or not token:
                    return problem or minter.missing_credential
                app, problem = await minter.find_app(token, app_id=app_id, object_id=object_id)
                if problem or app is None:
                    return problem or "Error: the app registration could not be read."
                secret, problem = await minter.add_secret(token, app, display_name=display_name, days=days)
                if problem or secret is None:
                    return problem or "Error: no client secret was added."
                try:
                    target = await _store_secret(
                        db, app_id=app.app_id, value=secret.value, provider=provider, integration=integration
                    )
                    await log_audit(
                        db,
                        user=user,
                        action="configure",
                        entity_type="integration" if integration is not None else "oauth_default",
                        entity_id=getattr(integration, "id", None),
                        entity_name=getattr(integration, "name", None) or provider,
                        summary=f"Created a client secret on {minter.display_name} app {app.app_id} and stored it",
                        details={
                            "credential_provider": minter.provider,
                            "app_id": app.app_id,
                            "key_id": secret.key_id,
                            "expires_at": secret.expires_at,
                        },
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    # Nothing stores the new secret: take it back off the app.
                    await minter.remove_secret(token, app, secret.key_id)
                    raise
                answer: dict[str, Any] = {
                    "platform": platform_label(),
                    "identity_provider": minter.display_name,
                    "app": {"app_id": app.app_id, "object_id": app.object_id, "display_name": app.display_name},
                    "secret": {"key_id": secret.key_id, "expires_at": secret.expires_at, "display_name": secret.display_name},
                    "stored_on": target,
                }
                if replace_key_id:
                    removed = await minter.remove_secret(token, app, replace_key_id)
                    answer["replaced_key_id"] = replace_key_id if removed is None else None
                    if removed:
                        answer["replace_error"] = removed
                return json.dumps(answer, ensure_ascii=False, default=str)
        except Exception as exc:
            logger.error("%s failed: %s", CREATE_INTEGRATION_APP_SECRET, format_exception_message(exc), exc_info=True)
            return "Error: the client secret could not be created and stored right now; nothing was stored."

    async def _apply_oauth_default(**args: Any) -> str:
        from app.core.security import decrypt_value
        from app.services.audit import log_audit
        from app.services.integration_state import (
            admin_oauth_app,
            put_on_admin_oauth_app,
            replace_oauth_client_id,
        )

        integration_id = str(args.get("integration_id") or "").strip()
        replace = bool(args.get("replace"))
        try:
            async with database.async_session() as db:
                user = await _admin(db, user_id)
                if user is None:
                    return _FORBIDDEN
                integration, problem = await _load_integration(db, integration_id)
                if problem:
                    return problem
                if integration.auth_type != "oauth":
                    return (
                        f"Error: integration {integration.name} uses auth_type '{integration.auth_type}'; "
                        "an OAuth default applies to an OAuth integration only."
                    )
                app = await admin_oauth_app(db, integration.provider)
                if app is None:
                    return (
                        f"Error 404: no readable OAuth default for provider '{integration.provider}' on "
                        f"{platform_label()}: store one with upsert_oauth_default (or create_integration_app_secret) first."
                    )
                try:
                    stored = decrypt_value(integration.client_id_enc) if integration.client_id_enc else ""
                except Exception:
                    stored = ""
                same_app = stored == app.client_id
                if stored and not same_app and not replace:
                    return (
                        f"Error 409: integration {integration.name} is on another OAuth app; pass replace=true "
                        "to move it to the default (its tokens are cleared and it must be authorized again)."
                    )
                status_before = integration.status
                replace_oauth_client_id(integration, app.client_id)
                put_on_admin_oauth_app(integration, app)
                if same_app and status_before == "connected":
                    integration.status = status_before
                await log_audit(
                    db,
                    user=user,
                    action="configure",
                    entity_type="integration",
                    entity_id=integration.id,
                    entity_name=integration.name,
                    summary=f"Put integration '{integration.name}' on the admin OAuth default of '{integration.provider}'",
                    details={"provider": integration.provider},
                )
                await db.commit()
                readback = _row_readback(integration)
                next_step = (
                    "Nothing: the integration is connected."
                    if readback["status"] == "connected"
                    else "A person clicks Authorize on this integration in the Integrations page."
                )
                return json.dumps(
                    {"platform": platform_label(), "integration": readback, "next_step": next_step},
                    ensure_ascii=False,
                    default=str,
                )
        except Exception as exc:
            logger.error("apply_oauth_default_to_integration failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: the OAuth default could not be applied right now."

    definitions = [
        ToolDefinition(
            source="builtin:platform_insights",
            name=CREATE_INTEGRATION_APP_SECRET,
            description=(
                "Create a client secret on an identity provider's app registration (app_id or object_id) with "
                "this platform's app-only credential for that provider (an integration in API key mode of a "
                "provider able to mint app secrets; credential_provider when several providers can, "
                "credential_integration_id when several credentials exist), never a user's token, and store it "
                "directly on ONE target: a provider's admin OAuth default (oauth_default_provider, the catalog "
                "key) or an integration row (integration_id). The secret value is never returned: the answer "
                "gives the key id, the expiry, the target's has_client_id / has_client_secret and the platform "
                "written. replace_key_id removes that older secret once the new one is stored (rotation). "
                "Administrators only (403 otherwise), audited."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "app_id": {"type": "string", "description": "Application (client) id of the app registration."},
                    "object_id": {"type": "string", "description": "Object id of the app registration (instead of app_id)."},
                    "oauth_default_provider": {"type": "string"},
                    "integration_id": {"type": "string"},
                    "credential_provider": {
                        "type": "string",
                        "description": "Catalog key of the provider holding the app-only credential (when several can mint).",
                    },
                    "credential_integration_id": {"type": "string"},
                    "days": {"type": "integer", "minimum": 1, "maximum": 730, "default": 180},
                    "display_name": {"type": "string"},
                    "replace_key_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
        ),
        ToolDefinition(
            source="builtin:platform_insights",
            name=APPLY_OAUTH_DEFAULT_TO_INTEGRATION,
            description=(
                "Put an OAuth integration on its provider's admin OAuth default (client id, secret and the "
                "default's extra config when the row has none), so a person only clicks Authorize. A row on "
                "another OAuth app is refused unless replace=true (its tokens are then cleared). Answers the row's "
                "status, has_client_id, has_client_secret, uses_admin_oauth_credentials, the next step and the "
                "platform written. Administrators only (403 otherwise), audited."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "integration_id": {"type": "string"},
                    "replace": {"type": "boolean", "default": False},
                },
                "required": ["integration_id"],
                "additionalProperties": False,
            },
        ),
    ]
    return definitions, {
        CREATE_INTEGRATION_APP_SECRET: _create_app_secret,
        APPLY_OAUTH_DEFAULT_TO_INTEGRATION: _apply_oauth_default,
    }
