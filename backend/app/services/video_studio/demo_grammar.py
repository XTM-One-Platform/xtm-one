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

"""How a product demo is told: the narration says what the product gives, the camera stays still unless a
detail needs it, the footage plays at its own speed.

A demo built from defaults read as a screen reading with a restless camera:
its narration said what was drawn ("Click the layered button: 23 entities,
78 relationships"), the automatic camera zoomed in on every click, a
recording was slowed to half speed under a narration twice its length. A
product manager demoing live says the value and the mechanism, lets the
footage show the rest and moves the camera only to a detail the viewer could
not read. Asking for it in a prompt holds nothing (invariant 16): the quality
check of every save says each slip instead (:func:`advise`, read by
``quality.content_findings``), as an advisory - it never blocks a render -
naming the scene, the words and the change, on each recording scene:

* ``demo_narration`` - a narration that reads its screen: a UI action said
  as an instruction (:func:`instructions`: the pointer and keyboard actions
  a take plays, ``spec.TakeStep.action``, opening a clause and followed by
  what they act on - "Click the layered button", "then open the graph"), a
  count of the data on screen (:func:`screen_counts`: "23 entities"), or a
  name of two words or more its frames show while it is said
  (:func:`labels_on_frame`; the frames read once by the person's chat model,
  :func:`frame_texts`, kept in ``render_cache``). The brand's names and the
  video's title are the subject, never a label read off the frame.
* ``demo_camera`` - an automatic camera making more than
  :data:`MOVES_PER_MINUTE` moves a minute of its scene (one always allowed),
  or moving in on a step whose element covers more than
  :data:`LARGE_ELEMENT_SHARE` of the page, which reads at full frame: the
  moves the render draws (``timeline.recording_camera``).
* ``demo_speed`` - a recording played under ``timeline.SLOWEST_FILL`` to last
  as long as its narration - by its ``fit: auto``, or by its own ``speed`` -,
  naming the narration that keeps it at :data:`ADVISED_SPEED`.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import tempfile
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: An automatic camera moves in at most this many times a minute of its scene, once in any scene.
MOVES_PER_MINUTE = 4
#: A step whose element covers more of the page than this reads at full frame: a move in on it is for effect.
LARGE_ELEMENT_SHARE = 0.4
#: The speed a slowed recording plays at under the narration length its advisory names.
ADVISED_SPEED = 0.8
#: The pointer and keyboard actions of a take (``spec.TakeStep.action``) and the verbs a narration says them with.
SPOKEN_ACTIONS: dict[str, tuple[str, ...]] = {
    "goto": ("open", "go to"),
    "click": ("click",),
    "double_click": ("double-click", "double click"),
    "right_click": ("right-click", "right click"),
    "type": ("type",),
    "press": ("press",),
    "hover": ("hover",),
    "drag": ("drag",),
    "scroll": ("scroll",),
    "select": ("select",),
}
#: What opens a clause before an instruction ("..., then click"), beside the start and the punctuation.
_SEQUENCERS = frozenset({"and", "then", "now", "just", "simply", "first", "next", "finally"})
#: What follows a verb said as an instruction - its object or its particle ("click the button", "scroll down"),
#: never a noun the word qualifies ("open source", "type and severity"). A capital or a quote opens an object too.
_OBJECTS = frozenset(
    {
        "the", "a", "an", "this", "that", "these", "those", "your", "its", "their", "our", "any", "each", "every",
        "it", "them", "here", "there", "on", "onto", "into", "through", "down", "up", "over",
    }
)  # fmt: skip
_CLAUSE_END = ".!?;:,\u2014\u2013"
_VERB = re.compile(
    r"(?<![\w-])("
    + "|".join(sorted({re.escape(verb) for verbs in SPOKEN_ACTIONS.values() for verb in verbs}, key=len, reverse=True))
    + r")(?![\w-])",
    re.IGNORECASE,
)
_TOKEN = re.compile(r"[\w'\u2019\"\u201c\u2018-]+")
#: A count of what a graph view draws, read off its counters.
_COUNT = re.compile(r"(?<![\w.,])(\d[\d,]*)\s+(entit(?:y|ies)|relationships?|nodes?|edges?)\b", re.IGNORECASE)
#: A name: two or more words with a capital each ("Threat Actor", "Operation Ghost").
_NAME = re.compile(r"[A-Z][\w'\u2019-]*(?:\s+[A-Z][\w'\u2019-]*)+")

DemoKind = Literal["demo_narration", "demo_camera", "demo_speed"]


@dataclass(frozen=True)
class DemoAdvice:
    """One slip of a demo's grammar in one recording scene (:func:`advise`)."""

    scene_id: str
    kind: DemoKind
    words: str


# ── The narration ─────────────────────────────────────────────────────────


def _opens_clause(before: str) -> bool:
    text = before.rstrip()
    if not text or text[-1] in _CLAUSE_END or text.endswith(" -"):
        return True
    last = re.findall(r"[\w']+", text[-24:])
    return bool(last) and last[-1].lower() in _SEQUENCERS


def _acts_on(after: str) -> bool:
    following = _TOKEN.match(after.lstrip())
    if following is None:
        return False
    word = following.group(0)
    return word[0].isupper() or word[0] in "\"'\u201c\u2018" or word.lower() in _OBJECTS


def instructions(narration: str) -> list[str]:
    """The UI actions *narration* says as instructions, each with the words it acts on ("Click the layered button"),
    once per verb."""
    found: dict[str, str] = {}
    for match in _VERB.finditer(narration):
        verb = match.group(1)
        if (
            verb.lower() in found
            or not _opens_clause(narration[: match.start()])
            or not _acts_on(narration[match.end() :])
        ):
            continue
        rest = re.split(rf"[{_CLAUSE_END}]", narration[match.end() :], maxsplit=1)[0]
        tail: list[str] = []
        for token in _TOKEN.findall(rest)[:4]:
            if token.lower() in _SEQUENCERS:
                break
            tail.append(token)
        found[verb.lower()] = " ".join([verb, *tail]).strip(" \"'\u201c\u201d\u2018\u2019")
    return list(found.values())


def screen_counts(narration: str) -> list[str]:
    """The counts of what a graph view draws that *narration* reads ("23 entities")."""
    return list(dict.fromkeys(f"{match.group(1)} {match.group(2)}" for match in _COUNT.finditer(narration)))


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", text.lower()).split())


def _runs_in(words: list[str], text: str) -> list[tuple[int, int]]:
    """``(start, end)`` of each run of two *words* or more found in *text*, the longest first."""
    padded = f" {_plain(text)} "
    return [
        (i, i + size)
        for size in range(len(words), 1, -1)
        for i in range(len(words) - size + 1)
        if f" {_plain(' '.join(words[i : i + size]))} " in padded
    ]


def names_in(narration: str, known: Iterable[str] = ()) -> list[list[str]]:
    """The names of two words or more *narration* says (each as its words), what *known* writes - the brand's names,
    the video's title - taken out of them ("The Threat Actor" is no name when "Threat Actor" is known)."""
    known = list(known)
    found = []
    for match in _NAME.finditer(narration):
        words = match.group(0).split()
        covered = {i for text in known for start, end in _runs_in(words, text) for i in range(start, end)}
        group: list[str] = []
        for i, word in enumerate([*words, ""]):
            if word and i not in covered:
                group.append(word)
                continue
            if len(group) >= 2:
                found.append(group)
            group = []
    return found


def labels_on_frame(narration: str, frame_text: str, known: Iterable[str] = ()) -> list[str]:
    """The names of two words or more *narration* says that *frame_text* - what its frames show - shows too: the
    longest run of each name found there, two words at least."""
    found: dict[str, str] = {}
    for words in names_in(narration, known):
        if runs := _runs_in(words, frame_text):
            start, end = runs[0]
            run = " ".join(words[start:end])
            found.setdefault(_plain(run), run)
    return list(found.values())


def narration_slips(narration: str, frame_text: str | None = None, known: Iterable[str] = ()) -> list[str]:
    """What *narration* reads off its screen: the words of each ``demo_narration`` advisory."""
    said = [
        f'its narration says the action on screen ("{words}"): say how the capability works and what it gives - '
        "the footage shows the action"
        for words in instructions(narration)
    ]
    said += [
        f'its narration reads a count off the screen ("{words}"): say what the picture means, never what it counts'
        for words in screen_counts(narration)
    ]
    if frame_text:
        said += [
            f'its narration reads a label its frame shows ("{words}"): say what it means for the viewer - the frame '
            "already shows the label"
            for words in labels_on_frame(narration, frame_text, known)
        ]
    return said


# ── The camera ────────────────────────────────────────────────────────────


def allowed_moves(seconds: float) -> int:
    """The moves an automatic camera may make in a scene of *seconds*: :data:`MOVES_PER_MINUTE` a minute, one in any."""
    return max(1, int(MOVES_PER_MINUTE * seconds / 60))


def camera_slips(scene: Any, planned: dict, take: Any) -> list[str]:
    """What a recording *scene*'s automatic camera does for effect, planned as *planned* over *take*: the
    words of each ``demo_camera`` advisory."""
    from app.services.video_studio.timeline import recording_camera

    points = [(float(a), float(b)) for a, b in planned.get("time_map") or []]
    if scene.camera.mode != "auto" or len(points) < 2:
        return []
    _frames, moves = recording_camera(
        scene, take, points, float((planned.get("transition_in") or {}).get("duration") or 0.0)
    )
    seconds = points[-1][0]
    said = []
    if seconds > 0 and len(moves) > (allowed := allowed_moves(seconds)):
        said.append(
            f"its automatic camera makes {len(moves)} moves in {seconds:.1f} s ({len(moves) * 60 / seconds:.1f} a "
            f"minute; {allowed} at most in a scene this long, {MOVES_PER_MINUTE} a minute): keep it still "
            "(camera.mode static), or move it only where a detail cannot be read at full frame (camera.zoom_on: "
            '["highlight"] and a highlight step on that detail)'
        )
    for move in moves:
        if move.share > LARGE_ELEMENT_SHARE:
            step = f"step {move.step}" if move.step is not None else "a step"
            said.append(
                f"its automatic camera moves in on {step} at {move.t:.1f} s, whose element covers {move.share:.0%} of "
                "the page - it reads at full frame: leave it wide (take its action out of camera.zoom_on, or keep "
                "the camera static)"
            )
    return said


# ── The footage's speed ───────────────────────────────────────────────────

#: The slowest speed the advisory solves for: a recording that would need less is said at it.
_SLOWEST_SOLVED = 0.1
#: Seconds a recording may fall short of its narration, or a narration end before its scene, and still be said to fit.
_TOLERANCE = 0.3


def _lasting(played_at: Callable[[float], float], need: float, *, below: float) -> float:
    """The fastest speed, at most *below*, at which footage playing ``played_at(speed)`` seconds lasts *need*."""
    low, high = _SLOWEST_SOLVED, below
    if played_at(low) < need:
        return low
    for _ in range(40):
        middle = (low + high) / 2
        if played_at(middle) >= need:
            low = middle
        else:
            high = middle
    return low


def speed_slip(scene: Any, planned: dict, take: Any, spoken: tuple[float, float] | None = None) -> str | None:
    """The words of a ``demo_speed`` advisory: a recording *scene* playing *take* under
    ``timeline.SLOWEST_FILL`` to last as long as its narration - *spoken*, ``(start, end)`` in seconds of the video -,
    with the narration that keeps it at :data:`ADVISED_SPEED`; ``None`` for one at its own pace."""
    from app.services.video_studio.timeline import NARRATION_TAIL, SLOWEST_FILL, WORDS_PER_SECOND, recording_seconds

    points = planned.get("time_map") or []
    if not scene.narration or len(points) < 2:
        return None
    start = float(planned.get("start") or 0.0)
    need = float(points[-1][0]) - float(scene.hold_end or 0.0)
    if spoken is not None and float(spoken[1]) + NARRATION_TAIL - start < need - _TOLERANCE:
        return None  # the scene lasts for its recording, not for its narration
    fill_to = need if scene.extend_mode == "zoom" else 0.0

    def played_at(speed: float) -> float:
        return recording_seconds(take, scene, speed=speed, fill_to=fill_to)

    speed = float(scene.speed)
    if scene.fit == "auto" and played_at(speed) < need - _TOLERANCE:
        speed = _lasting(played_at, need, below=speed)
    if speed >= SLOWEST_FILL:
        return None

    def plain_at(speed: float) -> float:
        return recording_seconds(take, scene, speed=speed)

    keep = plain_at(ADVISED_SPEED)
    target = max(1, math.floor(keep + 0.5))
    while target > 1 and _lasting(plain_at, target, below=ADVISED_SPEED) < SLOWEST_FILL:
        target -= 1
    lead = need - (float(spoken[1]) - float(spoken[0])) if spoken is not None else 0.35 + NARRATION_TAIL
    words = max(1, int((target - lead) * WORDS_PER_SECOND))
    return (
        f"its recording plays {played_at(1.0):.1f} s at 1x and its narration holds it {need:.1f} s, so it plays at "
        f"{speed:.2f}x - under {SLOWEST_FILL:g}x a cursor and typing read as slow motion: a narration holding it "
        f"about {target} s (some {words} words) keeps it at {ADVISED_SPEED:g}x - shorten the narration rather than "
        "slow the recording, or record more of the flow"
    )


# ── All of it ─────────────────────────────────────────────────────────────


def known_names(spec: Any, template: Any) -> list[str]:
    """The names that are a video's subject, never a label read off its frames: its brand's and its title."""
    from app.services.video_studio.templates import brand_names

    return [*brand_names(template), spec.title]


def advise(
    spec: Any,
    document: dict,
    takes: Mapping[str, Any],
    narration: Mapping[str, tuple[float, float]] | None = None,
    frame_text: Mapping[str, str] | None = None,
    known: Iterable[str] = (),
) -> list[DemoAdvice]:
    """Each slip of the demo grammar in the recording scenes of the effective *spec*, planned as *document* with the
    recordings *takes* (``timeline.TakeMedia`` by take id): *narration* the spans the plan gave each narration,
    *frame_text* what each scene's frames show while its narration is said (:func:`frame_texts`), *known* the names
    that are the video's subject (the brand's, its title)."""
    planned_by_id = {scene.get("id"): scene for scene in document.get("scenes") or [] if isinstance(scene, dict)}
    known = list(known)
    found: list[DemoAdvice] = []
    for scene in spec.scenes:
        if scene.type != "recording":
            continue
        if scene.narration:
            found.extend(
                DemoAdvice(scene.id, "demo_narration", words)
                for words in narration_slips(scene.narration, (frame_text or {}).get(scene.id), known)
            )
        planned, take = planned_by_id.get(scene.id), takes.get(scene.take_id)
        if planned is None or take is None:
            continue
        found.extend(DemoAdvice(scene.id, "demo_camera", words) for words in camera_slips(scene, planned, take))
        if words := speed_slip(scene, planned, take, (narration or {}).get(scene.id)):
            found.append(DemoAdvice(scene.id, "demo_speed", words))
    return found


# ── What the frames show ──────────────────────────────────────────────────

#: Where in its narration a scene's frames are read: a little after it starts, its middle, a little before it ends.
FRAME_MOMENTS = (0.15, 0.5, 0.85)
#: The frames one check reads, every scene together, and how long reading them may take: an advisory never holds a
#: save's check past it, and what was not read is read by the next one.
MAX_FRAMES_READ = 12
READ_TIMEOUT_SECONDS = 15.0
#: The frames read, kept by the take's file and the moment (``render_cache``): a take's frames never change.
CACHE_KIND = "frame-text"
_CACHE_VERSION = 1
#: A word read under this confidence is not taken as shown.
_READ_CONFIDENCE = 0.5
_READ_MAX_SIDE = 1568


def frame_moments(
    spec: Any, document: dict, narration: Mapping[str, tuple[float, float]], known: Iterable[str] = ()
) -> dict[str, list[tuple[str, float]]]:
    """``scene id -> [(take id, second of the take), ...]``: where the frames of each recording scene whose narration
    names something (:func:`names_in`) are read - :data:`FRAME_MOMENTS` of its narration, on its take."""
    from app.services.video_studio.timeline import map_forward

    planned_by_id = {scene.get("id"): scene for scene in document.get("scenes") or [] if isinstance(scene, dict)}
    known = list(known)
    found: dict[str, list[tuple[str, float]]] = {}
    for scene in spec.scenes:
        planned = planned_by_id.get(scene.id)
        if scene.type != "recording" or not scene.narration or planned is None or not names_in(scene.narration, known):
            continue
        points = [(float(a), float(b)) for a, b in planned.get("time_map") or []]
        if len(points) < 2:
            continue
        start, length = float(planned.get("start") or 0.0), points[-1][0]
        span = narration.get(scene.id)
        lo, hi = (float(span[0]) - start, float(span[1]) - start) if span is not None else (0.0, length)
        lo, hi = max(0.0, min(lo, length)), max(0.0, min(hi, length))
        found[scene.id] = list(
            dict.fromkeys(
                (scene.take_id, round(map_forward(points, lo + share * (hi - lo)), 1)) for share in FRAME_MOMENTS
            )
        )
    return found


def _digest(file_id: object, at: float) -> str:
    return hashlib.sha256(f"v{_CACHE_VERSION}:{file_id}:{at:.1f}".encode()).hexdigest()


async def _read_frames(
    user_id: object, frames: dict[tuple[str, float], tuple[object, str]]
) -> dict[tuple[str, float], str]:
    """The text each frame shows - ``(take id, second) -> (file id, its digest)`` - read in one call of *user_id*'s
    chat model, each take's video fetched once."""
    from app.core.database import async_session
    from app.llm.router import get_llm_for_user
    from app.services.video_studio import assets, encoder, text_drift

    async with async_session() as db:
        llm, model = await get_llm_for_user(db, str(user_id))
    pictures: dict[str, tuple[bytes, str]] = {}
    names: dict[str, tuple[str, float]] = {}
    with tempfile.TemporaryDirectory(prefix="xtm-demo-frames-") as work:
        videos: dict[object, Path | None] = {}
        for key, (file_id, _digest) in frames.items():
            if file_id not in videos:
                video = Path(work) / f"take-{len(videos)}"
                videos[file_id] = video if await assets.download_file(file_id, str(video)) is not None else None
            video = videos[file_id]
            if video is None:
                continue
            name = f"frame_{len(pictures) + 1}"
            out = Path(work) / f"{name}.jpg"
            await encoder.extract_still(str(video), at=key[1], out_path=str(out), max_side=_READ_MAX_SIDE)
            pictures[name], names[name] = (out.read_bytes(), "image/jpeg"), key
    if not pictures:
        return {}
    reading = await text_drift.transcribe(llm, model, pictures)
    if reading is None:
        return {}
    texts = {}
    for name, key in names.items():
        words = reading.words.get(name)
        if words is not None:
            texts[key] = " ".join(word.text for word in words if word.confidence >= _READ_CONFIDENCE)
        else:
            texts[key] = reading.texts.get(name, "")
    return texts


async def frame_texts(
    user_id: object,
    spec: Any,
    document: dict,
    narration: Mapping[str, tuple[float, float]],
    known: Iterable[str] = (),
    *,
    read: bool = True,
) -> dict[str, str]:
    """``scene id -> what its frames show`` while its narration is said, for each recording scene whose narration
    names something (:func:`frame_moments`): each frame read once, by *user_id*'s chat model, and kept; with *read*
    false only the frames read before. Never raises: a frame not read says nothing."""
    from app.core.database import async_session
    from app.models.video_studio import VideoTake
    from app.services.video_studio import render_cache

    moments = frame_moments(spec, document, narration, known)
    if not moments:
        return {}
    shown: dict[tuple[str, float], str] = {}
    missing: dict[tuple[str, float], tuple[object, str]] = {}
    try:
        files: dict[str, object] = {}
        async with async_session() as db:
            for take_id in {take_id for items in moments.values() for take_id, _at in items}:
                take = await db.get(VideoTake, uuid.UUID(take_id))
                if take is not None and take.file_id:
                    files[take_id] = take.file_id
        for key in dict.fromkeys(key for items in moments.values() for key in items):
            if key[0] not in files:
                continue
            digest = _digest(files[key[0]], key[1])
            kept = await render_cache.get_json(CACHE_KIND, digest)
            if isinstance(kept, dict) and isinstance(kept.get("text"), str):
                shown[key] = kept["text"]
            elif read and len(missing) < MAX_FRAMES_READ:
                missing[key] = (files[key[0]], digest)
    except Exception as exc:  # advisory: the narration's other checks stand without the frames
        logger.info("The frames of a demo were not looked up: %s", format_exception_message(exc) or type(exc).__name__)
        return {}
    if missing:
        try:
            fresh = await asyncio.wait_for(_read_frames(user_id, missing), timeout=READ_TIMEOUT_SECONDS)
        except Exception as exc:  # the frames read before still count; these are read by the next check
            logger.info("The frames of a demo were not read: %s", format_exception_message(exc) or type(exc).__name__)
            fresh = {}
        for key, text in fresh.items():
            shown[key] = text
            await render_cache.put_json(CACHE_KIND, missing[key][1], {"text": text})
    return {
        scene_id: " ".join(shown[key] for key in items if key in shown)
        for scene_id, items in moments.items()
        if any(key in shown for key in items)
    }
