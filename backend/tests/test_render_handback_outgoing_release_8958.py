"""Work a stopping worker hands back never goes to a worker of its own release while that release rolls out (#8958).

Observed 5 Nov 2026 15:27-16:02Z: a high render handed back at a rollout while no worker of the incoming release ran
yet was claimed by another worker of the outgoing release, stopped by the same rolling update minutes later; it sat
at 23 % "Resuming after a deploy" through two rollouts. A hand-back with no incoming worker to name is now kept from
the outgoing release's workers (``not_release``) for a bounded time, any worker after, and the render's line says
the frames each relaunch kept and which relaunches were handed on before their drawing resumed.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import settings
from app.services import orchestration_engine as engine
from app.services import orchestration_reconciler as reconciler
from app.services.orchestration_adapters import video_studio as adapters
from app.services.video_studio import render_process


class _Db:
    async def flush(self):
        return None


async def _deferred(monkeypatch, **options) -> SimpleNamespace:
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        adapter="video_render",
        state="running",
        lease_owner="old-a",
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=3),
        attempt_count=1,
        max_attempts=3,
        deadline_at=None,
        payload={
            "render_id": "r1",
            engine.NOT_RELEASE_PAYLOAD_KEY: {"version": "stale", "until": "2000-01-01T00:00:00+00:00"},
        },
        run_at=datetime.now(UTC),
        adapter_state=None,
        last_error=None,
        updated_at=None,
    )

    async def locked_get(_db, _execution_id, **_kwargs):
        return execution

    async def head(_db, _adapter, **_kwargs):
        return datetime.now(UTC) - timedelta(seconds=1)

    monkeypatch.setattr(engine, "_locked_get", locked_get)
    monkeypatch.setattr(engine, "head_of_queue", head)
    await engine.defer(
        _Db(),
        execution_id=execution.id,
        worker_id="old-a",
        reason="handed back",
        delay_seconds=0.0,
        adapter_state="handed_over",
        place="head",
        **options,
    )
    return execution


async def test_defer_records_the_release_held_from_and_until_when(monkeypatch):
    before = datetime.now(UTC)
    execution = await _deferred(monkeypatch, not_release=("1.0.0-old", 300.0))
    held = execution.payload[engine.NOT_RELEASE_PAYLOAD_KEY]
    assert held["version"] == "1.0.0-old"
    until = datetime.fromisoformat(held["until"])
    assert before + timedelta(seconds=299) <= until <= datetime.now(UTC) + timedelta(seconds=301)
    # Due now at the head of its queue, its attempt unspent: only who may claim it changed.
    assert execution.state == "pending" and execution.attempt_count == 0


async def test_a_deferral_without_it_clears_an_older_release_hold(monkeypatch):
    execution = await _deferred(monkeypatch)
    assert engine.NOT_RELEASE_PAYLOAD_KEY not in execution.payload


def test_a_claim_filters_on_the_release_held_from():
    conditions = engine._claimable("video_render", "new-a", datetime.now(UTC))
    compiled = conditions[-1].compile(dialect=postgresql.dialect())
    bound = {str(value) for value in compiled.params.values()}
    # The last condition reads the release hold, against this worker's own version.
    assert engine.NOT_RELEASE_PAYLOAD_KEY in bound or any(engine.NOT_RELEASE_PAYLOAD_KEY in v for v in bound)
    assert str(settings.app_version or "") in bound
    # A claim clears it with the other holds.
    assert engine.NOT_RELEASE_PAYLOAD_KEY in engine._CLAIM_HOLDS


@pytest.fixture
def stopping_render(monkeypatch):
    world = SimpleNamespace(deferred=[], taker=None)
    monkeypatch.setattr(settings, "app_version", "1.0.0-old")

    async def incoming(_worker_id):
        return (world.taker, adapters.INCOMING_HOLD_SECONDS) if world.taker else None

    async def defer(_db, **kwargs):
        world.deferred.append(kwargs)

    async def wake():
        return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return False

        async def commit(self):
            return None

    monkeypatch.setattr(adapters, "incoming_hand", incoming)
    monkeypatch.setattr(engine, "defer", defer)
    monkeypatch.setattr(engine, "notify_orchestration_wake", wake)
    monkeypatch.setattr("app.core.database.async_session", _Session)
    return world


async def test_a_render_torn_down_with_no_incoming_worker_is_kept_from_its_release(stopping_render):
    await adapters._hand_back(uuid.uuid4(), "old-a")
    (call,) = stopping_render.deferred
    assert call["claim_for"] is None
    assert call["not_release"] == ("1.0.0-old", reconciler.HANDBACK_OUTGOING_HOLD_SECONDS)
    assert call["place"] == "head"


async def test_a_render_torn_down_with_an_incoming_worker_is_handed_to_it(stopping_render):
    stopping_render.taker = "new-a"
    await adapters._hand_back(uuid.uuid4(), "old-a")
    (call,) = stopping_render.deferred
    assert call["claim_for"] == ("new-a", adapters.INCOMING_HOLD_SECONDS)
    assert "not_release" not in call


def test_the_render_line_names_the_frames_each_relaunch_kept():
    relaunches = [
        {"attempt": 1, "attempts": 3, "progress": 23, "after": "after a deploy", "frames_kept": 2_016, "version": "b2"},
        {
            "attempt": 1,
            "attempts": 3,
            "progress": 23,
            "after": "after a deploy",
            "frames_kept": 2_016,
            "version": "b3",
            "frames_resumed_from": 2_016,
        },
    ]
    line = render_process.relaunch_line(relaunches)
    assert "2,016 frames kept on its worker" in line
    # The first was handed on again before its drawing resumed; the second resumed from its kept frames.
    first, second = line.split(": ", 1)[1].split("; ")
    assert first.endswith("handed on before its drawing resumed")
    assert second.endswith("resumed from frame 2,016.")


def test_the_newest_relaunch_still_starting_is_not_said_handed_on():
    line = render_process.relaunch_line([{"progress": 5, "frames_kept": 0}])
    assert "handed on" not in line and "kept" not in line
