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

"""The text answer of a delegated task: stored whole, shown by window, never cut silently.

A delegated task's answer used to be stored as ``answer[:50_000]``: whatever
the child wrote past that was thrown away at write time, and
``get_full_task_result`` then handed the head over as "the FULL output". A
parent could neither tell the answer was cut nor read the rest.

This module is the one place that decides how much of an answer is kept and
how a shorter view of it is labelled:

* :func:`stored_answer` - what goes into ``SubAgentTask.result`` /
  ``AgentDelegation.result``: the whole answer up to :data:`STORED_ANSWER_CHARS`
  (a safety cap far above any real answer), and past it a head that ENDS with a
  marker naming the characters dropped, so even that cut is never silent;
* :func:`answer_window` - one window of a stored answer (``offset``,
  ``max_chars``) with a header line naming the total size, the range shown and
  the next offset (or the end), for ``get_full_task_result``;
* :func:`clipped` - a head of a text for a preview, a run summary, an event
  payload or a wake prompt, ending with ``[truncated: N of M characters shown
  ...]`` and how to read the whole text.
"""

from __future__ import annotations

from dataclasses import dataclass

#: What a task's answer may hold. The column is ``Text``: this bounds one row,
#: not a real answer (a 120,000-character report is stored whole).
STORED_ANSWER_CHARS = 1_000_000

#: One window of ``get_full_task_result`` when the caller names none - the
#: size the tool used to return whole, so a caller passing ``task_id`` alone
#: reads an answer under it exactly as before.
DEFAULT_WINDOW_CHARS = 50_000

#: The largest window a caller may ask for in one read.
MAX_WINDOW_CHARS = 150_000

#: The head of the answer kept as the linked assignment run's summary.
RUN_SUMMARY_CHARS = 10_000

_STORED_CUT_MARKER = "\n\n[truncated at storage: the answer was {total:,} characters, the first {kept:,} are kept]"


def stored_answer(answer: str | None) -> str:
    """The answer as stored on the task: whole, or a marked head past the cap."""
    text = answer or ""
    if len(text) <= STORED_ANSWER_CHARS:
        return text
    marker = _STORED_CUT_MARKER.format(total=len(text), kept=STORED_ANSWER_CHARS)
    return text[: STORED_ANSWER_CHARS - len(marker)] + marker


def clipped(text: str | None, limit: int, *, read_call: str = "") -> str:
    """*text* whole when it fits *limit*, else its head and one truncation line.

    The line names the characters shown, the total, and *read_call* - the call
    that returns the whole text - when one is given.
    """
    body = text or ""
    if len(body) <= limit:
        return body
    head = body[:limit].rstrip()
    where = f"; read the rest with {read_call}" if read_call else ""
    return f"{head}\n[truncated: {len(head):,} of {len(body):,} characters shown{where}]"


@dataclass(frozen=True)
class AnswerWindow:
    """One window of a stored answer."""

    text: str
    offset: int
    total: int
    next_offset: int | None

    @property
    def is_whole(self) -> bool:
        """The window is the whole answer: nothing to say about paging."""
        return self.offset == 0 and self.next_offset is None

    def header(self, task_id: str) -> str:
        """One line: total size, range shown, and how to read the rest."""
        end = self.offset + len(self.text)
        shown = f"characters {self.offset:,} to {end:,} of {self.total:,}"
        if self.next_offset is None:
            return f"[Task answer: {shown}; this is the end.]"
        return (
            f"[Task answer: {shown}; read the rest with "
            f'get_full_task_result(task_id="{task_id}", offset={self.next_offset})]'
        )


def answer_window(answer: str | None, offset: int = 0, max_chars: int = DEFAULT_WINDOW_CHARS) -> AnswerWindow:
    """The window of *answer* from *offset*, at most *max_chars* long.

    A negative offset reads from 0; an offset past the end answers an empty
    window at the end; *max_chars* is held between 1 and
    :data:`MAX_WINDOW_CHARS`.
    """
    text = answer or ""
    total = len(text)
    start = min(max(int(offset or 0), 0), total)
    size = min(max(int(max_chars or DEFAULT_WINDOW_CHARS), 1), MAX_WINDOW_CHARS)
    end = min(start + size, total)
    return AnswerWindow(
        text=text[start:end],
        offset=start,
        total=total,
        next_offset=end if end < total else None,
    )
