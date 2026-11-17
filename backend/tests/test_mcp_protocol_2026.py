"""XTM One's MCP servers speak the MCP 2026-07-28 revision too.

The servers are dual-era: a request of the handshake-free revision (its
``_meta`` names the protocol version) is served statelessly, and a client of an
earlier revision still opens with ``initialize``. These tests pin, on a real
router:

- a modern request is validated as the transport requires: required ``_meta``
  fields (400, -32602), ``MCP-Protocol-Version`` / ``Mcp-Method`` / ``Mcp-Name``
  agreeing with the body (400 ``HeaderMismatch``, -32020, Base64 sentinel
  decoded), an unknown version (400 -32022 listing the supported ones), an
  unknown or handshake-era method (404 -32601);
- modern results carry ``resultType``, the server's identity in ``_meta`` and,
  on ``tools/list``, ``ttlMs`` / ``cacheScope: private``;
- ``server/discover`` answers in both eras; ``subscriptions/listen`` is
  acknowledged with nothing and closed gracefully over SSE;
- a legacy client is served as before;
- a modern request needs an audience-bound OAuth token, a legacy one does not;
- the authorization server names its issuer on every authorization response
  (RFC 9207) and accepts ``application_type`` (a CIMD document's required
  ``client_name`` is pinned in ``test_mcp_oauth_cimd.py``); agents are listed
  in a deterministic order.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api.v1 import mcp_auth, mcp_endpoint, mcp_oauth, mcp_platform, mcp_xtm
from app.api.v1.mcp_oauth import MCP_OAUTH_TOKEN_TYPE, ClientRegistrationRequest
from app.api.v1.mcp_transport import (
    LATEST_LEGACY_PROTOCOL_VERSION,
    META_CLIENT_CAPABILITIES,
    META_PROTOCOL_VERSION,
    META_SERVER_INFO,
    META_SUBSCRIPTION_ID,
    SUPPORTED_PROTOCOL_VERSIONS,
    TOOLS_LIST_TTL_MS,
)
from app.core.config import settings
from app.core.database import get_db
from app.services import ai_state
from app.tools.base import ToolDefinition

MODERN = "2026-07-28"
_META = {META_PROTOCOL_VERSION: MODERN, META_CLIENT_CAPABILITIES: {}}


class _Session:
    def __init__(self):
        self.info: dict = {}

    async def commit(self):
        pass


@pytest.fixture
def client(monkeypatch):
    async def _auth(_request, _db, _path):
        return SimpleNamespace(id=uuid.uuid4(), is_active=True, is_admin=True)

    async def _on(_db):
        return True

    tools = [
        ToolDefinition(name="list_agents", description="List agents.", input_schema={"type": "object"}),
        ToolDefinition(name="get_agent", description="Get an agent.", input_schema={"type": "object"}),
    ]

    class _Executor:
        async def execute_tool(self, call):
            return SimpleNamespace(content=f"ran {call.name}", is_error=False)

    async def _tools(_db, _user):
        return tools, _Executor()

    monkeypatch.setattr(mcp_xtm, "authenticate_user", _auth)
    monkeypatch.setattr(mcp_xtm, "xtm_mcp_enabled", _on)
    monkeypatch.setattr(ai_state, "is_ai_enabled", _on)
    monkeypatch.setattr("app.api.v1.platform.is_agentic_ai_licensed", _on)
    monkeypatch.setattr(mcp_xtm, "_build_xtm_tools", _tools)

    app = FastAPI()
    app.include_router(mcp_endpoint.router, prefix="/mcp")
    app.include_router(mcp_platform.router, prefix="/mcp")
    app.include_router(mcp_xtm.router, prefix="/mcp")

    async def _db():
        yield _Session()

    app.dependency_overrides[get_db] = _db
    return TestClient(app)


def _modern(method: str, *, id=1, meta=None, **params) -> dict:
    return {"jsonrpc": "2.0", "id": id, "method": method, "params": {**params, "_meta": meta or _META}}


def _headers(method: str, *, version: str = MODERN, name: str | None = None) -> dict[str, str]:
    headers = {"MCP-Protocol-Version": version, "Mcp-Method": method}
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


def _post(client, body, headers=None):
    return client.post("/mcp/xtm", json=body, headers=headers or {})


# ── Modern requests are served statelessly ───────────────────────────────────


def test_a_modern_tools_list_carries_result_type_identity_and_caching_hints(client):
    response = _post(client, _modern("tools/list"), _headers("tools/list"))
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["resultType"] == "complete"
    assert result["_meta"][META_SERVER_INFO]["name"].endswith("Platform")
    assert result["ttlMs"] == TOOLS_LIST_TTL_MS
    # The list depends on the caller's visibility: never shared between callers.
    assert result["cacheScope"] == "private"
    # Deterministic order: the order the surface is built in.
    assert [tool["name"] for tool in result["tools"]] == ["list_agents", "get_agent"]


def test_a_modern_tools_call_is_a_complete_result(client):
    response = _post(
        client,
        _modern("tools/call", name="list_agents", arguments={}),
        _headers("tools/call", name="list_agents"),
    )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["resultType"] == "complete"
    assert result["content"][0]["text"] == "ran list_agents"


def test_a_name_in_the_base64_sentinel_form_is_decoded(client):
    import base64

    encoded = "=?base64?" + base64.b64encode(b"list_agents").decode() + "?="
    response = _post(
        client,
        _modern("tools/call", name="list_agents", arguments={}),
        _headers("tools/call", name=encoded),
    )
    assert response.status_code == 200


@pytest.mark.parametrize("discover_body", ["modern", "legacy"])
def test_server_discover_is_answered_in_both_eras(client, discover_body):
    if discover_body == "modern":
        response = _post(client, _modern("server/discover"), _headers("server/discover"))
    else:
        response = _post(client, {"jsonrpc": "2.0", "id": 1, "method": "server/discover"})
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["supportedVersions"] == list(SUPPORTED_PROTOCOL_VERSIONS)
    assert result["supportedVersions"][0] == MODERN
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["_meta"][META_SERVER_INFO]["version"] == settings.app_version
    assert result["resultType"] == "complete"
    assert result["cacheScope"] == "public" and result["ttlMs"] > 0


def test_a_subscription_is_acknowledged_with_nothing_and_closed(client):
    response = _post(client, _modern("subscriptions/listen", id=7, notifications={"toolsListChanged": True}),
                     _headers("subscriptions/listen"))
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(line[len("data: "):]) for line in response.text.splitlines() if line.startswith("data: ")]
    ack, closed = events
    assert ack["method"] == "notifications/subscriptions/acknowledged"
    # These servers emit no change notification: nothing is agreed to.
    assert ack["params"]["notifications"] == {}
    assert ack["params"]["_meta"][META_SUBSCRIPTION_ID] == 7
    assert closed["id"] == 7 and closed["result"]["resultType"] == "complete"
    assert closed["result"]["_meta"][META_SUBSCRIPTION_ID] == 7


# ── Modern requests are validated ────────────────────────────────────────────


def _error(response) -> dict:
    return response.json()["error"]


def test_a_modern_header_without_meta_is_a_400_invalid_params(client):
    response = _post(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, _headers("tools/list"))
    assert response.status_code == 400
    assert _error(response)["code"] == -32602


def test_missing_client_capabilities_is_a_400_invalid_params(client):
    response = _post(client, _modern("tools/list", meta={META_PROTOCOL_VERSION: MODERN}), _headers("tools/list"))
    assert response.status_code == 400
    assert _error(response)["code"] == -32602


@pytest.mark.parametrize(
    "headers",
    [
        {"Mcp-Method": "tools/list"},  # no MCP-Protocol-Version
        {"MCP-Protocol-Version": "2025-11-25", "Mcp-Method": "tools/list"},
        {"MCP-Protocol-Version": MODERN},  # no Mcp-Method
        {"MCP-Protocol-Version": MODERN, "Mcp-Method": "tools/call"},
    ],
    ids=["no-version", "version-mismatch", "no-method", "method-mismatch"],
)
def test_a_header_that_disagrees_with_the_body_is_a_header_mismatch(client, headers):
    response = _post(client, _modern("tools/list"), headers)
    assert response.status_code == 400
    assert _error(response)["code"] == -32020
    # The error answers the request it refuses.
    assert response.json()["id"] == 1


@pytest.mark.parametrize("name", [None, "get_agent", "=?base64?not base64?="], ids=["absent", "other", "malformed"])
def test_a_tool_name_header_that_disagrees_is_a_header_mismatch(client, name):
    response = _post(client, _modern("tools/call", name="list_agents", arguments={}), _headers("tools/call", name=name))
    assert response.status_code == 400
    assert _error(response)["code"] == -32020


def test_an_unknown_version_lists_the_supported_ones(client):
    meta = {META_PROTOCOL_VERSION: "2030-01-01", META_CLIENT_CAPABILITIES: {}}
    response = _post(client, _modern("tools/list", meta=meta), _headers("tools/list", version="2030-01-01"))
    assert response.status_code == 400
    error = _error(response)
    assert error["code"] == -32022
    assert error["data"] == {"supported": list(SUPPORTED_PROTOCOL_VERSIONS), "requested": "2030-01-01"}


@pytest.mark.parametrize("method", ["resources/list", "ping", "initialize", "logging/setLevel"])
def test_an_unknown_or_handshake_era_method_is_a_404(client, method):
    response = _post(client, _modern(method), _headers(method))
    assert response.status_code == 404
    assert _error(response)["code"] == -32601


# ── Legacy clients are served as before ──────────────────────────────────────


def test_a_legacy_client_still_opens_with_initialize(client):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    response = _post(client, body)
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert "resultType" not in result


def test_an_initialize_asking_for_the_modern_revision_negotiates_a_legacy_one(client):
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": MODERN}}
    response = _post(client, body, {"MCP-Protocol-Version": MODERN})
    assert response.json()["result"]["protocolVersion"] == LATEST_LEGACY_PROTOCOL_VERSION


def test_a_legacy_tools_list_is_unchanged(client):
    response = _post(client, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, {"MCP-Protocol-Version": "2025-11-25"})
    assert response.status_code == 200
    result = response.json()["result"]
    assert "resultType" not in result and "ttlMs" not in result


# ── 2026-07-28 bodies that are not one request ───────────────────────────────
#
# Only a request (an id and a method) takes the modern path; anything else that
# names 2026-07-28 is read like any body, never refused as an unsupported
# version that the refusal itself lists as supported.


def test_a_modern_notification_is_accepted(client):
    body = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1, "_meta": _META}}
    response = _post(client, body, _headers("notifications/cancelled"))
    assert response.status_code == 202
    assert response.content == b""


def test_a_modern_client_response_is_accepted(client):
    response = _post(client, {"jsonrpc": "2.0", "id": 3, "result": {}}, {"MCP-Protocol-Version": MODERN})
    assert response.status_code == 202


def test_a_modern_batch_is_refused_as_a_batch(client):
    response = _post(client, [_modern("tools/list")], _headers("tools/list"))
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == -32600
    assert "batches are not part of MCP 2026-07-28" in error["message"]


def test_an_unknown_version_is_still_refused_with_the_supported_ones(client):
    response = _post(client, {"jsonrpc": "2.0", "method": "notifications/cancelled"}, {"MCP-Protocol-Version": "2099-01-01"})
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == -32022
    assert error["data"] == {"supported": list(SUPPORTED_PROTOCOL_VERSIONS), "requested": "2099-01-01"}


# ── Every endpoint speaks both eras, as itself ───────────────────────────────


@pytest.fixture
def every_endpoint(client, monkeypatch):
    """Every MCP endpoint past its gates, with no agent and no platform tool."""
    user = SimpleNamespace(id=uuid.uuid4(), is_active=True, is_admin=True, mcp_exposed_agent_ids=[])

    async def _yes(*_args, **_kwargs):
        return True

    async def _user(_request, _db, *_path):
        return user

    async def _global(_request, _db):
        return SimpleNamespace(mcp_exposed_agent_ids=[]), str(user.id)

    async def _nothing(*_args, **_kwargs):
        return []

    async def _registration(_db, _provider):
        return SimpleNamespace(id=uuid.uuid4())

    async def _no_platform_tools(*_args):
        return [], None

    monkeypatch.setattr("app.api.v1.platform.is_chat_licensed", _yes)
    monkeypatch.setattr(mcp_endpoint, "authenticate_user", _user)
    monkeypatch.setattr(mcp_endpoint, "_authenticate_global_endpoint", _global)
    monkeypatch.setattr(mcp_endpoint, "_get_user_agents", _nothing)
    monkeypatch.setattr(mcp_endpoint, "_get_global_agents", _nothing)
    monkeypatch.setattr(mcp_platform, "authenticate_user", _user)
    monkeypatch.setattr(mcp_platform, "_platform_mcp_enabled", _yes)
    monkeypatch.setattr(mcp_platform, "_get_active_registration", _registration)
    monkeypatch.setattr(mcp_platform, "_build_platform_tools", _no_platform_tools)
    return client


_EXPECTED_IDENTITY = {
    "/mcp/user": lambda: mcp_endpoint._identity(),
    "/mcp/global": lambda: mcp_endpoint._identity(),
    "/mcp/opencti": lambda: mcp_platform._identity("opencti"),
    "/mcp/openaev": lambda: mcp_platform._identity("openaev"),
    "/mcp/opencrq": lambda: mcp_platform._identity("opencrq"),
    "/mcp/xtm": lambda: mcp_xtm._identity(),
}


@pytest.mark.parametrize("path", list(_EXPECTED_IDENTITY))
def test_every_endpoint_discovers_and_lists_as_itself(every_endpoint, path):
    """A wrong ``identity=`` at any call site names another server."""
    expected = _EXPECTED_IDENTITY[path]().implementation()

    discover = every_endpoint.post(path, json=_modern("server/discover"), headers=_headers("server/discover"))
    assert discover.status_code == 200
    assert discover.json()["result"]["_meta"][META_SERVER_INFO] == expected

    listed = every_endpoint.post(path, json=_modern("tools/list", id=2), headers=_headers("tools/list"))
    assert listed.status_code == 200
    result = listed.json()["result"]
    assert result["_meta"][META_SERVER_INFO] == expected
    assert result["resultType"] == "complete" and result["cacheScope"] == "private"


def test_the_six_identities_are_distinct_where_the_servers_are():
    names = {path: build().name for path, build in _EXPECTED_IDENTITY.items()}
    # /mcp/user and /mcp/global are one agent server reached two ways.
    assert names["/mcp/user"] == names["/mcp/global"]
    assert len(set(names.values())) == 5


# ── Audience ─────────────────────────────────────────────────────────────────


class _UserDB:
    def __init__(self, user):
        self._user = user

    async def execute(self, _statement):
        return SimpleNamespace(scalar_one_or_none=lambda: self._user)


def _token(aud: str | None) -> str:
    payload = {
        "sub": str(uuid.uuid4()),
        "iat": datetime.now(UTC),
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        "type": MCP_OAUTH_TOKEN_TYPE,
        "client_id": "mcp-test",
    }
    if aud:
        payload["aud"] = aud
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "aud, require, accepted",
    [
        (None, False, True),  # a handshake-era client keeps the old acceptance
        (None, True, False),  # a 2026-07-28 request must present an audience
        ("{base}/mcp/xtm", True, True),
        ("{base}/mcp/user", True, False),
    ],
)
async def test_a_modern_request_needs_an_audience_bound_token(monkeypatch, aud, require, accepted):
    monkeypatch.setattr(settings, "secret_key", "local-test-secret-" + "x" * 32)
    user = SimpleNamespace(id=uuid.uuid4(), is_active=True)
    audience = aud.format(base=settings.base_url.rstrip("/")) if aud else None
    found = await mcp_auth.try_oauth_token(_token(audience), _UserDB(user), "mcp/xtm", require_audience=require)
    assert (found is user) is accepted


def test_the_audience_rule_reads_the_version_header():
    def request(headers):
        return Request({"type": "http", "method": "POST", "path": "/mcp/xtm", "headers": headers, "query_string": b""})

    from app.api.v1.mcp_transport import is_modern_request

    assert is_modern_request(request([(b"mcp-protocol-version", MODERN.encode())]))
    assert not is_modern_request(request([(b"mcp-protocol-version", b"2025-11-25")]))
    assert not is_modern_request(request([]))


# ── Authorization server ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_authorization_response_names_its_issuer(monkeypatch):
    async def _store(*_args, **_kwargs):
        return None

    async def _audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(mcp_oauth, "_store_auth_code", _store)
    monkeypatch.setattr(mcp_oauth, "log_audit", _audit)
    target = await mcp_oauth._grant_authorization_code(
        None,
        user=SimpleNamespace(id=uuid.uuid4(), full_name="A", email="a@example.com"),
        client_row=SimpleNamespace(client_name="Claude"),
        client_id="mcp-abc",
        redirect_uri="http://localhost:3118/callback",
        state="s",
        code_challenge="x" * 43,
        code_challenge_method="S256",
        resource="",
        request=None,
        login_method="mcp_oauth_session",
    )
    query = parse_qs(urlsplit(target).query)
    assert query["iss"] == [settings.base_url.rstrip("/")]
    assert query["state"] == ["s"]


@pytest.mark.asyncio
async def test_the_metadata_advertises_the_issuer_parameter():
    response = await mcp_oauth.authorization_server_metadata("")
    metadata = json.loads(response.body)
    assert metadata["authorization_response_iss_parameter_supported"] is True


class _RegistrationDB:
    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)

    async def commit(self):
        pass


@pytest.fixture
def registration(monkeypatch):
    async def _no_limit(*_args, **_kwargs):
        return None

    async def _no_eviction(_db):
        return None

    monkeypatch.setattr(mcp_oauth, "enforce_rate_limit", _no_limit)
    monkeypatch.setattr(mcp_oauth, "_evict_oldest_clients_if_full", _no_eviction)
    monkeypatch.setattr(settings, "mcp_dcr_enabled", True)


def _registration_request() -> Request:
    return Request(
        {"type": "http", "method": "POST", "path": "/mcp/oauth/register", "headers": [], "query_string": b"",
         "client": ("203.0.113.9", 4000)}
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("application_type", ["native", "web"])
async def test_registration_accepts_and_returns_the_application_type(registration, application_type):
    body = ClientRegistrationRequest(
        client_name="VS Code", redirect_uris=["http://127.0.0.1/callback"], application_type=application_type
    )
    response = await mcp_oauth.register_client(_registration_request(), body, db=_RegistrationDB())
    assert response.status_code == 201
    assert json.loads(response.body)["application_type"] == application_type


@pytest.mark.asyncio
async def test_registration_refuses_an_unknown_application_type(registration):
    body = ClientRegistrationRequest(redirect_uris=["https://app.example.com/cb"], application_type="desktop")
    db = _RegistrationDB()
    response = await mcp_oauth.register_client(_registration_request(), body, db=db)
    assert isinstance(response, JSONResponse) and response.status_code == 400
    assert json.loads(response.body)["error"] == "invalid_client_metadata"
    assert db.added == []


# ── Deterministic order ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_agents_are_listed_in_a_deterministic_order(monkeypatch):
    statements = []

    class _DB:
        async def execute(self, statement):
            statements.append(str(statement))
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    async def _groups(_db, _user_id):
        return []

    monkeypatch.setattr(mcp_endpoint, "get_user_group_ids", _groups)
    user = SimpleNamespace(id=uuid.uuid4(), mcp_exposed_agent_ids=None, is_admin=False)
    await mcp_endpoint._get_user_agents(_DB(), user)
    await mcp_endpoint._get_global_agents(_DB(), SimpleNamespace(mcp_exposed_agent_ids=[str(uuid.uuid4())]))
    assert all("ORDER BY agents.name, agents.id" in statement for statement in statements)
