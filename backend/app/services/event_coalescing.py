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

The run folded into is any run of the key that has not started: queued behind
a run in flight, or ``pending`` in a free slot (#2117: the first page of a
backlog starts a run, the next page of the same poll tick folds into it). The
executor re-reads the payload under the row lock when it marks the run
running, so a fold committed before that is in the run's prompt, and one that
comes after finds the run started and queues the next run.

A poller declares how its own fields merge with ``coalesce_merge`` on the event
(:data:`MERGE_KEY`): ``union`` lists are joined without duplicates, ``by_key``
lists of objects are joined by an id field (the newer object wins), and
``count_at_least`` counts are at least the length of the list they count and
never below either side, and ``sum`` counts add up, less what the covers both
events list carry under the same name (an event covered twice counts once).
A field the spec does not name takes the newer event's value, as before. A
field also carried on ``entity`` is mirrored there.

Debounce (#2544). Folding into a run that has not started only merges the
events of one poll tick when the run it starts ends before the next tick:
a burst spread over several ticks still started one run per tick. A poller
may therefore declare a quiet period on its event (:data:`QUIET_KEY`, with
its cap :data:`MAX_KEY`): the run of the key is created ``delayed`` and held
until no event of the key arrived for the quiet period, never past the cap
counted from the burst's first event. Every event that arrives meanwhile
folds into the held run and moves its start (:func:`hold_seconds`). The
hold is the run row and its orchestration execution, both in PostgreSQL, so
a worker restart neither loses nor doubles a held burst. An event without
the declaration is dispatched as before.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

#: Event payload key a poller sets to make its events coalesce.
PAYLOAD_KEY = "coalesce_key"
#: Trigger-context key the dispatcher copies it to (read under the trigger lock).
CONTEXT_KEY = "coalesce_key"
#: Most runs a merged payload lists in ``covers``; ``run_count`` keeps the total.
MAX_COVERED = 50
#: Event payload key holding the poller's merge spec (``union``, ``by_key``, ``count_at_least``).
MERGE_KEY = "coalesce_merge"
#: Run statuses a coalescing event folds into: the run has not started.
FOLDABLE_STATUSES = ("queued", "pending")
#: Event payload key: seconds without a new event of the key before its run starts.
QUIET_KEY = "coalesce_quiet_seconds"
#: Event payload key: most seconds a burst is held, from its first event.
MAX_KEY = "coalesce_max_seconds"
#: Trigger-context keys of a held run: the declaration and the burst's first and last events.
HOLD_CONTEXT = "coalesce_hold"
#: A held run is ``delayed`` until its start: a debounced event folds into it too.
HELD_STATUS = "delayed"
#: Longest hold a poller may declare: half the stale-run sweep's default
#: (``STALE_ASSIGNMENT_RUN_SECONDS``, 1800 s), well under the queue-wait bound.
MAX_HOLD_SECONDS = 900


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


def now() -> datetime:
    """The clock the holds are counted on (a test moves it)."""
    return datetime.now(UTC)


def _seconds(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return max(0, min(int(value), MAX_HOLD_SECONDS))


def debounce(payload: Mapping[str, Any] | None) -> tuple[int, int] | None:
    """``(quiet, cap)`` seconds an event declares, or ``None`` (dispatched as before).

    The cap is never below the quiet period.
    """
    if not isinstance(payload, Mapping) or payload_key(payload) is None:
        return None
    quiet = _seconds(payload.get(QUIET_KEY))
    if quiet <= 0:
        return None
    return quiet, max(quiet, _seconds(payload.get(MAX_KEY)))


def _instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def held_context(
    trigger_context: Mapping[str, Any] | None, payload: Mapping[str, Any] | None, now: datetime
) -> dict[str, Any] | None:
    """*trigger_context* with the hold of a debounced event recorded, or ``None`` when it declares none.

    A context already holding keeps its burst's first event; the last moves to *now*.
    """
    declared = debounce(payload)
    if declared is None:
        return None
    context = dict(trigger_context or {})
    hold = context.get(HOLD_CONTEXT)
    first = _instant(hold.get("first_at")) if isinstance(hold, Mapping) else None
    context[HOLD_CONTEXT] = {
        "quiet_seconds": declared[0],
        "max_seconds": declared[1],
        "first_at": (first or now).isoformat(),
        "last_at": now.isoformat(),
    }
    return context


def hold_seconds(trigger_context: Mapping[str, Any] | None, now: datetime) -> int:
    """Seconds from *now* until a held run may start: the quiet period after the
    last event, never past the cap from the first one; 0 when nothing is held."""
    hold = trigger_context.get(HOLD_CONTEXT) if isinstance(trigger_context, Mapping) else None
    if not isinstance(hold, Mapping):
        return 0
    first = _instant(hold.get("first_at"))
    last = _instant(hold.get("last_at")) or first
    if first is None or last is None:
        return 0
    quiet = _seconds(hold.get("quiet_seconds"))
    cap = max(quiet, _seconds(hold.get("max_seconds")))
    start = min(last + timedelta(seconds=quiet), first + timedelta(seconds=cap))
    return max(0, math.ceil((start - now).total_seconds()))


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
    _merge_declared_fields(merged, waiting, incoming)
    return merged


def _spec(waiting: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
    spec = incoming.get(MERGE_KEY)
    if not isinstance(spec, Mapping):
        spec = waiting.get(MERGE_KEY)
    return dict(spec) if isinstance(spec, Mapping) else {}


def _names(value: Any) -> list[str]:
    return [name for name in value if isinstance(name, str)] if isinstance(value, list | tuple) else []


def _list(payload: Mapping[str, Any], field: str) -> list[Any]:
    value = payload.get(field)
    return list(value) if isinstance(value, list) else []


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _merge_declared_fields(merged: dict[str, Any], waiting: Mapping[str, Any], incoming: Mapping[str, Any]) -> None:
    """Apply the poller's ``coalesce_merge`` spec to *merged* (see the module doc)."""
    spec = _spec(waiting, incoming)
    if not spec:
        return
    written: list[str] = []
    for field in _names(spec.get("union")):
        joined: list[Any] = []
        for item in [*_list(waiting, field), *_list(incoming, field)]:
            if item not in joined:
                joined.append(item)
        merged[field] = joined
        written.append(field)
    by_key = spec.get("by_key")
    for field, id_field in by_key.items() if isinstance(by_key, Mapping) else []:
        if not isinstance(field, str) or not isinstance(id_field, str):
            continue
        keyed: dict[Any, Any] = {}
        loose: list[Any] = []
        for item in [*_list(waiting, field), *_list(incoming, field)]:
            item_id = item.get(id_field) if isinstance(item, Mapping) else None
            if item_id is None:
                loose.append(item)
            else:
                keyed.pop(item_id, None)
                keyed[item_id] = item
        if keyed or loose:
            merged[field] = [*loose, *keyed.values()]
        else:
            merged.pop(field, None)
        written.append(field)
    for field in _names(spec.get("sum")):
        if field not in waiting and field not in incoming:
            continue
        waiting_ids = {str(entry.get("id")) for entry in _covers(waiting) if entry.get("id")}
        twice = sum(
            _int(entry.get(field)) for entry in _covers(incoming) if entry.get("id") and str(entry["id"]) in waiting_ids
        )
        merged[field] = max(_int(waiting.get(field)) + _int(incoming.get(field)) - twice, 0)
        written.append(field)
    count_at_least = spec.get("count_at_least")
    for field, counted in count_at_least.items() if isinstance(count_at_least, Mapping) else []:
        if not isinstance(field, str) or not isinstance(counted, str):
            continue
        merged[field] = max(len(_list(merged, counted)), _int(waiting.get(field)), _int(incoming.get(field)))
        written.append(field)
    entity = merged.get("entity")
    if isinstance(entity, Mapping):
        entity = dict(entity)
        for field in written:
            if field not in entity:
                continue
            if field in merged:
                entity[field] = merged[field]
            else:
                entity.pop(field, None)
        merged["entity"] = entity
