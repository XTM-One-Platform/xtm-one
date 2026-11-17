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

"""Single-execution performance metrics.

``METRIC_METADATA`` lists every metric the suite emits, so a failed telemetry validation
marks each of them ``invalid``: a missing metric then always means "not computed".
"""

from app.evaluation_metrics.performance.metrics.cost import COST_META, estimated_llm_cost
from app.evaluation_metrics.performance.metrics.counts import (
    MODEL_CALL_COUNT_META,
    RETRY_COUNT_META,
    TOOL_CALL_COUNT_META,
    call_counts,
    retry_count,
)
from app.evaluation_metrics.performance.metrics.latency import (
    TTFT_META,
    WALL_CLOCK_META,
    time_to_first_token,
    wall_clock_latency,
)
from app.evaluation_metrics.performance.metrics.reliability import (
    COMPLETION_RATE_META,
    TOOL_ERROR_RATE_META,
    reliability,
)
from app.evaluation_metrics.performance.metrics.tokens import (
    TOTAL_TOKENS_META,
    token_usage_total,
)
from app.evaluation_metrics.performance.results import PerformanceMetadata

METRIC_METADATA: tuple[PerformanceMetadata, ...] = (
    WALL_CLOCK_META,
    TTFT_META,
    MODEL_CALL_COUNT_META,
    TOOL_CALL_COUNT_META,
    RETRY_COUNT_META,
    TOTAL_TOKENS_META,
    COST_META,
    COMPLETION_RATE_META,
    TOOL_ERROR_RATE_META,
)

__all__ = [
    "METRIC_METADATA",
    "call_counts",
    "estimated_llm_cost",
    "reliability",
    "retry_count",
    "time_to_first_token",
    "token_usage_total",
    "wall_clock_latency",
]
