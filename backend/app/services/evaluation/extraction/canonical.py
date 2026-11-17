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

"""Canonical serialisation of an extracted value.

``json.dumps(..., sort_keys=True, separators=(",", ":"))`` sorts object keys
recursively and leaves array order untouched. ``sort_keys`` and ``sorted`` order
strings by Unicode code point with no locale involvement, so the bytes are identical
across platforms.

Nothing in this package may import ``locale`` or use ``strxfrm`` / ``strcoll``:
collation is the only way this output could become locale-dependent, and a tripwire
test forbids it.
"""

from __future__ import annotations

import json
from typing import Any

from .failures import (
    LABEL_INVALID,
    LIMIT_EXCEEDED,
    TYPE_MISMATCH,
    ExtractionFailure,
)
from .limits import ExtractionLimits
from .spec import ARRAY, LABEL_SET, OBJECT


def _label_problem(value: Any) -> str | None:
    """Why ``value`` cannot be a label. ``None`` when it can.

    A blank or non-string element is a failure, never dropped or coerced: a quietly
    discarded element would change the score with no record that it did.
    """
    if not isinstance(value, str):
        return f"a label set accepts only strings, met {type(value).__name__}"
    if not value.strip():
        return "a label set accepts only non-empty strings, met a blank one"
    return None


def normalise_label_set(
    value: Any, limits: ExtractionLimits
) -> tuple[list[str] | None, ExtractionFailure | None]:
    """Validate, deduplicate and code-point sort a label set.

    Lives here so one definition of "a label set" serves both the ``dedupe_sort``
    operation and the canonical serialiser.
    """
    if not isinstance(value, list):
        return None, ExtractionFailure(
            reason=LABEL_INVALID,
            message=(
                f"a label set must be produced from a list, met {type(value).__name__}"
            ),
            expected_kind=ARRAY,
            actual_kind=type(value).__name__,
        )
    # Every element is checked before the set collapse below: deduplicating first
    # would hide duplicates of a blank or non-string element, and the failure with
    # them. ``max_source_bytes`` upstream caps the cost.
    for element in value:
        problem = _label_problem(element)
        if problem:
            return None, ExtractionFailure(reason=LABEL_INVALID, message=problem)
        if len(element) > limits.max_value_chars:
            return None, ExtractionFailure(
                reason=LIMIT_EXCEEDED,
                message=(
                    "a label exceeds the maximum length of "
                    f"{limits.max_value_chars} characters"
                ),
            )
    labels = sorted(set(value))
    if len(labels) > limits.max_values:
        return None, ExtractionFailure(
            reason=LIMIT_EXCEEDED,
            message=(
                f"the extracted label set holds {len(labels)} labels, at most "
                f"{limits.max_values} are allowed"
            ),
        )
    return labels, None


def canonicalise(
    value: Any, output_kind: str, limits: ExtractionLimits
) -> tuple[str | None, ExtractionFailure | None]:
    """Serialise ``value`` canonically, or explain why it cannot be."""
    if output_kind == LABEL_SET:
        labels, failure = normalise_label_set(value, limits)
        if failure is not None:
            return None, failure
        return _dump(labels), None

    # An unknown kind must fail, never fall through to ``array``: nothing
    # re-validates a stored extractor before a run, so this is the last check.
    if output_kind not in (OBJECT, ARRAY):
        return None, ExtractionFailure(
            reason=TYPE_MISMATCH,
            message=(
                f"output_kind {output_kind!r} is not a kind this version can "
                "serialise"
            ),
            expected_kind=OBJECT,
            actual_kind=_kind_of(value),
            configuration=True,
        )

    expected = output_kind
    actual = _kind_of(value)
    if actual != expected:
        return None, ExtractionFailure(
            reason=TYPE_MISMATCH,
            message=f"the chain produced {actual} but output_kind declares {expected}",
            expected_kind=expected,
            actual_kind=actual,
        )
    return _dump(value), None


def _kind_of(value: Any) -> str:
    if isinstance(value, dict):
        return OBJECT
    if isinstance(value, list):
        return ARRAY
    return type(value).__name__


def _dump(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = ["canonicalise", "normalise_label_set"]
