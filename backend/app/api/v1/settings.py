# SPDX-License-Identifier: Apache-2.0

"""Settings endpoints – LLM provider configuration, OAuth defaults, user preferences."""

import asyncio
import base64
import ipaddress
import json
import logging
import re
import re as _branding_re
import socket
import uuid
from contextlib import suppress
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, cast
from urllib.parse import urlparse, urlsplit

import httpx
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from app.models.general_assistant_config import GeneralAssistantConfig
    from app.models.platform_settings import PlatformSettings
    from app.models.sso_config import SSOConfig
    from app.services.model_catalog_announcements import CatalogSnapshot

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import Text as SAText
from sqlalchemy import cast as sa_cast
from sqlalchemy import delete, desc, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth_sso import check_oidc_discovery, clear_oidc_discovery_cache
from app.api.deps import (
    AfterCommit,
    get_current_user,
    require_admin,
    require_agentic_ai,
    require_ai_enabled,
    run_after_commit,
)
from app.api.v1.helpers import (
    EntityListScope,
    get_user_group_ids,
    get_user_integration,
    list_scope_admin_bypass,
    parse_path_id,
    parse_uuid,
    user_visible_filter,
)
from app.api.v1.schemas.settings import (
    AvailableDecisionModelsResponse,
    AvailableModelItem,
    DecisionListing,
    DecisionListingStatus,
    EmbeddingModelTestRequest,
    EmbeddingModelTestResponse,
    GeneralAssistantConfigRequest,
    GeneralAssistantConfigResponse,
    LangfuseConfigRequest,
    LangfuseConfigResponse,
    LLMModelOverlap,
    LLMProviderDeletionImpactResponse,
    LLMProviderRequest,
    LLMProviderResponse,
    LLMProviderUpdateRequest,
    ModelCatalogChangeSummary,
    OAuthDefaultRequest,
    OAuthDefaultResponse,
    ProviderDeletionEmbeddingRoute,
    ProviderDeletionReferences,
    ProviderDeletionRoute,
    ProviderRouteTarget,
    QuotaConfig,
    QuotaPeriodUsage,
    QuotaUsageEntry,
    RefreshModelsRequest,
    ResolveModelsRequest,
    ResolveModelsResponse,
    ScanContextWindowsRequest,
    ScanContextWindowsResponse,
    ScannableTool,
    ScannableToolGroup,
    ScannableToolsResponse,
    ScannedContextWindowItem,
    SecurityOptionItem,
    SecurityOptionsResponse,
    SecurityOptionsUpdateRequest,
    SpeechVoiceItem,
    SpeechVoicesResponse,
    SSOConfigRequest,
    SSOConfigResponse,
    SSOOIDCTestRequest,
    SSOOIDCTestResponse,
    SupportCodeRedeemRequest,
    UserPreferencesRequest,
    UserQuotaInfoResponse,
    ValidateProviderRequest,
    ValidateProviderResponse,
    VideoModelStatus,
)
from app.api.v1.schemas.vault_references import attach_vault_failures, vault_status
from app.core.database import get_db
from app.core.email_domains import parse_allowed_domains
from app.core.security import decrypt_stored_value, decrypt_value, encrypt_value
from app.core.utils import format_exception_message, normalize_language, safe_uuid
from app.llm.custom_openai_provider import CustomOpenAIProvider
from app.llm.decisions import DECISION_LISTING_TIMEOUT_SECONDS
from app.llm.env_models import models_env_locked, models_env_var
from app.llm.gateway_provider import (
    FiligranGatewayProvider,
    parse_gateway_credential,
    redact_token,
)
from app.llm.identity import set_llm_caller
from app.llm.provider_access import (
    ProviderAccessError,
    apply_provider_access,
    canonical_ids,
    is_restricted,
    provider_routing,
)
from app.llm.router import (
    EMBEDDING_DEFAULTS,
    EMBEDDING_ONLY_PROVIDERS,
    IMAGE_DEFAULTS,
    is_chat_capable_model,
    platform_video_model_problem,
    provider_capabilities,
    provider_for_config,
    provider_instance,
)
from app.llm.router import PROVIDER_MAP as ROUTER_PROVIDER_MAP
from app.models.agent import Agent
from app.models.agent_memory_entry import AgentMemoryEntry
from app.models.custom_tool import CustomTool
from app.models.embedding import Embedding
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.models.llm_config import LLMProviderConfig
from app.models.llm_model_price import LLMModelPrice
from app.models.llm_trace import LLMTrace
from app.models.mcp_server import MCPServer
from app.models.oauth_default import OAuthDefault
from app.models.sso_config import SSOConfig
from app.models.user import User, is_platform_admin
from app.services import ai_state, general_assistant_skills
from app.services.audit import log_audit
from app.services.audit_retention import audit_log_retention_days
from app.services.branding import (
    BRANDING_IMAGE_MAX_BYTES,
    SAFE_BRANDING_IMAGE_MIMES,
    estimated_base64_decoded_size,
    parse_branding_data_url,
)
from app.services.builtin_tool_catalog import (
    build_builtin_tool_catalog,
    withhold_evaluation_tools,
    withhold_flow_builder_tools,
    withhold_health_tools,
)
from app.services.ctem_verticals import normalize_vertical
from app.services.handover_types import with_added_handover_targets
from app.services.kb_helpers import count_provider_failures, enqueue_reembedding, retry_provider_failures_now
from app.services.llm_provider_deletion import (
    DeletionImpact,
    ReplacementError,
    RouteImpact,
    RouteTarget,
    apply_deletion_impact,
    compute_deletion_impact,
    lock_provider_configs,
    model_reference_is_orphaned,
    orphaned_agent_clause,
    orphaned_user_clause,
    pair_is_orphaned,
    provider_reference_problem,
)
from app.services.llm_provider_management import ensure_llm_provider_management_allowed
from app.services.tool_approval_settings import (
    TOOL_APPROVALS_AUDIT_ENTITY,
    apply_tool_approval_settings,
    serialize_tool_approval_settings,
)
from app.services.tracing.price_loader import (
    LITELLM_MODEL_PRICES_PATH,
    MODEL_PRICES_PATH,
    parse_model_prices_payload,
    resolve_model_prices_source_path,
    sync_model_prices,
)
from app.services.vault import (
    ADMIN_PRINCIPAL,
    SecretUnavailableError,
    reference_problem,
    resolve_secret_text,
    reveal_field,
)
from app.services.wait_settings import (
    WaitSettingsLockedError,
    apply_wait_settings,
    serialize_wait_settings,
)

logger = logging.getLogger(__name__)

LANGFUSE_DEFAULT_HOST = "https://cloud.langfuse.com"

router = APIRouter()

#: The router's provider types; a copy, so a test can stub one here alone.
PROVIDER_MAP = dict(ROUTER_PROVIDER_MAP)


async def _list_chat_models(provider) -> tuple[list[str], dict[str, list[str]] | None]:
    """Fetch chat models, preferring the grouped listing when the provider
    exposes one.

    ``list_models()`` and ``list_models_grouped()`` are two independent calls
    into the provider, but on ``FiligranGatewayProvider`` both hit the same
    upstream ``/v1/models`` endpoint — calling both would double that
    round-trip for no new data. Deriving the flat list from the groups
    avoids the second call; providers with no grouping fall back to
    ``list_models()`` as before. Only the models that chat are kept: the
    others are picked in their capability's own section.
    """
    groups = await provider.list_models_grouped()
    if groups is not None:
        chat_groups = {name: [m for m in ids if is_chat_capable_model(m)] for name, ids in groups.items()}
        chat_groups = {name: ids for name, ids in chat_groups.items() if ids}
        return sorted({model_id for ids in chat_groups.values() for model_id in ids}), chat_groups
    return [m for m in await provider.list_models() if is_chat_capable_model(m)], None


def _ensure_new_provider_allowed(provider: str, *, from_integration: bool = False) -> None:
    """Block configuring a NEW provider that is not addable by hand.

    Only guards the "create" surfaces (add-provider, and the pre-save
    resolve/validate calls the add-provider dialog makes before saving).
    A provider fed only by an integration (``integration_fed_only``: LM
    Studio) is created from that integration's console (*from_integration*).
    """
    provider_cls = PROVIDER_MAP.get(provider)
    if getattr(provider_cls, "integration_fed_only", False) and not from_integration:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This provider runs on an integration: create it from the Models tab of a "
                "company-managed integration of its kind."
            ),
        )


def env_managed_default_fields() -> set[str]:
    """The Settings > AI and models > Models fields an environment variable pins (read-only).

    The public name of :func:`_env_managed_ga_fields` for the agent tools,
    which refuse the same writes the page shows read-only.
    """
    return _env_managed_ga_fields()


def _env_managed_ga_fields() -> set[str]:
    """Return the GeneralAssistant/PlatformSettings field names that are
    *unconditionally* re-seeded from environment variables on every startup
    and therefore read-only in the UI.

    Empty under ``BYOLLM_ACTIVATED=true``: there the ``DEFAULT_*`` values
    only furnish an install that has no LLM configuration of its own yet, so
    nothing is re-seeded over a stored value and nothing is read-only (see
    the early return).

    These mirror the ``DEFAULT_*`` overrides applied to ``PlatformSettings``
    in ``main._seed_llm_providers_from_env``.  A field qualifies as
    env-managed only when its boot seeding always wins (re-applied on every
    restart regardless of the current DB value) — otherwise an admin edit
    would silently revert on the next restart, exactly the inconsistency the
    env-seeded LLM *provider* lock already prevents.  The set is computed from
    live env settings (no DB flag needed) and surfaced via
    ``GeneralAssistantConfigResponse.env_managed_fields`` so the frontend can
    show the read-only "Env" badge, while ``update_general_assistant_config``
    rejects any attempt to mutate one of these fields.

    Provider-name and provider-id companions are locked together with their
    model field because the frontend always saves them as a group — with one
    deliberate exception (shared by the embedding and image groups): when the
    model env var is set **without** its provider companion
    (``DEFAULT_IMAGE_MODEL`` / ``DEFAULT_EMBEDDING_MODEL`` alone), the boot
    seeding re-applies only ``ps.image_model`` / ``ps.embedding_model`` (the
    ``elif`` branches in ``_seed_llm_providers_from_env``), so only the model
    field is locked. That still makes the whole section read-only in the UI
    (the frontend locks the section off the model flag alone); locking the
    provider companions there would be a phantom lock since env never re-applies
    them on restart.

    Embedding defaults (``DEFAULT_EMBEDDING_PROVIDER`` / ``_MODEL`` /
    ``_DIMENSIONS``) ARE env-managed, exactly like the model / routing /
    reasoning / image defaults: their boot seeding is unconditional (env always
    wins on every restart — see ``_seed_llm_providers_from_env``), so the
    Embedding section must be read-only too, otherwise an admin edit would
    silently revert on the next restart. Changing the embedding model/dimensions
    to a different vector space after documents are ingested requires
    re-embedding; that is an operator responsibility handled out of band (the
    env var, like every other ``DEFAULT_*``, is the single source of truth).
    """
    from app.core.config import settings as app_settings

    locked: set[str] = set()
    # ``BYOLLM_ACTIVATED=true`` makes every ``DEFAULT_*`` furnish an install
    # that has no LLM configuration yet and nothing after that
    # (``main._env_may_seed``), so an admin edit no longer reverts on
    # restart — the only reason these fields were locked.
    # Nothing is env-managed then, and every Settings → AI and models → Models control
    # (including the embedding / image groups) stays editable.
    if app_settings.byollm_activated:
        return locked
    # ``DEFAULT_GENERAL_ASSISTANT_MODEL`` pins the General Assistant's own chat
    # model and is re-seeded on every boot (env always wins — see
    # ``main._seed_llm_providers_from_env``), so lock the GA model fields. The
    # frontend keys the GA "Chat Model" control read-only off ``llm_model``;
    # ``llm_provider`` is locked alongside it because the GA save always sends
    # them as a pair.
    if app_settings.default_general_assistant_model.strip():
        locked.update({"llm_provider", "llm_provider_id", "llm_model"})
    if app_settings.default_model.strip():
        locked.update(
            {
                "agent_default_provider",
                "agent_default_provider_id",
                "agent_default_model",
            }
        )
    if app_settings.default_reasoning_effort.strip():
        locked.add("default_reasoning_effort")
    if app_settings.default_routing_model.strip():
        locked.update({"routing_model", "routing_provider_id"})
    if app_settings.default_fallback_model.strip():
        locked.update({"fallback_model", "fallback_provider_id"})
    # Embedding defaults ARE seeded unconditionally outside BYOLLM (no
    # ``not ps.embedding_*`` guard in ``_seed_llm_providers_from_env``), so
    # env always wins → lock them, same provider-companion rule as the image
    # group below.
    if (
        app_settings.default_embedding_provider.strip()
        and app_settings.default_embedding_model.strip()
    ):
        locked.update(
            {"embedding_provider", "embedding_provider_id", "embedding_model"}
        )
    elif app_settings.default_embedding_model.strip():
        locked.add("embedding_model")
    if app_settings.default_embedding_dimensions is not None:
        locked.add("embedding_dimensions")
    # Image defaults ARE seeded unconditionally outside BYOLLM (no
    # ``not ps.image_*`` guard in ``_seed_llm_providers_from_env``), so env
    # always wins → lock them.
    if (
        app_settings.default_image_provider.strip()
        and app_settings.default_image_model.strip()
    ):
        locked.update({"image_provider", "image_provider_id", "image_model"})
    elif app_settings.default_image_model.strip():
        locked.add("image_model")
    # The video, speech, music and decision groups, by the seed's own rule.
    from app.llm.env_capability_defaults import env_locked_fields

    locked |= env_locked_fields()
    return locked


# ---------------------------------------------------------------------------
# Response builders (DRY helpers to avoid duplicated field mapping)
# ---------------------------------------------------------------------------


def _build_ga_response(
    config: "GeneralAssistantConfig", ps: "PlatformSettings | None" = None
) -> GeneralAssistantConfigResponse:
    """Build a GeneralAssistantConfigResponse from GA config + PlatformSettings.

    Fields that have been moved to PlatformSettings are read from *ps* when
    provided.  GA-only fields (llm_provider/model, system_prompt, bindings,
    etc.) are always read from *config*.
    """
    return GeneralAssistantConfigResponse(
        linked_agent_id=str(config.linked_agent_id) if config.linked_agent_id else None,
        llm_provider=config.llm_provider,
        llm_provider_id=(
            str(config.llm_provider_id)
            if getattr(config, "llm_provider_id", None)
            else None
        ),
        llm_model=config.llm_model,
        agent_default_provider=(
            getattr(ps, "agent_default_provider", None) if ps else None
        ),
        agent_default_provider_id=(
            str(getattr(ps, "agent_default_provider_id", None))
            if ps and getattr(ps, "agent_default_provider_id", None)
            else None
        ),
        agent_default_model=getattr(ps, "agent_default_model", None) if ps else None,
        default_reasoning_effort=(
            getattr(ps, "default_reasoning_effort", None) if ps else None
        ),
        embedding_provider=getattr(ps, "embedding_provider", None) if ps else None,
        embedding_provider_id=(
            str(getattr(ps, "embedding_provider_id", None))
            if ps and getattr(ps, "embedding_provider_id", None)
            else None
        ),
        embedding_model=getattr(ps, "embedding_model", None) if ps else None,
        embedding_dimensions=getattr(ps, "embedding_dimensions", None) if ps else None,
        image_provider=getattr(ps, "image_provider", None) if ps else None,
        image_provider_id=(
            str(getattr(ps, "image_provider_id", None))
            if ps and getattr(ps, "image_provider_id", None)
            else None
        ),
        image_model=getattr(ps, "image_model", None) if ps else None,
        video_provider=getattr(ps, "video_provider", None) if ps else None,
        video_provider_id=(
            str(getattr(ps, "video_provider_id", None))
            if ps and getattr(ps, "video_provider_id", None)
            else None
        ),
        video_model=getattr(ps, "video_model", None) if ps else None,
        video_fallback_models=list(getattr(ps, "video_fallback_models", None) or []) if ps else [],
        speech_provider=getattr(ps, "speech_provider", None) if ps else None,
        speech_provider_id=(
            str(getattr(ps, "speech_provider_id", None))
            if ps and getattr(ps, "speech_provider_id", None)
            else None
        ),
        speech_model=getattr(ps, "speech_model", None) if ps else None,
        speech_voice=getattr(ps, "speech_voice", None) if ps else None,
        music_provider=getattr(ps, "music_provider", None) if ps else None,
        music_provider_id=(
            str(getattr(ps, "music_provider_id", None))
            if ps and getattr(ps, "music_provider_id", None)
            else None
        ),
        music_model=getattr(ps, "music_model", None) if ps else None,
        routing_model=getattr(ps, "routing_model", None) if ps else None,
        routing_provider_id=(
            str(getattr(ps, "routing_provider_id", None))
            if ps and getattr(ps, "routing_provider_id", None)
            else None
        ),
        decision_model=getattr(ps, "decision_model", None) if ps else None,
        decision_provider_id=(
            str(getattr(ps, "decision_provider_id", None))
            if ps and getattr(ps, "decision_provider_id", None)
            else None
        ),
        fallback_model=getattr(ps, "fallback_model", None) if ps else None,
        fallback_provider_id=(
            str(getattr(ps, "fallback_provider_id", None))
            if ps and getattr(ps, "fallback_provider_id", None)
            else None
        ),
        max_iterations=config.max_iterations,
        system_prompt=config.system_prompt,
        knowledge_base_ids=config.knowledge_base_ids or [],
        mcp_server_ids=config.mcp_server_ids or [],
        custom_tool_ids=config.custom_tool_ids or [],
        builtin_tool_ids=config.builtin_tool_ids or [],
        skill_ids=(
            [str(sid) for sid in config.skill_ids] if getattr(config, "skill_ids", None) is not None else None
        ),
        default_skill_slugs=list(general_assistant_skills.GENERAL_ASSISTANT_SKILL_SLUGS),
        integration_bindings=config.integration_bindings or {},
        handover_agent_ids=config.handover_agent_ids or [],
        handover_types=config.handover_types or {},
        available_channel_types=config.available_channel_types,
        channel_config=config.channel_config,
        mcp_exposed_agent_ids=(ps.mcp_exposed_agent_ids or []) if ps else [],
        mcp_global_api_key_prefix=(
            getattr(ps, "mcp_global_api_key_prefix", None) if ps else None
        ),
        platform_mcp_enabled=(
            bool(getattr(ps, "platform_mcp_enabled", True)) if ps else True
        ),
        xtm_mcp_enabled=(
            bool(getattr(ps, "xtm_mcp_enabled", False)) if ps else False
        ),
        reply_suggestions_enabled=(
            getattr(ps, "reply_suggestions_enabled", True) is not False if ps else True
        ),
        env_managed_fields=sorted(_env_managed_ga_fields()),
    )


def _build_sso_response(row: "SSOConfig", base_url: str) -> SSOConfigResponse:
    """Build a SSOConfigResponse from a SSOConfig row."""
    return SSOConfigResponse(
        # An administrator's page: the reference's readable location is theirs to see.
        vault_secrets=vault_status(row, ADMIN_PRINCIPAL),
        oidc_enabled=row.oidc_enabled,
        oidc_display_name=row.oidc_display_name or "Microsoft",
        oidc_has_client_id=bool(row.oidc_client_id_enc),
        oidc_has_client_secret=bool(row.oidc_client_secret_enc),
        oidc_discovery_url=row.oidc_discovery_url or "",
        oidc_authorization_url=row.oidc_authorization_url or "",
        oidc_token_url=row.oidc_token_url or "",
        oidc_userinfo_url=row.oidc_userinfo_url or "",
        oidc_scopes=row.oidc_scopes or "openid email profile",
        saml_enabled=row.saml_enabled,
        saml_display_name=row.saml_display_name or "Corporate SSO",
        saml_idp_metadata_url=row.saml_idp_metadata_url or "",
        saml_sp_entity_id=row.saml_sp_entity_id or f"{base_url}/api/auth/saml/metadata",
        saml_requested_authn_context=row.saml_requested_authn_context,
        saml_groups_mapping_enabled=row.saml_groups_mapping_enabled,
        saml_groups_auto_create=row.saml_groups_auto_create,
        oidc_auto_admin_new_users=row.oidc_auto_admin_new_users,
        saml_auto_admin_new_users=row.saml_auto_admin_new_users,
        oidc_auto_create_users=row.oidc_auto_create_users,
        saml_auto_create_users=row.saml_auto_create_users,
        saml_entity_id=row.saml_sp_entity_id or f"{base_url}/api/auth/saml/metadata",
        saml_acs_url=f"{base_url}/api/auth/saml/acs",
        saml_metadata_url=f"{base_url}/api/auth/saml/metadata",
        local_auth_enabled=row.local_auth_enabled,
        registration_enabled=row.registration_enabled,
        allowed_domains=row.allowed_domains or "",
        allowed_domain_list=parse_allowed_domains(row.allowed_domains),
    )


async def _models_for_capability(
    db: AsyncSession,
    capability: str,
    user: User,
    *,
    usable_only: bool = False,
    listings: dict[str, DecisionListingStatus] | None = None,
) -> list[AvailableModelItem]:
    """Answer the capability-model reads without reaching a provider when AI is off.

    FR-007 is about the outbound request, not the read, and the distinction
    matters here in both directions. Gating the *routes* was wrong twice over:
    ``Settings.tsx`` calls these hooks on every tab and the global
    ``throwOnError`` sends the 403 to the page ErrorBoundary, so the whole of
    ``/settings`` vanished — **including the Parameters tab that turns AI back
    on**, which made the mode unexitable from the interface. And with no provider
    configured, ``_list_models_by_capability`` makes no call at all, so the
    refusal denied a read FR-008 requires to answer 200 (/code-review,
    2026-08-28).

    So the read answers 200 — with an **empty** list. The first attempt returned
    each provider row's ``available_models``, which was worse than refusing:
    that column is one flat admin-curated *chat* catalogue with no capability
    information, so the embedding and image endpoints both offered it and a
    model picker outside ``AiReadOnly`` — ``NovaRulesTab`` — invited an admin to
    choose ``gpt-4o`` as an embedding model (/code-review, 2026-08-28). These two
    endpoints exist to *discover* what a provider can do, and discovery needs the
    provider. Empty is the truthful answer while AI is off.

    Here rather than inside ``_list_models_by_capability`` because that helper is
    unit-tested with a scripted fake session, and an extra read there broke it —
    a fair complaint from the test: a "list from the providers" helper should not
    also decide policy.
    """
    from app.services.ai_state import is_ai_enabled

    if await is_ai_enabled(db):
        return await _list_models_by_capability(
            db, capability, user, usable_only=usable_only, listings=listings
        )
    return []


async def _list_models_by_capability(
    db: AsyncSession,
    capability: str,
    user: User,
    *,
    usable_only: bool = False,
    listings: dict[str, DecisionListingStatus] | None = None,
) -> list[AvailableModelItem]:
    """List available models for a given capability (embedding, image, video, speech, music, decision).

    Asks every configured LLM provider, concurrently, for its
    ``list_*_models()`` - with *usable_only*, only those the caller may use,
    which are never even asked otherwise. Video models are the
    Video Studio's own listing (``router.list_video_models_for``): a model its
    provider no longer serves is never offered, and a provider that
    excludes the caller offers none.

    *listings*, when given, receives each decision-listing provider's
    :data:`DecisionListingStatus` by config id; an unusable one only
    for an admin, who is shown every provider anyway.
    """
    # Bind the requesting user so FiligranGatewayProvider signs its outbound
    # JWT as them, not the service identity — otherwise gateway-side
    # attribution/quotas would be charged to the platform admin account.
    set_llm_caller(user.id, user.email)
    if capability == "video":
        from app.llm.router import list_video_models_for

        return [
            AvailableModelItem(model=m["model"], provider=m["provider"], provider_id=m["provider_id"])
            for m in await list_video_models_for(db, user_id=user.id)
        ]
    method_name = f"list_{capability}_models"
    result = await db.execute(select(LLMProviderConfig))
    configs = result.scalars().all()
    if usable_only:
        from app.llm.provider_access import provider_routing

        usable = (await provider_routing(db, str(user.id), configs)).usable
        if listings is not None and user.is_admin:
            usable_ids = {cfg.id for cfg in usable}
            for cfg in configs:
                if cfg.id not in usable_ids and _lists_decision_models(cfg):
                    listings[str(cfg.id)] = "unavailable"
        configs = usable

    # Run concurrently, so these read nothing from ``db``: one slow provider
    # must not hold the others' answers.
    async def _one(cfg: LLMProviderConfig) -> list[AvailableModelItem]:
        provider_cls = PROVIDER_MAP.get(cfg.provider)
        if not provider_cls:
            return []
        reports = listings is not None and _lists_decision_models(cfg)
        try:
            if capability == "decision":
                # On the config's own instance, which may remember what it
                # learnt (OpenAI's decision models), and within a deadline: the
                # route is open to every user and a provider may not answer.
                provider = await provider_for_config(cfg)
                async with asyncio.timeout(DECISION_LISTING_TIMEOUT_SECONDS):
                    provider_models, whole = await provider.decision_listing()
                if reports:
                    listings[str(cfg.id)] = (
                        "listed" if provider_models else "empty" if whole else "incomplete"
                    )
            else:
                api_key = await reveal_field(cfg, "api_key_enc") or ""
                provider = provider_instance(provider_cls, api_key, cfg.id)
                provider_models = await getattr(provider, method_name)()
            items = [
                AvailableModelItem(model=m, provider=cfg.provider, provider_id=str(cfg.id))
                for m in provider_models
            ]

            # Some embedding providers (for example TEI /embed endpoints) do not
            # expose model discovery APIs. Keep them selectable in settings by
            # surfacing a synthetic model option.
            if (
                capability == "embedding"
                and not provider_models
                and cfg.provider == "tei"
            ):
                fallback_model = (
                    cfg.default_model
                    or getattr(provider_cls, "default_model", None)
                    or "tei-embedding"
                )
                items.append(
                    AvailableModelItem(
                        model=fallback_model,
                        provider=cfg.provider,
                        provider_id=str(cfg.id),
                    )
                )
            return items
        except Exception as e:
            logger.warning(
                "Failed to list %s models for %s (%s): %s",
                capability,
                cfg.provider,
                cfg.id,
                format_exception_message(e),
            )
            if reports:
                listings[str(cfg.id)] = "failed"
            return []

    listed = await asyncio.gather(*(_one(cfg) for cfg in configs))
    return [item for items in listed for item in items]


def _lists_decision_models(cfg: LLMProviderConfig) -> bool:
    return bool(getattr(PROVIDER_MAP.get(cfg.provider), "lists_decision_models", False))


# ---------------------------------------------------------------------------
# LLM providers CRUD
# ---------------------------------------------------------------------------


def _first_chat_model(model: str | None, available_models: list[str] | None) -> str | None:
    """The first chat-capable model of a catalog, or of ``[model]`` without one."""
    return next(
        (m for m in (available_models or [model]) if m and is_chat_capable_model(m)),
        None,
    )


def platform_filled_models(config: LLMProviderConfig) -> dict[str, str | None]:
    """The model the platform writes into each empty default from *config*.

    Keyed by ``PlatformSettings`` field: what ``_ensure_platform_default_model``
    fills an empty agent, embedding and image default with, and, for an
    embedding-only provider, the model the XTM One embedding seed
    (``main._seed_xtm_one_embedding_server_from_env``) fills the embedding
    default with. The BYOLLM boot seed reads it to tell a value the platform
    wrote from one an administrator chose.
    """
    embedding = EMBEDDING_DEFAULTS.get(config.provider)
    if embedding is None and config.provider in EMBEDDING_ONLY_PROVIDERS:
        embedding = config.default_model
    return {
        "agent_default_model": _first_chat_model(
            config.default_model, config.available_models
        ),
        "embedding_model": embedding,
        "image_model": IMAGE_DEFAULTS.get(config.provider),
    }


async def _ensure_platform_default_model(
    db: AsyncSession,
    provider: str,
    model: str,
    provider_id: str | None = None,
    available_models: list[str] | None = None,
    *,
    fill_empty_defaults: bool = True,
    replaces: str | None = None,
) -> None:
    """Set platform default models if none are configured yet.

    GA-only fields (llm_provider/model) are written to
    ``GeneralAssistantConfig``.  Platform-wide defaults
    (agent_default_*, embedding_*, image_*) are written to
    ``PlatformSettings``.

    ``model`` is the provider's first/primary model. ``available_models``, when
    supplied, is the provider's full catalog and is scanned (in order) for the
    first chat-capable model so the agent default never depends on list
    ordering — see the comment below.

    ``fill_empty_defaults=False`` leaves the platform defaults alone, empty
    ones included: the boot seed passes it under ``BYOLLM_ACTIVATED`` on an
    install an administrator has configured, where an empty default is their
    choice.

    ``replaces``: a saved agent default of this very config equal to it counts
    as unset (the gateway's ``default`` alias once it marks a chat model, #1421).
    """
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )
    from app.models.general_assistant_config import (
        DEFAULT_GA_BUILTIN_TOOL_IDS,
        GeneralAssistantConfig,
        default_system_prompt,
    )

    # Only seed the platform agent *chat* default from a chat-capable model.
    # ``model`` is the provider's first model, which on an image/embedding-only
    # provider (e.g. an OpenAI row added solely for the Image Generation /
    # Embeddings sections, with ``available_models == ["gpt-image-1"]``) is a
    # non-chat model. Seeding it as ``agent_default_model`` is part of the root
    # cause of the "my default model is an image model" failure.
    #
    # Pick the first CHAT-CAPABLE model from the provider's catalog so the agent
    # default does not depend on the ordering of ``available_models``: a provider
    # that lists a non-chat model first but a chat model later (e.g.
    # ``["gpt-image-1", "gpt-4o"]``) still seeds the chat model as the agent
    # default. Falls back to ``model`` when no catalog is supplied. The embedding
    # / image auto-seeding below is independent of ``model`` (keyed on the
    # provider) and still runs so those sections light up as before.
    chat_default = _first_chat_model(model, available_models)

    result = await db.execute(select(GeneralAssistantConfig).limit(1))
    ga = result.scalar_one_or_none()
    if not ga:
        # The General Assistant defaults to "Agent default" (llm_model = NULL):
        # at runtime it follows ``PlatformSettings.agent_default_model`` unless
        # an admin picks a specific GA model in Settings, or
        # ``DEFAULT_GENERAL_ASSISTANT_MODEL`` pins one at boot (see
        # ``main._seed_llm_providers_from_env``). We deliberately do NOT pin the
        # GA to the first provider model here — that historic behaviour is what
        # seeded ``gpt-image-1`` as the GA chat model on image-only OpenAI
        # installs. Leaving it NULL keeps the GA tracking the agent
        # default the admin actually configured.
        ga = GeneralAssistantConfig(
            llm_provider=None,
            llm_model=None,
            system_prompt=default_system_prompt(),
            available_channel_types=["web"],
            # ``generate_file`` is part of the platform's core agentic
            # toolkit (just like ``generate_image``) and is wired on by
            # default so a brand-new Copilot deployment can ship STIX
            # bundles, CSVs, JSON reports, and other large outputs as
            # downloadable attachments out of the box; the presentation
            # tools ship the same way. Admins can remove any of
            # them from ``builtin_tool_ids`` via Settings → AI and models → General Assistant.
            builtin_tool_ids=list(DEFAULT_GA_BUILTIN_TOOL_IDS),
        )
        db.add(ga)

    if not fill_empty_defaults:
        await db.flush()
        return

    ps = await _load_ps(db)

    replaced = (
        replaces is not None
        and ps.agent_default_model == replaces
        and ps.agent_default_provider == provider
        and str(ps.agent_default_provider_id or provider_id) == str(provider_id)
    )
    if (not ps.agent_default_model or replaced) and chat_default:
        ps.agent_default_provider = provider
        ps.agent_default_provider_id = uuid.UUID(provider_id) if provider_id else None
        ps.agent_default_model = chat_default

    if not ps.embedding_model and provider in EMBEDDING_DEFAULTS:
        ps.embedding_provider = provider
        ps.embedding_provider_id = uuid.UUID(provider_id) if provider_id else None
        ps.embedding_model = EMBEDDING_DEFAULTS[provider]
    elif not ps.embedding_model and provider == FiligranGatewayProvider.provider_name and provider_id:
        # Its catalog differs per deployment: the gateway's flagged model, where it re-embeds nothing.
        from app.llm.gateway_embedding_default import fill_from_gateway

        if config := await db.get(LLMProviderConfig, uuid.UUID(provider_id)):
            await fill_from_gateway(db, ps, [config])

    if not ps.image_model and provider in IMAGE_DEFAULTS:
        ps.image_provider = provider
        ps.image_provider_id = uuid.UUID(provider_id) if provider_id else None
        ps.image_model = IMAGE_DEFAULTS[provider]

    # No video, speech or music model is filled in: a provider retires
    # its built-in video model (OpenAI's Sora), and whether one is still served
    # is known only by asking the provider, which a boot does not. Left empty
    # ("Auto"), the generation takes the first provider whose built-in model is
    # live (``router.get_video_provider``); ``DEFAULT_VIDEO_*`` pins one.

    await db.flush()


async def _cleanup_orphaned_model_references(
    db: AsyncSession,
    provider_name: str,
    removed_models: set[str],
    *,
    provider_deleted: bool = False,
    provider_id: uuid.UUID | None = None,
) -> dict[str, int]:
    """Null out agent / user / GA config references to models that are no longer available.

    Called when a provider is deleted (``provider_deleted=True``) or its
    ``available_models`` list shrinks.  This prevents agents from silently
    pointing at a ghost model and failing at runtime.

    Which choices are orphaned is decided by the predicates in
    ``services/llm_provider_deletion.py``, which the deletion preview reads
    too, so the confirmation never names a different set than this clears.

    Returns counts of cleaned entities for audit logging.
    """
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )
    from app.models.general_assistant_config import GeneralAssistantConfig

    counts: dict[str, int] = {"agents": 0, "users": 0, "ga_fields": 0}

    change = {
        "removed_models": removed_models,
        "provider_deleted": provider_deleted,
        "changed_provider_id": provider_id,
    }
    agent_clause = orphaned_agent_clause(provider_name=provider_name, **change)
    if agent_clause is None:
        return counts
    user_clause = orphaned_user_clause(provider_name=provider_name, **change)

    # --- Agents: reset to platform default ---
    agents_result = await db.execute(select(Agent).where(agent_clause))
    for agent in agents_result.scalars().all():
        agent.llm_provider = None
        agent.llm_provider_id = None
        agent.llm_model = None
        counts["agents"] += 1

    # --- Users: reset preferred model ---
    users_result = await db.execute(select(User).where(user_clause))
    for user in users_result.scalars().all():
        user.preferred_llm_provider = None
        user.preferred_llm_provider_id = None
        user.preferred_llm_model = None
        counts["users"] += 1

    # --- GeneralAssistantConfig: reset GA-only model pair (llm_*) ---
    ga_result = await db.execute(select(GeneralAssistantConfig).limit(1))
    ga = ga_result.scalar_one_or_none()
    if ga and pair_is_orphaned(
        ga.llm_provider,
        getattr(ga, "llm_provider_id", None),
        ga.llm_model,
        provider_name=provider_name,
        **change,
    ):
        ga.llm_provider = None
        ga.llm_provider_id = None
        ga.llm_model = None
        counts["ga_fields"] += 1

    # --- PlatformSettings: reset moved model pairs ---
    ps = await _load_ps(db)
    ps_pairs = [
        ("agent_default_provider", "agent_default_provider_id", "agent_default_model"),
        ("embedding_provider", "embedding_provider_id", "embedding_model"),
        ("image_provider", "image_provider_id", "image_model"),
        ("video_provider", "video_provider_id", "video_model"),
        ("speech_provider", "speech_provider_id", "speech_model"),
        ("music_provider", "music_provider_id", "music_model"),
    ]
    for prov_attr, prov_id_attr, model_attr in ps_pairs:
        if pair_is_orphaned(
            getattr(ps, prov_attr, None),
            getattr(ps, prov_id_attr, None),
            getattr(ps, model_attr, None),
            provider_name=provider_name,
            **change,
        ):
            setattr(ps, prov_attr, None)
            setattr(ps, prov_id_attr, None)
            setattr(ps, model_attr, None)
            if model_attr == "speech_model":
                # A voice belongs to its provider's library.
                ps.speech_voice = None
            counts["ga_fields"] += 1

    # --- PlatformSettings.routing_model / fallback_model ---
    # On a provider delete ``removed_models`` is that provider's full
    # ``available_models`` set; on a shrink it is ``old_models - new_models`` —
    # either way a stale model lands there and must be NULLed so the routing
    # evaluator and the failover chain fall back to the platform default. Kept
    # in lockstep with the one-time ``m1g8r9a0t1e2`` backfill migration.
    # Each gained a provider-config reference companion, which must be
    # cleared with it: a surviving reference outranks the model owner, so a wiped
    # model choice would otherwise keep routing to the deleted config's
    # replacement instead of falling back.
    for model_attr, ref_attr in (
        ("routing_model", "routing_provider_id"),
        ("fallback_model", "fallback_provider_id"),
        ("decision_model", "decision_provider_id"),
    ):
        if model_reference_is_orphaned(
            getattr(ps, model_attr, None), getattr(ps, ref_attr, None), **change
        ):
            setattr(ps, model_attr, None)
            setattr(ps, ref_attr, None)
            counts["ga_fields"] += 1

    if any(counts.values()):
        await db.flush()
        logger.info(
            "Cleaned orphaned model references for provider '%s': "
            "%d agent(s), %d user(s), %d config field(s)",
            provider_name,
            counts["agents"],
            counts["users"],
            counts["ga_fields"],
        )

    return counts


async def _reset_kb_embeddings_for_new_model(
    db: AsyncSession,
    *,
    target_embedding_model: str | None,
) -> tuple[int, list[str]]:
    """Purge KB embeddings, sync KB model metadata, and re-queue docs.

    The embedding purge is a single bulk ``DELETE FROM embeddings`` that can
    span the entire KB corpus. Under the global 30 s ``statement_timeout``
    (core/database.py) it gets cancelled on large installs
    (``QueryCanceledError``), which previously bubbled up as a 500 on the
    AI Models save. We raise the per-transaction timeout for this operation
    only — the same approach the KB sync scheduler already uses for embedding
    deletes — so the purge can complete. ``set_config(..., is_local=True)`` is
    transaction-scoped and auto-resets on COMMIT/ROLLBACK, so the pooled
    connection is never left with a relaxed cap.
    """
    from app.core.config import settings as app_settings

    timeout_secs = int(
        getattr(app_settings, "embedding_reset_statement_timeout_secs", 600) or 0
    )
    # ``0`` disables the per-statement cap entirely (Postgres treats
    # ``statement_timeout = 0`` as "no timeout"); otherwise clamp to the
    # signed-int millisecond range Postgres accepts.
    timeout_ms = (
        0 if timeout_secs <= 0 else min(timeout_secs * 1000, 2_147_483_647)
    )
    try:
        conn = await db.connection()
        await conn.execute(
            text("SELECT set_config('statement_timeout', :timeout_ms, true)"),
            {"timeout_ms": str(timeout_ms)},
        )
    except Exception:
        # Best-effort: if the backend can't raise the cap we still attempt
        # the purge (it may succeed on small corpora within the default
        # window). Never let timeout bookkeeping block the config save.
        logger.warning(
            "Could not raise statement_timeout for KB embedding reset; "
            "proceeding with the default cap.",
            exc_info=True,
        )

    doc_rows = await db.execute(select(KnowledgeDocument.id))
    doc_ids = [str(r[0]) for r in doc_rows.all()]

    deleted_result = await db.execute(
        delete(Embedding).where(
            Embedding.knowledge_base_id.is_not(None),
            Embedding.source_type == "document",
        )
    )
    deleted_chunks = int(getattr(deleted_result, "rowcount", 0) or 0)

    if doc_ids:
        await db.execute(
            update(KnowledgeDocument).values(
                status="pending",
                error_message=None,
                chunk_count=0,
            )
        )
        kb_updates: dict[str, object] = {"chunk_count": 0}
        if target_embedding_model:
            kb_updates["embedding_model"] = target_embedding_model
        await db.execute(update(KnowledgeBase).values(**kb_updates))

    return deleted_chunks, doc_ids


async def _run_embedding_reset_background(target_embedding_model: str | None) -> None:
    """Purge KB embeddings and re-queue documents off the request path.

    The bulk ``DELETE FROM embeddings`` and per-document re-queue can span the
    entire KB corpus. On large installs the DELETE over a pgvector/HNSW index
    runs for minutes, so doing it inline made the AI Models "Save" appear to
    hang forever (it blocked the response even once the 30s
    ``statement_timeout`` no longer cancelled it). We run it in a
    background task with its own session so the Save returns immediately.

    The autonomous recovery sweeps (enabled by default, run every scheduler
    tick) are the safety net if this task is interrupted (process restart) or
    fails, and which one applies depends on where it stops:

    * If the reset commits but the re-queue is lost (process restart between the
      commit and ``enqueue_reembedding``, or a swallowed enqueue error), the docs
      are left ``status='pending'`` with no embeddings and
      ``recover_stuck_documents`` re-enqueues them.
    * If the task fails before the reset commits, the transaction rolls back and
      the docs stay ``ready`` with their now-stale embeddings, so
      ``recover_stale_documents`` re-embeds them on a later tick.
    """
    from app.core.database import async_session

    try:
        async with async_session() as bg_db:
            deleted_chunks, requeue_doc_ids = (
                await _reset_kb_embeddings_for_new_model(
                    bg_db,
                    target_embedding_model=target_embedding_model,
                )
            )
            await bg_db.commit()
        if requeue_doc_ids:
            await enqueue_reembedding(requeue_doc_ids)
        logger.info(
            "Embedding reset complete: purged %d chunk(s), re-queued %d "
            "document(s) for the new embedding target (model=%s)",
            deleted_chunks,
            len(requeue_doc_ids),
            target_embedding_model,
        )
    except Exception:
        # Never let the background reset crash silently without a breadcrumb;
        # the autonomous document-recovery sweeps recover the affected docs
        # (recover_stuck_documents for pending docs left by a lost re-queue,
        # recover_stale_documents for ready docs if the reset rolled back).
        logger.error(
            "Background KB embedding reset failed; the autonomous "
            "document-recovery sweeps will recover the affected documents.",
            exc_info=True,
        )


async def _legacy_embedding_config_id(db: AsyncSession, provider: str | None) -> uuid.UUID | None:
    """The config an embedding provider stored without an id runs on: the newest
    of its type, as ``get_embedding_provider`` picks it."""
    from app.llm.router import newest_config, select_provider_configs

    same_type = select_provider_configs().where(LLMProviderConfig.provider == provider)
    config = newest_config((await db.execute(same_type)).scalars().all())
    return config.id if config else None


def _build_probe_candidates(
    *,
    max_probe: int,
    min_probe: int = 256,
    floor_probe: int = 64,
) -> tuple[int, ...]:
    """Build descending probe candidates from high to low without fixed presets."""
    upper = max(max_probe, floor_probe)
    lower = max(min_probe, floor_probe)
    candidates: list[int] = []
    current = upper
    while current >= lower:
        candidates.append(current)
        if current == lower:
            break
        next_value = current // 2
        if next_value < lower:
            candidates.append(lower)
            break
        current = next_value
    return tuple(dict.fromkeys(candidates))


def _suggest_smaller_dimensions(
    detected_dimensions: int,
    *,
    min_suggestion: int = 256,
) -> list[int]:
    """Return coherent smaller dimensions for speed/quality tradeoffs.

    Strategy:
    - Keep the detected dimension as the first option.
    - Then offer descending powers of 2 below it, down to min_suggestion.
    """
    if detected_dimensions <= 0:
        return []

    suggestions: list[int] = [detected_dimensions]
    current = 1
    while current * 2 <= detected_dimensions:
        current *= 2
    while current >= min_suggestion:
        if current < detected_dimensions:
            suggestions.append(current)
        current //= 2
    return suggestions


def build_model_owner_index(
    configs: "list[LLMProviderConfig]",
) -> dict[str, list[str]]:
    """``model id -> config ids offering it``, in the order *configs* is given.

    Overlap is a property of the whole list, so it is indexed once per listing
    rather than per config: a gateway can advertise ~1000 models, and the
    listing is now fetched by agent pages too.
    """
    index: dict[str, list[str]] = {}
    for config in configs:
        config_id = str(config.id)
        # Deduplicate per config, for the same reason ``get_available_models``
        # does: ``available_models`` is an unconstrained JSON list, so a config
        # CAN list a model twice, which would inflate the badge count.
        for model in dict.fromkeys(config.available_models or []):
            index.setdefault(model, []).append(config_id)
    return index


def _model_overlaps(
    config: LLMProviderConfig,
    owner_index: "dict[str, list[str]] | None" = None,
) -> list[LLMModelOverlap]:
    """Model ids *config* shares with another config, and who else offers them.

    Empty when nothing is shared, and when *owner_index* is not supplied — a
    single-config serialization has nothing to compare against, so it reports no
    overlap rather than guessing one.
    """
    mine = list(dict.fromkeys(config.available_models or []))
    if not mine or not owner_index:
        return []
    config_id = str(config.id)
    overlaps: list[LLMModelOverlap] = []
    for model in mine:
        sharers = [
            other for other in owner_index.get(model, ()) if other != config_id
        ]
        if sharers:
            overlaps.append(LLMModelOverlap(model=model, provider_ids=sharers))
    return overlaps


def _gateway_url(config: LLMProviderConfig) -> str | None:
    """The URL of a Filigran AI Gateway config; ``None`` for other providers or an unreadable credential.

    Scheme, host and path only: userinfo, query and fragment may carry a secret.
    """
    if config.provider != "filigran_gateway" or not config.api_key_enc:
        return None
    # `.port` raises on a malformed port, and env-seeded values are not validated:
    # one bad row must not fail the whole provider list.
    try:
        url = parse_gateway_credential(decrypt_stored_value(config.api_key_enc)).base_url
        parts = urlsplit(url)
        # A vault reference is not a URL: nothing to show.
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return None
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        port = f":{parts.port}" if parts.port else ""
    except Exception:
        logger.debug("Unreadable gateway credential for %s", config.id, exc_info=True)
        return None
    return f"{parts.scheme}://{host}{port}{parts.path}".rstrip("/")


def _serialize_llm_provider_response(
    config: LLMProviderConfig,
    owner_index: "dict[str, list[str]] | None" = None,
    *,
    with_audience: bool = True,
) -> LLMProviderResponse:
    """Serialize provider config safely for API responses.

    *with_audience* serves who it is restricted to: admins only.
    """
    return LLMProviderResponse(
        access_restricted=is_restricted(config),
        shared_group_ids=(
            canonical_ids(getattr(config, "shared_group_ids", None)) if with_audience else []
        ),
        shared_user_ids=(
            canonical_ids(getattr(config, "shared_user_ids", None)) if with_audience else []
        ),
        excluded_group_ids=(
            canonical_ids(getattr(config, "excluded_group_ids", None)) if with_audience else []
        ),
        excluded_user_ids=(
            canonical_ids(getattr(config, "excluded_user_ids", None)) if with_audience else []
        ),
        id=str(config.id),
        provider=config.provider,
        gateway_url=_gateway_url(config) if with_audience else None,
        display_name=config.display_name,
        default_model=config.default_model,
        available_models=config.available_models,
        context_windows=config.context_windows,
        max_output_tokens=config.max_output_tokens,
        # Safety net for legacy rows created before this column existed.
        is_from_env=bool(config.is_from_env),
        models_env_locked=models_env_locked(config),
        model_overlaps=_model_overlaps(config, owner_index),
        # The key's readable location is for those who edit providers: administrators.
        vault_secrets=vault_status(config, ADMIN_PRINCIPAL if with_audience else None),
        source_integration_id=(
            str(source) if (source := getattr(config, "source_integration_id", None)) else None
        ),
        capabilities=provider_capabilities(config.provider),
        lists_decision_models=_lists_decision_models(config),
    )


@router.get("/llm-providers", response_model=list[LLMProviderResponse])
async def list_llm_providers(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the LLM providers: every one for an admin, the ones they may use for anyone else.

    A provider restricted to groups and users is listed only for them,
    and only an admin is told who they are.
    """
    # Oldest first, tie-broken by id: several configs can exist per provider, and
    # an unordered select let their order shift between requests — which the UI
    # then showed as a reshuffling list.
    result = await db.execute(
        select(LLMProviderConfig).order_by(
            LLMProviderConfig.created_at, LLMProviderConfig.id
        )
    )
    configs = list(result.scalars().all())
    if not user.is_admin:
        configs = (await provider_routing(db, str(user.id), configs)).usable
    owner_index = build_model_owner_index(configs)
    responses = [
        _serialize_llm_provider_response(c, owner_index, with_audience=user.is_admin)
        for c in configs
    ]
    if user.is_admin:
        listed, followable = _listed_catalog_ids(configs), _followable_catalog_ids(configs)
        for response in responses:
            response.models_listed = response.id in listed
            response.models_followable = response.id in followable
    await attach_vault_failures(responses, LLMProviderConfig.__tablename__)
    return responses


def _listed_catalog_ids(configs: list[LLMProviderConfig]) -> set[str]:
    """Ids of the env-seeded configs whose catalog the boot seed would list again."""
    from app.llm.env_listing import catalogs_to_list

    return _env_config_ids(configs, catalogs_to_list)


def _followable_catalog_ids(configs: list[LLMProviderConfig]) -> set[str]:
    """Ids of the env-seeded configs whose admin-edited catalog may follow the listing again."""
    from app.llm.env_listing import catalogs_to_follow

    return _env_config_ids(configs, catalogs_to_follow)


def _env_config_ids(configs: list[LLMProviderConfig], rule) -> set[str]:
    from app.core.config import settings as app_settings
    from app.llm.env_listing import env_provider_keys

    names = rule(env_provider_keys(warn=False), configs, app_settings.provider_models_admin_curated)
    return {str(c.id) for c in configs if c.provider in names and c.user_id is None and c.is_from_env}


async def _snapshot_offered_models(db: AsyncSession) -> "CatalogSnapshot | None":
    """The models the users pick from before a provider edit, to tell what it changes.

    ``None`` when they cannot be read: the edit then goes through unrecorded -
    model announcements never stand in the way of managing providers.
    """
    from app.services import model_catalog_announcements as announcements

    try:
        async with db.begin_nested():
            return await announcements.snapshot_catalogs(db)
    except Exception as exc:
        logger.warning(
            "Model announcements: could not read the models before a provider edit: %s",
            format_exception_message(exc),
        )
        return None


async def _record_offered_models_edit(
    db: AsyncSession, before: "CatalogSnapshot | None"
) -> ModelCatalogChangeSummary | None:
    """Record the models a provider edit added or retired; the model changes the admin is offered to email.

    ``None`` when the edit changed nothing the users pick from.
    """
    if before is None:
        return None
    from app.api.v1.model_announcements import catalog_change_summary
    from app.services import model_catalog_announcements as announcements

    try:
        async with db.begin_nested():
            draft = await announcements.record_provider_edit(db, before)
    except Exception as exc:
        logger.warning(
            "Model announcements: could not record the models a provider edit changed: %s",
            format_exception_message(exc),
        )
        return None
    return catalog_change_summary(draft) if draft is not None else None


@router.post(
    "/llm-providers",
    response_model=LLMProviderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_llm_provider(
    body: LLMProviderRequest,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    return await create_provider_config(db, body=body, user=user, request=request)


async def create_provider_config(
    db: AsyncSession,
    *,
    body: LLMProviderRequest,
    user: User,
    request: Request | None,
    source_integration_id: uuid.UUID | None = None,
) -> LLMProviderResponse:
    """Create an LLM provider config, commit it and drop the provider cache.

    The one create path: ``POST /settings/llm-providers`` and an integration
    that populates the platform provider (``source_integration_id``,
    ``services/openrouter_llm_provider.py``) both go through it, so both get
    the env-only lock, the platform-default seeding, the model announcement
    and the audit. The caller has checked the admin role and the AI switch.
    """
    ensure_llm_provider_management_allowed()

    if body.provider not in PROVIDER_MAP:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported provider. Use one of: {', '.join(sorted(PROVIDER_MAP))}.",
        )
    _ensure_new_provider_allowed(body.provider, from_integration=source_integration_id is not None)

    # Shared: creations never wait on each other, a deletion waits for this one
    # (it may seed the platform defaults) and this one for a deletion.
    await lock_provider_configs(db, shared=True)
    offered_before = await _snapshot_offered_models(db)

    # If available_models provided, ensure default_model is in the list
    available = body.available_models or (
        [body.default_model] if body.default_model else []
    )

    config = LLMProviderConfig(
        user_id=user.id,
        provider=body.provider,
        display_name=body.display_name,
        api_key_enc=encrypt_value(body.api_key),
        default_model=body.default_model,
        available_models=available if available else None,
        context_windows=body.context_windows,
        max_output_tokens=body.max_output_tokens,
        source_integration_id=source_integration_id,
    )
    try:
        access_change = await apply_provider_access(
            db,
            config,
            access_restricted=body.access_restricted,
            group_ids=body.shared_group_ids,
            user_ids=body.shared_user_ids,
            excluded_group_ids=body.excluded_group_ids,
            excluded_user_ids=body.excluded_user_ids,
        )
    except ProviderAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    db.add(config)
    await db.flush()

    # Auto-set platform default model if none exists yet
    first_model = (available[0] if available else None) or body.default_model
    if first_model:
        await _ensure_platform_default_model(
            db,
            body.provider,
            first_model,
            provider_id=str(config.id),
            available_models=available or None,
        )
    catalog_change = await _record_offered_models_edit(db, offered_before)

    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="llm_provider",
        entity_id=config.id,
        entity_name=config.provider,
        summary=f"Configured LLM provider '{config.provider}'",
        details={
            "provider": config.provider,
            "available_models": config.available_models,
            **({"access": access_change["after"]} if access_change else {}),
            "source_integration_id": (
                str(source_integration_id) if source_integration_id else None
            ),
        },
        request=request,
    )
    # Commit before answering (and before dropping the cache, which must not
    # be refilled from the old rows): the get_db teardown only commits after
    # the response is sent.
    await db.commit()

    from app.llm.router import invalidate_provider_cache

    invalidate_provider_cache()

    # No overlap on this response by design: the client invalidates the provider
    # list on success, and the refetched list carries ``model_overlaps`` for
    # every config — computed once there instead of a full-table scan on each
    # save, and it survives a reload rather than living in one response (FR-010).
    response = _serialize_llm_provider_response(config)
    response.model_catalog_change = catalog_change
    return response


async def _load_deletable_llm_provider(
    db: AsyncSession, provider_id: str
) -> LLMProviderConfig:
    """The config a deletion, or its preview, acts on: 404 when absent.

    Env-only mode is not re-checked here: both callers ask
    ``ensure_llm_provider_management_allowed()`` one line earlier, which is
    the single gate (a duplicate would only ever
    agree with it, and would have to be kept in lockstep by hand).
    """
    result = await db.execute(
        select(LLMProviderConfig).where(
            LLMProviderConfig.id == parse_path_id(provider_id, not_found_detail="Provider config not found"),
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Provider config not found")
    return config


async def _load_general_assistant_config(
    db: AsyncSession,
) -> "GeneralAssistantConfig | None":
    from app.models.general_assistant_config import GeneralAssistantConfig

    # ``populate_existing``: read after the provider config lock, the row must
    # be what is committed now, not a copy the session loaded before the wait.
    return (
        await db.execute(
            select(GeneralAssistantConfig)
            .limit(1)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _load_locked_platform_settings(db: AsyncSession) -> "PlatformSettings":
    """The PlatformSettings singleton as committed, read under the provider config lock.

    The Terms of Use gate may have loaded it into this session before the lock
    was granted, and a plain query hands that copy back unchanged, so a
    deletion committed meanwhile would be invisible to this write.
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings

    ps = await get_platform_settings(db)
    await db.refresh(ps)
    return ps


async def _llm_provider_deletion_impact(
    db: AsyncSession,
    config: LLMProviderConfig,
    replacement_provider_id: str | None,
    *,
    platform_settings: "PlatformSettings | None",
    general_assistant: "GeneralAssistantConfig | None",
) -> DeletionImpact:
    """What deleting *config* does - the one computation the preview and the delete share.

    A ``replacement_provider_id`` that is not a surviving config able to serve
    chat is a 422, never a silent fallback to the rule's pick.
    """
    replacement_id = (
        parse_uuid(replacement_provider_id, label="replacement_provider_id")
        if replacement_provider_id
        else None
    )
    try:
        return await compute_deletion_impact(
            db,
            config,
            replacement_id=replacement_id,
            platform_settings=platform_settings,
            general_assistant=general_assistant,
            env_managed_fields=_env_managed_ga_fields(),
        )
    except ReplacementError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _lock_provider_configs(db: AsyncSession) -> None:
    """Serialize against concurrent deletions, catalog edits and reference writes.

    The impact is computed from the survivors and applied after the delete, so
    a replacement removed or re-cataloged meanwhile would be pinned stale (and
    two deletions could re-seed onto each other). The table is small; rows are
    locked in id order so two deletions never deadlock.
    """
    await lock_provider_configs(db)


async def _apply_provider_deletion(
    db: AsyncSession,
    config: LLMProviderConfig,
    impact: DeletionImpact,
    *,
    platform_settings: "PlatformSettings | None",
    general_assistant: "GeneralAssistantConfig | None",
    summary_prefix: str,
    notice_reason: str = "deleted by an administrator",
) -> tuple[str, dict[str, object], dict[str, int]]:
    """Delete *config* and apply *impact* - the one write path of every deletion.

    Returns the audit summary, the audit details and the cleanup counts; the
    caller records the audit entry with its own actor and commits.
    """
    config_id = config.id
    provider_name = config.provider
    removed_models = set(config.available_models or [])
    # An env-provisioned config comes back, under a new id, at the next boot.
    from_env = bool(config.is_from_env)
    await db.delete(config)

    cleanup = await _cleanup_orphaned_model_references(
        db,
        provider_name,
        removed_models,
        provider_deleted=True,
        provider_id=config_id,
    )
    # The cleanup reads the same singleton rows through this session, so it
    # nulled these very objects; re-seed them from the impact.
    apply_deletion_impact(
        impact, platform_settings=platform_settings, general_assistant=general_assistant
    )
    # No surviving config serves the embedding model: the default is cleared
    # and indexing waits for an administrator, so tell them in the bell.
    from app.services.llm_provider_deletion import notify_admins_embedding_cleared

    notified = await notify_admins_embedding_cleared(db, impact, reason=notice_reason)

    summary = summary_prefix
    for label, route in (
        ("platform default", impact.agent_default),
        ("General Assistant", impact.general_assistant),
    ):
        if route.action != "reseeded" or route.after is None:
            continue
        where = f"'{route.after.display_name or route.after.provider}'"
        if route.model_kept:
            summary += f"; {label} keeps {route.after.model} on {where}"
        else:
            summary += f"; {label} moved to {route.after.model or 'its default model'} on {where}"
    for label, route in (
        ("embedding default", impact.embedding_default),
        ("routing model", impact.routing_model),
        ("fallback model", impact.fallback_model),
        ("decision model", impact.decision_model),
    ):
        if route.action == "reseeded" and route.after is not None:
            where = f"'{route.after.display_name or route.after.provider}'"
            summary += f"; {label} keeps {route.after.model} on {where}"
        elif route.action == "cleared" and route.before is not None and route.before.model:
            summary += f"; {label} {route.before.model} cleared (no remaining provider serves it)"
    details: dict[str, object] = {
        **impact.audit_details(),
        "is_from_env": from_env,
        "references_reset": cleanup,
        "admins_notified_embedding_cleared": notified,
    }
    return summary, details, cleanup


async def delete_provider_config_as_system(
    db: AsyncSession, config: LLMProviderConfig, *, actor: str, reason: str
) -> dict[str, int]:
    """Delete *config* on the platform's own initiative, by the rule an admin gets.

    For the provisioning prune of a provider whose environment variable was
    removed: the same impact (the rule's replacement, nothing asked), the same
    writes, and an audit entry naming the system actor. The caller holds the
    provider config lock (:func:`_lock_provider_configs`) and commits.
    """
    from app.services.audit import log_system_audit
    from app.services.default_model_routes import mark_cause

    mark_cause(db, "env_provider_prune" if actor == "env_provider_prune" else "provider_deleted")
    ps = await _load_locked_platform_settings(db)
    ga = await _load_general_assistant_config(db)
    impact = await compute_deletion_impact(
        db,
        config,
        platform_settings=ps,
        general_assistant=ga,
        env_managed_fields=_env_managed_ga_fields(),
    )
    config_id, provider_name = config.id, config.provider
    summary, details, cleanup = await _apply_provider_deletion(
        db,
        config,
        impact,
        platform_settings=ps,
        general_assistant=ga,
        summary_prefix=f"Deleted LLM provider '{provider_name}' ({reason})",
        notice_reason=reason,
    )
    # ``AuditLog.user_id`` is NOT NULL: the entry is anchored on the oldest
    # admin, the system actor is what the row names as having acted.
    anchor = (
        await db.execute(
            select(User.id)
            .where(User.role == "admin")
            .order_by(User.created_at, User.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if anchor is None:
        logger.warning(
            "Deleted LLM provider '%s' (%s) with no admin to anchor its audit entry on: %s",
            provider_name,
            reason,
            summary,
        )
    else:
        await log_system_audit(
            db,
            actor=actor,
            anchor_user_id=anchor,
            action="delete",
            entity_type="llm_provider",
            entity_id=config_id,
            entity_name=provider_name,
            summary=summary,
            details={**details, "reason": reason},
        )
    return cleanup


def _route_target_response(target: RouteTarget | None) -> ProviderRouteTarget | None:
    if target is None:
        return None
    return ProviderRouteTarget(
        provider=target.provider,
        provider_id=str(target.provider_id) if target.provider_id else None,
        display_name=target.display_name,
        model=target.model,
    )


def _deletion_route_response(route: RouteImpact) -> dict[str, object]:
    return {
        "affected": route.affected,
        "action": route.action,
        "before": _route_target_response(route.before),
        "after": _route_target_response(route.after),
        "env_managed": route.env_managed,
        "model_kept": route.model_kept,
    }


def _serialize_deletion_impact(impact: DeletionImpact) -> LLMProviderDeletionImpactResponse:
    embedding = impact.embedding_default
    return LLMProviderDeletionImpactResponse(
        provider_id=str(impact.deleted.provider_id),
        provider=impact.deleted.provider or "",
        display_name=impact.deleted.display_name,
        replacement=_route_target_response(impact.replacement),
        replacement_explicit=impact.replacement_explicit,
        default_replacement_id=(
            str(impact.default_replacement_id) if impact.default_replacement_id else None
        ),
        candidates=[_route_target_response(c) for c in impact.candidates],
        chat_available=impact.chat_available,
        unlisted=[_route_target_response(t) for t in impact.unlisted],
        implicit_chat_fallback=_route_target_response(impact.implicit_chat_fallback),
        implicit_chat_moves=impact.implicit_chat_moves,
        previous_implicit_chat=_route_target_response(impact.previous_implicit_chat),
        general_assistant=ProviderDeletionRoute(**_deletion_route_response(impact.general_assistant)),
        agent_default=ProviderDeletionRoute(**_deletion_route_response(impact.agent_default)),
        image_default=ProviderDeletionRoute(**_deletion_route_response(impact.image_default)),
        embedding_default=ProviderDeletionEmbeddingRoute(
            **_deletion_route_response(embedding),
            fallback=_route_target_response(embedding.fallback),
            fallback_same_model=embedding.fallback_same_model,
            knowledge_base_count=embedding.knowledge_base_count,
            fallback_moves=embedding.fallback_moves,
            previous_fallback=_route_target_response(embedding.previous_fallback),
        ),
        routing_model=ProviderDeletionRoute(**_deletion_route_response(impact.routing_model)),
        fallback_model=ProviderDeletionRoute(**_deletion_route_response(impact.fallback_model)),
        decision_model=ProviderDeletionRoute(**_deletion_route_response(impact.decision_model)),
        agents=ProviderDeletionReferences(count=impact.agents.count, names=impact.agents.names),
        users=ProviderDeletionReferences(count=impact.users.count, names=impact.users.names),
        impact_fingerprint=impact.fingerprint(),
    )


@router.get(
    "/llm-providers/{provider_id}/deletion-impact",
    response_model=LLMProviderDeletionImpactResponse,
)
async def get_llm_provider_deletion_impact(
    provider_id: str,
    replacement_provider_id: str | None = None,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Preview what deleting a provider config does to every route that uses it.

    Admin only, behind the same guards as the delete. Nothing is written: the
    delete recomputes this with the same function and the same
    ``replacement_provider_id``, and with the ``impact_fingerprint`` returned
    here as its ``expected_impact`` it applies this or nothing.
    """
    from app.api.v1.platform_settings_helpers import peek_platform_settings

    ensure_llm_provider_management_allowed()
    config = await _load_deletable_llm_provider(db, provider_id)
    impact = await _llm_provider_deletion_impact(
        db,
        config,
        replacement_provider_id,
        platform_settings=await peek_platform_settings(db),
        general_assistant=await _load_general_assistant_config(db),
    )
    return _serialize_deletion_impact(impact)


@router.delete("/llm-providers/{provider_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_llm_provider(
    provider_id: str,
    request: Request,
    replacement_provider_id: str | None = None,
    expected_impact: str | None = None,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Delete a platform LLM provider config (admin only).

    Every route that used it is decided by the same computation as
    ``GET .../deletion-impact``: the platform default model and the
    General Assistant chat model move to the replacement - the requested
    ``replacement_provider_id``, else the first surviving chat-capable config,
    oldest first - pinned by id; the image default moves to a surviving config
    that can serve images; the embedding default is cleared, never switched;
    agents and user preferences pinned to the config are reset and inherit the
    defaults. All in one transaction, committed before the response so the
    Settings refetch reads the result, and recorded on the audit entry.

    Serialized with the other deletions and catalog edits, so the replacement
    is re-validated against what they committed. ``expected_impact`` (the
    preview's ``impact_fingerprint``) makes the delete conditional: a 409
    carrying the fresh preview when what it would apply has changed since.
    """
    ensure_llm_provider_management_allowed()
    from app.services.default_model_routes import mark_cause

    mark_cause(db, "provider_deleted", user.id)
    await _lock_provider_configs(db)
    config = await _load_deletable_llm_provider(db, provider_id)
    ps = await _load_locked_platform_settings(db)
    ga = await _load_general_assistant_config(db)
    impact = await _llm_provider_deletion_impact(
        db, config, replacement_provider_id, platform_settings=ps, general_assistant=ga
    )
    if expected_impact is not None and expected_impact != impact.fingerprint():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "deletion_impact_changed",
                "message": (
                    "What this deletion changes is no longer what was reviewed: "
                    "another change was saved in the meantime. Review it again."
                ),
                "impact": _serialize_deletion_impact(impact).model_dump(mode="json"),
            },
        )

    config_id = config.id
    provider_name = config.provider
    offered_before = await _snapshot_offered_models(db)
    summary, details, cleanup = await _apply_provider_deletion(
        db,
        config,
        impact,
        platform_settings=ps,
        general_assistant=ga,
        summary_prefix=f"Deleted LLM provider '{provider_name}'",
    )
    # Settings reads it from the announcement status: a 204 carries no body.
    await _record_offered_models_edit(db, offered_before)
    replacement = impact.replacement
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="llm_provider",
        entity_id=config_id,
        entity_name=provider_name,
        summary=summary,
        details=details,
        request=request,
    )
    await db.commit()

    from app.llm.router import invalidate_provider_cache

    invalidate_provider_cache()
    logger.info(
        "Admin %s deleted provider '%s' (%s): reset %d agent(s), %d user pref(s), "
        "%d config field(s); replacement: %s",
        user.email,
        provider_name,
        config_id,
        cleanup["agents"],
        cleanup["users"],
        cleanup["ga_fields"],
        f"{replacement.model} on {replacement.provider_id}" if replacement else "none",
    )


# ---------------------------------------------------------------------------
# Model resolution – fetch available models from provider API
# ---------------------------------------------------------------------------


@router.post("/llm-providers/resolve-models", response_model=ResolveModelsResponse)
async def resolve_models_with_key(
    body: ResolveModelsRequest,
    user: User = Depends(require_admin),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Resolve available models by providing a provider name and API key (before saving)."""
    # Gated: this reaches a provider, and FR-007 admits no exception —
    # "no request to any model or embedding provider". Configure and test a
    # provider *after* enabling AI, not before.
    provider_cls = PROVIDER_MAP.get(body.provider)
    if not provider_cls:
        raise HTTPException(
            status_code=400, detail=f"Unsupported provider: {body.provider}"
        )
    _ensure_new_provider_allowed(body.provider)
    if problem := reference_problem(body.api_key):
        raise HTTPException(status_code=422, detail=f"api_key: {problem}")
    api_key = await resolve_secret_text(body.api_key)

    try:
        # Attribute gateway calls to the admin performing the resolution (the
        # gateway provider signs its XTM JWT from this identity).
        set_llm_caller(user.id, user.email)
        provider = provider_cls(api_key=api_key)
        models, groups = await _list_chat_models(provider)
        return ResolveModelsResponse(
            provider=body.provider, models=models, model_groups=groups
        )
    except Exception as e:
        message = format_exception_message(e)
        gateway_token = (
            parse_gateway_credential(api_key).token
            if body.provider == "filigran_gateway"
            else None
        )
        # A gateway may echo the token it rejected.
        message = redact_token(message, gateway_token)
        logger.warning("Failed to resolve models for %s: %s", body.provider, message)
        detail = f"Failed to fetch models: {message}"
        if body.provider == "filigran_gateway" and not gateway_token:
            detail += (
                " — check that the gateway trusts this platform's BASE_URL as an "
                "issuer and can reach its /xtm/auth/jwks endpoint."
            )
        raise HTTPException(status_code=400, detail=detail) from e


# Providers that can ONLY do embeddings and expose no model-discovery API, so
# their validation must always fall back to a connectivity probe regardless of
# the capability requested by the caller.
_EMBEDDING_ONLY_PROVIDERS = EMBEDDING_ONLY_PROVIDERS

# OpenAI-compatible providers eligible for the connectivity-probe validation
# fallback when model discovery is unavailable. ``custom_openai`` / ``litellm``
# also support chat, so they only fall back for the embedding capability; the
# embedding-only providers fall back regardless of the requested capability.
_PROBE_FALLBACK_PROVIDERS = frozenset({"custom_openai", "litellm"}) | _EMBEDDING_ONLY_PROVIDERS


def _openai_compatible_auth_headers(raw_api_key: str) -> dict[str, str]:
    """Build Authorization headers for compound OpenAI-compatible credentials."""
    api_key = raw_api_key.split("||")[0].strip()
    if not api_key or api_key.lower() in {"none", "dummy"}:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def _openai_compatible_base_url(raw_api_key: str) -> str | None:
    """Extract base URL from ``api_key||base_url`` compound credentials."""
    parts = raw_api_key.split("||")
    if len(parts) < 2:
        return None
    base_url = parts[1].strip().rstrip("/")
    if not base_url:
        return None
    return base_url


def _classify_target_host(
    hostname: str,
) -> tuple[bool, str | None, bool]:
    """SSRF pre-check that does not perform DNS resolution.

    Returns ``(allowed, reason, needs_dns)``:
      - ``allowed=False`` short-circuits the caller (block / report reason).
      - ``allowed=True`` and ``needs_dns=False`` means the hostname is an
        already-validated public IP literal and the caller is done.
      - ``allowed=True`` and ``needs_dns=True`` means the caller must
        resolve DNS (sync or via the event loop) and validate the
        resolved addresses with :func:`_check_resolved_ips`.
    """
    host = hostname.strip().lower()
    if not host:
        return False, "Endpoint URL must include a host.", False

    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return (
            False,
            "Local/private hostnames are not allowed for endpoint probing.",
            False,
        )

    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True, None, True

    if not ip.is_global:
        return False, "Private or non-routable IP addresses are not allowed.", False
    return True, None, False


def _check_resolved_ips(
    addr_info: list[tuple],
) -> tuple[bool, str | None]:
    """Validate that every DNS-resolved address is globally routable."""
    resolved_ips: set[str] = set()
    for info in addr_info:
        sockaddr = info[4]
        if not sockaddr:
            continue
        resolved_ips.add(sockaddr[0])

    if not resolved_ips:
        return False, "Endpoint hostname did not resolve to any IP address."

    for resolved in resolved_ips:
        try:
            parsed_ip = ipaddress.ip_address(resolved)
        except ValueError:
            return False, "Endpoint hostname resolved to an invalid IP address."
        if not parsed_ip.is_global:
            return False, "Endpoint resolves to a private or non-routable IP address."

    return True, None


def _is_public_target_host(hostname: str) -> tuple[bool, str | None]:
    """Return whether the hostname resolves only to public, routable IPs.

    Synchronous variant — only safe to call from non-async contexts.
    Async endpoints must use :func:`_is_public_target_host_async` to keep
    the event loop responsive while DNS resolution is in flight.
    """
    allowed, reason, needs_dns = _classify_target_host(hostname)
    if not allowed or not needs_dns:
        return allowed, reason

    host = hostname.strip().lower()
    try:
        addr_info = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False, "Endpoint hostname could not be resolved."

    return _check_resolved_ips(addr_info)


async def _is_public_target_host_async(
    hostname: str,
) -> tuple[bool, str | None]:
    """Async variant of :func:`_is_public_target_host`.

    DNS resolution is delegated to the running event loop so the
    blocking ``getaddrinfo`` call cannot stall the server for other
    concurrent requests.
    """
    allowed, reason, needs_dns = _classify_target_host(hostname)
    if not allowed or not needs_dns:
        return allowed, reason

    host = hostname.strip().lower()
    loop = asyncio.get_running_loop()
    try:
        addr_info = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False, "Endpoint hostname could not be resolved."

    return _check_resolved_ips(addr_info)


async def _normalize_probe_base_url(
    raw_api_key: str,
) -> tuple[str | None, str | None]:
    """Normalize and validate probe target URL to mitigate SSRF risks.

    Asynchronous because DNS resolution for the hostname check is
    awaited on the event loop's executor.
    """
    base_url = _openai_compatible_base_url(raw_api_key)
    if not base_url:
        return None, "Missing base URL (expected api_key||base_url)."

    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        return None, (
            f"Invalid URL scheme '{parsed.scheme}'. "
            "Only http:// and https:// endpoints are supported."
        )

    if parsed.username or parsed.password:
        return None, "Userinfo is not allowed in endpoint URLs."

    if not parsed.hostname:
        return None, "Endpoint URL must include a valid hostname."

    allowed, reason = await _is_public_target_host_async(parsed.hostname)
    if not allowed:
        return None, reason

    normalized = parsed._replace(fragment="", query="").geturl().rstrip("/")
    return normalized, None


async def _probe_openai_compatible_connection(raw_api_key: str) -> tuple[bool, str]:
    """Probe basic connectivity for OpenAI-compatible endpoints.

    This is used as a fallback when ``/models`` is unavailable for
    embedding-only endpoints.
    """
    base_url, base_url_error = await _normalize_probe_base_url(raw_api_key)
    if not base_url:
        return False, base_url_error or "Invalid endpoint URL."

    base_variants = (
        [base_url, base_url[:-3]]
        if base_url.endswith("/v1")
        else [base_url, f"{base_url}/v1"]
    )

    # Keep probe order deterministic and unique.
    unique_variants: list[str] = []
    for candidate in base_variants:
        normalized = candidate.rstrip("/")
        if normalized and normalized not in unique_variants:
            unique_variants.append(normalized)

    headers = _openai_compatible_auth_headers(raw_api_key)
    embed_probe_payload = {"model": "__validation_probe__", "input": "ping"}

    def _auth_error_message(status_code: int, route: str) -> tuple[bool, str]:
        return (
            False,
            "Endpoint is reachable but authentication was rejected "
            f"(HTTP {status_code}) on {route}.",
        )

    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=10.0)) as client:
        # Probe model listing route first on both base variants.
        for base in unique_variants:
            models_route = f"{base}/models"
            try:
                resp = await client.get(models_route, headers=headers)
            except httpx.HTTPError as exc:
                logger.debug(
                    "Provider validation probe failed for route %s: %s",
                    models_route,
                    format_exception_message(exc),
                )
                continue

            if resp.status_code in {401, 403}:
                return _auth_error_message(resp.status_code, models_route)

            if resp.status_code == 200:
                return True, (
                    "Endpoint is reachable and supports model discovery via "
                    f"{models_route}."
                )

        # If /models is unavailable, detect embedding-only routes.
        for base in unique_variants:
            for suffix in ("embeddings", "embedding", "embed"):
                route = f"{base}/{suffix}"
                try:
                    resp = await client.post(
                        route,
                        headers=headers,
                        json=embed_probe_payload,
                    )
                except httpx.HTTPError as exc:
                    logger.debug(
                        "Provider validation probe failed for route %s: %s",
                        route,
                        format_exception_message(exc),
                    )
                    continue

                if resp.status_code in {401, 403}:
                    return _auth_error_message(resp.status_code, route)

                # 404 => route missing. 5xx => server-side failure for this route.
                # Any other code means the route exists and credentials reached the API.
                if resp.status_code != 404 and resp.status_code < 500:
                    return True, (
                        "Endpoint is reachable and exposes embedding route "
                        f"{route} (HTTP {resp.status_code}), even without model discovery."
                    )

        # Final fallback: probe base URL itself for generic reachability.
        # If reachable but API routes are missing, return a strict non-success signal.
        reachable_host_status: int | None = None
        for base in unique_variants:
            try:
                resp = await client.get(base, headers=headers)
            except httpx.HTTPError as exc:
                logger.debug(
                    "Provider validation probe failed for base URL %s: %s",
                    base,
                    format_exception_message(exc),
                )
                continue

            if resp.status_code in {401, 403}:
                return _auth_error_message(resp.status_code, base)

            if resp.status_code < 500:
                reachable_host_status = resp.status_code

        if reachable_host_status is not None:
            return False, (
                "Endpoint host is reachable with provided URL/token "
                f"(HTTP {reachable_host_status}), but API endpoint was not detected."
            )

    return False, (
        "Unable to validate OpenAI-compatible endpoint: no accessible "
        "/models, /embeddings, /embedding, or /embed route was detected."
    )


@router.post(
    "/llm-providers/validate",
    response_model=ValidateProviderResponse,
)
async def validate_provider_with_key(
    body: ValidateProviderRequest,
    user: User = Depends(require_admin),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Validate provider credentials for a target capability before saving.

    Validation strategy:
    - Try capability-specific model discovery first.
    - For embedding + OpenAI-compatible providers, fall back to endpoint
      connectivity probing when discovery APIs are unavailable.
    """
    # Gated: ``provider.list_embedding_models()`` is an outbound provider
    # request, which FR-007 forbids outright while AI is off.

    provider_cls = PROVIDER_MAP.get(body.provider)
    if not provider_cls:
        raise HTTPException(
            status_code=400, detail=f"Unsupported provider: {body.provider}"
        )
    _ensure_new_provider_allowed(body.provider)
    # A key typed as a vault reference is validated as the key it names.
    if problem := reference_problem(body.api_key):
        raise HTTPException(status_code=422, detail=f"api_key: {problem}")
    api_key = await resolve_secret_text(body.api_key)
    # A provider serving one capability (an embedding server, a voice
    # generator) is validated for it, whatever flow added it.
    capability = getattr(provider_cls, "only_capability", None) or body.capability

    try:
        # Attribute gateway calls to the admin performing the validation.
        set_llm_caller(user.id, user.email)
        provider = provider_cls(api_key=api_key)
        groups = None
        if capability == "embedding":
            models = await provider.list_embedding_models()
        elif capability == "image":
            models = await provider.list_image_models()
        elif capability == "speech":
            models = await provider.list_speech_models()
        elif capability == "video":
            models = await provider.list_video_models()
        else:
            # For chat/reasoning, also surface the per-provider grouping when
            # the provider exposes it (the gateway) so the picker groups by
            # provider — derived from the same call, not a second one.
            models, groups = await _list_chat_models(provider)

        if models:
            return ValidateProviderResponse(
                provider=body.provider,
                capability=capability,
                connection_ok=True,
                models_supported=True,
                models=models,
                model_groups=groups,
                message="Provider validated and models discovered successfully.",
            )
    except Exception as exc:
        logger.info(
            "Provider validation model discovery failed for %s/%s: %s",
            body.provider,
            capability,
            format_exception_message(exc),
        )
        from app.llm.speech import SpeechGenerationError
        from app.llm.video import VideoGenerationError

        if isinstance(exc, SpeechGenerationError | VideoGenerationError):
            # Worded to be shown: the provider's own reason, never the key.
            return ValidateProviderResponse(
                provider=body.provider,
                capability=capability,
                connection_ok=False,
                models_supported=False,
                models=[],
                message=str(exc),
            )

    # Connectivity-probe fallback for OpenAI-compatible / raw-/embed endpoints
    # that may have no model-discovery API. ``tei`` is embedding-only and
    # exposes no ``/models`` route, so it must be probed regardless of the
    # capability the client requested — otherwise validating it as
    # ``reasoning``/``image`` (e.g. from the generic "Add Provider" flow) hard
    # fails with "no fallback validation is available" and the provider can
    # never be saved. ``custom_openai`` / ``litellm`` stay gated to the
    # embedding capability (they support chat too, where model discovery is the
    # right validation path).
    if body.provider in _PROBE_FALLBACK_PROVIDERS and (
        body.capability == "embedding" or body.provider in _EMBEDDING_ONLY_PROVIDERS
    ):
        connection_ok, message = await _probe_openai_compatible_connection(api_key)
        return ValidateProviderResponse(
            provider=body.provider,
            capability=(
                "embedding"
                if body.provider in _EMBEDDING_ONLY_PROVIDERS
                else body.capability
            ),
            connection_ok=connection_ok,
            models_supported=False,
            models=[],
            message=(
                message
                if connection_ok
                else (
                    "Unable to validate embedding provider connectivity. "
                    f"{message}"
                )
            ),
        )

    return ValidateProviderResponse(
        provider=body.provider,
        capability=capability,
        connection_ok=False,
        models_supported=False,
        models=[],
        message=(
            "Unable to validate provider for this capability. "
            "Model discovery failed and no fallback validation is available."
        ),
    )


@router.get("/llm-providers/{provider_id}/models", response_model=ResolveModelsResponse)
async def list_provider_models(
    provider_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Fetch the chat models of an already-configured provider (uses stored key).

    Its catalogue is what chats: the image, video, speech and embedding models
    it lists are chosen in their own Settings section, and a provider that
    does not chat (``provider_capabilities``) offers none here.
    """
    # Gated even though it is a GET: FR-008 keeps *our stored data* readable,
    # but this fetches the model list from the provider, so it is an outbound
    # request and FR-007 admits no exception.

    result = await db.execute(
        select(LLMProviderConfig).where(
            LLMProviderConfig.id == parse_path_id(provider_id, not_found_detail="Provider config not found"),
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Provider config not found")

    provider_cls = PROVIDER_MAP.get(config.provider)
    if not provider_cls:
        raise HTTPException(
            status_code=400, detail=f"Unsupported provider: {config.provider}"
        )

    try:
        # Attribute gateway calls to the requesting user.
        set_llm_caller(user.id, user.email)
        api_key = await reveal_field(config, "api_key_enc") or ""
        provider = provider_instance(provider_cls, api_key, config.id)
        offered = None
        if "chat" not in provider_capabilities(config.provider):
            return ResolveModelsResponse(provider=config.provider, models=[], offered_models=None)
        if isinstance(provider, FiligranGatewayProvider):
            models, offered = await provider.list_models_and_offered()
        else:
            models = await provider.list_models()
        return ResolveModelsResponse(
            provider=config.provider,
            models=[m for m in models if is_chat_capable_model(m)],
            offered_models=offered,
        )
    except SecretUnavailableError:
        # The generic 502 of a vault read (main.py's handler), not a provider error.
        raise
    except Exception as e:
        logger.warning("Failed to fetch models for provider %s: %s", provider_id, format_exception_message(e))
        raise HTTPException(
            status_code=400,
            detail=f"Failed to fetch models: {format_exception_message(e)}",
        ) from e


@router.post(
    "/llm-providers/{provider_id}/context-windows/scan",
    response_model=ScanContextWindowsResponse,
)
async def scan_provider_context_windows(
    provider_id: str,
    body: ScanContextWindowsRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Look up the context window of some of a provider's models.

    Read-only: the administrator saves what it found with the models
    (``context_windows`` of the ``PUT``). Gated like the model listing, since
    it reads the provider's listing (FR-007).
    """
    from app.llm.context_window_scan import scan_context_windows

    config = (
        await db.execute(
            select(LLMProviderConfig).where(
                LLMProviderConfig.id
                == parse_path_id(provider_id, not_found_detail="Provider config not found"),
            )
        )
    ).scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Provider config not found")
    provider_cls = PROVIDER_MAP.get(config.provider)
    if not provider_cls:
        raise HTTPException(
            status_code=400, detail=f"Unsupported provider: {config.provider}"
        )

    set_llm_caller(user.id, user.email)
    api_key = await reveal_field(config, "api_key_enc") or ""
    try:
        provider = provider_instance(provider_cls, api_key, config.id)
    except Exception as e:
        logger.warning(
            "Context window scan: provider %s could not be built: %r",
            provider_id,
            format_exception_message(e)[:300],
        )
        raise HTTPException(
            status_code=400, detail="The provider could not be reached with its stored settings."
        ) from e
    scan = await scan_context_windows(provider, body.models)
    return ScanContextWindowsResponse(
        provider=config.provider,
        live_lookup=scan.live_lookup,
        results=[
            ScannedContextWindowItem(
                model=item.model, context_window=item.tokens, source=item.source
            )
            for item in scan.results
        ],
    )


@router.post("/llm-providers/{provider_id}/refresh-models", response_model=LLMProviderResponse)
async def refresh_llm_provider_models(
    provider_id: str,
    request: Request,
    body: RefreshModelsRequest | None = None,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """List an env-seeded provider's models again, as a boot would, without a restart.

    For a catalog that is the provider's own listing (``models_listed``),
    env-only mode included: it applies the environment, not an admin edit.
    With ``discard_edits``, also for one an admin edited (``models_followable``),
    which then follows the listing again. Nothing else of the env configuration
    is re-applied.
    """
    from app.core.config import settings as app_settings
    from app.core.migrations import bootstrap_lock
    from app.llm.env_listing import (
        apply_listing,
        declared_default,
        env_provider_keys,
        list_models_for_seed,
        listing_platform_model,
        replaced_alias,
    )
    from app.main import _byollm_install_configured
    from app.services.model_catalog_announcements import model_catalog_watch

    config_id = parse_path_id(provider_id, not_found_detail="Provider config not found")
    discard_edits = bool(body and body.discard_edits)

    async def _listed_config() -> LLMProviderConfig:
        # Fresh rows even for objects the session still holds: the read before
        # the provider call ends with a commit, which expires nothing.
        configs = list(
            (await db.execute(select(LLMProviderConfig).execution_options(populate_existing=True)))
            .scalars()
            .all()
        )
        config = next((c for c in configs if c.id == config_id), None)
        if config is None:
            raise HTTPException(status_code=404, detail="Provider config not found")
        allowed = _listed_catalog_ids(configs)
        if discard_edits:
            allowed |= _followable_catalog_ids(configs)
        if str(config.id) not in allowed:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"The '{config.provider}' model list is not read from the provider: "
                    f"{models_env_var(config.provider)} is set, the list was edited in Settings, "
                    "or another provider serves the chat models."
                ),
            )
        return config

    provider = (await _listed_config()).provider
    # No transaction held while the provider answers. A commit, not a rollback:
    # a rollback expires the user loaded on this session (MissingGreenlet).
    await db.commit()
    set_llm_caller(user.id, user.email)
    listing = await list_models_for_seed(provider, dict(env_provider_keys(warn=False))[provider])
    if listing is None:
        raise HTTPException(
            status_code=502,
            detail=f"The '{provider}' provider did not list its models; the model list is unchanged.",
        )
    try:
        # The catalog watch records the change for the users' emails, under the
        # lock it requires; bounded like the AI toggle.
        async with bootstrap_lock(wait_timeout=25), model_catalog_watch():
            await lock_provider_configs(db)
            config = await _listed_config()  # an admin may have edited it meanwhile
            listed = listing.models
            removed = sorted(set(config.available_models or []) - set(listed))
            # Read before anything is written, as at boot.
            install_configured = await _byollm_install_configured(db)
            apply_listing(config, listed, listing.chat_default)
            # A first listing fills the empty platform defaults, as boot does.
            declared = declared_default(config.provider, listing.chat_default, listed)
            if model := listing_platform_model(listed, config.default_model, app_settings.default_model, declared):
                await _ensure_platform_default_model(
                    db,
                    config.provider,
                    model,
                    provider_id=str(config.id),
                    available_models=None,
                    fill_empty_defaults=not install_configured,
                    replaces=replaced_alias(config.provider, declared),
                )
            await log_audit(
                db,
                user=user,
                action="update",
                entity_type="llm_provider",
                entity_id=config.id,
                entity_name=config.provider,
                summary=f"Refreshed the models of LLM provider '{config.provider}' from its listing",
                details={
                    "provider": config.provider,
                    "available_models": config.available_models,
                    "removed_models": removed or None,
                    "discarded_edits": discard_edits,
                },
                request=request,
            )
            await db.commit()
    except TimeoutError as exc:
        raise HTTPException(
            status_code=503,
            detail="Another instance is applying the provider configuration; try again in a minute.",
        ) from exc
    from app.llm.router import invalidate_provider_cache

    # After the commit, as for a catalog edit.
    invalidate_provider_cache()

    configs = list((await db.execute(select(LLMProviderConfig))).scalars().all())
    config = next(c for c in configs if c.id == config_id)
    response = _serialize_llm_provider_response(config, build_model_owner_index(configs))
    response.models_listed = str(config.id) in _listed_catalog_ids(configs)
    response.models_followable = str(config.id) in _followable_catalog_ids(configs)
    return response


# ---------------------------------------------------------------------------
# Update LLM provider (available models, default model)
# ---------------------------------------------------------------------------


@router.put("/llm-providers/{provider_id}", response_model=LLMProviderResponse)
async def update_llm_provider(
    provider_id: str,
    body: LLMProviderUpdateRequest,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Update an LLM provider's available models and/or default model."""
    config_id = parse_path_id(provider_id, not_found_detail="Provider config not found")
    return await update_provider_config(
        db, config_id=config_id, body=body, user=user, request=request
    )


async def update_provider_config(
    db: AsyncSession,
    *,
    config_id: uuid.UUID,
    body: LLMProviderUpdateRequest,
    user: User,
    request: Request | None,
) -> LLMProviderResponse:
    """Apply an edit to an LLM provider config, commit it and drop the provider cache.

    The one update path: ``PUT /settings/llm-providers/{id}`` and the model
    selection of an integration that feeds the provider both go through it,
    with the env locks, the cleanup of removed models, the platform-default
    seeding, the model announcement and the audit. The caller has checked the
    admin role and the AI switch.
    """
    ensure_llm_provider_management_allowed()
    # The edit clears the references to the models it removes and may seed the
    # platform defaults: serialized like a deletion. The row lock alone
    # misses a writer that checked its model against this config after its scan.
    await _lock_provider_configs(db)

    result = await db.execute(
        select(LLMProviderConfig).where(LLMProviderConfig.id == config_id)
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Provider config not found")
    from app.core.config import settings as app_settings
    # Env-only mode was already refused by
    # ``ensure_llm_provider_management_allowed()`` at the top of this
    # function; the duplicate check that used to sit here could only ever
    # agree with it.
    #
    # The model catalog of an env-seeded provider is env-pinned unless the
    # operator opted into admin curation: the boot seed would silently revert
    # the edit on the next restart, so refuse it up front with the way out.
    # Rename / context-window / max-output edits are untouched by
    # the seed and stay allowed.
    touches_catalog = body.available_models is not None or body.default_model is not None
    if touches_catalog and models_env_locked(config, app_settings):
        raise HTTPException(
            status_code=403,
            detail=(
                f"The model catalog of the '{config.provider}' provider is "
                f"managed by the environment ({models_env_var(config.provider)}) "
                "because the provider was provisioned from environment "
                "variables. Set LLM_PROVIDER_MODELS_EDITABLE=true and restart "
                "the backend to curate it from the UI, or edit the environment "
                "variable."
            ),
        )

    old_models = set(config.available_models or [])
    # Restricting a provider or opening it changes what everyone picks from too.
    touches_access = (
        body.access_restricted is not None
        or body.shared_group_ids is not None
        or body.shared_user_ids is not None
        or body.excluded_group_ids is not None
        or body.excluded_user_ids is not None
    )
    offered_before = (
        await _snapshot_offered_models(db)
        if body.available_models is not None or touches_access
        else None
    )

    access_change = None
    if touches_access:
        try:
            access_change = await apply_provider_access(
                db,
                config,
                access_restricted=body.access_restricted,
                group_ids=body.shared_group_ids,
                user_ids=body.shared_user_ids,
                excluded_group_ids=body.excluded_group_ids,
                excluded_user_ids=body.excluded_user_ids,
            )
        except ProviderAccessError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if body.available_models is not None:
        config.available_models = body.available_models
    if "display_name" in body.model_fields_set:
        config.display_name = body.display_name
    if body.default_model is not None:
        config.default_model = body.default_model
    if "context_windows" in body.model_fields_set:
        config.context_windows = body.context_windows
    if "max_output_tokens" in body.model_fields_set:
        config.max_output_tokens = body.max_output_tokens

    await db.flush()

    # Clean up references to models that were just removed
    new_models = set(config.available_models or [])
    removed_models = old_models - new_models
    if removed_models:
        await _cleanup_orphaned_model_references(
            db, config.provider, removed_models, provider_id=config.id
        )

    # Auto-populate platform defaults (GA chat model + embedding + image) when
    # this update is the first time the provider has any models. Without this,
    # the common "create provider with empty models → pick models via the
    # picker → Save" flow leaves ``platform_settings.image_*`` / ``embedding_*``
    # NULL forever, which strands the Image Generation and Embedding sections
    # in an empty state with no UI affordance to recover. The helper itself
    # short-circuits on each field that's already set, so it's safe to call on
    # every update (it never overwrites an admin's explicit choice).
    first_model = (
        (config.available_models[0] if config.available_models else None)
        or config.default_model
    )
    if first_model:
        await _ensure_platform_default_model(
            db,
            config.provider,
            first_model,
            provider_id=str(config.id),
            available_models=config.available_models or None,
        )
    catalog_change = await _record_offered_models_edit(db, offered_before)

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="llm_provider",
        entity_id=config.id,
        entity_name=config.provider,
        summary=f"Updated LLM provider '{config.provider}'",
        details={
            "provider": config.provider,
            "available_models": config.available_models,
            "removed_models": sorted(removed_models) if removed_models else None,
            **({"access": access_change} if access_change else {}),
        },
        request=request,
    )
    # Commit before answering (and before dropping the cache, which must not
    # be refilled from the old rows): the get_db teardown only commits after
    # the response is sent.
    await db.commit()

    from app.llm.router import invalidate_provider_cache

    invalidate_provider_cache()

    # No overlap on this response by design: the client invalidates the provider
    # list on success, and the refetched list carries ``model_overlaps`` for
    # every config — computed once there instead of a full-table scan on each
    # save, and it survives a reload rather than living in one response (FR-010).
    response = _serialize_llm_provider_response(config)
    response.model_catalog_change = catalog_change
    return response


# ---------------------------------------------------------------------------
# Available models (flat list across all providers)
# ---------------------------------------------------------------------------


@router.get("/available-models", response_model=list[AvailableModelItem])
async def list_available_models(
    scope: EntityListScope = Query(
        "visible",
        description=(
            "visible (default): the models of the providers the caller may use. "
            "all: every configured provider's, for an admin configuring the platform."
        ),
    ),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return all *chat-capable* models across all configured providers.

    This is the source for every chat-model picker in the UI (General
    Assistant, agent model, platform default, profile preference). Image and
    embedding models live in a provider's ``available_models`` when that
    provider also backs the Image Generation / Embeddings sections, so they are
    filtered out here via ``is_chat_capable_model`` — otherwise an admin could
    pick e.g. ``gpt-image-1`` as a chat model, which is the root cause of the
    "my default model is an image model" failure. Image / embedding
    models are selected through their dedicated capability endpoints
    (``/available-image-models`` / ``/available-embedding-models``).

    A provider restricted to groups and users is listed only for them;
    ``scope=all`` lists it for an admin too, who sets models platform-wide.
    """
    from app.llm.router import get_available_models

    models = await get_available_models(
        db,
        user_id=str(user.id),
        every_provider=list_scope_admin_bypass(scope, user),
    )
    return [
        AvailableModelItem(**m)
        for m in models
        if is_chat_capable_model(m.get("model"))
    ]


# ---------------------------------------------------------------------------
# Available embedding models (flat list across all providers)
# ---------------------------------------------------------------------------


@router.get("/available-embedding-models", response_model=list[AvailableModelItem])
async def list_available_embedding_models(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return available embedding models across all configured providers."""
    return await _models_for_capability(db, "embedding", user)


# ---------------------------------------------------------------------------
# Available image generation models (flat list across all providers)
# ---------------------------------------------------------------------------


@router.get("/available-image-models", response_model=list[AvailableModelItem])
async def list_available_image_models(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return available image generation models across all configured providers."""
    return await _models_for_capability(db, "image", user)


# ---------------------------------------------------------------------------
# Available video generation models (flat list across all providers)
# ---------------------------------------------------------------------------


@router.get("/available-video-models", response_model=list[AvailableModelItem])
async def list_available_video_models(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return available video generation models across all configured providers.

    Each provider answers from its own model list (``list_video_models``):
    Sora on OpenAI and Azure OpenAI, Veo on Gemini, and whatever video models
    an OpenAI-compatible gateway (LiteLLM, a custom endpoint, the Filigran
    gateway) serves behind the OpenAI Videos API. A model its provider no
    longer serves (it answered HTTP 404, ``video_served``) is left out, as in
    the Video Studio.
    """
    return await _models_for_capability(db, "video", user)


@router.get("/video-model-status", response_model=VideoModelStatus)
async def video_model_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Why the video model chosen in Settings can no longer make a video, if it cannot (a retired model)."""
    return VideoModelStatus(unavailable=await platform_video_model_problem(db))


# ---------------------------------------------------------------------------
# Speech generation: models and voices
# ---------------------------------------------------------------------------


@router.get("/available-speech-models", response_model=list[AvailableModelItem])
async def list_available_speech_models(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return available text-to-speech models across all configured providers.

    Each provider answers from its own model list (``list_speech_models``):
    OpenAI and Azure OpenAI, Gemini, ElevenLabs, and whatever speech models an
    OpenAI-compatible gateway (LiteLLM, a custom endpoint, the Filigran
    gateway) serves behind the OpenAI speech API.
    """
    return await _models_for_capability(db, "speech", user)


@router.get("/available-music-models", response_model=list[AvailableModelItem])
async def list_available_music_models(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return available music generation models across all configured providers.

    Each provider answers from its own model list (``list_music_models``):
    Eleven Music on ElevenLabs, Lyria on Gemini.
    """
    return await _models_for_capability(db, "music", user)


@router.get("/speech-voices", response_model=SpeechVoicesResponse)
async def list_speech_voices(
    provider_id: str | None = Query(None),
    model: str | None = Query(None, max_length=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The voices of a speech model, the platform's speech model when none is named.

    Empty while AI is off, like the model lists: listing voices asks the
    provider. A model whose voices cannot be listed answers no voice and why,
    never an error, so the picker can still take a typed voice.
    """
    from app.llm.router import list_voices_for
    from app.services.ai_state import is_ai_enabled

    if provider_id is not None and safe_uuid(provider_id) is None:
        raise HTTPException(status_code=422, detail="provider_id: not a valid id.")
    if not await is_ai_enabled(db):
        return SpeechVoicesResponse(voices=[], message="AI is disabled.")
    set_llm_caller(user.id, user.email)
    try:
        target, voices = await list_voices_for(db, user_id=user.id, provider_id=provider_id, model=model or None)
    except ValueError as exc:
        return SpeechVoicesResponse(voices=[], message=str(exc))
    except Exception as exc:
        logger.info("Speech voices not listed: %s", format_exception_message(exc))
        return SpeechVoicesResponse(voices=[], message="The provider did not list its voices.")
    return SpeechVoicesResponse(
        provider=target.config.provider,
        provider_id=str(target.config.id),
        model=target.model,
        voices=[SpeechVoiceItem(**v.as_dict()) for v in voices],
        message=None if voices else "This model lists no voice: type the voice its provider expects.",
    )


# ---------------------------------------------------------------------------
# Available decision models (flat list across all providers)
# ---------------------------------------------------------------------------


@router.get("/available-decision-models", response_model=AvailableDecisionModelsResponse)
async def list_available_decision_models(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the decision models (CLMs) the providers the caller may use report.

    Only the providers the caller may use are asked: a restricted
    provider's ids and models are not listed to whoever opens Settings, and
    it is never called on their behalf. ``listings`` says why a provider that
    lists its decision models offered none.
    """
    listings: dict[str, DecisionListingStatus] = {}
    models = await _models_for_capability(db, "decision", user, usable_only=True, listings=listings)
    return AvailableDecisionModelsResponse(
        models=models,
        listings=[DecisionListing(provider_id=pid, status=status) for pid, status in listings.items()],
    )


@router.post("/test-embedding-model", response_model=EmbeddingModelTestResponse)
async def test_embedding_model(
    body: EmbeddingModelTestRequest,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Actively test whether a provider/model can produce valid embeddings.

    Performs a real embedding call and returns the detected vector dimension.
    If embedding_dimensions is already configured in PlatformSettings, also
    validates that the model output matches the configured value.
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings as _load_ps

    provider_cls = PROVIDER_MAP.get(body.provider)
    if not provider_cls:
        raise HTTPException(
            status_code=400, detail=f"Unsupported provider: {body.provider}"
        )

    if body.provider_id:
        from app.core.utils import is_valid_uuid

        if not is_valid_uuid(body.provider_id):
            raise HTTPException(status_code=400, detail="Invalid provider_id format.")
        result = await db.execute(
            select(LLMProviderConfig).where(
                LLMProviderConfig.id == body.provider_id,
                LLMProviderConfig.provider == body.provider,
            )
        )
        config = result.scalar_one_or_none()
    else:
        result = await db.execute(
            select(LLMProviderConfig).where(LLMProviderConfig.provider == body.provider)
        )
        config = result.scalars().first()
    if not config:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Provider '{body.provider}' is not configured. "
                "Add and save the provider before testing embedding capability."
            ),
        )

    ps = await _load_ps(db)
    configured_dims: int | None = getattr(ps, "embedding_dimensions", None)
    from app.services.branding import get_branding

    branding = await get_branding(db)
    probe_text = body.text or f"Embedding capability probe for {branding.name}"

    # Read before the test: a key its vault cannot hand over is the generic 502, not "not capable".
    api_key = await reveal_field(config, "api_key_enc") or ""
    try:
        # Attribute gateway calls to the admin performing the test.
        set_llm_caller(user.id, user.email)
        provider = provider_instance(provider_cls, api_key, config.id)
        requested_dimensions: int | None = None
        probe_candidates: tuple[int, ...] = ()
        if body.provider == "custom_openai" and isinstance(provider, CustomOpenAIProvider):
            configured_or_default = max(configured_dims or 0, 4096)
            probe_candidates = _build_probe_candidates(max_probe=configured_or_default)
            vector, requested_dimensions = await provider.probe_supported_dimensions(
                probe_text,
                model=body.model,
                candidates=probe_candidates,
            )
        else:
            vector = await provider.embed(probe_text, model=body.model)
        dims = len(vector)

        suggested_dimensions = _suggest_smaller_dimensions(dims)

        # Dimensions that were tried before the successful one (custom_openai probe only)
        failed_candidates: list[int] = []
        if probe_candidates and requested_dimensions is not None:
            for c in probe_candidates:
                if c == requested_dimensions:
                    break
                failed_candidates.append(c)

        if configured_dims is not None and dims != configured_dims:
            return EmbeddingModelTestResponse(
                provider=body.provider,
                model=body.model,
                capable=True,
                returned_dimensions=dims,
                requested_dimensions=requested_dimensions,
                suggested_dimensions=suggested_dimensions,
                expected_dimensions=configured_dims,
                message=(
                    f"Model supports {dims} dimensions, but the platform is "
                    f"currently configured for {configured_dims}. Save Vector "
                    "Dimensions to apply this value and trigger re-embedding."
                ),
            )

        # Build probe detail suffix
        if requested_dimensions is not None:
            if dims == requested_dimensions:
                probe_detail = (
                    f"model returned {dims}-dimensional vectors as requested"
                )
            else:
                probe_detail = (
                    "endpoint accepted the requested dimensions "
                    f"({requested_dimensions}) but returned {dims}-dimensional "
                    "vectors"
                )
            if failed_candidates:
                failed_str = ", ".join(str(d) for d in failed_candidates)
                probe_detail += f" (rejected: {failed_str})"
            success_msg = f"Embedding test passed — {probe_detail}."
        else:
            success_msg = f"Embedding test passed — model produces {dims}-dimensional vectors."

        return EmbeddingModelTestResponse(
            provider=body.provider,
            model=body.model,
            capable=True,
            returned_dimensions=dims,
            requested_dimensions=requested_dimensions,
            suggested_dimensions=suggested_dimensions,
            expected_dimensions=configured_dims,
            message=(
                success_msg
                + (
                    ""
                    if configured_dims is not None
                    else " Set Vector Dimensions to lock in this value."
                )
            ),
        )
    except Exception as e:
        logger.warning(
            "Embedding model test failed for %s/%s: %s",
            body.provider,
            body.model,
            format_exception_message(e),
        )
        return EmbeddingModelTestResponse(
            provider=body.provider,
            model=body.model,
            capable=False,
            returned_dimensions=None,
            requested_dimensions=None,
            suggested_dimensions=[],
            expected_dimensions=configured_dims,
            message=f"Embedding test failed: {format_exception_message(e)}",
        )


# ---------------------------------------------------------------------------
# General Assistant Configuration (singleton)
# ---------------------------------------------------------------------------


@router.get("/general-assistant", response_model=GeneralAssistantConfigResponse)
async def get_general_assistant_config(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the General Assistant configuration (any authenticated user).

    Merges fields from both ``GeneralAssistantConfig`` (GA-only) and
    ``PlatformSettings`` (moved platform-wide defaults) into a single
    backward-compatible response.
    """
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )
    from app.models.general_assistant_config import (
        DEFAULT_MAX_ITERATIONS,
        GeneralAssistantConfig,
        default_system_prompt,
    )

    result = await db.execute(select(GeneralAssistantConfig).limit(1))
    config = result.scalar_one_or_none()
    ps = await _load_ps(db)

    if not config:
        return GeneralAssistantConfigResponse(
            linked_agent_id=None,
            max_iterations=DEFAULT_MAX_ITERATIONS,
            system_prompt=default_system_prompt(),
            agent_default_provider=ps.agent_default_provider,
            agent_default_provider_id=(
                str(ps.agent_default_provider_id)
                if getattr(ps, "agent_default_provider_id", None)
                else None
            ),
            agent_default_model=ps.agent_default_model,
            default_reasoning_effort=ps.default_reasoning_effort,
            embedding_provider=ps.embedding_provider,
            embedding_provider_id=(
                str(ps.embedding_provider_id)
                if getattr(ps, "embedding_provider_id", None)
                else None
            ),
            embedding_model=ps.embedding_model,
            embedding_dimensions=getattr(ps, "embedding_dimensions", None),
            image_provider=ps.image_provider,
            image_provider_id=(
                str(ps.image_provider_id)
                if getattr(ps, "image_provider_id", None)
                else None
            ),
            image_model=ps.image_model,
            video_provider=getattr(ps, "video_provider", None),
            video_provider_id=(
                str(ps.video_provider_id)
                if getattr(ps, "video_provider_id", None)
                else None
            ),
            video_model=getattr(ps, "video_model", None),
            video_fallback_models=list(getattr(ps, "video_fallback_models", None) or []),
            speech_provider=getattr(ps, "speech_provider", None),
            speech_provider_id=(
                str(ps.speech_provider_id)
                if getattr(ps, "speech_provider_id", None)
                else None
            ),
            speech_model=getattr(ps, "speech_model", None),
            speech_voice=getattr(ps, "speech_voice", None),
            music_provider=getattr(ps, "music_provider", None),
            music_provider_id=(
                str(ps.music_provider_id)
                if getattr(ps, "music_provider_id", None)
                else None
            ),
            music_model=getattr(ps, "music_model", None),
            routing_model=getattr(ps, "routing_model", None),
            routing_provider_id=(
                str(getattr(ps, "routing_provider_id", None))
                if getattr(ps, "routing_provider_id", None)
                else None
            ),
            decision_model=getattr(ps, "decision_model", None),
            decision_provider_id=(
                str(getattr(ps, "decision_provider_id", None))
                if getattr(ps, "decision_provider_id", None)
                else None
            ),
            fallback_model=getattr(ps, "fallback_model", None),
            fallback_provider_id=(
                str(getattr(ps, "fallback_provider_id", None))
                if getattr(ps, "fallback_provider_id", None)
                else None
            ),
            knowledge_base_ids=[],
            mcp_server_ids=[],
            custom_tool_ids=[],
            builtin_tool_ids=[],
            default_skill_slugs=list(general_assistant_skills.GENERAL_ASSISTANT_SKILL_SLUGS),
            integration_bindings={},
            handover_agent_ids=[],
            handover_types={},
            available_channel_types=["web"],
            mcp_exposed_agent_ids=ps.mcp_exposed_agent_ids or [],
            mcp_global_api_key_prefix=ps.mcp_global_api_key_prefix,
            platform_mcp_enabled=bool(
                getattr(ps, "platform_mcp_enabled", True)
            ),
            xtm_mcp_enabled=bool(getattr(ps, "xtm_mcp_enabled", False)),
            reply_suggestions_enabled=getattr(ps, "reply_suggestions_enabled", True) is not False,
            env_managed_fields=sorted(_env_managed_ga_fields()),
        )

    return _build_ga_response(config, ps)


def _refuse_suites_the_ga_cannot_run(requested: list[str], stored: list | None) -> None:
    """Refuse to add a built-in suite ``general_chat`` does not wire.

    Only what the write ADDS is checked: an id saved before this gate existed
    stays until someone removes it, so an unrelated edit never fails on it.
    The three gates below compare with the configuration this request read, so
    two administrators saving at once can at worst keep an entry that was
    stored a moment before, never add one a gate refuses.
    """
    from app.models.general_assistant_config import (
        GENERAL_ASSISTANT_BUILTIN_GROUP_IDS,
        GENERAL_ASSISTANT_BUILTIN_TOOL_IDS,
    )

    kept = {str(tool_id) for tool_id in stored or []}
    # A group and its [DEPRECATED] former ids are both accepted.
    runnable = set(GENERAL_ASSISTANT_BUILTIN_TOOL_IDS) | set(GENERAL_ASSISTANT_BUILTIN_GROUP_IDS)
    refused = sorted(
        {
            str(tool_id)
            for tool_id in requested
            if str(tool_id) not in kept
            and tool_id not in runnable
        }
    )
    if refused:
        raise HTTPException(
            status_code=422,
            detail=(
                f"The General Assistant cannot run {', '.join(refused)}. "
                f"Supported: {', '.join(GENERAL_ASSISTANT_BUILTIN_GROUP_IDS)}. "
                "Agent-scoped suites (work items, shared state, trace "
                "inspection) need an agent: enable them on one."
            ),
        )


def _refuse_handover_types_the_ga_cannot_run(
    requested: dict[str, str], stored: dict | None
) -> None:
    """Refuse a handover type the General Assistant does not implement.

    Only an entry the write adds or changes is checked: a ``mandatory_transfer``
    saved before this gate existed runs as delegate
    (``general_assistant_handover_type``), and a client that saves the whole
    map sends it back unchanged.
    """
    from app.models.general_assistant_config import GENERAL_ASSISTANT_HANDOVER_TYPES

    kept = {str(agent_id): handover_type for agent_id, handover_type in (stored or {}).items()}
    refused = sorted(
        {
            str(handover_type)
            for agent_id, handover_type in requested.items()
            if kept.get(str(agent_id)) != handover_type
            and handover_type not in GENERAL_ASSISTANT_HANDOVER_TYPES
        }
    )
    if refused:
        raise HTTPException(
            status_code=422,
            detail=(
                "The General Assistant hands over with "
                f"{', '.join(GENERAL_ASSISTANT_HANDOVER_TYPES)}, not "
                f"{', '.join(refused)}. A mandatory transfer is only available "
                "on agents."
            ),
        )


async def _refuse_unreachable_ga_handover_targets(
    db: AsyncSession, requested: list[str], stored: list | None
) -> list[str]:
    """Refuse a new General Assistant handover target not every user reaches.

    The General Assistant serves every user, and ``general_chat`` only offers
    the enabled targets the CHATTING user can see: a private or group-shared
    agent, or a company-managed one narrowed with ``access_restricted``,
    reaches the people who can see it and silently disappears for everyone
    else, and a disabled one reaches nobody. Same rule as a company-managed
    agent's handovers. Only what the write adds is checked, like the
    built-in suites above.

    Returns the ids the write adds, as sent.
    """
    kept = {str(agent_id) for agent_id in stored or []}
    added: dict[str, uuid.UUID] = {}
    for raw_id in requested:
        if str(raw_id) in kept:
            continue
        parsed = safe_uuid(str(raw_id))
        if parsed is None:
            raise HTTPException(
                status_code=422, detail=f"Invalid handover agent id: {raw_id!r}."
            )
        added[str(raw_id)] = parsed
    if not added:
        return []
    rows = (
        await db.execute(
            select(
                Agent.id,
                Agent.name,
                Agent.enabled,
                Agent.is_company_managed,
                Agent.access_restricted,
            ).where(Agent.id.in_(list(added.values())))
        )
    ).all()
    by_id = {row.id: row for row in rows}
    for agent_id in added.values():
        row = by_id.get(agent_id)
        if row is None:
            raise HTTPException(
                status_code=422, detail=f"Handover agent {agent_id} was not found."
            )
        if not row.is_company_managed:
            reason = "is not company-managed"
        elif row.access_restricted:
            reason = "is restricted to the people granted access"
        elif not row.enabled:
            reason = "is disabled"
        else:
            continue
        raise HTTPException(
            status_code=422,
            detail=(
                "The General Assistant can only hand over to enabled "
                "company-managed agents every user can reach. "
                f'"{row.name}" {reason}.'
            ),
        )
    return list(added)


# The fields of a General Assistant save that choose a model or a provider:
# what a provider deletion re-seeds or clears.
_GA_PROVIDER_REFERENCE_FIELDS = frozenset(
    {
        "llm_provider",
        "llm_provider_id",
        "llm_model",
        "agent_default_provider",
        "agent_default_provider_id",
        "agent_default_model",
        "embedding_provider",
        "embedding_provider_id",
        "embedding_model",
        "image_provider",
        "image_provider_id",
        "image_model",
        "video_provider",
        "video_provider_id",
        "video_model",
        "speech_provider",
        "speech_provider_id",
        "speech_model",
        "speech_voice",
        "music_provider",
        "music_provider_id",
        "music_model",
        "routing_model",
        "routing_provider_id",
        "fallback_model",
        "fallback_provider_id",
        "decision_model",
        "decision_provider_id",
    }
)

# (request field, the singleton storing it, how the refusal names it)
_GA_PROVIDER_ID_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("llm_provider_id", "general_assistant", "the General Assistant model"),
    ("agent_default_provider_id", "platform", "the platform default model"),
    ("embedding_provider_id", "platform", "the embedding model"),
    ("image_provider_id", "platform", "the image model"),
    ("video_provider_id", "platform", "the video model"),
    ("speech_provider_id", "platform", "the speech model"),
    ("music_provider_id", "platform", "the music model"),
    ("routing_provider_id", "platform", "the routing model"),
    ("fallback_provider_id", "platform", "the fallback model"),
    ("decision_provider_id", "platform", "the decision model"),
)

# The PlatformSettings (provider name, provider id) pairs a General Assistant
# save completes when one side is missing.
_GA_ALIGNED_PROVIDER_PAIRS: tuple[tuple[str, str], ...] = (
    ("agent_default_provider", "agent_default_provider_id"),
    ("embedding_provider", "embedding_provider_id"),
    ("image_provider", "image_provider_id"),
    ("video_provider", "video_provider_id"),
    ("speech_provider", "speech_provider_id"),
    ("music_provider", "music_provider_id"),
)


def _completes_a_provider_pair(ps: "PlatformSettings | None") -> bool:
    """Whether a save would fill in the missing side of a stored (name, id) pair.

    A default stored by provider name only (saved before the reference columns
    existed, never backfilled) is given the id of the newest config of that
    provider by any save, one that touches no model field included: a provider
    reference write like the others.
    """
    if ps is None:
        return False
    for provider_attr, provider_id_attr in _GA_ALIGNED_PROVIDER_PAIRS:
        provider_name = getattr(ps, provider_attr, None)
        provider_uuid = getattr(ps, provider_id_attr, None)
        if (provider_uuid and not provider_name) or (provider_name and provider_uuid is None):
            return True
    return False


async def _refuse_missing_ga_provider_references(
    db: AsyncSession,
    body: GeneralAssistantConfigRequest,
    config: "GeneralAssistantConfig",
    ps: "PlatformSettings",
) -> None:
    """422 for a provider id the save adds or changes that names no config."""
    for field, owner, label in _GA_PROVIDER_ID_FIELDS:
        if field not in body.model_fields_set:
            continue
        raw = getattr(body, field)
        requested = parse_uuid(raw, label=field) if raw else None
        stored = getattr(config if owner == "general_assistant" else ps, field, None)
        problem = await provider_reference_problem(db, requested, stored=stored, label=label)
        if problem:
            raise HTTPException(status_code=422, detail=problem)


async def _write_decision_model(
    db: AsyncSession,
    ps: "PlatformSettings",
    body: GeneralAssistantConfigRequest,
) -> None:
    """Store the decision model and the config serving it, or clear both.

    A decision model is named with its provider: decision models are seldom in a
    provider's model listing, so there is no owner to look up by model id, and a
    provider that serves no decision model would fail every ``act_on_page`` call.
    The provider's existence is checked with the other references
    (``_GA_PROVIDER_ID_FIELDS``).
    """
    from app.services.flow_classifier import decision_provider_types

    if not body.decision_model:
        ps.decision_model = None
        ps.decision_provider_id = None
        return
    if not body.decision_provider_id:
        raise HTTPException(
            status_code=422, detail="Pick the provider that serves the decision model."
        )
    provider_id = parse_uuid(body.decision_provider_id, label="decision_provider_id")
    config = await db.get(LLMProviderConfig, provider_id)
    if config is not None and config.provider not in decision_provider_types():
        raise HTTPException(
            status_code=422,
            detail="The provider selected for the decision model serves no decision models.",
        )
    ps.decision_model = body.decision_model
    ps.decision_provider_id = provider_id


#: The platform model defaults an administrator sets on the General Assistant
#: page, audited before and after on every save that changes one of them.
_AUDITED_MODEL_DEFAULT_FIELDS: tuple[str, ...] = (
    "agent_default_provider",
    "agent_default_provider_id",
    "agent_default_model",
    "embedding_provider",
    "embedding_provider_id",
    "embedding_model",
    "embedding_dimensions",
    "image_provider",
    "image_provider_id",
    "image_model",
    "speech_provider",
    "speech_provider_id",
    "speech_model",
    "routing_model",
    "routing_provider_id",
    "fallback_model",
    "fallback_provider_id",
    "decision_model",
    "decision_provider_id",
)


def _model_defaults_snapshot(ps: "PlatformSettings") -> dict[str, object]:
    """The audited model defaults of *ps*, as JSON-safe values."""
    snapshot: dict[str, object] = {}
    for field in _AUDITED_MODEL_DEFAULT_FIELDS:
        value = getattr(ps, field, None)
        snapshot[field] = str(value) if isinstance(value, uuid.UUID) else value
    return snapshot


def _model_defaults_diff(before: dict[str, object], after: dict[str, object]) -> dict[str, dict[str, object]]:
    """``{field: {"before": ..., "after": ...}}`` for each audited default that changed."""
    return {
        field: {"before": before.get(field), "after": after.get(field)}
        for field in _AUDITED_MODEL_DEFAULT_FIELDS
        if before.get(field) != after.get(field)
    }


@router.put("/general-assistant", response_model=GeneralAssistantConfigResponse)
async def update_general_assistant_config(
    body: GeneralAssistantConfigRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create or update the General Assistant configuration (admin only).

    GA-only fields are written to ``GeneralAssistantConfig``.  Moved
    platform-wide fields (agent_default_*, embedding_*, image_*, MCP, license)
    are written to ``PlatformSettings``.
    """
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )
    from app.models.general_assistant_config import (
        DEFAULT_GA_BUILTIN_TOOL_IDS,
        GeneralAssistantConfig,
        clamp_max_iterations,
        default_system_prompt,
    )
    from app.services.default_model_routes import mark_cause

    # The route changes this save commits are raised as settings_page (the
    # agent tool named agent_tool first when it is the caller).
    mark_cause(db, "settings_page", user.id)
    # A save that chooses a model or a provider is serialized with provider
    # deletions, which re-seed or clear these very fields: locked before
    # either singleton is read, so each side decides from what the other committed.
    writes_provider_reference = bool(
        _GA_PROVIDER_REFERENCE_FIELDS & body.model_fields_set
    )
    if writes_provider_reference:
        await _lock_provider_configs(db)
    config = await _load_general_assistant_config(db)

    if not config:
        config = GeneralAssistantConfig(
            system_prompt=default_system_prompt(),
            available_channel_types=["web"],
            # See sibling creation site above for rationale — the core
            # artifact tools ship ON by default so the Copilot General
            # Assistant can produce files and decks without admin opt-in.
            builtin_tool_ids=list(DEFAULT_GA_BUILTIN_TOOL_IDS),
        )
        db.add(config)

    ps = (
        await _load_locked_platform_settings(db)
        if writes_provider_reference
        else await _load_ps(db)
    )
    if not writes_provider_reference and _completes_a_provider_pair(ps):
        # Completing the pair below writes a provider reference too. Nothing a
        # deletion reads has been written yet (at most a new General Assistant
        # row no other transaction sees), so the lock still comes first: both
        # rows are re-read once it is granted.
        writes_provider_reference = True
        await _lock_provider_configs(db)
        config = await _load_general_assistant_config(db) or config
        ps = await _load_locked_platform_settings(db)

    # Reject mutations of env-seeded default fields.  Env vars are re-applied
    # to PlatformSettings on every startup (env always wins), so allowing an
    # edit here would silently revert on the next restart — the same reason
    # env-seeded LLM provider management returns 403.  The frontend hides the
    # affected "Configure" controls (replacing them with a read-only "Env"
    # badge) using ``env_managed_fields``, so this guard only fires for a
    # direct API call that tries to bypass the lock.
    env_locked = _env_managed_ga_fields()
    attempted_locked = sorted(env_locked & body.model_fields_set)
    if attempted_locked:
        raise HTTPException(
            status_code=403,
            detail=(
                "The following default-model settings are configured via "
                "environment variables and are read-only: "
                f"{', '.join(attempted_locked)}. Update the corresponding "
                "DEFAULT_* environment variable(s) and restart the backend."
            ),
        )

    if writes_provider_reference:
        await _refuse_missing_ga_provider_references(db, body, config, ps)

    previous_default_model = ps.agent_default_model if body.agent_default_model is not None else None
    if body.announce_default_model:
        from app.services.model_catalog_announcements import AnnouncementRefusedError, refuse_unless_emails_allowed

        if not body.agent_default_model:
            raise HTTPException(status_code=422, detail="announce_default_model needs the new agent_default_model.")
        if body.agent_default_model == previous_default_model:
            raise HTTPException(
                status_code=422,
                detail=(
                    "The default model did not change. To email the users about the current "
                    "default, use Email users in Settings > AI and models > Models."
                ),
            )
        try:
            refuse_unless_emails_allowed()
        except AnnouncementRefusedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        # The scheduler delivers only while agentic AI runs.
        await require_agentic_ai(db)

    previous_model_defaults = _model_defaults_snapshot(ps)
    previous_embedding_vectors = (ps.embedding_model, ps.embedding_dimensions)
    previous_embedding_provider = (ps.embedding_provider, ps.embedding_provider_id)

    # ── GA-only fields ──
    if "linked_agent_id" in body.model_fields_set:
        if body.linked_agent_id:
            from app.services.general_assistant_link import stand_in_refusal

            linked_uuid = safe_uuid(body.linked_agent_id)
            agent = (
                (
                    await db.execute(select(Agent).where(Agent.id == linked_uuid))
                ).scalar_one_or_none()
                if linked_uuid is not None
                else None
            )
            if not agent:
                raise HTTPException(status_code=404, detail="Linked agent not found")
            # Only a new link is checked, like a new handover target: a save
            # sending the stored link back keeps it, and a link that stopped
            # meeting the rule is simply not followed at run time.
            refusal = (
                stand_in_refusal(agent)
                if agent.id != config.linked_agent_id
                else None
            )
            if refusal:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "The General Assistant can only be linked to an "
                        "enabled company-managed agent every user can chat "
                        f'with. "{agent.name}" {refusal}.'
                    ),
                )
            config.linked_agent_id = agent.id
        else:
            config.linked_agent_id = None
        # The admin's choice, "no agent" included: boot and the AI toggle no
        # longer link the CTEM Assistant over it.
        config.linked_agent_configured = True
    # Model writes must come from a configured provider's curated
    # ``available_models`` catalog. The runtime resolver tolerates unknown
    # models via prefix inference (so brand-new models work before a
    # catalog refresh), but PERSISTING an unlisted id is how a stale or
    # mistyped model ends up 404ing on every call.
    from app.llm.router import validate_selectable_chat_model

    for candidate_model in {body.llm_model, body.agent_default_model}:
        if candidate_model:
            model_error = await validate_selectable_chat_model(db, candidate_model)
            if model_error:
                raise HTTPException(status_code=422, detail=model_error)

    if body.llm_provider is not None:
        config.llm_provider = body.llm_provider or None
    if "llm_provider_id" in body.model_fields_set:
        config.llm_provider_id = (
            uuid.UUID(body.llm_provider_id) if body.llm_provider_id else None
        )
    if body.llm_model is not None:
        config.llm_model = body.llm_model or None
    if body.max_iterations is not None:
        config.max_iterations = clamp_max_iterations(body.max_iterations)
    if body.system_prompt is not None:
        config.system_prompt = body.system_prompt or default_system_prompt()
    if body.knowledge_base_ids is not None:
        config.knowledge_base_ids = body.knowledge_base_ids
    if body.mcp_server_ids is not None:
        config.mcp_server_ids = body.mcp_server_ids
    if body.custom_tool_ids is not None:
        config.custom_tool_ids = body.custom_tool_ids
    if body.builtin_tool_ids is not None:
        _refuse_suites_the_ga_cannot_run(body.builtin_tool_ids, config.builtin_tool_ids)
        config.builtin_tool_ids = body.builtin_tool_ids
    if "skill_ids" in body.model_fields_set:
        if body.skill_ids is not None:
            refused = await general_assistant_skills.unbindable_skills(db, body.skill_ids)
            if refused:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "The General Assistant only carries enabled company-managed skills; "
                        f"not these: {', '.join(refused)}."
                    ),
                )
            config.skill_ids = list(dict.fromkeys(str(sid) for sid in body.skill_ids))
        else:
            config.skill_ids = None
    if body.integration_bindings is not None:
        config.integration_bindings = body.integration_bindings
    added_handover_targets: list[str] = []
    if body.handover_agent_ids is not None:
        added_handover_targets = await _refuse_unreachable_ga_handover_targets(
            db, body.handover_agent_ids, config.handover_agent_ids
        )
        config.handover_agent_ids = body.handover_agent_ids
    if body.handover_types is not None:
        _refuse_handover_types_the_ga_cannot_run(
            body.handover_types, config.handover_types
        )
        config.handover_types = body.handover_types
    if added_handover_targets:
        # Recording a type next to stored targets that run every type would
        # narrow them all to delegate: the added targets join them instead.
        added = set(added_handover_targets)
        kept_handover_targets = [
            agent_id for agent_id in config.handover_agent_ids or [] if str(agent_id) not in added
        ]
        config.handover_types = with_added_handover_targets(
            kept_handover_targets, config.handover_types, added_handover_targets
        )

    if "available_channel_types" in body.model_fields_set:
        config.available_channel_types = body.available_channel_types
    if "channel_config" in body.model_fields_set:
        config.channel_config = body.channel_config
        if body.channel_config:
            non_web = [
                ch
                for ch, cfg in body.channel_config.items()
                if cfg and cfg.get("receive_enabled")
            ]
            current = config.available_channel_types or []
            has_web = "web" in current if current else True
            synced = (["web"] if has_web else []) + non_web
            config.available_channel_types = synced if synced else ["web"]

    # ── Moved to PlatformSettings ──
    if body.agent_default_provider is not None:
        ps.agent_default_provider = body.agent_default_provider or None
    if "agent_default_provider_id" in body.model_fields_set:
        ps.agent_default_provider_id = (
            uuid.UUID(body.agent_default_provider_id)
            if body.agent_default_provider_id
            else None
        )
    if body.agent_default_model is not None:
        ps.agent_default_model = body.agent_default_model or None
    if (
        hasattr(body, "default_reasoning_effort")
        and body.default_reasoning_effort is not None
    ):
        ps.default_reasoning_effort = body.default_reasoning_effort or None
    if body.embedding_provider is not None:
        ps.embedding_provider = body.embedding_provider or None
    if "embedding_provider_id" in body.model_fields_set:
        ps.embedding_provider_id = (
            uuid.UUID(body.embedding_provider_id)
            if body.embedding_provider_id
            else None
        )
    if body.embedding_model is not None:
        ps.embedding_model = body.embedding_model or None
    if "embedding_dimensions" in body.model_fields_set:
        ps.embedding_dimensions = body.embedding_dimensions
    if body.image_provider is not None:
        ps.image_provider = body.image_provider or None
    if "image_provider_id" in body.model_fields_set:
        ps.image_provider_id = (
            uuid.UUID(body.image_provider_id)
            if body.image_provider_id
            else None
        )
    if body.image_model is not None:
        # Auto-remap deprecated OpenAI image model IDs (``dall-e-3`` etc.)
        # at save time so the persisted value stays in sync with what the
        # provider will actually call. Without this, the deprecated ID is
        # silently remapped at runtime in ``OpenAIProvider.generate_image``,
        # which used to spam a WARNING on every image request — and the
        # admin UI would still show the broken value forever because nothing
        # ever rewrote it.
        from app.llm.openai_provider import OpenAIProvider

        ps.image_model = OpenAIProvider.canonical_image_model(
            body.image_model or None
        )
    if body.video_provider is not None:
        ps.video_provider = body.video_provider or None
    if "video_provider_id" in body.model_fields_set:
        ps.video_provider_id = (
            uuid.UUID(body.video_provider_id)
            if body.video_provider_id
            else None
        )
    if body.video_model is not None:
        ps.video_model = body.video_model or None
    if body.video_fallback_models is not None:
        # In order, once each; the default model itself is no fallback of its own.
        fallbacks = list(dict.fromkeys(m.strip() for m in body.video_fallback_models if m and m.strip()))
        ps.video_fallback_models = [m for m in fallbacks if m != (ps.video_model or "")] or None
    if body.speech_provider is not None:
        ps.speech_provider = body.speech_provider or None
    if "speech_provider_id" in body.model_fields_set:
        ps.speech_provider_id = (
            uuid.UUID(body.speech_provider_id)
            if body.speech_provider_id
            else None
        )
    if body.speech_model is not None:
        ps.speech_model = body.speech_model or None
    if body.speech_voice is not None:
        ps.speech_voice = body.speech_voice.strip() or None
    if body.music_provider is not None:
        ps.music_provider = body.music_provider or None
    if "music_provider_id" in body.model_fields_set:
        ps.music_provider_id = (
            uuid.UUID(body.music_provider_id)
            if body.music_provider_id
            else None
        )
    if body.music_model is not None:
        ps.music_model = body.music_model or None

    # Keep provider names and UUIDs aligned when one side is omitted.
    for provider_attr, provider_id_attr in _GA_ALIGNED_PROVIDER_PAIRS:
        provider_name = getattr(ps, provider_attr, None)
        provider_uuid = getattr(ps, provider_id_attr, None)
        if provider_uuid and not provider_name:
            provider_row = await db.get(LLMProviderConfig, provider_uuid)
            if provider_row:
                setattr(ps, provider_attr, provider_row.provider)
        elif provider_name and provider_uuid is None:
            result = await db.execute(
                select(LLMProviderConfig)
                .where(LLMProviderConfig.provider == provider_name)
                .order_by(LLMProviderConfig.created_at.desc())
                .limit(1)
            )
            provider_row = result.scalar_one_or_none()
            if provider_row:
                setattr(ps, provider_id_attr, provider_row.id)
    if body.routing_model is not None:
        # Same curated-catalog gate as ``llm_model`` / ``agent_default_model``
        # above: the routing evaluator passes this straight to
        # ``get_llm_for_user`` as a ``model_override``, so a stale id here
        # breaks every mandatory-transfer routing decision.
        if body.routing_model:
            model_error = await validate_selectable_chat_model(
                db, body.routing_model
            )
            if model_error:
                raise HTTPException(status_code=422, detail=model_error)
        ps.routing_model = body.routing_model or None
    if "routing_provider_id" in body.model_fields_set:
        ps.routing_provider_id = (
            uuid.UUID(body.routing_provider_id) if body.routing_provider_id else None
        )
    if body.decision_model is not None:
        await _write_decision_model(db, ps, body)
    if body.fallback_model is not None:
        fb_model = body.fallback_model or None
        if fb_model is not None:
            # Reject non-chat models FIRST (completion-only, image, embedding,
            # audio, ...): the owner-resolution gate below happily accepts e.g.
            # ``gpt-3.5-turbo-instruct`` (it is in OpenAI's catalog / matches
            # the ``gpt-`` prefix), but every failover attempt to such a model
            # 404s on /v1/chat/completions, so persisting it makes the entire
            # redundancy chain non-functional. Same write-path guard
            # philosophy as ``validate_selectable_chat_model`` above.
            from app.llm.router import (
                _resolve_model_owner_config,
                is_chat_capable_model,
            )

            if not is_chat_capable_model(fb_model):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Fallback model '{fb_model}' is not a chat-capable "
                        "model (completion-only, image, embedding, audio or "
                        "similar) and cannot be used as the failover target — "
                        "every failover attempt to it would fail. Pick a "
                        "chat-capable model instead."
                    ),
                )
            # Accept any model a configured provider can actually serve, using
            # the SAME resolution the runtime failover uses
            # (``_resolve_model_owner_config``: explicit ``available_models``
            # membership OR model-name prefix inference to a configured
            # provider). This lets an admin pin a fallback model such as
            # ``claude-opus-4-8`` WITHOUT adding it to the user-selectable
            # ``available_models`` list, while still rejecting a model that no
            # configured provider could serve (so we never persist a fallback
            # ``get_llm_for_user`` can't wire up).
            fb_cfgs = await db.execute(select(LLMProviderConfig))
            if (
                _resolve_model_owner_config(list(fb_cfgs.scalars().all()), fb_model)
                is None
            ):
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Fallback model '{fb_model}' does not resolve to any "
                        "configured LLM provider (it is not in any provider's "
                        "available_models and its name prefix does not match a "
                        "configured provider). Configure a provider for it first."
                    ),
                )
        ps.fallback_model = fb_model
    if "fallback_provider_id" in body.model_fields_set:
        ps.fallback_provider_id = (
            uuid.UUID(body.fallback_provider_id) if body.fallback_provider_id else None
        )
    if body.mcp_exposed_agent_ids is not None:
        ps.mcp_exposed_agent_ids = body.mcp_exposed_agent_ids
    if body.platform_mcp_enabled is not None:
        ps.platform_mcp_enabled = body.platform_mcp_enabled
    if body.xtm_mcp_enabled is not None:
        ps.xtm_mcp_enabled = body.xtm_mcp_enabled
    if body.reply_suggestions_enabled is not None:
        ps.reply_suggestions_enabled = body.reply_suggestions_enabled

    license_changed = False
    if body.enterprise_license is not None:
        from app.enterprise.licensing import (
            LicenseValidationError,
            clear_license_cache,
            validate_enterprise_license_for_install,
        )
        from app.enterprise.xtm_ca import XTM_CA_PEM

        new_pem = body.enterprise_license or None
        if new_pem:
            try:
                validate_enterprise_license_for_install(
                    new_pem,
                    ca_pem=XTM_CA_PEM,
                    platform_created_at=ps.created_at,
                )
            except LicenseValidationError as exc:
                raise HTTPException(status_code=400, detail=exc.message) from exc
        ps.enterprise_license = new_pem
        license_changed = True
        clear_license_cache("copilot")
        clear_license_cache("xtm")

    current_embedding_provider = (ps.embedding_provider, ps.embedding_provider_id)
    # A row stored without an id runs on the newest config of its type: naming
    # that config is no change.
    if (
        previous_embedding_provider == (ps.embedding_provider, None)
        and ps.embedding_provider_id is not None
        and await _legacy_embedding_config_id(db, ps.embedding_provider) == ps.embedding_provider_id
    ):
        previous_embedding_provider = current_embedding_provider
    provider_changed = current_embedding_provider != previous_embedding_provider
    # Another provider of the same model and dimensions keeps the vectors valid,
    # as the stale sweepers and provider deletion already assume. Without a
    # model, each provider embeds with its own default, so the switch resets.
    kb_reset_scheduled = (ps.embedding_model, ps.embedding_dimensions) != previous_embedding_vectors or (
        provider_changed and not ps.embedding_model
    )
    embedding_config_changed = kb_reset_scheduled or provider_changed
    provider_failures: int | None = None
    if provider_changed and not kb_reset_scheduled:
        from app.core.config import settings as app_settings

        provider_failures = await count_provider_failures(db)
        provider_failures_retried = app_settings.embedding_auto_retry_enabled
        if provider_failures_retried:
            background_tasks.add_task(retry_provider_failures_now)

    if kb_reset_scheduled:
        # The embedding-config write itself is tiny and is committed atomically
        # with the audit log before this handler returns: FastAPI runs the task
        # below before the ``get_db`` teardown, so a commit left to it would
        # hold the provider config lock for the whole reset. Only the
        # KB re-embedding reset is heavy: the bulk
        # ``DELETE FROM embeddings`` + per-document re-queue can run for minutes
        # over a large pgvector/HNSW corpus, which made the AI Models "Save"
        # hang until it finally completed (or, with
        # EMBEDDING_RESET_STATEMENT_TIMEOUT_SECS=0, indefinitely). Hand it to a
        # background task with its own session so Save returns immediately. The
        # task only runs once the response (and this request's commit) has been
        # delivered, and it operates purely on the KB embeddings/documents — it
        # never reads the config row — so it stays correct regardless of commit
        # ordering. If the task is interrupted (process restart) or fails, the
        # autonomous recovery sweeps are the safety net: recover_stuck_documents
        # re-enqueues docs left ``pending`` by a lost re-queue, and
        # recover_stale_documents re-embeds ``ready`` docs if the reset rolled
        # back before committing.
        background_tasks.add_task(
            _run_embedding_reset_background, ps.embedding_model
        )

    default_model_announced: bool | None = None
    if body.agent_default_model is not None and (
        ps.agent_default_model != previous_default_model or body.announce_default_model
    ):
        # Committed with the setting: a queued announcement of the old default
        # is dropped, and the email the admin asked for exists exactly when
        # the new default does.
        from app.services.model_catalog_announcements import (
            AnnouncementRefusedError,
            record_default_model_change,
        )

        try:
            queued = await record_default_model_change(
                db,
                previous_default=previous_default_model,
                new_default=ps.agent_default_model,
                new_provider=ps.agent_default_provider,
                announce=body.announce_default_model,
            )
        except AnnouncementRefusedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if body.announce_default_model:
            default_model_announced = queued is not None

    await db.flush()
    audit_details: dict[str, object] = {
        "llm_model": config.llm_model,
        "agent_default_model": ps.agent_default_model,
        "embedding_config_changed": embedding_config_changed,
        "kb_reset_scheduled": kb_reset_scheduled,
    }
    if default_model_announced is not None:
        audit_details["default_model_announced"] = default_model_announced
    if provider_failures is not None:
        audit_details["provider_failures"] = provider_failures
        audit_details["provider_failures_retried"] = provider_failures_retried
    # Every change of a platform model default (embedding, routing, fallback,
    # decision, ...) is recorded with its value before and after, as the
    # provider deletion records where it moved them.
    model_defaults_changed = _model_defaults_diff(previous_model_defaults, _model_defaults_snapshot(ps))
    if model_defaults_changed:
        audit_details["model_defaults_changed"] = model_defaults_changed
    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="general_assistant",
        entity_name="General Assistant",
        summary=(
            "Updated General Assistant configuration; model defaults changed: "
            + ", ".join(
                f"{field} {change['before']} -> {change['after']}" for field, change in model_defaults_changed.items()
            )
            if model_defaults_changed
            else "Updated General Assistant configuration"
        ),
        details=audit_details,
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    if license_changed:
        # The edition may have changed: drop the background gate's cached
        # licence answer, after the commit above. Invalidating first
        # would let a concurrent reader re-cache the old edition for a whole TTL.
        from app.services import agentic_gate

        agentic_gate.invalidate_cache()
    response = _build_ga_response(config, ps)
    if default_model_announced is not None:
        response.default_model_announced = default_model_announced
    return response


# ---------------------------------------------------------------------------
# User preferences
# ---------------------------------------------------------------------------


MAX_FAVORITE_AGENTS = 100
"""Ceiling on ``users.favorite_agent_ids``.

Favorites are a shortlist — the whole point is to be shorter than the agent
list — so any request past this is either a bug or someone trying to grow a
``users`` row (and every ``GET /auth/me`` payload) without bound.
"""


async def _clean_favorite_agent_ids(
    db: AsyncSession, raw: list[str] | None
) -> list[str]:
    """Normalize a favorite-agents selection: de-duplicate, then drop ids that
    no longer name an agent.

    De-duplication keeps first-seen order so a double-click cannot leave a
    second entry that the un-star would only half remove.

    The existence filter is what makes ``purge_agent_references`` stick: a
    client holding a stale user (cached before an agent was deleted) would
    otherwise write the purged id straight back on the next star click. Note it
    filters on EXISTENCE, not visibility — favorites are only an ordering hint,
    and the list page already intersects them with the agents it can actually
    see, so re-deriving visibility here would risk silently un-starring an
    agent the viewer legitimately reaches through a group share.

    Raises 422 on a non-UUID entry (nothing but agent ids belongs here, and an
    unparseable value cannot be compared against a UUID column anyway) or on a
    selection above ``MAX_FAVORITE_AGENTS``.
    """
    parsed = _parse_uuid_list(
        raw,
        cap=MAX_FAVORITE_AGENTS,
        plural="favorite agents",
        singular="favorite agent",
    )
    if not parsed:
        return []

    existing = set(
        (await db.execute(select(Agent.id).where(Agent.id.in_(parsed)))).scalars().all()
    )
    # Rebuilt from ``parsed`` so the stored strings are canonical UUID text
    # rather than whatever casing/format the client happened to send.
    return [str(aid) for aid in parsed if aid in existing]


MAX_OAUTH_SHARE_DISMISSALS = 100
"""Ceiling on ``users.oauth_share_dismissed_integration_ids``.

The list can only ever name the caller's own shared OAuth integrations, which
is a handful, so anything past this is a bug or an attempt to grow the ``users``
row (and every ``GET /auth/me`` payload) without bound — same reasoning as
``MAX_FAVORITE_AGENTS``.
"""


def _parse_uuid_list(
    raw: list[str] | None, *, cap: int, plural: str, singular: str
) -> list[uuid.UUID]:
    """De-duplicate (first-seen order) and parse a client-sent list of ids.

    Shared by the per-user id-list preferences (favorite agents, closed
    OAuth-share reminders). Raises 422 on more than ``cap`` entries or on a
    value that is not a UUID — nothing but entity ids belongs in these lists,
    and an unparseable value cannot be compared against a UUID column anyway.
    """
    ids = raw or []
    if len(ids) > cap:
        raise HTTPException(
            status_code=422,
            detail=f"Too many {plural} (max {cap}, got {len(ids)})",
        )
    parsed: list[uuid.UUID] = []
    for value in dict.fromkeys(str(x) for x in ids):
        try:
            parsed.append(uuid.UUID(value))
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(
                status_code=422,
                detail=f"Invalid {singular} id: {value!r}",
            ) from None
    return parsed


async def _clean_oauth_share_dismissed_ids(
    db: AsyncSession, user: User, raw: list[str] | None
) -> list[str]:
    """Normalize the closed "personal OAuth token is shared" reminders.

    Keeps only ids naming an OAuth integration this user OWNS whose token is
    currently acknowledged as shared (``oauth_share_acknowledged``) — the exact
    condition under which the dashboard shows the reminder, so the stored list
    is always "reminders currently muted" and nothing else can be muted: not
    another user's integration (the reminder is owner-only, a client cannot
    close someone else's), not a deleted or unshared one (inert for display,
    it would only grow the row), not a non-OAuth row (never reminded about).

    Filtering on ``oauth_share_acknowledged`` rather than on the sharing fields
    is deliberate: ``integration_credentials.apply_oauth_share_consent`` resets
    that flag the moment an integration stops being shared (or its owner opts
    out), so it is the one column that says "this personal token is shared
    right now" in a single read.

    Raises 422 on a non-UUID entry or on a selection above
    ``MAX_OAUTH_SHARE_DISMISSALS``.
    """
    parsed = _parse_uuid_list(
        raw,
        cap=MAX_OAUTH_SHARE_DISMISSALS,
        plural="dismissed OAuth-share reminders",
        singular="integration",
    )
    if not parsed:
        return []
    shared = set(
        (
            await db.execute(
                select(Integration.id).where(
                    Integration.id.in_(parsed),
                    Integration.user_id == user.id,
                    Integration.auth_type == "oauth",
                    Integration.oauth_share_acknowledged.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    # Rebuilt from ``parsed`` so the stored strings are canonical UUID text.
    return [str(iid) for iid in parsed if iid in shared]


async def _checked_ga_integration_bindings(
    db: AsyncSession, user: User, requested: dict[str, list[str]] | None
) -> dict[str, list[str]] | None:
    """The General Assistant integration bindings a user may save.

    The assistant runs every bound integration with its stored credentials, so
    an id the request ADDS must name an integration the user can see (the
    three-tier rule, no admin bypass: the picker lists the same set for an
    admin). Ids already saved, and the platform's own General Assistant
    bindings (the picker starts from them when a user first customises the
    list), are not re-checked: a turn uses the saved list as saved, even one
    the user can no longer see (nothing is withheld at run time). A missing and
    an invisible integration are refused alike.

    Raises 422 on a malformed id the request adds, 403 on an id the user may
    not bind. A malformed value already saved (the map was stored as sent
    by an older version) never blocks a save - the Profile page re-sends the whole
    map on every toggle - and is dropped. Returns the bindings with canonical
    UUID text.
    """
    if requested is None:
        return None
    from app.models.general_assistant_config import GeneralAssistantConfig
    from app.services.integration_credentials import (
        flatten_binding_ids,
        visible_integration_ids,
    )

    stored = user.ga_integration_bindings if isinstance(user.ga_integration_bindings, dict) else {}
    stored_values = [
        item
        for ids in stored.values()
        for item in (ids if isinstance(ids, (list, tuple)) else [ids])
    ]
    requested_ids = flatten_binding_ids(requested)
    malformed = [
        raw
        for ids in requested.values()
        for raw in ids
        if (not isinstance(raw, str) or not raw or safe_uuid(raw) is None)
        and raw not in stored_values
    ]
    if malformed:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid integration binding: {len(malformed)} value(s) are not integration ids",
        )

    ga_config = (
        await db.execute(select(GeneralAssistantConfig).limit(1))
    ).scalar_one_or_none()
    known = {
        str(parsed)
        for raw in (
            flatten_binding_ids(user.ga_integration_bindings)
            + flatten_binding_ids(ga_config.integration_bindings if ga_config else None)
        )
        for parsed in [safe_uuid(raw)]
        if parsed is not None
    }
    added = list(
        dict.fromkeys(
            str(parsed)
            for raw in requested_ids
            for parsed in [safe_uuid(raw)]
            if parsed is not None and str(parsed) not in known
        )
    )
    if added:
        visible = await visible_integration_ids(db, user.id, added)
        refused = [iid for iid in added if iid not in visible]
        if refused:
            raise HTTPException(
                status_code=403,
                detail=(
                    "You cannot bind these integrations to your assistant: "
                    f"{', '.join(refused[:10])}. Pick integrations you own or "
                    "that are shared with you."
                ),
            )
    return {
        provider: [str(parsed) for raw in ids for parsed in [safe_uuid(raw)] if parsed is not None]
        for provider, ids in requested.items()
    }


async def _refuse_profile_tool_additions(
    db: AsyncSession,
    user: User,
    *,
    model: type,
    label: str,
    requested: list[str] | None,
    known: list,
) -> None:
    """Refuse (403) a save that ADDS to a profile tool list (the agent chat
    defaults, the General Assistant's MCP servers, knowledge bases and custom
    tools) an id the user could not bind to an agent of their own.

    A chat turn loads what the profile names with no check of its own - a
    request echoing it is not refused either (``chat_selection``) - so the
    check is here, with the rule of an agent's own bindings
    (``binding_access.refused_additions``: three-tier, admins bypass). Ids
    already saved, and the platform's own General Assistant lists a user
    starts from, are not re-checked, so a list holding a row unshared since
    keeps saving. A missing and an invisible row are refused alike.
    """
    if not requested:
        return
    from app.api.v1.binding_access import refused_additions

    refused = await refused_additions(
        db, user=user, model=model, value=list(requested), baseline=known
    )
    if refused:
        raise HTTPException(
            status_code=403,
            detail=(
                f"You cannot add these {label} to your profile: "
                f"{', '.join(str(entry) for entry in refused[:10])}. Pick ones you "
                "own or that are shared with you."
            ),
        )


async def _checked_ga_browser_profile_ids(db: AsyncSession, user: User, requested: list[str] | None) -> list[str] | None:
    """The browser profiles a person gives their General Assistant, as stored.

    An id the save ADDS must be a profile the person may use (three-tier,
    without the admin bypass: the rule of every use of a profile); ids
    already saved are not re-checked.
    """
    from app.api.v1.binding_access import refused_additions
    from app.models.browser_profile import BrowserProfile
    from app.services.browser_profiles.agent_access import normalize_profile_ids

    try:
        ids = normalize_profile_ids(requested)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if not ids:
        return None
    refused = await refused_additions(
        db, user=user, model=BrowserProfile, value=ids, baseline=user.ga_browser_profile_ids, is_admin=False
    )
    if refused:
        raise HTTPException(
            status_code=403,
            detail=(
                "You cannot give your assistant these browser profiles: "
                f"{', '.join(str(entry) for entry in refused[:10])}. Pick ones you own or that are shared with you."
            ),
        )
    return ids


async def _visible_fleet_ids(db: AsyncSession, user: User, ids: list[str]) -> list[str]:
    """The ids of *ids* that name a fleet *user* may read, in the order given (#11464)."""
    import uuid as _uuid

    from app.models.fleet import Fleet

    parsed: dict[str, _uuid.UUID] = {}
    for raw in ids:
        try:
            parsed[raw] = _uuid.UUID(raw)
        except ValueError:
            continue
    if not parsed:
        return []
    group_ids = await get_user_group_ids(db, user.id)
    found = {
        str(fid)
        for (fid,) in (
            await db.execute(
                select(Fleet.id).where(
                    Fleet.id.in_(list(parsed.values())),
                    user_visible_filter(Fleet, user.id, group_ids, is_admin=user.is_admin),
                )
            )
        ).all()
    }
    return [raw for raw, fid in parsed.items() if str(fid) in found]


@router.put("/preferences")
async def update_preferences(
    body: UserPreferencesRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    new_email = body.email.strip().lower() if body.email is not None else None
    if new_email is not None and new_email != user.email:
        # The platform admin is the ADMIN_EMAIL account: another address would
        # hand that standing to whoever registers it.
        if is_platform_admin(user):
            raise HTTPException(
                status_code=400,
                detail="The platform admin account's email is set by ADMIN_EMAIL and cannot be changed",
            )
        # An address exempt from the Terms of Use gate is not self-service:
        # taking it would skip the gate for this account.
        from app.core.config import settings as app_settings

        if app_settings.terms_of_use_exempt(new_email):
            raise HTTPException(status_code=403, detail="This email address cannot be used")
        # Check email isn't taken by another user (any case: one account per
        # address, ``ix_users_email_lower``)
        existing = await db.execute(
            select(User).where(func.lower(User.email) == new_email, User.id != user.id)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Email already in use")
        user.email = new_email
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.title is not None:
        user.title = body.title
    if "timezone" in body.model_fields_set:
        user.timezone = body.timezone
    if body.theme is not None:
        user.theme = body.theme if body.theme in ("dark", "light") else None
    if body.language is not None:
        # Same contract as OpenCTI: an unsupported language is a client error,
        # never silently coerced.
        lang = normalize_language(body.language)
        if lang is None:
            raise HTTPException(
                status_code=400,
                detail="The language you have provided is not valid",
            )
        user.language = lang
    if body.ctem_vertical is not None:
        user.ctem_vertical = normalize_vertical(body.ctem_vertical)
    if body.ctem_splash_seen is not None:
        user.ctem_splash_seen = body.ctem_splash_seen
    # What the person saw when they closed the fleets invitation (#9566),
    # written whole; an explicit null shows the card again.
    if "ctem_fleet_invite_dismissed" in body.model_fields_set:
        seen = body.ctem_fleet_invite_dismissed
        user.ctem_fleet_invite_dismissed = (
            {"fleets": list(dict.fromkeys(seen.fleets)), "modules": list(dict.fromkeys(seen.modules))}
            if seen is not None
            else None
        )
    # The organization context fields missing when the person closed the
    # command center's incomplete-context notice (#11586), written whole and
    # deduplicated; an explicit null shows the notice again.
    if "organization_context_notice_dismissed" in body.model_fields_set:
        closed = body.organization_context_notice_dismissed
        user.organization_context_notice_dismissed = (
            list(dict.fromkeys(closed)) if closed is not None else None
        )
    # The fleets the command center shows (#11464), written whole, in order,
    # deduplicated; an explicit null restores the built-in CTEM fleets. Only
    # ids the person may see are kept: the list never leaks another's fleet.
    if "ctem_dashboard_fleet_ids" in body.model_fields_set:
        chosen = body.ctem_dashboard_fleet_ids
        user.ctem_dashboard_fleet_ids = (
            await _visible_fleet_ids(db, user, list(dict.fromkeys(chosen)))
            if chosen is not None
            else None
        )
    # The Copilot home's own choice (#12063), the same rules; null = every fleet.
    if "copilot_dashboard_fleet_ids" in body.model_fields_set:
        chosen = body.copilot_dashboard_fleet_ids
        user.copilot_dashboard_fleet_ids = (
            await _visible_fleet_ids(db, user, list(dict.fromkeys(chosen)))
            if chosen is not None
            else None
        )
    # One vertical's command center at a time, validated by the request model;
    # the other verticals' layouts are kept. They are re-read under a row lock,
    # not taken from the user loaded with the request: two writes for two
    # verticals (two tabs) would otherwise each start from the same copy and
    # the later one drop the other's layout. A new dict, not an in-place edit:
    # a plain JSON column only sees a reassignment.
    if body.ctem_dashboard_layout is not None:
        layout_update = body.ctem_dashboard_layout
        stored = (
            await db.execute(
                select(User.ctem_dashboard_layouts)
                .where(User.id == user.id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        layouts = dict(stored or {})
        if layout_update.layout is None:
            layouts.pop(layout_update.vertical, None)
        else:
            # The optional context and widget settings are stored only when
            # set, so a layout saved without them is stored as it always was.
            layouts[layout_update.vertical] = layout_update.layout.model_dump(exclude_none=True)
        user.ctem_dashboard_layouts = layouts or None
    # The Copilot dashboard is one layout, written whole, validated by the
    # request model; an explicit null resets the default one.
    if "copilot_dashboard_layout" in body.model_fields_set:
        user.copilot_dashboard_layout = (
            body.copilot_dashboard_layout.model_dump()
            if body.copilot_dashboard_layout is not None
            else None
        )
    # The profile's tool lists: an id a save ADDS must be one the user could
    # bind to an agent of their own.
    _profile_tool_lists = (
        ("default_knowledge_base_ids", KnowledgeBase, "knowledge bases", None),
        ("default_mcp_server_ids", MCPServer, "MCP servers", None),
        ("ga_custom_tool_ids", CustomTool, "custom tools", "custom_tool_ids"),
        ("ga_mcp_server_ids", MCPServer, "MCP servers", "mcp_server_ids"),
        ("ga_knowledge_base_ids", KnowledgeBase, "knowledge bases", "knowledge_base_ids"),
    )
    _sent_lists = {
        field
        for field, *_rest in _profile_tool_lists
        if field in body.model_fields_set and getattr(body, field)
    }
    _ga_defaults = None
    if any(field.startswith("ga_") for field in _sent_lists):
        from app.models.general_assistant_config import GeneralAssistantConfig

        _ga_defaults = (
            await db.execute(select(GeneralAssistantConfig).limit(1))
        ).scalar_one_or_none()
    for _field, _model, _label, _ga_field in _profile_tool_lists:
        if _field in _sent_lists:
            await _refuse_profile_tool_additions(
                db,
                user,
                model=_model,
                label=_label,
                requested=getattr(body, _field),
                known=[
                    *(getattr(user, _field) or []),
                    *((getattr(_ga_defaults, _ga_field, None) or []) if _ga_field else []),
                ],
            )
    if body.default_knowledge_base_ids is not None:
        user.default_knowledge_base_ids = body.default_knowledge_base_ids
    if body.default_mcp_server_ids is not None:
        user.default_mcp_server_ids = body.default_mcp_server_ids
    if body.default_agent_id is not None:
        chosen_agent = uuid.UUID(body.default_agent_id) if body.default_agent_id else None
        if chosen_agent != user.default_agent_id or user.default_agent_source != "user":
            # The person's own choice, recorded as theirs (an empty value is
            # the General Assistant), so the reader never takes the platform's
            # wiring for a choice (``platform_insights.default_agents``).
            user.default_agent_id = chosen_agent
            user.default_agent_source = "user"
            user.default_agent_set_at = datetime.now(UTC)
    # The dashboard shown on /dashboard: a custom dashboard the caller may read
    # as themselves - their own, one shared with them, a company one - checked
    # on the row (an admin's reading of everyone's does not count: never
    # someone else's private dashboard); null brings back the platform's.
    if "home_dashboard_id" in body.model_fields_set:
        if body.home_dashboard_id is None:
            user.home_dashboard_id = None
        else:
            from app.core.feature_flags import is_feature_enabled
            from app.services.custom_dashboards import load_dashboard_for

            # Off, no dashboard is offered: none can become the home page.
            found = (
                await load_dashboard_for(db, body.home_dashboard_id, user_id=user.id)
                if is_feature_enabled("DASHBOARD_STUDIO")
                else None
            )
            if found is None:
                raise HTTPException(status_code=404, detail="Dashboard not found")
            user.home_dashboard_id = found[0].id
    # Per-user General Assistant tool overrides.
    # When the key is present in the request, update the column.
    # The frontend sends the value (or null to reset to platform defaults).
    if "ga_builtin_tool_ids" in body.model_fields_set:
        user.ga_builtin_tool_ids = body.ga_builtin_tool_ids
    if "ga_custom_tool_ids" in body.model_fields_set:
        user.ga_custom_tool_ids = body.ga_custom_tool_ids
    if "ga_integration_bindings" in body.model_fields_set:
        user.ga_integration_bindings = await _checked_ga_integration_bindings(
            db, user, body.ga_integration_bindings
        )
    if "ga_mcp_server_ids" in body.model_fields_set:
        user.ga_mcp_server_ids = body.ga_mcp_server_ids
    if "ga_knowledge_base_ids" in body.model_fields_set:
        user.ga_knowledge_base_ids = body.ga_knowledge_base_ids
    if "ga_browser_profile_ids" in body.model_fields_set:
        user.ga_browser_profile_ids = await _checked_ga_browser_profile_ids(db, user, body.ga_browser_profile_ids)
    if "channel_agent_config" in body.model_fields_set:
        user.channel_agent_config = body.channel_agent_config
    if "mcp_exposed_agent_ids" in body.model_fields_set:
        user.mcp_exposed_agent_ids = body.mcp_exposed_agent_ids
    if "favorite_agent_ids" in body.model_fields_set:
        user.favorite_agent_ids = await _clean_favorite_agent_ids(
            db, body.favorite_agent_ids
        )
    if body.notify_waiting_work_items is not None:
        user.notify_waiting_work_items = body.notify_waiting_work_items
    if body.reply_suggestions_enabled is not None:
        user.reply_suggestions_enabled = body.reply_suggestions_enabled
    if "chat_sidebar_expanded_workspaces" in body.model_fields_set:
        # Written whole, duplicates dropped in first-seen order; a key naming a
        # workspace since deleted is harmless (no group draws it).
        keys = body.chat_sidebar_expanded_workspaces
        user.chat_sidebar_expanded_workspaces = (
            list(dict.fromkeys(keys)) if keys is not None else None
        )
    if "oauth_share_dismissed_integration_ids" in body.model_fields_set:
        user.oauth_share_dismissed_integration_ids = (
            await _clean_oauth_share_dismissed_ids(
                db, user, body.oauth_share_dismissed_integration_ids
            )
        )
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="preferences",
        entity_name="User Preferences",
        summary="Updated user preferences",
        details={
            "preferred_llm_model": user.preferred_llm_model,
            **(
                {"home_dashboard_id": str(user.home_dashboard_id) if user.home_dashboard_id else None}
                if "home_dashboard_id" in body.model_fields_set
                else {}
            ),
        },
        request=request,
    )
    # Before the response: the ``get_db`` teardown commits after it is sent, and
    # the profile the client refetches next would read the previous values.
    await db.commit()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# OAuth defaults – admin pre-fills client_id/secret for OAuth providers
# ---------------------------------------------------------------------------


def _stored_oauth_default_extra(d: OAuthDefault | None) -> dict:
    """The decrypted extra config of an OAuth default (``{}`` when none or unreadable)."""
    if d is None or not d.extra_config_enc:
        return {}
    try:
        stored = json.loads(decrypt_value(d.extra_config_enc))
    except Exception:
        return {}
    return stored if isinstance(stored, dict) else {}


def _stored_oauth_default_secret_keys(d: OAuthDefault) -> list[str]:
    """The secret extra-config keys an OAuth default holds a value for (names only)."""
    from app.api.v1.integrations import _is_sensitive_setting_name

    return sorted(
        key
        for key, value in _stored_oauth_default_extra(d).items()
        if value not in (None, "") and _is_sensitive_setting_name(key)
    )


def _merge_oauth_default_extra(existing: OAuthDefault | None, submitted: dict | None) -> dict:
    """The extra config to store: a secret sent back as the mask keeps its stored value.

    The rule of an integration update (``EXTRA_CONFIG_MASK``): the form cannot
    read a stored secret, so it sends the mask for one left untouched. A mask
    with nothing stored behind it is dropped, never stored as the secret.
    """
    from app.api.v1.integrations import EXTRA_CONFIG_MASK, _is_sensitive_setting_name

    stored = _stored_oauth_default_extra(existing)
    merged: dict = {}
    for key, value in (submitted or {}).items():
        if value == EXTRA_CONFIG_MASK and _is_sensitive_setting_name(key):
            if key in stored:
                merged[key] = stored[key]
            continue
        merged[key] = value
    return merged


def _oauth_default_response(d: OAuthDefault) -> OAuthDefaultResponse:
    return OAuthDefaultResponse(
        id=str(d.id),
        provider=d.provider,
        has_client_id=bool(d.client_id_enc),
        has_client_secret=bool(d.client_secret_enc),
        has_extra_config=bool(d.extra_config_enc),
        stored_secret_keys=_stored_oauth_default_secret_keys(d),
        created_at=d.created_at.isoformat(),
        updated_at=d.updated_at.isoformat(),
    )


@router.get("/oauth-defaults", response_model=list[OAuthDefaultResponse])
async def list_oauth_defaults(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List all OAuth defaults. Any authenticated user can see which providers have defaults."""
    result = await db.execute(select(OAuthDefault))
    return [_oauth_default_response(d) for d in result.scalars().all()]


@router.post(
    "/oauth-defaults",
    response_model=OAuthDefaultResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upsert_oauth_default(
    body: OAuthDefaultRequest,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create or update admin OAuth defaults for a provider (admin only).

    The client secret may be a vault reference, which the caller must be
    allowed to store; the signature gates and the OAuth flows resolve it.
    """
    from app.api.v1.schemas.vault_references import require_vault_writer

    await require_vault_writer(db, user, client_secret=body.client_secret)
    result = await db.execute(
        select(OAuthDefault).where(OAuthDefault.provider == body.provider)
    )
    existing = result.scalar_one_or_none()
    extra_config = _merge_oauth_default_extra(existing, body.extra_config)
    extra_config_enc = encrypt_value(json.dumps(extra_config)) if extra_config else None

    if existing:
        existing.client_id_enc = encrypt_value(body.client_id)
        existing.client_secret_enc = encrypt_value(body.client_secret)
        existing.extra_config_enc = extra_config_enc
        d = existing
    else:
        d = OAuthDefault(
            provider=body.provider,
            client_id_enc=encrypt_value(body.client_id),
            client_secret_enc=encrypt_value(body.client_secret),
            extra_config_enc=extra_config_enc,
        )
        db.add(d)

    await db.flush()
    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="oauth_default",
        entity_id=d.id,
        entity_name=body.provider,
        summary=f"Configured OAuth defaults for '{body.provider}'",
        details={
            "provider": body.provider,
        },
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return _oauth_default_response(d)


@router.delete("/oauth-defaults/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_oauth_default(
    provider: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Remove admin OAuth defaults for a provider (admin only)."""
    result = await db.execute(
        select(OAuthDefault).where(OAuthDefault.provider == provider)
    )
    d = result.scalar_one_or_none()
    if not d:
        raise HTTPException(
            status_code=404, detail="No OAuth default found for this provider"
        )
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="oauth_default",
        entity_id=d.id,
        entity_name=provider,
        summary=f"Deleted OAuth defaults for '{provider}'",
        details={
            "provider": provider,
        },
        request=request,
    )
    await db.delete(d)
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()


# ---------------------------------------------------------------------------
# Disabled Integration Providers (admin-only catalog control)
# ---------------------------------------------------------------------------


@router.get("/disabled-providers")
async def list_disabled_providers(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[str]:
    """List providers that are globally disabled from the integration catalog (admin only)."""
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )

    ps = await _load_ps(db)
    if ps.disabled_integration_providers is None:
        logger.error(
            "PlatformSettings.disabled_integration_providers is None — "
            "this field should always be initialised (at least to an empty list)."
        )
        raise HTTPException(
            status_code=500,
            detail="Platform settings are in an inconsistent state: disabled_integration_providers is not initialised.",
        )
    return cast(list[str], ps.disabled_integration_providers)


class VaultProvidersResponse(BaseModel):
    """The vault providers a secret field may reference: names only, never their settings."""

    providers: list[str]
    cache_seconds: int


@router.get("/vault-providers", response_model=VaultProvidersResponse)
async def list_vault_providers(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> VaultProvidersResponse:
    """The providers the caller may name in a ``vault://<provider>/<path>#<key>`` reference, for the field hints."""
    from app.core.config import settings as app_settings
    from app.services.vault.resolver import get_resolver
    from app.services.vault.store import vault_principal

    resolver = get_resolver()
    principal = await vault_principal(db, user)
    return VaultProvidersResponse(
        providers=sorted(name for name in resolver.known_names() if resolver.policy(name).usable_by(principal)),
        cache_seconds=app_settings.secret_vault_cache_seconds,
    )


async def _purge_integrations_of_providers(
    db: AsyncSession, user: User, providers: set[str]
) -> int:
    """Delete every integration on ``providers`` and unbind it from agents.

    What disabling a provider costs, in one place: the whole-list PUT and the
    per-provider endpoint below must destroy exactly the same thing, or an
    admin's blast radius would depend on which button they pressed.
    """
    if not providers:
        return 0

    integrations_result = await db.execute(
        select(Integration).where(Integration.provider.in_(list(providers)))
    )
    integrations_to_delete = integrations_result.scalars().all()

    if integrations_to_delete:
        integration_ids_to_remove = {str(i.id) for i in integrations_to_delete}

        # Pre-filter to agents that reference at least one removed
        # integration id instead of scanning the whole agent table.
        agents_result = await db.execute(
            select(Agent).where(
                or_(
                    *[
                        sa_cast(Agent.integration_bindings, SAText).contains(iid)
                        for iid in integration_ids_to_remove
                    ]
                )
            )
        )
        for agent in agents_result.scalars().all():
            bindings = agent.integration_bindings or {}
            if any(v in integration_ids_to_remove for v in bindings.values()):
                agent.integration_bindings = {
                    k: v
                    for k, v in bindings.items()
                    if v not in integration_ids_to_remove
                }

        for integration in integrations_to_delete:
            await db.delete(integration)

    logger.info(
        "Admin %s disabled provider(s) %s — deleted %d integration(s)",
        user.email,
        providers,
        len(integrations_to_delete),
    )
    return len(integrations_to_delete)


async def _lock_platform_settings(db: AsyncSession) -> PlatformSettings:
    """Load the settings singleton with its row locked for update.

    The disabled-provider list is a single JSON array, so two concurrent
    read-modify-write requests would otherwise both compute from the same
    pre-image and the later write would drop the earlier one's provider —
    silently RE-ENABLING it. Taking the row lock makes the per-provider
    endpoints below serialise at the database instead.
    """
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )
    from app.models.platform_settings import PlatformSettings as _PlatformSettings

    # Ensure the singleton exists before asking to lock it.
    await _load_ps(db)
    result = await db.execute(
        select(_PlatformSettings)
        .order_by(_PlatformSettings.created_at.asc(), _PlatformSettings.id.asc())
        .limit(1)
        .with_for_update()
    )
    return result.scalar_one()


@router.put("/disabled-providers/{provider}")
async def disable_provider(
    provider: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[str]:
    """Disable ONE provider, leaving every other entry alone (admin only).

    The delta counterpart of the whole-list PUT: two admins disabling two
    different providers at the same time both get what they asked for, where
    sending the full array would have the second silently re-enable the first.

    Deleting the provider's integrations and cleaning agent bindings is the
    same operation the list endpoint performs.
    """
    ps = await _lock_platform_settings(db)
    current = set(ps.disabled_integration_providers or [])
    if provider not in current:
        await _purge_integrations_of_providers(db, user, {provider})
        ps.disabled_integration_providers = sorted(current | {provider})
        await db.flush()
        await log_audit(
            db,
            user=user,
            action="configure",
            entity_type="integration_catalog",
            entity_name="Integration Catalog",
            summary=f"Disabled integration provider: {provider}",
            request=request,
        )
    # Commit before answering (and release the row lock): the get_db teardown
    # only commits after the response is sent.
    await db.commit()
    return cast(list[str], ps.disabled_integration_providers)


@router.delete("/disabled-providers/{provider}")
async def enable_provider(
    provider: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[str]:
    """Re-enable ONE provider, leaving every other entry alone (admin only).

    Nothing is restored: disabling deleted the integrations, and this only
    puts the provider back in the catalog.
    """
    ps = await _lock_platform_settings(db)
    current = set(ps.disabled_integration_providers or [])
    if provider in current:
        ps.disabled_integration_providers = sorted(current - {provider})
        await db.flush()
        await log_audit(
            db,
            user=user,
            action="configure",
            entity_type="integration_catalog",
            entity_name="Integration Catalog",
            summary=f"Enabled integration provider: {provider}",
            request=request,
        )
    # Commit before answering (and release the row lock): the get_db teardown
    # only commits after the response is sent.
    await db.commit()
    return cast(list[str], ps.disabled_integration_providers)


@router.put("/disabled-providers")
async def update_disabled_providers(
    body: dict,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> list[str]:
    """Set the list of globally disabled integration providers (admin only).

    Body: { "providers": ["provider_key_1", "provider_key_2", ...] }

    When a provider is newly disabled, all existing integrations of that provider
    are deleted and their bindings are cleaned from agents.
    """
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )

    providers = body.get("providers", [])
    if not isinstance(providers, list):
        raise HTTPException(
            status_code=400, detail="providers must be a list of strings"
        )

    ps = await _load_ps(db)

    old_disabled = set(ps.disabled_integration_providers or [])
    new_disabled = set(providers)
    newly_disabled = new_disabled - old_disabled

    await _purge_integrations_of_providers(db, user, newly_disabled)

    ps.disabled_integration_providers = sorted(new_disabled)
    await db.flush()

    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="integration_catalog",
        entity_name="Integration Catalog",
        summary=f"Updated disabled integration providers: {sorted(new_disabled) or 'none'}",
        request=request,
    )

    if ps.disabled_integration_providers is None:
        logger.error(
            "PlatformSettings.disabled_integration_providers is None after update — "
            "this should never happen after an explicit assignment."
        )
        raise HTTPException(
            status_code=500,
            detail="Platform settings are in an inconsistent state: disabled_integration_providers is not initialised after update.",
        )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return cast(list[str], ps.disabled_integration_providers)


# ---------------------------------------------------------------------------
# SSO Configuration (admin-only)
# ---------------------------------------------------------------------------


@router.get("/sso", response_model=SSOConfigResponse)
async def get_sso_config(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get the SSO configuration (admin only)."""
    from app.core.config import settings as app_settings

    result = await db.execute(select(SSOConfig).limit(1))
    row = result.scalar_one_or_none()

    base_url = app_settings.base_url

    if row:
        return _build_sso_response(row, base_url)

    # No DB config yet (fresh install) — return sensible defaults
    return SSOConfigResponse(
        saml_entity_id=f"{base_url}/api/auth/saml/metadata",
        saml_sp_entity_id=f"{base_url}/api/auth/saml/metadata",
        saml_acs_url=f"{base_url}/api/auth/saml/acs",
        saml_metadata_url=f"{base_url}/api/auth/saml/metadata",
    )


@router.put("/sso", response_model=SSOConfigResponse)
async def update_sso_config(
    body: SSOConfigRequest,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create or update the SSO configuration (admin only)."""
    from app.core.config import settings as app_settings

    result = await db.execute(select(SSOConfig).limit(1))
    row = result.scalar_one_or_none()

    if not row:
        row = SSOConfig()
        db.add(row)

    row.oidc_enabled = body.oidc_enabled
    row.oidc_display_name = body.oidc_display_name or "Microsoft"
    row.oidc_discovery_url = body.oidc_discovery_url
    row.oidc_authorization_url = body.oidc_authorization_url
    row.oidc_token_url = body.oidc_token_url
    row.oidc_userinfo_url = body.oidc_userinfo_url
    row.oidc_scopes = body.oidc_scopes or "openid email profile"
    row.saml_enabled = body.saml_enabled
    row.saml_display_name = body.saml_display_name or "Corporate SSO"
    row.saml_idp_metadata_url = body.saml_idp_metadata_url
    row.saml_sp_entity_id = body.saml_sp_entity_id
    row.saml_requested_authn_context = body.saml_requested_authn_context
    row.saml_groups_mapping_enabled = body.saml_groups_mapping_enabled
    row.saml_groups_auto_create = body.saml_groups_auto_create
    row.oidc_auto_admin_new_users = body.oidc_auto_admin_new_users
    row.saml_auto_admin_new_users = body.saml_auto_admin_new_users
    row.oidc_auto_create_users = body.oidc_auto_create_users
    row.saml_auto_create_users = body.saml_auto_create_users
    row.local_auth_enabled = body.local_auth_enabled
    row.registration_enabled = body.registration_enabled
    row.allowed_domains = body.allowed_domains

    # Only update secrets if non-empty (don't wipe existing)
    if body.oidc_client_id:
        row.oidc_client_id_enc = encrypt_value(body.oidc_client_id)
    if body.oidc_client_secret:
        row.oidc_client_secret_enc = encrypt_value(body.oidc_client_secret)

    await db.flush()
    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="sso",
        entity_name="SSO",
        summary="Updated SSO configuration",
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    clear_oidc_discovery_cache()

    base_url = app_settings.base_url
    return _build_sso_response(row, base_url)


@router.post("/sso/test-oidc", response_model=SSOOIDCTestResponse)
async def test_sso_oidc(
    body: SSOOIDCTestRequest,
    user: User = Depends(require_admin),
):
    """Fetch the Discovery URL as sign-in would and say what is wrong (admin only).

    Takes the URL from the request, not the saved row, so it can be tried
    before saving. Saving never depends on it: the provider may be down then.
    """
    url = body.oidc_discovery_url.strip()
    if not url:
        raise HTTPException(status_code=422, detail="Discovery URL is required")
    error = await check_oidc_discovery(url)
    if error is None:
        return SSOOIDCTestResponse(
            ok=True,
            status="ok",
            detail="The authorization and token endpoints were found.",
        )
    return SSOOIDCTestResponse(ok=False, status=error.marker, detail=error.reason)


# ---------------------------------------------------------------------------
# Observability Settings (admin-writable, auth-readable)
# ---------------------------------------------------------------------------


class ObservabilityConfigResponse(BaseModel):
    trace_store_provider: str
    trace_store_health_status: str
    trace_store_health_message: str
    trace_storage_enabled: bool
    trace_retention_days: int
    audit_log_retention_days: int = 90
    trace_store_payloads: bool
    last_aggregation_at: datetime | None = None
    aggregation_stale: bool
    pricing_last_updated_at: datetime | None = None
    pricing_stale: bool
    pricing_staleness_days: int | None = None
    models_without_pricing: list[str] = Field(default_factory=list)


class ObservabilityConfigUpdate(BaseModel):
    trace_storage_enabled: bool
    trace_retention_days: int = Field(ge=1, le=365)
    trace_store_payloads: bool
    #: Days an audit log entry is kept (services/audit_retention.py); unchanged when absent.
    audit_log_retention_days: int | None = Field(default=None, ge=30, le=3650)


class WorkspaceKnowledgeConfig(BaseModel):
    """What a workspace gives its agents, platform-wide."""

    #: Share of the turn model's context window a workspace's sources may fill
    #: inline before they are searched instead.
    sources_context_percent: int = Field(ge=5, le=40)
    #: Minutes a filed conversation stays idle before it is indexed.
    conversation_idle_minutes: int = Field(ge=5, le=1440)


class BrandAssetSourceConfig(BaseModel):
    """Where the asset seed reads more brand packs: a GitHub folder and its integration."""

    #: ``https://github.com/<owner>/<repo>[/tree/<ref>[/<path>]]``; empty clears the source.
    url: str | None = Field(default=None, max_length=2048)
    #: The GitHub integration whose token reads the folder.
    integration_id: str | None = None


class BrandAssetSourceSyncResult(BaseModel):
    """What one download of the source read, and what the seed then uploaded.

    *shadowed*: packs of the source the platform also ships under the same
    name, so the bundled ones are used and these are not.
    """

    packs: list[str]
    shadowed: list[str] = []
    error: str | None = None
    assets_uploaded: int = 0


class BrandAssetSourceResponse(BrandAssetSourceConfig):
    integration_name: str | None = None
    #: Set by a save or a sync, which run the download and the asset seed.
    sync: BrandAssetSourceSyncResult | None = None


async def _resolve_trace_store_health(trace_store_provider: str) -> tuple[str, str]:
    """Return trace-store health status and a short human-readable message."""

    provider = (trace_store_provider or "postgres").strip().lower()
    if provider != "clickhouse":
        return "healthy", "PostgreSQL trace store is active."

    from app.services.tracing.clickhouse_client import (
        HTTPClickHouseClient,
        has_clickhouse_runtime_config,
    )

    if not has_clickhouse_runtime_config():
        return "unhealthy", "ClickHouse runtime credentials are incomplete."

    client: HTTPClickHouseClient | None = None
    try:
        client = HTTPClickHouseClient.from_settings()
        rows = await client.query_json_rows("SELECT 1 AS ok")
        if rows and str(rows[0].get("ok")) == "1":
            return "healthy", "ClickHouse connection is healthy."
        return "unhealthy", "ClickHouse healthcheck returned an unexpected result."
    except Exception as exc:
        details = str(exc).strip().replace("\n", " ")
        if details:
            details = details[:180]
            return "unhealthy", f"ClickHouse connection failed: {details}"
        return "unhealthy", "ClickHouse connection failed."
    finally:
        if client is not None:
            with suppress(Exception):
                await client.close()


class ModelPricingExampleSource(BaseModel):
    key: str
    label: str
    path: str
    exists: bool


class ModelPricingStatusResponse(BaseModel):
    source_path: str
    source_exists: bool
    source_readable: bool
    detected_schema_version: str | None = None
    detected_updated_at: datetime | None = None
    detected_model_count: int = 0
    examples: list[ModelPricingExampleSource] = Field(default_factory=list)


class ModelPricingSyncRequest(BaseModel):
    source_path: str | None = None


class ModelPricingImportRequest(BaseModel):
    payload: dict


class ModelPricingSyncResponse(BaseModel):
    status: str
    source_path: str
    synced_count: int
    skipped_custom_count: int
    pricing_last_updated_at: datetime | None = None
    message: str


class ModelPricingRowUpsertRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=50)
    model_id: str = Field(min_length=1, max_length=255)
    model_pattern: str | None = Field(default=None, max_length=255)
    input_price_per_1m: float = Field(ge=0)
    output_price_per_1m: float = Field(ge=0)
    cache_read_price_per_1m: float | None = Field(default=None, ge=0)
    cache_write_price_per_1m: float | None = Field(default=None, ge=0)
    effective_from: date | None = None
    is_custom: bool = True


class ModelPricingRowResponse(BaseModel):
    id: str
    provider: str
    model_id: str
    model_pattern: str
    input_price_per_1m: float
    output_price_per_1m: float
    cache_read_price_per_1m: float | None = None
    cache_write_price_per_1m: float | None = None
    effective_from: str
    is_custom: bool = False
    created_at: str


class ModelPricingRowListResponse(BaseModel):
    items: list[ModelPricingRowResponse]
    total: int


class ModelPricingUnpricedModelResponse(BaseModel):
    model: str
    unresolved_count: int


class ModelPricingRepriceRequest(BaseModel):
    # Optional exact model id filter (e.g. "claude-haiku-4-5-20251001").
    model: str | None = Field(default=None, max_length=255)
    # Skip rows that started before the matched price's effective_from —
    # trades coverage for the guarantee of never applying a tariff that
    # postdates the trace.
    enforce_effective_from: bool = False
    dry_run: bool = False


class ModelPricingRepriceResponse(BaseModel):
    status: str
    dry_run: bool
    traces_updated: int
    spans_updated: int
    traces_skipped_no_price: int
    traces_skipped_no_usage: int
    traces_skipped_effective_from: int
    spans_skipped_no_price: int
    spans_skipped_no_usage: int
    spans_skipped_effective_from: int
    days_reaggregated: int
    message: str


class ModelPricingUnpricedModelListResponse(BaseModel):
    items: list[ModelPricingUnpricedModelResponse]
    total: int


def _serialize_model_pricing_row(row: LLMModelPrice) -> ModelPricingRowResponse:
    created_at = row.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)

    return ModelPricingRowResponse(
        id=str(row.id),
        provider=row.provider,
        model_id=row.model_id,
        model_pattern=row.model_pattern or "",
        input_price_per_1m=float(row.input_price_per_1m),
        output_price_per_1m=float(row.output_price_per_1m),
        cache_read_price_per_1m=(
            float(row.cache_read_price_per_1m)
            if row.cache_read_price_per_1m is not None
            else None
        ),
        cache_write_price_per_1m=(
            float(row.cache_write_price_per_1m)
            if row.cache_write_price_per_1m is not None
            else None
        ),
        effective_from=row.effective_from.isoformat(),
        is_custom=bool(row.is_custom),
        created_at=created_at.isoformat(),
    )


def _normalize_model_pricing_text(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_model_pricing_upsert(
    body: ModelPricingRowUpsertRequest,
) -> tuple[str, str, str | None, date, bool]:
    provider = (body.provider or "").strip().lower()
    model_id = (body.model_id or "").strip()
    model_pattern = _normalize_model_pricing_text(body.model_pattern)
    effective_from = body.effective_from or datetime.now(UTC).date()

    if not provider:
        raise HTTPException(status_code=422, detail="Provider must not be empty")
    if not model_id:
        raise HTTPException(status_code=422, detail="Model id must not be empty")

    return provider, model_id, model_pattern, effective_from, bool(body.is_custom)


def _first_row(result) -> object | None:
    rows = result.all()
    if not rows:
        return None
    return rows[0]


async def _resolve_pricing_health(
    db: AsyncSession,
    user: User | None = None,
) -> tuple[datetime | None, bool, int | None, list[str]]:
    """Resolve pricing freshness and unresolved-model diagnostics."""
    pricing_row = _first_row(
        await db.execute(
            select(func.max(LLMModelPrice.created_at).label("pricing_last_updated_at")).where(
                LLMModelPrice.is_custom.is_(False)
            )
        )
    )
    pricing_last_updated_at = (
        getattr(pricing_row, "pricing_last_updated_at", None) if pricing_row else None
    )

    if pricing_last_updated_at is None:
        fallback_row = _first_row(
            await db.execute(
                select(func.max(LLMModelPrice.created_at).label("pricing_last_updated_at"))
            )
        )
        pricing_last_updated_at = (
            getattr(fallback_row, "pricing_last_updated_at", None) if fallback_row else None
        )

    if (
        pricing_last_updated_at is not None
        and pricing_last_updated_at.tzinfo is None
    ):
        pricing_last_updated_at = pricing_last_updated_at.replace(tzinfo=UTC)

    pricing_staleness_days: int | None = None
    if pricing_last_updated_at is not None:
        age = datetime.now(UTC) - pricing_last_updated_at
        pricing_staleness_days = max(0, int(age.total_seconds() // 86400))

    pricing_stale = (
        pricing_last_updated_at is None
        or datetime.now(UTC) - pricing_last_updated_at > timedelta(days=30)
    )

    unresolved_models_query = (
        select(
            LLMTrace.model.label("model"),
            func.count(LLMTrace.id).label("unresolved_count"),
        )
        .where(
            LLMTrace.total_cost.is_(None),
            LLMTrace.model.is_not(None),
            LLMTrace.model != "",
        )
        .group_by(LLMTrace.model)
        .order_by(desc("unresolved_count"), LLMTrace.model.asc())
        .limit(20)
    )
    user_id = getattr(user, "id", None) if user is not None else None
    if (
        user is not None
        and not bool(getattr(user, "is_admin", False))
        and user_id is not None
    ):
        unresolved_models_query = unresolved_models_query.where(
            LLMTrace.user_id == user_id
        )

    unresolved_rows = (await db.execute(unresolved_models_query)).all()

    prices_result = await db.execute(
        select(LLMModelPrice.model_id, LLMModelPrice.model_pattern).where(
            LLMModelPrice.model_id.is_not(None),
            LLMModelPrice.model_id != "",
        )
    )
    price_rows = prices_result.all()
    exact_model_ids = {str(row.model_id) for row in price_rows if row.model_id}
    regex_patterns: list[re.Pattern[str]] = []
    for row in price_rows:
        raw_pattern = getattr(row, "model_pattern", None)
        if not raw_pattern:
            continue
        try:
            regex_patterns.append(re.compile(str(raw_pattern)))
        except re.error:
            continue

    def _has_price_rule(model: str) -> bool:
        if model in exact_model_ids:
            return True
        return any(pattern.search(model) for pattern in regex_patterns)

    models_without_pricing = [
        str(row.model)
        for row in unresolved_rows
        if row.model and not _has_price_rule(str(row.model))
    ]

    return (
        pricing_last_updated_at,
        pricing_stale,
        pricing_staleness_days,
        models_without_pricing,
    )


async def _build_observability_config_response(
    ps: "PlatformSettings",
    db: AsyncSession,
    user: User | None = None,
) -> ObservabilityConfigResponse:
    from app.core.config import settings as app_settings

    last_aggregation_at = ps.last_aggregation_at
    if last_aggregation_at is not None and last_aggregation_at.tzinfo is None:
        last_aggregation_at = last_aggregation_at.replace(tzinfo=UTC)
    aggregation_stale = last_aggregation_at is None or (
        datetime.now(UTC) - last_aggregation_at
    ).total_seconds() > 48 * 3600
    (
        pricing_last_updated_at,
        pricing_stale,
        pricing_staleness_days,
        models_without_pricing,
    ) = await _resolve_pricing_health(db, user=user)
    trace_store_provider = (app_settings.tracing_store_provider or "postgres").strip().lower()
    (
        trace_store_health_status,
        trace_store_health_message,
    ) = await _resolve_trace_store_health(trace_store_provider)

    return ObservabilityConfigResponse(
        trace_store_provider=trace_store_provider,
        trace_store_health_status=trace_store_health_status,
        trace_store_health_message=trace_store_health_message,
        trace_storage_enabled=(
            True
            if ps.trace_storage_enabled is None
            else bool(ps.trace_storage_enabled)
        ),
        trace_retention_days=(
            90 if ps.trace_retention_days is None else int(ps.trace_retention_days)
        ),
        audit_log_retention_days=audit_log_retention_days(
            getattr(ps, "audit_log_retention_days", None)
        ),
        trace_store_payloads=(
            True if ps.trace_store_payloads is None else bool(ps.trace_store_payloads)
        ),
        last_aggregation_at=last_aggregation_at,
        aggregation_stale=aggregation_stale,
        pricing_last_updated_at=pricing_last_updated_at,
        pricing_stale=pricing_stale,
        pricing_staleness_days=pricing_staleness_days,
        models_without_pricing=models_without_pricing,
    )


def _model_pricing_examples() -> list[ModelPricingExampleSource]:
    candidates = [
        ("native_seed", "Native seed (app/data/model_prices.json)", MODEL_PRICES_PATH),
        (
            "litellm_seed",
            "LiteLLM seed (agents/seed_data/model_prices_and_context_window.json)",
            LITELLM_MODEL_PRICES_PATH,
        ),
    ]
    return [
        ModelPricingExampleSource(
            key=key,
            label=label,
            path=str(path),
            exists=path.exists(),
        )
        for key, label, path in candidates
    ]


async def _current_pricing_timestamp(db: AsyncSession) -> datetime | None:
    row = _first_row(
        await db.execute(
            select(func.max(LLMModelPrice.created_at).label("pricing_last_updated_at"))
        )
    )
    value = getattr(row, "pricing_last_updated_at", None) if row else None
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


@router.get("/model-pricing", response_model=ModelPricingStatusResponse)
async def get_model_pricing_status(
    user: User = Depends(require_admin),
):
    """Describe active model-pricing seed source and available example files."""
    source_path = resolve_model_prices_source_path()
    source_exists = source_path.exists()

    detected_schema_version: str | None = None
    detected_updated_at: datetime | None = None
    detected_model_count = 0
    source_readable = False

    if source_exists:
        try:
            payload = parse_model_prices_payload(
                json.loads(source_path.read_text(encoding="utf-8"))
            )
            source_readable = True
            detected_schema_version = payload.schema_version
            detected_updated_at = payload.updated_at
            detected_model_count = len(payload.prices)
        except Exception:
            source_readable = False

    return ModelPricingStatusResponse(
        source_path=str(source_path),
        source_exists=source_exists,
        source_readable=source_readable,
        detected_schema_version=detected_schema_version,
        detected_updated_at=detected_updated_at,
        detected_model_count=detected_model_count,
        examples=_model_pricing_examples(),
    )


@router.post("/model-pricing/sync", response_model=ModelPricingSyncResponse)
async def sync_model_pricing_from_seed(
    body: ModelPricingSyncRequest,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Sync model pricing from the detected seed path or an explicit source path."""
    from pathlib import Path

    if body.source_path and body.source_path.strip():
        # Admin-supplied path: confine it to the known pricing-seed directories
        # and require a .json file so a stray/probing path can't traverse to an
        # unrelated file on the host. The endpoint is admin-only and never
        # returns file content, but this is cheap defense in depth.
        candidate = Path(body.source_path).expanduser().resolve()
        allowed_roots = {
            MODEL_PRICES_PATH.parent.resolve(),
            LITELLM_MODEL_PRICES_PATH.parent.resolve(),
            resolve_model_prices_source_path().parent.resolve(),
        }
        within_allowed = any(root in candidate.parents for root in allowed_roots)
        if candidate.suffix.lower() != ".json" or not within_allowed:
            raise HTTPException(
                status_code=400,
                detail="source_path must be a .json file within an allowed pricing-seed directory",
            )
        source_path = candidate
    else:
        source_path = resolve_model_prices_source_path()

    if not source_path.exists():
        raise HTTPException(status_code=404, detail="Model pricing source file not found")

    try:
        synced_count, skipped_custom_count = await sync_model_prices(db, path=source_path)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to sync model pricing: {exc}") from exc

    pricing_last_updated_at = await _current_pricing_timestamp(db)

    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="observability",
        entity_name="Model Pricing",
        summary=(
            f"Synced model pricing from seed ({synced_count} rows, "
            f"{skipped_custom_count} custom skipped)"
        ),
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return ModelPricingSyncResponse(
        status="ok",
        source_path=str(source_path),
        synced_count=synced_count,
        skipped_custom_count=skipped_custom_count,
        pricing_last_updated_at=pricing_last_updated_at,
        message=(
            f"Pricing synchronized from seed. {synced_count} rows synced"
            f" ({skipped_custom_count} custom override(s) skipped)."
        ),
    )


@router.post("/model-pricing/import", response_model=ModelPricingSyncResponse)
async def import_model_pricing_payload(
    body: ModelPricingImportRequest,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Import model pricing from uploaded JSON content and sync into DB."""
    try:
        payload = parse_model_prices_payload(body.payload)
        synced_count, skipped_custom_count = await sync_model_prices(db, payload=payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to import model pricing payload: {exc}") from exc

    pricing_last_updated_at = await _current_pricing_timestamp(db)

    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="observability",
        entity_name="Model Pricing",
        summary=(
            f"Imported model pricing payload ({synced_count} rows, "
            f"{skipped_custom_count} custom skipped)"
        ),
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return ModelPricingSyncResponse(
        status="ok",
        source_path="uploaded_payload",
        synced_count=synced_count,
        skipped_custom_count=skipped_custom_count,
        pricing_last_updated_at=pricing_last_updated_at,
        message=(
            f"Pricing imported successfully. {synced_count} rows synced"
            f" ({skipped_custom_count} custom override(s) skipped)."
        ),
    )


@router.get("/model-pricing/rows", response_model=ModelPricingRowListResponse)
async def list_model_pricing_rows(
    search: str | None = Query(default=None, max_length=255),
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List all pricing rows for manual review and editing."""
    del user

    statement = select(LLMModelPrice)
    search_term = (search or "").strip()
    if search_term:
        pattern = f"%{search_term}%"
        statement = statement.where(
            or_(
                LLMModelPrice.provider.ilike(pattern),
                LLMModelPrice.model_id.ilike(pattern),
                LLMModelPrice.model_pattern.ilike(pattern),
            )
        )

    result = await db.execute(
        statement.order_by(
            desc(LLMModelPrice.is_custom),
            LLMModelPrice.provider.asc(),
            LLMModelPrice.model_id.asc(),
            desc(LLMModelPrice.effective_from),
            desc(LLMModelPrice.created_at),
        )
    )
    rows = list(result.scalars().all())
    items = [_serialize_model_pricing_row(row) for row in rows]
    return ModelPricingRowListResponse(items=items, total=len(items))


@router.post(
    "/model-pricing/rows",
    response_model=ModelPricingRowResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_model_pricing_row(
    body: ModelPricingRowUpsertRequest,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Create a model-pricing row for a provider/model pair."""
    (
        provider,
        model_id,
        model_pattern,
        effective_from,
        is_custom,
    ) = _normalize_model_pricing_upsert(body)

    row = LLMModelPrice(
        provider=provider,
        model_id=model_id,
        model_pattern=model_pattern,
        input_price_per_1m=body.input_price_per_1m,
        output_price_per_1m=body.output_price_per_1m,
        cache_read_price_per_1m=body.cache_read_price_per_1m,
        cache_write_price_per_1m=body.cache_write_price_per_1m,
        effective_from=effective_from,
        is_custom=is_custom,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)

    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="observability",
        entity_name="Model Pricing",
        summary=f"Created model price row for {provider}/{model_id}",
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return _serialize_model_pricing_row(row)


@router.put("/model-pricing/rows/{row_id}", response_model=ModelPricingRowResponse)
async def update_model_pricing_row(
    row_id: str,
    body: ModelPricingRowUpsertRequest,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Update an existing model-pricing row."""
    parsed_row_id = parse_path_id(row_id, not_found_detail="Model pricing row not found")

    result = await db.execute(
        select(LLMModelPrice).where(LLMModelPrice.id == parsed_row_id)
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Model pricing row not found")

    (
        provider,
        model_id,
        model_pattern,
        effective_from,
        is_custom,
    ) = _normalize_model_pricing_upsert(body)

    row.provider = provider
    row.model_id = model_id
    row.model_pattern = model_pattern
    row.input_price_per_1m = body.input_price_per_1m
    row.output_price_per_1m = body.output_price_per_1m
    row.cache_read_price_per_1m = body.cache_read_price_per_1m
    row.cache_write_price_per_1m = body.cache_write_price_per_1m
    row.effective_from = effective_from
    row.is_custom = is_custom

    await db.flush()
    await db.refresh(row)

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="observability",
        entity_name="Model Pricing",
        summary=f"Updated model price row for {provider}/{model_id}",
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return _serialize_model_pricing_row(row)


@router.get(
    "/model-pricing/unpriced-models",
    response_model=ModelPricingUnpricedModelListResponse,
)
async def list_models_without_pricing(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List every observed model that still has unresolved pricing."""
    from app.services.tracing.unpriced_models import unpriced_models

    del user

    items = [
        ModelPricingUnpricedModelResponse(model=item["model"], unresolved_count=item["trace_count"])
        for item in await unpriced_models(db)
    ]
    return ModelPricingUnpricedModelListResponse(items=items, total=len(items))


async def _audit_interrupted_reprice(
    *, user: User, request: Request, traces: int, spans: int, model: str | None
) -> None:
    """Audit what a failed reprice's committed batches applied.

    On a session of its own: the request's is still in the failed transaction,
    and rolling it back first would expire the admin this entry is written for.
    Best effort - the error answer does not depend on it.
    """
    from app.core.database import async_session

    try:
        async with async_session() as audit_db:
            await log_audit(
                audit_db,
                user=user,
                action="configure",
                entity_type="observability",
                entity_name="Model Pricing",
                summary=(
                    f"Repricing of NULL-cost traces failed after {traces} traces, "
                    f"{spans} spans were repriced"
                    + (f", model={model}" if model else "")
                ),
                request=request,
            )
            await audit_db.commit()
    except Exception as exc:
        logger.error(
            "Could not audit the partly applied trace repricing: %s",
            format_exception_message(exc),
            exc_info=True,
        )


@router.post("/model-pricing/reprice", response_model=ModelPricingRepriceResponse)
async def reprice_null_cost_traces(
    body: ModelPricingRepriceRequest,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Backfill costs for traces/spans whose cost was never resolved.

    Best effort at the CURRENT tariff: only rows with a NULL total_cost are
    filled in (existing costs are never rewritten), and the pricing table
    carries no dated tariff history, so a tariff change since the trace ran
    can yield a different figure than the one then in force.
    """
    from app.services.tracing.repricing import RepriceInterruptedError, reprice_null_costs

    model = _normalize_model_pricing_text(body.model)

    try:
        result = await reprice_null_costs(
            db,
            model=model,
            enforce_effective_from=body.enforce_effective_from,
            dry_run=body.dry_run,
        )
    except Exception as exc:
        logger.error(
            "Trace repricing failed: %s", format_exception_message(exc), exc_info=True
        )
        applied = exc.result if isinstance(exc, RepriceInterruptedError) else None
        if applied is not None and (applied.traces_updated or applied.spans_updated):
            # Batches commit one by one: the ones before the failure stay applied.
            await _audit_interrupted_reprice(
                user=user,
                request=request,
                traces=applied.traces_updated,
                spans=applied.spans_updated,
                model=model,
            )
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Repricing stopped after {applied.traces_updated} trace(s) and "
                    f"{applied.spans_updated} span(s) were repriced; run it again to "
                    "reprice the rest. See server logs."
                ),
            ) from exc
        raise HTTPException(
            status_code=500, detail="Failed to reprice traces; see server logs."
        ) from exc

    if not body.dry_run:
        await log_audit(
            db,
            user=user,
            action="configure",
            entity_type="observability",
            entity_name="Model Pricing",
            summary=(
                f"Repriced NULL-cost traces ({result.traces_updated} traces, "
                f"{result.spans_updated} spans"
                + (f", model={model}" if model else "")
                + ")"
            ),
            request=request,
        )
        # Commit before answering: the get_db teardown only commits after the response is sent.
        await db.commit()

    verb = "would be repriced" if body.dry_run else "repriced"
    return ModelPricingRepriceResponse(
        status="ok",
        dry_run=body.dry_run,
        traces_updated=result.traces_updated,
        spans_updated=result.spans_updated,
        traces_skipped_no_price=result.traces_skipped_no_price,
        traces_skipped_no_usage=result.traces_skipped_no_usage,
        traces_skipped_effective_from=result.traces_skipped_effective_from,
        spans_skipped_no_price=result.spans_skipped_no_price,
        spans_skipped_no_usage=result.spans_skipped_no_usage,
        spans_skipped_effective_from=result.spans_skipped_effective_from,
        days_reaggregated=result.days_reaggregated,
        message=(
            f"{result.traces_updated} trace(s) and {result.spans_updated} span(s) {verb}; "
            f"{result.traces_skipped_no_price} trace(s) still have no matching price."
        ),
    )


@router.get("/observability", response_model=ObservabilityConfigResponse)
async def get_observability_config(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get current observability configuration (admin only)."""
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )

    ps = await _load_ps(db)
    return await _build_observability_config_response(ps, db, user=user)


@router.get("/workspace-knowledge", response_model=WorkspaceKnowledgeConfig)
async def get_workspace_knowledge_config(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """The workspace knowledge settings (admin only)."""
    from app.api.v1.platform_settings_helpers import get_platform_settings as _load_ps

    ps = await _load_ps(db)
    return WorkspaceKnowledgeConfig(
        sources_context_percent=ps.workspace_sources_context_percent,
        conversation_idle_minutes=ps.workspace_conversation_idle_minutes,
    )


@router.put("/workspace-knowledge", response_model=WorkspaceKnowledgeConfig)
async def update_workspace_knowledge_config(
    body: WorkspaceKnowledgeConfig,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update the workspace knowledge settings (admin only). Read on every
    turn and every indexing tick, so a change applies at once."""
    from app.api.v1.platform_settings_helpers import get_platform_settings as _load_ps

    ps = await _load_ps(db)
    details = {
        "sources_context_percent": {
            "from": ps.workspace_sources_context_percent,
            "to": body.sources_context_percent,
        },
        "conversation_idle_minutes": {
            "from": ps.workspace_conversation_idle_minutes,
            "to": body.conversation_idle_minutes,
        },
    }
    ps.workspace_sources_context_percent = body.sources_context_percent
    ps.workspace_conversation_idle_minutes = body.conversation_idle_minutes
    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="workspace",
        entity_name="Workspace knowledge",
        summary="Updated workspace knowledge settings",
        details=details,
        request=request,
    )
    await db.commit()
    return body


async def _brand_asset_source_response(
    db: AsyncSession, sync: BrandAssetSourceSyncResult | None = None
) -> BrandAssetSourceResponse:
    from app.api.v1.platform_settings_helpers import get_platform_settings as _load_ps
    from app.models.integration import Integration

    ps = await _load_ps(db)
    integration_id = ps.brand_assets_source_integration_id
    name = None
    if integration_id is not None:
        name = (
            await db.execute(select(Integration.name).where(Integration.id == integration_id))
        ).scalar_one_or_none()
    return BrandAssetSourceResponse(
        url=ps.brand_assets_source_url,
        integration_id=str(integration_id) if integration_id else None,
        integration_name=name,
        sync=sync,
    )


async def _sync_brand_asset_source() -> BrandAssetSourceSyncResult:
    """Download the source, then run the asset seed under the lock every seed takes.

    The download runs before the lock, so a slow GitHub never holds a booting
    replica back. It still runs in the request: a source is meant to be a
    folder of logos (the archive is bounded, ``MAX_ARCHIVE_BYTES``).
    """
    from app.core.migrations import bootstrap_lock
    from app.services.ai_state import content_mode
    from app.services.brand_asset_source import downloaded_source, seed_with_source, source_unchanged

    async with downloaded_source() as (root, sync):
        if sync is None:
            return BrandAssetSourceSyncResult(packs=[], error="No brand asset source is set.")
        try:
            async with bootstrap_lock(wait_timeout=25):
                # A save may have landed while this downloaded: seeding the
                # older folder would retire what the newer one just seeded.
                if not await source_unchanged(sync):
                    return BrandAssetSourceSyncResult(
                        packs=[], error="The brand asset source changed while it was read; sync again."
                    )
                uploaded = await seed_with_source(content_mode(), root, sync)
        except TimeoutError:
            return BrandAssetSourceSyncResult(
                packs=list(sync.packs),
                shadowed=list(sync.shadowed),
                error="Another instance is seeding the built-in content; sync again in a moment.",
            )
    return BrandAssetSourceSyncResult(
        packs=list(sync.packs), shadowed=list(sync.shadowed), error=sync.error, assets_uploaded=uploaded
    )


@router.get("/brand-asset-source", response_model=BrandAssetSourceResponse)
async def get_brand_asset_source(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """The brand asset source (admin only): the GitHub folder of more brand packs."""
    return await _brand_asset_source_response(db)


@router.put("/brand-asset-source", response_model=BrandAssetSourceResponse)
async def update_brand_asset_source(
    body: BrandAssetSourceConfig,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Set or clear the brand asset source (admin only), then download it and seed.

    The URL must be a GitHub folder and the integration a connected GitHub
    integration the administrator may use (``require_oauth_credentials_usable``)
    that shares its account: the source is read unattended, at every boot. The
    save is committed before the download, whose outcome comes back in ``sync``.
    """
    from app.api.v1.platform_settings_helpers import get_platform_settings as _load_ps
    from app.services.brand_asset_source import (
        BrandAssetSourceError,
        parse_github_folder_url,
        resolve_source_credential,
    )
    ps = await _load_ps(db)
    url = (body.url or "").strip() or None
    integration_uuid: uuid.UUID | None = None
    if url is not None:
        try:
            parse_github_folder_url(url)
        except BrandAssetSourceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not body.integration_id:
            raise HTTPException(status_code=400, detail="Select the GitHub integration that reads the folder.")
        integration = await get_user_integration(db, user.id, body.integration_id, is_admin=True)
        try:
            await resolve_source_credential(db, integration.id)
        except BrandAssetSourceError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        integration_uuid = integration.id
    details = {
        "url": {"from": ps.brand_assets_source_url, "to": url},
        "integration_id": {
            "from": str(ps.brand_assets_source_integration_id) if ps.brand_assets_source_integration_id else None,
            "to": str(integration_uuid) if integration_uuid else None,
        },
    }
    ps.brand_assets_source_url = url
    ps.brand_assets_source_integration_id = integration_uuid
    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="platform_settings",
        entity_name="Brand asset source",
        summary="Set the brand asset source" if url else "Cleared the brand asset source",
        details=details,
        request=request,
    )
    await db.commit()
    sync = await _sync_brand_asset_source() if url else None
    return await _brand_asset_source_response(db, sync)


@router.post("/brand-asset-source/sync", response_model=BrandAssetSourceResponse)
async def sync_brand_asset_source(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Download the brand asset source again and run the asset seed (admin only)."""
    # End the transaction the auth dependencies opened: the download and the
    # seed must not keep a pooled connection idle in it.
    await db.commit()
    return await _brand_asset_source_response(db, await _sync_brand_asset_source())


@router.put("/observability", response_model=ObservabilityConfigResponse)
async def update_observability_config(
    body: ObservabilityConfigUpdate,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update observability configuration (admin only).

    The trace storage and payload switches apply in this process once the
    change is committed, and on every other web and worker process within
    the security options poll (``services/trace_switches.py``).
    """
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )
    from app.services.trace_switches import publish_trace_switches, switches_from_row

    ps = await _load_ps(db)
    previous_retention_days = (
        None if ps.trace_retention_days is None else int(ps.trace_retention_days)
    )
    previous = switches_from_row(ps.trace_storage_enabled, ps.trace_store_payloads)
    previous_audit_retention_days = audit_log_retention_days(
        getattr(ps, "audit_log_retention_days", None)
    )
    ps.trace_storage_enabled = body.trace_storage_enabled
    ps.trace_retention_days = body.trace_retention_days
    ps.trace_store_payloads = body.trace_store_payloads
    audit_details: dict = {}
    if body.audit_log_retention_days is not None:
        ps.audit_log_retention_days = body.audit_log_retention_days
        if body.audit_log_retention_days != previous_audit_retention_days:
            audit_details["audit_log_retention_days"] = {
                "from": previous_audit_retention_days,
                "to": body.audit_log_retention_days,
            }

    await db.flush()
    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="general_assistant",
        entity_name="Observability",
        summary="Updated observability settings",
        details={
            "trace_storage_enabled": {"from": previous.storage_enabled, "to": body.trace_storage_enabled},
            "trace_store_payloads": {"from": previous.store_payloads, "to": body.trace_store_payloads},
            "trace_retention_days": {"from": previous_retention_days, "to": body.trace_retention_days},
            **audit_details,
        },
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the
    # response is sent. Then publish: a switch published first could apply a
    # value the commit then fails to keep.
    await db.commit()
    publish_trace_switches(switches_from_row(body.trace_storage_enabled, body.trace_store_payloads))

    if body.trace_retention_days != previous_retention_days:
        # PostgreSQL retention is enforced by the hourly purge, which reads this
        # setting on every run. ClickHouse instead expires rows through a TTL
        # declared on the table, so a new retention window has to be pushed to
        # it. The maintenance tick reconciles that within the hour anyway; doing
        # it here is what makes lowering retention take effect when the admin
        # saves. Best-effort by design (see the helper): an unreachable
        # ClickHouse is logged and retried on the next tick rather than failing
        # the save. No-op under the postgres provider.
        from app.services.trace_storage import sync_clickhouse_trace_retention

        await sync_clickhouse_trace_retention(int(body.trace_retention_days))

    return await _build_observability_config_response(ps, db, user=user)


# ---------------------------------------------------------------------------
# Security options (admin-only, Settings > Access and security)
# ---------------------------------------------------------------------------


def _security_options_response() -> SecurityOptionsResponse:
    from app.core.security_options import describe_security_options, environment_locked_keys

    return SecurityOptionsResponse(
        options=[SecurityOptionItem(**item) for item in describe_security_options()],
        env_managed_fields=sorted(environment_locked_keys()),
    )


@router.get("/security-options", response_model=SecurityOptionsResponse)
async def get_security_options(
    user: User = Depends(require_admin),
):
    """The security options with their value, default and lock (admin only).

    Re-reads the stored values first, so the page shows what the database
    holds even when a write made on another replica has not reached this
    process's poll yet; when the database cannot be read it shows the
    values this process already has.
    """
    from app.services.security_options import refresh_security_options_cache

    await refresh_security_options_cache()
    return _security_options_response()


@router.put("/security-options", response_model=SecurityOptionsResponse)
async def update_security_options_endpoint(
    body: SecurityOptionsUpdateRequest,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Store security options (admin only); ``null`` resets one to its default.

    An option the platform mode pins (in XTM One, quotas always fail closed)
    or its environment variable sets is refused with 403, like an env-managed
    default model: the pin or the variable wins on every read, so a stored
    value would change nothing. An unknown option or an invalid value is a
    422, and nothing is written.
    """
    from app.services.security_options import (
        SecurityOptionsError,
        publish_security_options,
        update_security_options,
    )

    try:
        stored = await update_security_options(db, body.values, user=user, request=request)
    except SecurityOptionsError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    # Commit, then publish: a snapshot published first could serve a value
    # the commit then fails to keep. The other processes follow on their poll.
    await db.commit()
    publish_security_options(stored)
    return _security_options_response()


#: Built-in tools an agent receives from its own configuration (its knowledge
#: bases, memory, skills, handovers) rather than from a built-in suite, so
#: absent from the suite catalogue, each returning text someone else wrote.
_SCANNABLE_AGENT_TOOLS: dict[str, str] = {
    "search_knowledge": "Search the agent's knowledge bases.",
    "list_knowledge_documents": "List a knowledge base's documents.",
    "get_knowledge_document": "Read a knowledge base document's full source.",
    "search_memory": "Search the agent's persistent memory.",
    "load_skill": "Load a bound skill's instructions.",
    "read_skill_resource": "Read a file bundled with a bound skill.",
    "consult_agent": "Ask another agent and read its answer.",
    "check_task_status": "A background task's status and result.",
    "get_task_result": "A background task's result.",
    "get_full_task_result": "A background task's complete result.",
}

#: A candidate's description in the scan-scope editor, cut: the list is long.
_SCANNABLE_DESCRIPTION_CHARS = 160


def _scannable(name: str, description: str | None, *, entry: str | None = None) -> ScannableTool:
    text = " ".join(str(description or "").split())
    return ScannableTool(entry=entry or name, name=name, description=text[:_SCANNABLE_DESCRIPTION_CHARS])


async def _scannable_tool_groups(db: AsyncSession) -> list[ScannableToolGroup]:
    """Every tool whose results the prompt guard can screen, grouped by where it comes from.

    Built-ins by name, integration tools under the names their catalogue
    declares, MCP tools under the names each enabled server advertised - the
    names the agent loop matches the scope against - and custom tools by id
    (``custom:<id>``), each with its current name. Every server and
    custom tool, not only the administrator's: the scope applies
    platform-wide.
    """
    from app.api.v1.integration_catalog import PROVIDER_CATALOG, build_provider_tools
    from app.core.config import CUSTOM_TOOL_SCAN_PREFIX
    from app.core.feature_flags import is_feature_enabled

    groups: list[ScannableToolGroup] = []
    builtin: dict[str, str] = {}
    # The evaluation tools stay while evaluation is off: they never run
    # then, and an entry that read as stale would be dropped from the scope.
    catalog = withhold_flow_builder_tools(build_builtin_tool_catalog())
    if not is_feature_enabled("DEEP_INVESTIGATION"):
        catalog.pop("deep_investigation", None)
    if not is_feature_enabled("MOBILE_COMPANION"):
        catalog.pop("phone", None)
    withhold_health_tools(catalog)
    if not is_feature_enabled("DASHBOARD_STUDIO"):
        catalog.pop("custom_dashboards", None)
    if not is_feature_enabled("VIDEO_STUDIO"):
        catalog.pop("video_studio", None)
    for definitions in catalog.values():
        for definition in definitions:
            builtin.setdefault(definition["name"], definition.get("description") or "")
    for name, description in _SCANNABLE_AGENT_TOOLS.items():
        builtin.setdefault(name, description)
    groups.append(
        ScannableToolGroup(
            key="builtin",
            label="Built-in tools",
            tools=[_scannable(name, builtin[name]) for name in sorted(builtin)],
        )
    )

    custom_tools = (await db.execute(select(CustomTool.id, CustomTool.name, CustomTool.description))).all()
    if custom_tools:
        groups.append(
            ScannableToolGroup(
                key="custom",
                label="Custom tools",
                tools=[
                    _scannable(str(name or ""), description, entry=f"{CUSTOM_TOOL_SCAN_PREFIX}{tool_id}")
                    for tool_id, name, description in sorted(
                        custom_tools, key=lambda row: (str(row[1] or "").lower(), str(row[0]))
                    )
                ],
            )
        )

    servers = (
        await db.execute(
            select(MCPServer.id, MCPServer.name, MCPServer.discovered_tools).where(MCPServer.enabled.is_(True))
        )
    ).all()
    for server_id, server_name, discovered in sorted(servers, key=lambda row: str(row[1] or "").lower()):
        offered: dict[str, str] = {}
        for tool in discovered if isinstance(discovered, list) else []:
            name = tool.get("name") if isinstance(tool, dict) else None
            if isinstance(name, str) and name.strip():
                offered.setdefault(name, str(tool.get("description") or ""))
        if offered:
            groups.append(
                ScannableToolGroup(
                    key=f"mcp:{server_id}",
                    label=f"MCP: {server_name}",
                    tools=[_scannable(name, offered[name]) for name in sorted(offered)],
                )
            )

    for provider, tools in sorted(build_provider_tools().items()):
        if tools:
            groups.append(
                ScannableToolGroup(
                    key=provider,
                    label=(PROVIDER_CATALOG.get(provider) or {}).get("name") or provider,
                    tools=[_scannable(tool.name, tool.description) for tool in sorted(tools, key=lambda t: t.name)],
                )
            )
    return groups


@router.get("/security-options/scannable-tools", response_model=ScannableToolsResponse)
async def list_scannable_tools(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """The tools "Tools screened by the prompt guard" can list (admin only).

    What the scan-scope editor picks from, so an entry is a name a tool
    carries rather than a typo that screens nothing. Read-only.
    """
    return ScannableToolsResponse(groups=await _scannable_tool_groups(db))


# ---------------------------------------------------------------------------
# Langfuse Observability (admin-only)
# ---------------------------------------------------------------------------


def _langfuse_env_managed() -> bool:
    """Whether Langfuse is seeded from ``LANGFUSE_*`` environment variables.

    ``main._seed_langfuse_from_env`` creates/overwrites the DB row on every
    startup when both ``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY`` are
    set (env always wins), so the Settings → Usage and quotas → Observability Langfuse card must
    be read-only in that case — exactly like the env-seeded LLM provider lock.
    """
    from app.core.config import settings as app_settings

    # ``.strip()`` so whitespace-only keys (e.g. ``LANGFUSE_SECRET_KEY=" "``)
    # are treated as unset — they would not seed a usable config in
    # ``_seed_langfuse_from_env`` (which strips too), so they must not lock the
    # UI / 403 the PUT either.
    return bool(
        app_settings.langfuse_public_key.strip()
        and app_settings.langfuse_secret_key.strip()
    )


@router.get("/langfuse", response_model=LangfuseConfigResponse)
async def get_langfuse_config(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get current Langfuse configuration (admin only)."""
    from app.models.langfuse_config import LangfuseConfig

    result = await db.execute(select(LangfuseConfig))
    config = result.scalar_one_or_none()

    is_from_env = _langfuse_env_managed()

    if not config:
        return LangfuseConfigResponse(
            enabled=False,
            has_public_key=False,
            has_secret_key=False,
            host=LANGFUSE_DEFAULT_HOST,
            is_from_env=is_from_env,
        )

    return LangfuseConfigResponse(
        enabled=config.enabled,
        has_public_key=bool(config.public_key_enc),
        has_secret_key=bool(config.secret_key_enc),
        host=config.host or LANGFUSE_DEFAULT_HOST,
        is_from_env=is_from_env,
        vault_secrets=vault_status(config, ADMIN_PRINCIPAL),
    )


@router.put("/langfuse", response_model=LangfuseConfigResponse)
async def update_langfuse_config(
    body: LangfuseConfigRequest,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create or update Langfuse configuration (admin only)."""
    from app.models.langfuse_config import LangfuseConfig
    from app.services.langfuse_tracing import areset_langfuse_client

    # Reject edits when Langfuse is env-managed: the LANGFUSE_* env vars are
    # re-applied on every startup (env always wins), so a UI change would
    # silently revert on the next restart.  Same contract as the env-seeded
    # LLM provider lock and the env-managed default-model lock.
    if _langfuse_env_managed():
        raise HTTPException(
            status_code=403,
            detail=(
                "Langfuse is configured via LANGFUSE_* environment variables "
                "and is read-only. Update the environment variables and "
                "restart the backend to change it."
            ),
        )

    result = await db.execute(select(LangfuseConfig))
    config = result.scalar_one_or_none()

    is_new = config is None
    if not config:
        config = LangfuseConfig(
            enabled=body.enabled,
            host=body.host or LANGFUSE_DEFAULT_HOST,
        )
        db.add(config)
    else:
        config.enabled = body.enabled
        config.host = body.host or LANGFUSE_DEFAULT_HOST

    # Only update secrets if non-empty
    if body.public_key:
        config.public_key_enc = encrypt_value(body.public_key)
    if body.secret_key:
        config.secret_key_enc = encrypt_value(body.secret_key)

    await db.flush()

    logger.info(
        "Langfuse config %s by %s: enabled=%s, host=%s, public_key=%s, secret_key=%s",
        "created" if is_new else "updated",
        user.email,
        body.enabled,
        body.host,
        "set" if body.public_key else "unchanged",
        "set" if body.secret_key else "unchanged",
    )

    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="langfuse",
        entity_name="Langfuse",
        summary=f"{'Enabled' if body.enabled else 'Disabled'} Langfuse observability",
        request=request,
    )
    # Commit before answering, and before the reset below: the client re-reads
    # its config from the database on the next call, and the get_db teardown
    # only commits after the response is sent.
    await db.commit()

    # Reset the cached client so it picks up the new config. ``areset`` detaches
    # the client on the loop (atomic) then flushes + shuts it down off-loop, so
    # disabling a Langfuse pointed at an unreachable host never stalls the web
    # event loop and never races concurrent requests on the module globals.
    await areset_langfuse_client()

    return LangfuseConfigResponse(
        enabled=config.enabled,
        has_public_key=bool(config.public_key_enc),
        has_secret_key=bool(config.secret_key_enc),
        host=config.host,
        is_from_env=_langfuse_env_managed(),
        vault_secrets=vault_status(config, ADMIN_PRINCIPAL),
    )


@router.post("/langfuse/test")
async def test_langfuse_connection(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Test the Langfuse connection using saved credentials (admin only).

    Creates a temporary Langfuse client and runs a bounded ``projects.get()``
    auth probe to verify that the host is reachable and the API keys are
    valid.  Also resets the cached client so the next real trace uses the
    latest config.
    """
    from app.models.langfuse_config import LangfuseConfig
    from app.services.langfuse_tracing import (
        LANGFUSE_EXPORT_TIMEOUT,
        areset_langfuse_client,
    )

    result = await db.execute(select(LangfuseConfig))
    config = result.scalar_one_or_none()

    if not config:
        return {
            "status": "error",
            "message": "Langfuse is not configured. Save your credentials first.",
        }

    if not config.enabled:
        return {
            "status": "error",
            "message": "Langfuse tracing is disabled. Enable it and save first.",
        }

    if not config.public_key_enc or not config.secret_key_enc:
        return {
            "status": "error",
            "message": "Missing credentials. Set both public key and secret key.",
        }

    try:
        from langfuse import Langfuse

        public_key = await reveal_field(config, "public_key_enc") or ""
        secret_key = await reveal_field(config, "secret_key_enc") or ""
        host = config.host or LANGFUSE_DEFAULT_HOST

        def _blocking_test() -> bool:
            """Build a throwaway client, auth-check, and shut it down. BLOCKING.

            The Langfuse SDK is synchronous, so this runs off the event loop
            (via ``asyncio.to_thread``) so an unreachable host can't stall the
            web event loop for the whole HTTP timeout.

            The constructor ``timeout`` MUST match the real client's export
            timeout: the SDK's resource manager is a per-public-key singleton
            that ``shutdown()`` does not evict, so whichever ``Langfuse(...)``
            construction runs first pins the OTLP span exporter timeout for
            the whole process.  The auth check itself stays short via
            per-request options.
            """
            client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                host=host,
                timeout=LANGFUSE_EXPORT_TIMEOUT,
            )
            try:
                projects = client.api.projects.get(
                    request_options={"timeout_in_seconds": 10}
                )
                return bool(projects.data)
            finally:
                try:
                    client.shutdown()
                except Exception:
                    logger.debug(
                        "Langfuse test client shutdown failed", exc_info=True
                    )

        has_project = await asyncio.wait_for(
            asyncio.to_thread(_blocking_test), timeout=15
        )
        if not has_project:
            logger.warning("Langfuse test connection: no project found (host=%s)", host)
            return {
                "status": "error",
                "message": f"Authentication failed. No project found for the keys on {host}.",
            }

        # Reset cached client so the next real trace re-initializes
        await areset_langfuse_client()
        logger.info(
            "Langfuse test connection succeeded for %s (host=%s)", user.email, host
        )
        return {
            "status": "success",
            "message": f"Connected to {host} — authentication passed.",
        }

    except ImportError as imp_err:
        logger.warning("Langfuse import failed: %s", format_exception_message(imp_err), exc_info=True)
        return {
            "status": "error",
            "message": "Langfuse library is not installed or cannot be imported.",
        }
    except TimeoutError:
        # asyncio.wait_for timed out — the SDK auth check didn't return in time.
        # Its str() is empty, so handle it explicitly rather than letting the
        # generic handler below emit a blank "...failed: " message. ``host`` is
        # already bound here (a timeout can only originate at the wait_for above,
        # which runs after ``host`` is computed), so report the exact URL that
        # was tested — including the default host when ``config.host`` is null.
        logger.warning("Langfuse test connection timed out (host=%s)", host)
        return {
            "status": "error",
            "message": (
                f"Connection to {host} timed out. "
                "Check the host URL and network connectivity."
            ),
        }
    except Exception as exc:
        error_msg = str(exc)
        # Extract useful info from HTTP errors
        if "403" in error_msg:
            return {
                "status": "error",
                "message": f"403 Forbidden from {config.host or 'Langfuse'}. Check IP whitelisting and access control.",
            }
        if "401" in error_msg:
            return {
                "status": "error",
                "message": "401 Unauthorized. Your API keys are invalid.",
            }
        if "connection" in error_msg.lower() or "resolve" in error_msg.lower():
            return {
                "status": "error",
                "message": f"Cannot reach {config.host or 'Langfuse'}. Check the host URL and network connectivity.",
            }
        logger.error("Langfuse test connection failed: %s", format_exception_message(exc), exc_info=True)
        return {
            "status": "error",
            "message": "Connection failed. Check your Langfuse configuration and try again.",
        }


# ---------------------------------------------------------------------------
# Tags – aggregate all unique tags across entities visible to the current user
# ---------------------------------------------------------------------------


@router.get("/tags")
async def list_all_tags(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[str]:
    """Return a sorted, deduplicated list of all tags across entities the user can see."""
    tags: set[str] = set()
    user_group_ids = await get_user_group_ids(db, user.id)

    # Each entity model stores tags as a JSON list. We query all visible
    # entities (own + group-shared + company-managed).
    tag_models = [Agent, Integration, KnowledgeBase, MCPServer, CustomTool]

    for model in tag_models:
        result = await db.execute(
            select(model.tags)
            .where(user_visible_filter(model, user.id, user_group_ids))
            .where(model.tags.is_not(None))
        )
        for (row_tags,) in result.all():
            if isinstance(row_tags, list):
                for t in row_tags:
                    if isinstance(t, str) and t.strip():
                        tags.add(t.strip().lower())

    return sorted(tags)


# ---------------------------------------------------------------------------
# Emergency memory purge – wipe all agent memories across the platform
# ---------------------------------------------------------------------------


@router.post("/purge-all-memories")
async def purge_all_memories(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> dict:
    """Purge ALL agent memories across the platform (admin emergency action).

    Clears all memory tiers:
      1. PostgreSQL persistent memories (agent_memory_entries table)
      2. PostgreSQL style learning embeddings (embeddings table, source_type='style_guide')
      3. Redis short-term and medium-term memories (agent_mem:* keys)
      4. Redis user memories (user_mem:* keys)
    """
    from app.core.redis import redis_client

    # ── PostgreSQL: persistent memories ─────────────────────────────────
    count_result = await db.execute(select(func.count()).select_from(AgentMemoryEntry))
    persistent_count = count_result.scalar() or 0

    await db.execute(delete(AgentMemoryEntry))

    # ── PostgreSQL: style learning embeddings ───────────────────────────
    style_count_result = await db.execute(
        select(func.count())
        .select_from(Embedding)
        .where(Embedding.source_type == "style_guide")
    )
    style_count = style_count_result.scalar() or 0

    if style_count:
        await db.execute(
            delete(Embedding).where(Embedding.source_type == "style_guide")
        )

    await db.flush()
    await db.commit()

    # ── Redis: agent memories (agent_mem:*) ─────────────────────────────
    redis = await redis_client()
    redis_deleted = 0

    cursor: int = 0
    while True:
        _next, keys = await redis.scan(cursor, match="agent_mem:*", count=500)
        cursor = int(_next)
        if keys:
            redis_deleted += len(keys)
            await redis.delete(*keys)
        if not cursor:
            break

    # ── Redis: user memories (user_mem:*) ───────────────────────────────
    cursor = 0
    while True:
        _next, keys = await redis.scan(cursor, match="user_mem:*", count=500)
        cursor = int(_next)
        if keys:
            redis_deleted += len(keys)
            await redis.delete(*keys)
        if not cursor:
            break

    total = persistent_count + style_count + redis_deleted

    await log_audit(
        db,
        user=user,
        action="purge",
        entity_type="memory",
        entity_id=None,
        entity_name="all_agent_memories",
        summary=(
            f"Emergency purge: cleared {persistent_count} persistent memories, "
            f"{style_count} style learning embeddings, and {redis_deleted} Redis memory keys"
        ),
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    logger.warning(
        "EMERGENCY MEMORY PURGE by %s: %d persistent + %d style embeddings + %d redis keys deleted",
        user.email,
        persistent_count,
        style_count,
        redis_deleted,
    )

    return {
        "status": "success",
        "persistent_deleted": persistent_count,
        "style_embeddings_deleted": style_count,
        "redis_deleted": redis_deleted,
        "total_deleted": total,
    }


# ---------------------------------------------------------------------------
# User memory management – view and purge own memories
# ---------------------------------------------------------------------------


@router.get("/me/memory")
async def get_my_memory(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Get the authenticated user's memory across all agents.

    Returns the cross-agent user profile (user_mem) and per-agent
    Redis memories (agent_mem scoped to this user), plus persistent
    memory counts per agent.
    """
    from app.core.redis import redis_client
    from app.services.agent_memory import load_memory as load_agent_memory
    from app.services.user_memory import load_user_memory

    uid = str(user.id)

    # 1. Cross-agent user profile
    user_profile = await load_user_memory(uid)

    # 2. Discover all agent_mem keys scoped to this user
    redis = await redis_client()
    agent_ids: set[str] = set()
    pattern = f"agent_mem:*:scope:{uid}:*"
    cursor: int = 0
    while True:
        _next, keys = await redis.scan(cursor, match=pattern, count=200)
        cursor = int(_next)
        for key in keys:
            key_str = key if isinstance(key, str) else key.decode()
            parts = key_str.split(":")
            if len(parts) >= 4:
                agent_ids.add(parts[1])
        if not cursor:
            break

    # 3. Also check persistent memory entries scoped to this user
    from app.models.agent_memory_entry import AgentMemoryEntry

    persistent_result = await db.execute(
        select(
            AgentMemoryEntry.agent_id,
            func.count(AgentMemoryEntry.id),
        )
        .where(AgentMemoryEntry.user_id == user.id)
        .where(AgentMemoryEntry.agent_id.is_not(None))
        .group_by(AgentMemoryEntry.agent_id)
    )
    persistent_counts: dict[str, int] = {}
    for agent_uuid, count in persistent_result.all():
        aid = str(agent_uuid)
        persistent_counts[aid] = count
        agent_ids.add(aid)

    # 3b. The agents whose notes or recent interactions the user memory holds:
    # each is listed, with its Clear, even with its own memory off.
    for raw in (
        *(entry.get("owner_agent_id") for entry in user_profile.get("entries", [])),
        *(item.get("agent_id") for item in user_profile.get("short_term", [])),
    ):
        if owner := safe_uuid(raw):
            agent_ids.add(str(owner))

    # 4. Load agent details and per-agent Redis memories
    from app.models.agent import Agent as AgentModel

    agents_result = (
        await db.execute(
            select(AgentModel).where(
                AgentModel.id.in_([uuid.UUID(a) for a in agent_ids])
            )
        )
        if agent_ids
        else None
    )
    agents_map: dict[str, dict] = {}
    if agents_result:
        for agent in agents_result.scalars().all():
            agents_map[str(agent.id)] = {
                "agent_id": str(agent.id),
                "agent_name": agent.name,
                "agent_slug": agent.slug,
                "agent_icon": agent.icon,
            }

    agent_memories: list[dict] = []
    for aid in sorted(agent_ids):
        agent_info = agents_map.get(
            aid,
            {
                "agent_id": aid,
                "agent_name": "(deleted agent)",
                "agent_slug": "",
                "agent_icon": None,
            },
        )

        redis_mem = await load_agent_memory(aid, scope_id=uid)

        agent_memories.append(
            {
                **agent_info,
                "redis_memory": redis_mem,
                "persistent_count": persistent_counts.get(aid, 0),
            }
        )

    # 5. Name the agent each note of the profile belongs to.
    for entry in user_profile.get("entries", []):
        owner = entry.get("owner_agent_id")
        known = agents_map.get(str(safe_uuid(owner))) if owner else None
        entry["owner_agent_name"] = (known or {}).get("agent_name", "(deleted agent)") if owner else None

    return {
        "user_profile": user_profile,
        "agents": agent_memories,
    }


@router.delete("/me/memory")
async def purge_my_memory(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Purge ALL of the authenticated user's memories.

    Clears: user profile (user_mem), all per-agent scoped Redis keys
    (agent_mem:*:scope:{user_id}:*), and persistent memory entries
    scoped to this user.
    """
    from app.core.redis import redis_client
    from app.models.agent_memory_entry import AgentMemoryEntry
    from app.services.user_memory import purge_user_memory

    uid = str(user.id)
    redis = await redis_client()

    # 1. Clear user profile keys
    redis_deleted = await purge_user_memory(uid)

    # 2. Clear all agent_mem scoped to this user
    pattern = f"agent_mem:*:scope:{uid}:*"
    cursor: int = 0
    while True:
        _next, keys = await redis.scan(cursor, match=pattern, count=200)
        cursor = int(_next)
        if keys:
            redis_deleted += len(keys)
            await redis.delete(*keys)
        if not cursor:
            break

    # 3. Clear persistent memory entries scoped to this user
    count_result = await db.execute(
        select(func.count())
        .select_from(AgentMemoryEntry)
        .where(AgentMemoryEntry.user_id == user.id)
    )
    persistent_count = count_result.scalar() or 0
    if persistent_count:
        await db.execute(
            delete(AgentMemoryEntry).where(AgentMemoryEntry.user_id == user.id)
        )
        await db.flush()
    # The user's own deletion purges the consolidation history at once (B19).
    from app.services.health.memory_history import purge_lane_consolidations

    await purge_lane_consolidations(db, user_id=user.id)

    await db.commit()

    logger.info(
        "User %s purged all their memories: %d redis + %d persistent",
        user.email,
        redis_deleted,
        persistent_count,
    )

    return {
        "status": "success",
        "redis_deleted": redis_deleted,
        "persistent_deleted": persistent_count,
    }


@router.delete("/me/memory/user-profile")
async def purge_my_user_profile(
    user: User = Depends(get_current_user),
) -> dict:
    """Purge only the cross-agent user profile memory (user_mem)."""
    from app.services.user_memory import purge_user_memory

    deleted = await purge_user_memory(str(user.id))

    logger.info(
        "User %s purged their user profile memory (%d keys)", user.email, deleted
    )
    return {"status": "success", "redis_deleted": deleted}


@router.delete("/me/memory/agent/{agent_id}")
async def purge_my_agent_memory(
    agent_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Purge the user's memory with a specific agent.

    Clears both Redis (agent_mem scoped to user, and the notes the agent's
    work left in the user memory) and persistent memory entries for this
    agent+user pair.
    """
    from app.core.redis import redis_client
    from app.models.agent_memory_entry import AgentMemoryEntry
    from app.services.user_memory import forget_agent_notes

    uid = str(user.id)
    aid = str(agent_id)
    redis = await redis_client()
    redis_deleted = await forget_agent_notes(uid, aid)

    pattern = f"agent_mem:{aid}:scope:{uid}:*"
    cursor: int = 0
    while True:
        _next, keys = await redis.scan(cursor, match=pattern, count=200)
        cursor = int(_next)
        if keys:
            redis_deleted += len(keys)
            await redis.delete(*keys)
        if not cursor:
            break

    count_result = await db.execute(
        select(func.count())
        .select_from(AgentMemoryEntry)
        .where(
            AgentMemoryEntry.user_id == user.id,
            AgentMemoryEntry.agent_id == agent_id,
        )
    )
    persistent_count = count_result.scalar() or 0
    if persistent_count:
        await db.execute(
            delete(AgentMemoryEntry).where(
                AgentMemoryEntry.user_id == user.id,
                AgentMemoryEntry.agent_id == agent_id,
            )
        )
        await db.flush()
    from app.services.health.memory_history import purge_lane_consolidations

    await purge_lane_consolidations(db, user_id=user.id, agent_id=agent_id)

    await db.commit()

    logger.info(
        "User %s purged memory with agent %s: %d redis + %d persistent",
        user.email,
        aid,
        redis_deleted,
        persistent_count,
    )

    return {
        "status": "success",
        "redis_deleted": redis_deleted,
        "persistent_deleted": persistent_count,
    }


# ---------------------------------------------------------------------------
# MCP Endpoint key management
# ---------------------------------------------------------------------------


@router.post("/mcp-endpoint/generate-key")
async def generate_mcp_key(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Generate a new global MCP API key (admin only).

    Returns the full key exactly once.  The key is stored as a bcrypt hash
    and only the prefix is kept for display.
    """
    import secrets

    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )
    from app.core.security import bcrypt_hashpw_async

    ps = await _load_ps(db)

    raw_key = f"mcp-{secrets.token_hex(24)}"
    key_hash = (await bcrypt_hashpw_async(raw_key.encode("utf-8"))).decode("utf-8")
    key_prefix = raw_key[:12] + "..."

    ps.mcp_global_api_key_hash = key_hash
    ps.mcp_global_api_key_prefix = key_prefix

    await db.flush()
    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="general_assistant",
        entity_name="MCP Endpoint",
        summary="Generated new global MCP API key",
        request=request,
    )
    # Commit before answering (``get_db`` commits after the response): the
    # new key is used on the next request, and the one it replaces must stop
    # authenticating at once.
    await db.commit()

    return {"key": raw_key, "prefix": key_prefix}


@router.delete("/mcp-endpoint/revoke-key", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_mcp_key(
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Revoke the global MCP API key (admin only)."""
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )

    ps = await _load_ps(db)
    if not ps.mcp_global_api_key_hash:
        raise HTTPException(status_code=404, detail="No MCP API key configured")

    ps.mcp_global_api_key_hash = None
    ps.mcp_global_api_key_prefix = None

    await db.flush()
    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="general_assistant",
        entity_name="MCP Endpoint",
        summary="Revoked global MCP API key",
        request=request,
    )
    # Commit before answering (``get_db`` commits after the response): a
    # revoked key must stop authenticating the moment the caller is told so.
    await db.commit()


# ---------------------------------------------------------------------------
# Platform parameters
# ---------------------------------------------------------------------------


_BRANDING_IMAGE_FIELDS: tuple[str, ...] = (
    "platform_logo",
    "platform_favicon",
    "platform_og_image",
)

_BRANDING_COLOR_FIELDS: tuple[str, ...] = (
    "platform_theme_color",
    "platform_accent_color",
    "platform_background_color",
)

# Text fields with character-length caps.  Must match the column lengths in
# ``PlatformSettings`` so admin save flows never trip a 500 from SQL.
_BRANDING_TEXT_LIMITS: dict[str, int] = {
    "platform_name": 255,
    "platform_description": 1000,
    # Markdown login message — stored in a TEXT column, capped here so an
    # admin can't paste an unbounded payload that bloats every public
    # /platform/config response.
    "platform_login_message": 5000,
}

_BRANDING_IMAGE_FIELD_TO_KIND: dict[str, str] = {
    "platform_logo": "logo",
    "platform_favicon": "favicon",
    "platform_og_image": "og-image",
}

# Cap for absolute-URL branding values (currently only ``platform_og_image``).
_BRANDING_URL_MAX_LEN = 2048

_HEX_COLOR_RE = _branding_re.compile(
    r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$"
)


def _require_branding_string(field: str, value) -> str:
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail=f"{field}: must be a string")
    return value


def _validate_data_url(field: str, value: str) -> None:
    """Validate that ``value`` is a well-formed ``data:image/...;base64,`` URL."""
    value = _require_branding_string(field, value)
    parsed = parse_branding_data_url(value)
    if parsed is None:
        raise HTTPException(
            status_code=400,
            detail=f"{field}: must be a base64 data URL (data:image/...;base64,...)",
        )
    mime, payload = parsed
    if mime not in SAFE_BRANDING_IMAGE_MIMES:
        raise HTTPException(
            status_code=400,
            detail=f"{field}: unsupported MIME {mime!r}",
        )
    cap = BRANDING_IMAGE_MAX_BYTES.get(_BRANDING_IMAGE_FIELD_TO_KIND[field], 1024 * 1024)
    if estimated_base64_decoded_size(payload) > cap:
        raise HTTPException(
            status_code=400,
            detail=f"{field}: exceeds {cap // 1024} KB limit",
        )
    try:
        decoded = base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"{field}: invalid base64 payload ({exc})",
        ) from exc
    if len(decoded) > cap:
        raise HTTPException(
            status_code=400,
            detail=f"{field}: exceeds {cap // 1024} KB limit",
        )


def _validate_branding_image(field: str, value: str) -> None:
    """Validate an image field. ``platform_og_image`` also accepts http(s) URLs."""
    value = _require_branding_string(field, value)
    if field == "platform_og_image" and value.startswith(("http://", "https://")):
        if len(value) > _BRANDING_URL_MAX_LEN:
            raise HTTPException(
                status_code=400,
                detail=f"{field}: URL exceeds {_BRANDING_URL_MAX_LEN} characters",
            )
        return
    _validate_data_url(field, value)


def _validate_text_length(field: str, value: str) -> None:
    value = _require_branding_string(field, value)
    cap = _BRANDING_TEXT_LIMITS[field]
    if len(value) > cap:
        raise HTTPException(
            status_code=400,
            detail=f"{field}: must be at most {cap} characters",
        )


def _validate_hex_color(field: str, value: str) -> None:
    value = _require_branding_string(field, value)
    if not _HEX_COLOR_RE.match(value):
        raise HTTPException(
            status_code=400,
            detail=f"{field}: must be a hex color (#rgb, #rrggbb, or #rrggbbaa)",
        )


def _serialize_platform_settings(ps, app_settings) -> dict:
    # This endpoint (GET /settings/platform) intentionally still inlines
    # base64 images — it feeds the admin branding *edit* form, which needs
    # the raw bytes to render a live preview of an in-progress upload before
    # it's saved (see FileUpload / ParametersSection.tsx). It's authenticated,
    # low-traffic, and only fetched when an admin opens Settings, so the
    # payload-size tradeoff is acceptable here.
    #
    # The public, unauthenticated, every-page-load GET /platform/config
    # endpoint had the same problem at much higher cost (up to ~1.4 MB on
    # every boot) and has been fixed: dedicated binary asset endpoints
    #   GET /api/v1/platform/branding/asset/{logo,favicon,og-image}
    # return raw bytes with Content-Type + ETag (SHA-256 of content) +
    # Cache-Control: public, max-age=86400 for a URL whose ?v= names the
    # served bytes (public, no-cache otherwise), and
    # resolved_branding_payload() (app/services/branding.py) now returns only
    # an asset URL + a short content hash (platform_*_hash) used as that ?v=
    # cache-buster — see those functions for the current implementation.
    return {
        "instance_id": str(ps.id),
        # Return the stored override, not the resolved display name.  A NULL
        # value means "use the mode-aware default" in the runtime config.
        "platform_name": ps.platform_name,
        # Prefer the new base64 column; expose the deprecated URL as a fallback.
        "platform_logo": ps.platform_logo,
        "platform_logo_url": ps.platform_logo_url,
        "platform_description": ps.platform_description,
        "platform_login_message": ps.platform_login_message,
        "platform_theme_color": ps.platform_theme_color,
        "platform_favicon": ps.platform_favicon,
        "platform_accent_color": ps.platform_accent_color,
        "platform_background_color": ps.platform_background_color,
        "platform_og_image": ps.platform_og_image,
        "platform_language": getattr(ps, "platform_language", None) or "auto",
        # The stored override, not the resolved default: NULL means
        # "the built-in brand deck", resolved per call in
        # ``presentations.resolve_default_template_id``, so the admin form can
        # tell "explicitly chosen" from "inherited".
        "default_presentation_template_id": (
            str(dpt)
            if (dpt := getattr(ps, "default_presentation_template_id", None))
            else None
        ),
        "has_db_license": bool(ps.enterprise_license),
        "has_env_license": bool(app_settings.enterprise_license),
        # Env-related provider mode — kept in the canonical serializer
        # (not bolted on at the endpoint) so GET /platform and the PUT
        # /platform response carry the same shape.  Without this, the
        # frontend cache could briefly show env-only mode as OFF after a
        # branding save because the PUT response would lack the key and
        # TanStack would write a payload with the field undefined until
        # the GET cache invalidation completed.
        "has_env_providers": _has_env_llm_providers(),
        # The RESOLVED state the UI gates on, not the raw variable:
        # ``BYOLLM_ACTIVATED=true`` hands provider management back to
        # administrators even where ``MODELS_ENV_PROVIDING_ONLY`` is still set
        # (``provider_management_env_only``). Serving the raw flag would hide
        # the add / edit / delete controls the API now accepts. The flag that
        # resolved it travels next to it so a reader can tell the two apart.
        "models_env_providing_only": app_settings.provider_management_env_only,
        "byollm_activated": app_settings.byollm_activated,
        # The stored override, not the resolved state — same tri-state and the
        # same name the PUT accepts, so the form round-trips what it saved. The
        # resolved boolean every client gates on is destined for
        # /platform/config (T025); until that lands this admin-only read is the
        # only place it can be derived. ``ai_enabled_default`` is what clearing
        # the override lands
        # on, which the reset action has to be able to state.
        "ai_enabled": ai_state.override_to_wire(ps.ai_disabled),
        "ai_enabled_default": ai_state.ai_enabled_by_mode(),
        # Platform-wide kill switch for the per-tool approval gate
        # (``tool_approvals_enabled``) and what an agent with no approval
        # setting of its own does (``approve_tool_calls_default``); the same
        # serializer feeds the admin tool ``get_platform_ai_settings``.
        **serialize_tool_approval_settings(ps),
        # Agent runs: the ``wait`` policy, effective values (the
        # environment winning), ceilings and the env-locked fields.
        **serialize_wait_settings(ps),
    }


@router.get("/platform")
async def get_platform_settings_endpoint(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get platform branding / parameter settings (any authenticated user).

    Returns license metadata (has_db_license, has_env_license) so the
    frontend can display the license management card properly.
    """
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )
    from app.core.config import settings as app_settings

    ps = await _load_ps(db)

    return _serialize_platform_settings(ps, app_settings)


def _has_env_llm_providers() -> bool:
    """Check if any LLM_PROVIDER_*_API_KEY env vars are set."""
    import os

    return any(
        k.startswith("LLM_PROVIDER_") and k.endswith("_API_KEY") and v.strip()
        for k, v in os.environ.items()
    )


@router.put("/platform")
async def update_platform_settings_endpoint(
    body: dict,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update platform branding / parameter settings (admin only).

    Accepts the full branding payload: ``platform_name``, ``platform_logo``
    (base64 data URL), ``platform_description``, ``platform_login_message``
    (Markdown), ``platform_theme_color``, ``platform_accent_color``,
    ``platform_background_color``, ``platform_favicon`` (base64 data URL),
    and ``platform_og_image`` (base64 data URL or absolute http(s) URL).
    Image fields are validated as well-formed ``data:image/...`` base64 URLs
    within the configured size cap; ``platform_og_image`` additionally
    accepts an absolute URL (length-capped).  Color fields are validated as
    hex strings; text fields are length-capped to match the DB schema.

    Also accepts ``enterprise_license`` to install or remove the license PEM.
    Send ``null`` or ``""`` to remove the DB-stored license.

    Uploading a new ``platform_logo`` clears the deprecated
    ``platform_logo_url`` column so stale legacy values do not accumulate.
    """
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )
    from app.core.config import settings as app_settings
    ps = await _load_ps(db)

    branding_changed = False
    if "platform_name" in body:
        raw_name = body["platform_name"]
        if raw_name is None:
            ps.platform_name = None
        else:
            raw_name = _require_branding_string("platform_name", raw_name)
            if raw_name.strip():
                stripped = raw_name.strip()
                _validate_text_length("platform_name", stripped)
                ps.platform_name = stripped
            else:
                ps.platform_name = None
        branding_changed = True
    if "platform_logo_url" in body:
        raw_logo_url = body["platform_logo_url"]
        if raw_logo_url in (None, ""):
            ps.platform_logo_url = None
        else:
            ps.platform_logo_url = _require_branding_string(
                "platform_logo_url",
                raw_logo_url,
            )
        branding_changed = True

    for field in _BRANDING_IMAGE_FIELDS:
        if field not in body:
            continue
        raw = body[field]
        value = (raw or None) if isinstance(raw, str) else raw
        if value is not None:
            _validate_branding_image(field, value)
        setattr(ps, field, value)
        # Uploading a new logo supersedes the deprecated URL column so
        # legacy values do not silently linger after a customer rebrand.
        if field == "platform_logo" and value is not None:
            ps.platform_logo_url = None
        branding_changed = True

    if "platform_description" in body:
        raw_desc = body["platform_description"]
        if raw_desc is None:
            ps.platform_description = None
        else:
            raw_desc = _require_branding_string("platform_description", raw_desc)
            stripped = raw_desc.strip()
            if stripped:
                _validate_text_length("platform_description", stripped)
                ps.platform_description = stripped
            else:
                ps.platform_description = None
        branding_changed = True

    if "platform_login_message" in body:
        raw_msg = body["platform_login_message"]
        if raw_msg is None:
            ps.platform_login_message = None
        else:
            raw_msg = _require_branding_string("platform_login_message", raw_msg)
            stripped = raw_msg.strip()
            if stripped:
                _validate_text_length("platform_login_message", stripped)
                ps.platform_login_message = stripped
            else:
                ps.platform_login_message = None
        branding_changed = True

    for field in _BRANDING_COLOR_FIELDS:
        if field not in body:
            continue
        raw = body[field]
        value = (raw or None) if isinstance(raw, str) else raw
        if value is not None:
            _validate_hex_color(field, value)
        setattr(ps, field, value)
        branding_changed = True

    if "platform_language" in body:
        raw_lang = body["platform_language"]
        # None / "" reset to automatic browser detection.
        lang = normalize_language(raw_lang) if raw_lang not in (None, "") else "auto"
        if lang is None:
            raise HTTPException(
                status_code=400,
                detail="platform_language: unsupported language",
            )
        # Deliberately NOT flagged as a branding change: the language is not
        # part of the branding snapshot (name/logo/colors/OG), so it must not
        # trigger the branding-cache refresh or the index.html re-patch. The
        # request-scoped session commit persists it like any other setting,
        # and the frontend re-reads it via /platform/config.
        ps.platform_language = lang

    # Platform-wide kill switch for the per-tool approval gate and the
    # install default agents follow unless they set their own. Strict
    # booleans like ``ai_enabled`` below: a truthy string must not switch off
    # the one control that pauses destructive tools for a human. Validated
    # and applied by the helper the admin tool ``update_platform_ai_settings``
    # shares; each flip gets its own legible audit entry, since it changes the
    # security posture for every user.
    try:
        approval_changes = apply_tool_approval_settings(ps, body)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    for change in approval_changes:
        await log_audit(
            db,
            user=user,
            action="toggle",
            entity_type="platform_settings",
            entity_name=TOOL_APPROVALS_AUDIT_ENTITY,
            summary=change.summary,
            details={change.field: change.value},
            request=request,
        )

    # Agent runs: the ``wait`` policy. Clamped and validated by the
    # same helper as the admin tool; an env-pinned field is read-only (403).
    try:
        wait_changes = apply_wait_settings(ps, body)
    except WaitSettingsLockedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if wait_changes:
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="platform_settings",
            entity_name="Agent runs",
            summary="Changed the agent wait policy",
            details=wait_changes,
            request=request,
        )

    if "default_presentation_template_id" in body:
        # The container ``generate_presentation`` styles a deck with when the
        # agent names none. ``null`` / ``""`` reverts to the built-in
        # brand deck. An explicit choice must be a COMPANY-MANAGED presentation
        # container: the default is applied for every user, and a private one
        # would be invisible to everyone but its owner, whose decks would then
        # silently come out unthemed. Not a branding change — the deck theme is
        # not part of the branding snapshot.
        raw_template = body["default_presentation_template_id"]
        if raw_template in (None, ""):
            ps.default_presentation_template_id = None
        else:
            from app.core.utils import is_valid_uuid
            from app.models.template_container import TemplateContainer

            if not isinstance(raw_template, str) or not is_valid_uuid(raw_template):
                raise HTTPException(
                    status_code=400,
                    detail="default_presentation_template_id: must be a template container UUID",
                )
            template_row = (
                await db.execute(
                    select(TemplateContainer).where(
                        TemplateContainer.id == uuid.UUID(raw_template)
                    )
                )
            ).scalar_one_or_none()
            if template_row is None:
                raise HTTPException(
                    status_code=404,
                    detail="default_presentation_template_id: template container not found",
                )
            if (template_row.kind or "presentation") != "presentation":
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "default_presentation_template_id: the container must be a "
                        f"presentation template (kind is '{template_row.kind}')"
                    ),
                )
            if not template_row.is_company_managed:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "default_presentation_template_id: the platform default must be a "
                        "company-managed template so every user's decks can use it"
                    ),
                )
            ps.default_presentation_template_id = template_row.id

    license_changed = False
    if "enterprise_license" in body:
        from app.enterprise.licensing import (
            LicenseValidationError,
            clear_license_cache,
            validate_enterprise_license_for_install,
        )
        from app.enterprise.xtm_ca import XTM_CA_PEM

        new_pem = body["enterprise_license"] or None
        if new_pem:
            try:
                validate_enterprise_license_for_install(
                    new_pem,
                    ca_pem=XTM_CA_PEM,
                    platform_created_at=ps.created_at,
                )
            except LicenseValidationError as exc:
                raise HTTPException(status_code=400, detail=exc.message) from exc
        ps.enterprise_license = new_pem
        license_changed = True
        clear_license_cache("copilot")
        clear_license_cache("xtm")

    # The runtime AI toggle (FR-012/FR-023/FR-024). Three distinct
    # operations: ``true`` and ``false`` pin the platform, ``null`` clears the
    # override so it follows ``PLATFORM_MODE`` again. Handled here rather than
    # on its own endpoint so the existing administrator authorisation on this
    # route covers it without inventing a permission.
    ai_turned_on = False
    ai_state_changed = False
    if "ai_enabled" in body:
        requested = body["ai_enabled"]
        if not isinstance(requested, bool) and requested is not None:
            # Not coerced: a truthy string here would switch AI on for a client
            # that meant the opposite, on the one setting where being wrong
            # runs models on a deployment that forbids them.
            raise HTTPException(
                status_code=400,
                detail="ai_enabled: must be true, false or null",
            )
        previous = await ai_state.set_ai_enabled(db, requested)
        ai_state_changed = previous != requested
        ai_turned_on = ai_state.resolve(requested) and not ai_state.resolve(previous)
        await log_audit(
            db,
            user=user,
            action="toggle",
            entity_type="general_assistant",
            entity_name="Platform Settings",
            summary=(
                "AI enabled"
                if requested is True
                else "AI disabled"
                if requested is False
                else "AI set to follow the deployment mode"
            ),
            # Logged even when the value did not change, so both attempts of a
            # concurrent change stay attributable although only one survives
            # (FR-026).
            details={
                "setting": "ai_enabled",
                "from": previous,
                "to": requested,
                "resolved": ai_state.resolve(requested),
            },
            request=request,
        )

    await db.flush()

    summary = "Updated platform parameters"
    if license_changed:
        action = "installed" if ps.enterprise_license else "removed"
        summary = f"Enterprise license {action}"

    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="general_assistant",
        entity_name="Platform Settings",
        summary=summary,
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the
    # response is sent. Every cache below is refreshed or dropped after this
    # commit, never from a state that could still be rolled back.
    await db.commit()

    if branding_changed:
        from app.services.branding import (
            refresh_branding_cache,
            refresh_configured_branded_index_html,
        )

        await refresh_branding_cache(db)
        refresh_openapi_branding = getattr(
            request.app.state,
            "refresh_openapi_branding",
            None,
        )
        if callable(refresh_openapi_branding):
            refresh_openapi_branding()
        # Re-patch the cached frontend index.html so link-preview
        # crawlers (no JS) see the new title/description/OG fields
        # without requiring an app restart.
        with suppress(Exception):
            # Patching is best-effort — failure must not break the
            # admin save flow after the DB commit has succeeded.
            refresh_configured_branded_index_html()

    if license_changed:
        # The edition may have changed: same commit-then-invalidate order for
        # the background gate's cached licence answer.
        from app.services import agentic_gate

        agentic_gate.invalidate_cache()

    if ai_state_changed:
        # After the commit above: invalidating first would let a concurrent
        # request re-cache the pre-commit value for a whole TTL.
        ai_state.invalidate_cache()

        if ai_turned_on:
            # Provision before responding, not after (FR-012). An administrator
            # told "saved" who then finds an empty platform reports it as a bug,
            # and the seeding is check-then-insert against unique-constrained
            # tables, so it needs the same lock boot takes. Providers as well as
            # content: seeded agents with no provider behind them fail every run
            # until a restart, which is the redeploy this toggle exists to avoid.
            from app.core.migrations import bootstrap_lock
            from app.services.brand_asset_source import downloaded_source
            from app.services.provisioning import (
                provision_builtin_content,
                provision_env_providers,
                wire_default_agents,
            )

            try:
                # Bounded: boot may block on this lock forever, a request may
                # not — an unbounded wait outlives the ingress timeout. The
                # brand asset source is entered first, so its download runs
                # before the lock: a slow GitHub must not hold it.
                async with downloaded_source() as brand_source, bootstrap_lock(wait_timeout=25):
                    await provision_builtin_content(brand_source=brand_source)
                    await provision_env_providers()
                    # After the agents exist: the example flow resolves one by
                    # slug, which is why boot runs it *after*
                    # ``provision_builtin_content`` too (main.py). Missing here,
                    # a platform switched on at runtime had no CTI playbook until
                    # the next restart — and ``_seed_one_flow`` is
                    # create-if-absent, so nothing ever repaired it
                    # (/code-review, 2026-08-28).
                    # Behind "FLOW_BUILDER" like the boot path: an ACTIVE
                    # example flow where the builder is hidden manufactures
                    # runs nobody can reach (an `Ask user` node parks the run
                    # in ``awaiting_human``, and the canvas, the Executions
                    # feed and the HITL bell are all gated).
                    from app.core.database import async_session
                    from app.core.feature_flags import is_feature_enabled
                    from app.services.flow_examples import seed_example_flow

                    if is_feature_enabled("FLOW_BUILDER"):
                        async with async_session() as _flow_db:
                            await seed_example_flow(_flow_db)
                            await _flow_db.commit()
                    # Users who registered while AI was off have no default
                    # agent; without this the repair waits for a restart.
                    await wire_default_agents()
            except TimeoutError:
                # Not a failure: another replica holds the lock and is seeding
                # the same content, so say "wait", not "check the logs".
                logger.warning(
                    "AI was enabled but the bootstrap lock was busy; another "
                    "replica is provisioning the built-in content"
                )
                raise HTTPException(
                    status_code=503,
                    detail={
                        "code": "ai_provisioning_busy",
                        "message": (
                            "AI is now enabled. Another instance is currently "
                            "provisioning the built-in content — reload in a "
                            "moment to see it."
                        ),
                    },
                ) from None
            except Exception:
                # The state change is already committed, so failing the whole
                # request would leave the administrator looking at a stale
                # "disabled" with no idea AI is now on. Report the half that
                # succeeded and name the half that did not.
                logger.exception(
                    "AI was enabled but provisioning failed; the platform is on "
                    "and may be missing built-in content or providers"
                )
                raise HTTPException(
                    status_code=500,
                    detail={
                        "code": "ai_provisioning_failed",
                        "message": (
                            "AI is now enabled, but provisioning the built-in "
                            "content did not complete. Reload and check the "
                            "server logs."
                        ),
                    },
                ) from None

    return _serialize_platform_settings(ps, app_settings)


# ── Quota Management ─────────────────────────────────────────────────────


@router.get("/quotas")
async def get_platform_quotas(
    _user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Get platform-level quota configuration (admin only).

    In XTM One mode, also returns `agentic_source` so the frontend
    knows whether the agentic quota is license-piloted (read-only).
    """
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )
    from app.core.config import settings as app_settings
    from app.services.quota import legacy_quota_notices, normalize_quota_config

    ps = await _load_ps(db)

    agentic_source = "global"
    if app_settings.is_xtm_one:
        from app.services.quota import _resolve_xtm_agentic_config

        xtm_cfg = await _resolve_xtm_agentic_config(db)
        if xtm_cfg:
            agentic_source = "xtm_license"

    return {
        # A ``platform_limit`` stored as 0 means "no platform
        # limit"; it is reported as null, the one spelling of uncapped.
        "quota_config": normalize_quota_config(ps.quota_config),
        "agentic_source": agentic_source,
        "notices": await legacy_quota_notices(db, ps.quota_config, tier="platform"),
    }


@router.put("/quotas")
async def update_platform_quotas(
    body: QuotaConfig,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
    after_commit: AfterCommit = Depends(run_after_commit, scope="function"),
):
    """Update platform-level quota configuration (admin only)."""
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )
    from app.services.quota import invalidate_quota_cache, normalize_quota_config

    ps = await _load_ps(db)

    ps.quota_config = normalize_quota_config(
        body.model_dump(exclude_none=False), for_write=True
    )
    await db.flush()

    # Once the change has committed: dropped before, a quota check landing in
    # between re-caches the old config for the cache's whole TTL.
    after_commit.add(invalidate_quota_cache)

    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="general_assistant",
        entity_name="Quotas",
        summary="Updated platform quota configuration",
        request=request,
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()

    return {"quota_config": ps.quota_config}


@router.get("/my-quotas")
async def get_my_quotas(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get the authenticated user's resolved quota config and current usage."""
    from app.core.redis import redis_client
    from app.services.quota import (
        get_agentic_quota_periods,
        get_user_quota_usage,
        resolve_full_quota_info,
    )

    redis = await redis_client()
    resolved, sources = await resolve_full_quota_info(db, redis, user.id)
    usage = await get_user_quota_usage(db, user.id)
    periods = await get_agentic_quota_periods(db, user.id)

    return UserQuotaInfoResponse(
        resolved_config=QuotaConfig(**resolved) if resolved else None,
        usage=[QuotaUsageEntry(**e) for e in usage],
        sources=sources,
        periods=[QuotaPeriodUsage(**p) for p in periods],
    )


@router.get("/quotas/monitoring")
async def get_quotas_monitoring(
    _user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Platform-wide quota monitoring dashboard data (admin only)."""
    from app.services.quota import get_platform_quota_monitoring

    return await get_platform_quota_monitoring(db)


@router.get("/quotas/agent-failure-pool")
async def get_agent_failure_pool(
    _user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Agent failure pool state: window, per-agent consumption, code history.

    Admin only, and read-only: the pool's SIZE is an environment setting, not
    something a tenant administrator can raise from the UI. What this exposes
    is where the allowance has gone, when it renews, and the ledger of support
    codes already redeemed.
    """
    from app.services.agent_failure_pool import get_pool_status

    return await get_pool_status(db)


@router.post("/quotas/agent-failure-pool/redeem")
async def redeem_agent_failure_pool_code(
    body: SupportCodeRedeemRequest,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Redeem a Filigran support code to recharge every agent failure pool.

    Refusals (malformed, unsigned, expired, wrong platform, already used) come
    back as a 400 whose ``detail`` is written for the admin reading it — the
    same contract the license activation form relies on to render the reason
    inline.
    """
    from app.services.agent_failure_pool import (
        SupportCodeError,
        redeem_pool_recharge_code,
    )
    from app.services.quota import QuotaClearBusyError

    try:
        result = await redeem_pool_recharge_code(db, body.code, user.id)
    except SupportCodeError as exc:
        # A refused code is a user-input problem, not an incident: log at INFO
        # (someone mistyped) but keep the code itself out of the logs.
        logger.info("Support code refused for user %s: %s", user.id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except QuotaClearBusyError as exc:
        # Refused before anything was cleared, so the code is not burned.
        raise HTTPException(
            status_code=503,
            detail={"code": "quota_flush_busy", "message": str(exc)},
            headers={"Retry-After": "5"},
        ) from exc

    await log_audit(
        db,
        user=user,
        action="configure",
        entity_type="general_assistant",
        entity_name="Agent failure pool",
        summary=(
            "Recharged all agent failure pools with support code "
            f"{result['code_id']}"
        ),
        request=request,
    )
    await db.commit()

    return result


@router.post("/quotas/support-codes/redeem")
async def redeem_quota_support_code(
    body: SupportCodeRedeemRequest,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Redeem a Filigran support code of any kind this platform applies.

    An ``agent_failure_pool_recharge`` code recharges every agent failure pool
    (what ``/quotas/agent-failure-pool/redeem`` does); a
    ``platform_quota_reset`` code resets the platform-wide usage counters of
    the current windows, the counters the license ceilings are
    enforced against. The answer carries the code's ``kind`` next to what was
    applied.

    The audit row is written before the grant and committed with it, so a
    refused or failed redemption leaves none. Refusals come back as a 400
    whose ``detail`` is written for the admin, a flush holding the counters as
    a 503 with nothing changed and the code unused.
    """
    from app.services.agent_failure_pool import SupportCodeError
    from app.services.quota import QuotaClearBusyError
    from app.services.support_codes import (
        apply_support_code,
        describe_grant,
        verify_redeemable_code,
    )

    try:
        verified = verify_redeemable_code(body.code)
        await log_audit(
            db,
            user=user,
            action="configure",
            entity_type="platform_settings",
            entity_name="Quotas",
            summary=describe_grant(verified),
            details={
                "support_code_id": verified.code_id,
                "support_code_kind": verified.kind,
            },
            request=request,
        )
        result = await apply_support_code(db, verified, user.id)
    except SupportCodeError as exc:
        # A refused code is a user-input problem, not an incident: log at INFO
        # (someone mistyped) but keep the code itself out of the logs.
        logger.info("Support code refused for user %s: %s", user.id, exc)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except QuotaClearBusyError as exc:
        # Refused before anything was cleared, so the code is not burned.
        raise HTTPException(
            status_code=503,
            detail={"code": "quota_flush_busy", "message": str(exc)},
            headers={"Retry-After": "5"},
        ) from exc
    # ``apply_support_code`` committed the grant with its audit row.
    return result


# ── Built-in Tool Catalog ────────────────────────────────────────────────────


@router.get("/builtin-tools/catalog")
async def get_builtin_tool_catalog(
    _user: User = Depends(get_current_user),
):
    """Return sub-tool definitions for every built-in tool.

    ``flow_builder`` is withheld while the "FLOW_BUILDER" feature flag is off:
    its thirteen sub-tools name and describe an unannounced feature, and this is
    the endpoint an integrator reads. Filtered here rather than in
    ``build_builtin_tool_catalog`` because that function doubles as the
    platform's registry of valid built-in ids — skill-package validation reads
    its keys, so filtering there rejected packages that legitimately reference
    the tool (the flag is UI-only and must not break an existing agent).

    ``deep_investigation`` is withheld the same way while "DEEP_INVESTIGATION"
    is off, ``video_studio`` while "VIDEO_STUDIO" is off and ``phone`` while
    "MOBILE_COMPANION" is off, and every ``gate="health"`` group while
    Platform Health is not available; they stay registry keys for the same reason, and the
    flags also withhold the tool at run time. The evaluation tools leave the ``agent_builder``
    alias the same way while evaluation is off.
    """
    return _offered_builtin_tool_catalog()


def _offered_builtin_tool_catalog() -> dict[str, list[dict]]:
    """The registry less what this deployment withholds (see ``get_builtin_tool_catalog``)."""
    from app.core.feature_flags import is_feature_enabled

    catalog = withhold_flow_builder_tools(build_builtin_tool_catalog())
    if not is_feature_enabled("DEEP_INVESTIGATION"):
        catalog.pop("deep_investigation", None)
    if not is_feature_enabled("MOBILE_COMPANION"):
        catalog.pop("phone", None)
    if not is_feature_enabled("DASHBOARD_STUDIO"):
        catalog.pop("custom_dashboards", None)
    if not is_feature_enabled("VIDEO_STUDIO"):
        catalog.pop("video_studio", None)
    withhold_health_tools(catalog)
    return withhold_evaluation_tools(catalog)


@router.get("/builtin-tools/groups")
async def get_builtin_tool_groups(
    _user: User = Depends(get_current_user),
):
    """The built-in tool groups every picker and the Tools page render.

    Every group of ``app.services.builtin_tool_groups``, in display order, with
    the function names the catalog endpoint offers, their count and their
    schema size in characters. A group withheld by a flag or a disabled
    subsystem keeps its card (an agent may carry it) with no function and
    ``offered`` false.
    """
    from app.services.builtin_tool_groups import builtin_tool_groups_payload

    return builtin_tool_groups_payload(_offered_builtin_tool_catalog())
