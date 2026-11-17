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

"""Structured per-call observability logging for OpenSandbox executions.

Emits one structured ``sandbox_call`` log event per call to
``execute_python_opensandbox``, ``execute_js_tool_opensandbox``, and
``execute_python_tool_opensandbox`` (spec ``003-sandbox-call-logging``),
carrying CPU/RAM usage, time-to-execute, egress policy, and outcome —
so a log pipeline shipping stdout (e.g. into Grafana/Loki) can filter and
aggregate on these calls.

Deliberately independent of ``quota.py`` (quota enforcement / usage
ledger): this module owns only the observability record and never gates
or records quota state itself
(spec FR-011). See ``specs/003-sandbox-call-logging/data-model.md`` for
the authoritative field schema.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from opensandbox import Sandbox
    from opensandbox.models.execd import ExecutionComplete
    from opensandbox.models.sandboxes import SandboxMetrics

# Uses the stdlib logger (not structlog.get_logger()) plus `extra=` so the
# fields below surface as top-level structured JSON keys via the
# ExtraAdder processor in app.core.logging — a structlog-native logger
# here would break in production: app.core.logging's shared structlog
# config uses PrintLoggerFactory, whose PrintLogger objects have no
# `.name` attribute, which add_logger_name (also shared) unconditionally
# requires for any logger not routed through the stdlib bridge.
logger = logging.getLogger(__name__)

UsageType = Literal["code_interpreter", "sandbox_command", "custom_tool_js", "custom_tool_python"]
Outcome = Literal["success", "failure"]
FailureReason = Literal[
    "sandbox_ready_timeout",
    "sandbox_create_failed",
    "sandbox_create_error",
    "nonzero_exit",
    "execution_error",
]

# Bounded so a slow/unreachable metrics endpoint degrades to null resource
# fields instead of delaying the caller (spec FR-007a, /speckit-clarify
# Q2) — synchronous-with-bounded-timeout was chosen over fire-and-forget
# so the log record stays complete on the common case without risking an
# unbounded hang on the uncommon one.
_METRICS_FETCH_TIMEOUT_SECONDS = 3.0
# The same reasoning for the kill: the call it follows has ended, and the
# sandbox's TTL is the backstop, so an unanswered kill never holds it.
_KILL_TIMEOUT_SECONDS = 5.0


async def _fetch_metrics(sandbox: Sandbox) -> SandboxMetrics | None:
    """Best-effort sandbox resource metrics, bounded by a short timeout.

    Returns ``None`` on any failure (timeout, SDK error, or anything else)
    rather than raising — a metrics-collection failure must never block or
    fail the sandbox call it's describing (spec FR-007).
    """
    try:
        return await asyncio.wait_for(sandbox.get_metrics(), timeout=_METRICS_FETCH_TIMEOUT_SECONDS)
    except Exception:
        return None


async def kill_sandbox_best_effort(sandbox: Sandbox) -> None:
    """Terminate the remote sandbox now instead of waiting out its TTL.

    Call only after ``log_sandbox_call`` (which still needs the sandbox
    alive to fetch metrics) - see that function's docstring. Best-effort:
    the call this sandbox served has already completed by the time this
    runs, and OpenSandbox's own server-side TTL (the ``timeout=`` passed to
    ``Sandbox.create``) is still the backstop if this doesn't run or fails,
    so a failure here is logged but never raised, and a kill the server does
    not answer is given up after ``_KILL_TIMEOUT_SECONDS``.
    """
    try:
        await asyncio.wait_for(sandbox.kill(), timeout=_KILL_TIMEOUT_SECONDS)
    except Exception:
        logger.debug("Best-effort sandbox kill failed (TTL will still expire it)", exc_info=True)


async def log_sandbox_call(
    *,
    usage_type: UsageType,
    outcome: Outcome,
    user_id: uuid.UUID | None = None,
    failure_reason: FailureReason | None = None,
    sandbox: Sandbox | None = None,
    execution_complete: ExecutionComplete | None = None,
    egress_allow_list: frozenset[str] | list[str] = (),
) -> None:
    """Emit one structured ``sandbox_call`` log record (spec FR-001).

    *sandbox*, when given, is used to fetch CPU/RAM metrics (spec FR-003) —
    pass it only when the sandbox is still live (inside the `async with
    sandbox:` block). *execution_complete* (an ``Execution.complete``
    value) supplies the TTE (spec FR-004) when the run reached completion.
    Both are optional and independently omittable: a call that never
    created a sandbox, or whose execution never completed, simply logs
    without those fields (spec FR-007) rather than raising.
    """
    fields: dict[str, object] = {
        "usage_type": usage_type,
        "outcome": outcome,
        "egress_default_action": "deny",
        "egress_allow_list": sorted(egress_allow_list),
    }

    if user_id is not None:
        fields["user_id"] = str(user_id)

    if outcome == "failure" and failure_reason is not None:
        fields["failure_reason"] = failure_reason

    if execution_complete is not None:
        fields["tte_ms"] = execution_complete.execution_time_in_millis

    if sandbox is not None:
        metrics = await _fetch_metrics(sandbox)
        if metrics is not None:
            fields["cpu_used_percentage"] = metrics.cpu_used_percentage
            fields["cpu_count"] = metrics.cpu_count
            fields["memory_used_in_mib"] = metrics.memory_used_in_mib
            fields["memory_total_in_mib"] = metrics.memory_total_in_mib

    log = logger.info if outcome == "success" else logger.warning
    log("sandbox_call", extra=fields)


async def log_sandbox_create_failure(
    *,
    usage_type: UsageType,
    user_id: uuid.UUID | None,
    exc: Exception,
    egress_allow_list: frozenset[str] | list[str] = (),
) -> None:
    """Classify and log a `Sandbox.create` failure (research.md §7).

    Shared by all three call sites so the ``SandboxReadyTimeoutException``
    vs. other-``SandboxException`` vs. generic-``Exception`` classification
    lives in exactly one place — duplicating it per call site risked one
    site silently drifting out of sync with the others.
    """
    from opensandbox.exceptions.sandbox import SandboxException, SandboxReadyTimeoutException

    if isinstance(exc, SandboxReadyTimeoutException):
        failure_reason: FailureReason = "sandbox_ready_timeout"
    elif isinstance(exc, SandboxException):
        failure_reason = "sandbox_create_failed"
    else:
        failure_reason = "sandbox_create_error"

    await log_sandbox_call(
        usage_type=usage_type,
        user_id=user_id,
        outcome="failure",
        failure_reason=failure_reason,
        egress_allow_list=egress_allow_list,
    )


@asynccontextmanager
async def log_execution_errors(
    *,
    usage_type: UsageType,
    user_id: uuid.UUID | None,
    egress_allow_list: frozenset[str] | list[str] = (),
) -> AsyncIterator[None]:
    """Wrap a sandbox command call so any exception it raises is logged once.

    Deliberately does **not** pass ``sandbox=`` to `log_sandbox_call` here:
    an exception from ``commands.run()`` itself typically means the
    sandbox's connection is already unhealthy, so attempting a metrics
    fetch over that same channel would just burn the full bounded timeout
    for no benefit, adding latency to an already-failing call. It would
    also await inside this ``except`` block before the bare ``raise``,
    creating a window where a task cancellation could replace the original
    exception instead of letting it propagate unchanged (SC-004) — passing
    no ``sandbox`` means `log_sandbox_call` has no `await` left to take,
    so this block cannot be cancelled between logging and re-raising.
    """
    try:
        yield
    except Exception:
        await log_sandbox_call(
            usage_type=usage_type,
            user_id=user_id,
            outcome="failure",
            failure_reason="execution_error",
            egress_allow_list=egress_allow_list,
        )
        raise
