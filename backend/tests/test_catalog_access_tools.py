"""Owner transfer and sharing of skills and prompts from chat, without a database.

What these pin (``update_skill`` / ``update_prompt`` / ``get_skill`` / ``get_prompt``):

* the owner and sharing arguments are declared on both update tools, with
  ``binding_mode`` deciding how people and groups apply, as on ``update_agent``;
* an owner change is an administrator's: a non-admin is refused before any
  address is looked up, and nothing is committed;
* an administrator's transfer goes through the REST routes' own
  ``apply_owner_transfer`` (the ``transfer_ownership`` audit entry) and the
  entity type each route writes (``agent_skill``, ``prompt``);
* per-user grants go through ``apply_user_share_fields``; add, remove and
  clear are planned by ``plan_grants``; the owner named in ``users`` is
  reported and never stored; a company-managed entity is not shared with people;
* group grants are added, removed and cleared through ``normalize_group_grants``;
* a call without the new arguments answers as before;
* ``get_skill`` / ``get_prompt`` read back the owner and the audience.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest

import app.api.v1.helpers as api_helpers
import app.core.database as database
import app.services.builtin_tools as builtin_tools_module
import app.services.share_principals as share_principals
from app.services.builtin_tools import build_agent_builder_tool
from app.services.share_principals import Principal, ResolvedPrincipals
from app.tools.base import ToolCall


class _Result:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value

    def all(self):
        if self._value is None:
            return []
        return list(self._value) if isinstance(self._value, (list, tuple)) else [self._value]

    def scalars(self):
        return self


class _Session:
    def __init__(self, created, results):
        created.append(self)
        self._results = list(results)
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def execute(self, _stmt):
        return _Result(self._results.pop(0) if self._results else None)

    def add(self, _instance):
        pass

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _sessions(monkeypatch, results=()):
    created: list[_Session] = []
    monkeypatch.setattr(database, "async_session", lambda: _Session(created, results))
    return created


def _entity(**overrides):
    values = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "Method",
        "description": "d",
        "compatibility": None,
        "content": "# Method\nbody",
        "url": None,
        "tags": [],
        "enabled": True,
        "hide_from_chat": False,
        "is_company_managed": False,
        "access_restricted": False,
        "shared_user_ids": None,
        "shared_user_permissions": None,
        "shared_group_ids": None,
        "shared_group_permissions": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _manageable(monkeypatch, entity):
    async def _fake(_db, _model, _uid, _entity_uuid, *, action="modify", entity_label="entity", lock=False):
        return entity, None

    monkeypatch.setattr(builtin_tools_module, "_load_manageable_entity_for_builder", _fake)


def _caller(monkeypatch, *, is_admin):
    fake = SimpleNamespace(id=uuid.uuid4(), is_admin=is_admin, role="admin" if is_admin else "user")

    async def _fake(_db, _uid):
        return fake

    monkeypatch.setattr(builtin_tools_module, "_load_caller_user_for_builder", _fake)
    return fake


def _people(monkeypatch, *people: Principal, lookups: list | None = None):
    async def _resolve(_db, entries, groups=()):
        if lookups is not None:
            lookups.append(list(entries))
        known = {p.label.split("<")[-1].rstrip(">"): p for p in people}
        found = [known[e] for e in entries if e in known]
        unknown = [e for e in entries if e not in known]
        return ResolvedPrincipals(users=found, unknown_users=unknown)

    monkeypatch.setattr(share_principals, "resolve_principals", _resolve)


def _person(name: str, user_id: uuid.UUID | None = None) -> Principal:
    return Principal("user", str(user_id or uuid.uuid4()), f"{name.title()} <{name}@example.com>")


async def _call(name: str, **arguments) -> str:
    _, executor = build_agent_builder_tool(None, str(uuid.uuid4()))
    result = await executor.execute_tool(ToolCall(id="call", name=name, arguments=arguments))
    return result.content


TOOLS = [("update_skill", "skill_id", "agent_skill"), ("update_prompt", "prompt_id", "prompt")]


def test_the_owner_and_sharing_arguments_are_declared():
    definitions, executor = build_agent_builder_tool(None, str(uuid.uuid4()))
    by_name = {d.name: d for d in definitions}
    for tool, _id_arg, _type in TOOLS:
        properties = by_name[tool].input_schema["properties"]
        assert {
            "owner",
            "users",
            "users_permission",
            "shared_group_ids",
            "shared_group_permissions",
            "binding_mode",
        } <= set(properties)
        assert "administrator" in by_name[tool].description
        assert {"owner", "users", "binding_mode"} <= set(inspect.signature(executor.handler(tool)).parameters)


@pytest.mark.parametrize(("tool", "id_arg", "_type"), TOOLS)
async def test_a_non_admin_cannot_change_the_owner(monkeypatch, tool, id_arg, _type):
    entity = _entity()
    before = entity.user_id
    _manageable(monkeypatch, entity)
    _caller(monkeypatch, is_admin=False)
    lookups: list = []
    _people(monkeypatch, _person("ada"), lookups=lookups)
    created = _sessions(monkeypatch)

    result = await _call(tool, **{id_arg: str(entity.id)}, owner="ada@example.com")

    assert "Only an administrator can change the owner" in result and "Nothing was changed" in result
    assert entity.user_id == before
    assert lookups == []  # refused before any address is looked up
    assert all(session.commits == 0 for session in created)


@pytest.mark.parametrize(("tool", "id_arg", "entity_type"), TOOLS)
async def test_an_admin_transfers_the_owner_through_the_rest_helper(monkeypatch, tool, id_arg, entity_type):
    entity = _entity()
    ada = _person("ada")
    _manageable(monkeypatch, entity)
    admin = _caller(monkeypatch, is_admin=True)
    _people(monkeypatch, ada)
    created = _sessions(monkeypatch)
    calls: list[dict] = []

    async def _transfer(_db, *, actor, entity, new_owner_id, entity_type, entity_name, request=None):
        calls.append({"actor": actor, "new_owner_id": new_owner_id, "entity_type": entity_type})
        entity.user_id = uuid.UUID(new_owner_id)
        return True

    monkeypatch.setattr(api_helpers, "apply_owner_transfer", _transfer)

    result = await _call(tool, **{id_arg: str(entity.id)}, owner="ada@example.com")

    assert "updated: owner" in result and "Owner is now Ada <ada@example.com>" in result, result
    assert calls == [{"actor": admin, "new_owner_id": ada.id, "entity_type": entity_type}]
    assert str(entity.user_id) == ada.id
    assert sum(session.commits for session in created) == 1


@pytest.mark.parametrize(("tool", "id_arg", "_type"), TOOLS)
async def test_an_unknown_owner_is_refused(monkeypatch, tool, id_arg, _type):
    entity = _entity()
    _manageable(monkeypatch, entity)
    _caller(monkeypatch, is_admin=True)
    _people(monkeypatch)
    created = _sessions(monkeypatch)

    result = await _call(tool, **{id_arg: str(entity.id)}, owner="nobody@example.com")

    assert "no account on this platform matches" in result
    assert all(session.commits == 0 for session in created)


@pytest.mark.parametrize(("tool", "id_arg", "_type"), TOOLS)
async def test_people_are_added_then_removed_and_the_owner_is_never_stored(monkeypatch, tool, id_arg, _type):
    owner_id = uuid.uuid4()
    entity = _entity(user_id=owner_id)
    ada, bob, own = _person("ada"), _person("bob"), _person("own", owner_id)
    _manageable(monkeypatch, entity)
    _people(monkeypatch, ada, bob, own)
    _sessions(monkeypatch)
    writes: list = []

    async def _write(_db, target, body, *, partial):
        writes.append(body)
        target.shared_user_ids = body.shared_user_ids
        target.shared_user_permissions = body.shared_user_permissions

    monkeypatch.setattr(api_helpers, "apply_user_share_fields", _write)

    added = await _call(
        tool,
        **{id_arg: str(entity.id)},
        users="ada@example.com, bob@example.com, own@example.com",
        users_permission="manage",
        binding_mode="add",
    )
    assert "updated: users" in added and "[manage]" in added, added
    assert "Skipped, they own it: Own <own@example.com>" in added
    assert entity.shared_user_ids == [ada.id, bob.id]
    assert entity.shared_user_permissions == {ada.id: "config", bob.id: "config"}

    removed = await _call(tool, **{id_arg: str(entity.id)}, users=["bob@example.com"], binding_mode="remove")
    assert "Access removed: Bob <bob@example.com>" in removed, removed
    assert entity.shared_user_ids == [ada.id]
    assert len(writes) == 2


@pytest.mark.parametrize(("tool", "id_arg", "_type"), TOOLS)
async def test_clear_outside_replace_and_a_bad_permission_are_refused(monkeypatch, tool, id_arg, _type):
    entity = _entity()
    _manageable(monkeypatch, entity)
    created = _sessions(monkeypatch)

    clear = await _call(tool, **{id_arg: str(entity.id)}, users="clear", binding_mode="add")
    bad = await _call(tool, **{id_arg: str(entity.id)}, users="a@example.com", users_permission="owner")

    assert "only valid with binding_mode='replace'" in clear
    assert "Invalid users_permission" in bad
    assert entity.shared_user_ids is None
    assert all(session.commits == 0 for session in created)


@pytest.mark.parametrize(("tool", "id_arg", "_type"), TOOLS)
async def test_a_company_managed_entity_is_not_shared_with_people(monkeypatch, tool, id_arg, _type):
    entity = _entity(user_id=None, is_company_managed=True)
    _manageable(monkeypatch, entity)
    created = _sessions(monkeypatch)

    result = await _call(tool, **{id_arg: str(entity.id)}, users="a@example.com", binding_mode="add")

    assert "company-managed" in result and "Nothing was changed" in result
    assert all(session.commits == 0 for session in created)


@pytest.mark.parametrize(("tool", "id_arg", "_type"), TOOLS)
async def test_groups_are_added_removed_and_cleared(monkeypatch, tool, id_arg, _type):
    g1, g2 = str(uuid.uuid4()), str(uuid.uuid4())
    entity = _entity()
    _manageable(monkeypatch, entity)
    _sessions(monkeypatch)

    async def _normalize(_db, ids, perms, *, existing_ids=None):
        return (list(ids) or None), (dict(perms) or None)

    monkeypatch.setattr(api_helpers, "normalize_group_grants", _normalize)

    await _call(tool, **{id_arg: str(entity.id)}, shared_group_ids=g1)
    assert entity.shared_group_ids == [g1]
    await _call(
        tool,
        **{id_arg: str(entity.id)},
        shared_group_ids=g2,
        shared_group_permissions=f'{{"{g2}": "manage"}}',
        binding_mode="add",
    )
    assert entity.shared_group_ids == [g1, g2] and entity.shared_group_permissions == {g2: "config"}
    await _call(tool, **{id_arg: str(entity.id)}, shared_group_ids=g1, binding_mode="remove")
    assert entity.shared_group_ids == [g2]
    cleared = await _call(tool, **{id_arg: str(entity.id)}, shared_group_ids="none")
    assert "updated: groups" in cleared and entity.shared_group_ids is None


@pytest.mark.parametrize(("tool", "id_arg", "_type"), TOOLS)
async def test_a_call_without_the_new_arguments_answers_as_before(monkeypatch, tool, id_arg, _type):
    entity = _entity()
    _manageable(monkeypatch, entity)
    created = _sessions(monkeypatch)

    result = await _call(tool, **{id_arg: str(entity.id)}, description="new words")

    assert result.endswith("updated: description.")
    assert sum(session.commits for session in created) == 1
    assert await _call(tool, **{id_arg: str(entity.id)}) == "No fields to update. Provide at least one field."


@pytest.mark.parametrize(("tool", "id_arg"), [("get_skill", "skill_id"), ("get_prompt", "prompt_id")])
async def test_get_reads_back_the_owner_and_the_audience(monkeypatch, tool, id_arg):
    async def _no_groups(_db, _uid):
        return []

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)
    entity = _entity()
    _sessions(monkeypatch, results=[entity, [("ada@example.com", "Ada")]])

    result = await _call(tool, **{id_arg: str(entity.id)})

    assert f"Owner: Ada <ada@example.com> (`{entity.user_id}`)" in result, result
    assert "Shared with groups: None" in result and "Shared with people: None" in result
