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

"""#1778: the ``ctem_repair`` group, the CTEM administrator repairs from an agent."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.core import database
from app.core.config import settings
from app.services import ctem_attack_surface, ctem_threat_package_live, organizations
from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS
from app.services.builtin_tools import (
    AGENT_KEYED_BUILTIN_TOOL_IDS,
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

    async def _hosts(_db: Any, org: Any, *, dry_run: bool = True) -> dict[str, Any]:
        state.calls.append(("hosts", org, dry_run))
        return {"discarded": 2, "signal_ids": ["s1", "s2"]}

    async def _packages(_db: Any, org: Any, user_id: str, *, dry_run: bool = True) -> dict[str, Any]:
        state.calls.append(("packages", org, user_id, dry_run))
        return {"corrected": 1, "case_ids": ["c1"]}

    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(organizations, "get_primary", _primary)
    monkeypatch.setattr(ctem_attack_surface, "list_passes", _passes)
    monkeypatch.setattr(ctem_attack_surface, "pass_to_dict", lambda row: {"id": row.id})
    monkeypatch.setattr(ctem_attack_surface, "repair_partial_read_new_hosts", _hosts)
    monkeypatch.setattr(ctem_threat_package_live, "repair_stored_packages", _packages)
    return state


def _tools(user_id: str) -> Any:
    _, executor = build_ctem_repair_tools(user_id=user_id)
    return executor


def test_the_group_is_registered_agent_keyed_with_three_tools() -> None:
    group = next(g for g in BUILTIN_TOOL_GROUPS if g.id == "ctem_repair")
    assert group.agent_keyed
    assert "ctem_repair" in AGENT_KEYED_BUILTIN_TOOL_IDS
    defs, _ = build_ctem_repair_tools(user_id="")
    assert tuple(d.name for d in defs) == CTEM_REPAIR_TOOL_NAMES


def test_offered_to_the_ctem_case_agents_and_when_bound(monkeypatch: pytest.MonkeyPatch, xtm_one: None) -> None:
    assert offers_ctem_repair_tools(_agent(), ["ctem_repair"])
    assert offers_ctem_repair_tools(_agent("ctem-threat-response-orchestrator", builtin=True), [])
    assert not offers_ctem_repair_tools(_agent("ctem-threat-response-orchestrator", builtin=False), [])
    assert not offers_ctem_repair_tools(_agent(), ["ctem_read"])
    assert ctem_repair_tools_for(_agent(), ["ctem_repair"], user_id="") is None
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: True), raising=False)
    assert not offers_ctem_repair_tools(_agent(), ["ctem_repair"])


async def test_a_dry_run_is_the_default_and_answers_counts_and_ids(world: SimpleNamespace) -> None:
    admin = SimpleNamespace(id=uuid.uuid4(), is_admin=True)
    world.db = _Db(admin)
    executor = _tools(str(admin.id))
    hosts = json.loads(await executor.handler("repair_ctem_attack_surface_new_hosts")())
    packages = json.loads(await executor.handler("repair_ctem_threat_packages")())
    assert hosts == {"discarded": 2, "signal_ids": ["s1", "s2"], "dry_run": True}
    assert packages == {"corrected": 1, "case_ids": ["c1"], "dry_run": True}
    assert ("hosts", ORG, True) in world.calls
    assert ("packages", ORG, str(admin.id), True) in world.calls


async def test_apply_passes_dry_run_false_and_the_pass_summary_reads(world: SimpleNamespace) -> None:
    admin = SimpleNamespace(id=uuid.uuid4(), is_admin=True)
    world.db = _Db(admin)
    executor = _tools(str(admin.id))
    await executor.handler("repair_ctem_threat_packages")(dry_run=False)
    assert ("packages", ORG, str(admin.id), False) in world.calls
    passes = json.loads(await executor.handler("list_ctem_attack_surface_passes")(limit=500))
    assert passes == {"passes": [{"id": "p1"}], "count": 1}
    assert ("passes", ORG, ctem_attack_surface.MAX_PASSES) in world.calls


async def test_a_non_administrator_gets_the_fact_and_nothing_runs(world: SimpleNamespace) -> None:
    member = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    world.db = _Db(member)
    executor = _tools(str(member.id))
    for name in CTEM_REPAIR_TOOL_NAMES:
        answer = await executor.handler(name)()
        assert answer.startswith("Not run: these CTEM repairs are for an administrator")
    assert world.calls == []
