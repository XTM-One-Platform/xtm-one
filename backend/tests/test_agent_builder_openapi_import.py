"""The agent builder's ``openapi`` argument on create_integration / update_integration.

An agent creates a working OpenAPI integration in one call: the argument is
the block ``POST /api/v1/integrations`` takes, validated by the same schema
and loaded by the same helpers (``plan_openapi_import``,
``openapi_extra_config``, ``replace_spec``), and ``file_id`` names a
conversation file as the spec source.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app.core.database as database
import app.services.builtin_tools as builtin_tools_module
from app.core.security import decrypt_json
from app.llm.base import FileRef
from app.models.integration import Integration
from app.models.integration_openapi_spec import IntegrationOpenAPISpec
from app.tools.base import FileContext, ToolCall

FIXTURES = Path(__file__).parent / "openapi_fixtures"
PETSTORE_V3 = (FIXTURES / "petstore_v3.yaml").read_text()
INVENTORY_V31 = (FIXTURES / "inventory_v31.yaml").read_text()

build_agent_builder_tool = builtin_tools_module.build_agent_builder_tool


class _Session:
    """An async session double recording what the tool writes."""

    def __init__(self, sessions: list, spec: IntegrationOpenAPISpec | None = None) -> None:
        sessions.append(self)
        self.added: list[Any] = []
        self.commits = 0
        self.spec = spec

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    def add(self, obj: Any) -> None:
        if isinstance(obj, Integration) and obj.id is None:
            obj.id = uuid.uuid4()
        self.added.append(obj)

    async def flush(self) -> None:
        for obj in self.added:
            if isinstance(obj, Integration) and obj.id is None:
                obj.id = uuid.uuid4()

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None

    async def execute(self, _stmt: Any):
        spec = self.spec
        return SimpleNamespace(scalar_one_or_none=lambda: spec, scalar=lambda: spec)


@pytest.fixture
def env(monkeypatch):
    sessions: list[_Session] = []
    state = SimpleNamespace(sessions=sessions, spec=None, disabled=[], rate_limited=[])

    def _factory():
        return _Session(sessions, state.spec)

    monkeypatch.setattr(database, "async_session", _factory)
    caller = SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False, full_name="Owner")

    async def _caller(_db, _uid):
        return caller

    monkeypatch.setattr(builtin_tools_module, "_load_caller_user_for_builder", _caller)

    async def _platform_settings(_db):
        return SimpleNamespace(disabled_integration_providers=list(state.disabled))

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings", _platform_settings
    )

    async def _rate_limit(request, name, **kwargs):
        state.rate_limited.append((request, name, kwargs.get("bucket")))

    monkeypatch.setattr("app.api.v1.integration_openapi.enforce_rate_limit", _rate_limit)

    async def _no_discovery(_document, _url_base):
        return {}

    monkeypatch.setattr("app.services.openapi_import.service.fetch_discovery", _no_discovery)
    state.caller = caller
    return state


async def _call(executor, tool_name, /, **arguments):
    return await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=tool_name, arguments=arguments)
    )


def _created(env) -> tuple[Integration, IntegrationOpenAPISpec]:
    added = [obj for s in env.sessions for obj in s.added]
    integration = next(o for o in added if isinstance(o, Integration))
    spec = next(o for o in added if isinstance(o, IntegrationOpenAPISpec))
    return integration, spec


def test_both_tools_declare_the_openapi_argument():
    tools, _ = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    by_name = {t.name: t for t in tools}
    for name in ("create_integration", "update_integration"):
        props = by_name[name].input_schema["properties"]["openapi"]["properties"]
        assert {"url", "content", "archive", "file_id", "selected_operations", "auth_option",
                "tool_prefix", "tool_overrides", "filename"} <= set(props)


@pytest.mark.asyncio
async def test_create_imports_the_spec_in_one_call(env):
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_integration",
        provider="openapi",
        name="Petstore",
        api_key="pet-key",
        extra_config='{"base_url": "https://petstore3.swagger.io/api/v3/"}',
        openapi={
            "content": PETSTORE_V3,
            "filename": "openapi.yaml",
            "auth_option": "api_key",
            "selected_operations": ["GET /pet/{petId}", "GET /store/inventory"],
            "tool_prefix": "pets_",
        },
    )
    assert not result.is_error, result.content
    assert "created successfully" in result.content
    assert "Tools (2: 2 read, 0 write)" in result.content
    assert "[read] `GET /pet/{petId}`" in result.content
    integration, spec = _created(env)
    assert integration.provider == "openapi"
    assert integration.auth_type == "api_key"
    assert integration.status == "connected"
    assert decrypt_json(integration.extra_config_enc)["base_url"] == "https://petstore3.swagger.io/api/v3"
    assert spec.integration_id == integration.id
    assert spec.selected_operation_keys == ["GET /pet/{petId}", "GET /store/inventory"]
    assert all(t["name"].startswith("pets_") for t in spec.active_tools)
    assert env.sessions[0].commits == 1
    # The route's spec-load rate limit, keyed on the caller.
    assert env.rate_limited and env.rate_limited[0][2] == str(env.caller.id)


@pytest.mark.asyncio
async def test_create_reads_the_spec_from_a_conversation_file(env):
    file_context = FileContext()
    ref = FileRef.from_bytes(PETSTORE_V3.encode(), "petstore.yaml", "application/yaml")
    file_context.add(ref)
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    executor.file_context = file_context
    result = await _call(
        executor,
        "create_integration",
        provider="openapi",
        name="Petstore",
        extra_config='{"base_url": "https://petstore3.swagger.io/api/v3"}',
        openapi={"file_id": ref.file_id, "auth_option": "api_key"},
    )
    assert not result.is_error, result.content
    _, spec = _created(env)
    assert spec.source_type == "file"
    assert spec.source_filename == "petstore.yaml"


@pytest.mark.asyncio
async def test_an_unknown_file_is_refused_before_any_write(env):
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    executor.file_context = FileContext()
    result = await _call(
        executor,
        "create_integration",
        provider="openapi",
        name="Petstore",
        openapi={"file_id": "no-such-file"},
    )
    assert "no file 'no-such-file'" in result.content
    assert not env.sessions


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("openapi", "expected"),
    [
        ({"content": PETSTORE_V3, "selected_operations": ["GET /nope"]}, "not in the spec"),
        ({"content": "not: [a, spec"}, "Integration not created"),
        ({"content": PETSTORE_V3, "url": "https://example.com/openapi.json", "file_id": "x"}, "only one of them"),
        ({"selected_operations": "GET /pet"}, "Invalid openapi"),
        ("{not json", "Invalid openapi"),
    ],
)
async def test_a_bad_block_creates_nothing(env, openapi, expected):
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_integration",
        provider="openapi",
        name="Petstore",
        extra_config='{"base_url": "https://petstore3.swagger.io/api/v3"}',
        openapi=openapi,
    )
    assert expected in result.content
    assert all(s.commits == 0 for s in env.sessions)


@pytest.mark.asyncio
async def test_openapi_needs_its_spec_and_its_provider(env):
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    missing = await _call(executor, "create_integration", provider="openapi", name="X")
    assert "needs its spec" in missing.content
    wrong = await _call(
        executor, "create_integration", provider="github", name="X", openapi={"content": PETSTORE_V3}
    )
    assert "needs provider 'openapi'" in wrong.content
    assert not env.sessions


@pytest.mark.asyncio
async def test_the_admin_switch_closes_the_tool_as_it_closes_the_routes(env):
    env.disabled.append("openapi")
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "create_integration",
        provider="openapi",
        name="Petstore",
        openapi={"content": PETSTORE_V3},
    )
    assert "disabled by an admin" in result.content
    assert all(s.commits == 0 for s in env.sessions)


def _stub_manageable(monkeypatch, integration):
    async def _fake(_db, _model, _uid, _entity_uuid, *, action="modify", entity_label="entity", lock=False):
        return integration, None

    monkeypatch.setattr(builtin_tools_module, "_load_manageable_entity_for_builder", _fake)


@pytest.mark.asyncio
async def test_update_replaces_the_spec(env, monkeypatch):
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    await _call(
        executor,
        "create_integration",
        provider="openapi",
        name="Petstore",
        extra_config='{"base_url": "https://petstore3.swagger.io/api/v3"}',
        openapi={"content": PETSTORE_V3, "auth_option": "api_key", "selected_operations": ["GET /pet/{petId}"]},
    )
    integration, spec = _created(env)
    integration.is_builtin = False
    env.spec = spec
    _stub_manageable(monkeypatch, integration)
    before = len(env.sessions)
    result = await _call(
        executor,
        "update_integration",
        integration_id=str(integration.id),
        openapi={"content": PETSTORE_V3, "auth_option": "api_key", "selected_operations": ["GET /store/inventory"]},
    )
    assert not result.is_error, result.content
    assert "updated: openapi" in result.content
    assert spec.selected_operation_keys == ["GET /store/inventory"]
    # One commit for the update; the agent-account resync after it opens a
    # session of its own that writes nothing.
    assert sum(s.commits for s in env.sessions[before:]) == 1


@pytest.mark.asyncio
async def test_update_refuses_another_credential_type(env, monkeypatch):
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    await _call(
        executor,
        "create_integration",
        provider="openapi",
        name="Petstore",
        extra_config='{"base_url": "https://petstore3.swagger.io/api/v3"}',
        openapi={"content": PETSTORE_V3, "auth_option": "api_key"},
    )
    integration, spec = _created(env)
    integration.is_builtin = False
    env.spec = spec
    _stub_manageable(monkeypatch, integration)
    result = await _call(
        executor,
        "update_integration",
        integration_id=str(integration.id),
        openapi={"content": INVENTORY_V31, "auth_option": "oauth:authorization_code"},
    )
    assert "Integration not updated" in result.content
    assert "different kind of credential" in result.content
    assert env.sessions[-1].commits == 0


@pytest.mark.asyncio
async def test_update_refuses_a_spec_with_an_auth_type_change(env, monkeypatch):
    """The same-credential guard compares the stored type, not one the call sets."""
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    await _call(
        executor,
        "create_integration",
        provider="openapi",
        name="Petstore",
        extra_config='{"base_url": "https://petstore3.swagger.io/api/v3"}',
        openapi={"content": PETSTORE_V3, "auth_option": "api_key"},
    )
    integration, spec = _created(env)
    integration.is_builtin = False
    env.spec = spec
    _stub_manageable(monkeypatch, integration)
    before = list(spec.selected_operation_keys or [])
    result = await _call(
        executor,
        "update_integration",
        integration_id=str(integration.id),
        openapi={"content": PETSTORE_V3, "auth_option": "api_key"},
        auth_type="bearer",
    )
    assert "cannot be combined" in result.content
    assert env.sessions[-1].commits == 0
    assert integration.auth_type == "api_key"
    assert list(spec.selected_operation_keys or []) == before


def _spec_less(extra: dict | None) -> Integration:
    """An OpenAPI integration created before the spec import: a key, no spec row."""
    from app.core.security import encrypt_value

    integration = Integration(
        provider="openapi", auth_type="api_key", name="Legacy", status="connected",
        api_key_enc=encrypt_value("kept-key"),
    )
    integration.id = uuid.uuid4()
    integration.is_builtin = False
    if extra is not None:
        import json

        integration.extra_config_enc = encrypt_value(json.dumps(extra))
    return integration


@pytest.mark.asyncio
async def test_update_attaches_a_first_spec_to_a_spec_less_integration(env, monkeypatch):
    """An OpenAPI integration without a spec row gets one, its key kept."""
    integration = _spec_less({"base_url": "https://petstore3.swagger.io/api/v3"})
    key_before = integration.api_key_enc
    _stub_manageable(monkeypatch, integration)
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_integration",
        integration_id=str(integration.id),
        openapi={"content": PETSTORE_V3, "auth_option": "api_key", "tool_prefix": "pets_"},
    )
    assert not result.is_error, result.content
    assert "updated: openapi" in result.content
    assert "Tools (" in result.content and "none selected" not in result.content
    spec = next(o for s in env.sessions for o in s.added if isinstance(o, IntegrationOpenAPISpec))
    assert spec.integration_id == integration.id
    assert spec.active_tools and all(t["name"].startswith("pets_") for t in spec.active_tools)
    assert integration.api_key_enc == key_before
    assert sum(s.commits for s in env.sessions) == 1


@pytest.mark.asyncio
async def test_a_first_spec_without_any_base_url_is_refused(env, monkeypatch):
    """No stored base URL and a relative server: refused before anything is written."""
    integration = _spec_less(None)
    _stub_manageable(monkeypatch, integration)
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_integration",
        integration_id=str(integration.id),
        openapi={"content": PETSTORE_V3, "auth_option": "api_key"},
    )
    assert "Integration not updated" in result.content
    assert "base URL" in result.content
    assert env.sessions[-1].commits == 0
    assert not any(isinstance(o, IntegrationOpenAPISpec) for o in env.sessions[-1].added)


@pytest.mark.asyncio
async def test_update_refuses_a_spec_for_another_provider(env, monkeypatch):
    integration = SimpleNamespace(
        id=uuid.uuid4(), name="Acme", provider="github", auth_type="api_key",
        is_builtin=False, extra_config_enc=None, authorize_url=None, token_url=None,
    )
    _stub_manageable(monkeypatch, integration)
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(
        executor,
        "update_integration",
        integration_id=str(integration.id),
        openapi={"content": PETSTORE_V3},
    )
    assert "not" in result.content and "OpenAPI integration" in result.content
    assert env.sessions[-1].commits == 0
