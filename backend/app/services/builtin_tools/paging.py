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

"""Offset paging shared by the coordination list tools.

``list_work_items`` and ``list_shared_state`` used to stop at 50 rows and say
nothing about the rest, so a sweep trusting one call silently missed rows.
They now take ``limit`` / ``offset``, count the total under the same filters,
and open their answer with the line built here whenever a page is cut.

A page is also bounded by what the answer can carry: the agent loop cuts any
tool result from its end to ``max_tool_result_chars``, so a list tool keeps
only the rows that fit ``answer_budget()`` and its first line names the rows
actually shown, the next offset being the first row left out.
"""

from __future__ import annotations

from typing import Any

#: Rows a list tool returns when the caller names no ``limit``.
DEFAULT_PAGE_LIMIT = 50
#: The largest page a list tool serves in one call.
MAX_PAGE_LIMIT = 200
#: The longest list answer when the running loop names no tool-result cap.
LIST_BUDGET_CHARS = 50_000
#: Room left for the loop's own truncation notice under its cap.
_LOOP_NOTICE_CHARS = 200
#: Room kept for the page line, the count line and the closing hint.
LIST_FRAME_CHARS = 600


def answer_budget() -> int:
    """How long a list answer may be: ``LIST_BUDGET_CHARS``, or less when the
    running loop caps tool results lower (``tool_result_char_cap``)."""
    from app.tools.base import tool_result_char_cap

    cap = tool_result_char_cap()
    if cap is None:
        return LIST_BUDGET_CHARS
    return max(min(LIST_BUDGET_CHARS, cap - _LOOP_NOTICE_CHARS), 0)


def rows_that_fit(blocks: list[str], budget: int, reserved: int = LIST_FRAME_CHARS) -> int:
    """How many of *blocks* (one rendered row each, in page order) fit in
    *budget* characters after *reserved*; at least one when there is any, so
    a page always moves forward (a single oversized row is cut by the loop,
    its id first, and the next offset still points past it)."""
    used = reserved
    for n, block in enumerate(blocks):
        used += len(block) + 1
        if used > budget:
            return max(n, 1)
    return len(blocks)


def page_bounds(limit: Any, offset: Any) -> tuple[int, int]:
    """``limit`` clamped to 1..MAX_PAGE_LIMIT (default DEFAULT_PAGE_LIMIT), ``offset`` to >= 0."""
    try:
        size = int(limit) if limit not in (None, "") else DEFAULT_PAGE_LIMIT
    except (TypeError, ValueError):
        size = DEFAULT_PAGE_LIMIT
    try:
        start = int(offset) if offset not in (None, "") else 0
    except (TypeError, ValueError):
        start = 0
    return min(max(size, 1), MAX_PAGE_LIMIT), max(start, 0)


def next_offset(offset: int, shown: int, total: int) -> int | None:
    """The offset of the next page, or None when this page reaches the end."""
    end = offset + shown
    return end if shown and end < total else None


def page_line(offset: int, shown: int, total: int, noun: str) -> str:
    """The first line of a list answer: what this page holds out of the total.

    A cut page names the offset of the next one, e.g. ``showing 1-50 of 63
    work items, offset=50 for the next page``; a page that reaches the end
    says so, so a sweep knows it read everything.
    """
    if not shown:
        return f"showing none of {total} {noun} at offset={offset}; use a smaller offset."
    first, last = offset + 1, offset + shown
    nxt = next_offset(offset, shown, total)
    if nxt is not None:
        return f"showing {first}-{last} of {total} {noun}, offset={nxt} for the next page."
    if offset:
        return f"showing {first}-{last} of {total} {noun}, the last page."
    return f"showing all {total} {noun}."
