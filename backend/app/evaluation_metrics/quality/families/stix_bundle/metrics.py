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

"""STIX_BUNDLE — STIX entity and relationship precision/recall/F1 over canonical IDs.

``stix_entity_scores`` compares canonical object identifiers (SDOs + SCOs by default),
reporting ``stix.entity_prf`` (micro P/R/F1 with TP/FP/FN/missing/spurious) plus
``stix.entity_prf.by_type`` (a deterministic macro breakdown by STIX object type).
``stix_relationship_scores`` compares canonical ``(source_id, relationship_type,
target_id)`` triples (``stix.relationship_prf``), a sighting counting as ``(sighted_id,
"sighting", where_id)``, one per place it was sighted. The two F1 metrics pool their
counts over a run and the breakdown its per-type counts, so a run reads the F1
over all its objects and the macro F1 over all its types.

The same :class:`Canonicalizer` is applied to both sides, so raw IDs are never compared.
Both bundles go through the shared structural check in
:mod:`evaluation_metrics.quality.families.stix_bundle.envelope`, so a bundle
``stix.envelope_validity`` rejects is never scored here. A missing canonicalizer or a
malformed *reference* raises :class:`ConfigurationError`. A malformed *prediction* yields
``scored``/0 with the reason in ``detail``, so it counts in the run aggregate where
``invalid`` would vanish, and counts as the expected answer would
(``unreadable_answer``).
"""

from collections.abc import Mapping

from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality.families.stix_bundle.canonical import (
    Canonicalizer,
    StixObjectTypeSelection,
)
from app.evaluation_metrics.quality.families.stix_bundle.envelope import (
    BundleStructureError,
    StixObject,
    extract_bundle_objects,
)
from app.evaluation_metrics.quality.primitives.aggregate import macro_average
from app.evaluation_metrics.quality.primitives.empty_answer import (
    pooled_f1_counts,
    prf_with_empty_answer,
)
from app.evaluation_metrics.quality.primitives.prf import precision_recall_f1
from app.evaluation_metrics.quality.primitives.sets import DiagnosticSet, set_diagnostics
from app.evaluation_metrics.quality.results import (
    MACRO_CLASSES,
    JSONValue,
    MetricDirection,
    MetricMetadata,
    MetricRollup,
    MetricStatus,
    ScoreResult,
)
from app.evaluation_metrics.quality.unreadable import unreadable_answer

_ENTITY_META = MetricMetadata(
    key="stix.entity_prf",
    label="Entity F1",
    kind="prf",
    primary_measure="f1",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.pooled,
)
_BY_TYPE_META = MetricMetadata(
    key="stix.entity_prf.by_type",
    label="Entity F1, Averaged per Entity Type",
    kind="aggregate",
    primary_measure="f1",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.macro,
)
_RELATIONSHIP_META = MetricMetadata(
    key="stix.relationship_prf",
    label="Relationship F1",
    kind="prf",
    primary_measure="f1",
    direction=MetricDirection.higher_is_better,
    rollup=MetricRollup.pooled,
)

_RelationshipTriple = tuple[str, str, str]


def _require_canonicalizer(canonicalizer: Canonicalizer | None) -> Canonicalizer:
    if canonicalizer is None:
        raise ConfigurationError(
            "STIX scoring requires a Canonicalizer; canonicalization must be applied "
            "identically to reference and prediction (raw IDs are never compared)"
        )
    return canonicalizer


def _canonical_objects(
    bundle: object, canonicalizer: Canonicalizer, *, side: str
) -> list[StixObject]:
    """Canonicalize ``bundle`` and resolve it through the shared structural check.

    A malformed *reference* raises ``ConfigurationError``; a malformed *prediction*
    re-raises ``BundleStructureError`` for the metric to score 0.
    """
    try:
        return extract_bundle_objects(canonicalizer.canonicalize(bundle))
    except BundleStructureError as exc:
        if side == "reference":
            raise ConfigurationError(
                "reference STIX bundle is malformed after canonicalization: "
                f"{exc.message}"
            ) from None
        raise


def _entity_ids(
    objects: list[StixObject], selection: StixObjectTypeSelection
) -> list[str]:
    return [obj.id for obj in objects if selection.includes(obj.type)]


def _diag_detail[T](diag: DiagnosticSet[T], f1: float) -> dict[str, JSONValue]:
    precision, recall, _ = prf_with_empty_answer(diag.tp, diag.fp, diag.fn)
    return {
        "precision": precision * 100.0,
        "recall": recall * 100.0,
        "f1": f1 * 100.0,
        "tp": diag.tp,
        "fp": diag.fp,
        "fn": diag.fn,
        **pooled_f1_counts(diag.tp, diag.fp, diag.fn),
    }


def _malformed_detail(exc: BundleStructureError) -> dict[str, JSONValue]:
    """Why a malformed prediction scored 0: a ``scored`` result may not carry an
    ``error``, so the cause goes in ``detail``."""
    return {
        "reason": "prediction_not_well_formed",
        "error_code": "stix_invalid",
        "error_message": f"malformed STIX prediction: {exc.message}",
        "locations": list(exc.locations),
    }


def _ill_formed_pair(
    expected: Mapping[str, ScoreResult], exc: BundleStructureError
) -> dict[str, ScoreResult]:
    """A malformed prediction zeroes both entity metrics, counted as the expected
    answer would (``unreadable_answer``).

    Unparseable output is a finding about the agent, not an inability to assess it, so
    both keys are ``scored``/0.0 and stay in the run aggregate's denominator.
    """
    detail = _malformed_detail(exc)
    return {key: unreadable_answer(result, detail) for key, result in expected.items()}


def _by_type_result(
    ref_objects: list[StixObject],
    pred_objects: list[StixObject],
    selection: StixObjectTypeSelection,
) -> ScoreResult:
    types = sorted(
        {
            obj.type
            for obj in (*ref_objects, *pred_objects)
            if selection.includes(obj.type)
        }
    )
    per_type: dict[str, DiagnosticSet[str]] = {}
    for stix_type in types:
        ref_ids = [obj.id for obj in ref_objects if obj.type == stix_type]
        pred_ids = [obj.id for obj in pred_objects if obj.type == stix_type]
        per_type[stix_type] = set_diagnostics(ref_ids, pred_ids)

    # No entity on either side is an empty answer, correct.
    precision, recall, f1 = macro_average(per_type) if per_type else (1.0, 1.0, 1.0)
    detail: dict[str, JSONValue] = {
        "precision": precision * 100.0,
        "recall": recall * 100.0,
        "f1": f1 * 100.0,
        MACRO_CLASSES: {
            stix_type: {
                "tp": diag.tp,
                "fp": diag.fp,
                "fn": diag.fn,
                "f1": precision_recall_f1(diag.tp, diag.fp, diag.fn)[2] * 100.0,
            }
            for stix_type, diag in per_type.items()
        },
    }
    return ScoreResult(
        status=MetricStatus.scored,
        value=f1 * 100.0,
        detail=detail,
        metadata=_BY_TYPE_META,
    )


def stix_entity_scores(
    reference: object,
    prediction: object,
    canonicalizer: Canonicalizer | None = None,
    selection: StixObjectTypeSelection | None = None,
) -> Mapping[str, ScoreResult]:
    """Score STIX entities over canonical identifiers behind stable metric keys.

    Args:
        reference: The reference STIX bundle.
        prediction: The predicted STIX bundle.
        canonicalizer: The backend applied identically to both sides. Omitting it (or
            passing ``None``) is invalid caller configuration.
        selection: Optional object-type selection policy; defaults to all SDOs and SCOs.

    Returns:
        A deterministically ordered mapping with ``stix.entity_prf`` (micro P/R/F1 plus
        TP/FP/FN and deterministically ordered missing/spurious canonical IDs) and
        ``stix.entity_prf.by_type`` (macro breakdown by STIX object type). A malformed
        prediction yields ``scored``/0 for both keys.

    Raises:
        ConfigurationError: If ``canonicalizer`` is missing, or the reference bundle is
            malformed after canonicalization.
    """
    resolved = _require_canonicalizer(canonicalizer)
    selection = selection if selection is not None else StixObjectTypeSelection()

    ref_objects = _canonical_objects(reference, resolved, side="reference")
    try:
        pred_objects = _canonical_objects(prediction, resolved, side="prediction")
    except BundleStructureError as exc:
        return _ill_formed_pair(_entity_results(ref_objects, ref_objects, selection), exc)
    return _entity_results(ref_objects, pred_objects, selection)


def _entity_results(
    ref_objects: list[StixObject],
    pred_objects: list[StixObject],
    selection: StixObjectTypeSelection,
) -> dict[str, ScoreResult]:
    ref_ids = _entity_ids(ref_objects, selection)
    pred_ids = _entity_ids(pred_objects, selection)
    diag = set_diagnostics(ref_ids, pred_ids)
    _, _, f1 = prf_with_empty_answer(diag.tp, diag.fp, diag.fn)
    detail = _diag_detail(diag, f1)
    detail["missing"] = list(diag.missing)
    detail["spurious"] = list(diag.spurious)

    entity = ScoreResult(
        status=MetricStatus.scored,
        value=f1 * 100.0,
        detail=detail,
        metadata=_ENTITY_META,
    )
    by_type = _by_type_result(ref_objects, pred_objects, selection)
    return {entity.metadata.key: entity, by_type.metadata.key: by_type}


def _relationship_triples(
    objects: list[StixObject],
) -> list[_RelationshipTriple]:
    """Extract canonical ``(source_id, relationship_type, target_id)`` triples.

    A sighting is a relationship too: ``(sighted_id, "sighting", where_id)`` for
    each place it was sighted, ``""`` for the place when it names none.

    Explicit checks, not ``assert``, which ``python -O`` strips: a malformed relationship
    would silently produce a bogus triple.
    """
    triples: list[_RelationshipTriple] = []
    for obj in objects:
        if obj.type == "relationship":
            source, relationship_type, target = (
                _string_field(obj, name) for name in ("source_ref", "relationship_type", "target_ref")
            )
            triples.append((source, relationship_type, target))
        elif obj.type == "sighting":
            sighted = _string_field(obj, "sighting_of_ref")
            places = obj.data.get("where_sighted_refs")
            if places is None or places == []:
                places = [""]
            if not isinstance(places, list) or not all(isinstance(place, str) for place in places):
                raise BundleStructureError(
                    "sighting 'where_sighted_refs' must be a list of strings",
                    [f"{obj.location}/where_sighted_refs"],
                )
            triples.extend((sighted, "sighting", place) for place in places)
    return triples


def _string_field(obj: StixObject, name: str) -> str:
    field = obj.data.get(name)
    if not isinstance(field, str):
        raise BundleStructureError(
            f"{obj.type} must have a string '{name}'", [f"{obj.location}/{name}"]
        )
    return field


def stix_relationship_scores(
    reference: object,
    prediction: object,
    canonicalizer: Canonicalizer | None = None,
) -> ScoreResult:
    """Score STIX relationships and sightings over canonical triples (``stix.relationship_prf``).

    Args:
        reference: The reference STIX bundle.
        prediction: The predicted STIX bundle.
        canonicalizer: The backend applied identically to both sides; it rewrites source
            and target references. Omitting it (or passing ``None``) is invalid caller
            configuration.

    Returns:
        ``scored`` with the triple-set F1 on a 0-100 scale over canonical
        ``(source_id, relationship_type, target_id)`` triples, or ``scored``/0
        when the prediction is malformed.

    Raises:
        ConfigurationError: If ``canonicalizer`` is missing, or the reference bundle is
            malformed after canonicalization.
    """
    resolved = _require_canonicalizer(canonicalizer)

    ref_objects = _canonical_objects(reference, resolved, side="reference")
    try:
        ref_triples = _relationship_triples(ref_objects)
    except BundleStructureError as exc:
        raise ConfigurationError(
            f"reference STIX bundle is malformed after canonicalization: {exc.message}"
        ) from None
    try:
        pred_objects = _canonical_objects(prediction, resolved, side="prediction")
        pred_triples = _relationship_triples(pred_objects)
    except BundleStructureError as exc:
        # `scored`/0.0, never `invalid`/0, which the run aggregate would drop.
        return unreadable_answer(
            _relationship_result(ref_triples, ref_triples), _malformed_detail(exc)
        )
    return _relationship_result(ref_triples, pred_triples)


def _relationship_result(
    ref_triples: list[_RelationshipTriple], pred_triples: list[_RelationshipTriple]
) -> ScoreResult:
    diag = set_diagnostics(ref_triples, pred_triples)
    _, _, f1 = prf_with_empty_answer(diag.tp, diag.fp, diag.fn)
    detail = _diag_detail(diag, f1)
    detail["missing"] = [list(triple) for triple in diag.missing]
    detail["spurious"] = [list(triple) for triple in diag.spurious]
    return ScoreResult(
        status=MetricStatus.scored,
        value=f1 * 100.0,
        detail=detail,
        metadata=_RELATIONSHIP_META,
    )


def stix_expected_scores(
    reference: object, canonicalizer: Canonicalizer | None = None,
    selection: StixObjectTypeSelection | None = None,
) -> dict[str, ScoreResult]:
    """The three STIX metrics for the reference given as the answer: what an answer that
    cannot be read is counted against (``unreadable_answer``).

    Raises:
        ConfigurationError: As :func:`stix_entity_scores` and
            :func:`stix_relationship_scores` for the reference.
    """
    results = dict(stix_entity_scores(reference, reference, canonicalizer, selection))
    relationship = stix_relationship_scores(reference, reference, canonicalizer)
    results[relationship.metadata.key] = relationship
    return results


__all__ = ["stix_entity_scores", "stix_expected_scores", "stix_relationship_scores"]
