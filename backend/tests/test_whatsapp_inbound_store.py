"""WhatsApp inbound store and webhook routing by phone_number_id."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.channels import whatsapp
from app.services import whatsapp_inbound
from app.services.whatsapp_inbound import InboundIntegration, message_row, retention_days

INTEGRATION_PHONE_ID = "555000111"
BOT_PHONE_ID = "999000111"
SECRET = "integration-app-secret"


def _request(payload: dict, signature: str = "") -> Request:
    body = json.dumps(payload).encode()
    headers = [(b"x-hub-signature-256", signature.encode())] if signature else []
    scope = {"type": "http", "method": "POST", "path": "/api/channels/whatsapp/webhook", "query_string": b"", "headers": headers}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _sign(payload: dict, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), json.dumps(payload).encode(), hashlib.sha256).hexdigest()


def _change(phone_number_id: str, message: dict) -> dict:
    return {
        "field": "messages",
        "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": phone_number_id, "display_phone_number": "15550001111"},
            "contacts": [{"wa_id": "33612345678", "profile": {"name": "Alice"}}],
            "messages": [{"from": "33612345678", "id": "wamid.1", "timestamp": "1762100000", **message}],
        },
    }


def _payload(*changes: dict) -> dict:
    return {"object": "whatsapp_business_account", "entry": [{"id": "1", "changes": list(changes)}]}


_TARGET = InboundIntegration(integration_id=uuid.uuid4(), app_secret=SECRET, verify_token="vt", retention_days=30)


@pytest.fixture
def routing(monkeypatch):
    """The integration's number resolves to _TARGET; the bot path and the store are stubbed."""

    async def lookup(phone_number_id: str):
        return _TARGET if phone_number_id == INTEGRATION_PHONE_ID else None

    monkeypatch.setattr(whatsapp_inbound, "integration_for_phone_number", lookup)
    record = AsyncMock(return_value=1)
    monkeypatch.setattr(whatsapp_inbound, "record_change", record)
    monkeypatch.setattr(whatsapp, "_get_whatsapp_creds", AsyncMock(return_value=("token", BOT_PHONE_ID, "verify", "")))
    spawned: list = []

    def spawn(coro, **_kw):
        spawned.append(coro)
        coro.close()

    monkeypatch.setattr(whatsapp, "spawn_channel_task", spawn)
    return record, spawned


# ── message_row ──────────────────────────────────────────────────────────────


def test_text_message_row_keeps_text_sender_and_conversation():
    row = message_row({"from": "336", "id": "wamid.A", "timestamp": "1762100000", "type": "text", "text": {"body": "hi"}}, [{"wa_id": "336", "profile": {"name": "Bob"}}])
    assert row["text"] == "hi" and row["sender_name"] == "Bob"
    assert row["conversation_id"] == "336" and row["chat_type"] == "private"


def test_group_message_is_threaded_by_group_id():
    row = message_row({"from": "336", "id": "wamid.B", "type": "text", "text": {"body": "x"}, "group_id": "G1"}, [])
    assert row["conversation_id"] == "G1" and row["chat_type"] == "group"


def test_media_message_records_its_media_id_and_caption():
    row = message_row({"from": "336", "id": "wamid.C", "type": "image", "image": {"id": "4411", "caption": "look"}}, [])
    assert row["media_id"] == "4411" and row["text"] == "[image 4411] look"


def test_poll_is_recorded_in_words_not_dropped():
    msg = {"from": "336", "id": "wamid.D", "type": "unsupported", "unsupported": {"type": "poll_creation"}}
    assert message_row(msg, [])["text"] == whatsapp_inbound.POLL_WORDS


def test_other_unsupported_message_is_named():
    msg = {"from": "336", "id": "wamid.E", "type": "unsupported", "unsupported": {"type": "edit"}}
    assert "(edit)" in message_row(msg, [])["text"]


def test_message_without_id_or_sender_is_skipped():
    assert message_row({"type": "text", "text": {"body": "x"}}, []) is None


@pytest.mark.parametrize(("raw", "days"), [(None, 30), ("7", 7), ("0", 1), ("9999", 365), ("abc", 30)])
def test_retention_days_is_bounded(raw, days):
    assert retention_days({"inbound_retention_days": raw}) == days


# ── webhook routing ──────────────────────────────────────────────────────────


async def test_integration_number_is_stored_not_answered_by_the_bot(routing):
    record, spawned = routing
    payload = _payload(_change(INTEGRATION_PHONE_ID, {"type": "text", "text": {"body": "hello"}}))
    assert await whatsapp.whatsapp_webhook(_request(payload, _sign(payload))) == {"ok": True}
    record.assert_awaited_once()
    assert record.await_args.args[0] is _TARGET
    assert spawned == []


async def test_integration_number_with_a_bad_signature_is_rejected(routing):
    record, _ = routing
    payload = _payload(_change(INTEGRATION_PHONE_ID, {"type": "text", "text": {"body": "forged"}}))
    with pytest.raises(HTTPException) as exc:
        await whatsapp.whatsapp_webhook(_request(payload, _sign(payload, "wrong")))
    assert exc.value.status_code == 401
    record.assert_not_awaited()


async def test_bot_number_still_reaches_the_bot(routing):
    record, spawned = routing
    payload = _payload(_change(BOT_PHONE_ID, {"type": "text", "text": {"body": "to the bot"}}))
    assert await whatsapp.whatsapp_webhook(_request(payload)) == {"ok": True}
    record.assert_not_awaited()
    assert len(spawned) == 1


async def test_mixed_payload_splits_between_integration_and_bot(routing):
    record, spawned = routing
    payload = _payload(
        _change(INTEGRATION_PHONE_ID, {"type": "text", "text": {"body": "a"}}),
        _change(BOT_PHONE_ID, {"type": "text", "text": {"body": "b"}}),
    )
    assert await whatsapp.whatsapp_webhook(_request(payload, _sign(payload))) == {"ok": True}
    record.assert_awaited_once()
    assert len(spawned) == 1


async def test_verify_accepts_an_integration_verify_token(monkeypatch):
    monkeypatch.setattr(whatsapp, "_get_whatsapp_creds", AsyncMock(return_value=None))
    monkeypatch.setattr(whatsapp_inbound, "verify_tokens", AsyncMock(return_value=["integration-vt"]))
    assert await whatsapp.whatsapp_verify("subscribe", "integration-vt", "42") == 42
    with pytest.raises(HTTPException):
        await whatsapp.whatsapp_verify("subscribe", "other", "42")
