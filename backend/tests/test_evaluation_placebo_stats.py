"""The statistics of a placebo comparison (level 3).

``dev-docs/agent-placebo-evaluation.md`` FR-22 to FR-27: an arm's quality is rolled
up as the vertical rolls a run up (mean, pooled and macro metrics, no spread
penalty, equal-weight mean of the selection); ``D`` and ``R`` come with a cluster
bootstrap interval and p-value fixed by the seed; Holm adjusts across the targets
of one agent x model; the verdict follows the table; too few entries or an
incomplete run give none.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from app.services.evaluation.aggregation import combine_headline, reduce_grouped_metrics, scale_quality
from app.services.evaluation.placebo_comparisons import _loaded_skill
from app.services.evaluation.placebo_stats import (
    VERDICT_BEATS,
    VERDICT_NO_BETTER,
    VERDICT_NOT_COMPUTABLE,
    VERDICT_TRADE_OFF,
    VERDICT_WORSE,
    Cell,
    Effect,
    MetricValue,
    Trial,
    _metric,
    _quality_stat,
    comparison_statistics,
    holm,
    verdict,
)

MEAN_KEY = "prose.rouge_l"
POOLED_KEY = "prose.exact_match"
MACRO_KEY = "classification.macro_prf"
DEFAULTS = frozenset({MEAN_KEY})


def _trial(quality: float, tokens: float | None = 100.0, **extra) -> Trial:
    return Trial(quality={MEAN_KEY: MetricValue(quality)}, tokens=tokens, **extra)


def _arm(entries: int, quality, tokens=100.0, trials: int = 2) -> dict[str, list[Trial]]:
    """``quality`` and ``tokens`` are values or functions of (entry, trial)."""

    def _at(value, entry, trial):
        return value(entry, trial) if callable(value) else value

    return {
        f"e{entry}": [_trial(_at(quality, entry, t), _at(tokens, entry, t)) for t in range(trials)]
        for entry in range(entries)
    }


def _cell(real, placebo, no_target=None, *, target="persona", complete=True) -> Cell:
    return Cell(
        agent_id="a",
        agent_name="Analyst",
        model_id="m",
        target={"kind": target},
        target_key=target,
        real=real,
        placebo=placebo,
        no_target=no_target,
        complete=complete,
    )


def _stats(*cells: Cell, selection=None) -> dict:
    return comparison_statistics(cells, seed="seed", quality_selection=selection, default_quality_keys=DEFAULTS)


def test_an_arm_s_quality_is_rolled_up_as_the_vertical_rolls_up_a_run():
    # Uneven trials per entry, a 0-of-0 pooled entry, a macro entry seeing no class.
    rows = [
        ("e0", {MEAN_KEY: 80.0, POOLED_KEY: (1, 1), MACRO_KEY: ({"x": {"tp": 2, "fp": 1, "fn": 0}}, 66.7)}),
        ("e0", {MEAN_KEY: 40.0, POOLED_KEY: (0, 1), MACRO_KEY: ({"y": {"tp": 0, "fp": 0, "fn": 3}}, 0.0)}),
        ("e1", {MEAN_KEY: 55.5, POOLED_KEY: (0, 0), MACRO_KEY: ({"x": {"tp": 1, "fp": 0, "fn": 1}}, 66.7)}),
        ("e2", {MEAN_KEY: 100.0, POOLED_KEY: (3, 4), MACRO_KEY: ({}, 100.0)}),
    ]
    arm: dict[str, list[Trial]] = {}
    grouped: dict[str, list[float]] = {MEAN_KEY: [], POOLED_KEY: [], MACRO_KEY: []}
    counts: dict[str, list] = {POOLED_KEY: []}
    class_counts: dict[str, list] = {MACRO_KEY: []}
    for entry, metrics in rows:
        numerator, denominator = metrics[POOLED_KEY]
        classes, macro_value = metrics[MACRO_KEY]
        pooled_value = 100.0 * numerator / denominator if denominator else 100.0
        arm.setdefault(entry, []).append(
            Trial(
                quality={
                    MEAN_KEY: MetricValue(metrics[MEAN_KEY]),
                    POOLED_KEY: MetricValue(pooled_value, numerator=numerator, denominator=denominator),
                    MACRO_KEY: MetricValue(macro_value, class_counts=classes),
                },
                tokens=None,
            )
        )
        grouped[MEAN_KEY].append(metrics[MEAN_KEY])
        grouped[POOLED_KEY].append(pooled_value)
        counts[POOLED_KEY].append((numerator, denominator))
        grouped[MACRO_KEY].append(macro_value)
        class_counts[MACRO_KEY].append(classes)

    entries = sorted(arm)
    every_entry = np.arange(len(entries))[np.newaxis, :]
    vertical = scale_quality(reduce_grouped_metrics(grouped, 0.0, counts=counts, class_counts=class_counts))
    for key in grouped:
        assert _metric(arm, entries, key).values(every_entry)[0] == pytest.approx(vertical[key], abs=0.01), key

    metrics = [_metric(arm, entries, key) for key in grouped]
    headline = combine_headline(vertical, list(grouped))
    assert _quality_stat(metrics)(every_entry)[0] == pytest.approx(headline.value, abs=0.01)


def test_a_pooled_or_macro_metric_without_its_counts_is_left_out_as_the_vertical_leaves_it():
    arm = {"e0": [Trial(quality={POOLED_KEY: MetricValue(50.0)}, tokens=None)]}

    assert _metric(arm, ["e0"], POOLED_KEY) is None
    assert reduce_grouped_metrics({POOLED_KEY: [50.0]}, 0.0, counts={POOLED_KEY: [None]}) == {}


def test_d_and_r_compare_the_arms_with_an_interval_and_a_p_value():
    stats = _stats(_cell(_arm(12, 80.0, 100.0), _arm(12, 60.0, 200.0)))

    (row,) = stats["rows"]
    quality, tokens = row["vs_placebo"]["quality"], row["vs_placebo"]["tokens"]
    assert (quality["estimate"], quality["ci_low"], quality["ci_high"]) == (20.0, 20.0, 20.0)
    assert (tokens["estimate"], tokens["ci_low"], tokens["ci_high"]) == (0.5, 0.5, 0.5)
    # Every resample on the same side of the null: the bootstrap's smallest p.
    assert quality["p"] == tokens["p"] == round(2 / 10_001, 4)
    assert row["verdict"] == VERDICT_BEATS
    assert row["rests_on_tokens_alone"] is False
    assert row["entries"] == 12
    assert (row["arms"]["real"]["trials"], row["arms"]["placebo"]["trials"]) == (24, 24)
    assert row["warnings"] == []


def test_the_bootstrap_resamples_entries_and_is_fixed_by_the_seed():
    # D is 0, 1 or 2 points depending on the entry: 1 on average.
    real = _arm(12, lambda entry, _trial: 50.0 + 4 * entry)
    placebo = _arm(12, lambda entry, _trial: 50.0 + 4 * entry - entry % 3)

    first = _stats(_cell(real, placebo))
    again = _stats(_cell(real, placebo))
    other = comparison_statistics(
        [_cell(real, placebo)], seed="another", quality_selection=None, default_quality_keys=DEFAULTS
    )

    quality = first["rows"][0]["vs_placebo"]["quality"]
    assert quality["estimate"] == 1.0
    assert quality["ci_low"] < 1.0 < quality["ci_high"]
    assert first == again
    assert other["rows"][0]["vs_placebo"]["quality"]["estimate"] == 1.0


def test_holm_adjusts_in_input_order_and_leaves_missing_p_values_out():
    assert holm([0.01, 0.04, None, 0.03]) == pytest.approx([0.03, 0.06, None, 0.06])
    assert holm([]) == []


def test_holm_runs_across_the_targets_of_one_agent_and_model():
    better = _cell(_arm(12, 80.0), _arm(12, 60.0), target="persona")
    same = _cell(_arm(12, 70.0), _arm(12, 70.0), target="prompts")

    rows = _stats(better, same)["rows"]

    persona = rows[0]["vs_placebo"]["quality"]
    assert persona["p_adjusted"] == pytest.approx(min(1.0, 2 * persona["p"]), abs=1e-4)


def test_a_cell_without_a_verdict_does_not_widen_the_others_correction():
    better = _cell(_arm(12, 80.0), _arm(12, 60.0), target="persona")
    failed = _cell(_arm(12, 70.0), _arm(12, 60.0), target="prompts", complete=False)

    persona, prompts = _stats(better, failed)["rows"]

    assert persona["vs_placebo"]["quality"]["p_adjusted"] == persona["vs_placebo"]["quality"]["p"]
    assert prompts["vs_placebo"]["quality"]["p_adjusted"] is None
    assert prompts["verdict"] == VERDICT_NOT_COMPUTABLE


def _effect(estimate: float, p_adjusted: float | None) -> Effect:
    return Effect(estimate=estimate, ci_low=estimate, ci_high=estimate, p=p_adjusted or 1.0, p_adjusted=p_adjusted)


@pytest.mark.parametrize(
    ("quality", "tokens", "expected"),
    [
        (_effect(5.0, 0.01), _effect(1.0, 0.9), (VERDICT_BEATS, False)),
        (_effect(0.0, 0.9), _effect(0.7, 0.01), (VERDICT_BEATS, True)),
        (_effect(5.0, 0.01), _effect(1.4, 0.01), (VERDICT_TRADE_OFF, False)),
        (_effect(-5.0, 0.01), _effect(1.0, 0.9), (VERDICT_WORSE, False)),
        (_effect(-5.0, 0.01), _effect(1.3, 0.02), (VERDICT_WORSE, False)),
        (_effect(5.0, 0.2), _effect(0.7, 0.2), (VERDICT_NO_BETTER, False)),
        (_effect(5.0, 0.01), None, (VERDICT_BEATS, False)),
    ],
    ids=["quality-up", "tokens-only", "trade-off", "quality-down", "both-down", "nothing", "no-tokens"],
)
def test_the_verdict_follows_the_table(quality, tokens, expected):
    assert verdict(quality, tokens) == expected


@pytest.mark.parametrize(
    ("cell", "selection", "warning"),
    [
        (_cell(_arm(2, 80.0), _arm(2, 60.0)), None, "few_entries"),
        (_cell(_arm(12, 80.0), _arm(12, 60.0), complete=False), None, "run_incomplete"),
        (_cell(_arm(12, 80.0), _arm(12, 60.0)), [], "quality_unmeasured"),
    ],
    ids=["two-entries", "a-run-did-not-complete", "no-quality-metric-selected"],
)
def test_no_verdict_without_enough_entries_a_complete_cell_and_a_quality(cell, selection, warning):
    (row,) = _stats(cell, selection=selection)["rows"]

    assert row["verdict"] == VERDICT_NOT_COMPUTABLE
    assert warning in row["warnings"]


def test_unmeasured_tokens_leave_the_quality_verdict_and_say_so():
    (row,) = _stats(_cell(_arm(12, 80.0, tokens=None), _arm(12, 60.0, tokens=None)))["rows"]

    assert row["vs_placebo"]["tokens"] is None
    assert row["verdict"] == VERDICT_BEATS
    assert "tokens_unmeasured" in row["warnings"]


def test_the_no_target_arm_is_compared_with_both_others():
    (row,) = _stats(_cell(_arm(12, 80.0), _arm(12, 70.0), _arm(12, 50.0, tokens=50.0)))["rows"]

    assert row["vs_no_target"]["quality"]["estimate"] == 30.0
    assert row["placebo_vs_no_target"]["quality"]["estimate"] == 20.0
    assert row["vs_no_target"]["tokens"]["estimate"] == 2.0
    assert row["arms"]["no_target"]["tokens"] == 50.0


def test_the_secondary_measures_are_each_arm_s_plain_means():
    real = {"e0": [_trial(80.0, judge=90.0, latency=2.0, tool_calls=3.0, skill_loaded=True),
                   _trial(80.0, judge=70.0, latency=4.0, tool_calls=1.0, skill_loaded=False)]}

    (row,) = _stats(_cell(real, _arm(1, 60.0)))["rows"]

    assert row["arms"]["real"] == {
        "entries": 1, "trials": 2, "tokens": 100.0, "judge": 80.0, "latency": 3.0, "tool_calls": 2.0,
        "skill_loaded": 0.5,
    }


def _load(name) -> dict:
    return {"type": "tool", "tool_name": "load_skill", "tool_input": json.dumps({"name": name})}


@pytest.mark.parametrize(
    ("traces", "names", "expected"),
    [
        ([_load("Triage")], frozenset({"triage"}), True),
        ([_load("triage (2)")], frozenset({"triage"}), True),
        ([_load("Triage notes")], frozenset({"triage"}), False),
        ([{"type": "tool", "tool_name": "web_search", "tool_input": "{}"}], frozenset({"triage"}), False),
        ([_load("Anything")], None, True),
        ([{"type": "tool", "tool_name": "load_skill", "tool_input": "Triage"}], frozenset({"triage"}), True),
        (None, None, False),
    ],
    ids=["own-name", "duplicate-suffix", "another-skill", "another-tool", "any-skill", "plain-input", "no-timeline"],
)
def test_a_skill_load_is_read_from_the_result_s_timeline(traces, names, expected):
    assert _loaded_skill(traces, names) is expected
