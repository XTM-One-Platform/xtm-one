"""Routing surfaces: a resource bound to a relay group is reached from its network.

The egress funnel (``tests/test_egress.py``) decides where ONE request goes.
These tests pin the surfaces that hand it the route: remote MCP servers (the
SDK's own client), the MCP OAuth discovery and code exchange, the integration
OAuth callback, the element pickers and the flow HTTP node - and that a route
which cannot be read is never replaced by a direct request.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.core.egress import EgressRoute, current_route, egress_client, egress_route

ROUTE = EgressRoute(group_id="g-1", group_name="Paris DC")
GROUP_ID = str(uuid.uuid4())


def _recorder(name: str, calls: list[tuple[str, str]]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((name, str(request.url)))
        return httpx.Response(200, json={"via": name})

    return httpx.MockTransport(handler)


@pytest.fixture
def relay_calls(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(EgressRoute, "transport", lambda self: _recorder("relay", calls))
    return calls


# -- A client pinned to a route ------------------------------------------------


async def test_a_client_pinned_to_a_route_uses_it_whatever_the_context(relay_calls):
    async with egress_client(route=ROUTE) as client:
        response = await client.get("https://splunk.corp.local/services")
        with egress_route(None):
            again = await client.get("https://splunk.corp.local/again")
    assert response.json() == {"via": "relay"}
    assert again.json() == {"via": "relay"}
    assert [url for _, url in relay_calls] == [
        "https://splunk.corp.local/services",
        "https://splunk.corp.local/again",
    ]


async def test_a_client_pinned_to_direct_ignores_an_ambient_route(relay_calls):
    direct: list[tuple[str, str]] = []
    async with egress_client(route=None, transport=_recorder("direct", direct)) as client:
        with egress_route(ROUTE):
            response = await client.get("https://api.example.com/")
    assert response.json() == {"via": "direct"}
    assert relay_calls == []


# -- Remote MCP servers --------------------------------------------------------


async def test_the_mcp_client_factory_routes_every_request_of_the_sdk(relay_calls):
    from app.tools.mcp_client import mcp_http_client_factory

    client = mcp_http_client_factory(ROUTE)(headers={"X-Test": "1"})
    async with client:
        # The SDK sends from its own transport tasks: no ambient route there.
        response = await client.post("https://mcp.corp.local/mcp", json={})
    assert response.json() == {"via": "relay"}
    # The transports follow a redirect themselves, within the origin.
    assert client.follow_redirects is False
    assert client.headers["X-Test"] == "1"


def test_the_mcp_client_factory_keeps_the_sdk_defaults():
    from mcp.shared._httpx_utils import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT

    from app.tools.mcp_client import mcp_http_client_factory

    client = mcp_http_client_factory(None)()
    assert client.timeout.connect == MCP_DEFAULT_TIMEOUT
    assert client.timeout.read == MCP_DEFAULT_SSE_READ_TIMEOUT


async def test_an_unrouted_mcp_server_needs_no_lookup():
    from app.tools.mcp_client import MCPServerRuntime, runtime_egress_route

    assert await runtime_egress_route(MCPServerRuntime(name="s", transport="sse")) is None


async def test_a_routed_stdio_server_is_refused_never_started(monkeypatch):
    from app.tools import mcp_client

    async def _route(_config):
        return ROUTE

    monkeypatch.setattr(mcp_client, "runtime_egress_route", _route)
    started: list[object] = []
    monkeypatch.setattr(mcp_client, "stdio_client", lambda *a, **k: started.append(a))
    config = mcp_client.MCPServerRuntime(
        name="local", transport="stdio", command="npx", relay_group_id=GROUP_ID
    )
    with pytest.raises(Exception, match="relay group"):
        await mcp_client.MCPConnection(config).connect(force=True)
    assert started == []


def test_the_api_refuses_a_stdio_server_routed_through_a_relay_group():
    from app.api.v1.mcp_servers import _refuse_stdio_relay

    with pytest.raises(HTTPException) as refused:
        _refuse_stdio_relay("stdio", uuid.UUID(GROUP_ID))
    assert refused.value.status_code == 422
    _refuse_stdio_relay("sse", uuid.UUID(GROUP_ID))
    _refuse_stdio_relay("streamable_http", uuid.UUID(GROUP_ID))
    _refuse_stdio_relay("stdio", None)


async def test_the_runtime_carries_the_servers_relay_group():
    from app.api.v1.helpers import build_mcp_runtime

    srv = SimpleNamespace(
        id=uuid.uuid4(),
        name="Splunk MCP",
        transport="streamable_http",
        command=None,
        args=None,
        env_enc=None,
        url="https://splunk-mcp.corp.local/mcp",
        headers_enc=None,
        auth_mode="none",
        access_token_enc=None,
        is_company_managed=False,
        relay_group_id=uuid.UUID(GROUP_ID),
    )
    assert (await build_mcp_runtime(srv)).relay_group_id == GROUP_ID
    srv.relay_group_id = None
    assert (await build_mcp_runtime(srv)).relay_group_id is None


async def test_the_oauth_discovery_probes_from_the_chosen_group(monkeypatch):
    from app.api.v1 import mcp_servers
    from app.api.v1.schemas.mcp_server import MCPOAuthDiscoveryRequest
    from app.core import database, rate_limit
    from app.services import mcp_oauth_discovery

    seen: dict[str, object] = {}

    async def _checked(db, *, user, value, baseline=None, is_admin=None):
        seen["checked"] = value
        return uuid.UUID(value)

    async def _routes(db, targets):
        seen["targets"] = targets
        return {"discovery": ROUTE}

    async def _discover(**kwargs):
        seen["route"] = current_route()
        return SimpleNamespace(
            authorize_url="https://idp.corp.local/authorize",
            token_url="https://idp.corp.local/token",
            registration_url="https://idp.corp.local/register",
            resource=None,
            scopes=None,
            issuer=None,
            discovered=True,
        )

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(mcp_servers, "checked_relay_group_id", _checked)
    monkeypatch.setattr(mcp_servers, "load_egress_routes", _routes)
    monkeypatch.setattr(mcp_oauth_discovery, "discover_oauth_endpoints", _discover)
    monkeypatch.setattr(rate_limit, "enforce_rate_limit", _noop)
    monkeypatch.setattr(database, "release_db_connection_before_loop", _noop)

    body = MCPOAuthDiscoveryRequest(url="https://mcp.corp.local/mcp", relay_group_id=GROUP_ID)
    result = await mcp_servers.discover_mcp_oauth(
        body, SimpleNamespace(), SimpleNamespace(id=uuid.uuid4()), SimpleNamespace()
    )
    assert seen["checked"] == GROUP_ID
    assert seen["route"] is ROUTE
    assert result.token_url == "https://idp.corp.local/token"
    assert current_route() is None


# -- Integrations --------------------------------------------------------------


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _OneRowDB:
    def __init__(self, row):
        self.row = row

    async def execute(self, *_args, **_kwargs):
        return _Result(self.row)


async def test_the_integration_oauth_callback_runs_under_the_integrations_route(monkeypatch):
    from app.api import oauth_callbacks

    integration = SimpleNamespace(id=uuid.uuid4(), relay_group_id=uuid.UUID(GROUP_ID))
    seen: dict[str, object] = {}

    async def _route(db, integ):
        return ROUTE if integ is integration else None

    async def _complete(integ, state_data, code, db):
        seen["route"] = current_route()
        return "redirect"

    monkeypatch.setattr(oauth_callbacks, "integration_egress_route", _route)
    monkeypatch.setattr(oauth_callbacks, "_complete_integration_callback", _complete)
    state = {"integration_id": str(integration.id), "user_id": str(uuid.uuid4()), "provider": "jira"}
    assert await oauth_callbacks._handle_integration_callback(state, "code", _OneRowDB(integration)) == (
        "redirect"
    )
    assert seen["route"] is ROUTE
    assert current_route() is None


async def test_the_element_pickers_list_from_the_integrations_network(monkeypatch):
    from app.api.v1 import integration_elements
    from app.core.security import encrypt_value

    integration = SimpleNamespace(
        id=uuid.uuid4(),
        provider="github",
        access_token_enc=encrypt_value("gho_token"),
        api_key_enc=None,
        relay_group_id=uuid.UUID(GROUP_ID),
    )
    seen: dict[str, object] = {}

    async def _get(*args, **kwargs):
        return integration

    async def _route(db, integ):
        return ROUTE

    async def _list(provider, element_type, token):
        seen["route"] = current_route()
        return [{"id": "acme/infra", "name": "acme/infra"}]

    monkeypatch.setattr(integration_elements, "get_user_integration", _get)
    async def _holder(_db, integ, *_args, **_kwargs):
        return integ

    monkeypatch.setattr(integration_elements, "usable_credential_holder", _holder)
    monkeypatch.setattr(integration_elements, "integration_egress_route", _route)
    monkeypatch.setattr(integration_elements, "_list_provider_elements", _list)
    result = await integration_elements.list_integration_elements(
        str(integration.id), "repository", SimpleNamespace(id=uuid.uuid4(), is_admin=False), None
    )
    assert result["elements"] == [{"id": "acme/infra", "name": "acme/infra"}]
    assert seen["route"] is ROUTE


# -- Flow HTTP node ------------------------------------------------------------


def test_the_http_node_offers_a_relay_group_picker():
    from app.services.flow_catalog import DYNAMIC_SOURCES, SOURCE_RELAY_GROUPS, get_node_kind

    spec = get_node_kind("action.http_request")
    field = next(f for f in spec.config_fields if f.name == "relay_group_id")
    assert field.source == SOURCE_RELAY_GROUPS
    assert not field.required
    assert SOURCE_RELAY_GROUPS in DYNAMIC_SOURCES


async def test_the_picker_is_hidden_where_relays_are_off(monkeypatch):
    from app.api.v1 import flows
    from app.services import relays

    async def _sources(db, user, **kwargs):
        return {}

    monkeypatch.setattr(flows, "catalog_sources", _sources)
    monkeypatch.setattr(relays, "relays_active", lambda: False)
    off = await flows.get_node_catalog(SimpleNamespace(id=uuid.uuid4(), is_admin=False), None)
    monkeypatch.setattr(relays, "relays_active", lambda: True)
    on = await flows.get_node_catalog(SimpleNamespace(id=uuid.uuid4(), is_admin=False), None)

    def _fields(catalog) -> set[str]:
        node = next(nk for nk in catalog.node_kinds if nk["kind"] == "action.http_request")
        return {f["name"] for f in node["config_fields"]}

    assert "relay_group_id" not in _fields(off)
    assert "relay_group_id" in _fields(on)
    assert {"method", "url", "headers", "body"} <= _fields(off)


def _http_runner():
    from app.services.flow_runner import FlowWalker

    runner = FlowWalker.__new__(FlowWalker)
    runner.db = None
    runner.run = SimpleNamespace(flow_id=uuid.uuid4())
    runner._renderer = None
    return runner


async def test_a_routed_http_node_reaches_a_private_address_through_the_relay(
    monkeypatch, relay_calls
):
    from app.services import relays

    async def _route_for(db, source_type, source_id, group_id):
        assert (source_type, group_id) == ("flow", GROUP_ID)
        return ROUTE

    monkeypatch.setattr(relays, "egress_route_for", _route_for)
    outcome = await _http_runner()._exec_http_request(
        {"method": "GET", "url": "http://10.20.0.5:8089/services", "relay_group_id": GROUP_ID}
    )
    assert outcome.error is None
    assert outcome.outputs["status"] == 200
    assert relay_calls == [("relay", "http://10.20.0.5:8089/services")]


async def test_an_http_node_with_an_unreadable_group_is_never_sent_directly(relay_calls):
    outcome = await _http_runner()._exec_http_request(
        {"method": "GET", "url": "https://api.example.com/", "relay_group_id": "not-a-group"}
    )
    assert outcome.error and "relay group" in outcome.error
    assert relay_calls == []


async def test_an_unrouted_http_node_keeps_the_public_address_policy(relay_calls):
    outcome = await _http_runner()._exec_http_request(
        {"method": "GET", "url": "http://10.20.0.5:8089/services"}
    )
    assert outcome.error and outcome.error.startswith("URL refused")
    assert relay_calls == []
