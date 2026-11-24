"""Tests for the platform MCP servers (``/mcp/opencti``, ``/mcp/openaev``, ``/mcp/opencrq``).

Covers:

- 401 + ``WWW-Authenticate`` (RFC 9728) with the platform resource path
- 403 when the ``platform_mcp_enabled`` admin toggle is off
- 404 when the platform is not registered
- allow-list integrity: every allow-listed name exists on the real provider
  (catches typos / renames) and no orchestration tool is exposed (fail closed)
- OpenCTI direct-mutation mode: no draft gate, no draft prompt text
- JSON-RPC dispatch: initialize / tools-list filtering / tools-call
  (happy path, unknown tool, tool error surfaced via ``isError``)
"""

import ast
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.v1 import mcp_platform
from app.api.v1.mcp_platform import (
    MCP_TOOL_ALLOW_LISTS,
    OPENAEV_MCP_TOOLS,
    OPENCRQ_MCP_TOOLS,
    OPENCTI_MCP_TOOLS,
    _dispatch_platform_mcp,
    _handle_tools_call,
    _platform_mcp_endpoint,
)
from app.integrations.providers import openaev as openaev_provider
from app.integrations.providers import opencrq as opencrq_provider
from app.integrations.providers import opencti as opencti_provider
from app.services import ai_state
from app.tools.base import (
    CallableToolExecutor,
    ScopedToolExecutor,
    ToolDefinition,
)
from tests import app_sources


def _make_request(path: str, headers: list | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": headers or [],
            "query_string": b"",
        }
    )


def _fake_registration() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        platform_identifier="opencti",
        integration_id=uuid.uuid4(),
        status="active",
    )


def _fake_user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), is_active=True)


# ── Auth / gating ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["opencti", "openaev"])
async def test_unauthenticated_returns_401_with_platform_resource_metadata(
    provider,
):
    request = _make_request(f"/mcp/{provider}")

    with pytest.raises(HTTPException) as exc_info:
        await _platform_mcp_endpoint(provider, request, db=None)

    exc = exc_info.value
    assert exc.status_code == 401
    www = exc.headers["WWW-Authenticate"]
    assert f"oauth-protected-resource/mcp/{provider}" in www


async def _ai_on(_db):
    """AI enabled: these tests are about the toggle and registration gates."""
    return True


@pytest.mark.asyncio
async def test_disabled_toggle_returns_403(monkeypatch):
    async def _fake_auth(request, db, resource_path="mcp/user"):
        return _fake_user()

    async def _fake_enabled(db):
        return False

    monkeypatch.setattr(mcp_platform, "authenticate_user", _fake_auth)
    monkeypatch.setattr(mcp_platform, "_platform_mcp_enabled", _fake_enabled)
    # The endpoint refuses entirely with AI disabled (FR-029), before
    # the toggle and registration checks these tests are about.
    monkeypatch.setattr(ai_state, "is_ai_enabled", _ai_on)

    with pytest.raises(HTTPException) as exc_info:
        await _platform_mcp_endpoint("opencti", _make_request("/mcp/opencti"), db=None)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_unregistered_platform_returns_404(monkeypatch):
    async def _fake_auth(request, db, resource_path="mcp/user"):
        return _fake_user()

    async def _fake_enabled(db):
        return True

    async def _fake_registration_lookup(db, provider):
        return None

    monkeypatch.setattr(mcp_platform, "authenticate_user", _fake_auth)
    monkeypatch.setattr(mcp_platform, "_platform_mcp_enabled", _fake_enabled)
    # The endpoint refuses entirely with AI disabled (FR-029), before
    # the toggle and registration checks these tests are about.
    monkeypatch.setattr(ai_state, "is_ai_enabled", _ai_on)
    monkeypatch.setattr(
        mcp_platform, "_get_active_registration", _fake_registration_lookup
    )

    with pytest.raises(HTTPException) as exc_info:
        await _platform_mcp_endpoint("openaev", _make_request("/mcp/openaev"), db=None)

    exc = exc_info.value
    assert exc.status_code == 404
    assert "OpenAEV" in exc.detail


# ── Allow-list integrity (fail closed) ───────────────────────────────────────


def _real_tool_names(provider_module) -> set[str]:
    executor = CallableToolExecutor()
    defs = provider_module.build_tools("http://platform.local||token", executor)
    return {d.name for d in defs}


def test_opencti_allow_list_names_exist_on_provider():
    real = _real_tool_names(opencti_provider)
    missing = OPENCTI_MCP_TOOLS - real
    assert not missing, f"Allow-listed OpenCTI tools not found on provider: {missing}"


def test_openaev_allow_list_names_exist_on_provider():
    real = _real_tool_names(openaev_provider)
    missing = OPENAEV_MCP_TOOLS - real
    assert not missing, f"Allow-listed OpenAEV tools not found on provider: {missing}"


def test_opencrq_allow_list_names_exist_on_provider():
    real = _real_tool_names(opencrq_provider)
    missing = OPENCRQ_MCP_TOOLS - real
    assert not missing, f"Allow-listed OpenCRQ tools not found on provider: {missing}"


def test_opencti_allow_list_excludes_orchestration_and_draft_tools():
    forbidden = {
        # Platform orchestration
        "manage_opencti_user",
        "manage_opencti_group",
        "manage_opencti_role",
        "manage_opencti_playbook",
        "edit_opencti_playbook_graph",
        "manage_opencti_workspace",
        "import_opencti_dashboard",
        "manage_opencti_notifier",
        "manage_opencti_trigger",
        "manage_opencti_notification",
        "manage_opencti_entity_setting",
        "manage_opencti_vocabulary",
        "manage_opencti_decay_rule",
        "manage_opencti_retention_rule",
        "manage_opencti_exclusion_list",
        "query_opencti_activity",
        "manage_opencti_pir",
        "query_opencti_pir_entities",
        "query_opencti_pir_relationships",
        "manage_opencti_ingestion",
        "manage_opencti_form",
        "manage_opencti_data_sharing",
        "query_opencti_catalog",
        "manage_opencti_connector",
        # Draft workflow (stateless MCP mutations run direct)
        "create_opencti_draft",
        "use_opencti_draft",
        "get_opencti_draft_info",
        "list_opencti_drafts",
        # File transfer (needs the chat FileContext)
        "upload_opencti_file",
        "download_opencti_file",
    }
    leaked = OPENCTI_MCP_TOOLS & forbidden
    assert not leaked, f"Orchestration/draft tools leaked into OpenCTI MCP: {leaked}"


def test_openaev_allow_list_excludes_orchestration_tools():
    forbidden = {
        "manage_openaev_organization",
        "manage_openaev_user",
        "manage_openaev_group",
        "manage_openaev_role",
        "manage_openaev_profile",
        "manage_openaev_team",
        "manage_openaev_player",
        "manage_openaev_security_platform",
        "manage_openaev_tag",
        "manage_openaev_kill_chain_phase",
        "manage_openaev_attack_pattern",
        "query_openaev_connectors",
        "query_openaev_catalog",
        "manage_openaev_connector_instance",
        "manage_openaev_settings",
    }
    leaked = OPENAEV_MCP_TOOLS & forbidden
    assert not leaked, f"Orchestration tools leaked into OpenAEV MCP: {leaked}"


def test_opencrq_allow_list_excludes_admin_and_settings_tools():
    forbidden = {
        # Users / tenant membership
        "list_opencrq_users",
        "get_opencrq_user",
        "get_opencrq_user_permissions",
        "list_opencrq_tenant_users",
        "update_opencrq_tenant_user_profile",
        "remove_opencrq_tenant_user",
        # Tenant / organization lifecycle
        "create_opencrq_tenant",
        "delete_opencrq_tenants",
        "create_opencrq_organization",
        "update_opencrq_organization",
        "delete_opencrq_organizations",
        "list_opencrq_sectors",
        "list_opencrq_countries",
        # Tenant RBAC
        "list_opencrq_groups",
        "get_opencrq_group",
        "create_opencrq_group",
        "update_opencrq_group",
        "delete_opencrq_group",
        "manage_opencrq_group_membership",
        "manage_opencrq_group_roles",
        "list_opencrq_roles",
        "get_opencrq_role",
        "create_opencrq_role",
        "update_opencrq_role",
        "delete_opencrq_role",
        "manage_opencrq_role_capabilities",
        "list_opencrq_capabilities",
        # Service accounts / platform RBAC
        "list_opencrq_service_accounts",
        "get_opencrq_platform_rbac",
        # Engine settings + job writes
        "get_opencrq_crq_settings",
        "update_opencrq_crq_settings",
        "get_opencrq_threat_relevance_settings",
        "update_opencrq_threat_relevance_settings",
        "manage_opencrq_job",
    }
    leaked = OPENCRQ_MCP_TOOLS & forbidden
    assert not leaked, f"Admin/settings tools leaked into OpenCRQ MCP: {leaked}"


def test_opencrq_allow_list_covers_business_domains():
    """The business/knowledge subset must stay wired into /mcp/opencrq."""
    required = {
        # New business domains added with the full-coverage expansion
        "create_opencrq_compliance_assessment",
        "delete_opencrq_risk_scenarios",
        "get_opencrq_scenario_exposure",
        "get_opencrq_cve_exploitation",
        "get_opencrq_attack_path_reach",
        "run_opencrq_crq_simulation",
        "get_opencrq_crq_what_if",
        "update_opencrq_tef_input",
        "list_opencrq_loss_drivers",
        "create_opencrq_primary_asset",
        "create_opencrq_supporting_asset",
        "list_opencrq_asset_dependencies",
        "list_opencrq_cve_evidences",
        "list_opencrq_cves",
        "get_opencrq_mitre_technique",
        "list_opencrq_jobs",
        "get_opencrq_job",
    }
    missing = required - OPENCRQ_MCP_TOOLS
    assert not missing, f"Business tools missing from OpenCRQ MCP: {missing}"


def test_allow_lists_registered_for_all_providers():
    assert set(MCP_TOOL_ALLOW_LISTS) == {"opencti", "openaev", "opencrq"}
    assert MCP_TOOL_ALLOW_LISTS["opencti"] is OPENCTI_MCP_TOOLS
    assert MCP_TOOL_ALLOW_LISTS["openaev"] is OPENAEV_MCP_TOOLS
    assert MCP_TOOL_ALLOW_LISTS["opencrq"] is OPENCRQ_MCP_TOOLS


# ── OpenCTI direct-mutation mode ─────────────────────────────────────────────


def test_opencti_direct_mutations_bypass_draft_gate():
    executor = CallableToolExecutor()
    defs = opencti_provider.build_tools(
        "http://platform.local||token", executor, direct_mutations=True
    )
    by_name = {d.name: d for d in defs}

    create_entity = by_name["create_opencti_entity"]
    assert "draft" not in create_entity.description.lower()

    # Without direct mutations the same tool carries the draft-gate prompt.
    gated_executor = CallableToolExecutor()
    gated_defs = opencti_provider.build_tools(
        "http://platform.local||token", gated_executor
    )
    gated_create = next(d for d in gated_defs if d.name == "create_opencti_entity")
    assert "draft" in gated_create.description.lower()

    # And the registered handlers differ: the gated one is the wrapper.
    assert (
        executor._handlers["create_opencti_entity"].__name__
        != "_draft_gate_wrapper"
    )
    assert (
        gated_executor._handlers["create_opencti_entity"].__name__
        == "_draft_gate_wrapper"
    )


# ── JSON-RPC dispatch ────────────────────────────────────────────────────────


def _stub_tools() -> tuple[list[ToolDefinition], ScopedToolExecutor]:
    executor = CallableToolExecutor()

    async def _echo(message: str = "") -> str:
        return f"echo: {message}"

    async def _boom() -> str:
        raise RuntimeError("platform exploded")

    executor.register("search_opencti", _echo)
    executor.register("delete_everything", _echo)  # NOT allow-listed
    executor.register("broken_tool", _boom)

    definitions = [
        ToolDefinition(
            name="search_opencti",
            description="Search",
            input_schema={"type": "object", "properties": {}},
            source="integration:opencti",
        ),
        ToolDefinition(
            name="broken_tool",
            description="Always fails",
            source="integration:opencti",
        ),
    ]
    return definitions, ScopedToolExecutor(executor, definitions)


@pytest.mark.asyncio
async def test_dispatch_initialize_and_unknown_method(monkeypatch):
    body = mcp_platform.MCPRequest(jsonrpc="2.0", id=1, method="initialize")
    result = await _dispatch_platform_mcp(
        body, "opencti", None, _fake_registration(), _fake_user()
    )
    assert result["result"]["serverInfo"]["name"].startswith("OpenCTI")
    assert result["result"]["protocolVersion"]

    body = mcp_platform.MCPRequest(jsonrpc="2.0", id=2, method="bogus/method")
    result = await _dispatch_platform_mcp(
        body, "opencti", None, _fake_registration(), _fake_user()
    )
    assert result["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_a_notification_method_sent_as_a_request_is_answered():
    """A message with an id is a request: the transport never dispatches a
    notification, and one carrying an id gets an answer, not silence."""
    body = mcp_platform.MCPRequest(
        jsonrpc="2.0", id=4, method="notifications/initialized"
    )
    result = await _dispatch_platform_mcp(
        body, "opencti", None, _fake_registration(), _fake_user()
    )
    assert result["id"] == 4
    assert result["error"]["code"] == -32601


@pytest.mark.asyncio
async def test_dispatch_tools_list_returns_only_built_definitions(monkeypatch):
    definitions, scoped = _stub_tools()

    async def _fake_build(db, provider, registration, user):
        return definitions, scoped

    monkeypatch.setattr(mcp_platform, "_build_platform_tools", _fake_build)

    body = mcp_platform.MCPRequest(jsonrpc="2.0", id=3, method="tools/list")
    result = await _dispatch_platform_mcp(
        body, "opencti", None, _fake_registration(), _fake_user()
    )
    names = {t["name"] for t in result["result"]["tools"]}
    assert names == {"search_opencti", "broken_tool"}
    for tool in result["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"


@pytest.mark.asyncio
async def test_tools_call_happy_path_and_unknown_tool(monkeypatch):
    async def _fake_track(registration):
        _fake_track.called = True

    _fake_track.called = False
    monkeypatch.setattr(
        mcp_platform, "_track_platform_tool_call", _fake_track
    )

    definitions, scoped = _stub_tools()
    registration = _fake_registration()

    result = await _handle_tools_call(
        7,
        {"name": "search_opencti", "arguments": {"message": "hi"}},
        definitions,
        scoped,
        registration,
    )
    assert result["result"]["isError"] is False
    assert result["result"]["content"][0]["text"] == "echo: hi"
    assert _fake_track.called

    # Unknown / non-allow-listed tool -> -32602 (never executed)
    result = await _handle_tools_call(
        8,
        {"name": "delete_everything", "arguments": {}},
        definitions,
        scoped,
        registration,
    )
    assert result["error"]["code"] == -32602

    # Missing params / name
    result = await _handle_tools_call(9, None, definitions, scoped, registration)
    assert result["error"]["code"] == -32602
    result = await _handle_tools_call(
        10, {"arguments": {}}, definitions, scoped, registration
    )
    assert result["error"]["code"] == -32602


# ── Platform MCP servers descriptor endpoint ────────────────────────────────


class _RegsResult:
    def __init__(self, regs):
        self._regs = regs

    def scalars(self):
        return self

    def all(self):
        return self._regs


class _RegsSession:
    def __init__(self, regs):
        self._regs = regs

    async def execute(self, _statement):
        return _RegsResult(self._regs)


@pytest.mark.asyncio
async def test_mcp_servers_descriptor_lists_all_providers(monkeypatch):
    from app.api.v1 import platform as platform_api
    from app.api.v1 import platform_settings_helpers

    async def _fake_ps(_db):
        return SimpleNamespace(platform_mcp_enabled=True)

    monkeypatch.setattr(platform_settings_helpers, "get_platform_settings", _fake_ps)

    opencti_reg = SimpleNamespace(
        platform_identifier="opencti",
        integration_id=uuid.uuid4(),
        platform_title="OpenCTI Production",
        platform_version="7.0.0",
        is_healthy=lambda: True,
    )
    db = _RegsSession([opencti_reg])

    out = await platform_api.list_platform_mcp_servers(db=db, user=_fake_user())

    assert out.enabled is True
    by_provider = {s.provider: s for s in out.servers}
    assert set(by_provider) == {"opencti", "openaev", "opencrq"}

    octi = by_provider["opencti"]
    assert octi.connected is True
    assert octi.healthy is True
    assert octi.platform_title == "OpenCTI Production"
    assert octi.url.endswith("/mcp/opencti")
    assert octi.tool_count == len(mcp_platform.OPENCTI_MCP_TOOLS)

    oaev = by_provider["openaev"]
    assert oaev.connected is False
    assert oaev.healthy is False
    assert oaev.platform_title is None
    assert oaev.tool_count == len(mcp_platform.OPENAEV_MCP_TOOLS)


@pytest.mark.asyncio
async def test_mcp_servers_descriptor_maps_legacy_openbas(monkeypatch):
    from app.api.v1 import platform as platform_api
    from app.api.v1 import platform_settings_helpers

    async def _fake_ps(_db):
        return SimpleNamespace(platform_mcp_enabled=False)

    monkeypatch.setattr(platform_settings_helpers, "get_platform_settings", _fake_ps)

    openbas_reg = SimpleNamespace(
        platform_identifier="openbas",
        integration_id=uuid.uuid4(),
        platform_title="Legacy OpenBAS",
        platform_version="1.16.0",
        is_healthy=lambda: False,
    )
    db = _RegsSession([openbas_reg])

    out = await platform_api.list_platform_mcp_servers(db=db, user=_fake_user())

    assert out.enabled is False
    oaev = next(s for s in out.servers if s.provider == "openaev")
    assert oaev.connected is True
    assert oaev.healthy is False
    assert oaev.platform_title == "Legacy OpenBAS"


@pytest.mark.asyncio
async def test_tools_call_tool_failure_surfaced_as_is_error(monkeypatch):
    async def _fake_track(registration):
        return None

    monkeypatch.setattr(mcp_platform, "_track_platform_tool_call", _fake_track)

    definitions, scoped = _stub_tools()
    result = await _handle_tools_call(
        11,
        {"name": "broken_tool", "arguments": {}},
        definitions,
        scoped,
        _fake_registration(),
    )
    assert result["result"]["isError"] is True
    assert "platform exploded" in result["result"]["content"][0]["text"]


# ── No Platform MCP Server file imports the MCP Agent Endpoints ─────────────

#: The files the Platform MCP Servers run through: the endpoints, their
#: authentication, the transport, the OAuth server and the tool metadata.
PLATFORM_MCP_SERVER_FILES = (
    "api/v1/mcp_platform.py",
    "api/v1/mcp_auth.py",
    "api/v1/mcp_transport.py",
    "api/v1/mcp_oauth.py",
    "services/mcp_tool_metadata.py",
)


def _imports_mcp_endpoint(node: ast.AST) -> bool:
    if isinstance(node, ast.Import):
        return any(alias.name == "app.api.v1.mcp_endpoint" for alias in node.names)
    if isinstance(node, ast.ImportFrom):
        if node.module == "app.api.v1.mcp_endpoint":
            return True
        return node.module == "app.api.v1" and any(alias.name == "mcp_endpoint" for alias in node.names)
    return False


def test_no_platform_mcp_server_file_imports_mcp_endpoint():
    """The Platform MCP Servers authenticate through ``mcp_auth``, not the
    Enterprise Edition ``mcp_endpoint``. Direct imports only, lazy ones
    included: what the modules imported here reach is not checked."""
    for path in PLATFORM_MCP_SERVER_FILES:
        tree = app_sources.tree(app_sources.APP_DIR / path)
        offending = [node.lineno for node in app_sources.walk(tree) if _imports_mcp_endpoint(node)]
        assert not offending, f"{path} imports app.api.v1.mcp_endpoint (line {offending})"
