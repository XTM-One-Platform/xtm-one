"""Grouped counts per field value (#12105).

``get_shared_state(path=<list>, group_by=<field>)`` counts a list's items per
value (where_json first, a row for items without the field), and
``list_work_items(group_by=...)`` counts work items per status, owner,
priority or key prefix in one grouped query. Without ``group_by`` both answer
as before.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import app.services.shared_state as shared_state_service
import app.services.work_item as wi_service
from app.services.builtin_tools import build_shared_state_tools, build_work_item_tools
from app.tools.base import ToolCall


async def _call(executor, name, /, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


def _gaps_state(key: str) -> dict:
    statuses = ["coding"] * 40 + ["retest"] * 30 + ["parked"] * 14
    gaps = [
        {"id": f"gap-{i}", "status": s, "meta": {"class": "harness" if i % 2 else "procedure"}}
        for i, s in enumerate(statuses)
    ]
    gaps.append({"id": "no-status"})
    return {
        "id": str(uuid.uuid4()),
        "key": key,
        "label": None,
        "status": "active",
        "is_stale": False,
        "is_company_managed": False,
        "value": {"gaps": gaps, "rule": "r"},
        "notes": [],
    }


@pytest.fixture
def state_executor(monkeypatch):
    async def _state(**kwargs):
        return _gaps_state(kwargs["key"])

    monkeypatch.setattr(shared_state_service, "get_state", _state)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    return executor


# ── get_shared_state ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_one_call_counts_every_value_and_the_missing_field(state_executor):
    result = await _call(state_executor, "get_shared_state", key="dev:harness-gaps", path="gaps", group_by="status")
    assert not result.is_error
    counts = {("missing" if r.get("missing") else r["value"]): r["count"] for r in result.structured}
    assert counts == {"coding": 40, "retest": 30, "parked": 14, "missing": 1}
    assert sum(counts.values()) == 85
    assert '"coding": 40' in result.content and "(missing): 1" in result.content
    assert "group_by 'status'" in result.content and "4 group(s), read " in result.content
    assert "gap-1" not in result.content


@pytest.mark.asyncio
async def test_where_json_filters_before_grouping(state_executor):
    result = await _call(
        state_executor,
        "get_shared_state",
        key="dev:harness-gaps",
        path="gaps",
        where_json='[{"status": "coding"}, {"status": "parked"}]',
        group_by="status",
    )
    counts = {r["value"]: r["count"] for r in result.structured}
    assert counts == {"coding": 40, "parked": 14}
    assert "54 of 85 item(s) matching where_json" in result.content


@pytest.mark.asyncio
async def test_a_dotted_field_and_ids_from_fields(state_executor):
    result = await _call(
        state_executor,
        "get_shared_state",
        key="dev:harness-gaps",
        path="gaps",
        group_by="meta.class",
        fields=["id"],
    )
    rows = {("missing" if r.get("missing") else r["value"]): r for r in result.structured}
    assert rows["harness"]["count"] == 42 and rows["procedure"]["count"] == 42
    assert rows["missing"]["ids"] == ["no-status"]
    assert len(rows["harness"]["ids"]) == 20
    assert "+22 more" in result.content


@pytest.mark.asyncio
async def test_group_by_needs_a_list_path(state_executor):
    no_path = await _call(state_executor, "get_shared_state", key="dev:harness-gaps", group_by="status")
    assert "group_by needs `path`" in no_path.content
    not_list = await _call(state_executor, "get_shared_state", key="dev:harness-gaps", path="rule", group_by="status")
    assert "group_by counts a list" in not_list.content


@pytest.mark.asyncio
async def test_without_group_by_the_read_is_unchanged(state_executor):
    result = await _call(
        state_executor, "get_shared_state", key="dev:harness-gaps", path="gaps", where_json='{"status": "parked"}'
    )
    assert "where_json: 14 of 85 item(s) match" in result.content
    assert "group_by" not in result.content


# ── list_work_items ──────────────────────────────────────────────────────────


@pytest.fixture
def _no_scope(monkeypatch):
    from app.services import coordination_scope

    scope = SimpleNamespace(work_item_clause=lambda: None, label=lambda: "Scope: all.")

    async def _resolve(*_args, **_kwargs):
        return scope

    monkeypatch.setattr(coordination_scope, "resolve_coordination_scope", _resolve)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_scope")
async def test_list_work_items_group_by_status_counts_in_one_call(monkeypatch):
    seen: dict = {}

    async def _grouped(**kwargs):
        seen.update(kwargs)
        return [("blocked", 5), ("in_progress", 3), ("open", 1)]

    async def _list(**_kwargs):
        raise AssertionError("a grouped count lists no item")

    monkeypatch.setattr(wi_service, "count_work_items_grouped", _grouped)
    monkeypatch.setattr(wi_service, "list_work_items", _list)
    _defs, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor, "list_work_items", correlation_key_prefix="dev:harness", open_only=True, group_by="status"
    )
    assert not result.is_error
    assert seen["group_by"] == "status"
    assert seen["correlation_key_prefix"] == "dev:harness" and seen["open_only"] is True
    assert "9 work item(s) in 3 group(s) by status, read " in result.content
    assert "- blocked: 5" in result.content and "- open: 1" in result.content
    assert json.loads(json.dumps(result.structured))[0] == {"value": "blocked", "count": 5}


@pytest.mark.asyncio
@pytest.mark.usefixtures("_no_scope")
async def test_list_work_items_group_by_owner_names_the_unassigned(monkeypatch):
    async def _grouped(**_kwargs):
        return [(uuid.UUID(int=1), 4), (None, 2)]

    monkeypatch.setattr(wi_service, "count_work_items_grouped", _grouped)
    _defs, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "list_work_items", group_by="owner_agent_id")
    assert "- (unassigned): 2" in result.content
    assert f"- {uuid.UUID(int=1)}: 4" in result.content


@pytest.mark.asyncio
async def test_list_work_items_refuses_an_unknown_group_by():
    _defs, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "list_work_items", group_by="title")
    assert "group_by must be one of status, owner_agent_id, priority, key_prefix" in result.content


def test_the_key_prefix_groups_on_the_key_up_to_its_first_colon():
    sql = str(wi_service._group_column("key_prefix").compile(dialect=postgresql.dialect()))
    assert "split_part(work_items.correlation_key" in sql
    assert str(wi_service._group_column("status").compile(dialect=postgresql.dialect())) == "work_items.status"


@pytest.mark.asyncio
async def test_the_service_refuses_an_unknown_grouping():
    with pytest.raises(ValueError, match="group_by must be one of"):
        await wi_service.count_work_items_grouped(group_by="title", user_id=None)
