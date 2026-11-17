# SPDX-License-Identifier: Apache-2.0

"""The Email signature cards of an integration and of an agent.

Read-only views. The settings are written through ``PUT /integrations/{id}``
(``signature_enabled``, ``signature_integration_id``) and ``PUT /agents/{id}``
(``integration_signatures``); these routes say what they resolve to, through
the resolver the tool build uses (``services/email_signature_settings``), for
the viewer as the acting person.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.helpers import (
    can_manage,
    get_user_agent,
    get_user_group_ids,
    get_user_integration,
    user_visible_filter,
)
from app.api.v1.schemas.email_signature import (
    AgentBindingSignature,
    AgentEmailSignaturesResponse,
    IntegrationEmailSignatureResponse,
    ResolvedSignature,
    SignatureIntegrationOption,
)
from app.core.database import get_db
from app.models.integration import Integration
from app.models.user import User
from app.services.email_signature_settings import (
    bound_integration_ids,
    plan_signatures,
    signature_integration_options,
    stored_overrides,
)
from app.services.email_signatures import SignaturePlan, email_sender_providers

integration_router = APIRouter()
agent_router = APIRouter()


def _resolved(plan: SignaturePlan | None) -> ResolvedSignature:
    plan = plan or SignaturePlan()
    return ResolvedSignature(
        enabled=plan.enabled,
        integration_id=plan.integration_id if plan.signs else None,
        integration_name=(plan.integration_name or None) if plan.signs else None,
        off_by=plan.off_by or None,
        missing=plan.missing or None,
    )


async def _options(db: AsyncSession, user: User) -> list[SignatureIntegrationOption]:
    return [
        SignatureIntegrationOption(id=iid, name=name, provider=provider)
        for iid, name, provider in await signature_integration_options(db, user.id)
    ]


@integration_router.get(
    "/{integration_id}/email-signature", response_model=IntegrationEmailSignatureResponse
)
async def get_integration_email_signature(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The integration's Email signature setting and what it resolves to; 404 when it sends no email."""
    groups = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(
        db, user.id, integration_id, groups, is_admin=user.is_admin
    )
    if integration.provider not in email_sender_providers():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This integration sends no email.")
    key = str(integration.id)
    plan = (await plan_signatures(db, {key: integration}, acting_user_id=user.id)).get(key)
    options = await _options(db, user)
    chosen = str(integration.signature_integration_id) if integration.signature_integration_id else None
    chosen_name = next((o.name for o in options if o.id == chosen), None)
    if chosen and chosen_name is None and plan is not None and plan.integration_id == chosen:
        chosen_name = plan.integration_name or None
    return IntegrationEmailSignatureResponse(
        enabled=integration.signature_enabled is not False,
        integration_id=chosen,
        integration_name=chosen_name,
        resolved=_resolved(plan),
        signature_integrations=options,
        can_manage=can_manage(user, integration, groups),
    )


@agent_router.get("/{agent_id}/email-signatures", response_model=AgentEmailSignaturesResponse)
async def get_agent_email_signatures(
    agent_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Each email-sending binding of the agent the viewer can see: its override, what it inherits, the result."""
    groups = await get_user_group_ids(db, user.id)
    agent = await get_user_agent(db, user.id, agent_id, groups, is_admin=user.is_admin)
    bound = bound_integration_ids(agent.integration_bindings)
    if not bound:
        return AgentEmailSignaturesResponse()
    ids = [uuid.UUID(iid) for iid in bound]
    rows = {
        str(row.id): row
        for row in (await db.execute(select(Integration).where(Integration.id.in_(ids)))).scalars().all()
    }
    senders = email_sender_providers()
    visible = {
        str(iid)
        for iid in (
            await db.execute(
                select(Integration.id).where(
                    Integration.id.in_(ids),
                    Integration.provider.in_(sorted(senders)),
                    user_visible_filter(Integration, user.id, groups, is_admin=user.is_admin),
                )
            )
        ).scalars().all()
    }
    if not visible:
        return AgentEmailSignaturesResponse()
    inherited = await plan_signatures(db, rows, acting_user_id=user.id)
    effective = await plan_signatures(
        db, rows, overrides=agent.integration_signatures, acting_user_id=user.id
    )
    overrides = stored_overrides(agent.integration_signatures)
    bindings = [
        AgentBindingSignature(
            integration_id=iid,
            integration_name=rows[iid].name,
            provider=rows[iid].provider,
            signature_mode=overrides.get(iid, {}).get("signature_mode", "inherit"),
            signature_integration_id=overrides.get(iid, {}).get("signature_integration_id"),
            inherited=_resolved(inherited.get(iid)),
            effective=_resolved(effective.get(iid)),
        )
        for iid in sorted(visible, key=lambda i: (rows[i].name or "", i))
    ]
    return AgentEmailSignaturesResponse(bindings=bindings, signature_integrations=await _options(db, user))
