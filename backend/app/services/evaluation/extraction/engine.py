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

"""Applying a validated extractor chain to one source text.

Pure: no database, no HTTP, no models, so the same function serves save-time
preview, fresh-run scoring and re-score, and preview and run agree.

The fold is first-order and linear: one value in, one value out, left to right. No
operation receives the remainder of the chain or dispatches element-wise over a
sub-pipeline (see ``spec`` for why).

Nothing here evaluates author-supplied strings. Operations are selected from a
closed dict by exact name; there is no ``eval``, no ``exec``, no attribute dispatch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from app.services.structured_output import extract_json_text

from .canonical import canonicalise, normalise_label_set
from .failures import (
    LIMIT_EXCEEDED,
    PAYLOAD_UNPARSEABLE,
    SELECTION_EMPTY,
    TYPE_MISMATCH,
    ExtractionFailure,
)
from .limits import ExtractionLimits, current_limits
from .spec import (
    ARRAY,
    COLLECT_KEYS,
    DEDUPE_SORT,
    FIND_JSON,
    FLATTEN,
    OBJECT,
    OBJECT_KEYS,
    OBJECT_VALUES,
    PARSE_JSON,
    SELECT_PATH,
)


@dataclass(frozen=True)
class ExtractionOutcome:
    """Either a canonical prediction, or a typed failure. Never both, never neither."""

    prediction: str | None = None
    failure: ExtractionFailure | None = None

    @property
    def failed(self) -> bool:
        return self.failure is not None


def _kind_name(value: Any) -> str:
    if isinstance(value, dict):
        return OBJECT
    if isinstance(value, list):
        return ARRAY
    return type(value).__name__


def _depth_problem(value: Any, limits: ExtractionLimits) -> bool:
    """True when ``value`` nests deeper than the bound.

    Iterative rather than recursive: a hostile payload must cost a bounded walk,
    not a stack overflow, and Python's recursion limit is not a security boundary.
    """
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > limits.max_parse_depth:
            return True
        # Refuse on the way down, not after pushing a whole level, so a wide and
        # deep payload is rejected without a full walk.
        if isinstance(node, dict):
            if node and depth + 1 > limits.max_parse_depth:
                return True
            for child in node.values():
                stack.append((child, depth + 1))
        elif isinstance(node, list):
            if node and depth + 1 > limits.max_parse_depth:
                return True
            for child in node:
                stack.append((child, depth + 1))
    return False


def _too_many(
    count: int, limits: ExtractionLimits, op: str, index: int
) -> ExtractionFailure | None:
    """Refuse a projected size before the value that large is built."""
    if count > limits.max_values:
        return ExtractionFailure(
            reason=LIMIT_EXCEEDED,
            message=(
                f"'{op}' would produce {count} values, more than the maximum of "
                f"{limits.max_values}; the chain was stopped before the oversized "
                "value was built"
            ),
            operation_index=index + 1,
            operation=op,
        )
    return None


def _at(failure: ExtractionFailure, index: int, op: str) -> ExtractionFailure:
    """Attribute a failure raised by a shared helper to the operation that ran it."""
    return replace(failure, operation_index=index + 1, operation=op)


def _walk_path(
    value: Any, path: list[str], op: str, index: int
) -> tuple[Any, ExtractionFailure | None]:
    """Follow literal key segments. No wildcards, no indices, no filters."""
    current = value
    for segment in path:
        if not isinstance(current, dict):
            return None, ExtractionFailure(
                reason=TYPE_MISMATCH,
                message=f"'{op}' met a value that is not an object at the path",
                operation_index=index + 1,
                operation=op,
                path="/".join(path),
                expected_kind=OBJECT,
                actual_kind=_kind_name(current),
            )
        if segment not in current:
            return None, ExtractionFailure(
                reason=SELECTION_EMPTY,
                message=f"'{op}' matched nothing at the configured path",
                operation_index=index + 1,
                operation=op,
                path="/".join(path),
            )
        current = current[segment]
    return current, None


def _apply_one(
    op: str,
    operation: dict[str, Any],
    value: Any,
    index: int,
    limits: ExtractionLimits,
) -> tuple[Any, ExtractionFailure | None]:
    if op == FIND_JSON:
        # The text of the JSON payload inside an answer: the first fence holding JSON,
        # else the outermost object or array. Found, never repaired: ``parse_json`` still
        # refuses a payload that is not valid JSON.
        if not isinstance(value, str):
            return None, ExtractionFailure(
                reason=TYPE_MISMATCH,
                message="'find_json' expects text",
                operation_index=index + 1,
                operation=op,
                expected_kind="text",
                actual_kind=_kind_name(value),
            )
        found = extract_json_text(value)
        if not found.startswith(("{", "[")):
            return None, ExtractionFailure(
                reason=PAYLOAD_UNPARSEABLE,
                message="no JSON object or array found in the text",
                operation_index=index + 1,
                operation=op,
            )
        return found, None

    if op == PARSE_JSON:
        if not isinstance(value, str):
            return None, ExtractionFailure(
                reason=TYPE_MISMATCH,
                message="'parse_json' expects text",
                operation_index=index + 1,
                operation=op,
                expected_kind="text",
                actual_kind=_kind_name(value),
            )
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return None, ExtractionFailure(
                reason=PAYLOAD_UNPARSEABLE,
                message="the payload is not valid JSON",
                operation_index=index + 1,
                operation=op,
            )
        if not isinstance(parsed, (dict, list)):
            return None, ExtractionFailure(
                reason=TYPE_MISMATCH,
                message="'parse_json' expects the payload to be an object or an array",
                operation_index=index + 1,
                operation=op,
                expected_kind=OBJECT,
                actual_kind=_kind_name(parsed),
            )
        if _depth_problem(parsed, limits):
            return None, ExtractionFailure(
                reason=LIMIT_EXCEEDED,
                message=(
                    f"the payload nests deeper than {limits.max_parse_depth} levels"
                ),
                operation_index=index + 1,
                operation=op,
            )
        # parse_json is an expansion point: bound the container here rather than
        # let a huge list reach a later operation.
        problem = _too_many(len(parsed), limits, op, index)
        if problem is not None:
            return None, problem
        return parsed, None

    if op == SELECT_PATH:
        path = [str(segment) for segment in operation.get("path", [])]
        return _walk_path(value, path, op, index)

    if op in (OBJECT_KEYS, OBJECT_VALUES):
        if not isinstance(value, dict):
            return None, ExtractionFailure(
                reason=TYPE_MISMATCH,
                message=f"'{op}' expects an object",
                operation_index=index + 1,
                operation=op,
                expected_kind=OBJECT,
                actual_kind=_kind_name(value),
            )
        problem = _too_many(len(value), limits, op, index)
        if problem:
            return None, problem
        if op == OBJECT_KEYS:
            return list(value.keys()), None
        return list(value.values()), None

    if op == COLLECT_KEYS:
        # For each item of the array: select ``path``, require an object, collect its
        # keys. First-order: the element step is fixed by the operation, not by what
        # follows it, so the chain stays typeable.
        if not isinstance(value, list):
            return None, ExtractionFailure(
                reason=TYPE_MISMATCH,
                message="'collect_keys' expects an array",
                operation_index=index + 1,
                operation=op,
                expected_kind=ARRAY,
                actual_kind=_kind_name(value),
            )
        path = [str(segment) for segment in operation.get("path", [])]
        collected: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                return None, ExtractionFailure(
                    reason=TYPE_MISMATCH,
                    message="'collect_keys' met an array item that is not an object",
                    operation_index=index + 1,
                    operation=op,
                    path="/".join(path),
                    expected_kind=OBJECT,
                    actual_kind=_kind_name(item),
                )
            selected, failure = _walk_path(item, path, op, index)
            if failure is not None:
                return None, failure
            if not isinstance(selected, dict):
                return None, ExtractionFailure(
                    reason=TYPE_MISMATCH,
                    message=(
                        "'collect_keys' selected a value that is not an object, so "
                        "it has no keys to collect"
                    ),
                    operation_index=index + 1,
                    operation=op,
                    path="/".join(path),
                    expected_kind=OBJECT,
                    actual_kind=_kind_name(selected),
                )
            problem = _too_many(len(collected) + len(selected), limits, op, index)
            if problem:
                return None, problem
            collected.extend(str(key) for key in selected)
        return collected, None

    if op == FLATTEN:
        if not isinstance(value, list):
            return None, ExtractionFailure(
                reason=TYPE_MISMATCH,
                message="'flatten' expects an array",
                operation_index=index + 1,
                operation=op,
                expected_kind=ARRAY,
                actual_kind=_kind_name(value),
            )
        flattened: list[Any] = []
        for element in value:
            addition = len(element) if isinstance(element, list) else 1
            problem = _too_many(len(flattened) + addition, limits, op, index)
            if problem:
                return None, problem
            if isinstance(element, list):
                flattened.extend(element)
            else:
                flattened.append(element)
        return flattened, None

    if op == DEDUPE_SORT:
        if not isinstance(value, list):
            return None, ExtractionFailure(
                reason=TYPE_MISMATCH,
                message="'dedupe_sort' expects an array",
                operation_index=index + 1,
                operation=op,
                expected_kind=ARRAY,
                actual_kind=_kind_name(value),
            )
        # Deduplicate here, not at serialisation, so the value after this step
        # matches the operation's name.
        labels, failure = normalise_label_set(value, limits)
        if failure is not None:
            return None, _at(failure, index, op)
        return labels, None

    raise AssertionError(f"unhandled operation {op!r}")  # pragma: no cover


def apply_extractor(
    extractor: dict[str, Any],
    source_text: str,
    *,
    limits: ExtractionLimits | None = None,
) -> ExtractionOutcome:
    """Apply a validated extractor to one source text.

    The caller is responsible for having validated the chain. A chain that would fail
    validation still fails here as a typed failure rather than an exception, except an
    operation in ``_CATALOG`` with no branch in ``_apply_one``: that programming error
    raises ``AssertionError``, and
    ``test_every_catalogued_operation_has_a_dispatch_branch`` keeps it from a run.
    """
    active = limits or current_limits()

    if len(source_text.encode("utf-8")) > active.max_source_bytes:
        # Checked before parsing, so an oversized payload costs a length check.
        return ExtractionOutcome(
            failure=ExtractionFailure(
                reason=LIMIT_EXCEEDED,
                message=(
                    f"the source exceeds the maximum of {active.max_source_bytes} "
                    "bytes and was refused before parsing"
                ),
            )
        )

    value: Any = source_text
    for index, raw_operation in enumerate(extractor.get("operations", [])):
        operation = dict(raw_operation)
        value, failure = _apply_one(
            str(operation["op"]), operation, value, index, active
        )
        if failure is not None:
            return ExtractionOutcome(failure=failure)

    prediction, canonical_failure = canonicalise(
        value, str(extractor.get("output_kind")), active
    )
    if canonical_failure is not None:
        return ExtractionOutcome(failure=canonical_failure)
    return ExtractionOutcome(prediction=prediction)


__all__ = ["ExtractionOutcome", "apply_extractor"]
