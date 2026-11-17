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

"""The selectable metric catalogue for one agent family.

Read from the package's own declared metric metadata, per family, because:

- A hardcoded platform list goes stale silently: a metric added upstream would be
  computed and stored but never appear in the selector.
- Scanning ``evaluation_metrics`` rows would make a metric selectable only after it
  was computed once, so an author could not choose what a first run measures.

Performance metrics measure the call, not the output, so every family advertises
the same performance half.
"""

from __future__ import annotations

from typing import Final

from app.services.evaluation.metric_package import registry as _registry_module
from app.services.evaluation.metric_package.registry import FAMILY_MODULES, VALID_DIRECTIONS

# The quality metrics that compose Overall Quality when an evaluation's quality selection is
# unconfigured: one or a few per family, the measures of the answer itself.
# Every other quality metric is offered and selectable, never a default contributor:
#
# - a validity check (`stix.envelope_validity`, `stix.semantic_validity`,
#   `json.schema_validity`, `classification.vocabulary_validity`): an invalid answer
#   already scores 0 on the graded metrics, so counting validity again rewards any
#   well-formed answer (an empty valid STIX bundle read 40);
# - a stricter or redundant view of a default (`classification.subset_accuracy`,
#   `prose.exact_match`, `prose.rouge_l`, `stix.entity_prf.by_type`): it repeats what the
#   default measures, and doubles its weight in the headline.
#
# An allowlist, not an exclusion list: a metric added to the package is opt-in until it is
# deliberately made a default, never a silent change to every unconfigured headline.
DEFAULT_QUALITY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "stix.entity_prf",
        "stix.relationship_prf",
        "json.field_accuracy",
        "classification.micro_prf",
        "classification.macro_prf",
        "classification.samples_f1",
        "prose.token_prf",
    }
)

# Performance metrics that are measured and shown but do NOT enter Overall Performance when
# the evaluation's performance selection is unconfigured. Time to first token is the
# latency minus the time spent writing the answer, so for a short answer the two are nearly
# the same number: counting both by default weighs the latency twice. It stays selectable.
NON_DEFAULT_PERFORMANCE_KEYS: Final[frozenset[str]] = frozenset({"performance.latency.ttft"})


def quality_catalog(family: str | None) -> list[dict]:
    """Selectable quality metrics for *family*, or `[]` when it has none.

    A metric belongs to *family* when the family's sub-package declares it
    (``registry.FAMILY_MODULES``), as recorded by the registry walk.
    """
    if (family or "") not in FAMILY_MODULES:
        return []
    return [
        {
            "key": d.key,
            "label": d.label,
            "kind": "quality",
            # An undeclared direction is reported as `null`, never guessed, and the metric
            # is still offered; `directions.py` drops it because an unoriented metric must
            # not compose a headline. Guessing higher-is-better would invert an error rate
            # and report the worst run as the best.
            "direction": d.direction if d.direction in VALID_DIRECTIONS else None,
            # Whether this metric composes the headline when the author made no
            # selection. False does NOT mean hidden, unscored or unselectable.
            "default_contributor": d.key in DEFAULT_QUALITY_KEYS,
            # A quality value is a 0-100 score: no unit.
            "unit": None,
        }
        for d in _registry_module.registry().definitions
        if d.half == "quality" and family in d.families
    ]


def performance_catalog() -> list[dict]:
    """Selectable performance metrics, identical for every family.

    Read from ``performance_directions()``, which omits any metric without a declared
    direction, so this half never yields ``direction: null``. An unoriented performance
    metric cannot be normalised; offering it would compose a headline that silently
    excludes it.
    """
    from app.services.evaluation.metric_package.directions import performance_directions
    from app.services.evaluation.metric_package.units import performance_units

    units = performance_units()
    # The package's own label, as on the quality half, never one spelled from the key.
    labels = {
        d.key: d.label for d in _registry_module.registry().definitions if d.half == "performance"
    }
    return [
        {
            "key": key,
            "label": labels[key],
            "kind": "performance",
            "direction": direction,
            # Honoured by the score: an unconfigured performance selection leaves these
            # keys out (``aggregation.compute_performance_headlines``), as the selector does.
            "default_contributor": key not in NON_DEFAULT_PERFORMANCE_KEYS,
            "unit": units.get(key),
        }
        for key, direction in sorted(performance_directions().items())
    ]


def metric_catalog(family: str | None) -> list[dict]:
    """Everything selectable for *family*: its quality half plus performance."""
    return quality_catalog(family) + performance_catalog()


__all__ = [
    "DEFAULT_QUALITY_KEYS",
    "NON_DEFAULT_PERFORMANCE_KEYS",
    "metric_catalog",
    "performance_catalog",
    "quality_catalog",
]
