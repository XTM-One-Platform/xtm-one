# SPDX-License-Identifier: Apache-2.0

"""Why this web worker stops, and the agent turns it runs when it does.

uvicorn's bounded drain cancels the requests still running the same way,
whatever stopped the worker, so a turn it cuts cannot tell from its own
cancellation why it was cut. Two things stop a web worker:

- its own recycle (``core/worker_recycle.py``): it served its ``MAX_REQUESTS``
  quota; the pod stays up and nothing outside the process sees it;
- a stop signal from its host (SIGTERM / SIGINT): a deployment, a scale-in, a
  node drain, an eviction, a restart.

:func:`note_shutdown` records the first one (the recycle calls it before it
signals itself); :func:`install_stop_signal_hook` records the second by
chaining uvicorn's own handler, and logs at WARNING the turns the stop is about
to cut. :func:`cut_cause` is what a turn cut by the drain says it was cut by
(``services/interrupted_turn.py``).

The turns this process runs are counted here: every turn that holds a
conversation's lock (``chat_turn_guard`` calls :func:`turn_started` /
:func:`turn_ended`), plus the background turns ``core/shutdown_finalizers``
tracks. The recycle waits for them (:func:`agent_work_in_flight`).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
import os
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.core.shutdown_finalizers import SHUTDOWN_DRAIN_SECONDS, background_turns_in_flight
from app.core.utils import GRACEFUL_SHUTDOWN_CANCEL_HINT

logger = logging.getLogger(__name__)

#: Once a stop was asked, the part of the grace a checkpointed turn leaves
#: unused: past ``GRACEFUL_TIMEOUT`` minus this (at most half the
#: grace), its agent loop stops at the next iteration boundary - the tool call
#: in flight finished, the checkpoint holding it - instead of being cancelled
#: in the middle of a call when the grace runs out.
WIND_DOWN_MARGIN_SECONDS = 60.0
#: The cancellation a wound-down turn ends with: the graceful shutdown's own
#: message, so every handler of a cut turn (the stored reply, the memory note,
#: the flushed checkpoint, the interrupted terminal) takes it as one.
WIND_DOWN_CANCEL_MESSAGE = f"Task cancelled, {GRACEFUL_SHUTDOWN_CANCEL_HINT} (wound down at an iteration boundary)"


class TurnWoundDown(asyncio.CancelledError):
    """A checkpointed turn stopped at an iteration boundary because its worker is stopping."""

#: The worker recycled itself after its request quota (``core/worker_recycle``).
RECYCLE = "recycle"
#: Its host asked it to stop with a signal.
STOP = "stop"

#: The validated ``GRACEFUL_TIMEOUT`` ``start-uvicorn.sh`` hands its workers.
GRACEFUL_TIMEOUT_ENV = "XTM_ONE_GRACEFUL_TIMEOUT"

#: How many running turns a log line names before it sums the rest up.
_MAX_NAMED_TURNS = 10
_STOP_SIGNALS = ("SIGTERM", "SIGINT")
_HOOK_MARK = "_xtm_one_stop_hook"


@dataclass(frozen=True)
class ShutdownRequest:
    reason: str
    #: What asked for it: the signal's name, or the requests a recycle served.
    detail: str
    #: ``time.monotonic()`` when it was asked for.
    requested_at: float


@dataclass(frozen=True)
class RunningTurn:
    conversation_id: str
    #: ``time.monotonic()`` when the turn took its lock.
    started_at: float


_request: ShutdownRequest | None = None
_turns: dict[str, RunningTurn] = {}
#: The callbacks :func:`on_stop` registered, each with the event loop it runs on.
_stop_listeners: dict[int, tuple[asyncio.AbstractEventLoop, Callable[[], None]]] = {}
_listener_ids = itertools.count(1)


def note_shutdown(reason: str, detail: str) -> ShutdownRequest:
    """Record why this worker stops; the first request wins and tells the :func:`on_stop` listeners."""
    global _request
    if _request is None:
        _request = ShutdownRequest(reason=reason, detail=detail, requested_at=time.monotonic())
        _notify_stop()
    return _request


def on_stop(callback: Callable[[], None]) -> Callable[[], None]:
    """Call *callback* on the running event loop once this worker is asked to stop; returns its unregister.

    Scheduled with ``call_soon_threadsafe``, so a stop recorded from a signal
    handler runs it on the loop, never inside the handler. A stop asked
    already schedules it at once. Outside a running loop nothing is
    registered and the unregister does nothing.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return lambda: None
    token = next(_listener_ids)
    _stop_listeners[token] = (loop, callback)
    if _request is not None:
        _schedule(loop, callback)

    def _unregister() -> None:
        _stop_listeners.pop(token, None)

    return _unregister


def _notify_stop() -> None:
    for loop, callback in list(_stop_listeners.values()):
        _schedule(loop, callback)


def _schedule(loop: asyncio.AbstractEventLoop, callback: Callable[[], None]) -> None:
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(_guarded, callback)


def _guarded(callback: Callable[[], None]) -> None:
    try:
        callback()
    except Exception:
        logger.warning("A stop listener failed", exc_info=True)


def shutdown_request() -> ShutdownRequest | None:
    return _request


def cut_cause() -> str | None:
    """:data:`RECYCLE`, :data:`STOP`, or ``None`` when the worker does not know."""
    return _request.reason if _request is not None else None


def drain_cut_cause() -> str:
    """The cause of a turn uvicorn's graceful drain cut: :func:`cut_cause`, :data:`STOP` when unknown.

    Only the drain of a stopping server raises its cancellation
    (``is_graceful_shutdown_cancellation``), so a cut it ends is a platform
    stop even when the stop hook did not record why (a supervisor that does
    not chain the signal, a stop asked by other means).
    """
    return cut_cause() or STOP


def turn_started(conversation_id: str, token: str) -> None:
    _turns[token] = RunningTurn(conversation_id=conversation_id, started_at=time.monotonic())


def turn_ended(token: str) -> None:
    _turns.pop(token, None)


def running_turns() -> list[RunningTurn]:
    """The turns holding a conversation's lock in this process, oldest first."""
    return sorted(_turns.values(), key=lambda turn: turn.started_at)


def agent_work_in_flight() -> int:
    """The turns this process runs: lock holders and background turns."""
    return len(_turns) + background_turns_in_flight()


def describe_turns(turns: list[RunningTurn], now: float | None = None) -> str:
    """``conversation <id> (running 52 min), ...`` for a log line."""
    if not turns:
        return "none"
    moment = time.monotonic() if now is None else now
    named = [
        f"conversation {turn.conversation_id} (running {_duration(moment - turn.started_at)})"
        for turn in turns[:_MAX_NAMED_TURNS]
    ]
    if len(turns) > _MAX_NAMED_TURNS:
        named.append(f"and {len(turns) - _MAX_NAMED_TURNS} more")
    return ", ".join(named)


def _duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    if seconds < 120:
        return f"{seconds:.0f} s"
    if seconds < 7200:
        return f"{seconds / 60:.0f} min"
    return f"{seconds / 3600:.1f} h"


def graceful_timeout(environ: dict[str, str] | None = None) -> float | None:
    """The drain uvicorn gives running requests on a shutdown, ``None`` unknown."""
    raw = (os.environ if environ is None else environ).get(GRACEFUL_TIMEOUT_ENV, "").strip()
    if not raw.isascii() or not raw.isdigit():
        return None
    return float(raw)


def wind_down_due(now: float | None = None) -> bool:
    """Whether a checkpointed turn should stop at its next iteration boundary.

    A stop (or a recycle) was asked and what is left of the grace is within
    :data:`WIND_DOWN_MARGIN_SECONDS` (or half the grace, the smaller); with no
    known grace, as soon as it was asked. Before that, the turn goes on: it may
    well finish its answer within the grace.
    """
    request = _request
    if request is None:
        return False
    timeout = graceful_timeout()
    if timeout is None:
        return True
    margin = min(WIND_DOWN_MARGIN_SECONDS, timeout / 2)
    moment = time.monotonic() if now is None else now
    return moment >= request.requested_at + timeout - margin


def background_grace_deadline() -> float | None:
    """When the background turns' grace ends: the requests' drain, from the request.

    ``None`` when the worker does not know when it was asked to stop or how long
    uvicorn's drain lasts: the shutdown drain then cancels them at once.
    """
    timeout = graceful_timeout()
    if _request is None or timeout is None:
        return None
    return _request.requested_at + timeout


def log_stop_requested() -> None:
    """The WARNING a stop signal leaves, naming the turns it is about to cut."""
    request = _request
    if request is None or request.reason != STOP:
        return
    turns = running_turns()
    background = background_turns_in_flight()
    timeout = graceful_timeout()
    if not turns and not background:
        logger.warning(
            "Web worker (pid %d) was asked to stop by %s (a deployment, a scale-in, "
            "a node drain or a restart); no agent turn is running in it",
            os.getpid(),
            request.detail,
        )
        return
    grace = f"{timeout:.0f} s (GRACEFUL_TIMEOUT)" if timeout is not None else "uvicorn's graceful timeout"
    margin = (
        f"; the pod's terminationGracePeriodSeconds must exceed "
        f"{timeout + SHUTDOWN_DRAIN_SECONDS:.0f} s or the interrupted replies are lost too"
        if timeout is not None
        else ""
    )
    logger.warning(
        "Web worker (pid %d) was asked to stop by %s (a deployment, a scale-in, a "
        "node drain or a restart) while it runs %d chat turn(s) [%s] and %d "
        "background agent turn(s): they have %s to finish, then they are cut and "
        "store an interrupted reply%s",
        os.getpid(),
        request.detail,
        len(turns),
        describe_turns(turns),
        background,
        grace,
        margin,
    )


def install_stop_signal_hook() -> None:
    """Record a stop signal before uvicorn's own handler sees it.

    Chains the handler uvicorn installed (``Server.capture_signals``) for
    SIGTERM and SIGINT, in the main thread only. The chained handler records
    :data:`STOP` and schedules :func:`log_stop_requested` on the event loop (a
    signal handler must not log: it may interrupt a write of the logging lock's
    own thread), then calls uvicorn's handler unchanged. A signal with no
    Python handler is left alone; installing twice changes nothing.
    """
    if threading.current_thread() is not threading.main_thread():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for name in _STOP_SIGNALS:
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        previous = signal.getsignal(signum)
        if not callable(previous) or getattr(previous, _HOOK_MARK, False):
            continue
        signal.signal(signum, _chained(previous, loop))


def _chained(
    previous: Callable[[int, Any], Any], loop: asyncio.AbstractEventLoop
) -> Callable[[int, Any], Any]:
    def _handler(signum: int, frame: Any) -> Any:
        if _request is None:
            note_shutdown(STOP, signal.Signals(signum).name)
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(log_stop_requested)
        return previous(signum, frame)

    setattr(_handler, _HOOK_MARK, True)
    return _handler
