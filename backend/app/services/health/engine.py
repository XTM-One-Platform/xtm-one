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

"""Platform Health engine (#101 S2, ``dev-docs/platform-health.md`` section 3).

A **check** is a deterministic async function over database rows, counters or
a probe; it answers one :class:`CheckResult` per subject it read, passing or
failing. A **health pass** runs every registered check whose cadence is due and
records the results through :func:`record_results`:

* a failing result opens (or refreshes) the finding of its check and subject,
  whose id is stable (:func:`finding_key`), so a failure seen on every pass is
  one row;
* a passing result counts towards resolving an open finding: two passes in a
  row resolve it (:data:`RESOLVE_AFTER_PASSES`);
* a dismissed finding stays silent until its dismissal expires, then reopens
  if its check still fails.

**Not metered.** A pass is not an assignment run, not an agent run, holds no
``RunContext`` and imports nothing from ``services/quota.py``: a day of passes
changes no agentic quota counter and no agentic execution count (test (d),
``tests/test_health_engine.py``). No model is ever called, so the pass runs in
every edition and mode, AI switched off included.

**Agents and fleets are opt-in** (section 1.2): agent and fleet checks read
:func:`enrolled_agent_ids` alone, which is empty after an upgrade, whatever the
number of agents (test (a)).

**Score** (shown on the page): per module ``100 - 25 x critical - 8 x warning -
1 x info`` over open findings, floored at 0; the overall score is the lowest
module score. :func:`rollup_day` keeps one row per day and module for
:data:`HISTORY_DAYS` days.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.fleet import FLEET_ROLE_MEMBER, Fleet, FleetMember
from app.models.health_check import (
    MODULES,
    STATUS_DISMISSED,
    STATUS_OPEN,
    STATUS_RESOLVED,
    HealthCheckFinding,
    HealthScoreDay,
)

logger = logging.getLogger(__name__)

#: Consecutive passing reads that resolve an open finding.
RESOLVE_AFTER_PASSES = 2
#: Days of score history kept (7, 30 and 90 day views).
HISTORY_DAYS = 90
#: The penalty per open finding of each severity.
SEVERITY_WEIGHTS: dict[str, int] = {"critical": 25, "warning": 8, "info": 1}
#: The longest dismissal reason kept (characters).
MAX_DISMISSAL_REASON = 500


@dataclass(frozen=True)
class CheckResult:
    """What one check read about one subject, passing or not."""

    check_id: str
    module: str
    category: str
    severity: str
    failing: bool
    subject: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    recommendation: str = ""
    doc_link: str = ""


CheckFunction = Callable[[AsyncSession, datetime], Awaitable[Sequence[CheckResult]]]


@dataclass(frozen=True)
class Check:
    """A registered check: its id, module, cadence and function."""

    check_id: str
    module: str
    interval: timedelta
    run: CheckFunction


#: Every registered check, by id. Module slices (S3 onward) register theirs
#: with :func:`register_check` when their module is imported.
_CHECKS: dict[str, Check] = {}
#: When each check last ran in this process. A missed pass after a restart
#: only runs the check once more, which a deterministic read tolerates.
_LAST_RUN: dict[str, datetime] = {}


def register_check(check: Check) -> Check:
    if check.module not in MODULES:
        raise ValueError(f"Unknown health module: {check.module}")
    _CHECKS[check.check_id] = check
    return check


def registered_checks() -> list[Check]:
    return sorted(_CHECKS.values(), key=lambda c: c.check_id)


def finding_key(check_id: str, subject: dict[str, str]) -> str:
    """The stable id of a finding: ``sha256(check_id|subject)`` with the subject's keys sorted."""
    body = check_id + "|" + json.dumps(subject or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def _dismissal_live(row: HealthCheckFinding, now: datetime) -> bool:
    record = row.dismissal or {}
    until = record.get("until")
    if not until:
        return True
    try:
        return datetime.fromisoformat(str(until)) > now
    except ValueError:
        return False


async def record_results(db: AsyncSession, results: Iterable[CheckResult], *, now: datetime) -> dict[str, int]:
    """Apply one pass's results to the finding store; counts of ``opened``, ``updated``, ``resolved``."""
    counts = {"opened": 0, "updated": 0, "resolved": 0}
    for result in results:
        key = finding_key(result.check_id, result.subject)
        row = await db.get(HealthCheckFinding, key)
        if result.failing:
            if row is None:
                db.add(
                    HealthCheckFinding(
                        id=key,
                        check_id=result.check_id,
                        module=result.module,
                        subject=dict(result.subject),
                        category=result.category,
                        severity=result.severity,
                        status=STATUS_OPEN,
                        evidence=dict(result.evidence),
                        recommendation=result.recommendation,
                        doc_link=result.doc_link,
                        pass_streak=0,
                        first_seen=now,
                        last_seen=now,
                    )
                )
                counts["opened"] += 1
                continue
            row.severity, row.category = result.severity, result.category
            row.evidence, row.last_seen, row.pass_streak = dict(result.evidence), now, 0
            row.recommendation, row.doc_link = result.recommendation, result.doc_link
            if row.status == STATUS_DISMISSED and _dismissal_live(row, now):
                continue
            if row.status != STATUS_OPEN:
                row.status, row.resolved_at, row.dismissal = STATUS_OPEN, None, None
                counts["opened"] += 1
            else:
                counts["updated"] += 1
        elif row is not None and row.status == STATUS_OPEN:
            row.pass_streak += 1
            if row.pass_streak >= RESOLVE_AFTER_PASSES:
                row.status, row.resolved_at = STATUS_RESOLVED, now
                counts["resolved"] += 1
    await db.flush()
    return counts


async def close_out(db: AsyncSession, check_id: str, read: Sequence[CheckResult]) -> list[CheckResult]:
    """A passing result for each open finding of ``check_id`` whose subject this pass did not read.

    The subject left the check's scope (fixed, paused, deleted, unenrolled): its
    cause is gone for this check, so the engine resolves it after two such reads.
    """
    seen = {finding_key(r.check_id, r.subject) for r in read}
    rows = (
        await db.execute(
            select(HealthCheckFinding).where(
                HealthCheckFinding.check_id == check_id, HealthCheckFinding.status == STATUS_OPEN
            )
        )
    ).scalars()
    return [
        CheckResult(
            check_id=check_id,
            module=row.module,
            category=row.category,
            severity=row.severity,
            failing=False,
            subject=dict(row.subject or {}),
            evidence={"values": {"in_scope": False}},
            doc_link=row.doc_link or "",
        )
        for row in rows
        if row.id not in seen
    ]


async def dismiss_finding(
    db: AsyncSession,
    finding_id: str,
    *,
    reason: str,
    by: str,
    by_name: str,
    now: datetime,
    until: datetime | None = None,
) -> HealthCheckFinding:
    """Dismiss an open finding with a reason and an optional expiry (the ``fleet_health_dismissals`` pattern)."""
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("A dismissal needs a reason.")
    row = await db.get(HealthCheckFinding, finding_id)
    if row is None:
        raise LookupError("Health finding not found")
    row.status = STATUS_DISMISSED
    row.dismissal = {
        "reason": reason[:MAX_DISMISSAL_REASON],
        "by": by,
        "by_name": by_name,
        "at": now.isoformat(),
        "until": until.isoformat() if until else None,
    }
    await db.flush()
    return row


async def restore_finding(db: AsyncSession, finding_id: str) -> HealthCheckFinding:
    """Bring a dismissed finding back to the open list (its dismissal cleared); the next pass resolves it if it passes."""
    row = await db.get(HealthCheckFinding, finding_id)
    if row is None:
        raise LookupError("Health finding not found")
    if row.status != STATUS_DISMISSED:
        raise ValueError("Only a dismissed finding can be restored.")
    row.status = STATUS_OPEN
    row.dismissal = None
    await db.flush()
    return row


def score_from_counts(critical: int, warning: int, info: int) -> int:
    penalty = (
        SEVERITY_WEIGHTS["critical"] * critical
        + SEVERITY_WEIGHTS["warning"] * warning
        + SEVERITY_WEIGHTS["info"] * info
    )
    return max(0, 100 - penalty)


async def open_counts(db: AsyncSession) -> dict[str, dict[str, int]]:
    """Open findings per module and severity; every module present, zeros included."""
    counts = {module: dict.fromkeys(SEVERITY_WEIGHTS, 0) for module in MODULES}
    rows = await db.execute(
        select(HealthCheckFinding.module, HealthCheckFinding.severity, func.count())
        .where(HealthCheckFinding.status == STATUS_OPEN)
        .group_by(HealthCheckFinding.module, HealthCheckFinding.severity)
    )
    for module, severity, count in rows.all():
        counts[module][severity] = int(count)
    return counts


async def module_scores(db: AsyncSession) -> tuple[dict[str, int], int]:
    """``(score per module, overall score)``: the overall score is the lowest module score."""
    scores = {
        module: score_from_counts(c["critical"], c["warning"], c["info"])
        for module, c in (await open_counts(db)).items()
    }
    return scores, min(scores.values())


async def rollup_day(db: AsyncSession, day: date) -> None:
    """Write today's score row per module and drop rows past :data:`HISTORY_DAYS`."""
    for module, c in (await open_counts(db)).items():
        row = await db.get(HealthScoreDay, (day, module))
        score = score_from_counts(c["critical"], c["warning"], c["info"])
        if row is None:
            db.add(HealthScoreDay(day=day, module=module, score=score, **c))
        else:
            row.score, row.critical, row.warning, row.info = score, c["critical"], c["warning"], c["info"]
    await db.execute(delete(HealthScoreDay).where(HealthScoreDay.day < day - timedelta(days=HISTORY_DAYS)))
    await db.flush()


async def enrolled_agent_ids(db: AsyncSession) -> frozenset[uuid.UUID]:
    """The agents agent checks read: enrolled one by one, or home members of an enrolled fleet.

    Platform Health's own agents (``health_role``) are never checked. Nothing
    is enrolled by default: on an upgrade this is empty, whatever the count.
    """
    in_enrolled_fleet = (
        select(FleetMember.agent_id)
        .join(Fleet, Fleet.id == FleetMember.fleet_id)
        .where(Fleet.health_enrolled.is_(True), FleetMember.role == FLEET_ROLE_MEMBER)
    )
    rows = await db.execute(
        select(Agent.id).where(
            Agent.health_role.is_(None),
            or_(Agent.health_enrolled.is_(True), Agent.id.in_(in_enrolled_fleet)),
        )
    )
    return frozenset(rows.scalars())


async def run_pass(db: AsyncSession, *, now: datetime, checks: Sequence[Check] | None = None) -> dict[str, int]:
    """Run every due check once and record its results; a failing check is logged, never fatal.

    Each check runs in its own savepoint, so a check that raises undoes only
    its own writes: the results recorded by the checks before it in the same
    pass are kept and committed with the pass.
    """
    totals = {"checks": 0, "opened": 0, "updated": 0, "resolved": 0}
    for check in checks if checks is not None else registered_checks():
        last = _LAST_RUN.get(check.check_id)
        if last is not None and now - last < check.interval:
            continue
        _LAST_RUN[check.check_id] = now
        try:
            async with db.begin_nested():
                results = await check.run(db, now)
                counts = await record_results(db, results, now=now)
        except Exception:
            logger.exception("Platform Health check %s failed", check.check_id)
            continue
        totals["checks"] += 1
        for key, value in counts.items():
            totals[key] += value
    await rollup_day(db, now.date())
    await db.commit()
    return totals


async def health_pass_tick(*, now: datetime | None = None) -> dict[str, int]:
    """The scheduler's health pass (``autonomous._run_autonomous_tick_inner``), whatever the AI switch."""
    from app.core.database import async_session
    from app.services.health import (  # noqa: F401  (register the agent, XTM One, OpenCTI, OpenAEV and OpenCRQ checks, 4.1 to 4.5)
        agent_checks,
        openaev_checks,
        opencrq_checks,
        opencti_checks,
        platform_checks,
    )

    async with async_session() as db:
        return await run_pass(db, now=now or datetime.now(UTC))
