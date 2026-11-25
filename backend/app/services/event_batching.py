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

"""Batch window of event triggers: one run per batch of events.

An observer of ``run.failed`` or ``run.completed`` (or of any integration
event) used to start one whole run per event: a storm of 30 failures was 30
runs reading the same state. An assignment whose ``trigger_config`` sets
``batch_window_seconds`` collects its events instead:

- the first event of a batch creates the run, delayed by the window (or queued
  behind the runs in flight), its payload a batch listing that event;
- every later event finds that run still waiting (delayed with more than
  ``MERGE_MARGIN_SECONDS`` left, or queued) and is appended to its ``events``;
- the run starts once the window ends, its payload carrying the list;
- a batch holds at most ``MAX_BATCH_EVENTS`` events: the next one starts a
  batch of its own.

Absent or ``0`` keeps one run per event, as before.

Two optional keys narrow it (#2286): ``batch_events`` lists the event names the
window applies to (another event of the same trigger runs alone, at once: a
CTEM case reopened never waits behind the opened burst), and
``batch_max_events`` caps the events of one batch below ``MAX_BATCH_EVENTS``
(the cases one Loop run can work). Pure and unit-tested; the SQL stays in
``assignment_engine``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: ``trigger_config`` key of an assignment's batch window, in seconds.
WINDOW_CONFIG = "batch_window_seconds"
#: ``trigger_context`` key marking a batch run and the window it was given.
WINDOW_CONTEXT = "batch_window_seconds"
#: Longest window a trigger may set.
MAX_WINDOW_SECONDS = 600
#: Events one batch run carries at most; the next one opens a new batch.
MAX_BATCH_EVENTS = 100
#: ``trigger_config`` key of the event names the window applies to (absent: every event).
EVENTS_CONFIG = "batch_events"
#: ``trigger_config`` key of the most events one batch run carries (absent: ``MAX_BATCH_EVENTS``).
MAX_EVENTS_CONFIG = "batch_max_events"
#: Trigger types whose runs a window batches (``trigger_context["type"]``).
BATCHED_CONTEXT_TYPES = frozenset({"integration_event", "platform_event"})
#: Trigger types whose ``trigger_config`` may carry the window.
BATCHED_TRIGGER_TYPES = BATCHED_CONTEXT_TYPES


def window_error(trigger_type: object, trigger_config: object) -> str | None:
    """The refusal for a ``batch_window_seconds`` as it will be saved, else ``None``."""
    if not isinstance(trigger_config, Mapping) or WINDOW_CONFIG not in trigger_config:
        return None
    raw = trigger_config.get(WINDOW_CONFIG)
    if trigger_type not in BATCHED_TRIGGER_TYPES:
        return (
            f"Invalid trigger_config: '{WINDOW_CONFIG}' applies to integration_event and platform_event triggers only."
        )
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= MAX_WINDOW_SECONDS:
        return f"Invalid trigger_config: '{WINDOW_CONFIG}' must be a whole number of seconds from 0 to {MAX_WINDOW_SECONDS}."
    names = trigger_config.get(EVENTS_CONFIG)
    if names is not None and (
        not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names)
    ):
        return f"Invalid trigger_config: '{EVENTS_CONFIG}' must be a non-empty list of event names."
    cap = trigger_config.get(MAX_EVENTS_CONFIG)
    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= MAX_BATCH_EVENTS):
        return f"Invalid trigger_config: '{MAX_EVENTS_CONFIG}' must be a whole number from 1 to {MAX_BATCH_EVENTS}."
    return None


def window_seconds(trigger_config: Mapping[str, Any] | None, trigger_context: Mapping[str, Any] | None) -> int:
    """The batch window of a delivery: the assignment's, for an event run; 0 when none."""
    if not isinstance(trigger_context, Mapping) or trigger_context.get("type") not in BATCHED_CONTEXT_TYPES:
        return 0
    raw = trigger_config.get(WINDOW_CONFIG) if isinstance(trigger_config, Mapping) else None
    if raw is None or isinstance(raw, bool):
        return 0
    names = trigger_config.get(EVENTS_CONFIG) if isinstance(trigger_config, Mapping) else None
    if isinstance(names, list) and names and trigger_context.get("event") not in names:
        return 0
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(MAX_WINDOW_SECONDS, seconds))


def max_events(trigger_config: Mapping[str, Any] | None) -> int:
    """The most events one batch run of the trigger carries: ``batch_max_events``, else ``MAX_BATCH_EVENTS``."""
    raw = trigger_config.get(MAX_EVENTS_CONFIG) if isinstance(trigger_config, Mapping) else None
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return min(raw, MAX_BATCH_EVENTS)
    return MAX_BATCH_EVENTS


def events_of(payload: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """The events a batch payload lists (an unbatched payload is one event)."""
    if not isinstance(payload, Mapping):
        return []
    events = payload.get("events")
    if payload.get("batch") is True and isinstance(events, list):
        return [e for e in events if isinstance(e, dict)]
    return [dict(payload)]


def _event_id(event: Mapping[str, Any]) -> str:
    return str(event.get("event_id") or event.get("message_id") or "")


def batch_payload(payload: Mapping[str, Any] | None, window: int) -> dict[str, Any]:
    """The payload of a new batch run opened by *payload*."""
    event = dict(payload or {})
    batch: dict[str, Any] = {"batch": True, "batch_window_seconds": window, "events": [event], "event_count": 1}
    if event.get("trigger_type"):
        batch["trigger_type"] = event["trigger_type"]
    return batch


def can_join(waiting: Mapping[str, Any] | None, limit: int = MAX_BATCH_EVENTS) -> bool:
    """Whether the waiting batch has room for one more event (*limit*: the trigger's :func:`max_events`)."""
    return len(events_of(waiting)) < min(limit, MAX_BATCH_EVENTS)


def carries(waiting: Mapping[str, Any] | None, incoming: Mapping[str, Any] | None) -> bool:
    """Whether the waiting batch already lists *incoming* (the same event delivered twice)."""
    incoming_id = _event_id(incoming or {})
    return bool(incoming_id) and any(_event_id(e) == incoming_id for e in events_of(waiting))


def merged_payload(waiting: Mapping[str, Any] | None, incoming: Mapping[str, Any] | None) -> dict[str, Any]:
    """The waiting batch with *incoming* appended, in arrival order."""
    merged = dict(waiting or {})
    events = events_of(waiting)
    if not carries(waiting, incoming):
        events.append(dict(incoming or {}))
    merged["batch"] = True
    merged["events"] = events
    merged["event_count"] = len(events)
    return merged
