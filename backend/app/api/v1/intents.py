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

"""Intent and Vertical management API.

CRUD for intents (global registry) and verticals (business colorations),
plus intent-to-agent binding management.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, require_admin, require_ai_enabled
from app.api.v1.helpers import parse_path_id, parse_uuid
from app.api.v1.schemas.column_limits import column_length_validator
from app.models.intent import Intent, IntentBinding, PlatformIntentDeclaration, Vertical
from app.models.user import User
from app.services.intent_aliases import canonical_intent_name, canonical_vertical
from app.services.intent_binding_writes import agent_shared_scope_filter as _agent_shared_scope_filter

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────────


class IntentCreate(BaseModel):
    name: str
    description: str = ""

    _fit_columns = column_length_validator(Intent, "name")


class IntentUpdate(BaseModel):
    name: str | None = None
    description: str | None = None

    _fit_columns = column_length_validator(Intent, "name")


class IntentBindingCreate(BaseModel):
    agent_id: str
    vertical: str | None = None
    priority: int = 0
    is_locked: bool = False
    # When set, updates priority_overrides[vertical] instead of base priority
    priority_override_vertical: str | None = None

    _fit_columns = column_length_validator(IntentBinding, "vertical")


class IntentBindingOut(BaseModel):
    id: str
    intent_id: str
    agent_id: str
    agent_name: str | None = None
    agent_slug: str | None = None
    vertical: str | None
    user_id: str | None
    is_locked: bool
    is_builtin: bool = False
    priority: int
    priority_overrides: dict[str, int] | None = None
    created_at: str
    updated_at: str


class IntentOut(BaseModel):
    id: str
    name: str
    description: str | None
    platforms: list[str] = []
    bindings: list[IntentBindingOut] = []
    is_builtin: bool = False
    created_at: str
    updated_at: str


class VerticalCreate(BaseModel):
    name: str

    _fit_columns = column_length_validator(Vertical, "name")


class VerticalUpdate(BaseModel):
    name: str

    _fit_columns = column_length_validator(Vertical, "name")


class VerticalOut(BaseModel):
    id: str
    name: str
    source: str
    platform_identifier: str | None
    binding_count: int = 0
    created_at: str
    updated_at: str


class IntentCatalogAgent(BaseModel):
    agent_id: str
    agent_name: str
    agent_slug: str | None = None
    agent_description: str | None = None
    vertical: str | None = None
    priority: int = 0


class IntentCatalogEntry(BaseModel):
    intent: str
    description: str | None = None
    agents: list[IntentCatalogAgent] = []


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _intent_to_out(
    db: AsyncSession,
    intent: Intent,
    user_id: uuid.UUID | None = None,
    user_group_ids: list[str] | None = None,
    *,
    is_admin: bool = False,
) -> IntentOut:
    """Convert an Intent model to its API response format.

    When user_id is provided, bindings are filtered to agents visible to the user
    (own + group-shared + company-managed). Admins see all bindings.
    When user_id is None, all bindings are returned (backward compat).
    """
    from app.api.v1.helpers import user_visible_filter
    from app.models.agent import Agent

    # Platforms that declared this intent
    decl_result = await db.execute(
        select(PlatformIntentDeclaration.platform_identifier).where(
            PlatformIntentDeclaration.intent_id == intent.id
        )
    )
    platforms = [r for r in decl_result.scalars().all()]

    # Bindings with agent info
    query = (
        select(IntentBinding, Agent.name, Agent.slug)
        .join(Agent, IntentBinding.agent_id == Agent.id)
        .where(IntentBinding.intent_id == intent.id)
    )

    # Filter agents by user visibility when user_id provided (admins see all)
    if user_id is not None and not is_admin:
        query = query.where(
            user_visible_filter(Agent, user_id, user_group_ids or [])
        )

    query = query.order_by(IntentBinding.priority, Agent.name)
    bind_result = await db.execute(query)
    bindings = []
    for row in bind_result.all():
        binding = row[0]
        agent_name = row[1]
        agent_slug = row[2]
        bindings.append(
            IntentBindingOut(
                id=str(binding.id),
                intent_id=str(binding.intent_id),
                agent_id=str(binding.agent_id),
                agent_name=agent_name,
                agent_slug=agent_slug,
                vertical=binding.vertical,
                user_id=str(binding.user_id) if binding.user_id else None,
                is_locked=binding.is_locked,
                is_builtin=binding.is_builtin,
                priority=binding.priority,
                priority_overrides=binding.priority_overrides,
                created_at=binding.created_at.isoformat(),
                updated_at=binding.updated_at.isoformat(),
            )
        )

    return IntentOut(
        id=str(intent.id),
        name=intent.name,
        description=intent.description,
        platforms=platforms,
        bindings=bindings,
        is_builtin=any(b.is_builtin for b in bindings),
        created_at=intent.created_at.isoformat(),
        updated_at=intent.updated_at.isoformat(),
    )


async def _intents_to_out_batch(
    db: AsyncSession,
    intents: list[Intent],
    user_id: uuid.UUID | None = None,
    user_group_ids: list[str] | None = None,
    *,
    is_admin: bool = False,
) -> list[IntentOut]:
    """Batch variant of :func:`_intent_to_out` for list endpoints.

    Resolves all platform declarations and agent bindings for *intents* in two
    queries total (grouped by intent id) instead of the 2 queries per intent
    that calling ``_intent_to_out`` in a loop would issue.
    """
    from app.api.v1.helpers import user_visible_filter
    from app.models.agent import Agent

    if not intents:
        return []

    intent_ids = [intent.id for intent in intents]

    decl_result = await db.execute(
        select(
            PlatformIntentDeclaration.intent_id,
            PlatformIntentDeclaration.platform_identifier,
        ).where(PlatformIntentDeclaration.intent_id.in_(intent_ids))
    )
    platforms_by_intent: dict[uuid.UUID, list[str]] = {}
    for intent_id, platform_identifier in decl_result.all():
        platforms_by_intent.setdefault(intent_id, []).append(platform_identifier)

    bind_query = (
        select(IntentBinding, Agent.name, Agent.slug)
        .join(Agent, IntentBinding.agent_id == Agent.id)
        .where(IntentBinding.intent_id.in_(intent_ids))
    )
    if user_id is not None and not is_admin:
        bind_query = bind_query.where(
            user_visible_filter(Agent, user_id, user_group_ids or [])
        )
    bind_query = bind_query.order_by(IntentBinding.priority, Agent.name)
    bind_result = await db.execute(bind_query)
    bindings_by_intent: dict[uuid.UUID, list[IntentBindingOut]] = {}
    for binding, agent_name, agent_slug in bind_result.all():
        bindings_by_intent.setdefault(binding.intent_id, []).append(
            IntentBindingOut(
                id=str(binding.id),
                intent_id=str(binding.intent_id),
                agent_id=str(binding.agent_id),
                agent_name=agent_name,
                agent_slug=agent_slug,
                vertical=binding.vertical,
                user_id=str(binding.user_id) if binding.user_id else None,
                is_locked=binding.is_locked,
                is_builtin=binding.is_builtin,
                priority=binding.priority,
                priority_overrides=binding.priority_overrides,
                created_at=binding.created_at.isoformat(),
                updated_at=binding.updated_at.isoformat(),
            )
        )

    out: list[IntentOut] = []
    for intent in intents:
        intent_bindings = bindings_by_intent.get(intent.id, [])
        out.append(
            IntentOut(
                id=str(intent.id),
                name=intent.name,
                description=intent.description,
                platforms=platforms_by_intent.get(intent.id, []),
                bindings=intent_bindings,
                is_builtin=any(b.is_builtin for b in intent_bindings),
                created_at=intent.created_at.isoformat(),
                updated_at=intent.updated_at.isoformat(),
            )
        )
    return out


# ── Intent Catalog (lean, user-scoped) ───────────────────────────────────────


def _effective_priority(binding, vertical: str | None) -> int:
    """Resolve effective priority using priority_overrides with fallback.

    The rule lives in ``services/intent_binding_writes.effective_priority``,
    which the agent tools use to name the winner of an intent.
    """
    from app.services.intent_binding_writes import effective_priority

    return effective_priority(binding, vertical)


@router.get("/intents/catalog", response_model=list[IntentCatalogEntry])
async def get_intent_catalog(
    platform: str | None = None,
    vertical: str | None = None,
    intent: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Return a lean intent catalog scoped to agents visible to the current user.

    This is the endpoint platforms (OpenCTI, OpenAEV) should call after registration
    to get available intents and their bound agents. Results respect three-tier
    visibility (own + group-shared + company-managed) for non-admin callers; admins
    additionally see all bindings within that scope.

    A sharing-scope filter is always applied (even for admins): only agents that are
    company-managed or shared with at least one group are eligible to appear in the
    catalog. Private agents must never leak to external platforms via this endpoint.

    Optional filters:
      - platform: only intents declared by this platform identifier
      - vertical: only agents bound with this vertical (or universal)
      - intent: comma-separated intent names to filter (e.g. "analyze-email,draft-reply")

    A renamed vertical or intent namespace (``intent_aliases``) is read under
    its current name, so a platform build that predates the rename keeps its
    catalog.
    """
    vertical = canonical_vertical(vertical)
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.agent import Agent

    user_group_ids = await get_user_group_ids(db, user.id)

    # Select intents, optionally filtered by platform and/or intent names
    intent_query = select(Intent).order_by(Intent.name)
    if platform:
        intent_ids = select(PlatformIntentDeclaration.intent_id).where(
            PlatformIntentDeclaration.platform_identifier == platform
        )
        intent_query = intent_query.where(Intent.id.in_(intent_ids))
    if intent:
        intent_names = [canonical_intent_name(n.strip()) for n in intent.split(",") if n.strip()]
        if intent_names:
            intent_query = intent_query.where(Intent.name.in_(intent_names))

    result = await db.execute(intent_query)
    intents = result.scalars().all()

    catalog: list[IntentCatalogEntry] = []
    for intent in intents:
        # Build agent binding query with visibility filter
        bind_query = (
            select(IntentBinding, Agent.name, Agent.slug, Agent.description)
            .join(Agent, IntentBinding.agent_id == Agent.id)
            .where(
                IntentBinding.intent_id == intent.id,
                Agent.enabled.is_(True),
            )
        )

        # Vertical filter: match specific vertical OR universal (NULL)
        if vertical:
            bind_query = bind_query.where(
                or_(
                    IntentBinding.vertical == vertical,
                    IntentBinding.vertical.is_(None),
                )
            )

        # Sharing scope filter: only company-managed or group-shared agents may
        # appear in the catalog (private agents must never leak to external
        # platforms). See ``_agent_shared_scope_filter`` for the rationale and
        # the JSON-scalar robustness guard.
        bind_query = bind_query.where(_agent_shared_scope_filter())

        # Additionally apply per-user visibility for non-admins
        if not user.is_admin:
            bind_query = bind_query.where(
                user_visible_filter(Agent, user.id, user_group_ids)
            )

        bind_result = await db.execute(bind_query)
        rows = bind_result.all()

        # Highest effective priority first; a tie goes to the binding made
        # first (``intent_binding_writes.WINNER_RULE``, which the agent tools
        # state), so the order never depends on the database's row order.
        from app.services.intent_binding_writes import rank_bindings

        rows_sorted = rank_bindings(list(rows), vertical, key=lambda r: r[0])

        agents_list = [
            IntentCatalogAgent(
                agent_id=str(row[0].agent_id),
                agent_name=row[1],
                agent_slug=row[2],
                agent_description=row[3],
                vertical=row[0].vertical,
                priority=_effective_priority(row[0], vertical),
            )
            for row in rows_sorted
        ]

        # Only include intents that have at least one visible agent
        if agents_list:
            catalog.append(
                IntentCatalogEntry(
                    intent=intent.name,
                    description=intent.description,
                    agents=agents_list,
                )
            )

    return catalog


# ── Intent CRUD ──────────────────────────────────────────────────────────────


@router.get("/intents", response_model=list[IntentOut])
async def list_intents(
    platform: str | None = None,
    vertical: str | None = None,
    agent_id: str | None = None,
    status: str | None = None,  # "used", "unused", or None (all)
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """List all intents with optional filters.

    Bindings are scoped to agents visible to the current user
    (own + group-shared + company-managed).
    """
    from app.api.v1.helpers import get_user_group_ids

    user_group_ids = await get_user_group_ids(db, user.id)

    query = select(Intent).order_by(Intent.name)

    # Filter by platform
    if platform:
        intent_ids = select(PlatformIntentDeclaration.intent_id).where(
            PlatformIntentDeclaration.platform_identifier == platform
        )
        query = query.where(Intent.id.in_(intent_ids))

    # Filter by agent — only intents that have at least one binding to this agent
    if agent_id:
        agent_intent_ids = select(IntentBinding.intent_id).where(
            IntentBinding.agent_id == parse_uuid(agent_id, label="agent ID")
        )
        query = query.where(Intent.id.in_(agent_intent_ids))

    result = await db.execute(query)
    intents = result.scalars().all()

    # Batch declarations + bindings across all intents (2 queries total)
    # instead of 2 per intent.
    intent_outs = await _intents_to_out_batch(
        db, list(intents), user.id, user_group_ids, is_admin=user.is_admin
    )

    out = []
    for intent_out in intent_outs:
        # Filter by vertical (must have at least one binding with this vertical)
        if vertical:
            has_vertical = any(
                b.vertical == vertical or b.vertical is None
                for b in intent_out.bindings
            )
            if not has_vertical:
                continue

        # Filter by status
        if status == "used":
            if not intent_out.bindings and not intent_out.platforms:
                continue
        elif status == "unused" and (intent_out.bindings or intent_out.platforms):
            continue

        out.append(intent_out)

    return out


@router.get("/intents/{intent_id}", response_model=IntentOut)
async def get_intent(
    intent_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Get a single intent with its bindings."""
    from app.api.v1.helpers import get_user_group_ids

    result = await db.execute(
        select(Intent).where(Intent.id == parse_path_id(intent_id, not_found_detail="Intent not found"))
    )
    intent = result.scalar_one_or_none()
    if not intent:
        raise HTTPException(status_code=404, detail="Intent not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    return await _intent_to_out(
        db, intent, user.id, user_group_ids, is_admin=user.is_admin
    )


@router.post("/intents", response_model=IntentOut, status_code=201)
async def create_intent(
    body: IntentCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Create a custom intent (admin only)."""
    # Check uniqueness
    existing = await db.execute(select(Intent).where(Intent.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail=f"Intent '{body.name}' already exists"
        )

    intent = Intent(name=body.name, description=body.description or None)
    db.add(intent)
    await db.commit()
    await db.refresh(intent)
    logger.info("Created intent: %s", body.name)
    return await _intent_to_out(db, intent)


@router.put("/intents/{intent_id}", response_model=IntentOut)
async def update_intent(
    intent_id: str,
    body: IntentUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Update intent metadata (admin only)."""
    result = await db.execute(
        select(Intent).where(Intent.id == parse_path_id(intent_id, not_found_detail="Intent not found"))
    )
    intent = result.scalar_one_or_none()
    if not intent:
        raise HTTPException(status_code=404, detail="Intent not found")

    if body.name is not None:
        # Check uniqueness
        dup = await db.execute(
            select(Intent).where(Intent.name == body.name, Intent.id != intent.id)
        )
        if dup.scalar_one_or_none():
            raise HTTPException(
                status_code=409, detail=f"Intent '{body.name}' already exists"
            )
        intent.name = body.name

    if body.description is not None:
        intent.description = body.description

    await db.commit()
    await db.refresh(intent)
    return await _intent_to_out(db, intent)


@router.delete("/intents/{intent_id}", status_code=204)
async def delete_intent(
    intent_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Delete an unused intent (admin only)."""
    result = await db.execute(
        select(Intent).where(Intent.id == parse_path_id(intent_id, not_found_detail="Intent not found"))
    )
    intent = result.scalar_one_or_none()
    if not intent:
        raise HTTPException(status_code=404, detail="Intent not found")

    # Check if it has bindings
    binding_count = await db.execute(
        select(func.count())
        .select_from(IntentBinding)
        .where(IntentBinding.intent_id == intent.id)
    )
    if binding_count.scalar_one() > 0:
        raise HTTPException(
            status_code=409,
            detail="Cannot delete intent with active bindings. Remove bindings first.",
        )

    await db.delete(intent)
    await db.commit()
    logger.info("Deleted intent: %s", intent.name)


# ── Intent Binding ───────────────────────────────────────────────────────────


@router.put("/intents/{intent_id}/bind", response_model=IntentBindingOut)
async def upsert_binding(
    intent_id: str,
    body: IntentBindingCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Upsert an intent binding. Admins set global defaults; users set personal overrides."""
    from app.api.v1.helpers import get_user_agent

    result = await db.execute(
        select(Intent).where(Intent.id == parse_path_id(intent_id, not_found_detail="Intent not found"))
    )
    intent = result.scalar_one_or_none()
    if not intent:
        raise HTTPException(status_code=404, detail="Intent not found")

    # Verify agent exists and is accessible to the current user.  ``get_user_agent``
    # folds the existence + three-tier visibility check into a single DB round-trip
    # and returns 404 on either condition — so a non-admin caller cannot probe for
    # the existence of agents they cannot see, and we never materialise an Agent
    # row we won't expose.  Admins bypass visibility (they can bind any agent that
    # exists), but the global-binding sharing-scope gate below still applies and
    # blocks admin global bindings to private agents.
    #
    # ``parse_uuid`` runs first so a malformed ``body.agent_id`` returns HTTP 422
    # with a structured error instead of crashing in PostgreSQL with a 500.
    agent_uuid = parse_uuid(body.agent_id, label="agent ID")
    agent = await get_user_agent(
        db, user.id, str(agent_uuid), is_admin=user.is_admin
    )

    # Admins write the global binding, anyone else a personal override; a
    # global binding to a private agent, an override of a locked default and
    # the lock switch for a non-admin are refused by the single writer the
    # agent tools share (``services/intent_binding_writes.py``).
    from app.services.intent_binding_writes import IntentBindingRefusedError, upsert_intent_binding

    try:
        binding = await upsert_intent_binding(
            db,
            user,
            intent,
            agent,
            vertical=body.vertical,
            priority=body.priority,
            is_locked=body.is_locked,
            priority_override_vertical=body.priority_override_vertical,
        )
    except IntentBindingRefusedError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    await db.commit()
    await db.refresh(binding)

    return IntentBindingOut(
        id=str(binding.id),
        intent_id=str(binding.intent_id),
        agent_id=str(binding.agent_id),
        agent_name=agent.name,
        agent_slug=agent.slug,
        vertical=binding.vertical,
        user_id=str(binding.user_id) if binding.user_id else None,
        is_locked=binding.is_locked,
        is_builtin=binding.is_builtin,
        priority=binding.priority,
        priority_overrides=binding.priority_overrides,
        created_at=binding.created_at.isoformat(),
        updated_at=binding.updated_at.isoformat(),
    )


@router.delete("/intents/{intent_id}/bind/{binding_id}", status_code=204)
async def delete_binding(
    intent_id: str,
    binding_id: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Remove an intent binding."""
    result = await db.execute(
        select(IntentBinding).where(
            IntentBinding.id == parse_path_id(binding_id, not_found_detail="Binding not found"),
            IntentBinding.intent_id == parse_path_id(intent_id, not_found_detail="Binding not found"),
        )
    )
    binding = result.scalar_one_or_none()
    if not binding:
        raise HTTPException(status_code=404, detail="Binding not found")

    # Non-admin can only delete their own overrides
    from app.services.intent_binding_writes import IntentBindingRefusedError, check_intent_binding_delete

    try:
        check_intent_binding_delete(user, binding)
    except IntentBindingRefusedError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc

    await db.delete(binding)
    await db.commit()
    logger.info("Deleted intent binding %s for intent %s", binding_id, intent_id)


# ── Verticals CRUD ───────────────────────────────────────────────────────────


@router.get("/verticals", response_model=list[VerticalOut])
async def list_verticals(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """List all verticals with binding counts."""
    result = await db.execute(select(Vertical).order_by(Vertical.name))
    verticals = result.scalars().all()

    bc_result = await db.execute(
        select(IntentBinding.vertical, func.count().label("count")).group_by(
            IntentBinding.vertical
        )
    )
    binding_counts = {vertical: count for vertical, count in bc_result}

    return [
        VerticalOut(
            id=str(v.id),
            name=v.name,
            source=v.source,
            platform_identifier=v.platform_identifier,
            binding_count=binding_counts.get(v.name, 0),
            created_at=v.created_at.isoformat(),
            updated_at=v.updated_at.isoformat(),
        )
        for v in verticals
    ]


@router.post("/verticals", response_model=VerticalOut, status_code=201)
async def create_vertical(
    body: VerticalCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Create a custom vertical (admin only)."""
    existing = await db.execute(select(Vertical).where(Vertical.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail=f"Vertical '{body.name}' already exists"
        )

    vertical = Vertical(name=body.name, source="manual")
    db.add(vertical)
    await db.commit()
    await db.refresh(vertical)
    logger.info("Created vertical: %s", body.name)

    return VerticalOut(
        id=str(vertical.id),
        name=vertical.name,
        source=vertical.source,
        platform_identifier=vertical.platform_identifier,
        binding_count=0,
        created_at=vertical.created_at.isoformat(),
        updated_at=vertical.updated_at.isoformat(),
    )


@router.put("/verticals/{vertical_id}", response_model=VerticalOut)
async def update_vertical(
    vertical_id: str,
    body: VerticalUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Rename a vertical (admin only)."""
    result = await db.execute(
        select(Vertical).where(Vertical.id == parse_path_id(vertical_id, not_found_detail="Vertical not found"))
    )
    vertical = result.scalar_one_or_none()
    if not vertical:
        raise HTTPException(status_code=404, detail="Vertical not found")

    # Check uniqueness
    dup = await db.execute(
        select(Vertical).where(Vertical.name == body.name, Vertical.id != vertical.id)
    )
    if dup.scalar_one_or_none():
        raise HTTPException(
            status_code=409, detail=f"Vertical '{body.name}' already exists"
        )

    old_name = vertical.name
    vertical.name = body.name

    # Cascade rename to bindings
    from sqlalchemy import update as sa_update

    await db.execute(
        sa_update(IntentBinding)
        .where(IntentBinding.vertical == old_name)
        .values(vertical=body.name)
    )

    await db.commit()
    await db.refresh(vertical)

    bc = await db.execute(
        select(func.count())
        .select_from(IntentBinding)
        .where(IntentBinding.vertical == vertical.name)
    )

    return VerticalOut(
        id=str(vertical.id),
        name=vertical.name,
        source=vertical.source,
        platform_identifier=vertical.platform_identifier,
        binding_count=bc.scalar_one(),
        created_at=vertical.created_at.isoformat(),
        updated_at=vertical.updated_at.isoformat(),
    )


@router.delete("/verticals/{vertical_id}", status_code=204)
async def delete_vertical(
    vertical_id: str,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_admin),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Delete a vertical (blocked if in use by bindings)."""
    result = await db.execute(
        select(Vertical).where(Vertical.id == parse_path_id(vertical_id, not_found_detail="Vertical not found"))
    )
    vertical = result.scalar_one_or_none()
    if not vertical:
        raise HTTPException(status_code=404, detail="Vertical not found")

    # Check if in use
    bc = await db.execute(
        select(func.count())
        .select_from(IntentBinding)
        .where(IntentBinding.vertical == vertical.name)
    )
    count = bc.scalar_one()
    if count > 0:
        raise HTTPException(
            status_code=409,
            detail=f"Vertical '{vertical.name}' is used in {count} binding(s). Remove bindings first.",
        )

    await db.delete(vertical)
    await db.commit()
    logger.info("Deleted vertical: %s", vertical.name)
