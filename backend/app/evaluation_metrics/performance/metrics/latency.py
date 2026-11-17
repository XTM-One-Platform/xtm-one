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

"""End-to-end latency and time to first token, from caller-supplied durations."""

from app.evaluation_metrics.core.contracts import (
    JSONValue,
    MetricDirection,
    MetricRollup,
    MetricStatus,
)
from app.evaluation_metrics.performance.results import (
    PerformanceMetadata,
    PerformanceResult,
)
from app.evaluation_metrics.performance.telemetry import ExecutionTelemetry
from app.evaluation_metrics.performance.units import MetricUnit


def _duration_metadata(key: str, label: str, measure: str) -> PerformanceMetadata:
    return PerformanceMetadata(
        key=key,
        label=label,
        kind="duration",
        primary_measure=measure,
        unit=MetricUnit.milliseconds,
        direction=MetricDirection.lower_is_better,
        rollup=MetricRollup.mean,
    )


WALL_CLOCK_META = _duration_metadata(
    "performance.latency.wall_clock", "End-to-End Latency", "wall_clock"
)
TTFT_META = _duration_metadata(
    "performance.latency.ttft", "Time to First Token", "ttft"
)


def _scored(
    metadata: PerformanceMetadata, value: float, **detail: JSONValue
) -> PerformanceResult:
    return PerformanceResult(
        status=MetricStatus.scored, value=value, detail=detail, metadata=metadata
    )


def _unavailable(metadata: PerformanceMetadata, reason: str) -> PerformanceResult:
    return PerformanceResult(
        status=MetricStatus.unavailable,
        value=None,
        detail={"reason": reason},
        metadata=metadata,
    )


def wall_clock_latency(telemetry: ExecutionTelemetry) -> PerformanceResult:
    """Report the authoritative end-to-end latency.

    Args:
        telemetry: The execution's captured observations.

    Returns:
        ``scored`` with the caller-supplied wall-clock duration in milliseconds, or
        ``unavailable`` when it was not captured — never ``0``.
    """
    if telemetry.wall_clock_ms is None:
        return _unavailable(WALL_CLOCK_META, "wall_clock_not_captured")
    return _scored(WALL_CLOCK_META, telemetry.wall_clock_ms)


def time_to_first_token(telemetry: ExecutionTelemetry) -> PerformanceResult:
    """Report the time to the first token of the final user-visible response.

    Args:
        telemetry: The execution's captured observations.

    Returns:
        ``scored`` with the caller-supplied TTFT, or ``unavailable`` when the execution was not
        streamed or no such first-token event exists.
    """
    if telemetry.ttft_ms is None:
        return _unavailable(TTFT_META, "ttft_not_captured")
    return _scored(TTFT_META, telemetry.ttft_ms)


__all__ = [
    "TTFT_META",
    "WALL_CLOCK_META",
    "time_to_first_token",
    "wall_clock_latency",
]
