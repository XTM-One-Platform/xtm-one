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

"""A note written on a held work item reaches the run that holds it.

A run working a work item read the item once, at its start. A note another
run (or a person) adds to that item afterwards ("duplicate of X, push
nothing", "COLLISION: #N already open") stayed in the stored description,
unseen by the holder until its next ``get_work_item``, which it had no reason
to call: the holder pushed duplicate work (xtm-one #10328 and #10329, one fix
opened twice while the collision note sat on both items).

- **live**: a run triggered by a work item opens a session around its agent
  loop (``session``): a marker keyed by the run id names the item, refreshed
  while the loop runs.
- **offer**: ``update_work_item`` offers every note written on an item to the
  run holding it (``offer``), unless the holder wrote the note itself. The
  note is pushed to the session's inbox only while the marker is there.
- **inject**: a bridge moves the inbox into the loop's steering queue; the
  loop injects each note as a user steer at its next iteration boundary,
  marked as a note on its held item.

- **unseen**: a relayed note stays pending for its holder run until the loop
  injects it or the run reads the item again (``get_work_item``); a terminal
  write of that run on the item is refused while one is pending
  (``unseen_notes``), so a "do not close" written a second before the
  completion is never finalised over (#2266).

A note offered when the holder's session is closed (or the store is
unreachable) is not relayed: it stays on the item, as before, for the next
run to read. Nothing is lost and nothing changes for an unheld item.

A STOP (the item finished, reset or taken over under its live holder) is
relayed to any live session of the holder run, whatever item that session
was opened on (``offer(..., any_item=True)``): a run holds items it claimed
mid-run too, and a background task opens a session on its own run ids with
no item (``session_for_runs``), so the stop ends its pause and reaches its
next iteration (#11751).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any

from app.core.redis import redis_client
from app.core.utils import format_exception_message
from app.tools.agent_loop import SteeringMessage, SteeringQueue

logger = logging.getLogger(__name__)

#: How long a live marker holds without a refresh (a worker that died mid-run).
LIVE_TTL_SECONDS = 90
#: How long an inbox entry waits for the bridge at most.
INBOX_TTL_SECONDS = 600
#: How often the bridge moves the inbox into the loop and refreshes the marker.
POLL_SECONDS = 1.0
#: Notes one run takes in by steering at most; the next ones stay on the item.
MAX_STEERED_NOTES = 20

_KEY_PREFIX = "work_item_note_steering"


def pending_key(run_id: str) -> str:
    return f"{_KEY_PREFIX}:pending:{run_id}"


def live_key(run_id: str) -> str:
    return f"{_KEY_PREFIX}:live:{run_id}"


def inbox_key(run_id: str) -> str:
    return f"{_KEY_PREFIX}:inbox:{run_id}"


def steer_text(entry: dict) -> str:
    """The steer the loop injects for one note on the held item."""
    title = str(entry.get("title") or "").strip()
    item = str(entry.get("item") or "").strip()
    note = str(entry.get("note") or "").strip()
    named = f"{title} ({item})" if title else item
    return (
        f"[New note on the work item you hold, {named}, written by another writer while you work on it]\n"
        f"{note}\n\n"
        "Read it before your next write on this item or its repository: it may change what you should do."
    )


def _decode(raw: object) -> dict:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        data = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        data = None
    return data if isinstance(data, dict) else {}


async def offer(
    item_id: object,
    holder_run_id: object,
    note: str | None,
    *,
    title: str | None = None,
    writer_run_ids: Sequence[object] | None = None,
    any_item: bool = False,
) -> bool:
    """Push *note* to the live run holding *item_id*; ``True`` when it was pushed.

    ``False`` when there is nothing to relay (no note, no holder, the holder
    wrote it, its session is closed, the store is unreachable): the note stays
    on the item only, as before. With *any_item* (a stop), a live session of
    the holder opened on another item, or on none, takes it too. Never raises.
    """
    if not note or not str(note).strip() or not holder_run_id or not item_id:
        return False
    holder = str(holder_run_id)
    if any(str(r) == holder for r in (writer_run_ids or ()) if r):
        return False
    try:
        redis = await redis_client()
        live = _decode(await redis.get(live_key(holder)))
        if not live or (not any_item and str(live.get("item") or "") != str(item_id)):
            return False
        entry = json.dumps(
            {"id": uuid.uuid4().hex, "item": str(item_id), "title": title or "", "note": str(note)},
            default=str,
        )
        inbox = inbox_key(holder)
        await redis.rpush(inbox, entry)
        await redis.expire(inbox, INBOX_TTL_SECONDS)
        if await redis.get(live_key(holder)):
            await _mark_pending(redis, holder, entry)
            return True
        # The session closed between the read and the push: take the entry
        # back (the note stays on the item), unless the close popped it.
        return not await redis.lrem(inbox, 1, entry)
    except Exception as exc:
        logger.warning(
            "Work item note steering: could not offer a note on %s to its holder run %s: %s",
            item_id,
            holder,
            format_exception_message(exc),
        )
        return False


async def _mark_pending(redis: Any, holder: str, entry: str) -> None:
    """Record *entry* as a note its *holder* run has not seen yet; never raises."""
    try:
        data = _decode(entry)
        key = pending_key(holder)
        await redis.hset(key, str(data.get("id") or ""), entry)
        await redis.expire(key, INBOX_TTL_SECONDS)
    except Exception as exc:
        logger.debug("Work item note steering: could not mark a note pending: %s", format_exception_message(exc))


async def mark_seen(
    run_ids: Sequence[object], *, item_id: object = None, entry_ids: Sequence[str] | None = None
) -> None:
    """Drop the pending notes *run_ids* have now seen; never raises.

    *entry_ids* names the notes the loop injected; without them every pending
    note on *item_id* goes (the run read the item again).
    """
    runs = [str(r) for r in run_ids if r]
    if not runs or (entry_ids is None and not item_id):
        return
    try:
        redis: Any = await redis_client()  # redis stub: sync|async union
        for run in runs:
            key = pending_key(run)
            if entry_ids is not None:
                if entry_ids:
                    await redis.hdel(key, *entry_ids)
                continue
            stored = await redis.hgetall(key) or {}
            gone = [
                field.decode() if isinstance(field, bytes) else str(field)
                for field, raw in stored.items()
                if str(_decode(raw).get("item") or "") == str(item_id)
            ]
            if gone:
                await redis.hdel(key, *gone)
    except Exception as exc:
        logger.debug("Work item note steering: could not mark notes seen: %s", format_exception_message(exc))


async def unseen_notes(run_ids: Sequence[object], item_id: object) -> list[str]:
    """The notes on *item_id* relayed to *run_ids* that they have not seen yet.

    Empty when there are none or the store is unreachable (the write is not
    held up by the relay being down). Never raises.
    """
    runs = [str(r) for r in run_ids if r]
    if not runs or not item_id:
        return []
    notes: list[str] = []
    try:
        redis: Any = await redis_client()  # redis stub: sync|async union
        for run in runs:
            for raw in (await redis.hgetall(pending_key(run)) or {}).values():
                entry = _decode(raw)
                if str(entry.get("item") or "") == str(item_id) and str(entry.get("note") or "").strip():
                    notes.append(str(entry["note"]).strip())
    except Exception as exc:
        logger.debug("Work item note steering: could not read unseen notes: %s", format_exception_message(exc))
        return []
    return notes


class HeldItemNoteQueue(SteeringQueue):
    """The steering queue of a run holding a work item.

    Not an interactive chat turn: no person waits on a reply, so the loop
    keeps the park semantics of an assignment run (``interactive`` false).
    A note the loop takes in is no longer pending for its run (#2266).
    """

    interactive = False

    def __init__(self, run_ids: Sequence[str] = ()) -> None:
        super().__init__()
        self._seen: set[str] = set()
        self._entry_of: dict[int, str] = {}
        self.run_ids = list(run_ids)
        self.taken = 0

    async def admit(self, items: list[str | SteeringMessage]) -> list[str | SteeringMessage]:
        injected = [self._entry_of.pop(id(item)) for item in items if id(item) in self._entry_of]
        if injected:
            await mark_seen(self.run_ids, entry_ids=injected)
        return items

    def put_entry(self, entry: dict) -> bool:
        """Queue one note entry (a known one is skipped); ``False`` past the cap."""
        entry_id = str(entry.get("id") or "")
        if entry_id and entry_id in self._seen:
            return True
        if self.taken >= MAX_STEERED_NOTES:
            return False
        if entry_id:
            self._seen.add(entry_id)
        self.taken += 1
        message = SteeringMessage(text=steer_text(entry))
        if entry_id:
            self._entry_of[id(message)] = entry_id
        self.put_nowait(message)
        return True


class _Session:
    def __init__(self, run_ids: Sequence[str], item_id: str | None) -> None:
        self.run_ids = list(dict.fromkeys(run_ids))
        self.run_id = ", ".join(self.run_ids)
        self.item_id = item_id or ""
        self.queue = HeldItemNoteQueue(self.run_ids)

    def marker(self, run_id: str) -> str:
        return json.dumps({"run": run_id, "item": self.item_id})

    async def pump(self) -> None:
        redis = await redis_client()
        for run_id in self.run_ids:
            await redis.set(live_key(run_id), self.marker(run_id), ex=LIVE_TTL_SECONDS)
            inbox = inbox_key(run_id)
            while True:
                raw = await redis.lpop(inbox)  # type: ignore[misc]  # redis stub: sync|async union
                if raw is None:
                    break
                entry = _decode(raw)
                if entry:
                    self.queue.put_entry(entry)

    async def close(self) -> None:
        """Delete the markers and the inboxes: a note not taken stays on the item."""
        try:
            redis = await redis_client()
            await redis.delete(*(key for r in self.run_ids for key in (live_key(r), inbox_key(r), pending_key(r))))
        except Exception as exc:
            logger.warning(
                "Work item note steering: could not close the session of run %s (its marker expires): %s",
                self.run_id,
                format_exception_message(exc),
            )


def held_item_of(payload: object) -> str | None:
    """The work item a run's trigger payload names, else ``None``."""
    if not isinstance(payload, dict):
        return None
    item_id = payload.get("work_item_id")
    if not item_id and isinstance(payload.get("work_item"), dict):
        item_id = payload["work_item"].get("id")
    return str(item_id) if item_id else None


@contextlib.asynccontextmanager
async def session(run_id: object, payload: object) -> AsyncIterator[HeldItemNoteQueue | None]:
    """Open the note session of a run triggered by a work item.

    Yields the loop's steering queue, or ``None`` when the run names no work
    item or the store is unreachable (no relay, the notes stay on the item).
    """
    item_id = held_item_of(payload)
    if not run_id or not item_id:
        yield None
        return
    async with _open(_Session([str(run_id)], item_id)) as queue:
        yield queue


@contextlib.asynccontextmanager
async def session_for_runs(run_ids: Sequence[object]) -> AsyncIterator[HeldItemNoteQueue | None]:
    """Open a session on *run_ids* with no item: only a stop on an item they hold reaches it.

    For a background task (its task id and its child run, either of which
    holds the items it claims). Yields ``None`` when no run id is given or
    the store is unreachable.
    """
    ids = [str(r) for r in run_ids if r]
    if not ids:
        yield None
        return
    async with _open(_Session(ids, None)) as queue:
        yield queue


@contextlib.asynccontextmanager
async def _open(steering: _Session | None) -> AsyncIterator[HeldItemNoteQueue | None]:
    if steering is not None:
        try:
            await steering.pump()
        except Exception as exc:
            logger.warning(
                "Work item note steering: could not open the session of run %s (no relay): %s",
                steering.run_id,
                format_exception_message(exc),
            )
            steering = None
    if steering is None:
        yield None
        return

    async def _bridge() -> None:
        while True:
            await asyncio.sleep(POLL_SECONDS)
            try:
                await steering.pump()
            except Exception as exc:
                logger.debug("Work item note steering: inbox poll failed (retrying): %s", format_exception_message(exc))

    bridge = asyncio.create_task(_bridge())
    try:
        yield steering.queue
    finally:
        bridge.cancel()
        with contextlib.suppress(BaseException):
            await bridge
        await steering.close()
        if steering.queue.taken:
            logger.info(
                "Work item note steering: run %s took %d note(s) on item %s in mid-run",
                steering.run_id,
                steering.queue.taken,
                steering.item_id,
            )
