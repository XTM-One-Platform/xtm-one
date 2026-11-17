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

"""How a metric's entry values make its run value, read from the registry.

A pooled metric's value is a share of things counted inside an entry (5 failed tool calls
out of 10, 9 of 10 labels), so the run adds up its entries' counts and divides, rather than
averaging their rates: an entry with 1 call must not weigh as much as one with 100. Each
entry's counts travel in its result's ``detail`` under ``POOLED_NUMERATOR`` and
``POOLED_DENOMINATOR``.

A macro metric's value is the unweighted mean of a per-class F1, so the run sums each
class's counts over its entries, then averages the classes' F1: an F1 per entry would score
a rare class only where it appears. Each entry's per-class counts travel under
``MACRO_CLASSES``.

Every other metric is averaged, less the spread penalty.
"""

from __future__ import annotations

from app.evaluation_metrics.core.contracts import (
    MACRO_CLASSES,
    MACRO_COUNT_KEYS,
    POOLED_DENOMINATOR,
    POOLED_NUMERATOR,
)
from app.evaluation_metrics.quality.primitives.aggregate import macro_f1_from_counts
from app.services.evaluation.metric_package import registry as _registry_module


def is_pooled_key(key: str) -> bool:
    """True iff the package declares *key* pooled."""
    return key in _registry_module.registry().pooled_keys


def is_macro_key(key: str) -> bool:
    """True iff the package declares *key* a macro F1 over the run."""
    return key in _registry_module.registry().macro_keys


__all__ = [
    "MACRO_CLASSES",
    "MACRO_COUNT_KEYS",
    "POOLED_DENOMINATOR",
    "POOLED_NUMERATOR",
    "is_macro_key",
    "is_pooled_key",
    "macro_f1_from_counts",
]
