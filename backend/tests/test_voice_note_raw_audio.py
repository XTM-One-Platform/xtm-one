"""A voice note's raw audio stays reachable to the agent (#9920, S2).

The turn line of a WhatsApp voice note names its media id, the row keeps it,
and ``download_whatsapp_media`` fetches the audio as a file of the
conversation on both backends, so the agent can hear it again on demand.
"""

from __future__ import annotations

import pytest

from app.integrations.providers import whatsapp, whatsapp_gateway
from app.integrations.providers.whatsapp_gateway import gateway_token, sniffed_mime
from app.services import whatsapp_inbound
from app.services.whatsapp_gateway_inbound import parse_whapi
from app.tools.base import CallableToolExecutor

OWN = "33611111111"
_VOICE = {
    "id": "V1",
    "type": "voice",
    "voice": {"id": "oga-abc123", "seconds": 42, "mime_type": "audio/ogg; codecs=opus"},
    "timestamp": 1_800_000_000,
    "from_me": True,
    "chat_id": f"{OWN}@s.whatsapp.net",
    "from": OWN,
}


def test_audio_reference_follows_the_line():
    line = whatsapp_inbound.with_audio_reference("[voice note, 0:42, transcribed] hello", "oga-abc123")
    first, second = line.split("\n")
    assert first == "[voice note, 0:42, transcribed] hello"
    assert "media id oga-abc123" in second and "download_whatsapp_media" in second


@pytest.mark.parametrize("media_id", [None, "", "  ", "x" * (whatsapp_inbound.MAX_MEDIA_ID + 1)])
def test_no_reference_without_a_usable_media_id(media_id):
    assert whatsapp_inbound.with_audio_reference("line", media_id) == "line"


def test_gateway_voice_row_keeps_its_media_id():
    row = parse_whapi({"messages": [_VOICE]}).rows[0]
    assert row["media_id"] == "oga-abc123"


def test_gateway_text_row_has_no_media_id():
    msg = {**_VOICE, "type": "text", "text": {"body": "hi"}}
    msg.pop("voice")
    assert parse_whapi({"messages": [msg]}).rows[0]["media_id"] is None


async def test_gateway_transcribed_note_names_its_audio(monkeypatch):
    import uuid

    from app.services import whatsapp_gateway_inbound as gi

    gateway = gi.GatewayIntegration(
        target=whatsapp_inbound.InboundIntegration(
            integration_id=uuid.uuid4(), app_secret="", verify_token="", retention_days=30
        ),
        vendor="whapi",
        webhook_secret="s",
        channel_token="tok",
        owner_id=uuid.uuid4(),
    )

    async def fake_one(_gateway, _note):
        return "[voice note, 0:42, transcription low confidence: too few words]", None

    monkeypatch.setattr(gi, "_transcribe_one", fake_one)
    d = parse_whapi({"messages": [_VOICE]})
    await gi.transcribe_voice(gateway, d)
    text = d.rows[0]["text"]
    assert text.startswith("[voice note, 0:42, transcription low confidence: too few words]\n")
    assert "media id oga-abc123" in text


def _gateway_handlers():
    executor = CallableToolExecutor()
    tools = whatsapp.build_tools(gateway_token("whapi", "", "channel-token"), executor)
    return executor._handlers, {t.name for t in tools}


def test_gateway_backend_has_the_media_download():
    handlers, names = _gateway_handlers()
    assert "download_whatsapp_media" in names and "download_whatsapp_media" in handlers


async def test_gateway_download_delivers_the_audio(monkeypatch):
    seen = {}

    async def media(self, media_id, max_bytes):
        seen["id"] = media_id
        return b"OggS\x00\x02voice", None

    async def deliver(**kwargs):
        seen.update(kwargs)
        return "delivered"

    monkeypatch.setattr(whatsapp_gateway.WhapiClient, "download_media", media)
    monkeypatch.setattr(whatsapp_gateway, "deliver_file", deliver)
    handlers, _ = _gateway_handlers()
    assert await handlers["download_whatsapp_media"](media_id="oga-abc123") == "delivered"
    assert seen["id"] == "oga-abc123"
    assert seen["content_type"] == "audio/ogg" and seen["filename"] == "whatsapp-oga-abc123"


async def test_gateway_download_failure_is_an_error(monkeypatch):
    async def media(self, media_id, max_bytes):
        return b"", "404 not found"

    monkeypatch.setattr(whatsapp_gateway.WhapiClient, "download_media", media)
    handlers, _ = _gateway_handlers()
    result = await handlers["download_whatsapp_media"](media_id="oga-abc123")
    assert result.is_error and "404 not found" in result.content
    empty = await handlers["download_whatsapp_media"](media_id=" ")
    assert empty.is_error


@pytest.mark.parametrize(
    ("head", "mime"),
    [
        (b"OggS\x00\x02", "audio/ogg"),
        (b"\x00\x00\x00\x20ftypM4A ", "audio/mp4"),
        (b"\x00\x00\x00\x20ftypisom", "video/mp4"),
        (b"ID3\x04", "audio/mpeg"),
        (b"\xff\xd8\xff\xe0", "image/jpeg"),
        (b"\x89PNG\r\n", "image/png"),
        (b"%PDF-1.7", "application/pdf"),
        (b"????", "application/octet-stream"),
    ],
)
def test_sniffed_mime(head, mime):
    assert sniffed_mime(head) == mime
