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

"""Reading and repairing an outbound A2A registration.

Deliberately small. Creating, editing, sharing and deleting a registration are
the ORDINARY integration endpoints — a partner is an integration — and binding
it to an agent is the ordinary agent update. What is left is a read of the
verification state plus two repair actions.

**No expose/revoke pair, and no direction endpoint.** Two paths to one state
disagree the moment either is used, and endpoint-created state is invisible
in a UI deriving truth from bindings. Direction is set through the
ordinary integration update for the same reason.
"""

from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.a2a.client.verification import accept_card
from app.a2a.providers import (
    ALL_A2A_PROVIDERS,
    get_settings,
    get_target,
    outbound_targets,
)
from app.api.deps import get_current_user, get_current_user_ungated, require_chat_enabled
from app.api.v1.helpers import can_manage, get_user_group_ids, get_user_integration
from app.core.database import get_db
from app.models.a2a import DIRECTION_OUTBOUND
from app.models.user import User

logger = logging.getLogger(__name__)

# A2A is offered exactly while agentic chat is (AI on, Enterprise Edition). Declared once for the router; the inbound admin router does the same.
router = APIRouter(
    prefix="/a2a/client",
    tags=["a2a-client"],
    # The caller is authenticated BEFORE the gate: it reads the person the user
    # dependency bound, to refuse a member of an AI-disabled group.
    dependencies=[Depends(get_current_user_ungated), Depends(require_chat_enabled)],
)


class TargetState(BaseModel):
    """What an operator needs to know about ONE agent at a partner.

    Carries no credential and no fragment of one — not in a field, not in
    ``last_verification_error``, which is rendered in the UI and written to
    the audit trail (FR-004).
    """

    id: str
    card_url: str
    #: The tool an agent bound to this registration sees for THIS partner
    #: agent — one per agent, so a model choosing between them can. Computed
    #: where the names are minted, disambiguated across the registration, so
    #: the UI cannot show a name the model never sees.
    tool_name: str | None = None
    #: ``usable`` / ``limited`` / ``unusable`` — the health verdict (FR-020).
    usability: str
    unusable_reason: str | None = None
    last_verified_at: str | None = None
    last_verification_error: str | None = None
    #: ``verified`` when the card proved itself, ``unsigned`` when it carried
    #: no signature, ``None`` before a first verdict. An unverified card is
    #: never presented as verified (FR-017).
    signature_state: str | None = None
    agreed_protocol_version: str | None = None
    #: ``JSONRPC`` or ``HTTP+JSON`` — which binding the verified endpoint
    #: speaks: the first question when a partner answers 404 to everything.
    transport: str | None = None
    # What the verified card declares, shown BEFORE the registration is put
    # to use (FR-018).
    declared_name: str | None = None
    declared_description: str | None = None
    declared_skills: list | None = None
    declared_input_types: list | None = None
    declared_output_types: list | None = None
    # Whether the partner says it will call back, so an operator can see why a
    # delegation waited the way it did. NULL until the next verification.
    declares_callbacks: bool | None = None
    # Circuit state, so "why is nothing happening" has a visible answer.
    circuit_open_until: str | None = None
    #: True when the partner's card changed materially and delegation is
    #: suspended pending acceptance (FR-019).
    card_change_pending: bool = False
    #: Currently observed fingerprint. Returned because ``accept-card``
    #: requires it back and refuses a stale one — acceptance applies to the
    #: card the operator read, not to whichever arrives next.
    card_fingerprint: str | None = None


async def _load(db: AsyncSession, user: User, integration_id: str, *, manage: bool = True):
    """The registration, its settings row, and the authority to touch them.

    ``manage=False`` for the ONE read the page-level direction lookup makes:
    anyone who can see the integration may read its verification state, as
    they already see its status, and that lookup tolerates only a 404 — a 403
    replaced the whole detail page for viewers.

    Everything else keeps ``can_manage``, the delegation history included: it
    names the agents that delegated, and agents are scoped in their own
    right, so a company-managed registration would otherwise report a private
    agent's name and timings to every user who can see the integration.
    """
    group_ids = await get_user_group_ids(db, user.id)
    integration = await get_user_integration(
        db, user.id, integration_id, group_ids, is_admin=user.is_admin
    )
    if integration.provider not in ALL_A2A_PROVIDERS:
        raise HTTPException(status_code=404, detail="Not Found")
    if manage and not can_manage(user, integration, group_ids):
        raise HTTPException(
            status_code=403,
            detail="You don't have permission to manage this integration.",
        )
    row = await get_settings(db, integration.id)
    if row is None or row.direction != DIRECTION_OUTBOUND:
        # An inbound registration has no verification state to show, and
        # saying so through the same not-found outcome keeps this surface
        # from reporting on the other direction.
        raise HTTPException(status_code=404, detail="Not Found")
    # Returned rather than re-derived: the relaxed read still needs to know
    # whether this caller may cause a WRITE, and asking twice is how the two
    # answers drift apart.
    return integration, row, manage or can_manage(user, integration, group_ids)


#: Seconds between two discovery probes for one integration on the read path.
#: Shorter than the verification card's 30s poll on purpose — see
#: ``_discover_if_due``.
_DISCOVERY_THROTTLE_SECONDS = 10.0

#: When each integration last asked its partner which agents it exposes.
#: Process-local on purpose: it throttles a courtesy call, so losing it on
#: restart costs one extra request and nothing else. A column would make a
#: cache into schema, and a shared store would make it infrastructure.
_LAST_DISCOVERY: dict[str, float] = {}


async def _discover_tolerantly(db, integration) -> bool:
    """Run discovery; on failure leave the session usable. Returns whether targets changed.

    A partner that will not answer must not break the page reporting on it.
    The rollback clears the half-written transaction. The refresh comes
    before anything reads ``integration`` again: a rollback expires it, and
    an expired attribute on an async session raises instead of lazy-loading.
    """
    from app.a2a.client.registration import discover_partner_agents

    try:
        added, removed = await discover_partner_agents(db, integration)
    except Exception:
        await db.rollback()
        await db.refresh(integration)
        logger.warning(
            "a2a.client: discovery failed for integration %s", integration.id,
            exc_info=True,
        )
        return False
    return bool(added or removed)


async def _discover_if_due(db, integration) -> None:
    """Ask the partner what it exposes, at most once per staleness window.

    A GET that changes stored state is a compromise, made deliberately and
    worth naming. The alternative was worse in practice: an agent bound on the
    partner's side appeared here only after someone pressed Verify or after
    the hourly sweep, so the page contradicted the partner for up to an hour
    and the honest-looking answer — "refresh is a pure read" — was the thing
    making it wrong.

    The window MUST stay shorter than the UI's 30-second poll: a longer one
    is kept permanently warm by the poll and refuses exactly the refresh that
    matters — an operator changing something on the partner and reloading. A
    throttle exists to collapse a burst of reads from several open tabs, not
    to ration freshness.

    Partners that do not implement discovery cost nothing at all: their cards
    do not declare the extension, so there is no call to make — a Foundry or
    Vertex registration is never asked rather than asked and refused.
    """
    window = _DISCOVERY_THROTTLE_SECONDS
    key = str(integration.id)
    now = time.monotonic()
    # ``None`` sentinel, never ``0.0``: monotonic time counts from boot, and
    # a zero default would throttle the first read after a machine starts.
    last = _LAST_DISCOVERY.get(key)
    if last is not None and now - last < window:
        return
    _LAST_DISCOVERY[key] = now
    # Bounded: an entry past the window is dead weight.
    for stale in [k for k, t in _LAST_DISCOVERY.items() if now - t >= window]:
        del _LAST_DISCOVERY[stale]
    await _discover_tolerantly(db, integration)
    # Unconditionally: nothing to land when no target changed (or the failure
    # was rolled back), and the page reads the targets right after this.
    await db.commit()


def _state(row, *, integration=None, taken: set[str] | None = None) -> TargetState:
    from app.models.a2a import USABILITY_USABLE

    tool_name = None
    # Only a usable target has a tool, and only a usable target takes a name —
    # the builder skips the rest before naming anything. Letting one that is
    # not offered consume a name hands the base name to a target the model
    # never sees and an id-suffixed one to the target that gets it, which is
    # the one thing sharing ``taken`` exists to prevent. The row itself is
    # still listed: an operator cannot press Verify on what the page hides.
    if integration is not None and row.usability == USABILITY_USABLE:
        from app.a2a.client.tool import tool_name_for

        tool_name = tool_name_for(integration, row, taken=taken or set())
        if taken is not None:
            taken.add(tool_name)
    return TargetState(
        tool_name=tool_name,
        id=str(row.id),
        card_url=row.card_url,
        usability=row.usability,
        unusable_reason=row.unusable_reason,
        last_verified_at=row.last_verified_at.isoformat() if row.last_verified_at else None,
        last_verification_error=row.last_verification_error,
        signature_state=row.signature_state,
        agreed_protocol_version=row.agreed_protocol_version,
        transport=row.transport,
        declared_name=row.declared_name,
        declared_description=row.declared_description,
        declared_skills=row.declared_skills,
        declared_input_types=row.declared_input_types,
        declared_output_types=row.declared_output_types,
        declares_callbacks=row.declares_callbacks,
        circuit_open_until=row.circuit_open_until.isoformat() if row.circuit_open_until else None,
        card_fingerprint=row.card_fingerprint,
        card_change_pending=(
            row.card_fingerprint is not None
            and row.accepted_card_fingerprint is not None
            and row.card_fingerprint != row.accepted_card_fingerprint
        ),
    )


def _states(integration, rows) -> tuple[list[TargetState], str | None]:
    """Every target with its tool name, and the registration's cancel tool.

    One ``taken`` set across both, exactly as the builder uses: a name shown
    here that the builder would have disambiguated is a name the model never
    sees.
    """
    from app.a2a.client.tool import build_cancel_tool

    taken: set[str] = set()
    states = [_state(r, integration=integration, taken=taken) for r in rows]
    # Built only where something could be cancelled, like the tool itself: the
    # builder offers a cancel only when it built a delegation tool, and a
    # target that is not usable gets neither. ``taken`` is non-empty exactly
    # when one of them did, so it is the same condition rather than a second
    # one that can drift from it — keyed on a row existing, this named a
    # cancel tool for a partner none of whose agents can be reached.
    cancel = build_cancel_tool(integration, taken=taken).name if taken else None
    return states, cancel


async def _callbacks_reachable() -> bool:
    """Best-effort: an unreadable verdict must not cost the page."""
    try:
        from app.a2a.client import callbacks as callback_svc

        return await callback_svc.deployment_is_reachable()
    except Exception:
        return False


class RegistrationState(BaseModel):
    """The registration, and every agent it can reach.

    Always an OUTBOUND registration: ``_load`` answers 404 for any other
    direction, so this surface never reports on the inbound half.
    """

    targets: list[TargetState] = []
    #: Whether a partner could reach this deployment to call back. The one
    #: thing an outbound registration exposes depends on it.
    callbacks_reachable: bool = False
    #: The one cancel tool this registration confers, alongside a delegation
    #: tool per agent. ``None`` when no agent has verified: there is nothing
    #: to cancel and the tool is not built.
    cancel_tool_name: str | None = None


@router.get("/{integration_id}", response_model=RegistrationState)
async def get_verification_state(
    integration_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """What this partner declares, and whether we can currently reach each agent."""
    integration, _row, may_write = await _load(db, user, integration_id, manage=False)
    # Discovery WRITES — it syncs the target table and commits — so it runs
    # only for a caller who could have made those writes deliberately. The
    # relaxation above buys a viewer sight, not the right to delete their
    # target rows and spend the partner credential.
    if may_write:
        await _discover_if_due(db, integration)
    targets = await outbound_targets(db, integration.id)
    states, cancel = _states(integration, targets)
    return RegistrationState(
        targets=states,
        callbacks_reachable=await _callbacks_reachable(),
        cancel_tool_name=cancel,
    )


@router.post("/{integration_id}/verify", response_model=RegistrationState)
async def verify_now(
    integration_id: str,
    request: Request,
    target_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Re-verify this partner's card now.

    Also the operator's way out of a cooldown: a successful verification
    closes the circuit, so someone who has just corrected a credential is not
    made to wait one out (FR-030).
    """
    from app.a2a.client import breaker
    from app.a2a.client.registration import verify_registration
    from app.services.audit import log_audit

    integration, _row, _ = await _load(db, user, integration_id)
    if not target_id:
        # Ask the partner what it exposes BEFORE verifying, so a "Verify"
        # press picks up an agent bound on the partner's side since the last
        # one. A no-op unless the operator opted in, and for every partner
        # that does not implement it.
        # Tolerant like the read path: a lost ``uq_a2a_target_card`` race must
        # not turn "Verify" into a 500. Committed with the verdicts below.
        await _discover_tolerantly(db, integration)
    targets = await outbound_targets(db, integration.id)
    if target_id:
        # One agent, on request. Verifying the whole partner to check one card
        # would re-open a sibling's cooldown as a side effect of an operator
        # asking about something else.
        targets = [t for t in targets if str(t.id) == target_id]
        if not targets:
            raise HTTPException(status_code=404, detail="Not Found")

    for target in targets:
        verdict = await verify_registration(db, integration, target, trigger="manual")
        if verdict.is_usable:
            # FR-030's escape hatch: success closes the circuit, per target.
            breaker.close(target)

    await log_audit(
        db,
        user=user,
        action="verify",
        entity_type="integration",
        entity_id=integration.id,
        entity_name=integration.name,
        summary=(
            f"Verified {len(targets)} A2A partner card(s) for '{integration.name}'"
        ),
        request=request,
        details={
            "targets": [
                {"card_url": t.card_url, "usability": t.usability,
                 "reason": t.unusable_reason}
                for t in targets
            ]
        },
    )
    await db.commit()
    states, cancel = _states(
        integration, await outbound_targets(db, integration.id)
    )
    return RegistrationState(
        targets=states,
        callbacks_reachable=await _callbacks_reachable(),
        cancel_tool_name=cancel,
    )


class DelegationEntry(BaseModel):
    """One delegation this registration sent, as the UI renders it."""

    id: str
    agent_name: str | None = None
    outcome: str | None = None
    partner_reason: str | None = None
    protocol_version: str
    #: Which provenance the card had when THIS delegation ran — the
    #: registration's may have changed since (FR-017).
    signature_state: str
    continuation_count: int = 0
    #: How this delegation waited — asked, streamed, called back (FR-020). The
    #: question it answers is "why did this one take ten minutes", which the
    #: outcome alone cannot. NULL on a row written before it was recorded.
    mechanism: str | None = None
    #: Set when this side gave up on a silent partner. The distinction
    #: ``outcome`` cannot draw: a ``timeout`` from the attempt's ceiling
    #: cancelled the task at the partner, while this one cancelled nothing —
    #: so the work may still be running there, and still costing. That is the
    #: question an operator asks about a timeout, and it has one answer here.
    abandoned_at: str | None = None
    submitted_at: str
    #: When the delegation ended; NULL while it is outstanding. Without it the
    #: list says how a delegation ended but not when, which is the first
    #: question of an operator reading a refusal against a partner's report.
    terminal_at: str | None = None


@router.get("/{integration_id}/delegations", response_model=list[DelegationEntry])
async def list_delegations(
    integration_id: str,
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """What this partner was asked to do, newest first.

    Scoped by the ``integration_id`` recorded on each delegation at submission
    time — NOT by the agents this integration is bound to. Two registrations
    may be bound to one agent, and filtering by binding would make each report
    the other's traffic (FR-042).

    Needs authority over the integration, not merely sight of it: an entry
    names the agent that delegated, and the join that resolves that name
    applies no agent visibility of its own.
    """
    from sqlalchemy import select

    from app.models.a2a import A2ADelegation
    from app.models.agent import Agent

    integration, _, _ = await _load(db, user, integration_id)

    rows = (
        await db.execute(
            select(A2ADelegation, Agent.name)
            .outerjoin(Agent, Agent.id == A2ADelegation.agent_id)
            .where(A2ADelegation.integration_id == integration.id)
            .order_by(A2ADelegation.submitted_at.desc())
            .limit(limit)
        )
    ).all()

    return [
        DelegationEntry(
            id=str(d.id),
            agent_name=agent_name,
            outcome=d.outcome,
            partner_reason=d.partner_reason,
            protocol_version=d.protocol_version,
            signature_state=d.signature_state,
            continuation_count=d.continuation_count,
            mechanism=d.mechanism,
            abandoned_at=d.abandoned_at.isoformat() if d.abandoned_at else None,
            submitted_at=d.submitted_at.isoformat(),
            terminal_at=d.terminal_at.isoformat() if d.terminal_at else None,
        )
        for d, agent_name in rows
    ]


class AcceptCardRequest(BaseModel):
    #: WHICH agent's card is being accepted. Required now that a partner may
    #: expose several: accepting "the card" of a registration with three of
    #: them would be a trust decision the operator did not make about two.
    target_id: str
    #: The fingerprint being accepted. Required, and checked against what is
    #: currently observed: a trust decision applies to ONE card, not to
    #: whichever arrives next (FR-017).
    fingerprint: str


@router.post("/{integration_id}/accept-card", response_model=TargetState)
async def accept_card_change(
    integration_id: str,
    body: AcceptCardRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Accept a partner's changed card and resume delegation (FR-019)."""
    from app.services.audit import log_audit

    integration, _registration, _ = await _load(db, user, integration_id)
    try:
        row = await get_target(db, uuid.UUID(body.target_id))
    except ValueError:
        row = None
    # Scoped to THIS integration as well as found: a target id from another
    # registration is someone else's trust decision, and the id arrives in a
    # request body.
    if row is None or row.integration_id != integration.id:
        raise HTTPException(status_code=404, detail="Not Found")

    if row.unusable_reason == "signature_invalid":
        # No operator override, in any case (FR-015). Accepting here would be
        # the override the requirement forbids, wearing a different name.
        raise HTTPException(
            status_code=409,
            detail="This card's signature does not verify and cannot be accepted.",
        )
    if row.card_fingerprint is None:
        raise HTTPException(status_code=409, detail="Nothing to accept.")
    if body.fingerprint != row.card_fingerprint:
        raise HTTPException(
            status_code=409,
            detail=(
                "That fingerprint is not the card currently observed. "
                "Re-read the change before accepting it."
            ),
        )

    accept_card(row)
    await log_audit(
        db,
        user=user,
        action="accept_card",
        entity_type="integration",
        entity_id=integration.id,
        entity_name=integration.name,
        summary=(
            f"Accepted a changed partner card for '{integration.name}' "
            f"({row.declared_name or row.card_url})"
        ),
        request=request,
        details={"card_url": row.card_url, "fingerprint": row.card_fingerprint},
    )
    await db.commit()
    return _state(row)
