# SPDX-License-Identifier: Apache-2.0
"""Each A2A delegation runs in a database session of its own (#596).

The calls of one model round run together and used to share the run's
session: one database error that aborted its transaction failed every later
send, collect and delegation record of the run. Pinned here: the delegation
gets a fresh session, never the run's; it is committed at the end; two
parallel delegations get two sessions; a failed commit still returns the
partner's outcome and rolls that session back alone.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import integration_tools


class _Session:
    def __init__(self, registry: list[_Session], *, commit_fails: bool = False) -> None:
        self.committed = False
        self.rolled_back = False
        self.closed = False
        self._commit_fails = commit_fails
        registry.append(self)

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        self.closed = True
        return False

    async def commit(self) -> None:
        if self._commit_fails:
            raise RuntimeError("current transaction is aborted")
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def _factory(monkeypatch: pytest.MonkeyPatch, *, commit_fails: bool = False) -> list[_Session]:
    from app.core import database

    sessions: list[_Session] = []
    monkeypatch.setattr(database, "async_session", lambda: _Session(sessions, commit_fails=commit_fails))
    return sessions


@pytest.mark.asyncio
async def test_the_delegation_gets_a_session_of_its_own_committed_at_its_end(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = _factory(monkeypatch)
    seen: dict[str, Any] = {}
    integration = SimpleNamespace(id="i-1")
    target = SimpleNamespace(id="t-1")

    async def _delegate(db: Any, integ: Any, row: Any, **kwargs: Any) -> str:
        seen.update(db=db, integration=integ, target=row, kwargs=kwargs)
        return "outcome"

    result = await integration_tools._delegate_in_own_session(
        _delegate, integration, target, message="hello", calling=None
    )

    assert result == "outcome"
    assert len(sessions) == 1
    assert seen["db"] is sessions[0]
    assert seen["integration"] is integration and seen["target"] is target
    assert seen["kwargs"] == {"message": "hello", "calling": None}
    assert sessions[0].committed and sessions[0].closed


@pytest.mark.asyncio
async def test_parallel_delegations_of_one_round_never_share_a_session(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = _factory(monkeypatch)
    used: list[Any] = []

    async def _delegate(db: Any, _integ: Any, _row: Any, **_kwargs: Any) -> int:
        used.append(db)
        await asyncio.sleep(0)
        return id(db)

    first, second = await asyncio.gather(
        integration_tools._delegate_in_own_session(_delegate, SimpleNamespace(id="i"), None, message="a"),
        integration_tools._delegate_in_own_session(_delegate, SimpleNamespace(id="i"), None, message="b"),
    )

    assert len(sessions) == 2
    assert used[0] is not used[1]
    assert first != second


@pytest.mark.asyncio
async def test_a_failed_commit_keeps_the_outcome_and_rolls_back_that_session_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _factory(monkeypatch, commit_fails=True)

    async def _delegate(_db: Any, _integ: Any, _row: Any, **_kwargs: Any) -> str:
        return "partner answered"

    result = await integration_tools._delegate_in_own_session(_delegate, SimpleNamespace(id="i"), None, message="x")

    assert result == "partner answered"
    assert sessions[0].rolled_back and not sessions[0].committed


def test_the_tool_handler_delegates_through_its_own_session() -> None:
    src = inspect.getsource(integration_tools._build_a2a_delegation_tool)
    assert "_delegate_in_own_session(" in src
    assert "await delegate(\n" not in src
