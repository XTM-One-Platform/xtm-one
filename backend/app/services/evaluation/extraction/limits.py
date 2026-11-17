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

"""Bounds for the declarative prediction extractor.

The defaults are conservative estimates, not measurements: no real agent output
was available to size them. They are configurable through
``Settings`` so a wrong estimate can be adjusted without a release. When
representative outputs exist, record byte size, nesting depth and cardinality as
aggregate metadata only (never the content) and restate each default as measured.

Every bound is checked at a stated point, and every breach maps onto the
``limit_exceeded`` failure reason rather than raising.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import settings

# A chain with no operations yields raw text, which no family scores and which the
# absent-extractor path already expresses. Rejected at save time.
MIN_OPERATIONS = 1


@dataclass(frozen=True)
class ExtractionLimits:
    """Every bound in one place, so none can be restated and drift."""

    max_operations: int
    max_path_segments: int
    max_source_bytes: int
    max_sample_bytes: int
    max_parse_depth: int
    max_values: int
    max_value_chars: int

    min_operations: int = MIN_OPERATIONS

    def __post_init__(self) -> None:
        # Fail closed: a non-positive bound is unusable, and substituting a default
        # would hide an operator's typo.
        #
        # Spelled out rather than reflected over field names: the package forbids
        # dynamic attribute dispatch (see the no-code tripwire), with no exemption.
        for field_name, value in (
            ("max_operations", self.max_operations),
            ("max_path_segments", self.max_path_segments),
            ("max_source_bytes", self.max_source_bytes),
            ("max_sample_bytes", self.max_sample_bytes),
            ("max_parse_depth", self.max_parse_depth),
            ("max_values", self.max_values),
            ("max_value_chars", self.max_value_chars),
            ("min_operations", self.min_operations),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(
                    f"extraction limit {field_name!r} must be a positive integer, "
                    f"got {value!r}"
                )
        if self.max_operations < self.min_operations:
            raise ValueError(
                "extraction limit 'max_operations' must be at least "
                f"{self.min_operations}, got {self.max_operations}"
            )


def current_limits() -> ExtractionLimits:
    """The bounds in force, read from ``Settings`` on every call.

    Read per call rather than memoised so a test can override a single bound and see
    the boundary move.
    """
    return ExtractionLimits(
        max_operations=settings.extraction_max_operations,
        max_path_segments=settings.extraction_max_path_segments,
        max_source_bytes=settings.extraction_max_source_bytes,
        max_sample_bytes=settings.extraction_max_sample_bytes,
        max_parse_depth=settings.extraction_max_parse_depth,
        max_values=settings.extraction_max_values,
        max_value_chars=settings.extraction_max_value_chars,
    )


__all__ = ["MIN_OPERATIONS", "ExtractionLimits", "current_limits"]
