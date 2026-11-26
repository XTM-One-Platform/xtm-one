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

"""MCP Agent Endpoint – expose Copilot agents as MCP tools.

Implements MCP Streamable HTTP transport (JSON-RPC 2.0 over POST) with two
modes:

- **User endpoint** (``POST /mcp/user``) – authenticated with user API keys
  (``fcp-...``) or OAuth 2.1 access tokens. Exposes agents selected by the
  user in their profile (or all visible agents when no selection is configured).

- **Global endpoint** (``POST /mcp/global``) – authenticated with a
  platform-wide MCP key (``mcp-...``) or OAuth 2.1 access tokens configured
  in Settings. Exposes admin-selected agents from
  ``PlatformSettings.mcp_exposed_agent_ids``.

Authentication supports both bearer API keys and OAuth 2.1 JWTs. When no
valid token is provided, the server returns ``401`` with a
``WWW-Authenticate`` header pointing to the Protected Resource Metadata
(RFC 9728), enabling automatic OAuth discovery by MCP clients.

Each agent becomes a single MCP tool: the tool name is the agent slug,
the description is derived from the agent's persona, and the input is a
free-text message.  Tool execution runs the full agentic loop via
``run_agent_chat()`` (same path as the LLM proxy and chat UI).

Only Streamable HTTP transport is supported – no SSE, no stdio.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.helpers import (
    MCP_GLOBAL_KEY,
    get_user_group_ids,
    keeping_company_managed_tier,
    user_visible_filter,
)
from app.api.v1.mcp_auth import authenticate_user, raise_unauthorized, try_oauth_token
from app.api.v1.mcp_transport import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    MCPRequest,
    ServerIdentity,
    add_unsupported_method_routes,
    dispatch_all,
    initialize_result,
    is_modern_request,
    read_messages,
    require_valid_origin,
    respond,
    tool_error_result,
    tool_result,
)
from app.api.v1.mcp_transport import jsonrpc_error as _jsonrpc_error
from app.api.v1.mcp_transport import jsonrpc_response as _jsonrpc_response
from app.core.database import get_db
from app.core.redaction import known_secrets_scoped, redact_known_secrets_for_display
from app.core.security import bcrypt_checkpw_async
from app.core.utils import format_exception_message
from app.llm.base import ContextOverflowError, overflow_api_message
from app.llm.provider_access import bind_acting_user
from app.models.agent import Agent
from app.models.platform_settings import PlatformSettings
from app.models.user import User
from app.services.api_key_access import (
    agents_for_key,
    principal_of,
)
from app.services.branding import get_cached_branding
from app.services.mcp_tool_metadata import agent_tool_annotations, agent_tool_name

logger = logging.getLogger(__name__)
router = APIRouter()

# Maximum characters from agent persona included in the tool description.
PERSONA_TRUNCATE = 500


# ── Auth ─────────────────────────────────────────────────────────────────────


#: The caller of a ``/mcp/global`` request authenticated with the platform
#: key: returned only once the key verified, never a user id. It is no
#: user, so it never gets a cross-platform JWT (``integration_tools._generate_platform_jwt``).
GLOBAL_KEY_CALLER = "mcp-global"


async def _authenticate_global_endpoint(
    request: Request, db: AsyncSession
) -> tuple[PlatformSettings, str]:
    """Authenticate a request to ``/mcp/global``.

    Tries OAuth 2.1 JWT first, then falls back to ``mcp-`` API key auth.
    Returns ``(platform_settings, user_id)`` — for OAuth the user_id comes
    from the JWT; for API key auth a synthetic ``"mcp-global"`` is used.
    """
    from app.api.v1.platform_settings_helpers import (
        get_platform_settings as _load_ps,
    )

    raw_token: str | None = None

    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        raw_token = auth_header[7:]

    if not raw_token:
        raw_token = request.headers.get("x-api-key")

    if not raw_token:
        raise_unauthorized("mcp/global")

    ps = await _load_ps(db)

    # 1) Try OAuth JWT — bound to this endpoint, not the user one. Sharing the
    #    user endpoint's identity here would both refuse tokens legitimately
    #    minted for /mcp/global and accept /mcp/user tokens on it, which is the
    #    cross-resource replay the audience check exists to stop.
    user = await try_oauth_token(
        raw_token, db, "mcp/global", require_audience=is_modern_request(request)
    )
    if user:
        bind_acting_user(user.id)  # as on /mcp/user
        return ps, str(user.id)

    # 2) Fall back to mcp- API key
    if not raw_token.startswith("mcp-"):
        raise_unauthorized("mcp/global", "Invalid or expired token")

    if not ps.mcp_global_api_key_hash:
        raise_unauthorized("mcp/global", "Global MCP endpoint not configured")

    if not await bcrypt_checkpw_async(
        raw_token.encode("utf-8"), ps.mcp_global_api_key_hash.encode("utf-8")
    ):
        raise_unauthorized("mcp/global", "Invalid MCP API key")

    return ps, GLOBAL_KEY_CALLER


# ── Agent resolution ─────────────────────────────────────────────────────────


async def _get_user_agents(db: AsyncSession, user: User) -> list[Agent]:
    """Return agents visible to the user that are eligible for MCP exposure.

    If the user has configured ``mcp_exposed_agent_ids`` in their profile,
    only agents in that list (that the user can also see) are returned.
    Otherwise all visible agents are exposed.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    filters = [
        user_visible_filter(Agent, user.id, user_group_ids),
        Agent.enabled.is_(True),
        Agent.disable_chat.is_(False),
        Agent.slug.isnot(None),
    ]

    # Apply user-level selection if configured
    user_selected_ids = user.mcp_exposed_agent_ids or []
    if user_selected_ids:
        filters.append(Agent.id.in_(user_selected_ids))

    # Deterministic order (2026-07-28: clients cache the list), ties by id.
    result = await db.execute(select(Agent).where(*filters).order_by(Agent.name, Agent.id))
    return list(result.scalars().all())


async def _get_global_agents(db: AsyncSession, config: PlatformSettings) -> list[Agent]:
    """Return agents explicitly selected for the global MCP endpoint."""
    agent_ids = config.mcp_exposed_agent_ids or []
    if not agent_ids:
        return []

    result = await db.execute(
        select(Agent)
        .where(
            Agent.id.in_(agent_ids),
            Agent.enabled.is_(True),
            Agent.disable_chat.is_(False),
            Agent.slug.isnot(None),
        )
        .order_by(Agent.name, Agent.id)
    )
    return list(result.scalars().all())


# ── Tool building ────────────────────────────────────────────────────────────


def _agent_to_tool(agent: Agent) -> dict[str, Any]:
    """Convert an Agent model to an MCP tool definition.

    The name fits the 64 characters hosted clients accept (``agent_tool_name``)
    and the annotations say what an agent run is.
    """
    persona = (agent.persona or "")[:PERSONA_TRUNCATE]
    description = f"{agent.name}"
    if persona:
        description += f" — {persona}"

    return {
        "name": agent_tool_name(agent.slug),
        "title": agent.name,
        "description": description,
        "annotations": agent_tool_annotations(agent.name),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The message or task for the agent",
                },
                "context": {
                    "type": "string",
                    "description": "Optional additional context to include",
                },
            },
            "required": ["message"],
        },
    }


# ── Tool execution ───────────────────────────────────────────────────────────


@known_secrets_scoped()
async def _execute_agent_tool(
    db: AsyncSession,
    user_id: str,
    agent: Agent,
    message: str,
    context: str | None = None,
) -> str:
    """Run the full agentic loop for an agent and return the response text.

    A secret variable value the answer echoed is the display text.
    """
    from app.services.agent_chat import run_agent_chat

    full_message = message
    if context:
        full_message = f"{context}\n\n{message}"

    response_text, _new_agent_id = await run_agent_chat(
        db=db,
        user_id=user_id,
        agent=agent,
        history=[],
        user_message=full_message,
        allow_handover=False,
    )
    return redact_known_secrets_for_display(response_text)


# ── MCP method handlers ─────────────────────────────────────────────────────


def _handle_initialize(
    req_id: int | str | None,
    params: dict | None,
) -> dict[str, Any]:
    """Handle MCP initialize request.

    The server name comes from the branding resolver so MCP clients see the
    configured brand during initialization rather than a deployment-mode
    default.
    """
    identity = _identity()
    return initialize_result(req_id, params, name=identity.name, description=identity.description)


def _identity() -> ServerIdentity:
    """The agent endpoints' identity, named by the configured brand."""
    brand = get_cached_branding().name
    return ServerIdentity(
        name=brand,
        description=f"The {brand} agents you can use, each one a tool that runs the agent.",
    )


def _handle_tools_list(req_id: int | str | None, agents: list[Agent]) -> dict[str, Any]:
    """Handle MCP tools/list request."""
    tools = [_agent_to_tool(a) for a in agents]
    return _jsonrpc_response(req_id, {"tools": tools})


@known_secrets_scoped()
async def _handle_tools_call(
    req_id: int | str | None,
    params: dict | None,
    agents: list[Agent],
    db: AsyncSession,
    user_id: str,
) -> dict[str, Any]:
    """Handle MCP tools/call request – runs the agent agentic loop.

    A call to no tool is a protocol error; invalid arguments and a run that
    failed are tool execution errors, in the result the model reads.
    The failure it logs is redacted in its scope, which the run's nested one
    hands its secret variable values to.
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

    # Find the matching agent: by the name ``tools/list`` gave it, or by its
    # full slug, which a client configured before long slugs were shortened
    # may still send.
    agent = next(
        (a for a in agents if tool_name in (agent_tool_name(a.slug), a.slug)),
        None,
    )
    if not agent:
        return _jsonrpc_error(req_id, INVALID_PARAMS, f"Unknown tool: {tool_name}")

    if not isinstance(arguments, dict):
        return tool_error_result(req_id, "Tool arguments must be an object.")

    message = arguments.get("message", "")
    if not message:
        return tool_error_result(req_id, "Missing required argument: message")

    context = arguments.get("context")

    try:
        result_text = await _execute_agent_tool(db, user_id, agent, message, context)
    except ContextOverflowError as exc:
        logger.warning(
            "MCP tool %s hit context overflow: %s tokens vs %s limit",
            tool_name,
            exc.estimated_tokens,
            exc.context_window,
        )
        return tool_error_result(req_id, overflow_api_message(exc))
    except Exception as exc:
        logger.error(
            "MCP tool execution error for agent %s: %s", tool_name, format_exception_message(exc), exc_info=True
        )
        return tool_error_result(req_id, "Agent execution failed. Please try again.")

    return tool_result(req_id, result_text or "")


async def _dispatch_mcp(
    body: MCPRequest,
    agents: list[Agent],
    db: AsyncSession,
    user_id: str,
) -> dict[str, Any] | None:
    """Route an MCP JSON-RPC request to the appropriate handler."""
    method = body.method

    if method == "initialize":
        return _handle_initialize(body.id, body.params)

    if method == "tools/list":
        return _handle_tools_list(body.id, agents)

    if method == "tools/call":
        return await _handle_tools_call(body.id, body.params, agents, db, user_id)

    if method == "ping":
        return _jsonrpc_response(body.id, {})

    return _jsonrpc_error(body.id, METHOD_NOT_FOUND, f"Method not found: {method}")


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.post("/user")
async def mcp_user_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """MCP Streamable HTTP endpoint – user mode.

    Authenticated with the user's personal API key (``fcp-...``) or
    an OAuth 2.1 access token.  Returns ``401`` with
    ``WWW-Authenticate`` header when unauthenticated (RFC 9728).

    Exposes all agents visible to the user as MCP tools.
    """
    require_valid_origin(request)
    user = await authenticate_user(request, db, "mcp/user")

    # This endpoint exists to be consumed by a model and runs the same agentic
    # loop as a chat message, so it refuses **entirely** with AI disabled
    # (FR-029) — while staying routed, so it refuses rather than disappearing.
    #
    # Two reasons, two payloads: AI switched off gets the contract code a client
    # can branch on, a missing licence keeps the licence message. Collapsing
    # them told an operator to buy a licence they already had.
    from app.api.deps import refuse_chat_unavailable
    from app.api.v1.platform import is_chat_licensed
    from app.services.agentic_gate import refuse_ai_disabled
    from app.services.ai_state import is_ai_enabled

    if not await is_ai_enabled(db):
        refuse_ai_disabled()

    if not await is_chat_licensed(db):
        await refuse_chat_unavailable(db)

    incoming = await read_messages(request)
    if isinstance(incoming, JSONResponse):
        # The API key's usage was recorded during authentication.
        await db.commit()
        return incoming

    # A key restricted to agents exposes those only; a read-only key none,
    # since every tool here runs an agent with its tools. Said of every
    # message of a batch.
    agents = agents_for_key(user, await _get_user_agents(db, user))
    principal = principal_of(user)
    read_only = principal is not None and principal.read_only
    user_id = str(user.id)

    async def _answer(body):
        if read_only and body.method == "tools/call":
            return _jsonrpc_error(
                body.id, -32602, "This API key is read-only: every tool of this endpoint runs an agent."
            )
        return await _dispatch_mcp(body, agents, db, user_id)

    answers = await dispatch_all(incoming, _answer, identity=_identity())
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return respond(incoming, answers)


@router.post("/global")
async def mcp_global_endpoint(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """MCP Streamable HTTP endpoint – global mode.

    Authenticated with the platform-wide MCP API key (``mcp-...``) or
    an OAuth 2.1 access token.  Returns ``401`` with
    ``WWW-Authenticate`` header when unauthenticated (RFC 9728).

    Exposes admin-selected agents as MCP tools.
    """
    require_valid_origin(request)
    config, user_id = await _authenticate_global_endpoint(request, db)

    # See ``mcp_user_endpoint`` — same two-reason gate on the global endpoint.
    from app.api.deps import refuse_chat_unavailable
    from app.api.v1.platform import is_chat_licensed
    from app.services.agentic_gate import refuse_ai_disabled
    from app.services.ai_state import is_ai_enabled

    if not await is_ai_enabled(db):
        refuse_ai_disabled()

    if not await is_chat_licensed(db):
        await refuse_chat_unavailable(db)

    incoming = await read_messages(request)
    if isinstance(incoming, JSONResponse):
        return incoming

    agents = await _get_global_agents(db, config)
    # The platform key has no account; its callers keep main's tier.
    scope = (
        keeping_company_managed_tier(MCP_GLOBAL_KEY)
        if user_id == GLOBAL_KEY_CALLER
        else contextlib.nullcontext()
    )
    with scope:
        answers = await dispatch_all(
            incoming, lambda body: _dispatch_mcp(body, agents, db, user_id),
            identity=_identity(),
        )
    # Commit before answering: the get_db teardown only commits after the response is sent.
    await db.commit()
    return respond(incoming, answers)


add_unsupported_method_routes(router, "/user", "/global")
