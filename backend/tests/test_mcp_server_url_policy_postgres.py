"""The MCP server URL policy on the real routes (create, update, import, test, test-tool).

Driven through the MCP servers router against real PostgreSQL, as a non-admin
owner. DNS is a stand-in (``mcp_destinations._resolve``) so a name can answer
an internal address; the servers a refused URL points at are real sockets on
loopback that count the connections they accept, so "refused" means the
platform never connected, not merely that an error came back.

``integration``-marked and skips when PostgreSQL is unreachable; the temporary
database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import mcp_servers
from app.core import security_options
from app.models.mcp_server import MCPServer
from app.models.user import User
from app.tools import mcp_destinations
from app.tools.mcp_destinations import REFUSED_DETAIL
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

PREFIX = "/api/v1/mcp-servers"


def _policy(policy: str, *origins: str) -> None:
    options = security_options.SECURITY_OPTIONS
    security_options.publish_stored(
        {
            "mcp_server_url_policy": options["mcp_server_url_policy"].normalize(policy),
            "mcp_server_trusted_origins": options["mcp_server_trusted_origins"].normalize(list(origins)),
        }
    )


@pytest.fixture
def dns(monkeypatch) -> dict[str, list[str]]:
    """Name -> addresses for every lookup the policy makes, on save and on connect."""
    table: dict[str, list[str]] = {}

    async def _resolve(host: str, port: int) -> list:
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "unknown")
        return [
            (socket.AF_INET6 if ":" in a else socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, port))
            for a in table[host]
        ]

    monkeypatch.setattr(mcp_destinations, "_resolve", _resolve)
    return table


@pytest.fixture(autouse=True)
def _no_failure_bookkeeping(monkeypatch):
    # The connection-failure counters and circuit breaker live in Redis; they
    # are not what this suite tests.
    from app.tools import mcp_client

    async def _zero(*_a, **_k):
        return 0

    async def _false(*_a, **_k):
        return False

    for name in ("_record_mcp_connection_failure", "_reset_mcp_connection_failures", "_open_mcp_circuit", "_close_mcp_circuit"):
        monkeypatch.setattr(mcp_client, name, _zero)
    monkeypatch.setattr(mcp_client, "_is_mcp_circuit_open", _false)


@dataclass
class _Listener:
    port: int
    accepted: list[str] = field(default_factory=list)


@asynccontextmanager
async def _listener(*, redirect_to: str | None = None) -> AsyncIterator[_Listener]:
    """A loopback HTTP server: a 307 to *redirect_to*, else a 500, to every request."""
    record = _Listener(port=0)

    async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        record.accepted.append(writer.get_extra_info("peername")[0])
        with contextlib.suppress(asyncio.IncompleteReadError):
            await reader.readuntil(b"\r\n\r\n")
        if redirect_to:
            head = f"HTTP/1.1 307 Temporary Redirect\r\nLocation: {redirect_to}\r\n"
        else:
            head = "HTTP/1.1 500 Internal Server Error\r\n"
        writer.write(f"{head}Content-Length: 0\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(_handle, "127.0.0.1", 0)
    record.port = server.sockets[0].getsockname()[1]
    try:
        yield record
    finally:
        server.close()
        await server.wait_closed()


@asynccontextmanager
async def _scratch() -> AsyncIterator[tuple]:
    async with scratch_database(
        "mcp_url_policy", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            owner = User(email="owner@example.com", hashed_password="x", full_name="Owner", role="user")
            db.add(owner)
            await db.commit()
            await db.refresh(owner)
        yield scratch, owner


@asynccontextmanager
async def _client(scratch, user: User) -> AsyncIterator[httpx.AsyncClient]:
    # The real ``get_db`` (bound to the scratch database): its teardown is
    # what survives the cancellation an MCP SDK task group leaves behind when
    # a connection fails, which a test override would not.
    app = FastAPI()
    app.include_router(mcp_servers.router, prefix=PREFIX)
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[deps.require_agentic_ai] = lambda: None
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _count(scratch) -> int:
    async with scratch.sessions() as db:
        return (await db.execute(select(func.count()).select_from(MCPServer))).scalar_one()


async def _stored(scratch, user: User, url: str, transport: str = "sse") -> str:
    """A remote server saved before the policy was tightened (no save check)."""
    async with scratch.sessions() as db:
        srv = MCPServer(name="Stored", user_id=user.id, transport=transport, url=url, auth_mode="headers")
        db.add(srv)
        await db.commit()
        return str(srv.id)


# ---------------------------------------------------------------------------
# Save: create
# ---------------------------------------------------------------------------

# (policy, url, dns answers, admitted)
_CREATE_CASES = [
    ("permissive", "http://localhost:8080/mcp", {"localhost": ["127.0.0.1"]}, True),
    ("permissive", "http://10.0.0.5:8080/mcp", {}, True),
    ("permissive", "http://169.254.169.254/latest/meta-data/", {}, False),
    ("permissive", "http://[fd00:ec2::254]/", {}, False),
    ("permissive", "http://0xa9fea9fe/", {}, False),
    ("permissive", "http://metadata.example.com/", {"metadata.example.com": ["169.254.169.254"]}, False),
    ("private_networks", "http://127.0.0.1:8080/mcp", {}, False),
    ("private_networks", "http://[::1]:8080/mcp", {}, False),
    ("private_networks", "http://[::ffff:127.0.0.1]:8080/mcp", {}, False),
    ("private_networks", "http://169.254.10.10/mcp", {}, False),
    ("private_networks", "http://[fe80::1]/mcp", {}, False),
    ("private_networks", "http://localhost:8080/mcp", {"localhost": ["127.0.0.1"]}, False),
    ("private_networks", "https://rebind.example.com/mcp", {"rebind.example.com": ["93.184.216.34", "127.0.0.1"]}, False),
    ("private_networks", "http://10.0.0.5:8080/mcp", {}, True),
    ("private_networks", "https://mcp.example.com/mcp", {"mcp.example.com": ["93.184.216.34"]}, True),
    ("private_networks", "https://not-yet-up.example.com/mcp", {}, True),
    ("public_only", "http://10.0.0.5:8080/mcp", {}, False),
    ("public_only", "http://[fd12:3456::1]/mcp", {}, False),
    ("public_only", "https://intranet.corp/mcp", {"intranet.corp": ["10.9.8.7"]}, False),
    ("public_only", "https://mcp.example.com/mcp", {"mcp.example.com": ["93.184.216.34"]}, True),
]


@pytest.mark.parametrize(("policy", "url", "answers", "admitted"), _CREATE_CASES, ids=[f"{c[0]}-{c[1]}" for c in _CREATE_CASES])
async def test_create_holds_the_url_to_the_policy(policy, url, answers, admitted, dns):
    dns.update(answers)
    async with _scratch() as (scratch, owner):
        _policy(policy)
        async with _client(scratch, owner) as client:
            response = await client.post(PREFIX, json={"name": "Remote", "transport": "streamable_http", "url": url})
        if admitted:
            assert response.status_code == 201, response.text
            assert response.json()["url"] == url
            assert await _count(scratch) == 1
        else:
            assert response.status_code == 422, response.text
            assert REFUSED_DETAIL in response.text
            assert await _count(scratch) == 0


async def test_a_trusted_origin_is_saved_under_public_only(dns):
    dns["mcp.internal"] = ["10.0.0.5"]
    async with _scratch() as (scratch, owner):
        _policy("public_only", "http://mcp.internal:8080")
        async with _client(scratch, owner) as client:
            trusted = await client.post(PREFIX, json={"name": "A", "transport": "sse", "url": "http://mcp.internal:8080/sse"})
            other_port = await client.post(PREFIX, json={"name": "B", "transport": "sse", "url": "http://mcp.internal:9090/sse"})
            metadata = await client.post(
                PREFIX, json={"name": "C", "transport": "sse", "url": "http://169.254.169.254:8080/"}
            )
    assert trusted.status_code == 201, trusted.text
    assert other_port.status_code == 422
    assert metadata.status_code == 422


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"transport": "websocket", "url": "https://mcp.example.com/"}, "transport"),
        ({"transport": "sse", "url": "https://mcp.example.com/", "auth_mode": "basic"}, "auth_mode"),
        ({"transport": "sse", "url": "ftp://mcp.example.com/"}, "url"),
        ({"transport": "sse", "url": "https://mcp.example.com:99999/"}, "url"),
    ],
)
async def test_an_invalid_transport_auth_mode_or_url_is_a_422(body, field, dns):
    async with _scratch() as (scratch, owner), _client(scratch, owner) as client:
        response = await client.post(PREFIX, json={"name": "Remote", **body})
    assert response.status_code == 422, response.text
    assert [error["loc"][-1] for error in response.json()["detail"]] == [field]


async def test_a_stdio_server_has_no_url_to_judge(dns):
    async with _scratch() as (scratch, owner):
        _policy("public_only")
        async with _client(scratch, owner) as client:
            response = await client.post(PREFIX, json={"name": "Local", "transport": "stdio", "command": "npx"})
    assert response.status_code == 201, response.text


# ---------------------------------------------------------------------------
# Save: update and import
# ---------------------------------------------------------------------------


async def test_update_judges_the_server_as_it_will_be_saved(dns):
    async with _scratch() as (scratch, owner):
        server_id = await _stored(scratch, owner, "http://127.0.0.1:8080/mcp")
        _policy("private_networks")
        async with _client(scratch, owner) as client:
            renamed = await client.put(f"{PREFIX}/{server_id}", json={"name": "Renamed"})
            to_metadata = await client.put(f"{PREFIX}/{server_id}", json={"url": "http://169.254.169.254/"})
            to_loopback = await client.put(f"{PREFIX}/{server_id}", json={"url": "http://[::1]:9000/"})
            retransported = await client.put(f"{PREFIX}/{server_id}", json={"transport": "streamable_http"})
            to_private = await client.put(f"{PREFIX}/{server_id}", json={"url": "http://10.0.0.5:8080/mcp"})
            bad_transport = await client.put(f"{PREFIX}/{server_id}", json={"transport": "grpc"})
            bad_auth = await client.put(f"{PREFIX}/{server_id}", json={"auth_mode": "basic"})
    # Not touching the URL leaves a server saved before the policy alone...
    assert renamed.status_code == 200, renamed.text
    # ...and every change that sets where it connects is judged.
    assert (to_metadata.status_code, to_loopback.status_code, retransported.status_code) == (422, 422, 422)
    assert REFUSED_DETAIL in to_loopback.text
    assert to_private.status_code == 200, to_private.text
    assert to_private.json()["url"] == "http://10.0.0.5:8080/mcp"
    assert (bad_transport.status_code, bad_auth.status_code) == (422, 422)


async def test_the_edit_form_still_saves_a_server_kept_from_before_the_policy(dns):
    # The edit form sends transport, url and auth_mode on every save: unchanged,
    # they do not re-judge where the server connects, so it can still be
    # renamed or re-tagged (its connections stay refused by the policy).
    async with _scratch() as (scratch, owner):
        server_id = await _stored(scratch, owner, "http://127.0.0.1:8080/mcp")
        _policy("public_only")
        form = {
            "name": "Renamed",
            "tags": ["kept"],
            "transport": "sse",
            "hide_from_chat": True,
            "url": "http://127.0.0.1:8080/mcp",
            "auth_mode": "headers",
        }
        async with _client(scratch, owner) as client:
            saved = await client.put(f"{PREFIX}/{server_id}", json=form)
            moved = await client.put(f"{PREFIX}/{server_id}", json={**form, "url": "http://127.0.0.1:9090/mcp"})
            retransported = await client.put(f"{PREFIX}/{server_id}", json={**form, "transport": "streamable_http"})
    assert saved.status_code == 200, saved.text
    assert (saved.json()["name"], saved.json()["tags"], saved.json()["hide_from_chat"]) == ("Renamed", ["kept"], True)
    assert (moved.status_code, retransported.status_code) == (422, 422)
    assert REFUSED_DETAIL in moved.text


async def test_a_masked_url_sent_back_as_shown_is_no_new_destination(dns):
    # The API shows a URL's password masked; sent back as shown, it is
    # restored to the stored URL before the policy decides whether the
    # destination changed, so a server kept from before the policy still saves.
    async with _scratch() as (scratch, owner):
        stored = "http://user:pw-s3cret@127.0.0.1:8080/mcp"
        server_id = await _stored(scratch, owner, stored)
        _policy("public_only")
        async with _client(scratch, owner) as client:
            shown = (await client.get(f"{PREFIX}/{server_id}")).json()["url"]
            saved = await client.put(
                f"{PREFIX}/{server_id}", json={"name": "Renamed", "transport": "sse", "url": shown}
            )
        assert shown != stored and "pw-s3cret" not in shown
        assert saved.status_code == 200, saved.text
        async with scratch.sessions() as db:
            srv = await db.get(MCPServer, server_id)
        assert (srv.name, srv.url) == ("Renamed", stored)


async def test_a_refused_update_writes_nothing(dns):
    async with _scratch() as (scratch, owner):
        server_id = await _stored(scratch, owner, "https://mcp.example.com/mcp")
        _policy("permissive")
        async with _client(scratch, owner) as client:
            refused = await client.put(
                f"{PREFIX}/{server_id}", json={"name": "Changed", "url": "http://[::ffff:169.254.169.254]/"}
            )
            current = await client.get(f"{PREFIX}/{server_id}")
    assert refused.status_code == 422
    assert (current.json()["name"], current.json()["url"]) == ("Stored", "https://mcp.example.com/mcp")


@pytest.mark.parametrize(
    ("server", "status"),
    [
        ({"name": "S", "transport": "sse", "url": "http://169.254.169.254/"}, 422),
        ({"name": "S", "transport": "sse", "url": "http://127.0.0.1:8080/"}, 422),
        ({"name": "S", "transport": "websocket", "url": "https://mcp.example.com/"}, 422),
        ({"name": "S", "transport": "sse", "url": "https://mcp.example.com/", "auth_mode": "basic"}, 422),
        ({"name": "S", "transport": "sse", "url": 42}, 422),
        ({"name": "S", "transport": "sse", "url": "http://10.0.0.5/"}, 201),
    ],
)
async def test_bundle_import_holds_the_url_to_the_policy(server, status, dns):
    async with _scratch() as (scratch, owner):
        _policy("private_networks")
        async with _client(scratch, owner) as client:
            response = await client.post(
                f"{PREFIX}/import", json={"version": 1, "export_type": "mcp_server", "mcp_server": server}
            )
        assert response.status_code == status, response.text
        assert await _count(scratch) == (1 if status == 201 else 0)


# ---------------------------------------------------------------------------
# Connect: test and test-tool on a server saved before the policy
#
# Mostly over ``sse``; a failed ``streamable_http`` connection is answered as an
# error the same way, and both routes are checked for it.
# ---------------------------------------------------------------------------

_TEST_FAILED = {
    "status": "error",
    "tools": None,
    "tool_count": None,
    "message": "Connection test failed. Check the server configuration.",
}
_TOOL_FAILED = {
    "status": "error",
    "message": "Tool execution failed. Check the MCP server connection and tool configuration.",
}


async def test_the_connectivity_test_never_connects_to_a_refused_address(dns, caplog):
    async with _listener() as internal, _scratch() as (scratch, owner):
        dns["internal.example.test"] = ["127.0.0.1"]
        server_id = await _stored(scratch, owner, f"http://internal.example.test:{internal.port}/sse")
        _policy("private_networks")
        async with _client(scratch, owner) as client:
            with caplog.at_level("WARNING", logger="app.core.pinned_transport"):
                response = await client.post(f"{PREFIX}/{server_id}/test")
    assert response.status_code == 200, response.text
    assert response.json() == _TEST_FAILED
    assert internal.accepted == []
    # The SDK's connection and the authorization probe after it, both refused.
    assert caplog.text.count("Refused to connect to 'internal.example.test': it resolves to 127.0.0.1") == 2


async def test_the_tool_test_never_connects_to_a_refused_address(dns):
    async with _listener() as internal, _scratch() as (scratch, owner):
        server_id = await _stored(scratch, owner, f"http://[::ffff:127.0.0.1]:{internal.port}/sse")
        _policy("public_only")
        async with _client(scratch, owner) as client:
            response = await client.post(f"{PREFIX}/{server_id}/test-tool", json={"tool_name": "whoami", "arguments": {}})
    assert response.status_code == 200, response.text
    assert response.json() == _TOOL_FAILED
    assert internal.accepted == []


async def test_a_metadata_address_is_never_connected_to_even_when_permissive(dns, caplog):
    async with _listener() as metadata, _scratch() as (scratch, owner):
        # A name the policy never saw, resolving to a metadata address only
        # when the connection opens (a rebinding answer after the save).
        dns["rebind.example.test"] = ["169.254.169.254"]
        server_id = await _stored(scratch, owner, f"http://rebind.example.test:{metadata.port}/sse")
        async with _client(scratch, owner) as client:
            with caplog.at_level("WARNING", logger="app.core.pinned_transport"):
                response = await client.post(f"{PREFIX}/{server_id}/test")
    assert response.json() == _TEST_FAILED
    assert "a cloud metadata endpoint" in caplog.text
    assert metadata.accepted == []


async def test_a_streamable_http_server_at_a_refused_address_is_never_connected_to(dns, caplog):
    async with _listener() as internal, _scratch() as (scratch, owner):
        server_id = await _stored(scratch, owner, f"http://127.0.0.1:{internal.port}/mcp", "streamable_http")
        _policy("private_networks")
        async with _client(scratch, owner) as client:
            with caplog.at_level("WARNING", logger="app.core.pinned_transport"):
                tested = await client.post(f"{PREFIX}/{server_id}/test")
                tool = await client.post(
                    f"{PREFIX}/{server_id}/test-tool", json={"tool_name": "whoami", "arguments": {}}
                )
    assert (tested.status_code, tested.json()) == (200, _TEST_FAILED)
    assert (tool.status_code, tool.json()) == (200, _TOOL_FAILED)
    assert internal.accepted == []
    assert "Refused to connect to '127.0.0.1': it is 127.0.0.1, a loopback address" in caplog.text


async def test_a_streamable_http_server_at_a_closed_port_answers_an_error(dns):
    # The failure reaches the SDK's caller as its task group's cancellation;
    # both routes answer it as an error, and the connectivity test's own
    # database commit after the attempt is not cancelled.
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    async with _scratch() as (scratch, owner):
        server_id = await _stored(scratch, owner, f"http://127.0.0.1:{port}/mcp", "streamable_http")
        async with _client(scratch, owner) as client:
            tested = await client.post(f"{PREFIX}/{server_id}/test")
            tool = await client.post(f"{PREFIX}/{server_id}/test-tool", json={"tool_name": "whoami", "arguments": {}})
    assert (tested.status_code, tested.json()) == (200, _TEST_FAILED)
    assert (tool.status_code, tool.json()) == (200, _TOOL_FAILED)


@pytest.mark.parametrize(("transport", "path"), [("sse", "/sse"), ("streamable_http", "/mcp")])
async def test_a_redirect_to_an_internal_address_is_never_followed_and_named(dns, caplog, transport, path):
    # The server's own origin is trusted and answers a 307 to a name that
    # resolves to loopback. The MCP transport follows no redirect to another
    # origin (mcp 1.30), so the hop is never connected to; the test names
    # where the server points, without its query.
    async with _listener() as internal:
        dns["internal.example.test"] = ["127.0.0.1"]
        dns["public.example.test"] = ["127.0.0.1"]
        hop = f"http://internal.example.test:{internal.port}{path}"
        async with _listener(redirect_to=f"{hop}?token=hop-secret") as public, _scratch() as (scratch, owner):
            origin = f"http://public.example.test:{public.port}"
            server_id = await _stored(scratch, owner, f"{origin}{path}", transport)
            _policy("private_networks", origin)
            async with _client(scratch, owner) as client:
                with caplog.at_level("WARNING", logger="app.tools.mcp_client"):
                    response = await client.post(f"{PREFIX}/{server_id}/test")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "error",
        "tools": None,
        "tool_count": None,
        "message": (
            f"Connection test failed: the server redirects to {hop}, which the connection "
            "does not follow. If that is the intended server, save it with that URL."
        ),
    }
    # The trusted origin was reached once (the SDK); no authorization probe
    # after a redirect, and never the hop.
    assert public.accepted == ["127.0.0.1"]
    assert internal.accepted == []
    records = [r for r in caplog.records if r.name == "app.tools.mcp_client"]
    assert [(r.levelname, r.exc_info) for r in records] == [("WARNING", None)]
    assert "hop-secret" not in caplog.text


async def test_permissive_still_connects_to_loopback(dns):
    # Backward compatibility: the default policy reaches a local server as
    # before (it answers 500, so the test fails - after connecting).
    async with _listener() as local, _scratch() as (scratch, owner):
        server_id = await _stored(scratch, owner, f"http://127.0.0.1:{local.port}/sse")
        async with _client(scratch, owner) as client:
            response = await client.post(f"{PREFIX}/{server_id}/test")
    assert response.json() == _TEST_FAILED
    assert local.accepted[0] == "127.0.0.1"
