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

"""Shared foundations for the quality and performance suites.

Depends on neither suite, so the graph stays acyclic: ``quality -> core`` and
``performance -> core``, never ``quality <-> performance``. Only symbols both suites need live
here; result types, metadata types and scorer protocols stay in each suite.
"""

from app.evaluation_metrics.core.contracts import (
    JSONValue,
    MetricDirection,
    MetricError,
    MetricStatus,
)
from app.evaluation_metrics.core.errors import (
    ConfigurationError,
    EvaluationMetricsError,
    InternalContractError,
)

__all__ = [
    "ConfigurationError",
    "EvaluationMetricsError",
    "InternalContractError",
    "JSONValue",
    "MetricDirection",
    "MetricError",
    "MetricStatus",
]
