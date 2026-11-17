"""Tests for tool-generated file persistence (#SCREENSHOT-FIX / sandbox files).

Two related gaps are covered:

1. The headless browser ``screenshot`` action used to return only a truncated
   100-char base64 preview, so the capture was silently discarded — the agent
   could never actually take a usable screenshot. It now persists the PNG as a
   durable ``FileAttachment`` and returns an inline ``![](…)`` Markdown image.

2. Files produced by the code interpreter (matplotlib charts AND files the
   code writes to disk) were not persisted as conversation files: charts used
   an ephemeral presigned URL with no ``FileAttachment``, and disk files were
   deleted with the sandbox temp dir. They now persist through the same
   ``_persist_generated_file`` pipeline as uploads / ``generate_file``.
"""

from __future__ import annotations

import base64
import os
import tempfile
import uuid
from types import SimpleNamespace

import pytest

from app.services.builtin_tools import (
    build_browser_tool,
    build_code_interpreter_tool,
)
from app.tools.base import FileContext, ToolCall, ToolResult


def _fake_attachment(filename: str, content_type: str, size: int = 10) -> SimpleNamespace:
    """A stand-in for a persisted ``FileAttachment`` ORM row."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        filename=filename,
        content_type=content_type,
        size=size,
        s3_object_name=f"chat/conv/{filename}",
    )


# ──────────────────────────────────────────────────────────────────────────
# Browser screenshot
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_browser_screenshot_persists_and_returns_inline_image(monkeypatch):
    """The screenshot action persists the PNG and returns an inline image."""
    created = _fake_attachment("screenshot_abcd1234.png", "image/png", size=1234)

    async def _fake_screenshot(_session_key, full_page=False, selector=None):
        return b"\x89PNG\r\n\x1a\n fake-png-bytes"

    async def _fake_persist(**kwargs):
        # The browser tool must persist as a real image attachment.
        assert kwargs["content_type"] == "image/png"
        assert kwargs["filename"].endswith(".png")
        assert kwargs["raw_bytes"] == b"\x89PNG\r\n\x1a\n fake-png-bytes"
        return created

    monkeypatch.setattr(
        "app.services.browser_tool.browser_screenshot", _fake_screenshot
    )
    monkeypatch.setattr(
        "app.services.builtin_tools._shared._persist_generated_file", _fake_persist
    )

    defs, executor = build_browser_tool(
        "agent:conv",
        db=object(),
        user_id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
    )
    assert defs and defs[0].name == "browser"

    ctx = FileContext()
    executor.file_context = ctx
    result = await executor.execute_tool(
        ToolCall(id="call-1", name="browser", arguments={"action": "screenshot"})
    )

    assert isinstance(result, ToolResult)
    assert result.is_error is False
    assert result.files and result.files[0].file_id == str(created.id)
    # The PNG is referenced inline via an authenticated download URL so the
    # markdown renderer shows the image (same contract as generate_image).
    assert f"![Screenshot](/api/v1/chat/files/{created.id}/download)" in result.content
    # And it is registered on the file context → attached to the conversation.
    assert ctx.get(str(created.id)) is not None


@pytest.mark.asyncio
async def test_browser_screenshot_show_to_user_false_drops_the_markdown_handover(monkeypatch):
    """A working capture the agent will place itself: the result keeps
    the pixels, the file id and the visual summary, and carries NO "include
    this EXACT Markdown" instruction to paste into the reply."""
    created = _fake_attachment("screenshot_abcd1234.png", "image/png", size=1234)

    async def _fake_screenshot(_session_key, full_page=False, selector=None):
        return b"\x89PNG\r\n\x1a\n fake-png-bytes"

    async def _fake_persist(**kwargs):
        return created

    monkeypatch.setattr("app.services.browser_tool.browser_screenshot", _fake_screenshot)
    monkeypatch.setattr("app.services.builtin_tools._shared._persist_generated_file", _fake_persist)

    defs, executor = build_browser_tool(
        "agent:conv", db=object(), user_id=str(uuid.uuid4()), conversation_id=str(uuid.uuid4())
    )
    assert defs[0].input_schema["properties"]["show_to_user"]["default"] is True
    executor.file_context = FileContext()
    working = await executor.execute_tool(
        ToolCall(id="call-1", name="browser", arguments={"action": "screenshot", "show_to_user": False})
    )
    assert working.files and working.files[0].file_id == str(created.id)
    assert "EXACT Markdown" not in working.content
    assert "![Screenshot]" not in working.content
    assert f"file_id {created.id}" in working.content
    assert "Visual summary:" in working.content

    shown = await executor.execute_tool(
        ToolCall(id="call-2", name="browser", arguments={"action": "screenshot"})
    )
    assert "EXACT Markdown" in shown.content
    assert f"![Screenshot](/api/v1/chat/files/{created.id}/download)" in shown.content


def test_evaluate_results_are_cut_at_twenty_kb_and_a_base64_image_is_named():
    """A ``canvas.toDataURL()`` result is not readable text: the cut
    says how much was dropped and points at the tools that carry an image as a
    file; a long text result is cut with the plain advice."""
    from app.services.browser_tool import MAX_EVALUATE_RESULT_CHARS, format_evaluate_result

    short = format_evaluate_result({"ok": True})
    assert short == "JavaScript result:\n{'ok': True}"

    blob = "data:image/png;base64," + "A" * 200_000
    cut = format_evaluate_result(blob)
    assert len(cut) < MAX_EVALUATE_RESULT_CHARS + 600
    assert cut.startswith("JavaScript result:\ndata:image/png;base64,")
    assert f"limit {MAX_EVALUATE_RESULT_CHARS:,}" in cut
    assert "180,022 more characters were dropped" in cut
    assert "fetch_image_asset(url=...)" in cut
    assert "screenshot action" in cut

    text = "x" * 30_000
    cut_text = format_evaluate_result(text)
    assert "Return less from the script" in cut_text
    assert "fetch_image_asset" not in cut_text


@pytest.mark.asyncio
async def test_browser_screenshot_without_storage_context_degrades_gracefully(
    monkeypatch,
):
    """With no db/user context the action must not crash or dump base64."""

    async def _fake_screenshot(_session_key, full_page=False, selector=None):
        return b"x" * 4096

    monkeypatch.setattr(
        "app.services.browser_tool.browser_screenshot", _fake_screenshot
    )

    _defs, executor = build_browser_tool("agent:conv")  # no db / user_id
    result = await executor.execute_tool(
        ToolCall(id="call-2", name="browser", arguments={"action": "screenshot"})
    )

    assert result.is_error is False
    assert result.files is None
    assert "could not be saved" in result.content
    # Must NOT leak a raw/truncated base64 payload to the model.
    assert "base64" not in result.content.lower()
    # Bytes that are not a decodable image still get an honest summary line.
    assert "Visual summary: unavailable" in result.content


def _diagram_png() -> bytes:
    """White paper, a Filigran-blue card and a small cyan mark."""
    import io

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (800, 450), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([50, 50, 249, 149], fill="#0015A8")
    draw.rectangle([740, 400, 769, 429], fill="#0FBCFF")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_browser_screenshot_result_carries_the_visual_summary(monkeypatch):
    """The screenshot result names the colours the PNG contains, as text that
    outlives the vision block - an unexpected hex is checkable without pixels."""
    created = _fake_attachment("screenshot_cafe0000.png", "image/png", size=1234)
    png = _diagram_png()

    async def _fake_screenshot(_session_key, full_page=False, selector=None):
        return png

    async def _fake_persist(**kwargs):
        return created

    monkeypatch.setattr(
        "app.services.browser_tool.browser_screenshot", _fake_screenshot
    )
    monkeypatch.setattr(
        "app.services.builtin_tools._shared._persist_generated_file", _fake_persist
    )

    _defs, executor = build_browser_tool(
        "agent:conv",
        db=object(),
        user_id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
    )
    result = await executor.execute_tool(
        ToolCall(id="call-vs", name="browser", arguments={"action": "screenshot"})
    )

    assert isinstance(result, ToolResult)
    assert result.is_error is False
    assert f"![Screenshot](/api/v1/chat/files/{created.id}/download)" in result.content
    assert "Visual summary: 800x450 px; dominant #FFFFFF" in result.content
    # The blue card and the stray cyan mark, largest share first.
    assert "#0015A8 5.6%" in result.content
    assert "#0FBCFF 0.25%" in result.content
    assert "outlives the pixels" in result.content


@pytest.mark.asyncio
async def test_browser_screenshot_summary_rides_the_no_storage_path_too(monkeypatch):
    """Without storage the agent gets no pixels at all - the summary is then
    the only thing it can check the rendering against."""

    async def _fake_screenshot(_session_key, full_page=False, selector=None):
        return _diagram_png()

    monkeypatch.setattr(
        "app.services.browser_tool.browser_screenshot", _fake_screenshot
    )
    _defs, executor = build_browser_tool("agent:conv")  # no db / user_id
    result = await executor.execute_tool(
        ToolCall(id="call-vs2", name="browser", arguments={"action": "screenshot"})
    )
    assert result.is_error is False
    assert "could not be saved" in result.content
    assert "Visual summary: 800x450 px" in result.content
    assert "#0FBCFF" in result.content


@pytest.mark.asyncio
async def test_browser_screenshot_persists_for_company_managed_run(monkeypatch):
    """A company-managed assignment run has no file owner (user_id="") but a
    run scope — the screenshot must still persist (regression for the
    over-strict ``not user_id`` guard)."""
    created = _fake_attachment("screenshot_deadbeef.png", "image/png", size=99)

    async def _fake_screenshot(_session_key, full_page=False, selector=None):
        return b"\x89PNG\r\n\x1a\n bytes"

    captured: dict = {}

    async def _fake_persist(**kwargs):
        captured.update(kwargs)
        return created

    monkeypatch.setattr(
        "app.services.browser_tool.browser_screenshot", _fake_screenshot
    )
    monkeypatch.setattr(
        "app.services.builtin_tools._shared._persist_generated_file", _fake_persist
    )

    # No owner (company-managed run) — only an assignment_run_id scope.
    _defs, executor = build_browser_tool(
        "agent:assignment:run",
        db=object(),
        user_id="",
        assignment_run_id=str(uuid.uuid4()),
    )
    result = await executor.execute_tool(
        ToolCall(id="call-cm1", name="browser", arguments={"action": "screenshot"})
    )

    assert isinstance(result, ToolResult)
    assert result.is_error is False
    assert result.files and result.files[0].file_id == str(created.id)
    assert f"![Screenshot](/api/v1/chat/files/{created.id}/download)" in result.content
    # A NULL owner is stamped (safe_uuid("") -> None) while the run scope links
    # the file to the assignment run.
    assert captured["user_id"] == ""
    assert captured["assignment_run_id"]


# ──────────────────────────────────────────────────────────────────────────
# Code interpreter — disk output files
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_code_interpreter_persists_output_files(monkeypatch):
    """Files the sandbox wrote to disk are persisted + surfaced via markers."""
    created = _fake_attachment("report.csv", "text/csv", size=7)

    async def _fake_execute(_code, timeout=300, user_id=None, quota_user_id=None, db=None):
        return {
            "stdout": "done",
            "stderr": "",
            "error": None,
            "images": [],
            "output_files": [
                {
                    "filename": "report.csv",
                    "data_base64": base64.b64encode(b"a,b\n1,2").decode("ascii"),
                    "content_type": "text/csv",
                    "size": 7,
                }
            ],
        }

    captured: dict = {}

    async def _fake_persist(**kwargs):
        captured.update(kwargs)
        return created

    monkeypatch.setattr("app.services.code_interpreter.execute_python", _fake_execute)
    monkeypatch.setattr(
        "app.services.builtin_tools._shared._persist_generated_file", _fake_persist
    )

    _defs, executor = build_code_interpreter_tool(
        db=object(),
        user_id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
    )

    ctx = FileContext()
    executor.file_context = ctx
    result = await executor.execute_tool(
        ToolCall(
            id="call-3",
            name="code_interpreter",
            arguments={"code": "open('report.csv','w').write('a,b\\n1,2')"},
        )
    )

    assert isinstance(result, ToolResult)
    assert result.is_error is False
    assert result.files and result.files[0].filename == "report.csv"
    assert f"[[FILE:{created.id}]]" in result.content
    assert ctx.get(str(created.id)) is not None
    # The raw bytes (not a re-authored copy) were persisted.
    assert captured["raw_bytes"] == b"a,b\n1,2"
    assert captured["source"] == "agent_generated"


@pytest.mark.asyncio
async def test_code_interpreter_persists_for_company_managed_run(monkeypatch):
    """Disk files must persist for a company-managed assignment run that has a
    run scope but no file owner (user_id="") — regression for the over-strict
    ``not user_id`` guard."""
    created = _fake_attachment("out.csv", "text/csv", size=3)

    async def _fake_execute(_code, timeout=300, user_id=None, quota_user_id=None, db=None):
        return {
            "stdout": "",
            "stderr": "",
            "error": None,
            "images": [],
            "output_files": [
                {
                    "filename": "out.csv",
                    "data_base64": base64.b64encode(b"a,b").decode("ascii"),
                    "content_type": "text/csv",
                    "size": 3,
                }
            ],
        }

    captured: dict = {}

    async def _fake_persist(**kwargs):
        captured.update(kwargs)
        return created

    monkeypatch.setattr("app.services.code_interpreter.execute_python", _fake_execute)
    monkeypatch.setattr(
        "app.services.builtin_tools._shared._persist_generated_file", _fake_persist
    )

    # No owner (company-managed run) — only an assignment_run_id scope.
    _defs, executor = build_code_interpreter_tool(
        db=object(),
        user_id="",
        assignment_run_id=str(uuid.uuid4()),
    )
    ctx = FileContext()
    executor.file_context = ctx
    result = await executor.execute_tool(
        ToolCall(
            id="call-cm2",
            name="code_interpreter",
            arguments={"code": "open('out.csv','w').write('a,b')"},
        )
    )

    assert isinstance(result, ToolResult)
    assert result.files and result.files[0].filename == "out.csv"
    assert f"[[FILE:{created.id}]]" in result.content
    # A NULL owner is stamped (safe_uuid("") -> None) while the run scope links
    # the file to the assignment run.
    assert captured["user_id"] == ""
    assert captured["assignment_run_id"]


@pytest.mark.asyncio
async def test_code_interpreter_without_storage_returns_plain_text(monkeypatch):
    """No persistence context → plain text result (back-compat), charts to sink."""

    async def _fake_execute(_code, timeout=300, user_id=None, quota_user_id=None, db=None):
        return {
            "stdout": "hello",
            "stderr": "",
            "error": None,
            "images": ["Zm9v"],  # base64 'foo'
            "output_files": [
                {
                    "filename": "x.csv",
                    "data_base64": base64.b64encode(b"x").decode("ascii"),
                    "content_type": "text/csv",
                    "size": 1,
                }
            ],
        }

    monkeypatch.setattr("app.services.code_interpreter.execute_python", _fake_execute)

    sink: list[str] = []
    _defs, executor = build_code_interpreter_tool(image_sink=sink)
    result = await executor.execute_tool(
        ToolCall(id="call-4", name="code_interpreter", arguments={"code": "print(1)"})
    )

    # Falls back to a plain string (no FileAttachment persistence available).
    assert result.is_error is False
    assert result.files is None
    assert "[[FILE:" not in result.content
    # Charts still flow to the image sink for inject_code_images.
    assert sink == ["Zm9v"]


# ──────────────────────────────────────────────────────────────────────────
# Code interpreter — chart persistence (inject_code_images)
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_inject_code_images_persists_charts_as_attachments(monkeypatch):
    """Charts get a durable /files/{id}/download URL (not an ephemeral one)."""
    from app.services.code_interpreter import inject_code_images

    created = _fake_attachment("chart_deadbeef.png", "image/png", size=20)

    async def _fake_persist(**kwargs):
        assert kwargs["content_type"] == "image/png"
        return created

    monkeypatch.setattr(
        "app.services.builtin_tools._shared._persist_generated_file", _fake_persist
    )

    out = await inject_code_images(
        "Here is the chart:",
        [base64.b64encode(b"png-bytes").decode("ascii")],
        db=object(),
        user_id=str(uuid.uuid4()),
        conversation_id=str(uuid.uuid4()),
    )

    assert "Here is the chart:" in out
    assert f"![Figure 1](/api/v1/chat/files/{created.id}/download)" in out
    # The legacy ephemeral presigned ``charts/`` path must not be used.
    assert "charts/" not in out


@pytest.mark.asyncio
async def test_inject_code_images_noop_without_images():
    from app.services.code_interpreter import inject_code_images

    assert await inject_code_images("text", []) == "text"


# ──────────────────────────────────────────────────────────────────────────
# Disk-file capture helper
# ──────────────────────────────────────────────────────────────────────────


def test_collect_output_files_excludes_scaffolding_and_reads_content():
    from app.services.code_interpreter import _collect_output_files

    with tempfile.TemporaryDirectory() as tmp:
        code_file = os.path.join(tmp, "user_code.py")
        wrapper_file = os.path.join(tmp, "_sandbox_wrapper.py")
        out_file = os.path.join(tmp, "result.csv")
        for path, content in (
            (code_file, b"# user code"),
            (wrapper_file, b"# wrapper"),
            (out_file, b"a,b\n1,2\n"),
        ):
            with open(path, "wb") as fh:
                fh.write(content)

        files = _collect_output_files(tmp, exclude={code_file, wrapper_file})

    names = {f["filename"] for f in files}
    assert names == {"result.csv"}
    entry = files[0]
    assert base64.b64decode(entry["data_base64"]) == b"a,b\n1,2\n"
    # The exact CSV MIME differs per OS (Windows' registry maps .csv to
    # ``application/vnd.ms-excel``), so accept either canonical form.
    assert entry["content_type"] in {"text/csv", "application/vnd.ms-excel"}


def test_collect_output_files_respects_file_count_cap():
    from app.services.code_interpreter import MAX_OUTPUT_FILES, _collect_output_files

    with tempfile.TemporaryDirectory() as tmp:
        for i in range(MAX_OUTPUT_FILES + 5):
            with open(os.path.join(tmp, f"f{i:03d}.txt"), "wb") as fh:
                fh.write(b"data")
        files = _collect_output_files(tmp, exclude=set())

    assert len(files) == MAX_OUTPUT_FILES


def test_collect_output_files_skips_oversized_files():
    from app.services.code_interpreter import MAX_OUTPUT_FILE_BYTES, _collect_output_files

    with tempfile.TemporaryDirectory() as tmp:
        small = os.path.join(tmp, "small.bin")
        big = os.path.join(tmp, "big.bin")
        with open(small, "wb") as fh:
            fh.write(b"ok")
        with open(big, "wb") as fh:
            fh.write(b"0" * (MAX_OUTPUT_FILE_BYTES + 1))
        files = _collect_output_files(tmp, exclude=set())

    names = {f["filename"] for f in files}
    assert names == {"small.bin"}
