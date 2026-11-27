"""Tests for adaptive background-task polling + generalized result collection.

Two issues surfaced after self-fan-out shipped:

1. ``check_task_status`` blind-slept a fixed 15s, so a task that already
   terminated still cost the agent the full delay (the "nudge on terminated"
   never fired). ``wait_for_tasks`` now returns the moment every task reaches a
   terminal state.

2. ``collect_task_results`` only recognised OpenCTI/OpenAEV-shaped items (a
   known id-key or ``raw_configuration``) and silently returned "No items"
   for a flat structured JSON object (the common self-fan-out output). The
   extractor now treats a flat dict / an array of objects as items too.

Also: the chat path now exposes ``get_task_result`` / ``get_full_task_result``
so a self-fan-out agent can read and synthesize its workers' output.
"""

from __future__ import annotations

import json
import re
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from app.services import sub_agent
from app.services.builtin_tools import build_sub_agent_tools
from app.services.sub_agent import wait_for_tasks
from app.tools.agent_loop import _resolve_session_reference


def _make_agent(name: str = "Worker") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        slug=name.lower(),
        handover_types={},
    )


def _status(s: str) -> dict:
    return {"status": s}


# ── wait_for_tasks (adaptive polling) ───────────────────────────────────────


async def test_wait_returns_immediately_when_all_terminal():
    """A task that is already done returns on the first read - no blind sleep."""
    tid = str(uuid.uuid4())
    m = AsyncMock(return_value={tid: _status("completed")})
    with patch.object(sub_agent, "get_task_statuses", new=m):
        result = await wait_for_tasks([tid], str(uuid.uuid4()), budget=5, poll_interval=1)
    assert result[tid]["status"] == "completed"
    assert m.await_count == 1


async def test_wait_returns_as_soon_as_task_becomes_terminal():
    tid = str(uuid.uuid4())
    m = AsyncMock(
        side_effect=[
            {tid: _status("running")},
            {tid: _status("running")},
            {tid: _status("completed")},
        ]
    )
    with patch.object(sub_agent, "get_task_statuses", new=m):
        result = await wait_for_tasks(
            [tid], str(uuid.uuid4()), budget=5, poll_interval=0.01
        )
    assert result[tid]["status"] == "completed"
    assert m.await_count == 3


async def test_wait_stops_at_budget_when_still_running():
    tid = str(uuid.uuid4())
    m = AsyncMock(return_value={tid: _status("running")})
    with patch.object(sub_agent, "get_task_statuses", new=m):
        result = await wait_for_tasks(
            [tid], str(uuid.uuid4()), budget=0.05, poll_interval=0.01
        )
    assert result[tid]["status"] == "running"


async def test_wait_keeps_not_found_pending_until_budget():
    """An unknown id must NOT read as terminal.

    A typo'd or other-user id returning "all done" used to end an until=
    wait while the real work was still running. The cost of the new
    contract: a genuinely missing task now waits out the (bounded) budget,
    so this asserts the wait polls repeatedly and returns None at the end.

    The budget runs on a virtual clock that advances one poll interval per
    status read: on the wall clock a loaded xdist runner could spend the
    whole 0.05 s budget before the first read returned, and the wait then
    stopped after one poll (CI run 41550653548, ``assert 1 >= 2``).
    """
    tid = str(uuid.uuid4())
    # Binary fractions so the clock sums are exact (no float drift).
    step = 2**-7
    clock = {"now": 0.0}

    async def _read(_ids, _user_id):
        clock["now"] += step
        return {tid: None}

    m = AsyncMock(side_effect=_read)
    fake_time = SimpleNamespace(monotonic=lambda: clock["now"])
    with (
        patch.object(sub_agent, "get_task_statuses", new=m),
        patch.object(sub_agent, "time", new=fake_time),
    ):
        result = await wait_for_tasks(
            [tid], str(uuid.uuid4()), budget=4 * step, poll_interval=step
        )
    assert result[tid] is None
    # Deadline 4*step; reads end at step .. 4*step: the fourth meets it.
    assert m.await_count == 4


async def test_wait_batches_status_fetch_in_one_call_per_poll():
    """All statuses are fetched in ONE batched query per poll, not one per id."""
    tids = [str(uuid.uuid4()) for _ in range(3)]
    m = AsyncMock(return_value={t: _status("completed") for t in tids})
    with patch.object(sub_agent, "get_task_statuses", new=m):
        result = await wait_for_tasks(tids, str(uuid.uuid4()), budget=5, poll_interval=1)
    assert all(result[t]["status"] == "completed" for t in tids)
    # One batched fetch covering all three ids (terminal on first poll), not three.
    assert m.await_count == 1
    assert set(m.await_args.args[0]) == set(tids)


async def test_wait_empty_ids_returns_empty():
    assert await wait_for_tasks([], str(uuid.uuid4())) == {}


# ── check_task_status tool (uses adaptive wait) ─────────────────────────────


def _build_handlers(*, session_vars: dict | None = None):
    agent = _make_agent()
    result = build_sub_agent_tools(
        MagicMock(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[agent],
        parent_run_id=str(uuid.uuid4()),
        session_vars=session_vars,
    )
    assert result is not None
    defs, executor = result
    return defs, executor


async def test_check_task_status_uses_adaptive_wait_no_blind_sleep():
    _defs, executor = _build_handlers()
    check = executor._handlers["check_task_status"]
    tid = str(uuid.uuid4())
    m = AsyncMock(return_value={tid: {"status": "completed"}})
    with patch.object(sub_agent, "wait_for_tasks", new=m):
        out = await check(tid)
    assert "completed" in out
    m.assert_awaited_once()


async def test_check_task_status_all_invalid_skips_wait():
    _defs, executor = _build_handlers()
    check = executor._handlers["check_task_status"]
    m = AsyncMock()
    with patch.object(sub_agent, "wait_for_tasks", new=m):
        out = await check("not-a-uuid")
    m.assert_not_awaited()
    assert out  # an invalid-id message is returned


# ── collect_task_results (generalized extraction) ───────────────────────────


def _build_collect():
    sv: dict = {}
    _defs, executor = _build_handlers(session_vars=sv)
    return sv, executor._handlers["collect_task_results"]


async def test_collect_flat_json_object_is_one_item():
    """A flat structured object (the self-fan-out case) is collected as 1 item."""
    sv, collect = _build_collect()
    payload = json.dumps(
        {"batch": "C", "computation": 99, "marker": "BATCH_C_OK", "status": "completed"}
    )
    with patch.object(sub_agent, "get_task_result_by_id", new=AsyncMock(return_value=payload)):
        out = await collect(str(uuid.uuid4()))
    items = sv["$collected"]["items"]
    assert len(items) == 1
    assert next(iter(items.values()))["marker"] == "BATCH_C_OK"
    assert "No items could be extracted" not in out


async def test_collect_array_of_objects():
    sv, collect = _build_collect()
    payload = json.dumps([{"name": "a"}, {"name": "b"}])
    with patch.object(sub_agent, "get_task_result_by_id", new=AsyncMock(return_value=payload)):
        await collect(str(uuid.uuid4()))
    assert len(sv["$collected"]["items"]) == 2


async def test_collect_opencti_shaped_items_keyed_by_id():
    """Existing behavior preserved: items with an id-key are keyed by id."""
    sv, collect = _build_collect()
    payload = json.dumps([{"id": "w1", "type": "widget"}, {"id": "w2"}])
    with patch.object(sub_agent, "get_task_result_by_id", new=AsyncMock(return_value=payload)):
        await collect(str(uuid.uuid4()))
    items = sv["$collected"]["items"]
    assert set(items) == {"w1", "w2"}


async def test_collect_nested_response_envelope():
    sv, collect = _build_collect()
    payload = json.dumps({"response": [{"name": "x"}, {"name": "y"}]})
    with patch.object(sub_agent, "get_task_result_by_id", new=AsyncMock(return_value=payload)):
        await collect(str(uuid.uuid4()))
    assert len(sv["$collected"]["items"]) == 2


def _advertised_path(out: str) -> str | None:
    match = re.search(r"Per-item path: `([^`]+)`", out)
    return match.group(1) if match else None


async def _collect_results(results: list[object], **kwargs: str) -> tuple[dict[str, Any], str]:
    sv, collect = _build_collect()  # type: ignore[no-untyped-call]
    texts = {str(uuid.uuid4()): json.dumps(r) for r in results}

    async def _result(tid: str) -> str:
        return texts[tid]

    with patch.object(sub_agent, "get_task_result_by_id", new=_result):
        out = await collect(",".join(texts), **kwargs)
    return sv, out


async def test_collect_keeps_whole_items_and_drops_sibling_metadata() -> None:
    """#2661: lists of items plus metadata, and an item with nested objects."""
    rules = [{"name": f"r{i}", "pattern": f"p{i}"} for i in range(1, 7)]
    single = {"name": "single", "mitre": {"technique": "T1059"}}
    sv, out = await _collect_results(
        [
            {"rules": rules[:3], "summary": {"count": 3}},
            {"rules": rules[3:], "summary": {"count": 3}},
            {"rule": single},
        ],
        output_key="rules",
    )
    assert len(sv["$collected"]["rules"]) == 7
    assert _resolve_session_reference("$collected.rules[5].name", sv) == (True, True, "r6")
    assert _resolve_session_reference("$collected.rules[6]", sv) == (True, True, single)
    assert "Collected 7 items from 3 tasks" in out
    assert "name (7/7)" in out
    assert "count" not in out
    path = _advertised_path(out)
    assert path == "$collected.rules[0].name"
    assert _resolve_session_reference(path, sv) == (True, True, "r1")


async def test_collect_items_list_with_a_total_adds_only_the_items() -> None:
    sv, out = await _collect_results([{"items": [{"name": str(i)} for i in range(4)], "total": 4}])
    assert len(sv["$collected"]["items"]) == 4
    assert "Collected 4 items" in out


async def test_collect_empty_item_list_extracts_nothing() -> None:
    """An empty item list, alone or next to metadata, is zero items, never one bogus item."""
    for result in ({"rules": [], "total": 0}, {"rules": []}, {"data": {"rules": []}}):
        sv, out = await _collect_results([result], output_key="rules")
        assert out == "No items could be extracted from the provided task results."
        assert "$collected" not in sv


async def test_collect_advertises_only_a_key_item_zero_carries() -> None:
    sv, out = await _collect_results([[{"batch": "A", "score": 3}, {"batch": "B"}]], output_key="my-key")
    assert "name" not in out
    path = _advertised_path(out)
    assert path == '$collected["my-key"][0].batch'
    assert _resolve_session_reference(path, sv) == (True, True, "A")
    assert "score (1/2)" in out


async def test_collect_advertises_no_path_when_item_zero_has_no_scalar() -> None:
    _sv, out = await _collect_results([[{"steps": [1, 2]}]])
    assert _advertised_path(out) is None


async def test_collect_item_key_names_the_list() -> None:
    sv, _out = await _collect_results(
        [{"rules": [{"name": "a"}], "errors": [{"pattern": "bad"}]}],
        output_key="rules",
        item_key="rules",
    )
    assert list(sv["$collected"]["rules"].values()) == [{"name": "a"}]


async def test_collect_widget_envelopes_unchanged() -> None:
    """OpenCTI widget envelopes, alone, in a list or wrapped by id, stay one item each."""
    widget = {"title": "t", "raw_configuration": {"id": "w1", "type": "number"}}
    other = {"title": "u", "raw_configuration": {"id": "w2", "type": "list"}}
    sv, _out = await _collect_results([widget, [other], {"widgets": {"x": {"id": "w3"}}}], output_key="widgets")
    assert list(sv["$collected"]["widgets"]) == ["w1", "w2", "w3"]


# ── chat-path tool exposure ─────────────────────────────────────────────────


def test_chat_path_exposes_result_reading_tools():
    """Self-fan-out / research agents in chat can read results, not only collect."""
    defs, _executor = _build_handlers(session_vars={})
    names = {d.name for d in defs}
    assert "get_task_result" in names
    assert "get_full_task_result" in names
    # collect/clear remain available for the structured downstream-tool pattern.
    assert "collect_task_results" in names
    assert "clear_collected" in names
