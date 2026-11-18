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

"""What a Platform Health run may not do to production (#8255, PRD 6.6).

The only path to production is an approved proposal, so a Platform Health run gets
neither ``agent_builder``'s writes nor the flow builder's production writes.
Enforced twice, never by prompt: where the tools are built (the definitions are
withheld) and in the executor (a call that reaches the handler by name - a
replayed transcript, a set built before the run's scope opened - is refused on
the run's identity at call time).
"""

from __future__ import annotations

import functools
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from app.services.health.sandbox.side_effects import tool_call_effect, vouched_source
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

BUILDER_REFUSAL = (
    "Refused: a Platform Health agent does not change platform configuration directly. "
    "Experiment on a sandbox copy and file an improvement proposal."
)
PRODUCTION_FLOW_REFUSAL = (
    "Refused: a Platform Health agent changes, runs or activates only sandbox flows. "
    "Make a sandbox copy of this flow and file an improvement proposal."
)

#: The flow builder's tools that act on an existing flow, by the flow id they take.
FLOW_PRODUCTION_WRITES = frozenset({"update_flow_graph", "activate_flow", "deactivate_flow", "run_flow"})
#: The flow builder's writes of shared configuration no flow scopes.
FLOW_SHARED_WRITES = frozenset({"create_flow_event_type"})


def in_health_run() -> bool:
    from app.services.health.identity import in_health_run as _in_run

    return _in_run()


def _refused(message: str) -> ToolResult:
    return ToolResult(tool_call_id="", content=message, is_error=True, refused=True)


def _refused_in_health_run(handler: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(handler)
    async def _checked(*args: Any, **kwargs: Any) -> Any:
        if in_health_run():
            return _refused(BUILDER_REFUSAL)
        return await handler(*args, **kwargs)

    return _checked


def guard_agent_builder(definitions: list[ToolDefinition], executor: CallableToolExecutor) -> list[ToolDefinition]:
    """``agent_builder`` for a Platform Health run: its reads only.

    Every write (the shared classifier on the name; an unknown verb is a
    write) is withheld from a set built in a Platform Health run, and refused at
    call time in a Platform Health run whatever set it came from.
    """
    writes = [
        d.name
        for d in definitions
        if tool_call_effect(d.name, base_name=d.base_name, vouched=vouched_source(d.source)) == "write"
    ]
    for name in writes:
        executor.wrap_handler(name, _refused_in_health_run)
    if not in_health_run():
        return definitions
    withheld = set(writes)
    return [d for d in definitions if d.name not in withheld]


async def production_flow_refusal(db: Any, flow_id: Any) -> str | None:
    """Why a Platform Health run may not act on *flow_id* (``None`` outside one, or
    for a sandbox flow)."""
    if not in_health_run():
        return None
    from sqlalchemy import select

    from app.models.flow import Flow

    try:
        parsed = uuid.UUID(str(flow_id))
    except (TypeError, ValueError):
        return PRODUCTION_FLOW_REFUSAL
    if await db.scalar(select(Flow.is_sandbox).where(Flow.id == parsed)) is True:
        return None
    return PRODUCTION_FLOW_REFUSAL


def guard_flow_builder(db: Any, executor: CallableToolExecutor) -> None:
    """Refuse the flow builder's production writes in a Platform Health run, at call
    time, on the flow's row (``Flow.is_sandbox``)."""

    def _guard(handler: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @functools.wraps(handler)
        async def _checked(*args: Any, **kwargs: Any) -> Any:
            flow_id = kwargs.get("flow_id", args[0] if args else None)
            refusal = await production_flow_refusal(db, flow_id)
            if refusal is not None:
                return _refused(refusal)
            return await handler(*args, **kwargs)

        return _checked

    for name in FLOW_PRODUCTION_WRITES:
        if executor.handler(name) is not None:
            executor.wrap_handler(name, _guard)
    for name in FLOW_SHARED_WRITES:
        if executor.handler(name) is not None:
            executor.wrap_handler(name, _refused_in_health_run)
