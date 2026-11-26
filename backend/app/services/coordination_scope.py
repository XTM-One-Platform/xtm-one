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

"""The scope of an agent's coordination reads: its fleet, itself, or everything.

A work item belongs to a user, and every agent of that user used to read every
item the user can see: an orchestrator's sweep met the items of the user's
other fleets (or, through company-managed visibility, of other people's agents)
and had to filter by a hard-coded list of agent ids. The coordination tools now
narrow what an agent reads to a scope:

* ``fleet``: the items whose owner, creator or claimant is a current home
  member of the caller's fleet (the orchestrator included), plus the items
  filed in the fleet's workspace that no agent owns (a person's item filed for
  the fleet), as ``work_item.fleet_work_item_clause`` counts them. The default
  for an agent that has a home fleet.
* ``own``: the items the caller owns or created.
* ``all``: every item the user can see, today's behaviour. Explicit, and for an
  administrator's agent only (:func:`app.api.v1.helpers.agent_tool_admin_bypass`).

An agent in no fleet keeps today's read when it names no scope: everything its
user can see. Membership is read when the scope is resolved, so an item follows
the agents in the fleet now. A scope only narrows the caller's visibility; it
never widens it. Platform events (``work_item.*``) and the work-item inbox
trigger follow the item's owner, creator and holder, unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import ColumnElement, and_, or_, select

from app.core.utils import safe_uuid
from app.models.fleet import FLEET_ROLE_MEMBER, Fleet, FleetMember

SCOPE_FLEET = "fleet"
SCOPE_OWN = "own"
SCOPE_ALL = "all"
COORDINATION_SCOPES: tuple[str, ...] = (SCOPE_FLEET, SCOPE_OWN, SCOPE_ALL)

#: The ``scope`` property every coordination read tool declares.
SCOPE_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": ["", *COORDINATION_SCOPES],
    "description": (
        "Whose items to read: 'fleet' (default for an agent in a fleet: items owned, "
        "created or claimed by a member of your fleet), 'own' (items you own or created), "
        "'all' (every item your user can see; administrators only). An agent in no fleet "
        "reads everything its user can see unless it names 'own'."
    ),
}

#: The ``scope`` property of ``list_shared_state``: an entry has one owner agent.
SHARED_STATE_SCOPE_SCHEMA: dict[str, Any] = {
    "type": "string",
    "enum": ["", *COORDINATION_SCOPES],
    "description": (
        "Whose entries to read: 'fleet' (default for an agent in a fleet: entries owned by a member "
        "of your fleet, or filed in its workspace with no owner), 'own' (entries you own), 'all' "
        "(every entry your user can see; administrators only). An agent in no fleet reads everything "
        "its user can see unless it names 'own'."
    ),
}


@dataclass(frozen=True)
class CoordinationScope:
    """A resolved scope: its name, the caller, and its fleet when it has one."""

    name: str
    agent_id: uuid.UUID | None
    fleet_id: uuid.UUID | None = None
    fleet_name: str = ""
    members: tuple[uuid.UUID, ...] = ()
    workspace_id: uuid.UUID | None = None

    @property
    def narrows(self) -> bool:
        return self.name in (SCOPE_FLEET, SCOPE_OWN)

    def label(self, noun: str = "item") -> str:
        """The words the first line of a listing names the scope with.

        *noun* is what the listing reads (``item``, ``entry``): a shared-state
        entry has an owner and no creator, so ``own`` reads "you own" alone.
        """
        plural = "entries" if noun == "entry" else f"{noun}s"
        if self.name == SCOPE_FLEET:
            return f"Scope fleet: {self.fleet_name or self.fleet_id}, {len(self.members)} member(s)."
        if self.name == SCOPE_OWN:
            if noun == "entry":
                return "Scope own: entries you own."
            return f"Scope own: {plural} you own or created."
        if self.fleet_id is None:
            return f"Scope all: agent in no fleet, every {noun} your user can see."
        return f"Scope all: every {noun} your user can see."

    def work_item_clause(self) -> ColumnElement[bool] | None:
        """The SQL narrowing of ``work_items`` for this scope, or None for ``all``."""
        from app.models.work_item import WorkItem

        if self.name == SCOPE_FLEET:
            members = list(self.members)
            parts: list[ColumnElement[bool]] = [
                WorkItem.owner_agent_id.in_(members),
                WorkItem.created_by_agent_id.in_(members),
                WorkItem.claimed_by_agent_id.in_(members),
            ]
            if self.workspace_id is not None:
                parts.append(and_(WorkItem.workspace_id == self.workspace_id, WorkItem.owner_agent_id.is_(None)))
            return or_(*parts)
        if self.name == SCOPE_OWN:
            return or_(WorkItem.owner_agent_id == self.agent_id, WorkItem.created_by_agent_id == self.agent_id)
        return None

    def shared_state_clause(self) -> ColumnElement[bool] | None:
        """The SQL narrowing of ``shared_states`` for this scope, or None for ``all``.

        An entry has one owner agent and no creator or claimant column: ``fleet``
        keeps the entries a home member owns plus the unowned entries filed in
        the fleet's workspace; ``own`` keeps the caller's entries.
        """
        from app.models.shared_state import SharedState

        if self.name == SCOPE_FLEET:
            parts: list[ColumnElement[bool]] = [SharedState.owner_agent_id.in_(list(self.members))]
            if self.workspace_id is not None:
                parts.append(
                    and_(SharedState.workspace_id == self.workspace_id, SharedState.owner_agent_id.is_(None))
                )
            return or_(*parts)
        if self.name == SCOPE_OWN:
            return SharedState.owner_agent_id == self.agent_id
        return None

    def admits(self, item: Mapping[str, Any]) -> bool:
        """Whether a serialized work item is in this scope (``get`` / ``claim``)."""
        if not self.narrows:
            return True
        agents = {
            safe_uuid(str(item.get(key))) for key in ("owner_agent_id", "created_by_agent_id", "claimed_by_agent_id")
            if item.get(key)
        }
        if self.name == SCOPE_FLEET:
            if agents & set(self.members):
                return True
            filed = safe_uuid(str(item.get("workspace_id") or ""))
            return self.workspace_id is not None and filed == self.workspace_id and not item.get("owner_agent_id")
        agents.discard(safe_uuid(str(item.get("claimed_by_agent_id") or "")))
        return self.agent_id in agents

    def refusal(self) -> str:
        """Why an item outside the scope is not served."""
        if self.name == SCOPE_FLEET:
            return (
                f"not in your fleet ({self.fleet_name or self.fleet_id}): no member of it owns, "
                "created or claimed this item"
            )
        return "not yours: you neither own nor created this item"


def fleet_work_items_clause(fleet_id: uuid.UUID) -> ColumnElement[bool]:
    """The work items of a fleet as a query-time clause: the population of scope ``fleet``.

    The same rows :meth:`CoordinationScope.work_item_clause` keeps for a member
    of *fleet_id* (owned, created or claimed by a current home member, or filed
    in the fleet's workspace with no owner), with membership and workspace as
    subqueries, so the fleet Operations tab (``?fleet=``) counts exactly what
    the agents' ``list_work_items`` reads.
    """
    from app.models.work_item import WorkItem

    homes = select(FleetMember.agent_id).where(
        FleetMember.fleet_id == fleet_id,
        FleetMember.role == FLEET_ROLE_MEMBER,
        FleetMember.agent_id.isnot(None),
    )
    workspace = select(Fleet.workspace_id).where(Fleet.id == fleet_id).scalar_subquery()
    return or_(
        WorkItem.owner_agent_id.in_(homes),
        WorkItem.created_by_agent_id.in_(homes),
        WorkItem.claimed_by_agent_id.in_(homes),
        and_(WorkItem.workspace_id == workspace, WorkItem.owner_agent_id.is_(None)),
    )


def fleet_shared_states_clause(fleet_id: uuid.UUID) -> ColumnElement[bool]:
    """The shared-state entries of a fleet: the population of ``list_shared_state`` scope ``fleet``.

    Owned by a current home member, or filed in the fleet's workspace with no
    owner (:meth:`CoordinationScope.shared_state_clause`), as query-time
    subqueries for the fleet Operations tab.
    """
    from app.models.shared_state import SharedState

    homes = select(FleetMember.agent_id).where(
        FleetMember.fleet_id == fleet_id,
        FleetMember.role == FLEET_ROLE_MEMBER,
        FleetMember.agent_id.isnot(None),
    )
    workspace = select(Fleet.workspace_id).where(Fleet.id == fleet_id).scalar_subquery()
    return or_(
        SharedState.owner_agent_id.in_(homes),
        and_(SharedState.workspace_id == workspace, SharedState.owner_agent_id.is_(None)),
    )


async def resolve_coordination_scope(
    agent_id: str | None,
    user_id: str | None,
    requested: str | None,
    *,
    acting_admin: bool = False,
) -> CoordinationScope | str:
    """The scope a coordination read runs in, or the error to answer.

    *requested* empty is the default: ``fleet`` for an agent with a home fleet
    (archived fleets included: their members still read their own work), else
    ``all`` (unchanged for an agent in no fleet). ``all`` named explicitly by an
    agent with a fleet needs an administrator's agent: *user_id* is one, or
    *acting_admin* says the run acts for one (a company-managed run carries no
    person in *user_id*, #2381).
    """
    name = (requested or "").strip().lower()
    if name and name not in COORDINATION_SCOPES:
        return f"Error: scope must be one of {', '.join(COORDINATION_SCOPES)} (got '{requested}')."
    aid = safe_uuid(agent_id) if agent_id else None
    home = await read_home_fleet(aid)
    fleet_id, fleet_name, members, workspace_id = home if home is not None else (None, "", (), None)
    if not name:
        name = SCOPE_FLEET if fleet_id is not None else SCOPE_ALL
    if name == SCOPE_OWN:
        return CoordinationScope(SCOPE_OWN, aid, fleet_id, fleet_name)
    if name == SCOPE_FLEET:
        if fleet_id is None:
            return "Error: scope fleet needs an agent with a home fleet; this agent is in none (use own or omit scope)."
        return CoordinationScope(SCOPE_FLEET, aid, fleet_id, fleet_name, tuple(members), workspace_id)
    if fleet_id is not None and not acting_admin and not await user_is_admin(user_id):
        return "Error: scope all is for an administrator's agent; use fleet (the default) or own."
    return CoordinationScope(SCOPE_ALL, aid, fleet_id, fleet_name)


async def read_home_fleet(
    agent_id: uuid.UUID | None,
) -> tuple[uuid.UUID, str, tuple[uuid.UUID, ...], uuid.UUID | None] | None:
    """``(fleet id, name, home member ids, workspace id)`` of *agent_id*'s home fleet, or None."""
    from app.core.database import async_session

    if agent_id is None:
        return None
    async with async_session() as db:
        home = (
            await db.execute(
                select(Fleet.id, Fleet.name, Fleet.workspace_id)
                .join(FleetMember, FleetMember.fleet_id == Fleet.id)
                .where(FleetMember.agent_id == agent_id, FleetMember.role == FLEET_ROLE_MEMBER)
                .limit(1)
            )
        ).first()
        if home is None:
            return None
        members = (
            await db.execute(
                select(FleetMember.agent_id).where(
                    FleetMember.fleet_id == home[0],
                    FleetMember.role == FLEET_ROLE_MEMBER,
                    FleetMember.agent_id.isnot(None),
                )
            )
        ).scalars().all()
    return home[0], home[1], tuple(m for m in members if m is not None), home[2]


async def user_is_admin(user_id: str | None) -> bool:
    """Whether an agent acting for *user_id* takes the administrator's bypass."""
    from app.api.v1.helpers import agent_tool_admin_bypass
    from app.core.database import async_session
    from app.models.user import User

    uid = safe_uuid(user_id) if user_id else None
    if uid is None:
        return False
    async with async_session() as db:
        user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    return agent_tool_admin_bypass(user)
