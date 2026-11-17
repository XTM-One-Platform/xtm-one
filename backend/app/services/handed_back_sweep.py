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

"""Backstop for assignment runs handed back by a stopping worker that never resume.

A run whose worker is asked to stop (a rollout, a SIGTERM, a node drain) is
handed back (:func:`assignment_run_resume.hand_back`): the run stays
``running`` with ``trigger_context.handed_back`` recorded, so the work items it
claimed stay held by a live run, and its execution is deferred to be claimed
again at once. The next attempt's :func:`assignment_run_resume.prepare_resume`
clears the mark.

When that claim never comes (the execution kept behind a claim hold for a
worker or release that is gone, or the execution ended or vanished meanwhile),
nothing ended the run: the stale-run sweep skips a run whose execution is
pending or running, and the claim release only frees the items of runs that
ended. The run stayed "alive" with no worker on it and its items held, and an
owner could neither set them open ("still running") nor cancel the run.

This sweep, run on every scheduler tick, reads each running run still marked
handed back:

* younger than ``handed_back_resume_seconds``: left alone, the resume is due;
* its execution held by a worker (``running`` with a lease): left alone, that
  worker is resuming it, and a gone worker's lease is the reconciler's;
* its execution ``pending`` and older than that: nudged, its claim holds
  cleared and its ``run_at`` set to now, workers woken;
* still not resumed past ``handed_back_orphan_seconds``, or its execution
  terminal or missing past the resume bound: the run is failed and its
  execution cancelled, so the claim release of the same tick frees its items
  and publishes ``run.orphaned``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: Runs read per tick; the next tick takes the rest.
SWEEP_BATCH = 500

#: The prefix of the error a run failed by this sweep carries.
STRANDED_HAND_BACK_ERROR = "Handed back by its stopping worker and never resumed:"

Decision = Literal["wait", "nudge", "fail"]


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    reason: str = ""


def handed_back_at(trigger_context: dict[str, Any] | None) -> datetime | None:
    """When a run's stopping worker handed it back, else ``None``."""
    from app.services.background_checkpoint import HANDED_BACK_KEY

    back = (trigger_context or {}).get(HANDED_BACK_KEY) if isinstance(trigger_context, dict) else None
    if not isinstance(back, dict):
        return None
    raw = back.get("at")
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def decide(
    *,
    since: datetime,
    now: datetime,
    execution_state: str | None,
    lease_owner: str | None,
    resume_seconds: float,
    orphan_seconds: float,
) -> Verdict:
    """What to do with a run handed back at *since* whose execution is in *execution_state*."""
    from app.models.orchestration import TERMINAL_EXECUTION_STATES

    age = (now - since).total_seconds()
    if age < resume_seconds:
        return Verdict("wait")
    if execution_state is None or execution_state in TERMINAL_EXECUTION_STATES:
        what = "has no execution" if execution_state is None else f"execution is {execution_state}"
        return Verdict("fail", f"its {what} {int(age)}s after the hand-back")
    if execution_state == "running" and lease_owner:
        return Verdict("wait")
    if age >= orphan_seconds:
        return Verdict(
            "fail",
            f"no worker resumed it within {int(orphan_seconds)}s (execution {execution_state}, {int(age)}s)",
        )
    if execution_state == "pending":
        return Verdict("nudge")
    return Verdict("wait")


async def _locked_out(db: AsyncSession, keys: list[str], locked: set[str]) -> set[str]:
    """The *keys* whose execution exists but was not locked: a worker holds its row now."""
    from app.models.orchestration import OrchestrationExecution

    missing = [k for k in keys if k not in locked]
    if not missing:
        return set()
    rows = await db.execute(
        select(OrchestrationExecution.dedup_key).where(OrchestrationExecution.dedup_key.in_(missing))
    )
    return {row[0] for row in rows.all()}


def _bounds() -> tuple[float, float]:
    from app.core.config import settings

    resume = max(60.0, float(getattr(settings, "handed_back_resume_seconds", 180)))
    orphan = max(resume, float(getattr(settings, "handed_back_orphan_seconds", 300)))
    return resume, orphan


async def fail_stranded_hand_backs(db: AsyncSession, *, now: datetime | None = None) -> tuple[int, int]:
    """Nudge or fail the handed-back runs no worker resumed: ``(nudged, failed)``.

    The caller releases the claims of the runs failed here
    (:func:`work_item.release_ended_run_claims`).
    """
    from app.models.assignment import AssignmentRun
    from app.models.orchestration import TERMINAL_EXECUTION_STATES, OrchestrationExecution
    from app.services import orchestration_engine as engine
    from app.services.orchestration_adapters.assignment import execution_dedup_key

    moment = now or datetime.now(UTC)
    resume_seconds, orphan_seconds = _bounds()
    runs = (
        (
            await db.execute(
                select(AssignmentRun)
                .where(AssignmentRun.status == "running", AssignmentRun.trigger_context.is_not(None))
                .order_by(AssignmentRun.created_at)
                .limit(SWEEP_BATCH)
            )
        )
        .scalars()
        .all()
    )
    marked = [(run, at) for run in runs if (at := handed_back_at(run.trigger_context)) is not None]
    if not marked:
        return 0, 0
    keys = [execution_dedup_key(run.id) for run, _ in marked]
    executions = {
        row.dedup_key: row
        for row in (
            # Locked, skipping a row a worker is claiming now: a claim never
            # races a nudge or a cancel, and a skipped row is read next tick.
            await db.execute(
                select(OrchestrationExecution)
                .where(OrchestrationExecution.dedup_key.in_(keys))
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    }
    nudged = failed = 0
    locked_out = await _locked_out(db, keys, {k for k in executions if k is not None})
    for run, since in marked:
        key = execution_dedup_key(run.id)
        if key in locked_out:
            continue
        execution = executions.get(key)
        verdict = decide(
            since=since,
            now=moment,
            execution_state=execution.state if execution is not None else None,
            lease_owner=execution.lease_owner if execution is not None else None,
            resume_seconds=resume_seconds,
            orphan_seconds=orphan_seconds,
        )
        if verdict.decision == "nudge" and execution is not None:
            payload = dict(execution.payload or {})
            for hold in (
                engine.NOT_BEFORE_PAYLOAD_KEY,
                engine.CLAIM_FOR_PAYLOAD_KEY,
                engine.NOT_FOR_PAYLOAD_KEY,
                engine.NOT_RELEASE_PAYLOAD_KEY,
            ):
                payload.pop(hold, None)
            execution.payload = payload
            execution.run_at = moment
            execution.updated_at = moment
            nudged += 1
            logger.warning(
                "Handed-back assignment run %s not resumed after %ds: its execution made due now",
                run.id,
                int((moment - since).total_seconds()),
            )
        elif verdict.decision == "fail":
            # Read again under a lock: a resume that cleared the mark, or an
            # end the run reached, since the read above wins.
            await db.refresh(run, with_for_update=True)
            if run.status != "running" or handed_back_at(run.trigger_context) != since:
                continue
            run.status = "failed"
            run.error = f"{STRANDED_HAND_BACK_ERROR} {verdict.reason}; its work item claims are released."[:2000]
            run.completed_at = moment
            if execution is not None and execution.state not in TERMINAL_EXECUTION_STATES:
                await engine.cancel_execution(db, execution_id=execution.id)
            failed += 1
            logger.warning("Handed-back assignment run %s failed: %s", run.id, verdict.reason)
    if nudged or failed:
        await db.commit()
    if nudged:
        try:
            await engine.notify_orchestration_wake()
        except Exception:  # a missed wake costs one reconciler poll
            logger.debug("Orchestration wake after a hand-back nudge failed", exc_info=True)
    return nudged, failed
