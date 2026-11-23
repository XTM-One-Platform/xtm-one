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

"""Fleet CRUD, members, exposure and the Fleet Card.

A Fleet is a named, owned group of agents (an orchestrator and its desks)
bound to exactly one workspace, its coordination. The rules every write
follows live in ``app.services.fleets``; this router is the REST surface:

* ``""``: list (three-tier visibility, ``scope=all`` for an admin) and create
  (a workspace named after the fleet is created with it unless one is named);
* ``/{id}``: read, update (orchestrator, card fields, sharing, archive),
  delete (members go back to the playground, the workspace stays);
* ``/{id}/members``: home members and connected agents or fleets;
  ``/{id}/members/bulk`` sets the member settings of several connected rows
  in one transaction (a row's NULL setting inherits the fleet's
  ``member_defaults``, written through update);
  ``/{id}/members/{member_id}/move`` moves a home member to another fleet in
  one transaction;
* ``/{id}/exposure``: which home members other fleets may reach;
* ``/{id}/card``: the A2A-shaped card other fleets read;
* ``/{id}/spec`` and ``/apply``: the fleet as code, exported on the
  agent bundle format and applied with a dry run (``app.services.fleet_spec``);
* ``/{id}/boundary-violations``: configured handovers the exposure refuses,
  kept and reported;
* ``/by-agent/{agent_id}``: where one agent sits (its home fleet, the fleets
  that connect it), read by the agent page's Fleet card;
* ``/{id}/cost``: the cost of the runs attributed to the fleet
  (``app.services.fleet_runs``); its run defaults (``budget_config``,
  ``max_iterations``) are written through create and update under the
  agents API's authority rule;
* ``/{id}/graph``, ``/{id}/hierarchy``, ``/{id}/health``, ``/{id}/activity``:
  the fleet's views, read from what the platform records
  (``app.services.fleet_views``). The schedule view is
  ``GET /assignments/schedule?fleet=<id>`` with
  ``GET /work-items/scheduled?fleet=<id>``: one schedule read, narrowed.

Gating follows ``workspaces``: the licence question only, since managing a
fleet invokes no model.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_agentic_ai, require_agentic_ai_licensed
from app.api.v1.helpers import (
    ENTITY_LIST_SCOPE_DESCRIPTION,
    EntityListScope,
    apply_group_share_fields,
    apply_owner_transfer,
    apply_user_share_fields,
    assert_budget_within_ceiling,
    can_manage,
    get_manageable_entity,
    get_user_agent,
    get_user_group_ids,
    handle_company_managed_toggle,
    list_scope_admin_bypass,
    parse_path_id,
    parse_uuid,
    require_admin_for_company_managed,
    resolve_owner_names,
    run_visible_filter,
    user_visible_filter,
    user_visible_query,
    wants_access_restriction,
)
from app.api.v1.schemas.fleet import (
    AgentConnectedFleet,
    AgentFleetsResponse,
    AgentHomeFleet,
    CaseRunEntry,
    CaseRunsResponse,
    FleetActivityResponse,
    FleetAgentCostEntry,
    FleetApprovalGatesResponse,
    FleetApprovalGatesUpdateRequest,
    FleetAutomationsResponse,
    FleetAutonomyApplyRequest,
    FleetBoundaryViolationResponse,
    FleetBulkAction,
    FleetBulkOutcome,
    FleetBulkResponse,
    FleetCapabilitySlotsResponse,
    FleetCapabilityTypeSet,
    FleetCardResponse,
    FleetControlResponse,
    FleetCostResponse,
    FleetCreate,
    FleetDocFindingsDismissRequest,
    FleetDocFindingsRestoreRequest,
    FleetDocumentationHistoryResponse,
    FleetDocumentationRegenerateRequest,
    FleetDocumentationResponse,
    FleetDocumentationRestoreRequest,
    FleetDocumentationVersion,
    FleetEnableItemResponse,
    FleetEnableRequest,
    FleetEnableResponse,
    FleetExposureUpdate,
    FleetFlowEdge,
    FleetFlowNode,
    FleetFlowResponse,
    FleetFork,
    FleetForkResponse,
    FleetGraphEdge,
    FleetGraphNode,
    FleetGraphResponse,
    FleetHealthResponse,
    FleetHealthStartsResponse,
    FleetHierarchyNode,
    FleetInstructionsPositionRequest,
    FleetLivePrerequisite,
    FleetMemberCreate,
    FleetMemberHealthResponse,
    FleetMemberInstructionDraftRequest,
    FleetMemberInstructionDraftResponse,
    FleetMemberMove,
    FleetMemberResponse,
    FleetMembersBulkUpdate,
    FleetMemberSlots,
    FleetMemberUpdate,
    FleetOrganizationRunEstimate,
    FleetPrerequisiteConfirmRequest,
    FleetPrerequisiteState,
    FleetResponse,
    FleetRunBudget,
    FleetRunEntry,
    FleetRunEstimateResponse,
    FleetRunStatsResponse,
    FleetSetupStatusResponse,
    FleetSetupStep,
    FleetSharedStateHygieneResponse,
    FleetSlotBind,
    FleetSlotSet,
    FleetSourceChange,
    FleetSourceChangesResponse,
    FleetSpecApply,
    FleetSpecGenerate,
    FleetSpecGenerateResponse,
    FleetSpecPlanResponse,
    FleetUpdate,
)
from app.core.database import get_db
from app.core.utils import safe_uuid
from app.models.agent import Agent
from app.models.fleet import FLEET_DEFAULT_ICON, FLEET_ROLE_CONNECTED, FLEET_ROLE_MEMBER, Fleet, FleetMember
from app.models.general_assistant_config import clamp_max_iterations
from app.models.user import User
from app.models.workspace import Workspace
from app.services.audit import log_audit
from app.services.delegation import list_delegations
from app.services.fleet_autonomy import (
    effective_level,
    ensure_level_change_allowed,
    fleet_gates,
    gate_catalog,
    normalize_level,
    with_live_step_gates,
)
from app.services.fleet_control import (
    enable_fleet,
    fleet_control_audit_details,
    pausable_count,
    pause_fleet,
    plan_fleet_enable,
    resumable_count,
    resume_fleet,
    unset_fleet,
)
from app.services.fleet_doc_on_create import (
    GENERATION_FAILED,
    claim_generation,
    generation_running,
    generation_state,
    release_generation,
    schedule_documentation_on_create,
    schedule_pending_sections,
)
from app.services.fleet_flow import build_fleet_flow
from app.services.fleet_runs import fleet_cost
from app.services.fleet_views import (
    HealthFleetStarts,
    build_fleet_graph,
    build_fleet_health,
    case_runs,
    fleet_automation_counts,
    fleet_card_facts,
    fleet_run_stats,
    fleet_tree,
    home_member_ids,
    recent_runs,
)
from app.services.fleets import (
    MAX_SCOPE_ORGANIZATIONS,
    FleetActionRefusedError,
    add_connected,
    add_home_member,
    build_fleet_card,
    bulk_update_members,
    check_scope_organizations,
    create_fleet,
    effective_member_settings,
    ensure_builtin_fleet_writable,
    ensure_workspace_can_host,
    exposed_agent_ids,
    fleet_boundary_violations,
    fleet_members,
    move_member,
    normalize_accepted_handover_types,
    remove_member,
    resolve_organization_scope,
    set_exposed_agents,
    set_fleet_health_check,
    set_member_defaults,
    set_orchestrator,
    unique_fleet_slug,
    update_member,
)
from app.services.health.read_only import HEALTH_READ_ONLY_REFUSAL, is_health_fleet
from app.services.workspaces import create_workspace, lock_workspace_for_hosting

if TYPE_CHECKING:
    from app.services.fleet_datasheet import ValueFigures

router = APIRouter()

_LABEL = "Fleet"
_NOT_FOUND = f"{_LABEL} not found"


def _run_budget_value(budget: FleetRunBudget | None) -> dict[str, int] | None:
    """The stored form of a fleet's run budget (#1526): its bounds that are set, ``None`` when none is."""
    if budget is None:
        return None
    stored = budget.model_dump(exclude_none=True)
    return stored or None


def _can_manage_fleet(user: User, fleet: Fleet, user_group_ids: list[str] | None) -> bool:
    """The ``can_manage`` a fleet response carries for *user*.

    ``helpers.can_manage`` for every fleet but the built-in Platform Health
    one, which takes no write at all, administrators included
    (``ensure_builtin_fleet_writable``): the UI offers no switch the API
    refuses (#101 S1b).
    """
    if is_health_fleet(fleet):
        return False
    return can_manage(user, fleet, user_group_ids)


def _lock_fixes_on_health_fleet(fleet: Fleet, lines: list[Any]) -> None:
    """Nobody fixes a prerequisite of Platform Health from its page, but its Settings switch.

    Sets ``can_fix`` false on every live line of the health fleet, so the About
    page offers no Fix it link and no Fix with AI (#101 S1b, UX r2 B1 on #464).
    The ``platform_health_off`` line keeps ``can_fix`` as computed (an
    administrator opens Settings > Platform Health), UX r2 B3 on #980.
    """
    if is_health_fleet(fleet):
        for line in lines:
            if getattr(line, "reason_code", "") == "platform_health_off":
                continue
            line.can_fix = False


def _refused(exc: FleetActionRefusedError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail)


def _ensure_writable(fleet: Fleet, fields: set[str] | None = None) -> None:
    """403 on a write to a built-in fleet beyond its writable switches."""
    try:
        ensure_builtin_fleet_writable(fleet, fields)
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None


async def _visible_fleet(db: AsyncSession, user: User, fleet_id: str, user_group_ids: list[str] | None = None) -> Fleet:
    """A fleet the caller may READ (own, shared at any tier, company-managed), or 404."""
    fleet_uuid = parse_path_id(fleet_id, not_found_detail=_NOT_FOUND)
    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user.id)
    fleet = (
        await db.execute(
            select(Fleet).where(
                Fleet.id == fleet_uuid,
                user_visible_filter(Fleet, user.id, user_group_ids, is_admin=user.is_admin),
            )
        )
    ).scalar_one_or_none()
    if fleet is None:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return fleet


async def _manageable_fleet(db: AsyncSession, user: User, fleet_id: str, action: str = "edit") -> Fleet:
    return await get_manageable_entity(db, Fleet, user, fleet_id, action=action, entity_label=_LABEL)


async def _host_workspace(db: AsyncSession, user: User, workspace_id: str, *, company_managed: bool) -> Workspace:
    """The workspace a fleet is bound to: one the caller may manage, able to host it."""
    workspace = await get_manageable_entity(
        db, Workspace, user, workspace_id, action="bind a fleet to", entity_label="Workspace"
    )
    # Serialised with an archive of the same workspace.
    await lock_workspace_for_hosting(db, workspace)
    try:
        ensure_workspace_can_host(company_managed, workspace)
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    return workspace


def _health_starts(starts: HealthFleetStarts | None) -> FleetHealthStartsResponse | None:
    """The Platform Health fleet's starts line (#974); ``None`` on every other fleet."""
    if starts is None:
        return None
    return FleetHealthStartsResponse(
        events=starts.events,
        schedules=starts.schedules,
        next_run_at=_iso(starts.next_run_at),
        modules=list(starts.modules),
        settings_route=starts.settings_route,
        active=starts.active,
    )


async def _respond(db: AsyncSession, user: User, fleets: list[Fleet], user_group_ids: list[str] | None = None):
    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user.id)
    if not fleets:
        return []
    ids = [f.id for f in fleets]
    owner_names = await resolve_owner_names(db, fleets)
    workspace_names = dict(
        (
            await db.execute(
                select(Workspace.id, Workspace.name).where(Workspace.id.in_({f.workspace_id for f in fleets}))
            )
        ).all()
    )
    counts: dict[tuple[uuid.UUID, str], int] = {
        (fid, role): n
        for fid, role, n in (
            await db.execute(
                select(FleetMember.fleet_id, FleetMember.role, func.count(FleetMember.id))
                .where(FleetMember.fleet_id.in_(ids))
                .group_by(FleetMember.fleet_id, FleetMember.role)
            )
        ).all()
    }
    exposed_rows = (
        (
            await db.execute(
                select(FleetMember).where(
                    FleetMember.fleet_id.in_(ids),
                    FleetMember.role == FLEET_ROLE_MEMBER,
                    FleetMember.is_exposed.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    by_fleet: dict[uuid.UUID, list[FleetMember]] = {}
    for row in exposed_rows:
        by_fleet.setdefault(row.fleet_id, []).append(row)
    # The card facts of the Fleets list (#9977 S2), batched: no health per card, no run.
    facts = await fleet_card_facts(db, fleets, run_scope=run_visible_filter(user.id))
    from app.services.fleet_gate_proposals import fleets_gated_action_counts

    # What the card's approval line says (#10457): the level only where it gates an action, batched.
    gated_counts = await fleets_gated_action_counts(db, fleets)
    return [
        FleetResponse(
            id=str(f.id),
            user_id=str(f.user_id) if f.user_id else None,
            owner_name=owner_names.get(str(f.user_id)) if f.user_id else None,
            name=f.name,
            slug=f.slug,
            description=f.description or "",
            icon=f.icon or FLEET_DEFAULT_ICON,
            color=f.color or "",
            workspace_id=str(f.workspace_id),
            workspace_name=workspace_names.get(f.workspace_id, ""),
            orchestrator_agent_id=str(f.orchestrator_agent_id) if f.orchestrator_agent_id else None,
            skills=f.skills,
            accepted_handover_types=f.accepted_handover_types,
            is_archived=bool(f.is_archived),
            is_company_managed=bool(f.is_company_managed),
            hide_activity_from_non_admins=bool(getattr(f, "hide_activity_from_non_admins", False)),
            is_builtin=bool(f.is_builtin),
            shared_group_ids=f.shared_group_ids,
            shared_group_permissions=f.shared_group_permissions,
            shared_user_ids=f.shared_user_ids,
            shared_user_permissions=f.shared_user_permissions,
            access_restricted=bool(f.access_restricted),
            can_manage=_can_manage_fleet(user, f, user_group_ids),
            read_only_reason=HEALTH_READ_ONLY_REFUSAL if is_health_fleet(f) else None,
            member_count=counts.get((f.id, FLEET_ROLE_MEMBER), 0),
            connected_count=counts.get((f.id, FLEET_ROLE_CONNECTED), 0),
            exposed_agent_ids=[str(a) for a in exposed_agent_ids(f, by_fleet.get(f.id, []))],
            budget_config=f.budget_config or None,
            max_iterations=f.max_iterations,
            run_budget=getattr(f, "run_budget", None) or None,
            member_defaults={
                "context_inclusion": f.default_context_inclusion or "none",
                "collaboration_instruction": f.default_collaboration_instruction or "",
                "organization_scope": resolve_organization_scope(f.organization_scope, None),
            },
            autonomy_level=effective_level(getattr(f, "autonomy_level", None)),
            gated_action_count=gated_counts.get(f.id, 0),
            instructions_position=getattr(f, "instructions_position", None),
            health_check=f.health_check if isinstance(getattr(f, "health_check", None), dict) else None,
            forked_from=f.forked_from if isinstance(f.forked_from, dict) else None,
            automations=FleetAutomationsResponse(
                total=facts[f.id].automations.total,
                enabled=facts[f.id].automations.enabled,
                paused=facts[f.id].automations.paused,
                fleet_paused=facts[f.id].automations.fleet_paused,
            ),
            last_run_at=_iso(facts[f.id].last_run_at),
            health_starts=_health_starts(facts[f.id].health_starts),
            created_at=f.created_at.isoformat() if f.created_at else "",
            updated_at=f.updated_at.isoformat() if f.updated_at else "",
        )
        for f in fleets
    ]


@router.get("", response_model=list[FleetResponse])
async def list_fleets(
    include_archived: bool = False,
    workspace_id: str | None = Query(None, description="Only the fleets this workspace hosts."),
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """List the fleets visible to the caller."""
    user_group_ids = await get_user_group_ids(db, user.id)
    query = user_visible_query(Fleet, user.id, user_group_ids, is_admin=list_scope_admin_bypass(scope, user))
    if not include_archived:
        query = query.where(Fleet.is_archived.is_(False))
    if workspace_id is not None:
        query = query.where(Fleet.workspace_id == parse_uuid(workspace_id, label="workspace_id"))
    fleets = list((await db.execute(query.order_by(Fleet.name, Fleet.id))).scalars().all())
    return await _respond(db, user, fleets, user_group_ids)


@router.post("", response_model=FleetResponse, status_code=status.HTTP_201_CREATED)
async def create_fleet_endpoint(
    body: FleetCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Create a fleet bound to a workspace (one is created with it when none is named)."""
    require_admin_for_company_managed(body, user, "Only admins can create company-managed fleets")
    try:
        accepted = normalize_accepted_handover_types(body.accepted_handover_types)
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    # The run defaults of the home members, under the agents API's
    # authority rule: above the platform ceiling only for an admin.
    budget_config = assert_budget_within_ceiling(body.budget_config, user=user, what="a new fleet")
    max_iterations = clamp_max_iterations(body.max_iterations) if body.max_iterations is not None else None
    if body.workspace_id:
        workspace = await _host_workspace(db, user, body.workspace_id, company_managed=body.is_company_managed)
        workspace_created = False
    else:
        workspace = await create_workspace(
            db,
            user_id=user.id,
            name=body.name,
            description=f"Where the fleet {body.name} works.",
            is_company_managed=body.is_company_managed,
        )
        workspace_created = True
    fleet = await create_fleet(
        db,
        user_id=user.id,
        name=body.name,
        workspace_id=workspace.id,
        description=body.description or "",
        icon=body.icon or FLEET_DEFAULT_ICON,
        color=body.color or "",
        skills=[s.model_dump() for s in body.skills] if body.skills is not None else None,
        accepted_handover_types=accepted,
        is_company_managed=body.is_company_managed,
    )
    fleet.budget_config = budget_config
    fleet.max_iterations = max_iterations
    fleet.run_budget = _run_budget_value(body.run_budget)
    await apply_group_share_fields(db, fleet, body, partial=False)
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="fleet",
        entity_id=fleet.id,
        entity_name=fleet.name,
        summary=f"Created fleet '{fleet.name}'",
        details={
            "slug": fleet.slug,
            "workspace_id": str(workspace.id),
            "workspace_created": workspace_created,
            "is_company_managed": fleet.is_company_managed,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(fleet)
    # The first documentation, generated once in the background (#10316): the fleet opens on About.
    await schedule_documentation_on_create(db, fleet.id, user.id)
    return (await _respond(db, user, [fleet]))[0]


@router.get("/{fleet_id}", response_model=FleetResponse)
async def get_fleet(
    fleet_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """One fleet the caller can see."""
    user_group_ids = await get_user_group_ids(db, user.id)
    fleet = await _visible_fleet(db, user, fleet_id, user_group_ids)
    return (await _respond(db, user, [fleet], user_group_ids))[0]


@router.put("/{fleet_id}", response_model=FleetResponse)
async def update_fleet(
    fleet_id: str,
    body: FleetUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Update a fleet (owner, ``config`` grantee or admin). The slug does not follow the name."""
    fleet = await _manageable_fleet(db, user, fleet_id)
    fields = body.model_fields_set
    _ensure_writable(fleet, fields)
    previous_level = effective_level(fleet.autonomy_level)
    try:
        if "autonomy_level" in fields and body.autonomy_level is not None:
            new_level = normalize_level(body.autonomy_level)
            # Lowering is open to whoever manages the fleet; raising is admin-only.
            ensure_level_change_allowed(previous=previous_level, new=new_level, is_admin=bool(user.is_admin))
            fleet.autonomy_level = new_level
        if "accepted_handover_types" in fields:
            fleet.accepted_handover_types = normalize_accepted_handover_types(body.accepted_handover_types)
        if "orchestrator_agent_id" in fields:
            orchestrator = (
                parse_uuid(body.orchestrator_agent_id, label="orchestrator_agent_id")
                if body.orchestrator_agent_id
                else None
            )
            await set_orchestrator(db, fleet, orchestrator)
        if body.health_check is not None:
            await set_fleet_health_check(db, fleet, body.health_check, actor=str(user.id))
        if body.member_defaults is not None:
            changed = set_member_defaults(fleet, body.member_defaults.model_dump(exclude_unset=True))
            if "organization_scope" in changed:
                await check_scope_organizations(db, fleet.organization_scope)
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    if body.name is not None:
        fleet.name = body.name
    if body.description is not None:
        fleet.description = body.description
    if body.icon is not None:
        fleet.icon = body.icon or FLEET_DEFAULT_ICON
    if body.color is not None:
        fleet.color = body.color
    if "skills" in fields:
        fleet.skills = [s.model_dump() for s in body.skills] if body.skills is not None else None
    if body.is_archived is not None:
        fleet.is_archived = body.is_archived
    # Hide the activity from non-admins (#11624): an administrator's switch.
    if body.hide_activity_from_non_admins is not None:
        if not user.is_admin:
            raise HTTPException(
                status_code=403,
                detail="Only admins can hide a fleet's activity from non-admins",
            )
        fleet.hide_activity_from_non_admins = body.hide_activity_from_non_admins
    # ``null`` is meaningful for both run defaults: it clears them.
    if "budget_config" in fields:
        fleet.budget_config = assert_budget_within_ceiling(
            body.budget_config,
            previous=fleet.budget_config,
            user=user,
            what=f"the fleet '{fleet.name}'",
        )
    if "max_iterations" in fields:
        fleet.max_iterations = clamp_max_iterations(body.max_iterations) if body.max_iterations is not None else None
    if "run_budget" in fields:
        fleet.run_budget = _run_budget_value(body.run_budget)
    await apply_group_share_fields(db, fleet, body, partial=True)
    await handle_company_managed_toggle(
        db,
        user,
        fleet,
        body.is_company_managed,
        agent_field=None,  # agents never bind a fleet by id
        keep_grants=wants_access_restriction(body, fleet),
    )
    await apply_user_share_fields(db, fleet, body, partial=True)
    if body.workspace_id is not None and body.workspace_id != str(fleet.workspace_id):
        workspace = await _host_workspace(db, user, body.workspace_id, company_managed=fleet.is_company_managed)
        fleet.workspace_id = workspace.id
    elif "is_company_managed" in fields:
        current = (await db.execute(select(Workspace).where(Workspace.id == fleet.workspace_id))).scalar_one()
        await lock_workspace_for_hosting(db, current)
        try:
            ensure_workspace_can_host(bool(fleet.is_company_managed), current)
        except FleetActionRefusedError as exc:
            raise _refused(exc) from None
    new_owner_id = safe_uuid(body.user_id)
    if user.is_admin and new_owner_id is not None and new_owner_id != fleet.user_id:
        fleet.slug = await unique_fleet_slug(db, new_owner_id, fleet.slug)
    await apply_owner_transfer(
        db,
        actor=user,
        entity=fleet,
        new_owner_id=body.user_id,
        entity_type="fleet",
        entity_name=fleet.name,
        request=request,
    )
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="fleet",
        entity_id=fleet.id,
        entity_name=fleet.name,
        summary=f"Updated fleet '{fleet.name}'",
        details={"fields": sorted(fields)},
        request=request,
    )
    if effective_level(fleet.autonomy_level) != previous_level:
        await log_audit(
            db,
            user=user,
            action="autonomy_level_change",
            entity_type="fleet",
            entity_id=fleet.id,
            entity_name=fleet.name,
            summary=f"Set the autonomy level of fleet '{fleet.name}' to {fleet.autonomy_level}",
            details={"from": previous_level, "to": fleet.autonomy_level},
            request=request,
        )
    await db.commit()
    await db.refresh(fleet)
    return (await _respond(db, user, [fleet]))[0]


@router.delete("/{fleet_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_fleet(
    fleet_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Delete a fleet. Never deletes an agent or the workspace: members go back
    to the playground, connected rows naming this fleet cascade away."""
    fleet = await _manageable_fleet(db, user, fleet_id, action="delete")
    _ensure_writable(fleet)
    members = await fleet_members(db, fleet.id)
    await db.delete(fleet)
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="fleet",
        entity_id=fleet.id,
        entity_name=fleet.name,
        summary=f"Deleted fleet '{fleet.name}'",
        details={"slug": fleet.slug, "rows_removed": len(members)},
        request=request,
    )
    await db.commit()


# ── Members ──────────────────────────────────────────────────────────────────


async def _member_responses(
    db: AsyncSession, user: User, fleet: Fleet, rows: list[FleetMember], user_group_ids: list[str]
) -> list[FleetMemberResponse]:
    agent_ids = [r.agent_id for r in rows if r.agent_id is not None]
    fleet_ids = [r.target_fleet_id for r in rows if r.target_fleet_id is not None]
    agent_names = (
        dict(
            (
                await db.execute(
                    select(Agent.id, Agent.name).where(
                        Agent.id.in_(agent_ids),
                        user_visible_filter(Agent, user.id, user_group_ids, is_admin=user.is_admin),
                    )
                )
            ).all()
        )
        if agent_ids
        else {}
    )
    fleet_names = (
        dict(
            (
                await db.execute(
                    select(Fleet.id, Fleet.name).where(
                        Fleet.id.in_(fleet_ids),
                        user_visible_filter(Fleet, user.id, user_group_ids, is_admin=user.is_admin),
                    )
                )
            ).all()
        )
        if fleet_ids
        else {}
    )
    settings = {r.id: effective_member_settings(fleet, r) for r in rows}
    return [
        FleetMemberResponse(
            id=str(r.id),
            fleet_id=str(r.fleet_id),
            role=r.role,
            agent_id=str(r.agent_id) if r.agent_id else None,
            name=agent_names.get(r.agent_id, "") if r.agent_id else fleet_names.get(r.target_fleet_id, ""),
            target_fleet_id=str(r.target_fleet_id) if r.target_fleet_id else None,
            parent_member_id=str(r.parent_member_id) if r.parent_member_id else None,
            is_orchestrator=r.role == FLEET_ROLE_MEMBER and r.agent_id == fleet.orchestrator_agent_id,
            is_exposed=r.role == FLEET_ROLE_MEMBER
            and (bool(r.is_exposed) or r.agent_id == fleet.orchestrator_agent_id),
            exposed_explicitly=r.role == FLEET_ROLE_MEMBER and bool(r.is_exposed),
            role_label=r.role_label or "",
            collaboration_instruction=settings[r.id].collaboration_instruction,
            context_inclusion=settings[r.id].context_inclusion,
            organization_scope=settings[r.id].organization_scope,
            overridden=list(settings[r.id].overridden),
            created_at=r.created_at.isoformat() if r.created_at else "",
        )
        for r in rows
    ]


@router.get("/{fleet_id}/members", response_model=list[FleetMemberResponse])
async def list_fleet_members(
    fleet_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """The fleet's rows: home members, then connected agents and fleets.

    A member the caller cannot see keeps its row with an empty name (drawn as
    a placeholder): sharing a fleet gives nothing on the agents themselves.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    fleet = await _visible_fleet(db, user, fleet_id, user_group_ids)
    return await _member_responses(db, user, fleet, await fleet_members(db, fleet.id), user_group_ids)


@router.post("/{fleet_id}/members", response_model=FleetMemberResponse, status_code=status.HTTP_201_CREATED)
async def add_fleet_member(
    fleet_id: str,
    body: FleetMemberCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Add a home member (manage rights on the agent) or a connected agent or
    fleet (the right to see it)."""
    user_group_ids = await get_user_group_ids(db, user.id)
    fleet = await _manageable_fleet(db, user, fleet_id)
    _ensure_writable(fleet)
    try:
        if body.role == FLEET_ROLE_MEMBER:
            if not body.agent_id or body.target_fleet_id:
                raise FleetActionRefusedError(422, "A member is an agent: give agent_id only.")
            agent = await get_manageable_entity(
                db,
                Agent,
                user,
                body.agent_id,
                action="add to a fleet",
                entity_label="Agent",
                user_group_ids=user_group_ids,
            )
            row = await add_home_member(
                db,
                fleet,
                agent,
                parent_member_id=parse_uuid(body.parent_member_id, label="parent_member_id")
                if body.parent_member_id
                else None,
                is_exposed=body.is_exposed,
            )
            row.role_label = body.role_label.strip()
            target_name = agent.name
        else:
            if body.role_label.strip():
                raise FleetActionRefusedError(400, "A role label belongs to a home member.")
            if body.parent_member_id or body.is_exposed:
                raise FleetActionRefusedError(
                    400, "A connected agent has no place in the hierarchy and is not exposed."
                )
            agent = (
                await get_user_agent(db, user.id, body.agent_id, user_group_ids, is_admin=user.is_admin)
                if body.agent_id
                else None
            )
            target = (
                await _visible_fleet(db, user, body.target_fleet_id, user_group_ids) if body.target_fleet_id else None
            )
            row = await add_connected(
                db,
                fleet,
                agent=agent,
                target_fleet=target,
                collaboration_instruction=body.collaboration_instruction,
                context_inclusion=body.context_inclusion,
            )
            target_name = agent.name if agent else (target.name if target else "")
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="fleet",
        entity_id=fleet.id,
        entity_name=fleet.name,
        summary=f"Added '{target_name}' to fleet '{fleet.name}' as {body.role}",
        details={"member_id": str(row.id), "role": row.role},
        request=request,
    )
    await db.commit()
    await db.refresh(row)
    return (await _member_responses(db, user, fleet, [row], user_group_ids))[0]


@router.patch("/{fleet_id}/members/{member_id}", response_model=FleetMemberResponse)
async def update_fleet_member(
    fleet_id: str,
    member_id: str,
    body: FleetMemberUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Move a member in the hierarchy, expose it, or edit a connected link."""
    user_group_ids = await get_user_group_ids(db, user.id)
    fleet = await _manageable_fleet(db, user, fleet_id)
    _ensure_writable(fleet)
    fields = {name: getattr(body, name) for name in body.model_fields_set}
    if fields.get("organization_scope") is not None:
        fields["organization_scope"] = body.organization_scope.model_dump(exclude_none=True)  # type: ignore[union-attr]
    if fields.get("parent_member_id"):
        fields["parent_member_id"] = parse_uuid(fields["parent_member_id"], label="parent_member_id")
    try:
        row = await update_member(
            db, fleet, parse_path_id(member_id, not_found_detail="Fleet member not found"), fields=fields
        )
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    await db.commit()
    await db.refresh(row)
    return (await _member_responses(db, user, fleet, [row], user_group_ids))[0]


# --- Capability slots (#914 slice 2) ------------------------------------------
#
# A slot binds an integration on the member's AGENT, never on the fleet: on a
# built-in fleet that is the agent's customer layer
# (``services.builtin_customer_layer``), which the seed never rewrites, so the
# fleet's own write lock (``_ensure_writable``) does not apply. The agent's
# manage rule does: company-managed takes an administrator.


async def _connected_integrations(
    db: AsyncSession, user: User, user_group_ids: list[str], *, include_unconnected: bool = False
) -> list[Any]:
    """The integrations the reader can see, connected ones only unless *include_unconnected*.

    The read view passes *include_unconnected* so a visible integration of a
    capability that is not connected yet is shown as blocked with its reason
    (#1373); every write path binds connected integrations only.
    """
    from app.models.integration import Integration
    from app.services.fleet_capability_slots import IntegrationRef
    from app.services.integration_state import INTEGRATION_CONNECTED

    query = user_visible_query(Integration, user.id, user_group_ids, is_admin=user.is_admin)
    if not include_unconnected:
        query = query.where(Integration.status == INTEGRATION_CONNECTED)
    rows = (await db.execute(query)).scalars().all()
    return [
        IntegrationRef(
            integration_id=str(r.id),
            provider=r.provider,
            name=r.name,
            company_managed=bool(r.is_company_managed),
            connected=r.status == INTEGRATION_CONNECTED,
        )
        for r in rows
    ]


def _slot_ref(i: Any) -> Any:
    from app.api.v1.schemas.fleet import FleetSlotIntegration

    return FleetSlotIntegration(integration_id=i.integration_id, provider=i.provider, name=i.name)


def _blocked_ref(i: Any) -> Any:
    from app.api.v1.schemas.fleet import FleetSlotBlockedIntegration

    return FleetSlotBlockedIntegration(
        integration_id=i.integration_id, provider=i.provider, name=i.name, reason=i.reason or "not_connected"
    )


def _builtin_model(builtin: Any) -> Any:
    from app.api.v1.schemas.fleet import FleetSlotBuiltin

    if builtin is None:
        return None
    return FleetSlotBuiltin(key=builtin.key, label=builtin.label, always_on=builtin.always_on)


def _slot_model(state: Any) -> Any:
    from app.api.v1.schemas.fleet import FleetCapabilitySlot

    return FleetCapabilitySlot(
        capability=state.capability,
        label=state.label,
        state=state.state,
        bound=[_slot_ref(i) for i in state.bound],
        candidates=[_slot_ref(i) for i in state.candidates],
        builtin=_builtin_model(state.builtin),
        blocked=[_blocked_ref(i) for i in state.blocked],
    )


def _type_models(members: list[FleetMemberSlots], connected: list[Any]) -> list[Any]:
    """The member slots grouped by capability (#1201): one row per type."""
    from app.api.v1.schemas.fleet import FleetCapabilityType, FleetCapabilityTypeMember
    from app.services.fleet_capability_slots import (
        BUILTIN_OPTIONS,
        IntegrationRef,
        SlotState,
        TypeMember,
        type_states,
    )

    by_id = {i.integration_id: i for i in connected}

    def _ref(i: Any) -> IntegrationRef:
        return by_id.get(i.integration_id) or IntegrationRef(
            integration_id=i.integration_id, provider=i.provider, name=i.name
        )

    pairs = [
        (
            TypeMember(
                member_id=m.member_id,
                agent_id=m.agent_id,
                name=m.name,
                role_label=m.role_label,
                can_edit=m.can_edit,
                bound=[],
                custom=False,
            ),
            [
                SlotState(
                    capability=s.capability,
                    label=s.label,
                    state=s.state,
                    bound=[_ref(i) for i in s.bound],
                    candidates=[_ref(i) for i in s.candidates],
                    builtin=BUILTIN_OPTIONS.get(s.capability),
                    blocked=[
                        IntegrationRef(
                            integration_id=i.integration_id, provider=i.provider, name=i.name, reason=i.reason
                        )
                        for i in s.blocked
                    ],
                )
                for s in m.slots
            ],
        )
        for m in members
    ]
    return [
        FleetCapabilityType(
            capability=t.capability,
            label=t.label,
            state=t.state,
            choice=[_slot_ref(i) for i in t.choice],
            candidates=[_slot_ref(i) for i in t.candidates],
            members=[
                FleetCapabilityTypeMember(
                    member_id=m.member_id,
                    agent_id=m.agent_id,
                    name=m.name,
                    role_label=m.role_label,
                    can_edit=m.can_edit,
                    bound=[_slot_ref(i) for i in m.bound],
                    custom=m.custom,
                )
                for m in t.members
            ],
            builtin=_builtin_model(t.builtin),
            blocked=[_blocked_ref(i) for i in t.blocked],
        )
        for t in type_states(pairs)
    ]


async def _member_slots(
    db: AsyncSession, user: User, user_group_ids: list[str], row: FleetMember, agent: Agent, connected: list[Any]
) -> FleetMemberSlots:
    from app.api.v1.integration_catalog import PROVIDER_CATALOG
    from app.services.fleet_capability_slots import slot_states

    states = slot_states(
        row.role_label,
        agent.integration_bindings,
        connected,
        PROVIDER_CATALOG,
        company_managed_only=bool(agent.is_company_managed),
    )
    return FleetMemberSlots(
        member_id=str(row.id),
        agent_id=str(agent.id),
        name=agent.name,
        role_label=row.role_label or "",
        can_edit=can_manage(user, agent, user_group_ids),
        slots=[_slot_model(s) for s in states],
    )


@router.get("/{fleet_id}/capability-slots", response_model=FleetCapabilitySlotsResponse)
async def get_fleet_capability_slots(
    fleet_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
) -> FleetCapabilitySlotsResponse:
    """Each home member's capability slots: filled, empty or unavailable (#914 slice 2).

    Read only: no model call, no quota, no run. A member whose role declares
    no slot, or whose agent the reader cannot see, is left out.
    """
    from app.services.fleet_capability_slots import slots_for_role

    user_group_ids = await get_user_group_ids(db, user.id)
    fleet = await _visible_fleet(db, user, fleet_id, user_group_ids)
    rows = [
        r
        for r in await fleet_members(db, fleet.id)
        if r.role == FLEET_ROLE_MEMBER and r.agent_id is not None and slots_for_role(r.role_label)
    ]
    agents = (
        {
            a.id: a
            for a in (
                await db.execute(
                    select(Agent).where(
                        Agent.id.in_([r.agent_id for r in rows]),
                        user_visible_filter(Agent, user.id, user_group_ids, is_admin=user.is_admin),
                    )
                )
            )
            .scalars()
            .all()
        }
        if rows
        else {}
    )
    # Every visible integration, so one of the right type that cannot be bound is named with its reason (#1373).
    connected = await _connected_integrations(db, user, user_group_ids, include_unconnected=True) if agents else []
    members = [
        await _member_slots(db, user, user_group_ids, r, agents[r.agent_id], connected)
        for r in rows
        if r.agent_id in agents
    ]
    return FleetCapabilitySlotsResponse(fleet_id=str(fleet.id), members=members, types=_type_models(members, connected))


async def _slot_member_agent(
    db: AsyncSession, user: User, user_group_ids: list[str], fleet_id: str, member_id: str
) -> tuple[FleetMember, Agent]:
    fleet = await _visible_fleet(db, user, fleet_id, user_group_ids)
    member_uuid = parse_path_id(member_id, not_found_detail="Fleet member not found")
    row = (
        await db.execute(
            select(FleetMember).where(
                FleetMember.id == member_uuid,
                FleetMember.fleet_id == fleet.id,
                FleetMember.role == FLEET_ROLE_MEMBER,
            )
        )
    ).scalar_one_or_none()
    agent = (
        (
            await db.execute(
                select(Agent).where(
                    Agent.id == row.agent_id,
                    user_visible_filter(Agent, user.id, user_group_ids, is_admin=user.is_admin),
                )
            )
        ).scalar_one_or_none()
        if row is not None and row.agent_id is not None
        else None
    )
    if row is None or agent is None:
        raise HTTPException(status_code=404, detail="Fleet member not found")
    if not can_manage(user, agent, user_group_ids):
        detail = (
            "Only admins can edit company-managed agents"
            if agent.is_company_managed
            else "You don't have permission to edit this agent."
        )
        raise HTTPException(status_code=403, detail=detail)
    return row, agent


async def _audit_slot_change(
    db: AsyncSession, user: User, agent: Agent, *, verb: str, capability: str, integration_id: str
) -> None:
    # The seed never writes ``integration_bindings``, so nothing is recorded in
    # ``customer_fields`` for it (as on ``PUT /agents/{id}``).
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="agent",
        entity_id=agent.id,
        entity_name=agent.name,
        summary=f"{verb} an integration on the '{capability}' slot of '{agent.name}'",
        details={"capability": capability, "integration_id": integration_id, "slug": agent.slug},
    )


@router.post("/{fleet_id}/members/{member_id}/slots/{capability}", response_model=FleetMemberSlots)
async def bind_fleet_member_slot(
    fleet_id: str,
    member_id: str,
    capability: str,
    body: FleetSlotBind,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
) -> FleetMemberSlots:
    """Bind a connected integration to a member's capability slot, in one click (#914 slice 2).

    Adds it to the member agent's ``integration_bindings``; on a built-in that
    is the customer layer, kept by every seed run.
    """
    from app.api.v1.integration_catalog import PROVIDER_CATALOG
    from app.services.fleet_capability_slots import SlotBindingRefusedError, bindings_with

    user_group_ids = await get_user_group_ids(db, user.id)
    row, agent = await _slot_member_agent(db, user, user_group_ids, fleet_id, member_id)
    connected = await _connected_integrations(db, user, user_group_ids)
    wanted = parse_uuid(body.integration_id, label="integration_id")
    integration = next((i for i in connected if i.integration_id == str(wanted)), None)
    if integration is None:
        raise HTTPException(status_code=404, detail="Integration not found or not connected")
    try:
        agent.integration_bindings = bindings_with(
            row.role_label,
            capability,
            agent.integration_bindings,
            integration,
            PROVIDER_CATALOG,
            company_managed_only=bool(agent.is_company_managed),
        )
    except SlotBindingRefusedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    await _audit_slot_change(
        db, user, agent, verb="Bound", capability=capability, integration_id=integration.integration_id
    )
    await db.commit()
    await db.refresh(agent)
    return await _member_slots(db, user, user_group_ids, row, agent, connected)


@router.delete("/{fleet_id}/members/{member_id}/slots/{capability}/{integration_id}", response_model=FleetMemberSlots)
async def unbind_fleet_member_slot(
    fleet_id: str,
    member_id: str,
    capability: str,
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
) -> FleetMemberSlots:
    """Unbind an integration from a member's capability slot (#914 slice 2)."""
    from app.api.v1.integration_catalog import PROVIDER_CATALOG
    from app.services.fleet_capability_slots import SlotBindingRefusedError, bindings_without

    user_group_ids = await get_user_group_ids(db, user.id)
    row, agent = await _slot_member_agent(db, user, user_group_ids, fleet_id, member_id)
    target = str(parse_path_id(integration_id, not_found_detail="This integration is not bound to that slot."))
    try:
        agent.integration_bindings = bindings_without(
            row.role_label, capability, agent.integration_bindings, target, PROVIDER_CATALOG
        )
    except SlotBindingRefusedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    await _audit_slot_change(db, user, agent, verb="Unbound", capability=capability, integration_id=target)
    await db.commit()
    await db.refresh(agent)
    connected = await _connected_integrations(db, user, user_group_ids)
    return await _member_slots(db, user, user_group_ids, row, agent, connected)


def _wanted_integrations(integration_ids: list[str], connected: list[Any]) -> list[Any]:
    """The connected integrations *integration_ids* name, in order; 404 on one not connected."""
    by_id = {i.integration_id: i for i in connected}
    out: list[Any] = []
    for raw in dict.fromkeys(integration_ids):
        integration = by_id.get(str(parse_uuid(raw, label="integration_id")))
        if integration is None:
            raise HTTPException(status_code=404, detail="Integration not found or not connected")
        out.append(integration)
    return out


@router.put("/{fleet_id}/members/{member_id}/slots/{capability}", response_model=FleetMemberSlots)
async def set_fleet_member_slot(
    fleet_id: str,
    member_id: str,
    capability: str,
    body: FleetSlotSet,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
) -> FleetMemberSlots:
    """Bind exactly these integrations on one member's slot: an override or a reset to the fleet choice (#1201).

    The capability's other bindings on that agent are removed, every other
    provider's binding is kept. Same checks as a one-click bind.
    """
    from app.api.v1.integration_catalog import PROVIDER_CATALOG
    from app.services.fleet_capability_slots import SlotBindingRefusedError, bindings_set

    user_group_ids = await get_user_group_ids(db, user.id)
    row, agent = await _slot_member_agent(db, user, user_group_ids, fleet_id, member_id)
    connected = await _connected_integrations(db, user, user_group_ids)
    wanted = _wanted_integrations(body.integration_ids, connected)
    try:
        agent.integration_bindings = bindings_set(
            row.role_label,
            capability,
            agent.integration_bindings,
            wanted,
            PROVIDER_CATALOG,
            company_managed_only=bool(agent.is_company_managed),
        )
    except SlotBindingRefusedError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    await _audit_slot_change(
        db, user, agent, verb="Set", capability=capability, integration_id=",".join(i.integration_id for i in wanted)
    )
    await db.commit()
    await db.refresh(agent)
    return await _member_slots(db, user, user_group_ids, row, agent, connected)


@router.put("/{fleet_id}/capabilities/{capability}", response_model=FleetCapabilitySlotsResponse)
async def set_fleet_capability(
    fleet_id: str,
    capability: str,
    body: FleetCapabilityTypeSet,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
) -> FleetCapabilitySlotsResponse:
    """The fleet choice of a capability, written on every member slot of that type (#1201).

    Each member the reader may edit gets exactly *integration_ids* on that
    slot (same checks as a one-click bind, the company-managed rule per
    agent). A member whose slot is custom keeps its override unless
    *override_custom*. Every check runs before the first write: one refusal
    writes nothing. A built-in option is never stored: an empty list leaves
    only it active.
    """
    from app.api.v1.integration_catalog import PROVIDER_CATALOG
    from app.services.fleet_capability_slots import (
        CAPABILITIES,
        SlotBindingRefusedError,
        bindings_set,
        members_to_write,
    )

    if capability not in CAPABILITIES:
        raise HTTPException(status_code=422, detail=f"Unknown capability '{capability}'.")
    view = await get_fleet_capability_slots(fleet_id, user=user, db=db, _gate=None)
    state = next((t for t in view.types if t.capability == capability), None)
    if state is None:
        raise HTTPException(status_code=422, detail=f"No member of this fleet has a '{capability}' slot.")
    user_group_ids = await get_user_group_ids(db, user.id)
    connected = await _connected_integrations(db, user, user_group_ids)
    wanted = _wanted_integrations(body.integration_ids, connected)
    targets = members_to_write(state, override_custom=body.override_custom)
    planned: list[tuple[Agent, dict[str, list[str]]]] = []
    for member_id in targets:
        row, agent = await _slot_member_agent(db, user, user_group_ids, fleet_id, member_id)
        try:
            planned.append(
                (
                    agent,
                    bindings_set(
                        row.role_label,
                        capability,
                        agent.integration_bindings,
                        wanted,
                        PROVIDER_CATALOG,
                        company_managed_only=bool(agent.is_company_managed),
                    ),
                )
            )
        except SlotBindingRefusedError as exc:
            raise HTTPException(status_code=exc.status_code, detail=f"{agent.name}: {exc.detail}") from None
    joined = ",".join(i.integration_id for i in wanted)
    for agent, bindings in planned:
        agent.integration_bindings = bindings
        await _audit_slot_change(db, user, agent, verb="Set", capability=capability, integration_id=joined)
    await db.commit()
    return await get_fleet_capability_slots(fleet_id, user=user, db=db, _gate=None)


@router.post("/{fleet_id}/members/{member_id}/instruction-draft", response_model=FleetMemberInstructionDraftResponse)
async def draft_member_instruction_endpoint(
    fleet_id: str,
    member_id: str,
    body: FleetMemberInstructionDraftRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    # A model call on the caller's own model, run when the person clicks
    # "Generate with AI" in the instruction editor: gated as /generate is.
    _gate: None = Depends(require_agentic_ai),
):
    """An AI draft of a connected member's collaboration instruction, nothing saved.

    Review it in the editor, then save it with ``PATCH /fleets/{id}/members/{member_id}``.
    """
    from app.services.fleet_instruction_draft import InstructionDraftError, draft_member_instruction

    fleet = await _manageable_fleet(db, user, fleet_id)
    member_uuid = parse_path_id(member_id, not_found_detail="Fleet member not found")
    try:
        draft = await draft_member_instruction(
            db, user.id, fleet, member_uuid, brief=body.brief, current_text=body.current_text
        )
    except InstructionDraftError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from None
    except FleetActionRefusedError as exc:
        await db.rollback()
        raise _refused(exc) from None
    await db.rollback()
    return FleetMemberInstructionDraftResponse(text=draft.text, truncated=draft.truncated)


@router.post("/{fleet_id}/members/bulk", response_model=list[FleetMemberResponse])
async def bulk_update_fleet_members(
    fleet_id: str,
    body: FleetMembersBulkUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Set the member settings of several connected rows in one transaction.

    All or nothing: a row of another fleet or a home member refuses the whole
    call, naming the rows. ``null`` in ``set`` resets the field to the fleet
    default. One audit event names every row.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    fleet = await _manageable_fleet(db, user, fleet_id)
    _ensure_writable(fleet)
    ids = [parse_uuid(m, label="member_ids") for m in body.member_ids]
    fields = body.set.model_dump(exclude_unset=True)
    try:
        rows = await bulk_update_members(db, fleet, ids, fields=fields)
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="fleet",
        entity_id=fleet.id,
        entity_name=fleet.name,
        summary=f"Changed the settings of {len(rows)} member(s) of fleet '{fleet.name}'",
        details={"member_ids": [str(r.id) for r in rows], "fields": sorted(fields)},
        request=request,
    )
    await db.commit()
    for row in rows:
        await db.refresh(row)
    return await _member_responses(db, user, fleet, rows, user_group_ids)


@router.delete("/{fleet_id}/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_fleet_member(
    fleet_id: str,
    member_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Remove a row. The agent keeps every handover, assignment and binding."""
    fleet = await _manageable_fleet(db, user, fleet_id)
    _ensure_writable(fleet)
    try:
        row = await remove_member(db, fleet, parse_path_id(member_id, not_found_detail="Fleet member not found"))
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="fleet",
        entity_id=fleet.id,
        entity_name=fleet.name,
        summary=f"Removed a {row.role} row from fleet '{fleet.name}'",
        details={"member_id": str(row.id), "role": row.role},
        request=request,
    )
    await db.commit()


@router.post("/{fleet_id}/members/{member_id}/move", response_model=FleetMemberResponse)
async def move_fleet_member(
    fleet_id: str,
    member_id: str,
    body: FleetMemberMove,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Move a home member to another fleet in one transaction.

    Manage rights on both fleets and on the agent; a refused join leaves the
    member where it was, parent, exposure, role label and orchestrator
    included. Answers the new row in the target fleet.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    source = await _manageable_fleet(db, user, fleet_id)
    _ensure_writable(source)
    target = await _manageable_fleet(db, user, body.target_fleet_id)
    _ensure_writable(target)

    async def _agent(agent_id: uuid.UUID) -> Agent:
        return await get_manageable_entity(
            db,
            Agent,
            user,
            str(agent_id),
            action="move to another fleet",
            entity_label="Agent",
            user_group_ids=user_group_ids,
        )

    try:
        row = await move_member(
            db,
            source,
            parse_path_id(member_id, not_found_detail="Fleet member not found"),
            target,
            manageable_agent=_agent,
        )
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="fleet",
        entity_id=target.id,
        entity_name=target.name,
        summary=f"Moved a member from fleet '{source.name}' to fleet '{target.name}'",
        details={
            "member_id": str(row.id),
            "previous_member_id": member_id,
            "source_fleet_id": str(source.id),
            "agent_id": str(row.agent_id),
        },
        request=request,
    )
    await db.commit()
    await db.refresh(row)
    return (await _member_responses(db, user, target, [row], user_group_ids))[0]


# ── Exposure, card, boundary ─────────────────────────────────────────────────


@router.put("/{fleet_id}/exposure", response_model=list[str])
async def set_fleet_exposure(
    fleet_id: str,
    body: FleetExposureUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Replace the exposed home members; answers the exposed ids, orchestrator first."""
    fleet = await _manageable_fleet(db, user, fleet_id)
    _ensure_writable(fleet)
    try:
        exposed = await set_exposed_agents(db, fleet, [parse_uuid(a, label="agent_ids") for a in body.agent_ids])
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    await db.commit()
    return [str(a) for a in exposed]


@router.get("/{fleet_id}/card", response_model=FleetCardResponse)
async def get_fleet_card(
    fleet_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """The Fleet Card: what another fleet needs to call this one."""
    fleet = await _visible_fleet(db, user, fleet_id)
    return FleetCardResponse(**await build_fleet_card(db, fleet))


@router.get("/{fleet_id}/spec")
async def export_fleet_spec_endpoint(
    fleet_id: str,
    bundles: bool = Query(True, description="false: every member as a reference to its agent, no bundle."),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
) -> dict:
    """The fleet spec (``xtm-one/fleet/v1``): members as agent bundles, no secrets.

    ``bundles=false`` answers the structure only, every member a reference to
    its agent: a valid spec ``POST /apply`` adopts by slug.
    """
    from app.services.fleet_spec import export_fleet_spec

    user_group_ids = await get_user_group_ids(db, user.id)
    fleet = await _visible_fleet(db, user, fleet_id, user_group_ids)
    return await export_fleet_spec(db, fleet, user, user_group_ids, bundles=bundles)


@router.post("/{fleet_id}/fork", response_model=FleetForkResponse, status_code=status.HTTP_201_CREATED)
async def fork_fleet_endpoint(
    fleet_id: str,
    body: FleetFork,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    # A fork creates agents through the agent duplicate path, gated like it.
    _gate: None = Depends(require_agentic_ai),
):
    """Fork any fleet the caller sees, a built-in one included, as a personal copy.

    Every copied assignment is created switched off; the fork starts no run.
    """
    from app.services.fleet_fork import fork_fleet

    user_group_ids = await get_user_group_ids(db, user.id)
    source = await _visible_fleet(db, user, fleet_id, user_group_ids)
    workspace_uuid = parse_uuid(body.workspace_id, label="workspace_id") if body.workspace_id else None
    try:
        result = await fork_fleet(
            db,
            source,
            user,
            user_group_ids,
            name=body.name,
            workspace_id=workspace_uuid,
            members=body.members,
        )
    except FleetActionRefusedError as exc:
        await db.rollback()
        raise _refused(exc) from None
    fork = result.fleet
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="fleet",
        entity_id=fork.id,
        entity_name=fork.name,
        summary=f"Forked the fleet '{source.name}' as '{fork.name}'",
        details={
            "source_fleet_id": str(source.id),
            "source_is_builtin": bool(source.is_builtin),
            "members": body.members,
            "agents_copied": len(result.copied_agents),
            "assignments_copied": result.assignments_copied,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(fork)
    # A fork of an undocumented fleet gets its first documentation (#10316); a copied one is kept.
    await schedule_documentation_on_create(db, fork.id, user.id)
    responses = await _respond(db, user, [fork], user_group_ids)
    return FleetForkResponse(
        fleet=responses[0],
        copied_agent_ids={str(src): str(copy.id) for src, copy in result.copied_agents.items()},
        kept_agent_ids=[str(a) for a in result.kept_agents],
        assignments_copied=result.assignments_copied,
        warnings=result.warnings,
    )


@router.get("/{fleet_id}/source-changes", response_model=FleetSourceChangesResponse)
async def fleet_source_changes_endpoint(
    fleet_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Whether the source of a fork changed since the fork, and the structural diff.

    Read only: names slugs, ids and the fields that moved, never a persona.
    """
    from app.services.fleet_fork import source_changes

    fleet = await _visible_fleet(db, user, fleet_id)
    found = await source_changes(db, fleet)
    return FleetSourceChangesResponse(
        is_fork=found.is_fork,
        source_exists=found.source_exists,
        changed=found.changed,
        diff_available=found.diff_available,
        changes=[FleetSourceChange(**c) for c in found.changes],
        successor=found.successor,
    )


async def _documentation_response(db: AsyncSession, user: User, fleet: Fleet) -> FleetDocumentationResponse:
    from app.services.fleet_about import evaluate_prerequisites, live_prerequisites
    from app.services.fleet_docs import DOC_SECTIONS, documentation_view

    group_ids = await get_user_group_ids(db, user.id)
    # Platform Health takes no Generate nor suggestion write (#101 S1b, UX r1 B3).
    manager = _can_manage_fleet(user, fleet, group_ids)
    view = await documentation_view(db, fleet, can_edit=manager)
    if not view.version and view.structured is None:
        from app.services.fleet_doc_generation import configuration_facts

        # No version yet (#10316): the page draws the facts computed from the configuration, never a blank page.
        view.structured = await configuration_facts(db, fleet)
    documented = (view.structured or {}).get("prerequisites") or []
    states = await evaluate_prerequisites(db, user, documented, group_ids=group_ids, fleet=fleet)
    live = await live_prerequisites(db, user, fleet, documented, group_ids=group_ids, can_confirm=manager)
    _lock_fixes_on_health_fleet(fleet, live)
    from app.services.fleet_gate_proposals import fleet_effective_gates

    effective = await fleet_effective_gates(db, fleet.id)
    catalog = gate_catalog(fleet.autonomy_level, effective[1] if effective else [])
    # Any generation running on the server (#10316, #10817): the page derives its spinner from it on mount.
    generation = await generation_state(fleet.id)
    return FleetDocumentationResponse(
        section_keys=list(DOC_SECTIONS),
        sections=view.sections,
        version=view.version,
        updated_at=view.updated_at,
        updated_by=view.updated_by,
        editable=view.editable,
        out_of_date=view.out_of_date,
        local=view.local,
        restored_from=view.restored_from,
        # A step's approval badge follows the gate it names at the live level and pins (#1074).
        structured=with_live_step_gates(view.structured, catalog),
        prerequisite_states=[FleetPrerequisiteState(**vars(s)) for s in states],
        live_prerequisites=[FleetLivePrerequisite(**vars(line)) for line in live],
        autonomy_level=effective_level(fleet.autonomy_level),
        # The gates the runs enforce, computed ones included (#10153): the page says what holds.
        autonomy_catalog=catalog,
        findings=view.findings,
        dismissed_findings=view.dismissed_findings,
        generating=generation.generating,
        generating_started_at=generation.started_at,
        generation_failed=generation.failure,
    )


@router.get("/{fleet_id}/documentation", response_model=FleetDocumentationResponse)
async def get_fleet_documentation_endpoint(
    fleet_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """A fleet's documentation, built-in or custom; read only, starts no run."""
    fleet = await _visible_fleet(db, user, fleet_id)
    return await _documentation_response(db, user, fleet)


@router.post("/{fleet_id}/documentation/regenerate", response_model=FleetDocumentationResponse)
async def regenerate_fleet_documentation_endpoint(
    fleet_id: str,
    body: FleetDocumentationRegenerateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    # A model call on the caller's own model, run when the person clicks
    # "Regenerate documentation": gated as /generate is.
    _gate: None = Depends(require_agentic_ai),
):
    """Regenerate a fleet's documentation with AI and save it as its next version (#9768 S2).

    The documentation is never typed by hand: this is its one write path
    besides the seed and the shipped restore. One section (``section``) or all;
    on a built-in fleet the result is a local version the next boot keeps.

    The generation is recorded on the server while it runs (#10817): a reload,
    a second tab or a page opened again reads ``generating``; a click while one
    runs starts no second one and answers the documentation with ``generating``.
    """
    from app.services.fleet_doc_generation import FleetDocDraftError, check_section, regenerate_fleet_documentation

    fleet = await _manageable_fleet(db, user, fleet_id)
    try:
        # Refused before the claim (#393): a wrong name never shows as a failure on the About page.
        check_section(body.section)
    except FleetDocDraftError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    # Read once: a rollback below expires ``fleet``, and reading its id again in
    # the async ``finally`` would lazy-load outside the greenlet (MissingGreenlet).
    doc_fleet_id = fleet.id
    if not await claim_generation(doc_fleet_id):
        return await _documentation_response(db, user, fleet)
    failure: str | None = None
    pending: list[str] = []
    try:
        async with generation_running(doc_fleet_id):
            draft = await regenerate_fleet_documentation(
                db, user.id, fleet, section=body.section, base_version=body.base_version
            )
            await log_audit(
                db,
                user=user,
                action="update",
                entity_type="fleet",
                entity_id=fleet.id,
                entity_name=fleet.name,
                summary=f"Regenerated the documentation of fleet '{fleet.name}'",
                details={"documentation_version": (fleet.documentation or {}).get("version"), "section": body.section},
                request=request,
            )
            # Committed before the marker is cleared, so a poll never reads "done" on the old version.
            await db.commit()
            pending = list(draft.pending)
    except FleetDocDraftError as exc:
        failure = str(exc)
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except FleetActionRefusedError as exc:
        # A stale page is the clicker's own refusal, never a failure shown to the next reader.
        await db.rollback()
        raise _refused(exc) from None
    except Exception:
        failure = GENERATION_FAILED
        raise
    finally:
        await release_generation(doc_fleet_id, failure)
    # A section the provider failed on is filled in the background (#11292): the page polls `generating`.
    schedule_pending_sections(doc_fleet_id, user.id, pending)
    await db.refresh(fleet)
    return await _documentation_response(db, user, fleet)


@router.post("/{fleet_id}/documentation/restore-shipped", response_model=FleetDocumentationResponse)
async def restore_shipped_fleet_documentation_endpoint(
    fleet_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Bring back a built-in fleet's shipped documentation as its next version; no model call."""
    from app.services.fleet_docs import restore_shipped_documentation

    fleet = await _manageable_fleet(db, user, fleet_id)
    try:
        await restore_shipped_documentation(db, fleet, author=user.id)
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="fleet",
        entity_id=fleet.id,
        entity_name=fleet.name,
        summary=f"Restored the shipped documentation of fleet '{fleet.name}'",
        details={"documentation_version": (fleet.documentation or {}).get("version")},
        request=request,
    )
    await db.commit()
    await db.refresh(fleet)
    return await _documentation_response(db, user, fleet)


async def _approval_gates_response(db: AsyncSession, user: User, fleet: Fleet) -> FleetApprovalGatesResponse:
    """The stored or documented catalog, the proposals computed from the members, the approvals outside the level.

    Also the level and the effective catalog it acts on (#10153), the same one
    the runs read (``fleet_gate_proposals.fleet_effective_gates``).
    """
    from app.services.fleet_autonomy import customer_gates_of, effective_gates
    from app.services.fleet_gate_proposals import existing_approvals, fleet_configuration

    config = await fleet_configuration(db, fleet.id)
    proposals = config.proposals()
    stored = getattr(fleet, "approval_gates", None)
    pins = getattr(fleet, "approval_pins", None)
    computed_on = not fleet.is_builtin and pins is not None
    level = effective_level(fleet.autonomy_level)
    group_ids = await get_user_group_ids(db, user.id)
    customer = getattr(fleet, "customer_gates", None) if fleet.is_builtin else None
    return FleetApprovalGatesResponse(
        catalog=[dict(g) for g in fleet_gates(stored, fleet.documentation)],
        stored=isinstance(stored, list),
        proposals=proposals,
        existing_approvals=existing_approvals(config.agents, config.assignments),
        editable=not fleet.is_builtin and can_manage(user, fleet, group_ids),
        # A built-in fleet takes added gates in its customer layer (#916).
        customer_gates_editable=bool(fleet.is_builtin) and can_manage(user, fleet, group_ids),
        customer_gates=[dict(g) for g in customer_gates_of(customer)],
        autonomy_level=level,
        effective_catalog=gate_catalog(
            level, effective_gates(stored, fleet.documentation, proposals if computed_on else None, pins, customer)
        ),
        computed_catalog_on=computed_on,
        instructions_position=getattr(fleet, "instructions_position", None),
        instructions_position_announced=bool(getattr(fleet, "instructions_position_announced", None)),
        enforce_at_tool_level=bool(getattr(fleet, "enforce_gates_at_tool_level", False)),
    )


@router.get("/{fleet_id}/approval-gates", response_model=FleetApprovalGatesResponse)
async def get_fleet_approval_gates_endpoint(
    fleet_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """A fleet's approval gate catalog and the gates proposed from its configuration (#10014).

    Read only: no model call, no quota, no run.
    """
    fleet = await _visible_fleet(db, user, fleet_id)
    return await _approval_gates_response(db, user, fleet)


TOOL_LEVEL_ENFORCEMENT_REFUSAL = (
    "Only an administrator can turn the platform's tool-level pauses of a fleet's gates on or off."
)


@router.put("/{fleet_id}/autonomy", response_model=FleetApprovalGatesResponse)
async def apply_fleet_autonomy_endpoint(
    fleet_id: str,
    body: FleetAutonomyApplyRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Apply a level and the per-action pins in one transaction (#10153 C3, C4): one click of the cursor.

    Raising the level, pinning an action to run alone or removing an
    "Always wait" pin lets an action run with less approval: administrators
    only (403). A built-in fleet takes its pins too (#916, CTEM-34): they are
    the customer's, the seed never writes them, so they survive an upgrade.
    On a custom fleet it turns the computed catalog on. One audit row
    ``autonomy_level_change`` with the level and pins before and after.
    """
    from app.services.fleet_autonomy import PIN_RUN_REFUSAL, clean_pins, pins_lower_approval

    fleet = await _manageable_fleet(db, user, fleet_id)
    fields = {"autonomy_level"} if body.pins is None else {"autonomy_level", "approval_pins"}
    if body.enforce_at_tool_level is not None:
        fields.add("enforce_gates_at_tool_level")
    _ensure_writable(fleet, fields)
    previous_enforced = bool(getattr(fleet, "enforce_gates_at_tool_level", False))
    new_enforced = previous_enforced if body.enforce_at_tool_level is None else body.enforce_at_tool_level
    # Tool-level enforcement is an administrator's opt-in (#1467), both ways.
    if new_enforced != previous_enforced and not user.is_admin:
        raise HTTPException(status_code=403, detail=TOOL_LEVEL_ENFORCEMENT_REFUSAL)
    previous_level = effective_level(fleet.autonomy_level)
    previous_pins = clean_pins(getattr(fleet, "approval_pins", None))
    new_pins = clean_pins(body.pins) if body.pins is not None else previous_pins
    try:
        new_level = normalize_level(body.level)
        ensure_level_change_allowed(previous=previous_level, new=new_level, is_admin=bool(user.is_admin))
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    if pins_lower_approval(previous_pins, new_pins) and not user.is_admin:
        raise HTTPException(status_code=403, detail=PIN_RUN_REFUSAL)
    fleet.autonomy_level = new_level
    fleet.enforce_gates_at_tool_level = new_enforced
    if not fleet.is_builtin or body.pins is not None:
        fleet.approval_pins = new_pins
    await log_audit(
        db,
        user=user,
        action="autonomy_level_change",
        entity_type="fleet",
        entity_id=fleet.id,
        entity_name=fleet.name,
        summary=f"Set the autonomy level of fleet '{fleet.name}' to {new_level}",
        details={
            "from": previous_level,
            "to": new_level,
            "pins_from": previous_pins,
            "pins_to": new_pins,
            "tool_level_from": previous_enforced,
            "tool_level_to": new_enforced,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(fleet)
    return await _approval_gates_response(db, user, fleet)


@router.put("/{fleet_id}/instructions-position", response_model=FleetApprovalGatesResponse)
async def set_fleet_instructions_position_endpoint(
    fleet_id: str,
    body: FleetInstructionsPositionRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Set where a fleet's instructions put it on the four-level scale (#10168, B2-I of #10000).

    Never a platform gate: no tool call pauses and no level changes. A move is
    announced to the members (one line in their run prompt) unless
    ``announce`` is false (an initial reading, display only), and audited
    (``instructions_position_change``). Moving toward more autonomy is
    administrator only (403); lowering is open. A built-in fleet is refused
    like any field outside its writable set.
    """
    from app.services.fleet_autonomy import ensure_instructions_change_allowed, normalize_instructions_position

    fleet = await _manageable_fleet(db, user, fleet_id)
    _ensure_writable(fleet, {"instructions_position"})
    previous = getattr(fleet, "instructions_position", None)
    try:
        new = normalize_instructions_position(body.position)
        ensure_instructions_change_allowed(previous=previous, new=new, is_admin=bool(user.is_admin))
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    fleet.instructions_position = new
    fleet.instructions_position_announced = new is not None and body.announce
    await log_audit(
        db,
        user=user,
        action="instructions_position_change",
        entity_type="fleet",
        entity_id=fleet.id,
        entity_name=fleet.name,
        summary=f"Set the instructions position of fleet '{fleet.name}' to {new or 'not set'}",
        details={"from": previous, "to": new, "announced": new is not None and body.announce},
        request=request,
    )
    await db.commit()
    await db.refresh(fleet)
    return await _approval_gates_response(db, user, fleet)


@router.put("/{fleet_id}/approval-gates", response_model=FleetApprovalGatesResponse)
async def update_fleet_approval_gates_endpoint(
    fleet_id: str,
    body: FleetApprovalGatesUpdateRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Store a fleet's approval gate catalog (#10014): the gates its autonomy level acts on.

    On a built-in fleet (#916, CTEM-34) the body is the gates a person adds
    to the seeded catalog: stored in its customer layer (``customer_gates``),
    never written by the seed, so they survive an upgrade; a seeded key is
    refused (a pin moves a seeded gate). Removing a stored gate, a tool of one
    or lowering its impact lets an action run with less approval:
    administrators only, as raising the level. Audit logged.
    """
    from app.services.fleet_autonomy import customer_gates_of
    from app.services.fleet_docs import clean_structured
    from app.services.fleet_gate_proposals import removes_a_gate

    fleet = await _manageable_fleet(db, user, fleet_id)
    builtin = bool(fleet.is_builtin)
    _ensure_writable(fleet, {"customer_gates" if builtin else "approval_gates"})
    try:
        cleaned = (clean_structured({"gates": body.gates}) or {}).get("gates") or []
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    keys = [str(g.get("key") or "") for g in cleaned]
    if any(not k for k in keys) or len(set(keys)) != len(keys):
        raise HTTPException(status_code=422, detail="Every approval gate needs its own key.")
    if builtin:
        seeded = {str(g.get("key") or "") for g in fleet_gates(getattr(fleet, "approval_gates", None), None)}
        if seeded & set(keys):
            raise HTTPException(
                status_code=422,
                detail="A built-in fleet's seeded gate cannot be redeclared; pin it to wait or run instead.",
            )
        previous = [dict(g) for g in customer_gates_of(getattr(fleet, "customer_gates", None))]
    else:
        previous = [dict(g) for g in fleet_gates(getattr(fleet, "approval_gates", None), fleet.documentation)]
    if removes_a_gate(previous, cleaned) and not user.is_admin:
        raise HTTPException(
            status_code=403,
            detail="Only an administrator can remove an approval gate, a tool of one or lower its impact; adding is open.",
        )
    if builtin:
        fleet.customer_gates = cleaned or None
    else:
        fleet.approval_gates = cleaned
    await log_audit(
        db,
        user=user,
        action="approval_gates_change",
        entity_type="fleet",
        entity_id=fleet.id,
        entity_name=fleet.name,
        summary=f"Set the approval gates of fleet '{fleet.name}' ({len(cleaned)} gate(s))",
        details={"from": [g.get("key") for g in previous], "to": keys},
        request=request,
    )
    await db.commit()
    await db.refresh(fleet)
    return await _approval_gates_response(db, user, fleet)


@router.get("/{fleet_id}/documentation/history", response_model=FleetDocumentationHistoryResponse)
async def get_fleet_documentation_history_endpoint(
    fleet_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """The versions of a fleet's documentation, current first, without their texts; read only (#9768 S2)."""
    from app.services.fleet_docs import documentation_history

    fleet = await _visible_fleet(db, user, fleet_id)
    # Generator notes and the Regenerate flag are for the people who manage the fleet (#10246).
    managed = _can_manage_fleet(user, fleet, await get_user_group_ids(db, user.id))
    return FleetDocumentationHistoryResponse(
        versions=[
            FleetDocumentationVersion(**vars(entry)) for entry in documentation_history(fleet, with_notes=managed)
        ]
    )


@router.post("/{fleet_id}/documentation/findings/{finding_id}/dismiss", response_model=FleetDocumentationResponse)
async def dismiss_fleet_documentation_finding_endpoint(
    fleet_id: str,
    finding_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Hide one configuration suggestion while its member is unchanged (#10246, #10536); no model call."""
    from app.services.fleet_docs import dismiss_finding

    fleet = await _manageable_fleet(db, user, fleet_id)
    try:
        await dismiss_finding(db, fleet, finding_id[:40], by=str(user.id), by_name=user.full_name or "")
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="fleet",
        entity_id=fleet.id,
        entity_name=fleet.name,
        summary=f"Dismissed a documentation suggestion of fleet '{fleet.name}'",
        details={"finding": finding_id[:40]},
        request=request,
    )
    await db.commit()
    await db.refresh(fleet)
    return await _documentation_response(db, user, fleet)


@router.post("/{fleet_id}/documentation/findings/dismiss", response_model=FleetDocumentationResponse)
async def dismiss_fleet_documentation_findings_endpoint(
    fleet_id: str,
    body: FleetDocFindingsDismissRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Dismiss several suggestions, or every visible one (``ids`` null), with an optional reason (#10536)."""
    from app.services.fleet_docs import dismiss_findings

    fleet = await _manageable_fleet(db, user, fleet_id)
    ids = [i[:40] for i in body.ids] if body.ids is not None else None
    try:
        done = await dismiss_findings(db, fleet, ids, by=str(user.id), by_name=user.full_name or "", reason=body.reason)
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    if done:
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="fleet",
            entity_id=fleet.id,
            entity_name=fleet.name,
            summary=f"Dismissed {len(done)} documentation suggestion(s) of fleet '{fleet.name}'",
            details={"findings": [f["id"] for f in done], "reason": body.reason},
            request=request,
        )
    # Commit on every path, not only when something was dismissed: the service
    # may still rewrite fleet.documentation (pruned records), and get_db would
    # otherwise commit it only after the response is sent.
    await db.commit()
    await db.refresh(fleet)
    return await _documentation_response(db, user, fleet)


@router.post("/{fleet_id}/documentation/findings/restore", response_model=FleetDocumentationResponse)
async def restore_fleet_documentation_findings_endpoint(
    fleet_id: str,
    body: FleetDocFindingsRestoreRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Bring back dismissed suggestions, or every one (``ids`` null) (#10536); no model call."""
    from app.services.fleet_docs import restore_findings

    fleet = await _manageable_fleet(db, user, fleet_id)
    ids = [i[:40] for i in body.ids] if body.ids is not None else None
    try:
        restored = restore_findings(fleet, ids)
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    if restored:
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="fleet",
            entity_id=fleet.id,
            entity_name=fleet.name,
            summary=f"Restored {restored} documentation suggestion(s) of fleet '{fleet.name}'",
            details={"findings": ids or "all"},
            request=request,
        )
    # Commit on every path (see dismiss_fleet_documentation_findings_endpoint).
    await db.commit()
    await db.refresh(fleet)
    return await _documentation_response(db, user, fleet)


@router.post("/{fleet_id}/documentation/findings/{finding_id}/apply", response_model=FleetDocumentationResponse)
async def apply_fleet_practice_suggestion_endpoint(
    fleet_id: str,
    finding_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
) -> FleetDocumentationResponse:
    """Apply a good-practice suggestion a person clicked (#11948 S2); no model call.

    Three kinds apply in one click, each versioned and audited like an agent edit:
    ``member_cannot_consult_orchestrator`` gives the member a Consult handover to the orchestrator;
    ``orchestrator_cannot_write_knowledge`` grants the orchestrator write on the shared knowledge base
    (#11972); ``member_missing_working_knowledge`` binds the orchestrator's working knowledge base to the
    member, read-only, after the same binding checks as an agent edit. 404 when the id names no applicable
    suggestion of the fleet as configured now; 403 when the reader may not edit that member.
    """
    from app.api.v1.helpers import writable_agent_error
    from app.services.agent_config_versioning import record_agent_version_changes, versioned_agent_values
    from app.services.fleet_practice_suggestions import (
        KNOWLEDGE_KINDS,
        apply_back_consult,
        practice_suggestions,
    )

    fleet = await _manageable_fleet(db, user, fleet_id)
    wanted = finding_id[:40]
    match = next((s for s in await practice_suggestions(db, fleet) if s["id"] == wanted and s["applicable"]), None)
    member = await db.get(Agent, safe_uuid(match["agent_id"])) if match else None
    if match is None or member is None:
        raise HTTPException(status_code=404, detail="No such suggestion to apply on this fleet.")
    group_ids = await get_user_group_ids(db, user.id)
    denied = writable_agent_error(member, user, group_ids)
    if denied:
        raise HTTPException(status_code=403, detail=denied)
    before = versioned_agent_values(member)
    if match["kind"] in KNOWLEDGE_KINDS:
        await _apply_knowledge_suggestion(db, user, group_ids, fleet, member, match, request, before)
    elif await apply_back_consult(db, fleet, member):
        await record_agent_version_changes(
            db,
            agent=member,
            before=before,
            written={"handover_agent_ids", "handover_types"},
            author_id=user.id,
            commit_message=f"Consult handover to the orchestrator of fleet '{fleet.name}'",
        )
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="fleet",
            entity_id=fleet.id,
            entity_name=fleet.name,
            summary=f"Applied a suggestion of fleet '{fleet.name}': {member.name} may consult the orchestrator",
            details={"finding": wanted, "kind": match["kind"], "agent_id": str(member.id)},
            request=request,
        )
    await db.commit()
    await db.refresh(fleet)
    return await _documentation_response(db, user, fleet)


async def _apply_knowledge_suggestion(
    db: AsyncSession,
    user: User,
    group_ids: list[str],
    fleet: Fleet,
    member: Agent,
    match: dict[str, Any],
    request: Request,
    before: dict[str, Any],
) -> None:
    """Apply a knowledge suggestion (#11972) to *member*: a write grant, or a read-only binding."""
    from app.services.agent_bindings import validate_company_managed_bindings, validate_personal_agent_bindings
    from app.services.agent_config_versioning import record_agent_version_changes
    from app.services.fleet_practice_suggestions import apply_knowledge_suggestion

    kb_id = str(match.get("knowledge_base_id") or "")
    kind = match["kind"]
    if kind == "member_missing_working_knowledge":
        # The same binding checks as an agent edit: the reader sees the knowledge base, and a company-managed
        # member binds only a company-managed one.
        if member.is_company_managed:
            await validate_company_managed_bindings(
                db, mcp_server_ids=None, knowledge_base_ids=[kb_id], integration_bindings=None, handover_agent_ids=None
            )
        else:
            await validate_personal_agent_bindings(
                db,
                user.id,
                group_ids,
                is_admin=user.is_admin,
                mcp_server_ids=None,
                knowledge_base_ids=[kb_id],
                integration_bindings=None,
                handover_agent_ids=None,
                custom_tool_ids=None,
                skill_ids=None,
                prompt_ids=None,
            )
    if not apply_knowledge_suggestion(fleet, member, kind, kb_id):
        return
    granted = kind == "orchestrator_cannot_write_knowledge"
    await record_agent_version_changes(
        db,
        agent=member,
        before=before,
        written={"knowledge_base_permissions"} if granted else {"knowledge_base_ids"},
        author_id=user.id,
        commit_message=(
            f"Write access to the working knowledge of fleet '{fleet.name}'"
            if granted
            else f"Working knowledge of fleet '{fleet.name}' bound read-only"
        ),
    )
    what = "may write" if granted else "reads"
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="fleet",
        entity_id=fleet.id,
        entity_name=fleet.name,
        summary=f"Applied a suggestion of fleet '{fleet.name}': {member.name} {what} {match['subject']}",
        details={"finding": match["id"], "kind": kind, "agent_id": str(member.id), "knowledge_base_id": kb_id},
        request=request,
    )


@router.post("/{fleet_id}/documentation/prerequisites/confirm", response_model=FleetDocumentationResponse)
async def confirm_fleet_prerequisite_endpoint(
    fleet_id: str,
    body: FleetPrerequisiteConfirmRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Confirm or undo one manual prerequisite of the About page (#10469 S2); no model call.

    A fleet manager only (404 otherwise, like every manage route). Stored on the
    fleet, never in the documentation, so a regenerate keeping the line keeps
    it; built-in fleets included. One audit row ``prerequisite_confirmation``.
    """
    from app.services.fleet_about import set_prerequisite_confirmation

    fleet = await _manageable_fleet(db, user, fleet_id)
    try:
        line = await set_prerequisite_confirmation(db, user, fleet, body.check, confirmed=body.confirmed)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    verb = "Confirmed" if body.confirmed else "Undid the confirmation of"
    await log_audit(
        db,
        user=user,
        action="prerequisite_confirmation",
        entity_type="fleet",
        entity_id=fleet.id,
        entity_name=fleet.name,
        summary=f"{verb} the prerequisite '{line.label}' of fleet '{fleet.name}'",
        details={"check": line.check, "label": line.label, "confirmed": body.confirmed},
        request=request,
    )
    await db.commit()
    await db.refresh(fleet)
    return await _documentation_response(db, user, fleet)


def _datasheet_date(value: str | None) -> datetime:
    """When the documentation was generated, from its stored ISO 8601 stamp; now when absent or unreadable."""
    try:
        return datetime.fromisoformat(value) if value else datetime.now(UTC)
    except ValueError:
        return datetime.now(UTC)


def _datasheet_tool_labels() -> dict[str, str]:
    """Display names of the built-in tool groups and integration providers, by key (UX round 2 B1)."""
    from app.api.v1.integration_catalog import PROVIDER_CATALOG
    from app.services.flow_access import BUILTIN_TOOL_CATALOG

    labels = {str(key).lower(): str(entry.get("name") or key) for key, entry in PROVIDER_CATALOG.items()}
    labels.update({tool_id: label for tool_id, label, _icon, _desc in BUILTIN_TOOL_CATALOG})
    return labels


async def _datasheet_value(db: AsyncSession, fleet_id: uuid.UUID, user: User) -> ValueFigures | None:
    """The fleet's value over 30 days from the ROI ledger (#9836 R6d, #12077 L4b); ``None`` on a failed read.

    ``roi_ledger.figures(scope=fleet)`` for the datasheet's reader: the ``figures`` of
    ``GET /roi?scope=fleet&id=`` for the same user and window (invariant d), costs masked
    exactly as there; masked, the net value reads "Hidden by your administrator", never 0.
    """
    from app.services import roi_figures, roi_ledger
    from app.services.fleet_datasheet import ledger_value_figures

    try:
        viewer = await roi_ledger.viewer_for(db, user_id=user.id, is_admin=user.is_admin)
        figures = await roi_ledger.figures(
            db, scope=roi_figures.SCOPE_FLEET, viewer=viewer, scope_id=fleet_id, window="30d"
        )
    except Exception:  # the sheet says the value could not be read, never 0 or a 500
        logging.getLogger(__name__).warning("fleet datasheet value not read", exc_info=True)
        return None
    return ledger_value_figures(figures)


@router.get("/{fleet_id}/documentation/datasheet.pdf")
async def export_fleet_datasheet_endpoint(
    fleet_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """The fleet's one-page A4 datasheet (#9768 S4): the stored documentation and
    live state (automations, prerequisites, members) rendered server-side. No
    model call, no quota: licence-gated, not AI-gated, so it works with AI off.
    The value figures (#9836 R6d) are the fleet's ROI report over 30 days, the
    same read as ``GET /roi?scope=fleet&id=``, which opens to whoever sees the fleet.
    """
    from fastapi.responses import Response

    from app.core.config import settings
    from app.core.database import release_db_connection_before_loop
    from app.core.utils import build_content_disposition
    from app.services.branding import branding_defaults, get_branding
    from app.services.fleet_about import evaluate_prerequisites
    from app.services.fleet_datasheet import (
        GraphNode,
        PrerequisiteLine,
        build_datasheet_html,
        datasheet_author,
        datasheet_filename,
        datasheet_status,
    )
    from app.services.fleet_doc_generation import names_of_bindings, tool_use_facts
    from app.services.fleet_docs import documentation_view
    from app.services.fleet_views import fleet_automation_counts, fleet_tree
    from app.services.pdf_render import render_document_to_pdf
    from app.services.presentation_export import enforce_export_rate_limit

    group_ids = await get_user_group_ids(db, user.id)
    fleet = await _visible_fleet(db, user, fleet_id, group_ids)
    view = await documentation_view(db, fleet, can_edit=False)
    if not view.version:
        raise HTTPException(status_code=404, detail="This fleet has no documentation yet. Generate it first.")
    structured = view.structured or {}
    states = await evaluate_prerequisites(
        db, user, structured.get("prerequisites") or [], group_ids=group_ids, fleet=fleet
    )
    counts = await fleet_automation_counts(db, fleet)
    tree = fleet_tree(fleet.orchestrator_agent_id, await fleet_members(db, fleet.id))
    names = await _visible_agent_names(db, user, [row.agent_id for row in tree], group_ids)
    nodes = [
        GraphNode(
            agent_id=row.agent_id,
            parent_agent_id=row.parent_agent_id,
            depth=row.depth,
            name=names.get(row.agent_id, "Agent"),
            is_orchestrator=row.is_orchestrator,
        )
        for row in tree
    ]
    # Integrations and tools from the members' live bindings, by name only
    # (UX round 1 B3): the same facts the generator reads, never ids or secrets.
    member_ids = [row.agent_id for row in tree if row.agent_id in names]
    agents = (
        {a.id: a for a in (await db.execute(select(Agent).where(Agent.id.in_(member_ids)))).scalars().all()}
        if member_ids
        else {}
    )
    mcp_names, skill_names = await names_of_bindings(db, list(agents.values()))
    tool_uses = [
        tool_use_facts(agents[agent_id], names[agent_id], mcp_names, skill_names)
        for agent_id in member_ids
        if agent_id in agents
    ]
    branding = await get_branding(db)
    product = branding.name or branding_defaults(settings.is_xtm_one).name
    # Read last: a failed value read leaves no later read on this session.
    value = await _datasheet_value(db, fleet.id, user)
    document = build_datasheet_html(
        fleet_name=fleet.name,
        product_name=product,
        sections=view.sections,
        structured=structured,
        version=view.version,
        status=datasheet_status(counts.total, counts.enabled, counts.paused),
        automations_on=counts.enabled,
        automations_total=counts.total,
        nodes=nodes,
        prerequisites=[PrerequisiteLine(label=s.label, met=s.met) for s in states],
        generated_on=_datasheet_date(view.updated_at),
        author=datasheet_author(view.updated_by, view.restored_from),
        tool_uses=tool_uses,
        tool_labels=_datasheet_tool_labels(),
        value=value,
    )
    await enforce_export_rate_limit(user.id)
    await release_db_connection_before_loop(db)
    try:
        pdf = await render_document_to_pdf(document, first_page_only=True)
    except Exception as exc:
        logging.getLogger(__name__).warning("fleet datasheet render failed", exc_info=True)
        raise HTTPException(status_code=503, detail="The platform could not render the PDF. Try again later.") from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": build_content_disposition(datasheet_filename(fleet.name)),
            "Cache-Control": "no-store",
        },
    )


@router.post("/{fleet_id}/documentation/restore", response_model=FleetDocumentationResponse)
async def restore_fleet_documentation_version_endpoint(
    fleet_id: str,
    body: FleetDocumentationRestoreRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Bring a kept version of a fleet's documentation back as its next version; no model call (#9768 S2)."""
    from app.services.fleet_docs import restore_documentation_version

    fleet = await _manageable_fleet(db, user, fleet_id)
    try:
        await restore_documentation_version(db, fleet, body.version, author=user.id, base_version=body.base_version)
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="fleet",
        entity_id=fleet.id,
        entity_name=fleet.name,
        summary=f"Restored version {body.version} of the documentation of fleet '{fleet.name}'",
        details={"documentation_version": (fleet.documentation or {}).get("version"), "restored": body.version},
        request=request,
    )
    await db.commit()
    await db.refresh(fleet)
    return await _documentation_response(db, user, fleet)


@router.post("/apply", response_model=FleetSpecPlanResponse)
async def apply_fleet_spec_endpoint(
    body: FleetSpecApply,
    request: Request,
    dry_run: bool = Query(True, description="Compute the plan and write nothing (the default)."),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    # Applying a spec creates agents through ``create_agent_from_bundle``, the
    # path the agent import endpoints gate with ``require_agentic_ai``: AI off
    # refuses it the same way, unlike the fleet roster edits (licence only).
    _gate: None = Depends(require_agentic_ai),
):
    """Apply a fleet spec, idempotent by fleet slug and member key; ``dry_run`` by default."""
    from app.services.fleet_spec import FleetSpecError, apply_fleet_spec, parse_fleet_spec_text

    try:
        if body.spec is not None:
            spec = body.spec
        elif body.spec_text:
            spec = parse_fleet_spec_text(body.spec_text)
        else:
            raise FleetSpecError("Pass spec or spec_text.")
        workspace_uuid = parse_uuid(body.workspace_id, label="workspace_id") if body.workspace_id else None
        plan = await apply_fleet_spec(
            db,
            user,
            spec,
            await get_user_group_ids(db, user.id),
            dry_run=dry_run,
            allow_partial=body.allow_partial,
            workspace_id=workspace_uuid,
        )
    except FleetSpecError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except FleetActionRefusedError as exc:
        await db.rollback()
        raise _refused(exc) from None
    if dry_run:
        await db.rollback()
    else:
        if plan.fleet_id is not None:
            await log_audit(
                db,
                user=user,
                action="apply_spec",
                entity_type="fleet",
                entity_id=plan.fleet_id,
                entity_name=plan.fleet_slug,
                summary=f"Applied a fleet spec to '{plan.fleet_slug}'",
                details={"changes": len(plan.changes), "unresolved": len(plan.unresolved)},
                request=request,
            )
        await db.commit()
        if plan.created_fleet:
            # A fleet the spec created gets its first documentation (#10316).
            await schedule_documentation_on_create(db, plan.fleet_id, user.id)
    return FleetSpecPlanResponse(**plan.as_dict())


@router.post("/generate", response_model=FleetSpecGenerateResponse)
async def generate_fleet_spec_endpoint(
    body: FleetSpecGenerate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    # A model call, and a preview of the apply that creates agents: gated as
    # ``/apply`` is, AI enabled and the agentic licence (Sam, 31 Oct 2026).
    _gate: None = Depends(require_agentic_ai),
):
    """Create a fleet from a prompt, step one: a spec and its dry-run plan, nothing written.

    Apply the answered spec with ``POST /fleets/apply?dry_run=false``.
    """
    from app.services.fleet_spec import FleetSpecError
    from app.services.fleet_spec_generation import generate_fleet_spec

    try:
        workspace_uuid = parse_uuid(body.workspace_id, label="workspace_id") if body.workspace_id else None
        generated = await generate_fleet_spec(
            db,
            user,
            await get_user_group_ids(db, user.id),
            body.prompt,
            current=body.current,
            workspace_id=workspace_uuid,
        )
    except FleetSpecError as exc:
        await db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except FleetActionRefusedError as exc:
        await db.rollback()
        raise _refused(exc) from None
    await db.rollback()
    return FleetSpecGenerateResponse(
        spec=generated.spec, plan=FleetSpecPlanResponse(**generated.plan.as_dict()), notes=generated.notes
    )


@router.get("/{fleet_id}/boundary-violations", response_model=list[FleetBoundaryViolationResponse])
async def list_fleet_boundary_violations(
    fleet_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Configured handovers from outside the fleet to a member it does not expose."""
    fleet = await _visible_fleet(db, user, fleet_id)
    return [
        FleetBoundaryViolationResponse(source_agent_id=str(v.source_agent_id), target_agent_id=str(v.target_agent_id))
        for v in await fleet_boundary_violations(db, fleet)
    ]


@router.get("/by-agent/{agent_id}", response_model=AgentFleetsResponse)
async def get_agent_fleets(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Where an agent sits: its home fleet and the fleets that connect it.

    The agent must be visible to the caller (404 otherwise); only the fleets
    the caller can see are named; a home fleet the caller cannot see is only
    flagged (``home_hidden``), never named. Read only: membership is written on
    the fleet, never on the agent.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    agent = await get_user_agent(db, user.id, agent_id, user_group_ids, is_admin=user.is_admin)
    rows = (
        await db.execute(
            select(FleetMember, Fleet)
            .join(Fleet, Fleet.id == FleetMember.fleet_id)
            .where(
                FleetMember.agent_id == agent.id,
                user_visible_filter(Fleet, user.id, user_group_ids, is_admin=user.is_admin),
            )
            .order_by(Fleet.name, Fleet.id)
        )
    ).all()
    home: AgentHomeFleet | None = None
    connected: list[AgentConnectedFleet] = []
    for row, fleet in rows:
        if row.role == FLEET_ROLE_MEMBER:
            parent_name = ""
            if row.parent_member_id is not None:
                parent_agent_id = (
                    await db.execute(select(FleetMember.agent_id).where(FleetMember.id == row.parent_member_id))
                ).scalar_one_or_none()
                if parent_agent_id is not None:
                    names = await _visible_agent_names(db, user, [parent_agent_id], user_group_ids)
                    parent_name = names.get(parent_agent_id, "")
            is_orchestrator = fleet.orchestrator_agent_id == agent.id
            home = AgentHomeFleet(
                fleet_id=str(fleet.id),
                fleet_name=fleet.name,
                member_id=str(row.id),
                parent_member_id=_sid(row.parent_member_id),
                parent_name=parent_name,
                role_label=row.role_label or "",
                is_orchestrator=is_orchestrator,
                has_orchestrator=fleet.orchestrator_agent_id is not None,
                is_exposed=bool(row.is_exposed) or is_orchestrator,
                is_archived=bool(fleet.is_archived),
                can_manage=_can_manage_fleet(user, fleet, user_group_ids),
            )
        else:
            connected.append(
                AgentConnectedFleet(
                    fleet_id=str(fleet.id),
                    fleet_name=fleet.name,
                    member_id=str(row.id),
                    collaboration_instruction=effective_member_settings(fleet, row).collaboration_instruction,
                    context_inclusion=effective_member_settings(fleet, row).context_inclusion,
                    is_archived=bool(fleet.is_archived),
                    can_manage=_can_manage_fleet(user, fleet, user_group_ids),
                )
            )
    home_hidden = False
    if home is None:
        home_hidden = (
            await db.execute(
                select(func.count())
                .select_from(FleetMember)
                .where(FleetMember.agent_id == agent.id, FleetMember.role == FLEET_ROLE_MEMBER)
            )
        ).scalar_one() > 0
    return AgentFleetsResponse(agent_id=str(agent.id), home=home, home_hidden=home_hidden, connected_to=connected)


# ── Views: graph, hierarchy, health, activity ────────────────────────────────


def _sid(value: uuid.UUID | None) -> str | None:
    return str(value) if value is not None else None


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


async def _visible_agent_names(
    db: AsyncSession, user: User, agent_ids: list[uuid.UUID], user_group_ids: list[str]
) -> dict[uuid.UUID, str]:
    """Names of the agents the caller may see; the others stay placeholders."""
    if not agent_ids:
        return {}
    return dict(
        (
            await db.execute(
                select(Agent.id, Agent.name).where(
                    Agent.id.in_(agent_ids),
                    user_visible_filter(Agent, user.id, user_group_ids, is_admin=user.is_admin),
                )
            )
        ).all()
    )


@router.get("/{fleet_id}/graph", response_model=FleetGraphResponse)
async def get_fleet_graph(
    fleet_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """The handover graph around the fleet, cross-fleet edges included.

    Home members, connected agents and fleets, and the agents outside the
    fleet a configured handover reaches or comes from. A crossing edge says
    whether a connected row governs it and whether the target is exposed.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    fleet = await _visible_fleet(db, user, fleet_id, user_group_ids)
    graph = await build_fleet_graph(
        db,
        fleet,
        visible_agent_clause=user_visible_filter(Agent, user.id, user_group_ids, is_admin=user.is_admin),
        visible_fleet_clause=user_visible_filter(Fleet, user.id, user_group_ids, is_admin=user.is_admin),
    )
    return FleetGraphResponse(
        fleet_id=str(fleet.id),
        orchestrator_agent_id=_sid(fleet.orchestrator_agent_id),
        nodes=[
            FleetGraphNode(
                id=n.id,
                kind=n.kind,  # type: ignore[arg-type]
                name=n.name,
                icon=n.icon,
                agent_id=_sid(n.agent_id),
                fleet_id=_sid(n.fleet_id),
                fleet_name=n.fleet_name,
                member_id=_sid(n.member_id),
                parent_agent_id=_sid(n.parent_agent_id),
                is_orchestrator=n.is_orchestrator,
                is_exposed=n.is_exposed,
                enabled=n.enabled,
                visible=n.visible,
                collaboration_instruction=n.collaboration_instruction,
                context_inclusion=n.context_inclusion,
                entry_agent_id=_sid(n.entry_agent_id),
            )
            for n in graph.nodes
        ],
        edges=[
            FleetGraphEdge(
                source=e.source,
                target=e.target,
                type=e.type,
                scope=e.scope,  # type: ignore[arg-type]
                governed=e.governed,
                allowed=e.allowed,
                source_fleet_id=_sid(e.source_fleet_id),
                target_fleet_id=_sid(e.target_fleet_id),
            )
            for e in graph.edges
        ],
    )


@router.get("/{fleet_id}/flow", response_model=FleetFlowResponse)
async def get_fleet_flow(
    fleet_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """The fleet's data flow: what starts each home member, what it writes to.

    Trigger sources (integration events, platform events, schedules,
    webhooks, work item inboxes, manual), the members and their handovers,
    and the integrations each member is bound to (``write`` or ``read``),
    from configuration only: no run, no model call, no quota.
    """
    from app.models.integration import Integration

    user_group_ids = await get_user_group_ids(db, user.id)
    fleet = await _visible_fleet(db, user, fleet_id, user_group_ids)
    flow = await build_fleet_flow(
        db,
        fleet,
        visible_agent_clause=user_visible_filter(Agent, user.id, user_group_ids, is_admin=user.is_admin),
        visible_integration_clause=user_visible_filter(Integration, user.id, user_group_ids, is_admin=user.is_admin),
    )
    return FleetFlowResponse(
        fleet_id=str(fleet.id),
        nodes=[
            FleetFlowNode(
                id=n.id,
                lane=n.lane,  # type: ignore[arg-type]
                kind=n.kind,
                name=n.name,
                icon=n.icon,
                provider=n.provider,
                integration_id=_sid(n.integration_id),
                agent_id=_sid(n.agent_id),
                events=n.events,
                enabled=n.enabled,
                reason=n.reason,
                visible=n.visible,
            )
            for n in flow.nodes
        ],
        edges=[
            FleetFlowEdge(
                source=e.source,
                target=e.target,
                kind=e.kind,  # type: ignore[arg-type]
                assignment_id=_sid(e.assignment_id),
                assignment_name=e.assignment_name,
                events=e.events,
                detail=e.detail,
                cron=e.cron,
                timezone=e.timezone,
                filter=e.filter,
                gate=e.gate,
                type=e.type,
                enabled=e.enabled,
                reason=e.reason,
            )
            for e in flow.edges
        ],
    )


@router.get("/{fleet_id}/hierarchy", response_model=list[FleetHierarchyNode])
async def get_fleet_hierarchy(
    fleet_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """The declared tree of the home members, depth first, orchestrator first."""
    user_group_ids = await get_user_group_ids(db, user.id)
    fleet = await _visible_fleet(db, user, fleet_id, user_group_ids)
    tree = fleet_tree(fleet.orchestrator_agent_id, await fleet_members(db, fleet.id))
    names = await _visible_agent_names(db, user, [r.agent_id for r in tree], user_group_ids)
    return [
        FleetHierarchyNode(
            member_id=str(r.member_id),
            agent_id=str(r.agent_id),
            name=names.get(r.agent_id, ""),
            parent_agent_id=_sid(r.parent_agent_id),
            depth=r.depth,
            is_orchestrator=r.is_orchestrator,
        )
        for r in tree
    ]


@router.get("/{fleet_id}/health", response_model=FleetHealthResponse)
async def get_fleet_health(
    fleet_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Whether the fleet is set up (``automations``: its automations, on and
    paused, #9566), then per member: enabled, last run, runs in flight, the run verdict
    (``run_outcome.member_verdict``: interruptions by the platform never count
    as failing), open and stale work items; the fleet's status is the
    worst of its members' (``idle`` only when every member is idle).

    Runs are the ones the caller may see (``run_visible_filter``), work items
    the visible ones: the strip never shows what the caller could not open.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    fleet = await _visible_fleet(db, user, fleet_id, user_group_ids)
    status_, members, in_workspace = await build_fleet_health(
        db, fleet, user_id=str(user.id), run_scope=run_visible_filter(user.id)
    )
    names = await _visible_agent_names(db, user, [m.agent_id for m in members], user_group_ids)
    automations = await fleet_automation_counts(db, fleet)
    # The About KPI row's 30-day figures (#10123): reads only, no run, no quota.
    stats = await fleet_run_stats(db, [m.agent_id for m in members], run_scope=run_visible_filter(user.id))
    from app.services.coordination_scope import fleet_shared_states_clause
    from app.services.shared_state import visible_shared_state_hygiene_counts

    hygiene = await visible_shared_state_hygiene_counts(
        db, user_id=str(user.id), scope=fleet_shared_states_clause(fleet.id)
    )
    return FleetHealthResponse(
        fleet_id=str(fleet.id),
        status=status_,
        run_stats=FleetRunStatsResponse(
            runs=stats.runs,
            succeeded=stats.succeeded,
            failed=stats.failed,
            by_day=list(stats.by_day),
            awaiting_approval=stats.awaiting_approval,
            succeeded_by_day=list(stats.succeeded_by_day),
            failed_by_day=list(stats.failed_by_day),
            last_summary=stats.last_summary,
        ),
        automations=FleetAutomationsResponse(
            total=automations.total,
            enabled=automations.enabled,
            paused=automations.paused,
            fleet_paused=automations.fleet_paused,
            off_names=list(automations.off_names),
        ),
        members=[
            FleetMemberHealthResponse(
                agent_id=str(m.agent_id),
                name=names.get(m.agent_id, ""),
                status=m.status,  # type: ignore[arg-type]
                enabled=m.enabled,
                last_run_at=_iso(m.last_run_at),
                last_run_status=m.last_run_status,
                last_run_outcome=m.last_run_outcome,
                runs_in_flight=m.runs_in_flight,
                failures_24h=m.failures_24h,
                recent_runs=m.recent_runs,
                recent_interruptions=m.recent_interruptions,
                open_work_items=m.open_work_items,
                stale_work_items=m.stale_work_items,
            )
            for m in members
        ],
        workspace_open_work_items=in_workspace,
        shared_state_hygiene=FleetSharedStateHygieneResponse(**hygiene),
    )


async def _controllable_fleet(db: AsyncSession, user: User, fleet_id: str, action: str) -> Fleet:
    """A fleet whose automations *user* may switch: whoever manages it; a built-in
    fleet's seeded automations are switched by an administrator only (their
    opt-in, ``ADMIN_OPT_IN_WRITABLE_FIELDS``)."""
    fleet = await _manageable_fleet(db, user, fleet_id, action=action)
    if getattr(fleet, "is_builtin", False) and not user.is_admin:
        raise HTTPException(status_code=403, detail=f"Only an administrator can {action} a built-in fleet.")
    return fleet


async def _control_response(db: AsyncSession, fleet: Fleet, changed: int) -> FleetControlResponse:
    counts = await fleet_automation_counts(db, fleet)
    return FleetControlResponse(
        fleet_id=str(fleet.id),
        changed=changed,
        automations=FleetAutomationsResponse(
            total=counts.total, enabled=counts.enabled, paused=counts.paused, fleet_paused=counts.fleet_paused
        ),
    )


@router.post("/{fleet_id}/pause", response_model=FleetControlResponse)
async def pause_fleet_automations(
    fleet_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Pause all: switch off every automation of the fleet that is on (``paused_reason =
    fleet_pause``), so Resume restores exactly those. Runs in flight continue."""
    fleet = await _controllable_fleet(db, user, fleet_id, "pause the automations of")
    outcome = await pause_fleet(db, fleet, actor=user, request=request)
    if outcome.changed:
        await log_audit(
            db,
            user=user,
            action="fleet_pause",
            entity_type="fleet",
            entity_id=fleet.id,
            entity_name=fleet.name,
            summary=f"Paused {outcome.changed} automation(s) of fleet '{fleet.name}'",
            details=fleet_control_audit_details(outcome, fleet.id),
            request=request,
        )
    await db.commit()
    return await _control_response(db, fleet, outcome.changed)


@router.post("/{fleet_id}/resume", response_model=FleetControlResponse)
async def resume_fleet_automations(
    fleet_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Resume: switch back on exactly the automations the fleet's Pause all switched off;
    one a person paused before, an emergency stop holds or the platform quarantined stays off."""
    fleet = await _controllable_fleet(db, user, fleet_id, "resume the automations of")
    try:
        outcome = await resume_fleet(db, fleet, actor=user, request=request)
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    if outcome.changed:
        await log_audit(
            db,
            user=user,
            action="fleet_resume",
            entity_type="fleet",
            entity_id=fleet.id,
            entity_name=fleet.name,
            summary=f"Resumed {outcome.changed} automation(s) of fleet '{fleet.name}'",
            details=fleet_control_audit_details(outcome, fleet.id),
            request=request,
        )
    await db.commit()
    return await _control_response(db, fleet, outcome.changed)


@router.post("/{fleet_id}/unset", response_model=FleetControlResponse)
async def unset_fleet_automations(
    fleet_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Back to not set up (#11060): every automation of the fleet off, stamped ``created_disabled``
    (one an emergency stop holds left as it is), so the fleet reads "Not set up". Nothing is deleted."""
    fleet = await _controllable_fleet(db, user, fleet_id, "reset the automations of")
    outcome = await unset_fleet(db, fleet, actor=user, request=request)
    if outcome.changed:
        await log_audit(
            db,
            user=user,
            action="fleet_unset",
            entity_type="fleet",
            entity_id=fleet.id,
            entity_name=fleet.name,
            summary=f"Set {outcome.changed} automation(s) of fleet '{fleet.name}' back to not set up",
            details={**fleet_control_audit_details(outcome, fleet.id), "held_by_stop": list(outcome.held_by_stop)},
            request=request,
        )
    await db.commit()
    return await _control_response(db, fleet, outcome.changed)


ENABLE_RUN_AS_ADMIN_ONLY = "Only an administrator can pick another user for the fleet's automations to run as."
ENABLE_RUN_AS_UNKNOWN = "run_as_user_id does not reference an active user"


async def _enable_run_as(db: AsyncSession, user: User, raw: str | None) -> uuid.UUID:
    """The run-as of Enable fleet: the caller by default; another active user, an administrator's choice."""
    if not raw:
        return user.id
    run_as = parse_uuid(raw, label="run_as_user_id")
    if run_as == user.id:
        return run_as
    if not user.is_admin:
        raise HTTPException(status_code=403, detail=ENABLE_RUN_AS_ADMIN_ONLY)
    found = (await db.execute(select(User.id).where(User.id == run_as, User.is_active.is_(True)))).scalar_one_or_none()
    if found is None:
        raise HTTPException(status_code=422, detail=ENABLE_RUN_AS_UNKNOWN)
    return run_as


@router.post("/{fleet_id}/enable", response_model=FleetEnableResponse)
async def enable_fleet_automations(
    fleet_id: str,
    body: FleetEnableRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    # Unlike Pause all / Resume, Enable starts new AI work: refused while AI is off.
    _gate: None = Depends(require_agentic_ai),
):
    """Enable fleet (#9769 S4b): switch on the automations of the fleet that are off, the
    seeded fleet automations as ``run_as_user_id`` (the caller by default); every one, or
    the ``assignment_ids`` picked in the setup dialog (partial enable, #9173 S5). All or
    nothing over the pick: refused while the run-as user misses a module a picked automation
    needs (400), for an empty pick or an id that is not an automation of the fleet that is
    off (422), when nothing is off or during an emergency stop (409). ``dry_run`` answers
    the plan, writes nothing."""
    fleet = await _controllable_fleet(db, user, fleet_id, "enable the automations of")
    run_as = await _enable_run_as(db, user, body.run_as_user_id)
    picked = (
        None
        if body.assignment_ids is None
        else [parse_uuid(raw, label="assignment_ids") for raw in dict.fromkeys(body.assignment_ids)]
    )
    try:
        plan = await plan_fleet_enable(db, fleet, run_as_user_id=run_as, assignment_ids=picked)
    except FleetActionRefusedError as exc:
        raise _refused(exc) from None
    changed = plan.changed
    if not body.dry_run:
        try:
            outcome = await enable_fleet(
                db, fleet, actor=user, run_as_user_id=run_as, assignment_ids=picked, request=request
            )
        except FleetActionRefusedError as exc:
            raise _refused(exc) from None
        changed = outcome.changed
        await log_audit(
            db,
            user=user,
            action="fleet_enable",
            entity_type="fleet",
            entity_id=fleet.id,
            entity_name=fleet.name,
            summary=f"Enabled {outcome.changed} automation(s) of fleet '{fleet.name}'",
            details={
                **fleet_control_audit_details(outcome, fleet.id),
                "run_as_user_id": str(run_as),
                "partial": picked is not None and outcome.changed < len(plan.items),
            },
            request=request,
        )
    # Before the answer, every call (a dry run wrote nothing, so it commits nothing).
    await db.commit()
    if not body.dry_run and changed:
        # A CTEM case fleet takes the open cases it owns that nobody ran (#1390).
        from app.services.ctem_case_routing import catch_up_after_enable

        await catch_up_after_enable(fleet.slug)
    counts = await fleet_automation_counts(db, fleet)
    return FleetEnableResponse(
        fleet_id=str(fleet.id),
        dry_run=body.dry_run,
        run_as_user_id=str(run_as),
        changed=changed,
        ready=plan.ready,
        missing=plan.missing,
        items=[
            FleetEnableItemResponse(
                id=str(i.id), name=i.name, providers=list(i.providers), missing=list(i.missing), selected=i.selected
            )
            for i in plan.items
        ],
        automations=FleetAutomationsResponse(
            total=counts.total, enabled=counts.enabled, paused=counts.paused, fleet_paused=counts.fleet_paused
        ),
    )


BULK_ALL_FLEETS_ADMIN_ONLY = "Only an administrator can pause every fleet automation of the platform."
BULK_ALL_FLEETS_PAUSE_ONLY = "The every-fleet switch only pauses; resume fleets from the Fleets list selection."
BULK_NO_FLEET = "Select at least one fleet."
BULK_LEVEL_REQUIRED = "Choose the autonomy level to set."


async def _bulk_targets(db: AsyncSession, user: User, body: FleetBulkAction) -> list[str]:
    """The fleet ids a bulk call acts on: the selection, or every live fleet for the admin switch."""
    if body.all_fleets:
        if not user.is_admin:
            raise HTTPException(status_code=403, detail=BULK_ALL_FLEETS_ADMIN_ONLY)
        if body.action != "pause":
            raise HTTPException(status_code=422, detail=BULK_ALL_FLEETS_PAUSE_ONLY)
        rows = await db.execute(select(Fleet.id).where(Fleet.is_archived.is_(False)).order_by(Fleet.name, Fleet.id))
        return [str(fleet_id) for fleet_id in rows.scalars().all()]
    ids = list(dict.fromkeys(body.fleet_ids))
    if not ids:
        raise HTTPException(status_code=422, detail=BULK_NO_FLEET)
    if body.action == "set_autonomy" and body.autonomy_level is None:
        raise HTTPException(status_code=422, detail=BULK_LEVEL_REQUIRED)
    return ids


async def _bulk_one(db: AsyncSession, user: User, fleet: Fleet, body: FleetBulkAction, request: Request) -> int:
    """Apply (or, on a dry run, count) the bulk action on one fleet the caller may control."""
    if body.action == "set_autonomy":
        _ensure_writable(fleet, {"autonomy_level"})
        previous = effective_level(fleet.autonomy_level)
        new = normalize_level(body.autonomy_level)
        ensure_level_change_allowed(previous=previous, new=new, is_admin=bool(user.is_admin))
        if new == previous:
            return 0
        if not body.dry_run:
            fleet.autonomy_level = new
            await log_audit(
                db,
                user=user,
                action="autonomy_level_change",
                entity_type="fleet",
                entity_id=fleet.id,
                entity_name=fleet.name,
                summary=f"Set the autonomy level of fleet '{fleet.name}' to {new}",
                details={"from": previous, "to": new, "bulk": True},
                request=request,
            )
        return 1
    if body.dry_run:
        return await (pausable_count(db, fleet) if body.action == "pause" else resumable_count(db, fleet))
    if body.action == "pause":
        outcome = await pause_fleet(db, fleet, actor=user, request=request)
    else:
        outcome = await resume_fleet(db, fleet, actor=user, request=request)
    if outcome.changed:
        verb = "Paused" if body.action == "pause" else "Resumed"
        await log_audit(
            db,
            user=user,
            action=f"fleet_{body.action}",
            entity_type="fleet",
            entity_id=fleet.id,
            entity_name=fleet.name,
            summary=f"{verb} {outcome.changed} automation(s) of fleet '{fleet.name}' (bulk)",
            details={**fleet_control_audit_details(outcome, fleet.id), "bulk": True, "all_fleets": body.all_fleets},
            request=request,
        )
    return outcome.changed


@router.post("/bulk", response_model=FleetBulkResponse)
async def bulk_fleet_action(
    body: FleetBulkAction,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """Pause all, Resume or set the autonomy level on several fleets at once (#9769 S3).

    Each fleet follows its single-fleet rule (who may control it, admin-only raise,
    built-in fleets switched by an administrator); a refused fleet is reported, the
    others still apply. ``all_fleets`` is the administrators' emergency switch: Pause
    all on every live fleet. ``dry_run`` counts without writing.
    """
    targets = await _bulk_targets(db, user, body)
    outcomes: list[FleetBulkOutcome] = []
    for fleet_id in targets:
        name = ""
        try:
            # The single-fleet rule of each action: the level follows update_fleet (whoever
            # manages the fleet lowers, an admin raises), Pause all / Resume follow their routes.
            if body.action == "set_autonomy":
                fleet = await _manageable_fleet(db, user, fleet_id, action="set the autonomy level of")
            else:
                fleet = await _controllable_fleet(db, user, fleet_id, f"{body.action} the automations of")
            name = fleet.name
            changed = await _bulk_one(db, user, fleet, body, request)
        except HTTPException as exc:
            outcomes.append(FleetBulkOutcome(fleet_id=fleet_id, name=name, ok=False, detail=str(exc.detail)))
            continue
        except FleetActionRefusedError as exc:
            outcomes.append(FleetBulkOutcome(fleet_id=fleet_id, name=name, ok=False, detail=str(exc.detail)))
            continue
        outcomes.append(FleetBulkOutcome(fleet_id=fleet_id, name=name, ok=True, changed=changed))
    # One commit for the call, before the answer (a dry run wrote nothing, so it commits nothing).
    await db.commit()
    return FleetBulkResponse(
        action=body.action,
        dry_run=body.dry_run,
        changed=sum(o.changed for o in outcomes if o.ok),
        refused=sum(1 for o in outcomes if not o.ok),
        fleets=outcomes,
    )


@router.get("/{fleet_id}/activity", response_model=FleetActivityResponse)
async def get_fleet_activity(
    fleet_id: str,
    limit: int = Query(40, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """The members' latest runs the caller may see and the caller's own
    delegations from or to a member, newest first (``limit`` each)."""
    user_group_ids = await get_user_group_ids(db, user.id)
    fleet = await _visible_fleet(db, user, fleet_id, user_group_ids)
    ids = home_member_ids(await fleet_members(db, fleet.id), fleet.orchestrator_agent_id)
    runs = await recent_runs(db, ids, run_scope=run_visible_filter(user.id), limit=limit)
    delegations = await list_delegations(db, str(user.id), any_agent_ids=ids, limit=limit) if ids else []
    return FleetActivityResponse(
        fleet_id=str(fleet.id),
        runs=[
            FleetRunEntry(
                id=str(run_id),
                agent_id=str(agent_id),
                assignment_id=_sid(assignment_id),
                status=run_status,
                created_at=_iso(created_at) or "",
                completed_at=_iso(completed_at),
                outcome=outcome,
                trigger_type=trigger_type,
                trigger_provider=trigger_provider,
                trigger_event=trigger_event,
                case_id=case_id,
            )
            for (
                run_id,
                agent_id,
                assignment_id,
                run_status,
                created_at,
                completed_at,
                outcome,
                trigger_type,
                trigger_provider,
                trigger_event,
                case_id,
            ) in runs
        ],
        delegations=delegations,
    )


@router.get("/case-runs/{case_id}", response_model=CaseRunsResponse)
async def get_case_runs(
    case_id: uuid.UUID,
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """The fleet runs a CTEM case's events started (#915), oldest first, the ones
    the caller may see (``run_visible_filter``), for the case page that shows
    the agents' steps beside the case history. An id in, runs out: the case
    itself is read through ``GET /ctem-cases/{id}``, per viewer."""
    runs = await case_runs(db, case_id, run_scope=run_visible_filter(user.id), limit=limit)
    return CaseRunsResponse(
        case_id=str(case_id),
        runs=[
            CaseRunEntry(
                id=str(run_id),
                agent_id=str(agent_id),
                agent_name=agent_name,
                status=run_status,
                created_at=_iso(created_at) or "",
                completed_at=_iso(completed_at),
                outcome=outcome,
            )
            for (run_id, agent_id, agent_name, run_status, created_at, completed_at, outcome) in runs
        ],
    )


@router.get("/{fleet_id}/cost", response_model=FleetCostResponse)
async def get_fleet_cost(
    fleet_id: str,
    days: int = Query(30, ge=1, le=365),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
):
    """The cost of the runs attributed to the fleet over the last ``days``, by agent.

    Read by whoever manages the fleet: the totals span every member's runs,
    the caller's or not. A connected member's runs count for its own home
    fleet, never for this one.
    """
    fleet = await _manageable_fleet(db, user, fleet_id, action="read the cost of")
    since = datetime.now(UTC) - timedelta(days=days)
    costs = await fleet_cost(db, fleet.id, since=since)
    return FleetCostResponse(
        fleet_id=str(fleet.id),
        days=days,
        since=since.isoformat(),
        total_cost=round(sum(c.total_cost for c in costs), 8),
        total_tokens=sum(c.total_tokens for c in costs),
        trace_count=sum(c.trace_count for c in costs),
        agents=[
            FleetAgentCostEntry(
                agent_id=_sid(c.agent_id),
                agent_name=c.agent_name,
                trace_count=c.trace_count,
                total_cost=c.total_cost,
                total_tokens=c.total_tokens,
            )
            for c in costs
        ],
    )


@router.get("/{fleet_id}/run-estimate", response_model=FleetRunEstimateResponse)
async def get_fleet_run_estimate(
    fleet_id: str,
    organization_ids: list[str] | None = Query(None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
) -> FleetRunEstimateResponse:
    """Expected runs per month of the fleet's home members, per organization of a scope.

    ``organization_ids`` names the organizations (the scope picked in the
    editor, before the save); without it, the fleet's default scope. The
    primary is always listed. One read-only OpenCTI count per assignment of a
    member whose trigger serves a tracked organization, over 30 days, bounded
    by its daily cap (``dev-docs/ctem-fleets.md`` 9.7 and 10.3). No model
    call, nothing saved. Read by whoever manages the fleet.
    """
    from app.models.organization import Organization
    from app.services import organizations as orgs
    from app.services.organization_context import scoped_organizations
    from app.services.organization_opencti import opencti_gql
    from app.services.organization_run_cap import ESTIMATE_DAYS, estimate_runs

    fleet = await _manageable_fleet(db, user, fleet_id, action="read the run estimate of")
    if organization_ids:
        if len(organization_ids) > MAX_SCOPE_ORGANIZATIONS:
            raise HTTPException(
                status_code=422, detail=f"organization_ids names at most {MAX_SCOPE_ORGANIZATIONS} organizations."
            )
        ids = list(dict.fromkeys(parse_uuid(i, label="organization_ids") for i in organization_ids))
        rows = (
            (await db.execute(select(Organization).where(Organization.id.in_(ids), Organization.archived_at.is_(None))))
            .scalars()
            .all()
        )
        tracked = sorted((o for o in rows if o.role == "tracked"), key=lambda o: o.name.lower())
    else:
        tracked = await scoped_organizations(db, resolve_organization_scope(fleet.organization_scope, None))
    agent_ids = [
        r.agent_id for r in await fleet_members(db, fleet.id) if r.role == FLEET_ROLE_MEMBER and r.agent_id is not None
    ]
    out: list[FleetOrganizationRunEstimate] = []
    primary = await orgs.get_primary(db)
    if primary is not None:
        out.append(
            FleetOrganizationRunEstimate(
                organization_id=str(primary.id),
                name=primary.name,
                role="primary",
                monitoring="on",
                daily_run_cap=None,
                assignments=[],
                expected_runs_per_month=0,
                note="the primary's own triggers are not capped and not estimated per organization",
            )
        )
    gql = await opencti_gql(db, str(user.id)) if tracked else None
    for organization in tracked:
        if isinstance(gql, str):
            out.append(
                FleetOrganizationRunEstimate(
                    organization_id=str(organization.id),
                    name=organization.name,
                    role="tracked",
                    monitoring=organization.monitoring,
                    daily_run_cap=organization.daily_run_cap,
                    assignments=[],
                    expected_runs_per_month=0,
                    note=gql,
                )
            )
            continue
        estimate = await estimate_runs(db, gql, organization, agent_ids=agent_ids)
        out.append(
            FleetOrganizationRunEstimate(
                organization_id=str(organization.id),
                name=organization.name,
                role="tracked",
                monitoring=estimate["monitoring"],
                daily_run_cap=estimate["daily_run_cap"],
                assignments=estimate["assignments"],
                expected_runs_per_month=estimate["expected_runs_per_month"],
            )
        )
    return FleetRunEstimateResponse(
        fleet_id=str(fleet.id),
        window_days=ESTIMATE_DAYS,
        organizations=out,
        expected_runs_per_month=sum(o.expected_runs_per_month for o in out),
    )


@router.get("/setup/ctem-loop", response_model=FleetSetupStatusResponse)
async def get_ctem_loop_setup_status(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _gate: None = Depends(require_agentic_ai_licensed),
) -> FleetSetupStatusResponse:
    """Where the CTEM loop guided setup stands, step by step (#1087 slice 2).

    Administrators of an XTM One platform only (403 otherwise). Each step reads
    ``done`` or ``skipped`` as recorded for the current setup version, else
    ``ready`` or ``todo`` from the platform as it is: the saved profile, the
    active module registrations, the loop fleet's capability slots, its
    autonomy level, gates and automations. Read only: no model call, no quota,
    no write. ``resume_on`` names the step the flow opens on.
    """
    from app.agents.builtin_fleets import CTEM_LOOP_SLUG
    from app.api.v1.platform_settings_helpers import get_platform_settings
    from app.core.config import settings as app_settings
    from app.models.platform_registration import PlatformRegistration
    from app.services import fleet_setup_status as status_mod
    from app.services import organization_setup_prompt as prompt
    from app.services.fleet_autonomy import effective_gates, effective_level, gate_waits
    from app.services.fleet_views import opt_in_state
    from app.services.organization_context import current_version, parse_profile

    if not (user.is_admin and app_settings.is_xtm_one):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin privileges required")
    row = await get_platform_settings(db)
    saved = await current_version(db)
    profile = parse_profile(saved.profile) if saved is not None else None
    profile_facts = status_mod.ProfileFacts(
        saved=saved is not None,
        crown_jewels=len(profile.crown_jewels) if profile else 0,
        tech_stack=len(profile.tech_stack) if profile else 0,
        ticketing_target=bool(profile and profile.ticketing.integration),
        scope_min_criticality=profile.prioritization_policy.scope_min_criticality if profile else "",
    )
    modules = [
        str(identifier)
        for (identifier,) in (
            await db.execute(
                select(PlatformRegistration.platform_identifier)
                .where(
                    PlatformRegistration.platform_identifier.in_(status_mod.LOOP_MODULES),
                    PlatformRegistration.status == "active",
                )
                .distinct()
            )
        ).all()
    ]
    fleet = (
        await db.execute(select(Fleet).where(Fleet.is_builtin.is_(True), Fleet.slug == CTEM_LOOP_SLUG).limit(1))
    ).scalar_one_or_none()
    slots: dict[str, str] = status_mod.slot_summary(())
    fleet_facts = status_mod.LoopFleetFacts()
    if fleet is not None:
        slot_view = await get_fleet_capability_slots(str(fleet.id), user=user, db=db, _gate=None)
        # The Ecosystem step counts the customer's own tools: a built-in option
        # (#1201) fills a slot without connecting anything, so it is left out here.
        slots = status_mod.slot_summary(
            [
                {"capability": s.capability, "state": status_mod.external_slot_state(s.bound, s.candidates)}
                for s in m.slots
            ]
            for m in slot_view.members
        )
        level = effective_level(fleet.autonomy_level)
        gates = effective_gates(
            fleet.approval_gates, fleet.documentation, pins=fleet.approval_pins, customer=fleet.customer_gates
        )
        counts = await fleet_automation_counts(db, fleet)
        fleet_facts = status_mod.LoopFleetFacts(
            fleet_id=str(fleet.id),
            autonomy_level=level,
            opt_in=opt_in_state(counts),
            automations_total=counts.total,
            automations_enabled=counts.enabled,
            waiting_gates=tuple(str(g.get("key") or "") for g in gates if gate_waits(level, g)),
        )
    result = status_mod.setup_status(
        recorded=prompt.steps_of(row), profile=profile_facts, modules=modules, slots=slots, fleet=fleet_facts
    )
    return FleetSetupStatusResponse(
        version=prompt.SETUP_VERSION,
        fleet_id=fleet_facts.fleet_id,
        steps=[FleetSetupStep.model_validate({"key": s.key, "state": s.state, "facts": s.facts}) for s in result.steps],
        resume_on=result.resume_on,
    )
