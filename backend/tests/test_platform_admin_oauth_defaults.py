"""The OAuth defaults an administrator's agent lists, stores and deletes (#8494).

``list_oauth_defaults``, ``upsert_oauth_default`` and ``delete_oauth_default``
call the REST handlers of ``/api/v1/settings/oauth-defaults``: a default the
tool stores is the one ``create_integration`` puts an OAuth row on
(``integration_state.admin_oauth_app``, the path of #8480). Administrators
only; no answer carries a client id, a secret or a secret extra-config value.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.security import encrypt_value
from app.models.oauth_default import OAuthDefault

CLIENT_ID = "defender-admin-client-id-8494"
CLIENT_SECRET = "defender-admin-client-secret-8494"
TENANT_SECRET = "tenant-signing-secret-8494"


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
    """An in-memory OAuthDefault table behind ``async with async_session()``."""

    def __init__(self):
        self.rows: dict[str, OAuthDefault] = {}
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def add(self, obj):
        now = datetime.now(UTC)
        obj.id = obj.id or uuid.uuid4()
        obj.created_at = obj.created_at or now
        obj.updated_at = now
        self.rows[obj.provider] = obj

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1

    async def delete(self, obj):
        self.rows.pop(obj.provider, None)

    async def execute(self, stmt):
        params = stmt.compile().params
        wanted = [value for key, value in params.items() if key.startswith("provider")]
        if wanted:
            return _Result([self.rows[p] for p in wanted if p in self.rows])
        return _Result(list(self.rows.values()))


@pytest.fixture
def tools(monkeypatch):
    from app.api.v1 import settings as settings_api
    from app.services.builtin_tools import platform_admin

    db = _Db()
    audits: list[dict] = []
    state = {"admin": True}

    async def _admin(_db, _user_id):
        return SimpleNamespace(id=uuid.uuid4(), is_admin=True, is_active=True) if state["admin"] else None

    async def _audit(_db, **kwargs):
        audits.append(kwargs)

    monkeypatch.setattr("app.core.database.async_session", lambda: db)
    monkeypatch.setattr(platform_admin, "_admin", _admin)
    monkeypatch.setattr(settings_api, "log_audit", _audit)
    _defs, handlers = platform_admin.build_platform_admin_tools("00000000-0000-4000-8000-000000000001")

    def call(name, **args):
        return asyncio.run(handlers[name](**args))

    return SimpleNamespace(call=call, db=db, audits=audits, state=state)


def _no_secret_in(answer: str) -> None:
    for value in (CLIENT_ID, CLIENT_SECRET, TENANT_SECRET):
        assert value not in answer


def test_the_tools_are_served_with_the_suite_and_described():
    from app.services.builtin_tools import platform_admin

    defs, handlers = platform_admin.build_platform_admin_tools("")
    by_name = {d.name: d for d in defs}
    for name in ("list_oauth_defaults", "upsert_oauth_default", "delete_oauth_default"):
        assert name in by_name and name in handlers
        assert "Administrators only" in by_name[name].description
    assert by_name["upsert_oauth_default"].input_schema["required"] == ["provider", "client_id", "client_secret"]
    assert by_name["delete_oauth_default"].input_schema["required"] == ["provider"]


def test_a_non_admin_is_refused_naming_the_permission_and_nothing_is_written(tools):
    tools.state["admin"] = False
    for name, args in (
        ("list_oauth_defaults", {}),
        ("upsert_oauth_default", {"provider": "defender", "client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}),
        ("delete_oauth_default", {"provider": "defender"}),
    ):
        answer = tools.call(name, **args)
        assert answer.startswith("Error 403") and "administrator" in answer and "OAuth defaults" in answer, name
    assert tools.db.rows == {} and tools.audits == [] and tools.db.commits == 0


def test_an_admin_stores_a_default_the_integration_create_path_applies(tools):
    from app.services.integration_state import admin_oauth_app

    answer = tools.call(
        "upsert_oauth_default",
        provider="defender",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        extra_config={"tenant_id": "contoso", "signing_secret": TENANT_SECRET},
    )
    _no_secret_in(answer)
    saved = json.loads(answer)["saved"]
    assert saved["provider"] == "defender"
    assert saved["has_client_id"] is True and saved["has_client_secret"] is True and saved["has_extra_config"] is True
    assert saved["stored_secret_keys"] == ["signing_secret"]
    assert tools.audits and tools.audits[0]["entity_type"] == "oauth_default" and tools.db.commits == 1

    # The row create_integration(provider=defender, auth_type=oauth) is put on.
    app = asyncio.run(admin_oauth_app(tools.db, "defender"))
    assert app is not None and app.client_id == CLIENT_ID and app.client_secret == CLIENT_SECRET
    assert app.extra_config == {"tenant_id": "contoso", "signing_secret": TENANT_SECRET}


def test_the_list_answers_booleans_never_a_value(tools):
    tools.call("upsert_oauth_default", provider="defender", client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    answer = tools.call("list_oauth_defaults")
    _no_secret_in(answer)
    (row,) = json.loads(answer)["oauth_defaults"]
    assert row["provider"] == "defender" and row["has_client_secret"] is True and row["has_extra_config"] is False


def test_a_masked_secret_extra_key_keeps_its_stored_value(tools):
    from app.api.v1.integrations import EXTRA_CONFIG_MASK
    from app.services.integration_state import admin_oauth_app

    tools.call(
        "upsert_oauth_default",
        provider="defender",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        extra_config={"signing_secret": TENANT_SECRET},
    )
    tools.call(
        "upsert_oauth_default",
        provider="defender",
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        extra_config={"signing_secret": EXTRA_CONFIG_MASK, "tenant_id": "fabrikam"},
    )
    app = asyncio.run(admin_oauth_app(tools.db, "defender"))
    assert app.extra_config == {"signing_secret": TENANT_SECRET, "tenant_id": "fabrikam"}


def test_delete_removes_the_default_and_a_missing_one_is_a_404(tools):
    tools.db.add(
        OAuthDefault(provider="defender", client_id_enc=encrypt_value(CLIENT_ID), client_secret_enc=encrypt_value(CLIENT_SECRET))
    )
    assert json.loads(tools.call("delete_oauth_default", provider="defender")) == {"deleted": "defender"}
    assert tools.db.rows == {} and tools.audits[-1]["action"] == "delete"
    assert tools.call("delete_oauth_default", provider="defender").startswith("Error 404")


def test_missing_arguments_are_refused_before_any_write(tools):
    assert tools.call("upsert_oauth_default", provider="defender", client_id=CLIENT_ID).startswith("Error")
    assert tools.call("upsert_oauth_default", client_id=CLIENT_ID, client_secret=CLIENT_SECRET).startswith("Error")
    assert tools.call("delete_oauth_default").startswith("Error: provider is required")
    assert tools.db.rows == {} and tools.audits == []
