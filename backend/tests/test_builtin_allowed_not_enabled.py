"""An allow list for a built-in group the agent does not enable grants nothing (#1428).

The writers refuse such an entry, a stale one goes with its group, get_agent
flags it, and every agent holding a weekly fleet health check gets the
``platform`` group scoped to the health functions, at boot too.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.services.builtin_allowed_tools import (
    allowlisted_groups_not_enabled,
    not_enabled_written_or_pruned,
    without_groups_not_enabled,
)

FORMER_IDS_ONLY = ["work_items", "shared_state", "generate_file"]


def test_an_allowlist_for_a_group_not_enabled_is_named() -> None:
    assert allowlisted_groups_not_enabled(FORMER_IDS_ONLY, {"platform": ["get_fleet_health"]}) == ["platform"]


def test_an_allowlist_for_an_enabled_group_or_its_former_id_is_fine() -> None:
    assert allowlisted_groups_not_enabled(["platform"], {"platform": ["get_fleet_health"]}) == []
    # ``work_items`` is a former id of ``coordination``: a list keyed by the group is fine.
    assert allowlisted_groups_not_enabled(["work_items"], {"coordination": ["list_work_items"]}) == []


def test_a_stale_value_never_fails_the_read() -> None:
    assert allowlisted_groups_not_enabled(["web"], None) == []
    assert allowlisted_groups_not_enabled(["web"], "not a map") == []


def test_the_entries_of_groups_not_enabled_are_dropped_alone() -> None:
    kept = without_groups_not_enabled(["web"], {"platform": ["get_fleet_health"], "web": ["web_fetch"]})
    assert kept == {"web": ["web_fetch"]}
    assert without_groups_not_enabled(["web"], {"platform": ["get_fleet_health"]}) is None


def test_a_write_that_adds_an_entry_for_a_group_not_enabled_is_refused() -> None:
    agent = SimpleNamespace(builtin_tool_ids=["coordination"], builtin_allowed_tools={"platform": ["get_fleet_health"]})
    refusal = not_enabled_written_or_pruned(agent, None)
    assert refusal is not None and "'platform'" in refusal and "builtin_tool_ids" in refusal
    # Nothing changed: the caller answers the refusal.
    assert agent.builtin_allowed_tools == {"platform": ["get_fleet_health"]}


def test_a_group_switched_off_takes_its_entry_with_it() -> None:
    before = {"platform": ["get_fleet_health"]}
    agent = SimpleNamespace(builtin_tool_ids=["web"], builtin_allowed_tools=dict(before))
    assert not_enabled_written_or_pruned(agent, before) is None
    assert agent.builtin_allowed_tools is None


def test_an_enabled_group_passes_untouched() -> None:
    agent = SimpleNamespace(builtin_tool_ids=["platform"], builtin_allowed_tools={"platform": ["get_fleet_health"]})
    assert not_enabled_written_or_pruned(agent, None) is None
    assert agent.builtin_allowed_tools == {"platform": ["get_fleet_health"]}


def test_the_rest_create_path_refuses_it() -> None:
    from fastapi import HTTPException

    from app.api.v1.agents import _builtin_allowed_enabled_or_422

    agent = SimpleNamespace(builtin_tool_ids=["web"], builtin_allowed_tools={"platform": ["get_fleet_health"]})
    with pytest.raises(HTTPException) as exc:
        _builtin_allowed_enabled_or_422(agent, None)
    assert exc.value.status_code == 422


# ── The health-check holders ────────────────────────────────────────────────


def test_a_desk_lead_on_former_ids_gets_the_platform_group_scoped_to_the_health_tools() -> None:
    from app.services.fleets import FLEET_HEALTH_FUNCTIONS, grant_health_check_tools

    agent = SimpleNamespace(
        builtin_tool_ids=list(FORMER_IDS_ONLY),
        builtin_allowed_tools={"platform": ["get_fleet_health"]},
        config={},
    )
    assert grant_health_check_tools(cast(Any, agent))
    assert "platform" in agent.builtin_tool_ids
    assert agent.builtin_allowed_tools["platform"] == list(FLEET_HEALTH_FUNCTIONS)
    assert set(FLEET_HEALTH_FUNCTIONS) == {"get_fleet_health", "dismiss_fleet_health_findings"}
    # Moved off the former ids to their canonical groups.
    assert not set(FORMER_IDS_ONLY) & set(agent.builtin_tool_ids)
    assert allowlisted_groups_not_enabled(agent.builtin_tool_ids, agent.builtin_allowed_tools) == []
    # Idempotent.
    assert not grant_health_check_tools(cast(Any, agent))


def test_an_unscoped_platform_group_is_left_unscoped() -> None:
    from app.services.fleets import grant_health_check_tools

    agent = SimpleNamespace(builtin_tool_ids=["platform"], builtin_allowed_tools=None, config={})
    assert not grant_health_check_tools(cast(Any, agent))
    assert agent.builtin_allowed_tools is None


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> SimpleNamespace:
        return SimpleNamespace(all=lambda: self._rows)


class _Db:
    def __init__(self, assignments: list[Any], agents: dict[uuid.UUID, Any]) -> None:
        self._assignments = assignments
        self._agents = agents
        self.flushed = 0

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self._assignments)

    async def get(self, _model: Any, key: uuid.UUID) -> Any:
        return self._agents.get(key)

    async def flush(self) -> None:
        self.flushed += 1


@pytest.mark.asyncio
async def test_the_boot_repair_grants_every_holder_once(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import fleets

    lead = SimpleNamespace(builtin_tool_ids=list(FORMER_IDS_ONLY), builtin_allowed_tools=None, config={})
    lead_id = uuid.uuid4()
    check = SimpleNamespace(agent_id=lead_id, name=fleets.FLEET_HEALTH_CHECK_NAME)
    monkeypatch.setattr(fleets, "is_fleet_health_check", lambda _row: True)
    db = _Db([check, check], {lead_id: lead})
    assert await fleets.grant_health_check_holders(cast(Any, db)) == 1
    assert lead.builtin_allowed_tools["platform"] == list(fleets.FLEET_HEALTH_FUNCTIONS)
    assert db.flushed == 1
    # A second boot changes nothing.
    assert await fleets.grant_health_check_holders(cast(Any, db)) == 0
