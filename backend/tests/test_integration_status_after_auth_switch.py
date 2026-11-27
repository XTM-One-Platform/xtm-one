"""An integration's status follows the credentials it holds, not its past auth type (#2553).

An integration created with OAuth is ``configured`` ("Pre-configured") until
its first authorization. Switched to an API key with the key saved, it is
``connected``, as one created with the key is; a successful connection test of
its own key heals a status an older write left; OAuth without a token stays
``configured``. The REST cases hit the real router as a NON-admin user with
the in-memory DB stub of ``test_integration_admin_oauth_credentials``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from app.api.v1 import integrations as integrations_api
from app.core.security import encrypt_value
from app.models.integration import Integration
from app.services.integration_state import heal_status_after_test, status_for_auth_type
from tests.test_integration_admin_oauth_credentials import (
    ADMIN_CLIENT_ID,
    MIXED_AUTH_PROVIDER,
    _admin_default,
    _client,
    _existing,
    _FakeDb,
    member,  # noqa: F401 - the fixture
)


def _row(**fields) -> Integration:
    return Integration(provider="github", **fields)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param(
            {"auth_type": "api_key", "api_key_enc": "k", "client_id_enc": "app"}, "connected", id="key_stored"
        ),
        pytest.param({"auth_type": "bot_token", "api_key_enc": "k"}, "connected", id="bot_token_stored"),
        pytest.param({"auth_type": "api_key", "client_id_enc": "app"}, "disconnected", id="oauth_app_is_no_key"),
        pytest.param({"auth_type": "oauth", "client_id_enc": "app"}, "configured", id="oauth_without_token"),
        pytest.param(
            {"auth_type": "oauth", "client_id_enc": "app", "access_token_enc": "t"}, "connected", id="oauth_token"
        ),
        pytest.param({"auth_type": "oauth"}, "disconnected", id="oauth_without_app"),
        pytest.param({"auth_type": "none"}, None, id="none_keeps_its_rule"),
        pytest.param({"auth_type": "platform_managed"}, None, id="platform_managed_keeps_its_rule"),
    ],
)
def test_the_status_an_auth_type_gives_the_stored_credentials(fields, expected):
    assert status_for_auth_type(_row(**fields)) == expected


@pytest.mark.parametrize(
    ("fields", "test_status", "healed"),
    [
        pytest.param({"auth_type": "api_key", "api_key_enc": "k"}, "ok", True, id="key_passed"),
        pytest.param({"auth_type": "api_key", "api_key_enc": "k"}, "info", True, id="key_passed_with_caveat"),
        pytest.param({"auth_type": "api_key", "api_key_enc": "k"}, "error", False, id="key_failed"),
        pytest.param({"auth_type": "api_key"}, "ok", False, id="no_key"),
        pytest.param({"auth_type": "oauth", "client_id_enc": "app"}, "ok", False, id="oauth_reads_its_token"),
    ],
)
def test_a_passed_test_heals_a_typed_key_left_configured(fields, test_status, healed):
    integration = _row(status="configured", **fields)
    assert heal_status_after_test(integration, test_status) is healed
    assert integration.status == ("connected" if healed else "configured")


def _oauth_on_mixed_provider(fake_db, user, **overrides) -> Integration:
    return _existing(fake_db, user, provider=MIXED_AUTH_PROVIDER, uses_admin_oauth_credentials=True, **overrides)


def test_switching_oauth_to_an_api_key_with_the_key_connects_it(monkeypatch, member):  # noqa: F811
    fake_db = _FakeDb(oauth_default=_admin_default(MIXED_AUTH_PROVIDER))
    integration = _oauth_on_mixed_provider(fake_db, member)
    with _client(monkeypatch, fake_db, member) as client:
        resp = client.put(
            f"/api/v1/integrations/{integration.id}",
            json={"auth_type": "api_key", "api_key": "key-2553"},
        )

    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["auth_type"] == "api_key"
    assert payload["status"] == "connected"
    # Stored, not checked yet: the page says "Not tested" until a test runs.
    assert payload["connection_test_status"] == "untested"
    assert integration.status == "connected"


def test_saving_the_key_after_the_switch_connects_it(monkeypatch, member):  # noqa: F811
    """The page's two saves: the method first, the key after."""
    fake_db = _FakeDb(oauth_default=_admin_default(MIXED_AUTH_PROVIDER))
    integration = _oauth_on_mixed_provider(fake_db, member)
    with _client(monkeypatch, fake_db, member) as client:
        switched = client.put(f"/api/v1/integrations/{integration.id}", json={"auth_type": "api_key"})
        assert switched.status_code == 200, switched.text
        # Switched without a key: nothing usable yet, never "Pre-configured".
        assert switched.json()["status"] == "disconnected"
        keyed = client.put(f"/api/v1/integrations/{integration.id}", json={"api_key": "key-2553"})

    assert keyed.status_code == 200, keyed.text
    assert keyed.json()["status"] == "connected"


def test_oauth_without_a_token_stays_pre_configured(monkeypatch, member):  # noqa: F811
    fake_db = _FakeDb(oauth_default=_admin_default(MIXED_AUTH_PROVIDER))
    integration = _oauth_on_mixed_provider(fake_db, member)
    with _client(monkeypatch, fake_db, member) as client:
        resp = client.put(f"/api/v1/integrations/{integration.id}", json={"description": "edited"})

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "configured"


@pytest.mark.parametrize(
    ("verdict", "status", "test_status"),
    [
        pytest.param("ok", "connected", "ok", id="passed"),
        pytest.param("error", "configured", "error", id="failed"),
    ],
)
def test_the_connection_test_heals_a_key_left_pre_configured(
    monkeypatch,
    member,  # noqa: F811
    verdict,
    status,
    test_status,
):
    fake_db = _FakeDb()
    integration = _existing(
        fake_db,
        member,
        provider=MIXED_AUTH_PROVIDER,
        auth_type="api_key",
        status="configured",
        api_key_enc=encrypt_value("key-2553"),
        client_id_enc=encrypt_value(ADMIN_CLIENT_ID),
    )

    async def _holder(_db, row, *_a, **_k):
        return row

    async def _no_route(*_a, **_k):
        return None

    async def _result(*_a, **_k):
        return {"status": verdict, "message": "tested"}

    import app.services.relays as relays

    monkeypatch.setattr(integrations_api, "usable_credential_holder", _holder)
    monkeypatch.setattr(relays, "integration_egress_route", _no_route)
    monkeypatch.setattr(integrations_api, "_test_integration", _result)
    with _client(monkeypatch, fake_db, member) as client:
        resp = client.post(f"/api/v1/integrations/{integration.id}/test")
        assert resp.status_code == 200, resp.text
        shown = client.get(f"/api/v1/integrations/{integration.id}")

    assert integration.status == status
    assert integration.connection_test_status == test_status
    assert shown.status_code == 200, shown.text
    assert shown.json()["status"] == status


def _migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "t2553keystat01_connect_typed_keys_left_configured.py"
    )
    spec = importlib.util.spec_from_file_location("t2553keystat01", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_repairs_only_typed_keys_left_configured():
    rows = _migration().STUCK_TYPED_KEY_ROWS
    assert "auth_type IN ('api_key', 'bot_token')" in rows
    assert "status = 'configured'" in rows
    assert "api_key_enc IS NOT NULL" in rows
    # OAuth rows waiting for their first authorization are never matched.
    assert "'oauth'" not in rows
