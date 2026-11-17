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

"""Performance metric seam onto ``evaluation_metrics.performance``.

The only place the engine obtains deterministic performance metrics. It maps the captured
telemetry (durations, TTFT, the agent-scoped call timeline, token usage) onto the package's
:class:`ExecutionTelemetry`, runs the performance suite, and maps every
:class:`PerformanceResult` onto a platform-neutral :class:`MetricOutcome`. Package-native
types (``ExecutionTelemetry``, ``CallRecord``, ``TokenUsage``, ``PriceTable``,
``PerformanceResult``) never cross into an engine, API or serializer signature.

Pure adapter: no clock, no I/O. Incomplete telemetry for a metric yields that metric's
``unavailable`` / ``skipped`` outcome, never a failed run. A malformed payload surfaces as
the package's structured ``invalid`` result, not a crash. Cost fails soft: a missing or
incomplete price yields an ``unavailable`` cost metric, never a fabricated zero.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any

# Suite types are adapter-internal and must not be re-exported past this package
# (tests/test_evaluation_guards.py).
from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.performance import (
    ExecutionTelemetry,
    PerformanceResult,
    PriceTable,
    validate_telemetry,
)
from app.evaluation_metrics.performance.metrics import (
    call_counts,
    estimated_llm_cost,
    reliability,
    time_to_first_token,
    token_usage_total,
    wall_clock_latency,
)
from app.evaluation_metrics.performance.metrics.cost import COST_META
from app.evaluation_metrics.performance.metrics.counts import (
    RETRY_COUNT_META,
)
from app.evaluation_metrics.performance.metrics.counts import (
    retry_count as _retry_count_metric,
)
from app.services.evaluation.metric_package.results import MetricOutcome, MetricStatus

_ONE_MILLION = Decimal(1_000_000)

# Local trace ``type`` -> package call kind. Judge steps are left out of the agent's
# performance: the judge is the same for every agent an evaluation compares, so its time and
# cost are the evaluation's. Its time stays on the platform side, as ``judge_seconds``.
_MODEL_TRACE_TYPES = frozenset({"llm", "model"})
_TOOL_TRACE_TYPES = frozenset({"tool"})
_OVERHEAD_TRACE_TYPES = frozenset({"judge", "judge_skipped"})

# Price-row keys (per-1M-token), as sourced from ``llm_model_prices`` by the engine.
_PRICE_BUCKETS = (
    ("uncached_input_per_token", "input_price_per_1m"),
    ("output_per_token", "output_price_per_1m"),
    ("cache_read_per_token", "cache_read_price_per_1m"),
    ("cache_write_per_token", "cache_write_price_per_1m"),
)


@dataclass(frozen=True)
class PerformanceConfig:
    """Platform-neutral performance inputs, built once per run (mirrors ``QualityConfig``).

    The engine holds this OPAQUELY and never inspects the package types it stands in for. The
    token convention drives ``normalize_token_usage`` (cache/reasoning normalization); the
    currency prices a usage whose price row names none.
    """

    token_convention: str = "separate"  # noqa: S105 - a token-usage convention, not a secret
    currency: str = "USD"


@dataclass(frozen=True)
class ChainInputs:
    """An entry's chain ledger, as the engine resolved it (``run_ledger``).

    ``calls`` are the ledger calls of every agent in the chain. ``token_usage_by_model``
    pairs each serving model with its usage in the ``token_usage`` shape; ``price_rows_by_model``
    gives each model its own price rows. ``complete`` is ``False`` when a delegated task's
    ledger is missing: the chain's totals are then reported unavailable, never partial, with
    ``incomplete_reason`` as the reason (``run_ledger.IncompleteReason``).
    """

    calls: Sequence[Mapping[str, Any]]
    token_usage_by_model: Sequence[tuple[str | None, Mapping[str, Any]]]
    price_rows_by_model: Mapping[str | None, Sequence[Mapping[str, Any]]]
    complete: bool = True
    incomplete_reason: str | None = None


def run_performance(
    *,
    execution_details: Mapping[str, Any],
    token_usage: Mapping[str, Any] | None,
    retry_count: int | None,
    price_rows: Sequence[Mapping[str, Any]],
    config: PerformanceConfig,
    chain: ChainInputs | None = None,
) -> list[MetricOutcome]:
    """Compute the deterministic performance metrics for one execution.

    Returns a list of platform-neutral :class:`MetricOutcome`; no package
    ``ExecutionTelemetry`` / ``PerformanceResult`` / ``PriceTable`` crosses this boundary.
    Never raises for incomplete or malformed telemetry: those degrade to
    ``unavailable`` / ``skipped`` / ``invalid`` outcomes.

    With ``chain``, calls, usage and pricing come from the entry's chain ledger instead
    of ``execution_details["traces"]`` / ``token_usage`` / ``price_rows``.
    """
    if chain is not None:
        return _run_chain_performance(execution_details, retry_count, config, chain)
    raw = _build_raw_telemetry(execution_details, token_usage, retry_count, config)
    validation = validate_telemetry(raw)
    if validation.error is not None:
        # Malformed telemetry: one INVALID outcome per emitted metric key, never a crash.
        return [_to_outcome(key, res) for key, res in validation.invalid_results().items()]

    telemetry = validation.telemetry
    if telemetry is None:  # unreachable: validate_telemetry guarantees exactly one branch
        return []
    outcomes = [_to_outcome(key, res) for key, res in _run_suite(telemetry).items()]
    outcomes.append(_cost_outcome(telemetry, price_rows, config))
    return outcomes


def _run_chain_performance(
    execution_details: Mapping[str, Any],
    retry_count: int | None,
    config: PerformanceConfig,
    chain: ChainInputs,
) -> list[MetricOutcome]:
    """``run_performance`` over a chain ledger.

    An incomplete chain supplies no calls, no usage and no retry count, so every total
    reports ``unavailable`` through the suite's own rules while the agent's own latency
    and time to first token still score.
    """
    raw = _build_raw_telemetry(
        execution_details, None, retry_count if chain.complete else None, config
    )
    raw.pop("calls", None)
    prices_by_model: dict[str | None, PriceTable] = {}
    if chain.complete:
        raw["calls"] = [_ledger_call_record(call) for call in chain.calls]
        usages = [
            (model, usage)
            for model, usage in (
                (m, _build_token_usage(u, config)) for m, u in chain.token_usage_by_model
            )
            if usage is not None
        ]
        # Per model even when there is one: one part is priced and totalled
        # exactly as a single usage (``estimated_llm_cost``, ``token_usage_total``).
        if usages:
            raw["agent_token_usage_by_model"] = [{"model": m, "usage": u} for m, u in usages]
        prices_by_model = {
            model: table
            for model, rows in chain.price_rows_by_model.items()
            if (table := _build_price_table(rows, config)) is not None
        }
    validation = validate_telemetry(raw)
    if validation.error is not None:
        return [_to_outcome(key, res) for key, res in validation.invalid_results().items()]
    telemetry = validation.telemetry
    if telemetry is None:  # unreachable: validate_telemetry guarantees exactly one branch
        return []
    outcomes = [_to_outcome(key, res) for key, res in _run_suite(telemetry).items()]
    outcomes.append(_chain_cost_outcome(telemetry, prices_by_model))
    if not chain.complete:
        outcomes = [_as_chain_incomplete(outcome, chain.incomplete_reason) for outcome in outcomes]
    return outcomes


#: The suite's reasons for the telemetry an incomplete chain withholds.
_WITHHELD_BY_AN_INCOMPLETE_CHAIN = frozenset(
    {"calls_not_captured", "token_usage_not_captured", "retry_count_not_captured"}
)


def _as_chain_incomplete(outcome: MetricOutcome, reason: str | None) -> MetricOutcome:
    """Name why a total is unavailable: the chain is incomplete, not the telemetry uncaptured."""
    if outcome.status is not MetricStatus.UNAVAILABLE or outcome.detail not in _WITHHELD_BY_AN_INCOMPLETE_CHAIN:
        return outcome
    detail = f"chain_incomplete: {reason or 'ledger_missing'}"
    serialized = dict(outcome.serialized)
    serialized["detail"] = {**(serialized.get("detail") or {}), "reason": detail}
    return replace(outcome, detail=detail, serialized=serialized)


def _ledger_call_record(call: Mapping[str, Any]) -> dict[str, Any]:
    """A ledger call as a raw package ``CallRecord``: every chain call is agent-scoped."""
    record: dict[str, Any] = {
        "kind": call.get("kind"),
        "scope": "agent",
        "name": _text(call.get("name")),
        "duration_ms": _number(call.get("duration_ms")),
    }
    if call.get("outcome") in _CALL_OUTCOMES:
        record["outcome"] = call["outcome"]
    return record


def _chain_cost_outcome(
    telemetry: ExecutionTelemetry, prices_by_model: Mapping[str | None, PriceTable]
) -> MetricOutcome:
    """Each model's usage priced at its own table, failing soft like ``_cost_outcome``.

    One serving model is priced at the table the engine gave it, the run's own when it
    is the run's model, so an entry with no handover costs the same as without a chain.
    """
    try:
        result = estimated_llm_cost(telemetry, prices_by_model=prices_by_model)
    except ConfigurationError as exc:
        return _unavailable_outcome(COST_META.key, f"pricing_incomplete: {exc}")
    return _to_outcome(COST_META.key, result)


# The package's stable key for the retry-count metric, re-exported so callers never
# hardcode a metric key the package owns.
RETRY_COUNT_KEY = RETRY_COUNT_META.key


# --- Telemetry construction (adapter-internal, package types allowed) ------------------


def _build_raw_telemetry(
    execution_details: Mapping[str, Any],
    token_usage: Mapping[str, Any] | None,
    retry_count: int | None,
    config: PerformanceConfig,
) -> dict[str, Any]:
    """Shape captured telemetry into a raw mapping for :func:`validate_telemetry`.

    Built as plain dicts/lists (not typed package objects) so ``validate_telemetry`` is the
    single boundary that turns unvalidated caller data into typed telemetry: a malformed
    field surfaces as a structured error rather than a raised exception.
    """
    raw: dict[str, Any] = {
        # Elapsed time to the answer, timed by the engine around the agent call,
        # not ``agent_duration_ms``, which is the sum of recorded steps.
        "wall_clock_ms": _number(execution_details.get("agent_wall_clock_ms")),
        "ttft_ms": _number(execution_details.get("ttft_ms")),
        "retry_count": retry_count,
    }

    calls = _build_calls(execution_details.get("traces"))
    if calls is not None:
        raw["calls"] = calls

    usage = _build_token_usage(token_usage, config)
    if usage is not None:
        raw["agent_token_usage"] = usage

    return raw


_CALL_OUTCOMES = frozenset({"success", "error", "timeout", "cancelled"})


def _build_calls(traces: Any) -> list[dict[str, Any]] | None:
    """Map the agent-scoped local trace timeline onto ``CallRecord`` dicts.

    Returns ``None`` when no timeline was captured (uncaptured != zero calls). Judge steps
    are evaluation overhead and are dropped; unknown trace types are ignored. Call outcome is
    carried through from the capture site when it recorded one, and left absent otherwise: the
    package never assumes an absent outcome successful.
    """
    if not isinstance(traces, Sequence) or isinstance(traces, (str, bytes)) or not traces:
        return None
    calls: list[dict[str, Any]] = []
    for trace in traces:
        if not isinstance(trace, Mapping):
            continue
        ttype = trace.get("type")
        if ttype in _OVERHEAD_TRACE_TYPES:
            continue
        if ttype in _MODEL_TRACE_TYPES:
            kind = "model"
        elif ttype in _TOOL_TRACE_TYPES:
            kind = "tool"
        else:
            continue
        record: dict[str, Any] = {
            "kind": kind,
            "scope": "agent",
            "name": trace.get("name") or trace.get("description"),
            "duration_ms": _number(trace.get("duration_ms")),
        }
        outcome = _call_outcome(trace)
        if outcome is not None:
            record["outcome"] = outcome
            if outcome == "error":
                record["error_type"] = _text(trace.get("error_type"))
        calls.append(record)
    return calls


def _call_outcome(trace: Mapping[str, Any]) -> str | None:
    """Derive a recorded call outcome, or ``None`` when the status was not captured.

    Prefers the outcome the capture site stamped on the trace. Reliability is pooled over
    every agent call and is ``unavailable`` if one call lacks an outcome, so a success must
    be recorded as explicitly as a failure: inferring "no error field means it worked" would
    report a rate over calls whose fate was never observed. An unrecognised value is
    treated as uncaptured rather than coerced.
    """
    recorded = _text(trace.get("outcome"))
    if recorded in _CALL_OUTCOMES:
        return recorded
    if trace.get("error") or trace.get("is_error"):
        return "error"
    return None


def _build_token_usage(
    token_usage: Mapping[str, Any] | None, config: PerformanceConfig
) -> dict[str, Any] | None:
    """Build a raw ``TokenUsage`` mapping, or ``None`` when no bucket was captured."""
    if not token_usage:
        return None
    buckets = {
        "input_tokens": _int(token_usage.get("input_tokens")),
        "output_tokens": _int(token_usage.get("output_tokens")),
        "cache_read_input_tokens": _int(token_usage.get("cache_read_input_tokens")),
        "cache_write_input_tokens": _int(token_usage.get("cache_write_input_tokens")),
        "reasoning_output_tokens": _int(token_usage.get("reasoning_output_tokens")),
    }
    if all(value is None for value in buckets.values()):
        return None
    buckets["convention"] = token_usage.get("convention") or config.token_convention
    return buckets


# --- Suite execution + cost (adapter-internal) -----------------------------------------


def _run_suite(telemetry: ExecutionTelemetry) -> dict[str, PerformanceResult]:
    """Run every performance metric over ``telemetry`` (cost handled separately)."""
    results: dict[str, PerformanceResult] = {}

    def put(result: PerformanceResult) -> None:
        results[result.metadata.key] = result

    put(wall_clock_latency(telemetry))
    put(time_to_first_token(telemetry))
    for result in call_counts(telemetry).values():
        put(result)
    put(_retry_count_metric(telemetry))
    put(token_usage_total(telemetry))
    for result in reliability(telemetry).values():
        put(result)
    return results


def _cost_outcome(
    telemetry: ExecutionTelemetry,
    price_rows: Sequence[Mapping[str, Any]],
    config: PerformanceConfig,
) -> MetricOutcome:
    """Estimated LLM cost from the platform price table, failing soft.

    A missing price table yields ``unavailable`` from the package itself; a price table that
    omits a captured bucket raises ``ConfigurationError`` in the package, which is mapped to
    an ``unavailable`` outcome here rather than fabricating a zero cost.
    """
    price_table = _build_price_table(price_rows, config)
    try:
        result = estimated_llm_cost(telemetry, price_table)
    except ConfigurationError as exc:
        return _unavailable_outcome(COST_META.key, f"pricing_incomplete: {exc}")
    return _to_outcome(COST_META.key, result)


def _build_price_table(
    price_rows: Sequence[Mapping[str, Any]], config: PerformanceConfig
) -> PriceTable | None:
    """Build a package :class:`PriceTable` from platform per-1M-token price rows.

    Returns ``None`` (cost degrades to ``unavailable``) when no row was supplied or a row is
    unusable. Per-1M prices are converted to per-token so they price the normalized usage.
    """
    if not price_rows:
        return None
    row = price_rows[0]
    try:
        prices = {
            attr: _per_token(row.get(source_key))
            for attr, source_key in _PRICE_BUCKETS
        }
        return PriceTable(
            currency=row.get("currency") or config.currency,
            resolved_model=_text(row.get("resolved_model")),
            resolved_provider=_text(row.get("resolved_provider")),
            **prices,
        )
    except (ValueError, InvalidOperation, TypeError):
        return None


def _per_token(value: Any) -> Decimal | None:
    if value is None:
        return None
    return _to_decimal(value) / _ONE_MILLION


# --- Mapping helpers -------------------------------------------------------------------


def _to_outcome(key: str, result: PerformanceResult) -> MetricOutcome:
    """Map one package ``PerformanceResult`` onto a platform-neutral ``MetricOutcome``."""
    return MetricOutcome(
        key=key,
        status=MetricStatus(result.status.value),
        value=result.value,
        detail=_detail_text(result),
        serialized=result.to_dict(),
    )


def _unavailable_outcome(key: str, reason: str) -> MetricOutcome:
    """A synthetic ``unavailable`` outcome (mirrors the quality seam)."""
    return MetricOutcome(
        key=key,
        status=MetricStatus.UNAVAILABLE,
        value=None,
        detail=reason,
        serialized={
            "status": MetricStatus.UNAVAILABLE.value,
            "value": None,
            "detail": {"reason": reason},
            "metadata": {"key": key},
        },
    )


def _detail_text(result: PerformanceResult) -> str | None:
    """Extract a human-readable explanation from a package result."""
    if result.error is not None:
        return result.error.message
    detail = result.detail
    if isinstance(detail, Mapping):
        reason = detail.get("reason")
        if isinstance(reason, str):
            return reason
    return None


# --- Primitive coercion ----------------------------------------------------------------


def _number(value: Any) -> float | None:
    """Coerce a captured duration to ``float``; a non-number passes through for validation."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return value  # let validate_telemetry reject a malformed value structurally


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return value  # non-int passes through so validate_telemetry surfaces it


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _to_decimal(value: Any) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


__all__ = [
    "PerformanceConfig",
    "run_performance",
]
