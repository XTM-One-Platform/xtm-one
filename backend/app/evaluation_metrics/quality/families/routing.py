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

"""Pure, config-driven family routing.

``run_for_family`` runs the metrics of an already-resolved output family and returns a
deterministic mapping from stable metric key to :class:`ScoreResult`. Each entry is keyed
by its own metric metadata, so routed results equal direct invocation under the same key.

The runner imports only in-package family modules: it knows nothing of agents, database
models, traces or persistence, and gets every schema, policy, vocabulary and backend
through :class:`FamilyConfig`. Agent-to-family resolution stays outside the package.

A family missing its required configuration (``PROSE`` without a ``ProseConfig``,
``STRUCTURED_JSON`` without a schema, ...) and an unrecognized family both raise
:class:`ConfigurationError`.
"""

import json
from collections.abc import Mapping, Sequence
from collections.abc import Set as AbstractSet
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality.families.classification import classification_dataset
from app.evaluation_metrics.quality.families.prose import ProseConfig
from app.evaluation_metrics.quality.families.prose.metrics import (
    _EXACT_META as _PROSE_EXACT_META,
)
from app.evaluation_metrics.quality.families.prose.metrics import (
    _FORBIDDEN_META as _PROSE_FORBIDDEN_META,
)
from app.evaluation_metrics.quality.families.prose.metrics import (
    _REQUIRED_META as _PROSE_REQUIRED_META,
)
from app.evaluation_metrics.quality.families.prose.metrics import (
    _ROUGE_L_META as _PROSE_ROUGE_L_META,
)
from app.evaluation_metrics.quality.families.prose.metrics import (
    _TOKEN_PRF_META as _PROSE_TOKEN_PRF_META,
)
from app.evaluation_metrics.quality.families.prose.metrics import (
    prose_exact_match,
    prose_forbidden_phrase_absence,
    prose_required_phrase_coverage,
    prose_rouge_l,
    prose_token_prf,
)
from app.evaluation_metrics.quality.families.stix_bundle import (
    Canonicalizer,
    StixObjectTypeSelection,
    StixValidator,
    stix_entity_scores,
    stix_envelope_validity,
    stix_relationship_scores,
    stix_semantic_validity,
)
from app.evaluation_metrics.quality.families.stix_bundle.alignment import (
    stix_aligned_scores,
    stix_closed_world_scores,
)
from app.evaluation_metrics.quality.families.stix_bundle.envelope import BundleStructureError
from app.evaluation_metrics.quality.families.stix_bundle.metrics import stix_expected_scores
from app.evaluation_metrics.quality.families.structured_json import (
    json_field_accuracy,
    json_schema_validity,
)

# Results must carry the *same* metadata as a direct invocation, so the metric modules' own
# metadata constants are reused rather than restated here.
from app.evaluation_metrics.quality.families.structured_json.field_accuracy import (
    _META as _JSON_FIELD_META,
)
from app.evaluation_metrics.quality.families.structured_json.validity import (
    _SCHEMA_META as _JSON_SCHEMA_META,
)
from app.evaluation_metrics.quality.families.structured_json.validity import (
    json_parse_error,
)
from app.evaluation_metrics.quality.normalization.policies import ComparisonPolicy
from app.evaluation_metrics.quality.results import (
    POOLED_DENOMINATOR,
    POOLED_NUMERATOR,
    JSONValue,
    MetricError,
    MetricStatus,
    ScoreResult,
)
from app.evaluation_metrics.quality.unreadable import unreadable_answer


class Family(StrEnum):
    """An already-resolved output family."""

    STIX_BUNDLE = "STIX_BUNDLE"
    #: Every JSON answer, whatever it describes: one family for a JSON document judged
    #: against a schema, so an author never picks between synonyms. The id is
    #: stable: stored ``evaluations.family`` rows holding it keep resolving.
    STRUCTURED_JSON = "STRUCTURED_JSON"
    PROSE = "PROSE"
    CLASSIFICATION = "CLASSIFICATION"


class FamilyConfig(BaseModel):
    """Typed configuration supplied to the family runner.

    Every field is optional; each family validates the ones it requires and raises
    :class:`ConfigurationError` when a required entry is missing.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    # STRUCTURED_JSON
    json_schema: Mapping[str, JSONValue] | None = None
    comparison_policy: ComparisonPolicy | None = None
    # CLASSIFICATION
    vocabulary: frozenset[str] | None = None
    # STIX_BUNDLE
    canonicalizer: Canonicalizer | None = None
    selection: StixObjectTypeSelection | None = None
    stix_validator: StixValidator | None = None
    # The object types the run's dataset references annotate: with them, the aligned
    # scores are also read in that closed world (``stix_closed_world_scores``).
    reference_types: frozenset[str] | None = None
    # PROSE
    prose: ProseConfig | None = None


def _require[T](value: T | None, family: Family, name: str) -> T:
    if value is None:
        raise ConfigurationError(
            f"family {family.value} requires {name!r} in FamilyConfig"
        )
    return value


def _parse_json_reference(reference: object) -> object:
    """Normalize a STRUCTURED_JSON reference into parsed JSON.

    The reference is caller ground truth, normalized independently of the prediction: raw
    ``str``/``bytes`` is parsed, any other value is taken as already parsed. A malformed
    reference raises :class:`ConfigurationError`, never becomes a score.
    """
    if not isinstance(reference, (str, bytes)):
        return reference
    try:
        return json.loads(reference)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ConfigurationError(
            f"family {Family.STRUCTURED_JSON.value} reference is not valid JSON: {exc}"
        ) from exc


def _ill_formed(expected: ScoreResult, error: MetricError, *, limited: bool = False) -> ScoreResult:
    """A metric zeroed because the answer itself was ill-formed.

    The agent produced something unparseable. That failure counts against it: the result
    is `scored`/0.0 and stays in the run aggregate's denominator, where `invalid` would
    vanish, counted as *expected*, the metric for the reference given as the answer,
    would be (``unreadable_answer``).
    """
    # The cause goes in `detail`: a `scored` result may not carry an `error`, which
    # readers take to mean no number was produced. Here the number is the finding.
    return unreadable_answer(
        expected,
        {
            "reason": "prediction_not_well_formed",
            "error_code": error.code,
            "error_message": error.message,
        },
        limited=limited,
    )


def _run_structured_json(
    reference: object, prediction: object, config: FamilyConfig
) -> dict[str, ScoreResult]:
    schema = _require(config.json_schema, Family.STRUCTURED_JSON, "json_schema")
    policy = config.comparison_policy or ComparisonPolicy()
    # Parsed eagerly so a malformed reference raises instead of being silently mis-scored.
    parsed_reference = _parse_json_reference(reference)
    results: dict[str, ScoreResult] = {}

    # Raw text is parsed here; an already-parsed artifact is taken as is. Text that does
    # not parse is the agent's failure: schema validity and the field score are scored 0
    # off the parse diagnostic, so it counts in the run aggregate where `invalid` would
    # vanish, and the reason tells "not JSON" from "schema violation".
    parsed_prediction = prediction
    if isinstance(prediction, (str, bytes)):
        error = json_parse_error(prediction)
        if error is not None:
            for expected in (
                json_schema_validity(parsed_reference, schema),
                json_field_accuracy(parsed_reference, parsed_reference, policy, schema),
            ):
                # A reference too large to pair with itself has no weight to count the
                # answer against; an unparseable answer is at fault all the same.
                limited = expected.detail.get("reason") == "pairing_input_too_large"
                results[expected.metadata.key] = _ill_formed(expected, error, limited=limited)
            return results
        parsed_prediction = json.loads(prediction)

    # Schema conformance does not gate per-field scoring: one stray key under
    # `additionalProperties: false` would zero an answer whose required fields are all
    # right. Conformance is carried by `json.schema_validity` alone.
    results[_JSON_SCHEMA_META.key] = json_schema_validity(parsed_prediction, schema)

    results[_JSON_FIELD_META.key] = json_field_accuracy(
        parsed_reference, parsed_prediction, policy, schema
    )
    return results


def _run_classification(
    reference: object, prediction: object, config: FamilyConfig
) -> dict[str, ScoreResult]:
    vocabulary = _require(config.vocabulary, Family.CLASSIFICATION, "vocabulary")
    references: Sequence[AbstractSet[str]] = reference  # type: ignore[assignment]
    predictions: Sequence[AbstractSet[str]] = prediction  # type: ignore[assignment]
    return dict(classification_dataset(references, predictions, vocabulary))


def _run_stix_bundle(
    reference: object, prediction: object, config: FamilyConfig
) -> dict[str, ScoreResult]:
    canonicalizer = _require(config.canonicalizer, Family.STIX_BUNDLE, "canonicalizer")
    results: dict[str, ScoreResult] = {}

    envelope = stix_envelope_validity(prediction)
    results[envelope.metadata.key] = envelope
    semantic = stix_semantic_validity(prediction, config.stix_validator)
    results[semantic.metadata.key] = semantic

    # A malformed prediction is `scored`/0.0, not `invalid`/0, which the run aggregate
    # would drop, so the gate is detected by value. `envelope_validity` scores
    # only 100 or 0, so `value == 0.0` means malformed. The dependent metrics give the
    # gate's reason for their zero and count as the expected answer would.
    if envelope.status is MetricStatus.scored and envelope.value == 0.0:
        reason = {
            key: value
            for key, value in envelope.detail.items()
            if key not in (POOLED_NUMERATOR, POOLED_DENOMINATOR)
        }
        expectations = list(stix_expected_scores(reference, canonicalizer, config.selection).values())
        if config.reference_types:
            expectations += _closed_expected(reference, canonicalizer, config)
        for expectation in expectations:
            results[expectation.metadata.key] = unreadable_answer(
                expectation, reason, limited=_too_large(expectation)
            )
        return results

    results.update(
        stix_entity_scores(reference, prediction, canonicalizer, config.selection)
    )
    relationship = stix_relationship_scores(reference, prediction, canonicalizer)
    results[relationship.metadata.key] = relationship
    try:
        # One canonicalization for both readings (the dataset's types computed with them).
        results.update(
            stix_aligned_scores(
                reference, prediction, canonicalizer, config.selection, types=config.reference_types or None
            )
        )
    except BundleStructureError as exc:
        # Malformed only once canonicalized: scored 0 as the exact scores are.
        reason = {"reason": "prediction_not_well_formed", "error_message": exc.message}
        expected = stix_aligned_scores(
            reference, reference, canonicalizer, config.selection, types=config.reference_types or None
        )
        for key, result in expected.items():
            results[key] = unreadable_answer(result, reason, limited=_too_large(result))
    return results


def _closed_expected(reference: object, canonicalizer: Canonicalizer, config: FamilyConfig) -> list[ScoreResult]:
    """The closed-world scores of the reference given as the answer."""
    return list(
        stix_closed_world_scores(reference, reference, config.reference_types or frozenset(), canonicalizer, config.selection).values()
    )


def _too_large(expected: ScoreResult) -> bool:
    """*expected* is not scored only because the reference is too large to align with
    itself: an unreadable answer is at fault all the same and scores 0, as
    structured_json's does, instead of dropping out of the run aggregate."""
    return expected.status is MetricStatus.skipped and expected.detail.get("reason") == "too_many_objects"


def _run_prose(
    reference: object, prediction: object, config: ProseConfig
) -> dict[str, ScoreResult]:
    """Forward the prose configuration to the five metrics, adding no logic of its own.

    Keys follow the fixed documented order, and every ``skipped`` or ``invalid`` decision
    is made inside the metric functions, so routed results equal direct invocation by
    construction.
    """
    policy = config.policy
    return {
        _PROSE_EXACT_META.key: prose_exact_match(reference, prediction, policy),
        _PROSE_TOKEN_PRF_META.key: prose_token_prf(reference, prediction, policy),
        _PROSE_ROUGE_L_META.key: prose_rouge_l(reference, prediction, policy),
        _PROSE_REQUIRED_META.key: prose_required_phrase_coverage(
            prediction, config.required_phrases, policy
        ),
        _PROSE_FORBIDDEN_META.key: prose_forbidden_phrase_absence(
            prediction, config.forbidden_phrases, policy
        ),
    }


def run_for_family(
    reference: object,
    prediction: object,
    family: Family,
    config: FamilyConfig,
) -> Mapping[str, ScoreResult]:
    """Run the metrics applicable to ``family`` and return a stable-key mapping.

    Args:
        reference: The reference artifact in the shape the family expects.
        prediction: The predicted artifact in the shape the family expects.
        family: The already-resolved output family.
        config: Typed per-family configuration (schemas, policies, vocabularies, backends).

    Returns:
        A deterministically ordered mapping from stable metric key to ``ScoreResult``.

    Raises:
        ConfigurationError: If ``family`` is not recognized or required configuration for
            the requested family is missing.
    """
    try:
        resolved = Family(family)
    except ValueError as exc:
        raise ConfigurationError(
            f"unknown Family: {family!r}; expected one of "
            f"{sorted(member.value for member in Family)}"
        ) from exc

    if resolved is Family.PROSE:
        prose = _require(config.prose, Family.PROSE, "prose")
        return _run_prose(reference, prediction, prose)
    if resolved is Family.STRUCTURED_JSON:
        return _run_structured_json(reference, prediction, config)
    if resolved is Family.CLASSIFICATION:
        return _run_classification(reference, prediction, config)
    if resolved is Family.STIX_BUNDLE:
        return _run_stix_bundle(reference, prediction, config)
    # Reached only by a member added to `Family` without a runner: never score it as
    # another family.
    raise ConfigurationError(f"no metrics are implemented for family {resolved.value}")


__all__ = ["Family", "FamilyConfig", "run_for_family"]
