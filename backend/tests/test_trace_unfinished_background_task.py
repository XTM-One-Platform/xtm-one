# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""inspect_my_traces on the id of a background task still running: its trace is
stored when its turn ends, so the refusal names the task's state instead of
answering "trace not found" as if the task never ran."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.services.builtin_tools import traces as traces_module

_USER = SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user")


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _Db:
    """Answers every query with *rows*, keeping the statements it was given."""

    def __init__(self, rows):
        self._rows = rows
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self._rows)


class _NoTraceStore:
    async def get_trace_detail(self, _db, **_kwargs):
        raise HTTPException(status_code=404, detail="Trace not found")


@pytest.fixture
def _no_trace(monkeypatch):
    async def _no_run(_db, _value, *, own_agent_id):
        return None, None

    monkeypatch.setattr(traces_module, "_own_trace_of_run", _no_run)
    monkeypatch.setattr(traces_module, "build_trace_read_store", lambda: _NoTraceStore())


@pytest.mark.asyncio
async def test_a_running_task_id_is_answered_with_its_state(_no_trace):
    me = str(uuid.uuid4())
    task_id = uuid.uuid4()
    started = datetime(2026, 11, 19, 19, 0, tzinfo=UTC)
    db = _Db([(task_id, "running", started)])

    trace, refusal = await traces_module._own_trace_by_id(db, str(task_id), user=_USER, group_ids=[], own_agent_id=me)

    assert trace is None
    assert f"Background task {task_id} is running since 2026-11-19T19:00:00Z" in refusal
    assert "check_task_status" in refusal
    sql = str(db.statements[-1].compile(dialect=postgresql.dialect()))
    # Only the tasks this agent spawned or runs, and only unfinished ones.
    assert "sub_agent_tasks.parent_agent_id" in sql and "sub_agent_tasks.target_agent_id" in sql
    assert "sub_agent_tasks.status IN" in sql


@pytest.mark.asyncio
async def test_a_pending_task_prefix_is_answered_with_its_state(_no_trace):
    task_id = uuid.uuid4()
    db = _Db([(task_id, "pending", None)])

    trace, refusal = await traces_module._own_trace_by_id(
        db, str(task_id)[:8], user=_USER, group_ids=[], own_agent_id=str(uuid.uuid4())
    )

    assert trace is None
    assert f"Background task {task_id} is pending:" in refusal


@pytest.mark.asyncio
async def test_an_id_of_no_unfinished_task_keeps_trace_not_found(_no_trace):
    trace, refusal = await traces_module._own_trace_by_id(
        _Db([]), str(uuid.uuid4()), user=_USER, group_ids=[], own_agent_id=str(uuid.uuid4())
    )

    assert trace is None
    assert refusal == "Error: trace not found among this agent's traces."


@pytest.mark.asyncio
async def test_a_failing_task_lookup_keeps_trace_not_found(_no_trace):
    class _Broken(_Db):
        async def execute(self, statement):
            raise RuntimeError("no database here")

    trace, refusal = await traces_module._own_trace_by_id(
        _Broken([]), str(uuid.uuid4()), user=_USER, group_ids=[], own_agent_id=str(uuid.uuid4())
    )

    assert trace is None
    assert refusal == "Error: trace not found among this agent's traces."
