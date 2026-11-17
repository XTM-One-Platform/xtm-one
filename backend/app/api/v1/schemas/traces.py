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

"""Pydantic schemas for observability trace contracts."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class LLMTrace(BaseModel):
    id: str
    agent_id: str | None = None
    user_id: str | None = None
    conversation_id: str | None = None
    assignment_run_id: str | None = None
    parent_trace_id: str | None = None
    source: str
    name: str
    model: str | None = None
    environment: str
    metadata: dict[str, Any] | None = None
    input: Any = None
    output: Any = None
    # See ``LLMSpan.payload_meta``: names the payload fields this response had
    # to bound, with their real size and whether they live in object storage.
    payload_meta: dict[str, dict[str, Any]] | None = None
    tags: list[str] | None = None
    started_at: str
    ended_at: str | None = None
    duration_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    input_cost: float | None = None
    output_cost: float | None = None
    total_cost: float | None = None
    status: str
    error_message: str | None = None
    # Structured failure attribution. NULL on a successful trace, and
    # also on a failed one recorded before the classifier existed — the UI
    # distinguishes "no cause recorded" from the explicit ``unknown`` bucket,
    # because the two mean different things (never classified vs. classified
    # and not attributable).
    failure_cause: str | None = None
    level: str
    created_at: str
    updated_at: str

    model_config = {"from_attributes": True, "protected_namespaces": ()}


class LLMSpan(BaseModel):
    id: str
    trace_id: str
    parent_span_id: str | None = None
    type: str
    name: str
    model: str | None = None
    model_parameters: dict[str, Any] | None = None
    input: Any = None
    output: Any = None
    metadata: dict[str, Any] | None = None
    # Per-field transport metadata, present only for payloads the response had
    # to bound: ``{"output": {"truncated": true, "offloaded": false,
    # "size_bytes": 1234}}``.  ``None`` when every payload is complete as sent.
    # Fetch the full value from the span payload endpoint.
    payload_meta: dict[str, dict[str, Any]] | None = None
    level: str
    status_message: str | None = None
    started_at: str
    ended_at: str | None = None
    duration_ms: float | None = None
    time_to_first_token: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    input_cost: float | None = None
    output_cost: float | None = None
    total_cost: float | None = None
    usage_details: dict[str, Any] | None = None
    tool_call_id: str | None = None
    tool_arguments: dict[str, Any] | None = None
    stop_reason: str | None = None
    position: int
    created_at: str

    model_config = {"from_attributes": True, "protected_namespaces": ()}


class LLMScore(BaseModel):
    id: str
    trace_id: str
    span_id: str | None = None
    user_id: str | None = None
    name: str
    source: str
    data_type: str
    value: float | None = None
    string_value: str | None = None
    comment: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: str

    model_config = {"from_attributes": True}
