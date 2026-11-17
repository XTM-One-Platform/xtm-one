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

"""Conformance of a dataset's expected answers to its declared answer shape.

The dataset-level ``output_schema`` states the shape every expected answer is written to.
It is checked against the ground truth at write time, not discovered at scoring time: a
schema that contradicts its answers makes every entry score badly for a reason unrelated
to the agent.

Enforced in BOTH directions: saving a schema checks every entry, and saving an entry
checks it against the schema. Checking only the first lets an answer edit silently break
the shape the dataset still claims.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator

# Through the adapter seam: TID251 bans importing the metric suite from platform
# code, and the seam keeps the pointer format identical to the scorer's.
from app.services.evaluation.metric_package.quality_adapter import pointer_from_tokens

__all__ = [
    "ConformanceFailure",
    "check_expected_answers",
    "first_conformance_failure",
    "needs_expected_answers",
]


@dataclass(frozen=True, slots=True)
class ConformanceFailure:
    """One expected answer that does not match the declared shape.

    ``pointer`` is the JSON Pointer *inside the answer*, not inside the schema: the author
    needs to know which part of their answer is wrong.
    """

    entry_id: str
    pointer: str
    message: str

    def as_detail(self) -> str:
        location = self.pointer or "/"
        return (
            f"Expected answer of entry {self.entry_id} does not match the declared "
            f"answer shape at {location}: {self.message}"
        )


def _json_definition(output_schema: Any) -> dict | None:
    """The JSON Schema inside the wrapper, or ``None`` for the other schema types.

    ``regex`` and ``tool_sequence`` describe the agent's *output*, not the shape of a
    structured answer, so an expected answer has nothing to conform to and both are
    passed over.
    """
    if not isinstance(output_schema, dict):
        return None
    if output_schema.get("type") != "json_schema":
        return None
    definition = output_schema.get("definition")
    return definition if isinstance(definition, dict) else None


def needs_expected_answers(output_schema: Any) -> bool:
    """Whether checking this schema requires reading the dataset's expected answers.

    Asking first spares callers a full read of every entry on a save that
    `check_expected_answers` would answer with `[]`. Derived from `_json_definition` so the
    two cannot disagree about which schema types are checked.
    """
    return _json_definition(output_schema) is not None


def check_expected_answers(
    output_schema: Any,
    entries: list[tuple[str, str | None]],
) -> list[ConformanceFailure]:
    """Every expected answer that contradicts ``output_schema``.

    Args:
        output_schema: The dataset's wrapper-shaped schema, or ``None``.
        entries: ``(entry_id, expected_answer)`` pairs.

    Returns:
        One failure per non-conforming entry, ordered as ``entries`` was. An empty list
        means the dataset holds to its declared shape.

    An answer that is not valid JSON is reported too: prose ground truth under a JSON
    shape is a misconfiguration that would otherwise surface in a run as the agent's fault.

    An EMPTY expected answer is passed over: it is a half-written entry, and refusing it
    would block declaring a schema on a dataset still being drafted.
    """
    definition = _json_definition(output_schema)
    if definition is None:
        return []

    validator = Draft202012Validator(definition)
    failures: list[ConformanceFailure] = []

    for entry_id, expected_answer in entries:
        if not (expected_answer or "").strip():
            continue
        try:
            parsed = json.loads(expected_answer or "")
        except (TypeError, ValueError) as exc:
            failures.append(
                ConformanceFailure(entry_id, "", f"expected answer is not valid JSON ({exc})")
            )
            continue

        error = next(iter(validator.iter_errors(parsed)), None)
        if error is not None:
            # RFC 6901 escaping, not a naive join: an unescaped "/" reads as two levels
            # and a "~" makes an invalid escape, pointing the author nowhere.
            path = list(error.absolute_path)
            pointer = pointer_from_tokens(path) if path else ""
            failures.append(ConformanceFailure(entry_id, pointer, error.message))

    return failures


def first_conformance_failure(
    output_schema: Any,
    entries: list[tuple[str, str | None]],
) -> ConformanceFailure | None:
    """The first failure, or ``None``.

    The API reports one failure, not all: a schema wrong for one entry is usually wrong
    for most, and two hundred identical messages hide the one fact the author needs.
    """
    return next(iter(check_expected_answers(output_schema, entries)), None)
