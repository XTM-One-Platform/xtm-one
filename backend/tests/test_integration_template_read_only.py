"""Read-only template bindings: ``Agent.integration_template_read_only``.

An agent that binds a provider by template (``integration_template_bindings``,
resolved per user at run time) marks it read-only by provider key. Pinned here
without a database: the resolution of the read-only ids from the bindings a
run resolved, the parser and the refusal of an untemplated provider, the REST
schema and the agent-builder tools, every run path handing the resolved
bindings to the resolver, the copy and the seed.
"""

from __future__ import annotations

import inspect
import uuid

import pytest
from fastapi import HTTPException

from app.agents.opencti_pir_desk import PIR_DESK_AGENTS, TRIAGE_ANALYST
from app.api.v1 import agents as agents_api
from app.api.v1.schemas.agent import CreateAgentRequest, UpdateAgentRequest
from app.models.agent import Agent
from app.models.agent_copy import (
    AGENT_BUNDLE_PORTABLE_FIELDS,
    AGENT_CATALOG_CLONE_FIELDS,
    AGENT_COPY_FIELDS,
)
from app.services import (
    agent_chat,
    assignment_engine,
    evaluation_engine,
    scheduled_work_item_runner,
)
from app.services.builtin_tools import build_agent_builder_tool
from app.services.integration_read_only import (
    parse_integration_template_read_only,
    read_only_integration_ids,
    template_read_only_providers,
    untemplated_read_only_providers,
    withhold_write_tools,
)
from app.tools.base import (
    TOOL_NOT_AVAILABLE_SENTINEL,
    CallableToolExecutor,
    CompositeToolExecutor,
    ScopedToolExecutor,
    ToolCall,
    ToolDefinition,
)

HUBSPOT_USER, HUBSPOT_OWN, SLACK = (str(uuid.uuid4()) for _ in range(3))


def _agent(**fields) -> Agent:
    return Agent(**{"integration_template_bindings": ["hubspot", "slack"], **fields})


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_a_read_only_template_provider_marks_the_binding_resolved_for_it():
    agent = _agent(integration_template_read_only=["hubspot"])
    resolved = {"hubspot": HUBSPOT_USER, "slack": [SLACK]}
    assert read_only_integration_ids(agent, resolved) == {HUBSPOT_USER}


def test_every_instance_of_a_read_only_template_provider_is_read_only():
    second = str(uuid.uuid4())
    agent = _agent(integration_template_read_only=["hubspot"])
    resolved = {"hubspot": [HUBSPOT_USER], "hubspot#2": [second]}
    assert read_only_integration_ids(agent, resolved) == {HUBSPOT_USER, second}


def test_without_the_field_a_template_binding_keeps_its_write_tools():
    agent = _agent()
    assert read_only_integration_ids(agent, {"hubspot": HUBSPOT_USER}) == frozenset()


def test_an_own_id_binding_of_the_same_provider_stays_as_its_own_marks_say():
    """The template mark narrows the per-user resolution, never a binding
    the agent chose by id (which integration_read_only governs)."""
    agent = _agent(
        integration_bindings={"hubspot": [HUBSPOT_OWN]},
        integration_template_read_only=["hubspot"],
    )
    resolved = {"hubspot": [HUBSPOT_OWN, HUBSPOT_USER]}
    assert read_only_integration_ids(agent, resolved) == {HUBSPOT_USER}


def test_integration_read_only_by_id_is_unchanged():
    agent = Agent(
        integration_bindings={"hubspot": [HUBSPOT_OWN]},
        integration_read_only={"hubspot": [HUBSPOT_OWN]},
    )
    assert read_only_integration_ids(agent) == {HUBSPOT_OWN}
    assert read_only_integration_ids(agent, {"hubspot": [HUBSPOT_OWN]}) == {HUBSPOT_OWN}


def test_a_stale_provider_no_longer_template_bound_narrows_nothing():
    agent = Agent(integration_template_bindings=["slack"], integration_template_read_only=["hubspot"])
    assert template_read_only_providers(agent) == frozenset()
    assert read_only_integration_ids(agent, {"hubspot": HUBSPOT_USER}) == frozenset()


def test_dropping_a_template_binding_prunes_its_read_only_mark():
    agent = _agent(integration_template_read_only=["hubspot", "slack"])
    agent.integration_template_bindings = ["slack"]
    agent.prune_orphaned_integration_template_read_only()
    assert agent.integration_template_read_only == ["slack"]


# ---------------------------------------------------------------------------
# Enforcement: the withheld writes answer the read-only reason
# ---------------------------------------------------------------------------


async def test_a_read_only_template_binding_lists_reads_and_refuses_writes():
    executor = CallableToolExecutor()
    ran: list[str] = []

    async def _handler(**_kwargs):
        ran.append("ran")
        return "done"

    names = ("get_hubspot_contact", "create_hubspot_contact")
    for name in names:
        executor.register(name, _handler)
    defs = [ToolDefinition(name=n, description=n, input_schema={"type": "object"}) for n in names]

    kept = withhold_write_tools(defs, executor, integration_label="HubSpot")

    assert [d.name for d in kept] == ["get_hubspot_contact"]
    refused = await executor.execute_tool(ToolCall(id="1", name="create_hubspot_contact", arguments={}))
    assert refused.is_error and "read-only" in refused.content
    assert ran == []


async def test_the_read_only_refusal_reaches_the_model_through_the_scoped_executor():
    """The run paths wrap the composite in a ScopedToolExecutor of the offered
    tools: a withheld write is not offered, so the call must still be answered
    with the read-only refusal, never "not available in this context"."""
    executor = CallableToolExecutor()
    ran: list[str] = []

    async def _handler(**_kwargs):
        ran.append("ran")
        return "done"

    names = ("list_opencti_notes", "create_opencti_note")
    for name in names:
        executor.register(name, _handler)
    defs = [ToolDefinition(name=n, description=n, input_schema={"type": "object"}) for n in names]
    kept = withhold_write_tools(defs, executor, integration_label="OpenCTI")

    composite = CompositeToolExecutor()
    composite.add_tools(kept, executor)
    scoped = ScopedToolExecutor(composite, composite.all_tools)

    for runner in (composite, scoped):
        refused = await runner.execute_tool(ToolCall(id="1", name="create_opencti_note", arguments={}))
        assert refused.is_error
        assert refused.content.startswith("Refused: create_opencti_note writes to OpenCTI")
        assert TOOL_NOT_AVAILABLE_SENTINEL not in refused.content
    assert ran == []

    unknown = await scoped.execute_tool(ToolCall(id="2", name="delete_opencti_everything", arguments={}))
    assert unknown.is_error and TOOL_NOT_AVAILABLE_SENTINEL in unknown.content
    read = await scoped.execute_tool(ToolCall(id="3", name="list_opencti_notes", arguments={}))
    assert not read.is_error and ran == ["ran"]


def test_a_name_registered_again_is_no_longer_refused():
    executor = CallableToolExecutor()
    executor.refuse("create_opencti_note", "Refused: test")
    assert executor.refusals == {"create_opencti_note": "Refused: test"}

    async def _handler(**_kwargs):
        return "done"

    executor.register("create_opencti_note", _handler)
    assert executor.refusals == {}


# ---------------------------------------------------------------------------
# Every run path resolves the template marks from its resolved bindings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "module",
    [agent_chat, assignment_engine, evaluation_engine, scheduled_work_item_runner],
    ids=["agent_chat", "assignment_engine", "evaluation_engine", "scheduled_work_item_runner"],
)
def test_every_run_path_hands_its_resolved_bindings_to_the_resolver(module):
    """Tripwire: ``read_only_integration_ids(agent)`` alone in a run path
    that resolves template bindings gives a read-only provider its writes."""
    source = inspect.getsource(module)
    assert "read_only_integration_ids(agent)" not in source, module.__name__


# ---------------------------------------------------------------------------
# Setting it: parser, REST, agent-builder tools
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["hubspot, Ashby", '["hubspot", "ashby"]', ["hubspot", "ashby"]])
def test_the_parser_reads_every_shape(raw):
    assert parse_integration_template_read_only(raw) == (["hubspot", "ashby"], None, True)


@pytest.mark.parametrize(("raw", "provided"), [("", False), (None, False), ("clear", True), ("none", True), ([], True)])
def test_blank_leaves_and_clear_clears(raw, provided):
    assert parse_integration_template_read_only(raw) == (None, None, provided)


def test_the_parser_refuses_a_non_list():
    value, error, provided = parse_integration_template_read_only({"hubspot": True})
    assert value is None and provided and error


def test_an_untemplated_provider_is_refused_with_a_clear_message():
    assert untemplated_read_only_providers(["hubspot", "jira"], ["hubspot"]) == ["jira"]
    with pytest.raises(HTTPException) as exc:
        agents_api._checked_integration_template_read_only(["jira"], ["hubspot"])
    assert exc.value.status_code == 422
    assert "jira" in exc.value.detail and "integration_template_bindings" in exc.value.detail


def test_the_rest_schemas_carry_and_normalize_the_field():
    assert CreateAgentRequest.model_validate(
        {"name": "x", "integration_template_bindings": ["hubspot"], "integration_template_read_only": [" HubSpot "]}
    ).integration_template_read_only == ["hubspot"]
    assert UpdateAgentRequest.model_validate({"integration_template_read_only": []}).integration_template_read_only is None
    assert "integration_template_read_only" in agents_api._NULL_CLEARED_FIELDS


def test_create_and_update_agent_tools_take_the_field():
    definitions, executor = build_agent_builder_tool(None, str(uuid.uuid4()))
    by_name = {d.name: d for d in definitions}
    for tool in ("create_agent", "update_agent"):
        assert "integration_template_read_only" in by_name[tool].input_schema["properties"], tool
        assert "integration_template_read_only" in inspect.signature(executor.handler(tool)).parameters, tool


# ---------------------------------------------------------------------------
# Copy and seed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fields", [AGENT_COPY_FIELDS, AGENT_BUNDLE_PORTABLE_FIELDS, AGENT_CATALOG_CLONE_FIELDS]
)
def test_every_copy_keeps_the_read_only_template_providers(fields):
    assert "integration_template_read_only" in fields


def test_seeded_template_read_only_providers_are_template_bound():
    from app.agents.builtin_agents import BUILTIN_AGENTS

    for data in [*BUILTIN_AGENTS, *PIR_DESK_AGENTS]:
        marked = data.get("integration_template_read_only") or []
        assert not untemplated_read_only_providers(marked, data.get("integration_template_bindings")), data["slug"]


def test_the_pir_triage_analyst_only_reads_opencti():
    triage = next(a for a in PIR_DESK_AGENTS if a["slug"] == TRIAGE_ANALYST)
    assert triage["integration_template_read_only"] == ["opencti"]
