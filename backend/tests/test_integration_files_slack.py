"""Slack and Slack bot files: link, download, inline, pipe.

Every Slack tool that meets a file shows its id, permalink and
``url_private_download``; ``download_slack_file`` stores the bytes as a file of
the run (a download card and a file_id, the file on the result so the model
reads it); ``upload_slack_file`` takes any file_id of the run and uploads
through ``files.getUploadURLExternal`` / ``files.completeUploadExternal``,
since Slack retired ``files.upload`` on 2025-11-12. The Slack API is served by
a fake transport shaped like Slack's documented answers.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from app.integrations.providers import slack, slack_bot
from app.integrations.providers.slack_scope import SLACK_CHANNEL_SCOPE_KEY, channel_scope_from_bindings
from app.services.integration_tools import _SafeIntegrationExecutor
from tests.integration_file_fakes import (
    HOSTILE_FILE_NAMES,
    RecordingPersist,
    assert_name_only_inside,
    call,
    fake_api,
    file_context_with,
)

_PDF = b"%PDF-1.4 quarterly report"

_FILE = {
    "id": "F0123",
    "name": "report.pdf",
    "title": "Q3 report",
    "mimetype": "application/pdf",
    "filetype": "pdf",
    "size": len(_PDF),
    "user": "U1",
    "permalink": "https://acme.slack.com/files/U1/F0123/report.pdf",
    "url_private": "https://files.slack.com/files-pri/T1-F0123/report.pdf",
    "url_private_download": "https://files.slack.com/files-pri/T1-F0123/download/report.pdf",
    "channels": ["C1"],
}


def _slack(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/conversations.history":
        return httpx.Response(
            200,
            json={
                "ok": True,
                "messages": [{"user": "U1", "ts": "1.0", "text": "see attached", "files": [_FILE]}],
            },
        )
    if path == "/api/conversations.replies":
        return httpx.Response(
            200, json={"ok": True, "messages": [{"user": "U1", "ts": "1.0", "text": "", "files": [_FILE]}]}
        )
    if path == "/api/search.files":
        return httpx.Response(200, json={"ok": True, "files": {"total": 1, "matches": [_FILE]}})
    if path == "/api/files.info":
        return httpx.Response(200, json={"ok": True, "file": _FILE})
    if request.url.host == "files.slack.com" and "/download/" in path:
        return httpx.Response(200, content=_PDF, headers={"content-type": "application/pdf"})
    if path == "/api/files.getUploadURLExternal":
        return httpx.Response(
            200,
            json={"ok": True, "upload_url": "https://files.slack.com/upload/v1/ABC", "file_id": "F9"},
        )
    if request.url.host == "files.slack.com" and path.startswith("/upload/"):
        return httpx.Response(200, text="OK - 123")
    if path == "/api/files.completeUploadExternal":
        return httpx.Response(
            200,
            json={"ok": True, "files": [{"id": "F9", "title": "notes", "permalink": "https://acme.slack.com/files/U1/F9"}]},
        )
    if path == "/api/files.upload":
        return httpx.Response(200, json={"ok": False, "error": "method_deprecated"})
    return httpx.Response(404, json={"ok": False, "error": "unknown_method"})


def _slack_tools(persist=None, **kwargs):
    executor = _SafeIntegrationExecutor()
    slack.build_tools("xoxp-token", executor, persist_file_fn=persist, **kwargs)
    return executor


@pytest.mark.asyncio
async def test_a_message_shows_its_files_with_id_and_both_links():
    executor = _slack_tools()
    with fake_api(_slack):
        history = await call(executor, "read_slack_messages", channel="C1")
        thread = await call(executor, "read_slack_thread", channel="C1", thread_ts="1.0")
    for result in (history, thread):
        assert "F0123" in result.content
        assert _FILE["permalink"] in result.content
        assert _FILE["url_private_download"] in result.content


@pytest.mark.asyncio
async def test_file_search_and_info_carry_the_id_and_both_links():
    executor = _slack_tools()
    with fake_api(_slack):
        search = await call(executor, "search_slack_files", query="report")
        info = await call(executor, "get_slack_file_info", file_id="F0123")
    assert "ID: F0123" in search.content
    assert _FILE["url_private_download"] in search.content
    # The baseline line stays, the two links are added.
    assert f"URL: {_FILE['url_private']}" in info.content
    assert f"Web URL: {_FILE['permalink']}" in info.content
    assert f"Download URL: {_FILE['url_private_download']}" in info.content


@pytest.mark.asyncio
async def test_download_stores_the_file_and_answers_with_links_and_file_id():
    persist = RecordingPersist()
    executor = _slack_tools(persist)
    with fake_api(_slack) as sent:
        result = await call(executor, "download_slack_file", file_id="F0123")

    assert not result.is_error
    assert persist.calls == [("report.pdf", _PDF, "application/pdf")]
    (ref,) = result.files
    assert f"[[FILE:{ref.file_id}]]" in result.content
    assert f"file_id: {ref.file_id}" in result.content
    assert f"Web URL: {_FILE['permalink']}" in result.content
    assert f"Download URL: {_FILE['url_private_download']}" in result.content
    download = next(r for r in sent if r.url.host == "files.slack.com")
    assert download.headers["authorization"] == "Bearer xoxp-token"


@pytest.mark.asyncio
async def test_download_never_sends_the_token_outside_slack():
    foreign = {**_FILE, "url_private_download": "https://evil.example.com/steal"}

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/files.info":
            return httpx.Response(200, json={"ok": True, "file": foreign})
        return _slack(request)

    executor = _slack_tools(RecordingPersist())
    with fake_api(responder) as sent:
        result = await call(executor, "download_slack_file", file_id="F0123")
    assert result.is_error
    assert not any(r.url.host == "evil.example.com" for r in sent)


@pytest.mark.asyncio
async def test_a_sign_in_page_is_an_error_not_a_file():
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.host == "files.slack.com":
            return httpx.Response(200, text="<html>sign in</html>", headers={"content-type": "text/html"})
        return _slack(request)

    persist = RecordingPersist()
    executor = _slack_tools(persist)
    with fake_api(responder):
        result = await call(executor, "download_slack_file", file_id="F0123")
    assert result.is_error
    assert "files:read" in result.content
    assert persist.calls == []


@pytest.mark.asyncio
async def test_upload_forwards_a_file_id_through_the_external_upload_flow():
    executor = _slack_tools()
    ctx, file_id = file_context_with(_PDF, "report.pdf", "application/pdf")
    executor.file_context = ctx
    with fake_api(_slack) as sent:
        result = await call(executor, "upload_slack_file", channels="C1", file_id=file_id)

    assert not result.is_error, result.content
    assert "F9" in result.content
    paths = [r.url.path for r in sent]
    assert "/api/files.upload" not in paths
    ticket = next(r for r in sent if r.url.path == "/api/files.getUploadURLExternal")
    form = parse_qs(ticket.content.decode())
    assert form["filename"] == ["report.pdf"]
    assert form["length"] == [str(len(_PDF))]
    upload = next(r for r in sent if r.url.path.startswith("/upload/"))
    assert _PDF in upload.content
    complete = next(r for r in sent if r.url.path == "/api/files.completeUploadExternal")
    body = json.loads(complete.content)
    assert body["channel_id"] == "C1"
    assert body["files"][0]["id"] == "F9"


@pytest.mark.asyncio
async def test_upload_of_text_content_still_works_and_needs_exactly_one_source():
    executor = _slack_tools()
    with fake_api(_slack) as sent:
        ok = await call(executor, "upload_slack_file", channels="C1,C2", content="hello", filename="notes.txt")
        neither = await call(executor, "upload_slack_file", channels="C1")
    assert not ok.is_error, ok.content
    assert ok.content.startswith("Uploaded the content as a file to Slack (ID: F9), shared in C1, C2.\n")
    upload = next(r for r in sent if r.url.path.startswith("/upload/"))
    assert b"hello" in upload.content
    complete = next(r for r in sent if r.url.path == "/api/files.completeUploadExternal")
    assert json.loads(complete.content)["channels"] == "C1,C2"
    assert neither.is_error


@pytest.mark.asyncio
async def test_a_thread_upload_to_several_channels_is_refused_before_any_bytes_move():
    executor = _slack_tools()
    with fake_api(_slack) as sent:
        result = await call(
            executor, "upload_slack_file", channels="C1,C2", content="hello", thread_ts="1.0"
        )
    assert result.is_error
    assert "exactly one channel" in result.content
    assert sent == []


@pytest.mark.asyncio
async def test_a_file_outside_the_channel_scope_is_not_downloaded():
    scope = channel_scope_from_bindings({SLACK_CHANNEL_SCOPE_KEY: [{"id": "C7", "name": "ops"}]})
    persist = RecordingPersist()
    executor = _slack_tools(persist, channel_scope=scope)
    with fake_api(_slack) as sent:
        result = await call(executor, "download_slack_file", file_id="F0123")
    assert result.is_error
    assert "outside" in result.content or "not shared" in result.content
    assert not any(r.url.host == "files.slack.com" for r in sent)
    assert persist.calls == []


def _bot_tools(persist=None, **kwargs):
    executor = _SafeIntegrationExecutor()
    slack_bot.build_tools("xoxb-token", executor, persist_file_fn=persist, **kwargs)
    return executor


@pytest.mark.asyncio
async def test_the_bot_lists_files_and_downloads_one_from_a_readable_channel():
    persist = RecordingPersist()
    executor = _bot_tools(persist, publish_channels=[{"id": "C1"}])
    with fake_api(_slack):
        messages = await call(executor, "read_slack_bot_messages", channel="C1")
        result = await call(executor, "download_slack_bot_file", file_id="F0123")
    assert "F0123" in messages.content and _FILE["url_private_download"] in messages.content
    assert not result.is_error, result.content
    assert result.files and persist.calls


@pytest.mark.asyncio
async def test_the_bot_whitelists_bound_its_file_tools():
    persist = RecordingPersist()
    executor = _bot_tools(persist, publish_channels=[{"id": "C5"}])
    ctx, file_id = file_context_with(b"a,b\n1,2\n", "data.csv", "text/csv")
    executor.file_context = ctx
    with fake_api(_slack) as sent:
        download = await call(executor, "download_slack_bot_file", file_id="F0123")
        refused = await call(executor, "upload_slack_bot_file", channel="C1", file_id=file_id)
        allowed = await call(executor, "upload_slack_bot_file", channel="C5", file_id=file_id)
    assert download.is_error and "Access denied" in download.content
    assert "Access denied" in refused.content
    assert not allowed.is_error, allowed.content
    completes = [json.loads(r.content) for r in sent if r.url.path == "/api/files.completeUploadExternal"]
    assert [c["channel_id"] for c in completes] == ["C5"]
    assert persist.calls == []


# ---------------------------------------------------------------------------
# An upload answer names the file by its file_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
@pytest.mark.parametrize(
    ("tools", "tool", "target"),
    [(_slack_tools, "upload_slack_file", {"channels": "C1"}), (_bot_tools, "upload_slack_bot_file", {"channel": "C1"})],
    ids=["integration", "channel-bot"],
)
async def test_an_upload_names_the_file_by_its_id(hostile, tools, tool, target):
    name = HOSTILE_FILE_NAMES[hostile]

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/files.completeUploadExternal":
            uploaded = {"id": "F9", "name": name, "permalink": f"https://acme.slack.com/files/U1/F9/{name}"}
            return httpx.Response(200, json={"ok": True, "files": [uploaded]})
        return _slack(request)

    executor = tools()
    ctx, file_id = file_context_with(_PDF, name, "application/pdf")
    executor.file_context = ctx
    with fake_api(responder) as sent:
        result = await call(executor, tool, file_id=file_id, **target)
    assert not result.is_error, result.content
    outside = assert_name_only_inside(result.content, name)
    assert outside == f"Uploaded file {file_id} to Slack (ID: F9), shared in C1.\n"
    ticket = next(r for r in sent if r.url.path == "/api/files.getUploadURLExternal")
    assert parse_qs(ticket.content.decode())["filename"] == [name]
