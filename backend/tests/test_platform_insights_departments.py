"""Department-grade LLM analytics without a database.

The projection math, the metric matrix and its refusals, the department
resolution chain, the directory department a sign-in carries and the complete
months a projection is fitted on. The figures themselves are read against
PostgreSQL in ``test_platform_insights_departments_postgres.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest


def test_a_linear_projection_reproduces_the_slope_of_a_linear_series():
    from app.services.platform_insights.projection import fit

    series = [1000 + 125.5 * month for month in range(8)]
    result = fit("linear", series, horizon=14)
    assert result.params["slope_per_month"] == pytest.approx(125.5, rel=0.01)
    assert result.r2 == pytest.approx(1.0)
    assert result.mape == pytest.approx(0.0, abs=1e-9)
    assert len(result.projected) == 14
    assert result.projected[0] == pytest.approx(1000 + 125.5 * 8, rel=0.01)


def test_cagr_and_last_n_average_and_their_limits():
    from app.services.platform_insights.projection import ProjectionError, add_months, fit

    growing = [100 * 1.1**month for month in range(6)]
    cagr = fit("cagr", growing, horizon=3)
    assert cagr.params["monthly_growth"] == pytest.approx(0.1, rel=0.01)
    assert cagr.projected[-1] == pytest.approx(100 * 1.1**8, rel=0.01)

    flat = fit("last_n_avg", [10, 20, 30, 60], horizon=2, last_n=3)
    assert flat.params["average"] == pytest.approx(110 / 3)
    assert flat.projected == [pytest.approx(110 / 3)] * 2
    assert flat.r2 is None

    falling = fit("linear", [300, 200, 100], horizon=4)
    assert falling.clamped and min(falling.projected) == 0.0

    with pytest.raises(ProjectionError, match="at least 2"):
        fit("linear", [5.0], horizon=1)
    with pytest.raises(ProjectionError, match="above zero"):
        fit("cagr", [0.0, 5.0], horizon=1)
    assert add_months(date(2026, 11, 1), 14) == date(2028, 1, 1)


def test_every_cell_of_the_matrix_is_computed_or_names_an_alternative():
    from app.services.platform_insights.analytics import DIMENSIONS, GROUP_BYS, METRICS, check_combination
    from app.services.platform_insights.scope import InsightRefusedError

    computed = 0
    for metric in METRICS:
        for group_by in (None, *GROUP_BYS):
            for split_by in (None, *DIMENSIONS):
                try:
                    check_combination(metric, group_by, split_by)
                    computed += 1
                except InsightRefusedError as refusal:
                    assert "use " in refusal.message.casefold(), (metric, group_by, split_by, refusal.message)
    # Every metric but latency: every group_by, alone or split by any other dimension.
    per_metric = 1 + len(GROUP_BYS) + len(GROUP_BYS) * len(DIMENSIONS) - len(DIMENSIONS)
    assert computed == (len(METRICS) - 1) * per_metric + 3


def test_the_tool_publishes_the_matrix_the_resolution_chain_and_the_attribution_rules():
    from app.services.builtin_tools import build_platform_insights_tools
    from app.services.platform_insights.analytics import DIMENSIONS, GROUP_BYS, METRICS, capabilities

    definitions, _executor = build_platform_insights_tools("")
    analytics = next(item for item in definitions if item.name == "query_platform_analytics")
    for metric in METRICS:
        assert metric in analytics.description, metric
    for phrase in ("MATRIX", "ATTRIBUTION", "DEPARTMENTS", "directory department", "first admin group rule", "complement"):
        assert phrase in analytics.description, phrase
    schema = analytics.input_schema["properties"]
    assert schema["metric"]["enum"] == list(METRICS)
    assert schema["group_by"]["enum"] == ["", *GROUP_BYS]
    assert schema["split_by"]["enum"] == ["", *DIMENSIONS]
    assert capabilities()["metrics"] == list(METRICS)


def test_the_department_chain_reads_its_sources_in_order():
    from app.services.departments import DepartmentMap, DepartmentSettings

    engineering, finance = uuid.uuid4(), uuid.uuid4()
    finance_team, platform_team = uuid.uuid4(), uuid.uuid4()
    mapping = DepartmentMap(
        settings=DepartmentSettings(),
        names={engineering: "Engineering", finance: "Finance"},
        by_directory_value={"engineering": engineering, "r&d": engineering},
        group_rules=((finance_team, finance), (platform_team, engineering)),
    )
    # The directory first, any case, an alias included.
    assert mapping.resolve(" R&D ", [finance_team]) == engineering
    # Then the first group rule by priority.
    assert mapping.resolve(None, [platform_team, finance_team]) == finance
    assert mapping.resolve("Marketing", [platform_team]) == engineering
    assert mapping.resolve(None, []) is None

    groups_first = DepartmentMap(
        settings=DepartmentSettings(sources=("groups", "directory")),
        names=mapping.names,
        by_directory_value=mapping.by_directory_value,
        group_rules=mapping.group_rules,
    )
    assert groups_first.resolve("Engineering", [finance_team]) == finance
    assert not DepartmentMap().configured


def test_the_settings_keep_only_known_sources_once():
    from app.services.departments import normalize_settings

    settings = normalize_settings({"sources": ["groups", "hris", "groups"], "directory_attribute": "  division "})
    assert settings.sources == ("groups",)
    assert settings.directory_attribute == "division"
    assert normalize_settings(None).sources == ("directory", "groups")


def test_a_sign_in_carries_the_directory_department_under_its_attribute():
    from app.services.departments import directory_department_from_claims

    saml = {"http://schemas.example.test/claims/Department": ["", " Finance "], "email": ["a@b.test"]}
    assert directory_department_from_claims(saml, "department") == "Finance"
    assert directory_department_from_claims({"department": "Sales"}, "department") == "Sales"
    assert directory_department_from_claims({"division": "Ops"}, "department") is None
    assert directory_department_from_claims({"division": ["Ops"]}, "division") == "Ops"


def test_a_projection_is_fitted_on_complete_months_only():
    from app.services.platform_insights.analytics import _complete_months, _partial

    start = datetime(2026, 1, 15, 9, tzinfo=UTC)
    end = datetime(2026, 6, 10, 12, tzinfo=UTC)
    months = _complete_months(start, end, coverage=date(2026, 3, 1), today=end.date())
    # January is cut by the window, February is before the data, June is the current month.
    assert months == [date(2026, 3, 1), date(2026, 4, 1), date(2026, 5, 1)]
    assert _partial("month", "2026-01-01", start, end)
    assert _partial("month", "2026-06-01", start, end)
    assert not _partial("month", "2026-04-01", start, end)
    assert not _partial("day", "2026-04-01", start, end)


def test_the_attribution_classes_explain_every_residual():
    from app.services.tracing.attribution import PERSON_ATTRIBUTIONS, RESIDUAL_ATTRIBUTIONS, RESIDUAL_NOTES

    assert set(RESIDUAL_NOTES) == set(RESIDUAL_ATTRIBUTIONS)
    assert not set(PERSON_ATTRIBUTIONS) & set(RESIDUAL_ATTRIBUTIONS)
    assert all(len(value) <= 20 for value in (*PERSON_ATTRIBUTIONS, *RESIDUAL_ATTRIBUTIONS))
