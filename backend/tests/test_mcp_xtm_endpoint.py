"""Tests for the XTM One MCP server (``/mcp/xtm``).

Covers:

- 401 + ``WWW-Authenticate`` (RFC 9728) carrying the ``mcp/xtm`` resource path
- 403 when the ``xtm_mcp_enabled`` admin toggle is off (default off)
- refusal with AI disabled, like the other MCP servers
- refusal on an unlicensed deployment, with the structured
  ``license_required`` code and the REST routes' sentence
- tool composition: the agent-builder surface plus cross-agent trace
  inspection, with ``inspect_my_traces`` excluded (no calling agent)
- knowledge-base scoping: readable is visibility, writable additionally needs
  a manage grant and never covers built-in knowledge bases
- JSON-RPC dispatch: initialize / tools-list / tools-call (happy path, unknown
  tool, tool error surfaced via ``isError``)
- the descriptor endpoint reports the server with its own toggle
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.deps import licence_required_detail
from app.api.v1 import mcp_xtm, platform
from app.api.v1.mcp_xtm import (
    _build_xtm_tools,
    _dispatch_xtm_mcp,
    _handle_tools_call,
    _resolve_knowledge_bases,
    mcp_xtm_endpoint,
)
from app.services import ai_state
from app.services.agentic_gate import AI_DISABLED_CODE, LICENSE_REQUIRED_CODE
from app.tools.base import (
    CallableToolExecutor,
    ScopedToolExecutor,
    ToolCall,
    ToolDefinition,
)


def _make_request(headers: list | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp/xtm",
            "headers": headers or [],
            "query_string": b"",
        }
    )


def _fake_user(*, is_admin: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        is_active=True,
        is_admin=is_admin,
        role="admin" if is_admin else "user",
    )


async def _ai_on(_db):
    """AI enabled: these tests are about the toggle, not the AI-disabled gate."""
    return True


async def _licensed(_db):
    """Agentic AI licensed: these tests are about another gate."""
    return True


async def _unlicensed(_db):
    return False


# ── Auth / gating ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unauthenticated_returns_401_with_xtm_resource_metadata():
    with pytest.raises(HTTPException) as exc_info:
        await mcp_xtm_endpoint(_make_request(), db=None)

    exc = exc_info.value
    assert exc.status_code == 401
    # Audience binding: a token minted for /mcp/user must not be replayable here.
    assert "oauth-protected-resource/mcp/xtm" in exc.headers["WWW-Authenticate"]


@pytest.mark.asyncio
async def test_disabled_toggle_returns_403(monkeypatch):
    async def _fake_auth(request, db, resource_path="mcp/user"):
        return _fake_user()

    async def _disabled(_db):
        return False

    monkeypatch.setattr(mcp_xtm, "authenticate_user", _fake_auth)
    monkeypatch.setattr(mcp_xtm, "xtm_mcp_enabled", _disabled)
    monkeypatch.setattr(ai_state, "is_ai_enabled", _ai_on)
    monkeypatch.setattr(platform, "is_agentic_ai_licensed", _licensed)

    with pytest.raises(HTTPException) as exc_info:
        await mcp_xtm_endpoint(_make_request(), db=None)

    exc = exc_info.value
    assert exc.status_code == 403
    assert "Settings > Integrations > MCP Endpoint" in exc.detail


@pytest.mark.asyncio
async def test_toggle_defaults_to_off(monkeypatch):
    """A settings row from before the column existed must not open the server."""
    from app.api.v1 import platform_settings_helpers

    async def _fake_ps(_db):
        return SimpleNamespace()

    monkeypatch.setattr(platform_settings_helpers, "get_platform_settings", _fake_ps)

    assert await mcp_xtm.xtm_mcp_enabled(None) is False


@pytest.mark.asyncio
async def test_ai_disabled_refuses_before_the_toggle(monkeypatch):
    async def _fake_auth(request, db, resource_path="mcp/user"):
        return _fake_user()

    async def _enabled(_db):
        return True

    async def _ai_off(_db):
        return False

    monkeypatch.setattr(mcp_xtm, "authenticate_user", _fake_auth)
    monkeypatch.setattr(mcp_xtm, "xtm_mcp_enabled", _enabled)
    monkeypatch.setattr(ai_state, "is_ai_enabled", _ai_off)
    # Unlicensed too: the AI switch is asked first, its answer the more actionable.
    monkeypatch.setattr(platform, "is_agentic_ai_licensed", _unlicensed)

    with pytest.raises(HTTPException) as exc_info:
        await mcp_xtm_endpoint(_make_request(), db=None)

    exc = exc_info.value
    assert exc.status_code == 403
    assert exc.detail["code"] == AI_DISABLED_CODE


@pytest.mark.asyncio
async def test_an_unlicensed_deployment_is_refused_with_the_licence_code(monkeypatch):
    """The writes offered here are the ones REST holds to the licence."""

    async def _fake_auth(request, db, resource_path="mcp/user"):
        return _fake_user()

    async def _enabled(_db):
        return True

    built: list[object] = []

    async def _tools(db, user):
        built.append(user)
        return [], None

    monkeypatch.setattr(mcp_xtm, "authenticate_user", _fake_auth)
    monkeypatch.setattr(mcp_xtm, "xtm_mcp_enabled", _enabled)
    monkeypatch.setattr(mcp_xtm, "_build_xtm_tools", _tools)
    monkeypatch.setattr(ai_state, "is_ai_enabled", _ai_on)
    monkeypatch.setattr(platform, "is_agentic_ai_licensed", _unlicensed)

    with pytest.raises(HTTPException) as exc_info:
        await mcp_xtm_endpoint(_make_request(), db=None)

    exc = exc_info.value
    assert exc.status_code == 403
    assert exc.detail == {
        "code": LICENSE_REQUIRED_CODE,
        "message": licence_required_detail(),
    }
    # Refused before any tool surface is resolved.
    assert built == []


@pytest.mark.asyncio
async def test_the_licence_is_asked_before_the_toggle(monkeypatch):
    """Toggle off and unlicensed: switching the toggle on would change nothing."""

    async def _fake_auth(request, db, resource_path="mcp/user"):
        return _fake_user()

    async def _disabled(_db):
        return False

    monkeypatch.setattr(mcp_xtm, "authenticate_user", _fake_auth)
    monkeypatch.setattr(mcp_xtm, "xtm_mcp_enabled", _disabled)
    monkeypatch.setattr(ai_state, "is_ai_enabled", _ai_on)
    monkeypatch.setattr(platform, "is_agentic_ai_licensed", _unlicensed)

    with pytest.raises(HTTPException) as exc_info:
        await mcp_xtm_endpoint(_make_request(), db=None)

    exc = exc_info.value
    assert exc.status_code == 403
    assert exc.detail["code"] == LICENSE_REQUIRED_CODE


# ── Tool composition ─────────────────────────────────────────────────────────


class _EmptyResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _EmptySession:
    """A session that answers every SELECT with no rows."""

    def __init__(self):
        #: Per-request scratch space, as SQLAlchemy's AsyncSession provides.
        #: Per instance, or one test's memo would answer another's build.
        self.info: dict = {}

    async def execute(self, _statement):
        return _EmptyResult()


@pytest.mark.asyncio
async def test_tool_surface_pairs_management_with_cross_agent_traces():
    definitions, executor = await _build_xtm_tools(_EmptySession(), _fake_user())
    names = {d.name for d in definitions}

    # Observability — the reason the server exists.
    assert "inspect_agent_traces" in names
    assert "get_trace_payload" in names
    # No agent runs behind an MCP request, so there is no "my" traces to scope to.
    assert "inspect_my_traces" not in names

    # Full management parity with the REST API / UI.
    for expected in (
        "list_agents",
        "get_agent",
        "create_agent",
        "update_agent",
        "delete_agent",
        "list_runs",
        "get_run",
        "list_prompts",
        "create_prompt",
        "list_skills",
        "create_skill",
        "list_knowledge_bases",
        "add_kb_document",
        "list_custom_tools",
        "list_integrations",
        "list_variables",
        "list_mcp_servers",
    ):
        assert expected in names, f"{expected} missing from the /mcp/xtm surface"

    # Every offered tool is executable, and nothing else is (fail closed).
    assert isinstance(executor, ScopedToolExecutor)
    for defn in definitions:
        assert executor.allows(defn.name)


#: What the flow builder adds to the surface; the whole set is gated together.
_FLOW_TOOLS = {
    "get_node_catalog",
    "list_flows",
    "get_flow",
    "get_flow_examples",
    "validate_flow_graph",
    "create_flow",
    "update_flow_graph",
    "create_flow_event_type",
    "list_flow_event_types",
    "run_flow",
    "get_flow_run",
    "activate_flow",
    "deactivate_flow",
}


@pytest.mark.asyncio
async def test_the_flow_tools_are_offered_when_the_flow_builder_flag_is_on(monkeypatch):
    monkeypatch.setattr(mcp_xtm, "is_feature_enabled", lambda flag: flag == "FLOW_BUILDER")
    definitions, executor = await _build_xtm_tools(_EmptySession(), _fake_user())
    names = {d.name for d in definitions}
    assert names >= _FLOW_TOOLS
    assert all(executor.allows(name) for name in _FLOW_TOOLS)


@pytest.mark.asyncio
async def test_the_flow_tools_are_withheld_when_the_flag_is_off(monkeypatch):
    monkeypatch.setattr(mcp_xtm, "is_feature_enabled", lambda flag: False)
    definitions, executor = await _build_xtm_tools(_EmptySession(), _fake_user())
    assert not {d.name for d in definitions} & _FLOW_TOOLS
    # Fail closed: an unoffered tool cannot be called by guessing its name.
    assert not any(executor.allows(name) for name in _FLOW_TOOLS)


@pytest.mark.asyncio
async def test_a_real_tool_routes_through_the_scoped_composite():
    """The composite is wrapped, not replaced — calls must still reach a handler."""
    definitions, executor = await _build_xtm_tools(_EmptySession(), _fake_user())

    # Argument validation happens before any DB access, so this exercises the
    # routing (composite -> trace executor -> handler) without a live database.
    result = await executor.execute_tool(
        ToolCall(
            id="1",
            name="get_trace_payload",
            arguments={"trace_id": str(uuid.uuid4()), "field": "not_a_field"},
        )
    )
    assert "field must be one of" in result.content

    # A tool the composite knows nothing about is refused by the scope.
    assert not any(d.name == "delete_everything" for d in definitions)


@pytest.mark.asyncio
async def test_tool_surface_has_no_duplicate_names():
    """Two tools with one name is a hard error for MCP clients and providers."""
    definitions, _executor = await _build_xtm_tools(_EmptySession(), _fake_user())
    names = [d.name for d in definitions]
    assert len(names) == len(set(names))


@pytest.mark.asyncio
async def test_kb_search_appears_only_when_something_is_readable(monkeypatch):
    kb = SimpleNamespace(
        id=uuid.uuid4(),
        name="Threat reports",
        is_builtin=False,
        is_company_managed=False,
    )

    async def _fake_resolve(_db, _user, _group_ids):
        return [(str(kb.id), kb.name)], [(str(kb.id), kb.name)]

    monkeypatch.setattr(mcp_xtm, "_resolve_knowledge_bases", _fake_resolve)

    definitions, _executor = await _build_xtm_tools(_EmptySession(), _fake_user())
    names = {d.name for d in definitions}
    assert "search_knowledge" in names
    assert "update_knowledge_document" in names


@pytest.mark.asyncio
async def test_kb_scope_separates_readable_from_writable():
    owner = _fake_user(is_admin=False)
    visible = [
        SimpleNamespace(
            id=uuid.uuid4(),
            name="Built-in",
            is_builtin=True,
            is_company_managed=False,
            user_id=owner.id,
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            name="Company policies",
            is_builtin=False,
            is_company_managed=True,
            user_id=uuid.uuid4(),
        ),
        SimpleNamespace(
            id=uuid.uuid4(),
            name="My notes",
            is_builtin=False,
            is_company_managed=False,
            user_id=owner.id,
        ),
    ]

    class _KBResult:
        def scalars(self):
            return self

        def all(self):
            return visible

    class _KBSession:
        async def execute(self, _statement):
            return _KBResult()

    readable, writable = await _resolve_knowledge_bases(_KBSession(), owner, [])

    assert len(readable) == 3
    # Built-in KBs are immutable for everyone; company-managed ones need admin.
    assert [name for _id, name in writable] == ["My notes"]


# ── JSON-RPC dispatch ────────────────────────────────────────────────────────


def _stub_tools() -> tuple[list[ToolDefinition], ScopedToolExecutor]:
    executor = CallableToolExecutor()

    async def _echo(message: str = "") -> str:
        return f"echo: {message}"

    async def _boom() -> str:
        raise RuntimeError("tool exploded")

    executor.register("inspect_agent_traces", _echo)
    executor.register("delete_agent", _echo)  # registered but NOT offered
    executor.register("broken_tool", _boom)

    definitions = [
        ToolDefinition(
            name="inspect_agent_traces",
            description="Inspect traces",
            input_schema={"type": "object", "properties": {}},
            source="builtin:traces",
        ),
        ToolDefinition(
            name="broken_tool",
            description="Always fails",
            source="builtin:traces",
        ),
    ]
    return definitions, ScopedToolExecutor(executor, definitions)


@pytest.mark.asyncio
async def test_dispatch_initialize_ping_and_unknown_method():
    body = mcp_xtm.MCPRequest(jsonrpc="2.0", id=1, method="initialize")
    result = await _dispatch_xtm_mcp(body, None, _fake_user())
    assert result["result"]["protocolVersion"]
    assert result["result"]["serverInfo"]["name"]

    body = mcp_xtm.MCPRequest(jsonrpc="2.0", id=2, method="ping")
    assert await _dispatch_xtm_mcp(body, None, _fake_user()) == {
        "jsonrpc": "2.0",
        "id": 2,
        "result": {},
    }

    body = mcp_xtm.MCPRequest(jsonrpc="2.0", id=3, method="bogus/method")
    result = await _dispatch_xtm_mcp(body, None, _fake_user())
    assert result["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_a_notification_method_sent_as_a_request_is_answered():
    """A message with an id is a request: the transport never dispatches a
    notification, and one carrying an id gets an answer, not silence."""
    body = mcp_xtm.MCPRequest(jsonrpc="2.0", id=4, method="notifications/initialized")
    result = await _dispatch_xtm_mcp(body, None, _fake_user())
    assert result["id"] == 4
    assert result["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_dispatch_tools_list_returns_only_built_definitions(monkeypatch):
    definitions, scoped = _stub_tools()

    async def _fake_build(db, user):
        return definitions, scoped

    monkeypatch.setattr(mcp_xtm, "_build_xtm_tools", _fake_build)

    body = mcp_xtm.MCPRequest(jsonrpc="2.0", id=4, method="tools/list")
    result = await _dispatch_xtm_mcp(body, None, _fake_user())

    assert {t["name"] for t in result["result"]["tools"]} == {
        "inspect_agent_traces",
        "broken_tool",
    }
    for tool in result["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"


@pytest.mark.asyncio
async def test_tools_call_happy_path_unknown_tool_and_failure():
    definitions, scoped = _stub_tools()

    result = await _handle_tools_call(
        7, {"name": "inspect_agent_traces", "arguments": {"message": "hi"}},
        definitions, scoped,
    )
    assert result["result"]["isError"] is False
    assert result["result"]["content"][0]["text"] == "echo: hi"

    # Registered but never offered — refused without executing (fail closed).
    result = await _handle_tools_call(
        8, {"name": "delete_agent", "arguments": {}}, definitions, scoped
    )
    assert result["error"]["code"] == -32602

    # Missing params / name: protocol errors.
    assert (await _handle_tools_call(9, None, definitions, scoped))["error"][
        "code"
    ] == -32602
    assert (
        await _handle_tools_call(10, {"arguments": {}}, definitions, scoped)
    )["error"]["code"] == -32602
    # Non-object arguments: a tool execution error the model can correct
    # (SEP-1303).
    result = await _handle_tools_call(
        11, {"name": "inspect_agent_traces", "arguments": "nope"},
        definitions, scoped,
    )
    assert result["result"]["isError"] is True
    assert "must be an object" in result["result"]["content"][0]["text"]
    # ``"arguments": null`` is how some clients send an absent optional field.
    result = await _handle_tools_call(
        11, {"name": "inspect_agent_traces", "arguments": None},
        definitions, scoped,
    )
    assert result["result"]["isError"] is False
    assert result["result"]["content"][0]["text"] == "echo: "

    # A failing tool is surfaced in-band so the client sees the message.
    result = await _handle_tools_call(
        12, {"name": "broken_tool", "arguments": {}}, definitions, scoped
    )
    assert result["result"]["isError"] is True


# ── Descriptor endpoint ──────────────────────────────────────────────────────


class _RegsResult:
    def scalars(self):
        return self

    def all(self):
        return []


class _RegsSession:
    async def execute(self, _statement):
        return _RegsResult()


@pytest.mark.asyncio
async def test_descriptor_reports_the_xtm_server_with_its_own_toggle(monkeypatch):
    from app.api.v1 import platform as platform_api
    from app.api.v1 import platform_settings_helpers

    async def _fake_ps(_db):
        # Platform servers on, XTM server off — the two toggles are independent.
        return SimpleNamespace(platform_mcp_enabled=True, xtm_mcp_enabled=False)

    monkeypatch.setattr(platform_settings_helpers, "get_platform_settings", _fake_ps)

    out = await platform_api.list_platform_mcp_servers(
        db=_RegsSession(), user=_fake_user()
    )

    assert out.enabled is True
    assert out.xtm_server.enabled is False
    assert out.xtm_server.url.endswith("/mcp/xtm")
    assert out.xtm_server.description
