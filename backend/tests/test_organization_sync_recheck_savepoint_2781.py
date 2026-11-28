"""#2781: the held-signal recheck after an organization sync never commits inside its savepoint.

Pure checks with the gate store's reads stubbed: no database, no network. The
fake session records each commit with the savepoint depth it happened at.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_scope_gate as gate
from app.services import ctem_scope_gate_store as store
from app.services import organization_context_sync as osync


class _Nested:
    def __init__(self, db: _Db) -> None:
        self.db = db

    async def __aenter__(self) -> None:
        self.db.depth += 1

    async def __aexit__(self, *_exc: Any) -> bool:
        self.db.depth -= 1
        return False


class _Db:
    """A session double: commits are recorded with the savepoint depth they ran at."""

    def __init__(self) -> None:
        self.depth = 0
        self.commits: list[int] = []
        self.info: dict[str, Any] = {}

    def begin_nested(self) -> _Nested:
        return _Nested(self)

    def in_nested_transaction(self) -> bool:
        return self.depth > 0

    def in_transaction(self) -> bool:
        return True

    async def commit(self) -> None:
        self.commits.append(self.depth)

    async def flush(self) -> None:
        return None


def _row() -> Any:
    return SimpleNamespace(id=uuid.uuid4(), organization_id=uuid.uuid4(), state="watchlist")


def _stub_reads(monkeypatch: pytest.MonkeyPatch, releases: list[bool]) -> None:
    """The OpenCTI read commits when told to release, as release_db_connection_before_loop does."""

    async def with_remote_software(db: Any, *_args: Any, release: bool = False, **_kw: Any) -> Any:
        releases.append(release)
        if release:
            await db.commit()
        return None

    async def loaded(*_args: Any, **_kw: Any) -> Any:
        return SimpleNamespace()

    monkeypatch.setattr(store, "facts_of", lambda _row: gate.SignalFacts.__new__(gate.SignalFacts))
    monkeypatch.setattr(store, "_with_remote_software", with_remote_software)
    monkeypatch.setattr(store, "_profile", loaded)
    monkeypatch.setattr(store, "_primary_assets", loaded)


@pytest.mark.asyncio
async def test_prepare_cve_reads_inside_a_savepoint_never_commits(monkeypatch: pytest.MonkeyPatch) -> None:
    releases: list[bool] = []
    _stub_reads(monkeypatch, releases)
    db = _Db()
    async with db.begin_nested():
        await store.prepare_cve_reads(db, [_row()])  # type: ignore[arg-type]  # a session double
    assert releases == [False] and db.commits == []


@pytest.mark.asyncio
async def test_prepare_cve_reads_outside_a_savepoint_still_releases(monkeypatch: pytest.MonkeyPatch) -> None:
    """#2518 review: with no savepoint open, the connection is still let go during the OpenCTI I/O."""
    releases: list[bool] = []
    _stub_reads(monkeypatch, releases)
    db = _Db()
    await store.prepare_cve_reads(db, [_row()])  # type: ignore[arg-type]
    assert releases == [True] and db.commits == [0]


@pytest.mark.asyncio
async def test_the_recheck_after_a_sync_reads_before_its_savepoint_and_never_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import organizations

    releases: list[bool] = []
    _stub_reads(monkeypatch, releases)
    rows = [_row()]
    held_at_depth: list[int] = []
    decided_with: list[bool] = []

    async def held(db: Any, **_kw: Any) -> list[Any]:
        held_at_depth.append(db.depth)
        return rows

    async def decide_again(db: Any, picked: list[Any], *, now: Any = None, prefetched: bool = False) -> dict[str, int]:
        assert db.depth == 1 and picked == rows
        decided_with.append(prefetched)
        if not prefetched:
            await store.prepare_cve_reads(db, picked)
        return {"watchlist": len(picked)}

    async def get_primary(_db: Any) -> Any:
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(store, "_held_for_recheck", held)
    monkeypatch.setattr(store, "_decide_again", decide_again)
    monkeypatch.setattr(organizations, "get_primary", get_primary)
    db = _Db()
    counts = await osync._recheck_held_signals(db)  # type: ignore[arg-type]
    assert counts == {"watchlist": 1} and "error" not in counts
    assert db.commits == []
    # The prefetch selects the batch in its own savepoint, then reads with no release; the decision is prefetched.
    assert held_at_depth == [1, 1] and releases == [False] and decided_with == [True]


def test_a_failed_recheck_is_a_warning_on_the_run() -> None:
    assert osync.recheck_warning({"watchlist": 3}) is None
    assert osync.recheck_warning({}) is None
    warning = osync.recheck_warning({"error": "Can't operate on closed transaction inside context manager."})
    assert warning == "Held signals not decided again: Can't operate on closed transaction inside context manager."
