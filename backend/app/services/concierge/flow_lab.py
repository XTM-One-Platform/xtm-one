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

"""The Flow Lab's diagnostics: what its four modes read before experimenting (#8260).

The Flow Lab (PRD ``platform-concierge.md`` section 6.9, decision B14) repairs
failing flows, optimizes them, proposes new ones from observed usage and tests
replacing an assignment with a flow. Every experiment runs on a sandbox copy
(level 2) and is measured by the Experimenter (level 6); this module is the
read side the modes start from:

* **repair** - :func:`diagnose_flow`: a flow's failed runs, the nodes they
  failed on, and their trigger payloads as the cases a corrected graph is
  replayed on;
* **optimize** - the same diagnosis' per-node cost and duration, the cases
  being the flow's completed runs;
* **new flow** - :func:`repeated_sequences`: tool-call sequences recurring
  across the conversations of different users;
* **agent to flow** - :func:`assignment_path`: whether an assignment's runs
  follow one stable path, and whether it may later be switched over to a flow.

Nothing here calls a model or writes. Counts, statuses, tool names, costs and
durations are rollups, readable on every target of the supervision scope; a
trigger payload or a step output is content, returned only when the caller's
``content_allowed`` predicate (consent, level 1) admits that run, checked per
run at read time.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils import normalize_correlation_key, safe_uuid, schedule_timezone
from app.models.assignment import Assignment, AssignmentRun
from app.models.flow import Flow
from app.models.flow_run import (
    FLOW_RUN_STATUS_COMPLETED,
    FLOW_RUN_STATUS_FAILED,
    FlowRun,
    FlowRunStep,
)
from app.models.work_item import WORK_ITEM_PRIORITIES

#: How many characters of an error or a payload a diagnosis carries: enough to
#: read the cause, bounded so a hostile or huge value cannot flood the context.
ERROR_EXCERPT_CHARS = 300
PAYLOAD_EXCERPT_CHARS = 2000

#: An assignment's path is "stable" when this share of its completed runs made
#: the same tool-call sequence, over at least ``MIN_PATH_RUNS`` runs.
STABLE_PATH_SHARE = 0.8
MIN_PATH_RUNS = 10

#: The n-gram lengths :func:`repeated_sequences` mines: one call is not a
#: workflow, and past six the same sequence rarely repeats verbatim.
MIN_SEQUENCE_LENGTH = 2
MAX_SEQUENCE_LENGTH = 6

#: Assignment trigger types a flow can take over without anyone outside the
#: platform noticing: the flow's trigger node fires on the same schedule,
#: integration events or work items.
SWITCHABLE_TRIGGER_TYPES = frozenset({"schedule", "integration_event", "work_item"})

ContentAllowed = Callable[[Any], Awaitable[bool]]


def excerpt(value: Any, limit: int) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _percentile(values: Sequence[float], pct: float) -> float | None:
    """The nearest-rank percentile of *values*, ``None`` when there is none."""
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return None
    rank = max(1, round(pct / 100 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


# ── Repair and optimize ────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class NodeStats:
    """One node's record over the diagnosed runs."""

    node_id: str
    node_kind: str
    executions: int
    failures: int
    #: Distinct error excerpts, most frequent first (at most three).
    errors: tuple[str, ...]
    duration_p50_ms: float | None
    duration_p95_ms: float | None


@dataclass(frozen=True, slots=True)
class ReplayCase:
    """A past run whose trigger a sandbox graph is replayed on."""

    run_id: uuid.UUID
    status: str
    trigger_type: str
    #: ``None`` when consent does not admit this run's content: the case is
    #: still counted, never replayed.
    trigger_payload: dict | None


@dataclass(frozen=True, slots=True)
class FlowDiagnosis:
    flow_id: uuid.UUID
    flow_name: str
    version_number: int
    runs: int
    failed: int
    completed: int
    nodes: tuple[NodeStats, ...]
    #: Failed runs first, then completed ones; each mode picks its own.
    cases: tuple[ReplayCase, ...]
    #: Cases whose content consent withheld (counted, not listed with a payload).
    withheld: int

    @property
    def failure_rate(self) -> float | None:
        return self.failed / self.runs if self.runs else None


def node_stats(steps: Iterable[Any], *, error_run_ids: set[Any] | None = None) -> tuple[NodeStats, ...]:
    """Per-node executions, failures, top errors and duration percentiles.

    An error text is content: with *error_run_ids*, only the errors of those
    runs are kept (the failures of the others are still counted).
    """
    by_node: dict[str, dict[str, Any]] = {}
    for step in steps:
        entry = by_node.setdefault(
            step.node_id,
            {"kind": step.node_kind, "executions": 0, "failures": 0, "errors": Counter(), "durations": []},
        )
        entry["executions"] += 1
        if step.status == "failed":
            entry["failures"] += 1
            if step.error and (error_run_ids is None or step.flow_run_id in error_run_ids):
                entry["errors"][excerpt(str(step.error), ERROR_EXCERPT_CHARS)] += 1
        if step.started_at and step.completed_at:
            entry["durations"].append((step.completed_at - step.started_at).total_seconds() * 1000)
    return tuple(
        NodeStats(
            node_id=node_id,
            node_kind=entry["kind"],
            executions=entry["executions"],
            failures=entry["failures"],
            errors=tuple(error for error, _ in entry["errors"].most_common(3)),
            duration_p50_ms=_percentile(entry["durations"], 50),
            duration_p95_ms=_percentile(entry["durations"], 95),
        )
        for node_id, entry in sorted(by_node.items())
    )


async def diagnose_flow(
    db: AsyncSession,
    flow: Flow,
    *,
    content_allowed: ContentAllowed,
    last_runs: int = 50,
) -> FlowDiagnosis:
    """The diagnosis of *flow* over its last *last_runs* settled runs.

    The caller has already checked that its principal may experiment on
    *flow*; *content_allowed* is asked per run before a trigger payload is
    included.
    """
    runs = list(
        (
            await db.execute(
                select(FlowRun)
                .where(
                    FlowRun.flow_id == flow.id,
                    FlowRun.status.in_((FLOW_RUN_STATUS_COMPLETED, FLOW_RUN_STATUS_FAILED)),
                    # A child run belongs to its caller's diagnosis.
                    FlowRun.parent_run_id.is_(None),
                )
                .order_by(FlowRun.created_at.desc())
                .limit(max(1, min(last_runs, 200)))
            )
        ).scalars()
    )
    steps = (
        list(
            (
                await db.execute(select(FlowRunStep).where(FlowRunStep.flow_run_id.in_([r.id for r in runs])))
            ).scalars()
        )
        if runs
        else []
    )
    cases: list[ReplayCase] = []
    allowed_ids: set[uuid.UUID] = set()
    for run in sorted(runs, key=lambda r: (r.status != FLOW_RUN_STATUS_FAILED, r.created_at)):
        allowed = await content_allowed(run)
        if allowed:
            allowed_ids.add(run.id)
        cases.append(
            ReplayCase(
                run_id=run.id,
                status=run.status,
                trigger_type=run.trigger_type,
                trigger_payload=(run.trigger_payload or {}) if allowed else None,
            )
        )
    failed = sum(1 for r in runs if r.status == FLOW_RUN_STATUS_FAILED)
    return FlowDiagnosis(
        flow_id=flow.id,
        flow_name=flow.name,
        version_number=flow.version_number,
        runs=len(runs),
        failed=failed,
        completed=len(runs) - failed,
        nodes=node_stats(steps, error_run_ids=allowed_ids),
        cases=tuple(cases),
        withheld=len(runs) - len(allowed_ids),
    )


# ── Agent to flow ──────────────────────────────────────────────────────────


def tool_sequence(actions: Any) -> tuple[str, ...]:
    """The tool names an ``AssignmentRun.actions_taken`` value called, in order.

    Names only: arguments are content and play no part in what a path is.
    Anything that is not a list of ``{"tool": name}`` entries reads as no call.
    """
    if not isinstance(actions, list):
        return ()
    return tuple(
        str(action["tool"])[:255]
        for action in actions
        if isinstance(action, dict) and isinstance(action.get("tool"), str) and action["tool"].strip()
    )


@dataclass(frozen=True, slots=True)
class AssignmentPath:
    assignment_id: uuid.UUID
    assignment_name: str
    trigger_type: str
    runs: int
    #: The most frequent tool-call sequence and the share of runs that made it.
    path: tuple[str, ...]
    share: float | None
    stable: bool
    #: Whether an approved replacement may later be switched over (B12).
    switchable: bool
    #: Why not, when ``stable`` or ``switchable`` is false.
    reasons: tuple[str, ...] = field(default=())


def switch_over_refusal(trigger_type: str) -> str | None:
    """Why an assignment with *trigger_type* cannot be switched over to a flow."""
    if trigger_type in SWITCHABLE_TRIGGER_TYPES:
        return None
    if trigger_type == "webhook":
        return (
            "a webhook assignment cannot be switched over: the flow would get a new "
            "delivery URL and secret, so the sender would stop reaching it"
        )
    if trigger_type == "manual":
        return "a manual assignment has no live trigger to shadow or take over"
    return f"trigger type {trigger_type!r} has no flow trigger to take it over"


def assignment_switch_over_refusal(assignment: Any) -> str | None:
    """Why *assignment* cannot be switched over to a flow, whatever the flow."""
    if getattr(assignment, "run_as_all_users", False):
        # One run per user, each acting as that user: one flow run acting as
        # one identity would silently drop everyone else's.
        return "a per-user assignment (one run for each user) cannot be replaced by a single flow"
    return switch_over_refusal(assignment.trigger_type)


def modal_path(sequences: Sequence[tuple[str, ...]]) -> tuple[tuple[str, ...], float | None]:
    """The most frequent sequence and its share; ties go to the shorter one."""
    if not sequences:
        return (), None
    counts = Counter(sequences)
    best = min(counts.items(), key=lambda item: (-item[1], len(item[0]), item[0]))
    return best[0], best[1] / len(sequences)


def path_verdict(
    assignment: Assignment, sequences: Sequence[tuple[str, ...]]
) -> AssignmentPath:
    path, share = modal_path(sequences)
    reasons: list[str] = []
    if len(sequences) < MIN_PATH_RUNS:
        reasons.append(f"only {len(sequences)} completed runs, {MIN_PATH_RUNS} needed")
    elif share is None or share < STABLE_PATH_SHARE:
        reasons.append(
            f"the most frequent path covers {round((share or 0) * 100)} % of the runs, "
            f"{round(STABLE_PATH_SHARE * 100)} % needed"
        )
    elif not path:
        reasons.append("the runs call no tool, so there is no path to reproduce")
    stable = not reasons
    refusal = assignment_switch_over_refusal(assignment)
    if refusal:
        reasons.append(refusal)
    return AssignmentPath(
        assignment_id=assignment.id,
        assignment_name=assignment.name,
        trigger_type=assignment.trigger_type,
        runs=len(sequences),
        path=path,
        share=share,
        stable=stable,
        switchable=stable and refusal is None,
        reasons=tuple(reasons),
    )


async def assignment_path(
    db: AsyncSession, assignment: Assignment, *, last_runs: int = 30
) -> AssignmentPath:
    """Whether *assignment*'s last completed runs follow one stable path."""
    rows = (
        await db.execute(
            select(AssignmentRun.actions_taken)
            .where(
                AssignmentRun.assignment_id == assignment.id,
                AssignmentRun.status == "completed",
                # A delegated sub-run is its parent's path, not this one's.
                AssignmentRun.parent_run_id.is_(None),
            )
            .order_by(AssignmentRun.created_at.desc())
            .limit(max(1, min(last_runs, 200)))
        )
    ).scalars()
    return path_verdict(assignment, [tool_sequence(actions) for actions in rows])


def flow_trigger_for_assignment_run(
    trigger_type: str, payload: Any, context: Any
) -> tuple[str, dict] | None:
    """The flow trigger (type, payload) a flow run gets for an assignment run's event.

    Shaped as ``services/flow_triggers.py`` builds it for the same event, so a
    flow's ``{{trigger.*}}`` references resolve in a shadow run exactly as they
    will once the flow takes the trigger over. ``None`` for a trigger with no
    flow counterpart (manual).
    """
    payload = payload if isinstance(payload, dict) else {}
    context = context if isinstance(context, dict) else {}
    if trigger_type == "schedule":
        fired_at = payload.get("scheduled_at") or context.get("scheduled_at") or datetime.now(UTC).isoformat()
        return "schedule", {"fired_at": str(fired_at)}
    if trigger_type == "integration_event":
        return "integration_event", {"payload": payload, "event_type": str(context.get("event") or "")}
    if trigger_type == "work_item":
        item = payload.get("work_item") if isinstance(payload.get("work_item"), dict) else {}
        return "work_item", {
            "payload": item,
            "work_item_id": str(payload.get("work_item_id") or item.get("id") or ""),
            "title": item.get("title") or "",
            "description": item.get("description") or "",
        }
    if trigger_type == "webhook":
        # Measurement only (``switch_over_refusal``). The delivery headers stay
        # out: they may carry the sender's credentials.
        return "webhook", {"payload": payload, "headers": {}}
    return None


def trigger_takeover_refusal(assignment: Any, agent: Any, graph: Any) -> str | None:
    """Why *graph*'s triggers cannot take *assignment*'s over (``None`` when they can).

    The flow must fire on exactly what the assignment fires on, and on
    nothing more: one automatic trigger, of the matching kind, on the same
    schedule, integration and event. A manual or chat trigger beside it is
    fine (it starts nothing by itself).
    """
    from app.services.flow_runner import find_trigger_nodes
    from app.services.trigger_integration_access import trigger_integration_id

    refusal = assignment_switch_over_refusal(assignment)
    if refusal:
        return refusal
    automatic = [
        node
        for node in find_trigger_nodes(graph if isinstance(graph, dict) else {})
        if (node.get("kind") or "") not in ("trigger.manual", "trigger.chat")
    ]
    wanted = f"trigger.{assignment.trigger_type}"
    if len(automatic) != 1 or automatic[0].get("kind") != wanted:
        return f"the flow needs exactly one automatic trigger, a {wanted} node"
    node_config = automatic[0].get("config") if isinstance(automatic[0].get("config"), dict) else {}
    config = assignment.trigger_config if isinstance(assignment.trigger_config, dict) else {}
    if assignment.trigger_type == "schedule":
        if " ".join(str(node_config.get("cron") or "").split()) != " ".join(str(config.get("cron") or "").split()):
            return "the flow's schedule differs from the assignment's"
        # An assignment reads an unknown zone as UTC, a flow node refuses to
        # fire on it: only a recognised, equal zone fires at the same instants.
        node_zone, node_warning = schedule_timezone(node_config)
        if node_warning or str(node_zone) != str(schedule_timezone(config)[0]):
            return "the flow's schedule timezone differs from the assignment's"
    elif assignment.trigger_type == "integration_event":
        if str(node_config.get("integration_id") or "") != str(trigger_integration_id(config, agent) or ""):
            return "the flow listens to another integration than the assignment"
        if str(node_config.get("event_type") or "").strip() != str(config.get("event") or "").strip():
            return "the flow listens to another event than the assignment"
        # The two filter languages differ: even an identical predicate may
        # select other events, so only an unfiltered trigger is taken over.
        if config.get("filter") or node_config.get("filter"):
            return "the flow or the assignment filters events: their filter languages differ"
    elif assignment.trigger_type == "work_item":
        # Read as ``work_item.assignment_inbox`` / ``flow_triggers.flow_work_item_inbox`` do.
        node_owner = safe_uuid(str(node_config.get("owner_agent_id") or "").strip())
        if node_owner is None or node_owner != safe_uuid(str(config.get("owner_agent_id") or assignment.agent_id)):
            return "the flow drains another agent's work items than the assignment"
        if normalize_correlation_key(str(node_config.get("correlation_prefix") or "")) != normalize_correlation_key(
            str(config.get("correlation_prefix") or "")
        ):
            return "the flow takes work items of another correlation prefix than the assignment"
        if _priority_floor(str(node_config.get("priority_min") or "").strip()) != _priority_floor(
            config.get("priority_min")
        ):
            return "the flow takes work items of another minimum priority than the assignment"
    return None


def _priority_floor(value: Any) -> str | None:
    """The ``priority_min`` an inbox applies: an unknown value filters nothing."""
    return value if value in WORK_ITEM_PRIORITIES else None


# ── New flow ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SequenceCandidate:
    sequence: tuple[str, ...]
    #: Distinct conversations and distinct users the sequence occurred in.
    conversations: int
    users: int


def repeated_ngrams(
    sequences: Iterable[tuple[Any, tuple[str, ...]]],
    *,
    min_occurrences: int = 5,
    min_users: int = 2,
    limit: int = 10,
) -> list[SequenceCandidate]:
    """Contiguous tool-name sequences recurring across conversations.

    *sequences* yields ``(user_id, tool_names)`` per conversation. A sequence
    counts once per conversation; a candidate needs *min_occurrences*
    conversations and *min_users* distinct users, so one person's habit is not
    a platform workflow. A candidate contained in a longer one with the same
    support is dropped: the longer one says more.
    """
    conversations: Counter[tuple[str, ...]] = Counter()
    users: dict[tuple[str, ...], set[Any]] = {}
    for user_id, names in sequences:
        seen: set[tuple[str, ...]] = set()
        for length in range(MIN_SEQUENCE_LENGTH, MAX_SEQUENCE_LENGTH + 1):
            for start in range(len(names) - length + 1):
                gram = tuple(names[start : start + length])
                # A run of one repeated call is a retry loop, not a workflow.
                if len(set(gram)) == 1 or gram in seen:
                    continue
                seen.add(gram)
                conversations[gram] += 1
                users.setdefault(gram, set()).add(user_id)
    kept = [
        gram for gram, count in conversations.items() if count >= min_occurrences and len(users[gram]) >= min_users
    ]

    def _contains(longer: tuple[str, ...], shorter: tuple[str, ...]) -> bool:
        n = len(shorter)
        return any(longer[i : i + n] == shorter for i in range(len(longer) - n + 1))

    maximal = [
        gram
        for gram in kept
        if not any(
            len(other) > len(gram) and conversations[other] == conversations[gram] and _contains(other, gram)
            for other in kept
        )
    ]
    maximal.sort(key=lambda g: (-conversations[g], -len(g), g))
    return [
        SequenceCandidate(sequence=gram, conversations=conversations[gram], users=len(users[gram]))
        for gram in maximal[:limit]
    ]


async def repeated_sequences(
    db: AsyncSession,
    agent_ids: Sequence[uuid.UUID],
    *,
    days: int = 30,
    min_occurrences: int = 5,
    min_users: int = 2,
    max_conversations: int = 500,
) -> list[SequenceCandidate]:
    """Tool-call sequences recurring in the recent chats of *agent_ids*.

    Reads the names of the ``TOOL`` spans of chat traces only (a rollup, no
    payload), from the newest *max_conversations* conversations of the last
    *days* days, so the cost is bounded whatever the size of the history.
    PostgreSQL keeps every trace whatever the read store
    (``tracing/store.build_trace_write_sink`` mirrors ClickHouse writes), so
    this reads it there.
    """
    from sqlalchemy import func

    from app.models.llm_trace import LLMSpan, LLMTrace

    if not agent_ids:
        return []
    since = datetime.now(UTC) - timedelta(days=max(1, min(days, 90)))
    conversations = (
        select(LLMTrace.conversation_id, func.max(LLMTrace.started_at).label("last_at"))
        .where(
            LLMTrace.agent_id.in_(list(agent_ids)),
            LLMTrace.conversation_id.is_not(None),
            LLMTrace.started_at >= since,
        )
        .group_by(LLMTrace.conversation_id)
        .order_by(func.max(LLMTrace.started_at).desc())
        .limit(max(1, min(max_conversations, 2000)))
        .subquery()
    )
    rows = (
        await db.execute(
            select(LLMTrace.conversation_id, LLMTrace.user_id, LLMSpan.name)
            .join(LLMSpan, LLMSpan.trace_id == LLMTrace.id)
            .join(conversations, conversations.c.conversation_id == LLMTrace.conversation_id)
            .where(LLMSpan.type == "TOOL", LLMTrace.started_at >= since)
            .order_by(LLMTrace.conversation_id, LLMSpan.started_at, LLMSpan.position)
        )
    ).all()
    owners: dict[uuid.UUID, Any] = {}
    names: dict[uuid.UUID, list[str]] = {}
    for row in rows:
        owners.setdefault(row.conversation_id, row.user_id)
        names.setdefault(row.conversation_id, []).append(str(row.name)[:255])
    return repeated_ngrams(
        ((owners[cid], tuple(seq)) for cid, seq in names.items()),
        min_occurrences=min_occurrences,
        min_users=min_users,
    )
