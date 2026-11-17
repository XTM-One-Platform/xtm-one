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

"""The pooled completion rate and the tool error rate, with deterministic failure breakdowns.

An absent outcome is never assumed successful: if any agent-scoped call lacks one, every
reliability metric is ``unavailable``, rather than a rate biased by the calls that have one.
An observed empty call sequence is no missing telemetry: it leaves no rate to state, so both
are ``skipped``.
"""

from collections.abc import Mapping

from app.evaluation_metrics.core.contracts import (
    POOLED_DENOMINATOR,
    POOLED_NUMERATOR,
    MetricDirection,
    MetricRollup,
    MetricStatus,
)
from app.evaluation_metrics.performance.results import (
    PerformanceMetadata,
    PerformanceResult,
)
from app.evaluation_metrics.performance.telemetry import (
    CallKind,
    CallOutcome,
    CallRecord,
    ExecutionTelemetry,
)
from app.evaluation_metrics.performance.units import MetricUnit


def _rate_metadata(
    key: str, label: str, measure: str, direction: MetricDirection
) -> PerformanceMetadata:
    return PerformanceMetadata(
        key=key,
        label=label,
        kind="rate",
        primary_measure=measure,
        unit=MetricUnit.ratio,
        direction=direction,
        rollup=MetricRollup.pooled,
    )


COMPLETION_RATE_META = _rate_metadata(
    "performance.reliability.completion_rate",
    "Completion Rate",
    "completion_rate",
    MetricDirection.higher_is_better,
)
TOOL_ERROR_RATE_META = _rate_metadata(
    "performance.reliability.tool_error_rate",
    "Tool Error Rate",
    "tool_error_rate",
    MetricDirection.lower_is_better,
)


def _unavailable(metadata: PerformanceMetadata, reason: str) -> PerformanceResult:
    return PerformanceResult(
        status=MetricStatus.unavailable,
        value=None,
        detail={"reason": reason},
        metadata=metadata,
    )


def _skipped(metadata: PerformanceMetadata, reason: str) -> PerformanceResult:
    return PerformanceResult(
        status=MetricStatus.skipped,
        value=None,
        detail={"reason": reason},
        metadata=metadata,
    )


def _scored(metadata: PerformanceMetadata, value: float, **detail: object) -> PerformanceResult:
    return PerformanceResult(
        status=MetricStatus.scored, value=value, detail=detail, metadata=metadata
    )


def _all_unavailable(reason: str) -> dict[str, PerformanceResult]:
    return {
        metadata.key: _unavailable(metadata, reason)
        for metadata in (COMPLETION_RATE_META, TOOL_ERROR_RATE_META)
    }


def _tool_error_rate(calls: tuple[CallRecord, ...]) -> PerformanceResult:
    tool_calls = [call for call in calls if call.kind is CallKind.tool]
    if not tool_calls:
        return _skipped(TOOL_ERROR_RATE_META, "no_tool_calls")

    by_tool: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_error_type: dict[str, int] = {}
    error_count = 0
    for call in tool_calls:
        outcome = call.outcome
        if outcome is None:  # pragma: no cover - the caller rejects uncaptured outcomes
            continue
        by_status[outcome.value] = by_status.get(outcome.value, 0) + 1
        if outcome is CallOutcome.error:
            error_count += 1
            name = call.name or "<unnamed>"
            by_tool[name] = by_tool.get(name, 0) + 1
            error_type = call.error_type or "<unknown>"
            by_error_type[error_type] = by_error_type.get(error_type, 0) + 1

    return _scored(
        TOOL_ERROR_RATE_META,
        error_count / len(tool_calls),
        # Pooled over the run: failed tool calls over tool calls.
        **{POOLED_NUMERATOR: error_count, POOLED_DENOMINATOR: len(tool_calls)},
        by_tool=dict(sorted(by_tool.items())),
        by_status=dict(sorted(by_status.items())),
        by_error_type=dict(sorted(by_error_type.items())),
    )


def reliability(telemetry: ExecutionTelemetry) -> Mapping[str, PerformanceResult]:
    """Report the pooled completion rate and the tool error rate under their own stable keys.

    Args:
        telemetry: The execution's captured observations.

    Returns:
        A mapping with two keys: the pooled completion rate (carrying the per-status counts in
        ``detail``) and the tool error rate (carrying breakdowns by tool name, status and error
        type). ``unavailable`` when the call sequence, or any call's outcome, was not captured,
        never ``0``. Both are ``skipped`` when the agent made no call, and the tool error rate
        when it made no tool call.
    """
    calls = telemetry.agent_calls()
    if calls is None:
        return _all_unavailable("calls_not_captured")
    if not calls:
        return {
            COMPLETION_RATE_META.key: _skipped(COMPLETION_RATE_META, "no_calls"),
            TOOL_ERROR_RATE_META.key: _skipped(TOOL_ERROR_RATE_META, "no_tool_calls"),
        }
    if any(call.outcome is None for call in calls):
        return _all_unavailable("outcome_not_captured")

    counts = {outcome.value: 0 for outcome in CallOutcome}
    for call in calls:
        outcome = call.outcome
        if outcome is None:  # pragma: no cover - rejected by the guard above
            continue
        counts[outcome.value] += 1
    total = len(calls)

    return {
        COMPLETION_RATE_META.key: _scored(
            COMPLETION_RATE_META,
            counts["success"] / total,
            # Pooled over the run: completed calls over calls.
            **{POOLED_NUMERATOR: counts["success"], POOLED_DENOMINATOR: total},
            **counts,
        ),
        TOOL_ERROR_RATE_META.key: _tool_error_rate(calls),
    }


__all__ = [
    "COMPLETION_RATE_META",
    "TOOL_ERROR_RATE_META",
    "reliability",
]
