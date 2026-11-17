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

"""Platform-neutral result types crossing the metric-package seam.

These types are the only result vocabulary the engine, API and serializers see. The
package-native ``ScoreResult`` / ``PerformanceResult`` are mapped onto ``MetricOutcome``
inside the adapter and never leave it.

``MetricStatus`` mirrors the package's four statuses by value so the engine can branch
without importing ``evaluation_metrics``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class MetricStatus(StrEnum):
    """Platform-owned mirror of the package's four metric statuses.

    Values match ``evaluation_metrics.core.contracts.MetricStatus`` exactly so a
    package status string maps 1:1 without importing the package enum.
    """

    SCORED = "scored"
    INVALID = "invalid"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class MetricOutcome:
    """One metric result, opaque to the engine.

    Only ``key`` and ``serialized`` leave the adapters: the engine persists
    ``serialized`` verbatim (``to_persistable``), and a metric's orientation comes from
    the registry (``directions``), never from an outcome. ``status``, ``value`` and
    ``detail`` are what the adapters themselves branch on.
    """

    key: str
    status: MetricStatus
    value: float | None
    detail: str | None
    serialized: dict


__all__ = ["MetricOutcome", "MetricStatus"]
