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

"""Wire format for a live investigation: streamed frames and the chat marker.

Two things reach a client while (and after) an investigation runs, and both are
defined here so the shape has one home:

* **Frames** — ``investigation_planned`` / ``investigation_step`` /
  ``investigation_complete``, emitted by the orchestrator through the chat
  service's ``on_event`` callback, the same channel the agent loop already uses
  for ``tool_start`` / ``tool_done`` / ``segment_complete``.
* **The marker** — ``[[INVESTIGATION:<uuid>]]``, embedded in the assistant
  message, on the ``[[REPORT:<uuid>]]`` precedent.  The marker is what makes
  the run addressable after the frames are gone.

Frames are advisory; the record is authoritative
------------------------------------------------
Every builder here reads rows that are **already committed** (the orchestrator
checkpoints before it emits), and every frame carries the run's ``revision``.
That combination is what makes the client's job simple and the feature robust:

* A frame never describes state that is not in the database, so a client that
  missed every frame — page reload mid-run, late join, stream unavailable — renders
  the identical block from the record alone.
* ``revision`` is monotonic per run, so an out-of-order or duplicate frame is
  discarded by comparing it against what the client already holds, rather than
  being applied and corrupting the block.

This is why the transport is a callback rather than something this module owns:
losing a frame is a cosmetic latency problem, not a correctness one, so the
emission path is allowed to be lossy and does not deserve delivery machinery.

Why the marker points at a record and carries no state
------------------------------------------------------
The marker is deliberately just an id, exactly like ``[[REPORT:uuid]]``.  A
marker that encoded step state would be a second, divergent copy of the run's
history living in message text that nothing updates — the block would show
whatever the run looked like when the sentence was written.  Pointing at the
row keeps one source of truth.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

from app.channels.base import _INVESTIGATION_MARKER_RE
from app.models.investigation import Investigation, InvestigationStep
from app.services.investigation_state import RunSummary, duration_seconds

#: Frame statuses, in the order a run emits them. Named on the agent loop's
#: convention (``tool_start`` / ``segment_complete``) so a reader of the chat
#: stream sees one vocabulary.
INVESTIGATION_FRAME_STATUSES: tuple[str, ...] = (
    "investigation_planned",
    "investigation_step",
    "investigation_complete",
)

#: Matches ``[[INVESTIGATION:uuid]]``, case-insensitively: a marker echoed back
#: by an LLM in mixed case must not leak to the user as visible text. The one
#: compiled pattern the channel strippers in ``app/channels/base.py`` use, so the
#: producer and the strippers cannot disagree; the web app reads it with the
#: chat panel's marker grammar, and both are tested on the same samples
#: (``frontend/src/pages/chat/answer-markers.contract.json``).
INVESTIGATION_MARKER_RE = _INVESTIGATION_MARKER_RE


def build_investigation_marker(investigation_id: uuid.UUID | str) -> str:
    """The marker text for a run — the single place the shape is written.

    Callers must not format the marker themselves: the regexes that strip it
    (web renderer) and rewrite it (channels) are matched against this shape,
    and a hand-built marker that drifts leaks raw text to users.
    """
    return f"[[INVESTIGATION:{investigation_id}]]"


def step_payload(step: InvestigationStep) -> dict[str, Any]:
    """One step, flattened for the wire.

    The step's own state is ``step_status``, NOT ``status``: every frame's
    top-level ``status`` names the frame KIND (the agent loop's convention),
    and a step payload spread into a frame would otherwise overwrite it — a
    client reading ``frame.status`` would get ``"error"`` and have no idea what
    kind of frame it was holding. The key is named the same way inside
    ``planned_frame``'s nested steps, so one step object has one shape wherever
    it appears.

    ``confidence`` and every ``*_detail`` field are included even when
    ``None``: the client distinguishes "no confidence" from "not sent yet", and
    an omitted key would make an absent value indistinguishable from a partial
    frame.

    Public, and shared with the REST serializer (``services.investigations``)
    on purpose: the block hydrates from REST and then merges streamed frames into the
    same objects, so a field named differently on the two paths would force a
    translation layer that could drift. One step, one shape.
    """
    return {
        "step_id": str(step.id) if step.id else None,
        "position": step.position,
        "step_status": step.status,
        "source_name": step.source_name,
        "source_pack_slug": step.source_pack_slug,
        "query": step.query,
        "error_type": step.error_type,
        "status_detail": step.status_detail,
        # Localizable form; the client renders it and falls back to
        # `status_detail` only when there is no code (a third-party exception
        # message, which cannot be translated).
        "detail_code": step.detail_code,
        "detail_params": dict(step.detail_params or {}) if step.detail_params else None,
        "findings_count": step.findings_count,
        "confidence": step.confidence,
        "evidence": list(step.evidence or []),
        # The prose a writing source produced. Sent on the same field list as
        # everything else so REST and the frames cannot drift — the spans in
        # `evidence` index into this string, and a client that received one
        # without the other would place its markers by guess.
        "answer": step.answer,
        "iteration": step.iteration,
        "lead_from_step_id": (str(step.lead_from_step_id) if step.lead_from_step_id else None),
        # A goal plan's hop queries its own subject and serves one action of
        # the plan; both live in the step's stored address, the one record of
        # what the step was.
        "subject_value": _ref_text(step, "subject"),
        "action": step_action(step),
    }


#: The frame statuses that describe a persisted investigation run.
#:
#: Exported because a nested run — an agent consulted by another agent — has
#: to forward these to the client UNWRAPPED. Every other inner frame belongs
#: to the consultation and is wrapped in a `consult_progress` envelope; these
#: belong to the CONVERSATION, because the block they drive is attached to the
#: assistant message and outlives the consult that produced it. Wrapped, the
#: component that renders the block never sees them and the run is invisible.
INVESTIGATION_FRAME_STATUSES: frozenset[str] = frozenset(
    {"investigation_planned", "investigation_step", "investigation_complete"}
)


def _ref_text(step: InvestigationStep, key: str) -> str | None:
    ref = step.source_ref if isinstance(step.source_ref, dict) else {}
    value = ref.get(key)
    return value if isinstance(value, str) and value else None


def step_action(step: InvestigationStep) -> str | None:
    """The goal-plan action a step serves.

    The one its pack declared when it declared one, otherwise the default
    action for its kind of source (a lead is followed, a source read).
    """
    from app.services.investigation_packs import default_action_for

    return _ref_text(step, "action") or default_action_for(
        _ref_text(step, "kind"), is_lead=step.lead_from_step_id is not None
    )


def goal_plan_payload(investigation: Investigation, steps: Sequence[InvestigationStep]) -> dict[str, Any]:
    """The goal plan a run follows: what the block draws, whatever the pack.

    A pack that declares a plan gets the path A* computes for the run's subject
    type; any other run gets the default plan, read from the actions its steps
    serve in the order the run reached them. Computed, not stored: the plan is
    code, and the same run always yields the same path. Whether a predicate
    HOLDS is not here: the block derives it from the steps of the action that
    sets it, so the state shown cannot run ahead of what the sources answered.
    """
    from app.services.investigation_packs import (
        declared_plan_path,
        default_goal_plan,
        run_goal_plan,
        stored_plan_path,
    )

    found = run_goal_plan(investigation)
    actions = [step_action(s) for s in steps]
    if found is not None and steps and not {a for a in actions if a} & {a.slug for a in found[1].actions}:
        # A run recorded before its pack declared a plan: none of its steps
        # serves an action of it, and drawing that plan would show every
        # action unreached next to sources that answered. It reads as the
        # default plan instead, which its steps do serve.
        found = None
    if found is not None:
        pack_slug, plan = found
        if isinstance(getattr(investigation, "declared_plan", None), dict):
            # A stored plan (a built pack's): cached by its content, not by
            # slug, since the pack may have changed since the run.
            initial, planned = stored_plan_path(
                json.dumps(investigation.declared_plan, sort_keys=True), investigation.subject_type
            )
        else:
            initial, planned = declared_plan_path(pack_slug, investigation.subject_type)
        # Unreachable is said as such, not drawn as an empty path of cost 0.
        reachable = planned is not None
        path = planned or ()
    else:
        pack_slug = None
        ordered = sorted(steps, key=lambda s: s.position if s.position is not None else 0)
        plan = default_goal_plan([a for a in (step_action(s) for s in ordered) if a])
        initial = plan.initial_state(investigation.subject_type)
        path = plan.actions
        reachable = True
    # What the run can actually do: an action is servable when one of its
    # steps exists, when it writes the report, or when the action feeding it
    # is (a hop has no step until its feeder found something). A path through
    # an action nothing can serve does not reach the goal - say so up front
    # rather than draw it pending until the run ends "not reached".
    served = {a for a in actions if a}
    by_slug = {action.slug: action for action in plan.actions}

    def _servable(action, seen: frozenset[str] = frozenset()) -> bool:
        if action.produces_report or action.slug in served:
            return True
        feeder = by_slug.get(action.fed_by or "")
        return feeder is not None and feeder.slug not in seen and _servable(feeder, seen | {action.slug})

    servable = {action.slug: _servable(action) for action in path}
    reachable = reachable and all(servable.values())
    return {
        "pack_slug": pack_slug,
        "declared": found is not None,
        "reachable": reachable,
        # The template, not the sentence: the block translates it and places
        # the subject itself (`{value}`).
        "objective": plan.objective,
        "predicates": [{"slug": slug, "label": label} for slug, label in plan.predicates],
        "initial_state": [slug for slug, _label in plan.predicates if slug in initial],
        "goal_state": list(plan.goal),
        "actions": [
            {
                "slug": action.slug,
                "label": action.label,
                "description": action.description,
                "cost": action.cost,
                "preconditions": list(action.preconditions),
                "effects": list(action.effects),
                "icon": action.icon,
                "produces_report": action.produces_report,
                "servable": servable[action.slug],
            }
            for action in path
        ],
        "path_cost": sum(action.cost for action in path),
    }


#: Slug of the built-in template an executive summary deck is made with.
EXECUTIVE_SUMMARY_TEMPLATE_SLUG = "xtm-one-brand"


def executive_summary_template_name() -> str:
    """The display name of the built-in XTM One deck, read from its seed entry."""
    from app.services.template_container_seed import BUILTIN_TEMPLATE_CONTAINERS

    for entry in BUILTIN_TEMPLATE_CONTAINERS:
        if entry.get("slug") == EXECUTIVE_SUMMARY_TEMPLATE_SLUG:
            return str(entry["name"])
    raise LookupError(f"no built-in template {EXECUTIVE_SUMMARY_TEMPLATE_SLUG!r}")


def executive_summary_payload(
    investigation: Investigation, steps: Sequence[InvestigationStep]
) -> dict[str, Any] | None:
    """What the block's "Generate executive summary" sends, once there is a report.

    ``prompt`` is the instruction the conversation's agent receives: the deck
    is made by the Presentation feature (``generate_presentation``) from the
    report already in the conversation, with the XTM One template it names.
    None while the run has no report: there would be nothing to summarise.
    """
    if not investigation.report:
        return None
    from app.services.investigation_packs import (
        default_goal_plan,
        executive_summary_request,
        run_goal_plan,
    )

    plan_payload = goal_plan_payload(investigation, steps)
    found = run_goal_plan(investigation) if plan_payload["declared"] else None
    if found is not None:
        plan = found[1]
        actions = plan.plan(plan.initial_state(investigation.subject_type)) or ()
    else:
        plan = default_goal_plan([a["slug"] for a in plan_payload["actions"]])
        actions = plan.actions
    template_name = executive_summary_template_name()
    return {
        "template_name": template_name,
        "prompt": executive_summary_request(
            plan,
            actions,
            subject_value=investigation.subject_value,
            template_name=template_name,
            investigation_id=str(investigation.id),
        ),
    }


def planned_frame(investigation: Investigation, steps: Sequence[InvestigationStep]) -> dict[str, Any]:
    """The pre-run skeleton: the whole plan, before any source is queried.

    Sent as one frame rather than N ``investigation_step`` frames because the
    block renders the plan as a unit — a client that applied the steps one by
    one would animate a list assembling itself, which is noise rather than
    progress.
    """
    return {
        "status": "investigation_planned",
        "investigation_id": str(investigation.id) if investigation.id else None,
        "revision": investigation.revision,
        "subject_type": investigation.subject_type,
        "subject_value": investigation.subject_value,
        "source_pack_slugs": list(investigation.source_pack_slugs or []),
        "max_iterations": investigation.max_iterations,
        "goal_plan": goal_plan_payload(investigation, steps),
        "steps": [step_payload(s) for s in steps],
    }


def step_frame(investigation: Investigation, step: InvestigationStep) -> dict[str, Any]:
    """One step's new state.

    Also used for a step that did not exist in the skeleton (a multi-hop lead):
    a client that does not recognise ``step_id`` appends it at ``position``
    rather than dropping it, which is what lets leads appear in the block as
    they are discovered.
    """
    return {
        "status": "investigation_step",
        "investigation_id": str(investigation.id) if investigation.id else None,
        "revision": investigation.revision,
        "iterations_used": investigation.iterations_used,
        **step_payload(step),
    }


def complete_frame(investigation: Investigation, summary: RunSummary) -> dict[str, Any]:
    """The run's terminal state and its summary.

    ``duration_seconds`` is the measured wall clock or ``None`` — never derived
    from the step count, which is the invariant that keeps a four-minute
    run from reporting "25s". ``all_succeeded`` is carried rather than
    recomputed client-side so the "everything passed" affordance is gated by
    the same server-side rule everywhere.
    """
    return {
        "status": "investigation_complete",
        "investigation_id": str(investigation.id) if investigation.id else None,
        "revision": investigation.revision,
        "run_status": investigation.status,
        "end_reason": investigation.end_reason,
        "end_reason_code": investigation.end_reason_code,
        "end_reason_params": (
            dict(investigation.end_reason_params or {})
            if investigation.end_reason_params
            else None
        ),
        "leads_dropped": investigation.leads_dropped or 0,
        "iterations_used": investigation.iterations_used,
        "max_iterations": investigation.max_iterations,
        "duration_seconds": duration_seconds(investigation),
        "summary": {
            "total": summary.total,
            "by_status": dict(summary.by_status),
            "all_succeeded": summary.all_succeeded,
            "failed": summary.failed,
            "unfinished": summary.unfinished,
            "confidence": summary.confidence,
            "confidence_samples": summary.confidence_samples,
        },
    }
