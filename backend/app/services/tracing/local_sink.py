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

"""Local PostgreSQL tracing sink."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings
from app.core.database import ENGINE_CONNECT_ARGS, json_deserializer, json_serializer
from app.core.utils import format_exception_message, strip_null_bytes
from app.models.llm_trace import LLMScore, LLMSpan, LLMTrace
from app.services.langfuse_tracing import USAGE_PARTS_KEY
from app.services.trace_switches import apply_trace_switches, trace_switches
from app.services.tracing.attribution import TraceRefs, TraceStamp, stamp_traces
from app.services.tracing.base import SpanData, TraceData
from app.services.tracing.cost_calculator import (
    ModelPrice,
    cached_input_tokens,
    calculate_costs,
    resolve_model_price,
    resolve_model_price_any,
)
from app.services.tracing.feedback_scores import FEEDBACK_SCORE_NAME, lock_rater_score

logger = logging.getLogger(__name__)

_TRACING_ENGINE = None
_TRACING_SESSION_FACTORY: async_sessionmaker[AsyncSession] | None = None


def _build_tracing_session_factory() -> async_sessionmaker[AsyncSession]:
    global _TRACING_ENGINE, _TRACING_SESSION_FACTORY
    if _TRACING_SESSION_FACTORY is not None:
        return _TRACING_SESSION_FACTORY

    _TRACING_ENGINE = create_async_engine(
        settings.database_url,
        echo=settings.debug,
        # Same sanitizing serializer as the app engine:
        # the engine-level choke point must cover every ORM JSON write on
        # EVERY engine. The call-site ``_strip_null_bytes`` below stays —
        # it also covers TEXT columns, which no JSON serializer can reach.
        json_serializer=json_serializer,
        json_deserializer=json_deserializer,
        pool_size=settings.tracing_db_pool_size,
        max_overflow=settings.tracing_db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        pool_pre_ping=settings.db_pool_pre_ping,
        # The app engine's connect contract too: a bounded connect,
        # retried while PostgreSQL refuses it, or a database blip
        # drops every trace flushed meanwhile.
        connect_args=ENGINE_CONNECT_ARGS,
    )
    _TRACING_SESSION_FACTORY = async_sessionmaker(
        _TRACING_ENGINE,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    return _TRACING_SESSION_FACTORY


def _as_dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _decimal_or_none(value: Decimal | None) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _trace_provider(trace: TraceData) -> str | None:
    meta = _as_dict(trace.metadata) or {}
    for key in ("provider", "llm_provider", "model_provider"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


_STORED_COST_KEYS = ("input_cost", "output_cost", "total_cost")


async def _price_usage_parts(
    parts: list[dict[str, Any]],
    trace_model: str | None,
    resolve_price: Callable[[str | None], Awaitable[ModelPrice | None]],
) -> dict[str, Decimal | None]:
    """The cost of a trace priced part by part (``usage_details[USAGE_PARTS_KEY]``).

    Each part at the price of the model that served it, as the generations of
    its calls are priced. A part whose model has no price adds nothing, like a
    generation of that model; with no part priced the cost stays unknown.
    """
    costs: dict[str, Decimal | None] = dict.fromkeys(_STORED_COST_KEYS)
    for part in parts:
        if not isinstance(part, dict):
            continue
        model = part.get("model")
        price = await resolve_price(model if isinstance(model, str) and model else trace_model)
        part_costs = calculate_costs(_as_dict(part.get("usage_details")), price)
        for key in _STORED_COST_KEYS:
            value = part_costs.get(key)
            if value is not None:
                current = costs[key]
                costs[key] = value if current is None else current + value
    return costs


def _coerce_uuid(value: UUID | None) -> UUID | None:
    return value if isinstance(value, UUID) else None


def _coerce_uuid_any(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        try:
            return UUID(candidate)
        except ValueError:
            return None
    return None


def _token_int(value: int | None) -> int | None:
    return int(value) if isinstance(value, int) else None


def _cached_input_tokens(usage_details: dict[str, Any] | None) -> int | None:
    """The cached input tokens of a trace's usage, part by part when it carries parts; None without usage."""
    usage = _as_dict(usage_details)
    if usage is None:
        return None
    parts = usage.get(USAGE_PARTS_KEY)
    if isinstance(parts, list) and parts:
        return sum(
            cached_input_tokens(_as_dict(part.get("usage_details")) or _as_dict(part.get("usage")))
            for part in parts
            if isinstance(part, dict)
        )
    return cached_input_tokens(usage)


async def _stamp_trace(session: AsyncSession, trace: TraceData) -> TraceStamp | None:
    """*trace*'s attribution (``services/tracing/attribution.py``); None when it could not be read.

    In a savepoint: a failed lookup must not fail the flush, which would lose
    the trace. An unstamped trace is stamped later by the backfill.
    """
    refs = TraceRefs(
        user_id=_coerce_uuid(trace.user_id),
        agent_id=_coerce_uuid(trace.agent_id),
        assignment_run_id=_coerce_uuid(trace.assignment_run_id),
        parent_trace_id=_coerce_uuid(trace.parent_trace_id),
        conversation_id=_coerce_uuid(trace.conversation_id),
    )
    try:
        async with session.begin_nested():
            return (await stamp_traces(session, {trace.id: refs})).get(trace.id)
    except Exception as exc:
        logger.warning(
            "Trace attribution failed, left for the backfill: %s", format_exception_message(exc)
        )
        return None


def _score_value_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# PostgreSQL rejects the null byte (\u0000) in text and JSONB columns with
# ``UntranslatableCharacterError``.  Any tool-result or LLM output that
# contains a null byte (e.g. from a GitHub GraphQL response body) would
# otherwise cause the entire span batch to be silently dropped.  The recursive
# strip helper is shared with the Langfuse span emitters
# (``services/langfuse_tracing.py``) via ``core/utils.py`` — single source of
# truth for null-byte sanitization.
_strip_null_bytes = strip_null_bytes


async def write_score_row(session: AsyncSession, score: dict[str, Any], trace_id: UUID) -> bool:
    """Store one score payload in the caller's transaction; False when it changed nothing.

    Shared by the tracing sink and the one-time rewrite of the chat scores
    written earlier, so both store a rater's score the same way.
    """
    # Scores carry user-provided text (feedback comments) — sanitize the
    # same way as trace/span payloads so a null byte can't drop the row.
    user_id = _coerce_uuid_any(score.get("user_id"))
    name = _strip_null_bytes(str(score.get("name") or FEEDBACK_SCORE_NAME))
    fields: dict[str, Any] = {
        "span_id": _coerce_uuid_any(score.get("span_id")),
        "source": str(score.get("source") or "USER"),
        "data_type": str(score.get("data_type") or "BOOLEAN"),
        "value": _score_value_float(score.get("value")),
        "string_value": _strip_null_bytes(score.get("string_value"))
        if isinstance(score.get("string_value"), str)
        else None,
        "comment": _strip_null_bytes(score.get("comment"))
        if isinstance(score.get("comment"), str)
        else None,
        "metadata_json": _strip_null_bytes(score.get("metadata"))
        if isinstance(score.get("metadata"), dict)
        else None,
        # The time of the ACTION, when the caller says it: scores are
        # flushed by detached tasks, so they can land out of order.
        "created_at": score["created_at"] if isinstance(score.get("created_at"), datetime) else datetime.now(UTC),
    }
    retract = bool(score.get("retract"))
    if retract:
        fields["value"] = None
    if user_id is not None:
        # One score per (trace, user, name), holding the rater's
        # LATEST action, under the same lock as the admin REST path so the
        # two never both insert. A retraction is kept as a NULL-valued row,
        # never deleted: it is the marker a delayed older rating is refused
        # against, and every average skips a NULL.
        await lock_rater_score(session, trace_id, user_id, name)
        existing = list(
            (
                await session.execute(
                    select(LLMScore)
                    .where(
                        LLMScore.trace_id == trace_id,
                        LLMScore.user_id == user_id,
                        LLMScore.name == name,
                    )
                    .order_by(LLMScore.created_at.desc(), LLMScore.id.desc())
                )
            ).scalars()
        )
        if existing and existing[0].created_at > fields["created_at"]:
            return False
        for row in existing[1:]:
            await session.delete(row)
        if existing:
            for key, value in fields.items():
                setattr(existing[0], key, value)
            return True
    elif retract:
        return False
    session.add(LLMScore(trace_id=trace_id, user_id=user_id, name=name, **fields))
    return True


class LocalPGSink:
    """Flush tracing payloads to PostgreSQL using a dedicated engine/pool."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._session_factory = session_factory or _build_tracing_session_factory()
        self.tracing_errors = 0

    async def flush_trace(self, trace: TraceData, spans: list[SpanData]) -> None:
        switched = apply_trace_switches(trace, spans)
        if switched is None:
            return
        trace, spans = switched
        try:
            async with self._session_factory() as session:
                await self._flush_trace_with_session(session, trace, spans)
                await session.commit()
        except Exception as exc:
            self.tracing_errors += 1
            logger.warning(
                "LocalPGSink.flush_trace failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )

    async def _flush_trace_with_session(
        self,
        session: AsyncSession,
        trace: TraceData,
        spans: list[SpanData],
    ) -> None:
        provider = _trace_provider(trace)
        # Resolve a price per model, memoised for this flush. Producers for chat /
        # assignment / general-chat traces do not stamp a provider in metadata, so
        # a provider-only lookup would never find a price (cost would be NULL for
        # ~all traffic). Fall back to a provider-agnostic match by model id so cost
        # is computed regardless, and resolve each span against ITS OWN model.
        price_cache: dict[str, ModelPrice | None] = {}

        async def _resolve_price(model: str | None) -> ModelPrice | None:
            if not model:
                return None
            if model in price_cache:
                return price_cache[model]
            price: ModelPrice | None = None
            if provider:
                price = await resolve_model_price(
                    session, model_id=model, provider=provider
                )
            if price is None:
                price = await resolve_model_price_any(session, model_id=model)
            price_cache[model] = price
            return price

        usage_parts = (_as_dict(trace.usage_details) or {}).get(USAGE_PARTS_KEY)
        if isinstance(usage_parts, list) and usage_parts:
            trace_costs = await _price_usage_parts(usage_parts, trace.model, _resolve_price)
        else:
            resolved_price = await _resolve_price(trace.model)
            trace_costs = calculate_costs(trace.usage_details, resolved_price)
        trace_total = trace_costs.get("total_cost")
        # A child can end BEFORE its parent (a consulted agent inside a still-
        # open orchestrator turn, an agent step inside a flow run's burst
        # trace) — the parent's row does not exist yet, and a dangling FK
        # would reject THIS WHOLE FLUSH, silently losing the child trace.
        # Keep the link in metadata instead; the row survives.
        parent_trace_uuid = _coerce_uuid(trace.parent_trace_id)
        trace_metadata = trace.metadata
        if parent_trace_uuid is not None:
            parent_exists = await session.scalar(
                select(LLMTrace.id).where(LLMTrace.id == parent_trace_uuid)
            )
            if parent_exists is None:
                trace_metadata = {
                    **(trace.metadata or {}),
                    "parent_trace_id": str(parent_trace_uuid),
                }
                parent_trace_uuid = None
        stamp = await _stamp_trace(session, trace)
        stamped_at = datetime.now(UTC)
        trace_row = LLMTrace(
            id=trace.id,
            agent_id=_coerce_uuid(trace.agent_id),
            user_id=_coerce_uuid(trace.user_id),
            conversation_id=_coerce_uuid(trace.conversation_id),
            assignment_run_id=_coerce_uuid(trace.assignment_run_id),
            parent_trace_id=parent_trace_uuid,
            source=trace.source,
            name=_strip_null_bytes(trace.name),
            model=_strip_null_bytes(trace.model),
            environment=trace.environment,
            metadata_json=_strip_null_bytes(trace_metadata),
            input=_strip_null_bytes(trace.input),
            output=_strip_null_bytes(trace.output),
            tags=_strip_null_bytes(trace.tags),
            started_at=trace.started_at,
            ended_at=trace.ended_at,
            duration_ms=trace.duration_ms,
            input_tokens=_token_int(trace.input_tokens),
            output_tokens=_token_int(trace.output_tokens),
            total_tokens=_token_int(trace.total_tokens),
            input_cost=_decimal_or_none(trace_costs.get("input_cost")),
            output_cost=_decimal_or_none(trace_costs.get("output_cost")),
            total_cost=_decimal_or_none(trace_total),
            status=trace.status,
            error_message=_strip_null_bytes(trace.error_message),
            failure_cause=trace.failure_cause,
            level=trace.level,
            attributed_user_id=stamp.attributed_user_id if stamp else None,
            attribution=stamp.attribution if stamp else None,
            department_id=stamp.department_id if stamp else None,
            department_resolved_at=stamped_at if stamp and stamp.department_resolved else None,
            agent_name_snapshot=_strip_null_bytes(stamp.agent_name_snapshot) if stamp else None,
            cached_input_tokens=_cached_input_tokens(trace.usage_details),
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        session.add(trace_row)
        await session.flush()

        ordered_spans = sorted(spans, key=lambda span: int(span.position))
        span_positions = {span.id: int(span.position) for span in ordered_spans}

        span_rows: list[LLMSpan] = []
        for span in ordered_spans:
            span_price = await _resolve_price(span.model or trace.model)
            span_costs = calculate_costs(span.usage_details, span_price)
            span_total = span_costs.get("total_cost")
            parent_span_id = _coerce_uuid(span.parent_span_id)
            if parent_span_id is not None:
                parent_position = span_positions.get(parent_span_id)
                if parent_position is None or parent_position >= int(span.position):
                    parent_span_id = None
            span_rows.append(
                LLMSpan(
                    id=span.id,
                    trace_id=trace.id,
                    parent_span_id=parent_span_id,
                    type=span.type,
                    name=_strip_null_bytes(span.name),
                    model=_strip_null_bytes(span.model),
                    model_parameters=_strip_null_bytes(span.model_parameters),
                    input=_strip_null_bytes(span.input),
                    output=_strip_null_bytes(span.output),
                    metadata_json=_strip_null_bytes(span.metadata),
                    level=span.level,
                    status_message=_strip_null_bytes(span.status_message),
                    started_at=span.started_at,
                    ended_at=span.ended_at,
                    duration_ms=span.duration_ms,
                    time_to_first_token=span.time_to_first_token,
                    input_tokens=_token_int(span.input_tokens),
                    output_tokens=_token_int(span.output_tokens),
                    input_cost=_decimal_or_none(span_costs.get("input_cost")),
                    output_cost=_decimal_or_none(span_costs.get("output_cost")),
                    total_cost=_decimal_or_none(span_total),
                    usage_details=_strip_null_bytes(span.usage_details),
                    tool_call_id=_strip_null_bytes(span.tool_call_id),
                    tool_arguments=_strip_null_bytes(span.tool_arguments),
                    stop_reason=_strip_null_bytes(span.stop_reason),
                    position=int(span.position),
                    created_at=datetime.now(UTC),
                )
            )
        if span_rows:
            session.add_all(span_rows)

    async def flush_score(self, score: dict[str, Any]) -> None:
        trace_id = _coerce_uuid_any(score.get("trace_id"))
        if trace_id is None or not trace_switches().storage_enabled:
            return
        try:
            async with self._session_factory() as session:
                changed = await self._flush_score_with_session(session, score, trace_id)
                if changed and (score.get("name") or FEEDBACK_SCORE_NAME) == FEEDBACK_SCORE_NAME:
                    from app.services.trace_storage import record_feedback_day_stale

                    await record_feedback_day_stale(session, trace_id)
                await session.commit()
        except Exception as exc:
            self.tracing_errors += 1
            logger.warning(
                "LocalPGSink.flush_score failed: %s",
                format_exception_message(exc),
                exc_info=True,
            )

    async def _flush_score_with_session(
        self,
        session: AsyncSession,
        score: dict[str, Any],
        trace_id: UUID,
    ) -> bool:
        return await write_score_row(session, score, trace_id)

    async def close(self) -> None:
        # Dispose the dedicated tracing engine/pool at shutdown. Only touch the
        # module-global engine when this sink is using it (a sink constructed with
        # an injected factory - e.g. in tests - must not dispose the global pool).
        global _TRACING_ENGINE, _TRACING_SESSION_FACTORY
        if self._session_factory is _TRACING_SESSION_FACTORY and _TRACING_ENGINE is not None:
            await _TRACING_ENGINE.dispose()
            _TRACING_ENGINE = None
            _TRACING_SESSION_FACTORY = None
