"""Microsoft files: the link, the download, the inline text and the file id.

Every Microsoft tool that meets a file gives the agent all four at once: the
links Graph exposes, a way to bring the file into the conversation as a
reusable file id, the readable text inline within the drive readers' budgets,
and the ability to forward a conversation file to any tool that takes one.

The tools run for real against a fake Microsoft Graph served through
``httpx.MockTransport`` (documented response shapes, no network, no token):
``safe_request``, ``AutoRefreshClient``, redirects and request bodies are the
production code paths.
"""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations import file_delivery
from app.integrations.providers import microsoft as ms
from app.integrations.providers import teams_bot
from app.integrations.providers.microsoft._drive import _encode_drive_filename
from app.llm.base import FileRef
from app.services.teams_format import build_graph_chat_body
from app.tools.base import CallableToolExecutor, FileContext, ToolCall, ToolResult
from tests.integration_file_fakes import HOSTILE_FILE_NAMES, assert_name_only_inside, fenced_and_open

GRAPH = "https://graph.microsoft.com/v1.0"
MESSAGE_ID = "AAMkAGI2TG93AAA=" + "x" * 64
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
WEB_URL = "https://contoso.sharepoint.com/sites/sales/Shared%20Documents/notes.txt"
DOWNLOAD_URL = "https://contoso.sharepoint.com/sites/sales/_layouts/15/download.aspx?UniqueId=1&tempauth=abc"
OUTLOOK_LINK = "https://outlook.office365.com/owa/?ItemID=AAMk&exvsurl=1&viewmodel=ReadMessageItem"


class FakeGraph:
    """Routes ``(method, decoded path)`` to canned responses and records every request."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Any]] = []
        self.requests: list[httpx.Request] = []

    def on(self, method: str, path: str, response: Any) -> None:
        self.routes.append((method.upper(), path, response))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for method, path, response in self.routes:
            if request.method == method and request.url.path == path:
                return response(request) if callable(response) else response
        raise AssertionError(f"no fake route for {request.method} {request.url}")

    def paths(self, method: str | None = None) -> list[str]:
        return [r.url.path for r in self.requests if method is None or r.method == method]


@pytest.fixture
def graph(monkeypatch) -> FakeGraph:
    fake = FakeGraph()
    real_client = httpx.AsyncClient

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(fake)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    async def _public(url: str) -> str:
        return url

    # A redirect off Graph is checked against the outbound URL policy, which
    # resolves the host: no DNS in a unit test.
    monkeypatch.setattr(file_delivery, "validate_outbound_url_async", _public)
    return fake


class Persisted:
    """The ``persist_file_fn`` integration_tools injects, recording what it stored."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, str]] = []

    async def __call__(self, filename: str, data: bytes, mime: str) -> FileRef:
        self.calls.append((filename, data, mime))
        return FileRef.from_bytes(data=data, filename=filename, content_type=mime)


def _executor(**kwargs: Any) -> CallableToolExecutor:
    executor = CallableToolExecutor()
    ms.build_tools("tok", executor, **kwargs)
    return executor


async def _call(executor: CallableToolExecutor, name: str, **arguments: Any) -> ToolResult:
    return await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))


def _docx(*paragraphs: str) -> bytes:
    from docx import Document

    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def _json(payload: Any, status: int = 200) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _request: httpx.Response(status, json=payload)


def _listed(*files: tuple[str, int]) -> Callable[[httpx.Request], httpx.Response]:
    """A message's attachment listing, as the read-back after an attachment reads it."""
    return _json({"value": [{"id": f"A{i}", "name": n, "size": s, "isInline": False} for i, (n, s) in enumerate(files)]})


# ── Drive readers: every answer carries the links ─────────────────────────


@pytest.mark.asyncio
async def test_inline_text_answer_carries_the_download_and_web_urls(graph):
    graph.on(
        "GET",
        "/v1.0/me/drive/root:/Docs/notes.txt",
        _json(
            {
                "id": "ITEM1",
                "name": "notes.txt",
                "size": 5,
                "file": {"mimeType": "text/plain"},
                "webUrl": WEB_URL,
                "@microsoft.graph.downloadUrl": DOWNLOAD_URL,
            }
        ),
    )
    # Graph answers /content with a redirect to the pre-authenticated location.
    graph.on(
        "GET",
        "/v1.0/me/drive/items/ITEM1/content",
        httpx.Response(302, headers={"Location": "https://contoso.sharepoint.com/dl/notes.txt"}),
    )
    graph.on("GET", "/dl/notes.txt", httpx.Response(200, text="hello"))

    result = await _call(_executor(), "get_microsoft_file_content", item_path="Docs/notes.txt")

    assert not result.is_error
    assert "Content:\nhello" in result.content
    assert f"Download URL: {DOWNLOAD_URL}" in result.content
    assert f"Web URL: {WEB_URL}" in result.content


_WORKBOOK = {
    "id": "WB1",
    "name": "Budget.xlsx",
    "size": 9000,
    "file": {"mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    "webUrl": "https://contoso.sharepoint.com/sites/finance/Budget.xlsx",
    "@microsoft.graph.downloadUrl": DOWNLOAD_URL,
}


@pytest.mark.asyncio
async def test_workbook_answer_carries_the_links_and_the_download_tool(graph):
    base = "/v1.0/sites/SITE1/drive/items/WB1/workbook/worksheets"
    graph.on("GET", "/v1.0/sites/SITE1/drive/root:/Budget.xlsx", _json(_WORKBOOK))
    graph.on("GET", base, _json({"value": [{"id": "{S1}", "name": "Q3", "position": 0}]}))
    graph.on("GET", f"{base}/{{S1}}/usedRange", _json({"address": "Q3!A1:B2", "rowCount": 2, "columnCount": 2}))
    graph.on("GET", f"{base}/{{S1}}/range(address='A1:B2')", _json({"text": [["Item", "Cost"], ["Laptops", "9000"]]}))

    result = await _call(
        _executor(), "get_microsoft_site_file_content", site_id="SITE1", item_path="Budget.xlsx"
    )

    assert "Laptops\t9000" in result.content
    assert f'download_microsoft_file(url="{_WORKBOOK["webUrl"]}")' in result.content
    assert f"Download URL: {DOWNLOAD_URL}" in result.content
    assert f"Web URL: {_WORKBOOK['webUrl']}" in result.content


@pytest.mark.asyncio
async def test_targeted_worksheet_answer_carries_the_links(graph):
    graph.on("GET", "/v1.0/me/drive/root:/Budget.xlsx", _json(_WORKBOOK))
    graph.on(
        "GET",
        "/v1.0/me/drive/items/WB1/workbook/worksheets/Q3/range(address='A1:B2')",
        _json({"text": [["Item", "Cost"]]}),
    )

    result = await _call(
        _executor(),
        "get_microsoft_file_content",
        item_path="Budget.xlsx",
        worksheet="Q3",
        cell_range="A1:B2",
    )

    assert "Item\tCost" in result.content
    assert f"Web URL: {_WORKBOOK['webUrl']}" in result.content


@pytest.mark.asyncio
async def test_a_graph_error_is_not_dressed_with_links(graph):
    graph.on("GET", "/v1.0/me/drive/root:/Budget.xlsx", _json(_WORKBOOK))
    graph.on(
        "GET",
        "/v1.0/me/drive/items/WB1/workbook/worksheets",
        _json({"error": {"code": "accessDenied", "message": "no"}}, status=403),
    )

    result = await _call(_executor(), "get_microsoft_file_content", item_path="Budget.xlsx")

    assert result.is_error
    assert "Web URL" not in result.content


@pytest.mark.asyncio
async def test_download_keeps_the_file_and_hands_on_its_links(graph):
    persisted = Persisted()
    graph.on(
        "GET",
        f"/v1.0/shares/{ms._encode_share_id(WEB_URL)}/driveItem",
        _json(
            {
                "id": "ITEM1",
                "name": "notes.txt",
                "size": 5,
                "file": {"mimeType": "text/plain"},
                "parentReference": {"driveId": "D1"},
                "webUrl": WEB_URL,
                "@microsoft.graph.downloadUrl": DOWNLOAD_URL,
            }
        ),
    )
    graph.on(
        "GET",
        "/v1.0/drives/D1/items/ITEM1/content",
        httpx.Response(302, headers={"Location": "https://contoso.sharepoint.com/dl/notes.txt"}),
    )
    graph.on("GET", "/dl/notes.txt", httpx.Response(200, content=b"hello"))

    result = await _call(_executor(persist_file_fn=persisted), "download_microsoft_file", url=WEB_URL)

    assert persisted.calls == [("notes.txt", b"hello", "text/plain")]
    assert result.files and "[[FILE:" in result.content
    # The name and the links Graph gave are untrusted data.
    inside, outside = fenced_and_open(result.content)
    assert "Name: notes.txt" in inside.splitlines()
    assert f"Download URL: {DOWNLOAD_URL}" in inside.splitlines()
    assert f"Web URL: {WEB_URL}" in inside.splitlines()
    assert "notes.txt" not in outside
    # The redirect hop to the pre-authenticated location carries no bearer token.
    hop = next(r for r in graph.requests if r.url.path == "/dl/notes.txt")
    assert "authorization" not in {k.lower() for k in hop.headers}


# ── Outlook: attachments are listed, read, saved and linked ───────────────


def _email(**overrides: Any) -> dict[str, Any]:
    email = {
        "id": MESSAGE_ID,
        "subject": "Q3 figures",
        "from": {"emailAddress": {"name": "Ann", "address": "ann@contoso.com"}},
        "toRecipients": [{"emailAddress": {"address": "me@contoso.com"}}],
        "ccRecipients": [],
        "receivedDateTime": "2026-09-25T10:00:00Z",
        "body": {"contentType": "html", "content": "<p>See the attached report.</p>"},
        "isRead": True,
        "importance": "normal",
        "flag": {"flagStatus": "notFlagged"},
        "hasAttachments": True,
        "webLink": OUTLOOK_LINK,
    }
    email.update(overrides)
    return email


_ATTACHMENTS = {
    "value": [
        {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "id": "ATT-1==",
            "name": "report.docx",
            "contentType": DOCX_MIME,
            "size": 2048,
            "isInline": False,
        },
        {
            "@odata.type": "#microsoft.graph.itemAttachment",
            "id": "ATT-2==",
            "name": "Fwd: budget",
            "contentType": None,
            "size": 9000,
            "isInline": False,
        },
    ]
}


@pytest.mark.asyncio
async def test_email_lists_its_attachments_and_its_outlook_link(graph):
    graph.on("GET", f"/v1.0/me/messages/{MESSAGE_ID}", _json(_email()))
    graph.on("GET", f"/v1.0/me/messages/{MESSAGE_ID}/attachments", _json(_ATTACHMENTS))

    result = await _call(_executor(), "get_microsoft_email", message_id=MESSAGE_ID)

    assert f"Web link: {OUTLOOK_LINK}" in result.content
    assert "- report.docx (" in result.content and "attachment_id: ATT-1==" in result.content
    assert "attached Outlook item" in result.content
    assert "get_microsoft_attachment" in result.content
    # The attachment list never pulls the bytes.
    listing = next(r for r in graph.requests if r.url.path.endswith("/attachments"))
    assert listing.url.params["$select"] == "id,name,contentType,size,isInline"


@pytest.mark.asyncio
async def test_email_keeps_the_links_its_stripped_body_would_lose(graph):
    """A cloud-file attachment's URL lives in an anchor the plain-text body drops."""
    share = "https://contoso.sharepoint.com/:w:/s/sales/AABB?e=x&amp;at=9"
    email = _email(
        hasAttachments=False,
        body={"contentType": "html", "content": f'<p>Plan: <a href="{share}">plan.docx</a> <a href="{share}">again</a></p>'},
    )
    graph.on("GET", f"/v1.0/me/messages/{MESSAGE_ID}", _json(email))

    result = await _call(_executor(), "get_microsoft_email", message_id=MESSAGE_ID)

    assert "Links in the body:\n- https://contoso.sharepoint.com/:w:/s/sales/AABB?e=x&at=9\n" in result.content
    assert result.content.count("AABB?e=x&at=9") == 1


def test_outlook_upload_chunks_are_320_kib_multiples_under_4_mb():
    assert ms._outlook._OUTLOOK_UPLOAD_CHUNK_BYTES % (320 * 1024) == 0
    assert ms._outlook._OUTLOOK_UPLOAD_CHUNK_BYTES < 4 * 1024 * 1024


@pytest.mark.asyncio
async def test_email_with_only_a_pasted_image_still_lists_it(graph):
    """``hasAttachments`` ignores inline attachments; the ``cid:`` reference does not."""
    email = _email(hasAttachments=False, body={"contentType": "html", "content": '<img src="cid:image001.png@01">'})
    graph.on("GET", f"/v1.0/me/messages/{MESSAGE_ID}", _json(email))
    graph.on(
        "GET",
        f"/v1.0/me/messages/{MESSAGE_ID}/attachments",
        _json({"value": [{"id": "IMG", "name": "image001.png", "contentType": "image/png", "size": 10, "isInline": True}]}),
    )

    result = await _call(_executor(), "get_microsoft_email", message_id=MESSAGE_ID)

    assert "- image001.png (image/png, 0.0 KB, inline) - attachment_id: IMG" in result.content


def _attachment_routes(graph: FakeGraph, meta: dict[str, Any], data: bytes, owner: str | None = None) -> None:
    owner = owner or f"/v1.0/me/messages/{MESSAGE_ID}"
    graph.on("GET", f"{owner}/attachments/{meta['id']}", _json(meta))
    graph.on("GET", f"{owner}/attachments/{meta['id']}/$value", httpx.Response(200, content=data))
    graph.on("GET", owner, _json({"webLink": OUTLOOK_LINK}))


@pytest.mark.asyncio
async def test_attachment_is_saved_read_inline_and_linked(graph):
    persisted = Persisted()
    data = _docx("Revenue grew 12% in Q3.")
    meta = dict(_ATTACHMENTS["value"][0], size=len(data))
    _attachment_routes(graph, meta, data)

    result = await _call(
        _executor(persist_file_fn=persisted),
        "get_microsoft_attachment",
        message_id=MESSAGE_ID,
        attachment_id="ATT-1==",
    )

    assert not result.is_error
    assert persisted.calls == [("report.docx", data, DOCX_MIME)]
    assert "[[FILE:" in result.content and "file_id=" in result.content
    assert "Revenue grew 12% in Q3." in result.content
    assert f"Web link (email): {OUTLOOK_LINK}" in result.content
    # The text is inline: the file is not handed to the provider a second time.
    assert result.files == []
    metadata_call = next(r for r in graph.requests if r.url.path.endswith("/attachments/ATT-1=="))
    assert metadata_call.url.params["$select"] == "id,name,contentType,size,isInline"


@pytest.mark.asyncio
async def test_image_attachment_reaches_the_model_as_pixels(graph):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    _attachment_routes(
        graph,
        {"@odata.type": "#microsoft.graph.fileAttachment", "id": "IMG", "name": "chart.png", "contentType": "image/png", "size": len(png)},
        png,
    )

    result = await _call(
        _executor(persist_file_fn=Persisted()), "get_microsoft_attachment", message_id=MESSAGE_ID, attachment_id="IMG"
    )

    assert [f.filename for f in result.files] == ["chart.png"]
    assert "its pixels are attached" in result.content


@pytest.mark.asyncio
async def test_attached_email_is_saved_as_eml_and_read(graph):
    mime = (
        b"From: Bob <bob@contoso.com>\r\nTo: ann@contoso.com\r\nSubject: budget v2\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\nThe new budget is attached below.\r\n"
    )
    _attachment_routes(graph, dict(_ATTACHMENTS["value"][1]), mime)
    persisted = Persisted()

    result = await _call(
        _executor(persist_file_fn=persisted), "get_microsoft_attachment", message_id=MESSAGE_ID, attachment_id="ATT-2=="
    )

    assert persisted.calls[0][0] == "Fwd: budget.eml"
    assert persisted.calls[0][2] == "message/rfc822"
    assert "Subject: budget v2" in result.content
    assert "The new budget is attached below." in result.content


@pytest.mark.asyncio
async def test_cloud_link_attachment_points_at_the_drive_tools(graph):
    graph.on(
        "GET",
        f"/v1.0/me/messages/{MESSAGE_ID}/attachments/REF",
        _json({"@odata.type": "#microsoft.graph.referenceAttachment", "id": "REF", "name": "plan.docx"}),
    )

    result = await _call(_executor(), "get_microsoft_attachment", message_id=MESSAGE_ID, attachment_id="REF")

    assert "link to a cloud file" in result.content
    assert "download_microsoft_file" in result.content
    assert not any(p.endswith("$value") for p in graph.paths())


@pytest.mark.asyncio
async def test_event_attachment_in_a_shared_calendar(graph):
    owner = "/v1.0/me/calendars/CAL 1/events/EV1"
    _attachment_routes(
        graph,
        {"@odata.type": "#microsoft.graph.fileAttachment", "id": "A1", "name": "agenda.txt", "contentType": "text/plain", "size": 6},
        b"Agenda",
        owner=owner,
    )

    result = await _call(
        _executor(persist_file_fn=Persisted()),
        "get_microsoft_attachment",
        event_id="EV1",
        calendar_id="CAL 1",
        attachment_id="A1",
    )

    assert "from the calendar event" in result.content
    assert "Content:\nAgenda" in result.content


@pytest.mark.asyncio
async def test_attachment_needs_exactly_one_owner(graph):
    executor = _executor()
    both = await _call(executor, "get_microsoft_attachment", message_id=MESSAGE_ID, event_id="EV1", attachment_id="A")
    neither = await _call(executor, "get_microsoft_attachment", attachment_id="A")

    assert both.is_error and neither.is_error
    assert graph.requests == []


@pytest.mark.asyncio
async def test_without_persistence_the_file_is_still_registered_for_the_next_tools(graph):
    _attachment_routes(
        graph,
        {"@odata.type": "#microsoft.graph.fileAttachment", "id": "A1", "name": "notes.txt", "contentType": "text/plain", "size": 2},
        b"hi",
    )
    executor = _executor()
    executor.file_context = FileContext()

    result = await _call(executor, "get_microsoft_attachment", message_id=MESSAGE_ID, attachment_id="A1")

    [kept] = executor.file_context.list_files()
    assert kept.filename == "notes.txt" and kept.inline_data == b"hi"
    assert f"file_id={kept.file_id}" in result.content


@pytest.mark.asyncio
async def test_calendar_event_lists_its_attachments(graph):
    graph.on(
        "GET",
        "/v1.0/me/events/EV1",
        _json({"id": "EV1", "subject": "Review", "start": {}, "end": {}, "hasAttachments": True, "webLink": OUTLOOK_LINK, "body": {"contentType": "html", "content": '<a href="https://contoso.sharepoint.com/s/plan">plan</a>'}}),
    )
    graph.on(
        "GET",
        "/v1.0/me/events/EV1/attachments",
        _json({"value": [{"id": "A1", "name": "deck.pptx", "contentType": "application/pdf", "size": 1024}]}),
    )

    result = await _call(_executor(), "get_microsoft_calendar_event", event_id="EV1")

    assert "- deck.pptx (" in result.content and "attachment_id: A1" in result.content
    assert "event_id" in result.content
    assert "Links in the description:\n- https://contoso.sharepoint.com/s/plan\n" in result.content


# ── Outlook: a conversation file rides on the email ───────────────────────


def _file_context(*refs: FileRef) -> FileContext:
    return FileContext.from_file_refs(list(refs))


def _record_own_mail(monkeypatch) -> dict[str, list]:
    """What the tools record as the running assignment's own mail."""
    recorded: dict[str, list] = {"drafts": [], "sends": []}

    async def _draft(draft_id):
        recorded["drafts"].append(draft_id)

    async def _send(integration_id, subject, recipients, **_kwargs):
        recorded["sends"].append((integration_id, subject, sorted(recipients)))

    monkeypatch.setattr(ms.mail, "record_mail_draft", _draft)
    monkeypatch.setattr(ms.mail, "record_mail_send", _send)
    return recorded


@pytest.mark.asyncio
@pytest.mark.parametrize("sent", [True, False], ids=["sent", "send-failed"])
async def test_an_email_sent_with_attachments_is_recorded_as_the_runs_own_mail(graph, monkeypatch, sent):
    """The draft it goes through, and once it is sent the copy Sent Items files: the
    sending assignment's new_email poller skips both, every other one still sees them."""
    recorded = _record_own_mail(monkeypatch)
    ref = FileRef.from_bytes(data=b"%PDF-1.4 quarterly", filename="Q3.pdf", content_type="application/pdf")
    graph.on("POST", "/v1.0/me/messages", _json({"id": "DRAFT1"}, status=201))
    graph.on("POST", "/v1.0/me/messages/DRAFT1/attachments", _json({"id": "ATT"}, status=201))
    graph.on("GET", "/v1.0/me/messages/DRAFT1/attachments", _listed(("Q3.pdf", 18)))
    graph.on("POST", "/v1.0/me/messages/DRAFT1/send", httpx.Response(202 if sent else 500, json={}))
    executor = _executor(integration_id="mailbox-1")
    executor.file_context = _file_context(ref)

    await _call(
        executor,
        "send_microsoft_email",
        to="bob@contoso.com",
        cc="carol@contoso.com",
        subject="Q3",
        body="Here it is",
        attachment_file_ids=[ref.file_id],
    )

    assert recorded["drafts"] == ["DRAFT1"]
    assert recorded["sends"] == ([("mailbox-1", "Q3", ["bob@contoso.com", "carol@contoso.com"])] if sent else [])


@pytest.mark.asyncio
async def test_send_email_attaches_the_conversation_file_byte_for_byte(graph):
    ref = FileRef.from_bytes(data=b"%PDF-1.4 quarterly", filename="Q3.pdf", content_type="application/pdf")
    graph.on("POST", "/v1.0/me/messages", _json({"id": "DRAFT1"}, status=201))
    graph.on("POST", "/v1.0/me/messages/DRAFT1/attachments", _json({"id": "ATT"}, status=201))
    graph.on("GET", "/v1.0/me/messages/DRAFT1/attachments", _listed(("Q3.pdf", 18)))
    graph.on("POST", "/v1.0/me/messages/DRAFT1/send", httpx.Response(202))
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(
        executor,
        "send_microsoft_email",
        to="bob@contoso.com",
        subject="Q3",
        body="Here it is",
        attachment_file_ids=[ref.file_id],
    )

    assert not result.is_error
    assert f"Attachments: file {ref.file_id} (0.0 KB)" in result.content
    assert "Q3.pdf" not in result.content
    attach = next(r for r in graph.requests if r.url.path.endswith("/attachments"))
    body = json.loads(attach.content)
    assert body["@odata.type"] == "#microsoft.graph.fileAttachment"
    assert base64.b64decode(body["contentBytes"]) == b"%PDF-1.4 quarterly"
    assert body["name"] == "Q3.pdf"
    assert "/v1.0/me/sendMail" not in graph.paths()
    draft = json.loads(next(r for r in graph.requests if r.url.path == "/v1.0/me/messages").content)
    assert draft["toRecipients"] == [{"emailAddress": {"address": "bob@contoso.com"}}]


@pytest.mark.asyncio
async def test_a_large_attachment_goes_through_an_upload_session_without_the_token(graph, monkeypatch):
    monkeypatch.setattr(ms._outlook, "_OUTLOOK_SMALL_ATTACHMENT_BYTES", 8)
    monkeypatch.setattr(ms._outlook, "_OUTLOOK_UPLOAD_CHUNK_BYTES", 10)
    data = bytes(range(25))
    ref = FileRef.from_bytes(data=data, filename="big.bin", content_type="application/octet-stream")
    received: list[bytes] = []

    def _chunk(request: httpx.Request) -> httpx.Response:
        received.append(request.content)
        return httpx.Response(200, json={"nextExpectedRanges": []})

    graph.on("POST", "/v1.0/me/messages", _json({"id": "DRAFT1"}, status=201))
    graph.on(
        "POST",
        "/v1.0/me/messages/DRAFT1/attachments/createUploadSession",
        _json({"uploadUrl": "https://outlook.office.com/api/v2.0/Users('u')/Messages('d')/AttachmentSessions('s')?authtoken=t"}),
    )
    graph.on("PUT", "/api/v2.0/Users('u')/Messages('d')/AttachmentSessions('s')", _chunk)
    graph.on("GET", "/v1.0/me/messages/DRAFT1/attachments", _listed(("big.bin", 25)))
    graph.on("POST", "/v1.0/me/messages/DRAFT1/send", httpx.Response(202))
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(
        executor, "send_microsoft_email", to="bob@contoso.com", subject="s", body="b", attachment_file_ids=ref.file_id
    )

    assert not result.is_error
    assert b"".join(received) == data
    puts = [r for r in graph.requests if r.method == "PUT"]
    assert [r.headers["content-range"] for r in puts] == ["bytes 0-9/25", "bytes 10-19/25", "bytes 20-24/25"]
    assert all("authorization" not in {k.lower() for k in r.headers} for r in puts)
    session = json.loads(next(r for r in graph.requests if r.url.path.endswith("createUploadSession")).content)
    assert session["AttachmentItem"] == {
        "attachmentType": "file",
        "name": "big.bin",
        "size": 25,
        "contentType": "application/octet-stream",
    }


@pytest.mark.asyncio
async def test_send_email_without_attachments_keeps_the_send_mail_call(graph):
    graph.on("POST", "/v1.0/me/sendMail", httpx.Response(202))

    result = await _call(_executor(), "send_microsoft_email", to="bob@contoso.com", subject="s", body="b")

    assert result.content == "Email sent to bob@contoso.com. Subject: 's'."
    assert graph.paths() == ["/v1.0/me/sendMail"]


@pytest.mark.asyncio
async def test_unknown_file_is_refused_before_anything_is_written(graph):
    executor = _executor()
    executor.file_context = FileContext()

    result = await _call(
        executor, "send_microsoft_email", to="bob@contoso.com", subject="s", body="b", attachment_file_ids=["nope"]
    )

    assert result.is_error and "not found in this conversation" in result.content
    assert graph.requests == []


@pytest.mark.asyncio
async def test_a_failed_attachment_keeps_the_draft_and_says_nothing_was_sent(graph):
    ref = FileRef.from_bytes(data=b"x", filename="a.txt", content_type="text/plain")
    graph.on("POST", "/v1.0/me/messages", _json({"id": "DRAFT1"}, status=201))
    graph.on(
        "POST",
        "/v1.0/me/messages/DRAFT1/attachments",
        _json({"error": {"code": "ErrorQuotaExceeded", "message": "full"}}, status=400),
    )
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(
        executor, "send_microsoft_email", to="bob@contoso.com", subject="s", body="b", attachment_file_ids=[ref.file_id]
    )

    assert result.is_error
    assert "The email was NOT sent" in result.content and "DRAFT1" in result.content
    assert "/v1.0/me/messages/DRAFT1/send" not in graph.paths()


@pytest.mark.asyncio
async def test_new_draft_carries_the_attachment(graph):
    ref = FileRef.from_bytes(data=b"hello", filename="notes.txt", content_type="text/plain")
    graph.on("POST", "/v1.0/me/messages", _json({"id": "DRAFT9"}, status=201))
    graph.on("POST", "/v1.0/me/messages/DRAFT9/attachments", _json({"id": "ATT"}, status=201))
    graph.on("GET", "/v1.0/me/messages/DRAFT9/attachments", _listed(("notes.txt", 5)))
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(
        executor,
        "create_microsoft_draft_email",
        to="bob@contoso.com",
        subject="s",
        body="b",
        attachment_file_ids=[ref.file_id],
    )

    assert "Draft ID: DRAFT9" in result.content
    assert f"Attachments: file {ref.file_id} (0.0 KB)" in result.content
    assert "Read back: 1 file attachment(s), 0 inline image(s)" in result.content


@pytest.mark.asyncio
async def test_a_file_named_twice_or_under_two_ids_is_attached_once(graph):
    """One PDF attached twice. The same name and bytes under two ids is one attachment."""
    first = FileRef.from_bytes(data=b"%PDF-1.4 memo", filename="memo.pdf", content_type="application/pdf")
    again = FileRef.from_bytes(data=b"%PDF-1.4 memo", filename="memo.pdf", content_type="application/pdf")
    graph.on("POST", "/v1.0/me/messages", _json({"id": "DRAFT9"}, status=201))
    graph.on("POST", "/v1.0/me/messages/DRAFT9/attachments", _json({"id": "ATT"}, status=201))
    graph.on("GET", "/v1.0/me/messages/DRAFT9/attachments", _listed(("memo.pdf", 13)))
    executor = _executor()
    executor.file_context = _file_context(first, again)

    result = await _call(
        executor,
        "create_microsoft_draft_email",
        to="bob@contoso.com",
        subject="s",
        body="b",
        attachment_file_ids=[first.file_id, again.file_id, first.file_id],
    )

    assert not result.is_error
    assert graph.paths("POST").count("/v1.0/me/messages/DRAFT9/attachments") == 1
    assert result.content.count(f"Attachments: file {first.file_id} (0.0 KB).") == 1
    assert "memo.pdf" not in result.content
    assert "Read back: 1 file attachment(s)" in result.content


@pytest.mark.asyncio
async def test_a_second_copy_of_a_file_on_the_draft_is_removed_and_inline_images_are_left(graph):
    """A retried upload left a second copy: the read-back deletes it and says so; inline images stay."""
    ref = FileRef.from_bytes(data=b"%PDF-1.4 memo", filename="memo.pdf", content_type="application/pdf")
    graph.on("POST", "/v1.0/me/messages", _json({"id": "DRAFT9"}, status=201))
    graph.on("POST", "/v1.0/me/messages/DRAFT9/attachments", _json({"id": "ATT"}, status=201))
    graph.on(
        "GET",
        "/v1.0/me/messages/DRAFT9/attachments",
        _json(
            {
                "value": [
                    {"id": "A1", "name": "memo.pdf", "size": 13, "isInline": False},
                    {"id": "A2", "name": "memo.pdf", "size": 13, "isInline": False},
                    {"id": "I1", "name": "logo.png", "size": 9, "isInline": True},
                ]
            }
        ),
    )
    graph.on("DELETE", "/v1.0/me/messages/DRAFT9/attachments/A2", httpx.Response(204))
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(
        executor, "create_microsoft_draft_email", to="bob@contoso.com", subject="s", body="b",
        attachment_file_ids=[ref.file_id],
    )

    assert graph.paths("DELETE") == ["/v1.0/me/messages/DRAFT9/attachments/A2"]
    assert "Read back: 1 file attachment(s), 1 inline image(s), 1 duplicate copy removed" in result.content


@pytest.mark.asyncio
async def test_draft_reply_carries_the_attachment(graph):
    ref = FileRef.from_bytes(data=b"hello", filename="notes.txt", content_type="text/plain")
    graph.on(
        "POST",
        f"/v1.0/me/messages/{MESSAGE_ID}/createReply",
        _json({"id": "REPLY1", "body": {"content": "<html><body><hr></body></html>"}, "toRecipients": []}),
    )
    graph.on(
        "GET",
        "/v1.0/me/mailFolders/sentitems/messages",
        _json({"value": []}),
    )
    graph.on("PATCH", "/v1.0/me/messages/REPLY1", _json({"id": "REPLY1"}))
    graph.on("POST", "/v1.0/me/messages/REPLY1/attachments", _json({"id": "ATT"}, status=201))
    graph.on("GET", "/v1.0/me/messages/REPLY1/attachments", _listed(("notes.txt", 5)))
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(
        executor,
        "create_microsoft_draft_reply",
        message_id=MESSAGE_ID,
        body="Thanks",
        attachment_file_ids=[ref.file_id],
    )

    assert "Draft ID: REPLY1" in result.content
    assert f"Attachments: file {ref.file_id} (0.0 KB)" in result.content
    assert "/v1.0/me/messages/REPLY1/attachments" in graph.paths("POST")


# ── OneDrive / SharePoint / Teams uploads and file cards ──────────────────


@pytest.mark.asyncio
async def test_upload_into_a_sharepoint_site_library(graph):
    ref = FileRef.from_bytes(data=b"%PDF", filename="q3.pdf", content_type="application/pdf")
    site = "contoso.sharepoint.com,11111111-2222,33333333-4444"
    graph.on(
        "PUT",
        f"/v1.0/sites/{site}/drive/root:/Reports/q3.pdf:/content",
        _json({"id": "I1", "webUrl": "https://contoso.sharepoint.com/sites/x/q3.pdf", "@microsoft.graph.downloadUrl": DOWNLOAD_URL}, status=201),
    )
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(
        executor, "upload_microsoft_drive_file", file_id=ref.file_id, folder="Reports", site_id=site
    )

    assert "to the SharePoint site's document library folder Reports" in result.content
    assert "Web URL: https://contoso.sharepoint.com/sites/x/q3.pdf" in result.content
    assert f"Download URL: {DOWNLOAD_URL}" in result.content
    assert graph.requests[0].content == b"%PDF"
    # The PUT's own Content-Type rides on the session's headers, bearer token included.
    assert graph.requests[0].headers["authorization"] == "Bearer tok"
    assert graph.requests[0].headers["content-type"] == "application/pdf"


@pytest.mark.parametrize(
    "site_id",
    [
        "contoso.sharepoint.com:/sites/sales",
        "contoso.sharepoint.com:/sites/sales:",
        "https://contoso.sharepoint.com/sites/sales",
        "https://contoso.sharepoint.com/sites/sales/Shared%20Documents/Forms/AllItems.aspx",
    ],
)
@pytest.mark.asyncio
async def test_upload_into_a_site_given_by_path_or_url(graph, site_id):
    """The path form and the site URL the SharePoint readers take are resolved
    to the site's id first - URL-encoded into one segment, Graph answered
    "Resource not found for the segment 'root:'" and the brake ended the turn."""
    ref = FileRef.from_bytes(data=b"%PDF", filename="deck.pptx", content_type="application/pdf")
    resolved = "contoso.sharepoint.com,11111111-2222,33333333-4444"
    graph.on("GET", "/v1.0/sites/contoso.sharepoint.com:/sites/sales", _json({"id": resolved}))
    graph.on(
        "PUT",
        f"/v1.0/sites/{resolved}/drive/root:/APJ/2026/deck.pptx:/content",
        _json({"id": "I1", "webUrl": "https://contoso.sharepoint.com/sites/sales/deck.pptx"}, status=201),
    )
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(
        executor, "upload_microsoft_drive_file", file_id=ref.file_id, folder="APJ/2026", site_id=site_id
    )

    assert "Web URL: https://contoso.sharepoint.com/sites/sales/deck.pptx" in result.content
    assert graph.paths("PUT") == [f"/v1.0/sites/{resolved}/drive/root:/APJ/2026/deck.pptx:/content"]


@pytest.mark.asyncio
async def test_upload_into_a_site_the_account_cannot_open_says_so(graph):
    ref = FileRef.from_bytes(data=b"%PDF", filename="q3.pdf", content_type="application/pdf")
    graph.on(
        "GET",
        "/v1.0/sites/contoso.sharepoint.com:/sites/secret",
        _json({"error": {"code": "itemNotFound", "message": "Requested site could not be found"}}, status=404),
    )
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(
        executor, "upload_microsoft_drive_file", file_id=ref.file_id, site_id="contoso.sharepoint.com:/sites/secret"
    )

    assert "site not accessible" in result.content
    assert graph.paths("PUT") == []


FILE_CHAT = "19:chat@thread.v2"
SENDER_ID = "00000000-0000-0000-0000-00000000000a"
MEMBER_ID = "00000000-0000-0000-0000-00000000000b"
CHAT_MEMBERS = [
    {"id": "mm-a", "userId": SENDER_ID, "email": "agent@contoso.com", "displayName": "Agent"},
    {"id": "mm-b", "userId": MEMBER_ID, "email": "ada@contoso.com", "displayName": "Ada"},
]
GRANTED_TO_MEMBER = [
    {"id": "p0", "roles": ["owner"], "grantedToV2": {"user": {"id": SENDER_ID}}},
    {"id": "p1", "roles": ["read"], "grantedToV2": {"user": {"id": MEMBER_ID, "email": "ada@contoso.com"}}},
]


def _share_routes(graph, *, members=CHAT_MEMBERS, invite=None, permissions=GRANTED_TO_MEMBER, item_id="I1"):
    """The Graph calls that share a chat file with the chat's members."""
    graph.on("GET", "/v1.0/me", _json({"id": SENDER_ID}))
    graph.on("GET", f"/v1.0/chats/{FILE_CHAT}/members", _json({"value": members}))
    graph.on("POST", f"/v1.0/me/drive/items/{item_id}/invite", invite or _json({"value": []}))
    graph.on("GET", f"/v1.0/me/drive/items/{item_id}/permissions", _json({"value": permissions}))


@pytest.mark.asyncio
async def test_chat_file_answer_links_the_uploaded_file(graph):
    ref = FileRef.from_bytes(data=b"pdf", filename="report.pdf", content_type="application/pdf")
    item = {
        "id": "I1",
        "name": "report.pdf",
        "eTag": '"{11111111-2222-3333-4444-555555555555},1"',
        "webUrl": "https://contoso-my.sharepoint.com/personal/me/report.pdf",
        "webDavUrl": "https://contoso-my.sharepoint.com/personal/me/dav/report.pdf",
        "@microsoft.graph.downloadUrl": DOWNLOAD_URL,
    }
    graph.on("PUT", "/v1.0/me/drive/root:/Microsoft Teams Chat Files/report.pdf:/content", _json(item, status=201))
    _share_routes(graph)
    graph.on("GET", "/v1.0/me/drive/items/I1", _json(item))
    graph.on("POST", "/v1.0/me/chats/19:chat@thread.v2/messages", _json({"id": "m1"}, status=201))
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(executor, "send_microsoft_chat_file", chat_id="19:chat@thread.v2", file_id=ref.file_id)

    assert "sent to the chat" in result.content
    assert f"Web URL: {item['webUrl']}" in result.content
    assert f"Download URL: {DOWNLOAD_URL}" in result.content
    assert "OneDrive, folder 'Microsoft Teams Chat Files'" in result.content
    assert "Read access granted to the chat's 1 other member(s) and checked: Ada <ada@contoso.com>." in result.content


@pytest.mark.asyncio
async def test_chat_file_is_shared_read_only_with_every_other_member_before_it_is_posted(graph):
    """A file uploaded through Graph is not shared with the chat, so the members
    saw a card they could not open. Every member but the sender is invited read-only,
    without an email, before the message is posted."""
    ref = FileRef.from_bytes(data=b"pdf", filename="report.pdf", content_type="application/pdf")
    item = {"id": "I1", "name": "report.pdf", "eTag": '"{11111111-2222-3333-4444-555555555555},1"',
            "webDavUrl": "https://contoso-my.sharepoint.com/personal/me/dav/report.pdf"}
    graph.on("PUT", "/v1.0/me/drive/root:/Microsoft Teams Chat Files/report.pdf:/content", _json(item, status=201))
    _share_routes(graph)
    graph.on("GET", "/v1.0/me/drive/items/I1", _json(item))
    graph.on("POST", f"/v1.0/me/chats/{FILE_CHAT}/messages", _json({"id": "m1"}, status=201))
    executor = _executor()
    executor.file_context = _file_context(ref)

    await _call(executor, "send_microsoft_chat_file", chat_id=FILE_CHAT, file_id=ref.file_id)

    posts = graph.paths("POST")
    assert posts == ["/v1.0/me/drive/items/I1/invite", f"/v1.0/me/chats/{FILE_CHAT}/messages"]
    invite = next(r for r in graph.requests if r.url.path.endswith("/invite"))
    body = json.loads(invite.content)
    assert body["recipients"] == [{"objectId": MEMBER_ID}]
    assert body["roles"] == ["read"] and body["sendInvitation"] is False and body["requireSignIn"] is True


@pytest.mark.asyncio
async def test_chat_file_whose_grant_fails_is_an_error_and_posts_no_card(graph):
    ref = FileRef.from_bytes(data=b"pdf", filename="report.pdf", content_type="application/pdf")
    item = {"id": "I1", "name": "report.pdf", "eTag": '"{11111111-2222-3333-4444-555555555555},1"',
            "webUrl": "https://contoso-my.sharepoint.com/personal/me/report.pdf"}
    graph.on("PUT", "/v1.0/me/drive/root:/Microsoft Teams Chat Files/report.pdf:/content", _json(item, status=201))
    _share_routes(graph, invite=_json({"error": {"code": "accessDenied", "message": "denied"}}, status=403))
    graph.on("GET", "/v1.0/me/drive/items/I1", _json(item))
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(executor, "send_microsoft_chat_file", chat_id=FILE_CHAT, file_id=ref.file_id)

    assert "could not be shared with the chat's members" in result.content
    assert "sent to the chat" not in result.content
    assert f"/v1.0/me/chats/{FILE_CHAT}/messages" not in graph.paths("POST")


@pytest.mark.asyncio
async def test_chat_file_whose_grant_is_not_read_back_names_the_member_and_posts_no_card(graph):
    ref = FileRef.from_bytes(data=b"pdf", filename="report.pdf", content_type="application/pdf")
    item = {"id": "I1", "name": "report.pdf", "eTag": '"{11111111-2222-3333-4444-555555555555},1"',
            "webUrl": "https://contoso-my.sharepoint.com/personal/me/report.pdf"}
    graph.on("PUT", "/v1.0/me/drive/root:/Microsoft Teams Chat Files/report.pdf:/content", _json(item, status=201))
    _share_routes(graph, permissions=GRANTED_TO_MEMBER[:1])
    graph.on("GET", "/v1.0/me/drive/items/I1", _json(item))
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(executor, "send_microsoft_chat_file", chat_id=FILE_CHAT, file_id=ref.file_id)

    assert "Microsoft lists no access for Ada <ada@contoso.com>" in result.content
    assert f"/v1.0/me/chats/{FILE_CHAT}/messages" not in graph.paths("POST")


def test_chat_share_recipients_skip_the_sender_and_duplicates():
    members = [*CHAT_MEMBERS, CHAT_MEMBERS[1], {"id": "mm-c", "email": "guest@fabrikam.com"}]
    recipients = ms.files._chat_share_recipients(members, SENDER_ID.upper())
    assert [r.drive_recipient() for r in recipients] == [{"objectId": MEMBER_ID}, {"email": "guest@fabrikam.com"}]


def test_a_pending_invitation_counts_as_granted():
    recipients = ms.files._chat_share_recipients([{"email": "guest@fabrikam.com"}], SENDER_ID)
    permissions = [{"roles": ["read"], "invitation": {"email": "Guest@Fabrikam.com"}}]
    assert ms.files._ungranted_recipients(recipients, permissions) == []


@pytest.mark.asyncio
async def test_channel_file_can_reply_in_a_thread(graph):
    ref = FileRef.from_bytes(data=b"pdf", filename="report.pdf", content_type="application/pdf")
    item = {
        "id": "I1",
        "name": "report.pdf",
        "eTag": '"{11111111-2222-3333-4444-555555555555},1"',
        "webUrl": "https://contoso.sharepoint.com/sites/sales/General/report.pdf",
        "webDavUrl": "https://contoso.sharepoint.com/sites/sales/dav/report.pdf",
        "@microsoft.graph.downloadUrl": DOWNLOAD_URL,
    }
    channel = "/v1.0/teams/T1/channels/19:c@thread.tacv2"
    graph.on("GET", f"{channel}/filesFolder", _json({"id": "F1", "parentReference": {"driveId": "D1"}}))
    graph.on("PUT", "/v1.0/drives/D1/items/F1:/report.pdf:/content", _json(item, status=201))
    graph.on("GET", "/v1.0/drives/D1/items/I1", _json(item))
    graph.on("POST", f"{channel}/messages/1695000000000/replies", _json({"id": "r1"}, status=201))
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(
        executor,
        "send_microsoft_channel_file",
        team_id="T1",
        channel_id="19:c@thread.tacv2",
        file_id=ref.file_id,
        reply_to_message_id="1695000000000",
    )

    assert "Message ID: r1" in result.content
    assert f"Web URL: {item['webUrl']}" in result.content
    assert f"Download URL: {DOWNLOAD_URL}" in result.content
    assert f"{channel}/messages/1695000000000/replies" in graph.paths("POST")


def _logged_sends(monkeypatch) -> list[tuple[str, str, dict]]:
    """What the file tools hand to the agent accounts' log of the messages sent."""
    logged: list[tuple[str, str, dict]] = []

    async def _log(self, account_provider, tool, **kwargs):
        logged.append((account_provider, tool, kwargs))

    monkeypatch.setattr(ms._common.MicrosoftContext, "log_account_send", _log)
    return logged


@pytest.mark.asyncio
async def test_a_chat_file_is_logged_as_a_message_sent_and_a_failed_one_as_failed(graph, monkeypatch):
    logged = _logged_sends(monkeypatch)
    ref = FileRef.from_bytes(data=b"pdf", filename="report.pdf", content_type="application/pdf")
    item = {"id": "I1", "name": "report.pdf", "eTag": '"{11111111-2222-3333-4444-555555555555},1"',
            "webDavUrl": "https://contoso-my.sharepoint.com/personal/me/dav/report.pdf"}
    graph.on("PUT", "/v1.0/me/drive/root:/Microsoft Teams Chat Files/report.pdf:/content", _json(item, status=201))
    _share_routes(graph)
    graph.on("GET", "/v1.0/me/drive/items/I1", _json(item))
    graph.on("POST", "/v1.0/me/chats/19:chat@thread.v2/messages", _json({"id": "m1"}, status=201))
    executor = _executor()
    executor.file_context = _file_context(ref)
    await _call(executor, "send_microsoft_chat_file", chat_id="19:chat@thread.v2", file_id=ref.file_id, message="Here")
    ((provider, tool, sent),) = logged
    assert (provider, tool) == ("microsoft_teams", "send_microsoft_chat_file")
    assert sent["conversation_id"] == "19:chat@thread.v2" and sent["message_id"] == "m1"
    assert sent["text"] == "Here\n[File: report.pdf]" and sent["error"] is None

    logged.clear()
    # The first matching route answers: the refusal goes first.
    graph.routes.insert(
        0, ("POST", "/v1.0/me/chats/19:chat@thread.v2/messages", _json({"error": {"message": "no"}}, status=403))
    )
    result = await _call(executor, "send_microsoft_chat_file", chat_id="19:chat@thread.v2", file_id=ref.file_id)
    ((_, _, failed),) = logged
    assert failed["error"] and failed["message_id"] is None and failed["text"] == "[File: report.pdf]"
    assert "sent to the chat" not in result.content


@pytest.mark.asyncio
async def test_a_channel_file_is_logged_as_a_channel_message_sent(graph, monkeypatch):
    logged = _logged_sends(monkeypatch)
    monkeypatch.setattr(ms.files, "record_teams_channel_post", AsyncMock())
    ref = FileRef.from_bytes(data=b"pdf", filename="report.pdf", content_type="application/pdf")
    item = {"id": "I1", "name": "report.pdf", "eTag": '"{11111111-2222-3333-4444-555555555555},1"',
            "webDavUrl": "https://contoso.sharepoint.com/sites/sales/dav/report.pdf"}
    channel = "/v1.0/teams/T1/channels/19:c@thread.tacv2"
    graph.on("GET", f"{channel}/filesFolder", _json({"id": "F1", "parentReference": {"driveId": "D1"}}))
    graph.on("PUT", "/v1.0/drives/D1/items/F1:/report.pdf:/content", _json(item, status=201))
    graph.on("GET", "/v1.0/drives/D1/items/I1", _json(item))
    graph.on("POST", f"{channel}/messages", _json({"id": "p1"}, status=201))
    executor = _executor()
    executor.file_context = _file_context(ref)
    await _call(executor, "send_microsoft_channel_file", team_id="T1", channel_id="19:c@thread.tacv2", file_id=ref.file_id)
    ((provider, tool, sent),) = logged
    assert (provider, tool) == ("microsoft_teams", "send_microsoft_channel_file")
    assert sent["conversation_id"] == "19:c@thread.tacv2" and sent["conversation_type"] == "channel"
    assert sent["message_id"] == "p1" and sent["error"] is None


DECK_NAME = "Q3 2026 Quarterly Review.pdf"
# Bytes a text round-trip would change: NUL, high bytes, CRLF.
DECK_BYTES = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\r\n\x00\xff\xfe binary\r\n%%EOF"
EMT = "/v1.0/teams/T1/channels/19:emt@thread.tacv2"
GUID = "11111111-2222-3333-4444-555555555555"


def _deck() -> FileRef:
    return FileRef.from_bytes(data=DECK_BYTES, filename=DECK_NAME, content_type="application/pdf")


async def _post_deck(monkeypatch) -> tuple[ToolResult, AsyncMock]:
    recorded = AsyncMock()
    monkeypatch.setattr(ms.files, "record_teams_channel_post", recorded)
    ref = _deck()
    executor = _executor()
    executor.file_context = _file_context(ref)
    result = await _call(
        executor,
        "send_microsoft_channel_file",
        team_id="T1",
        channel_id="19:emt@thread.tacv2",
        file_id=ref.file_id,
        message="Q3 2026 Quarterly Review for review",
    )
    return result, recorded


@pytest.mark.asyncio
async def test_channel_file_answers_with_the_message_link_and_the_file_links(graph, monkeypatch):
    item = {
        "id": "I1",
        "name": DECK_NAME,
        "eTag": f'"{{{GUID}}},1"',
        "webUrl": "https://contoso.sharepoint.com/sites/emt/Shared%20Documents/General/Q3%202026%20Quarterly%20Flash%20Update.pdf",
        "webDavUrl": "https://contoso.sharepoint.com/sites/emt/Shared Documents/General/Q3 2026 Quarterly Review.pdf",
        "@microsoft.graph.downloadUrl": DOWNLOAD_URL,
    }
    message_url = "https://teams.microsoft.com/l/message/19%3Aemt%40thread.tacv2/1696582800000?groupId=T1"
    graph.on("GET", f"{EMT}/filesFolder", _json({"id": "F1", "parentReference": {"driveId": "D1"}}))
    graph.on("PUT", f"/v1.0/drives/D1/items/F1:/{DECK_NAME}:/content", _json(item, status=201))
    graph.on("GET", "/v1.0/drives/D1/items/I1", _json(item))
    graph.on("POST", f"{EMT}/messages", _json({"id": "1696582800000", "webUrl": message_url}, status=201))

    result, recorded = await _post_deck(monkeypatch)

    inside, outside = fenced_and_open(result.content)
    assert outside.startswith("File ") and DECK_NAME not in outside
    assert f"Message link: {message_url}" in outside
    assert f"Name: {DECK_NAME}" in inside.splitlines()
    assert f"Web URL: {item['webUrl']}" in inside.splitlines()
    assert f"Download URL: {DOWNLOAD_URL}" in inside.splitlines()
    upload = next(r for r in graph.requests if r.method == "PUT")
    assert upload.content == DECK_BYTES
    assert upload.headers["content-type"] == "application/pdf"
    post = json.loads(next(r for r in graph.requests if r.method == "POST").content)
    assert post["attachments"] == [
        {"id": GUID, "contentType": "reference", "contentUrl": item["webDavUrl"], "name": DECK_NAME}
    ]
    assert post["body"]["content"] == (
        build_graph_chat_body("Q3 2026 Quarterly Review for review")["content"]
        + f'<attachment id="{GUID}"></attachment>'
    )
    recorded.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failed_channel_file_upload_posts_no_message(graph, monkeypatch):
    graph.on("GET", f"{EMT}/filesFolder", _json({"id": "F1", "parentReference": {"driveId": "D1"}}))
    graph.on(
        "PUT",
        f"/v1.0/drives/D1/items/F1:/{DECK_NAME}:/content",
        _json({"error": {"code": "accessDenied", "message": "Access denied"}}, status=403),
    )

    result, recorded = await _post_deck(monkeypatch)

    assert result.content.startswith("Error:")
    assert "upload file" in result.content
    assert graph.paths("POST") == []
    recorded.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_channel_file_for_a_missing_channel_uploads_and_posts_nothing(graph, monkeypatch):
    graph.on(
        "GET",
        f"{EMT}/filesFolder",
        _json({"error": {"code": "NotFound", "message": "Channel not found"}}, status=404),
    )

    result, recorded = await _post_deck(monkeypatch)

    assert result.content.startswith("Error:")
    assert "not found" in result.content
    assert graph.paths("PUT") == []
    assert graph.paths("POST") == []
    recorded.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_read_only_binding_withholds_the_channel_file_tool():
    from app.services.integration_read_only import withhold_write_tools

    executor = CallableToolExecutor()
    definitions = withhold_write_tools(ms.build_tools("tok", executor), executor, integration_label="Microsoft")

    names = {definition.name for definition in definitions}
    assert "send_microsoft_channel_file" not in names
    assert "list_microsoft_channel_messages" in names
    result = await _call(executor, "send_microsoft_channel_file", team_id="T1", channel_id="19:emt@thread.tacv2", file_id="f")
    assert result.is_error
    assert result.content.startswith("Refused: send_microsoft_channel_file")


def test_teams_message_attachment_hint_names_the_download_tool():
    out = ms._teams._format_message_attachments(
        {"attachments": [{"contentType": "reference", "contentUrl": "https://contoso.sharepoint.com/x/plan.docx", "name": "plan.docx"}]}
    )

    assert "get_microsoft_file_by_url" in out and "download_microsoft_file" in out


@pytest.mark.asyncio
async def test_channel_recordings_show_both_links(graph):
    recording = {
        "id": "V1",
        "name": "Standup.mp4",
        "file": {"mimeType": "video/mp4"},
        "size": 10,
        "lastModifiedDateTime": "2026-09-25T09:00:00Z",
        "webUrl": "https://contoso.sharepoint.com/sites/sales/Recordings/Standup.mp4",
        "@microsoft.graph.downloadUrl": DOWNLOAD_URL,
    }
    graph.on("GET", "/v1.0/me/joinedTeams", _json({"value": [{"id": "T1", "displayName": "Sales"}]}))
    graph.on("GET", "/v1.0/teams/T1/channels", _json({"value": [{"id": "C1", "displayName": "General"}]}))
    graph.on("GET", "/v1.0/teams/T1/channels/C1/filesFolder", _json({"id": "F1", "parentReference": {"driveId": "D1"}}))
    graph.on(
        "GET",
        "/v1.0/drives/D1/items/F1/children",
        _json({"value": [{"id": "R1", "name": "Recordings", "folder": {"childCount": 1}}]}),
    )
    graph.on("GET", "/v1.0/drives/D1/items/R1/children", _json({"value": [recording]}))

    result = await _call(_executor(), "get_microsoft_channel_meeting_recordings", team="Sales", channel="General")

    assert f"URL: {DOWNLOAD_URL}" in result.content
    assert f"Web URL: {recording['webUrl']}" in result.content
    assert "download_microsoft_file" in result.content


@pytest.mark.asyncio
async def test_teams_bot_download_hands_on_the_links(graph):
    persisted = Persisted()
    url = "https://contoso.sharepoint.com/sites/x/roadmap.pptx"
    share = f"/v1.0/shares/{ms._encode_share_id(url)}/driveItem"
    graph.on(
        "GET",
        share,
        _json({"id": "I1", "name": "roadmap.pptx", "size": 5, "file": {"mimeType": "application/octet-stream"}, "webUrl": url, "@microsoft.graph.downloadUrl": DOWNLOAD_URL}),
    )
    graph.on("GET", f"{share}/content", httpx.Response(200, content=b"bytes"))
    executor = CallableToolExecutor()
    teams_bot.build_tools("tenant-123||client-abc||secret-xyz", executor, persist_file_fn=persisted)

    with patch.object(teams_bot, "_acquire_token", new=AsyncMock(return_value="tok")):
        result = await _call(executor, "download_teams_bot_file", file_url=url)

    assert persisted.calls[0][1] == b"bytes"
    inside, outside = fenced_and_open(result.content)
    assert "Name: roadmap.pptx" in inside.splitlines()
    assert f"Download URL: {DOWNLOAD_URL}" in inside.splitlines()
    assert f"Web URL: {url}" in inside.splitlines()
    assert "roadmap.pptx" not in outside
    meta_request = next(r for r in graph.requests if r.url.path == share)
    assert "webUrl" in meta_request.url.params["$select"]


# ── Online meeting recordings: streamed up to the cap ────────────

RECORDING = "/v1.0/me/onlineMeetings/MSpMEET=/recordings/REC1"
RECORDING_LINK = f"{GRAPH}/users/u1/onlineMeetings/MSpMEET=/recordings/REC1/content"


def _recording_routes(graph: FakeGraph, content: Any) -> None:
    graph.on(
        "GET",
        RECORDING,
        _json({"id": "REC1", "createdDateTime": "2026-09-25T09:00:00Z", "recordingContentUrl": RECORDING_LINK}),
    )
    graph.on("GET", f"{RECORDING}/content", content)


@pytest.mark.asyncio
async def test_meeting_recording_is_streamed_kept_and_linked(graph):
    persisted = Persisted()
    video = b"\x00\x00\x00\x18ftypmp42" + b"v" * 64
    # Graph answers /content with a redirect to the pre-authenticated location.
    _recording_routes(graph, httpx.Response(302, headers={"Location": "https://contoso.sharepoint.com/rec/standup.mp4"}))
    graph.on("GET", "/rec/standup.mp4", httpx.Response(200, content=video, headers={"Content-Type": "video/mp4"}))

    result = await _call(
        _executor(persist_file_fn=persisted),
        "download_microsoft_meeting_recording",
        meeting_id="MSpMEET=",
        recording_id="REC1",
    )

    assert persisted.calls == [("meeting-recording-2026-09-25.mp4", video, "video/mp4")]
    assert "[[FILE:" in result.content and f"Recording link: {RECORDING_LINK}" in result.content
    assert "list_microsoft_meeting_transcripts" in result.content
    # No model reads a video: it stays off the result, whose files are re-sent.
    assert not result.files
    hop = next(r for r in graph.requests if r.url.host == "contoso.sharepoint.com")
    assert "authorization" not in {k.lower() for k in hop.headers}


@pytest.mark.asyncio
async def test_a_recording_over_the_cap_is_not_downloaded_and_keeps_its_link(graph, monkeypatch):
    monkeypatch.setattr(ms.meetings, "_MS_MAX_FILE_BYTES", 1024 * 1024)
    persisted = Persisted()
    _recording_routes(graph, httpx.Response(200, content=b"v" * (1024 * 1024 + 1), headers={"Content-Type": "video/mp4"}))

    result = await _call(
        _executor(persist_file_fn=persisted),
        "download_microsoft_meeting_recording",
        meeting_id="MSpMEET=",
        recording_id="REC1",
    )

    assert not result.is_error
    assert "over the 1 MB download limit" in result.content
    assert f"Recording link (Microsoft Graph, opened with the organizer's access): {RECORDING_LINK}" in result.content
    assert persisted.calls == []


@pytest.mark.asyncio
async def test_a_refused_recording_says_who_graph_serves_it_to(graph):
    _recording_routes(graph, _json({"error": {"code": "Forbidden", "message": "no"}}, status=403))

    result = await _call(_executor(), "download_microsoft_meeting_recording", meeting_id="MSpMEET=", recording_id="REC1")

    assert result.is_error
    assert "meeting organizer" in result.content and RECORDING_LINK in result.content


@pytest.mark.asyncio
async def test_the_recording_listing_names_the_download_tool(graph):
    graph.on("GET", "/v1.0/me/onlineMeetings/M1/recordings", _json({"value": [{"id": "REC1", "recordingContentUrl": RECORDING_LINK}]}))

    result = await _call(_executor(), "list_microsoft_meeting_recordings", meeting_id="M1")

    assert f"Content URL: {RECORDING_LINK}" in result.content
    assert "download_microsoft_meeting_recording(meeting_id, recording_id)" in result.content


# ── Images pasted in Teams messages: hosted contents ─────────────

CHAT = "19:abc123@thread.v2"
CHANNEL = "19:c0ffee@thread.tacv2"


def _img(path: str) -> str:
    return f'<p>look</p><img src="https://graph.microsoft.com/v1.0/{path}/$value" width="250">'


def test_a_chat_message_names_each_pasted_image_and_its_download_call():
    body = _img(f"chats/{CHAT}/messages/1695/hostedContents/aWQ9eF8w") + _img(
        f"chats/{CHAT}/messages/1695/hostedContents/aWQ9eF8w"
    )
    out = ms._teams._format_message_attachments({"body": {"contentType": "html", "content": body}})

    assert out.count("download_microsoft_chat_message_image(") == 1
    assert f"chat_id={CHAT}, message_id=1695, hosted_content_id=aWQ9eF8w" in out


def test_a_channel_reply_names_its_thread_root():
    body = _img(f"teams/T1/channels/{CHANNEL}/messages/100/replies/200/hostedContents/HC%3D1")
    out = ms._teams._format_message_attachments({"body": {"contentType": "html", "content": body}})

    assert (
        f"download_microsoft_channel_message_image(team_id=T1, channel_id={CHANNEL}, "
        "message_id=200, parent_message_id=100, hosted_content_id=HC=1)"
    ) in out


@pytest.mark.asyncio
async def test_a_pasted_chat_image_is_kept_and_seen(graph):
    persisted = Persisted()
    png = b"\x89PNG\r\n\x1a\n" + b"p" * 32
    message = f"/v1.0/chats/{CHAT}/messages/1695"
    graph.on("GET", f"{message}/hostedContents/HC1/$value", httpx.Response(200, content=png, headers={"Content-Type": "image/png"}))
    graph.on("GET", message, _json({"id": "1695", "webUrl": "https://teams.microsoft.com/l/message/19:abc123@thread.v2/1695"}))

    result = await _call(
        _executor(persist_file_fn=persisted),
        "download_microsoft_chat_message_image",
        chat_id=CHAT,
        message_id="1695",
        hosted_content_id="HC1",
    )

    [(name, data, mime)] = persisted.calls
    assert (data, mime) == (png, "image/png") and name.endswith(".png")
    assert [f.file_id for f in result.files] and "[[FILE:" in result.content
    assert "Message link: https://teams.microsoft.com/l/message/" in result.content


@pytest.mark.asyncio
async def test_an_oversized_pasted_image_is_refused_without_being_buffered(graph, monkeypatch):
    monkeypatch.setattr(ms.files, "_MS_MAX_FILE_BYTES", 1024 * 1024)
    persisted = Persisted()
    message = f"/v1.0/chats/{CHAT}/messages/1695"
    graph.on("GET", f"{message}/hostedContents/HC1/$value", httpx.Response(200, content=b"p" * (1024 * 1024 + 1), headers={"Content-Type": "image/png"}))

    result = await _call(
        _executor(persist_file_fn=persisted),
        "download_microsoft_chat_message_image",
        chat_id=CHAT,
        message_id="1695",
        hosted_content_id="HC1",
    )

    assert result.is_error and "over the 1 MB download limit" in result.content
    assert persisted.calls == []


@pytest.mark.asyncio
async def test_a_message_with_several_images_lists_them_instead_of_guessing(graph):
    message = f"/v1.0/teams/T1/channels/{CHANNEL}/messages/100/replies/200"
    graph.on("GET", f"{message}/hostedContents", _json({"value": [{"id": "HC1"}, {"id": "HC2"}]}))

    result = await _call(
        _executor(persist_file_fn=Persisted()),
        "download_microsoft_channel_message_image",
        team_id="T1",
        channel_id=CHANNEL,
        message_id="200",
        parent_message_id="100",
    )

    assert "holds 2 pasted images" in result.content
    assert "hosted_content_id: HC1" in result.content and "hosted_content_id: HC2" in result.content
    assert not any(p.endswith("$value") for p in graph.paths())


# ── Outlook events carry conversation files ───────────────────────


def _pdf_ref() -> FileRef:
    return FileRef.from_bytes(data=b"%PDF-1.7 agenda", filename="agenda.pdf", content_type="application/pdf")


@pytest.mark.asyncio
async def test_a_new_event_invites_its_attendees_once_the_files_are_attached(graph):
    ref = _pdf_ref()
    graph.on("POST", "/v1.0/me/events", _json({"id": "EV1", "webLink": OUTLOOK_LINK, "start": {}, "end": {}}, status=201))
    graph.on("POST", "/v1.0/me/events/EV1/attachments", _json({"id": "ATT"}, status=201))
    graph.on("PATCH", "/v1.0/me/events/EV1", _json({"id": "EV1"}))
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(
        executor,
        "create_microsoft_calendar_event",
        subject="Review",
        start_datetime="2026-10-01T10:00:00",
        end_datetime="2026-10-01T11:00:00",
        attendees="bob@contoso.com",
        attachment_file_ids=[ref.file_id],
    )

    assert not result.is_error
    assert [(r.method, r.url.path) for r in graph.requests] == [
        ("POST", "/v1.0/me/events"),
        ("POST", "/v1.0/me/events/EV1/attachments"),
        ("PATCH", "/v1.0/me/events/EV1"),
    ]
    created, attached, invited = (json.loads(r.content) for r in graph.requests)
    assert "attendees" not in created
    assert base64.b64decode(attached["contentBytes"]) == b"%PDF-1.7 agenda"
    assert invited == {"attendees": [{"emailAddress": {"address": "bob@contoso.com"}, "type": "required"}]}
    assert f"Attachments: file {ref.file_id} (0.0 KB)" in result.content
    assert "agenda.pdf" not in result.content
    assert "invitation carries the attachments" in result.content


@pytest.mark.asyncio
async def test_a_new_event_without_files_is_created_with_its_attendees(graph):
    graph.on("POST", "/v1.0/me/events", _json({"id": "EV1", "start": {}, "end": {}}, status=201))

    await _call(
        _executor(),
        "create_microsoft_calendar_event",
        subject="Review",
        start_datetime="2026-10-01T10:00:00",
        end_datetime="2026-10-01T11:00:00",
        attendees="bob@contoso.com",
    )

    assert graph.paths() == ["/v1.0/me/events"]
    assert json.loads(graph.requests[0].content)["attendees"][0]["emailAddress"]["address"] == "bob@contoso.com"


@pytest.mark.asyncio
async def test_a_failed_attachment_leaves_the_attendees_uninvited(graph):
    ref = _pdf_ref()
    graph.on("POST", "/v1.0/me/events", _json({"id": "EV1", "start": {}, "end": {}}, status=201))
    graph.on("POST", "/v1.0/me/events/EV1/attachments", _json({"error": {"code": "ErrorQuotaExceeded", "message": "full"}}, status=400))
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(
        executor,
        "create_microsoft_calendar_event",
        subject="Review",
        start_datetime="2026-10-01T10:00:00",
        end_datetime="2026-10-01T11:00:00",
        attendees="bob@contoso.com",
        attachment_file_ids=[ref.file_id],
    )

    assert result.is_error
    assert "It was NOT sent to the attendees" in result.content and "EV1" in result.content
    assert graph.paths("PATCH") == []


@pytest.mark.asyncio
async def test_a_large_event_attachment_goes_through_the_events_upload_session(graph, monkeypatch):
    monkeypatch.setattr(ms._outlook, "_OUTLOOK_SMALL_ATTACHMENT_BYTES", 8)
    ref = FileRef.from_bytes(data=bytes(range(20)), filename="big.bin", content_type="application/octet-stream")
    graph.on("POST", "/v1.0/me/events", _json({"id": "EV1", "start": {}, "end": {}}, status=201))
    graph.on(
        "POST",
        "/v1.0/me/events/EV1/attachments/createUploadSession",
        _json({"uploadUrl": "https://outlook.office.com/api/v2.0/Users('u')/Events('e')/AttachmentSessions('s')?authtoken=t"}),
    )
    graph.on("PUT", "/api/v2.0/Users('u')/Events('e')/AttachmentSessions('s')", _json({"nextExpectedRanges": []}))
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(
        executor,
        "create_microsoft_calendar_event",
        subject="Review",
        start_datetime="2026-10-01T10:00:00",
        end_datetime="2026-10-01T11:00:00",
        attachment_file_ids=[ref.file_id],
    )

    assert not result.is_error and f"Attachments: file {ref.file_id} (0.0 KB)" in result.content
    [put] = [r for r in graph.requests if r.method == "PUT"]
    assert put.content == bytes(range(20)) and "authorization" not in {k.lower() for k in put.headers}


def _current_event(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "EV1",
        "subject": "Review",
        "isOrganizer": True,
        "attendees": [{"emailAddress": {"address": "bob@contoso.com"}}],
        "body": {"contentType": "html", "content": "<p>Agenda</p><div>meeting blob</div>"},
        **overrides,
    }


@pytest.mark.asyncio
async def test_an_update_attaching_files_sends_the_organizers_update_to_every_attendee(graph):
    ref = _pdf_ref()
    graph.on("GET", "/v1.0/me/events/EV1", _json(_current_event()))
    graph.on("POST", "/v1.0/me/events/EV1/attachments", _json({"id": "ATT"}, status=201))
    graph.on("PATCH", "/v1.0/me/events/EV1", _json({"id": "EV1", "subject": "Review"}))
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(executor, "update_microsoft_calendar_event", event_id="EV1", attachment_file_ids=[ref.file_id])

    assert [r.method for r in graph.requests] == ["GET", "POST", "PATCH"]
    # The body (meeting blob kept) makes the update reach every attendee.
    assert json.loads(graph.requests[-1].content) == {"body": _current_event()["body"]}
    assert f"Attachments: file {ref.file_id} (0.0 KB)" in result.content
    assert "update sent to the attendees carries the attachments" in result.content


@pytest.mark.asyncio
async def test_an_attendee_adds_files_to_their_own_copy_only(graph):
    ref = _pdf_ref()
    graph.on("GET", "/v1.0/me/events/EV1", _json(_current_event(isOrganizer=False)))
    graph.on("POST", "/v1.0/me/events/EV1/attachments", _json({"id": "ATT"}, status=201))
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(executor, "update_microsoft_calendar_event", event_id="EV1", attachment_file_ids=[ref.file_id])

    assert graph.paths("PATCH") == []
    assert "not the organizer" in result.content and "your copy" in result.content


# ── A forwarded file's stored name reaches the model only as untrusted data


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_a_drive_upload_names_the_file_by_its_id_and_fences_its_name(graph, hostile):
    name = HOSTILE_FILE_NAMES[hostile]
    ref = FileRef.from_bytes(data=b"%PDF", filename=name, content_type="application/pdf")
    put_path = httpx.URL(f"{ms._BASE_URL}/me/drive/root:/{_encode_drive_filename(name)}:/content").path
    web_url = f"https://contoso-my.sharepoint.com/personal/u/Documents/{_encode_drive_filename(name)}"
    graph.on("PUT", put_path, _json({"id": "I1", "name": name, "webUrl": web_url}, status=201))
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(executor, "upload_microsoft_drive_file", file_id=ref.file_id)

    outside = assert_name_only_inside(result.content, name)
    assert outside == f"Uploaded file {ref.file_id} (0.0 KB) to OneDrive.\n"
    assert graph.requests[0].content == b"%PDF"


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
async def test_an_email_attachment_is_named_by_its_id_never_by_its_name(graph, hostile):
    name = HOSTILE_FILE_NAMES[hostile]
    ref = FileRef.from_bytes(data=b"%PDF-1.4 quarterly", filename=name, content_type="application/pdf")
    graph.on("POST", "/v1.0/me/messages", _json({"id": "DRAFT1"}, status=201))
    graph.on("POST", "/v1.0/me/messages/DRAFT1/attachments", _json({"id": "ATT"}, status=201))
    graph.on("GET", "/v1.0/me/messages/DRAFT1/attachments", _listed((name, 18)))
    graph.on("POST", "/v1.0/me/messages/DRAFT1/send", httpx.Response(202))
    executor = _executor()
    executor.file_context = _file_context(ref)

    result = await _call(
        executor,
        "send_microsoft_email",
        to="bob@contoso.com",
        subject="Q3",
        body="Here it is",
        attachment_file_ids=[ref.file_id],
    )

    assert not result.is_error, result.content
    assert "PWNED" not in result.content and "UNTRUSTED" not in result.content.upper()
    assert f"Attachments: file {ref.file_id} (0.0 KB)" in result.content
    attach = next(r for r in graph.requests if r.url.path.endswith("/attachments"))
    assert json.loads(attach.content)["name"] == name
