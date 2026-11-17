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

"""The ``Run context`` line: a run's own identity, told to the model running it.

An assignment run or a background task never saw its own ``run_id``: when it
recorded something about itself (a friction disposition, a shared-state note,
a watch entry) it named itself by the trace id ``inspect_my_traces`` shows,
and every reader keyed on the run id (``get_run``, the friction ledger, the
``run.completed_with_undisposed_frictions`` event) missed the record.

This module renders the one line appended to the run's prompt next to the
trigger information. It carries identifiers only (no payload, no secret):
``run_id``, ``assignment_id`` (``null`` for a run outside an assignment, such
as a chat turn's parent run or a delegation), the trigger type, and, for a
delegation, ``parent_run_id`` and the background task's ``task_id``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.run_clock import utc_stamp

RUN_CONTEXT_HEADER = "--- Run context ---"


def _text(value: Any) -> str:
    """An identifier as printed on the line; ``null`` when absent."""
    if value is None:
        return "null"
    text = str(value).strip()
    return text or "null"


def format_run_context(
    *,
    run_id: Any,
    assignment_id: Any = None,
    trigger_type: Any = None,
    parent_run_id: Any = None,
    task_id: Any = None,
    started_at: datetime | None = None,
) -> str:
    """The ``Run context`` block for a run, or ``""`` without a run id.

    ``parent_run_id`` and ``task_id`` are printed only when set, so a top-level
    run reads the three fields the acceptance names and nothing else. The
    start time (``started_at``, default now) and where the live clock is read
    follow on a line of their own (#8660).
    """
    if run_id is None or not str(run_id).strip():
        return ""
    fields = [
        f"run_id: {_text(run_id)}",
        f"assignment_id: {_text(assignment_id)}",
        f"trigger: {_text(trigger_type)}",
    ]
    if parent_run_id is not None and str(parent_run_id).strip():
        fields.append(f"parent_run_id: {_text(parent_run_id)}")
    if task_id is not None and str(task_id).strip():
        fields.append(f"task_id: {_text(task_id)}")
    return (
        f"\n\n{RUN_CONTEXT_HEADER}\n"
        + ", ".join(fields)
        + "\nName this run by its run_id (the id get_run reads) in anything you record about it."
        + f"\nStarted at {utc_stamp(started_at)} (UTC); after a wait or a resume, the time to stamp "
        "what you write is the one the wait answer or the resume note ends with (It is now ...)."
    )


def run_trigger_type(run: Any) -> str | None:
    """The trigger type of an ``AssignmentRun``: its context's, else its payload's."""
    context = getattr(run, "trigger_context", None) or {}
    payload = getattr(run, "trigger_payload", None) or {}
    value = (context.get("type") if isinstance(context, dict) else None) or (
        payload.get("trigger_type") if isinstance(payload, dict) else None
    )
    return str(value) if value else None


def build_run_context(run: Any) -> str:
    """The ``Run context`` block for an ``AssignmentRun`` (or a stand-in with its fields)."""
    return format_run_context(
        run_id=getattr(run, "id", None),
        assignment_id=getattr(run, "assignment_id", None),
        trigger_type=run_trigger_type(run),
        parent_run_id=getattr(run, "parent_run_id", None),
        started_at=getattr(run, "started_at", None),
    )
