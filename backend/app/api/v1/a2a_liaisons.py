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

"""The A2A liaisons of the agents the caller sees, for the agent graphs.

One read: ``GET /api/v1/a2a/liaisons``. The Agentic Flow map and the fleet
graph draw its partner nodes beside the agents they already show, so the
answer covers every agent visible to the caller and each graph keeps the
edges whose agent it draws. Visibility is the platform's: an agent or an A2A
integration the caller cannot see contributes nothing.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.a2a.liaisons import (
    LiaisonAgent,
    LiaisonIntegration,
    LiaisonTarget,
    build_liaisons,
)
from app.a2a.providers import ALL_A2A_PROVIDERS, bound_integration_ids, directions_of
from app.api.deps import get_current_user, get_current_user_ungated, require_agentic_ai_licensed
from app.api.v1.helpers import get_user_group_ids, user_visible_filter
from app.core.database import get_db
from app.models.a2a import A2AClientTarget
from app.models.agent import Agent
from app.models.integration import Integration
from app.models.user import User

# A read the agent graphs draw, browsable with AI switched off (FR-008): the
# licence gate alone, never the AI state that gates the A2A write routers.
router = APIRouter(
    prefix="/a2a",
    tags=["a2a-liaisons"],
    dependencies=[Depends(get_current_user_ungated), Depends(require_agentic_ai_licensed)],
)


class PartnerNodeOut(BaseModel):
    """A partner drawn outside the platform. No credential, ever."""

    id: str
    name: str
    integration_id: str
    integration_name: str
    provider: str
    #: ``inbound`` (the partner tasks our agent) or ``outbound`` (our agent
    #: delegates to the partner agent).
    direction: str
    card_urls: list[str]


class LiaisonEdgeOut(BaseModel):
    source_id: str
    target_id: str
    direction: str
    integration_id: str


class LiaisonsOut(BaseModel):
    partners: list[PartnerNodeOut]
    edges: list[LiaisonEdgeOut]


@router.get("/liaisons", response_model=LiaisonsOut)
async def list_liaisons(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> LiaisonsOut:
    """Partner nodes and directed edges of the caller's visible A2A bindings."""
    group_ids = await get_user_group_ids(db, user.id)
    agent_rows = (
        await db.execute(
            select(Agent.id, Agent.integration_bindings).where(
                user_visible_filter(Agent, user.id, group_ids, is_admin=user.is_admin)
            )
        )
    ).all()
    agents = [LiaisonAgent(id=row[0], integration_bindings=row[1]) for row in agent_rows]
    bound = {iid for agent in agents for iid in bound_integration_ids(agent)}
    if not bound:
        return LiaisonsOut(partners=[], edges=[])

    integration_rows = (
        await db.execute(
            select(Integration.id, Integration.name, Integration.provider).where(
                Integration.id.in_(bound),
                Integration.provider.in_(sorted(ALL_A2A_PROVIDERS)),
                user_visible_filter(Integration, user.id, group_ids, is_admin=user.is_admin),
            )
        )
    ).all()
    integrations = [LiaisonIntegration(id=row[0], name=row[1], provider=row[2]) for row in integration_rows]
    visible_ids = [i.id for i in integrations]
    if not visible_ids:
        return LiaisonsOut(partners=[], edges=[])
    by_text = await directions_of(db, visible_ids)
    directions = {i.id: by_text[str(i.id)] for i in integrations if str(i.id) in by_text}
    target_rows = (
        await db.execute(
            select(
                A2AClientTarget.id,
                A2AClientTarget.integration_id,
                A2AClientTarget.card_url,
                A2AClientTarget.declared_name,
            )
            .where(A2AClientTarget.integration_id.in_(visible_ids))
            .order_by(A2AClientTarget.created_at, A2AClientTarget.id)
        )
    ).all()
    targets = [
        LiaisonTarget(id=row[0], integration_id=row[1], card_url=row[2], declared_name=row[3]) for row in target_rows
    ]

    liaisons = build_liaisons(agents, integrations, directions, targets)
    return LiaisonsOut(
        partners=[PartnerNodeOut(**vars(p)) for p in liaisons.partners],
        edges=[LiaisonEdgeOut(**vars(e)) for e in liaisons.edges],
    )
