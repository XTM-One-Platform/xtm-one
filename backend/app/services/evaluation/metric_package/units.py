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

"""The unit of each performance metric, as a platform-neutral lookup.

A performance value is unbounded and only means something with its unit: ``1200`` is
1.2 s of latency or 1,200 tokens depending on the metric, and ``0.05`` a 5 % rate. The
package declares the unit on every metric's metadata, so the API reads it from there and
the client formats a value by its unit rather than guessing from the key.

Quality metrics have no entry: every one is a 0-100 score. An unknown key resolves to
``None`` and the client shows the bare number rather than a unit nobody declared.
"""

from __future__ import annotations

from app.services.evaluation.metric_package import registry as _registry_module


def metric_unit(key: str) -> str | None:
    """The unit of one metric, or ``None`` for a quality metric or an unknown key."""
    return _registry_module.registry().performance_units.get(key)


def performance_units() -> dict[str, str]:
    """``{metric_key: unit}`` for every performance metric the package defines."""
    return dict(_registry_module.registry().performance_units)


__all__ = ["metric_unit", "performance_units"]
