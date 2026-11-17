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

"""Event-to-run latency of an event-triggered assignment run.

A platform event run carries ``occurred_at`` (when the event was raised) and
``triggered_at`` (when it was delivered to the subscriber) in its
``trigger_context``; an integration event run carries ``triggered_at`` (when
the push receiver or the poller handed it over). A run a provider webhook
delivered carries ``type: "webhook"`` whatever its assignment's trigger, so
the assignment's ``trigger_type`` names the kind of such a run. With the run's own
``created_at`` (queued) and ``started_at``, the pick-up latency is a measure:
:func:`event_latency` turns those timestamps into figures for the run's trace
metadata, and :func:`observe` feeds ``xtmone_event_to_run_seconds``.
:func:`task_event_latency` does the same for the background task a one-off
platform event watch starts (kind ``platform_event_watch``).

Timestamps only, nothing read from the event payload. Never raises: a missing
or malformed timestamp leaves its figure out.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from app.core.metrics import get_or_create_histogram

#: The ``trigger_context["type"]`` values of an event origin.
EVENT_KINDS: frozenset[str] = frozenset({"platform_event", "integration_event"})

#: The ``origin`` a one-off platform event watch writes in the background task
#: it starts, and the ``kind`` label of that task's latency.
WATCH_TASK_KIND = "platform_event_watch"

EVENT_TO_RUN_SECONDS = get_or_create_histogram(
    "xtmone_event_to_run_seconds",
    "Seconds from an event (raised, or handed over by the receiver or poller) to the start of the run it triggered, by kind.",
    ["kind"],
    buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1800, 3600),
)


def _when(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, str) and value:
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _ms(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return round(max(0.0, (end - start).total_seconds() * 1000.0), 1)


def _kind(trigger_context: Mapping[str, Any], trigger_type: Any) -> str | None:
    kind = trigger_context.get("type")
    if kind in EVENT_KINDS:
        return str(kind)
    # A provider webhook delivery writes ``type: "webhook"`` for every
    # assignment it starts: the assignment's own trigger says it is an event.
    if trigger_type in EVENT_KINDS:
        return str(trigger_type)
    return None


def event_latency(
    trigger_context: Mapping[str, Any] | None,
    *,
    created_at: Any,
    started_at: Any,
    trigger_type: Any = None,
) -> dict[str, Any] | None:
    """The ``event_latency`` trace metadata of a run, ``None`` without an event origin.

    ``trigger_type`` is the assignment's trigger: a webhook-delivered run of an
    ``integration_event`` assignment is measured as one.

    ``event_to_trigger_ms``: raised to delivered (platform events only);
    ``trigger_to_queue_ms``: delivered to the run row; ``queue_ms``: the run
    row to its start; ``event_to_run_ms``: the earliest known event instant to
    the start, the figure a pick-up target is read against; ``event_origin``:
    that instant, ``occurred_at`` (the event's own) or ``receipt`` (the
    platform's receipt, ``triggered_at``, when the provider gives none).
    """
    if not isinstance(trigger_context, Mapping):
        return None
    kind = _kind(trigger_context, trigger_type)
    if kind is None:
        return None
    occurred = _when(trigger_context.get("occurred_at"))
    triggered = _when(trigger_context.get("triggered_at"))
    queued = _when(created_at)
    started = _when(started_at)
    figures: dict[str, Any] = {
        "kind": kind,
        "event_to_trigger_ms": _ms(occurred, triggered),
        "trigger_to_queue_ms": _ms(triggered, queued),
        "queue_ms": _ms(queued, started),
        "event_to_run_ms": _ms(occurred or triggered, started),
        "event_origin": "occurred_at" if occurred else ("receipt" if triggered else None),
    }
    provider = trigger_context.get("provider")
    if isinstance(provider, str) and provider:
        figures["provider"] = provider[:64]
    return figures


def task_event_latency(metadata: Mapping[str, Any] | None, *, started_at: Any) -> dict[str, Any] | None:
    """The latency of a background task a platform event watch started, ``None`` for any other task.

    The watch writes ``occurred_at`` (the event raised) and ``triggered_at``
    (the watch fired, the task row created) in the task's metadata:
    ``event_to_trigger_ms`` is the dispatch, ``queue_ms`` the task row to its
    start, ``event_to_run_ms`` the figure read against the pick-up target.
    Never raises.
    """
    if not isinstance(metadata, Mapping) or metadata.get("origin") != WATCH_TASK_KIND:
        return None
    occurred = _when(metadata.get("occurred_at"))
    triggered = _when(metadata.get("triggered_at"))
    started = _when(started_at)
    return {
        "kind": WATCH_TASK_KIND,
        "event_to_trigger_ms": _ms(occurred, triggered),
        "queue_ms": _ms(triggered, started),
        "event_to_run_ms": _ms(occurred or triggered, started),
        "event_origin": "occurred_at" if occurred else ("receipt" if triggered else None),
    }


def observe(figures: Mapping[str, Any] | None) -> None:
    """Feed ``xtmone_event_to_run_seconds`` with one run's event-to-run figure."""
    if not figures:
        return
    ms = figures.get("event_to_run_ms")
    if isinstance(ms, int | float):
        EVENT_TO_RUN_SECONDS.labels(kind=str(figures.get("kind"))).observe(ms / 1000.0)
