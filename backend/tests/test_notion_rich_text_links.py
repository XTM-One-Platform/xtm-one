"""Notion text links and formatting reach the agent, and survive its edits.

The read tools flattened every rich_text array to its ``plain_text``, so a
paragraph linking two LinkedIn pages read as bare words: the agent could not
see the links, and any rewrite through ``update_notion_block`` - which only
took raw block JSON - would have removed them. Text now reads as the inline
Markdown the writer already parses (``[label](url)``, ``**bold**``, ...) and
``update_notion_block(content=...)`` writes that Markdown back, refusing a
block that holds what Markdown cannot carry instead of dropping it.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations.providers.notion import build_tools
from app.integrations.providers.notion._common import (
    _block_to_text,
    _rich_text_to_markdown,
    _rich_text_to_plain,
)
from app.integrations.providers.notion.blocks import _markdown_losses
from app.integrations.providers.notion.formatting import _parse_inline_markdown
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall, ToolResult

FBI = "https://www.linkedin.com/company/fbi/"
FBI_CYBER = "https://www.linkedin.com/showcase/fbicyber/"


def _text(content: str, *, link: str | None = None, **annotations) -> dict:
    body: dict = {"content": content}
    if link:
        body["link"] = {"url": link}
    return {
        "type": "text",
        "text": body,
        "plain_text": content,
        "href": link,
        "annotations": {
            "bold": False,
            "italic": False,
            "strikethrough": False,
            "underline": False,
            "code": False,
            "color": "default",
            **annotations,
        },
    }


def _mention(
    sub: str, target: dict[str, Any], plain: str, href: str | None = None, **annotations: bool
) -> dict[str, Any]:
    return {
        "type": "mention",
        "mention": {"type": sub, sub: target},
        "plain_text": plain,
        "href": href,
        "annotations": {**_text("")["annotations"], **annotations},
    }


def _paragraph(rich_text: list, block_type: str = "paragraph") -> dict:
    return {"id": "blk-1", "type": block_type, block_type: {"rich_text": rich_text}}


LINKED_POST = [
    _text("Grateful to the teams at "),
    _text("@Federal Bureau of Investigation (FBI)", link=FBI),
    _text(" and "),
    _text("@FBI Cyber Division", link=FBI_CYBER),
    _text(" for the partnership."),
]


# ── Reading ─────────────────────────────────────────────────────────────


def test_a_paragraph_reads_with_its_links():
    text = _block_to_text(_paragraph(LINKED_POST))

    assert text == (
        f"Grateful to the teams at [@Federal Bureau of Investigation (FBI)]({FBI})"
        f" and [@FBI Cyber Division]({FBI_CYBER}) for the partnership."
    )


def test_formatting_reads_as_markdown_with_edge_spaces_outside_the_markers():
    rich_text = [
        _text("Plain, "),
        _text("bold ", bold=True),
        _text("italic", italic=True),
        _text(" "),
        _text("both", bold=True, italic=True),
        _text(", "),
        _text("gone", strikethrough=True),
        _text(", "),
        _text("x = 1", code=True),
        _text(" and a "),
        _text("bold link", link=FBI, bold=True),
    ]

    assert _rich_text_to_markdown(rich_text) == (
        f"Plain, **bold** *italic* ***both***, ~~gone~~, `x = 1` and a "
        f"[**bold link**]({FBI})"
    )


def test_split_runs_of_one_style_read_as_one_span():
    rich_text = [_text("Hello ", bold=True), _text("world", bold=True)]

    assert _rich_text_to_markdown(rich_text) == "**Hello world**"


def test_a_workspace_link_reads_as_an_absolute_url_and_parentheses_are_encoded():
    rich_text = [
        {**_text("our runbook", link="/0b1c2d3e"), "href": None},
        _text(" and "),
        _text("the article", link="https://en.wikipedia.org/wiki/Foo_(bar)"),
    ]

    assert _rich_text_to_markdown(rich_text) == (
        "[our runbook](https://www.notion.so/0b1c2d3e) and "
        "[the article](https://en.wikipedia.org/wiki/Foo_%28bar%29)"
    )


def test_mentions_read_as_their_text_linked_to_what_they_point_at():
    rich_text = [
        _mention("user", {"id": "u-1"}, "@Byron Graham"),
        _text(" wrote "),
        _mention("page", {"id": "p-1"}, "Q3 plan", "https://www.notion.so/p1"),
    ]

    assert _rich_text_to_markdown(rich_text) == (
        "@[Byron Graham](user:u-1) wrote [Q3 plan](https://www.notion.so/p1)"
    )


def test_a_user_mention_reads_apart_from_the_literal_text_and_parses_back() -> None:
    rich_text = [
        _text("Owner: "),
        _mention("user", {"id": "0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"}, "@Ada Lovelace"),
        _text(", not @Ada Lovelace"),
    ]

    markdown = _rich_text_to_markdown(rich_text)

    assert markdown == ("Owner: @[Ada Lovelace](user:0b1c2d3e-aaaa-bbbb-cccc-1234567890ab), not @Ada Lovelace")
    parsed = _parse_inline_markdown(markdown)
    assert [r["type"] for r in parsed] == ["text", "mention", "text"]
    assert parsed[1]["mention"] == {
        "type": "user",
        "user": {"id": "0b1c2d3e-aaaa-bbbb-cccc-1234567890ab"},
    }
    assert _markdown_losses(rich_text) == []


def test_every_text_block_reads_its_links():
    link = [_text("see "), _text("this", link=FBI)]

    assert _block_to_text(_paragraph(link, "heading_2")) == f"## see [this]({FBI})"
    assert _block_to_text(_paragraph(link, "bulleted_list_item")) == (
        f"• see [this]({FBI})"
    )
    to_do = {"type": "to_do", "to_do": {"rich_text": link, "checked": True}}
    assert _block_to_text(to_do) == f"☑ see [this]({FBI})"
    row = {"type": "table_row", "table_row": {"cells": [link, [_text("b")]]}}
    assert _block_to_text(row) == f"see [this]({FBI}) | b"
    bookmark = {
        "type": "bookmark",
        "bookmark": {"url": FBI_CYBER, "caption": [_text("caption", bold=True)]},
    }
    assert _block_to_text(bookmark) == f"[Bookmark: **caption** - {FBI_CYBER}]"


def test_a_code_block_keeps_its_raw_text():
    code = {
        "type": "code",
        "code": {"rich_text": [_text("a = b ** 2  # *not* italic")], "language": "python"},
    }

    assert _block_to_text(code) == "```python\na = b ** 2  # *not* italic\n```"


def test_plain_text_readers_are_unchanged():
    assert _rich_text_to_plain(LINKED_POST) == (
        "Grateful to the teams at @Federal Bureau of Investigation (FBI) and "
        "@FBI Cyber Division for the partnership."
    )


# ── What reads back ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "rich_text",
    [
        LINKED_POST,
        [_text("Hello ", bold=True), _text("world", bold=True)],
        [_text("a ", bold=True), _text("b", italic=True), _text(" c")],
        [_text(" "), _text("spaced", bold=True), _text("  ")],
        [_text("x", code=True), _text(" + "), _text("y", strikethrough=True)],
        [_text("wiki", link="https://en.wikipedia.org/wiki/Foo_(bar)")],
        [_text("mail me", link="mailto:a@example.com")],
        [_mention("user", {"id": "u-1"}, "@Byron Graham"), _text(" wrote")],
        [_mention("user", {"id": "u-1"}, "@Byron Graham", bold=True)],
    ],
)
def test_markdown_the_reader_writes_parses_back_to_the_same_runs(rich_text):
    assert _markdown_losses(rich_text) == []


@pytest.mark.parametrize(
    ("rich_text", "named"),
    [
        ([_mention("page", {"id": "p-1"}, "Q3 plan")], "a page mention"),
        ([_text("x"), {"type": "equation", "equation": {"expression": "e=mc^2"},
                       "plain_text": "e=mc^2", "annotations": {}}],
         "an inline equation"),
        ([_text("under", underline=True)], "underlined text"),
        ([_text("red", color="red")], "red text"),
        ([_text("call", link="tel:+33100000000")], "a link to tel:"),
        ([_text("5*3*2 apples")], "read back as formatting"),
        ([_text("see [docs](https://example.com) here")], "[label](url)"),
    ],
)
def test_what_markdown_cannot_carry_is_named(rich_text, named):
    losses = _markdown_losses(rich_text)

    assert losses
    assert named in " ".join(losses)


# ── Writing ─────────────────────────────────────────────────────────────


def _ok(payload: dict | None = None) -> httpx.Response:
    return httpx.Response(200, json=payload or {})


async def _update(executor: _SafeIntegrationExecutor, **arguments: Any) -> ToolResult:
    return await executor.execute_tool(
        ToolCall(
            id="c1",
            name="update_notion_block",
            arguments={"block_id": "blk-1", **arguments},
        )
    )


@pytest.mark.asyncio
async def test_a_markdown_edit_keeps_the_links_it_did_not_touch():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    edited = (
        f"Thankful to the teams at [@Federal Bureau of Investigation (FBI)]({FBI})"
        f" and [@FBI Cyber Division]({FBI_CYBER}) for the partnership."
    )

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [(_ok(_paragraph(LINKED_POST)), None), (_ok(), None)]
        result = await _update(executor, content=edited)

    assert not result.is_error
    assert result.content == "Block blk-1 updated."
    method, url = mock_request.call_args_list[1].args[1:3]
    assert (method, url) == ("patch", "https://api.notion.com/v1/blocks/blk-1")
    sent = mock_request.call_args_list[1].kwargs["json"]["paragraph"]["rich_text"]
    assert [(r["text"]["content"], (r["text"].get("link") or {}).get("url")) for r in sent] == [
        ("Thankful to the teams at ", None),
        ("@Federal Bureau of Investigation (FBI)", FBI),
        (" and ", None),
        ("@FBI Cyber Division", FBI_CYBER),
        (" for the partnership.", None),
    ]


@pytest.mark.asyncio
async def test_a_markdown_edit_says_which_links_it_removed():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [(_ok(_paragraph(LINKED_POST)), None), (_ok(), None)]
        result = await _update(
            executor, content=f"Thanks to [@FBI Cyber Division]({FBI_CYBER})."
        )

    assert not result.is_error
    assert result.content == f"Block blk-1 updated. Links this update removed: {FBI}."


@pytest.mark.asyncio
async def test_the_read_prefix_copied_back_is_not_written_into_the_heading():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    heading = _paragraph([_text("Old title")], "heading_2")

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [(_ok(heading), None), (_ok(), None)]
        await _update(executor, content="## New **title**")

    sent = mock_request.call_args_list[1].kwargs["json"]["heading_2"]["rich_text"]
    assert [(r["text"]["content"], bool((r.get("annotations") or {}).get("bold"))) for r in sent] == [
        ("New ", False),
        ("title", True),
    ]


@pytest.mark.asyncio
async def test_a_markdown_edit_keeps_the_user_mention_it_did_not_touch() -> None:
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    live = [_mention("user", {"id": "u-1", "object": "user"}, "@Byron Graham"), _text(" owns it")]

    with patch("app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock) as mock_request:
        mock_request.side_effect = [(_ok(_paragraph(live)), None), (_ok(), None)]
        result = await _update(executor, content="@[Byron Graham](user:u-1) owns it now")

    assert not result.is_error, result.content
    sent = mock_request.call_args_list[1].kwargs["json"]["paragraph"]["rich_text"]
    assert sent[0] == {"type": "mention", "mention": {"type": "user", "user": {"id": "u-1"}}}
    assert sent[1]["text"]["content"] == " owns it now"


@pytest.mark.asyncio
async def test_a_block_holding_a_mention_is_refused_with_its_json_to_edit():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    live = [
        _mention("page", {"id": "p-1", "object": "page"}, "Q3 plan"),
        _text(" and "),
        _text("a link", link=FBI),
    ]

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = (_ok(_paragraph(live)), None)
        result = await _update(executor, content="Q3 plan and nothing else")

    assert result.is_error
    assert 'a page mention "Q3 plan"' in result.content
    assert "nothing has been updated" in result.content
    quoted = json.loads(result.content.split("`block_json`:\n", 1)[1])
    assert quoted == {
        "paragraph": {
            "rich_text": [
                {"type": "mention", "mention": {"type": "page", "page": {"id": "p-1"}}},
                {"type": "text", "text": {"content": " and "}},
                {"type": "text", "text": {"content": "a link", "link": {"url": FBI}}},
            ]
        }
    }
    assert mock_request.call_count == 1


@pytest.mark.asyncio
async def test_content_is_refused_for_a_code_block():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    code = {"id": "blk-1", "type": "code", "code": {"rich_text": [_text("x = 1")]}}

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = (_ok(code), None)
        result = await _update(executor, content="x = 2")

    assert result.is_error
    assert "`code` block" in result.content
    assert "block_json" in result.content
    assert mock_request.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"content": "new", "block_json": '{"paragraph": {}}'}, "not both"),
        ({}, "provide `content`"),
        ({"content": "   "}, "provide `content`"),
    ],
)
async def test_content_and_block_json_are_one_or_the_other(arguments, expected):
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        result = await _update(executor, **arguments)

    assert result.is_error
    assert expected in result.content
    assert mock_request.call_count == 0


@pytest.mark.asyncio
async def test_a_json_rewrite_that_drops_links_says_so():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    rewrite = {"paragraph": {"rich_text": [{"type": "text", "text": {"content": "No links"}}]}}

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [(_ok(_paragraph(LINKED_POST)), None), (_ok(), None)]
        result = await _update(executor, block_json=json.dumps(rewrite))

    assert not result.is_error
    assert result.content == (
        f"Block blk-1 updated. Links this update removed: {FBI}, {FBI_CYBER}."
    )


@pytest.mark.asyncio
async def test_a_blank_content_sent_with_block_json_applies_the_json():
    """A model fills an argument it does not mean with ``""``: the update is
    the ``block_json``, never a refusal and never an emptied block."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    rewrite = {"paragraph": {"rich_text": [{"type": "text", "text": {"content": "Updated"}}]}}

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [(_ok(_paragraph([_text("Old")])), None), (_ok(), None)]
        result = await _update(executor, content="", block_json=json.dumps(rewrite))

    assert not result.is_error
    assert result.content == "Block blk-1 updated."
    sent = mock_request.call_args_list[1].kwargs["json"]["paragraph"]["rich_text"]
    assert [r["text"]["content"] for r in sent] == ["Updated"]


@pytest.mark.asyncio
async def test_a_json_rewrite_keeping_its_links_on_runs_without_a_type_removes_none():
    """Notion reads a rich_text item with no ``type`` as text, so the links it
    carries are kept and not reported as removed."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    rewrite = {
        "paragraph": {
            "rich_text": [
                {"text": {"content": "FBI", "link": {"url": FBI}}},
                {"text": {"content": " and "}},
                {"text": {"content": "FBI Cyber", "link": {"url": FBI_CYBER}}},
            ]
        }
    }

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [(_ok(_paragraph(LINKED_POST)), None), (_ok(), None)]
        result = await _update(executor, block_json=json.dumps(rewrite))

    assert not result.is_error
    assert result.content == "Block blk-1 updated."
