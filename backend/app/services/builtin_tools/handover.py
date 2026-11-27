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

"""Sub-agent background task tools and handover agent resolution.

Moved verbatim from the original ``app/services/builtin_tools.py`` module
as part of the package split (mechanical refactor - no behavior change).
"""

from __future__ import annotations

import contextlib
import functools
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from typing import TYPE_CHECKING

from app.core.utils import format_exception_message, safe_uuid
from app.services.handover_types import handover_type_set, is_legacy_handover_map
from app.services.output_format import (
    CALL_OUTPUT_SCHEMA_KEY,
    CALL_OUTPUT_SCHEMA_PROPERTY,
    caller_output_format_refusal,
    parse_call_output_schema,
)
from app.services.run_ledger import EVALUATION_CHAIN_KEY, EVALUATION_TOOL_APPROVAL_KEY, record_spawned_task
from app.services.sub_agent_files import (
    format_result_files_section,
    load_result_file_refs,
    register_result_refs,
    task_result_file_ids,
)
from app.services.sub_agent_result_text import DEFAULT_WINDOW_CHARS, answer_window
from app.tools.base import CallableToolExecutor, ToolDefinition
from app.tools.session_input import prompt_safe_task_text

from ._shared import _invalid_task_id_message, _is_uuid_like, logger

#: Task statuses whose recorded files the result tools hand to the parent:
#: a completed task's output, and a failed task's partial output (the
#: batches written before its deadline). Mirrored by the ``wait`` extract's
#: hint (``builtin_tools/wait.py``), which points at ``get_full_task_result``
#: for exactly these.
_FILE_HANDOVER_STATUSES = frozenset({"completed", "failed"})

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.agent import Agent

# ── Sub-Agent Background Tasks ───────────────────────────────────────────────

#: Characters of the sample item the ``collect_task_results`` answer shows.
_COLLECTED_SAMPLE_CHARS = 400
#: Keys the answer lists, the most common first.
_COLLECTED_KEYS_SHOWN = 20
#: Keys a per-item example path prefers, in order, when item 0 carries one.
_COLLECTED_EXAMPLE_KEYS = ("name", "title", "rule_name", "id")
#: Items the per-item index of the answer lists (#2686), and the characters
#: of each identifying value it shows.
_COLLECTED_INDEX_ITEMS = 50
_COLLECTED_INDEX_VALUE_CHARS = 80
#: Short identifying keys the index shows whole; any ``*_id`` / ``*_ids`` key
#: is shown too. A long string field is shown by its length only.
_COLLECTED_INDEX_KEYS = ("name", "title", "rule_name", "id", "pattern_type", "type")
_COLLECTED_INDEX_LONG_CHARS = 120


def _index_value(value: object) -> str:
    """One identifying value of the per-item index, cut to a short line."""
    import json as _json

    text = value if isinstance(value, str) else _json.dumps(value, ensure_ascii=False, default=str)
    if len(text) > _COLLECTED_INDEX_VALUE_CHARS:
        text = text[:_COLLECTED_INDEX_VALUE_CHARS] + "..."
    return text


def _collected_index(items: list[object]) -> list[str]:
    """One line per item: its identifying fields, and the length of its long fields.

    The agent maps index i to an item (an existing object by its ``*_id`` /
    ``*_ids``, a rule by its name) without the full text, then passes the item's
    content by path instead of retyping it (#2686).
    """
    lines: list[str] = []
    for index, item in enumerate(items[:_COLLECTED_INDEX_ITEMS]):
        if not isinstance(item, dict):
            continue
        parts: list[str] = []
        for key, value in item.items():
            if key in _COLLECTED_INDEX_KEYS or key.endswith(("_id", "_ids")):
                parts.append(f"{key}={_index_value(value)}")
            elif isinstance(value, str) and len(value) > _COLLECTED_INDEX_LONG_CHARS:
                parts.append(f"{key}: {len(value)} chars")
        if parts:
            lines.append(f"  [{index}] " + "; ".join(parts))
    if len(items) > _COLLECTED_INDEX_ITEMS:
        lines.append(f"  ... and {len(items) - _COLLECTED_INDEX_ITEMS} more items")
    return lines


def _path_segment(key: str) -> str:
    """One session-path segment for *key*: ``.key``, or ``["key"]`` when it needs quoting."""
    if key.isidentifier():
        return f".{key}"
    escaped = key.replace("\\", "\\\\").replace('"', '\\"')
    return f'["{escaped}"]'


def _collected_summary(merged: Mapping[str, object], task_count: int, output_key: str) -> str:
    """The ``collect_task_results`` answer: what was stored, told truthfully.

    The item count, the keys the items carry (with how many items carry each),
    one sample item cut to a few hundred characters, and a per-item example
    path built only from a key item 0 really has, so the path it advertises
    resolves.
    """
    import json as _json

    items = list(merged.values())
    counts: dict[str, int] = {}
    for item in items:
        if isinstance(item, dict):
            for key in item:
                counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    shown = ", ".join(f"{key} ({n}/{len(items)})" for key, n in ranked[:_COLLECTED_KEYS_SHOWN])
    if len(ranked) > _COLLECTED_KEYS_SHOWN:
        shown += f", and {len(ranked) - _COLLECTED_KEYS_SHOWN} more"
    base = f"$collected{_path_segment(output_key)}"
    lines = [
        f"Collected {len(items)} items from {task_count} tasks, stored as `$collected` "
        f"(items under `{base}`, indexed 0 to {len(items) - 1}).",
        f"Keys of the items: {shown or 'none'}.",
    ]
    first = items[0] if items else None
    if isinstance(first, dict):
        sample = _json.dumps(first, ensure_ascii=False, default=str)
        if len(sample) > _COLLECTED_SAMPLE_CHARS:
            sample = sample[:_COLLECTED_SAMPLE_CHARS] + f"... [{len(sample)} characters in all]"
        lines.append(f"Item 0: {sample}")
        example = next((k for k in _COLLECTED_EXAMPLE_KEYS if k in first), None)
        if example is None:
            example = next((k for k, v in first.items() if not isinstance(v, (dict, list))), None)
        if example is not None:
            lines.append(
                f"Per-item path: `{base}[0]{_path_segment(example)}` (an item a key above "
                f"does not list for all items may lack it)."
            )
    index = _collected_index(items)
    if index:
        lines.append("Items by index:")
        lines.extend(index)
    lines.append(
        "Pass `$collected`, an item path or a field path as a tool argument value: the "
        "platform resolves it to the real value. A key with a dash, dot or space goes in "
        'quoted brackets: `["my-key"]`. Never retype an item\'s text: a write call whose '
        "argument is a near copy of a collected field is refused. To read one task's "
        "whole answer, call get_full_task_result with its task id."
    )
    return "\n".join(lines)


def build_sub_agent_tools(
    db: AsyncSession,
    user_id: str,
    parent_agent_id: str,
    handover_agents: list,
    conversation_id: str | None = None,
    parent_run_id: str | None = None,
    delegation_depth: int = 0,
    parent_run_id_resolver: object | None = None,
    session_vars: dict | None = None,
    session_persist: Callable[[], Awaitable[None]] | None = None,
    scope_headers: dict[str, str] | None = None,
    visited_agents: list[str] | None = None,
    self_agent: Agent | None = None,
    allow_self_fanout: bool = False,
    task_timeout_seconds: int | None = None,
    task_max_iterations: int | None = None,
    delegations_out: list | None = None,
    element_scope: dict | None = None,
    handover_type_map: Mapping[str, str] | None = None,
    handover_peers: list | None = None,
    resolve_owned_target: Callable[[str], Awaitable[Agent | None]] | None = None,
    evaluation_chain: bool = False,
    evaluation_tool_approval: bool | None = None,
    cap_scope: str | None = None,
    inherited_template_picks: Mapping[str, str] | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor] | None:
    """Build tools for spawning and managing background sub-agent tasks.

    ``inherited_template_picks`` is ``{provider: integration id}`` the spawning
    run resolved its per-user (template) bindings to: stored on every task it
    spawns, so the task's turn acts as the same account (#1792).

    Injected when the agent has handover_agent_ids configured — the same
    set of peer agents available for consult/transfer are also available
    as targets for background tasks.

    **Self-fan-out** (``allow_self_fanout=True`` with ``self_agent`` set):
    the agent can additionally spawn *parallel copies of itself* by passing
    ``agent_id="self"`` (or its own id/slug).  This lets a single agent
    parallelise a large/independent workload without needing a separate peer
    worker.  Self is normally rejected by the cycle guard (the parent is
    always in the visited chain); the carve-out is bounded by being offered
    only at the top delegation level (so a self-spawned worker can't recurse)
    and by the per-run concurrency cap.  When ``handover_agents`` is empty but
    self-fan-out is enabled, the tools are still built (with self as the only
    valid target).

    **Delegation tracing behaviour:**

    * ``parent_run_id`` provided (assignment context) → spawned tasks create
      linked child AssignmentRun records so the full delegation chain is
      visible in the Activity tab.
    * ``parent_run_id_resolver`` provided (chat context) → lazily creates a
      parent AssignmentRun on first handover, then chains child runs to it.
    * Both ``None`` → no AssignmentRun records are created; the delegation
      is traced only as tool calls within the chat thread.

    ``delegation_depth`` tracks how many levels deep we are.  It is
    forwarded to ``create_sub_agent_task`` → ``_execute_sub_agent_task``
    which uses it to cap further delegation at ``_MAX_DELEGATION_DEPTH``.

    ``visited_agents`` is the ordered chain of agent ids already involved in
    the current handover chain (cycle detection).  Spawning a task targeting
    an agent already in the chain is rejected with a friendly tool-result
    error; the chain (including the current agent) is stored on the task so
    nested delegations inherit it.

    ``task_timeout_seconds`` / ``task_max_iterations`` are the parent
    execution budget inherited by every task spawned through these tools
    (an assignment run's effective ``timeout_seconds`` and resolved
    ``max_iterations``).  ``None`` is a chat spawn:
    ``create_sub_agent_task`` derives the wall-clock from the target
    agent's ``max_iterations`` (same formula as ``run_agent_chat``).

    ``element_scope`` is the spawning run's effective Scope(s)
    (``services.element_scope``), carried by every task so the target
    agent narrows it with its own selection instead of escaping it.

    ``cap_scope`` scopes the per-run cap for a run with neither a parent run
    nor a conversation (an evaluated turn), so its spawns are bounded
    as a chat's or an assignment's are.

    **Targets outside the delegate peers.** ``handover_type_map`` and
    ``handover_peers`` (every configured peer, whatever its type) only word
    the refusal: a consult / transfer / mandatory-transfer peer is refused
    naming its type and the tool that reaches it. ``resolve_owned_target``,
    given to an agent holding the agent-builder tools only, resolves an agent
    the acting user OWNS - every agent ``create_agent`` makes - as a target
    with no handover binding: such an agent can already create, configure and
    trigger any agent of its user, so delegating to one widens nothing. A
    peer of another type stays refused, and a disabled agent is refused.
    """
    # Self-fan-out is only meaningful when a concrete current agent is known.
    _self_enabled = bool(allow_self_fanout and self_agent is not None)
    if not handover_agents and not _self_enabled and resolve_owned_target is None:
        return None

    _self_id = str(self_agent.id) if _self_enabled and self_agent else None

    # Normalize the visited chain (dropping non-sequence values — a bare
    # string would otherwise become a bogus per-character chain) and make
    # sure the CURRENT agent is in it so the chain the spawned task
    # inherits always includes its parent.
    _visited_chain: list[str] = normalize_visited_agents(visited_agents)
    if parent_agent_id and str(parent_agent_id) not in _visited_chain:
        _visited_chain.append(str(parent_agent_id))

    executor = CallableToolExecutor()

    # ── Per-run concurrency cap ──────────────────────────────────────────────
    # Prevents a single orchestrator run from spawning an unbounded number of
    # concurrent sub-agent tasks and monopolising the entire DB connection pool
    # (each executing task holds a connection for its full lifetime).
    #
    # The cap is enforced durably by counting in-flight (pending/running)
    # ``sub_agent`` orchestration executions for this parent run — so it is
    # accurate across the iterative spawn → collect → spawn pattern and survives
    # restarts (replaces the old in-process counter + ``on_complete_cb``).
    from app.core.config import settings as _settings

    _run_limit: int = _settings.max_concurrent_sub_agent_tasks_per_run

    async def _target_outside_delegate_peers(requested: str) -> tuple[Agent | None, str | None]:
        """``(target, None)`` for an owned agent a builder may reach, else ``(None, refusal)``.

        Every refusal names the condition that is missing.
        """
        if (
            requested.lower() == "self"
            or (parent_agent_id and requested == str(parent_agent_id))
            or (
                self_agent is not None
                and (
                    requested == str(self_agent.id)
                    or (self_agent.slug and requested.lower() == self_agent.slug.lower())
                )
            )
        ):
            return None, background_task_refusal(requested, self_fanout_refusal_reason(self_agent, delegation_depth))
        peer = resolve_handover_agent(requested, list(handover_peers or []))
        if peer is not None:
            return None, background_task_refusal(
                requested, _other_type_reason(handover_type_map, str(peer.id))
            )
        owned = await resolve_owned_target(requested) if resolve_owned_target else None
        if owned is None:
            how = (
                "add it to this agent's handover agents with the delegate type "
                "(update_agent handover_agent_ids + handover_types)"
            )
            if resolve_owned_target is not None:
                return None, background_task_refusal(
                    requested,
                    "it is neither a delegate peer of this agent nor an agent you "
                    f"own - pass the id or slug of an agent you own, or {how}",
                )
            return None, background_task_refusal(
                requested, f"it is not a delegate peer of this agent - {how}"
            )
        if str(owned.id) in (handover_type_map or {}) and "delegate" not in peer_handover_types(
            handover_type_map, str(owned.id)
        ):
            return None, background_task_refusal(
                requested, _other_type_reason(handover_type_map, str(owned.id))
            )
        if owned.enabled is False:
            return None, background_task_refusal(
                requested, "the agent is disabled - enable it (update_agent enabled=true) first"
            )
        return owned, None

    async def _spawn_background_task(agent_id: str, task: str, output_schema: object = None) -> str:
        """Spawn a background task on another agent (or a copy of self).

        ``output_schema`` (optional) fixes the result's shape: the task's run
        answers one JSON object valid against it.
        """
        _call_schema, _schema_error = parse_call_output_schema(output_schema)
        if _schema_error:
            return _schema_error
        # Resolve the target — self-fan-out first (``"self"`` / own id / slug),
        # then the peer handover list.
        target = None
        is_self = False
        _req = (agent_id or "").strip()
        if _self_id and self_agent is not None and (
            _req.lower() == "self"
            or _req == _self_id
            or (self_agent.slug and _req.lower() == self_agent.slug.lower())
        ):
            target = self_agent
            is_self = True
        if target is None:
            target = resolve_handover_agent(agent_id, handover_agents)
            if target is not None and _self_id and str(target.id) == _self_id:
                is_self = True
        if not target:
            target, refusal = await _target_outside_delegate_peers(_req)
            if refusal:
                return refusal
        _format_refusal = caller_output_format_refusal(target, _call_schema)
        if _format_refusal:
            return _format_refusal

        # Cycle detection: refuse to delegate to an agent already in the
        # handover chain (A -> B -> A) — friendly tool result, never raise.
        # Self-fan-out is the deliberate exception: the agent runs a *parallel
        # copy of itself*, bounded by the per-run cap and only offered at the
        # top delegation level, so it is allowed despite the parent already
        # being in the chain.
        if not is_self:
            _cycle_msg = handover_cycle_error(
                str(target.id),
                _visited_chain,
                agent_names={str(a.id): a.name for a in handover_agents},
                target_name=target.name,
            )
            if _cycle_msg:
                return _cycle_msg

        # Resolve the parent run id first so the active-execution count is
        # scoped correctly (the resolver may lazily create the parent run).
        resolved_parent = parent_run_id
        if not resolved_parent and parent_run_id_resolver:
            try:
                resolved_parent = await parent_run_id_resolver()
            except Exception as resolver_err:
                logger.error(
                    "Failed to resolve parent run id while spawning background "
                    "task for agent %s: %s",
                    agent_id,
                    format_exception_message(resolver_err),
                )
                return (
                    "Failed to spawn background task: "
                    f"{format_exception_message(resolver_err)}"
                )

        from app.services.integration_tools import INHERITED_TEMPLATE_PICKS_KEY
        from app.services.sub_agent import (
            count_active_sub_agent_executions,
            create_sub_agent_task,
        )

        # Fast-path per-run cap pre-check (durable count of in-flight sub-agent
        # executions for this parent run / conversation). This avoids taking the
        # advisory lock in the common case; the authoritative, race-free gate is
        # enforced atomically inside create_sub_agent_task (count+create under a
        # transaction-scoped advisory lock), which returns None when capped.
        #
        # No ``db`` is passed: the sub-agent tools execute concurrently under
        # ``asyncio.gather`` (one LLM turn can request several spawns at once),
        # and a shared ``AsyncSession`` cannot serve concurrent operations.
        # ``count_active_sub_agent_executions`` opens its own session instead.
        try:
            active = await count_active_sub_agent_executions(
                parent_run_id=resolved_parent,
                conversation_id=conversation_id,
                cap_scope=cap_scope,
            )
        except Exception as count_err:
            logger.error(
                "Failed to count active sub-agent executions while spawning background task: %s",
                format_exception_message(count_err),
                exc_info=True,
            )
            return (
                "Failed to spawn background task: "
                f"{format_exception_message(count_err)}"
            )
        cap_message = (
            f"Cannot spawn task: this run already has {_run_limit} active "
            f"background task(s) (the per-run limit). "
            f"Wait for some of them to complete before spawning more, "
            f"or use collect_task_results to wait for a batch."
        )
        if active >= _run_limit:
            return cap_message

        try:
            sub_task = await create_sub_agent_task(
                db,
                user_id=user_id,
                parent_agent_id=parent_agent_id,
                target_agent_id=str(target.id),
                task_description=task,
                conversation_id=conversation_id,
                parent_run_id=resolved_parent,
                delegation_depth=delegation_depth,
                scope_headers=scope_headers,
                file_context=executor.file_context,
                run_limit=_run_limit,
                visited_agents=_visited_chain,
                allow_self=is_self,
                timeout_seconds=task_timeout_seconds,
                max_iterations=task_max_iterations,
                element_scope=element_scope,
                cap_scope=cap_scope,
                # A task spawned in an evaluation's chain records its ledger,
                # so the entry counts its work, and runs under the
                # evaluation's tool approval setting.
                extra_metadata=(
                    {
                        **(
                            {
                                EVALUATION_CHAIN_KEY: True,
                                **(
                                    {EVALUATION_TOOL_APPROVAL_KEY: evaluation_tool_approval}
                                    if evaluation_tool_approval is not None
                                    else {}
                                ),
                            }
                            if evaluation_chain
                            else {}
                        ),
                        # The caller's per-call answer shape, read by the worker.
                        **({CALL_OUTPUT_SCHEMA_KEY: _call_schema} if _call_schema is not None else {}),
                        # The spawning run's per-user picks, kept by the task's turn.
                        **(
                            {INHERITED_TEMPLATE_PICKS_KEY: dict(inherited_template_picks)}
                            if inherited_template_picks
                            else {}
                        ),
                    }
                    or None
                ),
            )
        except Exception as spawn_err:
            logger.error(
                "Failed to spawn background task for agent %s: %s",
                agent_id,
                format_exception_message(spawn_err),
            )
            return (
                "Failed to spawn background task: "
                f"{format_exception_message(spawn_err)}"
            )
        if sub_task is None:
            # Lost the race against a concurrent spawn for the same scope — the
            # atomic gate in create_sub_agent_task declined this one.
            return cap_message
        if evaluation_chain:
            # The evaluation counts the task by what created it, not by reading
            # this result back.
            from app.services.langfuse_tracing import get_current_trace_ctx

            record_spawned_task(get_current_trace_ctx(), str(sub_task.id))
        _target_label = "yourself (parallel copy)" if is_self else target.name
        # Flow-visible trace of the delegation: an agent NODE with an
        # "On delegate" event attachment emits one event per entry.
        if delegations_out is not None:
            delegations_out.append(
                {
                    "task_id": str(sub_task.id),
                    "target_agent_id": str(target.id),
                    "target_agent_name": _target_label,
                    "task": task,
                }
            )
        # The result is parsed back out of the transcript by the agent loop
        # (``agent_loop.collect_task_labels`` / ``collect_spawned_task_ids``)
        # as a one-field-per-line block. The agent name is the only
        # free-text field in it and nothing constrains it to one line, so it
        # is flattened here: a newline inside the value would start a line
        # the parser has to disambiguate from the block's own labels. The
        # flow trace above keeps the raw name.
        _target_line = " ".join(_target_label.split())
        return (
            f"Background task spawned successfully.\n"
            f"Task ID: {sub_task.id}\n"
            f"Target agent: {_target_line}\n"
            f"Status: {sub_task.status}\n"
            f"Use check_task_status(task_ids) or get_task_result(task_ids) to monitor."
        )

    async def _check_task_status(task_ids: str) -> str:
        """Wait for one or more background tasks and report their status.

        Blocks until every valid task reaches a terminal state (completed /
        failed / cancelled), returning **the moment they are done** rather than
        after a fixed blind sleep — capped by a per-call budget so a still-
        running task yields control back to the agent. A clearly malformed
        ``task_ids`` argument (non-string, empty, or no UUID-shaped values)
        returns immediately.
        """
        from app.services.sub_agent import wait_for_tasks

        if not isinstance(task_ids, str):
            return _invalid_task_id_message(task_ids)

        ids = [tid.strip() for tid in task_ids.split(",") if tid.strip()]
        if not ids:
            return "No task IDs provided."

        valid = [tid for tid in ids if _is_uuid_like(tid)]
        # If EVERY supplied ID is malformed there is nothing real to wait on.
        if not valid:
            return "\n".join(_invalid_task_id_message(tid) for tid in ids)

        # Adaptive wait: returns immediately when the tasks already finished,
        # or as soon as they do (no more blind 15s sleeps on completed work).
        statuses = await wait_for_tasks(valid, user_id)

        lines: list[str] = []
        finished_file_ids: list[str] = []
        for tid in ids:
            if not _is_uuid_like(tid):
                lines.append(_invalid_task_id_message(tid))
                continue
            status = statuses.get(tid)
            if not status:
                lines.append(f"{tid[:12]}: not found")
                continue
            file_ids = _result_file_ids(status)
            suffix = f" ({len(file_ids)} file(s) produced)" if file_ids else ""
            lines.append(f"{tid[:12]}: {status['status']}{suffix}")
            if status.get("status") in _FILE_HANDOVER_STATUSES:
                finished_file_ids.extend(file_ids)
        return await _with_result_files("\n".join(lines), finished_file_ids)

    def _result_file_ids(payload: dict | None) -> list[str]:
        """The produced-file ids a status / result payload carries.

        The payload is the serialized task row, so the same tolerant parser
        that reads the row applies (valid UUIDs only, deduplicated).
        """
        return task_result_file_ids(payload or {})

    def _child_run_note(payload: dict | None) -> str:
        """``", run <id>"`` naming the task's own run (``get_run`` reads it), or ``""``.

        A parent that records what a delegate found (a case note, a ticket) cites
        this run, not its own: the audit trail then leads to the run that did the work.
        """
        run_id = str((payload or {}).get("run_id") or "").strip()
        if not run_id or run_id == str((payload or {}).get("task_id") or ""):
            return ""
        return f", run {run_id}"

    async def _with_result_files(text: str, file_ids: list[str]) -> str:
        """Register the files behind *file_ids* in the parent's FileContext.

        That registration - not the ``[[FILE:id]]`` markers in the child's
        text - is what lets the parent ``read_file_content`` /
        ``edit_file_content`` / upload what a delegated task wrote. The refs go
        STRAIGHT into ``executor.file_context`` (the same context the loop hands
        every tool, and the one ``spawn_background_task`` reads the parent's
        working files from), never onto ``ToolResult.files``: the loop copies
        those onto the tool message, and every provider then inlines each
        non-native file's extracted text (up to 80,000 characters per file,
        ``prepare_tool_result_attachments``) into the next request - a summary
        call over nine CSVs would push ~720k characters at the model before it
        could choose to read one. The text names the files instead (id,
        filename, type, size) and says their content is NOT in front of the
        model. Nothing to register - or no context to register in - returns
        the plain text unchanged.
        """
        if not file_ids:
            return text
        file_context = executor.file_context
        if file_context is None:
            return text
        # Own session inside (no ``db``): these tools run concurrently under
        # ``asyncio.gather`` and the shared chat session cannot be shared.
        refs = await load_result_file_refs(file_ids)
        if not refs:
            return text
        # A working file is registered to READ: the parent's reply carries it only when it names it.
        register_result_refs(file_context, refs)
        section = format_result_files_section(
            refs, heading="Files produced by the task(s)"
        )
        return f"{text}\n\n{section}"

    async def _get_task_result(task_ids: str) -> str:
        """Get summaries of completed background tasks (comma-separated IDs).

        Returns a compact summary per task (status + size + preview).
        Use get_full_task_result for the complete output of a single task.
        """
        from app.services.sub_agent import get_task_result

        if not isinstance(task_ids, str):
            return _invalid_task_id_message(task_ids)

        ids = [tid.strip() for tid in task_ids.split(",") if tid.strip()]
        if not ids:
            return "No task IDs provided."
        parts: list[str] = []
        finished_file_ids: list[str] = []
        for tid in ids:
            if not _is_uuid_like(tid):
                parts.append(_invalid_task_id_message(tid))
                continue
            result = await get_task_result(db, tid, user_id)
            if not result:
                parts.append(f"{tid[:12]}: not found")
                continue
            # The child's own run (#2240): a verdict the parent records cites the
            # run that produced it, never the parent's run.
            run_note = _child_run_note(result)
            if result["status"] == "failed":
                # A failed task's partial output (the batches written before
                # the deadline) is handed over like a completed one's:
                # for a parent with no conversation there is no wake to carry
                # it, so this is the only way it reaches them.
                error = str(result.get("error") or "no error recorded")
                file_ids = _result_file_ids(result)
                files_note = f", {len(file_ids)} file(s) produced" if file_ids else ""
                parts.append(
                    f"{tid[:12]}: failed{files_note}{run_note}\n"
                    f"  Error: {error[:300].replace(chr(10), ' ')}"
                )
                finished_file_ids.extend(file_ids)
            elif result["status"] != "completed":
                parts.append(f"{tid[:12]}: still {result['status']}")
            else:
                text = result["result"] or ""
                preview = text[:300].replace("\n", " ")
                file_ids = _result_file_ids(result)
                files_note = f", {len(file_ids)} file(s) produced" if file_ids else ""
                # An answer longer than one get_full_task_result window says
                # so here, with the call that pages it (#1394).
                paging_note = (
                    f"\n  Longer than one read: page it with "
                    f'get_full_task_result(task_id="{tid}", offset=N); each '
                    "read names the next offset."
                    if len(text) > DEFAULT_WINDOW_CHARS
                    else ""
                )
                parts.append(
                    f"{tid[:12]}: completed ({len(text):,} chars{files_note}){run_note}\n"
                    f"  Preview: {preview}...{paging_note}"
                )
                finished_file_ids.extend(file_ids)
        return await _with_result_files("\n".join(parts), finished_file_ids)

    async def _get_full_task_result(task_id: str, offset: int = 0, max_chars: int = DEFAULT_WINDOW_CHARS) -> str:
        """Get the result of a single completed (or failed) background task.

        The answer is stored whole and read by window (#1394): an answer that
        fits one window comes back exactly as stored, with no header; a
        longer one comes back from *offset* with one header line naming the
        total size, the range shown and the next offset (or the end).
        """
        from app.services.sub_agent import get_task_result

        if not _is_uuid_like(task_id):
            return _invalid_task_id_message(task_id)

        result = await get_task_result(db, task_id, user_id)
        if not result:
            return f"Task {task_id} not found."
        run_note = _child_run_note(result)
        if result["status"] == "failed":
            # The failure text plus whatever the task wrote before it failed -
            # a partial export is still the parent's to check.
            text = f"Task failed: {result.get('error') or 'no error recorded'}{run_note}"
            return await _with_result_files(text, _result_file_ids(result))
        if result["status"] != "completed":
            return f"Task is still {result['status']}. Check back later."
        stored = result["result"] or ""
        if not stored:
            return await _with_result_files("Task completed with no output.", _result_file_ids(result))
        window = answer_window(stored, offset, max_chars)
        text = window.text if window.is_whole else f"{window.header(task_id)}\n\n{window.text}"
        return await _with_result_files(text, _result_file_ids(result))

    async def _list_background_tasks(status: str = "") -> str:
        """List recent background tasks, and partner work still running.

        Both, because the question this answers is "what is still running for
        this user", and a delegation to another deployment is an answer to it:
        listing only sub-agents makes an empty list read as "the partner
        finished".
        """
        from app.a2a.client.delegation import outstanding_for_listing
        from app.services.sub_agent import list_user_tasks, task_liveness_suffix

        tasks = await list_user_tasks(
            db,
            user_id,
            parent_agent_id=parent_agent_id,
            status=status or None,
        )
        # The FULL id: check_task_status / get_task_result take a whole UUID,
        # so a shortened id here could not be passed on (#966).
        parts = [
            f"• {t['task_id']} | {t['status']} | "
            f"{t['task_description'][:120]}"
            + (f" | {t['held_reason']}" if t.get("held_reason") else "")
            # A pending / running task names its run and whether its execution
            # is still live: a zombie reads as one.
            + task_liveness_suffix(t)
            for t in tasks
        ]
        partner: list[dict] = []
        if status in ("", "pending", "running"):
            with contextlib.suppress(Exception):
                # An A2A registration that cannot be read is not a reason to
                # stop reporting sub-agents.
                partner = await outstanding_for_listing(
                    db,
                    run_ref=safe_uuid(parent_run_id),
                    conversation_ref=safe_uuid(conversation_id),
                )
        parts += [
            f"• {p['delegation_id']} | running | delegated to partner '{p['partner']}' — still working there"
            for p in partner
        ]
        if not parts:
            return "No background tasks found, and nothing running at a partner."
        return "\n".join(parts)

    async def _cancel_background_task(task_ids: str) -> str:
        """Cancel background tasks this agent spawned for the acting user (#2217).

        The same durable cancel as the UI's stop button (lease released, a
        running worker bails on its next step, delivery suppressed), scoped to
        the acting user's tasks spawned by this agent: another agent's task or
        another user's reads as not found, never as refused.
        """
        from app.services.sub_agent import cancel_sub_agent_task

        if not isinstance(task_ids, str):
            return _invalid_task_id_message(task_ids)
        ids = [tid.strip() for tid in task_ids.split(",") if tid.strip()]
        if not ids:
            return "No task IDs provided."
        lines: list[str] = []
        for tid in ids:
            if not _is_uuid_like(tid):
                lines.append(_invalid_task_id_message(tid))
                continue
            result = await cancel_sub_agent_task(tid, user_id, spawned_by=str(parent_agent_id))
            if result is None:
                lines.append(f"{tid}: not found among your background tasks")
            elif result.get("already_terminal"):
                lines.append(f"{tid}: already {result['status']}, unchanged")
            else:
                was = "running" if result.get("was_running") else "pending"
                lines.append(f"{tid}: cancelled (was {was})")
        return "\n".join(lines)

    async def _collect_task_results(
        task_ids: str,
        output_key: str = "items",
        extra_json: str = "",
        item_key: str = "",
    ) -> str:
        """Collect completed task results and store them as a session variable.

        Fetches results from the given task IDs, parses JSON, extracts items
        (handles ``response`` arrays/dicts and ``raw_configuration`` nesting),
        and merges everything into a single object stored as ``$collected``.
        Use ``$collected`` as a tool argument value in the next tool call —
        the platform resolves it automatically.
        """
        import json as _json

        from app.services.sub_agent import get_task_result_by_id

        _id_keys = ("id", "injector_contract_id", "contract_id", "payload_id")
        item_key = (item_key or "").strip()

        ids = [tid.strip() for tid in task_ids.split(",") if tid.strip()]
        if not ids:
            return "No task IDs provided."

        merged: dict = {}
        task_count = 0
        _seq = 0

        def _merge_item(item: object) -> None:
            nonlocal _seq
            raw = (
                item.get("raw_configuration", item) if isinstance(item, dict) else item
            )
            if not isinstance(raw, dict):
                return
            key: str | None = None
            for k in _id_keys:
                if k in raw:
                    key = raw[k]
                    break
            if key is None:
                key = f"_item_{_seq}"
            _seq += 1
            merged[key] = raw

        def _is_envelope(value: object) -> bool:
            return isinstance(value, dict) and ("raw_configuration" in value or any(k in value for k in _id_keys))

        def _holds_items(value: object) -> bool:
            """True for a list of dicts, an envelope, or a wrapper of either.

            A wrapper is a dict with no text value: a dict carrying text (a
            name, a title) is an item whose nested lists are its own fields.
            """
            if isinstance(value, list):
                return any(isinstance(el, dict) for el in value)
            if not isinstance(value, dict):
                return False
            if _is_envelope(value):
                return True
            if any(isinstance(v, str) for v in value.values()):
                return False
            return any(_holds_items(v) for v in value.values())

        def _holds_no_items(value: object) -> bool:
            """True for an item list that is empty (or holds no dict), or a
            text-free wrapper of one: a result that answers zero items."""
            if isinstance(value, list):
                return not any(isinstance(el, dict) for el in value)
            if not isinstance(value, dict) or _is_envelope(value):
                return False
            if any(isinstance(v, str) for v in value.values()):
                return False
            return any(_holds_no_items(v) for v in value.values())

        def _named_list(data: dict[str, object]) -> list[object] | None:
            """The ``item_key`` list of *data*, at any depth, first found."""
            named = data.get(item_key)
            if isinstance(named, list):
                return named
            for v in data.values():
                found = _named_list(v) if isinstance(v, dict) else None
                if found is not None:
                    return found
            return None

        def _extract_items(data: object) -> None:
            """Extract whole item dicts from one task result.

            Handles these shapes, in priority order:

            1. A list -> each element is an item.
            2. A dict holding the ``item_key`` list (at any depth) -> that
               list is the items.
            3. A dict carrying a known id-key / ``raw_configuration`` (the
               OpenCTI / OpenAEV envelope) -> the dict itself is one item.
            4. A dict whose values hold items (lists of dicts, envelopes, or
               dicts wrapping them) -> those values are walked; its other
               values (counts, summaries) are metadata, never items.
            5. A dict with a single key wrapping one object
               (``{"rule": {...}}``) -> that object is the item.
            6. A text-free dict whose item lists are empty
               (``{"rules": [], "total": 0}``) -> zero items.
            7. Otherwise the dict itself IS one item, kept whole: its nested
               objects are fields of the item, never items of their own.
            """
            if isinstance(data, list):
                for el in data:
                    _merge_item(el)
                return
            if not isinstance(data, dict):
                return
            named = _named_list(data) if item_key else None
            if named is not None:
                for el in named:
                    _merge_item(el)
                return
            if _is_envelope(data):
                _merge_item(data)
                return
            holders = [v for v in data.values() if _holds_items(v)]
            if holders:
                for v in holders:
                    if _is_envelope(v):
                        _merge_item(v)
                    else:
                        _extract_items(v)
                return
            if _holds_no_items(data):
                return
            if len(data) == 1:
                only = next(iter(data.values()))
                if isinstance(only, dict):
                    _merge_item(only)
                    return
            _merge_item(data)

        for tid in ids:
            text = await get_task_result_by_id(tid)
            if not text:
                continue
            task_count += 1
            try:
                parsed = _json.loads(text) if isinstance(text, str) else text
            except (_json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, list):
                _extract_items(parsed)
            elif isinstance(parsed, dict):
                items = parsed.get("response", parsed)
                _extract_items(items)
            else:
                continue

        if not merged:
            return "No items could be extracted from the provided task results."


        result_obj: dict = {}
        if extra_json and extra_json.strip():
            with contextlib.suppress(_json.JSONDecodeError, TypeError):
                result_obj.update(_json.loads(extra_json))
        result_obj[output_key] = merged

        if session_vars is not None:
            session_vars["$collected"] = result_obj
            # Persist across turns so a draft-gate / approval pause does not
            # discard the collected data (forcing a costly re-run of the
            # sub-agent conversions). Best-effort: a persistence failure must
            # not break collection.
            if session_persist is not None:
                with contextlib.suppress(Exception):
                    await session_persist()

        return _collected_summary(merged, task_count, output_key)

    async def _clear_collected() -> str:
        """Release ``$collected`` once the orchestration has consumed it.

        Removes ``$collected`` from the in-memory store and mirrors the cleared
        state to the persistent store (deleting the Redis key via
        ``session_persist``), so a later turn does not reload stale data and
        ``transfer_to_agent`` is no longer blocked by it. Best-effort: a
        persistence failure must not surface as a tool error.
        """
        if session_vars is None or "$collected" not in session_vars:
            return "Nothing to clear: `$collected` is not set."
        session_vars.pop("$collected", None)
        if session_persist is not None:
            with contextlib.suppress(Exception):
                await session_persist()
        return (
            "Cleared `$collected`. The collected data has been released and "
            "handover/transfer is no longer blocked by it."
        )

    executor.register("spawn_background_task", _spawn_background_task)
    executor.register("check_task_status", _check_task_status)
    executor.register("get_task_result", _get_task_result)
    executor.register("get_full_task_result", _get_full_task_result)
    executor.register("list_background_tasks", _list_background_tasks)
    executor.register("cancel_background_task", _cancel_background_task)
    executor.register("collect_task_results", _collect_task_results)
    executor.register("clear_collected", _clear_collected)

    _spawn_desc = (
        "Delegate a task that runs in the background while you keep working. "
        "After spawning, immediately continue with independent tools in this "
        "turn - do not wait unless you are blocked on those tasks. Use this "
        "for research, analysis, or any work that doesn't need an immediate "
        "response. "
    )
    _agent_id_desc = (
        "The ID or slug of the agent to delegate to (from the peer agents list)"
    )
    if resolve_owned_target is not None:
        _agent_id_desc += ", or of any agent you own (an agent you created included)"
    if _self_enabled:
        _spawn_desc += (
            'You can also run a parallel background copy of YOURSELF (agent_id '
            '"self") to split a genuinely large, independent workload into '
            "batches. Use this sparingly — for a single, normal, or small task, "
            "do the work yourself instead of spawning a sub-agent. "
        )
        _agent_id_desc += (
            ', or "self" to run a parallel background copy of yourself (only for '
            "large, independent, parallelizable workloads)"
        )
    _spawn_desc += "Check results later with get_task_result."

    definitions = [
        ToolDefinition(
            name="spawn_background_task",
            description=_spawn_desc,
            input_schema={
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": _agent_id_desc,
                    },
                    "task": {
                        "type": "string",
                        "description": "Detailed description of what the agent should do",
                    },
                    "output_schema": CALL_OUTPUT_SCHEMA_PROPERTY,
                },
                "required": ["agent_id", "task"],
            },
            source="builtin:sub_agent",
        ),
        ToolDefinition(
            name="check_task_status",
            description=(
                "Check the status of one or more background tasks. "
                "Pass all task IDs as a comma-separated string to check them in one call. "
                "Do not call this when you still have independent work - do that "
                "work first (you can combine this with other tools in the same "
                "round; they run in parallel). "
                "It returns the moment every task finishes, or after a short wait budget "
                "if some are still running — to keep waiting, use the wait tool with "
                "'until' instead of calling this again. Returns one line "
                "per task: running, completed, failed, cancelled, or not found."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "string",
                        "description": "One or more task IDs, comma-separated (e.g. 'id1,id2,id3')",
                    },
                },
                "required": ["task_ids"],
            },
            source="builtin:sub_agent",
        ),
    ]

    # Result-reading tools are exposed in BOTH modes: a self-fan-out or research
    # agent (chat path, ``session_vars`` set) needs to READ and synthesize its
    # background workers' output, not only merge structured items into
    # ``$collected``. Previously these were chat-excluded, leaving such an agent
    # unable to echo back what a sub-agent produced. ``collect_task_results``
    # (added below for the chat path) remains the tool for the structured
    # downstream-tool merge pattern.
    definitions.extend(
        [
            ToolDefinition(
                name="get_task_result",
                description=(
                    "Get summaries of completed background tasks (status, size, preview). "
                    "Pass all task IDs comma-separated. Use get_full_task_result for one "
                    "task's complete output. Files the tasks produced (CSV, XLSX, ...) are "
                    "attached to your context by this call - read them with "
                    "read_file_content(file_id)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "task_ids": {
                            "type": "string",
                            "description": "One or more task IDs, comma-separated",
                        },
                    },
                    "required": ["task_ids"],
                },
                source="builtin:sub_agent",
            ),
            ToolDefinition(
                name="get_full_task_result",
                description=(
                    "Retrieve the FULL output of a single completed background task "
                    "(or the error of a failed one). Use this to get the actual content "
                    "one task at a time. An answer longer than max_chars comes by "
                    "window: a header line gives the total size and the next offset to "
                    "pass, until it says the end. Files the task produced - a failed task's "
                    "partial output included - are attached to your context by this "
                    "call - read them with read_file_content(file_id), never rebuild "
                    "them from the text."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "task_id": {
                            "type": "string",
                            "description": "Single task ID to retrieve",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Character to start from: the next offset a previous read named (default 0)",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "Characters per read (default 50,000, at most 150,000)",
                        },
                    },
                    "required": ["task_id"],
                },
                source="builtin:sub_agent",
            ),
        ]
    )

    definitions.append(
        ToolDefinition(
            name="list_background_tasks",
            description=(
                "List your recent background tasks and their statuses. "
                "Optionally filter by status (pending, running, completed, failed)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Optional filter: pending, running, completed, or failed",
                        "default": "",
                    },
                },
                "required": [],
            },
            source="builtin:sub_agent",
        )
    )
    definitions.append(
        ToolDefinition(
            name="cancel_background_task",
            description=(
                "Stop background tasks you spawned that are no longer needed: a "
                "running one stops at its next step and its result is never "
                "delivered. One line per task: cancelled, already finished, or not found."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "string",
                        "description": "One or more task IDs, comma-separated",
                    },
                },
                "required": ["task_ids"],
            },
            source="builtin:sub_agent",
        )
    )

    if session_vars is not None:
        definitions.append(
            ToolDefinition(
                name="collect_task_results",
                description=(
                    "Collect and merge completed background task results into a "
                    "session variable. Fetches results from the given task IDs, "
                    "extracts structured items, and stores them as `$collected`. "
                    "Use `$collected` as a tool argument value in your next tool "
                    "call — the platform resolves it to the full data automatically. "
                    "This avoids passing large JSON payloads through the conversation."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "task_ids": {
                            "type": "string",
                            "description": "Comma-separated task IDs to collect results from",
                        },
                        "output_key": {
                            "type": "string",
                            "description": "Key name for the merged items in the stored object (e.g. 'widgets', 'injects', 'rules')",
                            "default": "items",
                        },
                        "extra_json": {
                            "type": "string",
                            "description": 'Optional JSON string with additional fields to include (e.g. \'{"name": "...", "description": "..."}\')',
                            "default": "",
                        },
                        "item_key": {
                            "type": "string",
                            "description": "Optional key of the list holding the items in each task result (e.g. 'rules'); its sibling fields are left out",
                            "default": "",
                        },
                    },
                    "required": ["task_ids"],
                },
                source="builtin:sub_agent",
            )
        )
        definitions.append(
            ToolDefinition(
                name="clear_collected",
                description=(
                    "Release the `$collected` session variable once you have "
                    "finished using it (e.g. after the Grouping / dashboard / "
                    "scenario has been created). Frees the stored data and "
                    "unblocks handover/transfer. Do NOT call it mid-orchestration "
                    "or during an approval pause — only when the collected data "
                    "is no longer needed."
                ),
                input_schema={
                    "type": "object",
                    "properties": {},
                    "required": [],
                },
                source="builtin:sub_agent",
            )
        )

    return definitions, executor


def background_task_refusal(agent_ref: str, reason: str) -> str:
    """What ``spawn_background_task`` answers for a target it may not reach, naming why."""
    return f"Agent {agent_ref} is not available for background tasks: {reason}."


def _other_type_reason(type_map: Mapping[str, str] | None, agent_id: str) -> str:
    """Why a configured peer of another handover type is no background-task target."""
    h_type = peer_handover_type(type_map, agent_id)
    if h_type is None:
        return (
            "it is a mandatory-transfer peer of this agent, routed after the "
            "answer and never by a tool call"
        )
    if h_type != "delegate":
        return (
            f"it is a {h_type} peer of this agent - use {HANDOVER_TOOL_NAMES[h_type]}, "
            "or change its handover type to delegate"
        )
    return "background tasks to it are not offered on this turn"


def consult_refusal(
    agent_ref: str,
    type_map: Mapping[str, str] | None,
    configured_peers: list | None,
    *,
    offered: Collection[str] | None = None,
    owned_reachable: bool = False,
) -> str:
    """What ``consult_agent`` answers for a target it may not reach, naming why.

    An unknown target gets the plain refusal; a configured peer of another
    handover type is told the type and the tool that reaches it on this turn
    (the background-task refusal's rule). *owned_reachable*: the agent
    holds the agent-builder tools, so an agent its user owns is a target too.
    """
    base = f"Agent {agent_ref} is not available for consultation"
    peer = resolve_handover_agent(agent_ref, list(configured_peers or []))
    if peer is None or is_legacy_handover_map(type_map):
        if owned_reachable:
            return f"{base}: it is neither a consult peer of this agent nor an enabled agent you own."
        return f"{base}."
    h_type = peer_handover_type(type_map, str(peer.id))
    if h_type is None:
        return (
            f"{base}: it is a mandatory-transfer peer of this agent, routed after the "
            "answer and never by a tool call."
        )
    if h_type in (TOOL_HANDOVER_TYPES if offered is None else offered):
        return (
            f"{base}: it is a {h_type} peer of this agent - use {HANDOVER_TOOL_NAMES[h_type]}, "
            "or change its handover type to consult."
        )
    return f"{base}: it is a {h_type} peer of this agent, and {HANDOVER_TOOL_NAMES[h_type]} is not offered on this turn."


async def owned_consult_target(
    agent_ref: str,
    *,
    configured_peers: list | None,
    resolve_owned_target: Callable[[str], Awaitable[Agent | None]] | None,
    self_id: str,
) -> Agent | None:
    """The agent a builder may consult once outside its consult peers, else ``None``.

    An enabled agent the acting user owns (``owned_target_resolver``), never
    the agent itself nor one of its configured peers of another type - those
    keep the refusal that names their tool (as the background-task refusal does).
    """
    if resolve_owned_target is None or resolve_handover_agent(agent_ref, list(configured_peers or [])):
        return None
    owned = await resolve_owned_target(agent_ref)
    if owned is None or owned.enabled is False or str(owned.id) == str(self_id):
        return None
    return owned


#: The handover types a consulted configured peer keeps on its turn.
CONSULTED_PEER_HANDOVER_TYPES: frozenset[str] = frozenset({"consult"})


async def live_consult_target(db: AsyncSession, target: Agent) -> Agent:
    """The consulted agent's row as it is NOW, else the one the turn loaded.

    A turn loads its handover peers once, when it starts; a long turn (an
    orchestrator's run) then consults a peer whose persona, guardrails,
    skills or bindings were saved since, and the consulted turn ran on the
    stale copy. One primary-key read on the consult's own session, at call
    time, gives the nested turn the configuration a run of that agent would
    get. A read that fails or finds nothing keeps the loaded row, so a
    consult never fails for the refresh.
    """
    from app.models.agent import Agent as _Agent

    try:
        fresh = await db.get(_Agent, target.id)
    except Exception as err:
        logger.warning(
            "consult of %s runs on the peer row loaded at turn start: %s",
            target.id,
            format_exception_message(err),
        )
        return target
    return fresh if fresh is not None else target


def agent_config_revision(agent: object) -> str | None:
    """The configuration revision a turn ran with, for its trace.

    The agent row's ``updated_at`` (ISO 8601), bumped by every save of the
    agent, so a trace says which persona, guardrails and bindings the turn
    carried without copying them. ``None`` for a stub without one.
    """
    updated_at = getattr(agent, "updated_at", None)
    if updated_at is None:
        return None
    iso = getattr(updated_at, "isoformat", None)
    return iso() if callable(iso) else str(updated_at)


def consulted_peer_handover(delegation_depth: int) -> tuple[bool, frozenset[str]]:
    """``(allow_handover, allowed_handover_types)`` of a configured peer's consulted turn.

    *delegation_depth* is that turn's own, one past the consulting turn's. The
    peer answers the agent that asked, so it may consult its own consult peers
    in turn - a clerk asking an analyst for a figure - but never transfers the
    conversation nor starts a background task that would outlive the consult.
    At ``_MAX_DELEGATION_DEPTH`` it holds no handover tool; before that, the
    visited chain each consult carries refuses an agent already in it.
    """
    from app.services.sub_agent import _MAX_DELEGATION_DEPTH

    return delegation_depth < _MAX_DELEGATION_DEPTH, CONSULTED_PEER_HANDOVER_TYPES


#: The ``handover_context`` key naming the agent that spawned a background
#: task, set by the sub-agent worker on a ``sub_agent_task`` turn.
DELEGATING_AGENT_KEY = "delegating_agent_id"

#: The iteration budget of a parent's answer to its delegated task: a
#: question, answered from what the parent knows and a few tool calls.
PARENT_CONSULT_MAX_ITERATIONS = 5


def delegated_parent_id(
    handover_context: Mapping | None,
    visited_agents: list[str] | tuple[str, ...] | None,
    *,
    self_id: str,
) -> str | None:
    """The agent that spawned this turn as a background task, when it is the direct parent.

    Read from a ``sub_agent_task`` handover context, and only when it is the
    chain's last ancestor (the visited chain ends ``parent -> self``): a
    grandparent is never one, nor the agent itself (a self-fan-out copy).
    ``None`` for any other turn.
    """
    if not isinstance(handover_context, Mapping) or handover_context.get("type") != "sub_agent_task":
        return None
    parent = str(handover_context.get(DELEGATING_AGENT_KEY) or "")
    chain = normalize_visited_agents(visited_agents)
    if not parent or parent == str(self_id) or len(chain) < 2:
        return None
    return parent if chain[-1] == str(self_id) and chain[-2] == parent else None


class ParentConsultBudget:
    """The questions one delegated run may still ask its direct parent through ``consult_agent``.

    The chain guard refuses a handover to an agent already in the chain; a
    consult of the direct parent is the one exception, ``limit`` times per
    run (``DELEGATED_PARENT_CONSULT_LIMIT``). A background task back to the
    parent never is. ``take`` holds no ``await``, so parallel consults under
    ``asyncio.gather`` count one by one.
    """

    def __init__(self, parent_agent_id: str | None, limit: int) -> None:
        self.parent_agent_id = parent_agent_id
        self.limit = max(0, int(limit or 0))
        self.used = 0

    def is_parent(self, target_id: object) -> bool:
        """Whether a consult of *target_id* is a question to the parent (never with a limit of 0)."""
        return self.limit > 0 and bool(self.parent_agent_id) and str(target_id) == self.parent_agent_id

    def take(self) -> str | None:
        """Count one question; the refusal naming the limit when none is left."""
        if self.used >= self.limit:
            return (
                f"Consultation refused: this delegated run already asked its parent agent "
                f"{self.limit} question(s), the limit DELEGATED_PARENT_CONSULT_LIMIT sets. "
                "Complete the task with what you have and name any open question in your result."
            )
        self.used += 1
        return None


def log_parent_consult(run_id: str | None, parent_agent_id: str, question: str, outcome: str) -> None:
    """The audit line of one question a delegated run asked its parent."""
    logger.info(
        "parent consult: run=%s parent_agent=%s question_chars=%d outcome=%s",
        run_id or "none",
        parent_agent_id,
        len(question or ""),
        outcome,
    )


def owned_target_resolver(
    builtin_tool_ids: Iterable[str] | None, user_id: str | None
) -> Callable[[str], Awaitable[Agent | None]] | None:
    """The owned-agent target resolver of ``build_sub_agent_tools``.

    Only for an agent holding the agent-builder tools, acting for a known user:
    any other agent reaches its configured delegate peers and nothing else.
    """
    if "agent_builder" not in set(builtin_tool_ids or ()) or safe_uuid(user_id) is None:
        return None
    return functools.partial(resolve_owned_delegation_target, user_id=str(user_id))


async def resolve_owned_delegation_target(agent_id_or_slug: str, *, user_id: str) -> Agent | None:
    """The agent *user_id* owns with this id (or, failing that, slug), else ``None``.

    The background-task target of an agent holding the agent-builder tools.
    Its own session: the sub-agent tools run concurrently under
    ``asyncio.gather``, and the chat session cannot serve two at once. A
    lookup failure resolves nothing, so the tool refuses instead of raising.
    """
    from sqlalchemy import func, select

    from app.core.database import async_session
    from app.models.agent import Agent as _Agent

    owner = safe_uuid(user_id)
    key = (agent_id_or_slug or "").strip()
    if owner is None or not key:
        return None
    by_id = safe_uuid(key)
    clauses = ([_Agent.id == by_id] if by_id is not None else []) + [
        func.lower(_Agent.slug) == key.lower()
    ]
    try:
        async with async_session() as db:
            for clause in clauses:
                row = (
                    await db.execute(select(_Agent).where(clause, _Agent.user_id == owner))
                ).scalar_one_or_none()
                if row is not None:
                    db.expunge(row)
                    return row
    except Exception as exc:
        logger.warning(
            "Could not resolve background-task target %r for user %s: %s",
            key,
            user_id,
            format_exception_message(exc),
        )
    return None


def self_fanout_enabled(agent: object) -> bool:
    """Whether *agent* may spawn background copies of itself on this platform.

    Both the platform switch (``AGENT_SELF_FANOUT_ENABLED``) and the agent's
    own ``allow_self_fanout`` must allow it; the platform switch wins. A NULL
    column (added bare by the startup safety net, or an in-memory ``Agent``
    never flushed) reads as on, the column default. The callers add the
    per-turn conditions (top delegation level, ``delegate`` offered).
    """
    from app.core.config import settings

    return bool(settings.agent_self_fanout_enabled) and getattr(agent, "allow_self_fanout", True) is not False


def self_fanout_effective_text(stored: bool) -> str:
    """The effective self fan-out value of an agent whose own setting is *stored*.

    The agent setting alone does not say whether a copy can be spawned: the
    platform switch wins, and only a top-level run (a chat or assignment run)
    may spawn, never a background copy or a consulted run. get_agent shows
    this next to the stored setting.
    """
    from app.core.config import settings

    if not settings.agent_self_fanout_enabled:
        return "No: turned off for the platform (AGENT_SELF_FANOUT_ENABLED)"
    if not stored:
        return "No: turned off for this agent"
    return (
        "Yes from a top-level run (chat or assignment run) only; never from a background "
        "copy or a consulted run, whatever the setting"
    )


def self_fanout_refusal_reason(self_agent: object | None, delegation_depth: int) -> str:
    """Why a copy of *self_agent* is refused on this turn, naming the condition that holds.

    A run below the top delegation level (a background copy, a consulted
    agent) is told so first: no switch would let it spawn. The switches are
    named only when one of them is off; otherwise the turn did not offer it.
    """
    if delegation_depth > 0:
        return (
            f"this run is itself a delegated run (delegation depth {delegation_depth}), and only "
            "a top-level run may spawn copies of itself: do the work in this run, or return "
            "what is left to the run that spawned you"
        )
    if self_agent is None or not self_fanout_enabled(self_agent):
        return (
            "running a copy of yourself is turned off for this agent "
            "(allow_self_fanout) or for the platform (AGENT_SELF_FANOUT_ENABLED)"
        )
    return (
        "running a copy of yourself is not offered on this turn: it needs handover allowed "
        "on the turn and the delegate handover type offered"
    )


def format_self_fanout_prompt(*, has_peers: bool) -> str:
    """System-prompt guidance for the (rare) self-parallelisation capability.

    ``spawn_background_task(agent_id="self")`` lets an agent fan a workload out
    across background copies of itself.  This guidance is deliberately
    **conservative**: the default is always "do the work yourself in this turn".
    Self-fan-out earns its overhead (a separate run per copy, polling, result
    gathering) only for genuinely large, independently-batchable workloads — so
    the prompt must steer agents *away* from spawning for ordinary single tasks,
    not toward it.  An earlier, more enthusiastic phrasing caused agents to
    over-spawn sub-agents for everyday requests.

    ``has_peers`` controls whether the shared ``## Agent Delegation`` header is
    emitted here (when the agent has no peer handover agents, the caller's
    delegation block is skipped, so this block must provide the header).
    """
    lead = "" if has_peers else "\n\n## Agent Delegation"
    return (
        f"{lead}\n**Parallel self-execution (use sparingly)**: by default, do "
        "the work yourself in this turn. ONLY when a task is genuinely large and "
        "splits into many independent batches that would be slow done one-by-one "
        "(e.g. processing dozens of items) may you fan it out to background "
        'copies of yourself — `spawn_background_task(agent_id="self")` once per '
        "batch, then keep working on anything independent. Gather with "
        "`check_task_status` and `get_task_result` when you need the outcomes, "
        "not as the next action after spawning. Do "
        "NOT spawn a sub-agent for a single, normal, or small task (ordinary "
        "research, a one-off answer or deliverable) — just do it directly."
    )


# ── Peer Handover Tags ───────────────────────────────────────────────────────

#: The tag of a peer in a legacy handover map: no peer has a type, so each
#: one runs every handover type the turn offers.
LEGACY_HANDOVER_TAG = "ANY"

#: The tool the model reaches a peer of each handover type through.
HANDOVER_TOOL_NAMES: Mapping[str, str] = {
    "delegate": "spawn_background_task",
    "consult": "consult_agent",
    "transfer": "transfer_to_agent",
}
TOOL_HANDOVER_TYPES: tuple[str, ...] = tuple(HANDOVER_TOOL_NAMES)

#: The order a legacy peer's tools are named in.
_LEGACY_TOOL_ORDER: tuple[str, ...] = ("consult", "delegate", "transfer")


def offered_handover_types(
    *,
    allowed_types: Collection[str] | None = None,
    delegate_available: bool = True,
    tool_deny_list: Iterable[str] | None = None,
) -> frozenset[str]:
    """The handover types whose tool a turn may register.

    Narrowed by the types the caller allows (``None`` allows every one: a
    mentioned agent never transfers, a flow edge's label names its types), by
    *delegate_available* (no ``spawn_background_task`` for an anonymous chat
    user, nor for an assignment run with no acting user), and by the agent's
    ``tool_deny_list``, which strips a denied tool from the turn's tools.
    """
    denied = set(tool_deny_list or ())
    return frozenset(
        h_type
        for h_type, tool in HANDOVER_TOOL_NAMES.items()
        if (allowed_types is None or h_type in allowed_types)
        and (delegate_available or h_type != "delegate")
        and tool not in denied
    )


def peer_handover_types(type_map: Mapping[str, str] | None, agent_id: str) -> frozenset[str]:
    """Every tool type a peer of an explicit map is reached through, empty for no tool.

    An entry holds one type, several joined by ``|`` or ``all``
    (``handover_type_set``). A ``mandatory_transfer`` peer is routed after the
    agent's answer, never by the model. A peer without a type runs as ``delegate``.
    """
    types = handover_type_set((type_map or {}).get(agent_id, "delegate"))
    if "mandatory_transfer" in types:
        return frozenset()
    return types & frozenset(TOOL_HANDOVER_TYPES)


def peer_handover_type(type_map: Mapping[str, str] | None, agent_id: str) -> str | None:
    """The first handover type a peer of an explicit map is reached through, ``None`` for no tool.

    The one a refusal names (``consult``, then ``delegate``, then ``transfer``
    for a peer of several types, ``peer_handover_types``).
    """
    types = peer_handover_types(type_map, agent_id)
    return next((h_type for h_type in _LEGACY_TOOL_ORDER if h_type in types), None)


def peer_tool_types(
    type_map: Mapping[str, str] | None,
    agent_id: str,
    offered: Collection[str] = TOOL_HANDOVER_TYPES,
) -> frozenset[str]:
    """The handover types whose tool reaches one peer on a turn that offers *offered*.

    A peer of a legacy map runs every type, a peer of an explicit map the type
    it runs as (``peer_handover_type``), a ``mandatory_transfer`` peer none.
    """
    offered_types = frozenset(offered).intersection(TOOL_HANDOVER_TYPES)
    if is_legacy_handover_map(type_map):
        return offered_types
    return offered_types & peer_handover_types(type_map, agent_id)


def reachable_handover_peers(
    peers: list[Agent],
    type_map: Mapping[str, str] | None,
    *,
    offered: Collection[str] = TOOL_HANDOVER_TYPES,
) -> list[Agent]:
    """The peers a turn lists under ``## Agent Delegation`` and hands its handover tools.

    The one list the prompt and the tools both read, so the model is never told
    to hand over to a peer it has no tool for, and no tool reaches a peer the
    prompt does not list: a peer stays when a tool of *offered*
    (``offered_handover_types``) reaches it (``peer_tool_types``). A
    ``mandatory_transfer`` peer never does. Each tool accepts only its own
    type's share of the list (``handover_peers_by_type``).
    """
    return [peer for peer in peers if peer_tool_types(type_map, str(peer.id), offered)]


def handover_peers_by_type(
    peers: list[Agent],
    type_map: Mapping[str, str] | None,
    *,
    offered: Collection[str] = TOOL_HANDOVER_TYPES,
) -> dict[str, list[Agent]]:
    """The peers each handover tool accepts, keyed by its type (every key of ``HANDOVER_TOOL_NAMES``).

    A tool reaches only the peers its own type reaches (``peer_tool_types``):
    ``consult_agent`` refuses a ``delegate`` peer as it refuses an unknown one,
    while a peer of a legacy map is accepted by every offered tool.
    """
    reach = {str(peer.id): peer_tool_types(type_map, str(peer.id), offered) for peer in peers}
    return {
        h_type: [peer for peer in peers if h_type in reach[str(peer.id)]]
        for h_type in TOOL_HANDOVER_TYPES
    }


def handover_tool_types(
    peers: list[Agent],
    type_map: Mapping[str, str] | None,
    *,
    offered: Collection[str] = TOOL_HANDOVER_TYPES,
) -> frozenset[str]:
    """The handover types whose tool a turn that offers *offered* registers for *peers*."""
    return frozenset().union(*(peer_tool_types(type_map, str(peer.id), offered) for peer in peers))


def handover_peer_tag(
    type_map: Mapping[str, str] | None,
    agent_id: str,
    type_instructions: Mapping[str, str],
    *,
    offered: Collection[str] = TOOL_HANDOVER_TYPES,
) -> tuple[str, str]:
    """The ``[TAG]`` and instruction a peer is listed with under ``## Agent Delegation``.

    Only for a peer of ``reachable_handover_peers`` with the same *offered*.
    In a map that holds a type, a peer is tagged with the type it runs as
    (``peer_handover_type``) and told that type's tool. An EMPTY map is the
    legacy mode in which every peer runs every type, so the peer is tagged
    ``ANY`` and told the tools the turn really registers for it
    (``peer_tool_types``). The legacy tag and instruction are shorter than the
    ``[DELEGATE]`` ones they replace, so the prompt does not grow.
    """
    legacy = is_legacy_handover_map(type_map)
    if legacy or len(peer_handover_types(type_map, agent_id)) > 1:
        types = peer_tool_types(type_map, agent_id, offered) or (
            frozenset(TOOL_HANDOVER_TYPES) if legacy else peer_handover_types(type_map, agent_id)
        )
        tools = [f"`{HANDOVER_TOOL_NAMES[h_type]}`" for h_type in _LEGACY_TOOL_ORDER if h_type in types]
        listed = ", ".join(tools[:-1])
        tag = (
            LEGACY_HANDOVER_TAG
            if legacy or len(types) == len(TOOL_HANDOVER_TYPES)
            else "|".join(h_type.upper() for h_type in _LEGACY_TOOL_ORDER if h_type in types)
        )
        return tag, f"Use {listed} or {tools[-1]}." if listed else f"Use {tools[-1]}."
    h_type = peer_handover_type(type_map, agent_id) or "delegate"
    return h_type.upper(), type_instructions[h_type]


# ── Handover Context Prompt ──────────────────────────────────────────────────


def format_handover_context_prompt(handover_context: dict) -> list[str]:
    """Render the system-prompt fragments for a handover context.

    Returns the context line describing HOW this agent was invoked (consult /
    transfer / background task / generic handover) plus, when present, the
    source-agent-response block — as a list of fragments to append to
    ``system_parts``.

    For **transfer** (and generic handover) the fragment states explicitly
    that the handover is ALREADY COMPLETE.  Without this, the target agent is
    re-invoked with the user's original message as the message to answer —
    and when that message is itself the routing request ("transfer me to the
    X assistant"), the target LLM reads it as a fresh, unfulfilled request
    and replies "you're already connected — no transfer is needed" instead
    of taking over the conversation.

    **Background tasks** (``sub_agent_task``) get task semantics instead:
    there is no live user to take over from or to greet, so the completion
    directive (and especially its "ask what they need" fallback) would push
    a worker toward a useless capability introduction instead of doing the
    delegated work.
    """
    ho_type = handover_context.get("type", "handover")
    ho_source = handover_context.get("source_agent", "another agent")
    ho_reason = handover_context.get("reason", "")
    # The task text is the run's message already: the prompt copy never
    # carries a detached $input document and is capped (#12095).
    ho_question = prompt_safe_task_text(handover_context.get("original_question", ""))

    if ho_type == "consult":
        ctx_line = (
            f"\nConsulted by {ho_source} — answer the question, "
            f"your response goes back to that agent."
        )
    elif ho_type == "sub_agent_task":
        ctx_line = (
            "\nYou are running as a delegated background task — complete "
            "the task described in the message and return the results; "
            "your output goes back to the delegating agent, not to a live "
            "user. Follow your persona and instructions above."
        )
    else:
        verb = "Transferred" if ho_type == "transfer" else "Handed over"
        ctx_line = (
            f"\n{verb} from {ho_source} — this conversation has been handed "
            f"over to you and the handover is ALREADY COMPLETE: you are now "
            f"the active agent. If the user's latest message is itself the "
            f"request to be transferred or connected to you, that request has "
            f"just been fulfilled by this handover — never reply that a "
            f"transfer is unnecessary or that the user is already connected. "
            f"Take over directly: address anything else in the message, or "
            f"briefly say what you can do and ask what they need. "
            f"Follow your persona and instructions above."
        )

    if ho_reason:
        ctx_line += f" Reason: {ho_reason}"
    if ho_question:
        ctx_line += f" Original question: {ho_question}"

    fragments = [ctx_line]
    # A background task's own identity (services/run_context.py), rendered
    # by the spawner: identifiers only.
    run_context = handover_context.get("run_context")
    if ho_type == "sub_agent_task" and isinstance(run_context, str) and run_context.strip():
        fragments.append(run_context)

    # When the source agent produced a response before handing over (e.g. a
    # mandatory transfer), inject it so the target agent has complete
    # context.  The verb matches the handover mode so this block never
    # contradicts the context line above ("Handed over from ..." must not be
    # followed by "before transferring to you").
    # Not in the run's message: kept whole, only a detached document's body
    # is replaced by its reference block.
    ho_response = prompt_safe_task_text(handover_context.get("source_agent_response", ""), max_chars=None)
    if ho_response:
        if ho_type == "transfer":
            response_verb = "transferring to"
        elif ho_type == "consult":
            response_verb = "consulting"
        else:
            response_verb = "handing over to"
        fragments.append(
            f"\n── Source Agent Response ──\n"
            f"The previous agent ({ho_source}) produced the following response "
            f"before {response_verb} you. Use this context to inform your "
            f"response — do NOT repeat it verbatim, but incorporate the "
            f"findings into your answer:\n\n{ho_response}"
        )
    return fragments


# ── Handover Agent Resolution ────────────────────────────────────────────────


def resolve_handover_agent(
    agent_id_or_slug: str,
    agents: list[Agent],
) -> Agent | None:
    """Resolve a handover agent by ID or slug from a list of candidates.

    Tries exact ID match first, then case-insensitive slug match.
    Returns the matching ``Agent`` or *None*.
    """
    # Exact ID match
    target = next((a for a in agents if str(a.id) == agent_id_or_slug), None)
    if target:
        return target
    # Case-insensitive slug match
    slug_lower = agent_id_or_slug.lower().strip()
    return next((a for a in agents if a.slug and a.slug.lower() == slug_lower), None)


def normalize_visited_agents(value: object) -> list[str]:
    """Return a NEW list of string agent ids from an untrusted chain value.

    Non-sequence values (``None``, a bare string, a dict, ...) yield an
    empty list — iterating a string would silently produce a bogus
    per-character chain that defeats cycle detection downstream while
    still looking like a valid list.  Use this everywhere a chain crosses
    a serialization or untyped boundary (orchestration payloads, handover
    contexts, legacy parameters).
    """
    if isinstance(value, (list, tuple)):
        return [str(a) for a in value]
    return []


def extract_visited_agents(
    handover_context: dict | None,
    transfer_chain: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    """Return the ordered visited-agents chain for handover cycle detection.

    The chain generalises the legacy ``transfer_chain`` (chat transfers only)
    to ALL handover types — delegate, consult and transfer — across both the
    chat and assignment paths.  Resolution order:

    1. The explicit **non-empty** ``transfer_chain`` argument (legacy
       ``run_agent_chat`` parameter, still used by the chat transfer path).
    2. The ``visited_agents`` key carried in ``handover_context``.
    3. The legacy ``transfer_chain`` key in ``handover_context`` (backward
       compatibility with old payloads).

    An EMPTY ``transfer_chain`` is deliberately treated like ``None`` and
    falls through to the context chain: an empty argument carries no chain
    information, and silently discarding an inherited ``handover_context``
    chain would weaken cycle detection.  There is no "reset the chain"
    semantic — the chain only ever grows along a handover path.

    Non-sequence values (e.g. a bare string) are treated like ``None`` for
    both sources — iterating a string would silently produce a bogus
    chain of characters.

    Always returns a NEW list of string agent ids (never mutates inputs).
    """
    if isinstance(transfer_chain, (list, tuple)) and transfer_chain:
        return normalize_visited_agents(transfer_chain)
    if handover_context:
        # Normalize each context key independently so a corrupted (e.g.
        # stringified) ``visited_agents`` value falls back to a valid legacy
        # ``transfer_chain`` instead of masking it.
        ctx_chain = normalize_visited_agents(handover_context.get("visited_agents"))
        if not ctx_chain:
            ctx_chain = normalize_visited_agents(
                handover_context.get("transfer_chain")
            )
        return ctx_chain
    return []


def handover_cycle_error(
    target_agent_id: str,
    visited_agents: list[str] | tuple[str, ...] | None,
    *,
    agent_names: dict[str, str] | None = None,
    target_name: str | None = None,
) -> str | None:
    """Return a friendly refusal message when a handover would create a cycle.

    ``visited_agents`` is the ordered chain of agent ids already involved in
    the current handover chain (the current agent included).  When the target
    agent is already in the chain the handover would loop (A -> B -> A), so a
    tool-result error string is returned for the LLM — callers must surface it
    instead of executing the handover (never raise).  Returns ``None`` when
    the handover is safe.

    ``agent_names`` optionally maps agent ids to display names for a readable
    chain; unknown ids fall back to the raw id.

    A non-sequence ``visited_agents`` (e.g. a bare string) is treated like
    ``None`` — iterating a string would produce a bogus chain of characters
    and yield false positives/negatives (mirrors ``extract_visited_agents``).
    """
    chain = normalize_visited_agents(visited_agents)
    if str(target_agent_id) not in chain:
        return None
    names = agent_names or {}
    target_label = target_name or names.get(
        str(target_agent_id), str(target_agent_id)
    )
    chain_labels = " -> ".join(names.get(a, a) for a in chain)
    return (
        f"Handover rejected: agent {target_label} is already in this "
        f"delegation chain ({chain_labels} -> {target_label}). Complete the "
        f"work yourself or choose a different agent."
    )
