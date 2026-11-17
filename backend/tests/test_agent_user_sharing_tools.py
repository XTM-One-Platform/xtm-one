"""Per-user agent sharing from chat, and the group listing, without a database.

What these pin (the PostgreSQL half is ``test_agent_user_sharing_postgres.py``):

* ``update_agent`` takes ``users`` (emails or user ids) with a
  ``users_permission``, following ``binding_mode`` like a binding, and
  ``create_agent`` takes them too; ``list_groups`` is declared and registered;
* the arguments are refused before anything is written: a permission with no
  people, an unknown permission, a clear sentinel outside replace mode, a
  separator-only value, more people than one call may name, a company-managed
  agent (already open to everyone), an address no account matches;
* the grants are written through the one grant write (``apply_user_share_fields``),
  never by hand;
* ``plan_grants`` turns add / remove / replace into the list to store;
* ``_agent_share_tier`` reads the dialog's words for the three tiers.
"""

from __future__ import annotations

import inspect
import uuid

import pytest

import app.core.database as database
import app.services.builtin_tools as builtin_tools_module
from app.models.agent import Agent
from app.services.builtin_tools import build_agent_builder_tool
from app.services.builtin_tools.agent_builder import _agent_share_tier
from app.services.share_principals import (
    MAX_PRINCIPALS_PER_CALL,
    Principal,
    plan_grants,
    principal_entries,
)
from app.tools.base import ToolCall

# ---------------------------------------------------------------------------
# Fakes (the shape ``test_agent_builder_crud_tools.py`` uses)
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value

    def all(self):
        return list(self._value) if self._value else []

    def scalars(self):
        return self


class _FakeSession:
    def __init__(self, created):
        created.append(self)
        self.commits = 0
        self.rollbacks = 0
        self.executes = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def execute(self, _stmt):
        self.executes += 1
        return _Result()

    def add(self, _instance):
        pass

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _sessions(monkeypatch):
    created: list[_FakeSession] = []
    monkeypatch.setattr(database, "async_session", lambda: _FakeSession(created))
    return created


def _writable(monkeypatch, agent):
    async def _fake(_db, _uid, _agent_uuid, *, action="modify"):
        return agent, None

    monkeypatch.setattr(builtin_tools_module, "_load_writable_agent_for_builder", _fake)


async def _call(name: str, **arguments):
    _, executor = build_agent_builder_tool(None, str(uuid.uuid4()))
    result = await executor.execute_tool(ToolCall(id="call", name=name, arguments=arguments))
    return result.content


def _private_agent() -> Agent:
    return Agent(id=uuid.uuid4(), user_id=uuid.uuid4(), name="Barclays onboarding", is_company_managed=False)


# ---------------------------------------------------------------------------
# Declared and registered
# ---------------------------------------------------------------------------


def test_the_sharing_arguments_and_the_group_listing_are_declared():
    definitions, executor = build_agent_builder_tool(None, str(uuid.uuid4()))
    by_name = {d.name: d for d in definitions}
    assert "list_groups" in by_name and "list_groups" in executor.tool_names
    update = by_name["update_agent"].input_schema["properties"]
    assert {"users", "users_permission", "shared_group_ids"} <= set(update)
    assert "users" in update["binding_mode"]["description"]
    # create_agent builds and shares in one call.
    create = by_name["create_agent"].input_schema["properties"]
    assert {"users", "users_permission"} <= set(create)
    assert {"users", "users_permission"} <= set(inspect.signature(executor.handler("create_agent")).parameters)
    # The group listing is paginated like the other list tools.
    assert {"search", "members", "limit", "offset"} == set(by_name["list_groups"].input_schema["properties"])


def test_the_grants_are_written_through_the_one_grant_write():
    """The agent tool hands the grants to ``apply_user_share_fields`` (hence
    ``normalize_user_grants``: canonical ids, existing active accounts, the
    account locks) and never assigns ``shared_user_ids`` itself."""
    _definitions, executor = build_agent_builder_tool(None, str(uuid.uuid4()))
    source = inspect.getsource(executor.handler("update_agent"))
    assert "await apply_user_share_fields(" in source
    assert "agent.shared_user_ids =" not in source
    assert "agent.shared_user_permissions =" not in source


async def test_an_anonymous_caller_lists_no_group():
    _, executor = build_agent_builder_tool(None, "anonymous")
    result = await executor.execute_tool(ToolCall(id="call", name="list_groups", arguments={}))
    assert "not available for anonymous users" in result.content


# ---------------------------------------------------------------------------
# Refused before anything is written
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"users_permission": "config"}, "pass `users` too"),
        ({"users": "alice@example.com", "users_permission": "owner"}, "Invalid users_permission"),
        ({"users": "clear", "binding_mode": "add"}, "only valid with binding_mode='replace'"),
        ({"users": ", ,"}, "No valid users"),
        ({"users": [f"p{i}@example.com" for i in range(MAX_PRINCIPALS_PER_CALL + 1)]}, "at most"),
        ({"users": "alice@example.com", "binding_mode": "add"}, "no account on this platform matches"),
    ],
    ids=["permission-alone", "unknown-permission", "clear-outside-replace", "separators", "too-many", "unknown-email"],
)
async def test_a_sharing_argument_is_refused_before_anything_is_written(monkeypatch, arguments, expected):
    agent = _private_agent()
    _writable(monkeypatch, agent)
    created = _sessions(monkeypatch)

    result = await _call("update_agent", agent_id=str(agent.id), **arguments)

    assert expected in result, result
    assert agent.shared_user_ids is None
    assert all(session.commits == 0 for session in created)


async def test_a_company_managed_agent_is_not_shared_with_people(monkeypatch):
    """Everyone can use it already; the restriction is an admin's, from the UI."""
    agent = Agent(id=uuid.uuid4(), user_id=None, name="Company helper", is_company_managed=True)
    _writable(monkeypatch, agent)
    created = _sessions(monkeypatch)

    result = await _call("update_agent", agent_id=str(agent.id), users="alice@example.com", binding_mode="add")

    assert "company-managed" in result and "Nothing was changed" in result, result
    assert agent.shared_user_ids is None
    assert all(session.commits == 0 and session.executes == 0 for session in created)


# ---------------------------------------------------------------------------
# The plan of one kind of grant, and the words for the tiers
# ---------------------------------------------------------------------------


def _people(*names: str) -> list[Principal]:
    return [Principal("user", name, f"{name} <{name}@example.com>") for name in names]


def test_plan_grants_add_grants_retiers_and_keeps_the_rest():
    current = [("ada", "read_only"), ("bob", "config")]
    ids, tiers, delta = plan_grants("add", "read_only", current, _people("bob", "cid"))
    assert ids == ["ada", "bob", "cid"]
    assert tiers == {"ada": "read_only", "bob": "read_only", "cid": "read_only"}
    assert (delta.added, delta.retiered, delta.unchanged, delta.removed) == (["cid"], ["bob"], [], [])
    assert delta.changed


def test_plan_grants_remove_drops_the_named_and_reports_the_unshared():
    current = [("ada", "read_only"), ("bob", "config")]
    ids, tiers, delta = plan_grants("remove", "read_only", current, _people("bob", "zoe"))
    assert ids == ["ada"] and tiers == {"ada": "read_only"}
    assert delta.removed == ["bob"] and delta.not_shared == ["zoe"]


def test_plan_grants_replace_makes_the_named_the_whole_audience():
    current = [("ada", "read_only"), ("bob", "config")]
    ids, tiers, delta = plan_grants("replace", "config", current, _people("bob", "cid"))
    assert ids == ["bob", "cid"] and tiers == {"bob": "config", "cid": "config"}
    assert delta.removed == ["ada"] and delta.added == ["cid"] and delta.unchanged == ["bob"]
    _ids, _tiers, same = plan_grants("add", "read_only", [("ada", "read_only")], _people("ada"))
    assert not same.changed and same.unchanged == ["ada"]


@pytest.mark.parametrize(
    ("raw", "tier"),
    [
        ("read_only", "read_only"),
        ("Read-only", "read_only"),
        ("use", "read_only"),
        ("chat", "read_only"),
        ("config", "config"),
        ("edit", "config"),
        ("manage", "config"),
        ("config_with_chats", "config_with_chats"),
        ("configuration with chats", "config_with_chats"),
        ("owner", None),
        ("", None),
    ],
)
def test_the_tier_words(raw, tier):
    assert _agent_share_tier(raw) == tier


def test_principal_entries_read_a_list_a_json_list_and_a_separated_string():
    assert principal_entries(["a@x.io", "b@x.io", "a@x.io"]) == ["a@x.io", "b@x.io"]
    assert principal_entries('["a@x.io", {"email": "b@x.io"}]') == ["a@x.io", "b@x.io"]
    assert principal_entries("a@x.io, b@x.io; c@x.io\nd@x.io") == ["a@x.io", "b@x.io", "c@x.io", "d@x.io"]
    assert principal_entries(None) == [] and principal_entries("") == []


def test_a_user_listed_in_the_schema_is_a_string_or_a_list():
    definitions, _executor = build_agent_builder_tool(None, str(uuid.uuid4()))
    users = next(d for d in definitions if d.name == "update_agent").input_schema["properties"]["users"]
    assert set(users["type"]) == {"array", "string"}
    assert users["items"] == {"type": "string"}
    # Nothing in the description reads as a hint to a model about emails the
    # platform does not know: the refusal names them, the schema says so.
    assert "refused by name" in users["description"]
