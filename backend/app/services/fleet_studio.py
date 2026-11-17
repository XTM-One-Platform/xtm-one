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

"""Fleet "Modify with AI" - a chat bound to the fleet it changes.

The twin of the Dashboard Studio (``services/dashboard_studio.py``): the fleet
page docks a conversation next to the fleet, and ``Conversation.fleet_id`` is
what binds it. Per turn (:func:`load_fleet_studio_context`, against the CALLING
user's own visibility, no admin reach) the chat layer derives what the agent
needs to change the fleet without the user naming it: which fleet, its
orchestrator, its members, its workspace, its autonomy level and whether the
user may change it (:meth:`FleetStudioContext.prompt`); it guarantees the
platform tools that carry the fleet functions (:attr:`FleetStudioContext.tool_ids`).
A fleet since unshared, archived or deleted drops out: the conversation goes
on as an ordinary chat. What each fleet tool guarantees (permission checks,
confirmations, audit) is the tool's own contract (invariant 16).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.fleet import Fleet

#: Built-in tool ids every fleet turn carries, on top of the agent's own: the
#: platform group holds the fleet functions (``builtin_tool_groups``).
STUDIO_TOOL_IDS: tuple[str, ...] = ("platform",)

#: Members named in the prompt; the rest are counted (get_fleet lists them all).
_MEMBERS_SHOWN = 30

_ACCESS_NOTES = {
    True: "you may change it with the fleet tools, as the user",
    False: (
        "READ-ONLY - the user may not change this fleet: the fleet tools will refuse; explain what "
        "you would change, or offer a copy (export_fleet_spec then apply_fleet_spec)"
    ),
}


@dataclass(frozen=True, slots=True)
class FleetStudioContext:
    """The fleet a conversation is bound to, as the calling user sees it."""

    fleet: Fleet
    can_edit: bool
    workspace_name: str
    orchestrator_name: str | None
    #: (agent name, role) of each agent member, orchestrator first.
    members: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    member_count: int = 0
    tool_ids: tuple[str, ...] = STUDIO_TOOL_IDS

    def prompt(self) -> str:
        return format_fleet_studio_prompt(self)


async def load_fleet_studio_context(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    fleet_id: uuid.UUID | None,
) -> FleetStudioContext | None:
    """Resolve a conversation's fleet binding for one turn, or ``None``.

    ``None`` when there is no binding, when the fleet is archived, or when it
    is not (or no longer) visible to *user_id* as themselves - the agent must
    not learn that a fleet exists from a binding the user can no longer follow.
    """
    if fleet_id is None:
        return None
    from app.api.v1.helpers import can_manage_as, get_user_group_ids, user_visible_filter
    from app.models.agent import Agent
    from app.models.fleet import FleetMember
    from app.models.user import User
    from app.models.workspace import Workspace

    user = await db.get(User, user_id)
    if user is None:
        return None
    group_ids = await get_user_group_ids(db, user_id)
    # As themselves: an administrator reads only the fleets they would see
    # without the admin bypass, like the studios' bindings.
    fleet = (
        await db.execute(
            select(Fleet).where(
                Fleet.id == fleet_id,
                Fleet.is_archived.is_(False),
                user_visible_filter(Fleet, user_id, group_ids, is_admin=False),
            )
        )
    ).scalar_one_or_none()
    if fleet is None:
        return None
    workspace_name = (await db.execute(select(Workspace.name).where(Workspace.id == fleet.workspace_id))).scalar()
    rows = (
        await db.execute(
            select(Agent.id, Agent.name, FleetMember.role)
            .join(FleetMember, FleetMember.agent_id == Agent.id)
            .where(FleetMember.fleet_id == fleet.id)
            .order_by(Agent.name)
        )
    ).all()
    orchestrator_name = next((name for aid, name, _role in rows if aid == fleet.orchestrator_agent_id), None)
    ordered = sorted(rows, key=lambda r: r[0] != fleet.orchestrator_agent_id)
    members = tuple((name, role) for _aid, name, role in ordered[:_MEMBERS_SHOWN])
    return FleetStudioContext(
        fleet=fleet,
        can_edit=can_manage_as(user_id, user.is_admin, fleet, group_ids),
        workspace_name=workspace_name or "",
        orchestrator_name=orchestrator_name,
        members=members,
        member_count=len(rows),
    )


def format_fleet_studio_prompt(ctx: FleetStudioContext) -> str:
    """The system-prompt block for a fleet turn: identity, access, roster, autonomy.

    Routing only - what each fleet tool guarantees is the tool's own
    description (invariant 16).
    """
    fleet = ctx.fleet
    lines = [
        "\n[Fleet]",
        (
            f'The user has the fleet "{fleet.name}" open next to this chat and is changing it with you. '
            f'fleet_id: {fleet.id} - workspace "{ctx.workspace_name}", {ctx.member_count} member(s), '
            f"autonomy level {fleet.autonomy_level}. Their page refreshes the moment you save."
        ),
        f"- Access: {_ACCESS_NOTES[ctx.can_edit]}.",
        (
            f"- Every change goes to THIS fleet (fleet_id={fleet.id}); never ask which fleet. "
            "Read get_fleet first when you need more than the roster below."
        ),
    ]
    if fleet.is_builtin:
        lines.append("- This is a built-in fleet: only its writable switches change; offer Duplicate for the rest.")
    if ctx.orchestrator_name:
        lines.append(f"- Orchestrator: {ctx.orchestrator_name}.")
    roster = ", ".join(f"{name} ({role})" for name, role in ctx.members) or "(no agent member)"
    more = ctx.member_count - len(ctx.members)
    lines.append(f"Members: {roster}" + (f", and {more} more" if more > 0 else "") + ".")
    return "\n".join(lines)


__all__ = ["STUDIO_TOOL_IDS", "FleetStudioContext", "load_fleet_studio_context"]
