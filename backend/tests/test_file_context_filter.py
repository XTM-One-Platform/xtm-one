"""Test FileContext basics (prompt, lookup, conversion)."""

import asyncio
from unittest.mock import AsyncMock, patch

from app.llm.base import FileRef
from app.tools.base import FileContext


def _make_ctx() -> FileContext:
    fc = FileContext()
    fc.add(
        FileRef(file_id="aaa-111", filename="report.pdf", content_type="application/pdf", size=100, s3_object_name="x")
    )
    fc.add(FileRef(file_id="bbb-222", filename="data.csv", content_type="text/csv", size=200, s3_object_name="y"))
    fc.add(FileRef(file_id="ccc-333", filename="image.png", content_type="image/png", size=300, s3_object_name="z"))
    return fc


def test_add_and_list():
    fc = _make_ctx()
    assert len(fc.list_files()) == 3


def test_get_by_id():
    fc = _make_ctx()
    assert fc.get("aaa-111") is not None
    assert fc.get("aaa-111").filename == "report.pdf"


def test_get_with_file_prefix():
    fc = _make_ctx()
    assert fc.get("file-bbb-222") is not None
    assert fc.get("file-bbb-222").file_id == "bbb-222"


def test_get_unknown():
    fc = _make_ctx()
    assert fc.get("zzz-999") is None


def test_is_empty():
    assert FileContext().is_empty
    assert not _make_ctx().is_empty


def test_from_file_refs():
    refs = [
        FileRef(file_id="a", filename="a.txt", content_type="text/plain", size=1, s3_object_name="a"),
        FileRef(file_id="b", filename="b.txt", content_type="text/plain", size=2, s3_object_name="b"),
    ]
    fc = FileContext.from_file_refs(refs)
    assert len(fc.list_files()) == 2


def test_from_file_refs_none():
    fc = FileContext.from_file_refs(None)
    assert fc.is_empty


def test_prompt_section_empty():
    assert FileContext().to_prompt_section() == ""


def test_prompt_section_lists_files():
    fc = _make_ctx()
    prompt = fc.to_prompt_section()
    assert "aaa-111" in prompt
    assert "report.pdf" in prompt
    assert "bbb-222" in prompt
    assert "data.csv" in prompt
    # Prompt must make it clear that file contents are already inlined
    # in the conversation and do NOT require a tool call to read.
    assert "ALREADY INCLUDED" in prompt
    assert "Never say you cannot read the file" in prompt


def test_prompt_section_without_current_turn_keeps_flat_listing():
    """When current_turn_file_ids is omitted, behaviour is unchanged."""
    fc = _make_ctx()
    prompt = fc.to_prompt_section()
    # No "Just attached" / "Earlier" headers when the caller does not
    # opt in to the split listing.
    assert "[Just attached" not in prompt
    assert "[Earlier in this conversation" not in prompt


def test_prompt_section_splits_current_vs_historical():
    """Current-turn files are surfaced separately from historical ones.

    This is what stops the LLM from confusing a freshly-uploaded
    screenshot with prior ones in long conversations.
    """
    fc = _make_ctx()
    prompt = fc.to_prompt_section(current_turn_file_ids={"ccc-333"})
    # Both sections must be present and clearly labelled.
    assert "[Just attached" in prompt
    assert "[Earlier in this conversation" in prompt
    # The current-turn marker must appear BEFORE the historical marker
    # so the LLM reads the most recent attachment first.
    assert prompt.index("[Just attached") < prompt.index("[Earlier in this conversation")
    # The "Just attached" section must contain the current file ONLY.
    just_attached_block = prompt.split("[Just attached", 1)[1].split("[Earlier in this conversation", 1)[0]
    assert "ccc-333" in just_attached_block
    assert "image.png" in just_attached_block
    assert "aaa-111" not in just_attached_block
    assert "bbb-222" not in just_attached_block
    # The "Earlier" section must contain the historical files.
    earlier_block = prompt.split("[Earlier in this conversation", 1)[1]
    assert "aaa-111" in earlier_block
    assert "bbb-222" in earlier_block
    # Strong wording so the LLM treats deictic expressions ("the file",
    # "this", "that") as referring to the freshly-attached file.
    assert "deictic" in prompt or "this" in prompt
    assert "primary subject" in prompt.lower() or "Just attached" in prompt


def test_prompt_section_handles_file_prefix_in_current_turn_ids():
    """``file-`` prefixed IDs (frontend convention) match plain stored IDs."""
    fc = _make_ctx()
    prompt = fc.to_prompt_section(current_turn_file_ids={"file-aaa-111"})
    assert "[Just attached" in prompt
    just_attached_block = prompt.split("[Just attached", 1)[1].split("[Earlier in this conversation", 1)[0]
    assert "aaa-111" in just_attached_block
    assert "report.pdf" in just_attached_block


def test_prompt_section_all_files_current_no_historical_section():
    """When every available file is current-turn, no Earlier section is shown."""
    fc = _make_ctx()
    prompt = fc.to_prompt_section(current_turn_file_ids={"aaa-111", "bbb-222", "ccc-333"})
    assert "[Just attached" in prompt
    assert "[Earlier in this conversation" not in prompt


def test_prompt_section_unknown_current_turn_id_falls_back_to_flat():
    """Unknown current-turn IDs degrade gracefully to the flat listing."""
    fc = _make_ctx()
    prompt = fc.to_prompt_section(current_turn_file_ids={"unknown-id"})
    # Nothing matched → no "Just attached" section is emitted, just the
    # plain listing.
    assert "[Just attached" not in prompt
    assert "[Earlier in this conversation" not in prompt
    assert "aaa-111" in prompt
    assert "bbb-222" in prompt
    assert "ccc-333" in prompt


def test_prompt_section_empty_current_turn_set_keeps_flat_listing():
    """An empty current_turn_file_ids set behaves like None."""
    fc = _make_ctx()
    prompt = fc.to_prompt_section(current_turn_file_ids=set())
    assert "[Just attached" not in prompt
    assert "[Earlier in this conversation" not in prompt


# ─── Inline FileRef paths ──────────────────────
# ``FileRef.from_bytes`` constructs an in-memory ``FileRef`` with no S3
# object — the bytes are held on the ``FileRef`` itself and must
# bypass the S3 download path entirely in both ``read_text`` and
# ``to_sandbox_files``.  These tests pin the contract so a future
# refactor that accidentally treats an inline FileRef like an
# S3-backed one (e.g. calling ``presigned_url("")``) gets caught.

def test_inline_file_ref_round_trips_through_file_context():
    """``FileRef.from_bytes`` must yield a FileRef with ``has_inline_data``."""
    ref = FileRef.from_bytes(
        b"hello world",
        filename="hello.txt",
        content_type="text/plain",
    )
    assert ref.has_inline_data
    assert ref.inline_data == b"hello world"
    assert ref.s3_object_name == ""

    fc = FileContext()
    fc.add(ref)
    fetched = fc.get(ref.file_id)
    assert fetched is not None
    assert fetched.has_inline_data
    assert fetched.inline_data == b"hello world"


def test_read_text_extracts_inline_text_without_touching_s3():
    """``read_text`` on an inline FileRef must extract directly from
    ``ref.inline_data`` and must NOT call ``file_storage.download``.
    """
    ref = FileRef.from_bytes(
        b"line one\nline two\n",
        filename="notes.txt",
        content_type="text/plain",
    )
    fc = FileContext()
    fc.add(ref)

    with patch(
        "app.services.file_storage.file_storage.download",
        new=AsyncMock(side_effect=AssertionError("S3 must not be hit for inline files")),
    ):
        text = asyncio.run(fc.read_text(ref.file_id))

    assert text is not None
    assert "line one" in text
    assert "line two" in text


def test_to_sandbox_files_inline_omits_url():
    """Inline FileRefs have no S3 object, so ``to_sandbox_files`` must
    NOT emit a ``url`` field (a presigned URL for ``""`` would 404 in
    the sandbox).  The entry should expose ``content`` (text) or
    ``data_base64`` (binary) instead.
    """
    text_ref = FileRef.from_bytes(
        b"sandbox-readable content",
        filename="readme.txt",
        content_type="text/plain",
    )
    fc = FileContext()
    fc.add(text_ref)

    entries = asyncio.run(fc.to_sandbox_files())

    assert len(entries) == 1
    entry = entries[0]
    assert "url" not in entry, (
        "Inline FileRef must not expose a presigned URL — sandboxes "
        f"cannot dereference an S3 URL for an in-memory payload: {entry}"
    )
    assert entry["filename"] == "readme.txt"
    assert entry["content"] == "sandbox-readable content"


def test_to_sandbox_files_inline_binary_uses_base64_not_url():
    """Inline binary FileRefs must expose ``data_base64`` (and no
    ``url``) so the sandbox can read them without network access.
    """
    import base64

    binary = bytes([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])  # PNG magic
    ref = FileRef.from_bytes(
        binary,
        filename="pixel.png",
        content_type="image/png",
    )
    fc = FileContext()
    fc.add(ref)

    entries = asyncio.run(fc.to_sandbox_files())

    assert len(entries) == 1
    entry = entries[0]
    assert "url" not in entry
    assert entry["data_base64"] == base64.b64encode(binary).decode("ascii")


def test_read_text_returns_none_when_s3_download_fails():
    """An S3-backed FileRef whose ``file_storage.download`` returns
    ``None`` (object missing, transient S3 failure) must surface as a
    ``read_text → None`` rather than raising, so downstream callers
    can degrade gracefully.
    """
    ref = FileRef(
        file_id="aaa-111",
        filename="report.pdf",
        content_type="application/pdf",
        size=100,
        s3_object_name="missing-object",
    )
    fc = FileContext()
    fc.add(ref)

    with patch(
        "app.services.file_storage.file_storage.download",
        new=AsyncMock(return_value=None),
    ):
        text = asyncio.run(fc.read_text(ref.file_id))

    assert text is None


def test_to_sandbox_files_s3_download_returning_none_skips_entry():
    """When ``file_storage.download`` returns ``None`` for an S3-backed
    FileRef, ``to_sandbox_files`` must skip the entry entirely rather
    than emitting a partially-populated record (the sandbox would
    otherwise read missing fields and fail in a confusing way).
    """
    ref = FileRef(
        file_id="aaa-111",
        filename="report.pdf",
        content_type="application/pdf",
        size=100,
        s3_object_name="missing-object",
    )
    fc = FileContext()
    fc.add(ref)

    with patch(
        "app.services.file_storage.file_storage.download",
        new=AsyncMock(return_value=None),
    ), patch(
        "app.services.file_storage.file_storage.presigned_url",
        new=AsyncMock(return_value="https://example.invalid/presigned"),
    ):
        entries = asyncio.run(fc.to_sandbox_files())

    assert entries == []
