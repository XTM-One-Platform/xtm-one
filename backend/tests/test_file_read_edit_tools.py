"""``read_file_content`` / ``edit_file_content`` - the agent's own files.

A file a tool wrote (``generate_file``, the code interpreter, a download)
hangs off an ASSISTANT message, which no provider inlines: its content is
not in the model's context, only its id is.  The platform once
told the model the content was "ALREADY INCLUDED" and offered no tool to read
it back or change it, so "remove one sentence from the HTML you just
generated" sent the model through the sandbox and its own traces until its
iteration budget ran out.

These tests pin the two tools that close the gap and the prompt that points
at them:

* ``read_file_content`` serves the stored bytes verbatim (windowed), the
  extracted text of a rendered document, or the image itself;
* ``edit_file_content`` applies exact find/replace operations SERVER-SIDE and
  saves a NEW revision - nothing is written unless every edit applies, an
  absent or ambiguous snippet is a refusal the model can act on, and a
  rendered PDF/DOCX is refused with the way out;
* ``FileContext.to_prompt_section`` tells the truth per file origin.
"""

from __future__ import annotations

import ast
import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.llm.base import FileRef
from app.services.builtin_tools import build_generate_file_tool, media
from app.tools.base import FileContext, ToolCall

HTML = (
    "<html><body>\n"
    "<h1>Briefing</h1>\n"
    "<p>JP Salles still shows as declined.</p>\n"
    "<p>Sean has not accepted.</p>\n"
    "<p>Steve rotates off this week.</p>\n"
    "</body></html>\n"
)


def _generated_ref(
    data: bytes,
    filename: str = "briefing.html",
    content_type: str = "text/html",
    source: str = "agent_download",
) -> FileRef:
    ref = FileRef.from_bytes(data, filename, content_type)
    ref.source = source
    return ref


def _build(monkeypatch, persisted: list[dict]):
    """Build the tool trio with the persistence seam captured in memory."""

    async def _fake_persist(**kwargs):
        persisted.append(kwargs)
        return SimpleNamespace(
            id=uuid.uuid4(),
            filename=kwargs["filename"],
            content_type=kwargs["content_type"],
            size=len(kwargs["raw_bytes"]),
            s3_object_name=f"chat/conv/{kwargs['filename']}",
            source=kwargs["source"],
        )

    monkeypatch.setattr(
        "app.services.builtin_tools._persist_generated_file", _fake_persist
    )
    defs, executor = build_generate_file_tool(
        db=None,
        user_id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
    )
    return defs, executor


async def _call(executor, name: str, **arguments):
    return await executor.execute_tool(
        ToolCall(id=f"call-{name}", name=name, arguments=arguments)
    )


# ── Pure helpers ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("filename", "content_type", "expected"),
    [
        ("briefing.html", "text/html", True),
        ("data.csv", "text/csv", True),
        ("bundle.json", "application/json", True),
        ("rules.yar", "application/octet-stream", True),
        ("notes.md", "application/octet-stream", True),
        ("Report.HTML", "APPLICATION/OCTET-STREAM", True),
        ("report.pdf", "application/pdf", False),
        (
            "report.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            False,
        ),
        ("chart.png", "image/png", False),
        ("archive.zip", "application/zip", False),
    ],
)
def test_is_text_like_file(filename, content_type, expected):
    assert media.is_text_like_file(filename, content_type) is expected


def test_is_text_like_file_svg_by_mime_and_by_extension():
    """The MIME list and the extension list are two lists; pin that an SVG
    is text-like through EITHER, so one of them drifting does not turn a
    ``.svg`` served as ``application/octet-stream`` into a picture."""
    assert media.is_text_like_file("x.svg", "image/svg+xml") is True
    assert media.is_text_like_file("x.svg", "application/octet-stream") is True


def test_decode_file_text_handles_bom_and_binary():
    assert media.decode_file_text(b"\xef\xbb\xbfhello") == "hello"
    assert media.decode_file_text(b"plain") == "plain"
    assert media.decode_file_text(b"PK\x03\x04\x00\x00binary") is None
    # UTF-16 interleaves NUL with its ASCII range: binary to the tripwire,
    # so it reads through extraction and is never edited in place.
    assert media.decode_file_text("hello".encode("utf-16")) is None


def test_decode_file_text_refuses_non_utf8_instead_of_replacing():
    """Strict UTF-8: a Windows-1252 byte is a refusal, never U+FFFD.

    The decoded text is what ``edit_file_content`` re-encodes, so a lossy
    decode here would silently rewrite every non-ASCII byte outside the
    edited region in the saved revision.
    """
    assert media.decode_file_text("café".encode("cp1252")) is None
    assert media.decode_file_text("café".encode()) == "café"


def test_apply_text_edits_deletes_a_sentence_and_accounts_for_it():
    new_text, account = media.apply_text_edits(
        HTML,
        [{"find": "<p>JP Salles still shows as declined.</p>\n", "replace": ""}],
    )
    assert "JP Salles" not in new_text
    assert "Sean has not accepted." in new_text
    assert len(account) == 1
    assert "deleted 1 occurrence" in account[0]


def test_apply_text_edits_is_sequential():
    text = "a b c"
    new_text, account = media.apply_text_edits(
        text,
        [{"find": "a", "replace": "x"}, {"find": "x b", "replace": "y"}],
    )
    assert new_text == "y c"
    assert len(account) == 2


def test_apply_text_edits_refuses_absent_snippet_without_partial_write():
    with pytest.raises(media.FileEditError) as exc:
        media.apply_text_edits(
            HTML,
            [
                {"find": "<h1>Briefing</h1>", "replace": "<h1>Brief</h1>"},
                {"find": "not in the document", "replace": ""},
            ],
        )
    message = str(exc.value)
    assert "edit 2" in message
    assert "was not found" in message
    assert "Nothing was changed" in message
    assert "read_file_content" in message


def test_apply_text_edits_refuses_ambiguous_snippet_unless_replace_all():
    with pytest.raises(media.FileEditError) as exc:
        media.apply_text_edits(HTML, [{"find": "<p>", "replace": "<p class=x>"}])
    assert "occurs 3 times" in str(exc.value)
    assert "replace_all" in str(exc.value)

    new_text, account = media.apply_text_edits(
        HTML, [{"find": "<p>", "replace": "<p class=x>", "replace_all": True}]
    )
    assert new_text.count("<p class=x>") == 3
    assert "replaced 3 occurrences" in account[0]


def test_apply_text_edits_validates_the_operation_shape():
    with pytest.raises(media.FileEditError):
        media.apply_text_edits(HTML, [])
    with pytest.raises(media.FileEditError):
        media.apply_text_edits(HTML, "not a list")
    with pytest.raises(media.FileEditError):
        media.apply_text_edits(HTML, [{"find": "", "replace": "x"}])
    with pytest.raises(media.FileEditError):
        media.apply_text_edits(HTML, [{"find": "<p>", "replace": 3}])
    with pytest.raises(media.FileEditError) as exc:
        media.apply_text_edits(
            HTML,
            [{"find": "<p>", "replace": ""}] * (media.EDIT_MAX_OPERATIONS + 1),
        )
    assert "Too many operations" in str(exc.value)


def test_edit_refusal_snippet_is_bounded():
    huge = "x" * 5_000
    with pytest.raises(media.FileEditError) as exc:
        media.apply_text_edits(HTML, [{"find": huge, "replace": ""}])
    assert len(str(exc.value)) < 600


# ── Tool surface ─────────────────────────────────────────────────────────


def test_generate_file_suite_carries_read_and_edit(monkeypatch):
    defs, _executor = _build(monkeypatch, [])
    names = [d.name for d in defs]
    assert names == [
        "generate_file",
        "read_file_content",
        "query_file_json",
        "edit_file_content",
        "annotate_file",
        "render_html_to_image",
    ]
    by_name = {d.name: d for d in defs}
    assert by_name["read_file_content"].input_schema["required"] == ["file_id"]
    # The structured read is a mode of the read tool, declared as an
    # enum so a provider validates it; the JSON query is its own tool.
    assert by_name["read_file_content"].input_schema["properties"]["mode"]["enum"] == [
        "source",
        "extract_structured",
    ]
    assert by_name["query_file_json"].input_schema["required"] == ["file_id"]
    assert by_name["edit_file_content"].input_schema["required"] == [
        "file_id",
        "edits",
    ]
    # Every tool of the suite shares the built-in id the catalog keys on.
    assert {d.source for d in defs} == {"builtin:files"}
    # generate_file points at the edit tool for small changes instead of
    # inviting a wholesale regeneration.
    assert "edit_file_content" in by_name["generate_file"].description


@pytest.mark.asyncio
async def test_read_file_content_serves_generated_text_verbatim(monkeypatch):
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    ref = _generated_ref(HTML.encode("utf-8"))
    ctx.add(ref)
    executor.file_context = ctx

    result = await _call(executor, "read_file_content", file_id=ref.file_id)

    assert result.is_error is False
    assert "briefing.html" in result.content
    assert "editable source text" in result.content
    assert f"Content (complete, {len(HTML)} characters):" in result.content
    assert HTML in result.content


@pytest.mark.asyncio
async def test_read_file_content_accepts_a_unique_filename(monkeypatch):
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    ctx.add(_generated_ref(HTML.encode("utf-8")))
    executor.file_context = ctx

    result = await _call(executor, "read_file_content", file_id="briefing.html")
    assert result.is_error is False
    assert "<h1>Briefing</h1>" in result.content


@pytest.mark.asyncio
async def test_read_file_content_accepts_the_marker_the_model_copies(monkeypatch):
    """``[[FILE:<id>]]`` is what the model sees in the previous tool result.

    The schemas advertise the marker, so pasting it back must resolve - as
    must the ``file-<id>`` and ``file:<uuid>`` spellings and stray blanks.
    """
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    ref = _generated_ref(HTML.encode("utf-8"))
    ctx.add(ref)
    executor.file_context = ctx

    for spelling in (
        f"[[FILE:{ref.file_id}]]",
        f"  [[FILE:{ref.file_id}]]\n",
        f"file-{ref.file_id}",
        f"file:{ref.file_id}",
    ):
        result = await _call(executor, "read_file_content", file_id=spelling)
        assert result.is_error is False, (spelling, result.content)
        assert "<h1>Briefing</h1>" in result.content

    assert media._resolve_file_ref(ctx, None) is None
    assert media._resolve_file_ref(ctx, 12) is None
    assert media._resolve_file_ref(None, ref.file_id) is None


@pytest.mark.asyncio
async def test_read_file_content_serves_an_empty_editable_file(monkeypatch):
    """A zero-byte text file is a file with nothing in it, not a missing one."""
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    ref = _generated_ref(b"", filename="empty.txt", content_type="text/plain")
    ctx.add(ref)
    executor.file_context = ctx

    result = await _call(executor, "read_file_content", file_id=ref.file_id)
    assert result.is_error is False, result.content
    assert "Content (complete, 0 characters):" in result.content
    assert "editable source text" in result.content


@pytest.mark.asyncio
async def test_a_file_with_nothing_to_read_answers_with_its_notes(monkeypatch):
    """A video has no text and is no picture: its notes say what it holds, never a bare refusal."""
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    ref = FileRef.from_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64, "scene-preview.mp4", "video/mp4")
    ref.notes = 'Preview of scene s2 of the video "Brand film": 4.2 s at 1920x1080.'
    ctx.add(ref)
    executor.file_context = ctx

    async def _nothing_extracted(self, file_id):
        return ""

    monkeypatch.setattr(FileContext, "read_text", _nothing_extracted)

    result = await _call(executor, "read_file_content", file_id=ref.file_id)
    assert result.is_error is False, result.content
    assert "no readable text content" in result.content
    assert 'Your notes: Preview of scene s2 of the video "Brand film"' in result.content
    # Without notes it is still the error it was.
    ref.notes = None
    bare = await _call(executor, "read_file_content", file_id=ref.file_id)
    assert bare.is_error is True and "Your notes" not in bare.content


@pytest.mark.asyncio
async def test_read_file_content_names_a_storage_failure(monkeypatch):
    """Bytes that cannot be loaded are a storage problem, not "no text"."""
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    ref = FileRef(
        file_id="gone-1",
        filename="briefing.html",
        content_type="text/html",
        size=10,
        s3_object_name="chat/conv/briefing.html",
        source="agent_download",
    )
    ctx.add(ref)
    executor.file_context = ctx

    async def _missing(self, file_id):
        return None

    monkeypatch.setattr(FileContext, "read_bytes", _missing)
    result = await _call(executor, "read_file_content", file_id=ref.file_id)
    assert result.is_error is True
    assert "could not be loaded from storage" in result.content
    assert "no readable text content" not in result.content

    async def _boom(self, file_id):
        raise OSError("s3 down")

    monkeypatch.setattr(FileContext, "read_bytes", _boom)
    result = await _call(executor, "read_file_content", file_id=ref.file_id)
    assert result.is_error is True
    assert "could not be loaded from storage" in result.content


@pytest.mark.asyncio
async def test_read_file_content_windows_large_files(monkeypatch):
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    text = "".join(f"line {i:05d}\n" for i in range(2_000))
    ref = _generated_ref(text.encode("utf-8"), filename="big.txt", content_type="text/plain")
    ctx.add(ref)
    executor.file_context = ctx

    first = await _call(executor, "read_file_content", file_id=ref.file_id, max_chars=1_000)
    assert first.is_error is False
    assert f"characters 0-1000 of {len(text)}" in first.content
    assert "offset=1000" in first.content
    assert text[:1_000] in first.content
    assert text[1_000:1_100] not in first.content

    second = await _call(
        executor, "read_file_content", file_id=ref.file_id, offset=1_000, max_chars=1_000
    )
    assert f"characters 1000-2000 of {len(text)}" in second.content
    assert text[1_000:2_000] in second.content

    past = await _call(executor, "read_file_content", file_id=ref.file_id, offset=len(text) + 5)
    assert "past the end" in past.content

    # The window is capped so one read cannot flood the context.
    capped = await _call(
        executor,
        "read_file_content",
        file_id=ref.file_id,
        max_chars=media.READ_WINDOW_MAX_CHARS * 10,
    )
    assert capped.is_error is False
    assert f"Content (complete, {len(text)} characters):" in capped.content


# ── The per-file window cap ───────────────────────────────────────


def _big_text_ref(filename: str = "big.txt", content_type: str = "text/plain") -> tuple[str, FileRef]:
    text = "".join(f"line {i:05d}\n" for i in range(2_000))
    return text, _generated_ref(text.encode("utf-8"), filename=filename, content_type=content_type)


def _cap(monkeypatch, value: int) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "agent_loop_read_windows_per_file", value)


@pytest.mark.asyncio
async def test_the_windows_of_one_file_are_capped_per_turn(monkeypatch):
    """A turn paged one 558k attachment 50k at a time for 97
    iterations, each window pushing the previous one out of the context.
    Past ``AGENT_LOOP_READ_WINDOWS_PER_FILE`` windows of one file the answer is
    an explicit tool error naming what was read and the two ways forward."""
    from app.services.builtin_tools import build_code_interpreter_tool
    from app.services.code_interpreter import mount_path_hint
    from app.tools.base import offering_tools

    _cap(monkeypatch, 3)
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    _text, ref = _big_text_ref()
    ctx.add(ref)
    executor.file_context = ctx
    mount = mount_path_hint(ref.filename, ref.file_id)

    # A turn that offers the code interpreter (nothing else names it).
    with offering_tools(build_code_interpreter_tool()[0]):
        first = await _call(executor, "read_file_content", file_id=ref.file_id, max_chars=1_000)
        second = await _call(executor, "read_file_content", file_id=ref.file_id, offset=1_000, max_chars=1_000)
        third = await _call(executor, "read_file_content", file_id=ref.file_id, offset=2_000, max_chars=1_000)
        fourth = await _call(executor, "read_file_content", file_id=ref.file_id, offset=3_000, max_chars=1_000)
    assert f'read_file_content(file_id="{ref.file_id}", offset=1000) (2 more window(s) this turn)' in first.content
    assert "Do not page through the whole file" in first.content
    assert "(1 more window(s) this turn)" in second.content
    assert "this was the last window this turn allows for this file" in third.content
    assert f"(mounted at {mount})" in third.content
    assert fourth.is_error is True
    assert fourth.content.startswith("Error: window budget for this file exhausted this turn, 3 of 3 served ('big.txt', ")
    assert f"The file is still attached (file_id={ref.file_id})" in fourth.content
    assert "no file" not in fourth.content
    assert "Answer from what you have and say what you could not read" in fourth.content
    assert f"use code_interpreter to process the whole file (every row, counts, sums), where it is mounted at {mount}." in fourth.content
    assert "mode=" not in fourth.content, "a plain text file has no rendered view to offer"
    assert ctx.read_window_count(ref.file_id, "source") == 3

    # Another file has its own allowance.
    _other_text, other = _big_text_ref("other.txt")
    ctx.add(other)
    assert (await _call(executor, "read_file_content", file_id=other.file_id, max_chars=10)).is_error is False


@pytest.mark.asyncio
async def test_a_read_that_serves_no_window_does_not_spend_one(monkeypatch):
    _cap(monkeypatch, 2)
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    text, ref = _big_text_ref()
    ctx.add(ref)
    executor.file_context = ctx
    for _ in range(3):
        past = await _call(executor, "read_file_content", file_id=ref.file_id, offset=len(text) + 5)
        assert "past the end" in past.content
    assert ctx.read_window_count(ref.file_id, "source") == 0

    original = FileContext.read_bytes

    async def _missing(self, file_id):
        return None

    monkeypatch.setattr(FileContext, "read_bytes", _missing)
    missing = await _call(executor, "read_file_content", file_id=ref.file_id)
    assert missing.is_error is True and "could not be loaded" in missing.content
    monkeypatch.setattr(FileContext, "read_bytes", original)
    assert ctx.read_window_count(ref.file_id, "source") == 0
    for offset in (0, 1_000):
        served = await _call(executor, "read_file_content", file_id=ref.file_id, offset=offset, max_chars=1_000)
        assert served.is_error is False


@pytest.mark.asyncio
async def test_the_rendered_view_is_counted_apart_and_the_refusal_offers_it(monkeypatch):
    """The cap is keyed on the EFFECTIVE view: the rendered text of an HTML
    file is another read than its source, while asking for the rendered view
    of a plain text file reads - and counts as - its source."""
    _cap(monkeypatch, 1)
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    page = "<html><body>" + "".join(f"<p>Paragraph {i} of the plan.</p>" for i in range(3_000)) + "</body></html>"
    html = _generated_ref(page.encode(), filename="plan.html", content_type="text/html")
    _text, plain = _big_text_ref()
    for ref in (html, plain):
        ctx.add(ref)
    executor.file_context = ctx

    assert (await _call(executor, "read_file_content", file_id=html.file_id, max_chars=1_000)).is_error is False
    refused = await _call(executor, "read_file_content", file_id=html.file_id, offset=1_000, max_chars=1_000)
    assert refused.is_error is True
    assert 'read it with mode="extract_structured"' in refused.content
    rendered = await _call(executor, "read_file_content", file_id=html.file_id, mode="extract_structured")
    assert rendered.is_error is False and "rendered for reading" in rendered.content
    assert ctx.read_window_count(html.file_id, "extract_structured") == 1

    assert (await _call(executor, "read_file_content", file_id=plain.file_id, max_chars=10)).is_error is False
    same_view = await _call(executor, "read_file_content", file_id=plain.file_id, mode="extract_structured", max_chars=10)
    assert same_view.is_error is True, "no rendering: the same source view, the same allowance"
    assert ctx.read_window_count(plain.file_id, "extract_structured") == 0


@pytest.mark.asyncio
async def test_parallel_reads_of_one_file_are_admitted_race_free(monkeypatch):
    """A batch dispatches its calls with ``asyncio.gather``: a count taken
    after the bytes load would let every call of the batch see the cap unmet.
    Admission is one synchronous step, before any await."""
    import asyncio

    _cap(monkeypatch, 2)
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    _text, ref = _big_text_ref()
    ctx.add(ref)
    executor.file_context = ctx
    original = FileContext.read_bytes

    async def _slow(self, file_id):
        await asyncio.sleep(0.01)
        return await original(self, file_id)

    monkeypatch.setattr(FileContext, "read_bytes", _slow)
    results = await asyncio.gather(
        *[_call(executor, "read_file_content", file_id=ref.file_id, offset=i * 1_000, max_chars=1_000) for i in range(6)]
    )
    assert sum(1 for r in results if not r.is_error) == 2
    assert sum(1 for r in results if r.is_error and "window budget for this file exhausted" in r.content) == 4


@pytest.mark.asyncio
async def test_an_image_read_is_never_counted_against_the_cap(monkeypatch):
    """Re-reading an image is the recovery path every dropped-image note
    names: it must never meet the text-window cap."""
    _cap(monkeypatch, 1)
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    ref = _generated_ref(b"\x89PNG fake", filename="chart.png", content_type="image/png")
    ctx.add(ref)
    executor.file_context = ctx
    for _ in range(3):
        result = await _call(executor, "read_file_content", file_id=ref.file_id)
        assert "window(s) of" not in result.content
    assert ctx.read_window_count(ref.file_id, "source") == 0


@pytest.mark.asyncio
async def test_a_cancelled_read_gives_its_window_back(monkeypatch):
    """Only ``except Exception`` handed the slot
    back, so a read cancelled by a tool timeout (``CancelledError``) kept it,
    and after a few the file read as spent for the rest of the turn under a
    message saying its windows had been read."""
    import asyncio

    _cap(monkeypatch, 2)
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    _text, ref = _big_text_ref()
    ctx.add(ref)
    executor.file_context = ctx
    original = FileContext.read_bytes

    async def _cancelled(self, file_id):
        raise asyncio.CancelledError

    monkeypatch.setattr(FileContext, "read_bytes", _cancelled)
    for _ in range(3):
        with pytest.raises(asyncio.CancelledError):
            await _call(executor, "read_file_content", file_id=ref.file_id)
    assert ctx.read_window_count(ref.file_id, "source") == 0
    monkeypatch.setattr(FileContext, "read_bytes", original)
    served = await _call(executor, "read_file_content", file_id=ref.file_id, max_chars=1_000)
    assert served.is_error is False and "(1 more window(s) this turn)" in served.content


@pytest.mark.asyncio
async def test_a_zero_cap_disables_the_limit(monkeypatch):
    _cap(monkeypatch, 0)
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    _text, ref = _big_text_ref()
    ctx.add(ref)
    executor.file_context = ctx
    for i in range(8):
        result = await _call(executor, "read_file_content", file_id=ref.file_id, offset=i * 1_000, max_chars=1_000)
        assert result.is_error is False
        assert "more window(s)" not in result.content and "last window" not in result.content


def test_the_file_context_admission_ledger():
    ctx = FileContext()
    assert ctx.admit_read_window("file-a", "source", 2) == 0, "the file- prefix is the same file"
    assert ctx.admit_read_window("a", "source", 2) == 1
    assert ctx.admit_read_window("a", "source", 2) is None
    assert ctx.admit_read_window("a", "extract_structured", 2) == 0
    ctx.release_read_window("a", "source")
    assert ctx.read_window_count("a", "source") == 1
    ctx.release_read_window("a", "source")
    ctx.release_read_window("a", "source")
    assert ctx.read_window_count("a", "source") == 0, "never below zero"
    assert all(ctx.admit_read_window("b", "source", 0) == i for i in range(10)), "0 = no cap"


def test_the_read_tool_schema_names_the_rendered_view_and_the_cap(monkeypatch):
    defs, _executor = _build(monkeypatch, [])
    read = next(d for d in defs if d.name == "read_file_content")
    assert read.input_schema["properties"]["mode"]["enum"] == ["source", "extract_structured"]
    assert read.input_schema["properties"]["mode"]["type"] == "string"
    assert 'mode="extract_structured"' in read.description
    assert "A few windows per file are served each turn" in read.description
    query = next(d for d in defs if d.name == "query_file_json")
    assert query.input_schema["properties"]["block"]["type"] == "integer"
    assert "JavaScript data literals" in query.description


@pytest.mark.asyncio
async def test_read_file_content_extracts_rendered_documents(monkeypatch):
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    ref = _generated_ref(b"%PDF-1.4 fake", filename="report.pdf", content_type="application/pdf")
    ctx.add(ref)
    executor.file_context = ctx

    async def _fake_read_text(self, file_id):
        assert file_id == ref.file_id
        return "Extracted PDF text"

    monkeypatch.setattr(FileContext, "read_text", _fake_read_text)

    result = await _call(executor, "read_file_content", file_id=ref.file_id)
    assert result.is_error is False
    assert "Extracted PDF text" in result.content
    assert "not editable with edit_file_content" in result.content
    assert "generate_file" in result.content


@pytest.mark.asyncio
async def test_read_file_content_shows_an_image(monkeypatch):
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    ref = _generated_ref(b"\x89PNG fake", filename="chart.png", content_type="image/png")
    ctx.add(ref)
    executor.file_context = ctx

    result = await _call(executor, "read_file_content", file_id=ref.file_id)
    assert result.is_error is False
    assert result.files == [ref]
    assert "attached to this result" in result.content


# ── The vision budget and the notes ("look once, remember") ────────


def _image_context(*names: str) -> tuple[FileContext, list[FileRef]]:
    ctx = FileContext()
    refs = []
    for name in names:
        ref = _generated_ref(b"\x89PNG fake " + name.encode(), filename=name, content_type="image/png")
        ctx.add(ref)
        refs.append(ref)
    return ctx, refs


def _carried_by_a_request(ctx: FileContext, ref: FileRef, step: int = 1) -> None:
    """What the LOOP does once a completed provider request carried *ref*'s
    pixels (``_consume_transcript`` -> ``_record_vision_views``): the tool
    itself never writes the ledger."""
    ctx.record_vision(ref.file_id, step)


@pytest.mark.asyncio
async def test_an_image_is_attached_once_per_turn_and_then_answered_with_the_notes(monkeypatch):
    """First read: pixels + the request to save notes. annotate_file stores
    them. Second read of the SAME file this turn, once a request carried the
    first: the notes, no pixels, not an error - the model works from what it
    wrote."""
    _defs, executor = _build(monkeypatch, [])
    ctx, (ref,) = _image_context("founders.png")
    executor.file_context = ctx

    first = await _call(executor, "read_file_content", file_id=ref.file_id)
    assert first.is_error is False
    assert first.files == [ref]
    assert "the image is attached to this result" in first.content
    assert "annotate_file" in first.content, "the footer asks for the notes"
    # The tool records nothing: a view is the loop's to date, when a request
    # has carried the pixels. Until then a re-read carries them again.
    assert ref.file_id not in ctx.vision_views
    pruned_before_any_request = await _call(executor, "read_file_content", file_id=ref.file_id)
    assert pruned_before_any_request.files == [ref]
    _carried_by_a_request(ctx, ref, step=1)

    noted = await _call(
        executor,
        "annotate_file",
        file_id=ref.file_id,
        notes="Two founders, circular photos; the ANSSI seal is the OLD one; card is flush left.",
    )
    assert noted.is_error is False, noted.content
    assert "Saved notes for founders.png" in noted.content
    assert ref.notes.startswith("Two founders, circular photos")

    second = await _call(executor, "read_file_content", file_id=ref.file_id)
    assert second.is_error is False
    assert second.files is None, "the pixels are attached at most once per turn"
    assert "already viewed at step 1 this turn" in second.content
    assert "Your notes: Two founders" in second.content
    assert "call read_file_content" not in second.content


@pytest.mark.asyncio
async def test_a_file_another_tool_already_showed_this_turn_is_not_re_sent(monkeypatch):
    """The ledger is the turn's ``FileContext.vision_views``, which the loop
    fills for EVERY image-bearing result (a browser screenshot, a rendered
    figure, a generated image): a first ``read_file_content`` of a file one of
    them attached at step 2 is the SECOND view, answered with the notes."""
    _defs, executor = _build(monkeypatch, [])
    ctx, (shot, fresh) = _image_context("shot.png", "fresh.png")
    ctx.record_vision(f"file-{shot.file_id}", 2)
    assert ctx.vision_step(shot.file_id) == 2 and ctx.vision_step(f"file-{shot.file_id}") == 2
    ctx.record_vision(shot.file_id, 7)
    assert ctx.vision_step(shot.file_id) == 2, "the first view wins"
    shot.notes = "the founders slide, cyan seal"
    executor.file_context = ctx

    result = await _call(executor, "read_file_content", file_id=shot.file_id)
    assert result.is_error is False
    assert result.files is None, "the pixels reached the model once already"
    assert "already viewed at step 2 this turn" in result.content
    assert "Your notes: the founders slide, cyan seal" in result.content
    # A file nothing showed yet is attached; the read leaves the ledger alone
    # (the loop enters it once a request has carried the result).
    first = await _call(executor, "read_file_content", file_id=fresh.file_id)
    assert first.files == [fresh]
    assert fresh.file_id not in ctx.vision_views


@pytest.mark.asyncio
async def test_a_second_read_without_notes_is_an_explicit_error_never_a_retry_hint(monkeypatch):
    _defs, executor = _build(monkeypatch, [])
    ctx, (ref,) = _image_context("diagram.png")
    executor.file_context = ctx

    await _call(executor, "read_file_content", file_id=ref.file_id)
    _carried_by_a_request(ctx, ref, step=1)
    again = await _call(executor, "read_file_content", file_id=ref.file_id)
    assert again.is_error is True
    assert again.files is None
    assert "already viewed at step 1" in again.content
    assert "no notes were saved" in again.content
    assert "annotate_file" in again.content
    assert "call read_file_content" not in again.content
    # And a third ask reads the same: the answer does not change by asking.
    third = await _call(executor, "read_file_content", file_id=ref.file_id)
    assert third.is_error is True and third.files is None


@pytest.mark.asyncio
async def test_the_read_tool_does_not_apply_the_vision_cap_itself(monkeypatch):
    """The per-turn cap is the LOOP's, applied where every tool result is
    appended (``_admit_tool_result_images`` - see ``test_loop_breaker``), so a
    screenshot and a read spend the same budget. The tool only answers from
    the ledger: with nothing carried yet, every distinct read attaches."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "agent_loop_vision_views_per_turn", 2)
    _defs, executor = _build(monkeypatch, [])
    ctx, refs = _image_context("a.png", "b.png", "c.png")
    executor.file_context = ctx
    for ref in refs:
        result = await _call(executor, "read_file_content", file_id=ref.file_id)
        assert result.files == [ref]
        assert result.is_error is False


@pytest.mark.asyncio
async def test_stored_notes_ride_the_first_view_too(monkeypatch):
    """A file annotated in an earlier turn (FileRef.notes from the row's
    description) is shown WITH its notes: the model reads both."""
    _defs, executor = _build(monkeypatch, [])
    ctx, (ref,) = _image_context("logo.png")
    ref.notes = "Filigran wordmark, cyan #0FBCFF on dark"
    executor.file_context = ctx
    first = await _call(executor, "read_file_content", file_id=ref.file_id)
    assert first.files == [ref]
    assert "Your notes: Filigran wordmark, cyan #0FBCFF on dark" in first.content
    assert "save 1-3 lines" not in first.content


@pytest.mark.asyncio
async def test_annotate_file_refuses_empty_notes_and_bounds_long_ones(monkeypatch):
    _defs, executor = _build(monkeypatch, [])
    ctx, (ref,) = _image_context("x.png")
    executor.file_context = ctx
    empty = await _call(executor, "annotate_file", file_id=ref.file_id, notes="   ")
    assert empty.is_error is True
    assert ref.notes is None
    long = await _call(executor, "annotate_file", file_id=ref.file_id, notes="y " * 1000)
    assert long.is_error is False
    assert len(ref.notes) <= media.FILE_NOTES_MAX_CHARS
    assert ref.notes.endswith("...")
    unknown = await _call(executor, "annotate_file", file_id="nope", notes="z")
    assert unknown.is_error is True
    assert "Available files" in unknown.content


@pytest.mark.asyncio
async def test_annotate_file_stores_one_line_of_plain_text(monkeypatch):
    """Line breaks collapse to spaces; the control characters ``split`` keeps
    (NUL fails the PostgreSQL write, ESC starts a terminal sequence) are
    dropped, so the caption a person reads in the Templates UI is text."""
    _defs, executor = _build(monkeypatch, [])
    ctx, (ref,) = _image_context("x.png")
    executor.file_context = ctx
    result = await _call(
        executor,
        "annotate_file",
        file_id=ref.file_id,
        notes="Two founders\r\n  circular photos\x00, the seal\x1b[31m is the OLD one\x07\tflush left",
    )
    assert result.is_error is False, result.content
    assert ref.notes == "Two founders circular photos, the seal[31m is the OLD one flush left"
    assert "\n" not in ref.notes and "\x00" not in ref.notes and "\x1b" not in ref.notes
    only_controls = await _call(executor, "annotate_file", file_id=ref.file_id, notes="\x00\x01\x1b")
    assert only_controls.is_error is True, "nothing readable was said"
    assert ref.notes.startswith("Two founders"), "the previous notes are kept"


@pytest.mark.asyncio
async def test_annotate_file_persists_on_the_stored_row(monkeypatch):
    """A file with a database row gets its description written on a short
    session of its own; an inline ref (no row) keeps the notes for the turn."""
    statements: list[object] = []
    rowcount = {"n": 1}

    class _Result:
        def __init__(self, n: int) -> None:
            self.rowcount = n

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, stmt):
            statements.append(stmt)
            return _Result(rowcount["n"])

        async def commit(self):
            statements.append("commit")

    monkeypatch.setattr(media, "_own_session", lambda: _Session())
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    stored = FileRef(
        file_id=str(uuid.uuid4()),
        filename="stored.png",
        content_type="image/png",
        size=10,
        s3_object_name="chat/conv/stored.png",
        source="agent",
    )
    inline = _generated_ref(b"\x89PNG", filename="inline.png", content_type="image/png")
    ctx.add(stored)
    ctx.add(inline)
    executor.file_context = ctx

    result = await _call(executor, "annotate_file", file_id=stored.file_id, notes="a stored picture")
    assert "stored with the file" in result.content
    assert statements and statements[-1] == "commit"
    assert stored.notes == "a stored picture"
    # The UPDATE is fenced to conversation / run rows: a template asset's
    # caption (a shared, human-authored description) is never rewritten
    # from a conversation, whatever handle the context holds.
    update_sql = str(statements[0].compile(compile_kwargs={"literal_binds": False}))
    assert "template_container_id IS NULL" in update_sql

    result = await _call(executor, "annotate_file", file_id=inline.file_id, notes="an inline picture")
    assert "kept for this turn" in result.content
    assert inline.notes == "an inline picture"
    # The fence matching NO row - a template asset held in this context, a
    # row already gone - is a real outcome: the answer reads the UPDATE's
    # row count, never the fact that the statement ran.
    rowcount["n"] = 0
    result = await _call(executor, "annotate_file", file_id=stored.file_id, notes="a caption nobody may rewrite")
    assert "stored with the file" not in result.content
    assert "kept for this turn only" in result.content and "description was not changed" in result.content
    assert stored.notes == "a caption nobody may rewrite", "the in-memory notes still serve this turn"


@pytest.mark.asyncio
async def test_the_first_view_claim_and_its_ask_are_one_sentence_the_strip_removes_whole(monkeypatch):
    """The loop's claim strip removes the SENTENCE that claims the
    attachment; "look at it now" must be part of it, or a dropped result
    would read "Look at it now ..." next to a note saying the pixels are not
    re-sent - two statements about the same pixels."""
    from app.tools.agent_loop import _strip_attachment_claims

    _defs, _executor = _build(monkeypatch, [])
    ctx, (ref,) = _image_context("x.png")
    _executor.file_context = ctx

    first = await _call(_executor, "read_file_content", file_id=ref.file_id)
    assert "the image is attached to this result; look at it now" in first.content
    stripped = _strip_attachment_claims(first.content)
    assert "look at it now" not in stripped.lower()
    assert "annotate_file" not in stripped, "the ask went with the claim; the dropped-files note carries its own"


def test_a_listing_quotes_the_notes_cut_not_whole():
    """The Available Files block rides the SYSTEM prompt and lists every file
    of the conversation, so a listing quotes at most QUOTED_NOTES_MAX_CHARS
    of a file's notes; the full notes stay with the read answer."""
    from app.services.builtin_tools._shared import list_available_files
    from app.tools.base import QUOTED_NOTES_MAX_CHARS, quoted_file_notes

    long_notes = "word " * 200
    assert len(quoted_file_notes(long_notes)) == QUOTED_NOTES_MAX_CHARS
    assert quoted_file_notes(long_notes).endswith("...")
    assert quoted_file_notes("short  note\nhere") == "short note here"
    assert quoted_file_notes(None) == ""
    ctx, (a,) = _image_context("a.png")
    a.notes = long_notes
    listing = list_available_files(ctx)
    assert "..." in listing and "word word word" in listing
    assert len(listing) < len(long_notes)
    block = ctx.to_prompt_section()
    assert "..." in block
    assert long_notes.strip() not in block


def test_available_files_blocks_show_the_notes():
    from app.services.builtin_tools._shared import list_available_files

    ctx, (a, _b) = _image_context("a.png", "b.png")
    a.notes = 'Blue "square" on white'
    section = ctx.to_prompt_section(inlined_file_ids=set())
    assert "notes=\"Blue 'square' on white\"" in section
    assert section.count("notes=") == 1
    listing = list_available_files(ctx)
    assert "- notes: Blue \"square\" on white" in listing
    assert "b.png" in listing and listing.count("notes:") == 1


@pytest.mark.asyncio
async def test_a_run_that_re_reads_a_pruned_image_gets_the_explicit_answer_never_the_retry_hint(monkeypatch):
    """End to end through the agent loop: the model
    reads an image, the retention pass prunes the pixels once seen, and the
    model asks for it again. What comes back is the explicit "already viewed"
    answer - the pixels once, then never a hint to call again."""
    from app.llm.base import ChatMessage, ChatResponse
    from app.tools import agent_loop as al
    from app.tools.base import CompositeToolExecutor
    from tests.test_tool_call_dedup import _quiet_langfuse, _ScriptedLLM  # noqa: F401

    for name in ("_lf_start_llm_gen", "_lf_end_generation", "_lf_end_span", "_lf_end_tool_span"):
        monkeypatch.setattr(al, name, lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_loop_span", lambda *a, **kw: None)
    # A vision budget no image fits, so the seen result loses its pixels on
    # the next pass - the incident's shape.
    monkeypatch.setattr(al, "_TOOL_RESULT_FILES_LIVE_MIN_BYTES", 1)
    monkeypatch.setattr(al, "_TOOL_RESULT_FILES_LIVE_MAX_BYTES", 1)

    defs, executor = _build(monkeypatch, [])
    ctx, (ref,) = _image_context("founders.png")
    ref.size = 300_000
    composite = CompositeToolExecutor()
    composite.add_tools(defs, executor)
    composite.file_context = ctx
    executor.file_context = ctx

    def _read(i: int) -> ChatResponse:
        return ChatResponse(
            content="",
            model="stub",
            stop_reason="tool_calls",
            tool_calls=[ToolCall(id=f"r{i}", name="read_file_content", arguments={"file_id": ref.file_id})],
        )

    llm = _ScriptedLLM([_read(1), _read(2), _read(3), ChatResponse(content="Done.", model="stub")])
    messages: list[ChatMessage] = [ChatMessage(role="user", content="fix the founders slide")]
    response = await al.run_agent_loop(
        llm=llm, messages=messages, tools=composite.all_tools, executor=composite, max_iterations=10
    )
    assert response.content == "Done."
    results = [m for m in messages if m.role == "tool"]
    assert [m.tool_call_id for m in results] == ["r1", "r2", "r3"]
    first, second, third = results
    # The first read attached the pixels (the ONE attachment of this file this
    # turn) at step 1 - the step the tool ran in, the SAME number the ledger's
    # own "already viewed at step 1" below uses (the request that carried it
    # was step 2's, which is not what the model calls the view); whether the
    # retention pass has since taken the pixels or kept them as the newest
    # file-bearing group, the message never invites a re-read.
    assert first.appended_at_iteration == 1
    assert first.seen_at_iteration == 1
    assert "call read_file_content" not in first.content
    if first.files is None:
        assert "Already viewed at step 1" in first.content
    else:
        assert "the image is attached to this result" in first.content
    # The re-reads are answered explicitly, without pixels and without a hint
    # to call again.
    for later in (second, third):
        assert later.files is None
        assert "already viewed at step 1 this turn" in later.content
        assert "call read_file_content" not in later.content
        assert "annotate_file" in later.content


def test_annotate_file_is_in_the_file_generation_suite(monkeypatch):
    defs, _executor = _build(monkeypatch, [])
    names = [d.name for d in defs]
    assert names == [
        "generate_file",
        "read_file_content",
        "query_file_json",
        "edit_file_content",
        "annotate_file",
        "render_html_to_image",
    ]
    annotate = next(d for d in defs if d.name == "annotate_file")
    assert annotate.input_schema["required"] == ["file_id", "notes"]
    assert annotate.source == "builtin:files"
    read = next(d for d in defs if d.name == "read_file_content")
    assert "ONCE per turn" in read.description


SVG = '<svg xmlns="http://www.w3.org/2000/svg"><text>Draft</text></svg>\n'


@pytest.mark.asyncio
async def test_svg_is_read_and_edited_as_source_text(monkeypatch):
    """An SVG is an image to a vision model AND markup to an editor.

    ``is_text_like_file`` admits it, so ``edit_file_content`` accepts it;
    ``read_file_content`` must therefore serve the markup, not attach the
    picture - or the edit contract ("read first, copy the snippet exactly")
    can never be honoured for the one image type it applies to.
    """
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    ctx = FileContext()
    ref = _generated_ref(SVG.encode("utf-8"), filename="logo.svg", content_type="image/svg+xml")
    ctx.add(ref)
    executor.file_context = ctx

    read = await _call(executor, "read_file_content", file_id=ref.file_id)
    assert read.is_error is False, read.content
    assert read.files is None
    assert "editable source text" in read.content
    assert SVG in read.content

    edited = await _call(
        executor,
        "edit_file_content",
        file_id=ref.file_id,
        edits=[{"find": "<text>Draft</text>", "replace": "<text>Final</text>"}],
    )
    assert edited.is_error is False, edited.content
    assert persisted[0]["raw_bytes"] == SVG.replace("Draft", "Final").encode("utf-8")
    assert persisted[0]["content_type"] == "image/svg+xml"

    # A raster behind an ``.svg`` name has no markup to read: it is shown.
    raster = _generated_ref(b"\x89PNG\r\n\x1a\n\x00\x00", filename="odd.svg", content_type="image/svg+xml")
    ctx.add(raster)
    shown = await _call(executor, "read_file_content", file_id=raster.file_id)
    assert shown.is_error is False
    assert shown.files == [raster]
    assert "attached to this result" in shown.content


@pytest.mark.asyncio
async def test_read_file_content_unknown_id_lists_what_is_available(monkeypatch):
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    ref = _generated_ref(HTML.encode("utf-8"))
    ctx.add(ref)
    executor.file_context = ctx

    result = await _call(executor, "read_file_content", file_id="nope")
    assert result.is_error is True
    assert "no file 'nope'" in result.content
    assert ref.file_id in result.content
    assert "briefing.html" in result.content


@pytest.mark.asyncio
async def test_edit_file_content_saves_a_new_revision(monkeypatch):
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    ctx = FileContext()
    ref = _generated_ref(HTML.encode("utf-8"))
    ctx.add(ref)
    executor.file_context = ctx

    result = await _call(
        executor,
        "edit_file_content",
        file_id=ref.file_id,
        edits=[{"find": "<p>JP Salles still shows as declined.</p>\n", "replace": ""}],
    )

    assert result.is_error is False, result.content
    assert len(persisted) == 1
    saved = persisted[0]
    new_text = saved["raw_bytes"].decode("utf-8")
    assert "JP Salles" not in new_text
    assert "Sean has not accepted." in new_text
    assert saved["filename"] == "briefing.html"
    assert saved["content_type"] == "text/html"
    # The revision keeps the provenance of the file it revises, so a
    # download card stays a download card.
    assert saved["source"] == "agent_download"

    assert result.files and result.files[0].file_id != ref.file_id
    new_id = result.files[0].file_id
    assert f"[[FILE:{new_id}]]" in result.content
    assert "1 edit applied" in result.content
    assert f"file_id={ref.file_id}" in result.content
    # The revision is registered on the loop's FileContext at once, so the
    # next edit in the same turn can target it; the original is untouched.
    assert ctx.get(new_id) is not None
    assert ctx.get(ref.file_id) is ref
    assert ctx.get(new_id).is_agent_generated


@pytest.mark.asyncio
async def test_edit_file_content_of_an_upload_is_an_agent_download(monkeypatch):
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    ctx = FileContext()
    ref = _generated_ref(b"a,b\n1,2\n", filename="data.csv", content_type="text/csv", source="chat")
    ctx.add(ref)
    executor.file_context = ctx

    result = await _call(
        executor,
        "edit_file_content",
        file_id=ref.file_id,
        edits=[{"find": "1,2", "replace": "1,3"}],
        filename="data-fixed.csv",
    )
    assert result.is_error is False, result.content
    assert persisted[0]["source"] == "agent_download"
    assert persisted[0]["filename"] == "data-fixed.csv"
    assert persisted[0]["raw_bytes"] == b"a,b\n1,3\n"


@pytest.mark.asyncio
async def test_edit_file_content_keeps_the_utf8_bom_of_the_original(monkeypatch):
    """An edit changes the snippets it named and nothing else.

    ``decode_file_text`` drops the BOM so the model never has to reproduce
    an invisible character in a ``find``; the revision must open with the
    same three bytes the original did, or Excel's CSV encoding detection
    breaks on a file the edit never asked to re-encode. A file WITHOUT a BOM
    does not get one.
    """
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    ctx = FileContext()
    with_bom = media.UTF8_BOM + b"name,city\nJulie,Paris\n"
    ref = _generated_ref(with_bom, filename="clients.csv", content_type="text/csv", source="chat")
    ctx.add(ref)
    executor.file_context = ctx

    result = await _call(
        executor,
        "edit_file_content",
        file_id=ref.file_id,
        edits=[{"find": "Paris", "replace": "Lyon"}],
    )
    assert result.is_error is False, result.content
    assert persisted[0]["raw_bytes"] == media.UTF8_BOM + b"name,city\nJulie,Lyon\n"

    plain = _generated_ref(b"a,b\n1,2\n", filename="plain.csv", content_type="text/csv", source="chat")
    ctx.add(plain)
    result = await _call(
        executor,
        "edit_file_content",
        file_id=plain.file_id,
        edits=[{"find": "1,2", "replace": "1,3"}],
    )
    assert result.is_error is False, result.content
    assert persisted[1]["raw_bytes"] == b"a,b\n1,3\n"
    assert not persisted[1]["raw_bytes"].startswith(media.UTF8_BOM)

    # A ``replace`` copied out of another BOM-prefixed source puts U+FEFF back
    # at offset 0 itself: the revision still opens with exactly ONE marker.
    result = await _call(
        executor,
        "edit_file_content",
        file_id=ref.file_id,
        edits=[{"find": "name,city", "replace": "\ufeffname,city"}],
    )
    assert result.is_error is False, result.content
    assert persisted[2]["raw_bytes"].startswith(media.UTF8_BOM)
    assert persisted[2]["raw_bytes"].count(media.UTF8_BOM) == 1
    assert persisted[2]["raw_bytes"] == media.UTF8_BOM + b"name,city\nJulie,Paris\n"


@pytest.mark.asyncio
async def test_edit_file_content_refuses_a_non_utf8_file_and_persists_nothing(monkeypatch):
    """A Windows-1252 CSV a user uploaded is refused, not corrupted.

    With a lossy decode the edit would have applied - the model copies the
    ``caf\\ufffd`` it was shown, so the find matches - and the saved revision
    would carry U+FFFD in place of every non-ASCII byte the edit never
    touched. The refusal names the encoding so the model does not retry the
    same call, and no revision is written. The file stays readable: the read
    tool falls back to the extraction path for it.
    """
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    ctx = FileContext()
    legacy = "name,city\nJulie,café\nMarc,Genève\n".encode("cp1252")
    ref = _generated_ref(legacy, filename="clients.csv", content_type="text/csv", source="chat")
    ctx.add(ref)
    executor.file_context = ctx

    result = await _call(
        executor,
        "edit_file_content",
        file_id=ref.file_id,
        edits=[{"find": "Marc", "replace": "Mark"}],
    )
    assert result.is_error is True
    assert "not UTF-8 text" in result.content
    assert "Windows-1252" in result.content
    assert "Nothing was changed" in result.content
    assert persisted == []
    assert ctx.get(ref.file_id) is ref

    async def _fake_read_text(self, file_id):
        assert file_id == ref.file_id
        return "name,city / Julie,café / Marc,Genève"

    monkeypatch.setattr(FileContext, "read_text", _fake_read_text)
    read = await _call(executor, "read_file_content", file_id=ref.file_id)
    assert read.is_error is False, read.content
    assert "not editable with edit_file_content" in read.content
    assert "Genève" in read.content


@pytest.mark.asyncio
async def test_edit_file_content_keeps_a_working_file_a_working_file(monkeypatch):
    """The revision keeps the ONE provenance that changes how it is shown.

    ``agent_working`` files are the code interpreter's intermediates and are
    rendered as such; every other origin (a download card, an upload, an
    MCP result) becomes ``agent_download`` - the deliverable the edit
    produced - so the revision of a working file never turns into a card
    and the revision of an upload never hides as an intermediate.
    """
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    ctx = FileContext()
    working = _generated_ref(b"a,b\n1,2\n", filename="scratch.csv", content_type="text/csv", source="agent_working")
    mcp = _generated_ref(b"a,b\n1,2\n", filename="fetched.csv", content_type="text/csv", source="mcp")
    ctx.add(working)
    ctx.add(mcp)
    executor.file_context = ctx

    result = await _call(
        executor, "edit_file_content", file_id=working.file_id, edits=[{"find": "1,2", "replace": "1,3"}]
    )
    assert result.is_error is False, result.content
    assert persisted[-1]["source"] == "agent_working"

    result = await _call(
        executor, "edit_file_content", file_id=mcp.file_id, edits=[{"find": "1,2", "replace": "1,3"}]
    )
    assert result.is_error is False, result.content
    assert persisted[-1]["source"] == "agent_download"


@pytest.mark.asyncio
async def test_edit_file_content_accepts_the_marker_the_model_copies(monkeypatch):
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    ctx = FileContext()
    ref = _generated_ref(HTML.encode("utf-8"))
    ctx.add(ref)
    executor.file_context = ctx

    result = await _call(
        executor,
        "edit_file_content",
        file_id=f"[[FILE:{ref.file_id}]]",
        edits=[{"find": "<h1>Briefing</h1>", "replace": "<h1>Brief</h1>"}],
    )
    assert result.is_error is False, result.content
    assert len(persisted) == 1
    assert b"<h1>Brief</h1>" in persisted[0]["raw_bytes"]


@pytest.mark.asyncio
async def test_edit_file_content_refuses_absent_snippet_and_writes_nothing(monkeypatch):
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    ctx = FileContext()
    ref = _generated_ref(HTML.encode("utf-8"))
    ctx.add(ref)
    executor.file_context = ctx

    result = await _call(
        executor,
        "edit_file_content",
        file_id=ref.file_id,
        edits=[{"find": "JP Salles still shows as declined", "replace": ""},
               {"find": "this text is not there", "replace": ""}],
    )
    assert result.is_error is True
    assert "edit 2" in result.content
    assert "Nothing was changed" in result.content
    assert persisted == []
    assert len(ctx.list_files()) == 1


@pytest.mark.asyncio
async def test_edit_file_content_noop_is_not_a_revision(monkeypatch):
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    ctx = FileContext()
    ref = _generated_ref(HTML.encode("utf-8"))
    ctx.add(ref)
    executor.file_context = ctx

    result = await _call(
        executor,
        "edit_file_content",
        file_id=ref.file_id,
        edits=[{"find": "<h1>Briefing</h1>", "replace": "<h1>Briefing</h1>"}],
    )
    assert result.is_error is False
    assert "No change" in result.content
    assert persisted == []


@pytest.mark.asyncio
async def test_edit_file_content_refuses_rendered_documents(monkeypatch):
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    ctx = FileContext()
    ref = _generated_ref(b"%PDF-1.4 fake", filename="report.pdf", content_type="application/pdf")
    ctx.add(ref)
    executor.file_context = ctx

    result = await _call(
        executor,
        "edit_file_content",
        file_id=ref.file_id,
        edits=[{"find": "x", "replace": "y"}],
    )
    assert result.is_error is True
    assert "not an editable text file" in result.content
    assert "generate_file" in result.content
    assert persisted == []


@pytest.mark.asyncio
async def test_edit_file_content_refuses_binary_behind_a_text_name(monkeypatch):
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    ctx = FileContext()
    ref = _generated_ref(b"PK\x03\x04\x00\x00", filename="weird.txt", content_type="text/plain")
    ctx.add(ref)
    executor.file_context = ctx

    result = await _call(
        executor,
        "edit_file_content",
        file_id=ref.file_id,
        edits=[{"find": "PK", "replace": "ZZ"}],
    )
    assert result.is_error is True
    assert "Nothing was changed" in result.content
    assert persisted == []


@pytest.mark.asyncio
async def test_edit_file_content_unknown_file(monkeypatch):
    _defs, executor = _build(monkeypatch, [])
    executor.file_context = FileContext()

    result = await _call(
        executor, "edit_file_content", file_id="missing", edits=[{"find": "a", "replace": "b"}]
    )
    assert result.is_error is True
    assert "no file 'missing'" in result.content
    assert "No files are attached" in result.content


# ── A second file of the same name in one turn ────────────────────────────


@pytest.mark.asyncio
async def test_generate_file_says_a_same_name_file_of_this_turn_is_superseded(monkeypatch):
    persisted: list[dict] = []
    _defs, executor = _build(monkeypatch, persisted)
    ctx = FileContext()
    executor.file_context = ctx

    first = await _call(
        executor, "generate_file", filename="run-prompt.txt", content="draft one", file_tag="download_file"
    )
    first_id = first.files[0].file_id
    assert first.content == f"[[FILE:{first_id}]]"

    second = await _call(
        executor, "generate_file", filename="run-prompt.txt", content="draft two", file_tag="download_file"
    )
    second_id = second.files[0].file_id
    assert second.is_error is False, second.content
    assert second_id != first_id
    assert second.content.startswith(f"[[FILE:{second_id}]]")
    assert (
        f"NOTE: run-prompt.txt was already generated in this turn (file_id {first_id}). "
        "This new file supersedes it; prefer edit_file_content for corrections."
    ) in second.content
    # Both stay readable: nothing about storage or ids changes.
    assert ctx.get(first_id) is not None and ctx.get(second_id) is not None


@pytest.mark.asyncio
async def test_the_note_names_the_latest_file_of_that_name(monkeypatch):
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    first = _generated_ref(b"one", filename="notes.md", content_type="text/markdown")
    ctx.add_produced(first)
    executor.file_context = ctx

    edited = await _call(
        executor, "edit_file_content", file_id=first.file_id, edits=[{"find": "one", "replace": "two"}]
    )
    revision_id = edited.files[0].file_id
    third = await _call(
        executor, "generate_file", filename="notes.md", content="three", content_type="text/markdown"
    )

    assert f"(file_id {revision_id})" in third.content
    assert f"(file_id {first.file_id})" not in third.content


@pytest.mark.asyncio
async def test_no_note_for_another_name_or_for_a_file_of_an_earlier_turn(monkeypatch):
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    # A file written in an earlier turn reaches this turn's context as an
    # ordinary file, never as one this turn produced.
    ctx.add(_generated_ref(b"old", filename="run-prompt.txt", content_type="text/plain"))
    executor.file_context = ctx

    same_name = await _call(executor, "generate_file", filename="run-prompt.txt", content="new")
    other_name = await _call(executor, "generate_file", filename="summary.txt", content="new")

    assert "NOTE:" not in same_name.content
    assert "NOTE:" not in other_name.content


@pytest.mark.asyncio
async def test_no_note_when_the_same_bytes_reuse_the_same_file(monkeypatch):
    rows: dict[tuple[str, bytes], SimpleNamespace] = {}

    async def _dedup_persist(**kwargs):
        key = (kwargs["filename"], kwargs["raw_bytes"])
        if key not in rows:
            rows[key] = SimpleNamespace(
                id=uuid.uuid4(),
                filename=kwargs["filename"],
                content_type=kwargs["content_type"],
                size=len(kwargs["raw_bytes"]),
                s3_object_name=f"chat/conv/{kwargs['filename']}",
                source=kwargs["source"],
            )
        return rows[key]

    _defs, executor = _build(monkeypatch, [])
    monkeypatch.setattr("app.services.builtin_tools._persist_generated_file", _dedup_persist)
    executor.file_context = FileContext()

    first = await _call(executor, "generate_file", filename="run-prompt.txt", content="same")
    again = await _call(executor, "generate_file", filename="run-prompt.txt", content="same")

    assert again.files[0].file_id == first.files[0].file_id
    assert again.content == f"[[FILE:{first.files[0].file_id}]]"


def test_the_note_only_suggests_an_edit_for_a_text_file():
    earlier = FileRef(file_id="a", filename="deck.pdf", content_type="application/pdf", size=1, s3_object_name="a")
    newer = FileRef(file_id="b", filename="deck.pdf", content_type="application/pdf", size=2, s3_object_name="b")

    note = media._same_name_note(newer, earlier)

    assert note == "NOTE: deck.pdf was already generated in this turn (file_id a). This new file supersedes it."


def test_produced_same_name_matches_name_and_type_of_this_turn_only():
    ctx = FileContext()

    def ref(file_id: str, filename: str, content_type: str) -> FileRef:
        return FileRef(file_id=file_id, filename=filename, content_type=content_type, size=1, s3_object_name=file_id)

    ctx.add(ref("uploaded", "report.csv", "text/csv"))
    ctx.add_produced(ref("first", "report.csv", "text/csv"))
    ctx.add_produced(ref("other-type", "report.csv", "application/json"))
    ctx.add_produced(ref("second", "report.csv", "text/csv; charset=utf-8"))

    newest = ref("third", "report.csv", "TEXT/CSV")
    assert ctx.produced_same_name(newest).file_id == "second"
    # The file itself is never its own predecessor.
    ctx.add_produced(newest)
    assert ctx.produced_same_name(newest).file_id == "second"
    assert ctx.produced_same_name(ref("x", "report.json", "text/csv")) is None
    assert ctx.produced_same_name(ref("y", "report.csv", "image/png")) is None
    assert ctx.produced_ids == ["first", "other-type", "second", "third"]


@pytest.mark.asyncio
async def test_edit_file_content_says_the_new_revision_supersedes_the_previous(monkeypatch):
    _defs, executor = _build(monkeypatch, [])
    ctx = FileContext()
    ref = _generated_ref(HTML.encode("utf-8"))
    ctx.add(ref)
    executor.file_context = ctx

    result = await _call(
        executor,
        "edit_file_content",
        file_id=ref.file_id,
        edits=[{"find": "Sean has not accepted.", "replace": "Sean accepted."}],
    )

    assert result.is_error is False, result.content
    assert (
        f"It supersedes the previous revision (file_id={ref.file_id}), which is kept unchanged."
    ) in result.content


# ── The prompt tells the truth per origin ─────────────────────────────────


INLINED_HEADER = "[Attached to the user's message(s)]"
BY_REFERENCE_HEADER = "[By reference only"


def _ref(file_id: str, filename: str, source: str | None, content_type: str = "text/csv") -> FileRef:
    return FileRef(
        file_id=file_id,
        filename=filename,
        content_type=content_type,
        size=1,
        s3_object_name=file_id,
        source=source,
    )


def test_prompt_section_separates_generated_files_from_uploads():
    ctx = FileContext()
    ctx.add(
        _ref(
            "up-1",
            "notes.docx",
            "chat",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    )
    ctx.add(_ref("gen-1", "briefing.html", "agent_download", "text/html"))

    # No word from the caller: provenance decides - uploads are on a user
    # message and inlined, a generated file hangs off an assistant message.
    prompt = ctx.to_prompt_section()

    inlined_block = prompt.split(INLINED_HEADER, 1)[1].split(BY_REFERENCE_HEADER, 1)[0]
    reference_block = prompt.split(BY_REFERENCE_HEADER, 1)[1]
    assert "up-1" in inlined_block and "gen-1" not in inlined_block
    assert "gen-1" in reference_block.split("\nThe contents", 1)[0]
    # Uploads: content is in front of the model.  Generated: it is NOT, and
    # the way to it is named - never "described from memory".
    assert "are ALREADY INCLUDED" in prompt
    assert "[By reference only] files are NOT in your context" in prompt
    assert "read_file_content" in prompt
    assert "edit_file_content" in prompt
    assert "never regenerate a whole file to change a line" in prompt


def test_prompt_section_with_only_generated_files_never_claims_inclusion():
    ctx = FileContext()
    ctx.add(_ref("gen-1", "export.csv", "agent_working"))
    prompt = ctx.to_prompt_section()
    assert "ALREADY INCLUDED" not in prompt
    assert INLINED_HEADER not in prompt
    assert BY_REFERENCE_HEADER in prompt
    assert "read_file_content" in prompt


def test_prompt_section_keeps_the_current_turn_split_for_inlined_files_only():
    ctx = FileContext()
    ctx.add(_ref("old", "a.csv", "chat"))
    ctx.add(_ref("new", "b.csv", "chat"))
    ctx.add(_ref("gen", "out.csv", "agent_working"))

    prompt = ctx.to_prompt_section(current_turn_file_ids={"new"})
    just_attached = prompt.split("[Just attached", 1)[1].split("[Earlier in this conversation", 1)[0]
    earlier = prompt.split("[Earlier in this conversation", 1)[1].split(BY_REFERENCE_HEADER, 1)[0]
    by_reference = prompt.split(BY_REFERENCE_HEADER, 1)[1]
    assert 'file_id="new"' in just_attached
    assert 'file_id="old"' not in just_attached and 'file_id="gen"' not in just_attached
    assert 'file_id="old"' in earlier and 'file_id="gen"' not in earlier
    assert 'file_id="gen"' in by_reference


def test_prompt_section_trusts_the_caller_over_provenance():
    """An upload handed to a run by id is NOT inlined - the caller says so.

    A delegated task or an assignment run receives the user's uploads as
    bare ids on its FileContext; no user message carries them, so no
    provider inlines them. Claiming they were "already included" left the
    agent unable to read a file it could have read with the tool.
    """
    ctx = FileContext()
    ctx.add(_ref("up-1", "notes.csv", "chat"))
    ctx.add(_ref("up-2", "more.csv", "chat"))
    ctx.add(_ref("gen-1", "out.csv", "agent_working"))

    # Nothing inlined: every file is by reference, nothing is "included".
    nothing = ctx.to_prompt_section(inlined_file_ids=set())
    assert "ALREADY INCLUDED" not in nothing
    assert INLINED_HEADER not in nothing
    reference_block = nothing.split(BY_REFERENCE_HEADER, 1)[1]
    assert 'file_id="up-1"' in reference_block
    assert 'file_id="up-2"' in reference_block
    assert 'file_id="gen-1"' in reference_block

    # One upload is on a user message (the ``file-`` spelling is accepted),
    # the other travelled by id: only the first is inlined, whatever the
    # provenance of either says.
    partial = ctx.to_prompt_section(inlined_file_ids={"file-up-1"})
    inlined_block = partial.split(INLINED_HEADER, 1)[1].split(BY_REFERENCE_HEADER, 1)[0]
    reference_block = partial.split(BY_REFERENCE_HEADER, 1)[1]
    assert 'file_id="up-1"' in inlined_block
    assert 'file_id="up-2"' not in inlined_block
    assert 'file_id="up-2"' in reference_block and 'file_id="gen-1"' in reference_block
    assert "listed above the [By reference only] section" in partial

    # The caller can also vouch for a generated file it re-attached to a
    # user message - provenance is only the fallback.
    vouched = ctx.to_prompt_section(inlined_file_ids={"up-1", "up-2", "gen-1"})
    assert BY_REFERENCE_HEADER not in vouched
    assert INLINED_HEADER not in vouched  # a single group needs no header
    assert "The contents of the files listed above are ALREADY INCLUDED" in vouched


def _module_source(module: str) -> str:
    """The source of *module* WITHOUT importing it (a tripwire reads code)."""
    spec = importlib.util.find_spec(module)
    assert spec is not None and spec.origin, module
    return Path(spec.origin).read_text(encoding="utf-8")


def _calls_named(tree: ast.AST, name: str) -> list[ast.Call]:
    found: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        attr = func.attr if isinstance(func, ast.Attribute) else None
        ident = func.id if isinstance(func, ast.Name) else None
        if name in (attr, ident):
            found.append(node)
    return found


def _keywords(call: ast.Call) -> dict[str, ast.expr]:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


@pytest.mark.parametrize(
    "module",
    ["app.services.agent_chat", "app.services.general_chat", "app.services.assignment_engine"],
)
def test_every_prompt_section_caller_says_what_is_inlined(module):
    """The prompt's "already included" claim is only as true as its caller.

    Every production caller of ``to_prompt_section`` passes
    ``inlined_file_ids`` - the provenance fallback is for tests and
    ad-hoc callers, never for a path that puts the section in front of a
    model.
    """
    source = _module_source(module)
    calls = _calls_named(ast.parse(source), "to_prompt_section")
    assert calls, f"{module} no longer calls to_prompt_section"
    for call in calls:
        assert "inlined_file_ids" in _keywords(call), (
            f"{module}: to_prompt_section() at line {call.lineno} does not say "
            "which files are inlined"
        )


def test_assignment_engine_attaches_the_trigger_files_it_lists():
    """The run's user message carries the trigger files (``files=``).

    The engine tells the model the trigger files are in front of it, so
    they must ride on the user message the providers inline - the flow
    runner and the evaluation engine already do this; the assignment
    engine listed bare ids under an "already included" claim.
    """
    source = _module_source("app.services.assignment_engine")
    user_messages = [
        call
        for call in _calls_named(ast.parse(source), "ChatMessage")
        if isinstance(_keywords(call).get("role"), ast.Constant)
        and _keywords(call)["role"].value == "user"
        and isinstance(_keywords(call).get("content"), ast.Name)
        and _keywords(call)["content"].id == "rendered_prompt"
    ]
    assert len(user_messages) == 1, "the run's user message is built in one place"
    assert "files" in _keywords(user_messages[0]), (
        "the trigger files must be attached to the run's user message"
    )


def test_inlined_user_message_file_ids_reads_user_messages_only():
    from app.llm.base import ChatMessage
    from app.tools.base import inlined_user_message_file_ids

    upload = _ref("up-1", "a.csv", "chat")
    generated = _ref("gen-1", "out.csv", "agent_working")
    history = [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="here", files=[upload]),
        # A generated file rides on the assistant message as metadata only.
        ChatMessage(role="assistant", content="[[FILE:gen-1]]", files=[generated]),
        ChatMessage(role="user", content="and now?", files=None),
        SimpleNamespace(role="user", files=[SimpleNamespace(file_id=None)]),
    ]
    assert inlined_user_message_file_ids(history) == {"up-1"}
    assert inlined_user_message_file_ids([]) == set()
    assert inlined_user_message_file_ids(None) == set()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (None, False),
        ("chat", False),
        ("assignment", False),
        ("public_chat", False),
        ("agent", True),
        ("agent_generated", True),
        ("agent_working", True),
        ("agent_download", True),
        ("mcp", True),
        (" Agent_Download ", True),
        # A Runner capture (screenshot / sidecar) is written by the platform
        # on the assistant side of the transcript - never inlined either.
        ("runner_capture", True),
        ("runner_capture_sidecar", True),
    ],
)
def test_file_ref_is_agent_generated(source, expected):
    ref = FileRef(file_id="x", filename="x", content_type="text/plain", size=1, s3_object_name="x", source=source)
    assert ref.is_agent_generated is expected


def test_file_ref_from_attachment_carries_source():
    att = SimpleNamespace(
        id=uuid.uuid4(),
        filename="f.txt",
        content_type="text/plain",
        size=1,
        s3_object_name="k",
        source="agent_working",
    )
    assert FileRef.from_attachment(att).source == "agent_working"
    legacy = SimpleNamespace(id=uuid.uuid4(), filename="f", content_type="text/plain", size=1, s3_object_name="k")
    assert FileRef.from_attachment(legacy).source is None
