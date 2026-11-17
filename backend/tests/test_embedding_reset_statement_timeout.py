"""Regression tests for the KB embedding-reset statement timeout.

When the platform embedding model / provider / dimensions change, the AI
Models save calls ``_reset_kb_embeddings_for_new_model`` which runs a single
bulk ``DELETE FROM embeddings``. Under the global 30 s ``statement_timeout``
(see ``core/database.py``) that delete is cancelled on large installs
(``asyncpg.exceptions.QueryCanceledError: canceling statement due to
statement timeout``) and the save returns a 500.

The fix raises the per-transaction ``statement_timeout`` for that one
operation (transaction-local, auto-reset on COMMIT) before issuing the
purge. These tests lock in that ordering and the configurable value.
"""

import uuid

import pytest

from app.api.v1.settings import _reset_kb_embeddings_for_new_model
from app.core import config as config_module


class _FakeResult:
    def __init__(self, rows=None, rowcount=0):
        self._rows = rows or []
        self.rowcount = rowcount

    def all(self):
        return list(self._rows)


class _FakeConn:
    """Stand-in for the AsyncConnection returned by ``db.connection()``."""

    def __init__(self, owner: "_FakeDB"):
        self._owner = owner

    async def execute(self, stmt, params=None):
        self._owner.calls.append(str(stmt))
        self._owner.conn_calls.append((str(stmt), params))
        return _FakeResult()


class _FakeDB:
    def __init__(self, *, doc_ids=None, deleted=0, fail_connection=False):
        self.calls: list[str] = []
        self.conn_calls: list[tuple[str, dict | None]] = []
        self._doc_ids = doc_ids if doc_ids is not None else [uuid.uuid4()]
        self._deleted = deleted
        self._fail_connection = fail_connection
        self._conn = _FakeConn(self)

    async def connection(self):
        if self._fail_connection:
            raise RuntimeError("connection unavailable")
        return self._conn

    async def execute(self, stmt, params=None):
        s = str(stmt)
        self.calls.append(s)
        low = s.lower().lstrip()
        if low.startswith("select"):
            return _FakeResult(rows=[(d,) for d in self._doc_ids])
        if low.startswith("delete"):
            return _FakeResult(rowcount=self._deleted)
        return _FakeResult()


def _index_of(calls: list[str], needle: str) -> int:
    needle = needle.lower()
    for i, c in enumerate(calls):
        if needle in c.lower():
            return i
    raise AssertionError(f"no call containing {needle!r} in {calls!r}")


def _set_config_params(db: _FakeDB) -> dict:
    for sql, params in db.conn_calls:
        if "statement_timeout" in sql.lower():
            return params or {}
    raise AssertionError(f"no set_config statement_timeout call in {db.conn_calls!r}")


@pytest.mark.asyncio
async def test_reset_raises_statement_timeout_before_delete(monkeypatch):
    """The timeout bump MUST be issued before the bulk embedding delete."""
    monkeypatch.setattr(
        config_module.settings, "embedding_reset_statement_timeout_secs", 600
    )
    db = _FakeDB(deleted=7)

    deleted, doc_ids = await _reset_kb_embeddings_for_new_model(
        db, target_embedding_model="text-embedding-3-large"
    )

    assert deleted == 7
    assert len(doc_ids) == 1

    timeout_idx = _index_of(db.calls, "statement_timeout")
    delete_idx = _index_of(db.calls, "delete from embeddings")
    assert timeout_idx < delete_idx, (
        "statement_timeout MUST be raised before the embedding purge so the "
        f"DELETE isn't cancelled by the 30s global cap; got {db.calls!r}"
    )

    # 600 seconds -> 600_000 ms, passed as a bound string param.
    assert _set_config_params(db)["timeout_ms"] == "600000"


@pytest.mark.asyncio
async def test_reset_timeout_zero_disables_cap(monkeypatch):
    """A ``0`` setting disables the per-statement cap (Postgres = no timeout)."""
    monkeypatch.setattr(
        config_module.settings, "embedding_reset_statement_timeout_secs", 0
    )
    db = _FakeDB(deleted=3)

    await _reset_kb_embeddings_for_new_model(db, target_embedding_model="m")

    assert _set_config_params(db)["timeout_ms"] == "0"


@pytest.mark.asyncio
async def test_reset_proceeds_when_timeout_bump_fails(monkeypatch):
    """If raising the cap fails, the purge still runs (best-effort)."""
    monkeypatch.setattr(
        config_module.settings, "embedding_reset_statement_timeout_secs", 600
    )
    db = _FakeDB(deleted=5, fail_connection=True)

    deleted, _ = await _reset_kb_embeddings_for_new_model(
        db, target_embedding_model="m"
    )

    assert deleted == 5
    # No set_config recorded because connection() raised, but the delete ran.
    assert any("delete from embeddings" in c.lower() for c in db.calls)
