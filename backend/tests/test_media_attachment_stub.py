"""A video or audio file a tool returns (a browser ``record`` MP4) or a user attaches is read by the model as
its label stub: its bytes are never decoded as text into the request.

Before, ``prepare_tool_result_attachments`` sent the MP4 of a 6 s recording to ``_extract_fallback``, whose plain
extraction decoded it as UTF-8: the ftyp/moov/mdat bytes, cut at 80,000 characters, in every request of the run.
"""

from __future__ import annotations

import pytest

from app.llm import file_resolver
from app.llm.base import FileRef

_MP4 = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2avc1mp41" + b"\x00\x00\x00\x08moov" + b"mdat" * 50_000


def _ref(filename: str, content_type: str) -> FileRef:
    return FileRef.from_bytes(_MP4, filename, content_type)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content_type"),
    [
        ("recording_ab12cd34.mp4", "video/mp4"),
        ("clip.webm", "video/webm"),
        ("voice.mp3", "audio/mpeg"),
        ("recording.mp4", "application/octet-stream"),
    ],
)
async def test_a_media_file_is_its_label_stub(filename, content_type):
    ref = _ref(filename, content_type)
    text = (await file_resolver._extract_fallback(ref)).text
    assert text.startswith(f"[File {ref.file_id} ({filename.rsplit('.', 1)[-1].upper()}, {len(_MP4):,} bytes)")
    assert "ftyp" not in text and "mdat" not in text
    assert len(text) < 2_000


@pytest.mark.asyncio
async def test_a_tool_result_recording_adds_no_bytes_to_the_tool_message():
    images, texts = await file_resolver.prepare_tool_result_attachments(
        [_ref("recording_ab12cd34.mp4", "video/mp4")], "anthropic", source="test"
    )
    assert images == []
    assert len(texts) == 1
    assert "mdat" not in texts[0] and len(texts[0]) < 2_000


@pytest.mark.asyncio
async def test_a_text_file_is_still_inlined():
    ref = FileRef.from_bytes(b"line one\nline two\n", "notes.txt", "text/plain")
    text = (await file_resolver._extract_fallback(ref)).text
    assert text.endswith("line one\nline two\n")
