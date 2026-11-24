"""The module reads wired into a Module Analyst cycle's run (#101 S14a-2b-4, platform-health.md 12.3):
the investigated registration's integration only, allow-listed reads only, a write refused by name."""

from __future__ import annotations

import uuid

from app.services.builtin_tools import health_modules
from app.services.health.module_analyst import REFUSAL_REASON
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolCall, ToolDefinition


def _defs(*names: str) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    executor = CallableToolExecutor()
    defs = []
    for name in names:

        async def _ok(**_kwargs):
            return "ok"

        executor.register(name, _ok)
        defs.append(ToolDefinition(name=name, description=name, input_schema={"type": "object"}))
    return defs, executor


def test_only_allow_listed_reads_are_kept():
    defs, executor = _defs("list_opencti_playbook_states", "manage_opencti_connector", "list_opencti_reports")
    kept = health_modules.keep_allow_listed_reads("opencti", defs, executor)
    assert [d.name for d in kept] == ["list_opencti_playbook_states"]
    assert set(executor.refusals) == {"manage_opencti_connector", "list_opencti_reports"}


async def test_a_withheld_tool_called_by_name_is_refused():
    defs, executor = _defs("list_opencti_playbook_states", "manage_opencti_connector")
    health_modules.keep_allow_listed_reads("opencti", defs, executor)
    result = await executor.execute_tool(ToolCall(id="c", name="manage_opencti_connector", arguments={}))
    assert result.is_error and result.content == REFUSAL_REASON


async def test_nothing_is_wired_without_the_group(monkeypatch):
    monkeypatch.setattr(health_modules, "module_tools_offered", lambda: True)
    composite = CompositeToolExecutor()
    added = await health_modules.add_module_read_tools(
        None, composite, ["health_diagnosis"], trigger_context={}, agent_id=uuid.uuid4(), user_id=None, run_id="r"
    )
    assert added == 0


async def test_nothing_is_wired_outside_xtm_one(monkeypatch):
    monkeypatch.setattr(health_modules, "module_tools_offered", lambda: False)
    added = await health_modules.add_module_read_tools(
        None,
        CompositeToolExecutor(),
        [health_modules.MODULES_GROUP_ID],
        trigger_context={},
        agent_id=uuid.uuid4(),
        user_id=None,
        run_id="r",
    )
    assert added == 0


async def test_a_run_without_a_cycle_gets_no_module_tool():
    assert await health_modules._cycle_module_target(None, {"type": "manual"}, uuid.uuid4()) is None


async def _wire(monkeypatch, built_names: tuple[str, ...], *, run_as, reaches: bool = True, run_user=None):
    """A Module Analyst cycle run on an OpenCTI registration: (tools added, what the build saw, composite)."""
    integration_id = uuid.uuid4()
    seen: dict = {"integration_id": integration_id}

    async def _target(db, trigger_context, agent_id):
        return "opencti", integration_id

    async def _build(db, bindings, *, context=None):
        seen["bindings"], seen["context"] = bindings, context
        return _defs(*built_names)

    async def _configured(db):
        return run_as

    async def _reaches(db, user_id, integration):
        seen["checked"] = (user_id, integration)
        return reaches

    monkeypatch.setattr(health_modules, "module_tools_offered", lambda: True)
    monkeypatch.setattr(health_modules, "_cycle_module_target", _target)
    monkeypatch.setattr("app.services.integration_tools.build_integration_tools", _build)
    monkeypatch.setattr("app.services.health.run_as.configured_run_as", _configured)
    monkeypatch.setattr("app.services.health.run_as.reaches_integration", _reaches)
    composite = CompositeToolExecutor()
    added = await health_modules.add_module_read_tools(
        None,
        composite,
        [health_modules.MODULES_GROUP_ID],
        trigger_context={"concierge_cycle_id": str(uuid.uuid4())},
        agent_id=uuid.uuid4(),
        user_id=run_user,
        run_id="r",
    )
    return added, seen, composite


async def test_the_cycle_registration_integration_is_built_read_only(monkeypatch):
    added, seen, composite = await _wire(
        monkeypatch, ("list_opencti_playbook_states", "delete_opencti_feed"), run_as=uuid.uuid4()
    )
    assert added == 1
    assert seen["bindings"] == {"opencti": str(seen["integration_id"])}
    from app.services.integration_read_only import READ_ONLY_CONTEXT_KEY

    assert seen["context"][READ_ONLY_CONTEXT_KEY] == frozenset({str(seen["integration_id"])})
    assert [d.name for d in composite.all_tools] == ["list_opencti_playbook_states"]


async def test_the_reads_act_as_the_run_as_user_not_the_run_user(monkeypatch):
    # ctem, 23 Nov 2026: the round's checker runs carried no user and the platform-managed
    # module integration could not sign a JWT. The JWT is minted for the "Run checks as" user.
    run_as = uuid.uuid4()
    added, seen, _ = await _wire(
        monkeypatch, ("get_opencti_platform_version",), run_as=run_as, run_user=str(uuid.uuid4())
    )
    assert added == 1
    assert seen["context"]["user_id"] == str(run_as)
    assert seen["checked"] == (run_as, seen["integration_id"])


async def test_no_run_as_user_wires_nothing_and_names_the_setting(monkeypatch):
    # No fallback: no service account, no seed owner, no administrator by default.
    added, seen, composite = await _wire(
        monkeypatch, ("get_opencti_platform_version",), run_as=None, run_user=str(uuid.uuid4())
    )
    assert added == 0
    assert "context" not in seen
    reason = composite.withheld_integrations["opencti"]
    assert reason.startswith("Checks need a run-as user with OpenCTI access")


async def test_a_run_as_user_without_module_access_wires_nothing(monkeypatch):
    added, seen, composite = await _wire(
        monkeypatch, ("get_opencti_platform_version",), run_as=uuid.uuid4(), reaches=False
    )
    assert added == 0
    assert "context" not in seen
    assert "run-as user with OpenCTI access" in composite.withheld_integrations["opencti"]


async def test_zero_wired_tools_is_named_a_platform_defect(monkeypatch):
    added, _, composite = await _wire(monkeypatch, (), run_as=uuid.uuid4())
    assert added == 0
    assert composite.withheld_integrations == {"opencti": health_modules.MODULE_TOOLS_NOT_WIRED}
    assert "not the checker's allow-list" in health_modules.MODULE_TOOLS_NOT_WIRED


def test_the_service_account_fallback_is_gone():
    assert not hasattr(health_modules, "module_reader_user_id")
