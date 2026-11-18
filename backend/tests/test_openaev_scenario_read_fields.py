"""Scenario read and search answers name recurrence, origin, inject count and dates (#252)."""

from app.integrations.providers.openaev._common import _fmt_scenario


def test_scheduled_builtin_scenario_names_recurrence_origin_and_counts() -> None:
    text = _fmt_scenario(
        {
            "scenario_id": "s-1",
            "scenario_name": "Ransomware drill",
            "scenario_recurrence": "0 0 9 * * MON",
            "scenario_recurrence_start": "2026-11-01T00:00:00Z",
            "scenario_recurrence_end": "2026-12-31T00:00:00Z",
            "scenario_dependencies": ["STARTERPACK"],
            "scenario_injects": ["i-1", "i-2", "i-3"],
            "scenario_tags": ["t-1"],
            "scenario_created_at": "2026-10-01T10:00:00Z",
            "scenario_updated_at": "2026-11-02T10:00:00Z",
        }
    )
    assert "Recurrence: 0 0 9 * * MON (cron, UTC), from 2026-11-01T00:00:00Z, until 2026-12-31T00:00:00Z" in text
    assert "Origin: built-in (starter pack)" in text
    assert "Injects: 3" in text
    assert "Tags: 1" in text
    assert "Created: 2026-10-01T10:00:00Z" in text
    assert "Updated: 2026-11-02T10:00:00Z" in text


def test_unplanned_user_scenario_says_not_planned_and_user_origin() -> None:
    text = _fmt_scenario(
        {
            "scenario_id": "s-2",
            "scenario_name": "Custom",
            "scenario_recurrence": None,
            "scenario_dependencies": [],
            "scenario_injects": [],
        }
    )
    assert "Recurrence: not planned" in text
    assert "Origin: created or imported by a user" in text
    assert "Injects: 0" in text
    assert "Tags:" not in text


def test_minimal_payload_adds_no_state_line() -> None:
    text = _fmt_scenario({"scenario_id": "s-3", "scenario_name": "Bare"})
    for label in ("Recurrence:", "Origin:", "Injects:", "Tags:", "Created:", "Updated:"):
        assert label not in text
