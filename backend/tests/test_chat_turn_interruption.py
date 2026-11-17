"""A conversation is never left silent.

A turn killed by a restart, a deploy or a crash used to end with no word: the
relay closed it on every viewer with an ``error`` the web client never showed,
and after a reload nothing remembered that a turn had died. A turn that takes
the lock now records itself until it reaches its own end; one that does not is
served as the conversation's ``interrupted_turn``, with the tool work its
checkpoint keeps. Work handed to the background (a Video Studio job) is served
as what the conversation waits on, and a wake a stopped worker took along is
recovered by a sweep.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services import chat_turn_guard as guard
from app.services import chat_turn_stream

CONV = "5d0c2f43-6a3e-4f43-9a10-7a1f3f6f2b11"


class _FakeRedis:
    """The commands the turn record uses, plus the guard's release script."""

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        self.fail_open_writes = False

    async def set(self, key, value, nx=False, ex=None):
        if self.fail_open_writes and key.startswith("chat:turn:open:"):
            raise ConnectionError("redis down")
        if nx and key in self.kv:
            return None
        self.kv[key] = value
        if ex is not None:
            self.ttl[key] = ex
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def delete(self, *keys):
        removed = 0
        for key in keys:
            self.ttl.pop(key, None)
            removed += self.kv.pop(key, None) is not None
        return removed

    async def exists(self, key):
        return 1 if key in self.kv else 0

    async def llen(self, _key):
        return 0

    async def eval(self, script, numkeys, *keys_and_args):
        key = keys_and_args[0]
        token = keys_and_args[numkeys]
        assert script == guard._RELEASE_SCRIPT
        if self.kv.get(key) == token:
            self.kv.pop(key, None)
            return 1
        return 0


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()

    async def _redis():
        return r

    monkeypatch.setattr(guard, "_redis", _redis)
    return r


def _record(r: _FakeRedis) -> dict | None:
    raw = r.kv.get(guard.open_turn_key(CONV))
    return json.loads(raw) if raw else None


# ── The guard's record ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_turn_that_ends_leaves_nothing_behind(fake_redis):
    token = await guard.acquire_turn(CONV)

    assert _record(fake_redis)["token"] == token
    assert fake_redis.ttl[guard.open_turn_key(CONV)] == guard.OPEN_TURN_TTL_SECONDS
    assert await guard.interrupted_turn(CONV) is None  # in flight

    assert await guard.release_turn(CONV, token) is True
    assert _record(fake_redis) is None
    assert await guard.interrupted_turn(CONV) is None


@pytest.mark.asyncio
async def test_a_turn_closed_as_interrupted_is_remembered_after_its_release(fake_redis):
    token = await guard.acquire_turn(CONV)
    before = time.time()

    await guard.mark_turn_interrupted(CONV, token)
    await guard.release_turn(CONV, token)

    turn = await guard.interrupted_turn(CONV)
    assert turn is not None
    assert turn.interrupted_at is not None
    assert turn.interrupted_at.timestamp() >= before
    assert turn.started_at is not None and turn.started_at <= turn.interrupted_at


@pytest.mark.asyncio
async def test_a_turn_whose_process_died_is_an_interrupted_turn_once_its_lock_lapses(fake_redis):
    await guard.acquire_turn(CONV)
    # The process is gone: nothing released anything, the marker's TTL lapses.
    del fake_redis.kv[guard.processing_key(CONV)]

    turn = await guard.interrupted_turn(CONV)
    assert turn is not None
    assert turn.interrupted_at is None


@pytest.mark.asyncio
async def test_the_next_turn_replaces_the_interrupted_one(fake_redis):
    first = await guard.acquire_turn(CONV)
    await guard.mark_turn_interrupted(CONV, first)
    await guard.release_turn(CONV, first)
    assert await guard.interrupted_turn(CONV) is not None

    second = await guard.acquire_turn(CONV)
    assert await guard.interrupted_turn(CONV) is None
    assert "interrupted_at" not in _record(fake_redis)

    await guard.release_turn(CONV, second)
    assert await guard.interrupted_turn(CONV) is None


@pytest.mark.asyncio
async def test_another_turns_mark_or_release_never_touches_the_record(fake_redis):
    token = await guard.acquire_turn(CONV)

    await guard.mark_turn_interrupted(CONV, "someone-else")
    await guard.release_turn(CONV, "someone-else")

    record = _record(fake_redis)
    assert record["token"] == token
    assert "interrupted_at" not in record


@pytest.mark.asyncio
async def test_a_channel_turn_is_not_recorded(fake_redis, monkeypatch):
    async def _acquire_channel(script, numkeys, *keys_and_args):
        fake_redis.kv[keys_and_args[0]] = keys_and_args[numkeys]
        return "OK"

    monkeypatch.setattr(fake_redis, "eval", _acquire_channel)

    token = await guard.acquire_turn(CONV, channel="slack")

    assert token and token.startswith(guard.CHANNEL_TURN_MARKER_PREFIX)
    assert _record(fake_redis) is None


@pytest.mark.asyncio
async def test_a_record_that_cannot_be_written_never_fails_the_turn(fake_redis):
    fake_redis.fail_open_writes = True

    token = await guard.acquire_turn(CONV)

    assert token is not None
    assert await guard.release_turn(CONV, token) is True


@pytest.mark.asyncio
async def test_dismissing_forgets_an_interrupted_turn_only(fake_redis):
    assert await guard.dismiss_interrupted_turn(CONV) is False

    token = await guard.acquire_turn(CONV)
    assert await guard.dismiss_interrupted_turn(CONV) is False  # in flight
    assert _record(fake_redis) is not None

    await guard.mark_turn_interrupted(CONV, token)
    await guard.release_turn(CONV, token)
    assert await guard.dismiss_interrupted_turn(CONV) is True
    assert await guard.interrupted_turn(CONV) is None


@pytest.mark.asyncio
async def test_a_malformed_record_is_no_interrupted_turn(fake_redis):
    fake_redis.kv[guard.open_turn_key(CONV)] = "not json"
    assert await guard.interrupted_turn(CONV) is None

    fake_redis.kv[guard.open_turn_key(CONV)] = json.dumps({"started_at": 1})
    assert await guard.interrupted_turn(CONV) is None


# ── The publisher marks what it closes as interrupted ────────────────────


class _Pipeline:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None

    async def execute(self):
        return []


class _StreamRedis:
    def pipeline(self, transaction=True):
        return _Pipeline()


@pytest.fixture
def marks(monkeypatch):
    seen: list[tuple[str, str]] = []

    async def _mark(conversation_id, token):
        seen.append((conversation_id, token))

    async def _redis():
        return _StreamRedis()

    monkeypatch.setattr(chat_turn_stream, "mark_turn_interrupted", _mark)
    monkeypatch.setattr(chat_turn_stream, "_redis", _redis)
    monkeypatch.setattr(chat_turn_stream, "_down_until", 0.0)
    return seen


@pytest.mark.asyncio
async def test_a_turn_closed_with_the_interrupted_terminal_is_marked(marks):
    publisher = chat_turn_stream.TurnPublisher(CONV, "tok-1")
    publisher.start()

    await publisher.aclose(terminal=chat_turn_stream.interrupted_frame())

    assert marks == [(CONV, "tok-1")]


@pytest.mark.asyncio
async def test_a_turn_that_reached_its_own_end_is_not_marked(marks):
    publisher = chat_turn_stream.TurnPublisher(CONV, "tok-2")
    publisher.start()
    publisher.offer({"type": "done", "content": "Here it is.", "message_id": "m-1"})

    await publisher.aclose(terminal=chat_turn_stream.interrupted_frame())

    assert marks == []


# ── What the checkpoint keeps, counted ───────────────────────────────────


class _Row:
    def __init__(self, row):
        self._row = row

    def one_or_none(self):
        return self._row


class _CheckpointDB:
    def __init__(self, row=None, fail=False):
        self._row = row
        self._fail = fail

    async def execute(self, _statement):
        if self._fail:
            raise RuntimeError("database down")
        return _Row(self._row)


def _round(call_ids: list[str], *, answered: bool = True) -> list[dict]:
    entries = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": cid, "name": "render_video", "arguments": {}} for cid in call_ids],
        }
    ]
    if answered:
        entries += [{"role": "tool", "content": "ok", "tool_call_id": cid} for cid in call_ids]
    return entries


@pytest.mark.asyncio
async def test_the_completed_tool_calls_a_checkpoint_keeps_are_counted():
    from app.services.turn_checkpoint import kept_tool_calls

    entries = [*_round(["a", "b"]), *_round(["c"]), *_round(["d"], answered=False)]
    db = _CheckpointDB((entries, datetime.now(UTC)))

    assert await kept_tool_calls(db, uuid.UUID(CONV)) == 3


@pytest.mark.asyncio
async def test_no_checkpoint_an_aged_one_or_a_failed_read_keeps_nothing():
    from app.services.turn_checkpoint import RESUME_MAX_AGE, kept_tool_calls

    conv = uuid.UUID(CONV)
    aged = datetime.now(UTC) - RESUME_MAX_AGE - timedelta(minutes=1)

    assert await kept_tool_calls(_CheckpointDB(None), conv) == 0
    assert await kept_tool_calls(_CheckpointDB((_round(["a"]), aged)), conv) == 0
    assert await kept_tool_calls(_CheckpointDB(fail=True), conv) == 0


# ── The work a conversation waits on ─────────────────────────────────────


def _job(status: str, *, progress: int = 0, stage: str | None = None, progress_at=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        progress=progress,
        stage=stage,
        progress_at=progress_at,
        provider_state=None,
        provider_eta_at=None,
        error=None,
        params={},
        created_at=datetime.now(UTC),
    )


@pytest.mark.asyncio
async def test_the_video_jobs_owed_to_a_conversation_are_what_it_waits_on(monkeypatch):
    from app.models.video_studio import JOB_PENDING, JOB_RUNNING
    from app.services import conversation_awaited_work as awaited
    from app.services.video_studio import jobs

    render = _job(JOB_RUNNING, progress=42, stage="Rendering frames", progress_at=datetime.now(UTC))
    clip = _job(JOB_PENDING)

    async def _pending(conversation_id):
        assert conversation_id == uuid.UUID(CONV)
        return [("render", render, "Brand film"), ("clip", clip, None)]

    monkeypatch.setattr(jobs, "owed_jobs_pending", _pending)

    items = [item.as_dict() for item in await awaited.awaited_work(uuid.UUID(CONV))]

    assert items == [
        {
            "kind": "video_render",
            "id": str(render.id),
            "name": "Brand film",
            "status": "running",
            "percent": 42,
            "stage": "Rendering frames",
        },
        {
            "kind": "video_clip",
            "id": str(clip.id),
            "name": None,
            "status": "queued",
            "percent": 0,
            "stage": "Queued",
        },
    ]


# ── A wake a stopped worker took along ───────────────────────────────────


@pytest.fixture
def sweep(monkeypatch):
    from app.services.video_studio import jobs, wake

    owed = {uuid.uuid4(): uuid.uuid4() for _ in range(3)}
    busy, broken = list(owed)[1], list(owed)[2]
    told: list[tuple[uuid.UUID, str]] = []
    asked: dict = {}

    async def _owed(*, ended_before, ended_after, limit):
        asked.update(ended_before=ended_before, ended_after=ended_after, limit=limit)
        return list(owed.items())

    async def _in_flight(conversation_id):
        return conversation_id == busy

    async def _claim(conversation_id):
        if conversation_id == broken:
            raise RuntimeError("database down")
        return {"render": [("r-1", datetime.now(UTC))]}

    async def _tell(conversation_id, user_id, claimed):
        told.append((conversation_id, user_id))

    monkeypatch.setattr(jobs, "conversations_owed_ended_jobs", _owed)
    monkeypatch.setattr(jobs, "owed_jobs_in_flight", _in_flight)
    monkeypatch.setattr(jobs, "claim_owed_jobs", _claim)
    monkeypatch.setattr(wake, "_tell", _tell)
    return SimpleNamespace(owed=owed, told=told, asked=asked, wake=wake)


@pytest.mark.asyncio
async def test_the_sweep_resumes_a_conversation_a_lost_wake_left_owed(sweep):
    now = datetime(2026, 10, 19, 22, 0, tzinfo=UTC)

    resumed = await sweep.wake.sweep_lost_wakes(now)
    await asyncio.gather(*list(sweep.wake._WAKES))

    first = next(iter(sweep.owed))
    assert resumed == 1
    assert sweep.told == [(first, str(sweep.owed[first]))]
    assert sweep.asked["ended_before"] == now - timedelta(seconds=sweep.wake.SWEEP_AFTER_SECONDS)
    assert sweep.asked["ended_after"] == now - sweep.wake.SWEEP_WINDOW
    assert sweep.wake.SWEEP_AFTER_SECONDS > sweep.wake.WAKE_GRACE_SECONDS


@pytest.mark.asyncio
async def test_the_worker_sweeps_at_most_once_per_interval(monkeypatch):
    from app.services.orchestration_adapters.video_studio import VideoStudioWakeSweepAdapter
    from app.services.video_studio import wake

    calls = []

    async def _sweep(now=None):
        calls.append(now)
        return 0

    monkeypatch.setattr(wake, "sweep_lost_wakes", _sweep)
    adapter = VideoStudioWakeSweepAdapter()

    await adapter.periodic(SimpleNamespace())
    await adapter._sweep_task
    await adapter.periodic(SimpleNamespace())

    assert len(calls) == 1


# ── The routes ───────────────────────────────────────────────────────────


class _Result:
    def __init__(self, scalar=None):
        self._scalar = scalar

    def scalar_one_or_none(self):
        return self._scalar


class _FakeDB:
    def __init__(self, results):
        self._queue = list(results)

    async def execute(self, _statement, *_args, **_kwargs):
        return self._queue.pop(0)


@pytest.mark.asyncio
async def test_only_its_owner_dismisses_an_interrupted_turn(monkeypatch):
    from app.api.v1 import chat as chat_api

    dismissed = []

    async def _dismiss(conversation_id):
        dismissed.append(conversation_id)
        return True

    monkeypatch.setattr(guard, "dismiss_interrupted_turn", _dismiss)
    user = SimpleNamespace(id=uuid.uuid4())

    with pytest.raises(HTTPException) as exc:
        await chat_api.dismiss_interrupted_turn(conversation_id=CONV, user=user, db=_FakeDB([_Result(None)]))
    assert exc.value.status_code == 404
    assert dismissed == []

    await chat_api.dismiss_interrupted_turn(conversation_id=CONV, user=user, db=_FakeDB([_Result(uuid.UUID(CONV))]))
    assert dismissed == [CONV]


@pytest.mark.asyncio
async def test_a_reader_who_is_not_the_owner_sees_no_awaited_work(monkeypatch):
    from app.api.v1 import chat as chat_api
    from app.services import conversation_awaited_work as awaited

    conv = SimpleNamespace(id=uuid.UUID(CONV))
    item = awaited.AwaitedWork(
        kind="video_render", id="r-1", name="Brand film", status="running", percent=10, stage=None
    )

    async def _awaited(conversation_id):
        return [item]

    monkeypatch.setattr(awaited, "awaited_work", _awaited)

    async def _as(access):
        async def _resolve(conversation_id, user, db):
            return conv, access

        monkeypatch.setattr(chat_api, "resolve_conversation_for_read", _resolve)
        return await chat_api.get_awaited_work(conversation_id=CONV, user=SimpleNamespace(), db=None)

    assert (await _as(chat_api.CONVERSATION_ACCESS_READ)).items == []
    owner_view = await _as(chat_api.CONVERSATION_ACCESS_OWNER)
    assert [i.model_dump() for i in owner_view.items] == [item.as_dict()]
