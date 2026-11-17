"""Integration files: link, download, inline, pipe for every provider.

Each provider API is served by a fake transport shaped like its documented
answers; nothing reaches a real provider. A download answers with the source's
links, stores the file (a download card and a file_id) and carries it on the
result so the model reads it; an upload takes any file_id of the run.
"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from app.integrations.providers import (
    credly,
    discord_bot,
    github,
    hibob,
    jira,
    newsapi,
    notion,
    openaev,
    opencti,
    rss,
    servicenow,
    telegram_bot,
    whatsapp_bot,
)
from app.integrations.providers.notion._common import _block_to_text
from app.services.integration_tools import _SafeIntegrationExecutor
from tests.integration_file_fakes import (
    HOSTILE_FILE_NAMES,
    RecordingPersist,
    assert_name_only_inside,
    call,
    fake_api,
    file_context_with,
)

_PDF = b"%PDF-1.4 findings"
_PNG = b"\x89PNG\r\n\x1a\nfake"


def _executor(builder, token, persist=None, **kwargs):
    executor = _SafeIntegrationExecutor()
    builder(token, executor, persist_file_fn=persist, **kwargs)
    return executor


def _stored(result, persist: RecordingPersist, data: bytes) -> str:
    """The stored file id, after checking the four capabilities' common shape."""
    assert not result.is_error, result.content
    assert any(stored == data for _, stored, _ in persist.calls)
    (ref,) = result.files
    assert f"[[FILE:{ref.file_id}]]" in result.content
    assert f"file_id: {ref.file_id}" in result.content
    return ref.file_id


# ---------------------------------------------------------------------------
# Jira
# ---------------------------------------------------------------------------

_JIRA = "https://api.atlassian.com/ex/jira/cloud-1/rest/api/3"
_JIRA_CONTENT = "https://acme.atlassian.net/rest/api/3/attachment/content/10001"
_JIRA_ATTACHMENT = {
    "id": "10001",
    "filename": "findings.pdf",
    "mimeType": "application/pdf",
    "size": len(_PDF),
    "created": "2026-09-01T10:00:00.000+0000",
    "author": {"displayName": "Ana"},
    "content": _JIRA_CONTENT,
}


def _jira(request: httpx.Request) -> httpx.Response:
    path = str(request.url).split("?")[0]
    if path == f"{_JIRA}/issue/PROJ-1" and request.method == "GET":
        return httpx.Response(
            200,
            json={
                "key": "PROJ-1",
                "fields": {"summary": "Leak", "attachment": [_JIRA_ATTACHMENT]},
                "names": {},
                "transitions": [],
            },
        )
    if path == f"{_JIRA}/issue/PROJ-1/comment":
        return httpx.Response(200, json={"comments": [], "total": 0})
    if path == f"{_JIRA}/attachment/10001":
        return httpx.Response(200, json=_JIRA_ATTACHMENT)
    if path == f"{_JIRA}/attachment/content/10001":
        return httpx.Response(200, content=_PDF)
    if path == f"{_JIRA}/issue/PROJ-1/attachments":
        return httpx.Response(200, json=[{"id": "10002", "filename": "report.pdf", "content": _JIRA_CONTENT}])
    return httpx.Response(404, json={})


@pytest.mark.asyncio
async def test_jira_issue_and_listing_carry_the_attachment_content_url():
    executor = _executor(jira.build_tools, "cloud-1||jira-token")
    with fake_api(_jira):
        issue = await call(executor, "get_jira_issue", issue_key="PROJ-1")
        listing = await call(executor, "list_jira_attachments", issue_key="PROJ-1")
    for result in (issue, listing):
        assert "findings.pdf" in result.content
        assert "id: 10001" in result.content
        assert _JIRA_CONTENT in result.content


@pytest.mark.asyncio
async def test_jira_download_stores_the_file_with_its_link_on_the_result():
    persist = RecordingPersist()
    executor = _executor(jira.build_tools, "cloud-1||jira-token", persist)
    with fake_api(_jira):
        result = await call(executor, "download_jira_attachment", attachment_id="10001")
    _stored(result, persist, _PDF)
    assert f"Download URL: {_JIRA_CONTENT}" in result.content


@pytest.mark.asyncio
async def test_jira_uploads_any_file_of_the_run():
    executor = _executor(jira.build_tools, "cloud-1||jira-token")
    ctx, file_id = file_context_with(_PDF, "report.pdf", "application/pdf")
    executor.file_context = ctx
    with fake_api(_jira) as sent:
        result = await call(executor, "upload_jira_attachment", issue_key="PROJ-1", file_id=file_id)
    assert not result.is_error, result.content
    assert "10002" in result.content
    upload = next(r for r in sent if r.url.path.endswith("/attachments"))
    assert upload.headers["x-atlassian-token"] == "no-check"
    assert _PDF in upload.content


# ---------------------------------------------------------------------------
# Notion
# ---------------------------------------------------------------------------

_NOTION_URL = "https://prod-files-secure.s3.us-west-2.amazonaws.com/ws/diagram.png?X-Amz-Signature=abc"


def test_a_notion_image_with_a_caption_keeps_its_url():
    block = {
        "type": "image",
        "image": {"file": {"url": _NOTION_URL}, "caption": [{"plain_text": "Architecture"}]},
    }
    text = _block_to_text(block)
    assert "Architecture" in text and _NOTION_URL in text


@pytest.mark.asyncio
async def test_notion_download_answers_with_the_file_and_block_links():
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.notion.com":
            return httpx.Response(
                200,
                json={
                    "id": "b1b1b1b1-0000-0000-0000-000000000000",
                    "type": "image",
                    "parent": {"type": "page_id", "page_id": "p2p2p2p2-0000-0000-0000-000000000000"},
                    "image": {"type": "file", "file": {"url": _NOTION_URL}},
                },
            )
        return httpx.Response(200, content=_PNG, headers={"content-type": "image/png"})

    persist = RecordingPersist()
    executor = _executor(notion.build_tools, "notion-token", persist)
    with fake_api(responder):
        result = await call(executor, "download_notion_file", block_id="b1b1b1b1-0000-0000-0000-000000000000")
    _stored_ref = result.files[0]
    assert persist.calls and f"[[FILE:{_stored_ref.file_id}]]" in result.content
    assert "https://www.notion.so/p2p2p2p2000000000000000000000000#b1b1b1b1000000000000000000000000" in result.content
    assert _NOTION_URL in result.content


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

_GH = "https://api.github.com/repos/acme/api"
_GH_HTML = "https://github.com/acme/api/blob/main/docs/logo.png"
_GH_RAW = "https://raw.githubusercontent.com/acme/api/main/docs/logo.png"


def _gh_file(content: bytes, name: str = "logo.png") -> dict:
    return {
        "type": "file",
        "name": name,
        "size": len(content),
        "encoding": "base64",
        "content": base64.b64encode(content).decode(),
        "html_url": _GH_HTML,
        "download_url": _GH_RAW,
    }


def _github(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/repos/acme/api/contents/docs/logo.png" and request.method == "GET":
        return httpx.Response(200, json=_gh_file(_PNG))
    if path == "/repos/acme/api/contents/docs/readme.md":
        return httpx.Response(200, json={**_gh_file(b"# Hello"), "html_url": _GH_HTML, "download_url": _GH_RAW})
    # A per-file write is one signed commit (#12051): the branch, its tree, the blob, the tree, the commit.
    if path == "/repos/acme/api/git/ref/heads/main":
        return httpx.Response(200, json={"object": {"sha": "a" * 40, "type": "commit"}})
    if path == "/repos/acme/api/git/commits/" + "a" * 40:
        return httpx.Response(200, json={"sha": "a" * 40, "tree": {"sha": "b" * 40}})
    if path in ("/repos/acme/api/git/trees/" + "b" * 40, "/repos/acme/api/git/trees/" + "d" * 40):
        docs = [{"path": "docs", "mode": "040000", "type": "tree", "sha": "d" * 40}]
        return httpx.Response(200, json={"tree": docs if path.endswith("b" * 40) else [], "truncated": False})
    if path == "/repos/acme/api/git/blobs":
        return httpx.Response(201, json={"sha": "e" * 40})
    if path == "/repos/acme/api/git/trees" and request.method == "POST":
        return httpx.Response(201, json={"sha": "c" * 40})
    if path == "/graphql":
        commit = {"oid": "f" * 40, "signature": {"isValid": True, "state": "VALID"}}
        return httpx.Response(200, json={"data": {"createCommitOnBranch": {"commit": commit}}})
    if path == "/repos/acme/api/pulls":
        return httpx.Response(200, json=[])
    if path == "/repos/acme/api/releases/tags/v1":
        return httpx.Response(
            200,
            json={
                "id": 3,
                "tag_name": "v1",
                "name": "v1",
                "html_url": "https://github.com/acme/api/releases/tag/v1",
                "assets": [
                    {
                        "id": 7,
                        "name": "sbom.json",
                        "size": 12,
                        "download_count": 2,
                        "browser_download_url": "https://github.com/acme/api/releases/download/v1/sbom.json",
                    }
                ],
            },
        )
    if path == "/repos/acme/api/releases/assets/7":
        if request.headers.get("accept") == "application/octet-stream":
            return httpx.Response(302, headers={"location": "https://objects.githubusercontent.com/asset/7?sig=x"})
        return httpx.Response(
            200,
            json={
                "id": 7,
                "name": "sbom.json",
                "size": 12,
                "content_type": "application/json",
                "browser_download_url": "https://github.com/acme/api/releases/download/v1/sbom.json",
            },
        )
    if request.url.host == "objects.githubusercontent.com":
        return httpx.Response(200, content=b'{"sbom": 1}')
    return httpx.Response(404, json={"message": "Not Found"})


@pytest.mark.asyncio
async def test_github_file_reads_carry_the_web_and_raw_links():
    executor = _executor(github.build_tools, "gh-token")
    with fake_api(_github):
        text = await call(executor, "get_github_file", repo="acme/api", path="docs/readme.md", branch="main")
        binary = await call(executor, "get_github_file", repo="acme/api", path="docs/logo.png", branch="main")
    assert "# Hello" in text.content
    for result in (text, binary):
        assert f"Web URL: {_GH_HTML}" in result.content
        assert f"Download URL: {_GH_RAW}" in result.content
    assert "download_github_file" in binary.content


@pytest.mark.asyncio
async def test_github_download_stores_any_file_with_its_links():
    persist = RecordingPersist()
    executor = _executor(github.build_tools, "gh-token", persist)
    with fake_api(_github):
        result = await call(executor, "download_github_file", repo="acme/api", path="docs/logo.png", branch="main")
    _stored(result, persist, _PNG)
    assert f"Web URL: {_GH_HTML}" in result.content


@pytest.mark.asyncio
async def test_github_commits_a_file_of_the_run_byte_for_byte():
    executor = _executor(github.build_tools, "gh-token")
    ctx, file_id = file_context_with(_PDF, "report.pdf", "application/pdf")
    executor.file_context = ctx
    with fake_api(_github) as sent:
        result = await call(
            executor,
            "create_or_update_github_file",
            repo="acme/api",
            path="docs/report.pdf",
            file_id=file_id,
            message="docs: add report",
            branch="main",
        )
    assert not result.is_error, result.content
    commit = next(r for r in sent if r.url.path == "/graphql")
    (added,) = json.loads(commit.content)["variables"]["input"]["fileChanges"]["additions"]
    assert base64.b64decode(added["contents"]) == _PDF
    assert "blob/main/docs/report.pdf" in result.content


@pytest.mark.asyncio
async def test_github_never_commits_an_empty_file_for_an_omitted_source():
    executor = _executor(github.build_tools, "gh-token")
    with fake_api(_github) as sent:
        omitted = await call(
            executor, "create_or_update_github_file", repo="acme/api", path="docs/report.pdf", message="m", branch="main"
        )
        empty = await call(
            executor,
            "create_or_update_github_file",
            repo="acme/api",
            path="docs/report.pdf",
            content="",
            message="m",
            branch="main",
        )
    assert omitted.is_error
    assert not empty.is_error, empty.content
    commits = [r for r in sent if r.url.path == "/graphql"]
    assert len(commits) == 1
    (added,) = json.loads(commits[0].content)["variables"]["input"]["fileChanges"]["additions"]
    assert added["contents"] == ""


@pytest.mark.asyncio
async def test_github_release_assets_are_linked_and_downloadable():
    persist = RecordingPersist()
    executor = _executor(github.build_tools, "gh-token", persist)
    with fake_api(_github) as sent:
        release = await call(executor, "get_github_release", repo="acme/api", tag="v1")
        asset = await call(executor, "download_github_release_asset", repo="acme/api", asset_id=7)
    assert "asset_id: 7" in release.content
    assert "releases/download/v1/sbom.json" in release.content
    _stored(asset, persist, b'{"sbom": 1}')
    cdn = next(r for r in sent if r.url.host == "objects.githubusercontent.com")
    assert "authorization" not in cdn.headers


# ---------------------------------------------------------------------------
# OpenCTI
# ---------------------------------------------------------------------------

_OCTI_FILE = "import/Report/r1/findings.pdf"


@pytest.mark.asyncio
async def test_opencti_download_is_stored_with_its_storage_links():
    def responder(request: httpx.Request) -> httpx.Response:
        if "findings.csv" in str(request.url):
            return httpx.Response(200, text="a,b\n1,2\n", headers={"content-type": "text/csv"})
        return httpx.Response(200, content=_PDF, headers={"content-type": "application/pdf"})

    persist = RecordingPersist()
    executor = _executor(opencti.build_tools, "https://octi.example.com||octi-token", persist)
    with fake_api(responder):
        binary = await call(executor, "download_opencti_file", file_id=_OCTI_FILE)
        text = await call(executor, "download_opencti_file", file_id="import/Report/r1/findings.csv")
    _stored(binary, persist, _PDF)
    encoded = "import%2FReport%2Fr1%2Ffindings.pdf"
    assert f"https://octi.example.com/storage/view/{encoded}" in binary.content
    assert f"https://octi.example.com/storage/get/{encoded}" in binary.content
    # A text file keeps its inline content and is stored too.
    assert "a,b\n1,2" in text.content
    assert "[[FILE:" in text.content and "storage/get/" in text.content
    assert persist.calls[-1][1] == b"a,b\n1,2\n"


# ---------------------------------------------------------------------------
# OpenAEV
# ---------------------------------------------------------------------------

_AEV = "https://aev.example.com"


def _openaev(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/documents/d1/file":
        return httpx.Response(
            200,
            content=_PDF,
            headers={"content-type": "application/pdf", "content-disposition": 'attachment; filename="plan.pdf"'},
        )
    if path == "/api/documents" and request.method == "POST":
        return httpx.Response(200, json={"document_id": "d9", "document_name": "report.pdf"})
    if path == "/api/documents/search":
        return httpx.Response(200, json={"content": [{"document_id": "d1", "document_name": "plan.pdf"}]})
    return httpx.Response(200, json={})


@pytest.mark.asyncio
async def test_openaev_documents_are_linked_downloadable_and_uploadable_from_a_file_id():
    persist = RecordingPersist()
    executor = _executor(openaev.build_tools, f"{_AEV}||aev-token", persist)
    ctx, file_id = file_context_with(_PDF, "report.pdf", "application/pdf")
    executor.file_context = ctx
    with fake_api(_openaev) as sent:
        listing = await call(executor, "manage_openaev_document", action="list")
        download = await call(executor, "manage_openaev_document", action="download", document_id="d1")
        upload = await call(executor, "manage_openaev_document", action="upload", file_id=file_id)
    assert f"{_AEV}/api/documents/d1/file" in listing.content
    _stored(download, persist, _PDF)
    assert download.files[0].filename == "plan.pdf"
    assert f"Download URL: {_AEV}/api/documents/d1/file" in download.content
    assert "d9" in upload.content and f"{_AEV}/api/documents/d9/file" in upload.content
    posted = next(r for r in sent if r.method == "POST" and r.url.path == "/api/documents")
    assert _PDF in posted.content
    unknown = await call(executor, "manage_openaev_document", action="fetch")
    assert "download" in unknown.content


# ---------------------------------------------------------------------------
# ServiceNow
# ---------------------------------------------------------------------------

_SN_LINK = "https://acme.service-now.com/api/now/attachment/a1/file"


@pytest.mark.asyncio
async def test_servicenow_download_link_and_file_on_the_result():
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/attachment/a1/file"):
            return httpx.Response(200, content=_PDF)
        return httpx.Response(
            200,
            json={
                "result": {
                    "sys_id": "a1",
                    "file_name": "evidence.pdf",
                    "content_type": "application/pdf",
                    "size_bytes": str(len(_PDF)),
                    "download_link": _SN_LINK,
                }
            },
        )

    persist = RecordingPersist()
    executor = _executor(servicenow.build_tools, "https://acme.service-now.com||user||pass", persist)
    with fake_api(responder):
        result = await call(executor, "download_servicenow_attachment", attachment_id="a1")
    _stored(result, persist, _PDF)
    assert f"Download URL: {_SN_LINK}" in result.content


# ---------------------------------------------------------------------------
# HiBob
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hibob_reports_are_stored_as_files():
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("format") == "xlsx":
            return httpx.Response(200, content=b"PK\x03\x04xlsx")
        return httpx.Response(200, text="name,team\nAna,Sec\n", headers={"content-type": "text/csv"})

    persist = RecordingPersist()
    executor = _executor(hibob.build_tools, "svc||key", persist)
    with fake_api(responder):
        xlsx = await call(executor, "download_hibob_report", report_id="r1", report_format="xlsx")
        csv = await call(executor, "download_hibob_report", report_id="r1", report_format="csv")
    _stored(xlsx, persist, b"PK\x03\x04xlsx")
    assert "name,team" in csv.content and "[[FILE:" in csv.content


# ---------------------------------------------------------------------------
# Channel bots
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_telegram_sends_a_document_and_a_photo_from_a_file_id():
    executor = _executor(telegram_bot.build_tools, "123:abc")
    ctx, file_id = file_context_with(_PDF, "report.pdf", "application/pdf")
    executor.file_context = ctx
    ok = httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})
    with fake_api(lambda r: ok) as sent:
        document = await call(executor, "send_telegram_bot_document", chat_id="99", file_id=file_id)
        photo = await call(executor, "send_telegram_bot_photo", chat_id="99", photo_url="https://example.com/a.png")
    assert "42" in document.content and "42" in photo.content
    assert sent[0].url.path.endswith("/sendDocument") and _PDF in sent[0].content
    assert json.loads(sent[1].content)["photo"] == "https://example.com/a.png"


_CDN = "https://cdn.discordapp.com/attachments/1/2/report.pdf?ex=1&is=2&hm=3"


def _discord(request: httpx.Request) -> httpx.Response:
    if request.url.host == "cdn.discordapp.com":
        return httpx.Response(200, content=_PDF)
    attachment = {"id": "555", "filename": "report.pdf", "content_type": "application/pdf", "size": len(_PDF), "url": _CDN}
    if request.method == "GET" and request.url.path.endswith("/messages"):
        return httpx.Response(200, json=[{"id": "m1", "author": {"username": "ana"}, "content": "see", "attachments": [attachment]}])
    if request.method == "GET":
        return httpx.Response(200, json={"id": "m1", "attachments": [attachment]})
    return httpx.Response(200, json={"id": "m2", "attachments": [{"url": _CDN}]})


@pytest.mark.asyncio
async def test_discord_attachments_are_listed_downloaded_and_sent():
    persist = RecordingPersist()
    executor = _executor(discord_bot.build_tools, "bot-token", persist)
    ctx, file_id = file_context_with(_PDF, "report.pdf", "application/pdf")
    executor.file_context = ctx
    with fake_api(_discord) as sent:
        messages = await call(executor, "read_discord_bot_messages", channel_id="c1")
        download = await call(
            executor, "download_discord_bot_attachment", channel_id="c1", message_id="m1", attachment_id="555"
        )
        sent_file = await call(executor, "send_discord_bot_file", channel_id="c1", file_id=file_id)
    assert "attachment_id: 555" in messages.content and _CDN in messages.content
    _stored(download, persist, _PDF)
    cdn = next(r for r in sent if r.url.host == "cdn.discordapp.com")
    assert "authorization" not in cdn.headers
    assert "m2" in sent_file.content
    post = next(r for r in sent if r.method == "POST")
    assert _PDF in post.content and b"payload_json" in post.content


@pytest.mark.asyncio
async def test_discord_bot_whitelists_bound_the_file_tools():
    executor = _executor(discord_bot.build_tools, "bot-token", RecordingPersist(), publish_channels=[{"id": "c9"}])
    ctx, file_id = file_context_with(_PDF, "report.pdf", "application/pdf")
    executor.file_context = ctx
    with fake_api(_discord) as sent:
        download = await call(
            executor, "download_discord_bot_attachment", channel_id="c1", message_id="m1", attachment_id="555"
        )
        upload = await call(executor, "send_discord_bot_file", channel_id="c1", file_id=file_id)
    assert "Access denied" in download.content and "Access denied" in upload.content
    assert sent == []


def _whatsapp(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/PHONE/media"):
        return httpx.Response(200, json={"id": "777"})
    if path.endswith("/PHONE/messages"):
        return httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})
    if path.endswith("/888"):
        return httpx.Response(
            200,
            json={"url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=888", "mime_type": "application/pdf", "file_size": len(_PDF)},
        )
    if request.url.host == "lookaside.fbsbx.com":
        return httpx.Response(200, content=_PDF)
    return httpx.Response(404, json={})


@pytest.mark.asyncio
async def test_whatsapp_sends_and_downloads_media():
    persist = RecordingPersist()
    executor = _executor(whatsapp_bot.build_tools, "PHONE||wa-token", persist)
    ctx, file_id = file_context_with(_PDF, "report.pdf", "application/pdf")
    executor.file_context = ctx
    with fake_api(_whatsapp) as sent:
        sent_media = await call(executor, "send_whatsapp_bot_media", to="14155552671", file_id=file_id)
        download = await call(executor, "download_whatsapp_bot_media", media_id="888")
    assert "wamid.1" in sent_media.content
    message = json.loads(next(r for r in sent if r.url.path.endswith("/messages")).content)
    assert message["type"] == "document" and message["document"]["id"] == "777"
    _stored(download, persist, _PDF)
    media = next(r for r in sent if r.url.host == "lookaside.fbsbx.com")
    assert media.headers["authorization"] == "Bearer wa-token"


def test_whatsapp_withholds_media_download_from_a_restricted_bot():
    """A media id names no conversation: a bot bound to some recipients cannot scope it."""
    for kwargs in (
        {"publish_channels": [{"id": "14155552671"}]},
        {"read_channels": [{"id": "14155552671"}]},
        {"publish_blocked": True},
    ):
        executor = _SafeIntegrationExecutor()
        names = {d.name for d in whatsapp_bot.build_tools("PHONE||wa-token", executor, **kwargs)}
        assert "download_whatsapp_bot_media" not in names
        assert "download_whatsapp_bot_media" not in executor.tool_names
        assert "send_whatsapp_bot_media" in names


@pytest.mark.asyncio
async def test_whatsapp_never_sends_the_token_outside_meta():
    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/888"):
            return httpx.Response(200, json={"url": "https://evil.example.com/x", "mime_type": "image/png"})
        return httpx.Response(200, content=_PNG)

    executor = _executor(whatsapp_bot.build_tools, "PHONE||wa-token", RecordingPersist())
    with fake_api(responder) as sent:
        result = await call(executor, "download_whatsapp_bot_media", media_id="888")
    assert result.is_error
    assert not any(r.url.host == "evil.example.com" for r in sent)


# ---------------------------------------------------------------------------
# Media links in feeds and badges
# ---------------------------------------------------------------------------


def test_news_rss_and_badge_answers_carry_their_media_urls():
    article = newsapi._format_article({"title": "t", "url": "https://n.example/a", "urlToImage": "https://n.example/a.jpg"})
    assert "Image URL: https://n.example/a.jpg" in article
    entry = rss._format_entry(
        {"title": "ep", "link": "https://p.example/1", "enclosures": [{"href": "https://p.example/1.mp3", "type": "audio/mpeg"}]},
        "Pod",
    )
    assert "Enclosure (audio/mpeg): https://p.example/1.mp3" in entry
    assert credly._image_url({"image_url": "https://c.example/b.png"}) == "https://c.example/b.png"
    assert credly._image_url({"image": {"url": "https://c.example/c.png"}}) == "https://c.example/c.png"


# ---------------------------------------------------------------------------
# An upload answer names the file by its file_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_jira_upload_names_the_file_by_its_id(hostile):
    name = HOSTILE_FILE_NAMES[hostile]

    def responder(request: httpx.Request) -> httpx.Response:
        if str(request.url).split("?")[0] == f"{_JIRA}/issue/PROJ-1/attachments":
            return httpx.Response(200, json=[{"id": "10002", "filename": name, "content": _JIRA_CONTENT}])
        return _jira(request)

    executor = _executor(jira.build_tools, "cloud-1||jira-token")
    ctx, file_id = file_context_with(_PDF, name, "application/pdf")
    executor.file_context = ctx
    with fake_api(responder):
        result = await call(executor, "upload_jira_attachment", issue_key="PROJ-1", file_id=file_id)
    assert not result.is_error, result.content
    outside = assert_name_only_inside(result.content, name)
    assert outside == f"Attached file {file_id} to PROJ-1 (id: 10002).\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_openaev_document_upload_names_the_file_by_its_id(hostile):
    name = HOSTILE_FILE_NAMES[hostile]

    def responder(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/documents" and request.method == "POST":
            return httpx.Response(200, json={"document_id": "d9", "document_name": name})
        return _openaev(request)

    executor = _executor(openaev.build_tools, f"{_AEV}||aev-token")
    ctx, file_id = file_context_with(_PDF, name, "application/pdf")
    executor.file_context = ctx
    with fake_api(responder):
        result = await call(executor, "manage_openaev_document", action="upload", file_id=file_id)
    outside = assert_name_only_inside(result.content, name)
    assert outside == (
        f"Uploaded file {file_id} as document d9. Download URL: {_AEV}/api/documents/d9/file\n"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_discord_file_names_the_file_by_its_id(hostile):
    name = HOSTILE_FILE_NAMES[hostile]
    executor = _executor(discord_bot.build_tools, "bot-token", RecordingPersist())
    ctx, file_id = file_context_with(_PDF, name, "application/pdf")
    executor.file_context = ctx
    with fake_api(_discord):
        result = await call(executor, "send_discord_bot_file", channel_id="c1", file_id=file_id)
    assert not result.is_error, result.content
    outside = assert_name_only_inside(result.content, name)
    assert outside == f"File {file_id} sent (message ID: m2).\n"


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_whatsapp_media_names_the_file_by_its_id(hostile):
    name = HOSTILE_FILE_NAMES[hostile]
    executor = _executor(whatsapp_bot.build_tools, "PHONE||wa-token", RecordingPersist())
    ctx, file_id = file_context_with(_PDF, name, "application/pdf")
    executor.file_context = ctx
    with fake_api(_whatsapp):
        result = await call(executor, "send_whatsapp_bot_media", to="14155552671", file_id=file_id)
    assert not result.is_error, result.content
    outside = assert_name_only_inside(result.content, name)
    assert outside == (
        f"WhatsApp document of file {file_id} sent to 14155552671 (message ID: wamid.1, media ID: 777).\n"
    )
