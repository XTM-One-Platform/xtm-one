"""CTEM stage assessments stay fresh (#12002).

Regression of the command center v2 default layout (e3685252, #10277): the v1
stage cards, the only place that generated a stage assessment on visit, left
the default layout and the stage page never generated one. These tests pin
the three halves of the contract:

* the weekly fan-out: one scheduled ``Weekly <stage> assessment`` per CTEM
  stage, run per user, enabled by default (never an admin opt-in);
* the freshness the stage page reads to generate on visit: ``stale`` past
  the weekly window, never on a fresh one or without a date;
* the organization-context info line is shown once, by the page banner,
  never inside the assessment or briefing prose.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.agents.builtin_agents import BUILTIN_ASSIGNMENTS
from app.agents.fleet_opt_in import ENABLED_BY_DEFAULT_ALLOWLIST
from app.api.v1 import ctem_metrics
from app.services.ctem_scoring import STAGE_KEYS
from app.services.organization_context import BRIEF_NOTICE_LINE


def _weekly_by_stage() -> dict[str, dict]:
    return {
        data["name"].removeprefix("Weekly ").removesuffix(" assessment"): data
        for data in BUILTIN_ASSIGNMENTS
        if data["name"].startswith("Weekly ") and data["name"].endswith(" assessment")
    }


def test_every_stage_has_a_weekly_per_user_assessment_enabled_by_default():
    weekly = _weekly_by_stage()
    assert set(weekly) == set(STAGE_KEYS)
    for stage, data in weekly.items():
        assert data["trigger_type"] == "schedule", stage
        assert data["trigger_config"] == {"cron": "0 8 * * 1", "fanout_audience": "ctem_users"}, stage
        assert data["run_as_all_users"] is True, stage
        assert data["enabled"] is True, stage
        # Never an automation an administrator must switch on first.
        assert not data.get("admin_opt_in"), stage
        assert (data["agent_slug"], data["name"]) in ENABLED_BY_DEFAULT_ALLOWLIST, stage


def test_every_stage_keeps_the_on_demand_assessment_the_page_triggers():
    names = {(data["agent_slug"], data["name"]) for data in BUILTIN_ASSIGNMENTS}
    for stage, data in _weekly_by_stage().items():
        assert (data["agent_slug"], f"Generate my {stage} assessment") in names


def test_an_assessment_past_the_weekly_window_is_stale():
    now = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
    window = timedelta(days=ctem_metrics.STAGE_ASSESSMENT_FRESH_DAYS)
    assert ctem_metrics._assessment_is_stale(now - window - timedelta(minutes=1), now) is True
    assert ctem_metrics._assessment_is_stale(now - window + timedelta(minutes=1), now) is False
    assert ctem_metrics._assessment_is_stale(None, now) is False


def test_a_naive_timestamp_is_read_as_utc():
    now = datetime(2026, 11, 15, 9, 0, tzinfo=UTC)
    naive = (now - timedelta(days=30)).replace(tzinfo=None)
    assert ctem_metrics._assessment_is_stale(naive, now) is True


def test_the_freshness_window_is_the_weekly_cadence():
    assert ctem_metrics.STAGE_ASSESSMENT_FRESH_DAYS == 7


def test_the_organization_context_line_is_dropped_from_the_prose():
    line = BRIEF_NOTICE_LINE.format(fields="name, sector, priority intelligence requirements (PIRs)")
    raw = (
        '{"headline": "OpenCTI tracks a wide picture.", '
        f'"assessment": "Simulation coverage is unmeasured. {line}", '
        f'"key_findings": [{{"title": "x", "detail": "Unvalidated. {line}"}}]}}'
    )
    parsed = ctem_metrics._parse_briefing(raw)
    assert parsed is not None
    assert parsed["assessment"] == "Simulation coverage is unmeasured."
    assert parsed["key_findings"][0]["detail"] == "Unvalidated."
    assert "Organization context incomplete" not in str(parsed)


def test_prose_without_the_line_is_unchanged():
    parsed = ctem_metrics._parse_briefing('{"headline": "h", "assessment": "a.", "score": 3}')
    assert parsed == {"headline": "h", "assessment": "a.", "score": 3}
