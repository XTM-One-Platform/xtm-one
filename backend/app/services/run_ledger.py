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

"""A run's ledger: the model and tool calls it recorded for its own trace.

An entry's tokens, cost and call counts are not rebuilt from the progress
events the evaluated agent emits: those miss a helper's work — a delegated
agent sends no events at all — and some of the agent's own calls, which report
usage but no event (a continuation, a compaction summary, a forced final
answer).

Every ``run_agent_chat`` already records each of its model generations and tool
calls as a span in its trace context (``trace_ctx["local_spans"]``), in memory,
whatever the trace-storage switches say: they only filter what is written. The
ledger is that list, reduced to what the metrics need, and handed back to the
caller at the end of the run. An entry's figures are the ledgers of its whole
chain added up: the evaluated agent, its consult and transfer helpers, and the
delegated tasks they spawned.

The shapes are plain JSON so a ledger can ride in ``loop_outcome_out``, in a
delegated task's ``metadata_json`` and in a result's ``execution_details``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Literal, NotRequired, TypedDict

#: Where a run hands its ledger back, in ``loop_outcome_out`` and in a delegated
#: task's ``metadata_json``.
LEDGER_KEY = "ledger"

#: Where an evaluation result keeps its chain ledger, in ``execution_details``.
CHAIN_KEY = "chain"

#: The mark on a delegated task that belongs to an evaluation's chain, in its
#: ``metadata_json``: such a task records its ledger, and passes the mark to the
#: tasks it spawns in turn. Measuring changes nothing else about the task; the
#: same mark also runs it under the evaluation's rules on who it
#: serves, on the prompt guard and on tool approval
#: (with :data:`EVALUATION_TOOL_APPROVAL_KEY`), read by ``run_agent_chat``.
EVALUATION_CHAIN_KEY = "evaluation_chain"

#: The evaluation's tool approval setting on a delegated task within its
#: chain, in ``metadata_json``, beside :data:`EVALUATION_CHAIN_KEY`: the task runs
#: in another process, so it reads the setting from its own row. ``True`` holds
#: tool calls as configured, ``False`` skips the gate; absent outside an
#: evaluation, or on a task spawned before the setting.
EVALUATION_TOOL_APPROVAL_KEY = "evaluation_tool_approval"

#: Where a run's in-process helpers (consult, transfer) leave their ledgers: on the
#: calling run's root trace context, under which they ran. ``ledger_from_trace``
#: folds them into the caller's ledger, so a run's ledger is its sub-chain.
HELPER_LEDGERS_KEY = "helper_ledgers"

#: Why a chain's totals are unavailable (``ChainLedger["incomplete_reason"]``):
#: a delegated task still running at the wait's bound; the user cancelled the run
#: during the wait; a ledger missing, partial or unreadable; a reused source
#: recorded before chain ledgers existed.
IncompleteReason = Literal[
    "wait_bound_reached",
    "run_cancelled",
    "ledger_missing",
    "recorded_before_chain",
]

#: Set on an ``incomplete`` task of an evaluation's chain whose wait-budget
#: continuation was not spawned (no pending task, the fan-out cap, an error):
#: the evaluation's wait then has no continuation to wait for.
NO_CONTINUATION_KEY = "wait_continuation_not_spawned"

#: Where a run keeps the tasks it delegated within an evaluation's chain, on its
#: root trace context. The spawn tool records each task as it creates it
#: (``record_spawned_task``), so the ledger never reads a tool's name or output:
#: a look-alike tool cannot add a task, nor a renamed one lose it.
SPAWNED_TASKS_KEY = "ledger_spawned_task_ids"

#: Span types that are calls: a model generation, a tool execution.
_CALL_KINDS: dict[str, Literal["model", "tool"]] = {"GENERATION": "model", "TOOL": "tool"}


class LedgerCall(TypedDict, total=False):
    """One model generation or tool call, as the run's trace recorded it."""

    kind: Literal["model", "tool"]
    name: str
    model: str | None
    duration_ms: float
    outcome: Literal["success", "error", "timeout", "cancelled"]
    usage: dict[str, Any]


class RunLedger(TypedDict, total=False):
    """What one ``run_agent_chat`` hands back: its own calls and its in-process helpers'.

    ``partial`` marks a delegated task's ledger that holds only its last attempt:
    an earlier attempt the engine lost made calls no ledger recorded.
    """

    calls: list[LedgerCall]
    spawned_task_ids: list[str]
    retries: int
    partial: bool


class ModelUsage(TypedDict):
    """The normalised usage one serving model accounts for across the chain."""

    model: str | None
    usage: dict[str, Any]


class ChainLedger(TypedDict):
    """An entry's total: the evaluated agent's ledger and every spawned task's."""

    calls: list[LedgerCall]
    usage_by_model: list[ModelUsage]
    retries: int
    complete: bool
    incomplete_reason: NotRequired[IncompleteReason]


def uncounted_chain(reason: IncompleteReason) -> ChainLedger:
    """A chain whose totals are unavailable, for *reason*, with nothing to count."""
    return {"calls": [], "usage_by_model": [], "retries": 0, "complete": False, "incomplete_reason": reason}


def empty_ledger() -> RunLedger:
    """The ledger of a run that made no call: measured as empty, not missing."""
    return {"calls": [], "spawned_task_ids": []}


def tool_ledger_call(name: str, duration_ms: float, *, failed: bool) -> LedgerCall:
    """A tool call timed by its caller, for a path that opens no trace span."""
    return {
        "kind": "tool",
        "name": name,
        "duration_ms": float(duration_ms),
        "outcome": "error" if failed else "success",
    }


def _outcome(span: dict[str, Any]) -> Literal["success", "error", "timeout", "cancelled"]:
    """The metric suite's call outcome for a closed span.

    ``end_generation`` / ``end_tool_span`` record an exception as level ``ERROR``
    with ``"<ExceptionType>: <message>"`` as the status, so the type names a
    timeout or a cancellation apart from any other failure. ``WARNING`` (a
    truncated generation) is not a failure.
    """
    if span.get("level") != "ERROR":
        return "success"
    status = str(span.get("status_message") or "")
    if status.startswith(("TimeoutError", "asyncio.TimeoutError")):
        return "timeout"
    if status.startswith("CancelledError"):
        return "cancelled"
    return "error"


def _tool_name(span: dict[str, Any]) -> str:
    metadata = span.get("metadata") if isinstance(span.get("metadata"), dict) else {}
    name = metadata.get("tool_name")
    if isinstance(name, str) and name:
        return name
    return str(span.get("name") or "").removeprefix("tool:")


def calls_from_spans(spans: Iterable[object]) -> list[LedgerCall]:
    """The calls among *spans*, each a span as a dict (``local_spans``, or a stored
    ``LLMSpan`` row read into the same keys): model generations and tool calls."""
    calls: list[LedgerCall] = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        kind = _CALL_KINDS.get(str(span.get("type") or ""))
        if kind is None:
            continue
        call: LedgerCall = {
            "kind": kind,
            "duration_ms": float(span.get("duration_ms") or 0.0),
            "outcome": _outcome(span),
        }
        if kind == "model":
            call["name"] = str(span.get("name") or "")
            call["model"] = span.get("model") if isinstance(span.get("model"), str) else None
            if isinstance(span.get("usage_details"), dict):
                call["usage"] = dict(span["usage_details"])
        else:
            call["name"] = _tool_name(span)
        calls.append(call)
    return calls


def ledger_from_trace(trace_ctx: dict | None) -> RunLedger:
    """The ledger of the run whose trace *trace_ctx* belongs to.

    Reads the root's ``local_spans`` and the tasks its spawn tool recorded
    (``record_spawned_task``): a nested agent's trace is its own root
    (``langfuse_tracing.trace_root_ctx``), so a helper's calls are never counted
    here, only folded in through its ledger. A run with no trace context has an
    empty ledger, not a missing one.
    """
    from app.services.langfuse_tracing import trace_root_ctx

    root = trace_root_ctx(trace_ctx)
    spans = root.get("local_spans") if isinstance(root, dict) else None
    calls: list[LedgerCall] = []
    recorded = root.get(SPAWNED_TASKS_KEY) if isinstance(root, dict) else None
    spawned: list[str] = list(dict.fromkeys(recorded if isinstance(recorded, list) else []))
    calls.extend(calls_from_spans(spans if isinstance(spans, list) else []))
    helpers = root.get(HELPER_LEDGERS_KEY) if isinstance(root, dict) else None
    ledger: RunLedger = {"calls": calls, "spawned_task_ids": spawned}
    for helper in helpers if isinstance(helpers, list) else []:
        calls.extend(helper.get("calls") or [])
        spawned.extend(t for t in helper.get("spawned_task_ids") or [] if t not in spawned)
        if helper.get("partial"):
            # A helper whose own ledger could not be read: so is part of this one.
            ledger["partial"] = True
    return ledger


def record_spawned_task(trace_ctx: dict | None, task_id: str) -> None:
    """Keep a task the spawn tool just created on the spawning run's root trace context.

    *trace_ctx* is the context current while the spawn tool runs, so its root
    is that run's trace, whichever tool span it sits under. A run with no trace
    context has no ledger to hand the task back in.
    """
    from app.services.langfuse_tracing import trace_root_ctx

    root = trace_root_ctx(trace_ctx)
    if isinstance(root, dict):
        spawned = root.setdefault(SPAWNED_TASKS_KEY, [])
        if task_id not in spawned:
            spawned.append(task_id)


def record_helper_ledger(trace_ctx: dict | None, ledger: RunLedger) -> None:
    """Keep an in-process helper's ledger on the calling run's root trace context.

    *trace_ctx* is the context current when the helper was called — a tool span
    for a consult, the run itself for a transfer — so its root is the caller's
    trace. Each helper appends one whole ledger, so parallel consults cannot
    interleave. A caller with no trace context has nowhere to keep it, and no
    ledger of its own to fold it into.
    """
    from app.services.langfuse_tracing import trace_root_ctx

    root = trace_root_ctx(trace_ctx)
    if isinstance(root, dict):
        root.setdefault(HELPER_LEDGERS_KEY, []).append(ledger)


def _counts(usage: dict[str, Any]) -> dict[str, int]:
    """A normalised usage's counts. A ledger read back from a task row is plain JSON."""
    return {
        key: int(value)
        for key, value in usage.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def assemble_chain(
    own: RunLedger,
    task_ledgers: Mapping[str, RunLedger | None],
    *,
    entry_retries: int = 0,
    stop_reason: IncompleteReason | None = None,
) -> ChainLedger:
    """An entry's chain ledger: the evaluated agent's run and every delegated task's.

    *task_ledgers* maps each task reached from the entry to its ledger, or to
    ``None`` when the task's ledger is missing — still running at the wait's
    bound, or never written. Keyed by task id, so a task reached twice is
    counted once. A missing ledger, or the agent's own ledger marked partial,
    leaves the chain incomplete, which reports its totals unavailable rather
    than as the part measured. *stop_reason* is why the wait ended
    early, when it did.
    """
    calls: list[LedgerCall] = list(own.get("calls") or [])
    retries = int(entry_retries)
    for ledger in task_ledgers.values():
        if ledger is None:
            continue
        calls.extend(ledger.get("calls") or [])
        retries += int(ledger.get("retries") or 0)

    # Span usage is already normalised (``langfuse_tracing._normalize_usage``), so
    # its key-by-key sum is too. Kept per serving model: two models' conventions
    # are never added raw, and each is priced at its own table.
    from app.services.langfuse_tracing import _sum_normalized_usage

    by_model: dict[str | None, list[dict[str, int]]] = {}
    for call in calls:
        usage = call.get("usage")
        if call.get("kind") == "model" and isinstance(usage, dict) and usage:
            by_model.setdefault(call.get("model"), []).append(_counts(usage))

    chain: ChainLedger = {
        "calls": calls,
        "usage_by_model": [
            {"model": model, "usage": _sum_normalized_usage(usages)} for model, usages in by_model.items()
        ],
        "retries": retries,
        "complete": not own.get("partial") and all(ledger is not None for ledger in task_ledgers.values()),
    }
    if not chain["complete"]:
        chain["incomplete_reason"] = stop_reason or "ledger_missing"
    return chain


def with_task_ledger(
    metadata: Mapping[str, Any] | None,
    loop_outcome: Mapping[str, Any],
    retries: int,
    *,
    partial: bool = False,
) -> dict[str, Any]:
    """A delegated task's ``metadata_json`` with its run's ledger and retry count.

    Written in the same transaction as the task's terminal status, so a task the
    evaluation sees as finished always has its ledger. A run that recorded
    no ledger — it failed before starting — still gets an empty one: measured as
    empty, not missing. *partial*: an earlier attempt the engine lost ran this
    task too, and its calls are in no ledger.
    """
    ledger = dict(loop_outcome.get(LEDGER_KEY) or empty_ledger())
    ledger["retries"] = int(retries)
    if partial:
        ledger["partial"] = True
    return {**(metadata or {}), LEDGER_KEY: ledger}
