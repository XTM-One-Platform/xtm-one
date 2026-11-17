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

"""Agent-scoped model and tool call counts.

An uncaptured call sequence yields ``unavailable``, an observed-empty one ``0``: "never
recorded" and "made no calls" support different conclusions.
"""

from collections.abc import Mapping

from app.evaluation_metrics.core.contracts import MetricDirection, MetricRollup, MetricStatus
from app.evaluation_metrics.performance.results import (
    PerformanceMetadata,
    PerformanceResult,
)
from app.evaluation_metrics.performance.telemetry import CallKind, ExecutionTelemetry
from app.evaluation_metrics.performance.units import MetricUnit


def _count_metadata(key: str, label: str, measure: str) -> PerformanceMetadata:
    return PerformanceMetadata(
        key=key,
        label=label,
        kind="count",
        primary_measure=measure,
        unit=MetricUnit.count,
        direction=MetricDirection.lower_is_better,
        rollup=MetricRollup.mean,
    )


MODEL_CALL_COUNT_META = _count_metadata(
    "performance.counts.model_calls", "Model Calls", "model_calls"
)
TOOL_CALL_COUNT_META = _count_metadata(
    "performance.counts.tool_calls", "Tool Calls", "tool_calls"
)
RETRY_COUNT_META = _count_metadata(
    "performance.counts.retries", "Retries", "retries"
)


def _count(
    telemetry: ExecutionTelemetry, kind: CallKind, metadata: PerformanceMetadata
) -> PerformanceResult:
    calls = telemetry.agent_calls(kind)
    if calls is None:
        return PerformanceResult(
            status=MetricStatus.unavailable,
            value=None,
            detail={"reason": "calls_not_captured"},
            metadata=metadata,
        )
    return PerformanceResult(
        status=MetricStatus.scored, value=float(len(calls)), metadata=metadata
    )


def _tool_call_count(telemetry: ExecutionTelemetry) -> PerformanceResult:
    calls = telemetry.agent_calls(CallKind.tool)
    if calls is None:
        return PerformanceResult(
            status=MetricStatus.unavailable,
            value=None,
            detail={"reason": "calls_not_captured"},
            metadata=TOOL_CALL_COUNT_META,
        )
    by_tool: dict[str, int] = {}
    for call in calls:
        name = call.name or "<unnamed>"
        by_tool[name] = by_tool.get(name, 0) + 1
    return PerformanceResult(
        status=MetricStatus.scored,
        value=float(len(calls)),
        detail={"by_tool": dict(sorted(by_tool.items()))},
        metadata=TOOL_CALL_COUNT_META,
    )


def call_counts(telemetry: ExecutionTelemetry) -> Mapping[str, PerformanceResult]:
    """Count agent-scoped model and tool calls under their own stable keys.

    Args:
        telemetry: The execution's captured observations.

    Returns:
        A mapping from stable metric key to result, one per call kind. Overhead-scoped calls
        are excluded. The tool-call result's ``detail`` carries a deterministic per-tool-name
        breakdown.
    """
    return {
        MODEL_CALL_COUNT_META.key: _count(
            telemetry, CallKind.model, MODEL_CALL_COUNT_META
        ),
        TOOL_CALL_COUNT_META.key: _tool_call_count(telemetry),
    }


def retry_count(telemetry: ExecutionTelemetry) -> PerformanceResult:
    """Report the caller-supplied retry count, distinct from the underlying call count.

    Args:
        telemetry: The execution's captured observations.

    Returns:
        ``scored`` with the caller-supplied retry count, or ``unavailable`` when it was not
        captured — never ``0``.
    """
    if telemetry.retry_count is None:
        return PerformanceResult(
            status=MetricStatus.unavailable,
            value=None,
            detail={"reason": "retry_count_not_captured"},
            metadata=RETRY_COUNT_META,
        )
    return PerformanceResult(
        status=MetricStatus.scored,
        value=float(telemetry.retry_count),
        metadata=RETRY_COUNT_META,
    )


__all__ = [
    "MODEL_CALL_COUNT_META",
    "RETRY_COUNT_META",
    "TOOL_CALL_COUNT_META",
    "call_counts",
    "retry_count",
]
