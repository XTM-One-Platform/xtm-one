"""What a Telegram message carrying a file hands to the channel router.

Telegram never sets ``text`` on a message that carries a file: a photo,
document, audio, video or voice message carries its text in ``caption`` (its
entities in ``caption_entities``), and a bare file has neither. The webhook
returned before anything else unless the update had ``text``, so every
attachment was dropped before the router, which downloads, stores and attaches
channel files for the other platforms.

Pinned here, through the webhook and its processing task with Bot API shaped
updates: a caption is the message text and the file an ``InboundFileAttachment``
the router downloads; a bare file gets the file-only placeholder Teams uses; a
bot mention in ``caption_entities`` is detected and stripped, entity offsets
read in UTF-16 code units; ``getFile`` runs off the webhook's answer and is not
asked for a file larger than it serves; a sticker, a service message, a caption
on another kind of media and an edited caption are ignored as before; and no
line logs the caption, the file name, the sender or the bot token.
"""

from __future__ import annotations

import contextlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.requests import Request

from app.channels import telegram
from app.channels.base import FILE_ONLY_PLACEHOLDER, OutboundMessage

TOKEN = "123456:AAH-telegram-bot-token"
BOT = "copilot_bot"
CAPTION = "What does the badge PIN 4471 on this slide unlock?"
FILE_NAME = "payslip-jane.pdf"
FIRST_NAME = "Jane"
LAST_NAME = "Placeholder"
USERNAME = "jane_placeholder"
PDF = b"%PDF-1.7 quarterly figures"


def _request(payload: object) -> Request:
    body = json.dumps(payload).encode()
    scope = {"type": "http", "method": "POST", "path": "/api/channels/telegram/webhook", "query_string": b"", "headers": []}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


class _FakeChannel:
    """A Telegram client that sends nothing: every call is an awaitable no-op."""

    supports_typing = False

    def __init__(self, *_args, **_kwargs):
        pass

    def __getattr__(self, _name):
        return AsyncMock(return_value=None)


class _BotApi:
    """``httpx.AsyncClient`` standing in for the Bot API: ``getFile`` and the file download.

    *files* maps a ``file_id`` to its ``file_path`` and bytes; ``getFile`` on an
    unknown id answers 400, as Telegram does for a file it will not serve. A
    GET is answered plain or streamed.
    """

    def __init__(self, files: dict[str, tuple[str, bytes]]):
        self.files = files
        self.get_file_ids: list[str] = []
        self.downloads: list[str] = []
        api = self

        class _Client:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

            async def get(self, url, params=None, headers=None):
                return api.answer(url, params)

            @contextlib.asynccontextmanager
            async def stream(self, method, url, params=None, headers=None):
                # A body a real client streams: one built from ``content=`` is already read.
                answer = api.answer(url, params)
                yield httpx.Response(
                    answer.status_code,
                    headers=answer.headers,
                    stream=httpx.ByteStream(answer.content),
                    request=answer.request,
                )

        self.client = _Client

    def answer(self, url: str, params: dict | None) -> httpx.Response:
        request = httpx.Request("GET", url, params=params)
        if url == f"https://api.telegram.org/bot{TOKEN}/getFile":
            file_id = (params or {})["file_id"]
            self.get_file_ids.append(file_id)
            if file_id not in self.files:
                return httpx.Response(400, json={"ok": False, "description": "Bad Request: file is too big"}, request=request)
            path, content = self.files[file_id]
            result = {"file_id": file_id, "file_unique_id": "u" + file_id, "file_size": len(content), "file_path": path}
            return httpx.Response(200, json={"ok": True, "result": result}, request=request)
        prefix = f"https://api.telegram.org/file/bot{TOKEN}/"
        path = url.removeprefix(prefix)
        for file_path, content in self.files.values():
            if url.startswith(prefix) and path == file_path:
                self.downloads.append(path)
                return httpx.Response(200, content=content, headers={"content-type": "application/pdf"}, request=request)
        return httpx.Response(404, request=request)


@pytest.fixture
def bot_api(monkeypatch):
    """The Bot API with a photo, a PDF and a voice note; the adapter's router and client stubbed."""
    api = _BotApi(
        {
            "AgACAgQ-large": ("photos/file_1.jpg", b"\xff\xd8\xff large"),
            "AgACAgQ-small": ("photos/file_0.jpg", b"\xff\xd8\xff"),
            "BQACAgQ-pdf": ("documents/file_7.pdf", PDF),
            "AwACAgQ-voice": ("voice/file_3.oga", b"OggS"),
        }
    )
    monkeypatch.setattr(httpx, "AsyncClient", api.client)
    monkeypatch.setattr(telegram, "TelegramChannel", _FakeChannel)
    monkeypatch.setattr(
        telegram,
        "_get_telegram_creds",
        AsyncMock(return_value={"token": TOKEN, "webhook_secret_token": "", "bot_username": BOT}),
    )
    monkeypatch.setattr(telegram, "handle_message", AsyncMock(return_value=OutboundMessage(text="", suppress=True)))
    api.spawned = []
    monkeypatch.setattr(telegram, "spawn_channel_task", lambda coro, **_kw: api.spawned.append(coro))
    return api


def _update(*, chat_type: str = "private", key: str = "message", **message) -> dict:
    sender = {"id": 42, "is_bot": False, "first_name": FIRST_NAME, "last_name": LAST_NAME, "username": USERNAME}
    chat = {"id": 42 if chat_type == "private" else -1001, "type": chat_type}
    return {"update_id": 900, key: {"message_id": 8, "date": 1759140000, "chat": chat, "from": sender, **message}}


_PHOTO = [
    {"file_id": "AgACAgQ-small", "file_unique_id": "s", "width": 90, "height": 60, "file_size": 1200},
    {"file_id": "AgACAgQ-large", "file_unique_id": "l", "width": 1280, "height": 853, "file_size": 98000},
]
_DOCUMENT = {"file_id": "BQACAgQ-pdf", "file_unique_id": "d", "file_name": FILE_NAME, "mime_type": "application/pdf", "file_size": len(PDF)}


async def _deliver(api: _BotApi, payload: dict):
    """Post *payload* to the webhook, run its processing task, return what reached the router."""
    response = await telegram.telegram_webhook(_request(payload))
    assert response.status_code == 200
    if not api.spawned:
        return None
    assert len(api.spawned) == 1
    assert api.get_file_ids == [], "getFile runs in the processing task, never before the webhook answers"
    await api.spawned.pop()
    telegram.handle_message.assert_awaited_once()
    return telegram.handle_message.await_args.args[0]


def _logged(caplog) -> str:
    return "\n".join(logging.Formatter("%(levelname)s %(message)s").format(r) for r in caplog.records)


def _info_and_above(caplog) -> str:
    return "\n".join(
        logging.Formatter("%(levelname)s %(message)s").format(r) for r in caplog.records if r.levelno >= logging.INFO
    )


@pytest.mark.asyncio
async def test_a_photo_with_a_caption_reaches_the_router_with_the_caption_and_the_largest_size(bot_api, caplog):
    caplog.set_level(logging.DEBUG, logger="app.channels")

    inbound = await _deliver(bot_api, _update(photo=_PHOTO, caption=CAPTION))

    assert inbound.text == CAPTION
    assert bot_api.get_file_ids == ["AgACAgQ-large"], "one getFile, for the largest size of the picture"
    [attachment] = inbound.file_attachments
    assert attachment.url == f"https://api.telegram.org/file/bot{TOKEN}/photos/file_1.jpg"
    assert (attachment.filename, attachment.content_type, attachment.platform_file_id) == (
        "photo.jpg",
        "image/jpeg",
        "AgACAgQ-large",
    )
    assert attachment.size == 98000
    assert inbound.metadata["chat_type"] == "private" and inbound.metadata["bot_was_mentioned"] is False
    logged = _logged(caplog)
    assert TOKEN not in logged and "photos/file_1.jpg" not in logged
    info = _info_and_above(caplog).lower()
    assert not [value for value in (CAPTION, "badge pin", FIRST_NAME, LAST_NAME, USERNAME) if value.lower() in info]


@pytest.mark.asyncio
async def test_the_largest_photo_is_read_by_its_pixels_when_its_file_size_is_missing(bot_api):
    """``file_size`` is optional on a ``PhotoSize``: the full picture is not passed over for a thumbnail."""
    photo = [_PHOTO[0], {key: value for key, value in _PHOTO[1].items() if key != "file_size"}]

    inbound = await _deliver(bot_api, _update(photo=photo, caption=CAPTION))

    assert bot_api.get_file_ids == ["AgACAgQ-large"]
    [attachment] = inbound.file_attachments
    assert attachment.platform_file_id == "AgACAgQ-large"


@pytest.mark.asyncio
async def test_a_document_without_a_caption_reaches_the_router_as_a_file_only_message(bot_api, caplog):
    caplog.set_level(logging.DEBUG, logger="app.channels")

    inbound = await _deliver(bot_api, _update(document=_DOCUMENT))

    assert inbound.text == FILE_ONLY_PLACEHOLDER, "the placeholder a Teams file-only message gets"
    [attachment] = inbound.file_attachments
    assert (attachment.filename, attachment.content_type, attachment.size) == (FILE_NAME, "application/pdf", len(PDF))
    assert attachment.url == f"https://api.telegram.org/file/bot{TOKEN}/documents/file_7.pdf"
    logged = _logged(caplog)
    assert TOKEN not in logged and FILE_NAME not in logged and "payslip" not in logged


@pytest.mark.asyncio
async def test_the_router_downloads_and_stores_the_file_the_adapter_hands_it(bot_api, monkeypatch, caplog):
    """The attachment is the one the router already downloads for Slack, Teams and Discord."""
    from app.channels import router
    from app.services import file_storage

    caplog.set_level(logging.DEBUG, logger="app")
    stored: list = []
    monkeypatch.setattr(file_storage.file_storage, "_client", SimpleNamespace(put_object=lambda *args: stored.append(args)))
    inbound = await _deliver(bot_api, _update(document=_DOCUMENT, caption=CAPTION))
    rows: list = []
    db = SimpleNamespace(add=rows.append, flush=AsyncMock())

    [ref] = await router._resolve_channel_files(db, inbound, None)

    assert bot_api.downloads == ["documents/file_7.pdf"]
    assert len(stored) == 1
    [row] = rows
    assert (row.filename, row.content_type, row.size, row.source) == (FILE_NAME, "application/pdf", len(PDF), "channel:telegram")
    assert ref.file_id == str(row.id)
    logged = _logged(caplog)
    assert TOKEN not in logged and FILE_NAME not in logged


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caption", "offset", "stripped"),
    [
        (f"@{BOT} summarise this", 0, "summarise this"),
        # U+1F440 is two UTF-16 code units: Telegram's offset is 8, the str index 7.
        (f"Look \U0001f440 @{BOT} what is it?", 8, "Look \U0001f440  what is it?"),
    ],
    ids=["leading_mention", "mention_after_an_emoji"],
)
async def test_a_bot_mention_in_a_caption_is_detected_and_stripped(bot_api, caption, offset, stripped):
    entities = [{"type": "mention", "offset": offset, "length": len(BOT) + 1}]

    inbound = await _deliver(
        bot_api, _update(chat_type="supergroup", photo=_PHOTO, caption=caption, caption_entities=entities)
    )

    assert inbound.metadata["bot_was_mentioned"] is True
    assert inbound.text == stripped
    assert len(inbound.file_attachments) == 1


@pytest.mark.asyncio
async def test_a_caption_that_is_only_the_mention_gets_the_file_only_placeholder(bot_api):
    entities = [{"type": "mention", "offset": 0, "length": len(BOT) + 1}]

    inbound = await _deliver(bot_api, _update(chat_type="group", document=_DOCUMENT, caption=f"@{BOT}", caption_entities=entities))

    assert inbound.metadata["bot_was_mentioned"] is True
    assert inbound.text == FILE_ONLY_PLACEHOLDER


@pytest.mark.asyncio
async def test_a_caption_is_not_read_through_the_text_entities(bot_api):
    """``entities`` belong to a text message: a group photo whose caption does not mention the bot is not a mention."""
    inbound = await _deliver(
        bot_api,
        _update(
            chat_type="group",
            photo=_PHOTO,
            caption="Anyone seen this?",
            entities=[{"type": "mention", "offset": 0, "length": len(BOT) + 1}],
        ),
    )

    assert inbound.metadata["bot_was_mentioned"] is False
    assert inbound.text == "Anyone seen this?"


@pytest.mark.asyncio
async def test_a_mention_after_an_emoji_in_a_text_message_is_detected(bot_api):
    """The UTF-16 offsets hold for ``entities`` too: the mention was missed after any emoji."""
    text = f"\U0001f44b @{BOT} status?"

    inbound = await _deliver(
        bot_api, _update(chat_type="group", text=text, entities=[{"type": "mention", "offset": 3, "length": len(BOT) + 1}])
    )

    assert inbound.metadata["bot_was_mentioned"] is True
    assert inbound.text == "\U0001f44b  status?"
    assert inbound.file_attachments == []


@pytest.mark.asyncio
async def test_a_bot_command_in_a_caption_keeps_the_command(bot_api):
    caption = f"/summary@{BOT} please"
    entities = [{"type": "bot_command", "offset": 0, "length": len(f"/summary@{BOT}")}]

    inbound = await _deliver(bot_api, _update(chat_type="group", document=_DOCUMENT, caption=caption, caption_entities=entities))

    assert inbound.metadata["bot_was_mentioned"] is True
    assert inbound.text == "/summary please"


@pytest.mark.asyncio
async def test_a_bot_whose_name_starts_with_this_ones_is_another_bot(bot_api):
    """``@copilot_bot_backup`` is not ``@copilot_bot``: its command passes no gate and is not rewritten."""
    caption = f"/help@{BOT}_backup for this"
    entities = [{"type": "bot_command", "offset": 0, "length": len(f"/help@{BOT}_backup")}]

    inbound = await _deliver(bot_api, _update(chat_type="group", document=_DOCUMENT, caption=caption, caption_entities=entities))

    assert inbound.metadata["bot_was_mentioned"] is False
    assert inbound.text == caption


@pytest.mark.asyncio
async def test_only_the_bots_own_mention_is_stripped_next_to_a_longer_name(bot_api):
    """The mention is removed where its entity is, not wherever its text appears."""
    caption = f"@{BOT} compare with @{BOT}_backup"
    entities = [
        {"type": "mention", "offset": 0, "length": len(BOT) + 1},
        {"type": "mention", "offset": len(f"@{BOT} compare with "), "length": len(BOT) + 8},
    ]

    inbound = await _deliver(bot_api, _update(chat_type="group", document=_DOCUMENT, caption=caption, caption_entities=entities))

    assert inbound.metadata["bot_was_mentioned"] is True
    assert inbound.text == f"compare with @{BOT}_backup"


@pytest.mark.asyncio
async def test_a_voice_message_is_handed_over_as_audio(bot_api):
    voice = {"file_id": "AwACAgQ-voice", "file_unique_id": "v", "duration": 4, "mime_type": "audio/ogg", "file_size": 4}

    inbound = await _deliver(bot_api, _update(voice=voice))

    assert inbound.text == FILE_ONLY_PLACEHOLDER
    [attachment] = inbound.file_attachments
    assert (attachment.filename, attachment.content_type) == ("voice.ogg", "audio/ogg")


@pytest.mark.asyncio
async def test_a_file_larger_than_getfile_serves_is_not_requested(bot_api, caplog):
    caplog.set_level(logging.DEBUG, logger="app.channels")
    large = {**_DOCUMENT, "file_size": telegram.TELEGRAM_GETFILE_MAX_BYTES + 1}

    inbound = await _deliver(bot_api, _update(document=large, caption=CAPTION))

    assert inbound.text == CAPTION, "the caption still reaches the agent"
    assert inbound.file_attachments == []
    assert bot_api.get_file_ids == []
    assert any(r.getMessage().startswith("Telegram file too large") for r in caplog.records)


@pytest.mark.asyncio
async def test_a_file_getfile_refuses_leaves_the_message_to_the_router_without_it(bot_api, caplog):
    """As a file the router fails to download: the message goes on, its file left out."""
    caplog.set_level(logging.DEBUG, logger="app.channels")
    unknown = {**_DOCUMENT, "file_id": "BQACAgQ-gone"}

    inbound = await _deliver(bot_api, _update(document=unknown))

    assert inbound.text == FILE_ONLY_PLACEHOLDER
    assert inbound.file_attachments == []
    assert bot_api.get_file_ids == ["BQACAgQ-gone"]
    failures = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Failed to resolve Telegram file")]
    assert failures == ["Failed to resolve Telegram file: application/pdf (HTTP 400)"]
    assert TOKEN not in _logged(caplog)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        _update(sticker={"file_id": "CAACAgIAAxkBAAE", "file_unique_id": "k", "type": "regular", "emoji": "+"}),
        _update(new_chat_members=[{"id": 7, "is_bot": False, "first_name": "Sam"}]),
        _update(paid_media={"star_count": 5, "paid_media": []}, caption=CAPTION),
        _update(key="edited_message", photo=_PHOTO, caption=CAPTION, edit_date=1759140100),
        _update(photo=[{"file_unique_id": "no-id", "width": 90, "height": 60}], caption=CAPTION),
        [{"message": {"text": CAPTION}}],
    ],
    ids=["sticker", "service_message", "caption_on_paid_media", "edited_caption", "photo_without_file_id", "not_an_object"],
)
async def test_an_update_with_neither_text_nor_a_supported_file_is_ignored(bot_api, payload):
    assert await _deliver(bot_api, payload) is None
    telegram.handle_message.assert_not_awaited()
    assert bot_api.get_file_ids == []


@pytest.mark.asyncio
async def test_an_edited_text_message_is_still_processed(bot_api):
    inbound = await _deliver(bot_api, _update(key="edited_message", text="Correction: the VPN alert", edit_date=1759140100))

    assert inbound.text == "Correction: the VPN alert"
    assert inbound.file_attachments == []
