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

"""Token usage normalized across provider conventions.

Only agent-scoped usage counts, so evaluation overhead never inflates the agent's figure.
"""

from app.evaluation_metrics.core.contracts import MetricDirection, MetricRollup, MetricStatus
from app.evaluation_metrics.performance.results import (
    PerformanceMetadata,
    PerformanceResult,
)
from app.evaluation_metrics.performance.telemetry import (
    ExecutionTelemetry,
    normalize_token_usage,
    sum_normalized_usage,
)
from app.evaluation_metrics.performance.units import MetricUnit

TOTAL_TOKENS_META = PerformanceMetadata(
    key="performance.tokens.total",
    label="Total Tokens",
    kind="tokens",
    primary_measure="total_tokens",
    unit=MetricUnit.tokens,
    direction=MetricDirection.lower_is_better,
    rollup=MetricRollup.mean,
)


def token_usage_total(telemetry: ExecutionTelemetry) -> PerformanceResult:
    """Report normalized total agent token usage with a per-bucket breakdown.

    Args:
        telemetry: The execution's captured observations.

    Returns:
        ``scored`` with the normalized total, whose ``detail`` carries every normalized bucket,
        or ``unavailable`` when usage was not captured. The declared convention is applied
        first, so ``separate`` and ``inclusive`` yield the same total and no token counts
        twice. Reasoning tokens are reported but excluded from the total.
    """
    parts = telemetry.agent_usage_parts()
    if parts is None:
        return PerformanceResult(
            status=MetricStatus.unavailable,
            value=None,
            detail={"reason": "token_usage_not_captured"},
            metadata=TOTAL_TOKENS_META,
        )

    # Normalized per model before adding, so two conventions never meet raw.
    normalized = sum_normalized_usage([normalize_token_usage(part.usage) for part in parts])
    conventions = {part.usage.convention.value for part in parts}
    if normalized.total_tokens is None:
        return PerformanceResult(
            status=MetricStatus.unavailable,
            value=None,
            detail={"reason": "token_usage_not_captured"},
            metadata=TOTAL_TOKENS_META,
        )

    return PerformanceResult(
        status=MetricStatus.scored,
        value=float(normalized.total_tokens),
        detail={
            "uncached_input_tokens": normalized.uncached_input_tokens,
            "output_tokens": normalized.output_tokens,
            "cache_read_input_tokens": normalized.cache_read_input_tokens,
            "cache_write_input_tokens": normalized.cache_write_input_tokens,
            "reasoning_output_tokens": normalized.reasoning_output_tokens,
            "convention": conventions.pop() if len(conventions) == 1 else "mixed",
        },
        metadata=TOTAL_TOKENS_META,
    )


__all__ = ["TOTAL_TOKENS_META", "token_usage_total"]
