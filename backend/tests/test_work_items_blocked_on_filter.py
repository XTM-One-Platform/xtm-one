"""``list_work_items(blocked_on=<id>)``: the items waiting for one item.

The service adds a JSONB containment clause on ``WorkItem.blocked_on``; the
tool resolves an id prefix like its other id arguments, names the filter on
its first line and answers an empty list, never an error, when nobody waits.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import app.services.work_item as wi_service
from app.services.builtin_tools import build_work_item_tools
from app.tools.base import ToolCall

BLOCKER = "1205bf16-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
OTHER = "1205bf16-ffff-4e5f-8a9b-0c1d2e3f4a5b"


async def _call(executor, name: str, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


def _compiled(clauses):
    return [c.compile(dialect=postgresql.dialect()) for c in clauses]


def _sql(clauses) -> str:
    return " AND ".join(str(c) for c in _compiled(clauses))


def _params(clauses) -> list:
    return [v for c in _compiled(clauses) for v in c.params.values()]


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


# ── service clause ───────────────────────────────────────────────────────────


def test_blocked_on_adds_a_jsonb_containment_of_the_id():
    clauses = _clauses(blocked_on=BLOCKER)
    sql = _sql(clauses)
    assert "blocked_on" in sql
    assert "@>" in sql
    assert [BLOCKER] in _params(clauses)


def test_a_dashless_or_upper_case_id_is_matched_in_its_stored_form():
    clauses = _clauses(blocked_on=BLOCKER.replace("-", "").upper())
    assert [BLOCKER] in _params(clauses)


def test_blocked_on_intersects_with_status():
    clauses = _clauses(blocked_on=BLOCKER, status="blocked")
    assert len(clauses) == 2
    sql = _sql(clauses)
    assert "@>" in sql
    assert "work_items.status =" in sql
    assert "blocked" in _params(clauses)


def test_a_value_that_is_not_an_id_matches_nothing():
    clauses = _clauses(blocked_on="not-an-id")
    assert len(clauses) == 1
    assert _sql(clauses) == "false"


def test_no_blocked_on_adds_no_clause():
    assert _clauses() == []


# ── tool ─────────────────────────────────────────────────────────────────────


def _row(item_id: str, title: str) -> dict:
    return {
        "id": item_id,
        "title": title,
        "status": "blocked",
        "priority": "medium",
        "owner_agent_id": None,
        "correlation_key": "dev:x",
        "is_stale": False,
        "blocked_on": [BLOCKER],
        "description": "",
    }


def _tools():
    return build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))


@pytest.fixture
def _no_scope(monkeypatch):
    from app.services import coordination_scope

    scope = SimpleNamespace(work_item_clause=lambda: None, label=lambda: "Scope: all.")

    async def _resolve(*_args, **_kwargs):
        return scope

    monkeypatch.setattr(coordination_scope, "resolve_coordination_scope", _resolve)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_scope")
async def test_the_tool_passes_the_id_and_names_the_filter(monkeypatch):
    seen: dict = {}
    waiting = _row(str(uuid.uuid4()), "Waits for the fix")

    async def _list(**kwargs):
        seen.update(kwargs)
        return [waiting]

    monkeypatch.setattr(wi_service, "list_work_items", _list)
    _defs, executor = _tools()
    result = await _call(executor, "list_work_items", blocked_on=BLOCKER, status="blocked")
    assert not result.is_error
    assert seen["blocked_on"] == BLOCKER
    assert seen["status"] == "blocked"
    assert result.content.startswith(f"Blocked on {BLOCKER}: 1 item(s).")
    assert "Waits for the fix" in result.content


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_scope")
async def test_an_id_nobody_waits_on_answers_an_empty_list(monkeypatch):
    async def _list(**_kwargs):
        return []

    monkeypatch.setattr(wi_service, "list_work_items", _list)
    _defs, executor = _tools()
    result = await _call(executor, "list_work_items", blocked_on=BLOCKER)
    assert not result.is_error
    assert result.content.startswith(f"Blocked on {BLOCKER}: 0 item(s).")
    assert result.structured == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_scope")
async def test_a_prefix_resolves_to_the_one_visible_item(monkeypatch):
    seen: dict = {}

    async def _by_prefix(prefix, *, user_id, reader=None, limit=6):
        seen["prefix"] = prefix
        return [{"id": BLOCKER, "title": "The fix"}]

    async def _list(**kwargs):
        seen["blocked_on"] = kwargs["blocked_on"]
        return []

    monkeypatch.setattr(wi_service, "work_items_by_id_prefix", _by_prefix)
    monkeypatch.setattr(wi_service, "list_work_items", _list)
    _defs, executor = _tools()
    await _call(executor, "list_work_items", blocked_on="1205bf16")
    assert seen == {"prefix": "1205bf16", "blocked_on": BLOCKER}


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_scope")
async def test_an_unresolved_prefix_is_refused_not_read_as_nobody_waits(monkeypatch):
    async def _by_prefix(prefix, *, user_id, reader=None, limit=6):
        return []

    async def _never(**_kwargs):
        raise AssertionError("a prefix that starts no item must not list")

    monkeypatch.setattr(wi_service, "work_items_by_id_prefix", _by_prefix)
    monkeypatch.setattr(wi_service, "list_work_items", _never)
    _defs, executor = _tools()
    result = await _call(executor, "list_work_items", blocked_on="deadbeef")
    assert "no work item you can see has an id starting with `deadbeef`" in result.content
    assert "Nothing was listed" in result.content


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_scope")
async def test_an_ambiguous_prefix_is_refused(monkeypatch):
    async def _by_prefix(prefix, *, user_id, reader=None, limit=6):
        return [{"id": BLOCKER, "title": "One"}, {"id": OTHER, "title": "Two"}]

    async def _never(**_kwargs):
        raise AssertionError("an ambiguous prefix must not list")

    monkeypatch.setattr(wi_service, "work_items_by_id_prefix", _by_prefix)
    monkeypatch.setattr(wi_service, "list_work_items", _never)
    _defs, executor = _tools()
    result = await _call(executor, "list_work_items", blocked_on="1205bf16")
    assert BLOCKER in result.content and OTHER in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["1205bf1", "not an id"])
async def test_a_short_or_non_hex_value_is_refused(monkeypatch, value):
    async def _never(**_kwargs):
        raise AssertionError("a refused value must not list")

    monkeypatch.setattr(wi_service, "list_work_items", _never)
    _defs, executor = _tools()
    result = await _call(executor, "list_work_items", blocked_on=value)
    assert "blocked_on must be a work item id" in result.content
    assert "Nothing was listed" in result.content


def test_the_schema_names_the_filter():
    definitions, _executor = _tools()
    listing = next(d for d in definitions if d.name == "list_work_items")
    assert "blocked_on" in listing.input_schema["properties"]
    assert "blocked_on, a work item's id" in listing.description
