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

"""Holding background AI work while agentic AI is unavailable.

Work that comes due while agentic AI cannot run here - Community Edition, or
the administrator's AI switch off - has not started: a delayed or queued
assignment run, a delegated task not yet picked up, the pending runs of an
evaluation, the resume of a paused run a person answered meanwhile. It is HELD,
not failed: its orchestration execution goes back to wait through
``orchestration_engine.defer`` (no attempt consumed, deadline measured only
while runnable), is due again after a capped backoff, and the adapter asks the
gate again. It runs as soon as agentic AI is available again, and is failed
with the reason once it has been held for ``AI_UNAVAILABLE_HOLD_MAX_SECONDS``.

This module is the one place the hold is decided and worded; the adapters
apply it and the domain rows carry its marker (``HOLD_MARKER_KEY``) so the API
and the UI can say the work is held, never that it failed.

The same hold waits out a quota store that cannot be reached while quotas
fail closed (``QUOTA_STORE_UNAVAILABLE_CODE``): the work met the store
before anything was charged, and runs - charged once - as soon as the store
answers, under the same bound.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.config import settings
from app.services.agentic_gate import AI_DISABLED_CODE, AgenticVerdict, refusal_message

# The first re-check comes after this long, then the delay doubles up to the
# cap: held work resumes at most ``HOLD_MAX_DELAY_SECONDS`` after agentic AI is
# available again, and a long hold costs one cached gate read per execution
# every few minutes.
HOLD_FIRST_DELAY_SECONDS = 30.0
HOLD_MAX_DELAY_SECONDS = 300.0

# ``adapter_state`` of a held execution: ``held:ai_disabled`` /
# ``held:license_required`` / ``held:quota_unavailable``.
HELD_STATE_PREFIX = "held:"

# Key of the marker a held domain row carries (``AssignmentRun.trigger_context``,
# ``SubAgentTask.metadata_json``): ``{"reason", "message", "since"}``.
HOLD_MARKER_KEY = "ai_hold"

# The reason of a hold on the quota store, the code the chat gate
# reports the same outage with.
QUOTA_STORE_UNAVAILABLE_CODE = "quota_unavailable"


def held_message(refusal: str) -> str:
    """What a person reads about held work: never an error. Copilot mode has
    no Community Edition: ``license_required`` there is the missing license
    (``deps.licence_required_detail``)."""
    if refusal == QUOTA_STORE_UNAVAILABLE_CODE:
        return (
            "Held: agentic usage cannot be metered right now (the quota store "
            "is unreachable). It runs by itself as soon as the quota store "
            "answers again."
        )
    if refusal == AI_DISABLED_CODE:
        # The platform's switch, or a group of the person the work runs for
        # (``group_ai_access``): the same code, so the same hold.
        why = "AI turned off, on the platform or for a group of the person it runs for"
    elif settings.is_copilot_like:
        why = "no valid Filigran license"
    else:
        why = "Community Edition"
    return (
        f"Held: AI is not available on this platform ({why}). "
        "It runs by itself as soon as agentic AI is available again."
    )


def held_adapter_state(refusal: str) -> str:
    return f"{HELD_STATE_PREFIX}{refusal}"


def is_held_state(adapter_state: str | None) -> bool:
    return bool(adapter_state) and str(adapter_state).startswith(HELD_STATE_PREFIX)


def hold_delay_seconds(previous_deferrals: int) -> float:
    """Seconds until held work is due again after ``previous_deferrals``."""
    exponent = min(max(0, previous_deferrals), 16)
    return min(HOLD_FIRST_DELAY_SECONDS * (2**exponent), HOLD_MAX_DELAY_SECONDS)


def hold_budget_left(execution, *, now: datetime | None = None) -> bool:
    """May this execution be held (again)? False once holding is disabled
    (``AI_UNAVAILABLE_HOLD_MAX_SECONDS=0``) or its hold lasted the maximum."""
    from app.services.orchestration_engine import hold_started_at

    maximum = settings.ai_unavailable_hold_max_seconds
    if maximum <= 0:
        return False
    since = hold_started_at(execution)
    if since is None:
        return True
    return ((now or datetime.now(UTC)) - since).total_seconds() < maximum


def expired_message(refusal: str, *, was_held: bool) -> str:
    """The failure reason once holding is over: the refusal, and - when the
    work was held - that it waited the maximum."""
    if refusal == QUOTA_STORE_UNAVAILABLE_CODE:
        from app.services.quota import QUOTA_UNAVAILABLE_MESSAGE

        message, waited_for = QUOTA_UNAVAILABLE_MESSAGE, "the quota store to answer"
    else:
        message, waited_for = refusal_message(refusal), "agentic AI to be available"
    if not was_held:
        return message
    if settings.ai_unavailable_hold_max_seconds <= 0:
        return (
            f"{message} It was held waiting for {waited_for} again until "
            "holding was turned off on this platform."
        )
    hours = settings.ai_unavailable_hold_max_seconds / 3600
    return (
        f"{message} It was held for {hours:g} hour(s) waiting for {waited_for} "
        "again, the maximum on this platform."
    )


def failure_cause_of(refusal: str):
    """The structured cause a hold's work fails with once holding is
    over."""
    from app.services.failure_causes import FailureCause

    if refusal == QUOTA_STORE_UNAVAILABLE_CODE:
        return FailureCause.QUOTA_BACKEND_UNAVAILABLE
    return FailureCause(refusal)


def hold_result(execution, refusal: str):
    """The adapter result that holds ``execution`` for ``refusal``."""
    from app.services.orchestration_adapters.base import AdapterResult
    from app.services.orchestration_engine import hold_record

    previous = int((hold_record(execution) or {}).get("count") or 0)
    return AdapterResult.defer(
        held_message(refusal),
        delay_seconds=hold_delay_seconds(previous),
        adapter_state=held_adapter_state(refusal),
    )


async def close_hold_if_ai_is_back(ctx, execution, verdict=None, user_id=None):
    """Close ``execution``'s hold when the held work is about to start, and
    return the gate's verdict it decided on - ``None`` for an execution that
    was never held, which reads nothing.

    A pure payload read for an execution that was never held (Enterprise
    Edition with AI on pays nothing). A held one asks the gate - unless the
    caller already did (``verdict``) - and, when agentic AI may run, drops
    the hold record through ``orchestration_engine.end_hold`` and commits,
    before the body runs: a crash of the real work followed by agentic AI
    going away again then starts a hold of its own. The caller hands the
    verdict to the body, so the start decides on ONE reading: a body reading
    again could start the work while the hold is still open.

    A hold on the quota store also needs the store to answer while
    quotas fail closed: while it is out, the hold stays open - its bound,
    backoff and runnable budget keep counting - and the verdict handed to the
    body is that refusal, so the body holds on this reading and never starts
    the work under an open hold.
    """
    from app.services import orchestration_engine

    record = orchestration_engine.hold_record(execution)
    if record is None:
        return verdict
    if verdict is None:
        from app.services.agentic_gate import agentic_verdict, group_verdict

        verdict = await agentic_verdict(ctx.db)
        if verdict.refusal is None:
            # For the person the work is for: a group of theirs may still
            # switch AI off, and a hold closed on a reading that ignores it
            # would start a new one - with a fresh bound - at the next gate.
            verdict = await group_verdict(ctx.db, user_id)
    if verdict.transient or verdict.refusal is not None:
        return verdict
    if record.get("reason") == held_adapter_state(QUOTA_STORE_UNAVAILABLE_CODE):
        from app.services.quota import quota_fail_closed, quota_store_out

        if quota_fail_closed() and await quota_store_out():
            return AgenticVerdict(QUOTA_STORE_UNAVAILABLE_CODE)
    await orchestration_engine.end_hold(
        ctx.db,
        execution_id=execution.id,
        worker_id=ctx.worker_id,
        held_adapter_state=is_held_state(execution.adapter_state),
    )
    await ctx.db.commit()
    return verdict


def hold_marker(refusal: str, *, since: str | None = None) -> dict:
    return {
        "reason": refusal,
        "message": held_message(refusal),
        "since": since or datetime.now(UTC).isoformat(),
    }


def with_hold_marker(data: dict | None, refusal: str) -> dict:
    """``data`` (a JSON column's value) carrying the hold marker, keeping the
    first hold's start. Reassign it: in-place JSON mutation is not tracked."""
    data = dict(data or {})
    previous = data.get(HOLD_MARKER_KEY)
    since = previous.get("since") if isinstance(previous, dict) else None
    data[HOLD_MARKER_KEY] = hold_marker(refusal, since=since)
    return data


def without_hold_marker(data: dict | None) -> dict | None:
    """``data`` without the marker, or ``None`` when it carried none - so a row
    that was never held is not written."""
    if not isinstance(data, dict) or HOLD_MARKER_KEY not in data:
        return None
    rest = dict(data)
    rest.pop(HOLD_MARKER_KEY, None)
    return rest


def held_execution_conditions() -> tuple:
    """SQL conditions on ``OrchestrationExecution`` for held work still in
    flight: ``pending`` between re-checks, ``running`` while it is claimed for
    one (and, after a worker was lost mid-check, until its lease is
    reclaimed). Read off the durable hold record, never ``adapter_state``: a
    transient retry or a lease reclaim mid-hold clears the adapter state but
    keeps the hold."""
    from app.models.orchestration import OrchestrationExecution
    from app.services.orchestration_engine import HOLD_PAYLOAD_KEY

    return (
        OrchestrationExecution.state.in_(("pending", "running")),
        OrchestrationExecution.payload.has_key(HOLD_PAYLOAD_KEY),
    )


async def held_evaluations(db, evaluation_ids) -> dict:
    """``{evaluation_id: refusal code}`` for the evaluations among
    *evaluation_ids* whose execution is held. An evaluation run has no JSON
    column to carry a marker, so its held state is read off the execution."""
    import uuid as _uuid

    from sqlalchemy import select

    from app.models.orchestration import OrchestrationExecution
    from app.services.orchestration_engine import HOLD_PAYLOAD_KEY

    ids = {str(e) for e in evaluation_ids if e}
    if not ids:
        return {}
    evaluation_key = OrchestrationExecution.payload["evaluation_id"].astext
    hold_reason = OrchestrationExecution.payload[HOLD_PAYLOAD_KEY]["reason"].astext
    rows = (
        await db.execute(
            select(evaluation_key, hold_reason).where(
                OrchestrationExecution.adapter == "evaluation",
                *held_execution_conditions(),
                evaluation_key.in_(sorted(ids)),
            )
        )
    ).all()
    held: dict = {}
    for raw_id, reason in rows:
        try:
            key = _uuid.UUID(str(raw_id))
        except (TypeError, ValueError):
            continue
        reason = str(reason or "")
        held[key] = reason[len(HELD_STATE_PREFIX) :] if is_held_state(reason) else reason
    return held


def held_code_of(data: dict | None) -> str | None:
    """The refusal code (``license_required`` / ``ai_disabled``) of the hold
    marker in *data*, or ``None`` - what the API gives the UI."""
    marker = data.get(HOLD_MARKER_KEY) if isinstance(data, dict) else None
    if isinstance(marker, dict):
        reason = marker.get("reason")
        return str(reason) if reason else None
    return None


def held_reason_of(data: dict | None) -> str | None:
    marker = data.get(HOLD_MARKER_KEY) if isinstance(data, dict) else None
    if isinstance(marker, dict):
        message = marker.get("message")
        return str(message) if message else None
    return None
