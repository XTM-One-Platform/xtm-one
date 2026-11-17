"""ROI storage helpers that need no database (issue #8990, slice 2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import roi
from app.services.roi_records import (
    apply_overrides,
    run_duration_s,
    structured_output_of,
    validate_override,
)


def test_structured_output_is_parsed_json_and_none_otherwise():
    assert structured_output_of('{"cves": [1, 2]}') == {"cves": [1, 2]}
    assert structured_output_of("Here are 12 CVEs") is None
    assert structured_output_of(None) is None
    assert structured_output_of("") is None


def test_duration_is_zero_when_a_bound_is_unknown_and_never_negative():
    start = datetime(2026, 11, 5, 20, 0, tzinfo=UTC)
    assert run_duration_s(start, start + timedelta(seconds=90)) == 90.0
    assert run_duration_s(None, start) == 0.0
    assert run_duration_s(start, None) == 0.0
    assert run_duration_s(start, start - timedelta(seconds=5)) == 0.0


def test_an_override_replaces_the_minutes_and_is_labelled():
    o = SimpleNamespace(
        unit="cve_assessed",
        manual_min_low=5,
        manual_min_likely=12,
        manual_min_high=30,
        review_min=2,
        source="time-and-motion study Q3",
    )
    merged, overridden = apply_overrides([o])
    b = merged["cve_assessed"]
    assert (b.manual_min_low, b.manual_min_likely, b.manual_min_high, b.review_min) == (5, 12, 30, 2)
    assert b.minutes_label == roi.LABEL_OVERRIDE
    assert b.minutes_source == "time-and-motion study Q3"
    assert overridden == {"cve_assessed"}
    # The default catalog itself is untouched.
    assert roi.BASELINES_BY_UNIT["cve_assessed"].manual_min_likely == 20


def test_an_override_of_an_unknown_unit_is_ignored():
    o = SimpleNamespace(
        unit="not_a_unit", manual_min_low=1, manual_min_likely=2, manual_min_high=3, review_min=0, source=""
    )
    merged, overridden = apply_overrides([o])
    assert "not_a_unit" not in merged
    assert overridden == set()


def test_overridden_figures_say_so_in_their_assumptions():
    o = SimpleNamespace(
        unit="cve_assessed", manual_min_low=5, manual_min_likely=12, manual_min_high=30, review_min=2, source=""
    )
    merged, overridden = apply_overrides([o])
    figures = roi.compute_roi(
        [roi.RunMeasure(unit="cve_assessed", units=10)],
        baselines=merged,
        overridden_units=overridden,
    )
    assert figures.minutes_saved == pytest.approx(10 * 12 - 10 * 2)


@pytest.mark.parametrize(
    ("unit", "low", "likely", "high", "review", "message"),
    [
        ("nope", 1, 2, 3, 0, "not a built-in unit of the ROI catalog"),
        ("cve_assessed", -1, 2, 3, 0, "negative"),
        ("cve_assessed", 5, 2, 3, 0, "ordered"),
        ("cve_assessed", 1, 2, 3, 2, "under the likely"),
    ],
    ids=["unknown-unit", "negative", "unordered", "review-too-long"],
)
def test_a_bad_override_is_refused_and_named(unit, low, likely, high, review, message):
    with pytest.raises(ValueError, match=message):
        validate_override(unit, low, likely, high, review)


def test_a_good_override_passes():
    validate_override("cve_assessed", 3, 15, 40, 2)


# ── Custom units (#9252) ───────────────────────────────────────────────────────


def _custom(**kw):
    fields = {
        "key": "contract_reviewed",
        "name": "Contract reviewed",
        "role": "Legal counsel",
        "manual_min_low": 30,
        "manual_min_likely": 60,
        "manual_min_high": 120,
        "review_min": 10,
        "rate_role": "analyst",
        "source": "internal time study",
    }
    fields.update(kw)
    return SimpleNamespace(**fields)


def test_a_custom_unit_is_a_labelled_baseline():
    from app.services.roi_records import LABEL_CUSTOM_UNIT, custom_baseline

    b = custom_baseline(_custom())
    assert (b.unit, b.name, b.role, b.rate_role) == (
        "contract_reviewed",
        "Contract reviewed",
        "Legal counsel",
        "analyst",
    )
    assert (b.manual_min_likely, b.review_min, b.gained_min_likely) == (60, 10, 50)
    assert b.minutes_label == LABEL_CUSTOM_UNIT and b.minutes_source == "internal time study"


def test_a_custom_unit_counts_in_the_figures_and_the_declaration():
    from app.services.roi_records import custom_baseline

    catalog = {**roi.BASELINES_BY_UNIT, "contract_reviewed": custom_baseline(_custom())}
    figures = roi.compute_roi(
        [roi.RunMeasure(unit="contract_reviewed", units=2, llm_cost=0.5, duration_s=30)],
        currency="USD",
        baselines=catalog,
    )
    assert figures.minutes_saved == pytest.approx(2 * (60 - 10))
    schema = {"type": "object", "x-xtm-roi": {"unit": "contract_reviewed", "count": "contracts"}}
    assert roi.declaration_error(schema) is not None  # closed catalog by default
    assert roi.declaration_error(schema, known_units=catalog) is None


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"name": " "}, "needs a name"),
        ({"low": -1}, "negative"),
        ({"low": 70}, "ordered"),
        ({"review": 60}, "under the likely"),
        ({"rate_role": "lawyer"}, "rate role"),
    ],
    ids=["no-name", "negative", "unordered", "review-too-long", "unknown-rate-role"],
)
def test_a_bad_custom_unit_is_refused_and_named(kw, message):
    from app.services.roi_records import validate_unit

    fields = {"name": "Contract reviewed", "low": 30, "likely": 60, "high": 120, "review": 10, "rate_role": "analyst"}
    fields.update(kw)
    with pytest.raises(ValueError, match=message):
        validate_unit(**fields)


def test_a_custom_unit_may_name_a_role_the_organization_rated():
    """#9321: a rate role is a built-in one or one the organization gave a rate."""
    from app.services.roi_records import validate_rate, validate_unit

    fields = {"name": "Contract reviewed", "low": 30, "likely": 60, "high": 120, "review": 10, "rate_role": "lawyer"}
    validate_unit(**fields, rate_roles={"lawyer"})
    for role, currency, amount, message in (
        ("L", "USD", 10, "rate role"),
        ("legal", "usd", 10, "currency"),
        ("legal", "USD", 0, "above zero"),
    ):
        with pytest.raises(ValueError, match=message):
            validate_rate(role, currency, amount)
