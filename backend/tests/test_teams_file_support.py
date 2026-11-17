"""Tests for Teams file read/upload support (channel bot + microsoft provider).

Covers:
- Inbound Bot Framework attachment extraction (personal ``file.download.info``
  with its pre-authenticated downloadUrl, group-chat/channel ``reference``
  attachments rewritten to the Graph /shares content endpoint, cards skipped)
- Graph /shares URL encoding (unpadded base64url + ``u!`` prefix)
- Graph token application restricted to self-constructed /shares URLs
- [[FILE:id]] marker rewriting for external channels (web renders cards;
  channels previously leaked the raw marker text)
- teams_bot download/upload tools (persist + [[FILE:id]] contract,
  filesFolder upload + link announcement)
- microsoft provider attachment surfacing helper
"""

import base64
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.channels.base import resolve_channel_file_markers
from app.channels.teams import (
    _FILE_ONLY_PLACEHOLDER,
    _apply_graph_attachment_auth_headers,
    _extract_graph_message_attachments,
    _extract_teams_file_attachments,
    _fetch_graph_inbound_message,
    _graph_share_download_url,
)
from app.integrations.providers import teams_bot
from app.integrations.providers.microsoft._teams import _format_message_attachments
from app.integrations.providers.teams_bot import GRAPH_API
from app.llm.base import FileRef
from app.tools.base import CallableToolExecutor, FileContext, ToolResult
from tests.integration_file_fakes import HOSTILE_FILE_NAMES, assert_name_only_inside, fenced_and_open

# ---------------------------------------------------------------------------
# Inbound webhook attachment extraction
# ---------------------------------------------------------------------------


def test_extract_personal_file_download_info():
    """1:1 file attachments (previously skipped by the vnd.microsoft prefix
    filter) must be extracted with their pre-authenticated downloadUrl."""
    data = {
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.teams.file.download.info",
                "contentUrl": "https://contoso-my.sharepoint.com/personal/x/file.pptx",
                "name": "roadmap.pptx",
                "content": {
                    "downloadUrl": "https://download.example/pre-authed",
                    "uniqueId": "1150D938-8870-4044-9F2C-5BBDEBA70C9D",
                    "fileType": "pptx",
                },
            }
        ]
    }
    atts = _extract_teams_file_attachments(data)
    assert len(atts) == 1
    att = atts[0]
    assert att.url == "https://download.example/pre-authed"
    assert att.filename == "roadmap.pptx"
    assert att.auth_headers == {}  # pre-authenticated — no token needed
    assert att.platform_file_id == "1150D938-8870-4044-9F2C-5BBDEBA70C9D"
    assert "presentation" in att.content_type  # guessed from .pptx


def test_extract_group_chat_reference_attachment():
    """Group-chat/channel files arrive as ``reference`` attachments pointing
    at SharePoint — rewritten to the Graph /shares content endpoint."""
    sp_url = "https://contoso.sharepoint.com/sites/x/Shared Documents/roadmap.pptx"
    data = {
        "attachments": [
            {"contentType": "reference", "contentUrl": sp_url, "name": "roadmap.pptx"}
        ]
    }
    atts = _extract_teams_file_attachments(data)
    assert len(atts) == 1
    att = atts[0]
    assert att.url.startswith(f"{GRAPH_API}/shares/u!")
    assert att.url.endswith("/driveItem/content")
    assert att.filename == "roadmap.pptx"


def test_extract_skips_cards_and_html_previews():
    data = {
        "attachments": [
            {"contentType": "application/vnd.microsoft.card.adaptive", "content": {}},
            {"contentType": "application/vnd.microsoft.card.hero", "content": {}},
            {"contentType": "text/html", "content": "<div>file card</div>"},
        ]
    }
    assert _extract_teams_file_attachments(data) == []


def test_extract_keeps_inline_images():
    data = {
        "attachments": [
            {
                "contentType": "image/png",
                "contentUrl": "https://smba.trafficmanager.net/amer/v3/attachments/x/views/original",
                "name": "pasted.png",
            }
        ]
    }
    atts = _extract_teams_file_attachments(data)
    assert len(atts) == 1
    assert atts[0].content_type == "image/png"


def test_graph_share_download_url_encoding():
    """Encoding per the Graph shares API: u! + unpadded base64url."""
    url = "https://contoso.sharepoint.com/sites/x/doc.pptx"
    result = _graph_share_download_url(url)
    prefix = f"{GRAPH_API}/shares/u!"
    assert result.startswith(prefix)
    assert result.endswith("/driveItem/content")
    token = result[len(prefix):].removesuffix("/driveItem/content")
    assert "=" not in token and "+" not in token and "/" not in token
    # Round-trip: base64url-decode back to the original URL.
    padded = token.replace("_", "/").replace("-", "+")
    padded += "=" * (-len(padded) % 4)
    assert base64.b64decode(padded).decode("utf-8") == url


def test_graph_auth_headers_only_on_share_urls():
    from app.channels.base import InboundFileAttachment

    graph_att = InboundFileAttachment(url=f"{GRAPH_API}/shares/u!abc/driveItem/content")
    external_att = InboundFileAttachment(url="https://attacker.example.com/shares/x")
    preauthed_att = InboundFileAttachment(url="https://download.example/pre-authed")
    applied = _apply_graph_attachment_auth_headers(
        [graph_att, external_att, preauthed_att], "graph-token"
    )
    assert applied == 1
    assert graph_att.auth_headers == {"Authorization": "Bearer graph-token"}
    assert external_att.auth_headers == {}
    assert preauthed_att.auth_headers == {}


# ---------------------------------------------------------------------------
# Graph chatMessage attachment extraction (group chats / private channels)
# ---------------------------------------------------------------------------


def test_extract_graph_message_attachments_reference():
    """Graph chatMessage ``reference`` attachments are rewritten to the
    /shares content endpoint — the fallback for group chats / channels
    where the Bot Framework activity carries only the HTML stub."""
    sp_url = "https://contoso.sharepoint.com/sites/x/Shared Documents/report.pdf"
    msg = {
        "attachments": [
            {
                "id": "att-1",
                "contentType": "reference",
                "contentUrl": sp_url,
                "name": "report.pdf",
            },
            # Cards and inline stubs must be skipped.
            {"id": "att-2", "contentType": "application/vnd.microsoft.card.adaptive"},
            {"id": "att-3", "contentType": "reference", "contentUrl": "http://insecure"},
        ]
    }
    atts = _extract_graph_message_attachments(msg)
    assert len(atts) == 1
    att = atts[0]
    assert att.url.startswith(f"{GRAPH_API}/shares/u!")
    assert att.url.endswith("/driveItem/content")
    assert att.filename == "report.pdf"
    assert att.platform_file_id == "att-1"
    assert att.content_type == "application/pdf"


def test_extract_graph_message_attachments_empty():
    assert _extract_graph_message_attachments({}) == []
    assert _extract_graph_message_attachments({"attachments": None}) == []
    assert _extract_graph_message_attachments({"attachments": ["junk", 42]}) == []


@pytest.mark.asyncio
async def test_fetch_graph_inbound_message_group_chat_url():
    """Group-chat fallback fetches /chats/{conv-id}/messages/{activity-id}."""
    from app.channels.base import ChannelType, InboundMessage

    inbound = InboundMessage(
        channel=ChannelType.TEAMS,
        text="hi",
        channel_thread_id="19:abc@thread.v2",
        channel_message_id="1751541567890",
        metadata={"conversation_type": "groupChat"},
    )
    captured: dict = {}

    class _FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "1751541567890"}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, timeout=None):
            captured["url"] = url
            captured["headers"] = headers
            return _FakeResp()

    with patch("app.channels.teams.httpx.AsyncClient", return_value=_FakeClient()):
        msg = await _fetch_graph_inbound_message(inbound, "graph-token")

    assert msg == {"id": "1751541567890"}
    assert captured["url"] == f"{GRAPH_API}/chats/19:abc@thread.v2/messages/1751541567890"
    assert captured["headers"] == {"Authorization": "Bearer graph-token"}


@pytest.mark.asyncio
async def test_fetch_graph_inbound_message_channel_tries_replies_first():
    """Channel replies live under the thread-root /replies endpoint."""
    from app.channels.base import ChannelType, InboundMessage

    inbound = InboundMessage(
        channel=ChannelType.TEAMS,
        text="hi",
        channel_thread_id="19:chan@thread.tacv2;messageid=111",
        channel_message_id="222",
        metadata={
            "conversation_type": "channel",
            "team_id": "team-guid",
            "channel_id": "19:chan@thread.tacv2",
            "reply_to_id": "111",
        },
    )
    urls: list[str] = []

    class _FakeResp:
        status_code = 200

        @staticmethod
        def json():
            return {"id": "222"}

    class _FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None, timeout=None):
            urls.append(url)
            return _FakeResp()

    with patch("app.channels.teams.httpx.AsyncClient", return_value=_FakeClient()):
        msg = await _fetch_graph_inbound_message(inbound, "tok")

    assert msg == {"id": "222"}
    assert urls[0] == (
        f"{GRAPH_API}/teams/team-guid/channels/19:chan@thread.tacv2"
        "/messages/111/replies/222"
    )


@pytest.mark.asyncio
async def test_fetch_graph_inbound_message_personal_skipped():
    """Personal chats already deliver files via Bot Framework — no fallback."""
    from app.channels.base import ChannelType, InboundMessage

    inbound = InboundMessage(
        channel=ChannelType.TEAMS,
        text="hi",
        channel_thread_id="a:1personal",
        channel_message_id="333",
        metadata={"conversation_type": "personal"},
    )
    assert await _fetch_graph_inbound_message(inbound, "tok") is None


def test_file_only_placeholder_is_nonempty():
    assert _FILE_ONLY_PLACEHOLDER.strip()


# ---------------------------------------------------------------------------
# [[FILE:id]] marker rewriting for external channels
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows
        self.queries = []

    async def execute(self, query):
        self.queries.append(query)
        return _FakeResult(self._rows)


_SCOPE = {"user_id": uuid.uuid4()}


@pytest.mark.asyncio
async def test_file_markers_become_signed_links():
    file_id = uuid.uuid4()
    db = _FakeDb([SimpleNamespace(id=file_id, filename="report.pdf")])
    text = f"Here is your report. [[FILE:{file_id}]]"
    out = await resolve_channel_file_markers(
        db, text, "https://copilot.example", **_SCOPE
    )
    assert "[[FILE" not in out
    assert "[report.pdf](https://copilot.example/api/v1/chat/files/" in out
    assert "token=" in out and "expires=" in out


@pytest.mark.asyncio
async def test_marker_lookup_is_scope_filtered():
    """The DB lookup must be restricted to the caller's conversation/user —
    an LLM-emitted marker must never sign a link for an arbitrary UUID."""
    conv_id = uuid.uuid4()
    user_id = uuid.uuid4()
    db = _FakeDb([])
    await resolve_channel_file_markers(
        db,
        f"[[FILE:{uuid.uuid4()}]]",
        "https://copilot.example",
        conversation_id=conv_id,
        user_id=user_id,
    )
    assert len(db.queries) == 1
    compiled = str(db.queries[0])
    assert "file_attachments.conversation_id" in compiled
    assert "file_attachments.user_id" in compiled


@pytest.mark.asyncio
async def test_no_scope_strips_all_markers_without_db_lookup():
    """With no conversation/user scope nothing may be signed — fail closed."""
    file_id = uuid.uuid4()
    db = _FakeDb([SimpleNamespace(id=file_id, filename="report.pdf")])
    out = await resolve_channel_file_markers(
        db, f"Take it: [[FILE:{file_id}]]", "https://copilot.example"
    )
    assert "[[FILE" not in out
    assert "token=" not in out
    assert db.queries == []


@pytest.mark.asyncio
async def test_unknown_file_marker_is_stripped():
    db = _FakeDb([])
    text = f"Result: [[FILE:{uuid.uuid4()}]] done."
    out = await resolve_channel_file_markers(
        db, text, "https://copilot.example", **_SCOPE
    )
    assert "[[FILE" not in out
    assert "done." in out


@pytest.mark.asyncio
async def test_text_without_markers_untouched():
    db = _FakeDb([])
    text = "No markers here."
    assert (
        await resolve_channel_file_markers(
            db, text, "https://copilot.example", **_SCOPE
        )
        == text
    )


@pytest.mark.asyncio
async def test_markdown_metacharacters_stripped_from_link_text():
    """Filenames with Markdown metacharacters must not break the link syntax
    or inject markup into external channels."""
    file_id = uuid.uuid4()
    db = _FakeDb(
        [SimpleNamespace(id=file_id, filename="evil](https://attacker.example)[.pdf")]
    )
    text = f"[[FILE:{file_id}]]"
    out = await resolve_channel_file_markers(
        db, text, "https://copilot.example", **_SCOPE
    )
    # Brackets are stripped from the display text; parens alone cannot close
    # the link text, so the attacker URL stays inert inside it.
    assert out.startswith("[evil(https://attacker.example)")
    assert "](https://copilot.example/api/v1/chat/files/" in out
    assert "[.pdf" not in out


@pytest.mark.asyncio
async def test_all_metacharacter_filename_falls_back_to_file():
    file_id = uuid.uuid4()
    db = _FakeDb([SimpleNamespace(id=file_id, filename="[]`\\")])
    out = await resolve_channel_file_markers(
        db, f"[[FILE:{file_id}]]", "https://copilot.example", **_SCOPE
    )
    assert out.startswith("[file](")


# ---------------------------------------------------------------------------
# teams_bot file tools
# ---------------------------------------------------------------------------


def _make_executor(**kwargs) -> CallableToolExecutor:
    executor = CallableToolExecutor()
    teams_bot.build_tools("tenant-123||client-abc||secret-xyz", executor, **kwargs)
    return executor


class _Recorder:
    def __init__(self, responses):
        self.calls = []
        self._responses = responses

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append(
            {"method": method, "url": url, "json": kwargs.get("json"),
             "content": kwargs.get("content"), "kwargs": kwargs}
        )
        payload = self._responses.pop(0) if self._responses else {}
        resp = AsyncMock()
        resp.json = lambda p=payload: p
        resp.content = payload.get("_raw", b"") if isinstance(payload, dict) else b""
        return resp, None


def _patched(recorder):
    return (
        patch.object(teams_bot, "_acquire_token", new=AsyncMock(return_value="tok")),
        patch.object(
            teams_bot,
            "_get_service_url",
            new=AsyncMock(return_value="https://smba.trafficmanager.net/amer"),
        ),
        patch.object(teams_bot, "safe_request", new=recorder),
    )


@pytest.mark.asyncio
async def test_download_teams_bot_file_persists_and_returns_marker():
    persisted = {}

    async def _persist(filename, raw, mime):
        persisted["args"] = (filename, raw, mime)
        return FileRef.from_bytes(data=raw, filename=filename, content_type=mime)

    executor = _make_executor(persist_file_fn=_persist)
    recorder = _Recorder(
        [
            {"id": "item1", "name": "roadmap.pptx", "size": 5,
             "file": {"mimeType": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}},
            {"_raw": b"bytes"},
        ]
    )
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        result = await executor._handlers["download_teams_bot_file"](
            file_url="https://contoso.sharepoint.com/sites/x/roadmap.pptx"
        )

    assert isinstance(result, ToolResult)
    assert "[[FILE:" in result.content
    assert result.files and result.files[0].filename == "roadmap.pptx"
    assert persisted["args"][1] == b"bytes"
    urls = [c["url"] for c in recorder.calls]
    assert urls[0].startswith(f"{GRAPH_API}/shares/u!")
    assert urls[1].endswith("/driveItem/content")


@pytest.mark.asyncio
async def test_upload_teams_bot_file_to_channel():
    ref = FileRef.from_bytes(
        data=b"pdf-bytes", filename="report.pdf", content_type="application/pdf"
    )
    file_context = FileContext.from_file_refs([ref])

    executor = _make_executor()
    recorder = _Recorder(
        [
            # GET filesFolder
            {"id": "folder1", "parentReference": {"driveId": "drive1"}},
            # PUT content
            {"id": "item1", "webUrl": "https://contoso.sharepoint.com/x/report.pdf"},
            # Bot Framework announcement (createConversation)
            {"activityId": "act-1"},
        ]
    )
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        result = await executor._handlers["upload_teams_bot_file_to_channel"](
            team_id="team-1",
            channel_id="19:chan@thread.tacv2",
            file_id=ref.file_id,
            message="Here is the report",
            file_context=file_context,
        )

    inside, outside = fenced_and_open(result)
    assert outside.startswith(f"Uploaded file {ref.file_id} (0.0 KB) to the channel's Files folder.")
    assert "report.pdf" not in outside
    assert "Name: report.pdf" in inside.splitlines()
    assert "Web URL: https://contoso.sharepoint.com/x/report.pdf" in inside.splitlines()
    urls = [c["url"] for c in recorder.calls]
    assert urls[0].endswith("/teams/team-1/channels/19:chan@thread.tacv2/filesFolder")
    assert "/drives/drive1/items/folder1:" in urls[1]
    assert recorder.calls[1]["content"] == b"pdf-bytes"


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_upload_teams_bot_file_to_channel_fences_the_stored_name(hostile):
    """The stored name is a third party's text; the answer names the file by its id."""
    name = HOSTILE_FILE_NAMES[hostile]
    ref = FileRef.from_bytes(data=b"pdf-bytes", filename=name, content_type="application/pdf")
    file_context = FileContext.from_file_refs([ref])
    executor = _make_executor()
    recorder = _Recorder(
        [
            {"id": "folder1", "parentReference": {"driveId": "drive1"}},
            {"id": "item1", "webUrl": "https://contoso.sharepoint.com/x/PWNED.pdf"},
            {"activityId": "act-1"},
        ]
    )
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        result = await executor._handlers["upload_teams_bot_file_to_channel"](
            team_id="team-1",
            channel_id="19:chan@thread.tacv2",
            file_id=ref.file_id,
            file_context=file_context,
        )

    outside = assert_name_only_inside(result, name)
    assert outside.startswith(f"Uploaded file {ref.file_id} (0.0 KB) to the channel's Files folder.")


@pytest.mark.asyncio
async def test_download_oversized_content_rejected_post_download():
    """The byte cap must hold even when Graph metadata omits/zeros size."""
    executor = _make_executor()
    recorder = _Recorder(
        [
            # Metadata WITHOUT a size field — the pre-check cannot trigger.
            {"id": "item1", "name": "big.bin", "file": {}},
            {"_raw": b"x" * 32},
        ]
    )
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3, patch.object(teams_bot, "_TEAMS_MAX_FILE_BYTES", 16):
        result = await executor._handlers["download_teams_bot_file"](
            file_url="https://contoso.sharepoint.com/sites/x/big.bin"
        )
    assert isinstance(result, str)
    assert result.startswith("Error: file is")


@pytest.mark.asyncio
async def test_upload_announcement_sanitizes_filename():
    """The Bot Framework announcement embeds the filename as Markdown link
    text — metacharacters must be stripped so the link cannot be broken."""
    ref = FileRef.from_bytes(
        data=b"x", filename="evil](https://attacker.example).pdf",
        content_type="application/pdf",
    )
    file_context = FileContext.from_file_refs([ref])

    executor = _make_executor()
    recorder = _Recorder(
        [
            {"id": "folder1", "parentReference": {"driveId": "drive1"}},
            {"id": "item1", "webUrl": "https://contoso.sharepoint.com/x/e.pdf"},
            {"activityId": "act-1"},
        ]
    )
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        await executor._handlers["upload_teams_bot_file_to_channel"](
            team_id="team-1",
            channel_id="19:chan@thread.tacv2",
            file_id=ref.file_id,
            file_context=file_context,
        )

    announcement = recorder.calls[2]["json"]["activity"]["text"]
    # Brackets stripped from the display text — the injected "]" cannot
    # close the link early and the whole name stays inside one link.
    assert "[evil(https://attacker.example).pdf](" in announcement
    assert "evil]" not in announcement


@pytest.mark.asyncio
async def test_upload_percent_encodes_filename():
    """LLM/user-controlled filenames with spaces, '#', '%' or '/' must be
    percent-encoded into a single path segment of the Graph upload URL."""
    ref = FileRef.from_bytes(
        data=b"x", filename="Q3 report #final/2026.pdf",
        content_type="application/pdf",
    )
    file_context = FileContext.from_file_refs([ref])

    executor = _make_executor()
    recorder = _Recorder(
        [
            {"id": "folder1", "parentReference": {"driveId": "drive1"}},
            {"id": "item1", "webUrl": "https://contoso.sharepoint.com/x/q3.pdf"},
            {"activityId": "act-1"},
        ]
    )
    p1, p2, p3 = _patched(recorder)
    with p1, p2, p3:
        await executor._handlers["upload_teams_bot_file_to_channel"](
            team_id="team-1",
            channel_id="19:chan@thread.tacv2",
            file_id=ref.file_id,
            file_context=file_context,
        )

    put_url = recorder.calls[1]["url"]
    assert "/items/folder1:/Q3%20report%20%23final%2F2026.pdf:/content" in put_url


@pytest.mark.asyncio
async def test_upload_without_file_context_fails_cleanly():
    executor = _make_executor()
    result = await executor._handlers["upload_teams_bot_file_to_channel"](
        team_id="t", channel_id="c", file_id="x", file_context=None
    )
    assert result.startswith("Error: no file context")


# ---------------------------------------------------------------------------
# microsoft provider file downloads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_microsoft_download_follows_redirects_and_returns_marker():
    """Graph /content 302s to a pre-authenticated location — the byte
    download must follow the redirect (like the text-read path) and hand
    back a reusable platform file with a [[FILE:id]] marker."""
    from app.integrations.providers import microsoft as ms

    executor = CallableToolExecutor()
    recorder = _Recorder(
        [
            {"id": "item1", "name": "roadmap.pptx", "size": 5,
             "parentReference": {"driveId": "drive1"},
             "file": {"mimeType": "application/vnd.openxmlformats-officedocument.presentationml.presentation"}},
            {"_raw": b"bytes"},
        ]
    )
    with patch.object(ms._common, "safe_request", new=recorder):
        ms.build_tools("dummy-token", executor)
        result = await executor._handlers["download_microsoft_file"](
            url="https://contoso.sharepoint.com/sites/x/roadmap.pptx"
        )

    assert isinstance(result, ToolResult)
    assert "[[FILE:" in result.content
    assert result.files and result.files[0].filename == "roadmap.pptx"
    content_call = recorder.calls[1]
    assert content_call["url"].endswith("/drives/drive1/items/item1/content")
    assert content_call["kwargs"].get("follow_redirects") is True


@pytest.mark.asyncio
async def test_microsoft_download_oversized_content_rejected_post_download():
    """The byte cap must hold even when Graph metadata omits/zeros size."""
    from app.integrations.providers import microsoft as ms

    executor = CallableToolExecutor()
    recorder = _Recorder(
        [
            # /shares driveItem metadata WITHOUT a size field.
            {"id": "item1", "name": "big.bin",
             "parentReference": {"driveId": "drive1"}},
            {"_raw": b"x" * 32},
        ]
    )
    with (
        patch.object(ms._common, "safe_request", new=recorder),
        patch.object(ms.files, "_MS_MAX_FILE_BYTES", 16),
    ):
        ms.build_tools("dummy-token", executor)
        result = await executor._handlers["download_microsoft_file"](
            url="https://contoso.sharepoint.com/sites/x/big.bin"
        )
    assert isinstance(result, str)
    assert result.startswith("Error: file is")


@pytest.mark.asyncio
async def test_microsoft_drive_upload_filename_is_single_segment():
    """A '/' in the LLM/user-controlled filename must be percent-encoded so
    it cannot inject extra path segments — only the explicit ``folder``
    argument keeps '/' as a separator."""
    from app.integrations.providers import microsoft as ms

    ref = FileRef.from_bytes(
        data=b"x", filename="../evil/2026 #report.pdf",
        content_type="application/pdf",
    )
    file_context = FileContext.from_file_refs([ref])

    executor = CallableToolExecutor()
    recorder = _Recorder(
        [{"id": "item1", "webUrl": "https://contoso-my.sharepoint.com/x/r.pdf"}]
    )
    with patch.object(ms._common, "safe_request", new=recorder):
        ms.build_tools("dummy-token", executor)
        result = await executor._handlers["upload_microsoft_drive_file"](
            file_id=ref.file_id,
            folder="Reports/Q3",
            file_context=file_context,
        )

    assert "Uploaded" in result
    put_url = recorder.calls[0]["url"]
    # Folder keeps its '/' separator; the filename is one encoded segment.
    assert "/root:/Reports/Q3/" in put_url
    assert "..%2Fevil%2F2026%20%23report.pdf:/content" in put_url


# ---------------------------------------------------------------------------
# microsoft provider file-card sends
# ---------------------------------------------------------------------------


# The Graph answers that share a chat file with the chat's members:
# the sender (/me), the chat's members, the invite, the permissions read back.
_CHAT_SHARE_RESPONSES = [
    {"id": "sender-1"},
    {"value": [
        {"id": "mm-a", "userId": "sender-1", "email": "agent@contoso.com"},
        {"id": "mm-b", "userId": "member-2", "email": "ada@contoso.com", "displayName": "Ada"},
    ]},
    {"value": []},
    {"value": [{"roles": ["read"], "grantedToV2": {"user": {"id": "member-2"}}}]},
]


@pytest.mark.asyncio
async def test_send_chat_file_fails_clearly_without_etag_guid():
    """A driveItem without an eTag GUID cannot render a native Teams file
    card — the send must fail with a clear error instead of posting a
    broken payload with a made-up attachment id."""
    from app.integrations.providers import microsoft as ms

    ref = FileRef.from_bytes(
        data=b"pdf", filename="report.pdf", content_type="application/pdf"
    )
    file_context = FileContext.from_file_refs([ref])

    executor = CallableToolExecutor()
    recorder = _Recorder(
        [
            # PUT upload — no eTag in the response
            {"id": "item1", "webUrl": "https://contoso.sharepoint.com/x/report.pdf"},
            # GET metadata — still no eTag
            {"id": "item1", "webUrl": "https://contoso.sharepoint.com/x/report.pdf"},
            *_CHAT_SHARE_RESPONSES,
        ]
    )
    with patch.object(ms._common, "safe_request", new=recorder):
        ms.build_tools("dummy-token", executor)
        result = await executor._handlers["send_microsoft_chat_file"](
            chat_id="19:chat@thread.v2",
            file_id=ref.file_id,
            file_context=file_context,
        )

    assert result.startswith("Error:")
    assert "eTag" in result
    # Upload, metadata fetch and the chat share, then no chatMessage POST.
    assert [c["method"] for c in recorder.calls] == ["put", "get", "get", "get", "post", "get"]
    assert not any(c["url"].endswith("/messages") for c in recorder.calls)


@pytest.mark.asyncio
async def test_send_files_encode_filename_as_single_segment():
    """send_microsoft_chat_file / send_microsoft_channel_file must keep a
    '/'-bearing filename as ONE encoded path segment in the upload URL."""
    from app.integrations.providers import microsoft as ms

    ref = FileRef.from_bytes(
        data=b"pdf", filename="nested/report.pdf", content_type="application/pdf"
    )
    file_context = FileContext.from_file_refs([ref])
    item = {
        "id": "item1",
        "name": "report.pdf",
        "eTag": '"{11111111-2222-3333-4444-555555555555},1"',
        "webUrl": "https://contoso.sharepoint.com/x/report.pdf",
        "webDavUrl": "https://contoso.sharepoint.com/x/dav/report.pdf",
    }

    executor = CallableToolExecutor()
    chat_recorder = _Recorder([item, item, {"id": "msg1"}])
    with patch.object(ms._common, "safe_request", new=chat_recorder):
        ms.build_tools("dummy-token", executor)
        await executor._handlers["send_microsoft_chat_file"](
            chat_id="19:chat@thread.v2",
            file_id=ref.file_id,
            file_context=file_context,
        )
    assert "/Microsoft Teams Chat Files/nested%2Freport.pdf:/content" in (
        chat_recorder.calls[0]["url"]
    )

    executor = CallableToolExecutor()
    channel_recorder = _Recorder(
        [
            {"id": "folder1", "parentReference": {"driveId": "drive1"}},
            item,
            item,
            {"id": "msg1"},
        ]
    )
    with patch.object(ms._common, "safe_request", new=channel_recorder):
        ms.build_tools("dummy-token", executor)
        await executor._handlers["send_microsoft_channel_file"](
            team_id="team-1",
            channel_id="19:chan@thread.tacv2",
            file_id=ref.file_id,
            file_context=file_context,
        )
    assert "/items/folder1:/nested%2Freport.pdf:/content" in (
        channel_recorder.calls[1]["url"]
    )


@pytest.mark.asyncio
async def test_send_chat_file_posts_native_file_card():
    from app.integrations.providers import microsoft as ms

    ref = FileRef.from_bytes(
        data=b"pdf", filename="report.pdf", content_type="application/pdf"
    )
    file_context = FileContext.from_file_refs([ref])
    item = {
        "id": "item1",
        "name": "report.pdf",
        "eTag": '"{11111111-2222-3333-4444-555555555555},1"',
        "webUrl": "https://contoso.sharepoint.com/x/report.pdf",
        "webDavUrl": "https://contoso.sharepoint.com/x/dav/report.pdf",
    }

    executor = CallableToolExecutor()
    recorder = _Recorder([item, item, *_CHAT_SHARE_RESPONSES, {"id": "msg1"}])
    with patch.object(ms._common, "safe_request", new=recorder):
        ms.build_tools("dummy-token", executor)
        result = await executor._handlers["send_microsoft_chat_file"](
            chat_id="19:chat@thread.v2",
            file_id=ref.file_id,
            message="here you go",
            file_context=file_context,
        )

    assert "sent to the chat" in result
    assert "Read access granted to the chat's 1 other member(s)" in result
    # put, metadata, /me, members, invite, permissions read-back, then the message.
    assert recorder.calls[4]["url"].endswith("/me/drive/items/item1/invite")
    post = recorder.calls[6]
    assert post["url"].endswith("/me/chats/19:chat@thread.v2/messages")
    attachment = post["json"]["attachments"][0]
    assert attachment["id"] == "11111111-2222-3333-4444-555555555555"
    assert attachment["contentUrl"] == item["webDavUrl"]
    assert 'attachment id="11111111-2222-3333-4444-555555555555"' in (
        post["json"]["body"]["content"]
    )


_STRUCTURED_MESSAGE = (
    "**Weekly digest** for the team.\n\n"
    "Second paragraph with a [link](https://example.com/report).\n\n"
    "- first point\n"
    "- second point\n\n"
    "| Area | State |\n"
    "| --- | --- |\n"
    "| Backend | green |\n"
)
_FILE_ITEM = {
    "id": "item1",
    "name": "report.pdf",
    "eTag": '"{11111111-2222-3333-4444-555555555555},1"',
    "webUrl": "https://contoso.sharepoint.com/x/report.pdf",
    "webDavUrl": "https://contoso.sharepoint.com/x/dav/report.pdf",
}
_FILE_CARD_TAG = '<attachment id="11111111-2222-3333-4444-555555555555"></attachment>'


def _plain_message_html(message: str) -> str:
    """The HTML the plain message tools post for *message* (no mention, no card)."""
    from app.services.teams_format import build_graph_chat_body

    return build_graph_chat_body(message)["content"]


def test_file_tools_and_plain_message_tools_share_one_conversion():
    """The file tools and the plain message tools convert their text with one function."""
    from app.integrations.providers.microsoft import _mentions, files
    from app.services import teams_format

    assert files.build_graph_chat_body is teams_format.build_graph_chat_body
    assert _mentions.build_graph_chat_body is teams_format.build_graph_chat_body


@pytest.mark.asyncio
async def test_chat_file_message_keeps_its_markdown_structure():
    from app.integrations.providers import microsoft as ms

    ref = FileRef.from_bytes(data=b"pdf", filename="report.pdf", content_type="application/pdf")
    file_context = FileContext.from_file_refs([ref])
    executor = CallableToolExecutor()
    recorder = _Recorder([_FILE_ITEM, _FILE_ITEM, *_CHAT_SHARE_RESPONSES, {"id": "msg1"}])
    with patch.object(ms._common, "safe_request", new=recorder):
        ms.build_tools("dummy-token", executor)
        await executor._handlers["send_microsoft_chat_file"](
            chat_id="19:chat@thread.v2",
            file_id=ref.file_id,
            message=_STRUCTURED_MESSAGE,
            file_context=file_context,
        )

    posts = [call for call in recorder.calls if call["url"].endswith("/messages")]
    assert len(posts) == 1, "one Graph call posts the text and the file card"
    body = posts[0]["json"]["body"]
    assert body["contentType"] == "html"
    assert body["content"] == _plain_message_html(_STRUCTURED_MESSAGE) + _FILE_CARD_TAG
    for tag in ("<strong>", "<ul>", "<li>", "<a ", "<table"):
        assert tag in body["content"]
    assert "**" not in body["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reply_to", ["", "1696582800000"])
async def test_channel_file_message_keeps_its_markdown_structure(reply_to):
    from app.integrations.providers import microsoft as ms

    ref = FileRef.from_bytes(data=b"pdf", filename="report.pdf", content_type="application/pdf")
    file_context = FileContext.from_file_refs([ref])
    executor = CallableToolExecutor()
    recorder = _Recorder(
        [{"id": "folder1", "parentReference": {"driveId": "drive1"}}, _FILE_ITEM, _FILE_ITEM, {"id": "msg1"}]
    )
    with (
        patch.object(ms._common, "safe_request", new=recorder),
        patch.object(ms.files, "record_teams_channel_post", new=AsyncMock()),
    ):
        ms.build_tools("dummy-token", executor)
        await executor._handlers["send_microsoft_channel_file"](
            team_id="team-1",
            channel_id="19:chan@thread.tacv2",
            file_id=ref.file_id,
            message=_STRUCTURED_MESSAGE,
            reply_to_message_id=reply_to,
            file_context=file_context,
        )

    post = recorder.calls[-1]
    assert post["url"].endswith(f"/messages/{reply_to}/replies" if reply_to else "/messages")
    assert post["json"]["body"]["content"] == _plain_message_html(_STRUCTURED_MESSAGE) + _FILE_CARD_TAG


@pytest.mark.asyncio
async def test_chat_file_without_message_posts_the_file_card_alone():
    from app.integrations.providers import microsoft as ms

    ref = FileRef.from_bytes(data=b"pdf", filename="report.pdf", content_type="application/pdf")
    file_context = FileContext.from_file_refs([ref])
    executor = CallableToolExecutor()
    recorder = _Recorder([_FILE_ITEM, _FILE_ITEM, *_CHAT_SHARE_RESPONSES, {"id": "msg1"}])
    with patch.object(ms._common, "safe_request", new=recorder):
        ms.build_tools("dummy-token", executor)
        await executor._handlers["send_microsoft_chat_file"](
            chat_id="19:chat@thread.v2", file_id=ref.file_id, file_context=file_context
        )

    assert recorder.calls[-1]["json"]["body"]["content"] == _FILE_CARD_TAG


# ---------------------------------------------------------------------------
# microsoft provider attachment surfacing
# ---------------------------------------------------------------------------


def test_format_message_attachments_reference():
    msg = {
        "attachments": [
            {
                "contentType": "reference",
                "contentUrl": "https://contoso.sharepoint.com/x/plan.docx",
                "name": "plan.docx",
            },
            {"contentType": "application/vnd.microsoft.card.adaptive"},
        ]
    }
    out = _format_message_attachments(msg)
    assert "plan.docx" in out
    assert "get_microsoft_file_by_url" in out
    assert "adaptive" not in out


def test_format_message_attachments_empty():
    assert _format_message_attachments({}) == ""
    assert _format_message_attachments({"attachments": []}) == ""
