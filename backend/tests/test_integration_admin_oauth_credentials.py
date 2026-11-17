"""Where an integration's OAuth app credentials came from.

``create_integration`` copies the admin's OAuth defaults into an integration
created without its own client id; ``uses_admin_oauth_credentials`` records
that the copy happened, so the integration page can tell an integration on
the admin's app (events through the provider-level webhook) from one on the
user's own app (needs its own webhook). A new client id is a new app, so the
tokens the previous one issued go with it, through the REST route and the
agent builder alike (``replace_oauth_client_id``). These tests hit the real
router as a NON-admin user - the admin bypass would hide a visibility
short-circuit - with a minimal in-memory DB stub.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
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
from app.models.integration import Integration
from app.models.oauth_default import OAuthDefault
from app.services.integration_state import replace_oauth_client_id
from app.tools.base import ToolCall

ADMIN_CLIENT_ID = "admin-app-client-id-4234"
ADMIN_CLIENT_SECRET = "admin-app-client-secret-4234"
CURRENT_APP_TOKEN = "token-issued-to-the-current-app"
CURRENT_APP_REFRESH = "refresh-issued-to-the-current-app"


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


def _apply_column_defaults(obj: Integration) -> None:
    """What a real INSERT flush does with the Python-side column defaults."""
    for column in Integration.__table__.columns:
        default = column.default
        if default is None or getattr(obj, column.key) is not None:
            continue
        if default.is_scalar:
            setattr(obj, column.key, default.arg)
        elif default.is_callable:
            setattr(obj, column.key, default.arg(None))


class _FakeDb:
    def __init__(self, *, oauth_default: OAuthDefault | None = None):
        self.integrations: dict[str, Integration] = {}
        self.oauth_default = oauth_default

    def add(self, obj):
        if isinstance(obj, Integration):
            _apply_column_defaults(obj)
            self.integrations[str(obj.id)] = obj

    async def flush(self):
        return None

    async def commit(self):
        return None

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0].get("entity")
        if entity is OAuthDefault:
            return _Result(rows=[self.oauth_default] if self.oauth_default else [])
        if entity is Integration:
            params = stmt.compile().params
            ids = [v for k, v in params.items() if k.split("_")[0] == "id"]
            return _Result(
                rows=[self.integrations[str(v)] for v in ids if str(v) in self.integrations]
            )
        return _Result(rows=[])


# A provider offering both OAuth and an API key, for the cases that switch.
MIXED_AUTH_PROVIDER = "entra"


def _admin_default(provider: str = "github") -> OAuthDefault:
    return OAuthDefault(
        provider=provider,
        client_id_enc=encrypt_value(ADMIN_CLIENT_ID),
        client_secret_enc=encrypt_value(ADMIN_CLIENT_SECRET),
    )


def _client(monkeypatch, fake_db: _FakeDb, user) -> TestClient:
    async def _get_db():
        yield fake_db

    async def _noop(*_a, **_k):
        return None

    async def _no_groups(*_a, **_k):
        return []

    async def _platform_settings(*_a, **_k):
        return SimpleNamespace(disabled_integration_providers=[])

    monkeypatch.setattr(integrations_api, "log_audit", _noop)
    monkeypatch.setattr(integrations_api, "dispatch_on_connect", _noop)
    monkeypatch.setattr(integrations_api, "apply_user_share_fields", _noop)
    monkeypatch.setattr(integrations_api, "get_user_group_ids", _no_groups)
    monkeypatch.setattr(platform_settings_helpers, "get_platform_settings", _platform_settings)

    app = FastAPI()
    app.include_router(integrations_api.router, prefix="/api/v1/integrations")
    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[integrations_api.require_agentic_ai_licensed] = lambda: None
    return TestClient(app)


@pytest.fixture
def member():
    return SimpleNamespace(id=uuid.uuid4(), full_name="Member", role="user", is_admin=False)


def _create(client: TestClient, **body) -> dict:
    resp = client.post("/api/v1/integrations", json={"provider": "github", "name": "GH"} | body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _stored(fake_db: _FakeDb, payload: dict) -> Integration:
    return fake_db.integrations[payload["id"]]


def _existing(fake_db: _FakeDb, user, **overrides) -> Integration:
    now = datetime.now(UTC)
    fields = dict(
        id=uuid.uuid4(),
        user_id=user.id,
        provider="github",
        auth_type="oauth",
        name="GitHub",
        is_company_managed=False,
        is_builtin=False,
        shared_group_ids=None,
        shared_group_permissions=None,
        tags=[],
        status="configured",
        client_id_enc=encrypt_value(ADMIN_CLIENT_ID),
        client_secret_enc=encrypt_value(ADMIN_CLIENT_SECRET),
        oauth_share_acknowledged=False,
        uses_admin_oauth_credentials=None,
        created_at=now,
        updated_at=now,
    )
    fields.update(overrides)
    recorded = fields.pop("uses_admin_oauth_credentials")
    integration = Integration(**fields)
    _apply_column_defaults(integration)
    # Set after the defaults: an unrecorded (legacy) row really is NULL.
    integration.uses_admin_oauth_credentials = recorded
    fake_db.integrations[str(integration.id)] = integration
    return integration


def test_the_providers_used_here_offer_the_auth_types_the_cases_need():
    github = PROVIDER_CATALOG["github"]
    assert "oauth" in (github.get("supported_auth_types") or [github["auth_type"]])
    mixed = set(PROVIDER_CATALOG[MIXED_AUTH_PROVIDER].get("supported_auth_types") or [])
    assert {"oauth", "api_key"} <= mixed


def test_creating_on_the_admin_defaults_records_it_without_serving_the_client_id(monkeypatch, member):
    fake_db = _FakeDb(oauth_default=_admin_default())
    with _client(monkeypatch, fake_db, member) as client:
        payload = _create(client, auth_type="oauth")

    assert payload["uses_admin_oauth_credentials"] is True
    stored = _stored(fake_db, payload)
    assert stored.uses_admin_oauth_credentials is True
    assert decrypt_value(stored.client_id_enc) == ADMIN_CLIENT_ID
    # Only the provenance travels - never the admin's app credentials.
    assert ADMIN_CLIENT_ID not in str(payload)
    assert ADMIN_CLIENT_SECRET not in str(payload)


def test_an_api_created_oauth_integration_is_ready_to_connect_on_the_admin_app(monkeypatch, member):
    """#8386: no client credentials in the request, admin defaults for the
    provider: the integration holds the admin app and waits for its sign-in."""
    fake_db = _FakeDb(oauth_default=_admin_default("microsoft"))
    with _client(monkeypatch, fake_db, member) as client:
        payload = _create(client, provider="microsoft", name="MS")

    assert payload["auth_type"] == "oauth"
    assert payload["has_client_id"] is True
    assert payload["uses_admin_oauth_credentials"] is True
    assert payload["status"] == "configured"
    assert payload.get("has_access_token") is not True


class _BuilderDb(_FakeDb):
    """The same in-memory DB as an ``async with`` session for the agent builder."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def rollback(self):
        return None


@pytest.mark.asyncio
async def test_the_rest_and_agent_builder_creates_land_on_the_same_admin_app(monkeypatch, member):
    """The admin OAuth fallback is one rule: the same request through the REST
    create and the agent builder's create_integration stores the same app,
    status and provenance, so the two paths cannot drift again."""
    rest_db = _FakeDb(oauth_default=_admin_default("microsoft"))
    with _client(monkeypatch, rest_db, member) as client:
        rest_row = _stored(rest_db, _create(client, provider="microsoft", name="MS"))

    builder_db = _BuilderDb(oauth_default=_admin_default("microsoft"))

    async def _caller(*_args, **_kwargs):
        return member

    monkeypatch.setattr(builtin_tools_module, "_load_caller_user_for_builder", _caller)
    monkeypatch.setattr(database, "async_session", lambda: builder_db)
    _, executor = builtin_tools_module.build_agent_builder_tool(SimpleNamespace(), str(member.id))
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="create_integration",
            arguments={"provider": "microsoft", "name": "MS"},
        )
    )
    assert not result.is_error, result.content
    (builder_row,) = builder_db.integrations.values()

    def _outcome(row: Integration):
        return (
            decrypt_value(row.client_id_enc),
            decrypt_value(row.client_secret_enc),
            row.status,
            row.uses_admin_oauth_credentials,
        )

    assert _outcome(builder_row) == _outcome(rest_row)
    assert _outcome(rest_row) == (ADMIN_CLIENT_ID, ADMIN_CLIENT_SECRET, "configured", True)
    assert ADMIN_CLIENT_ID not in result.content
    assert ADMIN_CLIENT_SECRET not in result.content


def test_an_edit_puts_an_oauth_integration_without_an_app_on_the_admin_app(monkeypatch, member):
    """A row the create fallback missed is repaired in place by the REST update."""
    fake_db = _FakeDb(oauth_default=_admin_default("microsoft"))
    integration = _existing(
        fake_db,
        member,
        provider="microsoft",
        status="disconnected",
        client_id_enc=None,
        client_secret_enc=None,
        uses_admin_oauth_credentials=False,
        tool_slug="ms_team",
    )
    with _client(monkeypatch, fake_db, member) as client:
        resp = client.put(f"/api/v1/integrations/{integration.id}", json={"name": "MS"})

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["has_client_id"] is True
    assert payload["uses_admin_oauth_credentials"] is True
    assert payload["status"] == "configured"
    assert decrypt_value(integration.client_id_enc) == ADMIN_CLIENT_ID
    assert integration.tool_slug == "ms_team"
    assert ADMIN_CLIENT_ID not in resp.text
    assert ADMIN_CLIENT_SECRET not in resp.text


def test_an_edit_leaves_an_integration_on_its_own_app_alone(monkeypatch, member):
    fake_db = _FakeDb(oauth_default=_admin_default("microsoft"))
    integration = _existing(
        fake_db,
        member,
        provider="microsoft",
        client_id_enc=encrypt_value("my-own-app"),
        uses_admin_oauth_credentials=False,
    )
    with _client(monkeypatch, fake_db, member) as client:
        resp = client.put(f"/api/v1/integrations/{integration.id}", json={"name": "MS"})

    assert resp.status_code == 200, resp.text
    assert decrypt_value(integration.client_id_enc) == "my-own-app"
    assert resp.json()["uses_admin_oauth_credentials"] is False


@pytest.mark.parametrize("auth_type", ["platform_managed", "api_key", "none"])
def test_creating_with_a_type_the_provider_does_not_support_is_refused(monkeypatch, member, auth_type):
    """#8386: an entry that declares only ``auth_type`` supports exactly it."""
    fake_db = _FakeDb(oauth_default=None)
    with _client(monkeypatch, fake_db, member) as client:
        resp = client.post(
            "/api/v1/integrations",
            json={"provider": "microsoft", "name": "MS", "auth_type": auth_type},
        )

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == (
        f"Auth type '{auth_type}' is not supported for provider 'microsoft'. Supported: oauth"
    )
    assert fake_db.integrations == {}


def test_switching_an_oauth_only_integration_to_platform_managed_is_refused(monkeypatch, member):
    """#8386: the zombie switch - no credential, read as connected - is a 400."""
    fake_db = _FakeDb(oauth_default=None)
    integration = _existing(fake_db, member, provider="microsoft")
    before = (integration.auth_type, integration.status)
    with _client(monkeypatch, fake_db, member) as client:
        resp = client.put(
            f"/api/v1/integrations/{integration.id}", json={"auth_type": "platform_managed"}
        )

    assert resp.status_code == 400, resp.text
    assert "Supported: oauth" in resp.json()["detail"]
    assert (integration.auth_type, integration.status) == before


def test_a_zombie_integration_can_switch_back_to_its_supported_type(monkeypatch, member):
    """#8386: a row left on a type its provider does not support can return."""
    fake_db = _FakeDb(oauth_default=None)
    integration = _existing(fake_db, member, provider="microsoft")
    integration.auth_type = "platform_managed"
    integration.status = "connected"
    with _client(monkeypatch, fake_db, member) as client:
        resp = client.put(f"/api/v1/integrations/{integration.id}", json={"auth_type": "oauth"})

    assert resp.status_code == 200, resp.text
    assert integration.auth_type == "oauth"
    assert resp.json()["status"] != "connected"


def test_the_supported_auth_types_rule():
    from app.api.v1.integration_catalog import supported_auth_types

    assert supported_auth_types(PROVIDER_CATALOG["microsoft"]) == ["oauth"]
    assert supported_auth_types({"auth_type": "api_key", "supported_auth_types": ["api_key", "platform_managed"]}) == [
        "api_key",
        "platform_managed",
    ]
    assert supported_auth_types({}) == []
    assert supported_auth_types(None) == []


def test_every_provider_offering_an_api_key_field_declares_it_as_an_auth_type():
    """#8386 makes the list binding: a provider whose form asks for an API key
    besides its OAuth app must list api_key, or that form would be refused."""
    from app.api.v1.integration_catalog import supported_auth_types

    missing = [
        key
        for key, entry in PROVIDER_CATALOG.items()
        if {"client_id", "api_key"} <= set(entry.get("fields") or [])
        and "api_key" not in supported_auth_types(entry)
    ]
    assert missing == []


def test_creating_with_own_credentials_records_false_even_with_admin_defaults(monkeypatch, member):
    fake_db = _FakeDb(oauth_default=_admin_default())
    with _client(monkeypatch, fake_db, member) as client:
        payload = _create(
            client, auth_type="oauth", client_id="my-own-app", client_secret="my-own-secret"
        )

    assert payload["uses_admin_oauth_credentials"] is False
    stored = _stored(fake_db, payload)
    assert decrypt_value(stored.client_id_enc) == "my-own-app"
    assert stored.uses_admin_oauth_credentials is False


def test_creating_oauth_without_admin_defaults_records_false(monkeypatch, member):
    fake_db = _FakeDb(oauth_default=None)
    with _client(monkeypatch, fake_db, member) as client:
        payload = _create(client, auth_type="oauth")

    assert payload["uses_admin_oauth_credentials"] is False
    assert _stored(fake_db, payload).client_id_enc is None


def test_creating_a_non_oauth_integration_records_false(monkeypatch, member):
    fake_db = _FakeDb(oauth_default=_admin_default(MIXED_AUTH_PROVIDER))
    with _client(monkeypatch, fake_db, member) as client:
        payload = _create(
            client, provider=MIXED_AUTH_PROVIDER, auth_type="api_key", api_key="key-4234"
        )

    assert payload["uses_admin_oauth_credentials"] is False
    assert _stored(fake_db, payload).client_id_enc is None


def test_an_unrelated_edit_leaves_an_unrecorded_integration_unrecorded(monkeypatch, member):
    fake_db = _FakeDb(oauth_default=_admin_default())
    legacy = _existing(fake_db, member, uses_admin_oauth_credentials=None)
    with _client(monkeypatch, fake_db, member) as client:
        resp = client.put(f"/api/v1/integrations/{legacy.id}", json={"name": "Renamed"})

    assert resp.status_code == 200, resp.text
    # Nothing is written for it: the stored value stays NULL. The response
    # reads it off the credentials, which carry the admin's client id.
    assert legacy.uses_admin_oauth_credentials is None
    assert resp.json()["uses_admin_oauth_credentials"] is True


@pytest.mark.parametrize(
    ("client_id", "oauth_default", "expected"),
    [
        (ADMIN_CLIENT_ID, "admin", True),
        ("my-own-app", "admin", False),
        (ADMIN_CLIENT_ID, None, False),
        (None, "admin", False),
    ],
    ids=["on-the-admin-app", "on-its-own-app", "no-admin-default", "no-client-id"],
)
def test_an_unrecorded_integration_is_read_off_its_credentials(
    monkeypatch, member, client_id, oauth_default, expected
):
    """A row the migration left NULL is judged by its stored client id against
    the provider's current OAuth default, so the
    webhooks and configuration cards match where its events actually go, and
    a legacy integration on the user's own app shows its own webhook URL and
    secret field."""
    fake_db = _FakeDb(oauth_default=_admin_default() if oauth_default else None)
    legacy = _existing(
        fake_db,
        member,
        uses_admin_oauth_credentials=None,
        client_id_enc=encrypt_value(client_id) if client_id else None,
    )
    with _client(monkeypatch, fake_db, member) as client:
        resp = client.get(f"/api/v1/integrations/{legacy.id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["uses_admin_oauth_credentials"] is expected
    assert legacy.uses_admin_oauth_credentials is None
    assert ADMIN_CLIENT_ID not in resp.text


def test_an_unreadable_unrecorded_client_id_keeps_the_provider_wide_answer(monkeypatch, member):
    fake_db = _FakeDb(oauth_default=_admin_default())
    legacy = _existing(
        fake_db, member, uses_admin_oauth_credentials=None, client_id_enc="not-a-fernet-token"
    )
    with _client(monkeypatch, fake_db, member) as client:
        resp = client.get(f"/api/v1/integrations/{legacy.id}")

    assert resp.status_code == 200, resp.text
    assert resp.json()["uses_admin_oauth_credentials"] is None


def test_a_recorded_value_is_served_as_recorded(monkeypatch, member):
    """The comparison only fills a NULL: a recorded False on a row that happens
    to carry the admin's client id stays False, and the reverse."""
    fake_db = _FakeDb(oauth_default=_admin_default())
    typed_admin_id = _existing(fake_db, member, uses_admin_oauth_credentials=False)
    rotated = _existing(
        fake_db,
        member,
        uses_admin_oauth_credentials=True,
        client_id_enc=encrypt_value("previous-admin-app"),
    )
    with _client(monkeypatch, fake_db, member) as client:
        first = client.get(f"/api/v1/integrations/{typed_admin_id.id}").json()
        second = client.get(f"/api/v1/integrations/{rotated.id}").json()

    assert first["uses_admin_oauth_credentials"] is False
    assert second["uses_admin_oauth_credentials"] is True


def test_an_unrelated_edit_keeps_the_admin_record(monkeypatch, member):
    fake_db = _FakeDb(oauth_default=_admin_default())
    on_admin_app = _existing(fake_db, member, uses_admin_oauth_credentials=True)
    with _client(monkeypatch, fake_db, member) as client:
        resp = client.put(
            f"/api/v1/integrations/{on_admin_app.id}",
            json={"name": "Renamed", "client_secret": "rotated-but-same-app"},
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["uses_admin_oauth_credentials"] is True


def _connected(fake_db: _FakeDb, user, **overrides) -> Integration:
    """An OAuth integration authorized through the app it holds now."""
    fields = dict(
        status="connected",
        access_token_enc=encrypt_value(CURRENT_APP_TOKEN),
        refresh_token_enc=encrypt_value(CURRENT_APP_REFRESH),
        token_expires_at=datetime.now(UTC),
    )
    fields.update(overrides)
    return _existing(fake_db, user, **fields)


@pytest.mark.parametrize("recorded", [None, True, False])
def test_a_new_client_id_is_a_new_app_to_authorize(monkeypatch, member, recorded):
    fake_db = _FakeDb(oauth_default=_admin_default())
    integration = _connected(fake_db, member, uses_admin_oauth_credentials=recorded)
    with _client(monkeypatch, fake_db, member) as client:
        resp = client.put(
            f"/api/v1/integrations/{integration.id}",
            json={"client_id": "my-own-app", "client_secret": "my-own-secret"},
        )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["uses_admin_oauth_credentials"] is False
    assert decrypt_value(integration.client_id_enc) == "my-own-app"
    # The tokens were issued to the previous app: gone, and the integration
    # waits for an authorization against the new one.
    assert payload["has_access_token"] is False
    assert payload["status"] == "configured"
    assert integration.refresh_token_enc is None
    assert integration.token_expires_at is None


@pytest.mark.parametrize(
    "body",
    [
        {"client_secret": "rotated-secret"},
        {"client_id": ADMIN_CLIENT_ID, "client_secret": "rotated-secret"},
    ],
    ids=["secret-only", "same-client-id"],
)
def test_the_same_app_keeps_its_connection_and_its_record(monkeypatch, member, body):
    fake_db = _FakeDb(oauth_default=_admin_default())
    integration = _connected(fake_db, member, uses_admin_oauth_credentials=True)
    with _client(monkeypatch, fake_db, member) as client:
        resp = client.put(f"/api/v1/integrations/{integration.id}", json=body)

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["uses_admin_oauth_credentials"] is True
    assert payload["status"] == "connected"
    assert payload["has_access_token"] is True
    assert decrypt_value(integration.refresh_token_enc) == CURRENT_APP_REFRESH
    assert decrypt_value(integration.client_secret_enc) == "rotated-secret"


def test_a_new_client_id_leaves_a_non_oauth_connection_alone(monkeypatch, member):
    fake_db = _FakeDb(oauth_default=_admin_default(MIXED_AUTH_PROVIDER))
    integration = _existing(
        fake_db,
        member,
        provider=MIXED_AUTH_PROVIDER,
        auth_type="api_key",
        status="connected",
        api_key_enc=encrypt_value("key-4234"),
        client_id_enc=None,
        client_secret_enc=None,
        uses_admin_oauth_credentials=False,
    )
    with _client(monkeypatch, fake_db, member) as client:
        resp = client.put(
            f"/api/v1/integrations/{integration.id}",
            json={"client_id": "app-registration"},
        )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "connected"
    assert payload["has_api_key"] is True
    assert decrypt_value(integration.client_id_enc) == "app-registration"


def test_leaving_oauth_clears_the_admin_record(monkeypatch, member):
    fake_db = _FakeDb(oauth_default=_admin_default(MIXED_AUTH_PROVIDER))
    integration = _existing(
        fake_db, member, provider=MIXED_AUTH_PROVIDER, uses_admin_oauth_credentials=True
    )
    with _client(monkeypatch, fake_db, member) as client:
        resp = client.put(
            f"/api/v1/integrations/{integration.id}", json={"auth_type": "api_key"}
        )

    assert resp.status_code == 200, resp.text
    assert resp.json()["uses_admin_oauth_credentials"] is False


def test_the_model_records_false_by_default_and_the_column_is_nullable():
    column = Integration.__table__.c.uses_admin_oauth_credentials
    assert column.nullable is True
    assert column.server_default is None
    assert column.default is not None and column.default.arg is False


def test_clearing_the_client_id_leaves_an_oauth_integration_without_an_app():
    integration = Integration(
        provider="github",
        auth_type="oauth",
        status="connected",
        client_id_enc=encrypt_value(ADMIN_CLIENT_ID),
        access_token_enc=encrypt_value(CURRENT_APP_TOKEN),
        uses_admin_oauth_credentials=True,
    )

    assert replace_oauth_client_id(integration, "") is True
    assert integration.client_id_enc is None
    assert integration.access_token_enc is None
    assert integration.status == "disconnected"
    assert integration.uses_admin_oauth_credentials is False


def test_an_unreadable_stored_client_id_is_replaced():
    integration = Integration(
        provider="github",
        auth_type="oauth",
        status="connected",
        client_id_enc="not-a-fernet-token",
        access_token_enc=encrypt_value(CURRENT_APP_TOKEN),
    )

    assert replace_oauth_client_id(integration, "my-own-app") is True
    assert decrypt_value(integration.client_id_enc) == "my-own-app"
    assert integration.access_token_enc is None
    assert integration.status == "configured"


# --- The agent builder's update_integration applies the same rule ---------


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


async def _builder_update(monkeypatch, integration: Integration, **arguments):
    """``update_integration`` called by the integration's owner, a non-admin."""
    owner = SimpleNamespace(id=integration.user_id, role="user", is_admin=False)

    async def _manageable(*_args, **_kwargs):
        return integration, None

    async def _caller(*_args, **_kwargs):
        return owner

    session = _BuilderSession()
    monkeypatch.setattr(
        builtin_tools_module, "_load_manageable_entity_for_builder", _manageable
    )
    monkeypatch.setattr(builtin_tools_module, "_load_caller_user_for_builder", _caller)
    monkeypatch.setattr(database, "async_session", lambda: session)
    _, executor = builtin_tools_module.build_agent_builder_tool(
        SimpleNamespace(), str(owner.id)
    )
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


@pytest.mark.asyncio
async def test_the_agent_builder_asks_for_an_authorization_on_a_new_client_id(
    monkeypatch, member
):
    integration = _connected(_FakeDb(), member, uses_admin_oauth_credentials=True)

    content = await _builder_update(monkeypatch, integration, client_id="my-own-app")

    assert "authorize the integration again" in content
    assert "my-own-app" not in content
    assert integration.uses_admin_oauth_credentials is False
    assert decrypt_value(integration.client_id_enc) == "my-own-app"
    assert integration.access_token_enc is None
    assert integration.refresh_token_enc is None
    assert integration.status == "configured"


@pytest.mark.asyncio
async def test_the_agent_builder_drops_the_personal_token_when_leaving_oauth(
    monkeypatch, member
):
    integration = _connected(
        _FakeDb(),
        member,
        provider=MIXED_AUTH_PROVIDER,
        uses_admin_oauth_credentials=True,
        oauth_share_acknowledged=True,
    )

    await _builder_update(monkeypatch, integration, auth_type="api_key")

    assert integration.auth_type == "api_key"
    assert integration.access_token_enc is None
    assert integration.refresh_token_enc is None
    assert integration.token_expires_at is None
    assert integration.oauth_share_acknowledged is False
    assert integration.uses_admin_oauth_credentials is False


@pytest.mark.asyncio
async def test_the_agent_builder_keeps_the_connection_for_the_same_client_id(
    monkeypatch, member
):
    integration = _connected(_FakeDb(), member, uses_admin_oauth_credentials=True)

    content = await _builder_update(monkeypatch, integration, client_id=ADMIN_CLIENT_ID)

    assert "authorize" not in content
    assert integration.uses_admin_oauth_credentials is True
    assert integration.status == "connected"
    assert decrypt_value(integration.access_token_enc) == CURRENT_APP_TOKEN
