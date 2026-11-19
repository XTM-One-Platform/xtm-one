"""``get_user_default_agents``: each person's own default chat agent, read by an agent (#529).

The reach rule (administrator: everyone; an agent: the people it is shared with
or whose fleet workspace it orchestrates; else the caller alone), the refusal
outside it, and the row of each person (their choice, the General Assistant,
the platform's wiring, nothing set) run without a database: the relation
lookup is replaced and a session double answers the agent and fleet reads.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

CALLER = uuid.uuid4()
OWNER = uuid.uuid4()
STRANGER = uuid.uuid4()
AGENT_ID = uuid.uuid4()
OFFICE_AGENT = uuid.uuid4()
CTEM = uuid.uuid4()


def _scope(platform: bool):
    from app.services.platform_insights.scope import InsightScope

    return InsightScope(user=SimpleNamespace(id=CALLER), platform=platform, group_ids=())


def _user(uid, email, agent=None, source=None, set_at=None):
    return SimpleNamespace(
        id=uid,
        email=email,
        full_name=email.split("@")[0],
        default_agent_id=agent,
        default_agent_source=source,
        default_agent_set_at=set_at,
    )


def _agent(aid, name, slug=None):
    return SimpleNamespace(id=aid, name=name, slug=slug, enabled=True)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Db:
    """Answers the agents read, then the home fleets read."""

    def __init__(self, agents=(), fleets=()):
        self._answers = [list(agents), list(fleets)]

    async def execute(self, _statement):
        return _Result(self._answers.pop(0) if self._answers else [])


def _call(monkeypatch, people, *, platform=False, related=(), agents=(), fleets=(), args=None):
    from app.services.platform_insights import default_agents as module

    async def _related(_db, _agent_id):
        return {str(uid) for uid in related}

    async def _resolve(_db, _args, _scope):
        return list(people)

    monkeypatch.setattr(module, "agent_reach", _related)
    monkeypatch.setattr(module, "_resolve", _resolve)
    return asyncio.run(
        module.get_user_default_agents(_Db(agents, fleets), _scope(platform), args or {}, agent_id=str(AGENT_ID))
    )


# ── Reach ───────────────────────────────────────────────────────────────────


def test_an_administrators_agent_reads_anyone(monkeypatch):
    result = _call(monkeypatch, [_user(STRANGER, "s@x.io")], platform=True)
    assert result["scope"] == "platform"
    assert result["people"][0]["email"] == "s@x.io"


def test_an_agent_reads_the_people_it_relates_to(monkeypatch):
    result = _call(monkeypatch, [_user(OWNER, "o@x.io")], related=[OWNER])
    assert result["scope"] == "agent"
    assert result["people"][0]["status"] == "none"


def test_a_person_outside_the_agents_reach_is_refused(monkeypatch):
    from app.services.platform_insights.scope import InsightRefusedError

    with pytest.raises(InsightRefusedError, match=r"outside your reach: s@x\.io"):
        _call(monkeypatch, [_user(OWNER, "o@x.io"), _user(STRANGER, "s@x.io")], related=[OWNER])


def test_an_agent_relating_to_nobody_but_the_caller_reads_the_caller_alone(monkeypatch):
    from app.services.platform_insights.scope import InsightRefusedError

    result = _call(monkeypatch, [_user(CALLER, "me@x.io")], related=[CALLER])
    assert result["scope"] == "self"
    with pytest.raises(InsightRefusedError, match="Your own default only"):
        _call(monkeypatch, [_user(OWNER, "o@x.io")], related=[CALLER])


def test_agent_reach_reads_shares_and_orchestrated_fleet_workspaces(monkeypatch):
    from app.services.platform_insights import default_agents as module

    group = uuid.uuid4()
    ws_group = uuid.uuid4()
    member = uuid.uuid4()
    ws_member = uuid.uuid4()
    shared = uuid.uuid4()
    ws_owner = uuid.uuid4()
    agent = SimpleNamespace(user_id=OWNER, shared_user_ids=[str(shared)], shared_group_ids=[str(group)])
    workspace = SimpleNamespace(user_id=ws_owner, shared_user_ids=None, shared_group_ids=[str(ws_group)])
    seen_groups: list[set] = []

    class _AgentDb:
        async def get(self, _model, _pk):
            return agent

        async def execute(self, _statement):
            return _Result([workspace])

    async def _members(_db, groups):
        seen_groups.append(set(groups))
        return {str(member), str(ws_member)}

    monkeypatch.setattr(module, "_members", _members)
    people = asyncio.run(module.agent_reach(_AgentDb(), str(AGENT_ID)))
    assert people == {str(OWNER), str(shared), str(ws_owner), str(member), str(ws_member)}
    assert seen_groups == [{group, ws_group}]
    assert asyncio.run(module.agent_reach(_AgentDb(), None)) == set()


# ── Rows ────────────────────────────────────────────────────────────────────


def test_a_chosen_agent_names_its_fleet_and_when(monkeypatch):
    when = datetime(2026, 11, 3, 9, 0, tzinfo=UTC)
    person = _user(OWNER, "o@x.io", agent=OFFICE_AGENT, source="user", set_at=when)
    result = _call(
        monkeypatch,
        [person],
        related=[OWNER],
        agents=[_agent(OFFICE_AGENT, "Office Assistant")],
        fleets=[(OFFICE_AGENT, "CEO Office")],
    )
    row = result["people"][0]
    assert row["status"] == "chosen"
    assert row["default_agent"] == {
        "id": str(OFFICE_AGENT),
        "name": "Office Assistant",
        "enabled": True,
        "fleet": "CEO Office",
    }
    assert row["set_at"] == when.isoformat()


def test_no_choice_is_none_set_never_the_platform_default():
    from app.services.platform_insights.default_agents import NONE_SET, describe

    row = describe(_user(OWNER, "o@x.io"), None, None)
    assert row["status"] == "none" and row["default_agent"] is None and row["note"] == NONE_SET


def test_the_platform_wiring_is_not_a_choice():
    from app.services.platform_insights.default_agents import describe

    wired = describe(_user(OWNER, "o@x.io", agent=CTEM, source="platform"), _agent(CTEM, "CTEM Assistant"), None)
    assert wired["status"] == "platform_wired" and wired["default_agent"] is None
    # A row written before the source was recorded: only the platform wires the CTEM Assistant.
    legacy = describe(_user(OWNER, "o@x.io", agent=CTEM), _agent(CTEM, "CTEM Assistant", slug="ctem-assistant"), None)
    assert legacy["status"] == "platform_wired"
    legacy_choice = describe(_user(OWNER, "o@x.io", agent=OFFICE_AGENT), _agent(OFFICE_AGENT, "Office"), None)
    assert legacy_choice["status"] == "chosen" and legacy_choice["set_at"] is None
    assert "set_at_note" in legacy_choice


def test_the_general_assistant_chosen_and_a_deleted_agent():
    from app.services.platform_insights.default_agents import describe

    general = describe(_user(OWNER, "o@x.io", source="user", set_at=datetime.now(UTC)), None, None)
    assert general["status"] == "general_assistant"
    dangling = describe(_user(OWNER, "o@x.io", agent=OFFICE_AGENT, source="user"), None, None)
    assert dangling["status"] == "dangling" and dangling["default_agent"] is None


# ── Wiring ──────────────────────────────────────────────────────────────────


def test_the_tool_is_a_read_of_the_suite_exempt_from_approval():
    from app.services.builtin_tools import build_platform_insights_tools
    from app.services.hitl_whitelist_default import _READ_ONLY_PLATFORM_INSIGHTS

    defs, _executor = build_platform_insights_tools("", agent_id=str(AGENT_ID))
    definition = next(d for d in defs if d.name == "get_user_default_agents")
    assert set(definition.input_schema["properties"]) == {"users", "group_ids"}
    assert "get_user_default_agents" in _READ_ONLY_PLATFORM_INSIGHTS
