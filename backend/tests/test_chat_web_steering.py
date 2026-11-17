"""Behavioral tests for mid-run steering on the web chat.

A message sent while a turn runs is a **steering message**: the steer route
(``POST /web/chat/messages/steer``) rate-limits it, persists it and folds it
into the running turn, whose steering bridge hands it to the agent loop at
the next iteration boundary. It ends in exactly one of "stored + handed over"
or a refusal — never a silent drop, and never handed over before it is
durably persisted.

Pins, through the real steer route and ``run_chat_connection`` (the SSE send
connection, with the module's fakes, which the other turn suites reuse):

* an over-limit steer is a 429, never stored nor handed over, and leaves the
  turn's stream alone;
* a steer whose persist fails is refused and never handed over;
* a successful steer is persisted BEFORE the running loop reads it, and the
  answer names the turn and echoes the client's ``steer_id``;
* a too-late steer runs a follow-up pass whose history replays what was
  injected, unless the account was deactivated meanwhile;
* after a Stop, the next message is a new turn, not steering.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import chat as chat_module
from app.core import config, rate_limit
from app.services import chat_turn_guard, chat_web_transport

STEER_TEXT = "actually, filter on subregion instead"


# ── Fakes ─────────────────────────────────────────────────────────────────


class _FakeRedisRL:
    """Just enough of the Lua EVAL path for ``enforce_rate_limit`` and ``refund_rate_limit``."""

    def __init__(self):
        self.store: dict[str, int] = {}

    async def eval(self, script, _numkeys, key, *_args):
        if script == rate_limit.DECR_IF_POSITIVE_LUA:
            if self.store.get(key, 0) > 0:
                self.store[key] -= 1
            return self.store.get(key, 0)
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]


class _FakeRedisKV:
    """In-memory stand-in for the shared Redis client.

    Covers the single-flight turn lock (get / set NX EX / delete /
    exists) and the cross-socket steering inbox (rpush / lpop / lrem /
    expire) used by ``chat_turn_guard``, plus the legacy
    processing-flag calls.  One instance is shared across the whole test
    (the guard's lock semantics are stateful).
    """

    def __init__(self):
        self.kv: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}
        # Sorted sets as member -> score (the channel-turn holds).
        self.zsets: dict[str, dict[str, float]] = {}
        # Plain sets (the index of queued channel messages).
        self.sets: dict[str, set[str]] = {}
        # The TTL a script gave a key (only the kept Stop's is read).
        self.ttls: dict[str, int] = {}
        # The clock the holds lapse on, in seconds; a test can replace it
        # with one only it moves.
        self.now = time.monotonic

    async def llen(self, key):
        return len(self.lists.get(key, []))

    async def lindex(self, key, index):
        lst = self.lists.get(key, [])
        return lst[index] if -len(lst) <= index < len(lst) else None

    async def lrange(self, key, start, end):
        lst = self.lists.get(key, [])
        return list(lst[start:] if end == -1 else lst[start : end + 1])

    async def sadd(self, key, member):
        before = len(self.sets.setdefault(key, set()))
        self.sets[key].add(member)
        return len(self.sets[key]) - before

    async def srem(self, key, member):
        members = self.sets.get(key, set())
        if member in members:
            members.discard(member)
            return 1
        return 0

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    def _live_members(self, key):
        now = self.now() * 1000
        members = self.zsets.setdefault(key, {})
        for member in [m for m, score in members.items() if score <= now]:
            del members[member]
        return members

    async def zrem(self, key, member):
        return 1 if self.zsets.get(key, {}).pop(member, None) is not None else 0

    async def set(self, key, value, nx=False, xx=False, ex=None):
        if nx and key in self.kv:
            return None
        if xx and key not in self.kv:
            return None
        self.kv[key] = value
        return True

    async def get(self, key):
        return self.kv.get(key)

    async def incr(self, key):
        self.kv[key] = str(int(self.kv.get(key) or 0) + 1)
        return int(self.kv[key])

    async def getdel(self, key):
        return self.kv.pop(key, None)

    async def delete(self, key):
        return 1 if self.kv.pop(key, None) is not None else 0

    async def exists(self, key):
        return 1 if key in self.kv else 0

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)
        return len(self.lists[key])

    async def lpop(self, key):
        lst = self.lists.get(key)
        if not lst:
            return None
        return lst.pop(0)

    async def lrem(self, key, count, value):
        lst = self.lists.get(key, [])
        removed = 0
        limit = abs(count) if count else len(lst)
        indices = range(len(lst) - 1, -1, -1) if count < 0 else range(len(lst))
        to_remove = []
        for i in indices:
            if lst[i] == value:
                to_remove.append(i)
                removed += 1
                if removed >= limit:
                    break
        for i in sorted(to_remove, reverse=True):
            lst.pop(i)
        return removed

    async def expire(self, key, ttl):
        return 1 if key in self.kv or key in self.lists else 0

    async def eval(self, script, numkeys, *keys_and_args):
        """Interpret the guard's Lua scripts."""
        from app.services import chat_turn_guard

        key = keys_and_args[0]
        args = [str(a) for a in keys_and_args[numkeys:]]
        # Matched by identity first: the fold script carries EXPIRE too.
        if script == chat_turn_guard._RECORD_STOP_LUA:  # moves forward only
            if int(args[0]) > int(self.kv.get(key) or 0):
                self.kv[key] = args[0]
            return 1
        if script == chat_turn_guard._REQUEST_CANCEL_LUA:
            request_key, pending_key = keys_and_args[1], keys_and_args[2]
            holder = self.kv.get(key)
            if holder is not None:
                self.kv[request_key] = holder
                self.kv.pop(pending_key, None)
                return 1
            if args[2] == "1":
                self.kv[pending_key] = "1"
                self.ttls[pending_key] = int(args[1])
            else:
                self.kv.pop(pending_key, None)
            return 0
        if script == chat_turn_guard._FOLD_UNLESS_STOPPED_LUA:
            stop_key = keys_and_args[1]
            if int(self.kv.get(stop_key) or 0) >= int(args[1]):
                return 0
            self.lists.setdefault(key, []).append(args[0])
            return 1
        if script == chat_turn_guard._RESERVE_TURN_ENTRY_LUA:
            holder = self.kv.get(key)
            if holder and str(holder).startswith(args[0]):
                return holder
            members = self._live_members(keys_and_args[1])
            members[args[1]] = self.now() * 1000 + int(args[2])
            return None
        if script == chat_turn_guard._ACQUIRE_CHANNEL_TURN_LUA:
            if self._live_members(keys_and_args[1]) or key in self.kv:
                return None
            self.kv[key] = args[0]
            return "OK"
        if script == chat_turn_guard._RENEW_TURN_ENTRY_LUA:
            members = self._live_members(key)
            if args[0] not in members:
                return 0
            members[args[0]] = self.now() * 1000 + int(args[1])
            return 1
        if script == chat_turn_guard._ENQUEUE_CHANNEL_MESSAGE_LUA:
            queue = self.lists.setdefault(key, [])
            if len(queue) >= int(args[1]):
                return 0
            queue.append(args[0])
            return len(queue)
        # The claimed list of queued channel messages: the turn holder only.
        if script == chat_turn_guard._CLAIM_QUEUED_CHANNEL_MESSAGE_LUA:
            if self.kv.get(key) != args[0]:
                return None
            queue = self.lists.get(keys_and_args[1]) or []
            if not queue:
                return None
            item = queue.pop(0)
            self.lists.setdefault(keys_and_args[2], []).append(item)
            return item
        if script == chat_turn_guard._ANSWER_CLAIMED_CHANNEL_MESSAGES_LUA:
            if self.kv.get(key) != args[0]:
                return 0
            return 1 if self.lists.pop(keys_and_args[1], None) is not None else 0
        if script == chat_turn_guard._REQUEUE_CLAIMED_CHANNEL_MESSAGES_LUA:
            if self.kv.get(key) != args[0]:
                return -1
            items = args[2:]
            self.lists[keys_and_args[1]] = [*items, *self.lists.get(keys_and_args[1], [])]
            self.lists.pop(keys_and_args[2], None)
            return len(items)
        if script == chat_turn_guard._FORGET_EMPTY_CHANNEL_QUEUE_LUA:
            if self.lists.get(key) or self.lists.get(keys_and_args[1]):
                return 0
            return await self.srem(keys_and_args[2], args[0])
        if "EXPIRE" in script:  # atomic compare-and-refresh
            current = self.kv.get(key)
            if current == args[0]:
                return 1
            if current is None:
                self.kv[key] = args[0]
                return 1
            return 0
        if "DEL" in script:  # atomic compare-and-delete
            if self.kv.get(key) == args[0]:
                self.kv.pop(key, None)
                return 1
            return 0
        raise NotImplementedError(f"unsupported test script: {script!r}")


class _FakeConnection(chat_web_transport.HeadlessTurnConnection):
    """The web chat's SSE send connection, its frames decoded onto a shared timeline.

    Its first inbound frame is the send; *then* (``cancel`` / ``skip_wait``)
    follow, as the Stop and "Continue now" routes inject them.
    """

    def __init__(
        self,
        conversation_id: str,
        frame: dict,
        timeline: list[tuple] | None = None,
        *,
        then: list[dict] = (),
    ):
        super().__init__(conversation_id, frame)
        self.timeline = [] if timeline is None else timeline
        for later in then:
            self.inject(later)
        self.bind(self._record)

    async def _record(self, message: dict) -> None:
        for chunk in (message.get("body") or b"").decode().split("\n\n"):
            if chunk.startswith("data: "):
                self.timeline.append(("sent", json.loads(chunk[6:])))

    @property
    def sent(self) -> list[dict]:
        return [entry[1] for entry in self.timeline if entry[0] == "sent"]


async def _run_connection(
    conn: _FakeConnection,
    user_id: uuid.UUID,
    *,
    origin: str | None = None,
    token_issued_at: int | None = None,
    impersonated_by: str | None = None,
) -> None:
    """Serve *conn* as ``POST /web/chat/messages`` does, then forget it as the response does."""
    from app.api.v1 import chat_web_stream

    auth = chat_module.ChatConnectionAuth(
        user_id=user_id,
        session=SimpleNamespace(),
        impersonated_by=impersonated_by,
        token_issued_at=token_issued_at,
        origin=origin,
    )
    try:
        await chat_web_stream._run_web_send(conn, conversation_id=conn.conversation_id, auth=auth)
    finally:
        chat_web_transport.unregister(conn)


async def _send(env, frame: dict, *, then: list[dict] = (), timeline=None, **auth) -> _FakeConnection:
    """One send on *env*'s conversation, run to its end; its frames land on *timeline* (env's by default)."""
    conn = _FakeConnection(str(env.conv.id), frame, env.timeline if timeline is None else timeline, then=then)
    await _run_connection(conn, env.user_id, **auth)
    return conn


async def _steer(env, content: str, *, steer_id: str | None = None, file_ids=None, client_id: str | None = None):
    """``POST /web/chat/messages/steer``: the answer, or the ``HTTPException`` it raised."""
    from app.api.v1 import chat_web_stream

    body = chat_web_stream.WebChatSteerRequest(
        conversation_id=str(env.conv.id), content=content, steer_id=steer_id, file_ids=file_ids
    )
    request = SimpleNamespace(headers={chat_web_stream.CLIENT_ID_HEADER: client_id} if client_id else {})
    try:
        return await chat_web_stream.steer_web_chat_message(
            body, request, db=_FakeSession(env.conv, []), user=SimpleNamespace(id=env.user_id)
        )
    except HTTPException as exc:
        return exc


class _FakeSession:
    """Per-call session: every scalar lookup finds the fake conversation
    (the only entity the no-agent WS turn needs); commits are recorded on
    the timeline so persist-before-ack ordering can be asserted."""

    def __init__(self, conv, timeline: list[tuple]):
        self._conv = conv
        self._timeline = timeline

    async def execute(self, _stmt):
        conv = self._conv

        class _R:
            def scalar_one_or_none(self):
                return conv

        return _R()

    async def commit(self):
        self._timeline.append(("commit",))

    async def rollback(self):
        pass

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False


async def _wait_for_frame(conn: _FakeConnection, frame_type: str, timeout: float = 2.0):
    """Poll the fake connection until a frame of *frame_type* appears."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        for frame in conn.sent:
            if frame.get("type") == frame_type:
                return frame
        await asyncio.sleep(0.01)
    return None


# ── Shared plumbing ───────────────────────────────────────────────────────


@pytest.fixture
def fake_rl(monkeypatch):
    fake = _FakeRedisRL()

    async def _client():
        return fake

    monkeypatch.setattr(rate_limit, "redis_client", _client)
    monkeypatch.setattr(config.settings, "rate_limit_fail_mode", "open")
    return fake


@pytest.fixture
def ws_env(monkeypatch, fake_rl):
    """Patch everything around ``run_chat_connection`` so a no-agent turn runs
    end-to-end against a scripted ``run_general_chat`` stub."""
    import app.api.v1.platform as platform_module
    import app.core.database as database_module
    import app.core.redis as redis_module
    import app.llm.router as llm_router_module

    user_id = uuid.uuid4()
    conv = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=user_id,
        channel="web",
        agent_id=None,
        title="steering test",
        transfer_source_agent_id=None,
        tool_overrides_json=None,
        # Presentation Studio binding: the WS turn resolves it every
        # time; ``None`` is an ordinary, unbound chat.
        presentation_id=None,
        video_project_id=None,
        custom_dashboard_id=None,
        fleet_id=None,
    )
    timeline: list[tuple] = []
    stored: list[tuple[str, str]] = []
    env = SimpleNamespace(
        user_id=user_id,
        conv=conv,
        timeline=timeline,
        stored=stored,
        fake_rl=fake_rl,
        fail_persist_for=set(),
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
    monkeypatch.setattr(chat_turn_guard, "BRIDGE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(database_module, "async_session", lambda: _FakeSession(conv, timeline))

    # One shared instance: the single-flight turn lock is stateful
    # (acquired at turn start, released at turn end).
    fake_kv = _FakeRedisKV()
    env.fake_kv = fake_kv

    async def _redis_client():
        return fake_kv

    monkeypatch.setattr(redis_module, "redis_client", _redis_client)
    monkeypatch.setattr(chat_module, "_spawn_cancel_cascade", lambda _cid, *_a: None)

    # The thread as a reader sees it, in sequence order: (id, role, content).
    env.thread = []

    async def _store_message(_db, _conv_id, role, content, **_kw):
        if content in env.fail_persist_for:
            raise RuntimeError("simulated persist failure")
        timeline.append(("persist", role, content))
        stored.append((role, content))
        message_id = uuid.uuid4()
        env.thread.append((message_id, role, content))
        return SimpleNamespace(id=message_id, metadata_json=None)

    monkeypatch.setattr(chat_module, "store_message", _store_message)

    async def _move_messages_to_end(_db, _conv_id, message_ids):
        wanted = [uuid.UUID(m) for m in message_ids]
        moved = [row for m in wanted for row in env.thread if row[0] == m]
        env.thread[:] = [row for row in env.thread if row[0] not in wanted] + moved
        return len(moved)

    monkeypatch.setattr(chat_module, "move_messages_to_end", _move_messages_to_end)

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


# ── Tests ─────────────────────────────────────────────────────────────────


def _start(env, content: str = "build the report", **kw) -> tuple[_FakeConnection, asyncio.Task]:
    conn = _FakeConnection(str(env.conv.id), {"content": content}, env.timeline, **kw)
    task = asyncio.create_task(_run_connection(conn, env.user_id))
    conn.turn_task = task
    return conn, task


# A guard against a hung turn only, never the measure of a slow one: on a
# loaded CI worker the path to ``turn_started`` (lazy imports, the turn lock,
# the pending-approval retirement) took over 2 s, so a fixed short deadline
# failed a turn that did start (xtm-one main CI run 43164414876).
_TURN_START_HANG_GUARD_SECONDS = 60.0


async def _turn_running(conn: _FakeConnection) -> str:
    """The ``turn_started`` frame of *conn*'s send, waited for until it comes
    or the send ends without it (a real failure, raised at once)."""
    task = getattr(conn, "turn_task", None)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _TURN_START_HANG_GUARD_SECONDS
    while loop.time() < deadline:
        frame = next((f for f in conn.sent if f.get("type") == "turn_started"), None)
        if frame is not None:
            return frame["turn_id"]
        if task is not None and task.done():
            task.result()  # the send's own error, when it raised
            break
        await asyncio.sleep(0.01)
    raise AssertionError("the turn never started")


async def test_steering_over_limit_is_429_and_never_reaches_the_turn(monkeypatch, ws_env):
    """An over-limit steer is refused with the throttle's 429, stored nowhere
    and never handed to the running loop; the turn's own stream carries no
    ``error`` frame for it (that would wreck the client's streaming state)."""
    # The turn send consumes the last slot; the steer exceeds it.
    ws_env.fake_rl.store[f"rl:chat-send:{ws_env.user_id}"] = chat_module.CHAT_MESSAGES_PER_MINUTE - 1
    release = asyncio.Event()
    queue_payloads: list = []

    async def _run(_db, _uid, _history, _content, **kwargs):
        await release.wait()
        await asyncio.sleep(0.05)  # a bridge poll or two
        from app.tools.agent_loop import drain_steering_queue

        queue_payloads.extend(drain_steering_queue(kwargs["steering_queue"]))
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    conn, task = _start(ws_env)
    await _turn_running(conn)
    answer = await _steer(ws_env, STEER_TEXT, steer_id="bubble-1")
    release.set()
    await asyncio.wait_for(task, 5)

    assert isinstance(answer, HTTPException)
    assert answer.status_code == 429
    assert "Too many requests" in answer.detail
    assert not any(f.get("type") == "error" for f in conn.sent)
    assert ("user", STEER_TEXT) not in ws_env.stored
    assert queue_payloads == []


async def test_steering_persist_failure_is_refused_and_never_reaches_the_turn(monkeypatch, ws_env):
    """If the steering message cannot be persisted, it is refused (503) and
    never handed over — the agent would otherwise act on a message missing
    from the stored thread after a refresh."""
    ws_env.fail_persist_for.add(STEER_TEXT)
    release = asyncio.Event()
    leftovers: list = []

    async def _run(_db, _uid, _history, _content, **kwargs):
        await release.wait()
        await asyncio.sleep(0.05)
        from app.tools.agent_loop import drain_steering_queue

        leftovers.extend(drain_steering_queue(kwargs["steering_queue"]))
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    conn, task = _start(ws_env)
    await _turn_running(conn)
    answer = await _steer(ws_env, STEER_TEXT)
    release.set()
    await asyncio.wait_for(task, 5)

    assert isinstance(answer, HTTPException)
    assert answer.status_code == 503
    assert leftovers == [], "a failed persist must never reach the queue"


async def test_steering_success_persists_before_the_turn_reads_it(monkeypatch, ws_env):
    """The happy path: rate-limit ok -> persisted -> handed to the running
    loop's queue, in that order; the answer names the turn it went to and
    echoes the client's ``steer_id``."""
    received: list[str] = []

    async def _run(_db, _uid, _history, _content, **kwargs):
        text = await asyncio.wait_for(kwargs["steering_queue"].get(), timeout=2)
        ws_env.timeline.append(("received", text))
        received.append(text)
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    conn, task = _start(ws_env)
    turn_id = await _turn_running(conn)
    answer = await _steer(ws_env, STEER_TEXT, steer_id="optimistic-steer-1234-abcd")
    await asyncio.wait_for(task, 5)

    assert answer == {"status": "steered", "steer_id": "optimistic-steer-1234-abcd", "turn_id": turn_id}
    assert received == [STEER_TEXT]
    persist_idx = ws_env.timeline.index(("persist", "user", STEER_TEXT))
    assert persist_idx < ws_env.timeline.index(("received", STEER_TEXT))


async def test_followup_pass_replays_injected_steering_texts_in_history(monkeypatch, ws_env):
    """When a too-late steering message triggers a fold-in follow-up pass,
    steering texts that WERE injected into the loop earlier in the same
    turn (drained from the queue, so absent from the leftovers) must be
    replayed in the follow-up history between the original user turn and
    the completed assistant segment — otherwise the follow-up pass runs
    against a history that diverges from the persisted thread."""
    injected = "use metric units"
    late = "and add a summary table"
    histories: list[list] = []
    took_injected = asyncio.Event()

    async def _run(_db, _uid, history, _content, **kwargs):
        histories.append(list(history))
        if len(histories) == 1:
            queue = kwargs["steering_queue"]
            # Simulate run_agent_loop injecting the first steering text...
            text = await asyncio.wait_for(queue.get(), timeout=2)
            assert text == injected
            took_injected.set()
            # ...then finishing its final stream while the second steering
            # message arrives too late (stays in the queue as a leftover).
            deadline = asyncio.get_event_loop().time() + 2
            while queue.qsize() == 0:
                assert asyncio.get_event_loop().time() < deadline
                await asyncio.sleep(0.01)
            return ("segment one", None)
        return ("final answer", None)

    _patch_general_chat(monkeypatch, _run)
    conn, task = _start(ws_env)
    await _turn_running(conn)
    answers = [await _steer(ws_env, injected)]
    await asyncio.wait_for(took_injected.wait(), 2)
    answers.append(await _steer(ws_env, late))
    await asyncio.wait_for(task, 5)

    assert len(histories) == 2, "the leftover must trigger a follow-up pass"
    followup = [(m.role, m.content) for m in histories[1]]
    assert followup[-3:] == [
        ("user", "build the report"),
        ("user", injected),
        ("assistant", "segment one"),
    ], "the injected steering text must sit between the user turn and the segment"
    # The follow-up turn itself is the late steering text.
    done_frames = [f for f in conn.sent if f.get("type") == "done"]
    assert [f["content"] for f in done_frames] == ["segment one", "final answer"]
    # Both steering messages were persisted and handed over.
    assert ("user", injected) in ws_env.stored
    assert ("user", late) in ws_env.stored
    assert [a["status"] for a in answers] == ["steered", "steered"]


async def test_a_steer_sent_during_the_final_answer_reads_after_that_answer(monkeypatch, ws_env):
    """A steer sent while the final answer streams is never read by it (the
    loop injects steering at an iteration boundary only): a follow-up pass
    answers it. Stored when sent, it would read ABOVE the answer that ignored
    it, and the two answers in a row read as one message answered twice. The
    turn moves it after that answer: user, answer, steer, follow-up answer."""
    late = "explain it step by step"

    async def _run(_db, _uid, _history, _content, **kwargs):
        queue = kwargs["steering_queue"]
        if not ws_env.thread or ws_env.thread[-1][2] != late:
            # The final generation: the steer arrives and stays queued.
            deadline = asyncio.get_event_loop().time() + 2
            while queue.qsize() == 0:
                assert asyncio.get_event_loop().time() < deadline
                await asyncio.sleep(0.01)
            return ("the full answer", None)
        return ("step one", None)

    _patch_general_chat(monkeypatch, _run)
    conn, task = _start(ws_env)
    await _turn_running(conn)
    answer = await _steer(ws_env, late)
    await asyncio.wait_for(task, 5)

    assert answer["status"] == "steered"
    assert [(role, content) for _id, role, content in ws_env.thread] == [
        ("user", "build the report"),
        ("assistant", "the full answer"),
        ("user", late),
        ("assistant", "step one"),
    ]


async def test_a_steer_the_loop_took_keeps_its_place_before_the_answer(monkeypatch, ws_env):
    """A steer injected at an iteration boundary shaped the answer: it stays
    where it was stored, before that answer, and no follow-up pass runs."""

    async def _run(_db, _uid, _history, _content, **kwargs):
        await asyncio.wait_for(kwargs["steering_queue"].get(), timeout=2)
        return ("answer that read it", None)

    _patch_general_chat(monkeypatch, _run)
    conn, task = _start(ws_env)
    await _turn_running(conn)
    await _steer(ws_env, STEER_TEXT)
    await asyncio.wait_for(task, 5)

    assert [(role, content) for _id, role, content in ws_env.thread] == [
        ("user", "build the report"),
        ("user", STEER_TEXT),
        ("assistant", "answer that read it"),
    ]


async def test_followup_pass_does_not_run_once_the_account_is_deactivated(monkeypatch, ws_env):
    """A too-late steering message starts a follow-up pass - new work on the
    account's behalf - so it passes the account gate like the turn
    that started it: an account deactivated during the first pass gets that
    pass's answer, and no second one."""
    passes = 0
    deactivated = False

    async def _account(_db, _uid, _issued_at, **_kw):
        return "Invalid token" if deactivated else None

    monkeypatch.setattr(chat_module, "resolve_account_rejection", _account)

    async def _run(_db, _uid, _history, _content, **kwargs):
        nonlocal passes, deactivated
        passes += 1
        queue = kwargs["steering_queue"]
        deadline = asyncio.get_event_loop().time() + 2
        while queue.qsize() == 0:
            assert asyncio.get_event_loop().time() < deadline
            await asyncio.sleep(0.01)
        deactivated = True
        return ("segment one", None)

    _patch_general_chat(monkeypatch, _run)
    conn, task = _start(ws_env)
    await _turn_running(conn)
    await _steer(ws_env, "and add a summary table")
    await asyncio.wait_for(task, 5)

    assert passes == 1, "a deactivated account must not start the follow-up pass"
    done_frames = [f for f in conn.sent if f.get("type") == "done"]
    assert [f["content"] for f in done_frames] == ["segment one"]


@pytest.mark.parametrize(
    "fields",
    [{"content": 12345}, {"content": STEER_TEXT, "file_ids": "not-a-list"}, {"content": STEER_TEXT, "file_ids": 12345}],
    ids=["non-string-content", "string-file-ids", "number-file-ids"],
)
def test_a_steer_body_of_the_wrong_shape_is_refused_before_anything_is_stored(fields):
    """``content`` and ``file_ids`` come straight from client JSON: a string
    would iterate per character (one bogus attachment lookup per char) and a
    number would raise mid-handling. The request model refuses both shapes
    (a 422) before the route stores or hands over anything."""
    from pydantic import ValidationError

    from app.api.v1 import chat_web_stream

    with pytest.raises(ValidationError):
        chat_web_stream.WebChatSteerRequest(conversation_id=str(uuid.uuid4()), **fields)


async def test_a_message_sent_after_stop_is_a_new_turn_not_steering(monkeypatch, ws_env):
    """After a Stop the turn is over: the next message is a new send that
    runs its own turn (its own ``thinking``, stored through the regular turn
    path), never steering for the aborted response."""
    run_calls: list[str] = []

    async def _run(_db, _uid, _history, content, **kwargs):
        run_calls.append(content)
        if len(run_calls) == 1:
            await asyncio.wait_for(kwargs["cancel_event"].wait(), timeout=2)
            return ("cancelled mid-run", None)
        return ("second turn answer", None)

    _patch_general_chat(monkeypatch, _run)
    first = await asyncio.wait_for(
        _send(ws_env, {"content": "build the report"}, then=[{"type": "cancel"}], timeline=[]), 5
    )
    second = await asyncio.wait_for(_send(ws_env, {"content": "late message"}, timeline=[]), 5)

    assert any(f.get("type") == "cancelled" for f in first.sent)
    assert run_calls == ["build the report", "late message"]
    assert [f.get("type") for f in second.sent].count("thinking") == 1
    assert not any(f.get("type") == "steered" for f in first.sent + second.sent)
    assert ("user", "late message") in ws_env.stored
