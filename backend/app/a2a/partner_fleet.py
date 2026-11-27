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

"""The fleet reads a partner task may make, behind its exposure's opt-in (#2202).

Inside a partner task the platform builder acts as an anonymous caller
(``partner_scope``) and wires no fleet tool. An exposure that carries
``partner_fleet_reads`` lends three reads back, for ONE fleet: the home fleet
of the exposed agent.

- ``list_fleets`` answers that fleet alone, with the build the platform runs.
- ``get_fleet`` and ``get_fleet_health`` answer it, and any other id exactly
  like an unknown fleet. ``get_fleet_health`` never applies a fix.

The opt-in and the home fleet are read at every call, never cached on the
task: switching the exposure off, revoking it or moving the agent narrows the
next call of a task already running. Off (the default) every call refuses.

The wider opt-in of #2638 (``partner_owner_reads``, ``a2a.partner_owner_reads``)
lends the same three reads with the owner's read scope: every fleet the
run-as identity reads, each call audited with the partner task named. It wins
over the home-fleet scope when both are on; writes stay unregistered either way.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.a2a.partner_scope import PartnerPrincipal, current_partner

if TYPE_CHECKING:
    from app.tools.base import CallableToolExecutor, ToolDefinition

#: The fleet reads a partner task gets behind the opt-in; never a write.
PARTNER_FLEET_TOOL_NAMES: tuple[str, ...] = ("list_fleets", "get_fleet", "get_fleet_health")

OPT_IN_OFF = (
    "Fleet reads are not enabled for this partner: the exposure's owner switches on 'partner fleet reads' to lend them."
)
NO_HOME_FLEET = "The agent this task reached is in no fleet: there is no fleet to read."


async def partner_home_fleet_id(partner: PartnerPrincipal) -> uuid.UUID | None:
    """The home fleet of the partner's exposed agent, ``None`` unless its live exposure opted in."""
    from app.core import database
    from app.models.a2a import A2AExposure
    from app.models.fleet import FLEET_ROLE_MEMBER, FleetMember

    query = (
        select(FleetMember.fleet_id)
        .join(A2AExposure, A2AExposure.agent_id == FleetMember.agent_id)
        .where(
            A2AExposure.id == partner.exposure_id,
            A2AExposure.partner_fleet_reads.is_(True),
            A2AExposure.revoked_at.is_(None),
            FleetMember.role == FLEET_ROLE_MEMBER,
        )
        .limit(1)
    )
    async with database.async_session() as db:
        return (await db.execute(query)).scalar_one_or_none()


async def partner_opted_in(partner: PartnerPrincipal) -> bool:
    """Whether the partner's live exposure carries ``partner_fleet_reads``."""
    from app.core import database
    from app.models.a2a import A2AExposure

    query = select(A2AExposure.id).where(
        A2AExposure.id == partner.exposure_id,
        A2AExposure.partner_fleet_reads.is_(True),
        A2AExposure.revoked_at.is_(None),
    )
    async with database.async_session() as db:
        return (await db.execute(query)).scalar_one_or_none() is not None


def _running_build_line() -> str:
    from app.services.platform_builds import running_build

    version, git_sha = running_build()
    return f"Running build: {version}" + (f" (commit {git_sha[:12]})" if git_sha else "") + "."


def _same_fleet(value: object, home: uuid.UUID) -> bool:
    try:
        return uuid.UUID(str(value or "").strip()) == home
    except ValueError:
        return False


def scope_fleet_tools_to_partner(
    definitions: list[ToolDefinition], executor: CallableToolExecutor
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Keep the three fleet reads of *executor* and gate each by the partner's home fleet.

    *executor* is the ``fleets`` suite built for the task's run-as identity;
    every other tool of it is unregistered, so no write is reachable by name.
    """
    from app.tools.base import ToolResult

    definitions = [d for d in definitions if d.name in PARTNER_FLEET_TOOL_NAMES]
    for name in list(executor.tool_names):
        if name not in PARTNER_FLEET_TOOL_NAMES:
            executor.unregister(name)
    list_fleets = executor.handler("list_fleets")
    get_fleet = executor.handler("get_fleet")
    get_health = executor.handler("get_fleet_health")

    async def _owner(tool: str, target: str = "") -> bool:
        """Whether the #2638 opt-in lends this call the owner's scope; audits it when it does."""
        from app.a2a.partner_owner_reads import audit_partner_read, partner_owner_id

        partner = current_partner()
        if partner is None:
            return False
        owner = await partner_owner_id(partner)
        if owner is None:
            return False
        await audit_partner_read(partner, owner, tool, target)
        return True

    async def _home() -> uuid.UUID | str:
        partner = current_partner()
        if partner is None or not await partner_opted_in(partner):
            return OPT_IN_OFF
        home = await partner_home_fleet_id(partner)
        return home if home is not None else NO_HOME_FLEET

    def _unknown(fleet_id: object) -> str:
        # Same words as the suite's own unknown fleet: another fleet is never confirmed.
        return f"Fleet `{fleet_id}` not found or you don't have access."

    async def _list_fleets(include_archived: object = False) -> Any:
        if list_fleets is not None and await _owner("list_fleets"):
            from app.a2a.partner_owner_reads import running_build_line

            text = await list_fleets(include_archived=include_archived)
            body = text.content if isinstance(text, ToolResult) else str(text)
            return "\n".join([body, running_build_line()])
        home = await _home()
        if not isinstance(home, uuid.UUID):
            return home
        if get_fleet is None:
            return NO_HOME_FLEET
        text = await get_fleet(fleet_id=str(home))
        body = text.content if isinstance(text, ToolResult) else str(text)
        return "\n".join(["1 fleet readable from this task:", body, _running_build_line()])

    async def _get_fleet(fleet_id: str) -> Any:
        if get_fleet is not None and await _owner("get_fleet", str(fleet_id)):
            return await get_fleet(fleet_id=fleet_id)
        home = await _home()
        if not isinstance(home, uuid.UUID):
            return home
        if get_fleet is None or not _same_fleet(fleet_id, home):
            return _unknown(fleet_id)
        return await get_fleet(fleet_id=str(home))

    async def _get_fleet_health(fleet_id: str, apply_safe_fixes: bool = False) -> Any:
        if get_health is not None and await _owner("get_fleet_health", str(fleet_id)):
            # A read: a partner never applies a fix, whatever it asks.
            return await get_health(fleet_id=fleet_id, apply_safe_fixes=False)
        home = await _home()
        if not isinstance(home, uuid.UUID):
            return home
        if get_health is None or not _same_fleet(fleet_id, home):
            return _unknown(fleet_id)
        # A read: a partner never applies a fix, whatever it asks.
        return await get_health(fleet_id=str(home), apply_safe_fixes=False)

    executor.register("list_fleets", _list_fleets)
    executor.register("get_fleet", _get_fleet)
    executor.register("get_fleet_health", _get_fleet_health)
    return definitions, executor
