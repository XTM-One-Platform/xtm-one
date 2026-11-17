from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.api.deps import require_agentic_ai_licensed
from app.api.v1 import integrations as integrations_api
from app.core import feature_flags
from app.core.database import get_db
from app.core.security import decrypt_json, encrypt_value
from app.models.agent import Agent
from app.models.custom_integration_definition import CustomIntegrationDefinition
from app.models.integration import Integration
from app.services.integration_bundle_manager import (
    build_integration_bundle_zip,
    parse_integration_bundle_zip,
)
from tests.a2a_chat_gate import stub_chat_gate


@dataclass
class _Result:
    rows: list | None = None
    scalar: object | None = None

    def scalar_one_or_none(self):
        if self.scalar is not None:
            return self.scalar
        rows = self.rows or []
        return rows[0] if rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self.rows or [])


class _FakeDb:
    def __init__(self):
        self.custom_defs: dict[str, CustomIntegrationDefinition] = {}
        self.integrations: dict[str, Integration] = {}
        self.agents: dict[str, Agent] = {}

    def add(self, obj):
        if isinstance(obj, CustomIntegrationDefinition):
            if obj.id is None:
                obj.id = uuid.uuid4()
            self.custom_defs[obj.provider_key] = obj
            return
        if isinstance(obj, Integration):
            if obj.id is None:
                obj.id = uuid.uuid4()
            if obj.is_builtin is None:
                obj.is_builtin = False
            now = datetime.now(UTC)
            if obj.created_at is None:
                obj.created_at = now
            if obj.updated_at is None:
                obj.updated_at = now
            self.integrations[str(obj.id)] = obj
            return

    async def delete(self, obj):
        if isinstance(obj, CustomIntegrationDefinition):
            self.custom_defs.pop(obj.provider_key, None)
            return
        if isinstance(obj, Integration):
            self.integrations.pop(str(obj.id), None)
            return

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0].get("entity")
        params = stmt.compile().params

        if entity is CustomIntegrationDefinition:
            provider = next((v for k, v in params.items() if "provider_key" in k), None)
            if provider is None:
                return _Result(rows=list(self.custom_defs.values()))
            return _Result(scalar=self.custom_defs.get(provider))

        if entity is Integration:
            # Compiled param names look like "id_1" / "provider_1"; match on the
            # column-name prefix only ("provider" itself contains "id").
            # ``Integration.id == x`` compiles to a scalar param while
            # ``Integration.id.in_([...])`` compiles to an expanding param
            # holding a list — support both so the batch row-cache loader in
            # ``_build_integration_tools_inner`` sees the same rows a real DB
            # would (a cache miss there now means "integration deleted").
            id_values: list = []
            for k, v in params.items():
                if k.split("_")[0] == "id":
                    if isinstance(v, list | tuple | set):
                        id_values.extend(v)
                    else:
                        id_values.append(v)
            if id_values:
                rows = [
                    self.integrations[str(v)]
                    for v in id_values
                    if str(v) in self.integrations
                ]
                return _Result(rows=rows)
            provider = next((v for k, v in params.items() if "provider" in k), None)
            if provider is None:
                return _Result(rows=list(self.integrations.values()))
            rows = [i for i in self.integrations.values() if i.provider == provider]
            return _Result(rows=rows)

        if entity is Agent:
            # Mirror cast(integration_bindings, Text).contains(...) with any
            # number of OR'd needles (provider key + integration ids).
            needles = [str(v) for v in params.values()]
            rows = []
            for agent in self.agents.values():
                blob = json.dumps(agent.integration_bindings or {})
                if any(needle in blob for needle in needles):
                    rows.append(agent)
            return _Result(rows=rows)

        return _Result(rows=[])


@pytest.fixture
def bundles_client(monkeypatch):
    fake_db = _FakeDb()
    fake_user = SimpleNamespace(
        id=uuid.uuid4(),
        full_name="Admin",
        role="admin",
        is_admin=True,
    )

    async def _get_db():
        yield fake_db

    async def _audit(*_a, **_k):
        return None

    async def _platform_settings(_db):
        return SimpleNamespace(disabled_integration_providers=[])

    monkeypatch.setattr(integrations_api, "log_audit", _audit)
    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _platform_settings,
    )
    # The catalog listing resolves the deployment tier to decide whether the
    # A2A tiles are offered, and the settings double above carries no licence
    # fields. These tests are about bundles, not A2A — answer "licensed" so
    # the listing reaches the tiles under test.
    stub_chat_gate(monkeypatch)

    app = FastAPI()
    app.include_router(integrations_api.router, prefix="/api/v1/integrations")
    app.dependency_overrides[deps.get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[require_agentic_ai_licensed] = lambda: None

    with TestClient(app) as client:
        yield client, fake_db


def _bundle(version: str = "1.0.0") -> bytes:
    metadata = {
        "format": "filigran-integration-v1",
        "version": version,
        "name": "Custom Demo",
        "slug": "custom-demo",
        "description": "Custom provider from bundle.",
        "author": "QA",
        "provider": "custom-demo",
        "auth_type": "none",
        "icon": "assets/icon.png",
        "settings": [
            {
                "key": "region",
                "label": "Region",
                "type": "select",
                "required": True,
                "options": [
                    {"value": "eu", "label": "EU"},
                    {"value": "us", "label": "US"},
                ],
            }
        ],
        "supports_polling": True,
        "supports_webhook": False,
        "test_connection": {"mode": "none"},
        "exported_at": "2026-07-07T00:00:00Z",
    }
    return build_integration_bundle_zip(metadata, assets={"assets/icon.png": b"PNG"})


def test_validate_bundle_route_is_not_feature_gated(bundles_client, monkeypatch):
    client, _db = bundles_client
    monkeypatch.setattr(feature_flags, "is_feature_enabled", lambda _flag: False)

    resp = client.post(
        "/api/v1/integrations/admin/import/validate",
        files={"bundle": ("custom.zip", _bundle(), "application/zip")},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["normalized_provider"] == "custom:custom-demo"


def test_oversized_bundle_upload_is_rejected_early(bundles_client):
    """Uploads beyond the bundle size limit are rejected by the bounded read
    (before ZIP parsing), on both the validate and import endpoints."""
    from app.services.integration_bundle_manager import INTEGRATION_BUNDLE_MAX_BYTES

    client, _db = bundles_client
    oversized = b"\0" * (INTEGRATION_BUNDLE_MAX_BYTES + 1)

    for endpoint in (
        "/api/v1/integrations/admin/import/validate",
        "/api/v1/integrations/admin/import",
    ):
        resp = client.post(
            endpoint,
            files={"bundle": ("big.zip", oversized, "application/zip")},
        )
        assert resp.status_code == 400, resp.text
        assert "maximum size" in resp.text


def test_validate_and_import_upgrade_flow(bundles_client):
    client, db = bundles_client

    validate_resp = client.post(
        "/api/v1/integrations/admin/import/validate",
        files={"bundle": ("custom.zip", _bundle("1.0.0"), "application/zip")},
    )
    assert validate_resp.status_code == 200, validate_resp.text
    assert validate_resp.json()["normalized_provider"] == "custom:custom-demo"

    import_v1 = client.post(
        "/api/v1/integrations/admin/import",
        files={"bundle": ("custom.zip", _bundle("1.0.0"), "application/zip")},
    )
    assert import_v1.status_code == 200, import_v1.text
    assert import_v1.json()["upgraded"] is False
    assert db.custom_defs["custom:custom-demo"].version == "1.0.0"

    import_v1_repeat = client.post(
        "/api/v1/integrations/admin/import",
        files={"bundle": ("custom.zip", _bundle("1.0.0"), "application/zip")},
    )
    assert import_v1_repeat.status_code == 409

    import_v2 = client.post(
        "/api/v1/integrations/admin/import",
        files={"bundle": ("custom.zip", _bundle("1.1.0"), "application/zip")},
    )
    assert import_v2.status_code == 200, import_v2.text
    assert import_v2.json()["upgraded"] is True
    assert db.custom_defs["custom:custom-demo"].version == "1.1.0"


def test_custom_provider_disable_blocks_create_integration(
    bundles_client,
):
    client, _db = bundles_client

    resp_import = client.post(
        "/api/v1/integrations/admin/import",
        files={"bundle": ("custom.zip", _bundle("1.0.0"), "application/zip")},
    )
    assert resp_import.status_code == 200, resp_import.text

    resp_disable = client.put(
        "/api/v1/integrations/admin/custom/custom-demo/enabled/false"
    )
    assert resp_disable.status_code == 200, resp_disable.text

    create_resp = client.post(
        "/api/v1/integrations",
        json={
            "provider": "custom:custom-demo",
            "name": "Demo Integration",
            "auth_type": "none",
            "extra_config": {"region": "eu"},
        },
    )
    assert create_resp.status_code == 403
    assert "disabled" in create_resp.text.lower()


def test_export_custom_provider_bundle_returns_valid_zip(
    bundles_client,
):
    client, _db = bundles_client

    resp_import = client.post(
        "/api/v1/integrations/admin/import",
        files={"bundle": ("custom.zip", _bundle("2.0.0"), "application/zip")},
    )
    assert resp_import.status_code == 200, resp_import.text

    export_resp = client.get("/api/v1/integrations/admin/export/custom:custom-demo")
    assert export_resp.status_code == 200, export_resp.text
    assert export_resp.headers["content-type"] == "application/zip"

    parsed = parse_integration_bundle_zip(export_resp.content)
    assert parsed.metadata.slug == "custom-demo"
    assert parsed.metadata.version == "2.0.0"


def test_export_existing_integration_includes_instance_config(
    bundles_client,
):
    client, db = bundles_client

    resp_import = client.post(
        "/api/v1/integrations/admin/import",
        files={"bundle": ("custom.zip", _bundle("2.0.0"), "application/zip")},
    )
    assert resp_import.status_code == 200, resp_import.text

    integration = Integration(
        id=uuid.uuid4(),
        provider="custom:custom-demo",
        auth_type="api_key",
        name="Prod Demo",
        description="Prod environment",
        tags=["prod"],
        client_id_enc=encrypt_value("client-id"),
        client_secret_enc=encrypt_value("client-secret"),
        api_key_enc=encrypt_value("api-key"),
        extra_config_enc=encrypt_value('{"region":"eu"}'),
    )
    db.integrations[str(integration.id)] = integration

    export_resp = client.get(
        f"/api/v1/integrations/admin/export/custom:custom-demo?integration_id={integration.id}"
    )
    assert export_resp.status_code == 200, export_resp.text

    parsed = parse_integration_bundle_zip(export_resp.content)
    assert parsed.integration_instance is not None
    assert parsed.integration_instance.name == "Prod Demo"
    assert parsed.integration_instance.auth_type == "api_key"
    assert parsed.integration_instance.client_id == "client-id"
    assert parsed.integration_instance.client_secret == "client-secret"
    assert parsed.integration_instance.api_key == "api-key"
    assert parsed.integration_instance.extra_config == {"region": "eu"}


def test_import_bundle_with_instance_creates_integration(
    bundles_client,
):
    client, db = bundles_client

    metadata = {
        "format": "filigran-integration-v1",
        "version": "1.0.0",
        "name": "Custom Demo",
        "slug": "custom-demo",
        "description": "Custom provider from bundle.",
        "author": "QA",
        "provider": "custom-demo",
        "auth_type": "none",
        "icon": None,
        "settings": [],
        "supports_polling": True,
        "supports_webhook": False,
        "test_connection": {"mode": "none"},
        "exported_at": "2026-07-07T00:00:00Z",
        "integration_instance": {
            "name": "Imported Demo Integration",
            "auth_type": "none",
            "description": "Imported instance config",
            "extra_config": {"region": "eu"},
            "tags": ["bundle"],
        },
    }
    bundle = build_integration_bundle_zip(metadata)

    import_resp = client.post(
        "/api/v1/integrations/admin/import",
        files={"bundle": ("custom.zip", bundle, "application/zip")},
    )
    assert import_resp.status_code == 200, import_resp.text
    payload = import_resp.json()
    assert payload["integration_created"] is True
    assert payload["integration_name"] == "Imported Demo Integration"
    assert payload["integration_id"] is not None

    created = db.integrations[payload["integration_id"]]
    assert created.provider == "custom:custom-demo"
    assert created.name == "Imported Demo Integration"


def test_imported_provider_uses_bundle_setup_guide(
    bundles_client,
):
    client, _db = bundles_client

    metadata = {
        "format": "filigran-integration-v1",
        "version": "1.0.0",
        "name": "YouTube Integration",
        "slug": "youtube-integration",
        "description": "YouTube custom provider.",
        "author": "QA",
        "provider": "youtube-integration",
        "auth_type": "api_key",
        "icon": None,
        "settings": [],
        "supports_polling": True,
        "supports_webhook": False,
        "test_connection": {"mode": "none"},
        "exported_at": "2026-07-08T00:00:00Z",
        "setup_guide": {
            "title": "Configure YouTube Data API",
            "url": "https://console.cloud.google.com/apis/library/youtube.googleapis.com",
            "steps": [
                "Create a Google Cloud project.",
                "Enable YouTube Data API v3.",
                "Create an API key and restrict it.",
            ],
            "permissions": [
                {
                    "scope": "API key",
                    "description": "Used to query public YouTube resources.",
                }
            ],
            "notes": "Prefer API restrictions + HTTP referrer restrictions.",
        },
    }

    bundle = build_integration_bundle_zip(metadata)
    import_resp = client.post(
        "/api/v1/integrations/admin/import",
        files={"bundle": ("youtube.zip", bundle, "application/zip")},
    )
    assert import_resp.status_code == 200, import_resp.text

    catalog_resp = client.get("/api/v1/integrations/catalog?category=integration")
    assert catalog_resp.status_code == 200, catalog_resp.text
    items = catalog_resp.json()
    youtube = next(i for i in items if i["provider"] == "custom:youtube-integration")
    guide = youtube["setup_guide"]
    assert guide["title"] == "Configure YouTube Data API"
    assert guide["url"] == "https://console.cloud.google.com/apis/library/youtube.googleapis.com"
    assert len(guide["steps"]) == 3
    assert guide["permissions"][0]["scope"] == "API key"


def test_catalog_setup_guide_credits_the_open_source_upstream(bundles_client):
    client, _db = bundles_client

    resp = client.get("/api/v1/integrations/catalog?category=integration")
    assert resp.status_code == 200, resp.text
    nova = next(i for i in resp.json() if i["provider"] == "nova")
    assert nova["setup_guide"]["open_source"] == {
        "name": "NOVA",
        "url": "https://github.com/Nova-Hunting/nova-framework",
        "author": "Thomas Roccia",
        "license": "MIT",
    }


def test_imported_bundle_cannot_set_the_open_source_credit(bundles_client):
    """The credit is rendered as a link: a bundle must not be able to supply it."""
    client, _db = bundles_client
    metadata = {
        "format": "filigran-integration-v1",
        "version": "1.0.0",
        "name": "Custom Demo",
        "slug": "custom-demo",
        "description": "Custom provider from bundle.",
        "author": "QA",
        "provider": "custom-demo",
        "auth_type": "none",
        "icon": None,
        "settings": [],
        "supports_polling": True,
        "supports_webhook": False,
        "test_connection": {"mode": "none"},
        "exported_at": "2026-07-08T00:00:00Z",
        "setup_guide": {
            "title": "Configure Demo",
            "steps": ["Step one."],
            "open_source": {
                "name": "x",
                "url": "javascript:alert(1)",
                "author": "x",
                "license": "x",
            },
        },
    }
    import_resp = client.post(
        "/api/v1/integrations/admin/import",
        files={"bundle": ("demo.zip", build_integration_bundle_zip(metadata), "application/zip")},
    )
    assert import_resp.status_code == 200, import_resp.text

    resp = client.get("/api/v1/integrations/catalog?category=integration")
    assert resp.status_code == 200, resp.text
    demo = next(i for i in resp.json() if i["provider"] == "custom:custom-demo")
    assert demo["setup_guide"]["title"] == "Configure Demo"
    assert demo["setup_guide"]["open_source"] is None


@pytest.mark.parametrize(
    ("url", "kept"),
    [
        ("https://console.example.com/keys", "https://console.example.com/keys"),
        ("  http://intranet.example:8080/admin  ", "http://intranet.example:8080/admin"),
        ("javascript:alert(1)", None),
        ("data:text/html,<b>x</b>", None),
        ("https://console.example.com@evil.example/", None),
        ("https://evil.example\\@console.example.com/", None),
        ("/relative/path", None),
    ],
)
def test_imported_bundle_console_url_is_linked_only_when_safe(bundles_client, url, kept):
    """The bundle's console URL is a link in the same dialog: unsafe ones are dropped."""
    client, _db = bundles_client
    metadata = {
        "format": "filigran-integration-v1",
        "version": "1.0.0",
        "name": "Custom Console",
        "slug": "custom-console",
        "description": "Custom provider from bundle.",
        "author": "QA",
        "provider": "custom-console",
        "auth_type": "none",
        "icon": None,
        "settings": [],
        "supports_polling": True,
        "supports_webhook": False,
        "test_connection": {"mode": "none"},
        "exported_at": "2026-07-08T00:00:00Z",
        "setup_guide": {"title": "Configure Console", "url": url, "steps": ["Step one."]},
    }
    import_resp = client.post(
        "/api/v1/integrations/admin/import",
        files={"bundle": ("console.zip", build_integration_bundle_zip(metadata), "application/zip")},
    )
    assert import_resp.status_code == 200, import_resp.text

    resp = client.get("/api/v1/integrations/catalog?category=integration")
    assert resp.status_code == 200, resp.text
    entry = next(i for i in resp.json() if i["provider"] == "custom:custom-console")
    assert entry["setup_guide"]["url"] == kept


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html,<b>x</b>",
        "http://github.com/x/y",
        "HTTPS://github.com/x/y",
        "//github.com/x/y",
        "",
        " https://github.com/x/y",
        "https://github.com/x/y\n",
        "https://github.com/x y",
        "https://",
        "https:///project",
        "https:github.com/x/y",
        "https://:443/x",
        "https://github.com@evil.example/x",
        "https://user:pass@github.com/x",
        "https://evil.example\\@github.com/x",
        "https://github.com:0/x",
        "https://github.com:99999/x",
        "https://[github.com/x",
        # A Cyrillic "i": a homograph host, refused unless spelt in punycode.
        "https://g\u0456thub.com/x",
    ],
)
def test_setup_guide_open_source_url_must_be_a_plain_https_link(url):
    from pydantic import ValidationError

    from app.api.v1.schemas.integration import SetupGuideOpenSource

    with pytest.raises(ValidationError):
        SetupGuideOpenSource(name="x", url=url)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Nova-Hunting/nova-framework",
        "https://xn--gthub-n4a.example:8443/a?b=1#c",
        "https://[2001:db8::1]/project",
    ],
)
def test_setup_guide_open_source_accepts_an_https_link(url):
    from app.api.v1.schemas.integration import SetupGuideOpenSource

    assert SetupGuideOpenSource(name="x", url=url).url == url


def test_imported_provider_tools_are_exposed_in_catalog_and_integration_tools(
    bundles_client,
):
    client, _db = bundles_client

    metadata = {
        "format": "filigran-integration-v1",
        "version": "1.0.0",
        "name": "YouTube Integration",
        "slug": "youtube-integration",
        "description": "YouTube custom provider.",
        "author": "QA",
        "provider": "youtube-integration",
        "auth_type": "api_key",
        "icon": None,
        "settings": [],
        "supports_polling": True,
        "supports_webhook": False,
        "test_connection": {"mode": "none"},
        "tools": [
            {
                "name": "search_youtube_videos",
                "description": "Search public YouTube videos.",
                "tool_type": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                    "required": ["query"],
                },
            }
        ],
        "exported_at": "2026-07-08T00:00:00Z",
    }

    bundle = build_integration_bundle_zip(metadata)
    import_resp = client.post(
        "/api/v1/integrations/admin/import",
        files={"bundle": ("youtube.zip", bundle, "application/zip")},
    )
    assert import_resp.status_code == 200, import_resp.text

    create_resp = client.post(
        "/api/v1/integrations",
        json={
            "provider": "custom:youtube-integration",
            "name": "YouTube Demo",
            "auth_type": "api_key",
            "api_key": "dummy",
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    integration_id = create_resp.json()["id"]

    tools_resp = client.get(f"/api/v1/integrations/{integration_id}/tools")
    assert tools_resp.status_code == 200, tools_resp.text
    tools = tools_resp.json()["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "search_youtube_videos"
    assert tools[0]["input_schema"]["required"] == ["query"]

    catalog_resp = client.get("/api/v1/integrations/catalog?category=integration")
    assert catalog_resp.status_code == 200, catalog_resp.text
    catalog_items = catalog_resp.json()
    youtube = next(i for i in catalog_items if i["provider"] == "custom:youtube-integration")
    assert youtube["available_tools"][0]["name"] == "search_youtube_videos"

    test_tool_resp = client.post(
        f"/api/v1/integrations/{integration_id}/test-tool",
        json={
            "tool_name": "search_youtube_videos",
            "arguments": {"query": "threat intelligence"},
        },
    )
    assert test_tool_resp.status_code == 200, test_tool_resp.text
    payload = test_tool_resp.json()
    assert payload["status"] == "error"
    assert "YouTube API key is missing or invalid" in payload["message"]


def test_disabled_custom_provider_stops_tool_resolution(
    bundles_client,
):
    """Disable is a reversible kill-switch: existing integrations of a
    disabled custom provider must stop resolving tools (not just block new
    integration creation)."""
    client, _db = bundles_client

    metadata = {
        "format": "filigran-integration-v1",
        "version": "1.0.0",
        "name": "YouTube Integration",
        "slug": "youtube-integration",
        "description": "YouTube custom provider.",
        "author": "QA",
        "provider": "youtube-integration",
        "auth_type": "api_key",
        "icon": None,
        "settings": [],
        "supports_polling": True,
        "supports_webhook": False,
        "test_connection": {"mode": "none"},
        "tools": [
            {
                "name": "search_youtube_videos",
                "description": "Search public YouTube videos.",
                "tool_type": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            }
        ],
        "exported_at": "2026-07-08T00:00:00Z",
    }
    bundle = build_integration_bundle_zip(metadata)
    import_resp = client.post(
        "/api/v1/integrations/admin/import",
        files={"bundle": ("youtube.zip", bundle, "application/zip")},
    )
    assert import_resp.status_code == 200, import_resp.text

    create_resp = client.post(
        "/api/v1/integrations",
        json={
            "provider": "custom:youtube-integration",
            "name": "YouTube Demo",
            "auth_type": "api_key",
            "api_key": "dummy",
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    integration_id = create_resp.json()["id"]

    disable_resp = client.put(
        "/api/v1/integrations/admin/custom/youtube-integration/enabled/false"
    )
    assert disable_resp.status_code == 200, disable_resp.text

    test_tool_resp = client.post(
        f"/api/v1/integrations/{integration_id}/test-tool",
        json={
            "tool_name": "search_youtube_videos",
            "arguments": {"query": "threat intelligence"},
        },
    )
    assert test_tool_resp.status_code == 200, test_tool_resp.text
    payload = test_tool_resp.json()
    assert payload["status"] == "error"
    assert "not found" in payload["message"]


def test_export_native_provider_with_underscore_key(bundles_client):
    client, _db = bundles_client

    export_resp = client.get("/api/v1/integrations/admin/export/mitre_attack")
    assert export_resp.status_code == 200, export_resp.text

    parsed = parse_integration_bundle_zip(export_resp.content)
    assert parsed.metadata.slug == "mitre-attack"
    assert parsed.metadata.provider == "mitre-attack"


def test_export_native_provider_uses_platform_version(bundles_client):
    """Native exports carry the running platform version (semver-guarded) so
    an export from a newer instance can be imported as an upgrade instead of
    always 409ing as another hardcoded 1.0.0."""
    from app.core.config import settings as app_settings
    from app.services.integration_bundle_manager import is_valid_semver

    client, _db = bundles_client

    export_resp = client.get("/api/v1/integrations/admin/export/mitre_attack")
    assert export_resp.status_code == 200, export_resp.text

    parsed = parse_integration_bundle_zip(export_resp.content)
    expected = (
        app_settings.app_version
        if is_valid_semver(app_settings.app_version)
        else "1.0.0"
    )
    assert parsed.metadata.version == expected
    # The download filename embeds the same version.
    assert f"-{expected}.zip" in export_resp.headers["content-disposition"]


def test_export_native_oauth_provider_with_no_settings(bundles_client):
    client, _db = bundles_client

    # "google" is pure OAuth (client_id/client_secret only) -> empty settings.
    export_resp = client.get("/api/v1/integrations/admin/export/google")
    assert export_resp.status_code == 200, export_resp.text

    parsed = parse_integration_bundle_zip(export_resp.content)
    assert parsed.metadata.slug == "google"
    assert parsed.metadata.settings == []


def test_semver_key_orders_prerelease_below_release():
    from app.api.v1.integrations import _semver_key

    assert _semver_key("1.0.0-alpha") < _semver_key("1.0.0")
    assert _semver_key("1.0.0") < _semver_key("1.0.1")
    assert _semver_key("1.0.0-alpha") < _semver_key("1.0.0-beta")


def test_semver_key_ignores_build_metadata():
    from app.api.v1.integrations import _semver_key

    # Per SemVer, build metadata MUST NOT affect precedence.
    assert _semver_key("1.0.0+build1") == _semver_key("1.0.0")
    assert _semver_key("1.0.0-alpha+build1") == _semver_key("1.0.0-alpha")
    assert _semver_key("1.0.0-alpha+build1") < _semver_key("1.0.0")


def test_semver_key_prerelease_precedence():
    from app.api.v1.integrations import _semver_key

    # Numeric pre-release identifiers compare numerically (spec item 4):
    # alpha.2 < alpha.10, so a re-import of alpha.2 over alpha.10 is a
    # downgrade and must be rejected.
    assert _semver_key("1.0.0-alpha.2") < _semver_key("1.0.0-alpha.10")
    # Numeric identifiers rank below alphanumeric ones.
    assert _semver_key("1.0.0-alpha.1") < _semver_key("1.0.0-alpha.beta")
    # Shorter identifier list sorts lower when the prefix matches.
    assert _semver_key("1.0.0-alpha") < _semver_key("1.0.0-alpha.1")
    # Canonical spec ordering chain.
    chain = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
    ]
    keys = [_semver_key(v) for v in chain]
    assert keys == sorted(keys)


def test_import_strips_instance_credentials_from_stored_definition(
    bundles_client,
):
    """The embedded integration_instance carries plaintext credentials: it must
    be materialized as an encrypted Integration row only, never persisted in
    metadata_json nor silently re-embedded in later provider exports."""
    client, db = bundles_client

    metadata = {
        "format": "filigran-integration-v1",
        "version": "1.0.0",
        "name": "Custom Demo",
        "slug": "custom-demo",
        "description": "Custom provider from bundle.",
        "author": "QA",
        "provider": "custom-demo",
        "auth_type": "api_key",
        "icon": None,
        "settings": [],
        "supports_polling": True,
        "supports_webhook": False,
        "test_connection": {"mode": "none"},
        "exported_at": "2026-07-07T00:00:00Z",
        "integration_instance": {
            "name": "Imported Demo Integration",
            "auth_type": "api_key",
            "api_key": "super-secret-key",
        },
    }
    bundle = build_integration_bundle_zip(metadata)

    import_resp = client.post(
        "/api/v1/integrations/admin/import",
        files={"bundle": ("custom.zip", bundle, "application/zip")},
    )
    assert import_resp.status_code == 200, import_resp.text
    assert import_resp.json()["integration_created"] is True

    stored = db.custom_defs["custom:custom-demo"]
    assert "integration_instance" not in stored.metadata_json
    assert "super-secret-key" not in json.dumps(stored.metadata_json)

    # A plain provider export (no ?integration_id=) must not leak the instance.
    export_resp = client.get("/api/v1/integrations/admin/export/custom:custom-demo")
    assert export_resp.status_code == 200, export_resp.text
    parsed = parse_integration_bundle_zip(export_resp.content)
    assert parsed.integration_instance is None
    assert b"super-secret-key" not in export_resp.content


def test_schema_declared_secret_extra_config_is_masked_and_preserved(
    bundles_client,
):
    """Schema-declared secrets (type: "secret") with non-obvious key names must
    be redacted in API responses, and the update endpoint must treat the mask
    as "keep the stored value" so the edit roundtrip is loss-free."""
    client, db = bundles_client

    metadata = {
        "format": "filigran-integration-v1",
        "version": "1.0.0",
        "name": "Custom Demo",
        "slug": "custom-demo",
        "description": "Custom provider from bundle.",
        "author": "QA",
        "provider": "custom-demo",
        "auth_type": "none",
        "icon": None,
        "settings": [
            {"key": "auth", "label": "Auth", "type": "secret", "required": True},
            {"key": "region", "label": "Region", "type": "string", "required": False},
        ],
        "supports_polling": True,
        "supports_webhook": False,
        "test_connection": {"mode": "none"},
        "exported_at": "2026-07-07T00:00:00Z",
    }
    bundle = build_integration_bundle_zip(metadata)
    import_resp = client.post(
        "/api/v1/integrations/admin/import",
        files={"bundle": ("custom.zip", bundle, "application/zip")},
    )
    assert import_resp.status_code == 200, import_resp.text

    create_resp = client.post(
        "/api/v1/integrations",
        json={
            "provider": "custom:custom-demo",
            "name": "Demo Integration",
            "auth_type": "none",
            "extra_config": {"auth": "plaintext-secret", "region": "eu"},
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    created = create_resp.json()
    assert created["extra_config"]["auth"] == "********"
    assert created["extra_config"]["region"] == "eu"
    assert "plaintext-secret" not in create_resp.text

    integration_id = created["id"]
    get_resp = client.get(f"/api/v1/integrations/{integration_id}")
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["extra_config"]["auth"] == "********"

    # Round-tripping the mask keeps the stored secret intact.
    update_resp = client.put(
        f"/api/v1/integrations/{integration_id}",
        json={"extra_config": {"auth": "********", "region": "us"}},
    )
    assert update_resp.status_code == 200, update_resp.text
    stored = decrypt_json(db.integrations[integration_id].extra_config_enc)
    assert stored == {"auth": "plaintext-secret", "region": "us"}

    # A real new value replaces the stored secret.
    update_resp2 = client.put(
        f"/api/v1/integrations/{integration_id}",
        json={"extra_config": {"auth": "rotated-secret", "region": "us"}},
    )
    assert update_resp2.status_code == 200, update_resp2.text
    stored2 = decrypt_json(db.integrations[integration_id].extra_config_enc)
    assert stored2["auth"] == "rotated-secret"

    # Mask preservation is scoped to secret keys only: a NON-secret field can
    # be set to the literal mask string intentionally.
    update_resp3 = client.put(
        f"/api/v1/integrations/{integration_id}",
        json={"extra_config": {"auth": "********", "region": "********"}},
    )
    assert update_resp3.status_code == 200, update_resp3.text
    stored3 = decrypt_json(db.integrations[integration_id].extra_config_enc)
    assert stored3["auth"] == "rotated-secret"
    assert stored3["region"] == "********"


async def test_youtube_v3_get_detects_referrer_restriction_in_nested_error(
    monkeypatch,
):
    """YouTube Data API errors nest under a top-level "error" object; the
    referrer-restriction guidance must trigger on that real payload shape."""
    from app.services import integration_tools as it

    class _Resp:
        status_code = 403

        def json(self):
            return {
                "error": {
                    "code": 403,
                    "message": "Requests from referer <empty> are blocked.",
                    "errors": [{"reason": "forbidden", "domain": "global"}],
                    "details": [
                        {
                            "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                            "reason": "API_KEY_HTTP_REFERRER_BLOCKED",
                        }
                    ],
                }
            }

        def raise_for_status(self):
            raise AssertionError("guard should raise before raise_for_status")

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(it.httpx, "AsyncClient", _Client)

    with pytest.raises(ValueError, match="restricted by HTTP referrer"):
        await it._youtube_v3_get(
            api_key="AIza" + "x" * 30,
            resource="search",
            params={"part": "snippet"},
        )


async def test_youtube_v3_get_generic_forbidden_is_not_referrer_guidance(
    monkeypatch,
):
    """A generic 403 (quota exceeded, API disabled) carries the catch-all
    "forbidden" reason and must NOT be rewritten into referrer guidance."""
    import httpx as httpx_mod

    from app.services import integration_tools as it

    class _Resp:
        status_code = 403

        def json(self):
            return {
                "error": {
                    "code": 403,
                    "message": "Quota exceeded for quota metric 'Queries'.",
                    "errors": [{"reason": "forbidden", "domain": "global"}],
                }
            }

        def raise_for_status(self):
            raise httpx_mod.HTTPStatusError(
                "403 Forbidden",
                request=httpx_mod.Request("GET", "https://example.test"),
                response=httpx_mod.Response(403),
            )

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return _Resp()

    monkeypatch.setattr(it.httpx, "AsyncClient", _Client)

    with pytest.raises(httpx_mod.HTTPStatusError):
        await it._youtube_v3_get(
            api_key="AIza" + "x" * 30,
            resource="search",
            params={"part": "snippet"},
        )


def test_uninstall_custom_provider_cleans_integrations_and_bindings(
    bundles_client,
):
    client, db = bundles_client

    resp_import = client.post(
        "/api/v1/integrations/admin/import",
        files={"bundle": ("custom.zip", _bundle("1.0.0"), "application/zip")},
    )
    assert resp_import.status_code == 200, resp_import.text

    integration = Integration(
        id=uuid.uuid4(),
        provider="custom:custom-demo",
        auth_type="none",
        name="Demo Integration",
    )
    db.integrations[str(integration.id)] = integration

    agent = Agent(
        id=uuid.uuid4(),
        name="Ops Agent",
        integration_bindings={"custom:custom-demo": [str(integration.id)]},
    )
    db.agents[str(agent.id)] = agent

    delete_resp = client.delete("/api/v1/integrations/admin/custom/custom-demo")
    assert delete_resp.status_code == 204, delete_resp.text

    assert "custom:custom-demo" not in db.custom_defs
    assert str(integration.id) not in db.integrations
    assert agent.integration_bindings == {}


def test_catalog_exposes_disabled_providers_to_members_only_on_request(
    bundles_client, monkeypatch
):
    """Members see admin-disabled providers only when they ask for them.

    The marketplace opts in so it can render a greyed-out tile with a
    "disabled by your administrator" banner; every other catalog consumer
    keeps the old behaviour of not listing what cannot be connected.
    """
    client, _db = bundles_client
    client.app.dependency_overrides[deps.get_current_user] = lambda: SimpleNamespace(
        id=uuid.uuid4(),
        full_name="Member",
        role="member",
        is_admin=False,
    )

    async def _platform_settings(_db):
        return SimpleNamespace(disabled_integration_providers=["slack"])

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _platform_settings,
    )

    default_resp = client.get("/api/v1/integrations/catalog?category=integration")
    assert default_resp.status_code == 200, default_resp.text
    assert not any(i["provider"] == "slack" for i in default_resp.json())

    included_resp = client.get(
        "/api/v1/integrations/catalog?category=integration&include_disabled=true"
    )
    assert included_resp.status_code == 200, included_resp.text
    slack = next(i for i in included_resp.json() if i["provider"] == "slack")
    assert slack["is_disabled"] is True


# ── OAuth endpoints of a bundle (``oauth`` block) ─────────────────────

_ENTRA_COMMON = "https://login.microsoftonline.com/common/oauth2/v2.0"


def _oauth_bundle(**overrides) -> bytes:
    metadata = {
        "format": "filigran-integration-v1",
        "version": "1.0.0",
        "name": "OAuth Demo",
        "slug": "oauth-demo",
        "description": "OAuth provider from bundle.",
        "author": "QA",
        "provider": "oauth-demo",
        "auth_type": "oauth",
        "settings": [
            {"key": "base_url", "label": "URL", "type": "string", "required": True},
            {"key": "tenant_id", "label": "Tenant ID", "type": "string", "required": True},
        ],
        "supports_polling": False,
        "supports_webhook": False,
        "oauth": {
            "authorize_url": f"{_ENTRA_COMMON}/authorize",
            "token_url": f"{_ENTRA_COMMON}/token",
            "scopes": "openid offline_access User.Read",
        },
        "exported_at": "2026-10-02T00:00:00Z",
    }
    metadata.update(overrides)
    return build_integration_bundle_zip(metadata)


def _create_oauth_integration(client, db, tenant_id: str = "tenant-1"):
    imported = client.post(
        "/api/v1/integrations/admin/import",
        files={"bundle": ("oauth.zip", _oauth_bundle(), "application/zip")},
    )
    assert imported.status_code == 200, imported.text
    created = client.post(
        "/api/v1/integrations",
        json={
            "provider": "custom:oauth-demo",
            "name": "OAuth Demo",
            "auth_type": "oauth",
            "client_id": "entra-client",
            "client_secret": "entra-secret",
            "extra_config": {"base_url": "https://svc.example", "tenant_id": tenant_id},
        },
    )
    assert created.status_code in (200, 201), created.text
    return db.integrations[created.json()["id"]]


def test_an_integration_created_from_an_oauth_bundle_takes_its_endpoints(bundles_client):
    client, db = bundles_client

    integration = _create_oauth_integration(client, db)

    assert integration.authorize_url == f"{_ENTRA_COMMON}/authorize"
    assert integration.token_url == f"{_ENTRA_COMMON}/token"
    assert integration.scopes == "openid offline_access User.Read"


async def test_the_tenant_setting_goes_into_the_bundle_s_common_urls(bundles_client):
    from app.integrations.oauth import _resolve_tenant_urls

    client, db = bundles_client
    integration = _create_oauth_integration(client, db, tenant_id="0000-tenant/../x")

    authorize_url, token_url = await _resolve_tenant_urls(integration, integration.authorize_url, integration.token_url)

    # The tenant is one percent-encoded path segment, whatever it spells.
    tenant = "https://login.microsoftonline.com/0000-tenant%2F..%2Fx/oauth2/v2.0"
    assert (authorize_url, token_url) == (f"{tenant}/authorize", f"{tenant}/token")


async def test_a_native_provider_outside_entra_keeps_its_common_urls():
    from app.integrations.oauth import _resolve_tenant_urls

    integration = SimpleNamespace(
        provider="github",
        extra_config_enc=encrypt_value(json.dumps({"tenant_id": "t"})),
    )

    assert await _resolve_tenant_urls(integration, "https://x/common/a", "https://x/common/t") == (
        "https://x/common/a",
        "https://x/common/t",
    )


def test_a_catalog_entry_exposes_only_a_valid_oauth_block_of_an_oauth_bundle():
    def _row(auth_type: str, oauth: object) -> CustomIntegrationDefinition:
        return CustomIntegrationDefinition(
            provider_key="custom:oauth-demo",
            name="OAuth Demo",
            description="d",
            auth_type=auth_type,
            settings_schema=[],
            metadata_json={"oauth": oauth},
            supports_polling=False,
            supports_webhook=False,
        )

    valid = {"authorize_url": f"{_ENTRA_COMMON}/authorize", "token_url": f"{_ENTRA_COMMON}/token"}
    entry = integrations_api._custom_row_to_catalog_entry(_row("oauth", valid))
    assert (entry["authorize_url"], entry["token_url"], "scopes" in entry) == (
        valid["authorize_url"],
        valid["token_url"],
        False,
    )

    for row in (_row("api_key", valid), _row("oauth", {**valid, "token_url": "ftp://x"})):
        entry = integrations_api._custom_row_to_catalog_entry(row)
        assert "authorize_url" not in entry
        assert "token_url" not in entry
