"""The deterministic comparison of two evaluation runs."""

from __future__ import annotations

import pytest

from app.services import evaluation_comparison as comparison


@pytest.mark.parametrize(
    ("delta", "verdict"),
    [
        (2.01, "progression"),
        (2.0, "neutral"),
        (0.0, "neutral"),
        (-2.0, "neutral"),
        (-2.01, "regression"),
        (35.0, "progression"),
        (-35.0, "regression"),
    ],
)
def test_the_noise_margin_bounds_are_neutral(delta, verdict):
    assert comparison.verdict_for(delta) == verdict


def test_a_metric_is_compared_on_its_rounded_delta():
    result = comparison.compare_metric("score", 71.5, 74.25)

    assert result == comparison.MetricComparison("score", 71.5, 74.25, 2.75, "progression")
    # 2.004 reads as 2.0: the verdict follows the number that is shown.
    assert comparison.compare_metric("score", 70.0, 72.004).verdict == "neutral"


def test_a_missing_value_has_no_verdict():
    for baseline, candidate in ((None, 80.0), (80.0, None), (float("nan"), 80.0), (True, 80.0)):
        result = comparison.compare_metric("score", baseline, candidate)
        assert (result.delta, result.verdict) == (None, None)


def test_every_metric_is_compared_in_order():
    """The two headlines come first, then the judge's score; the retired F1, speed
    and overall scores are not compared, even when both runs hold them.
    """
    retired = {"f1_score": 30.0, "speed_score": 50.0, "overall_score": 60.0}
    results = comparison.compare_scores(
        {"quality_score": 70.0, "performance_score": 50.0, "score": 80.0, **retired},
        {"quality_score": 71.5, "performance_score": 60.0, "score": 76.0, "f1_score": 40.0, "speed_score": 90.0},
    )

    assert [(r.metric, r.delta, r.verdict) for r in results] == [
        ("quality_score", 1.5, "neutral"),
        ("performance_score", 10.0, "progression"),
        ("score", -4.0, "regression"),
    ]


def test_the_margin_can_be_widened_by_a_caller():
    assert comparison.compare_metric("score", 80.0, 84.0, margin=5.0).verdict == "neutral"
