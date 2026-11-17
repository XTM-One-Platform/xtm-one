# SPDX-License-Identifier: Apache-2.0

"""OpenRouter console - the pages of an OpenRouter integration (``/integrations/{id}/openrouter``).

What the account has spent and has left, the activity per day and model, the
model catalog with the providers serving each model, the account's API keys,
and the platform OpenRouter LLM provider this integration can feed.

Who may do what:

* every route: the caller manages the integration (``helpers.can_manage``). An
  integration the caller cannot see is unknown (404); one they see and may not
  manage is a 403. Every route calls OpenRouter, an AI provider, so AI must be
  on (``require_ai_enabled``), as for the model listing in Settings;
* creating, editing and deleting API keys: the holder of the account - an
  admin for a company-managed integration, the owner for a personal one. A
  ``config`` grantee may replace the integration's credentials but not mint
  new ones it could take away;
* feeding the platform LLM provider: an admin, from a company-managed
  integration (``services/openrouter_llm_provider.py``).

OpenRouter's refusals come back as 424 (429 when it rate limits, 404 for an
unknown model or key) with ``detail = {code, message}``: a fixed sentence per
kind of failure, OpenRouter's own wording staying in the log. Never 401 or
502, which the web client reads as an expired session and a backend outage.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin, require_ai_enabled
from app.api.v1.helpers import (
    can_manage,
    get_user_group_ids,
    get_user_integration,
    parse_path_id,
)
from app.api.v1.schemas.openrouter import (
    ActivityResponse,
    ApiKey,
    CatalogResponse,
    ConsoleError,
    CreatedKeyResponse,
    CreateKeyRequest,
    EndpointsResponse,
    KeysResponse,
    LinkableProvider,
    LinkProviderRequest,
    ModelId,
    OverviewResponse,
    ProviderModelsRequest,
    UpdateKeyRequest,
)
from app.api.v1.schemas.settings import LLMProviderResponse, LLMProviderUpdateRequest
from app.core.database import get_db
from app.core.egress import EgressRoute, egress_route
from app.integrations.providers import openrouter as client
from app.models.integration import Integration
from app.models.llm_config import LLMProviderConfig
from app.models.user import User
from app.services import openrouter_llm_provider as llm_link
from app.services.audit import log_audit
from app.services.vault import SecretUnavailableError, reveal_field, reveal_json_field

logger = logging.getLogger(__name__)

router = APIRouter()

_NOT_FOUND = "Integration not found"

_STATUS_BY_KIND = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "rate_limited": status.HTTP_429_TOO_MANY_REQUESTS,
}


def _upstream_failure(exc: client.OpenRouterError) -> HTTPException:
    logger.info("OpenRouter console: %s (%s)", exc.kind, exc.detail[:300])
    return HTTPException(
        status_code=_STATUS_BY_KIND.get(exc.kind, status.HTTP_424_FAILED_DEPENDENCY),
        detail={"code": f"openrouter_{exc.kind}", "message": exc.public_message},
    )


def _console_error(exc: client.OpenRouterError) -> ConsoleError:
    logger.info("OpenRouter console: %s (%s)", exc.kind, exc.detail[:300])
    return ConsoleError(code=f"openrouter_{exc.kind}", message=exc.public_message)


async def _load(
    db: AsyncSession, user: User, integration_id: str
) -> Integration:
    """The OpenRouter integration ``integration_id``, for a caller who manages it."""
    group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(
        db, user.id, integration_id, group_ids, is_admin=user.is_admin
    )
    if integration.provider != client.PROVIDER:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    if not can_manage(user, integration, group_ids):
        raise HTTPException(
            status_code=403,
            detail="Only the people who manage this integration can open its OpenRouter console.",
        )
    return integration


async def _route(db: AsyncSession, integration: Integration) -> EgressRoute | None:
    """The integration's relay route (``None``: direct): every OpenRouter call
    of the console carries the account's credentials, so it leaves the way the
    integration's own calls do."""
    from app.services.relays import integration_egress_route

    return await integration_egress_route(db, integration)


def _is_account_holder(user: User, integration: Integration) -> bool:
    if integration.is_company_managed:
        return user.is_admin
    return user.is_admin or integration.user_id == user.id


def _require_account_holder(user: User, integration: Integration) -> None:
    if not _is_account_holder(user, integration):
        raise HTTPException(
            status_code=403,
            detail=(
                "Only the holder of this OpenRouter account can manage its API keys: "
                "an admin for a company-managed integration, its owner otherwise."
            ),
        )


async def _api_key(integration: Integration) -> str | None:
    key = ((await reveal_field(integration, "api_key_enc")) or "").strip()
    return key or None


async def _management_key(integration: Integration) -> str | None:
    """The management key of *integration*'s extra config, a vault reference resolved (#6080)."""
    try:
        extra = await reveal_json_field(integration, "extra_config_enc") if integration.extra_config_enc else None
    except Exception:
        logger.warning("OpenRouter integration %s: extra config unreadable", integration.id)
        return None
    value = extra.get(client.MANAGEMENT_KEY_FIELD) if isinstance(extra, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


async def _require_management_key(integration: Integration) -> str:
    key = await _management_key(integration)
    if key is None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "openrouter_management_key_missing",
                "message": (
                    "This needs an OpenRouter management key. Add one in the Configuration "
                    "tab (OpenRouter > Settings > Management keys)."
                ),
            },
        )
    return key


def _serialize_provider(config: LLMProviderConfig, user: User) -> LLMProviderResponse:
    """The provider as Settings serves it; who it is restricted to, to admins only."""
    from app.api.v1.settings import _serialize_llm_provider_response

    return _serialize_llm_provider_response(config, with_audience=user.is_admin)


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------


@router.get("/{integration_id}/openrouter/overview", response_model=OverviewResponse)
async def get_overview(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> OverviewResponse:
    """The key's limit and spend, the account's credits, and the providers this integration feeds."""
    integration = await _load(db, user, integration_id)
    api_key = await _api_key(integration)
    management_key = await _management_key(integration)

    async def _key():
        return await client.get_key_info(api_key) if api_key else None

    async def _credits():
        return await client.get_credits(management_key) if management_key else None

    with egress_route(await _route(db, integration)):
        key_result, credits_result = await asyncio.gather(
            _key(), _credits(), return_exceptions=True
        )
    parts: dict = {}
    for result, field in ((key_result, "key"), (credits_result, "credits")):
        if isinstance(result, client.OpenRouterError):
            parts[f"{field}_error"] = _console_error(result)
        elif isinstance(result, BaseException):
            raise result
        elif result is not None:
            parts[field] = result

    refusal = llm_link.link_refusal(integration)
    can_link = user.is_admin and refusal is None
    return OverviewResponse(
        **parts,
        has_api_key=api_key is not None,
        has_management_key=management_key is not None,
        can_manage_keys=_is_account_holder(user, integration),
        can_link_provider=can_link,
        link_refusal=refusal,
        llm_providers=[
            _serialize_provider(c, user) for c in await llm_link.linked_configs(db, integration.id)
        ],
        adoptable_providers=[
            LinkableProvider(id=str(c.id), display_name=c.display_name, default_model=c.default_model)
            for c in await llm_link.adoptable_configs(db)
        ]
        if can_link
        else [],
    )


@router.get("/{integration_id}/openrouter/activity", response_model=ActivityResponse)
async def get_activity(
    integration_id: str,
    date: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    key_hash: str | None = Query(None, pattern=r"^[0-9a-f]{64}$"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> ActivityResponse:
    """Spend per day, model and provider over the last 30 completed UTC days (management key)."""
    integration = await _load(db, user, integration_id)
    management_key = await _require_management_key(integration)
    route = await _route(db, integration)
    try:
        with egress_route(route):
            rows = await client.get_activity(management_key, date=date, api_key_hash=key_hash)
    except client.OpenRouterError as exc:
        raise _upstream_failure(exc) from exc
    return ActivityResponse(rows=rows)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


@router.get("/{integration_id}/openrouter/models", response_model=CatalogResponse)
async def get_models(
    integration_id: str,
    kind: str = Query("chat", pattern=r"^(chat|embeddings)$"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> CatalogResponse:
    """The models the integration's key may use: text models, or embedding models."""
    integration = await _load(db, user, integration_id)
    route = await _route(db, integration)
    api_key = await _api_key(integration)
    try:
        with egress_route(route):
            models = await client.cached_models(api_key, kind=kind)
    except client.OpenRouterError as exc:
        raise _upstream_failure(exc) from exc
    return CatalogResponse(kind=kind, models=models)


@router.get("/{integration_id}/openrouter/model-endpoints", response_model=EndpointsResponse)
async def get_model_endpoints(
    integration_id: str,
    model_id: Annotated[ModelId, Query()],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> EndpointsResponse:
    """The providers serving ``model_id``, with their price, uptime and speed."""
    integration = await _load(db, user, integration_id)
    route = await _route(db, integration)
    api_key = await _api_key(integration)
    try:
        with egress_route(route):
            endpoints = await client.list_model_endpoints(api_key, model_id)
    except client.OpenRouterError as exc:
        raise _upstream_failure(exc) from exc
    return EndpointsResponse(model_id=model_id, endpoints=endpoints)


# ---------------------------------------------------------------------------
# API keys (management key)
# ---------------------------------------------------------------------------


async def _keys_in_use(db: AsyncSession, integration: Integration) -> dict[str, set[str]]:
    """key hash -> what on this platform runs on it."""
    uses: dict[str, set[str]] = {}
    own = await _api_key(integration)
    if own:
        uses.setdefault(client.key_hash(own), set()).add("integration")
    configs = (
        await db.execute(
            select(LLMProviderConfig).where(LLMProviderConfig.provider == client.PROVIDER)
        )
    ).scalars()
    for config in configs:
        try:
            key = ((await reveal_field(config, "api_key_enc")) or "").strip()
        except SecretUnavailableError:
            # A vault that does not answer now may hold a key in use: never call it unused.
            raise
        except Exception:
            # An undecryptable key runs nothing: there is nothing to protect.
            logger.warning("LLM provider config %s: API key unreadable", config.id)
            continue
        if key:
            uses.setdefault(client.key_hash(key), set()).add("llm_provider")
    return uses


async def _hold_key_uses(db: AsyncSession, integration: Integration) -> Integration:
    """Freeze which keys the platform runs on until this request commits.

    Refusing to disable or delete a key in use is one decision with the
    upstream call: without these locks, an edit replacing the integration's
    key or a provider created or adopted with that key could land between the
    check and the call. The integration row is locked first (the order its
    update route writes in: the row, then the provider configs its
    ``on_update`` hook feeds), then every provider config, exclusively
    (``lock_provider_configs``, which creations and adoptions wait on). Both
    are held across the upstream call, until the commit or the rollback.
    Returns the integration re-read under its lock.
    """
    from app.services.llm_provider_deletion import lock_provider_configs

    locked = (
        await db.execute(
            select(Integration)
            .where(Integration.id == integration.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    await lock_provider_configs(db)
    return locked


def _with_uses(key: dict, uses: dict[str, set[str]]) -> dict:
    return {**key, "in_use": sorted(uses.get(key["hash"], ()))}


@router.get("/{integration_id}/openrouter/keys", response_model=KeysResponse)
async def list_keys(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> KeysResponse:
    integration = await _load(db, user, integration_id)
    management_key = await _require_management_key(integration)
    route = await _route(db, integration)
    try:
        with egress_route(route):
            listing = await client.list_api_keys(management_key)
    except client.OpenRouterError as exc:
        raise _upstream_failure(exc) from exc
    uses = await _keys_in_use(db, integration)
    return KeysResponse(keys=[_with_uses(k, uses) for k in listing.keys], truncated=listing.truncated)


async def _audit_key(
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


@router.post(
    "/{integration_id}/openrouter/keys",
    response_model=CreatedKeyResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_key(
    integration_id: str,
    body: CreateKeyRequest,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> CreatedKeyResponse:
    """Create an API key on the account. Its secret is in this answer only."""
    integration = await _load(db, user, integration_id)
    _require_account_holder(user, integration)
    management_key = await _require_management_key(integration)
    route = await _route(db, integration)
    try:
        with egress_route(route):
            created, secret = await client.create_api_key(management_key, body.to_openrouter())
    except client.OpenRouterError as exc:
        raise _upstream_failure(exc) from exc
    await _audit_key(
        db,
        user=user,
        integration=integration,
        request=request,
        summary=f"Created OpenRouter API key '{created['name']}' from integration '{integration.name}'",
        details={
            "key_hash": created["hash"],
            "limit": created["limit"],
            "limit_reset": created["limit_reset"],
            "expires_at": created["expires_at"],
        },
    )
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return CreatedKeyResponse(key={**created, "in_use": []}, secret=secret)


@router.patch("/{integration_id}/openrouter/keys/{key_hash}", response_model=ApiKey)
async def update_key(
    integration_id: str,
    key_hash: str,
    body: UpdateKeyRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> ApiKey:
    """Rename, disable, re-limit a key. Disabling one this platform runs on is refused."""
    if not client.KEY_HASH_RE.fullmatch(key_hash):
        raise HTTPException(status_code=404, detail="API key not found")
    integration = await _load(db, user, integration_id)
    _require_account_holder(user, integration)
    management_key = await _require_management_key(integration)
    if body.disabled is True:
        integration = await _hold_key_uses(db, integration)
    uses = await _keys_in_use(db, integration)
    if body.disabled is True and uses.get(key_hash):
        raise HTTPException(status_code=409, detail=_in_use_refusal(uses[key_hash], "disable"))
    route = await _route(db, integration)
    try:
        with egress_route(route):
            updated = await client.update_api_key(management_key, key_hash, body.to_openrouter())
    except client.OpenRouterError as exc:
        raise _upstream_failure(exc) from exc
    await _audit_key(
        db,
        user=user,
        integration=integration,
        request=request,
        summary=f"Updated OpenRouter API key '{updated['name']}' from integration '{integration.name}'",
        details={"key_hash": key_hash, "changed": sorted(body.model_fields_set)},
    )
    await db.commit()
    return ApiKey(**_with_uses(updated, uses))


def _in_use_refusal(uses: set[str], verb: str) -> str:
    what = " and ".join(
        {"integration": "this integration", "llm_provider": "the platform OpenRouter LLM provider"}[u]
        for u in sorted(uses)
    )
    return (
        f"This key is the one {what} runs on: replace it there before you {verb} it here, "
        "or every call made with it fails."
    )


@router.delete(
    "/{integration_id}/openrouter/keys/{key_hash}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_key(
    integration_id: str,
    key_hash: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
) -> Response:
    if not client.KEY_HASH_RE.fullmatch(key_hash):
        raise HTTPException(status_code=404, detail="API key not found")
    integration = await _load(db, user, integration_id)
    _require_account_holder(user, integration)
    management_key = await _require_management_key(integration)
    integration = await _hold_key_uses(db, integration)
    uses = await _keys_in_use(db, integration)
    if uses.get(key_hash):
        raise HTTPException(status_code=409, detail=_in_use_refusal(uses[key_hash], "delete"))
    route = await _route(db, integration)
    try:
        with egress_route(route):
            await client.delete_api_key(management_key, key_hash)
    except client.OpenRouterError as exc:
        raise _upstream_failure(exc) from exc
    await _audit_key(
        db,
        user=user,
        integration=integration,
        request=request,
        summary=f"Deleted an OpenRouter API key from integration '{integration.name}'",
        details={"key_hash": key_hash},
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Platform LLM provider (admin, company-managed integration)
# ---------------------------------------------------------------------------


def _link_failure(exc: llm_link.LinkRefusedError) -> HTTPException:
    return HTTPException(status_code=exc.status, detail=exc.message)


async def _catalog_by_id(db: AsyncSession, integration: Integration) -> dict[str, dict]:
    """Chat and embedding models together: a provider config offers both."""
    api_key = await _api_key(integration)
    try:
        with egress_route(await _route(db, integration)):
            chat, embeddings = await asyncio.gather(
                client.cached_models(api_key, kind="chat"),
                client.cached_models(api_key, kind="embeddings"),
            )
    except client.OpenRouterError as exc:
        raise _upstream_failure(exc) from exc
    return {m["id"]: m for m in [*chat, *embeddings]}


def _refuse_unknown_models(selected: list[str], catalog: dict[str, dict], keep: set[str]) -> None:
    unknown = [m for m in selected if m not in catalog and m not in keep]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Not in this integration's OpenRouter catalog: {', '.join(unknown[:10])}",
        )


@router.post(
    "/{integration_id}/openrouter/llm-provider",
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
    """Create the platform OpenRouter provider from this integration, or adopt one."""
    integration = await _load(db, user, integration_id)
    refusal = llm_link.link_refusal(integration)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)
    if body.provider_config_id is not None:
        try:
            config = await llm_link.adopt_provider(
                db, integration=integration, config_id=body.provider_config_id,
                user=user, request=request,
            )
        except llm_link.LinkRefusedError as exc:
            raise _link_failure(exc) from exc
        await db.commit()
        from app.llm.router import invalidate_provider_cache

        invalidate_provider_cache()
        return _serialize_provider(config, user)

    catalog = await _catalog_by_id(db, integration)
    _refuse_unknown_models(body.available_models, catalog, keep=set())
    try:
        return await llm_link.create_linked_provider(
            db,
            integration=integration,
            user=user,
            request=request,
            display_name=body.display_name,
            available_models=body.available_models,
            default_model=body.default_model,
            context_windows=llm_link.merged_context_windows(
                None, (), body.available_models, catalog
            ),
        )
    except llm_link.LinkRefusedError as exc:
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
    "/{integration_id}/openrouter/llm-provider/{config_id}/models",
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
    refusal = llm_link.link_refusal(integration)
    if refusal is not None:
        raise HTTPException(status_code=409, detail=refusal)
    config = await _linked_config(db, integration, config_id)
    previous = list(config.available_models or [])
    catalog = await _catalog_by_id(db, integration)
    # A model OpenRouter retired since it was enabled may be kept, never added.
    _refuse_unknown_models(body.available_models, catalog, keep=set(previous))
    windows = llm_link.merged_context_windows(
        config.context_windows, previous, body.available_models, catalog
    )
    # context_windows is sent only when it changes: an update that names it
    # replaces the whole map.
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
    "/{integration_id}/openrouter/llm-provider/{config_id}",
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
    """Stop feeding the provider from this integration; it keeps its key and models."""
    integration = await _load(db, user, integration_id)
    parsed = parse_path_id(config_id, not_found_detail="Provider config not found")
    try:
        await llm_link.unlink_provider(
            db, integration=integration, config_id=parsed, user=user, request=request
        )
    except llm_link.LinkRefusedError as exc:
        raise _link_failure(exc) from exc
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
