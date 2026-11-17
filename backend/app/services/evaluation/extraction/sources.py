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

"""Resolving an extractor's source from persisted execution material.

The last-successful-call rule is fixed and not configurable. The trace writer appends
in execution order, so "last" is list order, with no sort key or tie-break. Two facts
of the trace shape follow:

* ``tool_input`` / ``tool_output`` are stringified when the trace is written, which
  is why parsing is an explicit operation in the chain rather than implicit here;
* the span-reconstructed fallback writes neither ``tool_input`` nor ``outcome``, so
  a missing outcome counts as not successful (fail-closed): scoring material whose
  success was never recorded would be worse than refusing it.
"""

from __future__ import annotations

from typing import Any

from .failures import SOURCE_NOT_PRESENT, TYPE_MISMATCH, ExtractionFailure
from .spec import AGENT_OUTPUT, TOOL_INPUT, TOOL_OUTPUT

# Outcome values that count as a successful call. Anything else, an absent outcome
# included, does not.
_SUCCESS_OUTCOMES = frozenset({"success", "succeeded", "ok"})


def _is_successful(trace: dict[str, Any]) -> bool:
    outcome = trace.get("outcome")
    return isinstance(outcome, str) and outcome.strip().lower() in _SUCCESS_OUTCOMES


def last_successful_call(
    traces: object, tool_name: str
) -> dict[str, Any] | None:
    """The last successful call to ``tool_name``, or ``None``."""
    if not isinstance(traces, list):
        return None
    found: dict[str, Any] | None = None
    for entry in traces:
        if not isinstance(entry, dict):
            continue
        if entry.get("type") != "tool" or entry.get("tool_name") != tool_name:
            continue
        if _is_successful(entry):
            found = entry
    return found


def resolve_source(
    source: dict[str, Any],
    agent_output: str | None,
    execution_details: object,
) -> tuple[str | None, ExtractionFailure | None]:
    """Resolve the text an extractor reads, or explain why it is not there."""
    kind = source.get("kind")

    if kind == AGENT_OUTPUT:
        if not agent_output or not agent_output.strip():
            return None, ExtractionFailure(
                reason=SOURCE_NOT_PRESENT,
                message="the agent produced no answer to extract from",
            )
        return agent_output, None

    # A kind the catalog does not know is a configuration error, not missing
    # material: reporting it as source_not_present would send the author to
    # inspect traces for a problem that is in their extractor.
    if kind not in (TOOL_OUTPUT, TOOL_INPUT):
        return None, ExtractionFailure(
            reason=TYPE_MISMATCH,
            message=(
                f"unknown source kind {kind!r}; expected one of "
                f"{[AGENT_OUTPUT, TOOL_OUTPUT, TOOL_INPUT]}"
            ),
            configuration=True,
        )

    tool_name = str(source.get("tool_name") or "")
    traces = (
        execution_details.get("traces")
        if isinstance(execution_details, dict)
        else execution_details
    )
    call = last_successful_call(traces, tool_name)
    if call is None:
        return None, ExtractionFailure(
            reason=SOURCE_NOT_PRESENT,
            message=(
                f"no successful call to '{tool_name}' was recorded for this entry"
            ),
        )

    field = "tool_output" if kind == TOOL_OUTPUT else "tool_input"
    payload = call.get(field)
    if not isinstance(payload, str) or not payload.strip():
        return None, ExtractionFailure(
            reason=SOURCE_NOT_PRESENT,
            message=(
                f"the last successful call to '{tool_name}' recorded no {field}"
            ),
        )
    return payload, None


__all__ = ["last_successful_call", "resolve_source"]
