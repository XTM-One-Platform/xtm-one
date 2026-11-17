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

"""The one entry point scoring goes through, with or without an extractor.

Both engine call sites (a fresh run and a re-score) use this instead of
``run_quality`` directly, so they cannot drift apart on how extraction is assembled.
They differ only in where their inputs come from, which is a parameter.

It returns the metrics AND the prediction they were computed from: with an
extractor configured the two differ, and only this seam holds both.

On failure it never scores the raw output: raw markdown handed to the JSON family would
score a genuine ``0.0``, indistinguishable from an agent that answered badly. It returns
the ``extraction.failed`` blob and the family's metrics, scored 0 with the reason when the
answer caused the failure, left out with it otherwise.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.services.evaluation.metric_package import (
    QualityConfig,
    failed_answer_outcomes,
    run_quality,
    to_persistable,
    unscorable_outcomes,
)

from .engine import apply_extractor
from .failures import (
    EXTRACTION_FAILED_KEY,
    FAMILY_INCOMPATIBLE,
    ExtractionFailure,
    failure_metric_blob,
)
from .record import ExtractedPrediction
from .sources import resolve_source
from .spec import validate_output_kind_for_family

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScoredEntry:
    """One entry's metrics, and the prediction they were computed from.

    ``extracted`` is ``None`` whenever there is nothing an author could not already
    see: no extractor (the prediction IS the raw output), or an extraction that
    failed (the failure notice is the disclosure, and there is no value to show).
    """

    metric_results: dict[str, dict[str, Any]] = field(default_factory=dict)
    extracted: ExtractedPrediction | None = None


def quality_with_extraction(
    *,
    extractor: dict[str, Any] | None,
    family: str,
    reference: object,
    agent_output: str,
    execution_details: object,
    output_schema: Mapping[str, Any] | None,
    config: QualityConfig,
    agent_id: str | None = None,
) -> ScoredEntry:
    """Quality metrics for one entry, extracting the prediction first if configured.

    ``reference`` is passed through untouched on every path: extraction is
    prediction-side only.
    """
    if not extractor:
        return ScoredEntry(
            metric_results=dict(
                to_persistable(
                    run_quality(
                        family=family,
                        reference=reference,
                        prediction=agent_output,
                        output_schema=output_schema,
                        config=config,
                    )
                )
            )
        )

    def failed(failure: ExtractionFailure) -> ScoredEntry:
        return _failed(
            failure,
            family=family,
            reference=reference,
            output_schema=output_schema,
            config=config,
            agent_id=agent_id,
        )

    # Re-checked here, not only at save time: the evaluation's family can change
    # after the extractor was authored, so a pair valid when written can break.
    incompatible = validate_output_kind_for_family(
        str(extractor.get("output_kind", "")), family
    )
    if incompatible:
        return failed(ExtractionFailure(reason=FAMILY_INCOMPATIBLE, message=incompatible))

    source_text, failure = resolve_source(
        dict(extractor.get("source") or {}), agent_output, execution_details
    )
    if failure is not None:
        return failed(failure)

    outcome = apply_extractor(extractor, source_text or "")
    if outcome.failure is not None:
        return failed(outcome.failure)

    prediction = outcome.prediction or ""
    return ScoredEntry(
        metric_results=dict(
            to_persistable(
                run_quality(
                    family=family,
                    reference=reference,
                    prediction=prediction,
                    output_schema=output_schema,
                    config=config,
                )
            )
        ),
        extracted=ExtractedPrediction(
            prediction=prediction,
            output_kind=str(extractor.get("output_kind") or "") or None,
        ),
    )


def _failed(
    failure: ExtractionFailure,
    *,
    family: str,
    reference: object,
    output_schema: Mapping[str, Any] | None,
    config: QualityConfig,
    agent_id: str | None,
) -> ScoredEntry:
    """The failure's record, and the family's metrics: 0 when the answer caused it, else
    left out with its reason."""
    message = f"Extraction failed: {failure.message}"
    blob = failure_metric_blob(failure, agent_id=agent_id)
    if failure.answer_at_fault:
        blob.update(
            failed_answer_quality(
                family=family,
                reference=reference,
                output_schema=output_schema,
                config=config,
                reason="prediction_not_scorable",
                message=message,
            )
        )
    else:
        blob.update(to_persistable(unscorable_outcomes(family, "extraction_failed", message)))
    return ScoredEntry(metric_results=blob)


def failed_answer_quality(
    *,
    family: str,
    reference: object,
    output_schema: Mapping[str, Any] | None,
    config: QualityConfig,
    reason: str,
    message: str,
) -> dict[str, dict[str, Any]]:
    """The family's quality metrics for an entry the agent failed without an answer to
    score: 0 with the *reason*, counted as the expected answer would be.

    The metrics this entry scores for an answer it can read, the expected one: a metric
    its configuration leaves out (no schema, no STIX backend) stays out. Raises
    ``QualityConfigError`` as ``run_quality`` does for the reference.
    """
    scorable = run_quality(
        family=family,
        reference=reference,
        prediction=reference if isinstance(reference, str) else json.dumps(reference, default=str),
        output_schema=output_schema,
        config=config,
    )
    return to_persistable(failed_answer_outcomes(scorable, reason, message))


def tally_extraction_failure(
    metric_results: Mapping[str, Any], reason_counts: dict[str, int]
) -> None:
    """Count one entry's extraction failure, if it had one. Mutates in place."""
    blob = metric_results.get(EXTRACTION_FAILED_KEY)
    if not isinstance(blob, dict):
        return
    error = blob.get("error")
    reason = str(error.get("reason")) if isinstance(error, dict) else "unknown"
    reason_counts[reason] = reason_counts.get(reason, 0) + 1


def log_extraction_failures(
    *,
    evaluation_id: object,
    run_id: object,
    agent_id: object,
    reason_counts: Mapping[str, int],
) -> None:
    """One line per run. Identifiers and counts only, never a payload."""
    if not reason_counts:
        return
    logger.warning(
        "Extraction failed for %d entries (evaluation %s, run %s, agent %s): %s",
        sum(reason_counts.values()),
        evaluation_id,
        run_id,
        agent_id,
        ", ".join(
            f"{reason}={count}" for reason, count in sorted(reason_counts.items())
        ),
    )


__all__ = [
    "ScoredEntry",
    "log_extraction_failures",
    "quality_with_extraction",
    "tally_extraction_failure",
]
