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

"""The write-time guard on an author-supplied ``output_schema``.

Scoring an output against the schema belongs to the metric package
(``json.field_accuracy``); a second scorer here would be a competing definition of
the same number.

Only ``json_schema`` is accepted: ``regex`` and ``tool_sequence`` feed no score,
so saving one would silently do nothing at run time. Stored rows of those types are left
as they are; they can no longer be created or edited.

This guard checks that a schema an author just typed is well-formed **before** it is
stored. It screens every ``pattern`` in the document for catastrophic-backtracking (ReDoS)
shapes and bounds the inspection in wall-clock time; otherwise saving a dataset would be a
CPU-exhaustion primitive any author can reach.

Fail-closed throughout: anything malformed, hostile or slow comes back as an error message
the caller turns into a 422, never as an exception and never as silent acceptance.
"""

from __future__ import annotations

import re
import threading
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

# Wall-clock budget (seconds) for inspecting one author-supplied schema: `check_schema`
# and the pattern walk run over a document of user-controlled depth.
VALIDATION_TIMEOUT_SECONDS = 2.0


def _run_with_timeout(fn, timeout: float):
    """Run ``fn`` in a daemon thread; return ``(ok, result)``.

    ``ok`` is ``False`` if the call exceeded ``timeout`` (the thread is
    abandoned as a daemon) or raised. On success ``result`` is the return value;
    on failure it is the raised exception (or ``None`` on timeout).
    """
    box: dict[str, Any] = {}

    def _target():
        try:
            box["result"] = fn()
        except Exception as exc:  # pragma: no cover - defensive
            box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return False, None
    if "error" in box:
        return False, box["error"]
    return True, box.get("result")


def _looks_redos_prone(pattern: str) -> bool:
    """Heuristically flag regex patterns prone to catastrophic backtracking.

    Detects nested quantifiers such as ``(a+)+``, ``(a*)*``, ``(.*)*`` and
    quantified alternations like ``(a|aa)+`` — the classic ReDoS shapes. Errs on
    the side of rejecting (fail-closed) rather than risking an exponential match.
    """
    # A group whose body ends in a quantifier, immediately followed by another
    # quantifier applied to the group: (…+)+ / (…*)* / (…+)* etc.
    if re.search(r"\([^)]*[+*][^)]*\)\s*[+*]", pattern):
        return True
    # Quantified group containing an alternation: (a|aa)+ / (a|a?)* …
    return bool(re.search(r"\([^)]*\|[^)]*\)\s*[+*]", pattern))


def _find_redos_pattern(schema: Any, pointer: str) -> str | None:
    """The JSON Pointer of the first backtracking-prone ``pattern``, or ``None``.

    Walks the whole document: a nested `pattern` runs on the same input as one at the
    root.
    """
    if isinstance(schema, dict):
        candidate = schema.get("pattern")
        if isinstance(candidate, str) and _looks_redos_prone(candidate):
            return f"{pointer}/pattern" if pointer else "/pattern"
        for key, value in schema.items():
            found = _find_redos_pattern(value, f"{pointer}/{key}")
            if found is not None:
                return found
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            found = _find_redos_pattern(value, f"{pointer}/{index}")
            if found is not None:
                return found
    return None




def _inspect_json_schema(definition: dict) -> str | None:
    """Well-formedness + ReDoS screen for one JSON Schema. Error message, or ``None``."""
    try:
        Draft202012Validator.check_schema(definition)
    except SchemaError as exc:
        return f"invalid JSON Schema: {exc.message}"
    # `pattern` is a regex the validator runs against agent output on every entry of
    # every run, so it gets a ReDoS pre-screen.
    hostile = _find_redos_pattern(definition, "")
    if hostile is not None:
        return (
            f"invalid JSON Schema: pattern at {hostile} rejected: "
            "catastrophic-backtracking (ReDoS) risk"
        )
    return None


def validate_output_schema(output_schema: dict | None) -> str | None:
    """Validate an ``output_schema`` definition for API acceptance.

    Returns an error message string if the schema is malformed, or ``None`` if
    it is valid (or ``None``/empty). Callers raise HTTP 422 on a non-None
    result, so a broken schema is rejected at write time rather than silently
    failing every run.

    ``json_schema`` is the only accepted type. Every write path
    (dataset-level and per-entry) calls this function, so it is the one place
    the other types are refused.
    """
    if not output_schema:
        return None
    if not isinstance(output_schema, dict):
        return "output_schema must be an object"
    schema_type = output_schema.get("type")
    definition = output_schema.get("definition")
    if schema_type != "json_schema":
        return "output_schema.type must be 'json_schema'"
    if definition is None:
        return "output_schema.definition is required"

    if not isinstance(definition, dict):
        return "json_schema definition must be an object"
    # Both steps walk a document whose depth and size the author controls, and any
    # author reaches this by saving a dataset, so the pair is time-bounded.
    ok, result = _run_with_timeout(
        lambda: _inspect_json_schema(definition), VALIDATION_TIMEOUT_SECONDS
    )
    if not ok:
        return (
            "invalid JSON Schema: inspection exceeded "
            f"{VALIDATION_TIMEOUT_SECONDS:g}s and was rejected"
        )
    return result


