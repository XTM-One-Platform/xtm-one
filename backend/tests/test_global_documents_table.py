"""The documents page's data table reads GET /knowledge-bases/global/documents.

The route searches the file name and the knowledge base name (``q``), sorts by
the column the table offers (``sort``, ``direction``) and keeps newest first as
the tie-break, so a page boundary never moves under the user (#9378, slice 4).
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql

import app.api.v1.knowledge_bases as kb_mod


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def scalar_one(self):
        return self._rows[0]

    def scalars(self):
        return self


class _ScriptedDB:
    def __init__(self, *results):
        self._results = list(results)
        self.statements: list = []

    async def execute(self, stmt, *_args, **_kwargs):
        self.statements.append(stmt)
        return self._results.pop(0)


def _sql(stmt) -> str:
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


async def _list(monkeypatch, **params) -> _ScriptedDB:
    monkeypatch.setattr(kb_mod, "get_user_group_ids", AsyncMock(return_value=[]))
    db = _ScriptedDB(_Rows([0]), _Rows([]))
    await kb_mod.list_global_documents(
        limit=25,
        offset=0,
        status=None,
        kb_name=None,
        kb_ids=None,
        user=SimpleNamespace(id=uuid.uuid4(), is_admin=True),
        db=db,
        **params,
    )
    return db


@pytest.mark.asyncio
async def test_newest_first_by_default(monkeypatch):
    db = await _list(monkeypatch)
    page_sql = _sql(db.statements[1])
    assert "ORDER BY knowledge_documents.created_at DESC, knowledge_documents.id DESC" in page_sql


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sort", "direction", "expected"),
    [
        ("filename", "asc", "lower(knowledge_documents.filename) ASC"),
        ("knowledge_base", "desc", "lower(knowledge_bases.name) DESC"),
        ("status", "asc", "knowledge_documents.status ASC"),
        ("created_at", "asc", "knowledge_documents.created_at ASC"),
    ],
)
async def test_sorts_by_the_column_the_table_offers(monkeypatch, sort, direction, expected):
    db = await _list(monkeypatch, sort=sort, direction=direction)
    order = _sql(db.statements[1]).split("ORDER BY", 1)[1]
    assert order.strip().startswith(expected)
    # Newest first, then the id, break ties so a page boundary is stable.
    assert "knowledge_documents.id DESC" in order


@pytest.mark.asyncio
async def test_search_reads_the_file_name_and_the_knowledge_base_name(monkeypatch):
    db = await _list(monkeypatch, q="  50%_report ")
    for stmt in db.statements:  # the count and the page narrow alike
        sql = _sql(stmt)
        assert "knowledge_documents.filename ILIKE" in sql
        assert "knowledge_bases.name ILIKE" in sql
        params = stmt.compile(dialect=postgresql.dialect()).params
        # The words are trimmed and their LIKE wildcards escaped.
        assert r"%50\%\_report%" in params.values()


@pytest.mark.asyncio
async def test_blank_search_adds_no_clause(monkeypatch):
    db = await _list(monkeypatch, q="   ")
    assert "ILIKE" not in _sql(db.statements[0])
