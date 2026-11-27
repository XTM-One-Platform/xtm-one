"""The attachment work a provider request repeats is done once.

The agentic loop sends the whole transcript on every iteration, and every
provider builder resolved each attachment again: a storage download, a
sha256, a pypdf page count, a full text extraction for a provider that takes
no native PDF, and an image validated, resized and re-encoded ON the event
loop. A conversation with a photo, a PDF and three Retina screenshots spent
~1.2 s per request on it and stalled the loop ~300 ms at a time. These tests
pin that each piece runs once for the same input, that a new image is
prepared off the event loop, and that the cached results are the results.
"""

from __future__ import annotations

import asyncio
import base64
import io
import re
import sys
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from app.core.parse_offload import IMAGE_THREAD_NAME_PREFIX, get_parse_executor
from app.llm import attachment_cache, file_resolver
from app.llm import base as llm_base
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import (
    IMAGE_DETAIL_HIGH,
    ChatMessage,
    FileRef,
    prepare_base64_image_for_llm,
    prepare_base64_image_for_llm_async,
    prepare_image_bytes_for_llm,
    prepare_image_bytes_for_llm_async,
)
from app.llm.file_resolver import (
    FallbackText,
    NativeFile,
    prepare_tool_result_attachments,
    resolve_payload_files,
)
from app.services import file_storage as file_storage_module


def _png(width: int = 64, height: int = 48, color: tuple[int, int, int] = (15, 188, 255)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


class _CountingStorage:
    """Stands in for object storage: counts reads, can fail the next ones."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.reads: list[str] = []
        self.fail_next = 0

    async def download(self, object_name: str) -> bytes:
        self.reads.append(object_name)
        if self.fail_next:
            self.fail_next -= 1
            raise OSError("storage unavailable")
        return self.objects[object_name]


@pytest.fixture
def storage(monkeypatch) -> _CountingStorage:
    fake = _CountingStorage({})
    monkeypatch.setattr(file_storage_module.file_storage, "download", fake.download)
    return fake


def _stored(storage: _CountingStorage, key: str, data: bytes, content_type: str) -> FileRef:
    storage.objects[key] = data
    return FileRef(
        file_id=f"id-{key}",
        filename=key.rsplit("/", 1)[-1],
        content_type=content_type,
        size=len(data),
        s3_object_name=key,
    )


def _install_counting_pypdf(monkeypatch) -> list[bytes]:
    parsed: list[bytes] = []

    class _Reader:
        def __init__(self, stream):
            data = stream.read()
            parsed.append(data)
            match = re.search(rb"pages=(\d+)", data)
            self.pages = [SimpleNamespace(extract_text=lambda: "page text")] * (int(match.group(1)) if match else 1)

    monkeypatch.setitem(sys.modules, "pypdf", SimpleNamespace(PdfReader=_Reader))
    return parsed


# ---------------------------------------------------------------------------
# Storage reads, digests and page counts
# ---------------------------------------------------------------------------


async def test_a_stored_attachment_is_downloaded_once_across_requests(storage):
    photo = _stored(storage, "chat/photo.png", _png(), "image/png")
    report = _stored(storage, "chat/report.pdf", b"%PDF-1.4 pages=3", "application/pdf")
    messages = [ChatMessage(role="user", content="look", files=[photo, report])]

    first = await resolve_payload_files(messages, "anthropic")
    second = await resolve_payload_files(messages, "anthropic")

    assert sorted(storage.reads) == ["chat/photo.png", "chat/report.pdf"]
    for plan in (first, second):
        assert isinstance(plan.get(0, 0), NativeFile)
        assert plan.get(0, 0).data == storage.objects["chat/photo.png"]
    assert second.native_pdf_pages == first.native_pdf_pages


async def test_a_failed_download_is_not_kept(storage):
    photo = _stored(storage, "chat/photo.png", _png(), "image/png")
    messages = [ChatMessage(role="user", content="look", files=[photo])]
    storage.fail_next = 2  # the resolver's read and the text fallback's read

    failed = await resolve_payload_files(messages, "anthropic")
    recovered = await resolve_payload_files(messages, "anthropic")

    assert not isinstance(failed.get(0, 0), NativeFile)
    assert isinstance(recovered.get(0, 0), NativeFile)
    assert storage.reads.count("chat/photo.png") == 3


async def test_a_deleted_object_is_read_again(storage, monkeypatch):
    photo = _stored(storage, "chat/photo.png", _png(), "image/png")
    messages = [ChatMessage(role="user", content="look", files=[photo])]
    removed: list[str] = []
    monkeypatch.setattr(
        file_storage_module.file_storage,
        "_client",
        SimpleNamespace(remove_object=lambda _bucket, name: removed.append(name)),
    )

    await resolve_payload_files(messages, "anthropic")
    await file_storage_module.file_storage.delete("chat/photo.png")
    await resolve_payload_files(messages, "anthropic")

    assert removed == ["chat/photo.png"]
    assert storage.reads == ["chat/photo.png", "chat/photo.png"]


async def test_the_first_request_downloads_its_attachments_concurrently(storage, monkeypatch):
    refs = [_stored(storage, f"chat/p{i}.png", _png(color=(i, i, i)), "image/png") for i in range(4)]
    in_flight = 0
    peak = 0
    original = storage.download

    async def _tracking(object_name: str) -> bytes:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.01)
            return await original(object_name)
        finally:
            in_flight -= 1

    monkeypatch.setattr(file_storage_module.file_storage, "download", _tracking)
    messages = [ChatMessage(role="user", content="look", files=refs)]

    await resolve_payload_files(messages, "anthropic")

    assert peak > 1
    assert len(storage.reads) == 4


@pytest.mark.parametrize(
    ("max_item", "max_total"),
    [(8, 1 << 20), (1 << 20, 100)],
    ids=["over-the-per-file-cap", "past-the-total"],
)
async def test_a_file_the_byte_memo_would_not_keep_is_downloaded_when_the_plan_reaches_it(
    storage, monkeypatch, max_item, max_total
):
    """A prefetched file is held until the plan reaches it, while a file the
    plan turns into text is let go once it is decided - so the prefetch takes
    no more than the byte memo keeps, and a history of large files is still
    downloaded one file at a time, never held all at once."""
    monkeypatch.setattr(attachment_cache, "STORED_BYTES_MAX_ITEM", max_item)
    monkeypatch.setattr(attachment_cache, "STORED_BYTES_MAX_TOTAL", max_total)
    refs = [_stored(storage, f"chat/notes{i}.txt", bytes([97 + i]) * 64, "text/plain") for i in range(3)]
    held = 0
    peak = 0
    original = storage.download

    async def _tracking(object_name: str) -> bytes:
        nonlocal held, peak
        held += 1
        peak = max(peak, held)
        return await original(object_name)

    async def _extract(file_ref: FileRef, data: bytes | None = None) -> FallbackText:
        nonlocal held
        held -= 1
        return FallbackText(text=f"text of {file_ref.filename}", filename=file_ref.filename)

    monkeypatch.setattr(file_storage_module.file_storage, "download", _tracking)
    monkeypatch.setattr(file_resolver, "_extract_fallback", _extract)
    messages = [ChatMessage(role="user", content="read", files=refs)]

    plan = await resolve_payload_files(messages, "openai")

    assert peak == 1
    assert [plan.get(0, i).text for i in range(3)] == [f"text of notes{i}.txt" for i in range(3)]
    assert sorted(storage.reads) == [f"chat/notes{i}.txt" for i in range(3)]


async def test_a_pdf_is_parsed_once_for_its_page_count(storage, monkeypatch):
    parsed = _install_counting_pypdf(monkeypatch)
    report = _stored(storage, "chat/report.pdf", b"%PDF-1.4 pages=12", "application/pdf")
    messages = [ChatMessage(role="user", content="read", files=[report])]

    for _ in range(3):
        plan = await resolve_payload_files(messages, "anthropic")
        assert plan.native_pdf_pages == 12

    assert len(parsed) == 1


async def test_a_pdf_degraded_to_text_is_extracted_once(storage, monkeypatch):
    """An OpenAI-compatible backend takes no native PDF: its text is extracted - once, not per request."""
    parsed = _install_counting_pypdf(monkeypatch)
    report = _stored(storage, "chat/report.pdf", b"%PDF-1.4 pages=2", "application/pdf")
    messages = [ChatMessage(role="user", content="read", files=[report])]

    texts = [(await resolve_payload_files(messages, "openai_compat")).get(0, 0).text for _ in range(3)]

    assert len(set(texts)) == 1
    assert "page text" in texts[0]
    assert len(parsed) == 1


# ---------------------------------------------------------------------------
# Image preparation
# ---------------------------------------------------------------------------


async def test_a_new_image_is_prepared_once_and_off_the_event_loop(monkeypatch):
    calls: list[str] = []
    original = llm_base._prepare_image_bytes_uncached

    def _recording(*args, **kwargs):
        calls.append(threading.current_thread().name)
        return original(*args, **kwargs)

    monkeypatch.setattr(llm_base, "_prepare_image_bytes_uncached", _recording)
    raw = _png(2600, 1200)

    first = await prepare_image_bytes_for_llm_async(raw, "image/png", detail=IMAGE_DETAIL_HIGH)
    second = await prepare_image_bytes_for_llm_async(raw, "image/png", detail=IMAGE_DETAIL_HIGH)

    assert first == second
    assert len(calls) == 1
    assert calls[0].startswith(IMAGE_THREAD_NAME_PREFIX)


async def test_an_image_is_prepared_while_every_parse_worker_is_busy():
    """A request waits on its images; a long parse must not hold them up."""
    release = threading.Event()
    parse_pool = get_parse_executor()
    busy = [parse_pool.submit(release.wait, 30) for _ in range(parse_pool._max_workers)]
    try:
        raw = _png(1800, 900)
        b64 = base64.b64encode(_png(1700, 900)).decode()
        prepared = await asyncio.wait_for(
            prepare_image_bytes_for_llm_async(raw, "image/png", detail=IMAGE_DETAIL_HIGH), timeout=10
        )
        prepared_b64 = await asyncio.wait_for(prepare_base64_image_for_llm_async(b64), timeout=10)
    finally:
        release.set()
        for future in busy:
            future.result(timeout=10)
    assert prepared is not None
    assert prepared_b64 is not None


async def test_the_cached_image_is_the_uncached_preparation():
    raw = _png(2600, 1200)
    for detail in (IMAGE_DETAIL_HIGH, "auto"):
        expected = llm_base._prepare_image_bytes_uncached(raw, "image/png", source="t", detail=detail)
        assert await prepare_image_bytes_for_llm_async(raw, "image/png", detail=detail) == expected
        assert prepare_image_bytes_for_llm(raw, "image/png", detail=detail) == expected

    b64 = base64.b64encode(raw).decode()
    expected_b64 = llm_base._prepare_base64_image_uncached(b64, source="t")
    assert await prepare_base64_image_for_llm_async(b64) == expected_b64
    assert prepare_base64_image_for_llm(b64) == expected_b64


async def test_an_unusable_image_is_remembered_as_unusable(monkeypatch):
    calls = 0
    original = llm_base._prepare_base64_image_uncached

    def _counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(llm_base, "_prepare_base64_image_uncached", _counting)
    corrupt = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64).decode()

    assert await prepare_base64_image_for_llm_async(corrupt) is None
    assert await prepare_base64_image_for_llm_async(corrupt) is None
    assert calls == 1


async def test_tool_result_screenshots_are_prepared_once_across_requests(monkeypatch):
    calls = 0
    original = llm_base._prepare_image_bytes_uncached

    def _counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(llm_base, "_prepare_image_bytes_uncached", _counting)
    shot = FileRef.from_bytes(_png(2600, 1600), "shot.png", "image/png")

    first = await prepare_tool_result_attachments([shot], "anthropic", source="t")
    second = await prepare_tool_result_attachments([shot], "anthropic", source="t")

    assert calls == 1
    assert [img.data for img in first[0]] == [img.data for img in second[0]]


async def test_repeated_anthropic_requests_build_the_same_payload(storage, monkeypatch):
    calls = 0
    original = llm_base._prepare_image_bytes_uncached

    def _counting(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(llm_base, "_prepare_image_bytes_uncached", _counting)
    photo = _stored(storage, "chat/photo.png", _png(1800, 1200, (200, 30, 30)), "image/png")
    shot = _stored(storage, "tool/shot.png", _png(2600, 1600), "image/png")
    messages = [
        ChatMessage(role="system", content="You are helpful."),
        ChatMessage(role="user", content="Compare", files=[photo]),
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[llm_base.ToolCall(id="c1", name="browser", arguments={"action": "screenshot"})],
        ),
        ChatMessage(role="tool", content="Captured.", tool_call_id="c1", name="browser", files=[shot]),
    ]
    provider = AnthropicProvider(api_key="sk-test")

    first = await provider._prepare_request(messages, None, "claude-sonnet-4-5")
    second = await provider._prepare_request(messages, None, "claude-sonnet-4-5")

    assert first == second
    assert calls == 2  # one preparation per distinct image, whatever the requests
    assert sorted(storage.reads) == ["chat/photo.png", "tool/shot.png"]


# ---------------------------------------------------------------------------
# The bounded map itself
# ---------------------------------------------------------------------------


def test_the_map_evicts_the_least_recently_used_past_its_byte_budget():
    cache = attachment_cache.BoundedLRU(max_entries=10, max_bytes=10, weigh=len)
    cache.put("a", b"12345")
    cache.put("b", b"12345")
    assert cache.get("a") == b"12345"  # "a" is now the most recent
    cache.put("c", b"12345")

    assert cache.get("b") is attachment_cache.MISSING
    assert cache.get("a") == b"12345"
    assert cache.get("c") == b"12345"
    assert cache.weight == 10


def test_the_map_never_keeps_an_item_over_its_item_cap():
    cache = attachment_cache.BoundedLRU(max_entries=10, max_bytes=100, max_item_bytes=4, weigh=len)
    cache.put("small", b"1234")
    cache.put("large", b"12345")

    assert cache.get("small") == b"1234"
    assert cache.get("large") is attachment_cache.MISSING


def test_the_map_is_bounded_by_entries():
    cache = attachment_cache.BoundedLRU(max_entries=2)
    for key in ("a", "b", "c"):
        cache.put(key, 1)

    assert len(cache) == 2
    assert cache.get("a") is attachment_cache.MISSING


def test_none_is_a_value_not_a_miss():
    cache = attachment_cache.BoundedLRU(max_entries=2)
    cache.put("unusable", None)

    assert cache.get("unusable") is None
    assert cache.get("absent") is attachment_cache.MISSING


# ---------------------------------------------------------------------------
# Plain text extraction
# ---------------------------------------------------------------------------


def test_a_plain_extraction_is_memoised_per_content(monkeypatch):
    from app.services import attachment_extract, chunking

    calls: list[bytes] = []

    def _extract(content: bytes, filename: str) -> str:
        calls.append(content)
        return f"text of {filename} ({len(content)} bytes)"

    monkeypatch.setattr(chunking, "extract_text_from_file", _extract)

    first = attachment_extract.extract_attachment_text(b"doc-a", "a.docx")
    again = attachment_extract.extract_attachment_text(b"doc-a", "a.docx")
    other = attachment_extract.extract_attachment_text(b"doc-b", "a.docx")

    assert first == again == "text of a.docx (5 bytes)"
    assert other == "text of a.docx (5 bytes)"
    assert calls == [b"doc-a", b"doc-b"]


def test_a_plain_extraction_is_keyed_on_the_extension_its_extractor_reads(monkeypatch):
    """``report.pdf `` (a trailing blank) is read as text by
    ``extract_text_from_file``, not as a PDF: the same bytes under the two
    names are two extractions, never one answered from the other's memo."""
    from app.services import attachment_extract, chunking

    _install_counting_pypdf(monkeypatch)
    data = b"%PDF-1.4 pages=1"

    as_pdf = attachment_extract.extract_attachment_text(data, "report.pdf")
    as_text = attachment_extract.extract_attachment_text(data, "report.pdf ")

    assert as_pdf == chunking.extract_text_from_file(data, "report.pdf") == "page text"
    assert as_text == chunking.extract_text_from_file(data, "report.pdf ")
    assert as_text != as_pdf
