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

"""One engine-run turn, one writer.

The orchestration engine can take an execution back from a worker that is
still running it: a lease that expired while its heartbeats failed, a worker
the reconciler presumed gone. The execution goes back to ``pending`` and
another worker claims it and resumes the turn from its checkpoint, while the
first worker's agent loop runs on. Its last write is refused by the lease
check, but every tool call in between ran twice: two commits, two comments,
two answers.

The reconciler binds a check of the execution row around ``adapter.execute``
(:func:`single_writer`) for the adapters whose executions run agent turns
(``OrchestrationAdapter.single_writer_turns``); the agent loop asks it before
every tool round (:func:`ensure_single_writer`). A turn that is no longer its
execution's single writer stops there with :class:`TurnSuperseded`, runs no
further call and writes nothing: the attempt that took it over owns the turn.

Fail-open, like the lease heartbeat: a check that cannot be read lets
the turn go on. "Could not tell" is not "not ours".
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: Why the turn is no longer its execution's single writer, or ``None`` when it still is.
WriterCheck = Callable[[], Awaitable[str | None]]

#: What a superseded turn's cancellation says.
SUPERSEDED_MESSAGE = "Its execution is another worker's now: the turn stops before its next tool round"


class TurnSuperseded(asyncio.CancelledError):
    """The turn's execution was taken over by another attempt: it stops, and writes nothing.

    A cancellation, like ``worker_shutdown.TurnWoundDown``: no failure handler
    of the turn takes it for an error to record, and the reconciler, which
    knows the execution is another worker's, absorbs it.
    """


_check: ContextVar[WriterCheck | None] = ContextVar("turn_writer_check", default=None)


@contextmanager
def single_writer(check: WriterCheck) -> Iterator[None]:
    """Within the block, :func:`ensure_single_writer` asks *check*."""
    token = _check.set(check)
    try:
        yield
    finally:
        _check.reset(token)


def bound() -> bool:
    """Whether a single-writer check is bound to the running turn."""
    return _check.get() is not None


async def superseded_reason() -> str | None:
    """Why the running turn is no longer its execution's single writer, ``None`` while it is (#11802).

    The check before each write call: a round's calls run one after another,
    so a later call of a round the check passed can start after another
    attempt took the execution over. The loop answers such a call unrun
    instead of raising mid-round, and the next round's
    :func:`ensure_single_writer` stops the turn. Fail-open, like it.
    """
    check = _check.get()
    if check is None:
        return None
    try:
        return await check()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "The single-writer check of this write could not be read (%s); the write goes on",
            format_exception_message(exc),
        )
        return None


def superseded_write_refusal(tool_name: str, reason: str) -> str:
    """The answer a write call gets when its attempt was taken over before it ran."""
    return (
        f"Not run: this {tool_name} call belongs to an attempt another attempt took over ({reason}); "
        "the attempt that goes on with the run owns its writes."
    )


async def ensure_single_writer() -> None:
    """Raise :class:`TurnSuperseded` when the running turn is no longer its execution's single writer.

    A no-op for a turn no engine execution backs (a web chat turn, a consult
    run in its caller's process) and when the check cannot be read.
    """
    check = _check.get()
    if check is None:
        return
    try:
        reason = await check()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "The single-writer check of this turn could not be read (%s); the turn goes on",
            format_exception_message(exc),
        )
        return
    if reason is None:
        return
    logger.warning("%s: %s", SUPERSEDED_MESSAGE, reason)
    raise TurnSuperseded(f"{SUPERSEDED_MESSAGE}: {reason}")
