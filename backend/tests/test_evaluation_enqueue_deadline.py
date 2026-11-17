"""Tests for ``enqueue_evaluation`` deriving the execution deadline.

The evaluation execution must carry a ``deadline_at`` derived from the
evaluation's own ``timeout_seconds`` (the whole-job budget) plus a grace
margin. Without it the reconciler falls back to the generic
``worker_job_timeout`` (10 min) and prematurely dead-letters long evaluations.
"""

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core import database as core_db
from app.core.config import settings
from app.services import evaluation_engine as ee
from app.services import orchestration_engine as oe


def _patch(monkeypatch, *, evaluation):
    created: list[dict] = []
    woke: list[bool] = []

    @asynccontextmanager
    async def _fake_session():
        async def _get(_model, _pk):
            return evaluation

        async def _commit():
            return None

        yield SimpleNamespace(get=_get, commit=_commit)

    async def _create(session, *, adapter, payload, deadline_at=None, **kwargs):
        created.append(
            {"adapter": adapter, "payload": payload, "deadline_at": deadline_at}
        )
        return SimpleNamespace(id=uuid.uuid4())

    async def _wake():
        woke.append(True)

    monkeypatch.setattr(core_db, "async_session", _fake_session)
    monkeypatch.setattr(oe, "create_execution", _create)
    monkeypatch.setattr(oe, "notify_orchestration_wake", _wake)
    return created, woke


@pytest.mark.asyncio
async def test_enqueue_evaluation_sets_deadline_from_timeout(monkeypatch):
    evaluation = SimpleNamespace(timeout_seconds=7200)
    created, woke = _patch(monkeypatch, evaluation=evaluation)

    before = datetime.now(UTC)
    await ee.enqueue_evaluation(str(uuid.uuid4()), "manual")

    assert len(created) == 1
    deadline = created[0]["deadline_at"]
    assert deadline is not None
    # deadline ≈ now + timeout_seconds + grace; assert it lands in the
    # expected window rather than pinning an exact instant.
    expected = 7200 + settings.orchestration_grace_seconds
    delta = (deadline - before).total_seconds()
    assert expected - 5 <= delta <= expected + 30
    assert woke == [True]


@pytest.mark.asyncio
async def test_enqueue_evaluation_missing_evaluation_falls_back_to_grace_deadline(
    monkeypatch,
):
    created, woke = _patch(monkeypatch, evaluation=None)

    before = datetime.now(UTC)
    await ee.enqueue_evaluation(str(uuid.uuid4()), "manual")

    # No evaluation row -> fail-closed fallback of now + grace: a bare
    # ``None`` would let ``sweep_deadlines`` never time out this execution, a
    # worse failure than timing out too eagerly. Must not raise.
    assert len(created) == 1
    deadline = created[0]["deadline_at"]
    assert deadline is not None
    delta = (deadline - before).total_seconds()
    assert 0 <= delta <= settings.orchestration_grace_seconds + 5
    assert woke == [True]
