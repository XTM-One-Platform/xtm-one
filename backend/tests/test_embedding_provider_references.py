"""Chunks embedded by a deleted provider config keep no FK to it.

The ``ON DELETE SET NULL`` cascade on ``embeddings.embedding_provider_id`` ran
inside the provider deletion, under its lock on every provider config row. The
column carries no FK any more and the scheduler nulls the id of a deleted
config afterwards, in bounded batches. The database half (the deletion no
longer waits on a chunk row, the sweep skips held rows) is covered in
``test_llm_provider_deletion_postgres.py``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.exc import DBAPIError

from app.models.embedding import Embedding
from app.services.llm_provider_deletion import (
    _CLEAR_EMBEDDING_PROVIDER_BATCH,
    _DANGLING_EMBEDDING_PROVIDER_IDS,
    _LOCAL_STATEMENT_TIMEOUT,
    clear_deleted_embedding_provider_references,
)


def test_the_embedding_provider_reference_carries_no_foreign_key():
    column = Embedding.__table__.c.embedding_provider_id

    assert column.foreign_keys == set()
    assert column.nullable is True
    # The index stays: the sweep reads the distinct ids off it.
    assert any(
        [c.name for c in index.columns] == ["embedding_provider_id"]
        for index in Embedding.__table__.indexes
    )


class _Result:
    def __init__(self, *, ids=None, rowcount=0):
        self._ids = ids or []
        self.rowcount = rowcount

    def scalars(self):
        return iter(self._ids)


class _CanceledError(Exception):
    def __init__(self, sqlstate: str):
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _Session:
    """Answers the dangling-id query, then each batch with the next scripted count.

    A scripted count that is an exception is raised instead, as PostgreSQL
    cancelling the statement would.
    """

    def __init__(
        self,
        dangling: list[uuid.UUID],
        counts: dict[uuid.UUID, list],
        *,
        on_statement=None,
    ):
        self.dangling = dangling
        self.counts = {key: list(value) for key, value in counts.items()}
        self.batches: list[tuple[uuid.UUID, int]] = []
        self.timeouts: list[int] = []
        # (command, timeout in force when it started), in order.
        self.commands: list[tuple[str, int]] = []
        self.commits = 0
        self.rollbacks = 0
        self._timeout_pending = False
        self._on_statement = on_statement

    def _start(self, command: str) -> None:
        # Every command of the sweep, the commits included, runs under a
        # timeout set just before it.
        assert self._timeout_pending, command
        self._timeout_pending = False
        self.commands.append((command, self.timeouts[-1]))

    async def execute(self, statement, params=None):
        if statement is _LOCAL_STATEMENT_TIMEOUT:
            self.timeouts.append(int(params["timeout_ms"]))
            self._timeout_pending = True
            return _Result()
        if statement is _DANGLING_EMBEDDING_PROVIDER_IDS:
            self._start("dangling")
            if self._on_statement:
                self._on_statement()
            return _Result(ids=self.dangling)
        assert statement is _CLEAR_EMBEDDING_PROVIDER_BATCH
        self._start("batch")
        if self._on_statement:
            self._on_statement()
        self.batches.append((params["provider_id"], params["batch_size"]))
        outcome = self.counts[params["provider_id"]].pop(0)
        if isinstance(outcome, Exception):
            raise DBAPIError("UPDATE embeddings", {}, outcome)
        return _Result(rowcount=outcome)

    async def commit(self):
        self._start("commit")
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.mark.asyncio
async def test_the_sweep_batches_each_deleted_config_until_nothing_is_left():
    first, second = uuid.uuid4(), uuid.uuid4()
    db = _Session([first, second], {first: [3, 3, 1, 0], second: [0]})

    cleared = await clear_deleted_embedding_provider_references(db, batch_size=3)

    assert cleared == 7
    assert db.batches == [(first, 3)] * 4 + [(second, 3)]
    # Every batch is its own transaction, so no row lock outlives its batch.
    assert db.commits == 1 + len(db.batches)
    # Each statement is bounded by what is left of the budget, never more.
    assert db.timeouts and all(0 < ms <= 15_000 for ms in db.timeouts)


@pytest.mark.asyncio
async def test_a_commit_runs_under_what_the_statement_before_it_left(monkeypatch):
    """PostgreSQL times the COMMIT against the setting in force when it starts:
    a statement that used most of the budget must not hand its commit the
    timeout computed before it ran."""
    from app.services import llm_provider_deletion

    now = [1000.0]
    monkeypatch.setattr(
        llm_provider_deletion, "time", type("_Clock", (), {"monotonic": staticmethod(lambda: now[0])})
    )

    def _slow_statement():
        now[0] += 6.0

    provider_id = uuid.uuid4()
    db = _Session([provider_id], {provider_id: [5, 0]}, on_statement=_slow_statement)

    await clear_deleted_embedding_provider_references(db, batch_size=5, time_budget=15.0)

    assert db.commands == [
        ("dangling", 15_000),
        ("commit", 9_000),
        ("batch", 9_000),
        ("commit", 3_000),
        ("batch", 3_000),
        # The budget is spent: the batch's row locks are still released at
        # once, under the 1 ms floor rather than a fresh budget.
        ("commit", 1),
    ]


def test_each_batch_checks_again_that_no_config_carries_the_id():
    sql = " ".join(str(_CLEAR_EMBEDDING_PROVIDER_BATCH).split())
    assert "NOT EXISTS ( SELECT 1 FROM llm_provider_configs c WHERE c.id = :provider_id )" in sql


@pytest.mark.asyncio
async def test_the_sweep_stops_at_its_time_budget():
    provider_id = uuid.uuid4()
    db = _Session([provider_id], {provider_id: [1000]})

    cleared = await clear_deleted_embedding_provider_references(db, time_budget=0)

    assert cleared == 0
    assert db.batches == []


@pytest.mark.asyncio
async def test_a_batch_cancelled_by_the_budget_ends_the_sweep_and_keeps_what_was_cleared():
    first, second = uuid.uuid4(), uuid.uuid4()
    db = _Session([first, second], {first: [5, _CanceledError("57014")], second: [9]})

    cleared = await clear_deleted_embedding_provider_references(db, batch_size=5)

    assert cleared == 5
    assert db.batches == [(first, 5), (first, 5)]
    assert db.rollbacks == 1


@pytest.mark.asyncio
async def test_any_other_database_error_still_propagates():
    provider_id = uuid.uuid4()
    db = _Session([provider_id], {provider_id: [_CanceledError("40P01")]})

    with pytest.raises(DBAPIError):
        await clear_deleted_embedding_provider_references(db)
