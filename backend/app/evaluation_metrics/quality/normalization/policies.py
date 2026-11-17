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

"""Typed, strict-by-default comparison policy for STRUCTURED_JSON JSON field accuracy.

Unspecified aspects fall back to strict defaults (positional arrays, exact values, no
coercion, no normalization). Unknown option values raise ``ConfigurationError``, never
become a score.
"""

import math
from collections.abc import Callable
from enum import StrEnum
from functools import cached_property
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality.normalization.prose import ProseNormalizationPolicy


class ArraySemantics(StrEnum):
    """How an array at a given JSON path is compared."""

    positional = "positional"
    set = "set"
    multiset = "multiset"


class FieldMode(StrEnum):
    """How the field at a given JSON path is scored.

    Derived from the field's schema declaration; this enum lets an author override it.

    Defined here because ``ComparisonPolicy`` validates against it and ``normalization``
    must not import from ``families``. ``families.structured_json.field_modes``
    re-exports it, so the scorers and the policy share one object.
    """

    exact = "exact"
    enum = "enum"
    enum_set = "enum_set"
    free_text = "free_text"
    resource_address = "resource_address"
    object_list = "object_list"
    excluded = "excluded"


_ARRAY_SEMANTICS = frozenset(ArraySemantics)
_FIELD_MODES = frozenset(FieldMode)


# Named, order-independent string normalizations selectable per JSON path.
NORMALIZERS: dict[str, Callable[[str], str]] = {
    "lowercase": str.lower,
    "casefold": str.casefold,
    "strip": str.strip,
}


def _under(path: str, containers: list[str]) -> bool:
    """Whether ``path`` is one of ``containers`` or lies under one."""
    return any(path == container or path.startswith(f"{container}/") for container in containers)


class ComparisonPolicy(BaseModel):
    """Strict-by-default configuration for JSON field comparison.

    All maps are keyed by JSON Pointer path (e.g. ``/items/0/name``). Any path not
    mentioned uses the strict default.
    """

    model_config = ConfigDict(frozen=True)

    array_semantics: dict[str, str] = Field(default_factory=dict)
    numeric_tolerance: dict[str, float] = Field(default_factory=dict)
    type_coercion: dict[str, bool] = Field(default_factory=dict)
    normalization: dict[str, str] = Field(default_factory=dict)
    ignored_paths: list[str] = Field(default_factory=list)
    field_modes: dict[str, str] = Field(default_factory=dict)
    field_weights: dict[str, float] = Field(default_factory=dict)
    prose_policy: ProseNormalizationPolicy | None = None

    @model_validator(mode="after")
    def _validate_options(self) -> Self:
        for path, sem in self.array_semantics.items():
            # Membership against the members, not ``__members__.values()``, which accepts
            # a plain string only because this is a ``StrEnum``.
            if sem not in _ARRAY_SEMANTICS:
                raise ConfigurationError(
                    f"unknown array_semantics {sem!r} at {path!r}; "
                    f"expected one of {sorted(ArraySemantics.__members__)}"
                )
        for path, tol in self.numeric_tolerance.items():
            if math.isnan(tol) or math.isinf(tol) or tol < 0:
                raise ConfigurationError(
                    f"invalid numeric_tolerance {tol!r} at {path!r}; "
                    "expected a finite, non-negative number"
                )
        for path, name in self.normalization.items():
            if name not in NORMALIZERS:
                raise ConfigurationError(
                    f"unknown normalization {name!r} at {path!r}; "
                    f"expected one of {sorted(NORMALIZERS)}"
                )
        for path, mode in self.field_modes.items():
            if mode not in _FIELD_MODES:
                raise ConfigurationError(
                    f"unknown field_mode {mode!r} at {path!r}; "
                    f"expected one of {sorted(FieldMode.__members__)}"
                )
        for path, weight in self.field_weights.items():
            if math.isnan(weight) or math.isinf(weight) or weight < 0:
                raise ConfigurationError(
                    f"invalid field_weight {weight!r} at {path!r}; "
                    "expected a finite, non-negative number"
                )
        # A weighted path that is also ignored, or under an ignored container, states two
        # incompatible intents. Refuse.
        contradictory = sorted(
            path for path in self.field_weights if _under(path, self.ignored_paths)
        )
        if contradictory:
            raise ConfigurationError(
                f"paths carry a field_weight while also being ignored: {contradictory}; "
                "a weight asks the path to count and ignoring it asks the opposite"
            )
        # The same contradiction through ``excluded``, the field mode authors use to drop
        # a field; ``ignored_paths`` stays for the callers that use it.
        excluded = [path for path, mode in self.field_modes.items() if mode == FieldMode.excluded]
        excluded_and_weighted = sorted(
            path for path in self.field_weights if _under(path, excluded)
        )
        if excluded_and_weighted:
            raise ConfigurationError(
                f"paths carry a field_weight while also being excluded: "
                f"{excluded_and_weighted}; a weight asks the path to count and "
                "excluding it asks the opposite"
            )
        overridden_free_text = any(
            mode == FieldMode.free_text for mode in self.field_modes.values()
        )
        if overridden_free_text and self.prose_policy is None:
            raise ConfigurationError(
                "prose_policy is required when any field resolves to free_text; "
                "per-field and whole-answer prose scores are only comparable when "
                "computed under the same normalization policy"
            )
        return self

    def is_ignored(self, path: str) -> bool:
        """Whether ``path`` is in ``ignored_paths``: a set lookup, as it runs per field."""
        return path in self._ignored

    @cached_property
    def _ignored(self) -> frozenset[str]:
        return frozenset(self.ignored_paths)

    def array_mode(self, path: str) -> ArraySemantics:
        """Return the array-comparison mode configured for ``path``.

        Args:
            path: The JSON Pointer of the array.

        Returns:
            The configured :class:`ArraySemantics`, or ``positional`` by default.
        """
        return ArraySemantics(self.array_semantics.get(path, ArraySemantics.positional))

    def field_mode(self, path: str) -> FieldMode | None:
        """Return the author's mode override for ``path``, if any.

        Args:
            path: The JSON Pointer of the field.

        Returns:
            The overriding :class:`FieldMode`, or ``None`` when the mode should be
            derived from the field's schema declaration instead.
        """
        mode = self.field_modes.get(path)
        return FieldMode(mode) if mode is not None else None

    def field_weight(self, path: str) -> float:
        """Return the relative weight configured for ``path``.

        Args:
            path: The JSON Pointer of the field.

        Returns:
            The configured weight, or ``1.0``: with no weights set, the score is a plain
            mean.
        """
        return self.field_weights.get(path, 1.0)


__all__ = ["NORMALIZERS", "ArraySemantics", "ComparisonPolicy", "FieldMode"]
