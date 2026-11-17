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

"""STIX_BUNDLE — complete STIX 2.1 semantic validity via a typed validator protocol.

Validation is delegated to a caller-supplied :class:`StixValidator` backend
(``backends.stix_validator`` is the standards-based one). With no backend the metric
returns ``unavailable``/``None`` with a diagnostic reason, never ``invalid``, so a missing
backend is not a model failure.
"""

from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from app.evaluation_metrics.quality.primitives.pass_fail import pass_fail_counts
from app.evaluation_metrics.quality.results import (
    JSONValue,
    MetricDirection,
    MetricError,
    MetricMetadata,
    MetricRollup,
    MetricStatus,
    ScoreResult,
)

_META = MetricMetadata(
    key="stix.semantic_validity",
    label="Valid STIX 2.1",
    kind="validity",
    primary_measure="valid",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.pooled,
)


def _empty_findings() -> list[MetricError]:
    return []


class StixValidationReport(BaseModel):
    """Structured result of complete STIX 2.1 validation.

    ``valid`` is the overall verdict; ``findings`` are deterministically ordered,
    actionable validation errors (empty when ``valid`` is ``True``).
    """

    model_config = ConfigDict(frozen=True)

    valid: bool
    findings: list[MetricError] = Field(default_factory=_empty_findings)


@runtime_checkable
class StixValidator(Protocol):
    """A backend that performs complete STIX 2.1 semantic validation.

    Distinct from the shallow envelope check and from canonicalization.
    """

    def validate(self, bundle: object) -> StixValidationReport:
        """Perform complete STIX 2.1 semantic validation.

        Args:
            bundle: The candidate bundle.

        Returns:
            A structured report with the overall verdict and deterministically ordered
            findings.
        """
        ...


def stix_semantic_validity(
    prediction: object,
    validator: StixValidator | None = None,
) -> ScoreResult:
    """Score complete STIX 2.1 semantic validity through a ``validator`` backend.

    Args:
        prediction: The candidate bundle.
        validator: The backend performing complete STIX 2.1 validation, or ``None``.

    Returns:
        ``scored``/100 when the validator reports the bundle valid; ``scored``/0 when it
        does not, so the failure counts in the run aggregate where ``invalid`` would
        vanish; ``unavailable``/``None`` with a diagnostic reason when no backend
        is given.

    Note:
        The failure reason goes in ``detail``: a ``scored`` result may not carry an
        ``error``. ``detail['findings']`` keeps each finding's own ``code``, ``message``
        and ``locations``.
    """
    if validator is None:
        detail: dict[str, JSONValue] = {
            "reason": "no_validator_backend",
            "message": (
                "complete STIX 2.1 semantic validation requires a StixValidator "
                "backend; supply one explicitly"
            ),
        }
        return ScoreResult(
            status=MetricStatus.unavailable,
            value=None,
            detail=detail,
            metadata=_META,
        )

    report = validator.validate(prediction)
    if report.valid:
        return ScoreResult(
            status=MetricStatus.scored,
            value=100.0,
            detail=pass_fail_counts(True),
            metadata=_META,
        )

    # Sort so a backend's incidental finding order cannot change the emitted result.
    findings = sorted(
        report.findings,
        key=lambda finding: (
            finding.code,
            finding.message,
            tuple(finding.locations),
        ),
    )
    locations: list[str] = [
        location for finding in findings for location in finding.locations
    ]
    detail = {
        "reason": "semantic_validation_failed",
        "error_code": "stix_invalid",
        "error_message": f"{len(findings)} STIX validation finding(s)",
        "locations": locations,
        "findings": [
            {
                "code": finding.code,
                "message": finding.message,
                "locations": list(finding.locations),
            }
            for finding in findings
        ],
        **pass_fail_counts(False),
    }
    return ScoreResult(
        status=MetricStatus.scored, value=0.0, detail=detail, metadata=_META
    )


__all__ = [
    "StixValidationReport",
    "StixValidator",
    "stix_semantic_validity",
]
