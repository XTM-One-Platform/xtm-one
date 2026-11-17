"""``list_work_items(correlation_key_prefix=...)``: the items whose key starts with it.

The service adds a literal ``LIKE 'prefix%'`` clause (wildcards escaped) next to
the exact ``correlation_key`` match; the tool and the REST routes pass it through.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.dialects import postgresql

import app.services.work_item as wi_service
from app.models.work_item import WorkItem
from app.services.builtin_tools import build_work_item_tools
from app.tools.base import ToolCall


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


def _count(keys: list[str], **filters) -> int:
    """Run the clauses against an in-memory table holding *keys*."""
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE work_items (correlation_key TEXT, status TEXT)"))
        for key in keys:
            conn.execute(
                text("INSERT INTO work_items (correlation_key, status) VALUES (:k, 'done')"),
                {"k": key},
            )
        query = select(func.count()).select_from(WorkItem.__table__).where(*_clauses(**filters))
        return conn.execute(query).scalar_one()


# ── service clause ───────────────────────────────────────────────────────────


def test_a_prefix_keeps_the_keys_that_start_with_it():
    assert _count(["mail:a", "mail:b", "dev:x"], correlation_key_prefix="mail:") == 2


def test_the_prefix_combines_with_status():
    assert _count(["mail:a", "mail:b", "dev:x"], correlation_key_prefix="mail:", status="done") == 2
    assert _count(["mail:a", "mail:b", "dev:x"], correlation_key_prefix="mail:", status="open") == 0


def test_like_wildcards_in_the_prefix_are_literal():
    keys = ["order_1", "orderX1", "a%b:1", "abc:1"]
    assert _count(keys, correlation_key_prefix="order_") == 1
    assert _count(keys, correlation_key_prefix="a%b") == 1


def test_the_clause_is_an_index_friendly_like_with_an_escape():
    compiled = _clauses(correlation_key_prefix="mail:")[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert "work_items.correlation_key LIKE" in sql
    assert "ILIKE" not in sql
    assert "ESCAPE" in sql
    assert "mail:%" in compiled.params.values()


def test_the_exact_key_stays_an_exact_match():
    clauses = _clauses(correlation_key="mail:")
    assert len(clauses) == 1
    assert "LIKE" not in str(clauses[0].compile(dialect=postgresql.dialect()))
    assert _count(["mail:a", "mail:"], correlation_key="mail:") == 1


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_empty_prefix_adds_no_clause(value):
    assert _clauses(correlation_key_prefix=value) == []


# ── tool ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def _no_scope(monkeypatch):
    from app.services import coordination_scope

    scope = SimpleNamespace(work_item_clause=lambda: None, label=lambda: "Scope: all.")

    async def _resolve(*_args, **_kwargs):
        return scope

    monkeypatch.setattr(coordination_scope, "resolve_coordination_scope", _resolve)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_scope")
async def test_the_tool_passes_the_prefix_to_the_service(monkeypatch):
    seen: dict = {}

    async def _list(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(wi_service, "list_work_items", _list)
    _defs, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await executor.execute_tool(
        ToolCall(
            id=str(uuid.uuid4()),
            name="list_work_items",
            arguments={"status": "done", "correlation_key_prefix": "mail:", "sort": "completed_desc"},
        )
    )
    assert not result.is_error
    assert seen["correlation_key_prefix"] == "mail:"
    assert seen["status"] == "done"
    assert seen["correlation_key"] is None


def test_the_schema_names_the_prefix():
    definitions, _executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    listing = next(d for d in definitions if d.name == "list_work_items")
    assert "correlation_key_prefix" in listing.input_schema["properties"]
    assert "correlation_key_prefix keeps the items" in listing.description
