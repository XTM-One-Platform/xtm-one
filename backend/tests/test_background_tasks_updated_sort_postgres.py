"""The Updated sort of Operations > Background Tasks, against a real PostgreSQL.

The Updated cell shows an open task's execution move, else the task's end,
start or creation. The sort ordered on the execution's ``updated_at`` alone,
nulls last both ways: a finished task (no execution row) sat last in both
directions, so ``last_progress`` and ``last_progress_asc`` answered the same
order. Both directions now order on the value the cell shows, the id breaking
ties the same way, so one is the exact reverse of the other.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.agent import Agent
from app.models.orchestration import OrchestrationExecution
from app.models.sub_agent_task import SubAgentTask
from app.models.user import User
from app.services import sub_agent
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_T0 = dt.datetime(2026, 11, 15, 9, 0, tzinfo=dt.UTC)


@pytest.mark.asyncio
async def test_the_updated_sort_ascending_is_the_reverse_of_descending() -> None:
    owner_id, agent_id = uuid.uuid4(), uuid.uuid4()
    ended, pending, idle = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async with scratch_database(
        "taskssort", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as scratch:
        async with scratch.sessions() as db:
            db.add(User(id=owner_id, email=f"{owner_id}@example.com", hashed_password="x", full_name="Owner"))
            await db.flush()
            db.add(Agent(id=agent_id, user_id=owner_id, name="Worker", slug=f"worker-{agent_id.hex[:8]}", persona="p"))
            await db.flush()
            common = dict(user_id=owner_id, parent_agent_id=agent_id, target_agent_id=agent_id)
            db.add_all(
                [
                    # Ended at 10:00, no execution row left: the cell shows its end.
                    SubAgentTask(
                        id=ended,
                        task_description="ended",
                        status="completed",
                        created_at=_T0,
                        started_at=_T0,
                        completed_at=_T0 + dt.timedelta(hours=1),
                        **common,
                    ),
                    # Pending, its execution moved at 11:00: the cell shows that move.
                    SubAgentTask(
                        id=pending,
                        task_description="pending",
                        status="pending",
                        created_at=_T0 - dt.timedelta(hours=1),
                        **common,
                    ),
                    # Pending without an execution, created at 09:30: the cell shows its creation.
                    SubAgentTask(
                        id=idle,
                        task_description="idle",
                        status="pending",
                        created_at=_T0 + dt.timedelta(minutes=30),
                        **common,
                    ),
                ]
            )
            await db.flush()
            db.add(
                OrchestrationExecution(
                    id=uuid.uuid4(),
                    adapter="sub_agent",
                    state="pending",
                    payload={"task_id": str(pending)},
                    dedup_key=f"sub_agent_task:{pending}",
                    updated_at=_T0 + dt.timedelta(hours=2),
                )
            )
            await db.commit()

        async with scratch.sessions() as db:
            newest = await sub_agent.list_background_tasks(db, requester_id=str(owner_id), sort="last_progress")
            oldest = await sub_agent.list_background_tasks(db, requester_id=str(owner_id), sort="last_progress_asc")

    by_move = [str(pending), str(ended), str(idle)]
    assert [t["task_id"] for t in newest] == by_move
    assert [t["task_id"] for t in oldest] == list(reversed(by_move))
