"""list_work_items reads completion time: completed_at, its filters and sort (#8558).

An audit of a day's completions used to page every terminal item, newest
created first, then read each one to learn when it finished. Every item now
carries ``completed_at`` (set when it reaches done or cancelled, cleared when
it is reopened); ``completed_from`` / ``completed_to`` / ``updated_from``
bound the listing and ``sort="completed_desc"`` lists the newest completions
first. The first line of the tool's answer names the sort in force.
"""

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import app.services.work_item as wi_service
from app.models.work_item import WorkItem, stamp_completion
from app.services.builtin_tools import build_work_item_tools
from app.tools.base import ToolCall

T = datetime(2026, 11, 4, 22, 0, tzinfo=UTC)


def _sql(clause) -> str:
    return str(clause.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


async def _call(executor, name, /, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


# ── completed_at follows the status ───────────────────────────────────────


def test_a_terminal_status_stamps_the_completion_once():
    item = WorkItem(status="done")
    stamp_completion(item, T)
    assert item.completed_at == T
    item.status = "cancelled"
    stamp_completion(item, T + timedelta(hours=1))
    assert item.completed_at == T, "a finished item keeps the time it first finished"


@pytest.mark.parametrize("status", ["open", "claimed", "in_progress", "blocked"])
def test_reopening_clears_the_completion(status):
    item = WorkItem(status="done", completed_at=T)
    item.status = status
    stamp_completion(item, T + timedelta(hours=1))
    assert item.completed_at is None


def test_the_serialized_item_carries_completed_at():
    item = WorkItem(id=uuid.uuid4(), title="x", status="done", priority="medium", completed_at=T, source="agent")
    assert wi_service.serialize_work_item(item, now=T)["completed_at"] == T.isoformat()
    item.completed_at = None
    assert wi_service.serialize_work_item(item, now=T)["completed_at"] is None


# ── the filters and the sort ──────────────────────────────────────────────


def _clauses(**bounds) -> str:
    clauses = wi_service._filter_clauses(
        owner_agent_id=None,
        status="done,cancelled",
        correlation_key=None,
        open_only=False,
        stale_only=False,
        search=None,
        now=T,
        **bounds,
    )
    return " AND ".join(_sql(c) for c in clauses)


def test_completed_from_is_inclusive_and_completed_to_exclusive():
    sql = _clauses(completed_from=T, completed_to=T + timedelta(hours=1), updated_from=T)
    assert "work_items.completed_at >= '2026-11-04 22:00:00+00:00'" in sql
    assert "work_items.completed_at < '2026-11-04 23:00:00+00:00'" in sql
    assert "work_items.updated_at >= '2026-11-04 22:00:00+00:00'" in sql


def test_no_bound_adds_no_clause():
    assert "completed_at" not in _clauses()


def test_sort_defaults_to_created_and_refuses_an_unknown_one():
    assert wi_service.normalize_sort(None) == "created_desc"
    assert wi_service.normalize_sort(" Completed_Desc ") == "completed_desc"
    with pytest.raises(ValueError, match="sort must be one of"):
        wi_service.normalize_sort("oldest")


def test_time_filter_parsing():
    assert wi_service.parse_time_filter("", "completed_from") is None
    assert wi_service.parse_time_filter("2026-11-04T22:00:00Z", "completed_from") == T
    assert wi_service.parse_time_filter("2026-11-04T22:00:00", "completed_from") == T
    assert wi_service.parse_time_filter("2026-11-04T23:00:00+01:00", "completed_from") == T
    with pytest.raises(ValueError, match="completed_to must be an ISO-8601"):
        wi_service.parse_time_filter("yesterday", "completed_to")


# ── the tool ──────────────────────────────────────────────────────────────


def _store(monkeypatch, rows, calls):
    async def _list(**kwargs):
        calls.setdefault("list", []).append(kwargs)
        return rows

    async def _count(**kwargs):
        calls.setdefault("count", []).append(kwargs)
        return len(rows)

    monkeypatch.setattr(wi_service, "list_work_items", _list)
    monkeypatch.setattr(wi_service, "count_work_items", _count)


def _row(**extra) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "title": "Ship it",
        "status": "done",
        "priority": "medium",
        "owner_agent_id": None,
        "correlation_key": None,
        "is_stale": False,
        "claimed_by_agent_id": None,
        **extra,
    }


@pytest.mark.asyncio
async def test_the_tool_passes_the_bounds_and_the_sort_and_shows_the_times(monkeypatch):
    calls: dict = {}
    _store(
        monkeypatch,
        [_row(claimed_at="2026-11-04T21:00:00+00:00", completed_at="2026-11-04T22:30:00+00:00")],
        calls,
    )
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor,
        "list_work_items",
        status="done,cancelled",
        completed_from="2026-11-04T22:00:00Z",
        sort="completed_desc",
    )
    listed = calls["list"][0]
    assert listed["completed_from"] == T
    assert listed["completed_to"] is None
    assert listed["sort"] == "completed_desc"
    assert "Sorted completed_desc." in result.content.splitlines()[0]
    assert "completed at 2026-11-04T22:30:00+00:00" in result.content
    assert "claimed at 2026-11-04T21:00:00+00:00" in result.content


@pytest.mark.asyncio
async def test_the_tool_states_the_default_sort(monkeypatch):
    _store(monkeypatch, [_row(status="open")], {})
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "list_work_items")
    assert result.content.startswith("showing all 1 work items. Sorted created_desc.")


@pytest.mark.asyncio
async def test_an_unreadable_bound_lists_nothing(monkeypatch):
    calls: dict = {}
    _store(monkeypatch, [_row()], calls)
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "list_work_items", completed_from="last week")
    assert "completed_from must be an ISO-8601" in result.content
    assert "list" not in calls


# ── the migration ─────────────────────────────────────────────────────────


def test_the_migration_revises_the_previous_head():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "w8558wicomp1_add_work_item_completed_at.py"
    spec = importlib.util.spec_from_file_location("w8558wicomp1", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "w8558wicomp1"
    assert module.down_revision == "f8528frict01"
