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

"""Tracing sink contracts and payload dataclasses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID


@dataclass(slots=True)
class SpanData:
    id: UUID
    trace_id: UUID
    type: str
    name: str
    position: int
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: float | None = None
    parent_span_id: UUID | None = None
    model: str | None = None
    #: Milliseconds from a streamed generation's start to its first output.
    time_to_first_token: float | None = None
    model_parameters: dict[str, Any] | None = None
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] | None = None
    level: str = "DEFAULT"
    status_message: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    usage_details: dict[str, Any] | None = None
    tool_call_id: str | None = None
    tool_arguments: Any = None
    stop_reason: str | None = None


@dataclass(slots=True)
class TraceData:
    id: UUID
    source: str
    name: str
    started_at: datetime
    ended_at: datetime | None = None
    duration_ms: float | None = None
    status: str = "ok"
    user_id: UUID | None = None
    agent_id: UUID | None = None
    conversation_id: UUID | None = None
    assignment_run_id: UUID | None = None
    parent_trace_id: UUID | None = None
    model: str | None = None
    environment: str = "production"
    metadata: dict[str, Any] | None = None
    input: Any = None
    output: Any = None
    tags: list[str] | None = None
    level: str = "DEFAULT"
    error_message: str | None = None
    #: Structured failure attribution — the enum value from
    #: ``services/failure_causes``. ``None`` on a successful trace, and also on
    #: a failed one whose site has not been wired to the classifier yet, which
    #: is why the read path distinguishes "no cause" from ``unknown``.
    failure_cause: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    usage_details: dict[str, Any] | None = None
    spans: list[SpanData] = field(default_factory=list)


class TracingBackend(Protocol):
    async def flush_trace(self, trace: TraceData, spans: list[SpanData]) -> None:
        pass

    async def flush_score(self, score: dict[str, Any]) -> None:
        pass

    async def close(self) -> None:
        pass
