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

"""Prometheus series of the sandbox workspace.

Per process, like every collector of ``core/metrics.py``. The per-conversation
figures (size, last sweep, kernel restarts) are on the workspace row and in
``GET /api/v1/chat/conversations/{id}/sandbox-workspace``; the platform-wide
totals are gauges ``api/v1/metrics.py`` reads from the database.
"""

from __future__ import annotations

from app.core.metrics import get_or_create_counter, get_or_create_histogram

CALLS = get_or_create_counter(
    "xtmone_sandbox_workspace_calls_total",
    "code_interpreter calls by kernel mode (stateless / session) and workspace use (mounted / unmounted / unavailable).",
    ["mode", "workspace"],
)
BYTES = get_or_create_counter(
    "xtmone_sandbox_workspace_bytes_total",
    "Bytes the sandbox read from (materialised) or wrote to (committed) a workspace.",
    ["direction"],
)
OPERATIONS = get_or_create_counter(
    "xtmone_sandbox_workspace_operations_total",
    "Workspace operations (put, get, list, remove, ingest, materialize_repository, install, gc_delete) by outcome.",
    ["operation", "outcome"],
)
QUOTA_REFUSALS = get_or_create_counter(
    "xtmone_sandbox_workspace_quota_refusals_total",
    "Change sets a workspace refused for its quota or file limit, by operation.",
    ["operation"],
)
KERNEL_STARTS = get_or_create_counter(
    "xtmone_sandbox_kernel_starts_total",
    "session=true kernels started, by reason (new, restarted, reset).",
    ["reason"],
)
RESUME_RESULTS = get_or_create_counter(
    "xtmone_turn_resume_results_total",
    "Tool results an interrupted chat turn's checkpoint replayed into the next turn, by kind (replayed, truncated).",
    ["kind"],
)
SYNC_SECONDS = get_or_create_histogram(
    "xtmone_sandbox_workspace_sync_seconds",
    "Time spent moving a workspace in (materialise) and out (scan + upload) of the sandbox.",
    ["phase"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120),
)

#: The stages of one ``sandbox_run_command`` after the container create
#: (``xtmone_sandbox_create_seconds``) and the workspace upload
#: (``SYNC_SECONDS{phase="materialize"}``), which stay where they are.
COMMAND_STAGE_SECONDS = get_or_create_histogram(
    "xtmone_sandbox_command_stage_seconds",
    "Seconds one sandbox_run_command stage took (start, command, teardown), by outcome.",
    ["stage", "outcome"],
    buckets=(0.05, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1200),
)


def call(mode: str, workspace: str) -> None:
    CALLS.labels(mode=mode, workspace=workspace).inc()


def bytes_moved(direction: str, amount: int) -> None:
    if amount > 0:
        BYTES.labels(direction=direction).inc(amount)


def operation(name: str, outcome: str) -> None:
    OPERATIONS.labels(operation=name, outcome=outcome).inc()


def quota_refused(name: str) -> None:
    QUOTA_REFUSALS.labels(operation=name).inc()


def kernel_started(reason: str) -> None:
    KERNEL_STARTS.labels(reason=reason).inc()


def resume_results(kind: str, count: int) -> None:
    if count > 0:
        RESUME_RESULTS.labels(kind=kind).inc(count)


def observe_sync(phase: str, seconds: float) -> None:
    SYNC_SECONDS.labels(phase=phase).observe(max(0.0, seconds))


def observe_command_stage(stage: str, outcome: str, seconds: float) -> None:
    COMMAND_STAGE_SECONDS.labels(stage=stage, outcome=outcome).observe(max(0.0, seconds))
