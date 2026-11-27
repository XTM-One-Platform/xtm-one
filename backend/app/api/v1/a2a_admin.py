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

"""Internal exposure status — session-authenticated, read-only.

This router has ONE endpoint, and the absence of the other two is the shape of
the feature rather than an omission. Exposure is not a switch: an agent is
reachable exactly while it binds an A2A integration (``a2a.providers``), so
exposing and un-exposing happen through the ordinary agent-update call that
edits ``integration_bindings`` — the agent configuration's Tools tab — and the
flush hook in ``a2a.hooks`` keeps the exposure row in step wherever that edit
comes from. An explicit expose/revoke pair alongside it would be a second way
to reach the same state, and the two would disagree the moment one was used:
an exposure created without a binding is reachable with nothing to show for it
in the UI, which is exactly the drift migration ``a2a2intmodel1`` had to clean
up once already.

What is left is the read the agent configuration needs: the card URL to hand a
partner, and the identity external work runs as. One write sits beside it, and
it does not expose anything: ``PUT .../partner-fleet-reads`` sets the opt-in of
#2202 on the LIVE exposure (off by default), which lends a partner's task the
reads of the exposed agent's home fleet. It answers 404 when nothing is
exposed, so it can never create the state the bindings own. Authority is
``writable_agent_error()`` — the same predicate that decides who may edit the
agent — so owner / administrator / config-tier-group-member / company-managed
distinctions are inherited rather than re-invented.

Entra federation has no endpoints here either, for the same reason: its opt-in
is an ``entra_a2a`` integration bound through those same bindings.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.a2a.card import card_url, token_url
from app.a2a.exposure import get_live_exposure, user_can_expose
from app.api.deps import get_current_user, get_current_user_ungated, require_chat_enabled
from app.core.database import get_db
from app.core.utils import external_base
from app.models.agent import Agent
from app.models.user import User

# A2A is offered exactly while agentic chat is (AI on, Enterprise Edition). Declared once for the router: a router-level dependency runs before the
# route's own, so a deployment without chat answers the same 403 to everyone.
router = APIRouter(
    prefix="/agents/{agent_id}/a2a",
    tags=["a2a-admin"],
    # The caller is authenticated BEFORE the gate: it reads the person the user
    # dependency bound, to refuse a member of an AI-disabled group.
    dependencies=[Depends(get_current_user_ungated), Depends(require_chat_enabled)],
)


class ExposureStatus(BaseModel):
    exposed: bool
    card_url: str | None = None
    run_as_user_id: uuid.UUID | None = None
    # The re-displayable half of a LEGACY per-exposure credential: only an
    # exposure minted by the old explicit toggle has one, so this is null on
    # everything created since. A partner credential now belongs to the
    # ``oauth_a2a`` integration and is shown there, because it is valid on
    # every agent that binds it rather than on this one. The client_id is an
    # identifier, not a secret — the partner sends it in clear on every token
    # exchange — and the token URL is served openly in the agent card, so
    # neither needs the one-time treatment a secret gets. The secret itself
    # has no field here at all: nothing on this router can mint one.
    client_id: str | None = None
    token_url: str | None = None
    # The opt-in of #2202: a partner's task on this exposure also reads the
    # exposed agent's home fleet, its health, its work items and the running
    # build. Off by default; read at each partner call.
    partner_fleet_reads: bool = False
    # The opt-in of #2638: a partner's task on this exposure reads fleets,
    # fleet health and runs with the owner's read scope, and the running
    # build; writes stay refused, each read is audited. Off by default.
    partner_owner_reads: bool = False
    # Entra federation state is deliberately NOT part of this status: the
    # opt-in is an ``entra_a2a`` integration bound through the agent's
    # ordinary integration_bindings (Tools tab), so the agent configuration
    # is its single home — mirroring it here would be a second copy to drift.


async def _load_agent(db: AsyncSession, agent_id: uuid.UUID) -> Agent:
    agent = (
        await db.execute(select(Agent).where(Agent.id == agent_id))
    ).scalar_one_or_none()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


async def _status_of(
    request: Request, db: AsyncSession, exposure
) -> ExposureStatus:
    """Project a live exposure into its status.

    Kept as its own function even with one caller: it is the single definition
    of what a status read says, so a second read path added later starts from
    this rather than reassembling the fields and quietly disagreeing.
    """
    from app.models.oauth_client import OAuthClient

    client_id = (
        await db.execute(
            select(OAuthClient.client_id).where(
                OAuthClient.a2a_exposure_id == exposure.id
            )
        )
    ).scalar_one_or_none()
    return ExposureStatus(
        exposed=True,
        card_url=card_url(external_base(request), exposure.card_token),
        run_as_user_id=exposure.run_as_user_id,
        client_id=client_id,
        token_url=token_url(external_base(request)),
        partner_fleet_reads=bool(getattr(exposure, "partner_fleet_reads", False)),
        partner_owner_reads=bool(getattr(exposure, "partner_owner_reads", False)),
    )


@router.get("", response_model=ExposureStatus)
async def get_exposure_status(
    agent_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Whether *agent* is currently reachable, and on what terms.

    A 404 for an agent the caller may not manage, matching every other read on
    this feature: "not exposed", "not yours" and "does not exist" must stay
    indistinguishable (SC-008).
    """
    agent = await _load_agent(db, agent_id)
    if not await user_can_expose(db, user, agent):
        raise HTTPException(status_code=404, detail="Agent not found")

    exposure = await get_live_exposure(db, agent.id)
    if exposure is None:
        return ExposureStatus(exposed=False)
    return await _status_of(request, db, exposure)


class PartnerFleetReadsUpdate(BaseModel):
    enabled: bool


@router.put("/partner-fleet-reads", response_model=ExposureStatus)
async def set_partner_fleet_reads(
    agent_id: uuid.UUID,
    body: PartnerFleetReadsUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExposureStatus:
    """Switch the #2202 opt-in on the agent's live exposure.

    Same authority and the same 404 as the status read. No live exposure is a
    404 too: the opt-in belongs to an exposure, and exposing stays the
    bindings' job. Switching it off narrows the next call of a running task,
    since the partner tools read it at each call.
    """
    agent = await _load_agent(db, agent_id)
    if not await user_can_expose(db, user, agent):
        raise HTTPException(status_code=404, detail="Agent not found")

    exposure = await get_live_exposure(db, agent.id)
    if exposure is None:
        raise HTTPException(status_code=404, detail="Agent not exposed")
    exposure.partner_fleet_reads = body.enabled
    await db.commit()
    return await _status_of(request, db, exposure)


@router.put("/partner-owner-reads", response_model=ExposureStatus)
async def set_partner_owner_reads(
    agent_id: uuid.UUID,
    body: PartnerFleetReadsUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ExposureStatus:
    """Switch the #2638 opt-in on the agent's live exposure.

    Same authority, the same 404s and the same per-call reading as the #2202
    switch above; the change itself is audited, naming who flipped it.
    """
    from app.services.audit import log_audit

    agent = await _load_agent(db, agent_id)
    if not await user_can_expose(db, user, agent):
        raise HTTPException(status_code=404, detail="Agent not found")

    exposure = await get_live_exposure(db, agent.id)
    if exposure is None:
        raise HTTPException(status_code=404, detail="Agent not exposed")
    exposure.partner_owner_reads = body.enabled
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="agent",
        entity_id=agent.id,
        entity_name=agent.name,
        summary=f"A2A partner owner reads {'on' if body.enabled else 'off'}",
        details={"exposure_id": str(exposure.id), "partner_owner_reads": body.enabled},
        request=request,
    )
    await db.commit()
    return await _status_of(request, db, exposure)
