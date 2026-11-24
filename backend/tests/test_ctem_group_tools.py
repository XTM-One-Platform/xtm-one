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

"""#1748 slice 2: the ``ctem`` group, the CTEM signal and case tools on any agent."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.api.v1 import helpers
from app.core import database
from app.core.config import settings
from app.services import ctem_access, organizations
from app.services import ctem_scope_gate_store as scope_gate
from app.services import ctem_signals as signals
from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS
from app.services.builtin_tools import (
    AGENT_KEYED_BUILTIN_TOOL_IDS,
    CTEM_CASE_TOOL_NAMES,
    CTEM_SIGNAL_TOOL_NAMES,
    build_ctem_tools,
    ctem_tools_for,
    offers_ctem_tools,
)
from app.services.builtin_tools.ctem import TRIAGE_TOOL
from app.tools.base import ToolCall

ORG = uuid.uuid4()
ALL = (*CTEM_SIGNAL_TOOL_NAMES, TRIAGE_TOOL, *CTEM_CASE_TOOL_NAMES)


@pytest.fixture
def xtm_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)


def _agent(slug: str = "my-agent", builtin: bool = False) -> Any:
    return SimpleNamespace(id=uuid.uuid4(), slug=slug, is_builtin=builtin)


def test_the_group_is_registered_agent_keyed_with_every_signal_and_case_tool() -> None:
    group = next(g for g in BUILTIN_TOOL_GROUPS if g.id == "ctem")
    assert group.agent_keyed and not group.bind_admin_only and not group.admin_only
    assert "ctem" in AGENT_KEYED_BUILTIN_TOOL_IDS
    defs, executor = build_ctem_tools(user_id="", agent_id="")
    assert tuple(d.name for d in defs) == ALL
    assert all(d.source == "builtin:ctem" for d in defs)
    assert all(executor.handler(name) is not None for name in ALL)


def test_offered_when_bound_in_xtm_one_mode_only(monkeypatch: pytest.MonkeyPatch, xtm_one: None) -> None:
    assert offers_ctem_tools(["ctem"])
    assert not offers_ctem_tools(["ctem_read"])
    assert not offers_ctem_tools(None)
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: True), raising=False)
    assert not offers_ctem_tools(["ctem"])


def test_a_custom_agent_bound_to_the_group_gets_every_tool(xtm_one: None) -> None:
    wired = ctem_tools_for(_agent(), ["ctem"], user_id=str(uuid.uuid4()))
    assert wired is not None
    assert tuple(d.name for d in wired[0]) == ALL


def test_not_wired_without_a_person_or_without_the_binding(xtm_one: None) -> None:
    assert ctem_tools_for(_agent(), ["ctem"], user_id="") is None
    assert ctem_tools_for(_agent(), ["web_fetch"], user_id=str(uuid.uuid4())) is None


def test_no_function_is_offered_twice(xtm_one: None) -> None:
    # The read group, wired first, keeps its reads.
    wired = ctem_tools_for(_agent(), ["ctem_read", "ctem"], user_id=str(uuid.uuid4()))
    assert wired is not None
    defs, executor = wired
    names = {d.name for d in defs}
    assert not names & {"list_ctem_signals", "list_ctem_cases", "get_ctem_case"}
    assert executor.handler("list_ctem_cases") is None
    assert {"update_ctem_case", "record_ctem_signal", TRIAGE_TOOL} <= names
    # A built-in agent carrying both suites by slug only gains the triage tool.
    orchestrator = _agent("ctem-vuln-prioritization-orchestrator", builtin=True)
    wired = ctem_tools_for(orchestrator, ["ctem"], user_id=str(uuid.uuid4()))
    assert wired is not None
    assert [d.name for d in wired[0]] == [TRIAGE_TOOL]


class _Db:
    def __init__(self, user: Any) -> None:
        self.user = user
        self.commits = 0

    async def get(self, _model: Any, _key: Any) -> Any:
        return self.user

    async def commit(self) -> None:
        self.commits += 1

    async def __aenter__(self) -> _Db:
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        return None


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    state = SimpleNamespace(db=None, level=None, strict=False, selected=[], triaged=[])

    async def _primary(_db: Any) -> Any:
        return SimpleNamespace(id=ORG)

    async def _level(_db: Any, _user: Any) -> Any:
        return state.level

    async def _group_right(_db: Any, _person: Any) -> Any:
        return state.level if state.level in ("view", "decide") else None

    async def _select(_db: Any, **kw: Any) -> list[Any]:
        state.selected.append(kw)
        return [SimpleNamespace(id=uuid.uuid4())]

    async def _triage(_db: Any, rows: list[Any], **kw: Any) -> dict[str, int]:
        state.triaged.append((len(rows), kw))
        return {"changed": len(rows), "skipped": 0}

    monkeypatch.setattr(database, "async_session", lambda: state.db)
    monkeypatch.setattr(organizations, "get_primary", _primary)
    monkeypatch.setattr(ctem_access, "level", _level)
    monkeypatch.setattr(ctem_access, "group_right", _group_right)
    # Strict admin access for agent tools (or the Platform Health reach) keeps an administrator to their own.
    monkeypatch.setattr(
        helpers, "agent_tool_admin_bypass", lambda u: bool(getattr(u, "is_admin", False)) and not state.strict
    )
    monkeypatch.setattr(signals, "select_signals", _select)
    monkeypatch.setattr(scope_gate, "triage", _triage)
    return state


async def _call(user_id: str, **args: Any) -> str:
    _, executor = build_ctem_tools(user_id=user_id, agent_id=str(uuid.uuid4()))
    result = await executor.execute_tool(ToolCall(id="t1", name=TRIAGE_TOOL, arguments=args))
    return str(result.content)


async def test_triage_with_a_view_right_is_refused_and_writes_nothing(world: SimpleNamespace) -> None:
    person = uuid.uuid4()
    world.db = _Db(SimpleNamespace(id=person, is_admin=False, name="Viewer"))
    world.level = "view"
    answer = await _call(str(person), action="dismiss", ids=[str(uuid.uuid4())])
    assert answer.startswith("Refused:") and "Nothing was changed" in answer
    assert world.triaged == [] and world.db.commits == 0


@pytest.mark.parametrize(("level", "recorded_by_self"), [("decide", False), ("admin", False), (None, True)])
async def test_triage_reaches_the_organization_with_decide_else_own_signals(
    world: SimpleNamespace, level: str | None, recorded_by_self: bool
) -> None:
    person = uuid.uuid4()
    user = SimpleNamespace(id=person, is_admin=level == "admin", name="Decider")
    world.db = _Db(user)
    world.level = level
    answer = json.loads(await _call(str(person), action="dismiss", state="pending"))
    assert answer == {"matched": 1, "counts": {"changed": 1, "skipped": 0}}
    assert world.selected[0]["recorded_by"] == (str(person) if recorded_by_self else None)
    _count, kw = world.triaged[0]
    assert kw["action"] == "dismiss" and kw["actor"] == "Decider" and kw["evaluator_id"] == person
    assert world.db.commits == 1


async def test_triage_keeps_an_administrator_under_strict_agent_access_to_their_own(world: SimpleNamespace) -> None:
    # Same reach as list_ctem_signals: no organization-wide triage the agent could not list.
    person = uuid.uuid4()
    world.db = _Db(SimpleNamespace(id=person, is_admin=True, name="Admin"))
    world.level = "admin"
    world.strict = True
    answer = json.loads(await _call(str(person), action="dismiss", state="pending"))
    assert answer["matched"] == 1
    assert world.selected[0]["recorded_by"] == str(person)


async def test_triage_checks_its_arguments_before_any_read(world: SimpleNamespace) -> None:
    person = str(uuid.uuid4())
    assert (await _call(person, action="purge", ids=["x"])).startswith("action must be one of")
    assert "ids must be" in await _call(person, action="dismiss", ids=["not-a-uuid"])
    assert "either ids or a filter" in await _call(person, action="dismiss")
    assert "either ids or a filter" in await _call(person, action="dismiss", ids=[str(uuid.uuid4())], state="pending")
    assert world.selected == []
