"""Intent bindings from chat (#11865).

The agent tools ``list_agent_intent_bindings``, ``bind_agent_to_intent`` and
``unbind_agent_from_intent`` write through the REST routes' writer
(``services/intent_binding_writes.py``), so the gates are the API's: a
private agent is never bound globally, a locked default is never overridden
by a personal binding, the lock is an administrator's switch, and a built-in
default is not changed by a tool. ``clone_agent`` names the bindings a clone
does not carry. No database: the writer runs against a session double.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.api.v1 import mcp_xtm
from app.services import intent_binding_writes as writes
from app.services.builtin_tool_aliases import alias_scoped_functions
from app.services.builtin_tools.intent_bindings import (
    BIND_TOOL,
    INTENT_BINDING_TOOL_NAMES,
    LIST_TOOL,
    UNBIND_TOOL,
    binding_line,
    build_intent_binding_tools,
    catalog_eligibility,
)
from app.services.tool_classification import infer_tool_type


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(first=lambda: self._value, all=lambda: [self._value] if self._value else [])


class _Session:
    """Answers each ``select`` with the next value; the advisory lock is not a select."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.added: list[object] = []
        self.statements: list[object] = []

    async def execute(self, statement, params=None):
        self.statements.append(statement)
        if "pg_advisory_xact_lock" in str(statement):
            return _Result(None)
        return _Result(self.answers.pop(0) if self.answers else None)

    def add(self, row):
        self.added.append(row)


def _user(*, admin):
    return SimpleNamespace(id=uuid.uuid4(), is_admin=admin)


def _agent(*, company=False, groups=(), enabled=True):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="STIX transformer",
        slug="stix-transformer",
        is_company_managed=company,
        shared_group_ids=list(groups),
        enabled=enabled,
    )


_INTENT = SimpleNamespace(id=uuid.uuid4(), name="cti.stix_transformer")


async def test_an_administrator_binds_a_company_managed_agent_globally():
    db = _Session(None)  # no existing binding
    binding = await writes.upsert_intent_binding(
        db, _user(admin=True), _INTENT, _agent(company=True), vertical="cti", priority=5, is_locked=True
    )
    assert db.added == [binding]
    assert binding.user_id is None and binding.vertical == "cti"
    assert binding.priority == 5 and binding.is_locked is True


async def test_a_private_agent_is_never_bound_globally():
    with pytest.raises(writes.IntentBindingRefusedError) as refused:
        await writes.upsert_intent_binding(_Session(), _user(admin=True), _INTENT, _agent())
    assert refused.value.status == 422
    assert refused.value.detail == writes.PRIVATE_AGENT_GLOBAL_BINDING


async def test_a_person_writes_a_personal_override_without_the_lock():
    user = _user(admin=False)
    db = _Session(None, None)  # no locked default, no existing override
    binding = await writes.upsert_intent_binding(db, user, _INTENT, _agent(), is_locked=True)
    assert binding.user_id == user.id
    assert binding.is_locked is False


async def test_a_locked_default_is_not_overridden():
    locked = SimpleNamespace(is_locked=True)
    with pytest.raises(writes.IntentBindingRefusedError) as refused:
        await writes.upsert_intent_binding(_Session(locked), _user(admin=False), _INTENT, _agent())
    assert refused.value.status == 403
    assert refused.value.detail == writes.LOCKED_BINDING


async def test_a_tool_does_not_change_a_built_in_binding_the_api_still_does():
    def existing():
        return SimpleNamespace(is_builtin=True, priority=0, priority_overrides=None, is_locked=False)

    with pytest.raises(writes.IntentBindingRefusedError) as refused:
        await writes.upsert_intent_binding(
            _Session(existing()), _user(admin=True), _INTENT, _agent(company=True), refuse_builtin=True
        )
    assert refused.value.detail == writes.BUILTIN_BINDING

    kept = existing()
    out = await writes.upsert_intent_binding(
        _Session(kept), _user(admin=True), _INTENT, _agent(company=True), priority=3
    )
    assert out is kept and kept.priority == 3


def test_a_person_removes_only_their_own_override_and_never_a_built_in_one():
    user = _user(admin=False)
    with pytest.raises(writes.IntentBindingRefusedError):
        writes.check_intent_binding_delete(user, SimpleNamespace(user_id=None, is_builtin=False))
    writes.check_intent_binding_delete(user, SimpleNamespace(user_id=user.id, is_builtin=False))
    with pytest.raises(writes.IntentBindingRefusedError):
        writes.check_intent_binding_delete(
            _user(admin=True), SimpleNamespace(user_id=None, is_builtin=True), refuse_builtin=True
        )


def test_a_binding_line_says_whose_it_is_and_what_it_carries():
    viewer = uuid.uuid4()
    binding = SimpleNamespace(
        id=uuid.uuid4(),
        vertical=None,
        user_id=None,
        is_locked=True,
        is_builtin=False,
        priority=2,
        priority_overrides={"cti": 7},
    )
    line = binding_line(binding, "cti.stix_transformer", viewer)
    assert line.startswith("`cti.stix_transformer` - vertical all - priority 2 (cti: 7) - global, locked")
    binding.user_id = viewer
    assert "- yours" in binding_line(binding, "x", viewer)


def test_the_catalog_eligibility_names_what_keeps_an_agent_out():
    assert catalog_eligibility(_agent(company=True)) is None
    assert catalog_eligibility(_agent(groups=["g"])) is None
    assert "private" in catalog_eligibility(_agent())
    assert "disabled" in catalog_eligibility(_agent(company=True, enabled=False))


async def test_an_anonymous_caller_binds_nothing():
    definitions, handlers = build_intent_binding_tools("")
    assert {d.name for d in definitions} == INTENT_BINDING_TOOL_NAMES == set(handlers)
    arguments = {
        BIND_TOOL: {"agent_id": "a", "intent": "cti.stix_transformer"},
        UNBIND_TOOL: {"agent_id": "a", "intent": "cti.stix_transformer"},
    }
    for name, args in arguments.items():
        assert "not available for anonymous users" in await handlers[name](**args), name


class _SessionScope:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, *exc):
        return False


async def test_a_run_with_no_account_lists_the_global_bindings(monkeypatch):
    """An A2A run (no user) reads what get_agent shows it, instead of a refusal."""
    from app.core import database
    from app.services.builtin_tools import intent_bindings

    agent = _agent(company=True)
    db = _Session(agent)
    monkeypatch.setattr(database, "async_session", lambda: _SessionScope(db))
    seen: list[object] = []

    async def _lines(_db, the_agent, viewer_id):
        seen.append(viewer_id)
        return [f"`cti.stix_transformer` - {the_agent.slug}"]

    monkeypatch.setattr(intent_bindings, "agent_binding_lines", _lines)
    _definitions, handlers = build_intent_binding_tools("")
    out = await handlers[LIST_TOOL](agent_id="stix-transformer")
    assert "anonymous" not in out
    assert "Intent bindings of **STIX transformer**" in out
    assert seen == [None]  # global bindings only, nobody's personal override
    assert "is_company_managed" in str(db.statements[0])


async def test_a_run_with_no_account_does_not_see_an_agent_out_of_reach(monkeypatch):
    from app.core import database

    monkeypatch.setattr(database, "async_session", lambda: _SessionScope(_Session(None)))
    _definitions, handlers = build_intent_binding_tools("")
    assert "not found or you don't have access" in await handlers[LIST_TOOL](agent_id="private-one")


def test_the_list_is_a_read_and_the_writes_are_not():
    assert infer_tool_type(LIST_TOOL) == "read"
    assert infer_tool_type(BIND_TOOL) == "write"
    assert infer_tool_type(UNBIND_TOOL) == "write"
    assert LIST_TOOL in mcp_xtm.XTM_MCP_READ_TOOLS
    assert BIND_TOOL not in mcp_xtm.XTM_MCP_READ_TOOLS and UNBIND_TOOL not in mcp_xtm.XTM_MCP_READ_TOOLS


def test_the_agent_builder_alias_offers_the_three_tools():
    assert set(alias_scoped_functions("agent_builder")) >= INTENT_BINDING_TOOL_NAMES


def test_the_agent_builder_registers_the_tools():
    from app.services.builtin_tools import build_agent_builder_tool

    offered = {d.name for d in build_agent_builder_tool(None, "")[0]}
    assert offered >= INTENT_BINDING_TOOL_NAMES
