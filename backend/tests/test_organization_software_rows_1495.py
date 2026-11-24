# SPDX-License-Identifier: Apache-2.0

"""#1495: rows sent to the ``software`` section become Software entities, never profile rows."""

from __future__ import annotations

import pytest

from app.services import organization_context_rows as rows


def test_software_sections_cover_the_deprecated_alias() -> None:
    assert set(rows.SOFTWARE_SECTIONS) == {"software"}
    assert "software" not in rows.ROW_SECTIONS, "no alias (Sam, 24 Nov 2026)"
    assert set(rows.SOFTWARE_SECTIONS) <= set(rows.ROW_SECTIONS)


def test_a_new_software_is_planned_and_an_existing_one_kept() -> None:
    existing = {rows.software_key("Microsoft", "Defender")}
    plan = rows.plan_software_upsert(
        "intune",
        [
            {"product": "Defender", "vendor": "microsoft "},
            {"product": "CrowdStrike Falcon", "vendor": "CrowdStrike", "cpe": "", "version": "7.1"},
        ],
        existing=existing,
    )
    assert plan.unchanged == ["Defender"]
    assert plan.added == ["CrowdStrike Falcon"]
    assert plan.writes == [{"vendor": "CrowdStrike", "product": "CrowdStrike Falcon", "cpe": "", "version": "7.1"}]
    assert plan.changed and plan.profile is None


def test_bad_rows_are_refused_with_their_reason() -> None:
    plan = rows.plan_software_upsert(
        "",
        ["nope", {"vendor": "x"}, {"product": "Jira"}, {"product": "jira"}],
        existing=set(),
    )
    reasons = [reason for _, reason in plan.refused]
    assert reasons[0] == "not an object"
    assert "product" in reasons[1]
    assert reasons[2] == "sent twice in this call"
    assert plan.added == ["Jira"]


def test_intune_detected_apps_map_to_software_rows() -> None:
    mapped = rows.map_rows([{"displayName": "CrowdStrike Falcon Sensor"}], "intune_detected_app")
    plan = rows.plan_software_upsert("intune", mapped, existing=set())
    assert plan.added and not plan.refused


def test_a_batch_over_the_cap_is_refused() -> None:
    with pytest.raises(rows.RowError):
        rows.plan_software_upsert("intune", [{}] * (rows.MAX_BATCH + 1), existing=set())
