"""GET /agents/{id}/memory as the Memory tab's data table reads it (#9378 slice 4f).

The route pages (``offset``), filters (repeated ``memory_type`` and
``source``, ``state``), searches (``q``: a text match on the content and the
subject, refused where the content is redacted) and sorts (``sort``,
``direction``). Every parameter is additive: a caller sending none gets the
newest entries first, as before.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.api.v1 import agent_memory as agent_memory_api


class _Result:
    def __init__(self, *, scalar=None):
        self._scalar = scalar

    def scalar(self):
        return self._scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: [])


class _DB:
    def __init__(self):
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(scalar=0)


def _owner_and_agent(*, company_managed: bool = False, role: str = "user"):
    user = SimpleNamespace(id=uuid.uuid4(), role=role, is_admin=role == "admin")
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None if company_managed else user.id,
        is_company_managed=company_managed,
    )
    return user, agent


async def _list(*, company_managed: bool = False, role: str = "user", **params) -> _DB:
    user, agent = _owner_and_agent(company_managed=company_managed, role=role)
    db = _DB()
    with patch.object(agent_memory_api, "get_user_agent", new=AsyncMock(return_value=agent)):
        await agent_memory_api.list_agent_memories(str(agent.id), user=user, db=db, **params)
    return db


def _sql(statement) -> str:
    return str(statement.compile(compile_kwargs={"literal_binds": True}))


@pytest.mark.asyncio
async def test_no_parameter_keeps_the_newest_first_page():
    count, page = (await _list()).statements
    sql = _sql(page)
    assert "ORDER BY agent_memory_entries.created_at DESC" in sql
    assert "LIMIT 50" in sql
    assert "superseded_at" not in _sql(count).split("WHERE", 1)[1]


@pytest.mark.asyncio
async def test_filters_apply_to_the_count_and_the_page():
    db = await _list(memory_type=["fact", "state"], source=["tool"], state="current")
    for statement in db.statements:
        sql = _sql(statement)
        assert "agent_memory_entries.memory_type IN ('fact', 'state')" in sql
        assert "agent_memory_entries.source IN ('tool')" in sql
        assert "agent_memory_entries.superseded_at IS NULL" in sql


@pytest.mark.asyncio
async def test_superseded_state_keeps_the_retired_entries():
    _, page = (await _list(state="superseded")).statements
    assert "agent_memory_entries.superseded_at IS NOT NULL" in _sql(page)


@pytest.mark.asyncio
async def test_search_reads_content_and_subject_on_both_queries():
    db = await _list(q="  footer  ")
    for statement in db.statements:
        sql = _sql(statement).lower()
        assert "lower(agent_memory_entries.content) like lower('%footer%')" in sql
        assert "lower(agent_memory_entries.subject_key) like lower('%footer%')" in sql


@pytest.mark.asyncio
async def test_search_is_refused_where_the_content_is_redacted():
    with pytest.raises(HTTPException) as refused:
        await _list(company_managed=True, q="footer")
    assert refused.value.status_code == 403


@pytest.mark.asyncio
async def test_a_redacted_reader_still_lists_with_filters():
    db = await _list(company_managed=True, memory_type=["fact"])
    assert len(db.statements) == 2


@pytest.mark.asyncio
async def test_page_offset_and_type_sort():
    _, page = (await _list(sort="type", direction="asc", limit=25, offset=50)).statements
    sql = _sql(page)
    assert (
        "ORDER BY agent_memory_entries.memory_type ASC, agent_memory_entries.created_at DESC, "
        "agent_memory_entries.id"
    ) in sql
    assert "LIMIT 25 OFFSET 50" in sql


@pytest.mark.asyncio
async def test_oldest_first():
    _, page = (await _list(direction="asc")).statements
    assert "ORDER BY agent_memory_entries.created_at ASC" in _sql(page)
