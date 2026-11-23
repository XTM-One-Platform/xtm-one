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

"""Coalescing of polled integration events that stand for one state change.

Some providers report one change as a burst of events: a batch recompute in
OpenCRQ ends tens of job runs within minutes (``opencrq_job_events``), and each
would start a whole run of the assignment listening to it (#1417). A provider
poller that declares it puts a ``coalesce_key`` on the event it hands over; the
dispatcher copies it into the run's trigger context, and
``assignment_engine.trigger_or_queue_assignment`` folds a later event with the
same key into the run of the same assignment still waiting in the queue
instead of queuing a second one. With the assignment's concurrency cap at 1,
one run of the key runs and at most one waits, whatever the burst.

An event without the key is dispatched as before: coalescing applies only to
the providers and events that declare it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Event payload key a poller sets to make its events coalesce.
PAYLOAD_KEY = "coalesce_key"
#: Trigger-context key the dispatcher copies it to (read under the trigger lock).
CONTEXT_KEY = "coalesce_key"
#: Most runs a merged payload lists in ``covers``; ``run_count`` keeps the total.
MAX_COVERED = 50


def payload_key(payload: Mapping[str, Any] | None) -> str | None:
    """The coalesce key an event payload declares, or ``None``."""
    if not isinstance(payload, Mapping):
        return None
    key = payload.get(PAYLOAD_KEY)
    return key if isinstance(key, str) and key else None


def context_key(trigger_context: Mapping[str, Any] | None) -> str | None:
    """The coalesce key of an integration-event delivery, or ``None``."""
    if not isinstance(trigger_context, Mapping) or trigger_context.get("type") != "integration_event":
        return None
    key = trigger_context.get(CONTEXT_KEY)
    return key if isinstance(key, str) and key else None


def _covers(payload: Mapping[str, Any]) -> list[dict[str, Any]]:
    covers = payload.get("covers")
    if isinstance(covers, list):
        return [c for c in covers if isinstance(c, dict)]
    entity = payload.get("entity")
    return [dict(entity)] if isinstance(entity, Mapping) else []


def _count(payload: Mapping[str, Any]) -> int:
    count = payload.get("run_count")
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        return count
    return max(len(_covers(payload)), 1)


def merged_payload(waiting: Mapping[str, Any] | None, incoming: Mapping[str, Any] | None) -> dict[str, Any]:
    """The payload of a waiting run once *incoming* is folded into it.

    The newest event's entity, id and summary win (the run reads the state as
    it is when it starts); ``covers`` lists the events of both, deduplicated by
    id, the newest ``MAX_COVERED`` kept, and ``run_count`` adds both counts.
    """
    waiting = dict(waiting or {})
    incoming = dict(incoming or {})
    covers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in [*_covers(waiting), *_covers(incoming)]:
        entry_id = str(entry.get("id") or "")
        if entry_id and entry_id in seen:
            continue
        if entry_id:
            seen.add(entry_id)
        covers.append(entry)
    duplicates = len(_covers(waiting)) + len(_covers(incoming)) - len(covers)
    merged = {**waiting, **incoming}
    merged["covers"] = covers[-MAX_COVERED:]
    merged["run_count"] = max(_count(waiting) + _count(incoming) - duplicates, len(covers))
    return merged
