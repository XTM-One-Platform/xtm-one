"""Config-driven family routing.

Verifies that `run_for_family` returns a stable-key -> ScoreResult mapping, that routed
results are byte-identical to direct metric invocation, and that an unknown family id or a
missing or broken configuration (PROSE without a `ProseConfig` included) raises.
"""

from collections.abc import Callable, Mapping

import pytest

from app.evaluation_metrics.core.contracts import JSONValue, MetricStatus
from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.quality.families.classification import classification_dataset
from app.evaluation_metrics.quality.families.prose import ProseConfig
from app.evaluation_metrics.quality.families.routing import Family, FamilyConfig, run_for_family
from app.evaluation_metrics.quality.families.stix_bundle import (
    stix_entity_scores,
    stix_relationship_scores,
)
from app.evaluation_metrics.quality.families.structured_json import (
    json_field_accuracy,
    json_schema_validity,
)
from app.evaluation_metrics.quality.normalization.policies import ComparisonPolicy
from app.evaluation_metrics.quality.normalization.prose import ProseNormalizationPolicy
from app.evaluation_metrics.quality.results import ScoreResult


class _IdentityCanonicalizer:
    def canonicalize(self, bundle: object) -> object:
        return bundle


# --- fixtures ------------------------------------------------------------------------

_SCHEMA: dict[str, JSONValue] = {
    "type": "object",
    "properties": {"name": {"type": "string"}},
    "required": ["name"],
}


def _json_pair() -> tuple[dict[str, object], dict[str, object]]:
    return {"name": "alice", "role": "admin"}, {"name": "alice", "role": "user"}


def _bundle(object_id: str) -> dict[str, object]:
    return {
        "type": "bundle",
        "id": "bundle--1",
        "objects": [
            {"type": "indicator", "id": object_id},
            {
                "type": "relationship",
                "id": "relationship--1",
                "relationship_type": "indicates",
                "source_ref": object_id,
                "target_ref": "malware--1",
            },
            {"type": "malware", "id": "malware--1"},
        ],
    }


# --- family enum ---------------------------------------------------------------------


def test_the_removed_families_are_gone_from_the_enum() -> None:
    """The family enum holds exactly four ids.

    Every JSON document judged against a schema routes to `STRUCTURED_JSON`, whose id stays
    stable so stored `evaluations.family` rows still resolve.
    """
    assert {member.value for member in Family} == {
        "STIX_BUNDLE",
        "STRUCTURED_JSON",
        "PROSE",
        "CLASSIFICATION",
    }


@pytest.mark.parametrize(
    "family",
    ["A", "B", "C", "D", "E", "F", "Z", "DETECTION_RULE", "JSON_ANALYSIS"],
)
def test_an_unknown_or_retired_family_id_raises(family: str) -> None:
    """Retired letters, unknown ids and the families folded into `STRUCTURED_JSON` raise.

    Returning "unavailable" would let the family id silently decide whether an evaluation
    produces any deterministic metric.
    """
    with pytest.raises(ConfigurationError):
        run_for_family({}, {}, family, FamilyConfig())  # type: ignore[arg-type]


# --- every family ----------------------------------------------------------------------


def _json_direct(reference: object, prediction: object) -> dict[str, ScoreResult]:
    return {
        "json.field_accuracy": json_field_accuracy(reference, prediction, ComparisonPolicy()),
        "json.schema_validity": json_schema_validity(prediction, _SCHEMA),
    }


def _stix_direct(reference: object, prediction: object) -> dict[str, ScoreResult]:
    canonicalizer = _IdentityCanonicalizer()
    return {
        **stix_entity_scores(reference, prediction, canonicalizer),
        "stix.relationship_prf": stix_relationship_scores(reference, prediction, canonicalizer),
    }


_LABELS = frozenset({"T1059", "T1566", "T1204"})


@pytest.mark.parametrize(
    ("family", "reference", "prediction", "config", "direct", "every_key"),
    [
        pytest.param(
            Family.STRUCTURED_JSON,
            *_json_pair(),
            FamilyConfig(json_schema=_SCHEMA, comparison_policy=ComparisonPolicy()),
            _json_direct,
            False,
            id="structured-json",
        ),
        pytest.param(
            Family.STIX_BUNDLE,
            _bundle("indicator--1"),
            _bundle("indicator--2"),
            FamilyConfig(canonicalizer=_IdentityCanonicalizer()),
            _stix_direct,
            False,
            id="stix-bundle",
        ),
        pytest.param(
            Family.CLASSIFICATION,
            [{"T1059"}, {"T1566"}],
            [{"T1059"}, {"T1204"}],
            FamilyConfig(vocabulary=_LABELS),
            lambda references, predictions: classification_dataset(
                references, predictions, _LABELS
            ),
            True,
            id="classification",
        ),
    ],
)
def test_routed_output_is_byte_identical_to_direct_invocation(
    family: Family,
    reference: object,
    prediction: object,
    config: FamilyConfig,
    direct: Callable[[object, object], Mapping[str, ScoreResult]],
    every_key: bool,
) -> None:
    """Routing adds nothing to a metric's result: each key serializes as the direct call does."""
    routed = run_for_family(reference, prediction, family, config)
    expected = direct(reference, prediction)
    if every_key:
        assert set(routed) == set(expected)
    for key, result in expected.items():
        assert routed[key].to_json() == result.to_json(), key


# One well-formed entry per family, with its configuration, and the prefix of its keys. A
# family added to `Family` without an entry here fails the test below.
_WELL_FORMED: dict[Family, tuple[object, object, FamilyConfig, str]] = {
    Family.STIX_BUNDLE: (
        _bundle("indicator--1"),
        _bundle("indicator--1"),
        FamilyConfig(canonicalizer=_IdentityCanonicalizer()),
        "stix.",
    ),
    Family.STRUCTURED_JSON: (*_json_pair(), FamilyConfig(json_schema=_SCHEMA), "json."),
    Family.CLASSIFICATION: (
        [{"T1059"}],
        [{"T1059"}],
        FamilyConfig(vocabulary=_LABELS),
        "classification.",
    ),
    Family.PROSE: (
        "the actor used phishing",
        "the actor used phishing",
        FamilyConfig(prose=ProseConfig(policy=ProseNormalizationPolicy())),
        "prose.",
    ),
}


@pytest.mark.parametrize("family", list(Family), ids=lambda family: family.value)
def test_every_family_is_scored_by_its_own_metrics(family: Family) -> None:
    """Each family routes to its own runner: a family without one raises, and was
    once scored by the STIX metrics instead."""
    reference, prediction, config, prefix = _WELL_FORMED[family]
    routed = run_for_family(reference, prediction, family, config)
    assert routed
    assert all(key.startswith(prefix) for key in routed), sorted(routed)


@pytest.mark.parametrize(
    ("family", "reference", "prediction", "config"),
    [
        pytest.param(
            Family.STRUCTURED_JSON,
            *_json_pair(),
            FamilyConfig(),
            id="structured-json-without-schema",
        ),
        pytest.param(
            Family.STRUCTURED_JSON,
            "{not json",
            {"name": "alice"},
            FamilyConfig(json_schema=_SCHEMA),
            id="structured-json-malformed-reference",
        ),
        pytest.param(
            Family.CLASSIFICATION,
            [{"T1059"}],
            [{"T1059"}],
            FamilyConfig(),
            id="classification-without-vocabulary",
        ),
        pytest.param(
            Family.STIX_BUNDLE,
            _bundle("indicator--1"),
            _bundle("indicator--1"),
            FamilyConfig(),
            id="stix-bundle-without-canonicalizer",
        ),
        pytest.param(
            Family.PROSE,
            "the actor used phishing",
            "the actor used phishing",
            FamilyConfig(),
            id="prose-without-prose-config",
        ),
    ],
)
def test_a_missing_configuration_or_malformed_reference_raises(
    family: Family, reference: object, prediction: object, config: FamilyConfig
) -> None:
    """The reference and the configuration are caller ground truth: a broken one is
    misconfiguration, never a score."""
    with pytest.raises(ConfigurationError):
        run_for_family(reference, prediction, family, config)


# --- STRUCTURED_JSON ------------------------------------------------------------------------


def test_structured_json_defaults_to_strict_comparison_policy() -> None:
    reference, prediction = _json_pair()
    config = FamilyConfig(json_schema=_SCHEMA)
    routed = run_for_family(reference, prediction, Family.STRUCTURED_JSON, config)
    assert (
        routed["json.field_accuracy"].to_json()
        == json_field_accuracy(reference, prediction, ComparisonPolicy()).to_json()
    )


def test_structured_json_ill_formed_answer_scores_zero_rather_than_invalid() -> None:
    """An unparseable answer scores `scored`/0 on both JSON keys, so it counts in the run aggregate.

    Only `scored` outcomes are persisted, so `invalid` would drop the answer and the run
    would average only the answers that happened to parse.
    """
    config = FamilyConfig(json_schema=_SCHEMA)
    results = run_for_family('{"name": "a"}', "{not json", Family.STRUCTURED_JSON, config)

    assert results["json.schema_validity"].status is MetricStatus.scored
    assert results["json.schema_validity"].value == 0.0

    field_accuracy = results["json.field_accuracy"]
    assert field_accuracy.status is MetricStatus.scored
    assert field_accuracy.value == 0.0
    assert field_accuracy.detail["reason"] == "prediction_not_well_formed"


def test_structured_json_scores_identically_across_all_input_shapes() -> None:
    # Identical documents score 100 whether each side is raw JSON text or already parsed,
    # including mixed shapes.
    config = FamilyConfig(json_schema=_SCHEMA)
    parsed = {"name": "alice"}
    text = '{"name": "alice"}'
    for reference, prediction in ((parsed, parsed), (text, text), (text, parsed), (parsed, text)):
        results = run_for_family(reference, prediction, Family.STRUCTURED_JSON, config)
        field = results["json.field_accuracy"]
        assert field.status is MetricStatus.scored, (reference, prediction)
        assert field.value == 100, (reference, prediction)


# --- STIX_BUNDLE ------------------------------------------------------------------------


def test_stix_bundle_returns_expected_keys() -> None:
    config = FamilyConfig(canonicalizer=_IdentityCanonicalizer())
    results = run_for_family(
        _bundle("indicator--1"), _bundle("indicator--1"), Family.STIX_BUNDLE, config
    )
    assert set(results) == {
        "stix.envelope_validity",
        "stix.semantic_validity",
        "stix.entity_prf",
        "stix.entity_prf.by_type",
        "stix.relationship_prf",
    }


def test_stix_bundle_envelope_gate_invalidates_dependents() -> None:
    config = FamilyConfig(canonicalizer=_IdentityCanonicalizer())
    results = run_for_family(
        _bundle("indicator--1"), {"objects": []}, Family.STIX_BUNDLE, config
    )
    # The envelope failure and its dependents all score 0 (not `invalid`, which
    # the run aggregate would silently drop).
    assert results["stix.envelope_validity"].status is MetricStatus.scored
    assert results["stix.envelope_validity"].value == 0.0
    gate = {
        key: value
        for key, value in results["stix.envelope_validity"].detail.items()
        if key not in ("numerator", "denominator")
    }
    for key in ("stix.entity_prf", "stix.entity_prf.by_type", "stix.relationship_prf"):
        assert results[key].status is MetricStatus.scored
        assert results[key].value == 0.0
        assert results[key].error is None
        assert {k: results[key].detail[k] for k in gate} == gate
    # Counted as the expected answer would be: its two entities (2TP of 2TP) and
    # its relationship earn nothing, and each of its types is missed.
    entity = results["stix.entity_prf"].detail
    assert (entity["numerator"], entity["denominator"]) == (0, 4)
    assert results["stix.entity_prf.by_type"].detail["classes"] == {
        "indicator": {"tp": 0, "fp": 0, "fn": 1},
        "malware": {"tp": 0, "fp": 0, "fn": 1},
    }
    relationship = results["stix.relationship_prf"].detail
    assert (relationship["numerator"], relationship["denominator"]) == (0, 2)


# --- cross-cutting ---------------------------------------------------------------------


def test_every_result_is_keyed_by_its_own_metadata_key() -> None:
    """Each result's metadata key matches its mapping key."""
    config = FamilyConfig(canonicalizer=_IdentityCanonicalizer())
    results = run_for_family(
        _bundle("indicator--1"), _bundle("indicator--1"), Family.STIX_BUNDLE, config
    )
    for key, result in results.items():
        assert result.metadata.key == key
