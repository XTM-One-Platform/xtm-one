# SPDX-License-Identifier: Apache-2.0

"""Operator notices said once for the whole platform per interval.

A notice about a standing configuration - custom tools in the subprocess
fallback, an undeclared reverse proxy, a webhook accepting deliveries it cannot
verify - asks an operator to act once. Latched per process, it is said again by
every uvicorn worker (``WEB_WORKERS``), after every ``MAX_REQUESTS`` recycle and
on every replica. :func:`log_once` (and :func:`log_once_soon` for a synchronous
caller) claim the notice in Redis for the interval with ``SET NX EX``, under a
key carrying the deployed version so a new release says it again: the process
that wins says it, with the number of times it came up since the last line
anywhere on the platform; the others add theirs to that count and stay silent.
A caller ends its key with the :func:`fingerprint` of the configuration the
notice states, so changing that configuration says it again at once.

Each process asks Redis at most once per interval and key, and counts the
occurrences in between itself, so a notice met on every request costs one round
trip per interval. Fail-open: when Redis does not answer within
``_REDIS_TIMEOUT_SECONDS``, the process says it itself - a notice repeated is
better than a notice lost. For the same reason a decision, once started, is
never dropped with a cancelled caller: it finishes on its own, and one the event
loop cancels (the process is going away) releases the claim it took.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from app.core.config import settings
from app.core.redis import redis_client
from app.core.utils import format_exception_message

_log = logging.getLogger(__name__)

KEY_PREFIX = "log_once"
HELD_BACK_SUFFIX = " (%d more since the last one)"

#: The client retries a timed-out command, so its own socket timeouts would let
#: a Redis that never answers hold a notice far longer than this.
_REDIS_TIMEOUT_SECONDS = 2.0
_MAX_TRACKED_KEYS = 4096

_RELEASE_IF_OWNER_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)

#: Where the claims are made; ``None`` decides in this process alone.
shared_store: Callable[[], Awaitable[Any]] | None = redis_client

_lock = threading.Lock()
#: Per key: when this process may ask again, and the occurrences it counted since it last asked.
_windows: dict[str, tuple[float, int]] = {}
#: Kept referenced: the event loop holds only weak references to its tasks.
_tasks: set[asyncio.Task[bool]] = set()


def fingerprint(*parts: object) -> str:
    """A short token for the configuration a notice states, to end its key with.

    *parts* must read the same in every process: strings, numbers, booleans,
    ``None`` and lists or tuples of them (sort a set first).
    """
    return hashlib.sha256(repr(parts).encode("utf-8", "backslashreplace")).hexdigest()[:12]


def _take_turn(key: str, interval: float) -> int | None:
    """The occurrences held back here when this call asks for the notice, ``None`` when it is only counted."""
    now = time.monotonic()
    with _lock:
        window = _windows.get(key)
        if window is not None and now < window[0]:
            _windows[key] = (window[0], window[1] + 1)
            return None
        if window is None and len(_windows) >= _MAX_TRACKED_KEYS:
            del _windows[min(_windows, key=lambda k: _windows[k][0])]
        _windows[key] = (now + interval, 0)
        return window[1] if window is not None else 0


def _give_back(key: str, held: int) -> None:
    """Undo a turn whose decision never ran, so the next occurrence asks again."""
    with _lock:
        window = _windows.get(key)
        _windows[key] = (0.0, held + 1 + (window[1] if window is not None else 0))


def _as_int(value: object) -> int:
    if isinstance(value, bytes):
        value = value.decode("ascii", "replace")
    try:
        return max(0, int(value))  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0


def _claim_key(key: str) -> str:
    return f"{KEY_PREFIX}:{settings.app_version}:{key}"


async def _claim(store: Callable[[], Awaitable[Any]], key: str, interval: float, held: int, token: str) -> int | None:
    """The occurrences to report when this process says the notice, ``None`` when another one did."""
    redis = await store()
    claim_key = _claim_key(key)
    held_key = f"{claim_key}:held"
    ttl = max(1, math.ceil(interval))
    won = await redis.set(claim_key, token, nx=True, ex=ttl)
    # Once the claim is decided, a failing count loses the count, never the decision.
    others = 0
    with contextlib.suppress(Exception):
        if won:
            others = _as_int(await redis.getdel(held_key))
        else:
            await redis.incrby(held_key, held + 1)
            await redis.expire(held_key, 3 * ttl)
    return held + others if won else None


async def _release(store: Callable[[], Awaitable[Any]], key: str, token: str) -> None:
    async def _delete_if_owner() -> None:
        redis = await store()
        await redis.eval(_RELEASE_IF_OWNER_LUA, 1, _claim_key(key), token)

    try:
        await asyncio.wait_for(_delete_if_owner(), _REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        _log.debug("Could not release the notice %s (its claim expires on its own): %s", key, format_exception_message(exc))


def _emit(logger: logging.Logger, level: int, message: str, args: tuple[object, ...], held: int) -> None:
    if held:
        message += HELD_BACK_SUFFIX
        args = (*args, held)
    logger.log(level, message, *args)


async def _say_if_claimed(
    store: Callable[[], Awaitable[Any]],
    logger: logging.Logger,
    level: int,
    key: str,
    interval: float,
    message: str,
    args: tuple[object, ...],
    held: int,
) -> bool:
    token = uuid.uuid4().hex
    try:
        reported = await asyncio.wait_for(_claim(store, key, interval, held, token), _REDIS_TIMEOUT_SECONDS)
    except asyncio.CancelledError:
        # Callers are shielded from this task: only the event loop cancels it.
        # The SET may have landed with nobody left to say the notice.
        await _release(store, key, token)
        _give_back(key, held)
        raise
    except Exception as exc:
        _log.debug("Could not claim the notice %s in Redis; saying it here: %s", key, format_exception_message(exc))
        reported = held
    if reported is None:
        return False
    _emit(logger, level, message, args, reported)
    return True


def _start(loop: asyncio.AbstractEventLoop, decision: Coroutine[Any, Any, bool]) -> asyncio.Task[bool]:
    task = loop.create_task(decision)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def log_once(
    logger: logging.Logger, level: int, key: str, message: str, *args: object, interval: float
) -> bool:
    """Log *message* at most once per *interval* for *key* across the platform; whether this call said it."""
    held = _take_turn(key, interval)
    if held is None:
        return False
    store = shared_store
    if store is None:
        _emit(logger, level, message, args, held)
        return True
    decision = _start(
        asyncio.get_running_loop(), _say_if_claimed(store, logger, level, key, interval, message, args, held)
    )
    return await asyncio.shield(decision)


def log_once_soon(
    logger: logging.Logger, level: int, key: str, message: str, *args: object, interval: float
) -> None:
    """:func:`log_once` for a synchronous caller: the claim runs in a task, never on the caller's path.

    Outside an event loop the process decides alone.
    """
    held = _take_turn(key, interval)
    if held is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    store = shared_store
    if store is None or loop is None:
        _emit(logger, level, message, args, held)
        return
    _start(loop, _say_if_claimed(store, logger, level, key, interval, message, args, held))


async def settle() -> None:
    """Wait for the notice decisions in flight."""
    while _tasks:
        await asyncio.gather(*list(_tasks), return_exceptions=True)


def forget() -> None:
    """Forget every window this process holds."""
    with _lock:
        _windows.clear()
