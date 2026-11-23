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

"""Coordination tools of per-object timers: ``arm_timer``, ``cancel_timer``, ``list_timers``.

A run arms a timer for a date; the platform raises ``timer.due`` then, to the
``platform_event`` assignments whose filter matches. The rules live in
``services/platform_timers.py``. No model call at arm time, no approval: the
tools write only the calling agent's own timers for the person it runs for.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.core.utils import format_exception_message
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

ARM_TOOL = "arm_timer"
CANCEL_TOOL = "cancel_timer"
LIST_TOOL = "list_timers"
TIMER_TOOL_NAMES: frozenset[str] = frozenset({ARM_TOOL, CANCEL_TOOL, LIST_TOOL})
_SOURCE = "builtin:coordination"


def _line(timer: dict) -> str:
    line = f"- `{timer['correlation_key']}` due {timer['due_at']} - {timer['state']} (id `{timer['id']}`)"
    unless = timer.get("unless_work_item_id") or timer.get("unless_shared_state_key")
    if unless:
        line += f" unless {unless}"
    if timer.get("outcome") and timer["state"] != "armed":
        line += f"\n  {timer['outcome']}"
    return line


def build_timer_tools(
    user_id: str,
    agent_id: str,
    *,
    run_id: str | uuid.UUID | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The three timer tools of *agent_id* acting for *user_id*; *run_id* is recorded on each arm."""
    from app.services import platform_timers as pt

    async def _arm(
        correlation_key: str,
        due_at: str,
        payload: Any = None,
        unless_work_item_id: str = "",
        unless_shared_state_key: str = "",
    ) -> str:
        try:
            timer, replaced = await pt.arm_timer(
                user_id=user_id,
                agent_id=agent_id,
                correlation_key=correlation_key,
                due_at=due_at,
                payload=payload,
                unless_work_item_id=unless_work_item_id or None,
                unless_shared_state_key=unless_shared_state_key or None,
                run_id=run_id,
            )
        except pt.TimerError as exc:
            return f"Error: {exc} Nothing was armed."
        except Exception as exc:
            logger.error("arm_timer failed: %s", format_exception_message(exc), exc_info=True)
            return "Error: the timer could not be armed. Nothing was armed; try again."
        head = (
            f"Re-armed timer `{timer['correlation_key']}` for {timer['due_at']} (id `{timer['id']}`); "
            f"it replaces the one due {replaced['due_at']}."
            if replaced
            else f"Armed timer `{timer['correlation_key']}` for {timer['due_at']} (id `{timer['id']}`)."
        )
        text = (
            f"{head} At that time the platform raises timer.due once; a run of yours starts only if one "
            "of your own platform_event assignments on timer.due takes the key (e.g. correlation_key "
            "starts_with the prefix)"
            + (", unless its condition is met first." if (unless_work_item_id or unless_shared_state_key) else ".")
        )
        # Armed either way: a key no listener takes is warned about, never refused.
        warning = await pt.unheard_warning(timer, user_id=user_id)
        return f"{text}\n{warning}" if warning else text

    async def _cancel(correlation_key: str = "", timer_id: str = "") -> str:
        try:
            timer = await pt.cancel_timer(
                user_id=user_id, agent_id=agent_id, correlation_key=correlation_key or None, timer_id=timer_id or None
            )
        except pt.TimerError as exc:
            return f"Error: {exc}"
        if timer is None:
            named = timer_id or correlation_key
            return f"No armed timer `{named}` of this agent for this person: nothing to cancel (list_timers lists them)."
        return f"Cancelled timer `{timer['correlation_key']}` (was due {timer['due_at']}): it will not fire."

    async def _list(state: str = "armed", key_prefix: str = "", limit: int = 50) -> str | ToolResult:
        try:
            timers = await pt.list_timers(
                user_id=user_id, agent_id=agent_id, state=state or "armed", key_prefix=key_prefix or None, limit=limit
            )
        except pt.TimerError as exc:
            return f"Error: {exc}"
        scope = state or "armed"
        label = "" if scope == "all" else f"{scope} "
        # ``structured`` carries the list itself, so an assignment's gate check
        # (activation_check) can test it: an empty list is empty (#10721).
        if not timers:
            named = f" starting with `{key_prefix}`" if key_prefix else ""
            return ToolResult(tool_call_id="", content=f"No timers on this agent ({scope}){named}.", structured=[])
        content = "\n".join([f"{len(timers)} {label}timer(s):", *(_line(t) for t in timers)])
        return ToolResult(tool_call_id="", content=content, structured=list(timers))

    executor = CallableToolExecutor()
    executor.register(ARM_TOOL, _arm)
    executor.register(CANCEL_TOOL, _cancel)
    executor.register(LIST_TOOL, _list)

    definitions = [
        ToolDefinition(
            name=ARM_TOOL,
            description=(
                "Raise timer.due (correlation_key, payload) once at due_at (ISO 8601, offset). "
                "Same key replaces; unless_* item done or state resolved skips."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "correlation_key": {"type": "string"},
                    "due_at": {"type": "string"},
                    "payload": {"type": "object"},
                    "unless_work_item_id": {"type": "string"},
                    "unless_shared_state_key": {"type": "string"},
                },
                "required": ["correlation_key", "due_at"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name=CANCEL_TOOL,
            description="Cancel an armed timer.",
            input_schema={
                "type": "object",
                "properties": {"correlation_key": {"type": "string"}, "timer_id": {"type": "string"}},
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name=LIST_TOOL,
            description="List this agent's timers.",
            input_schema={
                "type": "object",
                "properties": {
                    "state": {"type": "string", "enum": list(pt.LIST_STATES)},
                    "key_prefix": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
    ]
    return definitions, executor


__all__ = ["ARM_TOOL", "CANCEL_TOOL", "LIST_TOOL", "TIMER_TOOL_NAMES", "build_timer_tools"]
