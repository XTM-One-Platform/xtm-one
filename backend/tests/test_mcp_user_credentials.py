"""Each person's own OAuth account on a shared MCP server (issue #443).

On a ``per_user`` OAuth MCP server every call carries the acting person's own
token, or none: the token stored on the server row is never lent. A ``shared``
server keeps today's behaviour, one token for everyone. These tests pin the
resolution (``build_mcp_runtime``), the token holder naming the refresh code and
the auth hook share, the per-person failure tracking, and the remediation an
agent reads on a credential failure.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.api.v1.helpers import build_mcp_runtime
from app.core.security import encrypt_value
from app.models.mcp_server import MCPServer
from app.models.mcp_server_user_credential import MCPServerUserCredential
from app.services import mcp_user_credentials
from app.services.mcp_user_credentials import is_per_user, load_mcp_holder, mcp_holder_key, server_of
from app.services.token_refresh import _mcp_refresh_still_due, _sendable_token
from app.tools.mcp_client import _auth_escalation_fix_hint, _build_mcp_error_message


def _server(mode: str = "shared", **values) -> MCPServer:
    defaults = dict(
        id=uuid.uuid4(),
        name="Remote",
        transport="streamable_http",
        url="https://mcp.example.com/mcp",
        auth_mode="oauth",
        credential_mode=mode,
        is_company_managed=True,
        access_token_enc=encrypt_value("row-token"),
        refresh_token_enc=encrypt_value("row-refresh"),
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        client_id_enc=encrypt_value("client"),
        token_url="https://auth.example.com/token",
        oauth_status="connected",
    )
    defaults.update(values)
    return MCPServer(**defaults)


def _credential(server: MCPServer, token: str = "alice-token") -> MCPServerUserCredential:  # noqa: S107
    credential = MCPServerUserCredential(
        id=uuid.uuid4(),
        server_id=server.id,
        user_id=uuid.uuid4(),
        access_token_enc=encrypt_value(token),
        refresh_token_enc=encrypt_value("alice-refresh"),
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        oauth_status="connected",
    )
    credential.server = server
    return credential


async def test_a_shared_server_sends_its_own_token_as_before():
    server = _server("shared")
    runtime = await build_mcp_runtime(server, _credential(server))

    assert runtime.auth is not None and runtime.auth.token == "row-token"
    assert runtime.per_user is False
    assert runtime.failure_key == str(server.id)


async def test_a_per_user_server_sends_the_acting_persons_own_token():
    server = _server("per_user")
    credential = _credential(server)
    runtime = await build_mcp_runtime(server, credential)

    assert runtime.auth is not None and runtime.auth.token == "alice-token"
    assert runtime.auth._server_id == f"user:{credential.id}"
    assert runtime.per_user is True
    # One person's failures never open the circuit for everyone.
    assert runtime.failure_key == f"{server.id}:user:{credential.user_id}"
    assert runtime.server_key == str(server.id)


async def test_a_per_user_server_never_lends_the_row_token():
    server = _server("per_user", headers_enc=encrypt_value('{"Authorization": "Bearer shared", "X-Tenant": "t1"}'))
    runtime = await build_mcp_runtime(server, None)

    assert runtime.auth is None
    assert runtime.headers == {"X-Tenant": "t1"}
    assert runtime.failure_key == f"{server.id}:user:anonymous"


async def test_a_credential_on_another_server_is_ignored():
    server = _server("per_user")
    other = _credential(_server("per_user"))
    runtime = await build_mcp_runtime(server, other)

    assert runtime.auth is None


def test_the_row_token_of_a_per_user_server_is_never_sendable_nor_refreshed():
    server = _server("per_user")

    assert _sendable_token(server, None) is None
    assert _mcp_refresh_still_due(server) is False
    assert _sendable_token(_server("shared"), None).value == "row-token"


def test_a_person_credential_reads_the_oauth_client_from_its_server():
    server = _server("per_user")
    credential = _credential(server)

    assert server_of(credential) is server
    assert credential.client_id_enc == server.client_id_enc
    assert credential.token_url == server.token_url
    assert credential.auth_mode == "oauth"
    assert _sendable_token(credential, None).value == "alice-token"
    assert mcp_holder_key(credential) == f"user:{credential.id}"
    assert mcp_holder_key(server) == str(server.id)


def test_per_user_only_applies_to_oauth():
    assert is_per_user(_server("per_user")) is True
    assert is_per_user(_server("per_user", auth_mode="headers")) is False
    assert is_per_user(_server("shared")) is False


class _Db:
    def __init__(self, *rows):
        self.rows = {row.id: row for row in rows}

    async def get(self, _model, ident, **_kwargs):
        return self.rows.get(ident)


async def test_a_person_credential_stops_resolving_once_the_server_is_shared_again():
    server = _server("per_user")
    credential = _credential(server)
    db = _Db(server, credential)

    assert await load_mcp_holder(db, mcp_holder_key(credential)) is credential
    assert await load_mcp_holder(db, mcp_holder_key(server)) is server
    server.credential_mode = "shared"
    assert await load_mcp_holder(db, mcp_holder_key(credential)) is None
    assert await load_mcp_holder(db, "user:not-a-uuid") is None


@pytest.mark.parametrize("company_managed", [True, False, None])
def test_a_credential_failure_on_a_per_user_server_asks_the_person_to_connect(company_managed):
    message = _build_mcp_error_message(
        "list_issues", RuntimeError("401 Unauthorized"), None, company_managed, True
    )
    assert "connect (or reconnect) their account" in message
    assert "administrator" not in _auth_escalation_fix_hint(company_managed, True)


async def test_the_resolver_returns_no_credential_on_a_shared_server(monkeypatch):
    async def _never(*_args, **_kwargs):
        raise AssertionError("a shared server reads no person's credential")

    monkeypatch.setattr(mcp_user_credentials, "user_credential", _never)
    assert await mcp_user_credentials.credential_for_runtime(object(), _server("shared"), uuid.uuid4()) is None
