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

"""The invented-lettering read of an AI shot whose take carries no text check (dev:invented-text-gate-skips-null-text-check).

A take made before the text check existed, or whose check was skipped, keeps
no ``frame_qc.TEXT_CHECK`` record: the video's quality check found no
``invented_text`` on it and said nothing, so a render played gibberish signage
with no finding. Now the quality check reads such a take's first, middle and
last frames itself (:func:`read_shots`) with the same reading and the same rule
as the text check (``text_drift.transcribe``, ``text_drift.invented_words``:
letters that make no word, no brand term, no word of the start picture, at
least ``text_drift.MIN_INVENTED_HEIGHT_PX`` tall at 1080p), each invented word
cropped and stored. What it found is kept by the take's file
(``render_cache``): a take's frames never change, so a take is read once.

A take read with no invented lettering is said as a fact ("read, no invented
lettering"); one that could not be read is said too ("text check not run on
clip X"): never a silent pass. No video model is called and no shot is made
again: the reading is the chat model the text check and the demo frames
already read with, one call per take, bounded by :data:`MAX_SHOTS_READ` and
:data:`READ_TIMEOUT_SECONDS`.

A read outlives the check that started it (issue #9021): a check waits
:data:`READ_TIMEOUT_SECONDS` for it and never cancels it, the read runs to its
end (at most :data:`READ_BOUND_SECONDS`) and keeps what it found, and the next
check joins a read still running instead of starting it again. Before, a take
slower to read than the budget was cut on every save and never read. A render
or a scene preview reads the takes its scenes play the same way, with a longer
budget (:func:`render_findings`): an ``invented_text`` finding with the crop as
its frame, or a facts line.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: The takes one quality check reads at most, and how long the reading may take: what was not read is said, and
#: read by the next check.
MAX_SHOTS_READ = 4
READ_TIMEOUT_SECONDS = 20.0
#: How long one read may run once started, whatever the budget of the check waiting for it; how long a render or a
#: scene preview waits for its reads; how long a read that failed is said with its reason before it is tried again.
READ_BOUND_SECONDS = 180.0
RENDER_READ_SECONDS = 90.0
FAILED_KEPT_SECONDS = 600.0
#: The kind of a render's finding of invented lettering, as the quality check names it.
INVENTED_TEXT = "invented_text"
#: What was read, kept by the take's file and the start picture's words (``render_cache``).
CACHE_KIND = "text-backfill"
_CACHE_VERSION = 1
_READ_MAX_SIDE = 1568
#: ``digest -> (the read, when it ended or None while it runs)``: this process's reads, joined by the next check.
_READS: dict[str, tuple[asyncio.Task, list[float]]] = {}
#: Past this many, the ended reads are forgotten (their results stay in ``render_cache``).
_READS_HELD = 256


@dataclass(frozen=True)
class Unchecked:
    """A take the quality check reads itself: its file, its length (seconds) and its start picture's words."""

    file_id: str
    duration: float | None = None
    start_words: tuple[str, ...] = ()


@dataclass(frozen=True)
class Backfill:
    """What the read of one take found: *read* false with *why* when it could not be read."""

    read: bool
    invented: tuple[Any, ...] = ()
    why: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "read": self.read,
            "invented": [item.as_dict() for item in self.invented],
            **({"why": self.why} if self.why else {}),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Backfill | None:
        from app.services.video_studio import text_drift

        if not isinstance(raw, dict) or not isinstance(raw.get("read"), bool) or not raw["read"]:
            return None
        found = (text_drift.Invented.from_dict(item) for item in raw.get("invented") or [])
        return cls(True, tuple(item for item in found if item is not None))


def unchecked_of(params: dict[str, Any] | None, file_id: object, duration_ms: int | None) -> Unchecked | None:
    """The take of *params* to read when its text was never checked (no ``frame_qc.TEXT_CHECK`` record) and it has
    a file; ``None`` when its own check stands."""
    from app.services.video_studio import clips, frame_qc

    if not file_id or frame_qc.text_check((params or {}).get(frame_qc.QC_KEY)) is not None:
        return None
    duration = duration_ms / 1000.0 if isinstance(duration_ms, int) and duration_ms > 0 else None
    return Unchecked(str(file_id), duration, tuple(clips.lettering_words(params or {})))


def fact_line(clip_id: str, result: Backfill | None) -> str:
    """The facts line of a take read here: what was read, or why the text check did not run."""
    if result is None:
        return (
            f"Text check not run on clip `{clip_id}` yet: its frames are still being read past this check's budget, "
            "the next check says what they show."
        )
    if not result.read:
        return f"Text check not run on clip `{clip_id}`: {result.why or 'its frames could not be read'}."
    said = f"Text check of clip `{clip_id}` read here (its take kept none)"
    if result.invented:
        return f"{said}: {len(result.invented)} invented word(s): {named_words(result.invented)}."
    return f"{said}: first, middle and last frames, no invented lettering."


def named_words(invented: tuple[Any, ...]) -> str:
    """The first four invented words with their frame, place and crop, as a finding names them."""
    named = ", ".join(
        f'"{item.word}" ({item.frame.replace("_", " ")}{", " + item.where if item.box else ""}'
        f"{', crop `' + item.crop_file_id + '`' if item.crop_file_id else ''})"
        for item in invented[:4]
    )
    return named + (f" and {len(invented) - 4} more" if len(invented) > 4 else "")


def _digest(take: Unchecked) -> str:
    words = "|".join(sorted(w.lower() for w in take.start_words))
    return hashlib.sha256(f"v{_CACHE_VERSION}:{take.file_id}:{words}".encode()).hexdigest()


def judge(reading: Any, start_words: tuple[str, ...], *, brand: set[str] | None = None) -> tuple[Any, ...]:
    """The invented lettering of *reading* (the take's frames, as ``text_drift.FRAMES``), decided in code by the
    text check's rule against the start picture's *start_words*."""
    from app.services.video_studio import text_drift

    start = [text_drift.Word(word, known=True) for word in start_words]
    frames = {name: reading.words_of(name) for name in text_drift.FRAMES}
    return tuple(text_drift.invented_words(start, frames, brand if brand is not None else text_drift.brand_terms()))


async def _store_crops(
    user_id: object, clip_id: str, invented: tuple[Any, ...], paths: dict[str, str]
) -> tuple[Any, ...]:
    """Each invented word with its crop stored as a file of *user_id* (best effort: a word keeps no crop on failure)."""
    from dataclasses import replace

    from app.services.video_studio import assets, text_drift

    out = []
    for index, item in enumerate(invented):
        path = paths.get(item.frame)
        if item.box is None or not path or not Path(path).exists():
            out.append(item)
            continue
        try:
            data = await asyncio.to_thread(text_drift.crop_word, path, item.box)
            row = await assets.store_bytes(
                data,
                filename=f"clip-{clip_id[:8]}-invented-{item.frame.split('_')[0]}-{index}.jpg",
                user_id=user_id,
                source=assets.SOURCE_CLIP,
                content_type="image/jpeg",
                description=(
                    f'The crop of the AI shot\'s {item.frame.replace("_", " ")} reading "{item.word}": lettering the '
                    "take invented, read by the video's quality check (the take kept no text check)."
                )[:1000],
            )
            out.append(replace(item, crop_file_id=str(row.id)))
        except Exception as exc:  # the word is said without its crop
            logger.warning("The crop of an invented word was not stored: %s", format_exception_message(exc))
            out.append(item)
    return tuple(out)


async def read_take(user_id: object, clip_id: str, take: Unchecked) -> Backfill:
    """Read *take*'s first, middle and last frames for invented lettering, as *user_id*'s chat model reads them."""
    from app.core.database import async_session
    from app.llm.router import get_llm_for_user
    from app.services.video_studio import assets, encoder, text_drift

    with tempfile.TemporaryDirectory(prefix="xtm-text-backfill-") as work:
        video = Path(work) / "take"
        if await assets.download_file(take.file_id, str(video)) is None:
            return Backfill(False, why="its take could not be fetched")
        pictures: dict[str, tuple[bytes, str]] = {}
        paths: dict[str, str] = {}
        for name, at in zip(text_drift.FRAMES, text_drift.frame_times(take.duration), strict=True):
            out = Path(work) / f"{name}.jpg"
            try:
                await encoder.extract_still(str(video), at=at, out_path=str(out), max_side=_READ_MAX_SIDE)
            except encoder.EncoderError as exc:
                return Backfill(False, why=f"no {name.replace('_', ' ')} ({format_exception_message(exc)[:120]})")
            pictures[name] = (out.read_bytes(), "image/jpeg")
            paths[name] = str(out)
        async with async_session() as db:
            llm, model = await get_llm_for_user(db, str(user_id))
        reading = await text_drift.transcribe(llm, model, pictures)
        if reading is None:
            return Backfill(False, why="the reading came back unusable")
        invented = judge(reading, take.start_words)
        return Backfill(True, await _store_crops(user_id, clip_id, invented, paths))


async def _read_to_the_end(user_id: object, clip_id: str, take: Unchecked, ended: list[float]) -> Backfill:
    """One take read to its end (at most :data:`READ_BOUND_SECONDS`) whoever waits for it, kept once read. Never
    raises: a read that fails is said with its reason."""
    from app.services.video_studio import render_cache

    try:
        result = await asyncio.wait_for(read_take(user_id, clip_id, take), timeout=READ_BOUND_SECONDS)
    except TimeoutError:
        result = Backfill(False, why=f"its frames took longer than {READ_BOUND_SECONDS:.0f} s to read")
    except Exception as exc:  # said as a facts line, never a silent pass
        result = Backfill(False, why=format_exception_message(exc)[:160] or type(exc).__name__)
    if result.read:
        try:
            await render_cache.put_json(CACHE_KIND, _digest(take), result.as_dict())
        except Exception as exc:  # this process still answers it from the read itself
            logger.info("Text backfill of clip %s not kept: %s", clip_id, format_exception_message(exc))
    ended.append(time.monotonic())
    return result


def _joined(digest: str) -> tuple[asyncio.Task, list[float]] | None:
    """The read of *digest* this process holds and can still answer with: running on this loop, read, or failed
    less than :data:`FAILED_KEPT_SECONDS` ago; ``None`` (and forgotten) otherwise."""
    held = _READS.get(digest)
    if held is None:
        return None
    task, ended = held
    if not task.done():
        if task.get_loop() is asyncio.get_running_loop():
            return held
    elif not task.cancelled() and (task.result().read or time.monotonic() - ended[0] < FAILED_KEPT_SECONDS):
        return held
    _READS.pop(digest, None)
    return None


async def read_shots(
    user_id: object,
    takes: dict[str, Unchecked],
    *,
    timeout: float = READ_TIMEOUT_SECONDS,
    limit: int = MAX_SHOTS_READ,
) -> dict[str, Backfill]:
    """``clip id -> Backfill`` of each take of *takes* read: from the cache, from a read of this process (running,
    joined), or a read started now (at most *limit*), each waited for *timeout* seconds. A read is never cancelled
    by the wait: one still running is absent from the answer, keeps reading and keeps what it finds, so the next
    check answers it (issue #9021). Never raises."""
    from app.services.video_studio import render_cache

    found: dict[str, Backfill] = {}
    waiting: dict[str, asyncio.Task] = {}
    started = 0
    for clip_id in sorted(takes):
        digest = _digest(takes[clip_id])
        try:
            kept = Backfill.from_dict(await render_cache.get_json(CACHE_KIND, digest))
        except Exception:  # unkept: read again
            kept = None
        if kept is not None:
            found[clip_id] = kept
            continue
        held = _joined(digest)
        if held is None:
            if started >= limit:
                continue
            started += 1
            ended: list[float] = []
            task = asyncio.get_running_loop().create_task(
                _read_to_the_end(user_id, clip_id, takes[clip_id], ended), name=f"text-backfill-{uuid.uuid4().hex[:8]}"
            )
            held = _READS[digest] = (task, ended)
        waiting[clip_id] = held[0]
    if len(_READS) > _READS_HELD:
        for digest in [d for d, (task, _ended) in _READS.items() if task.done()]:
            _READS.pop(digest, None)
    if waiting:
        pending = {task for task in waiting.values() if not task.done()}
        if pending:
            # ``asyncio.wait`` never cancels what it waits for: what is not done in time goes on reading.
            await asyncio.wait(pending, timeout=max(0.0, timeout))
        for clip_id, task in waiting.items():
            if task.done() and not task.cancelled():
                found[clip_id] = task.result()
    return found


def _played_window(scene: dict, *, offset: float, span: tuple[float, float] | None) -> tuple[float, float] | None:
    """Where *scene* plays in the MP4 (from *offset*), cut to a preview's *span* ``(start, seconds)``; ``None`` when
    it plays nowhere there."""
    start = float(scene.get("start") or 0.0)
    end = float(scene.get("end") or start)
    if span is not None:
        start, end = max(start, span[0]), min(end, span[0] + span[1])
        if end < start:
            return None
    return max(0.0, start - offset), max(0.0, end - offset)


async def render_findings(
    user_id: object,
    document: dict,
    shots: dict[str, str],
    takes: dict[str, tuple[Unchecked, bool]],
    *,
    offset: float = 0.0,
    span: tuple[float, float] | None = None,
    scene_id: str | None = None,
    timeout: float = RENDER_READ_SECONDS,
    read: Any = None,
) -> tuple[list[tuple[Any, str | None]], list[str]]:
    """A render's or a scene preview's read of the AI shots its scenes play whose take kept no text check.

    *shots*: scene id to the AI shot it plays; *takes*: AI shot to ``(its take, text_check)`` for the takes
    to read (:func:`unchecked_of`). Answers ``([(finding, crop file id)], facts lines)``: an ``invented_text``
    finding per scene showing invented lettering (failing; a warning when the shot was asked with
    ``text_check: false``), the crop of its first word as its frame, and one facts line per take read or not
    (:func:`fact_line`): never a silent pass. *scene_id*: a preview's own scene alone. Never raises.
    """
    from app.services.video_studio.render_checks import FAIL, WARN, Finding

    playing: dict[str, list[tuple[str, tuple[float, float]]]] = {}
    for scene in document.get("scenes") or []:
        sid = str(scene.get("id") or "")
        clip_id = shots.get(sid)
        if not clip_id or clip_id not in takes or (scene_id is not None and sid != scene_id):
            continue
        if (window := _played_window(scene, offset=offset, span=span)) is not None:
            playing.setdefault(clip_id, []).append((sid, window))
    if not playing:
        return [], []
    try:
        found = await (read or read_shots)(
            user_id, {clip_id: takes[clip_id][0] for clip_id in playing}, timeout=timeout, limit=len(playing)
        )
    except Exception as exc:  # each take is said not checked below
        logger.info("Text backfill of the render skipped: %s", format_exception_message(exc) or type(exc).__name__)
        found = {}
    out: list[tuple[Any, str | None]] = []
    lines: list[str] = []
    for clip_id in sorted(playing):
        result = found.get(clip_id)
        lines.append(fact_line(clip_id, result))
        if result is None or not result.read or not result.invented:
            continue
        strict = takes[clip_id][1]
        crop = next((item.crop_file_id for item in result.invented if item.crop_file_id), None)
        for sid, (at, end) in playing[clip_id]:
            said = (
                "letters that make no word, no brand term and no word of its start picture"
                if strict
                else "accepted, the shot was asked with text_check: false"
            )
            out.append(
                (
                    Finding(
                        INVENTED_TEXT,
                        at,
                        end,
                        sid,
                        f"Scene {sid}: its AI shot (clip `{clip_id}`) shows invented lettering: "
                        f"{named_words(result.invented)} - {said}; read by this render, its take kept no text check",
                        FAIL if strict else WARN,
                        {"clip_id": clip_id, "words": [item.as_dict() for item in result.invented[:8]]},
                        fix=(
                            f"generate the shot again in place (clip_id `{clip_id}`), keep that region from the start "
                            "picture (keep_regions), or set text_check: false on the shot to accept it"
                            if strict
                            else "nothing: accepted with text_check: false"
                        ),
                    ),
                    crop,
                )
            )
    return out, lines
