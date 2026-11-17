# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""HTTP + SSE transport of the native web chat (``/api/v1/web``).

One ``POST /chat/messages`` serves one send through the connection runner
(``chat.run_chat_connection``), over a :class:`HeadlessTurnConnection`: the
send frame is its first inbound frame, the cancel / skip-wait frames a route
injects on this replica follow, and every outbound frame is written as an SSE
``data:`` line.

The response starts with the first frame past the send's ``thinking`` ack: a
send refused before its turn starts is answered with an HTTP error and
its ``detail`` instead of a stream. No keep-alive is written before that
first frame, so nothing but the send's own outcome starts the response (and
gives back the admission limiter's slot, which it holds until then). A
pre-turn phase that outlasts a reverse proxy's response-header timeout
(nginx's ``proxy_read_timeout``, 60 s by default) is cut by the proxy rather
than turned into a ``200`` stream that carries a refusal as a frame; that
phase normally takes well under a second.

Every frame of the turn names it (``turn_id``): the stream's turn is the one
its ``turn_started`` or ``folded_into_active_turn`` names, and the frames
written before that one, the ack included, are named on the way out.

:class:`DetachedTurnStreamResponse` runs the connection inside the request's
own ASGI task, so uvicorn's graceful drain cancels the turn (its cut is
stored). A client that goes away only detaches the stream: the turn runs on
and stores its answer.

A write the client does not take within :data:`STREAM_WRITE_TIMEOUT_SECONDS`
(it stopped reading, with no FIN) ends either stream: the client reconnects
and resyncs from the turn's snapshot. The response is still completed, by a
last write that waits on the client up to :data:`STREAM_ABANDON_SECONDS`: the
client reads the end of the stream once it reads again, or the server sees it
gone. The ASGI server has no way to abandon a started response quietly: one
the application returns without completing, or raises out of, is logged as a
server error. So only a client that still reads nothing past that bound has
its response given up: the server logs it and closes the connection, and the
request holds nothing longer.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from starlette.responses import Response, StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from app.core.sessions import SESSION_EXPIRED_CODE, SessionEndReason
from app.services.chat_turn_stream import NON_TURN_FRAME_TYPES

if TYPE_CHECKING:
    from app.services.chat_turn_guard import SteeringPayload

logger = logging.getLogger(__name__)

#: Idle time after which a stream writes an SSE comment, so proxies keep it open.
SSE_KEEPALIVE_SECONDS = 15.0
SSE_KEEPALIVE = b": keepalive\n\n"
#: How long one write may wait on a client that reads nothing before the
#: stream ends.
STREAM_WRITE_TIMEOUT_SECONDS = 10.0
#: How long the last write of a stream whose client stopped reading waits on
#: it before the response is given up and its connection closed.
STREAM_ABANDON_SECONDS = 60.0
SSE_HEADERS: list[tuple[bytes, bytes]] = [
    (b"content-type", b"text/event-stream; charset=utf-8"),
    # no-transform: an intermediary must not compress (and so buffer) the stream.
    (b"cache-control", b"no-cache, no-transform"),
    (b"x-accel-buffering", b"no"),
]
#: Frames a send writes before it knows whether its turn starts: held until
#: the stream starts, dropped when the send is refused.
_PRE_TURN_FRAME_TYPES = frozenset({"thinking"})
#: Frames that name the stream's turn for the frames around them.
_TURN_NAMING_FRAME_TYPES = frozenset({"turn_started", "folded_into_active_turn"})
#: Frames a send stream writes that belong to no turn.
_UNNAMED_FRAME_TYPES = NON_TURN_FRAME_TYPES | {SESSION_EXPIRED_CODE}


def sse_data(frame: dict) -> bytes:
    """*frame* as one SSE ``data:`` event."""
    return f"data: {json.dumps(frame, default=str, ensure_ascii=False)}\n\n".encode()


def keepalive_delay(last_write: float, now: float) -> float:
    """Seconds until a stream last written at *last_write* owes its keep-alive (0 when due).

    The one rule of both web chat streams: a keep-alive at most
    :data:`SSE_KEEPALIVE_SECONDS` after the last thing written.
    """
    return max(0.0, last_write + SSE_KEEPALIVE_SECONDS - now)


class StreamConsumerGoneError(RuntimeError):
    """Nobody reads the stream any more; the connection runner reads it as a client gone."""


@dataclass(frozen=True)
class SendRefusal:
    """A send refused before its turn started, answered as an HTTP error."""

    status_code: int
    detail: str
    headers: Mapping[str, str] = field(default_factory=dict)


class HeadlessTurnConnection:
    """A chat connection that serves exactly one send frame over an SSE stream."""

    def __init__(self, conversation_id: str, frame: dict) -> None:
        self.conversation_id = conversation_id
        self._inbox: asyncio.Queue[dict] = asyncio.Queue()
        self._inbox.put_nowait(frame)
        self._send: Send | None = None
        self._write_lock = asyncio.Lock()
        self._detached = False
        self._ended = False
        self._started = False
        #: A write timed out: the client stopped reading, the response is still open.
        self._stalled = False
        self._held: list[dict] = []
        self.last_write = 0.0
        #: The turn this connection runs, once the runner said it started one.
        self.turn_id: str | None = None
        #: The turn this stream's frames belong to: the one it runs, or the
        #: one its send folded into.
        self.stream_turn_id: str | None = None
        #: Set when the send was refused before the stream started.
        self.refusal: SendRefusal | None = None
        self._steer_handoff: Callable[[SteeringPayload], None] | None = None
        self._steering_closed = False
        self._released = False

    # ── ChatConnection ─────────────────────────────────────────────────
    async def send_json(self, data: dict) -> None:
        kind = data.get("type")
        if kind in _TURN_NAMING_FRAME_TYPES and not data.get("relayed"):
            self.stream_turn_id = data.get("turn_id") or self.stream_turn_id
            if kind == "turn_started":
                self.turn_id = data.get("turn_id")
                register(self)
        await self._write(data, hold=kind in _PRE_TURN_FRAME_TYPES)

    async def receive_json(self) -> Any:
        return await self._inbox.get()

    async def close(self, *, reason: str = "") -> None:
        await self._end_with({"type": "error", "content": reason or "The connection was closed."})

    async def end_session(self, reason: SessionEndReason) -> None:
        await self._end_with({"type": SESSION_EXPIRED_CODE, "reason": reason})

    async def end_app_locked(self) -> None:
        await self._end_with({"type": APP_LOCKED_FRAME_TYPE})

    async def refuse(self, detail: str, *, status_code: int, headers: Mapping[str, str] | None = None) -> None:
        """Refuse the send before its turn: an HTTP error, or an ``error`` frame once the stream started."""
        async with self._write_lock:
            if not self._started and not self._ended:
                self.refusal = SendRefusal(status_code, detail, dict(headers or {}))
                self._held.clear()
        if self.refusal is None:
            await self._end_with({"type": "error", "content": detail})
        else:
            await self.end_stream()

    def accept_steering(self, handoff: Callable[[SteeringPayload], None] | None) -> None:
        self._steer_handoff = handoff
        self._steering_closed = handoff is None

    def turn_released(self) -> None:
        unregister(self)
        # No pass reads the inbox any more.
        self._released = True
        while not self._inbox.empty():
            self._inbox.get_nowait()

    async def _end_with(self, frame: dict) -> None:
        with contextlib.suppress(StreamConsumerGoneError):
            await self._write(frame)
        await self.end_stream()

    def client_present(self) -> bool:
        return not self._detached

    # ── Transport ──────────────────────────────────────────────────────
    def inject(self, frame: dict) -> None:
        """Hand the runner a ``cancel`` or ``skip_wait`` frame, after the send.

        The turn's running or next pass reads it; one no pass read is dropped
        when the turn releases its lock.
        """
        if not self._released:
            self._inbox.put_nowait(frame)

    def steer(self, payload: SteeringPayload) -> str | None:
        """Hand a stored steering message to the running turn: its id, or ``None`` when it takes no more."""
        if self._steer_handoff is None:
            return None
        self._steer_handoff(payload)
        return self.turn_id

    @property
    def accepts_steering(self) -> bool:
        return self._steer_handoff is not None

    @property
    def steering_closed(self) -> bool:
        """The turn reads no more steering: one folded through the inbox now would go unanswered."""
        return self._steering_closed

    def bind(self, send: Send) -> None:
        self._send = send

    def detach(self) -> None:
        """The client went away: frames are dropped from now on, the turn runs on."""
        self._detached = True

    @property
    def detached(self) -> bool:
        return self._detached

    async def _start(self, send: Send) -> None:
        """Start the SSE response with what was held; under ``_write_lock``."""
        if self._started:
            return
        await send({"type": "http.response.start", "status": 200, "headers": SSE_HEADERS})
        self._started = True
        if self._held:
            held, self._held = self._held, []
            body = b"".join(self._encoded(frame) for frame in held)
            await send({"type": "http.response.body", "body": body, "more_body": True})

    def _encoded(self, frame: dict) -> bytes:
        """*frame* as written: a frame of the stream's turn names it."""
        if self.stream_turn_id and "turn_id" not in frame and frame.get("type") not in _UNNAMED_FRAME_TYPES:
            frame = {**frame, "turn_id": self.stream_turn_id}
        return sse_data(frame)

    async def _write(self, data: dict | bytes, *, hold: bool = False) -> None:
        """Write a frame, or raw SSE bytes; *hold* keeps a frame until the stream starts."""
        send = self._send
        if self._detached or self._ended or send is None:
            raise StreamConsumerGoneError("stream consumer gone")
        async with self._write_lock:
            if self._detached or self._ended:
                raise StreamConsumerGoneError("stream consumer gone")
            try:
                if hold and not self._started and isinstance(data, dict):
                    self._held.append(data)
                else:
                    async with asyncio.timeout(STREAM_WRITE_TIMEOUT_SECONDS):
                        await self._start(send)
                        body = data if isinstance(data, bytes) else self._encoded(data)
                        await send({"type": "http.response.body", "body": body, "more_body": True})
            except Exception as exc:
                self._detached = True
                self._stalled = isinstance(exc, TimeoutError)
                raise StreamConsumerGoneError("stream consumer gone") from exc
            self.last_write = asyncio.get_running_loop().time()

    async def keepalive(self) -> bool:
        """Write a keep-alive comment; ``False`` once nothing can be written any more.

        Before the response starts nothing is written: a refusal must still
        be able to answer as HTTP (see the module docstring). The keep-alive
        clock restarts instead, so the next one is owed an interval later.
        """
        if not self._started:
            if self._detached or self._ended:
                return False
            self.last_write = asyncio.get_running_loop().time()
            return True
        try:
            await self._write(SSE_KEEPALIVE)
        except StreamConsumerGoneError:
            return False
        return True

    async def end_stream(self) -> None:
        """Complete the HTTP response; the connection keeps running detached."""
        async with self._write_lock:
            if self._ended:
                return
            self._ended = True
            send = self._send
            if self._detached or send is None:
                self._detached = True
                return
            try:
                async with asyncio.timeout(STREAM_WRITE_TIMEOUT_SECONDS):
                    if self.refusal is not None:
                        await _send_refusal(send, self.refusal)
                    else:
                        await self._start(send)
                        await send({"type": "http.response.body", "body": b"", "more_body": False})
            except TimeoutError:
                # Left to :meth:`complete`.
                self._stalled = self.refusal is None
            except Exception:
                logger.debug("The web chat stream could not be completed", exc_info=True)
        self._detached = True

    async def complete(self) -> None:
        """End the stream, and complete the response of a client that stopped reading.

        That last write waits on the client up to :data:`STREAM_ABANDON_SECONDS`
        (see the module docstring); the turn has detached from the stream by then.
        """
        await self.end_stream()
        send = self._send
        if not self._stalled or send is None:
            return
        self._stalled = False
        try:
            async with asyncio.timeout(STREAM_ABANDON_SECONDS):
                await self._start(send)
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except TimeoutError:
            _log_abandoned()
        except Exception:
            logger.debug("The web chat stream could not be completed", exc_info=True)


def _log_abandoned() -> None:
    logger.info(
        "A web chat stream's client read nothing for %.0f s: its response is given up and its connection closed",
        STREAM_ABANDON_SECONDS,
    )


async def _send_refusal(send: Send, refusal: SendRefusal) -> None:
    """*refusal* as a whole JSON response, the shape of an ``HTTPException``'s."""
    body = json.dumps({"detail": refusal.detail}, ensure_ascii=False).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        *((name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in refusal.headers.items()),
    ]
    await send({"type": "http.response.start", "status": refusal.status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body, "more_body": False})


# ── Same-replica registry ──────────────────────────────────────────────
_live: dict[str, HeadlessTurnConnection] = {}


def register(connection: HeadlessTurnConnection) -> None:
    _live[connection.conversation_id] = connection


def unregister(connection: HeadlessTurnConnection) -> None:
    if _live.get(connection.conversation_id) is connection:
        del _live[connection.conversation_id]


def live_connection(conversation_id: str) -> HeadlessTurnConnection | None:
    """The stream running *conversation_id*'s turn on this process, if any."""
    return _live.get(conversation_id)


class DetachedTurnStreamResponse(Response):
    """An SSE response whose producer outlives its client.

    Raw ASGI rather than ``StreamingResponse``, which cancels its producer on
    ``http.disconnect``: here a disconnect only detaches the connection. The
    producer runs in the request task, so the graceful drain still reaches it.
    """

    media_type = "text/event-stream"

    def __init__(
        self,
        connection: HeadlessTurnConnection,
        run: Callable[[HeadlessTurnConnection], Awaitable[None]],
    ) -> None:
        super().__init__(status_code=200)
        self.connection = connection
        self._run = run

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        connection = self.connection
        connection.bind(send)
        connection.last_write = asyncio.get_running_loop().time()
        watcher = asyncio.create_task(self._watch_disconnect(receive), name="chat-web-stream-disconnect")
        ticker = asyncio.create_task(self._keepalive(), name="chat-web-stream-keepalive")
        try:
            await self._run(connection)
        finally:
            unregister(connection)
            for task in (watcher, ticker):
                task.cancel()
            for task in (watcher, ticker):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        await connection.complete()

    async def _watch_disconnect(self, receive: Receive) -> None:
        while True:
            message: Message = await receive()
            if message["type"] == "http.disconnect":
                self.connection.detach()
                return

    async def _keepalive(self) -> None:
        connection = self.connection
        loop = asyncio.get_running_loop()
        while not connection.detached:
            delay = keepalive_delay(connection.last_write, loop.time())
            if delay > 0:
                await asyncio.sleep(delay)
            elif not await connection.keepalive():
                break


class EventStreamResponse(StreamingResponse):
    """The events stream's SSE response: a write its client does not take in time ends it.

    The body iterator is closed then, so what feeds it stops too, and the
    response completed (see the module docstring).
    """

    media_type = "text/event-stream"

    def __init__(self, content: AsyncIterator[bytes]) -> None:
        super().__init__(content, headers={k.decode(): v.decode() for k, v in SSE_HEADERS if k != b"content-type"})

    async def stream_response(self, send: Send) -> None:
        start = {"type": "http.response.start", "status": self.status_code, "headers": self.raw_headers}
        started = False
        try:
            async with asyncio.timeout(STREAM_WRITE_TIMEOUT_SECONDS):
                await send(start)
            started = True
            async for chunk in self.body_iterator:
                async with asyncio.timeout(STREAM_WRITE_TIMEOUT_SECONDS):
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
            async with asyncio.timeout(STREAM_WRITE_TIMEOUT_SECONDS):
                await send({"type": "http.response.body", "body": b"", "more_body": False})
        except TimeoutError:
            logger.info("A web chat events stream's client stopped reading: the stream ends")
            await self._close_body()
            try:
                async with asyncio.timeout(STREAM_ABANDON_SECONDS):
                    if not started:
                        await send(start)
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
            except TimeoutError:
                _log_abandoned()
        finally:
            await self._close_body()

    async def _close_body(self) -> None:
        aclose = getattr(self.body_iterator, "aclose", None)
        if aclose is not None:
            await aclose()


#: The frame a chat stream ends with once its companion session locked behind
#: its PIN, or the PIN ended it: the web app shows its lock screen, and its next
#: request reads which (a 423, or the 401 that signs it out).
APP_LOCKED_FRAME_TYPE = "app_locked"


SessionLockState = Literal["open", "locked", "unreadable"]


async def session_lock_state(user_id: object, web_session: object) -> SessionLockState:
    """Whether the companion session behind an open chat stream is open, locked behind its PIN (or ended by it), or cannot be told.

    Read with a fresh DB session: a stream releases its request's connection
    before it loops. A stream that is ``locked`` ends with an ``app_locked``
    frame, the lock screen's cue; one whose lock is ``unreadable`` (a database
    blip) just ends, as a lock it may be, so the app's next request reads the
    423 (its lock screen) or the 401 (signed out), and a person with no PIN is
    never shown the lock screen for it.
    """
    import uuid
    from types import SimpleNamespace

    from fastapi import HTTPException

    from app.core.database import async_session
    from app.services.app_lock import enforce as enforce_app_lock

    try:
        async with async_session() as lock_db:
            await enforce_app_lock(lock_db, SimpleNamespace(id=uuid.UUID(str(user_id)), web_session=web_session))
    except HTTPException:
        return "locked"
    except Exception:
        logger.warning("Chat stream: the app lock could not be read, the stream ends", exc_info=True)
        return "unreadable"
    return "open"
