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

"""What a chat turn cut by the graceful shutdown timeout leaves behind.

uvicorn's bounded drain cancels the requests still running when it expires (a
worker recycle, a rolling deploy, a restart). A chat turn in the middle of a
tool call then ended with nothing stored: no answer, so the person could not
tell what was done, and no memory, so the agent forgot the turn. The checkpoint
keeps the completed tool rounds for the next turn's model only.

:class:`TurnProgress` follows a turn's tool calls through the agent loop's
events: the calls started (``tool_start``), the calls that returned
(``tool_done``, with their outcome) and so the calls still running. On that
cancellation, and only that one (``is_graceful_shutdown_cancellation``):

- the web chat and REST turns store :func:`interrupted_reply` as the turn's
  assistant message, its metadata carrying the tool trace
  (:meth:`TurnProgress.meta`) and :data:`INTERRUPTED_META_KEY`;
- ``run_agent_chat`` records :func:`interrupted_memory_note` in the memory
  tiers the turn would have written.

Both writes run as shutdown finalizers (``core/shutdown_finalizers.py``).
Both say what stopped the process (``core/worker_shutdown.cut_cause``):
its own recycle, a stop signal from its host, or nothing it recorded.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field

from app.core.worker_shutdown import RECYCLE, STOP

#: Marks a message stored for a turn the graceful shutdown cut.
INTERRUPTED_META_KEY = "interrupted"
#: What stopped the process, as ``worker_shutdown.cut_cause`` read it.
INTERRUPTED_CAUSE_META_KEY = "interrupted_cause"
#: ``interrupted_resumable`` (its completed tool work is checkpointed and the
#: next turn resumes it) or ``interrupted``.
INTERRUPTED_STATE_META_KEY = "interrupted_state"
INTERRUPTED_RESUMABLE = "interrupted_resumable"

#: What stopped the process running ``{turn}``, by ``worker_shutdown`` cause.
_CUT_CAUSES = {
    RECYCLE: (
        "the web worker running {turn} was recycled after serving its request "
        "quota (MAX_REQUESTS) and could not wait any longer for {turn}"
    ),
    STOP: (
        "the server process running {turn} was stopped (a deployment, a scale-in, "
        "a node drain or a restart)"
    ),
}
_UNKNOWN_CUT_CAUSE = "the server process running {turn} shut down"

#: How much of a call's input and output the trace keeps.
TRACE_TEXT_LIMIT = 30_000
#: Distinct tools a list names before it sums the rest up.
MAX_LISTED_TOOLS = 12


def tool_trace_entry(event: dict) -> dict:
    """The reasoning details panel's record of one ``tool_done`` event.

    Input and output are bounded so a message's metadata and the stream's
    ``done`` frame stay within the column and frame limits, whatever a tool
    returned.
    """
    from app.tools.agent_loop import _trim_trace_text

    raw_input = event.get("tool_input_full") or event.get("tool_input") or ""
    raw_output = event.get("tool_output", "") or ""
    return {
        "name": event["tool"],
        "input": _trim_trace_text(raw_input, TRACE_TEXT_LIMIT),
        "output": _trim_trace_text(raw_output, TRACE_TEXT_LIMIT),
        "success": bool(event.get("success", True)),
    }


@dataclass
class TurnProgress:
    """The tool calls of a turn so far, read from its loop events."""

    #: Every call announced by ``tool_start``, in order.
    started: list[str] = field(default_factory=list)
    #: Every call that returned, as :func:`tool_trace_entry` records it.
    trace: list[dict] = field(default_factory=list)
    iterations: int = 0

    def observe(self, event: dict) -> None:
        status = event.get("status")
        if status == "tool_start" and event.get("tools"):
            self.started.extend(event["tools"])
        elif status == "thinking" and event.get("iteration"):
            self.iterations = max(self.iterations, event["iteration"])
        elif status == "tool_done" and event.get("tool"):
            self.trace.append(tool_trace_entry(event))

    def reset(self) -> None:
        """Start over: what was observed belongs to a message already stored."""
        self.started.clear()
        self.trace.clear()
        self.iterations = 0

    @property
    def running(self) -> list[str]:
        """The calls started that have not returned, latest of a name last."""
        returned = Counter(entry["name"] for entry in self.trace)
        running: list[str] = []
        for name in self.started:
            if returned[name] > 0:
                returned[name] -= 1
            else:
                running.append(name)
        return running

    def meta(self) -> dict:
        """The turn's tool trace, in the keys the reasoning details panel reads."""
        meta: dict = {}
        if self.started:
            meta["tool_names"] = list(dict.fromkeys(self.started))
            meta["tool_call_count"] = len(self.started)
            meta["iterations"] = self.iterations
        if self.trace:
            meta["tool_call_trace"] = list(self.trace)
        return meta


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _code(name: str) -> str:
    return f"`{name.replace('`', '')}`"


def _failed(entry: dict) -> bool:
    return not entry.get("success", True)


def _tool_lines(calls: Iterable[tuple[str, bool]]) -> list[str]:
    """One line per tool, in the order first called, its failures marked."""
    groups: dict[str, list[int]] = {}
    for name, failed in calls:
        counts = groups.setdefault(name, [0, 0])
        counts[0] += 1
        counts[1] += int(failed)
    lines: list[str] = []
    for name, (n, failed) in list(groups.items())[:MAX_LISTED_TOOLS]:
        if n == 1:
            lines.append(f"- {_code(name)}" + (" (failed)" if failed else ""))
        else:
            detail = _count(n, "call") + (f", {failed} failed" if failed else "")
            lines.append(f"- {_code(name)} ({detail})")
    if len(groups) > MAX_LISTED_TOOLS:
        lines.append(f"- and {_count(len(groups) - MAX_LISTED_TOOLS, 'other tool')}")
    return lines


def cut_cause_text(cause: str | None, *, turn: str = "it") -> str:
    """What stopped the process running a cut turn, named *turn* in the sentence."""
    return _CUT_CAUSES.get(cause or "", _UNKNOWN_CUT_CAUSE).format(turn=turn)


def interrupted_reply(progress: TurnProgress, cause: str | None = None) -> str:
    """The assistant message of a turn the graceful shutdown cut."""
    parts = [f"This turn was interrupted before it could finish: {cut_cause_text(cause)}."]
    if progress.trace:
        lines = _tool_lines((e["name"], _failed(e)) for e in progress.trace)
        parts.append(f"Completed tool calls ({len(progress.trace)}):\n" + "\n".join(lines))
    running = progress.running
    if running:
        lines = _tool_lines((name, False) for name in running)
        parts.append(f"Still running when it stopped ({len(running)}):\n" + "\n".join(lines))
    if progress.trace:
        last = progress.trace[-1]
        tail = ", which failed" if _failed(last) else ""
        parts.append(f"Last completed action: {_code(last['name'])}{tail}.")
    else:
        parts.append("No tool call had completed.")
    return "\n\n".join(parts)


#: How the memory note of a cut turn starts after the person's message.
INTERRUPTED_NOTE_LEAD = "Interrupted before the agent replied"


def interrupted_memory_prefix(asked_label: str) -> str:
    """The start of the memory entry a cut turn left for *asked_label*.

    The turn that answers the same message (its resume after a rollout) writes
    its reply in place of that entry, so the memory and the recent activity
    never keep "Interrupted" for a turn that was answered.
    """
    return f"{asked_label} → {INTERRUPTED_NOTE_LEAD}"


def interrupted_memory_note(progress: TurnProgress | None, cause: str | None = None) -> str:
    """What the memory of a cut turn says happened instead of a reply.

    *progress* is ``None`` when the turn had no event stream to read it from:
    the note then says nothing about its tool calls rather than something
    false.
    """
    note = f"{INTERRUPTED_NOTE_LEAD}: {cut_cause_text(cause, turn='the turn')}."
    if progress is None:
        return note
    if progress.trace:
        failed = sum(1 for e in progress.trace if _failed(e))
        last = progress.trace[-1]
        note += (
            f" {_count(len(progress.trace), 'tool call')} completed"
            + (f" ({failed} failed)" if failed else "")
            + f"; last completed action: {last['name']}"
            + (" (failed)" if _failed(last) else "")
            + "."
        )
    else:
        note += " No tool call had completed."
    running = progress.running
    if running:
        note += " Still running: " + ", ".join(dict.fromkeys(running)) + "."
    return note
