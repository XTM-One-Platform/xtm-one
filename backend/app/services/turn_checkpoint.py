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

"""Durable checkpoint of a chat turn's tool work.

A web chat turn keeps the tool calls it made, and the results they returned,
in the agent loop's memory: its answer and a mid-turn segment are all
it stores. A turn whose process dies before its answer is stored - a crash, an
OOM kill, a restart, a deploy - is lost, as it should be, but so is its work:
the next turn ("you crashed, go again") reads a history without any of it and
calls every tool again.

``run_agent_loop(on_checkpoint=...)`` hands what it appended to
:meth:`TurnCheckpoint.offer` at every iteration boundary, where every tool call
has its result. :func:`completed_work` keeps the completed tool rounds - an
assistant message that called tools and the results answering every one of
its calls - and leaves the rest out: a round still waiting for a result, and
the loop's scaffolding (system nudges, and steering messages, which are stored
as user messages already). A background task upserts the latest snapshot into
``chat_turn_checkpoints`` on a session of its own: latest wins, never on the
loop's path, bounded by :data:`MAX_CHECKPOINT_CHARS` (the oldest rounds go
first). The JSON column's serializer stores a secret variable value as its
marker, as for any stored run message.

A turn that stores its answer deletes the row in the transaction that stores
it (:func:`discard`). A turn that dies leaves it, and so does a turn that ends
on an error. The next web chat turn of the conversation reads it
(:func:`load_resumable`, younger than :data:`RESUME_MAX_AGE`) and appends the
rounds to its history, right before its new message, as what they are: the
assistant's tool calls and the results they returned. No prompt text is added
(invariant 16): the results are the grounding, and the model goes on from them
instead of calling the tools again. The turn that resumed them carries them in
its own checkpoint, so a second crash loses nothing either.

Best-effort by contract: a checkpoint that cannot be read or written costs the
resume, never the turn.

Bounded per result, not only per checkpoint: a result longer than
:data:`MAX_REPLAYED_RESULT_CHARS` is replayed cut, its whole text kept as a
conversation file the replay names (:data:`SPILL_SOURCE`), and a call's
arguments longer than :data:`MAX_REPLAYED_ARGUMENT_CHARS` have their longest
strings cut. Before, one oversized round (a code cell carrying twenty pasted
files) pushed every round out of :data:`MAX_CHECKPOINT_CHARS` and the next turn
resumed nothing. :func:`resume_note` is the line the resuming turn reads after
the replayed rounds: the calls replayed, the last one, the results cut and
where they are whole, and the sandbox workspace's manifest. Facts only, built
by code (invariant 16).

A round still waiting for a result is no work to replay, but it is a fact the
resuming turn needs: its calls may have reached their service (a pull request
created, a message sent) before the attempt ended. The loop offers a snapshot
right before it dispatches a round, and :meth:`TurnCheckpoint.offer` keeps the
calls of the trailing unanswered round as one :data:`IN_FLIGHT_ROLE` entry,
which every reader of the replayed rounds leaves out; :func:`resume_note` names
them as started, outcome not recorded.

A write made once the worker knows why it stops (``worker_shutdown.cut_cause``:
a stop signal from its host, its own recycle) stamps that entry with the cause
(:data:`IN_FLIGHT_CUT_KEY`). The cut that follows is the platform's, not the
call's: :func:`_seen_now` does not count it toward :data:`POISON_CUTS`, so a
long wait a rollout cut twice runs again (#9912). A cut with no recorded cause
(a crash, an OOM kill) still counts.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.utils import format_exception_message
from app.llm.base import CHECKPOINT_FILE_SOURCE, ChatMessage
from app.models.chat import ChatTurnCheckpoint
from app.services.run_clock import clock_sentence
from app.tools.base import ToolCall

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: What one checkpoint keeps, in characters of message text and call arguments.
MAX_CHECKPOINT_CHARS = 1_500_000
#: A tool result longer than this is replayed cut, kept whole as a file.
MAX_REPLAYED_RESULT_CHARS = 20_000
#: What the replay shows of a cut result.
REPLAY_HEAD_CHARS = 12_000
#: A call's arguments longer than this are replayed with their longest strings cut.
MAX_REPLAYED_ARGUMENT_CHARS = 20_000
#: ``FileAttachment.source`` of a result kept whole for a resume: an internal
#: file, never listed for the user nor attached to a reply (``app.llm.base``).
SPILL_SOURCE = CHECKPOINT_FILE_SOURCE
#: Cut results the resume note names one by one.
_NOTE_MAX_SPILLED = 10
#: A checkpoint older than this is not resumed: what its tools read has aged.
RESUME_MAX_AGE = timedelta(hours=24)
#: How long closing a checkpoint waits for the write in flight.
CLOSE_TIMEOUT_SECONDS = 10.0
#: ``role`` of the stored entry naming the calls started and not all answered.
IN_FLIGHT_ROLE = "in_flight"
#: A call in flight at this many consecutive interruptions is not run again unchanged.
POISON_CUTS = 2
#: Key of the :data:`IN_FLIGHT_ROLE` entry (and of each call :func:`_in_flight_of`
#: reads from it) naming the worker stop that cut the turn: ``recycle`` or ``stop``.
IN_FLIGHT_CUT_KEY = "cut_by"


@dataclass(frozen=True)
class ResumedWork:
    """What :func:`load_resumable` found for a conversation."""

    #: The completed tool rounds to append to the history.
    messages: list[ChatMessage] = field(default_factory=list)
    #: The same rounds as stored, for the resuming turn's own checkpoint.
    entries: list[dict] = field(default_factory=list)
    #: A checkpoint row exists, resumable or not: the turn's end deletes it.
    found: bool = False
    #: The results replayed cut: ``{"call_id", "name", "file_id", "chars"}``.
    spilled: list[dict] = field(default_factory=list)
    #: The calls of the round the interrupted turn had started and not all
    #: answered: ``{"id", "name", "arguments", "answered", "digest"}``. Not replayed.
    in_flight: list[dict] = field(default_factory=list)

    def with_note(self, note: ChatMessage | None) -> ResumedWork:
        """This work with the resume *note* kept in :attr:`entries`.

        The resuming turn's own checkpoint starts from :attr:`entries`: the
        note kept there is replayed by every later resume of the same turn, so
        a second resume still shows what the first one said was in flight.
        """
        if note is None:
            return self
        return replace(self, entries=[*self.entries, *_entries([note])])


def _completed_rounds(messages: Iterable[ChatMessage]) -> list[ChatMessage]:
    kept: list[ChatMessage] = []
    current: list[ChatMessage] = []
    waiting: set[str] = set()
    for message in messages:
        if message.role == "assistant":
            current, waiting = [], set()
            calls = message.tool_calls or []
            ids = {call.id for call in calls}
            if calls and len(ids) == len(calls):
                current, waiting = [message], ids
            elif not calls and (message.content or "").strip():
                kept.append(message)
        elif message.role == "tool" and message.tool_call_id in waiting:
            current.append(message)
            waiting.discard(message.tool_call_id)
            if not waiting:
                kept.extend(current)
                current = []
    return kept


def _superseded_dropped(inherited: list[dict], work: list[dict]) -> list[dict]:
    """*inherited* without the cut ``wait`` rounds *work* answers anew, by call id.

    A resumed turn finishes the ``wait`` its stopping worker cut before its
    first model call and offers that round again with the new result: the
    inherited copy, "cut short" text included, must not be replayed beside it.
    Only an inherited result carrying ``wait_cut`` whose call *work* answers
    with another text is superseded; any other inherited round (one a task
    checkpoint offers again unchanged, with its ``spill`` record) is kept.
    """
    answered = {str(entry.get("tool_call_id")): entry.get("content") for entry in work if entry.get("role") == "tool"}
    superseded = {
        str(entry.get("tool_call_id"))
        for entry in inherited
        if entry.get("role") == "tool"
        and entry.get("wait_cut")
        and str(entry.get("tool_call_id")) in answered
        and answered[str(entry.get("tool_call_id"))] != entry.get("content")
    }
    if not superseded:
        return inherited
    # The whole round goes: its call and every result, the turn offers it again.
    dropped = {
        str(call.get("id"))
        for entry in inherited
        if any(str(call.get("id")) in superseded for call in entry.get("tool_calls") or ())
        for call in entry.get("tool_calls") or ()
    }
    kept: list[dict] = []
    for entry in inherited:
        calls = entry.get("tool_calls") or ()
        if calls and any(str(call.get("id")) in dropped for call in calls):
            continue
        if entry.get("role") == "tool" and str(entry.get("tool_call_id")) in dropped:
            continue
        kept.append(entry)
    return kept


def in_flight_calls(messages: Iterable[ChatMessage]) -> list[dict]:
    """The calls of the trailing round of *messages* still waiting for a result.

    The last assistant message that called tools, when only tool results follow
    it and one of its calls has none: each call as ``{"id", "name",
    "arguments", "answered", "digest"}``, its arguments cut as a replayed
    call's are and its :func:`call_digest` taken on the whole arguments, so the
    resumed turn tells an identical repeat (``agent_loop``'s in-flight guard).
    ``[]`` when that round is complete or another message followed it.
    """
    trailing: ChatMessage | None = None
    answered: set[str] = set()
    for message in messages:
        if message.role == "assistant":
            trailing = message if message.tool_calls else None
            answered = set()
        elif message.role == "tool" and trailing is not None:
            answered.add(str(message.tool_call_id or ""))
        elif trailing is not None and message.role == "user":
            trailing = None
    if trailing is None:
        return []
    calls = trailing.tool_calls or []
    if all(call.id in answered for call in calls):
        return []
    return [
        {
            "id": call.id,
            "name": call.name,
            "arguments": _cut_arguments(_json_value(call.arguments)),
            "answered": call.id in answered,
            "digest": call_digest(call.name, call.arguments),
        }
        for call in calls
    ]


def call_digest(name: str, arguments: object) -> str:
    """A digest of one call: its tool *name* and its whole *arguments*, key order aside."""
    canonical = json.dumps(
        {"name": name, "arguments": _json_value(arguments)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def in_flight_writes(
    messages: Iterable[ChatMessage],
    *,
    replay_safe: frozenset[str] | set[str] = frozenset(),
    read_only: frozenset[str] | set[str] = frozenset(),
) -> dict[str, dict]:
    """The writes a resumed turn's note names as started and not answered, by :func:`call_digest`.

    Read from the ``in_flight`` of the resume note in *messages* (the latest
    one, the note of this resume). A call the shared classifier reads as a
    read is left out: a read may run again. A call of a tool named in
    *replay_safe* (``ToolDefinition.replay_safe``: the tool answers or
    replaces the job an identical call started) is left out too: refusing it
    only cost a resumed render whose job never queued one more call. A call
    stored without a digest (a checkpoint written before it existed) is left
    out as well, and so is a call of a tool named in *read_only*
    (``ToolDefinition.read_only``: it changes nothing whatever its arguments,
    ``compare_github_commits`` say, whose verb the classifier cannot read).
    A tool that reads for some arguments only (``read_only_call``) is told
    apart by the loop on the live call's whole arguments.
    """
    from app.services.tool_classification import classify_tool_name

    found: dict[str, dict] = {}
    for message in messages:
        if message.role == "assistant" and message.in_flight:
            found = {}
            for call in message.in_flight:
                digest = call.get("digest")
                # A write the workspace shows did not land (``landed`` False,
                # sandbox_workspace.landed) runs on its first repeat.
                if not digest or call.get("answered") or call.get("landed") is False:
                    continue
                name = str(call.get("name") or "")
                if name in replay_safe or name in read_only or classify_tool_name(name)[0] == "read":
                    continue
                found[str(digest)] = call
    return found


def in_flight_repeat_refusal(call: dict) -> str:
    """The answer the loop gives in place of a repeat of the in-flight write *call*."""
    return (
        f"Not run: this {call.get('name') or 'tool'} call repeats, with the same arguments, the call "
        "the interrupted attempt had started before this turn resumed (named in the resume note); its "
        "outcome was not recorded and it may have taken effect. Read the current state first (the file, "
        "the branch, the pull request, the entry); if the write did not land, make the same call again "
        "and it runs."
    )


def in_flight_landed_answer(call: dict[str, Any], landed: str, *, reworded: bool = False) -> str:
    """The answer the loop gives in place of a repeat of the in-flight write *call* its tool found landed.

    *landed* is what the tool's ``landed_probe`` named (the issue, the commit).
    *reworded*: the repeat differs from the interrupted call in its wording
    and creates on the same natural key (``ToolDefinition.landed_key``): it is
    answered once, and runs when made again.
    """
    if reworded:
        return (
            f"Not run: the interrupted attempt of this turn had already made this {call.get('name') or 'tool'} "
            f"call, reworded, before the turn resumed, and it landed: {landed.strip()}. Use it as this call's "
            "outcome (update it if its wording must change); do not create it a second time. If this call "
            "creates a different object, make it again and it runs."
        )
    return (
        f"Not run again: this {call.get('name') or 'tool'} call repeats the call the interrupted attempt had "
        f"started before this turn resumed, and that call landed: {landed.strip()}. Use it as this call's "
        "outcome; do not create it a second time."
    )


def landed_note_lines(content: str, writes: dict[str, dict[str, Any]], landed: dict[str, str]) -> str:
    """The resume note *content* with what each interrupted write of *landed* created, read back.

    *writes* are the in-flight writes by digest (:func:`in_flight_writes`),
    *landed* what the tool's ``landed_probe`` named for some of them, read at
    the start of the resumed turn. One sentence is added before the clock
    sentence that closes the note; the note is unchanged without a verdict.
    """
    lines = [
        f"{writes[digest].get('name') or 'tool'} landed: {what.strip()}"
        for digest, what in landed.items()
        if digest in writes and what.strip()
    ]
    if not lines:
        return content
    sentence = "Read back since: " + "; ".join(lines) + ". Use each as that call's outcome; do not create it again."
    body, sep, tail = content.rpartition("\n")
    return f"{body}\n{sentence}{sep}{tail}" if sep else f"{sentence}\n{content}"


async def readable_spills(spilled: list[dict[str, Any]], user_id: object) -> list[dict[str, Any]]:
    """*spilled* with ``file_id`` cleared on every entry whose file *user_id* cannot read now.

    The resume note names a cut result's file only when the resumed attempt
    can open it: a row of *user_id* that exists (never purged, never a write
    that failed after the checkpoint named it). Without a user, or when the
    lookup fails, the entries are returned as they are.
    """
    ids = [str(s["file_id"]) for s in spilled if s.get("file_id")]
    if not ids or user_id is None:
        return spilled
    try:
        from app.services.sub_agent_files import load_attachment_rows

        rows = await load_attachment_rows(ids, user_id=user_id)
    except Exception as exc:
        logger.debug("Could not check the files a resume note names: %s", format_exception_message(exc))
        return spilled
    readable = {str(row.id) for row in rows}
    return [s if not s.get("file_id") or str(s["file_id"]) in readable else {**s, "file_id": None} for s in spilled]


def _in_flight_of(entries: object) -> list[dict]:
    """The in-flight calls a stored checkpoint names (its last :data:`IN_FLIGHT_ROLE` entry)."""
    found: list[dict] = []
    for entry in entries if isinstance(entries, list) else ():
        if isinstance(entry, dict) and entry.get("role") == IN_FLIGHT_ROLE and isinstance(entry.get("calls"), list):
            cut_by = entry.get(IN_FLIGHT_CUT_KEY)
            found = [
                {
                    "id": str(call.get("id") or ""),
                    "name": str(call.get("name") or "tool"),
                    "arguments": call.get("arguments") if isinstance(call.get("arguments"), dict) else {},
                    "answered": bool(call.get("answered")),
                    **({"digest": str(call["digest"])} if call.get("digest") else {}),
                    **(
                        {"landed": call["landed"], "outcome": str(call.get("outcome") or "")}
                        if isinstance(call.get("landed"), bool)
                        else {}
                    ),
                    **({IN_FLIGHT_CUT_KEY: cut_by} if isinstance(cut_by, str) and cut_by else {}),
                }
                for call in entry["calls"]
                if isinstance(call, dict)
            ]
    return found


def _with_cut_cause(entries: list[dict]) -> list[dict]:
    """*entries* with their :data:`IN_FLIGHT_ROLE` entry stamped with why this worker stops, when it knows.

    Read at write time: the snapshot is offered before the round runs, and the
    stop comes during the call; the flush a stopping worker writes (its
    hand-back, the drain's close) is the write that carries it. The entries
    offered are left as they are.
    """
    from app.core.worker_shutdown import cut_cause

    cause = cut_cause()
    if not cause:
        return entries
    return [
        {**entry, IN_FLIGHT_CUT_KEY: cause}
        if isinstance(entry, dict) and entry.get("role") == IN_FLIGHT_ROLE
        else entry
        for entry in entries
    ]


def _json_value(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, default=str))
    except (TypeError, ValueError):
        return {}


def _entries(messages: list[ChatMessage]) -> list[dict]:
    """*messages* in the shape ``serialize_messages`` stores a run's transcript in.

    Not that function itself: importing ``builtin_tools`` takes seconds, and
    the chat API imports this module.
    """
    entries: list[dict] = []
    for message in messages:
        entry: dict = {"role": message.role, "content": message.content}
        if message.tool_calls:
            entry["tool_calls"] = [
                {"id": call.id, "name": call.name, "arguments": _json_value(call.arguments)}
                for call in message.tool_calls
            ]
        if message.tool_call_id:
            entry["tool_call_id"] = message.tool_call_id
        if message.name:
            entry["name"] = message.name
        if message.not_run:
            entry["not_run"] = message.not_run
        if message.wait_cut:
            entry["wait_cut"] = _json_value(message.wait_cut)
        if message.in_flight_seen is not None:
            entry["in_flight_seen"] = _json_value(message.in_flight_seen)
        entries.append(entry)
    return entries


def completed_work(messages: Iterable[ChatMessage]) -> list[dict]:
    """The completed tool rounds of *messages*, as JSON-safe entries.

    A round is an assistant message that called tools followed by the result
    of every call it made. A round that a new assistant message cuts short, or
    that the messages end in, is left out: an unanswered call is no work the
    next turn can reuse, and providers refuse a call without its result. An
    assistant message without tool calls is kept when it says something.
    ``system`` and ``user`` messages are left out.
    """
    return _entries(_completed_rounds(messages))


def _entry_chars(entry: dict) -> int:
    size = len(entry.get("content") or "")
    for call in entry.get("tool_calls") or ():
        size += len(json.dumps(call.get("arguments"), default=str))
    return size


def bounded(entries: list[dict], limit: int = MAX_CHECKPOINT_CHARS) -> list[dict]:
    """*entries* within *limit* characters, the oldest rounds dropped first."""
    rounds: list[list[dict]] = []
    for entry in entries:
        if entry.get("role") == "tool" and rounds:
            rounds[-1].append(entry)
        else:
            rounds.append([entry])
    sizes = [sum(_entry_chars(entry) for entry in r) for r in rounds]
    total = sum(sizes)
    start = 0
    while total > limit and start < len(rounds):
        total -= sizes[start]
        start += 1
    return [entry for r in rounds[start:] for entry in r]


#: An id as a result names a stored file: bare, ``file-<id>`` or ``[[FILE:<id>]]``.
_NAMED_ID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
#: Ids of the cut-away part a cut result names again, at most: the last ones, a
#: report or a summary file being named at the end of a long answer.
MAX_CUT_NAMED_IDS = 10


def cut_named_ids(content: str, *, exclude: Iterable[str] = ()) -> list[str]:
    """The ids *content* names past its replayed head and not in it, each once, the last ones kept.

    A replay keeps the first :data:`REPLAY_HEAD_CHARS` of a long result: a
    file the result named further down (a report, a contact sheet) vanished
    from the replayed text, and the resume, which finds the files to attach
    again in that text (``assignment_run_resume.replayed_file_ids``), left it
    unreadable. The cut note names the last :data:`MAX_CUT_NAMED_IDS` again,
    so the replay stays bounded; which of them is a file the run may read is
    decided by the resume's lookup (the run's own rows and its acting user's),
    never here.
    """
    head = content[:REPLAY_HEAD_CHARS].lower()
    skip = {value.lower() for value in exclude if value}
    seen: dict[str, None] = {}
    for match in _NAMED_ID_RE.findall(content[REPLAY_HEAD_CHARS:]):
        value = match.lower()
        if value not in head and value not in skip:
            seen.pop(value, None)
            seen[value] = None
    return list(seen)[-MAX_CUT_NAMED_IDS:]


def _cut_note(total: int, file_id: str | None, filename: str, named_ids: Sequence[str] = ()) -> str:
    named = (
        f" Ids the part not shown names (a file among them stays readable by its id): {', '.join(named_ids)}."
        if named_ids
        else ""
    )
    if file_id is None:
        return (
            f"\n\n[Result cut for the replay of an interrupted turn: the first {REPLAY_HEAD_CHARS:,} of "
            f"{total:,} characters are shown; the rest was not kept.{named}]"
        )
    from app.services.code_interpreter import code_interpreter_offered, mount_path_hint
    from app.tools.base import offered_tools

    # The mount is named only to a run that can call the code interpreter (#2677).
    mount = (
        f", and code_interpreter opens it as {mount_path_hint(filename, file_id)}"
        if code_interpreter_offered(offered_tools())
        else ""
    )
    return (
        f"\n\n[Result cut for the replay of an interrupted turn: the first {REPLAY_HEAD_CHARS:,} of "
        f"{total:,} characters are shown. The whole result is conversation file {file_id}: "
        f'read_file_content(file_id="{file_id}") reads it{mount}.{named}]'
    )


def _cut_arguments(arguments: object, limit: int = MAX_REPLAYED_ARGUMENT_CHARS) -> object:
    """*arguments* with their longest string values cut until they fit *limit* characters of JSON."""
    if not isinstance(arguments, dict):
        return arguments
    size = len(json.dumps(arguments, default=str))
    if size <= limit:
        return arguments
    cut = dict(arguments)
    for key, value in sorted(cut.items(), key=lambda kv: -len(kv[1]) if isinstance(kv[1], str) else 0):
        if size <= limit or not isinstance(value, str):
            break
        keep = max(200, len(value) - (size - limit))
        cut[key] = value[:keep] + f"\n... [cut for the replay: {len(value) - keep:,} more characters]"
        size = len(json.dumps(cut, default=str))
    return cut


async def _spill(
    entries: list[dict],
    *,
    known: dict[str, dict],
    conversation_id: uuid.UUID,
    user_id: str | None,
    files_conversation_id: str | None = None,
    files_run_id: str | None = None,
    kept_text: Callable[[str], str] | None = None,
) -> list[dict]:
    """*entries* with every oversized result cut and kept whole as a file of *user_id*.

    *known* maps a call id to the file its result was already kept in (this
    checkpoint's earlier writes, or the turn it resumed): a result is stored
    once whatever the number of writes. The file belongs to
    *files_conversation_id* (default: *conversation_id*) and to the assignment
    run *files_run_id*: a background task or a run checkpoints under an id
    that is no conversation, and names the scope its resume reads files from.
    *kept_text* turns the result into the text the file holds (a run keeps its
    known secrets as markers, as in its checkpoint).
    """
    out: list[dict] = []
    names: dict[str, str] = {}
    for entry in entries:
        if entry.get("role") == "assistant" and entry.get("tool_calls"):
            calls = []
            for call in entry["tool_calls"]:
                names[str(call.get("id"))] = str(call.get("name"))
                calls.append({**call, "arguments": _cut_arguments(call.get("arguments"))})
            entry = {**entry, "tool_calls": calls}
        elif entry.get("role") == "tool" and not entry.get("spill"):
            content = entry.get("content") or ""
            call_id = str(entry.get("tool_call_id") or "")
            if len(content) > MAX_REPLAYED_RESULT_CHARS:
                spill = known.get(call_id)
                # A result a flush keeps (*user_id*) that an earlier write cut
                # without keeping it is kept now.
                if spill is None or (spill.get("file_id") is None and user_id):
                    name = names.get(call_id) or str(entry.get("name") or "tool")
                    filename = f"interrupted-turn-result-{name}-{call_id[-8:] or 'call'}.txt"
                    file_id: str | None = None
                    if user_id:
                        try:
                            from app.services.builtin_tools._shared import _persist_generated_file

                            text = kept_text(content) if kept_text is not None else content
                            row = await _persist_generated_file(
                                user_id=user_id,
                                filename=filename,
                                raw_bytes=text.encode("utf-8"),
                                content_type="text/plain",
                                conversation_id=(
                                    files_conversation_id
                                    if files_conversation_id is not None or files_run_id is not None
                                    else str(conversation_id)
                                ),
                                assignment_run_id=files_run_id,
                                source=SPILL_SOURCE,
                            )
                            file_id = str(row.id)
                        except Exception as exc:
                            logger.warning(
                                "Could not keep a %d-character result of conversation %s whole for its "
                                "resume; the replay shows its start only: %s",
                                len(content),
                                conversation_id,
                                format_exception_message(exc),
                            )
                    spill = {"file_id": file_id, "chars": len(content), "filename": filename, "name": name}
                    known[call_id] = spill
                entry = {
                    **entry,
                    "content": content[:REPLAY_HEAD_CHARS]
                    + _cut_note(
                        spill["chars"],
                        spill["file_id"],
                        spill["filename"],
                        cut_named_ids(content, exclude=[spill["file_id"] or ""]),
                    ),
                    "spill": spill,
                }
        out.append(entry)
    return out


def with_spills(entries: list[dict], stored: object) -> list[dict]:
    """The cut results of *entries* named again with the files *stored* kept them in, as ``spilled`` lists them.

    Each entry the stored checkpoint cut gets its ``spill`` back (so a later
    write keeps no second file); the return is ``ResumedWork.spilled``.
    """
    spills = _spilled_of(stored)
    spilled: list[dict] = []
    for entry in entries:
        spill = spills.get(str(entry.get("tool_call_id") or "")) if entry.get("role") == "tool" else None
        if spill is not None:
            entry["spill"] = spill
            spilled.append({"call_id": entry.get("tool_call_id"), **spill})
    return spilled


def _spilled_of(entries: object) -> dict[str, dict]:
    """The cut results a stored checkpoint names, by call id."""
    spilled: dict[str, dict] = {}
    for entry in entries if isinstance(entries, list) else ():
        if isinstance(entry, dict) and entry.get("role") == "tool" and isinstance(entry.get("spill"), dict):
            spilled[str(entry.get("tool_call_id") or "")] = entry["spill"]
    return spilled


def _arguments_line(arguments: object, limit: int = 160) -> str:
    try:
        text = json.dumps(arguments, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(arguments)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def resume_note(
    resumed: ResumedWork, *, workspace: str | None = None, code_interpreter: bool = False
) -> ChatMessage | None:
    """The line the resuming turn reads after the replayed rounds; ``None`` with nothing replayed.

    The calls replayed, the last one completed, the results cut and the file
    each is whole in, and the sandbox workspace's manifest: what the
    model needs to go on from the last completed call. Facts only.
    *code_interpreter* says the resumed run can call the code interpreter
    (``code_interpreter.code_interpreter_callable``): only then does the note
    name ``attachments/`` (#2677); without it a cut result is read with
    ``read_file_content`` alone.
    """
    calls = [call for message in resumed.messages for call in message.tool_calls or ()]
    if not calls and not resumed.in_flight:
        return None
    if calls:
        last = calls[-1]
        parts = [
            f"[Resumed: this turn continues one that was interrupted before it answered. Its {len(calls)} "
            f"completed tool call(s) are replayed above with their results, in order; the last completed call "
            f"was {last.name}({_arguments_line(last.arguments)})."
        ]
    else:
        parts = ["[Resumed: this turn continues one that was interrupted before it answered. No tool call completed."]
    if resumed.in_flight:
        started = [
            f"{c.get('name') or 'tool'}({_arguments_line(c.get('arguments'))})"
            + (" answered, its result not kept" if c.get("answered") else "")
            + (f": {c['outcome']}" if c.get("outcome") else "")
            for c in resumed.in_flight
        ]
        parts.append(
            f"{len(started)} call(s) were started when it was interrupted and their outcome was not recorded; "
            "each may have taken effect: " + "; ".join(started) + ". Check what each one did before you "
            "repeat it: the same write repeated as it was is answered without being run once."
        )
    seen = _seen_now(resumed)
    poisoned = [s for s in seen if s["cuts"] >= POISON_CUTS]
    if poisoned:
        parts.append(
            "In flight at "
            + "; ".join(f"{s['cuts']} consecutive interruptions: {s['name']}" for s in poisoned)
            + ". The same call with the same arguments is not run again in this turn: it may be what "
            "stops the turn. Answer from what you have, or call it differently (narrower arguments, a "
            "workspace path). A tool that answers the job an identical call started (a service start) "
            "runs again and answers what that call left."
        )
    if resumed.spilled:
        named = [
            f"{s.get('name') or 'tool'} ({int(s.get('chars') or 0):,} characters) -> "
            + (f"conversation file {s['file_id']}" if s.get("file_id") else "not kept")
            for s in resumed.spilled[:_NOTE_MAX_SPILLED]
        ]
        more = f", +{len(resumed.spilled) - _NOTE_MAX_SPILLED} more" if len(resumed.spilled) > _NOTE_MAX_SPILLED else ""
        parts.append(
            f"{len(resumed.spilled)} result(s) are replayed cut and kept whole as conversation files "
            + (
                "(read_file_content, or attachments/ in code_interpreter): "
                if code_interpreter
                else "(read_file_content): "
            )
            + "; ".join(named)
            + more
            + "."
        )
    if workspace:
        parts.append(f"The sandbox workspace (workspace/) holds: {workspace}")
    # The prompt's start time is stale by the length of the cut (#8660).
    parts.append(clock_sentence())
    return ChatMessage(
        role="assistant",
        content="\n".join(parts) + ("]" if not parts[-1].endswith("]") else ""),
        in_flight=[dict(c) for c in resumed.in_flight] or None,
        in_flight_seen=seen,
    )


def _seen_now(resumed: ResumedWork) -> list[dict]:
    """The in-flight calls of this resume with the consecutive cuts each was in flight at.

    A call the previous resume's note (the last one among the replayed
    messages) also names, by digest, counts one more cut; any other call
    counts one. A call stored without a digest is not counted. A cut the
    worker's stop caused (:data:`IN_FLIGHT_CUT_KEY` on the call) adds nothing:
    the call keeps the count it had, so a rollout never poisons a long wait.
    """
    previous: dict[str, int] = {}
    for message in resumed.messages:
        if message.role == "assistant" and message.in_flight_seen is not None:
            previous = {s["digest"]: s["cuts"] for s in message.in_flight_seen}
    return [
        {
            "name": str(c.get("name") or "tool"),
            "digest": str(c["digest"]),
            "cuts": previous.get(str(c["digest"]), 0) + (0 if c.get(IN_FLIGHT_CUT_KEY) else 1),
        }
        for c in resumed.in_flight
        if c.get("digest") and not c.get("answered")
    ]


def in_flight_poison(
    messages: Iterable[ChatMessage], *, cut_safe: frozenset[str] | set[str] = frozenset()
) -> dict[str, dict]:
    """The calls a resumed turn never runs again unchanged, by :func:`call_digest`.

    Read from the latest resume note in *messages*: each call it names as in
    flight at :data:`POISON_CUTS` consecutive interruptions or more, read or
    write. A read is no safer than a write here: a call that stops its worker
    (a huge log parsed on the event loop) stops every attempt that repeats it,
    and the turn never answers. A call of a tool named in *cut_safe*
    (``ToolDefinition.cut_safe``: its work runs outside the worker and an
    identical repeat answers the job it started, a lab start say) is left
    out: a long call is the one a rollout cuts, never the cause of the cut.
    """
    found: dict[str, dict] = {}
    for message in messages:
        if message.role == "assistant" and message.in_flight_seen is not None:
            found = {
                s["digest"]: s
                for s in message.in_flight_seen
                if s["cuts"] >= POISON_CUTS and s.get("name") not in cut_safe
            }
    return found


def in_flight_poison_refusal(seen: dict) -> str:
    """The answer the loop gives in place of a call :func:`in_flight_poison` names."""
    return (
        f"Not run: this {seen.get('name') or 'tool'} call, with these arguments, was in flight at "
        f"{seen.get('cuts')} consecutive interruptions of this turn, so it may be what stops it. It is not run "
        "again unchanged in this turn. Answer from what you have, or call it differently (narrower arguments, "
        "a window, a workspace path)."
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _messages(entries: object) -> list[ChatMessage]:
    """Stored *entries* read back as messages; a malformed entry is left out."""
    if not isinstance(entries, list):
        return []
    messages: list[ChatMessage] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        content = entry.get("content")
        if role not in ("assistant", "tool") or not isinstance(content, str):
            continue
        calls: list[ToolCall] | None = None
        raw_calls = entry.get("tool_calls")
        if raw_calls is not None:
            if not isinstance(raw_calls, list):
                continue
            try:
                calls = [
                    ToolCall(
                        id=str(call["id"]),
                        name=str(call["name"]),
                        arguments=(
                            call["arguments"]
                            if isinstance(call.get("arguments"), dict)
                            else {}
                        ),
                    )
                    for call in raw_calls
                ]
            except (KeyError, TypeError, AttributeError):
                continue
        messages.append(
            ChatMessage(
                role=role,
                content=content,
                tool_calls=calls or None,
                tool_call_id=_text(entry.get("tool_call_id")),
                name=_text(entry.get("name")),
                not_run=_text(entry.get("not_run")),
                wait_cut=entry.get("wait_cut") if isinstance(entry.get("wait_cut"), dict) else None,
                in_flight_seen=_seen_of(entry.get("in_flight_seen")),
            )
        )
    return messages


def _seen_of(value: object) -> list[dict] | None:
    """A stored note's ``in_flight_seen`` read back; a malformed record is left out."""
    if not isinstance(value, list):
        return None
    return [
        {"name": str(item.get("name") or "tool"), "digest": str(item["digest"]), "cuts": int(item["cuts"])}
        for item in value
        if isinstance(item, dict) and item.get("digest") and isinstance(item.get("cuts"), int)
    ]


def _aware(value: object) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _young_enough(updated_at: datetime, now: datetime | None) -> bool:
    return (now or datetime.now(UTC)) - updated_at <= RESUME_MAX_AGE


async def kept_tool_calls(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> int:
    """How many completed tool calls the next turn of *conversation_id* resumes.

    What :func:`load_resumable` would hand that turn, counted for the
    conversation to show next to its interrupted turn; ``0`` when there is no
    checkpoint, it is too old or it cannot be read. A read, not a resume: it
    logs nothing and changes nothing.
    """
    try:
        row = (
            await db.execute(
                select(ChatTurnCheckpoint.entries, ChatTurnCheckpoint.updated_at).where(
                    ChatTurnCheckpoint.conversation_id == conversation_id
                )
            )
        ).one_or_none()
    except Exception as exc:
        logger.debug(
            "Could not read the turn checkpoint of conversation %s: %s",
            conversation_id,
            format_exception_message(exc),
        )
        return 0
    if row is None:
        return 0
    stored, updated_at = row
    updated_at = _aware(updated_at)
    if updated_at is None or not _young_enough(updated_at, now):
        return 0
    return sum(len(m.tool_calls or ()) for m in _completed_rounds(_messages(stored)))


async def load_resumable(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> ResumedWork:
    """The work an interrupted turn of *conversation_id* left to resume.

    Read on *db* by the turn that holds the conversation's lock, so no turn
    still writes it. A checkpoint older than :data:`RESUME_MAX_AGE` is found
    but not resumed; what the stored rounds hold is checked again
    (:func:`completed_work`), so a malformed row resumes what is sound in it.
    """
    try:
        row = (
            await db.execute(
                select(
                    ChatTurnCheckpoint.entries,
                    ChatTurnCheckpoint.turn_id,
                    ChatTurnCheckpoint.updated_at,
                ).where(ChatTurnCheckpoint.conversation_id == conversation_id)
            )
        ).one_or_none()
        if row is None:
            return ResumedWork()
        stored, turn_id, updated_at = row
        updated_at = _aware(updated_at)
        if updated_at is None:
            return ResumedWork(found=True)
        if not _young_enough(updated_at, now):
            logger.info(
                "Conversation %s has a turn checkpoint from %s, too old to "
                "resume",
                conversation_id,
                updated_at.isoformat(),
            )
            return ResumedWork(found=True)
        messages = _completed_rounds(_messages(stored))
        in_flight = _in_flight_of(stored)
    except Exception as exc:
        logger.warning(
            "Could not read the turn checkpoint of conversation %s; the turn "
            "starts without it: %s",
            conversation_id,
            format_exception_message(exc),
        )
        return ResumedWork()
    entries = _entries(messages)
    spilled = with_spills(entries, stored)
    if messages:
        replayed = sum(len(m.tool_calls or ()) for m in messages)
        logger.warning(
            "Conversation %s resumes %d tool call(s) of turn %s, which ended "
            "before it stored its answer (checkpointed %s, %d result(s) cut)",
            conversation_id,
            replayed,
            turn_id or "?",
            updated_at.isoformat(),
            len(spilled),
        )
        from app.services.sandbox_workspace import metrics

        metrics.resume_results("replayed", replayed)
        metrics.resume_results("truncated", len(spilled))
    if in_flight:
        logger.warning(
            "Conversation %s resumes turn %s with %d call(s) started and not recorded as answered: %s",
            conversation_id,
            turn_id or "?",
            len(in_flight),
            ", ".join(c["name"] for c in in_flight),
        )
    return ResumedWork(messages=messages, entries=entries, found=True, spilled=spilled, in_flight=in_flight)


async def load_replay(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    user_id: object = None,
    now: datetime | None = None,
    code_interpreter: bool = False,
) -> tuple[ResumedWork, list[ChatMessage]]:
    """:func:`load_resumable`, and the messages the resuming turn appends to its history.

    The replayed rounds, then :func:`resume_note` with the manifest of the
    conversation's sandbox workspace when it has one. *code_interpreter*: the
    resuming turn can call the code interpreter (:func:`resume_note`).
    """
    resumed = await load_resumable(db, conversation_id, now=now)
    if not resumed.messages and not resumed.in_flight:
        return resumed, []
    from app.services.sandbox_workspace.landed import judge_for_resume
    from app.services.sandbox_workspace.session import manifest_for_resume

    # Whether each sandbox write the cut segment started landed, so the note says it.
    await judge_for_resume(resumed.in_flight, conversation_id, user_id)
    from app.services.video_studio import resume_landed

    # Whether each recording the cut segment started was created, so the note names it.
    await resume_landed.judge_for_resume(resumed.in_flight, conversation_id, user_id)
    # A file the note names is one the resumed turn can open (#11847).
    resumed = replace(resumed, spilled=await readable_spills(resumed.spilled, user_id))
    note = resume_note(
        resumed, workspace=await manifest_for_resume(conversation_id, user_id), code_interpreter=code_interpreter
    )
    return resumed.with_note(note), [*resumed.messages, *([note] if note is not None else [])]


async def discard(db: AsyncSession, conversation_id: uuid.UUID) -> None:
    """Delete the checkpoint of *conversation_id* in the caller's transaction.

    Called by a turn that stored its answer, before it commits, so the answer
    and the deletion land together. Inside a SAVEPOINT: a failure here leaves
    a stale checkpoint behind, never the answer unstored.
    """
    try:
        async with db.begin_nested():
            await db.execute(
                delete(ChatTurnCheckpoint).where(
                    ChatTurnCheckpoint.conversation_id == conversation_id
                )
            )
    except Exception as exc:
        logger.warning(
            "Could not delete the turn checkpoint of conversation %s; the next "
            "turn resumes it: %s",
            conversation_id,
            format_exception_message(exc),
        )


class TurnCheckpoint:
    """The writer of one turn's checkpoint.

    :meth:`offer` is synchronous and never blocks the loop: it keeps the latest
    snapshot and starts a writer task when none runs. The task is created from
    the loop's context, so the turn's secret registry reaches the
    write. It writes the latest snapshot, then the next one if a newer one
    came meanwhile.
    """

    def __init__(
        self,
        conversation_id: uuid.UUID,
        *,
        turn_id: str | None = None,
        resumed: ResumedWork | None = None,
        session_factory: Callable[[], Any] | None = None,
        user_id: str | None = None,
        files_conversation_id: str | None = None,
        files_run_id: str | None = None,
    ) -> None:
        self.conversation_id = conversation_id
        self.turn_id = turn_id
        self.user_id = user_id
        #: The scope a flush keeps oversized results in (``_spill``): the
        #: conversation itself unless a subclass names another one.
        self._files_conversation_id = files_conversation_id
        self._files_run_id = files_run_id
        self._inherited = list(resumed.entries) if resumed else []
        self._found = bool(resumed and resumed.found)
        self._session_factory = session_factory
        #: Results already kept whole as files, by call id.
        self._spilled: dict[str, dict] = {
            str(s.get("call_id") or ""): {k: v for k, v in s.items() if k != "call_id"}
            for s in (resumed.spilled if resumed else [])
        }
        self._pending: list[dict] | None = None
        #: The latest snapshot offered, whole: what a flush keeps files of.
        self._latest: list[dict] | None = None
        #: Set by a flushing close: the oversized results of the writes from
        #: then on are kept whole as conversation files. Before it, a write
        #: keeps their start only, so a turn that ends normally creates no file.
        self._keep_files = False
        self._task: asyncio.Task | None = None
        self._closed = False
        self._attempted = False
        self._failure_logged = False
        self._last_shape: tuple | None = None
        #: The ids (and answered flags) of the trailing round's calls last offered as in flight.
        self._last_in_flight: tuple = ()

    @property
    def may_hold_row(self) -> bool:
        """Whether the conversation may hold a checkpoint row this turn must delete."""
        return self._found or self._attempted

    @property
    def holds_work(self) -> bool:
        """Whether the checkpoint has completed tool work a next turn resumes (``interrupted_resumable``)."""
        return bool(self._inherited) or self._last_shape is not None

    @property
    def advanced(self) -> bool:
        """Whether this turn completed a tool round of its own, beyond the rounds it inherited.

        A resumed turn cut again after it advanced made progress: its next
        automatic resume is not a loop (``cut_turn_resume.enqueue_cut_turn``).
        Only a round with a tool call counts: an assistant message of text
        alone (a narration, a steering reply) is kept for the next turn but is
        no progress, so a turn that only narrates stays under the stalled cap.
        """
        return any(calls for _, _, calls in self._last_shape or ())

    def offer(self, messages: Sequence[ChatMessage]) -> None:
        """Checkpoint *messages*, what the loop appended so far."""
        if self._closed:
            return
        work = completed_work(messages)
        # The round dispatched and not all answered (offered by the loop right
        # before it runs the calls): named, never replayed.
        in_flight = in_flight_calls(messages)
        flight_shape = tuple((call["id"], call["answered"]) for call in in_flight)
        # The same rounds as last time (an iteration that added none): the
        # stored snapshot already holds them.
        shape = tuple(
            (
                entry["role"],
                entry.get("tool_call_id"),
                tuple(call["id"] for call in entry.get("tool_calls") or ()),
            )
            for entry in work
        )
        if not work and not in_flight:
            return
        if shape == (self._last_shape or ()) and flight_shape == self._last_in_flight:
            return
        if work:
            self._last_shape = shape
        self._last_in_flight = flight_shape
        # Bounded once the oversized results are cut (``_drain``): cut, they
        # no longer push whole rounds out.
        flight = [{"role": IN_FLIGHT_ROLE, "content": "", "calls": in_flight}] if in_flight else []
        self._latest = self._pending = [*_superseded_dropped(self._inherited, work), *work, *flight]
        self._start_writer()

    def _start_writer(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(
                self._drain(), name=f"turn-checkpoint:{self.conversation_id}"
            )

    async def _drain(self) -> None:
        while self._pending is not None:
            entries, self._pending = self._pending, None
            self._attempted = True
            try:
                entries = _with_cut_cause(entries)
                entries = bounded(
                    await _spill(
                        entries,
                        known=self._spilled,
                        conversation_id=self.conversation_id,
                        user_id=self.user_id if self._keep_files else None,
                        files_conversation_id=self._files_conversation_id,
                        files_run_id=self._files_run_id,
                        kept_text=self._kept_text,
                    )
                )
                await self._write(entries)
            except Exception as exc:
                if not self._failure_logged:
                    self._failure_logged = True
                    logger.warning(
                        "Could not checkpoint the turn of conversation %s; "
                        "its work is lost if it ends before its answer: %s",
                        self.conversation_id,
                        format_exception_message(exc),
                    )

    def _kept_text(self, content: str) -> str:
        """The text the file keeping an oversized result holds: the result as it is."""
        return content

    async def _write(self, entries: list[dict]) -> None:
        factory = self._session_factory
        if factory is None:
            from app.core.database import async_session as factory
        now = datetime.now(UTC)
        stmt = pg_insert(ChatTurnCheckpoint).values(
            conversation_id=self.conversation_id,
            turn_id=self.turn_id,
            entries=entries,
            created_at=now,
            updated_at=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ChatTurnCheckpoint.conversation_id],
            set_={
                "turn_id": stmt.excluded.turn_id,
                "entries": stmt.excluded.entries,
                "updated_at": stmt.excluded.updated_at,
            },
        )
        async with factory() as session:
            await session.execute(stmt)
            await session.commit()

    async def aclose(self, *, flush: bool) -> None:
        """Take no more snapshots; with *flush*, write the latest one first.

        Called once the loop is over. A turn about to store its answer closes
        without flushing: its row is deleted next, and the write in flight is
        waited for so that it cannot land after the deletion. A turn that
        ended on an error flushes, so the next turn resumes all of its work:
        the latest snapshot is written once more, its oversized results kept
        whole as conversation files. A write still running after
        :data:`CLOSE_TIMEOUT_SECONDS` is cancelled, and the row keeps the
        previous write, whose oversized results show their start only.
        """
        self._closed = True
        if not flush:
            self._pending = None
        elif self._latest is not None and self.user_id:
            self._keep_files = True
            self._pending = self._latest
            self._start_writer()
        task = self._task
        if task is None or task.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), CLOSE_TIMEOUT_SECONDS)
        except TimeoutError:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            logger.warning(
                "The turn checkpoint of conversation %s was still being "
                "written after %.0fs; the write was cancelled",
                self.conversation_id,
                CLOSE_TIMEOUT_SECONDS,
            )
