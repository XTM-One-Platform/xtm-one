"""OAuth app credentials an administrator's agent prepares with no browser (#8529).

``create_integration_app_secret`` mints a client secret on an app registration
through the provider's registered minter (here the Entra one, over Microsoft
Graph) with the platform's app-only credential and stores it on its target;
``apply_oauth_default_to_integration`` puts an OAuth row on its provider's
admin OAuth default. No answer, audit detail or log carries a secret value;
every answer names the platform written; non-admins write nothing.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import pytest

from app.core.security import decrypt_value, encrypt_value
from app.models.integration import Integration
from app.models.oauth_default import OAuthDefault

APP_ID = "11111111-2222-4333-8444-555555555555"
OBJECT_ID = "66666666-7777-4888-8999-000000000000"
KEY_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
NEW_SECRET = "minted-secret-value-8529"
CRED_SECRET = "credential-secret-8529"
PLATFORM = "https://platform.example.test"


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def scalar_one_or_none(self):
        return self.rows[0] if self.rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


class _Db:
    """Integrations and OAuth defaults behind ``async with async_session()``."""

    def __init__(self):
        self.integrations: list[Integration] = []
        self.defaults: dict[str, OAuthDefault] = {}
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def add(self, obj):
        if isinstance(obj, OAuthDefault):
            obj.id = obj.id or uuid.uuid4()
            self.defaults[obj.provider] = obj

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0].get("entity")
        params = stmt.compile().params
        if entity is OAuthDefault:
            wanted = [v for k, v in params.items() if k.startswith("provider")]
            return _Result([self.defaults[p] for p in wanted if p in self.defaults])
        rows = list(self.integrations)
        ids = [v for k, v in params.items() if k.startswith("id")]
        if ids:
            rows = [r for r in rows if r.id in ids]
        providers = [v for k, v in params.items() if k.startswith("provider")]
        if providers:
            rows = [r for r in rows if r.provider in providers and r.auth_type != "oauth"]
        return _Result(rows)


def _integration(provider, auth_type, **kwargs):
    row = Integration(provider=provider, auth_type=auth_type, name=kwargs.pop("name", provider))
    row.id = uuid.uuid4()
    row.status = kwargs.pop("status", "disconnected")
    row.is_company_managed = False
    row.uses_admin_oauth_credentials = None
    for key, value in kwargs.items():
        setattr(row, key, value)
    return row


class _Graph:
    """Microsoft Graph's applications endpoints, recorded."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.status = {"get": 200, "addPassword": 200, "removePassword": 204}

    async def request(self, method, path, action, **kwargs):
        self.calls.append((method, path, kwargs))
        key = "get" if method == "get" else path.rsplit("/", 1)[-1]
        status = self.status[key]
        if status >= 400:
            return SimpleNamespace(status_code=status), f"Error: {status}"
        if key == "get":
            body = {"id": OBJECT_ID, "appId": APP_ID, "displayName": "Example app"}
        elif key == "addPassword":
            body = {"keyId": KEY_ID, "secretText": NEW_SECRET, "endDateTime": "2027-05-01T00:00:00Z", "displayName": "XTM One"}
        else:
            body = {}
        return SimpleNamespace(status_code=status, json=lambda: body), None


@pytest.fixture
def env(monkeypatch):
    from app.integrations.providers.entra import app_credentials
    from app.services.builtin_tools import platform_admin_credentials as mod

    db = _Db()
    graph = _Graph()
    audits: list[dict] = []
    state = {"admin": True}
    tokens: list[str] = []

    async def _admin(_db, _uid):
        return SimpleNamespace(id=uuid.uuid4(), email="admin@example.test") if state["admin"] else None

    async def _audit(_db, **kwargs):
        audits.append(kwargs)

    async def _reveal(entity, column):
        value = getattr(entity, column, None)
        return decrypt_value(value) if value else None

    class _Client:
        def __init__(self, token, refresh_fn=None):
            tokens.append(token)

        async def request(self, *a, **k):
            return await graph.request(*a, **k)

    monkeypatch.setattr("app.core.database.async_session", lambda: db)
    monkeypatch.setattr(mod, "_admin", _admin)
    monkeypatch.setattr(mod, "platform_label", lambda: PLATFORM)
    monkeypatch.setattr("app.services.audit.log_audit", _audit)
    monkeypatch.setattr("app.services.vault.reveal_field", _reveal)
    monkeypatch.setattr(app_credentials, "EntraClient", _Client)
    _defs, handlers = mod.build_platform_admin_credential_tools("00000000-0000-4000-8000-000000000001")

    def call(name, **args):
        return asyncio.run(handlers[name](**args))

    return SimpleNamespace(call=call, db=db, graph=graph, audits=audits, state=state, tokens=tokens)


def _credential(env):
    row = _integration(
        "entra",
        "api_key",
        name="Graph credential",
        client_id_enc=encrypt_value("cred-client"),
        client_secret_enc=encrypt_value(CRED_SECRET),
        extra_config_enc=encrypt_value(json.dumps({"tenant_id": "tenant-guid"})),
    )
    env.db.integrations.append(row)
    return row


def _no_secret_in(text: str) -> None:
    assert NEW_SECRET not in text and CRED_SECRET not in text


def test_the_entra_provider_registers_its_minter_through_the_seam():
    import app.integrations.providers.entra  # noqa: F401
    from app.integrations.app_secrets import resolve_minter

    minter, problem = resolve_minter("entra")
    assert problem is None and minter is not None
    assert minter.max_days == 730 and "Application.ReadWrite.All" in minter.missing_credential
    unknown, problem = resolve_minter("nope")
    assert unknown is None and "cannot mint app secrets" in problem and "entra" in problem


def test_the_generic_tool_imports_no_provider():
    from pathlib import Path

    from app.services.builtin_tools import platform_admin_credentials as mod

    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "integrations.providers" not in source


def test_the_tools_are_served_and_described():
    from app.services.builtin_tools import platform_admin_credentials as mod

    defs, handlers = mod.build_platform_admin_credential_tools("")
    by_name = {d.name: d for d in defs}
    for name in ("create_integration_app_secret", "apply_oauth_default_to_integration"):
        assert name in by_name and name in handlers
        assert "Administrators only" in by_name[name].description
        assert "platform" in by_name[name].description
    assert "never returned" in by_name["create_integration_app_secret"].description


def test_a_non_admin_is_refused_and_nothing_is_written(env):
    env.state["admin"] = False
    _credential(env)
    answer = env.call("create_integration_app_secret", app_id=APP_ID, oauth_default_provider="defender")
    assert answer.startswith("Error 403") and "administrator" in answer
    answer = env.call("apply_oauth_default_to_integration", integration_id=str(uuid.uuid4()))
    assert answer.startswith("Error 403")
    assert env.graph.calls == [] and env.db.defaults == {} and env.audits == [] and env.db.commits == 0


def test_a_secret_is_minted_app_only_and_stored_on_the_default_never_answered(env):
    from app.services.integration_state import admin_oauth_app

    _credential(env)
    answer = env.call("create_integration_app_secret", app_id=APP_ID, oauth_default_provider="defender", days=90)
    _no_secret_in(answer)
    data = json.loads(answer)
    assert data["platform"] == PLATFORM
    assert data["secret"]["key_id"] == KEY_ID and data["secret"]["expires_at"].startswith("2027")
    assert data["stored_on"]["oauth_default"]["has_client_secret"] is True
    # The platform's app-only credential, never a user token.
    assert env.tokens and set(env.tokens) == {f"tenant-guid||cred-client||{CRED_SECRET}"}
    assert env.graph.calls[1][1] == f"/applications/{OBJECT_ID}/addPassword"
    app = asyncio.run(admin_oauth_app(env.db, "defender"))
    assert app.client_id == APP_ID and app.client_secret == NEW_SECRET
    assert env.audits and NEW_SECRET not in json.dumps(env.audits, default=str) and env.db.commits == 1


def test_a_secret_stored_on_a_row_and_the_old_one_rotated_out(env):
    _credential(env)
    row = _integration("intune", "oauth")
    env.db.integrations.append(row)
    old_key = "12345678-1234-4234-8234-123456789012"
    answer = env.call(
        "create_integration_app_secret", object_id=OBJECT_ID, integration_id=str(row.id), replace_key_id=old_key
    )
    _no_secret_in(answer)
    data = json.loads(answer)
    assert data["stored_on"]["integration"]["has_client_secret"] is True
    assert data["stored_on"]["integration"]["has_client_id"] is True
    assert data["replaced_key_id"] == old_key
    assert decrypt_value(row.client_secret_enc) == NEW_SECRET and decrypt_value(row.client_id_enc) == APP_ID
    assert env.graph.calls[-1][1].endswith("/removePassword") and env.graph.calls[-1][2]["json"] == {"keyId": old_key}


def test_no_app_only_credential_is_a_sentence_naming_what_is_missing(env):
    answer = env.call("create_integration_app_secret", app_id=APP_ID, oauth_default_provider="defender")
    assert "no app-only Microsoft Entra credential" in answer and "Application.ReadWrite.All" in answer
    assert env.graph.calls == []


def test_a_graph_403_names_the_permission_and_nothing_is_stored(env):
    _credential(env)
    env.graph.status["addPassword"] = 403
    answer = env.call("create_integration_app_secret", app_id=APP_ID, oauth_default_provider="defender")
    assert answer.startswith("Error 403") and "Application.ReadWrite.All" in answer
    assert env.db.defaults == {} and env.db.commits == 0


def test_one_target_exactly_and_ids_checked(env):
    assert "exactly one target" in env.call("create_integration_app_secret", app_id=APP_ID)
    assert "exactly one target" in env.call(
        "create_integration_app_secret",
        app_id=APP_ID,
        oauth_default_provider="defender",
        integration_id=str(uuid.uuid4()),
    )
    assert "app_id or object_id" in env.call("create_integration_app_secret", oauth_default_provider="defender")
    assert "unknown provider" in env.call("create_integration_app_secret", app_id=APP_ID, oauth_default_provider="nope")


def test_apply_default_prepares_a_row_for_one_authorize_click(env):
    env.db.defaults["defender"] = OAuthDefault(
        provider="defender", client_id_enc=encrypt_value(APP_ID), client_secret_enc=encrypt_value(NEW_SECRET)
    )
    row = _integration("defender", "oauth")
    env.db.integrations.append(row)
    answer = env.call("apply_oauth_default_to_integration", integration_id=str(row.id))
    _no_secret_in(answer)
    data = json.loads(answer)
    assert data["platform"] == PLATFORM
    readback = data["integration"]
    assert readback["has_client_id"] is True and readback["has_client_secret"] is True
    assert readback["uses_admin_oauth_credentials"] is True and readback["status"] == "configured"
    assert "Authorize" in data["next_step"] and env.db.commits == 1


def test_apply_default_refuses_a_row_on_another_app_unless_replace(env):
    env.db.defaults["defender"] = OAuthDefault(
        provider="defender", client_id_enc=encrypt_value(APP_ID), client_secret_enc=encrypt_value(NEW_SECRET)
    )
    row = _integration("defender", "oauth", client_id_enc=encrypt_value("someone-else"), status="connected")
    env.db.integrations.append(row)
    assert env.call("apply_oauth_default_to_integration", integration_id=str(row.id)).startswith("Error 409")
    data = json.loads(env.call("apply_oauth_default_to_integration", integration_id=str(row.id), replace=True))
    assert data["integration"]["status"] == "configured" and decrypt_value(row.client_id_enc) == APP_ID


def test_apply_default_without_a_default_or_on_a_non_oauth_row(env):
    row = _integration("defender", "oauth")
    env.db.integrations.append(row)
    answer = env.call("apply_oauth_default_to_integration", integration_id=str(row.id))
    assert answer.startswith("Error 404") and PLATFORM in answer and "upsert_oauth_default" in answer
    key_row = _integration("defender", "api_key")
    env.db.integrations.append(key_row)
    assert "OAuth integration only" in env.call("apply_oauth_default_to_integration", integration_id=str(key_row.id))
