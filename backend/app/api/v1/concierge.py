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

"""Platform Concierge REST API (#8254): opt-in, opt-out, Settings > Agents and automations > Concierge, budget.

Every route needs the ``CONCIERGE`` flag. The rules (PRD
``dev-docs/platform-concierge.md`` 4 and 6.2):

* The improvement opt-in of an agent or a flow: read by whoever sees the
  target, written by whoever may manage it (``can_manage``: company-managed =
  admins), shared or not (decision B21). A Concierge fleet agent has none.
* The opt-out: the signed-in user's own, nobody else's.
* Settings and budget: administrators.

A revocation (opt-in off, opt-out on, scope narrowed) runs the registered
handlers in the same transaction (``consent.apply_revocation``): if one fails,
nothing changes and the caller gets a generic error.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_admin
from app.api.v1.helpers import can_manage, get_user_agent, get_user_group_ids, parse_path_id, user_visible_filter
from app.core.database import get_db
from app.core.feature_flags import require_feature
from app.models.concierge import ConciergeBudgetMonth, ConciergeCycle
from app.models.flow import Flow
from app.models.user import User
from app.services.audit import log_audit
from app.services.concierge import budget as concierge_budget
from app.services.concierge.consent import Revocation, apply_revocation
from app.services.concierge.naming import default_concierge_name, load_concierge_name
from app.services.concierge.settings import (
    ConciergeBudgetCaps,
    ConciergeSettings,
    load_concierge_settings,
    save_concierge_settings,
    scope_narrowed,
)

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=[Depends(require_feature("CONCIERGE"))])

#: What a caller is told when a revocation could not be applied (the reason is logged).
NOT_APPLIED_DETAIL = "The change could not be applied. Nothing was changed."


class OptInResponse(BaseModel):
    opt_in: bool
    can_manage: bool
    #: False for a Concierge fleet agent: it is never a target.
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
    caps: ConciergeBudgetCaps
    used: BudgetUsage
    remaining_tokens: int | None
    remaining_cost_usd: Decimal | None
    open_cycles: int
    #: Why no new Concierge work starts now, or ``None``.
    refusal: str | None
    refusal_message: str | None


async def _commit_with_revocation(db: AsyncSession, revocation: Revocation | None) -> None:
    try:
        if revocation is not None:
            await apply_revocation(db, revocation)
        await db.commit()
    except Exception:
        await db.rollback()
        logger.exception("Concierge consent change not applied (%s)", revocation)
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
    fleet = agent.concierge_role is not None
    return OptInResponse(
        opt_in=bool(agent.concierge_improvement_opt_in) and not fleet,
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
    if not can_manage(user, agent, groups):
        raise HTTPException(status_code=403, detail="You cannot manage this agent.")
    if agent.concierge_role is not None:
        raise HTTPException(status_code=409, detail="A Concierge agent is never improved by the Concierge.")
    was = bool(agent.concierge_improvement_opt_in)
    if was != body.opt_in:
        agent.concierge_improvement_opt_in = body.opt_in
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="agent",
            entity_id=agent.id,
            entity_name=agent.name,
            summary=f"Turned the Concierge improvement opt-in {'on' if body.opt_in else 'off'}",
            details={"concierge_improvement_opt_in": body.opt_in},
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
    return OptInResponse(opt_in=bool(flow.concierge_improvement_opt_in), can_manage=can_manage(user, flow, groups))


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
    if bool(flow.concierge_improvement_opt_in) != body.opt_in:
        flow.concierge_improvement_opt_in = body.opt_in
        await log_audit(
            db,
            user=user,
            action="update",
            entity_type="flow",
            entity_id=flow.id,
            entity_name=flow.name,
            summary=f"Turned the Concierge improvement opt-in {'on' if body.opt_in else 'off'}",
            details={"concierge_improvement_opt_in": body.opt_in},
            request=request,
        )
        revocation = None if body.opt_in else Revocation(kind="flow_opt_in_revoked", flow_id=flow.id)
        await _commit_with_revocation(db, revocation)
    return OptInResponse(opt_in=body.opt_in, can_manage=True)


# ── The user's opt-out ────────────────────────────────────────────────────


@router.get("/me/opt-out", response_model=OptOutResponse)
async def get_my_opt_out(user: User = Depends(get_current_user)) -> OptOutResponse:
    return OptOutResponse(opt_out=bool(user.concierge_opt_out))


@router.put("/me/opt-out", response_model=OptOutResponse)
async def set_my_opt_out(
    body: OptOutUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OptOutResponse:
    row = (await db.execute(select(User).where(User.id == user.id).with_for_update())).scalar_one()
    if bool(row.concierge_opt_out) != body.opt_out:
        row.concierge_opt_out = body.opt_out
        await log_audit(
            db,
            user=row,
            action="update",
            entity_type="user",
            entity_id=row.id,
            entity_name=row.email,
            summary=f"{'Opted out of' if body.opt_out else 'Opted back in to'} the Concierge",
            details={"concierge_opt_out": body.opt_out},
            request=request,
        )
        revocation = Revocation(kind="user_opt_out", user_id=row.id) if body.opt_out else None
        await _commit_with_revocation(db, revocation)
    return OptOutResponse(opt_out=body.opt_out)


# ── The Concierge's name ───────────────────────────────────────────────────


class ConciergeNameResponse(BaseModel):
    #: What people call it: Settings > Agents and automations > Concierge's name, else ``default_name``.
    name: str
    #: The platform mode's name ("Alfred" in copilot mode, "Concierge" otherwise).
    default_name: str


@router.get("/name", response_model=ConciergeNameResponse)
async def get_concierge_name(
    _user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConciergeNameResponse:
    """The name every signed-in person sees the Concierge under."""
    return ConciergeNameResponse(name=await load_concierge_name(db), default_name=default_concierge_name())


# ── Settings > Agents and automations > Concierge ──────────────────────────────────────────────────


@router.get("/settings", response_model=ConciergeSettings)
async def get_concierge_settings(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ConciergeSettings:
    return await load_concierge_settings(db)


@router.put("/settings", response_model=ConciergeSettings)
async def put_concierge_settings(
    body: ConciergeSettings,
    request: Request,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> ConciergeSettings:
    before, after = await save_concierge_settings(db, body, actor=admin, request=request)
    revocation = Revocation(kind="scope_narrowed") if scope_narrowed(before, after) else None
    await _commit_with_revocation(db, revocation)
    return after


@router.get("/budget", response_model=BudgetResponse)
async def get_budget(
    _admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> BudgetResponse:
    month = concierge_budget.current_month()
    caps = (await load_concierge_settings(db)).budget
    row = (
        await db.execute(select(ConciergeBudgetMonth).where(ConciergeBudgetMonth.month == month))
    ).scalar_one_or_none()
    left = await concierge_budget.remaining(db)
    refusal = await concierge_budget.budget_refusal(db)
    open_cycles = (
        await db.execute(select(func.count(ConciergeCycle.id)).where(ConciergeCycle.status == "open"))
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
        refusal_message=concierge_budget.refusal_message(refusal, await load_concierge_name(db)) if refusal else None,
    )
