"""OpenCTI descriptions are read whole, and a listing never cuts one silently.

``get_opencti_entity`` / ``get_opencti_relationship`` and the container's own
header return the description and content whole. A listing (search, list,
the objects of a container) still cuts a long text, at a word boundary, and
ends it with a marker that says how much is left and names the exact read.
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from app.integrations.http import clip_text
from app.integrations.providers.opencti import build_tools
from app.integrations.providers.opencti._common import (
    LISTING_CONTAINER_ITEM_TEXT_LIMIT,
    LISTING_ENTITY_TEXT_LIMIT,
    LISTING_RELATIONSHIP_TEXT_LIMIT,
    _format_entity,
    _format_relationship,
)
from app.tools.base import CallableToolExecutor, ToolCall

ENTITY_ID = "5f1c2a3b-0000-4000-8000-000000000001"
REL_ID = "5f1c2a3b-0000-4000-8000-000000000002"
ENTITY_CALL = f"get_opencti_entity(entity_id={ENTITY_ID})"
REL_CALL = f"get_opencti_relationship(relationship_id={REL_ID})"


def _words(n_chars: int, tail: str = "THE-END") -> str:
    """Words of five letters, *n_chars* long at least, ending with *tail*."""
    body = ("alpha " * (n_chars // 6 + 1)).strip()
    return f"{body} {tail}"


def _marker(read_call: str, left: int) -> str:
    return f" ... [truncated, {left} more characters: read it in full with {read_call}]"


def _expected_cut(text: str, limit: int, read_call: str) -> str:
    """What the listing shows: the head up to the last word that fits, marked."""
    head = text[:limit].rsplit(" ", 1)[0].rstrip()
    return head + _marker(read_call, len(text) - len(head))


# ---------------------------------------------------------------------------
# clip_text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "limit", "read_call", "expected"),
    [
        ("fits", 4, "c()", "fits"),
        ("", 4, "c()", ""),
        ("whatever length", None, "c()", "whatever length"),
        ("alpha beta gamma delta", 13, "c()", "alpha beta ... [truncated, 12 more characters: read it in full with c()]"),
        ("alpha beta gamma", 10, "c()", "alpha beta ... [truncated, 6 more characters: read it in full with c()]"),
        ("a " + "x" * 30, 20, "c()", "a " + "x" * 18 + " ... [truncated, 12 more characters: read it in full with c()]"),
        ("alpha beta gamma", 10, "", "alpha beta ... [truncated, 6 more characters]"),
    ],
)
def test_clip_text(text, limit, read_call, expected):
    assert clip_text(text, limit, read_call) == expected


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _description_line(out: str, label: str = "Description") -> str:
    lines = [line for line in out.splitlines() if line.startswith(f"  {label}: ")]
    assert len(lines) == 1
    return lines[0].removeprefix(f"  {label}: ")


@pytest.mark.parametrize(
    ("field", "entity_type", "label"),
    [
        ("description", "Incident", "Description"),
        ("x_opencti_description", "IPv4-Addr", "Description"),
        ("content", "Note", "Content"),
        ("media_content", "Media-Content", "Content"),
        ("report_content", "Report", "Content"),
    ],
)
def test_entity_listing_cuts_with_a_marker_and_the_single_read_is_whole(field, entity_type, label):
    text = _words(3000)
    entity = {"id": ENTITY_ID, "entity_type": entity_type, "name": "x", field: text}

    listed = _description_line(_format_entity(entity), label)
    assert listed == _expected_cut(text, LISTING_ENTITY_TEXT_LIMIT, ENTITY_CALL)

    assert _description_line(_format_entity(entity, full_text=True), label) == text


@pytest.mark.parametrize("entity_type", ["Report", "Note", "Media-Content"])
def test_entity_without_content_has_no_content_line(entity_type):
    entity = {"id": ENTITY_ID, "entity_type": entity_type, "name": "x", "description": "d"}
    assert "Content:" not in _format_entity(entity, full_text=True)


def test_report_content_of_another_type_is_not_read_as_note_content():
    """A Report's plain ``content`` key is Note's field; only the alias is the body."""
    entity = {"id": ENTITY_ID, "entity_type": "Report", "name": "x", "content": "stray"}
    assert "Content:" not in _format_entity(entity, full_text=True)


def test_entity_fragment_aliases_report_content():
    """Report.content (String) is aliased: unaliased it clashes with Note.content (String!)."""
    from app.integrations.providers.opencti._common import _SDO_INLINE_FIELDS

    report = next(line for line in _SDO_INLINE_FIELDS.splitlines() if "... on Report {" in line)
    assert "report_content: content" in report


def test_entity_text_that_fits_the_listing_is_unchanged():
    text = "x" * LISTING_ENTITY_TEXT_LIMIT
    entity = {"id": ENTITY_ID, "entity_type": "Incident", "name": "x", "description": text}
    assert _description_line(_format_entity(entity)) == text


def test_entity_without_id_is_marked_without_a_read_call():
    text = _words(1000)
    entity = {"entity_type": "Incident", "name": "x", "description": text}
    head = text[:LISTING_ENTITY_TEXT_LIMIT].rsplit(" ", 1)[0]
    assert _description_line(_format_entity(entity)) == (
        f"{head} ... [truncated, {len(text) - len(head)} more characters]"
    )


def test_relationship_listing_cuts_with_a_marker_and_the_single_read_is_whole():
    text = _words(2000)
    rel = {"id": REL_ID, "relationship_type": "uses", "description": text}

    assert _description_line(_format_relationship(rel)) == _expected_cut(
        text, LISTING_RELATIONSHIP_TEXT_LIMIT, REL_CALL
    )
    assert _description_line(_format_relationship(rel, full_text=True)) == text


# ---------------------------------------------------------------------------
# Tools, through the executor
# ---------------------------------------------------------------------------


async def _run(tool: str, arguments: dict, answer: dict) -> str:
    executor = CallableToolExecutor()
    build_tools("https://octi.test||static-token", executor)

    async def _fake(self, method, url, **kwargs):
        return httpx.Response(200, json=answer, request=httpx.Request("POST", url))

    with patch.object(httpx.AsyncClient, "request", _fake):
        result = await executor.execute_tool(ToolCall(id="1", name=tool, arguments=arguments))
    return result.content


LONG = _words(5000)


def _incident() -> dict:
    return {"id": ENTITY_ID, "entity_type": "Incident", "name": "Incident 1", "description": LONG}


@pytest.mark.asyncio
async def test_get_opencti_entity_returns_the_whole_description():
    out = await _run("get_opencti_entity", {"entity_id": ENTITY_ID}, {"data": {"stixCoreObject": _incident()}})
    assert _description_line(out) == LONG


@pytest.mark.asyncio
async def test_get_opencti_relationship_returns_the_whole_description():
    rel = {"id": REL_ID, "relationship_type": "uses", "description": LONG}
    out = await _run(
        "get_opencti_relationship", {"relationship_id": REL_ID}, {"data": {"stixCoreRelationship": rel}}
    )
    assert _description_line(out) == LONG


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments", "root"),
    [
        ("list_opencti_entities", {"types": ["Incident"]}, "stixDomainObjects"),
        ("search_opencti", {"search": "incident"}, "stixCoreObjects"),
    ],
)
async def test_listings_mark_the_cut_and_name_the_read(tool, arguments, root):
    answer = {"data": {root: {"edges": [{"node": _incident()}], "pageInfo": {"globalCount": 1}}}}
    out = await _run(tool, arguments, answer)
    assert _description_line(out) == _expected_cut(LONG, LISTING_ENTITY_TEXT_LIMIT, ENTITY_CALL)


@pytest.mark.asyncio
async def test_container_full_header_is_whole_and_its_objects_are_marked():
    container = {
        "id": "report-1",
        "entity_type": "Report",
        "name": "Report 1",
        "description": LONG,
        "objects": {
            "edges": [
                {"node": _incident()},
                {
                    "node": {
                        "id": REL_ID,
                        "entity_type": "uses",
                        "relationship_type": "uses",
                        "description": LONG,
                        "from": {"id": "a", "entity_type": "Malware", "name": "m"},
                        "to": {"id": "b", "entity_type": "Attack-Pattern", "name": "p"},
                    }
                },
            ],
            "pageInfo": {"globalCount": 2},
        },
    }
    out = await _run(
        "get_opencti_container_full", {"container_id": "report-1"}, {"data": {"stixDomainObject": container}}
    )
    lines = out.splitlines()
    assert lines.count(f"Description: {LONG}") == 1
    object_descriptions = [
        line.removeprefix("    Description: ") for line in lines if line.startswith("    Description: ")
    ]
    assert object_descriptions == [
        _expected_cut(LONG, LISTING_CONTAINER_ITEM_TEXT_LIMIT, ENTITY_CALL),
        _expected_cut(LONG, LISTING_CONTAINER_ITEM_TEXT_LIMIT, REL_CALL),
    ]


async def _container_full(container: dict) -> tuple[str, list[str]]:
    """The tool's text and the GraphQL queries it sent."""
    executor = CallableToolExecutor()
    build_tools("https://octi.test||static-token", executor)
    queries: list[str] = []

    async def _fake(self, method, url, **kwargs):
        queries.append((kwargs.get("json") or {}).get("query", ""))
        answer = {"data": {"stixDomainObject": container}}
        return httpx.Response(200, json=answer, request=httpx.Request("POST", url))

    with patch.object(httpx.AsyncClient, "request", _fake):
        result = await executor.execute_tool(
            ToolCall(id="1", name="get_opencti_container_full", arguments={"container_id": "report-1"})
        )
    return result.content, queries


@pytest.mark.asyncio
async def test_container_full_returns_the_whole_report_content():
    container = {"id": "report-1", "entity_type": "Report", "name": "Report 1", "report_content": LONG}
    out, queries = await _container_full(container)
    assert out.splitlines().count(f"Content: {LONG}") == 1
    assert any("report_content: content" in q for q in queries)


@pytest.mark.asyncio
async def test_container_full_report_without_content_has_no_content_line():
    out, _ = await _container_full({"id": "report-1", "entity_type": "Report", "name": "Report 1"})
    assert "Content:" not in out


@pytest.mark.asyncio
async def test_get_opencti_entity_returns_the_whole_report_content():
    report = {"id": ENTITY_ID, "entity_type": "Report", "name": "Report 1", "report_content": LONG}
    out = await _run("get_opencti_entity", {"entity_id": ENTITY_ID}, {"data": {"stixCoreObject": report}})
    assert _description_line(out, "Content") == LONG
