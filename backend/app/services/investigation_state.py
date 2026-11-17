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

"""Deep Investigation step-state machine.

The single place that decides what state an investigation step is in.  The
orchestrator calls into this module as it queries sources; the live
investigation block, the report's ``## Sources queried`` section and the
telemetry all read the rows these functions write.

Why this is a service and not inline orchestrator code
------------------------------------------------------
The central constraint is that step state is a **fact about the run**,
derived from the orchestrator, never parsed from model prose.  Concentrating
the transitions here is what makes that auditable: there is one function that
can produce a ``completed`` step, and it cannot produce one for a source that
failed or returned nothing.  A reviewer checking "can a failed source ever
render as a success?" reads ``record_step_result`` and is done.

The three invariants, and why each exists
-----------------------------------------
1. **A failure never becomes a success.**  ``record_step_result`` derives the
   status from the facts it is handed rather than accepting a caller-supplied
   one: an ``error_type`` forces ``error``, a partial result forces
   ``degraded``, and zero findings force ``empty``.  A caller cannot pass
   ``status="completed"`` because there is no such parameter.  The prior art
   this issue reacts to marked every step ``Complete`` in green while 3 of its
   8 model calls had failed — that is what an accepting API produces by
   default.

2. **No unearned confidence.**  ``confidence`` stays ``None`` unless the step
   actually produced findings, and there is no default value anywhere in the
   path.  A run whose confidence cannot be computed shows none.  (The prior art
   displayed a hardcoded ``94%`` next to an empty answer pane.)

3. **No derived duration.**  ``duration_seconds`` returns ``None`` until both
   wall-clock stamps exist, and there is deliberately no fallback that
   estimates from step count — the prior art's ``Duration`` was
   ``steps.length * 3.5``, which read "25s" for a four-minute run.

Session handling: every function mutates in place and ``flush``es so the caller
sees server-assigned ids, but **the caller owns the commit** — the orchestrator
batches a step transition with whatever else it is writing for that iteration.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investigation import (
    DETAIL_CODES,
    MAX_END_REASON_LENGTH,
    MAX_STATUS_DETAIL_LENGTH,
    MAX_SUBJECT_LENGTH,
    STEP_ERROR_TYPES,
    SUCCESS_STEP_STATUSES,
    TERMINAL_INVESTIGATION_STATUSES,
    TERMINAL_STEP_STATUSES,
    Investigation,
    InvestigationStep,
)


def _clamp(value: str | None, limit: int) -> str | None:
    """Fit a free-text field to its column.

    Every one of these strings is built from something outside our control —
    an exception message, a skip reason, a subject typed by a user — and lands
    in a bounded column. Truncating here, against a limit derived from that
    column, is what keeps a ``DataError`` at flush time off the error path.
    """
    return value if value is None else value[:limit]

logger = logging.getLogger(__name__)

#: Statuses that may legally follow ``active``.  Used only for the guard's
#: error message; the transitions themselves are enforced structurally by which
#: function the caller reaches for.
_NON_TERMINAL = ("pending", "active")


class InvalidStepTransitionError(RuntimeError):
    """A step transition that would corrupt the run's history.

    Raised rather than silently ignored: a second terminal write to the same
    step means the orchestrator lost track of which step it was on, and
    swallowing it would leave the block showing a state that never happened.
    Callers that legitimately race (a duplicate frame, a retried iteration)
    should check ``is_terminal`` first instead of catching this.
    """


@dataclass(frozen=True)
class PlannedStep:
    """One step of the pre-run skeleton, derived from the source pack.

    This is the honest version of the "optimistic skeleton" UX: the pack
    definition already names the sources to query, so rendering them as
    ``pending`` before the first model call is a real commitment, not a
    placeholder.  Steps discovered later as multi-hop leads are NOT planned
    here — they are appended with ``append_lead_step`` once a finding produces
    them, so the block can distinguish the two.
    """

    source_name: str
    source_pack_slug: str | None = None
    #: How to reach this step's source again — ``SourceDefinition.to_ref()``.
    #: Persisted on the step so a plan the budget never reached can be
    #: continued later instead of only re-planned from scratch.
    source_ref: dict | None = None


@dataclass(frozen=True)
class RunSummary:
    """Counts and derived values for a run's collapsed summary line."""

    total: int
    by_status: dict[str, int] = field(default_factory=dict)
    #: True only when there is at least one step and every step succeeded.
    #: ``empty`` and ``degraded`` steps make this False on purpose — a summary
    #: banner claiming everything passed must not be reachable otherwise.
    all_succeeded: bool = False
    #: Mean of the step confidences that exist, or ``None`` when no step
    #: carries one.  Never a default.
    confidence: float | None = None
    #: How many steps that mean is over. An UNWEIGHTED mean across the steps
    #: that happen to carry a confidence: one confident step among ten that
    #: carry none reads as the whole run's confidence unless the reader is
    #: also told it was a mean of one.
    #:
    #: No renderer consumes it yet — the block shows per-step confidence and
    #: not the run's. It is published so that whoever renders the aggregate
    #: cannot do so without the sample count in hand, rather than discovering
    #: afterwards that the number needed qualifying.
    confidence_samples: int = 0

    @property
    def failed(self) -> int:
        return self.by_status.get("error", 0)

    @property
    def unfinished(self) -> int:
        """Steps that never reached a terminal state (cancelled mid-run)."""
        return self.by_status.get("pending", 0) + self.by_status.get("active", 0)


def is_terminal(step: InvestigationStep) -> bool:
    """Whether the step can still change state."""
    return step.status in TERMINAL_STEP_STATUSES


def _validate_confidence(value: float | None) -> float | None:
    """Reject anything that is not a real 0..1 confidence.

    ``NaN`` is rejected explicitly: it would pass a naive range check, then
    poison every aggregate computed from it and render as "NaN" in the block.
    """
    if value is None:
        return None
    if math.isnan(value) or math.isinf(value):
        raise ValueError(f"confidence must be a finite number, got {value!r}")
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"confidence must be within 0..1, got {value!r}")
    return float(value)


def _validate_detail_code(code: str | None) -> None:
    """Reject a code the UI has no translation for.

    A typo'd code would fall through to the free-text fallback and silently
    reappear in English — the exact regression this vocabulary exists to
    prevent, and invisible without this check.
    """
    if code is not None and code not in DETAIL_CODES:
        raise ValueError(f"unknown detail code {code!r}; expected one of {DETAIL_CODES}")


def _touch(investigation: Investigation) -> None:
    """Bump the run's revision so a stale frame can be discarded by the client."""
    investigation.revision = (investigation.revision or 1) + 1
    investigation.updated_at = datetime.now(UTC)


async def create_investigation(
    db: AsyncSession,
    *,
    subject_type: str,
    subject_value: str,
    source_pack_slugs: Sequence[str],
    user_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    assignment_run_id: uuid.UUID | None = None,
    max_iterations: int = 10,
    continues_investigation_id: uuid.UUID | None = None,
    source_pack_versions: dict[str, int] | None = None,
    declared_plan: dict | None = None,
) -> Investigation:
    """Create a run in ``planning`` — no steps yet, nothing started.

    The subject is normalised and capped HERE rather than in a caller. The
    column is bounded, so validation belongs where the constraint is: every
    entry point — the chat turn, and the flow-triggered ``assignment_run_id``
    path — reaches this function, and a caller-side cap only protects the one
    caller that has it. Without this, an over-long subject is a ``DataError``
    from Postgres at flush time, after the row is half-built.
    """
    subject_value = (subject_value or "").strip()[:MAX_SUBJECT_LENGTH]
    if not subject_value:
        raise ValueError("an investigation needs a subject")

    investigation = Investigation(
        user_id=user_id,
        conversation_id=conversation_id,
        assignment_run_id=assignment_run_id,
        subject_type=subject_type,
        subject_value=subject_value,
        source_pack_slugs=list(source_pack_slugs),
        status="planning",
        max_iterations=max_iterations,
        continues_investigation_id=continues_investigation_id,
        source_pack_versions=source_pack_versions or None,
        declared_plan=declared_plan,
    )
    db.add(investigation)
    await db.flush()
    return investigation


async def plan_steps(
    db: AsyncSession,
    investigation: Investigation,
    planned: Sequence[PlannedStep],
) -> list[InvestigationStep]:
    """Write the pre-run skeleton as ``pending`` steps, in display order.

    Positions are assigned server-side and contiguous from 0, so step identity
    and ordering never depend on the order streamed frames happen to arrive
    in — the resilience requirement the block relies on.
    """
    steps: list[InvestigationStep] = []
    for position, item in enumerate(planned):
        step = InvestigationStep(
            investigation_id=investigation.id,
            position=position,
            source_name=item.source_name,
            source_pack_slug=item.source_pack_slug,
            source_ref=item.source_ref,
            status="pending",
        )
        db.add(step)
        steps.append(step)
    _touch(investigation)
    await db.flush()
    return steps


async def append_lead_step(
    db: AsyncSession,
    investigation: Investigation,
    *,
    source_name: str,
    source_pack_slug: str | None = None,
    lead_from_step_id: uuid.UUID | None = None,
    iteration: int | None = None,
    position: int | None = None,
    source_ref: dict | None = None,
) -> InvestigationStep:
    """Append a step discovered mid-run as a multi-hop lead.

    Kept separate from ``plan_steps`` because the distinction is user-visible:
    a planned step that never ran is a gap in coverage, while a lead that never
    ran is a lead not followed (the report's ``## Leads not followed`` section).

    ``source_ref`` is stored for the same reason ``plan_steps`` stores it, and
    omitting it here was worse than omitting it there: a lead the budget never
    reached is THE canonical remainder, so every one of them was permanently
    non-continuable — and the panel explained that with `no_address`, which
    renders as "recorded before continuations existed", about a row written
    minutes earlier by the build that was reading it.
    """
    # `position` is looked up only when the caller does not know it. The
    # orchestrator does — it appends every step of the run — and one
    # `SELECT max(position)` per lead is fifty round-trips on a hot loop for a
    # number it is already holding. Optional rather than required, because a
    # caller that appends out of band has no such number and must still be
    # able to ask.
    if position is None:
        result = await db.execute(
            select(InvestigationStep.position)
            .where(InvestigationStep.investigation_id == investigation.id)
            .order_by(InvestigationStep.position.desc())
            .limit(1)
        )
        last = result.scalar_one_or_none()
        position = 0 if last is None else last + 1
    step = InvestigationStep(
        investigation_id=investigation.id,
        position=position,
        source_name=source_name,
        source_pack_slug=source_pack_slug,
        lead_from_step_id=lead_from_step_id,
        iteration=iteration,
        status="pending",
        source_ref=source_ref,
    )
    db.add(step)
    _touch(investigation)
    await db.flush()
    return step


async def start_run(db: AsyncSession, investigation: Investigation) -> None:
    """Move the run to ``running`` and stamp the wall-clock start."""
    investigation.status = "running"
    investigation.started_at = datetime.now(UTC)
    _touch(investigation)
    await db.flush()


async def mark_step_active(
    db: AsyncSession,
    investigation: Investigation,
    step: InvestigationStep,
    *,
    query: str | None = None,
    iteration: int | None = None,
) -> None:
    """Mark the step as being queried right now, recording the actual query.

    ``query`` is the string genuinely issued to the source, not the template:
    an analyst reviewing a run with surprising results asks what was actually
    searched first.
    """
    if is_terminal(step):
        raise InvalidStepTransitionError(f"step {step.id} is already {step.status!r}; expected one of {_NON_TERMINAL}")
    step.status = "active"
    step.started_at = datetime.now(UTC)
    if query is not None:
        step.query = query
    if iteration is not None:
        step.iteration = iteration
        investigation.iterations_used = max(investigation.iterations_used or 0, iteration)
    _touch(investigation)
    await db.flush()


async def record_step_result(
    db: AsyncSession,
    investigation: Investigation,
    step: InvestigationStep,
    *,
    findings_count: int,
    evidence: Sequence[dict] | None = None,
    answer: str | None = None,
    confidence: float | None = None,
    partial: bool = False,
    error_type: str | None = None,
    detail: str | None = None,
    detail_code: str | None = None,
    detail_params: dict | None = None,
) -> str:
    """Record a queried source's outcome and DERIVE the resulting status.

    There is deliberately no ``status`` parameter — see invariant 1 in the
    module docstring.  The status falls out of the facts:

    * ``error_type`` set        → ``error``    (a failure, whatever else came back)
    * ``partial=True``          → ``degraded`` (rate-limited / truncated / auth-limited)
    * ``findings_count == 0``   → ``empty``    (answered, found nothing)
    * otherwise                 → ``completed``

    ``confidence`` is kept only for the two statuses that actually produced
    findings; on ``empty`` and ``error`` it is forced to ``None`` rather than
    stored, because a confidence attached to no findings is exactly the
    unearned metric invariant 2 exists to prevent.

    Returns the derived status so the caller can emit its frame without
    re-deriving it.
    """
    if is_terminal(step):
        raise InvalidStepTransitionError(f"step {step.id} already reached terminal state {step.status!r}")
    if error_type is not None and error_type not in STEP_ERROR_TYPES:
        raise ValueError(f"unknown error_type {error_type!r}; expected one of {STEP_ERROR_TYPES}")
    if findings_count < 0:
        raise ValueError(f"findings_count must be >= 0, got {findings_count}")
    _validate_detail_code(detail_code)

    validated_confidence = _validate_confidence(confidence)

    if error_type is not None:
        status = "error"
    elif partial:
        status = "degraded"
    elif findings_count == 0:
        status = "empty"
    else:
        status = "completed"

    step.status = status
    step.findings_count = findings_count
    step.evidence = list(evidence or [])
    # Kept on every status, `error` included: a search that was rate-limited
    # halfway still wrote something worth reading, and dropping it would make
    # a degraded step indistinguishable from one that produced nothing.
    step.answer = (answer or None)
    step.error_type = error_type
    step.status_detail = _clamp(detail, MAX_STATUS_DETAIL_LENGTH)
    step.detail_code = detail_code
    step.detail_params = detail_params
    step.confidence = validated_confidence if status in ("completed", "degraded") else None
    step.completed_at = datetime.now(UTC)
    _touch(investigation)
    await db.flush()
    return status


async def record_step_error(
    db: AsyncSession,
    investigation: Investigation,
    step: InvestigationStep,
    *,
    error_type: str,
    detail: str | None = None,
    detail_code: str | None = None,
    detail_params: dict | None = None,
) -> None:
    """Record a source that could not be queried at all.

    Thin wrapper over ``record_step_result`` so there is still exactly one
    function writing a step's terminal state — the error path cannot drift
    from the success path.
    """
    await record_step_result(
        db,
        investigation,
        step,
        findings_count=0,
        error_type=error_type,
        detail=detail,
        detail_code=detail_code,
        detail_params=detail_params,
    )


async def mark_step_skipped(
    db: AsyncSession,
    investigation: Investigation,
    step: InvestigationStep,
    *,
    reason: str,
    detail_code: str | None = None,
    detail_params: dict | None = None,
) -> None:
    """Mark a step the run never reached (budget spent, circular lead).

    ``reason`` is required: a skipped step with no explanation is indis-
    tinguishable from a bug, and this string is what the block and the
    report's ``## Leads not followed`` section show.
    """
    if is_terminal(step):
        raise InvalidStepTransitionError(f"step {step.id} already reached terminal state {step.status!r}")
    _validate_detail_code(detail_code)
    step.status = "skipped"
    step.status_detail = _clamp(reason, MAX_STATUS_DETAIL_LENGTH)
    step.detail_code = detail_code
    step.detail_params = detail_params
    step.completed_at = datetime.now(UTC)
    _touch(investigation)
    await db.flush()


async def finish_run(
    db: AsyncSession,
    investigation: Investigation,
    *,
    status: str,
    end_reason: str | None = None,
    end_reason_code: str | None = None,
    end_reason_params: dict | None = None,
    leads_dropped: int | None = None,
) -> None:
    """Close the run and stamp the wall-clock end.

    Steps still ``pending`` / ``active`` are left as they are rather than being
    coerced to a terminal state: a cancelled run showing two never-started
    sources is the truth, and ``RunSummary.unfinished`` surfaces them.

    Omitting ``leads_dropped`` means "leave it alone", not "zero". Defaulting
    to 0 meant every caller that does not pass it — including the finalizer
    that closes an interrupted run — erased a count the run had genuinely
    accumulated, so a run that dropped leads and then hit a failing checkpoint
    reported "0 leads dropped". That is a coverage claim the run cannot
    support, and not making those is the whole point of this block.
    """
    if status not in TERMINAL_INVESTIGATION_STATUSES:
        raise ValueError(f"invalid terminal run status {status!r}")
    _validate_detail_code(end_reason_code)
    investigation.status = status
    investigation.end_reason = _clamp(end_reason, MAX_END_REASON_LENGTH)
    investigation.end_reason_code = end_reason_code
    investigation.end_reason_params = end_reason_params
    if leads_dropped is not None:
        investigation.leads_dropped = leads_dropped
    investigation.completed_at = datetime.now(UTC)
    _touch(investigation)
    await db.flush()


def summarize(steps: Sequence[InvestigationStep]) -> RunSummary:
    """Aggregate a run's steps for the collapsed summary line.

    ``all_succeeded`` requires every step to be in ``SUCCESS_STEP_STATUSES``,
    so an aggregate "all checks passed" affordance is unreachable while any
    step is empty, degraded, errored, skipped or unfinished.
    """
    by_status: dict[str, int] = {}
    for step in steps:
        by_status[step.status] = by_status.get(step.status, 0) + 1

    confidences = [s.confidence for s in steps if s.confidence is not None]
    confidence = sum(confidences) / len(confidences) if confidences else None

    return RunSummary(
        total=len(steps),
        by_status=by_status,
        all_succeeded=bool(steps) and all(s.status in SUCCESS_STEP_STATUSES for s in steps),
        confidence=confidence,
        confidence_samples=len(confidences),
    )


def duration_seconds(investigation: Investigation) -> float | None:
    """Measured wall-clock duration, or ``None`` if the run is not finished.

    There is no estimate-from-step-count fallback, and there must never be
    one — see invariant 3 in the module docstring.
    """
    if investigation.started_at is None or investigation.completed_at is None:
        return None
    return (investigation.completed_at - investigation.started_at).total_seconds()
