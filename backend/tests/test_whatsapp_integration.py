"""The WhatsApp integration provider.

Covers the catalog entry, the credential chokepoint (``phone_number_id||token``),
the recipient shape (number vs group), the documented Cloud API limits the tools
enforce (no interactive message in a group), the read/write classification the
read-only binding relies on, and the connection probe.
"""

from __future__ import annotations

import pytest

from app.api.v1.integration_catalog import PROVIDER_CATALOG, build_dummy_token
from app.integrations.providers import whatsapp
from app.integrations.providers.whatsapp import (
    GRAPH_API,
    WhatsAppIntegration,
    build_tools,
    recipient_fields,
)
from app.services.integration_read_only import integration_tool_access
from app.services.integration_tools import PROVIDER_TOOLS, _SafeIntegrationExecutor

_TOKEN = "1234567890||EAAGtoken"
_GROUP_ID = "Y2FwaV9ncm91cDoxOTUwNTU1MDA3OToxMjAzNjMzOTQzMjA"


class _FakeResp:
    def __init__(self, payload: dict | None = None, status_code: int = 200):
        self._payload = payload or {}
        self.status_code = status_code
        self.is_success = 200 <= status_code < 300

    def json(self) -> dict:
        return self._payload


def _build(token: str = _TOKEN):
    executor = _SafeIntegrationExecutor()
    tools = build_tools(token, executor)
    return executor._handlers, tools


def _capture(monkeypatch, payload: dict | None = None) -> list[dict]:
    calls: list[dict] = []

    async def _fake(client, method, url, provider, action, **kwargs):
        calls.append({"method": method, "url": url, "action": action, **kwargs})
        return _FakeResp(payload if payload is not None else {"messages": [{"id": "wamid.X"}]}), None

    monkeypatch.setattr(whatsapp, "safe_request", _fake)
    return calls


def test_catalog_entry_declares_the_credential_fields():
    entry = PROVIDER_CATALOG["whatsapp"]
    assert entry["auth_type"] == "api_key"
    assert entry["supported_auth_types"] == ["api_key"]
    assert set(entry["extra_fields"]) == {
        "backend",
        "phone_number_id",
        "waba_id",
        "app_secret",
        "verify_token",
        "gateway_vendor",
        "base_url",
        "webhook_secret",
        "inbound_retention_days",
        "self_chat_agent_id",
    }
    assert [o["value"] for o in entry["extra_field_options"]["backend"]] == [
        "meta_cloud_api",
        "linked_device_gateway",
    ]
    assert entry["messaging_channel"] == "whatsapp"
    assert "messaging" in entry["integration_types"]
    assert PROVIDER_TOOLS["whatsapp"] is build_tools


def test_catalog_entry_declares_the_meta_callback_path():
    # The integration page shows base URL + this path with a copy button;
    # it is the route the channel webhook serves for the bot and the integrations.
    assert PROVIDER_CATALOG["whatsapp"]["webhook_path"] == "/api/channels/whatsapp/webhook"
    assert PROVIDER_CATALOG["whatsapp"].get("available_events", []) == []


def test_setup_guide_tells_where_the_callback_url_and_verify_token_go():
    # Step 6 of the Cloud API guide follows Test Connection: the callback URL from the
    # Webhooks card goes into the Meta app with the same verify_token.
    steps = PROVIDER_CATALOG["whatsapp"]["setup_guide"]["steps"]
    assert steps[4].startswith("Click 'Test Connection'")
    step = steps[5]
    assert "Webhooks card" in step
    assert "Meta app" in step
    assert "'verify_token'" in step


def test_dummy_token_builds_the_tools():
    assert build_tools(build_dummy_token("whatsapp"), _SafeIntegrationExecutor())


@pytest.mark.parametrize("token", ["", "||token", "1234||", "   "])
def test_incomplete_token_builds_no_tools(token):
    handlers, tools = _build(token)
    assert tools == []
    assert handlers == {}


def test_recipient_is_a_number_or_a_group():
    assert recipient_fields("+33612345678") == {"recipient_type": "individual", "to": "33612345678"}
    assert recipient_fields("14155552671") == {"recipient_type": "individual", "to": "14155552671"}
    assert recipient_fields(_GROUP_ID) == {"recipient_type": "group", "to": _GROUP_ID}
    assert recipient_fields("  ") is None


def test_write_tools_are_classified_write_and_reads_read():
    _, tools = _build()
    names = {t.name for t in tools}
    writes = {
        "send_whatsapp_message",
        "send_whatsapp_template",
        "send_whatsapp_media",
        "send_whatsapp_interactive",
        "react_whatsapp_message",
        "mark_whatsapp_message_read",
    }
    reads = {
        "get_whatsapp_business_number",
        "list_whatsapp_groups",
        "get_whatsapp_group",
        "download_whatsapp_media",
        "list_whatsapp_conversations",
        "read_whatsapp_messages",
    }
    assert names == writes | reads
    assert {n for n in names if integration_tool_access(n) == "write"} == writes


@pytest.mark.asyncio
async def test_send_text_to_a_group_uses_the_group_recipient(monkeypatch):
    calls = _capture(monkeypatch)
    handlers, _ = _build()
    out = await handlers["send_whatsapp_message"](to=_GROUP_ID, text="Hello board")
    assert "wamid.X" in out
    body = calls[0]["json"]
    assert calls[0]["url"] == f"{GRAPH_API}/1234567890/messages"
    assert body["recipient_type"] == "group"
    assert body["to"] == _GROUP_ID
    assert body["text"] == {"body": "Hello board"}


@pytest.mark.asyncio
async def test_long_text_is_cut_to_the_api_limit(monkeypatch):
    calls = _capture(monkeypatch)
    handlers, _ = _build()
    out = await handlers["send_whatsapp_message"](to="14155552671", text="x" * 5000)
    assert len(calls[0]["json"]["text"]["body"]) == 4096
    assert "cut to 4096" in out


@pytest.mark.asyncio
async def test_interactive_in_a_group_is_refused_with_the_limit(monkeypatch):
    calls = _capture(monkeypatch)
    handlers, _ = _build()
    out = await handlers["send_whatsapp_interactive"](to=_GROUP_ID, body="Which date?", buttons=["Mon", "Tue"])
    assert out.startswith("Error:")
    assert "groups" in out
    assert calls == []


@pytest.mark.asyncio
async def test_interactive_buttons_payload(monkeypatch):
    calls = _capture(monkeypatch)
    handlers, _ = _build()
    await handlers["send_whatsapp_interactive"](to="14155552671", body="Which date?", buttons=["Mon", "Tue"])
    interactive = calls[0]["json"]["interactive"]
    assert interactive["type"] == "button"
    assert [b["reply"]["title"] for b in interactive["action"]["buttons"]] == ["Mon", "Tue"]


@pytest.mark.asyncio
async def test_interactive_refuses_more_than_three_buttons(monkeypatch):
    calls = _capture(monkeypatch)
    handlers, _ = _build()
    out = await handlers["send_whatsapp_interactive"](to="14155552671", body="Pick", buttons=["a", "b", "c", "d"])
    assert out.startswith("Error:")
    assert calls == []


@pytest.mark.asyncio
async def test_template_parameters_fill_the_body(monkeypatch):
    calls = _capture(monkeypatch)
    handlers, _ = _build()
    await handlers["send_whatsapp_template"](
        to="14155552671", template_name="board_reminder", language_code="fr", body_parameters=["lundi", "10h"]
    )
    template = calls[0]["json"]["template"]
    assert template["language"] == {"code": "fr"}
    assert template["components"][0]["parameters"] == [
        {"type": "text", "text": "lundi"},
        {"type": "text", "text": "10h"},
    ]


@pytest.mark.asyncio
async def test_list_groups_formats_the_page(monkeypatch):
    _capture(monkeypatch, {"data": {"groups": [{"id": _GROUP_ID, "subject": "Board", "created_at": "1700000000"}]}})
    handlers, _ = _build()
    out = await handlers["list_whatsapp_groups"]()
    assert "Board" in out and _GROUP_ID in out


@pytest.mark.asyncio
async def test_validate_connection_reads_the_phone_number(monkeypatch):
    calls = _capture(monkeypatch, {"display_phone_number": "+33 1 23 45 67 89"})
    assert await WhatsAppIntegration().validate_connection(_TOKEN) is True
    assert calls[0]["url"] == f"{GRAPH_API}/1234567890"
    assert calls[0]["headers"]["Authorization"] == "Bearer EAAGtoken"


@pytest.mark.asyncio
async def test_validate_connection_refuses_an_incomplete_token(monkeypatch):
    calls = _capture(monkeypatch)
    assert await WhatsAppIntegration().validate_connection("1234||") is False
    assert calls == []
