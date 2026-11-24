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

"""Platform Health REST API (#8254): opt-in, opt-out, Settings > Agents and automations > Platform Health, budget.

Platform Health is built in: no feature flag gates these routes. The rules (PRD
``dev-docs/platform-concierge.md`` 4 and 6.2):

* The improvement opt-in of an agent or a flow: read by whoever sees the
  target, written by whoever may manage it (``can_manage``: company-managed =
  admins), shared or not (decision B21). A Platform Health fleet agent has none.
* The opt-out: the signed-in user's own, nobody else's.
* Settings and budget: administrators.

A revocation (opt-in off, opt-out on, scope narrowed) runs the registered
handlers in the same transaction (``consent.apply_revocation``): if one fails,
nothing changes and the caller gets a generic error.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.api.v1.helpers import can_manage, get_user_agent, get_user_group_ids, parse_path_id, user_visible_filter
from app.core.database import get_db
from app.models.flow import Flow
from app.models.health import HealthBudgetMonth, HealthCycle
from app.models.user import User
from app.services.audit import log_audit
from app.services.health import budget as health_budget
from app.services.health.consent import Revocation, apply_revocation
from app.services.health.naming import default_health_agent_name, load_health_agent_name
from app.services.health.settings import (
    SPEND_LIMITS_ENV_VAR,
    SPEND_LIMITS_LOCKED_REASON,
    HealthBudgetCaps,
    HealthSettings,
    SpendLimitsLockedError,
    describe_spend_limits,
    load_health_settings,
    load_stored_health_settings,
    save_health_settings,
    scope_narrowed,
    spend_limits_unlocked,
)

logger = logging.getLogger(__name__)

router = APIRouter()

#: What a caller is told when a revocation could not be applied (the reason is logged).
NOT_APPLIED_DETAIL = "The change could not be applied. Nothing was changed."


class OptInResponse(BaseModel):
    opt_in: bool
    can_manage: bool
    #: False for a Platform Health fleet agent: it is never a target.
    eligible: bool = True


class OptInUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opt_in: bool


class OptOutResponse(BaseModel):
    opt_out: bool


class OptOutUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opt_out: bool


class BudgetUsage(BaseModel):
    tokens: int
    cost_usd: Decimal
    reserved_tokens: int
    reserved_cost_usd: Decimal


class BudgetResponse(BaseModel):
    month: str
    caps: HealthBudgetCaps
    used: BudgetUsage
    remaining_tokens: int | None
    remaining_cost_usd: Decimal | None
    open_cycles: int
    #: Why no new Platform Health work starts now, or ``None``.
    refusal: str | None
    refusal_message: str | None


async def _commit_with_revocation(db: AsyncSession, revocation: Revocation | None) -> None:
    try:
        if revocation is not None:
            await apply_revocation(db, revocation)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("Platform Health consent change not applied (%s)", revocation)
        raise HTTPException(status_code=500, detail=NOT_APPLIED_DETAIL) from None


# ── Agents ────────────────────────────────────────────────────────────────


@router.get("/agents/{agent_id}/opt-in", response_model=OptInResponse)
async def get_agent_opt_in(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OptInResponse:
    groups = await get_user_group_ids(db, user.id)
    agent = await get_user_agent(db, user.id, agent_id, groups, is_admin=user.is_admin)
    fleet = agent.health_role is not None
    return OptInResponse(
        opt_in=bool(agent.health_enrolled) and not fleet,
        can_manage=can_manage(user, agent, groups) and not fleet,
        eligible=not fleet,
    )


@router.put("/agents/{agent_id}/opt-in", response_model=OptInResponse)
async def set_agent_opt_in(
    agent_id: str,
    body: OptInUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OptInResponse:
    groups = await get_user_group_ids(db, user.id)
    agent = await get_user_agent(db, user.id, agent_id, groups, is_admin=user.is_admin)
    # A Platform Health agent is checked before the manage rule: since it is built in and
    # read-only, can_manage refuses everyone, and the conflict is the more precise answer.
    if agent.health_role is not None:
        raise HTTPException(status_code=409, detail="A Platform Health agent is never improved by Platform Health.")
    if not can_manage(user, agent, groups):
        raise HTTPException(status_code=403, detail="You cannot manage this agent.")
    was = bool(agent.health_enrolled)
    if was != body.opt_in:
        agent.health_enrolled = body.opt_in
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="agent",
            entity_id=agent.id,
            entity_name=agent.name,
            summary=f"Turned the Platform Health improvement opt-in {'on' if body.opt_in else 'off'}",
            details={"health_enrolled": body.opt_in},
            request=request,
        )
        revocation = None if body.opt_in else Revocation(kind="agent_opt_in_revoked", agent_id=agent.id)
        await _commit_with_revocation(db, revocation)
    return OptInResponse(opt_in=body.opt_in, can_manage=True)


# ── Flows ─────────────────────────────────────────────────────────────────


async def _visible_flow(db: AsyncSession, user: User, flow_id: str, groups: list[str]) -> Flow:
    flow = (
        await db.execute(
            select(Flow).where(
                Flow.id == parse_path_id(flow_id, not_found_detail="Flow not found"),
                user_visible_filter(Flow, user.id, groups, is_admin=user.is_admin),
            )
        )
    ).scalar_one_or_none()
    if flow is None:
        raise HTTPException(status_code=404, detail="Flow not found")
    return flow


@router.get("/flows/{flow_id}/opt-in", response_model=OptInResponse)
async def get_flow_opt_in(
    flow_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OptInResponse:
    groups = await get_user_group_ids(db, user.id)
    flow = await _visible_flow(db, user, flow_id, groups)
    return OptInResponse(opt_in=bool(flow.health_enrolled), can_manage=can_manage(user, flow, groups))


@router.put("/flows/{flow_id}/opt-in", response_model=OptInResponse)
async def set_flow_opt_in(
    flow_id: str,
    body: OptInUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OptInResponse:
    groups = await get_user_group_ids(db, user.id)
    flow = await _visible_flow(db, user, flow_id, groups)
    if not can_manage(user, flow, groups):
        raise HTTPException(status_code=403, detail="You cannot manage this flow.")
    if bool(flow.health_enrolled) != body.opt_in:
        flow.health_enrolled = body.opt_in
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="flow",
            entity_id=flow.id,
            entity_name=flow.name,
            summary=f"Turned the Platform Health improvement opt-in {'on' if body.opt_in else 'off'}",
            details={"health_enrolled": body.opt_in},
            request=request,
        )
        revocation = None if body.opt_in else Revocation(kind="flow_opt_in_revoked", flow_id=flow.id)
        await _commit_with_revocation(db, revocation)
    return OptInResponse(opt_in=body.opt_in, can_manage=True)


# ── The user's opt-out ────────────────────────────────────────────────────


@router.get("/me/opt-out", response_model=OptOutResponse)
async def get_my_opt_out(user: User = Depends(get_current_user)) -> OptOutResponse:
    return OptOutResponse(opt_out=bool(user.health_content_opt_out))


@router.put("/me/opt-out", response_model=OptOutResponse)
async def set_my_opt_out(
    body: OptOutUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OptOutResponse:
    row = (await db.execute(select(User).where(User.id == user.id).with_for_update())).scalar_one()
    if bool(row.health_content_opt_out) != body.opt_out:
        row.health_content_opt_out = body.opt_out
        await log_audit(
            db,
            user=row,
            action="update",
            entity_type="user",
            entity_id=row.id,
            entity_name=row.email,
            summary=f"{'Opted out of' if body.opt_out else 'Opted back in to'} Platform Health",
            details={"health_content_opt_out": body.opt_out},
            request=request,
        )
        revocation = Revocation(kind="user_opt_out", user_id=row.id) if body.opt_out else None
        await _commit_with_revocation(db, revocation)
    return OptOutResponse(opt_out=body.opt_out)


# ── Platform Health's name ───────────────────────────────────────────────────


class HealthNameResponse(BaseModel):
    #: What people call it: the name set in Settings > Agents and automations > Platform Health, else ``default_name``.
    name: str
    #: "Platform Health", the same in every platform mode (#101).
    default_name: str


@router.get("/name", response_model=HealthNameResponse)
async def get_health_name(
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HealthNameResponse:
    """The name every signed-in person sees Platform Health under."""
    return HealthNameResponse(name=await load_health_agent_name(db), default_name=default_health_agent_name())


# ── Settings > Agents and automations > Platform Health ──────────────────────────────────────────────────


@router.get("/settings", response_model=HealthSettings)
async def get_health_settings(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> HealthSettings:
    return await load_health_settings(db)


@router.put("/settings", response_model=HealthSettings)
async def put_health_settings(
    body: HealthSettings,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> HealthSettings:
    """Store the settings. A changed spend ceiling while they are locked (#1202) is refused (403), nothing written.

    A new "Run checks as" user (12.3) who cannot read a registered module is refused (400, the modules named),
    nothing written, as a fleet automation's run-as (``fleet_run_as``, #9188).
    """
    if body.module_run_as_user_id != (await load_stored_health_settings(db)).module_run_as_user_id:
        await check_module_run_as(db, body.module_run_as_user_id)
    try:
        before, after = await save_health_settings(db, body, actor=admin, request=request)
    except SpendLimitsLockedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    revocation = Revocation(kind="scope_narrowed") if scope_narrowed(before, after) else None
    await _commit_with_revocation(db, revocation)
    return after


async def check_module_run_as(db: AsyncSession, user_id: uuid.UUID | None) -> None:
    """400 when *user_id* cannot read every registered module the checks need; ``None`` passes (not set).

    The ``detail`` is ``{code, modules?, message}``: the client words the refusal from its catalogs.
    """
    if user_id is None:
        return
    from app.services.health.run_as import (
        missing_module_access,
        run_as_inactive_detail,
        run_as_refusal_detail,
        user_is_active,
    )

    if not await user_is_active(db, user_id):
        raise HTTPException(status_code=400, detail=run_as_inactive_detail())
    missing = await missing_module_access(db, user_id)
    if missing:
        raise HTTPException(status_code=400, detail=run_as_refusal_detail(missing))


class ModuleRunAsCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: uuid.UUID


class ModuleRunAsSaved(BaseModel):
    module_run_as_user_id: uuid.UUID | None


@router.put("/settings/run-as", response_model=ModuleRunAsSaved)
async def put_module_run_as(
    body: ModuleRunAsCheck,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ModuleRunAsSaved:
    """Set the "Run checks as" user alone (the onboarding step), validated as the settings save."""
    await check_module_run_as(db, body.user_id)
    current = await load_health_settings(db)
    updated = current.model_copy(update={"module_run_as_user_id": body.user_id})
    try:
        _before, after = await save_health_settings(db, updated, actor=admin, request=request)
    except SpendLimitsLockedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    await db.commit()
    return ModuleRunAsSaved(module_run_as_user_id=after.module_run_as_user_id)


class SpendLimit(BaseModel):
    """One spend ceiling: the value in force, its default, what the row holds and where the value comes from."""

    block: str
    key: str
    value: int | Decimal | None
    default: int | Decimal | None
    stored: int | Decimal | None
    #: ``default`` (the built-in value applies) or ``database`` (a stored value applies).
    source: str


class SpendLimitsResponse(BaseModel):
    """Whether the spend ceilings are locked at their defaults (#1202), and each of them."""

    locked: bool
    #: ``environment`` while locked: only the variable below unlocks them.
    locked_reason: str | None
    env_var: str
    limits: list[SpendLimit]


@router.get("/settings/spend-limits", response_model=SpendLimitsResponse)
async def get_spend_limits(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> SpendLimitsResponse:
    """The spend lock and every ceiling, so the page needs no second rule."""
    locked = not spend_limits_unlocked()
    return SpendLimitsResponse(
        locked=locked,
        locked_reason=SPEND_LIMITS_LOCKED_REASON if locked else None,
        env_var=SPEND_LIMITS_ENV_VAR,
        limits=[SpendLimit(**item) for item in describe_spend_limits(await load_stored_health_settings(db))],
    )


@router.get("/budget", response_model=BudgetResponse)
async def get_budget(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> BudgetResponse:
    month = health_budget.current_month()
    caps = (await load_health_settings(db)).budget
    row = (await db.execute(select(HealthBudgetMonth).where(HealthBudgetMonth.month == month))).scalar_one_or_none()
    left = await health_budget.remaining(db)
    refusal = await health_budget.budget_refusal(db)
    open_cycles = (
        await db.execute(select(func.count(HealthCycle.id)).where(HealthCycle.status == "open"))
    ).scalar_one()
    return BudgetResponse(
        month=month,
        caps=caps,
        used=BudgetUsage(
            tokens=row.spent_tokens if row else 0,
            cost_usd=row.spent_cost_usd if row else Decimal(0),
            reserved_tokens=row.reserved_tokens if row else 0,
            reserved_cost_usd=row.reserved_cost_usd if row else Decimal(0),
        ),
        remaining_tokens=left.month_tokens,
        remaining_cost_usd=left.month_cost_usd,
        open_cycles=open_cycles,
        refusal=refusal,
        refusal_message=health_budget.refusal_message(refusal, await load_health_agent_name(db)) if refusal else None,
    )
