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

"""Which queued assignment runs start, how many at once, and when to back off.

The delivery queue (``assignment_engine.trigger_or_queue_assignment`` /
``drain_assignment_queues``) used to start ONE run per assignment at a time and
promote the next one on a later reconciler cycle; a polled integration event
arriving while its assignment was busy was handed back to the provider and
fetched again on the next scheduler tick. Under a steady stream of events an
assignment therefore ran one event a minute, whatever its runs took. This module
holds what replaced that, apart from the SQL, which stays with the queue:

- the per-assignment concurrency cap (``trigger_config.max_concurrent_runs``,
  default ``ASSIGNMENT_MAX_CONCURRENT_RUNS``) and the ordering key
  (``trigger_config.ordering_key``: a dotted path into the trigger payload; runs
  with the same value run one at a time, in arrival order). A mail trigger
  that sets none is ordered by its thread (``DEFAULT_ORDERING_KEYS``);
- which runs share an assignment: deliveries (``DELIVERY_TRIGGER_TYPES``) and
  platform events (``SHARED_SLOT_TRIGGER_TYPES``) run side by side up to the
  cap, any other run - manual, follow-up, scheduled, chained - keeps the
  assignment to itself;
- ``plan_promotions``: how many queued runs each assignment starts in one drain,
  round-robin over agents and then over each agent's assignments, bounded by
  the fleet's free dispatch capacity and by the runs that can start now;
- ``select_runs``: which queued runs of one assignment start - oldest first, and
  oldest and newest alternately once its oldest queued run is older than
  ``ASSIGNMENT_QUEUE_BACKLOG_SECONDS``, so a backlog never delays fresh work by
  its whole length (nothing is dropped);
- the rate-limit back-off: a rate limit met by an assignment's run (a model
  provider's or an integration's 429, the AI Gateway's throttle) halves that
  assignment's cap for ``ASSIGNMENT_DISPATCH_RATE_LIMIT_BACKOFF_SECONDS``, a
  fixed window; one met anywhere else (chat, evaluations, embeddings) is none of dispatch's business
  (the retries of ``retry_llm_call`` and ``safe_request`` are unchanged);
- the per-assignment dispatch gauges served on ``/metrics``.

Everything that decides is pure and unit-tested; the Redis signals never raise.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import math
import time
import uuid
from collections import deque
from collections.abc import Iterable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.config import settings
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: ``trigger_config`` key of an assignment's own concurrency cap.
MAX_CONCURRENT_RUNS_KEY = "max_concurrent_runs"
#: ``trigger_config`` key of the ordering-key path into the trigger payload.
ORDERING_KEY_CONFIG = "ordering_key"
#: ``trigger_config`` key of the tool groups a run reveals before its first
#: request (``run_agent_loop(preload_tool_groups=)``).
PRELOAD_TOOL_GROUPS_KEY = "preload_tool_groups"
#: ``trigger_context`` key a run's resolved ordering key is stored under.
ORDERING_KEY_CONTEXT = "ordering_key"

MAX_CONCURRENCY_CAP = 50
_ORDERING_PATH_MAX_CHARS = 200
_ORDERING_KEY_MAX_CHARS = 200

#: ``trigger_context["type"]`` of a delivery: a webhook delivery and an
#: integration event (quota pre-check, ordering key, chat coalescing).
DELIVERY_TRIGGER_TYPES = frozenset({"webhook", "integration_event"})

#: ``trigger_context["type"]`` of the runs that share an assignment's slots up to
#: its cap: the deliveries, and a platform event (each event is its own run;
#: two events about one object keep their order through the ordering key
#: ``platform_events.trigger_context`` sets). A platform event is not a
#: delivery otherwise: no quota pre-check, no chat coalescing. Every other run
#: (manual, follow-up, scheduled, chained, ...) has the assignment to itself.
SHARED_SLOT_TRIGGER_TYPES = DELIVERY_TRIGGER_TYPES | frozenset({"platform_event"})

#: The ordering key of a trigger that sets none, by ``(provider, event)``: two
#: mails of one thread never run side by side. ``ordering_key: ""``
#: opts out. A chat message is ordered by the ``conversation_key`` its
#: payload builder stamps (``CONVERSATION_KEY_FIELD``).
DEFAULT_ORDERING_KEYS: dict[tuple[str, str], str] = {
    ("microsoft", "new_email"): "conversation_id",
    ("google", "new_email"): "thread_id",
}

#: The payload field a chat channel's payload builder sets to the conversation
#: a message belongs to (a chat, a channel thread): the default ordering key of
#: such a delivery, so two messages of one conversation never run side by side,
#: and the mark ``chat_turn_coalescing`` recognises a conversation event by.
CONVERSATION_KEY_FIELD = "conversation_key"


def is_delivery(trigger_context: dict | None) -> bool:
    """Whether a run with this ``trigger_context`` is a delivery."""
    return isinstance(trigger_context, dict) and trigger_context.get("type") in DELIVERY_TRIGGER_TYPES


def shares_slots(run_type: object) -> bool:
    """Whether a run of ``trigger_context["type"]`` *run_type* shares its assignment up to the cap."""
    return run_type in SHARED_SLOT_TRIGGER_TYPES


def event_run_working_window(trigger_context: dict | None) -> int | None:
    """The working window, in tokens, of an event-driven run (#2430).

    A run of a platform event, an integration event or a webhook
    (``SHARED_SLOT_TRIGGER_TYPES``) gets
    ``AGENT_LOOP_EVENT_RUN_WORKING_WINDOW_TOKENS``, which the agent loop
    computes its working-context budgets on (``run_agent_loop(
    working_window_tokens=)``); every other run, or the setting at ``0``:
    ``None``, the model's window.
    """
    if not isinstance(trigger_context, dict) or not shares_slots(trigger_context.get("type")):
        return None
    tokens = settings.agent_loop_event_run_working_window_tokens
    return tokens if tokens > 0 else None


#: Trigger types of a seeded assignment that fires once per event: born with
#: ``SEEDED_EVENT_CONCURRENCY`` runs at once unless its seed names a cap, so an
#: event storm on one seeded fleet never takes the whole worker pool.
SEEDED_EVENT_TRIGGER_TYPES = frozenset({"integration_event", "platform_event"})
SEEDED_EVENT_CONCURRENCY = 1


def seeded_trigger_config(trigger_type: object, trigger_config: dict[str, Any] | None) -> dict[str, Any]:
    """The ``trigger_config`` a built-in seed writes: an event trigger capped at one run unless it names a cap."""
    config = dict(trigger_config) if isinstance(trigger_config, dict) else {}
    if trigger_type in SEEDED_EVENT_TRIGGER_TYPES:
        config.setdefault(MAX_CONCURRENT_RUNS_KEY, SEEDED_EVENT_CONCURRENCY)
    return config


def _clamp_cap(value: int) -> int:
    return max(1, min(MAX_CONCURRENCY_CAP, int(value)))


def default_concurrency_cap() -> int:
    return _clamp_cap(settings.assignment_max_concurrent_runs)


def assignment_concurrency_cap(trigger_config: dict | None) -> int:
    """How many runs of one assignment may be in flight at once.

    ``trigger_config.max_concurrent_runs`` when it is a whole number, else
    ``ASSIGNMENT_MAX_CONCURRENT_RUNS``; clamped to 1..``MAX_CONCURRENCY_CAP``.
    """
    raw = (trigger_config or {}).get(MAX_CONCURRENT_RUNS_KEY) if isinstance(trigger_config, dict) else None
    if raw is None or isinstance(raw, bool):
        return default_concurrency_cap()
    try:
        return _clamp_cap(int(raw))
    except (TypeError, ValueError):
        return default_concurrency_cap()


#: Ceiling of a ``work_item`` inbox's ``max_concurrent_runs``: each
#: slot is a whole agentic run on its own item, so the bound is tighter than a
#: delivery's.
WORK_ITEM_MAX_CONCURRENT_RUNS = 10


def work_item_concurrency_error(trigger_config: object) -> str | None:
    """The refusal for a ``work_item`` trigger's ``max_concurrent_runs``, else ``None``.

    Absent is the serial default (1); otherwise a whole number from 1 to
    ``WORK_ITEM_MAX_CONCURRENT_RUNS``. A boolean, a string or a float is
    refused rather than read as a number nobody chose. The express lane
    (``express_lane``) is judged here too, so every save path checks both.
    """
    if not isinstance(trigger_config, dict):
        return None
    express_error = work_item_express_lane_error(trigger_config)
    if express_error is not None:
        return express_error
    raw = trigger_config.get(MAX_CONCURRENT_RUNS_KEY)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= WORK_ITEM_MAX_CONCURRENT_RUNS:
        return (
            "Invalid trigger_config: max_concurrent_runs of a work_item trigger must be "
            f"a whole number from 1 to {WORK_ITEM_MAX_CONCURRENT_RUNS} (items run in parallel)."
        )
    return None


#: ``trigger_config`` key of a ``work_item`` inbox's express lane (#11082):
#: ``{"correlation_prefixes": [...], "slots": N}``.
EXPRESS_LANE_KEY = "express_lane"
#: Most extra runs an express lane adds above ``max_concurrent_runs``.
WORK_ITEM_MAX_EXPRESS_SLOTS = 5
#: Most correlation-key prefixes one express lane names.
WORK_ITEM_MAX_EXPRESS_PREFIXES = 10


@dataclass(frozen=True)
class ExpressLane:
    """Extra inbox slots kept for the items whose correlation key starts with a prefix.

    Short critical-path items (a verdict post, a merge) never wait behind
    long runs holding every regular slot: they may run up to ``slots`` runs
    above ``max_concurrent_runs``; every other item stops at the regular cap.
    """

    prefixes: tuple[str, ...]
    slots: int

    def matches(self, correlation_key: object) -> bool:
        """Whether an item with *correlation_key* may take an express slot."""
        if not isinstance(correlation_key, str):
            return False
        key = correlation_key.strip().lower()
        return any(key.startswith(prefix.lower()) for prefix in self.prefixes)


def work_item_express_lane_error(trigger_config: object) -> str | None:
    """The refusal for a ``work_item`` trigger's ``express_lane``, else ``None``.

    Absent or null is no lane. Otherwise an object with ``correlation_prefixes``
    (1 to ``WORK_ITEM_MAX_EXPRESS_PREFIXES`` non-empty strings) and ``slots``
    (a whole number from 1 to ``WORK_ITEM_MAX_EXPRESS_SLOTS``).
    """
    if not isinstance(trigger_config, dict):
        return None
    raw = trigger_config.get(EXPRESS_LANE_KEY)
    if raw is None:
        return None
    shape = (
        "Invalid trigger_config: express_lane of a work_item trigger must be an object with "
        f"correlation_prefixes (1 to {WORK_ITEM_MAX_EXPRESS_PREFIXES} non-empty strings) and "
        f"slots (a whole number from 1 to {WORK_ITEM_MAX_EXPRESS_SLOTS})."
    )
    if not isinstance(raw, dict) or set(raw) - {"correlation_prefixes", "slots"}:
        return shape
    prefixes = raw.get("correlation_prefixes")
    if (
        not isinstance(prefixes, list)
        or not 1 <= len(prefixes) <= WORK_ITEM_MAX_EXPRESS_PREFIXES
        or any(not isinstance(p, str) or not p.strip() for p in prefixes)
    ):
        return shape
    slots = raw.get("slots")
    if isinstance(slots, bool) or not isinstance(slots, int) or not 1 <= slots <= WORK_ITEM_MAX_EXPRESS_SLOTS:
        return shape
    return None


def work_item_express_lane(trigger_config: dict | None) -> ExpressLane | None:
    """The express lane a ``work_item`` inbox declares, or ``None`` (none, or unreadable)."""
    if not isinstance(trigger_config, dict) or trigger_config.get(EXPRESS_LANE_KEY) is None:
        return None
    if work_item_express_lane_error(trigger_config) is not None:
        return None
    raw = trigger_config[EXPRESS_LANE_KEY]
    prefixes = tuple(dict.fromkeys(p.strip() for p in raw["correlation_prefixes"]))
    return ExpressLane(prefixes=prefixes, slots=int(raw["slots"]))


def work_item_inbox_cap(trigger_config: dict | None) -> int:
    """How many items a ``work_item`` inbox runs at once.

    ``trigger_config.max_concurrent_runs`` when it is a whole number, clamped
    to 1..``WORK_ITEM_MAX_CONCURRENT_RUNS``; 1 (the serial inbox) otherwise.
    Unlike a delivery's cap, the default is never the platform-wide
    ``ASSIGNMENT_MAX_CONCURRENT_RUNS``: an inbox runs in parallel only when its
    author asked for it.
    """
    raw = trigger_config.get(MAX_CONCURRENT_RUNS_KEY) if isinstance(trigger_config, dict) else None
    if raw is None or isinstance(raw, bool):
        return 1
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(WORK_ITEM_MAX_CONCURRENT_RUNS, value))


def preload_tool_groups(trigger_config: dict | None) -> list[str]:
    """The tool groups an assignment's runs load before their first request.

    ``trigger_config.preload_tool_groups``: a list of group ids (or one id)
    the agent holds. Revealing them up front keeps the tool list, the head of
    the provider's cached prompt prefix, stable for the whole run, where a
    mid-run ``load_tools`` invalidates the cached prefix. Anything that is not
    a non-empty string is dropped; absent = ``[]`` (today's behaviour). The
    gate resolves the ids over the agent's own groups, so an id the agent does
    not hold preloads nothing.
    """
    raw = trigger_config.get(PRELOAD_TOOL_GROUPS_KEY) if isinstance(trigger_config, dict) else None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip() and item.strip() not in out:
            out.append(item.strip())
    return out


def concurrency_cap(trigger_type: str | None, trigger_config: dict | None) -> int:
    """An assignment's concurrency cap, read the way its trigger type reads it."""
    if trigger_type == "work_item":
        return work_item_inbox_cap(trigger_config)
    return assignment_concurrency_cap(trigger_config)


def ordering_key_path(trigger_config: dict | None) -> str | None:
    """The payload path of the ordering key: configured, else the trigger's default."""
    if not isinstance(trigger_config, dict):
        return None
    raw = trigger_config.get(ORDERING_KEY_CONFIG)
    if raw is None:
        event = trigger_config.get("event") or trigger_config.get("event_type")
        return DEFAULT_ORDERING_KEYS.get((str(trigger_config.get("provider") or ""), str(event or "")))
    if not isinstance(raw, str):
        return None
    return raw.strip()[:_ORDERING_PATH_MAX_CHARS] or None


def resolve_ordering_key(trigger_config: dict | None, payload: Any) -> str | None:
    """The ordering key of one delivery, or ``None`` (the run is unordered).

    The configured dotted path is read through dicts and list indexes; only a
    scalar value is a key. A value longer than the stored bound is replaced by
    its SHA-256, so two long values never collide on a shared prefix.
    """
    path = ordering_key_path(trigger_config)
    if path is None:
        configured = isinstance(trigger_config, dict) and trigger_config.get(ORDERING_KEY_CONFIG) is not None
        if configured or not isinstance(payload, dict) or not payload.get(CONVERSATION_KEY_FIELD):
            return None
        path = CONVERSATION_KEY_FIELD
    value: Any = payload
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return None
    if value is None or isinstance(value, (dict, list)):
        return None
    text = (str(value).lower() if isinstance(value, bool) else str(value)).strip()
    if not text:
        return None
    if len(text) > _ORDERING_KEY_MAX_CHARS:
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text


def with_ordering_key(trigger_config: dict | None, payload: Any, trigger_context: dict | None) -> dict | None:
    """``trigger_context`` carrying the delivery's ordering key, when it has one."""
    key = resolve_ordering_key(trigger_config, payload)
    if key is None:
        return trigger_context
    return {**(trigger_context or {}), ORDERING_KEY_CONTEXT: key}


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QueueState:
    """One assignment's queue as the drain reads it.

    ``active`` counts the runs holding a slot. ``held``: one of them waits for
    a human while ``ASSIGNMENT_QUEUE_HOLD_ON_AWAITING_HUMAN`` is on, which holds
    the whole queue until it is answered, whatever the cap. ``exclusive``: one
    of them is not a delivery, and has the assignment to itself.

    ``startable``: the queued runs that could start now - every unordered one
    and the head of each key no run in flight holds (``startable_runs``);
    ``None`` counts the whole ``depth``. A queue whose keys are all in flight
    takes no share of the drain's capacity.
    """

    assignment_id: uuid.UUID
    agent_id: uuid.UUID | None
    depth: int
    oldest_at: datetime
    active: int
    cap: int
    held: bool = False
    exclusive: bool = False
    startable: int | None = None

    @property
    def free(self) -> int:
        if self.held or self.exclusive:
            return 0
        waiting = self.depth if self.startable is None else self.startable
        return max(0, min(self.cap - self.active, waiting))


def plan_promotions(states: Iterable[QueueState], *, capacity: int, max_assignments: int) -> dict[uuid.UUID, int]:
    """How many queued runs each assignment promotes in this drain.

    Only assignments with a queued run and a free slot take part; at most
    ``max_assignments`` of them, those waiting longest first. Slots are handed
    out one at a time, round-robin over agents (the agent whose delivery has
    waited longest first) and, within an agent, over its assignments, until
    ``capacity`` runs are planned or none can take another. A busy agent with
    many assignments therefore never takes every slot from the others.
    """
    eligible = sorted(
        (s for s in states if s.free > 0),
        key=lambda s: (s.oldest_at, str(s.assignment_id)),
    )[: max(1, max_assignments)]
    by_agent: dict[str, deque[QueueState]] = {}
    for state in eligible:
        by_agent.setdefault(str(state.agent_id), deque()).append(state)
    remaining = {s.assignment_id: s.free for s in eligible}
    plan: dict[uuid.UUID, int] = {}
    left = max(0, capacity)
    while left > 0 and by_agent:
        for agent_key in list(by_agent):
            rotation = by_agent[agent_key]
            state = rotation.popleft()
            plan[state.assignment_id] = plan.get(state.assignment_id, 0) + 1
            remaining[state.assignment_id] -= 1
            left -= 1
            if remaining[state.assignment_id] > 0:
                rotation.append(state)
            if not rotation:
                del by_agent[agent_key]
            if left == 0:
                break
    return plan


@dataclass(frozen=True)
class QueuedRun:
    run_id: uuid.UUID
    created_at: datetime
    ordering_key: str | None = None


def startable_runs(queued: list[QueuedRun], busy_keys: set[str]) -> list[QueuedRun]:
    """The queued runs that may start now, oldest first.

    An unordered run always may; an ordered one only when it is the oldest
    queued run of its key and no run of that key is in flight.
    """
    seen: set[str] = set()
    out: list[QueuedRun] = []
    for run in queued:
        key = run.ordering_key
        if key is None:
            out.append(run)
            continue
        if key in seen:
            continue
        seen.add(key)
        if key not in busy_keys:
            out.append(run)
    return out


def select_runs(
    queued: list[QueuedRun],
    count: int,
    *,
    busy_keys: set[str],
    backlog: bool = False,
    newest_turn: bool = False,
) -> tuple[list[QueuedRun], bool]:
    """Which of ``queued`` (oldest first) to start, and whose turn is next.

    Oldest first. In backlog, oldest and newest alternate, starting with the
    newest when ``newest_turn``; the returned flag says whose turn follows.
    Ordered runs keep their order: only the head of a key is ever a candidate.

    Linear: starting a key's head leaves that key busy, so no run behind it
    becomes a candidate, and the candidates are read once.
    """
    candidates = deque(startable_runs(queued, set(busy_keys)))
    chosen: list[QueuedRun] = []
    while candidates and len(chosen) < count:
        chosen.append(candidates.pop() if backlog and newest_turn else candidates.popleft())
        if backlog:
            newest_turn = not newest_turn
    return chosen, newest_turn


def throttled_cap(cap: int) -> int:
    """The cap an assignment backs off to while a provider rate-limits it."""
    return max(1, cap // 2)


# ---------------------------------------------------------------------------
# Fair share of the worker pool across fleets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimCandidate:
    """One due assignment execution as the fair-share deal reads it.

    ``group``: the agent's home fleet, else the agent itself (an agent outside
    any fleet is its own group). ``child_of_live_parent``: a delegated run whose
    parent run still holds a slot, waiting for it. ``interactive``: a run a
    person's answer or a work item started (a gate answered, a released or
    dispatched work item), which never queues behind bulk event work (#2234).
    ``assignment``: the assignment the run belongs to; the assignments of one
    group take turns, so one event-driven assignment's burst never holds back
    another assignment of the same agent or fleet by more than one turn.
    """

    key: Any
    group: str
    agent: str
    run_at: datetime
    child_of_live_parent: bool = False
    interactive: bool = False
    assignment: str = ""


def share_cap(pool: int, share: float) -> int:
    """The slots one group (or agent) may hold while another waits: at least 1, at most the pool."""
    pool = max(1, int(pool))
    if share <= 0 or share >= 1:
        return pool
    return max(1, min(pool, math.ceil(pool * share)))


def fair_share_order(
    candidates: Iterable[ClaimCandidate],
    *,
    running_by_group: dict[str, int],
    running_by_agent: dict[str, int],
    pool: int,
    fleet_share: float,
    agent_share: float,
) -> list[Any]:
    """The order a free slot claims the due *candidates* in (their keys).

    1. Delegated children of a live parent first, oldest first: the parent holds
       a slot until its child ends, so the child never queues behind the
       backlog its parent's slot is part of.
    2. Then the interactive runs (``interactive``: a gate answered, a work item
       released or dispatched), oldest first: a person's answer never waits
       behind a burst of bulk event runs of its own agent (#2234).
    3. Then one candidate per group in turn, the group whose oldest due run is
       oldest first. Inside a group the assignments take turns, the assignment
       whose oldest due run is oldest first, each assignment's runs oldest
       first: a storm of one fleet never delays another fleet's run by more
       than one slot, and a storm of one assignment never delays another
       assignment of the same group by more than one turn of that group.
    4. A group already holding its share of the pool (``fleet_share``), or a
       run whose agent holds its share (``agent_share``), goes after every
       group still under its share, in the same turn order: work-conserving, an
       otherwise idle pool still runs it.

    Pure and linear in the candidates; claims taken in this order count against
    the shares as they are dealt.
    """
    items = sorted(candidates, key=lambda c: (c.run_at, str(c.key)))
    first = [c for c in items if c.child_of_live_parent]
    first += [c for c in items if c.interactive and not c.child_of_live_parent]
    rest = [c for c in items if not c.child_of_live_parent and not c.interactive]
    group_cap = share_cap(pool, fleet_share)
    agent_cap = share_cap(pool, agent_share)
    held_group = dict(running_by_group)
    held_agent = dict(running_by_agent)
    for child in first:
        held_group[child.group] = held_group.get(child.group, 0) + 1
        held_agent[child.agent] = held_agent.get(child.agent, 0) + 1

    # group -> its assignments in turn order -> each assignment's runs, oldest first.
    queues: dict[str, dict[str, deque[ClaimCandidate]]] = {}
    for candidate in rest:
        queues.setdefault(candidate.group, {}).setdefault(candidate.assignment, deque()).append(candidate)
    turns: dict[str, deque[str]] = {group: deque(by_assignment) for group, by_assignment in queues.items()}
    groups = deque(queues)
    under: list[ClaimCandidate] = []
    over: list[ClaimCandidate] = []
    while groups:
        group = groups.popleft()
        assignment = turns[group].popleft()
        queue = queues[group][assignment]
        candidate = queue.popleft()
        if held_group.get(group, 0) >= group_cap or held_agent.get(candidate.agent, 0) >= agent_cap:
            over.append(candidate)
        else:
            under.append(candidate)
            held_group[group] = held_group.get(group, 0) + 1
            held_agent[candidate.agent] = held_agent.get(candidate.agent, 0) + 1
        if queue:
            turns[group].append(assignment)
        if turns[group]:
            groups.append(group)
    return [c.key for c in (*first, *under, *over)]


# ---------------------------------------------------------------------------
# Rate-limit back-off
# ---------------------------------------------------------------------------

_ASSIGNMENT_THROTTLE_PREFIX = "assignment_dispatch:throttled:"
# A delivery decision reads its assignment's back-off at most this often per
# process, so a poll page dispatching many events reads it once.
_THROTTLE_CACHE_SECONDS = 2.0
_THROTTLE_CACHE_MAX = 1024

_current_assignment: ContextVar[str | None] = ContextVar("assignment_dispatch_assignment", default=None)
_throttle_cache: dict[uuid.UUID, tuple[float, bool]] = {}


async def _redis():
    from app.core.redis import redis_client

    return await redis_client()


@contextlib.contextmanager
def dispatch_scope(assignment_id: object) -> Iterator[None]:
    """Attribute the rate limits met inside the block to this assignment's runs."""
    token = _current_assignment.set(str(assignment_id) if assignment_id else None)
    try:
        yield
    finally:
        _current_assignment.reset(token)


async def note_rate_limited(source: str) -> None:
    """Record a provider's rate limit (a 429, or the AI Gateway's throttle) met by an assignment's run. Never raises.

    Only a rate limit met inside ``dispatch_scope`` counts, model provider
    (``source="llm"``) or integration alike, and it backs off that assignment
    alone: one met in a chat, an evaluation or an embedding - on a user's own
    key as often as not - throttles nothing. The window is fixed from
    the first one: the retries that follow do not extend it.
    """
    seconds = int(settings.assignment_dispatch_rate_limit_backoff_seconds)
    assignment_id = _current_assignment.get()
    if seconds <= 0 or not assignment_id:
        return
    try:
        redis = await _redis()
        if await redis.set(_ASSIGNMENT_THROTTLE_PREFIX + assignment_id, "1", ex=seconds, nx=True):
            logger.info(
                "Assignment dispatch backing off for %ds: %s rate limit on assignment %s",
                seconds,
                source,
                assignment_id,
            )
    except Exception as exc:
        logger.debug("Could not record a %s rate limit for dispatch: %s", source, format_exception_message(exc))
    with contextlib.suppress(ValueError):
        _throttle_cache[uuid.UUID(assignment_id)] = (time.monotonic() + _THROTTLE_CACHE_SECONDS, True)


async def read_throttles(assignment_ids: Iterable[uuid.UUID]) -> set[uuid.UUID]:
    """The assignments backing off. Never raises: unreadable, none does."""
    ids = list(assignment_ids)
    if not ids:
        return set()
    try:
        redis = await _redis()
        values = await redis.mget([_ASSIGNMENT_THROTTLE_PREFIX + str(i) for i in ids])
    except Exception as exc:
        logger.debug("Could not read the dispatch back-off: %s", format_exception_message(exc))
        return set()
    return {i for i, value in zip(ids, values or [], strict=False) if value}


async def is_throttled(assignment_id: uuid.UUID) -> bool:
    """Whether one assignment backs off, read at most every couple of seconds."""
    now = time.monotonic()
    cached = _throttle_cache.get(assignment_id)
    if cached is not None and cached[0] > now:
        return cached[1]
    throttled = assignment_id in await read_throttles([assignment_id])
    if len(_throttle_cache) >= _THROTTLE_CACHE_MAX:
        for key in [k for k, (until, _) in _throttle_cache.items() if until <= now]:
            del _throttle_cache[key]
        if len(_throttle_cache) >= _THROTTLE_CACHE_MAX:
            _throttle_cache.clear()
    _throttle_cache[assignment_id] = (now + _THROTTLE_CACHE_SECONDS, throttled)
    return throttled


# ---------------------------------------------------------------------------
# /metrics
# ---------------------------------------------------------------------------

_LAG_WINDOW_SECONDS = 900


async def read_dispatch_stats(db) -> dict[str, Any]:
    """Per-assignment queue depth, oldest age, active runs, cap and dispatch lag.

    Lag is ``started_at - created_at`` of the runs created in the last 15
    minutes, p95 per assignment, leaving out the waits that are configured
    rather than suffered: a scheduled run's spread, an assignment's own
    ``delay``, a retried run, a run resumed after a human answered.

    ``held``: the queue waits for a human's answer
    (``ASSIGNMENT_QUEUE_HOLD_ON_AWAITING_HUMAN``). Such a queue is reported,
    and left out of the platform-wide oldest age the lag alert reads.
    """
    from datetime import UTC, timedelta

    from sqlalchemy import func, or_, select

    from app.models.assignment import Assignment, AssignmentRun
    from app.services.assignment_engine import QUEUED_RUN_STATUS, _queue_blocking_statuses
    from app.services.decision_waits import holds_assignment

    now = datetime.now(UTC)
    hold_on_approval = bool(settings.assignment_queue_hold_on_awaiting_human)
    queued = (
        await db.execute(
            select(AssignmentRun.assignment_id, func.count(), func.min(AssignmentRun.created_at))
            .where(AssignmentRun.status == QUEUED_RUN_STATUS, AssignmentRun.assignment_id.is_not(None))
            .group_by(AssignmentRun.assignment_id)
        )
    ).all()
    active = (
        await db.execute(
            select(
                AssignmentRun.assignment_id,
                func.count(),
                func.count().filter(AssignmentRun.status == "awaiting_human"),
            )
            .where(
                holds_assignment(AssignmentRun, _queue_blocking_statuses()),
                AssignmentRun.assignment_id.is_not(None),
            )
            .group_by(AssignmentRun.assignment_id)
        )
    ).all()
    lag_seconds = func.extract("epoch", AssignmentRun.started_at - AssignmentRun.created_at)
    lag = (
        await db.execute(
            select(
                AssignmentRun.assignment_id,
                func.percentile_cont(0.95).within_group(lag_seconds),
                func.count(),
            )
            .join(Assignment, Assignment.id == AssignmentRun.assignment_id)
            .where(
                AssignmentRun.created_at >= now - timedelta(seconds=_LAG_WINDOW_SECONDS),
                AssignmentRun.started_at.is_not(None),
                AssignmentRun.retry_count == 0,
                AssignmentRun.human_input_question.is_(None),
                or_(Assignment.delay.is_(None), Assignment.delay == ""),
                or_(
                    AssignmentRun.trigger_context.is_(None),
                    AssignmentRun.trigger_context["type"].as_string().is_(None),
                    AssignmentRun.trigger_context["type"].as_string() != "schedule",
                ),
            )
            .group_by(AssignmentRun.assignment_id)
        )
    ).all()
    ids = {row[0] for row in (*queued, *active, *lag)}
    triggers: dict[uuid.UUID, tuple[Any, Any]] = {}
    if ids:
        triggers = {
            row[0]: (row[1], row[2])
            for row in (
                await db.execute(
                    select(Assignment.id, Assignment.trigger_type, Assignment.trigger_config).where(
                        Assignment.id.in_(ids)
                    )
                )
            ).all()
        }
    # No assignment name: a private assignment's name must not leave its owner's
    # scope through /metrics, and a label must not grow with free text.
    per: dict[uuid.UUID, dict[str, Any]] = {}
    for assignment_id in ids:
        per[assignment_id] = {
            "depth": 0,
            "oldest_age_seconds": 0.0,
            "active": 0,
            "cap": concurrency_cap(*triggers.get(assignment_id, (None, None))),
            "held": False,
            "lag_p95_seconds": None,
        }
    for assignment_id, depth, oldest in queued:
        per[assignment_id]["depth"] = int(depth)
        if oldest is not None:
            oldest = oldest if oldest.tzinfo is not None else oldest.replace(tzinfo=UTC)
            per[assignment_id]["oldest_age_seconds"] = max(0.0, (now - oldest).total_seconds())
    for assignment_id, count, awaiting_human in active:
        per[assignment_id]["active"] = int(count)
        per[assignment_id]["held"] = hold_on_approval and int(awaiting_human) > 0
    for assignment_id, p95, _count in lag:
        per[assignment_id]["lag_p95_seconds"] = float(p95) if p95 is not None else None
    return {
        "assignments": per,
        "oldest_age_seconds": max(
            (v["oldest_age_seconds"] for v in per.values() if not v["held"]),
            default=0.0,
        ),
        "queued": sum(v["depth"] for v in per.values()),
    }
