# SPDX-License-Identifier: Apache-2.0

"""The platform OpenRouter LLM provider, fed from an OpenRouter integration.

A company-managed OpenRouter integration can populate the platform's
OpenRouter LLM provider: create one from its API key, or adopt one an admin
made in Settings. The config then records the integration
(``LLMProviderConfig.source_integration_id``) and this module keeps it in step:

* the API key is copied into the config, and copied again on every edit of the
  integration that replaces it (the ``on_update`` lifecycle hook); a vault
  reference is copied as the reference, which the config resolves where it is
  used;
* the models the platform offers are picked from the integration's catalog and
  saved through the one Settings update path
  (``settings.update_provider_config``), which adds each newly enabled model's
  context window from OpenRouter's catalog when the config has none for it.

Only a company-managed integration may feed the platform provider: the provider
is shared by every user, and a personal integration's key is its owner's to
spend. An integration made private again is unlinked on its next edit - the
config keeps the key it holds - and deleting it unlinks it at the database
level (``ON DELETE SET NULL``). An env-seeded config is never adopted: the boot
seed re-applies its key from the environment. The linking itself is the
generic mechanism of ``services/integration_llm_provider.py``; this module is
the OpenRouter feed and its lifecycle hook.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_stored_value, encrypt_value
from app.integrations.lifecycle import IntegrationUpdate, register_lifecycle
from app.models.integration import Integration
from app.models.llm_config import LLMProviderConfig
from app.services import integration_llm_provider as feeds
from app.services.audit import log_audit
from app.services.integration_llm_provider import (
    LinkRefusedError,
    linked_configs,
    merged_context_windows,
    unlink_provider,
)
from app.services.vault import reference_problem

if TYPE_CHECKING:
    from fastapi import Request

    from app.api.v1.schemas.settings import LLMProviderResponse
    from app.models.user import User

logger = logging.getLogger(__name__)

PROVIDER = "openrouter"

__all__ = [
    "FEED",
    "PROVIDER",
    "LinkRefusedError",
    "adopt_provider",
    "adoptable_configs",
    "create_linked_provider",
    "link_refusal",
    "linked_configs",
    "merged_context_windows",
    "unlink_provider",
]


def _refusal(integration: Integration) -> str | None:
    if not integration.api_key_enc:
        return "Add the integration's OpenRouter API key first."
    return None


def _stored_key(integration: Integration) -> str:
    """The integration's API key as stored, for the config to hold the same: a vault reference stays one.

    The config resolves it where it is used, like any referenced key; a
    reference its provider no longer admits is refused here rather than on
    every call.
    """
    stored = decrypt_stored_value(integration.api_key_enc)
    if problem := reference_problem(stored):
        raise LinkRefusedError(f"The integration's API key cannot feed the provider: {problem}", status=422)
    return stored


FEED = feeds.IntegrationFeed(
    integration_provider=PROVIDER,
    llm_provider=PROVIDER,
    label="OpenRouter",
    refusal=_refusal,
    stored_credential=_stored_key,
    env_variable="LLM_PROVIDER_OPENROUTER_API_KEY",
)


def link_refusal(integration: Integration) -> str | None:
    """Why ``integration`` may not feed the platform provider, or ``None``."""
    return feeds.link_refusal(FEED, integration)


async def adoptable_configs(db: AsyncSession) -> list[LLMProviderConfig]:
    """The OpenRouter configs an integration may adopt: not env-seeded, not fed by another."""
    return await feeds.adoptable_configs(db, FEED)


async def create_linked_provider(
    db: AsyncSession,
    *,
    integration: Integration,
    user: User,
    request: Request | None,
    display_name: str | None,
    available_models: list[str],
    default_model: str | None,
    context_windows: dict[str, int] | None,
) -> LLMProviderResponse:
    """Create the platform OpenRouter provider from the integration's API key. Commits."""
    return await feeds.create_linked_provider(
        db,
        FEED,
        integration=integration,
        user=user,
        request=request,
        display_name=display_name,
        available_models=available_models,
        default_model=default_model,
        context_windows=context_windows,
    )


async def adopt_provider(
    db: AsyncSession,
    *,
    integration: Integration,
    config_id: uuid.UUID,
    user: User,
    request: Request | None,
) -> LLMProviderConfig:
    """Link an existing OpenRouter config to the integration and give it its key."""
    return await feeds.adopt_provider(
        db, FEED, integration=integration, config_id=config_id, user=user, request=request
    )


def _holds_key(config: LLMProviderConfig, key: str) -> bool:
    """Whether ``config`` already stores ``key`` (both compared as stored, a reference as one).

    A stored key that no longer decrypts (a rotated encryption key, a corrupted
    row) holds nothing usable: it is replaced like any other key, so the edit
    that repairs the link is never refused because of it.
    """
    try:
        return decrypt_stored_value(config.api_key_enc) == key
    except Exception:
        logger.warning("LLM provider config %s: stored API key unreadable, replacing it", config.id)
        return False


async def _on_integration_update(
    db: AsyncSession, integration: Integration, update: IntegrationUpdate
) -> None:
    """Carry an edit of the integration over to the configs it feeds.

    A replaced API key is copied into each of them; an integration that is no
    longer company-managed stops feeding them (its key, and whoever may now
    edit it, are no longer the platform's to use).
    """
    configs = await linked_configs(db, integration.id)
    if not configs:
        return
    if not integration.is_company_managed:
        for config in configs:
            config.source_integration_id = None
        logger.warning(
            "OpenRouter integration %s is no longer company-managed: unlinked %d LLM "
            "provider config(s), which keep the API key they hold",
            integration.id,
            len(configs),
        )
        return
    if not integration.api_key_enc:
        logger.warning(
            "OpenRouter integration %s has no API key any more: the LLM provider keeps the key it holds",
            integration.id,
        )
        return
    # Stored texts: a vault reference is copied as a reference, never resolved into the config.
    key = decrypt_stored_value(integration.api_key_enc)
    replaced = [config for config in configs if not _holds_key(config, key)]
    for config in replaced:
        config.api_key_enc = encrypt_value(key)
        await log_audit(
            db,
            user=update.actor,
            action="update",
            entity_type="llm_provider",
            entity_id=config.id,
            entity_name=config.display_name or config.provider,
            summary=(
                f"Replaced the API key of LLM provider '{config.provider}' with the new key "
                f"of integration '{integration.name}'"
            ),
            details={"source_integration_id": str(integration.id)},
            request=update.request,
        )
    if replaced:
        update.after_commit.add(_drop_provider_cache)


_drop_provider_cache = feeds.drop_provider_cache


register_lifecycle(PROVIDER, on_update=_on_integration_update)
