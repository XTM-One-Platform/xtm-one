"""Relays are on by default, and ``RELAYS_ENABLED=false`` turns them off.

``RELAYS_ENABLED`` is the one switch: the ``RELAYS`` feature flag that turned
relays on before general availability is gone, and a leftover
``ENABLED_FEATURES=RELAYS`` enables nothing.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings
from app.core.feature_flags import FEATURE_FLAG_ALL, KNOWN_FEATURE_FLAGS
from app.services import relay_transport, relays


def test_relays_are_on_by_default(monkeypatch):
    monkeypatch.delenv("RELAYS_ENABLED", raising=False)
    assert Settings(_env_file=None).relays_enabled is True


def test_relays_enabled_false_turns_them_off(monkeypatch):
    monkeypatch.setenv("RELAYS_ENABLED", "false")
    assert Settings(_env_file=None).relays_enabled is False


def test_relays_active_follows_the_setting_alone(monkeypatch):
    monkeypatch.setattr(relays.settings, "relays_enabled", True)
    assert relays.relays_active() is True
    monkeypatch.setattr(relays.settings, "relays_enabled", False)
    # Every feature flag on changes nothing: no flag turns relays on.
    monkeypatch.setattr(relays.settings, "enabled_features", FEATURE_FLAG_ALL)
    assert relays.relays_active() is False


def test_a_leftover_relays_flag_is_ignored_and_reported_once(monkeypatch, caplog):
    monkeypatch.delenv("RELAYS_ENABLED", raising=False)
    assert "RELAYS" not in KNOWN_FEATURE_FLAGS
    with caplog.at_level(logging.WARNING):
        settings = Settings(_env_file=None, enabled_features="RELAYS")
    assert settings.enabled_features == ""
    assert settings.enabled_feature_flags == frozenset()
    warnings = [r.getMessage() for r in caplog.records if "unknown flag id" in r.getMessage()]
    assert len(warnings) == 1
    assert "RELAYS" in warnings[0]
    # Harmless both ways: it neither turns relays off nor keeps them on.
    assert settings.relays_enabled is True


@pytest.mark.parametrize("enabled", [True, False])
async def test_the_login_page_config_reports_the_switch(monkeypatch, enabled):
    from app.api.v1 import auth

    cfg = SimpleNamespace(
        local_auth_enabled=True,
        registration_enabled=False,
        allowed_domain_list=[],
        oidc_enabled=False,
        oidc_display_name="",
        saml_enabled=False,
        saml_display_name="",
    )

    async def _get_sso_config(_db):
        return cfg

    monkeypatch.setattr("app.core.sso_helpers.get_sso_config", _get_sso_config)
    monkeypatch.setattr(relays.settings, "relays_enabled", enabled)
    assert (await auth.auth_config(db=None)).relays_enabled is enabled


def test_the_config_contract_defaults_to_on():
    from app.api.v1.auth import AuthConfigResponse

    assert AuthConfigResponse.model_fields["relays_enabled"].default is True


def _routed_request(monkeypatch, *, active: bool) -> tuple[relay_transport.RelayTransport, list[dict]]:
    logged: list[dict] = []
    monkeypatch.setattr(relays, "relays_active", lambda: active)
    monkeypatch.setattr(relays, "log_relay_request", logged.append)
    return relay_transport.RelayTransport(group_id=uuid.uuid4(), group_name="Paris"), logged


async def test_a_routed_request_is_refused_at_once_while_relays_are_off(monkeypatch):
    transport, logged = _routed_request(monkeypatch, active=False)

    async def _never(*args, **kwargs):
        raise AssertionError("nothing is looked up or queued while relays are off")

    monkeypatch.setattr(relay_transport, "online_connection_count", _never)
    monkeypatch.setattr(relay_transport, "open_relay_stream", _never)
    with pytest.raises(relay_transport.RelayConnectError) as exc_info:
        await transport.handle_async_request(httpx.Request("GET", "https://splunk.corp.local:8089/"))
    assert exc_info.value.code == "no_relay_online"
    assert "RELAYS_ENABLED=false" in str(exc_info.value)
    assert [entry["error_code"] for entry in logged] == ["no_relay_online"]


async def test_a_routed_request_goes_on_to_the_group_while_relays_are_on(monkeypatch):
    transport, _logged = _routed_request(monkeypatch, active=True)
    looked_up: list[object] = []

    async def _no_relay(group_id):
        looked_up.append(group_id)
        return 0

    monkeypatch.setattr(relay_transport, "online_connection_count", _no_relay)
    with pytest.raises(relay_transport.RelayConnectError) as exc_info:
        await transport.handle_async_request(httpx.Request("GET", "https://splunk.corp.local:8089/"))
    assert looked_up == [transport.group_id]
    assert "RELAYS_ENABLED" not in str(exc_info.value)
