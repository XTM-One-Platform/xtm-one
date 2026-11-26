"""Per-request attachment budget in the file resolver.

Chat attachments live in the conversation history and are re-sent on every
turn. Resolved one at a time, nothing bounded their sum: a conversation
carrying several PDFs reached Anthropic's 1M-token and 600-page request caps
and then failed on EVERY turn, because the payload being refused was the
history itself. ``resolve_payload_files`` resolves the attachments of one
request together, newest first, under the provider's page cap and a share of
the model's context window, and never sends the same bytes twice.
"""

from __future__ import annotations

import re
import sys
from types import SimpleNamespace

import pytest

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import (
    ChatMessage,
    ContextOverflowError,
    FileRef,
    build_openai_messages_async,
)
from app.llm.bedrock_provider import BedrockProvider
from app.llm.file_resolver import (
    ATTACHMENT_WINDOW_SHARE,
    PDF_TOKENS_PER_PAGE,
    PROVIDER_PDF_PAGE_LIMITS,
    FallbackText,
    NativeFile,
    _estimate_text_tokens,
    attachment_token_budget,
    resolve_payload_files,
)
from app.llm.gemini_provider import GeminiProvider
from app.llm.router import FailoverLLMProvider, is_request_too_large_error


def _install_fake_pypdf(monkeypatch) -> None:
    """A ``pypdf`` whose page count is written in the PDF bytes themselves.

    ``b"%PDF pages=250"`` reads as a 250-page document, so one fake serves a
    request mixing PDFs of different sizes.
    """

    class _FakePdfReader:
        def __init__(self, stream):
            data = stream.read()
            match = re.search(rb"pages=(\d+)", data)
            count = int(match.group(1)) if match else 1
            self.pages = [
                SimpleNamespace(extract_text=lambda: "extracted page text")
                for _ in range(count)
            ]

    monkeypatch.setitem(
        sys.modules, "pypdf", SimpleNamespace(PdfReader=_FakePdfReader)
    )


def _pdf(name: str, pages: int, *, salt: str = "") -> FileRef:
    return FileRef.from_bytes(
        f"%PDF-1.4 pages={pages} {salt}".encode(), name, "application/pdf"
    )


def _png(name: str, payload: bytes = b"\x89PNG fake") -> FileRef:
    return FileRef.from_bytes(payload, name, "image/png")


def _txt(name: str, text: str) -> FileRef:
    return FileRef.from_bytes(text.encode(), name, "text/plain")


def _user(text: str, *files: FileRef) -> ChatMessage:
    return ChatMessage(role="user", content=text, files=list(files) or None)


def _kinds(plan, messages) -> list[list[str]]:
    """``native`` / ``text`` / ``stub`` per file, in message order."""
    out: list[list[str]] = []
    for m_idx, msg in enumerate(messages):
        row: list[str] = []
        for f_idx, _ in enumerate(msg.files or []):
            resolved = plan.get(m_idx, f_idx)
            if isinstance(resolved, NativeFile):
                row.append("native")
            elif resolved.text.startswith("[File ") and "extracted page text" not in resolved.text:
                row.append("stub")
            else:
                row.append("text")
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# The request-wide page cap
# ---------------------------------------------------------------------------


async def test_the_page_cap_is_spent_newest_first(monkeypatch):
    """Three 250-page PDFs against Anthropic's 600: the two newest stay native,
    the oldest is the one that becomes text."""
    _install_fake_pypdf(monkeypatch)
    messages = [
        _user("first", _pdf("a.pdf", 250, salt="a")),
        ChatMessage(role="assistant", content="ok"),
        _user("second", _pdf("b.pdf", 250, salt="b")),
        ChatMessage(role="assistant", content="ok"),
        _user("third", _pdf("c.pdf", 250, salt="c")),
    ]

    plan = await resolve_payload_files(messages, "anthropic")

    assert _kinds(plan, messages) == [["text"], [], ["native"], [], ["native"]]
    assert plan.native_pdf_pages == 500
    assert plan.native_pdf_pages <= PROVIDER_PDF_PAGE_LIMITS["anthropic"]
    assert plan.native_pdf_tokens == 500 * PDF_TOKENS_PER_PAGE
    assert plan.degraded == ["a.pdf"]
    # The degraded file is real text the model can still read.
    assert "extracted page text" in plan.get(0, 0).text


async def test_the_newest_files_never_lose_to_older_ones(monkeypatch):
    """A cap the newest message alone exhausts leaves every older PDF as text."""
    _install_fake_pypdf(monkeypatch)
    messages = [
        _user("old", _pdf("old.pdf", 10, salt="o")),
        _user("new", _pdf("big.pdf", 595, salt="n")),
    ]

    plan = await resolve_payload_files(messages, "anthropic")

    assert _kinds(plan, messages) == [["text"], ["native"]]


async def test_a_single_pdf_over_the_per_file_cap_still_falls_back(monkeypatch):
    """Unchanged: one 601-page PDF is text on its own, whatever the
    rest of the request looks like."""
    _install_fake_pypdf(monkeypatch)
    messages = [_user("one", _pdf("huge.pdf", PROVIDER_PDF_PAGE_LIMITS["anthropic"] + 1))]

    plan = await resolve_payload_files(messages, "anthropic")

    assert _kinds(plan, messages) == [["text"]]
    assert plan.native_pdf_pages == 0


async def test_gemini_has_its_own_cap(monkeypatch):
    _install_fake_pypdf(monkeypatch)
    messages = [
        _user("a", _pdf("a.pdf", 500, salt="a")),
        _user("b", _pdf("b.pdf", 400, salt="b")),
    ]

    # 900 pages fit Gemini's 1000...
    assert _kinds(await resolve_payload_files(messages, "gemini"), messages) == [
        ["native"],
        ["native"],
    ]
    # ...and not Anthropic's 600.
    assert _kinds(await resolve_payload_files(messages, "anthropic"), messages) == [
        ["text"],
        ["native"],
    ]


# ---------------------------------------------------------------------------
# The context-window share
# ---------------------------------------------------------------------------


def test_the_attachment_budget_is_a_share_of_the_window():
    assert attachment_token_budget(1_000_000) == int(1_000_000 * ATTACHMENT_WINDOW_SHARE)
    assert attachment_token_budget(None) is None
    assert attachment_token_budget(0) is None


async def test_pdfs_are_costed_by_page_against_the_window(monkeypatch):
    """A 60-page PDF is ~120k tokens - far more than its bytes suggest. On a
    200k window the attachment budget is 100k, so it does not fit; a 40-page
    one (~80k) does."""
    _install_fake_pypdf(monkeypatch)
    too_big = [_user("x", _pdf("sixty.pdf", 60))]
    fits = [_user("x", _pdf("forty.pdf", 40))]

    assert _kinds(
        await resolve_payload_files(too_big, "anthropic", context_window=200_000),
        too_big,
    ) == [["text"]]
    assert _kinds(
        await resolve_payload_files(fits, "anthropic", context_window=200_000),
        fits,
    ) == [["native"]]


async def test_text_fallbacks_draw_on_the_same_budget(monkeypatch):
    """Degrading to text is not free either: once the budget is spent, an
    older attachment becomes a one-line stub that says it was omitted and
    can be re-attached - never a payload that fails the turn."""
    _install_fake_pypdf(monkeypatch)
    # A 1000-page PDF is ~2M tokens natively and ~7k tokens as text ("extracted
    # page text" x 1000). The budget of a 10k window is 5k tokens: the newest
    # goes to text regardless (see below), and the older ones have no room
    # left even for their text.
    messages = [
        _user("a", _pdf("a.pdf", 1000, salt="a")),
        _user("b", _pdf("b.pdf", 1000, salt="b")),
        _user("c", _pdf("c.pdf", 1000, salt="c")),
    ]

    plan = await resolve_payload_files(messages, "gemini", context_window=10_000)

    kinds = _kinds(plan, messages)
    assert kinds[2] == ["text"]
    assert kinds[0] == ["stub"] and kinds[1] == ["stub"]
    assert plan.omitted == ["b.pdf", "a.pdf"]
    stub = plan.get(0, 0)
    assert isinstance(stub, FallbackText)
    assert "a.pdf" in stub.text
    assert "1000 pages" in stub.text
    assert "attach it again" in stub.text


async def test_the_newest_attachment_is_never_stubbed(monkeypatch):
    """What the user just sent is read at least as text, budget or not: a
    turn that cannot see its own attachment is not worth saving. Whether that
    text fits at all is the overflow check's call, with real numbers."""
    _install_fake_pypdf(monkeypatch)
    messages = [_user("read this", _pdf("now.pdf", 1000))]

    plan = await resolve_payload_files(messages, "gemini", context_window=1_000)

    resolved = plan.get(0, 0)
    assert isinstance(resolved, FallbackText)
    assert "extracted page text" in resolved.text
    assert plan.omitted == []


async def test_the_guarantee_covers_the_current_turn_not_the_newest_attachment(
    monkeypatch,
):
    """The user turn that carries the request is the LAST user-role message.
    Once the conversation has moved on to a text-only turn, an oversized
    attachment of an earlier turn is history and may be stubbed; protecting
    it forever would let its fallback fail every later turn. Tool results
    and assistant turns after the user message do not move the turn."""
    _install_fake_pypdf(monkeypatch)
    moved_on = [
        _user("read this", _pdf("old.pdf", 1000)),
        ChatMessage(role="assistant", content="done"),
        _user("now a question with no attachment"),
    ]

    plan = await resolve_payload_files(moved_on, "gemini", context_window=1_000)

    assert _kinds(plan, moved_on) == [["stub"], [], []]
    assert plan.omitted == ["old.pdf"]

    same_turn = [
        _user("read this", _pdf("old.pdf", 1000)),
        ChatMessage(role="assistant", content=""),
        ChatMessage(role="tool", content="result", tool_call_id="t1", name="x"),
    ]

    plan = await resolve_payload_files(same_turn, "gemini", context_window=1_000)

    assert _kinds(plan, same_turn) == [["text"], [], []]
    assert plan.omitted == []


async def test_non_native_attachments_draw_on_the_budget_too(monkeypatch):
    """OpenAI takes no PDF natively, so every PDF is extracted text there -
    and that text is budgeted like any other fallback, or older turns' PDFs
    would pile up as 80k characters each. The stub of a file whose pages were
    never counted names its kind by extension."""
    _install_fake_pypdf(monkeypatch)
    messages = [
        _user("a", _pdf("a.pdf", 1000, salt="a")),
        _user("b", _pdf("b.pdf", 1000, salt="b")),
    ]

    plan = await resolve_payload_files(messages, "openai", context_window=10_000)

    assert _kinds(plan, messages) == [["stub"], ["text"]]
    assert plan.omitted == ["a.pdf"]
    assert plan.native_pdf_pages == 0
    stub = plan.get(0, 0)
    assert isinstance(stub, FallbackText)
    assert "(PDF, " in stub.text
    assert "attach it again" in stub.text


async def test_duplicate_stubs_are_charged_to_the_budget():
    """A stub is sent, so it is spent: enough re-attachments would otherwise
    admit text the budget has no room for. Budget exactly the two distinct
    texts - the duplicate's stub between them tips the oldest into a stub."""
    newest = _txt("x.txt", "lorem ipsum dolor sit amet " * 12)
    again = _txt("x.txt", "lorem ipsum dolor sit amet " * 12)  # same bytes
    oldest = _txt("y.txt", "consectetur adipiscing elit " * 4)
    messages = [_user("y", oldest), _user("x", again), _user("x again", newest)]

    probe = await resolve_payload_files(messages, "openai", context_window=10_000_000)
    x_tokens = _estimate_text_tokens(probe.get(2, 0).text)
    stub_tokens = _estimate_text_tokens(probe.get(1, 0).text)
    y_tokens = _estimate_text_tokens(probe.get(0, 0).text)
    assert probe.deduplicated == ["x.txt"] and probe.omitted == []

    # Room for x and y, not for the stub as well (the budget is half the window).
    plan = await resolve_payload_files(
        messages, "openai", context_window=2 * (x_tokens + y_tokens + 1)
    )
    assert plan.omitted == ["y.txt"]

    # Charge the stub in and y fits again.
    plan = await resolve_payload_files(
        messages, "openai", context_window=2 * (x_tokens + stub_tokens + y_tokens)
    )
    assert plan.omitted == []
    assert "consectetur" in plan.get(0, 0).text


async def test_no_window_means_only_the_page_cap_applies(monkeypatch):
    """A caller that does not know the model keeps the page cap and the
    dedupe; the window share needs the window."""
    _install_fake_pypdf(monkeypatch)
    messages = [_user("x", _pdf("hundred.pdf", 100))]

    plan = await resolve_payload_files(messages, "anthropic")

    assert _kinds(plan, messages) == [["native"]]


# ---------------------------------------------------------------------------
# The same bytes are sent once
# ---------------------------------------------------------------------------


async def test_a_re_attached_file_is_sent_once_in_its_newest_message(monkeypatch):
    """The production case: the same PDF uploaded three times. Two
    FileAttachment rows, two S3 objects, identical bytes - one document
    block, on the newest message; the older copies say where it is."""
    _install_fake_pypdf(monkeypatch)
    messages = [
        _user("here is the report", _pdf("report.pdf", 30)),
        ChatMessage(role="assistant", content="read it"),
        _user("again", _pdf("report.pdf", 30)),
        ChatMessage(role="assistant", content="still read it"),
        _user("and again", _pdf("report.pdf", 30)),
    ]

    plan = await resolve_payload_files(messages, "anthropic", context_window=1_000_000)

    assert _kinds(plan, messages) == [["stub"], [], ["stub"], [], ["native"]]
    assert plan.native_pdf_pages == 30
    assert plan.deduplicated == ["report.pdf", "report.pdf"]
    assert "later message" in plan.get(0, 0).text


async def test_dedupe_is_by_bytes_not_by_name(monkeypatch):
    """Two different documents that happen to share a filename are two
    documents."""
    _install_fake_pypdf(monkeypatch)
    messages = [
        _user("v1", _pdf("spec.pdf", 5, salt="v1")),
        _user("v2", _pdf("spec.pdf", 5, salt="v2")),
    ]

    plan = await resolve_payload_files(messages, "anthropic", context_window=1_000_000)

    assert _kinds(plan, messages) == [["native"], ["native"]]
    assert plan.deduplicated == []


async def test_dedupe_applies_where_pdfs_are_text_too(monkeypatch):
    """An OpenAI-compatible backend takes no PDF natively: the copies would be
    80k characters of identical text each. Once."""
    _install_fake_pypdf(monkeypatch)
    messages = [
        _user("a", _pdf("report.pdf", 20)),
        _user("b", _pdf("report.pdf", 20)),
    ]

    plan = await resolve_payload_files(messages, "openai_compat", context_window=128_000)

    assert _kinds(plan, messages) == [["stub"], ["text"]]
    assert plan.native_pdf_pages == 0


# ---------------------------------------------------------------------------
# What the budget leaves alone
# ---------------------------------------------------------------------------


async def test_images_are_native_and_outside_the_budget(monkeypatch):
    """Images are bounded by the optimizer and counted per image by the
    overflow check; the budget is about documents."""
    _install_fake_pypdf(monkeypatch)
    messages = [
        _user("shot", _png("one.png", b"one")),
        _user("shot", _png("two.png", b"two")),
        _user("doc", _pdf("doc.pdf", 4)),
    ]

    plan = await resolve_payload_files(messages, "anthropic", context_window=10)

    kinds = _kinds(plan, messages)
    assert kinds[0] == ["native"] and kinds[1] == ["native"]


async def test_native_pdfs_carry_their_page_count(monkeypatch):
    _install_fake_pypdf(monkeypatch)
    messages = [_user("x", _pdf("x.pdf", 12))]

    plan = await resolve_payload_files(messages, "anthropic")

    native = plan.get(0, 0)
    assert isinstance(native, NativeFile)
    assert native.pages == 12
    assert plan.native_pdf_bytes == len(native.data)


# ---------------------------------------------------------------------------
# The providers read the plan
# ---------------------------------------------------------------------------


async def test_anthropic_sends_one_document_block_for_a_re_attached_pdf(monkeypatch):
    _install_fake_pypdf(monkeypatch)
    provider = AnthropicProvider(api_key="test-key")
    messages = [
        _user("first", _pdf("report.pdf", 3)),
        ChatMessage(role="assistant", content="ok"),
        _user("again", _pdf("report.pdf", 3)),
    ]

    _system, conversation = await provider._build_anthropic_messages(messages)

    first_blocks = conversation[0]["content"]
    last_blocks = conversation[-1]["content"]
    assert [b["type"] for b in first_blocks] == ["text", "text"]
    assert "later message" in first_blocks[0]["text"]
    assert [b["type"] for b in last_blocks] == ["document", "text"]


async def test_anthropic_costs_native_pdfs_by_page_in_the_overflow_check(monkeypatch):
    """The byte proxy read a big PDF as a few thousand tokens. Through the
    plan the overflow check sees its pages: a 400-page PDF (~800k tokens)
    on a 1M window is over the 95% threshold once the rest is counted...
    but the budget degrades it first, so the request survives - which is
    the whole point. Pin both halves: the check's arithmetic, and that the
    prepared request never reaches it with more than the budget allows."""
    provider = AnthropicProvider(api_key="test-key")

    with pytest.raises(ContextOverflowError):
        provider._check_resolved_overflow(
            text_chars=0,
            image_count=0,
            native_file_bytes=0,
            native_document_tokens=int(provider.context_window * 0.96),
            tools=None,
            model=None,
        )

    _install_fake_pypdf(monkeypatch)
    messages = [_user("read this", _pdf("book.pdf", 400))]
    _system, conversation = await provider._prepare_request(
        messages, None, provider.default_model
    )
    # Degraded to text rather than refused: the turn goes through.
    assert [b["type"] for b in conversation[0]["content"]] == ["text", "text"]


def _re_attached_pdf_messages() -> list[ChatMessage]:
    return [
        _user("first", _pdf("report.pdf", 3)),
        ChatMessage(role="assistant", content="ok"),
        _user("again", _pdf("report.pdf", 3)),
    ]


async def test_gemini_builder_without_a_plan_builds_one(monkeypatch):
    """A direct builder call still gets the page cap and the dedupe."""
    _install_fake_pypdf(monkeypatch)
    provider = GeminiProvider(api_key="test-key")

    _system, contents = await provider._build_gemini_contents(
        _re_attached_pdf_messages()
    )

    first_parts, last_parts = contents[0].parts, contents[-1].parts
    assert all(p.text for p in first_parts)
    assert "later message" in first_parts[-1].text
    assert last_parts[-1].inline_data is not None
    assert last_parts[-1].inline_data.mime_type == "application/pdf"


async def test_bedrock_builder_without_a_plan_builds_one(monkeypatch):
    _install_fake_pypdf(monkeypatch)
    provider = BedrockProvider(api_key="AKIA||secret||us-east-1")

    _system, converse = await provider._build_converse_messages(
        _re_attached_pdf_messages()
    )

    first_blocks, last_blocks = converse[0]["content"], converse[-1]["content"]
    assert all("text" in b for b in first_blocks)
    assert "later message" in first_blocks[-1]["text"]
    assert "document" in last_blocks[-1]
    assert last_blocks[-1]["document"]["format"] == "pdf"


async def test_openai_shaped_builders_budget_older_attachments(monkeypatch):
    """The OpenAI-shaped providers pass ``context_window=`` to the shared
    builder: under a small window an older PDF's text becomes the stub, the
    current turn's stays text. Without a window only the page cap and the
    dedupe apply, so both are text."""
    _install_fake_pypdf(monkeypatch)
    messages = [
        _user("a", _pdf("a.pdf", 1000, salt="a")),
        _user("b", _pdf("b.pdf", 1000, salt="b")),
    ]

    budgeted = await build_openai_messages_async(
        messages, "openai_compat", context_window=10_000
    )
    assert "attached earlier in this conversation" in budgeted[0]["content"]
    assert "extracted page text" not in budgeted[0]["content"]
    assert "extracted page text" in budgeted[1]["content"]

    unbounded = await build_openai_messages_async(messages, "openai_compat")
    assert "extracted page text" in unbounded[0]["content"]
    assert "extracted page text" in unbounded[1]["content"]


# ---------------------------------------------------------------------------
# When the provider still says no
# ---------------------------------------------------------------------------


def test_the_pdf_page_cap_error_is_a_request_too_large_error():
    """The 600-page 400 is handled like a 413: friendly streamed message,
    REQUEST_TOO_LARGE classification, no failover to a provider that will
    refuse the same payload."""
    exc = Exception(
        "Error code: 400 - {'type': 'error', 'error': {'type': "
        "'invalid_request_error', 'message': 'A maximum of 600 PDF pages may "
        "be provided.'}}"
    )
    assert is_request_too_large_error(exc)
    assert FailoverLLMProvider._is_non_failoverable(exc)
