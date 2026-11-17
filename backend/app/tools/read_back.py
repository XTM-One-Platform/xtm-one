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

"""Post-write read-back with a consistency block.

After a write tool that declares :class:`~app.tools.base.WriteReadBack`
succeeds, the agentic loop appends to its result a compact ``read_back`` of
the object as the system now shows it, and a ``consistency`` block listing
what looks wrong across the run:

- ``same_object``: the object was written N times in this run;
- ``burst_send``: N sends to the same recipients within two minutes;
- ``near_duplicate``: a message more than 90% identical to an earlier one;
- ``overlap``: an invitation overlapping another event of the organizer.

Which tool is a write, which read tool shows its object and which arguments
carry the recipients and the body are declared on each tool definition, never
here (invariant 18). Every read the harness makes is bounded by
:data:`READ_BACK_TIMEOUT_S` and fails open: a slow or failing read leaves the
write's result as it was, with a line saying so.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any

from app.tools.base import ToolCall, ToolResult, WriteReadBack

#: The whole read-back of one write (the object's read and the overlap list)
#: stays under this budget.
READ_BACK_TIMEOUT_S = 1.5
#: Sends to the same recipients closer than this are a burst.
BURST_WINDOW_S = 120.0
#: Two message texts at least this similar are a near duplicate.
NEAR_DUPLICATE_RATIO = 0.9
#: The read-back block never grows past this many characters.
MAX_BLOCK_CHARS = 1200

ExecuteRead = Callable[[str, dict[str, Any]], Awaitable[ToolResult | None]]


@dataclass
class WriteRecord:
    """One successful write of the run, as the consistency check sees it."""

    kind: str
    object_id: str
    recipients: frozenset[str] = frozenset()
    text: str = ""
    at: float = field(default_factory=time.time)


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ",".join(_as_text(v) for v in value)
    return str(value)


def recipients_of(call: ToolCall, spec: WriteReadBack) -> frozenset[str]:
    """The recipient set the call names: declared arguments split on commas, lower-cased."""
    found: set[str] = set()
    for name in spec.recipients:
        for part in _as_text((call.arguments or {}).get(name)).split(","):
            part = part.strip().lower()
            if part:
                found.add(part)
    return frozenset(found)


def body_of(call: ToolCall, spec: WriteReadBack) -> str:
    """The declared body arguments of the call, joined."""
    return "\n".join(
        _as_text((call.arguments or {}).get(name)) for name in spec.body if (call.arguments or {}).get(name)
    )


def body_hash(text: str) -> str:
    """A short, stable hash of a body (12 hex characters of SHA-256)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def object_id_of(call: ToolCall, result: ToolResult, spec: WriteReadBack) -> str:
    """The written object's id: the call's identity argument, else the id the result names."""
    if spec.identity:
        value = _as_text((call.arguments or {}).get(spec.identity[0])).strip()
        if value:
            return value
    if spec.id_pattern:
        match = re.search(spec.id_pattern, result.content or "")
        if match:
            return match.group(1).strip()
    return ""


class ReadBackLedger:
    """The run's successful writes, and the anomalies a new one adds to them."""

    def __init__(self) -> None:
        self._writes: list[WriteRecord] = []

    def note(self, record: WriteRecord) -> list[str]:
        """Record *record* and return the anomalies it raises, in a stable order."""
        anomalies: list[str] = []
        if record.object_id:
            same = 1 + sum(
                1 for w in self._writes if w.kind == record.kind and w.object_id == record.object_id
            )
            if same > 1:
                anomalies.append(f"same_object: {record.kind} {record.object_id} written {same} times in this run")
        if record.recipients:
            burst = 1 + sum(
                1
                for w in self._writes
                if w.recipients == record.recipients and record.at - w.at <= BURST_WINDOW_S
            )
            if burst > 1:
                who = ", ".join(sorted(record.recipients))
                anomalies.append(f"burst_send: {burst} sends to {who} within {int(BURST_WINDOW_S)} s")
        if record.text:
            for w in self._writes:
                if w.kind != record.kind or not w.text:
                    continue
                ratio = SequenceMatcher(None, w.text, record.text).ratio()
                if ratio > NEAR_DUPLICATE_RATIO:
                    anomalies.append(
                        f"near_duplicate: this {record.kind} is {int(ratio * 100)}% identical to "
                        f"{w.object_id or 'an earlier one'} posted earlier in this run"
                    )
                    break
        self._writes.append(record)
        return anomalies


def _labelled(content: str, labels: Iterable[str]) -> list[str]:
    """The ``Label: value`` lines of *content* for *labels*, with their indented continuation."""
    wanted = tuple(labels)
    out: list[str] = []
    lines = (content or "").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        head = line.split(":", 1)[0].strip()
        base = head.split(" (", 1)[0]
        if ":" in line and base in wanted and not line.startswith((" ", "\t")):
            out.append(line.strip())
            i += 1
            while i < len(lines) and lines[i].startswith(("  ", "\t")) and lines[i].strip():
                out.append("  " + lines[i].strip())
                i += 1
            continue
        i += 1
    return out


def _section(content: str, label: str) -> str:
    """The text after a ``Label:`` line up to the next blank line followed by a label."""
    if not label:
        return ""
    lines = (content or "").splitlines()
    for i, line in enumerate(lines):
        if line.strip() == f"{label}:":
            body: list[str] = []
            for nxt in lines[i + 1 :]:
                if re.match(r"^[A-Z][\w ]{0,40}:( |$)", nxt) and body:
                    break
                body.append(nxt)
            return "\n".join(body).strip()
    return ""


def _parse_when(value: str) -> datetime | None:
    match = re.search(r"\d{4}-\d{2}-\d{2}(T\d{2}:\d{2}(:\d{2})?)?", value or "")
    if not match:
        return None
    try:
        return datetime.fromisoformat(match.group(0))
    except ValueError:
        return None


def _event_span(event: Any) -> tuple[datetime | None, datetime | None]:
    def _point(raw: Any) -> datetime | None:
        if isinstance(raw, dict):
            raw = raw.get("dateTime") or raw.get("date") or ""
        return _parse_when(_as_text(raw))

    if not isinstance(event, dict):
        return None, None
    return _point(event.get("start")), _point(event.get("end"))


def overlapping(events: Any, object_id: str, start: datetime, end: datetime) -> list[str]:
    """The events of *events* (a list tool's ``structured``) overlapping start..end, by subject."""
    found: list[str] = []
    for event in events if isinstance(events, list) else []:
        if not isinstance(event, dict) or event.get("id") == object_id:
            continue
        if event.get("isCancelled") or str(event.get("showAs") or "").lower() == "free":
            continue
        s, e = _event_span(event)
        if s is None or e is None:
            continue
        if s < end and start < e:
            found.append(_as_text(event.get("subject") or event.get("summary") or event.get("id")))
    return found


async def _read_object(
    spec: WriteReadBack, object_id: str, call: ToolCall, execute: ExecuteRead
) -> tuple[list[str], str, str]:
    """Read the object with the declared read tool: its kept lines, its body, its raw text."""
    if not spec.read_tool or not object_id:
        return [], "", ""
    arguments: dict[str, Any] = {spec.read_id_argument or spec.identity[0]: object_id}
    for name in spec.identity[1:]:
        value = (call.arguments or {}).get(name)
        if value not in (None, ""):
            arguments[name] = value
    read = await execute(spec.read_tool, arguments)
    if read is None or read.is_error:
        raise RuntimeError((read.content if read else "read tool unavailable")[:200])
    content = read.content or ""
    return _labelled(content, spec.fields), _section(content, spec.body_label), content


async def build(
    spec: WriteReadBack,
    call: ToolCall,
    result: ToolResult,
    ledger: ReadBackLedger,
    execute: ExecuteRead,
    *,
    timeout: float = READ_BACK_TIMEOUT_S,
) -> str:
    """The ``read_back`` and ``consistency`` blocks to append to a successful write's result."""
    object_id = object_id_of(call, result, spec)
    lines: list[str] = [f"kind: {spec.kind}"]
    if object_id:
        lines.append(f"id: {object_id}")
    overlap_with: list[str] = []
    started = time.monotonic()
    try:
        kept, body, raw = await asyncio.wait_for(_read_object(spec, object_id, call, execute), timeout)
        lines.extend(kept)
        if body:
            lines.append(f"body_sha256: {body_hash(body)}")
        if spec.overlap_tool and raw and _labelled(raw, ("Attendees",)):
            start_line = next((x for x in kept if x.startswith("Start:")), "")
            end_line = next((x for x in kept if x.startswith("End:")), "")
            start, end = _parse_when(start_line), _parse_when(end_line)
            remaining = timeout - (time.monotonic() - started)
            if start and end and remaining > 0:
                listed = await asyncio.wait_for(
                    execute(
                        spec.overlap_tool,
                        {spec.overlap_window[0]: start.isoformat(), spec.overlap_window[1]: end.isoformat()},
                    ),
                    remaining,
                )
                if listed is not None and not listed.is_error:
                    overlap_with = overlapping(listed.structured, object_id, start, end)
    except TimeoutError:
        lines.append(f"(the read-back took over {timeout:g} s and was skipped)")
    except Exception as exc:  # fail open: the write's own result stands
        lines.append(f"(the read-back failed: {str(exc)[:160]})")
    recipients = recipients_of(call, spec)
    if recipients:
        lines.append("recipients: " + ", ".join(sorted(recipients)))
    for name in spec.shown_arguments:
        value = _as_text((call.arguments or {}).get(name)).strip()
        if value:
            lines.append(f"{name}: {value[:200]}")
    text = body_of(call, spec)
    if text and not spec.read_tool:
        lines.append(f"body_sha256: {body_hash(text)}")
    anomalies = ledger.note(WriteRecord(spec.kind, object_id, recipients, text if spec.recipients else ""))
    if overlap_with:
        anomalies.append("overlap: the invitation overlaps " + "; ".join(overlap_with[:5]))
    block = "\n\nread_back:\n" + "\n".join(f"  {x}" for x in lines)
    block += "\nconsistency:\n" + (
        "\n".join(f"  - {a}" for a in anomalies) if anomalies else "  - none"
    )
    if len(block) > MAX_BLOCK_CHARS:
        block = block[: MAX_BLOCK_CHARS - 20] + "\n  [read_back cut]"
    return block
