"""The sandbox tools' one access rule (#8255), on the agent tool set and on the
platform MCP server's: the ``CONCIERGE`` flag on, the Concierge or an
administrator, the source in the supervision scope, outside the fleet and its
content readable (opted in, its owner not opted out), at every call.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.concierge.identity import ConciergePrincipal, acting_as_concierge
from app.services.concierge.sandbox import tools
from app.tools.base import ToolCall

_SOURCE = uuid.uuid4()


def _db(*, caller=None, source=None):
    db = MagicMock()

    async def _get(model, key):
        if model.__name__ == "User":
            return caller
        if model.__name__ == "Agent":
            return source
        return None

    db.get = AsyncMock(side_effect=_get)
    db.commit = AsyncMock()
    return db


def _user(role="user"):
    return SimpleNamespace(id=uuid.uuid4(), is_admin=role == "admin", is_active=True)


def _agent(**fields):
    return SimpleNamespace(
        id=_SOURCE, user_id=uuid.uuid4(), is_sandbox=False, health_role=None, is_company_managed=False, **fields
    )


@pytest.fixture
def world(monkeypatch):
    state = {"flag": True, "scope": True, "created": []}
    monkeypatch.setattr("app.services.concierge.availability.concierge_enabled", lambda: state["flag"])
    monkeypatch.setattr("app.services.concierge.identity.concierge_enabled", lambda: state["flag"])

    async def _scope(_db, _type, _id):
        return state["scope"]

    monkeypatch.setattr("app.services.concierge.consent.in_supervision_scope", _scope)
    state["readable"] = True
    state["read_as"] = []

    async def _readable(_db, **kwargs):
        state["read_as"].append(kwargs)
        return state["readable"]

    monkeypatch.setattr("app.services.concierge.consent.content_readable", _readable)

    async def _create(db, **kwargs):
        state["created"].append(kwargs)
        from datetime import UTC, datetime

        return SimpleNamespace(id=uuid.uuid4(), copy_id=uuid.uuid4(), expires_at=datetime.now(UTC))

    monkeypatch.setattr("app.services.concierge.sandbox.copies.create_sandbox_copy", _create)
    return state


async def _create_copy(db, user_id):
    _, executor = tools.build_sandbox_tools(db, user_id)
    return await executor.execute_tool(
        ToolCall(
            id="c1",
            name="create_sandbox_copy",
            arguments={"source_type": "agent", "source_id": str(_SOURCE), "created_for": "finding:1"},
        )
    )


@pytest.mark.parametrize(
    ("case", "allowed"),
    [
        ("admin", True),
        ("concierge", True),
        ("non_admin", False),
        ("inactive_admin", False),
        ("unknown_caller", False),
        ("flag_off", False),
        ("out_of_scope", False),
        ("fleet_agent", False),
        ("a_copy", False),
        ("not_consented", False),
        ("admin_not_consented", False),
    ],
)
async def test_one_access_rule(world, case, allowed):
    caller = _user("admin" if "admin" in case and case != "non_admin" else "user")
    world["readable"] = "not_consented" not in case
    if case == "inactive_admin":
        caller.is_active = False
    source = _agent()
    if case == "fleet_agent":
        source = _agent()
        source.health_role, source.is_company_managed = "experimenter", True
    if case == "a_copy":
        source.is_sandbox = True
    world["flag"] = case != "flag_off"
    world["scope"] = case != "out_of_scope"
    db = _db(caller=None if case in ("unknown_caller", "concierge", "not_consented") else caller, source=source)
    user_id = None if case in ("concierge", "not_consented") else str(caller.id)
    if case in ("concierge", "not_consented"):
        with acting_as_concierge(ConciergePrincipal(agent_id=uuid.uuid4(), role="experimenter")):
            result = await _create_copy(db, user_id)
    else:
        result = await _create_copy(db, user_id)
    if allowed:
        assert json.loads(result.content)["copy_id"]
        assert len(world["created"]) == 1
        # The copy runs as its source's owner: their opt-out counts with the opt-in.
        assert world["read_as"] == [{"agent_id": _SOURCE, "user_id": source.user_id}]
    else:
        assert result.content == tools.REFUSED
        assert world["created"] == []


async def test_a_run_of_a_copy_is_refused_once_its_source_is_no_longer_readable(world, monkeypatch):
    """Consent is read again at run time: an opt-in revoked after the copy was made stops its runs."""
    copy = SimpleNamespace(id=uuid.uuid4(), source_type="agent", source_id=_SOURCE, copy_agent_id=uuid.uuid4())
    monkeypatch.setattr("app.services.concierge.sandbox.copies.get_sandbox_copy", AsyncMock(return_value=copy))
    ran = AsyncMock()
    monkeypatch.setattr("app.services.concierge.sandbox.runs.run_sandbox_agent", ran)
    world["readable"] = False
    _, executor = tools.build_sandbox_tools(_db(source=_agent()), None)
    with acting_as_concierge(ConciergePrincipal(agent_id=uuid.uuid4(), role="experimenter")):
        result = await executor.execute_tool(
            ToolCall(id="r", name="run_sandbox", arguments={"copy_id": str(copy.id), "input": "hi"})
        )
    assert result.content == tools.REFUSED
    ran.assert_not_awaited()


async def test_a_run_of_a_copy_that_does_not_exist_is_refused_like_any_other(world):
    defs, executor = tools.build_sandbox_tools(_db(caller=_user()), str(uuid.uuid4()))
    assert [d.name for d in defs] == ["create_sandbox_copy", "run_sandbox", "get_sandbox_run"]
    result = await executor.execute_tool(
        ToolCall(id="c", name="run_sandbox", arguments={"copy_id": str(uuid.uuid4()), "input": "hi"})
    )
    assert result.content == tools.REFUSED


# ── A copy stands for a subject the server recorded ──────────────────────────


@pytest.mark.parametrize(
    "arguments",
    [
        # A flow prototype is the Flow Lab's create_sandbox_flow, which records its basis.
        {"source_type": "flow", "created_for": "finding:1"},
        # The server's own markers, which name a basis or make a copy the Flow Lab's.
        {"source_type": "flow", "source_id": str(_SOURCE), "created_for": f"flow_lab:agent:{uuid.uuid4()}"},
        {"source_type": "agent", "source_id": str(_SOURCE), "created_for": "flow_lab"},
        {"source_type": "agent", "source_id": str(_SOURCE), "created_for": f"concierge_experiment:{uuid.uuid4()}"},
        {"source_type": "agent", "source_id": str(_SOURCE), "created_for": " Proposal:1"},
    ],
)
async def test_the_tool_makes_no_copy_standing_for_nothing_nor_one_naming_a_server_marker(world, arguments):
    _, executor = tools.build_sandbox_tools(_db(source=_agent()), None)
    with acting_as_concierge(ConciergePrincipal(agent_id=uuid.uuid4(), role="experimenter")):
        result = await executor.execute_tool(ToolCall(id="c", name="create_sandbox_copy", arguments=arguments))
    assert "copy_id" not in result.content
    assert world["created"] == []


def _prototype(created_for: str):
    return SimpleNamespace(
        id=uuid.uuid4(),
        source_type="flow",
        source_id=None,
        created_for=created_for,
        copy_agent_id=None,
        copy_flow_id=uuid.uuid4(),
    )


@pytest.mark.parametrize(("readable", "allowed"), [(True, True), (False, False)])
async def test_a_prototype_runs_only_while_its_recorded_basis_agent_is_in_reach(world, monkeypatch, readable, allowed):
    """The basis is read on the server's marker, then held to the source rule:
    scope, opt-in and its owner's opt-out, at every run."""
    basis = _agent()
    copy = _prototype(f"flow_lab:agent:{basis.id}")
    monkeypatch.setattr("app.services.concierge.sandbox.copies.get_sandbox_copy", AsyncMock(return_value=copy))
    started = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    monkeypatch.setattr("app.services.concierge.sandbox.runs.start_sandbox_flow_run", started)
    monkeypatch.setattr("app.services.orchestration_engine.notify_orchestration_wake", AsyncMock())
    world["readable"] = readable
    _, executor = tools.build_sandbox_tools(_db(source=basis), None)
    with acting_as_concierge(ConciergePrincipal(agent_id=uuid.uuid4(), role="experimenter")):
        result = await executor.execute_tool(ToolCall(id="r", name="run_sandbox", arguments={"copy_id": str(copy.id)}))
    assert world["read_as"] == [{"agent_id": basis.id, "user_id": basis.user_id}]
    assert (result.content != tools.REFUSED) is allowed
    assert started.await_count == (1 if allowed else 0)


async def test_a_prototype_with_no_recorded_basis_never_runs(world, monkeypatch):
    copy = _prototype("finding:1")
    monkeypatch.setattr("app.services.concierge.sandbox.copies.get_sandbox_copy", AsyncMock(return_value=copy))
    started = AsyncMock()
    monkeypatch.setattr("app.services.concierge.sandbox.runs.start_sandbox_flow_run", started)
    _, executor = tools.build_sandbox_tools(_db(source=_agent()), None)
    with acting_as_concierge(ConciergePrincipal(agent_id=uuid.uuid4(), role="experimenter")):
        result = await executor.execute_tool(ToolCall(id="r", name="run_sandbox", arguments={"copy_id": str(copy.id)}))
    assert result.content == tools.REFUSED
    started.assert_not_awaited()


async def test_a_new_flow_experiments_arm_stands_for_the_experiments_agent():
    from app.services.concierge.flow_lab_access import EXPERIMENT_COPY_PREFIX, copy_subject

    agent_id, experiment_id = uuid.uuid4(), uuid.uuid4()
    db = MagicMock()
    db.get = AsyncMock(return_value=SimpleNamespace(subject_agent_id=agent_id))
    assert await copy_subject(db, _prototype(f"{EXPERIMENT_COPY_PREFIX}{experiment_id}")) == ("agent", agent_id)
    assert db.get.await_args.args[1] == experiment_id
    source = SimpleNamespace(source_type="flow", source_id=_SOURCE, created_for=f"flow_lab:agent:{agent_id}")
    assert await copy_subject(db, source) == ("flow", _SOURCE)
    assert await copy_subject(db, _prototype("finding:1")) is None


async def test_the_concierge_never_runs_an_ownerless_copy_as_the_admin_it_acts_for(monkeypatch):
    """Its subject's owner, else nobody; only an administrator's own call runs one as that administrator."""
    from app.services.concierge.sandbox import runs

    monkeypatch.setattr("app.services.concierge.identity.concierge_enabled", lambda: True)
    admin, owner = uuid.uuid4(), uuid.uuid4()
    db = MagicMock()
    db.scalar = AsyncMock(return_value=None)
    copy = _prototype(f"flow_lab:agent:{uuid.uuid4()}")
    on_behalf = ConciergePrincipal(agent_id=uuid.uuid4(), role="experimenter", on_behalf_of_admin_id=admin)
    with acting_as_concierge(on_behalf):
        assert await runs.runner_user_id(db, copy, requested_by=admin) is None
    assert await runs.runner_user_id(db, copy, requested_by=admin) == admin
    assert await runs.runner_user_id(db, _prototype("finding:1"), requested_by=None) is None
    db.scalar = AsyncMock(return_value=owner)
    with acting_as_concierge(on_behalf):
        assert await runs.runner_user_id(db, copy) == owner


async def test_every_run_of_a_copy_re_reads_its_subjects_reach_whoever_starts_it(monkeypatch):
    """An evaluation case or a flow run started by the worker, with no caller to check, still stops."""
    from app.services.concierge.sandbox import runs

    async def _refused(_db, _type, _id):
        return "the source is not opted in"

    monkeypatch.setattr(tools, "subject_refusal", _refused)
    flow_copy = SimpleNamespace(
        id=uuid.uuid4(),
        source_type="flow",
        source_id=_SOURCE,
        created_for="x",
        copy_agent_id=None,
        copy_flow_id=uuid.uuid4(),
    )
    db = MagicMock()
    db.get = AsyncMock(
        side_effect=lambda model, _key: SimpleNamespace(is_sandbox=True, graph={}) if model.__name__ == "Flow" else None
    )
    monkeypatch.setattr("app.services.assignment_pause.is_paused_by_deactivated_owner", AsyncMock(return_value=False))
    with pytest.raises(runs.SandboxRunError, match="no longer within"):
        await runs.start_sandbox_flow_run(db, flow_copy)
    agent_copy = SimpleNamespace(
        id=uuid.uuid4(), source_type="agent", source_id=_SOURCE, created_for="x", copy_agent_id=uuid.uuid4()
    )
    with pytest.raises(runs.SandboxRunError, match="no longer within"):
        async with runs.sandbox_case(db, agent_copy, recorded_calls=[]):
            pass
    db.add.assert_not_called()
