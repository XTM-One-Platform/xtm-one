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

"""The uniform result contract for every performance metric.

Separate from the quality suite's ``ScoreResult``: a latency of ``10_432 ms`` or a cost of
``0.87 USD`` cannot fit a 0-100 score, so the value is unbounded and qualified by a ``unit``.
The status, error and serialization conventions are shared with the quality suite.

Two rules differ from the quality suite and are enforced at construction:

* ``invalid`` carries ``value=None``, not ``0``: a zero would read as a fast or cheap execution.
* Only ``invalid`` carries an error; ``skipped`` and ``unavailable`` carry ``error=None``.
  Every ``unavailable`` result explains itself through ``detail.reason``.
"""

import json
import math
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.evaluation_metrics.core.contracts import (
    JSONValue,
    MetricDirection,
    MetricError,
    MetricRollup,
    MetricStatus,
    check_rollup_counts,
)
from app.evaluation_metrics.core.serialization import FrozenMapping, _empty_mapping
from app.evaluation_metrics.performance.units import MetricUnit

# The kinds a performance metric may declare.
_KINDS = frozenset({"duration", "tokens", "cost", "count", "rate"})


class PerformanceMetadata(BaseModel):
    """Stable identity of a performance metric (versioned public contract)."""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    kind: str
    primary_measure: str
    unit: MetricUnit
    direction: MetricDirection
    #: How the run combines this metric's entry values; declared, never defaulted.
    rollup: MetricRollup

    @model_validator(mode="after")
    def _validate_identity(self) -> Self:
        if not self.key:
            raise ValueError("PerformanceMetadata.key must be non-empty")
        if not self.key.startswith("performance."):
            raise ValueError(
                f"performance metric keys must be namespaced 'performance.', got {self.key!r}"
            )
        if self.kind not in _KINDS:
            raise ValueError(
                f"unknown metric kind {self.kind!r}; expected one of {sorted(_KINDS)}"
            )
        return self


class PerformanceResult(BaseModel):
    """The uniform outcome of every performance metric."""

    model_config = ConfigDict(frozen=True)

    status: MetricStatus
    value: float | None
    detail: FrozenMapping = Field(default_factory=_empty_mapping)
    error: MetricError | None = None
    metadata: PerformanceMetadata

    @model_validator(mode="after")
    def _validate_invariants(self) -> Self:
        status, value, error = self.status, self.value, self.error
        if value is not None and not math.isfinite(value):
            raise ValueError("performance value must be finite (NaN/Infinity rejected)")
        if status is MetricStatus.scored:
            if value is None:
                raise ValueError("scored result requires a numeric value")
            if error is not None:
                raise ValueError("scored result must not carry an error")
        else:
            if value is not None:
                raise ValueError(f"{status.value} result requires value is None")
            if status is MetricStatus.invalid:
                if error is None:
                    raise ValueError("invalid result requires a structured error")
            else:
                if error is not None:
                    raise ValueError(
                        f"{status.value} result must not carry an error; "
                        "only 'invalid' carries one"
                    )
                if status is MetricStatus.unavailable and "reason" not in self.detail:
                    raise ValueError(
                        "unavailable result requires a structured detail['reason']"
                    )
        check_rollup_counts(self.metadata.rollup, status is MetricStatus.scored, value, self.detail)
        return self

    def to_dict(self) -> dict[str, JSONValue]:
        """Return a JSON-compatible dict (not sorted, not canonical).

        Returns:
            The result rendered via ``model_dump(mode="json")``. Use :meth:`to_json` when
            byte-stability matters.
        """
        return self.model_dump(mode="json")

    def to_json(self) -> str:
        """Return the canonical JSON string.

        Returns:
            A byte-stable JSON string with sorted object keys, compact separators and
            non-ASCII preserved. Repeated calls with equal inputs return identical bytes.

        Raises:
            ValueError: If the result contains a non-finite number.
        """
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )


__all__ = ["MetricUnit", "PerformanceMetadata", "PerformanceResult"]
