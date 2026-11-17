"""list_work_items and list_shared_state page instead of stopping at 50.

A sweep trusting one call used to miss every row past the 50th with no word
about it. Both tools now take limit / offset, state the total on their first
line, and name the offset of the next page whenever a page is cut.
"""

import re
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import app.services.shared_state as shared_state_service
import app.services.work_item as wi_service
from app.services.builtin_tools import build_shared_state_tools, build_work_item_tools
from app.services.builtin_tools.paging import page_bounds, page_line
from app.tools.base import ToolCall, limiting_tool_results


async def _call(executor, name, /, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


def _items(n: int) -> list[dict]:
    return [
        {
            "id": f"00000000-0000-0000-0000-{i:012d}",
            "title": f"Item {i}",
            "status": "open",
            "priority": "medium",
            "owner_agent_id": None,
            "correlation_key": None,
            "is_stale": False,
            "claimed_by_agent_id": None,
        }
        for i in range(n)
    ]


def _fake_work_item_store(monkeypatch, rows: list[dict], calls: dict):
    async def _list(**kwargs):
        calls.setdefault("list", []).append(kwargs)
        start, size = kwargs["offset"], kwargs["limit"]
        return rows[start : start + size]

    async def _count(**kwargs):
        calls.setdefault("count", []).append(kwargs)
        return len(rows)

    monkeypatch.setattr(wi_service, "list_work_items", _list)
    monkeypatch.setattr(wi_service, "count_work_items", _count)


def _ids(text: str) -> list[str]:
    return re.findall(r"`(00000000-[0-9a-f-]+)`", text)


def test_page_bounds_clamps_and_defaults():
    assert page_bounds(None, None) == (50, 0)
    assert page_bounds(500, -3) == (200, 0)
    assert page_bounds(0, 10) == (1, 10)
    assert page_bounds("x", "y") == (50, 0)


def test_page_line_wording():
    assert page_line(0, 50, 63, "work items") == "showing 1-50 of 63 work items, offset=50 for the next page."
    assert page_line(50, 13, 63, "work items") == "showing 51-63 of 63 work items, the last page."
    assert page_line(0, 3, 3, "work items") == "showing all 3 work items."
    assert "smaller offset" in page_line(80, 0, 63, "work items")


@pytest.mark.asyncio
async def test_seventy_open_items_read_over_two_pages_without_gap_or_duplicate(monkeypatch):
    rows, calls = _items(70), {}
    _fake_work_item_store(monkeypatch, rows, calls)
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))

    first = await _call(executor, "list_work_items", open_only=True)
    head = first.content.splitlines()[0]
    assert head.startswith("showing 1-50 of 70 work items, offset=50 for the next page.")
    second = await _call(executor, "list_work_items", open_only=True, offset=50)
    assert second.content.splitlines()[0].startswith("showing 51-70 of 70 work items, the last page.")

    seen = _ids(first.content) + _ids(second.content)
    assert seen == [r["id"] for r in rows]
    assert len(set(seen)) == 70
    # The same filters reach the list and its count, so the total is the
    # total of what the pages return.
    assert calls["count"][0]["open_only"] is True
    assert calls["count"][0]["reader"] is calls["list"][0]["reader"]
    # The short last page needs no COUNT.
    assert len(calls["count"]) == 1


@pytest.mark.asyncio
async def test_unpaged_call_on_seventy_items_states_the_cut(monkeypatch):
    _fake_work_item_store(monkeypatch, _items(70), {})
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "list_work_items")
    assert result.content.startswith("showing 1-50 of 70 work items, offset=50 for the next page.")
    assert len(_ids(result.content)) == 50


@pytest.mark.asyncio
async def test_limit_is_capped_at_200(monkeypatch):
    calls: dict = {}
    _fake_work_item_store(monkeypatch, _items(250), calls)
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "list_work_items", limit=1000)
    assert calls["list"][0]["limit"] == 200
    assert result.content.startswith("showing 1-200 of 250 work items, offset=200")


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [3_686, 20_000, 57_600])
async def test_page_fits_the_tool_result_cap_and_names_the_rows_shown(monkeypatch, cap):
    # 200 rows with 500-character previews exceed the loop's
    # cap; the page keeps what fits, says so, and the next offset is the
    # first row left out, so walking the pages never skips a row.
    rows = _items(200)
    for r in rows:
        r["description"] = "d" * 900
    _fake_work_item_store(monkeypatch, rows, {})
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    seen: list[str] = []
    offset = 0
    with limiting_tool_results(cap):
        for _ in range(400):
            result = await _call(executor, "list_work_items", limit=200, offset=offset)
            assert len(result.content) <= cap - 200
            ids = _ids(result.content)
            assert ids, result.content.splitlines()[0]
            head = result.content.splitlines()[0]
            assert head.startswith(f"showing {offset + 1}-{offset + len(ids)} of 200 work items")
            seen.extend(ids)
            match = re.search(r"offset=(\d+) for the next page", head)
            if not match:
                break
            offset = int(match.group(1))
            assert offset == len(seen)
    assert seen == [r["id"] for r in rows]


@pytest.mark.asyncio
async def test_shared_state_page_fits_the_tool_result_cap(monkeypatch):
    rows = [
        {"key": f"k-{i:03d}", "label": "x" * 200, "status": "active", "is_stale": False} for i in range(200)
    ]

    async def _list(**kwargs):
        start, size = kwargs["offset"], kwargs["limit"]
        return rows[start : start + size]

    async def _count(**kwargs):
        return len(rows)

    monkeypatch.setattr(shared_state_service, "list_states", _list)
    monkeypatch.setattr(shared_state_service, "count_states", _count)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    with limiting_tool_results(3_686):
        result = await _call(executor, "list_shared_state", limit=200)
    assert len(result.content) <= 3_486
    shown = re.findall(r"`(k-\d+)`", result.content)
    assert result.content.splitlines()[0].startswith(
        f"showing 1-{len(shown)} of 200 active shared state entries, offset={len(shown)} for the next page."
    )


@pytest.mark.asyncio
async def test_offset_past_the_end_says_so(monkeypatch):
    _fake_work_item_store(monkeypatch, _items(10), {})
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "list_work_items", offset=40)
    assert "showing none of 10 work items at offset=40" in result.content


def test_several_statuses_filter_in_one_clause():
    clauses = wi_service._filter_clauses(
        owner_agent_id=None,
        status="open, in_progress,blocked,bogus,open",
        correlation_key=None,
        open_only=False,
        stale_only=False,
        search=None,
        now=wi_service.datetime.now(wi_service.UTC),
    )
    assert len(clauses) == 1
    sql = str(clauses[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "IN ('open', 'in_progress', 'blocked')" in sql


def test_single_status_keeps_its_meaning():
    assert wi_service.parse_status_filter("done") == ["done"]
    assert wi_service.parse_status_filter("nope") == []
    assert wi_service.parse_status_filter(None) == []


def test_status_schema_accepts_several_statuses():
    defs, _ = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    props = next(d for d in defs if d.name == "list_work_items").input_schema["properties"]
    assert "enum" not in props["status"]
    assert {"limit", "offset"} <= set(props)


@pytest.mark.asyncio
async def test_list_shared_state_pages_and_states_the_cut(monkeypatch):
    rows = [
        {"key": f"k-{i:03d}", "label": None, "status": "active", "is_stale": False} for i in range(70)
    ]
    counted: list[dict] = []

    async def _list(**kwargs):
        start, size = kwargs["offset"], kwargs["limit"]
        return rows[start : start + size]

    async def _count(**kwargs):
        counted.append(kwargs)
        return len(rows)

    monkeypatch.setattr(shared_state_service, "list_states", _list)
    monkeypatch.setattr(shared_state_service, "count_states", _count)
    defs, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))

    first = await _call(executor, "list_shared_state")
    assert first.content.splitlines()[0].startswith(
        "showing 1-50 of 70 active shared state entries, offset=50 for the next page."
    )
    second = await _call(executor, "list_shared_state", offset=50)
    assert second.content.splitlines()[0].startswith(
        "showing 51-70 of 70 active shared state entries, the last page."
    )
    keys = re.findall(r"`(k-\d+)`", first.content) + re.findall(r"`(k-\d+)`", second.content)
    assert keys == [r["key"] for r in rows]
    assert counted[0]["status"] == "active"
    props = next(d for d in defs if d.name == "list_shared_state").input_schema["properties"]
    assert {"limit", "offset"} <= set(props)
