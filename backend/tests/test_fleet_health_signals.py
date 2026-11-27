"""Fleet health findings (#11700): the pure rules, the report shape, the
orchestrator binding and the seeded skill, without a database."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import fleet_health_signals as signals

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)


# ── assignment_failing ──────────────────────────────────────────────────────


def test_three_failed_judged_runs_make_an_assignment_failing():
    runs = [("failed", "boom")] * 3
    assert signals.assignment_is_failing(runs)


def test_one_success_among_the_last_three_is_not_failing():
    assert not signals.assignment_is_failing([("failed", "x"), ("completed", None), ("failed", "x")])


def test_fewer_than_three_judged_runs_is_not_failing():
    assert not signals.assignment_is_failing([("failed", "x"), ("failed", "x")])


def test_a_platform_interruption_is_not_judged():
    cut = ("failed", "Run marked as failed by stale-assignment-run cleanup.")
    # Interruptions in between are skipped: the three judged runs all failed.
    assert signals.assignment_is_failing([("failed", "x"), cut, ("failed", "y"), cut, ("failed", "z")])
    # Interruptions alone never make an assignment failing.
    assert not signals.assignment_is_failing([cut] * 5)


# ── assignment_silent ───────────────────────────────────────────────────────


def test_an_old_schedule_that_never_fired_is_silent():
    assert signals.schedule_is_silent(created_at=NOW - timedelta(days=30), last_triggered_at=None, now=NOW)


def test_a_schedule_that_fired_recently_is_not_silent():
    assert not signals.schedule_is_silent(
        created_at=NOW - timedelta(days=30), last_triggered_at=NOW - timedelta(days=2), now=NOW
    )


def test_a_young_schedule_is_never_silent():
    assert not signals.schedule_is_silent(created_at=NOW - timedelta(days=3), last_triggered_at=None, now=NOW)


# ── cost_spike ──────────────────────────────────────────────────────────────


def test_a_day_over_three_times_the_weekly_average_is_a_spike():
    assert signals.is_cost_spike(today_tokens=1_000_000, week_tokens=7 * 200_000)


def test_a_normal_day_is_not_a_spike():
    assert not signals.is_cost_spike(today_tokens=500_000, week_tokens=7 * 200_000)


def test_a_small_day_is_never_a_spike():
    assert not signals.is_cost_spike(today_tokens=50_000, week_tokens=0)


def test_a_large_first_day_with_no_baseline_is_a_spike():
    assert signals.is_cost_spike(today_tokens=signals.SPIKE_MIN_TOKENS, week_tokens=0)


# ── blocked_without_blocker ─────────────────────────────────────────────────


_OWNER = uuid.uuid4()
_ASK = {
    "question": "Merge it?",
    "options": [{"key": "A", "label": "yes"}, {"key": "B", "label": "no"}],
    "recommendation": "A",
}


@pytest.mark.parametrize(
    "fields",
    [
        {"blocked_on": ["x"], "blocked_on_refs": None, "needs_human": None},
        {"blocked_on": None, "blocked_on_refs": [{"kind": "issue"}], "needs_human": None},
        {"blocked_on": None, "blocked_on_refs": None, "needs_human": True, "ask": _ASK},
        # One live blocker of two is enough.
        {"blocked_on": ["x", "y"], "blocked_on_refs": None, "needs_human": None, "cancelled_blockers": {"y"}},
    ],
)
def test_a_black_or_white_blocked_item_is_fine(fields: dict[str, Any]) -> None:
    assert not signals.blocked_without_blocker(owner_agent_id=_OWNER, **fields)


@pytest.mark.parametrize(
    "fields",
    [
        # Free text alone is no blocker (#762).
        {"blocked_on": None, "blocked_on_refs": None, "blocked_reason": "waits for X", "needs_human": None},
        {"blocked_on": [], "blocked_on_refs": [], "blocked_reason": "  ", "needs_human": False},
        # A person's decision without its card.
        {"blocked_on": None, "blocked_on_refs": None, "needs_human": True},
        {"blocked_on": None, "blocked_on_refs": None, "needs_human": True, "ask": {"question": "Merge it?"}},
        # Every item it waits for was cancelled: nothing will release it.
        {"blocked_on": ["x"], "blocked_on_refs": None, "needs_human": None, "cancelled_blockers": {"x"}},
    ],
)
def test_a_blocked_item_that_is_not_black_or_white_is_reported(fields: dict[str, Any]) -> None:
    assert signals.blocked_without_blocker(owner_agent_id=_OWNER, **fields)


def test_a_wait_on_its_own_open_pull_request_is_not_reported() -> None:
    # Written by the inbox finalizer for a draft, released by its CI completion.
    bound = {"pull_requests": [{"repo": "o/r", "number": 12, "state": "open", "draft": True}]}
    fields = {"blocked_on": None, "blocked_on_refs": None, "blocked_reason": "WAITS FOR: o/r#12", "needs_human": None}
    assert not signals.blocked_without_blocker(owner_agent_id=_OWNER, payload=bound, **fields)
    closed = {"pull_requests": [{"repo": "o/r", "number": 12, "state": "closed"}]}
    assert signals.blocked_without_blocker(owner_agent_id=_OWNER, payload=closed, **fields)


def test_an_ownerless_blocked_item_is_reported_whatever_it_waits_for() -> None:
    assert signals.blocked_without_blocker(blocked_on=["x"], blocked_on_refs=None, needs_human=None)


# ── The report ──────────────────────────────────────────────────────────────


def _report(findings):
    return signals.FleetHealthReport(fleet_id="f1", fleet_name="Ops", status="ok", members=[], findings=findings)


def test_a_report_with_no_finding_is_healthy():
    report = _report([])
    assert report.healthy
    assert report.structured()["healthy"] is True
    assert "healthy: no finding" in signals.format_report(report)


def test_every_kind_has_a_fix_and_advice():
    assert set(signals.FINDING_FIX) == set(signals.FINDING_ADVICE)
    assert set(signals.FINDING_FIX.values()) <= {signals.FIX_AUTO, signals.FIX_OWNER}
    # The safe fixes the orchestrator applies by itself.
    assert [k for k, v in signals.FINDING_FIX.items() if v == signals.FIX_AUTO] == [
        "orphaned_claim",
        "integration_not_shared",
    ]


def test_findings_carry_their_evidence_capped_with_the_whole_count():
    ids = [uuid.uuid4() for _ in range(signals.MAX_EVIDENCE + 5)]
    finding = signals._finding("past_due", "many", ids)
    assert finding.count == len(ids) and len(finding.evidence) == signals.MAX_EVIDENCE
    report = _report([finding])
    data = report.structured()
    assert data["healthy"] is False
    assert data["findings"][0]["fix"] == "owner" and data["findings"][0]["advice"]
    text = signals.format_report(report)
    assert "[past_due]" in text and "(+5 more)" in text


def test_the_report_names_the_fixes_applied():
    report = _report([signals._finding("orphaned_claim", "1 item", ["a"])])
    assert "Applied: orphaned_claim 1" in signals.format_report(report, applied={"orphaned_claim": 1})


def test_an_activation_check_gates_on_healthy():
    from app.services.assignment_activation_check import condition_met

    red = {"path": "$.healthy", "in": [False]}
    assert not condition_met(_report([]).structured(), red)
    assert condition_met(_report([signals._finding("past_due", "x", ["i"])]).structured(), red)


@pytest.mark.asyncio
async def test_safe_fixes_touch_nothing_without_an_orphaned_claim():
    assert await signals.apply_safe_fixes(_report([signals._finding("past_due", "x", ["i"])])) == {}


# ── The orchestrator binding and the seeded skill ───────────────────────────


class _Db:
    def __init__(self, skills):
        self._skills = skills

    async def execute(self, _stmt):
        rows = self._skills
        return SimpleNamespace(all=lambda: rows)


@pytest.mark.asyncio
async def test_an_orchestrator_gets_the_tool_and_the_skill_once():
    from app.services.fleets import grant_fleet_health_advisory

    skill_id = uuid.uuid4()
    db = _Db(
        [(uuid.uuid4(), ["builtin", "slug:memory-steward"]), (skill_id, ["builtin", "slug:fleet-health-advisory"])]
    )
    agent = SimpleNamespace(builtin_tool_ids=["web"], builtin_allowed_tools=None, skill_ids=[])
    assert await grant_fleet_health_advisory(db, agent)
    assert "platform" in agent.builtin_tool_ids
    assert agent.builtin_allowed_tools["platform"] == [
        "get_fleet_health",
        "dismiss_fleet_health_findings",
        "regenerate_fleet_documentation",
        "list_fleet_doc_findings",
    ]
    assert agent.skill_ids == [str(skill_id)]
    # Idempotent.
    assert not await grant_fleet_health_advisory(db, agent)
    assert agent.skill_ids == [str(skill_id)]


@pytest.mark.asyncio
async def test_an_unscoped_platform_group_is_left_alone_and_a_missing_skill_is_skipped():
    from app.services.fleets import grant_fleet_health_advisory

    agent = SimpleNamespace(builtin_tool_ids=["platform"], builtin_allowed_tools=None, skill_ids=["s1"])
    assert not await grant_fleet_health_advisory(_Db([]), agent)
    assert agent.builtin_tool_ids == ["platform"] and agent.skill_ids == ["s1"]


@pytest.mark.asyncio
async def test_an_orchestrator_also_gets_the_fleet_design_practices_skill():
    """#11948: the suggested practices ride the same grant path as the health advisory."""
    from app.services.fleets import grant_fleet_health_advisory

    health, design = uuid.uuid4(), uuid.uuid4()
    db = _Db(
        [(health, ["builtin", "slug:fleet-health-advisory"]), (design, ["builtin", "slug:fleet-design-practices"])]
    )
    agent = SimpleNamespace(builtin_tool_ids=["platform"], builtin_allowed_tools=None, skill_ids=[str(health)])
    assert await grant_fleet_health_advisory(db, agent)
    assert agent.skill_ids == [str(health), str(design)]
    assert not await grant_fleet_health_advisory(db, agent)


@pytest.mark.asyncio
async def test_an_orchestrator_on_the_health_tools_alone_gets_the_documentation_tools():
    """#1631: an orchestrator granted before the documentation tools gets them at the next backfill."""
    from app.services.fleets import FLEET_DOC_FUNCTIONS, grant_fleet_health_advisory

    agent = SimpleNamespace(
        builtin_tool_ids=["platform"],
        builtin_allowed_tools={"platform": ["get_fleet_health", "dismiss_fleet_health_findings"]},
        skill_ids=[],
    )
    assert await grant_fleet_health_advisory(_Db([]), agent)
    assert set(FLEET_DOC_FUNCTIONS) <= set(agent.builtin_allowed_tools["platform"])
    assert not await grant_fleet_health_advisory(_Db([]), agent)


def test_the_documentation_tools_granted_are_platform_fleet_functions():
    """#1631: the regeneration rule line of the seeded orchestrator personas names a granted tool."""
    from app.services.builtin_tool_aliases import BUILTIN_GROUP_ALIASES
    from app.services.fleets import FLEET_DOC_FUNCTIONS

    group, functions = BUILTIN_GROUP_ALIASES["fleets"]
    assert group == "platform"
    platform_fleet_tools = set(functions)
    assert set(FLEET_DOC_FUNCTIONS) <= platform_fleet_tools


def test_the_coverage_reporter_reads_the_desk_state():
    """#1631: its persona reads openaev:detection-desk:state; the seed gives it the shared-state tools."""
    from app.agents import openaev_detection_desk as desk

    reporter = next(a for a in desk.DETECTION_DESK_AGENTS if a["slug"] == desk.COVERAGE_REPORTER)
    assert "shared_state" in reporter["builtin_tool_ids"]


def test_the_fleet_design_skill_is_seeded_as_suggestions():
    import re

    from app.agents.builtin_agents import BUILTIN_SKILLS
    from app.agents.seed import _resolve_seed_content

    entry = next(s for s in BUILTIN_SKILLS if s["slug"] == "fleet-design-practices")
    assert entry["name"] == "Fleet design and operations"
    assert entry["platform_targets"] == ["global"]
    content = _resolve_seed_content("skill_content", entry["content"])
    assert "Consult" in content and "suggestion" in content
    # Recommendations, never platform rules about the fleet's shape.
    assert not re.search(r"\b(must|never)\b", content, re.I)


def test_the_skill_is_seeded_with_its_content():
    from app.agents.builtin_agents import BUILTIN_SKILLS
    from app.agents.seed import _resolve_seed_content

    entry = next(s for s in BUILTIN_SKILLS if s["slug"] == "fleet-health-advisory")
    assert entry["platform_targets"] == ["global"]
    content = _resolve_seed_content("skill_content", entry["content"])
    assert "get_fleet_health" in content and "apply_safe_fixes" in content
    # Every finding kind the read can answer is documented in the skill.
    for kind in signals.FINDING_FIX:
        assert f"`{kind}`" in content


# ── S2: the weekly built-in fleet health check ──────────────────────────────


class _AssignmentDb:
    """A session double holding assignments: select returns them, add / delete change them."""

    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.deleted = []

    async def execute(self, _stmt):
        rows = list(self.rows)
        return SimpleNamespace(
            scalars=lambda: SimpleNamespace(first=lambda: rows[0] if rows else None, all=lambda: rows)
        )

    def add(self, row):
        self.rows.append(row)

    async def delete(self, row):
        self.rows.remove(row)
        self.deleted.append(row)


def _fleet():
    return SimpleNamespace(id=uuid.uuid4(), name="Desk")


def _orchestrator():
    return SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), is_company_managed=False)


@pytest.mark.asyncio
async def test_an_orchestrator_gets_the_weekly_check_born_off_and_gated():
    from app.services.assignment_activation_check import parse_activation_check
    from app.services.assignment_pause import CREATED_DISABLED_REASON
    from app.services.fleets import (
        FLEET_HEALTH_CHECK_CRON,
        FLEET_HEALTH_CHECK_NAME,
        ensure_fleet_health_check,
        is_fleet_health_check,
    )

    db, fleet, agent = _AssignmentDb(), _fleet(), _orchestrator()
    assert await ensure_fleet_health_check(db, fleet, agent)
    (check,) = db.rows
    assert check.name == FLEET_HEALTH_CHECK_NAME and check.agent_id == agent.id
    assert check.trigger_type == "schedule" and check.trigger_config == {"cron": FLEET_HEALTH_CHECK_CRON}
    # Off by default, stamped "not set up yet": the fleet reads Not set up and Enable fleet lists it.
    assert check.enabled is False and check.paused_reason == CREATED_DISABLED_REASON
    # The gate is a valid read-only activation check on the fleet's own health.
    parsed, error, _ = parse_activation_check(check.activation_check)
    assert error is None and parsed["tool"] == "get_fleet_health"
    assert parsed["arguments"] == {"fleet_id": str(fleet.id)}
    assert is_fleet_health_check(check)
    # Idempotent: a second call creates nothing.
    assert not await ensure_fleet_health_check(db, fleet, agent)
    assert len(db.rows) == 1


@pytest.mark.asyncio
async def test_the_gate_skips_a_healthy_fleet_and_runs_on_a_finding():
    from app.services.assignment_activation_check import condition_met
    from app.services.fleets import fleet_health_check_gate

    condition = fleet_health_check_gate(uuid.uuid4())["condition"]
    assert not condition_met(_report([]).structured(), condition)
    assert condition_met(_report([signals._finding("orphaned_claim", "x", ["i"])]).structured(), condition)


@pytest.mark.asyncio
async def test_an_existing_check_keeps_its_switch_and_follows_its_fleet():
    from app.services.fleets import ensure_fleet_health_check

    db, agent = _AssignmentDb(), _orchestrator()
    await ensure_fleet_health_check(db, _fleet(), agent)
    db.rows[0].enabled = True
    other = _fleet()
    assert not await ensure_fleet_health_check(db, other, agent)
    assert db.rows[0].enabled is True
    assert db.rows[0].activation_check["arguments"] == {"fleet_id": str(other.id)}


@pytest.mark.asyncio
async def test_the_answer_listener_takes_the_health_decisions_with_the_check_switch():
    """#1406: an owner's answer on a fleet-health item reaches one listener, on with the check."""
    from app.services.assignment_pause import CREATED_DISABLED_REASON
    from app.services.fleets import (
        FLEET_HEALTH_KEY_PREFIX,
        ensure_fleet_health_answered,
        ensure_fleet_health_check,
        fleet_health_check_prompt,
        is_fleet_health_answered,
    )

    db, fleet, agent = _AssignmentDb(), _fleet(), _orchestrator()
    # No seeded check (opted out): nothing files the items, so no listener.
    assert not await ensure_fleet_health_answered(db, fleet, agent)
    await ensure_fleet_health_check(db, fleet, agent)
    (check,) = db.rows
    assert await ensure_fleet_health_answered(db, fleet, agent, check)
    listener = db.rows[1]
    assert is_fleet_health_answered(listener)
    flt = listener.trigger_config["filter"]
    assert listener.trigger_config["scope"] == "own"
    assert (flt["previous_status"], flt["status"]) == ("blocked", "open")
    assert flt["correlation_key"] == {"starts_with": [FLEET_HEALTH_KEY_PREFIX]}
    # The check names the key the listener takes; the listener claims first and dismisses once.
    assert f"{FLEET_HEALTH_KEY_PREFIX}{fleet.id}" in fleet_health_check_prompt(fleet)
    assert "claim_work_item" in listener.prompt and "dismiss_fleet_health_findings" in listener.prompt
    # Born off with the check (not set up yet).
    assert listener.enabled is False and listener.paused_reason == CREATED_DISABLED_REASON
    # Idempotent.
    assert not await ensure_fleet_health_answered(db, fleet, agent, check)
    assert len(db.rows) == 2


@pytest.mark.asyncio
async def test_the_answer_listener_of_a_running_check_is_born_on_as_its_run_as():
    from app.services.fleets import ensure_fleet_health_answered, ensure_fleet_health_check

    db, fleet, agent = _AssignmentDb(), _fleet(), _orchestrator()
    await ensure_fleet_health_check(db, fleet, agent)
    (check,) = db.rows
    check.enabled, check.paused_reason, check.run_as_user_id = True, None, uuid.uuid4()
    assert await ensure_fleet_health_answered(db, fleet, agent, check)
    listener = db.rows[1]
    assert listener.enabled is True and listener.run_as_user_id == check.run_as_user_id


def test_the_skill_keys_the_owner_decisions_for_the_listener():
    from app.agents.builtin_agents import BUILTIN_SKILLS
    from app.agents.seed import _resolve_seed_content
    from app.services.fleets import FLEET_HEALTH_ANSWERED_NAME, FLEET_HEALTH_KEY_PREFIX

    entry = next(s for s in BUILTIN_SKILLS if s["slug"] == "fleet-health-advisory")
    content = _resolve_seed_content("skill_content", entry["content"])
    assert f"`{FLEET_HEALTH_KEY_PREFIX}`" in content and FLEET_HEALTH_ANSWERED_NAME in content


@pytest.mark.asyncio
async def test_a_former_orchestrator_loses_a_check_that_never_ran_only():
    from app.services.fleets import drop_fleet_health_check, ensure_fleet_health_check

    db, agent = _AssignmentDb(), _orchestrator()
    await ensure_fleet_health_check(db, _fleet(), agent)
    db.rows[0].execution_count, db.rows[0].last_triggered_at = 0, None
    assert await drop_fleet_health_check(db, agent.id)
    assert db.rows == []

    await ensure_fleet_health_check(db, _fleet(), agent)
    db.rows[0].execution_count, db.rows[0].last_triggered_at = 2, NOW
    assert not await drop_fleet_health_check(db, agent.id)
    assert len(db.rows) == 1


def test_the_check_counts_as_a_fleet_automation_on_every_fleet():
    from app.services.fleet_views import is_fleet_automation
    from app.services.fleets import FLEET_HEALTH_CHECK_NAME

    assert is_fleet_automation("any-orchestrator", True, FLEET_HEALTH_CHECK_NAME, builtin_fleet=True)
    assert is_fleet_automation("custom", False, FLEET_HEALTH_CHECK_NAME, builtin_fleet=False)
    assert not is_fleet_automation("any-orchestrator", True, "Something else", builtin_fleet=True)
