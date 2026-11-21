"""``get_shared_state(key="PREFIX*", path=...)`` reads one path across a key family.

A record split over sibling keys (``dev:harness-gaps:evidence-3`` to ``-12``)
cost one probe per key and a not-found error for each miss (gap
gap-evidence-overflow-scatter); a trailing ``*`` on the key answers every key
where the path resolves in one call and counts the others.
"""

from __future__ import annotations

import uuid

import app.services.builtin_tools as builtin_tools_module
from app.tools.base import ToolCall


def _executor():
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
