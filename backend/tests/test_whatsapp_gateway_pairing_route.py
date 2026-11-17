"""The WhatsApp gateway Pairing card's route, ``GET /integrations/{id}/whatsapp/pairing``.

The card reads the gateway through the client the agent tools use, so the state it shows is
the state ``get_whatsapp_pairing_status`` answers. The QR and the webhook path with its secret
are for a caller who may manage the integration only.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute

from app.api.v1 import whatsapp_gateway_pairing as route
from app.integrations.providers.whatsapp_gateway import WhapiClient

_USER = SimpleNamespace(id=uuid.uuid4(), role="user", is_admin=False)
SECRET = "s3cret-value"


def _integration(provider: str = "whatsapp") -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), provider=provider, extra_config_enc=b"x", relay_group_id=None)


class _FakeGateway(WhapiClient):
    health_answer: tuple = ({"status": {"text": "AUTH"}}, None)
    qr_answer: tuple = ({"base64": "iVBORw0KGgo="}, None)
    qr_calls = 0

    async def health(self):
        return self.health_answer

    async def login_qr(self):
        type(self).qr_calls += 1
        return self.qr_answer


@pytest.fixture
def opened(monkeypatch):
    """Open *integration* with *extra* config; *manages* is what ``can_manage`` answers."""

    def _open(integration, extra, *, manages: bool, channel: str = "channel-value") -> type[_FakeGateway]:
        async def _group_ids(db, user_id):
            return []

        async def _visible(db, user_id, integration_id, group_ids=None, *, is_admin=False, lock=False):
            return integration

        async def _reveal(entity, column):
            return channel

        async def _route(db, integ):
            return None

        async def _reveal_json(entity, column, *, without=()):
            return extra

        gateway = type("Gateway", (_FakeGateway,), {"qr_calls": 0})
        monkeypatch.setattr(route, "get_user_group_ids", _group_ids)
        monkeypatch.setattr(route, "get_user_integration", _visible)
        monkeypatch.setattr(route, "reveal_json_field", _reveal_json)
        monkeypatch.setattr(route, "can_manage", lambda user, entity, group_ids=None: manages)
        monkeypatch.setattr(route, "reveal_field", _reveal)
        monkeypatch.setattr(route, "integration_egress_route", _route)
        monkeypatch.setitem(route.GATEWAY_CLIENTS, "whapi", gateway)
        return gateway

    return _open


GATEWAY_EXTRA = {"backend": "linked_device_gateway", "gateway_vendor": "whapi", "webhook_secret": SECRET}


# --- who may read it ------------------------------------------------------------------


async def test_an_integration_that_is_not_whatsapp_is_unknown(opened):
    integration = _integration(provider="slack")
    opened(integration, GATEWAY_EXTRA, manages=True)
    with pytest.raises(HTTPException) as raised:
        await route.get_whatsapp_pairing(str(integration.id), user=_USER, db=None)
    assert raised.value.status_code == 404


async def test_a_whatsapp_integration_on_the_meta_backend_is_unknown(opened):
    integration = _integration()
    opened(integration, {"backend": "meta_cloud_api", "phone_number_id": "1"}, manages=True)
    with pytest.raises(HTTPException) as raised:
        await route.get_whatsapp_pairing(str(integration.id), user=_USER, db=None)
    assert raised.value.status_code == 404


def test_the_route_is_mounted_under_the_integrations():
    from app.main import application

    paths = {r.path for r in application.routes if isinstance(r, APIRoute)}
    assert "/api/v1/integrations/{integration_id}/whatsapp/pairing" in paths


# --- the answer -----------------------------------------------------------------------


async def test_a_manager_sees_the_qr_and_the_webhook_path_while_not_paired(opened):
    integration = _integration()
    gateway = opened(integration, GATEWAY_EXTRA, manages=True)
    gateway.health_answer = ({"status": {"text": "QR"}}, None)
    response = await route.get_whatsapp_pairing(str(integration.id), user=_USER, db=None)
    assert response.state == "qr"
    assert response.qr == "data:image/png;base64,iVBORw0KGgo="
    assert response.webhook_path == f"/api/channels/whatsapp/gateway/{integration.id}?token={SECRET}"
    assert response.can_manage is True
    assert response.has_webhook_secret is True
    assert response.error is None
    assert response.error_code is None


async def test_a_viewer_sees_the_state_but_neither_the_qr_nor_the_secret(opened):
    integration = _integration()
    gateway = opened(integration, GATEWAY_EXTRA, manages=False)
    gateway.health_answer = ({"status": {"text": "QR"}}, None)
    response = await route.get_whatsapp_pairing(str(integration.id), user=_USER, db=None)
    assert response.state == "qr"
    assert response.qr is None
    assert response.webhook_path is None
    assert response.has_webhook_secret is True
    assert gateway.qr_calls == 0
    assert SECRET not in response.model_dump_json()


async def test_a_paired_number_asks_for_no_qr(opened):
    integration = _integration()
    gateway = opened(integration, GATEWAY_EXTRA, manages=True)
    response = await route.get_whatsapp_pairing(str(integration.id), user=_USER, db=None)
    assert response.state == "connected"
    assert response.qr is None
    assert gateway.qr_calls == 0


@pytest.mark.parametrize(
    ("text", "state"),
    [("AUTH", "connected"), ("QR", "qr"), ("BANNED", "banned"), ("SYNC_ERROR", "sync_error"), ("LAUNCH", "starting"), ("?", "unknown")],
)
async def test_the_state_is_the_tools_mapping(opened, text, state):
    integration = _integration()
    gateway = opened(integration, GATEWAY_EXTRA, manages=False)
    gateway.health_answer = ({"status": {"text": text}}, None)
    response = await route.get_whatsapp_pairing(str(integration.id), user=_USER, db=None)
    assert response.state == state


async def test_a_gateway_error_is_unknown_with_the_reason(opened):
    integration = _integration()
    gateway = opened(integration, GATEWAY_EXTRA, manages=True)
    gateway.health_answer = (None, "WhatsApp gateway: read channel health failed (401)")
    response = await route.get_whatsapp_pairing(str(integration.id), user=_USER, db=None)
    assert response.state == "unknown"
    assert response.error_code == "gateway_unreachable"
    assert response.error == "WhatsApp gateway: read channel health failed (401)"
    assert response.webhook_path is not None


async def test_no_secret_gives_no_webhook_path(opened):
    integration = _integration()
    opened(integration, {"backend": "linked_device_gateway"}, manages=True)
    response = await route.get_whatsapp_pairing(str(integration.id), user=_USER, db=None)
    assert response.webhook_path is None
    assert response.has_webhook_secret is False


async def test_no_channel_token_says_so_without_calling_the_gateway(opened):
    integration = _integration()
    gateway = opened(integration, GATEWAY_EXTRA, manages=True, channel="")
    response = await route.get_whatsapp_pairing(str(integration.id), user=_USER, db=None)
    assert response.state == "unknown"
    assert response.error_code == "no_channel_token"
    assert "API key" in (response.error or "")
    assert gateway.qr_calls == 0


async def test_an_unreadable_channel_token_is_a_code(opened, monkeypatch):
    integration = _integration()
    gateway = opened(integration, GATEWAY_EXTRA, manages=True)

    async def _broken(entity, column):
        raise RuntimeError("vault down")

    monkeypatch.setattr(route, "reveal_field", _broken)
    response = await route.get_whatsapp_pairing(str(integration.id), user=_USER, db=None)
    assert response.state == "unknown"
    assert response.error_code == "token_unreadable"
    assert gateway.qr_calls == 0


async def test_an_unknown_vendor_is_a_code(opened):
    integration = _integration()
    opened(integration, {**GATEWAY_EXTRA, "gateway_vendor": "nope"}, manages=True)
    response = await route.get_whatsapp_pairing(str(integration.id), user=_USER, db=None)
    assert response.state == "unknown"
    assert response.error_code == "unknown_vendor"


def test_a_qr_already_in_data_uri_form_is_kept():
    assert route.qr_data_uri({"base64": "data:image/png;base64,AAA"}) == "data:image/png;base64,AAA"
    assert route.qr_data_uri({"status": "OK"}) is None
    assert route.qr_data_uri(None) is None
