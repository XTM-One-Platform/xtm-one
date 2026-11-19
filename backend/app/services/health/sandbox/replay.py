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

"""Replay of recorded tool calls in a sandbox run (#8255, decision B5).

A call whose tool and arguments match a call recorded in the source traces gets
the recorded output: no external call, and the same answer on every run.

Matching (PRD section 13): the arguments are compared **exactly** first (keys
in any order, the same values), then **normalized**: surrounding and repeated
whitespace collapsed, empty values dropped, and dates and timestamps masked,
since "since 2026-11-03T08:00" in a recording and "since 2026-11-04T08:00" in a
replay ask the same question a day apart. Identifiers are never masked: another
id is another object, and answering it with a recording of the first would be
an invented result.

A key recorded several times answers in recorded order (the second identical
poll gets the second recorded answer) and then keeps the last one, so a replay
is reproducible call for call.

Only what a person really ran is a recording: spans a sandbox run or a dry-run
answered (``sandbox_outcome`` on the span) and traces of sandbox copies are
never indexed, so a simulated output is never replayed as a recording.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: Span metadata key a sandbox run or a dry-run stamps on the calls it answered.
SANDBOX_OUTCOME_KEY = "sandbox_outcome"

#: The most tool calls one index holds: a run replays from a bounded set.
MAX_RECORDED_CALLS = 2000

_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?\b"
)
_SPACES_RE = re.compile(r"\s+")
#: What a date or a timestamp becomes in a normalized key: a control character,
#: so no argument a caller writes can collide with it.
_TIME_MASK = "\x00time"


@dataclass(frozen=True)
class RecordedCall:
    """One tool call as a source trace recorded it."""

    tool: str
    arguments: dict[str, Any]
    output: str
    is_error: bool = False
    #: The trace it was recorded in (the flow run, for a flow step).
    trace_id: str | None = None
    agent_id: str | None = None
    user_id: str | None = None
    #: Set for a flow step: its consent is the flow's (level 1).
    flow_id: str | None = None


def _canonical(value: Any, *, normalized: bool) -> Any:
    if isinstance(value, dict):
        items = {str(k): _canonical(v, normalized=normalized) for k, v in value.items()}
        if normalized:
            items = {k: v for k, v in items.items() if v not in (None, "", [], {})}
        return dict(sorted(items.items()))
    if isinstance(value, (list, tuple)):
        return [_canonical(v, normalized=normalized) for v in value]
    if isinstance(value, str) and normalized:
        return _TIMESTAMP_RE.sub(_TIME_MASK, _SPACES_RE.sub(" ", value).strip())
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def arguments_key(tool: str, arguments: Any, *, normalized: bool = False) -> str:
    """The key a call is matched on: the tool and its canonical arguments."""
    args = arguments if isinstance(arguments, dict) else {}
    return json.dumps([tool, _canonical(args, normalized=normalized)], sort_keys=True, default=str)


@dataclass
class ReplayIndex:
    """The recorded calls of a run's source, looked up by tool and arguments."""

    calls: list[RecordedCall] = field(default_factory=list)
    _exact: dict[str, list[RecordedCall]] = field(default_factory=dict, init=False, repr=False)
    _normalized: dict[str, list[RecordedCall]] = field(default_factory=dict, init=False, repr=False)
    _cursor: dict[str, int] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.calls = list(self.calls)[:MAX_RECORDED_CALLS]
        for call in self.calls:
            self._exact.setdefault(arguments_key(call.tool, call.arguments), []).append(call)
            self._normalized.setdefault(
                arguments_key(call.tool, call.arguments, normalized=True), []
            ).append(call)

    def __len__(self) -> int:
        return len(self.calls)

    def candidates(self, tool: str, arguments: Any) -> tuple[str, list[RecordedCall]]:
        """The key and the recordings a call matches, exact first; ``("", [])`` for none."""
        for normalized, table in ((False, self._exact), (True, self._normalized)):
            key = ("n:" if normalized else "e:") + arguments_key(tool, arguments, normalized=normalized)
            found = table.get(key[2:])
            if found:
                return key, found
        return "", []

    def take(self, key: str, recordings: list[RecordedCall]) -> RecordedCall:
        """The recording that answers this occurrence of *key*, in recorded order."""
        position = self._cursor.get(key, 0)
        self._cursor[key] = position + 1
        return recordings[min(position, len(recordings) - 1)]

    def outputs_of(self, tool: str, *, limit: int = 3) -> list[RecordedCall]:
        """Up to *limit* successful recordings of *tool*, most recent last."""
        return [c for c in self.calls if c.tool == tool and not c.is_error][-limit:]


def _span_tool_name(span: Any) -> str:
    meta = span.metadata_json if isinstance(span.metadata_json, dict) else {}
    name = meta.get("tool_name") or span.name or ""
    return name[5:] if name.startswith("tool:") else name


def _span_arguments(span: Any) -> dict[str, Any]:
    if isinstance(span.tool_arguments, dict):
        return span.tool_arguments
    raw = span.input.get("arguments") if isinstance(span.input, dict) else None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return raw if isinstance(raw, dict) else {}


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


async def load_recorded_calls(
    db: AsyncSession,
    trace_ids: Iterable[str | uuid.UUID],
    *,
    excluded_agent_ids: Iterable[uuid.UUID] = (),
    only_agent_id: uuid.UUID | None = None,
) -> list[RecordedCall]:
    """The tool calls recorded in *trace_ids*, in trace then span order.

    Offloaded outputs are read back from object storage. Spans a sandbox or a
    dry-run answered, and traces of *excluded_agent_ids* (the sandbox copies),
    are left out; with *only_agent_id*, every trace of another agent too (a
    copy replays its own source's recordings only). Consent is not decided here: the session checks it on the
    recording it is about to use, at that moment.
    """
    from app.models.llm_trace import LLMSpan, LLMTrace
    from app.services.tracing.payload_offload import resolve_payload

    ids: list[uuid.UUID] = []
    for value in trace_ids:
        try:
            ids.append(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))
        except (TypeError, ValueError):
            continue
    if not ids:
        return []
    order = {tid: i for i, tid in enumerate(ids)}
    excluded = set(excluded_agent_ids)
    rows = (
        await db.execute(
            select(LLMSpan, LLMTrace.agent_id, LLMTrace.user_id)
            .join(LLMTrace, LLMTrace.id == LLMSpan.trace_id)
            .where(
                LLMSpan.trace_id.in_(ids),
                LLMSpan.type == "TOOL",
                *([LLMTrace.agent_id == only_agent_id] if only_agent_id is not None else []),
            )
        )
    ).all()
    rows.sort(key=lambda r: (order.get(r[0].trace_id, len(order)), r[0].position))
    calls: list[RecordedCall] = []
    for span, agent_id, user_id in rows:
        meta = span.metadata_json if isinstance(span.metadata_json, dict) else {}
        if meta.get(SANDBOX_OUTCOME_KEY) or (agent_id is not None and agent_id in excluded):
            continue
        is_error = span.level == "ERROR"
        try:
            output = _as_text(await resolve_payload(span.output)) if not is_error else (span.status_message or "")
        except Exception:
            logger.warning("Replay: the output of span %s could not be read", span.id)
            continue
        calls.append(
            RecordedCall(
                tool=_span_tool_name(span),
                arguments=_span_arguments(span),
                output=output,
                is_error=is_error,
                trace_id=str(span.trace_id),
                agent_id=str(agent_id) if agent_id else None,
                user_id=str(user_id) if user_id else None,
            )
        )
        if len(calls) >= MAX_RECORDED_CALLS:
            break
    return calls
