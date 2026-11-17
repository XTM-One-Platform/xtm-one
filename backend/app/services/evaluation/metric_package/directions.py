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

"""Metric orientation (higher/lower is better) as a platform-neutral lookup.

Aggregation must know a metric's orientation before combining it. The package owns
it, so it is resolved here, in the sole importer of ``evaluation_metrics``, never in
``aggregation.py`` (``tests/test_evaluation_guards.py``).

Orientation belongs to the metric definition, not to a measurement, so it comes from
the package's metadata rather than from each stored result: reading it back out of
``execution_details`` would scan a JSON blob on the main aggregation path and leave a
run whose blob lacks the field unoriented.

Every quality metric is higher-is-better today, but orientation is resolved for both
halves: a ``lower_is_better`` metric averaged unflipped would score the worse run higher,
so nothing assumes a half's direction.

Fail-closed: an unknown key resolves to ``None`` and a metric with no direction is
excluded from the headline, never assumed higher-is-better. Most performance metrics
are ``lower_is_better``, so that guess would make the slowest run the winner while
still rendering as a plausible 0-100 score.

This module also says which half a key belongs to, which separates package metrics
from the execution-level keys (``ttft_seconds``, ``input_tokens``, ``cost_usd``, ...)
that older rows still hold in ``evaluation_metrics``.
"""

from __future__ import annotations

from collections.abc import Mapping

from app.services.evaluation.metric_package import registry as _registry_module
from app.services.evaluation.metric_package.registry import (
    HIGHER_IS_BETTER,
    LOWER_IS_BETTER,
    VALID_DIRECTIONS,
)


def _quality_registry() -> Mapping[str, str]:
    """``{key: direction}`` of the oriented quality metrics, as a read-only view."""
    return _registry_module.registry().quality_directions


def _performance_registry() -> Mapping[str, str]:
    """``{key: direction}`` of the oriented performance metrics, as a read-only view."""
    return _registry_module.registry().performance_directions


def quality_directions() -> dict[str, str]:
    """``{metric_key: direction}`` for every quality metric the package defines."""
    return dict(_quality_registry())


def performance_directions() -> dict[str, str]:
    """``{metric_key: direction}`` for every performance metric the package defines."""
    return dict(_performance_registry())


def metric_direction(key: str) -> str | None:
    """Direction for one metric key, or ``None`` when the package declares none."""
    return _performance_registry().get(key) or _quality_registry().get(key)


def is_performance_key(key: str) -> bool:
    """True iff ``key`` is a package **performance** metric (comparative half)."""
    return key in _performance_registry()


def is_quality_key(key: str) -> bool:
    """True iff ``key`` is a package **quality** metric (absolute half)."""
    return key in _quality_registry()


__all__ = [
    "HIGHER_IS_BETTER",
    "LOWER_IS_BETTER",
    "VALID_DIRECTIONS",
    "is_performance_key",
    "is_quality_key",
    "metric_direction",
    "performance_directions",
    "quality_directions",
]
