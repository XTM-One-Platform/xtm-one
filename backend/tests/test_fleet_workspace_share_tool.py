"""``share_fleet_workspace``: a fleet and its workspace shared from an agent tool (#24).

What is pinned here, without a database: the gate (an administrator, or the
owner of the fleet's workspace; anyone else refused, nothing written), one
grant written on both the fleet and its workspace through the REST routes'
writers, a removal, the listing, the refusal of an unknown account, and the
audit entry of each entity naming the actor, the acting agent, the fleet, the
workspace, the permission and the people added or removed.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services.builtin_tools import fleets as fleet_tools
from app.services.share_principals import Principal, ResolvedPrincipals


class _Result:
    def __init__(self, value) -> None:
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _Session:
    def __init__(self, workspace) -> None:
        self.workspace = workspace
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, _query):
        return _Result(self.workspace)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _entity(owner_id: uuid.UUID, name: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        slug=name.lower(),
        user_id=owner_id,
        is_company_managed=False,
        access_restricted=False,
        is_builtin=False,
        shared_user_ids=None,
        shared_user_permissions=None,
        shared_group_ids=None,
        shared_group_permissions=None,
    )


@pytest.fixture
def world(monkeypatch):
    """A caller, a fleet and its workspace both owned by *owner*, and the writers recorded."""
    from app.api.v1 import helpers
    from app.services import audit, share_principals

    owner_id = uuid.uuid4()
    workspace = _entity(owner_id, "Ops")
    fleet = _entity(owner_id, "Fleet")
    fleet.workspace_id = workspace.id
    session = _Session(workspace)
    state = SimpleNamespace(
        caller=SimpleNamespace(id=owner_id, is_admin=False, email="owner@example.com"),
        fleet=fleet,
        workspace=workspace,
        session=session,
        audits=[],
        bob=Principal("user", str(uuid.uuid4()), "Bob <bob@example.com>"),
        unknown=[],
    )

    @asynccontextmanager
    async def _factory():
        yield session

    async def _load_caller(_db, _uid):
        return state.caller

    async def _manageable(_db, _model, _uid, _entity_uuid, **_kw):
        return fleet, None

    async def _resolve(_db, users, groups=()):
        resolved = ResolvedPrincipals()
        for entry in users:
            if entry == "bob@example.com":
                resolved.users.append(state.bob)
            else:
                resolved.unknown_users.append(entry)
        return resolved

    async def _apply_users(_db, entity, body, *, partial):
        if "shared_user_ids" in body.model_fields_set:
            entity.shared_user_ids = body.shared_user_ids
            entity.shared_user_permissions = body.shared_user_permissions

    async def _apply_groups(_db, entity, body, *, partial):
        if "shared_group_ids" in body.model_fields_set:
            entity.shared_group_ids = body.shared_group_ids
            entity.shared_group_permissions = body.shared_group_permissions

    async def _log_audit(_db, **kwargs):
        state.audits.append(kwargs)

    async def _load_principals(_db, _entity):
        from app.services.share_principals import PrincipalIndex

        index = PrincipalIndex()
        index.add([state.bob])
        return index

    monkeypatch.setattr(fleet_tools.database, "async_session", _factory)
    monkeypatch.setattr(fleet_tools._builder, "_load_caller_user_for_builder", _load_caller)
    monkeypatch.setattr(fleet_tools._builder, "_load_manageable_entity_for_builder", _manageable)
    monkeypatch.setattr(share_principals, "resolve_principals", _resolve)
    monkeypatch.setattr(share_principals, "load_principals", _load_principals)
    monkeypatch.setattr(helpers, "apply_user_share_fields", _apply_users)
    monkeypatch.setattr(helpers, "apply_group_share_fields", _apply_groups)
    monkeypatch.setattr(audit, "log_audit", _log_audit)
    return state


async def _call(state, agent_id: str | None = None, **args) -> str:
    _defs, executor = fleet_tools.build_fleet_tools(str(state.caller.id), agent_id=agent_id)
    return await executor.handler("share_fleet_workspace")(**args)


def test_the_tool_is_in_the_platform_group():
    definitions, _executor = fleet_tools.build_fleet_tools("")
    tool = next(d for d in definitions if d.name == "share_fleet_workspace")
    assert tool.source == "builtin:platform"
    assert tool.input_schema["properties"]["permission"]["enum"] == ["view", "edit"]
    assert tool.input_schema["required"] == ["fleet_id"]


@pytest.mark.asyncio
async def test_a_caller_who_is_neither_admin_nor_workspace_owner_is_refused(world):
    # A config grantee of the fleet who does not own its workspace: managing
    # the fleet is not enough to hand out access to the workspace.
    world.caller = SimpleNamespace(id=uuid.uuid4(), is_admin=False, email="grantee@example.com")
    answer = await _call(world, fleet_id=str(world.fleet.id), users=["bob@example.com"])
    assert answer == fleet_tools._SHARE_NOT_ALLOWED
    assert world.fleet.shared_user_ids is None
    assert world.workspace.shared_user_ids is None
    assert world.audits == []
    assert (world.session.commits, world.session.rollbacks) == (0, 1)


@pytest.mark.asyncio
async def test_the_owner_shares_the_fleet_and_its_workspace_in_one_grant(world):
    agent_id = str(uuid.uuid4())
    answer = await _call(world, agent_id=agent_id, fleet_id=str(world.fleet.id), users=["bob@example.com"])
    for entity in (world.fleet, world.workspace):
        assert entity.shared_user_ids == [world.bob.id]
        assert entity.shared_user_permissions == {world.bob.id: "read_only"}
    assert "Bob <bob@example.com>" in answer
    assert f"/fleets/{world.fleet.id}" in answer
    assert world.session.commits == 1
    # One audit entry per entity, naming who, through which agent, what and where.
    assert [a["entity_type"] for a in world.audits] == ["fleet", "workspace"]
    for entry in world.audits:
        assert entry["user"] is world.caller
        details = entry["details"]
        assert details["via"] == "agent"
        assert details["agent_id"] == agent_id
        assert details["action"] == "add"
        assert details["permission"] == "read_only"
        assert details["added_users"] == [world.bob.id]
        assert details["fleet_id"] == str(world.fleet.id)
        assert details["workspace_id"] == str(world.workspace.id)


@pytest.mark.asyncio
async def test_an_admin_shares_with_edit(world):
    world.caller = SimpleNamespace(id=uuid.uuid4(), is_admin=True, email="admin@example.com")
    await _call(world, fleet_id=str(world.fleet.id), users=["bob@example.com"], permission="edit")
    assert world.workspace.shared_user_permissions == {world.bob.id: "config"}
    assert world.fleet.shared_user_permissions == {world.bob.id: "config"}
    assert world.audits[0]["details"]["permission"] == "config"


@pytest.mark.asyncio
async def test_unshare_removes_the_grant_on_both(world):
    for entity in (world.fleet, world.workspace):
        entity.shared_user_ids = [world.bob.id]
        entity.shared_user_permissions = {world.bob.id: "read_only"}
    await _call(world, fleet_id=str(world.fleet.id), action="remove", users=["bob@example.com"])
    assert world.fleet.shared_user_ids is None
    assert world.workspace.shared_user_ids is None
    assert [a["details"]["removed_users"] for a in world.audits] == [[world.bob.id], [world.bob.id]]
    assert world.session.commits == 1


@pytest.mark.asyncio
async def test_list_names_who_has_them_and_writes_nothing(world):
    world.workspace.shared_user_ids = [world.bob.id]
    answer = await _call(world, fleet_id=str(world.fleet.id), action="list")
    assert "Who has fleet **Fleet** and its workspace **Ops**" in answer
    assert "Bob <bob@example.com>" in answer
    assert world.audits == []
    assert world.session.commits == 0


@pytest.mark.asyncio
async def test_an_unknown_account_refuses_the_whole_call(world):
    answer = await _call(world, fleet_id=str(world.fleet.id), users=["bob@example.com", "eve@example.com"])
    assert '"eve@example.com"' in answer
    assert "Nothing was changed" in answer
    assert world.fleet.shared_user_ids is None
    assert world.audits == []
    assert (world.session.commits, world.session.rollbacks) == (0, 1)


@pytest.mark.asyncio
async def test_a_company_managed_workspace_is_refused(world):
    world.workspace.is_company_managed = True
    answer = await _call(world, fleet_id=str(world.fleet.id), users=["bob@example.com"])
    assert "company-managed" in answer
    assert world.audits == []


@pytest.mark.asyncio
async def test_arguments_are_checked_before_anything_is_read(world):
    assert "action must be" in await _call(world, fleet_id=str(world.fleet.id), action="publish")
    assert "permission must be" in await _call(
        world, fleet_id=str(world.fleet.id), users=["bob@example.com"], permission="owner"
    )
    assert "Name at least one" in await _call(world, fleet_id=str(world.fleet.id))
    assert world.session.commits == 0
