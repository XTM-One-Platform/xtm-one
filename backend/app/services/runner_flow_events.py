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

"""Runner happenings on the flow event bus.

Runners already have their own transport (durable ``RunnerCommand`` rows +
Redis wake channels — see ``runner_transport.py``); this module does NOT
replace it. It mirrors the delegation-completion pattern instead: at the
discrete chokepoints of the runner lifecycle, emit a typed ``FlowEvent`` so
flows can REACT — an `On event` trigger restarts a collection when a machine
comes back online, a `Wait for event` joins on a command another flow started.

Three builtin, company-owned event types are seeded at startup (create-if-
absent by name, exactly like the example event types):

* ``Runner command completed`` — every terminal command result
  (completed/failed/cancelled/timeout), global scope.
* ``Runner online`` — a machine marked offline heartbeated again, or a machine
  that was not online enrolled (a re-enrollment of an online machine is not).
* ``Runner offline`` — a machine stopped heartbeating for
  ``runner_offline_after_seconds``, over any transport, detected by the
  scheduler's presence sweep; or a machine was revoked while online. A dropped
  WebSocket alone does not fire it: every deploy drops them all, and the runner
  is back within seconds. The two always alternate - see
  ``runner_transport.touch_runner``.

Every emitter here is fail-open: the runner transport must never break
because the flow bus had a bad day, so callers wrap in try/except and a
missing event type is a silent no-op.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import select

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.flow_event import FlowEventType
    from app.models.runner import Runner, RunnerCommand

logger = logging.getLogger(__name__)

RUNNER_EVENT_COMMAND_COMPLETED = "Runner command completed"
RUNNER_EVENT_ONLINE = "Runner online"
RUNNER_EVENT_OFFLINE = "Runner offline"

# Field declarations follow the FlowEventType shape ({name, type, required}).
RUNNER_EVENT_TYPE_SPECS: dict[str, dict] = {
    RUNNER_EVENT_COMMAND_COMPLETED: {
        "name": RUNNER_EVENT_COMMAND_COMPLETED,
        "fields": [
            {"name": "command_id", "type": "string", "required": True},
            {"name": "runner_id", "type": "string", "required": True},
            {"name": "runner_name", "type": "string", "required": False},
            {"name": "kind", "type": "string", "required": False},
            {"name": "status", "type": "string", "required": True},
            {"name": "output", "type": "string", "required": False},
            {"name": "error", "type": "string", "required": False},
        ],
    },
    RUNNER_EVENT_ONLINE: {
        "name": RUNNER_EVENT_ONLINE,
        "fields": [
            {"name": "runner_id", "type": "string", "required": True},
            {"name": "runner_name", "type": "string", "required": False},
        ],
    },
    RUNNER_EVENT_OFFLINE: {
        "name": RUNNER_EVENT_OFFLINE,
        "fields": [
            {"name": "runner_id", "type": "string", "required": True},
            {"name": "runner_name", "type": "string", "required": False},
        ],
    },
}


async def ensure_runner_event_types(db: AsyncSession) -> None:
    """Create-if-absent the builtin runner event types (company-owned)."""
    from app.models.flow_event import FlowEventType

    for spec in RUNNER_EVENT_TYPE_SPECS.values():
        existing = await db.scalar(
            select(FlowEventType.id).where(
                FlowEventType.name == spec["name"],
                FlowEventType.user_id.is_(None),
            )
        )
        if existing is not None:
            continue
        db.add(
            FlowEventType(
                user_id=None,
                name=spec["name"],
                description="Builtin runner event — emitted by the platform.",
                fields=spec["fields"],
                is_company_managed=True,
            )
        )
    await db.flush()


async def _load_type(db: AsyncSession, name: str) -> FlowEventType | None:
    from app.models.flow_event import FlowEventType

    return (
        await db.execute(
            select(FlowEventType).where(
                FlowEventType.name == name, FlowEventType.user_id.is_(None)
            )
        )
    ).scalar_one_or_none()


def _summarize_output(result: dict | None) -> str:
    """Small, text-first view of a structured command result.

    Event payload fields are strings; the full structured result stays on
    the ``RunnerCommand`` row (and on the waiting node's outputs), so this
    only needs to be a useful preview for listener flows.
    """
    if not result:
        return ""
    for key in ("output", "stdout", "text", "content"):
        value = result.get(key)
        if isinstance(value, str) and value:
            return value[:2000]
    return str(result)[:2000]


async def emit_runner_command_completed(
    db: AsyncSession, command: RunnerCommand, runner: Runner | None
) -> None:
    """Emit the terminal-result event (global scope, attributed to the
    dispatching user). The caller owns the transaction and the orchestration
    wake, mirroring ``emit_flow_event``'s contract."""
    event_type = await _load_type(db, RUNNER_EVENT_COMMAND_COMPLETED)
    if event_type is None:
        return
    from app.services.flow_events import (
        EVENT_SCOPE_GLOBAL,
        coerce_event_fields,
        emit_flow_event,
    )

    # The type is admin-editable: coerce against its CURRENT declaration so
    # listeners never receive fields the signature does not promise.
    payload, errors = coerce_event_fields(
        list(event_type.fields or []),
        {
            "command_id": str(command.id),
            "runner_id": str(command.runner_id),
            "runner_name": runner.name if runner is not None else "",
            "kind": command.kind,
            "status": command.status,
            "output": _summarize_output(command.result),
            "error": command.error or "",
        },
    )
    if errors:
        logger.warning(
            "Runner command event skipped — payload no longer matches the "
            "edited '%s' declaration: %s",
            event_type.name,
            errors,
        )
        return
    await emit_flow_event(
        db,
        event_type=event_type,
        user_id=command.user_id,
        flow_id=None,
        run_id=None,
        scope=EVENT_SCOPE_GLOBAL,
        payload=payload,
        chain_depth=0,
    )


async def emit_runner_presence(
    db: AsyncSession, runner_id: uuid.UUID, *, online: bool
) -> None:
    """Emit ``Runner online`` / ``Runner offline`` (global scope).

    Presence events are attributed to the runner's owner. Ownerless
    (company-managed) runners emit platform events: every parked
    `Wait for event` can hear them, while listener-run dispatch skips
    flows with no attributable user (a user-less run would only fail).
    """
    from app.models.runner import Runner

    runner = await db.get(Runner, runner_id)
    if runner is None:
        return
    event_type = await _load_type(
        db, RUNNER_EVENT_ONLINE if online else RUNNER_EVENT_OFFLINE
    )
    if event_type is None:
        return
    from app.services.flow_events import (
        EVENT_SCOPE_GLOBAL,
        coerce_event_fields,
        emit_flow_event,
    )

    payload, errors = coerce_event_fields(
        list(event_type.fields or []),
        {"runner_id": str(runner.id), "runner_name": runner.name},
    )
    if errors:
        logger.warning(
            "Runner presence event skipped — payload no longer matches the "
            "edited '%s' declaration: %s",
            event_type.name,
            errors,
        )
        return
    await emit_flow_event(
        db,
        event_type=event_type,
        user_id=runner.user_id,
        flow_id=None,
        run_id=None,
        scope=EVENT_SCOPE_GLOBAL,
        payload=payload,
        chain_depth=0,
    )
