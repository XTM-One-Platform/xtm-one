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

"""ClickHouse tracing sink and bulk import helpers."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID, uuid4

from app.core.config import settings
from app.core.utils import format_exception_message
from app.services.trace_switches import apply_trace_switches, trace_switches
from app.services.tracing.base import SpanData, TraceData
from app.services.tracing.clickhouse_client import (
    HTTPClickHouseClient,
    has_clickhouse_runtime_config,
)
from app.services.tracing.cost_calculator import calculate_costs

logger = logging.getLogger(__name__)


class ClickHouseClient(Protocol):
    async def insert_json_rows(self, table: str, rows: list[dict[str, Any]]) -> None: ...

    async def close(self) -> None: ...


class _NoopClickHouseClient:
    """Fallback client used when no ClickHouse connector is configured."""

    async def insert_json_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        raise RuntimeError(
            "ClickHouse client is not configured. "
            "Provide clickhouse_client_factory when tracing_store_provider=clickhouse."
        )

    async def close(self) -> None:
        return None


def _as_uuid_str(value: UUID | None) -> str | None:
    return str(value) if isinstance(value, UUID) else None


def _decimal_or_none(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _token_int(value: int | None) -> int | None:
    return int(value) if isinstance(value, int) else None


def _coalesce_cost(precomputed: Any, calculated: Decimal | None) -> float | None:
    """Prefer an upstream-computed cost (e.g. a Postgres source row's Decimal
    column during backfill) over the local null-price calculation, as a float."""
    if precomputed is not None:
        try:
            return float(precomputed)
        except (TypeError, ValueError):
            return None
    return _decimal_or_none(calculated)


def _coerce_uuid(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, str):
        try:
            return str(UUID(value))
        except ValueError:
            return None
    return None


class ClickHouseSink:
    """Flush tracing payloads to ClickHouse-compatible tables."""

    def __init__(
        self,
        *,
        client_factory: Callable[[], Awaitable[ClickHouseClient]] | None = None,
    ) -> None:
        self._client_factory = client_factory or self._build_default_client
        self._client: ClickHouseClient | None = None
        self.tracing_errors = 0

    async def _build_default_client(self) -> ClickHouseClient:
        _ = settings.clickhouse_database
        if not has_clickhouse_runtime_config():
            return _NoopClickHouseClient()
        return HTTPClickHouseClient.from_settings()

    async def _client_or_create(self) -> ClickHouseClient:
        if self._client is None:
            self._client = await self._client_factory()
        return self._client

    async def flush_trace(self, trace: TraceData, spans: list[SpanData]) -> None:
        switched = apply_trace_switches(trace, spans)
        if switched is None:
            return
        trace_rows, span_rows = self._build_trace_rows([switched[0]], [switched[1]])
        await self._insert_rows(trace_rows=trace_rows, span_rows=span_rows, score_rows=[])

    async def flush_score(self, score: dict[str, Any]) -> None:
        if not trace_switches().storage_enabled:
            return
        await self.write_score(score)

    async def write_score(self, score: dict[str, Any]) -> None:
        """Insert one score payload, whatever the trace storage switch says.

        The counterpart of ``local_sink.write_score_row``: the one-time rewrite
        of the chat scores an older writer stored copies what PostgreSQL already
        holds, so it writes here even while no new score is stored.
        """
        trace_id = score.get("trace_id")
        if trace_id is None:
            return
        try:
            trace_uuid = UUID(str(trace_id))
        except (TypeError, ValueError):
            return
        score_rows = [self._score_row_from_payload(score, trace_id=trace_uuid)]
        await self._insert_rows(trace_rows=[], span_rows=[], score_rows=score_rows)

    async def bulk_import(
        self,
        *,
        traces: Sequence[Any],
        spans_by_trace: dict[UUID, list[Any]],
        scores_by_trace: dict[UUID, list[Any]],
    ) -> None:
        trace_rows, span_rows = self._build_trace_rows(
            traces,
            [spans_by_trace.get(t.id, []) for t in traces],
        )
        score_rows: list[dict[str, Any]] = []
        for trace in traces:
            for score in scores_by_trace.get(trace.id, []):
                score_rows.append(
                    {
                        "id": str(score.id),
                        "trace_id": str(score.trace_id),
                        "span_id": _as_uuid_str(score.span_id),
                        "user_id": _as_uuid_str(score.user_id),
                        "name": score.name,
                        "source": score.source,
                        "data_type": score.data_type,
                        "value": score.value,
                        "string_value": score.string_value,
                        "comment": score.comment,
                        "metadata": score.metadata_json or {},
                        "created_at": score.created_at,
                    }
                )
        await self._insert_rows(trace_rows=trace_rows, span_rows=span_rows, score_rows=score_rows)

    def _build_trace_rows(
        self,
        traces: Sequence[Any],
        spans_by_trace_in_order: Sequence[Sequence[Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        trace_rows: list[dict[str, Any]] = []
        span_rows: list[dict[str, Any]] = []
        for trace, spans in zip(traces, spans_by_trace_in_order, strict=False):
            usage_details = getattr(trace, "usage_details", None)
            costs = calculate_costs(usage_details, None)
            # Prefer costs already computed upstream. Backfill source rows come
            # from Postgres and carry input/output/total_cost; recomputing here
            # with a null price would DISCARD them and write NULLs, diverging from
            # Postgres. Fall back to the (null-price) calculation only when the
            # source carries no cost.
            trace_input_cost = _coalesce_cost(getattr(trace, "input_cost", None), costs.get("input_cost"))
            trace_output_cost = _coalesce_cost(getattr(trace, "output_cost", None), costs.get("output_cost"))
            trace_total_cost = _coalesce_cost(getattr(trace, "total_cost", None), costs.get("total_cost"))
            trace_rows.append(
                {
                    "id": str(trace.id),
                    "agent_id": _as_uuid_str(getattr(trace, "agent_id", None)),
                    "user_id": _as_uuid_str(getattr(trace, "user_id", None)),
                    "conversation_id": _as_uuid_str(getattr(trace, "conversation_id", None)),
                    "assignment_run_id": _as_uuid_str(getattr(trace, "assignment_run_id", None)),
                    "parent_trace_id": _as_uuid_str(getattr(trace, "parent_trace_id", None)),
                    "source": trace.source,
                    "name": trace.name,
                    "model": trace.model,
                    "environment": trace.environment,
                    "metadata": (
                        getattr(trace, "metadata_json", None)
                        or getattr(trace, "metadata", None)
                        or {}
                    ),
                    "input": trace.input,
                    "output": trace.output,
                    "tags": trace.tags or [],
                    "started_at": trace.started_at,
                    "ended_at": trace.ended_at,
                    "duration_ms": trace.duration_ms,
                    "input_tokens": _token_int(trace.input_tokens),
                    "output_tokens": _token_int(trace.output_tokens),
                    "total_tokens": _token_int(trace.total_tokens),
                    "input_cost": trace_input_cost,
                    "output_cost": trace_output_cost,
                    "total_cost": trace_total_cost,
                    "status": trace.status,
                    "error_message": trace.error_message,
                    "failure_cause": getattr(trace, "failure_cause", None),
                    "level": trace.level,
                    "created_at": getattr(trace, "created_at", None) or datetime.now(UTC),
                    "updated_at": getattr(trace, "updated_at", None) or datetime.now(UTC),
                }
            )
            for span in spans:
                span_costs = calculate_costs(getattr(span, "usage_details", None), None)
                span_input_cost = _coalesce_cost(getattr(span, "input_cost", None), span_costs.get("input_cost"))
                span_output_cost = _coalesce_cost(getattr(span, "output_cost", None), span_costs.get("output_cost"))
                span_total_cost = _coalesce_cost(getattr(span, "total_cost", None), span_costs.get("total_cost"))
                span_rows.append(
                    {
                        "id": str(span.id),
                        "trace_id": str(span.trace_id),
                        "parent_span_id": _as_uuid_str(getattr(span, "parent_span_id", None)),
                        "type": span.type,
                        "name": span.name,
                        "model": span.model,
                        "model_parameters": span.model_parameters,
                        "input": span.input,
                        "output": span.output,
                        "metadata": (
                            getattr(span, "metadata_json", None)
                            or getattr(span, "metadata", None)
                            or {}
                        ),
                        "level": span.level,
                        "status_message": span.status_message,
                        "started_at": span.started_at,
                        "ended_at": span.ended_at,
                        "duration_ms": span.duration_ms,
                        "time_to_first_token": getattr(span, "time_to_first_token", None),
                        "input_tokens": _token_int(span.input_tokens),
                        "output_tokens": _token_int(span.output_tokens),
                        "input_cost": span_input_cost,
                        "output_cost": span_output_cost,
                        "total_cost": span_total_cost,
                        "usage_details": span.usage_details,
                        "tool_call_id": span.tool_call_id,
                        "tool_arguments": span.tool_arguments,
                        "stop_reason": span.stop_reason,
                        "position": int(span.position),
                        "created_at": getattr(span, "created_at", None) or datetime.now(UTC),
                    }
                )
        return trace_rows, span_rows

    def _score_row_from_payload(
        self,
        score: dict[str, Any],
        *,
        trace_id: UUID,
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        return {
            "id": str(score.get("id") or uuid4()),
            "trace_id": str(trace_id),
            "span_id": _coerce_uuid(score.get("span_id")),
            "user_id": _coerce_uuid(score.get("user_id")),
            "name": str(score.get("name") or "user_feedback"),
            "source": str(score.get("source") or "USER"),
            "data_type": str(score.get("data_type") or "BOOLEAN"),
            "value": score.get("value"),
            "string_value": (
                score.get("string_value")
                if isinstance(score.get("string_value"), str)
                else None
            ),
            "comment": score.get("comment") if isinstance(score.get("comment"), str) else None,
            "metadata": score.get("metadata") if isinstance(score.get("metadata"), dict) else {},
            "created_at": (
                score.get("created_at")
                if isinstance(score.get("created_at"), datetime)
                else now
            ),
        }

    async def _insert_rows(
        self,
        *,
        trace_rows: list[dict[str, Any]],
        span_rows: list[dict[str, Any]],
        score_rows: list[dict[str, Any]],
    ) -> None:
        if not trace_rows and not span_rows and not score_rows:
            return
        try:
            client = await self._client_or_create()
            if trace_rows:
                await client.insert_json_rows("llm_traces", trace_rows)
            if span_rows:
                await client.insert_json_rows("llm_spans", span_rows)
            if score_rows:
                await client.insert_json_rows("llm_scores", score_rows)
        except Exception as exc:
            self.tracing_errors += 1
            logger.warning(
                "ClickHouseSink write failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )

    async def close(self) -> None:
        if self._client is None:
            return
        await self._client.close()
