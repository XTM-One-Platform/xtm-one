"""The Streamable HTTP transport of XTM One's MCP servers.

``app/api/v1/mcp_transport.py`` is the one transport of ``/mcp/user``,
``/mcp/global``, ``/mcp/opencti``, ``/mcp/openaev``, ``/mcp/opencrq`` and
``/mcp/xtm``. These tests pin what the specification asks of a server:

- ``initialize`` echoes a supported protocol version and answers the latest
  otherwise, and ``serverInfo`` carries a title and a description;
- ``MCP-Protocol-Version``: unsupported -> 400, absent -> 2025-03-26;
- JSON-RPC batches under 2025-03-26 only, bounded (messages, ``tools/call``,
  no ``initialize``), each element answered on its own; notifications and
  client responses are never dispatched and get no answer (202); a
  ``jsonrpc`` other than ``"2.0"`` is an invalid request;
- an ``Origin`` the platform does not serve -> 403, before authentication;
- ``GET`` / ``DELETE`` -> 405 with ``Allow: POST`` on every endpoint;
- invalid arguments and failed calls are tool execution errors (``isError``).
"""

import json
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.api.v1 import mcp_endpoint, mcp_platform, mcp_transport, mcp_xtm
from app.api.v1.mcp_transport import (
    DEFAULT_PROTOCOL_VERSION,
    LATEST_LEGACY_PROTOCOL_VERSION,
    LEGACY_PROTOCOL_VERSIONS,
    MAX_BATCH_MESSAGES,
    MAX_BATCH_TOOL_CALLS,
    IncomingMessages,
    MCPRequest,
    ServerIdentity,
    dispatch_all,
    negotiate_protocol_version,
    read_messages,
    require_valid_origin,
    respond,
)
from app.core.config import settings
from app.core.database import get_db
from app.services import ai_state

_ENDPOINTS = ["/mcp/user", "/mcp/global", "/mcp/opencti", "/mcp/openaev", "/mcp/opencrq", "/mcp/xtm"]


def _request(body: object = None, headers: dict[str, str] | None = None) -> Request:
    raw = b"" if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/mcp/xtm",
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
            "query_string": b"",
        },
        receive,
    )


def _message(method: str, id: int | None = 1, **params) -> dict:
    body: dict = {"jsonrpc": "2.0", "method": method}
    if id is not None:
        body["id"] = id
    if params:
        body["params"] = params
    return body


# ── Version negotiation ──────────────────────────────────────────────────────


_IDENTITY = ServerIdentity(name="XTM One", description="Test server")


@pytest.mark.parametrize("version", LEGACY_PROTOCOL_VERSIONS)
def test_a_supported_version_is_echoed(version):
    assert negotiate_protocol_version({"protocolVersion": version}) == version


@pytest.mark.parametrize(
    "params",
    [
        None,
        {},
        {"protocolVersion": "2024-11-05"},
        {"protocolVersion": 7},
        # The handshake does not exist in 2026-07-28: initialize answers a legacy revision.
        {"protocolVersion": "2026-07-28"},
    ],
)
def test_anything_else_is_answered_with_the_latest(params):
    assert negotiate_protocol_version(params) == LATEST_LEGACY_PROTOCOL_VERSION


@pytest.mark.parametrize(
    "initialize",
    [
        lambda params: mcp_endpoint._handle_initialize(1, params),
        lambda params: mcp_platform._handle_initialize(1, "opencti", params),
        lambda params: mcp_xtm._handle_initialize(1, params),
    ],
    ids=["agents", "platform", "xtm"],
)
def test_every_server_negotiates_and_describes_itself(initialize):
    """``title`` (2025-06-18) and ``description`` (2025-11-25) go only to a
    client whose revision defines them: a strict client validates
    ``serverInfo`` against its own ``Implementation``."""
    keys_by_version = {
        "2025-03-26": {"name", "version"},
        "2025-06-18": {"name", "version", "title"},
        "2025-11-25": {"name", "version", "title", "description"},
    }
    for version, keys in keys_by_version.items():
        result = initialize({"protocolVersion": version})["result"]
        assert result["protocolVersion"] == version
        info = result["serverInfo"]
        assert set(info) == keys
        assert info["name"] and info.get("title", info["name"]) == info["name"]
        assert info["version"] == settings.app_version
        assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert initialize({"protocolVersion": "2025-11-25"})["result"]["serverInfo"]["description"]

    assert initialize(None)["result"]["protocolVersion"] == LATEST_LEGACY_PROTOCOL_VERSION


# ── MCP-Protocol-Version and the body ────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_version_header_speaks_2025_03_26():
    incoming = await read_messages(_request(_message("ping")))
    assert isinstance(incoming, IncomingMessages)
    assert incoming.protocol_version == DEFAULT_PROTOCOL_VERSION == "2025-03-26"


@pytest.mark.asyncio
async def test_an_unsupported_version_header_is_a_400():
    response = await read_messages(
        _request(_message("ping"), {"MCP-Protocol-Version": "1999-01-01"})
    )
    assert isinstance(response, JSONResponse)
    assert response.status_code == 400
    error = json.loads(response.body)["error"]
    assert error["code"] == -32022
    assert error["data"]["requested"] == "1999-01-01"
    assert error["data"]["supported"][0] == "2026-07-28"


@pytest.mark.asyncio
async def test_an_initialize_is_never_refused_for_its_version_header():
    """The revision is negotiated there: a newer client gets an answer."""
    incoming = await read_messages(
        _request(_message("initialize", protocolVersion="2030-01-01"), {"MCP-Protocol-Version": "2030-01-01"})
    )
    assert isinstance(incoming, IncomingMessages)
    assert [m.method for m in incoming.requests] == ["initialize"]
    assert negotiate_protocol_version(incoming.requests[0].params) == LATEST_LEGACY_PROTOCOL_VERSION


@pytest.mark.asyncio
async def test_a_body_that_is_not_json_is_a_400_parse_error():
    response = await read_messages(_request(b"{not json"))
    assert response.status_code == 400
    assert json.loads(response.body)["error"]["code"] == -32700


@pytest.mark.asyncio
async def test_a_single_invalid_request_is_a_400():
    response = await read_messages(_request({"jsonrpc": "2.0", "id": 1}))
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_a_batch_is_read_under_2025_03_26():
    incoming = await read_messages(
        _request(
            [_message("ping", 1), _message("tools/list", 2), "junk", {"jsonrpc": "2.0", "id": 9, "result": {}}],
            {"MCP-Protocol-Version": "2025-03-26"},
        )
    )
    assert incoming.batch
    assert [m.method for m in incoming.requests] == ["ping", "tools/list"]
    # The junk element is answered; the client's own response is not.
    assert [e["error"]["code"] for e in incoming.errors] == [-32600]


@pytest.mark.asyncio
@pytest.mark.parametrize("version", ["2025-06-18", "2025-11-25"])
async def test_a_batch_is_refused_by_the_revisions_that_removed_it(version):
    response = await read_messages(
        _request([_message("ping")], {"MCP-Protocol-Version": version})
    )
    assert response.status_code == 400
    assert "batches" in json.loads(response.body)["error"]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [0, MAX_BATCH_MESSAGES + 1])
async def test_an_empty_or_oversized_batch_is_a_400(size):
    response = await read_messages(_request([_message("ping", i) for i in range(size)]))
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_notifications_get_no_answer_and_a_bare_202():
    seen = []

    async def dispatch(message: MCPRequest):
        seen.append(message.method)
        return {"jsonrpc": "2.0", "id": message.id, "result": {}}

    incoming = IncomingMessages(
        batch=True,
        protocol_version="2025-03-26",
        requests=[
            MCPRequest(method="notifications/initialized"),
            # A call without an id has nobody to answer: never run.
            MCPRequest(method="tools/call", params={"name": "delete_agent"}),
        ],
    )
    answers = await dispatch_all(incoming, dispatch, identity=_IDENTITY)
    # Never dispatched: these servers act on no notification.
    assert seen == []
    assert answers == []
    assert respond(incoming, answers).status_code == 202


@pytest.mark.asyncio
@pytest.mark.parametrize("envelope", [{}, {"jsonrpc": "1.0"}, {"jsonrpc": 2}], ids=["missing", "1.0", "number"])
async def test_a_jsonrpc_version_other_than_2_0_is_an_invalid_request(envelope):
    message = {"id": 7, "method": "ping", **envelope}
    response = await read_messages(_request(message))
    assert response.status_code == 400
    assert json.loads(response.body)["error"]["code"] == -32600

    incoming = await read_messages(_request([message, _message("ping", 8)]))
    assert [m.id for m in incoming.requests] == [8]
    assert incoming.errors == [
        {"jsonrpc": "2.0", "id": 7, "error": {"code": -32600, "message": "Invalid request"}}
    ]


@pytest.mark.asyncio
async def test_an_initialize_is_never_part_of_a_batch():
    response = await read_messages(_request([_message("initialize", 1), _message("ping", 2)]))
    assert response.status_code == 400
    assert "initialize" in json.loads(response.body)["error"]["message"]


@pytest.mark.asyncio
async def test_a_batch_runs_a_bounded_number_of_tools():
    """A ``tools/call`` on /mcp/user runs a whole agent loop."""
    calls = [_message("tools/call", i, name="agent") for i in range(MAX_BATCH_TOOL_CALLS)]
    incoming = await read_messages(_request([*calls, _message("tools/list", 99)]))
    assert isinstance(incoming, IncomingMessages)
    assert len(incoming.requests) == MAX_BATCH_TOOL_CALLS + 1

    one_more = [*calls, _message("tools/call", 100, name="agent")]
    response = await read_messages(_request(one_more))
    assert response.status_code == 400
    assert "tools/call" in json.loads(response.body)["error"]["message"]


@pytest.mark.asyncio
async def test_a_failing_element_of_a_batch_keeps_the_other_answers(caplog):
    """The first call may have acted already: its answer must come back."""

    async def dispatch(message: MCPRequest):
        if message.id == 2:
            raise RuntimeError("database gone")
        return {"jsonrpc": "2.0", "id": message.id, "result": {}}

    batch = IncomingMessages(
        batch=True,
        protocol_version="2025-03-26",
        requests=[MCPRequest(id=1, method="tools/call"), MCPRequest(id=2, method="tools/call")],
    )
    with caplog.at_level(logging.ERROR):
        answers = await dispatch_all(batch, dispatch, identity=_IDENTITY)
    assert answers == [
        {"jsonrpc": "2.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 2, "error": {"code": -32603, "message": "Internal error"}},
    ]
    assert "database gone" not in json.dumps(answers)

    single = IncomingMessages(
        batch=False, protocol_version="2025-03-26", requests=[MCPRequest(id=2, method="tools/call")]
    )
    with pytest.raises(RuntimeError):
        await dispatch_all(single, dispatch, identity=_IDENTITY)


@pytest.mark.asyncio
async def test_a_batch_is_answered_in_order_and_a_single_message_alone():
    async def dispatch(message: MCPRequest):
        return {"jsonrpc": "2.0", "id": message.id, "result": message.method}

    batch = IncomingMessages(
        batch=True,
        protocol_version="2025-03-26",
        requests=[MCPRequest(id=1, method="a"), MCPRequest(id=2, method="b")],
    )
    response = respond(batch, await dispatch_all(batch, dispatch, identity=_IDENTITY))
    assert [a["id"] for a in json.loads(response.body)] == [1, 2]

    single = IncomingMessages(
        batch=False, protocol_version="2025-11-25", requests=[MCPRequest(id=3, method="c")]
    )
    response = respond(single, await dispatch_all(single, dispatch, identity=_IDENTITY))
    assert json.loads(response.body) == {"jsonrpc": "2.0", "id": 3, "result": "c"}


# ── Origin ───────────────────────────────────────────────────────────────────


@pytest.fixture
def _origins(monkeypatch):
    monkeypatch.setattr(settings, "base_url", "https://xtm.example.com")
    monkeypatch.setattr(settings, "frontend_url", "https://xtm.example.com")
    monkeypatch.setattr(settings, "cors_origins", ["http://localhost:5173"])


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "https://xtm.example.com",
        "https://xtm.example.com:443",
        "http://localhost:5173",
        "chrome-extension://abcdefghijklmnop",
        "vscode-file://vscode-app",
    ],
)
def test_an_origin_the_platform_serves_or_no_browser_origin_passes(_origins, origin):
    headers = {} if origin is None else {"Origin": origin}
    require_valid_origin(_request(headers=headers))


@pytest.mark.parametrize(
    "origin",
    ["https://evil.example", "http://xtm.example.com", "https://xtm.example.com:8443", "null"],
)
def test_another_web_origin_is_a_403(_origins, origin):
    with pytest.raises(HTTPException) as exc_info:
        require_valid_origin(_request(headers={"Origin": origin}))
    assert exc_info.value.status_code == 403


@pytest.mark.parametrize(
    ("configured", "sent"),
    [
        ("HTTPS://APP.Example.com", "https://app.example.com"),
        ("https://app.example.com/", "https://app.example.com"),
        ("https://app.example.com:443", "https://app.example.com"),
        ("http://[::1]:3000", "http://[::1]:3000"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
    ],
    ids=["uppercase", "trailing-slash", "default-port", "ipv6", "ipv4"],
)
def test_a_configured_origin_is_read_as_a_browser_sends_it(_origins, monkeypatch, configured, sent):
    monkeypatch.setattr(settings, "cors_origins", [configured])
    require_valid_origin(_request(headers={"Origin": sent}))


def test_an_unreadable_configured_origin_is_logged_once(_origins, monkeypatch, caplog):
    monkeypatch.setattr(settings, "cors_origins", ["http://my_host:3000"])
    monkeypatch.setattr(mcp_transport, "_UNREADABLE_ORIGINS_LOGGED", set())
    with caplog.at_level(logging.WARNING, logger=mcp_transport.logger.name):
        for _ in range(3):
            mcp_transport.allowed_origins()
    assert [r.getMessage().count("'http://my_host:3000'") for r in caplog.records] == [1]


def test_a_refusal_names_the_origin_without_letting_it_forge_a_line(_origins, caplog):
    with caplog.at_level(logging.INFO, logger=mcp_transport.logger.name), pytest.raises(HTTPException):
        require_valid_origin(_request(headers={"Origin": "https://evil.example\nFAKE"}))
    (record,) = caplog.records
    assert record.levelno == logging.INFO
    assert "'https://evil.example\\nFAKE'" in record.getMessage()


def test_cors_origins_star_admits_every_origin(_origins, monkeypatch):
    """``CORS_ORIGINS=*`` opened the platform to every page; this check follows."""
    monkeypatch.setattr(settings, "cors_origins", ["*"])
    for origin in ("https://evil.example", "null"):
        require_valid_origin(_request(headers={"Origin": origin}))


# ── The endpoints themselves ─────────────────────────────────────────────────


class _Session:
    def __init__(self):
        self.info: dict = {}
        self.commits = 0

    async def commit(self):
        self.commits += 1


@pytest.fixture
def client(monkeypatch):
    app = FastAPI()
    app.include_router(mcp_endpoint.router, prefix="/mcp")
    app.include_router(mcp_platform.router, prefix="/mcp")
    app.include_router(mcp_xtm.router, prefix="/mcp")
    session = _Session()

    async def _db():
        yield session

    app.dependency_overrides[get_db] = _db
    test_client = TestClient(app)
    test_client.session = session
    return test_client


@pytest.mark.parametrize("path", _ENDPOINTS)
@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_get_and_delete_are_405_on_every_endpoint(client, path, method):
    response = client.request(method, path)
    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


@pytest.mark.parametrize("path", _ENDPOINTS)
def test_a_foreign_origin_is_refused_before_authentication(client, path, monkeypatch):
    monkeypatch.setattr(settings, "cors_origins", [])
    response = client.post(path, json=_message("ping"), headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


@pytest.fixture
def xtm_client(client, monkeypatch):
    async def _auth(_request, _db, _path):
        return SimpleNamespace(id=uuid.uuid4(), is_active=True, is_admin=True)

    async def _on(_db):
        return True

    async def _no_tools(_db, _user):
        return [], None

    monkeypatch.setattr(mcp_xtm, "authenticate_user", _auth)
    monkeypatch.setattr(mcp_xtm, "xtm_mcp_enabled", _on)
    monkeypatch.setattr(ai_state, "is_ai_enabled", _on)
    monkeypatch.setattr("app.api.v1.platform.is_agentic_ai_licensed", _on)
    monkeypatch.setattr(mcp_xtm, "_build_xtm_tools", _no_tools)
    return client


def test_an_endpoint_negotiates_through_the_transport(xtm_client):
    response = xtm_client.post(
        "/mcp/xtm",
        json=_message("initialize", protocolVersion="2025-06-18"),
        headers={"MCP-Protocol-Version": "2025-06-18"},
    )
    assert response.status_code == 200
    assert response.json()["result"]["protocolVersion"] == "2025-06-18"
    assert xtm_client.session.commits == 1


def test_an_endpoint_answers_a_batch_under_2025_03_26(xtm_client):
    response = xtm_client.post(
        "/mcp/xtm",
        json=[_message("ping", 1), _message("notifications/initialized", None), _message("tools/list", 2)],
    )
    assert response.status_code == 200
    answers = response.json()
    assert [a["id"] for a in answers] == [1, 2]
    assert answers[1]["result"] == {"tools": []}


def test_an_endpoint_refuses_an_unsupported_version_header(xtm_client):
    response = xtm_client.post(
        "/mcp/xtm", json=_message("ping"), headers={"MCP-Protocol-Version": "2030-01-01"}
    )
    assert response.status_code == 400


def test_a_notification_alone_is_202(xtm_client):
    response = xtm_client.post("/mcp/xtm", json=_message("notifications/initialized", None))
    assert response.status_code == 202
    assert response.content == b""


def test_a_notification_method_with_an_id_is_answered(xtm_client):
    """A message with an id is a request: the client waits for its answer."""
    response = xtm_client.post("/mcp/xtm", json=_message("notifications/initialized", 5))
    assert response.status_code == 200
    assert response.json()["id"] == 5
    assert response.json()["error"]["code"] == -32601


# ── Tool errors are tool results ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_failed_agent_run_is_a_tool_execution_error(monkeypatch):
    agent = SimpleNamespace(slug="analyst", name="Analyst", persona="")

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("provider down")

    monkeypatch.setattr(mcp_endpoint, "_execute_agent_tool", _boom)
    result = await mcp_endpoint._handle_tools_call(
        1, {"name": "analyst", "arguments": {"message": "hi"}}, [agent], None, "u1"
    )
    assert result["result"]["isError"] is True
    assert "Agent execution failed" in result["result"]["content"][0]["text"]
    # The provider's message never reaches the client.
    assert "provider down" not in result["result"]["content"][0]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{}, {"message": ""}, "nope"])
async def test_invalid_agent_arguments_are_a_tool_execution_error(arguments):
    agent = SimpleNamespace(slug="analyst", name="Analyst", persona="")
    result = await mcp_endpoint._handle_tools_call(
        1, {"name": "analyst", "arguments": arguments}, [agent], None, "u1"
    )
    assert result["result"]["isError"] is True


@pytest.mark.asyncio
async def test_null_agent_arguments_are_no_arguments():
    """Some clients send an absent optional field as null."""
    agent = SimpleNamespace(slug="analyst", name="Analyst", persona="")
    as_null = await mcp_endpoint._handle_tools_call(
        1, {"name": "analyst", "arguments": None}, [agent], None, "u1"
    )
    as_empty = await mcp_endpoint._handle_tools_call(1, {"name": "analyst", "arguments": {}}, [agent], None, "u1")
    assert as_null == as_empty
    assert "must be an object" not in as_null["result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_an_unknown_tool_stays_a_protocol_error():
    result = await mcp_endpoint._handle_tools_call(
        1, {"name": "nobody", "arguments": {"message": "hi"}}, [], None, "u1"
    )
    assert result["error"]["code"] == -32602


def test_the_three_modules_read_the_body_through_the_transport():
    for module in (mcp_endpoint, mcp_platform, mcp_xtm):
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "request.json()" not in source, module.__name__
        assert "read_messages(request)" in source, module.__name__
    assert mcp_transport.MCPRequest is mcp_endpoint.MCPRequest
