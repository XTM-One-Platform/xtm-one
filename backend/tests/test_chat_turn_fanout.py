"""Live fan-out and resume of an in-flight chat turn.

An in-flight turn used to be visible only to the socket that started it:
a page reloaded mid-turn re-attached to a bare "Thinking..." and polled for
the answer, a second device or an impersonating admin saw neither the user
message nor the working state, and turns without a socket (REST, parent
wake) never showed anything at all.

Pins, on ``services/chat_turn_stream.py``, the real ``run_chat_connection``
and the web chat routes with fakes (same approach as ``test_chat_web_steering.py``):

* the publisher journals a turn the way the client holds it — status frames
  in order (chatty ones excluded, heavy tool output stripped), the reasoning
  and the answer of the CURRENT segment as strings, the resets the client's
  own handlers apply (a per-iteration ``thinking`` / ``stream_retract``
  clears the answer, ``transfer_start`` clears the reasoning, an
  intermediate ``done`` clears both) — and publishes live batches stamped
  ``origin`` / ``turn`` / ``seq``, coalescing adjacent text chunks;
* the terminal frame is published before the journal is dropped, and a
  dead Redis never raises into the turn;
* a relay attaching mid-turn is RESUMED through the client's own frame
  vocabulary (``thinking`` flagged ``resumed`` with ``elapsed_s``, the
  steps, one ``thinking_text``, one ``stream``), then follows the live
  frames — never its own, never a frame the snapshot already covered, never
  the turn it owns itself;
* one Redis subscription per process serves every socket of a conversation;
* a send publishes the user message, the turn frames and the steering
  messages; another tab's events stream receives them live and one attaching
  mid-turn is resumed; ``done`` is published before the marker is released;
  a non-owner can neither send, follow the turn nor request a cross-process
  Stop.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import chat as chat_module
from app.api.v1 import chat_web_stream
from app.core import config, rate_limit
from app.services import chat_turn_guard, chat_turn_stream
from app.services.chat_turn_stream import (
    AGENTIC_ACTION_FRAME,
    AGENTIC_ACTION_STATUS,
    RESYNC,
    TURN_INTERRUPTED_MESSAGE,
    TurnPublisher,
    TurnSnapshot,
    coalesce_frames,
    interrupted_frame,
    is_terminal_frame,
    journal_keys,
    read_snapshot,
    relay_turn_frames,
    resume_frames,
    slim_frame,
    turn_channel,
)
from tests.test_chat_web_steering import (
    _FakeConnection,
    _FakeRedisKV,
    _FakeRedisRL,
    _FakeSession,
    _run_connection,
    _send,
    _steer,
    _wait_for_frame,
)

CONV = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


# ── Fakes ─────────────────────────────────────────────────────────────────


class _FakePipeline:
    """MULTI/EXEC stand-in: commands are queued, applied in order on execute."""

    def __init__(self, redis: _FakeStreamRedis) -> None:
        self._redis = redis
        self._queued: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        def _queue(*args, **kwargs):
            self._queued.append((name, args, kwargs))
            return self

        return _queue

    async def execute(self, raise_on_error: bool = True):
        if self._redis.fail_pipelines:
            raise ConnectionError("redis down")
        results = []
        for name, args, kwargs in self._queued:
            results.append(await getattr(self._redis, name)(*args, **kwargs))
        await self._redis.notify_write()
        return results


class _FakePubSub:
    def __init__(self, redis: _FakeStreamRedis) -> None:
        self._redis = redis
        self._queue: asyncio.Queue = asyncio.Queue()
        self.channels: set[str] = set()
        self.fail_next_read = False
        self.fail_next_subscribe = False

    async def subscribe(self, *channels: str) -> None:
        if self.fail_next_subscribe:
            self.fail_next_subscribe = False
            raise TimeoutError("SUBSCRIBE timed out on a live connection")
        for ch in channels:
            self.channels.add(ch)
            self._redis.subscribers.setdefault(ch, set()).add(self)
            self.deliver(
                {"type": "subscribe", "pattern": None, "channel": ch, "data": len(self.channels)}
            )

    async def unsubscribe(self, *channels: str) -> None:
        for ch in channels:
            self.channels.discard(ch)
            self._redis.subscribers.get(ch, set()).discard(self)

    def deliver(self, message: dict) -> None:
        self._queue.put_nowait(message)

    async def get_message(self, ignore_subscribe_messages: bool = False, timeout: float = 0.0):
        if self.fail_next_read:
            self.fail_next_read = False
            raise ConnectionError("subscription lost")
        try:
            message = await asyncio.wait_for(self._queue.get(), timeout=timeout or 0.001)
        except TimeoutError:
            return None
        if ignore_subscribe_messages and message["type"] in ("subscribe", "unsubscribe"):
            return None
        return message

    async def aclose(self) -> None:
        self._drop_subscriptions()

    def _drop_subscriptions(self) -> None:
        # By what the server holds, not ``channels``: a close resets that first.
        for subscribers in self._redis.subscribers.values():
            subscribers.discard(self)
        self.channels = set()

    @property
    def connection(self) -> _FakePubSubConnection | None:
        """What ``close_pubsub()`` closes: Redis drops a closed socket's subscriptions."""
        if not hasattr(self, "_connection"):
            self._connection = _FakePubSubConnection(self)
        return self._connection

    @connection.setter
    def connection(self, value) -> None:
        self._connection = value


class _FakePubSubConnection:
    def __init__(self, pubsub: _FakePubSub) -> None:
        self._pubsub = pubsub

    async def disconnect(self, nowait: bool = False) -> None:
        self._pubsub._drop_subscriptions()

    def deregister_connect_callback(self, _callback) -> None:
        pass


class _FakePubSubClient:
    def __init__(self, redis: _FakeStreamRedis) -> None:
        self._redis = redis

    def pubsub(self) -> _FakePubSub:
        return _FakePubSub(self._redis)

    async def aclose(self) -> None:
        return None


class _FakeStreamRedis(_FakeRedisKV):
    """The steering fake plus everything the fan-out needs: hashes, APPEND,
    list range / trim, MULTI/EXEC pipelines, PUBLISH and the subscriber side
    (a message is delivered to every fake pub/sub subscribed to its channel)."""

    def __init__(self) -> None:
        super().__init__()
        self.hashes: dict[str, dict[str, str]] = {}
        self.published: list[tuple[str, str]] = []
        self.subscribers: dict[str, set[_FakePubSub]] = {}
        self.timeline: list[tuple] | None = None
        self.fail_pipelines = False
        self._written = asyncio.Condition()

    # -- keys --------------------------------------------------------------
    async def delete(self, *keys):
        removed = 0
        for key in keys:
            for store in (self.kv, self.lists, self.hashes):
                if key in store:
                    del store[key]
                    removed += 1
        return removed

    async def exists(self, *keys):
        return sum(
            1 for k in keys if k in self.kv or k in self.lists or k in self.hashes
        )

    async def expire(self, key, ttl):
        return 1 if key in self.kv or key in self.lists or key in self.hashes else 0

    async def eval(self, script, numkeys, *keys_and_args):
        result = await super().eval(script, numkeys, *keys_and_args)
        if "DEL" in script and result and self.timeline is not None:
            self.timeline.append(("release",))
        return result

    # -- hashes / strings / lists -----------------------------------------
    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def hset(self, key, mapping=None, **_kw):
        self.hashes.setdefault(key, {}).update(
            {str(k): str(v) for k, v in (mapping or {}).items()}
        )
        return len(mapping or {})

    async def append(self, key, value):
        self.kv[key] = self.kv.get(key, "") + value
        return len(self.kv[key])

    async def lrange(self, key, start, stop):
        lst = self.lists.get(key, [])
        return list(lst[start : None if stop == -1 else stop + 1])

    async def ltrim(self, key, start, stop):
        lst = self.lists.get(key)
        if lst is None:
            return True
        n = len(lst)
        s = start if start >= 0 else max(0, n + start)
        e = n if stop == -1 else stop + 1
        self.lists[key] = lst[s:e]
        return True

    # -- pub/sub -----------------------------------------------------------
    async def publish(self, channel, payload):
        self.published.append((channel, payload))
        if self.timeline is not None:
            self.timeline.append(("publish", channel, payload))
        for sub in list(self.subscribers.get(channel, ())):
            sub.deliver(
                {"type": "message", "pattern": None, "channel": channel, "data": payload}
            )
        await self.notify_write()
        return len(self.subscribers.get(channel, ()))

    def pipeline(self, transaction: bool = True) -> _FakePipeline:
        return _FakePipeline(self)

    # -- test helpers -----------------------------------------------------
    def frames(self, channel: str | None = None) -> list[dict]:
        out: list[dict] = []
        for ch, payload in self.published:
            if channel is not None and ch != channel:
                continue
            out.extend(json.loads(payload))
        return out

    async def notify_write(self) -> None:
        async with self._written:
            self._written.notify_all()

    async def wait_for_write(self, predicate) -> None:
        """Return once *predicate* holds, re-checked at every write.

        A PUBLISH or an executed MULTI/EXEC wakes it at once; a short
        re-check covers a write made outside both. The running turn's
        frames and journal land when the turn gets there: a loaded CI shard
        delays them, it cannot make a correct publisher fail, so no short
        wall clock decides. The bound is the hang guard of
        ``_BlockingFakeWebSocket.wait_for_frame``, not the contract.
        """
        deadline = asyncio.get_running_loop().time() + _FRAME_HANG_GUARD_SECONDS
        async with self._written:
            while not predicate():
                if asyncio.get_running_loop().time() >= deadline:
                    raise AssertionError("condition not met within the hang guard")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._written.wait(), timeout=0.05)


async def _events(env, *, client_id: str = "viewer") -> tuple[asyncio.Task, list[dict]]:
    """A viewer tab: ``GET /web/chat/conversations/{id}/events``, read as it streams.

    Returns the reading task (cancel it with :func:`_stop`) and the frames it decoded so far.
    """
    response = await chat_web_stream.stream_web_chat_events(
        str(env.conv.id),
        SimpleNamespace(headers={}),
        client_id=client_id,
        db=SimpleNamespace(execute=_FakeSession(env.conv, []).execute, in_transaction=lambda: False),
        user=SimpleNamespace(id=env.user_id, web_session=SimpleNamespace()),
    )
    frames: list[dict] = []

    async def _read():
        async for chunk in response.body_iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            if text.startswith("data: "):
                frames.append(json.loads(text[6:]))

    return asyncio.create_task(_read()), frames


async def _attached_viewer(env) -> tuple[asyncio.Task, list[dict]]:
    """A viewer whose subscription is confirmed, so it misses no frame of the next send."""
    task, frames = await _events(env)
    await _wait_until(lambda: env.redis.subscribers.get(turn_channel(str(env.conv.id))))
    await asyncio.sleep(0.02)
    return task, frames


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _start(env, content: str = "build the report") -> tuple[_FakeConnection, asyncio.Task]:
    conn = _FakeConnection(str(env.conv.id), {"content": content}, [])
    return conn, asyncio.create_task(_run_connection(conn, env.user_id))


# How long a wait on a fake write or a relayed frame may take before the test
# fails as hung: the guard of a correct publisher on a loaded CI shard, not a contract.
_FRAME_HANG_GUARD_SECONDS = 60.0


class _FrameSink:
    """The ``send`` of a bare relay: it keeps the frames and wakes waiters.

    Same contract as ``wait_for_frame``: woken by every frame relayed,
    never by a wall-clock poll, bounded by the hang guard only.
    """

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self._cond = asyncio.Condition()

    async def send(self, frame: dict) -> bool:
        self.frames.append(frame)
        async with self._cond:
            self._cond.notify_all()
        return True

    async def wait_until(self, predicate) -> None:
        async with self._cond:
            await asyncio.wait_for(self._cond.wait_for(predicate), timeout=_FRAME_HANG_GUARD_SECONDS)


async def _wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


def _types(frames: list[dict]) -> list[tuple[str, str | None]]:
    return [(f.get("type"), f.get("status")) for f in frames]


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture
def fanout_redis(monkeypatch):
    import app.core.redis as redis_module

    fake = _FakeStreamRedis()

    async def _client():
        return fake

    async def _pubsub_client():
        return _FakePubSubClient(fake)

    monkeypatch.setattr(redis_module, "redis_client", _client)
    monkeypatch.setattr(chat_turn_stream, "_pubsub_client", _pubsub_client)
    monkeypatch.setattr(chat_turn_stream, "DRAIN_INTERVAL_SECONDS", 0.005)
    # The quiet period is process-wide: a test that trips it (or an earlier
    # suite that ran without Redis) must not silence this one.
    monkeypatch.setattr(chat_turn_stream, "_down_until", 0.0)
    return fake


@pytest.fixture
def fake_rl(monkeypatch):
    fake = _FakeRedisRL()

    async def _client():
        return fake

    monkeypatch.setattr(rate_limit, "redis_client", _client)
    monkeypatch.setattr(config.settings, "rate_limit_fail_mode", "open")
    return fake


@pytest.fixture
def ws_env(monkeypatch, fake_rl, fanout_redis):
    """Same environment shape as ``test_chat_web_steering.ws_env``, on the
    fan-out-capable Redis fake, with stored messages rich enough for the
    chat API's message serializer (the relayed ``user_message`` frame)."""
    import app.api.v1.platform as platform_module
    import app.core.database as database_module
    import app.llm.router as llm_router_module

    user_id = uuid.uuid4()
    conv = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        agent_id=None,
        title="fan-out test",
        transfer_source_agent_id=None,
        tool_overrides_json=None,
        presentation_id=None,
        video_project_id=None,
        custom_dashboard_id=None,
        fleet_id=None,
    )
    timeline: list[tuple] = []
    fanout_redis.timeline = timeline
    stored: list[tuple[str, str]] = []
    env = SimpleNamespace(
        user_id=user_id,
        conv=conv,
        timeline=timeline,
        stored=stored,
        redis=fanout_redis,
    )

    async def _chat_enabled(_db):
        return True

    monkeypatch.setattr(platform_module, "is_chat_enabled", _chat_enabled)

    # The account-state gate a turn runs again is covered by
    # tests/test_deactivated_account_auth.py.
    async def _account_ok(_db, _uid, _issued_at, **_kw):
        return None

    monkeypatch.setattr(chat_module, "resolve_account_rejection", _account_ok)

    async def _session_holds(session):
        return session

    monkeypatch.setattr(chat_module, "recheck_session", _session_holds)
    monkeypatch.setattr(
        database_module, "async_session", lambda: _FakeSession(conv, timeline)
    )

    monkeypatch.setattr(chat_module, "_spawn_cancel_cascade", lambda _cid, *_a: None)
    monkeypatch.setattr(chat_turn_guard, "BRIDGE_POLL_SECONDS", 0.02)

    sequence = {"n": 0}

    async def _store_message(_db, _conv_id, role, content, **_kw):
        sequence["n"] += 1
        timeline.append(("persist", role, content))
        stored.append((role, content))
        return SimpleNamespace(
            id=uuid.uuid4(),
            conversation_id=conv.id,
            role=role,
            content=content,
            channel="web",
            channel_message_id=None,
            sequence=sequence["n"],
            created_at=datetime.now(UTC),
            metadata_json=None,
            llm_trace_id=None,
        )

    monkeypatch.setattr(chat_module, "store_message", _store_message)

    async def _history(_db, _conv_id):
        return []

    monkeypatch.setattr(chat_module, "get_conversation_history", _history)

    async def _langfuse_ids(_conv_id):
        return {}

    monkeypatch.setattr(chat_module, "load_latest_langfuse_ids", _langfuse_ids)

    async def _no_llm(_db, _uid):
        raise RuntimeError("no LLM in tests")

    monkeypatch.setattr(llm_router_module, "get_llm_for_user", _no_llm)
    return env


def _patch_general_chat(monkeypatch, stub):
    import app.services.general_chat as general_chat_module

    monkeypatch.setattr(general_chat_module, "run_general_chat", stub)


# ── Pure helpers ──────────────────────────────────────────────────────────


def test_coalesce_merges_only_adjacent_chunks_of_the_same_kind():
    batch = [
        {"type": "stream", "content": "Hel"},
        {"type": "stream", "content": "lo"},
        {"type": "status", "status": "thinking_text", "content": "a"},
        {"type": "status", "status": "thinking_text", "content": "b"},
        {"type": "status", "status": "tool_start", "tools": ["x"]},
        {"type": "stream", "content": " again"},
    ]
    out = coalesce_frames(batch)
    assert out == [
        {"type": "stream", "content": "Hello"},
        {"type": "status", "status": "thinking_text", "content": "ab"},
        {"type": "status", "status": "tool_start", "tools": ["x"]},
        {"type": "stream", "content": " again"},
    ]
    # Inputs are never mutated.
    assert batch[0] == {"type": "stream", "content": "Hel"}


def test_slim_frame_drops_the_heavy_tool_done_payload_only():
    done = {
        "type": "status",
        "status": "tool_done",
        "tool": "search",
        "success": True,
        "tool_input": "q",
        "tool_input_full": "q" * 500,
        "tool_output": "x" * 5000,
        "tool_output_full": "x" * 50000,
    }
    assert slim_frame(done) == {
        "type": "status",
        "status": "tool_done",
        "tool": "search",
        "success": True,
        "tool_input": "q",
    }
    start = {"type": "status", "status": "tool_start", "tools": ["search"]}
    assert slim_frame(start) is start
    # A consulted agent's tool completion rides ``consult_progress`` with the
    # ``tool_done`` event under ``inner``: stripped the same way, in place.
    inner = {k: v for k, v in done.items() if k != "type"}
    consult = {
        "type": "status",
        "status": "consult_progress",
        "agent_name": "Analyst",
        "inner": inner,
    }
    assert slim_frame(consult) == {
        "type": "status",
        "status": "consult_progress",
        "agent_name": "Analyst",
        "inner": {"status": "tool_done", "tool": "search", "success": True, "tool_input": "q"},
    }
    light_consult = {
        "type": "status",
        "status": "consult_progress",
        "inner": {"status": "tool_start", "tools": ["search"]},
    }
    assert slim_frame(light_consult) is light_consult


def test_slim_frame_drops_the_evaluation_only_prompt_copies():
    """``llm_step`` / ``tool_start`` carry the prompt and raw-response copies
    only the evaluation wrapper reads, up to ~190K characters per model call;
    every other field - the status bubble reads the frame - stays."""
    copies = {
        "llm_system_prompt": "s" * 30_000,
        "llm_user_prompt": "u" * 80_000,
        "llm_raw_response": "r" * 80_000,
    }
    step = {
        "type": "status",
        "status": "llm_step",
        "iteration": 3,
        "llm_tools": ["search"],
        "llm_usage": {"input_tokens": 10},
        **copies,
    }
    slim_step = {
        "type": "status",
        "status": "llm_step",
        "iteration": 3,
        "llm_tools": ["search"],
        "llm_usage": {"input_tokens": 10},
    }
    assert slim_frame(step) == slim_step
    start = {"type": "status", "status": "tool_start", "tools": ["search"], **copies}
    assert slim_frame(start) == {"type": "status", "status": "tool_start", "tools": ["search"]}
    consult = {
        "type": "status",
        "status": "consult_progress",
        "agent_name": "Analyst",
        "inner": {k: v for k, v in step.items() if k != "type"},
    }
    assert slim_frame(consult) == {
        "type": "status",
        "status": "consult_progress",
        "agent_name": "Analyst",
        "inner": {k: v for k, v in slim_step.items() if k != "type"},
    }
    # Inputs are never mutated.
    assert step["llm_user_prompt"] == copies["llm_user_prompt"]


def test_slim_frame_strips_a_peer_consulted_in_turn_under_both_wrappers():
    """A consulted agent that consults a peer of its own relays the
    peer's events under a second ``consult_progress``: stripped the same way."""
    done = {"status": "tool_done", "tool": "search", "success": True, "tool_output": "x" * 5000}
    nested = {
        "type": "status",
        "status": "consult_progress",
        "agent_name": "Clerk",
        "inner": {"status": "consult_progress", "agent_name": "Analyst", "inner": done},
    }
    assert slim_frame(nested) == {
        "type": "status",
        "status": "consult_progress",
        "agent_name": "Clerk",
        "inner": {
            "status": "consult_progress",
            "agent_name": "Analyst",
            "inner": {"status": "tool_done", "tool": "search", "success": True},
        },
    }
    assert done["tool_output"] == "x" * 5000, "inputs are never mutated"


def test_is_terminal_frame_excludes_a_mid_turn_segment_commit():
    assert is_terminal_frame({"type": "done", "content": "final"})
    assert is_terminal_frame({"type": "cancelled"})
    assert is_terminal_frame({"type": "error", "content": "boom"})
    # An intermediate ``done`` commits a segment; the turn goes on.
    assert not is_terminal_frame({"type": "done", "content": "seg", "intermediate": True})
    assert not is_terminal_frame({"type": "stream", "content": "x"})
    assert interrupted_frame() == {
        "type": "error",
        "content": TURN_INTERRUPTED_MESSAGE,
        "reason": "interrupted",
    }


def test_resume_frames_replay_the_journal_in_the_clients_order():
    snapshot = TurnSnapshot(
        token="tok",
        seq=7,
        started_at=1000.0,
        statuses=[
            {"type": "status", "status": "thinking", "iteration": 1},
            {"type": "status", "status": "tool_start", "tools": ["search"]},
        ],
        reasoning="Let me think.",
        answer="Partial ans",
    )
    frames = resume_frames(snapshot, now=1042.4)
    assert frames[0] == {"type": "thinking", "resumed": True, "elapsed_s": 42.4}
    assert frames[1:3] == snapshot.statuses
    assert frames[3] == {"type": "status", "status": "thinking_text", "content": "Let me think."}
    assert frames[4] == {"type": "stream", "content": "Partial ans"}
    # A bare snapshot (marker, no journal) is announced and nothing more.
    assert resume_frames(TurnSnapshot(token="tok")) == [{"type": "thinking", "resumed": True}]


# ── Publisher ─────────────────────────────────────────────────────────────


async def test_publisher_journals_the_turn_and_publishes_stamped_batches(fanout_redis):
    pub = TurnPublisher(CONV, "tok", origin="sock-a")
    pub.start()
    await pub.on_event({"status": "thinking", "iteration": 1})
    await pub.on_event({"status": "thinking_text", "content": "Let me "})
    await pub.on_event({"status": "thinking_text", "content": "think."})
    await pub.on_event({"status": "tool_start", "tools": ["search"]})
    await pub.on_event({"status": "tool_heartbeat", "elapsed_s": 10})
    await pub.on_event(
        {
            "status": "tool_done",
            "tool": "search",
            "success": True,
            "tool_input": "q",
            "tool_input_full": "q" * 100,
            "tool_output": "x" * 1000,
        }
    )
    await pub.on_event(
        {"status": "consult_progress", "agent_name": "Peer", "inner": {"status": "thinking_text"}}
    )
    # The reasoning of a peer the consulted agent consulted in turn.
    await pub.on_event(
        {
            "status": "consult_progress",
            "agent_name": "Peer",
            "inner": {"status": "consult_progress", "agent_name": "Analyst", "inner": {"status": "thinking_text"}},
        }
    )
    await pub.on_event(
        {"status": "consult_progress", "agent_name": "Peer", "inner": {"status": "tool_start"}}
    )
    pub.offer({"type": "stream", "content": "Hel"})
    pub.offer({"type": "stream", "content": "lo"})
    await fanout_redis.wait_for_write(lambda: fanout_redis.published)

    state_key, status_key, reasoning_key, answer_key = journal_keys(CONV, "tok")
    assert fanout_redis.kv[reasoning_key] == "Let me think."
    assert fanout_redis.kv[answer_key] == "Hello"
    statuses = [json.loads(s) for s in fanout_redis.lists[status_key]]
    # Chatty statuses stay out of the list; the heavy tool output is stripped.
    assert [s["status"] for s in statuses] == [
        "thinking",
        "tool_start",
        "tool_done",
        "consult_progress",
    ]
    assert "tool_output" not in statuses[2] and statuses[2]["tool_input"] == "q"
    assert statuses[3]["inner"] == {"status": "tool_start"}
    assert "origin" not in statuses[0] and "seq" not in statuses[0]
    assert fanout_redis.hashes[state_key]["seq"] == str(pub.seq)
    assert float(fanout_redis.hashes[state_key]["started_at"]) == pytest.approx(
        pub.started_at, abs=0.01
    )

    frames = fanout_redis.frames(turn_channel(CONV))
    assert frames[0]["type"] == "thinking"
    assert all(f["origin"] == "sock-a" and f["turn"] == "tok" for f in frames)
    assert [f["seq"] for f in frames] == list(range(1, len(frames) + 1))
    texts = [f for f in frames if f.get("status") == "thinking_text"]
    assert [t["content"] for t in texts] == ["Let me think."]
    streams = [f for f in frames if f["type"] == "stream"]
    assert [s["content"] for s in streams] == ["Hello"]
    # Chatty frames are relayed LIVE even though they are not journaled.
    assert any(f.get("status") == "tool_heartbeat" for f in frames)
    await pub.aclose()


async def test_publisher_resets_mirror_the_clients_own_state_transitions(fanout_redis):
    pub = TurnPublisher(CONV, "tok")
    pub.start()
    _, status_key, reasoning_key, answer_key = journal_keys(CONV, "tok")

    pub.offer({"type": "stream", "content": "provisional"})
    await fanout_redis.wait_for_write(lambda: fanout_redis.kv.get(answer_key) == "provisional")
    # A per-iteration ``thinking`` status: the client resets its stream.
    await pub.on_event({"status": "thinking", "iteration": 2})
    await fanout_redis.wait_for_write(lambda: answer_key not in fanout_redis.kv)

    pub.offer({"type": "stream", "content": "retracted"})
    await fanout_redis.wait_for_write(lambda: fanout_redis.kv.get(answer_key) == "retracted")
    await pub.on_event({"status": "stream_retract"})
    await fanout_redis.wait_for_write(lambda: answer_key not in fanout_redis.kv)
    assert not any(
        json.loads(s)["status"] == "stream_retract" for s in fanout_redis.lists[status_key]
    )

    await pub.on_event({"status": "thinking_text", "content": "reasoning"})
    await fanout_redis.wait_for_write(lambda: fanout_redis.kv.get(reasoning_key) == "reasoning")
    await pub.on_event({"status": "transfer_start", "agent_name": "Other"})
    await fanout_redis.wait_for_write(lambda: reasoning_key not in fanout_redis.kv)

    await pub.on_event({"status": "thinking_text", "content": "segment two"})
    pub.offer({"type": "stream", "content": "segment two answer"})
    await fanout_redis.wait_for_write(lambda: fanout_redis.kv.get(answer_key) == "segment two answer")
    # A committed mid-turn segment resets both texts, and is never
    # journaled itself: the reader's refetch already holds the row.
    pub.offer({"type": "done", "content": "segment two answer", "intermediate": True})
    await fanout_redis.wait_for_write(lambda: answer_key not in fanout_redis.kv)
    assert reasoning_key not in fanout_redis.kv
    assert not any(json.loads(s)["type"] == "done" for s in fanout_redis.lists[status_key])
    await pub.aclose()


async def test_publisher_publishes_the_terminal_frame_then_drops_the_journal(fanout_redis):
    pub = TurnPublisher(CONV, "tok")
    pub.start()
    await pub.on_event({"status": "tool_start", "tools": ["x"]})
    await fanout_redis.wait_for_write(lambda: fanout_redis.published)
    assert any(k in fanout_redis.lists for k in journal_keys(CONV, "tok"))

    pub.offer({"type": "done", "content": "final", "message_id": "m-1"})
    await pub.aclose()
    assert pub.closed
    assert fanout_redis.frames()[-1]["type"] == "done"
    state_key, *content_keys = journal_keys(CONV, "tok")
    assert not any(
        k in fanout_redis.kv or k in fanout_redis.lists or k in fanout_redis.hashes
        for k in content_keys
    )
    # What stays is the ``ended`` tombstone, on a bounded TTL.
    assert fanout_redis.hashes[state_key]["ended"] == "1"
    # Closed: later offers are ignored, closing again is a no-op.
    published = len(fanout_redis.published)
    pub.offer({"type": "stream", "content": "late"})
    await pub.aclose()
    await asyncio.sleep(0.02)
    assert len(fanout_redis.published) == published


async def test_the_terminal_frame_does_not_wait_out_the_drain_window(fanout_redis, monkeypatch):
    """The owner's ``done`` and the REST response wait for :meth:`aclose`:
    it used to sit through the rest of the drain window and a second
    round-trip for the tombstone. A terminal frame, or the
    close, now cuts the window short; the order is unchanged - the terminal
    frame is published, the journal dropped, the tombstone left."""
    monkeypatch.setattr(chat_turn_stream, "DRAIN_INTERVAL_SECONDS", 5.0)
    pub = TurnPublisher(CONV, "tok")
    pub.start()
    await pub.on_event({"status": "tool_start", "tools": ["x"]})

    started = asyncio.get_event_loop().time()
    pub.offer({"type": "done", "content": "final", "message_id": "m-1"})
    await pub.aclose()
    elapsed = asyncio.get_event_loop().time() - started

    assert elapsed < 1.0
    assert [f["type"] for f in fanout_redis.frames()][-1] == "done"
    state_key, *content_keys = journal_keys(CONV, "tok")
    assert not any(
        k in fanout_redis.kv or k in fanout_redis.lists or k in fanout_redis.hashes
        for k in content_keys
    )
    assert fanout_redis.hashes[state_key]["ended"] == "1"


async def test_a_closed_turn_is_no_turn_to_resume_into_while_its_marker_lingers(
    fanout_redis,
):
    """The owner closes the publisher (terminal frame out, journal dropped)
    and releases the marker right after. A socket attaching in between has
    missed the terminal frame and still sees the marker: resumed from the
    empty journal, it would sit on "Thinking" until its safety poll. The
    ``ended`` tombstone makes that interval read as no turn at all."""
    token = await chat_turn_guard.acquire_turn(CONV)
    pub = TurnPublisher(CONV, token)
    pub.start()
    await pub.on_event({"status": "tool_start", "tools": ["x"]})
    await fanout_redis.wait_for_write(lambda: fanout_redis.published)
    assert (await read_snapshot(CONV)) is not None

    pub.offer({"type": "done", "content": "final"})
    await pub.aclose()
    assert await chat_turn_guard.is_turn_active(CONV), "marker still held here"
    assert await read_snapshot(CONV) is None
    # A viewer attaching now is not resumed into anything.
    sink = _FrameSink()
    sent = sink.frames

    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=sink.send))
    try:
        await _wait_until(lambda: chat_turn_stream._hub()._ready)
        await asyncio.sleep(0.05)
        assert sent == []
    finally:
        relay.cancel()
        with pytest.raises(asyncio.CancelledError):
            await relay
    assert await chat_turn_guard.release_turn(CONV, token)
    assert await read_snapshot(CONV) is None


async def _resynced_viewer(monkeypatch, resyncs: int, origin: str = "tab-after-restart") -> list[dict]:
    """What a viewer attaching now is sent over *resyncs* resyncs of its stream."""
    queue: asyncio.Queue = asyncio.Queue()

    class _ScriptedHub:
        async def attach(self, _conversation_id: str) -> asyncio.Queue:
            return queue

        async def detach(self, _conversation_id: str, _queue: asyncio.Queue) -> None:
            return None

    monkeypatch.setattr(chat_turn_stream, "_hub", lambda: _ScriptedHub())
    for _ in range(resyncs):
        queue.put_nowait(RESYNC)
    sent: list[dict] = []

    async def _send(frame: dict) -> bool:
        sent.append(frame)
        return True

    relay = asyncio.create_task(relay_turn_frames(CONV, origin=origin, send=_send))
    try:
        await _wait_until(queue.empty)
        await asyncio.sleep(0.05)
    finally:
        relay.cancel()
        with pytest.raises(asyncio.CancelledError):
            await relay
    return sent


async def test_a_stream_that_attaches_after_a_restart_cut_the_turn_hears_how_it_ended(
    fanout_redis, monkeypatch
):
    """A restart cuts the turn while the client's streams are down: its
    interrupted terminal goes out to nobody. The stream the client reopens
    once the backend is back finds no turn in flight, and is told how the cut
    turn ended - once, named as its frames named it - instead of waiting for
    a replay that never comes."""
    token = await chat_turn_guard.acquire_turn(CONV)
    pub = TurnPublisher(CONV, token, origin="tab-before-restart")
    pub.start()
    await pub.on_event({"status": "tool_start", "tools": ["x"]})
    await pub.aclose(terminal=interrupted_frame())
    assert await chat_turn_guard.release_turn(CONV, token)

    sent = await _resynced_viewer(monkeypatch, resyncs=2)

    assert [f["type"] for f in sent] == ["error"], "said once, over two resyncs"
    assert sent[0]["reason"] == interrupted_frame()["reason"]
    assert sent[0]["turn_id"] == chat_turn_stream.public_turn_id(token)
    assert sent[0]["relayed"] is True
    assert not {"origin", "turn", "seq"} & sent[0].keys()


async def test_a_stream_that_attaches_after_a_turn_reached_its_own_end_hears_nothing(
    fanout_redis, monkeypatch
):
    """Only a turn the shutdown cut is reported: one that reached its own end
    is over on every client already."""
    token = await chat_turn_guard.acquire_turn(CONV)
    pub = TurnPublisher(CONV, token, origin="tab")
    pub.start()
    pub.offer({"type": "done", "content": "final", "message_id": "m-1"})
    await pub.aclose(terminal=interrupted_frame())
    assert await chat_turn_guard.release_turn(CONV, token)

    assert await _resynced_viewer(monkeypatch, resyncs=1) == []


async def test_aclose_synthesizes_the_interrupted_terminal_only_when_none_was_offered(
    fanout_redis,
):
    """The abnormal exits (cancellation, a persistence failure, a server
    shutdown) close the publisher with ``interrupted_frame()``: viewers
    leave the working state NOW instead of at their next safety poll. A
    turn that ended properly is never told twice, and a mid-turn segment
    commit is not a terminal."""
    proper = TurnPublisher(CONV, "tok-done")
    proper.start()
    proper.offer({"type": "done", "content": "final", "message_id": "m-1"})
    assert proper.terminal_offered
    await proper.aclose(terminal=interrupted_frame())
    frames = fanout_redis.frames()
    assert [f["type"] for f in frames] == ["thinking", "done"]

    fanout_redis.published.clear()
    torn = TurnPublisher(CONV, "tok-torn")
    torn.start()
    await torn.on_event({"status": "tool_start", "tools": ["x"]})
    torn.offer({"type": "done", "content": "segment", "intermediate": True})
    assert not torn.terminal_offered, "an intermediate done is not the end of the turn"
    await torn.aclose(terminal=interrupted_frame())
    frames = fanout_redis.frames()
    assert frames[-1]["type"] == "error"
    assert frames[-1]["content"] == TURN_INTERRUPTED_MESSAGE
    assert frames[-1]["turn"] == "tok-torn" and frames[-1]["seq"] == len(frames)
    # Closed like any other turn: content keys gone, the ``ended`` tombstone left.
    state_key, *content_keys = journal_keys(CONV, "tok-torn")
    assert not any(
        k in fanout_redis.kv or k in fanout_redis.lists or k in fanout_redis.hashes
        for k in content_keys
    )
    assert fanout_redis.hashes[state_key]["ended"] == "1"

    # A plain ``aclose()`` still synthesizes nothing.
    fanout_redis.published.clear()
    quiet = TurnPublisher(CONV, "tok-quiet")
    quiet.start()
    await fanout_redis.wait_for_write(lambda: fanout_redis.published)
    await quiet.aclose()
    assert [f["type"] for f in fanout_redis.frames()] == ["thinking"]


async def test_publisher_never_raises_into_the_turn_when_redis_fails(fanout_redis, caplog):
    fanout_redis.fail_pipelines = True
    pub = TurnPublisher(CONV, "tok")
    pub.start()
    await pub.on_event({"status": "tool_start", "tools": ["x"]})
    await asyncio.sleep(0.03)
    pub.offer({"type": "done", "content": "final"})
    await pub.aclose()
    assert fanout_redis.published == []
    # One warning, then the quiet period — shared by every later publisher
    # and by the live path, so a dead Redis is paid for once per cooldown,
    # not once per turn.
    warnings = [r for r in caplog.records if "fan-out publish failed" in r.getMessage()]
    assert len(warnings) == 1
    assert chat_turn_stream._redis_is_quiet()
    other = TurnPublisher(CONV, "tok-2")
    other.start()
    await other.on_event({"status": "tool_start", "tools": ["y"]})
    await asyncio.sleep(0.03)
    await other.aclose()
    chat_turn_stream.publish_live(CONV, {"type": "user_message", "message": {}}, origin="o")
    await asyncio.sleep(0.02)
    assert fanout_redis.published == []
    assert len([r for r in caplog.records if "failed for" in r.getMessage()]) == 1


async def test_publisher_ignores_socket_local_and_relayed_frames(fanout_redis):
    pub = TurnPublisher(CONV, "tok")
    pub.start()
    await fanout_redis.wait_for_write(lambda: fanout_redis.published)  # the ack
    before = len(fanout_redis.published)
    pub.offer({"type": "pong"})
    pub.offer({"type": "steered", "steer_id": "s"})
    pub.offer({"type": "folded_into_active_turn"})
    # A frame this socket RECEIVED from the relay must not be republished
    # under a new origin (a fan-out loop).
    pub.offer({"type": "status", "status": "tool_start", "relayed": True})
    pub.offer("not a frame")
    await asyncio.sleep(0.03)
    assert len(fanout_redis.published) == before
    await pub.aclose()


# ── Snapshot ──────────────────────────────────────────────────────────────


async def test_read_snapshot_is_none_without_a_turn_in_flight(fanout_redis):
    assert await read_snapshot(CONV) is None


async def test_read_snapshot_reads_the_journal_of_the_marker_holder(fanout_redis):
    token = await chat_turn_guard.acquire_turn(CONV)
    pub = TurnPublisher(CONV, token)
    pub.start()
    await pub.on_event({"status": "tool_start", "tools": ["search"]})
    await pub.on_event({"status": "thinking_text", "content": "so far"})
    pub.offer({"type": "stream", "content": "partial"})
    await fanout_redis.wait_for_write(lambda: fanout_redis.published)

    snapshot = await read_snapshot(CONV)
    assert snapshot is not None
    assert snapshot.token == token
    assert snapshot.seq == pub.seq
    assert snapshot.started_at == pytest.approx(pub.started_at, abs=0.01)
    assert [s["status"] for s in snapshot.statuses] == ["tool_start"]
    assert snapshot.reasoning == "so far"
    assert snapshot.answer == "partial"
    await pub.aclose()


async def test_read_snapshot_with_an_unreadable_journal_is_the_marker_alone(fanout_redis):
    token = await chat_turn_guard.acquire_turn(CONV)
    fanout_redis.fail_pipelines = True
    snapshot = await read_snapshot(CONV)
    assert snapshot == TurnSnapshot(token=token)


async def test_the_turns_agentic_charges_survive_the_status_trim(fanout_redis, monkeypatch):
    """A charge is the FIRST status of its pass, the list keeps the LAST ones."""
    monkeypatch.setattr(chat_turn_stream, "MAX_JOURNALED_STATUS_FRAMES", 3)
    token = await chat_turn_guard.acquire_turn(CONV)
    pub = TurnPublisher(CONV, token, origin="sock-a")
    pub.start()
    await pub.on_event({"status": AGENTIC_ACTION_STATUS, "actions": 1, "period": "2026-09-26"})
    for iteration in range(1, 6):
        await pub.on_event({"status": "thinking", "iteration": iteration})
    # Later passes (a steering message folded in) are charged again - here
    # after a daily period rolled over, and once with no period named.
    await pub.on_event({"status": AGENTIC_ACTION_STATUS, "actions": 1, "period": "2026-09-27"})
    await pub.on_event({"status": AGENTIC_ACTION_STATUS, "actions": 2, "period": "2026-09-27"})
    await pub.on_event({"status": AGENTIC_ACTION_STATUS, "actions": 1})
    await pub.on_event({"status": "thinking", "iteration": 6})

    totals = {"": 1, "2026-09-26": 1, "2026-09-27": 3}
    state_key, status_key, _, _ = journal_keys(CONV, token)
    await fanout_redis.wait_for_write(
        lambda: (
            fanout_redis.hashes.get(state_key, {}).get("agentic_actions") == json.dumps(totals, sort_keys=True)
            and len(fanout_redis.lists.get(status_key, [])) == 3
        )
    )
    statuses = [json.loads(s) for s in fanout_redis.lists[status_key]]
    assert [s["iteration"] for s in statuses] == [4, 5, 6]
    # Viewers attached now receive every charge live, one frame each.
    live = [
        f for f in fanout_redis.frames(turn_channel(CONV))
        if f.get("type") == AGENTIC_ACTION_FRAME
    ]
    assert [f["actions"] for f in live] == [1, 1, 2, 1]

    snapshot = await read_snapshot(CONV)
    assert snapshot is not None and snapshot.agentic_actions == totals
    frames = resume_frames(snapshot)
    assert frames[0]["type"] == "thinking" and frames[0]["resumed"] is True
    # One frame per period, right after the ack, each naming its period.
    assert frames[1:4] == [
        {"type": AGENTIC_ACTION_FRAME, "actions": 1},
        {"type": AGENTIC_ACTION_FRAME, "actions": 1, "period": "2026-09-26"},
        {"type": AGENTIC_ACTION_FRAME, "actions": 3, "period": "2026-09-27"},
    ]
    assert [f for f in frames[4:] if f.get("type") == AGENTIC_ACTION_FRAME] == []
    await pub.aclose()


async def test_a_turn_resumed_by_itself_tells_every_viewer_live_and_on_attach(fanout_redis):
    """The ``turn_resuming`` frame was dropped by ``offer``; it now
    reaches the live viewers and is replayed, right after the ack, to a socket
    that attaches mid-resume (a reload, another device)."""
    from app.services.cut_turn_resume import RESUMING_FRAME_TYPE

    token = await chat_turn_guard.acquire_turn(CONV)
    pub = TurnPublisher(CONV, token, origin="sweep")
    pub.start()
    pub.offer({"type": RESUMING_FRAME_TYPE, "cause": "stop", "resume": 2, "of": 20})
    await pub.on_event({"status": "thinking", "iteration": 1})
    await fanout_redis.wait_for_write(
        lambda: any(f.get("type") == RESUMING_FRAME_TYPE for f in fanout_redis.frames(turn_channel(CONV)))
    )
    live = [f for f in fanout_redis.frames(turn_channel(CONV)) if f.get("type") == RESUMING_FRAME_TYPE]
    assert [(f["cause"], f["resume"], f["of"]) for f in live] == [("stop", 2, 20)]

    state_key, _, _, _ = journal_keys(CONV, token)
    await fanout_redis.wait_for_write(lambda: "resuming" in fanout_redis.hashes.get(state_key, {}))
    snapshot = await read_snapshot(CONV)
    assert snapshot is not None
    assert snapshot.resuming is not None
    assert {k: snapshot.resuming[k] for k in ("type", "cause", "resume", "of")} == {
        "type": RESUMING_FRAME_TYPE,
        "cause": "stop",
        "resume": 2,
        "of": 20,
    }
    frames = resume_frames(snapshot)
    assert frames[0]["type"] == "thinking" and frames[0]["resumed"] is True
    assert frames[1] == snapshot.resuming
    await pub.aclose()


async def test_the_client_running_a_resumed_turn_hears_it_on_attach(fanout_redis, monkeypatch):
    """The client's own turn is never replayed to it, but its ``turn_resuming``
    frame comes from another producer: an event stream that attaches after it
    went out (a new conversation opens its stream once the send created it) is
    told it, alone, so the line saying the turn resumed by itself shows."""
    from app.services.cut_turn_resume import RESUMING_FRAME_TYPE

    token = await chat_turn_guard.acquire_turn(CONV)
    pub = TurnPublisher(CONV, token, origin="tab")
    pub.start()
    pub.offer({"type": RESUMING_FRAME_TYPE, "cause": "stop", "resume": 1, "of": 100})
    await pub.on_event({"status": "thinking", "iteration": 1})
    state_key, _, _, _ = journal_keys(CONV, token)
    await fanout_redis.wait_for_write(lambda: "resuming" in fanout_redis.hashes.get(state_key, {}))

    own = await _resynced_viewer(monkeypatch, resyncs=1, origin="tab")
    other = await _resynced_viewer(monkeypatch, resyncs=1, origin="other-tab")

    assert [f["type"] for f in own] == [RESUMING_FRAME_TYPE]
    assert (own[0]["cause"], own[0]["resume"], own[0]["of"]) == ("stop", 1, 100)
    assert own[0]["turn_id"] == chat_turn_stream.public_turn_id(token)
    assert own[0]["relayed"] is True
    assert [f["type"] for f in other][:2] == ["thinking", RESUMING_FRAME_TYPE]
    await pub.aclose()


async def test_the_client_running_a_turn_not_resumed_hears_nothing_on_attach(fanout_redis, monkeypatch):
    token = await chat_turn_guard.acquire_turn(CONV)
    pub = TurnPublisher(CONV, token, origin="tab")
    pub.start()
    await pub.on_event({"status": "thinking", "iteration": 1})
    state_key, _, _, _ = journal_keys(CONV, token)
    await fanout_redis.wait_for_write(lambda: "origin" in fanout_redis.hashes.get(state_key, {}))

    assert await _resynced_viewer(monkeypatch, resyncs=1, origin="tab") == []
    await pub.aclose()


def test_a_turn_that_was_not_resumed_replays_no_resuming_frame():
    assert all(
        f.get("type") != "turn_resuming" for f in resume_frames(TurnSnapshot(token="tok"))
    )


@pytest.mark.parametrize("raw", [None, "", "not json", "[]", '{"type": "status"}'])
def test_a_malformed_resuming_record_replays_nothing(raw):
    assert chat_turn_stream._read_resuming(raw) is None


async def test_a_charge_dropped_by_a_quiet_period_stays_in_the_totals(fanout_redis, monkeypatch):
    """A publish quiet period drops the charge FRAME, not the charge. It
    is the first frame of its pass, so nothing later would bring it back, and a
    socket resumed after the quiet period would show no ``(+N)`` for it."""
    quiet = {"on": False}
    monkeypatch.setattr(chat_turn_stream, "_redis_is_quiet", lambda: quiet["on"])
    token = await chat_turn_guard.acquire_turn(CONV)
    pub = TurnPublisher(CONV, token, origin="sock-a")
    pub.start()
    await _wait_until(lambda: pub.seq == 1)
    quiet["on"] = True
    await pub.on_event({"status": AGENTIC_ACTION_STATUS, "actions": 1, "period": "2026-09"})
    await _wait_until(lambda: pub.seq == 2)
    quiet["on"] = False
    await pub.on_event({"status": "thinking", "iteration": 1})

    state_key, *_ = journal_keys(CONV, token)
    await fanout_redis.wait_for_write(
        lambda: fanout_redis.hashes.get(state_key, {}).get("agentic_actions") == json.dumps({"2026-09": 1})
    )
    # The frame itself was never published: a live viewer cannot count it twice.
    assert [f for f in fanout_redis.frames(turn_channel(CONV)) if f.get("type") == AGENTIC_ACTION_FRAME] == []
    snapshot = await read_snapshot(CONV)
    assert snapshot is not None and snapshot.agentic_actions == {"2026-09": 1}
    await pub.aclose()


@pytest.mark.parametrize(
    ("raw", "totals"),
    [
        pytest.param(None, {}, id="absent"),
        pytest.param("not json", {}, id="malformed"),
        pytest.param("[1]", {}, id="not-an-object"),
        pytest.param('{"a": 0, "b": 2, "c": "x", "d": -1}', {"b": 2}, id="only-positive-counts"),
        pytest.param('{"a": true, "b": 2, "c": 1.5}', {"b": 2}, id="only-integers-not-booleans"),
    ],
)
def test_a_malformed_charge_total_replays_nothing(raw, totals):
    assert chat_turn_stream._read_agentic_actions(raw) == totals


@pytest.mark.parametrize(
    ("actions", "charged"),
    [(1, 1), (3, 3), (True, 0), (False, 0), (0, 0), (-2, 0), (1.0, 0), ("2", 0), (None, 0)],
)
def test_a_charge_frame_counts_only_real_integers(actions, charged):
    frame = {"type": AGENTIC_ACTION_FRAME, "actions": actions}
    assert chat_turn_stream._charged_actions(frame) == charged


def test_resume_frames_announce_no_charge_for_a_turn_that_had_none():
    frames = resume_frames(TurnSnapshot(token="tok", statuses=[{"type": "status", "status": "thinking"}]))
    assert all(f.get("type") != AGENTIC_ACTION_FRAME for f in frames)


def test_resume_frames_count_down_what_is_left_of_a_pause():
    """A page reloaded mid-pause shows the seconds left, not the whole pause again (#10910)."""
    journaled = {
        "type": "status",
        "status": "wait_start",
        "seconds": 60.0,
        "reason": "The enrichment is running",
        chat_turn_stream.JOURNALED_AT_KEY: 1000.0,
    }
    frames = resume_frames(TurnSnapshot(token="tok", statuses=[journaled]), now=1042.4)
    assert frames[1] == {
        "type": "status",
        "status": "wait_start",
        "seconds": 17.6,
        "reason": "The enrichment is running",
    }
    # A pause already over replays as over, never as a negative countdown.
    late = resume_frames(TurnSnapshot(token="tok", statuses=[journaled]), now=1200.0)
    assert late[1]["seconds"] == 0.0
    # A status journaled before the stamp existed replays as journaled.
    legacy = {"type": "status", "status": "wait_start", "seconds": 60.0}
    assert resume_frames(TurnSnapshot(token="tok", statuses=[legacy]), now=1042.4)[1] == legacy


_PROPOSAL = {"tool_call_id": "toolu_1", "tool_name": "store_memory", "arguments": {"content": "x"}}


def test_resume_frames_end_with_the_prompt_a_paused_turn_waits_on():
    """A socket attaching mid-pause must be asked, not shown "Thinking"."""
    frames = resume_frames(TurnSnapshot(token="tok", answer="so far", pending_approvals=[_PROPOSAL]))
    assert frames[-1] == {"type": "approval_required", "proposals": [_PROPOSAL]}
    assert all(f["type"] != "approval_required" for f in resume_frames(TurnSnapshot(token="tok")))


async def test_read_snapshot_carries_the_proposals_a_paused_turn_waits_on(fanout_redis):
    token = await chat_turn_guard.acquire_turn(CONV)
    await chat_turn_guard.set_pending_approvals(CONV, [_PROPOSAL])
    snapshot = await read_snapshot(CONV)
    assert snapshot is not None and snapshot.token == token
    assert snapshot.pending_approvals == [_PROPOSAL]

    await chat_turn_guard.clear_pending_approvals(CONV)
    assert (await read_snapshot(CONV)).pending_approvals == []


async def test_an_unreadable_journal_still_resumes_into_the_prompt(fanout_redis):
    """The client's own restore stands down on a resume, so the prompt must not
    depend on the journal pipeline succeeding."""
    await chat_turn_guard.acquire_turn(CONV)
    await chat_turn_guard.set_pending_approvals(CONV, [_PROPOSAL])
    fanout_redis.fail_pipelines = True
    assert (await read_snapshot(CONV)).pending_approvals == [_PROPOSAL]


# ── Relay ─────────────────────────────────────────────────────────────────


async def test_relay_resumes_into_the_turn_then_forwards_live_frames(fanout_redis):
    token = await chat_turn_guard.acquire_turn(CONV)
    owner = TurnPublisher(CONV, token, origin="owner")
    owner.start()
    await owner.on_event({"status": "tool_start", "tools": ["search"]})
    await owner.on_event({"status": "thinking_text", "content": "so far"})
    owner.offer({"type": "stream", "content": "partial"})
    await fanout_redis.wait_for_write(lambda: fanout_redis.published)

    sink = _FrameSink()
    sent = sink.frames

    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=sink.send))
    try:
        await sink.wait_until(lambda: any(f["type"] == "stream" for f in sent))
        assert sent[0]["type"] == "thinking"
        assert sent[0]["resumed"] is True and sent[0]["relayed"] is True
        assert sent[0]["elapsed_s"] >= 0
        assert _types(sent[1:]) == [
            ("status", "tool_start"),
            ("status", "thinking_text"),
            ("stream", None),
        ]
        assert sent[2]["content"] == "so far" and sent[3]["content"] == "partial"
        assert all(f["relayed"] is True for f in sent)
        replayed = len(sent)

        # A live batch from the owner: forwarded with the transport keys
        # stripped and the ``relayed`` flag set.
        owner.offer({"type": "stream", "content": " more"})
        await sink.wait_until(lambda: len(sent) > replayed)
        assert sent[-1] == {
            "type": "stream",
            "content": " more",
            "turn_id": chat_turn_stream.public_turn_id(token),
            "relayed": True,
        }

        # A frame of the replayed turn the snapshot already covered (published
        # between the subscription and the snapshot read) is dropped.
        seen = len(sent)
        await fanout_redis.publish(
            turn_channel(CONV),
            json.dumps([{"type": "stream", "content": "dup", "origin": "owner", "turn": token, "seq": 1}]),
        )
        # The viewer's own frames never come back to it.
        chat_turn_stream.publish_live(
            CONV, {"type": "user_message", "message": {"id": "mine"}}, origin="viewer"
        )
        await asyncio.sleep(0.05)
        assert len(sent) == seen
        # Another socket's user message does.
        chat_turn_stream.publish_live(
            CONV, {"type": "user_message", "message": {"id": "theirs"}}, origin="owner"
        )
        await sink.wait_until(lambda: len(sent) > seen)
        assert sent[-1] == {
            "type": "user_message",
            "message": {"id": "theirs"},
            "relayed": True,
        }
    finally:
        relay.cancel()
        with pytest.raises(asyncio.CancelledError):
            await relay
        await owner.aclose()


async def test_relay_hands_the_approval_prompt_to_every_other_socket(fanout_redis):
    """The prompt used to reach only the socket whose turn paused, so a
    reload or a second device saw a turn "thinking" that was waiting on it."""
    token = await chat_turn_guard.acquire_turn(CONV)
    owner = TurnPublisher(CONV, token, origin="owner")
    owner.start()
    await chat_turn_guard.set_pending_approvals(CONV, [_PROPOSAL])
    owner.offer({"type": "approval_required", "proposals": [_PROPOSAL]})
    await fanout_redis.wait_for_write(lambda: any("approval_required" in p for _, p in fanout_redis.published))

    sink = _FrameSink()
    sent = sink.frames

    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=sink.send))
    try:
        # Attached mid-pause: the resume ends on the prompt.
        await sink.wait_until(lambda: any(f["type"] == "approval_required" for f in sent))
        assert sent[0]["resumed"] is True
        assert sent[-1] == {
            "type": "approval_required",
            "proposals": [_PROPOSAL],
            "turn_id": chat_turn_stream.public_turn_id(token),
            "relayed": True,
        }

        # Answered elsewhere: the end of the pause reaches it too.
        await chat_turn_guard.clear_pending_approvals(CONV)
        owner.offer({"type": "approval_resolved"})
        await sink.wait_until(lambda: sent[-1]["type"] == "approval_resolved")

        # A later pause of the same turn reaches it live, in the order
        # production writes it: the record, then the frame.
        second = {**_PROPOSAL, "tool_call_id": "toolu_2"}
        await chat_turn_guard.set_pending_approvals(CONV, [second])
        owner.offer({"type": "approval_required", "proposals": [second]})
        await sink.wait_until(lambda: sent[-1]["type"] == "approval_required")
        assert sent[-1]["proposals"] == [second] and sent[-1]["relayed"] is True
    finally:
        relay.cancel()
        with pytest.raises(asyncio.CancelledError):
            await relay
        await owner.aclose()


async def test_a_new_conversation_title_reaches_the_other_sockets_and_no_turn(fanout_redis):
    """A new conversation's AI title is written after its first turn's
    ``done``. The owner's other sockets (each runs the relay)
    refetched the list at ``done``, when
    the provisional title was still stored, so the title reaches them live -
    never echoed to the socket that produced it, and never carried by a turn
    publisher (a turn the same socket started meanwhile), which would journal
    and publish it a second time."""
    sink = _FrameSink()
    sent = sink.frames

    title = {"type": "conversation_title", "conversation_id": CONV, "title": "Greeting"}
    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=sink.send))
    try:
        await _wait_until(lambda: chat_turn_stream._hub()._ready)
        chat_turn_stream.publish_live(CONV, title, origin="viewer")
        chat_turn_stream.publish_live(CONV, title, origin="owner")
        await sink.wait_until(lambda: sent)
        await asyncio.sleep(0.05)
        assert sent == [{**title, "relayed": True}]
    finally:
        relay.cancel()
        with pytest.raises(asyncio.CancelledError):
            await relay

    published_live = len(fanout_redis.published)
    pub = TurnPublisher(CONV, "tok", origin="owner")
    pub.start()
    await fanout_redis.wait_for_write(lambda: len(fanout_redis.published) > published_live)  # the ack
    before = len(fanout_redis.published)
    pub.offer(title)
    await asyncio.sleep(0.03)
    assert len(fanout_redis.published) == before
    await pub.aclose()


async def test_relay_drops_a_misordered_producers_late_terminal_defence_in_depth(fanout_redis):
    """Defence in depth, not the current lifecycle: every producer today
    publishes its terminal BEFORE releasing the lock (the REST path through
    ``_rest_end_turn``, pinned by
    ``test_rest_and_parent_wake_turns_publish_their_progress``), so the next
    turn's opening ack cannot precede a previous turn's ``done`` on the
    channel. This drives the LEGACY misordered producer by hand - lock
    released, next turn opened, then the old terminal published - because
    forwarded unqualified, that ``done`` would close the newer turn on every
    viewer; the relay follows one turn at a time and drops the late frame."""
    sink = _FrameSink()
    sent = sink.frames

    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=sink.send))
    # Attached and resynced with nothing in flight.
    await _wait_until(lambda: chat_turn_stream._hub()._ready)
    await asyncio.sleep(0.05)
    try:
        first_token = await chat_turn_guard.acquire_turn(CONV)
        first = TurnPublisher(CONV, first_token, origin="misordered")
        first.start()
        await sink.wait_until(lambda: any(f["type"] == "thinking" for f in sent))
        # The misordered producer releases the lock, and the next turn opens...
        assert await chat_turn_guard.release_turn(CONV, first_token)
        second_token = await chat_turn_guard.acquire_turn(CONV)
        second = TurnPublisher(CONV, second_token, origin="ws-b")
        second.start()
        await sink.wait_until(lambda: sum(1 for f in sent if f["type"] == "thinking") == 2)
        # ...before the first turn's terminal frame is published: dropped.
        first.offer({"type": "done", "content": "late answer"})
        await first.aclose()
        await asyncio.sleep(0.05)
        assert not any(f["type"] == "done" for f in sent)
        # The turn the socket now follows still reaches it, terminal included.
        second.offer({"type": "stream", "content": "x"})
        second.offer({"type": "done", "content": "x"})
        await second.aclose()
        await sink.wait_until(lambda: any(f["type"] == "done" for f in sent))
        assert _types(sent[-2:]) == [("stream", None), ("done", None)]
        assert sent[-1]["content"] == "x"
        # Only a TERMINAL frame of another turn is stale: a non-terminal frame
        # of a turn the socket has not seen (its opening ack swallowed by a
        # quiet period) moves the relay onto that turn, and that turn's own
        # terminal then passes while a late one of the previous turn does not.
        seen = len(sent)

        async def _raw(frame: dict) -> None:
            await fanout_redis.publish(turn_channel(CONV), json.dumps([frame]))

        await _raw(
            {"type": "status", "status": "tool_start", "tools": ["x"], "origin": "p3", "turn": "third", "seq": 1}
        )
        await sink.wait_until(lambda: len(sent) > seen)
        assert _types(sent[-1:]) == [("status", "tool_start")]
        await _raw({"type": "done", "content": "second again", "origin": "ws-b", "turn": second_token, "seq": 99})
        await _raw({"type": "done", "content": "third", "origin": "p3", "turn": "third", "seq": 2})
        await sink.wait_until(lambda: sent[-1].get("content") == "third")
        assert not any(f.get("content") == "second again" for f in sent)
    finally:
        relay.cancel()
        with pytest.raises(asyncio.CancelledError):
            await relay


@pytest.mark.parametrize("snapshot_read", ["empty", "tombstone", "fails"])
async def test_relay_follows_nothing_after_an_empty_resync_so_a_newer_terminal_lands(
    fanout_redis, monkeypatch, snapshot_read
):
    """The race the one-turn guard must not turn into a loss: the socket
    followed T1 to its ``done``; the hub reconnects (or the listener
    overflowed) and a whole newer turn T2 starts AND ends between the
    subscription confirmation and the snapshot read - so the resync finds
    no turn in flight (no marker, or T2's ``ended`` tombstone) while T2's
    ``done`` is already queued behind the RESYNC. With ``following`` still
    T1, that terminal read as the stale frame of another turn and was
    dropped: the viewer got neither T2's answer nor a safety poll. An empty
    (or unreadable) resync now follows nothing, so the queued terminal names
    the turn and is delivered. Driven through a fake hub so the queue order
    is exact."""
    queue: asyncio.Queue = asyncio.Queue()

    class _ScriptedHub:
        async def attach(self, _conversation_id: str) -> asyncio.Queue:
            return queue

        async def detach(self, _conversation_id: str, _queue: asyncio.Queue) -> None:
            return None

    monkeypatch.setattr(chat_turn_stream, "_hub", lambda: _ScriptedHub())
    t2 = "t2"
    if snapshot_read == "fails":

        async def _boom(_conversation_id: str):
            raise ConnectionError("redis blip")

        monkeypatch.setattr(chat_turn_stream, "read_snapshot", _boom)
    elif snapshot_read == "tombstone":
        # T2 published its terminal and closed (tombstone) but its owner has
        # not released the marker yet: exactly the window Copilot named.
        t2 = await chat_turn_guard.acquire_turn(CONV)
        second = TurnPublisher(CONV, t2, origin="b")
        second.start()
        second.offer({"type": "done", "content": "second answer", "message_id": "m-2"})
        await second.aclose()
        assert await chat_turn_guard.is_turn_active(CONV)
        assert await read_snapshot(CONV) is None
    # "empty": no marker in the fake Redis, so read_snapshot() is None -
    # what a socket reads once T2's owner closed and released.

    def _batch(*frames: dict) -> str:
        return json.dumps(list(frames))

    queue.put_nowait(
        _batch(
            {"type": "thinking", "origin": "a", "turn": "t1", "seq": 1},
            {"type": "done", "content": "first answer", "origin": "a", "turn": "t1", "seq": 2},
        )
    )
    queue.put_nowait(RESYNC)
    queue.put_nowait(
        _batch(
            {"type": "done", "content": "second answer", "message_id": "m-2", "origin": "b", "turn": t2, "seq": 7}
        )
    )
    sink = _FrameSink()
    sent = sink.frames

    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=sink.send))
    try:
        await sink.wait_until(lambda: len(sent) >= 3)
        assert _types(sent) == [("thinking", None), ("done", None), ("done", None)]
        assert sent[1]["content"] == "first answer"
        assert sent[2] == {
            "type": "done",
            "content": "second answer",
            "message_id": "m-2",
            "turn_id": chat_turn_stream.public_turn_id(t2),
            "relayed": True,
        }
        # The guard itself still stands once a turn is followed again: a
        # terminal of yet another turn is stale while T3 is the one followed.
        # (T2 cannot be the device: a frame of a turn the socket saw end
        # reopens nothing.)
        queue.put_nowait(
            _batch(
                {"type": "stream", "content": "x", "origin": "c", "turn": "t3", "seq": 1},
                {"type": "done", "content": "ghost", "origin": "z", "turn": "t0", "seq": 3},
            )
        )
        await sink.wait_until(lambda: len(sent) >= 4)
        await asyncio.sleep(0.02)
        assert _types(sent[3:]) == [("stream", None)]
    finally:
        relay.cancel()
        with pytest.raises(asyncio.CancelledError):
            await relay


async def test_relay_follows_nothing_after_forwarding_the_followed_turns_terminal(
    fanout_redis: _FakeStreamRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The socket followed T1 to its ``done``. A publish quiet period (a
    Redis failure trips a 30 s cooldown during which frames are dropped)
    swallows T2's opening ack and every non-terminal frame; T2's ``done`` is
    published once the cooldown lifts. Still bound to T1, the guard read
    that terminal as another turn's and dropped it - and since the socket
    never saw T2 start, it never started a poll either. Once the followed
    turn's terminal has been forwarded the relay follows nothing, and the
    next frame - T2's ``done`` here - names the turn and lands."""
    queue: asyncio.Queue = asyncio.Queue()

    class _ScriptedHub:
        async def attach(self, _conversation_id: str) -> asyncio.Queue:
            return queue

        async def detach(self, _conversation_id: str, _queue: asyncio.Queue) -> None:
            return None

    monkeypatch.setattr(chat_turn_stream, "_hub", lambda: _ScriptedHub())

    def _batch(*frames: dict) -> str:
        return json.dumps(list(frames))

    queue.put_nowait(
        _batch(
            {"type": "thinking", "origin": "a", "turn": "t1", "seq": 1},
            {"type": "done", "content": "first answer", "origin": "a", "turn": "t1", "seq": 2},
        )
    )
    queue.put_nowait(
        _batch({"type": "done", "content": "second answer", "origin": "b", "turn": "t2", "seq": 9})
    )
    sink = _FrameSink()
    sent = sink.frames

    relay = asyncio.create_task(relay_turn_frames(CONV, origin="viewer", send=sink.send))
    try:
        await sink.wait_until(lambda: len(sent) >= 3)
        assert [f.get("content") for f in sent] == [None, "first answer", "second answer"]
    finally:
        relay.cancel()
        with pytest.raises(asyncio.CancelledError):
            await relay


async def test_relay_hub_shares_one_subscription_and_resyncs_on_reconnect(fanout_redis):
    token = await chat_turn_guard.acquire_turn(CONV)
    owner = TurnPublisher(CONV, token, origin="owner")
    owner.start()
    await owner.on_event({"status": "tool_start", "tools": ["search"]})
    await fanout_redis.wait_for_write(lambda: fanout_redis.published)

    sink_a, sink_b = _FrameSink(), _FrameSink()
    sent_a, sent_b = sink_a.frames, sink_b.frames

    relay_a = asyncio.create_task(relay_turn_frames(CONV, origin="a", send=sink_a.send))
    await sink_a.wait_until(lambda: len(sent_a) >= 2)
    relay_b = asyncio.create_task(relay_turn_frames(CONV, origin="b", send=sink_b.send))
    await sink_b.wait_until(lambda: len(sent_b) >= 2)
    try:
        channel = turn_channel(CONV)
        # One Redis subscription for the whole process, two listeners.
        assert len(fanout_redis.subscribers[channel]) == 1
        assert sent_b[0]["resumed"] is True

        owner.offer({"type": "stream", "content": "live"})
        await sink_a.wait_until(lambda: sent_a and sent_a[-1].get("content") == "live")
        await sink_b.wait_until(lambda: sent_b and sent_b[-1].get("content") == "live")

        # The subscription drops: the hub reconnects, re-subscribes, and every
        # listener re-reads the journal (a fresh ``resumed`` ack) so nothing
        # published during the gap leaves a hole in its text buffers.
        (subscriber,) = fanout_redis.subscribers[channel]
        before_a, before_b = len(sent_a), len(sent_b)
        subscriber.fail_next_read = True
        # The replay is complete (ack, the step, the answer so far) before
        # the assertions read it: wait for its last frame, not the ack alone.
        await sink_a.wait_until(
            lambda: any(f.get("resumed") for f in sent_a[before_a:]) and sent_a[-1].get("content") == "live"
        )
        await sink_b.wait_until(lambda: any(f.get("resumed") for f in sent_b[before_b:]))
        # The replay is complete: ack, the step, the answer so far.
        assert _types(sent_a[before_a:]) == [
            ("thinking", None),
            ("status", "tool_start"),
            ("stream", None),
        ]
        assert sent_a[-1]["content"] == "live"
        assert len(fanout_redis.subscribers[channel]) == 1
        assert subscriber not in fanout_redis.subscribers[channel]
    finally:
        for relay in (relay_a, relay_b):
            relay.cancel()
            with pytest.raises(asyncio.CancelledError):
                await relay
        await owner.aclose()
    # The last listener gone: the hub closed its subscription.
    await asyncio.sleep(0.02)


async def test_relay_hub_reconnects_when_a_live_subscribe_fails(
    fanout_redis: _FakeStreamRedis,
) -> None:
    """A SUBSCRIBE that fails on a connection that stays alive (a timeout,
    a transient error) used to leave the channel unsubscribed for as long as
    the connection lived: the reader had no reason to reconnect, so the
    listener that attached got neither a confirmation resync nor a live
    frame. ``_apply`` now asks the reader to tear the connection down; the
    reconnect subscribes every listener channel again."""
    sink_a, sink_b = _FrameSink(), _FrameSink()
    sent_b = sink_b.frames

    other = "cccccccc-dddd-eeee-ffff-000000000000"
    relay_a = asyncio.create_task(relay_turn_frames(CONV, origin="a", send=sink_a.send))
    hub = chat_turn_stream._hub()
    await _wait_until(lambda: hub._ready)
    try:
        # The live subscribe for the second channel fails once.
        hub._pubsub.fail_next_subscribe = True
        relay_b = asyncio.create_task(relay_turn_frames(other, origin="b", send=sink_b.send))
        try:
            # Reconnected: both channels are on the new connection.
            await _wait_until(
                lambda: turn_channel(other) in fanout_redis.subscribers
                and fanout_redis.subscribers[turn_channel(other)]
                and fanout_redis.subscribers[turn_channel(CONV)],
                timeout=6.0,
            )
            (sub_other,) = fanout_redis.subscribers[turn_channel(other)]
            (sub_conv,) = fanout_redis.subscribers[turn_channel(CONV)]
            assert sub_other is sub_conv
            token = await chat_turn_guard.acquire_turn(other)
            pub = TurnPublisher(other, token, origin="owner")
            pub.start()
            await sink_b.wait_until(lambda: any(f["type"] == "thinking" for f in sent_b))
            await pub.aclose()
        finally:
            relay_b.cancel()
            with pytest.raises(asyncio.CancelledError):
                await relay_b
    finally:
        relay_a.cancel()
        with pytest.raises(asyncio.CancelledError):
            await relay_a
    # The last detach retires the reader without waiting for its close.
    await _wait_until(lambda: not fanout_redis.subscribers.get(turn_channel(CONV)))


async def test_relay_hub_starts_the_next_generation_after_the_last_one_retired(
    fanout_redis,
):
    """The last ``detach`` cancels the reader without waiting for it (a
    socket tearing down never waits on Redis). A listener attaching right
    after must not race that teardown: the retiring reader's ``finally``
    closes the hub's CURRENT connection and resets its readiness, which
    would be the replacement's if one had connected in between. ``attach``
    waits for the retiring generation instead."""
    token = await chat_turn_guard.acquire_turn(CONV)
    owner = TurnPublisher(CONV, token, origin="owner")
    owner.start()
    await owner.on_event({"status": "tool_start", "tools": ["search"]})
    await fanout_redis.wait_for_write(lambda: fanout_redis.published)

    hub = chat_turn_stream._hub()
    queue_a = await hub.attach(CONV)
    first_task = hub._task
    assert first_task is not None
    await _wait_until(lambda: hub._ready)
    first_conn = hub._pubsub
    assert await asyncio.wait_for(queue_a.get(), 1.0) is RESYNC

    # The last listener leaves and a new one attaches in the same breath.
    await hub.detach(CONV, queue_a)
    assert hub._task is None and hub._retiring is first_task
    assert not first_task.done(), "the cancel is delivered later - that is the race"
    queue_b = await hub.attach(CONV)
    try:
        assert first_task.done(), "attach waited for the retiring generation"
        assert hub._retiring is None
        assert hub._task is not None and hub._task is not first_task
        # The new generation serves the listener on a connection of its own:
        # resynced on subscription, then fed the live frames.
        assert await asyncio.wait_for(queue_b.get(), 1.0) is RESYNC
        assert hub._ready and hub._pubsub is not None and hub._pubsub is not first_conn
        owner.offer({"type": "stream", "content": "live"})
        batch = chat_turn_stream.decode_batch(await asyncio.wait_for(queue_b.get(), 1.0))
        assert batch[-1]["content"] == "live"
        assert len(fanout_redis.subscribers[turn_channel(CONV)]) == 1
    finally:
        await hub.detach(CONV, queue_b)
        await owner.aclose()


async def test_relay_hub_applies_the_desired_subscription_whatever_the_order(fanout_redis):
    """The hub keeps serving another conversation, so the reader stays up;
    this channel's last listener leaves (its unsubscribe is scheduled, not
    yet run) and a replacement attaches in the same breath. The stale
    unsubscribe must not silence the replacement: every change applies the
    desired state as it stands when it runs, serialized on the wire, and
    the replacement is announced even though no confirmation will come."""
    other = "ffffffff-0000-0000-0000-000000000001"
    hub = chat_turn_stream._hub()
    keep = await hub.attach(other)
    await _wait_until(lambda: hub._ready)
    assert await asyncio.wait_for(keep.get(), 1.0) is RESYNC
    channel = turn_channel(CONV)
    first = await hub.attach(CONV)
    assert await asyncio.wait_for(first.get(), 1.0) is RESYNC
    assert len(fanout_redis.subscribers[channel]) == 1

    await hub.detach(CONV, first)
    second = await hub.attach(CONV)
    try:
        await asyncio.sleep(0.05)  # the scheduled unsubscribe ran - as a no-op
        assert not hub._apply_tasks
        assert len(fanout_redis.subscribers[channel]) == 1, "still subscribed"
        assert channel in hub._wire
        assert await asyncio.wait_for(second.get(), 1.0) is RESYNC
        token = await chat_turn_guard.acquire_turn(CONV)
        owner = TurnPublisher(CONV, token, origin="owner")
        owner.start()
        batch = chat_turn_stream.decode_batch(await asyncio.wait_for(second.get(), 1.0))
        assert batch[0]["type"] == "thinking"
        await owner.aclose()
    finally:
        await hub.detach(CONV, second)
    # The last listener really gone: the channel is unsubscribed, the other
    # conversation's is not.
    await _wait_until(lambda: not fanout_redis.subscribers.get(channel))
    assert channel not in hub._wire
    assert len(fanout_redis.subscribers[turn_channel(other)]) == 1
    await hub.detach(other, keep)


async def test_relay_hub_unsubscribes_a_channel_whose_listeners_left_mid_connect(
    fanout_redis, monkeypatch
):
    """``detach`` only applies a change once the connection is ready; a
    channel whose last listener leaves while the connect loop is subscribing
    it must not stay subscribed with nobody to read it until the next
    reconnect, and its late confirmation must not leave a confirmation
    event behind for a channel nobody holds."""
    gate = asyncio.Event()

    class _GatedPubSub(_FakePubSub):
        async def subscribe(self, *channels: str) -> None:
            await gate.wait()
            await super().subscribe(*channels)

    class _GatedClient(_FakePubSubClient):
        def pubsub(self) -> _FakePubSub:
            return _GatedPubSub(self._redis)

    async def _pubsub_client():
        return _GatedClient(fanout_redis)

    monkeypatch.setattr(chat_turn_stream, "_pubsub_client", _pubsub_client)
    hub = chat_turn_stream._hub()
    other = "bbbbbbbb-cccc-dddd-eeee-ffffffffffff"

    leaving = await hub.attach(CONV)
    staying = await hub.attach(other)
    await asyncio.sleep(0.02)  # the connect loop is blocked inside subscribe()
    assert not hub._ready
    await hub.detach(CONV, leaving)  # not ready: nothing to unsubscribe yet
    gate.set()
    try:
        await _wait_until(lambda: hub._ready)
        await asyncio.sleep(0.02)
        assert not fanout_redis.subscribers.get(turn_channel(CONV))
        assert len(fanout_redis.subscribers[turn_channel(other)]) == 1
        assert turn_channel(CONV) not in hub._confirmed
        assert staying.get_nowait() is RESYNC
    finally:
        await hub.detach(other, staying)
        await asyncio.sleep(0.05)


async def test_relay_listener_that_falls_behind_is_resynced(fanout_redis, monkeypatch):
    monkeypatch.setattr(chat_turn_stream, "LISTENER_QUEUE_SIZE", 2)
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    chat_turn_stream._offer_to_listener(queue, "one")
    chat_turn_stream._offer_to_listener(queue, "two")
    chat_turn_stream._offer_to_listener(queue, "three")
    items = []
    while not queue.empty():
        items.append(queue.get_nowait())
    assert items == [RESYNC]


# ── Send connection ───────────────────────────────────────────────────────


async def test_turn_frames_are_fanned_out_and_done_precedes_the_release(
    monkeypatch, ws_env
):
    conv_id = str(ws_env.conv.id)

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"]({"status": "tool_start", "tools": ["search"]})
        await kwargs["on_event"]({"status": "thinking_text", "content": "hmm"})
        await kwargs["on_token"]("Hel")
        await kwargs["on_token"]("lo")
        return ("Hello", None)

    _patch_general_chat(monkeypatch, _run)
    conn = await _send(ws_env, {"content": "build the report"})

    frames = ws_env.redis.frames(turn_channel(conv_id))
    kinds = _types(frames)
    # The user message first (published on commit, before the lock), then
    # the turn: ack, setup phases, the loop's events, the token stream, the
    # persisting phase, the terminal frame.
    assert kinds[0] == ("user_message", None)
    assert frames[0]["message"]["content"] == "build the report"
    assert kinds[1] == ("thinking", None)
    assert ("status", "preparing") in kinds
    assert ("status", "tool_start") in kinds
    assert ("status", "thinking_text") in kinds
    assert ("status", "persisting") in kinds
    assert [f["content"] for f in frames if f["type"] == "stream"] == ["Hello"]
    assert kinds[-1] == ("done", None)
    assert frames[-1]["content"] == "Hello" and frames[-1]["message_id"]
    # One origin for everything this connection produced.
    assert len({f["origin"] for f in frames}) == 1
    turn_frames = [f for f in frames if f["type"] != "user_message"]
    assert len({f["turn"] for f in turn_frames}) == 1

    # The terminal frame reaches the channel BEFORE the marker is released.
    publish_done_at = next(
        i
        for i, entry in enumerate(ws_env.timeline)
        if entry[0] == "publish" and any(f["type"] == "done" for f in json.loads(entry[2]))
    )
    release_at = next(i for i, entry in enumerate(ws_env.timeline) if entry == ("release",))
    assert publish_done_at < release_at
    # The journal is gone with the turn; only the ``ended`` tombstone stays.
    token = turn_frames[0]["turn"]
    state_key, *content_keys = journal_keys(conv_id, token)
    assert not any(
        k in ws_env.redis.kv or k in ws_env.redis.lists or k in ws_env.redis.hashes
        for k in content_keys
    )
    assert ws_env.redis.hashes[state_key]["ended"] == "1"
    # The sender's stream got every frame exactly once (never its own echo).
    assert [f["type"] for f in conn.sent].count("thinking") == 1
    assert [f["type"] for f in conn.sent].count("done") == 1
    assert not any(f.get("relayed") for f in conn.sent)


async def test_pause_and_its_end_go_out_on_the_channel(monkeypatch, ws_env):
    """#173: every other tab is shown the prompt and told when it is gone,
    and the turn gets the answer given elsewhere."""
    conv_id = str(ws_env.conv.id)
    decided: dict = {}

    def _published() -> list[str]:
        return [f["type"] for f in ws_env.redis.frames(turn_channel(conv_id))]

    async def _answer_from_elsewhere():
        await ws_env.redis.wait_for_write(lambda: "approval_required" in _published())
        await chat_turn_guard.push_approval_decision(
            conv_id, {"toolu_1": {"decision": "approve", "rejection_reason": None}}
        )

    async def _run(_db, _uid, _history, _content, **kwargs):
        decided.update(await kwargs["request_approval"]([_PROPOSAL]))
        return ("Stored", None)

    _patch_general_chat(monkeypatch, _run)
    answer = asyncio.create_task(_answer_from_elsewhere())
    # Bounded: unpublished, the prompt is never answered and the turn waits on.
    await asyncio.wait_for(
        _send(ws_env, {"content": "remember this", "supports_tool_approval": True}), timeout=10
    )
    await answer

    kinds = _published()
    assert kinds.index("approval_required") < kinds.index("approval_resolved") < kinds.index("done")
    assert decided["toolu_1"]["decision"] == "approve"
    assert await chat_turn_guard.get_pending_approvals(conv_id) is None


async def test_a_video_studio_send_keeps_the_scene_on_screen_with_the_user_message(monkeypatch, ws_env):
    """The Video Studio rail's send names the scene on screen (`studio_scene`):
    the stored user message keeps it, as the REST path does, so the agent's
    `get_video` feedback reads the requests made on that scene."""
    import app.services.video_studio.studio as studio_module

    ws_env.conv.video_project_id = uuid.uuid4()

    async def _no_studio(*_args, **_kwargs):
        return None

    monkeypatch.setattr(studio_module, "load_video_studio_context", _no_studio)
    kept: list[str | None] = []
    store = chat_module.store_message

    async def _store(db, conv_id, role, content, **kwargs):
        if role == "user":
            kept.append(kwargs.get("metadata_json"))
        return await store(db, conv_id, role, content, **kwargs)

    monkeypatch.setattr(chat_module, "store_message", _store)

    async def _run(_db, _uid, _history, _content, **_kwargs):
        return ("Done", None)

    _patch_general_chat(monkeypatch, _run)
    conn = _FakeConnection(str(ws_env.conv.id), {"content": "tighten the intro", "studio_scene": "s3"}, [])
    await asyncio.wait_for(_run_connection(conn, ws_env.user_id), timeout=5.0)
    assert kept and kept[0], "the user message was stored without its metadata"
    assert json.loads(kept[0]).get("studio_scene") == "s3"


async def test_another_tab_of_the_owner_follows_the_turn_live(monkeypatch, ws_env):
    """Scenario 2 of #3712: a second device / an impersonating admin has the
    conversation open when the first tab sends — it sees the user message,
    the working state, the stream and the answer as they happen."""
    gate = asyncio.Event()

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"]({"status": "tool_start", "tools": ["search"]})
        await kwargs["on_token"]("Hello")
        await gate.wait()
        return ("Hello", None)

    _patch_general_chat(monkeypatch, _run)
    viewer, seen = await _attached_viewer(ws_env)
    conn, owner = _start(ws_env)
    try:
        await _wait_until(lambda: any(f["type"] == "stream" for f in seen), timeout=4.0)
        kinds = _types(seen)
        assert kinds[0] == ("user_message", None)
        assert seen[0]["message"]["content"] == "build the report"
        assert kinds[1] == ("thinking", None)
        assert seen[1]["relayed"] is True and "resumed" not in seen[1]
        assert ("status", "tool_start") in kinds
        assert seen[-1]["content"] == "Hello"
        assert all(f["relayed"] is True for f in seen)

        gate.set()
        await owner
        await _wait_until(lambda: any(f["type"] == "done" for f in seen), timeout=4.0)
        done = next(f for f in seen if f["type"] == "done")
        assert done["content"] == "Hello" and done["message_id"] and done["relayed"] is True
        # The sender's own stream is untouched by the fan-out.
        assert [f["type"] for f in conn.sent].count("thinking") == 1
        assert not any(f.get("relayed") for f in conn.sent)
    finally:
        gate.set()
        await _stop(viewer)


async def test_the_charge_reaches_every_tab_as_a_frame_of_its_own(monkeypatch, ws_env):
    """#2545: the charge a runner announces through ``on_event`` reaches the
    sender's stream and every viewer as an ``agentic_action`` frame, never as
    a ``status`` that a client released before it would put on its label."""

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"]({"status": AGENTIC_ACTION_STATUS, "actions": 1, "period": "2026-09"})
        await kwargs["on_token"]("Hello")
        return ("Hello", None)

    _patch_general_chat(monkeypatch, _run)
    viewer, seen = await _attached_viewer(ws_env)
    try:
        conn = await _send(ws_env, {"content": "build the report"}, timeline=[])
        await _wait_until(lambda: any(f["type"] == "done" for f in seen), timeout=4.0)

        def _charges(frames):
            return [
                (f["actions"], f["period"], f.get("relayed"))
                for f in frames
                if f["type"] == AGENTIC_ACTION_FRAME
            ]

        assert _charges(conn.sent) == [(1, "2026-09", None)]
        assert _charges(seen) == [(1, "2026-09", True)]
        for frames in (conn.sent, seen):
            assert not [f for f in frames if f.get("status") == AGENTIC_ACTION_STATUS]
    finally:
        await _stop(viewer)


async def test_turn_started_stays_on_the_stream_that_sent(monkeypatch, ws_env):
    """#4403: ``turn_started`` answers ONE send. The conversation's other tabs
    learn of the turn from its relayed opening ack; the frame is neither
    published nor journaled."""
    conv_id = str(ws_env.conv.id)

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_token"]("Hello")
        return ("Hello", None)

    _patch_general_chat(monkeypatch, _run)
    viewer, seen = await _attached_viewer(ws_env)
    try:
        conn = await _send(ws_env, {"content": "build the report"}, timeline=[])
        await _wait_until(lambda: any(f["type"] == "done" for f in seen), timeout=4.0)
        assert [f["type"] for f in conn.sent][:2] == ["thinking", "turn_started"]
        assert "turn_started" not in [f["type"] for f in seen]
        channel = ws_env.redis.frames(turn_channel(conv_id))
        assert "turn_started" not in [f["type"] for f in channel]
    finally:
        await _stop(viewer)


async def test_a_tab_attaching_mid_turn_is_resumed_into_it(monkeypatch, ws_env):
    """Scenario 1 of #3712: the page is reloaded while the turn runs. Its new
    events stream is resumed into the running turn — the steps, the reasoning
    and the answer streamed so far, with the elapsed counter anchored on the
    turn's real start — and then follows it to ``done``."""
    conv_id = str(ws_env.conv.id)
    gate = asyncio.Event()

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"]({"status": "thinking", "iteration": 1})
        await kwargs["on_event"]({"status": "thinking_text", "content": "Let me "})
        await kwargs["on_event"]({"status": "thinking_text", "content": "look."})
        await kwargs["on_event"]({"status": "tool_start", "tools": ["search"]})
        await kwargs["on_event"]({"status": "tool_done", "tool": "search", "success": True, "tool_output": "big" * 100})
        await kwargs["on_event"]({"status": "streaming"})
        await kwargs["on_token"]("Partial ")
        await kwargs["on_token"]("answer")
        await gate.wait()
        await kwargs["on_token"] (" - complete")
        return ("Partial answer - complete", None)

    _patch_general_chat(monkeypatch, _run)
    _conn, owner = _start(ws_env)
    # The turn is well under way (its partial answer is journaled)...
    await ws_env.redis.wait_for_write(
        lambda: any(f["type"] == "stream" for f in ws_env.redis.frames(turn_channel(conv_id)))
    )
    await asyncio.sleep(0.03)

    # ...when the reloaded page attaches.
    reloaded, seen = await _events(ws_env, client_id="reloaded")
    try:
        await _wait_until(lambda: any(f["type"] == "stream" for f in seen), timeout=4.0)
        ack = seen[0]
        assert ack["type"] == "thinking" and ack["resumed"] is True and ack["relayed"] is True
        assert ack["elapsed_s"] >= 0
        replayed = _types(seen)
        # The steps in order (the runner's own setup phases included), the
        # heavy tool output stripped, then ONE reasoning frame and ONE
        # stream frame holding everything so far.
        assert ("status", "preparing") in replayed
        assert replayed.index(("status", "thinking")) < replayed.index(("status", "tool_start"))
        assert replayed.index(("status", "tool_start")) < replayed.index(("status", "tool_done"))
        tool_done = next(f for f in seen if f.get("status") == "tool_done")
        assert "tool_output" not in tool_done
        texts = [f for f in seen if f.get("status") == "thinking_text"]
        assert [t["content"] for t in texts] == ["Let me look."]
        assert seen[-1] == {
            "type": "stream",
            "content": "Partial answer",
            "turn_id": ack["turn_id"],
            "relayed": True,
        }
        replayed_count = len(seen)

        # Then the turn continues live on the new stream, to its end.
        gate.set()
        await owner
        await _wait_until(lambda: any(f["type"] == "done" for f in seen), timeout=4.0)
        tail = seen[replayed_count:]
        assert [f["content"] for f in tail if f["type"] == "stream"] == [" - complete"]
        assert tail[-1]["type"] == "done" and tail[-1]["content"] == "Partial answer - complete"
        # Nothing came twice: the journal's frames were not re-forwarded live.
        assert [f.get("status") for f in seen].count("tool_start") == 1
    finally:
        gate.set()
        await _stop(reloaded)


async def test_steering_message_is_relayed_to_the_other_tabs(monkeypatch, ws_env):
    async def _run(_db, _uid, _history, _content, **kwargs):
        text = await asyncio.wait_for(kwargs["steering_queue"].get(), timeout=2)
        assert text == "actually, filter on subregion"
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    viewer, seen = await _attached_viewer(ws_env)
    conn, owner = _start(ws_env)
    try:
        assert await _wait_for_frame(conn, "turn_started") is not None
        assert (await _steer(ws_env, "actually, filter on subregion"))["status"] == "steered"
        await asyncio.wait_for(owner, 5)
        await _wait_until(lambda: any(f["type"] == "done" for f in seen), timeout=4.0)
        user_messages = [f for f in seen if f["type"] == "user_message"]
        assert [m["message"]["content"] for m in user_messages] == [
            "build the report",
            "actually, filter on subregion",
        ]
        assert all(m["message"]["role"] == "user" and m["message"]["id"] for m in user_messages)
    finally:
        await _stop(viewer)


def _stop_requests(monkeypatch) -> tuple[list[str], list[str]]:
    cancel_requests: list[str] = []
    cascades: list[str] = []

    async def _request_cancel(cid, **_kw):
        cancel_requests.append(cid)
        return True

    monkeypatch.setattr(chat_turn_guard, "request_turn_cancel", _request_cancel)
    monkeypatch.setattr(chat_module, "_spawn_cancel_cascade", lambda cid, *_a: cascades.append(cid))
    return cancel_requests, cascades


async def test_a_non_owner_can_neither_send_follow_nor_stop_the_turn(monkeypatch, ws_env):
    """The conversation is not found for anybody else: no send stream, no
    events stream, and no Stop, neither of the turn nor of its sub-agent tasks."""
    conv_id = str(ws_env.conv.id)
    cancel_requests, cascades = _stop_requests(monkeypatch)
    stranger = SimpleNamespace(id=uuid.uuid4(), web_session=SimpleNamespace())

    def _nothing_found():
        return _FakeSession(None, [])

    attempts = (
        lambda: chat_web_stream.send_web_chat_message(
            chat_web_stream.WebChatSendRequest(content="build the report", conversation_id=conv_id),
            SimpleNamespace(headers={}),
            db=_nothing_found(),
            user=stranger,
        ),
        lambda: chat_web_stream.stream_web_chat_events(
            conv_id, SimpleNamespace(headers={}), client_id="stranger", db=_nothing_found(), user=stranger
        ),
        lambda: chat_web_stream.cancel_web_chat_turn(
            chat_web_stream.WebChatCancelRequest(conversation_id=conv_id),
            SimpleNamespace(headers={}),
            db=_nothing_found(),
            user=stranger,
        ),
    )
    for attempt in attempts:
        with pytest.raises(HTTPException) as refused:
            await attempt()
        assert refused.value.status_code == 404
    assert cancel_requests == [], "a non-owner must not stop the owner's turn"
    # ``cancel_tasks_for_conversation`` takes the conversation id alone, so
    # the cascade is gated on the same ownership check as the cross-process stop.
    assert cascades == [], "a non-owner must not cancel the owner's sub-agent tasks"
    assert ws_env.stored == []
    assert not ws_env.redis.subscribers.get(turn_channel(conv_id))


async def test_the_owners_stop_without_a_turn_here_requests_the_cross_process_stop(
    monkeypatch, ws_env
):
    conv_id = str(ws_env.conv.id)
    cancel_requests, cascades = _stop_requests(monkeypatch)
    answer = await chat_web_stream.cancel_web_chat_turn(
        chat_web_stream.WebChatCancelRequest(conversation_id=conv_id),
        SimpleNamespace(headers={}),
        db=_FakeSession(ws_env.conv, []),
        user=SimpleNamespace(id=ws_env.user_id),
    )
    assert answer == {"status": "cancelled", "turn": "remote"}
    assert cancel_requests == [conv_id]
    assert cascades == [conv_id], "the owner's Stop still cascades to the sub-agent tasks"


# ── Socket-less turns ─────────────────────────────────────────────────────


def test_rest_and_parent_wake_turns_publish_their_progress():
    """A REST ``send_message`` and a parent wake hold the marker with no
    socket: without a publisher a viewer saw a bare "Thinking..." until the
    answer landed. Both pass the publisher's ``on_event`` adapter to the
    loop (never ``on_token`` — the LLM call mode of those paths is
    unchanged), publish the user message on commit (REST), and close the
    turn with a ``done`` frame."""
    from app.services import parent_wake

    rest_src = inspect.getsource(chat_module.send_message)
    # The agent run and the General Assistant run publish their progress
    # through ``_rest_primary_event``, which keeps the run's context reading
    # and forwards every event to the publisher; an @mentioned agent's
    # run publishes through a guard that silences it once the dispatch
    # cut it.
    assert rest_src.count("on_event=_rest_primary_event") == 2
    primary = rest_src[rest_src.index("async def _rest_primary_event") :]
    primary = primary[: primary.index("\n\n")]
    assert "await _rest_publisher.on_event(event)" in primary
    # The mentioned run's own wrapper keeps ITS reading and forwards
    # every event to that guard.
    assert "on_event=_mention_event" in rest_src
    mention = rest_src[rest_src.index("async def _mention_event") :]
    mention = mention[: mention.index("\n\n")]
    assert "await _rest_mention_event(event)" in mention
    assert "await _rest_publisher.on_event(event)" in rest_src
    assert "on_token=_rest_publisher" not in rest_src
    assert '"type": "user_message"' in rest_src
    assert "_rest_publisher.offer(_rest_done_payload)" in rest_src
    # The REST turn ends in ONE place, ``_rest_end_turn``: the lock refresher
    # stopped, the publisher flushed and closed (terminal frame out, journal
    # dropped), and only THEN the lock released - the web chat / parent-wake
    # ordering. The persistence phase runs with the lock HELD: the reply is
    # stored and committed, the ``done`` offered, and the phase's ``finally``
    # ends the turn on every exit (a DB error in ``store_message`` included,
    # so a failed request leaks neither a drainer task nor the journal).
    assert rest_src.count("release_turn_and_drain(") == 1
    end_turn = rest_src[rest_src.index("async def _rest_end_turn") :]
    end_turn = end_turn[: end_turn.index("Pre-loop setup")]
    # The close carries the interrupted terminal: a no-op once the ``done``
    # was offered, the frame that releases every viewer when the turn never
    # reached it (torn down, or failed in the persistence phase).
    close_call = "await _rest_publisher.aclose(terminal=chat_turn_stream.interrupted_frame())"
    assert (
        end_turn.index("_rest_refresher_task.cancel()")
        < end_turn.index(close_call)
        < end_turn.index("release_turn_and_drain(")
    )
    phase = rest_src[rest_src.index("assistant_msg = await store_message(") :]
    offer_at = phase.index("_rest_publisher.offer(_rest_done_payload)")
    assert phase.index("await db.commit()") < offer_at
    assert offer_at < phase.index("await _rest_end_turn(\n            follow_up=")
    # The pre-loop failure (history load, compaction) closes the same way.
    assert rest_src.count("terminal=chat_turn_stream.interrupted_frame()") == 2

    wake_src = inspect.getsource(parent_wake._run_parent_wake_turn)
    assert wake_src.count("on_event=publisher.on_event") == 2
    assert "on_token=publisher" not in wake_src
    assert '"type": "done"' in wake_src
    # The ``done`` is published before the marker is released - which happens
    # right after it, BEFORE the outbound channel delivery (an HTTP call that
    # can take seconds, during which a refetching viewer would read the held
    # marker as a running turn), and once: the ``finally`` only releases an
    # exit that never reached the normal end.
    assert wake_src.count("chat_turn_guard.release_turn_and_drain(") == 1
    offer_at = wake_src.index("publisher.offer(_done_frame)")
    release_at = wake_src.index("stranded = await _release_turn()", offer_at)
    assert offer_at < release_at < wake_src.index("_send_proactive_channel_message(", offer_at)
    assert "if not released:\n            stranded = await _release_turn()" in wake_src
    # A wake torn down before its ``done`` closes for its viewers with the
    # interrupted terminal.
    assert "terminal=chat_turn_stream.interrupted_frame()" in wake_src


async def test_parent_wake_done_frame_carries_the_attachments_the_web_chat_path_would(
    fanout_redis, monkeypatch
):
    """A parent-wake reply may carry the finished task's files
    (``attach_rows_to_message`` binds the rows and writes the ``attachments``
    block). The relayed ``done`` used to carry the text alone, so a viewer
    cached a same-id assistant row WITHOUT its file chips and only a later
    refetch repaired it. The frame now carries the attachments exactly as the
    web chat / REST terminals do - through ``parse_attachments`` over the stored
    row, message-scoped download URLs included."""
    from unittest.mock import AsyncMock

    from app.services import parent_wake
    from app.services.parent_wake import _run_parent_wake_turn, format_parent_wake_prompt
    from tests.agentic_gate_stub import stub_agentic_gate
    from tests.test_parent_wake import _conv, _FakeResult, _FakeSession

    # The wake turn asks the background gate first; AI may run.
    stub_agentic_gate(monkeypatch)
    conv = _conv()
    user_id = str(conv.user_id)
    task_id = str(uuid.uuid4())
    wake_text = format_parent_wake_prompt(
        task_id=task_id, agent_name="Researcher", terminal_status="completed"
    )
    file_id = uuid.uuid4()
    result_ref = SimpleNamespace(file_id=str(file_id), filename="report.csv")
    row = SimpleNamespace(id=file_id, filename="report.csv", content_type="text/csv", size=12)
    stored: dict[str, SimpleNamespace] = {}

    async def _store(_db, _conv_id, role, content, **kwargs):
        msg = SimpleNamespace(
            id=uuid.uuid4(), role=role, content=content, metadata_json=kwargs.get("metadata_json")
        )
        stored[role] = msg
        return msg

    async def _attach(_db, message, rows, *, conversation_id):
        # The real helper's contract: bind, then write the chip block.
        message.metadata_json = json.dumps(
            {
                "attachments": [
                    {
                        "filename": r.filename,
                        "type": "csv",
                        "size": r.size,
                        "file_id": str(r.id),
                        "content_type": r.content_type,
                    }
                    for r in rows
                ]
            }
        )
        return list(rows)

    monkeypatch.setattr("app.channels.router.store_message", AsyncMock(side_effect=_store))
    monkeypatch.setattr(parent_wake, "load_attachment_rows", AsyncMock(return_value=[row]))
    monkeypatch.setattr(parent_wake, "attach_rows_to_message", AsyncMock(side_effect=_attach))
    monkeypatch.setattr(
        "app.services.chat_turn_guard.acquire_or_fold", AsyncMock(return_value=("token-1", False))
    )
    monkeypatch.setattr(
        "app.services.chat_turn_guard.release_turn_and_drain", AsyncMock(return_value=[])
    )
    monkeypatch.setattr("app.core.database.release_db_connection_before_loop", AsyncMock())
    monkeypatch.setattr(
        "app.channels.router.get_conversation_history",
        AsyncMock(return_value=[SimpleNamespace(role="user", content=wake_text, files=None)]),
    )
    monkeypatch.setattr(
        "app.services.agent_chat.run_agent_chat", AsyncMock(return_value=("Here is the report.", None))
    )
    monkeypatch.setattr("app.services.sub_agent.publish_task_event", AsyncMock())
    agent = SimpleNamespace(id=conv.agent_id, enabled=True, name="Parent")
    session = _FakeSession([_FakeResult(agent)])

    await _run_parent_wake_turn(
        session,
        conversation=conv,
        user_id=user_id,
        wake_text=wake_text,
        persist_user=True,
        followup_depth=0,
        task_id=task_id,
        activity_user_id=user_id,
        result_files=[result_ref],
    )

    assistant = stored["assistant"]
    frames = fanout_redis.frames(turn_channel(str(conv.id)))
    done = frames[-1]
    assert done["type"] == "done" and done["turn"] == "token-1"
    assert done["content"] == "Here is the report."
    assert done["message_id"] == str(assistant.id)
    # The same serializer the web chat / REST terminals run over the stored row.
    expected = chat_module.parse_attachments(
        assistant.metadata_json, conversation_id=str(conv.id), message_id=str(assistant.id)
    )
    assert expected and done["attachments"] == expected
    assert done["attachments"][0]["download_url"] == (
        f"/api/v1/chat/conversations/{conv.id}/messages/{assistant.id}/files/{file_id}/download"
    )
    # Terminal published before the marker release, journal closed.
    state_key, *_content_keys = journal_keys(str(conv.id), "token-1")
    assert fanout_redis.hashes[state_key]["ended"] == "1"


def test_abnormal_exits_close_the_turn_with_the_interrupted_terminal():
    """``_clear_processing_marker`` is the funnel of every connection exit
    that never published a terminal frame (a mid-run cancellation, a
    failure in the persistence phase, the runner's own teardown): it
    closes the publisher with the interrupted terminal so viewers are
    released at once, then releases the marker."""
    src = inspect.getsource(chat_module.run_chat_connection)
    marker_fn = src.index("async def _clear_processing_marker(")
    closed = src.index("aclose(terminal=chat_turn_stream.interrupted_frame())", marker_fn)
    released = src.index("release_turn_and_drain(", marker_fn)
    assert marker_fn < closed < released
