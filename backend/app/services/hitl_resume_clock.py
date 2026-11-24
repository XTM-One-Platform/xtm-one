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

"""The clocks of a run paused for a person and resumed (issue #1645).

A HITL resume continues the run it paused: its first ``started_at`` is kept,
so the pause is never read as queue time, and the time spent in
``awaiting_human`` is counted apart on the run's ``trigger_context``
(:data:`AWAITING_HUMAN_SECONDS_KEY`, summed over every pause).

The resumed conversation is the one stored at the pause, system prompt
included: its ``Current date and time (UTC)`` line (and the local time line
under it) is restamped at the resume by :func:`restamp_prompt_clock`, so the
run never stamps what it writes with the time it paused at.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

#: Seconds the run spent in ``awaiting_human``, summed over its pauses.
AWAITING_HUMAN_SECONDS_KEY = "awaiting_human_seconds"
#: When the current pause began (ISO 8601), removed when it ends.
AWAITING_HUMAN_SINCE_KEY = "awaiting_human_since"

_LOCAL_LINE = re.compile(r"^User's local time \(([^)]+)\): .*$")


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _context(run: Any) -> dict[str, Any]:
    context = getattr(run, "trigger_context", None)
    return dict(context) if isinstance(context, dict) else {}


def mark_paused(run: Any, *, now: datetime | None = None) -> None:
    """Record on *run* that a pause for a person begins at *now*."""
    context = _context(run)
    context[AWAITING_HUMAN_SINCE_KEY] = _aware(now or datetime.now(UTC)).isoformat()
    # A new dict: the column tracks an assignment, not an in-place change.
    run.trigger_context = context


def close_pause(run: Any, *, now: datetime | None = None) -> float | None:
    """End *run*'s current pause at *now*; answer its length in seconds.

    The length is added to :data:`AWAITING_HUMAN_SECONDS_KEY`. A run paused
    before this was recorded has no start of pause: nothing is added and
    ``None`` is answered. Never raises on an unreadable value.
    """
    context = _context(run)
    since_raw = context.pop(AWAITING_HUMAN_SINCE_KEY, None)
    if since_raw is None:
        return None
    seconds: float | None = None
    try:
        since = _aware(datetime.fromisoformat(str(since_raw)))
        seconds = max(0.0, (_aware(now or datetime.now(UTC)) - since).total_seconds())
    except (TypeError, ValueError):
        seconds = None
    if seconds is not None:
        try:
            total = float(context.get(AWAITING_HUMAN_SECONDS_KEY) or 0)
        except (TypeError, ValueError):
            total = 0.0
        context[AWAITING_HUMAN_SECONDS_KEY] = round(total + seconds, 3)
    run.trigger_context = context
    return seconds


def awaiting_human_seconds(run: Any) -> float | None:
    """Seconds *run* spent waiting for a person, ``None`` when never recorded."""
    value = _context(run).get(AWAITING_HUMAN_SECONDS_KEY)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def restamp_prompt_clock(text: str) -> str:
    """*text* with its clock line, and the local time line under it, set to now.

    The lines are the ones ``format_datetime_prompt`` writes; the user's time
    zone is read off the local line itself. A text without a clock line is
    answered unchanged.
    """
    from app.llm.system_prompt_split import SYSTEM_VOLATILE_TAIL_MARKER
    from app.services.builtin_tools.prompting import format_datetime_prompt

    lines = text.split("\n")
    for index, line in enumerate(lines):
        if not line.startswith(SYSTEM_VOLATILE_TAIL_MARKER):
            continue
        timezone = None
        span = 1
        if index + 1 < len(lines):
            local = _LOCAL_LINE.match(lines[index + 1])
            if local:
                timezone = local.group(1)
                span = 2
        # An unreadable zone answers the UTC line alone: the stale local
        # line is replaced by nothing, never kept.
        fresh = format_datetime_prompt(timezone).lstrip("\n").split("\n")
        lines[index : index + span] = fresh
        return "\n".join(lines)
    return text
