"""``get_shared_state(key="PREFIX*", path=...)`` reads one path across a key family.

A record split over sibling keys (``dev:harness-gaps:evidence-3`` to ``-12``)
cost one probe per key and a not-found error for each miss (gap
gap-evidence-overflow-scatter); a trailing ``*`` on the key answers every key
where the path resolves in one call and counts the others.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

import app.services.builtin_tools as builtin_tools_module
from app.tools.base import ToolCall


def _executor() -> Any:
    _defs, executor = builtin_tools_module.build_shared_state_tools(None, str(uuid.uuid4()), str(uuid.uuid4()))
    return executor


async def test_a_family_read_answers_every_key_holding_the_path(monkeypatch):
    from app.services import shared_state

    states = [
        {"key": "ev-3", "status": "active", "value": {"by_id": {"other": {"x": 1}}}},
        {"key": "ev-10", "status": "active", "value": {"by_id": {"g1": {"folded": ["a", "b"]}}}},
        {"key": "ev-12", "status": "active", "value": {"by_id": {"g1": {"last_seen": "20 Nov"}}}},
    ]
    calls: list[dict] = []

    async def _list_states(**kwargs):
        calls.append(kwargs)
        return [s for s in states if s["key"].startswith(kwargs["key_prefix"])]

    monkeypatch.setattr(shared_state, "list_states", _list_states)
    result = await _executor().execute_tool(
        ToolCall(id="a", name="get_shared_state", arguments={"key": "ev-*", "path": "by_id.g1"})
    )
    text = result.content if hasattr(result, "content") else str(result)
    assert "found in 2 of 3 active key(s)" in text
    assert "`ev-10`" in text and "folded" in text
    assert "`ev-12`" in text and "20 Nov" in text
    assert "`ev-3`" not in text
    assert calls[0]["key_prefix"] == "ev-" and calls[0]["status"] == "active"


async def test_a_family_past_one_page_is_searched_whole(monkeypatch):
    """A family of more keys than one page (53 dev:harness-gaps* keys) finds a path in the last key
    (gap-lookup-across-split-keys: the read stopped at the newest 50)."""
    from app.services import shared_state
    from app.services.builtin_tools import shared_state as tool_module

    states = [{"key": f"ev-{n}", "status": "active", "value": {"by_id": {}}} for n in range(130)]
    states[-1]["value"] = {"by_id": {"g1": {"evidence": "old key"}}}
    calls: list[dict] = []

    async def _list_states(**kwargs):
        calls.append(kwargs)
        start = kwargs.get("offset", 0)
        return states[start : start + kwargs["limit"]]

    monkeypatch.setattr(shared_state, "list_states", _list_states)
    result = await _executor().execute_tool(
        ToolCall(id="p", name="get_shared_state", arguments={"key": "ev-*", "path": "by_id.g1"})
    )
    text = result.content if hasattr(result, "content") else str(result)
    assert "found in 1 of 130 active key(s)" in text and "old key" in text
    assert "keys only" not in text
    assert [c["offset"] for c in calls] == [0, tool_module._FAMILY_PAGE_KEYS]


async def test_a_family_read_needs_a_path(monkeypatch):
    result = await _executor().execute_tool(ToolCall(id="b", name="get_shared_state", arguments={"key": "ev-*"}))
    text = result.content if hasattr(result, "content") else str(result)
    assert text.startswith("Error:") and "path" in text


async def test_a_long_part_is_cut_with_the_exact_read_named(monkeypatch):
    from app.services import shared_state

    async def _list_states(**_kwargs):
        return [{"key": "ev-1", "status": "active", "value": {"by_id": {"g1": "y" * 30_000}}}]

    monkeypatch.setattr(shared_state, "list_states", _list_states)
    result = await _executor().execute_tool(
        ToolCall(id="c", name="get_shared_state", arguments={"key": "ev-*", "path": "by_id.g1"})
    )
    text = result.content if hasattr(result, "content") else str(result)
    assert "get_shared_state(key='ev-1', path='by_id.g1') reads it whole" in text


async def test_a_family_read_applies_where_json_and_fields_to_each_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """where_json and fields filter the list under the path in each key (#1760)."""
    from app.services import shared_state

    states = [
        {"key": "g", "status": "active", "value": {"gaps": [{"id": "a", "n": 1}, {"id": "b", "n": 2}]}},
        {"key": "g:low", "status": "active", "value": {"gaps": [{"id": "c", "n": 3, "what": "long"}]}},
        {"key": "g:low-2", "status": "active", "value": {"gaps": [{"id": "d", "n": 4}]}},
    ]

    async def _list_states(**kwargs: Any) -> list[dict[str, Any]]:
        return [s for s in states if str(s["key"]).startswith(str(kwargs["key_prefix"]))]

    monkeypatch.setattr(shared_state, "list_states", _list_states)
    result = await _executor().execute_tool(
        ToolCall(
            id="d",
            name="get_shared_state",
            arguments={
                "key": "g*",
                "path": "gaps",
                "where_json": '[{"id": "b"}, {"id": "c"}]',
                "fields": ["id"],
            },
        )
    )
    text = result.content if hasattr(result, "content") else str(result)
    assert "found in 3 of 3 active key(s); where_json matched 2 item(s) in 2 key(s)" in text
    assert '[{"id": "b"}]' in text and '[{"id": "c"}]' in text
    assert '"a"' not in text and '"d"' not in text and "long" not in text
    assert "`g:low-2`" not in text
