"""Tests for ``resume_assignment_run`` orchestration wiring.

A HITL-paused run resumes by re-arming its live (AWAIT) orchestration
execution via a ``resume`` outbox event. When no live execution exists — a
legacy run created before the engine landed, or one already cleaned up — the
resume must instead schedule a *fresh* execution (keyed on the run's dedup key
so repeated respond calls stay idempotent). A regression that drops that
fallback silently commits the human response but never actually resumes the
run.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.services import access_requests
from app.services import assignment_engine as ae
from app.services import orchestration_engine as oe


class _FakeDB:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def _patch(monkeypatch, *, existing_execution):
    """Stub the engine calls ``resume_assignment_run`` reaches for."""
    events: list[dict] = []
    created: list[dict] = []
    woke: list[bool] = []

    async def _get_by_dedup(db, dedup_key):
        return existing_execution

    async def _emit(db, *, execution_id, event_type, dedup_key, payload=None):
        events.append(
            {
                "execution_id": execution_id,
                "event_type": event_type,
                "dedup_key": dedup_key,
                "payload": payload,
            }
        )
        return uuid.uuid4()

    async def _create(db, *, adapter, payload, dedup_key=None, **kwargs):
        created.append({"adapter": adapter, "payload": payload, "dedup_key": dedup_key})
        return SimpleNamespace(id=uuid.uuid4())

    async def _wake():
        woke.append(True)

    async def _close(db, run_id, **kwargs):
        # The open action requests on the run close with the answer.
        return 0

    monkeypatch.setattr(access_requests, "close_run_action_requests", _close)
    monkeypatch.setattr(oe, "get_execution_by_dedup_key", _get_by_dedup)
    monkeypatch.setattr(oe, "emit_outbox_event", _emit)
    monkeypatch.setattr(ae, "create_execution", _create)
    monkeypatch.setattr(ae, "notify_orchestration_wake", _wake)
    return events, created, woke


@pytest.mark.asyncio
async def test_resume_running_execution_emits_resume_event_only(monkeypatch):
    run = SimpleNamespace(id=uuid.uuid4())
    paused_at = datetime.now(UTC)
    execution = SimpleNamespace(id=uuid.uuid4(), state="running", updated_at=paused_at)
    events, created, woke = _patch(monkeypatch, existing_execution=execution)
    db = _FakeDB()

    await ae.resume_assignment_run(db, run)

    # Live paused execution → re-arm via a resume event, do NOT create a duplicate.
    assert len(events) == 1
    assert events[0]["event_type"] == "resume"
    assert events[0]["execution_id"] == execution.id
    # The dedup_key is keyed on the pause instant (execution.updated_at), so
    # repeated respond clicks within the same pause collapse onto one event
    # while a later HITL round (fresh updated_at) gets a distinct key.
    assert events[0]["dedup_key"] == (
        f"resume:{run.id}:{int(paused_at.timestamp() * 1000)}"
    )
    assert created == []
    assert db.commits == 1
    assert woke == [True]


@pytest.mark.asyncio
async def test_resume_without_execution_creates_fallback_with_dedup_key(monkeypatch):
    run = SimpleNamespace(id=uuid.uuid4())
    events, created, woke = _patch(monkeypatch, existing_execution=None)
    db = _FakeDB()

    await ae.resume_assignment_run(db, run)

    # No live execution (legacy run) → schedule a fresh, idempotent execution.
    assert events == []
    assert len(created) == 1
    assert created[0]["adapter"] == "assignment"
    assert created[0]["payload"] == {"run_id": str(run.id)}
    assert created[0]["dedup_key"] == f"assignment_run:{run.id}"
    assert db.commits == 1
    assert woke == [True]


@pytest.mark.asyncio
async def test_resume_non_running_execution_falls_back_to_create(monkeypatch):
    run = SimpleNamespace(id=uuid.uuid4())
    execution = SimpleNamespace(id=uuid.uuid4(), state="completed")
    events, created, woke = _patch(monkeypatch, existing_execution=execution)
    db = _FakeDB()

    await ae.resume_assignment_run(db, run)

    # A non-``running`` existing row is not a live pause → take the create
    # fallback (idempotent on the dedup key) rather than emitting a resume.
    assert events == []
    assert len(created) == 1
    assert created[0]["dedup_key"] == f"assignment_run:{run.id}"
    assert db.commits == 1
    assert woke == [True]
