"""Agent-builder and knowledge-search gaps found building a delegated agent.

What these pin without a database (the PostgreSQL half is
``test_emt_agent_builder_postgres.py``):

* ``create_agent`` takes every field ``update_agent`` takes - signature and
  tool schema - so one call builds a complete agent;
* read-only integration bindings: the setting's parser, the one read/write
  classifier over every tool of the provider registry, write tools withheld
  AND refused when called by name, every agent call site passing the ids;
* ``spawn_background_task``: an agent holding the agent-builder tools reaches
  the agents its user owns, every other refusal names what is missing;
* ``run_agent_once`` runs an agent without creating an assignment;
* ``search_knowledge`` diversifies across knowledge bases, takes a
  ``knowledge_base`` filter and names the knowledge base of every hit.
"""

from __future__ import annotations

import inspect
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest

import app.services.builtin_tools as builtin_tools_module
from app.api.v1 import public_catalog as public_catalog_api
from app.api.v1 import public_chat
from app.models.agent import Agent
from app.services import agent_chat, assignment_engine, evaluation_engine, sub_agent, vector
from app.services.builtin_tools import build_agent_builder_tool, build_sub_agent_tools
from app.services.builtin_tools.agent_builder import (
    UPDATE_AGENT_ONLY_FIELDS,
    _parse_group_sharing_args,
)
from app.services.builtin_tools.handover import owned_target_resolver, resolve_owned_delegation_target
from app.services.integration_read_only import (
    READ_ONLY_CONTEXT_KEY,
    carried_read_only,
    integration_tool_access,
    parse_integration_read_only,
    read_only_integration_ids,
    unbound_read_only_entries,
    withhold_write_tools,
)
from app.tools.base import CallableToolExecutor, ToolCall, ToolDefinition

LINKEDIN_A, LINKEDIN_B = str(uuid.uuid4()), str(uuid.uuid4())
PLANHAT = str(uuid.uuid4())


def _builder():
    definitions, executor = build_agent_builder_tool(None, str(uuid.uuid4()))
    return {d.name: d for d in definitions}, executor


async def _call(executor, name: str, **arguments):
    return await executor.execute_tool(ToolCall(id="call", name=name, arguments=arguments))


# ---------------------------------------------------------------------------
# create_agent: every update_agent field
# ---------------------------------------------------------------------------


def test_create_agent_takes_every_field_update_agent_takes():
    definitions, executor = _builder()
    update_params = set(inspect.signature(executor.handler("update_agent")).parameters)
    create_params = set(inspect.signature(executor.handler("create_agent")).parameters)
    assert update_params - UPDATE_AGENT_ONLY_FIELDS <= create_params

    update_schema = set(definitions["update_agent"].input_schema["properties"])
    create_schema = set(definitions["create_agent"].input_schema["properties"])
    assert update_schema - UPDATE_AGENT_ONLY_FIELDS <= create_schema
    assert {
        "llm_model",
        "llm_provider_id",
        "temperature",
        "reasoning_effort",
        "max_iterations",
        "builtin_tool_ids",
        "memory_short_term_enabled",
        "memory_medium_term_enabled",
        "memory_persistent_enabled",
        "memory_user_enabled",
        "available_channel_types",
        "tool_deny_list",
        "integration_allowed_tools",
        "output_format",
        "integration_read_only",
    } <= create_schema
    # create_agent keeps its own typed fields rather than update_agent's strings.
    assert definitions["create_agent"].input_schema["properties"]["max_iterations"]["type"] == [
        "integer",
        "null",
    ]


def test_the_new_builder_tools_are_declared_and_registered():
    definitions, executor = _builder()
    for name in ("clone_agent", "list_integration_tools", "run_agent_once"):
        assert name in definitions
        assert name in executor.tool_names


# ---------------------------------------------------------------------------
# Read-only bindings: the setting
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        f"linkedin:{LINKEDIN_A}, planhat:{PLANHAT}",
        f'["linkedin:{LINKEDIN_A}", "planhat:{PLANHAT}"]',
        [f"LinkedIn:{LINKEDIN_A}", f"planhat:{PLANHAT}"],
        {"linkedin": [LINKEDIN_A], "planhat": PLANHAT},
    ],
    ids=["csv", "json-array", "list", "object"],
)
def test_integration_read_only_reads_every_shape(raw):
    value, error, provided = parse_integration_read_only(raw)
    assert (error, provided) == (None, True)
    assert value == {"linkedin": [LINKEDIN_A], "planhat": [PLANHAT]}


@pytest.mark.parametrize(("raw", "provided"), [("", False), ("  ", False), (None, False), ("clear", True), ([], True)])
def test_integration_read_only_blank_leaves_and_clear_clears(raw, provided):
    value, error, was_provided = parse_integration_read_only(raw)
    assert (value, error, was_provided) == (None, None, provided)


@pytest.mark.parametrize("raw", ["linkedin", "linkedin:not-a-uuid", "{not json"])
def test_integration_read_only_refuses_a_malformed_entry(raw):
    value, error, provided = parse_integration_read_only(raw)
    assert value is None and provided and error


def test_only_bound_integrations_are_read_only():
    agent = Agent(
        integration_bindings={"linkedin": [LINKEDIN_A, LINKEDIN_B]},
        integration_read_only={"linkedin": [LINKEDIN_A], "planhat": [PLANHAT]},
    )
    assert read_only_integration_ids(agent) == {LINKEDIN_A}
    assert unbound_read_only_entries(agent.integration_read_only, agent.integration_bindings) == [
        f"planhat:{PLANHAT}"
    ]


def test_a_catalog_clone_carries_the_read_only_mark_onto_the_installers_choice():
    """A kept binding stays read-only; a replaced provider's choice takes its mark."""
    installer_planhat, installer_slack = str(uuid.uuid4()), str(uuid.uuid4())
    source_read_only = {"linkedin": [LINKEDIN_A], "planhat": [PLANHAT]}
    final_bindings = {
        "linkedin": [LINKEDIN_A, LINKEDIN_B],
        "planhat": [installer_planhat],
        "slack": [installer_slack],
    }

    carried = carried_read_only(source_read_only, final_bindings, replaced_providers={"planhat", "slack"})

    assert carried == {"linkedin": [LINKEDIN_A], "planhat": [installer_planhat]}
    assert carried_read_only(source_read_only, {"slack": [installer_slack]}) is None
    assert carried_read_only(None, final_bindings, replaced_providers={"planhat"}) is None


def test_the_catalog_clone_re_keys_the_read_only_map_with_the_bindings():
    source = inspect.getsource(public_catalog_api.add_catalog_item_to_account)
    assert "integration_read_only=carried_read_only(" in source
    assert "replaced_providers=set(chosen_bindings)" in source


def test_a_built_in_agent_may_clear_its_read_only_map_with_null():
    """The built-in carve-out reads an explicit null as clearing it."""
    from app.api.v1.agents import _is_integration_binding_change
    from app.api.v1.schemas.agent import UpdateAgentRequest

    agent = Agent(integration_read_only={"linkedin": [LINKEDIN_A]})
    assert _is_integration_binding_change(agent, UpdateAgentRequest(integration_read_only=None))
    assert not _is_integration_binding_change(Agent(), UpdateAgentRequest(integration_read_only=None))


def test_unbinding_an_integration_drops_its_read_only_mark():
    agent = Agent(
        integration_bindings={"linkedin": [LINKEDIN_A, LINKEDIN_B]},
        integration_read_only={"linkedin": [LINKEDIN_A, LINKEDIN_B]},
    )
    agent.integration_bindings = {"linkedin": [LINKEDIN_B]}
    agent.prune_orphaned_integration_scopes()
    assert agent.integration_read_only == {"linkedin": [LINKEDIN_B]}
    agent.integration_bindings = {}
    agent.prune_orphaned_integration_scopes()
    assert agent.integration_read_only is None


# ---------------------------------------------------------------------------
# Read-only bindings: the classification and its enforcement
# ---------------------------------------------------------------------------

#: The verbs the issue names as writes.
_WRITE_VERBS = {"create", "update", "delete", "send", "post", "transition", "upload"}


def test_every_tool_of_the_provider_registry_is_read_or_write():
    """The one classifier names every integration tool's access, and agrees
    with the catalog badge; a name made of a write verb is never a read."""
    from app.api.v1.integration_catalog import build_dummy_token, build_provider_tools
    from app.services.integration_tools import PROVIDER_TOOLS
    from app.services.tool_classification import READ_VERBS
    from app.tools.base import CallableToolExecutor

    catalogue = build_provider_tools()
    assert sum(len(tools) for tools in catalogue.values()) > 500
    for provider, tools in catalogue.items():
        # A definition declared read_only is a read whatever its verb: the
        # badge and a read-only binding (withhold_write_tools) both trust it.
        builder: Any = PROVIDER_TOOLS[provider]
        declared = {
            d.name
            for d in builder(build_dummy_token(provider), CallableToolExecutor(), refresh_fn=lambda: None)
            if d.read_only
        }
        for tool in tools:
            access = "read" if tool.name in declared else integration_tool_access(tool.name)
            assert access in ("read", "write")
            assert access == tool.tool_type, (provider, tool.name)
            tokens = tool.name.lower().split("_")
            if tokens[0] in _WRITE_VERBS or (
                tokens[0] not in READ_VERBS and _WRITE_VERBS & set(tokens)
            ):
                assert access == "write", (provider, tool.name)


@pytest.mark.asyncio
async def test_list_integration_tools_reports_each_tools_access():
    _definitions, executor = _builder()

    result = await _call(executor, "list_integration_tools", provider="LinkedIn")

    assert "**LinkedIn** (`linkedin`)" in result.content
    assert "  - get_linkedin_profile [read]" in result.content
    assert "  - list_linkedin_posts [read]" in result.content
    assert "  - create_linkedin_post [write]" in result.content
    assert "  - delete_linkedin_post [write]" in result.content
    unknown = await _call(executor, "list_integration_tools", provider="nope")
    assert "Unknown provider 'nope'" in unknown.content


@pytest.mark.asyncio
async def test_list_integration_tools_honours_a_read_only_declaration() -> None:
    """A hunt declared read_only is a read whatever its verb, as the catalog
    badge and a read-only binding already say."""
    _definitions, executor = _builder()

    result = await _call(executor, "list_integration_tools", provider="defender")

    assert "  - run_defender_hunting_query [read]" in result.content
    assert "  - create_defender_indicator [write]" in result.content


CISO = str(uuid.uuid4())
BARE = str(uuid.uuid4())


def _openapi_tool(name: str, method: str, path: str) -> dict:
    return {
        "name": name,
        "description": name,
        "execution": {"method": method, "path": path},
        "openapi": {"operation": f"{method} {path}", "sends_credential": True},
    }


_CISO_TOOLS = [
    _openapi_tool("ciso_list_risks", "GET", "/risks"),
    _openapi_tool("ciso_create_risk", "POST", "/risks"),
    _openapi_tool("ciso_recompute_risks", "GET", "/risks/recompute"),
]


@pytest.mark.asyncio
async def test_list_integration_tools_lists_each_openapi_integrations_imported_operations(monkeypatch):
    """The openapi provider's tools live per integration, in its imported spec."""
    from app.services.builtin_tools import agent_builder

    calls: list[object] = []

    async def _listing(user_id, provider="", integration_id=None):
        calls.append(integration_id)
        rows = [(CISO, "CISO assistant", "openapi", _CISO_TOOLS), (BARE, "Bare", "openapi", None)]
        if integration_id is None:
            assert provider == "openapi"
            return rows
        return [row for row in rows if row[0] == str(integration_id)]

    monkeypatch.setattr(agent_builder, "_load_imported_tool_listing", _listing)
    _definitions, executor = _builder()

    result = await _call(executor, "list_integration_tools", provider="openapi")
    content = result.content
    assert f"**CISO assistant** (`{CISO}`) - 3 tools (2 read, 1 write):" in content
    assert "  - ciso_list_risks [read] `GET /risks`" in content
    assert "  - ciso_create_risk [write] `POST /risks`" in content
    # The access is the one the read-only binding enforces: the HTTP method,
    # never the generated name (a GET named recompute stays a read).
    assert "  - ciso_recompute_risks [read] `GET /risks/recompute`" in content
    assert f"**Bare** (`{BARE}`): no imported operation" in content
    assert "has no tools" not in content

    one = await _call(executor, "list_integration_tools", integration_id=CISO)
    assert f"**CISO assistant** (`{CISO}`) - 3 tools" in one.content
    assert "Bare" not in one.content
    assert str(calls[-1]) == CISO

    bare = await _call(executor, "list_integration_tools", integration_id=BARE)
    assert "has no tools: no spec is imported" in bare.content
    missing = await _call(executor, "list_integration_tools", integration_id=str(uuid.uuid4()))
    assert "not found, or you cannot see it" in missing.content
    invalid = await _call(executor, "list_integration_tools", integration_id="nope")
    assert "is not a valid UUID" in invalid.content


@pytest.mark.asyncio
async def test_list_integration_tools_openapi_without_imported_spec_has_no_tools(monkeypatch):
    from app.services.builtin_tools import agent_builder

    async def _none(user_id, provider="", integration_id=None):
        return []

    monkeypatch.setattr(agent_builder, "_load_imported_tool_listing", _none)
    _definitions, executor = _builder()

    result = await _call(executor, "list_integration_tools", provider="openapi")
    assert "(`openapi`) has no tools" in result.content


@pytest.mark.asyncio
async def test_list_integration_tools_by_id_of_a_catalog_provider_lists_the_catalog(monkeypatch):
    from app.services.builtin_tools import agent_builder

    async def _linkedin(user_id, provider="", integration_id=None):
        return [(LINKEDIN_A, "Corp LinkedIn", "linkedin", None)]

    monkeypatch.setattr(agent_builder, "_load_imported_tool_listing", _linkedin)
    _definitions, executor = _builder()

    result = await _call(executor, "list_integration_tools", integration_id=LINKEDIN_A)
    assert "**LinkedIn** (`linkedin`)" in result.content
    assert "  - create_linkedin_post [write]" in result.content


def _definition(name: str, base: str | None = None) -> ToolDefinition:
    return ToolDefinition(name=name, description=name, base_name=base, source="integration:fake")


@pytest.mark.asyncio
async def test_a_read_only_binding_keeps_its_reads_and_refuses_its_writes():
    executor = CallableToolExecutor()
    ran: list[str] = []

    async def _handler(**_kwargs):
        ran.append("ran")
        return "done"

    for name in ("get_fake_record__acme", "create_fake_record__acme", "send_fake_message__acme"):
        executor.register(name, _handler)
    defs = [
        _definition("get_fake_record__acme", "get_fake_record"),
        _definition("create_fake_record__acme", "create_fake_record"),
        _definition("send_fake_message__acme", "send_fake_message"),
    ]

    kept = withhold_write_tools(defs, executor, integration_label="the fake integration 'Acme'")

    assert [d.name for d in kept] == ["get_fake_record__acme"]
    refused = await executor.execute_tool(ToolCall(id="1", name="create_fake_record__acme", arguments={}))
    assert refused.is_error
    assert refused.content.startswith("Refused: create_fake_record__acme writes to the fake integration 'Acme'")
    read = await executor.execute_tool(ToolCall(id="2", name="get_fake_record__acme", arguments={}))
    assert read.content == "done"
    assert ran == ["ran"], "no write handler may run"


@pytest.mark.parametrize(
    "module",
    [agent_chat, assignment_engine, evaluation_engine, public_chat],
    ids=["agent_chat", "assignment_engine", "evaluation_engine", "public_chat"],
)
def test_every_agent_run_hands_its_read_only_bindings_to_the_tool_builder(module):
    """Tripwire: an agent's integration tools built without its read-only ids
    would hand a read-only binding its write tools again."""
    source = inspect.getsource(module)
    starts = [i for i in range(len(source)) if source.startswith("await build_integration_tools(", i)]
    assert starts
    for start in starts:
        window = source[max(0, start - 800) : start + 1600]
        assert READ_ONLY_CONTEXT_KEY in window, f"{module.__name__} call at {start}"


# ---------------------------------------------------------------------------
# spawn_background_task: owned targets and named refusals
# ---------------------------------------------------------------------------


def test_only_an_agent_builder_agent_resolves_owned_agents():
    user = str(uuid.uuid4())
    assert owned_target_resolver(["web_fetch"], user) is None
    assert owned_target_resolver(["agent_builder"], "anonymous") is None
    resolver = owned_target_resolver(["agent_builder"], user)
    assert resolver is not None
    assert resolver.func is resolve_owned_delegation_target
    assert resolver.keywords == {"user_id": user}


def _owned(name: str = "EMT Executive Assistant", *, enabled: bool = True) -> Agent:
    return Agent(id=uuid.uuid4(), name=name, slug="emt-executive-assistant", enabled=enabled)


def _spawn_tools(monkeypatch, owned: Agent | None, **kwargs):
    created: list[dict] = []

    async def _resolver(reference: str):
        if owned is not None and reference in (str(owned.id), owned.slug):
            return owned
        return None

    async def _count(**_kwargs):
        return 0

    async def _create(_db, **task):
        created.append(task)
        return SimpleNamespace(id=uuid.uuid4(), status="pending")

    monkeypatch.setattr(sub_agent, "count_active_sub_agent_executions", _count)
    monkeypatch.setattr(sub_agent, "create_sub_agent_task", _create)
    built = build_sub_agent_tools(
        None,
        str(uuid.uuid4()),
        str(uuid.uuid4()),
        [],
        resolve_owned_target=_resolver,
        **kwargs,
    )
    assert built is not None, "a builder agent with no peers still gets the tool"
    return built, created


@pytest.mark.asyncio
async def test_a_builder_agent_delegates_to_an_agent_it_just_created(monkeypatch):
    owned = _owned()
    (definitions, executor), created = _spawn_tools(monkeypatch, owned)

    result = await executor.execute_tool(
        ToolCall(id="1", name="spawn_background_task", arguments={"agent_id": str(owned.id), "task": "Brief me."})
    )

    assert result.content.startswith("Background task spawned successfully."), result.content
    assert "Target agent: EMT Executive Assistant" in result.content
    assert created[0]["target_agent_id"] == str(owned.id)
    spawn = next(d for d in definitions if d.name == "spawn_background_task")
    assert "any agent you own" in spawn.input_schema["properties"]["agent_id"]["description"]


@pytest.mark.asyncio
async def test_an_owned_peer_of_another_type_is_still_refused(monkeypatch):
    owned = _owned()
    (_definitions, executor), created = _spawn_tools(
        monkeypatch, owned, handover_type_map={str(owned.id): "consult"}
    )

    result = await executor.execute_tool(
        ToolCall(id="1", name="spawn_background_task", arguments={"agent_id": owned.slug, "task": "Brief me."})
    )

    assert result.content == (
        f"Agent {owned.slug} is not available for background tasks: it is a consult peer of "
        "this agent - use consult_agent, or change its handover type to delegate."
    )
    assert created == []


@pytest.mark.asyncio
async def test_a_disabled_or_unknown_target_is_refused_naming_why(monkeypatch):
    owned = _owned(enabled=False)
    (_definitions, executor), created = _spawn_tools(monkeypatch, owned)

    disabled = await executor.execute_tool(
        ToolCall(id="1", name="spawn_background_task", arguments={"agent_id": str(owned.id), "task": "x"})
    )
    unknown = await executor.execute_tool(
        ToolCall(id="2", name="spawn_background_task", arguments={"agent_id": "someone-else", "task": "x"})
    )

    assert disabled.content.endswith("the agent is disabled - enable it (update_agent enabled=true) first.")
    assert unknown.content.startswith(
        "Agent someone-else is not available for background tasks: it is neither a delegate "
        "peer of this agent nor an agent you own"
    )
    assert created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reference", ["self", "id", "slug"])
async def test_its_own_id_or_slug_names_the_disabled_self_fan_out(monkeypatch, reference):
    """With self fan-out off, the agent's own id or slug is refused naming the
    switch, never resolved as an owned agent and refused as a cycle."""
    builder = _owned("CEO Assistant")
    (_definitions, executor), created = _spawn_tools(monkeypatch, builder, self_agent=builder)
    agent_ref = {"self": "self", "id": str(builder.id), "slug": builder.slug.upper()}[reference]
    builder.allow_self_fanout = False

    result = await executor.execute_tool(
        ToolCall(id="1", name="spawn_background_task", arguments={"agent_id": agent_ref, "task": "x"})
    )

    assert result.content == (
        f"Agent {agent_ref} is not available for background tasks: running a copy of yourself "
        "is turned off for this agent (allow_self_fanout) or for the platform "
        "(AGENT_SELF_FANOUT_ENABLED)."
    )
    assert created == []


@pytest.mark.asyncio
async def test_a_delegated_run_is_told_it_is_below_the_top_level(monkeypatch: pytest.MonkeyPatch) -> None:
    """A background copy asking for a copy of itself is told why: its depth, not a switch."""
    builder = _owned("CEO Assistant")
    (_definitions, executor), created = _spawn_tools(monkeypatch, builder, self_agent=builder, delegation_depth=1)

    result = await executor.execute_tool(
        ToolCall(id="1", name="spawn_background_task", arguments={"agent_id": "self", "task": "x"})
    )

    assert result.content == (
        "Agent self is not available for background tasks: this run is itself a delegated run "
        "(delegation depth 1), and only a top-level run may spawn copies of itself: do the work "
        "in this run, or return what is left to the run that spawned you."
    )
    assert "allow_self_fanout" not in result.content
    assert created == []


@pytest.mark.asyncio
async def test_switches_on_but_not_offered_does_not_blame_the_switches(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both switches on at the top level: the turn did not offer it, no switch is named."""
    builder = _owned("CEO Assistant")
    (_definitions, executor), created = _spawn_tools(monkeypatch, builder, self_agent=builder)

    result = await executor.execute_tool(
        ToolCall(id="1", name="spawn_background_task", arguments={"agent_id": "self", "task": "x"})
    )

    assert result.content == (
        "Agent self is not available for background tasks: running a copy of yourself is not "
        "offered on this turn: it needs handover allowed on the turn and the delegate handover "
        "type offered."
    )
    assert created == []


@pytest.mark.asyncio
async def test_resolving_an_owned_agent_needs_a_user():
    assert await resolve_owned_delegation_target("emt", user_id="anonymous") is None
    assert await resolve_owned_delegation_target("", user_id=str(uuid.uuid4())) is None


# ---------------------------------------------------------------------------
# run_agent_once
# ---------------------------------------------------------------------------


def _run_once(monkeypatch, statuses: list[str]):
    agent = _owned()
    started: list[dict] = []
    task_id = uuid.uuid4()

    async def _writable(_db, _uid, _agent_uuid, *, action="modify", assignment_write=False):
        return agent, None

    async def _create(_db, **task):
        started.append(task)
        return SimpleNamespace(id=task_id, status="pending")

    async def _wait(_ids, _user, **_kwargs):
        return {}

    async def _result(_db, _task_id, _user):
        status = statuses.pop(0)
        return {"status": status, "result": "Here is your brief.", "error": None}

    @asynccontextmanager
    async def _session():
        yield SimpleNamespace()

    monkeypatch.setattr(builtin_tools_module, "_load_writable_agent_for_builder", _writable)
    monkeypatch.setattr("app.core.database.async_session", _session)
    monkeypatch.setattr(sub_agent, "create_sub_agent_task", _create)
    monkeypatch.setattr(sub_agent, "wait_for_tasks", _wait)
    monkeypatch.setattr(sub_agent, "get_task_result", _result)
    _definitions, executor = build_agent_builder_tool(None, str(uuid.uuid4()))
    return executor, agent, started, task_id


@pytest.mark.asyncio
async def test_run_agent_once_answers_without_an_assignment(monkeypatch):
    executor, agent, started, task_id = _run_once(monkeypatch, ["completed"])

    result = await _call(executor, "run_agent_once", agent_id=str(agent.id), prompt="Give me today's brief.")

    assert result.content == f"Run `{task_id}` completed. The agent answered:\n\nHere is your brief."
    assert started == [
        {
            "user_id": started[0]["user_id"],
            "parent_agent_id": str(agent.id),
            "target_agent_id": str(agent.id),
            "task_description": "Give me today's brief.",
            "allow_self": True,
            "extra_metadata": {"origin": "run_agent_once"},
            # A run of its own, hence a sandbox workspace (#12028).
            "own_run_trigger": "run_once",
            "parent_run_id": None,
        }
    ]


@pytest.mark.asyncio
async def test_run_agent_once_names_the_stored_run_beside_the_task(monkeypatch):
    # The run get_run reads, not the task id alone (#12095).
    executor, agent, _started, task_id = _run_once(monkeypatch, ["completed"])
    run_id = uuid.uuid4()

    async def _result(_db, _task_id, _user):
        return {"status": "completed", "result": "ok", "error": None, "run_id": str(run_id)}

    monkeypatch.setattr(sub_agent, "get_task_result", _result)

    result = await _call(executor, "run_agent_once", agent_id=str(agent.id), prompt="Brief.")

    assert result.content.startswith(f"Run `{run_id}` (task `{task_id}`) completed.")


@pytest.mark.asyncio
async def test_run_agent_once_from_a_run_makes_the_run_its_parent(monkeypatch):
    agent = _owned()
    started: list[dict] = []
    caller_run = uuid.uuid4()

    async def _writable(_db, _uid, _agent_uuid, *, action="modify", assignment_write=False):
        return agent, None

    async def _create(_db, **task):
        started.append(task)
        return SimpleNamespace(id=uuid.uuid4(), status="pending")

    async def _wait(_ids, _user, **_kwargs):
        return {}

    async def _result(_db, _task_id, _user):
        return {"status": "completed", "result": "ok", "error": None}

    @asynccontextmanager
    async def _session():
        yield SimpleNamespace()

    monkeypatch.setattr(builtin_tools_module, "_load_writable_agent_for_builder", _writable)
    monkeypatch.setattr("app.core.database.async_session", _session)
    monkeypatch.setattr(sub_agent, "create_sub_agent_task", _create)
    monkeypatch.setattr(sub_agent, "wait_for_tasks", _wait)
    monkeypatch.setattr(sub_agent, "get_task_result", _result)
    _definitions, executor = build_agent_builder_tool(None, str(uuid.uuid4()), run_id=str(caller_run))

    await _call(executor, "run_agent_once", agent_id=str(agent.id), prompt="Brief.")

    assert started[0]["parent_run_id"] == str(caller_run)
    assert started[0]["own_run_trigger"] == "run_once"


@pytest.mark.asyncio
async def test_a_long_run_is_collected_with_its_task_id(monkeypatch):
    executor, agent, started, task_id = _run_once(monkeypatch, ["running", "completed"])

    first = await _call(executor, "run_agent_once", agent_id=str(agent.id), prompt="Brief.")
    assert f"run_agent_once(task_id='{task_id}')" in first.content

    second = await _call(executor, "run_agent_once", task_id=str(task_id))
    assert "Here is your brief." in second.content
    assert len(started) == 1, "collecting never starts a second run"


def _attachable(monkeypatch, refs: list, refused: list[str]):
    seen: list[dict] = []

    async def _resolve(file_ids, **kwargs):
        seen.append({"file_ids": list(file_ids), **kwargs})
        return SimpleNamespace(refs=refs, refused=refused)

    monkeypatch.setattr("app.services.work_item_files.resolve_attachable_files", _resolve)
    return seen


@pytest.mark.asyncio
async def test_run_agent_once_attaches_the_files_to_the_run_message(monkeypatch):
    from app.llm.base import FileRef
    from app.services.sub_agent_files import TURN_FILE_IDS_KEY

    executor, agent, started, _task_id = _run_once(monkeypatch, ["completed"])
    ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    refs = [
        FileRef(file_id=fid, filename=f"shot-{n}.png", content_type="image/png", size=1, s3_object_name=f"k/{n}")
        for n, fid in enumerate(ids)
    ]
    seen = _attachable(monkeypatch, refs, [])

    result = await _call(executor, "run_agent_once", agent_id=str(agent.id), prompt="Name each image.", file_ids=ids)

    assert "Here is your brief." in result.content
    assert seen[0]["file_ids"] == ids
    assert started[0]["extra_metadata"] == {"origin": "run_agent_once", TURN_FILE_IDS_KEY: ids}
    assert started[0]["own_run_trigger"] == "run_once"
    assert [ref.file_id for ref in started[0]["file_context"].list_files()] == ids


@pytest.mark.asyncio
async def test_run_agent_once_refuses_a_file_it_cannot_attach(monkeypatch):
    executor, agent, started, _task_id = _run_once(monkeypatch, ["completed"])
    _attachable(monkeypatch, [], ["not-mine"])

    result = await _call(executor, "run_agent_once", agent_id=str(agent.id), prompt="Read it.", file_ids=["not-mine"])

    assert "not-mine" in result.content and "Nothing was run." in result.content
    assert started == []


@pytest.mark.asyncio
async def test_run_agent_once_caps_the_files_of_one_message(monkeypatch):
    executor, agent, started, _task_id = _run_once(monkeypatch, ["completed"])

    result = await _call(
        executor,
        "run_agent_once",
        agent_id=str(agent.id),
        prompt="Read.",
        file_ids=[str(uuid.uuid4()) for _ in range(31)],
    )

    assert "At most 30 files" in result.content
    assert started == []


def test_task_turn_file_refs_names_the_held_files_in_order():
    from app.llm.base import FileRef
    from app.services.sub_agent_files import TURN_FILE_IDS_KEY, task_turn_file_refs
    from app.tools.base import FileContext

    a, b, c = (str(uuid.uuid4()) for _ in range(3))
    context = FileContext.from_file_refs(
        [
            FileRef(file_id=fid, filename=f"{fid}.png", content_type="image/png", size=1, s3_object_name=f"k/{fid}")
            for fid in (a, b, c)
        ]
    )

    turn = task_turn_file_refs(context, {TURN_FILE_IDS_KEY: [c, a, str(uuid.uuid4())]})
    assert [ref.file_id for ref in turn] == [c, a]
    assert task_turn_file_refs(context, {}) == []
    assert task_turn_file_refs(None, {TURN_FILE_IDS_KEY: [a]}) == []


# ---------------------------------------------------------------------------
# update_integration: group sharing arguments
# ---------------------------------------------------------------------------


def test_group_sharing_maps_use_and_manage_to_the_share_tiers():
    group_a, group_b = str(uuid.uuid4()), str(uuid.uuid4())
    ids, perms, error, provided = _parse_group_sharing_args(
        f"{group_a},{group_b}", f'{{"{group_a}": "use", "{group_b}": "Manage"}}'
    )
    assert (error, provided) == (None, True)
    assert ids == [group_a, group_b]
    assert perms == {group_a: "read_only", group_b: "config"}

    ids, perms, error, _ = _parse_group_sharing_args("", f"{group_a}:manage")
    assert (ids, perms, error) == (None, {group_a: "config"}, None)
    assert _parse_group_sharing_args("clear", "")[:3] == ([], None, None)
    assert _parse_group_sharing_args("", "")[3] is False
    assert "Invalid shared_group_permissions" in _parse_group_sharing_args(group_a, f"{group_a}:own")[2]
    # A pair without its tier is refused, never dropped.
    refused = _parse_group_sharing_args(f"{group_a},{group_b}", f"{group_a}:use,{group_b}")[2]
    assert refused.startswith("shared_group_permissions must be a JSON object")
    assert "Invalid shared_group_ids" in _parse_group_sharing_args("not-a-group", "")[2]


# ---------------------------------------------------------------------------
# search_knowledge
# ---------------------------------------------------------------------------

KB_SCHEMAS, KB_OPENCTI, KB_OTHER = (str(uuid.uuid4()) for _ in range(3))


def _hit(kb: str, *, vector_score: float, text_rank: int | None = None, vector_rank: int | None = 1):
    return SimpleNamespace(
        id=str(uuid.uuid4()),
        knowledge_base_id=kb,
        vector_score=vector_score,
        vector_rank=vector_rank,
        text_rank=text_rank,
    )


def test_a_relevant_knowledge_base_crowded_out_of_the_top_k_gets_a_slot():
    ranked = [_hit(KB_SCHEMAS, vector_score=0.62 - i / 100) for i in range(5)]
    opencti = _hit(KB_OPENCTI, vector_score=0.55, text_rank=1)
    unrelated = _hit(KB_OTHER, vector_score=0.31)

    result = vector.diversify_across_knowledge_bases(ranked, [unrelated, opencti], 5)

    assert len(result) == 5
    assert result[-1] is opencti, "the full-text match earns a slot"
    assert unrelated not in result, "an unrelated collection is not promoted"
    assert result[:4] == ranked[:4], "the lowest hit of the crowded collection gives way"


def test_a_close_vector_match_earns_a_slot_but_never_evicts_a_collections_only_hit():
    ranked = [_hit(KB_SCHEMAS, vector_score=0.62), _hit(KB_OTHER, vector_score=0.60)]
    close = _hit(KB_OPENCTI, vector_score=0.57)

    assert vector.diversify_across_knowledge_bases(ranked, [close], 2) == ranked
    assert vector.diversify_across_knowledge_bases(ranked, [close], 3) == [*ranked, close]


def test_no_candidate_or_a_single_slot_keeps_the_ranking():
    ranked = [_hit(KB_SCHEMAS, vector_score=0.62)]
    assert vector.diversify_across_knowledge_bases(ranked, [], 5) == ranked
    assert vector.diversify_across_knowledge_bases(ranked, [_hit(KB_OPENCTI, vector_score=0.9)], 1) == ranked


@pytest.mark.asyncio
async def test_every_hit_names_its_knowledge_base(monkeypatch):
    async def _titles(_db, ids):
        return {doc: "connectors.md" for doc in ids}

    async def _names(_db, ids):
        return {KB_OPENCTI: "OpenCTI Documentation"} if KB_OPENCTI in ids else {}

    monkeypatch.setattr(vector, "_load_document_titles", _titles)
    monkeypatch.setattr(vector, "_load_knowledge_base_names", _names)
    hit = SimpleNamespace(
        content="Connectors import data into OpenCTI.",
        source_type="document",
        source_id="doc-1",
        knowledge_base_id=KB_OPENCTI,
    )

    rendered = await vector._render_search_results(None, [hit])

    assert rendered.startswith(
        "[1] from 'connectors.md' (document id: doc-1) in knowledge base 'OpenCTI Documentation'"
    )


@pytest.mark.asyncio
async def test_search_knowledge_takes_a_knowledge_base_filter(monkeypatch):
    searched: list[list[str] | None] = []

    def _factory(_db, _user, _kb_ids, **_kwargs):
        async def _search(query, limit=5, *, scope_kb_ids=None):
            searched.append(scope_kb_ids)
            return "results"

        return _search

    async def _names(_db, kb_ids):
        return {
            KB_SCHEMAS: "Enterprise Data Platform Schemas",
            KB_OPENCTI: "OpenCTI Documentation",
            KB_OTHER: "Sales playbook",
        }

    @asynccontextmanager
    async def _session():
        yield SimpleNamespace()

    monkeypatch.setattr(vector, "make_search_knowledge_fn", _factory)
    monkeypatch.setattr(builtin_tools_module, "load_kb_names", _names)
    monkeypatch.setattr("app.core.database.async_session", _session)
    definitions, executor = builtin_tools_module.build_kb_search_tool(
        None, str(uuid.uuid4()), [KB_SCHEMAS, KB_OPENCTI, KB_OTHER]
    )
    assert "knowledge_base" in definitions[0].input_schema["properties"]

    await _call(executor, "search_knowledge", query="OpenCTI connectors")
    await _call(executor, "search_knowledge", query="OpenCTI connectors", knowledge_base="opencti documentation")
    await _call(executor, "search_knowledge", query="q", knowledge_base=f"{KB_OTHER}, OpenCTI Documentation")
    unknown = await _call(executor, "search_knowledge", query="q", knowledge_base="Wiki")

    assert searched == [None, [KB_OPENCTI], [KB_OTHER, KB_OPENCTI]]
    assert unknown.content.startswith("Unknown or ambiguous knowledge base 'Wiki'")
    assert "OpenCTI Documentation" in unknown.content


@pytest.mark.asyncio
async def test_a_filter_never_widens_the_bound_knowledge_bases(monkeypatch):
    """``scope_kb_ids`` only narrows: an id outside the bound set searches nothing."""

    async def _fail(*_args, **_kwargs):
        raise AssertionError("no search may run")

    monkeypatch.setattr(vector, "_diversified_hybrid_search", _fail)
    monkeypatch.setattr(vector, "search_knowledge_bases", _fail)
    search = vector.make_search_knowledge_fn(None, str(uuid.uuid4()), [KB_SCHEMAS])

    assert await search("q", scope_kb_ids=[KB_OPENCTI]) == "No relevant results found in knowledge bases."
