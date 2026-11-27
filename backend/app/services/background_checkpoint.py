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

"""Checkpoint and resume of a background task's turn.

A background task (a sub-agent delegation, the child run of an assignment)
runs one agent turn on a worker. A rollout, a scale-in, a node drain or a lost
lease takes that worker away and the orchestration engine runs the task again
on another one (``SubAgentAdapter``): before, from scratch, every tool call of
the attempt the engine lost made a second time, and every edit it had not
committed lost with its memory.

The turn now checkpoints its completed tool rounds at every iteration
boundary, with the writer of a chat turn (:class:`TurnCheckpoint`):
latest snapshot wins, written off the loop's path, bounded the same way. The
row is a Redis key per task (the task has no conversation of its own),
kept :data:`CHECKPOINT_TTL_SECONDS`. The next attempt reads it
(:func:`load_task_replay`) and starts its turn from those rounds, followed by
the resume note of a chat turn: the calls replayed, the last one, the results
cut, and the manifest of the sandbox workspace the task runs in, so the
resumed turn knows its files and uncommitted edits are still there. No prompt
text is added beyond those facts (invariant 16).

At most :data:`MAX_RESUMES` resumes per task after a loss without a word (a
crash, an OOM kill, a lapsed lease): a turn that keeps losing its worker is
failed with its reason instead of looping. A resume after a graceful stop (the
turn handed back by its stopping worker, or taken back from a worker that said
it was stopping) is recorded but not counted against it: a rollout is
not the work failing, and rollouts every few minutes spent the cap of every
long task. Graceful resumes follow the same streak rule as the losses without
a word: at most :data:`MAX_GRACEFUL_STREAK` in a row with no tool call
completed between them, so a task that progresses between rollouts is never
ended by them, and :data:`MAX_GRACEFUL_RESUMES` in all, far above any rollout
cadence, on top of the task's own deadline. Each resume is written on the task
(``metadata_json["resumes"]``, the trace link from the attempt lost to the
attempt that resumed it, ``graceful`` or not) and logged at INFO with the
task, the attempt, the cause, both counts and what was replayed.

Best-effort by contract, like a chat turn's checkpoint: a checkpoint that
cannot be read or written costs the resume, never the task.
"""

from __future__ import annotations

import json
import logging
import uuid
import weakref
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from app.core.utils import format_exception_message
from app.llm.base import ChatMessage
from app.services import turn_checkpoint
from app.services.turn_checkpoint import ResumedWork, TurnCheckpoint

logger = logging.getLogger(__name__)

#: The Redis key of a background task's checkpoint.
_KEY = "background-task:checkpoint:{task_id}"
#: How long a checkpoint outlives its last write: a task resumed later
#: than this starts again from its description.
CHECKPOINT_TTL_SECONDS = 24 * 3600
#: The resumes one task may make after losses without a word IN A ROW, with no
#: progress between them: no tool call completed since the loss before.
#: The next such loss fails it with its reason. A task that progressed since its
#: last loss starts its streak again.
MAX_RESUMES = 3
#: The resumes one task may make after a loss without a word in all, progress
#: or not: what still stops a crash loop that advances slowly. Its
#: deadline bounds it too.
MAX_LOST_RESUMES = 20
#: The resumes one task may make after a graceful stop IN A ROW, with no
#: progress between them (the rule for graceful stops): no tool
#: call completed since the resume before. The next such stop fails it with its
#: reason. A task that progressed since its last resume starts its streak again.
MAX_GRACEFUL_STREAK = 3
#: The resumes one task may make after a graceful stop in all, progress or not:
#: not counted against :data:`MAX_RESUMES`, far above
#: the rollout cadence (10 to 15 rollouts an hour) so only a task handed back
#: for more than half a day of rollouts reaches it; its deadline bounds it too.
MAX_GRACEFUL_RESUMES = 200
#: The orchestration attempts an assignment run's execution is created with:
#: its first run and :data:`MAX_RESUMES` resumes. Below that, the
#: engine's default (3) dead-lettered it at its third loss, after two
#: resumes, with the hand-back's text as its error.
MAX_ATTEMPTS = MAX_RESUMES + 1
#: The orchestration attempts a background task's execution runs under: its
#: first run and :data:`MAX_LOST_RESUMES` resumes. Only a loss without a
#: word spends one (a hand-back defers, a take-back on its word gives it back);
#: the streak of :data:`MAX_RESUMES` is the task's own refusal
#: (:func:`resume_refused`), so the engine never dead-letters a task that
#: progressed between its losses at its fourth.
MAX_TASK_ATTEMPTS = MAX_LOST_RESUMES + 1


def task_max_attempts(configured: int) -> int:
    """The ``max_attempts`` a background task's execution is created with.

    *configured* is ``orchestration_default_max_attempts``. An operator's bound
    above the old floor (:data:`MAX_ATTEMPTS`) is kept as it is: the streak of
    :data:`MAX_RESUMES` losses with no progress still refuses below it. The
    default (3), or any value at or below the old floor, gets
    :data:`MAX_TASK_ATTEMPTS`.
    """
    return configured if configured > MAX_ATTEMPTS else MAX_TASK_ATTEMPTS


#: What a task at its resume cap fails with starts with this.
RESUME_CAP_REACHED = "Resume cap reached"
#: ``metadata_json`` key of the resumes a task made.
RESUMES_KEY = "resumes"
#: Key of a resume entry: whether the attempt it follows ended on a graceful stop.
GRACEFUL_KEY = "graceful"
#: How :func:`lost_cause` ends the cause of a hand-back: read on the entries
#: recorded before :data:`GRACEFUL_KEY` existed.
_HANDED_BACK_CAUSE_END = "and handed it back"
#: How long a checkpoint read or deletion may take before it is given up.
_IO_TIMEOUT_SECONDS = 2.0
#: Key of the checkpoint payload, and of a resume entry: the tool calls the task
#: completed in all its attempts, never reduced when the bounded checkpoint
#: drops its oldest rounds.
COMPLETED_CALLS_KEY = "completed_calls"
#: Key of a resume entry: the attempt it follows was taken back from a worker a rollout killed without a word
#: (``orchestration_engine.killed_by_rollout``). Such an entry counts on the graceful total alone, never on the
#: graceful streak (#12386).
ROLLOUT_KILL_KEY = "rollout_kill"


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _call_ids(entries: list[dict] | None) -> set[str]:
    return {
        str(call.get("id"))
        for entry in entries or ()
        for call in entry.get("tool_calls") or ()
        if isinstance(call, dict) and call.get("id")
    }


def checkpoint_key(task_id: Any) -> str:
    return _KEY.format(task_id=task_id)


async def _redis() -> Any:
    from app.core.redis import redis_client

    return await redis_client()


class TaskCheckpoint(TurnCheckpoint):
    """:class:`TurnCheckpoint` writing a background task's rounds to Redis.

    The task id stands for the conversation id. With the acting *user_id*, the
    flushing close (a hand-back) keeps every oversized result whole as a file
    of that user, in the scope the resuming attempt reads files from
    (*files_conversation_id*, the delegating conversation, or *files_run_id*,
    the run), and the replayed text names it, as a chat turn's flush does.
    Without a user, an oversized result is replayed cut, its rest not kept.

    The same keeping starts the moment the worker is asked to stop
    (``worker_shutdown.on_stop``): the latest snapshot is written again with
    its oversized results kept, and so is every later one. A turn still inside
    a tool call or a wait when the worker goes never reaches its hand-back;
    the engine takes it back, and the attempt that resumes it reads the files.
    A turn that ends with no stop keeps no file.
    """

    def __init__(
        self,
        task_id: uuid.UUID,
        *,
        resumed: ResumedWork | None = None,
        attempt: int | None = None,
        completed_calls: int = 0,
        place: tuple[int, int] | None = None,
        user_id: Any = None,
        files_conversation_id: Any = None,
        files_run_id: Any = None,
    ) -> None:
        super().__init__(
            task_id,
            turn_id=f"attempt-{attempt}" if attempt else None,
            resumed=resumed,
            user_id=str(user_id) if user_id else None,
            files_conversation_id=str(files_conversation_id) if files_conversation_id else None,
            files_run_id=str(files_run_id) if files_run_id else None,
        )
        self.task_id = task_id
        #: This attempt's ``(epoch, attempt)`` place: a write of an attempt a later one replaced never
        #: overwrites that attempt's snapshot (``assignment_run_resume._set_if_not_later``, as a run's).
        self.place = place
        #: The calls the attempts before this one completed (:attr:`TaskReplay.completed_calls`).
        self._inherited_calls = max(0, int(completed_calls))
        self._inherited_ids = _call_ids(self._inherited)
        #: Every call id this attempt completed, from every snapshot offered:
        #: only ever grows, whatever a later snapshot still holds.
        self._own_ids: set[str] = set()
        #: With an acting user, the stop of this worker keeps the oversized results as files at once
        #: (:meth:`_worker_stopping`): a turn still inside a call or a wait when its worker goes is
        #: taken back by the engine with no hand-back, so no flushing close would keep them.
        self._unregister_stop: Any = None
        if self.user_id:
            from app.core.worker_shutdown import on_stop

            # Held weakly: a checkpoint dropped without its close leaves no listener alive.
            stopping = weakref.WeakMethod(self._worker_stopping)

            def _listener() -> None:
                method = stopping()
                if method is not None:
                    method()

            self._unregister_stop = on_stop(_listener)

    def _worker_stopping(self) -> None:
        """The worker was asked to stop: keep every oversized result as a file from now on, the latest snapshot first."""
        if self._closed or not self.user_id:
            return
        self._keep_files = True
        if self._latest is not None and self._pending is None:
            self._pending = self._latest
            self._start_writer()

    def offer(self, messages: Sequence[ChatMessage]) -> None:
        """Checkpoint *messages*, and add the calls they completed to this attempt's meter."""
        super().offer(messages)
        self._own_ids |= _call_ids(self._latest) - self._inherited_ids

    def completed_calls(self) -> int:
        """The tool calls the task completed in all its attempts.

        The inherited count plus every call this attempt completed in any
        snapshot it offered. Monotonic: neither :func:`bounded` dropping the
        oldest rounds nor the loop compacting its transcript before a later
        snapshot lowers it.
        """
        return self._inherited_calls + len(self._own_ids)

    async def aclose(self, *, flush: bool) -> None:
        """Take no more snapshots; with *flush*, write the latest one once more first.

        With a ``user_id`` the chat turn's flush runs: its oversized results
        kept whole as files. Without one, its latest snapshot is written again
        as it is, cut, so a turn wound down by its stopping worker leaves every
        round it completed to the attempt that resumes it.
        """
        if self._unregister_stop is not None:
            self._unregister_stop()
            self._unregister_stop = None
        if flush and not self.user_id and not self._closed and self._latest is not None and self._pending is None:
            self._pending = self._latest
            self._start_writer()
        await super().aclose(flush=flush)

    async def _write(self, entries: list[dict]) -> None:
        payload = json.dumps(
            {
                "turn_id": self.turn_id,
                "place": list(self.place) if self.place is not None else None,
                "updated_at": datetime.now(UTC).isoformat(),
                COMPLETED_CALLS_KEY: self.completed_calls(),
                "entries": entries,
            },
            default=str,
        )
        client = await _redis()
        if self.place is None:
            await client.set(checkpoint_key(self.task_id), payload, ex=CHECKPOINT_TTL_SECONDS)
            return
        # A superseded attempt's write still in flight lands after its successor's own only if its place is not
        # earlier: compared and written in one Redis script, as a run's checkpoint.
        from app.services.assignment_run_resume import _set_if_not_later

        if not await _set_if_not_later(client, checkpoint_key(self.task_id), payload, self.place):
            logger.info(
                "Task checkpoint %s not written by attempt %s: a later attempt owns it",
                self.task_id,
                self.place,
            )


@dataclass(frozen=True)
class TaskReplay:
    """What :func:`load_task_replay` found for a task."""

    resumed: ResumedWork
    #: The messages the resuming turn starts its history with: the replayed
    #: rounds, then the resume note.
    history: list[ChatMessage]
    #: The attempt that wrote the checkpoint (``attempt-N``), when known.
    turn_id: str | None = None
    #: The checkpoint's cumulative count of completed calls; ``None``
    #: for a checkpoint written before it existed.
    stored_completed_calls: int | None = None

    @property
    def replayed_calls(self) -> int:
        """The calls the checkpoint still holds: fewer than were made once its oldest rounds were dropped."""
        return sum(len(m.tool_calls or ()) for m in self.resumed.messages)

    @property
    def completed_calls(self) -> int:
        """The calls the task completed in all its attempts: the progress signal.

        Never below what is replayed; :attr:`replayed_calls` for a checkpoint
        written before the count existed.
        """
        stored = self.stored_completed_calls
        return max(stored, self.replayed_calls) if stored is not None else self.replayed_calls


async def load_task_replay(
    task_id: Any,
    *,
    conversation_id: Any = None,
    user_id: Any = None,
    assignment_run_id: Any = None,
    code_interpreter: bool = False,
) -> TaskReplay:
    """The checkpointed work of an earlier attempt of *task_id*, ready to replay.

    *code_interpreter*: the resumed attempt can call the code interpreter, so
    the resume note names ``attachments/`` (``turn_checkpoint.resume_note``).

    *conversation_id* (else *assignment_run_id*) and *user_id* name the
    sandbox workspace the task runs in (the delegating conversation's, or an
    assignment run's own): its manifest and the sandbox commands still
    running in it go in the resume note, so the resumed turn finds its files,
    its uncommitted edits and the run_id of a check its cut segment started.
    Nothing found, or nothing readable: an empty replay, and the task starts
    from its description.
    """
    empty = TaskReplay(resumed=ResumedWork(), history=[])
    try:
        client = await _redis()
        raw = await client.get(checkpoint_key(task_id))
    except Exception as exc:
        logger.warning(
            "Could not read the checkpoint of background task %s; it starts from its description: %s",
            task_id,
            format_exception_message(exc),
        )
        return empty
    if not raw:
        return empty
    try:
        stored = json.loads(raw)
        entries = stored.get("entries") if isinstance(stored, dict) else None
        messages = turn_checkpoint._completed_rounds(turn_checkpoint._messages(entries))
        in_flight = turn_checkpoint._in_flight_of(entries)
    except Exception as exc:
        logger.warning(
            "Background task %s has an unreadable checkpoint; it starts from its description: %s",
            task_id,
            format_exception_message(exc),
        )
        return TaskReplay(resumed=ResumedWork(found=True), history=[])
    completed = _count(stored.get(COMPLETED_CALLS_KEY)) if isinstance(stored, dict) else None
    if not messages and not in_flight:
        return TaskReplay(resumed=ResumedWork(found=True), history=[], stored_completed_calls=completed)
    replayed_entries = turn_checkpoint._entries(messages)
    resumed = ResumedWork(
        messages=messages,
        entries=replayed_entries,
        found=True,
        # The results cut, with the files a hand-back kept them in: named by the
        # note, and never kept a second time by this attempt's own flush.
        spilled=turn_checkpoint.with_spills(replayed_entries, entries),
        in_flight=in_flight,
    )
    workspace = None
    if (conversation_id is not None or assignment_run_id is not None) and user_id is not None:
        try:
            from app.services.sandbox_workspace.session import manifest_for_resume

            workspace = await (
                manifest_for_resume(conversation_id, user_id)
                if conversation_id is not None
                else manifest_for_resume(None, user_id, assignment_run_id=assignment_run_id)
            )
        except Exception as exc:
            logger.debug("No workspace manifest for the resume of task %s: %s", task_id, format_exception_message(exc))
        from app.services.sandbox_workspace.landed import judge_for_resume

        # Whether each sandbox write the cut segment started landed, so the note says it.
        await judge_for_resume(in_flight, conversation_id, user_id, assignment_run_id=assignment_run_id)
        from app.services.video_studio import resume_landed

        # Whether each recording the cut segment started was created, so the note names it.
        await resume_landed.judge_for_resume(in_flight, conversation_id, user_id)
    # A cut result whose whole text the checkpoint holds becomes a file the resumed attempt reads (#2689),
    # in the scope it reads files from: the delegating conversation, else the run.
    resumed = await turn_checkpoint.keep_wholes(
        resumed,
        user_id=user_id,
        conversation_id=str(conversation_id) if conversation_id is not None else None,
        run_id=str(assignment_run_id) if conversation_id is None and assignment_run_id is not None else None,
    )
    # A file the note names is one the resumed attempt can open (#11847).
    resumed = replace(resumed, spilled=await turn_checkpoint.readable_spills(resumed.spilled, user_id))
    note = turn_checkpoint.resume_note(resumed, workspace=workspace, code_interpreter=code_interpreter)
    history = [*resumed.messages, *([note] if note is not None else [])]
    # Kept by this attempt's checkpoint: a later resume replays it, its in-flight line included.
    resumed = resumed.with_note(note)
    turn_id = stored.get("turn_id") if isinstance(stored, dict) else None
    return TaskReplay(
        resumed=resumed,
        history=history,
        turn_id=turn_id if isinstance(turn_id, str) else None,
        stored_completed_calls=completed,
    )


async def discard_task_checkpoint(task_id: Any) -> None:
    """Delete the checkpoint of *task_id*: called once the task ended, however it ended."""
    try:
        client = await _redis()
        await client.delete(checkpoint_key(task_id))
    except Exception as exc:
        logger.debug("Could not delete the checkpoint of background task %s: %s", task_id, format_exception_message(exc))


def resumes_of(metadata: dict | None) -> list[dict]:
    """The resumes recorded on a task's ``metadata_json``."""
    found = (metadata or {}).get(RESUMES_KEY)
    return [r for r in found if isinstance(r, dict)] if isinstance(found, list) else []


def is_graceful(resume: dict) -> bool:
    """Whether a recorded resume followed a graceful stop.

    Its :data:`GRACEFUL_KEY` says it; an entry recorded before that key
    existed is read by its cause, a hand-back's.
    """
    flag = resume.get(GRACEFUL_KEY)
    if isinstance(flag, bool):
        return flag
    cause = resume.get("cause")
    return isinstance(cause, str) and cause.endswith(_HANDED_BACK_CAUSE_END)


def resume_counts(metadata: dict | None) -> tuple[int, int]:
    """The resumes a task made: ``(after a loss without a word, after a graceful stop)``."""
    resumes = resumes_of(metadata)
    graceful = sum(1 for r in resumes if is_graceful(r))
    return len(resumes) - graceful, graceful


def _calls(resume: dict | None) -> int:
    """The calls the task had completed when *resume* was made.

    Its :data:`COMPLETED_CALLS_KEY`; an entry recorded before that key existed
    is read by its ``replayed_calls``.
    """
    found = resume or {}
    completed = _count(found.get(COMPLETED_CALLS_KEY))
    if completed is not None:
        return completed
    return _count(found.get("replayed_calls")) or 0


def _progressed(resume: dict, previous: dict | None) -> bool:
    """Whether the attempt lost before *resume* advanced the task past *previous*.

    Progress is the checkpoint's cumulative count of completed calls, which
    each write raises by the calls of the new rounds and which the bounded
    checkpoint's eviction of its oldest rounds never reduces. A crash loop
    that does nothing leaves the count as it was.
    """
    return _calls(resume) > _calls(previous)


def lost_streak(metadata: dict | None) -> int:
    """The resumes after a loss without a word made in a row with no progress between them.

    Read over every recorded resume, oldest first: one that shows progress
    since the resume before it (:func:`_progressed`) starts the streak again,
    graceful or not; one after a loss without a word adds to it.
    """
    streak = 0
    previous: dict | None = None
    for resume in resumes_of(metadata):
        if _progressed(resume, previous):
            streak = 0
        if not is_graceful(resume):
            streak += 1
        previous = resume
    return streak


def graceful_streak(metadata: dict | None, *, graceful: Any = None) -> int:
    """The resumes after a graceful stop made in a row with no progress between them.

    The rule of :func:`lost_streak` for graceful stops, read over every
    recorded resume, oldest first: one that shows progress since the resume
    before it starts the streak again, graceful or not; one after a graceful
    stop adds to it, unless it followed a rollout's kill (:data:`ROLLOUT_KILL_KEY`): such an entry neither
    adds to the streak nor ends it. An entry recorded before :data:`COMPLETED_CALLS_KEY`
    existed tells no reliable progress (its retained count stays flat once the
    checkpoint is full), so it starts the streak again rather than ending a
    run alive across the deploy. *graceful* reads an entry (default
    :func:`is_graceful`; an assignment run passes its own reader).
    """
    reader = graceful or is_graceful
    streak = 0
    previous: dict | None = None
    for resume in resumes_of(metadata):
        if COMPLETED_CALLS_KEY not in resume:
            streak = 0
        else:
            if _progressed(resume, previous):
                streak = 0
            if reader(resume) and resume.get(ROLLOUT_KILL_KEY) is not True:
                streak += 1
        previous = resume
    return streak


def next_graceful_streak(
    metadata: dict | None,
    *,
    replayed_calls: int | None = None,
    completed_calls: int | None = None,
    graceful: Any = None,
) -> int:
    """The streak a resume after a graceful stop would make, given what it loaded.

    *completed_calls* is the count the resuming attempt loaded
    (:attr:`TaskReplay.completed_calls`); *replayed_calls* stands for it when
    it is not given. More than the last resume recorded: the task progressed
    since, and the streak starts again at 1. Neither known (``None``), no
    progress is assumed.
    """
    resumes = resumes_of(metadata)
    calls = completed_calls if completed_calls is not None else replayed_calls
    if calls is not None:
        upcoming = {COMPLETED_CALLS_KEY: int(calls)}
        if _progressed(upcoming, resumes[-1] if resumes else None):
            return 1
    return graceful_streak(metadata, graceful=graceful) + 1


def next_lost_streak(
    metadata: dict | None, *, replayed_calls: int | None = None, completed_calls: int | None = None
) -> int:
    """The streak a resume after a loss without a word would make, given what it loaded.

    *completed_calls* is the count the resuming attempt loaded
    (:attr:`TaskReplay.completed_calls`); *replayed_calls* stands for it when
    it is not given. Neither known (``None``), no progress is assumed.
    """
    resumes = resumes_of(metadata)
    calls = completed_calls if completed_calls is not None else replayed_calls
    if calls is not None:
        upcoming = {COMPLETED_CALLS_KEY: int(calls)}
        if _progressed(upcoming, resumes[-1] if resumes else None):
            return 1
    return lost_streak(metadata) + 1


def _hand_back_is_current(metadata: dict | None, attempt: int | None) -> bool:
    """Whether the task's hand-back is the one *attempt* resumes, not one an attempt since lost without a word.

    The hand-back's deferral gives its attempt back, so the claim that
    resumes it runs at the attempt the hand-back recorded. A claim lost without
    a word before :func:`note_resume` cleared the record spent its attempt: the
    next one runs at a later attempt and is not graceful.
    """
    back = (metadata or {}).get(HANDED_BACK_KEY)
    if not isinstance(back, dict):
        return False
    recorded = back.get("attempt")
    return not isinstance(recorded, int) or attempt is None or attempt <= recorded


def next_resume_is_graceful(
    metadata: dict | None, *, attempt: int | None = None, taken_back_on_its_word: bool = False
) -> bool:
    """Whether *attempt*, about to resume a task, follows a graceful stop.

    The turn handed back by its stopping worker (:data:`HANDED_BACK_KEY` on the
    task, recorded at this very attempt: :func:`_hand_back_is_current`), or the
    execution taken back from a worker that said it was stopping
    (*taken_back_on_its_word*, ``orchestration_engine.stopped_on_its_word``).
    """
    return taken_back_on_its_word or _hand_back_is_current(metadata, attempt)


def resume_refused(
    metadata: dict | None,
    *,
    attempt: int | None = None,
    taken_back_on_its_word: bool = False,
    replayed_calls: int | None = None,
    completed_calls: int | None = None,
    reason: str | None = None,
    killed_by_rollout: bool = False,
) -> str | None:
    """Why the next resume of a task is refused, else ``None``.

    After a loss without a word: :data:`MAX_RESUMES` such resumes already made
    in a row with no progress between them (:func:`next_lost_streak`, given the
    replay the resuming attempt loaded), or :data:`MAX_LOST_RESUMES` in
    all. After a graceful stop, :data:`MAX_GRACEFUL_STREAK` such resumes
    already made in a row with no progress between them
    (:func:`next_graceful_streak`), or :data:`MAX_GRACEFUL_RESUMES` in
    all. A graceful resume never spends the other bounds. *reason* is
    the engine's own word on the take-back (:func:`lost_cause`). *killed_by_rollout*: taken back from a worker
    a rollout killed without a word (``orchestration_engine.killed_by_rollout``), counted on the graceful total
    alone.
    """
    lost, graceful = resume_counts(metadata)
    if killed_by_rollout and taken_back_on_its_word and not _hand_back_is_current(metadata, attempt):
        # Taken back from a worker a rollout killed (an older release than the newest one there, gone with no
        # out-of-memory sign: ``worker_presence.gone``). One rollout kills such a task as often as it lands on a
        # pod replaced next, whatever the task did: neither the streak of losses without a word nor the graceful
        # streak counts it, only the graceful total (#12386). A worker that said it was stopping keeps its streak.
        if graceful < MAX_GRACEFUL_RESUMES:
            return None
        return graceful_cap_reached_reason(metadata, taken_back_on_its_word=True, streak=False)
    if next_resume_is_graceful(metadata, attempt=attempt, taken_back_on_its_word=taken_back_on_its_word):
        streak = next_graceful_streak(metadata, replayed_calls=replayed_calls, completed_calls=completed_calls)
        if streak <= MAX_GRACEFUL_STREAK and graceful < MAX_GRACEFUL_RESUMES:
            return None
        return graceful_cap_reached_reason(
            metadata, taken_back_on_its_word=taken_back_on_its_word, streak=streak > MAX_GRACEFUL_STREAK
        )
    streak = next_lost_streak(metadata, replayed_calls=replayed_calls, completed_calls=completed_calls)
    if streak <= MAX_RESUMES and lost < MAX_LOST_RESUMES:
        return None
    return cap_reached_reason(metadata, cause=lost_cause(metadata, attempt=attempt, reason=reason))


def _both_counts(metadata: dict | None) -> str:
    lost, graceful = resume_counts(metadata)
    return (
        f"{lost_streak(metadata)} resume(s) in a row after a loss without a word with no progress between "
        f"them (the most a task makes is {MAX_RESUMES}), {lost} after a loss without a word in all "
        f"(the most a task makes is {MAX_LOST_RESUMES}), {graceful_streak(metadata)} in a row after a graceful "
        f"stop with no progress between them (the most a task makes is {MAX_GRACEFUL_STREAK}), {graceful} "
        f"after a graceful stop in all (the most a task makes is {MAX_GRACEFUL_RESUMES})"
    )


def cap_reached_reason(metadata: dict | None, *, cause: str | None = None) -> str:
    """What a task that lost its worker once more than it may be resumed fails with.

    Said by the attempt that refuses the resume (:func:`resume_refused`) and by
    the engine's dead letter when the loss came with no attempt left (the
    adapter's ``on_dead_letter``): the same words either way, never the
    hand-back's "runs again at once". Every count is named.
    """
    lost, _graceful = resume_counts(metadata)
    why = cause or lost_cause(metadata)
    return (
        f"{RESUME_CAP_REACHED}: the background task lost its worker again after {lost} resume(s) "
        f"({_both_counts(metadata)}); last cause: {why}. These losses were not a rollout's (a worker of an older "
        "release than the newest one running, killed with no out-of-memory sign, is resumed on the graceful bound): "
        "a crash of the work, an out-of-memory kill or a node lost on the live release. It is failed rather than "
        "resumed once more, and its work item claims are released."
    )


def graceful_bound_reached(*, streak: bool, entry: str = "task") -> str:
    """Which graceful bound ended a run or a task, named in its cap text."""
    if streak:
        return (
            f"the bound of {MAX_GRACEFUL_STREAK} graceful resume(s) in a row with no tool call completed "
            f"between them, which a {entry} that progresses never reaches"
        )
    return f"the bound of {MAX_GRACEFUL_RESUMES} graceful resume(s) in all"


def graceful_cap_reached_reason(
    metadata: dict | None, *, taken_back_on_its_word: bool = False, streak: bool = False
) -> str:
    """What a task stopped gracefully once more than its graceful bounds allow fails with.

    *streak*: the bound reached is :data:`MAX_GRACEFUL_STREAK` with no
    progress, else :data:`MAX_GRACEFUL_RESUMES` in all; the text names it.
    """
    _lost, graceful = resume_counts(metadata)
    why = lost_cause(metadata, taken_back_on_its_word=taken_back_on_its_word)
    return (
        f"{RESUME_CAP_REACHED}: the background task was stopped gracefully again after {graceful} graceful "
        f"resume(s), at {graceful_bound_reached(streak=streak)} ({_both_counts(metadata)}); "
        f"last cause: {why}. It is failed rather than resumed once more, and its work item claims are released."
    )


def record_resume(
    metadata: dict | None,
    *,
    attempt: int | None,
    cause: str,
    replayed_calls: int,
    resumed_from: str | None,
    now: datetime | None = None,
    graceful: bool = False,
    completed_calls: int | None = None,
    rollout_kill: bool = False,
) -> dict:
    """*metadata* with one more resume recorded: when, the attempt, the cause, what was replayed.

    ``resumed_from`` is the attempt the checkpoint was written by
    (``attempt-N``); ``resumed_as`` is this attempt: the link a trace reader
    follows from the attempt lost to the one that went on with its work.
    ``graceful``: the attempt lost ended on a graceful stop, a resume not
    counted against :data:`MAX_RESUMES`. ``completed_calls``: the
    task's cumulative count of completed calls, recorded when known.
    ``rollout_kill``: the attempt lost was killed by a rollout (:data:`ROLLOUT_KILL_KEY`), a graceful resume
    counted on the graceful total but never on the graceful streak.
    """
    moment = now or datetime.now(UTC)
    entry = {
        "at": moment.isoformat(),
        "attempt": attempt,
        "cause": cause,
        "replayed_calls": int(replayed_calls),
        "resumed_from": resumed_from,
        "resumed_as": f"attempt-{attempt}" if attempt else None,
        GRACEFUL_KEY: bool(graceful),
    }
    if completed_calls is not None:
        entry[COMPLETED_CALLS_KEY] = int(completed_calls)
    if rollout_kill:
        entry[ROLLOUT_KILL_KEY] = True
    return {**(metadata or {}), RESUMES_KEY: [*resumes_of(metadata), entry]}


#: ``metadata_json`` key of the hand-back of a task whose worker was asked to stop.
HANDED_BACK_KEY = "handed_back"


def handed_back(metadata: dict | None, *, attempt: int | None, signal: str, now: datetime | None = None) -> dict:
    """*metadata* with the hand-back of *attempt* recorded: its worker was asked to stop by *signal*."""
    moment = now or datetime.now(UTC)
    return {
        **(metadata or {}),
        HANDED_BACK_KEY: {"at": moment.isoformat(), "attempt": attempt, "signal": signal},
    }


def lost_cause(
    metadata: dict | None,
    *,
    attempt: int | None = None,
    taken_back_on_its_word: bool = False,
    reason: str | None = None,
) -> str:
    """What took the earlier attempt away, as the task's row (and the execution's take-back) says it.

    *reason*: the engine's own word on a take-back without a word
    (``lease_expired.why`` or its lease's silence, read by the adapter): the
    worker gone, its host, its version and its memory at its last word, said
    instead of the generic cause when no hand-back explains the loss.
    """
    back = (metadata or {}).get(HANDED_BACK_KEY)
    if isinstance(back, dict) and not taken_back_on_its_word and not _hand_back_is_current(metadata, attempt):
        return (
            f"the attempt that took it over after its worker was asked to stop "
            f"({back.get('signal') or 'a stop request'}) was lost without a word before it resumed"
        )
    if isinstance(back, dict):
        return f"its worker was asked to stop ({back.get('signal') or 'a stop request'}) {_HANDED_BACK_CAUSE_END}"
    if taken_back_on_its_word:
        return "its worker said it was stopping (a rollout or a node drain) and the task was taken back from it"
    if isinstance(reason, str) and reason.strip():
        said = reason.strip().rstrip(".")
        return said[:1].lower() + said[1:]
    orphaned = (metadata or {}).get("orphaned")
    if isinstance(orphaned, dict):
        heard = orphaned.get("last_heartbeat_at")
        return "its worker lost its lease" + (f" (last heartbeat at {heard})" if heard else "")
    return "its worker stopped or lost its lease before the turn ended"


def cause_class(cause: str | None) -> str:
    """The bounded class of a :func:`lost_cause` text, the ``cause`` label of the resume counter.

    One of ``handback_lost``, ``handed_back``, ``taken_back``, ``lease_lost``,
    ``unknown`` and ``engine_reason`` (the engine's own word); never
    the text itself, which carries hosts and timestamps.
    """
    text = cause or ""
    if text.startswith("the attempt that took it over after its worker was asked to stop"):
        return "handback_lost"
    if text.startswith("its worker was asked to stop") and text.endswith(_HANDED_BACK_CAUSE_END):
        return "handed_back"
    if text.startswith("its worker said it was stopping"):
        return "taken_back"
    if text.startswith("its worker lost its lease"):
        return "lease_lost"
    if not text or text.startswith("its worker stopped or lost its lease before the turn ended"):
        return "unknown"
    return "engine_reason"


#: The trace metadata key a resumed attempt's first trace carries: the
#: key a resumed chat turn's trace carries too (``cut_turn_resume``).
RESUMED_FROM_TRACE_KEY = "resumed_from"


def resumed_trace_meta(task_id: Any, metadata: dict | None) -> dict | None:
    """The ``metadata.resumed_from`` of the first trace a resumed attempt opens, or ``None`` for a first run.

    Opened through ``langfuse_tracing.next_root_trace`` around the resumed
    turn, so a trace search on ``resumed_from`` finds every resumed task with
    its task, its run, the cause, the attempt it resumed and the count.
    """
    resumes = resumes_of(metadata)
    if not resumes:
        return None
    last = resumes[-1]
    return {
        RESUMED_FROM_TRACE_KEY: {
            "entry": "background_task",
            "task_id": str(task_id),
            "run_id": (metadata or {}).get("assignment_run_id"),
            "cause": last.get("cause"),
            "attempt": last.get("resumed_from"),
            "resumed_as": last.get("resumed_as"),
            # ``resume`` of ``of``: the resumes counted against the cap, never
            # the graceful ones, which ``graceful_resumes`` of
            # ``graceful_of`` count on their own bound.
            "resume": resume_counts(metadata)[0],
            "of": MAX_LOST_RESUMES,
            # The wordless losses in a row with no progress between them, against MAX_RESUMES.
            "streak": lost_streak(metadata),
            "streak_of": MAX_RESUMES,
            "graceful": is_graceful(last),
            "graceful_resumes": resume_counts(metadata)[1],
            "graceful_of": MAX_GRACEFUL_RESUMES,
            # The graceful stops in a row with no progress between them, against MAX_GRACEFUL_STREAK.
            "graceful_streak": graceful_streak(metadata),
            "graceful_streak_of": MAX_GRACEFUL_STREAK,
            "at": last.get("at"),
        }
    }


def note_resume(
    task: Any,
    *,
    replay: TaskReplay,
    attempt: int | None,
    taken_back_on_its_word: bool = False,
    reason: str | None = None,
    killed_by_rollout: bool = False,
) -> dict:
    """*task*'s metadata with this resume recorded, and the INFO line naming task, run, cause and counts.

    *killed_by_rollout*: taken back from a worker a rollout killed without a word; the entry is marked
    (:data:`ROLLOUT_KILL_KEY`) so the graceful streak never counts it (#12386).

    The work item claims stay where they are: held by the task (or its run),
    alive again, so the resumed attempt carries them. No query is made: the
    task's session is the one its attempt locked the row with.
    """
    cause = lost_cause(
        task.metadata_json, attempt=attempt, taken_back_on_its_word=taken_back_on_its_word, reason=reason
    )
    graceful = next_resume_is_graceful(
        task.metadata_json, attempt=attempt, taken_back_on_its_word=taken_back_on_its_word
    )
    metadata = record_resume(
        task.metadata_json,
        attempt=attempt,
        cause=cause,
        replayed_calls=replay.replayed_calls,
        resumed_from=replay.turn_id,
        graceful=graceful,
        completed_calls=replay.completed_calls,
        # The same test as resume_refused's rollout branch: a current hand-back keeps its streak.
        rollout_kill=killed_by_rollout
        and taken_back_on_its_word
        and not _hand_back_is_current(task.metadata_json, attempt),
    )
    metadata.pop("orphaned", None)
    metadata.pop(HANDED_BACK_KEY, None)
    from app.services.rollout_counters import count_resume

    count_resume("background_task", cause_class(cause), graceful=graceful)
    lost, graceful_made = resume_counts(metadata)
    logger.info(
        "Background task %s (run %s) resumed as attempt %s from %s: %d tool call(s) replayed "
        "(%d completed in all), %s resume; "
        "%d of %d in a row after a loss without a word with no progress, %d of %d in all, "
        "%d of %d in a row after a graceful stop with no progress, %d of %d after a graceful stop in all; "
        "cause: %s; its work item claims carried",
        task.id,
        (task.metadata_json or {}).get("assignment_run_id") or "none",
        attempt,
        replay.turn_id or "no checkpoint",
        replay.replayed_calls,
        replay.completed_calls,
        "a graceful" if graceful else "a counted",
        lost_streak(metadata),
        MAX_RESUMES,
        lost,
        MAX_LOST_RESUMES,
        graceful_streak(metadata),
        MAX_GRACEFUL_STREAK,
        graceful_made,
        MAX_GRACEFUL_RESUMES,
        cause,
    )
    return metadata
