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

"""PostgreSQL -> ClickHouse trace backfill service."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.llm_trace import LLMScore, LLMSpan, LLMTrace
from app.services.tracing.clickhouse_sink import ClickHouseSink


@dataclass(slots=True)
class TraceBackfillCursor:
    created_at: datetime
    trace_id: UUID


@dataclass(slots=True)
class TraceBackfillResult:
    imported_traces: int
    imported_spans: int
    imported_scores: int
    cursor: TraceBackfillCursor | None
    exhausted: bool


class TraceBackfillService:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        sink: ClickHouseSink,
        batch_size: int = 500,
    ) -> None:
        self._session_factory = session_factory
        self._sink = sink
        self._batch_size = max(1, int(batch_size))

    async def run(
        self,
        *,
        cursor: TraceBackfillCursor | None = None,
        max_batches: int | None = None,
    ) -> TraceBackfillResult:
        imported_traces = 0
        imported_spans = 0
        imported_scores = 0
        current_cursor = cursor
        batches = 0
        exhausted = False

        async with self._session_factory() as session:
            while True:
                if max_batches is not None and batches >= max_batches:
                    break
                traces = await self._load_trace_batch(session, cursor=current_cursor)
                if not traces:
                    exhausted = True
                    break
                trace_ids = [trace.id for trace in traces]
                span_rows = await self._load_spans(session, trace_ids)
                score_rows = await self._load_scores(session, trace_ids)

                spans_by_trace: dict[UUID, list[LLMSpan]] = {trace_id: [] for trace_id in trace_ids}
                for span in span_rows:
                    spans_by_trace.setdefault(span.trace_id, []).append(span)
                scores_by_trace: dict[UUID, list[LLMScore]] = {
                    trace_id: [] for trace_id in trace_ids
                }
                for score in score_rows:
                    scores_by_trace.setdefault(score.trace_id, []).append(score)

                await self._sink.bulk_import(
                    traces=traces,
                    spans_by_trace=spans_by_trace,
                    scores_by_trace=scores_by_trace,
                )

                imported_traces += len(traces)
                imported_spans += len(span_rows)
                imported_scores += len(score_rows)
                batches += 1
                last_trace = traces[-1]
                current_cursor = TraceBackfillCursor(
                    created_at=last_trace.created_at,
                    trace_id=last_trace.id,
                )

        return TraceBackfillResult(
            imported_traces=imported_traces,
            imported_spans=imported_spans,
            imported_scores=imported_scores,
            cursor=current_cursor,
            exhausted=exhausted,
        )

    async def _load_trace_batch(
        self,
        session: AsyncSession,
        *,
        cursor: TraceBackfillCursor | None,
    ) -> list[LLMTrace]:
        query = select(LLMTrace)
        if cursor is not None:
            query = query.where(
                or_(
                    LLMTrace.created_at > cursor.created_at,
                    and_(
                        LLMTrace.created_at == cursor.created_at,
                        LLMTrace.id > cursor.trace_id,
                    ),
                )
            )
        query = query.order_by(LLMTrace.created_at.asc(), LLMTrace.id.asc()).limit(self._batch_size)
        result = await session.execute(query)
        return list(result.scalars().all())

    async def _load_spans(self, session: AsyncSession, trace_ids: list[UUID]) -> list[LLMSpan]:
        if not trace_ids:
            return []
        result = await session.execute(
            select(LLMSpan)
            .where(LLMSpan.trace_id.in_(trace_ids))
            .order_by(LLMSpan.trace_id.asc(), LLMSpan.position.asc(), LLMSpan.started_at.asc())
        )
        return list(result.scalars().all())

    async def _load_scores(self, session: AsyncSession, trace_ids: list[UUID]) -> list[LLMScore]:
        if not trace_ids:
            return []
        result = await session.execute(
            select(LLMScore)
            .where(LLMScore.trace_id.in_(trace_ids))
            .order_by(LLMScore.trace_id.asc(), LLMScore.created_at.asc())
        )
        return list(result.scalars().all())
