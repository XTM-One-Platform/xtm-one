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

"""Mathematical and statistical toolkit for FIMI analytics.

Deterministic calculations for overlap, coverage, growth, volatility,
consistency, weighted scoring, and statistical hypothesis tests.
"""

from __future__ import annotations

import json
import math

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._shared import normalize_tool_error

_FIMI_SOURCE = "builtin:cti"


# ---------------------------------------------------------------------------
# Metric operations
# ---------------------------------------------------------------------------

def _overlap(set_a: list, set_b: list) -> dict:
    """Jaccard overlap between two sets."""
    a, b = set(set_a), set(set_b)
    intersection = a & b
    union = a | b
    score = len(intersection) / len(union) if union else 0.0
    return {
        "formula": "|A ∩ B| / |A ∪ B|",
        "intermediate_values": {
            "set_a_size": len(a),
            "set_b_size": len(b),
            "intersection_size": len(intersection),
            "union_size": len(union),
            "intersection_items": sorted(str(x) for x in intersection)[:20],
        },
        "score": round(score, 6),
        "interpretation": (
            f"Jaccard overlap = {score:.4f}. "
            + ("Identical sets." if score == 1.0
               else "No overlap." if score == 0.0
               else f"{score * 100:.1f}% overlap between the two sets.")
        ),
    }


def _coverage(subset: list, superset: list) -> dict:
    """Coverage: what fraction of the superset is covered by the subset."""
    sub, sup = set(subset), set(superset)
    covered = sub & sup
    score = len(covered) / len(sup) if sup else 0.0
    return {
        "formula": "|subset ∩ superset| / |superset|",
        "intermediate_values": {
            "subset_size": len(sub),
            "superset_size": len(sup),
            "covered_size": len(covered),
        },
        "score": round(score, 6),
        "interpretation": (
            f"Coverage = {score * 100:.1f}%. "
            f"{len(covered)} of {len(sup)} items in the superset are covered."
        ),
    }


def _growth_rate(values: list[float]) -> dict:
    """Compound growth rate over a time series."""
    _formula = "(last / first) ^ (1 / n) - 1, where n = number of periods"
    if len(values) < 2:
        return {
            "formula": _formula,
            "error": "Need at least 2 data points.",
        }
    first, last = values[0], values[-1]
    n = len(values) - 1
    if first == 0:
        return {
            "formula": _formula,
            "intermediate_values": {"first": first, "last": last, "periods": n},
            "error": "Cannot compute growth rate: first value is zero.",
        }
    ratio = last / first
    if ratio < 0:
        return {
            "formula": _formula,
            "intermediate_values": {"first": first, "last": last, "periods": n},
            "error": "Cannot compute growth rate: negative ratio (sign change in series).",
        }
    cagr = ratio ** (1 / n) - 1
    return {
        "formula": _formula,
        "intermediate_values": {"first": first, "last": last, "periods": n, "ratio": round(ratio, 6)},
        "score": round(cagr, 6),
        "interpretation": (
            f"Compound growth rate = {cagr * 100:.2f}% per period over {n} periods."
        ),
    }


def _coefficient_of_variation(values: list[float]) -> dict:
    """Shared coefficient-of-variation computation for volatility/consistency.

    Returns either ``{"error": ...}`` (fewer than 2 points, or a zero mean
    where the CV is mathematically undefined) or ``{"cv", "mean", "std_dev",
    "n"}``. Centralising this keeps ``_volatility`` and ``_consistency`` in
    lockstep and avoids the ``float('inf')`` JSON-serialization hazard when the
    mean is zero.
    """
    if len(values) < 2:
        return {"error": "Need at least 2 data points."}
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / (len(values) - 1)
    std = math.sqrt(variance)
    if mean == 0:
        return {
            "error": "Coefficient of variation is undefined when the mean is zero.",
            "n": len(values),
            "mean": 0.0,
            "std_dev": round(std, 6),
        }
    return {"cv": std / abs(mean), "mean": mean, "std_dev": std, "n": len(values)}


def _volatility(values: list[float]) -> dict:
    """Coefficient of variation (std / mean)."""
    stats = _coefficient_of_variation(values)
    if "error" in stats:
        return {"formula": "σ / μ (coefficient of variation, sample std dev)", **stats}
    cv = stats["cv"]
    return {
        "formula": "σ / μ (coefficient of variation, sample std dev)",
        "intermediate_values": {
            "n": stats["n"],
            "mean": round(stats["mean"], 6),
            "std_dev": round(stats["std_dev"], 6),
        },
        "score": round(cv, 6),
        "interpretation": (
            f"Volatility (CV) = {cv:.4f}. "
            + ("Low volatility (CV < 0.15)." if cv < 0.15
               else "Moderate volatility (0.15 ≤ CV < 0.5)." if cv < 0.5
               else "High volatility (CV ≥ 0.5).")
        ),
    }


def _consistency(values: list[float]) -> dict:
    """Consistency score: 1 - CV, clamped to [0, 1]."""
    stats = _coefficient_of_variation(values)
    if "error" in stats:
        return {"formula": "max(0, 1 - CV)", **stats}
    cv = stats["cv"]
    score = max(0.0, min(1.0, 1.0 - cv))
    return {
        "formula": "max(0, 1 - CV)",
        "intermediate_values": {
            "n": stats["n"],
            "mean": round(stats["mean"], 6),
            "cv": round(cv, 6),
        },
        "score": round(score, 6),
        "interpretation": (
            f"Consistency = {score:.4f}. "
            + ("Very consistent (> 0.85)." if score > 0.85
               else "Moderate consistency." if score > 0.5
               else "Low consistency (< 0.5).")
        ),
    }


def _weighted_score(
    values: list[float],
    weights: list[float],
    labels: list[str] | None = None,
) -> dict:
    """Weighted average score."""
    if len(values) != len(weights):
        return {
            "formula": "Σ(wᵢ × vᵢ) / Σ(wᵢ)",
            "error": f"values ({len(values)}) and weights ({len(weights)}) must have the same length.",
        }
    if not values:
        return {"formula": "Σ(wᵢ × vᵢ) / Σ(wᵢ)", "error": "Empty input."}

    total_weight = sum(weights)
    if total_weight == 0:
        return {
            "formula": "Σ(wᵢ × vᵢ) / Σ(wᵢ)",
            "error": "Total weight is zero.",
        }

    weighted_sum = sum(v * w for v, w in zip(values, weights, strict=False))
    score = weighted_sum / total_weight

    components: list[dict] = []
    for i, (v, w) in enumerate(zip(values, weights, strict=False)):
        lbl = (labels[i] if labels and i < len(labels) else f"item_{i}")
        components.append({
            "label": lbl,
            "value": v,
            "weight": w,
            "contribution": round(v * w / total_weight, 6),
        })

    return {
        "formula": "Σ(wᵢ × vᵢ) / Σ(wᵢ)",
        "intermediate_values": {
            "total_weight": total_weight,
            "weighted_sum": round(weighted_sum, 6),
            "components": components,
        },
        "score": round(score, 6),
        "interpretation": f"Weighted score = {score:.4f}.",
    }


_OPERATIONS = {
    "overlap": "Jaccard overlap between two sets",
    "coverage": "Fraction of superset covered by subset",
    "growth_rate": "Compound growth rate over a time series",
    "volatility": "Coefficient of variation (std/mean)",
    "consistency": "Consistency score (1 - CV, clamped to [0,1])",
    "weighted_score": "Weighted average with explicit weights",
}


# ---------------------------------------------------------------------------
# Statistical tests
# ---------------------------------------------------------------------------

def _chi_squared(observed: list[float], expected: list[float]) -> dict:
    """Chi-squared goodness-of-fit test."""
    from scipy import stats as sp_stats

    if len(observed) != len(expected):
        return {"error": "observed and expected must have the same length."}
    if not observed:
        return {"error": "Empty input."}

    stat, p_value = sp_stats.chisquare(observed, f_exp=expected)
    k = len(observed)
    n = sum(observed)
    # Cohen's w is the correct effect size for a goodness-of-fit test:
    # w = sqrt(χ² / n). (Cramér's V is defined for r×c contingency tables via
    # min(r-1, c-1) in the denominator and is undefined for a 1×k GoF table.)
    effect_size = math.sqrt(float(stat) / n) if n > 0 else 0.0

    return {
        "test": "chi_squared",
        "formula": "χ² = Σ((Oᵢ - Eᵢ)² / Eᵢ)",
        "intermediate_values": {"df": k - 1, "n": n},
        "statistic": round(float(stat), 6),
        "p_value": round(float(p_value), 6),
        "effect_size": round(effect_size, 6),
        "effect_size_measure": "Cohen's w (√(χ²/n))",
        "interpretation": (
            f"χ²({k - 1}) = {stat:.4f}, p = {p_value:.4f}. "
            + ("Statistically significant (p < 0.05)." if p_value < 0.05
               else "Not statistically significant (p ≥ 0.05).")
        ),
    }


def _proportion_z(
    successes_a: int, n_a: int, successes_b: int, n_b: int,
) -> dict:
    """Two-proportion z-test."""
    if n_a <= 0 or n_b <= 0:
        return {"error": "Sample sizes must be positive."}
    # Guard invalid counts: a proportion > 1 breaks math.asin(sqrt(p)) below
    # (Cohen's h) with an unhandled domain error, and silently misreports the test.
    if not (0 <= successes_a <= n_a) or not (0 <= successes_b <= n_b):
        return {"error": "successes must be between 0 and the sample size."}

    p_a = successes_a / n_a
    p_b = successes_b / n_b
    p_pool = (successes_a + successes_b) / (n_a + n_b)

    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b)) if 0 < p_pool < 1 else 0
    z = (p_a - p_b) / se if se > 0 else 0.0
    p_value = 2 * (1 - _normal_cdf(abs(z)))

    diff = p_a - p_b
    # Wald CI for the difference, computed term-wise: a single boundary
    # proportion (0 or 1) only zeroes its own variance term instead of silently
    # collapsing the whole interval to zero width.
    margin = 1.96 * math.sqrt(p_a * (1 - p_a) / n_a + p_b * (1 - p_b) / n_b)

    # Cohen's h
    h = 2 * math.asin(math.sqrt(p_a)) - 2 * math.asin(math.sqrt(p_b))

    return {
        "test": "proportion_z",
        "formula": "z = (p̂₁ - p̂₂) / √(p̂(1-p̂)(1/n₁ + 1/n₂))",
        "intermediate_values": {
            "p_a": round(p_a, 6),
            "p_b": round(p_b, 6),
            "p_pooled": round(p_pool, 6),
            "standard_error": round(se, 6),
        },
        "statistic": round(z, 6),
        "p_value": round(p_value, 6),
        "effect_size": round(abs(h), 6),
        "effect_size_measure": "Cohen's h",
        "confidence_interval": {
            "level": 0.95,
            "lower": round(diff - margin, 6),
            "upper": round(diff + margin, 6),
        },
        "interpretation": (
            f"z = {z:.4f}, p = {p_value:.4f}. "
            f"Proportions: {p_a:.4f} vs {p_b:.4f}. "
            + ("Significant difference (p < 0.05)." if p_value < 0.05
               else "No significant difference (p ≥ 0.05).")
        ),
    }


def _mann_whitney(sample_a: list[float], sample_b: list[float]) -> dict:
    """Mann-Whitney U test for two independent samples."""
    from scipy import stats as sp_stats

    if len(sample_a) < 2 or len(sample_b) < 2:
        return {"error": "Each sample needs at least 2 observations."}

    stat, p_value = sp_stats.mannwhitneyu(
        sample_a, sample_b, alternative="two-sided",
    )
    n1, n2 = len(sample_a), len(sample_b)
    # Rank-biserial correlation as effect size
    r = 1 - (2 * float(stat)) / (n1 * n2)

    return {
        "test": "mann_whitney",
        "formula": "U = n₁n₂ + n₁(n₁+1)/2 - R₁",
        "intermediate_values": {"n1": n1, "n2": n2},
        "statistic": round(float(stat), 6),
        "p_value": round(float(p_value), 6),
        "effect_size": round(abs(r), 6),
        "effect_size_measure": "rank-biserial correlation |r|",
        "interpretation": (
            f"U = {stat:.1f}, p = {p_value:.4f}. "
            + ("Significant difference (p < 0.05)." if p_value < 0.05
               else "No significant difference (p ≥ 0.05).")
        ),
    }


def _ks_test(sample_a: list[float], sample_b: list[float]) -> dict:
    """Kolmogorov-Smirnov two-sample test."""
    from scipy import stats as sp_stats

    if len(sample_a) < 2 or len(sample_b) < 2:
        return {"error": "Each sample needs at least 2 observations."}

    stat, p_value = sp_stats.ks_2samp(sample_a, sample_b)

    return {
        "test": "ks_test",
        "formula": "D = sup|F₁(x) - F₂(x)|",
        "intermediate_values": {
            "n1": len(sample_a),
            "n2": len(sample_b),
        },
        "statistic": round(float(stat), 6),
        "p_value": round(float(p_value), 6),
        "effect_size": round(float(stat), 6),
        "effect_size_measure": "D statistic (max CDF difference)",
        "interpretation": (
            f"D = {stat:.4f}, p = {p_value:.4f}. "
            + ("Distributions differ significantly (p < 0.05)." if p_value < 0.05
               else "No significant difference in distributions (p ≥ 0.05).")
        ),
    }


_STATISTICAL_TESTS = {
    "chi_squared": "Chi-squared goodness-of-fit test",
    "proportion_z": "Two-proportion z-test",
    "mann_whitney": "Mann-Whitney U test for independent samples",
    "ks_test": "Kolmogorov-Smirnov two-sample distribution test",
}


def _normal_cdf(x: float) -> float:
    """Standard normal CDF using the complementary error function."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


# ---------------------------------------------------------------------------
# Tool builders
# ---------------------------------------------------------------------------


def build_fimi_math_tool() -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build FIMI math operations and statistical test tools."""

    async def _compute_fimi_metrics(
        operation: str = "",
        set_a: list | None = None,
        set_b: list | None = None,
        values: list[float] | None = None,
        weights: list[float] | None = None,
        labels: list[str] | None = None,
    ) -> str:
        if not operation:
            return json.dumps({
                "error": True,
                "message": "Provide an 'operation' parameter.",
                "available_operations": _OPERATIONS,
            })

        if operation not in _OPERATIONS:
            return json.dumps({
                "error": True,
                "message": f"Unknown operation '{operation}'.",
                "available_operations": _OPERATIONS,
            })

        if operation == "overlap":
            result = _overlap(set_a or [], set_b or [])
        elif operation == "coverage":
            result = _coverage(set_a or [], set_b or [])
        elif operation == "growth_rate":
            result = _growth_rate(values or [])
        elif operation == "volatility":
            result = _volatility(values or [])
        elif operation == "consistency":
            result = _consistency(values or [])
        elif operation == "weighted_score":
            result = _weighted_score(values or [], weights or [], labels)
        else:
            return json.dumps({"error": True, "message": f"Unimplemented: {operation}"})

        result = normalize_tool_error(result)
        result["operation"] = operation
        return json.dumps(result, ensure_ascii=False)

    async def _run_statistical_test(
        test: str = "",
        sample_a: list[float] | None = None,
        sample_b: list[float] | None = None,
        observed: list[float] | None = None,
        expected: list[float] | None = None,
        successes_a: int = 0,
        n_a: int = 0,
        successes_b: int = 0,
        n_b: int = 0,
    ) -> str:
        if not test:
            return json.dumps({
                "error": True,
                "message": "Provide a 'test' parameter.",
                "available_tests": _STATISTICAL_TESTS,
            })

        if test not in _STATISTICAL_TESTS:
            return json.dumps({
                "error": True,
                "message": f"Unknown test '{test}'.",
                "available_tests": _STATISTICAL_TESTS,
            })

        if test == "chi_squared":
            result = _chi_squared(observed or [], expected or [])
        elif test == "proportion_z":
            result = _proportion_z(successes_a, n_a, successes_b, n_b)
        elif test == "mann_whitney":
            result = _mann_whitney(sample_a or [], sample_b or [])
        elif test == "ks_test":
            result = _ks_test(sample_a or [], sample_b or [])
        else:
            return json.dumps({"error": True, "message": f"Unimplemented: {test}"})

        return json.dumps(normalize_tool_error(result), ensure_ascii=False)

    executor = CallableToolExecutor()
    executor.register("compute_fimi_metrics", _compute_fimi_metrics)
    executor.register("run_statistical_test", _run_statistical_test)

    metric_def = ToolDefinition(
        name="compute_fimi_metrics",
        description=(
            "Compute deterministic FIMI-oriented metrics. Operations: "
            "overlap (Jaccard), coverage, growth_rate (CAGR), volatility "
            "(CV), consistency (1-CV), weighted_score. Each result includes "
            "formula, intermediate values, score, and interpretation."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": list(_OPERATIONS.keys()),
                    "description": "Which metric to compute.",
                },
                "set_a": {
                    "type": "array",
                    "description": "First set (for overlap/coverage).",
                },
                "set_b": {
                    "type": "array",
                    "description": "Second set (for overlap/coverage).",
                },
                "values": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Numeric values (for growth_rate/volatility/consistency/weighted_score).",
                },
                "weights": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Weights (for weighted_score).",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Labels for each value (for weighted_score).",
                },
            },
            "required": ["operation"],
        },
        source=_FIMI_SOURCE,
    )

    stat_def = ToolDefinition(
        name="run_statistical_test",
        description=(
            "Run statistical hypothesis tests for FIMI analysis. Tests: "
            "chi_squared (goodness-of-fit), proportion_z (two-proportion), "
            "mann_whitney (independent samples), ks_test (distribution "
            "comparison). Returns statistic, p-value, effect size, and "
            "interpretation; proportion_z also returns a confidence interval."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "test": {
                    "type": "string",
                    "enum": list(_STATISTICAL_TESTS.keys()),
                    "description": "Which statistical test to run.",
                },
                "sample_a": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "First sample (for mann_whitney/ks_test).",
                },
                "sample_b": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Second sample (for mann_whitney/ks_test).",
                },
                "observed": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Observed frequencies (for chi_squared).",
                },
                "expected": {
                    "type": "array",
                    "items": {"type": "number"},
                    "description": "Expected frequencies (for chi_squared).",
                },
                "successes_a": {
                    "type": "integer",
                    "description": "Successes in group A (for proportion_z).",
                },
                "n_a": {
                    "type": "integer",
                    "description": "Sample size of group A (for proportion_z).",
                },
                "successes_b": {
                    "type": "integer",
                    "description": "Successes in group B (for proportion_z).",
                },
                "n_b": {
                    "type": "integer",
                    "description": "Sample size of group B (for proportion_z).",
                },
            },
            "required": ["test"],
        },
        source=_FIMI_SOURCE,
    )

    return [metric_def, stat_def], executor
