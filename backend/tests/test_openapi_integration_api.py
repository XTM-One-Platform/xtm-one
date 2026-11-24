"""OpenAPI integrations end to end: preview, create, tools, runtime, refresh, replace."""

from __future__ import annotations

import json
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import require_agentic_ai_licensed
from app.api.v1 import integration_openapi as openapi_api
from app.api.v1 import integrations as integrations_api
from app.core.database import get_db
from app.core.security import decrypt_json, decrypt_value
from app.integrations.oauth_token_grants import clear_token_cache
from app.models.integration import Integration
from app.models.integration_openapi_spec import IntegrationOpenAPISpec
from app.services import web_fetch
from app.services.integration_tools import BindingBasis, ToolBindings, build_integration_tools
from app.services.web_fetch import FetchedBytes
from app.tools.base import ToolCall
from tests.a2a_chat_gate import stub_chat_gate

FIXTURES = Path(__file__).parent / "openapi_fixtures"
PETSTORE_V3 = (FIXTURES / "petstore_v3.yaml").read_text()
PETSTORE_V2 = (FIXTURES / "petstore_v2.json").read_text()
INVENTORY_V31 = (FIXTURES / "inventory_v31.yaml").read_text()

_REAL_ASYNC_CLIENT = httpx.AsyncClient


@dataclass
class _Result:
    rows: list | None = None

    def scalar_one_or_none(self):
        rows = self.rows or []
        return rows[0] if rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self.rows or [])


class _FakeDb:
    def __init__(self) -> None:
        self.integrations: dict[str, Integration] = {}
        self.specs: dict[str, IntegrationOpenAPISpec] = {}
        self.commits = 0
        self.locked: list[str] = []

    def add(self, obj: Any) -> None:
        now = datetime.now(UTC)
        if isinstance(obj, Integration):
            if obj.id is None:
                obj.id = uuid.uuid4()
            if obj.is_builtin is None:
                obj.is_builtin = False
            obj.created_at = obj.created_at or now
            obj.updated_at = obj.updated_at or now
            obj.call_count = obj.call_count or 0
            self.integrations[str(obj.id)] = obj
        elif isinstance(obj, IntegrationOpenAPISpec):
            if obj.id is None:
                obj.id = uuid.uuid4()
            obj.created_at = obj.created_at or now
            self.specs[str(obj.integration_id)] = obj

    async def delete(self, obj: Any) -> None:
        if isinstance(obj, Integration):
            self.integrations.pop(str(obj.id), None)
            self.specs.pop(str(obj.id), None)

    async def flush(self) -> None:
        return None

    async def refresh(self, obj: Any, *_a: Any, with_for_update: bool = False, **_k: Any) -> None:
        if with_for_update:
            self.locked.append(str(obj.id))

    async def commit(self) -> None:
        self.commits += 1

    async def execute(self, stmt: Any) -> _Result:
        entity = stmt.column_descriptions[0].get("entity")
        params = stmt.compile().params
        if entity is IntegrationOpenAPISpec:
            wanted = next((v for k, v in params.items() if k.startswith("integration_id")), None)
            spec = self.specs.get(str(wanted))
            return _Result(rows=[spec] if spec else [])
        if entity is Integration:
            ids: list = []
            for key, value in params.items():
                if key.split("_")[0] == "id":
                    ids.extend(value if isinstance(value, list | tuple | set) else [value])
            if ids:
                return _Result(rows=[self.integrations[str(i)] for i in ids if str(i) in self.integrations])
            return _Result(rows=list(self.integrations.values()))
        return _Result(rows=[])


@pytest.fixture
def api(monkeypatch):
    clear_token_cache()
    fake_db = _FakeDb()
    user = SimpleNamespace(id=uuid.uuid4(), full_name="Owner", role="admin", is_admin=True)
    disabled: list[str] = []

    async def _get_db():
        yield fake_db

    async def _audit(*_a, **_k):
        return None

    async def _platform_settings(_db):
        return SimpleNamespace(disabled_integration_providers=list(disabled))

    monkeypatch.setattr(integrations_api, "log_audit", _audit)
    monkeypatch.setattr(openapi_api, "log_audit", _audit)
    monkeypatch.setattr("app.api.v1.platform_settings_helpers.get_platform_settings", _platform_settings)
    stub_chat_gate(monkeypatch)

    # Every test host resolves to a public address; loopback stays loopback.
    def fake_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        ip = "127.0.0.1" if host == "localhost" else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(web_fetch.socket, "getaddrinfo", fake_getaddrinfo)

    # Discovery documents are fetched only by the tests that serve them.
    async def _no_discovery(_document, _url_base):
        return {}

    monkeypatch.setattr("app.services.openapi_import.service.fetch_discovery", _no_discovery)

    app = FastAPI()
    app.include_router(integrations_api.router, prefix="/api/v1/integrations")
    app.include_router(openapi_api.router, prefix="/api/v1/integrations")
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[require_agentic_ai_licensed] = lambda: None

    with TestClient(app) as client:
        yield SimpleNamespace(client=client, db=fake_db, user=user, disabled=disabled)
    clear_token_cache()


def _mock_api(monkeypatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(recording)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return seen


def _serve_spec(monkeypatch, text: str, final_url: str = "https://petstore3.swagger.io/api/v3/openapi.yaml"):
    fetches: list[str] = []

    async def fake_fetch(url, **kwargs):
        fetches.append(url)
        return FetchedBytes(url=final_url, content=text.encode(), content_type="application/yaml", status_code=200)

    monkeypatch.setattr("app.services.web_fetch.fetch_url_bytes", fake_fetch)
    return fetches


def _create(api, **overrides: Any) -> dict:
    body: dict[str, Any] = {
        "provider": "openapi",
        "name": "Petstore",
        "api_key": "pet-key",
        "extra_config": {"base_url": "https://petstore3.swagger.io/api/v3"},
        "openapi": {"content": PETSTORE_V3, "filename": "openapi.yaml", "auth_option": "api_key"},
    }
    body.update(overrides)
    resp = api.client.post("/api/v1/integrations", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── Preview ──────────────────────────────────────────────────────────────────


def test_preview_from_file_content(api):
    resp = api.client.post("/api/v1/integrations/openapi/preview", json={"content": PETSTORE_V3})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert (data["spec_version"], data["document_format"]) == ("3.0.4", "yaml")
    assert data["title"] == "Swagger Petstore - OpenAPI 3.0"
    assert data["tool_prefix"] == "petstore"
    assert data["default_auth_option"] == "api_key"
    assert data["supported_count"] == data["operation_count"] == 8
    keys = {op["key"]: op for op in data["operations"]}
    assert keys["GET /pet/{petId}"]["tool_name"] == "petstore_get_pet_by_id"
    assert keys["GET /pet/{petId}"]["selected"] is True
    assert {o["id"] for o in data["auth_options"]} >= {"none", "api_key", "manual_bearer"}


def test_preview_from_url_uses_the_final_url_for_relative_servers(api, monkeypatch):
    fetches = _serve_spec(monkeypatch, PETSTORE_V3)
    resp = api.client.post(
        "/api/v1/integrations/openapi/preview", json={"url": "https://petstore3.swagger.io/api/v3/openapi.yaml"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["default_server_url"] == "https://petstore3.swagger.io/api/v3"
    assert fetches == ["https://petstore3.swagger.io/api/v3/openapi.yaml"]


def test_preview_errors_are_400(api):
    resp = api.client.post("/api/v1/integrations/openapi/preview", json={"content": "not: [a spec"})
    assert resp.status_code == 400
    assert "YAML" in resp.json()["detail"]
    resp = api.client.post("/api/v1/integrations/openapi/preview", json={})
    assert resp.status_code == 400


def test_every_openapi_route_is_closed_when_an_admin_disables_the_provider(api):
    created = _create(api)
    api.disabled.append("openapi")
    base = f"/api/v1/integrations/{created['id']}/openapi"
    for method, url, body in [
        ("post", "/api/v1/integrations/openapi/preview", {"content": PETSTORE_V3}),
        ("get", base, None),
        ("put", f"{base}/operations", {"selected_operations": []}),
        ("post", f"{base}/refresh", {}),
        ("put", base, {"content": PETSTORE_V3}),
        ("delete", f"{base}/client-certificate", None),
        ("post", f"{base}/device/poll", {"flow_id": "x"}),
        ("post", "/api/v1/integrations/openapi/oauth/implicit", {"state": "s", "access_token": "t"}),
    ]:
        kwargs = {"json": body} if body is not None else {}
        resp = api.client.request(method.upper(), url, **kwargs)
        assert resp.status_code == 403, (method, url, resp.text)
        assert "disabled by an admin" in resp.json()["detail"]


def test_catalog_lists_the_provider_with_its_marker(api):
    resp = api.client.get("/api/v1/integrations/catalog?category=integration")
    assert resp.status_code == 200, resp.text
    entry = next(i for i in resp.json() if i["provider"] == "openapi")
    assert entry["spec_import"] == "openapi"
    assert "client_credentials" in entry["supported_auth_types"]
    assert entry["available_tools"] == []


# ── Create ───────────────────────────────────────────────────────────────────


def test_create_stores_the_spec_and_derives_the_auth_type(api):
    created = _create(
        api,
        auth_type="oauth",  # ignored: the auth option decides
        openapi={
            "content": PETSTORE_V3,
            "filename": "C:\\specs\\openapi.yaml",
            "auth_option": "api_key",
            "selected_operations": ["GET /pet/{petId}", "DELETE /pet/{petId}", "GET /store/inventory"],
        },
    )
    assert created["auth_type"] == "api_key"
    assert created["status"] == "connected"
    spec = api.db.specs[created["id"]]
    assert spec.source_type == "file"
    assert spec.source_filename == "openapi.yaml"
    assert spec.selected_operation_keys == ["GET /pet/{petId}", "DELETE /pet/{petId}", "GET /store/inventory"]
    assert [t["name"] for t in spec.active_tools] == [
        "petstore_get_pet_by_id",
        "petstore_delete_pet",
        "petstore_get_inventory",
    ]
    assert len(spec.tool_definitions) == 8


def test_create_requires_a_spec_and_a_base_url(api):
    resp = api.client.post("/api/v1/integrations", json={"provider": "openapi", "name": "x", "api_key": "k"})
    assert resp.status_code == 400
    assert "needs a spec" in resp.json()["detail"]
    # The v3 petstore's server is relative: a file import cannot resolve it.
    resp = api.client.post(
        "/api/v1/integrations",
        json={"provider": "openapi", "name": "x", "api_key": "k", "openapi": {"content": PETSTORE_V3}},
    )
    assert resp.status_code == 400
    assert "base URL" in resp.json()["detail"]
    assert api.db.integrations == {}


def test_create_uses_the_spec_server_as_default_base_url(api):
    created = _create(api, extra_config=None, openapi={"content": PETSTORE_V2, "auth_option": "api_key"})
    integration = api.db.integrations[created["id"]]
    assert decrypt_json(integration.extra_config_enc)["base_url"] == "https://petstore.swagger.io/v2"


@pytest.mark.parametrize(
    ("openapi", "fragment"),
    [
        ({"content": PETSTORE_V3, "selected_operations": ["GET /nope"]}, "not in the spec"),
        ({"content": INVENTORY_V31, "auth_option": "oidc"}, "not supported"),
        ({"content": PETSTORE_V3, "auth_option": "unknown"}, "Unknown authentication option"),
        ({"content": INVENTORY_V31, "selected_operations": ["TRACE /items/{item-id}"]}, "cannot become tools"),
    ],
)
def test_create_refuses_invalid_choices(api, openapi, fragment):
    resp = api.client.post(
        "/api/v1/integrations",
        json={"provider": "openapi", "name": "x", "extra_config": {"base_url": "https://a.test"}, "openapi": openapi},
    )
    assert resp.status_code == 400
    assert fragment in resp.json()["detail"]
    assert api.db.integrations == {}


def test_create_oauth_authorization_code_prefills_urls_and_scopes(api):
    created = _create(
        api,
        api_key=None,
        client_id="cid",
        client_secret="csecret",
        extra_config=None,
        openapi={"content": INVENTORY_V31, "auth_option": "oauth:authorization_code"},
    )
    integration = api.db.integrations[created["id"]]
    assert created["auth_type"] == "oauth"
    assert created["status"] == "configured"
    assert integration.authorize_url == "https://auth.acme.test/authorize"
    assert integration.token_url == "https://eu.inventory.acme.test/oauth/token"
    assert integration.scopes == "items:read items:write"


def test_create_client_credentials(api):
    created = _create(
        api,
        api_key=None,
        client_id="cid",
        client_secret="csecret",
        scopes="items:read",
        extra_config=None,
        openapi={"content": INVENTORY_V31, "auth_option": "oauth:client_credentials"},
    )
    integration = api.db.integrations[created["id"]]
    assert created["auth_type"] == "client_credentials"
    assert created["status"] == "connected"
    assert integration.token_url == "https://auth.acme.test/oauth/token"
    assert integration.scopes == "items:read"


# ── Tools ────────────────────────────────────────────────────────────────────


def test_tools_listing_hides_the_credential_parameter(api):
    created = _create(api)
    resp = api.client.get(f"/api/v1/integrations/{created['id']}/tools")
    assert resp.status_code == 200, resp.text
    tools = {t["name"]: t for t in resp.json()["tools"]}
    assert "petstore_delete_pet" in tools
    # DELETE /pet/{petId} declares an `api_key` header — the credential fills it.
    assert set(tools["petstore_delete_pet"]["input_schema"]["properties"]) == {"petId"}
    assert tools["petstore_delete_pet"]["tool_type"] == "write"
    assert tools["petstore_get_pet_by_id"]["tool_type"] == "read"


async def test_runtime_tools_call_the_api_with_the_credential(api, monkeypatch):
    created = _create(api)
    seen = _mock_api(monkeypatch, lambda r: httpx.Response(200, json={"id": 7, "name": "Rex"}))
    defs, executor = await build_integration_tools(api.db, ToolBindings({"openapi": created["id"]}, basis=BindingBasis.CALLER_CHECKED, reason="test"))
    names = {d.name for d in defs}
    assert "petstore_get_pet_by_id" in names
    assert all(d.source == "integration:openapi" and d.entity_id == created["id"] for d in defs)
    result = await executor.execute_tool(ToolCall(id="1", name="petstore_get_pet_by_id", arguments={"petId": 7}))
    assert not result.is_error, result.content
    assert json.loads(result.content) == {"id": 7, "name": "Rex"}
    request = seen[0]
    assert str(request.url) == "https://petstore3.swagger.io/api/v3/pet/7"
    assert request.headers["api_key"] == "pet-key"
    assert request.headers["Accept"] == "application/json"


async def test_public_operations_do_not_carry_the_credential(api, monkeypatch):
    created = _create(api, openapi={"content": PETSTORE_V3, "auth_option": "api_key", "selected_operations": ["GET /user/logout"]})
    seen = _mock_api(monkeypatch, lambda r: httpx.Response(200, json={}))
    _defs, executor = await build_integration_tools(api.db, ToolBindings({"openapi": created["id"]}, basis=BindingBasis.CALLER_CHECKED, reason="test"))
    await executor.execute_tool(ToolCall(id="1", name="petstore_logout_user", arguments={}))
    assert "api_key" not in seen[0].headers


async def test_two_openapi_integrations_on_one_agent_keep_their_own_account(api, monkeypatch):
    """Every imported API shares the ``openapi`` provider key, so an agent bound
    to two of them goes through multi-instance suffixing: each tool must reach
    its own base URL with its own credential, never the last-built instance's."""
    prod = _create(api, name="Petstore prod", api_key="key-prod")
    staging = _create(
        api,
        name="Petstore staging",
        api_key="key-staging",
        extra_config={"base_url": "https://staging.petstore.test/api/v3"},
    )
    seen = _mock_api(monkeypatch, lambda r: httpx.Response(200, json={"id": 7}))
    defs, executor = await build_integration_tools(api.db, ToolBindings({"openapi": [prod["id"], staging["id"]]}, basis=BindingBasis.CALLER_CHECKED, reason="test"))
    by_entity: dict[str, set[str]] = {}
    for d in defs:
        by_entity.setdefault(d.entity_id, set()).add(d.name)
    assert set(by_entity) == {prod["id"], staging["id"]}
    assert not by_entity[prod["id"]] & by_entity[staging["id"]]
    assert "petstore_get_pet_by_id" not in {d.name for d in defs}

    def get_pet_tool(integration_id: str) -> str:
        return next(n for n in by_entity[integration_id] if n.startswith("petstore_get_pet_by_id__"))

    for integration_id in (staging["id"], prod["id"]):
        result = await executor.execute_tool(
            ToolCall(id="1", name=get_pet_tool(integration_id), arguments={"petId": 7})
        )
        assert not result.is_error, result.content
    assert [(r.url.host, r.headers["api_key"]) for r in seen] == [
        ("staging.petstore.test", "key-staging"),
        ("petstore3.swagger.io", "key-prod"),
    ]

    # Tool scoping is keyed per integration for OpenAPI: restricting prod
    # leaves staging's tools alone.
    from app.services.builtin_tools.filtering import filter_integration_tools, instance_scope_key

    scoped = filter_integration_tools(
        defs, {instance_scope_key("openapi", prod["id"]): ["petstore_get_pet_by_id"]}
    )
    assert {d.name for d in scoped if d.entity_id == prod["id"]} == {get_pet_tool(prod["id"])}
    assert {d.name for d in scoped if d.entity_id == staging["id"]} == by_entity[staging["id"]]


async def test_client_credentials_runtime(api, monkeypatch):
    created = _create(
        api,
        api_key=None,
        client_id="cid",
        client_secret="csecret",
        extra_config=None,
        openapi={
            "content": INVENTORY_V31,
            "auth_option": "oauth:client_credentials",
            "selected_operations": ["GET /categories/tree"],
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "m2m-token", "expires_in": 3600})
        return httpx.Response(200, json={"tree": []})

    seen = _mock_api(monkeypatch, handler)
    _defs, executor = await build_integration_tools(api.db, ToolBindings({"openapi": created["id"]}, basis=BindingBasis.CALLER_CHECKED, reason="test"))
    for _ in range(2):
        result = await executor.execute_tool(
            ToolCall(id="1", name="acme_inventory_get_category_tree", arguments={})
        )
        assert not result.is_error, result.content
    api_calls = [r for r in seen if r.url.path != "/oauth/token"]
    assert len(seen) - len(api_calls) == 1  # one token for both calls
    assert all(r.headers["Authorization"] == "Bearer m2m-token" for r in api_calls)
    assert str(api_calls[0].url) == "https://eu.inventory.acme.test/v2/categories/tree"


def test_test_tool_endpoint_runs_the_operation(api, monkeypatch):
    created = _create(api)
    _mock_api(monkeypatch, lambda r: httpx.Response(200, json={"available": 3}))
    resp = api.client.post(
        f"/api/v1/integrations/{created['id']}/test-tool",
        json={"tool_name": "petstore_get_inventory", "arguments": {}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"
    assert json.loads(resp.json()["output"]) == {"available": 3}


def test_connectivity_probe_calls_an_argument_less_get(api, monkeypatch):
    created = _create(api)
    seen = _mock_api(monkeypatch, lambda r: httpx.Response(200, json={}))
    resp = api.client.post(f"/api/v1/integrations/{created['id']}/test")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"
    assert "GET /store/inventory" in resp.json()["message"]
    assert [r.url.path for r in seen] == ["/api/v3/store/inventory"]


def test_the_probe_prefers_operations_that_take_no_argument():
    from app.services.openapi_import import pick_probe_tool

    def tool(name: str, path: str, properties: dict | None = None) -> dict:
        return {
            "name": name,
            "input_schema": {"type": "object", "properties": properties or {}},
            "execution": {"method": "GET", "path": path, "auth": {"type": "none"}},
        }

    search = tool("geocode_address", "/search", {"q": {"type": "string"}})
    capabilities = tool("geoplateforme_get_capabilities", "/getCapabilities")
    assert pick_probe_tool([search, capabilities]) is capabilities
    assert pick_probe_tool([search]) is search


def test_the_probe_tests_the_credential_on_the_base_url():
    """An operation on its own server is called without the credential."""
    from app.services.openapi_import import pick_probe_tool

    def tool(name: str, **execution: object) -> dict:
        return {
            "name": name,
            "input_schema": {"type": "object", "properties": {}},
            "execution": {"method": "GET", "path": "/status", "auth": {"type": "bearer"}, **execution},
        }

    elsewhere = tool("status_elsewhere", server_url="https://status.example.org")
    home = tool("status_home")
    assert pick_probe_tool([elsewhere, home]) is home


def test_connectivity_probe_reports_http_failures(api, monkeypatch):
    created = _create(api)
    _mock_api(monkeypatch, lambda r: httpx.Response(401, json={"message": "bad key"}))
    resp = api.client.post(f"/api/v1/integrations/{created['id']}/test")
    assert resp.json()["status"] == "error"
    assert "authentication failed" in resp.json()["message"]


def test_connectivity_probe_without_a_safe_operation(api):
    created = _create(api, openapi={"content": PETSTORE_V3, "auth_option": "api_key", "selected_operations": ["DELETE /pet/{petId}"]})
    resp = api.client.post(f"/api/v1/integrations/{created['id']}/test")
    assert resp.json()["status"] == "info"


# ── Spec management ──────────────────────────────────────────────────────────


def test_get_spec_and_change_the_selection(api):
    created = _create(api)
    resp = api.client.get(f"/api/v1/integrations/{created['id']}/openapi")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["source_type"] == "file"
    assert data["auth_option"]["id"] == "api_key"
    assert data["selected_count"] == 8
    assert [t["name"] for t in data["tags"]] == ["pet", "store"]

    resp = api.client.put(
        f"/api/v1/integrations/{created['id']}/openapi/operations",
        json={"selected_operations": ["GET /store/inventory", "GET /pet/{petId}"]},
    )
    assert resp.status_code == 200, resp.text
    spec = api.db.specs[created["id"]]
    # Document order, not request order.
    assert spec.selected_operation_keys == ["GET /pet/{petId}", "GET /store/inventory"]
    assert [t["name"] for t in spec.active_tools] == ["petstore_get_pet_by_id", "petstore_get_inventory"]

    resp = api.client.put(
        f"/api/v1/integrations/{created['id']}/openapi/operations",
        json={"selected_operations": ["GET /missing"]},
    )
    assert resp.status_code == 400


async def test_the_api_key_is_optional(api, monkeypatch):
    created = _create(api, api_key=None)
    assert created["status"] == "connected"
    seen = _mock_api(monkeypatch, lambda r: httpx.Response(200, json={"id": 1}))
    defs, executor = await build_integration_tools(api.db, ToolBindings({"openapi": created["id"]}, basis=BindingBasis.CALLER_CHECKED, reason="test"))
    assert "petstore_get_pet_by_id" in {d.name for d in defs}
    result = await executor.execute_tool(ToolCall(id="1", name="petstore_get_pet_by_id", arguments={"petId": 1}))
    assert not result.is_error, result.content
    assert "api_key" not in seen[0].headers


# ── Tool names and descriptions ──────────────────────────────────────────────


_RENAMED = {
    "GET /pet/{petId}": {"name": "find_pet", "description": "Look up one pet by its id."},
    "GET /store/inventory": {"description": "Stock per pet status."},
}


async def test_tools_can_be_renamed_and_described_at_creation(api):
    created = _create(
        api,
        openapi={
            "content": PETSTORE_V3,
            "auth_option": "api_key",
            "selected_operations": ["GET /pet/{petId}", "GET /store/inventory"],
            "tool_overrides": _RENAMED,
        },
    )
    tools = {t["name"]: t for t in api.client.get(f"/api/v1/integrations/{created['id']}/tools").json()["tools"]}
    assert set(tools) == {"find_pet", "petstore_get_inventory"}
    assert tools["find_pet"]["description"] == "Look up one pet by its id."
    assert tools["petstore_get_inventory"]["description"] == "Stock per pet status."
    defs, _executor = await build_integration_tools(api.db, ToolBindings({"openapi": created["id"]}, basis=BindingBasis.CALLER_CHECKED, reason="test"))
    assert {d.name for d in defs} == {"find_pet", "petstore_get_inventory"}

    spec = api.client.get(f"/api/v1/integrations/{created['id']}/openapi").json()
    by_key = {op["key"]: op for op in spec["operations"]}
    pet = by_key["GET /pet/{petId}"]
    assert (pet["tool_name"], pet["default_tool_name"]) == ("find_pet", "petstore_get_pet_by_id")
    assert pet["tool_description"] == "Look up one pet by its id."
    assert pet["default_tool_description"].startswith("GET /pet/{petId}")


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"GET /pet/{petId}": {"name": "9lives"}}, "must start with a letter"),
        ({"GET /pet/{petId}": {"name": "pet__lookup"}}, "without '__'"),
        ({"GET /pet/{petId}": {"name": "web_fetch"}}, "built-in tool"),
        ({"GET /pet/{petId}": {"name": "petstore_get_inventory"}}, "Two selected tools would be named"),
        ({"GET /nope": {"name": "x"}}, "is not an operation"),
    ],
)
def test_tool_overrides_are_validated(api, overrides, fragment):
    resp = api.client.post(
        "/api/v1/integrations",
        json={
            "provider": "openapi",
            "name": "x",
            "api_key": "k",
            "extra_config": {"base_url": "https://a.test"},
            "openapi": {"content": PETSTORE_V3, "tool_overrides": overrides},
        },
    )
    assert resp.status_code == 400, resp.text
    assert fragment in resp.json()["detail"]
    assert api.db.integrations == {}


def test_selection_updates_keep_or_replace_the_overrides(api):
    created = _create(api, openapi={"content": PETSTORE_V3, "auth_option": "api_key", "tool_overrides": _RENAMED})
    url = f"/api/v1/integrations/{created['id']}/openapi/operations"
    keys = ["GET /pet/{petId}", "GET /store/inventory"]

    resp = api.client.put(url, json={"selected_operations": keys})
    assert resp.status_code == 200, resp.text
    spec = api.db.specs[created["id"]]
    assert [t["name"] for t in spec.active_tools] == ["find_pet", "petstore_get_inventory"]

    resp = api.client.put(url, json={"selected_operations": keys, "tool_overrides": {"GET /store/inventory": {"name": "stock"}}})
    assert resp.status_code == 200, resp.text
    assert [t["name"] for t in spec.active_tools] == ["petstore_get_pet_by_id", "stock"]

    resp = api.client.put(url, json={"selected_operations": keys, "tool_overrides": {}})
    assert resp.status_code == 200, resp.text
    assert spec.tool_overrides is None
    assert [t["name"] for t in spec.active_tools] == ["petstore_get_pet_by_id", "petstore_get_inventory"]


def test_refresh_keeps_the_tool_overrides(api, monkeypatch):
    _serve_spec(monkeypatch, PETSTORE_V3)
    created = _create(
        api,
        extra_config=None,
        openapi={
            "url": "https://petstore3.swagger.io/api/v3/openapi.yaml",
            "auth_option": "api_key",
            "selected_operations": ["GET /pet/{petId}"],
            "tool_overrides": _RENAMED,
        },
    )
    resp = api.client.post(f"/api/v1/integrations/{created['id']}/openapi/refresh")
    assert resp.status_code == 200, resp.text
    spec = api.db.specs[created["id"]]
    assert [t["name"] for t in spec.active_tools] == ["find_pet"]
    assert spec.tool_overrides == _RENAMED


async def test_a_provider_wide_openapi_allow_list_is_split_per_integration(api):
    from app.services.openapi_import import per_integration_tool_scopes

    pets = _create(api, name="Petstore")
    stock = _create(
        api,
        name="Inventory",
        api_key=None,
        client_id="cid",
        client_secret="csecret",
        extra_config=None,
        openapi={"content": INVENTORY_V31, "auth_option": "oauth:client_credentials"},
    )
    ids = [pets["id"], stock["id"]]

    split = await per_integration_tool_scopes(
        api.db, {"openapi": ["petstore_get_pet_by_id", "unknown_tool"], "github": ["x"]}, ids
    )
    # Only the integration the list names is restricted.
    assert split == {"github": ["x"], f"openapi@{pets['id']}": ["petstore_get_pet_by_id"]}

    suffixed = await per_integration_tool_scopes(api.db, {"openapi": ["petstore_get_pet_by_id__petstore"]}, ids)
    assert suffixed == {f"openapi@{pets['id']}": ["petstore_get_pet_by_id"]}

    narrowed = await per_integration_tool_scopes(
        api.db,
        {"openapi": ["petstore_get_pet_by_id", "petstore_get_inventory"], f"openapi@{pets['id']}": ["petstore_get_inventory"]},
        ids,
    )
    assert narrowed == {f"openapi@{pets['id']}": ["petstore_get_inventory"]}

    none_at_all = await per_integration_tool_scopes(api.db, {"openapi": []}, ids)
    assert none_at_all == {f"openapi@{pets['id']}": [], f"openapi@{stock['id']}": []}

    assert await per_integration_tool_scopes(api.db, {"openapi": None}, ids) is None
    assert await per_integration_tool_scopes(api.db, {"github": ["x"]}, ids) == {"github": ["x"]}


def test_spec_routes_refuse_other_providers(api):
    integration = Integration(provider="github", auth_type="api_key", name="gh", user_id=api.user.id)
    api.db.add(integration)
    resp = api.client.get(f"/api/v1/integrations/{integration.id}/openapi")
    assert resp.status_code == 400


def test_refresh_keeps_the_selection_and_leaves_new_operations_off(api, monkeypatch):
    fetches = _serve_spec(monkeypatch, PETSTORE_V3)
    created = _create(
        api,
        extra_config=None,
        openapi={
            "url": "https://petstore3.swagger.io/api/v3/openapi.yaml",
            "auth_option": "api_key",
            "selected_operations": ["GET /pet/{petId}", "GET /store/inventory"],
        },
    )
    grown = PETSTORE_V3.replace(
        "  /store/inventory:",
        "  /store/orders:\n    delete:\n      operationId: purgeOrders\n      responses:\n        '204':\n          description: gone\n  /store/inventory:",
    ).replace("  /pet/{petId}:\n    get:", "  /pet/{petId}:\n    x-removed:")
    _serve_spec(monkeypatch, grown)
    resp = api.client.post(f"/api/v1/integrations/{created['id']}/openapi/refresh")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["changed"] is True
    assert data["added_operations"] == ["DELETE /store/orders"]
    assert data["removed_operations"] == ["GET /pet/{petId}"]
    spec = api.db.specs[created["id"]]
    assert spec.selected_operation_keys == ["GET /store/inventory"]
    assert len(fetches) == 1


def test_refresh_needs_a_url_source(api):
    created = _create(api)
    resp = api.client.post(f"/api/v1/integrations/{created['id']}/openapi/refresh")
    assert resp.status_code == 400
    assert "uploaded as a file" in resp.json()["detail"]


def test_replace_within_the_same_credential_type(api):
    created = _create(api)
    resp = api.client.put(
        f"/api/v1/integrations/{created['id']}/openapi",
        json={"content": PETSTORE_V2, "auth_option": "api_key", "selected_operations": ["GET /store/inventory"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["spec_version"] == "2.0"
    assert api.db.specs[created["id"]].selected_operation_keys == ["GET /store/inventory"]

    # An OAuth option would need a credential this integration does not store.
    resp = api.client.put(
        f"/api/v1/integrations/{created['id']}/openapi",
        json={"content": INVENTORY_V31, "auth_option": "oauth:authorization_code"},
    )
    assert resp.status_code == 400
    assert "different kind of credential" in resp.json()["detail"]


def test_replace_attaches_a_first_spec_to_a_spec_less_integration(api):
    """PUT /{id}/openapi on an OpenAPI integration with no spec row imports it."""
    from app.core.security import encrypt_value

    integration = Integration(
        provider="openapi", auth_type="api_key", name="Legacy", user_id=api.user.id,
        api_key_enc=encrypt_value("kept-key"),
        extra_config_enc=encrypt_value('{"base_url": "https://petstore3.swagger.io/api/v3"}'),
    )
    api.db.add(integration)
    key_before = integration.api_key_enc
    resp = api.client.put(
        f"/api/v1/integrations/{integration.id}/openapi",
        json={"content": PETSTORE_V3, "auth_option": "api_key", "selected_operations": ["GET /store/inventory"]},
    )
    assert resp.status_code == 200, resp.text
    spec = api.db.specs[str(integration.id)]
    assert spec.selected_operation_keys == ["GET /store/inventory"]
    assert integration.api_key_enc == key_before
    # The integration row is locked before the spec is read.
    assert api.db.locked == [str(integration.id)]
    # A second first attach (it waited on the lock) finds the spec and replaces it.
    resp = api.client.put(
        f"/api/v1/integrations/{integration.id}/openapi",
        json={"content": PETSTORE_V3, "auth_option": "api_key", "selected_operations": ["GET /pet/{petId}"]},
    )
    assert resp.status_code == 200, resp.text
    assert api.db.specs[str(integration.id)] is spec
    assert spec.selected_operation_keys == ["GET /pet/{petId}"]


def test_the_edit_authority_is_checked_again_after_the_lock(api, monkeypatch):
    """A permission lost while the call waited on the row lock refuses the attach."""
    from app.core.security import encrypt_value

    integration = Integration(
        provider="openapi", auth_type="api_key", name="Legacy", user_id=api.user.id,
        extra_config_enc=encrypt_value('{"base_url": "https://petstore3.swagger.io/api/v3"}'),
    )
    api.db.add(integration)
    checks: list[str] = []

    def _can_manage(_user, _integration, _groups):
        checks.append("check")
        # Allowed before the lock, revoked once the row is locked.
        return not api.db.locked

    monkeypatch.setattr(openapi_api, "can_manage", _can_manage)
    resp = api.client.put(
        f"/api/v1/integrations/{integration.id}/openapi",
        json={"content": PETSTORE_V3, "auth_option": "api_key"},
    )
    assert resp.status_code == 403
    assert len(checks) == 2
    assert str(integration.id) not in api.db.specs


@pytest.mark.parametrize("base_url", ["ftp://example.com", "not a url", 42])
def test_a_first_spec_refuses_a_malformed_legacy_base_url(api, base_url):
    import json as _json

    from app.core.security import encrypt_value

    integration = Integration(
        provider="openapi", auth_type="api_key", name="Legacy", user_id=api.user.id,
        api_key_enc=encrypt_value("kept-key"),
        extra_config_enc=encrypt_value(_json.dumps({"base_url": base_url})),
    )
    api.db.add(integration)
    resp = api.client.put(
        f"/api/v1/integrations/{integration.id}/openapi",
        json={"content": PETSTORE_V3, "auth_option": "api_key"},
    )
    assert resp.status_code == 400
    assert "http:// or https://" in resp.json()["detail"]
    assert str(integration.id) not in api.db.specs


def test_a_first_spec_completes_a_token_grant_integration(api):
    """The spec's token URL completes a client-credentials integration: it becomes connected."""
    from app.core.security import encrypt_value

    integration = Integration(
        provider="openapi", auth_type="client_credentials", name="Legacy", user_id=api.user.id,
        status="disconnected", client_id_enc=encrypt_value("cid"), client_secret_enc=encrypt_value("cs"),
        extra_config_enc=encrypt_value('{"base_url": "https://inventory.acme.test/v2"}'),
    )
    api.db.add(integration)
    resp = api.client.put(
        f"/api/v1/integrations/{integration.id}/openapi",
        json={"content": INVENTORY_V31, "auth_option": "oauth:client_credentials"},
    )
    assert resp.status_code == 200, resp.text
    assert integration.token_url
    assert integration.status == "connected"


def test_update_recomputes_client_credentials_status(api):
    created = _create(
        api,
        api_key=None,
        client_id="cid",
        client_secret="csecret",
        extra_config=None,
        openapi={"content": INVENTORY_V31, "auth_option": "oauth:client_credentials"},
    )
    resp = api.client.put(f"/api/v1/integrations/{created['id']}", json={"token_url": ""})
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "disconnected"
    resp = api.client.put(
        f"/api/v1/integrations/{created['id']}", json={"token_url": "https://auth.acme.test/oauth/token"}
    )
    assert resp.json()["status"] == "connected"
    assert decrypt_value(api.db.integrations[created["id"]].client_secret_enc) == "csecret"


def test_generic_update_cannot_switch_the_auth_type(api):
    created = _create(api)
    resp = api.client.put(f"/api/v1/integrations/{created['id']}", json={"auth_type": "none"})
    assert resp.status_code == 400
    assert "authentication option" in resp.json()["detail"]


# ── Per-user (template) bindings ─────────────────────────────────────────────
# Every OpenAPI integration is a different API, so "each user's own openapi
# integration" names no API in particular: the provider never resolves per user.


def test_per_user_resolution_never_picks_an_openapi_integration():
    from sqlalchemy import select
    from sqlalchemy.dialects import postgresql

    from app.services.integration_tools import template_candidate_clause

    for acting_user in (uuid.uuid4(), None):
        compiled = (
            select(Integration.id)
            .where(template_candidate_clause(acting_user))
            .compile(dialect=postgresql.dialect())
        )
        assert "integrations.provider NOT IN" in str(compiled)
        assert any(
            isinstance(v, list | tuple | set | frozenset) and set(v) == {"openapi"}
            for v in compiled.params.values()
        )


@pytest.mark.parametrize("model", ["CreateAgentRequest", "UpdateAgentRequest"])
def test_agents_cannot_bind_openapi_per_user(model):
    from pydantic import ValidationError

    from app.api.v1.schemas import agent as agent_schemas

    cls = getattr(agent_schemas, model)
    base = {"name": "Agent"} if model == "CreateAgentRequest" else {}
    with pytest.raises(ValidationError, match="Bind the specific integration instead"):
        cls(**base, integration_template_bindings=["opencti", "openapi"])
    assert cls(**base, integration_template_bindings=["opencti"]).integration_template_bindings == ["opencti"]

