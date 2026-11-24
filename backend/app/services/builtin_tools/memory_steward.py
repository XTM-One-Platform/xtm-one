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

"""Memory Steward tools (Platform Health, #8258).

Platform Health's Memory Steward works through the memory lanes it may read
and consolidates each one (``services/health/memory_steward.py``). The
tools return statuses and counts, never memory content: the pass reads the
lane itself, and only counts go to the digest.

One access rule: the caller is Platform Health, a run of one of its fleet
agents while Platform Health is available (``identity.current_health_principal``). Nobody
else, an administrator included, may run or read a pass. The platform MCP
server serves logins, which Platform Health never is, so it does not offer them.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

from app.core.database import async_session
from app.core.utils import format_exception_message
from app.models.agent import Agent
from app.models.user import User
from app.services.health import identity, memory_history
from app.services.health import memory_steward as steward
from app.services.health import memory_steward_deps as deps
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

from ._shared import logger

#: Its functions belong to Platform Health's one internal group (#1943).
MEMORY_STEWARD_SUITE_ID = "health_specialists"
_SOURCE = f"builtin:{MEMORY_STEWARD_SUITE_ID}"
_DEFAULT_LANES = 20
_MAX_LANES = 100
_DEFAULT_SINCE_HOURS = 24
_MAX_SINCE_HOURS = 24 * 31

_REFUSAL = "Error: the memory tools are reserved to Platform Health."


def _caller_is_health_principal() -> bool:
    """Whether the running work acts as Platform Health: a run of a fleet agent,
    flag on (``identity.current_health_principal`` re-checks it). A login, an
    administrator included, never is."""
    return identity.current_health_principal() is not None


def _refused(message: str = _REFUSAL) -> ToolResult:
    return ToolResult(tool_call_id="", content=message, is_error=True, refused=True)


def _uuid(value: object) -> uuid.UUID | None:
    if value is None:
        return None
    try:
        return uuid.UUID(str(value).strip())
    except (TypeError, ValueError):
        return None




def _bounded(value: object, *, default: int, maximum: int) -> int:
    raw = value if isinstance(value, int) and not isinstance(value, bool) else default
    return max(1, min(raw, maximum))


def build_memory_steward_tools(run_id: str | None = None) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The group. Each call checks the running work is Platform Health's: the
    run's user plays no part, Platform Health being no user. *run_id* is the
    assignment run the tools serve: its Platform Health cycle is charged."""
    async def _list_memory_lanes(agent_id: str | None = None, limit: int = _DEFAULT_LANES) -> str | ToolResult:
        agent = None
        if agent_id:
            agent = _uuid(agent_id)
            if agent is None:
                return "Error: agent_id must be a UUID."
        try:
            async with async_session() as db:
                if not _caller_is_health_principal():
                    return _refused()
                lanes = await steward.list_eligible_lanes(
                    db, agent_id=agent, limit=_bounded(limit, default=_DEFAULT_LANES, maximum=_MAX_LANES)
                )
        except Exception as e:
            logger.error("list_memory_lanes failed: %s", format_exception_message(e), exc_info=True)
            return "Error: failed to list memory lanes."
        return json.dumps(
            {
                "count": len(lanes),
                "lanes": [
                    {
                        "agent_id": str(lane.agent_id),
                        "user_id": str(lane.user_id) if lane.user_id else None,
                        "active_entries": lane.active_entries,
                        "last_pass_at": lane.checked_at.isoformat() if lane.checked_at else None,
                    }
                    for lane in lanes
                ],
            }
        )

    async def _consolidate_memory_lane(agent_id: str, user_id: str | None = None) -> str | ToolResult:
        agent = _uuid(agent_id)
        if agent is None:
            return "Error: agent_id must be a UUID."
        lane_user = None
        if user_id:
            lane_user = _uuid(user_id)
            if lane_user is None:
                return "Error: user_id must be a UUID."
        try:
            async with async_session() as db:
                if not _caller_is_health_principal():
                    return _refused()
                if await db.get(Agent, agent) is None or (
                    lane_user is not None and await db.get(User, lane_user) is None
                ):
                    return "Error: memory lane not found."
                result = await steward.consolidate_lane(
                    db, memory_history.Lane(agent, lane_user), cycle_id=await deps.run_cycle_id(db, _uuid(run_id))
                )
                await db.commit()
        except Exception as e:
            logger.error("consolidate_memory_lane failed: %s", format_exception_message(e), exc_info=True)
            return "Error: the consolidation pass failed; nothing was reported as applied."
        payload = {"status": result.status, "counts": result.counts}
        if result.status == "refused":
            # Consent and budget are stated; their details stay in the logs.
            message = {
                "consent": "Error: this memory lane may not be read by Platform Health.",
                "budget": "Error: the Platform Health budget is spent; no pass was run.",
            }.get(result.reason or "", _REFUSAL)
            return _refused(message)
        if result.status == "unchanged":
            return ToolResult(
                tool_call_id="",
                content="No change: this lane is unchanged since its last pass.",
                no_op=True,
                structured=payload,
            )
        return json.dumps(payload)

    async def _list_memory_consolidations(
        since_hours: int = _DEFAULT_SINCE_HOURS, agent_id: str | None = None
    ) -> str | ToolResult:
        hours = _bounded(since_hours, default=_DEFAULT_SINCE_HOURS, maximum=_MAX_SINCE_HOURS)
        agent = None
        if agent_id:
            agent = _uuid(agent_id)
            if agent is None:
                return "Error: agent_id must be a UUID."
        since = datetime.now(UTC) - timedelta(hours=hours)
        try:
            async with async_session() as db:
                if not _caller_is_health_principal():
                    return _refused()
                counts = await memory_history.consolidation_counts(db, since=since, agent_id=agent)
        except Exception as e:
            logger.error("list_memory_consolidations failed: %s", format_exception_message(e), exc_info=True)
            return "Error: failed to count memory consolidations."
        return json.dumps({"since": since.isoformat(), "counts": counts})

    definitions = [
        ToolDefinition(
            name="list_memory_lanes",
            source=_SOURCE,
            description=(
                "List the memory lanes Platform Health may consolidate: one per agent (its own memory) and "
                "one per user of an agent (that user's memory with it), only for agents opted in and users "
                "who did not opt out. Lanes never passed come first. Counts only, never memory content."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "Optional: only this agent's lanes."},
                    "limit": {
                        "type": "integer",
                        "default": _DEFAULT_LANES,
                        "description": f"How many lanes to return (at most {_MAX_LANES}).",
                    },
                },
            },
        ),
        ToolDefinition(
            name="consolidate_memory_lane",
            source=_SOURCE,
            description=(
                "Run one consolidation pass over one memory lane: merge duplicates, settle contradictions "
                "(the newest wins unless the older is confirmed more often, then it is flagged), demote "
                "time-bound entries, promote recurring observations to durable facts. Applied at once and "
                "reversible by the lane's owner; a lane unchanged since its last pass is left alone. "
                "Returns the status and the count per operation, never memory content."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string", "description": "The agent whose memory lane to consolidate."},
                    "user_id": {
                        "type": "string",
                        "description": "The user whose lane with the agent to consolidate; omit for the agent's own lane.",
                    },
                },
                "required": ["agent_id"],
            },
        ),
        ToolDefinition(
            name="list_memory_consolidations",
            source=_SOURCE,
            description=(
                "Count the memory consolidations applied over a recent window, per operation (merge, "
                "promote, demote, remove, flag), for the digest. Counts only, never memory content."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "since_hours": {
                        "type": "integer",
                        "default": _DEFAULT_SINCE_HOURS,
                        "description": f"The window, in hours back from now (at most {_MAX_SINCE_HOURS}).",
                    },
                    "agent_id": {"type": "string", "description": "Optional: only this agent's lanes."},
                },
            },
        ),
    ]
    handlers = {
        "list_memory_lanes": _list_memory_lanes,
        "consolidate_memory_lane": _consolidate_memory_lane,
        "list_memory_consolidations": _list_memory_consolidations,
    }
    executor = CallableToolExecutor()
    for definition in definitions:
        executor.register(definition.name, handlers[definition.name])
    return definitions, executor


__all__ = ["MEMORY_STEWARD_SUITE_ID", "build_memory_steward_tools"]
