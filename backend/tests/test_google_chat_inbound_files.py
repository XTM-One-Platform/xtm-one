"""What a Google Chat message carrying files hands to the channel router.

The webhook read the message text only: a file uploaded with a message never
reached the agent, and a message carrying only a file was dropped. Content
uploaded to Chat has an ``attachmentDataRef``, whose ``resourceName``
``media.download`` serves to the app's own ``chat.bot`` credentials; a Drive
file has a ``driveDataRef`` instead, which ``media.download`` does not serve.

Pinned here, through the webhook and its processing task with Chat API shaped
events: an upload is an ``InboundFileAttachment`` the router downloads, with
the app's token, from the endpoint the Google integration reads; a message
carrying only files gets the file-only placeholder; a Drive file is named in
the text for a Google integration to read; a resource name that is not one is
left out, as is every file when no token can be had, while the message goes
on; and no line logs a file name.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from starlette.requests import Request

from app.channels import google_chat
from app.channels.base import FILE_ONLY_PLACEHOLDER, OutboundMessage

TOKEN = "ya29.chat-bot-token"
TEXT = "Can you summarise the attached board deck?"
FILE_NAME = "board-q3-confidential.pdf"
RESOURCE = "ClxzcGFjZXMvQUFBL21lc3NhZ2VzLzEuMS9hdHRhY2htZW50cy8y="
PDF = b"%PDF-1.7 board deck"
_RealAsyncClient = httpx.AsyncClient


def _request(payload: object) -> Request:
    body = json.dumps(payload).encode()
    scope = {"type": "http", "method": "POST", "path": "/api/channels/googlechat/webhook", "query_string": b"", "headers": []}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


class _FakeChannel:
    """A Chat client that sends nothing; its token is the fixture's."""

    token = TOKEN
    supports_typing = False

    def __init__(self, *_args, **_kwargs):
        pass

    async def _get_access_token(self) -> str:
        return type(self).token

    async def send_message(self, _outbound):
        return None

    async def send_typing_indicator(self, *_args):
        return None


@pytest.fixture
def chat(monkeypatch):
    state = SimpleNamespace(spawned=[])
    _FakeChannel.token = TOKEN
    monkeypatch.setattr(google_chat, "GoogleChatChannel", _FakeChannel)
    monkeypatch.setattr(google_chat, "_get_googlechat_creds", AsyncMock(return_value={"credentials_json": "{}"}))
    monkeypatch.setattr(google_chat, "handle_message", AsyncMock(return_value=OutboundMessage(text="", suppress=True)))
    monkeypatch.setattr(google_chat, "spawn_channel_task", lambda coro, **_kw: state.spawned.append(coro))
    return state


def _upload(resource: str = RESOURCE, name: str = FILE_NAME) -> dict:
    return {
        "name": "spaces/AAA/messages/1.1/attachments/2",
        "contentName": name,
        "contentType": "application/pdf",
        "attachmentDataRef": {"resourceName": resource},
        "thumbnailUri": "https://chat.google.com/api/get_attachment_url?thumbnail",
        "downloadUri": "https://chat.google.com/api/get_attachment_url?download",
        "source": "UPLOADED_CONTENT",
    }


_DRIVE_FILE = {
    "name": "spaces/AAA/messages/1.1/attachments/3",
    "contentName": "Roadmap 2027",
    "contentType": "application/vnd.google-apps.document",
    "driveDataRef": {"driveFileId": "1AbCdEfGhIjKlMnOpQrStUvWxYz"},
    "source": "DRIVE_FILE",
}


def _event(*, text: str | None = TEXT, attachments: list[dict] | None = None, space_type: str = "DM") -> dict:
    message = {
        "name": "spaces/AAA/messages/1.1",
        "sender": {"name": "users/123", "displayName": "Jane Placeholder", "email": "jane@example.com", "type": "HUMAN"},
        "thread": {"name": "spaces/AAA/threads/T1"},
    }
    if text is not None:
        message["text"] = text
        message["argumentText"] = text
    if attachments is not None:
        message["attachment"] = attachments
    return {"type": "MESSAGE", "space": {"name": "spaces/AAA", "type": space_type}, "message": message}


async def _deliver(state, payload: dict):
    """Post *payload* to the webhook, run its processing task, return what reached the router."""
    response = await google_chat.googlechat_webhook(_request(payload))
    assert response.status_code == 200
    if not state.spawned:
        return None
    assert len(state.spawned) == 1
    await state.spawned.pop()
    google_chat.handle_message.assert_awaited_once()
    return google_chat.handle_message.await_args.args[0]


def _info_and_above(caplog) -> str:
    return "\n".join(
        logging.Formatter("%(levelname)s %(message)s").format(r) for r in caplog.records if r.levelno >= logging.INFO
    )


@pytest.mark.asyncio
async def test_an_uploaded_file_reaches_the_router_with_the_apps_token(chat, caplog):
    caplog.set_level(logging.DEBUG, logger="app.channels")

    inbound = await _deliver(chat, _event(attachments=[_upload()]))

    assert inbound.text == TEXT
    [attachment] = inbound.file_attachments
    assert attachment.url == f"https://chat.googleapis.com/v1/media/{RESOURCE}?alt=media"
    assert (attachment.filename, attachment.content_type, attachment.platform_file_id) == (
        FILE_NAME,
        "application/pdf",
        RESOURCE,
    )
    assert attachment.auth_headers == {"Authorization": f"Bearer {TOKEN}"}
    info = _info_and_above(caplog)
    assert FILE_NAME not in info and TOKEN not in info and "board" not in info.lower()


def test_the_download_url_is_the_one_the_google_integration_reads():
    from app.channels import router
    from app.integrations.providers.google import chat_media_url

    assert chat_media_url(RESOURCE) == f"https://chat.googleapis.com/v1/media/{RESOURCE}"
    assert chat_media_url(RESOURCE).startswith(router._REDIRECTING_CONTENT_ENDPOINTS), (
        "the router follows the redirect a Chat media download may answer"
    )
    for forged in ("", "../spaces/AAA", "spaces/../../v1/spaces", "a b", "spaces/AAA?x=1"):
        assert chat_media_url(forged) is None, forged


@pytest.mark.asyncio
async def test_a_message_carrying_only_a_file_gets_the_file_only_placeholder(chat):
    inbound = await _deliver(chat, _event(text=None, attachments=[_upload()]))

    assert inbound.text == FILE_ONLY_PLACEHOLDER
    assert len(inbound.file_attachments) == 1


@pytest.mark.asyncio
async def test_a_drive_file_is_named_for_a_google_integration_to_read(chat):
    inbound = await _deliver(chat, _event(attachments=[_DRIVE_FILE]))

    assert inbound.file_attachments == [], "media.download does not serve a Drive file"
    first, drive_line = inbound.text.split("\n")
    assert first == TEXT
    assert "Roadmap 2027" in drive_line and "1AbCdEfGhIjKlMnOpQrStUvWxYz" in drive_line
    assert "download_google_drive_file" in drive_line


@pytest.mark.asyncio
async def test_a_message_carrying_only_a_drive_file_is_still_for_the_agent(chat):
    inbound = await _deliver(chat, _event(text=None, attachments=[_DRIVE_FILE]))

    assert inbound.text.startswith(FILE_ONLY_PLACEHOLDER + "\n[Google Drive file: Roadmap 2027")


@pytest.mark.asyncio
async def test_a_resource_name_that_is_not_one_is_left_out(chat, caplog):
    caplog.set_level(logging.DEBUG, logger="app.channels")

    inbound = await _deliver(chat, _event(attachments=[_upload("../../spaces/AAA/members"), _upload()]))

    assert inbound.text == TEXT
    assert [a.platform_file_id for a in inbound.file_attachments] == [RESOURCE]
    assert any(r.getMessage().startswith("Google Chat file left out") for r in caplog.records)


@pytest.mark.asyncio
async def test_without_a_token_the_files_are_left_out_and_the_message_goes_on(chat, caplog):
    caplog.set_level(logging.DEBUG, logger="app.channels")
    _FakeChannel.token = ""

    inbound = await _deliver(chat, _event(attachments=[_upload()]))

    assert inbound.text == TEXT
    assert inbound.file_attachments == []
    assert any("left out - no access token" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_the_router_downloads_and_stores_the_upload(chat, monkeypatch):
    """The attachment is the one the router already downloads for Slack, Teams and Telegram."""
    from app.channels import router
    from app.services import file_storage

    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "application/pdf"}, stream=httpx.ByteStream(PDF))

    class _Client(_RealAsyncClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, transport=httpx.MockTransport(handle), **kwargs)

    stored: list = []
    monkeypatch.setattr(file_storage.file_storage, "_client", SimpleNamespace(put_object=lambda *args: stored.append(args)))
    inbound = await _deliver(chat, _event(attachments=[_upload()]))
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    rows: list = []
    db = SimpleNamespace(add=rows.append, flush=AsyncMock())

    [ref] = await router._resolve_channel_files(db, inbound, None)

    [request] = requests
    assert str(request.url) == f"https://chat.googleapis.com/v1/media/{RESOURCE}?alt=media"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    [row] = rows
    assert (row.filename, row.content_type, row.size, row.source) == (FILE_NAME, "application/pdf", len(PDF), "channel:google_chat")
    assert ref.file_id == str(row.id) and len(stored) == 1


@pytest.mark.asyncio
async def test_a_message_with_neither_text_nor_files_is_still_ignored(chat):
    assert await _deliver(chat, _event(text=None, attachments=[{"name": "x", "contentName": "y"}])) is None
    google_chat.handle_message.assert_not_awaited()
