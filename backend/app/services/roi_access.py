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

"""Who reads a fleet's ROI and reviews its runs (issue #9350, decision D1 of ``dev-docs/roi-outcomes.md``).

The ONE place the rule lives. Administrators read every report. Beside them,
a fleet's ROI (its scoped report, its export, its records) is read, and its
runs' records reviewed, by:

* the fleet's managers: its owner and whoever holds a ``config`` grant on it
  (``helpers.can_manage_as``, the rule its own edit routes apply; a
  company-managed fleet is managed by administrators only);
* the group managers of the fleet's workspace: the managers
  (``services/group_managers.py``) of a group the workspace is shared with.

An agent's scoped report is read by whoever can see the agent (its owner, a
user or group it is shared with, everyone for a company-managed agent: the
visibility rule of ``helpers.user_visible_filter``), so the agent Overview
shows its ROI to every reader of the agent, never only to administrators
(#9807, design standards s6). The unscoped records list stays administrators
only. Nothing here grants an edit right.

ROI everywhere (#9836, slice R1, ``dev-docs/roi-outcomes.md`` 10.2):
:func:`reader_scope` is what one person reads: an administrator the whole
organization, anyone else the agents and fleets they can see (own, shared
with them or their groups, company-managed) plus the fleets
:func:`fleet_roi_reader` opens to them. ``GET /roi?scope=mine`` reports
exactly that scope; the platform report shown to a non-administrator (when
the organization setting ``roi_org_totals_visible`` is on) folds every row
outside it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RoiScope:
    """The agents and fleets one person reads the ROI of; ``organization`` reads every record."""

    organization: bool = False
    agent_ids: frozenset[uuid.UUID] = frozenset()
    fleet_ids: frozenset[uuid.UUID] = frozenset()

    def holds_agent(self, agent_id: uuid.UUID | None) -> bool:
        return self.organization or (agent_id is not None and agent_id in self.agent_ids)

    def holds_fleet(self, fleet_id: uuid.UUID | None) -> bool:
        return self.organization or (fleet_id is not None and fleet_id in self.fleet_ids)

    def holds_record(self, agent_id: uuid.UUID | None, fleet_id: uuid.UUID | None) -> bool:
        """A record is in scope when its agent or its fleet is: counted once either way."""
        return self.holds_agent(agent_id) or self.holds_fleet(fleet_id)


#: The scope of a caller whose scope could not be read: nothing (a refusal, never a 500).
EMPTY_SCOPE = RoiScope()


def shares_a_managed_group(workspace_group_ids: object, managed_group_ids: set[str]) -> bool:
    """Whether a workspace shared with *workspace_group_ids* names a group in *managed_group_ids*."""
    if not managed_group_ids or not isinstance(workspace_group_ids, list):
        return False
    return any(str(gid) in managed_group_ids for gid in workspace_group_ids)


async def fleet_roi_reader(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    is_admin: bool,
    fleet_id: uuid.UUID | None,
) -> bool:
    """Whether *user_id* reads the ROI of fleet *fleet_id* and reviews its runs.

    ``False`` for an absent fleet or caller; never raises (a failed read
    answers ``False``, the caller's refusal stands).
    """
    if is_admin:
        return True
    if user_id is None or fleet_id is None:
        return False
    try:
        from app.api.v1.helpers import can_manage_as, get_user_group_ids
        from app.models.fleet import Fleet
        from app.models.workspace import Workspace
        from app.services.group_managers import managed_groups

        fleet = await db.get(Fleet, fleet_id)
        if fleet is None:
            return False
        if can_manage_as(user_id, False, fleet, await get_user_group_ids(db, user_id)):
            return True
        managed = {group.id for group in await managed_groups(db, user_id)}
        if not managed:
            return False
        workspace = await db.get(Workspace, fleet.workspace_id)
        return workspace is not None and shares_a_managed_group(workspace.shared_group_ids, managed)
    except Exception as exc:  # a refusal, never a 500, on a failed read
        logger.warning("ROI access of fleet %s not read: %s", fleet_id, format_exception_message(exc))
        return False


async def agent_roi_reader(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    is_admin: bool,
    agent_id: uuid.UUID | None,
) -> bool:
    """Whether *user_id* reads the scoped ROI report of agent *agent_id*: whoever sees the agent.

    ``False`` for an absent agent or caller; never raises (a failed read
    answers ``False``, the caller's refusal stands).
    """
    if is_admin:
        return True
    if user_id is None or agent_id is None:
        return False
    try:
        from sqlalchemy import select

        from app.api.v1.helpers import get_user_group_ids, user_visible_filter
        from app.models.agent import Agent

        found = await db.execute(
            select(Agent.id).where(
                Agent.id == agent_id,
                user_visible_filter(Agent, user_id, await get_user_group_ids(db, user_id)),
            )
        )
        return found.scalar_one_or_none() is not None
    except Exception as exc:  # a refusal, never a 500, on a failed read
        logger.warning("ROI access of agent %s not read: %s", agent_id, format_exception_message(exc))
        return False


async def fleet_visible(db: AsyncSession, *, user_id: uuid.UUID | None, fleet_id: uuid.UUID | None) -> bool:
    """Whether *user_id* sees fleet *fleet_id* (own, shared, company-managed): its ROI opens to them (#9836 R1).

    ``False`` for an absent fleet or caller; never raises.
    """
    if user_id is None or fleet_id is None:
        return False
    try:
        from sqlalchemy import select

        from app.api.v1.helpers import get_user_group_ids, user_visible_filter
        from app.models.fleet import Fleet

        found = await db.execute(
            select(Fleet.id).where(
                Fleet.id == fleet_id,
                user_visible_filter(Fleet, user_id, await get_user_group_ids(db, user_id)),
            )
        )
        return found.scalar_one_or_none() is not None
    except Exception as exc:  # a refusal, never a 500, on a failed read
        logger.warning("Visibility of fleet %s not read: %s", fleet_id, format_exception_message(exc))
        return False


async def reader_scope(db: AsyncSession, *, user_id: uuid.UUID | None, is_admin: bool) -> RoiScope:
    """What *user_id* reads the ROI of (#9836 R1, design 10.2).

    An administrator reads the organization. Anyone else reads the agents and
    the fleets of ``helpers.user_visible_filter`` (the library they already
    see and run) plus the fleets whose workspace is shared with a group they
    manage (the managers' rule of :func:`fleet_roi_reader`; the owner and the
    config grantees already see their fleet). Never raises: a failed read
    answers :data:`EMPTY_SCOPE`.
    """
    if is_admin:
        return RoiScope(organization=True)
    if user_id is None:
        return EMPTY_SCOPE
    try:
        from sqlalchemy import select

        from app.api.v1.helpers import get_user_group_ids, user_visible_filter
        from app.models.agent import Agent
        from app.models.fleet import Fleet
        from app.models.workspace import Workspace
        from app.services.group_managers import managed_groups

        group_ids = await get_user_group_ids(db, user_id)
        agent_ids = frozenset(
            (await db.execute(select(Agent.id).where(user_visible_filter(Agent, user_id, group_ids)))).scalars().all()
        )
        fleet_ids = set(
            (await db.execute(select(Fleet.id).where(user_visible_filter(Fleet, user_id, group_ids)))).scalars().all()
        )
        managed = {group.id for group in await managed_groups(db, user_id)}
        if managed:
            workspaces = (await db.execute(select(Workspace.id, Workspace.shared_group_ids))).all()
            managed_workspaces = [wid for wid, groups in workspaces if shares_a_managed_group(groups, managed)]
            if managed_workspaces:
                fleet_ids.update(
                    (
                        await db.execute(
                            # Every fleet of the workspace, as fleet_roi_reader opens it (one rule).
                            select(Fleet.id).where(Fleet.workspace_id.in_(managed_workspaces))
                        )
                    )
                    .scalars()
                    .all()
                )
        return RoiScope(organization=False, agent_ids=agent_ids, fleet_ids=frozenset(fleet_ids))
    except Exception as exc:  # nothing, never a 500, on a failed read
        logger.warning("ROI scope of user %s not read: %s", user_id, format_exception_message(exc))
        return EMPTY_SCOPE
