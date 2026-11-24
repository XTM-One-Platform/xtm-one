"""The base URL of a custom bundle integration is held to a destination policy.

Any user who sees a ``custom:`` provider may configure an integration of it,
so its ``extra_config`` base URL is user-supplied. The policy
(``app.integrations.base_url_policy``) is the operator-network one: private
ranges admitted (on-prem APIs), loopback, link-local, reserved and cloud
metadata addresses refused. These tests cover it on save (the real create,
update and import routes), on the "Test connection" probe and on the bundle's
tool calls, a DNS answer that turns inward after the check included.
"""

from __future__ import annotations

import json
import socket
import uuid
from types import SimpleNamespace

import httpcore
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import require_agentic_ai_licensed
from app.api.v1 import integrations as integrations_api
from app.core.database import get_db
from app.core.security import decrypt_json, encrypt_value
from app.integrations import base_url_policy
from app.integrations import http as integration_http
from app.integrations.base_url_policy import (
    REFUSED_DETAIL,
    BaseURLRefusedError,
    assert_base_url_allowed,
    base_url_keys,
)
from app.models.custom_integration_definition import CustomIntegrationDefinition
from app.models.integration import Integration
from app.services import web_fetch
from app.services.integration_bundle_manager import build_integration_bundle_zip
from app.services.integration_tools import _register_custom_declared_tools
from app.tools.base import CallableToolExecutor, ToolCall
from tests.a2a_chat_gate import stub_chat_gate
from tests.test_integration_bundles_api import _FakeDb

PROVIDER = "custom:acme-api"
TOKEN = "tok-5256"


def _metadata(*, test_connection: dict | None = None) -> dict:
    return {
        "format": "filigran-integration-v1",
        "version": "1.0.0",
        "name": "Acme API",
        "slug": "acme-api",
        "description": "Acme API bundle.",
        "author": "QA",
        "provider": "acme-api",
        "auth_type": "api_key",
        "icon": None,
        "settings": [{"key": "base_url", "label": "Base URL", "type": "string", "required": True}],
        "supports_polling": False,
        "supports_webhook": False,
        "test_connection": test_connection or {"mode": "none"},
        "tools": [
            {
                "name": "list_things",
                "description": "List things.",
                "input_schema": {"type": "object", "properties": {}},
                "execution": {"method": "GET", "path": "/things"},
            },
            {
                "name": "upload_thing",
                "description": "Upload a thing.",
                "input_schema": {"type": "object", "properties": {}},
                "execution": {"method": "GET", "path": "/upload", "base_url_key": "upload_url"},
            },
        ],
        "exported_at": "2026-10-24T00:00:00Z",
    }


# ── DNS and socket doubles ───────────────────────────────────────────────────


def _infos(ip: str, port: int = 0) -> list:
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    return [(family, socket.SOCK_STREAM, 6, "", (ip, port))]


@pytest.fixture
def save_dns(monkeypatch):
    """The save-time resolver: ``host -> address``; a missing host does not resolve."""
    table: dict[str, str] = {}

    async def _resolve(host: str, port: int) -> list:
        if host not in table:
            raise socket.gaierror("no such host")
        return _infos(table[host], port)

    monkeypatch.setattr(base_url_policy, "_resolve", _resolve)
    return table


class _Resolver:
    """The connection-time resolver: answers from a queue, the last one repeating."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []

    def __call__(self, host, port, family=0, type=0, proto=0, flags=0):
        self.calls.append(host)
        ip = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        return _infos(ip, port or 0)


@pytest.fixture
def connects(monkeypatch):
    """Every TCP connection attempt, as ``(address, port)``; none is opened."""
    attempts: list[tuple[str, int]] = []

    async def fake_connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        attempts.append((host, port))
        raise httpcore.ConnectError("no sockets in tests")

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", fake_connect)
    monkeypatch.setattr(web_fetch, "_environment_proxies", dict)
    return attempts


# ── The policy ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080",
        "http://[::1]/api",
        "http://0x7f000001",  # 127.0.0.1 spelled as an inet_aton shorthand
        "http://169.254.169.254/latest",
        "http://[::ffff:169.254.169.254]",
        "http://[64:ff9b::a9fe:a9fe]",  # NAT64 of the metadata address
        "http://100.100.100.200",
        "http://169.254.10.20",
        "http://0.0.0.0",
        "http://224.0.0.1",
    ],
)
async def test_a_spelled_refused_address_is_refused_on_save(save_dns, url):
    with pytest.raises(BaseURLRefusedError) as refused:
        await assert_base_url_allowed(url, routed=False)
    assert str(refused.value) == REFUSED_DETAIL


@pytest.mark.parametrize(
    ("ip", "allowed"),
    [
        ("93.184.216.34", True),
        ("10.0.0.5", True),
        ("192.168.1.20", True),
        ("172.16.4.4", True),
        ("100.64.3.4", True),
        ("127.0.0.1", False),
        ("::1", False),
        ("169.254.169.254", False),
        ("fd00:ec2::254", False),
        ("169.254.3.3", False),
    ],
)
async def test_every_address_a_name_resolves_to_is_judged_on_save(save_dns, ip, allowed):
    save_dns["api.acme.test"] = ip
    if allowed:
        assert await assert_base_url_allowed("https://api.acme.test/v1", routed=False) is None
    else:
        with pytest.raises(BaseURLRefusedError) as refused:
            await assert_base_url_allowed("https://api.acme.test/v1", routed=False)
        # Generic: the refusal never says where the name resolved.
        assert str(refused.value) == REFUSED_DETAIL
        assert ip not in str(refused.value)


async def test_a_name_that_does_not_resolve_is_left_to_the_connection(save_dns):
    assert await assert_base_url_allowed("https://not-up-yet.acme.test", routed=False) is None


async def test_a_slow_resolver_is_left_to_the_connection(monkeypatch):
    async def _hang(host, port):
        raise TimeoutError

    monkeypatch.setattr(base_url_policy, "_resolve", _hang)
    assert await assert_base_url_allowed("https://slow.acme.test", routed=False) is None


@pytest.mark.parametrize("url", ["ftp://files.acme.test", "file:///etc/passwd", "https://", "not a url", "http://h:99999"])
async def test_a_url_that_is_not_http_with_a_host_is_refused(save_dns, url):
    with pytest.raises(BaseURLRefusedError) as refused:
        await assert_base_url_allowed(url, routed=False)
    assert str(refused.value) == "The base URL must be an http or https URL with a host."


@pytest.mark.parametrize("value", [None, "", "   ", 42, ["http://127.0.0.1"]])
async def test_an_unset_or_non_string_value_is_left_alone(save_dns, value):
    # The executor reads such a value as a missing base URL.
    assert await assert_base_url_allowed(value, routed=False) is None


async def test_a_routed_integration_is_judged_on_what_the_url_spells_only(save_dns):
    save_dns["api.acme.test"] = "127.0.0.1"
    # The relay group reaches the customer's network and checks its own list.
    assert await assert_base_url_allowed("https://api.acme.test", routed=True) is None
    assert await assert_base_url_allowed("http://10.0.0.5", routed=True) is None
    assert await assert_base_url_allowed("http://127.0.0.1:8080", routed=True) is None
    with pytest.raises(BaseURLRefusedError):
        await assert_base_url_allowed("http://169.254.169.254", routed=True)


def test_base_url_keys_are_the_default_and_every_key_a_tool_names():
    assert base_url_keys(_metadata()) == ["base_url", "upload_url"]
    assert base_url_keys({"available_tools": [{"execution": {"base_url_key": "api"}}]}) == ["api", "base_url"]
    assert base_url_keys(None) == ["base_url"]
    assert base_url_keys({"tools": ["junk", {"execution": "junk"}, {"execution": {"base_url_key": 3}}]}) == [
        "base_url"
    ]


# ── On save: the real routes ─────────────────────────────────────────────────


@pytest.fixture
def routes(monkeypatch, save_dns):
    """The integrations router, as a non-admin user, the bundle already imported."""
    fake_db = _FakeDb()
    fake_db.custom_defs[PROVIDER] = CustomIntegrationDefinition(
        id=uuid.uuid4(),
        provider_key=PROVIDER,
        name="Acme API",
        slug="acme-api",
        version="1.0.0",
        enabled=True,
        metadata_json=_metadata(),
        settings_schema=_metadata()["settings"],
    )
    user = SimpleNamespace(id=uuid.uuid4(), full_name="User", role="user", is_admin=False)

    async def _get_db():
        yield fake_db

    async def _audit(*_a, **_k):
        return None

    async def _platform_settings(_db):
        return SimpleNamespace(disabled_integration_providers=[])

    monkeypatch.setattr(integrations_api, "log_audit", _audit)
    monkeypatch.setattr("app.api.v1.platform_settings_helpers.get_platform_settings", _platform_settings)
    stub_chat_gate(monkeypatch)

    app = FastAPI()
    app.include_router(integrations_api.router, prefix="/api/v1/integrations")
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[require_agentic_ai_licensed] = lambda: None
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, db=fake_db, user=user, dns=save_dns)


def _create(routes, extra_config: dict):
    return routes.client.post(
        "/api/v1/integrations",
        json={
            "provider": PROVIDER,
            "name": "Acme",
            "auth_type": "api_key",
            "api_key": TOKEN,
            "extra_config": extra_config,
        },
    )


@pytest.mark.parametrize(
    "extra_config",
    [
        {"base_url": "http://127.0.0.1:8080"},
        {"base_url": "http://169.254.169.254/latest/meta-data"},
        {"base_url": "https://loopback.acme.test"},
        # A key a tool's spec reads the base URL from.
        {"base_url": "https://api.acme.test", "upload_url": "http://[fe80::1]"},
    ],
)
def test_create_refuses_a_base_url_the_policy_refuses(routes, extra_config):
    routes.dns["loopback.acme.test"] = "127.0.0.1"
    routes.dns["api.acme.test"] = "93.184.216.34"

    resp = _create(routes, extra_config)

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == REFUSED_DETAIL
    assert routes.db.integrations == {}


@pytest.mark.parametrize("base_url", ["https://api.acme.test", "http://10.0.0.5:8443", "https://onprem.acme.test"])
def test_create_keeps_a_public_or_private_base_url(routes, base_url):
    routes.dns["api.acme.test"] = "93.184.216.34"
    routes.dns["onprem.acme.test"] = "192.168.1.20"

    resp = _create(routes, {"base_url": base_url})

    assert resp.status_code == 201, resp.text
    (stored,) = routes.db.integrations.values()
    assert decrypt_json(stored.extra_config_enc) == {"base_url": base_url}


def test_update_refuses_a_base_url_the_policy_refuses_and_keeps_the_stored_one(routes):
    created = _create(routes, {"base_url": "https://api.acme.test"})
    assert created.status_code == 201, created.text
    integration_id = created.json()["id"]

    resp = routes.client.put(
        f"/api/v1/integrations/{integration_id}",
        json={"name": "Renamed", "extra_config": {"base_url": "http://127.0.0.1:2375"}},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == REFUSED_DETAIL
    stored = routes.db.integrations[integration_id]
    assert decrypt_json(stored.extra_config_enc) == {"base_url": "https://api.acme.test"}


def test_update_keeps_a_private_base_url(routes):
    created = _create(routes, {"base_url": "https://api.acme.test"})
    integration_id = created.json()["id"]

    resp = routes.client.put(
        f"/api/v1/integrations/{integration_id}",
        json={"extra_config": {"base_url": "http://10.1.2.3"}},
    )

    assert resp.status_code == 200, resp.text
    stored = routes.db.integrations[integration_id]
    assert decrypt_json(stored.extra_config_enc) == {"base_url": "http://10.1.2.3"}


def test_a_bundle_import_refuses_an_instance_whose_base_url_the_policy_refuses(routes, monkeypatch):
    admin = SimpleNamespace(id=uuid.uuid4(), full_name="Admin", role="admin", is_admin=True)
    routes.client.app.dependency_overrides[deps.get_current_user] = lambda: admin
    routes.db.custom_defs.clear()
    metadata = {
        **_metadata(),
        "integration_instance": {
            "name": "Acme",
            "auth_type": "api_key",
            "api_key": TOKEN,
            "extra_config": {"base_url": "https://api.acme.test", "upload_url": "http://127.0.0.1:9000"},
        },
    }

    resp = routes.client.post(
        "/api/v1/integrations/admin/import",
        files={"bundle": ("acme.zip", build_integration_bundle_zip(metadata), "application/zip")},
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == REFUSED_DETAIL
    assert routes.db.custom_defs == {}
    assert routes.db.integrations == {}


# ── On connection: the "Test connection" probe ──────────────────────────────


class _ProbeSession:
    def __init__(self, definition: CustomIntegrationDefinition) -> None:
        self._definition = definition

    async def execute(self, _stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self._definition)

    async def commit(self) -> None:
        return None


async def _probe(base_url: str) -> dict:
    definition = CustomIntegrationDefinition(
        provider_key=PROVIDER,
        metadata_json=_metadata(test_connection={"mode": "http", "path": "/health", "method": "GET"}),
    )
    integration = Integration(
        id=uuid.uuid4(),
        provider=PROVIDER,
        name="Acme",
        auth_type="api_key",
        status="connected",
        api_key_enc=encrypt_value(TOKEN),
        extra_config_enc=encrypt_value(json.dumps({"base_url": base_url})),
    )
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    return await integrations_api._test_integration(integration, user, [], _ProbeSession(definition))


@pytest.mark.parametrize("ip", ["127.0.0.1", "169.254.169.254", "169.254.7.7", "::1"])
async def test_the_probe_never_connects_to_a_refused_address(monkeypatch, connects, ip):
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", _Resolver(ip))

    result = await _probe("https://api.acme.test")

    assert result == {"status": "error", "message": "Acme connection failed"}
    assert connects == []


async def test_the_probe_connects_to_an_allowed_private_host_at_the_address_checked(monkeypatch, connects):
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", _Resolver("10.0.0.5"))

    result = await _probe("http://onprem.acme.test:8080")

    # The double refuses the socket; the attempt went to the checked address.
    assert result == {"status": "error", "message": "Acme connection failed"}
    assert connects == [("10.0.0.5", 8080)]


async def test_the_probe_never_connects_to_a_spelled_loopback_address(connects):
    result = await _probe("http://127.0.0.1:2375")

    assert result == {"status": "error", "message": "Acme connection failed"}
    assert connects == []


# ── On connection: the bundle's tool calls ───────────────────────────────────


def _tools(extra_config: dict) -> CallableToolExecutor:
    executor = CallableToolExecutor()
    _register_custom_declared_tools(
        executor,
        provider=PROVIDER,
        integration_id=str(uuid.uuid4()),
        metadata_json=_metadata(),
        integration_token=TOKEN,
        integration_extra_config=extra_config,
    )
    return executor


async def _call(executor: CallableToolExecutor, name: str = "list_things"):
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments={}))


@pytest.mark.parametrize("ip", ["127.0.0.1", "169.254.169.254", "169.254.7.7", "fd00:ec2::254", "0.0.0.0"])  # noqa: S104
async def test_a_tool_call_never_connects_to_a_refused_address(monkeypatch, connects, ip):
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", _Resolver(ip))

    result = await _call(_tools({"base_url": "https://api.acme.test"}))

    assert result.is_error is True
    assert result.content.startswith("Error: acme-api request refused:")
    assert ip not in result.content
    assert connects == []


async def test_a_tool_call_reading_another_base_url_key_is_held_to_the_policy(monkeypatch, connects):
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", _Resolver("127.0.0.1"))

    result = await _call(
        _tools({"base_url": "https://api.acme.test", "upload_url": "https://upload.acme.test"}), "upload_thing"
    )

    assert result.content.startswith("Error: acme-api request refused:")
    assert connects == []


async def test_a_rebound_answer_is_refused_at_connect_time(monkeypatch, connects):
    """The URL check sees a public address; the connection's own lookup gets loopback."""
    resolver = _Resolver("93.184.216.34", "127.0.0.1")
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", resolver)

    result = await _call(_tools({"base_url": "https://rebind.acme.test"}))

    assert result.is_error is True
    assert "127.0.0.1" not in result.content
    assert connects == []
    assert resolver.calls == ["rebind.acme.test", "rebind.acme.test"]


async def test_a_tool_call_connects_to_an_allowed_private_host_at_the_address_checked(monkeypatch, connects):
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", _Resolver("192.168.1.20"))
    monkeypatch.setattr(integration_http, "_CONNECT_BASE_DELAY", 0.0)

    result = await _call(_tools({"base_url": "https://onprem.acme.test"}))

    # The double refuses the socket: a connection never established is
    # retried twice, and every attempt went to the checked address.
    assert result.is_error is True
    assert connects == [("192.168.1.20", 443)] * 3


async def test_a_bundle_spec_server_still_gets_the_credential_without_a_base_url(monkeypatch):
    """The spec is the admin's bundle: its server is the integration's own (unchanged)."""
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", _Resolver("93.184.216.34"))
    monkeypatch.setattr(web_fetch, "_environment_proxies", dict)
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "app.services.declarative_http_executor.egress_client",
        lambda **kwargs: real_client(**{**kwargs, "transport": httpx.MockTransport(handler)}),
    )
    metadata = _metadata()
    metadata["tools"][0]["execution"]["server_url"] = "https://api.acme.test/v2"
    executor = CallableToolExecutor()
    _register_custom_declared_tools(
        executor,
        provider=PROVIDER,
        integration_id=str(uuid.uuid4()),
        metadata_json=metadata,
        integration_token=TOKEN,
        integration_extra_config={},
    )

    result = await _call(executor)

    assert result.is_error is False
    assert [str(r.url) for r in sent] == ["https://api.acme.test/v2/things"]
    assert sent[0].headers["authorization"] == f"Bearer {TOKEN}"


async def test_an_untrusted_answer_is_fetched_directly_even_behind_an_environment_proxy(monkeypatch):
    """``untrusted_response`` reads no proxy from the environment; the guard must not add one."""
    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", _Resolver("93.184.216.34"))
    monkeypatch.setattr(web_fetch, "_environment_proxies", lambda: {"all://": "http://proxy.corp.test:3128"})
    seen: list[dict] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=httpx.ByteStream(json.dumps({"ok": True}).encode()),
        )

    real_client = httpx.AsyncClient

    def factory(**kwargs):
        seen.append(kwargs)
        return real_client(**{**kwargs, "transport": httpx.MockTransport(handler)})

    monkeypatch.setattr("app.services.declarative_http_executor.egress_client", factory)
    metadata = _metadata()
    metadata["tools"][0]["execution"]["untrusted_response"] = True
    executor = CallableToolExecutor()
    _register_custom_declared_tools(
        executor,
        provider=PROVIDER,
        integration_id=str(uuid.uuid4()),
        metadata_json=metadata,
        integration_token=TOKEN,
        integration_extra_config={"base_url": "https://api.acme.test"},
    )

    result = await _call(executor)

    assert result.is_error is False
    assert len(seen) == 1
    assert type(seen[0]["transport"]) is web_fetch.GuardedTransport
    assert "mounts" not in seen[0]
    assert "event_hooks" not in seen[0]
    assert seen[0]["trust_env"] is False
