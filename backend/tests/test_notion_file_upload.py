"""Tests for the Notion ``upload_notion_file`` tool (Direct Upload API).

Covers the full create -> send -> attach flow for:
  - single-part uploads (<= 20 MB) attached as a page block,
  - multi-part uploads (> 20 MB) split into chunks + completed,
  - ``external_url`` imports attached to a database ``files`` property
    (immediate + pending->uploaded polling),
  - block-type auto-detection from the content type,
  - and the validation / guard rails (missing page_id, mutually exclusive
    sources, missing file, oversize, non-HTTPS external URL).

The HTTP layer is mocked at ``notion._common.safe_request`` (what the shared
``NotionContext.request`` wrapper delegates to), so each test drives a
deterministic sequence of API responses.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

import app.integrations.providers.notion as notion
from app.llm.base import FileRef
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import FileContext, ToolCall
from tests.integration_file_fakes import HOSTILE_FILE_NAMES, assert_name_only_inside, fenced_and_open


def _resp(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def _ok(payload: dict) -> tuple[httpx.Response, None]:
    return (_resp(payload), None)


def _executor_with_file(
    data: bytes, filename: str, content_type: str, file_id: str = "file-1"
) -> _SafeIntegrationExecutor:
    """Build an executor whose FileContext holds one in-memory file."""
    executor = _SafeIntegrationExecutor()
    notion.build_tools("dummy-token", executor)
    fc = FileContext()
    fc.add(FileRef.from_bytes(data, filename, content_type, file_id=file_id))
    executor.file_context = fc
    return executor


# ── Schema ──────────────────────────────────────────────────────────────


def test_upload_notion_file_schema():
    executor = _SafeIntegrationExecutor()
    definitions = notion.build_tools("dummy-token", executor)
    tool = next(t for t in definitions if t.name == "upload_notion_file")

    props = tool.input_schema["properties"]
    assert tool.input_schema["required"] == ["page_id"]
    for key in (
        "page_id",
        "file_id",
        "external_url",
        "property_name",
        "caption",
        "filename",
        "block_type",
        "attach_as",
    ):
        assert key in props
    assert props["block_type"]["enum"] == ["auto", "file", "pdf", "image", "video", "audio"]
    assert props["attach_as"]["enum"] == ["block", "page_icon", "page_cover"]
    assert tool.source == "integration:notion"


# ── Block-type detection (pure helper) ──────────────────────────────────


@pytest.mark.parametrize(
    ("mime", "expected"),
    [
        ("application/pdf", "pdf"),
        ("image/png", "image"),
        ("image/jpeg", "image"),
        ("video/mp4", "video"),
        ("audio/mpeg", "audio"),
        ("text/csv", "file"),
        ("application/octet-stream", "file"),
        ("", "file"),
        ("application/pdf; charset=binary", "pdf"),
    ],
)
def test_notion_file_block_type(mime, expected):
    assert notion._notion_file_block_type(mime) == expected


# ── A forwarded file's stored name reaches the model only as untrusted data


@pytest.mark.asyncio
@pytest.mark.parametrize("hostile", list(HOSTILE_FILE_NAMES))
@pytest.mark.parametrize("attached", [True, False])
async def test_an_upload_names_the_file_by_its_id_and_fences_its_name(hostile, attached):
    name = HOSTILE_FILE_NAMES[hostile]
    executor = _executor_with_file(b"%PDF-1.4 fake", name, "application/pdf")

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-1", "status": "pending", "upload_url": "https://files.notion.so/upload/fu-1/send"}),
            _ok({"id": "fu-1", "status": "uploaded"}),
            _ok({"results": [{"id": "block-1"}]})
            if attached
            else (None, "Error: Notion resource not found while trying to attach file block."),
        ]
        result = await executor.execute_tool(
            ToolCall(id="c1", name="upload_notion_file", arguments={"page_id": "page-1", "file_id": "file-1"})
        )

    assert result.is_error is not attached
    outside = assert_name_only_inside(result.content, name)
    if attached:
        assert outside.startswith("Uploaded file file-1 (0.0 KB) to Notion and attached it to the page body")
    else:
        assert outside.startswith("Uploaded file file-1 to Notion (file id fu-1) but could not attach it")
    assert mock_request.call_args_list[0].kwargs["json"]["filename"] == name


# ── Single-part upload + page block attach ──────────────────────────────


@pytest.mark.asyncio
async def test_single_part_upload_attaches_pdf_block():
    executor = _executor_with_file(b"%PDF-1.4 fake", "report.pdf", "application/pdf")

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok(
                {
                    "id": "fu-1",
                    "status": "pending",
                    "upload_url": "https://files.notion.so/upload/fu-1/send",
                }
            ),
            _ok({"id": "fu-1", "status": "uploaded"}),
            _ok({"results": [{"id": "block-1"}]}),
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="c1",
                name="upload_notion_file",
                arguments={"page_id": "page-1", "file_id": "file-1"},
            )
        )

    assert not result.is_error, result.content
    inside, outside = fenced_and_open(result.content)
    assert outside.startswith("Uploaded file file-1 (0.0 KB) to Notion")
    assert "report.pdf" not in outside and "Name: report.pdf" in inside.splitlines()
    assert mock_request.call_count == 3

    create_call, send_call, attach_call = mock_request.call_args_list
    # 1) create
    assert create_call.args[1] == "post"
    assert create_call.args[2].endswith("/file_uploads")
    assert create_call.kwargs["json"]["mode"] == "single_part"
    assert create_call.kwargs["json"]["content_type"] == "application/pdf"
    # 2) send — uses upload_url, multipart, NO json content-type header
    assert send_call.args[2] == "https://files.notion.so/upload/fu-1/send"
    sent_files = send_call.kwargs["files"]
    assert sent_files["file"][0] == "report.pdf"
    assert sent_files["file"][1] == b"%PDF-1.4 fake"
    assert sent_files["file"][2] == "application/pdf"
    assert "Content-Type" not in send_call.kwargs["headers"]
    assert "part_number" not in send_call.kwargs["data"]
    # 3) attach — pdf block referencing the file_upload id
    assert attach_call.args[1] == "patch"
    assert attach_call.args[2].endswith("/blocks/page-1/children")
    block = attach_call.kwargs["json"]["children"][0]
    assert block["type"] == "pdf"
    assert block["pdf"]["type"] == "file_upload"
    assert block["pdf"]["file_upload"]["id"] == "fu-1"


@pytest.mark.asyncio
async def test_single_part_image_block_with_caption_and_no_name():
    executor = _executor_with_file(b"\x89PNG fake", "diagram.png", "image/png")

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-2", "status": "pending"}),
            _ok({"id": "fu-2", "status": "uploaded"}),
            _ok({"results": []}),
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="c2",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-9",
                    "file_id": "file-1",
                    "caption": "Architecture diagram",
                },
            )
        )

    assert not result.is_error, result.content
    block = mock_request.call_args_list[-1].kwargs["json"]["children"][0]
    assert block["type"] == "image"
    # image blocks must NOT carry a display name, only file/file_upload + caption
    assert "name" not in block["image"]
    assert block["image"]["caption"][0]["text"]["content"] == "Architecture diagram"
    # send constructs the URL from the id when no upload_url is returned
    assert mock_request.call_args_list[1].args[2].endswith("/file_uploads/fu-2/send")


@pytest.mark.asyncio
async def test_generic_file_block_carries_display_name():
    executor = _executor_with_file(b"a,b,c\n1,2,3", "data.csv", "text/csv")

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-7", "status": "pending"}),
            _ok({"id": "fu-7", "status": "uploaded"}),
            _ok({"results": []}),
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="c7",
                name="upload_notion_file",
                arguments={"page_id": "page-7", "file_id": "file-1"},
            )
        )

    assert not result.is_error, result.content
    block = mock_request.call_args_list[-1].kwargs["json"]["children"][0]
    assert block["type"] == "file"
    assert block["file"]["name"] == "data.csv"
    assert block["file"]["file_upload"]["id"] == "fu-7"


# ── Multi-part upload (> single-part limit) ─────────────────────────────


@pytest.mark.asyncio
async def test_multi_part_upload_splits_completes_and_attaches(monkeypatch):
    # Shrink the thresholds so a 5-byte payload triggers multi-part in 3 chunks.
    monkeypatch.setattr(notion.files, "_NOTION_SINGLE_PART_LIMIT", 4)
    monkeypatch.setattr(notion.files, "_NOTION_MULTI_PART_CHUNK", 2)
    executor = _executor_with_file(b"ABCDE", "big.pdf", "application/pdf")

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-m", "status": "pending"}),  # create
            _ok({"id": "fu-m", "status": "pending"}),  # send part 1
            _ok({"id": "fu-m", "status": "pending"}),  # send part 2
            _ok({"id": "fu-m", "status": "pending"}),  # send part 3
            _ok({"id": "fu-m", "status": "uploaded"}),  # complete
            _ok({"results": []}),  # attach
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="c3",
                name="upload_notion_file",
                arguments={"page_id": "page-3", "file_id": "file-1"},
            )
        )

    assert not result.is_error, result.content
    assert mock_request.call_count == 6
    calls = mock_request.call_args_list

    assert calls[0].kwargs["json"]["mode"] == "multi_part"
    assert calls[0].kwargs["json"]["number_of_parts"] == 3
    # three send calls carry sequential part numbers + the right byte slices
    assert [c.kwargs["data"]["part_number"] for c in calls[1:4]] == ["1", "2", "3"]
    assert [c.kwargs["files"]["file"][1] for c in calls[1:4]] == [b"AB", b"CD", b"E"]
    # complete then attach
    assert calls[4].args[2].endswith("/file_uploads/fu-m/complete")
    assert calls[5].args[2].endswith("/blocks/page-3/children")


# ── external_url import + database files property attach ────────────────


@pytest.mark.asyncio
async def test_external_url_immediate_upload_attaches_to_files_property():
    executor = _SafeIntegrationExecutor()
    notion.build_tools("dummy-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-ext", "status": "uploaded"}),  # create (already done)
            _ok({"id": "page-2"}),  # attach to property
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="c4",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-2",
                    "external_url": "https://example.com/files/spec.pdf",
                    "property_name": "Attachments",
                },
            )
        )

    assert not result.is_error, result.content
    assert "property 'Attachments'" in result.content
    create_call, attach_call = mock_request.call_args_list
    assert create_call.kwargs["json"]["mode"] == "external_url"
    assert create_call.kwargs["json"]["external_url"] == "https://example.com/files/spec.pdf"
    # filename + content type inferred from the URL
    assert create_call.kwargs["json"]["filename"] == "spec.pdf"
    assert create_call.kwargs["json"]["content_type"] == "application/pdf"
    # attach -> PATCH the page's files property
    assert attach_call.args[1] == "patch"
    assert attach_call.args[2].endswith("/pages/page-2")
    files_prop = attach_call.kwargs["json"]["properties"]["Attachments"]["files"]
    assert files_prop[0]["type"] == "file_upload"
    assert files_prop[0]["file_upload"]["id"] == "fu-ext"
    assert files_prop[0]["name"] == "spec.pdf"


@pytest.mark.asyncio
async def test_external_url_without_filename_falls_back_to_upload():
    """A URL with no filename segment still yields a non-empty display name.

    Without the fallback, ``filename`` would be ``""`` — Notion would receive
    an empty filename and the success text would read ``Uploaded '' ...``.
    """
    executor = _SafeIntegrationExecutor()
    notion.build_tools("dummy-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-nofn", "status": "uploaded"}),  # create (already done)
            _ok({"results": []}),  # attach block
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="c9",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-9",
                    "external_url": "https://example.com/files/",
                },
            )
        )

    assert not result.is_error, result.content
    # filename falls back to a safe default instead of an empty string ...
    create_call = mock_request.call_args_list[0]
    assert create_call.kwargs["json"]["filename"] == "upload"
    # ... and the success text names the file "upload", inside the block
    inside, outside = fenced_and_open(result.content)
    assert outside.startswith("Uploaded the external_url file to Notion")
    assert "Name: upload" in inside.splitlines()
    # no content type could be inferred, so it attaches as a generic file block
    block = mock_request.call_args_list[-1].kwargs["json"]["children"][0]
    assert block["type"] == "file"


@pytest.mark.asyncio
async def test_external_url_polls_until_uploaded(monkeypatch):
    monkeypatch.setattr(notion.files.asyncio, "sleep", AsyncMock())
    executor = _SafeIntegrationExecutor()
    notion.build_tools("dummy-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-p", "status": "pending"}),  # create
            _ok({"id": "fu-p", "status": "pending"}),  # poll 1
            _ok({"id": "fu-p", "status": "uploaded"}),  # poll 2
            _ok({"results": []}),  # attach block
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="c5",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-5",
                    "external_url": "https://example.com/a.png",
                },
            )
        )

    assert not result.is_error, result.content
    calls = mock_request.call_args_list
    assert calls[1].args[1] == "get"
    assert calls[1].args[2].endswith("/file_uploads/fu-p")
    assert calls[-1].args[2].endswith("/blocks/page-5/children")


@pytest.mark.asyncio
async def test_external_url_import_failure_is_reported(monkeypatch):
    monkeypatch.setattr(notion.files.asyncio, "sleep", AsyncMock())
    executor = _SafeIntegrationExecutor()
    notion.build_tools("dummy-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-f", "status": "pending"}),
            _ok(
                {
                    "id": "fu-f",
                    "status": "failed",
                    "file_import_result": {
                        "type": "error",
                        "error": {"message": "download_error: 404"},
                    },
                }
            ),
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="c6",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-6",
                    "external_url": "https://example.com/missing.pdf",
                },
            )
        )

    assert result.is_error
    assert "failed to import" in result.content.lower()
    assert "download_error: 404" in result.content
    # never reached the attach step
    assert mock_request.call_count == 2


# ── Upload error surfaces (send fails) ──────────────────────────────────


@pytest.mark.asyncio
async def test_send_failure_skips_attach():
    executor = _executor_with_file(b"data", "x.pdf", "application/pdf")

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-x", "status": "pending"}),
            (None, "Notion API error (400): validation_error"),
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="c8",
                name="upload_notion_file",
                arguments={"page_id": "page-8", "file_id": "file-1"},
            )
        )

    assert result.is_error
    assert "validation_error" in result.content
    # create + send only — attach never happened
    assert mock_request.call_count == 2


# ── Validation / guard rails (no HTTP calls) ────────────────────────────


@pytest.mark.asyncio
async def test_missing_page_id_is_rejected():
    executor = _executor_with_file(b"data", "x.pdf", "application/pdf")
    result = await executor.execute_tool(
        ToolCall(id="v1", name="upload_notion_file", arguments={"file_id": "file-1"})
    )
    assert result.is_error
    assert "page_id is required" in result.content


@pytest.mark.asyncio
async def test_both_sources_rejected():
    executor = _executor_with_file(b"data", "x.pdf", "application/pdf")
    result = await executor.execute_tool(
        ToolCall(
            id="v2",
            name="upload_notion_file",
            arguments={
                "page_id": "p",
                "file_id": "file-1",
                "external_url": "https://e.com/a.pdf",
            },
        )
    )
    assert result.is_error
    assert "not both" in result.content


@pytest.mark.asyncio
async def test_no_source_rejected():
    executor = _SafeIntegrationExecutor()
    notion.build_tools("dummy-token", executor)
    result = await executor.execute_tool(
        ToolCall(id="v3", name="upload_notion_file", arguments={"page_id": "p"})
    )
    assert result.is_error
    assert "provide file_id" in result.content


@pytest.mark.asyncio
async def test_file_id_without_file_context_rejected():
    executor = _SafeIntegrationExecutor()
    notion.build_tools("dummy-token", executor)  # no file_context attached
    result = await executor.execute_tool(
        ToolCall(
            id="v4",
            name="upload_notion_file",
            arguments={"page_id": "p", "file_id": "file-1"},
        )
    )
    assert result.is_error
    assert "no files are" in result.content


@pytest.mark.asyncio
async def test_file_id_not_in_context_rejected():
    executor = _executor_with_file(b"data", "x.pdf", "application/pdf", file_id="file-1")
    result = await executor.execute_tool(
        ToolCall(
            id="v5",
            name="upload_notion_file",
            arguments={"page_id": "p", "file_id": "missing-id"},
        )
    )
    assert result.is_error
    assert "not available in this conversation" in result.content


@pytest.mark.asyncio
async def test_oversize_file_rejected(monkeypatch):
    monkeypatch.setattr(notion.files, "_NOTION_MAX_UPLOAD_BYTES", 3)
    executor = _executor_with_file(b"ABCDE", "big.pdf", "application/pdf")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="v6",
                name="upload_notion_file",
                arguments={"page_id": "p", "file_id": "file-1"},
            )
        )
    assert result.is_error
    assert "upload limit" in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_external_url_must_be_https():
    executor = _SafeIntegrationExecutor()
    notion.build_tools("dummy-token", executor)
    result = await executor.execute_tool(
        ToolCall(
            id="v7",
            name="upload_notion_file",
            arguments={"page_id": "p", "external_url": "http://insecure.example/a.pdf"},
        )
    )
    assert result.is_error
    assert "HTTPS" in result.content


# ── Render choice (block_type) + attach target (attach_as) ───────────────


@pytest.mark.asyncio
async def test_block_type_file_forces_compact_download_row_for_pdf():
    """block_type='file' must attach a PDF as a compact `file` block (with a
    display name) instead of the default inline `pdf` reader block."""
    executor = _executor_with_file(b"%PDF-1.4 x", "report.pdf", "application/pdf")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-1", "status": "pending"}),
            _ok({"id": "fu-1", "status": "uploaded"}),
            _ok({"results": []}),
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="r1",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-1",
                    "file_id": "file-1",
                    "block_type": "file",
                },
            )
        )
    assert not result.is_error, result.content
    assert "as a file block" in result.content
    block = mock_request.call_args_list[-1].kwargs["json"]["children"][0]
    assert block["type"] == "file"
    assert block["file"]["type"] == "file_upload"
    assert block["file"]["name"] == "report.pdf"


@pytest.mark.asyncio
async def test_block_type_pdf_explicit_keeps_inline_reader():
    executor = _executor_with_file(b"%PDF", "doc.pdf", "application/pdf")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-2", "status": "pending"}),
            _ok({"id": "fu-2", "status": "uploaded"}),
            _ok({"results": []}),
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="r2",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-2",
                    "file_id": "file-1",
                    "block_type": "pdf",
                },
            )
        )
    assert not result.is_error, result.content
    block = mock_request.call_args_list[-1].kwargs["json"]["children"][0]
    assert block["type"] == "pdf"
    # pdf blocks must NOT carry a display name
    assert "name" not in block["pdf"]


@pytest.mark.asyncio
async def test_attach_as_page_icon():
    executor = _executor_with_file(b"\x89PNG", "icon.png", "image/png")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-i", "status": "pending"}),
            _ok({"id": "fu-i", "status": "uploaded"}),
            _ok({"id": "page-7"}),
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="r3",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-7",
                    "file_id": "file-1",
                    "attach_as": "page_icon",
                },
            )
        )
    assert not result.is_error, result.content
    assert "icon" in result.content
    attach = mock_request.call_args_list[-1]
    assert attach.args[1] == "patch"
    assert attach.args[2].endswith("/pages/page-7")
    icon = attach.kwargs["json"]["icon"]
    assert icon == {"type": "file_upload", "file_upload": {"id": "fu-i"}}


@pytest.mark.asyncio
async def test_attach_as_page_cover():
    executor = _executor_with_file(b"\x89PNG", "cover.png", "image/png")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-c", "status": "pending"}),
            _ok({"id": "fu-c", "status": "uploaded"}),
            _ok({"id": "page-8"}),
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="r4",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-8",
                    "file_id": "file-1",
                    "attach_as": "page_cover",
                },
            )
        )
    assert not result.is_error, result.content
    cover = mock_request.call_args_list[-1].kwargs["json"]["cover"]
    assert cover == {"type": "file_upload", "file_upload": {"id": "fu-c"}}


@pytest.mark.asyncio
async def test_invalid_block_type_rejected():
    executor = _executor_with_file(b"data", "x.pdf", "application/pdf")
    result = await executor.execute_tool(
        ToolCall(
            id="r5",
            name="upload_notion_file",
            arguments={"page_id": "p", "file_id": "file-1", "block_type": "spreadsheet"},
        )
    )
    assert result.is_error
    assert "block_type must be one of" in result.content


@pytest.mark.asyncio
async def test_invalid_attach_as_rejected():
    executor = _executor_with_file(b"data", "x.pdf", "application/pdf")
    result = await executor.execute_tool(
        ToolCall(
            id="r6",
            name="upload_notion_file",
            arguments={"page_id": "p", "file_id": "file-1", "attach_as": "sidebar"},
        )
    )
    assert result.is_error
    assert "attach_as must be one of" in result.content


@pytest.mark.asyncio
async def test_after_block_id_inserts_after_and_returns_new_block_id():
    """after_block_id must add `after` to the children PATCH (so the file lands
    under that block, not at the page end) and the result must surface the new
    block id so the agent can chain the next file after it."""
    executor = _executor_with_file(b"%PDF", "plan.pdf", "application/pdf")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-1", "status": "pending"}),
            _ok({"id": "fu-1", "status": "uploaded"}),
            _ok({"results": [{"id": "new-block-1"}]}),
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="a1",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-1",
                    "file_id": "file-1",
                    "block_type": "file",
                    "after_block_id": "heading-2026h1",
                },
            )
        )
    assert not result.is_error, result.content
    assert "after block heading-2026h1" in result.content
    assert "New block id: new-block-1" in result.content
    attach = mock_request.call_args_list[-1]
    assert attach.args[2].endswith("/blocks/page-1/children")
    assert attach.kwargs["json"]["after"] == "heading-2026h1"
    assert attach.kwargs["json"]["children"][0]["type"] == "file"


@pytest.mark.asyncio
async def test_after_block_id_rejected_with_page_icon():
    executor = _executor_with_file(b"\x89PNG", "i.png", "image/png")
    result = await executor.execute_tool(
        ToolCall(
            id="a2",
            name="upload_notion_file",
            arguments={
                "page_id": "p",
                "file_id": "file-1",
                "attach_as": "page_icon",
                "after_block_id": "x",
            },
        )
    )
    assert result.is_error
    assert "after_block_id only positions" in result.content


@pytest.mark.asyncio
async def test_after_block_id_rejected_with_property_name():
    executor = _executor_with_file(b"data", "d.pdf", "application/pdf")
    result = await executor.execute_tool(
        ToolCall(
            id="a3",
            name="upload_notion_file",
            arguments={
                "page_id": "p",
                "file_id": "file-1",
                "property_name": "Files",
                "after_block_id": "x",
            },
        )
    )
    assert result.is_error
    assert "after_block_id only positions" in result.content


def _bytes_resp(
    data: bytes, content_type: str = "application/pdf"
) -> tuple[httpx.Response, None]:
    return (httpx.Response(200, content=data, headers={"content-type": content_type}), None)


_S3_URL = "https://prod-files-secure.s3.us-west-2.amazonaws.com/x/report.pdf"


@pytest.mark.asyncio
async def test_download_notion_file_schema():
    executor = _SafeIntegrationExecutor()
    defs = notion.build_tools("dummy-token", executor)
    tool = next(t for t in defs if t.name == "download_notion_file")
    assert tool.input_schema["required"] == []
    for key in ("block_id", "file_url", "filename"):
        assert key in tool.input_schema["properties"]


@pytest.mark.asyncio
async def test_download_notion_file_via_block_id_returns_file_id():
    executor = _SafeIntegrationExecutor()
    notion.build_tools("dummy-token", executor)
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok(
                {
                    "type": "file",
                    "file": {
                        "type": "file",
                        "file": {"url": _S3_URL},
                        "name": "report.pdf",
                    },
                }
            ),
            _bytes_resp(b"%PDF-1.4 risk assessment"),
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="dl1",
                name="download_notion_file",
                arguments={"block_id": "block-1"},
            )
        )
    assert not result.is_error, result.content
    inside, outside = fenced_and_open(result.content)
    assert "Name: report.pdf" in inside.splitlines()
    assert "report.pdf" not in outside
    assert outside.count(f"[[FILE:{result.files[0].file_id}]]") == 1
    assert result.files and len(result.files) == 1
    assert result.files[0].filename == "report.pdf"
    # block metadata fetched, then the signed S3 URL fetched for the bytes
    assert mock_request.call_args_list[0].args[2].endswith("/blocks/block-1")
    assert mock_request.call_args_list[1].args[2] == _S3_URL


@pytest.mark.asyncio
async def test_download_notion_file_via_file_url():
    executor = _SafeIntegrationExecutor()
    notion.build_tools("dummy-token", executor)
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = _bytes_resp(b"PNGDATA", "image/png")
        result = await executor.execute_tool(
            ToolCall(
                id="dl2",
                name="download_notion_file",
                arguments={"file_url": _S3_URL, "filename": "diagram.png"},
            )
        )
    assert not result.is_error, result.content
    inside, outside = fenced_and_open(result.content)
    assert "Name: diagram.png" in inside.splitlines()
    assert "diagram.png" not in outside
    assert result.files[0].content_type == "image/png"
    assert mock_request.call_args_list[0].args[2] == _S3_URL


@pytest.mark.asyncio
async def test_download_notion_file_rejects_foreign_host():
    executor = _SafeIntegrationExecutor()
    notion.build_tools("dummy-token", executor)
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="dl3",
                name="download_notion_file",
                arguments={"file_url": "https://169.254.169.254/latest/meta-data/"},
            )
        )
    assert result.is_error
    assert "refusing to download" in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_download_notion_file_rejects_non_file_block():
    executor = _SafeIntegrationExecutor()
    notion.build_tools("dummy-token", executor)
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = _ok(
            {"type": "paragraph", "paragraph": {"rich_text": []}}
        )
        result = await executor.execute_tool(
            ToolCall(
                id="dl4",
                name="download_notion_file",
                arguments={"block_id": "para-1"},
            )
        )
    assert result.is_error
    assert "not a downloadable" in result.content


@pytest.mark.asyncio
async def test_download_notion_file_requires_a_source():
    executor = _SafeIntegrationExecutor()
    notion.build_tools("dummy-token", executor)
    result = await executor.execute_tool(
        ToolCall(id="dl5", name="download_notion_file", arguments={})
    )
    assert result.is_error
    assert "block_id" in result.content and "file_url" in result.content


@pytest.mark.asyncio
async def test_download_notion_file_rejects_both_sources():
    executor = _SafeIntegrationExecutor()
    notion.build_tools("dummy-token", executor)
    result = await executor.execute_tool(
        ToolCall(
            id="dl6",
            name="download_notion_file",
            arguments={"block_id": "b", "file_url": _S3_URL},
        )
    )
    assert result.is_error
    assert "not both" in result.content


@pytest.mark.asyncio
async def test_property_name_conflicts_with_attach_as():
    executor = _executor_with_file(b"data", "x.png", "image/png")
    result = await executor.execute_tool(
        ToolCall(
            id="r7",
            name="upload_notion_file",
            arguments={
                "page_id": "p",
                "file_id": "file-1",
                "property_name": "Files",
                "attach_as": "page_icon",
            },
        )
    )
    assert result.is_error
    assert "cannot be combined" in result.content


# ── Visual summary + colour gate on rendered images ──────────────
#
# The incident: a diagram published with the OpenCRQ mark cyan (#0FBCFF)
# instead of Filigran blue (#0015A8) after a DOM probe said "clean". The
# upload result now carries what the picture actually contains, and
# expect_colors / forbid_colors refuse the upload BEFORE Notion sees a byte.

_BLUE = "#0015A8"
_CYAN = "#0FBCFF"


def _diagram_png(*, mark: str | None = _CYAN) -> bytes:
    """White paper, a Filigran-blue card, and (by default) a small cyan mark."""
    import io

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (1600, 900), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([100, 100, 499, 299], fill=_BLUE)
    if mark:
        draw.rectangle([1500, 820, 1539, 859], fill=mark)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


_UPLOAD_OK = [
    _ok({"id": "fu-v", "status": "pending"}),
    _ok({"id": "fu-v", "status": "uploaded"}),
    _ok({"results": [{"id": "block-v"}]}),
]


def test_upload_schema_declares_the_colour_gate():
    executor = _SafeIntegrationExecutor()
    definitions = notion.build_tools("dummy-token", executor)
    tool = next(t for t in definitions if t.name == "upload_notion_file")
    props = tool.input_schema["properties"]
    for key in ("expect_colors", "forbid_colors"):
        # ONE type per property: Gemini refuses a type union, and one refused
        # tool fails every request of the agent carrying it. The handler takes
        # the comma-separated string a model sends anyway.
        assert props[key]["type"] == "array"
        assert props[key]["items"] == {"type": "string"}
        # The model learns from the schema, not from a refusal, which colours
        # the check can see.
        assert "Saturated colours only" in props[key]["description"]
    assert "expect_colors" in tool.description
    # The runtime half of the AGENTS.md guidance rides the description.
    assert "read_file_content" in tool.description
    assert "not a visual check" in tool.description


@pytest.mark.asyncio
async def test_a_forbidden_colour_refuses_the_upload_before_any_http_call():
    executor = _executor_with_file(_diagram_png(), "diagram.png", "image/png")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="vs1",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-1",
                    "file_id": "file-1",
                    "forbid_colors": [_CYAN],
                },
            )
        )
    assert result.is_error
    assert "refused to upload file file-1" in result.content
    assert "diagram.png" not in result.content
    # The colour and its share are named, and so is what was NOT done.
    assert "forbidden colour #0FBCFF is present (exactly, 0.11% of the image)" in result.content
    assert "Nothing has been uploaded" in result.content
    assert "read_file_content" in result.content
    assert "Visual summary: 1600x900 px" in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_a_missing_expected_colour_refuses_the_upload():
    executor = _executor_with_file(_diagram_png(mark=None), "diagram.png", "image/png")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="vs2",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-1",
                    "file_id": "file-1",
                    "expect_colors": ["#FF0000"],
                },
            )
        )
    assert result.is_error
    assert "expected colour #FF0000 is absent" in result.content
    assert "present: #0015A8" in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_a_passing_colour_check_uploads_and_says_what_was_checked():
    executor = _executor_with_file(_diagram_png(mark=None), "diagram.png", "image/png")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = list(_UPLOAD_OK)
        result = await executor.execute_tool(
            ToolCall(
                id="vs3",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-1",
                    "file_id": "file-1",
                    # The one string a model sends instead of a list is accepted.
                    "expect_colors": "0015a8",
                    "forbid_colors": [_CYAN],
                },
            )
        )
    assert not result.is_error, result.content
    assert mock_request.call_count == 3
    assert "Uploaded file file-1" in result.content
    assert "Visual summary: 1600x900 px; dominant #FFFFFF" in result.content
    assert "#0015A8 5.6%" in result.content
    assert (
        "Colour check passed: expected #0015A8 present; forbidden #0FBCFF absent."
        in result.content
    )


@pytest.mark.asyncio
async def test_an_image_upload_carries_the_visual_summary_without_a_gate():
    executor = _executor_with_file(_diagram_png(), "diagram.png", "image/png")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = list(_UPLOAD_OK)
        result = await executor.execute_tool(
            ToolCall(
                id="vs4",
                name="upload_notion_file",
                arguments={"page_id": "page-1", "file_id": "file-1"},
            )
        )
    assert not result.is_error, result.content
    # No gate asked for: the upload happens, and the stray cyan is VISIBLE in
    # the result for the agent (and the trace reader) to catch.
    assert "#0FBCFF 0.11%" in result.content
    assert "Colour check passed" not in result.content


@pytest.mark.asyncio
async def test_a_non_image_upload_has_no_visual_summary():
    executor = _executor_with_file(b"%PDF-1.4 fake", "report.pdf", "application/pdf")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = list(_UPLOAD_OK)
        result = await executor.execute_tool(
            ToolCall(
                id="vs5",
                name="upload_notion_file",
                arguments={"page_id": "page-1", "file_id": "file-1"},
            )
        )
    assert not result.is_error, result.content
    assert "Visual summary" not in result.content


@pytest.mark.asyncio
async def test_the_colour_gate_is_refused_for_a_non_image_file():
    executor = _executor_with_file(b"%PDF-1.4 fake", "report.pdf", "application/pdf")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="vs6",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-1",
                    "file_id": "file-1",
                    "forbid_colors": [_CYAN],
                },
            )
        )
    assert result.is_error
    assert "colour check could not run" in result.content
    assert "not a raster image" in result.content
    assert "Nothing has been uploaded" in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_the_colour_gate_is_refused_with_an_external_url():
    executor = _SafeIntegrationExecutor()
    notion.build_tools("dummy-token", executor)
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="vs7",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-1",
                    "external_url": "https://example.com/diagram.png",
                    "expect_colors": [_BLUE],
                },
            )
        )
    assert result.is_error
    assert "only file_id provides" in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_a_malformed_colour_list_is_refused_naming_the_entry():
    executor = _executor_with_file(_diagram_png(), "diagram.png", "image/png")
    result = await executor.execute_tool(
        ToolCall(
            id="vs8",
            name="upload_notion_file",
            arguments={
                "page_id": "page-1",
                "file_id": "file-1",
                "forbid_colors": ["cyan"],
            },
        )
    )
    assert result.is_error
    assert "forbid_colors entry 'cyan' is not a 6-digit hex colour" in result.content


@pytest.mark.asyncio
async def test_a_grey_gate_colour_is_refused_before_the_bytes_are_read():
    """The check measures saturated colours only; a grey it could never see
    is refused up front, never silently passed."""
    executor = _executor_with_file(_diagram_png(), "diagram.png", "image/png")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="vs10",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-1",
                    "file_id": "file-1",
                    "forbid_colors": ["#333333"],
                },
            )
        )
    assert result.is_error
    assert "forbid_colors entry '#333333' is a grey, white, near-black" in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_an_attach_failure_still_carries_the_visual_summary():
    """The file reached Notion but not the page: the agent's next move is
    about this image, so the result says what the image contains."""
    executor = _executor_with_file(_diagram_png(), "diagram.png", "image/png")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-v", "status": "pending"}),
            _ok({"id": "fu-v", "status": "uploaded"}),
            (None, "Notion API error (404): object_not_found"),
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="vs11",
                name="upload_notion_file",
                arguments={"page_id": "page-gone", "file_id": "file-1"},
            )
        )
    assert result.is_error
    assert "could not attach it" in result.content
    assert "Visual summary: 1600x900 px" in result.content
    assert "#0FBCFF 0.11%" in result.content


@pytest.mark.asyncio
async def test_a_colour_both_expected_and_forbidden_is_refused():
    executor = _executor_with_file(_diagram_png(), "diagram.png", "image/png")
    result = await executor.execute_tool(
        ToolCall(
            id="vs9",
            name="upload_notion_file",
            arguments={
                "page_id": "page-1",
                "file_id": "file-1",
                "expect_colors": [_BLUE],
                "forbid_colors": ["#0015a8"],
            },
        )
    )
    assert result.is_error
    assert "#0015A8 is listed in BOTH expect_colors and forbid_colors" in result.content
    assert "Nothing has been uploaded" in result.content


@pytest.mark.asyncio
async def test_an_expected_and_a_forbidden_colour_too_close_to_tell_apart_are_refused():
    """#001AB0 is dE 2.6 from Filigran blue: both would match the same listed
    colour, and the gate would refuse 'forbidden #001AB0 is present' about the
    blue card the agent asked for. Refused up front, naming both, before the
    bytes are read."""
    executor = _executor_with_file(_diagram_png(), "diagram.png", "image/png")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="vs12",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-1",
                    "file_id": "file-1",
                    "expect_colors": [_BLUE],
                    "forbid_colors": ["#001AB0"],
                },
            )
        )
    assert result.is_error
    assert "expected #0015A8 and forbidden #001AB0 are too close" in result.content
    assert "Nothing has been uploaded" in result.content
    mock_request.assert_not_called()


def _animated_gif_png_clean_then_cyan() -> bytes:
    """Two frames: a clean first frame, the cyan mark only in the second."""
    import io

    from PIL import Image, ImageDraw

    first = Image.new("RGB", (400, 300), "white")
    ImageDraw.Draw(first).rectangle([20, 20, 120, 120], fill=_BLUE)
    second = first.copy()
    ImageDraw.Draw(second).rectangle([300, 200, 380, 280], fill=_CYAN)
    buffer = io.BytesIO()
    first.save(buffer, format="GIF", save_all=True, append_images=[second], duration=100)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_a_forbid_check_on_an_animation_is_refused_not_passed_on_frame_one():
    """The forbidden cyan lives in frame two; a first-frame read cannot rule it
    out, so the upload is refused rather than let through on the frame it saw."""
    executor = _executor_with_file(_animated_gif_png_clean_then_cyan(), "anim.gif", "image/gif")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="vs13",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-1",
                    "file_id": "file-1",
                    "forbid_colors": [_CYAN],
                },
            )
        )
    assert result.is_error
    assert "forbidden colour #0FBCFF cannot be ruled out" in result.content
    assert "only the first of its 2 frames was read" in result.content
    assert "animated, only the first of 2 frames read" in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_a_forbid_check_on_a_sampled_image_is_refused_not_passed(monkeypatch):
    """An image too colourful to count every pixel is sampled; a colour the
    sample did not see cannot be ruled out, so the upload is refused."""
    from app.services import image_summary

    monkeypatch.setattr(image_summary, "_MAX_EXACT_COLORS", 1)
    executor = _executor_with_file(_diagram_png(mark=None), "diagram.png", "image/png")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="vs15",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-1",
                    "file_id": "file-1",
                    "forbid_colors": [_CYAN],
                },
            )
        )
    assert result.is_error
    assert "forbidden colour #0FBCFF cannot be ruled out" in result.content
    assert "was sampled" in result.content
    assert "sampled, not every pixel counted" in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_gate_colours_with_overlapping_match_regions_are_refused(monkeypatch):
    """#0037C8 is 12.6 from Filigran blue - past one match radius - yet a card
    in #0018AB is within 12 of both: the gate would have called the expected
    colour present and the forbidden one present on the same pixels. The pair
    is refused before the bytes are read, on twice the radius."""
    executor = _executor_with_file(_diagram_png(mark=None), "diagram.png", "image/png")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="vs16",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-1",
                    "file_id": "file-1",
                    "expect_colors": [_BLUE],
                    "forbid_colors": ["#0037C8"],
                },
            )
        )
    assert result.is_error
    assert "expected #0015A8 and forbidden #0037C8 are too close" in result.content
    assert "at least 24 apart" in result.content
    assert "Nothing has been uploaded" in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_a_forbid_check_on_a_cut_palette_is_refused_not_passed(monkeypatch):
    """A small image counted whole but holding more saturated colours than
    the gate palette keeps: the rarer colour is exactly the one dropped, so
    its absence cannot be claimed and the upload is refused."""
    import io

    from PIL import Image, ImageDraw

    from app.services import image_summary

    monkeypatch.setattr(image_summary, "_MAX_EXACT_COLORS", 1)
    image = Image.new("RGB", (400, 300), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([20, 20, 219, 119], fill=_BLUE)  # the one colour kept
    draw.rectangle([300, 200, 339, 239], fill=_CYAN)  # the rarer one, dropped
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    executor = _executor_with_file(buffer.getvalue(), "diagram.png", "image/png")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="vs17",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-1",
                    "file_id": "file-1",
                    "forbid_colors": [_CYAN],
                },
            )
        )
    assert result.is_error
    assert "forbidden colour #0FBCFF cannot be ruled out" in result.content
    assert "only the 1 most frequent were kept" in result.content
    assert "colour check limited to the 1 most frequent colours" in result.content
    assert "sampled" not in result.content  # every pixel WAS counted
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_a_blank_gate_entry_is_refused_instead_of_becoming_no_gate():
    """`forbid_colors=[""]` used to tokenise to nothing and let the upload
    through as if no check had been asked for. A supplied blank is malformed."""
    executor = _executor_with_file(_diagram_png(), "diagram.png", "image/png")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="vs18",
                name="upload_notion_file",
                arguments={"page_id": "page-1", "file_id": "file-1", "forbid_colors": [""]},
            )
        )
    assert result.is_error
    assert "forbid_colors has a blank entry" in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_a_raster_that_cannot_be_analysed_refuses_the_gate_by_its_real_cause():
    """PNG magic, truncated bytes: it IS an image file, so the refusal must not
    send the agent to re-render 'a non-image' - it names the decode failure."""
    truncated = _diagram_png()[:200]
    executor = _executor_with_file(truncated, "diagram.png", "image/png")
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="vs14",
                name="upload_notion_file",
                arguments={
                    "page_id": "page-1",
                    "file_id": "file-1",
                    "forbid_colors": [_CYAN],
                },
            )
        )
    assert result.is_error
    assert "looks like a raster image but could not be analysed" in result.content
    assert "not a raster image" not in result.content
    assert "Nothing has been uploaded" in result.content
    mock_request.assert_not_called()
