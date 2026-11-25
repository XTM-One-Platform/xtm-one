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

"""Rebuild a run's integration tools inside the turn, through the caller's own filters (#1874).

A caller (the agent chat, the assignment engine) builds its tool surface once:
the integration tools through ``build_integration_tools`` and its allow lists,
then the whole composite narrowed by the built-in scoping, the deny list and the
caller's own strips. When a tool of the run changes an integration the run is
bound to (a new OpenAPI spec, a new operation), the loop asks for the surface
again. :class:`IntegrationSurfaceRefresher` answers it with the SAME two
functions the caller built its first surface with, so a refresh never widens
past what a fresh turn of the same agent would get: no second copy of the
allow, deny or strip rules lives here.

The refresh rewrites the registries in place: the integration names the new
build no longer carries leave the composite, the new ones are registered, and
the scoped executor's allowed set is swapped for the narrowed surface.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.tools.base import CompositeToolExecutor, ScopedToolExecutor, ToolDefinition, ToolExecutor

logger = logging.getLogger(__name__)

INTEGRATION_SOURCE_PREFIX = "integration:"

RebuildIntegrations = Callable[[], Awaitable[tuple[list[ToolDefinition], ToolExecutor]]]
NarrowSurface = Callable[[list[ToolDefinition]], list[ToolDefinition]]
#: The caller's built-in build read again from the agent row: the built-in
#: definitions and their executors a fresh turn of the same agent would get (#2205).
RebuildBuiltins = Callable[[], Awaitable[list[tuple[list[ToolDefinition], ToolExecutor]]]]


async def rebuild_bound_builtin_groups(
    agent_id: str,
    user_id: str | None,
    *,
    run_id: str | None = None,
    conversation_id: str | None = None,
) -> list[tuple[list[ToolDefinition], ToolExecutor]]:
    """The bound built-in groups a fresh turn of *agent_id* for *user_id* would wire now (#2205).

    The agent row and the acting person are read again in a session of their
    own (the loop holds none), and each group goes through its own gate
    (``offers_*`` / ``*_tools_for``), so a group granted by ``update_agent`` in
    the run loads from the next step and a group the person may not run never
    does. The groups covered are the ones bound by a write of the agent's
    ``builtin_tool_ids``: the administrator groups (organization profile
    write, user administration) and the CTEM groups.
    """
    if not agent_id or not user_id:
        return []
    from app.core.database import async_session
    from app.models.agent import Agent
    from app.models.user import User
    from app.services.builtin_tool_aliases import runtime_builtin_tool_ids
    from app.services.builtin_tools import (
        build_organization_context_write_tools,
        build_user_admin_tools,
        ctem_read_tools_for,
        ctem_repair_tools_for,
        ctem_tools_for,
        offers_organization_context_write,
        offers_user_admin,
    )

    async with async_session() as db:
        agent = await db.get(Agent, agent_id)
        person = await db.get(User, user_id)
    if agent is None:
        return []
    is_admin = bool(getattr(person, "is_admin", False))
    ids = runtime_builtin_tool_ids(list(agent.builtin_tool_ids or []), getattr(agent, "builtin_allowed_tools", None))
    groups: list[tuple[list[ToolDefinition], ToolExecutor]] = []
    if offers_organization_context_write(ids, acting_user_id=user_id, acting_is_admin=is_admin):
        groups.append(
            build_organization_context_write_tools(
                user_id=user_id, caller_is_admin=is_admin, agent_name=agent.name, conversation_id=conversation_id
            )
        )
    if offers_user_admin(ids, acting_user_id=user_id, acting_is_admin=is_admin):
        groups.append(
            build_user_admin_tools(user_id=user_id, caller_is_admin=is_admin, agent_name=agent.name, run_id=run_id)
        )
    for build in (ctem_read_tools_for, ctem_tools_for, ctem_repair_tools_for):
        built = build(agent, ids, user_id=user_id)
        if built is not None:
            groups.append(built)
    return groups


def integration_tool_names(tools: list[ToolDefinition]) -> set[str]:
    """The names of the integration tools in *tools* (source ``integration:<provider>``)."""
    return {t.name for t in tools if (t.source or "").startswith(INTEGRATION_SOURCE_PREFIX)}


@dataclass(frozen=True, slots=True)
class SurfaceRefresh:
    """What one refresh changed: the narrowed surface and the names it added or dropped."""

    tools: list[ToolDefinition]
    added: tuple[str, ...]
    removed: tuple[str, ...]


@dataclass(slots=True)
class IntegrationSurfaceRefresher:
    """The caller's integration build and narrowing, replayed on the live registries.

    ``rebuild_integrations`` answers the integration definitions and their
    executor after the caller's integration allow lists (it opens its own
    database session: the loop holds none). ``narrow`` is the function the
    caller computed its offered surface with from ``composite.all_tools``.

    ``agent_id`` is the agent the run serves: an ``update_agent`` result that
    changed THAT agent's tool scope (``ToolResult.agent_tool_scope``) asks for
    a refresh too (#2205). ``rebuild_builtins``, when the caller passes it,
    answers the built-in groups its own build would wire now; the groups it
    answers that the composite does not hold yet are added, never removed,
    and the caller's narrowing still applies, so a refresh never widens past
    a fresh turn of the same agent.
    """

    composite: CompositeToolExecutor
    scoped: ScopedToolExecutor
    rebuild_integrations: RebuildIntegrations
    narrow: NarrowSurface
    agent_id: str | None = None
    rebuild_builtins: RebuildBuiltins | None = None

    def follows(self, agent_tool_scope: str | None) -> bool:
        """Whether a result naming *agent_tool_scope* changed the tools of this run's agent."""
        return bool(agent_tool_scope) and agent_tool_scope == self.agent_id

    async def refresh(self) -> SurfaceRefresh:
        before = self.narrow(self.composite.all_tools)
        old_integrations = integration_tool_names(self.composite.all_tools)
        definitions, executor = await self.rebuild_integrations()
        new_integrations = {t.name for t in definitions}
        self.composite.remove_tools(old_integrations - new_integrations)
        if definitions:
            self.composite.add_tools(definitions, executor)
        if self.rebuild_builtins is not None:
            held = {t.name for t in self.composite.all_tools}
            for builtin_defs, builtin_executor in await self.rebuild_builtins():
                fresh = [t for t in builtin_defs if t.name not in held]
                if fresh:
                    self.composite.add_tools(fresh, builtin_executor)
                    held.update(t.name for t in fresh)
        after = self.narrow(self.composite.all_tools)
        self.scoped.replace_allowed(after)
        before_names = {t.name for t in before}
        after_names = {t.name for t in after}
        refresh = SurfaceRefresh(
            tools=after,
            added=tuple(sorted(after_names - before_names)),
            removed=tuple(sorted(before_names - after_names)),
        )
        logger.info(
            "Tool surface refreshed inside the turn: %d tool(s), %d added, %d removed",
            len(after),
            len(refresh.added),
            len(refresh.removed),
        )
        return refresh
