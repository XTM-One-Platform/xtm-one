"""The connectivity probe of an imported ``filigran-integration-v1`` bundle.

"Test connection" on a ``custom:`` integration calls the bundle-declared
``test_connection`` endpoint. Only the status code matters, so the probe is
built like the bundle's tool calls: environment ignored (``trust_env=False``),
no redirect followed, the body never read, one overall deadline. These tests
drive the real handler (``_test_integration``) against an ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest

from app.api.v1 import integrations as integrations_module
from app.core.egress import egress_client as real_egress_client
from app.core.security import encrypt_value
from app.integrations.http import PLATFORM_USER_AGENT
from app.models.custom_integration_definition import CustomIntegrationDefinition
from app.models.integration import Integration

PROVIDER = "custom:acme-probe"
BASE_URL = "https://acme.example"
TOKEN = "tok-123"


class _CountingStream(httpx.AsyncByteStream):
    """A response body that records every chunk pulled from it."""

    def __init__(self, chunks: int = 4) -> None:
        self._chunks = chunks
        self.pulled = 0
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for _ in range(self._chunks):
            self.pulled += 1
            yield b"x" * 65536

    async def aclose(self) -> None:
        self.closed = True


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _Session:
    def __init__(self, definition: CustomIntegrationDefinition | None) -> None:
        self._definition = definition
        self.commits = 0

    async def execute(self, _stmt):
        return _Result(self._definition)

    async def commit(self) -> None:
        self.commits += 1


def _definition(test_connection: dict) -> CustomIntegrationDefinition:
    return CustomIntegrationDefinition(
        provider_key=PROVIDER,
        metadata_json={"test_connection": test_connection},
    )


def _integration(base_url: str = BASE_URL, token: str = TOKEN) -> Integration:
    return Integration(
        id=uuid.uuid4(),
        provider=PROVIDER,
        name="Acme",
        auth_type="api_key",
        status="connected",
        api_key_enc=encrypt_value(token),
        extra_config_enc=encrypt_value(json.dumps({"base_url": base_url})),
    )


@pytest.fixture
def mock_client(monkeypatch):
    """Route the probe's ``egress_client`` to a mock transport, recording the call."""
    state = SimpleNamespace(handler=None, requests=[], client_kwargs=[], clients=[])

    async def _dispatch(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        response = state.handler(request)
        if asyncio.iscoroutine(response):
            response = await response
        return response

    def _spy(*args, **kwargs):
        state.client_kwargs.append(dict(kwargs))
        # A mount, not ``transport=``: an explicit transport makes httpx skip
        # the environment's proxies whatever ``trust_env`` says, while a
        # catch-all mount still loses to an env ``https://`` proxy mount.
        client = real_egress_client(*args, mounts={"all://": httpx.MockTransport(_dispatch)}, **kwargs)
        state.clients.append(client)
        return client

    monkeypatch.setattr(integrations_module, "egress_client", _spy)
    return state


async def _run(test_connection: dict, *, base_url: str = BASE_URL, token: str = TOKEN) -> dict:
    session = _Session(_definition(test_connection))
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True)
    return await integrations_module._test_integration(_integration(base_url, token), user, [], session)


_HTTP_PROBE = {"mode": "http", "path": "/health", "method": "GET"}


async def test_client_ignores_the_environment_and_never_follows_redirects(mock_client):
    mock_client.handler = lambda _r: httpx.Response(200, stream=_CountingStream())

    result = await _run(_HTTP_PROBE)

    assert result == {"status": "ok", "message": "Acme is connected and responding"}
    assert len(mock_client.client_kwargs) == 1
    kwargs = mock_client.client_kwargs[0]
    assert kwargs["trust_env"] is False
    assert kwargs["follow_redirects"] is False
    assert kwargs["timeout"] == integrations_module._CUSTOM_PROBE_TIMEOUT_S
    client = mock_client.clients[0]
    assert client.trust_env is False
    assert client.follow_redirects is False


async def test_an_https_proxy_in_the_environment_is_not_used(mock_client, monkeypatch):
    # With trust_env the client would mount a proxy transport for https:// and
    # the request would go to this dead proxy instead of the mock transport.
    for var in ("NO_PROXY", "no_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
    mock_client.handler = lambda _r: httpx.Response(200, stream=_CountingStream())

    result = await _run(_HTTP_PROBE)

    assert result["status"] == "ok"
    assert len(mock_client.requests) == 1


async def test_the_answer_body_is_never_read(mock_client):
    body = _CountingStream(chunks=8)
    mock_client.handler = lambda _r: httpx.Response(200, stream=body)

    result = await _run(_HTTP_PROBE)

    assert result["status"] == "ok"
    assert body.pulled == 0
    assert body.closed is True


async def test_an_error_answer_body_is_never_read_either(mock_client):
    body = _CountingStream(chunks=8)
    mock_client.handler = lambda _r: httpx.Response(500, stream=body)

    result = await _run(_HTTP_PROBE)

    assert result == {"status": "error", "message": "Acme returned HTTP 500"}
    assert body.pulled == 0
    assert body.closed is True


async def test_request_carries_identity_encoding_platform_agent_and_auth(mock_client):
    mock_client.handler = lambda _r: httpx.Response(204, stream=httpx.ByteStream(b""))

    result = await _run({"mode": "http", "path": "status", "method": "head"})

    assert result["status"] == "ok"
    assert len(mock_client.requests) == 1
    request = mock_client.requests[0]
    assert request.method == "HEAD"
    assert str(request.url) == f"{BASE_URL}/status"
    assert request.headers["accept-encoding"] == "identity"
    assert request.headers["user-agent"] == PLATFORM_USER_AGENT
    assert request.headers["authorization"] == "Bearer tok-123"


async def test_bundle_declared_header_auth_is_kept(mock_client):
    mock_client.handler = lambda _r: httpx.Response(200, stream=httpx.ByteStream(b"{}"))

    result = await _run(
        {
            "mode": "http",
            "path": "/health",
            "auth": {"type": "header", "header": "X-Api-Key", "prefix": ""},
        }
    )

    assert result["status"] == "ok"
    request = mock_client.requests[0]
    assert request.headers["x-api-key"] == "tok-123"
    assert "authorization" not in request.headers
    assert request.headers["accept-encoding"] == "identity"
    assert request.headers["user-agent"] == PLATFORM_USER_AGENT


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (200, {"status": "ok", "message": "Acme is connected and responding"}),
        (204, {"status": "ok", "message": "Acme is connected and responding"}),
        (
            401,
            {
                "status": "error",
                "message": "Authentication failed — the API key may be invalid or revoked.",
            },
        ),
        (
            403,
            {
                "status": "error",
                "message": "Authentication failed — the API key may be invalid or revoked.",
            },
        ),
        (404, {"status": "error", "message": "Acme returned HTTP 404"}),
        (503, {"status": "error", "message": "Acme returned HTTP 503"}),
    ],
)
async def test_status_codes_keep_their_messages(mock_client, status_code, expected):
    mock_client.handler = lambda _r: httpx.Response(status_code, stream=httpx.ByteStream(b"body"))

    assert await _run(_HTTP_PROBE) == expected


@pytest.mark.parametrize("status_code", [301, 302, 303, 307, 308])
async def test_a_redirect_is_reported_not_followed(mock_client, status_code):
    mock_client.handler = lambda _r: httpx.Response(
        status_code,
        headers={"Location": "https://elsewhere.example/login"},
        stream=httpx.ByteStream(b""),
    )

    result = await _run(_HTTP_PROBE)

    assert result == {
        "status": "error",
        "message": (
            f"Acme answered with a redirect (HTTP {status_code}), which is not "
            "followed — check the base URL in the integration configuration."
        ),
    }
    assert len(mock_client.requests) == 1
    assert str(mock_client.requests[0].url) == f"{BASE_URL}/health"


@pytest.mark.parametrize(
    ("status_code", "headers"),
    [(304, {}), (300, {}), (302, {})],
)
async def test_a_3xx_without_a_redirect_location_keeps_the_status_message(mock_client, status_code, headers):
    mock_client.handler = lambda _r: httpx.Response(status_code, headers=headers, stream=httpx.ByteStream(b""))

    assert await _run(_HTTP_PROBE) == {
        "status": "error",
        "message": f"Acme returned HTTP {status_code}",
    }


@pytest.mark.parametrize("name", ["user-agent", "Accept-Encoding", "ACCEPT-ENCODING"])
async def test_a_bundle_auth_header_named_like_a_default_replaces_it(mock_client, name):
    mock_client.handler = lambda _r: httpx.Response(200, stream=httpx.ByteStream(b""))

    result = await _run({"mode": "http", "path": "/health", "auth": {"type": "header", "header": name, "prefix": ""}})

    assert result["status"] == "ok"
    sent = mock_client.requests[0].headers.get_list(name)
    assert sent == ["tok-123"]


@pytest.mark.parametrize("token", ["tök-123", "tok-€", "トークン"])
@pytest.mark.parametrize(
    "test_connection",
    [
        _HTTP_PROBE,
        {"mode": "http", "path": "/health", "auth": {"type": "header", "header": "X-Api-Key"}},
    ],
    ids=["bearer", "header"],
)
async def test_a_credential_that_is_not_a_valid_header_value_is_a_connection_error(mock_client, token, test_connection):
    # The stored key is free text: one httpx cannot put in a header must end
    # as the probe's generic error, never escape the handler as a 500.
    mock_client.handler = lambda _r: httpx.Response(200, stream=httpx.ByteStream(b""))

    result = await _run(test_connection, token=token)

    assert result == {"status": "error", "message": "Acme connection failed"}
    assert mock_client.requests == []


async def test_network_error_stays_generic_and_the_reason_goes_to_the_debug_log(mock_client, caplog):
    def _boom(request):
        raise httpx.ConnectError("dns says 10.0.0.7 is unreachable", request=request)

    mock_client.handler = _boom

    with caplog.at_level(logging.DEBUG, logger=integrations_module.logger.name):
        result = await _run(_HTTP_PROBE)

    assert result == {"status": "error", "message": "Acme connection failed"}
    records = [r for r in caplog.records if "connectivity probe failed" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.DEBUG
    assert "10.0.0.7" in str(records[0].exc_info[1])


async def test_overall_deadline_bounds_a_hanging_endpoint(mock_client, monkeypatch):
    # httpx's per-phase timeouts never fire on a mock transport, so only the
    # overall asyncio deadline can end this request.
    monkeypatch.setattr(integrations_module, "_CUSTOM_PROBE_TIMEOUT_S", 0.05)

    async def _hang(_request):
        await asyncio.sleep(30)
        return httpx.Response(200)

    mock_client.handler = _hang

    result = await asyncio.wait_for(_run(_HTTP_PROBE), timeout=5)

    assert result == {"status": "error", "message": "Acme connection failed"}


async def test_mode_none_never_opens_a_client(mock_client):
    result = await _run({"mode": "none"})

    assert result == {"status": "ok", "message": "Acme appears configured"}
    assert mock_client.client_kwargs == []
