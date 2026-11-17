"""The rules of feedback criteria, without a database.

What a definition may be, what a score must be, what a score is worth on the
common [0, 1] scale, and how scores add up - including three edge cases:
no criteria falls back to the thumbs, a single criterion is the
identity, and enabled weights summing to zero are refused.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import feedback_criteria as fc


def _criterion(criterion_type="scale", *, weight=1.0, enabled=True, options=None, lo=1.0, hi=5.0, name="Accuracy"):
    return SimpleNamespace(
        id=uuid.uuid4(),
        key=name.lower(),
        name=name,
        type=criterion_type,
        weight=weight,
        enabled=enabled,
        options=options,
        min_value=None if criterion_type == "categorical" else lo,
        max_value=None if criterion_type == "categorical" else hi,
    )


TONE_OPTIONS = [
    {"value": "cold", "label": "Cold", "score": 0.0},
    {"value": "neutral", "label": "Neutral", "score": 0.5},
    {"value": "warm", "label": "Warm", "score": 1.0},
]


# ---------------------------------------------------------------------------
# Definitions
# ---------------------------------------------------------------------------


def test_a_categorical_definition_keeps_clean_options_and_no_bounds():
    options, lo, hi = fc.validate_shape(
        "categorical",
        [{"value": " cold ", "label": "", "score": 0}, {"value": "warm", "label": "Warm", "score": 1}],
        None,
        None,
    )
    assert options == [
        {"value": "cold", "label": "cold", "score": 0.0},
        {"value": "warm", "label": "Warm", "score": 1.0},
    ]
    assert (lo, hi) == (None, None)


@pytest.mark.parametrize(
    ("options", "lo", "hi", "message"),
    [
        ([{"value": "only", "score": 1}], None, None, "between 2 and 20 options"),
        ([{"value": "a", "score": 0}, {"value": "A", "score": 1}], None, None, "appears twice"),
        ([{"value": "a", "score": 0}, {"value": "b", "score": 1.5}], None, None, "score between 0 and 1"),
        ([{"value": "", "score": 0}, {"value": "b", "score": 1}], None, None, "has no value"),
        (TONE_OPTIONS, 1.0, 5.0, "not a minimum or a maximum"),
    ],
)
def test_bad_categorical_definitions_are_refused(options, lo, hi, message):
    with pytest.raises(fc.CriterionError, match=message):
        fc.validate_shape("categorical", options, lo, hi)


def test_numeric_and_scale_definitions_keep_their_bounds_as_floats():
    assert fc.validate_shape("numeric", None, 0, 10.5) == (None, 0.0, 10.5)
    assert fc.validate_shape("scale", None, 1, 5) == (None, 1.0, 5.0)


@pytest.mark.parametrize(
    ("criterion_type", "options", "lo", "hi", "message"),
    [
        ("numeric", None, None, 5, "needs a minimum and a maximum"),
        ("numeric", None, 5, 5, "greater than the minimum"),
        ("numeric", TONE_OPTIONS, 0, 1, "not options"),
        ("scale", None, 1.5, 5, "whole number"),
        ("scale", None, 0, 21, "at most 21 steps"),
        ("stars", None, 1, 5, "Unknown criterion type"),
    ],
)
def test_bad_bounded_definitions_are_refused(criterion_type, options, lo, hi, message):
    with pytest.raises(fc.CriterionError, match=message):
        fc.validate_shape(criterion_type, options, lo, hi)


def test_a_scale_of_exactly_21_steps_is_accepted():
    assert fc.validate_shape("scale", None, 0, 20) == (None, 0.0, 20.0)


def test_weights_must_be_non_negative_numbers():
    assert fc.validate_weight(2) == 2.0
    for bad in (-1, float("nan"), float("inf"), True, "1"):
        with pytest.raises(fc.CriterionError):
            fc.validate_weight(bad)


def test_enabled_weights_summing_to_zero_are_refused():
    fc.assert_weights_usable([])  # nothing enabled: ratings fall back to the thumbs
    fc.assert_weights_usable([0.0, 0.5])
    with pytest.raises(fc.CriterionError, match="all weigh zero"):
        fc.assert_weights_usable([0.0, 0.0])


def test_keys_are_slugs_made_unique():
    assert fc.base_key("Tone of voice!") == "tone-of-voice"
    assert fc.base_key("正確さ") == "criterion"
    assert fc.unique_key("tone", set()) == "tone"
    assert fc.unique_key("tone", {"tone"}) == "tone-2"
    assert fc.unique_key("tone", {"tone", "tone-2"}) == "tone-3"
    assert len(fc.unique_key("x" * 64, {"x" * 64})) <= fc.MAX_KEY_LENGTH


# ---------------------------------------------------------------------------
# Scores
# ---------------------------------------------------------------------------


def test_valid_scores_of_every_type_are_accepted():
    tone = _criterion("categorical", options=TONE_OPTIONS, name="Tone")
    accuracy = _criterion("scale", lo=1, hi=5)
    latency = _criterion("numeric", lo=0, hi=10, name="Latency")

    validated = fc.validate_scores(
        [tone, accuracy, latency], [(tone.id, "warm"), (accuracy.id, 4), (latency.id, 2.5)]
    )

    assert validated == [
        fc.ValidatedScore(tone.id, None, "warm"),
        fc.ValidatedScore(accuracy.id, 4.0, None),
        fc.ValidatedScore(latency.id, 2.5, None),
    ]


def test_invalid_scores_are_refused_naming_the_criterion():
    tone = _criterion("categorical", options=TONE_OPTIONS, name="Tone")
    accuracy = _criterion("scale", lo=1, hi=5)
    latency = _criterion("numeric", lo=0, hi=10, name="Latency")
    retired = _criterion("scale", enabled=False, name="Retired")
    criteria = [tone, accuracy, latency, retired]

    cases = [
        ([(uuid.uuid4(), 3)], "does not exist or is disabled"),
        ([(retired.id, 3)], "does not exist or is disabled"),
        ([(accuracy.id, 3), (accuracy.id, 4)], "'Accuracy' is scored twice"),
        ([(tone.id, "hot")], "'Tone' takes one of its options"),
        ([(tone.id, 1)], "'Tone' takes one of its options"),
        ([(latency.id, "fast")], "'Latency' takes a number"),
        ([(latency.id, True)], "'Latency' takes a number"),
        ([(latency.id, 11)], "'Latency' takes a value from 0 to 10"),
        ([(accuracy.id, 3.5)], "'Accuracy' takes a whole step"),
    ]
    for raw, message in cases:
        with pytest.raises(fc.CriterionError, match=message):
            fc.validate_scores(criteria, raw)


def test_normalized_values_of_every_type():
    tone = _criterion("categorical", options=TONE_OPTIONS)
    accuracy = _criterion("scale", lo=1, hi=5)
    latency = _criterion("numeric", lo=0, hi=10)

    assert fc.normalized_value(tone, None, "neutral") == 0.5
    # An option removed since the score was given no longer maps.
    assert fc.normalized_value(tone, None, "hot") is None
    assert fc.normalized_value(accuracy, 1, None) == 0.0
    assert fc.normalized_value(accuracy, 4, None) == 0.75
    assert fc.normalized_value(latency, 2.5, None) == 0.25
    # Bounds narrowed since: clamped rather than out of range.
    assert fc.normalized_value(latency, 15, None) == 1.0


def test_weighted_score_edge_cases():
    # No criterion scored: the thumbs.
    assert fc.weighted_score("positive", []) == 1.0
    assert fc.weighted_score("negative", []) == 0.0
    # A single criterion: its own normalized value, whatever its weight.
    assert fc.weighted_score("negative", [(3.0, 0.75)]) == 0.75
    # Scored criteria that all weigh zero: the thumbs again.
    assert fc.weighted_score("positive", [(0.0, 0.2), (0.0, 0.4)]) == 1.0
    # Unmappable scores are ignored.
    assert fc.weighted_score("negative", [(1.0, None)]) == 0.0
    assert fc.weighted_score("positive", [(1.0, 1.0), (3.0, 0.0)]) == 0.25


def test_a_rating_weighs_only_its_enabled_criteria():
    accuracy = _criterion("scale", lo=1, hi=5, weight=1.0)
    retired = _criterion("scale", lo=1, hi=5, weight=9.0, enabled=False)
    scores = [
        SimpleNamespace(criterion_id=accuracy.id, value_numeric=5.0, value_text=None),
        SimpleNamespace(criterion_id=retired.id, value_numeric=1.0, value_text=None),
        SimpleNamespace(criterion_id=uuid.uuid4(), value_numeric=1.0, value_text=None),  # deleted
    ]

    assert fc.rating_weighted_score("negative", scores, {accuracy.id: accuracy, retired.id: retired}) == 1.0


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def test_statistics_of_a_categorical_criterion():
    tone = _criterion("categorical", options=TONE_OPTIONS)

    stats = fc.criterion_statistics(tone, [(None, "warm", 3), (None, "cold", 1), (None, "removed", 7)])

    assert stats.count == 4
    assert stats.mean_score == 0.75
    assert [(b.key, b.label, b.count) for b in stats.histogram] == [
        ("cold", "Cold", 1),
        ("neutral", "Neutral", 0),
        ("warm", "Warm", 3),
    ]


def test_statistics_of_a_scale_criterion():
    accuracy = _criterion("scale", lo=1, hi=5)

    stats = fc.criterion_statistics(accuracy, [(5.0, None, 2), (1.0, None, 2)])

    assert stats.count == 4
    assert stats.mean_score == 0.5
    assert [(b.key, b.count) for b in stats.histogram] == [("1", 2), ("2", 0), ("3", 0), ("4", 0), ("5", 2)]


def test_statistics_of_a_numeric_criterion_bucket_its_range():
    latency = _criterion("numeric", lo=0, hi=10)

    stats = fc.criterion_statistics(latency, [(0.0, None, 1), (9.9, None, 1), (10.0, None, 1), (4.0, None, 1)])

    assert stats.count == 4
    assert [b.label for b in stats.histogram] == ["0-2", "2-4", "4-6", "6-8", "8-10"]
    assert [b.count for b in stats.histogram] == [1, 0, 1, 0, 2]


def test_statistics_without_scores():
    stats = fc.criterion_statistics(_criterion("scale"), [])
    assert (stats.count, stats.mean_score) == (0, None)


def test_the_agent_weighted_score():
    accuracy = fc.CriterionStatistics(criterion=_criterion(weight=1.0), count=4, mean_score=0.5)
    tone = fc.CriterionStatistics(criterion=_criterion(weight=3.0), count=2, mean_score=1.0)
    retired = fc.CriterionStatistics(criterion=_criterion(weight=5.0, enabled=False), count=9, mean_score=0.0)
    never = fc.CriterionStatistics(criterion=_criterion(weight=5.0), count=0, mean_score=None)

    assert fc.aggregate_weighted_score([accuracy, tone, retired, never], 1, 1) == 0.875
    # No criterion scored: the share of thumbs up.
    assert fc.aggregate_weighted_score([never], 3, 1) == 0.75
    # Nothing rated at all.
    assert fc.aggregate_weighted_score([], 0, 0) is None
