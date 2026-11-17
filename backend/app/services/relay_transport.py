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

"""The XTM One Relay bus.

A request a resource routes through a relay group never leaves the platform as
a socket. It is written to the group's Redis stream as an encrypted ``open``
frame, claimed by ONE connected relay of the group (whichever replica holds
its WebSocket or answers its long poll), sent on the customer's network, and
answered frame by frame on a per-request stream. Every relay of a group pulls
from the same stream, which is the load balancing and the high availability.

Wire frames are ``[u32 BE header length][JSON header][payload]``, the same
bytes on the WebSocket, in the long-poll bodies and in Redis, where each frame
is Fernet-encrypted with the platform key and expires with its stream. Nothing
of a request reaches PostgreSQL but its metadata
(:func:`app.services.relays.log_relay_request`).

Delivery is AT MOST ONCE: an open is acknowledged the moment a relay claims
it, a relay that drops mid-request fails it with ``relay_disconnected``, and
nothing is retried behind the caller's back - a POST must never run twice.
Whatever cannot be sent through the group fails closed: there is no fallback
to a direct connection from the platform.

Keys (``{gid}`` a group id, ``{sid}`` a request id, ``{rid}`` a relay id):

* ``relay:group:{gid}:open`` - the group's open stream (consumer group
  ``relays``, one consumer per relay);
* ``relay:group:{gid}:online`` - presence ZSET, one member per connection,
  scored by its last heartbeat;
* ``relay:s:{sid}:up`` / ``relay:s:{sid}:down`` - the response frames and
  the frames the platform sends to the relay (request body chunks, window
  grants, cancel), each expiring with the request's idle timeout;
* ``relay:s:{sid}:owner`` / ``:cancelled`` / ``:credit`` - who claimed it,
  whether the caller gave up before it was claimed, and the bytes the relay
  may still send (the credit window, enforced here and not only on the relay);
* ``relay:r:{rid}:inflight`` - the requests a relay holds (capacity and the
  long poll's down frames);
* ``relay:r:{rid}:open`` - opens addressed to one relay of its group (the
  diagnostics "test from this relay"), claimed before the group's own;
* ``relay:w:*`` - wake-up notifications (pub/sub), one listener per process.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import InvalidToken
from redis.exceptions import ResponseError
from sqlalchemy import update

from app.core.config import settings
from app.core.metrics import get_or_create_counter, get_or_create_histogram
from app.core.redis import close_pubsub, open_pubsub, redis_client, redis_pubsub_client
from app.core.security import _fernet
from app.models.relay import (
    RELAY_CAPABILITIES,
    RELAY_PROTOCOL_VERSION,
    RELAY_RESERVED_CAPABILITIES,
    Relay,
)

logger = logging.getLogger(__name__)

# ── Constants ───────────────────────────────────────────────────────────────

MAX_HEADER_BYTES = 64 * 1024
# What the platform cuts a request body and the relay cuts a response into.
DATA_CHUNK_BYTES = 64 * 1024
# The largest data frame a relay may send (a little room over its chunk size).
MAX_FRAME_PAYLOAD_BYTES = 256 * 1024
# The largest body of frames a long-polling relay may upload in one request.
MAX_UPLOAD_BODY_BYTES = 8 * 1024 * 1024
# Bytes a relay may send on one request before the caller consumed them.
INITIAL_WINDOW_BYTES = 1024 * 1024
# Request body bytes carried inline in the open frame; the rest follows as
# down ``data`` frames so no single Redis entry holds a whole upload.
INLINE_BODY_BYTES = 1024 * 1024
OPEN_STREAM_MAXLEN = 10_000
STREAM_MAXLEN = 512
CONSUMER_GROUP = "relays"
WAIT_SLICE_SECONDS = 1.0
MAX_CLAIM_BATCH = 16
MAX_RESPONSE_HEADERS = 200
MAX_HEADER_VALUE_CHARS = 16 * 1024
MAX_HOST_FACTS_BYTES = 8 * 1024
MAX_ERROR_MESSAGE_CHARS = 500
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
MAX_REQUEST_TIMEOUT_SECONDS = 900.0

# Hop-by-hop headers (RFC 9110 7.6.1) and the ones the relay sets itself.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
_REQUEST_HEADERS_SET_BY_RELAY = frozenset({"host", "content-length"})

RELAY_TO_PLATFORM_TYPES = frozenset({"hello", "heartbeat", "response_head", "data", "end", "error"})
PLATFORM_TO_RELAY_TYPES = frozenset(
    {"hello_ack", "open", "data", "end", "window", "cancel", "update_required"}
)

RELAY_ERROR_MESSAGES: dict[str, str] = {
    "destination_denied": "The relay does not allow this destination",
    "dns_failed": "The relay could not resolve the host name",
    "connect_failed": "The relay could not connect to the destination",
    "tls_failed": "The TLS handshake between the relay and the destination failed",
    "timeout": "The request through the relay timed out",
    "too_large": "The request or the response is larger than a relay carries",
    "no_relay_online": "No relay of this group is online",
    "relay_disconnected": "The relay disconnected before the request finished",
    "protocol_error": "The relay answered something the platform could not read",
}
RELAY_ERROR_CODES: tuple[str, ...] = tuple(RELAY_ERROR_MESSAGES)

# Labelled by group id, never by its name: a name is free text its creator chose.
_requests_total = get_or_create_counter(
    "xtm_one_relay_requests_total",
    "Requests sent through a relay group, by group and outcome (ok or an error code).",
    ["group_id", "outcome"],
)
_bytes_total = get_or_create_counter(
    "xtm_one_relay_bytes_total",
    "Bytes carried through relay groups, by group and direction (sent / received).",
    ["group_id", "direction"],
)
_request_seconds = get_or_create_histogram(
    "xtm_one_relay_request_duration_seconds",
    "Time from the platform queueing a relayed request to its end, by group and outcome.",
    ["group_id", "outcome"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300),
)


# ── Errors ──────────────────────────────────────────────────────────────────


class RelayError(httpx.TransportError):
    """A request a relay group could not carry; ``code`` says why."""

    # Read by ``integrations.http.format_network_error``, which must not
    # import this module (it pulls Redis and SQLAlchemy into every provider).
    is_relay_error = True

    def __init__(self, code: str, message: str, *, request: httpx.Request | None = None):
        super().__init__(message, request=request)
        self.code = code


class RelayConnectError(RelayError, httpx.ConnectError):
    """The destination could not be reached (or no relay could)."""


class RelayTimeoutError(RelayError, httpx.ReadTimeout):
    """The request did not complete in time."""


class RelayProtocolError(RelayError, httpx.RemoteProtocolError):
    """The relay went away or answered out of protocol."""


_ERROR_CLASSES: dict[str, type[RelayError]] = {
    "dns_failed": RelayConnectError,
    "connect_failed": RelayConnectError,
    "tls_failed": RelayConnectError,
    "no_relay_online": RelayConnectError,
    "timeout": RelayTimeoutError,
    "relay_disconnected": RelayProtocolError,
    "protocol_error": RelayProtocolError,
}


def relay_error(
    code: str,
    detail: str | None = None,
    *,
    group_name: str | None = None,
    request: httpx.Request | None = None,
) -> RelayError:
    """The exception a caller sees for *code*: an ``httpx`` transport error.

    The class follows the code (a connect error, a timeout, a protocol error),
    so the retry and error formatting every integration already does on
    ``httpx`` exceptions keeps working through a relay.
    """
    if code not in RELAY_ERROR_MESSAGES:
        code = "protocol_error"
    message = RELAY_ERROR_MESSAGES[code]
    if group_name:
        message = f"{message} (relay group '{group_name}')"
    if detail:
        message = f"{message}: {detail[:MAX_ERROR_MESSAGE_CHARS]}"
    return _ERROR_CLASSES.get(code, RelayError)(code, message, request=request)


# ── Frames ──────────────────────────────────────────────────────────────────


class FrameError(ValueError):
    """Bytes that are not a frame of the relay protocol."""


@dataclass(slots=True)
class Frame:
    header: dict[str, Any]
    payload: bytes = b""

    @property
    def type(self) -> str:
        return str(self.header.get("type") or "")

    @property
    def sid(self) -> str | None:
        sid = self.header.get("sid")
        return sid if isinstance(sid, str) else None


def encode_frame(header: dict[str, Any], payload: bytes = b"") -> bytes:
    """``[u32 BE header length][JSON header][payload]``; ``len`` is set for you."""
    head = {k: v for k, v in header.items() if k != "len"}
    if payload:
        head["len"] = len(payload)
    raw = json.dumps(head, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(raw) > MAX_HEADER_BYTES:
        raise FrameError("frame header too large")
    return struct.pack(">I", len(raw)) + raw + payload


def encode_frames(frames: Iterable[Frame]) -> bytes:
    """A body of concatenated frames (a long-poll answer), each carrying ``len``."""
    out = bytearray()
    for frame in frames:
        head = {k: v for k, v in frame.header.items() if k != "len"}
        head["len"] = len(frame.payload)
        raw = json.dumps(head, separators=(",", ":"), ensure_ascii=True).encode("ascii")
        if len(raw) > MAX_HEADER_BYTES:
            raise FrameError("frame header too large")
        out += struct.pack(">I", len(raw)) + raw + frame.payload
    return bytes(out)


def decode_frame(data: bytes, offset: int = 0) -> tuple[Frame, int]:
    """Read one frame at *offset*; returns it and the offset after it.

    A header without ``len`` owns the rest of *data* (one frame per WebSocket
    message); concatenated frames must each carry ``len``.
    """
    if len(data) - offset < 4:
        raise FrameError("truncated frame")
    (header_len,) = struct.unpack_from(">I", data, offset)
    if header_len <= 0 or header_len > MAX_HEADER_BYTES:
        raise FrameError("invalid frame header length")
    start = offset + 4
    end = start + header_len
    if end > len(data):
        raise FrameError("truncated frame header")
    try:
        header = json.loads(bytes(data[start:end]))
    except (ValueError, UnicodeDecodeError) as exc:
        raise FrameError("frame header is not JSON") from exc
    if not isinstance(header, dict) or not isinstance(header.get("type"), str):
        raise FrameError("frame header has no type")
    length = header.get("len")
    if length is None:
        payload_end = len(data)
    else:
        if not isinstance(length, int) or isinstance(length, bool) or length < 0:
            raise FrameError("invalid frame payload length")
        payload_end = end + length
        if payload_end > len(data):
            raise FrameError("truncated frame payload")
    return Frame(header, bytes(data[end:payload_end])), payload_end


def decode_frames(data: bytes) -> list[Frame]:
    """Every frame of a body of concatenated frames."""
    frames: list[Frame] = []
    offset = 0
    while offset < len(data):
        frame, offset = decode_frame(data, offset)
        frames.append(frame)
    return frames


def seal(frame_bytes: bytes) -> str:
    """Encrypt a frame for Redis (a str: the shared client decodes responses)."""
    return _fernet.encrypt(frame_bytes).decode("ascii")


def unseal(token: str) -> bytes:
    return _fernet.decrypt(token.encode("ascii"))


def _sealed_frame(header: dict[str, Any], payload: bytes = b"") -> dict[str, str]:
    return {"f": seal(encode_frame(header, payload))}


def _unseal_entry(fields: dict) -> Frame | None:
    token = fields.get("f") if isinstance(fields, dict) else None
    if not isinstance(token, str):
        return None
    try:
        frame, _ = decode_frame(unseal(token))
    except (InvalidToken, FrameError, ValueError):
        logger.warning("Dropped an unreadable relay frame")
        return None
    return frame


# ── Keys ────────────────────────────────────────────────────────────────────


def open_stream_key(group_id: Any) -> str:
    return f"relay:group:{group_id}:open"


def presence_key(group_id: Any) -> str:
    return f"relay:group:{group_id}:online"


def up_key(sid: str) -> str:
    return f"relay:s:{sid}:up"


def down_key(sid: str) -> str:
    return f"relay:s:{sid}:down"


def owner_key(sid: str) -> str:
    return f"relay:s:{sid}:owner"


def cancel_key(sid: str) -> str:
    return f"relay:s:{sid}:cancelled"


def credit_key(sid: str) -> str:
    return f"relay:s:{sid}:credit"


def inflight_key(relay_id: Any) -> str:
    return f"relay:r:{relay_id}:inflight"


def direct_open_key(relay_id: Any) -> str:
    return f"relay:r:{relay_id}:open"


WAKE_PREFIX = "relay:w:"


def wake_channel(kind: str, ident: Any) -> str:
    return f"{WAKE_PREFIX}{kind}:{ident}"


def _idle_ttl() -> int:
    return max(30, int(settings.relay_stream_idle_timeout_seconds))


def _valid_sid(sid: Any) -> bool:
    return isinstance(sid, str) and 8 <= len(sid) <= 64 and sid.isalnum()


# ── Wake-ups ────────────────────────────────────────────────────────────────


class _Waker:
    """One pub/sub listener per process that sets the events waiting on a channel.

    A wake-up is only a hint: every waiter also re-reads its stream every
    :data:`WAIT_SLICE_SECONDS`, so a notification lost to a reconnect costs a
    second of latency, never a request.
    """

    def __init__(self) -> None:
        self._events: dict[str, set[asyncio.Event]] = {}
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _ensure_listener(self) -> None:
        loop = asyncio.get_running_loop()
        if self._task is not None and not self._task.done() and self._loop is loop:
            return
        self._loop = loop
        self._task = loop.create_task(self._listen(), name="relay-waker")

    async def _listen(self) -> None:
        backoff = 1.0
        while True:
            client = None
            pubsub = None
            try:
                client = await redis_pubsub_client()
                pubsub = await open_pubsub(client, patterns=(f"{WAKE_PREFIX}*",))
                backoff = 1.0
                while self._events:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=1.0
                    )
                    if not message or message.get("type") != "pmessage":
                        continue
                    for event in tuple(self._events.get(message.get("channel"), ())):
                        event.set()
                if not self._events:
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("Relay wake listener reconnecting", exc_info=True)
            finally:
                await close_pubsub(pubsub, client)
            # Closed first: its connection goes back to the pool before the backoff.
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    @contextlib.contextmanager
    def watch(self, *channels: str):
        """An event set whenever one of *channels* is notified."""
        event = asyncio.Event()
        for channel in channels:
            self._events.setdefault(channel, set()).add(event)
        self._ensure_listener()
        try:
            yield event
        finally:
            for channel in channels:
                waiters = self._events.get(channel)
                if waiters is not None:
                    waiters.discard(event)
                    if not waiters:
                        self._events.pop(channel, None)


_WAKER = _Waker()


async def _wait(event: asyncio.Event, timeout: float) -> None:
    if timeout <= 0:
        return
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(event.wait(), timeout)


async def _notify(r, *channels: str) -> None:
    try:
        async with r.pipeline(transaction=False) as pipe:
            for channel in channels:
                pipe.publish(channel, "1")
            await pipe.execute()
    except Exception:
        logger.debug("Relay wake-up publish failed", exc_info=True)


# ── Presence ────────────────────────────────────────────────────────────────


def _now() -> float:
    return time.time()


def poll_presence_member(relay_id: Any) -> str:
    """The presence member of a long-polling relay (one per relay)."""
    return f"{relay_id}:poll"


async def touch_presence(group_id: Any, member: str) -> None:
    """Mark one connection of a group as alive now."""
    r = await redis_client()
    now = _now()
    async with r.pipeline(transaction=False) as pipe:
        pipe.zadd(presence_key(group_id), {member: now})
        pipe.zremrangebyscore(
            presence_key(group_id), "-inf", now - 2 * settings.relay_offline_after_seconds
        )
        await pipe.execute()


async def drop_presence(group_id: Any, member: str) -> None:
    with contextlib.suppress(Exception):
        r = await redis_client()
        await r.zrem(presence_key(group_id), member)


async def online_connection_count(group_id: Any) -> int:
    """Connections of the group heard from within the offline window."""
    r = await redis_client()
    since = _now() - settings.relay_offline_after_seconds
    return int(await r.zcount(presence_key(group_id), since, "+inf"))


async def relay_connected(group_id: Any, relay_id: Any) -> bool:
    """Whether one relay of the group holds a live connection right now."""
    r = await redis_client()
    since = _now() - settings.relay_offline_after_seconds
    members = await r.zrangebyscore(presence_key(group_id), since, "+inf")
    prefix = f"{relay_id}:"
    return any(isinstance(m, str) and m.startswith(prefix) for m in members or [])


async def relay_inflight_count(relay_id: Any) -> int:
    with contextlib.suppress(Exception):
        r = await redis_client()
        return int(await r.zcard(inflight_key(relay_id)))
    return 0


async def signal_relay_revoked(relay_id: Any) -> None:
    """Close a revoked relay's WebSocket now (the database refuses it anyway)."""
    with contextlib.suppress(Exception):
        r = await redis_client()
        await _notify(r, wake_channel("ctl", relay_id))


# ── Relay side (called by the WebSocket and long-poll endpoints) ────────────

_groups_ready: set[str] = set()


async def _ensure_consumer_group(r, group_id: Any) -> None:
    key = open_stream_key(group_id)
    if key in _groups_ready:
        return
    try:
        await r.xgroup_create(key, CONSUMER_GROUP, id="0", mkstream=True)
    except ResponseError as exc:
        if "BUSYGROUP" not in str(exc):
            raise
    _groups_ready.add(key)


async def claim_opens(relay_id: Any, group_id: Any, limit: int) -> list[Frame]:
    """Claim up to *limit* opens of the group for this relay.

    An open is acknowledged and deleted as soon as it is read: it is never
    redelivered. One past its claim deadline or cancelled by its caller is
    dropped here, so a relay never starts a request nobody waits for.
    """
    if limit <= 0:
        return []
    r = await redis_client()
    batch_size = min(limit, MAX_CLAIM_BATCH)
    entries: list = []
    direct = direct_open_key(relay_id)
    direct_entries = await r.xrange(direct, "-", "+", count=batch_size)
    if direct_entries:
        await r.xdel(direct, *[entry_id for entry_id, _ in direct_entries])
        entries.extend(direct_entries)
    if len(entries) < batch_size:
        await _ensure_consumer_group(r, group_id)
        key = open_stream_key(group_id)
        try:
            result = await r.xreadgroup(
                CONSUMER_GROUP,
                str(relay_id),
                {key: ">"},
                count=batch_size - len(entries),
            )
        except ResponseError as exc:
            if "NOGROUP" not in str(exc):
                raise
            _groups_ready.discard(key)
            await _ensure_consumer_group(r, group_id)
            result = None
        group_entries = [entry for _, batch in (result or []) for entry in batch]
        if group_entries:
            ids = [entry_id for entry_id, _ in group_entries]
            async with r.pipeline(transaction=False) as pipe:
                pipe.xack(key, CONSUMER_GROUP, *ids)
                pipe.xdel(key, *ids)
                await pipe.execute()
            entries.extend(group_entries)
    if not entries:
        return []

    claimed: list[Frame] = []
    now_ms = int(_now() * 1000)
    ttl = _idle_ttl()
    for _, fields in entries:
        frame = _unseal_entry(fields)
        if frame is None or frame.type != "open" or not _valid_sid(frame.sid):
            continue
        sid = frame.sid
        deadline = frame.header.get("deadline")
        if isinstance(deadline, int) and deadline < now_ms:
            continue
        if await r.exists(cancel_key(sid)):
            continue
        if not await r.set(owner_key(sid), str(relay_id), ex=ttl, nx=True):
            continue
        async with r.pipeline(transaction=False) as pipe:
            pipe.xadd(
                up_key(sid),
                _sealed_frame({"type": "claimed", "sid": sid, "relay_id": str(relay_id)}),
                maxlen=STREAM_MAXLEN,
                approximate=True,
            )
            pipe.expire(up_key(sid), ttl)
            pipe.zadd(inflight_key(relay_id), {sid: _now()})
            pipe.expire(inflight_key(relay_id), ttl)
            pipe.publish(wake_channel("u", sid), "1")
            await pipe.execute()
        claimed.append(frame)
    return claimed


async def drain_down(relay_id: Any, sids: Iterable[str]) -> list[Frame]:
    """The frames waiting for this relay on its requests (read and deleted)."""
    sid_list = [sid for sid in dict.fromkeys(sids) if _valid_sid(sid)]
    if not sid_list:
        return []
    r = await redis_client()
    async with r.pipeline(transaction=False) as pipe:
        for sid in sid_list:
            pipe.xrange(down_key(sid), "-", "+", count=128)
        batches = await pipe.execute()
    frames: list[Frame] = []
    to_delete: list[tuple[str, list[str]]] = []
    finished: list[str] = []
    for sid, batch in zip(sid_list, batches, strict=True):
        if not batch:
            continue
        to_delete.append((sid, [entry_id for entry_id, _ in batch]))
        for _, fields in batch:
            frame = _unseal_entry(fields)
            if frame is None or frame.type not in PLATFORM_TO_RELAY_TYPES:
                continue
            frame.header["sid"] = sid
            frames.append(frame)
            if frame.type == "cancel":
                finished.append(sid)
    if to_delete or finished:
        async with r.pipeline(transaction=False) as pipe:
            for sid, ids in to_delete:
                pipe.xdel(down_key(sid), *ids)
            if finished:
                pipe.zrem(inflight_key(relay_id), *finished)
            await pipe.execute()
    return frames


async def inflight_sids(relay_id: Any) -> list[str]:
    """The requests this relay holds; entries idle past the timeout are pruned."""
    r = await redis_client()
    key = inflight_key(relay_id)
    async with r.pipeline(transaction=False) as pipe:
        pipe.zremrangebyscore(key, "-inf", _now() - _idle_ttl())
        pipe.zrange(key, 0, -1)
        _, members = await pipe.execute()
    return [m for m in members or [] if isinstance(m, str)]


def _clean_response_header(frame: Frame) -> dict[str, Any] | None:
    """The header of a relay's response frame, rebuilt from known keys only."""
    sid = frame.sid
    kind = frame.type
    if kind == "response_head":
        status = frame.header.get("status")
        if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 999:
            return None
        headers: list[list[str]] = []
        for pair in frame.header.get("headers") or []:
            if (
                isinstance(pair, list | tuple)
                and len(pair) == 2
                and isinstance(pair[0], str)
                and isinstance(pair[1], str)
                and 0 < len(pair[0]) <= 256
                and len(pair[1]) <= MAX_HEADER_VALUE_CHARS
            ):
                headers.append([pair[0], pair[1]])
            if len(headers) >= MAX_RESPONSE_HEADERS:
                break
        return {"type": kind, "sid": sid, "status": status, "headers": headers}
    if kind in ("data", "end"):
        return {"type": kind, "sid": sid}
    if kind == "error":
        code = frame.header.get("code")
        message = frame.header.get("message")
        return {
            "type": kind,
            "sid": sid,
            "code": code if code in RELAY_ERROR_MESSAGES else "protocol_error",
            "message": message[:MAX_ERROR_MESSAGE_CHARS] if isinstance(message, str) else "",
        }
    return None


async def accept_from_relay(relay_id: Any, frame: Frame, owned: set[str] | None = None) -> bool:
    """Pass one response frame of this relay to the caller waiting on it.

    Refused (``False``) for a request the relay did not claim, an unknown
    frame type, an oversized data frame or data past the credit window; a
    relay over its window gets its request failed rather than buffered.
    """
    sid = frame.sid
    if not _valid_sid(sid) or frame.type not in ("response_head", "data", "end", "error"):
        return False
    if frame.type == "data" and len(frame.payload) > MAX_FRAME_PAYLOAD_BYTES:
        return False
    header = _clean_response_header(frame)
    if header is None:
        return False
    r = await redis_client()
    if owned is None or sid not in owned:
        if await r.get(owner_key(sid)) != str(relay_id):
            return False
        if owned is not None:
            owned.add(sid)
    payload = frame.payload if frame.type == "data" else b""
    if payload:
        left = await r.decrby(credit_key(sid), len(payload))
        if left < 0:
            header = {
                "type": "error",
                "sid": sid,
                "code": "protocol_error",
                "message": "the relay sent past its credit window",
            }
            payload = b""
    ttl = _idle_ttl()
    final = header["type"] in ("end", "error")
    async with r.pipeline(transaction=False) as pipe:
        pipe.xadd(
            up_key(sid),
            _sealed_frame(header, payload),
            maxlen=STREAM_MAXLEN,
            approximate=True,
        )
        pipe.expire(up_key(sid), ttl)
        pipe.expire(owner_key(sid), ttl)
        pipe.expire(credit_key(sid), ttl)
        if final:
            pipe.zrem(inflight_key(relay_id), sid)
        else:
            pipe.zadd(inflight_key(relay_id), {sid: _now()})
        pipe.publish(wake_channel("u", sid), "1")
        await pipe.execute()
    if final and owned is not None:
        owned.discard(sid)
    return header["type"] == frame.type


async def fail_requests(sids: Iterable[str], code: str, message: str = "") -> None:
    """Fail requests a relay can no longer answer (its connection dropped)."""
    sid_list = [sid for sid in sids if _valid_sid(sid)]
    if not sid_list:
        return
    with contextlib.suppress(Exception):
        r = await redis_client()
        ttl = _idle_ttl()
        async with r.pipeline(transaction=False) as pipe:
            for sid in sid_list:
                pipe.xadd(
                    up_key(sid),
                    _sealed_frame(
                        {"type": "error", "sid": sid, "code": code, "message": message}
                    ),
                    maxlen=STREAM_MAXLEN,
                    approximate=True,
                )
                pipe.expire(up_key(sid), ttl)
                pipe.publish(wake_channel("u", sid), "1")
            await pipe.execute()


# ── Liveness bookkeeping in the database ────────────────────────────────────


def _clean_hello(header: dict[str, Any]) -> dict[str, Any]:
    """What a hello may write on the relay row, bounded and typed."""
    from app.services.relays import allows_all

    known = set(RELAY_CAPABILITIES) | set(RELAY_RESERVED_CAPABILITIES)
    capabilities = [
        c for c in header.get("capabilities") or [] if isinstance(c, str) and c in known
    ][:10]
    destinations = [
        d[:300] for d in header.get("destinations") or [] if isinstance(d, str) and d.strip()
    ][:200]
    max_concurrent = header.get("max_concurrent")
    if not isinstance(max_concurrent, int) or isinstance(max_concurrent, bool):
        max_concurrent = 32
    host_facts = header.get("host_facts")
    if not isinstance(host_facts, dict) or len(json.dumps(host_facts, default=str)) > MAX_HOST_FACTS_BYTES:
        host_facts = None
    version = header.get("version")
    protocol = header.get("protocol")
    values: dict[str, Any] = {
        "capabilities": capabilities,
        "declared_destinations": destinations,
        "allows_all_destinations": allows_all(destinations),
        "max_concurrent_requests": max(1, min(256, max_concurrent)),
        "protocol_version": protocol if isinstance(protocol, int) else None,
    }
    if isinstance(version, str) and version.strip():
        values["agent_version"] = version.strip()[:40]
    if host_facts is not None:
        values["host_facts"] = host_facts
        hostname = host_facts.get("hostname")
        if isinstance(hostname, str) and hostname.strip():
            values["hostname"] = hostname.strip()[:255]
    return values


async def touch_relay(relay_id: Any, *, transport: str, hello: dict[str, Any] | None = None) -> bool:
    """Refresh ``last_seen_at`` (and what a hello reports); ``False`` when revoked."""
    from app.core.database import async_session

    values: dict[str, Any] = {
        "last_seen_at": datetime.now(UTC),
        "status": "online",
        "transport": transport,
    }
    if hello is not None:
        values.update(_clean_hello(hello))
    async with async_session() as db:
        result = await db.execute(
            update(Relay)
            .where(Relay.id == relay_id, Relay.is_active.is_(True))
            .values(**values)
            .returning(Relay.id)
        )
        touched = result.scalar_one_or_none() is not None
        await db.commit()
    return touched


def hello_ack(relay: Relay) -> dict[str, Any]:
    return {
        "type": "hello_ack",
        "protocol": RELAY_PROTOCOL_VERSION,
        "relay_id": str(relay.id),
        "group_id": str(relay.group_id),
        "heartbeat_interval": settings.relay_heartbeat_interval_seconds,
        "long_poll_timeout": settings.relay_long_poll_timeout_seconds,
        "max_request_bytes": settings.relay_max_request_bytes,
        "max_response_bytes": settings.relay_max_response_bytes,
        "window": INITIAL_WINDOW_BYTES,
        "chunk": DATA_CHUNK_BYTES,
    }


def check_hello(header: dict[str, Any]) -> str | None:
    """Why a hello is refused, else ``None``."""
    if header.get("type") != "hello":
        return "the first frame must be a hello"
    if header.get("protocol") != RELAY_PROTOCOL_VERSION:
        return f"unsupported protocol version (this platform speaks {RELAY_PROTOCOL_VERSION})"
    capabilities = header.get("capabilities")
    if not isinstance(capabilities, list) or "http" not in capabilities:
        return "the relay must serve http"
    return None


# ── WebSocket serve loop ────────────────────────────────────────────────────


async def serve_relay_websocket(websocket, relay: Relay) -> None:
    """Serve one relay's WebSocket until it drops or is revoked.

    The first frame is the relay's hello; then opens are pushed as long as the
    relay has free capacity, its response frames are passed to their callers,
    and the requests it held fail with ``relay_disconnected`` when it drops.
    """
    relay_id = relay.id
    group_id = relay.group_id
    try:
        message = await asyncio.wait_for(websocket.receive(), timeout=15)
        data = message.get("bytes")
        if data is None:
            raise FrameError("expected a binary frame")
        hello, _ = decode_frame(data)
    except (TimeoutError, FrameError, KeyError):
        with contextlib.suppress(Exception):
            await websocket.close(code=4400)
        return
    refusal = check_hello(hello.header)
    if refusal:
        with contextlib.suppress(Exception):
            await websocket.send_bytes(
                encode_frame({"type": "error", "code": "protocol_error", "message": refusal})
            )
            await websocket.close(code=4400)
        return
    if not await touch_relay(relay_id, transport="ws", hello=hello.header):
        with contextlib.suppress(Exception):
            await websocket.close(code=4401)
        return
    capacity = max(1, min(256, int(hello.header.get("max_concurrent") or 32)))
    send_lock = asyncio.Lock()

    async def send(header: dict[str, Any], payload: bytes = b"") -> None:
        async with send_lock:
            await websocket.send_bytes(encode_frame(header, payload))

    await send(hello_ack(relay))
    member = f"{relay_id}:{uuid.uuid4().hex[:12]}"
    await touch_presence(group_id, member)
    held: set[str] = set()
    owned: set[str] = set()

    async def reader() -> None:
        while True:
            message = await asyncio.wait_for(
                websocket.receive(), timeout=settings.relay_offline_after_seconds
            )
            if message.get("type") == "websocket.disconnect":
                return
            data = message.get("bytes")
            if not data:
                continue
            try:
                frames = decode_frames(data)
            except FrameError:
                logger.info("Relay %s sent an unreadable frame", relay_id)
                return
            for frame in frames:
                if frame.type == "heartbeat":
                    continue
                if frame.type in ("end", "error"):
                    held.discard(frame.sid or "")
                await accept_from_relay(relay_id, frame, owned)

    async def pump() -> None:
        with (
            _WAKER.watch(wake_channel("g", group_id), wake_channel("r", relay_id)) as wake,
            _WAKER.watch(wake_channel("ctl", relay_id)) as revoked,
        ):
            while True:
                wake.clear()
                if revoked.is_set():
                    with contextlib.suppress(Exception):
                        await websocket.close(code=4401)
                    return
                opens = await claim_opens(relay_id, group_id, capacity - len(held))
                for frame in opens:
                    held.add(frame.sid)
                    owned.add(frame.sid)
                    await send(frame.header, frame.payload)
                downs = await drain_down(relay_id, held)
                for frame in downs:
                    await send(frame.header, frame.payload)
                    if frame.type == "cancel":
                        held.discard(frame.sid or "")
                if not opens and not downs:
                    await _wait(wake, WAIT_SLICE_SECONDS)

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(settings.relay_heartbeat_interval_seconds)
            await touch_presence(group_id, member)
            if not await touch_relay(relay_id, transport="ws"):
                with contextlib.suppress(Exception):
                    await websocket.close(code=4401)
                return

    tasks = [
        asyncio.create_task(reader(), name=f"relay-reader-{relay_id}"),
        asyncio.create_task(pump(), name=f"relay-pump-{relay_id}"),
        asyncio.create_task(heartbeat(), name=f"relay-heartbeat-{relay_id}"),
    ]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception() if not task.cancelled() else None
            if exc is not None and not isinstance(exc, TimeoutError):
                logger.debug("Relay %s connection ended", relay_id, exc_info=exc)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        await drop_presence(group_id, member)
        if held:
            await fail_requests(held, "relay_disconnected")
            with contextlib.suppress(Exception):
                r = await redis_client()
                await r.zrem(inflight_key(relay_id), *held)
        with contextlib.suppress(Exception):
            await websocket.close()


# ── Long-poll serve functions ───────────────────────────────────────────────


async def serve_relay_poll(relay: Relay, *, free: int, wait: bool) -> list[Frame]:
    """One long poll: opens to start and frames for the requests the relay holds."""
    relay_id = relay.id
    group_id = relay.group_id
    member = poll_presence_member(relay_id)
    await touch_presence(group_id, member)
    free = max(0, min(int(free), int(relay.max_concurrent_requests or 32)))
    deadline = time.monotonic() + (settings.relay_long_poll_timeout_seconds if wait else 0)
    with _WAKER.watch(wake_channel("g", group_id), wake_channel("r", relay_id)) as wake:
        while True:
            wake.clear()
            held = await inflight_sids(relay_id)
            opens = await claim_opens(relay_id, group_id, free)
            downs = await drain_down(relay_id, [*held, *(f.sid for f in opens if f.sid)])
            if opens or downs:
                return [*opens, *downs]
            left = deadline - time.monotonic()
            if left <= 0:
                return []
            await _wait(wake, min(WAIT_SLICE_SECONDS * 5, left))
            await touch_presence(group_id, member)


async def accept_relay_upload(relay: Relay, frames: list[Frame]) -> int:
    """The response frames a long-polling relay uploaded; returns how many passed."""
    accepted = 0
    for frame in frames:
        if frame.type == "heartbeat":
            continue
        if await accept_from_relay(relay.id, frame):
            accepted += 1
    return accepted


# ── Platform side: sending a request through a group ────────────────────────


class RelayStream:
    """One request in flight through a relay group, as the caller sees it."""

    def __init__(self, group_id: Any, sid: str) -> None:
        self.group_id = group_id
        self.sid = sid
        self.relay_id: str | None = None
        self._cursor = "0-0"
        self._pending: deque[Frame] = deque()
        self._consumed = 0
        self.finished = False

    async def _read(self) -> None:
        r = await redis_client()
        result = await r.xread({up_key(self.sid): self._cursor}, count=64)
        for _, batch in result or []:
            for entry_id, fields in batch:
                self._cursor = entry_id
                frame = _unseal_entry(fields)
                if frame is None:
                    frame = Frame({"type": "error", "sid": self.sid, "code": "protocol_error"})
                self._pending.append(frame)

    def _pop(self) -> Frame:
        frame = self._pending.popleft()
        if frame.type == "claimed":
            self.relay_id = str(frame.header.get("relay_id") or "") or None
        return frame

    async def next_frame(self, timeout: float) -> Frame | None:
        """The next response frame, or ``None`` once *timeout* passed."""
        if self._pending:
            return self._pop()
        end = time.monotonic() + max(0.0, timeout)
        with _WAKER.watch(wake_channel("u", self.sid)) as wake:
            while True:
                wake.clear()
                await self._read()
                if self._pending:
                    return self._pop()
                left = end - time.monotonic()
                if left <= 0:
                    return None
                await _wait(wake, min(WAIT_SLICE_SECONDS, left))

    async def _send_down(self, header: dict[str, Any], payload: bytes = b"") -> None:
        r = await redis_client()
        ttl = _idle_ttl()
        async with r.pipeline(transaction=False) as pipe:
            pipe.xadd(
                down_key(self.sid),
                _sealed_frame({**header, "sid": self.sid}, payload),
                maxlen=STREAM_MAXLEN,
                approximate=True,
            )
            pipe.expire(down_key(self.sid), ttl)
            pipe.get(owner_key(self.sid))
            *_, owner = await pipe.execute()
        if owner:
            await _notify(r, wake_channel("r", owner))

    async def consumed(self, nbytes: int) -> None:
        """Grant the relay more window once half of it was read."""
        self._consumed += nbytes
        if self._consumed < INITIAL_WINDOW_BYTES // 2 or self.finished:
            return
        grant, self._consumed = self._consumed, 0
        r = await redis_client()
        await r.incrby(credit_key(self.sid), grant)
        await r.expire(credit_key(self.sid), _idle_ttl())
        await self._send_down({"type": "window", "increment": grant})

    async def cancel(self) -> None:
        """Give up: the relay stops (or never starts) the request."""
        if self.finished:
            return
        self.finished = True
        with contextlib.suppress(Exception):
            r = await redis_client()
            await r.set(cancel_key(self.sid), "1", ex=_idle_ttl())
            await self._send_down({"type": "cancel"})


async def open_relay_stream(
    group_id: Any,
    *,
    method: str,
    url: str,
    headers: list[list[str]],
    body: bytes,
    timeout_s: float,
    connect_timeout_s: float | None,
    queue_timeout_s: float,
    relay_id: Any = None,
) -> RelayStream:
    """Queue one request on the group's stream and return its stream.

    With *relay_id* the request is addressed to that relay alone (its direct
    queue) instead of whichever relay of the group claims it first.
    """
    r = await redis_client()
    if relay_id is None:
        await _ensure_consumer_group(r, group_id)
    sid = uuid.uuid4().hex
    stream = RelayStream(group_id, sid)
    ttl = _idle_ttl()
    inline, rest = body[:INLINE_BODY_BYTES], body[INLINE_BODY_BYTES:]
    header: dict[str, Any] = {
        "type": "open",
        "sid": sid,
        "kind": "http",
        "method": method,
        "url": url,
        "headers": headers,
        "timeout_ms": int(timeout_s * 1000),
        "deadline": int((_now() + queue_timeout_s) * 1000),
        "window": INITIAL_WINDOW_BYTES,
        "body_len": len(body),
    }
    if connect_timeout_s:
        header["connect_timeout_ms"] = int(connect_timeout_s * 1000)
    async with r.pipeline(transaction=False) as pipe:
        pipe.set(credit_key(sid), INITIAL_WINDOW_BYTES, ex=ttl)
        for start in range(0, len(rest), MAX_FRAME_PAYLOAD_BYTES):
            pipe.xadd(
                down_key(sid),
                _sealed_frame({"type": "data", "sid": sid}, rest[start : start + MAX_FRAME_PAYLOAD_BYTES]),
                maxlen=STREAM_MAXLEN,
                approximate=True,
            )
        if rest:
            pipe.expire(down_key(sid), ttl)
        if relay_id is None:
            pipe.xadd(
                open_stream_key(group_id),
                _sealed_frame(header, inline),
                maxlen=OPEN_STREAM_MAXLEN,
                approximate=True,
            )
            pipe.publish(wake_channel("g", group_id), "1")
        else:
            pipe.xadd(
                direct_open_key(relay_id),
                _sealed_frame(header, inline),
                maxlen=STREAM_MAXLEN,
                approximate=True,
            )
            pipe.expire(direct_open_key(relay_id), ttl)
            pipe.publish(wake_channel("r", relay_id), "1")
        await pipe.execute()
    return stream


def _request_timeouts(request: httpx.Request) -> tuple[float, float | None]:
    """The relay's request timeout and connect timeout, from the httpx request."""
    timeout = request.extensions.get("timeout") or {}
    values = [v for v in (timeout.get("read"), timeout.get("write")) if isinstance(v, int | float)]
    read = max(values) if values else DEFAULT_REQUEST_TIMEOUT_SECONDS
    connect = timeout.get("connect")
    return (
        min(MAX_REQUEST_TIMEOUT_SECONDS, max(1.0, float(read))),
        float(connect) if isinstance(connect, int | float) else None,
    )


def _forwarded_request_headers(request: httpx.Request) -> list[list[str]]:
    out: list[list[str]] = []
    for name, value in request.headers.multi_items():
        lowered = name.lower()
        if lowered in _HOP_BY_HOP or lowered in _REQUEST_HEADERS_SET_BY_RELAY:
            continue
        out.append([name, value])
    return out


def _response_headers(pairs: list[list[str]]) -> list[tuple[str, str]]:
    return [(k, v) for k, v in pairs if k.lower() not in _HOP_BY_HOP]


class _RequestRecord:
    """The metadata of one relayed request, logged once when it ends."""

    def __init__(self, transport: RelayTransport, request: httpx.Request) -> None:
        url = request.url
        self.entry: dict[str, Any] = {
            "group_id": transport.group_id,
            "source_type": transport.source_type,
            "source_id": transport.source_id,
            "method": request.method,
            "scheme": url.scheme,
            "host": url.host,
            "port": url.port or (443 if url.scheme == "https" else 80),
            "path": url.path or "/",
            "bytes_sent": 0,
            "bytes_received": 0,
        }
        self.started = time.monotonic()
        self.done = False
        # The tool call this request serves names the group in its trace.
        fields = {"egress.relay_group": str(transport.group_id)}
        if transport.group_name:
            fields["egress.relay_group_name"] = transport.group_name
        with contextlib.suppress(Exception):
            from app.services.langfuse_tracing import annotate_current_tool_span

            annotate_current_tool_span(fields)

    def finish(self, *, status: int | None = None, error: str | None = None, relay_id: str | None = None) -> None:
        if self.done:
            return
        self.done = True
        self.entry["duration_ms"] = int((time.monotonic() - self.started) * 1000)
        if status is not None:
            self.entry["status_code"] = status
        if error is not None:
            self.entry["error_code"] = error
        if relay_id:
            with contextlib.suppress(ValueError):
                self.entry["relay_id"] = uuid.UUID(relay_id)
        group = str(self.entry["group_id"])
        outcome = error or "ok"
        _requests_total.labels(group_id=group, outcome=outcome).inc()
        _request_seconds.labels(group_id=group, outcome=outcome).observe(self.entry["duration_ms"] / 1000)
        _bytes_total.labels(group_id=group, direction="sent").inc(self.entry["bytes_sent"])
        _bytes_total.labels(group_id=group, direction="received").inc(self.entry["bytes_received"])
        from app.services.relays import log_relay_request

        with contextlib.suppress(Exception):
            log_relay_request(self.entry)


class _RelayResponseStream(httpx.AsyncByteStream):
    def __init__(
        self,
        stream: RelayStream,
        record: _RequestRecord,
        *,
        status: int,
        read_timeout: float,
        group_name: str | None,
    ) -> None:
        self._stream = stream
        self._record = record
        self._status = status
        self._read_timeout = read_timeout
        self._group_name = group_name

    async def __aiter__(self) -> AsyncIterator[bytes]:
        limit = settings.relay_max_response_bytes
        received = 0
        try:
            while True:
                frame = await self._stream.next_frame(self._read_timeout)
                if frame is None:
                    raise relay_error("timeout", "no data within the read timeout", group_name=self._group_name)
                if frame.type == "data":
                    received += len(frame.payload)
                    self._record.entry["bytes_received"] = received
                    if received > limit:
                        raise relay_error("too_large", f"the response passed {limit} bytes", group_name=self._group_name)
                    if frame.payload:
                        yield frame.payload
                    await self._stream.consumed(len(frame.payload))
                elif frame.type == "end":
                    self._stream.finished = True
                    self._record.finish(status=self._status, relay_id=self._stream.relay_id)
                    return
                elif frame.type == "error":
                    code = str(frame.header.get("code") or "protocol_error")
                    raise relay_error(
                        code, str(frame.header.get("message") or "") or None, group_name=self._group_name
                    )
        except RelayError as exc:
            await self._stream.cancel()
            self._record.finish(status=self._status, error=exc.code, relay_id=self._stream.relay_id)
            raise

    async def aclose(self) -> None:
        # A caller closing the response early (a status check, a cut stream)
        # is its choice, not a relay error: the request is logged as answered.
        if not self._stream.finished:
            await self._stream.cancel()
        self._record.finish(status=self._status, relay_id=self._stream.relay_id)


class RelayTransport(httpx.AsyncBaseTransport):
    """An ``httpx`` transport that sends every request through a relay group.

    Fails closed: relays turned off (``RELAYS_ENABLED=false``), a destination
    the group's platform-side allow-list refuses, a group with no relay
    online, a request nobody claims in time - each is an ``httpx`` transport
    error, never a direct connection from the platform.
    Redirects are followed by the client, so every hop goes through the relay
    (and through its own allow-list) again.
    """

    def __init__(
        self,
        *,
        group_id: Any,
        group_name: str | None = None,
        allowed_destinations: list[str] | None = None,
        source_type: str | None = None,
        source_id: Any = None,
        relay_id: Any = None,
    ) -> None:
        self.group_id = group_id
        self.group_name = group_name
        self.allowed_destinations = allowed_destinations
        self.source_type = source_type
        self.source_id = source_id
        self.relay_id = relay_id

    async def _read_body(self, request: httpx.Request) -> bytes:
        limit = settings.relay_max_request_bytes
        chunks: list[bytes] = []
        total = 0
        async for chunk in request.stream:
            total += len(chunk)
            if total > limit:
                raise relay_error(
                    "too_large", f"the request body passed {limit} bytes", group_name=self.group_name, request=request
                )
            chunks.append(chunk)
        return b"".join(chunks)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        from app.services.relays import destination_allowed, relays_active

        record = _RequestRecord(self, request)
        url = request.url
        try:
            if not relays_active():
                raise relay_error(
                    "no_relay_online",
                    "relays are turned off on this platform (RELAYS_ENABLED=false)",
                    group_name=self.group_name,
                    request=request,
                )
            if url.scheme not in ("http", "https") or not url.host:
                raise relay_error("destination_denied", "only http and https URLs are relayed", group_name=self.group_name, request=request)
            port = url.port or (443 if url.scheme == "https" else 80)
            if not destination_allowed(self.allowed_destinations, url.host, port):
                raise relay_error(
                    "destination_denied",
                    f"{url.host}:{port} is not in the group's allowed destinations",
                    group_name=self.group_name,
                    request=request,
                )
            if self.relay_id is not None:
                if not await relay_connected(self.group_id, self.relay_id):
                    raise relay_error(
                        "no_relay_online",
                        "the chosen relay is not connected",
                        group_name=self.group_name,
                        request=request,
                    )
            elif await online_connection_count(self.group_id) == 0:
                raise relay_error("no_relay_online", group_name=self.group_name, request=request)
            body = await self._read_body(request)
            record.entry["bytes_sent"] = len(body)
            timeout_s, connect_s = _request_timeouts(request)
            queue_s = float(settings.relay_queue_timeout_seconds)
            stream = await open_relay_stream(
                self.group_id,
                method=request.method,
                url=str(url),
                headers=_forwarded_request_headers(request),
                body=body,
                timeout_s=timeout_s,
                connect_timeout_s=connect_s,
                queue_timeout_s=queue_s,
                relay_id=self.relay_id,
            )
        except RelayError as exc:
            record.finish(error=exc.code)
            raise

        try:
            frame = await stream.next_frame(queue_s)
            if frame is None:
                await stream.cancel()
                raise relay_error(
                    "timeout",
                    f"no relay of the group picked the request up within {int(queue_s)} seconds",
                    group_name=self.group_name,
                    request=request,
                )
            if frame.type == "claimed":
                frame = await stream.next_frame(timeout_s + (connect_s or 0) + 5)
                if frame is None:
                    await stream.cancel()
                    raise relay_error("timeout", "no response from the destination", group_name=self.group_name, request=request)
            if frame.type == "error":
                stream.finished = True
                raise relay_error(
                    str(frame.header.get("code") or "protocol_error"),
                    str(frame.header.get("message") or "") or None,
                    group_name=self.group_name,
                    request=request,
                )
            if frame.type != "response_head":
                await stream.cancel()
                raise relay_error("protocol_error", f"unexpected '{frame.type}' frame", group_name=self.group_name, request=request)
        except RelayError as exc:
            record.finish(error=exc.code, relay_id=stream.relay_id)
            raise
        except BaseException:
            await stream.cancel()
            record.finish(error="cancelled", relay_id=stream.relay_id)
            raise

        status = int(frame.header["status"])
        return httpx.Response(
            status,
            headers=_response_headers(frame.header.get("headers") or []),
            stream=_RelayResponseStream(
                stream, record, status=status, read_timeout=timeout_s, group_name=self.group_name
            ),
            request=request,
            extensions={
                "http_version": b"HTTP/1.1",
                "relay_group": self.group_name or str(self.group_id),
                "relay_id": stream.relay_id,
            },
        )


# ── Diagnostics ─────────────────────────────────────────────────────────────


async def test_destination(
    group_id: Any,
    url: str,
    *,
    group_name: str | None = None,
    allowed_destinations: list[str] | None = None,
    timeout_s: float = 15.0,
    relay_id: Any = None,
) -> dict[str, Any]:
    """Send one ``GET`` through the group (or one of its relays) and say what happened.

    Nothing of the response body is kept.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return {"ok": False, "error_code": "destination_denied", "message": "Enter an http(s) URL"}
    transport = RelayTransport(
        group_id=group_id,
        group_name=group_name,
        allowed_destinations=allowed_destinations,
        source_type="test",
        relay_id=relay_id,
    )
    started = time.monotonic()
    try:
        async with (
            httpx.AsyncClient(transport=transport, timeout=timeout_s) as client,
            client.stream("GET", url) as response,
        ):
            return {
                "ok": True,
                "status_code": response.status_code,
                "duration_ms": int((time.monotonic() - started) * 1000),
                "relay_id": response.extensions.get("relay_id"),
            }
    except RelayError as exc:
        return {
            "ok": False,
            "error_code": exc.code,
            "message": str(exc),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
