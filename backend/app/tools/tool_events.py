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

"""Progress a tool handler reports from INSIDE its own execution.

The agentic loop tells its consumer (the web chat stream, the platform SSE
proxy, the assignment engine) what happens AROUND a tool call - ``tool_start``
before the batch, ``tool_heartbeat`` while it runs, ``tool_done`` once the
handler has returned. Nothing crosses that boundary from within: a handler is
a plain callable with no handle on the loop's ``on_event``.

That gap costs real time when a tool commits something the user is looking at
and then keeps working. ``edit_presentation`` / ``generate_presentation``
commit the new revision, then render the deck in a headless browser for the
post-save layout check (bounded by ``LAYOUT_CHECK_TIMEOUT_SECS``, queued
behind the export render slots) BEFORE returning - and the stage in the
Presentation Studio refreshed on ``tool_done``, so the committed revision sat
in the database, invisible, for the whole render.

This module is the one channel for a tool to speak mid-execution. The loop
binds a sink per tool call (a ``ContextVar``: tasks copy the context at
creation, so a parallel batch keeps each call's events on its own call - the
same mechanism the loop already uses to scope the Langfuse tool span), and the
handler calls :func:`emit_tool_event`. The loop forwards the payload as a
``{"status": "tool_progress", "tool": <name>, "tool_call_id": <id>, ...}``
frame on the very ``on_event`` every other status frame rides.

Contract:

* :func:`emit_tool_event` NEVER raises and is a no-op outside a bound tool
  call (a handler under test, a REST route reusing a tool function): a
  progress signal is never worth failing the work it reports on.
* The binding is CLOSED when the loop's ``finally`` resets it, not merely
  unbound: a task the handler spawned inherits the binding through its copied
  context and may outlive the call, and its late report would otherwise reach
  the consumer tagged with a call that already finished. After the close,
  every emit through that binding is a silent no-op.
* The payload is made JSON-safe here, once, because the consumers serialise it
  in different places: the web chat stream serialises inside ``_emit`` (a bad
  value there is caught and logged), the platform SSE proxy serialises in the
  response generator (a bad value there ends the stream). A ``UUID`` or
  ``datetime`` becomes its string form; a payload that still cannot be
  serialised, or is larger than :data:`MAX_TOOL_EVENT_PAYLOAD_CHARS`, is
  dropped with a warning naming the tool and the event. A progress frame is a
  pointer to state the consumer refetches (an id, a revision), not the state
  itself.
* The payload's ``event`` key says WHAT happened (:data:`PRESENTATION_SAVED_EVENT`
  carries ``presentation_id`` + ``revision``). Consumers key on it, never on
  the tool name, so an aliased or reordered model-supplied name (resolved by
  the loop's ``_ExecutorNameMap``) cannot hide a save from them.
* The loop owns ``status`` / ``tool`` / ``tool_call_id``; a payload cannot
  override them.

Dependency-free by design: the loop AND the builtin tools import it.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar, Token
from typing import Any

logger = logging.getLogger(__name__)

#: A sink receives the payload of one :func:`emit_tool_event` call.
ToolEventSink = Callable[[dict[str, Any]], Awaitable[None]]

#: ``status`` of the frame the loop emits for a tool's mid-execution report.
TOOL_PROGRESS_STATUS = "tool_progress"

#: ``event`` value: a presentation revision is committed and readable. Payload
#: keys: ``presentation_id`` (str), ``revision`` (int).
PRESENTATION_SAVED_EVENT = "presentation_saved"

#: ``event`` value: a video revision is committed and readable (Video Studio) - the editor's preview refreshes. Payload keys: ``video_id`` (str),
#: ``revision`` (int).
VIDEO_SAVED_EVENT = "video_saved"

#: ``event`` value: a custom dashboard revision is committed and readable
#: (Dashboard Studio) - the studio page refreshes. Payload keys:
#: ``dashboard_id`` (str), ``revision`` (int).
DASHBOARD_SAVED_EVENT = "dashboard_saved"

#: ``event`` value: a runner command is held for the user's approval and the
#: tool is WAITING for the decision (an XTM One Desktop chat). Payload
#: keys: ``command_id`` (str), ``runner_id`` (str), ``runner_name`` (str),
#: ``kind`` (str, e.g. ``run_shell``), ``summary`` (str, the command / path the
#: agent asked for), ``expires_in_seconds`` (int, how long the tool waits).
#: The chat renders it as an inline approval card wired to the existing
#: ``POST /runners/commands/{id}/approve|reject`` endpoints.
RUNNER_APPROVAL_REQUIRED_EVENT = "runner_approval_required"

#: ``event`` value: the held command left the approval queue - approved and
#: executed, rejected, or the wait ran out. Payload keys: ``command_id`` (str),
#: ``status`` (str, the command's status when the wait ended).
RUNNER_APPROVAL_RESOLVED_EVENT = "runner_approval_resolved"

#: ``event`` values: a ``computer_use`` command appeared or changed state
#: (running, awaiting approval), then ended. Payload key: ``command``,
#: the FULL bounded display record built by ``services/runner_activity.py`` - an
#: upsert keyed by its ``command_id``, nested so the loop's own ``status`` /
#: ``tool`` keys cannot collide with it. The chat renders it as a card in the
#: transcript and persists it with the message (``runner_activity``).
RUNNER_COMMAND_STARTED_EVENT = "runner_command_started"
RUNNER_COMMAND_FINISHED_EVENT = "runner_command_finished"

#: Upper bound on one serialised payload (``event`` included). A frame past
#: this is a handler streaming its result through the wrong channel, and it
#: would be pushed to every client of the conversation.
MAX_TOOL_EVENT_PAYLOAD_CHARS = 8_000


class ToolEventBinding:
    """One tool call's sink, closed by the loop when the call ends.

    Returned by :func:`bind_tool_event_sink` and handed back to
    :func:`reset_tool_event_sink`. ``closed`` is what a task that inherited
    the binding (a continuation the handler spawned) observes after the call
    it belonged to has finished: its emits become no-ops instead of frames
    tagged with a finished call.
    """

    __slots__ = ("_token", "closed", "deadline", "iteration", "sink", "tool_name")

    def __init__(
        self,
        sink: ToolEventSink | None,
        tool_name: str | None = None,
        iteration: int | None = None,
        deadline: float | None = None,
    ) -> None:
        self.sink = sink
        #: The LLM-visible name of the call, for the warnings only: the loop
        #: tags the frame itself, and two tools may emit the same ``event``.
        self.tool_name = tool_name
        #: The 1-based loop iteration the call runs in - what a tool
        #: that must say "already viewed at step N" reads through
        #: :func:`current_tool_iteration`. ``None`` outside the loop.
        self.iteration = iteration
        #: The ``time.monotonic()`` instant the loop cancels the call,
        #: read through :func:`current_tool_deadline`. ``None`` without a limit.
        self.deadline = deadline
        self.closed = False
        self._token: Token[ToolEventBinding | None] | None = None


_tool_event_binding: ContextVar[ToolEventBinding | None] = ContextVar("tool_event_binding", default=None)


def bind_tool_event_sink(
    sink: ToolEventSink | None,
    *,
    tool_name: str | None = None,
    iteration: int | None = None,
    timeout: float | None = None,
) -> ToolEventBinding:
    """Route the current context's :func:`emit_tool_event` calls to *sink*.

    Bind in the task that runs the handler (or before the handler's task is
    created - a task copies the context at creation) and reset with the
    returned binding in the same ``finally`` that closes the call, so a nested
    loop (a consult running inside a tool) restores the outer call's sink
    after each of its own calls. ``None`` silences the emitter - which is how
    a nested loop without a consumer shadows the outer call's sink instead of
    leaking its tools' reports onto it. *tool_name* names the call in the
    warnings a dropped or undeliverable report leaves behind; *iteration* is
    the 1-based loop iteration the call belongs to; *timeout* is the
    call's own limit in seconds from now. The deadline kept is never
    later than the enclosing call's: the outer loop cancels a nested loop's
    calls with the call they run in.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    outer = _tool_event_binding.get()
    if outer is not None and not outer.closed and outer.deadline is not None:
        deadline = outer.deadline if deadline is None else min(deadline, outer.deadline)
    binding = ToolEventBinding(sink, tool_name, iteration, deadline)
    binding._token = _tool_event_binding.set(binding)
    return binding


def current_tool_iteration() -> int | None:
    """The 1-based agent-loop iteration of the tool call running in this context.

    ``None`` outside a bound tool call (a REST route reusing a tool function,
    a handler under test) or once the call's binding has closed. Read-only
    context for a tool that wants to date what it answers - never a
    reason for the tool to behave differently outside the loop.
    """
    binding = _tool_event_binding.get()
    if binding is None or binding.closed:
        return None
    return binding.iteration


def current_tool_deadline() -> float | None:
    """The ``time.monotonic()`` instant the agent loop cancels the tool call
    running in this context.

    ``None`` outside a bound tool call, for a call bound without a limit, or
    once the call's binding has closed. For a tool whose work is waiting - a
    ``computer_use`` command's result - so that it stops in time to say what
    happened, instead of the loop's bare timeout saying it for it.
    """
    binding = _tool_event_binding.get()
    if binding is None or binding.closed:
        return None
    return binding.deadline


def reset_tool_event_sink(binding: ToolEventBinding) -> None:
    """Close *binding* and undo :func:`bind_tool_event_sink` (same context).

    Closing comes first and is what matters for a task that outlives the
    call: the ``ContextVar`` reset only affects the context that bound it,
    while ``closed`` is seen through every copy of that context.
    """
    binding.closed = True
    if binding._token is not None:
        _tool_event_binding.reset(binding._token)
        binding._token = None


def tool_event_sink_bound() -> bool:
    """Whether the current context can deliver a tool progress event."""
    binding = _tool_event_binding.get()
    return binding is not None and not binding.closed and binding.sink is not None


def _json_safe_frame(event: str, payload: dict[str, Any], tool_name: str | None) -> dict[str, Any] | None:
    """The frame as every consumer will serialise it, or ``None`` to drop it.

    ``default=str`` turns the values this codebase passes around (``UUID``,
    ``datetime``, ``Decimal``) into their string form; ``allow_nan=False``
    refuses ``NaN`` / ``Infinity``, which ``json.dumps`` would otherwise emit
    as bare tokens no browser ``JSON.parse`` accepts. The round-trip also
    normalises containers (a tuple becomes a list) so the sink sees exactly
    what the client will. The whole round-trip is one guarded step and every
    ``Exception`` is a drop, not only the ``TypeError`` / ``ValueError`` the
    encoder documents: ``default=str`` runs the value's own ``__str__``, a
    deeply nested payload recurses, and the handler that reported must not
    fail because of what it reported.
    """
    try:
        encoded = json.dumps({**payload, "event": event}, default=str, allow_nan=False)
        if len(encoded) > MAX_TOOL_EVENT_PAYLOAD_CHARS:
            logger.warning(
                "tool progress event %r from tool %r dropped: payload is %d chars (limit %d)",
                event,
                tool_name,
                len(encoded),
                MAX_TOOL_EVENT_PAYLOAD_CHARS,
            )
            return None
        return json.loads(encoded)
    except Exception:
        logger.warning(
            "tool progress event %r from tool %r dropped: payload is not JSON-serialisable",
            event,
            tool_name,
            exc_info=True,
        )
        return None


async def emit_tool_event(event: str, /, **payload: Any) -> None:
    """Report *event* from inside a tool handler.

    Never raises: a delivery failure is logged and the handler carries on.
    A no-op when no sink is bound (the handler is not running inside the
    agentic loop, or the loop has no consumer) and once the call the binding
    belonged to has closed. *event* is positional-only so a payload key of
    the same name cannot collide with it - the name given here always wins.
    """
    binding = _tool_event_binding.get()
    if binding is None or binding.closed or binding.sink is None:
        return
    frame = _json_safe_frame(event, payload, binding.tool_name)
    if frame is None:
        return
    try:
        await binding.sink(frame)
    except Exception:
        logger.warning(
            "tool progress event %r from tool %r could not be delivered",
            event,
            binding.tool_name,
            exc_info=True,
        )
