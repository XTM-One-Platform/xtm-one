"""The fleet scope of the coordination reads.

A fleet agent's ``list_work_items`` used to read every item its user can see,
other fleets' included. It now reads its fleet by default, ``own`` on demand,
and ``all`` only for an administrator's agent; ``get_work_item`` and
``claim_work_item`` refuse an item outside the fleet. An agent in no fleet
reads as before.
"""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import app.services.work_item as wi_service
from app.services import coordination_scope
from app.services.builtin_tools import build_work_item_tools
from app.services.coordination_scope import CoordinationScope, resolve_coordination_scope
from app.tools.base import ToolCall

FLEET = uuid.uuid4()
WORKSPACE = uuid.uuid4()
ME = uuid.uuid4()
PEER = uuid.uuid4()
STRANGER = uuid.uuid4()


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


async def _call(executor, name, /, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


def _in_fleet(monkeypatch, *, admin: bool = False):
    async def home(agent_id):
        return (FLEET, "CEO Office", (ME, PEER), WORKSPACE) if agent_id in (ME, PEER) else None

    async def is_admin(_user_id):
        return admin

    monkeypatch.setattr(coordination_scope, "read_home_fleet", home)
    monkeypatch.setattr(coordination_scope, "user_is_admin", is_admin)


def _item(**agents) -> dict:
    base = {
        "id": str(uuid.uuid4()),
        "title": "x",
        "status": "open",
        "priority": "medium",
        "owner_agent_id": None,
        "created_by_agent_id": None,
        "claimed_by_agent_id": None,
        "workspace_id": None,
        "correlation_key": None,
        "is_stale": False,
        "description": "",
    }
    base.update({k: (str(v) if v else None) for k, v in agents.items()})
    return base


# ── Resolution ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_fleet_agent_reads_its_fleet_by_default(monkeypatch):
    _in_fleet(monkeypatch)
    scope = await resolve_coordination_scope(str(ME), str(uuid.uuid4()), "")
    assert scope.name == "fleet"
    assert scope.label() == "Scope fleet: CEO Office, 2 member(s)."


@pytest.mark.asyncio
async def test_an_agent_in_no_fleet_reads_as_before(monkeypatch):
    _in_fleet(monkeypatch)
    scope = await resolve_coordination_scope(str(STRANGER), str(uuid.uuid4()), "")
    assert scope.name == "all"
    assert scope.work_item_clause() is None
    assert "agent in no fleet" in scope.label()
    refused = await resolve_coordination_scope(str(STRANGER), None, "fleet")
    assert isinstance(refused, str) and "in none" in refused


@pytest.mark.asyncio
async def test_scope_all_is_for_an_administrators_agent(monkeypatch):
    _in_fleet(monkeypatch, admin=False)
    refused = await resolve_coordination_scope(str(ME), str(uuid.uuid4()), "all")
    assert isinstance(refused, str) and "administrator" in refused
    _in_fleet(monkeypatch, admin=True)
    scope = await resolve_coordination_scope(str(ME), str(uuid.uuid4()), "ALL")
    assert scope.name == "all"
    assert scope.label() == "Scope all: every item your user can see."


@pytest.mark.asyncio
async def test_an_unknown_scope_is_refused(monkeypatch):
    _in_fleet(monkeypatch)
    refused = await resolve_coordination_scope(str(ME), None, "team")
    assert isinstance(refused, str) and "fleet, own, all" in refused


# ── Clauses and admission ─────────────────────────────────────────────────


def test_the_fleet_clause_names_owner_creator_claimant_and_the_unowned_filed_items():
    sql = _sql(CoordinationScope("fleet", ME, FLEET, "f", (ME, PEER), WORKSPACE).work_item_clause())
    for column in ("owner_agent_id", "created_by_agent_id", "claimed_by_agent_id"):
        assert f"work_items.{column} IN ('{ME}', '{PEER}')" in sql
    assert f"work_items.workspace_id = '{WORKSPACE}' AND work_items.owner_agent_id IS NULL" in sql


def test_the_own_clause_names_owner_and_creator_only():
    sql = _sql(CoordinationScope("own", ME).work_item_clause())
    assert sql == f"work_items.owner_agent_id = '{ME}' OR work_items.created_by_agent_id = '{ME}'"


def test_admission_follows_the_clause():
    fleet = CoordinationScope("fleet", ME, FLEET, "f", (ME, PEER), WORKSPACE)
    assert fleet.admits(_item(owner_agent_id=PEER))
    assert fleet.admits(_item(owner_agent_id=STRANGER, created_by_agent_id=ME))
    assert fleet.admits(_item(owner_agent_id=STRANGER, claimed_by_agent_id=PEER))
    assert fleet.admits(_item(workspace_id=WORKSPACE))
    assert not fleet.admits(_item(owner_agent_id=STRANGER, workspace_id=WORKSPACE))
    assert not fleet.admits(_item(owner_agent_id=STRANGER, created_by_agent_id=STRANGER))
    own = CoordinationScope("own", ME)
    assert own.admits(_item(owner_agent_id=ME))
    assert not own.admits(_item(owner_agent_id=PEER, claimed_by_agent_id=ME))


# ── The tools ─────────────────────────────────────────────────────────────


def _store(monkeypatch, rows: list[dict], calls: dict):
    async def _list(**kwargs):
        calls.setdefault("list", []).append(kwargs)
        return rows

    async def _count(**kwargs):
        return len(rows)

    async def _get(work_item_id, *, user_id, reader=None):
        return next((r for r in rows if r["id"] == work_item_id), None)

    async def _claim(work_item_id, **kwargs):
        calls.setdefault("claim", []).append(work_item_id)
        return {"claimed": True, "work_item": {"id": work_item_id}}

    monkeypatch.setattr(wi_service, "list_work_items", _list)
    monkeypatch.setattr(wi_service, "count_work_items", _count)
    monkeypatch.setattr(wi_service, "get_work_item", _get)
    monkeypatch.setattr(wi_service, "claim_work_item", _claim)


@pytest.mark.asyncio
async def test_list_without_scope_narrows_a_fleet_agent_and_says_so(monkeypatch):
    _in_fleet(monkeypatch)
    calls: dict = {}
    _store(monkeypatch, [_item(owner_agent_id=PEER)], calls)
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(ME))
    result = await _call(executor, "list_work_items", open_only=True)
    assert result.content.splitlines()[0].endswith("Scope fleet: CEO Office, 2 member(s).")
    assert "claimed_by_agent_id IN" in _sql(calls["list"][0]["scope"])


@pytest.mark.asyncio
async def test_list_scope_all_for_an_admin_adds_no_clause(monkeypatch):
    _in_fleet(monkeypatch, admin=True)
    calls: dict = {}
    _store(monkeypatch, [_item(owner_agent_id=STRANGER)], calls)
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(ME))
    result = await _call(executor, "list_work_items", scope="all")
    assert calls["list"][0]["scope"] is None
    assert result.content.splitlines()[0].endswith("Scope all: every item your user can see.")


@pytest.mark.asyncio
async def test_list_scope_all_refused_for_a_member_agent(monkeypatch):
    _in_fleet(monkeypatch, admin=False)
    calls: dict = {}
    _store(monkeypatch, [], calls)
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(ME))
    result = await _call(executor, "list_work_items", scope="all")
    assert "administrator" in result.content
    assert "list" not in calls


@pytest.mark.asyncio
async def test_get_and_claim_refuse_an_item_outside_the_fleet(monkeypatch):
    _in_fleet(monkeypatch, admin=False)
    foreign = _item(owner_agent_id=STRANGER, created_by_agent_id=STRANGER)
    calls: dict = {}
    _store(monkeypatch, [foreign], calls)
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(ME))
    read = await _call(executor, "get_work_item", work_item_id=foreign["id"])
    assert "not in your fleet (CEO Office)" in read.content
    claim = await _call(executor, "claim_work_item", work_item_id=foreign["id"])
    assert "not in your fleet" in claim.content and "Nothing was claimed" in claim.content
    assert "claim" not in calls


@pytest.mark.asyncio
async def test_an_admin_reads_a_foreign_item_but_never_claims_it(monkeypatch):
    _in_fleet(monkeypatch, admin=True)
    foreign = _item(owner_agent_id=STRANGER)
    calls: dict = {}
    _store(monkeypatch, [foreign], calls)
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(ME))
    read = await _call(executor, "get_work_item", work_item_id=foreign["id"])
    assert "not in your fleet" not in read.content
    claim = await _call(executor, "claim_work_item", work_item_id=foreign["id"])
    assert "not in your fleet" in claim.content
    assert "claim" not in calls


@pytest.mark.asyncio
async def test_a_fleet_item_is_read_and_claimed(monkeypatch):
    _in_fleet(monkeypatch)
    mine = _item(owner_agent_id=ME)
    calls: dict = {}
    _store(monkeypatch, [mine], calls)
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(ME))
    claim = await _call(executor, "claim_work_item", work_item_id=mine["id"])
    assert "Claimed work item" in claim.content
    assert calls["claim"] == [mine["id"]]


@pytest.mark.asyncio
async def test_an_agent_in_no_fleet_lists_gets_and_claims_as_before(monkeypatch):
    _in_fleet(monkeypatch)
    foreign = _item(owner_agent_id=PEER)
    calls: dict = {}
    _store(monkeypatch, [foreign], calls)
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(STRANGER))
    listed = await _call(executor, "list_work_items")
    assert calls["list"][0]["scope"] is None
    assert "agent in no fleet" in listed.content.splitlines()[0]
    claim = await _call(executor, "claim_work_item", work_item_id=foreign["id"])
    assert "Claimed work item" in claim.content


def test_the_tool_schema_declares_scope():
    defs, _ = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    schema = next(d for d in defs if d.name == "list_work_items").input_schema
    assert schema["properties"]["scope"]["enum"] == ["", "fleet", "own", "all"]


# ── list_shared_state: the same scope, one implementation ──


def test_the_shared_state_fleet_clause_names_member_owners_and_unowned_filed_entries():
    sql = _sql(CoordinationScope("fleet", ME, FLEET, "f", (ME, PEER), WORKSPACE).shared_state_clause())
    assert f"shared_states.owner_agent_id IN ('{ME}', '{PEER}')" in sql
    assert f"shared_states.workspace_id = '{WORKSPACE}' AND shared_states.owner_agent_id IS NULL" in sql


def test_the_shared_state_own_clause_names_the_owner_and_all_adds_none():
    assert _sql(CoordinationScope("own", ME).shared_state_clause()) == f"shared_states.owner_agent_id = '{ME}'"
    assert CoordinationScope("all", ME).shared_state_clause() is None


def _state_store(monkeypatch, rows: list[dict], calls: dict):
    import app.services.shared_state as ss_service

    async def _list(**kwargs):
        calls.setdefault("list", []).append(kwargs)
        return rows

    async def _count(**kwargs):
        calls.setdefault("count", []).append(kwargs)
        return len(rows)

    monkeypatch.setattr(ss_service, "list_states", _list)
    monkeypatch.setattr(ss_service, "count_states", _count)


def _shared_state_tools(agent):
    from app.services.builtin_tools import build_shared_state_tools

    return build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(agent))


@pytest.mark.asyncio
async def test_list_shared_state_without_scope_narrows_a_fleet_agent_and_says_so(monkeypatch):
    _in_fleet(monkeypatch)
    calls: dict = {}
    _state_store(monkeypatch, [{"key": "dev:open-prs", "label": None, "status": "active", "is_stale": False}], calls)
    _, executor = _shared_state_tools(ME)
    result = await _call(executor, "list_shared_state")
    assert result.content.splitlines()[0].endswith("Scope fleet: CEO Office, 2 member(s).")
    assert "shared_states.owner_agent_id IN" in _sql(calls["list"][0]["scope"])


@pytest.mark.asyncio
async def test_list_shared_state_own_and_all_scopes(monkeypatch):
    _in_fleet(monkeypatch, admin=False)
    calls: dict = {}
    _state_store(monkeypatch, [], calls)
    _, executor = _shared_state_tools(ME)
    own = await _call(executor, "list_shared_state", scope="own")
    assert own.content.endswith("Scope own: entries you own.")
    assert _sql(calls["list"][0]["scope"]) == f"shared_states.owner_agent_id = '{ME}'"
    refused = await _call(executor, "list_shared_state", scope="all")
    assert "administrator" in refused.content
    assert len(calls["list"]) == 1
    _in_fleet(monkeypatch, admin=True)
    everything = await _call(executor, "list_shared_state", scope="all")
    assert calls["list"][1]["scope"] is None
    assert everything.content.endswith("Scope all: every entry your user can see.")


@pytest.mark.asyncio
async def test_list_shared_state_for_an_agent_in_no_fleet_reads_as_before(monkeypatch):
    _in_fleet(monkeypatch)
    calls: dict = {}
    _state_store(monkeypatch, [], calls)
    _, executor = _shared_state_tools(STRANGER)
    result = await _call(executor, "list_shared_state")
    assert calls["list"][0]["scope"] is None
    assert "agent in no fleet" in result.content


# ── The fleet Operations tab counts what the agents read ──


def test_the_operations_work_items_fleet_filter_is_the_scope_fleet_population():
    from app.api.v1 import work_items as work_items_api

    sql = _sql(work_items_api._workspace_scope(None, str(FLEET)))
    for column in ("owner_agent_id", "created_by_agent_id", "claimed_by_agent_id"):
        assert f"work_items.{column} IN (SELECT fleet_members.agent_id" in sql
    assert "work_items.workspace_id = (SELECT fleets.workspace_id" in sql
    assert "work_items.owner_agent_id IS NULL" in sql
    assert FLEET.hex in sql.replace("-", "")


def test_the_operations_shared_state_fleet_filter_is_the_scope_fleet_population():
    from app.api.v1 import shared_state as shared_state_api

    sql = _sql(shared_state_api._workspace_scope(None, str(FLEET)))
    assert "shared_states.owner_agent_id IN (SELECT fleet_members.agent_id" in sql
    assert "shared_states.workspace_id = (SELECT fleets.workspace_id" in sql
    assert "shared_states.owner_agent_id IS NULL" in sql


@pytest.mark.parametrize("value", ["not-a-uuid", "none"])
def test_the_operations_fleet_filter_refuses_a_malformed_id(value):
    from fastapi import HTTPException

    from app.api.v1 import shared_state as shared_state_api
    from app.api.v1 import work_items as work_items_api

    for scope in (work_items_api._workspace_scope, shared_state_api._workspace_scope):
        with pytest.raises(HTTPException) as exc:
            scope(None, value)
        assert exc.value.status_code == 422


def test_the_shared_state_tool_schema_declares_scope():
    defs, _ = _shared_state_tools(ME)
    schema = next(d for d in defs if d.name == "list_shared_state").input_schema
    assert schema["properties"]["scope"]["enum"] == ["", "fleet", "own", "all"]
