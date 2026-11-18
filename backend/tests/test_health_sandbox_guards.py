"""A Concierge run gets no production write through the builders (#8255).

Enforced where the tools are built and in the executor, on the real tool
sets: ``agent_builder`` keeps its reads only, ``flow_builder`` refuses its
production writes on a flow that is not a sandbox copy.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.health.identity import HealthPrincipal, acting_as_concierge
from app.services.health.sandbox import guards
from app.services.tool_classification import infer_tool_type
from app.tools.base import ToolCall

_PRINCIPAL = HealthPrincipal(agent_id=uuid.uuid4(), role="flow_lab")


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr("app.services.health.identity.concierge_enabled", lambda: True)


def _agent_builder():
    from app.services.builtin_tools import build_agent_builder_tool

    return build_agent_builder_tool(MagicMock(), str(uuid.uuid4()))


def test_agent_builder_keeps_its_writes_outside_a_concierge_run():
    definitions, _ = _agent_builder()
    assert any(infer_tool_type(d.name) == "write" for d in definitions)


def test_agent_builder_built_in_a_concierge_run_offers_reads_only():
    everything, _ = _agent_builder()
    with acting_as_concierge(_PRINCIPAL):
        definitions, _ = _agent_builder()
    reads = [d.name for d in everything if infer_tool_type(d.name) == "read"]
    assert [d.name for d in definitions] == reads
    assert "update_agent" not in {d.name for d in definitions}


async def test_agent_builder_refuses_a_write_called_by_name_in_a_concierge_run():
    _, executor = _agent_builder()  # built outside the run: the definitions are all there
    with acting_as_concierge(_PRINCIPAL):
        result = await executor.execute_tool(ToolCall(id="c1", name="delete_agent", arguments={"agent_id": "x"}))
    assert result.content == guards.BUILDER_REFUSAL
    assert (result.is_error, result.refused) == (True, True)


def _flow_builder(is_sandbox: bool | None):
    from app.services.builtin_tools import build_flow_builder_tool

    db = MagicMock()
    db.scalar = AsyncMock(return_value=is_sandbox)
    return build_flow_builder_tool(db, str(uuid.uuid4()))[1]


@pytest.mark.parametrize("tool", sorted(guards.FLOW_PRODUCTION_WRITES))
async def test_flow_builder_refuses_production_writes_in_a_concierge_run(tool):
    executor = _flow_builder(is_sandbox=False)
    with acting_as_concierge(_PRINCIPAL):
        result = await executor.execute_tool(ToolCall(id="c1", name=tool, arguments={"flow_id": str(uuid.uuid4())}))
    assert result.content == guards.PRODUCTION_FLOW_REFUSAL


@pytest.mark.parametrize("tool", sorted(guards.FLOW_SHARED_WRITES))
async def test_flow_builder_refuses_shared_writes_in_a_concierge_run_only(tool):
    executor = _flow_builder(is_sandbox=True)
    with acting_as_concierge(_PRINCIPAL):
        result = await executor.execute_tool(ToolCall(id="c1", name=tool, arguments={"name": "incident"}))
    assert result.content == guards.BUILDER_REFUSAL
    assert executor.handler(tool) is not None


async def test_a_sandbox_flow_is_not_refused():
    db = MagicMock()
    db.scalar = AsyncMock(return_value=True)
    with acting_as_concierge(_PRINCIPAL):
        assert await guards.production_flow_refusal(db, uuid.uuid4()) is None
        assert await guards.production_flow_refusal(db, "not-an-id") == guards.PRODUCTION_FLOW_REFUSAL


async def test_outside_a_concierge_run_nothing_is_refused():
    db = MagicMock()
    db.scalar = AsyncMock(return_value=False)
    assert await guards.production_flow_refusal(db, uuid.uuid4()) is None
    db.scalar.assert_not_called()


async def test_a_concierge_turn_while_dry_run_is_on_gets_a_dry_run_session(monkeypatch):
    from app.services.health.sandbox import turns
    from app.services.health.settings import HealthSettings

    async def _settings(_db):
        return HealthSettings(dry_run=True)

    monkeypatch.setattr("app.services.health.settings.load_concierge_settings", _settings)
    agent = MagicMock(is_sandbox=False)
    assert await turns.agent_turn_session(MagicMock(), "u", agent) is None
    with acting_as_concierge(_PRINCIPAL):
        session = await turns.agent_turn_session(MagicMock(), "u", agent)
    assert session is not None and session.mode == "dry_run"


async def test_dry_run_off_binds_nothing(monkeypatch):
    from app.services.health.sandbox import turns
    from app.services.health.settings import HealthSettings

    async def _settings(_db):
        return HealthSettings()

    monkeypatch.setattr("app.services.health.settings.load_concierge_settings", _settings)
    with acting_as_concierge(_PRINCIPAL):
        assert await turns.agent_turn_session(MagicMock(), "u", MagicMock(is_sandbox=False)) is None


async def test_a_service_write_kept_by_dry_run_is_audited_as_the_concierge(monkeypatch):
    from app.services.health.sandbox import dry_run

    entries: list[dict] = []

    async def _audit(_db, **kwargs):
        entries.append(kwargs)

    monkeypatch.setattr("app.services.audit.log_system_audit", _audit)
    admin = uuid.uuid4()
    with acting_as_concierge(HealthPrincipal(agent_id=uuid.uuid4(), role="memory_steward", on_behalf_of_admin_id=admin)):
        await dry_run.record_dry_run_write(MagicMock(), action="consolidate lane", target_type="memory", target_id="m1")
    await dry_run.record_dry_run_write(MagicMock(), action="consolidate lane", target_type="memory")
    assert [(e["actor"], e["anchor_user_id"], e["action"], e["entity_id"]) for e in entries] == [
        ("concierge", admin, dry_run.DRY_RUN_AUDIT_ACTION, "m1")
    ]


def test_a_copy_is_told_by_its_flag():
    from app.services.health.sandbox.copies import is_sandbox_copy

    assert [is_sandbox_copy(MagicMock(is_sandbox=v)) for v in (True, False)] == [True, False]
    assert is_sandbox_copy(object()) is False
