"""PDFs reach OpenAI, and the gateway's PDF-capable upstreams, natively (#2519).

Before, ``PROVIDER_NATIVE_MIMES`` listed only images for ``openai`` and the
gateway resolved every file as ``openai_compat``: each PDF became our
extracted text, even for Claude behind the gateway.
"""

import base64
import json
import sys
from types import SimpleNamespace

import httpx
import pytest

from app.llm.base import ChatMessage, FileRef, build_openai_messages_async
from app.llm.file_resolver import PDF_TOKENS_PER_PAGE, PROVIDER_PDF_PAGE_LIMITS
from app.llm.gateway_provider import FiligranGatewayProvider
from app.llm.identity import set_llm_caller

_PDF_BYTES = b"%PDF-1.4 fake"
_PDF_DATA_URL = "data:application/pdf;base64," + base64.b64encode(_PDF_BYTES).decode()
_FILE_ID = "6f9619ff-8b86-d011-b42d-00c04fc964ff"
_FILE_PART = {"type": "file", "file": {"filename": f"{_FILE_ID}.pdf", "file_data": _PDF_DATA_URL}}


def _install_fake_pypdf(monkeypatch, page_count: int) -> None:
    class _FakePdfReader:
        def __init__(self, _stream):
            self.pages = [SimpleNamespace(extract_text=lambda: "extracted page text") for _ in range(page_count)]

    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=_FakePdfReader))


def _turn() -> list[ChatMessage]:
    pdf = FileRef.from_bytes(_PDF_BYTES, "report.pdf", "application/pdf", file_id=_FILE_ID)
    return [ChatMessage(role="user", content="Summarise it.", files=[pdf])]


def _parts(oai_messages: list[dict]) -> list:
    content = oai_messages[-1]["content"]
    return content if isinstance(content, list) else [content]


async def test_openai_gets_the_pdf_as_a_file_part(monkeypatch):
    _install_fake_pypdf(monkeypatch, 3)
    oai = await build_openai_messages_async(_turn(), "openai")
    assert _parts(oai) == [{"type": "text", "text": "Summarise it."}, _FILE_PART]


async def test_openai_over_its_page_limit_gets_the_text(monkeypatch):
    _install_fake_pypdf(monkeypatch, PROVIDER_PDF_PAGE_LIMITS["openai"] + 1)
    oai = await build_openai_messages_async(_turn(), "openai")
    assert _FILE_PART not in _parts(oai)
    assert "extracted page text" in json.dumps(oai)


async def test_an_openai_compatible_backend_still_gets_the_text(monkeypatch):
    _install_fake_pypdf(monkeypatch, 3)
    oai = await build_openai_messages_async(_turn(), "openai_compat")
    assert "extracted page text" in json.dumps(oai)
    assert "file_data" not in json.dumps(oai)


async def test_the_payload_costs_a_native_pdf_by_its_pages(monkeypatch):
    """The overflow check reads pages, not the bytes of the base64 file part."""
    from app.llm.base import BaseLLMProvider, build_openai_payload

    _install_fake_pypdf(monkeypatch, 3)
    oai, pdf_tokens = await build_openai_payload(_turn(), "openai", context_window=1_000_000)
    assert _FILE_PART in _parts(oai)
    assert pdf_tokens == 3 * PDF_TOKENS_PER_PAGE
    assert BaseLLMProvider._measure_oai_payload(oai)[2] == 0


def _gateway(sent: dict, model: str, owner: str) -> FiligranGatewayProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"object": "list", "data": [
                {"id": model, "object": "model", "created": 0, "owned_by": owner},
            ]})
        sent.update(json.loads(request.content))
        return httpx.Response(200, json={
            "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        })

    return FiligranGatewayProvider(
        api_key="https://gateway.test||openai||tok",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize(
    ("model", "owner", "native"),
    [
        ("claude-opus-5-5", "anthropic", True),
        ("gemini-3-pro", "gemini", True),
        ("default", "openai", True),
        ("default", "azure_openai", False),
        ("grok-4", "xai", False),
    ],
)
async def test_the_gateway_sends_the_pdf_natively_to_an_upstream_that_reads_it(monkeypatch, model, owner, native):
    _install_fake_pypdf(monkeypatch, 3)
    set_llm_caller("00000000-0000-0000-0000-000000000001", "u@example.com")
    sent: dict = {}
    provider = _gateway(sent, model, owner)
    provider._target = None
    await provider.chat(_turn(), model=model)
    body = json.dumps(sent["messages"])
    assert ("file_data" in body) is native
    assert ("extracted page text" in body) is not native


async def test_a_pdf_over_the_request_byte_cap_gets_the_text(monkeypatch):
    """Bytes, not only pages: a few large scans exceed the request size cap (#2519 review)."""
    from app.llm import file_resolver

    _install_fake_pypdf(monkeypatch, 3)
    monkeypatch.setitem(file_resolver.PROVIDER_PDF_BYTE_LIMITS, "openai", len(_PDF_BYTES) - 1)
    oai = await build_openai_messages_async(_turn(), "openai")
    assert "file_data" not in json.dumps(oai)
    assert "extracted page text" in json.dumps(oai)


async def test_older_pdfs_go_to_text_once_the_byte_cap_is_spent(monkeypatch):
    from app.llm import file_resolver

    _install_fake_pypdf(monkeypatch, 3)
    older = FileRef.from_bytes(b"%PDF-1.4 older", "older.pdf", "application/pdf")
    messages = [ChatMessage(role="user", content="first", files=[older]), *_turn()]
    monkeypatch.setitem(file_resolver.PROVIDER_PDF_BYTE_LIMITS, "openai", len(_PDF_BYTES) + 1)
    plan = await file_resolver.resolve_payload_files(messages, "openai", context_window=1_000_000)
    assert isinstance(plan.get(1, 0), file_resolver.NativeFile)
    assert not isinstance(plan.get(0, 0), file_resolver.NativeFile)
