"""The native web chat over HTTP + SSE (``/api/v1/web/chat/*``).

Runs the real connection runner (``chat.run_chat_connection``) through the
SSE transport, with the steering suite's fakes around it
(``test_chat_web_steering.py``):

* a client that goes away mid-turn leaves the turn running and its answer
  stored;
* Stop and "Continue now" reach the turn on this replica (injected into the
  stream's connection) and on another one (a Redis request the turn's bridge
  consumes);
* steering answers 200 with the turn it went to, 409 with no turn running;
* a send folded into a running turn relays that turn up to its end;
* the events stream follows the conversation under the caller's client id;
* a session that ends closes the stream with ``session_expired``, never the turn.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import uuid
from types import SimpleNamespace

import pytest
import uvicorn
from fastapi import HTTPException

from app.api.v1 import chat as chat_module
from app.api.v1 import chat_web_stream
from app.core.sessions import SessionEndedError
from app.services import chat_turn_guard, chat_turn_stream, chat_web_transport
from tests.test_chat_web_steering import (  # noqa: F401 - fixtures
    _FakeSession,
    _patch_general_chat,
    fake_rl,
    ws_env,
)


class _Asgi:
    """The ASGI side of one SSE response: what was written, and a disconnect to trigger."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.disconnect = asyncio.Event()

    async def receive(self) -> dict:
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(self, message: dict) -> None:
        if self.disconnect.is_set() and message["type"] == "http.response.body" and message.get("body"):
            raise OSError("client gone")
        self.messages.append(message)

    @property
    def frames(self) -> list[dict]:
        out = []
        for m in self.messages:
            body = m.get("body") or b""
            for chunk in body.decode().split("\n\n"):
                if chunk.startswith("data: "):
                    out.append(json.loads(chunk[6:]))
        return out

    @property
    def completed(self) -> bool:
        return any(m["type"] == "http.response.body" and not m.get("more_body") for m in self.messages)


async def _wait_until(predicate, timeout: float = 3.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def _auth(env, *, origin: str = "tab-1") -> chat_module.ChatConnectionAuth:
    return chat_module.ChatConnectionAuth(
        user_id=env.user_id, session=SimpleNamespace(), origin=origin
    )


def _stream(env, frame: dict, *, origin: str = "tab-1"):
    conversation_id = str(env.conv.id)
    connection = chat_web_transport.HeadlessTurnConnection(conversation_id, frame)

    async def _run(conn):
        await chat_module.run_chat_connection(conn, conversation_id, _auth(env, origin=origin))

    return connection, chat_web_transport.DetachedTurnStreamResponse(connection, _run)


@pytest.fixture
def web_env(request, monkeypatch):
    env = request.getfixturevalue("ws_env")
    monkeypatch.setattr(chat_turn_guard, "BRIDGE_POLL_SECONDS", 0.01)

    async def _session_holds(session):
        return session

    monkeypatch.setattr(chat_module, "recheck_session", _session_holds)
    stopped: list[str] = []

    async def _stop(conversation_id, request_cancel=None):
        requested = await request_cancel() if request_cancel is not None else None
        stopped.append(conversation_id)
        return requested

    monkeypatch.setattr(chat_module, "_stop_conversation_work", _stop)
    env.stopped = stopped
    return env


_REAL_STOP_CONVERSATION_WORK = chat_module._stop_conversation_work


def _assistant_answers(env) -> list[str]:
    return [content for role, content in env.stored if role == "assistant"]


async def test_a_client_that_goes_away_leaves_the_turn_to_store_its_answer(web_env, monkeypatch):
    asgi = _Asgi()
    turn_running = asyncio.Event()

    async def _run(_db, _uid, _history, _content, **_kwargs):
        turn_running.set()
        await _wait_until(lambda: connection.detached)
        return ("the answer nobody watched", None)

    _patch_general_chat(monkeypatch, _run)
    connection, response = _stream(web_env, {"content": "hello"})
    task = asyncio.create_task(response({"type": "http"}, asgi.receive, asgi.send))
    await asyncio.wait_for(turn_running.wait(), 3)
    asgi.disconnect.set()
    await asyncio.wait_for(task, 5)

    assert _assistant_answers(web_env) == ["the answer nobody watched"]
    assert not any(f.get("type") == "done" for f in asgi.frames)
    assert chat_web_transport.live_connection(str(web_env.conv.id)) is None


class _StalledClient(_Asgi):
    """A client that stopped reading (no FIN): every body write after *readable* waits forever."""

    def __init__(self, readable: int = 0) -> None:
        super().__init__()
        self.readable = readable
        self.reading = asyncio.Event()

    async def send(self, message: dict) -> None:
        if message["type"] == "http.response.body":
            if self.readable <= 0:
                await self.reading.wait()
            self.readable -= 1
        self.messages.append(message)


async def test_a_send_stream_whose_client_stops_reading_ends_and_the_turn_stores_its_answer(
    web_env, monkeypatch
):
    """The response is completed all the same: one the application leaves
    open is a server error to uvicorn, and the client reads the stream's end
    once it reads again."""
    monkeypatch.setattr(chat_web_transport, "STREAM_WRITE_TIMEOUT_SECONDS", 0.05)
    client = _StalledClient()

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_token"]("a token the client never reads")
        return ("stored for the reload", None)

    _patch_general_chat(monkeypatch, _run)
    connection, response = _stream(web_env, {"content": "hello"})
    task = asyncio.create_task(response({"type": "http"}, client.receive, client.send))
    assert await _wait_until(lambda: _assistant_answers(web_env) == ["stored for the reload"])
    assert connection.detached
    assert not task.done(), "the response returned with the client's stream left open"

    client.reading.set()
    await asyncio.wait_for(task, 2)
    assert client.completed


async def test_a_send_streams_writes_are_bounded(monkeypatch):
    monkeypatch.setattr(chat_web_transport, "STREAM_WRITE_TIMEOUT_SECONDS", 0.05)
    connection = chat_web_transport.HeadlessTurnConnection("conv", {"content": "hello"})
    connection.bind(_StalledClient().send)

    with pytest.raises(chat_web_transport.StreamConsumerGoneError):
        await asyncio.wait_for(connection.send_json({"type": "stream", "content": "x"}), 2)
    assert connection.detached
    assert await asyncio.wait_for(connection.keepalive(), 2) is False

    # The response's last write, once the turn ended.
    ending = chat_web_transport.HeadlessTurnConnection("conv", {"content": "hello"})
    ending.bind(_StalledClient(readable=1).send)
    await ending.send_json({"type": "stream", "content": "read"})
    await asyncio.wait_for(ending.end_stream(), 2)
    assert ending.detached


_BIG_FRAME = {"type": "stream", "content": "x" * 65536}


def _stalling_events_stream(produced: asyncio.Event):
    async def _frames():
        try:
            while True:
                yield chat_web_transport.sse_data(_BIG_FRAME)
        finally:
            produced.set()

    return chat_web_transport.EventStreamResponse(_frames())


def _stalling_send_stream(produced: asyncio.Event):
    connection = chat_web_transport.HeadlessTurnConnection("conv", {"content": "hello"})

    async def _turn(conn):
        with contextlib.suppress(chat_web_transport.StreamConsumerGoneError):
            while True:
                await conn.send_json(_BIG_FRAME)
        produced.set()
        # The turn runs on, detached, and ends.
        await asyncio.sleep(0.1)

    return chat_web_transport.DetachedTurnStreamResponse(connection, _turn)


class _Records(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.mark.parametrize(
    "stream", [_stalling_events_stream, _stalling_send_stream], ids=["events", "send"]
)
async def test_a_stream_whose_client_stops_reading_ends_without_a_server_error(monkeypatch, stream):
    """Under uvicorn itself: a client that stops reading (no FIN) fills the
    socket until a write times out. The response is completed, not left open
    or raised out of (each a "returned without completing response" or an
    "Exception in ASGI application" at ERROR), and the client reads a whole
    chunked stream once it reads again."""
    monkeypatch.setattr(chat_web_transport, "STREAM_WRITE_TIMEOUT_SECONDS", 0.2)
    produced = asyncio.Event()

    async def _app(scope, receive, send):
        if scope["type"] == "http":
            await stream(produced)(scope, receive, send)

    records = _Records()
    logging.getLogger("uvicorn.error").addHandler(records)
    server = uvicorn.Server(
        uvicorn.Config(_app, host="127.0.0.1", port=0, log_config=None, lifespan="off", timeout_graceful_shutdown=5)
    )
    serving = asyncio.create_task(server.serve())
    client = socket.socket()
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client.setblocking(False)
        loop = asyncio.get_running_loop()
        await loop.sock_connect(client, ("127.0.0.1", port))
        await loop.sock_sendall(client, b"GET / HTTP/1.1\r\nHost: test\r\n\r\n")
        await asyncio.wait_for(produced.wait(), 10)

        received = bytearray()
        async with asyncio.timeout(20):
            while not received.endswith(b"\r\n0\r\n\r\n"):
                chunk = await loop.sock_recv(client, 1 << 20)
                if not chunk:
                    break
                received += chunk
        assert received.endswith(b"\r\n0\r\n\r\n"), "the stream was cut, not ended"
    finally:
        client.close()
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        logging.getLogger("uvicorn.error").removeHandler(records)

    assert [r.getMessage() for r in records.records if r.levelno >= logging.ERROR] == []


@pytest.mark.parametrize(
    "stream", [_stalling_events_stream, _stalling_send_stream], ids=["events", "send"]
)
async def test_a_stream_whose_client_never_reads_again_gives_its_request_up(monkeypatch, stream):
    """Under uvicorn itself: a client that stops reading and never reads
    again (alive, no FIN) holds its request only up to the abandon bound: the
    response's last write gives up, the request returns and the server closes
    the connection."""
    monkeypatch.setattr(chat_web_transport, "STREAM_WRITE_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(chat_web_transport, "STREAM_ABANDON_SECONDS", 0.3)
    produced = asyncio.Event()
    returned = asyncio.Event()

    async def _app(scope, receive, send):
        if scope["type"] == "http":
            try:
                await stream(produced)(scope, receive, send)
            finally:
                returned.set()

    server = uvicorn.Server(
        uvicorn.Config(_app, host="127.0.0.1", port=0, log_config=None, lifespan="off", timeout_graceful_shutdown=5)
    )
    serving = asyncio.create_task(server.serve())
    client = socket.socket()
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client.setblocking(False)
        loop = asyncio.get_running_loop()
        await loop.sock_connect(client, ("127.0.0.1", port))
        await loop.sock_sendall(client, b"GET / HTTP/1.1\r\nHost: test\r\n\r\n")
        await asyncio.wait_for(produced.wait(), 10)
        await asyncio.wait_for(returned.wait(), 5)
    finally:
        client.close()
        server.should_exit = True
        await asyncio.wait_for(serving, 10)


def _events_response(env, monkeypatch, relay):
    monkeypatch.setattr(chat_turn_stream, "relay_turn_frames", relay)

    async def _release(_db):
        return None

    monkeypatch.setattr(chat_web_stream, "release_db_connection_before_loop", _release)
    return chat_web_stream.stream_web_chat_events(
        str(env.conv.id),
        _request(),
        client_id="tab-9",
        db=SimpleNamespace(execute=_FakeSession(env.conv, []).execute),
        user=SimpleNamespace(id=env.user_id, web_session=SimpleNamespace()),
    )


def _endless_relay(accepted: list[int], ended: asyncio.Event):
    async def _relay(_conv_id, *, send, **_kw):
        try:
            while await send({"type": "stream", "content": str(len(accepted))}):
                accepted.append(len(accepted))
                await asyncio.sleep(0)
        finally:
            ended.set()

    return _relay


async def test_an_events_stream_holds_a_bounded_number_of_frames_for_a_client_that_stopped_reading(
    web_env, monkeypatch
):
    monkeypatch.setattr(chat_web_stream, "EVENTS_QUEUE_SIZE", 5)
    accepted: list[int] = []
    relay_ended = asyncio.Event()
    response = await _events_response(web_env, monkeypatch, _endless_relay(accepted, relay_ended))
    client = _StalledClient()

    task = asyncio.create_task(response({"type": "http"}, client.receive, client.send))
    await asyncio.wait_for(relay_ended.wait(), 2)

    # The frame being written, and a full queue: the relay is told to stop.
    assert len(accepted) <= 5 + 1
    client.reading.set()
    await asyncio.wait_for(task, 2)
    assert client.completed


async def test_an_events_stream_whose_client_stops_reading_ends(web_env, monkeypatch):
    monkeypatch.setattr(chat_web_transport, "STREAM_WRITE_TIMEOUT_SECONDS", 0.05)
    accepted: list[int] = []
    relay_ended = asyncio.Event()
    response = await _events_response(web_env, monkeypatch, _endless_relay(accepted, relay_ended))
    client = _StalledClient()

    task = asyncio.create_task(response({"type": "http"}, client.receive, client.send))
    await asyncio.wait_for(relay_ended.wait(), 2)
    assert not task.done(), "the response returned with the client's stream left open"

    client.reading.set()
    await asyncio.wait_for(task, 2)
    assert client.completed


async def test_a_runtime_error_that_is_not_a_client_gone_is_a_failure_the_client_is_told(
    web_env, monkeypatch, caplog
):
    async def _broken(_session):
        raise RuntimeError("session store broke")

    monkeypatch.setattr(chat_module, "recheck_session", _broken)
    asgi = _Asgi()
    _connection, response = _stream(web_env, {"content": "hello"})
    with caplog.at_level("ERROR", logger=chat_module.logger.name):
        await asyncio.wait_for(response({"type": "http"}, asgi.receive, asgi.send), 5)

    # Before the turn started: an HTTP error the client is told by, not a 200 stream.
    start = next(m for m in asgi.messages if m["type"] == "http.response.start")
    assert start["status"] == 500
    assert any("Chat connection error" in r.getMessage() for r in caplog.records)


async def test_the_stream_carries_the_turn_frames_and_ends_with_the_turn(web_env, monkeypatch):
    asgi = _Asgi()

    async def _run(_db, _uid, _history, _content, **_kwargs):
        return ("done here", None)

    _patch_general_chat(monkeypatch, _run)
    _connection, response = _stream(web_env, {"content": "hello"})
    await asyncio.wait_for(response({"type": "http"}, asgi.receive, asgi.send), 5)

    types = [f["type"] for f in asgi.frames]
    assert types[0] == "thinking"
    assert "turn_started" in types
    done = [f for f in asgi.frames if f["type"] == "done"]
    assert done and done[-1]["content"] == "done here" and done[-1]["turn_id"]
    assert asgi.completed
    headers = dict(asgi.messages[0]["headers"])
    assert headers[b"content-type"].startswith(b"text/event-stream")
    assert headers[b"x-accel-buffering"] == b"no"
    assert b"no-transform" in headers[b"cache-control"]


def test_the_widget_streams_are_neither_buffered_nor_compressed_by_a_proxy():
    from app.api.v1.public_chat import PUBLIC_SSE_HEADERS

    assert PUBLIC_SSE_HEADERS["X-Accel-Buffering"] == "no"
    assert "no-transform" in PUBLIC_SSE_HEADERS["Cache-Control"]


def _unnamed(frames: list[dict]) -> list[dict]:
    """The frames of a turn that do not name it (``user_message``, ``conversation_title``
    and ``session_expired`` belong to no turn)."""
    no_turn = chat_turn_stream.NON_TURN_FRAME_TYPES | {"session_expired"}
    return [f for f in frames if f["type"] not in no_turn and not f.get("turn_id")]


@pytest.mark.parametrize("ending", ["stop", "loop_failure", "store_failure"])
async def test_every_end_of_a_send_names_its_turn_and_only_a_provisional_end_says_so(
    web_env, monkeypatch, ending
):
    """A Stop and a failed loop end the turn provisionally, then with its
    stored end; a turn that cannot store its reply ends with the error that
    stopped it. Every frame, the send's acknowledgement included, names the turn."""
    asgi = _Asgi()
    turn_running = asyncio.Event()

    async def _run(_db, _uid, _history, _content, **kwargs):
        turn_running.set()
        await kwargs["on_event"]({"status": "tool_start", "tools": ["search"]})
        if ending == "stop":
            assert await _wait_until(kwargs["cancel_event"].is_set), "the Stop never reached the turn"
            return ("The request was cancelled.", None)
        raise RuntimeError("the model is unavailable")

    if ending == "store_failure":
        store = chat_module.store_message

        async def _store_fails(db, conv_id, role, content, **kwargs):
            if role == "assistant":
                raise RuntimeError("the database is gone")
            return await store(db, conv_id, role, content, **kwargs)

        monkeypatch.setattr(chat_module, "store_message", _store_fails)
    _patch_general_chat(monkeypatch, _run)
    _connection, response = _stream(web_env, {"content": "long job"})
    task = asyncio.create_task(response({"type": "http"}, asgi.receive, asgi.send))
    await asyncio.wait_for(turn_running.wait(), 3)
    if ending == "stop":
        assert await _cancel(web_env) == {"status": "cancelled", "turn": "local"}
    await asyncio.wait_for(task, 5)

    frames = asgi.frames
    turn_id = next(f["turn_id"] for f in frames if f["type"] == "turn_started")
    assert frames[0]["type"] == "thinking"
    assert not _unnamed(frames)
    assert {f["turn_id"] for f in frames if f.get("turn_id")} == {turn_id}
    ends = [f for f in frames if chat_turn_stream.is_terminal_frame(f)]
    assert [chat_turn_stream.is_provisional_end(f) for f in ends] == [True, False]
    assert ends[0]["type"] == ("cancelled" if ending == "stop" else "error")
    assert "provisional" not in ends[-1]
    if ending == "store_failure":
        assert ends[-1]["type"] == "error" and not ends[-1].get("message_id")
    else:
        assert ends[-1]["message_id"]


@pytest.mark.parametrize("state", ["locked", "unreadable"])
async def test_a_send_stream_whose_session_locks_ends_on_the_lock_frame_not_an_error(web_env, monkeypatch, state):
    """The companion's PIN locks while a turn runs: the send stream ends with
    ``app_locked``, which the web app turns into its lock screen, never an
    ``error`` frame the transcript would show. A lock that cannot be read ends
    it without a word: no lock screen for a database blip."""
    asgi = _Asgi()
    turn_running = asyncio.Event()

    async def _run(_db, _uid, _history, _content, **kwargs):
        turn_running.set()
        await asyncio.sleep(5)
        return ("an answer", None)

    async def _lock_state(_user_id, _session):
        return state if turn_running.is_set() else "open"

    monkeypatch.setattr(chat_module, "SESSION_RECHECK_SECONDS", 0.01)
    monkeypatch.setattr(chat_module, "session_lock_state", _lock_state)
    _patch_general_chat(monkeypatch, _run)
    _connection, response = _stream(web_env, {"content": "long job"})
    task = asyncio.create_task(response({"type": "http"}, asgi.receive, asgi.send))
    await asyncio.wait_for(turn_running.wait(), 3)
    assert await _wait_until(lambda: asgi.completed), "the stream did not end once its session locked"

    assert not any(f["type"] == "error" for f in asgi.frames)
    assert any(f["type"] == "app_locked" for f in asgi.frames) is (state == "locked")
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task


_RAW_ERROR = 'relation "internal_secrets" does not exist'


async def test_a_send_that_fails_before_its_turn_is_an_http_error_naming_no_internals(web_env, monkeypatch):
    """A failure the turn runner did not expect, before the turn started: the
    send is refused with a 500 and the chat's own sentence, never the exception's text."""
    asgi = _Asgi()

    async def _fails(*_args, **_kwargs):
        raise RuntimeError(_RAW_ERROR)

    monkeypatch.setattr(chat_module, "resolve_account_rejection", _fails)
    _connection, response = _stream(web_env, {"content": "hello"})
    await asyncio.wait_for(response({"type": "http"}, asgi.receive, asgi.send), 5)

    start = next(m for m in asgi.messages if m["type"] == "http.response.start")
    body = b"".join(m.get("body") or b"" for m in asgi.messages if m["type"] == "http.response.body").decode()
    assert start["status"] == 500
    assert chat_module._GENERIC_ERROR_MESSAGE in body
    assert _RAW_ERROR not in body


async def test_a_send_that_fails_after_its_turn_started_ends_on_a_sentence_naming_no_internals(web_env, monkeypatch):
    """The same failure once the turn streams (its reply cannot be stored): an
    ``error`` frame with the chat's own sentence, never the exception's text."""
    asgi = _Asgi()

    async def _run(_db, _uid, _history, _content, **kwargs):
        return ("an answer", None)

    store = chat_module.store_message

    async def _store_fails(db, conv_id, role, content, **kwargs):
        if role == "assistant":
            raise RuntimeError(_RAW_ERROR)
        return await store(db, conv_id, role, content, **kwargs)

    monkeypatch.setattr(chat_module, "store_message", _store_fails)
    _patch_general_chat(monkeypatch, _run)
    _connection, response = _stream(web_env, {"content": "hello"})
    await asyncio.wait_for(response({"type": "http"}, asgi.receive, asgi.send), 5)

    errors = [f for f in asgi.frames if f["type"] == "error"]
    assert errors, "the send did not end on its error"
    assert all(_RAW_ERROR not in str(f.get("content")) for f in errors)
    assert errors[-1]["content"] == chat_module._GENERIC_ERROR_MESSAGE


def _marker_refreshers() -> list[asyncio.Task]:
    """The live tasks refreshing a turn's processing marker."""
    return [
        t for t in asyncio.all_tasks()
        if not t.done() and getattr(t.get_coro(), "__name__", "") == "_refresh_turn_marker"
    ]


async def test_a_send_that_fails_after_its_turn_started_stops_refreshing_the_turn_marker(web_env, monkeypatch):
    """The marker refresh starts with the turn and loops until stopped: a send
    that ends on an error (here, its reply cannot be stored) stops it too, or it
    would wake every 20 s for the life of the worker."""
    asgi = _Asgi()
    turn_running = asyncio.Event()

    async def _run(_db, _uid, _history, _content, **kwargs):
        turn_running.set()
        return ("an answer", None)

    store = chat_module.store_message

    async def _store_fails(db, conv_id, role, content, **kwargs):
        if role == "assistant":
            raise RuntimeError("the database is gone")
        return await store(db, conv_id, role, content, **kwargs)

    monkeypatch.setattr(chat_module, "store_message", _store_fails)
    _patch_general_chat(monkeypatch, _run)
    before = set(_marker_refreshers())
    _connection, response = _stream(web_env, {"content": "long job"})
    task = asyncio.create_task(response({"type": "http"}, asgi.receive, asgi.send))
    await asyncio.wait_for(turn_running.wait(), 3)
    await asyncio.wait_for(task, 5)
    await asyncio.sleep(0)

    assert any(f["type"] == "error" for f in asgi.frames), "the send did not end on its error"
    assert set(_marker_refreshers()) - before == set(), "the turn marker is still refreshed after the send ended"


@pytest.mark.parametrize("streamed", [["Half ", "an answer"], []])
async def test_a_stop_keeps_what_the_answer_had_written(web_env, monkeypatch, streamed):
    """What the current pass streamed before a Stop is stored as the answer, as
    the panel showed it; a pass the loop started over is not, and a Stop before
    any text stores the cancellation. Either way the turn ends ``cancelled``."""
    asgi = _Asgi()
    turn_running = asyncio.Event()

    async def _run(_db, _uid, _history, _content, **kwargs):
        await kwargs["on_event"]({"status": "thinking", "iteration": 1})
        await kwargs["on_token"]("a pass the loop started over ")
        await kwargs["on_event"]({"status": "thinking", "iteration": 2})
        for chunk in streamed:
            await kwargs["on_token"](chunk)
        turn_running.set()
        assert await _wait_until(kwargs["cancel_event"].is_set), "the Stop never reached the turn"
        return ("The request was cancelled.", None)

    _patch_general_chat(monkeypatch, _run)
    _connection, response = _stream(web_env, {"content": "long job"})
    task = asyncio.create_task(response({"type": "http"}, asgi.receive, asgi.send))
    await asyncio.wait_for(turn_running.wait(), 3)
    assert await _cancel(web_env) == {"status": "cancelled", "turn": "local"}
    await asyncio.wait_for(task, 5)

    assert _assistant_answers(web_env) == (["Half an answer"] if streamed else ["The request was cancelled."])
    assert asgi.frames[-1]["type"] == "cancelled"


@pytest.mark.parametrize("where", ["local", "remote"])
async def test_stop_reaches_the_turn_on_this_replica_and_on_another(web_env, monkeypatch, where):
    asgi = _Asgi()
    turn_running = asyncio.Event()
    conversation_id = str(web_env.conv.id)

    async def _run(_db, _uid, _history, _content, **kwargs):
        turn_running.set()
        assert await _wait_until(kwargs["cancel_event"].is_set), "the Stop never reached the turn"
        return ("The request was cancelled.", None)

    _patch_general_chat(monkeypatch, _run)
    _connection, response = _stream(web_env, {"content": "long job"})
    task = asyncio.create_task(response({"type": "http"}, asgi.receive, asgi.send))
    await asyncio.wait_for(turn_running.wait(), 3)
    if where == "remote":
        monkeypatch.setattr(chat_web_transport, "live_connection", lambda _cid: None)
        monkeypatch.setattr(chat_web_stream, "live_connection", lambda _cid: None)

    db = SimpleNamespace(execute=_FakeSession(web_env.conv, []).execute)
    user = SimpleNamespace(id=web_env.user_id)
    answer = await chat_web_stream.cancel_web_chat_turn(
        chat_web_stream.WebChatCancelRequest(conversation_id=conversation_id), _request(), db=db, user=user
    )
    await asyncio.wait_for(task, 5)

    assert answer["turn"] == where
    assert web_env.stopped == [conversation_id]
    assert asgi.frames[-1]["type"] == "cancelled"


async def test_a_stop_while_the_answer_is_stored_stops_the_background_work_and_keeps_the_answer(
    web_env, monkeypatch
):
    """No pass reads the stream's inbox once the agent loop returned: the route
    itself records the Stop and stops the background work, once."""
    monkeypatch.setattr(chat_module, "_stop_conversation_work", _REAL_STOP_CONVERSATION_WORK)
    cascaded: list = []
    monkeypatch.setattr(chat_module, "_spawn_cancel_cascade", lambda cid, stopped_at=None: cascaded.append(cid))
    conversation_id = str(web_env.conv.id)
    storing, store_gate = asyncio.Event(), asyncio.Event()
    store = chat_module.store_message

    async def _store_held(db, conv_id, role, content, **kwargs):
        if role == "assistant":
            storing.set()
            await store_gate.wait()
        return await store(db, conv_id, role, content, **kwargs)

    monkeypatch.setattr(chat_module, "store_message", _store_held)

    async def _run(_db, _uid, _history, _content, **_kwargs):
        return ("the final answer", None)

    _patch_general_chat(monkeypatch, _run)
    asgi = _Asgi()
    connection, response = _stream(web_env, {"content": "hello"})
    task = asyncio.create_task(response({"type": "http"}, asgi.receive, asgi.send))
    try:
        await asyncio.wait_for(storing.wait(), 3)
        assert await _cancel(web_env) == {"status": "cancelled", "turn": "local"}
        assert await chat_turn_guard.recorded_stop(conversation_id) is not None
        assert cascaded == [conversation_id]
    finally:
        store_gate.set()
        await asyncio.wait_for(task, 5)

    assert _assistant_answers(web_env) == ["the final answer"]
    assert asgi.frames[-1]["type"] == "done"
    assert not any(f["type"] == "cancelled" for f in asgi.frames)
    assert cascaded == [conversation_id]
    assert connection._inbox.empty()


@pytest.mark.parametrize("where", ["local", "remote"])
async def test_continue_now_ends_the_wait_without_ending_the_turn(web_env, monkeypatch, where):
    asgi = _Asgi()
    turn_running = asyncio.Event()
    conversation_id = str(web_env.conv.id)

    async def _run(_db, _uid, _history, _content, **kwargs):
        turn_running.set()
        assert await _wait_until(kwargs["wait_skip_event"].is_set), "the skip never reached the wait"
        return ("carried on", None)

    _patch_general_chat(monkeypatch, _run)
    _connection, response = _stream(web_env, {"content": "wait for it"})
    task = asyncio.create_task(response({"type": "http"}, asgi.receive, asgi.send))
    await asyncio.wait_for(turn_running.wait(), 3)
    if where == "remote":
        monkeypatch.setattr(chat_web_stream, "live_connection", lambda _cid: None)

    db = SimpleNamespace(execute=_FakeSession(web_env.conv, []).execute)
    user = SimpleNamespace(id=web_env.user_id)
    answer = await chat_web_stream.skip_web_chat_wait(
        chat_web_stream.WebChatTurnRequest(conversation_id=conversation_id), db=db, user=user
    )
    await asyncio.wait_for(task, 5)

    assert answer["turn"] == where
    assert _assistant_answers(web_env) == ["carried on"]
    assert not any(f["type"] == "cancelled" for f in asgi.frames)


async def _cancel(env, client_id: str = "tab-1", *, awaiting_send: bool = False) -> dict:
    db = SimpleNamespace(execute=_FakeSession(env.conv, []).execute)
    return await chat_web_stream.cancel_web_chat_turn(
        chat_web_stream.WebChatCancelRequest(conversation_id=str(env.conv.id), awaiting_send=awaiting_send),
        _request(client_id),
        db=db,
        user=SimpleNamespace(id=env.user_id),
    )


def _cancel_seen_at_start(monkeypatch) -> list[bool]:
    """Patch the turn to say whether it was already stopped when it started."""
    seen: list[bool] = []

    async def _run(_db, _uid, _history, _content, **kwargs):
        seen.append(kwargs["cancel_event"].is_set())
        return ("The request was cancelled." if seen[-1] else "answered", None)

    _patch_general_chat(monkeypatch, _run)
    return seen


async def test_a_stop_sent_before_the_send_took_the_lock_ends_its_turn_as_it_starts(web_env, monkeypatch):
    seen = _cancel_seen_at_start(monkeypatch)
    assert await _cancel(web_env, "tab-1", awaiting_send=True) == {"status": "cancelled", "turn": "none"}
    # The package sends its Stop again until its turn starts: the same answer.
    assert await _cancel(web_env, "tab-1", awaiting_send=True) == {"status": "cancelled", "turn": "none"}

    asgi = _Asgi()
    _connection, response = _stream(web_env, {"content": "long job"}, origin="tab-1")
    await asyncio.wait_for(response({"type": "http"}, asgi.receive, asgi.send), 5)

    assert seen == [True]
    types = [f["type"] for f in asgi.frames]
    assert "turn_started" in types and types[-1] == "cancelled"
    assert asgi.frames[-1].get("message_id")
    kept = chat_turn_guard.pending_cancel_key(str(web_env.conv.id), "tab-1")
    assert kept not in web_env.fake_kv.kv


async def test_a_stop_with_no_send_on_its_way_keeps_nothing_and_the_next_send_runs(web_env, monkeypatch):
    """A Stop pressed as the answer finished finds no turn: the tab's next send is not stopped."""
    seen = _cancel_seen_at_start(monkeypatch)
    assert await _cancel(web_env, "tab-1") == {"status": "cancelled", "turn": "none"}
    kept = chat_turn_guard.pending_cancel_key(str(web_env.conv.id), "tab-1")
    assert kept not in web_env.fake_kv.kv

    asgi = _Asgi()
    _connection, response = _stream(web_env, {"content": "next question"}, origin="tab-1")
    await asyncio.wait_for(response({"type": "http"}, asgi.receive, asgi.send), 5)

    assert seen == [False]
    assert asgi.frames[-1]["type"] == "done"


async def test_a_stop_with_no_send_on_its_way_drops_a_stop_kept_before(web_env):
    await _cancel(web_env, "tab-1", awaiting_send=True)
    kept = chat_turn_guard.pending_cancel_key(str(web_env.conv.id), "tab-1")
    assert kept in web_env.fake_kv.kv

    assert (await _cancel(web_env, "tab-1"))["turn"] == "none"
    assert kept not in web_env.fake_kv.kv


async def test_a_stop_kept_for_another_tab_does_not_stop_this_turn(web_env, monkeypatch):
    seen = _cancel_seen_at_start(monkeypatch)
    assert (await _cancel(web_env, "tab-2", awaiting_send=True))["turn"] == "none"

    asgi = _Asgi()
    _connection, response = _stream(web_env, {"content": "long job"}, origin="tab-1")
    await asyncio.wait_for(response({"type": "http"}, asgi.receive, asgi.send), 5)

    assert seen == [False]
    assert asgi.frames[-1]["type"] == "done"
    assert chat_turn_guard.pending_cancel_key(str(web_env.conv.id), "tab-2") in web_env.fake_kv.kv


async def test_a_kept_stop_is_short_lived_and_an_expired_one_stops_nothing(web_env, monkeypatch):
    seen = _cancel_seen_at_start(monkeypatch)
    await _cancel(web_env, "tab-1", awaiting_send=True)
    kept = chat_turn_guard.pending_cancel_key(str(web_env.conv.id), "tab-1")
    assert web_env.fake_kv.ttls[kept] == chat_turn_guard.PENDING_CANCEL_TTL_SECONDS <= 30
    web_env.fake_kv.kv.pop(kept)  # expired

    asgi = _Asgi()
    _connection, response = _stream(web_env, {"content": "long job"}, origin="tab-1")
    await asyncio.wait_for(response({"type": "http"}, asgi.receive, asgi.send), 5)

    assert seen == [False]
    assert asgi.frames[-1]["type"] == "done"


async def test_a_folded_send_drops_its_kept_stop_without_stopping_the_turn_it_joined(web_env, monkeypatch):
    conversation_id = str(web_env.conv.id)
    await _cancel(web_env, "tab-1", awaiting_send=True)
    kept = chat_turn_guard.pending_cancel_key(conversation_id, "tab-1")
    await web_env.fake_kv.set(chat_turn_guard.processing_key(conversation_id), "turn-a")

    async def _relay(conv_id, *, origin, send, **_kw):
        await send({"type": "done", "content": "final", "message_id": "m1", "relayed": True})
        await asyncio.Event().wait()

    monkeypatch.setattr(chat_turn_stream, "relay_turn_frames", _relay)
    asgi = _Asgi()
    _connection, response = _stream(web_env, {"content": "and this too"}, origin="tab-1")
    await asyncio.wait_for(response({"type": "http"}, asgi.receive, asgi.send), 5)

    # The turn it folded into is another's: the kept Stop does not stop it, and
    # with no turn of its own to wait for, it would stop the tab's next send.
    assert kept not in web_env.fake_kv.kv
    assert not await chat_turn_guard.consume_turn_cancel(conversation_id, "turn-a")
    # The package's Stop sent again reaches that turn.
    assert (await _cancel(web_env, "tab-1"))["turn"] == "remote"
    assert await chat_turn_guard.consume_turn_cancel(conversation_id, "turn-a")


async def test_a_finished_turn_waiting_on_its_title_leaves_the_next_turn_to_redis(web_env, monkeypatch):
    """Its connection is off this replica's routes once the lock is released: Stop,
    "Continue now" and steering reach a next turn running elsewhere."""
    conversation_id = str(web_env.conv.id)
    web_env.conv.title = "New conversation"
    title_gate = asyncio.Event()

    async def _title(**_kwargs):
        await title_gate.wait()

    monkeypatch.setattr(chat_module, "_generate_title_after_done", _title)

    async def _run(_db, _uid, _history, _content, **_kwargs):
        return ("first answer", None)

    _patch_general_chat(monkeypatch, _run)
    asgi = _Asgi()
    _connection, response = _stream(web_env, {"content": "hello"})
    task = asyncio.create_task(response({"type": "http"}, asgi.receive, asgi.send))
    try:
        assert await _wait_until(lambda: any(f["type"] == "done" for f in asgi.frames))
        assert not task.done()
        assert chat_web_transport.live_connection(conversation_id) is None

        # The next turn runs on another replica.
        await web_env.fake_kv.set(chat_turn_guard.processing_key(conversation_id), "turn-b")
        assert (await _cancel(web_env))["turn"] == "remote"
        assert await chat_turn_guard.consume_turn_cancel(conversation_id, "turn-b")
        db = SimpleNamespace(execute=_FakeSession(web_env.conv, []).execute)
        skipped = await chat_web_stream.skip_web_chat_wait(
            chat_web_stream.WebChatTurnRequest(conversation_id=conversation_id),
            db=db,
            user=SimpleNamespace(id=web_env.user_id),
        )
        assert skipped["turn"] == "remote"
        steered = await chat_web_stream.steer_web_chat_message(
            _steer_request(web_env), _request(), db=db, user=SimpleNamespace(id=web_env.user_id)
        )
        assert steered["turn_id"] == chat_turn_stream.public_turn_id("turn-b")
    finally:
        title_gate.set()
        await asyncio.wait_for(task, 5)


async def test_a_skip_wait_request_names_the_turn_it_is_for(web_env):
    conversation_id = str(web_env.conv.id)
    assert await chat_turn_guard.request_wait_skip(conversation_id) is False
    await web_env.fake_kv.set(chat_turn_guard.processing_key(conversation_id), "turn-a")
    assert await chat_turn_guard.request_wait_skip(conversation_id) is True
    assert await chat_turn_guard.consume_wait_skip(conversation_id, "turn-b") is False
    assert await chat_turn_guard.consume_wait_skip(conversation_id, "turn-a") is False
    await chat_turn_guard.request_wait_skip(conversation_id)
    assert await chat_turn_guard.consume_wait_skip(conversation_id, "turn-a") is True


def _steer_request(env, content: str = "also check the logs"):
    return chat_web_stream.WebChatSteerRequest(
        conversation_id=str(env.conv.id), content=content, steer_id="bubble-1"
    )


def _request(client_id: str = "tab-1"):
    return SimpleNamespace(headers={chat_web_stream.CLIENT_ID_HEADER: client_id})


async def test_steering_with_no_turn_running_is_409_and_stores_nothing(web_env):
    db = SimpleNamespace(execute=_FakeSession(web_env.conv, []).execute)
    with pytest.raises(HTTPException) as exc:
        await chat_web_stream.steer_web_chat_message(
            _steer_request(web_env), _request(), db=db, user=SimpleNamespace(id=web_env.user_id)
        )
    assert exc.value.status_code == 409
    assert web_env.stored == []


async def test_steering_a_running_turn_stores_and_hands_it_over(web_env):
    conversation_id = str(web_env.conv.id)
    await web_env.fake_kv.set(chat_turn_guard.processing_key(conversation_id), "turn-a")
    db = SimpleNamespace(execute=_FakeSession(web_env.conv, []).execute)

    answer = await chat_web_stream.steer_web_chat_message(
        _steer_request(web_env), _request(), db=db, user=SimpleNamespace(id=web_env.user_id)
    )

    assert answer == {
        "status": "steered",
        "steer_id": "bubble-1",
        "turn_id": chat_turn_stream.public_turn_id("turn-a"),
    }
    assert web_env.stored == [("user", "also check the logs")]
    payloads = await chat_turn_guard.drain_steering_inbox(conversation_id)
    assert [p.text for p in payloads] == ["also check the logs"]


async def test_a_send_folded_into_a_running_turn_relays_it_to_its_end(web_env, monkeypatch):
    conversation_id = str(web_env.conv.id)
    await web_env.fake_kv.set(chat_turn_guard.processing_key(conversation_id), "turn-a")
    turn_id = chat_turn_stream.public_turn_id("turn-a")
    relayed_for: list[str] = []

    async def _relay(conv_id, *, origin, send, **_kw):
        relayed_for.append(origin)
        for frame in (
            {"type": "stream", "content": "partial", "turn_id": turn_id, "relayed": True},
            {"type": "done", "content": "x", "intermediate": True, "turn_id": turn_id, "relayed": True},
            {"type": "done", "content": "final", "message_id": "m1", "turn_id": turn_id, "relayed": True},
            {"type": "stream", "content": "never sent", "turn_id": turn_id, "relayed": True},
        ):
            if not await send(frame):
                return
        await asyncio.Event().wait()

    monkeypatch.setattr(chat_turn_stream, "relay_turn_frames", _relay)
    asgi = _Asgi()
    _connection, response = _stream(web_env, {"content": "and this too"}, origin="tab-2")
    await asyncio.wait_for(response({"type": "http"}, asgi.receive, asgi.send), 5)

    types = [(f["type"], f.get("content")) for f in asgi.frames]
    assert ("folded_into_active_turn", None) in types
    assert types[-1] == ("done", "final")
    assert ("stream", "never sent") not in types
    assert relayed_for == ["tab-2"]
    assert asgi.completed


async def test_the_events_stream_follows_the_conversation_as_the_callers_tab(web_env, monkeypatch):
    origins: list[str] = []

    async def _relay(conv_id, *, origin, send, **_kw):
        origins.append(origin)
        await send({"type": "user_message", "content": "from my phone"})
        await send({"type": "conversation_title", "title": "Logs"})

    monkeypatch.setattr(chat_turn_stream, "relay_turn_frames", _relay)

    async def _release(_db):
        return None

    monkeypatch.setattr(chat_web_stream, "release_db_connection_before_loop", _release)
    db = SimpleNamespace(execute=_FakeSession(web_env.conv, []).execute)
    user = SimpleNamespace(id=web_env.user_id, web_session=SimpleNamespace())
    response = await chat_web_stream.stream_web_chat_events(
        str(web_env.conv.id), _request(), client_id="tab-9", db=db, user=user
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert origins == ["tab-9"]
    frames = [json.loads(c[6:]) for c in (b"".join(chunks).decode().split("\n\n")) if c.startswith("data: ")]
    assert [f["type"] for f in frames] == ["user_message", "conversation_title"]


async def test_a_session_that_ends_closes_the_stream_and_not_the_turn(web_env, monkeypatch):
    asgi = _Asgi()
    monkeypatch.setattr(chat_module, "SESSION_RECHECK_SECONDS", 0.01)

    checks: list[object] = []

    async def _ended(session):
        # The send's own check passes; the watch that follows ends the session.
        checks.append(session)
        if len(checks) > 1:
            raise SessionEndedError("idle")
        return session

    monkeypatch.setattr(chat_module, "recheck_session", _ended)

    async def _run(_db, _uid, _history, _content, **_kwargs):
        assert await _wait_until(lambda: asgi.completed), "the stream was not closed"
        return ("stored anyway", None)

    _patch_general_chat(monkeypatch, _run)
    _connection, response = _stream(web_env, {"content": "hello"})
    await asyncio.wait_for(response({"type": "http"}, asgi.receive, asgi.send), 5)

    assert _assistant_answers(web_env) == ["stored anyway"]
    expired = [f for f in asgi.frames if f["type"] == "session_expired"]
    assert expired == [{"type": "session_expired", "reason": "idle"}]
    assert asgi.frames[-1]["type"] == "session_expired"


@pytest.mark.parametrize("reason", ["idle", "lifetime"])
async def test_a_session_that_ends_ends_the_events_stream(web_env, monkeypatch, reason):
    # Short against the 15 s keep-alive: the stream must wake for the check itself.
    monkeypatch.setattr(chat_web_stream, "SESSION_RECHECK_SECONDS", 0.05)
    checks: list[object] = []

    async def _ends_on_second_check(session):
        checks.append(session)
        if len(checks) > 1:
            raise SessionEndedError(reason)
        return session

    monkeypatch.setattr(chat_web_stream, "recheck_session", _ends_on_second_check)
    relay_stopped = asyncio.Event()

    async def _relay(conv_id, *, origin, send, **_kw):
        await send({"type": "user_message", "content": "from my phone"})
        try:
            await asyncio.Event().wait()
        finally:
            relay_stopped.set()

    async def _release(_db):
        return None

    monkeypatch.setattr(chat_turn_stream, "relay_turn_frames", _relay)
    monkeypatch.setattr(chat_web_stream, "release_db_connection_before_loop", _release)
    response = await chat_web_stream.stream_web_chat_events(
        str(web_env.conv.id),
        _request(),
        client_id="tab-1",
        db=SimpleNamespace(execute=_FakeSession(web_env.conv, []).execute),
        user=SimpleNamespace(id=web_env.user_id, web_session=SimpleNamespace()),
    )

    async def _read() -> list[bytes]:
        return [chunk async for chunk in response.body_iterator]

    chunks = await asyncio.wait_for(_read(), 2)

    frames = [json.loads(c[6:]) for c in b"".join(chunks).decode().split("\n\n") if c.startswith("data: ")]
    assert frames == [
        {"type": "user_message", "content": "from my phone"},
        {"type": "session_expired", "reason": reason},
    ]
    assert len(checks) == 2
    assert relay_stopped.is_set(), "the stream ended and left its relay running"


async def test_the_admission_limiter_knows_the_web_streams_by_method_and_path():
    from app.main import ConcurrencyLimitMiddleware

    stream = ConcurrencyLimitMiddleware._is_long_lived_stream
    assert stream("POST", "/api/v1/web/chat/messages")
    assert stream("GET", f"/api/v1/web/chat/conversations/{uuid.uuid4()}/events")
    assert not stream("GET", "/api/v1/web/chat/messages")
    assert not stream("POST", f"/api/v1/web/chat/conversations/{uuid.uuid4()}/events")
    assert not stream("POST", "/api/v1/web/chat/messages/steer")
    assert not stream("GET", "/api/v1/web/chat/sessions")


class _PageDb:
    """The conversation, then the cursor message, then the page rows, in query order."""

    def __init__(self, conv, rows, anchor=None):
        self._answers = [conv, *([anchor] if anchor is not None else []), rows]

    async def execute(self, _stmt):
        answer = self._answers.pop(0)

        class _R:
            def scalar_one_or_none(self):
                return answer

            def scalars(self):
                return SimpleNamespace(all=lambda: list(answer))

        return _R()


def _message(role: str, content: str, minute: int):
    from datetime import UTC, datetime

    return SimpleNamespace(
        id=uuid.uuid4(),
        role=role,
        content=content,
        created_at=datetime(2026, 10, 20, 12, minute, tzinfo=UTC),
        metadata_json=None,
        sequence=minute,
    )


@pytest.fixture
def page_env(monkeypatch):
    from app.api.v1 import platform as platform_module

    async def _no_feedback(_db, _ids, _uid):
        return {}

    async def _no_artifacts(*_a, **_kw):
        return None

    monkeypatch.setattr(platform_module, "load_user_feedback_by_message", _no_feedback)
    monkeypatch.setattr(platform_module, "_present_restored_artifacts", _no_artifacts)
    user_id = uuid.uuid4()
    return SimpleNamespace(user=SimpleNamespace(id=user_id), conv=SimpleNamespace(id=uuid.uuid4(), user_id=user_id))


async def test_a_page_of_messages_is_restore_shaped_and_says_whether_more_exist(page_env):
    newest_first = [_message("assistant", "a2", 4), _message("user", "q2", 3), _message("assistant", "a1", 2)]

    page = await chat_web_stream.list_web_chat_messages(
        str(page_env.conv.id),
        before=str(uuid.uuid4()),
        limit=2,
        supports_artifacts=False,
        user=page_env.user,
        db=_PageDb(page_env.conv, newest_first, anchor=_message("user", "q3", 5)),
    )

    assert page["has_more"] is True
    assert [(m["role"], m["content"]) for m in page["messages"]] == [("user", "q2"), ("assistant", "a2")]
    assert set(page["messages"][0]) == {"id", "role", "content", "created_at"}


async def test_a_page_after_an_unknown_cursor_is_404(page_env):
    with pytest.raises(HTTPException) as exc:
        await chat_web_stream.list_web_chat_messages(
            str(page_env.conv.id),
            before=str(uuid.uuid4()),
            limit=50,
            supports_artifacts=False,
            user=page_env.user,
            db=_CursorMissingDb(page_env.conv),
        )
    assert exc.value.status_code == 404


class _CursorMissingDb(_PageDb):
    def __init__(self, conv):
        self._answers = [conv, None]


async def test_the_events_stream_reads_the_client_id_from_the_header_too(web_env, monkeypatch):
    origins: list[str] = []

    async def _relay(conv_id, *, origin, send, **_kw):
        origins.append(origin)

    async def _release(_db):
        return None

    monkeypatch.setattr(chat_turn_stream, "relay_turn_frames", _relay)
    monkeypatch.setattr(chat_web_stream, "release_db_connection_before_loop", _release)
    response = await chat_web_stream.stream_web_chat_events(
        str(web_env.conv.id),
        _request("tab-from-header"),
        client_id=None,
        db=SimpleNamespace(execute=_FakeSession(web_env.conv, []).execute),
        user=SimpleNamespace(id=web_env.user_id, web_session=SimpleNamespace()),
    )
    _ = [chunk async for chunk in response.body_iterator]
    assert origins == ["tab-from-header"]


@pytest.mark.parametrize(("stored", "has_more"), [(50, False), (51, True)])
async def test_a_restore_says_whether_older_messages_exist(page_env, monkeypatch, stored, has_more):
    from app.api.v1 import platform as platform_module
    from app.services import general_assistant_link

    async def _no_stand_in(_db, **_kwargs):
        return None

    monkeypatch.setattr(general_assistant_link, "turn_stand_in", _no_stand_in)

    conv = SimpleNamespace(
        id=page_env.conv.id, user_id=page_env.user.id, agent_id=None, original_agent_id=None, workspace_id=None, channel="web"
    )
    newest_first = [_message("user" if n % 2 else "assistant", f"m{n}", n % 60) for n in range(stored, 0, -1)]
    body = chat_web_stream.WebChatSessionRequest(conversation_id=str(conv.id))

    restored = await chat_web_stream.open_web_chat_session(
        body, db=_PageDb(conv, newest_first), user=page_env.user
    )
    platform_restored, _has_more = await platform_module.restore_chat_session_window(
        _RowsDb(newest_first), page_env.user, conv, supports_artifacts=False
    )

    assert restored.has_more is has_more
    assert len(restored.messages) == 50
    turn_state = {"has_more", "is_processing", "pending_approvals", "interrupted_turn"}
    # The web chat names who answers a conversation without an agent; the
    # platform route, which answers with the agent asked for, names none.
    assert restored.agent_name == "General Assistant" and platform_restored.agent_name is None
    assert restored.model_dump(exclude=turn_state | {"agent_name"}) == platform_restored.model_dump(exclude={"agent_name"})
    assert "has_more" not in platform_restored.model_dump()


class _RowsDb:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        rows = self._rows

        class _R:
            def scalars(self):
                return SimpleNamespace(all=lambda: list(rows))

        return _R()


def _shared_with_caller(monkeypatch, conv):
    async def _readable(conversation_id, user, db):
        assert conversation_id == str(conv.id)
        return conv, chat_module.CONVERSATION_ACCESS_READ

    async def _no_new_conversation(**_kw):
        raise AssertionError("a conversation shared with the caller is read, never replaced")

    monkeypatch.setattr(chat_module, "resolve_conversation_for_read", _readable)
    monkeypatch.setattr(chat_module, "create_conversation", _no_new_conversation)


async def test_a_restore_reads_a_conversation_shared_with_the_caller(page_env, monkeypatch):
    owner_id = uuid.uuid4()
    conv = SimpleNamespace(id=uuid.uuid4(), user_id=owner_id, agent_id=None, original_agent_id=None, workspace_id=None, channel="web")
    _shared_with_caller(monkeypatch, conv)
    rows = [_message("assistant", "a1", 2), _message("user", "q1", 1)]

    restored = await chat_web_stream.open_web_chat_session(
        chat_web_stream.WebChatSessionRequest(conversation_id=str(conv.id)),
        db=_RowsDb(rows),
        user=page_env.user,
    )

    assert restored.conversation_id == str(conv.id)
    assert [(m["role"], m["content"]) for m in restored.messages] == [("user", "q1"), ("assistant", "a1")]


async def test_older_messages_of_a_conversation_shared_with_the_caller_page_in(page_env, monkeypatch):
    conv = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())
    _shared_with_caller(monkeypatch, conv)

    page = await chat_web_stream.list_web_chat_messages(
        str(conv.id),
        before=None,
        limit=50,
        supports_artifacts=False,
        user=page_env.user,
        db=_RowsDb([_message("user", "q1", 1)]),
    )

    assert [(m["role"], m["content"]) for m in page["messages"]] == [("user", "q1")]
    assert page["has_more"] is False
