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

"""The live clock a run reads where its own clock goes stale (issue #8660).

The prompt states ``Current date and time (UTC)`` once, when the turn starts.
After a ``wait`` or a resume the model had no newer clock and extrapolated, so
the times it wrote in its notes and records drifted by minutes from the
platform's own stamps.

:func:`clock_sentence` is the one sentence the ``wait`` answer and the resume
note end with: the time they hand the turn back at. It is grounding carried by
a tool result or the resume note (invariant 16), not system prompt text.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_stamp(now: datetime | None = None) -> str:
    """``now`` (default: the current time) as ``YYYY-MM-DDTHH:MM:SSZ`` in UTC."""
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def clock_sentence(now: datetime | None = None) -> str:
    """``It is now 2026-11-05T05:30:12Z (UTC).``"""
    return f"It is now {utc_stamp(now)} (UTC)."
