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

"""The closed, typed operation catalog and its save-time chain validation.

## Admitting a new operation

The catalog is closed. An operation is admitted only when all six hold:

1. **Demonstrated need**: a real agent output the existing operations cannot
   extract, not an anticipated one.
2. **One deterministic typed signature**: a single accepted kind, a single produced
   kind, the same result for the same input.
3. **Statically composable**: its signature participates in the chain fold below
   with no special case.
4. **Bounded at every expansion point**: ``limits`` applies to it as it applies to
   ``flatten``.
5. **No arbitrary code**: no expression evaluation, no author-supplied control flow.
6. **Parity tests** across save-time validation, preview, fresh run and re-score.

``parse_json`` already sits at the edge of condition 2 (it produces whichever kind
the document is) and is the limit of that edge, not an invitation to stretch it.

The pipeline is first-order: every operation consumes one value and produces one.
A higher-order operation cannot be typed: the chain after it would apply per
element, so the declared output kind would describe the element, not the result.
"""

from __future__ import annotations

from typing import Any, Final

from .limits import ExtractionLimits, current_limits

# --- Kinds -------------------------------------------------------------------

TEXT: Final = "text"
OBJECT: Final = "object"
ARRAY: Final = "array"
LABEL_SET: Final = "label_set"

KINDS: Final[frozenset[str]] = frozenset({TEXT, OBJECT, ARRAY, LABEL_SET})

# `text` is not a legal final kind: nothing downstream would score it, and the
# absent-extractor path already delivers raw text.
OUTPUT_KINDS: Final[tuple[str, ...]] = (OBJECT, ARRAY, LABEL_SET)

# --- Sources -----------------------------------------------------------------

AGENT_OUTPUT: Final = "agent_output"
TOOL_OUTPUT: Final = "tool_output"
TOOL_INPUT: Final = "tool_input"

SOURCE_KINDS: Final[tuple[str, ...]] = (AGENT_OUTPUT, TOOL_OUTPUT, TOOL_INPUT)
TOOL_SOURCE_KINDS: Final[frozenset[str]] = frozenset({TOOL_OUTPUT, TOOL_INPUT})

MAX_TOOL_NAME_CHARS: Final = 128

# --- Operations --------------------------------------------------------------

FIND_JSON: Final = "find_json"
PARSE_JSON: Final = "parse_json"
SELECT_PATH: Final = "select_path"
OBJECT_KEYS: Final = "object_keys"
OBJECT_VALUES: Final = "object_values"
COLLECT_KEYS: Final = "collect_keys"
FLATTEN: Final = "flatten"
DEDUPE_SORT: Final = "dedupe_sort"

# op -> (accepts, produces). ``None`` for ``produces`` means "whichever kind the
# value turns out to be", which only ``parse_json`` and ``select_path`` may claim.
_CATALOG: Final[dict[str, tuple[str, str | None]]] = {
    # Admitted: 5 of 44 extraction failures on the development data were
    # fenced or prose-wrapped JSON that ``parse_json`` rightly refuses.
    FIND_JSON: (TEXT, TEXT),
    PARSE_JSON: (TEXT, None),
    SELECT_PATH: (OBJECT, None),
    OBJECT_KEYS: (OBJECT, ARRAY),
    OBJECT_VALUES: (OBJECT, ARRAY),
    COLLECT_KEYS: (ARRAY, ARRAY),
    FLATTEN: (ARRAY, ARRAY),
    DEDUPE_SORT: (ARRAY, LABEL_SET),
}

OPERATIONS: Final[tuple[str, ...]] = tuple(_CATALOG)

# Operations taking a ``path`` parameter.
PATH_OPERATIONS: Final[frozenset[str]] = frozenset({SELECT_PATH, COLLECT_KEYS})


# --- v1 output-kind / family support matrix ----------------------------------
#
# Exactly two pairs are supported. PROSE and STIX_BUNDLE are unsupported in
# v1, not forbidden: a STIX extractor would need a canonical bundle form, and a
# prose one a target shape nobody has asked for. The refusal says "not supported
# in this version" to read as the scope decision it is.
_FAMILY_OUTPUT_KINDS: Final[dict[str, frozenset[str]]] = {
    "CLASSIFICATION": frozenset({LABEL_SET}),
    "STRUCTURED_JSON": frozenset({OBJECT, ARRAY}),
}


def supported_output_kinds(family: str) -> frozenset[str]:
    return _FAMILY_OUTPUT_KINDS.get(family, frozenset())


def validate_output_kind_for_family(output_kind: str, family: str | None) -> str | None:
    """Return an error message, or ``None`` when the pair is supported in v1."""
    if not family:
        return (
            "an output extractor needs the evaluation to declare a family; "
            "set one before configuring extraction"
        )
    allowed = supported_output_kinds(family)
    if not allowed:
        return (
            f"output extraction is not supported in this version for the "
            f"{family} family"
        )
    if output_kind not in allowed:
        return (
            f"output_kind {output_kind!r} is not supported in this version for the "
            f"{family} family (supported: {', '.join(sorted(allowed))})"
        )
    return None


# --- Validation --------------------------------------------------------------


def _source_problem(source: Any) -> str | None:
    if not isinstance(source, dict):
        return "source must be an object"
    unknown = sorted(set(source) - {"kind", "tool_name"})
    if unknown:
        return f"unknown source keys: {unknown}"
    kind = source.get("kind")
    if kind not in SOURCE_KINDS:
        return f"unknown source kind {kind!r}; expected one of {list(SOURCE_KINDS)}"
    tool_name = source.get("tool_name")
    if kind in TOOL_SOURCE_KINDS:
        if not isinstance(tool_name, str) or not tool_name.strip():
            return f"source kind {kind!r} requires a non-empty 'tool_name'"
        if len(tool_name) > MAX_TOOL_NAME_CHARS:
            return f"'tool_name' must be at most {MAX_TOOL_NAME_CHARS} characters"
    elif tool_name is not None:
        return f"source kind {kind!r} does not take a 'tool_name'"
    return None


def _operation_problem(
    operation: Any, index: int, limits: ExtractionLimits
) -> str | None:
    if not isinstance(operation, dict):
        return f"operations[{index}] must be an object"
    op = operation.get("op")
    if op not in _CATALOG:
        return (
            f"operations[{index}]: unknown operation {op!r}; "
            f"expected one of {list(OPERATIONS)}"
        )
    allowed_keys = {"op", "path"} if op in PATH_OPERATIONS else {"op"}
    unknown = sorted(set(operation) - allowed_keys)
    if unknown:
        return f"operations[{index}] ({op}): unknown keys {unknown}"
    if op in PATH_OPERATIONS:
        path = operation.get("path")
        if not isinstance(path, list) or not path:
            return f"operations[{index}] ({op}): 'path' must be a non-empty list"
        if len(path) > limits.max_path_segments:
            return (
                f"operations[{index}] ({op}): 'path' has {len(path)} segments, "
                f"at most {limits.max_path_segments} are allowed"
            )
        for segment in path:
            if not isinstance(segment, str) or not segment:
                return (
                    f"operations[{index}] ({op}): every path segment must be "
                    "a non-empty string"
                )
    return None


def validate_extractor_chain(
    extractor: Any, *, family: str | None = None, limits: ExtractionLimits | None = None
) -> str | None:
    """Validate a whole extractor. Returns an error message, or ``None`` if valid.

    Folds one kind through the chain: the source always produces ``text`` (every
    source is a text column or a stringified trace field), each operation must accept
    the kind before it, and the final kind must equal the declared ``output_kind``.
    Checked when the extractor is written, so a wrong extractor is a 422 to the
    author rather than an error on entry 40 of a paid run.
    """
    active = limits or current_limits()
    if not isinstance(extractor, dict):
        return "extractor must be an object"

    unknown = sorted(set(extractor) - {"source", "operations", "output_kind"})
    if unknown:
        return f"unknown extractor keys: {unknown}"

    problem = _source_problem(extractor.get("source"))
    if problem:
        return problem

    output_kind = extractor.get("output_kind")
    if output_kind not in OUTPUT_KINDS:
        return (
            f"unknown output_kind {output_kind!r}; "
            f"expected one of {list(OUTPUT_KINDS)}"
        )

    operations = extractor.get("operations")
    if not isinstance(operations, list):
        return "operations must be a list"
    if len(operations) < active.min_operations:
        return (
            "operations must not be empty: a source with no operations yields raw "
            "text, which no family scores and which an absent extractor already "
            "expresses"
        )
    if len(operations) > active.max_operations:
        return (
            f"operations has {len(operations)} entries, at most "
            f"{active.max_operations} are allowed"
        )

    for index, operation in enumerate(operations):
        problem = _operation_problem(operation, index, active)
        if problem:
            return problem

    # Fold the kinds. ``None`` means "unknown until run time" (an object or an
    # array), so it satisfies any later `accepts` statically and is checked
    # against the real value at run time as `type_mismatch`.
    current: str | None = TEXT
    for index, operation in enumerate(operations):
        op = str(operation["op"])
        accepts, produces = _CATALOG[op]
        if current is not None and current != accepts:
            return (
                f"operations[{index}] ({op}) accepts {accepts} but receives "
                f"{current}"
            )
        current = produces

    if current is not None and current != output_kind:
        return (
            f"the chain produces {current} but output_kind declares {output_kind}"
        )

    if family is not None:
        return validate_output_kind_for_family(str(output_kind), family)
    return None


__all__ = [
    "AGENT_OUTPUT",
    "ARRAY",
    "COLLECT_KEYS",
    "DEDUPE_SORT",
    "FIND_JSON",
    "FLATTEN",
    "KINDS",
    "LABEL_SET",
    "MAX_TOOL_NAME_CHARS",
    "OBJECT",
    "OBJECT_KEYS",
    "OBJECT_VALUES",
    "OPERATIONS",
    "OUTPUT_KINDS",
    "PARSE_JSON",
    "PATH_OPERATIONS",
    "SELECT_PATH",
    "SOURCE_KINDS",
    "TEXT",
    "TOOL_INPUT",
    "TOOL_OUTPUT",
    "TOOL_SOURCE_KINDS",
    "supported_output_kinds",
    "validate_extractor_chain",
    "validate_output_kind_for_family",
]
