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

"""STIX_BUNDLE — STIX bundle-envelope validity (shallow structural check).

Reports the shared structural check of
:mod:`evaluation_metrics.quality.families.stix_bundle.envelope`, the one the entity and
relationship metrics use, so a bundle rejected here is never scored by them.

It MUST NEVER be presented as complete STIX 2.1 validity, which ``stix.semantic_validity``
checks through the :class:`StixValidator` protocol.
"""

from app.evaluation_metrics.quality.families.stix_bundle.envelope import (
    BundleStructureError,
    extract_bundle_objects,
)
from app.evaluation_metrics.quality.primitives.pass_fail import pass_fail_counts
from app.evaluation_metrics.quality.results import (
    JSONValue,
    MetricDirection,
    MetricMetadata,
    MetricRollup,
    MetricStatus,
    ScoreResult,
)

_META = MetricMetadata(
    key="stix.envelope_validity",
    label="Readable Bundle",
    kind="validity",
    primary_measure="valid",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.pooled,
)


def stix_envelope_validity(prediction: object) -> ScoreResult:
    """Score whether ``prediction`` is a well-formed STIX bundle envelope.

    Args:
        prediction: The candidate bundle.

    Returns:
        ``scored``/100 for a JSON object with ``type == "bundle"`` and a list of
        ``objects`` each carrying a string ``type`` and ``id``; otherwise ``scored``/0, so
        a malformed answer counts in the run aggregate where ``invalid`` would vanish.
        The reason goes in ``detail``: a ``scored`` result may not carry an
        ``error``.

    Note:
        A shallow structural check, never complete STIX 2.1 validity; see
        ``stix_semantic_validity``.
    """
    try:
        objects = extract_bundle_objects(prediction)
    except BundleStructureError as exc:
        detail: dict[str, JSONValue] = {
            "reason": "prediction_not_well_formed",
            "error_code": "stix_invalid",
            "error_message": exc.message,
            "locations": list(exc.locations),
            **pass_fail_counts(False),
        }
        return ScoreResult(
            status=MetricStatus.scored, value=0.0, detail=detail, metadata=_META
        )
    detail = {"object_count": len(objects), **pass_fail_counts(True)}
    return ScoreResult(
        status=MetricStatus.scored, value=100.0, detail=detail, metadata=_META
    )


__all__ = ["stix_envelope_validity"]
