"""An XTM Hub API token belongs to the Hub it was made on.

The token acts as its user on that Hub. Whoever may edit the integration -
a ``config`` grantee included - may change its XTM Hub URL without holding
the token, so a new URL, or the switch back to the public catalog (under
which a kept key would still be read), forgets the token unless the same
edit brings one. These tests go through the real ``PUT`` route and the
agent builder's ``update_integration`` as a NON-admin group grantee, with a
minimal in-memory DB stub.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.core.database as database
import app.services.builtin_tools as builtin_tools_module
from app.api import deps
from app.api.v1 import integrations as integrations_api
from app.api.v1 import platform_settings_helpers
from app.api.v1.integration_catalog import PROVIDER_CATALOG
from app.core.database import get_db
from app.core.security import decrypt_value, encrypt_value
from app.integrations.providers.xtm_hub import xtm_hub_default_url
from app.models.integration import Integration
from app.tools.base import ToolCall

_TOKEN = "xhp_" + "Ab3_-" * 8 + "Ab3"
_NEW_TOKEN = "xhp_" + "Zy9-_" * 8 + "Zy9"
_GROUP = str(uuid.uuid4())


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, integration: Integration):
        self.integration = integration

    def add(self, _obj):
        return None

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0].get("entity")
        if entity is Integration:
            return _Result(rows=[self.integration])
        return _Result(rows=[])


def _apply_column_defaults(obj: Integration) -> None:
    for column in Integration.__table__.columns:
        default = column.default
        if default is None or getattr(obj, column.key) is not None:
            continue
        if default.is_scalar:
            setattr(obj, column.key, default.arg)
        elif default.is_callable:
            setattr(obj, column.key, default.arg(None))


def _hub_integration(
    *, base_url: str | None, auth_type: str = "api_key", provider: str = "xtm_hub"
) -> Integration:
    now = datetime.now(UTC)
    integration = Integration(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),  # someone else's
        provider=provider,
        auth_type=auth_type,
        name="XTM Hub",
        is_company_managed=False,
        is_builtin=False,
        shared_group_ids=[_GROUP],
        shared_group_permissions={_GROUP: "config"},
        tags=[],
        status="connected",
        api_key_enc=encrypt_value(_TOKEN),
        extra_config_enc=(
            encrypt_value(json.dumps({"base_url": base_url})) if base_url is not None else None
        ),
        created_at=now,
        updated_at=now,
    )
    _apply_column_defaults(integration)
    return integration


@pytest.fixture
def grantee():
    """A non-admin who may configure the integration through a group grant."""
    return SimpleNamespace(id=uuid.uuid4(), full_name="Grantee", role="user", is_admin=False)


def _client(monkeypatch, fake_db: _FakeDb, user, audits: list[dict]) -> TestClient:
    async def _get_db():
        yield fake_db

    async def _noop(*_a, **_k):
        return None

    async def _record_audit(*_a, **kwargs):
        audits.append(kwargs.get("details") or {})

    async def _groups(*_a, **_k):
        return [_GROUP]

    async def _platform_settings(*_a, **_k):
        return SimpleNamespace(disabled_integration_providers=[])

    monkeypatch.setattr(integrations_api, "log_audit", _record_audit)
    monkeypatch.setattr(integrations_api, "dispatch_on_connect", _noop)
    monkeypatch.setattr(integrations_api, "apply_user_share_fields", _noop)
    monkeypatch.setattr(integrations_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(platform_settings_helpers, "get_platform_settings", _platform_settings)

    app = FastAPI()
    app.include_router(integrations_api.router, prefix="/api/v1/integrations")
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[integrations_api.require_agentic_ai_licensed] = lambda: None
    return TestClient(app)


def _put(monkeypatch, grantee, integration: Integration, body: dict) -> list[dict]:
    audits: list[dict] = []
    with _client(monkeypatch, _FakeDb(integration), grantee, audits) as client:
        resp = client.put(f"/api/v1/integrations/{integration.id}", json=body)
    assert resp.status_code == 200, resp.text
    return audits


@pytest.mark.parametrize(
    ("stored", "new_url"),
    [
        ("https://hub.example.com", "https://attacker.example.com"),
        (None, "https://attacker.example.com"),
        ("https://hub.example.com", "https://hub.example.com/elsewhere"),
        ("https://hub.example.com", ""),
    ],
    ids=["another-host", "from-the-default", "another-path", "back-to-the-default"],
)
def test_pointing_the_integration_at_another_hub_forgets_the_token(
    monkeypatch, grantee, stored, new_url
):
    integration = _hub_integration(base_url=stored)
    audits = _put(monkeypatch, grantee, integration, {"extra_config": {"base_url": new_url}})
    assert integration.api_key_enc is None
    assert integration.status == "not_connected"
    assert audits[-1]["api_key_forgotten"] is True


@pytest.mark.parametrize(
    ("stored", "new_url"),
    [
        ("https://hub.example.com", "https://hub.example.com/"),
        ("https://hub.example.com", " https://hub.example.com "),
        (None, "https://hub.filigran.io"),
        ("", ""),
    ],
    ids=["trailing-slash", "spaces", "default-spelled-out", "default-kept"],
)
def test_the_same_hub_keeps_the_token(monkeypatch, grantee, stored, new_url):
    integration = _hub_integration(base_url=stored)
    audits = _put(monkeypatch, grantee, integration, {"extra_config": {"base_url": new_url}})
    assert decrypt_value(integration.api_key_enc) == _TOKEN
    assert integration.status == "connected"
    assert "api_key_forgotten" not in audits[-1]


def test_a_new_hub_with_its_own_token_keeps_the_new_token(monkeypatch, grantee):
    integration = _hub_integration(base_url="https://hub.example.com")
    _put(
        monkeypatch,
        grantee,
        integration,
        {"extra_config": {"base_url": "https://other-hub.example.com"}, "api_key": _NEW_TOKEN},
    )
    assert decrypt_value(integration.api_key_enc) == _NEW_TOKEN


def test_switching_to_the_public_catalog_forgets_the_token(monkeypatch, grantee):
    integration = _hub_integration(base_url="https://hub.example.com")
    audits = _put(monkeypatch, grantee, integration, {"auth_type": "none"})
    assert integration.api_key_enc is None
    assert audits[-1]["api_key_forgotten"] is True


def test_an_unrelated_edit_keeps_the_token(monkeypatch, grantee):
    integration = _hub_integration(base_url="https://hub.example.com")
    audits = _put(monkeypatch, grantee, integration, {"name": "Renamed"})
    assert decrypt_value(integration.api_key_enc) == _TOKEN
    assert "api_key_forgotten" not in audits[-1]


def test_another_provider_keeps_its_key_when_its_url_changes(monkeypatch, grantee):
    integration = _hub_integration(base_url="https://grafana.example.com")
    # Not OpenCTI: a registered platform's base URL is its registration's, and
    # the API refuses to change it, which is not what this test is about.
    integration.provider = "grafana"
    _put(monkeypatch, grantee, integration, {"extra_config": {"base_url": "https://other.example.com"}})
    assert decrypt_value(integration.api_key_enc) == _TOKEN


class _BuilderSession:
    def __init__(self):
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None


async def _builder_update(monkeypatch, grantee, integration: Integration, **arguments) -> str:
    """The agent builder's ``update_integration``, called by the grantee."""

    async def _manageable(*_args, **_kwargs):
        return integration, None

    async def _caller(*_args, **_kwargs):
        return grantee

    session = _BuilderSession()
    monkeypatch.setattr(builtin_tools_module, "_load_manageable_entity_for_builder", _manageable)
    monkeypatch.setattr(builtin_tools_module, "_load_caller_user_for_builder", _caller)
    monkeypatch.setattr(database, "async_session", lambda: session)
    _, executor = builtin_tools_module.build_agent_builder_tool(SimpleNamespace(), str(grantee.id))
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="update_integration",
            arguments={"integration_id": str(integration.id), **arguments},
        )
    )
    assert not result.is_error, result.content
    assert session.commits == 1
    return result.content


@pytest.mark.parametrize(
    "arguments",
    [
        {"extra_config": json.dumps({"base_url": "https://attacker.example.com"})},
        {"auth_type": "none"},
    ],
    ids=["another-hub", "public-catalog"],
)
@pytest.mark.asyncio
async def test_the_agent_builder_forgets_the_token_the_same_way(monkeypatch, grantee, arguments):
    integration = _hub_integration(base_url="https://hub.example.com")
    content = await _builder_update(monkeypatch, grantee, integration, **arguments)
    assert integration.api_key_enc is None
    assert "The API key was removed" in content
    assert _TOKEN not in content


@pytest.mark.asyncio
async def test_the_agent_builder_keeps_the_token_on_the_same_hub(monkeypatch, grantee):
    integration = _hub_integration(base_url="https://hub.example.com")
    content = await _builder_update(
        monkeypatch,
        grantee,
        integration,
        extra_config=json.dumps({"base_url": "https://hub.example.com/"}),
    )
    assert decrypt_value(integration.api_key_enc) == _TOKEN
    assert "removed" not in content


@pytest.mark.asyncio
async def test_the_agent_builder_keeps_a_token_given_with_the_new_hub(monkeypatch, grantee):
    integration = _hub_integration(base_url="https://hub.example.com")
    await _builder_update(
        monkeypatch,
        grantee,
        integration,
        extra_config=json.dumps({"base_url": "https://other-hub.example.com"}),
        api_key=_NEW_TOKEN,
    )
    assert decrypt_value(integration.api_key_enc) == _NEW_TOKEN


def test_the_declared_default_hub_is_the_provider_default():
    # The catalog declares the binding; an empty URL means the public Hub,
    # so spelling it out is the same Hub.
    assert PROVIDER_CATALOG["xtm_hub"]["api_key_bound_to"] == {
        "field": "base_url",
        "default": xtm_hub_default_url(),
    }


def test_a_provider_declaring_no_binding_keeps_its_key_on_a_new_url(monkeypatch, grantee):
    # The rule is the catalog's declaration, not a provider name: an API key
    # provider without ``api_key_bound_to`` keeps its key when its URL moves.
    assert "api_key_bound_to" not in PROVIDER_CATALOG["misp"]
    integration = _hub_integration(base_url="https://misp.example.com", provider="misp")
    audits = _put(
        monkeypatch, grantee, integration, {"extra_config": {"base_url": "https://other.example.com"}}
    )
    assert decrypt_value(integration.api_key_enc) == _TOKEN
    assert integration.status == "connected"
    assert "api_key_forgotten" not in audits[-1]
