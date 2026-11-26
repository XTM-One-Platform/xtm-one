# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""XTM One MCP server – expose the platform's own surface via MCP.

Implements MCP Streamable HTTP transport (JSON-RPC 2.0 over POST) on a single
endpoint:

- ``POST /mcp/xtm`` – XTM One's own management and observability tools.

Where ``/mcp/user`` exposes *agents* (one tool per agent, each running the full
agentic loop) and ``/mcp/platform`` servers expose the *connected XTM suite
platforms*, this server exposes **XTM One itself**: the agents, assignments,
runs, knowledge bases, prompts, skills, custom tools, integrations, MCP servers
and variables an operator manages day to day, plus the LLM traces those runs
produce.  It is what lets an external MCP client (Claude Desktop, an IDE, a
CI job) read an agent's traces to diagnose a failure and then fix the agent's
configuration without leaving the client.

Tool surface
------------
Every tool is the *same builder* the agentic loop uses, so there is one
implementation and one set of permission gates per capability:

- ``services/builtin_tools/agent_builder`` – full CRUD parity with the REST
  API / UI (agents, assignments, runs, knowledge bases, custom tools, MCP
  servers, integrations, prompts, skills, variables).
- ``services/builtin_tools/flow_builder`` – author, validate, run and activate
  flows (node catalog, graph create / update, runs and their step journal),
  offered only while the ``FLOW_BUILDER`` feature flag is on.
- ``services/builtin_tools/traces`` – ``inspect_agent_traces`` (cross-agent
  when no target is named) and ``get_trace_payload``.  ``inspect_my_traces``
  is deliberately absent: there is no calling agent behind an MCP request.
- ``services/builtin_tools/kb`` – ``search_knowledge`` over every knowledge
  base the caller can read, and ``update_knowledge_document`` over the ones
  they can manage.
- ``services/builtin_tools/skills`` – ``load_skill`` / ``read_skill_resource``
  over every skill the caller can see, which is how a skill's bundled resource
  files are read at all.

The tool set is resolved **per request** from the authenticated caller, so it
narrows and widens with that user's own visibility and permissions.  Those
builders enforce the three-tier model themselves (built-in entities immutable,
company-managed admin-only, group-shared needing a ``config`` grant), which is
why this server can offer write tools without re-implementing a single gate —
a non-admin gets exactly what the UI would give them.

Authentication
--------------
Bearer token: the user's personal API key (``fcp-...``) or an OAuth 2.1 access
token, audience-bound to ``mcp/xtm``.  Unauthenticated requests receive ``401``
with a ``WWW-Authenticate`` header pointing at the Protected Resource Metadata
(RFC 9728) – same discovery flow as ``/mcp/user``.

Availability
------------
Off by default.  Unlike the platform MCP servers, this one hands an external
client the instance's own configuration surface, so an administrator has to
turn it on explicitly with ``xtm_mcp_enabled`` in Settings > Integrations > MCP Endpoint.

Licensed, unlike the platform MCP servers (deliberately Community Edition:
they proxy platform APIs and run no model).  The writes offered here are the
ones the REST routes hold to the agentic licence (``require_agentic_ai`` /
``require_agentic_ai_licensed``), so an unlicensed deployment refuses the
whole endpoint with the structured ``license_required`` refusal
(``agentic_gate.refuse_licence_required``) - as ``/mcp/user`` and
``/mcp/global`` refuse on the same licence.  The licence question
only, as ``require_agentic_ai_licensed`` asks it: the AI switch is the gate
before it.

Only Streamable HTTP transport is supported – no SSE, no stdio.
"""

from __future__ import annotations

import logging
import uuid as _uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.mcp_auth import authenticate_user
from app.api.v1.mcp_transport import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    MCPRequest,
    ServerIdentity,
    add_unsupported_method_routes,
    dispatch_all,
    initialize_result,
    read_messages,
    require_valid_origin,
    respond,
    tool_error_result,
    tool_result,
)
from app.api.v1.mcp_transport import jsonrpc_error as _jsonrpc_error
from app.api.v1.mcp_transport import jsonrpc_response as _jsonrpc_response
from app.core.database import get_db
from app.core.feature_flags import is_feature_enabled
from app.core.utils import format_exception_message
from app.models.user import User
from app.services.api_key_access import mcp_tools_for_key, require_configuration_surface
from app.services.branding import get_cached_branding
from app.services.mcp_tool_metadata import tools_to_mcp
from app.tools.base import (
    CompositeToolExecutor,
    ScopedToolExecutor,
    ToolCall,
    ToolDefinition,
    offering_tools,
)

logger = logging.getLogger(__name__)
router = APIRouter()

#: Resource path used for RFC 9728 discovery and OAuth audience binding.
XTM_MCP_RESOURCE_PATH = "mcp/xtm"

#: Short user-facing description of the exposed surface. Single source of truth
#: for every UI surface (Profile, Settings) via ``GET /platform/mcp-servers``.
XTM_MCP_DESCRIPTION = (
    "XTM One's own platform tools (agent traces, agents, assignments, runs, "
    "flows, knowledge bases, prompts, skills, custom tools, integrations, variables, "
    "agent accounts)"
)

#: Key for the per-request memo in ``AsyncSession.info`` (see
#: ``_build_xtm_tools``). A session belongs to exactly one request, so this
#: never crosses users.
_TOOLS_CACHE_KEY = "_xtm_mcp_tools"

#: Trace tools that make no sense without a calling agent.  ``inspect_my_traces``
#: scopes to "the agent running this loop", and an MCP request has none — the
#: cross-agent ``inspect_agent_traces`` is the one that answers the question.
_EXCLUDED_TRACE_TOOLS = frozenset({"inspect_my_traces"})

#: What a read-only API key may call here, reviewed tool by tool: each
#: reads and changes nothing (no secret value is ever echoed back, see the
#: agent builder). Fail closed - a tool absent here is a write for a read-only
#: key. ``tests/test_api_key_access.py`` holds it to the tools the builders
#: offer and to the integration catalog's read / write classifier.
XTM_MCP_READ_TOOLS: frozenset[str] = frozenset(
    {
        # Improvement proposals (#8256).
        "list_improvements",
        # Agent builder: the list / get half of every entity.
        "list_agents",
        "get_agent",
        "list_groups",
        "list_agent_templates",
        "list_assignments",
        "get_assignment",
        "list_runs",
        "get_run",
        "list_knowledge_bases",
        "get_knowledge_base",
        "list_kb_documents",
        "get_kb_document",
        "get_knowledge_base_sources",
        "list_custom_tools",
        "get_custom_tool",
        "list_mcp_servers",
        "get_mcp_server",
        "list_integrations",
        "list_integration_catalog",
        "list_integration_tools",
        "get_integration",
        "list_prompts",
        "get_prompt",
        "list_skills",
        "get_skill",
        "list_variables",
        "get_variable",
        "list_available_resources",
        # Agent accounts: the accounts, what creating one needs, the
        # rule engine's preview of a sample message (it saves nothing) and the
        # activity log.
        "list_agent_accounts",
        "get_agent_account",
        "list_agent_account_options",
        "preview_agent_account_decision",
        "list_agent_account_decisions",
        # Intent bindings: the bindings of one agent.
        "list_agent_intent_bindings",
        # Flow builder: what reads a flow, the node library or a run (the
        # dry validation saves nothing).
        "get_node_catalog",
        "list_flows",
        "get_flow",
        "get_flow_examples",
        "validate_flow_graph",
        "list_flow_event_types",
        "get_flow_run",
        # Observability, knowledge, skills.
        "inspect_agent_traces",
        "get_trace_payload",
        "search_knowledge",
        "load_skill",
        "read_skill_resource",
        # Platform Health (#8259): findings, signals and experiments of the
        # agents the caller manages; the writes are Platform Health's only.
        "get_quality_signals",
        "list_findings",
        "list_experiments",
        "get_experiment",
        # Flow Lab (#8260): diagnostics, shadows and comparisons of the flows
        # and assignments the caller manages; the shadow start is Platform Health's.
        "get_flow_diagnosis",
        "get_assignment_path",
        "find_flow_candidates",
        "get_flow_shadow",
        "get_flow_run_comparison",
        # Platform Health (#8261): signals, cycles and digests of the targets the caller manages.
        "list_health_signals",
        "list_health_cycles",
        "get_health_cycle",
        # The Platform Health page, read in the caller's role scope (#2199).
        "get_platform_health",
    }
)


# ── Gates ────────────────────────────────────────────────────────────────────


async def xtm_mcp_enabled(db: AsyncSession) -> bool:
    """Whether the XTM One MCP server is enabled (admin toggle, default off)."""
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )

    ps = await _load_ps(db)
    return bool(getattr(ps, "xtm_mcp_enabled", False))


# ── Caller-scoped resource resolution ────────────────────────────────────────


async def _resolve_knowledge_bases(
    db: AsyncSession, user: User, user_group_ids: list[str]
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return ``(readable, writable)`` ``(kb_id, name)`` lists for *user*.

    Readable is plain visibility; writable additionally requires the same
    ``can_manage`` grant the REST API and the agent builder check before a
    write, minus content-locked knowledge bases (built-in, installed by a
    skill package, read-only - ``helpers.knowledge_base_content_lock``).
    """
    from app.api.v1.helpers import (
        agent_tool_admin_bypass,
        can_manage,
        content_locked_knowledge_base_ids,
        user_visible_query,
    )
    from app.models.knowledge_base import KnowledgeBase

    result = await db.execute(
        user_visible_query(
            KnowledgeBase, user.id, user_group_ids, is_admin=agent_tool_admin_bypass(user)
        )
    )
    kbs = list(result.scalars().all())

    readable = [(str(kb.id), kb.name) for kb in kbs]
    manageable = [kb for kb in kbs if can_manage(user, kb, user_group_ids)]
    locked = await content_locked_knowledge_base_ids(db, manageable)
    writable = [(str(kb.id), kb.name) for kb in manageable if str(kb.id) not in locked]
    return readable, writable


async def _resolve_skill_ids(
    db: AsyncSession, user: User, user_group_ids: list[str]
) -> list[str]:
    """Return the ids of every skill visible to *user*."""
    from app.api.v1.helpers import agent_tool_admin_bypass, user_visible_query
    from app.models.agent_skill import AgentSkill

    result = await db.execute(
        user_visible_query(AgentSkill, user.id, user_group_ids, is_admin=agent_tool_admin_bypass(user))
    )
    return [str(skill.id) for skill in result.scalars().all()]


# ── Tool building ────────────────────────────────────────────────────────────


async def _build_xtm_tools(
    db: AsyncSession, user: User
) -> tuple[list[ToolDefinition], ScopedToolExecutor]:
    """Build the MCP tool surface for *user*.

    Composed from the builtin-tool builders the agentic loop already uses, so
    the permission gates, the argument schemas and the result formatting are
    the ones the platform tests cover — this module only decides *which*
    builders take part and with what scope.

    The returned executor is scoped to the definitions actually offered, so a
    tool that was filtered out cannot execute even if a client guesses its
    registered name (fail closed — same reasoning as the platform servers).

    Memoised on the **session**, which ``get_db`` scopes to one request: the
    result cannot outlive the request or reach another user, so a permission
    changed between two requests is still picked up on the next one. The cost
    is two visibility queries plus a ``can_manage`` pass over every knowledge
    base the caller can see, which scales with their entity count — worth
    paying once per request rather than once per resolution.
    """
    cached = db.info.get(_TOOLS_CACHE_KEY)
    if cached is not None:
        return cached
    from app.api.v1.helpers import get_user_group_ids
    from app.services.builtin_tools import (
        build_agent_builder_tool,
        build_flow_builder_tool,
        build_kb_document_update_tool,
        build_kb_search_tool,
        build_skill_access_context,
        build_skill_tools,
        build_trace_tools,
    )

    user_id = str(user.id)
    user_group_ids = await get_user_group_ids(db, user.id)
    composite = CompositeToolExecutor()

    # Platform management: agents, assignments, runs, KBs, prompts, skills,
    # custom tools, integrations, MCP servers, variables, workspaces (and the
    # caller's conversations to file in them; an MCP call has no current one).
    composite.add_tools(*build_agent_builder_tool(db, user_id))

    # Flows: the visual builder's authoring, run and activation tools. The
    # flag only decides whether the feature is offered at all (as for the
    # builder's UI and the General Assistant); the builder enforces the
    # visibility and write gates itself.
    if is_feature_enabled("FLOW_BUILDER"):
        composite.add_tools(*build_flow_builder_tool(db, user_id))

    # The Platform Health sandbox (#8255): the agent tools' own rule, administrators only here.
    from app.services.health.sandbox.tools import build_sandbox_tools

    composite.add_tools(*build_sandbox_tools(db, user_id))
    # Platform Health evaluation (#8257): administrators, while evaluation is on.
    from app.services.health.evaluation_tools import build_health_evaluation_tools

    composite.add_tools(*build_health_evaluation_tools(db, user_id))
    # Improvement proposals (#8256): the caller's managed targets; filing
    # stays the Platform Health Experimenter's, so an MCP caller is refused it.
    from app.services.builtin_tools.improvements import build_improvement_tools

    composite.add_tools(*build_improvement_tools(db, user_id))

    # Platform Health (#8261): a person's reach - the targets they manage - on every call.
    from app.services.builtin_tools.health_orchestrator import build_health_orchestrator_tools

    composite.add_tools(*build_health_orchestrator_tools(user_id))

    # Observability. ``allow_cross_agent`` grants the supervision surface; the
    # tools then gate it on the caller's own admin privilege, so a non-admin
    # gets the same refusal the REST Trace Explorer gives them.
    trace_defs, trace_exec = build_trace_tools(
        db, user_id, None, allow_cross_agent=True
    )
    composite.add_tools(
        [d for d in trace_defs if d.name not in _EXCLUDED_TRACE_TOOLS], trace_exec
    )

    # Knowledge: semantic search over what the caller can read, revision over
    # what they can manage.  The agent builder covers the rest of the document
    # surface (list / get / add / delete), so only the two tools it has no
    # equivalent for are added here rather than a second, near-identical set.
    readable_kbs, writable_kbs = await _resolve_knowledge_bases(
        db, user, user_group_ids
    )
    search_tools = build_kb_search_tool(db, user_id, [kb_id for kb_id, _ in readable_kbs])
    if search_tools:
        composite.add_tools(*search_tools)
    update_tools = build_kb_document_update_tool(db, user_id, writable_kbs)
    if update_tools:
        composite.add_tools(*update_tools)

    # Skills: ``get_skill`` (agent builder) returns the skill row; these two
    # open the bundled resource files, which nothing else can reach.
    skill_ids = await _resolve_skill_ids(db, user, user_group_ids)
    if skill_ids:
        access = await build_skill_access_context(db, user.id)
        skill_tools = build_skill_tools(skill_ids=skill_ids, access=access)
        if skill_tools:
            composite.add_tools(*skill_tools)

    # Platform Health (#8259): the read half only - a person is never the
    # Platform Health, whose agents alone write findings and run experiments.
    from app.services.builtin_tools.health_experiments import build_health_experiment_tools
    from app.services.builtin_tools.health_quality import build_health_diagnosis_tools, health_tools_offered

    if health_tools_offered():
        composite.add_tools(*build_health_diagnosis_tools(user_id, read_only=True))
        composite.add_tools(*build_health_experiment_tools(user_id, read_only=True))
        from app.services.builtin_tools.flow_lab import build_flow_lab_tools

        composite.add_tools(*build_flow_lab_tools(user_id, read_only=True))

    # A read-only API key keeps the read list; the executor is scoped to it too.
    definitions = mcp_tools_for_key(user, composite.all_tools, XTM_MCP_READ_TOOLS)
    built = (definitions, ScopedToolExecutor(composite, definitions))
    db.info[_TOOLS_CACHE_KEY] = built
    return built


# ── MCP method handlers ──────────────────────────────────────────────────────


def _handle_initialize(req_id: int | str | None, params: dict | None = None) -> dict[str, Any]:
    """Handle MCP initialize – identifies the server by the configured brand."""
    identity = _identity()
    return initialize_result(req_id, params, name=identity.name, description=identity.description)


def _identity() -> ServerIdentity:
    """The XTM One MCP server's identity, named by the configured brand."""
    return ServerIdentity(name=f"{get_cached_branding().name} Platform", description=XTM_MCP_DESCRIPTION)


async def _handle_tools_call(
    req_id: int | str | None,
    params: dict | None,
    definitions: list[ToolDefinition],
    executor: ScopedToolExecutor,
) -> dict[str, Any]:
    """Handle MCP tools/call – executes the underlying builtin tool.

    A call to no tool is a protocol error; invalid arguments and a failed call
    are tool execution errors, in the result the model reads.
    """
    if not params:
        return _jsonrpc_error(req_id, INVALID_PARAMS, "Missing params")

    tool_name = params.get("name")
    # Some clients send an absent optional field as null.
    arguments = params.get("arguments")
    if arguments is None:
        arguments = {}
    if not tool_name:
        return _jsonrpc_error(req_id, INVALID_PARAMS, "Missing tool name")

    if not any(d.name == tool_name for d in definitions):
        return _jsonrpc_error(req_id, INVALID_PARAMS, f"Unknown tool: {tool_name}")
    if not isinstance(arguments, dict):
        return tool_error_result(req_id, "Tool arguments must be an object.")

    call = ToolCall(
        id=str(req_id) if req_id is not None else str(_uuid.uuid4()),
        name=tool_name,
        arguments=arguments,
    )
    try:
        # The tool's result names another tool only when this server serves it.
        with offering_tools(definitions):
            result = await executor.execute_tool(call)
    except Exception as exc:
        logger.error(
            "XTM MCP tool execution error for %s: %s",
            tool_name,
            format_exception_message(exc),
            exc_info=True,
        )
        return tool_error_result(req_id, "Tool execution failed. Please try again.")

    # Tool-level failures are surfaced in-band (MCP ``isError``) so the client
    # sees the actionable message instead of a JSON-RPC fault.
    return tool_result(req_id, result.content or "", is_error=bool(result.is_error))


async def _dispatch_xtm_mcp(
    body: MCPRequest,
    db: AsyncSession,
    user: User,
) -> dict[str, Any] | None:
    """Route an XTM MCP JSON-RPC request to the appropriate handler."""
    method = body.method

    if method == "initialize":
        return _handle_initialize(body.id, body.params)

    if method == "ping":
        return _jsonrpc_response(body.id, {})

    if method in ("tools/list", "tools/call"):
        definitions, executor = await _build_xtm_tools(db, user)
        if method == "tools/list":
            return _jsonrpc_response(
                body.id,
                {"tools": tools_to_mcp(definitions)},
            )
        return await _handle_tools_call(body.id, body.params, definitions, executor)

    return _jsonrpc_error(body.id, METHOD_NOT_FOUND, f"Method not found: {method}")


# ── Endpoint ─────────────────────────────────────────────────────────────────


@router.post("/xtm")
async def mcp_xtm_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """XTM One MCP server – MCP Streamable HTTP endpoint.

    Authenticated with the user's personal API key (``fcp-...``) or an OAuth
    2.1 access token.  Exposes XTM One's own management and observability tool
    surface, scoped to what the calling user can see and manage.

    Disabled by default: an administrator enables it with the XTM One MCP
    server toggle in Settings > Integrations > MCP Endpoint. Refused with AI disabled, and
    on a deployment where agentic AI is not licensed.
    """
    # 0) A web page of another origin (DNS rebinding) never gets this far.
    require_valid_origin(request)

    # 1) Authenticate (fcp- API key or OAuth 2.1 token — RFC 9728 on 401).
    user = await authenticate_user(request, db, XTM_MCP_RESOURCE_PATH)
    # The platform's configuration surface: no place for a key restricted to agents.
    require_configuration_surface(user)

    # 2) AI disabled — refuse entirely (FR-029), as the other MCP servers do.
    # The tools behind this are product APIs rather than model calls, but an
    # MCP endpoint exists to be consumed by a model: on a deployment chosen
    # because model access is prohibited, leaving it live keeps open the path
    # the mode closes. Refused after authentication and still routed, so it
    # states why rather than disappearing or 401-ing a client that worked
    # yesterday.
    from app.services.agentic_gate import refuse_ai_disabled
    from app.services.ai_state import is_ai_enabled

    if not await is_ai_enabled(db):
        refuse_ai_disabled()

    # 3) Not licensed — refuse entirely. Every write this server
    # offers (agents, assignments, flows, KBs, ...) is one the REST routes hold
    # to the agentic licence; the MCP surface must not be the way around it.
    # The licence question only (``require_agentic_ai_licensed``): the AI
    # switch was asked just above. Structured like the AI refusal, so a client
    # branches on ``detail.code`` rather than parsing the sentence.
    from app.api.v1.platform import is_agentic_ai_licensed
    from app.services.agentic_gate import refuse_licence_required

    if not await is_agentic_ai_licensed(db):
        logger.warning(
            "XTM MCP request rejected (403): agentic AI is not licensed on this "
            "deployment. See GET /api/v1/platform/config for the current state."
        )
        refuse_licence_required()

    # 4) Admin toggle — off until an administrator opts in.
    if not await xtm_mcp_enabled(db):
        raise HTTPException(
            status_code=403,
            detail=(
                "The XTM One MCP server is disabled on this deployment. "
                "An administrator can enable it in Settings > Integrations > MCP Endpoint."
            ),
        )

    # 5) Read the JSON-RPC message(s).
    incoming = await read_messages(request)
    if isinstance(incoming, JSONResponse):
        # The API key's usage was recorded during authentication.
        await db.commit()
        return incoming

    answers = await dispatch_all(
        incoming, lambda body: _dispatch_xtm_mcp(body, db, user), identity=_identity()
    )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return respond(incoming, answers)


add_unsupported_method_routes(router, "/xtm")
