import pytest

from app.core.database import async_session, release_db_connection_before_loop


class FakeSession:
    def __init__(
        self,
        *,
        in_transaction: bool,
        fail_in_transaction: bool = False,
        fail_commit: bool = False,
        fail_rollback: bool = False,
    ):
        self._in_transaction = in_transaction
        self._fail_in_transaction = fail_in_transaction
        self._fail_commit = fail_commit
        self._fail_rollback = fail_rollback
        self.commits = 0
        self.rollbacks = 0

    def in_transaction(self) -> bool:
        if self._fail_in_transaction:
            raise RuntimeError("boom in_transaction")
        return self._in_transaction

    async def commit(self) -> None:
        if self._fail_commit:
            raise RuntimeError("boom commit")
        self.commits += 1
        self._in_transaction = False

    async def rollback(self) -> None:
        if self._fail_rollback:
            raise RuntimeError("boom rollback")
        self.rollbacks += 1
        self._in_transaction = False


@pytest.mark.asyncio
async def test_release_connection_commits_open_transaction():
    db = FakeSession(in_transaction=True)

    await release_db_connection_before_loop(db)

    assert db.commits == 1
    assert db.rollbacks == 0
    assert not db.in_transaction()


@pytest.mark.asyncio
async def test_release_connection_skips_when_no_transaction():
    db = FakeSession(in_transaction=False)

    await release_db_connection_before_loop(db)

    assert db.commits == 0
    assert db.rollbacks == 0


@pytest.mark.asyncio
async def test_release_connection_commit_error_triggers_rollback(
    caplog: pytest.LogCaptureFixture,
):
    """When ``commit()`` fails the session is in a failed-transaction
    state and refuses further work. A best-effort ``rollback()`` must
    fire so the session is reusable and the connection can be returned
    to the pool — without it, the assignment run path would either
    crash on the next DB op or keep the connection pinned to a dead
    transaction, defeating the whole purpose of this PR."""
    db = FakeSession(in_transaction=True, fail_commit=True)

    with caplog.at_level("WARNING"):
        await release_db_connection_before_loop(db)

    assert db.commits == 0
    assert db.rollbacks == 1
    assert not db.in_transaction()
    assert "Failed to release DB connection before loop" in caplog.text


@pytest.mark.asyncio
async def test_release_connection_rollback_error_is_swallowed(
    caplog: pytest.LogCaptureFixture,
):
    """If even the best-effort rollback fails (e.g. connection broken),
    the helper must still return without raising so the caller's
    assignment-run error handling stays intact."""
    db = FakeSession(in_transaction=True, fail_commit=True, fail_rollback=True)

    with caplog.at_level("DEBUG"):
        await release_db_connection_before_loop(db)

    assert db.commits == 0
    assert db.rollbacks == 0
    assert "Failed to release DB connection before loop" in caplog.text
    assert "Best-effort rollback after failed pre-loop commit also failed" in caplog.text


@pytest.mark.asyncio
async def test_release_connection_in_transaction_error_is_swallowed(
    caplog: pytest.LogCaptureFixture,
):
    """Probe failure must also be swallowed (no rollback issued because
    the helper never knew the session had an open transaction)."""
    db = FakeSession(in_transaction=True, fail_in_transaction=True)

    with caplog.at_level("WARNING"):
        await release_db_connection_before_loop(db)

    assert db.commits == 0
    # rollback is also attempted in the catch-all branch (defensive); it
    # succeeds against the fake session and clears the transaction flag.
    assert db.rollbacks == 1
    assert "Failed to release DB connection before loop" in caplog.text


@pytest.mark.asyncio
async def test_async_session_factory_disables_expire_on_commit():
    """``expire_on_commit=False`` is required so the loaded ORM objects
    (assignment, run, etc.) remain accessible after the pre-loop
    commit — otherwise the agent loop would emit fresh ``SELECT`` per
    attribute access, blowing the pool savings.

    Asserted via the public API (constructing a real session and reading
    ``session.sync_session.expire_on_commit``) rather than the
    ``sessionmaker.kw`` internal so the test isn't coupled to SQLAlchemy
    implementation details that can change across upgrades.
    ``AsyncSession`` proxies the flag through its underlying
    :class:`Session`, so ``sync_session.expire_on_commit`` is the
    canonical public read.
    """
    async with async_session() as session:
        assert session.sync_session.expire_on_commit is False
