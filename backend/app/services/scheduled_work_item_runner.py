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

"""Dispatch and execution of scheduled work items.

The rules of a scheduled item live in ``scheduled_work_items.py``; this module
runs them.

**Dispatch** (:func:`dispatch_due_scheduled_work_items`, step 3c of the 60 s
scheduler tick). Every due item - ``action_mode`` set, ``open``, due within the
grace window - is claimed with THE atomic claim
(``work_item.claim_open_work_item``), and in the same transaction gets ONE
assignment-less ``AssignmentRun`` (``agent_id`` = owner, ``user_id`` = the
person who scheduled it, ``trigger_payload.trigger_type =
scheduled_work_item``) and that run's orchestration execution, under the
canonical ``assignment_run:<run id>`` key: the engine, the stale-run sweep, a
run deletion and the emergency stop all find it like any run's execution. An
item past the grace window is blocked as ``missed`` and its creator notified;
an item whose run ended without settling it (a run deleted, a desync the
stale-run sweep repaired) is settled from its run. Nothing new is queued: the
only queue is the orchestration engine's.

**Execution** (:func:`execute_scheduled_run`, the ``scheduled_work_item``
adapter in the worker). The run acts for ``item.user_id`` and re-checks
everything when it starts - the kill switch, the action, the agent, the
person's account, their right to use the agent, the edition and AI gate - and
for an exact action the tool itself, resolved for that person from the agent's
bindings (deny list, tool scoping, read-only bindings, element scope, the
approval gate and an agent account's pinned integration included). A failed
check runs nothing and blocks the item with the reason. Then:

* ``exact`` - the stored tool call is executed as stored, with no model: the
  arguments are never rewritten.
* ``instruction`` - the owner agent runs the prompt in a background turn
  (``run_agent_chat``), metered like any background task.

**Never sent twice blind.** The run records ``action_started_at`` before the
action is sent. Only a failure the action provably never reached is retried,
at most ``SCHEDULED_WORK_ITEM_MAX_ATTEMPTS`` times: the AI state that could not
be read, a quota store outage, a target that answered 429, a model provider
outage before any tool ran. A refused check, an invalid payload and an error
the tool returned are never retried.

**Cut by a rollout.** An action a restart cuts is never handed to a
person: it goes on by itself, like a background task and an assignment run.

* ``instruction``: the turn is checkpointed at every iteration boundary
  (``assignment_run_resume``); a worker asked to stop winds it down and hands
  it back, and the next worker resumes it from the checkpoint, after a
  graceful stop at most ``MAX_GRACEFUL_STREAK`` (3) times in a row with no
  progress and ``MAX_GRACEFUL_RESUMES`` (200) in all, and
  ``MAX_RESUMES`` (3) after a loss without a word, the cause recorded on the
  run and in the resumed trace's ``metadata.resumed_from``.
* ``exact``: a read-only tool (the approval whitelist's reads) is run again;
  any other call may already have reached its target, so the owner agent
  gets it as an instruction run that reads the tool's state first.
* Past the resume caps, or when its run was lost outside an attempt, the item
  is re-queued at once for its owner agent as a fresh instruction run that
  carries the previous outcome and the resume counts
  (:func:`owner_requeue_prompt`), at most :data:`MAX_OWNER_REQUEUES` times.

``blocked`` stays for a failure that is not a restart; nothing in it asks the
person to check or reschedule.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update

from app.core.config import settings
from app.core.utils import format_exception_message, safe_uuid
from app.models.assignment import AssignmentRun
from app.models.work_item import WorkItem
from app.services import scheduled_work_items as swi

logger = logging.getLogger(__name__)

_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled", "skipped"})
_PROOF_MAX_CHARS = 4000
_EXACT_ATTEMPT_SECONDS = 600
_RETRY_FAILURE_CAUSES = frozenset(
    {
        "provider_rate_limited",
        "provider_overloaded",
        "provider_timeout",
        "provider_connection_failed",
        "all_providers_failed",
    }
)

#: What an attempt that finds its action already started does.
RESUME = "resume"
RERUN = "rerun"
REQUEUE = "requeue"
#: ``RunOutcome.kind`` of an attempt its stopping worker handed back.
HANDED_BACK = "handed_back"
#: ``RunOutcome.kind`` of a run replaced by a fresh one for the owner agent.
REQUEUED = "requeued"
#: Fresh runs the owner agent gets for one action cut by restarts, at most.
MAX_OWNER_REQUEUES = 3
#: ``trigger_context`` key of the prompt a re-queued run runs instead of the action.
OWNER_PROMPT_KEY = "owner_prompt"
#: ``trigger_context`` key of the re-queue a run carries: count, run before, cause.
REQUEUE_KEY = "requeued"


def _now() -> datetime:
    return datetime.now(UTC)


def execution_max_attempts() -> int:
    """The ``max_attempts`` of a scheduled run's execution: its retries, and its resumes after a wordless loss."""
    from app.services import assignment_run_resume

    return max(int(settings.scheduled_work_item_max_attempts or 0), assignment_run_resume.execution_max_attempts())


def _requeue_count(context: dict | None) -> int:
    record = (context or {}).get(REQUEUE_KEY)
    count = record.get("count") if isinstance(record, dict) else None
    return count if isinstance(count, int) and not isinstance(count, bool) and count > 0 else 0


def _with_restart(payload: Any, key: str) -> dict:
    counts = swi.restarts_of(payload)
    counts[key] += 1
    return {**(payload if isinstance(payload, dict) else {}), swi.RESTARTS_KEY: counts}


def resume_decision(
    context: dict | None,
    *,
    attempt: int,
    mode: str | None,
    read_only: bool,
    taken_back_on_its_word: bool = False,
) -> tuple[str, str | None]:
    """What an attempt that finds its action already started does, and why when it is not a resume.

    :data:`RESUME` an instruction (or a re-queued run) from its checkpoint and
    :data:`RERUN` a read-only exact call, within the caps of a background
    task (``assignment_run_resume.resume_refused``: after a graceful stop 3 in
    a row with no progress or 200 in all, 3 after a loss without a word);
    :data:`REQUEUE` it for its owner agent
    past a cap, or for an exact call that may already have reached its target.
    Never a block: a restart is never the person's to handle. A take-back from
    a worker that said it was stopping counts as a graceful stop.
    """
    from app.services import assignment_run_resume

    refused = assignment_run_resume.resume_refused(
        context, attempt=attempt, taken_back_on_its_word=taken_back_on_its_word
    )
    if refused:
        return REQUEUE, refused
    if (context or {}).get(OWNER_PROMPT_KEY) or mode != "exact":
        return RESUME, None
    if read_only:
        return RERUN, None
    return REQUEUE, NOT_IDEMPOTENT_REASON


NOT_IDEMPOTENT_REASON = (
    "a platform restart cut the run after its tool call was started, and the call may change "
    "something, so it is not sent again blind"
)


def owner_requeue_prompt(
    *,
    title: str,
    action: dict,
    mode: str | None,
    previous_outcome: str | None,
    lost: int,
    graceful: int,
    requeue: int,
) -> str:
    """The prompt of the fresh run the owner agent gets for an action restarts cut."""
    import json

    lines = [
        f"Scheduled action '{title}', run again for you because platform restarts cut it "
        f"({graceful} graceful stop(s), {lost} loss(es) without a word; fresh run {requeue} "
        f"of at most {MAX_OWNER_REQUEUES})."
    ]
    if mode == "exact":
        arguments = json.dumps(action.get("arguments") or {}, ensure_ascii=False)[:4000]
        lines.append(
            f"The action is one call: {action.get('tool')} with the arguments {arguments}. An earlier "
            "run started it, so it may already have reached its target. First read the target's state "
            "with your tools (for example whether the message is already there), make the call only "
            "if it was not done, and say what you found."
        )
    else:
        lines.append(f"The instruction: {action.get('prompt') or ''}")
        lines.append(
            "An earlier run started it and may have done part of it. First check what is already "
            "done, then do only what remains, and say what you found."
        )
    if previous_outcome:
        lines.append(f"What the previous run recorded: {_clip(previous_outcome, 1500)}")
    return "\n\n".join(lines)


def exhausted_reason(context: dict | None, *, cause: str) -> str:
    """What an item cut by restarts past every resume and every fresh run says."""
    from app.services import assignment_run_resume

    lost, graceful = assignment_run_resume.resume_counts(context)
    return (
        f"Platform restarts cut it again after {_requeue_count(context)} fresh run(s) of its agent "
        f"(the last one: {graceful} graceful resume(s), {lost} after a loss without a word); "
        f"last cause: {cause}. The platform stopped running it."
    )


def _clip(text: str | None, limit: int = _PROOF_MAX_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else f"{text[: limit - 1]}\u2026"


def receipt_token(run_id: str | uuid.UUID, attempt: int) -> str:
    """The metering receipt an instruction turn writes, one per attempt."""
    return f"scheduled_work_item:{run_id}:{attempt}"


def turn_receipt_token(run_id: str | uuid.UUID, attempt: int, context: dict | None) -> str:
    """The receipt of this turn: one per attempt, and one per resume of a handed-back attempt.

    A hand-back gives its attempt back, so the turn that resumes it runs at
    the same attempt number: its receipt names the resume too.
    """
    from app.services import background_checkpoint

    resumes = len(background_checkpoint.resumes_of(context))
    base = receipt_token(run_id, attempt)
    return f"{base}:r{resumes}" if resumes else base


def cut_turn_receipt(run_id: str | uuid.UUID, context: dict | None) -> str | None:
    """The receipt the turn a restart cut wrote, read before this attempt records its resume.

    That turn recorded its attempt on the run when it started, and no resume
    was recorded since: its receipt is :func:`turn_receipt_token` of both.
    Credited back once the action goes on, so one action stays one metered
    action whatever the restarts (as a background task's lost attempt).
    """
    attempt = (context or {}).get("attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        return None
    return turn_receipt_token(run_id, attempt, context)


async def _credit_cut_turn(token: str | None, reason: str) -> None:
    if not token:
        return
    from app.services.quota import refund_agentic_usage_by_receipt

    await refund_agentic_usage_by_receipt(token, reason=reason)



# ── Exact mode: the agent's tools, for one person ────────────────────────────


@dataclass
class ExactToolset:
    """What an exact action may call: the agent's integration, channel and custom tools."""

    composite: Any
    names: list[str] = field(default_factory=list)

    def definition(self, name: str):
        canonical = self.composite.resolve_canonical_name(name) or name
        if canonical not in self.names:
            return None
        return self.composite.get_tool_definition(canonical)


async def resolve_exact_toolset(db, agent, user_id: str, *, pinned: dict | None = None) -> ExactToolset:
    """The tools an exact action of *agent* may run for *user_id*, as a background run sees them.

    The resolution of an assignment run, for the person the item acts for:
    the agent's fixed bindings (a colleague's binding the person cannot see is
    dropped, unless the agent lends it), its template bindings resolved to the person's own or a
    company-wide integration, an agent account's pinned integration, the
    agent's tool scoping, read-only bindings and enforced element scope, its
    channel and custom tools, then its deny list. MCP tools are not offered:
    an exact action calls a tool the platform can name and check in advance.
    """
    from app.api.v1.helpers import get_user_group_ids
    from app.core.security_options import security_option
    from app.services.builtin_tools import (
        apply_tool_deny_list,
        filter_builtin_tools,
        filter_integration_tools,
        load_custom_tools,
    )
    from app.services.element_scope import agent_run_scope
    from app.services.email_signature_settings import SIGNATURE_CONTEXT_KEY
    from app.services.integration_live_writes import LIVE_WRITES_CONTEXT_KEY, live_writes_overrides
    from app.services.integration_read_only import (
        READ_ONLY_CONTEXT_KEY,
        read_only_integration_ids,
    )
    from app.services.integration_tools import (
        build_channel_tools,
        build_integration_tools,
        pin_bindings,
        resolve_agent_integration_bindings,
    )
    from app.tools.base import CompositeToolExecutor

    user_uuid = uuid.UUID(str(user_id))
    group_ids = await get_user_group_ids(db, user_uuid)
    # The resolution every run that builds an agent's tools for a person uses.
    resolved = await resolve_agent_integration_bindings(db, agent, str(user_id))
    effective, origin = resolved.bindings, resolved.origins
    if pinned:
        await pin_bindings(
            db,
            effective,
            origin,
            pinned,
            acting_user_id=str(user_id),
            used_providers=list(agent.integration_template_bindings or []),
        )

    composite = CompositeToolExecutor()
    if effective:
        definitions, executor = await build_integration_tools(
            db,
            resolved.tool_bindings,
            context={
                "source": swi.SCHEDULED_TRIGGER_TYPE,
                "agent": agent.name,
                "agent_id": str(agent.id),
                "user_id": str(user_id),
                "binding_origin_map": origin,
                "autonomous": True,
                "hitl_enabled": False,
                "element_scope": agent_run_scope(
                    getattr(agent, "integration_element_bindings", None),
                    getattr(agent, "integration_element_enforcement", None),
                    None,
                ).enforced,
                READ_ONLY_CONTEXT_KEY: read_only_integration_ids(agent, effective),
                SIGNATURE_CONTEXT_KEY: getattr(agent, "integration_signatures", None),
                LIVE_WRITES_CONTEXT_KEY: live_writes_overrides(agent, effective),
            },
        )
        allowed = agent.integration_allowed_tools or {}
        if allowed:
            definitions = filter_integration_tools(definitions, allowed)
        if definitions:
            composite.add_tools(definitions, executor)
    if agent.channel_config:
        channel_definitions, channel_executor = await build_channel_tools(
            db,
            agent.channel_config,
            context={"source": swi.SCHEDULED_TRIGGER_TYPE, "agent": agent.name},
        )
        if channel_definitions:
            composite.add_tools(channel_definitions, channel_executor)
    if agent.custom_tool_ids and not security_option("disable_custom_tools"):
        from app.services.variable_resolver import load_variables

        variables = await load_variables(db, str(user_id), group_ids)
        await load_custom_tools(
            db,
            list(agent.custom_tool_ids),
            composite,
            variables=variables,
            user_id=str(user_id),
        )
    kept = apply_tool_deny_list(
        filter_builtin_tools(composite.all_tools, getattr(agent, "builtin_allowed_tools", None)),
        agent.tool_deny_list,
    )
    return ExactToolset(composite=composite, names=[d.name for d in kept])


def argument_errors(definition, arguments: dict) -> list[str]:
    """The ways *arguments* break the tool's input schema (at most three)."""
    schema = getattr(definition, "input_schema", None)
    if not isinstance(schema, dict) or not schema:
        return []
    from jsonschema import Draft7Validator, SchemaError

    try:
        Draft7Validator.check_schema(schema)
    except SchemaError:
        return []
    found: list[str] = []
    for error in Draft7Validator(schema).iter_errors(arguments):
        where = ".".join(str(p) for p in error.absolute_path) or "arguments"
        found.append(f"{where}: {error.message}")
        if len(found) >= 3:
            break
    return found


async def approval_refusal(db, agent, user_id: str, toolset: ExactToolset, call) -> str | None:
    """The approval gate's answer for *call*: a sentence when it would ask a person."""
    from app.services.hitl_whitelist import build_approval_gate, resolve_approve_tool_calls

    gate = await build_approval_gate(
        db,
        toolset.composite,
        user_id=str(user_id),
        hitl_enabled=await resolve_approve_tool_calls(db, agent),
    )
    if gate is None:
        return None
    toolset.composite.set_approval_gate(gate)
    proposals = toolset.composite.screen_for_approval([call])
    if proposals and proposals[0].get("always_asks"):
        # No setting clears this call, and an instruction turn would
        # stop on it too: naming either would send the reader to a lever
        # that does nothing.
        return (
            f"A person must approve every '{call.name}' call before it runs, and a "
            "scheduled exact action has nobody to ask: nothing was sent. Ask for it "
            "in a conversation instead."
        )
    if proposals:
        return (
            f"{agent.name} asks a person to approve '{call.name}' before it runs, and a "
            "scheduled exact action has nobody to ask: nothing was sent. Allow the tool "
            "for this agent in Settings > Access and security > Tool approvals, or schedule an instruction "
            "instead."
        )
    return None


async def check_exact_action(db, agent, user_id: str, action: dict) -> tuple[dict, str | None]:
    """At scheduling time: the action with its tool's canonical name, or the refusal.

    The same resolution the run will use, so an action that cannot run is
    refused while the person is still in the conversation.
    """
    from app.tools.base import ToolCall

    toolset = await resolve_exact_toolset(
        db, agent, user_id, pinned=action.get("pinned_integrations")
    )
    definition = toolset.definition(action["tool"])
    if definition is None:
        offered = ", ".join(sorted(toolset.names)[:25]) or "none"
        return action, (
            f"'{action['tool']}' is not a tool {agent.name} can run for you, so nothing "
            "was scheduled. An exact action runs one of the agent's integration, channel "
            f"or custom tools: {offered}. For anything else, schedule an instruction."
        )
    errors = argument_errors(definition, action.get("arguments") or {})
    if errors:
        return action, (
            f"The arguments do not fit '{definition.name}', so nothing was scheduled: "
            + "; ".join(errors)
        )
    action = {**action, "tool": definition.name}
    call = ToolCall(id="schedule-check", name=definition.name, arguments=action.get("arguments") or {})
    refusal = await approval_refusal(db, agent, user_id, toolset, call)
    return action, refusal


# ── Dispatch ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _DueItem:
    """A due item read for the tick, as plain values: a rollback must not expire it."""

    id: uuid.UUID
    owner_agent_id: uuid.UUID | None
    user_id: uuid.UUID | None
    action_mode: str
    due_at: datetime | None
    title: str
    payload: dict | None


def _run_prompt(item: _DueItem) -> str:
    action = swi.action_of(item.payload) or {}
    if item.action_mode == "instruction":
        return str(action.get("prompt") or "")
    import json

    return (
        f"Scheduled exact action '{item.title}': {action.get('tool')} "
        f"{json.dumps(action.get('arguments') or {}, ensure_ascii=False)[:4000]}"
    )


async def _execution_deadline(db, item: _DueItem, now: datetime) -> datetime:
    # The same attempt count the execution is created with, so the deadline
    # leaves room for every resume after a wordless loss.
    attempts = max(execution_max_attempts(), 1)
    per_attempt = _EXACT_ATTEMPT_SECONDS
    if item.action_mode == "instruction" and item.owner_agent_id:
        from app.services.sub_agent import resolve_chat_sub_agent_timeout

        per_attempt = int(
            await resolve_chat_sub_agent_timeout(db, str(item.owner_agent_id)) or per_attempt
        )
    retry = max(int(settings.scheduled_work_item_retry_seconds or 0), 1)
    return now + timedelta(
        seconds=attempts * (per_attempt + retry) + settings.orchestration_grace_seconds
    )


async def publish_transition(
    work_item_id: object,
    *,
    previous_status: str | None,
    actor_agent_id: object = None,
    actor_run_id: object = None,
) -> None:
    """Raise the work_item.* events of a scheduled item's committed status change; never raises.

    *actor_agent_id* / *actor_run_id* name the scheduled run that made the
    change (the item's owner agent and its run), so an assignment ignoring its
    own events is not woken by them; a change the scheduler made with no run
    (a missed item) leaves both ``None``.
    """
    from app.services import work_item as work_item_service

    if previous_status is None:
        return
    await work_item_service.publish_transition(
        work_item_id, previous_status=previous_status, actor_agent_id=actor_agent_id, actor_run_id=actor_run_id
    )


async def _block(
    db,
    item: WorkItem,
    run: AssignmentRun | None,
    *,
    code: str,
    reason: str,
    now: datetime,
) -> None:
    item.status = "blocked"
    item.outcome_code = code
    item.result = _clip(reason)
    item.claimed_by_agent_id = None
    item.last_activity_at = now
    if run is not None and run.status not in _TERMINAL_RUN_STATUSES:
        run.status = "failed"
        run.error = _clip(reason, 2000)
        run.completed_at = now


async def _mark_missed(db, now: datetime) -> list[uuid.UUID]:
    cutoff = now - swi.grace_window()
    candidates = (
        await db.execute(
            select(WorkItem.id)
            .where(
                WorkItem.action_mode.is_not(None),
                WorkItem.status == "open",
                WorkItem.due_at < cutoff,
            )
            .order_by(WorkItem.due_at)
            .limit(200)
        )
    ).scalars().all()
    if not candidates:
        return []
    minutes = int(swi.grace_window().total_seconds() // 60)
    reason = (
        f"The platform could not run it within {minutes} minutes of its time (it was "
        "unavailable, or scheduling was turned off), so it was not run late. "
        "Reschedule it to run it, or cancel it."
    )
    missed = (
        await db.execute(
            update(WorkItem)
            .where(
                WorkItem.id.in_(candidates),
                WorkItem.status == "open",
                WorkItem.action_mode.is_not(None),
                WorkItem.due_at < cutoff,
            )
            .values(
                status="blocked",
                outcome_code=swi.OUTCOME_MISSED,
                result=reason,
                last_activity_at=now,
            )
            .returning(WorkItem.id)
        )
    ).scalars().all()
    await db.commit()
    return list(missed)


async def _settle_from_runs(db, now: datetime) -> list[tuple[uuid.UUID, str, object, object]]:
    """Settle the running items whose run ended without settling them.

    One (id, status before, owner agent, run id) per settled item: the item's
    scheduled run is the writer its events name when that run still exists;
    when it is gone, the scheduler alone wrote the settlement and both are
    ``None`` (as for a missed item).
    """
    rows = (
        await db.execute(
            select(WorkItem)
            .where(
                WorkItem.action_mode.is_not(None),
                WorkItem.status.in_(("claimed", "in_progress")),
                WorkItem.last_activity_at < now - timedelta(minutes=2),
            )
            .order_by(WorkItem.last_activity_at)
            .limit(100)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    settled: list[tuple[uuid.UUID, str, object, object]] = []
    for item in rows:
        run = (
            await db.execute(select(AssignmentRun).where(AssignmentRun.id == item.run_id))
        ).scalar_one_or_none() if item.run_id else None
        if run is not None and run.status not in _TERMINAL_RUN_STATUSES:
            continue
        previous = item.status
        if run is not None and run.status == "completed":
            item.status = "done"
            item.outcome_code = None
            item.result = _clip(run.result_summary) or "Done."
            item.last_activity_at = now
        elif run is not None and run.status == "failed":
            # Its run failed without settling it: the process running it died
            # (a restart, a drain, a crash) and the stale-run sweep ended it.
            # Never the person's to handle: its agent runs it again.
            await _requeue_in_session(
                db, item, run, cause=_clip(run.error, 500) or "its worker stopped", now=now
            )
        else:
            reason = (
                "Its run was stopped (an emergency stop or a cancelled run) before it reported "
                "an outcome, so it was not run again."
                if run is not None
                else "Its run was deleted before it reported an outcome, so it was not run again."
            )
            await _block(db, item, None, code=swi.OUTCOME_INTERRUPTED, reason=reason, now=now)
        writer = (item.owner_agent_id, run.id) if run is not None else (None, None)
        settled.append((item.id, previous, *writer))
    await db.commit()
    return settled


#: The work-item statuses after which nothing is left for an action to serve.
_SETTLED_WORK_ITEM_STATUSES = frozenset({"done", "cancelled"})
TARGET_ALREADY_DONE = "target already done"
#: Blocked scheduled items read per tick: one keyset batch, resumed from
#: ``_leftover_cursor`` at the next tick so every row is reached in turn.
_LEFTOVER_BATCH_SIZE = 500
#: The last blocked item id read by the previous tick (``None``: start over).
#: Process-local: a restart, or another replica, only restarts the walk, and
#: the ``skip_locked`` lock keeps two replicas from cancelling the same row.
_leftover_cursor: uuid.UUID | None = None


async def _target_statuses(
    db, served: Any, *, lock: bool = False
) -> dict[tuple[uuid.UUID, uuid.UUID | None], str]:
    """The status of each served work item, keyed by (id, person)."""
    wanted = {target for targets in served for target in targets}
    if not wanted:
        return {}
    query = select(WorkItem.id, WorkItem.user_id, WorkItem.status).where(WorkItem.id.in_(wanted))
    if lock:
        query = query.order_by(WorkItem.id).with_for_update(read=True)
    return {(row.id, row.user_id): row.status for row in (await db.execute(query)).all()}


def _settled_targets(
    served: list[uuid.UUID],
    user_id: uuid.UUID | None,
    statuses: dict[tuple[uuid.UUID, uuid.UUID | None], str],
) -> dict[uuid.UUID, str]:
    """The person's served items and their statuses when all are settled, else empty."""
    targets = {target: statuses[(target, user_id)] for target in served if (target, user_id) in statuses}
    if targets and all(status in _SETTLED_WORK_ITEM_STATUSES for status in targets.values()):
        return targets
    return {}


async def _cancel_served_leftovers(db, now: datetime) -> list[uuid.UUID]:
    """Cancel the blocked actions whose work item is already settled.

    A blocked or missed action (a platform restart cut its run, or it came due
    while the platform was down) that serves work items of the same person
    (``swi.served_work_item_ids``) has nothing left to do once every one of
    them is done or cancelled: the scheduler cancels it, notes "target already
    done" and audits it, so no leftover waits for a click. An id that names no
    work item of the person is not a target.
    """
    from app.services.audit import log_system_audit

    global _leftover_cursor
    # Bounded per tick, resumed from a cursor: ONE keyset batch read without a
    # lock (most blocked items wait for a person and serve no settled item),
    # the next tick continuing after it and wrapping at the end, so the whole
    # population is reached every ceil(N / batch) ticks and no row is starved.
    query = select(WorkItem).where(WorkItem.action_mode.is_not(None), WorkItem.status == "blocked")
    if _leftover_cursor is not None:
        query = query.where(WorkItem.id > _leftover_cursor)
    batch = (await db.execute(query.order_by(WorkItem.id).limit(_LEFTOVER_BATCH_SIZE))).scalars().all()
    _leftover_cursor = batch[-1].id if len(batch) == _LEFTOVER_BATCH_SIZE else None
    served = {item.id: swi.served_work_item_ids(item) for item in batch}
    statuses = await _target_statuses(db, served.values())
    candidates = {
        item.id: served[item.id]
        for item in batch
        if _settled_targets(served[item.id], item.user_id, statuses)
    }
    if not candidates:
        await db.rollback()
        return []
    rows = (
        await db.execute(
            select(WorkItem)
            .where(WorkItem.id.in_(candidates), WorkItem.status == "blocked")
            .order_by(WorkItem.id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    # Re-read the targets under a share lock once the rows to cancel are held:
    # a target requeued since the unlocked scan is seen (or waits for this
    # commit), so an action whose work item came back is never cancelled.
    locked_statuses = await _target_statuses(db, (candidates[item.id] for item in rows), lock=True)
    cancelled: list[uuid.UUID] = []
    for item in rows:
        targets = _settled_targets(candidates[item.id], item.user_id, locked_statuses)
        if not targets:
            continue
        previous_code = item.outcome_code
        item.status = "cancelled"
        item.outcome_code = None
        item.result = _clip(
            f"Cancelled by the scheduler on {now:%Y-%m-%d %H:%M} UTC: {TARGET_ALREADY_DONE} "
            f"(work item {', '.join(str(t) for t in targets)}: "
            f"{' / '.join(sorted(set(targets.values())))}). Nothing was run."
        )
        item.claimed_by_agent_id = None
        item.claimed_at = None
        item.last_activity_at = now
        if item.user_id is not None:
            await log_system_audit(
                db,
                actor="scheduler",
                anchor_user_id=item.user_id,
                action="cancel",
                entity_type="work_item",
                entity_id=item.id,
                entity_name=item.title,
                summary=f"Cancelled the scheduled work item '{item.title}': {TARGET_ALREADY_DONE}",
                details={
                    "scheduled": True,
                    "reason": TARGET_ALREADY_DONE,
                    "previous_outcome_code": previous_code,
                    "served_work_items": {str(t): s for t, s in targets.items()},
                },
            )
        cancelled.append(item.id)
    await db.commit()
    return cancelled


async def _create_run(db, item, *, now: datetime, extra_context: dict | None = None) -> uuid.UUID:
    """ONE assignment-less run of *item* and its execution, keyed ``assignment_run:<run id>``, in *db*'s transaction.

    *item* is a due item or a locked :class:`WorkItem`. *extra_context* goes
    on the run's ``trigger_context``: a fresh run for the owner agent after
    restarts carries its prompt and re-queue count there. The caller
    commits.
    """
    from app.services.audit import log_system_audit
    from app.services.orchestration_engine import create_execution

    due = _DueItem(item.id, item.owner_agent_id, item.user_id, item.action_mode, item.due_at, item.title, item.payload)
    requeued = bool(extra_context and extra_context.get(OWNER_PROMPT_KEY))
    run = AssignmentRun(
        assignment_id=None,
        agent_id=due.owner_agent_id,
        user_id=due.user_id,
        is_company_managed=False,
        status="pending",
        trigger_payload={
            "trigger_type": swi.SCHEDULED_TRIGGER_TYPE,
            "work_item_id": str(due.id),
        },
        trigger_context={
            "type": swi.SCHEDULED_TRIGGER_TYPE,
            "work_item_id": str(due.id),
            "mode": due.action_mode,
            "title": due.title[:300],
            "due_at": due.due_at.isoformat() if due.due_at else None,
            **(extra_context or {}),
        },
        messages=[
            {
                "role": "user",
                "content": extra_context[OWNER_PROMPT_KEY] if requeued else _run_prompt(due),
            }
        ],
    )
    db.add(run)
    await db.flush()
    await db.execute(update(WorkItem).where(WorkItem.id == due.id).values(run_id=run.id))
    if requeued:
        # A fresh run of the owner agent runs an instruction, whatever the mode.
        due = _DueItem(due.id, due.owner_agent_id, due.user_id, "instruction", due.due_at, due.title, due.payload)
    await create_execution(
        db,
        adapter=swi.ADAPTER_NAME,
        payload={"run_id": str(run.id), "work_item_id": str(due.id)},
        deadline_at=await _execution_deadline(db, due, now),
        dedup_key=f"assignment_run:{run.id}",
        max_attempts=execution_max_attempts(),
    )
    await log_system_audit(
        db,
        actor="scheduler",
        anchor_user_id=due.user_id,
        action="trigger",
        entity_type="work_item",
        entity_id=due.id,
        entity_name=due.title[:255],
        summary=(
            f"Ran the scheduled work item '{due.title[:200]}' again for its agent after platform restarts"
            if requeued
            else f"Dispatched the scheduled work item '{due.title[:200]}'"
        ),
        details={"scheduled": True, "run_id": str(run.id), "mode": item.action_mode, "requeued": requeued},
    )
    return run.id


async def dispatch_due_scheduled_work_items(db) -> int:
    """Step 3c of the scheduler tick: settle, mark missed, dispatch, then cancel served leftovers."""
    from app.services.orchestration_engine import notify_orchestration_wake
    from app.services.work_item import claim_open_work_item
    from app.services.work_item import publish_transition as publish_claim

    now = _now()
    settled = await _settle_from_runs(db, now)
    missed = await _mark_missed(db, now)
    # Settled and missed items raise their work_item.* events after the commit.
    for item_id, previous, owner_agent_id, item_run_id in settled:
        await publish_transition(
            item_id, previous_status=previous, actor_agent_id=owner_agent_id, actor_run_id=item_run_id
        )
    # A missed item never ran: the scheduler alone wrote it, so no actor.
    for item_id in missed:
        await publish_transition(item_id, previous_status="open")
    to_deliver = [*(entry[0] for entry in settled), *missed]
    dispatched = 0
    if swi.scheduling_enabled():
        per_tick = max(int(settings.scheduled_work_item_dispatch_per_tick or 0), 1)
        due = [
            _DueItem(*row)
            for row in (
                await db.execute(
                    select(
                        WorkItem.id,
                        WorkItem.owner_agent_id,
                        WorkItem.user_id,
                        WorkItem.action_mode,
                        WorkItem.due_at,
                        WorkItem.title,
                        WorkItem.payload,
                    )
                    .where(
                        WorkItem.action_mode.is_not(None),
                        WorkItem.status == "open",
                        WorkItem.due_at <= now,
                        WorkItem.due_at >= now - swi.grace_window(),
                    )
                    .order_by(WorkItem.due_at, WorkItem.created_at)
                    .limit(per_tick)
                )
            ).all()
        ]
        for item in due:
            try:
                if item.owner_agent_id is None or item.user_id is None:
                    await db.execute(
                        update(WorkItem)
                        .where(WorkItem.id == item.id, WorkItem.status == "open")
                        .values(
                            status="blocked",
                            outcome_code=swi.OUTCOME_AGENT_UNAVAILABLE,
                            result="The agent that scheduled it no longer exists, so it was not run.",
                            last_activity_at=now,
                        )
                    )
                    await db.commit()
                    await publish_claim(item.id, previous_status="open")
                    to_deliver.append(item.id)
                    continue
                if not await claim_open_work_item(
                    db,
                    item.id,
                    agent_id=item.owner_agent_id,
                    now=now,
                    conditions=(WorkItem.action_mode.is_not(None), WorkItem.due_at <= now),
                ):
                    # Taken by another tick (another replica) or cancelled meanwhile.
                    continue
                run_id = await _create_run(db, item, now=now)
                await db.commit()
                await publish_claim(
                    item.id, previous_status="open", claimed_by_agent_id=item.owner_agent_id, actor_run_id=run_id
                )
                dispatched += 1
            except Exception as exc:
                await db.rollback()
                logger.error(
                    "Could not dispatch scheduled work item %s: %s",
                    item.id,
                    format_exception_message(exc),
                    exc_info=True,
                )
        if dispatched:
            await notify_orchestration_wake()
            logger.info("Dispatched %d scheduled work item(s)", dispatched)
    # Cleanup after dispatch, so a due item never waits on it.
    leftovers = await _cancel_served_leftovers(db, now)
    for item_id in leftovers:
        await publish_transition(item_id, previous_status="blocked")
    # An item settled or missed this tick can also be a served leftover: one
    # delivery, of its final state.
    for item_id in dict.fromkeys([*to_deliver, *leftovers]):
        await swi.deliver_outcome(item_id)
    return dispatched


# ── Execution ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RunOutcome:
    """What one attempt came to, for the adapter."""

    kind: str  # "done" | "blocked" | "retry" | "ended"
    error: str | None = None
    backoff_seconds: float | None = None


@dataclass(frozen=True)
class _Refusal:
    code: str
    reason: str
    transient: bool = False


async def _run_time_refusal(db, item: WorkItem) -> _Refusal | None:
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.agent import Agent
    from app.models.user import User
    from app.services.agentic_gate import UNKNOWN_STATE_DETAIL, agentic_verdict, refusal_message

    if not swi.scheduling_enabled():
        return _Refusal(swi.OUTCOME_DISABLED, swi.SCHEDULING_DISABLED_MESSAGE + " Nothing was run.")
    try:
        swi.validate_action(swi.action_of(item.payload))
    except swi.ScheduledWorkItemError as exc:
        return _Refusal(swi.OUTCOME_VALIDATION, f"Its action is invalid: {exc} Nothing was run.")
    agent = (
        await db.execute(select(Agent).where(Agent.id == item.owner_agent_id))
    ).scalar_one_or_none() if item.owner_agent_id else None
    if agent is None or not agent.enabled:
        return _Refusal(
            swi.OUTCOME_AGENT_UNAVAILABLE,
            "The agent that scheduled it was deleted or disabled, so it was not run.",
        )
    user = (
        await db.execute(select(User).where(User.id == item.user_id))
    ).scalar_one_or_none() if item.user_id else None
    if user is None or not user.is_active:
        return _Refusal(
            swi.OUTCOME_OWNER_DEACTIVATED,
            "The account of the person who scheduled it is deactivated, so it was not run.",
        )
    group_ids = await get_user_group_ids(db, user.id)
    usable = (
        await db.execute(
            select(Agent.id).where(
                Agent.id == agent.id,
                user_visible_filter(Agent, user.id, group_ids, is_admin=user.is_admin),
            )
        )
    ).scalar_one_or_none()
    if usable is None:
        return _Refusal(
            swi.OUTCOME_PERMISSION,
            f"{user.full_name or user.email} can no longer use the agent {agent.name}, "
            "so it was not run.",
        )
    verdict = await agentic_verdict(db)
    if verdict.transient:
        return _Refusal(swi.OUTCOME_AI_UNAVAILABLE, UNKNOWN_STATE_DETAIL, transient=True)
    if verdict.refusal:
        return _Refusal(
            swi.OUTCOME_AI_UNAVAILABLE,
            f"{refusal_message(verdict.refusal)} It was not run.",
        )
    return None


@dataclass(frozen=True)
class _Executed:
    kind: str  # "done" | "blocked" | "retry"
    text: str = ""
    code: str | None = None
    messages: list | None = None
    actions: list | None = None


async def _execute_exact(agent_id, user_id: str, action: dict, run_id: str) -> _Executed:
    from app.core.database import async_session
    from app.models.agent import Agent
    from app.tools.base import ToolCall

    async with async_session() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
        toolset = await resolve_exact_toolset(
            db, agent, user_id, pinned=action.get("pinned_integrations")
        )
        definition = toolset.definition(action["tool"])
        if definition is None:
            return _Executed(
                "blocked",
                f"'{action['tool']}' is no longer a tool {agent.name} can run for this person: "
                "its integration was disconnected, revoked or is no longer shared, or the "
                "tool was removed from the agent. Nothing was sent.",
                code=swi.OUTCOME_BINDING,
            )
        arguments = action.get("arguments") or {}
        errors = argument_errors(definition, arguments)
        if errors:
            return _Executed(
                "blocked",
                f"The stored arguments no longer fit '{definition.name}': "
                + "; ".join(errors)
                + ". Nothing was sent.",
                code=swi.OUTCOME_VALIDATION,
            )
        call = ToolCall(
            id=f"scheduled-{str(run_id)[:8]}",
            name=definition.name,
            arguments=copy.deepcopy(arguments),
        )
        refusal = await approval_refusal(db, agent, user_id, toolset, call)
        if refusal:
            return _Executed("blocked", refusal, code=swi.OUTCOME_APPROVAL)
        result = await toolset.composite.execute_tool(call)
        await db.commit()
    content = result.content if isinstance(result.content, str) else str(result.content or "")
    record = [
        {"role": "assistant", "content": "", "tool_calls": [{"name": call.name, "arguments": arguments}]},
        {"role": "tool", "name": call.name, "content": _clip(content, 20000)},
    ]
    actions = [{"tool": call.name, "source": definition.source}]
    if result.is_error and getattr(result, "target_throttled", False):
        return _Executed("retry", _clip(content), messages=record, actions=actions)
    if result.is_error:
        return _Executed(
            "blocked",
            f"'{call.name}' returned an error, so it may not have been done: {_clip(content, 1500)}",
            code=swi.OUTCOME_EXECUTION,
            messages=record,
            actions=actions,
        )
    return _Executed("done", _clip(content) or "Done.", messages=record, actions=actions)


def _tool_calls_in(messages: list) -> list[dict]:
    calls: list[dict] = []
    for message in messages or []:
        for call in getattr(message, "tool_calls", None) or []:
            name = getattr(call, "name", None)
            if name:
                calls.append({"tool": name})
    return calls


async def _execute_instruction(
    agent_id,
    user_id: str,
    action: dict,
    run_id: str,
    *,
    attempt: int,
    cancel_event: asyncio.Event | None,
    deadline_at: datetime | None,
    receipt: str | None = None,
    replay: Any = None,
    place: tuple[int, int] | None = None,
    trace_meta: dict | None = None,
) -> _Executed:
    """Run the instruction turn; its checkpoint outlives it only when it is handed back.

    On every other exit (done, blocked, retry, timeout, an exception) the
    checkpoint is closed and discarded in ``finally``, so a later attempt never
    replays the transcript of a failed one.
    """
    from app.services import assignment_run_resume

    # Its completed tool rounds are checkpointed at every iteration boundary,
    # so a restart resumes it from them instead of starting again.
    # With its acting user, a hand-back or a stop of the worker keeps an oversized
    # result whole as a file stamped with the run, which the resumed attempt reads.
    checkpoint = assignment_run_resume.checkpoint_for(
        run_id, replay=replay, attempt=attempt, place=place, user_id=user_id
    )
    history = assignment_run_resume.restore_history(list(replay.history)) if replay is not None else []
    executed: _Executed | None = None
    try:
        executed = await _execute_instruction_body(
            agent_id,
            user_id,
            action,
            run_id,
            attempt=attempt,
            cancel_event=cancel_event,
            deadline_at=deadline_at,
            receipt=receipt,
            place=place,
            trace_meta=trace_meta,
            checkpoint=checkpoint,
            history=history,
        )
        return executed
    finally:
        if executed is None or executed.kind != HANDED_BACK:
            await _drop_checkpoint(run_id, checkpoint, place)


async def _drop_checkpoint(run_id: str, checkpoint: Any, place: tuple[int, int] | None) -> None:
    """Close and discard a turn's checkpoint, best effort: nothing is left to resume."""
    from app.services import assignment_run_resume

    try:
        await checkpoint.aclose(flush=False)
        await assignment_run_resume.discard(run_id, place=place)
    except Exception as exc:
        logger.warning("Could not discard the checkpoint of run %s: %s", run_id, format_exception_message(exc))


async def _execute_instruction_body(
    agent_id,
    user_id: str,
    action: dict,
    run_id: str,
    *,
    attempt: int,
    cancel_event: asyncio.Event | None,
    deadline_at: datetime | None,
    receipt: str | None,
    place: tuple[int, int] | None,
    trace_meta: dict | None,
    checkpoint: Any,
    history: list,
) -> _Executed:
    import contextlib

    from app.core.database import async_session
    from app.core.worker_shutdown import TurnWoundDown
    from app.llm.provider_access import acting_for
    from app.models.agent import Agent
    from app.services import assignment_run_resume
    from app.services.agent_chat import QUOTA_STORE_OUTAGE_KEY, run_agent_chat
    from app.services.builtin_tools import serialize_messages
    from app.services.failure_causes import FailureContext, classify_run_failure
    from app.services.langfuse_tracing import next_root_trace
    from app.services.orchestration_reconciler import worker_stopping
    from app.services.sub_agent import resolve_chat_sub_agent_timeout

    messages_out: list = []
    error_out: dict = {}
    loop_outcome: dict = {}
    async with async_session() as db:
        agent = (await db.execute(select(Agent).where(Agent.id == agent_id))).scalar_one()
        timeout = float(await resolve_chat_sub_agent_timeout(db, str(agent.id), agent=agent) or 1800)
        if deadline_at is not None:
            timeout = max(min(timeout, (deadline_at - _now()).total_seconds() - 30), 30)
        try:
            # The one-offs this turn schedules inherit this one's ``notify``
            # (a silent check that re-schedules itself stays silent).
            with (
                acting_for(user_id),
                swi.running_one_off(action),
                (next_root_trace(trace_meta) if trace_meta else contextlib.nullcontext()),
            ):
                text, _ = await asyncio.wait_for(
                    run_agent_chat(
                        db,
                        user_id,
                        agent,
                        history,
                        action["prompt"],
                        restore_user_message=True,
                        parent_run_id=str(run_id),
                        messages_out=messages_out,
                        error_out=error_out,
                        cancel_event=cancel_event,
                        quota_receipt_token=receipt or receipt_token(run_id, attempt),
                        pinned_integrations=action.get("pinned_integrations"),
                        loop_outcome_out=loop_outcome,
                        on_checkpoint=checkpoint.offer,
                    ),
                    timeout=timeout,
                )
        except TurnWoundDown:
            # Its worker was asked to stop (a rollout, a scale-in, a node
            # drain): the turn stopped at an iteration boundary. Handed back,
            # resumed by the next worker from its checkpoint.
            await assignment_run_resume.hand_back(run_id, checkpoint=checkpoint, attempt=attempt, place=place)
            return _Executed(HANDED_BACK, assignment_run_resume.HANDED_BACK_ERROR)
        except TimeoutError:
            return _Executed(
                "blocked",
                f"The agent did not finish within {int(timeout)} seconds; what it did before "
                "the limit was kept.",
                code=swi.OUTCOME_EXECUTION,
                messages=serialize_messages(messages_out),
                actions=_tool_calls_in(messages_out),
            )
        except Exception as exc:
            ran_tools = bool(_tool_calls_in(messages_out))
            cause = classify_run_failure(exc, FailureContext()).cause.value
            if cause in _RETRY_FAILURE_CAUSES and not ran_tools:
                return _Executed("retry", format_exception_message(exc))
            return _Executed(
                "blocked",
                f"The agent's run failed: {format_exception_message(exc)}",
                code=swi.OUTCOME_EXECUTION,
                messages=serialize_messages(messages_out),
                actions=_tool_calls_in(messages_out),
            )
    if loop_outcome.get("stop_reason") == "cancelled" and worker_stopping():
        # Cut mid call by its stopping worker (the engine took the execution
        # back from the draining worker): the same graceful stop, handed back.
        await assignment_run_resume.hand_back(run_id, checkpoint=checkpoint, attempt=attempt, place=place)
        return _Executed(HANDED_BACK, assignment_run_resume.HANDED_BACK_ERROR)
    record = serialize_messages(messages_out)
    actions = _tool_calls_in(messages_out)
    if error_out.get(QUOTA_STORE_OUTAGE_KEY):
        return _Executed("retry", str(error_out.get("error") or "quota store unavailable"))
    code = error_out.get("error_code")
    if code in ("quota_exceeded", "quota_unavailable"):
        return _Executed("blocked", str(error_out.get("error") or text), code=swi.OUTCOME_QUOTA)
    if code:
        return _Executed(
            "blocked",
            str(error_out.get("error") or text),
            code=swi.OUTCOME_EXECUTION,
            messages=record,
            actions=actions,
        )
    if cancel_event is not None and cancel_event.is_set():
        return _Executed(
            "blocked",
            "It was stopped while it ran (an emergency stop or a cancelled run).",
            code=swi.OUTCOME_INTERRUPTED,
            messages=record,
            actions=actions,
        )
    return _Executed("done", _clip(text) or "Done.", messages=record, actions=actions)


async def _locked(db, run_id: uuid.UUID) -> tuple[AssignmentRun | None, WorkItem | None]:
    run = (
        await db.execute(
            select(AssignmentRun)
            .where(AssignmentRun.id == run_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if run is None:
        return None, None
    item_uuid = safe_uuid((run.trigger_payload or {}).get("work_item_id"))
    item = None
    if item_uuid is not None:
        item = (
            await db.execute(
                select(WorkItem)
                .where(WorkItem.id == item_uuid)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    return run, item


async def _credit_attempts(run_id: str, attempts: range, reason: str) -> None:
    from app.services.quota import refund_agentic_usage_by_receipt

    for attempt in attempts:
        await refund_agentic_usage_by_receipt(receipt_token(run_id, attempt), reason=reason)


async def execute_scheduled_run(
    run_id: str,
    *,
    attempt: int = 1,
    cancel_event: asyncio.Event | None = None,
    deadline_at: datetime | None = None,
    taken_back_on_its_word: bool = False,
) -> RunOutcome:
    """Run one attempt of a scheduled item's run; settle the item from its outcome.

    *taken_back_on_its_word*: this claim follows a take-back from a worker that
    said it was stopping (or a deploy replaced), so its resume is graceful.
    """
    from app.core.database import async_session

    run_uuid = safe_uuid(run_id)
    if run_uuid is None:
        return RunOutcome("ended", "The execution names no run.")
    attempt = max(int(attempt or 1), 1)
    place = await _place_attempt(run_uuid, attempt)
    async with async_session() as db:
        run, item = await _locked(db, run_uuid)
        if run is None:
            return RunOutcome("ended", "The run of this scheduled work item no longer exists.")
        if run.status in _TERMINAL_RUN_STATUSES:
            return RunOutcome("ended")
        now = _now()
        if item is None or item.action_mode is None:
            run.status = "failed"
            run.error = "Its scheduled work item was deleted before it ran."
            run.completed_at = now
            await db.commit()
            return RunOutcome("ended", run.error)
        if item.status in ("done", "cancelled"):
            run.status = "cancelled"
            run.error = f"Its scheduled work item was {item.status} before it ran."
            run.completed_at = now
            await db.commit()
            return RunOutcome("ended")
        context = dict(run.trigger_context or {})
        previous = item.status
        # Started before and cut by a restart (a rollout, a drain, a crash):
        # it goes on by itself, never handed to the person.
        resuming = bool(context.get("action_started_at"))
        decision = None
        cut_receipt = cut_turn_receipt(run.id, context) if resuming else None
        if resuming:
            action_now = swi.action_of(item.payload) or {}
            read_only = (
                item.action_mode == "exact"
                and not context.get(OWNER_PROMPT_KEY)
                and await _is_read_only_tool(db, action_now.get("tool"))
            )
            decision, why = resume_decision(
                context,
                attempt=attempt,
                mode=item.action_mode,
                read_only=read_only,
                taken_back_on_its_word=taken_back_on_its_word,
            )
            if decision == REQUEUE:
                outcome = await _requeue_for_owner(db, item, run, cause=why or "a platform restart", now=now)
                await _credit_cut_turn(cut_receipt, "scheduled run cut by a restart, run again for its agent")
                return outcome
        refusal = await _run_time_refusal(db, item)
        if refusal is not None and refusal.transient:
            await db.rollback()
            return RunOutcome(
                "retry",
                refusal.reason,
                backoff_seconds=float(settings.scheduled_work_item_retry_seconds or 60),
            )
        if refusal is not None:
            await _block(db, item, run, code=refusal.code, reason=refusal.reason, now=now)
            actor = {"actor_agent_id": item.owner_agent_id, "actor_run_id": run_uuid}
            await db.commit()
            await publish_transition(item.id, previous_status=previous, **actor)
            await swi.deliver_outcome(item.id)
            return RunOutcome("blocked", refusal.reason)
        from app.services import assignment_run_resume

        replay = None
        if decision in (RESUME, RERUN):
            # The resume recorded on the run (cause, graceful or not, counts)
            # with the checkpoint of the turn it goes on from.
            replay = await assignment_run_resume.prepare_resume(
                run, attempt=attempt, taken_back_on_its_word=taken_back_on_its_word
            )
            item.payload = _with_restart(item.payload, "resumed")
        context = dict(run.trigger_context or {})
        item.status = "in_progress"
        item.last_activity_at = now
        run.status = "running"
        run.started_at = run.started_at or now
        run.trigger_context = {**context, "action_started_at": now.isoformat(), "attempt": attempt}
        action = copy.deepcopy(swi.action_of(item.payload) or {})
        owner_prompt = context.get(OWNER_PROMPT_KEY)
        if owner_prompt:
            # A fresh run of the owner agent: the prompt it was given
            # with the previous outcome, never the stored call sent blind.
            action = {**action, "mode": "instruction", "prompt": owner_prompt}
        turn_receipt = turn_receipt_token(run_uuid, attempt, run.trigger_context)
        trace_meta = assignment_run_resume.resumed_trace_meta(run.id, run.trigger_context) if replay else None
        agent_id, user_id, item_id = item.owner_agent_id, str(item.user_id), item.id
        await db.commit()

    await publish_transition(item_id, previous_status=previous, actor_agent_id=agent_id, actor_run_id=run_uuid)
    await swi.publish_update(item)
    await _credit_cut_turn(cut_receipt, "scheduled run cut by a restart, resumed")
    try:
        if action.get("mode") == "exact":
            executed = await _execute_exact(agent_id, user_id, action, str(run_uuid))
        else:
            executed = await _execute_instruction(
                agent_id,
                user_id,
                action,
                str(run_uuid),
                attempt=attempt,
                cancel_event=cancel_event,
                deadline_at=deadline_at,
                receipt=turn_receipt,
                replay=replay,
                place=place,
                trace_meta=trace_meta,
            )
    except Exception as exc:
        logger.error(
            "Scheduled work item %s failed while running: %s",
            item_id,
            format_exception_message(exc),
            exc_info=True,
        )
        executed = _Executed(
            "blocked",
            f"It failed while running: {format_exception_message(exc)}",
            code=swi.OUTCOME_EXECUTION,
        )
    if executed.kind == HANDED_BACK:
        # Its worker is stopping: the run stays running, handed back with its
        # checkpoint flushed; the adapter makes it due again at once.
        return RunOutcome(HANDED_BACK, executed.text)
    return await _settle(run_uuid, executed, place=place)


async def _place_attempt(run_uuid: uuid.UUID, attempt: int) -> tuple[int, int] | None:
    """This attempt's ``(epoch, attempt)`` place on the run, recorded in its own transaction.

    It fences the checkpoint writes of an attempt this one replaced
    (as an assignment run's). ``None`` when the run is gone.
    """
    from app.core.database import async_session
    from app.services.attempt_charges import fence_attempt

    async with async_session() as db:
        run = (await db.execute(select(AssignmentRun).where(AssignmentRun.id == run_uuid))).scalar_one_or_none()
        if run is None:
            return None
        place = await fence_attempt(db, run, attempt=attempt)
        await db.commit()
        return place


async def _is_read_only_tool(_db, tool: str | None) -> bool:
    """Whether *tool* only reads, by the product's own classification.

    Only the product-owned classification counts: the curated shipped reads
    and the platform reads the classifier derives. An administrator's
    addition to the approval whitelist is an approval exemption, not a proof
    that a tool only reads, so it never makes a call run again; nor does a
    connected MCP server's own annotation. Any doubt (unreadable, absent) is
    "may change something": the owner agent gets it, never a blind resend.
    Read on its own session: a failed read never touches the caller's locks.
    """
    if not tool:
        return False
    if tool in _product_read_tools():
        return True
    from app.core.database import async_session
    from app.services.hitl_whitelist import platform_read_default

    try:
        async with async_session() as db:
            return tool in await platform_read_default(db)
    except Exception as exc:
        logger.warning(
            "Could not read whether %s only reads, so it is not run again: %s", tool, format_exception_message(exc)
        )
        return False


def _product_read_tools() -> frozenset[str]:
    """The curated reads the product ships, whatever an administrator changed."""
    from app.services.hitl_whitelist_default import SHIPPED_DEFAULT_WHITELIST

    return SHIPPED_DEFAULT_WHITELIST


async def _requeue_in_session(
    db, item: WorkItem, run: AssignmentRun, *, cause: str, now: datetime
) -> tuple[str, str, uuid.UUID | None]:
    """End *run* and create the fresh instruction run of its owner agent in *db*'s transaction.

    ``(REQUEUED, text, fresh run id)``, or ``("blocked", reason, None)`` past
    :data:`MAX_OWNER_REQUEUES`: the item then says what happened and that the
    platform stopped, with nothing asked of the person. The caller commits.
    """
    from app.services import assignment_run_resume

    context = dict(run.trigger_context or {})
    lost, graceful = assignment_run_resume.resume_counts(context)
    count = _requeue_count(context) + 1
    run_id = str(run.id)
    if count > MAX_OWNER_REQUEUES:
        reason = exhausted_reason(context, cause=cause)
        await _block(db, item, run, code=swi.OUTCOME_RESUME_EXHAUSTED, reason=reason, now=now)
        return "blocked", reason, None
    action = swi.action_of(item.payload) or {}
    prompt = owner_requeue_prompt(
        title=item.title,
        action=action,
        mode=item.action_mode,
        previous_outcome=run.error or run.result_summary or f"cut by {cause}",
        lost=lost,
        graceful=graceful,
        requeue=count,
    )
    if run.status not in _TERMINAL_RUN_STATUSES:
        run.status = "failed"
        run.error = _clip(f"Cut by platform restarts ({cause}); run again for its agent.", 2000)
        run.completed_at = now
    fresh = await _create_run(
        db,
        item,
        now=now,
        extra_context={
            OWNER_PROMPT_KEY: prompt,
            REQUEUE_KEY: {"count": count, "after_run_id": run_id, "cause": _clip(cause, 500)},
        },
    )
    item.payload = _with_restart(item.payload, "requeued")
    item.status = "claimed"
    item.claimed_by_agent_id = item.owner_agent_id
    item.last_activity_at = now
    logger.warning(
        "Scheduled work item %s: run %s cut by restarts (%s); run again for its agent as run %s "
        "(fresh run %d of %d)",
        item.id,
        run_id,
        cause,
        fresh,
        count,
        MAX_OWNER_REQUEUES,
    )
    return REQUEUED, f"Run again for its agent as run {fresh}.", fresh


async def _requeue_for_owner(db, item: WorkItem, run: AssignmentRun, *, cause: str, now: datetime) -> RunOutcome:
    """End *run* and give the action to its owner agent as a fresh instruction run, at once.

    The caller's session holds both rows locked; this commits.
    """
    from app.services import assignment_run_resume
    from app.services.orchestration_engine import notify_orchestration_wake

    previous = item.status
    actor = {"actor_agent_id": item.owner_agent_id, "actor_run_id": run.id}
    run_id, item_id = str(run.id), item.id
    kind, text, _fresh = await _requeue_in_session(db, item, run, cause=cause, now=now)
    await db.commit()
    await assignment_run_resume.discard(run_id)
    await publish_transition(item_id, previous_status=previous, **actor)
    if kind == "blocked":
        await swi.deliver_outcome(item_id)
        return RunOutcome("blocked", text)
    await notify_orchestration_wake()
    return RunOutcome(REQUEUED, text)


#: Run statuses a late attempt never writes over: the run ended for good. A
#: ``cancelled`` run is not one: its item is still settled as stopped.
_ENDED_RUN_STATUSES = frozenset({"completed", "failed", "skipped"})


def superseded_settle(run: AssignmentRun, item: WorkItem, place: tuple[int, int] | None) -> str | None:
    """Why this attempt's outcome must not be written, ``None`` when it may.

    An attempt on a draining worker can finish after the run was ended
    without it: a later attempt fenced the run (``attempt_charges.fence_attempt``),
    a re-queue for the owner agent failed it and gave the item to a fresh run,
    or the stale-run sweep failed it. Writing its outcome then moves
    an ended run back to ``pending`` or ``completed``, so the claims it holds
    read alive and are never released, and settles an item another run owns.
    """
    from app.services.attempt_charges import attempt_fence

    if run.status in _ENDED_RUN_STATUSES:
        return f"its run already ended {run.status}"
    if place is not None and attempt_fence(run.trigger_context) > place:
        return "a later attempt of its run took it over"
    if item.run_id is not None and item.run_id != run.id:
        return f"its work item is now run by {item.run_id}"
    return None


async def _settle(
    run_uuid: uuid.UUID, executed: _Executed, *, place: tuple[int, int] | None = None
) -> RunOutcome:
    from app.core.database import async_session

    async with async_session() as db:
        run, item = await _locked(db, run_uuid)
        if run is None or item is None:
            await db.commit()
            return RunOutcome("ended", "The run or its work item was deleted while it ran.")
        stale = superseded_settle(run, item, place)
        if stale is not None:
            # Replaced while it ran: what it did is the newer owner's to settle.
            await db.rollback()
            logger.warning(
                "Scheduled run %s: the outcome of attempt %s is not written, %s",
                run_uuid,
                place,
                stale,
            )
            return RunOutcome("ended", f"Not settled: {stale}.")
        now = _now()
        previous = item.status
        if executed.messages:
            run.messages = executed.messages
        if executed.actions is not None:
            run.actions_taken = executed.actions
        if executed.kind == "retry":
            # Nothing reached its target: the next attempt may send it.
            context = dict(run.trigger_context or {})
            context.pop("action_started_at", None)
            run.trigger_context = context
            run.status = "pending"
            run.error = _clip(executed.text, 2000)
            item.status = "claimed"
            item.last_activity_at = now
            actor = {"actor_agent_id": item.owner_agent_id, "actor_run_id": run_uuid}
            await db.commit()
            await publish_transition(item.id, previous_status=previous, **actor)
            return RunOutcome(
                "retry",
                executed.text,
                backoff_seconds=float(settings.scheduled_work_item_retry_seconds or 60),
            )
        if run.status == "cancelled":
            # Stopped from outside while it ran (an emergency stop).
            executed = _Executed(
                "blocked",
                "It was stopped while it ran (an emergency stop or a cancelled run).",
                code=swi.OUTCOME_INTERRUPTED,
            )
        if executed.kind == "done":
            item.status = "done"
            item.outcome_code = None
            item.result = _clip(executed.text)
            item.last_activity_at = now
            run.status = "completed"
            run.result_summary = _clip(executed.text, 4000)
            run.error = None
            run.completed_at = now
        else:
            await _block(
                db,
                item,
                run,
                code=executed.code or swi.OUTCOME_EXECUTION,
                reason=executed.text,
                now=now,
            )
        actor = {"actor_agent_id": item.owner_agent_id, "actor_run_id": run_uuid}
        await db.commit()
        item_id, kind, text = item.id, executed.kind, item.result
    await publish_transition(item_id, previous_status=previous, **actor)
    await swi.deliver_outcome(item_id)
    return RunOutcome("done" if kind == "done" else "blocked", text)


def _cut_by_restarts(run: AssignmentRun) -> bool:
    """Whether a dead-lettered run was lost to restarts rather than failing on its own.

    It was left ``running`` by an attempt whose worker went away, or it
    recorded a resume or a hand-back.
    """
    from app.services import background_checkpoint

    context = run.trigger_context if isinstance(run.trigger_context, dict) else {}
    return (
        run.status == "running"
        or bool(background_checkpoint.resumes_of(context))
        or background_checkpoint.HANDED_BACK_KEY in context
        # A fresh run of the owner agent lost before it started is still
        # recovery from a restart, never the person's.
        or bool(context.get(OWNER_PROMPT_KEY))
        or REQUEUE_KEY in context
    )


async def settle_dead_letter(run_id: str | None, last_error: str | None, attempts: int) -> None:
    """The engine gave up on a run (attempts exhausted, deadline): its agent runs it again, else block its item.

    A run lost to restarts is given to its owner agent as a fresh run;
    any other dead letter blocks the item with what failed.
    """
    from app.core.database import async_session

    run_uuid = safe_uuid(run_id) if run_id else None
    if run_uuid is None:
        return
    requeued = False
    async with async_session() as db:
        run, item = await _locked(db, run_uuid)
        if run is None:
            return
        now = _now()
        previous = item.status if item is not None else None
        reason = (
            "It could not be run after "
            f"{attempts} attempt(s): {last_error or 'the platform gave up on it'}."
        )
        if (
            item is not None
            and item.status not in ("done", "cancelled", "blocked")
            and _cut_by_restarts(run)
        ):
            kind, _text, _fresh = await _requeue_in_session(
                db, item, run, cause=_clip(last_error, 500) or "its worker stopped", now=now
            )
            requeued = kind == REQUEUED
        elif item is not None and item.status not in ("done", "cancelled", "blocked"):
            await _block(db, item, run, code=swi.OUTCOME_RETRIES_EXHAUSTED, reason=reason, now=now)
        elif run.status not in _TERMINAL_RUN_STATUSES:
            run.status = "failed"
            run.error = _clip(reason, 2000)
            run.completed_at = now
        blocked = item is not None and previous != item.status
        actor = {"actor_agent_id": item.owner_agent_id if item is not None else None, "actor_run_id": run_uuid}
        receipts = [cut_turn_receipt(run_uuid, run.trigger_context)]
        await db.commit()
        item_id = item.id if item is not None else None
    if blocked:
        await publish_transition(item_id, previous_status=previous, **actor)
    await _credit_attempts(str(run_uuid), range(1, max(attempts, 1) + 1), "scheduled run abandoned")
    for token in receipts:
        await _credit_cut_turn(token, "scheduled run abandoned")
    if requeued:
        from app.services import assignment_run_resume
        from app.services.orchestration_engine import notify_orchestration_wake

        await assignment_run_resume.discard(str(run_uuid))
        await notify_orchestration_wake()
        return
    if item_id is not None:
        await swi.deliver_outcome(item_id)
