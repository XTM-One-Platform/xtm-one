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

"""The closed failure taxonomy for prediction extraction.

An extraction that cannot complete produces one of these reasons. One the agent's output
caused scores the family's metrics 0 with its reason, ``limit_exceeded`` included: every
limit measures the output, and an answer the size of an expected one stays far within it.
One the configuration caused (``configuration``, ``family_incompatible``) leaves the entry
out with it, so it never looks like an agent that answered badly.

Every field of the record is bounded metadata. No raw output, tool payload or pasted
sample may appear here, in whole or in part (not truncated, not the first line).
Messages are composed from the reason, the configured tool name or path, and the
kinds involved, never from the value that failed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

# The metric key the failure is recorded under, beside the family's metrics. Not in the
# metric package's registry (walked from ``app/evaluation_metrics``), so it is never
# selectable, aggregated or a headline contributor.
EXTRACTION_FAILED_KEY: Final = "extraction.failed"

SOURCE_NOT_PRESENT: Final = "source_not_present"
PAYLOAD_UNPARSEABLE: Final = "payload_unparseable"
SELECTION_EMPTY: Final = "selection_empty"
TYPE_MISMATCH: Final = "type_mismatch"
LIMIT_EXCEEDED: Final = "limit_exceeded"
LABEL_INVALID: Final = "label_invalid"
FAMILY_INCOMPATIBLE: Final = "family_incompatible"

FAILURE_REASONS: Final[frozenset[str]] = frozenset(
    {
        SOURCE_NOT_PRESENT,
        PAYLOAD_UNPARSEABLE,
        SELECTION_EMPTY,
        TYPE_MISMATCH,
        LIMIT_EXCEEDED,
        LABEL_INVALID,
        FAMILY_INCOMPATIBLE,
    }
)


@dataclass(frozen=True)
class ExtractionFailure:
    """Why an extraction could not produce a prediction. Bounded metadata only."""

    reason: str
    message: str
    operation_index: int | None = None
    operation: str | None = None
    path: str | None = None
    expected_kind: str | None = None
    actual_kind: str | None = None
    # A failure of the extractor's own configuration, not of the agent's output.
    configuration: bool = False

    def __post_init__(self) -> None:
        if self.reason not in FAILURE_REASONS:
            raise ValueError(f"unknown extraction failure reason: {self.reason!r}")

    @property
    def answer_at_fault(self) -> bool:
        """Whether the agent's output caused the failure, so the entry scores 0."""
        return not self.configuration and self.reason != FAMILY_INCOMPATIBLE

    def as_detail(self, *, agent_id: str | None = None) -> dict[str, Any]:
        """The ``error`` object written into the metric blob and the API response."""
        return {
            "reason": self.reason,
            "message": self.message,
            "agent_id": agent_id,
            "operation_index": self.operation_index,
            "operation": self.operation,
            "path": self.path,
            "expected_kind": self.expected_kind,
            "actual_kind": self.actual_kind,
        }


def failure_metric_blob(
    failure: ExtractionFailure, *, agent_id: str | None = None
) -> dict[str, dict[str, Any]]:
    """The ``metric_results`` entry for a failed extraction.

    Mirrors the metric package's ``invalid`` contract (value exactly 0 with a
    structured error), so any reader of the blob can read this. The status also means
    ``extract_metrics`` writes no ``EvaluationMetric`` row for it, a second, independent
    reason a failed entry cannot reach an aggregate.
    """
    return {
        EXTRACTION_FAILED_KEY: {
            "status": "invalid",
            "value": 0,
            "error": failure.as_detail(agent_id=agent_id),
        }
    }


def count_extraction_failures(results_details: object) -> int:
    """Count failed extractions in one already-loaded blob."""
    if not isinstance(results_details, dict):
        return 0
    metric_results = results_details.get("metric_results")
    if not isinstance(metric_results, dict):
        return 0
    return 1 if EXTRACTION_FAILED_KEY in metric_results else 0


__all__ = [
    "EXTRACTION_FAILED_KEY",
    "FAILURE_REASONS",
    "FAMILY_INCOMPATIBLE",
    "LABEL_INVALID",
    "LIMIT_EXCEEDED",
    "PAYLOAD_UNPARSEABLE",
    "SELECTION_EMPTY",
    "SOURCE_NOT_PRESENT",
    "TYPE_MISMATCH",
    "ExtractionFailure",
    "count_extraction_failures",
    "failure_metric_blob",
]
