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

"""``get_user_default_agents``: the agent each person chose as their own default in chat.

``users.default_agent_id`` is the agent a person's chat opens on instead of the
General Assistant (Profile > Default chat agent). The platform also writes it:
it wires the CTEM Assistant for everyone with none (``provisioning``, SSO,
SCIM, registration). ``users.default_agent_source`` tells the two apart
(issue #529); a row written before that column existed is read as the
person's choice unless it points at the CTEM Assistant, which only the
platform wires. A default the platform wired is never reported as a choice.

Who can be read (:func:`reach`): an administrator's agent reads everyone; an
agent reads the people it is shared with (its owner, the users and the current
members of the groups it is shared with) and the people of the workspace of a
fleet it orchestrates (the workspace's owner, its shared users and the current
members of its shared groups); anyone else reads only themselves. A
company-managed agent or workspace widens nothing: being open to everyone is
not a relation to a person. Read only: each person switches their default
themselves.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import safe_uuid
from app.models.agent import Agent
from app.models.fleet import Fleet, FleetMember
from app.models.group import GroupMember
from app.models.user import User
from app.models.workspace import Workspace
from app.services.platform_insights.scope import (
    MAX_REFERENCES,
    InsightRefusedError,
    InsightScope,
    as_list,
    group_member_ids,
    resolve_groups,
)

#: The agent the platform wires as everyone's default (``provisioning.wire_default_agents``).
PLATFORM_WIRED_SLUG = "ctem-assistant"

AGENT_SCOPE = "agent"
NONE_SET = "none set: platform default applies"
PLATFORM_SET = "none chosen: the platform wired this default"


@dataclass(frozen=True)
class Reach:
    """Who the call may name: ``None`` is everyone (the administrator scope)."""

    label: str
    note: str
    user_ids: frozenset[str] | None


def _ids(value: Any) -> set[uuid.UUID]:
    return {parsed for entry in as_list(value) if (parsed := safe_uuid(entry)) is not None}


async def _members(db: AsyncSession, group_ids: set[uuid.UUID]) -> set[str]:
    if not group_ids:
        return set()
    rows = await db.execute(select(GroupMember.user_id).where(GroupMember.group_id.in_(group_ids)))
    return {str(user_id) for (user_id,) in rows.all()}


async def agent_reach(db: AsyncSession, agent_id: str | None) -> set[str]:
    """The people *agent_id* relates to: shared with it, or in a fleet workspace it orchestrates."""
    parsed = safe_uuid(agent_id) if agent_id else None
    if parsed is None:
        return set()
    agent = await db.get(Agent, parsed)
    if agent is None:
        return set()
    people: set[str] = {str(agent.user_id)} if agent.user_id else set()
    people |= {str(user) for user in _ids(agent.shared_user_ids)}
    groups = _ids(agent.shared_group_ids)
    orchestrated = (
        (
            await db.execute(
                select(Workspace)
                .join(Fleet, Fleet.workspace_id == Workspace.id)
                .where(Fleet.orchestrator_agent_id == parsed, Fleet.is_archived.is_(False))
            )
        )
        .scalars()
        .all()
    )
    for workspace in orchestrated:
        if workspace.user_id:
            people.add(str(workspace.user_id))
        people |= {str(user) for user in _ids(workspace.shared_user_ids)}
        groups |= _ids(workspace.shared_group_ids)
    return people | await _members(db, groups)


async def reach(db: AsyncSession, scope: InsightScope, agent_id: str | None) -> Reach:
    if scope.platform:
        return Reach(scope.label, scope.note, None)
    related = await agent_reach(db, agent_id)
    if related - {str(scope.user.id)}:
        return Reach(
            AGENT_SCOPE,
            "Yourself, and the people this agent is shared with or whose fleet workspace it orchestrates "
            "(owner, shared users, current members of shared groups). Everyone needs an administrator's agent.",
            frozenset(related | {str(scope.user.id)}),
        )
    return Reach(
        scope.without_team().label,
        "Your own default only: naming other people needs an administrator's agent, or an agent shared "
        "with them or orchestrating their fleet.",
        frozenset({str(scope.user.id)}),
    )


async def _resolve(db: AsyncSession, args: dict[str, Any], scope: InsightScope) -> list[User]:
    refs = as_list(args.get("users"))
    # Outside the administrator scope only the caller's own groups resolve.
    groups = await resolve_groups(db, args.get("group_ids"), scope)
    if len(refs) > MAX_REFERENCES:
        raise InsightRefusedError(f"Error: name at most {MAX_REFERENCES} people in one call.")
    ids = {parsed for entry in refs if (parsed := safe_uuid(entry)) is not None}
    emails = {entry.casefold() for entry in refs if safe_uuid(entry) is None}
    ids |= {uuid.UUID(member) for member in await group_member_ids(db, groups)}
    if not refs and not groups:
        ids = {scope.user.id}
    found: dict[uuid.UUID, User] = {}
    if ids:
        for user in (await db.execute(select(User).where(User.id.in_(ids)))).scalars().all():
            found[user.id] = user
    if emails:
        for user in (await db.execute(select(User).where(func.lower(User.email).in_(emails)))).scalars().all():
            found[user.id] = user
    known = {str(user.id) for user in found.values()} | {user.email.casefold() for user in found.values()}
    unknown = [entry for entry in refs if _key(entry) not in known]
    if unknown:
        raise InsightRefusedError(f"Error: no user matches {', '.join(repr(u) for u in unknown)}.")
    if len(found) > MAX_REFERENCES * 4:
        raise InsightRefusedError(f"Error: at most {MAX_REFERENCES * 4} people in one call; name fewer groups.")
    return sorted(found.values(), key=lambda user: user.email)


def _key(entry: str) -> str:
    parsed = safe_uuid(entry)
    return str(parsed) if parsed is not None else entry.casefold()


def describe(user: User, agent: Agent | None, fleet: str | None) -> dict[str, Any]:
    """One person's row: their own choice, the platform's wiring, or nothing set."""
    row: dict[str, Any] = {"user_id": str(user.id), "email": user.email, "name": user.full_name}
    source = user.default_agent_source
    if user.default_agent_id is None:
        row["default_agent"] = None
        row["status"] = "general_assistant" if source == "user" else "none"
        row["note"] = "chose the General Assistant" if source == "user" else NONE_SET
        if source == "user" and user.default_agent_set_at:
            row["set_at"] = user.default_agent_set_at.isoformat()
        return row
    if source is None:
        # Written before the source was recorded: only the platform wires the CTEM Assistant.
        source = "platform" if agent is not None and agent.slug == PLATFORM_WIRED_SLUG else "user"
    if agent is None:
        row.update(
            status="dangling",
            default_agent=None,
            note=f"points at agent {user.default_agent_id}, which no longer exists: the platform default applies",
        )
        return row
    if source == "platform":
        row.update(
            status="platform_wired",
            default_agent=None,
            note=PLATFORM_SET,
            platform_wired_agent={"id": str(agent.id), "name": agent.name},
        )
        return row
    row.update(
        status="chosen",
        default_agent={"id": str(agent.id), "name": agent.name, "enabled": bool(agent.enabled), "fleet": fleet},
        set_at=user.default_agent_set_at.isoformat() if user.default_agent_set_at else None,
    )
    if user.default_agent_set_at is None:
        row["set_at_note"] = "set before the platform recorded when"
    return row


async def get_user_default_agents(
    db: AsyncSession, scope: InsightScope, args: dict[str, Any], *, agent_id: str | None = None
) -> dict[str, Any]:
    allowed = await reach(db, scope, agent_id)
    people = await _resolve(db, args, scope)
    if allowed.user_ids is not None:
        foreign = [user.email for user in people if str(user.id) not in allowed.user_ids]
        if foreign:
            raise InsightRefusedError(
                "Error: outside your reach: "
                + ", ".join(foreign[:10])
                + (" ..." if len(foreign) > 10 else "")
                + f". {allowed.note}"
            )
    agent_ids = {user.default_agent_id for user in people if user.default_agent_id}
    agents: dict[uuid.UUID, Agent] = {}
    fleets: dict[uuid.UUID, str] = {}
    if agent_ids:
        agents = {a.id: a for a in (await db.execute(select(Agent).where(Agent.id.in_(agent_ids)))).scalars().all()}
        rows = await db.execute(
            select(FleetMember.agent_id, Fleet.name)
            .join(Fleet, Fleet.id == FleetMember.fleet_id)
            .where(FleetMember.agent_id.in_(agent_ids), FleetMember.role == "member")
        )
        fleets = {agent: name for agent, name in rows.all()}
    rows_out = [
        describe(
            user,
            agents.get(user.default_agent_id) if user.default_agent_id else None,
            fleets.get(user.default_agent_id) if user.default_agent_id else None,
        )
        for user in people
    ]
    return {
        "scope": allowed.label,
        "scope_note": allowed.note,
        "people": rows_out,
        "caveats": [
            "status chosen is the person's own choice; platform_wired and none both mean the person chose "
            "nothing (their chat opens on the platform's default). Read only: each person switches it themselves.",
        ],
    }
