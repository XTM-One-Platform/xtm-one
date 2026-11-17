"""Trace media is stored, not stripped.

The trace layer used to replace every image with the literal strings
``"[image]"`` / ``"[base64 image]"`` before writing the durable row, so the
screenshot an agent actually received — or the chart it produced — could not be
recovered from its own trace.  The durable store is the only copy, which made
that a silent, permanent loss on the audit path.

These tests pin the replacement behaviour: blobs move to object storage and the
payload keeps a reference (content type, byte size, sha256), the *external*
Langfuse export still gets placeholders because its SDK would otherwise upload
every blob to its own media storage, and the objects are cleaned up when the
trace is deleted or purged.
"""

from __future__ import annotations

import base64
import uuid

import pytest

from app.core.config import settings
from app.services import langfuse_tracing as lft
from app.services.tracing import media_offload as mo
from app.services.tracing import payload_offload as po

# A payload big enough to clear ``_MIN_MEDIA_BYTES`` (1 KiB), so the tests
# exercise the offload path rather than the keep-tiny-blobs-inline shortcut.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"pixel-data" * 200
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")
PNG_DATA_URI = f"data:image/png;base64,{PNG_B64}"


class _FakeStorage:
    """Enough of ``file_storage`` to exercise offload, fetch and cleanup.

    Object names are unique per upload, like the real implementation's uuid —
    a fake that derived the name from the filename alone would silently make
    two different images in one payload collide onto one object.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.fail = fail
        self._counter = 0

    async def upload(self, data, filename, content_type=None, prefix=""):
        if self.fail:
            raise RuntimeError("object storage unreachable")
        self._counter += 1
        name = f"{prefix}/{self._counter:04d}-{filename}"
        self.objects[name] = data
        return name

    async def download(self, object_name):
        return self.objects[object_name]

    async def delete(self, object_name):
        self.deleted.append(object_name)
        self.objects.pop(object_name, None)

    async def list_object_names(self, prefix):
        return [name for name in self.objects if name.startswith(prefix)]


@pytest.fixture
def storage(monkeypatch):
    fake = _FakeStorage()
    monkeypatch.setattr("app.services.file_storage.file_storage", fake)
    return fake


@pytest.fixture(autouse=True)
def media_enabled(monkeypatch):
    monkeypatch.setattr(settings, "trace_media_offload_enabled", True)


def _openai_payload(uri: str = PNG_DATA_URI) -> dict:
    return {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is in this screenshot?"},
                    {"type": "image_url", "image_url": {"url": uri}},
                ],
            }
        ]
    }


def _anthropic_payload(data: str = PNG_B64) -> dict:
    return {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Describe it."},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": data,
                        },
                    },
                ],
            }
        ]
    }


async def _offload(payload, *, trace_id=None, span_id=None, field="input"):
    return await mo.offload_media(
        payload,
        trace_id=trace_id or uuid.uuid4(),
        span_id=span_id,
        field=field,
    )


# ---------------------------------------------------------------------------
# The blob reaches object storage and the payload keeps a reference
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_openai_data_uri_becomes_a_reference(storage):
    result = await _offload(_openai_payload())

    part = result["messages"][0]["content"][1]
    reference = part["image_url"]["url"]
    assert mo.is_media_reference(reference)
    assert reference["content_type"] == "image/png"
    assert reference["size_bytes"] == len(PNG_BYTES)
    assert storage.objects[reference["object_name"]] == PNG_BYTES


@pytest.mark.asyncio
async def test_anthropic_source_block_becomes_a_reference(storage):
    """Bare base64 under ``source.data`` carries no ``data:`` prefix."""
    result = await _offload(_anthropic_payload())

    source = result["messages"][0]["content"][1]["source"]
    assert mo.is_media_reference(source["data"])
    assert storage.objects[source["data"]["object_name"]] == PNG_BYTES
    # The block's own descriptors stay put, so the shape is still recognisable.
    assert source["type"] == "base64"
    assert source["media_type"] == "image/png"


@pytest.mark.asyncio
async def test_the_reference_is_addressable_and_round_trips(storage):
    result = await _offload(_openai_payload())
    reference = result["messages"][0]["content"][1]["image_url"]["url"]

    found = mo.find_media_reference(result, reference["sha256"])
    assert found == reference

    raw, content_type = await mo.resolve_media(found)
    assert raw == PNG_BYTES
    assert content_type == "image/png"


@pytest.mark.asyncio
async def test_surrounding_text_is_untouched(storage):
    result = await _offload(_openai_payload())

    assert result["messages"][0]["content"][0] == {
        "type": "text",
        "text": "What is in this screenshot?",
    }
    assert result["messages"][0]["role"] == "user"


@pytest.mark.asyncio
async def test_pdf_documents_are_offloaded_too(storage):
    pdf = b"%PDF-1.7" + b"\x00stream" * 300
    payload = {
        "content": [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.b64encode(pdf).decode("ascii"),
                },
            }
        ]
    }

    result = await _offload(payload)

    reference = result["content"][0]["source"]["data"]
    assert reference["content_type"] == "application/pdf"
    assert storage.objects[reference["object_name"]] == pdf


@pytest.mark.asyncio
async def test_repeated_blobs_share_one_object(storage):
    """The same screenshot appears in several spans of one trace."""
    payload = {"a": PNG_DATA_URI, "b": PNG_DATA_URI, "c": PNG_DATA_URI}

    result = await _offload(payload)

    assert len(storage.objects) == 1
    assert result["a"]["sha256"] == result["c"]["sha256"]
    assert result["a"]["object_name"] == result["c"]["object_name"]


@pytest.mark.asyncio
async def test_distinct_blobs_get_distinct_objects(storage):
    other = base64.b64encode(b"different" * 300).decode("ascii")
    payload = {"a": PNG_DATA_URI, "b": f"data:image/webp;base64,{other}"}

    result = await _offload(payload)

    assert len(storage.objects) == 2
    assert result["a"]["object_name"] != result["b"]["object_name"]
    assert result["b"]["content_type"] == "image/webp"


# ---------------------------------------------------------------------------
# What must NOT be touched
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_urls_are_left_alone(storage):
    """Previously replaced by "[image]", which was itself a fidelity loss.

    Keeping the URL costs nothing, and this module never fetches one — an
    outbound request driven by traced content would be an SSRF vector.
    """
    payload = _openai_payload("https://example.test/shot.png")

    result = await _offload(payload)

    assert result["messages"][0]["content"][1]["image_url"]["url"] == ("https://example.test/shot.png")
    assert storage.objects == {}


@pytest.mark.asyncio
async def test_tiny_blobs_stay_inline(storage):
    """An inline icon costs less in the row than an object plus a round-trip."""
    tiny = base64.b64encode(b"icon").decode("ascii")
    payload = {"icon": f"data:image/png;base64,{tiny}"}

    result = await _offload(payload)

    assert result == payload
    assert storage.objects == {}


@pytest.mark.asyncio
async def test_short_base64_shaped_strings_are_not_mistaken_for_media(storage):
    """A tool_call_id or token can be base64-shaped without being an image."""
    payload = {"source": {"type": "base64", "media_type": "image/png", "data": "YWJj"}}

    result = await _offload(payload)

    assert result == payload
    assert storage.objects == {}


@pytest.mark.asyncio
async def test_plain_text_payloads_are_unchanged(storage):
    payload = {"content": "no media here", "reasoning": "step " * 5_000}

    result = await _offload(payload)

    assert result == payload
    assert storage.objects == {}


@pytest.mark.asyncio
async def test_an_existing_reference_is_not_re_offloaded(storage):
    once = await _offload(_openai_payload())
    uploads_after_first = len(storage.objects)

    twice = await _offload(once)

    assert len(storage.objects) == uploads_after_first
    assert twice == once


@pytest.mark.asyncio
async def test_disabled_setting_falls_back_to_leaving_the_payload_alone(storage, monkeypatch):
    monkeypatch.setattr(settings, "trace_media_offload_enabled", False)
    payload = _openai_payload()

    result = await _offload(payload)

    assert result is payload
    assert storage.objects == {}


# ---------------------------------------------------------------------------
# Failure is recorded, never silent, and never fatal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_bucket_failure_records_the_gap_instead_of_losing_it_silently(
    monkeypatch,
):
    monkeypatch.setattr("app.services.file_storage.file_storage", _FakeStorage(fail=True))

    result = await _offload(_openai_payload())

    reference = result["messages"][0]["content"][1]["image_url"]["url"]
    assert reference["offload_failed"] is True
    assert reference["error"]
    assert "object_name" not in reference
    # The descriptors survive, so the UI can say what was lost and how big.
    assert reference["content_type"] == "image/png"
    assert reference["size_bytes"] == len(PNG_BYTES)
    assert reference["sha256"]


@pytest.mark.asyncio
async def test_resolve_media_returns_none_for_a_failed_reference():
    assert await mo.resolve_media({mo.MEDIA_KEY: 1, "offload_failed": True}) is None


@pytest.mark.asyncio
async def test_resolve_media_ignores_non_references():
    assert await mo.resolve_media({"content": "text"}) is None
    assert await mo.resolve_media(None) is None


@pytest.mark.asyncio
async def test_undecodable_data_uri_is_left_as_text(storage):
    payload = {"broken": "data:image/png;base64,!!!not-base64!!!"}

    result = await _offload(payload)

    assert result == payload
    assert storage.objects == {}


# ---------------------------------------------------------------------------
# The durable path keeps media; the Langfuse export still strips it
# ---------------------------------------------------------------------------


def test_to_jsonable_keeps_media_for_the_durable_store():
    """The regression this whole change exists to fix."""
    stored = lft._to_jsonable(_openai_payload())

    url = stored["messages"][0]["content"][1]["image_url"]["url"]
    assert url == PNG_DATA_URI
    assert "[image]" not in str(stored)
    assert "[base64 image]" not in str(stored)


def test_to_jsonable_keeps_anthropic_media_for_the_durable_store():
    stored = lft._to_jsonable(_anthropic_payload())

    assert stored["messages"][0]["content"][1]["source"]["data"] == PNG_B64


def test_bound_for_langfuse_still_strips_media():
    """Its SDK would otherwise upload every blob to its own media storage."""
    bounded = lft._bound_for_langfuse(_openai_payload())

    part = bounded["messages"][0]["content"][1]
    assert part == {"type": "text", "text": "[image]"}
    assert PNG_B64 not in str(bounded)


def test_bound_for_langfuse_strips_bare_base64_data_uris():
    bounded = lft._bound_for_langfuse({"url": PNG_DATA_URI})

    assert bounded["url"] == "[base64 image]"


def test_sanitize_for_storage_keeps_media_but_drops_null_bytes():
    """Null bytes are a hard JSONB constraint; media is not."""
    sanitized = lft._sanitize_for_storage({"url": PNG_DATA_URI, "text": "before\x00after"})

    assert sanitized["url"] == PNG_DATA_URI
    assert "\x00" not in sanitized["text"]


# ---------------------------------------------------------------------------
# Interaction with payload offload
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extracting_media_can_keep_the_payload_inline(storage, monkeypatch):
    """Media is usually what makes a payload oversized in the first place."""
    monkeypatch.setattr(settings, "trace_payload_offload_threshold_bytes", 1_024)
    trace_id = uuid.uuid4()
    payload = _openai_payload()
    assert po.payload_size_bytes(payload) > 1_024, "precondition: oversized as-is"

    after_media = await _offload(payload, trace_id=trace_id)
    after_payload = await po.offload_if_oversized(after_media, trace_id=trace_id, span_id=None, field="input")

    assert not po.is_offload_envelope(after_payload), "should now fit inline"
    assert mo.is_media_reference(after_payload["messages"][0]["content"][1]["image_url"]["url"])


@pytest.mark.asyncio
async def test_references_survive_a_payload_offload_round_trip(storage, monkeypatch):
    """A still-oversized payload keeps its media individually addressable."""
    monkeypatch.setattr(settings, "trace_payload_offload_threshold_bytes", 1_024)
    trace_id = uuid.uuid4()
    payload = _openai_payload()
    payload["filler"] = "x" * 5_000

    after_media = await _offload(payload, trace_id=trace_id)
    envelope = await po.offload_if_oversized(after_media, trace_id=trace_id, span_id=None, field="input")
    assert po.is_offload_envelope(envelope), "precondition: still oversized"

    restored = await po.resolve_payload(envelope)
    reference = restored["messages"][0]["content"][1]["image_url"]["url"]
    assert mo.is_media_reference(reference)
    raw, _ = await mo.resolve_media(reference)
    assert raw == PNG_BYTES


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def test_collect_media_object_names_finds_deeply_nested_references(storage):
    payload = {
        "messages": [
            {"content": [{"image_url": {"url": {mo.MEDIA_KEY: 1, "object_name": "a"}}}]},
            {"content": [{"source": {"data": {mo.MEDIA_KEY: 1, "object_name": "b"}}}]},
        ]
    }

    assert sorted(mo.collect_media_object_names(payload)) == ["a", "b"]


def test_collect_media_object_names_deduplicates_and_skips_failures():
    payload = {
        "a": {mo.MEDIA_KEY: 1, "object_name": "same"},
        "b": {mo.MEDIA_KEY: 1, "object_name": "same"},
        "c": {mo.MEDIA_KEY: 1, "offload_failed": True},
        "d": {"not": "a reference"},
    }

    assert mo.collect_media_object_names(payload) == ["same"]


@pytest.mark.asyncio
async def test_delete_trace_objects_sweeps_payloads_and_media(storage, monkeypatch):
    """One prefix owns everything a trace stored, media included."""
    monkeypatch.setattr(settings, "trace_payload_offload_threshold_bytes", 1_024)
    trace_id = uuid.uuid4()
    other_trace = uuid.uuid4()

    await _offload(_openai_payload(), trace_id=trace_id)
    await po.offload_if_oversized({"filler": "x" * 5_000}, trace_id=trace_id, span_id=None, field="output")
    await _offload(_openai_payload(), trace_id=other_trace)
    assert len(storage.objects) == 3

    deleted = await po.delete_trace_objects([trace_id])

    assert deleted == 2
    remaining = list(storage.objects)
    assert len(remaining) == 1
    assert str(other_trace) in remaining[0]


@pytest.mark.asyncio
async def test_delete_trace_objects_is_a_noop_without_traces(storage):
    assert await po.delete_trace_objects([]) == 0
    assert storage.deleted == []


@pytest.mark.asyncio
async def test_delete_trace_objects_survives_a_listing_failure(monkeypatch):
    class _FailingList(_FakeStorage):
        async def list_object_names(self, prefix):
            raise RuntimeError("bucket unreachable")

    monkeypatch.setattr("app.services.file_storage.file_storage", _FailingList())

    # Best-effort: a storage problem must never block the row deletion it
    # accompanies, so this reports zero rather than raising.
    assert await po.delete_trace_objects([uuid.uuid4()]) == 0


@pytest.mark.asyncio
async def test_list_object_names_returns_empty_on_any_storage_error(monkeypatch):
    """Its contract is "empty list, never raise" — for every failure, not just S3Error.

    A misconfigured endpoint raises something else entirely, and this feeds a
    retention sweep that runs alongside a row deletion which has to proceed.
    """
    from app.services.file_storage import file_storage

    class _Boom:
        def list_objects(self, *_args, **_kwargs):
            raise RuntimeError("endpoint misconfigured")

    monkeypatch.setattr(type(file_storage), "client", property(lambda _self: _Boom()))

    assert await file_storage.list_object_names("traces/whatever/") == []


# ---------------------------------------------------------------------------
# The dispatcher applies it before anything reaches a sink
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatcher_offloads_media_before_the_queue(storage, monkeypatch):
    """Every sink must carry the reference, not the blob.

    The offload runs in ``record()`` rather than per-sink, so the queue, the
    Redis overflow buffer and the ClickHouse insert batch all see the small
    payload.
    """
    from datetime import UTC, datetime

    from app.services.tracing.base import SpanData, TraceData
    from app.services.tracing.dispatcher import TracingDispatcher

    monkeypatch.setattr(settings, "trace_payload_offload_threshold_bytes", 1_000_000)
    trace_id = uuid.uuid4()
    now = datetime.now(UTC)
    trace = TraceData(id=trace_id, source="test", name="t", started_at=now)
    span = SpanData(
        id=uuid.uuid4(),
        trace_id=trace_id,
        type="GENERATION",
        name="llm",
        position=0,
        started_at=now,
        input=_openai_payload(),
    )

    dispatcher = TracingDispatcher(sinks=[])
    dispatcher._running = True
    try:
        await dispatcher.record(trace, [span])
    finally:
        dispatcher._running = False

    _, queued_spans = dispatcher._queue.get_nowait()
    reference = queued_spans[0].input["messages"][0]["content"][1]["image_url"]["url"]
    assert mo.is_media_reference(reference)
    assert storage.objects[reference["object_name"]] == PNG_BYTES
    # Keyed under the span that carried it, so retention can find it by prefix.
    assert reference["object_name"].startswith(f"traces/{trace_id}/")


@pytest.mark.asyncio
async def test_a_payload_without_media_is_passed_through_unchanged(storage):
    """Identity, not just equality: rebuilding every payload would be pure waste.

    Media-free payloads are the overwhelming majority and they all run through
    the walker on every trace.
    """
    payload = {"messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]}

    result = await _offload(payload)

    assert result is payload
    assert result["messages"] is payload["messages"]


def test_media_objects_live_under_the_trace_prefix():
    """Retention sweeps ``traces/<trace_id>/``, so media must nest under it."""
    trace_id = uuid.uuid4()
    span_id = uuid.uuid4()

    media = mo.media_object_prefix(trace_id, span_id)
    payload = po.offload_object_prefix(trace_id, span_id)

    assert media.startswith(po.trace_object_prefix(trace_id))
    assert payload.startswith(po.trace_object_prefix(trace_id))
