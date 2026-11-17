"""create_work_item lands a carrier item finished in one call (#10190).

A run that merges a slice files an item only to carry its fleet_impact. Created
``open`` with an owner, the owner's inbox pre-claimed it at once and the
creating run's complete_work_item was refused ("held by run X"). With
``status`` done or cancelled the tool creates the item held by its own run
(never dispatchable) and completes it with ``result`` in the same call.
"""

import uuid
from types import SimpleNamespace

import pytest

import app.services.work_item as wi_service
from app.services.builtin_tools import build_work_item_tools
from app.tools.base import ToolCall


async def _call(executor, name, /, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


def _item(**extra) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "title": "Carrier",
        "status": "claimed",
        "priority": "medium",
        "owner_agent_id": str(uuid.uuid4()),
        "correlation_key": "dev:x-1",
        **extra,
    }


@pytest.fixture
def store(monkeypatch):
    calls: dict = {"create": [], "complete": []}
    created = _item()

    async def _create(**kwargs):
        calls["create"].append(kwargs)
        return {**created, "status": kwargs["status"]}

    async def _complete(work_item_id, **kwargs):
        calls["complete"].append({"id": work_item_id, **kwargs})
        return {**created, "status": kwargs["status"], "result": kwargs["result"]}

    monkeypatch.setattr(wi_service, "create_work_item", _create)
    monkeypatch.setattr(wi_service, "complete_work_item", _complete)
    return calls


def _executor():
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    return executor


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["done", "cancelled"])
async def test_a_finished_create_is_held_then_completed(store, status):
    result = await _call(
        _executor(),
        "create_work_item",
        title="Carrier",
        owner_agent_id=str(uuid.uuid4()),
        status=status,
        result="merged abc123",
    )
    assert store["create"][0]["status"] == "claimed", "never created open, so no inbox dispatches it"
    done = store["complete"][0]
    assert done["status"] == status
    assert done["result"] == "merged abc123"
    assert f"status: {status}" in result.content
    assert "Created finished: no inbox picks it up" in result.content


@pytest.mark.asyncio
async def test_an_open_create_completes_nothing(store):
    await _call(_executor(), "create_work_item", title="Work", owner_agent_id=str(uuid.uuid4()))
    assert store["create"][0]["status"] == "open"
    assert store["complete"] == []


@pytest.mark.asyncio
async def test_a_deduplicated_create_completes_nothing(monkeypatch, store):
    async def _found(**_kwargs):
        return {**_item(status="in_progress"), "deduplicated": True, "dedupe_by": "key"}

    monkeypatch.setattr(wi_service, "create_work_item", _found)
    result = await _call(
        _executor(), "create_work_item", title="Carrier", correlation_key="dev:x-1", dedupe=True, status="done"
    )
    assert result.content.startswith("Not created")
    assert store["complete"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [
        {"blocked_on": [str(uuid.uuid4())]},
        {"blocked_reason": "waits for a person"},
        {"needs_human": True},
        {"blocked_on_refs": [{"kind": "pull_request", "repo": "o/r", "number": 1}]},
    ],
)
async def test_a_finished_create_that_waits_is_refused(store, extra):
    result = await _call(_executor(), "create_work_item", title="Carrier", status="done", **extra)
    assert "waits for nothing" in result.content
    assert next(iter(extra)) in result.content
    assert store["create"] == []


@pytest.mark.asyncio
async def test_a_result_without_a_finished_status_is_refused(store):
    result = await _call(_executor(), "create_work_item", title="Work", result="nothing yet")
    assert "result is only taken with status done or cancelled" in result.content
    assert store["create"] == []


@pytest.mark.asyncio
async def test_a_completion_that_fails_is_named(monkeypatch, store):
    async def _none(_work_item_id, **_kwargs):
        return None

    monkeypatch.setattr(wi_service, "complete_work_item", _none)
    result = await _call(_executor(), "create_work_item", title="Carrier", status="done", result="r")
    assert "was created but not completed" in result.content


def test_the_schema_offers_the_finished_statuses_and_result():
    defs, _ = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    schema = next(d for d in defs if d.name == "create_work_item").input_schema["properties"]
    assert {"done", "cancelled"} <= set(schema["status"]["enum"])
    assert "result" in schema
