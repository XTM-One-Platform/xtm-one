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

"""Uniform, typed result contract for every metric.

Defines the four-status ``ScoreResult``, its ``MetricError``, stable ``MetricMetadata``
and canonical serialization.

``to_json`` is byte-stable: sorted keys, compact separators, non-ASCII preserved,
NaN/Infinity rejected. ``model_dump(mode="json")`` alone neither sorts keys nor
guarantees identical bytes.
"""

import json
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.evaluation_metrics.core.contracts import (
    MACRO_CLASSES,
    POOLED_DENOMINATOR,
    POOLED_NUMERATOR,
    JSONValue,
    MetricDirection,
    MetricError,
    MetricRollup,
    MetricStatus,
    check_rollup_counts,
)
from app.evaluation_metrics.core.serialization import FrozenMapping, _empty_mapping


class MetricMetadata(BaseModel):
    """Stable identity/description of a metric (versioned public contract)."""

    model_config = ConfigDict(frozen=True)

    key: str
    label: str
    kind: str
    primary_measure: str
    direction: MetricDirection
    #: How the run combines this metric's entry values; declared, never defaulted.
    rollup: MetricRollup
    #: Whether one entry's value means anything on its own. ``False`` when, on a
    #: single entry, the value repeats another metric or loses the meaning its name gives
    #: it; the run's value is unaffected and still shown. How a page shows the metric, not
    #: what it returns: left out of the serialized result, which stays byte for byte as it was.
    shown_on_entry: bool = Field(default=True, exclude=True)
    #: The metric's name on a single entry, when it means something narrower there than
    #: over a run; ``None`` keeps ``label``. Left out of the serialized result too.
    entry_label: str | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _validate_key(self) -> Self:
        if not self.key:
            raise ValueError("MetricMetadata.key must be non-empty")
        return self


class ScoreResult(BaseModel):
    """The uniform outcome of every metric."""

    model_config = ConfigDict(frozen=True)

    status: MetricStatus
    value: float | None
    detail: FrozenMapping = Field(default_factory=_empty_mapping)
    error: MetricError | None = None
    metadata: MetricMetadata

    @model_validator(mode="after")
    def _validate_invariants(self) -> Self:
        status, value, error = self.status, self.value, self.error
        if status is MetricStatus.scored:
            if value is None or not (0.0 <= value <= 100.0):
                raise ValueError("scored result requires a numeric value in [0, 100]")
            if error is not None:
                raise ValueError("scored result must not carry an error")
        elif status is MetricStatus.invalid:
            if value != 0:
                raise ValueError("invalid result requires value == 0")
            if error is None:
                raise ValueError("invalid result requires a structured error")
        elif status is MetricStatus.skipped:
            if value is not None:
                raise ValueError("skipped result requires value is None")
            if error is not None:
                raise ValueError("skipped result must not carry an error")
        elif status is MetricStatus.unavailable:
            if value is not None:
                raise ValueError("unavailable result requires value is None")
        check_rollup_counts(
            self.metadata.rollup, status is MetricStatus.scored, value, self.detail, scale=100.0
        )
        return self

    def to_dict(self) -> dict[str, JSONValue]:
        """Return a JSON-compatible dict (not sorted, not canonical).

        Returns:
            The result rendered via ``model_dump(mode="json")``. Key order is unspecified;
            use :meth:`to_json` when byte-stability matters.
        """
        return self.model_dump(mode="json")

    def to_json(self) -> str:
        """Return the canonical JSON string.

        Returns:
            A byte-stable JSON string with sorted object keys, compact separators and
            non-ASCII preserved. Repeated calls with equal inputs return identical bytes.

        Raises:
            ValueError: If the result contains ``NaN`` or ``Infinity``, which are rejected.
        """
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )


__all__ = [
    "MACRO_CLASSES",
    "POOLED_DENOMINATOR",
    "POOLED_NUMERATOR",
    "JSONValue",
    "MetricDirection",
    "MetricError",
    "MetricMetadata",
    "MetricRollup",
    "MetricStatus",
    "ScoreResult",
]
