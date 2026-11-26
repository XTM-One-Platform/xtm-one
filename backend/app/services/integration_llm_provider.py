# SPDX-License-Identifier: Apache-2.0

"""A platform LLM provider fed from an integration - the one linking mechanism.

Some integrations hold what a platform LLM provider runs on: an OpenRouter
account (``services/openrouter_llm_provider.py``), an LM Studio server
(``services/lmstudio_llm_provider.py``). Their console creates
the provider from the integration, or adopts one already configured, then picks
the models it offers. The config records the integration
(``LLMProviderConfig.source_integration_id``, at most one config per
integration, held by a partial unique index) and each feed keeps it in step
through its own lifecycle hook.

A feed (:class:`IntegrationFeed`) says which integration provider feeds which
LLM provider, why an integration may not feed it, and what credential the
config stores. Only a company-managed integration may feed the platform: the
provider is shared by every user, and a personal integration is its owner's.
Deleting the integration unlinks the config at the database level
(``ON DELETE SET NULL``). An env-seeded config is never adopted: the boot seed
re-applies its key from the environment.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import encrypt_value
from app.llm.model_limits import is_model_id, reported_context_window
from app.models.integration import Integration
from app.models.llm_config import LLMProviderConfig
from app.services.audit import log_audit
from app.services.llm_provider_deletion import lock_provider_configs

if TYPE_CHECKING:
    from fastapi import Request

    from app.api.v1.schemas.settings import LLMProviderResponse
    from app.models.user import User

logger = logging.getLogger(__name__)


class LinkRefusedError(Exception):
    """A link the integration or the config does not allow; ``status`` is the HTTP answer."""

    def __init__(self, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class IntegrationFeed:
    """One integration provider feeding one platform LLM provider."""

    #: ``Integration.provider`` of the integrations that feed.
    integration_provider: str
    #: ``LLMProviderConfig.provider`` of the configs they feed.
    llm_provider: str
    #: The name people read (``OpenRouter``, ``LM Studio``).
    label: str
    #: Why the integration may not feed the provider (besides being of another
    #: provider or personal), or ``None``.
    refusal: Callable[[Integration], str | None]
    #: The credential the config stores, as stored (a vault reference stays one);
    #: raises :class:`LinkRefusedError` when the integration has none usable.
    stored_credential: Callable[[Integration], str]
    #: The environment variable an env-seeded config of this provider reads,
    #: for the message refusing to adopt one.
    env_variable: str | None = None


def link_refusal(feed: IntegrationFeed, integration: Integration) -> str | None:
    """Why ``integration`` may not feed ``feed``'s platform provider, or ``None``."""
    if integration.provider != feed.integration_provider:
        return f"Only {feed.label} integrations can feed the {feed.label} LLM provider."
    if not integration.is_company_managed:
        return (
            "Make this integration company-managed first: the platform LLM provider is "
            "shared by every user, so it is only fed by a company-managed integration."
        )
    return feed.refusal(integration)


def _require_linkable(feed: IntegrationFeed, integration: Integration) -> None:
    refusal = link_refusal(feed, integration)
    if refusal is not None:
        raise LinkRefusedError(refusal)


async def linked_configs(db: AsyncSession, integration_id: uuid.UUID) -> list[LLMProviderConfig]:
    """The provider configs ``integration_id`` feeds, oldest first."""
    rows = await db.execute(
        select(LLMProviderConfig)
        .where(LLMProviderConfig.source_integration_id == integration_id)
        .order_by(LLMProviderConfig.created_at, LLMProviderConfig.id)
    )
    return list(rows.scalars().all())


async def adoptable_configs(db: AsyncSession, feed: IntegrationFeed) -> list[LLMProviderConfig]:
    """The configs of ``feed``'s provider an integration may adopt: not env-seeded, not fed by another."""
    rows = await db.execute(
        select(LLMProviderConfig)
        .where(
            LLMProviderConfig.provider == feed.llm_provider,
            LLMProviderConfig.source_integration_id.is_(None),
            LLMProviderConfig.is_from_env.is_(False),
        )
        .order_by(LLMProviderConfig.created_at, LLMProviderConfig.id)
    )
    return list(rows.scalars().all())


#: The partial unique index that makes an integration feed one config at most.
_ONE_CONFIG_INDEX = "uq_llm_provider_configs_source_integration_id"


def _already_feeds(feed: IntegrationFeed) -> str:
    return "This integration already feeds a platform LLM provider: pick its models instead."


def _is_second_link(exc: IntegrityError) -> bool:
    return _ONE_CONFIG_INDEX in str(exc.orig)


async def create_linked_provider(
    db: AsyncSession,
    feed: IntegrationFeed,
    *,
    integration: Integration,
    user: User,
    request: Request | None,
    display_name: str | None,
    available_models: list[str],
    default_model: str | None,
    context_windows: dict[str, int] | None,
) -> LLMProviderResponse:
    """Create ``feed``'s platform provider from the integration. Commits."""
    from app.api.v1.schemas.settings import LLMProviderRequest
    from app.api.v1.settings import create_provider_config

    _require_linkable(feed, integration)
    if await linked_configs(db, integration.id):
        raise LinkRefusedError(_already_feeds(feed))
    try:
        return await create_provider_config(
            db,
            body=LLMProviderRequest(
                provider=feed.llm_provider,
                api_key=feed.stored_credential(integration),
                display_name=display_name or integration.name,
                default_model=default_model,
                available_models=available_models,
                context_windows=context_windows,
            ),
            user=user,
            request=request,
            source_integration_id=integration.id,
        )
    except IntegrityError as exc:
        # A concurrent create won between the check above and the insert.
        await db.rollback()
        if _is_second_link(exc):
            raise LinkRefusedError(_already_feeds(feed)) from exc
        raise


async def adopt_provider(
    db: AsyncSession,
    feed: IntegrationFeed,
    *,
    integration: Integration,
    config_id: uuid.UUID,
    user: User,
    request: Request | None,
) -> LLMProviderConfig:
    """Link an existing config of ``feed``'s provider to the integration and give it its credential.

    Replaces the credential the platform calls the provider with, so the caller
    commits and drops the provider cache after it.
    """
    from app.services.llm_provider_management import ensure_llm_provider_management_allowed

    _require_linkable(feed, integration)
    ensure_llm_provider_management_allowed()
    # A replaced key is a provider edit: serialized with deletions like one.
    await lock_provider_configs(db)
    config = (
        await db.execute(select(LLMProviderConfig).where(LLMProviderConfig.id == config_id))
    ).scalar_one_or_none()
    if config is None:
        raise LinkRefusedError("Provider config not found", status=404)
    if config.provider != feed.llm_provider:
        raise LinkRefusedError(f"Only {feed.label} providers can be fed by this integration.")
    if config.is_from_env:
        source = f" ({feed.env_variable})" if feed.env_variable else ""
        raise LinkRefusedError(
            f"This provider's key comes from the environment{source} "
            "and is re-applied on every restart, so it cannot be fed by an integration."
        )
    if config.source_integration_id is not None and config.source_integration_id != integration.id:
        raise LinkRefusedError("This provider is already fed by another integration.")
    if any(other.id != config.id for other in await linked_configs(db, integration.id)):
        raise LinkRefusedError(
            "This integration already feeds a platform LLM provider: unlink it first."
        )
    config.source_integration_id = integration.id
    config.api_key_enc = encrypt_value(feed.stored_credential(integration))
    try:
        await db.flush()
    except IntegrityError as exc:
        # A concurrent create or adoption won after the check above.
        await db.rollback()
        if _is_second_link(exc):
            raise LinkRefusedError(_already_feeds(feed)) from exc
        raise
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="llm_provider",
        entity_id=config.id,
        entity_name=config.display_name or config.provider,
        summary=f"Linked LLM provider '{config.provider}' to integration '{integration.name}' and replaced its API key",
        details={"source_integration_id": str(integration.id)},
        request=request,
    )
    return config


async def unlink_provider(
    db: AsyncSession,
    *,
    integration: Integration,
    config_id: uuid.UUID,
    user: User,
    request: Request | None,
) -> LLMProviderConfig:
    """Stop feeding a config from the integration. The config keeps its key and models."""
    config = (
        await db.execute(
            select(LLMProviderConfig).where(
                LLMProviderConfig.id == config_id,
                LLMProviderConfig.source_integration_id == integration.id,
            )
        )
    ).scalar_one_or_none()
    if config is None:
        raise LinkRefusedError("Provider config not found", status=404)
    config.source_integration_id = None
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="llm_provider",
        entity_id=config.id,
        entity_name=config.display_name or config.provider,
        summary=f"Unlinked LLM provider '{config.provider}' from integration '{integration.name}'",
        details={"source_integration_id": str(integration.id)},
        request=request,
    )
    return config


def merged_context_windows(
    current: dict[str, Any] | None,
    previous_models: Iterable[str],
    selected_models: Iterable[str],
    catalog: dict[str, dict[str, Any]],
) -> dict[str, int] | None:
    """The config's context windows once the newly selected models are added.

    A model newly enabled with no window of its own gets the catalog's
    ``context_length``: the provider's flat default (1M tokens) overstates
    most models, and an overstated window makes compaction fire too late. A
    window already set - by an admin or an earlier save - is kept as it is.
    ``None`` when there is nothing to store, so the caller leaves the field out.
    """
    # Keys and values bounded like an admin's (``is_model_id``,
    # ``reported_context_window``), so the update built from them always passes
    # the request's own validation, a map stored before it included.
    windows: dict[str, int] = {
        model: tokens
        for model, value in (current or {}).items()
        if is_model_id(model) and (tokens := reported_context_window(value))
    }
    before = set(previous_models)
    added = False
    for model in selected_models:
        if model in before or model in windows or not is_model_id(model):
            continue
        length = reported_context_window((catalog.get(model) or {}).get("context_length"))
        if length:
            windows[model] = length
            added = True
    if not added:
        return None
    return windows


async def drop_provider_cache() -> None:
    """Drop the provider instances, after a commit that changed what they run on."""
    from app.llm.router import invalidate_provider_cache

    invalidate_provider_cache()
