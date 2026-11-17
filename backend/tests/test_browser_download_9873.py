"""The browser tool's ``download`` action (#9873).

A file the page serves behind its sign-in (a lab's PDF datasheet) is fetched by
the page itself and kept as a file of the conversation: the URL is resolved
against the page and held to the session's URL policy, the headers expression
runs in the page, the bytes reach the platform (never the model), and a PDF
answers its page count. Nothing here launches a browser: a fake page answers
the fetch script.
"""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pypdf import PdfWriter

from app.services import browser_tool
from app.services.builtin_tools import build_browser_tool, web
from app.tools.base import ToolCall, ToolResult

PAGE = "https://lab.example.com/fleets/1"


def _pdf(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class FakePage:
    def __init__(self, answer: dict) -> None:
        self.url = PAGE
        self.answer = answer
        self.scripts: list[tuple[str, object]] = []

    async def evaluate(self, script: str, arg: object = None):
        self.scripts.append((script, arg))
        return self.answer

    def on(self, event: str, handler) -> None:
        return None


@pytest.fixture
def lab(monkeypatch):
    def install(answer: dict, *, refuse: str | None = None) -> FakePage:
        fake = FakePage(answer)
        session = browser_tool.BrowserSession(pw=None, browser=None, context=None, page=fake)
        monkeypatch.setattr(browser_tool, "_get_or_create_session", AsyncMock(return_value=session))

        async def policy(url: str, session=None) -> str | None:
            return refuse

        monkeypatch.setattr(browser_tool, "_url_policy_refusal", policy)
        return fake

    return install


@pytest.fixture
def kept(monkeypatch):
    calls: list[dict] = []

    async def persist(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(file_id="file-1", filename=kwargs["filename"], size=len(kwargs["raw_bytes"]))

    monkeypatch.setattr(web, "persist_tool_artifact", persist)
    return calls


async def _call(arguments: dict, **context) -> ToolResult:
    _defs, executor = build_browser_tool("agent:conv", **context)
    return await executor.execute_tool(ToolCall(id="call-1", name="browser", arguments=arguments))


def _answer(body: bytes, content_type: str = "application/pdf", status: int = 200) -> dict:
    return {"status": status, "type": content_type, "size": len(body), "data": base64.b64encode(body).decode()}


def test_the_schema_offers_download():
    defs, _executor = build_browser_tool("agent:conv")
    assert "download" in defs[0].input_schema["properties"]["action"]["enum"]
    assert "- download:" in defs[0].description
    assert "download" in defs[0].input_schema["properties"]["url"]["description"]


async def test_a_lab_pdf_is_kept_with_its_page_count(lab, kept):
    page = lab(_answer(_pdf(3)))
    result = await _call(
        {"action": "download", "url": "/api/v1/fleets/1/documentation/datasheet.pdf",
         "expression": "({Authorization: 'Bearer ' + localStorage.getItem('copilot_token')})"},
        db=object(), user_id="u1", conversation_id="c1",
    )
    assert not result.is_error, result.content
    script, arg = page.scripts[0]
    # Resolved against the page, the size cap passed, the headers expression run in the page.
    assert arg == [
        "https://lab.example.com/api/v1/fleets/1/documentation/datasheet.pdf", browser_tool.MAX_DOWNLOAD_BYTES, "GET", None
    ]
    assert "localStorage.getItem('copilot_token')" in script
    assert "credentials: 'include'" in script
    assert "A PDF of 3 page(s)." in result.content
    assert "render_source_pages" in result.content and "file-1" in result.content
    assert kept[0]["filename"] == "datasheet.pdf"
    assert kept[0]["content_type"] == "application/pdf"
    assert kept[0]["raw_bytes"].startswith(b"%PDF")
    # The bytes never reach the model.
    assert "JVBER" not in result.content


async def test_no_expression_sends_no_headers(lab, kept):
    page = lab(_answer(b"a,b\n1,2\n", "text/csv"))
    result = await _call({"action": "download", "url": "export.csv"}, db=object(), user_id="u1")
    assert not result.is_error, result.content
    assert "(async () => ({}))()" in page.scripts[0][0]
    assert "page(s)" not in result.content


async def test_a_url_the_policy_refuses_is_not_fetched(lab, kept):
    page = lab(_answer(b"x"), refuse="a private address")
    result = await _call({"action": "download", "url": "http://10.0.0.1/x.pdf"}, db=object(), user_id="u1")
    assert result.is_error
    assert "URL policy refuses" in result.content
    assert page.scripts == [] and kept == []


async def test_an_http_error_keeps_nothing(lab, kept):
    lab({"status": 403, "type": "application/json", "size": 10, "data": None})
    result = await _call({"action": "download", "url": "/x.pdf"}, db=object(), user_id="u1")
    assert result.is_error and "HTTP 403" in result.content
    assert kept == []


async def test_an_oversized_response_keeps_nothing(lab, kept):
    lab({"status": 200, "type": "application/pdf", "size": 99_000_000, "data": None, "tooLarge": True})
    result = await _call({"action": "download", "url": "/x.pdf"}, db=object(), user_id="u1")
    assert result.is_error and "over the" in result.content
    assert kept == []


async def test_url_is_required(lab, kept):
    result = await _call({"action": "download"})
    assert "'url' is required for the download action" in result.content


async def test_without_storage_it_says_so(lab):
    lab(_answer(_pdf(1)))
    result = await _call({"action": "download", "url": "/x.pdf"})
    assert result.is_error and "could not be kept" in result.content


def test_the_schema_offers_method_and_body():
    defs, _executor = build_browser_tool("agent:conv")
    properties = defs[0].input_schema["properties"]
    assert properties["method"]["enum"] == ["GET", "POST"]
    assert properties["body"]["type"] == "string"
    assert "method POST with body" in defs[0].description


async def test_a_post_only_export_is_kept(lab, kept):
    """#10218: an export served from a POST route with a JSON body (a deck's PDF)."""
    page = lab(_answer(_pdf(2)))
    result = await _call(
        {"action": "download", "url": "/api/v1/presentations/p1/export", "method": "post",
         "body": '{"format": "pdf"}',
         "expression": "({Authorization: 'Bearer ' + localStorage.getItem('copilot_token')})"},
        db=object(), user_id="u1", conversation_id="c1",
    )
    assert not result.is_error, result.content
    script, arg = page.scripts[0]
    # Method and body travel as arguments, never spliced into the script.
    assert arg == ["https://lab.example.com/api/v1/presentations/p1/export", browser_tool.MAX_DOWNLOAD_BYTES, "POST",
                   '{"format": "pdf"}']
    assert '"format"' not in script
    assert "'Content-Type'" in script and "application/json" in script
    assert "A PDF of 2 page(s)." in result.content
    assert kept[0]["raw_bytes"].startswith(b"%PDF")


async def test_a_body_without_post_is_refused(lab, kept):
    page = lab(_answer(b"x"))
    result = await _call({"action": "download", "url": "/x", "body": "{}"}, db=object(), user_id="u1")
    assert result.is_error and "method POST only" in result.content
    assert page.scripts == [] and kept == []


async def test_an_invalid_json_body_is_refused(lab, kept):
    page = lab(_answer(b"x"))
    result = await _call(
        {"action": "download", "url": "/x", "method": "POST", "body": "{format: pdf}"}, db=object(), user_id="u1"
    )
    assert result.is_error and "not valid JSON" in result.content
    assert page.scripts == []


async def test_an_unknown_method_is_refused(lab, kept):
    page = lab(_answer(b"x"))
    result = await _call({"action": "download", "url": "/x", "method": "DELETE"}, db=object(), user_id="u1")
    assert result.is_error and "is not one of GET, POST" in result.content
    assert page.scripts == []


async def test_a_read_only_profile_refuses_a_post(lab, kept, monkeypatch):
    page = lab(_answer(b"x"))
    monkeypatch.setattr(
        browser_tool, "session_profile", lambda key: SimpleNamespace(name="Lab", read_only=True)
    )
    result = await _call(
        {"action": "download", "url": "/x", "method": "POST", "body": "{}"}, db=object(), user_id="u1"
    )
    assert result.is_error and "a POST may change state" in result.content
    assert page.scripts == []


async def test_browser_download_checks_method_before_fetching(lab):
    page = lab(_answer(b"x"))
    with pytest.raises(ValueError, match="POST only"):
        await browser_tool.browser_download("agent:conv", "/x", body="{}")
    with pytest.raises(ValueError, match="not one of"):
        await browser_tool.browser_download("agent:conv", "/x", method="PUT")
    assert page.scripts == []


def test_page_count_reads_pdfs_only():
    assert web.download_page_count(_pdf(2), "application/pdf") == 2
    assert web.download_page_count(b"plain", "text/plain") is None
    assert web.download_page_count(b"%PDF-broken", "application/pdf") is None
