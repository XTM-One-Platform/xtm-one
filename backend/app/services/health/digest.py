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

"""The digest of a Platform Health cycle (#8261, PRD 6.11, decision B22).

Built by code when the cycle closes, from the rows the cycle's work left -
the cycle's budget line, its run, the consolidations, findings, experiments
and proposals of its window - never from what a model says, so a step that
did not succeed cannot read as one:

* ``status`` is ``completed`` only when the run completed and nothing was
  skipped; ``partial`` when it completed with skipped work; otherwise the
  cycle's own end (``failed``, ``budget_reached``, ``cancelled``).
* A count a lower level cannot give is ``unavailable``, never 0.

Language: the counts and statuses are rendered by the UI in the viewer's
language; the text written by a model (the run's report, findings, proposal
summaries) was asked in the platform language (``cycles.platform_language_code``).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.health import HealthCycle
from app.models.health_cycle import HealthCycleRun
from app.services.health.identity import MODULE_CHECKER_ROLES

logger = logging.getLogger(__name__)

#: Longest run report kept in a digest.
NARRATIVE_MAX_CHARS: Final = 8000


@dataclass
class Window:
    since: datetime
    until: datetime
    target_type: str | None
    target_id: uuid.UUID | None
    fleet_agent_ids: list[uuid.UUID]
    #: The cycle the digest is built for (its check report is keyed by it).
    cycle_id: uuid.UUID | None = None


@dataclass
class CycleDigest:
    cycle_id: str
    specialist: str
    trigger: str
    target_type: str | None
    target_id: str | None
    status: str
    opened_at: str
    closed_at: str | None
    run_id: str | None = None
    run_status: str | None = None
    stop_reason: str | None = None
    signal: dict | None = None
    consolidations: dict[str, int] | None = None
    findings: dict[str, int] | None = None
    experiments: dict[str, int] | None = None
    proposals: dict[str, Any] | None = None
    budget: dict[str, Any] = field(default_factory=dict)
    dry_run_writes: int | None = None
    #: A Module Analyst cycle (#101 S14d, dev-docs 12.7): recommendations it attached, by confidence.
    recommendations: dict[str, int] | None = None
    #: A Module Analyst cycle: agentic findings of its registration opened and resolved in the window.
    agentic_findings: dict[str, int] | None = None
    #: A module checker cycle (#101 S15c, dev-docs 13.8): its check report's counts and failing checks.
    check_report: dict[str, Any] | None = None
    #: The weekly digest (#101 S15e, dev-docs 13.8): its trigger's Modules part, per module, as the
    #: schedule computed it from the stored reports (``cycles._digest_payload``), never a model's count.
    modules: dict[str, Any] | None = None
    skipped: list[dict[str, Any]] = field(default_factory=list)
    #: Sections a lower level could not give (not installed, failed): shown as such.
    unavailable: list[str] = field(default_factory=list)
    narrative: str | None = None


# ── Sections a lower level owns, loaded lazily ──────────────────────────────


async def _consolidations(db: AsyncSession, w: Window) -> dict[str, int] | None:
    from app.services.health import memory_steward  # level 5

    return dict(await memory_steward.consolidation_counts(db, since=w.since, until=w.until))


async def _findings(db: AsyncSession, w: Window) -> dict[str, int] | None:
    from app.services.health import findings  # level 6

    return dict(await findings.window_counts(db, since=w.since, until=w.until, target=_target(w)))


async def _experiments(db: AsyncSession, w: Window) -> dict[str, int] | None:
    from app.services.health import experiments  # level 6

    return dict(await experiments.status_counts(db, since=w.since, until=w.until, target=_target(w)))


async def _dry_run_writes(db: AsyncSession, w: Window) -> int | None:
    from app.services.health.sandbox import dry_run  # level 2

    return int(await dry_run.count_dry_run_writes(db, since=w.since, until=w.until))


async def _proposals(db: AsyncSession, w: Window) -> dict[str, Any] | None:
    """Proposals filed by the fleet in the window, and those applied or rolled back in it (level 3's rows)."""
    from app.models.improvement_proposal import ImprovementProposal  # level 3

    def scoped(query):
        if w.target_type == "flow":
            return query.where(ImprovementProposal.target_type == "flow", ImprovementProposal.target_id == w.target_id)
        if w.target_type == "agent":
            return query.where(ImprovementProposal.agent_id == w.target_id)
        if w.target_type == "assignment":
            return query.where(
                ImprovementProposal.target_type == "assignment", ImprovementProposal.target_id == w.target_id
            )
        return query

    by_fleet = ImprovementProposal.author_agent_id.in_(w.fleet_agent_ids)
    filed_rows = await db.execute(
        scoped(
            select(ImprovementProposal.experiment_verdict, func.count()).where(
                by_fleet, ImprovementProposal.created_at >= w.since, ImprovementProposal.created_at < w.until
            )
        ).group_by(ImprovementProposal.experiment_verdict)
    )
    by_verdict = {str(verdict or "none"): int(count) for verdict, count in filed_rows.all()}
    applied = await db.scalar(
        scoped(
            select(func.count()).where(
                by_fleet, ImprovementProposal.applied_at >= w.since, ImprovementProposal.applied_at < w.until
            )
        )
    )
    rolled_back = await db.scalar(
        scoped(
            select(func.count()).where(
                by_fleet,
                ImprovementProposal.status == "rolled_back",
                ImprovementProposal.settled_at >= w.since,
                ImprovementProposal.settled_at < w.until,
            )
        )
    )
    return {
        "filed": sum(by_verdict.values()),
        "applied": int(applied or 0),
        "rolled_back": int(rolled_back or 0),
        "by_verdict": by_verdict,
    }


async def _recommendations(db: AsyncSession, w: Window) -> dict[str, int] | None:
    """Recommendations attached to the module registration's findings in the window, by confidence."""
    from app.models.health_module_analyst import RECOMMENDATION_CONFIDENCES, HealthFindingRecommendation

    if w.target_type != "module" or w.target_id is None:
        return None
    rows = await db.execute(
        select(HealthFindingRecommendation.confidence, func.count())
        .where(
            HealthFindingRecommendation.registration_id == w.target_id,
            HealthFindingRecommendation.created_at >= w.since,
            HealthFindingRecommendation.created_at < w.until,
        )
        .group_by(HealthFindingRecommendation.confidence)
    )
    counts = {confidence: 0 for confidence in RECOMMENDATION_CONFIDENCES}
    counts.update({str(confidence): int(count) for confidence, count in rows.all()})
    return {"attached": sum(counts.values()), **counts}


async def _agentic_findings(db: AsyncSession, w: Window) -> dict[str, int] | None:
    """Agentic findings (``<module>.agentic.<slug>``) of the registration opened and resolved in the window."""
    from app.models.health_check import HealthCheckFinding

    if w.target_type != "module" or w.target_id is None:
        return None
    scoped = (
        HealthCheckFinding.check_id.contains(".agentic."),
        HealthCheckFinding.subject["registration_id"].astext == str(w.target_id),
    )
    opened = await db.scalar(
        select(func.count()).where(
            *scoped, HealthCheckFinding.first_seen >= w.since, HealthCheckFinding.first_seen < w.until
        )
    )
    resolved = await db.scalar(
        select(func.count()).where(
            *scoped, HealthCheckFinding.resolved_at >= w.since, HealthCheckFinding.resolved_at < w.until
        )
    )
    return {"opened": int(opened or 0), "resolved": int(resolved or 0)}


async def _check_report(db: AsyncSession, w: Window) -> dict[str, Any] | None:
    """The check report the module checker stored in this cycle (13.6): counts and the checks not passed.

    ``recorded`` is false when the run stored none, which the digest lists as skipped work.
    """
    from app.models.health_module_analyst import HealthModuleCheckReport

    if w.target_type != "module" or w.cycle_id is None:
        return None
    row = (
        await db.execute(select(HealthModuleCheckReport).where(HealthModuleCheckReport.cycle_id == w.cycle_id))
    ).scalar_one_or_none()
    if row is None:
        return {"recorded": False}
    return {
        "recorded": True,
        "report_id": str(row.id),
        "passed": int(row.passed),
        "warned": int(row.warned),
        "failed": int(row.failed),
        "not_run": int(row.not_run),
        "not_passed": [
            {"id": entry.get("id"), "verdict": entry.get("verdict"), "finding_id": entry.get("finding_id")}
            for entry in (row.checks or [])
            if isinstance(entry, dict) and entry.get("verdict") != "pass"
        ],
    }


async def _module_analyst_budget(db: AsyncSession, run: Any, cap_tokens: int | None) -> dict[str, Any]:
    """A Module Analyst cycle's spend: its run's traces against its own per-cycle cap (dev-docs 12.5).

    Its cycle row carries nothing (``cycles._book_runs`` books it at zero on its own ledger), so
    the fleet figures would read 0 against the fleet cap; ``ledger`` says which ceiling applies.
    """
    from decimal import Decimal

    from app.models.llm_trace import LLMTrace

    tokens, cost = 0, Decimal(0)
    if run is not None:
        used = func.coalesce(
            LLMTrace.total_tokens,
            func.coalesce(LLMTrace.input_tokens, 0) + func.coalesce(LLMTrace.output_tokens, 0),
        )
        row = (
            await db.execute(
                select(func.coalesce(func.sum(used), 0), func.coalesce(func.sum(LLMTrace.total_cost), 0)).where(
                    LLMTrace.assignment_run_id == run.id
                )
            )
        ).one()
        tokens, cost = int(row[0] or 0), Decimal(row[1] or 0)
    return {
        "ledger": "module_analyst",
        "spent_tokens": tokens,
        "spent_cost_usd": str(cost),
        "cap_tokens": cap_tokens,
        "cap_cost_usd": None,
    }


def _target(w: Window) -> tuple[str, uuid.UUID] | None:
    return (w.target_type, w.target_id) if w.target_id is not None else None


Section = Callable[[AsyncSession, Window], Awaitable[Any]]
_SECTIONS: Final[dict[str, Section]] = {
    "consolidations": _consolidations,
    "findings": _findings,
    "experiments": _experiments,
    "proposals": _proposals,
    "dry_run_writes": _dry_run_writes,
    "recommendations": _recommendations,
    "agentic_findings": _agentic_findings,
    "check_report": _check_report,
}
#: Which sections each specialist's cycle reports.
_SECTIONS_BY_SPECIALIST: Final[dict[str, tuple[str, ...]]] = {
    "memory_steward": ("consolidations", "dry_run_writes"),
    "quality_analyst": ("findings", "experiments", "proposals", "dry_run_writes"),
    "flow_lab": ("findings", "experiments", "proposals", "dry_run_writes"),
    "experimenter": ("experiments", "proposals", "dry_run_writes"),
    # What a module checker attached to its registration's findings and the agentic findings it
    # opened (#101 S14d, S15); ``module_analyst`` reads the cycles the S14 member closed.
    **{
        role: ("check_report", "recommendations", "agentic_findings", "proposals", "dry_run_writes")
        for role in sorted(MODULE_CHECKER_ROLES)
    },
    # An investigation delegates to every specialist; the weekly digest reads cycles.
    "orchestrator": ("findings", "experiments", "proposals", "dry_run_writes"),
}

#: Experiment statuses that are not a success, listed as skipped work.
_NOT_SUCCESS_EXPERIMENTS: Final = ("skipped", "degraded", "failed", "cancelled", "budget_reached", "incomplete")


async def _load_section(db: AsyncSession, name: str, w: Window) -> tuple[Any, bool]:
    try:
        async with db.begin_nested():
            return await _SECTIONS[name](db, w), True
    except (ImportError, AttributeError):
        # The level that owns the section is not installed in this build.
        return None, False
    except Exception:
        logger.exception("Platform Health digest: section %s failed", name)
        return None, False


def _weekly_modules(run: Any) -> tuple[dict[str, Any] | None, bool]:
    """The Modules part the weekly digest's trigger carries, and whether its read failed.

    ``(None, False)`` for any other run; ``(None, True)`` when the schedule could not read the
    reports (``modules_unavailable``), which the page shows as unavailable, never as 0.
    """
    payload = getattr(run, "trigger_payload", None) if run is not None else None
    if not isinstance(payload, dict):
        return None, False
    if payload.get("modules_unavailable"):
        return None, True
    modules = payload.get("modules")
    return (modules, False) if isinstance(modules, dict) else (None, False)


def digest_status(cycle_status: str, run_status: str | None, skipped: list[dict]) -> str:
    """``completed`` only for a completed run with nothing skipped."""
    if cycle_status != "completed":
        return cycle_status
    if run_status != "completed":
        return "failed"
    return "partial" if skipped else "completed"


async def build_digest(db: AsyncSession, cycle: HealthCycle) -> CycleDigest:
    """Build and store the digest of a closed *cycle*. Flushed, not committed."""
    from app.models.assignment import AssignmentRun
    from app.services.health.fleet_seed import fleet_agent_ids
    from app.services.health.settings import load_health_settings

    record = await db.get(HealthCycleRun, cycle.id)
    run = await db.get(AssignmentRun, record.run_id) if record is not None and record.run_id else None
    until = cycle.closed_at or datetime.now(UTC)
    window = Window(
        since=cycle.opened_at,
        until=until,
        target_type=cycle.target_type,
        target_id=cycle.target_id,
        fleet_agent_ids=await fleet_agent_ids(db),
        cycle_id=cycle.id,
    )
    health = await load_health_settings(db)
    caps = health.budget
    digest = CycleDigest(
        cycle_id=str(cycle.id),
        specialist=cycle.specialist,
        trigger=cycle.trigger,
        target_type=cycle.target_type,
        target_id=str(cycle.target_id) if cycle.target_id else None,
        status=cycle.status,
        opened_at=cycle.opened_at.isoformat(),
        closed_at=cycle.closed_at.isoformat() if cycle.closed_at else None,
        run_id=str(run.id) if run is not None else None,
        run_status=run.status if run is not None else None,
        stop_reason=cycle.stop_reason,
        signal=record.signal if record is not None else None,
        budget=(
            await _module_analyst_budget(db, run, health.module_analyst.cycle_tokens)
            if cycle.specialist in MODULE_CHECKER_ROLES
            else {
                "spent_tokens": int(cycle.spent_tokens),
                "spent_cost_usd": str(cycle.spent_cost_usd),
                "cap_tokens": caps.cycle_tokens,
                "cap_cost_usd": str(caps.cycle_cost_usd) if caps.cycle_cost_usd is not None else None,
            }
        ),
    )
    for name in _SECTIONS_BY_SPECIALIST.get(cycle.specialist, ()):
        value, available = await _load_section(db, name, window)
        if available:
            setattr(digest, name, value)
        else:
            digest.unavailable.append(name)
    digest.modules, modules_unavailable = _weekly_modules(run)
    if modules_unavailable:
        digest.unavailable.append("modules")

    if run is None:
        digest.skipped.append({"what": "run", "reason": cycle.stop_reason or "not_started"})
    elif run.status != "completed":
        digest.skipped.append({"what": "run", "reason": run.status})
    if cycle.status == "budget_reached":
        digest.skipped.append({"what": "budget", "reason": cycle.stop_reason or "budget_reached"})
    for status in _NOT_SUCCESS_EXPERIMENTS:
        count = (digest.experiments or {}).get(status)
        if count:
            digest.skipped.append({"what": "experiments", "reason": status, "count": int(count)})
    if digest.check_report is not None and not digest.check_report.get("recorded"):
        digest.skipped.append({"what": "check_report", "reason": "not_recorded"})
    unmeasured = ((digest.proposals or {}).get("by_verdict") or {}).get("unmeasured")
    if unmeasured:
        digest.skipped.append({"what": "measurement", "reason": "unmeasured", "count": int(unmeasured)})
    digest.status = digest_status(cycle.status, run.status if run is not None else None, digest.skipped)
    if run is not None and run.result_summary:
        digest.narrative = run.result_summary[:NARRATIVE_MAX_CHARS]

    if record is None:
        record = HealthCycleRun(cycle_id=cycle.id, target_type=cycle.target_type, target_id=cycle.target_id)
        db.add(record)
    record.digest = asdict(digest)
    record.digest_built_at = datetime.now(UTC)
    await db.flush()
    return digest
