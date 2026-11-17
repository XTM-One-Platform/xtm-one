"""A co-editor may only put on an entity an integration they can see.

Someone who may edit an agent, a flow or a KB sync source they do not own
could otherwise type the id of an integration they cannot see and have the
entity run with it. The rule is checked on the ids a save SETS or CHANGES,
compared with what the entity already holds: an unrelated edit, or an id sent
back unchanged, always saves, and nothing is re-validated at run time. The
owner and admins keep the rules they had, except that a KB sync source's
binder - its owner included - is held to ``access_restricted``.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import app.api.v1.flows as flows_api
import app.api.v1.knowledge_bases as kb_mod
from app.api.v1 import agents as agents_api
from app.api.v1.schemas.agent import UpdateAgentRequest
from app.api.v1.schemas.knowledge_base import (
    CreateSyncSourceRequest,
    UpdateSyncSourceRequest,
)
from app.models.integration import Integration
from app.services.flow_references import check_integration_references
from app.services.integration_credentials import (
    added_integration_bindings,
    integration_ids_writer_cannot_see,
)
from tests.integration_credential_fakes import (
    COLLEAGUE_ID,
    OWNER_ID,
    FakeSession,
    make_integration,
)

pytestmark = pytest.mark.asyncio

EDITOR_ID = COLLEAGUE_ID


def _user(user_id, *, admin: bool = False):
    return SimpleNamespace(
        id=user_id, is_admin=admin, role="admin" if admin else "user"
    )


def _integrations():
    """The owner's private integration, one the editor owns, a restricted company one."""
    private = make_integration(auth_type="api_key", name="Owner's private")
    editors = make_integration(auth_type="api_key", user_id=EDITOR_ID, name="Editor's")
    restricted = make_integration(
        auth_type="api_key",
        is_company_managed=True,
        access_restricted=True,
        name="Restricted company",
    )
    return private, editors, restricted


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


async def test_only_an_id_the_writer_adds_and_cannot_see_is_refused():
    private, editors, restricted = _integrations()
    db = FakeSession({Integration: [private, editors, restricted]})

    assert await integration_ids_writer_cannot_see(
        db, EDITOR_ID, is_admin=False, ids=[editors.id]
    ) == set()
    assert await integration_ids_writer_cannot_see(
        db, EDITOR_ID, is_admin=False, ids=[private.id, restricted.id]
    ) == {str(private.id), str(restricted.id)}
    # Already held: someone allowed to put it there did.
    assert await integration_ids_writer_cannot_see(
        db, EDITOR_ID, is_admin=False, ids=[private.id], stored=[str(private.id).upper()]
    ) == set()
    # Admins set anything.
    assert await integration_ids_writer_cannot_see(
        db, EDITOR_ID, is_admin=True, ids=[private.id]
    ) == set()


async def test_added_bindings_keep_only_new_ids_as_sent():
    old, new = str(uuid.uuid4()), str(uuid.uuid4())
    assert added_integration_bindings({"github": [old, new]}, [old.upper()]) == {
        "github": [new]
    }
    assert added_integration_bindings({"github": [old]}, [old]) is None
    assert added_integration_bindings({"github": ["not-a-uuid"]}, [old]) == {
        "github": ["not-a-uuid"]
    }


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


def _graph(*nodes):
    return {"nodes": list(nodes), "edges": []}


def _node(node_id, kind, **config):
    return {"id": node_id, "kind": kind, "config": config}


async def _flow_issues(user, graph, previous_graph, owner=OWNER_ID):
    private, editors, restricted = _integrations()
    db = FakeSession({Integration: [private, editors, restricted]})
    return await check_integration_references(
        db,
        user,
        graph=graph(private, editors, restricted),
        previous_graph=previous_graph(private, editors, restricted),
        flow_owner_id=owner,
    )


@pytest.mark.parametrize(
    ("kind", "field", "as_list"),
    [
        ("integration", "integration_id", False),
        ("trigger.integration_event", "integration_id", False),
        ("capability.integration", "integration_ids", True),
    ],
)
async def test_a_co_editor_cannot_put_an_integration_they_cannot_see_on_a_flow(
    kind, field, as_list
):
    def value(i):
        return [str(i.id)] if as_list else str(i.id)

    issues = await _flow_issues(
        _user(EDITOR_ID),
        lambda p, e, r: _graph(_node("n1", kind, **{field: value(p)})),
        lambda p, e, r: _graph(),
    )

    assert [(i["node_id"], i["field"], i["severity"]) for i in issues] == [
        ("n1", field, "error")
    ]
    assert "not shared with you" in issues[0]["message"]


async def test_a_co_editor_puts_an_integration_they_can_see_on_a_flow():
    issues = await _flow_issues(
        _user(EDITOR_ID),
        lambda p, e, r: _graph(_node("n1", "integration", integration_id=str(e.id))),
        lambda p, e, r: _graph(),
    )
    assert issues == []


async def test_a_co_editor_editing_another_node_keeps_the_owner_s_integration():
    """The owner's node is sent back untouched (or moved, or copied): saved."""
    issues = await _flow_issues(
        _user(EDITOR_ID),
        lambda p, e, r: _graph(
            _node("n1", "integration", integration_id=str(p.id), operation="edited"),
            _node("n2", "integration", integration_id=str(p.id)),
        ),
        lambda p, e, r: _graph(_node("n1", "integration", integration_id=str(p.id))),
    )
    assert issues == []


async def test_a_co_editor_cannot_swap_in_a_restricted_company_integration():
    issues = await _flow_issues(
        _user(EDITOR_ID),
        lambda p, e, r: _graph(_node("n1", "integration", integration_id=str(r.id))),
        lambda p, e, r: _graph(_node("n1", "integration", integration_id=str(e.id))),
    )
    assert [i["node_id"] for i in issues] == ["n1"]


async def test_admins_put_any_integration_on_a_flow():
    issues = await _flow_issues(
        _user(EDITOR_ID, admin=True),
        lambda p, e, r: _graph(_node("n1", "integration", integration_id=str(r.id))),
        lambda p, e, r: _graph(),
    )
    assert issues == []


@pytest.mark.parametrize("owner", [OWNER_ID, None], ids=["existing_flow", "new_flow"])
async def test_the_owner_cannot_plant_an_integration_they_cannot_see(owner):
    """The runtime accepts a node's integration when ANY editor of the flow can
    see it, and uses an editor's own token as vouched for: an owner who typed a
    colleague's integration id, then shared the flow with them, would run with
    the colleague's credentials. The owner is held to what they can see too."""
    issues = await _flow_issues(
        _user(OWNER_ID),
        lambda p, e, r: _graph(_node("n1", "integration", integration_id=str(e.id))),
        lambda p, e, r: _graph(),
        owner=owner,
    )
    assert [(i["node_id"], i["code"]) for i in issues] == [("n1", "integration.not_visible")]


async def test_worked_on_main_the_owner_keeps_their_own_and_stored_integrations():
    """The owner's own integration, an id the flow already held (a co-editor's
    node, moved or copied), and an id naming no integration (an imported flow;
    the run reports it) all save as before."""
    dangling = str(uuid.uuid4())
    issues = await _flow_issues(
        _user(OWNER_ID),
        lambda p, e, r: _graph(
            _node("n1", "integration", integration_id=str(p.id)),
            _node("n2", "integration", integration_id=str(e.id)),
            _node("n3", "integration", integration_id=str(e.id)),
            _node("n4", "integration", integration_id=dangling),
        ),
        lambda p, e, r: _graph(_node("n2", "integration", integration_id=str(e.id))),
    )
    assert issues == []


async def test_the_flow_write_gate_refuses_the_save(monkeypatch):
    """``_assert_writable`` (every REST graph write) turns the issue into a 422."""
    private, editors, restricted = _integrations()
    db = FakeSession({Integration: [private, editors, restricted]})
    monkeypatch.setattr(flows_api, "_assert_graph_valid", lambda _g: [])
    monkeypatch.setattr(
        flows_api, "check_flow_references", AsyncMock(side_effect=lambda *a, **k: [])
    )
    monkeypatch.setattr(
        flows_api, "check_tool_references", AsyncMock(side_effect=lambda *a, **k: [])
    )
    monkeypatch.setattr(flows_api, "get_user_group_ids", AsyncMock(return_value=[]))
    graph = _graph(_node("n1", "integration", integration_id=str(private.id)))

    with pytest.raises(HTTPException) as exc:
        await flows_api._assert_writable(
            db,
            _user(EDITOR_ID),
            graph,
            flow_id=uuid.uuid4(),
            previous_graph=_graph(),
            flow_owner_id=OWNER_ID,
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["issues"][0]["code"] == "integration.not_visible"

    # The same graph from its owner saves.
    await flows_api._assert_writable(
        db, _user(OWNER_ID), graph, previous_graph=_graph(), flow_owner_id=OWNER_ID
    )


async def test_the_flow_update_and_split_routes_pass_the_owner():
    update = inspect.getsource(flows_api.update_flow)
    assert "flow_owner_id=flow.user_id" in update
    split = inspect.getsource(flows_api.detach_selection)
    assert "check_integration_references" in split


def test_every_flow_graph_writer_runs_the_integration_check():
    """Create and import go through ``_assert_writable``; the flow-builder tool
    writes graphs outside the REST routes and must run the same check."""
    from app.services.builtin_tools import flow_builder

    for route in (flows_api.create_flow, flows_api.import_flow):
        assert "_assert_writable(" in inspect.getsource(route)
    tool = inspect.getsource(flow_builder)
    assert "check_integration_references(" in tool
    # Once per graph write: create and update.
    assert tool.count("await _integration_refusals(") == 2


def test_every_flow_graph_writer_runs_the_reference_check():
    """Every other id a node names is checked where the integrations are:
    the REST write gate, the split route, the flow-builder tool."""
    from app.services.builtin_tools import flow_builder

    assert "check_source_references(" in inspect.getsource(flows_api._assert_writable)
    detach = inspect.getsource(flows_api.detach_selection)
    assert detach.count("check_source_references(") == 2
    # The detached half is a new flow of the caller's, checked as one.
    assert detach.count("graph=child_graph, previous_graph=None, flow_owner_id=None") == 2
    assert "check_source_references(" in inspect.getsource(flow_builder)
    # A duplicate leaves out what its copier cannot use; a restore is held to
    # what the flow holds now.
    assert "strip_unseen_references(" in inspect.getsource(flows_api.duplicate_flow)
    restore = inspect.getsource(flows_api.restore_version)
    assert "check_source_references(" in restore and "check_integration_references(" in restore
    assert "check_tool_references(" in restore


def test_every_flow_graph_writer_runs_the_tool_name_check():
    """A Call-tool name resolves in the flow's reach, so every graph
    write checks the names it adds against that reach and its writer: the
    REST write gate with the flow's owner, the split route, and the
    flow-builder tool's writes and strict-mode runs."""
    from app.services.builtin_tools import flow_builder

    gate = inspect.getsource(flows_api._assert_writable)
    assert "check_tool_references(" in gate
    assert "flow_owner_id=flow_owner_id" in gate
    assert "is_company_managed=is_company_managed" in gate
    assert "is_company_managed=flow.is_company_managed" in inspect.getsource(flows_api.update_flow)
    assert "is_company_managed=body.is_company_managed" in inspect.getsource(flows_api.create_flow)
    assert "check_tool_references(" in inspect.getsource(flows_api.detach_selection)
    tool = inspect.getsource(flow_builder)
    assert tool.count("check_tool_references(") == 2
    assert "admin_bypass=agent_tool_admin_bypass(user)" in tool


# ---------------------------------------------------------------------------
# Agents (REST)
# ---------------------------------------------------------------------------


class _AgentDB:
    """Every query answers nothing: an integration check finds no visible row."""

    def __init__(self):
        self.integration_checks = 0

    async def commit(self):
        return None

    async def execute(self, stmt):
        if "integrations" in str(stmt):
            self.integration_checks += 1
        return _EmptyResult()


class _EmptyResult:
    def scalar(self):
        return 0

    def scalar_one_or_none(self):
        return None

    def one_or_none(self):
        return None

    def first(self):
        return None

    def scalars(self):
        return self

    def all(self):
        return []

    def __iter__(self):
        return iter([])


def _shared_agent(bindings):
    from tests.test_agent_update_partial_payload import _DummyAgent

    agent = _DummyAgent()
    agent.user_id = OWNER_ID
    agent.shared_user_ids = [str(EDITOR_ID)]
    agent.shared_user_permissions = {str(EDITOR_ID): "config"}
    agent.integration_bindings = bindings
    return agent


async def _update_agent(monkeypatch, agent, user, bindings):
    from tests.test_agent_update_partial_payload import _response_payload

    async def _none(*_a, **_k):
        return None

    async def _versions(*_a, **_k):
        return None, {}

    monkeypatch.setattr(agents_api, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(agents_api, "get_user_agent", AsyncMock(return_value=agent))
    monkeypatch.setattr(agents_api, "require_admin_for_company_managed", lambda *a, **k: None)
    monkeypatch.setattr(agents_api, "log_audit", _none)
    monkeypatch.setattr(agents_api, "record_agent_version_changes", _versions)
    monkeypatch.setattr(agents_api, "_to_response", _response_payload)
    db = _AgentDB()
    request = Request({"type": "http", "method": "PUT", "path": "/x", "headers": []})
    await agents_api.update_agent(
        str(agent.id), UpdateAgentRequest(integration_bindings=bindings), request, user, db
    )
    return db


async def test_a_co_editor_resending_an_agent_s_bindings_saves(monkeypatch):
    """The UI sends the whole map; the owner's binding (invisible to the
    editor) is not new, so it is not checked. Refused."""
    old = str(uuid.uuid4())
    agent = _shared_agent({"github": [old]})
    db = await _update_agent(monkeypatch, agent, _user(EDITOR_ID), {"github": [old]})
    assert db.integration_checks == 0
    assert agent.integration_bindings == {"github": [old]}


async def test_a_co_editor_adding_an_integration_they_cannot_see_is_refused(monkeypatch):
    old, new = str(uuid.uuid4()), str(uuid.uuid4())
    agent = _shared_agent({"github": [old]})
    with pytest.raises(HTTPException) as exc:
        await _update_agent(monkeypatch, agent, _user(EDITOR_ID), {"github": [old, new]})
    assert exc.value.status_code == 400
    assert "not found or are not accessible" in exc.value.detail


async def test_only_the_added_binding_reaches_the_visibility_check(monkeypatch):
    old, new = str(uuid.uuid4()), str(uuid.uuid4())
    seen: dict = {}

    async def _record(*_a, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(agents_api, "validate_personal_agent_bindings", _record)
    await _update_agent(
        monkeypatch, _shared_agent({"github": [old]}), _user(EDITOR_ID), {"github": [old, new]}
    )
    assert seen["integration_bindings"] == {"github": [new]}
    assert seen["is_admin"] is False


@pytest.mark.parametrize(
    "user", [_user(OWNER_ID), _user(EDITOR_ID, admin=True)], ids=["owner", "admin"]
)
async def test_the_agent_owner_and_admins_keep_their_rules(monkeypatch, user):
    new = str(uuid.uuid4())
    seen: dict = {}

    async def _record(*_a, **kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(agents_api, "validate_personal_agent_bindings", _record)
    await _update_agent(monkeypatch, _shared_agent(None), user, {"github": [new]})
    assert seen["integration_bindings"] == {"github": [new]}
    assert seen["is_admin"] is user.is_admin


# ---------------------------------------------------------------------------
# KB sync sources
# ---------------------------------------------------------------------------


def _patch_kb(monkeypatch, kb, *, invisible=frozenset()):
    monkeypatch.setattr(kb_mod, "get_user_knowledge_base", AsyncMock(return_value=kb))
    monkeypatch.setattr(kb_mod, "require_admin_for_company_managed", MagicMock())
    monkeypatch.setattr(kb_mod, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(kb_mod, "can_manage", MagicMock(return_value=True))
    monkeypatch.setattr(kb_mod, "_sync_source_to_response", MagicMock(return_value=None))
    monkeypatch.setattr(kb_mod, "is_package_origin_knowledge_base", AsyncMock(return_value=False))
    monkeypatch.setattr(kb_mod, "assert_knowledge_base_not_package_origin", AsyncMock())
    monkeypatch.setattr(kb_mod, "_assert_sync_sources_licensed", AsyncMock())

    async def _sees(_db, integration_id, _user_id):
        return str(integration_id) not in invisible

    monkeypatch.setattr(kb_mod, "integration_visible_to_user", _sees)


def _kb():
    return SimpleNamespace(id=uuid.uuid4(), user_id=OWNER_ID, is_builtin=False)


class _Rows:
    def __init__(self, *values):
        self.values = list(values)

    def __call__(self, _stmt):
        value = self.values.pop(0) if self.values else None
        return SimpleNamespace(scalar_one_or_none=lambda: value)


async def _update_source(monkeypatch, user, source, integration_id, *, rows, invisible=()):
    kb = _kb()
    _patch_kb(monkeypatch, kb, invisible=frozenset(invisible))
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_Rows(source, *rows))
    db.flush = AsyncMock()
    await kb_mod.update_sync_source(
        kb_id=str(kb.id),
        source_id=str(uuid.uuid4()),
        body=UpdateSyncSourceRequest(integration_id=integration_id, sync_interval="daily"),
        user=user,
        db=db,
    )


async def test_a_co_manager_resending_a_source_s_integration_saves(monkeypatch):
    """The owner's private integration, sent back with an interval change:
    saved, still syncing as the owner. Refused (403)."""
    private, _, _ = _integrations()
    source = SimpleNamespace(
        integration_id=private.id, user_id=OWNER_ID, sync_interval="manual",
        config={}, enabled=True, source_type="website",
    )
    await _update_source(
        monkeypatch, _user(EDITOR_ID), source, str(private.id), rows=["Owner's private"]
    )
    assert source.integration_id == private.id
    assert source.user_id == OWNER_ID


async def test_a_co_manager_cannot_bind_a_restricted_company_integration(monkeypatch):
    """A co-manager cannot bind a restricted company-managed integration."""
    _, _, restricted = _integrations()
    source = SimpleNamespace(
        integration_id=None, user_id=OWNER_ID, sync_interval="manual",
        config={}, enabled=True, source_type="website",
    )
    with pytest.raises(HTTPException) as exc:
        await _update_source(
            monkeypatch, _user(EDITOR_ID), source, str(restricted.id),
            rows=[restricted], invisible={str(restricted.id)},
        )
    assert exc.value.status_code == 403
    assert source.integration_id is None


async def test_a_co_manager_binds_their_own_integration(monkeypatch):
    _, editors, _ = _integrations()
    source = SimpleNamespace(
        integration_id=None, user_id=OWNER_ID, sync_interval="manual",
        config={}, enabled=True, source_type="website",
    )
    await _update_source(monkeypatch, _user(EDITOR_ID), source, str(editors.id), rows=[editors])
    assert source.integration_id == editors.id
    # A non-OAuth credential still works for the source's identity, so the
    # identity (and the source's lifecycle) stays as it was.
    assert source.user_id == OWNER_ID


@pytest.mark.parametrize(
    "user", [_user(OWNER_ID), _user(EDITOR_ID, admin=True)], ids=["its_owner", "admin"]
)
async def test_the_integration_s_owner_and_admins_bind_it_whatever_the_restriction(
    monkeypatch, user
):
    _, _, restricted = _integrations()
    source = SimpleNamespace(
        integration_id=None, user_id=OWNER_ID, sync_interval="manual",
        config={}, enabled=True, source_type="website",
    )
    await _update_source(
        monkeypatch, user, source, str(restricted.id),
        rows=[restricted], invisible={str(restricted.id)},
    )
    assert source.integration_id == restricted.id


@pytest.mark.parametrize(("visible", "bound"), [(False, False), (True, True)], ids=["outside", "granted"])
async def test_the_kb_owner_is_held_to_the_restriction_like_a_co_manager(monkeypatch, visible, bound):
    """The KB's owner is held to ``access_restricted`` like a co-manager."""
    restricted = make_integration(
        auth_type="api_key", user_id=uuid.uuid4(), is_company_managed=True, access_restricted=True
    )
    source = SimpleNamespace(
        integration_id=None, user_id=OWNER_ID, sync_interval="manual",
        config={}, enabled=True, source_type="website",
    )
    invisible = () if visible else {str(restricted.id)}
    if bound:
        await _update_source(
            monkeypatch, _user(OWNER_ID), source, str(restricted.id), rows=[restricted], invisible=invisible
        )
        assert source.integration_id == restricted.id
    else:
        with pytest.raises(HTTPException) as exc:
            await _update_source(
                monkeypatch, _user(OWNER_ID), source, str(restricted.id), rows=[restricted], invisible=invisible
            )
        assert exc.value.status_code == 403
        assert source.integration_id is None


async def test_a_co_manager_creating_a_source_is_held_to_visibility(monkeypatch):
    _, _, restricted = _integrations()
    kb = _kb()
    _patch_kb(monkeypatch, kb, invisible={str(restricted.id)})
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=_Rows(restricted))
    db.add = MagicMock()
    db.flush = AsyncMock()
    body = CreateSyncSourceRequest(
        source_type="website",
        config={"url": "https://example.com/docs"},
        sync_interval="manual",
        integration_id=str(restricted.id),
    )
    with pytest.raises(HTTPException) as exc:
        await kb_mod.create_sync_source(kb_id=str(kb.id), body=body, user=_user(EDITOR_ID), db=db)
    assert exc.value.status_code == 403
    db.add.assert_not_called()


# ---------------------------------------------------------------------------
# Agents (agent-builder tool)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["add", "replace"])
async def test_the_agent_builder_checks_only_the_binding_a_call_adds(monkeypatch, mode):
    """``add`` merges into the stored map, ``replace`` re-sends it: the owner's
    binding already there is never forwarded to the visibility check (the
    whole merged map would let an invisible stored id refuse the call)."""
    from app.core import database
    from app.models.agent import Agent
    from app.services.builtin_tools import build_agent_builder_tool
    from tests.test_agent_builder_crud_tools import (
        _admin_caller,
        _call,
        _session_factory,
        _stub_personal_bindings,
        _stub_writable_agent,
    )

    old, new = str(uuid.uuid4()), str(uuid.uuid4())
    agent = Agent(
        id=uuid.uuid4(),
        name="Shared helper",
        is_company_managed=False,
        integration_bindings={"github": [old]},
    )
    _stub_writable_agent(monkeypatch, agent=agent)
    _admin_caller(monkeypatch, is_admin=False)
    seen: dict = {}
    _stub_personal_bindings(monkeypatch, recorder=seen)
    monkeypatch.setattr(database, "async_session", _session_factory([]))
    pairs = f"github:{new}" if mode == "add" else f"github:{old},github:{new}"

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(EDITOR_ID))
    result = await _call(
        executor,
        "update_agent",
        agent_id=str(agent.id),
        integration_bindings=pairs,
        binding_mode=mode,
    )

    assert not result.is_error, result.content
    assert seen["integration_bindings"] == {"github": [new]}
    assert seen["is_admin"] is False
