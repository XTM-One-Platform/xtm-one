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

"""Encapsulated seam onto the ``evaluation_metrics`` package.

This package is the sole importer of ``evaluation_metrics`` in the backend
(enforced by ``tests/test_evaluation_guards.py``). This module exports only
platform-neutral callables and the ``MetricOutcome`` / ``MetricStatus`` result types.
Package-native types (``Family``, ``ScoreResult``, telemetry/result models) stay inside
the adapter modules and never appear in an engine, API or serializer signature, so an
upstream API change is absorbed here.
"""

from __future__ import annotations

from app.services.evaluation.metric_package.availability import (
    MetadataNestedTooDeeplyError,
    MetricPackageUnavailableError,
    guard_package_call,
    metric_suite_version,
)
from app.services.evaluation.metric_package.catalog import (
    metric_catalog,
    performance_catalog,
    quality_catalog,
)
from app.services.evaluation.metric_package.directions import (
    HIGHER_IS_BETTER,
    LOWER_IS_BETTER,
    is_performance_key,
    is_quality_key,
    metric_direction,
    performance_directions,
    quality_directions,
)
from app.services.evaluation.metric_package.entry_view import EntryView, metric_entry_view
from app.services.evaluation.metric_package.families import (
    family_is_valid,
    list_families,
)
from app.services.evaluation.metric_package.performance_adapter import (
    RETRY_COUNT_KEY,
    ChainInputs,
    PerformanceConfig,
    run_performance,
)
from app.services.evaluation.metric_package.quality_adapter import (
    QualityConfig,
    QualityConfigError,
    build_quality_config,
    failed_answer_outcomes,
    normalize_field_scoring_overrides,
    resolve_declared_vocabulary,
    run_quality,
    to_persistable,
    unscorable_outcomes,
    validate_field_scoring_overrides,
)
from app.services.evaluation.metric_package.reference_view import (
    CanonicalBundle,
    canonical_stix_bundle,
    reference_key,
    stix_backend_available,
)
from app.services.evaluation.metric_package.results import MetricOutcome, MetricStatus
from app.services.evaluation.metric_package.rollups import (
    MACRO_CLASSES,
    MACRO_COUNT_KEYS,
    POOLED_DENOMINATOR,
    POOLED_NUMERATOR,
    is_macro_key,
    is_pooled_key,
    macro_f1_from_counts,
)
from app.services.evaluation.metric_package.units import metric_unit, performance_units

__all__ = [
    "HIGHER_IS_BETTER",
    "LOWER_IS_BETTER",
    "MACRO_CLASSES",
    "MACRO_COUNT_KEYS",
    "POOLED_DENOMINATOR",
    "POOLED_NUMERATOR",
    "RETRY_COUNT_KEY",
    "CanonicalBundle",
    "ChainInputs",
    "EntryView",
    "MetadataNestedTooDeeplyError",
    "MetricOutcome",
    "MetricPackageUnavailableError",
    "MetricStatus",
    "PerformanceConfig",
    "QualityConfig",
    "QualityConfigError",
    "build_quality_config",
    "canonical_stix_bundle",
    "failed_answer_outcomes",
    "family_is_valid",
    "guard_package_call",
    "is_macro_key",
    "is_performance_key",
    "is_pooled_key",
    "is_quality_key",
    "list_families",
    "macro_f1_from_counts",
    "metric_catalog",
    "metric_direction",
    "metric_entry_view",
    "metric_suite_version",
    "metric_unit",
    "normalize_field_scoring_overrides",
    "performance_catalog",
    "performance_directions",
    "performance_units",
    "quality_catalog",
    "quality_directions",
    "reference_key",
    "resolve_declared_vocabulary",
    "run_performance",
    "run_quality",
    "stix_backend_available",
    "to_persistable",
    "unscorable_outcomes",
    "validate_field_scoring_overrides",
]
