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

"""The tool work of an agent account turn, kept so a turn a platform stop cuts resumes it.

An agent account turn (``pipeline._answer``) a rolling deploy cuts before it
acted is parked by ``pipeline._park_cut_turn`` and run again by the next
process. Without a checkpoint that run started over: every tool call the cut
turn had completed ran again, and a turn longer than the interval between two
builds never landed.

The turn now checkpoints its completed tool rounds at every iteration boundary
with the background tasks' writer (``background_checkpoint.TaskCheckpoint``,
Redis, 24 h), under an id derived from the account, the conversation and the
message, so the run that answers the same message again finds them
(:func:`load`), replays them before the message (as a cut web or channel turn
does) and opens its trace with ``metadata.resumed_from`` (:func:`trace_meta`).
A call the cut segment started and never saw answered is named by the resume
note, never run again blindly. The turn that ends in this process, however it
ends, deletes the checkpoint (:func:`discard`); a turn the stop cuts flushes
it (``TaskCheckpoint.aclose(flush=True)``) and leaves it to the resume.

The cut turn's trace id is kept beside the checkpoint (:func:`remember_cut_trace`),
so the resumed trace names it (``resumed_from.trace_id``) and the cut trace
names its resume (``resumed_as``), as a cut web turn's do: a reader of the cut
trace finds the resume without matching messages by hand.
"""

from __future__ import annotations

import logging
import uuid

from app.services.agent_accounts.events import AccountEvent
from app.services.background_checkpoint import (
    CHECKPOINT_TTL_SECONDS,
    RESUMED_FROM_TRACE_KEY,
    TaskCheckpoint,
    TaskReplay,
    discard_task_checkpoint,
    load_task_replay,
)

logger = logging.getLogger(__name__)

#: The namespace of the checkpoint ids of agent account turns.
_NAMESPACE = uuid.UUID("6f1c3a52-2d0e-4c55-9a51-0a8c6b1e7d21")


def checkpoint_id(account_id: str, event: AccountEvent) -> uuid.UUID:
    """The checkpoint id of the turn answering *event* on *account_id*: the same for every attempt."""
    return uuid.uuid5(_NAMESPACE, f"{account_id}:{event.conversation_id}:{event.message_id}")


async def load(account_id: str, event: AccountEvent) -> TaskReplay:
    """What an earlier, cut attempt at the same message completed; an empty replay for a first attempt."""
    replay = await load_task_replay(checkpoint_id(account_id, event))
    if replay.history:
        logger.warning(
            "Agent account %s: the turn on message %s resumes %d completed tool call(s) of the turn a platform stop cut",
            account_id,
            event.message_id,
            replay.replayed_calls,
        )
    return replay


def writer(account_id: str, event: AccountEvent, replay: TaskReplay) -> TaskCheckpoint:
    """The checkpoint writer of this attempt, carrying what the attempts before it completed.

    No acting user: the turn has no conversation of the platform to keep an
    oversized result in, so such a result is replayed cut, its start kept.
    """
    return TaskCheckpoint(
        checkpoint_id(account_id, event),
        resumed=replay.resumed if replay.history else None,
        completed_calls=replay.completed_calls,
    )


def trace_meta(
    account_id: str, event: AccountEvent, replay: TaskReplay, *, cut_trace_id: str | None = None
) -> dict[str, dict[str, object]] | None:
    """The ``metadata.resumed_from`` of the resumed turn's first trace, or ``None`` for a first attempt.

    *cut_trace_id*, the trace of the cut turn when it was kept, is written as
    ``trace_id``, the key a cut web turn's resume carries.
    """
    if not replay.history:
        return None
    meta: dict[str, object] = {
        "entry": "agent_account",
        "account_id": account_id,
        "message_id": event.message_id,
        "attempt": replay.turn_id,
        "replayed_calls": replay.replayed_calls,
        "completed_calls": replay.completed_calls,
    }
    if cut_trace_id:
        meta["trace_id"] = cut_trace_id
    return {RESUMED_FROM_TRACE_KEY: meta}


def _cut_trace_key(account_id: str, event: AccountEvent) -> str:
    return f"agent_account:cut_trace:{checkpoint_id(account_id, event)}"


async def remember_cut_trace(account_id: str, event: AccountEvent, trace_id: object) -> None:
    """Keep the trace of the turn a platform stop cut, for its resume to name. Never raises."""
    if not trace_id:
        return
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        await client.set(_cut_trace_key(account_id, event), str(trace_id), ex=CHECKPOINT_TTL_SECONDS)
    except Exception:
        logger.debug("Agent account %s: could not keep the cut turn's trace id", account_id, exc_info=True)


async def cut_trace(account_id: str, event: AccountEvent) -> str | None:
    """The trace of the cut turn this attempt resumes, when it was kept. Never raises."""
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        value = await client.get(_cut_trace_key(account_id, event))
    except Exception:
        return None
    if isinstance(value, bytes):
        value = value.decode()
    return str(value) if value else None


async def discard(account_id: str, event: AccountEvent) -> None:
    """Delete the checkpoint: the turn ended in this process, nothing is left to resume."""
    await discard_task_checkpoint(checkpoint_id(account_id, event))
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        await client.delete(_cut_trace_key(account_id, event))
    except Exception:
        logger.debug("Agent account %s: could not drop the cut turn's trace id", account_id, exc_info=True)
