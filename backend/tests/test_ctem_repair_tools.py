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

"""#1778, #2443: the CTEM attack surface passes read (the former ``ctem_repair`` group is renamed into ``ctem``)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.core import database
from app.core.config import settings
from app.services import ctem_attack_surface, organizations
from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS
from app.services.builtin_tools import (
    CTEM_REPAIR_TOOL_NAMES,
    build_ctem_repair_tools,
    ctem_repair_tools_for,
    offers_ctem_repair_tools,
)

ORG = uuid.uuid4()


@pytest.fixture
def xtm_one(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)


def _agent(slug: str = "my-agent", builtin: bool = False) -> Any:
    return SimpleNamespace(id=uuid.uuid4(), slug=slug, is_builtin=builtin)


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
    state = SimpleNamespace(db=None, calls=[])

    def _session() -> Any:
        return state.db

    async def _primary(_db: Any) -> Any:
        return SimpleNamespace(id=ORG)

    async def _passes(_db: Any, org: Any, *, limit: int = 10) -> list[Any]:
        state.calls.append(("passes", org, limit))
        return [SimpleNamespace(id="p1")]

    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(organizations, "get_primary", _primary)
    monkeypatch.setattr(ctem_attack_surface, "list_passes", _passes)
    monkeypatch.setattr(ctem_attack_surface, "pass_to_dict", lambda row: {"id": row.id})
    return state


def _tools(user_id: str) -> Any:
    _, executor = build_ctem_repair_tools(user_id=user_id)
    return executor


def test_only_the_passes_read_is_left_on_the_agent_surface() -> None:
    """#2443: the two one-off repairs left the agent surface (they stay admin REST routes)."""
    assert not {"ctem_repair", "ctem_read"} & {g.id for g in BUILTIN_TOOL_GROUPS}
    defs, _ = build_ctem_repair_tools(user_id="")
    assert tuple(d.name for d in defs) == CTEM_REPAIR_TOOL_NAMES == ("list_ctem_attack_surface_passes",)
    assert all(d.source == "builtin:ctem" for d in defs)


def test_offered_by_slug_to_the_ctem_case_agents_not_bound_to_ctem(
    monkeypatch: pytest.MonkeyPatch, xtm_one: None
) -> None:
    case_agent = _agent("ctem-threat-response-orchestrator", builtin=True)
    assert offers_ctem_repair_tools(case_agent, [])
    assert not offers_ctem_repair_tools(_agent("ctem-threat-response-orchestrator", builtin=False), [])
    # Bound to ctem (or a former id): the group serves it, never twice.
    assert not offers_ctem_repair_tools(case_agent, ["ctem"])
    assert not offers_ctem_repair_tools(_agent(), ["ctem_repair"])
    assert ctem_repair_tools_for(case_agent, [], user_id="") is None
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: True), raising=False)
    assert not offers_ctem_repair_tools(case_agent, [])


async def test_the_pass_summary_reads_for_an_administrator(world: SimpleNamespace) -> None:
    admin = SimpleNamespace(id=uuid.uuid4(), is_admin=True)
    world.db = _Db(admin)
    passes = json.loads(await _tools(str(admin.id)).handler("list_ctem_attack_surface_passes")(limit=500))
    assert passes == {"passes": [{"id": "p1"}], "count": 1}
    assert ("passes", ORG, ctem_attack_surface.MAX_PASSES) in world.calls


async def test_a_non_administrator_gets_the_fact_and_nothing_runs(world: SimpleNamespace) -> None:
    member = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    world.db = _Db(member)
    answer = await _tools(str(member.id)).handler("list_ctem_attack_surface_passes")()
    assert answer.startswith("Not run: these CTEM repairs are for an administrator")
    assert world.calls == []


async def test_an_empty_pass_list_says_why(world: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """#2331: no pass record answers why_empty instead of a bare empty list."""

    async def _none(_db: Any, _org: Any, *, limit: int = 10) -> list[Any]:
        return []

    async def _why(_db: Any, org: Any) -> str:
        world.calls.append(("why", org))
        return "the attack surface pass is turned off"

    monkeypatch.setattr(ctem_attack_surface, "list_passes", _none)
    monkeypatch.setattr(ctem_attack_surface, "why_no_pass", _why)
    admin = SimpleNamespace(id=uuid.uuid4(), is_admin=True)
    world.db = _Db(admin)
    passes = json.loads(await _tools(str(admin.id)).handler("list_ctem_attack_surface_passes")())
    assert passes == {"passes": [], "count": 0, "why_empty": "the attack surface pass is turned off"}
    assert ("why", ORG) in world.calls
