"""The metric package adapters: the performance seam, the quality seam, and per-field
scoring overrides.
"""

from __future__ import annotations

import json
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.evaluation.metric_package import (
    ChainInputs,
    MetricOutcome,
    MetricStatus,
    PerformanceConfig,
    build_quality_config,
    normalize_field_scoring_overrides,
    run_performance,
    run_quality,
    to_persistable,
    validate_field_scoring_overrides,
)
from app.services.evaluation.metric_package.quality_adapter import (
    PROSE_NORMALIZATION_POLICY,
    WRAPPED_JSON_HINTS,
    QualityConfigError,
    _build_family_config,
    parse_labels,
)

# ── Performance adapter ──
# Telemetry mapping (durations, TTFT, agent-scoped call counts, token conventions), cost
# from a platform price table, the fail-soft contracts (missing price -> ``unavailable``,
# never a fabricated zero; incomplete telemetry -> ``skipped``/``unavailable``; malformed
# telemetry -> ``invalid`` rather than a crash), and the nine ``performance.*`` keys.


# --- Fixtures --------------------------------------------------------------------------

_DETAILS = {
    "agent_wall_clock_ms": 1200,
    "ttft_ms": 300,
    "traces": [
        {"type": "llm", "description": "call", "duration_ms": 800},
        {"type": "tool", "name": "search", "duration_ms": 120},
        # A judge trace is agent-external overhead and must be excluded from the
        # agent call counts.
        {"type": "judge", "duration_ms": 400},
    ],
}
_USAGE = {"input_tokens": 1500, "output_tokens": 400}
_PRICES = [
    {
        "input_price_per_1m": Decimal("2.5"),
        "output_price_per_1m": Decimal("10"),
        "cache_read_price_per_1m": None,
        "cache_write_price_per_1m": None,
        "currency": "USD",
        "resolved_model": "gpt-x",
        "resolved_provider": "openai",
    }
]


def _config() -> PerformanceConfig:
    return PerformanceConfig()


def _by_key(outcomes: list[MetricOutcome]) -> dict[str, MetricOutcome]:
    return {o.key: o for o in outcomes}


def _run(**overrides) -> dict[str, MetricOutcome]:
    kwargs = {
        "execution_details": _DETAILS,
        "token_usage": _USAGE,
        "retry_count": 0,
        "price_rows": _PRICES,
        "config": _config(),
    }
    kwargs.update(overrides)
    return _by_key(run_performance(**kwargs))


# --- Telemetry mapping -----------------------------------------------------------------


def test_wall_clock_and_ttft_are_mapped():
    outcomes = _run()
    wall = outcomes["performance.latency.wall_clock"]
    ttft = outcomes["performance.latency.ttft"]
    assert wall.status is MetricStatus.SCORED
    assert wall.value == 1200.0
    assert ttft.status is MetricStatus.SCORED
    assert ttft.value == 300.0


def test_call_counts_exclude_judge_overhead():
    outcomes = _run()
    assert outcomes["performance.counts.model_calls"].value == 1.0
    assert outcomes["performance.counts.tool_calls"].value == 1.0


def test_token_total_sums_input_and_output():
    outcomes = _run()
    tokens = outcomes["performance.tokens.total"]
    assert tokens.status is MetricStatus.SCORED
    assert tokens.value == 1900.0


# --- Cost ------------------------------------------------------------------------------


def test_cost_from_price_table():
    outcomes = _run()
    cost = outcomes["performance.cost.llm"]
    assert cost.status is MetricStatus.SCORED
    # 1500 * 2.5/1e6 + 400 * 10/1e6 = 0.00375 + 0.004 = 0.00775
    assert cost.value is not None
    assert abs(cost.value - 0.00775) < 1e-9


@pytest.mark.parametrize(
    ("overrides", "key", "reason"),
    [
        ({"price_rows": []}, "performance.cost.llm", "pricing_not_supplied"),
        # With no usage captured there is nothing to price.
        ({"token_usage": None}, "performance.cost.llm", "token_usage_not_captured"),
        # Local traces record no per-call success or failure, so reliability is not a
        # perfect completion rate.
        ({}, "performance.reliability.completion_rate", "outcome_not_captured"),
        ({}, "performance.reliability.tool_error_rate", "outcome_not_captured"),
    ],
    ids=["missing_price", "missing_token_usage", "completion_rate_without_outcomes", "tool_error_rate_without_outcomes"],
)
def test_uncaptured_telemetry_is_unavailable_never_zero(overrides, key, reason):
    outcome = _run(**overrides)[key]
    assert outcome.status is MetricStatus.UNAVAILABLE
    assert outcome.value is None
    assert outcome.detail == reason


# --- Incomplete telemetry --------------------------------------------------------------


def test_missing_ttft_degrades_only_ttft():
    details = {k: v for k, v in _DETAILS.items() if k != "ttft_ms"}
    outcomes = _run(execution_details=details)
    ttft = outcomes["performance.latency.ttft"]
    assert ttft.status in {MetricStatus.SKIPPED, MetricStatus.UNAVAILABLE}
    # Wall clock is unaffected.
    assert outcomes["performance.latency.wall_clock"].status is MetricStatus.SCORED


def test_empty_traces_still_scores_latency():
    details = {"agent_wall_clock_ms": 500, "ttft_ms": 100, "traces": []}
    outcomes = _run(execution_details=details)
    assert outcomes["performance.latency.wall_clock"].value == 500.0
    # An empty call timeline is "no calls recorded", not "zero tool calls": the
    # package degrades the count to unavailable rather than fabricating a 0.
    assert outcomes["performance.counts.tool_calls"].status in {
        MetricStatus.UNAVAILABLE,
        MetricStatus.SKIPPED,
    }


# The nine metrics the suite emits. Pinned so a metric cannot be added to the
# package, or dropped from the adapter, without this list moving with it.
_EMITTED_KEYS = {
    "performance.latency.wall_clock",
    "performance.latency.ttft",
    "performance.counts.model_calls",
    "performance.counts.tool_calls",
    "performance.counts.retries",
    "performance.tokens.total",
    "performance.cost.llm",
    "performance.reliability.completion_rate",
    "performance.reliability.tool_error_rate",
}


def test_the_adapter_emits_exactly_the_nine_performance_metrics():
    assert set(_run()) == _EMITTED_KEYS


def test_malformed_telemetry_marks_every_emitted_metric_invalid():
    """A missing metric then always means "not computed", never "the telemetry was bad"."""
    outcomes = _by_key(
        run_performance(
            execution_details={"agent_wall_clock_ms": -5},
            token_usage=None,
            retry_count=0,
            price_rows=[],
            config=_config(),
        )
    )
    assert set(outcomes) == _EMITTED_KEYS
    assert all(o.status is MetricStatus.INVALID for o in outcomes.values())


def test_the_catalogue_offers_exactly_the_emitted_metrics():
    """No metric is offered that no run can produce."""
    from app.services.evaluation.metric_package.catalog import performance_catalog

    assert {e["key"] for e in performance_catalog()} == _EMITTED_KEYS


# --- The chain ledger ----------------------------------------------------------


_LEDGER_CALLS = [
    {"kind": "model", "name": "llm_call", "model": "gpt-x", "duration_ms": 800, "outcome": "success"},
    {"kind": "tool", "name": "search", "duration_ms": 120, "outcome": "success"},
]


def _chain(calls=_LEDGER_CALLS, usage_by_model=(("gpt-x", _USAGE),), prices=None, complete=True) -> ChainInputs:
    return ChainInputs(
        calls=calls,
        token_usage_by_model=list(usage_by_model),
        price_rows_by_model=prices if prices is not None else {"gpt-x": _PRICES},
        complete=complete,
    )


def _scores(outcomes: dict[str, MetricOutcome]) -> dict[str, tuple]:
    return {k: (o.status, o.value) for k, o in outcomes.items()}


def test_a_one_model_chain_with_the_same_calls_scores_what_the_steps_did():
    """Scoring from the chain ledger changes the source, not the figures.

    Compared against steps shaped as the evaluation records them, with an outcome
    each; the shared ``_DETAILS`` fixture omits outcomes, which ledger calls never do.
    """
    recorded_steps = {
        **_DETAILS,
        "traces": [
            {**step, "outcome": "success"} if step["type"] != "judge" else step
            for step in _DETAILS["traces"]
        ],
    }
    legacy = _run(execution_details=recorded_steps)
    chained = _run(execution_details=recorded_steps, chain=_chain())

    assert _scores(chained) == _scores(legacy)


def test_every_call_of_the_chain_is_counted():
    helper_calls = [
        {"kind": "model", "name": "llm_call", "model": "gpt-x", "duration_ms": 300, "outcome": "success"},
        {"kind": "tool", "name": "lookup", "duration_ms": 50, "outcome": "error"},
    ]

    outcomes = _run(chain=_chain(calls=_LEDGER_CALLS + helper_calls))

    assert outcomes["performance.counts.model_calls"].value == 2
    assert outcomes["performance.counts.tool_calls"].value == 2


def test_each_model_of_the_chain_is_priced_at_its_own_table():
    cheap = [dict(_PRICES[0], input_price_per_1m=Decimal("0.5"), output_price_per_1m=Decimal("1"), resolved_model="gpt-mini")]
    helper_usage = {"input_tokens": 1000, "output_tokens": 100}

    outcomes = _run(
        chain=_chain(
            usage_by_model=(("gpt-x", _USAGE), ("gpt-mini", helper_usage)),
            prices={"gpt-x": _PRICES, "gpt-mini": cheap},
        )
    )

    # 1500 * 2.5/1M + 400 * 10/1M, plus 1000 * 0.5/1M + 100 * 1/1M
    assert outcomes["performance.cost.llm"].value == pytest.approx(0.00775 + 0.0006)
    assert outcomes["performance.tokens.total"].value == 1900 + 1100


def test_a_chain_with_no_call_skips_the_rates_instead_of_reporting_missing_telemetry():
    """Zero calls is a measurement: counted as 0, with no rate to state."""
    outcomes = _run(chain=_chain(calls=[], usage_by_model=()))

    assert outcomes["performance.counts.model_calls"].value == 0
    assert (outcomes["performance.reliability.completion_rate"].status, outcomes["performance.reliability.completion_rate"].detail) == (
        MetricStatus.SKIPPED,
        "no_calls",
    )
    assert (outcomes["performance.reliability.tool_error_rate"].status, outcomes["performance.reliability.tool_error_rate"].detail) == (
        MetricStatus.SKIPPED,
        "no_tool_calls",
    )


def test_an_incomplete_chain_reports_its_totals_unavailable_but_keeps_latency():
    outcomes = _run(chain=_chain(complete=False))

    for key in (
        "performance.tokens.total",
        "performance.cost.llm",
        "performance.counts.model_calls",
        "performance.counts.tool_calls",
        "performance.counts.retries",
    ):
        assert outcomes[key].status is MetricStatus.UNAVAILABLE, key
    assert outcomes["performance.latency.wall_clock"].status is MetricStatus.SCORED


def test_an_incomplete_chain_says_why_its_totals_are_unavailable():
    chain = ChainInputs(calls=[], token_usage_by_model=[], price_rows_by_model={}, complete=False, incomplete_reason="wait_bound_reached")

    outcomes = _run(chain=chain)

    for key in ("performance.tokens.total", "performance.cost.llm", "performance.counts.model_calls", "performance.reliability.completion_rate"):
        assert outcomes[key].detail == "chain_incomplete: wait_bound_reached", key
        assert outcomes[key].serialized["detail"]["reason"] == "chain_incomplete: wait_bound_reached", key
    assert outcomes["performance.latency.wall_clock"].status is MetricStatus.SCORED


# ── Quality adapter ──
# Status mapping, STRUCTURED_JSON with and without a schema, CLASSIFICATION vocabulary
# derivation and its configuration-error edge, removed families refused, PROSE and
# STIX_BUNDLE scoring, the ``QualityConfigError`` contract, and the persisted blob of two
# independently built configs being byte-identical.


def _evaluation(default_output_schema=None):
    return SimpleNamespace(default_output_schema=default_output_schema)


# --- STRUCTURED_JSON -------------------------------------------------------------------

_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "count": {"type": "integer"}},
    "required": ["name", "count"],
}


def test_structured_json_with_schema_scores() -> None:
    config = build_quality_config("STRUCTURED_JSON", _evaluation(), [])
    reference = json.dumps({"name": "alpha", "count": 2})
    prediction = json.dumps({"name": "alpha", "count": 2})
    outcomes = run_quality(
        family="STRUCTURED_JSON",
        reference=reference,
        prediction=prediction,
        output_schema=_SCHEMA,
        config=config,
    )
    assert {o.key for o in outcomes} == {"json.schema_validity", "json.field_accuracy"}
    field = next(o for o in outcomes if o.key == "json.field_accuracy")
    assert field.status is MetricStatus.SCORED
    assert field.value == 100.0


def test_structured_json_without_schema_says_so() -> None:
    """No schema -> `unavailable` with a reason that names where to fix it.

    Neither `invalid` nor `0`: the omission is the author's, not the model's. The two
    payloads are identical, so a perfect answer still scores nothing without a schema.
    """
    config = build_quality_config("STRUCTURED_JSON", _evaluation(), [])
    outcomes = run_quality(
        family="STRUCTURED_JSON",
        reference=json.dumps({"name": "a", "count": 1}),
        prediction=json.dumps({"name": "a", "count": 1}),
        output_schema=None,
        config=config,
    )

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.key == "json.field_accuracy"
    assert outcome.status is MetricStatus.UNAVAILABLE
    assert outcome.value is None
    assert "schema" in (outcome.detail or "").lower()
    assert outcome.serialized["detail"]["reason"] == outcome.detail


# Scoring by content, not by omission or order.


def _enum(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


def _object(**properties: object) -> dict:
    return {"type": "object", "properties": properties}


_FINDINGS = _object(
    f={"type": "array", "items": _object(host=_enum("A", "B"), sev=_enum("low", "high"), tlp=_enum("red", "green"))}
)
_FINDINGS_REFERENCE = {
    "f": [{"host": "A", "sev": "low", "tlp": "green"}, {"host": "B", "sev": "high", "tlp": "green"}]
}
_INTEGER = {"type": "integer"}
_TIE = _object(l={"type": "array", "items": _object(a=_INTEGER, b=_INTEGER)})
_FINDING = _object(title={"type": "string"}, seen_at={"type": "string"})
_NOISY = _object(findings={"type": "array", "items": _FINDING})
_NOISY_REFERENCE = {"findings": [{"title": "SSH brute force", "seen_at": "2024-01-01"}]}
_RIGHT = {"title": "SSH brute force", "seen_at": "2024-06-06"}
_WRONG = {"title": "Port scan", "seen_at": "2024-01-01"}
_SEEN_AT_EXCLUDED = {"field_modes": {"/findings/0/seen_at": "excluded"}}
_ACTOR = _object(
    tlp=_enum("red", "green"),
    actor=_object(name={"type": "string"}, country=_enum("RU", "CN"), motive=_enum("espionage", "crime")),
)
_ACTOR_REFERENCE = {"tlp": "red", "actor": {"name": "APT28", "country": "RU", "motive": "espionage"}}


@pytest.mark.parametrize(
    ("schema", "reference", "prediction", "expected", "overrides"),
    [
        pytest.param(
            {**_object(severity=_enum("high", "low"), actor=_enum("APT28", "APT29"), tlp=_enum("red", "green")),
             "required": ["severity"]},
            {"severity": "high", "actor": "APT28", "tlp": "red"},
            {"severity": "high"},
            33.3,
            None,
            id="case-7-an-omitted-optional-field-is-a-miss",
        ),
        pytest.param(
            _FINDINGS,
            _FINDINGS_REFERENCE,
            {"f": [{"host": "A", "sev": "high", "tlp": "green"}, {"host": "A", "sev": "low", "tlp": "red"}]},
            66.7,
            None,
            id="case-8-pairing-is-optimal",
        ),
        pytest.param(
            _FINDINGS,
            _FINDINGS_REFERENCE,
            {"f": [{"host": "A", "sev": "low", "tlp": "red"}, {"host": "A", "sev": "high", "tlp": "green"}]},
            66.7,
            None,
            id="case-8-in-the-other-order",
        ),
        pytest.param(
            _FINDINGS,
            _FINDINGS_REFERENCE,
            {"f": [{"host": "A", "sev": "low", "tlp": "green"}]},
            50.0,
            None,
            id="case-9-an-omitted-entry",
        ),
        pytest.param(
            _FINDINGS,
            _FINDINGS_REFERENCE,
            {"f": [{"host": "A", "sev": "low", "tlp": "green"}, {"host": "A", "sev": "low", "tlp": "red"}]},
            50.0,
            None,
            id="case-9-a-wrong-entry-scores-as-the-omitted-one",
        ),
        pytest.param(
            _object(iocs={"type": "array", "items": {"type": "string"}}),
            {"iocs": ["1.2.3.4", "evil.com"]},
            {"iocs": ["evil.com", "1.2.3.4"]},
            100.0,
            None,
            id="case-14-a-list-of-values-is-a-set",
        ),
        pytest.param(
            _object(iocs={"type": "array", "items": {"type": "string"}}),
            {"iocs": ["a", "b", "c"]},
            {"iocs": ["a", "b"]},
            80.0,
            None,
            id="case-14-set-f1",
        ),
        pytest.param(
            _object(ports={"type": "array", "items": {"type": "integer"}}),
            {"ports": [1, 2]},
            {"ports": ["1", "2"]},
            0.0,
            None,
            id="a-string-is-not-a-number",
        ),
        pytest.param(
            _ACTOR, _ACTOR_REFERENCE, {"tlp": "red", "actor": None}, 25.0, None, id="a-null-object-misses-its-leaves"
        ),
        pytest.param(
            _ACTOR,
            _ACTOR_REFERENCE,
            {"tlp": "red", "actor": {"name": "APT28"}},
            50.0,
            None,
            id="a-partial-object-beats-null",
        ),
        pytest.param(_TIE, {"l": [{"a": 1, "b": 2}]}, {"l": [{"a": 1}, {"a": 1, "b": 3}]}, 33.3, None, id="credit-tie"),
        pytest.param(
            _TIE, {"l": [{"a": 1, "b": 2}]}, {"l": [{"a": 1, "b": 3}, {"a": 1}]}, 33.3, None, id="credit-tie-other-order"
        ),
        pytest.param(
            _NOISY,
            _NOISY_REFERENCE,
            {"findings": [_WRONG, _RIGHT]},
            33.3,
            _SEEN_AT_EXCLUDED,
            id="an-override-follows-its-reference-entry",
        ),
        pytest.param(
            _NOISY,
            _NOISY_REFERENCE,
            {"findings": [_RIGHT, _WRONG]},
            33.3,
            _SEEN_AT_EXCLUDED,
            id="an-override-follows-its-reference-entry-other-order",
        ),
        pytest.param(
            _object(iocs={"type": "array", "items": {"type": "string"}}),
            {"iocs": ["1.2.3.4", "evil.com"]},
            {"iocs": ["evil.com", "1.2.3.4"]},
            0.0,
            {"field_modes": {"/iocs": "exact"}},
            id="exact-makes-order-count",
        ),
        pytest.param(
            _object(iocs={"type": "array", "items": {"type": "string"}}),
            {"iocs": ["evil.com"]},
            {"iocs": "evil.com"},
            0.0,
            None,
            id="a-value-for-a-list-is-a-miss",
        ),
        pytest.param(
            _object(n={"type": "array", "items": {"type": "number"}}),
            {"n": [1, 2]},
            {"n": [1.0, 2.0]},
            100.0,
            None,
            id="one-is-one-point-zero",
        ),
        pytest.param(
            _object(t={"type": "array"}),
            {"t": [{"a": 1, "b": 2}]},
            {"t": [{"b": 2, "a": 1}]},
            100.0,
            None,
            id="objects-in-a-set-compare-by-content",
        ),
        pytest.param(
            _object(p={"type": "array", "prefixItems": [_INTEGER, _INTEGER]}),
            {"p": [1, 2]},
            {"p": [2, 1]},
            0.0,
            None,
            id="a-tuple-keeps-its-order",
        ),
        pytest.param(
            _ACTOR,
            _ACTOR_REFERENCE,
            {"tlp": "red", "actor": None},
            100.0,
            {"field_modes": {"/actor": "excluded"}},
            id="an-excluded-container-drops-its-leaves",
        ),
        pytest.param(
            _ACTOR,
            _ACTOR_REFERENCE,
            {"tlp": "red", "actor": {"name": "APT28"}},
            42.9,
            {"field_weights": {"/actor": 2.0}},
            id="a-container-weight-multiplies-its-leaves",
        ),
        pytest.param(
            _ACTOR,
            _ACTOR_REFERENCE,
            {"tlp": "red", "actor": {"name": "APT28", "country": "RU"}},
            50.0,
            {"field_modes": {"/actor": "exact"}},
            id="a-mode-on-an-object-scores-it-whole",
        ),
        pytest.param(
            _ACTOR, {"tlp": "red", "actor": None}, {"tlp": "red"}, 100.0, None, id="a-reference-null-omitted-is-left-out"
        ),
        pytest.param(
            _ACTOR, {"tlp": "red"}, {"tlp": "red", "actor": None}, 100.0, None, id="an-answer-null-unasked-is-left-out"
        ),
        pytest.param(
            _object(tlp=_enum("red", "green"), iocs={"type": "array", "items": {"type": "string"}}),
            {"tlp": "red", "iocs": []},
            {"tlp": "red"},
            100.0,
            None,
            id="an-empty-reference-list-omitted-is-left-out",
        ),
        pytest.param(
            _TIE,
            {"l": [{"a": 1, "b": 2}]},
            {"l": [{"a": 1, "b": 9}, {"a": 7, "b": 2}]},
            76.9,
            {"field_weights": {"/l/0/b": 10.0}},
            id="a-weight-inside-an-entry-steers-the-pairing",
        ),
        pytest.param(
            _object(l={"type": "array", "items": {"properties": {"a": _INTEGER, "b": _INTEGER}}}),
            {"l": [{"a": 1, "b": 2}]},
            {"l": [{"a": 1, "b": 3}]},
            50.0,
            None,
            id="items-with-properties-and-no-type-are-objects",
        ),
        pytest.param(
            _object(tlp=_enum("red", "green"), f=_FINDINGS["properties"]["f"]),
            {"tlp": "red", "f": []},
            {"tlp": "red", "f": [{"host": "A", "sev": "low", "tlp": "green"}]},
            25.0,
            None,
            id="entries-against-no-reference-entry-are-extra",
        ),
        pytest.param(
            _object(tlp=_enum("red", "green"), f=_FINDINGS["properties"]["f"]),
            {"tlp": "red", "f": [{"host": "A", "sev": "low", "tlp": "green"}]},
            {"tlp": "red", "f": []},
            25.0,
            None,
            id="no-answer-entry-misses-the-reference-entries",
        ),
        pytest.param(
            _ACTOR,
            {"tlp": "red", "actor": None},
            {"tlp": "red", "actor": {"name": "APT28", "country": "RU"}},
            33.3,
            None,
            id="an-object-for-a-reference-null-is-extra",
        ),
    ],
)
def test_json_fields_and_lists_are_scored_by_content(
    schema: dict, reference: object, prediction: object, expected: float, overrides: dict | None
) -> None:
    evaluation = SimpleNamespace(default_output_schema=None, field_scoring_overrides=overrides)
    config = build_quality_config("STRUCTURED_JSON", evaluation, [])
    outcomes = run_quality(
        family="STRUCTURED_JSON",
        reference=json.dumps(reference),
        prediction=json.dumps(prediction),
        output_schema=schema,
        config=config,
    )
    field = next(o for o in outcomes if o.key == "json.field_accuracy")
    assert round(field.value, 1) == expected


def test_a_stored_override_the_policy_now_refuses_is_a_configuration_error() -> None:
    """A weight under an excluded container, saved before it was refused, reaches
    the engine as a configuration error, so the entry is not reported as failed."""
    evaluation = SimpleNamespace(
        default_output_schema=None,
        field_scoring_overrides={
            "field_modes": {"/actor": "excluded"},
            "field_weights": {"/actor/name": 2.0},
        },
    )
    config = build_quality_config("STRUCTURED_JSON", evaluation, [])
    with pytest.raises(QualityConfigError, match="/actor/name"):
        run_quality(
            family="STRUCTURED_JSON",
            reference=json.dumps(_ACTOR_REFERENCE),
            prediction=json.dumps(_ACTOR_REFERENCE),
            output_schema=_ACTOR,
            config=config,
        )


@pytest.mark.parametrize(
    "summary",
    [{"type": ["string", "null"]}, {"anyOf": [{"type": "string"}, {"type": "null"}]}],
    ids=["type-list", "any-of"],
)
def test_a_nullable_field_keeps_the_mode_of_its_type(summary: dict) -> None:
    """A nullable string is a string: a paraphrase earns partial credit."""
    config = build_quality_config("STRUCTURED_JSON", _evaluation(), [])
    outcomes = run_quality(
        family="STRUCTURED_JSON",
        reference=json.dumps({"summary": "the host was compromised by a phishing email"}),
        prediction=json.dumps({"summary": "a phishing email compromised the host"}),
        output_schema=_object(summary=summary),
        config=config,
    )
    field = next(o for o in outcomes if o.key == "json.field_accuracy")
    assert 0.0 < field.value < 100.0


def test_an_evaluation_level_schema_is_no_longer_a_fallback() -> None:
    """A schema on the evaluation is ignored; the run reports `unavailable`.

    The expected answer's shape belongs to the dataset: two evaluations sharing one would
    otherwise score the same prediction against two different contracts.
    """
    config = build_quality_config(
        "STRUCTURED_JSON", _evaluation(default_output_schema=_SCHEMA), []
    )
    assert not hasattr(config, "default_output_schema")
    outcomes = run_quality(
        family="STRUCTURED_JSON",
        reference=json.dumps({"name": "a", "count": 1}),
        prediction=json.dumps({"name": "a", "count": 1}),
        output_schema=None,
        config=config,
    )
    assert len(outcomes) == 1
    assert outcomes[0].status is MetricStatus.UNAVAILABLE
    assert "schema" in (outcomes[0].detail or "").lower()


def test_structured_json_invalid_prediction_scores_zero_not_invalid() -> None:
    """An unparseable prediction scores `SCORED`/0 on both JSON metrics, so it counts in
    the run aggregate instead of vanishing as `INVALID` would.
    """
    config = build_quality_config("STRUCTURED_JSON", _evaluation(), [])
    outcomes = run_quality(
        family="STRUCTURED_JSON",
        reference=json.dumps({"name": "a", "count": 1}),
        prediction="{not json",
        output_schema=_SCHEMA,
        config=config,
    )
    by_key = _by_key(outcomes)
    assert set(by_key) == {"json.schema_validity", "json.field_accuracy"}
    for outcome in by_key.values():
        assert outcome.status is MetricStatus.SCORED
        assert outcome.value == 0.0
        assert outcome.detail  # carries the failure reason
        # The detail is the parse error message, not the bare reason code.
        assert outcome.detail != "prediction_not_well_formed"


def test_structured_json_schema_violation_detail_has_readable_message() -> None:
    """A schema violation's detail is a readable message, not the bare `reason`
    code.
    """
    config = build_quality_config("STRUCTURED_JSON", _evaluation(), [])
    outcomes = run_quality(
        family="STRUCTURED_JSON",
        reference=json.dumps({"name": "a", "count": 1}),
        prediction=json.dumps({"name": "a", "count": "not-a-number"}),
        output_schema=_SCHEMA,
        config=config,
    )
    schema = next(o for o in outcomes if o.key == "json.schema_validity")
    assert schema.status is MetricStatus.SCORED
    assert schema.value == 0.0
    assert schema.detail == "1 schema violation(s)"


def test_structured_json_malformed_reference_is_config_error() -> None:
    config = build_quality_config("STRUCTURED_JSON", _evaluation(), [])
    with pytest.raises(QualityConfigError):
        run_quality(
            family="STRUCTURED_JSON",
            reference="{not json",
            prediction=json.dumps({"name": "a", "count": 1}),
            output_schema=_SCHEMA,
            config=config,
        )


# --- CLASSIFICATION --------------------------------------------------------------------


def test_classification_scores_against_derived_vocabulary() -> None:
    refs = ['["malware", "phishing"]', '["phishing"]']
    config = build_quality_config("CLASSIFICATION", _evaluation(), refs)
    outcomes = run_quality(
        family="CLASSIFICATION",
        reference='["malware", "phishing"]',
        prediction='["malware", "phishing"]',
        output_schema=None,
        config=config,
    )
    keys = {o.key for o in outcomes}
    assert "classification.micro_prf" in keys
    micro = next(o for o in outcomes if o.key == "classification.micro_prf")
    assert micro.status is MetricStatus.SCORED
    assert micro.value == 100.0


def test_classification_prediction_out_of_vocab_is_scored_not_withheld() -> None:
    """A hallucinated label is a wrong answer, not an unscoreable one.

    Every outcome stays ``scored``: ``metrics.py`` persists only those, so an INVALID
    gate would leave the entry with nothing to show.
    """
    config = build_quality_config("CLASSIFICATION", _evaluation(), ['["malware"]'])
    outcomes = run_quality(
        family="CLASSIFICATION",
        reference='["malware"]',
        prediction='["ransomware"]',  # not in vocabulary
        output_schema=None,
        config=config,
    )
    assert outcomes
    assert all(o.status is MetricStatus.SCORED for o in outcomes)

    gate = next(o for o in outcomes if o.key == "classification.vocabulary_validity")
    assert gate.value == 0.0

    # Nothing was recalled and something was invented: a real zero, not an absence.
    micro = next(o for o in outcomes if o.key == "classification.micro_prf")
    assert micro.value == 0.0


def test_classification_empty_vocabulary_is_config_error() -> None:
    with pytest.raises(QualityConfigError):
        build_quality_config("CLASSIFICATION", _evaluation(), ["", "   ", None])


def test_classification_declared_vocabulary_used_verbatim() -> None:
    """A declared vocabulary is used as-is, even when broader than the dataset's own
    reference answers."""
    refs = ['["malware"]']
    declared = frozenset({"malware", "phishing", "benign"})
    config = build_quality_config(
        "CLASSIFICATION", _evaluation(), refs, declared_vocabulary=declared
    )
    assert config.vocabulary == declared


def test_classification_declared_vocabulary_lets_a_broader_label_through() -> None:
    """A predicted label absent from THIS entry's reference but present in the
    declared vocabulary scores as a classification false positive (degraded, non-zero
    metrics) rather than invalidating the run outright."""
    declared = frozenset({"malware", "phishing"})
    config = build_quality_config(
        "CLASSIFICATION", _evaluation(), ['["malware"]'], declared_vocabulary=declared
    )
    outcomes = run_quality(
        family="CLASSIFICATION",
        reference='["malware"]',
        prediction='["malware", "phishing"]',
        output_schema=None,
        config=config,
    )
    assert outcomes
    micro = next(o for o in outcomes if o.key == "classification.micro_prf")
    assert micro.status is MetricStatus.SCORED
    assert 0.0 < micro.value < 100.0

    # A label absent from the declared vocabulary is reported by the gate's value and
    # penalised by the metrics, not withheld.
    offending_outcomes = run_quality(
        family="CLASSIFICATION",
        reference='["malware"]',
        prediction='["ransomware"]',
        output_schema=None,
        config=config,
    )
    gate = next(
        o for o in offending_outcomes if o.key == "classification.vocabulary_validity"
    )
    assert gate.status is MetricStatus.SCORED
    assert gate.value == 0.0


def test_classification_no_declared_vocabulary_falls_back_to_derived() -> None:
    """No declared vocabulary (``None`` or empty) derives it from the dataset's own
    reference answers."""
    refs = ['["malware", "phishing"]', '["phishing"]', "benign"]
    for declared in (None, frozenset()):
        config = build_quality_config(
            "CLASSIFICATION", _evaluation(), refs, declared_vocabulary=declared
        )
        assert config.vocabulary == frozenset({"malware", "phishing", "benign"})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('["a", "b"]', {"a", "b"}),
        ('"single"', {"single"}),
        ("a, b; c\nd", {"a", "b", "c", "d"}),
        ("benign", {"benign"}),
        ("", set()),
        (None, set()),
        (["x", " y "], {"x", "y"}),
        ('["Phishing", "`Malware`."]', {"phishing", "malware"}),
        ("Initial  Access", {"initial access"}),
        ("...", set()),
        # The marks around a label go, those inside it stay, as do those that identify it.
        ('["Phishing (high confidence).", "C#", "C"]', {"phishing (high confidence)", "c#", "c"}),
        # Prose is split as a list: the wrong label stays, the right one is clean.
        ("The answer is A, B.", {"the answer is a", "b"}),
    ],
)
def test_parse_labels(text, expected) -> None:
    """Labels compare case-folded, with edge punctuation trimmed and whitespace collapsed."""
    assert parse_labels(text) == frozenset(expected)


def test_a_label_differing_only_by_case_scores_as_the_same_label() -> None:
    """``Phishing`` against ``phishing`` scored 0 on every metric and counted as a
    vocabulary violation."""
    config = build_quality_config(
        "CLASSIFICATION", _evaluation(), ['["Phishing"]'], declared_vocabulary=frozenset({"PHISHING", "Malware"})
    )
    outcomes = _by_key(
        run_quality(
            family="CLASSIFICATION",
            reference='["Phishing"]',
            prediction="phishing.",
            output_schema=None,
            config=config,
        )
    )
    assert outcomes["classification.micro_prf"].value == 100.0
    assert outcomes["classification.vocabulary_validity"].value == 100.0


def test_the_write_time_check_accepts_exactly_what_scoring_matches() -> None:
    """The dataset entry check parses labels with the scoring rule, so an author's
    reference is refused only when it could never match."""
    from app.services.evaluation.scorers.classification_vocabulary import (
        validate_reference_labels,
    )

    assert validate_reference_labels(["Phishing", "MALWARE"], "phishing, `Malware`.") is None
    error = validate_reference_labels(["Phishing"], "phishing, Ransomware")
    assert error is not None and '"ransomware"' in error


# --- A Markdown code fence around a label list ---------------------------------

_FENCE = "`" * 3
_TECHNIQUES = '["phishing", "credential-access", "persistence"]'
_TECHNIQUE_LABELS = frozenset({"phishing", "credential-access", "persistence"})
_FENCED = [
    pytest.param(f"{_FENCE}json\n{_TECHNIQUES}\n{_FENCE}", id="json-fence"),
    pytest.param(f"{_FENCE}\n{_TECHNIQUES}\n{_FENCE}", id="bare-fence"),
    pytest.param(f"{_FENCE}json\r\n{_TECHNIQUES}\r\n{_FENCE}\n", id="crlf-fence"),
    pytest.param(f"{_FENCE}\nphishing, credential-access\npersistence\n{_FENCE}", id="fenced-list"),
    # The fences CommonMark reads: any info string, tildes, longer fences, an indented closer.
    pytest.param(f"{_FENCE} json\n{_TECHNIQUES}\n{_FENCE}", id="space-before-language"),
    pytest.param(f"{_FENCE}json title=labels\n{_TECHNIQUES}\n{_FENCE}", id="info-string"),
    pytest.param(f"{_FENCE}c#\nphishing, credential-access, persistence\n{_FENCE}", id="language-with-a-mark"),
    pytest.param(f"~~~json\n{_TECHNIQUES}\n~~~", id="tilde-fence"),
    pytest.param(f"````json\n{_TECHNIQUES}\n`````", id="four-backtick-fence"),
    pytest.param(f"{_FENCE}json\n{_TECHNIQUES}\n  {_FENCE}", id="indented-closer"),
    # Not CommonMark, but how models close a block.
    pytest.param(f"{_FENCE}json\n{_TECHNIQUES}{_FENCE}", id="closer-on-the-last-line"),
]


@pytest.mark.parametrize("answer", [pytest.param(_TECHNIQUES, id="bare-array"), *_FENCED])
def test_an_answer_that_is_one_code_block_reads_as_what_the_block_holds(answer) -> None:
    """A Markdown agent fences the array it was asked for; the fence is markup, so the
    answer reads as its labels, never as ``json`` and a backtick on top."""
    assert parse_labels(answer) == _TECHNIQUE_LABELS


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param(f'{_FENCE}\n"phishing"\n{_FENCE}', frozenset({"phishing"}), id="fenced-json-string"),
        pytest.param(f"{_FENCE}\n{_FENCE}", frozenset(), id="empty-block"),
    ],
)
def test_a_code_block_is_parsed_as_its_content_would_be(answer, expected) -> None:
    """What a block holds is parsed as an unfenced answer would be: a JSON string is one
    label, and an empty block is no label."""
    assert parse_labels(answer) == expected


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        # The prose stays a (wrong) label, as any prose is split today: only its fence goes.
        pytest.param(
            f"Here are the labels:\n{_FENCE}json\n{_TECHNIQUES}\n{_FENCE}",
            _TECHNIQUE_LABELS | {"here are the labels"},
            id="prose-before",
        ),
        # Two blocks with prose between them are not one block: split as a list.
        pytest.param(
            f'{_FENCE}json\n["phishing", "credential-access"]\n{_FENCE}\nand\n~~~\n["persistence"]\n~~~',
            _TECHNIQUE_LABELS | {"and"},
            id="two-blocks",
        ),
        # A closer shorter than its opener does not close it, as in CommonMark.
        pytest.param(f"````json\n{_TECHNIQUES}\n{_FENCE}", _TECHNIQUE_LABELS, id="short-closer"),
        # A fence that is never closed: its opener is dropped, its content still read.
        pytest.param(f"{_FENCE}json\n{_TECHNIQUES}", _TECHNIQUE_LABELS, id="unclosed"),
    ],
)
def test_a_fence_line_is_never_a_label(answer, expected) -> None:
    """An answer that is not one code block is still split as a list, its fence lines
    dropped."""
    assert parse_labels(answer) == expected


@pytest.mark.parametrize("fenced_side", ["prediction", "reference"])
@pytest.mark.parametrize("fenced", _FENCED)
def test_a_fenced_answer_on_a_perfect_entry_scores_as_the_perfect_answer_it_is(fenced_side, fenced) -> None:
    """The entry that scored TP 3, FP 2 for ``json`` and a backtick scores TP 3, FP 0, FN 0,
    whichever side holds the fence; the reference side also derives the vocabulary."""
    reference, prediction = (_TECHNIQUES, fenced) if fenced_side == "prediction" else (fenced, _TECHNIQUES)
    config = build_quality_config("CLASSIFICATION", _evaluation(), [reference])

    outcomes = _by_key(
        run_quality(
            family="CLASSIFICATION",
            reference=reference,
            prediction=prediction,
            output_schema=None,
            config=config,
        )
    )

    micro = outcomes["classification.micro_prf"].serialized["detail"]
    assert (micro["tp"], micro["fp"], micro["fn"]) == (3, 0, 0)
    assert outcomes["classification.subset_accuracy"].value == 100.0
    assert outcomes["classification.vocabulary_validity"].value == 100.0
    assert config.vocabulary == _TECHNIQUE_LABELS


@pytest.mark.parametrize("fenced", _FENCED)
def test_a_fenced_expected_answer_passes_the_dataset_vocabulary_check(fenced) -> None:
    """The dataset write-time check reads a reference with the same rule."""
    from app.services.evaluation.scorers.classification_vocabulary import (
        validate_reference_labels,
    )

    assert validate_reference_labels(sorted(_TECHNIQUE_LABELS), fenced) is None


# --- Removed families ------------------------------------------------------------------


@pytest.mark.parametrize("family", ["DETECTION_RULE", "JSON_ANALYSIS"])
def test_removed_family_is_refused_rather_than_scored_as_unavailable(family: str) -> None:
    """A removed family id is refused with a configuration error naming the valid
    families, never scored as nothing.

    `STRUCTURED_JSON` covers both.
    """
    with pytest.raises(QualityConfigError) as exc:
        build_quality_config(family, _evaluation(), [])
    assert family in str(exc.value)
    assert "STRUCTURED_JSON" in str(exc.value)


# --- PROSE ---------------------------------------------------------------------

_PROSE_SIMILARITY_KEYS = {"prose.exact_match", "prose.token_prf", "prose.rouge_l"}
_PROSE_PHRASE_KEYS = {"prose.required_phrase_coverage", "prose.forbidden_phrase_absence"}


def _prose(reference, prediction="anything") -> dict[str, MetricOutcome]:
    config = build_quality_config("PROSE", _evaluation(), [])
    outcomes = run_quality(
        family="PROSE",
        reference=reference,
        prediction=prediction,
        output_schema=None,
        config=config,
    )
    return {o.key: o for o in outcomes}


def test_the_prose_normalization_policy_is_pinned_on_all_four_axes() -> None:
    """A change to any normalization axis must be deliberate and recorded.

    It moves every prose score at once, so a silent change looks like an agent regression.
    """
    bump = (
        "The prose normalization policy changed. This alters what every prose metric "
        "computes, so you MUST also bump app/evaluation_metrics/__init__.py "
        "__version__ -- otherwise runs scored under the old policy are "
        "indistinguishable from runs where the AGENT regressed. Update the expected "
        "values here only after bumping."
    )
    assert PROSE_NORMALIZATION_POLICY.unicode_form.value == "NFC", bump
    assert PROSE_NORMALIZATION_POLICY.case.value == "casefold", bump
    assert PROSE_NORMALIZATION_POLICY.punctuation.value == "edges", bump
    assert PROSE_NORMALIZATION_POLICY.whitespace.value == "collapse", bump


def test_free_text_fields_are_scored_under_the_whole_answer_prose_policy() -> None:
    """``free_text`` fields use the very same prose policy instance as whole-answer prose.

    The two scores are read side by side, so they must share one set of rules. Identity,
    not equality: an equal copy drifts the first time one definition is edited alone.
    """
    config = build_quality_config("STRUCTURED_JSON", _evaluation(_SCHEMA), [])
    family_config = _build_family_config("STRUCTURED_JSON", _SCHEMA, config)

    assert family_config.comparison_policy.prose_policy is PROSE_NORMALIZATION_POLICY


def test_prose_exact_match_is_case_insensitive_and_inner_punctuation_sensitive() -> None:
    """Exact match ignores case but keeps the punctuation inside a token significant."""
    identical = _prose("the cat sat", "the cat sat")["prose.exact_match"]
    assert identical.status is MetricStatus.SCORED
    assert identical.value == 100.0

    # Casing alone never costs a match.
    cased = _prose("The APT29 campaign", "the apt29 campaign")["prose.exact_match"]
    assert cased.status is MetricStatus.SCORED
    assert cased.value == 100.0

    # Punctuation stays significant: CTI identifiers are distinguished by it.
    punctuated = _prose("evil-domain.com", "evil domain com")["prose.exact_match"]
    assert punctuated.status is MetricStatus.SCORED
    assert punctuated.value == 0.0


def test_prose_rouge_l_scores_and_matches_the_suite_directly() -> None:
    """ROUGE-L is graded and equals the suite's own value.

    The adapter reshapes inputs per family; a different value would mean the reshaping
    changes the measurement.
    """
    from app.evaluation_metrics.quality import (
        Family,
        FamilyConfig,
        ProseConfig,
        run_for_family,
    )

    full = _prose("the cat sat", "the cat sat")["prose.rouge_l"]
    partial = _prose("the cat sat on the mat", "the cat sat")["prose.rouge_l"]
    none_ = _prose("alpha beta", "gamma delta")["prose.rouge_l"]

    for outcome in (full, partial, none_):
        assert outcome.status is MetricStatus.SCORED
    assert full.value == 100.0
    assert 0.0 < partial.value < 100.0
    assert none_.value == 0.0

    reference, prediction = "the cat sat on the mat", "the cat sat"
    direct = run_for_family(
        reference,
        prediction,
        Family.PROSE,
        FamilyConfig(prose=ProseConfig(policy=PROSE_NORMALIZATION_POLICY)),
    )["prose.rouge_l"]
    through_adapter = _prose(reference, prediction)["prose.rouge_l"]
    assert through_adapter.value == direct.value
    assert through_adapter.status.value == direct.status.value
    assert through_adapter.serialized == direct.to_dict()


def test_prose_without_a_reference_skips_rather_than_scoring_zero() -> None:
    """An absent reference is a configuration state, not a failed answer."""
    outcomes = _prose(None, "a prediction")
    for key in _PROSE_SIMILARITY_KEYS:
        assert outcomes[key].status is MetricStatus.SKIPPED
        assert outcomes[key].value is None
        assert "reference_not_configured" in (outcomes[key].detail or "")


def test_a_non_text_prose_reference_raises_rather_than_scoring() -> None:
    """A non-text reference raises ``QualityConfigError`` and never becomes a score.

    An absent reference is configuration (`skipped`); a non-text one is a dataset error.
    The engine catches it per entry: that entry loses deterministic scoring, the judge
    still scores it, other entries are unaffected. A zero or `invalid` outcome would
    record the malformed row as a measured failure of the agent.
    """
    config = build_quality_config("PROSE", _evaluation(), [])
    with pytest.raises(QualityConfigError):
        run_quality(
            family="PROSE",
            reference={"not": "text"},
            prediction="a prediction",
            output_schema=None,
            config=config,
        )


def test_the_phrase_metrics_are_never_stored() -> None:
    """The package computes the two phrase metrics; the platform drops them.

    No evaluation can configure a phrase list, so they would be `skipped` on
    every result: noise to every author. The package keeps them and their tests.
    """
    outcomes = _prose("the cat sat", "the cat sat")
    assert set(outcomes) == _PROSE_SIMILARITY_KEYS
    assert set(outcomes).isdisjoint(_PROSE_PHRASE_KEYS)


def test_no_prose_or_phrase_configuration_surface_is_exposed() -> None:
    """No per-evaluation normalization field or phrase list exists on the evaluation.

    Two evaluations normalizing differently would produce incomparable prose scores, and
    where phrase lists belong is still undecided.
    """
    from app.api.v1.schemas.evaluation import (
        CreateEvaluationRequest,
        UpdateEvaluationRequest,
    )
    from app.models.evaluation import Evaluation

    forbidden = ("prose", "normaliz", "phrase", "casefold", "punctuation")
    surfaces = {
        "Evaluation model": set(Evaluation.__table__.columns.keys()),
        "CreateEvaluationRequest": set(CreateEvaluationRequest.model_fields),
        "UpdateEvaluationRequest": set(UpdateEvaluationRequest.model_fields),
    }
    for where, names in surfaces.items():
        offenders = [n for n in names if any(token in n.lower() for token in forbidden)]
        assert not offenders, (
            f"{where} exposes {offenders}. The prose normalization policy is fixed "
            "platform-wide and phrase lists are not configurable; adding either "
            "surface needs a decision, not just a column."
        )


def test_a_trailing_period_or_markdown_does_not_cost_a_prose_match() -> None:
    """Manual test case 15: one trailing period took token F1 from 100 to 66.7,
    bold to 33.3. Inner punctuation still distinguishes identifiers."""
    for reference, prediction in (
        ("The actor used evil.com", "The actor used evil.com."),
        ("APT29 used phishing", "**APT29** used `phishing`"),
    ):
        outcomes = _prose(reference, prediction)
        assert outcomes["prose.token_prf"].value == 100.0, prediction
        assert outcomes["prose.exact_match"].value == 100.0, prediction
    assert _prose("evil.com", "evil com")["prose.token_prf"].value == 0.0


# --- STIX_BUNDLE -----------------------------------------------------------------------

_STIX_BUNDLE = {
    "type": "bundle",
    "id": "bundle--00000000-0000-4000-8000-000000000000",
    "objects": [
        {
            "type": "identity",
            "spec_version": "2.1",
            "id": "identity--00000000-0000-4000-8000-000000000001",
            "name": "ACME",
            "identity_class": "organization",
        }
    ],
}


def test_stix_prediction_non_json_scores_zero_not_invalid() -> None:
    # A malformed STIX prediction scores 0, not INVALID, which the run aggregate would
    # drop and show as "Overall Quality: N/A".
    config = build_quality_config("STIX_BUNDLE", _evaluation(), [])
    # stix2 and pycti are required dependencies: a missing backend is a failure, not a skip.
    assert config.stix_unavailable_reason is None
    outcomes = run_quality(
        family="STIX_BUNDLE",
        reference=json.dumps(_STIX_BUNDLE),
        prediction="this is not a stix bundle",
        output_schema=None,
        config=config,
    )
    envelope = next(o for o in outcomes if o.key == "stix.envelope_validity")
    assert envelope.status is MetricStatus.SCORED
    assert envelope.value == 0.0
    assert envelope.detail  # carries the failure reason


def test_stix_malformed_reference_is_config_error() -> None:
    config = build_quality_config("STIX_BUNDLE", _evaluation(), [])
    # stix2 and pycti are required dependencies: a missing backend is a failure, not a skip.
    assert config.stix_unavailable_reason is None
    with pytest.raises(QualityConfigError):
        run_quality(
            family="STIX_BUNDLE",
            reference="{not json",
            prediction=json.dumps(_STIX_BUNDLE),
            output_schema=None,
            config=config,
        )


# --- A 0 the answer's wrapping caused says so -------------------------------


def _json_outcomes(prediction: str) -> dict[str, MetricOutcome]:
    config = build_quality_config("STRUCTURED_JSON", _evaluation(), [])
    return _by_key(
        run_quality(
            family="STRUCTURED_JSON",
            reference=json.dumps({"name": "a", "count": 1}),
            prediction=prediction,
            output_schema=_SCHEMA,
            config=config,
        )
    )


@pytest.mark.parametrize(
    "prediction",
    [
        pytest.param('```json\n{"name": "a", "count": 1}\n```', id="code-fence"),
        pytest.param('Here is the answer: {"name": "a", "count": 1}', id="surrounding-text"),
    ],
)
def test_a_wrapped_json_answer_scores_0_and_says_why(prediction: str) -> None:
    """Scoring stays strict, as the agent's consumers parse strictly, but the stored
    reason names the wrapping and the two ways out."""
    outcomes = _json_outcomes(prediction)
    hint = WRAPPED_JSON_HINTS["STRUCTURED_JSON"]
    assert outcomes["json.field_accuracy"].value == 0.0
    for outcome in outcomes.values():
        if outcome.value == 0.0:
            assert outcome.serialized["detail"]["hint"] == hint
            assert hint in (outcome.detail or "")


@pytest.mark.parametrize(
    "prediction",
    [
        pytest.param('{"name": "a", "count": 1', id="broken-json"),
        pytest.param("no json here", id="prose"),
        pytest.param('{"name": "a", "count": 1}', id="bare-json"),
    ],
)
def test_no_hint_when_no_json_hides_in_the_answer(prediction: str) -> None:
    for outcome in _json_outcomes(prediction).values():
        assert "hint" not in (outcome.serialized.get("detail") or {})


def _stix_outcomes(prediction: str) -> dict[str, MetricOutcome]:
    config = build_quality_config("STIX_BUNDLE", _evaluation(), [])
    return _by_key(
        run_quality(
            family="STIX_BUNDLE",
            reference=json.dumps(_STIX_BUNDLE),
            prediction=prediction,
            output_schema=None,
            config=config,
        )
    )


def test_an_identical_bundle_without_relationships_scores_full_marks() -> None:
    """No relationship on either side is an empty answer, correct."""
    outcomes = _stix_outcomes(json.dumps(_STIX_BUNDLE))
    for key in ("stix.entity_prf", "stix.entity_prf.by_type", "stix.relationship_prf"):
        assert outcomes[key].value == 100.0, key


def test_an_object_of_an_unknown_stix_type_is_a_false_positive() -> None:
    """A type no implementation knows is a scored miss, not an unscored entry."""
    custom = {
        "type": "x-custom-thing",
        "spec_version": "2.1",
        "id": "x-custom-thing--00000000-0000-4000-8000-000000000002",
        "name": "thing",
    }
    prediction = {**_STIX_BUNDLE, "objects": [*_STIX_BUNDLE["objects"], custom]}
    entity = _stix_outcomes(json.dumps(prediction))["stix.entity_prf"]
    assert round(entity.value, 1) == 66.7
    assert (entity.serialized["detail"]["tp"], entity.serialized["detail"]["fp"]) == (1, 1)


def test_an_empty_prose_expected_answer_is_left_out() -> None:
    """An empty expected answer is a draft, never a score for the agent."""
    config = build_quality_config("PROSE", _evaluation(), [])
    outcomes = run_quality(
        family="PROSE", reference="", prediction="the cat sat", output_schema=None, config=config
    )
    assert {o.key: (o.status, o.serialized["detail"]["reason"]) for o in outcomes} == {
        key: (MetricStatus.SKIPPED, "reference_empty")
        for key in ("prose.exact_match", "prose.token_prf", "prose.rouge_l")
    }


def _list_of(count: int, nested: int = 0) -> str:
    entry = {"n": [{"w": str(index)} for index in range(nested)]} if nested else {}
    return json.dumps({"l": [{"v": str(index), **entry} for index in range(count)]})


_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "l": {
            "type": "array",
            "items": {"type": "object", "properties": {"n": {"type": "array", "items": {"type": "object"}}}},
        }
    },
}


@pytest.mark.parametrize(
    ("reference", "prediction", "nested", "status", "reason"),
    [
        pytest.param(50, 300, 0, MetricStatus.SCORED, "prediction_too_many_entries", id="the-answer-overflows"),
        # 1,000 outer pairs of 100 nested pairs each; the reference paired with itself is
        # 100 outer pairs of 100: 10,100.
        pytest.param(10, 100, 10, MetricStatus.SCORED, "prediction_too_many_entries", id="a-nested-answer-overflows"),
        pytest.param(150, 150, 0, MetricStatus.SKIPPED, "pairing_input_too_large", id="the-reference-overflows"),
    ],
)
def test_a_list_too_long_to_pair_scores_0_only_when_the_answer_overflows(
    reference: int, prediction: int, nested: int, status: MetricStatus, reason: str
) -> None:
    """The reference paired with itself fits, so the excess is the answer's and scores 0;
    a reference too long to pair is our limit, left out."""
    config = build_quality_config("STRUCTURED_JSON", _evaluation(), [])
    outcomes = _by_key(
        run_quality(
            family="STRUCTURED_JSON",
            reference=_list_of(reference, nested),
            prediction=_list_of(prediction, nested),
            output_schema=_LIST_SCHEMA,
            config=config,
        )
    )
    field = outcomes["json.field_accuracy"]
    assert (field.status, field.serialized["detail"]["reason"]) == (status, reason)
    assert field.value == (0.0 if status is MetricStatus.SCORED else None)


def test_an_empty_free_text_field_on_both_sides_is_correct() -> None:
    """Nothing expected and nothing given."""
    config = build_quality_config("STRUCTURED_JSON", _evaluation(), [])
    outcomes = _by_key(
        run_quality(
            family="STRUCTURED_JSON",
            reference=json.dumps({"note": "", "tlp": "red"}),
            prediction=json.dumps({"note": "", "tlp": "red"}),
            output_schema={"type": "object", "properties": {"note": {"type": "string"}, "tlp": {"type": "string", "enum": ["red"]}}},
            config=config,
        )
    )
    assert outcomes["json.field_accuracy"].value == 100.0


def test_a_fenced_stix_bundle_says_why_its_not_well_formed_zeros_scored_0() -> None:
    """The hint goes to the zeros the wrapping caused, and only to them: semantic validity
    fails for its own reason. STIX has no extractor, so the hint does not offer one."""
    outcomes = _stix_outcomes(f"```json\n{json.dumps(_STIX_BUNDLE)}\n```")
    hint = WRAPPED_JSON_HINTS["STIX_BUNDLE"]
    assert "extractor" not in hint
    for key in ("stix.envelope_validity", "stix.entity_prf", "stix.relationship_prf"):
        assert outcomes[key].serialized["detail"]["hint"] == hint, key
    semantic = outcomes["stix.semantic_validity"]
    assert semantic.value == 0.0
    assert "hint" not in semantic.serialized["detail"]


def test_a_bare_bundle_is_never_said_to_be_wrapped() -> None:
    """A fence inside one of its strings does not make a bare JSON answer "wrapped"."""
    bundle = {
        "type": "bundle",
        "id": "bundle--00000000-0000-4000-8000-000000000000",
        "objects": [{"type": "note", "content": "see ```[1, 2]```"}],
    }
    envelope = _stix_outcomes(json.dumps(bundle))["stix.envelope_validity"]
    assert envelope.value == 0.0  # the note has no id
    assert "hint" not in envelope.serialized["detail"]


# --- Config / persistence --------------------------------------------------------------


def test_run_quality_rejects_unknown_family() -> None:
    config = build_quality_config("STRUCTURED_JSON", _evaluation(), [])
    with pytest.raises(QualityConfigError):
        run_quality(
            family="NOPE",
            reference="x",
            prediction="y",
            output_schema=None,
            config=config,
        )


_PROSE_KEYS = _PROSE_SIMILARITY_KEYS


@pytest.mark.parametrize(
    ("family", "reference", "prediction", "dataset_refs", "output_schema", "expected_keys"),
    [
        ("STIX_BUNDLE", json.dumps(_STIX_BUNDLE), json.dumps(_STIX_BUNDLE), None, None, None),
        (
            "STRUCTURED_JSON",
            json.dumps({"name": "a", "count": 1}),
            json.dumps({"name": "a", "count": 2}),
            None,
            {**_SCHEMA, "required": ["name"]},
            None,
        ),
        # The vocabulary is derived from the whole dataset's references, so both paths
        # must see the same references to derive the same one.
        (
            "CLASSIFICATION",
            '["malware", "phishing"]',
            '["malware"]',
            ['["malware", "phishing"]', '["ransomware"]'],
            None,
            None,
        ),
        # Partial overlap fills the graded metrics' detail payloads, where a varying
        # detail could hide behind an all-100 case.
        (
            "PROSE",
            "The APT29 campaign targeted evil-domain.com",
            "the apt29 campaign targeted example.org",
            None,
            None,
            _PROSE_KEYS,
        ),
    ],
    ids=["stix_bundle", "structured_json", "classification", "prose"],
)
def test_a_family_blob_is_byte_stable(
    family, reference, prediction, dataset_refs, output_schema, expected_keys
) -> None:
    """Two independently built configs persist byte-identical ``metric_results``.

    The inline run and a rescore each build the family config from scratch, score with
    ``run_quality`` and persist ``to_persistable``: re-scoring stored predictions must
    reproduce the blob the run persisted.
    """

    def blob() -> bytes:
        config = build_quality_config(
            family, _evaluation(output_schema), dataset_refs or [reference]
        )
        assert getattr(config, "stix_unavailable_reason", None) is None
        outcomes = run_quality(
            family=family,
            reference=reference,
            prediction=prediction,
            output_schema=output_schema,
            config=config,
        )
        persisted = to_persistable(outcomes)
        assert persisted
        if expected_keys is not None:
            assert set(persisted) == expected_keys
        return json.dumps(persisted, sort_keys=True).encode()

    assert blob() == blob()


def test_to_persistable_keys_by_metric_key() -> None:
    outcomes = [
        MetricOutcome(
            key="json.field_accuracy",
            status=MetricStatus.SCORED,
            value=100.0,
            detail=None,
            serialized={"status": "scored", "value": 100.0},
        ),
        MetricOutcome(
            key="json.schema_validity",
            status=MetricStatus.SCORED,
            value=100.0,
            detail=None,
            serialized={"status": "scored", "value": 100.0},
        ),
    ]
    blob = to_persistable(outcomes)
    assert set(blob) == {"json.field_accuracy", "json.schema_validity"}
    assert blob["json.field_accuracy"] == {"status": "scored", "value": 100.0}


def test_a_vocabulary_violation_survives_into_the_persisted_metrics() -> None:
    """A vocabulary violation is readable from the persisted row, not only in memory.

    ``extract_metrics`` indexes only ``scored`` outcomes, so this runs the family,
    persists as the engine does, then reads the blob back.
    """
    from app.services.evaluation.metric_package.quality_adapter import to_persistable
    from app.services.evaluation.metrics import extract_metrics

    config = build_quality_config("CLASSIFICATION", _evaluation(), ['["malware"]'])
    outcomes = run_quality(
        family="CLASSIFICATION",
        reference='["malware"]',
        prediction='["malware", "ransomware"]',
        output_schema=None,
        config=config,
    )

    indexed = extract_metrics({"metric_results": to_persistable(outcomes)})

    # The violation itself, as a number a run aggregate can carry.
    assert indexed["classification.vocabulary_validity"] == 0.0
    # And the penalty beside it, rather than an entry with nothing on it at all.
    assert "classification.micro_prf" in indexed
    assert 0.0 < indexed["classification.micro_prf"] < 100.0


# ── Per-field scoring overrides ──
# An override changes how a field is judged, never what the right answer is: the dataset
# owns the answer, the evaluation owns what this measurement cares about, and two
# evaluations reusing one dataset may disagree without editing each other's ground truth.
# These exercise the real adapter and the real policy, not a mock of either.


_OVERRIDES_SCHEMA = {
    "type": "json_schema",
    "definition": {
        "type": "object",
        "properties": {
            "verdict": {"type": "string", "enum": ["clean", "malicious"]},
            "summary": {"type": "string"},
            "score": {"type": "number"},
        },
        "required": ["verdict", "summary", "score"],
    },
}


def _score(
    reference: str,
    prediction: str,
    overrides: dict | None = None,
) -> float | None:
    evaluation = SimpleNamespace(
        default_output_schema=None, field_scoring_overrides=overrides
    )
    config = build_quality_config("STRUCTURED_JSON", evaluation, [])
    outcomes = run_quality(
        family="STRUCTURED_JSON",
        reference=reference,
        prediction=prediction,
        output_schema=_OVERRIDES_SCHEMA,
        config=config,
    )
    return next(o.value for o in outcomes if o.key == "json.field_accuracy")


_REFERENCE = '{"verdict": "malicious", "summary": "beaconing to a C2 host", "score": 8}'
_PREDICTION = '{"verdict": "malicious", "summary": "talks to a command server", "score": 8}'


# --------------------------------------------------------------------------- overrides


def test_an_override_changes_only_the_overridden_field() -> None:
    """Overriding one field must not move the others.

    The two untouched fields are exact matches in both runs, so any movement beyond
    ``/summary``'s own contribution means the override leaked across fields.
    """
    derived = _score(_REFERENCE, _PREDICTION)
    exacted = _score(_REFERENCE, _PREDICTION, {"field_modes": {"/summary": "exact"}})
    assert derived is not None and exacted is not None
    # Derived treatment reads the paraphrase as prose and gives partial credit; forcing
    # `exact` makes the same answer wrong. Two exact fields out of three survive.
    assert exacted == pytest.approx(200 / 3)
    assert derived > exacted


def test_an_excluded_field_neither_earns_credit_nor_incurs_a_penalty() -> None:
    """Exclusion removes the field from the denominator, whatever the agent returned.

    Counted as a miss, it would penalise the author for not caring about it; counted as
    a hit, it would inflate every score that used the override.
    """
    wrong = '{"verdict": "clean", "summary": "beaconing to a C2 host", "score": 8}'
    excluded = {"field_modes": {"/verdict": "excluded"}}

    # The excluded field is wrong: the two remaining fields are both right, so 100.
    assert _score(_REFERENCE, wrong, excluded) == pytest.approx(100.0)
    # The excluded field is right: still 100 over the same two fields, not 100 over three.
    assert _score(_REFERENCE, _REFERENCE, excluded) == pytest.approx(100.0)
    # And without the override the wrong field costs exactly its share.
    assert _score(_REFERENCE, wrong) == pytest.approx(200 / 3)


def test_a_weight_changes_a_field_s_share_of_the_total() -> None:
    """A weight is the "this matters more" control, and it has to actually bite."""
    wrong_verdict = '{"verdict": "clean", "summary": "beaconing to a C2 host", "score": 8}'
    unweighted = _score(_REFERENCE, wrong_verdict)
    weighted = _score(
        _REFERENCE, wrong_verdict, {"field_weights": {"/verdict": 4.0}}
    )
    assert unweighted is not None and weighted is not None
    assert weighted < unweighted


# --------------------------------------------------------------------------- derived treatment


def test_with_no_override_the_treatment_follows_from_the_schema_alone() -> None:
    """The derivation table is the default, and no configuration is required to get it.

    An enum field is compared as an enum, a plain string as prose.
    """
    for empty in (None, {}, {"field_modes": {}, "field_weights": {}}):
        assert _score(_REFERENCE, _PREDICTION, empty) == _score(_REFERENCE, _PREDICTION)

    # Derived: `/summary` is prose, so a paraphrase is not a zero — and not a 100 either.
    derived = _score(_REFERENCE, _PREDICTION)
    assert derived is not None
    assert 200 / 3 < derived < 100.0


# --------------------------------------------------------------------------- write-time validation


@pytest.mark.parametrize("weighted", ["/summary", "/summary/text"], ids=["field", "under-container"])
def test_a_weight_on_an_excluded_field_is_refused(weighted: str) -> None:
    """Contradictory configuration is refused at write time, not half-honoured: an excluded
    container drops its leaves."""
    error = validate_field_scoring_overrides(
        {"field_modes": {"/summary": "excluded"}, "field_weights": {weighted: 2.0}}
    )
    assert error is not None
    assert weighted in error


def test_an_unknown_mode_or_negative_weight_is_refused() -> None:
    """Write-time validation delegates to the scorer's own policy.

    The API never accepts a configuration the scorer would reject.
    """
    assert validate_field_scoring_overrides({"field_modes": {"/a": "sort_of"}})
    assert validate_field_scoring_overrides({"field_weights": {"/a": -1}})
    assert validate_field_scoring_overrides({"field_weights": {"/a": "two"}})
    assert validate_field_scoring_overrides({"unexpected": {}})
    assert validate_field_scoring_overrides({"field_modes": []})


def test_an_absent_or_empty_configuration_is_accepted() -> None:
    """"No overrides" is the overwhelmingly common case and must never be an error."""
    assert validate_field_scoring_overrides(None) is None
    assert validate_field_scoring_overrides({}) is None
    assert (
        validate_field_scoring_overrides({"field_modes": {}, "field_weights": {}})
        is None
    )


def test_a_free_text_override_is_accepted_at_write_time() -> None:
    """``free_text`` needs a prose policy, and every structured-JSON run carries one.

    The write-time check asks the same question the run asks.
    """
    assert (
        validate_field_scoring_overrides({"field_modes": {"/verdict": "free_text"}})
        is None
    )


def test_a_malformed_stored_configuration_degrades_to_derived_treatment() -> None:
    """A hand-edited or historical row must not crash a run.

    The column is free-form JSON and predates the validator. A run that cannot read
    its overrides scores under derived treatment, as with no overrides at all.
    """
    for junk in ("not a mapping", 42, {"field_modes": "nope"}, {"field_weights": []}):
        assert _score(_REFERENCE, _PREDICTION, junk) == _score(_REFERENCE, _PREDICTION)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- normalizing


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        pytest.param(None, None, id="none"),
        pytest.param({}, None, id="empty-object"),
        pytest.param({"field_modes": {}}, None, id="empty-modes"),
        pytest.param({"field_modes": {}, "field_weights": None}, None, id="says-nothing"),
        pytest.param(
            {"field_modes": {"/a": "exact"}, "field_weights": {}},
            {"field_modes": {"/a": "exact"}},
            id="drops-the-empty-half",
        ),
        pytest.param(
            {"field_weights": {"/a": 2.0}},
            {"field_weights": {"/a": 2.0}},
            id="keeps-a-real-override",
        ),
        pytest.param("not-an-object", None, id="not-an-object"),
    ],
)
def test_an_override_object_that_declares_nothing_is_stored_as_null(
    stored: object, expected: dict | None
) -> None:
    """An override object that declares nothing is stored as `NULL`.

    `NULL` is the one canonical "no overrides" state, whichever endpoint wrote it.
    Scoring treats `{}` and `NULL` alike; the column keeps one spelling of "nothing".
    """
    assert normalize_field_scoring_overrides(stored) == expected  # type: ignore[arg-type]


# ── STIX objects match as a reviewer would, without crashing ──────────────────

_T = "2024-01-01T00:00:00.000Z"


def _sdo(stix_type: str, n: int, **fields: object) -> dict[str, object]:
    return {
        "type": stix_type,
        "spec_version": "2.1",
        "id": f"{stix_type}--00000000-0000-4000-8000-{n:012d}",
        "created": _T,
        "modified": _T,
        **fields,
    }


def _sco(stix_type: str, n: int, **fields: object) -> dict[str, object]:
    return {"type": stix_type, "spec_version": "2.1", "id": f"{stix_type}--00000000-0000-4000-8000-{n:012d}", **fields}


def _stix(reference: list[dict], prediction: list[dict]) -> dict[str, MetricOutcome]:
    def bundle(objects: list[dict]) -> str:
        return json.dumps({"type": "bundle", "id": "bundle--00000000-0000-4000-8000-000000000000", "objects": objects})

    config = build_quality_config("STIX_BUNDLE", _evaluation(), [])
    assert config.stix_unavailable_reason is None
    return _by_key(
        run_quality(
            family="STIX_BUNDLE", reference=bundle(reference), prediction=bundle(prediction), output_schema=None, config=config
        )
    )


def _counts(outcome: MetricOutcome) -> tuple[int, int, int]:
    detail = outcome.serialized["detail"]
    return detail["tp"], detail["fp"], detail["fn"]


_MALWARE = _sdo("malware", 1, name="Emotet", is_family=True)


@pytest.mark.parametrize(
    "unidentifiable",
    [
        _sdo("malware", 2, is_family=False),
        _sdo("identity", 3, name="ACME"),
        _sdo("location", 4, country="FR"),
        _sdo("sighting", 5, sighting_of_ref=_MALWARE["id"]),
    ],
    ids=["malware-without-name", "identity-without-class", "location-with-only-a-country", "sighting-without-a-place"],
)
def test_a_valid_object_pycti_cannot_identify_is_scored_never_a_crash(unidentifiable: dict) -> None:
    """In the answer it is a miss; in the expected answer, the same object matches it."""
    extra = _stix([_MALWARE], [_MALWARE, unidentifiable])
    same = _stix([_MALWARE, unidentifiable], [_MALWARE, unidentifiable])

    if unidentifiable["type"] == "sighting":
        assert _counts(extra["stix.relationship_prf"]) == (0, 1, 0)
        assert _counts(same["stix.relationship_prf"]) == (1, 0, 0)
    else:
        assert _counts(extra["stix.entity_prf"]) == (1, 1, 0)
        assert _counts(same["stix.entity_prf"]) == (2, 0, 0)


_IPV4 = _sco("ipv4-addr", 10, value="1.2.3.4")
_IPV4_OTHER_ID = {**_IPV4, "id": "ipv4-addr--00000000-0000-4000-8000-000000000011"}
_DST = _sco("ipv4-addr", 12, value="5.6.7.8")
_DST_OTHER_ID = {**_DST, "id": "ipv4-addr--00000000-0000-4000-8000-000000000013"}
_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_TOOL = _sdo("tool", 20, name="Cobalt Strike")
_PROCESS_V5_ID = "process--5f3b2a1e-0c4d-5e6f-8a9b-0c1d2e3f4a5b"


def _observed(n: int, ref: str) -> dict[str, object]:
    return _sdo(
        "observed-data", n, first_observed="2024-01-01T00:00:00Z", last_observed="2024-01-01T00:00:00Z",
        number_observed=1, object_refs=[ref],
    )


def _uses(relationship_type: str) -> dict[str, object]:
    return _sdo("relationship", 30, relationship_type=relationship_type, source_ref=_MALWARE["id"], target_ref=_TOOL["id"])


@pytest.mark.parametrize(
    ("reference", "prediction"),
    [
        ([_IPV4, _observed(40, _IPV4["id"])], [_IPV4_OTHER_ID, _observed(41, _IPV4_OTHER_ID["id"])]),
        ([_observed(40, _IPV4["id"]), _IPV4], [_observed(41, _IPV4_OTHER_ID["id"]), _IPV4_OTHER_ID]),
        (
            [_IPV4, _DST, _sco("network-traffic", 42, src_ref=_IPV4["id"], dst_ref=_DST["id"], protocols=["tcp"])],
            [_IPV4_OTHER_ID, _DST_OTHER_ID, _sco("network-traffic", 43, src_ref=_IPV4_OTHER_ID["id"], dst_ref=_DST_OTHER_ID["id"], protocols=["tcp"])],
        ),
        (
            [_MALWARE, _sdo("report", 44, name="Q1", published="2024-01-01T00:00:00Z", object_refs=[_MALWARE["id"]])],
            [_MALWARE, _sdo("report", 44, name="Q1", published="2024-01-01T00:00:00.000Z", object_refs=[_MALWARE["id"]])],
        ),
        (
            [_MALWARE, _sdo("note", 45, content="Seen on the VPN", object_refs=[_MALWARE["id"]])],
            [_MALWARE, {**_sdo("note", 45, content="Seen on the VPN", object_refs=[_MALWARE["id"]]), "created": "2024-02-02T00:00:00.000Z"}],
        ),
        ([_sco("domain-name", 46, value="evil.com")], [_sco("domain-name", 46, value="Evil.com")]),
        ([_sco("file", 47, hashes={"SHA-256": _SHA256})], [_sco("file", 47, hashes={"SHA-256": _SHA256.upper()})]),
        (
            [_sdo("indicator", 48, pattern="[ipv4-addr:value = '1.2.3.4']", pattern_type="stix", valid_from=_T)],
            [_sdo("indicator", 48, pattern="[ipv4-addr:value='1.2.3.4']", pattern_type="stix", valid_from=_T)],
        ),
        ([_TOOL], [{**_TOOL, "name": "Cobalt  Strike"}]),
        ([_MALWARE, _TOOL, _uses("uses")], [_MALWARE, _TOOL, _uses("Uses")]),
        # stix2 gives a process a random id: never a match by chance, and never a miss.
        ([_sco("process", 49, pid=1337)], [_sco("process", 49, pid=1337)]),
        ([_sco("ipv6-addr", 54, value="2001:db8::1")], [_sco("ipv6-addr", 54, value="2001:DB8:0:0::1")]),
        (
            [_sdo("malware-analysis", 56, product="sandbox", analysis_started="2024-01-01T00:00:00Z")],
            [_sdo("malware-analysis", 56, product="sandbox", analysis_started="2024-01-01T00:00:00.000Z")],
        ),
        ([_sco("file", 55, hashes={"SHA-256": _SHA256})], [_sco("file", 55, hashes={"sha_256": _SHA256.upper()})]),
        # STIX requires an id: an answer's object without one fails the envelope check, an
        # expected answer's is identified like any other.
        ([{k: v for k, v in _TOOL.items() if k != "id"}], [_TOOL]),
    ],
    ids=[
        "observed-data-raw-ids", "observed-data-listed-first", "network-traffic-raw-ids", "report-timestamp-form", "note-created",
        "domain-case", "hash-case", "pattern-spacing", "name-spacing", "relationship-type-case",
        "process-random-official-id", "ipv6-form", "malware-analysis-timestamp", "hash-name-spelling", "expected-object-without-id",
    ],
)
def test_the_same_content_written_another_way_matches(reference: list[dict], prediction: list[dict]) -> None:
    outcomes = _stix(reference, prediction)
    assert outcomes["stix.entity_prf"].value == 100.0
    assert outcomes["stix.relationship_prf"].value == 100.0


@pytest.mark.parametrize(
    ("reference", "prediction"),
    [
        ([_sco("domain-name", 46, value="evil.com")], [_sco("domain-name", 46, value="evil.org")]),
        (
            [_sdo("report", 44, name="Q1", published="2024-01-01T00:00:00Z")],
            [_sdo("report", 44, name="Q1", published="2024-01-02T00:00:00Z")],
        ),
        ([_MALWARE, _TOOL, _uses("uses")], [_MALWARE, _TOOL, _uses("drops")]),
        # An extension is part of a file's identity; an SSDEEP hash is case-sensitive.
        (
            [_sco("file", 47, name="a.zip", extensions={"archive-ext": {"contains_refs": [], "comment": "x"}})],
            [_sco("file", 47, name="a.zip")],
        ),
        ([_sco("file", 47, hashes={"SSDEEP": "3:AXGBicFlgVNhBGcL6wCrFQEv:AXGHsNhxLsr2C"})], [_sco("file", 47, hashes={"SSDEEP": "3:axgbicflgvnhbgcl6wcrfqev:axghsnhxlsr2c"})]),
        ([_sco("file", 47, hashes={"TLSH": "T1" + "AB" * 35})], [_sco("file", 47, hashes={"TLSH": "T1" + "ab" * 35})]),
        ([_sco("file", 47, hashes={"TLSH": "AB" * 35})], [_sco("file", 47, hashes={"TLSH": "ab" * 35})]),
        # Only a STIX pattern is re-serialized: a YARA rule's layout is part of it.
        (
            [_sdo("indicator", 48, pattern="rule a {\n condition: true\n}", pattern_type="yara", valid_from=_T)],
            [_sdo("indicator", 48, pattern="rule a { condition: true }", pattern_type="yara", valid_from=_T)],
        ),
        ([_sco("domain-name", 46, value="straße.de")], [_sco("domain-name", 46, value="strasse.de")]),
        # A file's name is compared as written; only a timestamp property is a timestamp.
        ([_sco("file", 47, name="a  b.txt")], [_sco("file", 47, name="a b.txt")]),
        ([_sdo("malware", 1, name="2024-01-01T00:00:00Z", is_family=True)], [_sdo("malware", 1, name="2024-01-01T00:00:00.000Z", is_family=True)]),
        # A network written with its host bits is another value.
        ([_sco("ipv4-addr", 57, value="10.0.0.1/24")], [_sco("ipv4-addr", 57, value="10.0.0.0/24")]),
        # The author's id is never the identifier, even a deterministic-looking one.
        ([{**_sco("process", 49, pid=1), "id": _PROCESS_V5_ID}], [{**_sco("process", 49, pid=2), "id": _PROCESS_V5_ID}]),
    ],
    ids=[
        "another-domain", "another-publication-date", "another-relationship-type", "another-file-extension",
        "another-fuzzy-hash", "tlsh-case", "tlsh-without-prefix-case", "yara-layout", "idn-domain",
        "file-name-spacing", "timestamp-shaped-name", "network-host-bits", "author-uuid5-id",
    ],
)
def test_other_content_still_does_not_match(reference: list[dict], prediction: list[dict]) -> None:
    outcomes = _stix(reference, prediction)
    assert min(outcomes["stix.entity_prf"].value, outcomes["stix.relationship_prf"].value) < 100.0


def test_the_manual_test_s_stix_matching_case_scores_full_marks() -> None:
    """Manual test case 12 scores full marks."""
    domain = _sco("domain-name", 50, value="evil.com")
    file = _sco("file", 51, hashes={"SHA-256": _SHA256})
    talks = _sdo("relationship", 52, relationship_type="communicates-with", source_ref=_MALWARE["id"], target_ref=domain["id"])
    report = _sdo("report", 53, name="Emotet wave", published="2024-01-01T00:00:00Z", object_refs=[_MALWARE["id"]])
    outcomes = _stix(
        [_MALWARE, domain, talks, file, report],
        [
            _MALWARE,
            {**domain, "value": "Evil.com"},
            talks,
            {**file, "hashes": {"SHA-256": _SHA256.upper()}},
            {**report, "published": "2024-01-01T00:00:00.000Z"},
        ],
    )
    assert outcomes["stix.entity_prf"].value == 100.0
    assert outcomes["stix.relationship_prf"].value == 100.0


def test_a_sighting_is_scored_as_a_relationship() -> None:
    where = _sdo("identity", 60, name="ACME", identity_class="organization")
    seen = _sdo("sighting", 61, sighting_of_ref=_MALWARE["id"], where_sighted_refs=[where["id"]])
    found = _stix([_MALWARE, where, seen], [_MALWARE, where, seen])
    missed = _stix([_MALWARE, where, seen], [_MALWARE, where])

    assert _counts(found["stix.relationship_prf"]) == (1, 0, 0)
    assert _counts(missed["stix.relationship_prf"]) == (0, 0, 1)


@pytest.mark.parametrize(
    ("first_modified", "second_modified", "kept"),
    [("2024-01-01T00:00:00.000Z", "2024-02-01T00:00:00.000Z", "second"), ("2024-02-01T00:00:00.000Z", "2024-01-01T00:00:00.000Z", "first"), (_T, _T, "second")],
    ids=["later-second", "later-first", "tie"],
)
def test_objects_sharing_an_id_are_versions_and_the_latest_counts(first_modified, second_modified, kept) -> None:
    first = {**_TOOL, "name": "Mimikatz", "modified": first_modified}
    second = {**_TOOL, "name": "Cobalt Strike", "modified": second_modified}
    latest = second if kept == "second" else first
    outcomes = _stix([_MALWARE, latest, _uses("uses")], [_MALWARE, first, second, _uses("uses")])

    assert _counts(outcomes["stix.entity_prf"]) == (2, 0, 0)
    assert _counts(outcomes["stix.relationship_prf"]) == (1, 0, 0)


def test_an_incident_is_identified_with_when_it_happened() -> None:
    """pycti needs an incident's ``created``: the authoring metadata is left out only where
    the identifier does without it."""
    incident = _sdo("incident", 70, name="Emotet wave", description="Phishing wave")
    outcomes = _stix([incident], [{**incident, "description": "A phishing wave hit the SOC"}])
    assert outcomes["stix.entity_prf"].value == 100.0


def _custom(n: int, name: str, ref: str) -> dict[str, object]:
    return _sco("x-acme-thing", n, name=name, peer_ref=ref)


def test_objects_that_refer_to_each_other_match_whatever_their_order_and_ids() -> None:
    a, b = "x-acme-thing--00000000-0000-4000-8000-000000000081", "x-acme-thing--00000000-0000-4000-8000-000000000082"
    c, d = "x-acme-thing--00000000-0000-4000-8000-000000000091", "x-acme-thing--00000000-0000-4000-8000-000000000092"
    reference = [{**_custom(81, "left", b), "id": a}, {**_custom(82, "right", a), "id": b}, _observed(83, a)]
    prediction = [_observed(93, c), {**_custom(92, "right", c), "id": d}, {**_custom(91, "left", d), "id": c}]
    outcomes = _stix(reference, prediction)
    assert _counts(outcomes["stix.entity_prf"]) == (3, 0, 0)


def test_a_reference_is_mapped_once_even_when_an_id_is_another_object_s_canonical_one() -> None:
    """An answer whose second tool reuses the first tool's canonical id still relates the
    malware to the first tool."""
    from app.evaluation_metrics.quality.backends.stix_opencti import OpenCtiCanonicalizer

    bundle = {"type": "bundle", "id": "bundle--00000000-0000-4000-8000-000000000000", "objects": [_TOOL]}
    canonical_id = OpenCtiCanonicalizer().canonicalize(bundle)["objects"][0]["id"]
    mimikatz = {**_sdo("tool", 21, name="Mimikatz")}
    reference = [_MALWARE, _TOOL, mimikatz, _uses("uses")]
    prediction = [_MALWARE, _TOOL, {**mimikatz, "id": canonical_id}, _uses("uses")]
    assert _counts(_stix(reference, prediction)["stix.relationship_prf"]) == (1, 0, 0)


def test_a_sighting_whose_places_are_not_a_list_is_not_well_formed() -> None:
    sighting = _sdo("sighting", 62, sighting_of_ref=_MALWARE["id"], where_sighted_refs="")
    outcome = _stix([_MALWARE], [_MALWARE, sighting])["stix.relationship_prf"]
    assert (outcome.value, outcome.serialized["detail"]["reason"]) == (0.0, "prediction_not_well_formed")


def _thing(n: int, name: str, *refs: str) -> dict[str, object]:
    return {"type": "x-acme-thing", "id": f"x-acme-thing--00000000-0000-4000-8000-{n:012d}", "name": name, "peer_refs": list(refs)}


@pytest.mark.parametrize(
    "shape",
    ["cycles-around-an-object", "three-member-cycle", "self-reference", "nested-reference"],
)
def test_references_never_leak_raw_ids_whatever_the_graph(shape: str) -> None:
    """Both sides write the same content with other raw ids, in another order."""

    def objects(base: int, reverse: bool) -> list[dict]:
        def tid(k: int) -> str:
            return f"x-acme-thing--00000000-0000-4000-8000-{base + k:012d}"

        if shape == "cycles-around-an-object":
            # A1 <-> A2 -> X -> C1 <-> C2
            built = [
                _thing(base + 1, "a1", tid(2)), _thing(base + 2, "a2", tid(1), tid(3)),
                _thing(base + 3, "x", tid(4)), _thing(base + 4, "c1", tid(5)), _thing(base + 5, "c2", tid(4)),
            ]
        elif shape == "three-member-cycle":
            built = [_thing(base + 1, "a", tid(2)), _thing(base + 2, "b", tid(3)), _thing(base + 3, "c", tid(1))]
        elif shape == "self-reference":
            built = [_thing(base + 1, "self", tid(1))]
        else:
            inner = _sco("file", base + 1, name="inner.txt")
            built = [inner, _sco("file", base + 2, name="a.zip", extensions={"archive-ext": {"contains_refs": [inner["id"]]}})]
        return built[::-1] if reverse else built

    outcomes = _stix(objects(100, False), objects(200, True))
    tp, fp, fn = _counts(outcomes["stix.entity_prf"])
    assert (fp, fn) == (0, 0) and tp > 0


def test_a_long_chain_of_references_is_identified() -> None:
    chain = [_thing(1000 + k, f"link {k}", f"x-acme-thing--00000000-0000-4000-8000-{1001 + k:012d}") for k in range(3000)]
    outcomes = _stix(chain, chain[::-1])
    assert outcomes["stix.entity_prf"].value == 100.0
