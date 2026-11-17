"""An agent cancels its scheduled one-off from the work-item tools.

``complete_work_item`` and ``update_work_item`` refuse a scheduled item (one
with an action): the platform runs it at its time. The refusal names the
scheduling tools every agent run holds, and ``complete_work_item`` with
status ``cancelled`` cancels the item through the audited scheduled-item
cancel, under that cancel's own authority rule.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.services.scheduled_work_items as swi
import app.services.work_item as wi_service
from app.services.builtin_tools import build_work_item_tools
from app.tools.base import ToolCall

ITEM_ID = "3c7ffc63-b2f6-4af3-a7bc-b2c2d4c97bba"


async def _call(executor, name: str, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


def _scheduled_item():
    return SimpleNamespace(id=uuid.UUID(ITEM_ID), title="48h trace check", action_mode="instruction")


def _refusal() -> swi.ScheduledWorkItemError:
    with pytest.raises(swi.ScheduledWorkItemError) as caught:
        wi_service._refuse_scheduled(_scheduled_item(), "completed")
    return caught.value


def test_the_refusal_names_the_tools_an_agent_holds():
    message = str(_refusal())
    assert f"cancel_scheduled_work_item(work_item_id='{ITEM_ID}')" in message
    assert "reschedule_scheduled_work_item" in message
    assert "complete_work_item with status 'cancelled'" in message
    assert "the scheduling tools" not in message


def test_an_item_without_an_action_is_not_refused():
    wi_service._refuse_scheduled(SimpleNamespace(id=uuid.uuid4(), title="x", action_mode=None), "completed")


def _tools(user_id: str, agent_id: str):
    return build_work_item_tools(SimpleNamespace(), user_id, agent_id)


@pytest.mark.asyncio
async def test_complete_with_status_cancelled_cancels_through_the_scheduled_cancel(monkeypatch):
    user_id, agent_id = str(uuid.uuid4()), str(uuid.uuid4())
    seen: dict = {}

    async def _complete(*_args, **_kwargs):
        raise _refusal()

    async def _cancel(item_id, *, user_id, agent_id=None, via="agent"):
        seen.update(item_id=item_id, user_id=user_id, agent_id=agent_id, via=via)
        return {"id": item_id, "title": "48h trace check", "status": "cancelled"}

    monkeypatch.setattr(wi_service, "complete_work_item", _complete)
    monkeypatch.setattr(swi, "cancel_scheduled_work_item", _cancel)
    _, executor = _tools(user_id, agent_id)

    result = await _call(executor, "complete_work_item", work_item_id=ITEM_ID, status="cancelled")

    assert not result.is_error, result.content
    assert "cancelled before it ran" in result.content
    assert seen == {"item_id": ITEM_ID, "user_id": user_id, "agent_id": agent_id, "via": "complete_work_item"}


@pytest.mark.asyncio
async def test_an_agent_the_cancel_rule_refuses_keeps_the_refusal(monkeypatch):
    async def _complete(*_args, **_kwargs):
        raise _refusal()

    async def _cancel(*_args, **_kwargs):
        return None  # not the scheduling agent, no handover, not a leftover

    monkeypatch.setattr(wi_service, "complete_work_item", _complete)
    monkeypatch.setattr(swi, "cancel_scheduled_work_item", _cancel)
    _, executor = _tools(str(uuid.uuid4()), str(uuid.uuid4()))

    result = await _call(executor, "complete_work_item", work_item_id=ITEM_ID, status="cancelled")

    assert result.is_error
    assert "is a scheduled work item" in result.content


@pytest.mark.asyncio
async def test_complete_done_on_a_scheduled_item_is_still_refused(monkeypatch):
    called = []

    async def _complete(*_args, **_kwargs):
        raise _refusal()

    async def _cancel(*_args, **_kwargs):
        called.append(True)

    monkeypatch.setattr(wi_service, "complete_work_item", _complete)
    monkeypatch.setattr(swi, "cancel_scheduled_work_item", _cancel)
    _, executor = _tools(str(uuid.uuid4()), str(uuid.uuid4()))

    result = await _call(executor, "complete_work_item", work_item_id=ITEM_ID, status="done")

    assert result.is_error
    assert "cancel_scheduled_work_item" in result.content
    assert called == []


@pytest.mark.asyncio
async def test_a_running_item_answers_the_cancel_refusal(monkeypatch):
    async def _complete(*_args, **_kwargs):
        raise _refusal()

    async def _cancel(*_args, **_kwargs):
        raise swi.ScheduledWorkItemError("'48h trace check' is running now and can no longer be cancelled.")

    monkeypatch.setattr(wi_service, "complete_work_item", _complete)
    monkeypatch.setattr(swi, "cancel_scheduled_work_item", _cancel)
    _, executor = _tools(str(uuid.uuid4()), str(uuid.uuid4()))

    result = await _call(executor, "complete_work_item", work_item_id=ITEM_ID, status="cancelled")

    assert result.is_error
    assert "running now" in result.content
