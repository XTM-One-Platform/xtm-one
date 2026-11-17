import json
import uuid

import pytest

from app.services.builtin_tools import build_fimi_math_tool
from app.tools.base import ToolCall


async def _run_metric(operation, **kwargs):
    _, executor = build_fimi_math_tool()
    args = {"operation": operation, **kwargs}
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="compute_fimi_metrics",
            arguments=args,
        )
    )
    payload = json.loads(result.content)
    # ``is_error`` must agree with the payload's own error marker: a
    # canonical ``{"error": true, …}`` body is a FAILED call, and the flag is
    # what the trace span, the ``tool_done`` event and the loop's per-tool
    # error counters read.
    assert result.is_error is (payload.get("error") is True), result.content
    return payload


async def _run_stat(test, **kwargs):
    _, executor = build_fimi_math_tool()
    args = {"test": test, **kwargs}
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="run_statistical_test",
            arguments=args,
        )
    )
    payload = json.loads(result.content)
    # ``is_error`` must agree with the payload's own error marker: a
    # canonical ``{"error": true, …}`` body is a FAILED call, and the flag is
    # what the trace span, the ``tool_done`` event and the loop's per-tool
    # error counters read.
    assert result.is_error is (payload.get("error") is True), result.content
    return payload


# ── Metric operations ───────────────────────────────────────────────────


class TestOverlap:
    @pytest.mark.asyncio
    async def test_identical_sets(self):
        payload = await _run_metric("overlap", set_a=[1, 2, 3], set_b=[1, 2, 3])
        assert payload["score"] == 1.0

    @pytest.mark.asyncio
    async def test_disjoint_sets(self):
        payload = await _run_metric("overlap", set_a=[1, 2], set_b=[3, 4])
        assert payload["score"] == 0.0

    @pytest.mark.asyncio
    async def test_partial_overlap(self):
        payload = await _run_metric("overlap", set_a=[1, 2, 3], set_b=[2, 3, 4])
        # Intersection = {2,3}, Union = {1,2,3,4} → 2/4 = 0.5
        assert payload["score"] == 0.5

    @pytest.mark.asyncio
    async def test_empty_sets(self):
        payload = await _run_metric("overlap", set_a=[], set_b=[])
        assert payload["score"] == 0.0


class TestCoverage:
    @pytest.mark.asyncio
    async def test_full_coverage(self):
        payload = await _run_metric("coverage", set_a=[1, 2, 3], set_b=[1, 2, 3])
        assert payload["score"] == 1.0

    @pytest.mark.asyncio
    async def test_partial_coverage(self):
        payload = await _run_metric("coverage", set_a=[1], set_b=[1, 2])
        assert payload["score"] == 0.5


class TestGrowthRate:
    @pytest.mark.asyncio
    async def test_doubling(self):
        payload = await _run_metric("growth_rate", values=[100, 200])
        assert payload["score"] == 1.0  # 100% growth

    @pytest.mark.asyncio
    async def test_no_growth(self):
        payload = await _run_metric("growth_rate", values=[100, 100])
        assert payload["score"] == 0.0

    @pytest.mark.asyncio
    async def test_single_value(self):
        payload = await _run_metric("growth_rate", values=[100])
        assert "error" in payload

    @pytest.mark.asyncio
    async def test_first_value_zero_is_error(self):
        # first == 0 -> compound growth is undefined; surface an explicit error
        # (canonical error: True + message), not a silent score=None.
        payload = await _run_metric("growth_rate", values=[0, 50])
        assert payload.get("error") is True
        assert "message" in payload

    @pytest.mark.asyncio
    async def test_negative_ratio_is_error(self):
        # A sign change (last/first < 0) cannot yield a real CAGR.
        payload = await _run_metric("growth_rate", values=[100, -50])
        assert payload.get("error") is True


class TestVolatility:
    @pytest.mark.asyncio
    async def test_constant_values(self):
        payload = await _run_metric("volatility", values=[10, 10, 10, 10])
        assert payload["score"] == 0.0

    @pytest.mark.asyncio
    async def test_variable_values(self):
        payload = await _run_metric("volatility", values=[10, 20, 10, 20])
        assert payload["score"] > 0

    @pytest.mark.asyncio
    async def test_single_value(self):
        payload = await _run_metric("volatility", values=[5])
        assert "error" in payload

    @pytest.mark.asyncio
    async def test_zero_mean_series_is_error(self):
        # All-zero series -> mean 0 -> CV undefined. Must return an explicit
        # error rather than float('inf'), which would crash json.dumps.
        payload = await _run_metric("volatility", values=[0, 0, 0])
        assert payload.get("error") is True


class TestConsistency:
    @pytest.mark.asyncio
    async def test_constant_values(self):
        payload = await _run_metric("consistency", values=[10, 10, 10])
        assert payload["score"] == 1.0

    @pytest.mark.asyncio
    async def test_highly_variable(self):
        payload = await _run_metric("consistency", values=[1, 100, 1, 100])
        assert payload["score"] < 0.5


class TestWeightedScore:
    @pytest.mark.asyncio
    async def test_equal_weights(self):
        payload = await _run_metric(
            "weighted_score", values=[10, 20], weights=[1, 1], labels=["a", "b"],
        )
        assert payload["score"] == 15.0

    @pytest.mark.asyncio
    async def test_unequal_weights(self):
        payload = await _run_metric(
            "weighted_score", values=[10, 20], weights=[3, 1],
        )
        # (10*3 + 20*1) / 4 = 12.5
        assert payload["score"] == 12.5

    @pytest.mark.asyncio
    async def test_mismatched_lengths(self):
        payload = await _run_metric(
            "weighted_score", values=[1, 2], weights=[1],
        )
        assert "error" in payload


class TestUnknownOperation:
    @pytest.mark.asyncio
    async def test_unknown_operation(self):
        payload = await _run_metric("nonexistent")
        assert payload.get("error") is True

    @pytest.mark.asyncio
    async def test_no_operation(self):
        _, executor = build_fimi_math_tool()
        result = await executor.execute_tool(
            ToolCall(
                id=str(uuid.uuid4()),
                name="compute_fimi_metrics",
                arguments={},
            )
        )
        payload = json.loads(result.content)
        assert payload.get("error") is True


# ── Statistical tests ────────────────────────────────────────────────────


class TestChiSquared:
    @pytest.mark.asyncio
    async def test_uniform_distribution(self):
        payload = await _run_stat(
            "chi_squared",
            observed=[25, 25, 25, 25],
            expected=[25, 25, 25, 25],
        )
        assert payload["statistic"] == 0.0
        assert payload["p_value"] == 1.0

    @pytest.mark.asyncio
    async def test_skewed_distribution(self):
        payload = await _run_stat(
            "chi_squared",
            observed=[90, 5, 3, 2],
            expected=[25, 25, 25, 25],
        )
        assert payload["p_value"] < 0.05

    @pytest.mark.asyncio
    async def test_mismatched_lengths(self):
        payload = await _run_stat(
            "chi_squared", observed=[10, 20], expected=[10],
        )
        assert "error" in payload

    @pytest.mark.asyncio
    async def test_effect_size_is_cohens_w(self):
        # Goodness-of-fit effect size must be Cohen's w = sqrt(chi2 / n),
        # not the previous (incorrect) Cramer's V approximation.
        payload = await _run_stat(
            "chi_squared", observed=[90, 5, 3, 2], expected=[25, 25, 25, 25],
        )
        assert "Cohen's w" in payload["effect_size_measure"]
        n = sum([90, 5, 3, 2])
        expected_w = round((payload["statistic"] / n) ** 0.5, 6)
        assert abs(payload["effect_size"] - expected_w) < 1e-6


class TestProportionZ:
    @pytest.mark.asyncio
    async def test_equal_proportions(self):
        payload = await _run_stat(
            "proportion_z",
            successes_a=50, n_a=100,
            successes_b=50, n_b=100,
        )
        assert payload["statistic"] == 0.0
        assert payload["p_value"] == 1.0

    @pytest.mark.asyncio
    async def test_different_proportions(self):
        payload = await _run_stat(
            "proportion_z",
            successes_a=80, n_a=100,
            successes_b=20, n_b=100,
        )
        assert payload["p_value"] < 0.05
        assert "confidence_interval" in payload

    @pytest.mark.asyncio
    async def test_successes_exceed_sample_size_is_error(self):
        # successes > n is invalid input: it must be rejected, not crash
        # Cohen's h (math.asin(sqrt(p)) with p > 1 raises a domain error).
        payload = await _run_stat(
            "proportion_z",
            successes_a=150, n_a=100,
            successes_b=50, n_b=100,
        )
        assert payload.get("error") is True

    @pytest.mark.asyncio
    async def test_boundary_proportion_has_nonzero_ci(self):
        # A single boundary proportion (here p_a = 1.0) must not silently
        # collapse the whole confidence interval to zero width.
        payload = await _run_stat(
            "proportion_z",
            successes_a=100, n_a=100,
            successes_b=50, n_b=100,
        )
        ci = payload["confidence_interval"]
        assert ci["upper"] > ci["lower"]


class TestMannWhitney:
    @pytest.mark.asyncio
    async def test_identical_samples(self):
        payload = await _run_stat(
            "mann_whitney",
            sample_a=[1, 2, 3, 4, 5],
            sample_b=[1, 2, 3, 4, 5],
        )
        assert payload["p_value"] > 0.05

    @pytest.mark.asyncio
    async def test_different_samples(self):
        payload = await _run_stat(
            "mann_whitney",
            sample_a=[1, 2, 3, 4, 5],
            sample_b=[100, 200, 300, 400, 500],
        )
        assert payload["p_value"] < 0.05

    @pytest.mark.asyncio
    async def test_insufficient_data(self):
        payload = await _run_stat(
            "mann_whitney", sample_a=[1], sample_b=[2],
        )
        assert "error" in payload


class TestKSTest:
    @pytest.mark.asyncio
    async def test_same_distribution(self):
        payload = await _run_stat(
            "ks_test",
            sample_a=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
            sample_b=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
        )
        assert payload["statistic"] == 0.0
        assert payload["p_value"] == 1.0

    @pytest.mark.asyncio
    async def test_different_distributions(self):
        payload = await _run_stat(
            "ks_test",
            sample_a=[1, 1, 1, 1, 1],
            sample_b=[100, 100, 100, 100, 100],
        )
        assert payload["p_value"] < 0.05


class TestUnknownTest:
    @pytest.mark.asyncio
    async def test_unknown_test(self):
        payload = await _run_stat("nonexistent")
        assert payload.get("error") is True
