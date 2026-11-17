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

"""Run-to-run comparison for the Flow Lab: flow vs flow, flow vs assignment (#8260).

``get_flow_run_comparison`` (PRD section 8) sets two settled runs side by side: a
sandbox replay against the run it replays, or a shadow flow run against the
assignment run it shadows. It reports, per side, the status, the final output,
tokens, cost and duration, and how many writes were intercepted or answered by
a simulation (level 2), then one verdict.

The verdict is deterministic, with no model call: the outputs agree when they
are equal once whitespace and case are normalized, or similar above
:data:`AGREEMENT_RATIO`. Judging *quality* is the evaluation engine's job
(level 4); this comparison answers "did the replacement do the same thing",
which is what a shadow measures (decision B12).

A run that has not settled, or a side that cannot be read, makes the verdict
``incomplete``: a missing answer is never counted as agreement. A side whose
writes were simulated says so beside the verdict (decision B17).
"""

from __future__ import annotations

import difflib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

#: Two final outputs whose normalized texts are at least this similar agree.
AGREEMENT_RATIO = 0.85

#: How much of each output the comparison reads: the final output of a run is
#: already bounded (``FlowWalker._final_summary``: 2000), an assignment's
#: summary is not.
COMPARED_CHARS = 4000

#: How far around a run's own start and end its traces are looked for.
TRACE_WINDOW_SLACK = timedelta(minutes=5)

RunKind = Literal["flow_run", "assignment_run"]
Verdict = Literal["agree", "disagree", "both_failed", "incomplete"]

_SETTLED = {
    "flow_run": ("completed", "failed", "cancelled", "skipped"),
    "assignment_run": ("completed", "failed", "cancelled", "skipped"),
}
_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class RunSide:
    kind: RunKind
    run_id: uuid.UUID
    status: str
    output: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None
    duration_ms: float | None = None
    intercepted_writes: int = 0
    simulated_writes: int = 0

    @property
    def settled(self) -> bool:
        return self.status in _SETTLED[self.kind]

    @property
    def succeeded(self) -> bool:
        return self.status == "completed"


@dataclass(frozen=True, slots=True)
class RunComparison:
    left: RunSide | None
    right: RunSide | None
    verdict: Verdict
    #: ``None`` unless both outputs were compared.
    similarity: float | None
    #: Why the verdict is what it is, one plain sentence.
    note: str

    @property
    def simulated_writes(self) -> int:
        return sum(side.simulated_writes for side in (self.left, self.right) if side)

    def as_dict(self) -> dict[str, Any]:
        """The JSON a shadow pair stores and a tool reports."""

        def side(s: RunSide | None) -> dict | None:
            if s is None:
                return None
            return {
                "kind": s.kind,
                "run_id": str(s.run_id),
                "status": s.status,
                "input_tokens": s.input_tokens,
                "output_tokens": s.output_tokens,
                "cost": s.cost,
                "duration_ms": s.duration_ms,
                "intercepted_writes": s.intercepted_writes,
                "simulated_writes": s.simulated_writes,
            }

        return {
            "verdict": self.verdict,
            "similarity": self.similarity,
            "note": self.note,
            "left": side(self.left),
            "right": side(self.right),
        }


def normalize_output(text: str | None) -> str:
    return _WHITESPACE.sub(" ", (text or "")[:COMPARED_CHARS]).strip().casefold()


def compare_sides(left: RunSide | None, right: RunSide | None) -> RunComparison:
    """The verdict on two run sides (pure)."""
    if left is None or right is None:
        return RunComparison(left, right, "incomplete", None, "One of the two runs cannot be read.")
    if not (left.settled and right.settled):
        return RunComparison(left, right, "incomplete", None, "One of the two runs has not ended yet.")
    if not left.succeeded and not right.succeeded:
        return RunComparison(left, right, "both_failed", None, "Both runs failed.")
    if left.succeeded != right.succeeded:
        failed = left if not left.succeeded else right
        return RunComparison(
            left, right, "disagree", None, f"The {failed.kind.replace('_', ' ')} ended {failed.status}; the other completed."
        )
    a, b = normalize_output(left.output), normalize_output(right.output)
    if not a and not b:
        return RunComparison(left, right, "agree", 1.0, "Both runs completed without a final output.")
    similarity = 1.0 if a == b else difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    verdict: Verdict = "agree" if similarity >= AGREEMENT_RATIO else "disagree"
    note = (
        f"Both runs completed; their final outputs are {round(similarity * 100)} % similar "
        f"({'at or above' if verdict == 'agree' else 'below'} {round(AGREEMENT_RATIO * 100)} %)."
    )
    return RunComparison(left, right, verdict, round(similarity, 4), note)


def _duration_ms(started: Any, completed: Any) -> float | None:
    if started is None or completed is None:
        return None
    return round((completed - started).total_seconds() * 1000, 1)


async def _write_stats(db: AsyncSession, run_id: uuid.UUID) -> tuple[int, int]:
    """``(intercepted, simulated)`` writes of a sandbox or shadow flow run (level 2); zeros otherwise."""
    from app.services.concierge.sandbox.runs import sandbox_run_report

    report = await sandbox_run_report(db, flow_run_id=run_id)
    if report is None:
        return 0, 0
    return int(report.intercepted_writes), int(report.simulated_writes)


async def flow_run_side(db: AsyncSession, run: Any) -> RunSide:
    """One flow run's side; tokens and cost from its ``flow`` trace."""
    from app.models.llm_trace import LLMTrace

    totals = (
        await db.execute(
            select(
                func.sum(LLMTrace.input_tokens),
                func.sum(LLMTrace.output_tokens),
                func.sum(LLMTrace.total_cost),
            ).where(
                LLMTrace.source == "flow",
                # The (source, started_at) index bounds the scan to the run's own window.
                LLMTrace.started_at >= run.created_at - TRACE_WINDOW_SLACK,
                LLMTrace.started_at <= (run.completed_at or datetime.now(UTC)) + TRACE_WINDOW_SLACK,
                LLMTrace.metadata_json["flow_run_id"].astext == str(run.id),
            )
        )
    ).one()
    intercepted, simulated = await _write_stats(db, run.id)
    return RunSide(
        kind="flow_run",
        run_id=run.id,
        status=run.status,
        output=run.result_summary or "",
        input_tokens=int(totals[0]) if totals[0] is not None else None,
        output_tokens=int(totals[1]) if totals[1] is not None else None,
        cost=float(totals[2]) if totals[2] is not None else None,
        duration_ms=_duration_ms(run.started_at, run.completed_at),
        intercepted_writes=intercepted,
        simulated_writes=simulated,
    )


async def assignment_run_side(db: AsyncSession, run: Any) -> RunSide:
    """One assignment run's side; cost from its trace when one was recorded."""
    from app.models.llm_trace import LLMTrace

    cost = None
    if run.llm_trace_id is not None:
        cost = await db.scalar(select(LLMTrace.total_cost).where(LLMTrace.id == run.llm_trace_id))
    return RunSide(
        kind="assignment_run",
        run_id=run.id,
        status=run.status,
        output=run.result_summary or "",
        input_tokens=run.total_input_tokens,
        output_tokens=run.total_output_tokens,
        cost=float(cost) if cost is not None else None,
        duration_ms=_duration_ms(run.started_at, run.completed_at),
    )


async def run_side(db: AsyncSession, kind: RunKind, run: Any | None) -> RunSide | None:
    if run is None:
        return None
    if kind == "flow_run":
        return await flow_run_side(db, run)
    return await assignment_run_side(db, run)
