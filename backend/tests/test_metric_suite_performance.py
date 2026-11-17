"""The performance metrics: identity and the uncaptured-vs-observed rule for every
emitted metric, cost, call counts, latency, overhead exclusion, reliability, tokens, the raw
mapping validation boundary, and the telemetry input contract.
"""

import ast
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.evaluation_metrics.core.contracts import MetricDirection, MetricStatus
from app.evaluation_metrics.core.errors import ConfigurationError
from app.evaluation_metrics.performance.metrics import (
    call_counts,
    estimated_llm_cost,
    reliability,
    retry_count,
    time_to_first_token,
    token_usage_total,
    wall_clock_latency,
)
from app.evaluation_metrics.performance.pricing import PriceTable
from app.evaluation_metrics.performance.results import PerformanceResult
from app.evaluation_metrics.performance.telemetry import (
    CallKind,
    CallOutcome,
    CallRecord,
    CallScope,
    ExecutionTelemetry,
    ModelTokenUsage,
    TelemetryValidation,
    TokenConvention,
    TokenUsage,
    validate_telemetry,
)
from app.evaluation_metrics.performance.units import MetricUnit
from tests.evaluation_helpers import PACKAGE_DIR

# ── identity and the uncaptured-vs-observed rule ──

WALL_CLOCK = "performance.latency.wall_clock"
TTFT = "performance.latency.ttft"
MODEL_CALLS = "performance.counts.model_calls"
TOOL_CALLS = "performance.counts.tool_calls"
RETRIES = "performance.counts.retries"
TOTAL_TOKENS = "performance.tokens.total"
COST = "performance.cost.llm"
COMPLETION_RATE = "performance.reliability.completion_rate"
TOOL_ERROR_RATE = "performance.reliability.tool_error_rate"

PRICES = PriceTable(
    currency="USD",
    uncached_input_per_token=Decimal("0.000003"),
    output_per_token=Decimal("0.000015"),
)

Metric = Callable[[ExecutionTelemetry], PerformanceResult | Mapping[str, PerformanceResult]]


def _cost(telemetry: ExecutionTelemetry) -> PerformanceResult:
    return estimated_llm_cost(telemetry, PRICES)


# Each emitted key and the public function that reports it.
_METRICS: dict[str, Metric] = {
    WALL_CLOCK: wall_clock_latency,
    TTFT: time_to_first_token,
    MODEL_CALLS: call_counts,
    TOOL_CALLS: call_counts,
    RETRIES: retry_count,
    TOTAL_TOKENS: token_usage_total,
    COST: _cost,
    COMPLETION_RATE: reliability,
    TOOL_ERROR_RATE: reliability,
}


def _result(key: str, telemetry: ExecutionTelemetry) -> PerformanceResult:
    out = _METRICS[key](telemetry)
    return out[key] if isinstance(out, Mapping) else out


_CAPTURED = ExecutionTelemetry(
    wall_clock_ms=1_000.0,
    ttft_ms=250.0,
    calls=(
        CallRecord(kind=CallKind.model, scope=CallScope.agent, outcome=CallOutcome.success),
        CallRecord(kind=CallKind.tool, scope=CallScope.agent, outcome=CallOutcome.success),
    ),
    agent_token_usage=TokenUsage(
        input_tokens=100, output_tokens=20, convention=TokenConvention.separate
    ),
    retry_count=1,
)

_LOWER = MetricDirection.lower_is_better
_HIGHER = MetricDirection.higher_is_better


@pytest.mark.parametrize(
    ("key", "kind", "primary_measure", "unit", "direction"),
    [
        (WALL_CLOCK, "duration", "wall_clock", MetricUnit.milliseconds, _LOWER),
        (TTFT, "duration", "ttft", MetricUnit.milliseconds, _LOWER),
        (MODEL_CALLS, "count", "model_calls", MetricUnit.count, _LOWER),
        (TOOL_CALLS, "count", "tool_calls", MetricUnit.count, _LOWER),
        (RETRIES, "count", "retries", MetricUnit.count, _LOWER),
        (TOTAL_TOKENS, "tokens", "total_tokens", MetricUnit.tokens, _LOWER),
        (COST, "cost", "estimated_cost", MetricUnit.currency, _LOWER),
        (COMPLETION_RATE, "rate", "completion_rate", MetricUnit.ratio, _HIGHER),
        (TOOL_ERROR_RATE, "rate", "tool_error_rate", MetricUnit.ratio, _LOWER),
    ],
    ids=[
        "wall_clock",
        "ttft",
        "model_calls",
        "tool_calls",
        "retries",
        "total_tokens",
        "cost",
        "completion_rate",
        "tool_error_rate",
    ],
)
def test_every_metric_declares_its_key_unit_kind_and_direction(
    key: str, kind: str, primary_measure: str, unit: MetricUnit, direction: MetricDirection
) -> None:
    """The key, unit and direction are what the run views read to label and rank a value."""
    metadata = _result(key, _CAPTURED).metadata

    assert metadata.key == key
    assert metadata.kind == kind
    assert metadata.primary_measure == primary_measure
    assert metadata.unit is unit
    assert metadata.direction is direction


@pytest.mark.parametrize(
    ("metric", "keys"),
    [
        (call_counts, {MODEL_CALLS, TOOL_CALLS}),
        (reliability, {COMPLETION_RATE, TOOL_ERROR_RATE}),
    ],
    ids=["call_counts", "reliability"],
)
def test_a_metric_returning_a_mapping_reports_exactly_its_own_keys(
    metric: Callable[[ExecutionTelemetry], Mapping[str, PerformanceResult]], keys: set[str]
) -> None:
    assert set(metric(_CAPTURED)) == keys


def _calls(calls: tuple[CallRecord, ...] | None) -> ExecutionTelemetry:
    return ExecutionTelemetry(calls=calls)


def _usage(usage: TokenUsage | None) -> ExecutionTelemetry:
    return ExecutionTelemetry(agent_token_usage=usage)


def _retries(retries: int | None) -> ExecutionTelemetry:
    return ExecutionTelemetry(calls=(), retry_count=retries)


_ZERO_USAGE = TokenUsage(input_tokens=0, output_tokens=0, convention=TokenConvention.separate)
_NO_BUCKET = TokenUsage(convention=TokenConvention.separate)
_OFF = MetricStatus.unavailable
_ON = MetricStatus.scored
_SKIP = MetricStatus.skipped


@pytest.mark.parametrize(
    ("key", "telemetry", "status", "value", "reason"),
    [
        pytest.param(WALL_CLOCK, ExecutionTelemetry(), _OFF, None, "wall_clock_not_captured", id="wall_clock-uncaptured"),
        pytest.param(TTFT, ExecutionTelemetry(wall_clock_ms=10.0), _OFF, None, "ttft_not_captured", id="ttft-uncaptured"),
        pytest.param(MODEL_CALLS, _calls(None), _OFF, None, "calls_not_captured", id="model_calls-uncaptured"),
        pytest.param(MODEL_CALLS, _calls(()), _ON, 0.0, None, id="model_calls-observed_empty"),
        pytest.param(TOOL_CALLS, _calls(None), _OFF, None, "calls_not_captured", id="tool_calls-uncaptured"),
        pytest.param(TOOL_CALLS, _calls(()), _ON, 0.0, None, id="tool_calls-observed_empty"),
        pytest.param(RETRIES, _retries(None), _OFF, None, "retry_count_not_captured", id="retries-uncaptured"),
        pytest.param(RETRIES, _retries(0), _ON, 0.0, None, id="retries-observed_zero"),
        pytest.param(TOTAL_TOKENS, _usage(None), _OFF, None, "token_usage_not_captured", id="total_tokens-uncaptured"),
        pytest.param(
            TOTAL_TOKENS, _usage(_NO_BUCKET), _OFF, None, "token_usage_not_captured", id="total_tokens-no_bucket"
        ),
        pytest.param(TOTAL_TOKENS, _usage(_ZERO_USAGE), _ON, 0.0, None, id="total_tokens-observed_zero"),
        pytest.param(COST, _usage(None), _OFF, None, "token_usage_not_captured", id="cost-uncaptured"),
        pytest.param(COMPLETION_RATE, _calls(None), _OFF, None, "calls_not_captured", id="completion_rate-uncaptured"),
        pytest.param(TOOL_ERROR_RATE, _calls(None), _OFF, None, "calls_not_captured", id="tool_error_rate-uncaptured"),
        # Zero calls leave no rate to state: skipped, never read as missing telemetry.
        pytest.param(
            COMPLETION_RATE, _calls(()), _SKIP, None, "no_calls", id="completion_rate-observed_empty"
        ),
        pytest.param(
            TOOL_ERROR_RATE, _calls(()), _SKIP, None, "no_tool_calls", id="tool_error_rate-observed_empty"
        ),
    ],
)
def test_uncaptured_telemetry_is_unavailable_and_observed_empty_is_a_measurement(
    key: str,
    telemetry: ExecutionTelemetry,
    status: MetricStatus,
    value: float | None,
    reason: str | None,
) -> None:
    """``None`` means "never recorded" and is never reported as ``0``; ``()`` or ``0`` is,
    or, for a rate, which zero calls leave without a denominator, ``skipped``."""
    result = _result(key, telemetry)

    assert result.status is status
    assert result.value == value
    assert result.error is None
    assert result.detail.get("reason") == reason


# ── LLM-only cost from caller-supplied pricing ──

USAGE = TokenUsage(
    input_tokens=1000,
    output_tokens=300,
    cache_read_input_tokens=200,
    cache_write_input_tokens=50,
    reasoning_output_tokens=120,
    convention=TokenConvention.separate,
)

FULL_PRICES = PriceTable(
    currency="USD",
    uncached_input_per_token=Decimal("0.000003"),
    output_per_token=Decimal("0.000015"),
    cache_read_per_token=Decimal("0.0000003"),
    cache_write_per_token=Decimal("0.00000375"),
    resolved_model="gpt-5",
    resolved_provider="openai",
    price_snapshot_id="2026-08-01",
)


def test_cost_equals_the_sum_of_normalized_bucket_times_price() -> None:
    result = estimated_llm_cost(_usage(USAGE), FULL_PRICES)

    assert FULL_PRICES.uncached_input_per_token is not None
    assert FULL_PRICES.output_per_token is not None
    assert FULL_PRICES.cache_read_per_token is not None
    assert FULL_PRICES.cache_write_per_token is not None
    expected = (
        Decimal(1000) * FULL_PRICES.uncached_input_per_token
        + Decimal(300) * FULL_PRICES.output_per_token
        + Decimal(200) * FULL_PRICES.cache_read_per_token
        + Decimal(50) * FULL_PRICES.cache_write_per_token
    )

    assert result.status is MetricStatus.scored
    assert result.value == pytest.approx(float(expected))


def test_detail_echoes_the_cost_basis() -> None:
    result = estimated_llm_cost(_usage(USAGE), FULL_PRICES)

    assert result.detail["currency"] == "USD"
    assert result.detail["resolved_model"] == "gpt-5"
    assert result.detail["resolved_provider"] == "openai"
    assert result.detail["price_snapshot_id"] == "2026-08-01"
    assert result.detail["prices"] == {
        "uncached_input_tokens": "0.000003",
        "output_tokens": "0.000015",
        "cache_read_input_tokens": "0.0000003",
        "cache_write_input_tokens": "0.00000375",
    }
    assert result.detail["normalized_counts"] == {
        "uncached_input_tokens": 1000,
        "output_tokens": 300,
        "cache_read_input_tokens": 200,
        "cache_write_input_tokens": 50,
    }


def test_missing_price_table_is_unavailable_not_zero() -> None:
    result = estimated_llm_cost(_usage(USAGE))

    assert result.status is MetricStatus.unavailable
    assert result.value is None
    assert result.detail["reason"] == "pricing_not_supplied"


@pytest.mark.parametrize(
    ("usage", "prices"),
    [
        (USAGE, PriceTable(currency="USD", uncached_input_per_token=Decimal("0.000003"))),
        # ``uncached_input_tokens`` is observed as 0 (captured, not missing), so it still
        # needs a price: omitting one is a caller error even though it would add 0.
        (
            TokenUsage(input_tokens=0, output_tokens=100, convention=TokenConvention.separate),
            PriceTable(currency="USD", output_per_token=Decimal("0.000015")),
        ),
    ],
    ids=["non_zero_bucket", "observed_zero_bucket"],
)
def test_a_captured_bucket_without_a_price_is_a_configuration_error(
    usage: TokenUsage, prices: PriceTable
) -> None:
    with pytest.raises(ConfigurationError):
        estimated_llm_cost(_usage(usage), prices)


# --- no hard-coded pricing ---------------------------------------------------------

_PRICING_MODULES = (
    str(PACKAGE_DIR / "performance/pricing.py"),
    str(PACKAGE_DIR / "performance/metrics/cost.py"),
)
_PROVIDERS = ("openai", "anthropic", "google", "mistral", "cohere")


@pytest.mark.parametrize("module", _PRICING_MODULES, ids=lambda module: Path(module).name)
def test_no_price_is_hard_coded(module: str) -> None:
    """Tripwire: pricing is caller-supplied, so the package holds no price of its own."""
    source = Path(module).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            pytest.fail(f"{module} contains float literal {node.value!r}")
        if (
            isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id == "Decimal")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "Decimal")
            )
            and any(
                isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                for arg in [*node.args, *(keyword.value for keyword in node.keywords)]
            )
        ):
            pytest.fail(f"{module} builds a Decimal from a string literal at line {node.lineno}")
    for provider in _PROVIDERS:
        assert provider not in source.lower(), f"{module} appears to embed a {provider} price table"


# --- Per-model pricing ------------------------------------------------

CHEAP_PRICES = PriceTable(
    currency="USD",
    uncached_input_per_token=Decimal("0.0000001"),
    output_per_token=Decimal("0.0000004"),
    cache_read_per_token=Decimal("0.00000001"),
    cache_write_per_token=Decimal("0.0000001"),
    resolved_model="gpt-5-mini",
)
EURO_PRICES = CHEAP_PRICES.model_copy(update={"currency": "EUR"})

HELPER_USAGE = TokenUsage(
    input_tokens=4000,
    output_tokens=500,
    cache_read_input_tokens=1000,
    cache_write_input_tokens=0,
    convention=TokenConvention.inclusive,
)


def _split(*parts: tuple[str, TokenUsage]) -> ExecutionTelemetry:
    return ExecutionTelemetry(
        agent_token_usage_by_model=[ModelTokenUsage(model=m, usage=u) for m, u in parts]
    )


def test_each_model_is_priced_at_its_own_table_and_the_parts_are_summed() -> None:
    telemetry = _split(("gpt-5", USAGE), ("gpt-5-mini", HELPER_USAGE))

    result = estimated_llm_cost(
        telemetry, prices_by_model={"gpt-5": FULL_PRICES, "gpt-5-mini": CHEAP_PRICES}
    )

    alone = estimated_llm_cost(_usage(USAGE), FULL_PRICES).value
    helper = estimated_llm_cost(_usage(HELPER_USAGE), CHEAP_PRICES).value
    assert result.status is MetricStatus.scored
    assert result.value == pytest.approx(alone + helper)
    assert [p["model"] for p in result.detail["parts"]] == ["gpt-5", "gpt-5-mini"]
    assert result.detail["currency"] == "USD"


@pytest.mark.parametrize(
    "pricing",
    [{"prices_by_model": {"gpt-5": FULL_PRICES}}, {"prices": FULL_PRICES}],
    ids=["prices_by_model", "one_table"],
)
def test_one_model_through_the_split_costs_exactly_what_it_did(pricing: dict[str, Any]) -> None:
    split = estimated_llm_cost(_split(("gpt-5", USAGE)), **pricing)
    single = estimated_llm_cost(_usage(USAGE), FULL_PRICES)

    assert (split.status, split.value, split.detail) == (single.status, single.value, single.detail)


@pytest.mark.parametrize(
    ("pricing", "reason"),
    [
        ({"prices_by_model": {"gpt-5": FULL_PRICES}}, "pricing_not_supplied"),
        ({"prices_by_model": {"gpt-5": FULL_PRICES, "gpt-5-mini": EURO_PRICES}}, "mixed_currency"),
        # One table given for several models must not price a helper at the agent's rates.
        ({"prices": FULL_PRICES}, "pricing_per_model_not_supplied"),
    ],
    ids=["a_model_has_no_price", "different_currencies", "one_table_for_several_models"],
)
def test_a_split_that_cannot_be_priced_whole_is_unavailable_not_partial(
    pricing: dict[str, Any], reason: str
) -> None:
    result = estimated_llm_cost(_split(("gpt-5", USAGE), ("gpt-5-mini", HELPER_USAGE)), **pricing)

    assert result.status is MetricStatus.unavailable
    assert result.detail == {"reason": reason}


def test_total_tokens_normalize_each_model_before_adding() -> None:
    """Two conventions are never summed raw: ``inclusive`` input already holds its cache."""
    result = token_usage_total(_split(("gpt-5", USAGE), ("gpt-5-mini", HELPER_USAGE)))

    alone = token_usage_total(_usage(USAGE)).value
    helper = token_usage_total(_usage(HELPER_USAGE)).value
    assert result.value == alone + helper
    assert result.detail["convention"] == "mixed"


# ── agent-scoped model and tool call counts ──

MODEL_KEY = "performance.counts.model_calls"
TOOL_KEY = "performance.counts.tool_calls"


def _call(kind: CallKind, name: str | None = None) -> CallRecord:
    return CallRecord(kind=kind, scope=CallScope.agent, name=name, duration_ms=10.0)


def _telemetry_counts(
    calls: Sequence[CallRecord] | None, retries: int | None = None
) -> ExecutionTelemetry:
    return ExecutionTelemetry(calls=calls, retry_count=retries)


def test_model_and_tool_calls_are_counted_separately() -> None:
    calls = (
        _call(CallKind.model),
        _call(CallKind.model),
        _call(CallKind.tool, name="search"),
        _call(CallKind.tool),
    )

    results = call_counts(_telemetry_counts(calls))

    assert results[MODEL_KEY].value == 2.0
    assert results[TOOL_KEY].value == 2.0
    # Sorted by tool name, so the detail is byte-stable; a call without a name still counts.
    assert list(results[TOOL_KEY].detail["by_tool"].items()) == [("<unnamed>", 1), ("search", 1)]


def test_retries_are_distinct_from_the_underlying_call_count() -> None:
    calls = (_call(CallKind.model), _call(CallKind.model))

    result = retry_count(_telemetry_counts(calls, retries=5))

    # Retries are their own measurement, never derived from the call sequence.
    assert result.value == 5.0


# ── latency: the authoritative wall clock and the time to first token ──
# Both are reported as the caller supplied them, never rebuilt from per-call durations.

def _call_latency(kind: CallKind, duration: float | None, scope: CallScope = CallScope.agent) -> CallRecord:
    return CallRecord(
        kind=kind, scope=scope, duration_ms=duration, outcome=CallOutcome.success
    )


def _telemetry_latency() -> ExecutionTelemetry:
    return ExecutionTelemetry(
        wall_clock_ms=1_000.0,
        ttft_ms=250.0,
        calls=(
            _call_latency(CallKind.model, 400.0),
            _call_latency(CallKind.model, 150.0),
            _call_latency(CallKind.tool, 200.0),
            _call_latency(CallKind.model, 90.0, scope=CallScope.overhead),
        ),
    )


@pytest.mark.parametrize(
    ("metric", "key", "value"),
    [
        (wall_clock_latency, "performance.latency.wall_clock", 1_000.0),
        (time_to_first_token, "performance.latency.ttft", 250.0),
    ],
    ids=["wall_clock", "ttft"],
)
def test_the_supplied_duration_is_reported_as_given(
    metric: Callable[[ExecutionTelemetry], PerformanceResult], key: str, value: float
) -> None:
    """The caller's duration is authoritative, never rebuilt from the per-call durations."""
    result = metric(_telemetry_latency())

    assert result.status is MetricStatus.scored
    assert result.value == value
    assert result.metadata.key == key
    assert result.metadata.unit is MetricUnit.milliseconds


# ── evaluation overhead never moves an agent-scoped value ──
# The suite reports no overhead metric of its own: overhead-scoped calls and usage are
# excluded from every agent metric, so adding overhead leaves each one byte-identical.

_AGENT_CALLS = (
    CallRecord(kind=CallKind.model, scope=CallScope.agent, outcome=CallOutcome.success),
    CallRecord(kind=CallKind.tool, scope=CallScope.agent, name="search", outcome=CallOutcome.success),
)
# Failing overhead calls: counting them would add calls and lower both rates.
_OVERHEAD_CALLS = (
    CallRecord(kind=CallKind.model, scope=CallScope.overhead, outcome=CallOutcome.error),
    CallRecord(kind=CallKind.tool, scope=CallScope.overhead, name="judge", outcome=CallOutcome.error),
)


def _telemetry_overhead(*, with_overhead: bool) -> ExecutionTelemetry:
    return ExecutionTelemetry(
        wall_clock_ms=1_000.0,
        ttft_ms=250.0,
        calls=_AGENT_CALLS + _OVERHEAD_CALLS if with_overhead else _AGENT_CALLS,
        agent_token_usage=TokenUsage(
            input_tokens=1000, output_tokens=200, convention=TokenConvention.separate
        ),
        overhead_token_usage=(
            TokenUsage(input_tokens=400, output_tokens=100, convention=TokenConvention.separate)
            if with_overhead
            else None
        ),
        retry_count=1,
    )


@pytest.mark.parametrize(
    ("metric", "key"),
    [
        (wall_clock_latency, "performance.latency.wall_clock"),
        (time_to_first_token, "performance.latency.ttft"),
        (call_counts, "performance.counts.model_calls"),
        (call_counts, "performance.counts.tool_calls"),
        (retry_count, "performance.counts.retries"),
        (token_usage_total, "performance.tokens.total"),
        (_cost, "performance.cost.llm"),
        (reliability, "performance.reliability.completion_rate"),
        (reliability, "performance.reliability.tool_error_rate"),
    ],
    ids=[
        "wall_clock",
        "ttft",
        "model_calls",
        "tool_calls",
        "retries",
        "total_tokens",
        "cost",
        "completion_rate",
        "tool_error_rate",
    ],
)
def test_overhead_calls_and_usage_leave_every_agent_metric_byte_identical(
    metric: Callable[[ExecutionTelemetry], PerformanceResult | Mapping[str, PerformanceResult]],
    key: str,
) -> None:
    def result(telemetry: ExecutionTelemetry) -> PerformanceResult:
        out = metric(telemetry)
        return out[key] if isinstance(out, Mapping) else out

    base = result(_telemetry_overhead(with_overhead=False))

    assert base.status is MetricStatus.scored
    assert result(_telemetry_overhead(with_overhead=True)).to_json() == base.to_json()


# ── pooled reliability rates and deterministic failure breakdowns ──

COMPLETION_KEY = "performance.reliability.completion_rate"
TOOL_ERROR_KEY = "performance.reliability.tool_error_rate"


def _call_reliability(
    kind: CallKind = CallKind.model,
    outcome: CallOutcome | None = CallOutcome.success,
    name: str | None = None,
    error_type: str | None = None,
) -> CallRecord:
    return CallRecord(
        kind=kind, scope=CallScope.agent, outcome=outcome, name=name, error_type=error_type
    )


# --- pooled rates, per-status counts, breakdowns --------------------------------------


_OK, _ERROR, _TIMEOUT, _CANCELLED = (
    CallOutcome.success,
    CallOutcome.error,
    CallOutcome.timeout,
    CallOutcome.cancelled,
)


@pytest.mark.parametrize(
    ("outcomes", "rate", "counts"),
    [
        (
            (_OK, _OK, _ERROR, _TIMEOUT, _CANCELLED),
            2 / 5,
            {"success": 2, "error": 1, "timeout": 1, "cancelled": 1},
        ),
        # Every status is reported, even one no call ended with.
        ((_OK, _ERROR), 1 / 2, {"success": 1, "error": 1, "timeout": 0, "cancelled": 0}),
    ],
    ids=["every_status", "unseen_statuses_are_zero"],
)
def test_the_completion_rate_counts_every_non_success_as_incomplete(
    outcomes: tuple[CallOutcome, ...], rate: float, counts: dict[str, int]
) -> None:
    result = reliability(_calls(tuple(_call_reliability(outcome=o) for o in outcomes)))[COMPLETION_KEY]

    assert result.value == rate
    assert {status: result.detail[status] for status in counts} == counts


def test_tool_error_rate_is_scoped_to_tool_calls() -> None:
    calls = (
        _call_reliability(kind=CallKind.model, outcome=CallOutcome.error),
        _call_reliability(kind=CallKind.tool, outcome=CallOutcome.success, name="search"),
        _call_reliability(
            kind=CallKind.tool,
            outcome=CallOutcome.error,
            name="search",
            error_type="timeout_error",
        ),
    )

    result = reliability(_calls(calls))[TOOL_ERROR_KEY]

    assert result.status is MetricStatus.scored
    assert result.value == 1 / 2


def test_tool_error_rate_breakdowns_by_tool_status_and_error_type() -> None:
    calls = (
        _call_reliability(
            kind=CallKind.tool,
            outcome=CallOutcome.error,
            name="search",
            error_type="rate_limited",
        ),
        _call_reliability(kind=CallKind.tool, outcome=CallOutcome.success, name="search"),
        _call_reliability(
            kind=CallKind.tool, outcome=CallOutcome.error, name="fetch", error_type="timeout"
        ),
    )

    result = reliability(_calls(calls))[TOOL_ERROR_KEY]

    assert result.detail["by_tool"] == {"fetch": 1, "search": 1}
    assert result.detail["by_status"] == {"error": 2, "success": 1}
    assert result.detail["by_error_type"] == {"rate_limited": 1, "timeout": 1}


def test_no_tool_calls_makes_tool_error_rate_skipped_not_scored_zero() -> None:
    calls = (_call_reliability(kind=CallKind.model, outcome=CallOutcome.success),)

    result = reliability(_calls(calls))[TOOL_ERROR_KEY]

    assert result.status is MetricStatus.skipped
    assert result.value is None


# --- absent outcome --------------------------------------------------------------------


def test_an_absent_outcome_on_any_call_makes_reliability_unavailable() -> None:
    """An absent outcome is never assumed successful, so it cannot be silently excluded."""
    calls = (
        _call_reliability(outcome=CallOutcome.success),
        _call_reliability(outcome=None),
    )

    results = reliability(_calls(calls))

    for key in (COMPLETION_KEY, TOOL_ERROR_KEY):
        assert results[key].status is MetricStatus.unavailable
        assert results[key].value is None
        assert results[key].error is None


# ── total tokens under both provider conventions ──

SEPARATE = TokenUsage(
    input_tokens=1000,
    output_tokens=300,
    cache_read_input_tokens=200,
    cache_write_input_tokens=50,
    reasoning_output_tokens=120,
    convention=TokenConvention.separate,
)

# Same underlying execution: the cache buckets are already inside ``input_tokens``.
INCLUSIVE = TokenUsage(
    input_tokens=1250,
    output_tokens=300,
    cache_read_input_tokens=200,
    cache_write_input_tokens=50,
    reasoning_output_tokens=120,
    convention=TokenConvention.inclusive,
)


def test_separate_convention_totals_every_billable_bucket_and_reports_reasoning_apart() -> None:
    result = token_usage_total(_usage(SEPARATE))

    assert result.status is MetricStatus.scored
    # Reasoning is reported for diagnosis, never added to a total that would double-count output.
    assert result.value == 1550.0
    assert result.detail == {
        "uncached_input_tokens": 1000,
        "output_tokens": 300,
        "cache_read_input_tokens": 200,
        "cache_write_input_tokens": 50,
        "reasoning_output_tokens": 120,
        "convention": "separate",
    }


def test_both_conventions_yield_identical_normalized_totals() -> None:
    separate = token_usage_total(_usage(SEPARATE))
    inclusive = token_usage_total(_usage(INCLUSIVE))

    buckets = (
        "uncached_input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_write_input_tokens",
        "reasoning_output_tokens",
    )

    assert separate.value == inclusive.value
    # The declared convention is retained for traceability, so only the normalized
    # buckets are required to match.
    assert {k: separate.detail[k] for k in buckets} == {
        k: inclusive.detail[k] for k in buckets
    }
    assert separate.detail["convention"] == "separate"
    assert inclusive.detail["convention"] == "inclusive"


# ── raw-mapping validation boundary ──
# Callers hold telemetry as plain mappings (JSON, DB rows), so the package accepts that
# shape without a try/except around every metric call: structural malformation becomes a
# structured error, never an exception.

def _raw() -> dict[str, object]:
    return {
        "wall_clock_ms": 1200.0,
        "ttft_ms": 300.0,
        "calls": [
            {
                "kind": "model",
                "scope": "agent",
                "duration_ms": 800.0,
                "outcome": "success",
            },
            {
                "kind": "tool",
                "scope": "agent",
                "name": "search",
                "duration_ms": 200.0,
                "outcome": "error",
                "error_type": "HTTPError",
            },
        ],
        "agent_token_usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "convention": "separate",
        },
        "retry_count": 1,
    }


def test_valid_mapping_yields_telemetry() -> None:
    validation = validate_telemetry(_raw())
    assert isinstance(validation, TelemetryValidation)
    assert validation.error is None
    assert validation.telemetry is not None
    assert validation.telemetry.wall_clock_ms == 1200.0
    assert validation.telemetry.calls is not None
    assert len(validation.telemetry.calls) == 2


def test_negative_duration_does_not_raise() -> None:
    validation = validate_telemetry({"wall_clock_ms": -1.0})
    assert validation.telemetry is None
    assert validation.error is not None
    assert validation.error.code == "malformed_telemetry"
    assert validation.error.locations


def _call_payload(**fields: object) -> list[dict[str, object]]:
    return [{"kind": "model", "scope": "agent", **fields}]


def _usage_payload(**fields: object) -> dict[str, object]:
    return {"convention": "separate", **fields}


_MALFORMED: list[tuple[str, object, tuple[str, ...]]] = [
    ("negative_call_duration", {"calls": _call_payload(duration_ms=-1.0)}, ("calls.0.duration_ms",)),
    ("non_finite_call_duration", {"calls": _call_payload(duration_ms=float("inf"))}, ("calls.0.duration_ms",)),
    ("unrecognized_outcome", {"calls": _call_payload(outcome="exploded")}, ("calls.0.outcome",)),
    ("negative_token_count", {"agent_token_usage": _usage_payload(input_tokens=-1)}, ("agent_token_usage.input_tokens",)),
    ("boolean_token_count", {"agent_token_usage": _usage_payload(input_tokens=True)}, ("agent_token_usage.input_tokens",)),
    ("non_integer_token_count", {"agent_token_usage": _usage_payload(input_tokens=1.5)}, ("agent_token_usage.input_tokens",)),
    (
        "inclusive_cache_exceeding_input",
        {
            "agent_token_usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 8,
                "cache_write_input_tokens": 5,
                "convention": "inclusive",
            }
        },
        ("agent_token_usage",),
    ),
    ("non_finite_wall_clock", {"wall_clock_ms": float("nan")}, ("wall_clock_ms",)),
    ("negative_ttft", {"ttft_ms": -1.0}, ("ttft_ms",)),
    ("negative_retry_count", {"retry_count": -1}, ("retry_count",)),
    ("boolean_retry_count", {"retry_count": True}, ("retry_count",)),
    ("unknown_field", {"wall_clock_seconds": 1.2}, ("wall_clock_seconds",)),
    ("non_mapping", ["not", "a", "mapping"], ("<root>",)),
    # Several errors are listed in a stable, sorted order, not the fields' order.
    ("two_errors", {"wall_clock_ms": -1.0, "retry_count": -1}, ("retry_count", "wall_clock_ms")),
]


@pytest.mark.parametrize(
    ("raw", "locations"),
    [(raw, locations) for _id, raw, locations in _MALFORMED],
    ids=[case_id for case_id, _raw, _locations in _MALFORMED],
)
def test_malformed_telemetry_is_a_structured_error_naming_each_field(
    raw: object, locations: tuple[str, ...]
) -> None:
    """Each case breaks fields of an otherwise valid payload, so only those fields are named."""
    payload = {**_raw(), **raw} if isinstance(raw, dict) else raw

    validation = validate_telemetry(payload)

    assert validation.telemetry is None
    assert validation.error is not None
    assert validation.error.code == "malformed_telemetry"
    assert tuple(validation.error.locations) == locations


def test_uncaptured_calls_survive_validation_as_none() -> None:
    raw = _raw()
    del raw["calls"]
    validation = validate_telemetry(raw)
    assert validation.telemetry is not None
    assert validation.telemetry.calls is None


def test_observed_empty_calls_survive_validation_as_empty() -> None:
    raw = _raw()
    raw["calls"] = []
    validation = validate_telemetry(raw)
    assert validation.telemetry is not None
    assert validation.telemetry.calls == ()


def test_invalid_results_carry_the_validation_error() -> None:
    """Metrics derived from a failed validation yield `invalid`, carrying that error."""
    from app.evaluation_metrics.performance.metrics.latency import wall_clock_latency

    validation = validate_telemetry({"wall_clock_ms": -1.0})
    assert validation.error is not None
    results = validation.invalid_results()
    assert results
    for result in results.values():
        assert result.status is MetricStatus.invalid
        assert result.value is None
        assert result.error == validation.error
    assert "performance.latency.wall_clock" in results
    # And the metric itself still works on validated telemetry.
    good = validate_telemetry({"wall_clock_ms": 5.0})
    assert good.telemetry is not None
    assert wall_clock_latency(good.telemetry).status is MetricStatus.scored


# ── telemetry input contract ──

def _call_defaults(**overrides: object) -> CallRecord:
    fields: dict[str, object] = {
        "kind": CallKind.model,
        "scope": CallScope.agent,
        "duration_ms": 120.0,
        "outcome": CallOutcome.success,
    }
    fields.update(overrides)
    return CallRecord(**fields)  # type: ignore[arg-type]


def test_component_sum_may_exceed_wall_clock() -> None:
    """Overlapping calls are a diagnostic, not an error - construction must succeed."""
    telemetry = ExecutionTelemetry(
        wall_clock_ms=100.0,
        calls=(_call_defaults(duration_ms=80.0), _call_defaults(duration_ms=80.0)),
    )
    assert telemetry.wall_clock_ms == 100.0
