"""Unit tests for the two-stage metric aggregation.

Covers Stage 1 (reduce across data entries), normalisation (comparative, performance
only) and Stage 2 (equal-weight combination), and the distinctions they must keep:

* absent vs. ``0.0``: an unmeasured metric is never reduced to a measured zero;
* ``None`` vs. ``[]`` selection: "unconfigured" and "select none" differ;
* a **single** run (degenerate, N/A) vs. **several equal** runs (a genuine tie at the
  top). The obvious guard for the first also matches the second, so both directions
  are asserted.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services.evaluation.aggregation import (
    REASON_FAILED,
    REASON_MISSING_FROM_OTHER_RUNS,
    REASON_NOT_A_DEFAULT_CONTRIBUTOR,
    REASON_NOT_APPLICABLE,
    AggregatedMetric,
    classify_exclusions,
    combine_headline,
    normalise_across_runs,
    normalise_performance,
    reduce_grouped_metrics,
    scale_quality,
)
from app.services.evaluation.metric_package import (
    HIGHER_IS_BETTER,
    LOWER_IS_BETTER,
    is_pooled_key,
)
from app.services.evaluation.metric_package.families import FAMILY_IDS

# Real package keys, so the tests exercise the same registry production does.
# Averaged ones: a pooled or macro metric needs its counts, tested on its own.
QUALITY_HIGHER = "prose.token_prf"  # higher_is_better, 0-100, averaged
QUALITY_SECOND = "classification.samples_f1"  # higher_is_better, 0-100, a default contributor
# No shipped quality metric is lower_is_better (Hamming loss is removed), so the
# orientation tests declare this one lower_is_better (`_declare_lower_is_better`): the
# flip is generic and must keep working for the next such metric. The only other averaged
# default contributor: a flipped metric must compose an unconfigured headline.
QUALITY_FLIPPED = QUALITY_SECOND

# Package quality values are on a 0-100 scale: `ScoreResult` refuses a scored result
# outside `[0, 100]`. Fixtures use that scale; 0-1 fractions once hid a field defect.
PERF_LOWER = "performance.latency.wall_clock"  # lower_is_better, seconds
PERF_HIGHER = "performance.reliability.completion_rate"  # higher_is_better
LEGACY_KEY = "ttft_seconds"  # execution-level, not a package metric


def _declare_lower_is_better(monkeypatch, key: str) -> None:
    """Make aggregation read ``key`` as lower_is_better, as a future metric may declare."""
    from app.services.evaluation import aggregation

    declared = aggregation.metric_direction
    monkeypatch.setattr(
        aggregation,
        "metric_direction",
        lambda k: LOWER_IS_BETTER if k == key else declared(k),
    )


def _rid() -> uuid.UUID:
    return uuid.uuid4()


# --- Stage 1 ---------------------------------------------------------------------------


def test_a_key_with_no_usable_values_is_absent_rather_than_zero():
    """Absent and 0.0 are different answers: a key with no usable value is dropped."""
    out = reduce_grouped_metrics({QUALITY_HIGHER: []}, penalty_factor=0.5)
    assert QUALITY_HIGHER not in out
    assert out == {}


def test_inconsistency_is_penalised_on_the_quality_side():
    """A run that scores identically beats one with the same mean but spread."""
    steady = reduce_grouped_metrics({QUALITY_HIGHER: [50.0, 50.0]}, penalty_factor=0.5)
    spread = reduce_grouped_metrics({QUALITY_HIGHER: [0.0, 100.0]}, penalty_factor=0.5)
    assert steady[QUALITY_HIGHER].value == 50.0
    assert spread[QUALITY_HIGHER].value < steady[QUALITY_HIGHER].value


def test_the_consistency_penalty_always_moves_a_metric_toward_worse():
    """For a lower_is_better metric the penalty must ADD deviation, not subtract it.

    Subtracting would make an erratic run look faster than a steady one with the same
    mean, quietly rewarding inconsistency.
    """
    steady = reduce_grouped_metrics({PERF_LOWER: [10.0, 10.0]}, penalty_factor=0.5)
    erratic = reduce_grouped_metrics({PERF_LOWER: [5.0, 15.0]}, penalty_factor=0.5)
    assert steady[PERF_LOWER].value == 10.0
    assert erratic[PERF_LOWER].value > steady[PERF_LOWER].value


def test_performance_values_keep_their_native_units():
    """Raw measurements must not be clamped into the 0-100 score range at Stage 1.

    ``_penalized_mean`` clamps to [0, 100]; reusing it here would flatten a 120-second
    run and a 5000-token run to the same value before they were ever compared.
    """
    out = reduce_grouped_metrics({PERF_LOWER: [120.0, 120.0]})
    assert out[PERF_LOWER].value == 120.0


def test_stored_selection_with_legacy_key_stays_inert():
    """A persisted selection naming a removed key contributes nothing and raises nothing.

    Nothing validates ``overall_quality_metrics`` / ``overall_performance_metrics`` on
    write, so a selection can name a key no longer produced. Stage 1 discards any
    key that is neither package quality nor package performance before the selection
    applies.
    """
    reduced = reduce_grouped_metrics({LEGACY_KEY: [1.0, 2.0], PERF_LOWER: [5.0]})
    scaled = {LEGACY_KEY: 1.5, PERF_LOWER: 5.0}

    only_legacy = combine_headline(scaled, [LEGACY_KEY])
    assert only_legacy.value == 1.5, "a selected key present in the input still counts"
    assert LEGACY_KEY not in reduced, "but Stage 1 never lets one reach Stage 2"

    # The realistic path: Stage 1 output is what Stage 2 receives.
    headline = combine_headline(
        {k: m.value for k, m in reduced.items()}, [LEGACY_KEY, PERF_LOWER]
    )
    assert headline.contributing_keys == [PERF_LOWER]
    assert LEGACY_KEY not in headline.contributing_keys

    # A selection naming ONLY removed keys reads not-applicable, never 0.0.
    none_left = combine_headline({k: m.value for k, m in reduced.items()}, [LEGACY_KEY])
    assert none_left.value is None


def test_an_out_of_range_quality_value_is_excluded_rather_than_scaled():
    # Outside the package's 0-100 range: a value it cannot produce is dropped, not
    # rendered as a valid percentage.
    assert reduce_grouped_metrics({QUALITY_HIGHER: [142.0]}) == {}
    assert reduce_grouped_metrics({QUALITY_HIGHER: [-1.0]}) == {}


def test_direction_is_carried_through_for_normalisation():
    out = reduce_grouped_metrics(
        {PERF_LOWER: [1.0], PERF_HIGHER: [0.9]},
        counts={PERF_HIGHER: [(9, 10)]},  # a pooled rate's run value comes from its counts
    )
    assert out[PERF_LOWER].direction == LOWER_IS_BETTER
    assert out[PERF_HIGHER].direction == HIGHER_IS_BETTER


# --- Normalisation ---------------------------------------------------------------------


def _agg(key: str, value: float, direction: str) -> AggregatedMetric:
    return AggregatedMetric(key=key, value=value, entry_count=1, direction=direction)


def test_a_single_run_is_not_normalised_to_the_top_of_the_scale():
    """One run is simultaneously best and worst, so there is no basis.

    Scoring it 100.0 would give a rescored run a perfect speed score whatever its real
    duration.
    """
    run = _rid()
    out = normalise_across_runs({run: _agg(PERF_LOWER, 9999.0, LOWER_IS_BETTER)})
    assert out == {}


def test_two_equal_runs_still_tie_at_the_top_and_are_not_not_applicable():
    """The converse of the rule above, and the easier half to lose.

    A single-run rule keyed on "best == worst" also matches several runs holding equal
    values. Those runs were genuinely compared and the tolerance band ranks them
    together; N/A would discard a real result.
    """
    a, b = _rid(), _rid()
    out = normalise_across_runs(
        {a: _agg(PERF_LOWER, 10.0, LOWER_IS_BETTER), b: _agg(PERF_LOWER, 10.0, LOWER_IS_BETTER)}
    )
    assert out == {a: 100.0, b: 100.0}


def test_lower_is_better_maps_the_fastest_run_to_the_top():
    fast, slow = _rid(), _rid()
    out = normalise_across_runs(
        {
            fast: _agg(PERF_LOWER, 1.0, LOWER_IS_BETTER),
            slow: _agg(PERF_LOWER, 10.0, LOWER_IS_BETTER),
        },
        tolerance=0.0,
    )
    assert out[fast] == 100.0
    assert out[slow] == 0.0


def test_higher_is_better_maps_the_largest_run_to_the_top():
    good, poor = _rid(), _rid()
    out = normalise_across_runs(
        {
            good: _agg(PERF_HIGHER, 1.0, HIGHER_IS_BETTER),
            poor: _agg(PERF_HIGHER, 0.0, HIGHER_IS_BETTER),
        },
        tolerance=0.0,
    )
    assert out[good] == 100.0
    assert out[poor] == 0.0


def test_the_tolerance_band_is_a_shared_victory_zone():
    a, b, c = _rid(), _rid(), _rid()
    out = normalise_across_runs(
        {
            a: _agg(PERF_LOWER, 10.0, LOWER_IS_BETTER),
            b: _agg(PERF_LOWER, 10.4, LOWER_IS_BETTER),
            c: _agg(PERF_LOWER, 40.0, LOWER_IS_BETTER),
        },
        tolerance=5.0,
    )
    assert out[a] == 100.0
    assert out[b] == 100.0  # within 5% of the best
    assert out[c] < 100.0


def test_a_zero_best_still_opens_a_tolerance_band():
    """A zero best value still opens a tolerance band.

    `best * (1 + tolerance/100)` is `0` whatever the tolerance, so a multiplicative
    victory zone collapses to an exact tie. Not an edge case: 8 of the 9 performance
    metrics are lower-is-better, and the counts and reliability rates
    (`performance.counts.retries`, `performance.reliability.tool_error_rate`, ...) are `0.0`
    on a healthy run.
    """
    clean, blip, bad = _rid(), _rid(), _rid()
    out = normalise_across_runs(
        {
            clean: _agg(PERF_LOWER, 0.0, LOWER_IS_BETTER),
            blip: _agg(PERF_LOWER, 0.02, LOWER_IS_BETTER),
            bad: _agg(PERF_LOWER, 0.5, LOWER_IS_BETTER),
        },
        tolerance=5.0,
    )
    assert out[clean] == 100.0
    assert out[blip] == 100.0  # within 5% of the span of a zero-anchored band
    assert out[bad] == 0.0


def test_a_non_positive_best_keeps_the_band_on_the_favourable_side():
    """The sign of `best` must not push the threshold past the best run itself.

    Once `best` is negative, `× (1 + tolerance/100)` moves the threshold further from
    zero and `× (1 - tolerance/100)` moves it closer; either way it lands beyond `best`
    and excludes the best run from its own victory zone. No metric produces a negative
    value today, so this pins the arithmetic, not a live regression.
    """
    good, mid, poor = _rid(), _rid(), _rid()
    higher = normalise_across_runs(
        {
            good: _agg(PERF_HIGHER, -1.0, HIGHER_IS_BETTER),
            mid: _agg(PERF_HIGHER, -5.0, HIGHER_IS_BETTER),
            poor: _agg(PERF_HIGHER, -10.0, HIGHER_IS_BETTER),
        },
        tolerance=5.0,
    )
    assert [higher[good], higher[mid], higher[poor]] == [100.0, 58.48, 0.0]

    a, b, c = _rid(), _rid(), _rid()
    lower = normalise_across_runs(
        {
            a: _agg(PERF_LOWER, -10.0, LOWER_IS_BETTER),
            b: _agg(PERF_LOWER, -5.0, LOWER_IS_BETTER),
            c: _agg(PERF_LOWER, -1.0, LOWER_IS_BETTER),
        },
        tolerance=5.0,
    )
    assert [lower[a], lower[b], lower[c]] == [100.0, 46.78, 0.0]


def test_a_strictly_positive_set_is_scored_exactly_as_before():
    """A strictly positive set keeps the multiplicative band, so no stored score moves.

    A span-anchored band everywhere would be defensible but shifts every stored number
    (`[10, 20, 30]` lower-is-better moves 51.28 -> 52.63). The outputs below are pinned
    verbatim.
    """
    a, b, c = _rid(), _rid(), _rid()
    lower = normalise_across_runs(
        {
            a: _agg(PERF_LOWER, 10.0, LOWER_IS_BETTER),
            b: _agg(PERF_LOWER, 20.0, LOWER_IS_BETTER),
            c: _agg(PERF_LOWER, 30.0, LOWER_IS_BETTER),
        },
        tolerance=5.0,
    )
    assert [lower[a], lower[b], lower[c]] == [100.0, 51.28, 0.0]

    d, e, f = _rid(), _rid(), _rid()
    higher = normalise_across_runs(
        {
            d: _agg(PERF_HIGHER, 0.9, HIGHER_IS_BETTER),
            e: _agg(PERF_HIGHER, 0.5, HIGHER_IS_BETTER),
            f: _agg(PERF_HIGHER, 0.1, HIGHER_IS_BETTER),
        },
        tolerance=5.0,
    )
    assert [higher[d], higher[e], higher[f]] == [100.0, 52.98, 0.0]


def test_a_negative_tolerance_cannot_demote_the_best_run():
    """A tolerance is a width, so a negative one has no meaning and would invert the band.

    `best * (1 - 5/100)` for lower-is-better puts the threshold *below* the best value,
    excluding the best run from its own victory zone.

    The schema refuses a negative, but the bundle import reads `performance_tolerance`
    straight out of JSON and older rows are read back unvalidated. The floor sits where
    a tolerance becomes a threshold.
    """
    a, b, c = _rid(), _rid(), _rid()
    out = normalise_across_runs(
        {
            a: _agg(PERF_LOWER, 10.0, LOWER_IS_BETTER),
            b: _agg(PERF_LOWER, 20.0, LOWER_IS_BETTER),
            c: _agg(PERF_LOWER, 30.0, LOWER_IS_BETTER),
        },
        tolerance=-5.0,
    )
    # Clamped to zero: an exact-tie band, the narrowest meaningful one.
    assert [out[a], out[b], out[c]] == [100.0, 50.0, 0.0]

    d, e, f = _rid(), _rid(), _rid()
    higher = normalise_across_runs(
        {
            d: _agg(PERF_HIGHER, 0.9, HIGHER_IS_BETTER),
            e: _agg(PERF_HIGHER, 0.5, HIGHER_IS_BETTER),
            f: _agg(PERF_HIGHER, 0.1, HIGHER_IS_BETTER),
        },
        tolerance=-5.0,
    )
    assert higher[d] == 100.0


def test_the_api_refuses_a_negative_performance_tolerance():
    """The schema rejects a negative tolerance rather than only absorbing it.

    The floor in `_tolerance_threshold` keeps rankings sound whatever is stored, but alone
    it would silently accept a setting it then ignores. The neighbouring bounded fields
    (`timeout_seconds`, `judge_max_tokens`, `schema_weight`) are constrained the same way.
    """
    from pydantic import ValidationError

    from app.api.v1.schemas.evaluation import (
        CreateEvaluationRequest,
        UpdateEvaluationRequest,
    )

    with pytest.raises(ValidationError):
        CreateEvaluationRequest(name="x", performance_tolerance=-5.0)
    with pytest.raises(ValidationError):
        UpdateEvaluationRequest(performance_tolerance=-0.1)

    # Zero is a legitimate setting: exact ties only.
    assert CreateEvaluationRequest(name="x", performance_tolerance=0.0).performance_tolerance == 0.0


def test_a_metric_without_direction_contributes_to_no_run():
    a, b = _rid(), _rid()
    out = normalise_across_runs(
        {a: _agg(PERF_LOWER, 1.0, None), b: _agg(PERF_LOWER, 2.0, None)}
    )
    assert out == {}


def test_normalisation_uses_the_intersection_of_keys_across_runs():
    """A metric one run lacks is dropped from the whole evaluation.

    Per-run composition would let a run holding only its single most favourable metric
    average that one value and score near the top, while a run measured on five averages
    its weaker ones — displayed side by side as if comparable.
    """
    rich, poor = _rid(), _rid()
    out = normalise_performance(
        {
            rich: {
                PERF_LOWER: _agg(PERF_LOWER, 1.0, LOWER_IS_BETTER),
                PERF_HIGHER: _agg(PERF_HIGHER, 1.0, HIGHER_IS_BETTER),
            },
            poor: {PERF_LOWER: _agg(PERF_LOWER, 2.0, LOWER_IS_BETTER)},
        },
        tolerance=0.0,
    )
    assert set(out[rich]) == {PERF_LOWER}
    assert set(out[poor]) == {PERF_LOWER}


def test_scale_quality_selects_only_the_quality_half():
    metrics = {
        QUALITY_HIGHER: _agg(QUALITY_HIGHER, 80.0, HIGHER_IS_BETTER),
        PERF_LOWER: _agg(PERF_LOWER, 3.0, LOWER_IS_BETTER),
    }
    assert scale_quality(metrics) == {QUALITY_HIGHER: 80.0}


# --- Stage 2 ---------------------------------------------------------------------------


def test_none_selection_uses_every_available_key():
    out = combine_headline({"a": 100.0, "b": 0.0}, selection=None)
    assert out.value == 50.0
    assert out.contributing_keys == ["a", "b"]


def test_empty_selection_is_not_applicable_and_never_zero():
    """``None`` and ``[]`` are not interchangeable: an empty selection is N/A, never 0."""
    out = combine_headline({"a": 100.0}, selection=[])
    assert out.value is None
    assert out.contributing_keys == []


def test_unavailable_selected_keys_are_ignored_rather_than_counted_as_zero():
    out = combine_headline({"a": 80.0}, selection=["a", "not_available_here"])
    assert out.value == 80.0
    assert out.contributing_keys == ["a"]


def test_contributing_keys_reflect_what_actually_composed_the_value():
    out = combine_headline({"a": 10.0, "b": 20.0, "c": 30.0}, selection=["c", "a"])
    assert out.contributing_keys == ["a", "c"]
    assert out.value == 20.0


def test_a_genuine_measured_zero_stays_zero():
    out = combine_headline({"a": 0.0}, selection=None)
    assert out.value == 0.0
    assert out.value is not None


# --- Exclusion reasons ------------------------------------------------------------------


def test_an_unavailable_metric_is_reported_as_not_applicable():
    run = _rid()
    out = classify_exclusions(
        selection=["a", "b"],
        available={"a"},
        statuses_by_run={run: {"a": "scored", "b": "unavailable"}},
    )
    assert [e.key for e in out] == ["b"]
    assert out[0].reason == REASON_NOT_APPLICABLE
    assert out[0].run_ids == [run]


def test_an_invalid_metric_is_reported_as_failed():
    run = _rid()
    out = classify_exclusions(
        selection=["b"], available=set(), statuses_by_run={run: {"b": "invalid"}}
    )
    assert out[0].reason == REASON_FAILED


def test_failed_wins_a_mixed_set_so_a_recoverable_metric_is_not_written_off():
    a, b = _rid(), _rid()
    out = classify_exclusions(
        selection=["m"],
        available=set(),
        statuses_by_run={a: {"m": "unavailable"}, b: {"m": "invalid"}},
    )
    assert out[0].reason == REASON_FAILED


def test_no_selection_discloses_no_narrowing():
    out = classify_exclusions(selection=None, available=set(), statuses_by_run={})
    assert out == []


def test_a_key_outside_the_family_catalogue_is_not_applicable():
    """A stale/cross-family selection can never be produced by re-running.

    Nothing on the write path validates ``overall_quality_metrics`` against the
    evaluation's family, and family is independently mutable after a selection was
    configured, so a selected key entirely absent from the family's catalogue is a
    reachable state, not a hypothetical one. Absent from the blob AND outside the
    catalogue must read as ``not_applicable``, never ``failed``.
    """
    from app.services.evaluation.metric_package import families

    out = classify_exclusions(
        selection=["stix.entity_prf"],
        available=set(),
        statuses_by_run={},
        family=families.PROSE,
    )
    assert [e.key for e in out] == ["stix.entity_prf"]
    assert out[0].reason == REASON_NOT_APPLICABLE


def test_a_blob_absent_key_inside_the_family_is_still_failed():
    """The family check must not swallow a genuine in-family execution failure."""
    from app.services.evaluation.metric_package import families

    out = classify_exclusions(
        selection=["prose.token_prf"],
        available=set(),
        statuses_by_run={},
        family=families.PROSE,
    )
    assert out[0].reason == REASON_FAILED


def test_family_check_does_not_override_an_observed_status():
    """An observed run status (recoverable or not) still wins over the family check."""
    from app.services.evaluation.metric_package import families

    run = _rid()
    out = classify_exclusions(
        selection=["stix.entity_prf"],
        available=set(),
        statuses_by_run={run: {"stix.entity_prf": "invalid"}},
        family=families.PROSE,
    )
    assert out[0].reason == REASON_FAILED


def test_no_family_given_falls_back_to_the_blob_absent_default():
    """Omitting ``family`` (existing callers, older code paths) must be unchanged."""
    out = classify_exclusions(
        selection=["stix.entity_prf"], available=set(), statuses_by_run={}
    )
    assert out[0].reason == REASON_FAILED


def test_a_stale_unimplemented_family_falls_back_to_the_blob_absent_default():
    """A stored family no longer implemented must not be trusted as a catalogue.

    ``DETECTION_RULE``/``JSON_ANALYSIS`` were retired: an evaluation created
    before that still has the old value stored. ``quality_catalog`` degrades to ``[]``
    for an unimplemented family, which would make ``metric_catalog`` performance-only
    and misclassify every missing quality key as not_applicable. The family check must
    be skipped for such a family, preserving the original failed default.
    """
    out = classify_exclusions(
        selection=["stix.entity_prf"],
        available=set(),
        statuses_by_run={},
        family="DETECTION_RULE",
    )
    assert out[0].reason == REASON_FAILED


@pytest.mark.parametrize("selection", [None, []])
def test_exclusions_are_carried_onto_the_headline(selection):
    from app.services.evaluation.aggregation import ExcludedMetric

    disclosure = [ExcludedMetric(key="m", reason=REASON_FAILED, run_ids=[])]
    out = combine_headline({"a": 1.0}, selection=selection, excluded=disclosure)
    assert out.excluded == disclosure


# ── A judge-off run reports a real Overall Quality ────────────────────────────
#
# An evaluation with the LLM judge disabled must not read "0 everywhere". These run the
# whole path on real PostgreSQL: stored per-entry metric rows in, a persisted non-zero
# ``quality_score`` out.


from sqlalchemy import select

import app.models  # noqa: F401 - register model metadata
from app.models.agent import Agent
from app.models.evaluation import (
    Evaluation,
    EvaluationResult,
    EvaluationRun,
)
from app.models.evaluation_metric import EvaluationMetric
from app.services.evaluation.aggregation import (
    compute_quality_headline,
    load_metric_statuses,
    load_metric_statuses_by_run,
)
from tests.scratch_database import ALL_TABLES, scratch_database


async def _make_db():
    """An isolated database with every table and the ``vector`` extension (``tests/scratch_database.py``).

    Returns ``(sessionmaker, cleanup)``, the shape the tests here take: the database
    is dropped once ``cleanup`` is awaited. Skips if PostgreSQL is unavailable.
    """
    stack = contextlib.AsyncExitStack()
    db = await stack.enter_async_context(scratch_database("agg", tables=ALL_TABLES, extensions=("vector",)))
    return db.sessions, stack.aclose


async def _seed_judge_off_run(
    session,
    entries: list[dict[str, float]],
    *,
    selection: list[str] | None = None,
    details: list[dict] | None = None,
    anomaly_kinds: list[str | None] | None = None,
):
    """Seed one run whose entries carry family quality metrics and **no judge score**."""
    agent = Agent(name="Agent")
    evaluation = Evaluation(
        name="Eval",
        # Judge off: an explicit empty selection. NULL would mean "unset", which
        # still runs the judge (scorers.registry.judge_enabled).
        scorer_selection=[],
        overall_quality_metrics=selection,
    )
    session.add_all([agent, evaluation])
    await session.flush()

    run = EvaluationRun(
        evaluation_id=evaluation.id,
        agent_id=agent.id,
        model_id="gpt-4o",
        trigger_reason="manual",
        status="completed",
        score=None,  # judge off: no judge score anywhere
        # The headline pass reads the run's own selection snapshot, so a later edit
        # to the evaluation cannot re-grade a completed run.
        scorer_selection=[],
    )
    session.add(run)
    await session.flush()

    for i, metrics in enumerate(entries):
        result = EvaluationResult(
            run_id=run.id,
            agent_output="",
            # `evaluation_results.score` is NOT NULL, so a judge-off entry stores 0.0
            # here. The headline ignores this column; averaging it reads "0 everywhere".
            score=0.0,
            execution_anomaly_kind=(anomaly_kinds or [])[i]
            if anomaly_kinds and i < len(anomaly_kinds)
            else None,
            execution_details=(details or [])[i]
            if details and i < len(details)
            else None,
        )
        session.add(result)
        await session.flush()
        for key, value in metrics.items():
            session.add(
                EvaluationMetric(
                    run_id=run.id, result_id=result.id, key=key, value=value
                )
            )
    await session.flush()
    return evaluation, run


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_judge_off_run_reports_a_non_zero_overall_quality():
    """A judge-off run's headline is a real number derived from its family metrics.

    Judge disabled, family metrics recorded per entry: the headline is never ``0``.
    """
    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, run = await _seed_judge_off_run(
                session,
                [{QUALITY_HIGHER: 90.0}, {QUALITY_HIGHER: 80.0}, {QUALITY_HIGHER: 85.0}],
            )
            headline = await compute_quality_headline(session, run.id, evaluation)

        assert run.score is None, "precondition: the judge contributed nothing"
        assert headline.value is not None
        assert headline.value > 0, "the '0 everywhere' defect"
        # mean 85.0, penalised by 0.5 * std(4.082) -> 82.96
        assert headline.value == pytest.approx(82.96, abs=0.01)
        assert headline.contributing_keys == [QUALITY_HIGHER]
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_lower_is_better_quality_metric_is_flipped_before_it_contributes(monkeypatch):
    """A lower_is_better quality metric is flipped before it contributes.

    Unflipped, a run that makes *more* mistakes would score *higher*.
    """
    _declare_lower_is_better(monkeypatch, QUALITY_FLIPPED)
    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, run = await _seed_judge_off_run(
                session, [{QUALITY_FLIPPED: 10.0}, {QUALITY_FLIPPED: 10.0}]
            )
            headline = await compute_quality_headline(session, run.id, evaluation)
        # a loss of 10 out of 100 is *good* -> 90, not 10.
        assert headline.value == pytest.approx(90.0, abs=0.01)
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_run_with_no_family_metrics_is_not_applicable_not_zero():
    """Nothing measured is ``None``, which renders "N/A" — never a measured ``0.0``."""
    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, run = await _seed_judge_off_run(
                session, [{"ttft_seconds": 1.2}]  # legacy execution key only
            )
            headline = await compute_quality_headline(session, run.id, evaluation)
        assert headline.value is None
        assert headline.contributing_keys == []
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("anomaly_kind", "expected"),
    [
        # The entry's 0 is included: mean 50, pulled to 25 by the default 0.5
        # inconsistency penalty.
        ("confirmation_seeking", 25.0),
        # Excluded: only the clean entry counts, 100 rather than 25.
        ("transient_error", 100.0),
    ],
    ids=["confirmation-seeking-counts", "transient-error-excluded"],
)
async def test_an_anomalous_entry_counts_toward_the_headline_only_when_confirmation_seeking(
    anomaly_kind, expected
):
    """Anomaly exclusion before reduction matches the judge aggregate."""
    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, run = await _seed_judge_off_run(
                session,
                [{QUALITY_HIGHER: 100.0}, {QUALITY_HIGHER: 0.0}],
                anomaly_kinds=[None, anomaly_kind],
            )
            headline = await compute_quality_headline(session, run.id, evaluation)
        assert headline.value == pytest.approx(expected, abs=0.01)
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_confirmation_seeking_entry_counts_in_the_multi_run_status_reader():
    """The multi-run status reader (`load_metric_statuses_by_run`) applies the
    same legacy-safe inclusion contract as `aggregate_metrics_by_run`."""
    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, run = await _seed_judge_off_run(
                session,
                [{QUALITY_HIGHER: 0.9}],
                selection=[QUALITY_HIGHER],
            )
            run2 = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=run.agent_id,
                model_id="gpt-4o",
                trigger_reason="manual",
                status="completed",
                score=None,
                scorer_selection=[],
            )
            session.add(run2)
            await session.flush()
            # A confirmation-seeking result on run2 whose execution_details still
            # carries a metric_results entry must be counted; a transient-error
            # sibling must not.
            confirm_result = EvaluationResult(
                run_id=run2.id,
                agent_output="",
                score=0.0,
                execution_anomaly_kind="confirmation_seeking",
                execution_details={
                    "metric_results": {QUALITY_HIGHER: {"status": "scored"}}
                },
            )
            transient_result = EvaluationResult(
                run_id=run2.id,
                agent_output="",
                score=0.0,
                execution_anomaly_kind="transient_error",
                execution_details={
                    "metric_results": {QUALITY_HIGHER: {"status": "scored"}}
                },
            )
            session.add_all([confirm_result, transient_result])
            await session.flush()

            statuses = await load_metric_statuses_by_run(session, [run2.id])
        # Only the confirmation-seeking entry's details are read for run2.
        assert statuses[run2.id] == {QUALITY_HIGHER: "scored"}
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_selected_but_unavailable_metric_is_disclosed_with_a_reason():
    """A metric the author selected but that never scored must say *why*."""
    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, run = await _seed_judge_off_run(
                session,
                [{QUALITY_HIGHER: 0.9}],
                selection=[QUALITY_HIGHER, QUALITY_SECOND],
                details=[
                    {
                        "metric_results": {
                            QUALITY_HIGHER: {"status": "scored"},
                            QUALITY_SECOND: {"status": "unavailable"},
                        }
                    }
                ],
            )
            headline = await compute_quality_headline(session, run.id, evaluation)

        assert headline.contributing_keys == [QUALITY_HIGHER]
        assert [(e.key, e.reason) for e in headline.excluded] == [
            (QUALITY_SECOND, REASON_NOT_APPLICABLE)
        ]
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_metric_scored_on_any_entry_is_not_reported_as_unavailable():
    """A later entry skipping a metric is an entry-level gap, not an absent metric."""
    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            _, run = await _seed_judge_off_run(
                session,
                [{QUALITY_HIGHER: 0.9}, {}],
                details=[
                    {"metric_results": {QUALITY_HIGHER: {"status": "scored"}}},
                    {"metric_results": {QUALITY_HIGHER: {"status": "skipped"}}},
                ],
            )
            statuses = await load_metric_statuses(session, run.id)
        assert statuses[QUALITY_HIGHER] == "scored"
    finally:
        await cleanup()


# Values captured before the Stage-1 fetch was batched; see
# `test_batching_leaves_every_headline_value_untouched`.
PIN_VALUES = [100.0, 83.77, 0.0]
PIN_KEYS = [[PERF_LOWER], [PERF_LOWER], [PERF_LOWER]]
PIN_ENTRY_COUNTS = [{PERF_LOWER: 2}, {PERF_LOWER: 1}, {PERF_LOWER: 1}]
# Each run's record says what is true for it: the two runs that measured
# PERF_HIGHER lost it because the third lacks it; the third keeps the reason that
# applies to it. ``performance.tokens.total`` was measured by no run.
PIN_EXCLUDED = [
    [
        (PERF_HIGHER, REASON_MISSING_FROM_OTHER_RUNS),
        ("performance.tokens.total", REASON_FAILED),
    ]
] * 2 + [
    [
        (PERF_HIGHER, REASON_NOT_APPLICABLE),
        ("performance.tokens.total", REASON_FAILED),
    ]
]


# ---------------------------------------------------------------------------
# Stage 1 batching — the fetch is per evaluation, not per run
# ---------------------------------------------------------------------------


async def _seed_performance_runs(
    session,
    per_run_entries: list[list[dict[str, float]]],
    *,
    selection: list[str] | None = None,
    details: list[list[dict]] | None = None,
):
    """Seed one evaluation with several completed runs carrying performance metrics."""
    agent = Agent(name="Agent")
    evaluation = Evaluation(
        name="Eval",
        scorer_selection=[],
        overall_performance_metrics=selection,
    )
    session.add_all([agent, evaluation])
    await session.flush()

    runs = []
    for r, entries in enumerate(per_run_entries):
        run = EvaluationRun(
            evaluation_id=evaluation.id,
            agent_id=agent.id,
            model_id="gpt-4o",
            trigger_reason="manual",
            status="completed",
            score=None,
        )
        session.add(run)
        await session.flush()
        runs.append(run)

        for i, metrics in enumerate(entries):
            result = EvaluationResult(
                run_id=run.id,
                agent_output="",
                score=0.0,
                execution_details=(details or [])[r][i]
                if details and r < len(details) and i < len(details[r])
                else None,
            )
            session.add(result)
            await session.flush()
            for key, value in metrics.items():
                # A pooled metric's run value comes from its entries' counts:
                # counts whose ratio is the seeded value, so a one-entry run reads it back.
                counts = (
                    (round(value * 1000), 1000)
                    if is_pooled_key(key)
                    else (None, None)
                )
                session.add(
                    EvaluationMetric(
                        run_id=run.id,
                        result_id=result.id,
                        key=key,
                        value=value,
                        numerator=counts[0],
                        denominator=counts[1],
                    )
                )
    await session.flush()
    return evaluation, runs


class _QueryCounter:
    """Counts real cursor executions on the session's engine, scoped to a block.

    The listener sits on the engine, where the cursor is, reached through the session's
    public ``bind`` (``sessionmaker.kw["bind"]`` is an internal detail).
    """

    def __init__(self, session):
        self._engine = session.bind.sync_engine
        self.count = 0

    def _on_execute(self, *args, **kwargs):
        self.count += 1

    def __enter__(self):
        from sqlalchemy import event

        event.listen(self._engine, "before_cursor_execute", self._on_execute)
        return self

    def __exit__(self, *exc):
        from sqlalchemy import event

        event.remove(self._engine, "before_cursor_execute", self._on_execute)
        return False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_performance_headlines_cost_the_same_whatever_the_run_count():
    """The Stage-1 fetch is one round trip per evaluation, not one per run.

    Overall Performance is comparative, so every run completion rescores **every**
    completed sibling. A per-run fetch makes one completion cost O(R) and a batch of R
    runs O(R squared), serialized on one session inside an advisory lock. The test
    asserts a *constant*: a bound that scales with R is the defect itself.
    """
    from app.services.evaluation.aggregation import compute_performance_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        counts = {}
        for run_count in (2, 8):
            async with sessionmaker() as session:
                evaluation, runs = await _seed_performance_runs(
                    session,
                    [[{PERF_LOWER: 1.0 + r}] for r in range(run_count)],
                )
                await session.commit()
                with _QueryCounter(session) as counter:
                    await compute_performance_headlines(
                        session, evaluation, [r.id for r in runs]
                    )
                counts[run_count] = counter.count

        assert counts[2] == counts[8], (
            f"Stage-1 fetch still scales with run count: {counts}"
        )
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_exclusion_status_pass_also_costs_the_same_whatever_the_run_count():
    """The exclusion status pass is also one round trip per evaluation.

    It only runs when a selected key is unavailable, so the common path never
    exercises it.
    """
    from app.services.evaluation.aggregation import compute_performance_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        counts = {}
        for run_count in (2, 8):
            async with sessionmaker() as session:
                evaluation, runs = await _seed_performance_runs(
                    session,
                    [[{PERF_LOWER: 1.0 + r}] for r in range(run_count)],
                    # A selected key that never scored, so exclusions must be classified.
                    selection=[PERF_LOWER, "performance.tokens.total"],
                    details=[
                        [{"metric_results": {PERF_LOWER: {"status": "scored"}}}]
                        for _ in range(run_count)
                    ],
                )
                await session.commit()
                with _QueryCounter(session) as counter:
                    await compute_performance_headlines(
                        session, evaluation, [r.id for r in runs]
                    )
                counts[run_count] = counter.count

        assert counts[2] == counts[8], (
            f"Status pass still scales with run count: {counts}"
        )
    finally:
        await cleanup()


# ---------------------------------------------------------------------------
# Coalesced bulk re-score performance pass — one pass, not N
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("run_count", [2, 6])
async def test_coalesced_performance_pass_runs_exactly_once_per_batch(
    monkeypatch, run_count
):
    """A re-score batch runs the shared Overall Performance pass exactly once.

    Drives the real ``execute_rescore_runs``, the entry point of bulk and single
    Re-score, with a counting shim around the real ``_recompute_performance_headlines``
    so every call still executes. Two batch sizes make "once, whatever the size"
    load-bearing.
    """
    from app.services import evaluation_engine
    from app.services.evaluation_engine import (
        _recompute_performance_headlines,
        execute_rescore_runs,
    )

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            _evaluation, runs = await _seed_performance_runs(
                session, [[{PERF_LOWER: 1.0 + r}] for r in range(run_count)]
            )
            # The state ``execute_rescore_runs`` expects: the caller flips every
            # targeted run to "running" before enqueuing the batch.
            for run in runs:
                run.status = "running"
            await session.commit()
            run_ids = [str(r.id) for r in runs]

        monkeypatch.setattr("app.core.database.async_session", sessionmaker)

        call_count = 0

        async def _counting_recompute(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return await _recompute_performance_headlines(*args, **kwargs)

        monkeypatch.setattr(
            evaluation_engine,
            "_recompute_performance_headlines",
            _counting_recompute,
        )

        await execute_rescore_runs(
            run_ids, previous_statuses=dict.fromkeys(run_ids, "completed")
        )

        assert call_count == 1, (
            f"a {run_count}-run batch must trigger exactly one shared "
            f"performance pass regardless of size, got {call_count}"
        )

        async with sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(EvaluationRun).where(
                            EvaluationRun.id.in_([r.id for r in runs])
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert all(r.status == "completed" for r in rows), (
                "every run in the batch must have been driven to a terminal "
                "status by the real execute_rescore_runs pass"
            )
            scores = {r.id: r.performance_score for r in rows}
            assert all(s is not None for s in scores.values()), (
                "the single shared pass must still write every run's "
                "performance_score, not just skip it because it runs once"
            )
            # The fastest run tops the scale only if the shared pass really ran:
            # a no-op shim would leave every score None.
            assert scores[runs[0].id] == 100.0
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execute_rescore_runs_skips_the_shared_pass_when_every_run_ends_cancelled(
    monkeypatch,
):
    """An all-cancelled batch writes no performance headline.

    Its terminal runs are never forced into a comparative basis.
    """
    from app.services.evaluation_engine import execute_rescore_runs

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            _evaluation, runs = await _seed_performance_runs(
                session, [[{PERF_LOWER: 1.0}], [{PERF_LOWER: 2.0}]]
            )
            for run in runs:
                # Cancelled before the batch's turn comes up, e.g. a bulk cancel
                # racing the dispatch.
                run.status = "cancelled"
            await session.commit()
            run_ids = [str(r.id) for r in runs]

        monkeypatch.setattr("app.core.database.async_session", sessionmaker)

        await execute_rescore_runs(
            run_ids, previous_statuses=dict.fromkeys(run_ids, "completed")
        )

        async with sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(EvaluationRun).where(
                            EvaluationRun.id.in_([r.id for r in runs])
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert all(r.status == "cancelled" for r in rows)
            assert all(r.performance_score is None for r in rows), (
                "an all-cancelled batch has no genuinely rescored run, so no "
                "performance headline may be written for any of them"
            )
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execute_rescore_runs_skips_the_shared_pass_when_every_run_fails(
    monkeypatch,
):
    """A batch whose every run fails skips the shared pass, like an all-cancelled one.

    A run that was attempted and raised gets back its previous status, here
    ``"failed"``. Only a completed run counts as work done: with no completed run, the
    whole-evaluation pass would write nothing useful and hold the advisory lock for
    nothing.
    """
    from app.services.evaluation_engine import execute_rescore_runs

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            _evaluation, runs = await _seed_performance_runs(
                session, [[{PERF_LOWER: 1.0}], [{PERF_LOWER: 2.0}]]
            )
            for run in runs:
                # Dispatched this attempt, unlike the cancelled case above, which
                # never reaches ``rescore_run``.
                run.status = "running"
            await session.commit()
            run_ids = [str(r.id) for r in runs]

        from unittest.mock import AsyncMock

        from app.services import evaluation_engine

        shared_pass = AsyncMock()
        monkeypatch.setattr(
            evaluation_engine, "_recompute_shared_performance_basis", shared_pass
        )

        async def _rescore_run_always_raises(_session, _run_id, **_kwargs):
            raise ValueError("no_predictions")

        monkeypatch.setattr(
            evaluation_engine, "rescore_run", _rescore_run_always_raises
        )
        monkeypatch.setattr("app.core.database.async_session", sessionmaker)

        # Runs that had failed before this rescore, which fails them again.
        await execute_rescore_runs(
            run_ids, previous_statuses=dict.fromkeys(run_ids, "failed")
        )

        shared_pass.assert_not_awaited()

        async with sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(EvaluationRun).where(
                            EvaluationRun.id.in_([r.id for r in runs])
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert all(r.status == "failed" for r in rows)
            assert all(r.performance_score is None for r in rows)
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_cancelled_run_is_excluded_from_its_siblings_shared_basis(
    monkeypatch,
):
    """A run cancelled mid-batch keeps its score and stays out of its siblings' basis.

    The run ends ``cancelled`` concurrently, before its turn in the bounded pool.
    Forcing the whole batch into the basis would include runs finalized
    ``cancelled``/``failed`` by the time the deferred pass runs.
    """
    from app.services.evaluation_engine import execute_rescore_runs

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            _evaluation, runs = await _seed_performance_runs(
                session,
                [
                    [{PERF_LOWER: 1.0}],  # will end cancelled
                    [{PERF_LOWER: 5.0}],  # rescored, faster of the two survivors
                    [{PERF_LOWER: 50.0}],  # rescored, slower of the two survivors
                ],
            )
            cancelled_run, fast_run, slow_run = runs
            # A score from an earlier, now-stale basis: the pass must leave it alone.
            cancelled_run.performance_score = 42.0
            cancelled_run.status = "cancelled"
            fast_run.status = "running"
            slow_run.status = "running"
            await session.commit()
            # The cancelled run must be a batch member: only a run named in
            # ``run_ids`` could be force-included in the basis. This models
            # "cancelled before its turn in the bounded pool":
            # ``_rescore_run_with_own_session`` sees it is no longer "running" and
            # skips it, while ``fast_run``/``slow_run`` are rescored.
            run_ids = [str(cancelled_run.id), str(fast_run.id), str(slow_run.id)]

        monkeypatch.setattr("app.core.database.async_session", sessionmaker)

        await execute_rescore_runs(
            run_ids, previous_statuses=dict.fromkeys(run_ids, "completed")
        )

        async with sessionmaker() as session:
            after_cancelled = await session.get(EvaluationRun, cancelled_run.id)
            after_fast = await session.get(EvaluationRun, fast_run.id)
            after_slow = await session.get(EvaluationRun, slow_run.id)

        assert after_cancelled.status == "cancelled"
        assert after_cancelled.performance_score == 42.0, (
            "a cancelled run's stale performance_score must be left alone by "
            "the shared pass, not overwritten because it was force-included "
            "in the batch's basis"
        )
        assert after_fast.status == "completed"
        assert after_slow.status == "completed"
        assert after_fast.performance_score == 100.0, (
            "the faster surviving run must top the scale against its "
            "genuinely-rescored sibling only, not against the cancelled run"
        )
        assert after_slow.performance_score is not None
        assert after_slow.performance_score < 100.0
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_is_rescore_pending_reflects_the_batchs_own_orchestration_execution():
    """Pending follows the batch's ``rescore_runs`` execution, not the run row.

    A run reaches its terminal status before the batch's coalesced Overall Performance
    pass commits, so a poll gated on run status could stop with a stale
    ``performance_score``. ``is_rescore_pending_for_run`` reads ``True`` while that
    execution is non-terminal, and ``False`` once it is terminal (the adapter awaits
    ``execute_rescore_runs``, shared pass included, before completing it) or when no
    execution names the run.
    """
    from app.models.orchestration import OrchestrationExecution
    from app.services.evaluation_engine import is_rescore_pending_for_run

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            _evaluation, runs = await _seed_performance_runs(
                session, [[{PERF_LOWER: 1.0}], [{PERF_LOWER: 2.0}]]
            )
            run, sibling = runs
            run.status = "completed"
            sibling.status = "completed"
            await session.commit()

            # A run with no rescore execution naming it at all.
            assert await is_rescore_pending_for_run(session, str(run.id)) is False

            execution = OrchestrationExecution(
                adapter="evaluation",
                state="running",
                payload={
                    "kind": "rescore_runs",
                    "run_ids": [str(run.id), str(sibling.id)],
                },
            )
            session.add(execution)
            await session.commit()

            # Still in flight: both named runs read pending.
            assert await is_rescore_pending_for_run(session, str(run.id)) is True
            assert await is_rescore_pending_for_run(session, str(sibling.id)) is True

            # A run genuinely absent from the execution's own run_ids must
            # never read pending just because *an* execution exists.
            other_id = str(uuid.uuid4())
            assert await is_rescore_pending_for_run(session, other_id) is False

            execution.state = "completed"
            await session.commit()

            # The shared pass has landed (execution terminal): no longer
            # pending, so the frontend's poll is free to stop.
            assert await is_rescore_pending_for_run(session, str(run.id)) is False
            assert await is_rescore_pending_for_run(session, str(sibling.id)) is False
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_stringified_run_id_cannot_silently_lose_its_rows():
    """A guard against silent data loss, **not** a supported input type.

    Every caller passes UUIDs: `_compute_run_headlines` converts its string
    `batch_run_ids` at the query boundary, and the annotation stays
    `Sequence[uuid.UUID]`. The ``WHERE`` clause accepts a stringified UUID while the
    driver returns a :class:`uuid.UUID`, so an accumulator keyed on the caller's objects
    would drop every row and surface as a plausible N/A headline. The guard makes the
    mistake survivable without making it an API.
    """
    from app.services.evaluation.aggregation import aggregate_metrics_by_run

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            _, runs = await _seed_performance_runs(
                session, [[{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]]
            )
            await session.commit()
            aggregates = await aggregate_metrics_by_run(
                session, [str(r.id) for r in runs]
            )

        assert [aggregates[str(r.id)][PERF_LOWER].value for r in runs] == [1.0, 4.0]
    finally:
        await cleanup()


_PIN_SELECTION = [PERF_LOWER, PERF_HIGHER, "performance.tokens.total"]
_PIN_DETAILS = [
    [
        {"metric_results": {PERF_LOWER: {"status": "scored"}}},
        {"metric_results": {PERF_LOWER: {"status": "scored"}}},
    ],
    [{"metric_results": {PERF_LOWER: {"status": "scored"}}}],
    [
        {
            "metric_results": {
                PERF_LOWER: {"status": "scored"},
                PERF_HIGHER: {"status": "unavailable"},
            }
        }
    ],
]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selection", "details", "expected_excluded"),
    [
        (_PIN_SELECTION, _PIN_DETAILS, PIN_EXCLUDED),
        # Without per-entry details the third run cannot tell "unavailable" apart
        # from a failed measurement.
        (
            None,
            None,
            [[(PERF_HIGHER, REASON_MISSING_FROM_OTHER_RUNS)]] * 2
            + [[(PERF_HIGHER, REASON_FAILED)]],
        ),
    ],
    ids=["with-selection-and-disclosure", "no-selection-no-details"],
)
async def test_batching_leaves_every_headline_value_untouched(
    selection, details, expected_excluded
):
    """Batching changes the fetch, never the arithmetic.

    The values were captured before batching. Covers both directions, a run with several
    entries reduced together, a run missing a metric its siblings have, and the
    exclusion disclosure: every place a regrouping bug would surface. With no
    selection and no abstainer the same values hold, so abstention reaches no further
    than runs with nothing to say.
    """
    from app.services.evaluation.aggregation import compute_performance_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [
                    # Two entries reduced together, both metrics present.
                    [
                        {PERF_LOWER: 1.0, PERF_HIGHER: 100.0},
                        {PERF_LOWER: 2.0, PERF_HIGHER: 80.0},
                    ],
                    # Middle of the pack.
                    [{PERF_LOWER: 3.0, PERF_HIGHER: 50.0}],
                    # Missing PERF_HIGHER entirely: the intersection drops it for all.
                    [{PERF_LOWER: 9.0}],
                ],
                selection=selection,
                details=details,
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        values = [
            headlines[r.id].value if headlines.get(r.id) else None for r in runs
        ]
        keys = [
            headlines[r.id].contributing_keys if headlines.get(r.id) else None
            for r in runs
        ]
        counts = [
            headlines[r.id].entry_counts if headlines.get(r.id) else None for r in runs
        ]
        excluded = [
            sorted((e.key, e.reason) for e in headlines[r.id].excluded)
            if headlines.get(r.id)
            else None
            for r in runs
        ]
        assert values == PIN_VALUES
        assert keys == PIN_KEYS
        assert counts == PIN_ENTRY_COUNTS
        assert excluded == expected_excluded
    finally:
        await cleanup()


# ---------------------------------------------------------------------------
# An unmeasured headline is NULL, never 0
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unmeasured_headline_persists_as_null_not_zero():
    """The two must stay distinguishable at rest, not just on screen.

    A headline stored as ``0.0`` claims the family was measured and scored zero.
    Persisting an *absent* measurement that way reads "0 everywhere".
    """
    from app.services.evaluation_engine import _compute_run_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            # No family quality metrics recorded for any entry.
            evaluation, run = await _seed_judge_off_run(session, [{}, {}])
            await _compute_run_headlines(session, evaluation.id)
            await session.commit()
            await session.refresh(run)

        assert run.quality_score is None, "unmeasured must persist as NULL"
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_genuine_zero_headline_persists_as_zero():
    """The other half of the same contract: a real 0 must survive as 0.

    Fixing "unmeasured reads 0" by treating 0 as unmeasured would silently hide
    the worst possible result, which is the one most worth seeing.
    """
    from app.services.evaluation_engine import _compute_run_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            # Measured, and every entry scored the floor.
            evaluation, run = await _seed_judge_off_run(
                session,
                [{QUALITY_HIGHER: 0.0}, {QUALITY_HIGHER: 0.0}],
            )
            await _compute_run_headlines(session, evaluation.id)
            await session.commit()
            await session.refresh(run)

        assert run.quality_score == 0.0
        assert run.quality_score is not None
        assert run.headline_details["quality_metric_keys"] == [QUALITY_HIGHER]
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_judge_off_evaluation_still_produces_chart_data():
    """A judge-off evaluation still charts.

    Requiring a non-NULL judge ``score`` would empty the chart of a judge-off
    evaluation even when family quality metrics were measured for every run.
    """
    from app.api.v1.evaluations import get_chart_data
    from app.models.user import User
    from app.services.evaluation_engine import _compute_run_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, run = await _seed_judge_off_run(
                session,
                [{QUALITY_HIGHER: 0.9}, {QUALITY_HIGHER: 0.8}],
            )
            admin = User(
                email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password="x",
                full_name="Admin",
                role="admin",  # `is_admin` is a derived property, not a column
            )
            session.add(admin)
            await session.flush()
            await _compute_run_headlines(session, evaluation.id)
            await session.commit()
            await session.refresh(run)

            assert run.score is None, "precondition: the judge contributed nothing"
            assert run.quality_score is not None, "precondition: quality was measured"

            chart = await get_chart_data(str(evaluation.id), user=admin, db=session)

        quality = chart["trends"]["quality"]
        assert quality, "a judge-off evaluation must still chart"
        assert [v for p in quality for v in p["scores"].values()] == [
            run.quality_score
        ]
        # The judge trend is empty rather than a flat line at zero.
        assert all(not p["scores"] for p in chart["trends"]["judge"])
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_run_with_no_headline_at_all_is_omitted_from_the_chart():
    """A run with no headline is not plotted, never placed at 0.

    ``ChartDataPoint.scores`` is ``dict[str, float]``, so the only honest way
    to represent "not measured" is absence — on every one of the three
    trends, and on every correlation view, where a missing axis omits the point
    rather than pinning it to an edge.
    """
    from app.api.v1.evaluations import get_chart_data
    from app.models.user import User
    from app.services.evaluation_engine import _compute_run_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, run = await _seed_judge_off_run(session, [{}, {}])
            admin = User(
                email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password="x",
                full_name="Admin",
                role="admin",  # `is_admin` is a derived property, not a column
            )
            session.add(admin)
            await session.flush()
            await _compute_run_headlines(session, evaluation.id)
            await session.commit()
            await session.refresh(run)

            assert run.score is None and run.quality_score is None

            chart = await get_chart_data(str(evaluation.id), user=admin, db=session)

        for name, points in chart["trends"].items():
            assert all(not p["scores"] for p in points), (
                f"no headline must mean no point on the {name} trend"
            )
        for name, points in chart["correlations"].items():
            assert points == [], f"no headline must mean no {name} point"
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_judge_on_run_still_reports_its_judge_headline():
    """The gate must not silence a judge that genuinely ran."""
    from app.services.evaluation_engine import _compute_run_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, run = await _seed_judge_off_run(
                session, [{QUALITY_HIGHER: 0.9}, {QUALITY_HIGHER: 0.8}]
            )
            evaluation.scorer_selection = ["judge"]
            # Rewriting the entry scores simulates a rescore under the judge, which
            # also stamps the run's own selection: the headline pass reads that
            # snapshot, not the evaluation's.
            run.scorer_selection = ["judge"]
            for result in (
                await session.execute(
                    select(EvaluationResult).where(EvaluationResult.run_id == run.id)
                )
            ).scalars():
                result.score = 80.0
            await session.flush()
            await _compute_run_headlines(session, evaluation.id)
            await session.commit()
            await session.refresh(run)

        assert run.score == 80.0
    finally:
        await cleanup()


# ---------------------------------------------------------------------------
# Overall Performance: one basis per evaluation
# ---------------------------------------------------------------------------


async def _seed_perf_run(session, evaluation, agent, latencies: list[float]):
    """Add one completed run carrying a lower-is-better performance metric."""
    run = EvaluationRun(
        evaluation_id=evaluation.id,
        agent_id=agent.id,
        model_id=f"model-{uuid.uuid4().hex[:6]}",
        trigger_reason="manual",
        status="completed",
        score=None,
    )
    session.add(run)
    await session.flush()
    for latency in latencies:
        result = EvaluationResult(run_id=run.id, agent_output="", score=0.0)
        session.add(result)
        await session.flush()
        session.add(
            EvaluationMetric(
                run_id=run.id, result_id=result.id, key=PERF_LOWER, value=latency
            )
        )
    await session.flush()
    return run


@pytest.mark.integration
@pytest.mark.asyncio
async def test_rescoring_one_run_does_not_raise_it_to_the_top_of_the_scale():
    """A rescored run must not be normalised against itself.

    Against itself it is both the fastest and the slowest, and a ``t_max <= t_min``
    branch would hand it a perfect 100. The slowest run must stay the slowest.
    """
    from app.services.evaluation_engine import _compute_run_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(name="Eval", scorer_selection=[])
            session.add_all([agent, evaluation])
            await session.flush()

            fast = await _seed_perf_run(session, evaluation, agent, [1.0, 1.0])
            slow = await _seed_perf_run(session, evaluation, agent, [10.0, 10.0])
            await _compute_run_headlines(session, evaluation.id)
            await session.commit()

            # Now rescore ONLY the slow run, as `rescore_run` does.
            await _compute_run_headlines(
                session, evaluation.id, batch_run_ids=[str(slow.id)]
            )
            await session.commit()
            await session.refresh(fast)
            await session.refresh(slow)

        assert fast.performance_score == 100.0
        assert slow.performance_score is not None
        assert slow.performance_score < 100.0, (
            "a single-run rescore must not award the top of the scale"
        )
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_second_batch_rescores_the_first_against_one_basis():
    """A second batch rescores the first against one shared basis.

    Scoping the basis to the trigger batch would give two runs of one evaluation
    performance numbers that are not comparable, displayed side by side.
    """
    from app.services.evaluation_engine import _compute_run_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(name="Eval", scorer_selection=[])
            session.add_all([agent, evaluation])
            await session.flush()

            first = await _seed_perf_run(session, evaluation, agent, [5.0, 5.0])
            await _compute_run_headlines(
                session, evaluation.id, batch_run_ids=[str(first.id)]
            )
            await session.commit()
            await session.refresh(first)
            # Alone, it has nothing to be compared against.
            assert first.performance_score is None

            second = await _seed_perf_run(session, evaluation, agent, [1.0, 1.0])
            await _compute_run_headlines(
                session, evaluation.id, batch_run_ids=[str(second.id)]
            )
            await session.commit()
            await session.refresh(first)
            await session.refresh(second)

        # The FIRST batch's run was rescored by the SECOND batch's completion.
        assert second.performance_score == 100.0
        assert first.performance_score is not None
        assert first.performance_score < 100.0
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deleting_a_run_rescores_every_survivor():
    """Deleting a run rescores every survivor against the new basis.

    Otherwise the whole evaluation reads against a deleted run.
    """
    from app.services.evaluation_engine import _compute_run_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(name="Eval", scorer_selection=[])
            session.add_all([agent, evaluation])
            await session.flush()

            fastest = await _seed_perf_run(session, evaluation, agent, [1.0, 1.0])
            middle = await _seed_perf_run(session, evaluation, agent, [5.0, 5.0])
            slowest = await _seed_perf_run(session, evaluation, agent, [10.0, 10.0])
            await _compute_run_headlines(session, evaluation.id)
            await session.commit()
            await session.refresh(middle)
            before = middle.performance_score
            assert before is not None and before < 100.0

            await session.delete(fastest)
            await session.commit()
            await _compute_run_headlines(session, evaluation.id)
            await session.commit()
            await session.refresh(middle)
            await session.refresh(slowest)

        # With the fastest gone, the middle run IS the fastest survivor.
        assert middle.performance_score == 100.0
        assert slowest.performance_score is not None
        assert slowest.performance_score < 100.0
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_reused_prediction_still_contributes_to_the_performance_comparison():
    """A reused prediction keeps its performance metrics and stays in the comparison.

    Without ``performance.*`` it would contribute no key to the shared intersection:
    its headline would read N/A and every other run would be normalised against a set
    it had silently left. The measurements describe the very execution being reused,
    so they are kept and the reuse is disclosed as provenance.
    """
    from app.services.evaluation.metrics import log_result_metrics

    details = {
        "metric_results": {PERF_LOWER: {"status": "scored", "value": 3.0}},
        "ttft_ms": 400,
    }
    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(name="Eval", scorer_selection=[])
            session.add_all([agent, evaluation])
            await session.flush()
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="reused",
                trigger_reason="manual",
                status="completed",
            )
            session.add(run)
            await session.flush()
            result = EvaluationResult(
                run_id=run.id, agent_output="", score=0.0, is_reused=True
            )
            session.add(result)
            await session.flush()
            await log_result_metrics(
                session,
                result_id=result.id,
                run_id=run.id,
                execution_details=details,
                is_reused=True,
            )
            await session.commit()

            keys = {
                row.key
                for row in (
                    await session.execute(
                        select(EvaluationMetric).where(EvaluationMetric.run_id == run.id)
                    )
                )
                .scalars()
                .all()
            }

        assert PERF_LOWER in keys, (
            "a reused prediction keeps its performance measurements"
        )
        # The agent-level legacy keys are still omitted: the agent was not re-run.
        assert "ttft_seconds" not in keys
    finally:
        await cleanup()


# ---------------------------------------------------------------------------
# Six series, one window
# ---------------------------------------------------------------------------


async def _make_admin(session):
    from app.models.user import User

    admin = User(
        email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name="Admin",
        role="admin",  # `is_admin` is a derived property, not a column
    )
    session.add(admin)
    await session.flush()
    return admin


async def _seed_headline_run(
    session,
    evaluation,
    agent,
    *,
    quality=None,
    performance=None,
    judge=None,
    created_at=None,
    model_id=None,
):
    """A completed run with headline values written directly.

    The chart reads persisted headlines and never recomputes them, so
    seeding the columns is exactly what it consumes.
    """
    run = EvaluationRun(
        evaluation_id=evaluation.id,
        agent_id=agent.id,
        model_id=model_id or f"model-{uuid.uuid4().hex[:6]}",
        trigger_reason="manual",
        status="completed",
        score=judge,
        quality_score=quality,
        performance_score=performance,
    )
    session.add(run)
    await session.flush()
    if created_at is not None:
        run.created_at = created_at
        await session.flush()
    return run


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_chart_payload_carries_all_six_series():
    """The chart payload carries three trends and three correlation datasets."""
    from app.api.v1.evaluations import get_chart_data

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(name="Eval")
            session.add_all([agent, evaluation])
            await session.flush()
            admin = await _make_admin(session)
            await _seed_headline_run(
                session, evaluation, agent, quality=80.0, performance=60.0, judge=70.0
            )
            await session.commit()

            chart = await get_chart_data(str(evaluation.id), user=admin, db=session)

        assert set(chart["trends"]) == {"quality", "performance", "judge"}
        assert set(chart["correlations"]) == {
            "quality_performance",
            "judge_performance",
            "quality_judge",
        }
        assert all(chart["trends"][k] for k in chart["trends"])
        assert all(chart["correlations"][k] for k in chart["correlations"])
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_correlation_point_never_mixes_axis_values_from_two_runs():
    """Both axes come from the pair's LATEST run, or the point is dropped.

    Taking each axis from whichever run happened to have it plots a pair that
    never existed: a quality value one run achieved next to a performance value
    a different run achieved, presented as one measurement.
    """
    from app.api.v1.evaluations import get_chart_data

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(name="Eval")
            session.add_all([agent, evaluation])
            await session.flush()
            admin = await _make_admin(session)
            # Same agent/model pair, twice. The OLD run has both axes; the
            # LATEST has only quality.
            await _seed_headline_run(
                session,
                evaluation,
                agent,
                model_id="gpt-4o",
                quality=10.0,
                performance=99.0,
                created_at=datetime.now(UTC) - timedelta(days=2),
            )
            await _seed_headline_run(
                session, evaluation, agent, model_id="gpt-4o", quality=80.0
            )
            await session.commit()

            chart = await get_chart_data(str(evaluation.id), user=admin, db=session)

        # The pair is omitted entirely rather than borrowing 99.0 from the run
        # that is no longer the latest.
        assert chart["correlations"]["quality_performance"] == []
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_range_bounds_all_three_trends_identically_and_leaves_correlations_alone():
    """One window bounds all three trends; the correlations are never windowed.

    Reading the three headlines over different windows makes a false comparison
    between them. Windowing the correlations would be
    a different error: a point is defined as a pair's latest run, so a filter
    drops whole pairs rather than shortening a history.
    """
    from app.api.v1.evaluations import get_chart_data

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(name="Eval")
            session.add_all([agent, evaluation])
            await session.flush()
            admin = await _make_admin(session)
            await _seed_headline_run(
                session,
                evaluation,
                agent,
                model_id="old",
                quality=50.0,
                performance=50.0,
                judge=50.0,
                created_at=datetime.now(UTC) - timedelta(days=200),
            )
            await _seed_headline_run(
                session,
                evaluation,
                agent,
                model_id="recent",
                quality=80.0,
                performance=80.0,
                judge=80.0,
                created_at=datetime.now(UTC) - timedelta(days=10),
            )
            await session.commit()

            default = await get_chart_data(str(evaluation.id), user=admin, db=session)
            everything = await get_chart_data(
                str(evaluation.id), range="all", user=admin, db=session
            )
            narrow = await get_chart_data(
                str(evaluation.id), range="30d", user=admin, db=session
            )

        # Omitting the range applies 90d, NOT all history.
        assert default["range"] == "90d"
        assert default["runs_in_range"] == 1
        assert everything["runs_in_range"] == 2
        assert narrow["runs_in_range"] == 1
        # All three trends are bounded identically.
        for name in ("quality", "performance", "judge"):
            assert len(default["trends"][name]) == 1
            assert len(everything["trends"][name]) == 2
        # Correlations ignore the window entirely.
        for chart in (default, everything, narrow):
            assert len(chart["correlations"]["quality_performance"]) == 2
        # The total is reported so "filtered out" is distinguishable from
        # "never run".
        assert default["total_runs"] == 2
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_range_matching_no_runs_returns_empty_trends_not_an_error():
    """A range matching no runs returns empty trends, not an error."""
    from app.api.v1.evaluations import get_chart_data

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(name="Eval")
            session.add_all([agent, evaluation])
            await session.flush()
            admin = await _make_admin(session)
            await _seed_headline_run(
                session,
                evaluation,
                agent,
                quality=50.0,
                created_at=datetime.now(UTC) - timedelta(days=400),
            )
            await session.commit()

            chart = await get_chart_data(
                str(evaluation.id), range="30d", user=admin, db=session
            )

        assert chart["trends"]["quality"] == []
        assert chart["runs_in_range"] == 0
        # ...but the evaluation is visibly non-empty, which is what makes the
        # empty chart legible as a filter rather than as "never run".
        assert chart["total_runs"] == 1
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_invalid_range_is_refused_rather_than_silently_widened():
    from fastapi import HTTPException

    from app.api.v1.evaluations import get_chart_data

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation = Evaluation(name="Eval")
            session.add(evaluation)
            await session.flush()
            admin = await _make_admin(session)
            await session.commit()

            with pytest.raises(HTTPException) as exc:
                await get_chart_data(
                    str(evaluation.id), range="7d", user=admin, db=session
                )
        assert exc.value.status_code == 400
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_pre_change_run_is_never_ranked_on_its_displayed_legacy_fallback():
    """The legacy fallback is display-only and never used for ranking.

    A historical run's `speed_score` was normalised against whatever narrow set
    existed at the time. Ranking it beside values normalised across the whole
    evaluation compares two different scales and reports the result as an
    ordering.
    """
    from app.api.v1.evaluations import get_chart_data

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(name="Eval")
            session.add_all([agent, evaluation])
            await session.flush()
            admin = await _make_admin(session)
            historical = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="legacy",
                trigger_reason="manual",
                status="completed",
                speed_score=100.0,  # displayed, but never ranked
            )
            session.add(historical)
            await _seed_headline_run(
                session, evaluation, agent, model_id="current", performance=42.0
            )
            await session.commit()

            chart = await get_chart_data(
                str(evaluation.id), range="all", user=admin, db=session
            )

        plotted = {
            k: v
            for p in chart["trends"]["performance"]
            for k, v in p["scores"].items()
        }
        assert list(plotted.values()) == [42.0]
        assert not any("legacy" in k for k in plotted), (
            "a legacy speed_score must never enter the performance ranking"
        )
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_three_genuinely_different_leaders_are_all_surfaced():
    """A leader per headline, and no single overall winner.

    Three runs, each best on exactly one headline. The data the summary reads
    must be able to name three different leaders; a single winner would pick one
    of them over the other two, which is a blended score again.
    """
    from app.api.v1.evaluations import get_chart_data

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation = Evaluation(name="Eval")
            fast = Agent(name="Fast but weak")
            accurate = Agent(name="Slow but accurate")
            graded = Agent(name="Judge favourite")
            session.add_all([evaluation, fast, accurate, graded])
            await session.flush()
            admin = await _make_admin(session)
            await _seed_headline_run(
                session, evaluation, fast, quality=40.0, performance=100.0, judge=50.0
            )
            await _seed_headline_run(
                session, evaluation, accurate, quality=95.0, performance=20.0, judge=60.0
            )
            await _seed_headline_run(
                session, evaluation, graded, quality=55.0, performance=45.0, judge=99.0
            )
            await session.commit()

            chart = await get_chart_data(str(evaluation.id), user=admin, db=session)

        # Each headline's best value belongs to a different pair. The three
        # correlation datasets carry per-pair values, which is what the ranked
        # lists and leader cards read from.
        best_on = {}
        for name, axis in (
            ("quality_performance", "x"),
            ("quality_performance", "y"),
            ("judge_performance", "x"),
        ):
            points = chart["correlations"][name]
            top = max(points, key=lambda pt: pt[axis])
            best_on[(name, axis)] = top["series"]

        quality_leader = best_on[("quality_performance", "x")]
        performance_leader = best_on[("quality_performance", "y")]
        judge_leader = best_on[("judge_performance", "x")]
        assert len({quality_leader, performance_leader, judge_leader}) == 3

        # Nothing in the payload asserts an overall winner: there is no blended
        # field to rank on.
        assert "overall" not in chart["trends"]
        assert not any(
            "overall" in point for points in chart["correlations"].values() for point in points
        )
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_pair_missing_one_axis_is_counted_as_omitted_not_plotted():
    """A pair missing one axis is disclosed as omitted, never pinned to zero.

    A shortened view with no explanation reads as fewer runs rather than fewer
    measurements, which sends the reader looking for runs that did happen.
    """
    from app.api.v1.evaluations import get_chart_data

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation = Evaluation(name="Eval")
            complete = Agent(name="Complete")
            partial = Agent(name="No performance")
            session.add_all([evaluation, complete, partial])
            await session.flush()
            admin = await _make_admin(session)
            await _seed_headline_run(
                session, evaluation, complete, quality=80.0, performance=60.0, judge=70.0
            )
            await _seed_headline_run(
                session, evaluation, partial, quality=90.0, performance=None, judge=75.0
            )
            await session.commit()

            chart = await get_chart_data(str(evaluation.id), user=admin, db=session)

        # Quality × Performance drops the pair with no performance value...
        assert len(chart["correlations"]["quality_performance"]) == 1
        assert chart["correlations_omitted"]["quality_performance"] == 1
        assert all(pt["y"] != 0 for pt in chart["correlations"]["quality_performance"])
        # ...while Quality × Judge, whose axes it does have, still plots it.
        assert len(chart["correlations"]["quality_judge"]) == 2
        assert chart["correlations_omitted"]["quality_judge"] == 0
    finally:
        await cleanup()


async def _seed_quality_run(session, evaluation, agent, values: dict[str, float]):
    """One completed run carrying a fixed value for each named quality metric."""
    run = EvaluationRun(
        evaluation_id=evaluation.id,
        agent_id=agent.id,
        model_id=f"model-{uuid.uuid4().hex[:6]}",
        trigger_reason="manual",
        status="completed",
        score=None,
    )
    session.add(run)
    await session.flush()
    result = EvaluationResult(run_id=run.id, agent_output="", score=0.0)
    session.add(result)
    await session.flush()
    for key, value in values.items():
        session.add(
            EvaluationMetric(run_id=run.id, result_id=result.id, key=key, value=value)
        )
    await session.flush()
    return run


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_selection_round_trips_through_all_three_states():
    """`null`, `[]` and a subset must persist distinctly.

    The revert path is the one that breaks silently. Applied inside the
    `if value is not None` field loop, an explicit `null` is swallowed as "no
    change", so an author can narrow a headline and can never widen it again —
    a one-way door that looks like a saved form.
    """
    from app.api.v1.evaluations import update_evaluation
    from app.api.v1.schemas.evaluation import UpdateEvaluationRequest

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation = Evaluation(name="Eval", family="structured_json")
            session.add(evaluation)
            await session.flush()
            admin = await _make_admin(session)
            await session.commit()

            assert evaluation.overall_quality_metrics is None

            async def put(**fields):
                await update_evaluation(
                    str(evaluation.id),
                    UpdateEvaluationRequest(**fields),
                    user=admin,
                    db=session,
                )
                await session.refresh(evaluation)

            # null -> [] : compose from nothing, headline reads N/A.
            await put(overall_quality_metrics=[])
            assert evaluation.overall_quality_metrics == []

            # [] -> subset : refine.
            await put(overall_quality_metrics=[QUALITY_HIGHER])
            assert evaluation.overall_quality_metrics == [QUALITY_HIGHER]

            # subset -> null : back to inherit-all. This is the assertion the
            # one-way door fails.
            await put(overall_quality_metrics=None)
            assert evaluation.overall_quality_metrics is None

            # An omitted field is left alone, which is what makes the explicit
            # `null` above meaningful rather than indistinguishable from it.
            await put(overall_performance_metrics=[PERF_LOWER])
            await put(name="Renamed")
            assert evaluation.overall_performance_metrics == [PERF_LOWER]
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_changing_the_selection_recomposes_without_re_running_anything():
    """Selection is read-time only: changing it recomposes without re-running.

    Every metric the family measures stays computed and stored whatever the
    selection says; the selection only governs which of them compose the
    headline. If it gated measurement, narrowing a headline would destroy the
    data needed to widen it again.
    """
    from app.api.v1.evaluations import update_evaluation
    from app.api.v1.schemas.evaluation import UpdateEvaluationRequest

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(
                name="Eval", family="structured_json", scorer_selection=[]
            )
            session.add_all([agent, evaluation])
            await session.flush()
            admin = await _make_admin(session)
            run = await _seed_quality_run(
                session, evaluation, agent, {QUALITY_HIGHER: 100.0, QUALITY_SECOND: 0.0}
            )
            await session.commit()

            stored_before = (
                await session.execute(
                    select(EvaluationMetric).where(EvaluationMetric.run_id == run.id)
                )
            ).scalars().all()

            # Compose from the higher-is-better metric alone: 100 -> 100.
            await update_evaluation(
                str(evaluation.id),
                UpdateEvaluationRequest(overall_quality_metrics=[QUALITY_HIGHER]),
                user=admin,
                db=session,
            )
            await session.refresh(run)
            assert run.quality_score == 100.0

            # Add the second metric, which scored 0: the headline moves without a
            # single agent call.
            await update_evaluation(
                str(evaluation.id),
                UpdateEvaluationRequest(
                    overall_quality_metrics=[QUALITY_HIGHER, QUALITY_SECOND]
                ),
                user=admin,
                db=session,
            )
            await session.refresh(run)
            assert run.quality_score == 50.0

            stored_after = (
                await session.execute(
                    select(EvaluationMetric).where(EvaluationMetric.run_id == run.id)
                )
            ).scalars().all()
            assert {(m.key, m.value) for m in stored_after} == {
                (m.key, m.value) for m in stored_before
            }
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_changing_the_spread_penalty_recomposes_the_headlines():
    """The judge's score and Overall Quality average their entries with the spread
    penalty (``mean - k * std``), so saving a new penalty recomputes both from the
    stored entry values, like a tolerance or a selection change. A save
    that resends the stored penalty and tolerance, as the Configuration tab does on
    every save, recomputes nothing: the stored values stay as they were. Two entries
    scored 100 and 60 by the judge and on the quality metric: mean 80, spread 20."""
    from app.api.v1.evaluations import update_evaluation
    from app.api.v1.schemas.evaluation import UpdateEvaluationRequest

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(
                name="Eval",
                family="structured_json",
                penalty_factor=0.5,
                overall_quality_metrics=[QUALITY_HIGHER],
            )
            session.add_all([agent, evaluation])
            await session.flush()
            admin = await _make_admin(session)
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="m",
                trigger_reason="manual",
                status="completed",
                # Not what the stored entries give: a recompute would show.
                score=99.0,
                quality_score=99.0,
            )
            session.add(run)
            await session.flush()
            for value in (100.0, 60.0):
                result = EvaluationResult(run_id=run.id, agent_output="", score=value)
                session.add(result)
                await session.flush()
                session.add(EvaluationMetric(run_id=run.id, result_id=result.id, key=QUALITY_HIGHER, value=value))
            await session.commit()

            async def save(**fields) -> tuple[float | None, float | None]:
                await update_evaluation(
                    str(evaluation.id), UpdateEvaluationRequest(**fields), user=admin, db=session
                )
                await session.refresh(run)
                return run.score, run.quality_score

            assert await save(penalty_factor=0.5, performance_tolerance=5.0) == (99.0, 99.0)
            assert await save(penalty_factor=1.0, performance_tolerance=5.0) == (60.0, 60.0)
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_metric_that_stops_contributing_loses_its_entry_count():
    """``metric_entry_counts`` counts the entries of the metrics composing each
    headline as it stands: a metric narrowed out of either headline loses its count,
    and the other headline's counts stay."""
    from app.api.v1.evaluations import update_evaluation
    from app.api.v1.schemas.evaluation import UpdateEvaluationRequest

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(name="Eval", family="structured_json", scorer_selection=[])
            session.add_all([agent, evaluation])
            await session.flush()
            admin = await _make_admin(session)
            # Two runs: Overall Performance compares a run with its siblings.
            run, _sibling = [
                await _seed_quality_run(
                    session, evaluation, agent, {QUALITY_HIGHER: 100.0, QUALITY_SECOND: 20.0, PERF_LOWER: seconds}
                )
                for seconds in (3.0, 6.0)
            ]
            await session.commit()

            async def counts_after(**fields) -> dict[str, int]:
                await update_evaluation(
                    str(evaluation.id), UpdateEvaluationRequest(**fields), user=admin, db=session
                )
                await session.refresh(run)
                return run.headline_details["metric_entry_counts"]

            assert await counts_after(
                overall_quality_metrics=[QUALITY_HIGHER, QUALITY_SECOND], overall_performance_metrics=[PERF_LOWER]
            ) == {QUALITY_HIGHER: 1, QUALITY_SECOND: 1, PERF_LOWER: 1}
            assert await counts_after(overall_quality_metrics=[QUALITY_HIGHER]) == {QUALITY_HIGHER: 1, PERF_LOWER: 1}
            assert await counts_after(overall_performance_metrics=[]) == {QUALITY_HIGHER: 1}
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_selection_semantics_null_empty_subset_and_unknown_keys():
    """The four selection outcomes.

    `null` inherits all, `[]` yields N/A, a subset refines, and a key the family
    never measures is ignored rather than raising: an evaluation whose family
    changed would otherwise become unreadable rather than merely differently
    composed.
    """
    from app.api.v1.evaluations import update_evaluation
    from app.api.v1.schemas.evaluation import UpdateEvaluationRequest

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(
                name="Eval", family="structured_json", scorer_selection=[]
            )
            session.add_all([agent, evaluation])
            await session.flush()
            admin = await _make_admin(session)
            run = await _seed_quality_run(
                session, evaluation, agent, {QUALITY_HIGHER: 100.0, QUALITY_SECOND: 0.0}
            )
            await session.commit()

            async def recompose(selection):
                await update_evaluation(
                    str(evaluation.id),
                    UpdateEvaluationRequest(overall_quality_metrics=selection),
                    user=admin,
                    db=session,
                )
                await session.refresh(run)
                return run.quality_score

            # subset: refines to the one named.
            assert await recompose([QUALITY_HIGHER]) == 100.0
            # unknown key alongside a real one: ignored, not an error.
            assert await recompose([QUALITY_HIGHER, "nope.not_a_metric"]) == 100.0
            # []: composes from nothing, so the headline is N/A — never 0.
            assert await recompose([]) is None
            # null: back to the default contributors, both measured here — 100 and 0
            # average to 50.
            # Deliberately last, because it is the revert that a selection kept
            # inside the `if value is not None` loop can never reach.
            assert await recompose(None) == 50.0
            # An unknown key ALONE is not the same as `null`: it composes from
            # nothing measurable, so the headline is N/A rather than inheriting.
            assert await recompose(["nope.not_a_metric"]) is None
    finally:
        await cleanup()


# --- Package quality metrics are 0-100, not 0-1 ---------------------------------------
#
# A STIX_BUNDLE evaluation scored every family metric yet read Overall Quality N/A:
# ``ScoreResult`` keeps a value in ``[0, 100]``, so a 0-1 range guard drops every real
# quality metric, and 0-1 fixtures hid it. These use values observed in a real run.

_PROSE_TOKEN_PRF = "prose.token_prf"            # higher_is_better, averaged


@pytest.mark.parametrize(
    ("key", "values", "expected"),
    [
        (QUALITY_HIGHER, [100.0, 100.0], 100.0),
        # A perfect, perfectly consistent metric is 100 -- not 10000, and not dropped.
        (_PROSE_TOKEN_PRF, [35.09, 27.78], 29.61),
        (_PROSE_TOKEN_PRF, [85.0], 85.0),
    ],
    ids=["package-maximum", "observed-spread", "single-entry"],
)
def test_quality_values_are_kept_on_the_package_score_range(key, values, expected) -> None:
    out = reduce_grouped_metrics({key: values}, penalty_factor=0.5)
    assert set(out) == {key}
    assert out[key].value == expected
    assert out[key].entry_count == len(values)


@pytest.mark.parametrize(
    ("values", "expected"),
    [([0.0, 0.0], 100.0), ([100.0, 100.0], 0.0), ([10.0], 90.0)],
    ids=["no-mistakes", "all-mistakes", "ten-percent-mistakes"],
)
def test_a_lower_is_better_quality_metric_is_oriented_not_averaged_raw(
    monkeypatch, values, expected
) -> None:
    """A lower_is_better quality metric: more mistakes must score LOWER.

    Averaging it raw would invert Overall Quality: a run that makes more mistakes would
    rank higher. 10 mistakes out of 100 is a good run: it reads 90, not -900.
    """
    _declare_lower_is_better(monkeypatch, QUALITY_FLIPPED)
    out = reduce_grouped_metrics({QUALITY_FLIPPED: values}, penalty_factor=0.5)
    assert out[QUALITY_FLIPPED].value == expected



# --- Per-metric contributions (merged Metrics section) --------------------------------


def test_combine_headline_reports_each_contributing_value():
    """The headline must disclose *what each metric contributed*, not just which ones.

    The Metrics panel shows raw means in native units, which do not reproduce the
    headline (a quality mean-of-means is not the mean of the penalised values). Without
    the contributed value the two numbers sit side by side and silently disagree.
    """
    headline = combine_headline(
        {"stix.entity_prf": 29.61, "stix.envelope_validity": 100.0},
        None,
    )
    assert headline.contributions == {
        "stix.entity_prf": 29.61,
        "stix.envelope_validity": 100.0,
    }


def test_combine_headline_contributions_cover_only_selected_metrics():
    """A measured-but-deselected metric contributed nothing and must not be listed."""
    headline = combine_headline(
        {"a": 10.0, "b": 20.0, "c": 30.0},
        ["a", "c"],
    )
    assert headline.contributions == {"a": 10.0, "c": 30.0}


def test_combine_headline_contributions_empty_when_not_applicable():
    """An explicitly emptied selection contributes nothing at all."""
    assert combine_headline({"a": 10.0}, []).contributions == {}


# --- Default headline composition ----------------------------------------------


def _resolve_default(available):
    from app.services.evaluation.aggregation import resolve_default_quality_selection

    return resolve_default_quality_selection(set(available))


@pytest.mark.parametrize("family", sorted(FAMILY_IDS))
def test_the_default_selection_is_each_family_s_answer_measures(family: str) -> None:
    """Every family's unconfigured headline counts its default contributors and nothing else.

    Every family's headline flows through this one code path, so the cross-family
    regression argument is asserted rather than reasoned about.
    """
    from app.services.evaluation.metric_package.catalog import (
        DEFAULT_QUALITY_KEYS,
        quality_catalog,
    )

    keys = {metric["key"] for metric in quality_catalog(family)}
    defaults = keys & DEFAULT_QUALITY_KEYS
    assert defaults, f"{family} has no default contributor: its unconfigured headline is N/A"

    assert _resolve_default(keys) == sorted(defaults)
    # End to end: a non-default metric at 0 does not move the headline.
    metrics = {key: (50.0 if key in defaults else 0.0) for key in keys}
    assert combine_headline(metrics, _resolve_default(keys)).value == 50.0


def test_prose_default_headline_is_token_prf_alone() -> None:
    """Exact match and ROUGE-L are advertised and scored, but not default contributors."""
    metrics = {"prose.exact_match": 0.0, "prose.token_prf": 80.0, "prose.rouge_l": 60.0}
    headline = combine_headline(metrics, _resolve_default(metrics))

    assert headline.contributing_keys == ["prose.token_prf"]
    assert headline.value == 80.0


def test_rouge_l_is_opt_in() -> None:
    """ROUGE-L is capped by token F1 and penalises a faithful reordering:
    not a default contributor, still selectable."""
    assert _resolve_default({"prose.rouge_l"}) == []
    assert combine_headline({"prose.rouge_l": 42.0}, ["prose.rouge_l"]).value == 42.0


def test_a_prose_headline_exposes_its_per_key_composition_to_the_ui() -> None:
    """The data the run detail page renders as the headline's breakdown.

    ``contributions`` is persisted as ``headline_details.quality_contributions`` and
    shown per key by ``MetricRowsTable``. Without it an administrator sees the number but
    not which metrics produced it, so exact match's absence from the default would be
    invisible.
    """
    metrics = {"prose.exact_match": 0.0, "prose.token_prf": 80.0, "prose.rouge_l": 60.0}
    headline = combine_headline(metrics, _resolve_default(metrics))

    assert headline.contributions == {"prose.token_prf": 80.0}
    # The excluded metric is absent from the breakdown rather than shown as a zero.
    assert "prose.exact_match" not in headline.contributions

    # Opting it in makes it visible in the same breakdown, without special casing.
    opted_in = combine_headline(metrics, [*_resolve_default(metrics), "prose.exact_match"])
    assert opted_in.contributions["prose.exact_match"] == 0.0


def test_a_default_that_resolves_to_nothing_reads_not_applicable() -> None:
    """Never a fallback to every metric, and never a zero.

    Reached when the only metric that scored on a run is a non-default one. Widening back
    to "everything available" here would restore exact match to the headline on exactly the
    runs where it is the only signal.
    """
    metrics = {"prose.exact_match": 0.0}
    resolved = _resolve_default(metrics)
    assert resolved == []

    headline = combine_headline(metrics, resolved)
    assert headline.value is None
    assert headline.value != 0.0
    assert headline.contributing_keys == []


def test_explicitly_selecting_exact_match_puts_it_back_at_equal_weight() -> None:
    """Nothing is removed; the default merely leaves it out."""
    metrics = {"prose.exact_match": 0.0, "prose.token_prf": 90.0, "prose.rouge_l": 90.0}
    headline = combine_headline(metrics, sorted(metrics))
    assert headline.contributing_keys == sorted(metrics)
    assert headline.value == 60.0

    # An explicitly emptied selection still reads N/A rather than zero.
    assert combine_headline(metrics, []).value is None


def test_a_stored_selection_naming_a_metric_that_never_scores_is_harmless() -> None:
    """A selected key with no value must not be able to break a headline.

    A selection saved before a metric was hidden (the phrase metrics) or removed
    still names it. That must cost nothing: `combine_headline` intersects the selection
    with the keys actually present, so a metric that never scores contributes nothing
    rather than zeroing the mean or making it uncomputable.
    """
    metrics = {"prose.token_prf": 80.0, "prose.rouge_l": 60.0}
    selection = [
        "prose.token_prf",
        "prose.rouge_l",
        "prose.required_phrase_coverage",  # never scores today
    ]
    headline = combine_headline(metrics, selection)

    assert headline.value == 70.0  # the mean of the two that DID score, not of three
    assert headline.contributing_keys == ["prose.rouge_l", "prose.token_prf"]

    # And selecting only never-scoring metrics reads N/A, never zero.
    only_skipped = combine_headline(metrics, ["prose.required_phrase_coverage"])
    assert only_skipped.value is None


def test_exclusion_disclosure_uses_the_stored_selection_not_the_derived_default() -> None:
    """A derived key must never be reported as something the author excluded.

    ``classify_exclusions`` is only ever called with the stored value; this pins the
    contract at the unit level so a future refactor cannot quietly pass the resolved
    selection instead.
    """
    available = {"prose.token_prf"}
    stored = ["prose.token_prf", "prose.rouge_l"]

    disclosed = classify_exclusions(stored, available, {})
    assert {item.key for item in disclosed} == {"prose.rouge_l"}

    # Nothing derived is disclosed when the author stored no selection at all.
    assert classify_exclusions(None, available, {}) == []


# --- Default-omission disclosure ------------------------------------------------


def _disclose_default(available):
    from app.services.evaluation.aggregation import classify_default_omissions

    return classify_default_omissions(set(available))


def test_a_headline_that_reads_na_says_which_metric_it_did_not_count() -> None:
    """A default-resolved N/A names the metric it left out, though that metric scored.

    ``classify_exclusions`` cannot reach this case -- it keys on the author's stored
    selection and returns nothing when there is none, which is precisely when the default
    resolution does the narrowing.
    """
    metrics = {"prose.exact_match": 100.0}
    headline = combine_headline(metrics, _resolve_default(metrics), _disclose_default(metrics))

    assert headline.value is None  # the disclosure explains the N/A, it does not remove it
    assert headline.contributing_keys == []
    assert [(item.key, item.reason) for item in headline.excluded] == [
        ("prose.exact_match", REASON_NOT_A_DEFAULT_CONTRIBUTOR)
    ]


def test_a_scored_headline_still_discloses_the_metrics_it_left_out() -> None:
    """Disclosure is not conditional on the headline being N/A."""
    metrics = {"prose.exact_match": 100.0, "prose.token_prf": 80.0}
    headline = combine_headline(metrics, _resolve_default(metrics), _disclose_default(metrics))

    assert headline.value == 80.0
    assert headline.contributing_keys == ["prose.token_prf"]
    assert {item.key for item in headline.excluded} == {"prose.exact_match"}


def test_a_run_that_measured_nothing_discloses_nothing() -> None:
    """Distinguishable from a run whose only values came from non-default metrics.

    Both read N/A. Only one of them has something to explain, and inventing an omission for
    the other would describe a metric that never ran.
    """
    assert _disclose_default(set()) == []
    empty = combine_headline({}, _resolve_default(set()), _disclose_default(set()))
    assert empty.value is None
    assert empty.excluded == []


def test_a_failed_non_default_metric_is_reported_as_failed_under_a_selection() -> None:
    """Selection branch: the actionable reason wins."""
    disclosed = classify_exclusions(
        ["prose.exact_match"], set(), {uuid.uuid4(): {"prose.exact_match": "invalid"}}
    )
    assert [(item.key, item.reason) for item in disclosed] == [
        ("prose.exact_match", REASON_FAILED)
    ]


def test_a_failed_non_default_metric_is_not_disclosed_without_a_selection() -> None:
    """No-selection branch: it never entered the scored set, so it is not a candidate.

    Failure is only reported against a selection. Pinning the real outcome makes
    widening the candidate set beyond the scored metrics break a test rather than a
    user's trust.
    """
    assert _disclose_default(set()) == []  # a failed metric never reaches `available`
    assert _disclose_default({"prose.token_prf"}) == []  # nor does a default one that scored


def test_the_new_reason_is_never_emitted_when_a_selection_exists() -> None:
    """The two disclosure paths are mutually exclusive."""
    available = {"prose.exact_match", "prose.token_prf"}
    disclosed = classify_exclusions(["prose.token_prf", "prose.rouge_l"], available, {})

    assert REASON_NOT_A_DEFAULT_CONTRIBUTOR not in {item.reason for item in disclosed}
    assert {item.reason for item in disclosed} <= {REASON_FAILED, REASON_NOT_APPLICABLE}


def test_an_explicitly_empty_selection_produces_no_default_omission() -> None:
    """An empty selection is an author choice; the absence of one is not."""
    metrics = {"prose.exact_match": 100.0}
    headline = combine_headline(metrics, [], classify_exclusions([], set(metrics), {}))

    assert headline.value is None
    assert headline.excluded == []


def test_the_disclosure_follows_the_single_default_set() -> None:
    """Every quality metric outside the default set is disclosed, and no other key.

    Membership changes with one edit and no change to disclosure logic.
    """
    from app.services.evaluation.metric_package.catalog import (
        DEFAULT_QUALITY_KEYS,
        quality_catalog,
    )

    offered = {m["key"] for family in FAMILY_IDS for m in quality_catalog(family)}
    disclosed = {item.key for item in _disclose_default(offered)}
    assert disclosed == offered - DEFAULT_QUALITY_KEYS

    # A default contributor or a key that is not a quality metric is never disclosed.
    assert _disclose_default({"prose.token_prf", "stix_bundle.entity_f1", PERF_LOWER}) == []


@pytest.mark.parametrize("family", sorted(FAMILY_IDS))
def test_disclosure_never_moves_a_headline_for_any_family(family: str) -> None:
    """The disclosure never changes a headline's value or composition.

    Reusing the non-default intersection to widen the contributing keys would change
    the default headline composition. The disclosure is passed only as the opaque ``excluded`` argument.
    """
    from app.services.evaluation.metric_package.catalog import quality_catalog

    keys = {metric["key"] for metric in quality_catalog(family)}
    metrics = {key: 50.0 for key in keys}
    resolved = _resolve_default(keys)

    without = combine_headline(metrics, resolved)
    with_disclosure = combine_headline(metrics, resolved, _disclose_default(keys))

    assert with_disclosure.value == without.value
    assert with_disclosure.contributing_keys == without.contributing_keys
    assert with_disclosure.contributions == without.contributions


def test_the_performance_headline_never_carries_the_new_reason() -> None:
    """Performance has no default-resolution step, so it has nothing to disclose.

    Every performance metric is a default contributor. The classifier is called from the
    quality path only; this asserts the separation at the vocabulary level so wiring it
    into the performance headline "for symmetry" fails here.
    """
    from app.services.evaluation.metric_package.catalog import performance_catalog

    keys = {metric["key"] for metric in performance_catalog()}
    assert keys  # guard against an empty catalogue passing this vacuously
    assert _disclose_default(keys) == []


# --- Structured JSON: the per-field score IS the headline ----------------------


def test_json_default_headline_is_the_per_field_score_alone() -> None:
    """An unconfigured JSON evaluation averages nothing but `json.field_accuracy`.

    Schema validity is a gate, not a measure of answer quality, and averaging it in
    manufactures a number nobody would defend. The arithmetic below is the argument: an
    answer that conformed and got *every field wrong* would report 50 on an equal-weight
    mean of the two keys — a passing-looking headline for a completely wrong answer. The
    per-field score is 0.0, so the headline is 0.0.
    """
    metrics = {"json.schema_validity": 100.0, "json.field_accuracy": 0.0}
    headline = combine_headline(metrics, _resolve_default(metrics))

    assert _resolve_default(metrics) == ["json.field_accuracy"]
    assert headline.contributing_keys == ["json.field_accuracy"]
    assert headline.value == 0.0
    assert headline.value != pytest.approx(50.0)


def test_a_non_conforming_json_answer_reports_what_it_got_right() -> None:
    """The conformance bit must not drag the headline of a mostly-right answer.

    A prediction whose only fault is one off-enum value scores 75.0 per field while
    `json.schema_validity` is 0. Averaged equally the run would read 58.3 — a number that
    describes neither the answer's accuracy nor its conformance, but blends the two into
    something no reviewer can act on.
    """
    metrics = {"json.schema_validity": 0.0, "json.field_accuracy": 75.0}
    headline = combine_headline(metrics, _resolve_default(metrics))

    assert headline.value == 75.0
    assert headline.value != pytest.approx(37.5)


def test_json_schema_validity_stays_selectable_and_advertised() -> None:
    """Non-default is not hidden: an author who wants a conformance headline can ask.

    The distinction matters because the alternative implementation — dropping the key
    from the catalog — would also satisfy the headline requirement while destroying the
    reporting the gate exists for.
    """
    from app.services.evaluation.metric_package import families
    from app.services.evaluation.metric_package.catalog import quality_catalog

    keys = {metric["key"] for metric in quality_catalog(families.STRUCTURED_JSON)}

    assert "json.schema_validity" in keys
    assert combine_headline(
        {"json.schema_validity": 0.0, "json.field_accuracy": 75.0},
        ["json.schema_validity", "json.field_accuracy"],
    ).value == 37.5


def test_the_json_validity_exclusion_does_not_leak_into_other_families() -> None:
    """Another family's default contributors still compose its headline."""
    metrics = {"classification.macro_prf": 40.0, "classification.micro_prf": 60.0}
    assert combine_headline(metrics, _resolve_default(metrics)).value == 50.0


# --- An ill-formed answer scores 0 and STAYS in the denominator -----------------


def _details(field_accuracy: dict) -> dict:
    return {"metric_results": {"json.field_accuracy": field_accuracy}}


def test_an_ill_formed_answer_is_persisted_as_a_measured_zero() -> None:
    """An ill-formed answer is a measured zero, asserted at the line that would drop it.

    ``extract_metrics`` keeps only ``scored`` outcomes. An ill-formed answer reported as
    ``invalid`` would therefore be dropped here — silently — and the run mean would be
    taken over the entries that *did* parse. A model that emits prose instead of JSON on
    the hard half of a dataset would then score higher than one that attempts every entry
    and gets some wrong, which inverts the ranking the evaluation exists to produce.
    """
    from app.services.evaluation.metrics import extract_metrics

    kept = extract_metrics(_details({"status": "scored", "value": 0.0}))
    dropped = extract_metrics(_details({"status": "invalid", "value": 0.0}))

    assert kept["json.field_accuracy"] == 0.0
    assert "json.field_accuracy" not in dropped


def test_an_unscorable_entry_is_absent_from_the_denominator_not_zero() -> None:
    """An unscorable entry moves the mean the opposite way to an ill-formed answer.

    Scoring 0 pulls the mean down; being absent leaves it alone. An entry the platform
    declined to score — no scorable field, or a pairing input past the compute bound — is
    not a failed answer, so it must not be counted as one.
    """
    from app.services.evaluation.metrics import extract_metrics

    skipped = extract_metrics(
        _details({"status": "skipped", "value": None, "detail": {"reason": "no_scorable_fields"}})
    )

    assert "json.field_accuracy" not in skipped


def test_the_two_outcomes_move_a_run_aggregate_in_opposite_directions() -> None:
    """The arithmetic, end to end, over a three-entry run.

    Two entries at 90 plus one ill-formed answer means 60.0 over three entries. Two entries
    at 90 plus one skipped entry means 90.0 over two. Same run, same failure count, and the
    difference is entirely whether the model caused the outcome.
    """
    from app.services.evaluation.metrics import aggregate_metrics

    ill_formed = aggregate_metrics(
        [("json.field_accuracy", 90.0), ("json.field_accuracy", 90.0), ("json.field_accuracy", 0.0)]
    )["json.field_accuracy"]
    unscorable = aggregate_metrics(
        [("json.field_accuracy", 90.0), ("json.field_accuracy", 90.0), ("json.field_accuracy", None)]
    )["json.field_accuracy"]

    assert ill_formed["mean"] == 60.0
    assert ill_formed["count"] == 3
    assert unscorable["mean"] == 90.0
    assert unscorable["count"] == 2


# ---------------------------------------------------------------------------
# A run with no rankable performance metric ABSTAINS
#
# A run that measured nothing has no performance opinion. Its empty key set in the
# cross-run intersection would collapse the shared basis for *every* run of the
# evaluation, for good, since the basis is rebuilt from full history on every refresh.
#
# A run that recorded NOTHING abstains; a run that recorded 0.0 is fully measured and
# may anchor the range.
# ---------------------------------------------------------------------------


def test_rankability_reads_direction_never_value():
    """The predicate keys on presence and direction, never on the value."""
    from app.services.evaluation.aggregation import rankable_performance_keys

    zero = {PERF_LOWER: _agg(PERF_LOWER, 0.0, LOWER_IS_BETTER)}
    assert rankable_performance_keys(zero) == {PERF_LOWER}

    directionless = {PERF_LOWER: AggregatedMetric(PERF_LOWER, 1.0, 1, None)}
    assert rankable_performance_keys(directionless) == set()

    non_performance = {QUALITY_HIGHER: _agg(QUALITY_HIGHER, 90.0, HIGHER_IS_BETTER)}
    assert rankable_performance_keys(non_performance) == set()

    assert rankable_performance_keys({}) == set()


def test_the_intersection_and_the_disclosure_share_one_predicate():
    """One definition of "abstains", or the disclosure names runs the basis kept.

    Asserts the CALL, not the absence of a re-spelled comprehension: an absence
    assertion over source text is formatting-sensitive and fails on reflow.
    """
    import inspect

    from app.services.evaluation import aggregation

    for fn in (
        aggregation.normalise_performance,
        aggregation.classify_run_level_exclusions,
    ):
        assert "rankable_performance_keys" in inspect.getsource(fn), fn.__name__


# --- A legacy run must not veto its siblings -------------------------------------------

# A real legacy run is not an empty run: it has results and metric rows, they are just
# execution telemetry rather than package metrics. Seeding it that way exercises the
# path production actually produces.
LEGACY_ENTRIES = [{LEGACY_KEY: 1.2}]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_run_with_no_performance_data_does_not_veto_its_siblings():
    """One legacy run must not zero a whole evaluation."""
    from app.services.evaluation.aggregation import compute_performance_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [LEGACY_ENTRIES, [{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]],
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        legacy, fast, slow = runs
        # An abstainer stays consistent: no value, no contributing keys, no contributions.
        assert headlines[legacy.id].value is None
        assert headlines[legacy.id].contributing_keys == []
        assert headlines[legacy.id].contributions == {}
        assert headlines[fast.id].value == 100.0
        assert headlines[slow.id].value == 0.0
        assert headlines[fast.id].contributing_keys == [PERF_LOWER]
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_run_abstaining_scores_nobody():
    """No basis at all is N/A for everyone, never a fabricated value."""
    from app.services.evaluation.aggregation import compute_performance_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session, [LEGACY_ENTRIES, LEGACY_ENTRIES, LEGACY_ENTRIES]
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        assert [headlines[r.id].value for r in runs] == [None, None, None]
        assert all(headlines[r.id].contributing_keys == [] for r in runs)
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_single_eligible_run_is_not_awarded_the_top_of_the_scale():
    """One run has nothing to be compared against: N/A, not 100."""
    from app.services.evaluation.aggregation import compute_performance_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session, [LEGACY_ENTRIES, LEGACY_ENTRIES, [{PERF_LOWER: 1.0}]]
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        assert [headlines[r.id].value for r in runs] == [None, None, None]
        assert headlines[runs[2].id].value != 100.0
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_previously_vetoed_evaluation_recovers_on_the_next_refresh():
    """Recovery rides the ordinary refresh: no migration, no backfill.

    One recompute is enough, which is what the manual recalculate route does: a second
    one changes nothing.
    """
    from app.services.evaluation_engine import _compute_run_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [LEGACY_ENTRIES, [{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]],
            )
            await session.commit()
            await _compute_run_headlines(session, evaluation.id)
            for r in runs:
                await session.refresh(r)

            legacy, fast, slow = runs
            assert fast.performance_score == 100.0
            assert slow.performance_score == 0.0
            assert fast.headline_details["performance_metric_keys"] == [PERF_LOWER]
            assert legacy.performance_score is None
            assert legacy.headline_details["performance_metric_keys"] == []

            await _compute_run_headlines(session, evaluation.id)
            for r in runs:
                await session.refresh(r)
            assert [r.performance_score for r in runs] == [None, 100.0, 0.0]
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_batch_scoped_refresh_still_rescores_the_whole_evaluation():
    """The basis is the evaluation, never the trigger batch."""
    from app.services.evaluation_engine import _compute_run_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [LEGACY_ENTRIES, [{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]],
            )
            await session.commit()
            # Only the fast run is "in the batch"; the slow one must be rescored too.
            await _compute_run_headlines(
                session, evaluation.id, batch_run_ids=[str(runs[1].id)]
            )
            for r in runs:
                await session.refresh(r)

        assert runs[1].performance_score == 100.0
        assert runs[2].performance_score == 0.0, "a non-batch run was left unscored"
    finally:
        await cleanup()


# --- A measured zero is a data point, not a silence ------------------------------------
#
# A value-sensitive abstention predicate would silently drop the most extreme
# observation in the set.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_measured_zero_anchors_the_range():
    """Zero tool calls / zero cost is the BEST result, not a missing one."""
    from app.services.evaluation.aggregation import compute_performance_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [[{PERF_LOWER: 0.0}], [{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]],
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        zero, mid, slow = runs
        assert headlines[zero.id].value == 100.0
        assert headlines[zero.id].contributing_keys == [PERF_LOWER]
        assert headlines[mid.id].value == pytest.approx(78.95, abs=0.01)
        assert headlines[slow.id].value == 0.0
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_absent_and_zero_are_different_answers():
    """The crux, asserted on VALUES rather than on "they differ".

    A difference assertion could pass for the wrong reason: an absent base vetoed to
    all-`None` differs from a zero base without the distinction being exercised. Both
    bases score, and the siblings differ because a measured zero re-bases the range and
    a silence does not.
    """
    from app.services.evaluation.aggregation import compute_performance_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            with_zero_eval, with_zero = await _seed_performance_runs(
                session,
                [[{PERF_LOWER: 0.0}], [{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]],
            )
            with_absent_eval, with_absent = await _seed_performance_runs(
                session,
                [LEGACY_ENTRIES, [{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]],
            )
            await session.commit()
            zero_headlines = await compute_performance_headlines(
                session, with_zero_eval, [r.id for r in with_zero]
            )
            absent_headlines = await compute_performance_headlines(
                session, with_absent_eval, [r.id for r in with_absent]
            )

        # Siblings are identical in both bases (1.0 and 4.0); only the third run differs.
        zero_siblings = [zero_headlines[r.id].value for r in with_zero[1:]]
        absent_siblings = [absent_headlines[r.id].value for r in with_absent[1:]]
        assert zero_siblings == [pytest.approx(78.95, abs=0.01), 0.0]
        assert absent_siblings == [100.0, 0.0]
        # The measured zero participates and anchors; the silence abstains.
        assert zero_headlines[with_zero[0].id].value == 100.0
        assert absent_headlines[with_absent[0].id].value is None
    finally:
        await cleanup()


# --- The abstention is disclosed, and the cause is decidable --------------------------


def _run_level(headline, reason):
    """The run-level entry for `reason`, or None. Keyed by the sentinel, not a metric."""
    from app.services.evaluation.aggregation import RUN_LEVEL_EXCLUSION_KEY

    for e in headline.excluded:
        if e.key == RUN_LEVEL_EXCLUSION_KEY and e.reason == reason:
            return e
    return None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_abstention_is_disclosed_with_the_abstaining_run_ids():
    """A reader must be able to see WHICH runs were set aside, and why.

    The abstainer's own cause (no performance data, not too few runs) is decidable
    from the stored disclosure.
    """
    from app.services.evaluation.aggregation import (
        REASON_NO_PERFORMANCE_DATA,
        REASON_TOO_FEW_COMPARABLE_RUNS,
        compute_performance_headlines,
    )

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [LEGACY_ENTRIES, [{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]],
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        entry = _run_level(headlines[runs[1].id], REASON_NO_PERFORMANCE_DATA)
        assert entry is not None
        assert entry.run_ids == [runs[0].id]

        abstainer = headlines[runs[0].id]
        own = _run_level(abstainer, REASON_NO_PERFORMANCE_DATA)
        assert own is not None and runs[0].id in own.run_ids
        assert _run_level(abstainer, REASON_TOO_FEW_COMPARABLE_RUNS) is None
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_configured_selection_and_abstainers_are_disclosed_together():
    """Both disclosures survive in one list.

    The selection-gated branch REASSIGNS `excluded`, so run-level entries added before
    it would be silently discarded exactly when a selection is configured.
    """
    from app.services.evaluation.aggregation import (
        REASON_NO_PERFORMANCE_DATA,
        REASON_NOT_APPLICABLE,
        compute_performance_headlines,
    )

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [LEGACY_ENTRIES, [{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]],
                selection=[PERF_LOWER, PERF_HIGHER],
                details=[
                    [{"metric_results": {}}],
                    [
                        {
                            "metric_results": {
                                PERF_LOWER: {"status": "scored"},
                                PERF_HIGHER: {"status": "unavailable"},
                            }
                        }
                    ],
                    [{"metric_results": {PERF_LOWER: {"status": "scored"}}}],
                ],
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        headline = headlines[runs[1].id]
        metric_level = [(e.key, e.reason) for e in headline.excluded if e.key == PERF_HIGHER]
        assert metric_level == [(PERF_HIGHER, REASON_NOT_APPLICABLE)]
        assert _run_level(headline, REASON_NO_PERFORMANCE_DATA) is not None
        assert headline.value == 100.0, "the selection-gated path still scores"
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_too_few_comparable_runs_is_disclosed_separately():
    """Too few comparable runs is its own reason, listing the runs that WERE eligible."""
    from app.services.evaluation.aggregation import (
        REASON_TOO_FEW_COMPARABLE_RUNS,
        compute_performance_headlines,
    )

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session, [LEGACY_ENTRIES, LEGACY_ENTRIES, [{PERF_LOWER: 1.0}]]
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        entry = _run_level(headlines[runs[2].id], REASON_TOO_FEW_COMPARABLE_RUNS)
        assert entry is not None
        assert entry.run_ids == [runs[2].id]
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_eligible_run_is_never_named_as_an_abstainer():
    """A measured zero is eligible. Asserted non-vacuously: presence, THEN absence.

    "X is not in Y" is satisfied by Y not existing, so step 1 is what makes this fail
    before the disclosure is built rather than pass while proving nothing.
    """
    from app.services.evaluation.aggregation import (
        REASON_NO_PERFORMANCE_DATA,
        compute_performance_headlines,
    )

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [LEGACY_ENTRIES, [{PERF_LOWER: 0.0}], [{PERF_LOWER: 4.0}]],
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        abstainer, zero_run, _ = runs
        entry = _run_level(headlines[zero_run.id], REASON_NO_PERFORMANCE_DATA)
        assert entry is not None, "no disclosure at all: the absence below proves nothing"
        assert abstainer.id in entry.run_ids
        assert zero_run.id not in entry.run_ids
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_run_ids_reach_the_persisted_headline_details():
    """The disclosure has to survive persistence, stringified."""
    from app.services.evaluation.aggregation import REASON_NO_PERFORMANCE_DATA
    from app.services.evaluation_engine import _compute_run_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [LEGACY_ENTRIES, [{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]],
            )
            await session.commit()
            await _compute_run_headlines(session, evaluation.id)
            await session.refresh(runs[1])

            stored = runs[1].headline_details["performance_excluded"]
            entry = next(e for e in stored if e["reason"] == REASON_NO_PERFORMANCE_DATA)
            assert entry["run_ids"] == [str(runs[0].id)]
            assert entry["key"] == "__run__"
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_completed_run_with_zero_results_still_gets_a_headline_record():
    """A run with nothing measurable must not look like one whose headlines were
    never computed.

    A `NULL` `headline_details` is what the stranded-run recovery adapter looks for,
    so a legitimately empty run left `NULL` would be reselected and recomputed forever.
    """
    from app.services.evaluation_engine import _compute_run_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(session, [[]])
            await session.commit()
            await _compute_run_headlines(session, evaluation.id)
            await session.refresh(runs[0])

            assert runs[0].headline_details is not None
            assert runs[0].quality_score is None
            assert runs[0].score is None
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_disclosure_is_rewritten_wholesale_on_each_refresh():
    """A run leaving the basis must not survive in anyone's stored disclosure."""
    from app.services.evaluation.aggregation import REASON_NO_PERFORMANCE_DATA
    from app.services.evaluation_engine import _compute_run_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [LEGACY_ENTRIES, [{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]],
            )
            await session.commit()
            await _compute_run_headlines(session, evaluation.id)
            await session.refresh(runs[1])
            assert any(
                e["reason"] == REASON_NO_PERFORMANCE_DATA
                for e in runs[1].headline_details["performance_excluded"]
            )

            # The abstainer ceases to be completed, so it leaves the basis.
            runs[0].status = "failed"
            await session.commit()
            await _compute_run_headlines(session, evaluation.id)
            await session.refresh(runs[1])

            assert not any(
                e["reason"] == REASON_NO_PERFORMANCE_DATA
                for e in runs[1].headline_details["performance_excluded"]
            ), "a stale run-level exclusion survived the recompute"
    finally:
        await cleanup()


# --- Cost, regression proof, and the adjacent guarantees -------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_abstention_adds_no_database_round_trip():
    """Deciding who abstains reads aggregates already in memory."""
    from app.services.evaluation.aggregation import compute_performance_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            clean_eval, clean = await _seed_performance_runs(
                session, [[{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]]
            )
            mixed_eval, mixed = await _seed_performance_runs(
                session,
                [LEGACY_ENTRIES, LEGACY_ENTRIES, [{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]],
            )
            await session.commit()

            with _QueryCounter(session) as c1:
                await compute_performance_headlines(
                    session, clean_eval, [r.id for r in clean]
                )
            with _QueryCounter(session) as c2:
                await compute_performance_headlines(
                    session, mixed_eval, [r.id for r in mixed]
                )

        assert c1.count == c2.count, f"abstainers cost extra queries: {c1.count} vs {c2.count}"
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_adding_abstainers_does_not_move_a_measured_run_s_score():
    """Abstainers are inert for the runs that measured something."""
    from app.services.evaluation.aggregation import compute_performance_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            without_eval, without = await _seed_performance_runs(
                session, [[{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]]
            )
            with_eval, with_runs = await _seed_performance_runs(
                session,
                [LEGACY_ENTRIES, LEGACY_ENTRIES, [{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]],
            )
            await session.commit()
            a = await compute_performance_headlines(
                session, without_eval, [r.id for r in without]
            )
            b = await compute_performance_headlines(
                session, with_eval, [r.id for r in with_runs]
            )

        assert [a[r.id].value for r in without] == [b[r.id].value for r in with_runs[2:]]
        assert [a[r.id].value for r in without] == [100.0, 0.0]
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_recompute_never_writes_a_measurement():
    """Abstention decides the basis; it must not touch the data."""
    from app.services.evaluation_engine import _compute_run_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, _runs = await _seed_performance_runs(
                session,
                [LEGACY_ENTRIES, [{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]],
            )
            await session.commit()

            async def snapshot():
                rows = (
                    await session.execute(
                        select(
                            EvaluationMetric.run_id,
                            EvaluationMetric.key,
                            EvaluationMetric.value,
                        )
                    )
                ).all()
                return sorted((str(r), k, v) for r, k, v in rows)

            before = await snapshot()
            await _compute_run_headlines(session, evaluation.id)
            after = await snapshot()

        assert before == after
        assert before, "precondition: the fixture recorded measurements"
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_quality_and_judge_headlines_are_untouched_by_abstention():
    """Quality is absolute and per-run; the performance basis cannot reach it."""
    from app.services.evaluation_engine import _compute_run_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [
                    [{LEGACY_KEY: 1.2, QUALITY_HIGHER: 90.0}],
                    [{PERF_LOWER: 1.0, QUALITY_HIGHER: 80.0}],
                    [{PERF_LOWER: 4.0, QUALITY_HIGHER: 70.0}],
                ],
            )
            await session.commit()
            await _compute_run_headlines(session, evaluation.id)
            for r in runs:
                await session.refresh(r)
            quality_before = [r.quality_score for r in runs]
            keys_before = [r.headline_details["quality_metric_keys"] for r in runs]
            judge_before = [r.score for r in runs]

            await _compute_run_headlines(session, evaluation.id)
            for r in runs:
                await session.refresh(r)

        assert quality_before == [90.0, 80.0, 70.0]
        assert [r.quality_score for r in runs] == quality_before
        assert [r.headline_details["quality_metric_keys"] for r in runs] == keys_before
        # The judge half is recomputed by the same pass and must land on the same value:
        # what must not happen is the performance basis reaching across into it.
        assert [r.score for r in runs] == judge_before
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_null_performance_score_is_never_sorted_or_charted_as_zero():
    """`None` and `0.0` must stay apart on the chart and sort surfaces.

    An unmeasured run must never be plotted at the bottom of the axis or ordered beside
    the genuinely worst run. Calls the real `get_chart_data`, so a coalesce in either
    the trend or the correlation builder fails here. The fixture holds a genuine `0.0`
    beside the unmeasured run: without both, absence and zero cannot be told apart.
    """
    from app.api.v1.evaluations import _CORRELATIONS, _TREND_SERIES, get_chart_data
    from app.services.evaluation_engine import _compute_run_headlines

    assert ("performance", "performance_score") in _TREND_SERIES, "registry drifted"
    perf_correlations = [
        name for name, x, y in _CORRELATIONS if "performance_score" in (x, y)
    ]
    assert set(perf_correlations) == {"quality_performance", "judge_performance"}

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            admin = await _make_admin(session)
            evaluation, runs = await _seed_performance_runs(
                session,
                [
                    [{LEGACY_KEY: 1.2, QUALITY_HIGHER: 90.0}],
                    [{PERF_LOWER: 1.0, QUALITY_HIGHER: 80.0}],
                    [{PERF_LOWER: 4.0, QUALITY_HIGHER: 70.0}],
                ],
            )
            # A correlation point is one agent/model PAIR's latest run, so the three
            # runs need distinct models or two of them collapse into one point.
            for i, run in enumerate(runs):
                run.model_id = f"model-{i}"
            await session.commit()
            await _compute_run_headlines(session, evaluation.id)
            for r in runs:
                await session.refresh(r)

            abstainer, fast, slow = runs
            assert (abstainer.performance_score, fast.performance_score) == (None, 100.0)
            assert slow.performance_score == 0.0, "precondition: a genuine 0.0 is present"

            chart = await get_chart_data(
                evaluation_id=str(evaluation.id), user=admin, db=session
            )

            # Trends: a run with no value for a series is ABSENT from it, never 0.
            plotted = {
                series
                for point in chart["trends"]["performance"]
                for series in point["scores"]
            }
            assert plotted == {"Agent/model-1", "Agent/model-2"}
            assert "Agent/model-0" not in plotted
            assert 0.0 in {
                v for point in chart["trends"]["performance"] for v in point["scores"].values()
            }

            # Correlations: the unmeasured pair is omitted and COUNTED as omitted,
            # rather than pinned to an axis edge next to the genuine zero.
            for name in perf_correlations:
                ys = {p["y"] for p in chart["correlations"][name]}
                series = {p["series"] for p in chart["correlations"][name]}
                assert series == {"Agent/model-1", "Agent/model-2"}, name
                assert ys == {100.0, 0.0}, name
                assert chart["correlations_omitted"][name] == 1, name

            ordered = (
                (
                    await session.execute(
                        select(EvaluationRun.performance_score)
                        .where(EvaluationRun.evaluation_id == evaluation.id)
                        .order_by(EvaluationRun.performance_score.asc().nullslast())
                    )
                )
                .scalars()
                .all()
            )

        assert ordered == [0.0, 100.0, None], "N/A collapsed into the numeric range"
    finally:
        await cleanup()


# --- Basis membership is derived on every refresh, in BOTH directions ------------------
#
# A run leaves the basis by ceasing to be completed, through the same code path as
# deletion: the basis query filters on `status == "completed"`.


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_abstainer_that_gains_measurements_rejoins_and_rebases_its_siblings():
    """Abstention is a property of the data, re-derived each refresh -- not a label."""
    from app.services.evaluation.aggregation import REASON_NO_PERFORMANCE_DATA
    from app.services.evaluation_engine import _compute_run_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [LEGACY_ENTRIES, [{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]],
            )
            await session.commit()
            await _compute_run_headlines(session, evaluation.id)
            for r in runs:
                await session.refresh(r)

            abstainer, fast, slow = runs
            assert [r.performance_score for r in runs] == [None, 100.0, 0.0]

            # The abstainer is rescored and now records a measurement -- a better one
            # than any sibling, so rejoining must move everyone, not just itself.
            result_id = (
                await session.execute(
                    select(EvaluationResult.id).where(
                        EvaluationResult.run_id == abstainer.id
                    )
                )
            ).scalar_one()
            session.add(
                EvaluationMetric(
                    run_id=abstainer.id,
                    result_id=result_id,
                    key=PERF_LOWER,
                    value=0.5,
                )
            )
            await session.commit()
            await _compute_run_headlines(session, evaluation.id)
            for r in runs:
                await session.refresh(r)

            assert abstainer.performance_score == 100.0, "it did not rejoin the basis"
            assert fast.performance_score == pytest.approx(86.33, abs=0.01), (
                "the sibling was not re-based against the new best value"
            )
            assert slow.performance_score == 0.0
            assert abstainer.headline_details["performance_metric_keys"] == [PERF_LOWER]
            assert not any(
                e["reason"] == REASON_NO_PERFORMANCE_DATA
                for e in fast.headline_details["performance_excluded"]
            ), "nobody abstains any more, so the disclosure must be gone"
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_when_the_eligible_runs_leave_every_survivor_returns_to_not_measured():
    """A stored score must be CLEARED when its basis stops existing, not left stale.

    The strong half of this is `e2`: it held a real `0.0`, and once it is the only
    eligible run there is nothing to compare it against, so the stored value has to
    become `None` again. An implementation that only ever writes scores and never
    clears them passes every other test in this file and fails here.
    """
    from app.services.evaluation.aggregation import REASON_TOO_FEW_COMPARABLE_RUNS
    from app.services.evaluation_engine import _compute_run_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [LEGACY_ENTRIES, [{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]],
            )
            await session.commit()
            await _compute_run_headlines(session, evaluation.id)
            for r in runs:
                await session.refresh(r)

            abstainer, e1, e2 = runs
            assert [r.performance_score for r in runs] == [None, 100.0, 0.0]

            # One eligible run leaves the basis: one remains, which cannot be ranked.
            e1.status = "failed"
            await session.commit()
            await _compute_run_headlines(session, evaluation.id)
            for r in (abstainer, e2):
                await session.refresh(r)

            assert e2.performance_score is None, "a stale score survived its basis"
            assert e2.headline_details["performance_metric_keys"] == []
            assert abstainer.performance_score is None
            assert any(
                e["reason"] == REASON_TOO_FEW_COMPARABLE_RUNS
                for e in e2.headline_details["performance_excluded"]
            )

            # The last eligible run leaves too: only abstainers remain.
            e2.status = "failed"
            await session.commit()
            await _compute_run_headlines(session, evaluation.id)
            await session.refresh(abstainer)

            assert abstainer.performance_score is None
            assert abstainer.headline_details["performance_metric_keys"] == []
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_stored_legacy_row_still_traverses_the_read_path():
    """A stored legacy row stays readable through `get_run_aggregates`.

    `test_aggregates_still_answer_for_a_legacy_key` calls `aggregate_metrics` with
    synthetic tuples, so a regression in the query behind `get_run_aggregates` would
    pass it. Pre-cutover rows are not migrated, so they must stay readable on the path
    production reads them through.
    """
    from app.services.evaluation.metrics import get_run_aggregates

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(name="Eval", scorer_selection=[])
            session.add_all([agent, evaluation])
            await session.flush()

            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4o",
                trigger_reason="manual",
                status="completed",
                score=None,
            )
            session.add(run)
            await session.flush()

            # A pre-cutover result: legacy keys and nothing the package produces.
            for value in (1.0, 3.0):
                result = EvaluationResult(run_id=run.id, agent_output="", score=0.0)
                session.add(result)
                await session.flush()
                session.add(
                    EvaluationMetric(
                        run_id=run.id,
                        result_id=result.id,
                        key=LEGACY_KEY,
                        value=value,
                    )
                )
            await session.commit()

            aggregates = await get_run_aggregates(session, run.id)

        assert LEGACY_KEY in aggregates, "a pre-cutover run stopped being readable"
        assert aggregates[LEGACY_KEY]["mean"] == 2.0
        assert aggregates[LEGACY_KEY]["count"] == 2
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_persisted_selection_naming_a_removed_key_stays_inert():
    """A stored selection naming a removed key stays inert on the production path.

    `test_stored_selection_with_legacy_key_stays_inert` feeds dictionaries straight
    into Stage 1 and Stage 2, so a regression in *loading* the stored selection would
    pass it. This persists an `overall_performance_metrics` naming a key no longer
    produced and drives `_compute_run_headlines`. The selection is unvalidated on
    write, so the state is reachable: it scores on the surviving key and raises nothing.
    """
    from app.services.evaluation_engine import _compute_run_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [[{PERF_LOWER: 1.0, LEGACY_KEY: 1.2}], [{PERF_LOWER: 4.0, LEGACY_KEY: 9.9}]],
                selection=[LEGACY_KEY, PERF_LOWER],
            )
            await session.commit()
            await _compute_run_headlines(session, evaluation.id)
            for r in runs:
                await session.refresh(r)

            fast, slow = runs
            assert [fast.performance_score, slow.performance_score] == [100.0, 0.0]
            for r in runs:
                assert r.headline_details["performance_metric_keys"] == [PERF_LOWER], (
                    "the removed key must contribute nothing, silently"
                )
    finally:
        await cleanup()


# --- What the intersection drops is recorded whatever the selection --------------

_TOOL_ERRORS = "performance.reliability.tool_error_rate"


def _skipped(key: str) -> dict:
    """An entry's ``execution_details`` saying *key* was skipped (a run with no tool call)."""
    return {"metric_results": {key: {"status": "skipped", "value": None}}}


def _metric_exclusions(headline):
    from app.services.evaluation.aggregation import RUN_LEVEL_EXCLUSION_KEY

    return [e for e in headline.excluded if e.key != RUN_LEVEL_EXCLUSION_KEY]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_default_selection_records_a_metric_some_runs_lack():
    """The manual-test case: an agent with tools beside one without."""
    from app.services.evaluation.aggregation import compute_performance_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [[{PERF_LOWER: 1.0, _TOOL_ERRORS: 0.0}], [{PERF_LOWER: 4.0}]],
                selection=None,
                details=[[None], [_skipped(_TOOL_ERRORS)]],
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        # The headline itself is unchanged: built from what every run has.
        assert all(headlines[r.id].contributing_keys == [PERF_LOWER] for r in runs)
        # Each run's record says what is true for it: the run that measured the
        # metric lost it because the other lacks it; the other cannot produce it.
        [measured] = _metric_exclusions(headlines[runs[0].id])
        assert (measured.key, measured.reason, measured.run_ids) == (
            _TOOL_ERRORS,
            REASON_MISSING_FROM_OTHER_RUNS,
            [runs[1].id],
        )
        [lacking] = _metric_exclusions(headlines[runs[1].id])
        assert (lacking.key, lacking.reason, lacking.run_ids) == (
            _TOOL_ERRORS,
            REASON_NOT_APPLICABLE,
            [runs[1].id],
        )
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_default_selection_reads_a_metric_no_status_explains_as_failed():
    from app.services.evaluation.aggregation import compute_performance_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [[{PERF_LOWER: 1.0, PERF_HIGHER: 1.0}], [{PERF_LOWER: 4.0}]],
                selection=None,
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        [entry] = _metric_exclusions(headlines[runs[1].id])
        assert (entry.key, entry.reason, entry.run_ids) == (PERF_HIGHER, REASON_FAILED, [runs[1].id])
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_default_selection_records_nothing_when_every_run_shares_its_metrics():
    from app.services.evaluation.aggregation import compute_performance_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [[{PERF_LOWER: 1.0, PERF_HIGHER: 1.0}], [{PERF_LOWER: 4.0, PERF_HIGHER: 0.5}]],
                selection=None,
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        assert all(_metric_exclusions(h) == [] for h in headlines.values())
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_explicitly_empty_selection_still_records_no_metric():
    """``[]`` asks for no metric: nothing it asked for is missing."""
    from app.services.evaluation.aggregation import compute_performance_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [[{PERF_LOWER: 1.0, _TOOL_ERRORS: 0.0}], [{PERF_LOWER: 4.0}]],
                selection=[],
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        assert all(_metric_exclusions(h) == [] for h in headlines.values())
        assert all(h.value is None for h in headlines.values())
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_abstaining_run_is_disclosed_once_not_as_lacking_each_metric():
    """A run with no performance data is a run-level entry, not a metric's offender."""
    from app.services.evaluation.aggregation import (
        REASON_NO_PERFORMANCE_DATA,
        compute_performance_headlines,
    )

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [LEGACY_ENTRIES, [{PERF_LOWER: 1.0, PERF_HIGHER: 1.0}], [{PERF_LOWER: 4.0}]],
                selection=None,
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        headline = headlines[runs[1].id]
        assert _run_level(headline, REASON_NO_PERFORMANCE_DATA).run_ids == [runs[0].id]
        [entry] = _metric_exclusions(headline)
        assert (entry.key, entry.reason, entry.run_ids) == (
            PERF_HIGHER,
            REASON_MISSING_FROM_OTHER_RUNS,
            [runs[2].id],
        )
        # The abstaining run's own record: the run-level entry, and no metric.
        abstained = headlines[runs[0].id]
        assert _run_level(abstained, REASON_NO_PERFORMANCE_DATA) is not None
        assert _metric_exclusions(abstained) == []
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selection",
    [None, [PERF_LOWER, PERF_HIGHER]],
    ids=["no-selection", "selection-of-its-measured-metrics"],
)
async def test_a_lone_run_records_no_metric_only_that_it_cannot_be_compared(selection):
    """Nothing is ranked with one run, but no run lacks a metric: the run-level entry
    says why, with or without a selection."""
    from app.services.evaluation.aggregation import (
        REASON_TOO_FEW_COMPARABLE_RUNS,
        compute_performance_headlines,
    )

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session, [[{PERF_LOWER: 1.0, PERF_HIGHER: 1.0}]], selection=selection
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        headline = headlines[runs[0].id]
        assert _run_level(headline, REASON_TOO_FEW_COMPARABLE_RUNS) is not None
        assert _metric_exclusions(headline) == []
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_explicit_selection_never_names_an_abstaining_run_as_lacking_a_metric():
    """An abstaining run is disclosed once, at run level, whatever the selection.

    One run measured the selected metric, the other recorded nothing: nothing is
    ranked, and the run-level entries say why. The measuring run's record must not
    say the metric is missing from the abstaining run.
    """
    from app.services.evaluation.aggregation import (
        REASON_NO_PERFORMANCE_DATA,
        REASON_TOO_FEW_COMPARABLE_RUNS,
        compute_performance_headlines,
    )

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [LEGACY_ENTRIES, [{PERF_LOWER: 1.0}]],
                selection=[PERF_LOWER],
                details=[
                    [{"metric_results": {}}],
                    [{"metric_results": {PERF_LOWER: {"status": "scored"}}}],
                ],
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        for headline in headlines.values():
            assert _metric_exclusions(headline) == []
            assert _run_level(headline, REASON_NO_PERFORMANCE_DATA) is not None
            assert _run_level(headline, REASON_TOO_FEW_COMPARABLE_RUNS) is not None
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_default_selections_status_pass_costs_the_same_whatever_the_run_count():
    """With the default selection, a metric some runs lack is classified in one query.

    The sibling of the explicit-selection pin above: this pass runs on every
    performance recompute of runs that measured different metrics.
    """
    from app.services.evaluation.aggregation import compute_performance_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        counts = {}
        for run_count in (2, 8):
            async with sessionmaker() as session:
                evaluation, runs = await _seed_performance_runs(
                    session,
                    # Only the first run measured PERF_HIGHER, so it is dropped and classified.
                    [[{PERF_LOWER: 1.0, PERF_HIGHER: 1.0}]]
                    + [[{PERF_LOWER: 1.0 + r}] for r in range(1, run_count)],
                    selection=None,
                )
                await session.commit()
                with _QueryCounter(session) as counter:
                    headlines = await compute_performance_headlines(
                        session, evaluation, [r.id for r in runs]
                    )
                counts[run_count] = counter.count
                assert _metric_exclusions(headlines[runs[0].id]), (
                    "the dropped metric was not classified"
                )

        assert counts[2] == counts[8], f"Status pass scales with run count: {counts}"
    finally:
        await cleanup()


# --- Time to first token is not counted by default -------------------------------

PERF_TTFT = "performance.latency.ttft"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_default_selection_leaves_time_to_first_token_out_and_says_why():
    """Measured and shown, but not counted: on a short answer it is nearly the latency."""
    from app.services.evaluation.aggregation import compute_performance_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [[{PERF_LOWER: 1.0, PERF_TTFT: 0.5}], [{PERF_LOWER: 4.0, PERF_TTFT: 3.0}]],
                selection=None,
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        for run in runs:
            headline = headlines[run.id]
            assert headline.contributing_keys == [PERF_LOWER]
            assert [(e.key, e.reason) for e in _metric_exclusions(headline)] == [
                (PERF_TTFT, REASON_NOT_A_DEFAULT_CONTRIBUTOR)
            ]
        assert headlines[runs[0].id].value == 100.0, "the fast run still ranks first on latency"
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_time_to_first_token_counts_once_an_author_selects_it():
    from app.services.evaluation.aggregation import compute_performance_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [[{PERF_LOWER: 1.0, PERF_TTFT: 0.5}], [{PERF_LOWER: 4.0, PERF_TTFT: 3.0}]],
                selection=[PERF_LOWER, PERF_TTFT],
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        for run in runs:
            assert headlines[run.id].contributing_keys == sorted([PERF_LOWER, PERF_TTFT])
            assert _metric_exclusions(headlines[run.id]) == []
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_time_to_first_token_some_runs_lack_is_not_counted_by_default_not_missing():
    """The default would leave it out anyway: that is the reason given, on the runs that measured it."""
    from app.services.evaluation.aggregation import compute_performance_headlines

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [[{PERF_LOWER: 1.0, PERF_TTFT: 0.5}], [{PERF_LOWER: 4.0}]],
                selection=None,
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        measured, lacking = (headlines[r.id] for r in runs)
        assert [(e.key, e.reason) for e in _metric_exclusions(measured)] == [
            (PERF_TTFT, REASON_NOT_A_DEFAULT_CONTRIBUTOR)
        ]
        assert _metric_exclusions(lacking) == []
        assert measured.contributing_keys == lacking.contributing_keys == [PERF_LOWER]
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_n_a_headline_says_when_the_only_shared_metric_is_opt_in():
    """Time to first token is shared but not counted by default: the runs do share a
    metric, so the reader must not be told they share none."""
    from app.services.evaluation.aggregation import (
        REASON_ONLY_OPT_IN_METRICS_SHARED,
        compute_performance_headlines,
    )

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [[{PERF_LOWER: 1.0, PERF_TTFT: 0.5}], [{PERF_TTFT: 3.0}]],
                selection=None,
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        for run in runs:
            assert headlines[run.id].value is None
            assert _run_level(headlines[run.id], REASON_ONLY_OPT_IN_METRICS_SHARED) is not None
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_headline_with_a_counted_metric_names_no_opt_in_cause():
    from app.services.evaluation.aggregation import (
        REASON_ONLY_OPT_IN_METRICS_SHARED,
        compute_performance_headlines,
    )

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [[{PERF_LOWER: 1.0, PERF_TTFT: 0.5}], [{PERF_LOWER: 4.0, PERF_TTFT: 3.0}]],
                selection=None,
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        for run in runs:
            assert _run_level(headlines[run.id], REASON_ONLY_OPT_IN_METRICS_SHARED) is None
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_explicitly_empty_selection_is_disclosed_as_the_cause():
    """An author who selected no metric is told so, not that the runs share none."""
    from app.services.evaluation.aggregation import (
        REASON_NO_METRIC_SELECTED,
        compute_performance_headlines,
    )

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(
                session,
                [[{PERF_LOWER: 1.0}], [{PERF_LOWER: 4.0}]],
                selection=[],
            )
            await session.commit()
            headlines = await compute_performance_headlines(
                session, evaluation, [r.id for r in runs]
            )

        for run in runs:
            assert headlines[run.id].value is None
            assert _run_level(headlines[run.id], REASON_NO_METRIC_SELECTED) is not None
    finally:
        await cleanup()


# --- A pooled metric's run value adds up its entries' counts ---------------------

POOLED_RATE = "performance.reliability.tool_error_rate"


def test_a_pooled_metric_divides_the_runs_counts_with_no_spread_penalty():
    """5 failed calls of 10, then 0 of 1. Averaging the rates gives 0.25,
    raised to 0.375 by the penalty; the share of the run's calls that failed is 5 / 11."""
    out = reduce_grouped_metrics(
        {POOLED_RATE: [0.5, 0.0]}, penalty_factor=0.5, counts={POOLED_RATE: [(5, 10), (0, 1)]}
    )
    assert out[POOLED_RATE].value == pytest.approx(5 / 11, abs=1e-6)
    assert out[POOLED_RATE].entry_count == 2


def test_a_pooled_metric_with_a_zero_total_has_no_run_value():
    out = reduce_grouped_metrics({POOLED_RATE: [0.0]}, counts={POOLED_RATE: [(0, 0)]})
    assert POOLED_RATE not in out, "a pooled metric over nothing is unavailable, never 0"


@pytest.mark.parametrize(
    "counts",
    [None, {}, {POOLED_RATE: [(5, 10), None]}, {POOLED_RATE: [(5, 10)]}],
    ids=["no_counts", "no_key", "one_entry_without", "fewer_than_values"],
)
def test_a_pooled_metric_missing_an_entry_s_counts_has_no_run_value(counts):
    """Pooling over only some entries would misstate the run, so it has no value."""
    out = reduce_grouped_metrics({POOLED_RATE: [0.5, 0.0]}, counts=counts)
    assert POOLED_RATE not in out


def test_an_averaged_metric_ignores_counts():
    out = reduce_grouped_metrics(
        {PERF_LOWER: [100.0, 300.0]}, penalty_factor=0.5, counts={PERF_LOWER: [(1, 2), (1, 2)]}
    )
    assert out[PERF_LOWER].value == 250.0, "the mean plus the spread penalty, as before"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_case_7_pools_the_run_s_tool_calls_end_to_end():
    """From the package's results to the run's value, through the stored counts.

    Two entries' real performance results (10 tool calls with 5 failures, then 1 call
    without) are stored the way the engine stores them, logged into `evaluation_metrics`,
    and aggregated: the run reads 5 / 11, and each entry keeps its own rate.
    """
    from app.services.evaluation import metrics as run_metrics
    from app.services.evaluation.aggregation import aggregate_metrics_by_run
    from app.services.evaluation.metric_package import PerformanceConfig, run_performance

    def entry(outcomes: list[str]) -> dict:
        traces = [{"type": "llm", "duration_ms": 10, "outcome": "success"}] + [
            {"type": "tool", "tool_name": "check", "duration_ms": 1, "outcome": o}
            for o in outcomes
        ]
        results = run_performance(
            execution_details={"agent_wall_clock_ms": 100, "traces": traces},
            token_usage=None,
            retry_count=0,
            price_rows=[],
            config=PerformanceConfig(),
        )
        return {"metric_results": {r.key: r.serialized for r in results}}

    details = [entry(["error", "success"] * 5), entry(["success"])]

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            _evaluation, (run,) = await _seed_performance_runs(
                session, [[{}, {}]], details=[details]
            )
            results = (
                await session.execute(
                    select(EvaluationResult).where(EvaluationResult.run_id == run.id)
                )
            ).scalars().all()
            for result in results:
                await run_metrics.log_result_metrics(
                    session,
                    result_id=result.id,
                    run_id=run.id,
                    execution_details=result.execution_details,
                )
            await session.commit()

            stored = (
                await session.execute(
                    select(
                        EvaluationMetric.value,
                        EvaluationMetric.numerator,
                        EvaluationMetric.denominator,
                    ).where(
                        EvaluationMetric.run_id == run.id,
                        EvaluationMetric.key == POOLED_RATE,
                    )
                )
            ).all()
            aggregated = (await aggregate_metrics_by_run(session, [run.id]))[run.id]
            panel = await run_metrics.get_run_aggregates(session, run.id)

        assert sorted(stored) == [(0.0, 0, 1), (0.5, 5, 10)]
        assert aggregated[POOLED_RATE].value == pytest.approx(5 / 11, abs=1e-6)
        # The per-entry view is unchanged: the panel still describes the entries' rates.
        assert panel[POOLED_RATE]["mean"] == pytest.approx(0.25)
        assert (panel[POOLED_RATE]["min"], panel[POOLED_RATE]["max"]) == (0.0, 0.5)
    finally:
        await cleanup()


@pytest.mark.parametrize(
    ("key", "expected"),
    [(QUALITY_HIGHER, 25.0), (QUALITY_FLIPPED, 75.0)],
    ids=["higher_is_better", "lower_is_better"],
)
def test_a_pooled_quality_metric_is_a_score_and_keeps_its_orientation(monkeypatch, key, expected):
    """No quality metric pools yet, so the branch is reached by declaring one:
    the ratio becomes a 0-100 score, and a lower-is-better one is flipped like any other."""
    from app.services.evaluation import aggregation

    _declare_lower_is_better(monkeypatch, QUALITY_FLIPPED)
    monkeypatch.setattr(aggregation, "is_pooled_key", lambda k: k == key)
    out = reduce_grouped_metrics({key: [100.0, 0.0]}, counts={key: [(1, 1), (0, 3)]})
    assert out[key].value == expected


def test_a_pooled_quality_score_over_empty_answers_only_reads_full(monkeypatch):
    """Nothing expected and nothing given is correct: 0 of 0 adds nothing to a
    pool, and a run of empty answers only reads 100. A rate over nothing stays absent."""
    from app.services.evaluation import aggregation

    monkeypatch.setattr(aggregation, "is_pooled_key", lambda k: k in (QUALITY_HIGHER, POOLED_RATE))
    out = reduce_grouped_metrics(
        {QUALITY_HIGHER: [100.0, 100.0], POOLED_RATE: [0.0]},
        counts={QUALITY_HIGHER: [(0, 0), (0, 0)], POOLED_RATE: [(0, 0)]},
    )
    assert out[QUALITY_HIGHER].value == 100.0
    assert POOLED_RATE not in out
    mixed = reduce_grouped_metrics({QUALITY_HIGHER: [100.0, 50.0]}, counts={QUALITY_HIGHER: [(0, 0), (1, 2)]})
    assert mixed[QUALITY_HIGHER].value == 50.0


# --- A share counted inside entries is pooled over the run ----------------------


def _malware(index: int) -> dict:
    return {
        "type": "malware",
        "spec_version": "2.1",
        "id": f"malware--00000000-0000-4000-8000-{index:012d}",
        "created": "2024-01-01T00:00:00.000Z",
        "modified": "2024-01-01T00:00:00.000Z",
        "name": f"malware {index}",
        "is_family": False,
    }


def _stix(*objects: dict) -> str:
    return json.dumps(
        {"type": "bundle", "id": "bundle--00000000-0000-4000-8000-000000000000", "objects": list(objects)}
    )


_TEN_FIELDS = {
    "type": "json_schema",
    "definition": {
        "type": "object",
        "properties": {f"f{i}": {"type": "string", "enum": ["a", "b"]} for i in range(10)},
    },
}
_TEN_RIGHT = json.dumps({f"f{i}": "a" for i in range(10)})
_TEN_LABELS = json.dumps([f"label {i}" for i in range(10)])

# Two entries per family: ten items all right, then one item wrong (manual test
# case 3). Averaged less the spread penalty they read 25.0; the share right is 10 of 11.
_SIZE_CASES = {
    "STIX_BUNDLE": (
        "stix.entity_prf",
        None,
        [
            (_stix(*(_malware(i) for i in range(10))), _stix(*(_malware(i) for i in range(10)))),
            (_stix(_malware(20)), _stix(_malware(21))),
        ],
    ),
    "STRUCTURED_JSON": (
        "json.field_accuracy",
        _TEN_FIELDS,
        [(_TEN_RIGHT, _TEN_RIGHT), (json.dumps({"f0": "a"}), json.dumps({"f0": "b"}))],
    ),
    "CLASSIFICATION": (
        "classification.micro_prf",
        None,
        [(_TEN_LABELS, _TEN_LABELS), ("left", "right")],
    ),
}


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("family", sorted(_SIZE_CASES))
async def test_a_share_counted_inside_entries_is_pooled_over_the_run(family):
    """Each entry keeps its own value; the run's is the share right over every item of
    every entry, with no spread penalty: one small wrong entry no longer outweighs a
    large right one. Scored by the package, stored and rolled up by the platform."""
    from app.services.evaluation import metrics as run_metrics
    from app.services.evaluation.aggregation import aggregate_run_metrics
    from app.services.evaluation.metric_package import build_quality_config, run_quality, to_persistable

    key, schema, pairs = _SIZE_CASES[family]
    config = build_quality_config(family, None, [reference for reference, _ in pairs])
    if config.stix_unavailable_reason:
        pytest.skip(config.stix_unavailable_reason)
    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            _, run = await _seed_judge_off_run(session, [])
            for reference, prediction in pairs:
                details = {
                    "metric_results": to_persistable(
                        run_quality(
                            family=family,
                            reference=reference,
                            prediction=prediction,
                            output_schema=schema,
                            config=config,
                        )
                    )
                }
                result = EvaluationResult(run_id=run.id, agent_output=prediction, score=0.0, execution_details=details)
                session.add(result)
                await session.flush()
                await run_metrics.log_result_metrics(
                    session, result_id=result.id, run_id=run.id, execution_details=details
                )
            await session.commit()

            entries = (
                await session.execute(
                    select(EvaluationMetric.value).where(EvaluationMetric.run_id == run.id, EvaluationMetric.key == key)
                )
            ).scalars()
            assert sorted(entries) == pytest.approx([0.0, 100.0])
            pooled = (await aggregate_run_metrics(session, run.id))[key]
            assert (pooled.value, pooled.entry_count) == (pytest.approx(90.91), 2)
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_sql_and_python_counting_rules_agree_on_every_kind_of_row():
    """The headlines' SQL filter and the Python one keep the same rows: each anomaly kind
    and none, judge failed or not."""
    import itertools

    from sqlalchemy import select

    from app.models.agent import Agent
    from app.models.evaluation import Evaluation, EvaluationResult, EvaluationRun
    from app.services.evaluation.aggregation import counts_in_headlines, result_counts_in_headlines
    from tests.scratch_database import ALL_TABLES, scratch_database

    kinds = [None, "confirmation_seeking", "transient_error", "platform_error"]
    combos = list(itertools.product(kinds, [False, True]))
    async with scratch_database("countrule", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as s:
        agent = Agent(name="Agent", slug=f"a-{uuid.uuid4().hex[:6]}")
        evaluation = Evaluation(name="Eval")
        s.add_all([agent, evaluation])
        await s.flush()
        run = EvaluationRun(
            evaluation_id=evaluation.id, agent_id=agent.id, model_id="gpt-4o", trigger_reason="manual", status="completed"
        )
        s.add(run)
        await s.flush()
        rows = {
            combo: EvaluationResult(
                run_id=run.id,
                agent_output="a",
                score=0,
                evaluator_reasoning="r",
                duration_seconds=1,
                execution_anomaly_kind=combo[0],
                is_judge_failure=combo[1],
            )
            for combo in combos
        }
        s.add_all(rows.values())
        await s.commit()

        kept = set((await s.execute(select(EvaluationResult.id).where(result_counts_in_headlines()))).scalars())

        assert {combo for combo, row in rows.items() if row.id in kept} == {
            combo for combo, row in rows.items() if counts_in_headlines(row)
        }
