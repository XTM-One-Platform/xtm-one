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

"""Nova prompt-safety rules API (#nova) — CRUD for ``.nov`` detection rules.

Any authenticated user can author personal rules; company-managed rules apply
platform-wide and only the pre-LLM guard loads them, so only admins may create
or edit those. ``rule_text`` is validated with the Nova-compatible parser on
every write, and the ``/validate`` endpoint gives the builder live feedback
without persisting.

Three-tier visibility (own / group-shared / company-managed) mirrors
``prompts.py`` exactly, reusing the shared visibility/permission helpers.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_current_user,
    require_agentic_ai,
    require_agentic_ai_licensed,
)
from app.api.v1.helpers import (
    apply_group_share_fields,
    apply_owner_transfer,
    get_manageable_entity,
    get_user_group_ids,
    handle_company_managed_toggle,
    parse_path_id,
    require_admin_for_company_managed,
    resolve_owner_names,
    user_visible_query,
)
from app.api.v1.schemas.nova_rule import (
    NovaRuleCreate,
    NovaRuleResponse,
    NovaRuleUpdate,
    NovaRuleValidateRequest,
    NovaRuleValidateResponse,
    valid_actions,
)
from app.core.database import get_db
from app.models.nova_rule import NovaRule
from app.models.user import User
from app.services.audit import log_audit
from app.services.nova_guard import NovaRuleError, ParsedRule, has_semantic_patterns, parse_rule

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# Tags starting with this prefix are written by the rule sync to mark which
# source owns a rule (it prunes by them). A client must not be able to forge or
# drop one: forging would let a hand-made rule be deleted when Nova
# disconnects, dropping one would strand a synced rule as un-prunable.
_SYNC_TAG_PREFIX = "nova-source:"


def _client_tags(tags: list[str] | None) -> list[str]:
    """Client-supplied tags with the reserved sync tags stripped out."""
    return [
        t
        for t in (tags or [])
        if isinstance(t, str) and not t.startswith(_SYNC_TAG_PREFIX)
    ]


def _owned_sync_tags(rule: NovaRule) -> list[str]:
    """The sync-ownership tags already on *rule* (preserved across edits)."""
    return [
        t for t in (rule.tags or []) if isinstance(t, str) and t.startswith(_SYNC_TAG_PREFIX)
    ]


def _to_response(rule: NovaRule, *, owner_name: str | None = None) -> NovaRuleResponse:
    return NovaRuleResponse(
        id=str(rule.id),
        user_id=str(rule.user_id) if rule.user_id else None,
        owner_name=owner_name,
        name=rule.name,
        description=rule.description or "",
        category=rule.category or "",
        action=rule.action,
        rule_text=rule.rule_text or "",
        enabled=rule.enabled,
        tags=rule.tags or [],
        is_company_managed=rule.is_company_managed,
        shared_group_ids=rule.shared_group_ids,
        shared_group_permissions=rule.shared_group_permissions,
        created_at=rule.created_at.isoformat(),
        updated_at=rule.updated_at.isoformat(),
        has_semantics=has_semantic_patterns(rule.rule_text),
    )


def _tiers_of(parsed: ParsedRule) -> list[str]:
    """Rule tiers present in the source, in evaluation order. 'semantics'/'llm'
    are parsed but inert until the optional Nova engine is installed."""
    tiers: list[str] = []
    if parsed.keywords:
        tiers.append("keywords")
    for tier in ("semantics", "llm"):
        if any(v.startswith(f"{tier}.") for v in parsed.inert_vars):
            tiers.append(tier)
    tiers.append("condition")  # always required by the parser
    return tiers


def _validate_rule_text(rule_text: str) -> ParsedRule:
    """Parse ``rule_text`` or raise HTTP 422 with the parser's message."""
    try:
        return parse_rule(rule_text)
    except NovaRuleError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid Nova rule: {exc}") from exc


def _check_action(action: str) -> None:
    if action not in valid_actions():
        raise HTTPException(
            status_code=422,
            detail=f"action must be one of {list(valid_actions())}",
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[NovaRuleResponse])
async def list_nova_rules(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
):
    """List Nova rules visible to the caller (own + group-shared + company)."""
    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        user_visible_query(NovaRule, user.id, user_group_ids, is_admin=user.is_admin)
    )
    rules = list(result.scalars().all())
    owner_names = await resolve_owner_names(db, rules)
    return [
        _to_response(
            r, owner_name=owner_names.get(str(r.user_id)) if r.user_id else None
        )
        for r in rules
    ]


@router.post(
    "/validate",
    response_model=NovaRuleValidateResponse,
)
async def validate_nova_rule(
    body: NovaRuleValidateRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Dry-parse a ``.nov`` rule for live builder feedback (no write)."""
    try:
        parsed = parse_rule(body.rule_text)
    except NovaRuleError as exc:
        return NovaRuleValidateResponse(valid=False, error=str(exc))
    return NovaRuleValidateResponse(
        valid=True,
        name=parsed.name,
        category=parsed.category or None,
        keyword_count=len(parsed.keywords),
        tiers=_tiers_of(parsed),
    )


@router.post(
    "", response_model=NovaRuleResponse, status_code=status.HTTP_201_CREATED
)
async def create_nova_rule(
    body: NovaRuleCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Create a Nova rule (admins only for company-managed)."""
    require_admin_for_company_managed(
        body, user, "Only admins can create company-managed Nova rules"
    )
    _check_action(body.action)
    parsed = _validate_rule_text(body.rule_text)
    # Default the category from the rule's meta when the caller left it blank,
    # so a rule and its listing label never silently disagree.
    category = body.category or parsed.category or ""

    rule = NovaRule(
        user_id=user.id,
        name=body.name,
        description=body.description or "",
        category=category,
        action=body.action,
        rule_text=body.rule_text,
        enabled=body.enabled,
        tags=_client_tags(body.tags),
        is_company_managed=body.is_company_managed,
    )
    await apply_group_share_fields(db, rule, body, partial=False)
    db.add(rule)
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="nova_rule",
        entity_id=rule.id,
        entity_name=rule.name,
        summary=f"Created Nova rule '{rule.name}'",
        details={
            "action": rule.action,
            "category": rule.category,
            "enabled": rule.enabled,
            "is_company_managed": rule.is_company_managed,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(rule)
    return _to_response(rule, owner_name=user.full_name)


@router.get("/engine-status")
async def nova_engine_status(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
):
    """Which rule tiers can actually fire, and on which models.

    The keyword tier is always live. The semantics/llm tiers need the Nova
    engine installed; they then run on the platform's own embedding and LLM
    providers (see ``services/nova_engine``).
    """
    from app.services import nova_engine
    from app.services.nova_calibration import calibration_status

    engine = nova_engine.NOVA_HUNTING_AVAILABLE
    models = await nova_engine.resolve_nova_models(db)
    scoring = await nova_engine.semantic_scoring(db, models)
    return {
        "engine_available": engine,
        "tiers": ["keywords"] + (["semantics", "llm"] if engine else []),
        "llm_model": models.llm_model,
        "semantic_model": models.semantic_model,
        # Whether the semantics: thresholds of the rules mean what they say on
        # the model that computes the scores, and the Nova override that is not
        # that model when the tier fell back to the platform one.
        "semantic_calibration": {
            **calibration_status(scoring.model, scoring.dimensions),
            "override_ignored": scoring.override_ignored,
            "override_reason": scoring.override_reason,
        },
    }


_SEMANTIC_PROBE_TEXT = "Nova semantic model check"


async def _semantic_override_provider(db: AsyncSession, model: str) -> str:
    """The platform embedding provider configuration that serves *model*, or a 422.

    The semantics: tier embeds with the platform embedding provider, while the
    picker lists the embedding models of every configured provider: one of
    another provider would return no embedding. One embedding call proves it;
    the configuration id is kept with the override, which is not used once
    the platform embedding provider changes.
    """
    from app.core.utils import format_exception_message, is_zero_norm_vector
    from app.llm.router import EmbeddingConfigurationError, get_embedding_provider

    try:
        provider, _default_model, provider_id = await get_embedding_provider(db)
    except EmbeddingConfigurationError as exc:
        raise HTTPException(
            status_code=422,
            detail="No embedding provider is configured: the semantics: tier has nothing to score with.",
        ) from exc
    # The provider is called directly: a failure (timeout, rate limit, outage)
    # is not the model's fault and must not be answered as an invalid choice.
    try:
        vector = await provider.embed(_SEMANTIC_PROBE_TEXT, model=model)
    except Exception as exc:
        logger.warning(
            "Nova semantic model check of %r failed on the platform embedding provider: %s",
            model,
            format_exception_message(exc),
        )
        raise HTTPException(
            status_code=502,
            detail="The platform embedding provider could not be reached to check this model: try again.",
        ) from exc
    if not vector or is_zero_norm_vector(vector):
        raise HTTPException(
            status_code=422,
            detail=(
                f"The platform embedding provider returned no embedding for `{model}`: pick one of its "
                "models, or the platform embedding model."
            ),
        )
    return str(provider_id or "")


@router.put("/models")
async def update_nova_models(
    body: dict,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Set the models the semantics:/llm: tiers use (admin).

    Stored on the Nova integration itself — it owns its configuration — with
    ``None``/empty meaning "use the platform default".
    """
    import json as _json

    from sqlalchemy import select

    from app.core.security import decrypt_json, encrypt_value
    from app.models.integration import Integration

    _require_admin(user)
    row = (
        await db.execute(
            # Must target the SAME row resolve_nova_models() reads, or the
            # admin's choice is written somewhere evaluation never looks.
            select(Integration)
            .where(
                Integration.provider == "nova",
                Integration.status == "connected",
                # access_restricted: bypass - admin-only route configuring the platform-wide Nova row.
                Integration.is_company_managed.is_(True),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="No connected company-managed Nova integration found",
        )
    extra = decrypt_json(row.extra_config_enc) or {}
    if not isinstance(extra, dict):
        extra = {}
    if "nova_semantic_model" in body:
        semantic = (body.get("nova_semantic_model") or "").strip()
        if semantic:
            extra["nova_semantic_provider_id"] = await _semantic_override_provider(db, semantic)
        else:
            extra.pop("nova_semantic_provider_id", None)
    for key in ("nova_llm_model", "nova_semantic_model"):
        if key in body:
            value = (body.get(key) or "").strip()
            if value:
                extra[key] = value
            else:
                extra.pop(key, None)
    row.extra_config_enc = encrypt_value(_json.dumps(extra))
    await db.commit()
    if "nova_semantic_model" in body:
        from app.services.nova_engine import clear_override_unusable_marker

        # Fail-open: with Redis unreachable a stale marker lasts its TTL (one hour) at most.
        await clear_override_unusable_marker()
    return {
        "nova_llm_model": extra.get("nova_llm_model"),
        "nova_semantic_model": extra.get("nova_semantic_model"),
    }


@router.get("/sources")
async def list_nova_sources(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
):
    """List the Nova rule-sync sources (admin)."""
    _require_admin(user)
    from sqlalchemy import select

    from app.models.nova_rule_source import NovaRuleSource

    rows = (await db.execute(select(NovaRuleSource))).scalars().all()
    return [_source_to_dict(s, can_manage=user.is_admin) for s in rows]


@router.put("/sources/{source_id}")
async def update_nova_source(
    source_id: str,
    body: dict,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Update a source's sync interval / enabled flag (admin)."""
    _require_admin(user)
    from app.models.nova_rule_source import NOVA_SYNC_INTERVALS, NovaRuleSource

    source = await db.get(NovaRuleSource, parse_path_id(source_id, not_found_detail="Source not found"))
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    interval = body.get("sync_interval")
    if interval is not None:
        if interval not in NOVA_SYNC_INTERVALS:
            raise HTTPException(
                status_code=422,
                detail=f"sync_interval must be one of {list(NOVA_SYNC_INTERVALS)}",
            )
        source.sync_interval = interval
    if body.get("enabled") is not None:
        source.enabled = bool(body["enabled"])
    await db.commit()
    await db.refresh(source)
    return _source_to_dict(source, can_manage=user.is_admin)


@router.post("/sources/{source_id}/sync")
async def sync_nova_source_now(
    source_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Run a source's sync immediately (admin). Returns the rule count."""
    _require_admin(user)
    from datetime import UTC, datetime

    from app.models.nova_rule_source import NovaRuleSource
    from app.services.nova_rule_sync import sync_nova_rule_source

    source = await db.get(NovaRuleSource, parse_path_id(source_id, not_found_detail="Source not found"))
    if source is None:
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        count = await sync_nova_rule_source(db, source)
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=502, detail=f"Sync failed: {exc}") from exc
    source.last_synced_at = datetime.now(UTC)
    source.last_sync_status = "ok"
    # last_sync_error was written by the sync: what it kept from before, or None.
    source.last_sync_rules = count
    await db.commit()
    return {"synced_rules": count}


@router.get("/{rule_id}", response_model=NovaRuleResponse)
async def get_nova_rule(
    rule_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
):
    """Get a single Nova rule (own, group-shared, company-managed, or admin)."""
    rule = await get_manageable_entity(
        db, NovaRule, user, rule_id, action="view", entity_label="Nova rule"
    )
    owner_names = await resolve_owner_names(db, [rule])
    return _to_response(
        rule, owner_name=owner_names.get(str(rule.user_id)) if rule.user_id else None
    )


@router.put("/{rule_id}", response_model=NovaRuleResponse)
async def update_nova_rule(
    rule_id: str,
    body: NovaRuleUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Update a Nova rule (creator or admin only)."""
    rule = await get_manageable_entity(
        db, NovaRule, user, rule_id, action="edit", entity_label="Nova rule"
    )

    if body.name is not None:
        rule.name = body.name
    if body.description is not None:
        rule.description = body.description
    if body.category is not None:
        rule.category = body.category
    if body.action is not None:
        _check_action(body.action)
        rule.action = body.action
    if body.rule_text is not None:
        parsed = _validate_rule_text(body.rule_text)
        rule.rule_text = body.rule_text
        # Keep the mirrored category coherent when the source's meta changed and
        # the caller did not set one explicitly this request.
        if body.category is None and parsed.category:
            rule.category = parsed.category
    if body.enabled is not None:
        rule.enabled = body.enabled
    if body.tags is not None:
        rule.tags = _owned_sync_tags(rule) + _client_tags(body.tags)
    await apply_group_share_fields(db, rule, body, partial=True)

    await handle_company_managed_toggle(
        db,
        user,
        rule,
        body.is_company_managed,
        agent_field=None,  # agents never bind Nova rules by id
    )

    await apply_owner_transfer(
        db,
        actor=user,
        entity=rule,
        new_owner_id=body.user_id,
        entity_type="nova_rule",
        entity_name=rule.name,
        request=request,
    )

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="nova_rule",
        entity_id=rule.id,
        entity_name=rule.name,
        summary=f"Updated Nova rule '{rule.name}'",
        details={
            "action": rule.action,
            "category": rule.category,
            "enabled": rule.enabled,
            "is_company_managed": rule.is_company_managed,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(rule)
    owner_names = await resolve_owner_names(db, [rule])
    return _to_response(
        rule, owner_name=owner_names.get(str(rule.user_id)) if rule.user_id else None
    )


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_nova_rule(
    rule_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Delete a Nova rule (creator or admin only)."""
    rule = await get_manageable_entity(
        db, NovaRule, user, rule_id, action="delete", entity_label="Nova rule"
    )
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="nova_rule",
        entity_id=rule.id,
        entity_name=rule.name,
        summary=f"Deleted Nova rule '{rule.name}'",
        details={"is_company_managed": rule.is_company_managed},
        request=request,
    )
    await db.delete(rule)
    await db.commit()


# ---------------------------------------------------------------------------
# Rule sync sources + engine status (admin) — powers the Nova integration's
# "Rules" admin tab under /integrations/:id.
# ---------------------------------------------------------------------------


def _require_admin(user: User) -> None:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admins only.")


def _source_to_dict(source, *, can_manage: bool) -> dict:
    from app.services.nova_rule_sync import served_sync_error

    return {
        "id": str(source.id),
        "integration_id": str(source.integration_id)
        if source.integration_id
        else None,
        "repo": source.repo,
        "branch": source.branch,
        "path": source.path,
        "sync_interval": source.sync_interval,
        "enabled": source.enabled,
        "last_synced_at": source.last_synced_at.isoformat()
        if source.last_synced_at
        else None,
        "last_sync_status": source.last_sync_status,
        "last_sync_error": served_sync_error(source.last_sync_error, can_manage=can_manage),
        "last_sync_rules": source.last_sync_rules,
    }
