"""Tests for per-result metric extraction and aggregates."""

from __future__ import annotations

import uuid

import pytest

from app.services.evaluation.metrics import (
    _MAX_METRIC_KEY_LENGTH,
    RUN_DURATION_KEY,
    _is_storable_key,
    aggregate_metrics,
    extract_metrics,
)


def _details(**over):
    d = {
        "total_duration_ms": 1500,
        "agent_duration_ms": 1200,
        "judge_duration_ms": 300,
        "ttft_ms": 250,
        "execution_mode": "full_loop",
        "traces": [
            {"type": "llm", "duration_ms": 600, "model": "gpt-4o", "usage": {"input_tokens": 100, "output_tokens": 50}},
            {"type": "tool", "duration_ms": 200, "tool_name": "search"},
            {"type": "llm", "duration_ms": 600, "model": "gpt-4o", "usage": {"input_tokens": 80, "output_tokens": 40}},
            {"type": "judge", "duration_ms": 300, "usage": {"input_tokens": 999}},
        ],
    }
    d.update(over)
    return d


# ── Extraction ──────────────────────────────────────────────────────────


def test_extract_durations():
    m = extract_metrics(_details())
    assert m[RUN_DURATION_KEY] == 1.5
    assert m["judge_seconds"] == 0.3


def test_empty_execution_details_no_crash():
    assert extract_metrics(None) == {}


def test_package_metric_results_are_extracted():
    m = extract_metrics(
        _details(
            metric_results={
                "performance.counts.tool_calls": {"status": "scored", "value": 1.0},
                "json.schema_validity": {"status": "invalid", "value": 0.0},
            }
        )
    )
    assert m["performance.counts.tool_calls"] == 1.0
    # Only ``scored`` outcomes contribute a value.
    assert "json.schema_validity" not in m


# ── Run Duration is what this run did ─────────────────────────────


def test_run_duration_is_agent_plus_judge_on_a_fresh_execution():
    assert extract_metrics(_details())[RUN_DURATION_KEY] == 1.5


def test_run_duration_is_the_judge_alone_on_a_reused_result():
    """The agent did not re-run, so its time belongs to another run, not this one."""
    m = extract_metrics(_details(), is_reused=True)
    assert m[RUN_DURATION_KEY] == 0.3
    assert m["judge_seconds"] == 0.3


def test_run_duration_is_omitted_when_the_reused_result_has_no_judge_timing():
    """Absent, not zero -- "no measurement" and "took no time" are different claims."""
    m = extract_metrics(_details(judge_duration_ms=None), is_reused=True)
    assert RUN_DURATION_KEY not in m


def test_total_seconds_is_never_written_again():
    """Run Duration replaces it; stored rows keep the old key untouched."""
    assert "total_seconds" not in extract_metrics(_details())
    assert "total_seconds" not in extract_metrics(_details(), is_reused=True)


# ── The legacy performance keys shadowed by the metric package ───
#
# The first group asserts each shadowed key is gone; the second guards the keys
# that must stay, so an over-deletion fails too.

# The six keys `extract_metrics` must not emit. `cost_usd`, the seventh, belongs
# to `log_result_metrics` and is asserted separately.
SHADOWED_EXTRACT_KEYS = (
    "ttft_seconds",
    "agent_seconds",
    "input_tokens",
    "output_tokens",
    "llm_iterations",
    "tool_calls",
)

# Legacy key → the package key that measures the same thing from the same input.
SHADOW_PAIRS = {
    "agent_seconds": "performance.latency.wall_clock",
    "ttft_seconds": "performance.latency.ttft",
    "llm_iterations": "performance.counts.model_calls",
    "tool_calls": "performance.counts.tool_calls",
    "input_tokens": "performance.tokens.total",
    "output_tokens": "performance.tokens.total",
    "cost_usd": "performance.cost.llm",
}


def _details_with_package_metrics():
    """A result carrying the package's own performance metrics, as a real run does."""
    return _details(
        metric_results={
            key: {"status": "scored", "value": 1.0}
            for key in set(SHADOW_PAIRS.values())
        }
    )


class _FakeResult:
    def scalar_one_or_none(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []


class _RecordingSession:
    """Captures what `log_result_metrics` would persist, without a database.

    The contract under test is *which keys are written*, which is observable
    from the `add` calls alone. Every read resolves to "nothing found", so a
    price lookup reports an unpriced model — the case that must not fabricate a
    zero.
    """

    def __init__(self):
        self.added = []

    async def execute(self, *_args, **_kwargs):
        return _FakeResult()

    def add(self, obj):
        self.added.append(obj)

    @property
    def keys(self):
        return {o.key for o in self.added}

    @property
    def values(self):
        return {o.key: o.value for o in self.added}


async def _log(is_reused: bool = False, **over):
    session = _RecordingSession()
    from app.services.evaluation.metrics import log_result_metrics

    await log_result_metrics(
        session,
        result_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        execution_details=_details(**over),
        is_reused=is_reused,
    )
    return session


# --- The shadowed keys are gone -----------------------------------------


@pytest.mark.parametrize("key", SHADOWED_EXTRACT_KEYS)
def test_extract_metrics_emits_no_shadowed_legacy_key(key):
    """The package measures each of these from the same input.

    Parametrized so a partial removal names the key that came back, rather than
    reporting one opaque failure for six independent measurements.
    """
    metrics = extract_metrics(_details_with_package_metrics())
    assert key not in metrics, (
        f"{key!r} duplicates {SHADOW_PAIRS[key]!r} from the same execution_details"
    )


def test_one_row_per_performance_measurement():
    """No measurement is persisted twice under two names.

    Would also pass under a naive over-deletion, so it is only meaningful paired
    with `test_judge_and_run_duration_still_logged`.
    """
    metrics = extract_metrics(_details_with_package_metrics())
    duplicated = {
        legacy: package
        for legacy, package in SHADOW_PAIRS.items()
        if legacy in metrics and package in metrics
    }
    assert not duplicated, f"same measurement stored under two keys: {duplicated}"


@pytest.mark.asyncio
async def test_log_result_metrics_writes_no_cost_usd_row():
    session = await _log()
    assert "cost_usd" not in session.keys


@pytest.mark.asyncio
async def test_an_unavailable_cost_is_not_persisted_as_a_value():
    """An unpriced model reports cost as unavailable, never as zero.

    The package's cost key is written when the outcome is `scored` and withheld
    when it is `unavailable`: a 0 would be averaged in as a real price.
    """
    unavailable = await _log(
        metric_results={"performance.cost.llm": {"status": "unavailable", "value": None}}
    )
    assert "cost_usd" not in unavailable.keys
    assert "performance.cost.llm" not in unavailable.keys, (
        "an unavailable cost must not be persisted at all, let alone as 0"
    )

    scored = await _log(
        metric_results={"performance.cost.llm": {"status": "scored", "value": 0.34}}
    )
    assert scored.keys == unavailable.keys | {"performance.cost.llm"}, (
        "only the cost key may differ between the priced and unpriced cases"
    )


# --- The keys with no package equivalent stay ----------------------------


def test_judge_and_run_duration_still_logged():
    """The retained keys have no package equivalent.

    The package drops judge steps as evaluation overhead, so these are the only
    measure of judge cost.
    """
    metrics = extract_metrics(_details_with_package_metrics())
    assert metrics["judge_seconds"] == 0.3
    assert metrics[RUN_DURATION_KEY] == 1.5


@pytest.mark.asyncio
async def test_a_reused_prediction_logs_a_run_duration_it_actually_performed():
    """Reuse narrows what Run Duration measures instead of removing it.

    It equals the judge time, the only work this run did, never the agent+judge
    sum, which would borrow an execution that did not happen here.
    """
    session = await _log(is_reused=True)
    assert "total_seconds" not in session.keys
    assert session.values[RUN_DURATION_KEY] == 0.3
    assert session.values["judge_seconds"] == 0.3


@pytest.mark.asyncio
async def test_package_metrics_survive_reuse():
    """Reuse strips agent-level keys but never package metrics."""
    session = await _log(
        is_reused=True,
        metric_results={"performance.cost.llm": {"status": "scored", "value": 2.0}},
    )
    assert "performance.cost.llm" in session.keys


@pytest.mark.asyncio
async def test_a_stored_legacy_row_is_never_rewritten():
    """The cutover is by date, not by backfill.

    Pre-cutover results carry only legacy keys, so rewriting or clearing them
    would erase their only performance record. The upsert is driven by the keys
    just produced, so a legacy key is never looked up; asserted on the lookups.
    """

    class _KeyRecordingSession(_RecordingSession):
        def __init__(self):
            super().__init__()
            self.queried = set()

        async def execute(self, stmt, *_args, **_kwargs):
            self.queried.update(
                v for v in stmt.compile().params.values() if isinstance(v, str)
            )
            return _FakeResult()

    session = _KeyRecordingSession()
    from app.services.evaluation.metrics import log_result_metrics

    await log_result_metrics(
        session,
        result_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        execution_details=_details_with_package_metrics(),
    )

    touched = session.queried & set(SHADOW_PAIRS)
    assert not touched, f"historical rows would be rewritten for {touched}"
    assert RUN_DURATION_KEY in session.queried, "the retained keys are still written"


@pytest.mark.asyncio
async def test_a_legacy_only_result_never_yields_a_package_key():
    """The logger invariant a rescore relies on.

    `rescore_run` calls `log_result_metrics(is_reused=True)` on a stored
    prediction, so a pre-cutover result must not gain a package key beside its
    legacy rows. Package keys come only from ``execution_details["metric_results"]``,
    which such a result lacks. Exercises the logger on that blob, not `rescore_run`.
    """
    legacy_only = _details()
    legacy_only.pop("metric_results", None)

    session = _RecordingSession()
    from app.services.evaluation.metrics import log_result_metrics

    await log_result_metrics(
        session,
        result_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        execution_details=legacy_only,
        is_reused=True,
    )

    assert not any(k.startswith("performance.") for k in session.keys), (
        "a legacy-only blob produced a package key it never carried"
    )
    assert session.keys == {"judge_seconds", RUN_DURATION_KEY}


def test_aggregates_still_answer_for_a_legacy_key():
    """The reduction arithmetic still accepts a legacy key.

    Covers `aggregate_metrics` only; reading a pre-cutover row through
    `get_run_aggregates` is covered by
    `test_a_stored_legacy_row_still_traverses_the_read_path` in
    `test_evaluation_aggregation.py`.
    """
    agg = aggregate_metrics([("cost_usd", 1.0), ("cost_usd", 3.0)])
    assert agg["cost_usd"]["mean"] == 2.0
    assert agg["cost_usd"]["count"] == 2


# ── Aggregates ──────────────────────────────────────────────────────────


def test_aggregate_mean_min_max():
    agg = aggregate_metrics([("latency", 10.0), ("latency", 20.0), ("latency", 30.0)])
    assert agg["latency"]["mean"] == 20.0
    assert agg["latency"]["min"] == 10.0
    assert agg["latency"]["max"] == 30.0
    assert agg["latency"]["count"] == 3


def test_aggregate_percentiles():
    vals = [("m", float(i)) for i in range(1, 101)]  # 1..100
    agg = aggregate_metrics(vals)["m"]
    assert 49 <= agg["p50"] <= 51
    assert 94 <= agg["p95"] <= 96


def test_aggregate_skips_none_values():
    agg = aggregate_metrics([("m", None), ("m", 5.0), ("m", None)])
    assert agg["m"]["count"] == 1
    assert agg["m"]["mean"] == 5.0


def test_aggregate_empty_input():
    assert aggregate_metrics([]) == {}


def test_aggregate_single_value():
    agg = aggregate_metrics([("m", 7.0)])["m"]
    assert agg["mean"] == agg["min"] == agg["max"] == agg["p50"] == agg["p95"] == 7.0


# `extract_metrics` has no token counters. Reading Anthropic's
# uncached-only `input_tokens` verbatim (a 150k-token call logged as 2) is
# guarded by the timeline tests in `test_run_ledger.py` and by the package
# adapter, which carries the cache buckets and a `convention` explicitly.


# ── The blob may come from an uploaded package ─────────────────────────
#
# The evaluation import derives metric rows from ``execution_details`` read out
# of a user-uploaded zip, so every field is untrusted. A malformed field drops
# that result's metrics; raising would fail the whole import with a 500.


@pytest.mark.parametrize(
    "blob",
    [
        "not a mapping",
        {"metric_results": "not a mapping"},
        {"judge_duration_ms": "soon"},
        {"total_duration_ms": ["9000"]},
        {"metric_results": {"json.k": {"status": "scored", "value": "ninety"}}},
    ],
    ids=["details", "results", "judge_ms", "total_ms", "value"],
)
def test_a_malformed_blob_yields_no_metrics_rather_than_raising(blob):
    assert extract_metrics(blob) == {}


def test_a_boolean_is_not_a_measurement():
    """``bool`` is an ``int``, so nothing here may quietly become 1.0 or 0.0."""
    assert extract_metrics({"judge_duration_ms": True}) == {}
    assert (
        extract_metrics({"metric_results": {"json.k": {"status": "scored", "value": True}}})
        == {}
    )


def test_the_guards_do_not_touch_a_well_formed_blob():
    """The shape the engine writes extracts unchanged."""
    m = extract_metrics(
        {
            "judge_duration_ms": 300,
            "total_duration_ms": 1500,
            "metric_results": {"json.field_accuracy": {"status": "scored", "value": 90.0}},
        }
    )
    assert m == {
        "judge_seconds": 0.3,
        RUN_DURATION_KEY: 1.5,
        "json.field_accuracy": 90.0,
    }


@pytest.mark.parametrize(
    "blob",
    [
        {"judge_duration_ms": float("inf")},
        {"total_duration_ms": float("nan")},
        {"metric_results": {"json.k": {"status": "scored", "value": float("nan")}}},
        {"metric_results": {"json.k": {"status": "scored", "value": float("-inf")}}},
        # 400 digits: parses as a Python int, then OverflowError on conversion.
        {"judge_duration_ms": int("9" * 400)},
        {"metric_results": {"json.k": {"status": "scored", "value": int("9" * 400)}}},
    ],
    ids=[
        "judge_inf", "total_nan", "value_nan", "value_neg_inf",
        "judge_overflow", "value_overflow",
    ],
)
def test_a_non_finite_number_is_not_a_measurement(blob):
    """``json.loads`` accepts ``NaN``/``Infinity``, so a package can carry them.

    They are dropped: one non-finite value would spread through that key's mean,
    max and percentiles for the whole run.
    """
    assert extract_metrics(blob) == {}


def test_a_zero_is_still_a_measurement():
    """The finite gate must not confuse "no number" with "the number nought"."""
    assert extract_metrics(
        {"metric_results": {"json.k": {"status": "scored", "value": 0.0}}}
    ) == {"json.k": 0.0}
    assert extract_metrics({"judge_duration_ms": 0})["judge_seconds"] == 0.0


def test_a_package_cannot_restate_a_platform_measurement():
    """``metric_results`` is user-supplied; these two keys are the platform's.

    They are assigned before the package blob is read, so without the guard an
    uploaded key would overwrite the measured Judge time or Run Duration.
    """
    m = extract_metrics(
        {
            "judge_duration_ms": 2000,
            "total_duration_ms": 9000,
            "metric_results": {
                "judge_seconds": {"status": "scored", "value": 999.0},
                RUN_DURATION_KEY: {"status": "scored", "value": 1.0},
                "json.field_accuracy": {"status": "scored", "value": 90.0},
            },
        }
    )
    assert m["judge_seconds"] == 2.0, "a package overwrote the measured judge time"
    assert m[RUN_DURATION_KEY] == 9.0, "a package overwrote the measured duration"
    # Everything that is genuinely the package's still arrives.
    assert m["json.field_accuracy"] == 90.0


# ── The key needs its own guard ────────────────────────────────────────
#
# ``EvaluationMetric.key`` is a ``VARCHAR``, not a JSON column, so the ORM JSON
# serializer (which strips NUL and non-finite numbers) never sees it, and the
# key comes verbatim from the package's ``metric_results``.


def test_the_key_limit_is_read_off_the_column():
    """Derived, so widening the column cannot leave a stale number behind."""
    from app.models.evaluation_metric import EvaluationMetric

    assert EvaluationMetric.__table__.c.key.type.length == _MAX_METRIC_KEY_LENGTH


@pytest.mark.parametrize(
    ("key", "storable"),
    [
        ("classification.vocabulary_validity", True),
        ("x" * _MAX_METRIC_KEY_LENGTH, True),
        ("x" * (_MAX_METRIC_KEY_LENGTH + 1), False),
        # Text columns refuse NUL outright.
        ("pkg." + chr(0) + "bad", False),
        ("", False),
    ],
    ids=["ordinary", "at_the_limit", "over_the_limit", "nul_byte", "empty"],
)
def test_only_a_key_the_column_can_hold_is_written(key, storable):
    assert _is_storable_key(key) is storable


def test_an_unbounded_key_column_means_no_length_to_enforce(monkeypatch):
    """``None`` is "no limit", not "a limit of nothing".

    The column is ``VARCHAR(100)``; widened to ``Text`` its length would be
    ``None``, and comparing against it would raise on every metric write.
    """
    import app.services.evaluation.metrics as run_metrics

    monkeypatch.setattr(run_metrics, "_MAX_METRIC_KEY_LENGTH", None)

    assert run_metrics._is_storable_key("x" * 500) is True
    # The other two reasons are unaffected by the column's width.
    assert run_metrics._is_storable_key("pkg." + chr(0) + "bad") is False
    assert run_metrics._is_storable_key("") is False


def test_every_package_metric_key_is_namespaced():
    """The convention ``is_platform_metric_key`` rests on, pinned.

    The guard refuses any unprefixed key (the platform's namespace) rather than a
    list of names, so it holds only while every package key is namespaced. This
    fails the moment one is added without a prefix.

    Keys come from the metadata objects, not the source: the performance family
    builds its keys through helpers, which a scan for string literals would miss.
    ``PerformanceMetadata`` already refuses an unprefixed key; ``MetricMetadata``
    checks only that the key is non-empty, so a quality key is where a drift
    would go unnoticed.
    """
    import importlib
    import pkgutil

    import app.evaluation_metrics as package
    from app.evaluation_metrics.performance.results import PerformanceMetadata
    from app.evaluation_metrics.quality.results import MetricMetadata

    keys: set[str] = set()
    for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        # An optional extra may be absent (the STIX backends are installed
        # separately); the count assertion below is what catches a walk that
        # collects nothing because everything failed to import.
        try:
            loaded = importlib.import_module(module.name)
        except ImportError:  # pragma: no cover - environment dependent
            continue
        for value in vars(loaded).values():
            if isinstance(value, MetricMetadata | PerformanceMetadata):
                keys.add(value.key)

    # The walk must have collected keys: an empty walk (a renamed metadata class, a
    # moved package) would pass the assertion below while guarding nothing.
    for representative in (
        "classification.micro_prf",
        "json.field_accuracy",
        "prose.rouge_l",
        "stix.entity_prf",
        "performance.latency.wall_clock",
    ):
        assert representative in keys, f"the walk collected nothing from {representative}'s family"
    assert len(keys) >= 26, f"collected only {len(keys)} keys, so the walk is not working"

    unprefixed = sorted(key for key in keys if "." not in key)
    assert not unprefixed, (
        "a package metric key is not namespaced, so the platform's namespace is no "
        f"longer the platform's alone: {unprefixed}"
    )


# --- A pooled metric's counts are stored with its value --------------------------


def _scored(key: str, value: float, **detail) -> dict:
    return {key: {"status": "scored", "value": value, "detail": detail}}


def test_a_scored_metric_s_counts_are_extracted():
    from app.services.evaluation.metrics import extract_pooled_counts

    blob = {"metric_results": _scored("performance.reliability.tool_error_rate", 0.5, numerator=5, denominator=10)}
    assert extract_pooled_counts(blob) == {"performance.reliability.tool_error_rate": (5, 10)}


@pytest.mark.parametrize(
    "result",
    [
        {"status": "unavailable", "value": None, "detail": {"numerator": 1, "denominator": 2}},
        {"status": "scored", "value": 0.5, "detail": {"numerator": True, "denominator": 2}},
        {"status": "scored", "value": 0.5, "detail": {"numerator": -1, "denominator": 2}},
        {"status": "scored", "value": 0.5, "detail": {"numerator": 3, "denominator": 2}},
        {"status": "scored", "value": 0.5, "detail": {"numerator": 0, "denominator": 0}},
        {"status": "scored", "value": 0.5, "detail": {"numerator": float("nan"), "denominator": 2}},
        {"status": "scored", "value": 0.5, "detail": "not a mapping"},
        # Consistent with its value, but above the float range: the ratio check would raise.
        {"status": "scored", "value": 1.0, "detail": {"numerator": 10**400, "denominator": 10**400}},
    ],
    ids=[
        "not_scored", "bool", "negative", "more_than_all", "zero_total", "nan", "no_detail",
        "beyond_float",
    ],
)
def test_malformed_counts_are_never_stored(result):
    """The blob can come from an uploaded package: only well-formed counts count."""
    from app.services.evaluation.metrics import extract_pooled_counts

    assert extract_pooled_counts({"metric_results": {"performance.reliability.tool_error_rate": result}}) == {}


@pytest.mark.parametrize(("value", "stored"), [(100.0, {"q.pooled": (0, 0)}), (0.0, {})])
def test_an_empty_answer_on_a_pooled_quality_score_stores_0_of_0(monkeypatch, value, stored):
    """Nothing expected and nothing given is correct: 0 of 0 beside a full score."""
    from app.services.evaluation import metrics

    monkeypatch.setattr(metrics, "is_pooled_key", lambda key: key == "q.pooled")
    monkeypatch.setattr(metrics, "is_quality_key", lambda key: key == "q.pooled")
    blob = {"metric_results": _scored("q.pooled", value, numerator=0, denominator=0)}
    assert metrics.extract_pooled_counts(blob) == stored


def test_weighted_counts_are_stored(monkeypatch):
    """A weighted share (``json.field_accuracy``: credit over weight) is not whole."""
    from app.services.evaluation.metrics import extract_pooled_counts

    blob = {"metric_results": _scored("json.field_accuracy", 75.0, numerator=1.5, denominator=2.0)}
    assert extract_pooled_counts(blob) == {"json.field_accuracy": (1.5, 2.0)}


def test_a_macro_metric_s_class_counts_are_extracted():
    from app.services.evaluation.metrics import extract_class_counts

    classes = {"a": {"tp": 1, "fp": 0, "fn": 0, "f1": 100.0}, "b": {"tp": 0, "fp": 1, "fn": 0}}
    blob = {"metric_results": _scored("classification.macro_prf", 50.0, classes=classes)}
    assert extract_class_counts(blob) == {
        "classification.macro_prf": {"a": {"tp": 1, "fp": 0, "fn": 0}, "b": {"tp": 0, "fp": 1, "fn": 0}}
    }


@pytest.mark.parametrize(
    ("value", "classes"),
    [
        (50.0, [["a", 1]]),
        (50.0, {"a": {"tp": 1, "fp": 0}}),
        (50.0, {"a": {"tp": 1.5, "fp": 0, "fn": 0}}),
        (50.0, {"a": {"tp": True, "fp": 0, "fn": 0}}),
        (50.0, {"a": {"tp": -1, "fp": 0, "fn": 0}}),
        # The classes' mean F1 is 100, not the 50 stored beside them.
        (50.0, {"a": {"tp": 1, "fp": 0, "fn": 0}}),
    ],
    ids=["not_a_mapping", "missing_count", "float", "bool", "negative", "contradicts_value"],
)
def test_malformed_class_counts_are_never_stored(value, classes):
    """The blob can come from an uploaded package: only well-formed counts that
    agree with the value count."""
    from app.services.evaluation.metrics import extract_class_counts

    blob = {"metric_results": _scored("classification.macro_prf", value, classes=classes)}
    assert extract_class_counts(blob) == {}


def test_class_counts_on_a_metric_that_is_not_macro_are_never_stored():
    from app.services.evaluation.metrics import extract_class_counts

    classes = {"a": {"tp": 1, "fp": 0, "fn": 0}}
    assert extract_class_counts({"metric_results": _scored("classification.samples_f1", 100.0, classes=classes)}) == {}


def test_a_platform_key_never_carries_counts():
    from app.services.evaluation.metrics import JUDGE_SECONDS_KEY, extract_pooled_counts

    assert extract_pooled_counts({"metric_results": _scored(JUDGE_SECONDS_KEY, 1.0, numerator=1, denominator=1)}) == {}


def test_counts_that_contradict_the_stored_value_are_never_stored():
    """An uploaded blob skips the package's own check: 1 / 10 beside a value of 0.5 would
    make the run's figure contradict its entries'."""
    from app.services.evaluation.metrics import extract_pooled_counts

    blob = {"metric_results": _scored("performance.reliability.tool_error_rate", 0.5, numerator=1, denominator=10)}
    assert extract_pooled_counts(blob) == {}


def test_counts_on_a_metric_that_is_not_pooled_are_never_stored():
    from app.services.evaluation.metrics import extract_pooled_counts

    blob = {"metric_results": _scored("performance.counts.tool_calls", 0.5, numerator=1, denominator=2)}
    assert extract_pooled_counts(blob) == {}


def test_refused_counts_are_logged_and_missing_ones_are_not(caplog):
    """A result whose counts are refused leaves its run without a value for the metric: a
    warning says why. One scored before counts existed reports none, silently."""
    import logging

    from app.services.evaluation.metrics import extract_class_counts, extract_pooled_counts

    blob = {
        "metric_results": {
            **_scored("performance.reliability.tool_error_rate", 0.5, numerator=1, denominator=10),
            **_scored("classification.macro_prf", 50.0, classes={"a": {"tp": 1, "fp": 0, "fn": 0}}),
            **_scored("classification.micro_prf", 80.0),
        }
    }
    with caplog.at_level(logging.WARNING, logger="app.services.evaluation.metrics"):
        assert extract_pooled_counts(blob) == {}
        assert extract_class_counts(blob) == {}
    refused = sorted(r.getMessage() for r in caplog.records)
    assert refused == [
        "Refusing the counts reported for metric classification.macro_prf: the classes' mean F1 is not the value stored beside them",
        "Refusing the counts reported for metric performance.reliability.tool_error_rate: their ratio is not the value stored beside them",
    ]
