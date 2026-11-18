"""list_shared_state narrows to one family of keys with key_prefix.

Finding the live ``dev:green-claim:*`` entries took a whole listing page
(about 50k characters) because the tool had no key filter. key_prefix is
passed to the store query (before the page is cut, so the total counts the
family alone) and, under a namespaced scope, applies inside the namespace.
"""

import uuid
from types import SimpleNamespace

import pytest

import app.services.shared_state as shared_state_service
from app.services.builtin_tools import build_shared_state_tools
from app.tools.base import ToolCall


async def _call(executor, name, /, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


def _fake_store(monkeypatch, rows: list[dict], calls: list[dict]) -> None:
    async def _list(**kwargs):
        calls.append(kwargs)
        prefix = kwargs.get("key_prefix") or ""
        kept = [r for r in rows if r["key"].startswith(prefix)]
        start, size = kwargs["offset"], kwargs["limit"]
        return kept[start : start + size]

    async def _count(**kwargs):
        prefix = kwargs.get("key_prefix") or ""
        return sum(1 for r in rows if r["key"].startswith(prefix))

    monkeypatch.setattr(shared_state_service, "list_states", _list)
    monkeypatch.setattr(shared_state_service, "count_states", _count)


def _row(key: str) -> dict:
    return {"key": key, "label": None, "status": "active", "is_stale": False}


@pytest.mark.asyncio
async def test_key_prefix_reaches_the_query_and_keeps_only_that_family(monkeypatch):
    rows = [_row("dev:green-claim:a@1"), _row("dev:open-prs"), _row("dev:green-claim:b@2")]
    calls: list[dict] = []
    _fake_store(monkeypatch, rows, calls)
    _defs, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))

    result = await _call(executor, "list_shared_state", key_prefix="dev:green-claim:")

    assert calls[0]["key_prefix"] == "dev:green-claim:"
    assert "dev:green-claim:a@1" in result.content
    assert "dev:green-claim:b@2" in result.content
    assert "dev:open-prs" not in result.content


@pytest.mark.asyncio
async def test_no_key_prefix_lists_as_before(monkeypatch):
    calls: list[dict] = []
    _fake_store(monkeypatch, [_row("dev:open-prs")], calls)
    _defs, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))

    result = await _call(executor, "list_shared_state")

    assert calls[0]["key_prefix"] is None
    assert "dev:open-prs" in result.content


@pytest.mark.asyncio
async def test_key_prefix_applies_inside_a_namespace(monkeypatch):
    rows = [_row("flow-run:r1:claim:x"), _row("flow-run:r1:other"), _row("claim:outside")]
    calls: list[dict] = []
    _fake_store(monkeypatch, rows, calls)
    _defs, executor = build_shared_state_tools(None, "user-1", "agent-1", key_namespace="flow-run:r1")

    result = await _call(executor, "list_shared_state", key_prefix="claim:")

    assert calls[0]["key_prefix"] == "flow-run:r1:claim:"
    assert "claim:x" in result.content
    assert "other" not in result.content
    assert "outside" not in result.content


def test_schema_offers_key_prefix():
    defs, _ = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    props = next(d for d in defs if d.name == "list_shared_state").input_schema["properties"]
    assert props["key_prefix"]["type"] == "string"
