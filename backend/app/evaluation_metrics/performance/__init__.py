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

"""Deterministic performance metrics for agentic executions.

Interprets **already-captured, caller-supplied telemetry** and measures nothing itself: no
clock, no provider call, no network or filesystem. Values are carried by
:class:`PerformanceResult`, never by the quality suite's ``ScoreResult``.

The public surface is this module's exports, documented in ``app/evaluation_metrics/README.md``.
"""

from app.evaluation_metrics.performance.metrics import (
    call_counts,
    estimated_llm_cost,
    reliability,
    retry_count,
    time_to_first_token,
    token_usage_total,
    wall_clock_latency,
)
from app.evaluation_metrics.performance.pricing import CostBasis, PriceTable
from app.evaluation_metrics.performance.results import (
    PerformanceMetadata,
    PerformanceResult,
)
from app.evaluation_metrics.performance.telemetry import (
    CallKind,
    CallOutcome,
    CallRecord,
    CallScope,
    ExecutionTelemetry,
    ModelTokenUsage,
    NormalizedTokenUsage,
    TelemetryValidation,
    TokenConvention,
    TokenUsage,
    normalize_token_usage,
    validate_telemetry,
)
from app.evaluation_metrics.performance.units import MetricUnit

__all__ = [
    "CallKind",
    "CallOutcome",
    "CallRecord",
    "CallScope",
    "CostBasis",
    "ExecutionTelemetry",
    "MetricUnit",
    "ModelTokenUsage",
    "NormalizedTokenUsage",
    "PerformanceMetadata",
    "PerformanceResult",
    "PriceTable",
    "TelemetryValidation",
    "TokenConvention",
    "TokenUsage",
    "call_counts",
    "estimated_llm_cost",
    "normalize_token_usage",
    "reliability",
    "retry_count",
    "time_to_first_token",
    "token_usage_total",
    "validate_telemetry",
    "wall_clock_latency",
]
