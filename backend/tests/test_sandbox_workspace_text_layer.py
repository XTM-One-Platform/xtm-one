"""``to_workspace_path`` writes a PDF or Office document's text layer beside it.

The sandbox has no PDF library, so a document written to the workspace as raw
bytes alone could not be searched in code. The ingestion extracts the text on
the server with the platform's reader: the raw file at its path and the text at
``<path>.txt``, or the text alone when the target ends in ``.txt``. Synthetic
fixtures only: hand-written PDFs, an in-memory workspace store.
"""

from __future__ import annotations

import re
import uuid

from app.integrations.file_delivery import deliver_file
from app.tools.base import WORKSPACE_PATH_ARGUMENT, CallableToolExecutor, ToolCall, ToolDefinition, ToolResult
from tests.kb_figure_pdfs import SCANNED_PAGE, build_pdf
from tests.test_sandbox_workspace_tools import _composite, _workspace_bytes, store  # noqa: F401

PHRASE = "Ransomware incidents rose by forty percent in the second half."
REPORT = build_pdf([[("text", "Survey of the year, page one.")], [("text", PHRASE)]])


async def _never(*_a):
    raise AssertionError("bound for the workspace: not stored as a conversation file")


def _download_tool(data: bytes, filename: str, content_type: str) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    executor = CallableToolExecutor()

    async def download_report() -> ToolResult:
        return await deliver_file(
            data=data, filename=filename, content_type=content_type, source="Fixture", persist_file_fn=_never
        )

    executor.register("download_report", download_report)
    return [
        ToolDefinition(
            name="download_report",
            description="d",
            input_schema={"type": "object", "properties": {}},
            workspace_ingest=True,
        )
    ], executor


async def _ingest(
    target: str, data: bytes = REPORT, filename: str = "survey.pdf", content_type: str = "application/pdf"
):
    conversation, user = uuid.uuid4(), uuid.uuid4()
    composite, _ = _composite(conversation, user)
    composite.add_tools(*_download_tool(data, filename, content_type))
    result = await composite.execute_tool(
        ToolCall(id="c1", name="download_report", arguments={WORKSPACE_PATH_ARGUMENT: target})
    )
    assert not result.is_error, result.content
    return conversation, result


async def test_a_pdf_lands_raw_with_its_searchable_text_beside_it(store):  # noqa: F811
    conversation, result = await _ingest("reports/survey.pdf")
    assert await _workspace_bytes(store, conversation, "reports/survey.pdf") == REPORT
    text = (await _workspace_bytes(store, conversation, "reports/survey.pdf.txt")).decode("utf-8")
    assert re.search(r"forty percent in the second half", text)
    assert "workspace/reports/survey.pdf (" in result.content and "workspace/reports/survey.pdf.txt (" in result.content
    assert (
        "Open them in code_interpreter as workspace/reports/survey.pdf and workspace/reports/survey.pdf.txt."
        in result.content
    )
    # Written instead of returned: the text is not echoed in the answer.
    assert PHRASE not in result.content


async def test_a_txt_target_receives_the_text_alone(store):  # noqa: F811
    conversation, result = await _ingest("reports/survey.txt")
    text = (await _workspace_bytes(store, conversation, "reports/survey.txt")).decode("utf-8")
    assert PHRASE in text and not text.startswith("%PDF")
    assert "reports/survey.txt.txt" not in store.files[next(iter(store.files))]
    assert "holds the text layer of the document survey.pdf, not its raw bytes" in result.content


async def test_a_document_named_without_extension_is_read_by_its_type(store):  # noqa: F811
    conversation, _result = await _ingest("in/report", filename="download", content_type="application/pdf")
    assert await _workspace_bytes(store, conversation, "in/report") == REPORT
    assert PHRASE in (await _workspace_bytes(store, conversation, "in/report.txt")).decode("utf-8")


async def test_a_scanned_pdf_keeps_its_raw_bytes_and_says_there_is_no_text(store):  # noqa: F811
    scanned = build_pdf([SCANNED_PAGE])
    conversation, result = await _ingest("scan.txt", data=scanned, filename="scan.pdf")
    assert await _workspace_bytes(store, conversation, "scan.txt") == scanned
    assert "No text layer could be extracted from workspace/scan.txt" in result.content


async def test_a_file_that_is_not_a_document_gets_no_text_layer(store):  # noqa: F811
    conversation, result = await _ingest(
        "data/blob.bin", data=b"\x00binary", filename="blob.bin", content_type="application/octet-stream"
    )
    assert await _workspace_bytes(store, conversation, "data/blob.bin") == b"\x00binary"
    assert list(store.files[next(iter(store.files))]) == ["data/blob.bin"]
    assert "Open it in code_interpreter as workspace/data/blob.bin." in result.content
