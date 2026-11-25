"""An agent can stop a background task it spawned (#2217).

The UI's stop button had no agent-facing twin: a task the user asked the agent
to drop kept running and spending until it ended. ``cancel_background_task``
wraps the same durable cancel, scoped to the acting user's tasks spawned by
this agent.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services import sub_agent as sub_agent_mod
from app.services.builtin_tools import handover


def _build(user_id: str, agent_id: str) -> Any:
    built = handover.build_sub_agent_tools(
        MagicMock(),
        user_id=user_id,
        parent_agent_id=agent_id,
        handover_agents=[
            SimpleNamespace(
                id=uuid.uuid4(),
                name="Peer",
                slug="peer",
                description="",
                persona="",
                is_company_managed=False,
            )
        ],
        conversation_id=str(uuid.uuid4()),
        parent_run_id=str(uuid.uuid4()),
    )
    assert built is not None
    return built


def test_the_tool_is_offered_with_the_other_background_task_tools() -> None:
    definitions, executor = _build(str(uuid.uuid4()), str(uuid.uuid4()))
    names = [d.name for d in definitions]
    assert "cancel_background_task" in names
    assert "cancel_background_task" in executor._handlers
    (definition,) = [d for d in definitions if d.name == "cancel_background_task"]
    assert definition.input_schema["required"] == ["task_ids"]


@pytest.mark.asyncio
async def test_the_tool_cancels_through_the_ui_path_scoped_to_this_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id, agent_id = str(uuid.uuid4()), str(uuid.uuid4())
    running, done, foreign = (str(uuid.uuid4()) for _ in range(3))
    calls: list[tuple[str, str, bool, str | None]] = []

    async def _cancel(
        task_id: str, requester_id: str, *, is_admin: bool = False, spawned_by: str | None = None
    ) -> dict[str, Any] | None:
        calls.append((task_id, requester_id, is_admin, spawned_by))
        if task_id == running:
            return {"task_id": task_id, "status": "cancelled", "was_running": True}
        if task_id == done:
            return {"task_id": task_id, "status": "completed", "already_terminal": True}
        return None

    monkeypatch.setattr(sub_agent_mod, "cancel_sub_agent_task", _cancel)
    _defs, executor = _build(user_id, agent_id)

    answer = await executor._handlers["cancel_background_task"](f"{running}, {done},{foreign}, nope")

    lines = answer.splitlines()
    assert lines[0] == f"{running}: cancelled (was running)"
    assert lines[1] == f"{done}: already completed, unchanged"
    assert lines[2] == f"{foreign}: not found among your background tasks"
    assert "nope" in lines[3]
    # Never as an administrator, always narrowed to this agent's own tasks.
    assert {(c[1], c[2], c[3]) for c in calls} == {(user_id, False, agent_id)}


class _Result:
    def __init__(self, row: object) -> None:
        self._row = row

    def scalar_one_or_none(self) -> object:
        return self._row


class _Session:
    def __init__(self, row: object) -> None:
        self._row = row

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def execute(self, *_a: object, **_k: object) -> _Result:
        return _Result(self._row)


@pytest.mark.asyncio
async def test_spawned_by_hides_a_task_another_agent_spawned(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import database

    user = uuid.uuid4()
    spawner, other = uuid.uuid4(), uuid.uuid4()
    task = SimpleNamespace(id=uuid.uuid4(), user_id=user, parent_agent_id=spawner, status="completed")
    monkeypatch.setattr(database, "async_session", lambda: _Session(task))

    refused = await sub_agent_mod.cancel_sub_agent_task(str(task.id), str(user), spawned_by=str(other))
    assert refused is None

    own = await sub_agent_mod.cancel_sub_agent_task(str(task.id), str(user), spawned_by=str(spawner))
    assert own == {"task_id": str(task.id), "status": "completed", "already_terminal": True}

    # The UI path (no spawned_by) keeps the owner gate alone.
    ui = await sub_agent_mod.cancel_sub_agent_task(str(task.id), str(user))
    assert ui is not None and ui["already_terminal"] is True
