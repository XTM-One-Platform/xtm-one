# SPDX-License-Identifier: Apache-2.0

"""LM Studio console - the pages of an LM Studio integration (``/integrations/{id}/lmstudio``).

The models on the server and their loaded instances, loading and unloading a
model, downloading one from the LM Studio catalog or Hugging Face, and the
platform LM Studio LLM provider this integration can feed. Every call to the
server follows the integration's route: through its relay group when it has
one, directly from the platform otherwise.

Who may do what:

* every route: the caller manages the integration (``helpers.can_manage``). An
  integration the caller cannot see is unknown (404); one they see and may not
  manage is a 403. Every route reaches a model server, so AI must be on
  (``require_ai_enabled``), as for the model listing in Settings;
* loading, unloading and downloading models: the holder of the server - an
  admin for a company-managed integration, the owner for a personal one. They
  change what the server holds in memory and on disk for everyone using it;
* feeding the platform LLM provider: an admin, from a company-managed
  integration (``services/lmstudio_llm_provider.py``).

LM Studio's refusals and the relay's failures come back as 424 (404 for an
unknown model, instance or download) with ``detail = {code, message}``: a
fixed sentence per kind of failure, the upstream wording staying in the log.
Never 401 or 502, which the web client reads as an expired session and a
backend outage.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin, require_ai_enabled
from app.api.v1.helpers import (
    can_manage,
    get_user_group_ids,
    get_user_integration,
    parse_path_id,
)
from app.api.v1.schemas.lmstudio import (
    DownloadJob,
    DownloadRequest,
    LinkableProvider,
    LinkProviderRequest,
    LoadRequest,
    LoadResponse,
    ModelsResponse,
    OverviewResponse,
    ProviderModelsRequest,
    ServerModel,
    UnloadRequest,
    UnloadResponse,
)
from app.api.v1.schemas.settings import LLMProviderResponse, LLMProviderUpdateRequest
from app.core.database import get_db
from app.core.egress import egress_route
from app.integrations.providers import lmstudio as client
from app.models.integration import Integration
from app.models.llm_config import LLMProviderConfig
from app.models.user import User
from app.services import integration_llm_provider as feeds
from app.services import lmstudio_llm_provider as llm_link
from app.services.audit import log_audit

logger = logging.getLogger(__name__)

router = APIRouter()

_NOT_FOUND = "Integration not found"

_STATUS_BY_KIND = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "no_base_url": status.HTTP_409_CONFLICT,
    "invalid_base_url": status.HTTP_409_CONFLICT,
}


def _upstream_failure(exc: client.LMStudioError) -> HTTPException:
    logger.info("LM Studio console: %s (%s)", exc.kind, exc.detail[:300])
    return HTTPException(
        status_code=_STATUS_BY_KIND.get(exc.kind, status.HTTP_424_FAILED_DEPENDENCY),
        detail={"code": f"lmstudio_{exc.kind}", "message": exc.public_message},
    )


async def _load(db: AsyncSession, user: User, integration_id: str) -> Integration:
    """The LM Studio integration ``integration_id``, for a caller who manages it."""
    group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(
        db, user.id, integration_id, group_ids, is_admin=user.is_admin
    )
    if integration.provider != client.PROVIDER:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    if not can_manage(user, integration, group_ids):
        raise HTTPException(
            status_code=403,
            detail="Only the people who manage this integration can open its LM Studio console.",
        )
    return integration


def _is_server_holder(user: User, integration: Integration) -> bool:
    if integration.is_company_managed:
        return user.is_admin
    return user.is_admin or integration.user_id == user.id


def _require_server_holder(user: User, integration: Integration) -> None:
    if not _is_server_holder(user, integration):
        raise HTTPException(
            status_code=403,
            detail=(
                "Only the holder of this LM Studio server can load, unload or download models: "
                "an admin for a company-managed integration, its owner otherwise."
            ),
        )


async def _connection(integration: Integration) -> client.Connection:
    """The server's connection; a 409 naming what is missing (the server URL)."""
    try:
        return await client.integration_connection(integration)
    except client.LMStudioError as exc:
        raise _upstream_failure(exc) from exc


async def _call(db: AsyncSession, integration: Integration, call):
    """Run ``call(connection)`` on the integration's route (its relay group, or direct)."""
    from app.services.relays import integration_egress_route

    connection = await _connection(integration)
    route = await integration_egress_route(db, integration)
    try:
        with egress_route(route):
            return await call(connection)
    except client.LMStudioError as exc:
        raise _upstream_failure(exc) from exc


async def _audit(
    db: AsyncSession,
    *,
    user: User,
    integration: Integration,
    request: Request,
    summary: str,
    details: dict,
) -> None:
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="integration",
        entity_id=integration.id,
        entity_name=integration.name,
        summary=summary,
        details={"provider": client.PROVIDER, **details},
        request=request,
    )


def _serialize_provider(config: LLMProviderConfig, user: User) -> LLMProviderResponse:
    """The provider as Settings serves it; who it is restricted to, to admins only."""
    from app.api.v1.settings import _serialize_llm_provider_response

    return _serialize_llm_provider_response(config, with_audience=user.is_admin)


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


@router.get("/{integration_id}/lmstudio/overview", response_model=OverviewResponse)
async def get_overview(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> OverviewResponse:
    """How the server is reached, and the platform provider this integration feeds. Calls nothing."""
    integration = await _load(db, user, integration_id)
    base_url: str | None = None
    server_refusal: str | None = None
    has_token = False
    try:
        connection = await client.integration_connection(integration)
        base_url, has_token = connection.base_url, connection.token is not None
    except client.LMStudioError as exc:
        server_refusal = exc.public_message
    refusal = llm_link.link_refusal(integration)
    can_link = user.is_admin and refusal is None
    return OverviewResponse(
        routed=integration.relay_group_id is not None,
        relay_group_id=str(integration.relay_group_id) if integration.relay_group_id else None,
        base_url=base_url,
        server_refusal=server_refusal,
        has_api_token=has_token,
        can_manage_server=_is_server_holder(user, integration),
        can_link_provider=can_link,
        link_refusal=refusal,
        llm_providers=[
            _serialize_provider(c, user) for c in await feeds.linked_configs(db, integration.id)
        ],
        adoptable_providers=[
            LinkableProvider(id=str(c.id), display_name=c.display_name, default_model=c.default_model)
            for c in await feeds.adoptable_configs(db, llm_link.FEED)
        ]
        if can_link
        else [],
    )


# ---------------------------------------------------------------------------
# Models on the server
# ---------------------------------------------------------------------------


@router.get("/{integration_id}/lmstudio/models", response_model=ModelsResponse)
async def get_models(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> ModelsResponse:
    """The models on the server, each with its loaded instances and whether the platform offers it."""
    integration = await _load(db, user, integration_id)
    models = await _call(db, integration, client.list_models)
    offered: set[str] = set()
    for config in await feeds.linked_configs(db, integration.id):
        offered.update(m for m in (config.available_models or []) if isinstance(m, str))
    return ModelsResponse(
        models=[ServerModel(**m, offered=m["key"] in offered) for m in models]
    )


@router.post("/{integration_id}/lmstudio/models/load", response_model=LoadResponse)
async def load_model(
    integration_id: str,
    body: LoadRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> LoadResponse:
    """Load a model into the server's memory, with its load options."""
    integration = await _load(db, user, integration_id)
    _require_server_holder(user, integration)
    options = body.model_dump(exclude={"model"}, exclude_none=True)

    async def _do(connection: client.Connection) -> dict:
        return await client.load_model(connection, body.model, **options)

    loaded = await _call(db, integration, _do)
    await _audit(
        db,
        user=user,
        integration=integration,
        request=request,
        summary=f"Loaded model '{body.model}' on LM Studio integration '{integration.name}'",
        details={"model": body.model, "instance_id": loaded["instance_id"], **options},
    )
    await db.commit()
    return LoadResponse(**loaded)


@router.post("/{integration_id}/lmstudio/models/unload", response_model=UnloadResponse)
async def unload_model(
    integration_id: str,
    body: UnloadRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> UnloadResponse:
    """Unload one loaded instance."""
    integration = await _load(db, user, integration_id)
    _require_server_holder(user, integration)

    async def _do(connection: client.Connection) -> str:
        return await client.unload_model(connection, body.instance_id)

    instance_id = await _call(db, integration, _do)
    await _audit(
        db,
        user=user,
        integration=integration,
        request=request,
        summary=f"Unloaded model instance '{instance_id}' on LM Studio integration '{integration.name}'",
        details={"instance_id": instance_id},
    )
    await db.commit()
    return UnloadResponse(instance_id=instance_id)


@router.post(
    "/{integration_id}/lmstudio/downloads",
    response_model=DownloadJob,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_download(
    integration_id: str,
    body: DownloadRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> DownloadJob:
    """Start downloading a model onto the server; its job, to follow with the status route."""
    integration = await _load(db, user, integration_id)
    _require_server_holder(user, integration)

    async def _do(connection: client.Connection) -> dict:
        return await client.download_model(connection, body.model, quantization=body.quantization)

    job = await _call(db, integration, _do)
    await _audit(
        db,
        user=user,
        integration=integration,
        request=request,
        summary=f"Started downloading model '{body.model}' on LM Studio integration '{integration.name}'",
        details={
            "model": body.model,
            "job_id": job["job_id"],
            "status": job["status"],
            **({"quantization": body.quantization} if body.quantization else {}),
        },
    )
    await db.commit()
    return DownloadJob(**job)


@router.get("/{integration_id}/lmstudio/downloads/{job_id}", response_model=DownloadJob)
async def get_download(
    integration_id: str,
    job_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> DownloadJob:
    """Where a download stands."""
    integration = await _load(db, user, integration_id)
    if not client.is_valid_job_id(job_id):
        raise HTTPException(status_code=404, detail="Download not found")

    async def _do(connection: client.Connection) -> dict:
        return await client.download_status(connection, job_id)

    return DownloadJob(**await _call(db, integration, _do))


# ---------------------------------------------------------------------------
# Platform LLM provider (admin, company-managed integration)
# ---------------------------------------------------------------------------


def _link_failure(exc: feeds.LinkRefusedError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


async def _catalog_by_key(db: AsyncSession, integration: Integration) -> dict[str, dict]:
    """The server's models with the context each is served with.

    A loaded instance's context is the one LM Studio serves, so it is the
    window a newly offered model gets; a model not loaded gets its maximum.
    """
    models = await _call(db, integration, client.list_models)
    catalog: dict[str, dict] = {}
    for model in models:
        loaded = [i["context_length"] for i in model["loaded_instances"] if i["context_length"]]
        catalog[model["key"]] = {
            "context_length": min(loaded) if loaded else model["max_context_length"],
            "type": model["type"],
        }
    return catalog


def _refuse_unknown_models(selected: list[str], catalog: dict[str, dict], keep: set[str]) -> None:
    unknown = [m for m in selected if m not in catalog and m not in keep]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Not on this LM Studio server: {', '.join(unknown[:10])}",
        )


def _require_linkable(integration: Integration) -> None:
    refusal = llm_link.link_refusal(integration)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)


@router.post(
    "/{integration_id}/lmstudio/llm-provider",
    response_model=LLMProviderResponse,
    status_code=status.HTTP_201_CREATED,
)
async def link_provider(
    integration_id: str,
    body: LinkProviderRequest,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> LLMProviderResponse:
    """Create the platform LM Studio provider from this integration, or adopt an unlinked one."""
    integration = await _load(db, user, integration_id)
    _require_linkable(integration)
    if body.provider_config_id is not None:
        try:
            config = await feeds.adopt_provider(
                db, llm_link.FEED, integration=integration, config_id=body.provider_config_id,
                user=user, request=request,
            )
        except feeds.LinkRefusedError as exc:
            raise _link_failure(exc) from exc
        await db.commit()
        llm_link.forget_target(config_id=config.id)
        await feeds.drop_provider_cache()
        return _serialize_provider(config, user)

    catalog = await _catalog_by_key(db, integration)
    _refuse_unknown_models(body.available_models, catalog, keep=set())
    try:
        return await feeds.create_linked_provider(
            db,
            llm_link.FEED,
            integration=integration,
            user=user,
            request=request,
            display_name=body.display_name,
            available_models=body.available_models,
            default_model=body.default_model,
            context_windows=feeds.merged_context_windows(None, (), body.available_models, catalog),
        )
    except feeds.LinkRefusedError as exc:
        raise _link_failure(exc) from exc


async def _linked_config(
    db: AsyncSession, integration: Integration, config_id: str
) -> LLMProviderConfig:
    parsed: uuid.UUID = parse_path_id(config_id, not_found_detail="Provider config not found")
    config = (
        await db.execute(
            select(LLMProviderConfig).where(
                LLMProviderConfig.id == parsed,
                LLMProviderConfig.source_integration_id == integration.id,
            )
        )
    ).scalar_one_or_none()
    if config is None:
        raise HTTPException(status_code=404, detail="Provider config not found")
    return config


@router.put(
    "/{integration_id}/lmstudio/llm-provider/{config_id}/models",
    response_model=LLMProviderResponse,
)
async def update_provider_models(
    integration_id: str,
    config_id: str,
    body: ProviderModelsRequest,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> LLMProviderResponse:
    """The models the platform provider fed by this integration offers."""
    from app.api.v1.settings import update_provider_config

    integration = await _load(db, user, integration_id)
    _require_linkable(integration)
    config = await _linked_config(db, integration, config_id)
    previous = list(config.available_models or [])
    catalog = await _catalog_by_key(db, integration)
    # A model deleted from the server since it was offered may be kept, never added.
    _refuse_unknown_models(body.available_models, catalog, keep=set(previous))
    windows = feeds.merged_context_windows(
        config.context_windows, previous, body.available_models, catalog
    )
    # The default follows the selection: kept while still offered, else the
    # first model offered - never a model the provider no longer lists.
    default_model = body.default_model or (
        config.default_model
        if config.default_model in body.available_models
        else (body.available_models[0] if body.available_models else None)
    )
    update = LLMProviderUpdateRequest(
        available_models=body.available_models,
        default_model=default_model,
        **({"context_windows": windows} if windows is not None else {}),
    )
    return await update_provider_config(
        db, config_id=config.id, body=update, user=user, request=request
    )


@router.delete(
    "/{integration_id}/lmstudio/llm-provider/{config_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def unlink_provider(
    integration_id: str,
    config_id: str,
    request: Request,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> Response:
    """Stop feeding the provider from this integration: it refuses every request until fed again."""
    integration = await _load(db, user, integration_id)
    parsed = parse_path_id(config_id, not_found_detail="Provider config not found")
    try:
        await feeds.unlink_provider(
            db, integration=integration, config_id=parsed, user=user, request=request
        )
    except feeds.LinkRefusedError as exc:
        raise _link_failure(exc) from exc
    await db.commit()
    llm_link.forget_target(config_id=parsed)
    await feeds.drop_provider_cache()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
