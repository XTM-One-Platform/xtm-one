"""Notion image / media block alignment is UI-only.

The Notion public API exposes no alignment (left / center / right) for an
image or any other media block - the request schemas accept only the file
source and a ``caption`` and are ``additionalProperties: false`` - on the
provider's API version AND on the latest published one. Alignment is set in
the Notion UI (hover the block > alignment icon).

Before this fix two code paths failed badly:

* ``update_notion_block`` with ``{"image": {"alignment": "left"}}`` went to
  Notion, which answered a raw HTTP 400 the agent retried;
* ``upload_notion_file(..., alignment="left")`` had the argument SILENTLY
  dropped by the executor's unknown-kwargs filter, so the upload succeeded and
  the agent reported an alignment it never applied.

These tests pin the deterministic contract: an ``alignment`` key anywhere in
a block payload and an ``alignment`` argument on ANY write tool (upload and the
three block writers - the executor's silent drop applied to all of them) are
refused with the explicit message before any HTTP call, every image block the
upload tool attaches carries the structured hint, both tool descriptions say
so, no schema advertises the refused argument, and the API version is one
whose block schema was actually checked - so a version bump has to re-verify
instead of regressing silently.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import app.integrations.providers.notion as notion
from app.integrations.providers.notion._common import (
    _NOTION_ALIGNMENT_NOTE,
    _NOTION_ALIGNMENT_UI_ONLY,
    _NOTION_ALIGNMENT_VERIFIED_VERSIONS,
    _NOTION_VERSION,
)
from app.integrations.providers.notion.formatting import _find_block_alignment
from app.llm.base import FileRef
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import FileContext, ToolCall, ToolDefinition, ToolResult
from tests.integration_file_fakes import fenced_and_open

_SAFE_REQUEST = "app.integrations.providers.notion._common.safe_request"
# Every Notion tool that writes a block: each declares an ``alignment``
# argument (absent from its schema) only to refuse it.
_WRITE_TOOLS = (
    "upload_notion_file",
    "update_notion_block",
    "append_notion_blocks_json",
    "append_notion_blocks",
)


def _ok(payload: dict) -> tuple[httpx.Response, None]:
    return (httpx.Response(200, json=payload), None)


def _executor() -> tuple[_SafeIntegrationExecutor, list[ToolDefinition]]:
    executor = _SafeIntegrationExecutor()
    definitions = notion.build_tools("dummy-token", executor)
    return executor, definitions


def _executor_with_file(
    data: bytes, filename: str, content_type: str, file_id: str = "file-1"
) -> _SafeIntegrationExecutor:
    executor, _ = _executor()
    fc = FileContext()
    fc.add(FileRef.from_bytes(data, filename, content_type, file_id=file_id))
    executor.file_context = fc
    return executor


async def _update(executor: _SafeIntegrationExecutor, block_json: str) -> ToolResult:
    return await executor.execute_tool(
        ToolCall(
            id="u1",
            name="update_notion_block",
            arguments={"block_id": "blk-1", "block_json": block_json},
        )
    )


async def _upload(executor: _SafeIntegrationExecutor, **arguments: Any) -> ToolResult:
    return await executor.execute_tool(
        ToolCall(id="up1", name="upload_notion_file", arguments=arguments)
    )


# ── The claim is pinned to the API versions it was verified against ───────


def test_notion_version_is_one_whose_block_schema_was_checked_for_alignment() -> None:
    """Bumping ``Notion-Version`` must re-verify the image block schema.

    The guard's whole premise is "this API version has no alignment field".
    If the header moves to a version that is not in the verified set, this
    test fails and asks the author to re-read the schema: if alignment has
    appeared, implement it and drop the guard; otherwise add the version to
    ``_NOTION_ALIGNMENT_VERIFIED_VERSIONS``.
    """
    assert _NOTION_VERSION in _NOTION_ALIGNMENT_VERIFIED_VERSIONS, (
        f"Notion-Version {_NOTION_VERSION} was not checked for an image block "
        "alignment field - re-read the block schema before shipping the bump."
    )


# ── Tool descriptions say so, and the upload tool does not advertise it ──


def test_tool_descriptions_state_alignment_is_ui_only() -> None:
    _, definitions = _executor()
    by_name = {t.name: t for t in definitions}
    sentence = "UI-only setting in Notion and cannot be set through the API"
    for name in ("update_notion_block", "upload_notion_file"):
        description = by_name[name].description
        assert "alignment" in description.lower(), name
        assert sentence in description, name
    # Every write tool accepts an ``alignment`` argument only to REFUSE it;
    # advertising the parameter in a schema would invite the model to use it.
    for name in _WRITE_TOOLS:
        assert "alignment" not in by_name[name].input_schema["properties"], name


# ── The pure payload walker ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # update shape: the block type IS the top-level key
        ({"image": {"alignment": "left"}}, "image.alignment"),
        # alignment next to legitimate fields is still the key to drop
        ({"image": {"alignment": "right", "caption": []}}, "image.alignment"),
        # append shape: explicit ``type`` discriminator
        (
            {"type": "image", "image": {"external": {"url": "https://x/a.png"}, "alignment": "center"}},
            "image.alignment",
        ),
        # a stray top-level key
        ({"image": {"caption": []}, "alignment": "left"}, "alignment"),
        # tucked inside the file source (no request field is named alignment,
        # so the key is a refusal wherever the model puts it)
        (
            {"image": {"external": {"url": "https://x/a.png", "alignment": "left"}}},
            "image.external.alignment",
        ),
        (
            {"type": "image", "image": {"file_upload": {"id": "fu-1", "alignment": "left"}}},
            "image.file_upload.alignment",
        ),
        # on a caption item (a list hop)
        (
            {"image": {"caption": [{"type": "text", "text": {"content": "x"}, "alignment": "left"}]}},
            "image.caption[0].alignment",
        ),
        # nested children (column_list > column > image)
        (
            {
                "type": "column_list",
                "column_list": {
                    "children": [
                        {
                            "type": "column",
                            "column": {
                                "children": [
                                    {"type": "paragraph", "paragraph": {"rich_text": []}},
                                    {"type": "image", "image": {"alignment": "left"}},
                                ]
                            },
                        }
                    ]
                },
            },
            "column_list.children[0].column.children[1].image.alignment",
        ),
        # clean payloads are left alone - including the word inside a VALUE
        ({"image": {"caption": [{"type": "text", "text": {"content": "Diagram"}}]}}, None),
        ({"paragraph": {"rich_text": [{"type": "text", "text": {"content": "alignment: left"}}]}}, None),
        ({"archived": True}, None),
        # non-dict input never raises
        ("not a block", None),
        (None, None),
        ([], None),
    ],
)
def test_find_block_alignment_paths(payload: Any, expected: str | None) -> None:
    assert _find_block_alignment(payload) == expected


def test_find_block_alignment_is_bounded() -> None:
    """An absurdly deep payload stops at the scan bound instead of recursing
    until Python gives up (the key sits below the bound, so it is not found -
    Notion itself accepts two levels of nesting per request)."""
    deep: dict = {"alignment": "left"}
    for _ in range(200):
        deep = {"paragraph": {"children": [deep]}}
    assert _find_block_alignment(deep) is None


# ── update_notion_block refuses before any HTTP call ──────────────────────


@pytest.mark.asyncio
async def test_update_block_image_alignment_refused_without_any_http_call() -> None:
    """``{"image": {"alignment": "left"}}`` used to reach Notion and come back
    as a raw 400 the agent retried; it is now refused deterministically with
    the explicit UI-only message - no pre-flight GET, no PATCH."""
    executor, _ = _executor()

    with patch(_SAFE_REQUEST, new_callable=AsyncMock) as mock_request:
        result = await _update(executor, '{"image": {"alignment": "left"}}')

    assert result.content.startswith("Error:")
    # A refused write is a tool ERROR on the trace / tool_done event / error
    # counters, not a success that happens to read "Error:".
    assert result.is_error
    assert _NOTION_ALIGNMENT_UI_ONLY in result.content
    # Names the exact key to drop and how to resend the rest.
    assert "`image.alignment`" in result.content
    assert "Remove `alignment` from the payload" in result.content
    # The remedy is the user's, in Notion.
    assert "hovering the block" in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_update_block_alignment_beside_a_caption_is_still_refused() -> None:
    """Notion rejects the WHOLE request when ``alignment`` rides along with a
    legitimate ``caption`` (``additionalProperties: false``), so the guard
    must refuse the mixed payload too and tell the agent to resend the rest."""
    executor, _ = _executor()
    payload = json.dumps(
        {
            "image": {
                "alignment": "right",
                "caption": [{"type": "text", "text": {"content": "Architecture"}}],
            }
        }
    )

    with patch(_SAFE_REQUEST, new_callable=AsyncMock) as mock_request:
        result = await _update(executor, payload)

    assert _NOTION_ALIGNMENT_UI_ONLY in result.content
    assert "`image.alignment`" in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_update_block_top_level_alignment_is_refused() -> None:
    executor, _ = _executor()

    with patch(_SAFE_REQUEST, new_callable=AsyncMock) as mock_request:
        result = await _update(executor, '{"image": {"caption": []}, "alignment": "left"}')

    assert _NOTION_ALIGNMENT_UI_ONLY in result.content
    assert "`alignment`" in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_update_block_alignment_inside_the_file_source_is_refused() -> None:
    """``alignment`` tucked under ``image.external`` used to slip past a
    body-only check and reach Notion. No request
    field of any block type is named ``alignment``, so the key is refused
    wherever it sits - and the path tells the agent exactly where."""
    executor, _ = _executor()
    payload = json.dumps(
        {"image": {"external": {"url": "https://example.com/wide.png", "alignment": "left"}}}
    )

    with patch(_SAFE_REQUEST, new_callable=AsyncMock) as mock_request:
        result = await _update(executor, payload)

    assert result.is_error
    assert _NOTION_ALIGNMENT_UI_ONLY in result.content
    assert "`image.external.alignment`" in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_update_image_caption_without_alignment_still_patches() -> None:
    """The guard must not over-trigger: a legitimate image edit (caption only)
    still runs the pre-flight GET and the PATCH."""
    executor, _ = _executor()
    payload = json.dumps(
        {"image": {"caption": [{"type": "text", "text": {"content": "Diagram"}}]}}
    )

    with patch(_SAFE_REQUEST, new_callable=AsyncMock) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "blk-1", "type": "image"}),
            _ok({}),
        ]
        result = await _update(executor, payload)

    assert not result.is_error, result.content
    assert "updated" in result.content
    assert [c.args[1] for c in mock_request.call_args_list] == ["get", "patch"]
    sent = mock_request.call_args_list[1].kwargs["json"]
    assert "alignment" not in sent["image"]


# ── append_notion_blocks_json cannot be used to route around the guard ────


@pytest.mark.asyncio
async def test_append_blocks_json_image_alignment_refused_without_any_http_call() -> None:
    executor, _ = _executor()
    children = json.dumps(
        [
            {"type": "heading_2", "heading_2": {"rich_text": [{"type": "text", "text": {"content": "Diagram"}}]}},
            {
                "type": "image",
                "image": {"external": {"url": "https://example.com/wide.png"}, "alignment": "left"},
            },
        ]
    )

    with patch(_SAFE_REQUEST, new_callable=AsyncMock) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="a1",
                name="append_notion_blocks_json",
                arguments={"block_id": "page-1", "children_json": children},
            )
        )

    assert result.is_error
    assert _NOTION_ALIGNMENT_UI_ONLY in result.content
    # The path names the offending child so the agent drops the right key.
    assert "`children[1].image.alignment`" in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_append_blocks_json_nested_alignment_is_refused() -> None:
    executor, _ = _executor()
    children = json.dumps(
        [
            {
                "type": "column_list",
                "column_list": {
                    "children": [
                        {
                            "type": "column",
                            "column": {
                                "children": [{"type": "image", "image": {"alignment": "left"}}]
                            },
                        }
                    ]
                },
            }
        ]
    )

    with patch(_SAFE_REQUEST, new_callable=AsyncMock) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="a2",
                name="append_notion_blocks_json",
                arguments={"block_id": "page-1", "children_json": children},
            )
        )

    assert result.is_error
    assert _NOTION_ALIGNMENT_UI_ONLY in result.content
    assert (
        "`children[0].column_list.children[0].column.children[0].image.alignment`"
        in result.content
    )
    mock_request.assert_not_called()


# ── The alignment ARGUMENT is refused on every block write tool too ───────

# (tool, its legitimate arguments, what the refusal says was left untouched)
_BLOCK_WRITE_CALLS: list[tuple[str, dict[str, Any], str]] = [
    (
        "append_notion_blocks",
        {"block_id": "page-1", "content": "Architecture overview"},
        "nothing has been appended",
    ),
    (
        "append_notion_blocks_json",
        {
            "block_id": "page-1",
            "children_json": json.dumps(
                [{"type": "image", "image": {"external": {"url": "https://x/a.png"}}}]
            ),
        },
        "nothing has been appended",
    ),
    (
        "update_notion_block",
        {"block_id": "blk-1", "block_json": json.dumps({"image": {"caption": []}})},
        "nothing has been updated",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "arguments", "nothing_done"), _BLOCK_WRITE_CALLS)
async def test_block_write_tools_refuse_an_alignment_argument_without_any_http_call(
    name: str, arguments: dict[str, Any], nothing_done: str
) -> None:
    """The silent drop on ``upload_notion_file`` applied to the
    block write tools too: ``alignment="left"`` passed as an ARGUMENT (not
    inside the block JSON) was filtered out by the executor as an unknown
    kwarg, the write went through, and the agent reported an alignment it
    never applied. Each tool now declares the argument only to refuse it -
    explicit message, zero HTTP calls, the call is resent minus the argument.
    """
    executor, _ = _executor()

    with patch(_SAFE_REQUEST, new_callable=AsyncMock) as mock_request:
        result = await executor.execute_tool(
            ToolCall(id="k1", name=name, arguments={**arguments, "alignment": "left"})
        )

    assert result.content.startswith("Error:"), result.content
    # Flagged like ``upload_notion_file``'s refusal: the integration executor
    # wraps a plain string with ``is_error=False``, so the handler
    # returns an explicit error ToolResult.
    assert result.is_error
    assert _NOTION_ALIGNMENT_UI_ONLY in result.content
    assert "alignment='left'" in result.content
    assert "Retry the same call without the alignment argument" in result.content
    assert nothing_done in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_update_block_blank_alignment_argument_still_patches() -> None:
    """A blank ``alignment`` argument (a model echoing the default) must not
    trip the refusal on the block tools either: the pre-flight GET and
    the PATCH still run, with nothing alignment-shaped in the body."""
    executor, _ = _executor()
    payload = json.dumps(
        {"image": {"caption": [{"type": "text", "text": {"content": "Diagram"}}]}}
    )

    with patch(_SAFE_REQUEST, new_callable=AsyncMock) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "blk-1", "type": "image"}),
            _ok({}),
        ]
        result = await executor.execute_tool(
            ToolCall(
                id="k2",
                name="update_notion_block",
                arguments={"block_id": "blk-1", "block_json": payload, "alignment": "  "},
            )
        )

    assert not result.is_error, result.content
    assert "updated" in result.content
    assert [c.args[1] for c in mock_request.call_args_list] == ["get", "patch"]
    assert "alignment" not in mock_request.call_args_list[1].kwargs["json"]["image"]


# ── upload_notion_file: the argument is refused, never silently dropped ──


@pytest.mark.asyncio
async def test_upload_notion_file_alignment_argument_refused_before_any_upload() -> None:
    """``alignment="left"`` used to be filtered out by the executor as an
    unknown kwarg: the file was uploaded, the result said nothing, and the
    agent reported a left-aligned diagram that was centered. It is now a
    declared, refused argument - explicit message, zero HTTP calls, nothing
    lands on the page under a false promise."""
    executor = _executor_with_file(b"\x89PNG wide", "diagram.png", "image/png")

    with patch(_SAFE_REQUEST, new_callable=AsyncMock) as mock_request:
        result = await _upload(
            executor, page_id="page-1", file_id="file-1", alignment="left"
        )

    assert result.is_error
    assert _NOTION_ALIGNMENT_UI_ONLY in result.content
    assert "alignment='left'" in result.content
    assert "Retry the same call without the alignment argument" in result.content
    assert "nothing has been uploaded" in result.content
    mock_request.assert_not_called()


@pytest.mark.asyncio
async def test_upload_notion_file_blank_alignment_is_not_an_alignment() -> None:
    """An empty / whitespace ``alignment`` (a model echoing the default) must
    not trip the refusal - the upload proceeds normally."""
    executor = _executor_with_file(b"%PDF-1.4", "report.pdf", "application/pdf")

    with patch(_SAFE_REQUEST, new_callable=AsyncMock) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-1", "status": "pending"}),
            _ok({"id": "fu-1", "status": "uploaded"}),
            _ok({"results": [{"id": "block-1"}]}),
        ]
        result = await _upload(
            executor, page_id="page-1", file_id="file-1", alignment="  "
        )

    assert not result.is_error, result.content
    assert mock_request.call_count == 3


# ── upload_notion_file: every image block says the alignment is UI-only ──


@pytest.mark.asyncio
async def test_upload_notion_file_image_block_result_carries_alignment_note() -> None:
    executor = _executor_with_file(b"\x89PNG wide", "diagram.png", "image/png")

    with patch(_SAFE_REQUEST, new_callable=AsyncMock) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-1", "status": "pending"}),
            _ok({"id": "fu-1", "status": "uploaded"}),
            _ok({"results": [{"id": "new-block-1"}]}),
        ]
        result = await _upload(
            executor,
            page_id="page-1",
            file_id="file-1",
            caption="Architecture",
            after_block_id="heading-1",
        )

    assert not result.is_error, result.content
    assert "image block" in result.content
    # The structured hint closes the answer's own text, after the chain hint,
    # verbatim; only the block of the file's name follows it.
    _inside, outside = fenced_and_open(result.content)
    assert outside.endswith(f"{_NOTION_ALIGNMENT_NOTE}\n")
    assert outside.index("New block id: new-block-1") < outside.index(_NOTION_ALIGNMENT_NOTE)
    # And nothing alignment-shaped was sent to Notion.
    block = mock_request.call_args_list[-1].kwargs["json"]["children"][0]
    assert block["type"] == "image"
    assert "alignment" not in block["image"]


@pytest.mark.asyncio
async def test_upload_notion_file_external_image_url_result_carries_alignment_note() -> None:
    """An image imported from a public URL renders as an image block too, so
    it gets the same hint (the type is inferred from the URL's extension)."""
    executor, _ = _executor()

    with patch(_SAFE_REQUEST, new_callable=AsyncMock) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-ext", "status": "uploaded"}),
            _ok({"results": [{"id": "new-block-2"}]}),
        ]
        result = await _upload(
            executor,
            page_id="page-2",
            external_url="https://example.com/diagrams/wide.png",
        )

    assert not result.is_error, result.content
    _inside, outside = fenced_and_open(result.content)
    assert outside.endswith(f"{_NOTION_ALIGNMENT_NOTE}\n")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "filename", "content_type", "extra"),
    [
        # inline PDF reader - full width, no alignment to speak of
        (b"%PDF-1.4", "report.pdf", "application/pdf", {}),
        # compact download row
        (b"a,b\n1,2", "data.csv", "text/csv", {}),
        # an image forced into a compact file row is not an image block
        (b"\x89PNG", "diagram.png", "image/png", {"block_type": "file"}),
    ],
)
async def test_upload_notion_file_non_image_blocks_carry_no_alignment_note(
    data: bytes, filename: str, content_type: str, extra: dict[str, str]
) -> None:
    executor = _executor_with_file(data, filename, content_type)

    with patch(_SAFE_REQUEST, new_callable=AsyncMock) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-1", "status": "pending"}),
            _ok({"id": "fu-1", "status": "uploaded"}),
            _ok({"results": [{"id": "block-1"}]}),
        ]
        result = await _upload(executor, page_id="page-1", file_id="file-1", **extra)

    assert not result.is_error, result.content
    assert _NOTION_ALIGNMENT_NOTE not in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"attach_as": "page_icon"},
        {"attach_as": "page_cover"},
        {"property_name": "Attachments"},
    ],
)
async def test_upload_notion_file_icon_cover_and_property_targets_carry_no_alignment_note(
    extra: dict[str, str],
) -> None:
    """Only a page-body IMAGE block has an alignment; the page icon / cover
    and a database ``files`` property do not."""
    executor = _executor_with_file(b"\x89PNG", "cover.png", "image/png")

    with patch(_SAFE_REQUEST, new_callable=AsyncMock) as mock_request:
        mock_request.side_effect = [
            _ok({"id": "fu-1", "status": "pending"}),
            _ok({"id": "fu-1", "status": "uploaded"}),
            _ok({"id": "page-1"}),
        ]
        result = await _upload(executor, page_id="page-1", file_id="file-1", **extra)

    assert not result.is_error, result.content
    assert _NOTION_ALIGNMENT_NOTE not in result.content
