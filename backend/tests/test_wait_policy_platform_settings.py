"""The wait policy as a platform AI setting with per-agent and per-assignment overrides."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.services import wait_settings
from app.tools.wait_policy import (
    MAXIMUM_TOTAL_WAIT_TIME_CEILING,
    WaitBudget,
    WaitPolicy,
    build_wait_tool_definition,
    execute_wait,
    grant_wait,
    normalize_wait_policy_override,
    resolve_wait_policy,
)

PLATFORM = WaitPolicy(per_call_seconds=600, total_seconds=7200)


# -- resolution order ---------------------------------------------------------


def test_no_override_resolves_to_the_platform_policy():
    assert resolve_wait_policy(PLATFORM, None, None) == PLATFORM


def test_the_agent_override_narrows_the_platform():
    agent = {"per_call_seconds": 120, "total_seconds": 900}
    assert resolve_wait_policy(PLATFORM, agent, None) == WaitPolicy(120, 900)


def test_the_assignment_override_wins_over_the_agent():
    agent = {"per_call_seconds": 120, "total_seconds": 900, "unbounded_with_until": False}
    assignment = {"total_seconds": 3600, "unbounded_with_until": True}
    resolved = resolve_wait_policy(PLATFORM, agent, assignment)
    assert resolved == WaitPolicy(per_call_seconds=120, total_seconds=3600, unbounded_with_until=True)


def test_an_override_never_exceeds_the_platform_values():
    resolved = resolve_wait_policy(PLATFORM, {"per_call_seconds": 3600, "total_seconds": 86400})
    assert resolved == PLATFORM


def test_a_malformed_stored_override_is_ignored_not_fatal():
    assert resolve_wait_policy(PLATFORM, {"bogus": 1}, "not-an-object") == PLATFORM


# -- override validation ------------------------------------------------------


@pytest.mark.parametrize("cleared", [None, {}, "none", "None", ""], ids=["null", "empty", "none", "None", "blank"])
def test_none_clears_an_override(cleared):
    assert normalize_wait_policy_override(cleared) is None


def test_an_override_accepts_durations_and_rejects_unknown_keys():
    assert normalize_wait_policy_override({"per_call_seconds": "5m", "total_seconds": 3600}) == {
        "per_call_seconds": 300,
        "total_seconds": 3600,
    }
    with pytest.raises(ValueError, match="unknown key"):
        normalize_wait_policy_override({"total": 10})
    with pytest.raises(ValueError, match="positive duration"):
        normalize_wait_policy_override({"total_seconds": 0})
    with pytest.raises(ValueError, match="true or false"):
        normalize_wait_policy_override({"unbounded_with_until": "yes"})


# -- ceilings come from the platform -----------------------------------------


def test_the_total_ceiling_an_admin_may_set_is_24_hours():
    assert MAXIMUM_TOTAL_WAIT_TIME_CEILING == 86400
    ps = SimpleNamespace(maximum_wait_time=None, maximum_total_wait_time=10**9, wait_unbounded_with_until=None)
    assert WaitPolicy.from_config(ps).total_seconds == 86400


def test_platform_policy_reads_platform_settings(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.agent_maximum_wait_time", None, raising=False)
    monkeypatch.setattr("app.core.config.settings.agent_maximum_total_wait_time", None, raising=False)
    ps = SimpleNamespace(maximum_wait_time=90, maximum_total_wait_time=5400, wait_unbounded_with_until=True)
    assert wait_settings.platform_wait_policy(ps) == WaitPolicy(90, 5400, True)
    assert wait_settings.platform_wait_policy(None) == WaitPolicy()


# -- env lock -----------------------------------------------------------------


def test_the_environment_wins_and_locks_the_field(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.agent_maximum_wait_time", None, raising=False)
    monkeypatch.setattr("app.core.config.settings.agent_maximum_total_wait_time", 14400, raising=False)
    ps = SimpleNamespace(maximum_wait_time=60, maximum_total_wait_time=600, wait_unbounded_with_until=None)

    assert wait_settings.platform_wait_policy(ps).total_seconds == 14400
    assert wait_settings.env_locked_wait_fields() == {"maximum_total_wait_time"}
    assert wait_settings.serialize_wait_settings(ps)["wait_env_locked_fields"] == ["maximum_total_wait_time"]
    with pytest.raises(wait_settings.WaitSettingsLockedError):
        wait_settings.apply_wait_settings(ps, {"maximum_total_wait_time": 60})
    # An unlocked field is still writable in the same state.
    assert wait_settings.apply_wait_settings(ps, {"maximum_wait_time": 120}) == {"maximum_wait_time": 120}


def test_apply_wait_settings_clamps_validates_and_reports_changes(monkeypatch):
    monkeypatch.setattr("app.core.config.settings.agent_maximum_wait_time", None, raising=False)
    monkeypatch.setattr("app.core.config.settings.agent_maximum_total_wait_time", None, raising=False)
    ps = SimpleNamespace(maximum_wait_time=None, maximum_total_wait_time=None, wait_unbounded_with_until=None)

    changes = wait_settings.apply_wait_settings(
        ps, {"maximum_wait_time": 99999, "maximum_total_wait_time": 3600, "wait_unbounded_with_until": True}
    )
    assert changes == {"maximum_wait_time": 3600, "maximum_total_wait_time": 3600, "wait_unbounded_with_until": True}
    assert wait_settings.apply_wait_settings(ps, {"maximum_wait_time": 3600}) == {}
    with pytest.raises(ValueError):
        wait_settings.apply_wait_settings(ps, {"maximum_wait_time": -1})
    with pytest.raises(ValueError):
        wait_settings.apply_wait_settings(ps, {"wait_unbounded_with_until": "yes"})


# -- unbounded while ``until`` names a condition ------------------------------


def test_a_wait_on_a_condition_is_not_charged_when_unbounded():
    budget = WaitBudget(WaitPolicy(per_call_seconds=300, total_seconds=60, unbounded_with_until=True))
    budget.charge(50, on_condition=True)
    assert budget.remaining_total() == 60
    budget.charge(50)
    assert budget.remaining_total() == 10


def test_the_grant_ignores_the_spent_total_only_for_a_condition_wait():
    budget = WaitBudget(WaitPolicy(per_call_seconds=300, total_seconds=60, unbounded_with_until=True))
    budget.charge(60)
    assert grant_wait(120, budget=budget).refused
    on_condition = grant_wait(120, budget=budget, on_condition=True)
    assert on_condition.granted == 120
    # The per-call cap still applies.
    assert grant_wait(900, budget=budget, on_condition=True).granted == 300


def test_without_the_flag_a_condition_wait_is_charged():
    budget = WaitBudget(WaitPolicy(per_call_seconds=300, total_seconds=60))
    budget.charge(50, on_condition=True)
    assert budget.remaining_total() == 10


def test_execute_wait_does_not_charge_a_condition_wait_when_unbounded():
    budget = WaitBudget(WaitPolicy(per_call_seconds=300, total_seconds=600, unbounded_with_until=True))
    task_id = "11111111-1111-4111-8111-111111111111"
    calls = {"n": 0}

    async def condition(ids):
        calls["n"] += 1
        return (calls["n"] > 1, {i: "completed" if calls["n"] > 1 else "running" for i in ids})

    result = asyncio.run(
        execute_wait({"duration": "10s", "until": task_id}, budget=budget, condition=condition, poll_interval=0.01)
    )
    assert result.reason == "condition"
    assert budget.spent_seconds == 0
    assert budget.wait_count == 1


def test_the_tool_description_says_condition_waits_are_not_charged():
    text = build_wait_tool_definition(WaitPolicy(300, 1800, True)).description
    assert "not charged" in text
    assert "not charged" not in build_wait_tool_definition(WaitPolicy(300, 1800)).description


# -- admin tools: get/update_platform_ai_settings ------------------------------


class _FakeSession:
    def __init__(self):
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def commit(self):
        self.committed = True


def _admin_tools(monkeypatch, *, admin, ps):
    from app.services.builtin_tools import platform_admin

    session = _FakeSession()
    audits: list[dict] = []

    async def _admin(_db, _user_id):
        return SimpleNamespace(id="u", is_admin=True) if admin else None

    async def _ps(_db):
        return ps

    async def _audit(_db, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(platform_admin, "_admin", _admin)
    monkeypatch.setattr("app.api.v1.platform_settings_helpers.get_platform_settings", _ps)
    monkeypatch.setattr("app.services.audit.log_audit", _audit)
    monkeypatch.setattr("app.core.config.settings.agent_maximum_wait_time", None, raising=False)
    monkeypatch.setattr("app.core.config.settings.agent_maximum_total_wait_time", None, raising=False)
    monkeypatch.setattr("app.core.config.settings.default_reasoning_effort", "", raising=False)
    _defs, handlers = platform_admin.build_platform_admin_tools("00000000-0000-4000-8000-000000000001")
    return handlers, session, audits


def _ps():
    return SimpleNamespace(
        maximum_wait_time=None,
        maximum_total_wait_time=None,
        wait_unbounded_with_until=None,
        default_reasoning_effort=None,
        agent_default_model=None,
        agent_default_provider=None,
    )


def test_a_non_admin_gets_403_from_both_admin_tools(monkeypatch):
    ps = _ps()
    handlers, session, audits = _admin_tools(monkeypatch, admin=False, ps=ps)
    assert asyncio.run(handlers["get_platform_ai_settings"]()).startswith("Error 403")
    answer = asyncio.run(handlers["update_platform_ai_settings"](maximum_total_wait_time=7200))
    assert answer.startswith("Error 403")
    assert ps.maximum_total_wait_time is None and not session.committed and not audits


def test_an_admin_changes_the_wait_policy_audited(monkeypatch):
    import json

    ps = _ps()
    handlers, session, audits = _admin_tools(monkeypatch, admin=True, ps=ps)
    answer = json.loads(
        asyncio.run(
            handlers["update_platform_ai_settings"](maximum_total_wait_time=7200, wait_unbounded_with_until=True)
        )
    )
    assert answer["changed"] == {"maximum_total_wait_time": 7200, "wait_unbounded_with_until": True}
    assert answer["settings"]["agent_runs"]["maximum_total_wait_time"] == 7200
    assert session.committed
    assert audits and audits[0]["details"]["via"] == "update_platform_ai_settings"
    read = json.loads(asyncio.run(handlers["get_platform_ai_settings"]()))
    assert read["agent_runs"]["wait_unbounded_with_until"] is True


def test_the_admin_tool_refuses_an_env_pinned_value(monkeypatch):
    ps = _ps()
    handlers, session, _audits = _admin_tools(monkeypatch, admin=True, ps=ps)
    monkeypatch.setattr("app.core.config.settings.agent_maximum_wait_time", 120, raising=False)
    answer = asyncio.run(handlers["update_platform_ai_settings"](maximum_wait_time=60))
    assert answer.startswith("Error 403")
    assert ps.maximum_wait_time is None and not session.committed


# -- agent builder: wait_policy on agents and assignments ------------------------


def test_the_agent_builder_parses_wait_policy_like_the_api():
    from app.services.builtin_tools.agent_builder import _parse_wait_policy_arg

    assert _parse_wait_policy_arg("") == (None, None, False)
    assert _parse_wait_policy_arg("none") == (None, None, True)
    assert _parse_wait_policy_arg('{"total_seconds": 3600}') == ({"total_seconds": 3600}, None, True)
    _value, error, provided = _parse_wait_policy_arg({"total": 1})
    assert provided and error and "unknown key" in error
