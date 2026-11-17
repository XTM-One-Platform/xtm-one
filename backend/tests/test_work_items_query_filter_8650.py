"""``list_work_items(query=..., compact=...)``: a text filter and a one-line mode (#8650).

The service's ``search`` clause matches the title or the correlation key,
case-insensitive, with the LIKE metacharacters escaped so a tool name such as
``list_work_items`` matches its underscores literally. The tool passes the
text with the visibility scope unchanged, names the filter on its first line
and, with ``compact``, answers one line per item.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import app.services.work_item as wi_service
from app.services.builtin_tools import build_work_item_tools
from app.services.builtin_tools.work_items import LIST_QUERY_MAX_CHARS
from app.tools.base import ToolCall

SCOPE_CLAUSE = object()


async def _call(executor, name: str, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


def _clauses(**kwargs):
    base = {
        "owner_agent_id": None,
        "status": None,
        "correlation_key": None,
        "open_only": False,
        "stale_only": False,
        "search": None,
        "now": datetime.now(UTC),
    }
    return wi_service._filter_clauses(**{**base, **kwargs})


def _compiled(clauses):
    return [c.compile(dialect=postgresql.dialect()) for c in clauses]


# ── service clause ───────────────────────────────────────────────────────────


def test_search_matches_title_or_key_case_insensitive():
    compiled = _compiled(_clauses(search="Pr-States"))
    sql = str(compiled[0])
    assert "work_items.title ILIKE" in sql
    assert "work_items.correlation_key ILIKE" in sql
    assert "%Pr-States%" in compiled[0].params.values()


def test_search_escapes_like_metacharacters():
    compiled = _compiled(_clauses(search="list_work_items 50%"))
    assert "ESCAPE" in str(compiled[0])
    assert "%list\\_work\\_items 50\\%%" in compiled[0].params.values()


def test_a_blank_search_adds_no_clause():
    assert _clauses(search="   ") == []


def test_search_intersects_with_open_only():
    clauses = _clauses(search="x", open_only=True)
    assert len(clauses) == 2


def test_like_literal_escapes_the_escape_character_first():
    assert wi_service._like_literal("a\\_b%") == "a\\\\\\_b\\%"


# ── tool ─────────────────────────────────────────────────────────────────────


def _row(title: str, key: str, description: str = "x" * 400) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "title": title,
        "status": "open",
        "priority": "medium",
        "owner_agent_id": str(uuid.uuid4()),
        "correlation_key": key,
        "is_stale": False,
        "description": description,
        "payload": {"a": 1},
    }


def _tools():
    return build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))


@pytest.fixture
def _scope(monkeypatch):
    from app.services import coordination_scope

    scope = SimpleNamespace(work_item_clause=lambda: SCOPE_CLAUSE, label=lambda: "Scope: fleet.")

    async def _resolve(*_args, **_kwargs):
        return scope

    monkeypatch.setattr(coordination_scope, "resolve_coordination_scope", _resolve)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_scope")
async def test_the_tool_passes_the_query_with_the_scope_unchanged(monkeypatch):
    seen: dict = {}

    async def _list(**kwargs):
        seen.update(kwargs)
        return [_row("list_work_items takes a text filter", "dev:list-work-items-text-filter")]

    monkeypatch.setattr(wi_service, "list_work_items", _list)
    _defs, executor = _tools()
    result = await _call(executor, "list_work_items", open_only=True, query="  list_work_items ")
    assert not result.is_error
    assert seen["search"] == "list_work_items"
    assert seen["open_only"] is True
    assert seen["scope"] is SCOPE_CLAUSE
    assert result.content.startswith("Title or key contains 'list_work_items'.")


@pytest.mark.asyncio
@pytest.mark.usefixtures("_scope")
async def test_no_query_passes_no_search(monkeypatch):
    seen: dict = {}

    async def _list(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(wi_service, "list_work_items", _list)
    _defs, executor = _tools()
    result = await _call(executor, "list_work_items")
    assert seen["search"] is None
    assert "contains" not in result.content


@pytest.mark.asyncio
async def test_an_overlong_query_is_refused(monkeypatch):
    async def _never(**_kwargs):
        raise AssertionError("a refused query must not list")

    monkeypatch.setattr(wi_service, "list_work_items", _never)
    _defs, executor = _tools()
    result = await _call(executor, "list_work_items", query="x" * (LIST_QUERY_MAX_CHARS + 1))
    assert "Nothing was listed" in result.content


@pytest.mark.asyncio
@pytest.mark.usefixtures("_scope")
async def test_compact_answers_one_line_per_item(monkeypatch):
    rows = [_row(f"Friction {n} on list_work_items", f"dev:friction-{n}") for n in range(3)]

    async def _list(**_kwargs):
        return rows

    monkeypatch.setattr(wi_service, "list_work_items", _list)
    _defs, executor = _tools()
    result = await _call(executor, "list_work_items", open_only=True, query="list_work_items", compact=True)
    assert not result.is_error
    for row in rows:
        assert f"- `{row['id']}` {row['correlation_key']} [open] {row['title']}" in result.content
    assert "x" * 50 not in result.content
    assert "has a payload" not in result.content
    assert "get_work_item(work_item_id)" not in result.content
    assert len(result.content) < 1500


@pytest.mark.asyncio
@pytest.mark.usefixtures("_scope")
async def test_without_compact_the_preview_is_kept(monkeypatch):
    async def _list(**_kwargs):
        return [_row("One", "dev:one")]

    monkeypatch.setattr(wi_service, "list_work_items", _list)
    _defs, executor = _tools()
    result = await _call(executor, "list_work_items")
    assert "x" * 50 in result.content
    assert "has a payload" in result.content


def test_the_schema_names_query_and_compact():
    definitions, _executor = _tools()
    listing = next(d for d in definitions if d.name == "list_work_items")
    assert listing.input_schema["properties"]["query"]["type"] == "string"
    assert listing.input_schema["properties"]["compact"]["type"] == "boolean"
    assert "query keeps the items" in listing.description
