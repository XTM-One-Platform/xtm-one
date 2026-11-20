"""cancel / reschedule_scheduled_work_item take the 8+ hex prefix other tools take.

The prefix is looked up among the person's scheduled items only (a range on
``work_items.id`` with the person and ``action_mode`` clauses), one match
stands for the full id handed to the service, several are refused with their
ids and nothing is changed, none leaves the value as given so the service
answers its usual refusal. The same lookup against PostgreSQL is in
``test_scheduled_work_items_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

import app.core.database as database
import app.services.builtin_tools.scheduling as scheduling
from app.services import scheduled_work_items as swi
from app.services.builtin_tools import build_scheduling_tools
from app.tools.base import CallableToolExecutor, ToolCall

USER = str(uuid.uuid4())
AGENT = str(uuid.uuid4())
ITEM = uuid.UUID("90f1fe59-1234-4abc-8def-0123456789ab")


class _Rows:
    def __init__(self, rows: list[tuple[uuid.UUID, str]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[uuid.UUID, str]]:
        return self._rows


class _FakeSession:
    def __init__(self, rows: list[tuple[uuid.UUID, str]]) -> None:
        self.rows = rows
        self.statements: list[str] = []

    async def execute(self, stmt: Any) -> _Rows:
        dialect: Any = postgresql.dialect
        compiled = stmt.compile(dialect=dialect(), compile_kwargs={"literal_binds": True})
        self.statements.append(str(compiled))
        return _Rows(self.rows)


def _patch(monkeypatch: pytest.MonkeyPatch, rows: list[tuple[uuid.UUID, str]]) -> tuple[_FakeSession, list[str]]:
    session = _FakeSession(rows)

    @asynccontextmanager
    async def _session() -> AsyncIterator[_FakeSession]:
        yield session

    monkeypatch.setattr(database, "async_session", _session)
    calls: list[str] = []

    async def _cancel(item_id: str, **_kwargs: Any) -> dict[str, str]:
        calls.append(item_id)
        return {"id": item_id, "title": "Re-run the merge"}

    async def _reschedule(item_id: str, _due: Any, **_kwargs: Any) -> dict[str, str]:
        calls.append(item_id)
        return {"id": item_id, "title": "Re-run the merge"}

    async def _no_timezone(_user_id: str) -> None:
        return None

    monkeypatch.setattr(swi, "cancel_scheduled_work_item", _cancel)
    monkeypatch.setattr(swi, "reschedule_scheduled_work_item", _reschedule)
    monkeypatch.setattr(swi, "scheduling_enabled", lambda: True)
    monkeypatch.setattr(scheduling, "_timezone", _no_timezone)
    return session, calls


def _executor() -> CallableToolExecutor:
    _definitions, executor = build_scheduling_tools(user_id=USER, agent_id=AGENT, agent_name="Desk")
    return executor


async def _call(name: str, **arguments: str) -> str:
    result = await _executor().execute_tool(ToolCall(id="c1", name=name, arguments=arguments))
    return str(result.content)


@pytest.mark.asyncio
async def test_a_prefix_cancels_the_one_scheduled_item_it_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    session, calls = _patch(monkeypatch, [(ITEM, "")])
    answer = await _call("cancel_scheduled_work_item", work_item_id="90f1fe59")
    assert calls == [str(ITEM)]
    assert "Cancelled" in answer and str(ITEM) in answer
    lookup = session.statements[0]
    # Only the person's scheduled items are searched, by a range on the id.
    assert USER in lookup and "action_mode IS NOT NULL" in lookup
    assert "BETWEEN" in lookup


@pytest.mark.asyncio
async def test_an_ambiguous_prefix_changes_nothing_and_names_the_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = uuid.UUID("90f1fe59-ffff-4abc-8def-0123456789ab")
    _session, calls = _patch(monkeypatch, [(ITEM, ""), (other, "")])
    answer = await _call("cancel_scheduled_work_item", work_item_id="90f1fe59")
    assert calls == []
    assert answer.startswith("Error:") and str(ITEM) in answer and str(other) in answer
    assert "Nothing was done" in answer


@pytest.mark.asyncio
async def test_an_unknown_prefix_is_handed_on_as_given(monkeypatch: pytest.MonkeyPatch) -> None:
    _session, calls = _patch(monkeypatch, [])
    await _call("cancel_scheduled_work_item", work_item_id="90f1fe59")
    # The service is asked with the value as given and answers its own refusal.
    assert calls == ["90f1fe59"]


@pytest.mark.asyncio
async def test_a_full_id_reads_nothing_more(monkeypatch: pytest.MonkeyPatch) -> None:
    session, calls = _patch(monkeypatch, [])
    await _call("cancel_scheduled_work_item", work_item_id=str(ITEM))
    assert calls == [str(ITEM)]
    assert session.statements == []


@pytest.mark.asyncio
async def test_a_prefix_reschedules_the_one_scheduled_item_it_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    _session, calls = _patch(monkeypatch, [(ITEM, "")])
    due = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    answer = await _call("reschedule_scheduled_work_item", work_item_id="90f1fe59", due_at=due)
    assert calls == [str(ITEM)]
    assert "Rescheduled" in answer
