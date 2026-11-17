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

"""Machine activity - what an agent did on a runner, as the chat shows it.

The ``computer_use`` tools answer the MODEL in prose (``Runner 'x' exit code:
0 ...``). This module builds what the HUMAN sees instead: one record per runner
command, rendered by the chat as a card in the transcript - the command line
and its output in a small terminal window, the file read or written, the folder
listed, the capture taken - live while the turn runs and persisted with the
message, so a reload, a relayed socket and a shared viewer show the
same cards.

* :class:`RunnerActivity` follows one command through the tool funnel
  (``computer_use._run_command``) and reports it through the tool progress
  channel (``emit_tool_event``): ``runner_command_started`` when the
  card appears or changes state, ``runner_command_finished`` when it ends.
  Every frame carries the FULL record - an upsert keyed by ``command_id`` - so
  a client that missed the start (a socket resumed mid-turn, a refusal that
  never reached a runner) still renders the card. The record is nested under
  ``command``: the loop owns the frame's ``status`` / ``tool`` keys.
* The record is bounded to the tool event cap (``MAX_TOOL_EVENT_PAYLOAD_CHARS``):
  previews are head / tail excerpts of the raw values, rebuilt at smaller
  budgets until the frame fits. What a card cannot hold stays in the message's
  ``tool_call_trace``.
* :class:`RunnerActivityLog` is the chat's per-turn fold of those frames (a
  consulted agent's included, under ``consult_progress``), persisted under
  :data:`RUNNER_ACTIVITY_META_KEY`; a record still live when it is persisted
  never reported back and is persisted as ``interrupted``.

Display data only: the model never reads it, and the tool result the model
reads is unchanged (no prompt text, AGENTS.md invariant 16). What a person
typed through the agent (``computer_action`` / ``browser_control`` ``type``)
is carried as a length, never as text.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from app.tools.tool_events import (
    MAX_TOOL_EVENT_PAYLOAD_CHARS,
    RUNNER_COMMAND_FINISHED_EVENT,
    RUNNER_COMMAND_STARTED_EVENT,
    TOOL_PROGRESS_STATUS,
    emit_tool_event,
    tool_event_sink_bound,
)

if TYPE_CHECKING:
    from app.models.runner import Runner

logger = logging.getLogger(__name__)

#: Assistant message metadata key (and ``done`` frame / API field) holding the
#: turn's records, in the order the commands were first reported.
RUNNER_ACTIVITY_META_KEY = "runner_activity"

#: The two frames a command produces, both carrying the full record.
RUNNER_ACTIVITY_EVENTS = frozenset({RUNNER_COMMAND_STARTED_EVENT, RUNNER_COMMAND_FINISHED_EVENT})

# ── Record statuses ─────────────────────────────────────────────────────────
STATUS_RUNNING = "running"
STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
#: The user rejected the held command.
STATUS_REJECTED = "rejected"
#: Held for approval and not decided in this turn - it stays queued on the
#: Desktop page (or the user cannot decide for that machine).
STATUS_NOT_APPROVED = "not_approved"
#: The machine's permission policy refused it before dispatch.
STATUS_REFUSED = "refused"
#: The runner was at its concurrent-command limit; nothing was queued.
STATUS_BUSY = "busy"
#: No runner could be targeted (offline, unknown id, none available).
STATUS_UNAVAILABLE = "unavailable"
#: The turn ended (or the row was cancelled) before the command reported back.
STATUS_INTERRUPTED = "interrupted"
#: The user stopped the running command from its card.
STATUS_STOPPED = "stopped"

#: Statuses a card shows while the tool is still waiting on the command.
LIVE_STATUSES = frozenset({STATUS_RUNNING, STATUS_AWAITING_APPROVAL})
RECORD_STATUSES = LIVE_STATUSES | frozenset(
    {
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_TIMEOUT,
        STATUS_REJECTED,
        STATUS_NOT_APPROVED,
        STATUS_REFUSED,
        STATUS_BUSY,
        STATUS_UNAVAILABLE,
        STATUS_INTERRUPTED,
        STATUS_STOPPED,
    }
)

#: Runner command kinds a record describes (``host_facts`` is never a tool call).
ACTIVITY_KINDS = frozenset(
    {
        "run_shell",
        "read_file",
        "write_file",
        "list_dir",
        "screenshot",
        "computer_action",
        "browser_control",
    }
)

#: Most records one message persists (the latest win). A turn that drives a
#: machine harder than this still has every call in ``tool_call_trace``.
MAX_RUNNER_ACTIVITY_RECORDS = 60

# ── Bounds ──────────────────────────────────────────────────────────────────
#: The encoded ``{"event", "command"}`` payload must stay under the tool event
#: cap; the margin is for the event name difference and rounding.
RECORD_CHAR_BUDGET = MAX_TOOL_EVENT_PAYLOAD_CHARS - 64
#: Successive budget scales the previews are rebuilt at until the frame fits;
#: the last one keeps no preview at all (the card still says what ran).
_FIT_SCALES = (1.0, 0.6, 0.35, 0.2, 0.1, 0.0)

_COMMAND_CHARS = 2_000
_CWD_CHARS = 1_000
_PATH_CHARS = 1_000
_URL_CHARS = 1_000
_SELECTOR_CHARS = 300
_KEYS_CHARS = 100
_SHORT_CHARS = 60
_TITLE_CHARS = 300
_ERROR_CHARS = 1_000
_STDOUT_CHARS = 4_000
_STDERR_CHARS = 2_000
_FILE_PREVIEW_CHARS = 4_000
_WRITE_PREVIEW_CHARS = 3_000
_MAX_ENTRIES = 200
_ENTRY_NAME_CHARS = 200
#: Marker between the head and the tail of a cut preview.
_ELISION = "\n...\n"


def _clip(value: object, limit: int) -> str:
    """*value* as a single bounded string (``...`` marks a cut)."""
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _scaled(base: int, scale: float, floor: int = 0) -> int:
    return max(floor, int(base * scale))


def head_tail(text: str, limit: int) -> tuple[str, bool]:
    """A preview of *text* within *limit* characters, and whether it was cut.

    Keeps the head AND the tail - the tail weighted heavier, since the end of
    a command's output is where its errors and its summary usually are.
    """
    if len(text) <= limit:
        return text, False
    if limit <= len(_ELISION):
        return "", bool(text)
    room = limit - len(_ELISION)
    head = room * 2 // 5
    tail = room - head
    return text[:head] + _ELISION + text[len(text) - tail :], True


def head(text: str, limit: int) -> tuple[str, bool]:
    """The first *limit* characters of *text*, and whether it was cut."""
    if len(text) <= limit:
        return text, False
    return text[: max(0, limit)], True


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _str_or_none(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _render_input(kind: str, payload: dict[str, Any], scale: float, content_bytes: int) -> dict[str, Any]:
    """What the agent asked for, as the card displays it."""
    if kind == "run_shell":
        command, cut = head_tail(str(payload.get("command") or ""), _scaled(_COMMAND_CHARS, scale, 160))
        out: dict[str, Any] = {"command": command}
        if cut:
            out["command_truncated"] = True
        cwd = _str_or_none(payload.get("cwd"))
        if cwd:
            out["cwd"] = _clip(cwd, _CWD_CHARS)
        return out
    if kind in ("read_file", "list_dir"):
        return {"path": _clip(payload.get("path") or "", _PATH_CHARS)}
    if kind == "write_file":
        content = str(payload.get("content") or "")
        preview, cut = head(content, _scaled(_WRITE_PREVIEW_CHARS, scale))
        return {
            "path": _clip(payload.get("path") or "", _PATH_CHARS),
            "content": preview,
            "content_truncated": cut,
            "content_bytes": content_bytes,
        }
    if kind == "screenshot":
        display = _str_or_none(payload.get("display"))
        return {"display": _clip(display, _SHORT_CHARS)} if display else {}
    if kind in ("computer_action", "browser_control"):
        out = {}
        for key in ("action", "button", "browser", "tab_id"):
            value = _str_or_none(payload.get(key))
            if value:
                out[key] = _clip(value, _SHORT_CHARS)
        for key in ("x", "y", "x2", "y2", "amount"):
            number = _int_or_none(payload.get(key))
            if number is not None:
                out[key] = number
        keys = _str_or_none(payload.get("keys"))
        if keys:
            out["keys"] = _clip(keys, _KEYS_CHARS)
        url = _str_or_none(payload.get("url"))
        if url:
            out["url"] = _clip(url, _URL_CHARS)
        selector = _str_or_none(payload.get("selector"))
        if selector:
            out["selector"] = _clip(selector, _SELECTOR_CHARS)
        text = payload.get("text")
        if isinstance(text, str) and text:
            # A length, never the text: it may be a password typed into a form.
            out["text_length"] = len(text)
        return out
    return {}


def _entry(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    name = _str_or_none(raw.get("name"))
    if name is None:
        return None
    entry: dict[str, Any] = {"name": _clip(name, _ENTRY_NAME_CHARS), "is_dir": bool(raw.get("is_dir"))}
    size = _int_or_none(raw.get("size"))
    if size is not None and not entry["is_dir"]:
        entry["size"] = size
    return entry


def _listing(result: dict[str, Any]) -> list[dict[str, Any]]:
    """A listing's entries, folders first then names - the order a file manager shows."""
    raw_entries = result.get("entries")
    entries = [e for e in (_entry(r) for r in raw_entries) if e] if isinstance(raw_entries, list) else []
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].casefold()))
    return entries


def _render_output(
    kind: str,
    result: dict[str, Any],
    file_id: str | None,
    scale: float,
    listing: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """What came back, as the card displays it; ``None`` when nothing does."""
    if kind == "run_shell" and result.get("background"):
        # A background job: what the card shows is the job, started.
        first, first_cut = head_tail(str(result.get("first_output") or ""), _scaled(_STDOUT_CHARS, scale))
        return {
            "background": True,
            "job_id": _str_or_none(result.get("job_id")),
            "pid": _int_or_none(result.get("pid")),
            "job_status": _str_or_none(result.get("status")),
            "exit_code": None,
            "stdout": first,
            "stdout_truncated": first_cut,
            "stderr": "",
            "stderr_truncated": False,
        }
    if kind == "run_shell":
        stdout, stdout_cut = head_tail(str(result.get("stdout") or ""), _scaled(_STDOUT_CHARS, scale))
        stderr, stderr_cut = head_tail(str(result.get("stderr") or ""), _scaled(_STDERR_CHARS, scale))
        return {
            "exit_code": _int_or_none(result.get("exit_code")),
            "stdout": stdout,
            "stdout_truncated": stdout_cut,
            "stderr": stderr,
            "stderr_truncated": stderr_cut,
        }
    if kind == "read_file":
        size = _int_or_none(result.get("size"))
        if file_id:
            return {"binary": True, "size": size, "file_id": file_id}
        content, cut = head(str(result.get("content") or ""), _scaled(_FILE_PREVIEW_CHARS, scale))
        return {"content": content, "content_truncated": cut, "size": size}
    if kind == "write_file":
        return {"bytes_written": _int_or_none(result.get("bytes_written"))}
    if kind == "list_dir":
        limit = _scaled(_MAX_ENTRIES, scale)
        return {
            "entries": listing[:limit],
            "entry_count": len(listing),
            "entries_truncated": len(listing) > limit,
        }
    if kind in ("screenshot", "browser_control"):
        out: dict[str, Any] = {}
        if file_id:
            out["file_id"] = file_id
        for key in ("width", "height"):
            number = _int_or_none(result.get(key))
            if number is not None:
                out[key] = number
        url = _str_or_none(result.get("url"))
        if url:
            out["url"] = _clip(url, _URL_CHARS)
        title = _str_or_none(result.get("title"))
        if title:
            out["title"] = _clip(title, _TITLE_CHARS)
        content = result.get("content")
        if isinstance(content, str):
            out["content_chars"] = len(content)
        return out or None
    return None


def encoded_size(record: dict[str, Any]) -> int:
    """Size of the tool event payload carrying *record*, as the cap measures it."""
    return len(
        json.dumps(
            {"command": record, "event": RUNNER_COMMAND_FINISHED_EVENT},
            default=str,
            allow_nan=False,
        )
    )


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


#: The longest name a card carries for who stopped its command.
_STOPPED_BY_NAME_CHARS = 120


def _stopped_by(raw: object) -> dict[str, Any] | None:
    """Who stopped a command from its card, from a result or a record: ``{user_id, name}`` or ``None``."""
    value = raw.get("stopped_by") if isinstance(raw, dict) else None
    if not isinstance(value, dict) or not isinstance(value.get("user_id"), str) or not value["user_id"]:
        return None
    name = value.get("name")
    return {"user_id": value["user_id"], "name": _clip(name, _STOPPED_BY_NAME_CHARS) if isinstance(name, str) and name else None}


class RunnerActivity:
    """One runner command on its way through the ``computer_use`` funnel.

    Holds the RAW values (the dispatched payload, the runner's result) and
    renders the bounded record from them on each report, so a preview shrunk
    to fit is always cut from the original, never re-cut from a cut. What does
    not depend on the budget (the size of a write, the sorted listing) is
    computed once, not at every step of the ladder.
    """

    def __init__(self, kind: str, payload: dict[str, Any] | None, *, command_id: str | None = None) -> None:
        self.kind = kind
        self.payload = dict(payload or {})
        # A synthetic id until the runner queue mints the real one: a command
        # refused before dispatch still needs a card key - unique, since it is
        # the key two cards of one batch are upserted by.
        self.command_id = command_id or f"local-{uuid.uuid4().hex}"
        self._content_bytes = (
            len(str(self.payload.get("content") or "").encode("utf-8", errors="replace"))
            if kind == "write_file"
            else 0
        )
        self._listing: list[dict[str, Any]] | None = None
        self.started_at = _iso_now()
        self.status = STATUS_RUNNING
        self.runner_id: str | None = None
        self.runner_name: str | None = None
        self.platform: str | None = None
        self.local = False
        self.result: dict[str, Any] = {}
        self.file_id: str | None = None
        self.error: str | None = None
        self.duration_ms: int | None = None
        self._run_started: float | None = None

    def bind_runner(self, runner: Runner, *, local: bool) -> None:
        self.runner_id = str(runner.id)
        self.runner_name = runner.name
        self.platform = runner.platform
        self.local = local

    def mark_running(self) -> None:
        """The command may run from now on (dispatched, or approved)."""
        self.status = STATUS_RUNNING
        self._run_started = time.monotonic()

    def settle(
        self,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        file_id: str | None = None,
        error: str | None = None,
    ) -> None:
        self.status = status
        if isinstance(result, dict):
            self.result = result
            self._listing = None
        self.file_id = file_id
        self.error = error
        if self._run_started is not None:
            self.duration_ms = int((time.monotonic() - self._run_started) * 1000)

    def settle_from_result(self, command: dict[str, Any] | None) -> None:
        """Map what ``await_result`` returned onto the record."""
        if command is None:
            self.settle(STATUS_FAILED, error="The command could not be found after dispatch.")
            return
        state = command.get("status")
        payload = command.get("result") if isinstance(command.get("result"), dict) else None
        error = _str_or_none(command.get("error"))
        if state == "completed":
            self.settle(STATUS_COMPLETED, result=payload, file_id=_str_or_none(command.get("result_file_id")))
        elif state in ("pending", "dispatched"):
            self.settle(STATUS_TIMEOUT, error="Timed out waiting for the machine to complete the command.")
        elif state == "timeout":
            self.settle(STATUS_TIMEOUT, result=payload, error=error)
        elif state == "cancelled":
            self.settle(STATUS_INTERRUPTED, error=error)
        elif payload is not None and payload.get("stopped") == "user":
            self.settle(STATUS_STOPPED, result=payload, error=error)
        else:
            self.settle(STATUS_FAILED, result=payload, error=error or "unknown error")

    def _entries(self) -> list[dict[str, Any]]:
        """The listing a ``list_dir`` result holds, normalised and sorted once."""
        if self._listing is None:
            self._listing = _listing(self.result) if self.kind == "list_dir" else []
        return self._listing

    def _render(self, scale: float) -> dict[str, Any]:
        record: dict[str, Any] = {
            "command_id": self.command_id,
            "kind": self.kind,
            "runner_id": self.runner_id,
            "runner_name": self.runner_name,
            "platform": self.platform,
            "local": self.local,
            "status": self.status,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "input": _render_input(self.kind, self.payload, scale, self._content_bytes) if scale > 0 else {},
            "output": (
                _render_output(self.kind, self.result, self.file_id, scale, self._entries())
                if self.status == STATUS_COMPLETED or self.result or self.file_id
                else None
            ),
            "error": _clip(self.error, _ERROR_CHARS) if self.error else None,
            "stopped_by": _stopped_by(self.result) if self.status == STATUS_STOPPED else None,
        }
        return record

    def record(self) -> dict[str, Any]:
        """The bounded record: the largest previews that fit the event cap."""
        for scale in _FIT_SCALES:
            candidate = self._render(scale)
            if encoded_size(candidate) <= RECORD_CHAR_BUDGET:
                return candidate
        # Unreachable with the bounds above (every field is clipped); keep the
        # frame deliverable whatever a future field adds.
        skeleton = self._render(0.0)
        skeleton["output"] = None
        skeleton["error"] = _clip(self.error, 200) if self.error else None
        return skeleton

    async def report(self) -> None:
        """Tell the chat where the command stands (a no-op outside a chat turn).

        Never raises, like ``emit_tool_event`` itself: the card is display
        data, and the funnel also reports from its ``except`` path - a card
        that could not be rendered must never replace the command's outcome.
        """
        if not tool_event_sink_bound():
            return
        try:
            record = self.record()
        except Exception:
            logger.warning("machine activity card for command %s not rendered", self.command_id, exc_info=True)
            return
        event = RUNNER_COMMAND_STARTED_EVENT if self.status in LIVE_STATUSES else RUNNER_COMMAND_FINISHED_EVENT
        await emit_tool_event(event, command=record)


# ── Chat-side fold + persistence ────────────────────────────────────────────

_RECORD_STRING_KEYS = ("runner_id", "runner_name", "platform", "started_at", "error")


def sanitize_record(raw: object) -> dict[str, Any] | None:
    """A record as the chat stores and serves it, or ``None`` when malformed.

    Defensive on both ends: a frame is data from a tool, a persisted row may be
    older than this code. Unknown keys are dropped; the nested ``input`` /
    ``output`` objects are passed through as the builder shaped them.
    """
    if not isinstance(raw, dict):
        return None
    command_id = raw.get("command_id")
    kind = raw.get("kind")
    status = raw.get("status")
    if not (isinstance(command_id, str) and command_id) or kind not in ACTIVITY_KINDS:
        return None
    if status not in RECORD_STATUSES:
        return None
    record: dict[str, Any] = {"command_id": command_id, "kind": kind, "status": status}
    for key in _RECORD_STRING_KEYS:
        value = raw.get(key)
        record[key] = value if isinstance(value, str) else None
    record["local"] = raw.get("local") is True
    record["duration_ms"] = _int_or_none(raw.get("duration_ms"))
    record["input"] = raw.get("input") if isinstance(raw.get("input"), dict) else {}
    record["output"] = raw.get("output") if isinstance(raw.get("output"), dict) else None
    record["stopped_by"] = _stopped_by(raw) if status == STATUS_STOPPED else None
    return record


def sanitize_activity_list(raw: object) -> list[dict[str, Any]] | None:
    """The persisted ``runner_activity`` list, malformed entries dropped."""
    if not isinstance(raw, list) or not raw:
        return None
    records = [r for r in (sanitize_record(item) for item in raw) if r is not None]
    return records[-MAX_RUNNER_ACTIVITY_RECORDS:] or None


#: The wrapper a consulted agent's frames arrive in, once per consult level
#: (``agent_chat``'s consult relay: ``{"status": ..., "inner": <frame>}``).
CONSULT_PROGRESS_STATUS = "consult_progress"
#: Wrappers unwrapped before a frame is ignored - far past any handover chain.
MAX_CONSULT_DEPTH = 8


def unwrap_consult_frame(frame: dict[str, Any]) -> dict[str, Any]:
    """The frame a consulted agent's loop emitted, however deep the consult."""
    for _ in range(MAX_CONSULT_DEPTH):
        inner = frame.get("inner")
        if frame.get("status") != CONSULT_PROGRESS_STATUS or not isinstance(inner, dict):
            break
        frame = inner
    return frame


class RunnerActivityLog:
    """The machine activity of one chat turn (or turn segment), in first-report order."""

    def __init__(self) -> None:
        self._records: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def observe(self, frame: dict[str, Any]) -> bool:
        """Fold one status frame in; ``True`` when it was a machine-activity frame.

        A command a CONSULTED agent ran is part of this turn too: its frame is
        read through the consult wrappers.
        """
        frame = unwrap_consult_frame(frame)
        if frame.get("status") != TOOL_PROGRESS_STATUS or frame.get("event") not in RUNNER_ACTIVITY_EVENTS:
            return False
        record = sanitize_record(frame.get("command"))
        if record is None:
            return True
        # An upsert keeps the card where it first appeared.
        self._records[record["command_id"]] = record
        while len(self._records) > MAX_RUNNER_ACTIVITY_RECORDS:
            self._records.popitem(last=False)
        return True

    def snapshot(self) -> list[dict[str, Any]]:
        """The records to persist, the live ones closed as ``interrupted``.

        The chat persists at a segment boundary - before the round's tool calls
        run - and at the final answer - after the last one returned: no command
        of the turn is in flight at either. A record still running or awaiting
        approval there never reported back (the turn was stopped, the tool was
        cancelled or ran out of time), and its card must not spin on a reload.
        """
        return [
            {**record, "status": STATUS_INTERRUPTED} if record["status"] in LIVE_STATUSES else record
            for record in self._records.values()
        ]

    def reset(self) -> None:
        self._records.clear()

    def __len__(self) -> int:
        return len(self._records)
