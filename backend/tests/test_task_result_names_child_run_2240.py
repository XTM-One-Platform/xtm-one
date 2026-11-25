"""The result tools name the child task's own run (#2240).

A parent that notes a delegate's verdict (a CTEM case note) can only cite the
run that produced it when the result tool hands that run over; before, the
parent saw the task id and its own run alone.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

from app.services import sub_agent
from tests.test_sub_agent_result_files import _handlers


def _payload(status: str, run_id: str | None, task_id: str) -> dict:
    return {
        "task_id": task_id,
        "run_id": run_id,
        "status": status,
        "result": "relevant: score 72" if status == "completed" else None,
        "error": "timed out" if status == "failed" else None,
        "result_file_ids": [],
    }


async def test_get_task_result_names_the_child_run_of_a_completed_task():
    handlers = _handlers()
    tid, run = str(uuid.uuid4()), str(uuid.uuid4())
    with patch.object(sub_agent, "get_task_result", new=AsyncMock(return_value=_payload("completed", run, tid))):
        out = await handlers["get_task_result"](tid)
    assert f"{tid[:12]}: completed (18 chars), run {run}" in out


async def test_get_task_result_names_the_child_run_of_a_failed_task():
    handlers = _handlers()
    tid, run = str(uuid.uuid4()), str(uuid.uuid4())
    with patch.object(sub_agent, "get_task_result", new=AsyncMock(return_value=_payload("failed", run, tid))):
        summary = await handlers["get_task_result"](tid)
        full = await handlers["get_full_task_result"](tid)
    assert f"{tid[:12]}: failed, run {run}" in summary
    assert full.startswith(f"Task failed: timed out, run {run}")


async def test_no_run_line_when_the_task_has_no_run_of_its_own():
    handlers = _handlers()
    tid = str(uuid.uuid4())
    with patch.object(sub_agent, "get_task_result", new=AsyncMock(return_value=_payload("completed", None, tid))):
        out = await handlers["get_task_result"](tid)
    assert ", run " not in out
    # A run id equal to the task id names nothing new.
    with patch.object(sub_agent, "get_task_result", new=AsyncMock(return_value=_payload("completed", tid, tid))):
        out = await handlers["get_task_result"](tid)
    assert ", run " not in out


async def test_a_completed_full_answer_stays_exactly_as_stored():
    """A caller-fixed JSON answer must still parse: nothing is appended to it."""
    handlers = _handlers()
    tid, run = str(uuid.uuid4()), str(uuid.uuid4())
    with patch.object(sub_agent, "get_task_result", new=AsyncMock(return_value=_payload("completed", run, tid))):
        out = await handlers["get_full_task_result"](tid)
    assert out == "relevant: score 72"
