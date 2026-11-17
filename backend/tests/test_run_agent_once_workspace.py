"""``run_agent_once`` gives its run a run of its own, hence a sandbox workspace (#12028).

A one-off run used to have neither a conversation nor an assignment run, so
``WorkspaceScope.build`` opened no workspace: a file code_interpreter wrote
under ``workspace/`` was dropped at the end of the call and a ``$workspace:``
reference was refused. The task now creates an ``AssignmentRun`` (trigger
``run_once``, the caller's run as parent when there is one) and runs under it.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.assignment import AssignmentRun
from app.services.sandbox_workspace.store import WorkspaceScope
from app.services.sub_agent import (
    RUN_ONCE_TRIGGER_TYPE,
    RUN_TRIGGER_TYPE_KEY,
    create_sub_agent_task,
)


class _SessionCM:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc):
        return False


def _own_db(added: list) -> MagicMock:
    db = MagicMock()
    db.add = MagicMock(side_effect=added.append)

    async def _flush():
        # The database's job: an id on every new row.
        for row in added:
            if getattr(row, "id", None) is None:
                row.id = uuid.uuid4()

    db.flush = AsyncMock(side_effect=_flush)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    return db


async def _create(**kwargs) -> tuple[list, dict]:
    added: list = []
    create_execution = AsyncMock()
    with (
        patch("app.core.database.async_session", lambda: _SessionCM(_own_db(added))),
        patch("app.services.orchestration_engine.create_execution", new=create_execution),
        patch("app.services.orchestration_engine.notify_orchestration_wake", new=AsyncMock()),
        patch("app.services.delegation.log_delegation", new=AsyncMock()),
    ):
        agent = str(uuid.uuid4())
        task = await create_sub_agent_task(
            None,
            user_id=str(uuid.uuid4()),
            parent_agent_id=agent,
            target_agent_id=agent,
            task_description="Write workspace/out.json, then validate $workspace:out.json",
            allow_self=True,
            **kwargs,
        )
    return added, {"task": task, "payload": create_execution.await_args.kwargs["payload"]}


@pytest.mark.asyncio
async def test_a_run_once_task_gets_a_run_and_so_a_workspace():
    added, out = await _create(own_run_trigger=RUN_ONCE_TRIGGER_TYPE)

    runs = [row for row in added if isinstance(row, AssignmentRun)]
    assert len(runs) == 1, "the one-off task is a run of the target agent"
    run = runs[0]
    assert run.assignment_id is None
    assert run.parent_run_id is None
    assert run.trigger_payload["trigger_type"] == "run_once"
    assert run.messages == [{"role": "user", "content": out["task"].task_description}]

    run_id = out["payload"]["assignment_run_id"]
    assert run.id is not None and run_id == str(run.id)
    assert out["task"].metadata_json[RUN_TRIGGER_TYPE_KEY] == "run_once"
    # The worker runs the turn under this run: the scope a workspace keys on.
    scope = WorkspaceScope.build(user_id=uuid.uuid4(), conversation_id=None, assignment_run_id=run_id)
    assert scope is not None and str(scope.assignment_run_id) == run_id


@pytest.mark.asyncio
async def test_a_run_once_task_called_from_a_run_is_its_child():
    parent = str(uuid.uuid4())
    added, out = await _create(own_run_trigger=RUN_ONCE_TRIGGER_TYPE, parent_run_id=parent)

    run = next(row for row in added if isinstance(row, AssignmentRun))
    assert str(run.parent_run_id) == parent
    assert run.trigger_payload["trigger_type"] == "run_once"
    assert out["payload"]["parent_run_id"] == parent


@pytest.mark.asyncio
async def test_a_chat_spawn_still_has_no_run():
    added, out = await _create()

    assert not [row for row in added if isinstance(row, AssignmentRun)]
    assert out["payload"]["assignment_run_id"] is None
    assert RUN_TRIGGER_TYPE_KEY not in out["task"].metadata_json


@pytest.mark.asyncio
async def test_a_delegation_keeps_its_trigger_type():
    added, _out = await _create(parent_run_id=str(uuid.uuid4()))

    run = next(row for row in added if isinstance(row, AssignmentRun))
    assert run.trigger_payload["trigger_type"] == "delegation"
