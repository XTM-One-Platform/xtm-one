"""create_work_item(hold=true) keeps a new item out of the queue until its creator releases it (#11515).

A run that files an item it finishes in later calls (a note, more attachments,
a blocker known after the create) had no way to keep it from its owner's
inbox: created open with an owner, it was claimed within seconds and the
owner's run started on an incomplete item. Held, it is created claimed by the
creating run (never dispatchable); update_work_item status open from that run
releases it, and the ended-run sweep releases it if the run ends first.
"""

import uuid
from types import SimpleNamespace

import pytest

import app.services.work_item as wi_service
from app.services import work_item_dispatch_wake as wake
from app.services.builtin_tools import build_work_item_tools
from app.services.builtin_tools.work_items import _hold_create_refusal
from app.services.work_item import WorkItemOrigin, creator_claim_columns
from app.tools.base import ToolCall

RUN = uuid.uuid4()


async def _call(executor, name, /, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


@pytest.fixture
def store(monkeypatch):
    calls: dict = {"create": []}

    async def _create(**kwargs):
        calls["create"].append(kwargs)
        return {
            "id": str(uuid.uuid4()),
            "title": kwargs["title"],
            "status": kwargs["status"],
            "priority": "medium",
            "owner_agent_id": kwargs.get("owner_agent_id"),
        }

    monkeypatch.setattr(wi_service, "create_work_item", _create)
    return calls


def _executor(run_id=RUN):
    origin = WorkItemOrigin(run_id=str(run_id)) if run_id else None
    _, executor = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()), origin=origin)
    return executor


@pytest.mark.asyncio
async def test_a_held_create_is_created_claimed_by_its_run(store):
    result = await _call(_executor(), "create_work_item", title="Slice", owner_agent_id=str(uuid.uuid4()), hold=True)
    assert store["create"][0]["status"] == "claimed", "never created open, so no inbox dispatches it"
    assert "Held by this run" in result.content
    assert 'update_work_item(status="open")' in result.content


@pytest.mark.asyncio
async def test_an_unheld_create_stays_open(store):
    await _call(_executor(), "create_work_item", title="Slice", owner_agent_id=str(uuid.uuid4()))
    assert store["create"][0]["status"] == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ({"status": "in_progress"}, "only taken with status open"),
        ({"blocked_reason": "waits for a person"}, "created blocked"),
        ({"needs_human": True}, "created blocked"),
        ({"blocked_on": [str(uuid.uuid4())]}, "created blocked"),
    ],
)
async def test_a_hold_that_cannot_apply_is_refused(store, extra, needle):
    result = await _call(_executor(), "create_work_item", title="Slice", hold=True, **extra)
    assert needle in result.content
    assert store["create"] == []


@pytest.mark.asyncio
async def test_a_hold_outside_a_run_is_refused(store):
    result = await _call(_executor(run_id=None), "create_work_item", title="Slice", hold=True)
    assert "needs a run or background task" in result.content
    assert store["create"] == []


def test_the_refusal_helper_accepts_an_open_item_with_a_holder():
    assert _hold_create_refusal("open", None, None, "", False, holder=RUN) is None


def test_the_held_item_carries_the_creating_run_claim():
    # The claim the service stores for a held create: the ended-run sweep
    # (release_ended_run_claims) puts it back to open if the run never releases it.
    now = wi_service.datetime.now(wi_service.UTC)
    columns = creator_claim_columns("claimed", uuid.uuid4(), WorkItemOrigin(run_id=str(RUN)), now)
    assert columns["claimed_by_run_id"] == RUN


def test_the_release_to_open_wakes_the_dispatcher_and_the_held_create_does_not():
    released = [("work_item.status_changed", {"status": "open"})]
    created_held = [("work_item.created", {"status": "claimed"})]
    assert wake.reopens(released)
    assert not wake.reopens(created_held)


def test_the_schema_offers_hold():
    defs, _ = build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    schema = next(d for d in defs if d.name == "create_work_item").input_schema["properties"]
    assert schema["hold"]["type"] == "boolean"
