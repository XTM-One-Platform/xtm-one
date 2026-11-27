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

"""The ``video_studio`` built-in suite: record demos, produce and render videos.

Capture and production stay apart in the tools as in the services:

* browser profiles - ``list_browser_profiles``, ``save_browser_profile`` (the
  demo environment's URLs and sign-in settings; never its credentials, which
  are typed on the Browser profiles page: a tool call's arguments stay in the
  conversation and its traces), ``sign_in_browser_profile`` and ``submit_browser_mfa_code`` (an
  interactive MFA: the person gives the code in the chat, or approves a push),
  ``test_browser_profile_sign_in`` (a dry run that saves nothing and returns
  the step trace), ``check_browser_profile_session`` (the pre-flight a
  recording runs: the start URL opened in the saved session, renewed by itself
  when it can), ``snapshot_browser_profile_page`` (a page of the
  signed-in application looked at before scripting: its screenshot and its
  interactive elements' accessible names, nothing typed; how long it
  took to settle once it opened);
* recordings - ``record_browser_demo`` (a scripted take in the backend
  browser), ``get_video_recording`` (its action log, to place callouts on
  steps, and a contact sheet the agent looks at; its holds on a page still
  loading flagged), ``list_video_recordings``;
* AI media - ``generate_video_clip`` (from a first frame - an image, a
  recording's step, another shot's end; a failed or a ready shot
  generated again in place with ``clip_id``; silent with ``sound: false``;
  with reference images its subjects keep, its frames read against
  its prompt; with ingredients, its frames kept and measured against
  its start frame), ``preview_start_frame`` (that frame, looked at before paying, a
  page still loading flagged), ``save_video_frame`` (a frame of a recording, a shot or a video
  file - a render's MP4 its answers name - kept as a picture of the conversation; a region of it cut at
  100 %), ``compose_picture`` (real
  pictures drawn inside a picture, flat or in perspective),
  ``generate_music`` (an AI track, fitted to a video and made its music with
  ``video_id``; composed again at the video's length with ``refit``;
  generated again in place with ``music_id``) and ``preview_voiceover``;
* production - ``create_video`` / ``edit_video`` (the timeline spec, whole or
  by operations; screen inserts on footage), ``get_video``,
  ``list_videos``, ``render_video``, ``render_video_scene`` (one scene as a
  draft with its transitions, in the preview lane),
  ``delete_video_renders`` (renders by selector or id, with their files,
  ``render_cleanup``);
* sharing - ``share_video`` (people and groups, the deck's one sharing
  write) and ``set_video_public_link`` (the link anyone may watch);
* jobs - ``get_video_jobs`` waits on recordings, clips, AI tracks and renders
  (without ids, lists the person's jobs in flight), ``cancel_video_job`` stops
  one of theirs; ``video_studio_capabilities`` says which speech, clip and
  music providers the person may use, the voices, models, built-in tracks and
  quality presets.

Every picture a result shows (a contact sheet, a poster, a sign-in screen) is
a file of the conversation, attached once and named after (:func:`_file_refs`);
a render's answer names its checks' files - its inserts' crops, its findings'
frames - by those copies too, never by the render's own ids.

Recordings, clips and renders run in the worker; a tool waits for them only
until shortly before its own deadline (``jobs.tool_wait_budget``) and names
what to call to follow up. Every write runs on a session of its own. The
provider names, voices and models are data of ``services/video_studio``.

What a job's state is - queued, running, ready, failed, its stage and its
last progress - is ``jobs.job_state`` read from the job's row; the
tools that report it declare ``live_state``, so the agent loop never answers
them from its identical-call cache.

A job started in a chat turn is owed to its conversation: the tool
passes the conversation to ``jobs.enqueue`` and to every wait that reports
the job, and the worker resumes a conversation never told how a job ended
(``services/video_studio/wake.py``).
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import inspect
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import String, cast, func, select

from app.core.config import settings
from app.core.database import async_session
from app.core.utils import format_exception_message, safe_uuid
from app.llm.base import AGENT_CHECK_FILE_SOURCE
from app.models.browser_profile import BrowserProfile
from app.models.user import User
from app.models.video_studio import (
    JOB_PENDING,
    JOB_READY,
    JOB_RUNNING,
    JOB_TERMINAL_STATUSES,
    VideoProject,
    VideoRender,
    VideoTake,
)
from app.services.video_studio import worker_class as worker_class_of
from app.services.video_studio.quality import check_video_quality, format_quality_report
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult
from app.tools.tool_events import VIDEO_SAVED_EVENT, emit_tool_event

logger = logging.getLogger(__name__)

_SOURCE = "builtin:video_studio"
VIDEO_MARKER = "[[VIDEO:{id}]]"
#: The voices a capabilities answer lists; a voice library holds hundreds.
_VOICES_SHOWN = 60
#: The frames of one video ``save_video_frame`` saves in a turn, each at its own moment or region,
#: and the most moments one ``save_video_frames`` call takes.
_FRAMES_A_TURN = 60
#: Where a browser profile's credentials are given: never through a tool,
#: whose arguments stay in the conversation and its traces.
_CREDENTIALS_ON_THE_PAGE = (
    "Its username, password and TOTP secret are typed by the person on the Browser profiles page "
    "(Tools & Integrations > Browser profiles), never in the chat: ask them to add them there."
)


@dataclass
class _Pictures:
    """The pictures a result shows: attached the first time the conversation sees them, named after."""

    #: The pictures attached to this result (``FileRef``), seen for the first time.
    refs: list = field(default_factory=list)
    #: Every picture's id as a file of the conversation, in the order asked.
    ids: list[str] = field(default_factory=list)
    #: The ids of those the conversation was already shown: named, never attached again.
    named: list[str] = field(default_factory=list)
    #: Each asked id -> its file in the conversation (the same id, or the conversation's copy).
    copies: dict[str, str] = field(default_factory=dict)
    #: Every file the answer names (``FileRef``), attached or not: what a run without a conversation registers.
    every: list = field(default_factory=list)

    def __add__(self, other: _Pictures) -> _Pictures:
        return _Pictures(
            [*self.refs, *other.refs],
            [*self.ids, *other.ids],
            [*self.named, *other.named],
            {**self.copies, **other.copies},
            [*self.every, *other.every],
        )

    def note(self) -> str:
        if not self.named:
            return ""
        listed = ", ".join(f"[[FILE:{fid}]]" for fid in self.named)
        return f"\nAlready shown in this conversation, not attached again (read_file_content opens it): {listed}."


#: How far back a ``record_browser_demo`` call looks for the same recording asked by the same user in the same
#: conversation: a call replayed after a run resume (or repeated by the agent) answers it instead of queueing another.
SAME_TAKE_WINDOW_SECONDS = 15 * 60
#: The statuses of a recording such a call answers: queued, recording, or recorded within the window.
_SAME_TAKE_STATUSES = (JOB_PENDING, JOB_RUNNING, JOB_READY)
#: How many recent recordings of that user and conversation are compared.
_SAME_TAKE_SCANNED = 20


async def same_take(
    session: Any,
    *,
    user_id: Any,
    conversation_id: uuid.UUID | None,
    name: str,
    submitted: dict,
    now: Any = None,
) -> VideoTake | None:
    """The recording this user already asked, in this conversation (or with none), within
    :data:`SAME_TAKE_WINDOW_SECONDS`, with the same submitted script: still queued or recording whatever its name (a
    cosmetic field a retry after a resume often changes), recorded only with the same name (a new take of a finished
    one stays possible under another name); ``None`` when there is none. ``record_browser_demo`` answers it instead of
    queueing a copy: a call replayed after a run resume, or repeated, never records twice."""
    from datetime import UTC, datetime, timedelta

    cutoff = (now or datetime.now(UTC)) - timedelta(seconds=SAME_TAKE_WINDOW_SECONDS)
    conversation = (
        VideoTake.conversation_id.is_(None) if conversation_id is None else VideoTake.conversation_id == conversation_id
    )
    result = await session.execute(
        select(VideoTake)
        .where(
            VideoTake.user_id == user_id,
            conversation,
            VideoTake.created_at >= cutoff,
            VideoTake.status.in_(_SAME_TAKE_STATUSES),
        )
        .order_by(VideoTake.created_at.desc())
        .limit(_SAME_TAKE_SCANNED)
    )
    for row in result.scalars().all():
        if row.submitted_script == submitted and (row.name == name or row.status in (JOB_PENDING, JOB_RUNNING)):
            return row
    return None


def _result(
    text: str, *, error: bool = False, files: list | _Pictures | None = None, outcome: bool = False
) -> ToolResult:
    """A tool result; *outcome*: the error is a generation that came out rejected, not a wrong call (``outcome_failure``)."""
    if isinstance(files, _Pictures):
        text, files = text + files.note(), files.refs
    return ToolResult(
        tool_call_id="", content=text, is_error=error, files=files or None, outcome_failure=error and outcome
    )


def _json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _script_words(take: VideoTake) -> str:
    """The ``script`` block of get_video_recording: the steps and parameters *take* was submitted with, as JSON."""
    from app.services.video_studio.take_script import readback

    kept = readback(getattr(take, "submitted_script", None))
    if kept is None:
        return (
            "Script: null (recorded before the submitted script was kept; "
            f"record_browser_demo(from_recording_id='{take.id}') records it again from its validated script)."
        )
    block = json.dumps({"script": kept}, ensure_ascii=False, separators=(",", ":"))
    return (
        f"Script as submitted (record_browser_demo(from_recording_id='{take.id}') records it again, "
        f"any argument given overriding it):\n{block}"
    )


async def _user(db, user_id: str) -> User | None:
    uid = safe_uuid(user_id)
    return await db.get(User, uid) if uid else None


#: The most recordings ``list_video_recordings`` answers in one call; ``offset`` reads past them.
MAX_RECORDINGS_LISTED = 50
#: The shortest recording id prefix ``get_video_recording`` resolves, in hex characters.
MIN_RECORDING_PREFIX = 8
#: How many candidates an ambiguous prefix names.
PREFIX_CANDIDATES_SAID = 5
#: The canonical text of a UUID, ``x`` for a hex character.
_UUID_SHAPE = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
_HEX = frozenset("0123456789abcdef")


def recording_id_prefix(value: object) -> str | None:
    """*value* as the start of a recording id's canonical text (lower case, hyphens where a UUID has them), or ``None``
    when it is not one or is shorter than :data:`MIN_RECORDING_PREFIX` hex characters.

    ``8a3f2b1c``, ``8A3F2B1C-9D`` and ``8a3f2b1c9d`` are prefixes; ``8a3f`` (too short), ``8a3f-2b1c`` (a hyphen
    out of place) and ``8a3f2b1z`` are not."""
    text = str(value or "").strip().lower()
    hexes = text.replace("-", "")
    if len(hexes) < MIN_RECORDING_PREFIX or len(hexes) > 32 or not set(hexes) <= _HEX:
        return None
    shaped: list[str] = []
    rest = iter(hexes)
    for slot in _UUID_SHAPE:
        if slot == "-":
            shaped.append("-")
            continue
        char = next(rest, None)
        if char is None:
            break
        shaped.append(char)
    canonical = "".join(shaped).rstrip("-")
    # Hyphens the caller typed must sit where a UUID has them.
    if "-" in text and not canonical.startswith(text.rstrip("-")):
        return None
    return canonical


def take_prefix_query(owner_id: uuid.UUID, prefix: str) -> Any:
    """The recordings of *owner_id* whose id starts with the canonical *prefix*, newest first. Only the user's
    own recordings are resolved by prefix: one shared through a video is read by its full id, as before."""
    return (
        select(VideoTake)
        .where(VideoTake.user_id == owner_id, cast(VideoTake.id, String).like(f"{prefix}%"))
        .order_by(VideoTake.created_at.desc())
    )


async def resolve_take_id(session: Any, user: Any, recording_id: object) -> tuple[uuid.UUID | None, ToolResult | None]:
    """The id of the recording *recording_id* names: a full id as given, an id prefix of 8+ hexadecimal characters
    (:func:`recording_id_prefix`) resolved among *user*'s own recordings when it starts exactly one
    (:func:`take_prefix_query`). A prefix starting several answers the refusal naming up to
    :data:`PREFIX_CANDIDATES_SAID` of them; a prefix starting none answers ``(None, None)``. ``get_video_recording``
    and ``record_browser_demo(from_recording_id=...)`` read a recording id through it."""
    text = str(recording_id or "").strip()
    tid = safe_uuid(text)
    if tid is not None or user is None:
        return tid, None
    prefix = recording_id_prefix(text)
    if prefix is None:
        return None, None
    found = (
        (await session.execute(take_prefix_query(user.id, prefix).limit(PREFIX_CANDIDATES_SAID + 1))).scalars().all()
    )
    if len(found) > 1:
        shown = ", ".join(f"`{t.id}` ({t.name})" for t in found[:PREFIX_CANDIDATES_SAID])
        more = " and more" if len(found) > PREFIX_CANDIDATES_SAID else ""
        return None, _result(
            f"Recording id prefix `{prefix}` is ambiguous: it matches {shown}{more}. "
            "Call again with a longer prefix or the full id.",
            error=True,
        )
    return (found[0].id if found else None), None


def _like_text(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def recordings_query(owner_id: uuid.UUID, *, name_contains: str = "", take_ids: set[uuid.UUID] | None = None) -> Any:
    """What ``list_video_recordings`` pages through, newest first: the user's own recordings, or, with
    *take_ids* (a video's), those recordings whoever made them (each checked readable after); *name_contains*
    keeps those whose name holds it, case-insensitively."""
    query = select(VideoTake)
    if take_ids is not None:
        query = query.where(VideoTake.id.in_(sorted(take_ids, key=str)))
    else:
        query = query.where(VideoTake.user_id == owner_id)
    if name_contains:
        query = query.where(VideoTake.name.ilike(f"%{_like_text(name_contains)}%", escape="\\"))
    return query.order_by(VideoTake.created_at.desc(), VideoTake.id.desc())


#: How a picture a tool shows is stored, and the conversation's copy of one: a check file, shown as a working
#: file, one per bytes per conversation, purged after ``AGENT_CHECK_FILE_RETENTION_DAYS`` unless a video uses it.
_SHOWN_FILE_SOURCE = AGENT_CHECK_FILE_SOURCE
#: The sources a conversation's copy was stored under: ``agent_working`` in older copies.
_SHOWN_FILE_SOURCES = (AGENT_CHECK_FILE_SOURCE, "agent_working")


async def _conversation_copy(conversation_id: uuid.UUID, digest: str | None) -> Any | None:
    """The conversation's copy of a picture with these bytes, shown by any message or none yet; ``None`` for none."""
    from app.models.file_attachment import FileAttachment

    if not digest:
        return None
    async with async_session() as db:
        return (
            (
                await db.execute(
                    select(FileAttachment)
                    .where(
                        FileAttachment.conversation_id == conversation_id,
                        FileAttachment.sha256 == digest,
                        FileAttachment.source.in_(_SHOWN_FILE_SOURCES),
                    )
                    .order_by(FileAttachment.created_at)
                    .limit(1)
                )
            )
            .scalars()
            .first()
        )


async def _file_refs(
    file_ids: list[object],
    *,
    conversation_id: uuid.UUID | None = None,
    user_id: str = "",
    shown: set[str] | None = None,
    show: bool = True,
    run_copies: dict[str, str] | None = None,
) -> _Pictures:
    """The stored pictures a result shows (a contact sheet, a poster, a sign-in prompt), so the model sees them.

    In a conversation, each is a file OF that conversation: one the
    conversation does not hold - a recording's contact sheet, a shot's or a
    render's poster, which stay their media's - is shown through a copy the
    conversation owns (byte-identical copies are one file per conversation),
    so the agent opens it again with its file tools, after a restart too, and
    deleting the conversation never takes the media's own picture with it.

    And each is attached ONCE per conversation: a picture the
    conversation was already shown - earlier in this turn (*shown*, the ids
    this turn's tools attached), by a message of an earlier turn, or through a
    copy it already holds - is named, never attached again, so polling a batch
    of shots never pays for the same posters on every answer.

    *show* ``False`` links them instead: each becomes a file of the
    conversation the same way, its id in ``ids`` / ``copies`` for the answer
    to name (``read_file_content`` opens it), and none is attached or counted
    as shown. A copy keeps the stored file's caption (its ``description``):
    a frame read once in a turn answers a second read with it.

    In a run without a conversation (a background task, an assignment run),
    a picture that is not the agent's own file - a render's frame, a shot's
    poster - is shown through a check file of the run, one per
    picture a run (*run_copies*, the asked id to its copy's): what the run
    hands back to its parent is then a working file of its own, never the
    media's picture, which a parent reply would otherwise take over.
    """
    from app.llm.base import FileRef, is_agent_generated_source
    from app.models.file_attachment import FileAttachment
    from app.services.builtin_tools._shared import _persist_generated_file, store_description
    from app.services.file_storage import file_storage

    pictures = _Pictures()
    ids = list(dict.fromkeys(fid for fid in (safe_uuid(str(f)) for f in file_ids if f) if fid))
    if not ids:
        return pictures
    async with async_session() as db:
        rows = (await db.execute(select(FileAttachment).where(FileAttachment.id.in_(ids)))).scalars().all()
    found = {row.id: row for row in rows}
    seen = shown if shown is not None else set()

    def ref(row: Any, asked: Any) -> FileRef:
        # A conversation's copy is byte-identical to the file asked: what the copy's row does not carry is the asked one's.
        return FileRef(
            file_id=str(row.id),
            filename=getattr(row, "filename", None) or asked.filename,
            content_type=getattr(row, "content_type", None) or asked.content_type,
            size=getattr(row, "size", None) or asked.size,
            s3_object_name=getattr(row, "s3_object_name", None) or asked.s3_object_name,
            source=getattr(row, "source", None),
            notes=getattr(row, "description", None) or None,
        )

    def attach(row: Any, asked: Any) -> None:
        pictures.ids.append(str(row.id))
        pictures.copies[str(asked.id)] = str(row.id)
        pictures.every.append(ref(row, asked))
        if show:
            pictures.refs.append(pictures.every[-1])
            seen.add(str(row.id))

    def name(row: Any, asked: Any) -> None:
        pictures.ids.append(str(row.id))
        pictures.copies[str(asked.id)] = str(row.id)
        pictures.every.append(ref(row, asked))
        if show:
            pictures.named.append(str(row.id))

    for row in (found[fid] for fid in ids if fid in found):
        if conversation_id is None and user_id and run_copies is not None and not is_agent_generated_source(row.source):
            copied = run_copies.get(str(row.id))
            if copied is None:
                try:
                    copy = await _persist_generated_file(
                        user_id=user_id,
                        filename=row.filename,
                        raw_bytes=await file_storage.download(row.s3_object_name),
                        content_type=row.content_type,
                        conversation_id=None,
                        assignment_run_id=None,
                        source=_SHOWN_FILE_SOURCE,
                        description=row.description,
                    )
                except Exception as exc:  # the picture is still shown for this turn
                    logger.warning("Video Studio file %s not copied for the run: %s", row.id, format_exception_message(exc))
                    attach(row, row)
                    continue
                run_copies[str(row.id)] = str(copy.id)
                attach(copy, row)
            else:
                async with async_session() as db:
                    kept = await db.get(FileAttachment, uuid.UUID(copied))
                if kept is None:
                    run_copies.pop(str(row.id), None)
                    attach(row, row)
                else:
                    name(kept, row)
        elif conversation_id is None or not user_id:
            attach(row, row)
        elif row.conversation_id == conversation_id:
            if str(row.id) in seen or row.message_id is not None:
                name(row, row)
            else:
                attach(row, row)
        else:
            try:
                raw = None
                digest = row.sha256
                if not digest:
                    raw = await file_storage.download(row.s3_object_name)
                    digest = hashlib.sha256(raw).hexdigest()
                existing = await _conversation_copy(conversation_id, digest)
                if existing is not None:
                    if row.description and not existing.description:
                        # Its caption, so a second read in the turn answers with it.
                        await store_description(existing, row.description)
                    name(existing, row)
                    continue
                copy = await _persist_generated_file(
                    user_id=user_id,
                    filename=row.filename,
                    raw_bytes=raw if raw is not None else await file_storage.download(row.s3_object_name),
                    content_type=row.content_type,
                    conversation_id=str(conversation_id),
                    assignment_run_id=None,
                    source=_SHOWN_FILE_SOURCE,
                    description=row.description,
                )
            except Exception as exc:  # the picture is still shown for this turn
                logger.warning(
                    "Video Studio file %s not copied to the conversation: %s", row.id, format_exception_message(exc)
                )
                attach(row, row)
                continue
            attach(copy, row)
    return pictures


def register_named_files(context: Any, pictures: _Pictures) -> None:
    """Put every file an answer names in the run's file context, for a run without a conversation.

    In a conversation each is a file of it (:func:`_file_refs`), which ``read_file_content`` finds by its scope. A
    run without one - an assignment run - has no such scope: a render's crops, frames and captions stay the render's,
    so each named file joins the run's file context as a file it fetched to READ (never a deliverable), where
    ``read_file_content`` finds it in that call and every later one of the run. Only files the answer was allowed to
    name are given: the person the run acts for can read the render they come from. A file already there is left.
    """
    if context is None:
        return
    from app.tools.base import register_fetched_file

    for ref in pictures.every:
        if context.get(str(ref.file_id)) is None:
            register_fetched_file(context, ref)


def _fmt_seconds(ms: int | None) -> str:
    return f"{(ms or 0) / 1000:.1f} s"


def _ultra_size(width: Any, height: Any) -> str:
    """The size an ultra render of a video framed *width* x *height* draws at (``spec.QUALITY_PRESETS``): ``3840x2160``."""
    from app.services.video_studio.spec import QUALITY_PRESETS

    w, h = max(int(width or 0), 1), max(int(height or 0), 1)
    scale = QUALITY_PRESETS["ultra"].short_side / min(w, h)
    return f"{round(w * scale)}x{round(h * scale)}"


def _moved(state: Any) -> str:
    """``, last moved 4 s ago`` from a job state's heartbeat, then its lease when it is late; empty before
    the worker wrote one."""
    from app.services.video_studio import jobs

    seconds = state.as_dict().get("last_progress_seconds_ago")
    moved = "" if seconds is None else f", last moved {seconds} s ago"
    return moved + jobs.lease_clause(state)


def stalled_line(render: Any) -> str:
    """A render's stalls on a line of their own: where each stood and the frames it kept; then its worker
    changes: the attempt, what it resumed after and the frame it resumed from; empty for none."""
    from app.services.video_studio.render_process import relaunch_line, stall_line

    said = [text for text in (stall_line(getattr(render, "stalls", None)), relaunch_line(getattr(render, "relaunches", None))) if text]
    return "".join(f"\n{text}" for text in said)


def estimated_line(render: Any) -> str:
    """A render's estimate and what its drawing measured on a line of their own; empty for neither."""
    from app.services.video_studio.render_preflight import said_of

    said = said_of(render)
    return f"\n{said}" if said else ""


async def ranges_line(render: Any) -> str:
    """Where the scene ranges of a render drawn on several workers stand; empty for one drawn whole."""
    from app.services.video_studio import render_ranges

    try:
        said = render_ranges.describe_ranges(await render_ranges.rows_of(render.id))
    except Exception as exc:  # diagnostics: the render's own state is said whatever
        logger.debug("Render ranges not read: %s", format_exception_message(exc))
        return ""
    return f"\n{said}" if said else ""


#: The renders ``get_video`` lists, newest first; the storage line counts them all.
_RENDERS_LISTED = 8


def live_versions_line() -> str:
    """``live build 1.261106.0, quality report v52, screen-insert drawing 21``: what the running release judges and
    draws at, said once above ``get_video``'s render list so a render drawn at an older drawing reads as one."""
    from app.core.config import settings
    from app.services.video_studio import quality, screen_insert

    return (
        f"live build {settings.app_version or 'not recorded'}, quality report v{quality.QUALITY_REPORT_VERSION}, "
        f"screen-insert drawing {screen_insert.DRAWING_VERSION}"
    )


#: The node steps a failed recording's result says how they were located, the last ones.
_NODE_STEPS_SAID = 6


def render_list_line(
    render: Any, state: Any, *, current: object, size: int | None = None, kept: str | None = None
) -> str:
    """One render in ``get_video``'s list: its state, its MP4 file once made, whether the video plays it, the
    bytes its files hold and what keeps it from the retention (*kept*: ``render_retention.kept_by``)."""
    from app.services.video_studio import jobs, render_segments
    from app.services.video_studio.render_checks import provenance_line, release_line, report_provenance
    from app.services.video_studio.resources import format_bytes

    kept_until = render_segments.frames_kept_until(render)
    if state.status == jobs.STATE_READY:
        detail = f", {_fmt_seconds(render.duration_ms)} {render.width}x{render.height}"
    elif state.status == jobs.STATE_FAILED:
        detail = f": {state.reason}"
    else:
        detail = f" ({jobs.describe(state)}{_moved(state)}; get_video_jobs(render_ids=['{render.id}']) waits for it)"
    # A failed render keeps its MP4 to look at: any moment of it can be saved as a picture.
    made = render.file_id and state.status in (jobs.STATE_READY, jobs.STATE_FAILED)
    ended = render.status in JOB_TERMINAL_STATUSES
    judged = provenance_line(
        report_provenance(
            getattr(render, "checks", None), getattr(render, "relaunches", None), getattr(render, "drawing", None)
        )
    )
    # The release that drew it, beside the checker that judged it: what a start compares to cover a revision.
    drawn = release_line(getattr(render, "drawing", None))
    return (
        f"- render `{render.id}`: {state.status} {render.quality} rev {render.revision}{detail}"
        + (f", MP4 file `{render.file_id}`" if made else "")
        + (f" (scene `{render.scene_id}` preview)" if render.scene_id else "")
        + (f", {drawn}" if drawn and ended else "")
        + (f", {judged}" if judged and ended else "")
        + (f", {render_segments.kept_until_line(kept_until)}" if kept_until else "")
        + (f", {format_bytes(size)} stored (size_bytes {size})" if size is not None and ended else "")
        + (f", kept_by {kept}" if kept else ", due: the nightly retention deletes it" if ended and size is not None else "")
        + (" - current (what the video plays)" if render.id == current else "")
    )


#: The characters of a spec ``get_video`` shows in one answer: whole scenes up to it, never one cut.
SPEC_PAGE_CHARS = 12000
#: The parts of a video ``get_video`` answers with, all by default.
GET_VIDEO_FIELDS = ("spec", "scenes", "voiceover", "music", "renders")
#: The parts shown only when asked: the studio requests made on the video (``studio_feedback``) and the
#: revision history (``revisions.list_revisions``).
GET_VIDEO_ASKED_FIELDS = ("feedback", "revisions")
_SCENE_RANGE = re.compile(r"(\d+)\s*-\s*(\d*)")


def video_fields(raw: Any) -> set[str]:
    """The parts ``get_video`` answers with (:data:`GET_VIDEO_FIELDS`), or :class:`ValueError` naming them."""
    if raw in (None, "", []):
        return set(GET_VIDEO_FIELDS)
    asked = raw if isinstance(raw, list) else str(raw).split(",")
    fields = {str(f).strip().lower() for f in asked if str(f).strip()}
    known = (*GET_VIDEO_FIELDS, *GET_VIDEO_ASKED_FIELDS)
    unknown = sorted(fields - set(known))
    if unknown:
        raise ValueError(f"fields takes {', '.join(known)}; not {', '.join(unknown)}.")
    return fields


def scene_selection(raw: Any, scenes: list) -> list[int]:
    """The scenes a ``get_video`` *scenes* selector names, as 0-based indexes in spec order, or :class:`ValueError`.

    Scene ids, 1-based numbers and ranges (``18-24``, ``18-`` to the last), as a list or comma-separated; none: all.
    """
    count = len(scenes)
    if raw in (None, "", []):
        return list(range(count))
    by_id = {str(s.get("id")): i for i, s in enumerate(scenes) if isinstance(s, dict) and s.get("id")}
    picked: list[int] = []
    for item in raw if isinstance(raw, list) else str(raw).split(","):
        text = str(item).strip()
        if not text:
            continue
        span = _SCENE_RANGE.fullmatch(text)
        if text in by_id:
            picked.append(by_id[text])
        elif text.isdigit() and 1 <= int(text) <= count:
            picked.append(int(text) - 1)
        elif span and 1 <= int(span[1]) <= (int(span[2]) if span[2] else count) <= count:
            picked.extend(range(int(span[1]) - 1, int(span[2]) if span[2] else count))
        else:
            raise ValueError(
                f"No scene '{text}' in this video: it has {count} scene(s), numbered 1-{count}; give scene ids, numbers "
                "or ranges such as '18-24'."
            )
    return sorted(dict.fromkeys(picked))


def scene_ranges(indexes: list[int]) -> str:
    """0-based scene indexes as the 1-based ranges a selector takes: ``[17, 18, 19, 22]`` is ``18-20,23``."""
    parts: list[tuple[int, int]] = []
    for i in sorted(set(indexes)):
        if parts and parts[-1][1] == i - 1:
            parts[-1] = (parts[-1][0], i)
        else:
            parts.append((i, i))
    return ",".join(f"{a + 1}" if a == b else f"{a + 1}-{b + 1}" for a, b in parts)


def spec_page(
    spec: dict, video_id: str, *, selected: list[int], fields: set[str], budget: int = SPEC_PAGE_CHARS
) -> str:
    """The spec as ``get_video`` shows it: never cut inside a scene, the scenes left named with their call.

    Everything but the scenes (voiceover, music, format, ...) first and whole, then the *selected* scenes, whole, as
    many as *budget* holds (one at least); the rest named with the ``get_video`` call that shows them. A spec that
    fits whole is the spec as written, as before.
    """
    scenes = spec.get("scenes") if isinstance(spec.get("scenes"), list) else []
    every_part = "spec" in fields
    head = {k: v for k, v in spec.items() if k != "scenes" and (every_part or k in fields)}
    show_scenes = every_part or "scenes" in fields
    shown: list[int] = []
    if show_scenes:
        used = len(json.dumps(head, indent=1))
        for i in selected:
            size = len(json.dumps(scenes[i], indent=1)) + 2
            if shown and used + size > budget:
                break
            shown.append(i)
            used += size
    whole = every_part and shown == list(range(len(scenes)))
    if whole:
        return f"Spec as written (edit_video works on it):\n```json\n{json.dumps(spec, indent=1)}\n```\n"
    body = dict(head)
    if show_scenes:
        body["scenes"] = [scenes[i] for i in shown]
    named = f"scenes {scene_ranges(shown)} of {len(scenes)}" if shown else f"no scene (of {len(scenes)})"
    parts = ", ".join(k for k in head) or "none"
    text = (
        f"Part of the spec as written ({named}; other keys: {parts}) - edit_video works on the whole spec, so keep "
        f"what is not shown here:\n```json\n{json.dumps(body, indent=1)}\n```\n"
    )
    left = [i for i in selected if i not in shown] if show_scenes else []
    if left:
        text += (
            f"Scenes {scene_ranges(left)} did not fit this answer ({budget} characters of spec at most, whole scenes "
            f"only): get_video(video_id='{video_id}', scenes='{scene_ranges(left)}') shows them.\n"
        )
    return text


def storage_line(renders: list, sizes: dict, standing: Any) -> str:
    """What the video's renders hold in storage, by kind, and what frees it."""
    from app.models.video_studio import JOB_CANCELLED, JOB_FAILED
    from app.services.video_studio.resources import format_bytes

    kinds: dict[str, list] = {"ready renders": [], "scene previews": [], "failed or cancelled": []}
    for render in renders:
        if render.status in (JOB_FAILED, JOB_CANCELLED):
            kinds["failed or cancelled"].append(render)
        elif render.status == JOB_READY:
            kinds["scene previews" if render.scene_id else "ready renders"].append(render)
    total = sum(sizes.get(render.id, 0) for render in renders)
    parts = ", ".join(
        f"{len(rows)} {label} {format_bytes(sum(sizes.get(r.id, 0) for r in rows))}" for label, rows in kinds.items() if rows
    )
    held = "1 render holds" if len(renders) == 1 else f"{len(renders)} renders hold"
    line = f"Storage: {held} {format_bytes(total)} (size_bytes {total}): {parts}."
    spare = [r for r in renders if r.status in JOB_TERMINAL_STATUSES and not standing.is_latest_ready(r)]
    if spare:
        line += (
            f" {len(spare)} of them are not the newest ready render of their quality: delete_video_renders frees them "
            "when the user asks (dry_run: true first lists what would go)."
        )
    return line


def render_frames_line(file_id: object, duration_ms: int | None) -> str:
    """How to look at any moment of a whole render's MP4 *file_id* before it is called final; empty without one."""
    if not file_id:
        return ""
    return (
        f" Its MP4 is file `{file_id}`: save_video_frame(file_id='{file_id}', at_seconds=t) saves its frame at any "
        f"moment t up to {_fmt_seconds(duration_ms)} as a picture to look at."
    )


async def _state_of(kind: str, row: Any) -> Any:
    from app.services.video_studio import jobs

    return (await jobs.job_states(kind, [row]))[0]


def _measured(params: dict) -> str:
    """What a made shot's frames measure, as a sentence; empty when they were not kept."""
    from app.services.video_studio import frame_qc

    measures = frame_qc.summary(params)["frame_qc"] or {}
    parts = []
    if "reference_ssim" in measures:
        read = measures.get("reference_text_match")
        words = f", {round(read * 100)}% of its words still read" if read is not None else ""
        parts.append(f"its first frame keeps {measures['reference_ssim']:.2f} of its start frame's structure (SSIM){words}")
    if "motion_ssim" in measures:
        parts.append(f"its last frame {measures['motion_ssim']:.2f} of its first")
    if (camera := measures.get(frame_qc.CAMERA_MOTION)) is not None:
        parts.append(f"its camera moved {camera:.2f} of the frame (measured on the background)")
    return f" Frames: {'; '.join(parts)}." if parts else ""


def _failed_clip_text(clip: Any) -> str:
    """A shot that did not come out: the provider's reason, and how to recover without changing the video."""
    from app.services.video_studio import clips

    failed = clips.failed_regeneration(clip)
    if failed is not None:
        # A ready shot generated again: only the new attempt failed, the scenes still play its take.
        return (
            f"AI shot `{clip.id}` ({clip.model}): {clips.regeneration_reason(failed)} - every scene using it still "
            "plays that take and a render uses it, nothing is broken. Tell the user why the new take did not come "
            f"out. Generate it again in place with generate_video_clip(clip_id='{clip.id}') only to replace that "
            "take, changing what the reason calls for (a reworded prompt, another model, a calmer camera); "
            "discard: true drops the previous take instead of keeping it until the new one is ready."
        )
    tried = clips.fallback_note(clip)
    return (
        f"AI shot `{clip.id}` {clip.status} ({clip.model}): {clip.error or 'no reason given'}.{' ' + tried if tried else ''} "
        f"Tell the user why. Generate it again in place with generate_video_clip(clip_id='{clip.id}'), changing the "
        "prompt (a filtered prompt) or the model (one the error says is unavailable) when the reason calls for it; the "
        "scenes using it keep it. A still image is a different scene: never put one in its place without saying so."
    )


def _validated(plan: Any, *, clip_id: str | None = None, cooling: Any = None) -> ToolResult:
    """``generate_video_clip(validate_only=true)``: the request every check passed, nothing queued or paid.

    *cooling* is its provider's state: what a shot queued now meets.
    """
    from app.services.video_studio import clips

    again = f"generate_video_clip(clip_id='{clip_id}')" if clip_id else "generate_video_clip"
    provider = (
        f" Careful: {cooling.sentence()}. A shot queued now waits in the queue until then and starts by itself - "
        "nothing fails and nothing is paid twice; queue the batch once, never ask again for a shot that waits."
        if cooling is not None
        else ""
    )
    return _result(
        f"{json.dumps({'validated': True, 'clip_id': clip_id, **plan.summary(cooling=cooling)})}\n"
        f"Every check passed (model, reference images, start frame, shape): nothing was queued or paid. "
        f"{clips.mode_note(plan.params)}{provider} Call {again} with the same arguments and without validate_only "
        "to make it."
    )


#: The status of a ``generate_video_clip`` call refused on its arguments before anything reached a provider.
INVALID_PARAMETERS = "invalid_parameters"


def _invalid_parameters(reason: str, *, clip_id: str | None = None) -> ToolResult:
    """A call refused on its arguments before anything was queued or paid: a validation note, never a tool error.

    Nothing reached a provider, so it is no failure the agent loop's
    repeated-failure brake counts (``is_error`` unset), and no write that took
    effect either (``no_op``).
    """
    reason = reason.strip().rstrip(".")
    head = {"status": INVALID_PARAMETERS, "clip_id": clip_id, "reason": reason, "queued": False}
    return ToolResult(
        tool_call_id="",
        content=f"{json.dumps(head)}\nInvalid parameters, nothing was queued or paid: {reason}.",
        no_op=True,
    )


def _failed_music_text(row: Any) -> str:
    """A track that did not come out: the provider's reason, and how to recover without changing the video."""
    return (
        f"AI track `{row.id}` {row.status} ({row.model}): {row.error or 'no reason given'}. Tell the user why. "
        f"Generate it again in place with generate_music(music_id='{row.id}'), changing the prompt (a filtered "
        "prompt: no artist or song names) or the model when the reason calls for it; the videos using it keep it. "
        "A built-in track (video_studio_capabilities, music_library) is the other way to score the video."
    )


def _session_text(data: dict) -> str:
    """A serialized profile's session as the agent reads it: the probe's answer, never the cookies'."""
    challenge = data.get("mfa_challenge")
    if challenge:
        return (
            f"waiting for MFA ({challenge['kind']}, challenge_id `{challenge['challenge_id']}`, browser open until "
            f"{challenge['expires_at']})"
        )
    status = data["session_status"]
    probe = data.get("last_probe_result") or {}
    if status == "active":
        return f"signed in (checked {data.get('last_probe_at')}: start_url renders the application)"
    if status == "expired":
        return f"session expired: start_url lands on {probe.get('url') or 'nothing'} ({probe.get('reason') or ''})"
    if status == "unverified":
        return "session saved, not checked yet (check_browser_profile_session)"
    if status == "failed":
        return f"sign-in failed: {data.get('last_sign_in_message') or ''}"
    if status == "signing_in":
        return "signing in"
    return "not signed in"


def _template_text(applied: Any) -> str:
    """Which template the video wears, the scenes it adds and what it kept against the spec."""
    template = applied.template
    if template is None:
        return "Template: none (free style).\n"
    lines = [f"Template: **{template.name}** (`{template.id}`)."]
    if applied.added_scenes:
        lines.append(
            f"It adds the scenes {', '.join(applied.added_scenes)} around yours (not in the spec you edit: their "
            "length and kicker are template_scenes: {intro: {duration, kicker, align}, outro: {duration, kicker}}; "
            "each card's duration is at least what its words take to read, the quality check below saying it)."
        )
    lines.extend(f"- {note}" for note in applied.notes)
    return "\n".join(lines) + "\n"


def build_video_studio_tools(
    db: Any = None,
    user_id: str = "",
    conversation_id: str | None = None,
    automation: bool = False,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """The Video Studio tools for *user_id*; *db* is unused (every call opens its own session).

    *automation*: an assignment run, with nobody to ask - what ``render_video`` would offer to do (a re-check of a
    fallback take) it does.
    """
    from app.services.video_studio import clips as clip_providers
    from app.services.video_studio import voice as voice_providers
    from app.services.video_studio.spec import ASPECT_RATIOS, PREVIEW_QUALITIES, QUALITY_PRESETS, RENDER_QUALITIES

    user_id = str(user_id) if user_id else ""
    conversation_uuid = safe_uuid(str(conversation_id)) if conversation_id else None
    # A job a chat turn started is owed to its conversation: the worker
    # resumes it with the outcome when the turn ended first.
    resumed_note = (
        " If your turn ends first, this conversation is resumed with the outcome when the work ends."
        if conversation_uuid is not None
        else ""
    )

    #: The pictures this turn's answers attached: a later answer of the turn names them.
    shown_this_turn: set[str] = set()
    #: A run without a conversation: the check file each media picture is shown through.
    run_copies: dict[str, str] | None = {} if conversation_uuid is None else None

    def _readable(pictures: _Pictures) -> _Pictures:
        """Every file an answer names, readable by the run that asked (:func:`register_named_files`)."""
        if conversation_uuid is None:
            register_named_files(executor.file_context, pictures)
        return pictures

    async def _refs(file_ids: list[object], *, shown: set[str] | None = None, show: bool = True) -> _Pictures:
        """The files an answer names, as files of this conversation (:func:`_file_refs`), readable by the run."""
        return _readable(
            await _file_refs(
                file_ids, conversation_id=conversation_uuid, user_id=user_id, shown=shown, show=show, run_copies=run_copies
            )
        )

    async def _shown(file_ids: list[object]) -> _Pictures:
        """The pictures a result shows, as files of this conversation, each attached once (:func:`_file_refs`)."""
        return await _refs(file_ids, shown=shown_this_turn)

    async def _render_pictures(checks: dict | None, lead: list[object]) -> tuple[_Pictures, dict | None, int]:
        """What a render's answer shows: *lead* (its frames, its poster), the 100 % crops of its screen
        inserts and the frames its findings show, as files of this conversation - attached up to
        ``render_checks.ATTACHED_EVIDENCE`` (its frames, each insert's worst finding and crops first),
        the rest linked, which ``read_file_content`` opens; with its *checks* naming each by the conversation's file,
        never the render's own (``render_checks.with_files``). Also answers how many were linked."""
        from app.services.video_studio.render_checks import evidence_files, with_files

        attached, linked = evidence_files(checks, lead)
        pictures = await _shown(attached)
        if linked:
            pictures = pictures + await _refs(linked, shown=shown_this_turn, show=False)
        return pictures, with_files(checks, pictures.copies), len(linked)

    def _proofs_line(checks: dict | None, linked: int) -> str:
        """What the answer says of its findings' frames and of the files it linked rather than attached."""
        from app.services.video_studio.render_checks import ATTACHED_EVIDENCE, finding_frame_ids

        said = ""
        if finding_frame_ids(checks):
            said = (
                " The frame each finding shows is attached too (its data.frame_file_id): look at it before changing anything."
                if not linked
                else " The worst finding's frame of each insert is attached (its data.frame_file_id)."
            )
        if linked:
            said += (
                f" {linked} more file(s) its checks name are linked rather than attached (at most {ATTACHED_EVIDENCE} "
                "pictures an answer): each is named above, and read_file_content opens one when you need it."
            )
        return said

    async def _frames_named(params: dict) -> dict:
        """A shot's ``frame_qc.summary``, its frames named as files of this conversation, never attached."""
        from app.services.video_studio import frame_qc

        summary = frame_qc.summary(params)
        kept = [summary[name] for name, _at in frame_qc.FRAMES if summary[name]]
        if not kept:
            return summary
        linked = await _refs(kept, show=False)
        return {key: linked.copies.get(value, value) if key.endswith("_file_id") else value for key, value in summary.items()}

    async def _clip_checks(clip: Any) -> dict:
        """A shot's check record as saved at completion, the block ``generate_video_clip`` answers inline: its
        frames and measures, stabilisation, logo and text checks, consistency, mode and model. Read from
        the row, never recomputed."""
        from app.services.video_studio import frame_qc

        params = clip.params or {}
        return {
            # Its own first, middle and last frames, and how they measure against its start frame.
            **(await _frames_named(params)),
            **({"ingredients_path": params[clip_providers.INGREDIENTS_PATH]} if params.get(clip_providers.INGREDIENTS_PATH) else {}),
            # How the logo drawn onto the take reads against its surface: 0-1, under 0.5 failed.
            **(
                {"integration_score": score}
                if (score := frame_qc.integration_score(params.get(frame_qc.QC_KEY))) is not None
                else {}
            ),
            # The video's references the shot is held to and how they reached the model: native | prompt.
            **(
                {"consistency": {"path": kept.get("path"), "references": [item.get("id") for item in kept.get("references") or []]}}
                if isinstance(kept := params.get(clip_providers.CONSISTENCY), dict)
                else {}
            ),
            "mode": clip_providers.request_mode(params),
            # Which model made the take, when the shot falls back to others.
            "model": clip.model,
            **({"fallback": fallback} if (fallback := clip_providers.fallback_state(clip)) else {}),
        }

    async def _failed_attempt(clip: Any) -> dict | None:
        """The regeneration of a ready shot that failed beside the take that plays: its reason, file and frames."""
        from app.services.video_studio import frame_qc

        failed = clip_providers.failed_regeneration(clip)
        if failed is None:
            return None
        return {
            "reason": failed.get("reason"),
            "file_id": failed.get("file_id"),
            **(await _frames_named({frame_qc.QC_KEY: failed.get("frame_qc")})),
        }

    # ── Browser profiles ──────────────────────────────────────────────

    async def _profile_id(session: Any, profile_id: str, user_uuid: uuid.UUID) -> tuple[str, str | None]:
        """*profile_id*, an id prefix (8+ hexadecimal characters) of one usable
        profile taken for its full id; the refusal when it starts several."""
        from app.services.browser_profiles.profiles import resolve_profile_id_prefix

        return await resolve_profile_id_prefix(session, profile_id, user_uuid)

    async def _list_profiles(url: str | None = None) -> ToolResult:
        from app.api.v1.helpers import can_manage
        from app.services.browser_profiles.profiles import covers_url, list_usable_profiles, serialize_profile

        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Browser profiles are available to signed-in users only.", error=True)
            rows = await list_usable_profiles(session, user.id)
            if url and rows:
                rows = [profile for profile in rows if covers_url(profile, str(url))]
                if not rows:
                    return _result(f"No browser profile you can use signs in {url} (its start URL or its apps).")
            if not rows:
                return _result(
                    "No browser profile yet. Create one with save_browser_profile (the demo environment's start URL); "
                    f"{_CREDENTIALS_ON_THE_PAGE}"
                )
            lines = []
            for profile in rows:
                data = serialize_profile(profile, can_manage=can_manage(user, profile))
                totp_label = {"valid": "TOTP", "vault": "TOTP", "invalid": "TOTP(invalid secret)", "none": None}.get(
                    data["totp_secret_status"]
                )
                from_vault = set(data["vault_credentials"])
                secrets = (
                    ", ".join(
                        label + (" (read from a vault)" if key in from_vault else "")
                        for key, label, on in (
                            ("username", "username", data["has_username"]),
                            ("password", "password", data["has_password"]),
                            ("totp_secret", totp_label, bool(totp_label)),
                        )
                        if on and label
                    )
                    or "no credentials"
                )
                renews = " - renews by itself" if data["renews_by_itself"] else ""
                also = "".join(f"\n  - also signs in: {app['name']} - {app['url']}" for app in data["apps"])
                lines.append(
                    f"- **{data['name']}** (`{data['id']}`): {data['start_url']} - {_session_text(data)}; {secrets}"
                    + renews
                    + (" (company)" if data["is_company_managed"] else "")
                    + also
                )
            return _result(
                "Browser profiles you can record with (the session status is the last probe of start_url in the "
                "saved session; check_browser_profile_session probes it now):\n" + "\n".join(lines)
            )

    async def _save_profile(
        name: str = "",
        start_url: str = "",
        profile_id: str | None = None,
        login_url: str | None = None,
        allowed_hosts: Any = None,
        apps: Any = None,
        login_selectors: Any = None,
        color_scheme: str | None = None,
        locale: str | None = None,
        timezone: str | None = None,
        keepalive_minutes: int | None = None,
        description: str | None = None,
        sign_in_timeout_seconds: int | None = None,
        session_save_timeout_seconds: int | None = None,
        mfa_timeout_seconds: int | None = None,
    ) -> ToolResult:
        from app.api.v1.helpers import can_manage, get_user_group_ids
        from app.models.browser_profile import KEEPALIVE_MINUTES_DEFAULT
        from app.services.audit import log_audit
        from app.services.browser_profiles.profiles import (
            ProfileError,
            apply_fields,
            clean_fields,
            destination,
            repoint_problem,
            serialize_profile,
        )

        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Browser profiles are available to signed-in users only.", error=True)
            profile = None
            if profile_id:
                pid = safe_uuid(str(profile_id))
                profile = await session.get(BrowserProfile, pid) if pid else None
                if profile is None or not can_manage(user, profile, await get_user_group_ids(session, user.id)):
                    return _result(f"No browser profile `{profile_id}` you can edit.", error=True)
            hosts = _json(allowed_hosts)
            given_apps = _json(apps)
            selectors = _json(login_selectors)
            options = {
                k: v for k, v in (("color_scheme", color_scheme), ("locale", locale), ("timezone", timezone)) if v
            }
            try:
                fields = clean_fields(
                    name=name or (profile.name if profile else ""),
                    start_url=start_url or (profile.start_url if profile else ""),
                    login_url=login_url if login_url is not None else (profile.login_url if profile else None),
                    allowed_hosts=hosts
                    if isinstance(hosts, list)
                    else (list(profile.allowed_hosts or []) if profile else []),
                    apps=given_apps if given_apps is not None else (list(profile.apps or []) if profile else []),
                    login_config=selectors
                    if isinstance(selectors, dict)
                    else (profile.login_config if profile else None),
                    options=options or (profile.browser_options if profile else None),
                    description=description if description is not None else (profile.description if profile else None),
                    keepalive_minutes=keepalive_minutes
                    if keepalive_minutes is not None
                    else (profile.keepalive_minutes if profile else KEEPALIVE_MINUTES_DEFAULT),
                    timeouts={
                        "sign_in_timeout_seconds": sign_in_timeout_seconds,
                        "session_save_timeout_seconds": session_save_timeout_seconds,
                        "mfa_timeout_seconds": mfa_timeout_seconds,
                    },
                )
                created = profile is None
                if created:
                    profile = BrowserProfile(user_id=user.id, start_url=fields.start_url, name=fields.name)
                    session.add(profile)
                before = destination(profile)
                apply_fields(profile, fields)
            except ProfileError as exc:
                return _result(str(exc), error=True)
            if not created and (refused := await repoint_problem(session, user, profile, before)):
                await session.rollback()
                return _result(f"The profile was not changed: {refused}.", error=True)
            await session.flush()
            await log_audit(
                session,
                user=user,
                action="create" if created else "update",
                entity_type="browser_profile",
                entity_id=str(profile.id),
                entity_name=profile.name,
                summary=f"{'Created' if created else 'Updated'} browser profile {profile.name}",
                details={"hosts": fields.allowed_hosts, "apps": fields.apps, "via": "agent"},
            )
            await session.commit()
            data = serialize_profile(profile, can_manage=True)
        also = "".join(f" Also signs in: {app['name']} - {app['url']}." for app in data["apps"])
        has_any = data["has_username"] or data["has_password"] or data["has_totp"]
        stored = (
            f"Credentials on file: username {'yes' if data['has_username'] else 'no'}, password "
            f"{'yes' if data['has_password'] else 'no'}, TOTP {'yes' if data['has_totp'] else 'no'}. "
        )
        return _result(
            f"{'Created' if created else 'Updated'} browser profile **{data['name']}** (`{data['id']}`) for {data['start_url']}.{also} "
            + stored
            + ("" if has_any else f"{_CREDENTIALS_ON_THE_PAGE} ")
            + "Next: sign_in_browser_profile to open and save a session."
        )

    async def _sign_in(profile_id: str = "", restart: bool = False) -> ToolResult:
        from app.services.audit import log_audit
        from app.services.browser_profiles import signin
        from app.services.browser_profiles.profiles import load_usable_profile

        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Sign-ins are available to signed-in users only.", error=True)
            profile_id, ambiguous = await _profile_id(session, profile_id, user.id)
            if ambiguous:
                return _result(ambiguous, error=True)
            profile = await load_usable_profile(session, profile_id, user.id)
            if profile is None:
                return _result(f"No browser profile `{profile_id}` you can use.", error=True)
            await log_audit(
                session,
                user=user,
                action="trigger",
                entity_type="browser_profile",
                entity_id=str(profile.id),
                entity_name=profile.name,
                summary=f"Signed browser profile {profile.name} in",
                details={"via": "agent"},
            )
            await session.commit()
        outcome = await signin.start_sign_in(
            profile, user_id=user.id, wait_seconds=min(80.0, _wait_budget(90.0)), restart=bool(restart)
        )
        return await _sign_in_result(outcome)

    async def _submit_code(challenge_id: str = "", code: str = "") -> ToolResult:
        from app.services.browser_profiles import signin

        outcome = await signin.submit_code(str(challenge_id), str(code), wait_seconds=min(60.0, _wait_budget(60.0)))
        return await _sign_in_result(outcome)

    async def _test_sign_in(profile_id: str = "") -> ToolResult:
        from app.services.audit import log_audit
        from app.services.browser_profiles import signin
        from app.services.browser_profiles.profiles import load_usable_profile

        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Sign-in tests are available to signed-in users only.", error=True)
            profile_id, ambiguous = await _profile_id(session, profile_id, user.id)
            if ambiguous:
                return _result(ambiguous, error=True)
            profile = await load_usable_profile(session, profile_id, user.id)
            if profile is None:
                return _result(f"No browser profile `{profile_id}` you can use.", error=True)
            await log_audit(
                session,
                user=user,
                action="trigger",
                entity_type="browser_profile",
                entity_id=str(profile.id),
                entity_name=profile.name,
                summary=f"Tested the sign-in of browser profile {profile.name} (dry run)",
                details={"via": "agent"},
            )
            await session.commit()
        outcome = await signin.dry_run_sign_in(profile, user_id=user.id)
        files = await _shown([outcome.screenshot_file_id]) if outcome.screenshot_file_id else []
        verdict = "Sign-in dry run succeeded" if outcome.status == signin.STATUS_ACTIVE else "Sign-in dry run did not finish"
        where = f" Stage: {outcome.stage}; last URL: {outcome.last_url}." if outcome.stage else ""
        return _result(
            f"{verdict}: {outcome.message}{where} Nothing was saved (the live session is untouched).{_trace_summary(outcome)}",
            error=outcome.status != signin.STATUS_ACTIVE,
            files=files,
        )

    def _trace_summary(outcome: Any) -> str:
        steps = getattr(outcome, "trace", None) or []
        if not steps:
            return ""
        return "\nSteps: " + " -> ".join(str(s.get("decision", "?")) for s in steps[-10:])

    async def _sign_in_result(outcome: Any) -> ToolResult:
        from app.services.browser_profiles import signin

        files = await _shown([outcome.screenshot_file_id]) if outcome.screenshot_file_id else []
        payload = {
            k: v
            for k, v in outcome.to_dict().items()
            if k in ("status", "challenge_id", "kind", "number", "expires_at", "stage", "last_url") and v
        }
        state = f"\n{json.dumps(payload)}{_trace_summary(outcome)}"
        if outcome.status == signin.STATUS_ACTIVE:
            return _result(f"Signed in: {outcome.message} Recordings reuse the saved session.{state}")
        if outcome.status == signin.STATUS_MFA_REQUIRED and outcome.kind == signin.KIND_CODE:
            return _result(
                f"MFA required: {outcome.message}\nAsk the user for the current code from their authenticator, then "
                f"call submit_browser_mfa_code(challenge_id='{outcome.challenge_id}', code=...). The platform's browser "
                f"keeps the prompt open until {outcome.expires_at}; signing in in the user's own browser tab does not "
                f"sign this profile in.{state}",
                files=files,
            )
        if outcome.status == signin.STATUS_MFA_REQUIRED:
            number = f" The number to enter on the phone is {outcome.number}." if outcome.number else ""
            return _result(
                f"Approval required: {outcome.message}\nTell the user to approve the sign-in in their authenticator "
                f"app on the phone{number} (the attached screenshot shows the prompt) - not in a browser tab of "
                f"their own. The sign-in goes on by itself once approved, until {outcome.expires_at}; call "
                f"list_browser_profiles in a minute to see the session status.{state}",
                files=files,
            )
        if outcome.status == signin.STATUS_RUNNING:
            return _result(
                f"Still signing in ({outcome.message}). Call list_browser_profiles in a moment: it shows an MFA "
                f"challenge as soon as one opens.{state}"
            )
        where = f" Stage: {outcome.stage}; last URL: {outcome.last_url}." if outcome.stage else ""
        return _result(f"The sign-in failed: {outcome.message}{where}{state}", error=True, files=files)

    async def _check_session(profile_id: str = "") -> ToolResult:
        from app.services.browser_profiles import signin
        from app.services.browser_profiles.profiles import load_usable_profile, resolved_snapshot

        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Session checks are available to signed-in users only.", error=True)
            profile_id, ambiguous = await _profile_id(session, profile_id, user.id)
            if ambiguous:
                return _result(ambiguous, error=True)
            profile = await load_usable_profile(session, profile_id, user.id)
            if profile is None:
                return _result(f"No browser profile `{profile_id}` you can use.", error=True)
            snap = await resolved_snapshot(profile)
        try:
            probe = await asyncio.wait_for(signin.probe_profile(snap), timeout=_wait_budget(90.0))
        except TimeoutError:
            return _result("The session check did not finish before the tool's deadline: try again.", error=True)
        except Exception as exc:
            return _result(f"The session check could not run: {format_exception_message(exc)}", error=True)
        renewal = ""
        if probe.authenticated:
            if snap.storage_state:
                await signin.store_probe(snap.id, probe)
        elif snap.renews_unattended and probe.http_status is None:
            # Expired, but the profile comes back by itself: a valid TOTP secret, or a saved IdP session to replay.
            outcome = await signin.start_sign_in(profile, user_id=user.id, wait_seconds=min(90.0, _wait_budget(90.0)))
            renewal = f" Renewal: {outcome.message}"
            if outcome.probe:
                probe = signin.ProbeResult(
                    authenticated=bool(outcome.probe.get("authenticated")),
                    url=str(outcome.probe.get("url") or ""),
                    reason=str(outcome.probe.get("reason") or ""),
                    checked_at=str(outcome.probe.get("checked_at") or probe.checked_at),
                    http_status=status if isinstance(status := outcome.probe.get("http_status"), int) else None,
                )
        elif snap.storage_state:
            await signin.store_probe(snap.id, probe)
        apps_checked = await _check_apps(snap) if probe.authenticated and snap.apps else []
        check = {
            "session_check": {
                "profile_id": snap.id,
                "authenticated": probe.authenticated,
                "landed_url": probe.url,
                "reason": probe.reason,
                "checked_at": probe.checked_at,
                "renews_by_itself": snap.renews_unattended,
                "stores_password": bool(snap.credentials.get("password")),
                "http_status": probe.http_status,
                **({"apps": apps_checked} if apps_checked else {}),
            }
        }
        from app.services.video_studio.take_preflight import PROFILES_PAGE

        # Without a password the sign-in stops at the form that asks for it: typed by a step, it would be
        # kept in the recording's action log.
        no_password = (
            f" The profile stores no password: once its session expires it cannot sign in by itself (the sign-in "
            f"stops at the form asking for it). Store its credentials in {PROFILES_PAGE}; a recording refuses a step "
            "that types one."
            if snap.needs_sign_in and not snap.credentials.get("password") and not snap.renews_unattended
            else ""
        )
        if probe.http_status is not None:
            verdict = (
                f"Could not check: start_url answered HTTP {probe.http_status} ({probe.reason}). The saved session "
                "is untouched and was not signed in again: do not sign the profile in, check again later."
            )
        elif probe.authenticated:
            verdict = f"Signed in: start_url renders the application ({probe.url}). record_browser_demo can use it."
        elif snap.renews_unattended:
            verdict = (
                f"Not signed in: start_url lands on {probe.url or 'nothing'} ({probe.reason}). The profile renews by "
                "itself (stored credentials that sign in without a person, a valid TOTP secret, or a saved "
                "identity-provider session)."
            )
        elif snap.renewable:
            verdict = (
                f"Not signed in: start_url lands on {probe.url or 'nothing'} ({probe.reason}). A recording tries to "
                "sign the profile in again by itself before step 1, but the MFA needs a person without a valid TOTP "
                "secret: sign_in_browser_profile does it now and says when the user must answer."
            )
        else:
            verdict = (
                f"Not signed in: start_url lands on {probe.url or 'nothing'} ({probe.reason}). A recording with this "
                "profile would stop before step 1: call sign_in_browser_profile first (the user answers the MFA)."
            )
        per_app = "".join(
            f" {app['name']}: {'signed in' if app['authenticated'] else 'not signed in'} ({app['landed_url'] or 'nothing'})."
            for app in apps_checked
        )
        return _result(f"{verdict}{per_app}{renewal}{no_password}\n{json.dumps(check)}")

    async def _check_apps(snap: Any) -> list[dict]:
        """Each application the profile declares, opened in its saved session: signed in or not, per application."""
        from app.services.browser_profiles import signin

        checked = []
        for app in snap.apps:
            try:
                probe = await asyncio.wait_for(signin.probe_profile(snap.as_app(app)), timeout=_wait_budget(60.0))
            except TimeoutError:
                checked.append({"name": app["name"], "url": app["url"], "authenticated": False, "landed_url": "", "reason": "timed out"})
                continue
            except Exception as exc:
                reason = format_exception_message(exc)
                checked.append({"name": app["name"], "url": app["url"], "authenticated": False, "landed_url": "", "reason": reason})
                continue
            checked.append(
                {
                    "name": app["name"],
                    "url": app["url"],
                    "authenticated": probe.authenticated,
                    "landed_url": probe.url,
                    "reason": probe.reason,
                }
            )
        return checked

    async def _delete_profile(profile_id: str = "", confirm: bool = False) -> ToolResult:
        from app.api.v1.helpers import can_manage, get_user_group_ids
        from app.services.audit import log_audit
        from app.services.browser_profiles.profiles import resolve_profile_id_prefix

        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Browser profiles are available to signed-in users only.", error=True)
            profile_id, ambiguous = await resolve_profile_id_prefix(session, str(profile_id or "").strip(), user.id)
            if ambiguous:
                return _result(ambiguous, error=True)
            pid = safe_uuid(str(profile_id))
            profile = await session.get(BrowserProfile, pid) if pid else None
            if profile is None or not can_manage(user, profile, await get_user_group_ids(session, user.id)):
                return _result(f"No browser profile `{profile_id}` you can delete.", error=True)
            shared = bool(profile.is_company_managed or profile.shared_group_ids or profile.shared_user_ids)
            takes = await session.scalar(
                select(func.count()).select_from(VideoTake).where(VideoTake.profile_id == profile.id)
            )
            if (shared or takes) and not confirm:
                why = ["it is shared"] if shared else []
                if takes:
                    why.append(f"{takes} recording(s) were made with it (they are kept)")
                return _result(
                    f"Not deleted: {' and '.join(why)}. Call delete_browser_profile again with confirm=true to delete "
                    f"**{profile.name}** (`{profile.id}`).",
                    error=True,
                )
            name, deleted_id = profile.name, str(profile.id)
            await log_audit(
                session,
                user=user,
                action="delete",
                entity_type="browser_profile",
                entity_id=deleted_id,
                entity_name=name,
                summary=f"Deleted browser profile {name}",
                details={"via": "agent", "recordings_kept": int(takes or 0)},
            )
            await session.delete(profile)
            await session.commit()
        return _result(f"Deleted browser profile **{name}** (`{deleted_id}`); its recordings are kept.")

    async def _snapshot_page(profile_id: str = "", url: str | None = None, reset_state: Any = None) -> ToolResult:
        from pydantic import ValidationError

        from app.llm.base import FileRef
        from app.services.browser_profiles.browser import TargetRefusedError
        from app.services.browser_profiles.profiles import load_usable_profile, resolved_snapshot, save_storage_state
        from app.services.builtin_tools._shared import _persist_generated_file
        from app.services.video_studio import capture, take_state
        from app.services.video_studio.spec import PageReset

        asked = _json(reset_state)
        try:
            reset = True if asked is None else asked if isinstance(asked, bool) else PageReset.model_validate(asked)
        except ValidationError as exc:
            problem = exc.errors(include_url=False)[0]
            return _result(
                "reset_state is true, false or an object of url_query, local_storage, session_storage and indexed_db "
                f"(booleans): {'.'.join(str(p) for p in problem.get('loc') or ())} {problem.get('msg')}.",
                error=True,
            )

        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Page snapshots are available to signed-in users only.", error=True)
            profile_id, ambiguous = await _profile_id(session, profile_id, user.id)
            if ambiguous:
                return _result(ambiguous, error=True)
            profile = await load_usable_profile(session, profile_id, user.id)
            if profile is None:
                return _result(f"No browser profile `{profile_id}` you can use.", error=True)
            snap = await resolved_snapshot(profile)
        target = (url or "").strip() or snap.start_url
        try:
            page = await asyncio.wait_for(capture.snapshot_page(snap, target, reset_state=reset), timeout=_wait_budget(120.0))
        except TimeoutError:
            return _result("The page did not open before the tool's deadline: try again.", error=True)
        except capture.ProfileNotAuthenticatedError as exc:
            return _result(
                f"{exc}. Sign the profile in with sign_in_browser_profile(profile_id='{snap.id}') first (the user "
                "answers the MFA if it asks).",
                error=True,
            )
        except TargetRefusedError as exc:
            return _result(f"The profile may not open {target}: {exc}", error=True)
        except Exception as exc:
            return _result(f"The page could not be looked at: {format_exception_message(exc)}", error=True)
        if page.storage_state is not None and page.probe is not None and page.probe.authenticated:
            # Signed in again to look: the profile keeps the fresh session, as a recording's would.
            async with async_session() as session:
                row = await session.get(BrowserProfile, uuid.UUID(snap.id))
                if row is not None:
                    save_storage_state(row, page.storage_state, probe=page.probe.to_dict())
                    await session.commit()
        shot = await _persist_generated_file(
            user_id=user_id,
            filename=f"{snap.name[:60]} - page.png",
            raw_bytes=page.png,
            content_type="image/png",
            conversation_id=str(conversation_uuid) if conversation_uuid else None,
            assignment_run_id=None,
            source=_SHOWN_FILE_SOURCE,
        )
        shown_this_turn.add(str(shot.id))
        listed = "\n".join(f'- {e["role"]} "{e["name"]}" -> {e["selector"]}' for e in page.names) or "- none found"
        # The page state a recording opens in: the names below are those its steps find.
        if page.opened is not None:
            state = f"Opened in the clean page state a recording opens in: {take_state.cleared_words(page.opened.get('cleared'))}.\n"
        elif snap.storage_state:
            state = "reset_state false: looked at in the page state the saved session carried from the takes before.\n"
        else:
            state = ""
        return _result(
            f"Page of **{snap.name}**: {page.url}{f' ({page.title})' if page.title else ''} - looked at in the saved "
            f"session, nothing typed, no step run. The screenshot is attached ([[FILE:{shot.id}]]).\n"
            f"{state}{page.settle_words()}\nIts "
            f"interactive elements (role, accessible name -> the selector a step uses; a step with role and name "
            f"matches these names exactly):\n{listed}",
            files=[FileRef.from_attachment(shot)],
        )

    # ── Recordings ────────────────────────────────────────────────────

    async def _record(
        name: str = "",
        steps: Any = None,
        profile_id: str | None = None,
        start_url: str | None = None,
        viewport: Any = None,
        pixel_ratio: float | None = None,
        browser_zoom: int | None = None,
        redact: Any = None,
        hide: Any = None,
        pace: str | None = None,
        color_scheme: str | None = None,
        local_storage: Any = None,
        settle_ms: int | None = None,
        end_hold_ms: int | None = None,
        record_sign_in: bool | None = None,
        target_render: str | None = None,
        video_id: str | None = None,
        scene: Any = None,
        insert: Any = None,
        fps: int | None = None,
        fps_fallback: str | None = None,
        reset_state: Any = None,
        from_recording_id: str | None = None,
        worker_class: str | None = None,
    ) -> ToolResult:
        from app.services.browser_profiles.profiles import load_usable_profile, resolved_snapshot
        from app.services.video_studio import jobs, take_preflight, take_script
        from app.services.video_studio.spec import VideoSpecError, parse_take_script, validate_take_script

        given: dict[str, Any] = {
            "profile_id": profile_id,
            "start_url": start_url,
            "viewport": _json(viewport),
            "pixel_ratio": pixel_ratio,
            "browser_zoom": browser_zoom,
            "redact": _json(redact),
            "hide": _json(hide),
            "pace": pace,
            "color_scheme": color_scheme,
            "local_storage": _json(local_storage),
            "settle_ms": settle_ms,
            "end_hold_ms": end_hold_ms,
            "record_sign_in": record_sign_in,
            "fps": fps,
            "fps_fallback": fps_fallback,
            "reset_state": _json(reset_state),
            "target_render": target_render,
            "video_id": video_id,
            "scene": scene,
            "insert": insert,
            "worker_class": worker_class_of.normalized(worker_class),
        }
        steps = _json(steps)
        if str(from_recording_id or "").strip():
            # Recorded again from a take's script: its steps and parameters, every argument given overriding them.
            source = await _source_take(str(from_recording_id).strip())
            if isinstance(source, ToolResult):
                return source
            steps, given = take_script.merged(source["script"], steps=steps, params=given)
            name = name or f"{source['name'] or 'Demo recording'} (re-recorded)"
            profile_id, start_url = given.get("profile_id"), given.get("start_url")
            viewport, pixel_ratio, browser_zoom = given.get("viewport"), given.get("pixel_ratio"), given.get("browser_zoom")
            redact, hide, pace, color_scheme = given.get("redact"), given.get("hide"), given.get("pace"), given.get("color_scheme")
            local_storage = given.get("local_storage")
            settle_ms, end_hold_ms = given.get("settle_ms"), given.get("end_hold_ms")
            record_sign_in, fps, fps_fallback = given.get("record_sign_in"), given.get("fps"), given.get("fps_fallback")
            reset_state, target_render = given.get("reset_state"), given.get("target_render")
            video_id, scene, insert = given.get("video_id"), given.get("scene"), given.get("insert")
        if steps is None:
            return _result("Give the steps, or from_recording_id to record a take again from its script.", error=True)
        # A worker class asked: refused before anything is stored or charged unless a running recording worker is of it.
        asked_class = worker_class_of.normalized(given.get("worker_class"))
        if asked_class is not None and (refused_class := await _worker_class_refusal(asked_class)):
            return _result(refused_class, error=True)
        raw: dict[str, Any] = {"steps": steps}
        for key, value in (
            ("start_url", start_url),
            ("viewport", _json(viewport)),
            ("pixel_ratio", pixel_ratio),
            ("browser_zoom", browser_zoom),
            ("redact", _json(redact)),
            ("hide", _json(hide)),
            ("pace", pace),
            ("color_scheme", color_scheme),
            ("local_storage", _json(local_storage)),
            ("settle_ms", settle_ms),
            ("end_hold_ms", end_hold_ms),
            ("record_sign_in", record_sign_in),
            ("fps", fps),
            ("fps_fallback", fps_fallback),
            ("reset_state", _json(reset_state)),
        ):
            if value is not None:
                raw[key] = value
        try:
            script = validate_take_script(raw)
        except VideoSpecError as exc:
            return _result(str(exc), error=True)
        # Before anything is stored or launched: a script longer than a take may last, a credential typed.
        played = parse_take_script(script)
        refused = take_preflight.length_refusal(played, float(settings.video_studio_max_take_seconds))
        refused = refused or take_preflight.credential_refusal(played)
        if refused:
            return _result(refused, error=True)
        if target_render not in (None, "") and target_render not in RENDER_QUALITIES:
            return _result(f"target_render is one of {', '.join(RENDER_QUALITIES)}.", error=True)
        if target_render and (not video_id or scene in (None, "")):
            return _result(
                "target_render sizes the recording for a screen insert of a saved video: give its video_id and scene "
                "(and insert when the scene has several screen inserts).",
                error=True,
            )
        sized_text = ""
        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Recordings are available to signed-in users only.", error=True)
            profile = None
            if profile_id:
                profile_id, ambiguous = await _profile_id(session, profile_id, user.id)
                if ambiguous:
                    return _result(ambiguous, error=True)
                profile = await load_usable_profile(session, profile_id, user.id)
                if profile is None:
                    return _result(f"No browser profile `{profile_id}` you can use.", error=True)
                stored = (await resolved_snapshot(profile)).credentials
                if typed := take_preflight.credential_refusal(played, stored):
                    return _result(typed, error=True)
            if not script.get("start_url") and profile is None:
                return _result("Give a start_url or a profile_id (whose start URL the recording opens).", error=True)
            if target_render:
                sized = await _sized_for_render(
                    session, user, script, target_render, video_id, scene, insert, explicit_ratio=pixel_ratio, explicit_fps=fps
                )
                if isinstance(sized, ToolResult):
                    return sized
                script, sized_text = sized
            take_name = (name or "Demo recording").strip()
            submitted = take_script.submitted(steps, given)
            existing = await same_take(
                session, user_id=user.id, conversation_id=conversation_uuid, name=take_name, submitted=submitted
            )
            if existing is not None:
                # The same recording, asked again (a call replayed after a run resume): answered, never queued twice.
                take_id = str(existing.id)
                again = f"The same recording was already asked as {take_id} ({existing.status}): not queued again."
                sized_text = f"{again}\n{sized_text}" if sized_text else again
            else:
                take = jobs.new_take(
                    user_id=user.id,
                    profile_id=profile.id if profile else None,
                    conversation_id=conversation_uuid,
                    name=take_name,
                    script=script,
                    # As given, once refused scripts (a credential typed) are out: what get_video_recording reads back.
                    submitted_script=submitted,
                )
                if target_render:
                    # Made for a screen insert: held to the insert capture floor once recorded (``insert_floor``).
                    from app.services.video_studio import insert_floor

                    take.capture = insert_floor.mark(
                        video_id=str(video_id), scene=scene, insert=insert, quality=str(target_render)
                    )
                session.add(take)
                await session.flush()
                await jobs.enqueue(
                    session,
                    "take",
                    take,
                    owed_to=conversation_uuid,
                    payload={worker_class_of.PAYLOAD_KEY: asked_class} if asked_class else None,
                )
                await session.commit()
                take_id = str(take.id)
        if asked_class:
            asked_said = (
                f"Asked for a worker of class {asked_class}: only one of that class records it, queued while busy."
            )
            sized_text = f"{sized_text}\n{asked_said}" if sized_text else asked_said
        await jobs.wake()
        rows = await jobs.wait_for("take", [take_id], timeout=_wait_budget(240.0), told_to=conversation_uuid)
        result = await _take_result(rows[0] if rows else None, include_sheet=True)
        if sized_text:
            result.content = f"{sized_text}\n{result.content}"
        return result

    async def _worker_class_refusal(asked: str) -> str | None:
        """Why no take may ask for worker class *asked* now (``worker_class.refusal``); ``None`` when a running
        recording worker is of it, or when the workers cannot be read (the take then waits for one, as any take)."""
        from app.services import worker_presence
        from app.services.video_studio.jobs import TAKE_ADAPTER

        found = await worker_presence.present()
        if found is None:
            return worker_class_of.refusal(asked, []) if worker_class_of.malformed(asked) else None
        available = worker_class_of.served(
            found,
            serves=lambda record: (
                record.get("state") == worker_presence.STATE_RUNNING and worker_presence.claims(record, TAKE_ADAPTER)
            ),
        )
        return worker_class_of.refusal(asked, available)

    async def _source_take(recording_id: str) -> dict[str, Any] | ToolResult:
        """The take *recording_id* names, as a re-record copies it (``{name, script}``), or the refusal: a take the
        user can read, as for get_video_recording."""
        from app.services.video_studio import take_script
        from app.services.video_studio.projects import can_read_project_asset

        async with async_session() as session:
            user = await _user(session, user_id)
            # A full id, or an id prefix of 8+ characters of one of the user's own recordings, as get_video_recording.
            tid, refused = await resolve_take_id(session, user, recording_id)
            if refused is not None:
                return refused
            take = await session.get(VideoTake, tid) if tid else None
            if user is None or take is None or not await can_read_project_asset(session, "take", take, user):
                return _result(f"No recording `{recording_id}` you can read to record again.", error=True)
            script = take_script.source_script(getattr(take, "submitted_script", None), take.script, take.profile_id)
            return {"name": take.name, "script": script}

    async def _sized_for_render(
        session: Any,
        user: Any,
        script: dict,
        quality: str,
        video_id: str,
        scene: Any,
        insert: Any,
        *,
        explicit_ratio: float | None,
        explicit_fps: int | None = None,
    ) -> tuple[dict, str] | ToolResult:
        """*script* sized for screen insert *insert* of *scene* in video *video_id* at a whole render of *quality*
        (``render_checks.size_recording``), and the words that say how; a refusal as a :class:`ToolResult`. Its
        motion is paced at the video's rate there unless *explicit_fps* says otherwise: 60 for a 60 fps render."""
        from app.services.video_studio.projects import load_visible_project
        from app.services.video_studio.render_checks import size_recording
        from app.services.video_studio.spec import VideoSpecError, browser_zoom, validate_take_script

        project = await load_visible_project(session, video_id, user)
        if project is None:
            return _result(f"No video `{video_id}` you can open.", error=True)
        try:
            index = None if insert in (None, "") else int(insert)
        except (TypeError, ValueError):
            return _result("insert is the index of the screen insert among its scene's overlays (its screen insert N).", error=True)
        viewport = script.get("viewport") or {}
        try:
            sized = await size_recording(
                session,
                project,
                user,
                quality=quality,
                scene=scene,
                index=index,
                viewport=(int(viewport.get("width") or 1600), int(viewport.get("height") or 900)),
                pixel_ratio=float(explicit_ratio) if explicit_ratio is not None else None,
                zoom=browser_zoom(script),
            )
        except VideoSpecError as exc:
            return _result(str(exc), error=True)
        recording = sized.recording
        if recording is None:
            return _result(
                f"{sized.words()} No recording passes the soft check there, even a window as large as a recording "
                "takes at pixel ratio 2: give the insert a smaller quad or a smaller push, or render at a lower quality.",
                error=True,
            )
        ratio = recording.pixel_ratio
        resized = {
            **script,
            "viewport": {"width": recording.viewport[0], "height": recording.viewport[1]},
            "pixel_ratio": int(ratio) if float(ratio).is_integer() else ratio,
        }
        if recording.browser_zoom != 100:
            resized["browser_zoom"] = recording.browser_zoom
        if explicit_fps is None:
            resized["fps"] = 60 if sized.fps >= 50 else 30
        return validate_take_script(resized), sized.words()

    async def _take_result(take: VideoTake | None, *, include_sheet: bool, state: Any = None) -> ToolResult:
        from app.services.video_studio import jobs, take_state

        if take is None:
            return _result("The recording was not found.", error=True)
        data = jobs.serialize_take(take)
        # What it opened on: the URL, what its page state reset cleared, a fps_fallback taken, the wait in the
        # recording lane.
        captured = getattr(take, "capture", None) or {}
        opened = take_state.opened_words(
            captured.get("opened"),
            captured.get("fallback"),
            captured.get("waited"),
            captured.get("worker"),
            # The build that recorded it and the one that finished it after a relaunch (#11407).
            build=take_state.build_words(captured),
            resumed=take_state.resumed_build_words(captured),
        )
        if take.status != JOB_READY:
            if take.status in JOB_TERMINAL_STATUSES:
                hint = ""
                if take.profile_id and (take.error or "").startswith("profile session not checked"):
                    hint = (
                        "\nNo video was recorded. The application did not answer: record again later "
                        f"(check_browser_profile_session(profile_id='{take.profile_id}') tells when it answers); "
                        "the profile does not need to be signed in again."
                    )
                elif take.profile_id and (take.error or "").startswith("profile not authenticated"):
                    hint = (
                        "\nNo video was recorded. Sign the profile in with sign_in_browser_profile(profile_id="
                        f"'{take.profile_id}') - the user answers the MFA if it asks - then record again; "
                        "check_browser_profile_session confirms it first."
                    )
                if opened:
                    hint += f"\nIt {opened}."
                # How its node steps found their node, up to the step that failed.
                nodes = [
                    f"step {event.get('step')} {event.get('action')}: {said}"
                    for event in take.events or []
                    if isinstance(event, dict) and (said := jobs.located_words(event))
                ]
                if nodes:
                    hint += "\nIts node steps: " + "; ".join(nodes[-_NODE_STEPS_SAID:]) + "."
                # What drew its canvases and what they captured alone up to the failure: the same line a ready take says.
                from app.services.video_studio.canvas_alone import take_capture_words
                from app.services.video_studio.capture_rate import rates_words

                # A take refused once measured keeps its rates too: the frames kept each second, then the canvases.
                if drawn_said := "; ".join(s for s in (rates_words(captured), take_capture_words(captured)) if s):
                    hint += f"\nCapture up to the failure: {drawn_said}."
                # A step that failed it: the contact sheet ends on the frame where it did.
                files = await _shown([take.contact_sheet_file_id]) if include_sheet and take.contact_sheet_file_id else []
                if files:
                    hint += "\nThe attached contact sheet ends on the frame where the step failed: the page it missed its element on."
                # A status, not a tool error: the call read the take as asked, whatever it ended as (#9780).
                return _result(f"Recording `{data['id']}` {take.status}: {take.error or ''}{hint}", files=files)
            state = state or await _state_of("take", take)
            return _result(
                f"Recording `{data['id']}` is {state.status} ({jobs.describe(state)}{_moved(state)}). "
                f"Call get_video_jobs(recording_ids=['{data['id']}']) to wait for it.{resumed_note}"
            )
        from app.services.video_studio import page_settle, scroll_pacing
        from app.services.video_studio.capture_rate import canvas_words

        steps = jobs.take_timeline(take)
        # A step's capture rate is said where it fell under the take's: the stretch that will step.
        slow_under = 0.9 * float((take.script or {}).get("fps") or 30)
        timeline_lines = "\n".join(
            f"  step {s['step']} {s['action']} at {s['at']} s{' - ' + str(s['label'])[:60] if s['label'] else ''}"
            + ("" if s["ok"] else " (skipped)")
            + (f" (captured at {s['capture_fps']:g} fps)" if s.get("capture_fps") is not None and s["capture_fps"] < slow_under else "")
            # Every canvas transition at its step, whatever its rate.
            + (f" ({said})" if (said := canvas_words(s.get("canvas_motion"))) else "")
            # Its motion mostly synthesised from keyframes: it steps on screen.
            + (
                f" (stepping motion: {float(s['synthesised']):.0%} of its frames synthesised from keyframes)"
                if s.get("synthesised")
                else ""
            )
            # Its scroll moving unevenly from one frame to the next: a move-move-hold cadence that steps on screen.
            + scroll_pacing.cadence_words(s.get("scroll_cadence"))
            + page_settle.settled_words(s.get("settled"))
            + (f" ({s['located']})" if s.get("located") else "")
            for s in steps
        )
        # A hold on a page still loading: seen here, before the take is placed in a video.
        holds = page_settle.holds_words(take.events or [])
        holds_line = f"{holds}\n" if holds else ""
        # A decorative loop the page always shows: a fact, never a finding.
        if ambient := page_settle.ambient_words(take.events or []):
            holds_line += f"{ambient}\n"
        notes = f"\nNotes: {take.error}" if take.error else ""
        # A repaired take: what it repaired, from what, and the residual to record again.
        from app.services.video_studio.take_repair import repair_words

        if repaired_said := repair_words(captured):
            notes = f"\n{repaired_said}{notes}"
        files = await _shown([take.contact_sheet_file_id]) if include_sheet and take.contact_sheet_file_id else []
        from app.services.video_studio.capture import layout_words, size_words
        from app.services.video_studio.projects import take_media
        from app.services.video_studio.scroll_pacing import MIN_TAKE_FPS, scroll_steps_words, take_rate
        from app.services.video_studio.timeline import recording_lengths

        # How long it plays under a narration, said before one is written: a save would say it too late.
        lengths = recording_lengths(take_media(take))
        # How far down its page keeps this layout: a narrower viewport below it records another layout.
        layout = layout_words(getattr(take, "layout", None))
        layout_line = f"{layout}\n" if layout else ""
        # Its painted size first: the size a soft insert or a legibility advice quotes for it.
        size = size_words((take.viewport_width or 1600, take.viewport_height or 900), take.pixel_ratio or 1.0, take.script)
        # Its real frame rate: what its scrolls play at, drawn again from what the browser painted.
        rate = take_rate(take.events, captured_frames=take.captured_frames, duration=(take.duration_ms or 0) / 1000)
        steps_line = (
            f"\nIts {rate.stepping()} will step: it carries {rate.fps:.1f} fps on {rate.where()} (under {MIN_TAKE_FPS}) - "
            f"{rate.remedy()}."
            if rate.steps
            else ""
        )
        # Each scroll step's rates: one that steps is seen here, before the take is placed in a video.
        scroll_steps = scroll_steps_words(take.events)
        scroll_line = f"{scroll_steps}\n" if scroll_steps else ""
        cut = data["truncated_at_step"]
        ready = "ready" if cut is None else f"ready (truncated at step {cut})"
        # How fast it captured its page: each second, and the GL backend that drew its canvases.
        from app.services.video_studio.canvas_alone import take_capture_words
        from app.services.video_studio.capture_rate import rates_words

        opened_line = f"  {opened}\n" if opened else ""
        capture_line = rates_words(captured)
        # What drew its canvases (WebGL backend, GPU or software GL) and what its canvas-only path did (``canvas_alone``).
        if drawn_said := take_capture_words(captured):
            capture_line += f"{'; ' if capture_line else ''}{drawn_said}"
        capture_line = f"Capture: {capture_line}.\n" if capture_line else ""
        # A 60 fps take drawn on software GL: its canvas transitions step; fps 30 is the fallback.
        from app.services.video_studio.capture_rate import software_gl_notice

        if notice := software_gl_notice((take.script or {}).get("fps"), captured):
            capture_line += f"{notice}\n"
        return _result(
            f"Recording **{take.name}** (`{data['id']}`) is {ready}: {_fmt_seconds(take.duration_ms)}, "
            f"{size}, {take.captured_frames} frames painted, {rate.words()}.{steps_line}\n"
            f"{holds_line}{scroll_line}{capture_line}{lengths}\n{layout_line}"
            f"Action log (use at_step in overlays and highlights):\n{opened_line}{timeline_lines}{notes}\n"
            "The attached contact sheet shows frames after the main steps: check the take reached what it should "
            "(signed in, the right page) before producing with it.",
            files=files,
        )

    async def _get_take(
        recording_id: str = "",
        include_contact_sheet: bool = True,
        repair: bool = False,
        include_script: bool = True,
        window: str = "",
    ) -> ToolResult:
        from app.services.video_studio import jobs, take_window
        from app.services.video_studio.projects import can_read_project_asset

        span = take_window.parse_window(window) if window else None
        if isinstance(span, str):
            return _result(f"Refused: {span}.", error=True)

        async with async_session() as session:
            user = await _user(session, user_id)
            # An id prefix of 8+ characters: resolved among the user's own recordings when unique.
            tid, refused = await resolve_take_id(session, user, recording_id)
            if refused is not None:
                return refused
            take = await session.get(VideoTake, tid) if tid else None
            if user is None or take is None or not await can_read_project_asset(session, "take", take, user):
                return _result(f"No recording `{recording_id}` you can read.", error=True)
            if repair:
                return await _repair_take(session, user, take, include_sheet=bool(include_contact_sheet))
        await jobs.mark_told("take", [take], conversation_uuid)
        result = await _take_result(take, include_sheet=bool(include_contact_sheet))
        if span is not None:
            # The raw capture records over the window: why a frame there shows what it shows.
            records = take_window.window_records(getattr(take, "capture", None), take.events, *span)
            records.update(await _window_frames(getattr(take, "capture", None), *span))
            block = json.dumps(records, ensure_ascii=False, separators=(",", ":"), default=str)
            result.content = f"{result.content}\nCapture records over {span[0]:g}-{span[1]:g} s:\n{block}"
        if include_script:
            result.content = f"{result.content}\n{_script_words(take)}"
        return result

    async def _window_frames(capture: dict | None, start: float, end: float) -> dict:
        """The take's frames over ``[start, end]`` from its frame list (``take_window.window_frames``), or why not."""
        import tempfile
        from pathlib import Path

        from app.services.video_studio import assets, take_window

        listed = take_window.frame_list_file(capture)
        if not listed:
            return {"frames_unlisted": "recorded before its frame list was kept: record or repair it again"}
        with tempfile.TemporaryDirectory(prefix="xtm-frames-") as folder:
            path = Path(folder) / "frames.json"
            try:
                stored = await assets.download_file(listed, str(path))
                listing = json.loads(path.read_text()) if stored is not None else None
            except (OSError, ValueError) as exc:
                return {"frames_unlisted": f"its frame list was not read: {exc}"}
        if listing is None:
            return {"frames_unlisted": "its frame list is gone"}
        frames, cut = take_window.window_frames(listing, start, end)
        return {"frames": frames, **({"frames_cut": cut} if cut else {})}

    async def _repair_take(session: Any, user: Any, source: VideoTake, *, include_sheet: bool) -> ToolResult:
        """A NEW take made by repairing the finished take *source* (``runners.run_repair``): its passes run
        again on the CPU from its keyframes, or from its MP4 when it was recorded before they were kept. The take
        repaired is kept; the answer is the new take's, saying the stretches repaired and the residual to record again."""
        from app.services.video_studio import jobs, take_repair

        if source.status != JOB_READY or source.file_id is None:
            return _result(
                f"Recording `{source.id}` is {source.status}: only a ready recording is repaired.", error=True
            )
        # Read here: no longer owed to this conversation, its own wake never comes after the repair's answer.
        await jobs.mark_told("take", [source], conversation_uuid)
        # Used now: the retention counts its days from here, not from before the repair was queued.
        take_repair.hold_source(source)
        take = jobs.new_take(
            user_id=user.id,
            profile_id=source.profile_id,
            conversation_id=conversation_uuid,
            name=f"{source.name or 'Demo recording'} (repaired)",
            script=dict(source.script or {}),
        )
        take.capture = {take_repair.REPAIR_KEY: str(source.id)}
        session.add(take)
        await session.flush()
        await jobs.enqueue(session, "take", take, owed_to=conversation_uuid)
        await session.commit()
        take_id = str(take.id)
        await jobs.wake()
        rows = await jobs.wait_for("take", [take_id], timeout=_wait_budget(240.0), told_to=conversation_uuid)
        result = await _take_result(rows[0] if rows else None, include_sheet=include_sheet)
        result.content = f"Repair of recording `{source.id}` (kept as it was) into `{take_id}`.\n{result.content}"
        return result

    async def _list_takes(
        limit: int = 20, offset: int = 0, name_contains: str = "", video_id: str = ""
    ) -> ToolResult:
        from app.services.video_studio import jobs
        from app.services.video_studio.projects import can_read_project_asset, load_visible_project
        from app.services.video_studio.spec import referenced_ids

        try:
            size = max(1, min(int(limit or 20), MAX_RECORDINGS_LISTED))
            start = max(0, int(offset or 0))
        except (TypeError, ValueError):
            return _result("limit and offset are whole numbers.", error=True)
        name = str(name_contains or "").strip()
        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Recordings are available to signed-in users only.", error=True)
            take_ids: set[uuid.UUID] | None = None
            if str(video_id or "").strip():
                # The recordings a video's spec references, whoever recorded them, each one readable.
                project = await load_visible_project(session, video_id, user)
                if project is None:
                    return _result(f"No video `{video_id}` you can open.", error=True)
                spec = project.spec if isinstance(project.spec, dict) else {}
                take_ids = {tid for raw in referenced_ids(spec)["take"] if (tid := safe_uuid(str(raw)))}
                if not take_ids:
                    return _result(f"Video `{video_id}` places no recording.")
            query = recordings_query(user.id, name_contains=name, take_ids=take_ids)
            # One row past the page says whether more remain.
            rows = list((await session.execute(query.offset(start).limit(size + 1))).scalars().all())
            more = len(rows) > size
            rows = rows[:size]
            if take_ids is not None:
                rows = [t for t in rows if await can_read_project_asset(session, "take", t, user)]
        scope = f" in video `{video_id}`" if take_ids is not None else ""
        named = f" named like '{name}'" if name else ""
        if not rows:
            if start or name or take_ids is not None:
                return _result(f"No recording{scope}{named} from offset {start}.")
            return _result("No recording yet: record one with record_browser_demo.")
        tail = (
            f"\nMore remain: call again with offset={start + size}."
            if more
            else "\nNo more recordings past these."
        )
        return _result(
            f"{'Recordings' if scope else 'Your recordings'}{scope}{named} "
            f"({start + 1} to {start + len(rows)}, newest first):\n"
            + "\n".join(
                f"- **{t.name}** (`{t.id}`): {t.status}, {_fmt_seconds(t.duration_ms)}, {jobs.serialize_take(t)['steps']} steps"
                for t in rows
            )
            + tail
        )

    # ── AI media ──────────────────────────────────────────────────────

    async def _capabilities(speech_provider_id: str | None = None, speech_model: str | None = None) -> ToolResult:
        from app.services.video_studio import templates as video_templates

        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Available to signed-in users only.", error=True)
            speech = await _speech_offer(session, user, speech_provider_id, speech_model)
            from app.llm.router import get_video_provider, video_model_listing, video_reference_listing

            video_models, not_served = await video_model_listing(session, user_id=user.id)
            ai_shots: dict = {"available": True}
            if not_served:
                # Listed by their provider, which answered HTTP 404 for them: offered nowhere, said why.
                ai_shots["not_served"] = not_served
            try:
                default_provider, default_model, default_config = await get_video_provider(session, user_id=user.id)
                default_key = (str(default_config.id), default_model)
            except ValueError as exc:
                default_key = None
                if not video_models:
                    # Said before any shot is asked for: no stand-in is chosen without the reason.
                    ai_shots = {"available": False, "reason": str(exc)}
            for entry in video_models:
                entry["default"] = (entry["provider_id"], entry["model"]) == default_key
            if default_key and not any(entry["default"] for entry in video_models):
                video_models.insert(
                    0,
                    {
                        "model": default_key[1],
                        "provider": default_config.provider,
                        "provider_id": default_key[0],
                        "label": getattr(default_config, "display_name", None) or default_config.provider,
                        **video_reference_listing(default_provider, default_key[1]),
                        "default": True,
                    },
                )
            video_models.sort(key=lambda entry: not entry["default"])
            # Only the configured default and video fallback models make AI shots: any other is listed, not usable.
            from app.services.video_studio import clips as video_clips

            configured_default, configured_fallbacks = await video_clips.configured_models(session)
            if configured_default is not None:
                for entry in video_models:
                    if entry["default"] or entry["model"] == configured_default:
                        entry["usable"] = True
                    elif entry["model"] in configured_fallbacks:
                        entry["usable"] = True
                        entry["fallback"] = configured_fallbacks.index(entry["model"]) + 1
                    else:
                        entry["usable"] = False
                        entry["not_usable_reason"] = "not allowed by configuration"
                ai_shots["configured_models"] = {
                    "default": configured_default,
                    "fallbacks": configured_fallbacks,
                    "setting": video_clips.VIDEO_MODELS_SETTING,
                }
            # A provider that refused for its quota or rate limit: shots queued on it wait until it cools -
            # all of its models for a rate limit, the model whose quota refused for a quota.
            from app.services.video_studio import provider_cooling

            cooled = await provider_cooling.coolings(
                (entry.get("provider_id"), entry.get("model")) for entry in video_models
            )
            for entry in video_models:
                cooling = cooled.get((str(entry.get("provider_id")), entry.get("model") or None))
                if cooling is not None:
                    entry["cooling"] = cooling.as_dict()
            if cooled:
                ai_shots["providers_cooling"] = sorted({cooling.sentence() for cooling in cooled.values()})
            music_offer = await _music_offer(session, user)
            default = await video_templates.default_template(session)
            template_rows = await video_templates.list_video_templates(session, user)
            template_list = [
                {
                    key: value
                    for key, value in video_templates.template_summary(
                        row, is_default=default is not None and row.id == default.id
                    ).items()
                    if key
                    in (
                        "id",
                        "name",
                        "description",
                        "is_default",
                        "purposes",
                        "supported_schemes",
                        "aspect_ratios",
                        "locked",
                        "intro",
                        "outro",
                        "watermark",
                        "shots",
                        "guidance",
                    )
                }
                for row in template_rows
            ]
            # How long renders are kept: the administrator's days, and what the nightly sweep never deletes.
            from app.services.video_studio import render_retention

            retention_rules = {
                **(await render_retention.load_policy(session)).as_dict(),
                "never_deleted": (
                    "a render in progress, the newest ready render of each quality of a video, the render its cards and "
                    "public link play, renders kept by hand; 0 days keeps renders for good"
                ),
                "scene_previews": "a scene's new preview replaces its previous one once ready",
                "sweep": "nightly; delete_video_renders frees renders at once when the user asks",
            }
            # What each quality renders on the worker: its pages, its memory ceiling, the
            # hours it may run and the longest video that fits them - known before a render is asked.
            from app.services.video_studio import render_preflight

            try:
                presets: dict = await render_preflight.limits(session)
            except Exception as exc:  # the qualities are offered whatever their limits
                logger.info("Video render limits not read: %s", format_exception_message(exc))
                presets = {name: {"output": f"{p.short_side}p {p.fps} fps"} for name, p in QUALITY_PRESETS.items()}
            # How long a scene preview waits and draws now: what a sweep of every scene costs, measured.
            from app.services.video_studio import preview_lane, preview_routing

            try:
                lane: dict | None = (await preview_lane.read_measure(session, await preview_routing.lane_workers())).as_dict()
            except Exception as exc:  # the capabilities are offered whatever the lane says
                logger.info("Video preview lane not measured: %s", format_exception_message(exc))
                lane = None
        return _result(
            json.dumps(
                {
                    "video_templates": template_list,
                    "ai_shots": ai_shots,
                    "video_models": video_models,
                    **speech,
                    **music_offer,
                    "render_quality": presets,
                    "render_retention": retention_rules,
                    "preview_lane": lane,
                    "aspect_ratios": list(ASPECT_RATIOS),
                },
                indent=1,
            )
        )

    async def _accept_attempt(clip_id: str | None, file_id: str) -> ToolResult:
        """Promote a failed take of AI shot *clip_id* to its take after review (``clips.promote_attempt``), audited."""
        from app.models.video_studio import VideoClip
        from app.services.audit import log_audit

        if not clip_id:
            return _invalid_parameters("accept_attempt promotes a failed take of a shot: give its clip_id")
        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Clips are available to signed-in users only.", error=True)
            clip_id, ambiguous = await _resolve_clip_id_prefix(session, clip_id, user)
            if ambiguous:
                return _invalid_parameters(ambiguous.removeprefix("Error: "), clip_id=str(clip_id))
            cid = safe_uuid(str(clip_id))
            clip = await session.get(VideoClip, cid, with_for_update=True) if cid else None
            if clip is None or clip.user_id != user.id:
                return _invalid_parameters(f"no AI shot `{clip_id}` of yours", clip_id=str(clip_id))
            try:
                columns, notes = clip_providers.promote_attempt(clip, file_id)
            except clip_providers.ClipError as exc:
                return _result(str(exc), error=True)
            for key, value in columns.items():
                setattr(clip, key, safe_uuid(str(value)) if key in ("file_id", "poster_file_id") and value else value)
            await log_audit(
                session,
                user=user,
                action="update",
                entity_type="video_clip",
                entity_id=str(clip.id),
                entity_name=(clip.prompt or "")[:120] or None,
                summary="Accepted a failed take of an AI shot after review (lettering under its screen insert)",
                details={"file_id": file_id, **columns["params"].get(clip_providers.ACCEPTED_ATTEMPT, {}), "via": "agent"},
            )
            await session.commit()
        return _result(
            f"Shot `{clip_id}` now plays the take `{file_id}`. {' '.join(notes)} The quality check and the render read "
            "it as the shot's take."
        )

    async def _restore_take(clip_id: str | None, file_id: str) -> ToolResult:
        """Put a previous take of AI shot *clip_id* back as its take (``clips.restore_previous_take``), audited."""
        from app.models.video_studio import VideoClip
        from app.services.audit import log_audit

        if not clip_id:
            return _invalid_parameters("restore_take puts a previous take of a shot back: give its clip_id")
        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Clips are available to signed-in users only.", error=True)
            clip_id, ambiguous = await _resolve_clip_id_prefix(session, clip_id, user)
            if ambiguous:
                return _invalid_parameters(ambiguous.removeprefix("Error: "), clip_id=str(clip_id))
            cid = safe_uuid(str(clip_id))
            clip = await session.get(VideoClip, cid, with_for_update=True) if cid else None
            if clip is None or clip.user_id != user.id:
                return _invalid_parameters(f"no AI shot `{clip_id}` of yours", clip_id=str(clip_id))
            try:
                columns, notes = clip_providers.restore_previous_take(clip, file_id)
            except clip_providers.ClipError as exc:
                return _result(str(exc), error=True)
            for key, value in columns.items():
                setattr(clip, key, safe_uuid(str(value)) if key in ("file_id", "poster_file_id") and value else value)
            await log_audit(
                session,
                user=user,
                action="update",
                entity_type="video_clip",
                entity_id=str(clip.id),
                entity_name=(clip.prompt or "")[:120] or None,
                summary="Restored a previous take of an AI shot",
                details={"file_id": file_id, "via": "agent"},
            )
            await session.commit()
        return _result(
            f"Shot `{clip_id}` now plays the take `{file_id}`. {' '.join(notes)} The take it replaced is kept: "
            "restore_take puts it back. The quality check, the preview and the render read the shot's take."
        )

    async def _generate_clip(
        prompt: str = "",
        model: str | None = None,
        provider_id: str | None = None,
        duration_seconds: int | None = None,
        aspect_ratio: str | None = None,
        resolution: str | None = None,
        negative_prompt: str | None = None,
        reference_file_id: str | None = None,
        reference_recording_id: str | None = None,
        reference_clip_id: str | None = None,
        reference_at_seconds: float | None = None,
        reference_at_step: int | None = None,
        crop: str | None = None,
        clip_id: str | None = None,
        sound: bool | None = None,
        text_check: bool | None = None,
        content_check: bool | None = None,
        reference_image_file_ids: Any = None,
        start_frame: bool | None = None,
        validate_only: bool | None = None,
        ingredients: Any = None,
        min_reference_similarity: Any = None,
        video_id: str | None = None,
        template_id: str | None = None,
        freestyle: bool | None = None,
        accept_minor_drift: bool | None = None,
        screen: Any = None,
        discard: bool | None = None,
        max_camera_motion: Any = None,
        keep_regions: Any = None,
        stray_screens: Any = None,
        stabilise: bool | None = None,
        fallback_models: Any = None,
        insert: bool | None = None,
        consistency: Any = None,
        ingredients_quality: str | None = None,
        reference_fit: str | None = None,
        accept_attempt: str | None = None,
        restore_take: str | None = None,
        **_ignored: Any,
    ) -> ToolResult:
        from app.models.video_studio import VideoClip
        from app.services.video_studio import jobs, provider_cooling

        if accept_attempt and restore_take:
            return _invalid_parameters("give accept_attempt or restore_take, not both")
        if accept_attempt:
            return await _accept_attempt(clip_id, str(accept_attempt))
        if restore_take:
            return await _restore_take(clip_id, str(restore_take))

        references = _json(reference_image_file_ids)
        ingredients = _json(ingredients)
        consistency = _json(consistency)
        screen = _json(screen)
        keep_regions = _json(keep_regions)
        stray_screens = _json(stray_screens)
        fallback_models = _json(fallback_models)
        start = {
            "reference_file_id": reference_file_id,
            "reference_recording_id": reference_recording_id,
            "reference_clip_id": reference_clip_id,
            "reference_at_seconds": reference_at_seconds,
            "reference_at_step": reference_at_step,
            "crop": crop,
        }
        # The video the shot is made for: its template's art direction shapes the shot.
        made_for = {"video_id": video_id or None, "template_id": template_id or None, "freestyle": bool(freestyle)}

        if not clip_id and not (prompt or "").strip():
            return _invalid_parameters("a clip needs a prompt describing the shot (subject, action, camera, light, style)")
        if start_frame is not None and not clip_id:
            return _invalid_parameters(
                "start_frame keeps or drops the start frame of a shot generated again (with clip_id); a new shot "
                "starts from the reference_* arguments, or from its prompt alone"
            )
        if discard and not clip_id:
            return _invalid_parameters("discard drops the ready take of a shot generated again (with clip_id)")
        # From the call's start: every call of a batch waits the same.
        started = time.monotonic()
        budget, capped = jobs.clip_inline_wait_budget()
        said: list[str] = []
        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Clips are available to signed-in users only.", error=True)
            try:
                if clip_id:
                    clip_id, ambiguous = await _resolve_clip_id_prefix(session, clip_id, user)
                    if ambiguous:
                        return _invalid_parameters(ambiguous.removeprefix("Error: "), clip_id=str(clip_id))
                    cid = safe_uuid(str(clip_id))
                    clip = await session.get(VideoClip, cid) if cid else None
                    if clip is None or clip.user_id != user.id:
                        return _invalid_parameters(f"no AI shot `{clip_id}` of yours to generate again", clip_id=str(clip_id))
                    # What the agent leaves out stays as the shot was asked.
                    changes = {
                        "prompt": prompt or None,
                        "model": model,
                        "provider_id": provider_id,
                        "duration_seconds": duration_seconds,
                        "aspect_ratio": aspect_ratio,
                        "resolution": resolution,
                        "negative_prompt": negative_prompt,
                        "sound": sound,
                        "text_check": text_check,
                        "content_check": content_check,
                        "accept_minor_drift": accept_minor_drift,
                        "reference_image_file_ids": references,
                        "start_frame": start_frame,
                        "ingredients": ingredients,
                        "min_reference_similarity": min_reference_similarity,
                        "max_camera_motion": max_camera_motion,
                        "keep_regions": keep_regions,
                        "stray_screens": stray_screens,
                        "stabilise": stabilise,
                        "fallback_models": fallback_models,
                        "screen": screen,
                        "insert": insert,
                        "consistency": consistency,
                        "ingredients_quality": ingredients_quality,
                        "reference_fit": reference_fit,
                        "discard": bool(discard),
                        **start,
                        **made_for,
                    }
                    if validate_only:
                        plan = await clip_providers.plan_restart(session, user, clip, **changes)
                        cooling = await provider_cooling.cooling_of(plan.config.id, model=plan.request.model)
                        return _validated(plan, clip_id=str(clip.id), cooling=cooling)
                    plan = await clip_providers.restart_clip(session, user, clip, owed_to=conversation_uuid, **changes)
                    label, said = plan.label, plan.notes
                    if plan.previous_take == "kept":
                        said = [*said, "Its previous take keeps playing in every scene until the new one is ready."]
                    elif plan.previous_take == "discarded":
                        said = [*said, "Its previous take was discarded (discard: true)."]
                else:
                    asked = {
                        "prompt": prompt,
                        "model": model,
                        "provider_id": provider_id,
                        "duration_seconds": duration_seconds or 8,
                        "aspect_ratio": aspect_ratio or "16:9",
                        "resolution": resolution or "1080p",
                        "negative_prompt": negative_prompt,
                        "sound": sound,
                        "text_check": text_check is not False,
                        "content_check": content_check is not False,
                        "accept_minor_drift": accept_minor_drift is not False,
                        "reference_image_file_ids": references,
                        "ingredients": ingredients,
                        "min_reference_similarity": min_reference_similarity,
                        "max_camera_motion": max_camera_motion,
                        "keep_regions": keep_regions,
                        "stray_screens": stray_screens,
                        "stabilise": stabilise,
                        "fallback_models": fallback_models,
                        "screen": screen,
                        "insert": insert,
                        "consistency": consistency,
                        "ingredients_quality": ingredients_quality,
                        "reference_fit": reference_fit,
                        **start,
                        **made_for,
                    }
                    plan = await clip_providers.plan_clip(session, user, **asked)
                    if validate_only:
                        cooling = await provider_cooling.cooling_of(plan.config.id, model=plan.request.model)
                        return _validated(plan, cooling=cooling)
                    clip = await clip_providers.queue_clip(session, user, plan, conversation_id=conversation_uuid)
                    label, said = plan.label, plan.notes
            except clip_providers.ClipError as exc:
                hint = " (video_studio_capabilities lists the video models you can use)" if exc.about_model else ""
                return _invalid_parameters(f"{str(exc).rstrip('. ')}{hint}", clip_id=str(clip_id) if clip_id else None)
            chosen_model, chosen_seconds = clip.model, (clip.params or {}).get("duration_seconds")
            # Queued on a provider that cools: nothing comes out before it does, so the call does not wait.
            held = clip.provider_state == provider_cooling.HELD_BY_PROVIDER
            said = [*said, clip_providers.mode_note(clip.params or {})]
            await session.commit()
            clip_id = str(clip.id)
        retry_note = (" ".join(said) + " ") if said else ""
        await jobs.wake()
        rows = await jobs.wait_for(
            "clip",
            [clip_id],
            timeout=0.0 if held else max(0.0, budget - (time.monotonic() - started)),
            told_to=conversation_uuid,
        )
        clip = rows[0] if rows else None
        if clip is None:
            return _result(f"The AI shot `{clip_id}` was not found.", error=True)
        state = await _state_of("clip", clip)
        # Known once the worker cut it: the frame kept from a video, else the start image.
        frame_id = clip_providers.start_frame_file_id(clip.params)
        params = clip.params or {}
        outcome = {
            "clip_id": clip_id,
            "status": "generating",
            "waited_seconds": round(time.monotonic() - started),
            "completion": "inline",
            "reason": None,
            "start_frame_file_id": frame_id,
            # Its frames, measures and checks as saved on the row; get_video_jobs serves the same block.
            **(await _clip_checks(clip)),
        }
        if state.status == jobs.STATE_FAILED:
            outcome.update(status="failed", reason=state.reason)
            failed = await _failed_attempt(clip)
            if failed is not None:
                # The attempt that failed beside the take that plays: its frames, to look at what came out.
                outcome["previous_take_plays"] = True
                outcome["failed_attempt"] = failed
            return _result(f"{json.dumps(outcome)}\n{retry_note}{_failed_clip_text(clip)}", error=True, outcome=True)
        if state.status != jobs.STATE_READY:
            if state.held is not None:
                limit = "inline wait skipped"
            else:
                limit = f"inline wait budget of {round(budget)} s exceeded" + (
                    " (capped by this call's deadline)" if capped else ""
                )
            outcome.update(completion="background", reason=f"{limit}: {jobs.describe(state)}")
            direction = clip_providers.directed(clip.params or {})
            directed = f"Made under the {direction.get('template')} art direction." if direction else ""
            where = "waits in the queue for its provider" if state.held is not None else "is being generated"
            plays = (
                " - the scenes using it keep playing its previous take until the new one is ready and passes its checks"
                if clip_providers.keeps_take(clip.params)
                else " (the preview shows it as being generated)"
            )
            return _result(
                f"{json.dumps(outcome)}\n{retry_note}"
                f"Clip `{clip_id}` {where} ({chosen_model} on {label}, {chosen_seconds} s) in the background: place "
                f'it in the video now as {{"type": "clip", "clip_id": "{clip_id}"}}{plays}. Call '
                f"get_video_jobs(clip_ids=['{clip_id}']) before rendering; never ask for the same shot again while it "
                f"waits.{' ' + directed if directed else ''}{resumed_note}"
            )
        outcome["status"] = "ready"
        made_notes = [*((clip.params or {}).get("notes") or []), *filter(None, [clip_providers.fallback_note(clip)])]
        notes = (" " + " ".join(made_notes)) if made_notes else ""
        files = await _shown([clip.poster_file_id])
        opened = ""
        if frame_id and isinstance((clip.params or {}).get("reference_video"), dict):
            # The frame cut from the footage, before the poster so the model sees what the shot opened on.
            frame = await _shown([frame_id])
            files = frame + files
            if frame.ids:
                how = "attached before the shot's poster" if frame.refs else "shown earlier"
                opened = f" The frame it opened on ({how}): [[FILE:{frame.ids[0]}]]."
        sound = (
            " Under the video's music its own sound is off by default (audio: true on the scene keeps it)."
            if clip.has_audio
            else ""
        )
        return _result(
            f"{json.dumps(outcome)}\n{retry_note}"
            f"Clip `{clip_id}` is ready: {_fmt_seconds(clip.duration_ms)}, {clip.width}x{clip.height}"
            f"{', with sound' if clip.has_audio else ''}.{notes}{opened}{_measured(params)} Use it in a video as "
            f'{{"type": "clip", "clip_id": "{clip_id}"}}.{sound}',
            files=files,
        )

    async def _save_frame(
        recording_id: str | None = None,
        clip_id: str | None = None,
        file_id: str | None = None,
        at_step: int | None = None,
        at_seconds: float | None = None,
        region: Any = None,
        deliver: Any = False,
        **_ignored: Any,
    ) -> ToolResult:
        import tempfile

        from app.llm.base import FileRef
        from app.services.builtin_tools._shared import _persist_generated_file

        # A frame the person asked for is a deliverable (a download card); any other is a check file.
        delivered = deliver is True or str(deliver).strip().lower() == "true"

        box = None
        if region not in (None, "", {}):
            try:
                box = clip_providers.frame_region(_json(region))
            except clip_providers.ClipError as exc:
                return _result(f"{exc}", error=True)
        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Frames are available to signed-in users only.", error=True)
            try:
                source = await clip_providers.resolve_frame(
                    session,
                    user,
                    recording_id=recording_id,
                    clip_id=clip_id,
                    file_id=file_id,
                    at_seconds=at_seconds,
                    at_step=at_step,
                )
            except clip_providers.ClipError as exc:
                return _result(f"{exc}", error=True)
        with tempfile.TemporaryDirectory(prefix="xtm-frame-") as work:
            try:
                if box is None:
                    png = await clip_providers.frame_picture(source, work)
                else:
                    png = await clip_providers.frame_picture(source, work, max_side=clip_providers.REGION_FRAME_MAX_SIDE)
            except clip_providers.ClipError as exc:
                return _result(f"{exc}", error=True)
        at = source.get("at_seconds")
        moment = (
            f"step {source['step']}"
            if source.get("step") is not None
            else (f"{at:.1f}s" if at is not None else "last-frame")
        )
        stem = (source.get("name") or source["kind"]).rsplit(".", 1)[0]
        label = clip_providers.frame_label(source)
        cut = ""
        if box is not None:
            # A region at 100 %: the frame's own pixels, never scaled - an insert judged on what a render drew.
            png, (left, top, right, bottom), (width, height) = await asyncio.to_thread(clip_providers.cut_region, png, box)
            cut = (
                f", its region [{', '.join(f'{v:.4g}' for v in box)}] cut at 100% ({right - left}x{bottom - top} px of the "
                f"{width}x{height} frame)"
            )
        row = await _persist_generated_file(
            user_id=user_id,
            filename=f"{stem[:60]} - {moment}{' - region' if box else ''}.png",
            raw_bytes=png,
            content_type="image/png",
            conversation_id=str(conversation_uuid) if conversation_uuid else None,
            assignment_run_id=None,
            source="agent_download" if delivered else _SHOWN_FILE_SOURCE,
            description=f"{label[:1].upper()}{label[1:]}{cut}, kept as a picture.",
        )
        late = (
            "That moment is past its end: its last frame is kept instead. "
            if source.get("clamped_from") is not None
            else ""
        )
        late = clip_providers.resolved_prefix_note(recording_id or clip_id or file_id, source) + late
        if box is not None:
            return _result(
                f"{late}Saved {label}{cut} as the picture `{row.id}` ([[FILE:{row.id}]]): the real pixels of that moment, "
                "to judge a screen insert's compositing on (its bezel, its key's edges, its sharpness, its colours).",
                files=[FileRef.from_attachment(row)],
            )
        return _result(
            f"{late}Saved {label} as the picture `{row.id}` ([[FILE:{row.id}]]). "
            "Use it as an image scene "
            f'{{"type": "image", "file_id": "{row.id}"}}, a statement backdrop {{"file_id": "{row.id}"}}, or the '
            f"start picture of an AI shot (generate_video_clip reference_file_id).",
            files=[FileRef.from_attachment(row)],
        )

    async def _save_frames(
        recording_id: str | None = None,
        clip_id: str | None = None,
        file_id: str | None = None,
        at_seconds: Any = None,
        step_seconds: Any = None,
        from_seconds: Any = None,
        to_seconds: Any = None,
        region: Any = None,
        keep_frames: Any = False,
        **_ignored: Any,
    ) -> ToolResult:
        """Many frames of one video in one call: one labelled contact sheet attached; each frame a file named
        only with *keep_frames*, since most frames of a sweep are never opened."""
        import tempfile

        from app.llm.base import FileRef
        from app.services.builtin_tools._shared import _persist_generated_file
        from app.services.video_studio.render_checks import clock

        box = None
        if region not in (None, "", {}):
            try:
                box = clip_providers.frame_region(_json(region))
            except clip_providers.ClipError as exc:
                return _result(f"{exc}", error=True)
        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Frames are available to signed-in users only.", error=True)
            try:
                # The video once, checked like one frame's: whoever can read it, ready (its last frame: no moment).
                source = await clip_providers.resolve_frame(
                    session, user, recording_id=recording_id, clip_id=clip_id, file_id=file_id
                )
            except clip_providers.ClipError as exc:
                return _result(f"{exc}", error=True)
        source = {**source, "at_seconds": None, "step": None}
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="xtm-frames-") as work:
            try:
                sweep = await clip_providers.frame_sweep(
                    source,
                    work,
                    box=box,
                    at_seconds=_json(at_seconds),
                    step_seconds=step_seconds,
                    from_seconds=from_seconds,
                    to_seconds=to_seconds,
                )
                sheet = await asyncio.to_thread(
                    clip_providers.frame_grid, [(clock(f.at_seconds), f.png) for f in sweep.frames]
                )
            except clip_providers.ClipError as exc:
                return _result(f"{exc}", error=True)
        stem = (source.get("name") or source["kind"]).rsplit(".", 1)[0][:60]
        # 'video "launch.mp4"' without its article: every sentence below says "of the {what}" itself.
        what = clip_providers.frame_label({**source, "at_seconds": None}).removeprefix("the last frame of the ")
        cut = ""
        if box is not None and sweep.cut is not None:
            left, top, right, bottom = sweep.cut
            cut = (
                f", its region [{', '.join(f'{v:.4g}' for v in box)}] cut at 100% ({right - left}x{bottom - top} px of the "
                f"{sweep.frame_size[0]}x{sweep.frame_size[1]} frame)"
            )
        conversation = str(conversation_uuid) if conversation_uuid else None
        keep = keep_frames is True or str(keep_frames).strip().lower() == "true"
        frames: list[dict] = []
        for frame in sweep.frames:
            if not keep:
                frames.append(
                    {"at_seconds": frame.at_seconds} | ({"asked_seconds": frame.asked} if frame.asked is not None else {})
                )
                continue
            moment = f"{frame.at_seconds:.2f}s"
            row = await _persist_generated_file(
                user_id=user_id,
                filename=f"{stem} - {moment}{' - region' if box else ''}.png",
                raw_bytes=frame.png,
                content_type="image/png",
                conversation_id=conversation,
                assignment_run_id=None,
                source=_SHOWN_FILE_SOURCE,
                description=f"The frame at {frame.at_seconds:.2f} s of the {what}{cut}, kept as a picture.",
            )
            frames.append(
                {"at_seconds": frame.at_seconds, "file_id": str(row.id)}
                | ({"asked_seconds": frame.asked} if frame.asked is not None else {})
            )
            # Named, not attached: a file the run READ (never a deliverable), in a conversation too, so
            # sandbox_put_file and code_interpreter's attachments/ find it in this run (dev:video-frames-sandbox-put).
            register_named_files(executor.file_context, _Pictures(every=[FileRef.from_attachment(row)]))
        first, last = sweep.frames[0].at_seconds, sweep.frames[-1].at_seconds
        sheet_row = await _persist_generated_file(
            user_id=user_id,
            filename=f"{stem} - {first:.2f}s-{last:.2f}s{' - region' if box else ''} - contact sheet.png",
            raw_bytes=sheet,
            content_type="image/png",
            conversation_id=conversation,
            assignment_run_id=None,
            source=_SHOWN_FILE_SOURCE,
            description=(
                f"Contact sheet of {len(frames)} frames of the {what}{cut}, from {first:.2f} s to {last:.2f} s, "
                "each tile labelled with its moment (m:ss.xx)."
            ),
        )
        head = {"sheet_file_id": str(sheet_row.id), "frames": frames, "duration_seconds": round(sweep.duration, 3)}
        late = " A moment past its end gave its last frame (asked_seconds says which)." if any("asked_seconds" in f for f in frames) else ""
        kept = (
            "every frame is a picture of this conversation at full size (its file_id above), which "
            "read_file_content opens."
            if keep
            else "the frames themselves are not kept as files (keep_frames true keeps each one at full size; "
            "save_video_frame takes one moment)."
        )
        resolved = clip_providers.resolved_prefix_note(recording_id or clip_id or file_id, source)
        return _result(
            f"{json.dumps(head)}\n{resolved}Saved {len(frames)} frames of the {what}{cut}, {first:.2f} s to {last:.2f} s, in "
            f"{time.monotonic() - started:.1f} s: the contact sheet `{sheet_row.id}` ([[FILE:{sheet_row.id}]]) is attached, "
            f"each tile labelled with its moment; {kept}{late}",
            files=[FileRef.from_attachment(sheet_row)],
        )

    async def _insert_sweep(
        video_id: str = "",
        render_id: str | None = None,
        scene: str | None = None,
        step_seconds: Any = None,
        **_ignored: Any,
    ) -> ToolResult:
        """Every screen insert of a render looked at whole (dev:video-render-wide-insert-gate): one contact sheet per
        scene, the region at 100 % every *step_seconds*, and the table of what each tile's frame measured."""
        import tempfile
        from pathlib import Path

        from app.llm.base import FileRef
        from app.services.builtin_tools._shared import _persist_generated_file
        from app.services.video_studio import assets, encoder, insert_sweep
        from app.services.video_studio.projects import load_visible_project
        from app.services.video_studio.render_checks import findings_of, insert_reports

        step = insert_sweep.step_of(step_seconds)
        async with async_session() as session:
            user = await _user(session, user_id)
            project = await load_visible_project(session, video_id, user) if user else None
            if project is None:
                return _result(f"No video `{video_id}` you can open.", error=True)
            if render_id:
                rid = safe_uuid(str(render_id))
                render = await session.get(VideoRender, rid) if rid else None
                if render is None or render.project_id != project.id:
                    return _result(f"No render `{render_id}` of this video.", error=True)
            else:
                render = (
                    await session.execute(
                        select(VideoRender)
                        .where(VideoRender.project_id == project.id, VideoRender.status == JOB_READY, VideoRender.scene_id.is_(None))
                        .order_by(VideoRender.created_at.desc())
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if render is None:
                    return _result("This video has no finished render yet: render_video first, or name a scene preview's render_id.", error=True)
        from app.models.video_studio import JOB_FAILED

        # A failed render keeps its MP4 to look at (render_line): its inserts are swept as a ready one's are.
        if render.status not in (JOB_READY, JOB_FAILED) or not render.file_id:
            said = (
                "no MP4 was kept of it: render the video again"
                if render.status == JOB_FAILED
                else "its MP4 is swept once it is ready (a failed render's kept MP4 is swept too)"
            )
            return _result(f"Render `{render.id}` is {render.status}: {said}.", error=True)
        reports = insert_reports(render.checks)
        if not reports:
            return _result(f"Render `{render.id}` draws no screen insert: nothing to sweep.", error=True)
        fps = float(render.fps or 30)
        with tempfile.TemporaryDirectory(prefix="xtm-sweep-") as work:
            video = str(Path(work) / "render.mp4")
            if await assets.download_file(render.file_id, video) is None:
                return _result(f"The MP4 of render `{render.id}` is gone: render the video again.", error=True)
            frames_dir = Path(work) / "frames"
            frames_dir.mkdir()

            async def read(frames: list[int]) -> dict[int, str]:
                return await encoder.extract_frames(video, frames, fps=fps, out_dir=str(frames_dir))

            try:
                found, skipped = await insert_sweep.sheets(
                    reports,
                    read,
                    rate=fps,
                    step=step,
                    out_dir=work,
                    scene=str(scene) if scene not in (None, "") else None,
                    findings=findings_of(render.checks),
                )
            except encoder.EncoderError as exc:
                return _result(f"The frames of render `{render.id}` could not be read: {exc}", error=True)
            if not found:
                said = "; ".join(skipped) or "no insert of that scene in this render"
                return _result(f"Nothing to sweep in render `{render.id}`: {said}.", error=True)
            rows = []
            refs = []
            for sheet in found:
                row = await _persist_generated_file(
                    user_id=user_id,
                    filename=f"insert sweep - {sheet.scene_id or 'video'} - render {str(render.id)[:8]}.png",
                    raw_bytes=Path(sheet.path).read_bytes(),
                    content_type="image/png",
                    conversation_id=str(conversation_uuid) if conversation_uuid else None,
                    assignment_run_id=None,
                    source=_SHOWN_FILE_SOURCE,
                    description=f"The screen inserts of scene {sheet.scene_id} swept every {step:g} s at 100%, kept as a picture.",
                )
                refs.append(FileRef.from_attachment(row))
                rows.append({**sheet.as_dict(), "file_id": str(row.id)})
        outcome = {"render_id": str(render.id), "step_seconds": step, "sheets": rows, **({"skipped": skipped} if skipped else {})}
        named = ", ".join(f"scene {r['scene_id']}: [[FILE:{r['file_id']}]]" for r in rows)
        return _result(
            f"{json.dumps(outcome)}\nSwept {sum(len(r['inserts']) for r in rows)} screen insert(s) of render `{render.id}` "
            f"in {len(rows)} contact sheet(s), one per scene ({named}): each tile is the insert's region at 100% of the "
            "MP4's pixels, its time and frame under it, a frame failing a gate on its own framed in red with the gates "
            "named. Look at each sheet before calling the inserts right; save_video_frame with the region box cuts any "
            "one moment again.",
            files=refs,
        )

    async def _compose_picture(base_file_id: str = "", layers: Any = None, name: str | None = None, **_ignored: Any) -> ToolResult:
        from app.llm.base import FileRef
        from app.services.builtin_tools._shared import _persist_generated_file
        from app.services.video_studio import pictures

        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Pictures are available to signed-in users only.", error=True)
            try:
                png, (width, height) = await pictures.compose_files(session, user, base_file_id, _json(layers))
            except pictures.PictureError as exc:
                return _result(str(exc), error=True)
        row = await _persist_generated_file(
            user_id=user_id,
            filename=f"{(name or 'composed picture').strip()[:80]}.png",
            raw_bytes=png,
            content_type="image/png",
            conversation_id=str(conversation_uuid) if conversation_uuid else None,
            assignment_run_id=None,
            source=_SHOWN_FILE_SOURCE,
        )
        shown_this_turn.add(str(row.id))
        return _result(
            f"Composed the picture `{row.id}` ([[FILE:{row.id}]]), {width}x{height}: look at it, and move a layer "
            "(its quad's corners) if it does not sit right. Use it as an image scene "
            f'{{"type": "image", "file_id": "{row.id}"}}, a statement backdrop, the start picture of an AI shot '
            "(reference_file_id) or one of its reference images.",
            files=[FileRef.from_attachment(row)],
        )

    async def _preview_start_frame(
        reference_file_id: str | None = None,
        reference_recording_id: str | None = None,
        reference_clip_id: str | None = None,
        reference_at_seconds: float | None = None,
        reference_at_step: int | None = None,
        aspect_ratio: str | None = None,
        crop: str | None = None,
        **_ignored: Any,
    ) -> ToolResult:
        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Available to signed-in users only.", error=True)
            try:
                plan = await clip_providers.plan_start_frame_preview(
                    session,
                    user,
                    file_id=reference_file_id,
                    recording_id=reference_recording_id,
                    clip_id=reference_clip_id,
                    at_seconds=reference_at_seconds,
                    at_step=reference_at_step,
                    aspect_ratio=aspect_ratio or "16:9",
                    crop=crop,
                )
            except clip_providers.ClipError as exc:
                return _result(str(exc), error=True)
            owner = user.id
        try:
            preview = await clip_providers.preview_start_frame(plan, user_id=owner, conversation_id=conversation_uuid)
        except clip_providers.ClipError as exc:
            return _result(str(exc), error=True)
        shown = await _shown([preview.file_id])
        return _result(
            f"{json.dumps(preview.as_dict())}\n{preview.note} [[FILE:{shown.ids[0] if shown.ids else preview.file_id}]]",
            files=shown,
        )

    async def _music_offer(session, user) -> dict:
        """The built-in tracks (always there) and the music models *user* may compose with, the default first."""
        from app.llm.router import get_music_provider, list_music_models_for
        from app.services.video_studio import music as music_tracks

        models = await list_music_models_for(session, user_id=user.id)
        offer: dict = {"music_library": music_tracks.library(), "music_models": models, "ai_music": {"available": True}}
        try:
            _p, default_model, default_config = await get_music_provider(session, user_id=user.id)
            default_key = (str(default_config.id), default_model)
        except ValueError as exc:
            default_key = None
            if not models:
                offer["ai_music"] = {"available": False, "reason": str(exc)}
        for entry in models:
            entry["default"] = (entry["provider_id"], entry["model"]) == default_key
        models.sort(key=lambda entry: not entry["default"])
        return offer

    async def _generate_music(
        prompt: str = "",
        video_id: str | None = None,
        duration_seconds: float | None = None,
        model: str | None = None,
        provider_id: str | None = None,
        instrumental: bool | None = None,
        negative_prompt: str | None = None,
        name: str | None = None,
        music_id: str | None = None,
        refit: bool | None = None,
        margin_seconds: Any = None,
        extend: bool | None = None,
        pad: bool | None = None,
        **_ignored: Any,
    ) -> ToolResult:
        from app.models.video_studio import VideoMusic
        from app.services.audit import log_audit
        from app.services.video_studio import jobs
        from app.services.video_studio import music as music_tracks
        from app.services.video_studio.projects import (
            can_manage_project,
            load_visible_project,
            planned_seconds,
            render_music_line,
            rendered_seconds,
            save_spec,
        )
        from app.services.video_studio.spec import VideoSpecError

        if refit and not video_id:
            return _result("refit composes a video's AI track again at its length: give video_id.", error=True)
        if extend and not refit:
            return _result(
                "extend keeps a video's take and composes its tail again to the video's length: it goes with "
                "refit: true and video_id.",
                error=True,
            )
        if pad and (not refit or extend):
            return _result(
                "pad keeps a video's take and holds its last chord to the video's length with no model call: it goes "
                "with refit: true and video_id, without extend.",
                error=True,
            )
        if not refit and not music_id and not (prompt or "").strip():
            return _result(
                "A track needs a prompt describing it: genre, mood, instruments, tempo, how it evolves.", error=True
            )
        try:
            margin = music_tracks.fit_margin(margin_seconds)
        except music_tracks.MusicError as exc:
            return _result(str(exc), error=True)
        said: list[str] = []
        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Music is available to signed-in users only.", error=True)
            project = None
            if video_id:
                project = await load_visible_project(session, video_id, user)
                if project is None or not await can_manage_project(session, project, user):
                    return _result(f"No video `{video_id}` you can edit.", error=True)
            try:
                if refit:
                    # The video's own AI track, composed again in place at its length now.
                    playing = dict((project.spec or {}).get("music") or {})
                    mid = safe_uuid(str(playing.get("music_id") or ""))
                    row = await session.get(VideoMusic, mid) if mid else None
                    if row is None or row.user_id != user.id:
                        return _result(
                            f"Video `{video_id}` plays no AI track of yours: refit composes the video's AI track "
                            "again at its length (generate_music with video_id and a prompt composes one).",
                            error=True,
                        )
                    planned_now = await planned_seconds(session, project, user)
                    rendered_now = await rendered_seconds(session, project)
                    take = row.duration_ms / 1000 if row.duration_ms else None
                    own = (row.model, row.provider)
                    if pad:
                        # Held to the video's length with no model call: its fade ends on the last frame.
                        seconds = music_tracks.pad_seconds(planned_now, rendered_now, margin_seconds)
                        label = await music_tracks.restart_music(
                            session, user, row, owed_to=conversation_uuid, refit=True, pad=True, duration_seconds=seconds
                        )
                        said.append(
                            f"Padding the take: kept as it is, its last chord held from {take or 0:.1f} s to {seconds} s "
                            "and faded out - no model call, no credits; the take plays again if the pad is not made."
                        )
                    else:
                        # Never shorter than the video plus the margin: the track's own fade lands after the outro,
                        # the video as long as its current render when that outran the plan.
                        seconds, fit_line = music_tracks.refit_seconds(
                            planned_now, duration_seconds, margin, rendered=rendered_now
                        )
                        said.append(fit_line)
                        label = await music_tracks.restart_music(
                            session,
                            user,
                            row,
                            owed_to=conversation_uuid,
                            refit=True,
                            extend=bool(extend),
                            prompt=prompt or None,
                            model=model,
                            provider_id=provider_id,
                            duration_seconds=seconds,
                            instrumental=instrumental,
                            negative_prompt=negative_prompt,
                        )
                    if extend and take is not None:
                        # The take approved stays: only its ending is composed again, after a clip of it.
                        cut = float((row.params or {}).get(music_tracks.EXTENDED_FROM, {}).get("cut", 0.0))
                        clip = min(cut, music_tracks.EXTEND_REFERENCE_SECONDS)
                        said.append(
                            f"Extending the take: its first {cut:.0f} s are kept as they are, its last {take - cut:.0f} s "
                            f"composed again as the continuation of the {clip:.0f} s before them, to {seconds} s, with "
                            f"{row.model} on {label}; the take plays again if the extension is not made."
                        )
                        if (row.model, row.provider) != own:
                            # Another model, another provider's account: said, so the person can refuse it.
                            said.append(
                                f"The take's own {own[0]} does not continue a clip: the extension is composed with "
                                f"{row.model} on {label}, paid from that account. To keep {own[0]}, cancel it "
                                f"(cancel_video_job(music_ids=['{row.id}']) - the take plays as it was) and refit "
                                "without extend."
                            )
                elif music_id:
                    mid = safe_uuid(str(music_id))
                    row = await session.get(VideoMusic, mid) if mid else None
                    if row is None or row.user_id != user.id:
                        return _result(f"No AI track `{music_id}` of yours to generate again.", error=True)
                    label = await music_tracks.restart_music(
                        session,
                        user,
                        row,
                        owed_to=conversation_uuid,
                        prompt=prompt or None,
                        model=model,
                        provider_id=provider_id,
                        duration_seconds=duration_seconds,
                        instrumental=instrumental,
                        negative_prompt=negative_prompt,
                    )
                else:
                    seconds = duration_seconds or (
                        music_tracks.fitted_seconds(
                            music_tracks.video_seconds(
                                await planned_seconds(session, project, user), await rendered_seconds(session, project)
                            ),
                            margin,
                        )
                        if project is not None
                        else 60
                    )
                    row, label = await music_tracks.start_music(
                        session,
                        user,
                        prompt=prompt,
                        duration_seconds=seconds,
                        model=model,
                        provider_id=provider_id,
                        instrumental=True if instrumental is None else bool(instrumental),
                        negative_prompt=negative_prompt,
                        conversation_id=conversation_uuid,
                        name=name,
                    )
            except music_tracks.MusicError as exc:
                return _result(f"{exc} (video_studio_capabilities lists the music models you can use).", error=True)
            if project is not None and refit:
                await log_audit(
                    session,
                    user=user,
                    action="update",
                    entity_type="video_project",
                    entity_id=str(project.id),
                    entity_name=project.title,
                    summary=f"Composed the music of video {project.title} again at its length",
                    details={
                        "music_id": str(row.id),
                        "model": row.model,
                        "refit": True,
                        "extend": bool(extend),
                        "pad": bool(pad),
                        "via": "agent",
                    },
                )
                # The video keeps its revision: only the render's own record says it played the earlier take.
                if line := await render_music_line(session, project, user):
                    said.append(line)
            elif project is not None:
                spec = dict(project.spec or {})
                levels = {
                    k: v
                    for k, v in dict(spec.get("music") or {}).items()
                    if k in ("volume", "ducking", "duck_db", "start", "loop", "ending", "fade_in", "fade_out")
                }
                spec["music"] = {**levels, "music_id": str(row.id)}
                try:
                    await save_spec(
                        session, user=user, raw=spec, project=project, conversation_id=conversation_uuid, via="agent"
                    )
                except VideoSpecError as exc:
                    return _result(f"The video did not take the track: {exc}", error=True)
                await log_audit(
                    session,
                    user=user,
                    action="update",
                    entity_type="video_project",
                    entity_id=str(project.id),
                    entity_name=project.title,
                    summary=f"Composed the music of video {project.title}",
                    details={"music_id": str(row.id), "model": row.model, "via": "agent"},
                )
            chosen_model, seconds_asked = row.model, (row.params or {}).get("duration_seconds")
            await session.commit()
            track_id = str(row.id)
            saved = (str(project.id), project.revision) if project is not None else None
        if saved is not None:
            await emit_tool_event(VIDEO_SAVED_EVENT, video_id=saved[0], revision=saved[1])
        await jobs.wake()
        rows = await jobs.wait_for("music", [track_id], timeout=_wait_budget(200.0), told_to=conversation_uuid)
        return _music_result(rows[0] if rows else None, track_id, chosen_model, label, seconds_asked, saved, said)

    def _music_result(
        row: Any,
        track_id: str,
        model: str,
        label: str,
        seconds: Any,
        saved: tuple | None,
        said: list[str] | None = None,
    ) -> ToolResult:
        placed = (
            f" It is the music of video `{saved[0]}` (revision {saved[1]})."
            if saved
            else f' Use it in a video as music: {{"music_id": "{track_id}"}}.'
        )
        placed += "".join(f" {line}" for line in said or [])
        if row is None or row.status not in JOB_TERMINAL_STATUSES:
            return _result(
                f"AI track `{track_id}` is being composed ({model} on {label}, {seconds} s).{placed} The preview "
                f"plays it once it is ready; call get_video_jobs(music_ids=['{track_id}']) before rendering."
                f"{resumed_note}"
            )
        if row.status != JOB_READY:
            return _result(_failed_music_text(row), error=True, outcome=True)
        params = row.params or {}
        from app.services.video_studio.music import EXTEND_FAILED

        if unmade := params.get(EXTEND_FAILED):
            # Ready on the take it kept: the extension itself failed, said by this call.
            return _result(
                f"AI track `{track_id}`: its extension was not made ({unmade}). The take approved still plays, as it "
                f"was: {_fmt_seconds(row.duration_ms)}.{placed} Tell the user why; refit without extend composes the "
                "whole track again at the video's length.",
                error=True,
                outcome=True,
            )
        notes = " ".join(params.get("notes") or [])
        described = f"\nWhat the model made: {str(params['description'])[:600]}" if params.get("description") else ""
        return _result(
            f"AI track `{track_id}` is ready: {_fmt_seconds(row.duration_ms)} ({row.model}).{placed}"
            + (f" {notes}" if notes else "")
            + described
        )

    async def _speech_offer(session, user, provider_id: str | None, model: str | None) -> dict:
        """The speech models *user* may use, the default marked, and the voices of the default (or the named) one."""
        from app.llm.router import get_speech_provider, list_speech_models_for

        models = await list_speech_models_for(session, user_id=user.id)
        offer: dict = {"speech_models": models, "voices": [], "default_voice": None}
        try:
            default = await get_speech_provider(session, user_id=user.id)
            default_key = (str(default.config.id), default.model)
            offer["default_voice"] = default.voice
        except ValueError as exc:
            default_key = None
            offer["speech_note"] = str(exc)
        for entry in models:
            entry["default"] = (entry["provider_id"], entry["model"]) == default_key
        models.sort(key=lambda entry: not entry["default"])
        if provider_id and safe_uuid(str(provider_id)) is None:
            offer["voices_note"] = "speech_provider_id must be an id from speech_models."
            return offer
        try:
            target = await get_speech_provider(session, user_id=user.id, provider_id=provider_id, model=model or None)
            voices = await asyncio.wait_for(target.provider.list_voices(target.model), timeout=20)
        except Exception as exc:
            offer["voices_note"] = f"No voice listed: {format_exception_message(exc)[:200]}"
            return offer
        offer["voices_of"] = {"provider_id": str(target.config.id), "model": target.model}
        offer["voices"] = [v.as_dict() for v in voices[:_VOICES_SHOWN]]
        if len(voices) > _VOICES_SHOWN:
            offer["voices_note"] = f"The first {_VOICES_SHOWN} of {len(voices)} voices; the account's own voices come first."
        elif not voices:
            offer["voices_note"] = "This model lists no voice: name the voice its provider expects."
        return offer

    async def _preview_voice(
        text: str = "",
        voice: str | None = None,
        provider_id: str | None = None,
        model: str | None = None,
        style: str | None = None,
        speed: float = 1.0,
        provider: str = "auto",
        pronunciations: Any = None,
        transcript_only: Any = False,
        **_ignored: Any,
    ) -> ToolResult:
        import tempfile
        from pathlib import Path

        from app.llm.base import FileRef
        from app.services.video_studio import assets
        from app.services.video_studio.spec import Voiceover

        if not (text or "").strip():
            return _result("Give the text to speak.", error=True)
        try:
            wanted = Voiceover(
                provider=provider or "auto",
                provider_id=provider_id,
                model=model,
                voice=voice,
                pronunciations=_json(pronunciations) or [],
            )
        except ValueError as exc:
            return _result(f"Invalid voice settings: {format_exception_message(exc)[:300]}", error=True)
        from app.services.video_studio import lexicon_store

        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Available to signed-in users only.", error=True)
            try:
                target = await voice_providers.resolve(session, user.id, wanted)
            except voice_providers.VoiceUnavailableError as exc:
                return _result(str(exc), error=True)
            # The platform's lexicon under the entries given here, as a video would read them.
            lexicon = await lexicon_store.for_voiceover(session, wanted)
            transcriber, unheard = await _transcriber(session, user.id)
        spoken_text = text[:1500]
        rendition = voice_providers.rendition_of(
            target, voice_providers.Line("preview", spoken_text, None, None), wanted, lexicon
        )
        with tempfile.TemporaryDirectory(prefix="xtm-voice-") as work:
            path = str(Path(work) / "voice.wav")
            try:
                chosen, notes = await voice_providers.synthesize(
                    target,
                    text=spoken_text,
                    voice=voice,
                    style=style,
                    speed=float(speed or 1.0),
                    out_path=path,
                    lexicon=lexicon,
                )
                from app.services.video_studio import encoder

                info = await encoder.probe(path)
            except Exception as exc:
                return _result(f"The voice-over failed: {format_exception_message(exc)}", error=True)
            readings, checked = await _heard_back(spoken_text, path, lexicon, transcriber, unheard)
            row = (
                None
                if transcript_only is True or str(transcript_only).strip().lower() == "true"
                else await assets.store_path(
                    path,
                    filename="voiceover-preview.wav",
                    user_id=user.id,
                    source=assets.SOURCE_VOICE,
                    content_type="audio/wav",
                    conversation_id=conversation_uuid,
                )
            )
        label = getattr(target.config, "display_name", None) or target.config.provider
        # Only what the call named goes into the video: the rest keeps following the platform's choice.
        fields: dict[str, str] = {"voice": chosen or voice} if voice else {}
        follows = " provider_id and model stay unset: the video follows the platform's speech model."
        if voice_providers.names_its_own(wanted):
            fields = {"provider_id": str(target.config.id), "model": target.model, **fields}
            follows = ""
        advice = (
            f"To narrate the video with it, set its voiceover {json.dumps(fields)} (with the same style and speed).{follows}"
            if fields
            else "To narrate the video with it, leave its voiceover provider_id, model and voice unset (the platform's "
            "speech model and voice), with the same style and speed."
        )
        said = ""
        if rendition.terms:
            respelled = "".join(f"\n{note}" for note in rendition.notes(target.model))
            said = (
                f"\nLexicon terms said with {rendition.mechanism}"
                f"{f' (platform lexicon v{lexicon.version})' if lexicon.version else ''}; sent: "
                f"{json.dumps(rendition.text[:600], ensure_ascii=False)}{respelled}\n{readings}"
            )
        audio = f"[[FILE:{row.id}]]" if row is not None else "the audio not attached (transcript_only)"
        return _result(
            f"Voice-over preview ({label}, {target.model}, voice {chosen or 'default'}, {info.duration:.1f} s): "
            f"{audio}. " + "".join(f"{note} " for note in notes) + advice + said + f"\n{checked}",
            files=[FileRef.from_attachment(row)] if row is not None else None,
        )

    async def _transcriber(session: Any, uid: Any) -> tuple[Any, str | None]:
        """What hears a preview back, or why nothing does."""
        from app.llm.transcription import resolve_transcriber

        try:
            return await resolve_transcriber(session, uid), None
        except Exception as exc:
            return None, format_exception_message(exc)[:200]

    async def _heard_back(text: str, path: str, lexicon: Any, transcriber: Any, unheard: str | None) -> tuple[str, str]:
        """*text* as the voice said it into *path*, heard back: ``(how each lexicon term was heard, the transcript
        check)``. The check is the very one ``render_video`` holds each narration to (``voice_check.compare``: the same
        speech-to-text, normalisation and lexicon), so a line that passes here passes there at the same voice and speed.
        A term heard is the speech-to-text's words where the text has it, a pause inside one rejecting it
        #(6098)."""
        from app.services.video_studio import voice_check

        has_terms = bool(lexicon.voice().find(text))
        if transcriber is None:
            why = unheard or "no speech-to-text"
            return (f"The terms were not heard back: {why}." if has_terms else ""), f"Not heard back: {why}."
        try:
            # The one transcript check of a narration: the function render_video's voice check runs on each take.
            check, _heard = await voice_check.hear_back(transcriber, text, path, lexicon)
        except Exception as exc:
            why = format_exception_message(exc)[:200]
            return (f"The terms were not heard back ({why})." if has_terms else ""), f"Not heard back ({why})."
        return (_term_lines(check.terms) if has_terms else ""), _transcript_check(check)

    def _transcript_check(check: Any) -> str:
        """The transcript check of a preview, as ``render_video``'s voice check reads it: the share of words heard as
        written, the verdict, and the narration word by word with each word heard otherwise."""
        from app.services.video_studio import pronunciation_check, voice_check

        verdict = "it passes" if check.passed else f"it FAILS: {check.reason()}"
        lines = [
            f"Transcript check (render_video's voice check: same speech-to-text, normalisation and lexicon): "
            f"{check.heard_share:.0%} of its words heard as written (passes from {1 - voice_check.MAX_WER:.0%}, "
            f"word error rate {check.wer:.0%}); {verdict}.",
            f"Heard: {json.dumps(check.transcript[:1500], ensure_ascii=False)}",
        ]
        if check.otherwise():
            diff = pronunciation_check.diff_line(check.words)
            lines.append(f"Word by word, each word heard otherwise in brackets: {diff}")
        else:
            lines.append("Every word heard as written.")
        lines.append(
            "What a render adds: it speaks each narration at the video's speed with the narrations around it as "
            "context, so its take is another one than this preview's; render_video(recheck: true) speaks a narration "
            "playing a fallback take again and hears it with this very check."
        )
        return "\n".join(lines)

    def _term_lines(terms: Any) -> str:
        """How the speech-to-text heard each lexicon term of a preview."""
        lines = [
            f"- {t.term}: heard {json.dumps(t.heard, ensure_ascii=False) if t.heard else 'nothing'}"
            + (
                f" (accepted, {t.reading}{'; WARNING: not heard as written, listen to it' if t.heard_otherwise else ''})"
                if t.passed
                else f" (REJECTED: {t.message()})"
            )
            for t in terms
        ]
        return "Heard back by the speech-to-text:\n" + "\n".join(lines)

    # ── Production ────────────────────────────────────────────────────

    async def _bezel_frames(measured: list[Any]) -> list[Any]:
        """The footage frames a refused save's screen inserts were measured on, as pictures of this conversation."""
        from app.llm.base import FileRef
        from app.services.builtin_tools._shared import _persist_generated_file
        from app.services.video_studio import insert_bezels

        files: list[Any] = []
        for item in measured[:4]:
            png = await insert_bezels.frame_png(item)
            if png is None:
                continue
            row = await _persist_generated_file(
                user_id=user_id,
                filename=f"scene {item.scene_id} - screen insert {item.index} - footage at {item.seconds:.2f} s.png",
                raw_bytes=png,
                content_type="image/png",
                conversation_id=str(conversation_uuid) if conversation_uuid else None,
                assignment_run_id=None,
                source=_SHOWN_FILE_SOURCE,
                description=f"Scene {item.scene_id}: the footage frame {item.label} comes in on ({item.where}).",
            )
            files.append(FileRef.from_attachment(row))
        return files

    async def _create_or_edit(video_id: str | None, spec: Any, operations: Any) -> ToolResult:
        from app.services.audit import log_audit
        from app.services.video_studio.projects import (
            apply_operations,
            can_manage_project,
            load_visible_project,
            queue_figure_stills,
            restored_revisions,
            save_spec,
            silent_clip_scenes,
            transitions_line,
        )
        from app.services.video_studio.revisions import RevisionNotFoundError, spec_at
        from app.services.video_studio.spec import VideoSpecError, parse_video_spec, sound_summary, spec_summary

        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Videos are available to signed-in users only.", error=True)
            project = None
            if video_id:
                project = await load_visible_project(session, video_id, user)
                if project is None or not await can_manage_project(session, project, user):
                    return _result(f"No video `{video_id}` you can edit.", error=True)
            raw = _json(spec)
            changes: list[str] = []
            try:
                if operations is not None:
                    if project is None:
                        return _result("operations edit an existing video: give video_id.", error=True)
                    ops = _json(operations)
                    earlier: dict[int, dict] = {}
                    for asked in sorted(restored_revisions(ops)):
                        try:
                            earlier[asked] = await spec_at(session, project.id, asked, current=project.revision)
                        except RevisionNotFoundError as exc:
                            return _result(str(exc), error=True)
                    raw, changes = apply_operations(dict(project.spec or {}), ops, earlier)
                elif raw is None:
                    return _result("Give the video spec (or operations to edit one).", error=True)
                result = await save_spec(
                    session,
                    user=user,
                    raw=raw,
                    project=project,
                    conversation_id=conversation_uuid,
                    via="agent",
                    summary=changes,
                )
            except VideoSpecError as exc:
                frames = await _bezel_frames(getattr(exc, "measured", None) or [])
                shown = (
                    " The footage frame each screen insert named comes in on is attached: the picture the bezel was "
                    "measured on."
                    if frames
                    else ""
                )
                return _result(str(exc) + f"\nFix the fields named and call again.{shown}", error=True, files=frames)
            saved = result.project
            if result.unchanged:
                # Nothing was written: no revision, no audit entry, no preview out of date.
                change_text = ("\nChanges asked: " + "; ".join(changes)) if changes else ""
                return _result(
                    f"No change: video **{saved.title}** (`{saved.id}`) already reads this way at revision "
                    f"{saved.revision} - the spec this edit gives is byte-identical to the stored one, so nothing was "
                    f"saved, no revision was made and every scene keeps its preview.{change_text}\n"
                    f"Scenes (the indexes operations use):\n{spec_summary(result.authored)}\n"
                    f"Render it with render_video(video_id='{saved.id}'). Include this marker in your reply: "
                    + VIDEO_MARKER.format(id=str(saved.id))
                )
            applied_spec = parse_video_spec(result.applied.spec)
            silent = await silent_clip_scenes(session, applied_spec)
            voice_line = await voice_providers.describe(session, user.id, applied_spec)
            cuts_line = await transitions_line(session, result.applied.spec)
            await log_audit(
                session,
                user=user,
                action="create" if project is None else "update",
                entity_type="video_project",
                entity_id=str(saved.id),
                entity_name=saved.title,
                summary=f"{'Created' if project is None else 'Updated'} video {saved.title}",
                details={
                    "revision": saved.revision,
                    "template_id": str(saved.template_container_id) if saved.template_container_id else None,
                    "via": "agent",
                },
            )
            await session.commit()
            vid = str(saved.id)
            revision = saved.revision
            uid = user.id
        # Handed to the worker before the studio is told: the preview it refreshes polls each figure until drawn.
        still_notes = await queue_figure_stills(result, user)
        await emit_tool_event(VIDEO_SAVED_EVENT, video_id=vid, revision=revision)
        if result.animations or result.music_started:
            from app.services.video_studio import jobs

            await jobs.wake()
        report = await check_video_quality(uuid.UUID(vid), uid, expected_revision=revision)
        # A screen insert's legibility advisory is said once per insert, never again on every save.
        from app.services.video_studio.insert_legibility import said_once
        from app.services.video_studio.quality import MAX_REPORTED_FINDINGS

        report = await said_once(vid, report, shown=MAX_REPORTED_FINDINGS)
        quality_text = format_quality_report(report)
        change_text = ("\nChanges: " + "; ".join(changes)) if changes else ""
        if result.animations:
            change_text += (
                f"\nAnimating {result.animations} image(s) with the video model: each scene plays its still until "
                "its shot is made (get_video_jobs lists them)."
            )
        if result.music_started:
            track = (result.authored.get("music") or {}).get("music_id")
            change_text += (
                f"\nComposing the music with the music model (AI track `{track}`): the preview plays it once it is "
                f"made; call get_video_jobs(music_ids=['{track}']) before rendering."
            )
        if result.notes or still_notes:
            change_text += "\n" + "\n".join([*result.notes, *still_notes])
        render_text = (
            f"Render it with render_video(video_id='{vid}') once the quality check is clean."
            if report.findings
            else f"Render it with render_video(video_id='{vid}')."
        )
        return _result(
            f"{'Saved' if video_id else 'Created'} video **{result.authored['title']}** (`{vid}`), revision {revision}, "
            f"about {result.seconds:.0f} s{f' ({result.seconds_basis})' if result.seconds_basis else ''}."
            f"{change_text}\n{_template_text(result.applied)}"
            f"Scenes (the indexes operations use):\n{spec_summary(result.authored)}\n"
            f"{cuts_line}\n"
            f"{sound_summary(applied_spec, silent)}\n"
            + (f"{voice_line}\n" if voice_line else "")
            + (f"{quality_text}\n" if quality_text else "")
            + f"{render_text} Include this marker in your reply: "
            + VIDEO_MARKER.format(id=vid)
        )

    async def _create_video(spec: Any = None) -> ToolResult:
        return await _create_or_edit(None, spec, None)

    async def _edit_video(video_id: str = "", spec: Any = None, operations: Any = None) -> ToolResult:
        return await _create_or_edit(video_id, spec, operations)

    async def _get_video(
        video_id: str = "", scenes: Any = None, fields: Any = None, revision: Any = None
    ) -> ToolResult:
        from app.services.presentation_sharing import describe_sharing
        from app.services.video_studio import jobs, public_links, render_retention
        from app.services.video_studio import music as music_tracks
        from app.services.video_studio.projects import (
            effective_spec,
            holding_line,
            load_visible_project,
            measured_plan,
            preview_holds,
            render_music_line,
            rendered_seconds,
            serialize_render,
            silent_clip_scenes,
            transitions_line,
        )
        from app.services.video_studio.spec import VideoSpecError, parse_video_spec, sound_summary

        try:
            parts = video_fields(_json(fields))
        except ValueError as exc:
            return _result(str(exc), error=True)
        async with async_session() as session:
            user = await _user(session, user_id)
            project = await load_visible_project(session, video_id, user) if user else None
            if project is None:
                return _result(f"No video `{video_id}` you can open.", error=True)
            if revision not in (None, "") and str(revision).strip().isdigit() and int(revision) != project.revision:
                return await _video_at_revision(session, project, int(revision), scenes)
            if revision not in (None, "") and not str(revision).strip().isdigit():
                return _result("revision takes a revision number.", error=True)
            spec_scenes = (project.spec or {}).get("scenes") if isinstance(project.spec, dict) else None
            try:
                selected = scene_selection(_json(scenes), spec_scenes if isinstance(spec_scenes, list) else [])
            except ValueError as exc:
                return _result(str(exc), error=True)
            try:
                applied = await effective_spec(session, project, user)
                applied_spec = parse_video_spec(applied.spec)
                voice_line = await voice_providers.describe(session, user.id, applied_spec)
                # On the length the render places: the voice takes spoken, or the current render's own.
                music_line = await music_tracks.length_note(
                    session,
                    applied_spec.music,
                    music_tracks.video_seconds(
                        (await measured_plan(session, applied.spec, user)).duration, await rendered_seconds(session, project)
                    ),
                )
                played_line = await render_music_line(session, project, user, applied_spec)
                # The scenes whose passing preview still holds: a render needs no new preview of them.
                try:
                    holding = await preview_holds(session, project, applied.spec)
                except Exception as exc:  # said when it can be read; the render gate decides anyway
                    logger.debug("Previews that hold not read for video %s: %s", project.id, exc)
                    holding = []
                holds_line = holding_line(holding) if holding else ""
                template_text = (
                    _template_text(applied)
                    + await transitions_line(session, applied.spec)
                    + "\n"
                    + sound_summary(applied_spec, await silent_clip_scenes(session, applied_spec))
                    + "\n"
                    + (f"{music_line}\n" if music_line else "")
                    + (f"{played_line}\n" if played_line else "")
                    + (f"{voice_line}\n" if voice_line else "")
                    + (f"{holds_line}\n" if holds_line else "")
                )
            except VideoSpecError as exc:
                template_text = f"The template no longer applies cleanly: {exc}\n"
            link = await public_links.load_public_link(session, project.id)
            sharing_text = await describe_sharing(session, project)
            link_line = public_links.public_link_status_line(
                has_link=link is not None, allow_download=bool(link and link.allow_download)
            )
            sharing_text += f"\n{link_line}" if link_line else ""
            renders = list(
                (
                    await session.execute(
                        select(VideoRender)
                        .where(VideoRender.project_id == project.id)
                        .order_by(VideoRender.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            held = render_retention.standing_of(
                project, renders, public_link=link is not None, policy=await render_retention.load_policy(session)
            )
            sizes = await render_retention.measure(session, renders)
        shown = renders[:_RENDERS_LISTED]
        states = await jobs.job_states("render", shown)
        render_lines = (
            "\n".join(
                render_list_line(
                    r, s, current=project.latest_render_id, size=sizes.get(r.id, 0), kept=render_retention.kept_by(r, held)
                )
                for r, s in zip(shown, states, strict=True)
            )
            or "- none yet"
        )
        if len(renders) > len(shown):
            render_lines += f"\n- and {len(renders) - len(shown)} older renders (delete_video_renders with dry_run: true lists them all)"
        if renders:
            render_lines += "\n" + storage_line(renders, sizes, held)
        if any(r.file_id for r in shown):
            render_lines += (
                "\nWith a render's MP4 file, save_video_frame(file_id, at_seconds=t) saves its frame at any moment t as "
                "a picture to look at before calling the video final; save_video_frames(file_id, step_seconds=1, "
                "from_seconds, to_seconds) sweeps a stretch of it as one contact sheet."
            )
        latest = serialize_render(renders[0]) if renders else None
        notes = (
            ("\nNotes of the latest render: " + "; ".join(latest["notes"])) if latest and latest.get("notes") else ""
        )
        if latest and latest.get("captions_file_id"):
            # Its captions: a file the run can read, named.
            captions = await _refs([latest["captions_file_id"]], show=False)
            if captions.ids:
                notes += f"\nCaptions of the latest render: `{captions.ids[0]}` ([[FILE:{captions.ids[0]}]])."
        whole = parts == set(GET_VIDEO_FIELDS)
        quality_text = (
            format_quality_report(await check_video_quality(project.id, user.id, expected_revision=project.revision))
            if "spec" in parts
            else ""
        )
        spec_text = (
            spec_page(project.spec or {}, str(project.id), selected=selected, fields=parts)
            if parts & {"spec", "scenes", "voiceover", "music"}
            else ""
        )
        feedback_text = ""
        if "feedback" in parts:
            from app.services.video_studio.studio_feedback import studio_feedback

            ids = [s.get("id") for s in spec_scenes if isinstance(s, dict)] if isinstance(spec_scenes, list) else []
            picked = (
                {str(ids[i]) for i in selected if i < len(ids) and ids[i]} if scenes not in (None, "", []) else None
            )
            async with async_session() as session:
                feedback_text = await studio_feedback(session, user, project, scenes=picked) + "\n"
        if "revisions" in parts:
            from app.services.video_studio.revisions import list_revisions

            async with async_session() as session:
                lines = await list_revisions(session, project.id)
            feedback_text += (
                "Revisions (newest first; get_video(revision=N) reads one):\n"
                + ("\n".join(line.text() for line in lines) or "- none recorded yet")
                + "\n"
            )
        return _result(
            f"Video **{project.title}** (`{project.id}`), revision {project.revision}, about "
            f"{_fmt_seconds(project.estimated_duration_ms)}.\n"
            + (f"{template_text}{sharing_text}\n" if whole or "spec" in parts else "")
            + spec_text
            + (f"{quality_text}\n" if quality_text else "")
            + (f"Renders ({live_versions_line()}):\n{render_lines}{notes}" if "renders" in parts else "")
            + feedback_text
        )

    async def _video_at_revision(session, project, revision: int, scenes: Any) -> ToolResult:
        """``get_video(revision=N)``: the authored spec, or the scenes picked, as stored at revision N."""
        from app.services.video_studio.revisions import RevisionNotFoundError, spec_at

        try:
            earlier = await spec_at(session, project.id, revision, current=project.revision)
        except RevisionNotFoundError as exc:
            return _result(str(exc), error=True)
        listed = earlier.get("scenes") if isinstance(earlier.get("scenes"), list) else []
        try:
            selected = scene_selection(_json(scenes), listed)
        except ValueError as exc:
            return _result(str(exc), error=True)
        return _result(
            f"Video **{project.title}** (`{project.id}`) at revision {revision} (current: {project.revision}), "
            "read only; edit_video op restore_scene copies a scene back.\n"
            + spec_page(earlier, str(project.id), selected=selected, fields={"spec"})
        )

    async def _list_videos(limit: int = 20) -> ToolResult:
        from app.api.v1.helpers import get_user_group_ids, user_visible_filter

        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Videos are available to signed-in users only.", error=True)
            groups = await get_user_group_ids(session, user.id)
            rows = (
                (
                    await session.execute(
                        select(VideoProject)
                        .where(user_visible_filter(VideoProject, user.id, groups, is_admin=False))
                        .order_by(VideoProject.updated_at.desc())
                        .limit(max(1, min(int(limit or 20), 50)))
                    )
                )
                .scalars()
                .all()
            )
        if not rows:
            return _result("No video yet: create one with create_video.")
        return _result(
            "Videos:\n"
            + "\n".join(
                f"- **{p.title}** (`{p.id}`): {p.scene_count} scenes, about {_fmt_seconds(p.estimated_duration_ms)}"
                for p in rows
            )
        )

    # ── Sharing ───────────────────────────────────────────────
    # AS THE CALLING USER, through the deck's one sharing write
    # (`presentation_sharing.apply_sharing`) and the video's public link
    # (`video_studio.public_links`): only a manager of the video, never a
    # company-managed one, one audit row per change.

    async def _standing_refusal(session, project: VideoProject, user: User, what: str) -> str | None:
        from app.services.video_studio.projects import can_manage_project

        if project.is_company_managed:
            return (
                f'"{project.title}" is published to the whole platform (company-managed): {what} is managed by '
                "admins from the Share dialog, not by this tool. Nothing was changed."
            )
        if not await can_manage_project(session, project, user):
            return (
                f'You can watch "{project.title}" but not share it - only its owner or someone it is shared with '
                "as an editor (can edit) can. Nothing was changed: tell the user to ask the owner."
            )
        return None

    async def _share_video(
        video_id: str = "",
        users: Any = None,
        groups: Any = None,
        permission: str | None = "read_only",
        action: str | None = "add",
    ) -> ToolResult:
        from fastapi import HTTPException

        from app.services.audit import log_audit
        from app.services.presentation_sharing import (
            SHARE_PERMISSIONS,
            apply_sharing,
            audience_line,
            format_sharing_result,
            lock_for_sharing,
            normalize_share_action,
            normalize_share_permission,
            plan_sharing,
        )
        from app.services.share_principals import (
            MAX_PRINCIPALS_PER_CALL,
            SHARE_ACTIONS,
            load_principals,
            principal_entries,
            resolve_principals,
        )
        from app.services.video_studio.projects import load_visible_project

        verb = normalize_share_action(action)
        if verb is None:
            return _result(f"action must be one of {', '.join(SHARE_ACTIONS)}.", error=True)
        tier = normalize_share_permission(permission)
        if tier is None and verb != "remove":
            return _result(f"permission must be one of {', '.join(SHARE_PERMISSIONS)}.", error=True)
        user_entries, group_entries = principal_entries(users), principal_entries(groups)
        if verb != "replace" and not user_entries and not group_entries:
            return _result(f"Name at least one user (email or user id) or group to {verb}.", error=True)
        if len(user_entries) + len(group_entries) > MAX_PRINCIPALS_PER_CALL:
            return _result(
                f"One call names at most {MAX_PRINCIPALS_PER_CALL} people and groups - share with a group instead.",
                error=True,
            )
        try:
            async with async_session() as session:
                user = await _user(session, user_id)
                project = await load_visible_project(session, video_id, user) if user else None
                if project is None:
                    return _result(f"No video `{video_id}` you can open. Nothing was shared.", error=True)
                refusal = await _standing_refusal(session, project, user, "its audience")
                if refusal:
                    return _result(refusal, error=True)
                resolved = await resolve_principals(session, user_entries, group_entries)
                refusal = resolved.refusal(granting=verb != "remove")
                if refusal:
                    return _result(refusal, error=True)
                # Re-read locked: a colleague who changed the sharing meanwhile
                # (or took the caller's own grant away) is what this call plans from.
                await lock_for_sharing(session, project, resolved.users)
                refusal = await _standing_refusal(session, project, user, "its audience")
                if refusal:
                    return _result(refusal, error=True)
                title, vid = project.title, project.id
                index = await load_principals(session, project)
                index.add(resolved.users)
                index.add(resolved.groups)
                plan = plan_sharing(
                    project, action=verb, permission=tier or "read_only", users=resolved.users, groups=resolved.groups
                )
                if not plan.changed:
                    return _result(f"{format_sharing_result(title, plan, index)}\n{audience_line(project, index)}")
                try:
                    await apply_sharing(session, user, project, plan.changes)
                except HTTPException as exc:
                    return _result(f"{str(exc.detail).rstrip('.')}. Nothing was changed.", error=True)
                await log_audit(
                    session,
                    user=user,
                    action="update",
                    entity_type="video_project",
                    entity_id=str(vid),
                    entity_name=title,
                    summary=f"Changed who can open video {title} through an agent",
                    details={**plan.audit_details(), "sharing_changed": True},
                )
                audience = audience_line(project, index)
                await session.commit()
        except Exception as exc:
            logger.error("share_video failed: %s", format_exception_message(exc), exc_info=True)
            return _result("Failed to change who can open the video. Nothing was changed.", error=True)
        return _result(
            f"{format_sharing_result(title, plan, index)}\n{audience}\nThis applies now. Those with access find it "
            f"under Videos, or at /videos/{vid}. To show it in the chat: {VIDEO_MARKER.format(id=vid)}"
        )

    async def _set_public_link(video_id: str = "", action: str | None = "enable", download: Any = None) -> ToolResult:
        from app.services.audit import log_audit
        from app.services.presentation_public_links import (
            OUTCOME_DISABLED,
            OUTCOME_KEPT,
            PUBLIC_LINK_ACTIONS,
            normalize_download_flag,
            normalize_public_link_action,
        )
        from app.services.video_studio import public_links
        from app.services.video_studio.projects import load_visible_project

        verb = normalize_public_link_action(action)
        if verb is None:
            return _result(f"action must be one of {', '.join(PUBLIC_LINK_ACTIONS)}.", error=True)
        try:
            wanted = None if verb == "disable" else normalize_download_flag(download, field="download")
        except ValueError as exc:
            return _result(f"{exc}. Nothing was changed.", error=True)
        if verb != "disable" and not public_links.public_links_enabled():
            return _result(f"{public_links.PUBLIC_LINKS_OFF_DETAIL} Nothing was changed: tell the user.", error=True)
        try:
            async with async_session() as session:
                user = await _user(session, user_id)
                project = await load_visible_project(session, video_id, user) if user else None
                if project is None:
                    return _result(f"No video `{video_id}` you can open. Nothing was changed.", error=True)
                refusal = await _standing_refusal(session, project, user, "its public link")
                if refusal:
                    return _result(refusal, error=True)
                title = project.title
                token: str | None = None
                allow, download_changed = False, False
                if verb == "disable":
                    if not await public_links.disable_public_link(session, project.id):
                        return _result(
                            f"**{title}** has no public link: nothing to turn off. Only the people it is shared "
                            "with on the platform can open it."
                        )
                    outcome = OUTCOME_DISABLED
                else:
                    change = await public_links.enable_public_link(
                        session, project.id, actor_id=user.id, regenerate=verb == "regenerate"
                    )
                    outcome, token, allow = change.outcome, change.token, change.allow_download
                    if wanted is not None:
                        download_changed = bool(await public_links.set_download(session, project.id, wanted))
                        allow = wanted
                rendered = await public_links.current_render(session, project) is not None
                changes = [outcome] if outcome != OUTCOME_KEPT else []
                if download_changed:
                    changes.append(public_links.OUTCOME_DOWNLOAD_ON if allow else public_links.OUTCOME_DOWNLOAD_OFF)
                for change_outcome in changes:
                    await log_audit(
                        session,
                        user=user,
                        action="update",
                        entity_type="video_project",
                        entity_id=str(project.id),
                        entity_name=title,
                        summary=f"{public_links.audit_summary(change_outcome, title)} through an agent",
                        details=public_links.audit_details(change_outcome, via_agent=True),
                    )
                await session.commit()
        except Exception as exc:
            logger.error("set_video_public_link failed: %s", format_exception_message(exc), exc_info=True)
            return _result("Failed to change the public link of the video. Nothing was changed.", error=True)
        return _result(
            public_links.format_public_link_result(
                title, outcome, token, rendered=rendered, allow_download=allow, download_changed=download_changed
            )
        )

    # ── Cleaning up renders ───────────────────────────────────
    # AS THE CALLING USER: `render_cleanup` plans and applies, the one
    # deletion of renders (`render_retention.delete`) removes them, one audit
    # row per call that deleted something.

    async def _delete_renders(
        video_id: str = "",
        render_ids: Any = None,
        select: Any = None,
        keep_quality: Any = None,
        before: Any = None,
        dry_run: Any = False,
        confirm: Any = False,
    ) -> ToolResult:
        from app.services.audit import log_audit
        from app.services.video_studio import render_cleanup, render_retention
        from app.services.video_studio.projects import load_visible_project

        try:
            ask = render_cleanup.Ask.parse(
                render_ids=render_ids,
                select=select,
                keep_quality=keep_quality,
                before=before,
                dry_run=dry_run,
                confirm=confirm,
            )
        except render_cleanup.CleanupError as exc:
            return _result(f"{exc} Nothing was deleted.", error=True)
        try:
            async with async_session() as session:
                user = await _user(session, user_id)
                project = await load_visible_project(session, video_id, user) if user else None
                if project is None:
                    return _result(f"No video `{video_id}` you can open. Nothing was deleted.", error=True)
                try:
                    outcome = await render_cleanup.run(session, project, user, ask)
                except render_cleanup.CleanupError as exc:
                    return _result(f"{exc} Nothing was deleted.", error=True)
                if outcome.deleted:
                    await log_audit(
                        session,
                        user=user,
                        action="delete",
                        entity_type="video_project",
                        entity_id=str(project.id),
                        entity_name=project.title,
                        summary=f"{render_cleanup.audit_summary(outcome)} through an agent",
                        details=render_cleanup.audit_details(outcome, via_agent=True),
                    )
                await session.commit()
        except Exception as exc:
            logger.error("delete_video_renders failed: %s", format_exception_message(exc), exc_info=True)
            return _result("Failed to delete the renders of the video. Nothing was deleted.", error=True)
        await render_retention.purge(outcome.deletion)
        refused_only = not ask.dry_run and not outcome.deleted and any(
            item.action == render_cleanup.ACTION_REFUSE for item in outcome.plan.items
        )
        return _result(render_cleanup.answer_text(outcome), error=refused_only)

    async def _delete_video(video_id: str = "", confirm: Any = False, **_kw: object) -> ToolResult:
        """Delete a video the user OWNS, for good: the Studio's Delete, through the same service.

        The bar is the owner of a personal video, or an administrator on a company-managed one
        (``deletion.agent_may_delete``): a ``config`` grantee edits a video but never makes it disappear
        for its owner. A video someone else opens (people, groups, the company, a public link) goes only
        with ``confirm``. Recordings and AI shots stay in the library.
        """
        from app.services.video_studio import deletion
        from app.services.video_studio.projects import load_visible_project

        confirmed = confirm is True or str(confirm).strip().lower() == "true"
        try:
            async with async_session() as session:
                user = await _user(session, user_id)
                project = await load_visible_project(session, video_id, user) if user else None
                if project is None:
                    return _result(f"No video `{video_id}` you can open. Nothing was deleted.", error=True)
                if not deletion.agent_may_delete(project, user):
                    return _result(
                        f"Nothing was deleted: **{project.title}** (`{project.id}`) is not yours to delete. Only its "
                        "owner can delete it (an administrator for a company-managed video); ask them, or stop "
                        "using it.",
                        error=True,
                    )
                who = await deletion.audience(session, project)
                if who and not confirmed:
                    return _result(
                        f"Nothing was deleted: **{project.title}** (`{project.id}`) is opened by "
                        f"{', '.join(who)}; deleting it removes it for them too. Ask the user, then call "
                        "delete_video again with confirm: true.",
                        error=True,
                    )
                deleted = await deletion.delete_video(session, project, user, via_agent=True)
                await session.commit()
        except Exception as exc:
            logger.error("delete_video failed: %s", format_exception_message(exc), exc_info=True)
            return _result("Failed to delete the video. Nothing was deleted; please try again.", error=True)
        await deleted.finish()
        audience = f" It no longer opens for {', '.join(who)}." if who else ""
        return _result(
            f"Deleted the video **{deleted.title}** (`{deleted.project_id}`) for good: there is no trash to restore "
            f"it from. {deleted.renders} render(s) and {deleted.previews} scene preview(s) went with it, with their "
            f"files.{audience} Its recordings and AI shots stay in the library. Its card in a conversation now reads "
            "'no longer available'. Do not show it again; tell the user it is deleted."
        )

    def _gate_refused(title: str, exc: Any) -> ToolResult:
        """What ``render_video`` says of a start its quality gate refused."""
        listed = "\n".join(f"- {reason.message}" for reason in exc.reasons)
        worker = any(reason.code == "preflight" for reason in exc.reasons)
        remedy = (
            "Tell the user what the worker needs, and the quality that fits it now if one was offered: theirs "
            "to choose - never render at another quality on your own"
            if worker
            else "Fix them (edit_video, then render_video_scene on each scene changed)"
        )
        return _result(
            f"**{title}** was not rendered: its quality gate refused it.\n{listed}\n{remedy}, or, if the "
            "user accepts the risk, render_video(force: true) renders anyway: the override is recorded on the "
            "render and audited.",
            error=True,
        )

    async def _over_limit(exc: Any) -> ToolResult:
        """What ``render_video`` says of a start past the person's renders in flight."""
        from app.services.video_studio import jobs

        states = await jobs.job_states("render", [render for render, _title in exc.in_flight])
        listed = "\n".join(
            f"- render `{render.id}` of **{title}** (`{render.project_id}`): {state.status}, "
            f"{jobs.describe(state)}"
            for (render, title), state in zip(exc.in_flight, states, strict=True)
        )
        ids = json.dumps([str(render.id) for render, _title in exc.in_flight])
        return _result(
            f"You already have {len(exc.in_flight)} renders queued or running (at most "
            f"{exc.limit} at once):\n{listed}\nWait for one with "
            f"get_video_jobs(render_ids={ids}), or, if the user agrees, cancel one with "
            "cancel_video_job(render_ids=[...]), then render again.",
            error=True,
        )

    def _not_renderable(title: str) -> ToolResult:
        return _result(
            f"You can watch **{title}** but not render it: only its owner or someone it is shared "
            "with as an editor can. Tell the user; they can duplicate it to render their own version.",
            error=True,
        )

    async def _hearing(video_id: str, origin: str, *, manage: bool = False) -> Any:
        """What re-checks the narrations of *video_id* (``voice_recheck.hearing_for``), read on a session closed before
        anything is spoken; a ``ToolResult`` when the video cannot be opened (or, with *manage*, rendered)."""
        from app.services.video_studio import voice_recheck
        from app.services.video_studio.projects import can_manage_project, effective_spec, load_visible_project

        async with async_session() as session:
            user = await _user(session, user_id)
            project = await load_visible_project(session, video_id, user) if user else None
            if project is None:
                return _result(f"No video `{video_id}` you can open.", error=True)
            if manage and not await can_manage_project(session, project, user):
                return _not_renderable(project.title)
            effective = (await effective_spec(session, project, user)).spec
            return await voice_recheck.hearing_for(session, user, effective, origin=origin)

    async def _splice(
        video_id: str, quality: str, source: str, cleared: tuple[str, ...], settled: dict[str, str]
    ) -> Any:
        """A whole render splicing the takes of *cleared* into the ready render *source* (``voice_recheck.Splice``),
        the scenes of *settled* (``scene id -> fallback digest``) settled on it: the start, ``None`` when nothing
        cleared, a ``ToolResult`` when it was refused."""
        from app.services.video_studio import voice_recheck
        from app.services.video_studio.projects import RenderLimitError, load_visible_project, start_render
        from app.services.video_studio.render_checks import RenderGateError

        started = None
        async with async_session() as session:
            user = await _user(session, user_id)
            project = await load_visible_project(session, video_id, user) if user else None
            if project is None:
                return _result(f"No video `{video_id}` you can open.", error=True)
            if cleared:
                try:
                    started = await start_render(
                        session,
                        project,
                        user,
                        quality=quality,
                        owed_to=conversation_uuid,
                        via_agent=True,
                        splice=voice_recheck.Splice(source, cleared),
                    )
                except RenderGateError as exc:
                    return _gate_refused(project.title, exc)
                except RenderLimitError as exc:
                    return await _over_limit(exc)
            # The scenes it could not clear are never offered again at the digest they have now.
            await voice_recheck.settle(session, source, settled)
            await session.commit()
        return started

    async def _recheck(
        video_id: str, quality: str, source: str, offer: list, measured: Any = ()
    ) -> tuple[str, Any] | ToolResult:
        """The re-check ``render_video`` was asked for (``voice_recheck``): each scene offered measured again
        (*measured*: those this call already measured before it started), on no session; those that now pass at the
        video's speed spliced into a new render of the ready render *source*, the others settled on it. ``(what it
        says, the splice started or None)``."""
        from app.services.video_studio import voice_recheck

        known = {outcome.scene_id: outcome for outcome in measured}
        missing = [recheck.scene_id for recheck in offer if recheck.scene_id not in known]
        if missing:
            hearing = await _hearing(video_id, f"the re-check of render {source[:8]}")
            if isinstance(hearing, ToolResult):
                return hearing
            known.update({outcome.scene_id: outcome for outcome in await voice_recheck.recheck(hearing, missing)})
        outcomes = [known[recheck.scene_id] for recheck in offer if recheck.scene_id in known]
        cleared = tuple(outcome.scene_id for outcome in outcomes if outcome.cleared)
        settled = {r.scene_id: r.digest for r in offer if r.scene_id not in cleared}
        started = await _splice(video_id, quality, source, cleared, settled)
        if isinstance(started, ToolResult):
            return started
        spliced = str(started.render.id) if started is not None and not started.reused else None
        return voice_recheck.outcome_line(source, outcomes, spliced), started

    async def _measure_fallbacks(video_id: str) -> tuple[list, str | None] | ToolResult:
        """``render_video(recheck: true)`` before it starts: every narration the voice cache places with a fallback
        take spoken again at the video's speed and heard back (``voice_recheck.fallback_scenes``, ``recheck``) - what
        the render then places. ``(the outcomes, why nothing could be measured)``."""
        from app.services.video_studio import voice_recheck

        hearing = await _hearing(video_id, "the re-check asked of render_video", manage=True)
        if isinstance(hearing, ToolResult):
            return hearing
        if hearing.speaker is None:
            return [], hearing.unavailable or "nothing can speak or hear the narrations"
        scenes = await voice_recheck.fallback_scenes(hearing)
        return (await voice_recheck.recheck(hearing, scenes) if scenes else []), None

    async def _render(video_id: str = "", quality: str = "high", force: Any = False, recheck: Any = False) -> ToolResult:
        from app.services.video_studio import jobs, voice_recheck
        from app.services.video_studio.projects import (
            RenderLimitError,
            RenderStartBusyError,
            can_manage_project,
            load_visible_project,
            replaced_line,
            start_render,
        )
        from app.services.video_studio.render_checks import RenderGateError

        if quality not in QUALITY_PRESETS:
            return _result(f"quality is one of {', '.join(QUALITY_PRESETS)}.", error=True)
        asked_recheck = recheck is True or str(recheck).strip().lower() == "true"
        # Asked to re-check: every narration playing a fallback take measured again first - spoken anew at the video's
        # speed, heard with preview_voiceover's check - so the render places what is heard now, never a stale figure.
        measured: list = []
        unmeasured: str | None = None
        if asked_recheck:
            found = await _measure_fallbacks(video_id)
            if isinstance(found, ToolResult):
                return found
            measured, unmeasured = found
        async with async_session() as session:
            user = await _user(session, user_id)
            project = await load_visible_project(session, video_id, user) if user else None
            if project is None:
                return _result(f"No video `{video_id}` you can open.", error=True)
            if not await can_manage_project(session, project, user):
                return _not_renderable(project.title)
            try:
                started = await start_render(
                    session,
                    project,
                    user,
                    quality=quality,
                    owed_to=conversation_uuid,
                    via_agent=True,
                    force=force is True or str(force).strip().lower() == "true",
                )
            except RenderGateError as exc:
                return _gate_refused(project.title, exc)
            except RenderLimitError as exc:
                return await _over_limit(exc)
            except RenderStartBusyError as exc:
                return _result(str(exc), error=True)
            await session.commit()
            revision = project.revision
            vid = str(project.id)
        said_first = []
        # A ready render covering the picture, scenes of it playing a fallback take the voice pipeline or their lexicon
        # changed since: offered, re-checked when asked - in an automation, with nobody to ask, at once.
        offer = list(getattr(started, "recheck", None) or [])
        if offer and (asked_recheck or automation):
            rechecked = await _recheck(vid, quality, str(started.render.id), offer, measured)
            if isinstance(rechecked, ToolResult):
                return rechecked
            line, splice_started = rechecked
            said_first.append(line)
            offer = []
            if splice_started is not None:
                started = splice_started
            else:
                # Nothing cleared: the ready render stays, answered as it is.
                started.covered_from, started.recheck = None, []
        elif asked_recheck and getattr(started, "covered_from", None) is not None and measured:
            # The ready render covers the revision, yet a narration it plays with a fallback take now passes at the
            # video's speed: spliced in, as an offered re-check would be.
            source = str(started.render.id)
            drawing = getattr(started.render, "drawing", None)
            kept = drawing.get("fallbacks") if isinstance(drawing, dict) else None
            plays = [o for o in measured if not isinstance(kept, dict) or o.scene_id in kept]
            cleared = tuple(o.scene_id for o in plays if o.cleared)
            splice_started = await _splice(vid, quality, source, cleared, {}) if cleared else None
            if isinstance(splice_started, ToolResult):
                return splice_started
            if splice_started is not None:
                spliced = None if splice_started.reused else str(splice_started.render.id)
                said_first.append(voice_recheck.outcome_line(source, plays, spliced))
                started = splice_started
            else:
                said_first.append(voice_recheck.measured_line(measured, source, state=voice_recheck.COVERED))
        elif asked_recheck:
            state = voice_recheck.IN_FLIGHT if started.reused else voice_recheck.QUEUED
            said_first.append(
                voice_recheck.measured_line(measured, str(started.render.id), state=state, unavailable=unmeasured)
            )
        render_id = str(started.render.id)
        replaced = replaced_line(started.superseded, revision)
        reused = started.reused
        carried = getattr(started, "carried_from", None)
        covered = getattr(started, "covered_from", None)
        redrawn = getattr(started, "redrawn", None)
        relaunched = started.relaunched
        estimate = started.preflight
        linted = list(started.notes)
        await jobs.wake()
        rows = await jobs.wait_for("render", [render_id], timeout=_wait_budget(240.0), told_to=conversation_uuid)
        if redrawn:
            # The ready render that matched this revision, drawn by an older release: drawn again, said why.
            said_first.append(redrawn)
        if linted:
            # What the pre-render lint wrote before the render started: the render draws it.
            said_first.append("Before rendering: " + "; ".join(linted) + ".")
        if relaunched:
            said_first.append(
                f"Render `{render_id}` of this revision at {quality}, which no worker resumed after its worker "
                "stopped, is queued again as the same render, first in the queue: it resumes from the segments it "
                "kept instead of starting over."
            )
        if replaced:
            said_first.append(replaced)
        if offer:
            said_first.append(voice_recheck.offer_line(render_id, revision, offer))
        elif covered is not None:
            drawn = f" (drawn for revision {covered})" if covered != revision else ""
            # The re-check of its fallback takes could not be computed: covered as before, said why.
            unrechecked = getattr(started, "unrechecked", None)
            not_rechecked = (
                f" The re-check could not be computed: {unrechecked}; render_video(force: true) hears every "
                "narration again."
                if unrechecked
                else ""
            )
            said_first.append(
                f"Render `{render_id}` already covers revision {revision}, nothing to draw: it was rendered at {quality}"
                f"{drawn}, its gates green, and this revision draws and plays exactly what it does. Nothing was queued; "
                f"it is the render answered below. render_video(force: true) renders it again anyway.{not_rechecked}"
            )
        elif reused and carried is not None:
            said_first.append(
                f"Render `{render_id}` at {quality}, started for revision {carried}, draws exactly what this revision "
                "draws: it was kept and carried to it, its sound mixed for it, instead of being cancelled and drawn "
                "again - it is the one followed."
            )
        elif reused:
            said_first.append(
                f"This revision is already being rendered at {quality}: render `{render_id}`, queued or running, is "
                "the one followed - nothing was cancelled or queued again."
            )
        # Said before it is followed: how long it should take; the platform's sizing once.
        return await _render_result(rows[0] if rows else None, vid, estimate=estimate, said_first=said_first)

    async def _render_scene(
        video_id: str = "", scene: Any = None, quality: Any = None, force: Any = False, **_ignored: Any
    ) -> ToolResult:
        from app.services.video_studio import jobs
        from app.services.video_studio.projects import (
            RenderStartBusyError,
            can_manage_project,
            load_visible_project,
            run_started,
            start_scene_render,
        )
        from app.services.video_studio.render_checks import RenderGateError
        from app.services.video_studio.spec import VideoSpecError

        if scene in (None, ""):
            return _result("Give the scene to preview: its id, or its 0-based index in the spec as written.", error=True)
        if quality not in (None, "", *PREVIEW_QUALITIES):
            return _result(f"quality is {' or '.join(PREVIEW_QUALITIES)} for a scene preview.", error=True)
        async with async_session() as session:
            user = await _user(session, user_id)
            project = await load_visible_project(session, video_id, user) if user else None
            if project is None:
                return _result(f"No video `{video_id}` you can open.", error=True)
            if not await can_manage_project(session, project, user):
                return _result(
                    f"You can watch **{project.title}** but not preview its scenes: only its owner or someone it is "
                    "shared with as an editor can. Tell the user; they can duplicate it to work on their own version.",
                    error=True,
                )
            try:
                started = await start_scene_render(
                    session,
                    project,
                    user,
                    scene=scene,
                    quality=quality or None,
                    owed_to=conversation_uuid,
                    via_agent=True,
                    force=force is True or str(force).strip().lower() == "true",
                )
            except VideoSpecError as exc:
                return _result(str(exc), error=True)
            except RenderStartBusyError as exc:
                # A start of the same scene held its lock past the statement timeout: said plainly, never the SQL.
                return _result(str(exc), error=True)
            except RenderGateError as exc:
                # The pre-render lint: what a preview would rediscover, every fault at once, nothing drawn.
                listed = "\n".join(f"- {reason.message}" for reason in exc.reasons)
                return _result(
                    f"The scene of **{project.title}** was not previewed: its pre-render lint found what its preview "
                    f"would show, without drawing a frame.\n{listed}\nFix them (edit_video), then preview it again; "
                    "a finding that only a regeneration fixes says so first. render_video_scene(force: true) previews "
                    "it anyway.",
                    error=True,
                )
            await session.commit()
            render_id, vid = str(started.render.id), str(project.id)
            linted = list(started.notes)
            advised = list(started.advisories)
        # Every frame cached: served here before the wait reads it; else handed to the workers.
        await run_started(started)
        rows = await jobs.wait_for("render", [render_id], timeout=_wait_budget(240.0), told_to=conversation_uuid)
        result = await _render_result(rows[0] if rows else None, vid)
        if advised:
            # A whole render at a larger quality would refuse it: said with the preview, never after the sweep.
            result.content = (
                "Advisory for a whole render (this preview is not refused): " + "; ".join(advised) + ".\n" + result.content
            )
        if linted:
            result.content = "Before previewing: " + "; ".join(linted) + ".\n" + result.content
        return result

    def _measured_facts(render: Any, *, compact: bool = False) -> list[str]:
        """What a render's answer says of its drawing beside its checks (facts): its stalls, its estimate and
        what it measured; *compact* says the segments it kept in one summary (issue #10480)."""
        from app.services.video_studio.render_preflight import said_of
        from app.services.video_studio.render_process import relaunch_line, stall_line

        return [
            text
            for text in (
                stall_line(getattr(render, "stalls", None)),
                relaunch_line(getattr(render, "relaunches", None)),
                said_of(render, compact=compact),
            )
            if text
        ]

    async def _scene_preview_result(render: VideoRender, state: Any) -> ToolResult:
        """A scene preview: its MP4 linked as a file of this conversation, its three frames attached; its
        verdict and what it is made of first (``render_answer``)."""
        from app.services.video_studio import jobs, render_answer
        from app.services.video_studio.render_checks import insert_crop_ids, insert_reports
        from app.services.video_studio.render_preflight import infra_of

        if render.status == JOB_READY:
            # Its three frames, the 100 % crops of each screen insert's region at the preview's resolution and
            # its findings' frames, attached: every file the answer names is the conversation's.
            frames, checks, linked = await _render_pictures(render.checks, list(render.frame_file_ids or []))
            crop_ids = insert_crop_ids(checks)
            # Linked, never attached: the model reads the pictures, the person plays the video.
            video = await _refs([render.file_id], shown=shown_this_turn)
            answer = render_answer.of_render(render, checks, facts=_measured_facts(render), infra_once=infra_of(render))
            outcome = {
                "render_id": str(render.id),
                "scene_id": render.scene_id,
                "state": "ready",
                # Its verdict: findings alone block or warn; by design and facts are no findings.
                **answer.as_dict(),
                "quality": render.quality,
                "size": [render.width, render.height],
                "file_id": video.ids[0] if video.ids else (str(render.file_id) if render.file_id else None),
                "frame_file_ids": frames.ids,
                "checks": {
                    "pronunciation": (checks or {}).get("pronunciation") or None,
                    # What each screen insert's compositing measured - its colours at its light -, its
                    # crops' file ids among them.
                    "inserts": insert_reports(checks) or None,
                },
            }
            link = f" [[FILE:{outcome['file_id']}]]" if outcome["file_id"] else ""
            # The MP4 is the person's to play; any other moment of it is cut as a picture.
            another = (
                f" The MP4 is a video, not a picture: to look at another moment, save_video_frame(file_id="
                f"'{outcome['file_id']}', at_seconds=t) for t up to {_fmt_seconds(render.duration_ms)} (a later "
                "moment gives its last frame)."
                if outcome["file_id"]
                else ""
            )
            cropped = (
                f" The region of each screen insert is attached too, cut at 100% of the {render.width}x{render.height} "
                "frame at its first, middle and last frame - and, for a residual key, the side of the quad where most of "
                "it sits: judge the compositing on those real pixels (the alignment to the bezel, the key's edges, the "
                "sharpness, the colours, the footage's own key colour past the bezel), never on the downsized frames. "
                "Its numbers - its colours at its light included: tune light (0-1) on them - are this draft's; a render "
                f"measures and crops them again on its own frames ({_ultra_size(render.width, render.height)} for ultra)."
                if crop_ids
                else ""
            )
            # What a player scaling the MP4 to its display shows: a draft is too small to judge sharpness on.
            size_note = (
                f" A {render.quality} preview: a player scales it to its display, so judge sharpness on a standard "
                f"(1080p) preview - render_video_scene(scene='{render.scene_id}', quality='standard')."
                if render.quality == "draft"
                else ""
            )
            return _result(
                f"{json.dumps(outcome)}\nScene `{render.scene_id}` previewed: {_fmt_seconds(render.duration_ms)} at "
                f"{render.width}x{render.height} {render.fps} fps ({render.quality}), its transitions in and out "
                f"included.{size_note}{link}\n{answer.text()}\nIts first, middle and last frames are attached: look at "
                f"them before rendering the video (render_video).{cropped}{_proofs_line(checks, linked)}{another}",
                files=frames,
            )
        if render.status in JOB_TERMINAL_STATUSES:
            shown, checks, linked = await _render_pictures(render.checks, [])
            answer = render_answer.of_render(render, checks, facts=_measured_facts(render), infra_once=infra_of(render))
            outcome = {"render_id": str(render.id), "scene_id": render.scene_id, "state": render.status, **answer.as_dict()}
            return _result(
                f"{json.dumps(outcome)}\nScene preview `{render.id}` {render.status}: {render.error or ''}\n"
                f"{answer.text()}{_proofs_line(checks, linked)}",
                error=True,
                files=shown,
            )
        state = state or await _state_of("render", render)
        place = (
            f", position {state.queue.position} in the {state.queue.lane or 'render'} lane of the render queue"
            if state.queue is not None
            else ""
        )
        return _result(
            f"Scene preview `{render.id}` of `{render.scene_id}` is {state.status} ({jobs.describe(state)}{place}"
            f"{_moved(state)}): call get_video_jobs(render_ids=['{render.id}']) to wait for it.{stalled_line(render)}"
            f"{resumed_note}"
        )

    async def _render_result(
        render: VideoRender | None,
        video_id: str,
        state: Any = None,
        *,
        estimate: Any = None,
        said_first: list[str] | None = None,
        compact: bool = False,
    ) -> ToolResult:
        """A render's answer: a finished one's verdict and what it is made of first (``render_answer``).

        *compact* (``get_video_jobs`` without ``verbose``): a finished whole render answers its findings in full, its
        headline lines and its files, the whole answer stored as a file of the conversation (:func:`_compacted`).

        *estimate* (``render_preflight.Preflight``) and *said_first* are the call that started it: its estimate and
        what the start did said among the facts, and the advice on the platform's sizing its pre-flight was the
        first to need (``infra_once``) - on that answer only, never on a later one of the same render.
        """
        from app.services.video_studio import jobs, render_answer

        if render is None:
            return _result("The render was not found.", error=True)
        started_facts = [*(said_first or []), *([estimate.line()] if estimate is not None else [])]
        infra_once = estimate.infra_once() if estimate is not None else []
        marker = VIDEO_MARKER.format(id=video_id)
        if render.superseded_by_id is not None:
            newer = render.superseded_by_id
            async with async_session() as session:
                exists = await session.get(VideoRender, newer) is not None
            if not exists:
                # The newer render was deleted since (by hand, or by the render retention): nothing to follow.
                return _result(
                    f"Render `{render.id}` was cancelled: a newer render of the same video (`{newer}`) replaced it, "
                    "and that render no longer exists. get_video(video_id=...) says what the video plays now; "
                    "render_video starts a new render."
                )
            return _result(
                f"Render `{render.id}` was cancelled: a newer render of the same video (`{newer}`) replaced it. "
                f"Follow that one: get_video_jobs(render_ids=['{newer}'])."
            )
        if render.scene_id:
            return await _scene_preview_result(render, state)
        forced = " It was forced past its quality gate." if render.gate_override else ""
        if render.status == JOB_READY:
            from app.services.video_studio.render_checks import insert_crop_ids

            # The poster, the 100 % crops of each screen insert's region at the render's resolution and its
            # findings' frames, attached: every file the answer names is the conversation's.
            files, checks, linked = await _render_pictures(render.checks, [render.poster_file_id])
            crop_ids = insert_crop_ids(checks)
            answer = render_answer.of_render(
                render,
                checks,
                facts=[*started_facts, *_measured_facts(render)],
                infra_once=infra_once,
                headlines=[*started_facts, *_measured_facts(render, compact=True)],
            )
            # Its captions file: named, readable by the run, never attached.
            captions_id = getattr(render, "captions_file_id", None)
            captions = await _refs([captions_id], show=False) if captions_id else None
            outcome = {
                "render_id": str(render.id),
                "state": "ready",
                **answer.as_dict(),
                "quality": render.quality,
                "size": [render.width, render.height],
                "fps": render.fps,
                "captions_file_id": captions.ids[0] if captions and captions.ids else None,
            }
            cropped = (
                f" Each screen insert's region is attached too, cut at 100% of the {render.width}x{render.height} frame "
                "at its first, middle and last frame - and, for a residual key, the side of the quad where most of it "
                "sits: the real pixels of the compositing. Its gates read every frame it is drawn on (each insert's "
                "coverage line says how many and the worst); get_video_insert_sweep shows each whole, one picture per "
                "scene. Its numbers (sharpness, colours) were measured on this "
                f"render's own {render.width}x{render.height} frames, never on a preview's."
                if crop_ids
                else ""
            )
            # The check frames of every scene compositing a real screen, at the render's own size: linked as
            # files of the conversation, never attached - a 4K still is the reviewer's to open (read_file_content).
            checked_scenes = ""
            if render.frame_file_ids:
                # Its own name: ``linked`` is the count ``_proofs_line`` says, never these pictures (their repr
                # leaked every FileRef, S3 object names included, into the answer).
                scene_frames = await _refs(list(render.frame_file_ids), shown=shown_this_turn, show=False)
                if scene_frames.ids:
                    checked_scenes = (
                        f"\nCheck frames of the scenes with a screen insert or a replaced screen, at the render's native "
                        f"{render.width}x{render.height} (first, middle and last frame of each scene, files of this "
                        f"conversation): {', '.join(f'`{fid}`' for fid in scene_frames.ids)}. Look at them (read_file_content) "
                        "before telling the user the screens are right."
                    )
            headline = (
                f"Render `{render.id}` is ready: {_fmt_seconds(render.duration_ms)}, "
                f"{render.width}x{render.height} at {render.fps} fps ({(render.size_bytes or 0) / 1_000_000:.1f} MB)."
                f"{forced}"
            )
            closing = (
                f"{render_frames_line(render.file_id, render.duration_ms)} Include this marker in your reply so the "
                f"user can play it: {marker}"
            )
            full = (
                f"{json.dumps(outcome)}\n{headline}\n{answer.text()}\nThe poster frame is attached.{cropped}"
                f"{_proofs_line(checks, linked)}{checked_scenes}{closing}"
            )
            if compact:
                brief = {
                    "render_id": str(render.id),
                    "state": "ready",
                    **answer.compact_dict(),
                    "quality": render.quality,
                    "size": [render.width, render.height],
                    "fps": render.fps,
                    "mp4_file_id": str(render.file_id) if render.file_id else None,
                    "captions_file_id": outcome["captions_file_id"],
                }
                return await _compacted(
                    render,
                    full,
                    brief,
                    f"{headline}\n{answer.compact_text()}\nThe poster frame is attached."
                    f"{_proofs_line(checks, linked)}{closing}",
                    files=files,
                )
            return _result(full, files=files)
        if render.status in JOB_TERMINAL_STATUSES:
            # Its inserts' crops and its findings' frames attached as files of the conversation, named so.
            shown, checks, linked = await _render_pictures(render.checks, [])
            answer = render_answer.of_render(
                render,
                checks,
                facts=[*started_facts, *_measured_facts(render)],
                infra_once=infra_once,
                headlines=[*started_facts, *_measured_facts(render, compact=True)],
            )
            outcome = {"render_id": str(render.id), "state": render.status, **answer.as_dict()}
            kept = ""
            if render.file_id:
                video = await _refs([render.file_id], shown=shown_this_turn)
                if video.ids:
                    kept = (
                        f"\nIts MP4, to look at the times above: [[FILE:{video.ids[0]}]]."
                        f"{render_frames_line(video.ids[0], render.duration_ms)}"
                    )
            full = (
                f"{json.dumps(outcome)}\nRender `{render.id}` {render.status}: {render.error or ''}\n{answer.text()}"
                f"{kept}{_proofs_line(checks, linked)}"
            )
            if compact:
                brief = {"render_id": str(render.id), "state": render.status, **answer.compact_dict()}
                return await _compacted(
                    render,
                    full,
                    brief,
                    f"Render `{render.id}` {render.status}: {render.error or ''}\n{answer.compact_text()}"
                    f"{kept}{_proofs_line(checks, linked)}",
                    files=shown,
                    error=True,
                )
            return _result(full, error=True, files=shown)
        state = state or await _state_of("render", render)
        # Not finished: no verdict yet. What the start said, then the platform's sizing once, apart.
        started = "".join(f"{text}\n" for text in started_facts)
        sizing = (
            "\nPlatform sizing (said once, for the administrator; no finding): " + " ".join(infra_once) if infra_once else ""
        )
        return _result(
            f"{started}Render `{render.id}` is {state.status} ({jobs.describe(state)}{_moved(state)}). "
            f"Call get_video_jobs(render_ids=['{render.id}']) to wait. The card "
            f"{marker} shows it as soon as it is ready - include it in your reply.{stalled_line(render)}"
            f"{estimated_line(render)}{await ranges_line(render)}{await _competing_line(render)}{resumed_note}{sizing}"
        )

    async def _competing_line(render: VideoRender) -> str:
        """The jobs sharing a running render's worker, with their owner and video and which the caller may cancel
        (``cpu_share.competitors``); empty for none, or while no worker runs it."""
        from app.services.video_studio import cpu_share

        if render.status != JOB_RUNNING:
            return ""
        try:
            async with async_session() as session:
                user = await _user(session, user_id)
                found = await cpu_share.competitors(session, render, user) if user is not None else []
        except Exception as exc:  # what shares its worker, never a reason the answer fails
            logger.info("Video render %s: the jobs sharing its worker were not read: %s", render.id, format_exception_message(exc))
            return ""
        return f"\n{cpu_share.competitors_words(found)}" if found else ""

    async def _compacted(
        render: VideoRender, full: str, brief: dict, text: str, *, files: Any, error: bool = False
    ) -> ToolResult:
        """A finished whole render's compact answer: *brief* (its JSON head) and *text*, the *full* answer stored as a
        markdown check file of the conversation, named and readable by the run (issue #8719)."""
        from app.llm.base import FileRef
        from app.services.builtin_tools._shared import _persist_generated_file

        report_id = None
        try:
            row = await _persist_generated_file(
                user_id=user_id,
                filename=f"render {str(render.id)[:8]} - full report.md",
                raw_bytes=full.encode("utf-8"),
                content_type="text/markdown",
                conversation_id=str(conversation_uuid) if conversation_uuid else None,
                assignment_run_id=None,
                source=_SHOWN_FILE_SOURCE,
                description=f"The full answer of render {render.id}: every by-design and facts line, per insert.",
            )
            _readable(_Pictures(every=[FileRef.from_attachment(row)]))
            report_id = str(row.id)
        except Exception as exc:  # the full answer is still given inline
            logger.warning("Render %s report not stored: %s", render.id, format_exception_message(exc))
            return _result(full, error=error, files=files)
        brief["report_file_id"] = report_id
        return _result(
            f"{json.dumps(brief)}\n{text}\nThe full report (every by-design and facts line, per insert) is file "
            f"`{report_id}`: read_file_content opens it; get_video_jobs(render_ids=['{render.id}'], verbose=true) "
            "answers it inline.",
            error=error,
            files=files,
        )

    async def _job_text(kind: str, row: Any, state: Any, *, verbose: bool = False) -> ToolResult:
        """What to do with one job: the render card, the recording's action log, a failed shot's recovery."""
        from app.services.video_studio import jobs

        if kind == "render":
            return await _render_result(row, str(row.project_id), state, compact=not verbose)
        if kind == "take":
            return await _take_result(row, include_sheet=False, state=state)
        if state.status == jobs.STATE_FAILED:
            return _result(_failed_clip_text(row) if kind == "clip" else _failed_music_text(row))
        noun = "Clip" if kind == "clip" else "AI track"
        if state.status == jobs.STATE_READY and kind == "clip":
            # Its poster once, the first answer that reports it made; named after that.
            poster = await _shown([row.poster_file_id])
            seen = (
                f" Its poster: [[FILE:{poster.ids[0]}]]{'' if poster.refs else ' (shown earlier)'}." if poster.ids else ""
            )
            return _result(f"{noun} `{row.id}`: ready, {_fmt_seconds(row.duration_ms)}.{seen}", files=poster.refs)
        if state.status == jobs.STATE_READY:
            from app.services.video_studio.music import EXTEND_FAILED

            # An extension that was not made: the track is ready on the take it kept.
            unmade = (row.params or {}).get(EXTEND_FAILED) if kind == "music" else None
            kept = f" Its extension was not made ({unmade}): the take approved still plays, as it was." if unmade else ""
            return _result(f"{noun} `{row.id}`: ready, {_fmt_seconds(row.duration_ms)}.{kept}")
        return _result(f"{noun} `{row.id}`: {state.status}, {jobs.describe(state)}{_moved(state)}.")

    async def _jobs(
        recording_ids: Any = None,
        clip_ids: Any = None,
        render_ids: Any = None,
        music_ids: Any = None,
        verbose: Any = False,
        rejudge: Any = False,
    ) -> ToolResult:
        from app.services.video_studio import jobs

        verbose = verbose is True or str(verbose).strip().lower() == "true"
        rejudge = rejudge is True or str(rejudge).strip().lower() == "true"
        if rejudge and not (_json(render_ids) or []):
            return _result("rejudge needs render_ids: the ready renders to judge again.", error=True)

        started = time.monotonic()
        budget = _wait_budget(180.0)
        asked = {
            "take": _json(recording_ids) or [],
            "clip": _json(clip_ids) or [],
            "music": _json(music_ids) or [],
            "render": _json(render_ids) or [],
        }
        asked = {
            k: [v for v in (vals if isinstance(vals, list) else [vals]) if str(v).strip()] for k, vals in asked.items()
        }
        given = any(asked.values())
        resolved: dict[str, str] = {}
        unknown: list[str] = []
        if given:
            # Full ids as they are, id prefixes taken for the one job they start, anything else not found:
            # ids given never fall back to the in-flight listing.
            async with async_session() as session:
                user = await _user(session, user_id)
                if user is None:
                    return _result("Available to signed-in users only.", error=True)
                named = await jobs.resolve_job_ids(session, asked, user)
            if named.ambiguous:
                return _result(named.ambiguous, error=True)
            asked, resolved, unknown = named.ids, named.resolved, named.not_found
            if not any(asked.values()):
                return _result(f"No job you can read among {', '.join(unknown)}.", error=True)
        listing = not given
        if listing:
            # No ids: what the person has in flight, reported at once rather than waited on.
            in_flight = await jobs.jobs_in_flight(safe_uuid(user_id)) if safe_uuid(user_id) else {}
            if not in_flight:
                return _result(
                    "Nothing of yours is being made: no recording, AI shot, AI track or render is queued or running."
                )
            asked = {kind: in_flight.get(kind, []) for kind in asked}
            budget = 0.0
        # Only the jobs the caller may read are waited on and reported (``jobs.may_read_job``).
        found, _ = await jobs.wait_for_jobs(asked, timeout=0)
        readable: dict[str, list[str]] = {}
        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Available to signed-in users only.", error=True)
            for kind, rows in found.items():
                readable[kind] = [str(row.id) for row in rows if await jobs.may_read_job(session, kind, row, user)]
        missing = unknown + [jid for kind, ids in asked.items() for jid in ids if jid not in readable.get(kind, [])]
        if not any(readable.values()):
            return _result(f"No job you can read among {', '.join(missing)}.", error=True)
        if rejudge:
            # Judged again under the running quality report, read-only on each MP4: nothing drawn, nothing queued.
            from app.services.video_studio import render_rejudge

            # The whole call stays inside its wait budget: renders it cannot reach are named for a further call.
            judged, left = await render_rejudge.rejudge_many(
                readable.get("render", []), safe_uuid(user_id), budget=max(0.0, budget - (time.monotonic() - started))
            )
            payload: dict[str, Any] = {"rejudged": judged}
            if missing:
                payload["not_found"] = missing
            if left:
                payload["not_rejudged_yet"] = left
            tail = (
                f"\n\nNot re-judged in this call's time: get_video_jobs(render_ids={json.dumps(left)}, rejudge=true)."
                if left
                else ""
            )
            return _result(
                json.dumps(payload, indent=1, default=str)
                + "\n\n"
                + "\n\n".join(render_rejudge.rejudge_line(r) for r in judged)
                + tail
            )
        from app.services.video_studio import provider_cooling, provider_waits

        still_open = [row for rows in found.values() for row in rows if row.status not in JOB_TERMINAL_STATUSES]
        waiting = (provider_cooling.HELD_BY_PROVIDER, provider_waits.WAITING)
        if still_open and all(getattr(row, "provider_state", None) in waiting for row in still_open):
            # Only shots waiting for their provider to cool or to answer: waiting here ends none sooner.
            budget = 0.0
        rows, why = await jobs.wait_for_jobs(
            readable, timeout=max(0.0, budget - (time.monotonic() - started)), told_to=conversation_uuid
        )
        reported: list[dict] = []
        lines: list[str] = []
        files: list = []
        unfinished = False
        for kind in asked:
            kind_rows = rows.get(kind, [])
            for row, state in zip(kind_rows, await jobs.job_states(kind, kind_rows), strict=True):
                entry = state.as_dict()
                if kind == "clip" and state.status in (jobs.STATE_READY, jobs.STATE_FAILED):
                    # The check record saved at completion, as an inline answer gives it: a shot made in
                    # the background is read here, never recomputed.
                    entry["checks"] = await _clip_checks(row)
                    if (failed := await _failed_attempt(row)) is not None:
                        entry["failed_attempt"] = failed
                    if previous := clip_providers.previous_takes(row.params):
                        # The takes it played before, newest last: restore_take puts one back.
                        entry["previous_takes"] = [
                            {k: p[k] for k in ("file_id", "poster_file_id", "model", "replaced_at") if p.get(k)} for p in previous
                        ]
                if kind == "render" and row.status in JOB_TERMINAL_STATUSES:
                    # Which checker judged it, and whether that is the running one.
                    from app.services.video_studio.render_checks import report_provenance

                    entry["quality_report"] = report_provenance(
                        row.checks, getattr(row, "relaunches", None), getattr(row, "drawing", None)
                    )
                reported.append(entry)
                unfinished = unfinished or state.status in (jobs.STATE_QUEUED, jobs.STATE_RUNNING)
                result = await _job_text(kind, row, state, verbose=verbose)
                lines.append(result.content)
                files.extend(result.files or [])
        summary: dict[str, Any] = {
            "jobs": reported,
            "waited_seconds": round(time.monotonic() - started),
            "returned_because": (
                "listing what is in flight (no ids given)"
                if listing
                else f"the wait budget of {round(budget)} s ran out"
                if why == jobs.WAIT_BUDGET
                else why
            ),
        }
        if resolved:
            summary["resolved"] = resolved
        if missing:
            summary["not_found"] = missing
        text = json.dumps(summary, indent=1) + "\n\n" + "\n\n".join(lines)
        if unfinished and listing:
            ids = {jobs.JOB_ID_ARGUMENTS[kind]: kind_ids for kind, kind_ids in asked.items() if kind_ids}
            text += (
                f"\n\nTo wait for them, call get_video_jobs({', '.join(f'{k}={json.dumps(v)}' for k, v in ids.items())}); "
                "cancel_video_job stops one the user no longer wants."
            )
        elif unfinished:
            text += (
                "\n\nCall get_video_jobs again with the same ids to keep waiting: each call returns as soon as one "
                f"of them finishes.{resumed_note}"
            )
        return _result(text, files=files)

    async def _cancel(
        recording_ids: Any = None, clip_ids: Any = None, render_ids: Any = None, music_ids: Any = None
    ) -> ToolResult:
        from app.models.video_studio import VideoClip, VideoMusic
        from app.services.audit import log_audit
        from app.services.video_studio import jobs

        models = {"take": VideoTake, "clip": VideoClip, "music": VideoMusic, "render": VideoRender}
        asked = {
            kind: [
                str(jid)
                for jid in (safe_uuid(str(v)) for v in (vals if isinstance(vals, list) else [vals]) if v)
                if jid
            ]
            for kind, vals in (
                ("take", _json(recording_ids) or []),
                ("clip", _json(clip_ids) or []),
                ("music", _json(music_ids) or []),
                ("render", _json(render_ids) or []),
            )
        }
        if not any(asked.values()):
            return _result(
                "Give the recording_ids, clip_ids, music_ids or render_ids to cancel (get_video_jobs without ids lists "
                "what is in flight).",
                error=True,
            )
        lines: list[str] = []
        cancelled = 0
        async with async_session() as session:
            user = await _user(session, user_id)
            if user is None:
                return _result("Available to signed-in users only.", error=True)
            for kind, ids in asked.items():
                noun = jobs.KIND_LABELS[kind]
                for jid in ids:
                    row = await session.get(models[kind], uuid.UUID(jid))
                    if row is None or not jobs.may_cancel(row, user):
                        lines.append(f"- {noun} `{jid}`: none of yours (only who started a job cancels it).")
                    elif await jobs.cancel(session, kind, row, reason=jobs.cancelled_by(user, "cancel_video_job")):
                        cancelled += 1
                        lines.append(f"- {noun} `{jid}`: cancelled.")
                    else:
                        lines.append(f"- {noun} `{jid}`: already {row.status}, nothing to cancel.")
            if cancelled:
                await log_audit(
                    session,
                    user=user,
                    action="update",
                    entity_type="video_job",
                    entity_id=None,
                    entity_name=None,
                    summary=f"Cancelled {cancelled} Video Studio job(s) through an agent",
                    details={"jobs": {kind: ids for kind, ids in asked.items() if ids}, "via": "agent"},
                )
            await session.commit()
        return _result("\n".join(lines), error=cancelled == 0)

    executor = CallableToolExecutor()
    for name, handler in (
        ("list_browser_profiles", _list_profiles),
        ("save_browser_profile", _save_profile),
        ("sign_in_browser_profile", _sign_in),
        ("submit_browser_mfa_code", _submit_code),
        ("test_browser_profile_sign_in", _test_sign_in),
        ("check_browser_profile_session", _check_session),
        ("snapshot_browser_profile_page", _snapshot_page),
        ("delete_browser_profile", _delete_profile),
        ("record_browser_demo", _record),
        ("get_video_recording", _get_take),
        ("list_video_recordings", _list_takes),
        ("video_studio_capabilities", _capabilities),
        ("generate_video_clip", _generate_clip),
        ("preview_start_frame", _preview_start_frame),
        ("save_video_frame", _save_frame),
        ("save_video_frames", _save_frames),
        ("get_video_insert_sweep", _insert_sweep),
        ("compose_picture", _compose_picture),
        ("generate_music", _generate_music),
        ("preview_voiceover", _preview_voice),
        ("create_video", _create_video),
        ("edit_video", _edit_video),
        ("get_video", _get_video),
        ("list_videos", _list_videos),
        ("share_video", _share_video),
        ("set_video_public_link", _set_public_link),
        ("delete_video_renders", _delete_renders),
        ("delete_video", _delete_video),
        ("render_video", _render),
        ("render_video_scene", _render_scene),
        ("get_video_jobs", _jobs),
        ("cancel_video_job", _cancel),
    ):
        executor.register(name, handler)
        if "video_id" in inspect.signature(handler).parameters:
            executor.wrap_handler(name, lambda inner: _with_video_id_prefix(inner, user_id))
    return _definitions(list(QUALITY_PRESETS)), executor


def _with_video_id_prefix(handler: Any, user_id: str) -> Any:
    """*handler* (the tools of *user_id*) taking a ``video_id`` that is an id prefix (8+ hexadecimal
    characters) of one video the caller can open for its full id
    (``projects.resolve_video_id_prefix``); a prefix starting several videos is
    refused naming them, before the handler runs. A full id, an empty one or a
    prefix starting none reaches the handler unchanged."""
    from app.services.builtin_tools.id_prefix import id_prefix

    @functools.wraps(handler)
    async def resolved(*args: Any, **kwargs: Any) -> Any:
        value = kwargs.get("video_id")
        if id_prefix(value) is not None:
            from app.services.video_studio.projects import resolve_video_id_prefix

            async with async_session() as session:
                user = await _user(session, user_id)
                if user is not None:
                    value, ambiguous = await resolve_video_id_prefix(session, value, user)
                    if ambiguous:
                        return _result(ambiguous, error=True)
                    kwargs["video_id"] = value
        return await handler(*args, **kwargs)

    return resolved


async def _resolve_clip_id_prefix(session: Any, clip_id: Any, user: Any) -> tuple[Any, str | None]:
    """*clip_id* with an id prefix (8+ hexadecimal characters) of one AI shot of *user* taken for its full id;
    the refusal naming the candidates when it starts several. A full id, or a prefix starting none, comes back
    unchanged, so the caller answers its usual not found."""
    from app.models.video_studio import VideoClip
    from app.services.builtin_tools.id_prefix import resolve_id_prefix

    return await resolve_id_prefix(
        session, VideoClip, clip_id, [VideoClip.user_id == user.id], noun="AI shots", label=VideoClip.name
    )


def _wait_budget(default: float) -> float:
    from app.services.video_studio.jobs import tool_wait_budget

    return tool_wait_budget(default)


#: A page state reset (``spec.PageReset``): ``true`` clears the parts below set to their defaults.
_RESET_PARTS_SCHEMA = {
    "type": "object",
    "properties": {
        "url_query": {
            "type": "boolean",
            "description": "Open the URL exactly as given (default true); false reloads the page at the address it landed on.",
        },
        "local_storage": {"type": "boolean", "description": "Clear the origin's localStorage (default true)."},
        "session_storage": {"type": "boolean", "description": "Clear the origin's sessionStorage (default true)."},
        "indexed_db": {"type": "boolean", "description": "Delete the origin's IndexedDB databases (default false)."},
    },
}


def _reset_state_schema(description: str) -> dict:
    return {"description": description, "anyOf": [{"type": "boolean"}, _RESET_PARTS_SCHEMA]}


_STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "goto",
                "click",
                "double_click",
                "right_click",
                "type",
                "press",
                "hover",
                "drag",
                "wheel",
                "scroll",
                "select",
                "wait",
                "wait_for",
                "highlight",
                "pause",
                "marker",
            ],
        },
        "at": {
            "type": "object",
            "properties": {"x": {"type": "number"}, "y": {"type": "number"}, "in": {"type": "string"}},
            "required": ["x", "y"],
            "description": (
                "click / double_click / right_click / hover / drag / wheel on a point instead of an element (a spot "
                "of a canvas graph): CSS px of the window, or with in (a selector) on that element - a value up to 1 "
                "a share of its box ({x: 0.5, y: 0.5, in: 'canvas'} is its centre), larger CSS px from its corner."
            ),
        },
        "node": {
            "type": "object",
            "properties": {"label": {"type": "string"}},
            "required": ["label"],
            "description": (
                "The same pointer actions on a node of a graph canvas, found by its label: the entry of the list the "
                "graph keeps for screen readers (its text before the first comma is the label, or ends with it: "
                "'Host web-01, 3 relationships' for web-01), the label painted on the canvas read "
                "by OCR, else the highlight the node takes when that entry is made active - read again before every "
                "step, so a pan or a zoom in between is followed. The action log's located says which found it. A "
                "label the list does not hold fails the step naming its three closest entries. The camera frames "
                "the node as it frames an element."
            ),
        },
        "hover_ms": {
            "type": "integer",
            "description": "ms the cursor rests on its target, moving over it, before the button (default 250 on at / node: a canvas opens its menu or card on hover).",
        },
        "modifiers": {
            "type": "array",
            "items": {"type": "string", "enum": ["Control", "Shift", "Alt", "Meta"]},
            "description": "Keys held through a pointer action (Control + click adds to a graph's selection).",
        },
        "selector": {
            "type": "string",
            "description": "CSS or Playwright selector (#id, .class, text=Save, role=button[name=Save]).",
        },
        "role": {
            "type": "string",
            "description": "ARIA role (button, link, textbox, tab, menuitem...); with name, the most robust locator.",
        },
        "name": {"type": "string", "description": "Accessible name for role."},
        "text": {
            "type": "string",
            "description": "type: the text to type. click/hover/highlight: the element's visible text when no selector.",
        },
        "nth": {"type": "integer", "description": "Which match (0 = first)."},
        "url": {"type": "string", "description": "goto: the URL."},
        "key": {"type": "string", "description": "press: Enter, Tab, Escape, ArrowDown, Control+K..."},
        "value": {
            "type": "string",
            "description": (
                "select: the option to choose - a native select element's value or label, else the name of the "
                "option a combobox's list shows (typed into an editable one first when it is not listed)."
            ),
        },
        "seconds": {
            "type": "number",
            "description": (
                "wait / pause / highlight duration; scroll by pixels and wheel: how long the wheel takes (default: "
                "the pace's speed); drag: how long the button is held while the cursor moves (default 0.8)."
            ),
        },
        "by": {"type": "integer", "description": "scroll / wheel: wheel pixels (positive = down; over a canvas graph the wheel zooms it, negative in)."},
        "to": {
            "description": (
                "scroll: top or bottom. drag: where it releases - {x, y, in?} like at, or {node: {label}}; it moves "
                "in 24+ pointer moves so a lasso or a node follows smoothly."
            ),
            "anyOf": [
                {"type": "string", "enum": ["top", "bottom"]},
                {
                    "type": "object",
                    "properties": {
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "in": {"type": "string"},
                        "node": {"type": "object", "properties": {"label": {"type": "string"}}},
                    },
                },
            ],
        },
        "delay_ms": {"type": "integer", "description": "type: ms between keystrokes."},
        "clear": {"type": "boolean"},
        "submit": {"type": "boolean", "description": "type: press Enter after."},
        "optional": {
            "type": "boolean",
            "description": "A step that may fail without failing the take; skipped, its wait is cut from the video.",
        },
        "timeout": {
            "type": "number",
            "description": (
                "Seconds to wait for the element (default 15, 3 for an optional step); a wait held until settled: "
                "how much longer than its seconds it may wait (default 10)."
            ),
        },
        "until_settled": {
            "type": "boolean",
            "description": (
                "wait: hold past its seconds until the page settled - no progress indicator (role=progressbar, "
                "aria-busy), its DOM and canvases still 0.5 s -, at most its timeout more; the action log says when "
                "it settled. Default true after a click that opened another page or switched a tab, else false."
            ),
        },
        "label": {"type": "string", "description": "How the step is named in the action log and callouts."},
        "zoom": {"type": "boolean", "description": "false keeps the automatic camera from zooming on this step."},
        "reset_state": _reset_state_schema(
            "goto: the page it opens starts from a clean state - its origin's localStorage and sessionStorage cleared "
            "before its scripts run (the profile's keep_storage_keys kept, cookies never cleared). true for the "
            "default parts; the event logs what was cleared."
        ),
    },
    "required": ["action"],
}


#: The first frame of a shot, as generate_video_clip takes it and preview_start_frame shows it.
_START_FRAME_PROPERTIES: dict[str, Any] = {
    "reference_file_id": {
        "type": "string",
        "description": "An image the shot starts from, or a video file (a frame of it).",
    },
    "reference_recording_id": {
        "type": "string",
        "description": "A recording the shot starts from: its last frame unless a step or a moment.",
    },
    "reference_clip_id": {
        "type": "string",
        "description": "An AI shot this one continues from its last frame (or a moment).",
    },
    "reference_at_step": {
        "type": "integer",
        "minimum": 0,
        "description": "The recording's step (as in its action log) whose result the shot starts "
        "from: the frame its contact sheet shows.",
    },
    "reference_at_seconds": {
        "type": "number",
        "minimum": 0,
        "description": "The moment of the video the shot starts from.",
    },
    "crop": {
        "type": "string",
        "enum": ["center"],
        "description": "A frame of a recording, shot or video file must have the shot's aspect "
        "ratio; 'center' starts from its middle, cut to that ratio (a 16:9 recording into a 9:16 "
        "shot). An image is sent as it is.",
    },
}


def _definitions(qualities: list[str]) -> list[ToolDefinition]:
    from app.services.video_studio.render_cleanup import SELECTORS
    from app.services.video_studio.scroll_pacing import PACED_RATES
    from app.services.video_studio.spec import PREVIEW_QUALITIES, RENDER_QUALITIES

    spec_help = (
        "The video spec (JSON): {title, template_id? (a video template from video_studio_capabilities; omitted, the "
        "video keeps its look - its template or free style - and a new one wears the platform default; it presets "
        "the look, adds its intro / outro cards - a first title / last outro scene of "
        "your own replaces them, dressed in their look - and keeps its locked choices), "
        "freestyle? (true: no template at all), pace? calm|dynamic (dynamic - for a short social cut, never a demo "
        "narrated as a product manager would - presets where the spec leaves them unset: recordings with idle.mode cut, camera pace dynamic and "
        "move_seconds 0.5, end_hold_ms 300 and fit both ways; auto transitions at the dynamic pace; statements "
        "revealed word by word; the save's quality check says the pace: words a minute, scene lengths, still screen "
        "under the voice), format: {aspect_ratio 16:9|9:16|1:1|4:5, fps?}, theme: {mode "
        "dark|light, background aurora|gradient|mesh|grid|mosaic|solid, accent?, accent_2?, logo_file_id?, watermark? (unset: the "
        "template's corner), motion? {personality precise|calm|energetic|cinematic|playful (how every card, statement, "
        "overlay and motion scene moves; unset, the template's), speed? 0.5-2, title? lines|words|letters, logo? "
        "rise|fade|scale|mask, accent? none|sweep, beat_sync? off|cuts|all (cuts - and with all, builds - land on the "
        "music's beats), motion_blur? off|on (drawn at high and ultra, three times the frames)}}, "
        "scenes: [...], default_transition: {type auto (the default: each cut picked from the scenes on either side) "
        "|cut|crossfade|fade_black|slide_left|slide_up|zoom|blur|wipe|whip|flash, pace calm|dynamic|classic (for "
        "auto; classic only cuts, crossfades and fades to black - a brand film's grammar), duration?}; the results "
        "list the transition planned into each scene, "
        "voiceover: {enabled, provider_id? + model? (a speech model from video_studio_capabilities; omitted, the "
        "platform's), voice? (one of its voices), style? (how the voice speaks, or a preset: energetic|calm), speed "
        "(0.7-1.3), pronunciations? [{text: "
        "'SaaS', say: 'sass', ipa?, match? exact|word|case_insensitive, apply_to? voice|captions|both}] (over the "
        "platform's pronunciation lexicon; the captions keep the written words)}, music?: "
        "{ONE source - track (a built-in track from music_library: composed for this video, its length and its "
        "scene changes; needs no provider), music_id (an AI track, generate_music) or file_id (an uploaded sound) - "
        "or prompt (an AI track composed for the video when it is saved: genre, mood, instruments, tempo, "
        "instrumental), volume (0-1, its level against the voice where no one speaks; default 0.5), ducking, duck_db "
        "(how far it goes down under the narration, default 10), start (seconds into a track), loop, ending "
        "fade|natural, fade_in, fade_out}, clip_sound? auto|keep|off (the clips' own sound where a scene does not "
        "set audio: auto - the default - drops an AI shot's under the music and keeps an uploaded video's; off "
        "leaves the voice and the music alone), captions: {enabled, burn_in, style pill|bar|outline}, "
        "template_scenes? {intro?: {duration?, kicker?, align? left|center}, outro?: {duration?, kicker?}} (the cards "
        "the template adds, which are not in the spec: their length - at least what their words take to read, each "
        "save's quality check states each card's minimum - and kicker, null for none; where the opening card sets "
        "its title, the closing card always centred)}, references? [{id, file_id (a crop of the prop from the scene that "
        "got it right - save_video_frame with a region - or the character's portrait), label ('the branded rug'), kind "
        "prop|character, description? (what must stay the same, for a prompt: 'navy quilted rug, teal piping')}] (the "
        "recurring props and characters the AI shots must keep across scenes: a clip scene names them in consistency, "
        "the save reads the shot's first, middle and last frames against each - a scene whose shot drifted is the "
        "finding reference_drift with the crop -, and generate_video_clip with consistency sends them to the "
        "model)}. The voice "
        "leads the mix by itself: the music and every clip sound kept are levelled with it and go down under each "
        "sentence. Scene types (every "
        "scene: id?, narration? - "
        "spoken by the voice-over, the scene lasts at least as long -, narration_speed? (0.7-1.3: this scene's "
        "narration said faster or slower than voiceover.speed), transition? (a cut takes no duration), "
        "watermark? (false: the corner watermark off on this scene alone, for footage that shows the brand's logo "
        "itself - a render refuses a scene showing the brand's mark twice, duplicate_logo; unset: the video's), "
        "overlays? [{type callout|lower_third|badge|caption|spotlight, text, subtext?, start, duration, at_step? "
        "(recording step), position, style} or a picture: {type: image, file_id (a logo, an emblem, a product mark "
        "- a file or a template asset) OR asset logo|logo_light|logo_mark (the template's brand mark by role), "
        "position (auto|top|bottom|left|right|center|top_left|top_right|bottom_left|bottom_right), width (a share "
        "of the frame's width, default 0.12, or pixels of a 1080p frame), opacity, start, duration, at_step?, text? "
        "(its label)} - drawn above the picture and below the captions; never ask a video model to draw a logo; "
        "or, on a clip, a recording, an image (a still of a room with its screens) or a statement with a backdrop, a "
        "screen insert: {type: screen_insert, source: "
        "{file_id (a picture - a PNG keeps its alpha - or a video) | take_id | clip_id | asset logo|logo_light|"
        "logo_mark (the template's own file of that role: the full logo and the bare mark are two files)}, quad? "
        "[[x, y] x 4] (the surface's corners in its footage, 0-1: top left, top "
        "right, bottom right, bottom left; leave it out on an insert without track: the save measures the bezel "
        "on the footage frame the insert comes in on and stores that quad - get_video shows it; several "
        "take a screen each, left to right -, refusing the "
        "save, that frame attached, when it finds none; a quad given within 2% of the frame's width of the bezel is "
        "snapped to it, one further off refused), trim_start? (seconds of a video source skipped), fit cover|contain "
        "(kept whole on transparency, never a black card), glow? (0-1: its light blooming around what it draws), "
        "light? (0-1, default 0.15: how much of the footage's own light and reflections play over it, as light - "
        "never a colour cast; 0 draws the source as it is), zoom? {start (seconds after the insert comes in, "
        "default 0), end, scale (1-4), focus? [x, y] (0-1 of the source, default the centre)} (a camera push: the "
        "source eased to scale times its size within the quad, panned to focus - the page's own layout, larger), "
        "track? (true: the quad, given on the frame the insert comes in on, follows the surface and its bezel in "
        "perspective - a render fails 'surface lost at t=...' with the frame), key? {color green|blue|magenta|"
        "#rrggbb, tolerance 0.05-1 (0.3), feather px (1.5)} (drawn only where the surface shows that colour, spill "
        "removed - a named colour is the shade the shot's surface shows, a #rrggbb exactly that: ask the video "
        "model for a flat green screen and key it green; unset, what crosses the surface through "
        "its edge stays in front of it), despill? {edge (default true), width_px (0-16, default 2), strength (0-1, "
        "default 1), own_hues? [red|orange|yellow|green|cyan|blue|purple|magenta|#rrggbb, at most 8]} (with a key: "
        "the key's colour taken off the edge of what stands in front - fur, hair, a sleeve carrying the screen's "
        "green - width_px past it and fading inside it, the insert's light wrapped onto it, the subject's own colours "
        "read over the shot at save kept; a halo finding left after it says its width: raise width_px before "
        "generating the shot again; one no setting moved says 'not a setting: the foreground's own colour (magenta)', naming its hue: "
        "list that colour in own_hues, or accept it; edge false draws that edge as the footage shows it), panes? (a wall of screens on the one quad: N side by side or [rows, cols], "
        "with sources [one per pane, row by row] instead of source, gutter? 0-0.1 of the quad, default 0.015), "
        "legibility? (legible, the default: the save checks its text is drawn large enough to read | ambient: "
        "scenery: wall monitors, a screen under a statement - its text size said, never judged), "
        "start, duration, at_step?} - the real take, shot or picture mapped in perspective onto the monitor, "
        "billboard or poster the video model drew, frame by frame and legible at the render's resolution; the quad "
        "is snapped to the bezel the footage shows and the surface painted flat under the insert; the render and "
        "render_video_scene fail an insert drawn over the foreground or seen through it (the finding names its "
        "cause), a foreground the mask is unsure of on over 5% of frames (dark on dark: key the screen), "
        "a keyed screen's colour spilled past it on the footage (a floor reflecting it), a picture with a card or letterbox baked in, a "
        "brand mark that stops showing, an AI shot showing its composited logo twice, a quad more than 0.5% of the "
        "frame's width off its bezel, the key left in or around the surface, a matte that flickers, an insert drawn "
        "soft, a halo on the foreground's edge and an insert whose colours the compositing changed (insert colour "
        "shift: its blacks lifted past 5% or its hue turned - a cast on a grey source - past a few degrees against its "
        "source: lower its light when the footage's light lifts its blacks), each with its frame and one fix, and "
        "attach 100% crops of each insert's region "
        "with its colours at its light and the resolution its numbers come from; a quad may keep its true corners "
        "outside the frame (the insert shows through the visible part - "
        "never squeeze it to the frame's edge); a curved surface (an edge bowing by more than 5% on its visible part, "
        "its points confirming it; an edge mostly behind what stands in front is not judged) is a save-time "
        "finding only a shot made again with a flat screen fixes - a flat one seen at an angle is drawn in its "
        "perspective, never a finding -, and an insert running past its clip's loop seam (extend_mode loop) says "
        "where the seam falls]): "
        "title {title, subtitle?, kicker?, duration, style aurora|minimal|spotlight|split}; "
        "statement {lines [1-4 short lines, revealed one after the other], kicker?, figure? (a number counted up: "
        "'6,000+'), secondary? [smaller lines: a proof point, a translation], chips? [pill labels], reveal "
        "lines|words|together, align center|left, duration, show_logo?, backdrop? {clip_id (an AI shot), file_id "
        "(an uploaded video or image) or take_id + at_step|at_seconds (a frame of a recording: the save keeps it as "
        "the picture, no save_video_frame needed), trim_start?}, dim (0-0.9, how dark the backdrop is under the text)}; "
        "recording {take_id, trim_start?, trim_end?, end_hold_ms? (ms of the take kept after its last step; unset, all "
        "the recorder held), speed?, fit? auto (the default: a recording its narration "
        "outruns is slowed by the save to last as long, never below 0.5; in a dynamic video one longer than its "
        "narration is sped up to end with it, never above 1.25)|off, idle {mode off|compress|cut}, camera {mode "
        "auto|static|manual (a demo keeps it static - the Filigran Product Video default - and moves it only to a "
        "detail unreadable at full frame, max_zoom 1.3 at most, one move a scene), max_zoom, pace calm (the default: "
        "smooth moves)|dynamic (quicker), move_seconds? (a zoom in's length, 0.5-3)}, cursor {visible, size, style, "
        "click_effect}, frame {style window|none, url_bar, fill? (true: "
        "drawn edge to edge - no margin, bar, corner or halo - for a scene whose UI must be read; a template locking "
        "the frame leaves it open)}, "
        "highlights [{at_step, duration}], extend_mode? zoom (the default: a recording shorter than its narration "
        "gives back its compressed pauses, then drifts in slowly)|hold (keep the pauses, freeze its last frame)}; slide {presentation_id, slide (1-based), duration, motion, chrome? (false: "
        "without the deck's footer, logo and slide number)}; figure {ONE block drawn alone on the template's "
        "palette and built in step by step - an architecture's zones and nodes, a process flow's steps, a timeline's "
        "items, a roadmap's phases, a chart's series, a diagram's nodes appear in order, edges draw, series grow; "
        "an org chart level by level, the root first, its bubbles growing to size, its numbers counting up, the "
        "dotted lines last: "
        "presentation_id + slide (or slide_id) + block (0-based index among the slide's blocks; a roadmap slide is "
        "its phases), OR block_spec (a block written as in a presentation spec, e.g. {type: process_flow, steps: "
        "[...]}), duration, build sequence|together, reveal even|narration (each step comes in when the narration "
        "says its words), dwell? (seconds a step stays on screen before the next, default 0.8), camera follow "
        "(moves to each step as it comes in, ends on the whole figure)|still, motion? drift (the default: once built "
        "it drifts in slowly, pulses run along its edges, its core node glows)|none (holds still), perspective? flat|"
        "tilt (leans back, turning towards the camera as it builds)|orbit (turns slowly side to side)}; clip {clip_id "
        "(an AI shot) or file_id (an uploaded video, e.g. an extract of an event film: trim_start / trim_end cut it), "
        "trim_start?, trim_end?, fit? cover (the default: fills the frame, what overflows is cut)|contain (the whole "
        "clip as a framed card), audio? (true keeps its own sound, false drops it; unset, clip_sound decides), "
        "volume? (its level against the voice, default 0.8), extend_mode? zoom (the default: a clip shorter than its "
        "narration plays slower to fill it, then drifts in slowly)|loop (at its speed, then its last second again "
        "and again, crossfaded)|hold (freeze its last frame; the save check refuses a render on one frozen over 0.5 s), "
        "motion? intended (the shot's camera move is wanted: never a camera-moved finding), consistency? [reference "
        "ids] (the video's references this shot must match; unknown ids are refused)}; image {file_id (an upload, a template asset - SVG "
        "included - or a frame from save_video_frame) or take_id + at_step|at_seconds (a frame of a recording, kept as "
        "the picture by the save), duration, fit? contain (the default)|cover, motion "
        "ken_burns|zoom_in|zoom_out|pan_left|pan_right|none, animate? (true, or {prompt: how it moves}: the video "
        "model makes a silent shot FROM the image - a screenshot, a photo - and the scene plays it once made, the "
        "still until then or when the shot redrew the image's text wrong), extend_mode? zoom|loop|hold (how the scene "
        "plays once that shot ended, as a clip's)}; outro {title, subtitle?, kicker?, cta?, url?, "
        "duration}; motion {motion graphics in the video's motion: composition? kinetic_title (title, kicker?, "
        "subtitle?)|logo_reveal (the template's logo, title as its tagline)|stat_counter (figure counted up, label?)|"
        "list_build (items [1-6], title?)|split_statement (lines [2-4])|quote (title as the quote, subtitle as who said "
        "it)|constellation (title over connected particles), show_logo?, align center|left, background theme|solid, "
        "duration, elements? [up to 40, drawn over the composition, last on top: {kind text|shape|path|image|lottie|"
        "particles, id?, x, y (0-1 of the frame), anchor center|top_left|top|...|bottom_right, width?, height? (0-1), "
        "below? (an earlier element's id: placed under it as laid out), gap?, z (depth under the camera), start, end?, "
        "opacity, scale, rotate, blur, enter? {preset fade|rise|drop|slide_left|slide_right|scale|pop|blur|track|"
        "mask_up|mask_down|mask_left|mask_right|iris|draw|flip|typewriter, at (seconds into the scene), duration?, "
        "ease?, stagger?, order start|center|end|random}, exit? {preset, at?, duration?}, loop? {preset float|pulse|"
        "breathe|spin|sway|shimmer, period, amount}, keys? [{t, x?, y?, scale?, rotate?, rotate_x?, rotate_y?, z?, "
        "opacity?, blur?, reveal?, draw?, morph?, progress?, ease?}] (ease: in|out|in_out|out_back|out_expo|"
        "in_out_expo|out_quint|snap|spring|spring_soft|spring_bouncy|enter|exit|move|emphasis, cubic-bezier(x1, y1, x2, "
        "y2) or spring(stiffness, damping)), path? {d (SVG path data in 0-1000 frame units), start, end, orient?}, "
        "mask? {shape rect|circle, side, feather}; text: text, role headline|title|body|kicker|label|figure, size?, "
        "weight?, color? (a theme role: foreground|muted|accent|accent_2|gradient|...), split? none|lines|words|"
        "letters, max_width?, count? (its number counted up), marker? dot|check|dash; shape: shape rect|ellipse|line|"
        "ring|polygon|star, fill?, stroke?, stroke_width?, radius?; path: d, view_box [w, h], morph_to?, fill?, "
        "stroke?; image: file_id or asset logo|logo_light|logo_mark, fit?; lottie: file_id (a Lottie JSON: its "
        "pictures embedded, its text as glyphs), speed?, loop_playback?; particles: number, seed?, particle dot|"
        "square|plus|ring, size?, drift?, twinkle?, connect? (0-0.4: lines between near ones)}], camera? {perspective, "
        "keys [{t, x, y, zoom, rotate_x, rotate_y, rotate, ease?}]}}. In an edit, setting one source of an object (a "
        "backdrop's clip_id, the music's track) drops the other. In a "
        "statement, title or outro text, *words* between asterisks are drawn in the accent colour and \\n starts a "
        "new line. The camera zooms "
        "toward clicked and typed elements by itself; narration drives the pacing. An AI shot that is still being "
        "generated or that failed can already be placed: the preview shows its state, and a render refuses it "
        "until it is ready (an animated image plays its still meanwhile)."
    )
    return [
        ToolDefinition(
            name="list_browser_profiles",
            description=(
                "List the browser profiles (saved sign-ins) you can use, with the other apps each signs in. The "
                "session status is the last probe of start_url in the saved session, never the cookies: signed in, "
                "expired (with the URL it landed on), not checked yet, or waiting for MFA (with the challenge_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Only profiles covering this URL's host (start URL or apps).",
                    }
                },
            },
            source=_SOURCE,
            live_state=True,
        ),
        ToolDefinition(
            name="save_browser_profile",
            description=(
                "Create or update a browser profile: a demo environment to record (start URL, optional login URL) and "
                "how its sign-in form is read. Its credentials - username, password and the TOTP secret of an "
                "authenticator app - are never given here: the person types them on the Browser profiles page "
                "(Tools & Integrations > Browser profiles), stored encrypted and never shown again. allowed_hosts "
                "lists extra private-network hosts the recording may reach (honoured only when an administrator "
                "allows private targets)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "profile_id": {"type": "string", "description": "Update this profile; omit to create one."},
                    "name": {"type": "string"},
                    "start_url": {"type": "string", "description": "Where recordings start (the app's home page)."},
                    "login_url": {"type": "string"},
                    "allowed_hosts": {"type": "array", "items": {"type": "string"}},
                    "apps": {
                        "type": "array",
                        "description": "Other apps the session signs in (same SSO); [] clears.",
                        "items": {
                            "type": "object",
                            "properties": {"name": {"type": "string"}, "url": {"type": "string"}},
                            "required": ["url"],
                        },
                    },
                    "login_selectors": {
                        "type": "object",
                        "description": (
                            "Optional selectors when the sign-in form is unusual: username_selector, password_selector, "
                            "submit_selector, otp_selector, otp_submit_selector, confirm_selector (a button the "
                            "sign-in clicks when it shows, e.g. the application's 'sign in with SSO'), "
                            "signed_in_selector (an element only the signed-in application shows: the surest "
                            "session check), signed_in_url_pattern (a regex the signed-in URL matches), "
                            "signed_out_url_pattern, keep_storage_keys (a list: the localStorage / sessionStorage "
                            "keys the application keeps its sign-in in, which a recording's page state reset keeps; "
                            "default none)."
                        ),
                    },
                    "color_scheme": {"type": "string", "enum": ["light", "dark"]},
                    "locale": {"type": "string"},
                    "timezone": {"type": "string"},
                    "keepalive_minutes": {
                        "type": "integer",
                        "description": (
                            "Probe and refresh the saved session this often, in minutes (0 = never, 30, 60, 120, 240, "
                            "480, 720, 1440; default 240 on a new profile)."
                        ),
                    },
                    "sign_in_timeout_seconds": {
                        "type": "integer",
                        "description": "How long the sign-in waits without a navigation before it fails (30-600, default 120).",
                    },
                    "session_save_timeout_seconds": {
                        "type": "integer",
                        "description": "How long reading and saving the session may take (5-120, default 30).",
                    },
                    "mfa_timeout_seconds": {
                        "type": "integer",
                        "description": "How long an MFA challenge keeps its browser open for the user (60-900, default 300).",
                    },
                    "description": {"type": "string"},
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="sign_in_browser_profile",
            description=(
                "Sign a browser profile in, in the platform's own browser, and save its session. It drives a "
                "first-party form or a hosted identity provider (SAML / OIDC redirect) alike, and is done only "
                "when the browser is back on the application and start_url (or the profile's signed_in_selector / "
                "signed_in_url_pattern) renders signed in; then the cookies of every site visited and the "
                "application's storage are saved and probed again. With a TOTP secret the MFA code is computed (a "
                "refused one retried once). Without one, an MFA prompt returns status 'mfa_required' with a "
                "challenge_id, a kind and a screenshot: kind 'code' -> ask the user for the code and call "
                "submit_browser_mfa_code; kind 'push_number' / 'push' -> tell the user to approve in the "
                "authenticator app (enter the number shown) - the sign-in goes on by itself. The prompt stays open "
                "for the profile's MFA window (default 5 min). The user signing in in their own tab never signs the "
                "profile in. A failure names its stage (idp_form, mfa, redirect_chain, app_load, "
                "session_save) and the last URL. Called again while a challenge waits, it returns that challenge; "
                "restart=true (or a login setting changed since) closes it and starts again."
            ),
            input_schema={
                "type": "object",
                "properties": {"profile_id": {"type": "string"}, "restart": {"type": "boolean"}},
                "required": ["profile_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="submit_browser_mfa_code",
            description=(
                "Type the one-time code the user read from their authenticator into the sign-in waiting on "
                "challenge_id (an mfa_required challenge of kind 'code'): the platform's browser is still open on "
                "the prompt, in the profile's own context. Returns the sign-in's next state - signed in, the code "
                "refused (a new challenge message: ask for a fresh code), an approval to make, or the failure with "
                "its stage."
            ),
            input_schema={
                "type": "object",
                "properties": {"challenge_id": {"type": "string"}, "code": {"type": "string"}},
                "required": ["challenge_id", "code"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="test_browser_profile_sign_in",
            description=(
                "Dry-run the whole sign-in once WITHOUT saving anything (the live session is untouched), to see "
                "where it stalls; one that would need a person stops there. Returns the result, a screenshot of "
                "where it ended and a step trace (password_submitted, totp_submitted, ...). Use it after "
                "save_browser_profile to prove a profile signs in."
            ),
            input_schema={
                "type": "object",
                "properties": {"profile_id": {"type": "string"}},
                "required": ["profile_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="check_browser_profile_session",
            description=(
                "Pre-flight before record_browser_demo: open start_url in the saved session and tell whether it "
                "renders the signed-in application (each declared app too, under apps); an expired profile that "
                "renews by itself signs in again here. Returns and stores a session_check. Not authenticated and "
                "cannot renew alone: sign_in_browser_profile first. http_status set: the application did not answer."
            ),
            input_schema={
                "type": "object",
                "properties": {"profile_id": {"type": "string"}},
                "required": ["profile_id"],
            },
            source=_SOURCE,
            live_state=True,
        ),
        ToolDefinition(
            name="snapshot_browser_profile_page",
            description=(
                "Look at a page of the signed-in application BEFORE scripting a recording: opens url (the "
                "profile's start_url by default) in the profile's saved session - signed in again first when the "
                "profile can do it alone, like record_browser_demo's pre-flight -, in the clean page state a "
                "recording opens in (reset_state, default true: what the takes before left in its storage cleared), "
                "waits for it to settle, and "
                "returns how long it took to settle once it opened (what a wait after the step that opens it "
                "holds), its screenshot and its interactive elements: role, accessible name and the selector a "
                "step uses (a step with role + name matches these names exactly, so 'Flows' is written as the menu "
                "really reads, e.g. 'Agentic Flow'). Nothing is typed, no step is run."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "profile_id": {"type": "string"},
                    "url": {
                        "type": "string",
                        "description": "A page of the application (the profile's allowed hosts); start_url by default.",
                    },
                    "reset_state": _reset_state_schema(
                        "The page state it opens in, as record_browser_demo's reset_state (default true): give the "
                        "recording's own so the names are those its steps find."
                    ),
                },
                "required": ["profile_id"],
            },
            source=_SOURCE,
            live_state=True,
        ),
        ToolDefinition(
            name="delete_browser_profile",
            description=(
                "Delete a browser profile you manage (its recordings are kept); confirm=true when it is shared or "
                "recordings were made with it."
            ),
            input_schema={
                "type": "object",
                "properties": {"profile_id": {"type": "string"}, "confirm": {"type": "boolean"}},
                "required": ["profile_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="record_browser_demo",
            description=(
                "Record a scripted demo in the platform's browser at high resolution (1600x900 at 2x by default; "
                "for a screen insert, target_render with its video_id, scene and insert sizes the window and pixel "
                "ratio for the insert's saved quad at that render quality, keeping the page's layout; a save refuses "
                "a recording too soft for its quad at the quality the video is made for - ultra before its first "
                "render - naming the window and pixel ratio to record it at). "
                "Steps run with human pacing (the cursor glides, typing has a natural rhythm, scrolling is smooth); the "
                "action log of every step (its element's box, the cursor path) drives the automatic camera, cursor "
                "and callouts at production. With profile_id the session is checked before step 1 (the "
                "check_browser_profile_session probe): when start_url does not render the signed-in application, a "
                "profile that can sign in by itself (credentials and a TOTP secret, or a saved identity-provider "
                "session) is signed in again and checked once more; otherwise the take fails with 'profile not "
                "authenticated: landed on' and the URL it landed on, and no video - sign_in_browser_profile, then "
                "record again. Once the session is checked, the take opens start_url exactly as given in a clean page "
                "state (reset_state, default true): its origin's localStorage and sessionStorage cleared but the "
                "profile's keep_storage_keys, cookies never - what a graph's layout or a list's filters left from the "
                "takes before; the action log's first line says what was cleared and the URL it opened on. Script it "
                "from snapshot_browser_profile_page (the page's real names, in the same state). A step that "
                "does not find its element is reported with the three closest names the page offered then, the page's "
                "URL (its query included), title and how many elements of the step's role it showed, and where an "
                "earlier take of the profile found it ('found on take X at URL Y; this take is at URL Z: the page is "
                "in another state'); the contact sheet keeps the frame where it failed. A role "
                "step also matches its role's family (menuitem: menuitemcheckbox, menuitemradio), its own role first. "
                "A pointer step (click, double_click, right_click, hover, drag, wheel) may act on a canvas graph: at a "
                "point (at {x, y, in?}) or on a node (node {label}: found by its label, nothing needed from the "
                "application); the cursor rests on it 250 ms first, a right_click logs the "
                "menu it opened (on a node, a menu headed with something else fails the step there), modifiers hold "
                "keys (Control + click); a step on an entry of a graph's "
                "screen-reader-only list (role=option mirroring a node) is refused naming the node step to use. "
                "Before step 1 the worker's capture rate is "
                "measured at the take's size: short of its fps beside other jobs on its worker, it waits in the "
                "recording lane and starts by itself once their load allows it (the answer says its place and what it "
                "waits behind); on an idle worker, one it cannot sustain records at pixel ratio 1.5 (said), else is "
                "refused naming the largest window and pixel ratio measured sustaining its fps on this worker and, "
                "for fps 60, fps 30 at its size - or records on one of them with fps_fallback; the answer gives the "
                "frames kept each second and any 'low capture rate "
                "at step N'. Which GL a worker shape draws a page's canvases and WebGL with: a GPU worker (its "
                "Chromium finds a GPU) draws them on it, captured with their page at the take's rate; a CPU worker "
                "draws them on SwiftShader and also captures each canvas alone at reduced quality, composited on the "
                "page's last full frame where the screencast fell short of a transition and the copy matches what the "
                "browser painted - the answer's capture line says which ('canvases drawn with GPU GL' or 'software "
                "GL'), and a step whose motion was mostly synthesised from keyframes says 'stepping motion', a save "
                "finding on every scene showing it: dense where it steps, advice where a brief transition still reads "
                "as motion. A target_render take painting under 15 fps is recorded once more, then failed naming the "
                "window and pixel ratio that sustain it. On a deployment with no GPU worker, a 60 fps take of a page drawing canvases steps on "
                "their transitions: record it at fps 30 (or fps_fallback: fps); its answer says so. A wait after a click "
                "that opened a page or switched a tab holds until the page settled "
                "(until_settled); the answer flags a wait, pause or highlight that holds on a page still loading (a "
                "progress indicator, an empty canvas): 'step 5 holds on a loading page for 2.1 s' - record it again "
                "before placing it. "
                "A script estimated past 90% of the recording's time limit is refused before launch, naming the step "
                "that crosses it and where to split it; a take that still reaches the limit is kept, ready (truncated "
                "at step N). A step typing into a password field or typing a stored credential is refused: the "
                "profile signs in by itself. redact "
                "blurs elements in every frame, hide removes them (cookie banners). Waits for the recording; "
                "returns its action log and a contact sheet to check. from_recording_id records a NEW take from a "
                "recording's script (its steps and parameters as submitted, get_video_recording's script block): "
                "every argument given overrides what it copies (an explicit viewport, pixel_ratio or browser_zoom "
                "replaces a copied target_render sizing); the recording it copies is untouched."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "profile_id": {"type": "string"},
                    "start_url": {"type": "string"},
                    "steps": {"type": "array", "items": _STEP_SCHEMA, "description": "1 to 200 steps."},
                    "viewport": {
                        "type": "object",
                        "properties": {"width": {"type": "integer"}, "height": {"type": "integer"}},
                    },
                    "pixel_ratio": {
                        "type": "number",
                        "enum": [1, 1.5, 2],
                        "description": (
                            "Device pixels per CSS pixel: 2 (the default) is sharp under zoom and on a screen insert "
                            "of a 4K render, 1.5 the balance for a 1080p video, 1 the smoothest capture. A recording "
                            "too small for its quad fails the render as a soft insert, naming the ratio it needs."
                        ),
                    },
                    "browser_zoom": {
                        "type": "integer",
                        "minimum": 100,
                        "maximum": 500,
                        "description": (
                            "The page zoomed in, in percent, as a browser zoom lays it out: its text larger for the "
                            "width of the frame, the window and frame keeping their size, the page laid out that many "
                            "times narrower - for a recording drawn on a small screen insert (the save names the zoom "
                            "its text needs when its page keeps its layout that narrow). Default 100."
                        ),
                    },
                    "redact": {"type": "array", "items": {"type": "string"}},
                    "hide": {"type": "array", "items": {"type": "string"}},
                    "pace": {"type": "string", "enum": ["fast", "natural", "slow"]},
                    "color_scheme": {"type": "string", "enum": ["light", "dark"]},
                    "local_storage": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": (
                            "localStorage keys of the start URL's origin (at most 20), set before its scripts run and "
                            "kept by the page reset: an app's own theme, e.g. the Copilot web app light: "
                            '{"copilot-theme": "light"}.'
                        ),
                    },
                    "settle_ms": {"type": "integer"},
                    "end_hold_ms": {"type": "integer"},
                    "record_sign_in": {"type": "boolean"},
                    "fps": {
                        "type": "integer",
                        "enum": list(PACED_RATES),
                        "description": (
                            "The rate the recording's motion plays at (default 30; 60 for a scene rendered at 60 fps, "
                            "which target_render sets from the video): a scroll step turns the wheel once a frame at "
                            "this rate, and a scroll the browser painted at fewer frames a second, or unevenly, is drawn "
                            "again at this rate from the frames it painted."
                        ),
                    },
                    "fps_fallback": {
                        "type": "string",
                        "enum": ["size", "fps", "refuse"],
                        "description": (
                            "When the worker cannot capture the take's size at its fps (default refuse): size records "
                            "at the largest window sustaining it, fps at fps 30; the action log says which."
                        ),
                    },
                    "reset_state": _reset_state_schema(
                        "The page state the take starts from (default true): once the session is checked, start_url "
                        "opens exactly as given with its origin's localStorage and sessionStorage cleared (the "
                        "profile's keep_storage_keys kept, cookies never cleared); false opens it in the state the "
                        "saved session carried from the takes before."
                    ),
                    "target_render": {
                        "type": "string",
                        "enum": list(RENDER_QUALITIES),
                        "description": (
                            "Render quality of the screen insert (with video_id and scene) the take is sized for: the "
                            "smallest pixel ratio from the one given passing the soft check on its quad, else a wider "
                            "window zoomed in so the layout holds (a save's 'soft at ultra' advice)."
                        ),
                    },
                    "video_id": {"type": "string", "description": "With target_render: the video whose insert it is."},
                    "from_recording_id": {
                        "type": "string",
                        "description": (
                            "Record again from this recording's script (id or 8+ hex prefix), every argument given "
                            "overriding it."
                        ),
                    },
                    "worker_class": {
                        "type": "string",
                        "description": (
                            "GPU class to record on: l4, v100s, cpu (default any). Waits while busy; a class no "
                            "worker serves is refused unpaid."
                        ),
                    },
                    "scene": {
                        "type": "string",
                        "description": "With target_render: the scene's id, or its 0-based index in the spec as written.",
                    },
                    "insert": {
                        "type": "integer",
                        "description": (
                            "With target_render: the screen insert's index among the scene's overlays (its screen "
                            "insert N); needed when the scene has several."
                        ),
                    },
                },
            },
            source=_SOURCE,
            # same_take answers the recording an identical call queued: a repeat after a resume never records twice.
            replay_safe=True,
        ),
        ToolDefinition(
            name="get_video_recording",
            description=(
                "A recording's status, its action log (step, action, label, time) and, optionally, its contact sheet. "
                "repair: true makes a NEW recording from a ready one without recording again (its scrolls and "
                "transitions paced again - a canvas zoom drawn from the nearer keyframe, never two blended -, its "
                "canvases composited, its capture measured, its off-scale frames dropped): exact from its kept "
                "keyframes, approximate from its MP4 for one recorded before; the "
                "answer names what it repaired and the residual to record again. The recording repaired is kept. "
                "include_script (default true): the steps and parameters it was submitted with, what "
                "record_browser_demo(from_recording_id) records again."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "recording_id": {
                        "type": "string",
                        "description": (
                            "The recording's id, or the first 8 or more characters of one of your recordings' ids "
                            "(an ambiguous prefix is refused naming the candidates)."
                        ),
                    },
                    "include_contact_sheet": {"type": "boolean"},
                    "repair": {"type": "boolean"},
                    "window": {
                        "type": "string",
                        "description": "'30.5-32': add its raw capture records over those seconds.",
                    },
                    "include_script": {
                        "type": "boolean",
                        "description": "false keeps the answer short.",
                    },
                },
                "required": ["recording_id"],
            },
            source=_SOURCE,
            live_state=True,
        ),
        ToolDefinition(
            name="list_video_recordings",
            description=(
                "Your recordings, newest first, up to 50 a call; the answer says when more remain and the offset "
                "that reads them. name_contains keeps those whose name holds the text; video_id lists the "
                "recordings a video places instead of yours."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "At most 50 (default 20)."},
                    "offset": {"type": "integer", "description": "Recordings to skip, newest first (default 0)."},
                    "name_contains": {"type": "string", "description": "Case-insensitive text in the name."},
                    "video_id": {"type": "string", "description": "Only the recordings this video's scenes place."},
                },
            },
            source=_SOURCE,
            live_state=True,
        ),
        ToolDefinition(
            name="video_studio_capabilities",
            description=(
                "The video templates you may use (the default first: what each is made for - product demo, brand "
                "film, event, launch, explainer, social -, what it presets, adds and locks, the art direction of its "
                "AI shots (shots: the look and what they avoid) and its guidance; "
                "get_template lists a template's assets, logos and pictures to place as image scenes or statement "
                "backdrops), the speech models you may narrate with (the default first) and the voices of the "
                "default one - or of the speech model you name -, the AI video clip models (each with how many "
                "reference_images it takes and whether with a start frame, its reference_constraints - the aspect "
                "ratio range, sides, formats and count of a reference image or ingredient, or unverified - and "
                "cooling when its provider refused "
                "for its rate limit, or that model for its quota - a quota is the model's, so another model of the "
                "same provider may take the shot: until when shots asked of it wait in the queue; only the models "
                "their provider serves - one it answered HTTP 404 for is under ai_shots.not_served, with why), the built-in music "
                "tracks (music_library: mood, genre, tempo, the kinds of video each suits) and the music models "
                "(music_models, ai_music), the aspect ratios and the render qualities (render_quality: each one's "
                "output, the pages the worker draws it on, its memory_mb ceiling, the hours it may run "
                "and max_duration_s, the longest video it renders within them)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "speech_provider_id": {
                        "type": "string",
                        "description": "List the voices of this speech provider (a provider_id from speech_models).",
                    },
                    "speech_model": {"type": "string", "description": "List the voices of this speech model."},
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="generate_video_clip",
            description=(
                "Generate a fully synthetic, animated AI video shot (4-12 s) with a video model from a prompt "
                "(subject, action, camera move, framing; not a style), optionally starting from a real frame: an "
                "image (reference_file_id), a frame of a recording (reference_recording_id at reference_at_step), the "
                "end of another shot or a video file (reference_clip_id); look at it first with preview_start_frame. "
                "Uses the platform's video model unless you name another from video_studio_capabilities; a shot is "
                "never moved to another model by itself. Give every shot meant for a video its video_id (template_id "
                "before the video exists): the template's art direction is added; freestyle: true for none. Runs for "
                "minutes in the background; ask several shots at once. With clip_id, a failed or ready shot is "
                "generated again in place (what you leave out stays as asked); a ready one keeps playing its take "
                "until the new take passes its checks (a failed regeneration answers previous_take_plays: true), "
                "discard: true drops it at once. MODES (the answer's mode): start frame only (start_frame, for "
                "continuity), reference images only (reference_images: reference_image_file_ids, for consistency), "
                "or both (start_frame_and_reference_images, on a model whose reference_images_with_start_frame is "
                "true). On a retry: reference_image_file_ids alone switches to reference images, start_frame: false "
                "drops the start frame, start_frame: true with a reference_* keeps the start frame alone. "
                "validate_only: true runs every check and answers the resolved request (mode, provider_state) "
                "without queueing or paying: check a batch with it first. A cooling or busy provider is waited for "
                "in the queue (status queued), never failed: never ask again for a shot that waits. Every take is "
                "checked on its first, middle and last frames (text drift, content drift, reference drift, camera "
                "moved, screen or logo lost) as its arguments say; a failed take keeps its video to look at. The "
                "Video Production skill (section 4, AI shots) explains ingredients, screen replacement, "
                "consistency, the checks and their thresholds. The answer opens with {clip_id, status: "
                "ready|generating|failed, waited_seconds, completion: inline|background, reason, "
                "start_frame_file_id, first_frame_file_id, mid_frame_file_id, last_frame_file_id, frame_qc, "
                "stabilisation, logo_check, logo_tracked, text_check, consistency?, mode, model, fallback?}. A call "
                "refused on its arguments opens with {status: invalid_parameters, clip_id, reason, queued: false}: "
                "nothing was queued or paid - change what the reason names and call again."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "clip_id": {
                        "type": "string",
                        "description": (
                            "An AI shot (id or 8+ hex prefix) to generate again in place, failed or ready "
                            "(prompt, model, settings, "
                            "reference images and start frame optional: what you leave out stays as it was asked). "
                            "A ready one keeps playing its take until the new one is ready and passes its checks."
                        ),
                    },
                    "discard": {
                        "type": "boolean",
                        "description": (
                            "With clip_id: true drops the shot's ready take now instead of keeping it playing until "
                            "the new one is ready (a failed regeneration then leaves the shot failed)."
                        ),
                    },
                    "start_frame": {
                        "type": "boolean",
                        "description": (
                            "With clip_id, the mode of the shot generated again. false: reference images only (drops "
                            "the start frame; its file stays in the conversation). true with a reference_* and no "
                            "reference_image_file_ids: start frame only (drops the kept reference images). true with "
                            "reference_image_file_ids: both (refused by a model whose "
                            "reference_images_with_start_frame is false). Unset, the start frame is kept - unless "
                            "reference_image_file_ids are given for a model that takes no start frame beside them: "
                            "then it is dropped and the answer says so."
                        ),
                    },
                    "reference_fit": {
                        "type": "string",
                        "enum": ["refuse", "pad"],
                        "description": (
                            "What happens to a reference image or ingredient outside the shape the model takes "
                            "(video_studio_capabilities: reference_constraints). refuse (default): the call is "
                            "refused before anything is paid, naming the picture, its shape and the accepted range. "
                            "pad: it is padded on transparent to the nearest accepted shape, every pixel kept, and "
                            "the answer says so. An SVG is rasterised to PNG before it is checked and sent."
                        ),
                    },
                    "validate_only": {
                        "type": "boolean",
                        "description": (
                            "true runs every check (model, reference images and their shape for the model, start "
                            "frame, its shape) and answers "
                            "the resolved request with its mode and its provider's state (provider_state: cooling, "
                            "retry after ...): nothing is queued, locked or paid."
                        ),
                    },
                    "sound": {
                        "type": "boolean",
                        "description": "false: a silent shot (the model is asked for none, any track it makes is dropped).",
                    },
                    "text_check": {
                        "type": "boolean",
                        "description": (
                            "false skips the text check of a shot made from a picture: only for a shot meant to "
                            "change the picture's words."
                        ),
                    },
                    "content_check": {
                        "type": "boolean",
                        "description": (
                            "false skips the check of the subjects the prompt names against the shot's frames: "
                            "only for a shot meant to transform them. A negative term the start picture already "
                            "shows is never failed on the first frame, only where a later frame shows more of it "
                            "(validate_only names them: start_shows_excluded). A start picture failing the subject "
                            "check is refused unpaid."
                        ),
                    },
                    "accept_minor_drift": {
                        "type": "boolean",
                        "description": (
                            "Default true: a content drift seen only up close (a detail nobody notices at 1080p) is "
                            "kept with a note. false fails the shot on it too (a close-up where every detail shows)."
                        ),
                    },
                    "screen": {
                        "type": "object",
                        "description": (
                            "Screen replacement: the real screenshot drawn into the screen of the start picture "
                            "(reference_file_id, of the shot's aspect ratio), so the screen's text is exact and only "
                            "the room, light and reflections are generated. The screen is carried to its bezel and "
                            "every frame is measured: a shot whose model did not keep the screen flat fails as "
                            "'screen lost', one with the key colour left around the screen as 'key spill', one whose "
                            "matte jitters as 'key flicker'. With clip_id, replaces the shot's own."
                        ),
                        "properties": {
                            "file_id": {"type": "string", "description": "The screenshot (a picture you can read)."},
                            "quad": {
                                "type": "array",
                                "minItems": 4,
                                "maxItems": 4,
                                "items": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2},
                                "description": (
                                    "The screen's four corners [x, y] in the start picture's pixels: top left, top "
                                    "right, bottom right, bottom left. Leave it out when the start picture's screen "
                                    "is one solid green, blue or magenta surface: it is found there (validate_only "
                                    "shows it)."
                                ),
                            },
                            "fit": {
                                "type": "string",
                                "enum": ["cover", "contain", "stretch"],
                                "description": (
                                    "How the screenshot fills the screen. contain (the default) keeps its content "
                                    "whole, letterboxed in the screenshot's own background colour; cover fills the "
                                    "screen and is refused when it would cut the content (a sidebar, a column)."
                                ),
                            },
                            "light": {
                                "type": "number",
                                "minimum": 0,
                                "maximum": 1,
                                "description": "How much of the shot's light and reflections play over it (default 0.6).",
                            },
                        },
                        "required": ["file_id"],
                    },
                    "reference_image_file_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 3,
                        "description": (
                            "1-3 pictures (conversation files or template assets) whose subjects the shot keeps: "
                            "the same character, animal, garment, product or logo in every shot. Only for a model "
                            "with reference_images in video_studio_capabilities; with clip_id, replaces the shot's "
                            "own ([] drops them, like start_frame: true with a reference_*) and, for a model whose "
                            "reference_images_with_start_frame is false, drops its start frame unless start_frame "
                            "says otherwise. Never with ingredients."
                        ),
                    },
                    "ingredients": {
                        "type": "array",
                        "maxItems": 3,
                        "items": {
                            "type": "object",
                            "properties": {
                                "file_id": {
                                    "type": "string",
                                    "description": "The picture: a conversation file, an upload or a template asset.",
                                },
                                "role": {"type": "string", "enum": ["logo", "product", "character"]},
                                "label": {
                                    "type": "string",
                                    "description": (
                                        "What to call it in the prompt when the model takes no reference image "
                                        "('the Filigran logo'); 'the logo', 'the product' or 'the character' by "
                                        "default."
                                    ),
                                },
                                "quad": {
                                    "type": "array",
                                    "minItems": 4,
                                    "maxItems": 4,
                                    "items": {
                                        "type": "array",
                                        "items": {"type": "number"},
                                        "minItems": 2,
                                        "maxItems": 2,
                                    },
                                    "description": (
                                        "A logo only: its four corners in the start frame, 0-1 across and down (top "
                                        "left, top right, bottom right, bottom left), where it is drawn back from. "
                                        "Leave it out to have it found there."
                                    ),
                                },
                                "subject": {
                                    "type": "object",
                                    "properties": {
                                        "file_id": {
                                            "type": "string",
                                            "description": (
                                                "A picture of the subject wearing the logo: a conversation file, "
                                                "or a frame saved from another shot."
                                            ),
                                        },
                                        "label": {
                                            "type": "string",
                                            "description": "What to call the subject ('the cow in the navy blanket').",
                                        },
                                    },
                                    "required": ["file_id"],
                                    "description": (
                                        "A logo only, never with quad: the subject it sits on, for a shot whose start "
                                        "frame does not show it (an establishing shot the branded subject walks into). "
                                        "The subject is found in every frame of the made take, the surface the logo "
                                        "sits on followed, and the logo drawn there with its own pixels, on every "
                                        "instance of the subject (ingredients_path: tracked); works on every model, "
                                        "start frame or not. On a model with reference images the model is sent the "
                                        "logo first and the tracked path draws it only if the take fails the logo "
                                        "check. Fails as 'ingredient subject not found' when the subject is never "
                                        "seen, 'ingredient surface lost at t=...' when its surface cannot be followed "
                                        "past a frame (that frame kept); the answer's logo_tracked says the share of "
                                        "frames the logo was drawn on."
                                    ),
                                },
                            },
                            "required": ["file_id", "role"],
                        },
                        "description": (
                            "Pictures the shot keeps exactly, beside its start frame: sent through the model's own "
                            "reference mechanism, else named in the prompt (then the start frame must show them) - "
                            "a logo then drawn back onto every frame (ingredients_path: composited), or, with its "
                            "subject named, drawn onto that subject wherever the take shows it (ingredients_path: "
                            "tracked, no start frame needed). With clip_id, replaces the shot's own ([] drops them). "
                            "A logo drawn onto the take takes the light and relief of its surface and is measured "
                            "against it (logo_check.integration: shading, follow, size, score): one that reads as a "
                            "flat sticker fails as 'logo not integrated' unless ingredients_quality is 'flat'."
                        ),
                    },
                    "ingredients_quality": {
                        "type": "string",
                        "enum": ["integrated", "flat"],
                        "description": (
                            "Default 'integrated': a logo drawn onto the take (composited or tracked) that reads as "
                            "a flat sticker - lit flat on a shaded surface, moving rigidly or keeping its size while "
                            "the surface bends, moves or grows - fails the take's logo check, every render and the "
                            "video's quality check as 'logo not integrated'. 'flat' accepts it, recorded."
                        ),
                    },
                    "min_reference_similarity": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": (
                            "Fails a take whose first frame's SSIM against its start frame is under it ('drifted "
                            "from reference'), its video kept to generate again in place. Needs a start frame; "
                            "0.5 tolerates a moving camera, 0.7 asks the first frame to stay close. 0 drops it."
                        ),
                    },
                    "max_camera_motion": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": (
                            "How much the take's last frame may change from its first (1 - motion_ssim): a take "
                            "past it fails as 'camera moved', its video kept. A camera move is measured, never "
                            "read off a frame: a negative prompt term about the camera is not checked on the "
                            "frames. 0.3 holds a locked-off shot, 0.5 tolerates a slow push. 1 drops it. A take "
                            "whose camera only drifted is stabilised first (stabilise). At 0.5 or under, a model "
                            "that does not hold a locked camera is replaced, or said, before the take is paid for."
                        ),
                    },
                    "stabilise": {
                        "type": "boolean",
                        "description": (
                            "Lock a drifting camera off once the take is made: each frame warped back onto the first "
                            "and the border cropped, kept only when it moves less (the un-stabilised take stays the "
                            "shot's). Unset: done when max_camera_motion is set or the prompt asks for a locked "
                            "camera. false never; true always (a shot whose camera must not move)."
                        ),
                    },
                    "fallback_models": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 3,
                        "description": (
                            "Leave out: the configured video fallback models are the chain. Set only to narrow or "
                            "reorder them; any other model is refused (invalid_parameters). Tried in order when the "
                            "default fails (provider failure or a failed frame check), each once; a busy or cooling "
                            "model is waited for."
                        ),
                    },
                    "keep_regions": {
                        "type": "array",
                        "maxItems": 4,
                        "items": {"type": "array", "items": {"type": "number"}, "minItems": 4, "maxItems": 4},
                        "description": (
                            "Boxes [x, y, width, height] (shares of the start picture) kept exactly as the picture "
                            "has them: a banner, a sign, a caption the model would redraw. Its pixels are drawn back "
                            "there in every frame, following the shot's camera; a camera that cannot be followed "
                            "fails the take as 'kept region lost'. Needs a start frame; [] drops them."
                        ),
                    },
                    "stray_screens": {
                        "description": (
                            "What a take showing a negative-prompt term ONLY on screens (monitors, laptops, phones) "
                            "gets: 'fail' (default, content drift), 'flatten' (each such screen switched off in every "
                            "frame, following the camera) or {file_id, fit?, light?} (a real screenshot drawn into "
                            "each). A screen something passes in front of is never drawn over: the take fails. Needs "
                            "a negative_prompt and the content check."
                        ),
                    },
                    "accept_attempt": {
                        "type": "string",
                        "description": (
                            "With clip_id: the file_id of a failed take (a failed_attempt, or a failed shot's take) "
                            "to promote after review. Only a take that failed its text check on lettering its screen "
                            "insert covers. Audited."
                        ),
                    },
                    "restore_take": {
                        "type": "string",
                        "description": (
                            "With clip_id: the file_id of a previous take (get_video_jobs previous_takes) to put back "
                            "as the shot's take. Audited."
                        ),
                    },
                    "insert": {
                        "type": "boolean",
                        "description": (
                            "Whether the shot is footage a screen insert will be drawn on (its screen is left out of "
                            "the text check: the insert covers it). Unset: its start "
                            "picture's screen is checked (flat, edges bowing under 5%; seen at an angle is fine) only "
                            "for a screen replacement or a shot a scene already draws a screen_insert on. true checks "
                            "it now; false says the shot carries no insert: no screen is checked on its start picture "
                            "or its take, and stray_screens is dropped. With clip_id, replaces the shot's own."
                        ),
                    },
                    "consistency": {
                        "type": "array",
                        "maxItems": 3,
                        "items": {"type": "string"},
                        "description": (
                            "The ids of the video's references (video_id; its `references`: a crop of a recurring prop "
                            "or character) the shot must match. Their pictures are sent as the model's reference images "
                            "when it takes them beside the start frame, else each is named in the prompt from its label "
                            "and description ('Keep the rug exactly as in the start picture: ...'); the answer says "
                            "which. The take's first, middle and last frames are then read against each reference "
                            "(similarity and dominant colours on the located crop): a take that drifted fails as "
                            "'reference drift', its video kept. Unknown ids are refused. With clip_id, replaces the "
                            "shot's own; [] drops them."
                        ),
                    },
                    "prompt": {"type": "string"},
                    "video_id": {
                        "type": "string",
                        "description": (
                            "The video the shot is for: its template's art direction shapes the shot (with clip_id, "
                            "the shot is generated again under it)."
                        ),
                    },
                    "template_id": {
                        "type": "string",
                        "description": "A video template whose art direction shapes the shot, before its video exists.",
                    },
                    "freestyle": {
                        "type": "boolean",
                        "description": "true: no template's art direction, the prompt alone (a free-style video).",
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Leave out: shots, regenerations included, are made on the configured default video "
                            "model. Only a configured video fallback (usable in video_studio_capabilities) is "
                            "accepted; any other model is refused (invalid_parameters)."
                        ),
                    },
                    "provider_id": {
                        "type": "string",
                        "description": "The provider_id listed with that model (required when several providers offer it).",
                    },
                    "duration_seconds": {"type": "integer"},
                    "aspect_ratio": {"type": "string", "enum": ["16:9", "9:16"]},
                    "resolution": {"type": "string", "enum": ["720p", "1080p"]},
                    "negative_prompt": {"type": "string"},
                    **_START_FRAME_PROPERTIES,
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="preview_start_frame",
            description=(
                "See the frame an AI shot would start from before paying for it: give the same reference_* "
                "arguments (and aspect_ratio, crop) you would give generate_video_clip; returns the frame attached, "
                "its file_id and a note. Look at it, adjust reference_at_seconds until it shows what the shot "
                "should open on, then generate. A recording frame caught while its page still loads (a spinner, "
                "a blank page) is flagged with the moment it settles - pass that as reference_at_seconds. The "
                "answer opens with {file_id, at_seconds, width, height, loading: {at_seconds, settled_at_seconds} "
                "or null}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "aspect_ratio": {
                        "type": "string",
                        "enum": ["16:9", "9:16"],
                        "description": "The shot's aspect ratio (16:9 by default): the frame must have it.",
                    },
                    **_START_FRAME_PROPERTIES,
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="save_video_frame",
            description=(
                "Save one frame of a recording (after a step of its action log - the frame its contact sheet shows "
                "- or at a moment), of an AI shot or of a video file as a picture (PNG, up to 3840 px) and return "
                "its file_id: use it as an image scene, a statement backdrop, or the start picture of an AI shot "
                "(reference_file_id). Without a step or a moment, or at a moment past its end, the last frame (the "
                "answer says so). A render's MP4 is a video file: the file_id render_video, get_video_jobs and "
                "get_video give for a whole render, or render_video_scene for a scene preview. With region, only that "
                "part of the frame, cut at 100% of its pixels (never scaled): a screen insert's compositing judged at "
                "any moment of a render (the answers give each insert's region box). The picture is a working file "
                "(for you to look at, collapsed in the reply); deliver: true when the person asked for the frame "
                "itself, to hand it over as a download."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "recording_id": {"type": "string"},
                    "clip_id": {"type": "string", "description": "An AI shot."},
                    "file_id": {"type": "string", "description": "A video file."},
                    "at_step": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "A step of the recording, as in its action log.",
                    },
                    "at_seconds": {"type": "number", "minimum": 0},
                    "region": {
                        "type": "object",
                        "description": (
                            "Cut only this part of the frame, at 100%: box [x0, y0, x1, y1], or quad [[x, y] x 4] (cut by "
                            "its bounding box), 0-1 across and down the frame."
                        ),
                        "properties": {
                            "box": {
                                "type": "array",
                                "items": {"type": "number", "minimum": 0, "maximum": 1},
                                "minItems": 4,
                                "maxItems": 4,
                            },
                            "quad": {
                                "type": "array",
                                "items": {
                                    "type": "array",
                                    "items": {"type": "number", "minimum": 0, "maximum": 1},
                                    "minItems": 2,
                                    "maxItems": 2,
                                },
                                "minItems": 4,
                                "maxItems": 4,
                            },
                        },
                    },
                    "deliver": {
                        "type": "boolean",
                        "description": "The person asked for this frame: hand it over as a download file (default false: a working file).",
                    },
                },
            },
            source=_SOURCE,
            # Each moment and region of one video is its own frame, never a repeat: a sweep of one MP4.
            position_arguments=("at_seconds", "at_step", "region"),
            position_budget=_FRAMES_A_TURN,
        ),
        ToolDefinition(
            name="save_video_frames",
            description=(
                f"Save many frames of one video in one call (up to {_FRAMES_A_TURN}): at_seconds lists the moments, or "
                "step_seconds takes one every so many seconds from from_seconds (0) up to to_seconds (its end) - an 8 s "
                "insert at step_seconds 1 is 8 frames. Returns ONE contact sheet attached (a grid, each tile labelled "
                "with its moment m:ss.xx), the only file written; keep_frames true also keeps every frame as a picture "
                "of the conversation at full size (its file_id, opened with read_file_content). With region, each "
                "frame is that part only, cut at 100% like "
                "save_video_frame: a screen insert swept second by second in one look. The same sources as "
                "save_video_frame (a render's MP4 by file_id, a recording, an AI shot). The sheet and the frames are "
                "working files (for you to look at, collapsed in the reply)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "recording_id": {"type": "string"},
                    "clip_id": {"type": "string", "description": "An AI shot."},
                    "file_id": {"type": "string", "description": "A video file: a render's MP4."},
                    "at_seconds": {
                        "type": "array",
                        "items": {"type": "number", "minimum": 0},
                        "minItems": 1,
                        "maxItems": _FRAMES_A_TURN,
                        "description": "The moments, in seconds (or step_seconds instead).",
                    },
                    "step_seconds": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "description": "One frame every so many seconds.",
                    },
                    "from_seconds": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Where a step_seconds sweep starts (0).",
                    },
                    "to_seconds": {
                        "type": "number",
                        "minimum": 0,
                        "description": "Where it stops, not included (the end).",
                    },
                    "region": {
                        "type": "object",
                        "description": (
                            "Cut only this part of each frame, at 100%: box [x0, y0, x1, y1], or quad [[x, y] x 4] (cut "
                            "by its bounding box), 0-1 across and down the frame."
                        ),
                        "properties": {
                            "box": {
                                "type": "array",
                                "items": {"type": "number", "minimum": 0, "maximum": 1},
                                "minItems": 4,
                                "maxItems": 4,
                            },
                            "quad": {
                                "type": "array",
                                "items": {
                                    "type": "array",
                                    "items": {"type": "number", "minimum": 0, "maximum": 1},
                                    "minItems": 2,
                                    "maxItems": 2,
                                },
                                "minItems": 4,
                                "maxItems": 4,
                            },
                        },
                    },
                    "keep_frames": {
                        "type": "boolean",
                        "default": False,
                        "description": "Also keep every frame as a full-size file (false: the sheet only).",
                    },
                },
            },
            source=_SOURCE,
            position_arguments=("at_seconds", "step_seconds", "from_seconds", "to_seconds", "region"),
            position_budget=_FRAMES_A_TURN,
        ),
        ToolDefinition(
            name="get_video_insert_sweep",
            description=(
                "Look at every screen insert of a render whole, in one picture per scene: a contact sheet of each "
                "insert's region cut at 100% of the MP4's pixels every step_seconds (1 s by default) through the span "
                "its scene is on screen alone, each tile labelled with its time and frame, a frame failing an insert "
                "gate on its own framed in red with the gates named (insert_matte_halo, insert_matte_lag, "
                "insert_occluded among them; a scene's duplicate_logo finding frames its nearest tile), plus the table of every tile and the insert's "
                "coverage (its gates read every frame or a sample, the worst frame, the longest run of failing frames). "
                "The latest finished render of the video unless render_id names another, or a scene preview; scene "
                "keeps one scene. One call instead of one save_video_frame per second. The sheets are working files "
                "(for you to look at, collapsed in the reply)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "render_id": {
                        "type": "string",
                        "description": "A render or scene preview of the video (default: its latest finished render).",
                    },
                    "scene": {"type": "string", "description": "Only this scene's inserts (its id)."},
                    "step_seconds": {
                        "type": "number",
                        "minimum": 0.1,
                        "description": "Seconds between two tiles (default 1); widened to fit 60 tiles an insert, said.",
                    },
                },
                "required": ["video_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="compose_picture",
            description=(
                "Put real pictures inside a picture: the real product screenshot on the screen drawn in a still (a "
                "booth screen, a monitor, a phone), a real wordmark on a backwall, a logo on a product. A base "
                "picture and 1-4 layers, each a picture placed flat (x, y, width, height? - its box in the base's "
                "pixels) or in perspective (quad: the four corners of the place in the base's pixels - top left, "
                "top right, bottom right, bottom left), with opacity and fit (contain keeps the whole layer, cover "
                "fills the place and cuts its edges, stretch fills it as it is). Drawn by the platform (no "
                "browser, no AI); returns a PNG file of the conversation, attached: look at it before using it as "
                "an image scene, a statement backdrop, a start picture or a reference image of an AI shot."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "base_file_id": {
                        "type": "string",
                        "description": "The base picture: a conversation file, an upload or a template asset.",
                    },
                    "layers": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 4,
                        "items": {
                            "type": "object",
                            "properties": {
                                "file_id": {"type": "string", "description": "The layer's picture."},
                                "x": {"type": "number"},
                                "y": {"type": "number"},
                                "width": {"type": "number"},
                                "height": {"type": "number"},
                                "quad": {
                                    "type": "array",
                                    "items": {
                                        "type": "array",
                                        "items": {"type": "number"},
                                        "minItems": 2,
                                        "maxItems": 2,
                                    },
                                    "minItems": 4,
                                    "maxItems": 4,
                                    "description": "[[x, y] x 4]: top left, top right, bottom right, bottom left.",
                                },
                                "opacity": {"type": "number", "description": "Above 0, up to 1 (default 1)."},
                                "fit": {"type": "string", "enum": ["contain", "cover", "stretch"]},
                            },
                            "required": ["file_id"],
                        },
                    },
                    "name": {"type": "string", "description": "The file's name."},
                },
                "required": ["base_file_id", "layers"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="generate_music",
            description=(
                "Compose an AI music track with a music model from a prompt - genre, mood, instruments, tempo (BPM), "
                "how it builds, what to avoid - instrumental unless you say otherwise. With video_id it is composed "
                "at that video's length and becomes its music (the levels kept); without, use it as music: "
                "{music_id}. Compose it LAST, once every shot is made: shots come out longer than asked and "
                "narrations lengthen scenes. With video_id and refit: true, the video's AI track is composed again "
                "in place at the video's length now (the video keeps it; the save and get_video say when the "
                "music ends or loops before the video); add extend: true to keep the take the author approved and "
                "compose only its ending again, as its continuation, or pad: true to hold its last chord to the "
                "video's length with no model call (a take up to 8 s short). Never name an artist or a song: the "
                "providers refuse it. "
                "Uses the platform's music model unless you name another from video_studio_capabilities. Runs for "
                "up to a few minutes in the background. With music_id, a failed track is generated again in place. "
                "A built-in track (music_library) needs no model at all."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "video_id": {"type": "string", "description": "The video it is composed for and plays under."},
                    "duration_seconds": {
                        "type": "number",
                        "description": (
                            "Its length (5-600 s); with video_id, the video's own plus margin_seconds by default. "
                            "A refit never composes shorter than that."
                        ),
                    },
                    "margin_seconds": {
                        "type": "number",
                        "description": (
                            "With video_id: seconds composed past the video's planned length (0-60, default 8), so "
                            "the track's own end lands after the outro."
                        ),
                    },
                    "instrumental": {"type": "boolean", "description": "No vocals (default true)."},
                    "negative_prompt": {"type": "string", "description": "What it must not have."},
                    "model": {"type": "string", "description": "A music model from video_studio_capabilities."},
                    "provider_id": {"type": "string", "description": "The provider_id listed with that model."},
                    "name": {"type": "string"},
                    "music_id": {
                        "type": "string",
                        "description": "A failed AI track to generate again in place (prompt, model optional).",
                    },
                    "refit": {
                        "type": "boolean",
                        "description": (
                            "With video_id: compose the video's AI track again, in place, at the video's planned "
                            "length now plus margin_seconds (prompt and model optional; the video keeps its "
                            "music_id). The answer says whether the current render plays the earlier take."
                        ),
                    },
                    "extend": {
                        "type": "boolean",
                        "description": (
                            "With refit: keep the take and compose only its ending again, as the continuation of a "
                            "clip of the take before it, to the video's length: the take is kept sample for sample up "
                            "to there (the answer says where). Needs a music model that continues a clip (continues: true in "
                            "video_studio_capabilities; one of the take's own provider first, another's when it has "
                            "none). A failed extension leaves the take playing."
                        ),
                    },
                    "pad": {
                        "type": "boolean",
                        "description": (
                            "With refit, instead of extend: keep the take and hold its last chord to the video's "
                            "length, then fade it out - no model call, no credits - for a take at most 8 s short "
                            "(margin_seconds adds to the length, none by default). A failed pad leaves the take playing."
                        ),
                    },
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="preview_voiceover",
            description=(
                "Speak a line with a voice and style to choose the video's narrator; returns the audio file. Uses "
                "the platform's speech model unless you name another from video_studio_capabilities. The platform's "
                "pronunciation lexicon applies under the pronunciations given; the answer says how each lexicon term "
                "was sent and how the speech-to-text heard it, and runs render_video's voice check on the line: the "
                "share of its words heard as written (a narration passes from 90%) and the line word by word with "
                "each word heard otherwise, to test a rewording before a render."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "voice": {"type": "string", "description": "A voice id or name from video_studio_capabilities."},
                    "provider_id": {"type": "string", "description": "A provider_id from speech_models."},
                    "model": {"type": "string", "description": "A speech model from speech_models."},
                    "style": {
                        "type": "string",
                        "description": "How to speak: tone, pace, energy (for the models that take an instruction).",
                    },
                    "speed": {"type": "number"},
                    "pronunciations": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "text": {"type": "string"},
                                "say": {"type": "string"},
                                "ipa": {"type": "string"},
                                "match": {"type": "string", "enum": ["exact", "word", "case_insensitive"]},
                                "apply_to": {"type": "string", "enum": ["voice", "captions", "both"]},
                            },
                            "required": ["text", "say"],
                        },
                        "description": (
                            "Words read differently from how they are written (the video's voiceover.pronunciations), "
                            "over the platform's lexicon."
                        ),
                    },
                    "transcript_only": {
                        "type": "boolean",
                        "description": "true: the voice check alone, the audio not attached (default false).",
                    },
                },
                "required": ["text"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="create_video",
            description="Create a video project from a timeline spec (validated, references checked). " + spec_help,
            input_schema={"type": "object", "properties": {"spec": {"type": "object"}}, "required": ["spec"]},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="edit_video",
            description=(
                "Edit a video: the whole spec, or operations applied in order - {op: update, fields} (top-level), "
                "{op: update_scene, scene (id or index), fields}, {op: replace_scene, scene, value}, {op: insert_scene, "
                "index, value}, {op: remove_scene, scene}, {op: move_scene, scene, index}, {op: restore_scene, scene, "
                "revision}."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "spec": {"type": "object"},
                    "operations": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["video_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_video",
            description=(
                "A video's spec, revision, renders, scenes whose preview holds, sharing and public link. A long "
                f"spec shows whole scenes up to {SPEC_PAGE_CHARS} characters, the rest named: scenes picks ids, "
                "numbers or ranges ('18-24'); fields picks parts (spec: all; scenes, voiceover, music: those keys; "
                "renders; opt-in feedback: your studio requests, revisions: history); revision: the spec then."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "scenes": {
                        "type": "string",
                        "description": "Scene ids, 1-based numbers or ranges: 'intro,3,7-9'.",
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string", "enum": [*GET_VIDEO_FIELDS, *GET_VIDEO_ASKED_FIELDS]},
                        "description": "Default: all but the opt-in.",
                    },
                    "revision": {"type": "integer"},
                },
                "required": ["video_id"],
            },
            source=_SOURCE,
            live_state=True,
        ),
        ToolDefinition(
            name="list_videos",
            description="The videos you can open.",
            input_schema={"type": "object", "properties": {"limit": {"type": "integer"}}},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="share_video",
            description=(
                "Change who can open a video: give people (email or user id on this platform) and groups access, "
                "change it or take it away. Call it ONLY when the user asks, naming who gets or loses access first. "
                "It acts as the user with the Share dialog's rules: only the owner or an editor shares, only with "
                "existing accounts (nobody is invited). permission: 'read_only' (can view: watch, download, "
                "duplicate) or 'config' (can edit: also edit, render and share). action: 'add' (default), 'remove' "
                "or 'replace' (the audience becomes exactly those listed). For people without an account, use "
                "set_video_public_link."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "users": {"type": "array", "items": {"type": "string"}, "description": "Emails or user ids."},
                    "groups": {"type": "array", "items": {"type": "string"}, "description": "Group ids or names."},
                    "permission": {"type": "string", "enum": ["read_only", "config"]},
                    "action": {"type": "string", "enum": ["add", "remove", "replace"]},
                },
                "required": ["video_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="set_video_public_link",
            description=(
                "Turn on, replace or turn off the PUBLIC link of a video: a URL that lets ANYONE who has it watch "
                "the video's current render without signing in (a customer, a partner, an audience). Call it ONLY "
                "when the user asks, and say that anyone with the link can watch it. 'enable' (default) turns it "
                "on or returns the existing one; 'regenerate' replaces it (the old URL stops working); 'disable' "
                "turns it off. Only the owner or an editor may; an administrator can turn public links off. Give "
                "the returned URL as is. download: true also gives visitors a download button for the MP4 and its "
                "captions, false removes it; omit it to leave it unchanged (a new link offers none). It is a "
                "convenience, not a protection: anyone who can watch the video can save what it plays - never tell "
                "the user that turning it off stops visitors from keeping a copy."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "action": {"type": "string", "enum": ["enable", "regenerate", "disable"]},
                    "download": {"type": "boolean"},
                },
                "required": ["video_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="delete_video_renders",
            description=(
                "Delete renders of a video with their files, to free storage - ONLY when the user asks to delete or "
                "clean up renders. Pick them with select (default all_but_latest_ready: keeps the newest ready render "
                "of each quality the video has - of keep_quality alone when given - and deletes every other render, "
                "scene previews included; failed_and_cancelled; previews; all, the newest ready renders too, only with "
                "confirm: true) or name them with render_ids; before keeps the scope to renders made before a time. "
                "Each render goes whole: its MP4, poster, captions, frames, insert crops and the segments kept for a "
                "resume; files saved in the conversation (save_video_frame) stay. Refused by id: a render still queued "
                "or running (cancel_video_job first), the render the video's public link plays unless confirm: true, "
                "and on a video shared with you to watch only, a render someone else asked for; a render kept by hand "
                "stays unless named. dry_run: true answers the same list and deletes nothing. The answer lists each "
                "render {render_id, quality, revision, status, size_bytes, action delete|keep|refuse, reason}, the "
                "renders and bytes freed, and what the video serves now (its current render, its newest ready render "
                "per quality). Audited."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "render_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Exactly these renders (get_video lists them); not with select.",
                    },
                    "select": {"type": "string", "enum": list(SELECTORS)},
                    "keep_quality": {
                        "type": "string",
                        "enum": qualities,
                        "description": "With all_but_latest_ready: keep the newest ready render of this quality only.",
                    },
                    "before": {
                        "type": "string",
                        "description": "ISO 8601 date or time: only the renders made before it.",
                    },
                    "dry_run": {"type": "boolean", "description": "List what would go; delete nothing."},
                    "confirm": {
                        "type": "boolean",
                        "description": (
                            "Also delete the render the public link plays (the link then plays the newest remaining "
                            "ready render), and with select all the newest ready renders - only once the user agreed."
                        ),
                    },
                },
                "required": ["video_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="delete_video",
            description=(
                "Delete a video for good - ONLY when the user explicitly asks to delete that video. There is no "
                "trash. Its owner only (an administrator for a company-managed video); a video shared with the user "
                "is refused. It goes with its renders, scene previews and their files; its recordings and AI shots "
                "stay in the library. A video someone else opens (shared with people or groups, company-managed, or "
                "with a public link) is refused unless confirm: true, once the user agreed. Audited. Afterwards "
                "list_videos no longer lists it and its card in a conversation reads 'no longer available'. To free "
                "storage and keep the video, use delete_video_renders instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "video_id": {"type": "string", "description": "The video to delete (list_videos, get_video)."},
                    "confirm": {
                        "type": "boolean",
                        "description": (
                            "Delete a video other people open (shares, company-managed, public link) - only once "
                            "the user agreed."
                        ),
                    },
                },
                "required": ["video_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="render_video",
            description=(
                "Render a video to MP4 (H.264, AAC, captions): draft 720p30 to check, high 1080p60 (default), ultra "
                "2160p60. Voice-over is synthesised from the scenes' narration, each narration transcribed and held "
                "against its text (a failed one is spoken again, a term failed twice set apart, then slower; one only "
                "its model's closest voice says right FAILS it like a failed narration). The result is "
                "heard and watched back: the render FAILS on a voice-over with over 10% of its words heard wrong, a "
                "sentence heard twice or a stutter, and flags a picture frozen over 0.5 s; every finding comes with "
                "its time (m:ss.xx) and scene. A quality gate REFUSES the render, listing every reason: while an AI "
                "shot or a recording it plays is not ready, while the quality check saved for the video has findings "
                "the audience would see, when a narration failed its check, when the previous render of the same "
                "revision left findings open, or when a scene changed since it was last previewed "
                "(render_video_scene, previews queue in parallel) or rendered - judged on what the scene renders "
                "(its fields, overlays, media, transitions with its neighbours, look and music), never the revision, "
                "so an edit undone or one of what draws nothing (references, consistency) keeps its preview, and "
                "each refused scene says what changed (narration, overlay 0 quad, transition in) - fix, wait, "
                "preview, then render; "
                "force: true renders anyway, recorded and audited - only when the user asks for it as it is. A "
                "scene never previewed never holds a render; a passing preview of every scene showing an AI shot "
                "clears that shot's findings. Asked again for a revision already being rendered at the same quality, "
                "it answers that render (nothing restarts); asked for a revision the video's latest ready render "
                "already covers - same quality, gates green, nothing drawn or heard changed (a legibility "
                "declaration, a deck's speaker notes) - it answers that render and queues nothing (force: true renders "
                "again anyway). When that render plays a fallback take (a narration slower than the video's speed, a "
                "term set apart, another voice) and the voice check or that narration's lexicon entry changed since, "
                "it answers that the render covers the picture and offers a re-check of those scenes instead: ask the "
                "user, then recheck: true speaks those narrations alone again and hears them back; one that now "
                "passes at the video's speed is spliced into a new render - every other narration as it played, no "
                "frame drawn where the new take draws the same - and one that does not is said with the fix that "
                "would clear it (in an automation the re-check runs at once); a render made before fallback takes were "
                "recorded is offered from its voice summaries and the voice cache, and when its re-check cannot be "
                "computed it is answered as covering the revision, said why. A new revision or another quality "
                "replaces the video's "
                "render still in progress, which is cancelled. Takes minutes; waits as long as it can, then "
                "get_video_jobs follows up. The answer first says how long it should take and what that is made of "
                "(frames, seconds a frame per page and their source, pages, CPUs, memory); past one worker's 4 h it is "
                "drawn in scene ranges on several workers. What would stop it (ultra on a small worker) refuses it like "
                "the gate, saying what the worker needs and the quality that fits now - the user's choice - and "
                "video_studio_capabilities says each quality's limits beforehand. "
                "A drawn frame showing the brand's mark twice (the watermark over footage that shows the logo, or two "
                "logos in the shot) is a duplicate_logo finding with the frame and both boxes, and refuses the next "
                "render like a preview's: turn the watermark off on that scene (watermark: false) or generate the shot "
                "again with one logo. "
                "Renders queue in the render lane (the answer says where this one stands; scene previews have a lane "
                "of their own and never delay it); past the person's "
                "limit of renders in flight the answer lists them, to wait for or cancel."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "quality": {"type": "string", "enum": qualities},
                    "force": {
                        "type": "boolean",
                        "description": (
                            "Render past the quality gate's refusal (the refusal lists its reasons; the override is "
                            "recorded and audited), or render again a revision a ready render already covers."
                        ),
                    },
                    "recheck": {
                        "type": "boolean",
                        "description": (
                            "Accept the re-check a previous answer offered, or ask for one: every narration playing a "
                            "fallback take is spoken again at the video's speed past the voice cache and heard with "
                            "preview_voiceover's check; the answer names each result (speed, voice, share heard, "
                            "replaced or kept) or says no re-check was performed, and those that now pass are "
                            "spliced into a new render."
                        ),
                    },
                },
                "required": ["video_id"],
            },
            source=_SOURCE,
            # start_render answers the render of this revision and quality already queued or running.
            replay_safe=True,
            # Drawn by the render worker, not the agent's: a repeat after two cuts answers the render in flight.
            cut_safe=True,
        ),
        ToolDefinition(
            name="render_video_scene",
            description=(
                "Preview ONE scene of a video before rendering it all: an MP4 of just that scene with "
                "its voice-over, its music where it plays in the video and its transitions in and out (planned with "
                "the scene before and after it), and its first, middle and last frames attached - check a changed "
                "scene in under a minute instead of a full render. 1080p30 for a scene showing a figure, a recording "
                "or a screen insert, a 720p30 draft otherwise (quality overrides it). Every preview asked is queued - "
                "a sweep of every scene is one batch of calls, a few running at once. Previews have a lane of their "
                "own in the render queue, beside the whole renders (never waiting for one, nor one for them), dealt "
                "round-robin by video (the answer says its place and lane). A preview never replaces or becomes the "
                "video's render, and a new preview of the same scene replaces one still in progress. It runs the "
                "render's own audio and video pipeline and checks: the scene's "
                "narration transcribed against its text, stutters, frozen pictures - validate a scene here before "
                "render_video, which refuses a scene changed since its last preview. Before anything is drawn it runs "
                "the scene's pre-render lint, as render_video does: a screen insert's quad is measured on its footage "
                "(one a little off its bezel is snapped and saved, said), and every fault the preview would find "
                "without drawing (a surface not flat, footage shorter than its narration, a frozen hold, an insert "
                "past its loop seam, text that does not fit, media not ready) refuses it with the whole list at "
                "once; force: true previews anyway. A screen insert source it draws sharp that a larger quality "
                "would refuse as soft (a 4K render) is said as an advisory, never a refusal. Waits for it, then "
                "get_video_jobs(render_ids=[...]) follows up. The answer opens with {render_id, scene_id, status, "
                "quality, size, file_id (the MP4, a file of this conversation), frame_file_ids, checks {status, findings [{kind, "
                "severity, at, end, scene_id, message, data.frame_file_id?}] - times in the preview's MP4}}; every file "
                "its checks name (each finding's frame, each insert's crops) is a file of this conversation, the first "
                "12 attached and the rest named for read_file_content."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "video_id": {"type": "string"},
                    "scene": {
                        "type": "string",
                        "description": (
                            "The scene: its id, or its 0-based index in the spec as written ('3'), as edit_video "
                            "reads it; the id of a card the template adds works too."
                        ),
                    },
                    "quality": {
                        "type": "string",
                        "enum": list(PREVIEW_QUALITIES),
                        "description": "draft (720p30) or standard (1080p30); omitted, chosen from what the scene shows.",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "true previews the scene past its pre-render lint (audited); only when the user asks.",
                    },
                },
                "required": ["video_id", "scene"],
            },
            source=_SOURCE,
            # start_scene_render replaces the preview of the same scene still in progress: one job at most.
            replay_safe=True,
            # Drawn by the render worker: a repeat after two cuts queues that one preview, never a second job.
            cut_safe=True,
        ),
        ToolDefinition(
            name="get_video_jobs",
            description=(
                "Wait for recordings, clips, AI music tracks and renders and report them: returns as soon as one "
                "finishes, each with status queued|running|ready|failed, "
                "percent, stage (says stalled once a step stands past its time left), last progress time, "
                "the reason when it failed, a queued one's place in the queue, "
                "a running one's lease, for a running render the jobs sharing its "
                "worker with their owner and video and which you may cancel, "
                "and for an AI shot the provider's state and estimated time left. A ready or failed AI shot also "
                "carries checks: the block generate_video_clip answers inline, read from the record saved when it "
                "was made (frame file ids, frame_qc, stabilisation, logo_check, logo_tracked, text_check, surface, "
                "foreground_overlap, consistency, mode, model), and a failed regeneration its failed_attempt "
                "{reason, file_id, frames}: read the checks of a "
                "shot that finished in the background here. An ended render carries quality_report (which checker "
                "judged it) and answers compact: findings in full, headline lines, report_file_id for the full report "
                "(verbose=true: inline); rejudge=true judges ready renders anew, nothing drawn. Ids may be 8+ hex "
                "prefixes (resolved echoes them; none: not_found). "
                "Without ids, lists at once every job of yours still queued or running."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "recording_ids": {"type": "array", "items": {"type": "string"}},
                    "clip_ids": {"type": "array", "items": {"type": "string"}},
                    "music_ids": {"type": "array", "items": {"type": "string"}},
                    "render_ids": {"type": "array", "items": {"type": "string"}},
                    "verbose": {"type": "boolean"},
                    "rejudge": {"type": "boolean"},
                },
            },
            source=_SOURCE,
            live_state=True,
        ),
        ToolDefinition(
            name="cancel_video_job",
            description=(
                "Cancel recordings, AI shots, AI tracks or renders you started that are still queued or running "
                "(a running render stops between frames). Only when the user asks, or agrees to free a slot for "
                "another render; a cancelled shot can be generated again in place later."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "recording_ids": {"type": "array", "items": {"type": "string"}},
                    "clip_ids": {"type": "array", "items": {"type": "string"}},
                    "music_ids": {"type": "array", "items": {"type": "string"}},
                    "render_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
            source=_SOURCE,
        ),
    ]
