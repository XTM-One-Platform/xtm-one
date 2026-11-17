# SPDX-License-Identifier: Apache-2.0

"""In-process async event bus for internal platform extensibility.

Provides a lightweight publish/subscribe mechanism so that different
parts of the platform can react to system events without tight coupling.
All listeners run in the same process (asyncio tasks); for cross-process
distribution use the existing Redis pub/sub (activity feed, SSE stream).

Usage::

    from app.services.event_bus import event_bus, Event

    # --- Register a listener (typically at module load or startup) ---
    @event_bus.on("assignment_run.completed")
    async def on_assignment_done(event: Event) -> None:
        logger.info("Assignment completed: %s", event.data)

    # --- Emit an event (fire-and-forget by default) ---
    await event_bus.emit("assignment_run.completed", {
        "run_id": "...",
        "assignment_id": "...",
        "agent_id": "...",
        "status": "completed",
    })
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# ─── Event types ────────────────────────────────────────────────────────

# Well-known event names (use these constants to avoid typos).
# Additional custom event names are allowed — the bus is open by design.
ASSIGNMENT_RUN_STARTED = "assignment_run.started"
ASSIGNMENT_RUN_COMPLETED = "assignment_run.completed"
ASSIGNMENT_RUN_FAILED = "assignment_run.failed"

AGENT_CHAT_MESSAGE = "agent.chat.message"
AGENT_CHAT_RESPONSE = "agent.chat.response"
AGENT_CREATED = "agent.created"
AGENT_UPDATED = "agent.updated"
AGENT_DELETED = "agent.deleted"

INTEGRATION_CONNECTED = "integration.connected"
INTEGRATION_DISCONNECTED = "integration.disconnected"
INTEGRATION_ERROR = "integration.error"

KNOWLEDGE_BASE_UPDATED = "knowledge_base.updated"
KNOWLEDGE_BASE_DOCUMENT_ADDED = "knowledge_base.document.added"

CHANNEL_MESSAGE_RECEIVED = "channel.message.received"
CHANNEL_MESSAGE_SENT = "channel.message.sent"

TOOL_EXECUTED = "tool.executed"
TOOL_FAILED = "tool.failed"

USER_LOGIN = "user.login"
USER_CREATED = "user.created"

MEMORY_PERSISTED = "memory.persisted"

SUB_AGENT_TASK_COMPLETED = "sub_agent.task.completed"

# ─── Event dataclass ───────────────────────────────────────────────────

ListenerFn = Callable[["Event"], Coroutine[Any, Any, None]]


@dataclass(frozen=True, slots=True)
class Event:
    """An immutable event emitted through the bus."""

    name: str
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))


# ─── EventBus ──────────────────────────────────────────────────────────


class EventBus:
    """Simple in-process async event bus with wildcard matching.

    Supports exact event names and ``*`` wildcard patterns:
    - ``"assignment_run.completed"`` — matches only that event
    - ``"assignment_run.*"`` — matches any event starting with ``assignment_run.``
    - ``"*"`` — matches all events (global listener)
    """

    def __init__(self) -> None:
        self._listeners: dict[str, list[ListenerFn]] = {}
        # Strong references to in-flight fire-and-forget listener tasks.
        # asyncio only keeps weak references to tasks, so without this a
        # background listener (e.g. an admin platform-hook dispatcher) could be
        # garbage-collected mid-run, silently dropping the delivery.
        self._background_tasks: set[asyncio.Task] = set()

    # ── Registration ────────────────────────────────────────────────

    def on(self, event_name: str) -> Callable[[ListenerFn], ListenerFn]:
        """Decorator to register an async listener for an event name."""

        def _decorator(fn: ListenerFn) -> ListenerFn:
            self.add_listener(event_name, fn)
            return fn

        return _decorator

    def add_listener(self, event_name: str, fn: ListenerFn) -> None:
        """Programmatically register a listener."""
        self._listeners.setdefault(event_name, []).append(fn)

    def remove_listener(self, event_name: str, fn: ListenerFn) -> None:
        """Remove a previously registered listener."""
        listeners = self._listeners.get(event_name)
        if listeners:
            with contextlib.suppress(ValueError):
                listeners.remove(fn)

    # ── Emission ────────────────────────────────────────────────────

    async def emit(
        self,
        event_name: str,
        data: dict[str, Any] | None = None,
        *,
        wait: bool = False,
    ) -> None:
        """Emit an event to all matching listeners.

        Args:
            event_name: The event name (e.g. ``"assignment_run.completed"``).
            data: Arbitrary event payload.
            wait: If ``True``, await all listeners before returning.
                  If ``False`` (default), listeners run as background tasks.
        """
        event = Event(name=event_name, data=data or {})
        matched = self._resolve_listeners(event_name)

        if not matched:
            return

        if wait:
            await asyncio.gather(
                *(self._safe_call(fn, event) for fn in matched),
                return_exceptions=True,
            )
        else:
            for fn in matched:
                task = asyncio.create_task(self._safe_call(fn, event))
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

    def _resolve_listeners(self, event_name: str) -> list[ListenerFn]:
        """Collect all listeners that match the event name."""
        matched: list[ListenerFn] = []

        # Exact match
        if event_name in self._listeners:
            matched.extend(self._listeners[event_name])

        # Wildcard match: "assignment_run.*" matches "assignment_run.completed"
        prefix = event_name.rsplit(".", 1)[0] + ".*" if "." in event_name else None
        if prefix and prefix in self._listeners:
            matched.extend(self._listeners[prefix])

        # Global wildcard
        if "*" in self._listeners:
            matched.extend(self._listeners["*"])

        return matched

    @staticmethod
    async def _safe_call(fn: ListenerFn, event: Event) -> None:
        """Invoke a listener, catching and logging any exception."""
        try:
            await fn(event)
        except Exception:
            logger.exception(
                "Event listener %s failed for event '%s'",
                getattr(fn, "__qualname__", fn),
                event.name,
            )

    # ── Introspection ───────────────────────────────────────────────

    def clear(self) -> None:
        """Remove all listeners (useful in tests)."""
        self._listeners.clear()


# ─── Singleton ─────────────────────────────────────────────────────────

event_bus = EventBus()
"""Global event bus instance for the platform."""
