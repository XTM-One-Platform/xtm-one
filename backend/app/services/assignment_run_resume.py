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

"""Checkpoint and resume of an assignment run cut by a rollout.

An assignment run that is not a background task's child runs its agent turn
directly in ``execute_assignment_run``, on the worker that claimed its
orchestration execution. A rollout, a scale-in or a node drain takes that
worker away: before, the turn was cancelled in the middle of a call, the next
attempt started again from the prompt, and every tool call it had made was
made a second time.

The run now does what a background task does (``background_checkpoint``), with the same writer, the same bounds and the same words:

* its completed tool rounds are checkpointed at every iteration boundary
  (:func:`checkpoint_for`, a Redis key per run);
* a turn whose worker is asked to stop winds down at the next boundary and is
  handed back (:func:`hand_back`): the checkpoint flushed, the hand-back
  recorded on the run, the run left ``running`` so its work item claims stay
  held by a live run, and its execution due again at once;
* the next attempt replays those rounds with the resume note
  (:func:`prepare_resume`) and records the resume on the run's
  ``trigger_context`` (``resumes``: at, attempt, cause, replayed calls,
  ``resumed_from`` / ``resumed_as``);
* at most :data:`background_checkpoint.MAX_RESUMES` resumes in a row after a
  loss without a word (a crash, an OOM kill, a lapsed lease) with no tool
  call completed between them, :data:`background_checkpoint.MAX_LOST_RESUMES`
  such resumes in all (the rule for tasks), and
  after a graceful stop (a hand-back, or a take-back from a worker
  that said it was stopping or that a deploy replaced)
  :data:`background_checkpoint.MAX_GRACEFUL_STREAK` resumes in a row with no
  tool call completed between them and
  :data:`background_checkpoint.MAX_GRACEFUL_RESUMES` in all (the same
  streak rule, so rollouts never end a run that progresses between them):
  the next loss past its bound
  fails the run with :func:`cap_reached_reason` or
  :func:`graceful_cap_reached_reason` (:func:`resume_refused`), and the
  claims it held go back to the queue.

Its first trace after a resume carries ``metadata.resumed_from`` with
``entry: "assignment_run"`` (:func:`resumed_trace_meta`).

Best-effort by contract: a checkpoint that cannot be read or written costs the
resume, never the run.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.services import background_checkpoint
from app.services.background_checkpoint import (
    COMPLETED_CALLS_KEY,
    GRACEFUL_KEY,
    HANDED_BACK_KEY,
    MAX_GRACEFUL_RESUMES,
    MAX_GRACEFUL_STREAK,
    MAX_LOST_RESUMES,
    MAX_RESUMES,
    RESUME_CAP_REACHED,
    RESUMED_FROM_TRACE_KEY,
    RESUMES_KEY,
    TaskCheckpoint,
    TaskReplay,
)

logger = logging.getLogger(__name__)

#: The old fixed floor of an assignment run's execution: its first run
#: and :data:`MAX_RESUMES` resumes. An execution created at it is a legacy row,
#: raised to :func:`execution_max_attempts` at its next claim.
MAX_ATTEMPTS = background_checkpoint.MAX_ATTEMPTS

#: What the adapter reads as "handed back to the queue": the run stays
#: ``running`` and its execution is due again at once.
RUN_HANDED_BACK = "handed_back"

HANDED_BACK_ERROR = (
    "Its worker was asked to stop (a deployment, a scale-in, a node drain): the run stopped at an "
    "iteration boundary, checkpointed, and runs again at once from that checkpoint."
)

#: The ``trigger_context`` keys this module writes; a retry run starts without them.
RESUME_STATE_KEYS = (RESUMES_KEY, HANDED_BACK_KEY)


def execution_max_attempts() -> int:
    """The ``max_attempts`` of an assignment run's execution: a background task's.

    Its first run and :data:`MAX_LOST_RESUMES` resumes after a loss without a
    word (``background_checkpoint.task_max_attempts``). Only such a loss spends
    an attempt (a hand-back defers, a take-back on its word gives it back): the
    streak of :data:`MAX_RESUMES` losses with no progress is the run's own
    refusal (:func:`resume_refused`), so the engine never dead-letters a run
    that progressed between its losses at its fourth.
    """
    from app.core.config import settings

    return background_checkpoint.task_max_attempts(settings.orchestration_default_max_attempts)


def checkpoint_id(run_id: Any) -> str:
    """The checkpoint name of an assignment run: never the id of a background task."""
    return f"assignment-run:{run_id}"


class RunCheckpoint(TaskCheckpoint):
    """:class:`TaskCheckpoint` of one attempt of a run: its secrets as markers, fenced by its place.

    A run's transcript is stored with its secret values as markers:
    so is its checkpoint, written in the run's scope (the writer task is
    created from the loop's context), and restored by the attempt that
    resumes it (:func:`restore_history`).

    An attempt the engine replaced may still be running on a live worker:
    its writes never overwrite the snapshot of a later attempt. Each
    write carries its ``(epoch, attempt)`` place and is skipped when the stored
    one is later. The comparison and the write are one Redis script
    (:data:`_WRITE_IF_NOT_LATER_LUA`), so no later attempt's write lands
    between them; the hand-back flush (``aclose(flush=True)``) writes through
    the same path.
    """

    def __init__(self, *args: Any, place: tuple[int, int] | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.place = place

    def _kept_text(self, content: str) -> str:
        """A result kept whole for the resume holds the run's secrets as markers, as its checkpoint does."""
        from app.core.redaction import redact_known_secrets

        return redact_known_secrets(content)

    async def _write(self, entries: list[dict[str, Any]]) -> None:
        from app.core.redaction import redact_known_secrets

        client = await background_checkpoint._redis()
        key = background_checkpoint.checkpoint_key(self.task_id)
        payload = json.dumps(
            {
                "turn_id": self.turn_id,
                "place": list(self.place) if self.place is not None else None,
                "updated_at": datetime.now(UTC).isoformat(),
                # The run's progress across its attempts (as for tasks).
                COMPLETED_CALLS_KEY: self.completed_calls(),
                "entries": redact_known_secrets(entries),
            },
            default=str,
        )
        if self.place is None:
            await client.set(key, payload, ex=background_checkpoint.CHECKPOINT_TTL_SECONDS)
            return
        if not await _set_if_not_later(client, key, payload, self.place):
            logger.info(
                "Assignment run checkpoint %s not written by attempt %s: a later attempt owns it, "
                "or the run ended",
                self.task_id,
                self.place,
            )


#: Write ``ARGV[1]`` at ``KEYS[1]`` (TTL ``ARGV[4]``) unless the value stored
#: there fences the writer's ``(epoch, attempt)`` place (``ARGV[2]``,
#: ``ARGV[3]``): a checkpoint written at a later place, or the tombstone of
#: an ending (``ended``) at the same or a later place. One compare-and-set,
#: atomic in Redis. A stored value with no readable place is overwritten.
#: Returns 1 when written, 0 when fenced.
_WRITE_IF_NOT_LATER_LUA = """
local raw = redis.call('GET', KEYS[1])
if raw then
  local ok, doc = pcall(cjson.decode, raw)
  if ok and type(doc) == 'table' and type(doc['place']) == 'table' then
    local epoch = tonumber(doc['place'][1]) or 0
    local attempt = tonumber(doc['place'][2]) or 0
    local own_epoch = tonumber(ARGV[2])
    local own_attempt = tonumber(ARGV[3])
    if epoch > own_epoch or (epoch == own_epoch and attempt > own_attempt) then
      return 0
    end
    if doc['ended'] == true and epoch == own_epoch and attempt == own_attempt then
      return 0
    end
  end
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[4]))
return 1
"""


async def _set_if_not_later(client: Any, key: str, payload: str, place: tuple[int, int]) -> bool:
    """Store *payload* at *key* unless a later attempt or an ending fences *place*."""
    written = await client.eval(
        _WRITE_IF_NOT_LATER_LUA,
        1,
        key,
        payload,
        place[0],
        place[1],
        background_checkpoint.CHECKPOINT_TTL_SECONDS,
    )
    return bool(int(written or 0))


def checkpoint_for(
    run_id: Any,
    *,
    replay: TaskReplay | None,
    attempt: int | None,
    place: tuple[int, int] | None = None,
    user_id: Any = None,
) -> TaskCheckpoint:
    """The checkpoint writer of *run_id*'s turn at *place*, carrying what *replay* resumed and its count of calls.

    With the run's acting *user_id*, a hand-back keeps every oversized result
    whole as a file of that user stamped with the run, which the resumed
    attempt reads again (``load_run_generated_files``); without one, the rest
    of such a result is not kept.
    """
    return RunCheckpoint(
        checkpoint_id(run_id),
        resumed=replay.resumed if replay is not None else None,
        attempt=attempt,
        place=place,
        completed_calls=replay.completed_calls if replay is not None else 0,
        user_id=user_id,
        files_run_id=run_id if user_id else None,
    )


async def load_replay(run_id: Any, *, user_id: Any = None, code_interpreter: bool = False) -> TaskReplay:
    """The checkpointed work of *run_id*'s lost attempt: read before the resume is decided.

    Its :attr:`TaskReplay.completed_calls` is the progress the streak reads
    (:func:`resume_refused`); the same replay is handed to
    :func:`prepare_resume`, so the checkpoint is read once. With the run's
    *user_id* the resume note also reads the run's own sandbox workspace: its
    manifest and the commands the cut segment left running, with their run_ids.
    """
    return await background_checkpoint.load_task_replay(
        checkpoint_id(run_id),
        assignment_run_id=run_id if user_id else None,
        user_id=user_id,
        code_interpreter=code_interpreter,
    )


def restore_history(history: list[Any]) -> list[Any]:
    """The replayed *history* with each secret marker of the run's scope turned back into its value.

    Called where the run's prompt is rendered, its variables resolved: the
    model and the tools get the values back, as on a HITL resume.
    """
    from app.core.redaction import restore_known_secrets

    for message in history:
        if isinstance(message.content, str):
            message.content = restore_known_secrets(message.content)
        for call in message.tool_calls or ():
            call.arguments = restore_known_secrets(call.arguments)
    return history


#: A file id as a tool result names it: bare, ``file-<id>`` or ``[[FILE:<id>]]``.
_FILE_ID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

#: The most ids one resume looks up: a replay is bounded, this keeps one query small.
MAX_REPLAYED_FILE_IDS = 500


def replayed_file_ids(history: list[Any]) -> list[str]:
    """The file ids the replayed tool results of *history* name, in order, each once.

    The checkpoint keeps a result's text, never the ``files`` it carried
    (``turn_checkpoint._entries``): the ids are read from the text, where every
    file-producing tool names them (a sheet's id, a render's
    ``captions_file_id``).
    """
    seen: dict[str, None] = {}
    for message in history:
        if getattr(message, "role", None) != "tool" or not isinstance(message.content, str):
            continue
        for match in _FILE_ID_RE.findall(message.content):
            seen.setdefault(match.lower(), None)
            if len(seen) >= MAX_REPLAYED_FILE_IDS:
                return list(seen)
    return list(seen)


async def replayed_files(
    db: Any, run: Any, history: list[Any], *, exclude_ids: set[str], user_id: Any = None
) -> list[Any]:
    """The ``FileAttachment`` rows the replayed results of *history* name that *run* may read again.

    A file a replayed call produced is attached to the resumed attempt as it
    was to the lost one, with the same id. The rows
    ``load_run_generated_files`` finds are stamped with the run; a file a tool
    stored for the run's acting user (*user_id*) without that stamp (a Video
    Studio frame, contact sheet, preview or captions file) is found here: a row
    of that user or of the run itself, never a template asset, never another user's file
    whose id a result happened to quote. A row retention purged is simply not
    found and stays unreadable with the usual message. Best effort: a failed
    lookup costs the files, never the resume.
    """
    from sqlalchemy import or_

    from app.models.file_attachment import FileAttachment

    ids = [uuid.UUID(fid) for fid in replayed_file_ids(history) if fid not in exclude_ids]
    if not ids:
        return []
    scopes = [FileAttachment.assignment_run_id == run.id]
    if user_id is not None:
        scopes.append(FileAttachment.user_id == user_id)
    try:
        rows = (
            (
                await db.execute(
                    select(FileAttachment).where(
                        FileAttachment.id.in_(ids),
                        FileAttachment.template_container_id.is_(None),
                        or_(*scopes),
                    )
                )
            )
            .scalars()
            .all()
        )
    except Exception:
        logger.warning("Run %s: could not look up the files its replayed calls named", run.id, exc_info=True)
        return []
    return list(rows)


async def discard(run_id: Any, *, place: tuple[int, int] | None = None) -> None:
    """Drop *run_id*'s checkpoint: called once the run ended.

    With the ending's *place*, the checkpoint is replaced by a tombstone
    stamped with that place (in the same compare-and-set as every write), so
    a writer of a replaced attempt still in flight, or a hand-back flush,
    cannot bring the transcript back; a checkpoint a later attempt wrote is
    left to it. The tombstone holds no rounds and expires with the TTL.
    Without a place the key is deleted. Best-effort.
    """
    if place is None:
        await background_checkpoint.discard_task_checkpoint(checkpoint_id(run_id))
        return
    tombstone = json.dumps({"place": list(place), "ended": True, "updated_at": datetime.now(UTC).isoformat()})
    try:
        client = await background_checkpoint._redis()
        await _set_if_not_later(client, background_checkpoint.checkpoint_key(checkpoint_id(run_id)), tombstone, place)
    except Exception as exc:
        logger.debug("Could not drop the checkpoint of assignment run %s: %s", run_id, exc)


def handed_back_attempt(trigger_context: dict[str, Any] | None) -> int | None:
    """The attempt a run's stopping worker handed it back at, else ``None``."""
    back = (trigger_context or {}).get(HANDED_BACK_KEY)
    if not isinstance(back, dict):
        return None
    attempt = back.get("attempt")
    return attempt if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0 else None


def dead_letter_place(
    fence: tuple[int, int], *, attempts: int | None, handed_back: int | None = None
) -> tuple[int, int]:
    """The place of a dead letter's tombstone: at or after every writer of the execution it ends.

    *fence* is the latest ``(epoch, attempt)`` the run records as started
    (``attempt_fence``). A first attempt records none (``fence_attempt``
    writes nothing for it), so the fence alone reads ``(0, 0)`` while that
    attempt's writer, still winding down on its worker, writes at ``(0, 1)``.
    The engine's *attempts* count bounds the attempt of every writer the
    execution started, in the fence's epoch: the tombstone takes the later of
    the two, so no writer of the dead-lettered execution recreates the
    checkpoint.

    *handed_back* is the attempt a hand-back recorded (:func:`handed_back_attempt`):
    its deferral gives the attempt back, so a first attempt handed back and
    dead-lettered at its deadline before any claim reads *attempts* 0 while
    its flushed checkpoint is at ``(0, 1)``.
    """
    epoch, attempt = fence
    return (epoch, max(attempt, attempts or 0, handed_back or 0))


async def cancel_partner_tasks(db: Any, run_id: Any) -> None:
    """Cancel the partner tasks of a run no attempt will go on with (FR-005).

    A handed-back or lost attempt's teardown leaves them for the attempt that
    resumes it: once no attempt will (the resume cap, the dead letter), they
    are cancelled and their delegation rows concluded. Best-effort, as at any
    run's teardown.
    """
    from app.services.integration_tools import cancel_run_partner_tasks

    await cancel_run_partner_tasks(db, run_id)


def _is_graceful(resume: dict) -> bool:
    """``background_checkpoint.is_graceful``, plus an assignment run's resume recorded by an older writer.

    The older assignment-run writer called ``record_resume`` without
    ``graceful``, so a hand-back resumed then carries ``graceful: false`` with
    the hand-back's cause. The current writer records the hand-back's cause
    only when the hand-back is current (``lost_cause`` and
    :func:`next_resume_is_graceful` read the same record), so that pair exists
    in the older records alone, and is graceful.
    """
    if background_checkpoint.is_graceful(resume):
        return True
    cause = resume.get("cause")
    return (
        resume.get(GRACEFUL_KEY) is False
        and isinstance(cause, str)
        and cause.endswith(background_checkpoint._HANDED_BACK_CAUSE_END)
    )


def resume_counts(trigger_context: dict[str, Any] | None) -> tuple[int, int]:
    """The resumes a run made: ``(after a loss without a word, after a graceful stop)``.

    Read by :func:`_is_graceful`: an entry recorded before the ``graceful``
    flag existed, or by the older writer, is read by its cause.
    """
    resumes = background_checkpoint.resumes_of(trigger_context)
    graceful = sum(1 for r in resumes if _is_graceful(r))
    return len(resumes) - graceful, graceful


def next_resume_is_graceful(
    trigger_context: dict[str, Any] | None, *, attempt: int | None, taken_back_on_its_word: bool = False
) -> bool:
    """Whether *attempt*, about to resume a run, follows a graceful stop.

    The run was handed back by its stopping worker, and the hand-back is the
    one this claim resumes: the hand-back's deferral gave its attempt back, so
    this claim runs at the attempt the hand-back recorded
    (``background_checkpoint._hand_back_is_current``). Or its execution was
    taken back from a worker that said it was stopping, or that a deploy
    replaced (*taken_back_on_its_word*, ``orchestration_engine.stopped_on_its_word``). A claim lost without a word in between (a crash, an OOM kill, a
    lapsed lease) spent its attempt, so the claim after it runs at a later
    one, and its resume counts against the wordless bounds. A run left
    ``running`` with no hand-back recorded lost its worker without a word.
    """
    return background_checkpoint.next_resume_is_graceful(
        trigger_context, attempt=attempt, taken_back_on_its_word=taken_back_on_its_word
    )


def lost_cause(
    trigger_context: dict[str, Any] | None, *, attempt: int | None = None, taken_back_on_its_word: bool = False
) -> str:
    """What took the earlier attempt of a run away, as its ``trigger_context`` and the take-back say it.

    Never the engine's own word on a wordless take-back (the worker's host,
    version and memory): a run's ``trigger_context`` is read by whoever can
    see the run.
    """
    return background_checkpoint.lost_cause(
        trigger_context, attempt=attempt, taken_back_on_its_word=taken_back_on_its_word
    )


def lost_streak(trigger_context: dict[str, Any] | None) -> int:
    """The resumes after a loss without a word a run made in a row with no progress between them.

    ``background_checkpoint.lost_streak`` read with :func:`_is_graceful`, so a
    hand-back recorded by the older writer never adds to it.
    """
    streak = 0
    previous: dict | None = None
    for resume in background_checkpoint.resumes_of(trigger_context):
        if background_checkpoint._progressed(resume, previous):
            streak = 0
        if not _is_graceful(resume):
            streak += 1
        previous = resume
    return streak


def next_lost_streak(trigger_context: dict[str, Any] | None, *, completed_calls: int | None = None) -> int:
    """The streak a resume after a loss without a word would make, given the calls its replay holds.

    *completed_calls* is the count the resuming attempt loaded
    (:attr:`TaskReplay.completed_calls`): more than the last resume recorded,
    and the run progressed since, so the streak starts again at 1. Unknown
    (``None``): no progress is assumed.
    """
    resumes = background_checkpoint.resumes_of(trigger_context)
    if completed_calls is not None and background_checkpoint._progressed(
        {COMPLETED_CALLS_KEY: int(completed_calls)}, resumes[-1] if resumes else None
    ):
        return 1
    return lost_streak(trigger_context) + 1


def graceful_streak(trigger_context: dict[str, Any] | None) -> int:
    """The resumes after a graceful stop a run made in a row with no progress between them.

    ``background_checkpoint.graceful_streak`` read with :func:`_is_graceful`,
    so a hand-back recorded by the older writer adds to it too.
    """
    return background_checkpoint.graceful_streak(trigger_context, graceful=_is_graceful)


def next_graceful_streak(trigger_context: dict[str, Any] | None, *, completed_calls: int | None = None) -> int:
    """The streak a resume after a graceful stop would make, given the calls its replay holds."""
    return background_checkpoint.next_graceful_streak(
        trigger_context, completed_calls=completed_calls, graceful=_is_graceful
    )


def _both_counts(trigger_context: dict[str, Any] | None) -> str:
    lost, graceful = resume_counts(trigger_context)
    return (
        f"{lost_streak(trigger_context)} resume(s) in a row after a loss without a word with no progress "
        f"between them (the most a run makes is {MAX_RESUMES}), {lost} after a loss without a word in all "
        f"(the most a run makes is {MAX_LOST_RESUMES}), {graceful_streak(trigger_context)} in a row after a "
        f"graceful stop with no progress between them (the most a run makes is {MAX_GRACEFUL_STREAK}), "
        f"{graceful} after a graceful stop in all (the most a run makes is {MAX_GRACEFUL_RESUMES})"
    )


def cap_reached_reason(trigger_context: dict[str, Any] | None, *, cause: str | None = None) -> str:
    """What a run that lost its worker once more than it may be resumed fails with.

    The words of a background task at its cap (``background_checkpoint``),
    naming the assignment run and both counts: never the hand-back's
    "runs again at once".
    """
    lost, _graceful = resume_counts(trigger_context)
    why = cause or lost_cause(trigger_context)
    return (
        f"{RESUME_CAP_REACHED}: the assignment run lost its worker again after {lost} resume(s) "
        f"({_both_counts(trigger_context)}); last cause: {why}. It is failed rather than "
        "resumed once more, and its work item claims are released."
    )


def graceful_cap_reached_reason(
    trigger_context: dict[str, Any] | None, *, taken_back_on_its_word: bool = False, streak: bool = False
) -> str:
    """What a run stopped gracefully once more than its graceful bounds allow fails with.

    *streak*: the bound reached is :data:`MAX_GRACEFUL_STREAK` with no
    progress, else :data:`MAX_GRACEFUL_RESUMES` in all; the text names it.
    """
    _lost, graceful = resume_counts(trigger_context)
    why = lost_cause(trigger_context, taken_back_on_its_word=taken_back_on_its_word)
    bound = background_checkpoint.graceful_bound_reached(streak=streak, entry="run")
    return (
        f"{RESUME_CAP_REACHED}: the assignment run was stopped gracefully again after {graceful} graceful "
        f"resume(s), at {bound} ({_both_counts(trigger_context)}); last cause: {why}. It is "
        "failed rather than resumed once more, and its work item claims are released."
    )


def resume_refused(
    trigger_context: dict[str, Any] | None,
    *,
    attempt: int | None = None,
    completed_calls: int | None = None,
    taken_back_on_its_word: bool = False,
    graceful_streak_bound: int = MAX_GRACEFUL_STREAK,
) -> str | None:
    """Why a run left ``running`` by a lost attempt is not resumed once more, else ``None``.

    *graceful_streak_bound* replaces :data:`MAX_GRACEFUL_STREAK` for a run a
    fire of its schedule waits behind (``schedule_overlap.graceful_streak_bound``): never above it.

    After a loss without a word: :data:`MAX_RESUMES` such resumes already made
    in a row with no progress between them (:func:`next_lost_streak`, given
    the *completed_calls* of the replay the resuming attempt loaded), or
    :data:`MAX_LOST_RESUMES` in all. After a graceful stop (a hand-back, or a
    take-back on its worker's word), :data:`MAX_GRACEFUL_STREAK` such resumes
    in a row with no progress between them (:func:`next_graceful_streak`)
    or :data:`MAX_GRACEFUL_RESUMES` in all. A graceful resume
    never spends the other bounds; the run's deadline bounds them all.
    """
    lost, graceful = resume_counts(trigger_context)
    if next_resume_is_graceful(trigger_context, attempt=attempt, taken_back_on_its_word=taken_back_on_its_word):
        bound = max(0, min(int(graceful_streak_bound), MAX_GRACEFUL_STREAK))
        streak = next_graceful_streak(trigger_context, completed_calls=completed_calls)
        if streak <= bound and graceful < MAX_GRACEFUL_RESUMES:
            return None
        reason = graceful_cap_reached_reason(
            trigger_context, taken_back_on_its_word=taken_back_on_its_word, streak=streak > bound
        )
        if streak > bound and bound < MAX_GRACEFUL_STREAK:
            from app.services.schedule_overlap import starving_note

            reason += starving_note(bound)
        return reason
    streak = next_lost_streak(trigger_context, completed_calls=completed_calls)
    if streak <= MAX_RESUMES and lost < MAX_LOST_RESUMES:
        return None
    return cap_reached_reason(trigger_context, cause=lost_cause(trigger_context, attempt=attempt))


def resuming(trigger_context: dict[str, Any] | None, *, reclaimed: bool) -> bool:
    """Whether this attempt goes on with the work of an attempt lost or handed back.

    *reclaimed*: the attempt found the run left ``running``. A run put
    back to ``pending`` by a hold after a hand-back still carries the hand-back.
    """
    return reclaimed or (isinstance(trigger_context, dict) and HANDED_BACK_KEY in trigger_context)


def may_hold_checkpoint(trigger_context: dict[str, Any] | None) -> bool:
    """Whether a run's checkpoint may be stored: it was resumed or handed back.

    A run lost before any of either may hold one too; it expires with its TTL.
    """
    if not isinstance(trigger_context, dict):
        return False
    return bool(background_checkpoint.resumes_of(trigger_context)) or HANDED_BACK_KEY in trigger_context


def without_resume_state(trigger_context: dict[str, Any] | None) -> dict[str, Any] | None:
    """*trigger_context* without the resumes and hand-back of a run: what a retry run starts with."""
    if not isinstance(trigger_context, dict):
        return trigger_context
    if not any(key in trigger_context for key in RESUME_STATE_KEYS):
        return trigger_context
    return {k: v for k, v in trigger_context.items() if k not in RESUME_STATE_KEYS}


async def prepare_resume(
    run: Any,
    *,
    attempt: int | None,
    replay: TaskReplay | None = None,
    taken_back_on_its_word: bool = False,
) -> TaskReplay:
    """Record this resume on *run*'s ``trigger_context``, with the checkpoint of its lost attempt.

    *replay* is the checkpoint :func:`load_replay` read for the refusal; read
    here when not given. Called by an attempt that found the run left
    ``running`` (``reclaimed``), once its own place and charge are settled,
    before its "running" commit, which writes the record. The work item claims
    stay held by the run. The resume records the run's cumulative count of
    completed calls, the progress the next refusal reads.
    """
    if replay is None:
        replay = await load_replay(run.id, user_id=getattr(run, "user_id", None))
    context = run.trigger_context if isinstance(run.trigger_context, dict) else {}
    cause = lost_cause(context, attempt=attempt, taken_back_on_its_word=taken_back_on_its_word)
    graceful = next_resume_is_graceful(context, attempt=attempt, taken_back_on_its_word=taken_back_on_its_word)
    updated = background_checkpoint.record_resume(
        context,
        attempt=attempt,
        cause=cause,
        replayed_calls=replay.replayed_calls,
        resumed_from=replay.turn_id,
        graceful=graceful,
        completed_calls=replay.completed_calls,
    )
    updated.pop(HANDED_BACK_KEY, None)
    run.trigger_context = updated
    from app.services.rollout_counters import count_resume

    count_resume("assignment_run", background_checkpoint.cause_class(cause), graceful=graceful)
    # A resumed turn is a friction of the run (services/frictions.py).
    from app.services.frictions import note_turn_resumed
    from app.tools.wait_policy import cut_wait_to_continue

    note_turn_resumed(
        run,
        cause=cause,
        replayed_calls=replay.replayed_calls,
        graceful=graceful,
        wait_continued=cut_wait_to_continue(list(replay.resumed.messages)) is not None,
    )
    lost, graceful_made = resume_counts(updated)
    logger.info(
        "Assignment run %s resumed as attempt %s from %s: %d tool call(s) replayed "
        "(%d completed in all), %s resume; "
        "%d of %d in a row after a loss without a word with no progress, %d of %d in all, "
        "%d of %d in a row after a graceful stop with no progress, %d of %d after a graceful stop in all; "
        "cause: %s; its work item claims carried",
        run.id,
        attempt,
        replay.turn_id or "no checkpoint",
        replay.replayed_calls,
        replay.completed_calls,
        "a graceful" if graceful else "a counted",
        lost_streak(updated),
        MAX_RESUMES,
        lost,
        MAX_LOST_RESUMES,
        graceful_streak(updated),
        MAX_GRACEFUL_STREAK,
        graceful_made,
        MAX_GRACEFUL_RESUMES,
        cause,
    )
    return replay


def resumed_trace_meta(run_id: Any, trigger_context: dict[str, Any] | None) -> dict[str, Any] | None:
    """The ``metadata.resumed_from`` of a resumed run's trace, ``None`` for a first run."""
    resumes = background_checkpoint.resumes_of(trigger_context)
    if not resumes:
        return None
    last = resumes[-1]
    lost, graceful = resume_counts(trigger_context)
    return {
        RESUMED_FROM_TRACE_KEY: {
            "entry": "assignment_run",
            "run_id": str(run_id),
            "cause": last.get("cause"),
            "attempt": last.get("resumed_from"),
            "resumed_as": last.get("resumed_as"),
            # ``resume`` of ``of``: the resumes after a loss without a word, in
            # all, never the graceful ones, which ``graceful_resumes``
            # of ``graceful_of`` count on their own bound.
            "resume": lost,
            "of": MAX_LOST_RESUMES,
            # The wordless losses in a row with no progress between them, against MAX_RESUMES.
            "streak": lost_streak(trigger_context),
            "streak_of": MAX_RESUMES,
            GRACEFUL_KEY: _is_graceful(last),
            "graceful_resumes": graceful,
            "graceful_of": MAX_GRACEFUL_RESUMES,
            # The graceful stops in a row with no progress between them, against MAX_GRACEFUL_STREAK.
            "graceful_streak": graceful_streak(trigger_context),
            "graceful_streak_of": MAX_GRACEFUL_STREAK,
            "at": last.get("at"),
        }
    }


def cap_reached_error(trigger_context: dict[str, Any] | None, *, attempts: int, allowed: int) -> str | None:
    """The resume cap's reason for a run dead-lettered with no attempt left after a resume or a hand-back.

    ``None`` for any other dead letter (a deadline, a run never resumed): its
    own error stands.
    """
    if not allowed or attempts < allowed:
        return None
    context = trigger_context if isinstance(trigger_context, dict) else {}
    if not background_checkpoint.resumes_of(context) and HANDED_BACK_KEY not in context:
        return None
    return cap_reached_reason(context)


async def _keep_browser_sessions(run_id: Any) -> None:
    """Keep and share the open browser sessions' states before run *run_id* is handed back; never raises.

    A page that signs in from its URL (a ``#token=`` callback) writes its token
    after the navigate that opened it returned and kept the state. The worker's
    stop hook kept it too late: the next worker had already claimed the run and
    re-created the session from the state without the token, signed out.
    """
    try:
        from app.services.browser_tool import keep_open_sessions

        await keep_open_sessions()
    except Exception as exc:
        logger.warning("Browser sessions not kept at the hand-back of run %s: %s", run_id, exc)


async def hand_back(
    run_id: Any,
    *,
    checkpoint: TaskCheckpoint,
    attempt: int | None,
    place: tuple[int, int] | None,
) -> None:
    """Hand a run its stopping worker wound down back to the queue.

    The checkpoint is flushed first, every completed round in it; the run
    stays ``running`` with the hand-back recorded (signal, attempt), so the
    next attempt resumes it. A run that ended meanwhile, or whose attempt a
    later one replaced, is left as it is.

    The adapter defers the execution: the claim that resumes the run
    runs at this attempt's number again. This attempt's charge stays recorded
    on the run until that claim takes it as a lost attempt's charge and
    credits it back (``take_lost_charge`` with ``handed_back_at``), or
    the dead letter does: never removed before a credit, so no worker death
    between the two loses it.

    The open browser sessions' cookies and local storage are kept and shared
    before the run is handed back (:func:`_keep_browser_sessions`): the next
    worker can claim it the moment it is, before this worker's stop hook runs.
    """
    from app.core.database import async_session
    from app.models.assignment import AssignmentRun
    from app.services.attempt_charges import attempt_fence
    from app.services.orchestration_reconciler import stop_signal

    await _keep_browser_sessions(run_id)
    await checkpoint.aclose(flush=True)
    signal = stop_signal()
    async with async_session() as db:
        run = (
            await db.execute(
                select(AssignmentRun).where(AssignmentRun.id == uuid.UUID(str(run_id))).with_for_update()
            )
        ).scalar_one_or_none()
        if run is None or run.status != "running":
            return
        if place is not None and attempt_fence(run.trigger_context) > place:
            return
        run.trigger_context = background_checkpoint.handed_back(run.trigger_context, attempt=attempt, signal=signal)
        await db.commit()
    logger.warning(
        "Assignment run %s handed back at attempt %s: its worker was asked to stop by %s; "
        "it is resumed from its checkpoint by the next worker, its work item claims carried",
        run_id,
        attempt,
        signal,
    )
