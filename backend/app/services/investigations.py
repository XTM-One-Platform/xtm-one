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

"""Read-side serialization for investigation runs.

One function, and the reason it exists is the requirement that the block
render identically from the record alone: the REST payload the block hydrates
from and the streamed frames it then merges must describe a step **the same way**.

``serialize_investigation`` therefore builds each step through
``investigation_events.step_payload`` — the very function the frame builders
use — rather than repeating the field list. A second copy would be free to
drift, and the drift would surface as a step that changes shape the moment a
frame arrives (or, worse, one that renders from REST and then disappears
because the merge key moved).

The summary is computed server-side for the same reason it is carried on the
completion frame: ``all_succeeded`` gates the "everything passed" affordance,
and one server-side rule must decide it everywhere.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.models.investigation import Investigation, InvestigationStep
from app.services.investigation_events import (
    executive_summary_payload,
    goal_plan_payload,
    step_payload,
)
from app.services.investigation_state import (
    RunSummary,
    duration_seconds,
    summarize,
)


def serialize_investigation(
    investigation: Investigation,
    steps: Sequence[InvestigationStep],
    *,
    total_steps: int | None = None,
    summary: RunSummary | None = None,
    page_limit: int | None = None,
) -> dict[str, Any]:
    """The run, its steps in display order, and the derived summary.

    ``total_steps`` is how many steps the run HAS, which is not always how many
    were handed to this function: the read endpoints cap at ``MAX_STEPS``. The
    summary is derived from the steps in hand, so a truncated view silently
    made the aggregate a lie — a step that errored past the cap is invisible to
    `summarize`, and `all_succeeded` comes back true for a run that contained
    failures. That is the exact affordance this module exists to keep
    unreachable, and the cap is designed to fire, so "it cannot happen today"
    is not the same as "it cannot happen".

    A truncated view therefore cannot claim success. It reports the real total
    and forces `all_succeeded` false, because what it holds is a sample and a
    sample proves nothing about the steps it does not contain.
    """
    ordered = sorted(steps, key=lambda s: s.position)
    # `summary` is supplied by the read endpoints, aggregated over EVERY step
    # rather than the page they return. Derived from `ordered` only when no
    # caller supplied one — the streamed completion frame, which holds the whole run
    # in memory and has nothing to be truncated against.
    #
    # Gating `all_succeeded` on truncation was not enough on its own: the
    # sibling counts described the sample too, so a run whose only failure sat
    # past the cap reported `failed: 0` — and a zero is as much a claim as a
    # banner. Taking the counts from the database keeps every field true and
    # leaves truncation a statement about which STEPS came back, not about
    # what the run contained.
    supplied = summary is not None
    if summary is None:
        summary = summarize(ordered)
    # Truncation is a statement about the CAP, so the cap is what it tests.
    # `total_steps > len(ordered)` alone also fires on a read-consistency race:
    # the steps and the aggregate are two statements, the block polls DURING a
    # run, and the orchestrator commits after every step — so a lead appended
    # between them gives `total=4` against three rows and flags a three-step
    # run as truncated. `page_limit` is the caller's own cap; below it, more
    # steps means the run grew, not that this read withheld any.
    reached_cap = page_limit is None or len(ordered) >= page_limit
    truncated = reached_cap and total_steps is not None and total_steps > len(ordered)
    # The gate stays, and is not redundant. With a supplied summary it never
    # fires — that summary already covers every step, so nothing is truncated
    # from its point of view. Without one it is the only thing between a
    # sample and a claim, and a caller that knows the total but derives the
    # summary from a page is exactly the shape that produced this bug.
    sampled = truncated and not supplied
    return {
        "id": str(investigation.id),
        "subject_type": investigation.subject_type,
        "subject_value": investigation.subject_value,
        "source_pack_slugs": list(investigation.source_pack_slugs or []),
        "status": investigation.status,
        "end_reason": investigation.end_reason,
        "end_reason_code": investigation.end_reason_code,
        "end_reason_params": (
            dict(investigation.end_reason_params or {})
            if investigation.end_reason_params
            else None
        ),
        "leads_dropped": investigation.leads_dropped or 0,
        "max_iterations": investigation.max_iterations,
        "report": investigation.report,
        "report_sources": list(investigation.report_sources or []),
        "report_claims": investigation.report_claims,
        "report_uncited_claims": investigation.report_uncited_claims,
        "iterations_used": investigation.iterations_used,
        # Same role as ``Report.revision``: lets the block tell "same run, new
        # step states" from a different run, and discard a frame older than
        # what it already holds.
        "revision": investigation.revision,
        # Measured wall clock or null — never derived from the step count.
        "duration_seconds": duration_seconds(investigation),
        # The run this one continues, when it is a continuation. The
        # block needs it to say "continuing an earlier run" rather than
        # presenting a partial plan as a fresh one.
        "continues_investigation_id": (
            str(investigation.continues_investigation_id)
            if investigation.continues_investigation_id
            else None
        ),
        "conversation_id": (str(investigation.conversation_id) if investigation.conversation_id else None),
        "assignment_run_id": (str(investigation.assignment_run_id) if investigation.assignment_run_id else None),
        "started_at": (investigation.started_at.isoformat() if investigation.started_at else None),
        "completed_at": (investigation.completed_at.isoformat() if investigation.completed_at else None),
        # The plan the run follows, when its pack declares one (goal mode).
        "goal_plan": goal_plan_payload(investigation, ordered),
        # The block's "Generate executive summary": the instruction its agent
        # is sent, once a report exists.
        "executive_summary": executive_summary_payload(investigation, ordered),
        "steps": [step_payload(s) for s in ordered],
        # So a client can tell "this run had five steps" from "this is the
        # first five hundred of them".
        "steps_truncated": truncated,
        "summary": {
            "total": total_steps if total_steps is not None else summary.total,
            "by_status": dict(summary.by_status),
            "all_succeeded": summary.all_succeeded and not sampled,
            "failed": summary.failed,
            "unfinished": summary.unfinished,
            "confidence": summary.confidence,
            # The mean alone invites "the run scored 0.9" when it is the mean
            # of one step out of eleven. No consumer renders the aggregate yet
            # — this travels with it so the first one cannot omit it.
            "confidence_samples": summary.confidence_samples,
        },
    }
