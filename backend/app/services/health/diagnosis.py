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

"""Quality signals of one supervised agent, and evidence checked on its rows (#8259).

The Quality Analyst's one aggregated read (PRD 6.5): no model call, nothing
new measured, only the platform's existing read surfaces -

* the agent's traces in the window and in the window before it, read through
  the trace read store (PostgreSQL or ClickHouse), each carrying its stored
  rollup (``tracing/run_rollup``): runs, failures and their ``failure_cause``,
  tool calls and errors per tool, tokens, cost, wall time (p50 / p90);
* its ratings: the users' thumbs and the platform reactions apart (#5530),
  criteria means, the latest negative ratings;
* its findings still open, so the analyst reuses a pattern rather than
  inventing a new one each cycle.

The listing is a bounded sample (``SAMPLE_RUNS`` per window) and says so when
it is cut. Trace ids and rating comments are conversation content: they are
served only with content access (``access.AgentAccess.content``), never from
an opted-out user, and every comment sent to the model is audited through
level 1's ``require_content_read`` first.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.core.utils import format_exception_message
from app.services.health.findings import MAX_EVIDENCE_IDS, Evidence, clip_text
from app.services.tracing.run_rollup import rollup_percentiles, stored_rollup

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.services.health.access import AgentAccess, Reader

logger = logging.getLogger(__name__)

DEFAULT_PERIOD_DAYS = 7
MAX_PERIOD_DAYS = 30
#: Traces read per window: enough for rates and percentiles, bounded on large stores.
SAMPLE_RUNS = 200
MAX_TOOLS = 10
MAX_NEGATIVE_RATINGS = 10
COMMENT_CHARS = 400
MAX_FAILED_TRACE_IDS = 10
#: Run statuses of a run that ended without its loop ever starting (#1416).
_ENDED_UNSTARTED = ("failed", "cancelled")
#: Causes of a run stopped in the queue by its deadline: ``deadline_before_start``.
_DEADLINE_CAUSES = frozenset({"engine_deadline_exceeded", "engine_never_claimed"})
_DEADLINE_PREFIX = "deadline exceeded"
#: The percentile figures served, out of ``PERCENTILE_FIGURES``.
_SERVED_FIGURES = ("wall_ms", "input_tokens", "output_tokens", "total_cost", "tool_calls", "tool_errors", "iterations")


def _viewer(reader: Reader) -> SimpleNamespace:
    """The trace store's reader for a listing hard-scoped to one agent already granted.

    The store's per-user visibility is skipped (``is_admin``) only because the
    agent was authorized before (``access.Reader.agent_access``) and the
    listing names that agent alone - the pattern of ``traces._own_trace_reader``.
    """
    user_id = reader.user.id if reader.user is not None else uuid.UUID(int=0)
    return SimpleNamespace(id=user_id, is_admin=True)


async def _list_window(
    db: AsyncSession, reader: Reader, agent_id: uuid.UUID, start: datetime, end: datetime
) -> tuple[list[Any], int]:
    from app.services.tracing.store import build_trace_read_store

    store = build_trace_read_store()
    rows, total = await store.list_traces(
        db=db,
        user=_viewer(reader),
        user_group_ids=[],
        search=None,
        conversation_id=None,
        source=None,
        agent_id=[str(agent_id)],
        model=None,
        status_filter=None,
        level=None,
        date_from=start,
        date_to=end,
        cursor=None,
        cursor_id=None,
        limit=SAMPLE_RUNS,
    )
    # A listed row is the trace and its listing columns (agent name, ...), in both stores.
    return [row[0] for row in rows], total


async def _list_unstarted(
    db: AsyncSession, agent_id: uuid.UUID, start: datetime, end: datetime, traces: list[Any]
) -> tuple[list[Any], int]:
    """The agent's runs created in the window that ended failed or cancelled before their loop started (#1416).

    Such a run (a deadline reached in the queue, a refusal before setup) writes
    no trace, so the trace listing never counts it. It is read from the run
    records, for the same agent and window: ``started_at`` and ``llm_trace_id``
    unset, and not named by a listed trace (``assignment_run_id``), so a run
    with both a record and a trace counts once. Bounded like the traces.
    """
    from sqlalchemy import func

    from app.models.assignment import AssignmentRun

    traced = {t.assignment_run_id for t in traces if getattr(t, "assignment_run_id", None) is not None}
    conditions = [
        AssignmentRun.agent_id == agent_id,
        AssignmentRun.status.in_(_ENDED_UNSTARTED),
        AssignmentRun.started_at.is_(None),
        AssignmentRun.llm_trace_id.is_(None),
        AssignmentRun.created_at >= start,
        AssignmentRun.created_at < end,
    ]
    if traced:
        conditions.append(AssignmentRun.id.not_in([uuid.UUID(str(r)) for r in traced]))
    total = int(await db.scalar(select(func.count()).select_from(AssignmentRun).where(*conditions)) or 0)
    if not total:
        return [], 0
    rows = (
        await db.execute(
            select(
                AssignmentRun.id,
                AssignmentRun.user_id,
                AssignmentRun.failure_cause,
                func.substr(AssignmentRun.error, 1, len(_DEADLINE_PREFIX)).label("error_head"),
            )
            .where(*conditions)
            .order_by(AssignmentRun.created_at.desc())
            .limit(SAMPLE_RUNS)
        )
    ).all()
    return list(rows), total


def unstarted_cause(run: Any) -> str:
    """``deadline_before_start`` for a run its deadline stopped in the queue, else ``other``."""
    if (getattr(run, "failure_cause", None) or "") in _DEADLINE_CAUSES:
        return "deadline_before_start"
    if (getattr(run, "error_head", None) or "").lower().startswith(_DEADLINE_PREFIX):
        return "deadline_before_start"
    return "other"


def _failed(trace: Any) -> bool:
    return (getattr(trace, "status", None) or "") == "error"


def _stopped_unfinished(trace: Any, unfinished: frozenset[str]) -> bool:
    """A run that ended without an error but stopped before its task was done
    (deadline, iteration cap, budget...): the stop reason the run wrote on its
    trace metadata (``agent_stop_reason``). Counted apart from clean successes."""
    if _failed(trace):
        return False
    meta = getattr(trace, "metadata_json", None)
    reason = meta.get("agent_stop_reason") if isinstance(meta, dict) else None
    return isinstance(reason, str) and reason in unfinished


def _rate(part: int, whole: int) -> float | None:
    return round(100.0 * part / whole, 1) if whole else None


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return round(sum(items) / len(items), 6) if items else None


def summarize_window(
    traces: list[Any], total: int, unstarted: list[Any] | None = None, unstarted_total: int | None = None
) -> dict[str, Any]:
    """The figures of one window from its listed traces and its runs failed before start (pure: unit-tested).

    *unstarted* are the window's runs that ended failed or cancelled before
    their loop started (:func:`_list_unstarted`), *unstarted_total* their count
    when the listing is cut: they count in ``runs`` and ``failed`` and are
    also served apart (``failed_before_start``, #1416).
    """
    unstarted = list(unstarted or [])
    unstarted_total = max(int(unstarted_total if unstarted_total is not None else len(unstarted)), len(unstarted))
    runs = len(traces) + len(unstarted)
    failed = [t for t in traces if _failed(t)]
    from app.tools.agent_loop import UNFINISHED_STOP_REASONS

    partial = [t for t in traces if _stopped_unfinished(t, UNFINISHED_STOP_REASONS)]
    partial_reasons = Counter(str(t.metadata_json["agent_stop_reason"]) for t in partial)
    causes = Counter((getattr(t, "failure_cause", None) or "unrecorded") for t in [*failed, *unstarted])
    before_start = Counter(unstarted_cause(r) for r in unstarted)
    rollups = [r for r in (stored_rollup(getattr(t, "metadata_json", None)) for t in traces) if r]
    tools: dict[str, dict[str, int]] = {}
    for rollup in rollups:
        for name, entry in (rollup.get("tools") or {}).items():
            if not isinstance(entry, dict):
                continue
            row = tools.setdefault(str(name), {"calls": 0, "errors": 0})
            row["calls"] += int(entry.get("calls") or 0)
            row["errors"] += int(entry.get("errors") or 0)
    ranked = sorted(tools.items(), key=lambda kv: (-kv[1]["errors"], -kv[1]["calls"], kv[0]))[:MAX_TOOLS]
    costs = [float(t.total_cost) for t in traces if getattr(t, "total_cost", None) is not None]
    tokens = [float((getattr(t, "input_tokens", None) or 0) + (getattr(t, "output_tokens", None) or 0)) for t in traces]
    percentiles = rollup_percentiles(rollups)
    return {
        "runs": runs,
        # The store's count of the window: above ``runs`` when the sample is cut.
        "runs_in_window": total + unstarted_total,
        "sampled": total + unstarted_total > runs,
        "failed": len(failed) + len(unstarted),
        "failure_rate_pct": _rate(len(failed) + len(unstarted), runs),
        # Runs that ended failed or cancelled before their loop started: no trace, counted above too.
        "failed_before_start": unstarted_total,
        "failed_before_start_causes": dict(before_start.most_common()),
        "failure_causes": dict(causes.most_common()),
        # Ended without an error but stopped before the task was done: not a
        # clean success (additive keys, #1420).
        "partial": len(partial),
        "partial_rate_pct": _rate(len(partial), runs),
        "partial_stop_reasons": dict(partial_reasons.most_common()),
        "tools": [
            {
                "tool": name,
                "calls": row["calls"],
                "errors": row["errors"],
                "error_rate_pct": _rate(row["errors"], row["calls"]),
            }
            for name, row in ranked
        ],
        "cost_per_run": _mean(costs),
        "tokens_per_run": _mean(tokens),
        "percentiles": {name: percentiles[name] for name in _SERVED_FIGURES if name in percentiles},
        "runs_with_rollup": len(rollups),
    }


async def window_figures(
    db: AsyncSession, reader: Reader, agent_id: uuid.UUID, *, days: int = DEFAULT_PERIOD_DAYS
) -> dict[str, Any]:
    """The figures of the agent's last *days* alone, what sizes an experiment: no rating, no content read."""
    now = datetime.now(UTC)
    start = now - timedelta(days=days)
    rows, total = await _list_window(db, reader, agent_id, start, now)
    unstarted, unstarted_total = await _list_unstarted(db, agent_id, start, now, rows)
    return summarize_window(rows, total, unstarted, unstarted_total)


def _drift(current: float | None, previous: float | None) -> float | None:
    if current is None or previous is None or previous == 0:
        return None
    return round(100.0 * (current - previous) / previous, 1)


async def _feedback(db: AsyncSession, reader: Reader, access: AgentAccess, days: int) -> dict[str, Any]:
    from app.services import annotations as annotation_service

    agent_id = access.agent.id
    users = await annotation_service.summarize_agent_annotations(db, agent_id, raters="users")
    platform = await annotation_service.summarize_agent_annotations(db, agent_id, raters="platform")
    criteria = await annotation_service.summarize_agent_criteria(db, agent_id, days=max(days, 1))
    negatives, negative_total = await annotation_service.list_agent_annotations(
        db, agent_id, rating="negative", limit=MAX_NEGATIVE_RATINGS
    )
    opted_out = reader.opted_out_user_ids
    latest: list[dict[str, Any]] = []
    for entry in negatives:
        if entry.user_id is not None and entry.user_id in opted_out:
            continue
        item: dict[str, Any] = {
            "rating_id": str(entry.id),
            "kind": entry.target_type,
            "weighted_score": round(entry.weighted_score, 3),
            "at": entry.updated_at.isoformat() if entry.updated_at else None,
        }
        if access.content and entry.comment and await _comment_readable(db, reader, access, entry):
            item["comment"] = clip_text(entry.comment, COMMENT_CHARS)
        latest.append(item)
    return {
        "users": {kind: {"positive": up, "negative": down} for kind, (up, down) in users.items() if up or down},
        "platform_reactions": {
            kind: {"positive": up, "negative": down} for kind, (up, down) in platform.items() if up or down
        },
        "criteria": [
            {
                "criterion_id": str(stats.criterion.id),
                "name": stats.criterion.name,
                "scores": stats.count,
                "mean_score": stats.mean_score,
            }
            for stats in criteria.statistics
            if stats.count
        ],
        "negative_total": negative_total,
        "latest_negative": latest,
    }


async def _comment_readable(db: AsyncSession, reader: Reader, access: AgentAccess, entry: Any) -> bool:
    """Audit a rating comment read by Platform Health before it reaches the model; ``False`` when refused.

    A person's read is their own REST-equivalent read (``get_agent_feedback``):
    nothing to audit as Platform Health.
    """
    if not reader.is_health_principal:
        return True
    from app.services.health.consent import ConsentDeniedError, require_content_read

    try:
        await require_content_read(db, kind="rating", ref_id=entry.id, agent_id=access.agent.id, user_id=entry.user_id)
    except ConsentDeniedError:
        return False
    return True


async def quality_signals(
    db: AsyncSession,
    reader: Reader,
    access: AgentAccess,
    *,
    days: int = DEFAULT_PERIOD_DAYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The signals of *access*'s agent over the last *days* and the *days* before."""
    from app.models.health_finding import STATUS_DISMISSED, STATUS_RESOLVED, HealthFinding

    days = max(1, min(int(days or DEFAULT_PERIOD_DAYS), MAX_PERIOD_DAYS))
    now = now or datetime.now(UTC)
    start = now - timedelta(days=days)
    previous_start = start - timedelta(days=days)
    agent = access.agent
    if agent is None:
        raise ValueError("Quality signals read one agent's access.")

    current_rows, current_total = await _list_window(db, reader, agent.id, start, now)
    previous_rows, previous_total = await _list_window(db, reader, agent.id, previous_start, start)
    current_unstarted, current_unstarted_total = await _list_unstarted(db, agent.id, start, now, current_rows)
    previous_unstarted, previous_unstarted_total = await _list_unstarted(
        db, agent.id, previous_start, start, previous_rows
    )
    current = summarize_window(current_rows, current_total, current_unstarted, current_unstarted_total)
    previous = summarize_window(previous_rows, previous_total, previous_unstarted, previous_unstarted_total)

    result: dict[str, Any] = {
        "agent": {"id": str(agent.id), "name": agent.name},
        "window": {"days": days, "start": start.isoformat(), "end": now.isoformat()},
        "current": current,
        "previous": {
            key: previous[key]
            for key in (
                "runs",
                "runs_in_window",
                "failed",
                "failure_rate_pct",
                "failed_before_start",
                "cost_per_run",
                "tokens_per_run",
            )
        },
        "drift_pct": {
            "cost_per_run": _drift(current["cost_per_run"], previous["cost_per_run"]),
            "failure_rate": _drift(current["failure_rate_pct"], previous["failure_rate_pct"]),
        },
        "content_readable": access.content,
    }
    if access.content:
        opted_out = {str(u) for u in reader.opted_out_user_ids}
        result["failed_trace_ids"] = [
            str(t.id) for t in current_rows if _failed(t) and str(getattr(t, "user_id", None) or "") not in opted_out
        ][:MAX_FAILED_TRACE_IDS]
        # Run ids, beside the trace ids: the runs failed before start have no trace (#1416).
        result["failed_before_start_run_ids"] = [
            str(r.id) for r in current_unstarted if str(getattr(r, "user_id", None) or "") not in opted_out
        ][:MAX_FAILED_TRACE_IDS]
    try:
        result["feedback"] = await _feedback(db, reader, access, days)
    except Exception as exc:
        logger.warning("Platform Health feedback read failed for agent %s: %s", agent.id, format_exception_message(exc))
        result["feedback"] = None
        result["feedback_unavailable"] = True

    findings = (
        (
            await db.execute(
                select(HealthFinding)
                .where(
                    HealthFinding.target_type == "agent",
                    HealthFinding.target_id == agent.id,
                    HealthFinding.status.not_in((STATUS_RESOLVED, STATUS_DISMISSED)),
                )
                .order_by(HealthFinding.last_seen_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    result["known_findings"] = [
        {"id": str(f.id), "pattern_kind": f.pattern_kind, "pattern_key": f.pattern_key, "status": f.status}
        for f in findings
    ]
    return result


def _uuids(values: Iterable[Any] | None) -> list[uuid.UUID]:
    parsed: list[uuid.UUID] = []
    for value in values or ():
        try:
            parsed.append(uuid.UUID(str(value).strip()))
        except (ValueError, TypeError):
            continue
        if len(parsed) >= MAX_EVIDENCE_IDS:
            break
    return parsed


def _counts(raw: Any) -> dict[str, int | float]:
    """Numeric counts only, named by short keys."""
    counts: dict[str, int | float] = {}
    if not isinstance(raw, dict):
        return counts
    for key, value in list(raw.items())[:20]:
        if isinstance(value, bool) or not isinstance(value, int | float):
            continue
        name = clip_text(str(key), 64)
        if name:
            counts[name] = value
    return counts


async def verify_evidence(
    db: AsyncSession, reader: Reader, access: AgentAccess, raw: dict[str, Any] | None
) -> tuple[Evidence, int]:
    """The evidence of *raw* that its rows confirm for *access*'s agent, and how many refs were dropped.

    A rating id must be a rating of the agent's work, a run id one of its
    assignment runs, a trace id one of its traces; a trace or rating of an
    opted-out user is dropped, and trace ids need content access at all. The
    refs are read from the rows, never trusted from the argument.
    """
    from app.models.assignment import AssignmentRun
    from app.services import annotations as annotation_service

    raw = raw if isinstance(raw, dict) else {}
    agent_id = access.agent.id
    opted_out = reader.opted_out_user_ids
    asked = 0

    rating_ids = _uuids(raw.get("rating_ids"))
    asked += len(rating_ids)
    raters = await annotation_service.agent_rating_raters(db, agent_id, rating_ids)
    ratings = tuple(str(r) for r in rating_ids if r in raters and (raters[r] is None or raters[r] not in opted_out))

    run_ids = _uuids(raw.get("run_ids"))
    asked += len(run_ids)
    found_runs: set[uuid.UUID] = set()
    if run_ids:
        found_runs = set(
            (
                await db.execute(
                    select(AssignmentRun.id).where(AssignmentRun.id.in_(run_ids), AssignmentRun.agent_id == agent_id)
                )
            )
            .scalars()
            .all()
        )
    runs = tuple(str(r) for r in run_ids if r in found_runs)

    trace_ids = _uuids(raw.get("trace_ids"))
    asked += len(trace_ids)
    traces: list[str] = []
    if trace_ids and access.content:
        traces = await _agent_trace_ids(db, reader, agent_id, trace_ids)

    evidence = Evidence(trace_ids=tuple(traces), rating_ids=ratings, run_ids=runs, counts=_counts(raw.get("counts")))
    kept = len(evidence.trace_ids) + len(evidence.rating_ids) + len(evidence.run_ids)
    return evidence, asked - kept


async def _agent_trace_ids(
    db: AsyncSession, reader: Reader, agent_id: uuid.UUID, trace_ids: list[uuid.UUID]
) -> list[str]:
    from app.services.tracing.store import build_trace_read_store

    store = build_trace_read_store()
    opted_out = reader.opted_out_user_ids
    kept: list[str] = []
    for trace_id in trace_ids:
        try:
            trace, *_ = await store.get_trace_detail(db, user=_viewer(reader), user_group_ids=[], trace_id=trace_id)
        except Exception as exc:
            # Not found, or not readable: not evidence.
            logger.debug("Platform Health evidence trace %s dropped: %s", trace_id, format_exception_message(exc))
            continue
        if str(getattr(trace, "agent_id", "") or "") != str(agent_id):
            continue
        if getattr(trace, "user_id", None) is not None and trace.user_id in opted_out:
            continue
        kept.append(str(trace_id))
    return kept
