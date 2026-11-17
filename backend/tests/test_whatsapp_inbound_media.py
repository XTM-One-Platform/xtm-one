"""What a WhatsApp message carrying a media hands to the channel router.

The webhook skipped every message that was not ``type: "text"``: a photo, a
document, a voice note or a video a user sent never reached the agent. A media
message names its media by id; ``GET /{media-id}`` answers a Meta URL, valid
five minutes, that is downloaded with the access token.

Pinned here, through the webhook and its processing task with Cloud API shaped
payloads: a caption is the message text and the media an
``InboundFileAttachment`` the router downloads (a bare media gets the
file-only placeholder); the media id is resolved off the webhook's answer, for
the bot's own number; the token is only handed over with a Meta URL; a media
over the stored-file limit, or one WhatsApp cannot serve, is left out while the
message goes on; a sticker is ignored; and no line logs the caption, the file
name, the number or the token.
"""

from __future__ import annotations

import contextlib
import json
import logging
import mimetypes
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.requests import Request

from app.channels import whatsapp
from app.channels.base import FILE_ONLY_PLACEHOLDER, OutboundMessage

TOKEN = "EAAJB-whatsapp-access-token"
PHONE_NUMBER_ID = "106540352242922"
PHONE = "16505551234"
NAME = "Sheena Nelson"
CAPTION = "my receipt for the Lisbon trip"
FILE_NAME = "receipt-sheena.pdf"
PDF = b"%PDF-1.7 receipt"
MEDIA_URL = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1337"


def _request(payload: object) -> Request:
    body = json.dumps(payload).encode()
    scope = {"type": "http", "method": "POST", "path": "/api/channels/whatsapp/webhook", "query_string": b"", "headers": []}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


class _FakeChannel:
    """A WhatsApp client that sends nothing."""

    def __init__(self, *_args, **_kwargs):
        pass

    async def send_message(self, _outbound):
        return None


class _CloudApi:
    """``httpx.AsyncClient`` standing in for the Cloud API: ``GET /{media-id}`` and the media download.

    *media* maps a media id to what ``GET /{media-id}`` answers; an unknown id
    answers 404, as Meta does for an expired one.
    """

    def __init__(self, media: dict[str, dict]):
        self.media = media
        self.lookups: list[tuple[str, dict, dict]] = []
        self.downloads: list[tuple[str, dict]] = []
        api = self

        class _Client:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def get(self, url, params=None, headers=None):
                return api.lookup(url, params or {}, headers or {})

            @contextlib.asynccontextmanager
            async def stream(self, method, url, params=None, headers=None):
                api.downloads.append((url, dict(headers or {})))
                request = httpx.Request(method, url)
                if url != MEDIA_URL:
                    yield httpx.Response(404, request=request, stream=httpx.ByteStream(b""))
                    return
                yield httpx.Response(
                    200, headers={"content-type": "application/pdf"}, stream=httpx.ByteStream(PDF), request=request
                )

        self.client = _Client

    def lookup(self, url: str, params: dict, headers: dict) -> httpx.Response:
        request = httpx.Request("GET", url, params=params)
        media_id = url.removeprefix(f"{whatsapp._BASE_URL}/")
        self.lookups.append((media_id, params, headers))
        if media_id not in self.media:
            return httpx.Response(404, json={"error": {"message": "Unsupported get request"}}, request=request)
        return httpx.Response(200, json={"messaging_product": "whatsapp", "id": media_id, **self.media[media_id]}, request=request)


@pytest.fixture
def cloud_api(monkeypatch):
    """The Cloud API with a PDF, a photo and a voice note; the adapter's router and client stubbed."""
    api = _CloudApi(
        {
            "622684793477189": {"url": MEDIA_URL, "mime_type": "application/pdf", "file_size": str(len(PDF))},
            "1003383421387256": {"url": MEDIA_URL, "mime_type": "image/jpeg", "file_size": 2048},
            "4411": {"url": MEDIA_URL, "mime_type": "audio/ogg", "file_size": 512},
        }
    )
    monkeypatch.setattr(httpx, "AsyncClient", api.client)
    monkeypatch.setattr(whatsapp, "WhatsAppChannel", _FakeChannel)
    monkeypatch.setattr(whatsapp, "_get_whatsapp_creds", AsyncMock(return_value=(TOKEN, PHONE_NUMBER_ID, "verify", "")))
    monkeypatch.setattr(whatsapp, "handle_message", AsyncMock(return_value=OutboundMessage(text="", suppress=True)))
    api.spawned = []
    monkeypatch.setattr(whatsapp, "spawn_channel_task", lambda coro, **_kw: api.spawned.append(coro))
    return api


def _payload(message: dict, *, group_id: str | None = None) -> dict:
    msg = {"from": PHONE, "id": "wamid.HBgLMTY1MDM4Nzk0MzkVAgASGBQzQTRB", "timestamp": "1744344496", **message}
    if group_id:
        msg["group_id"] = group_id
    value = {"messaging_product": "whatsapp", "contacts": [{"wa_id": PHONE, "profile": {"name": NAME}}], "messages": [msg]}
    return {"object": "whatsapp_business_account", "entry": [{"id": "102290129340398", "changes": [{"value": value, "field": "messages"}]}]}


_DOCUMENT = {
    "type": "document",
    "document": {"caption": CAPTION, "filename": FILE_NAME, "mime_type": "application/pdf", "sha256": "V5O=", "id": "622684793477189"},
}


async def _deliver(api: _CloudApi, payload: dict):
    """Post *payload* to the webhook, run its processing task, return what reached the router."""
    assert await whatsapp.whatsapp_webhook(_request(payload)) == {"ok": True}
    if not api.spawned:
        return None
    assert len(api.spawned) == 1
    assert api.lookups == [], "GET /{media-id} runs in the processing task, never before the webhook answers"
    await api.spawned.pop()
    whatsapp.handle_message.assert_awaited_once()
    return whatsapp.handle_message.await_args.args[0]


def _logged(caplog) -> str:
    return "\n".join(logging.Formatter("%(levelname)s %(message)s").format(r) for r in caplog.records)


def _info_and_above(caplog) -> str:
    return "\n".join(
        logging.Formatter("%(levelname)s %(message)s").format(r) for r in caplog.records if r.levelno >= logging.INFO
    )


@pytest.mark.asyncio
async def test_a_document_with_a_caption_reaches_the_router_with_the_caption_and_the_file(cloud_api, caplog):
    caplog.set_level(logging.DEBUG, logger="app.channels")

    inbound = await _deliver(cloud_api, _payload(_DOCUMENT))

    assert inbound.text == CAPTION
    [(media_id, params, headers)] = cloud_api.lookups
    assert media_id == "622684793477189"
    assert params == {"phone_number_id": PHONE_NUMBER_ID}, "WhatsApp answers only for media of the bot's own number"
    assert headers == {"Authorization": f"Bearer {TOKEN}"}
    [attachment] = inbound.file_attachments
    assert (attachment.url, attachment.filename, attachment.content_type, attachment.size) == (
        MEDIA_URL,
        FILE_NAME,
        "application/pdf",
        len(PDF),
    )
    assert attachment.platform_file_id == "622684793477189"
    assert attachment.auth_headers == {"Authorization": f"Bearer {TOKEN}"}, "the media URL needs the access token"
    assert inbound.channel_user_id == PHONE and inbound.metadata["chat_type"] == "private"
    assert TOKEN not in _logged(caplog)
    info = _info_and_above(caplog).lower()
    assert not [value for value in (CAPTION, FILE_NAME, NAME, PHONE, "lookaside") if value.lower() in info]


@pytest.mark.asyncio
async def test_a_photo_without_a_caption_reaches_the_router_as_a_file_only_message(cloud_api):
    image = {"type": "image", "image": {"mime_type": "image/jpeg", "sha256": "SfI=", "id": "1003383421387256"}}

    inbound = await _deliver(cloud_api, _payload(image, group_id="120363041234567890@g.us"))

    assert inbound.text == FILE_ONLY_PLACEHOLDER, "the placeholder a Teams or Telegram file-only message gets"
    [attachment] = inbound.file_attachments
    assert attachment.filename == "image" + (mimetypes.guess_extension("image/jpeg") or "")
    assert attachment.content_type == "image/jpeg"
    assert inbound.metadata["chat_type"] == "group"


@pytest.mark.asyncio
async def test_a_voice_note_is_handed_over_as_audio_without_its_codec(cloud_api):
    voice = {"type": "audio", "audio": {"mime_type": "audio/ogg; codecs=opus", "sha256": "a=", "id": "4411", "voice": True}}

    inbound = await _deliver(cloud_api, _payload(voice))

    assert inbound.text == FILE_ONLY_PLACEHOLDER
    [attachment] = inbound.file_attachments
    assert attachment.content_type == "audio/ogg"
    assert attachment.filename == "voice" + (mimetypes.guess_extension("audio/ogg") or "")


@pytest.mark.asyncio
async def test_the_router_downloads_and_stores_the_media_the_adapter_hands_it(cloud_api, monkeypatch, caplog):
    """The attachment is the one the router already downloads for Slack, Teams, Telegram and Discord."""
    from app.channels import router
    from app.services import file_storage

    caplog.set_level(logging.DEBUG, logger="app")
    stored: list = []
    monkeypatch.setattr(file_storage.file_storage, "_client", SimpleNamespace(put_object=lambda *args: stored.append(args)))
    inbound = await _deliver(cloud_api, _payload(_DOCUMENT))
    rows: list = []
    db = SimpleNamespace(add=rows.append, flush=AsyncMock())

    [ref] = await router._resolve_channel_files(db, inbound, None)

    [(url, headers)] = cloud_api.downloads
    assert url == MEDIA_URL and headers["Authorization"] == f"Bearer {TOKEN}"
    assert len(stored) == 1
    [row] = rows
    assert (row.filename, row.content_type, row.size, row.source) == (FILE_NAME, "application/pdf", len(PDF), "channel:whatsapp")
    assert ref.file_id == str(row.id)
    logged = _logged(caplog)
    assert TOKEN not in logged and FILE_NAME not in logged


@pytest.mark.asyncio
async def test_the_token_is_never_handed_over_with_a_url_outside_meta(cloud_api, caplog):
    caplog.set_level(logging.DEBUG, logger="app.channels")
    cloud_api.media["622684793477189"] = {"url": "https://attacker.example/steal", "mime_type": "application/pdf"}

    inbound = await _deliver(cloud_api, _payload(_DOCUMENT))

    assert inbound.text == CAPTION, "the caption still reaches the agent"
    assert inbound.file_attachments == []
    assert any(r.getMessage() == "Failed to resolve WhatsApp media: application/pdf (no Meta media URL)" for r in caplog.records)
    assert "attacker.example" not in _logged(caplog)


@pytest.mark.asyncio
async def test_a_media_over_the_stored_file_limit_is_left_out(cloud_api, caplog):
    from app.services.file_storage import MAX_UPLOAD_SIZE_BYTES

    caplog.set_level(logging.DEBUG, logger="app.channels")
    cloud_api.media["622684793477189"] = {"url": MEDIA_URL, "mime_type": "application/pdf", "file_size": MAX_UPLOAD_SIZE_BYTES + 1}

    inbound = await _deliver(cloud_api, _payload(_DOCUMENT))

    assert inbound.text == CAPTION
    assert inbound.file_attachments == []
    assert any(r.getMessage().startswith("WhatsApp media too large") for r in caplog.records)


@pytest.mark.asyncio
async def test_a_media_whatsapp_no_longer_serves_leaves_the_message_to_the_router_without_it(cloud_api, caplog):
    caplog.set_level(logging.DEBUG, logger="app.channels")
    expired = {"type": "document", "document": {**_DOCUMENT["document"], "id": "999"}}

    inbound = await _deliver(cloud_api, _payload(expired))

    assert inbound.text == CAPTION
    assert inbound.file_attachments == []
    failures = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Failed to resolve WhatsApp media")]
    assert failures == ["Failed to resolve WhatsApp media: application/pdf (HTTP 404)"]
    assert TOKEN not in _logged(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        {"type": "sticker", "sticker": {"mime_type": "image/webp", "sha256": "k=", "id": "5566", "animated": False}},
        {"type": "image", "image": {"mime_type": "image/jpeg", "id": "../me/accounts"}},
        {"type": "document", "document": None},
        {"type": "reaction", "reaction": {"message_id": "wamid.X", "emoji": "+"}},
    ],
    ids=["sticker", "media_id_not_digits", "null_media", "reaction"],
)
async def test_a_message_with_neither_text_nor_a_readable_media_is_ignored(cloud_api, message):
    assert await _deliver(cloud_api, _payload(message)) is None
    whatsapp.handle_message.assert_not_awaited()
    assert cloud_api.lookups == []


@pytest.mark.asyncio
async def test_a_text_message_is_still_handed_over_without_files(cloud_api):
    inbound = await _deliver(cloud_api, _payload({"type": "text", "text": {"body": "Where is my order?"}}))

    assert inbound.text == "Where is my order?"
    assert inbound.file_attachments == []
    assert cloud_api.lookups == []
