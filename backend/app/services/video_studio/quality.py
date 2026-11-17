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

"""Quality check of a saved video.

``validate_video_spec`` guarantees a video is well-formed; it cannot tell
whether the words of a scene stay clear of its subtitles, fit the frame or
stay on screen long enough to be read - those are properties of pixels,
fonts, wrapping and time, not of the JSON. The agent cannot see the video it
wrote, and prompting it to "keep scenes light" changes nothing. Two
deterministic answers instead, as for decks (``presentation_layout``):

* The compositor lays every frame out so nothing covers anything else
  (``compositor_page``): the subtitles keep a band at their edge that the
  scene's text, its lower thirds and a recording's window stay out of, a
  card's whole text is scaled to fit the room left, badges and callouts take
  a free spot. That holds for every video, whoever wrote it.
* Here, right after a save, the video is played in the compositor's headless
  Chromium at the moments that matter and ``window.__xtm.audit()`` measures
  what is left: text covering text, text past the frame, text shrunk to fit,
  text still coming in when its scene ends. With what the timeline alone
  tells (reading time, subtitle speed, words over a bright backdrop, an
  overlay too short to read), those become findings the agent acts on in the
  same turn: WHICH scene, WHAT, and what to change.

The check is advisory and bounded: a missing browser, a timeout or a disabled
switch produce a one-line note, and the video is saved either way. It plays
the video as the editor previews it - recordings and AI shots stand in as
placeholders, narration is paced at the planner's reading speed - which is
what the layout depends on. A report is about one revision and kept briefly,
so a read-back of an unchanged video does not launch a second browser.

The pictures and the sound are checked too, from what the database
knows of the media - no browser, so these findings stand even when the layout
could not be measured (:func:`content_findings`): an AI shot that failed or is
still being made, a shot whose prompt asks for a locked camera that moved
(its kept frames, ``frame_qc``), an image scene smaller than the frame it is
drawn on, a screen insert's corner outside the footage the frame shows, a
music track that ends before the video with ``loop`` off, an AI track the
video outgrew by more than 2 s (``music_outgrown``: refit it, extend
it, or accept the loop), a narration no
voice will speak, a narration placed in another voice than the video's
(its check record: ``voice_check.voice_changes``) - and, as a fact
with the fix that would clear it, one placed slower or with a term set apart
(``voice_check.fallback_takes``) -, a card writing the
brand name its logo already spells (``templates.brand_repeats``). How
loud the music plays is measured on the bed the render will prepare
(``music_level``): a silent or too quiet track, a long quiet stretch or music
lost under the voice is an advisory. The audit also counts the brand marks
each frame it plays draws: two on one frame is "brand mark drawn twice".
A clip or a recording shorter than its narration whose
``extend_mode`` is ``hold`` freezes its last frame: planned with each
narration at its spoken length where it was spoken (``preview.spoken_lengths``),
a frozen end past the render's own threshold (``frame_freeze.FROZEN_SECONDS``)
is a finding that refuses the render (``frozen_hold``), zoom or loop
suggested - before a 4K render is paid for to warn "frozen 0.7 s".

What a static screen insert's footage shows is measured before anything is
drawn too (``insert_bezels``): a curved surface is a finding only a
shot made again fixes (``insert_flatness``; one seen at an angle is none), and footage made
again since the save measured its bezel is ``insert_bezel``. A shortfall says
the ``speed`` that closes it, the planner's own arithmetic
(``timeline.speed_to_fill``), and a screen insert running past its clip's loop
seam says where the seam falls (``insert_loop_seam``). A screen insert whose
recording or file is drawn with its smallest text under 10 px of the 1080p
frame is an advisory, under 6 px a finding
(``insert_legibility``): both offer only what brings its text to 12 px and
keeps the page's layout - the share of the frame its quad needs, a
push on the insert (``zoom``), a higher browser zoom (a narrow quad) or a
narrower viewport only where the recorder saw the layout hold; a surface its
scene has no room for, beside the inserts sharing its width, is said so and
offered a browser zoom up to the largest its page keeps its layout at or a
tighter page; a save's answer says the advisory once per insert. One
declared ``legibility: ambient`` is scenery: no finding, the report saying its
smallest text's size as a fact - ``ambient, not judged - ... reads about
5 px`` - (:attr:`QualityReport.unchecked`). A shot's failed regeneration is an advisory
said once, naming the take that plays. A screen insert or a replaced
screen whose source a whole render would refuse as soft at one of the
qualities a video may be rendered at - standard, high, ultra - is an advisory
naming them and the source size needed (``insert_resolution``, the
render gate's own measure, ``render_checks.soft_qualities``): a preview at
1080p never draws what only a 4K render refuses. Said once per insert, like
legibility; ``render_video`` refuses it at the quality asked, as before. The
legibility advice offers only a scale the source carries at the quality the
video's renders are made at (``render_checks.target_quality``), and
names the larger render that would refuse it: a push it suggests is never
one the gate refuses there.

A demo is held to the grammar of a product manager demoing live
(``demo_grammar``), as advisories: a recording scene's narration that reads
its screen - a UI action said as an instruction, a count of the data on
screen, a label its frames show (read once, never at the render gate) -
(``demo_narration``), an automatic camera moving for effect (``demo_camera``)
and a recording slowed under 0.7x to last as long as its narration
(``demo_speed``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from typing import Any, Literal, NamedTuple

from app.core.config import settings
from app.core.redis import redis_client
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: Longer than an interactive turn should wait for a courtesy; the video is
#: saved before the check starts, so a slow one is skipped, never failed.
QUALITY_CHECK_TIMEOUT_SECS = 40.0
#: How long a report of one (video, user, revision) is kept.
QUALITY_CHECK_CACHE_TTL_SECS = 900
#: Moved when what a report finds changes (2: a surface seen at an angle is no ``insert_flatness``; 3: a screen
#: insert's text drawn too small to read, ``insert_legibility``; 4: an ambient screen insert not checked; 5: a
#: bow judged on the visible part of an edge, legibility a finding under 6 px, a failed regeneration said once;
#: 6: a bow whose points' agreement was not read is not judged, and the legibility advice keeping the page's
#: layout; 7: a source soft at a quality the video may be rendered at, ``insert_resolution``; 8: the
#: legibility advice offering only a scale the source carries at the video's render quality; 9: no push offered
#: on a source drawn under 0.3x, and ``legibility: ambient`` offered; 10: a video that outgrew its AI track,
#: ``music_outgrown``, and a fading bed's decay under the closing card no quiet stretch; 11: a figure judged as
#: it is drawn in motion; 12: a recording whose scrolls play under 24 fps, ``take_frame_rate``; 13: its
#: transitions too, the step named; 14: a recording that animated under 20 fps, ``take_capture_rate``, and a
#: scene that reads slow, ``pace``; 15: a recording holding on a page still loading, ``take_loading_hold``;
#: 16: a demo's narration, camera and speed, ``demo_narration``, ``demo_camera``, ``demo_speed``; 17: a
#: canvas transition under 20 fps, the GL it was drawn with; 18: the least each template card lasts, a track
#: short of the length the render places; 19: an unkeyed insert's foreground read at the frame's own
#: resolution, the insert seen through it, a ghost mask and the key past the quad gated; 20: a recording whose
#: motion is mostly synthesised from keyframes, ``stepping_motion``; 21: a beat_sync that syncs nothing,
#: ``beat_sync_unsynced``, a pad offered for a track a few seconds short; 22: an AI shot whose frames drifted
#: from one of the video's references, ``reference_drift``; 23: lettering an AI shot invents,
#: ``invented_text``, dev:invented-text-check; 24: ``reference_drift`` read on real footage, the reference located
#: by a viewpoint-robust window and an advisory when it is not seen; 25: each screen insert's coverage - every
#: frame its gates read, the worst and the longest run of failing frames - and a ghost mask on six frames in a row,
#: dev:video-render-wide-insert-gate; 26: ``stepping_motion`` graded as each scene draws it - a brief transition read as
#: motion an advisory, a stretch a slowed scene blends not said - and said on every scene showing it; 27: the
#: screen of a shot carrying a screen insert occluded on its take, ``insert_occluded``, with the trim_end that ends the
#: scene before it; 28: a recording scene crossfading into another of the same recording with two framings of
#: its page blended, ``doubled_ui``; 29: frames drawn at another scale on a step holding the page,
#: ``off_scale_frames``, a capture fault never said as ``stepping_motion``; 30: a logo drawn onto an AI shot that
#: reads as a flat sticker, ``logo_not_integrated``; 31: a take that steps, captured too slowly or kept
#: off-scale frames offered a repair as well as a new recording, ``get_video_recording(repair=true)``; 32: a
#: locked-off shot's camera measured on its background, a subject crossing the frame no camera move; 33: a
#: ``wait`` step's settling page advisory, never dense ``stepping_motion``; 34: a canvas zoom whose frames were
#: drawn as a cross-dissolve of two keyframes, ``double_image``; 35: a warped canvas zoom that held the boxes
#: over its canvas from the keyframe before while the canvas came from the one after, ``double_image``; 36: an
#: unkeyed insert's mask past its subject and a legible insert hidden by what stands in front, ``insert_matte_halo``,
#: ``insert_matte_lag``, ``insert_occluded``; 37: a slowed recording scene shows one painted frame at a time,
#: never a blend of two: its stepping stretches said, slowing never offered as a fix; 38: the brand's mark twice
#: on a drawn frame, ``duplicate_logo``, and a scene's ``watermark: false``; 39: a recording frame of a render or a
#: preview drawn as two pictures at once, ``double_image`` read render-wide on the encoded frames; 40: the corner
#: watermark read where and at the size the page draws it, and a logo up to the frame's width, for ``duplicate_logo``;
#: 41: a flat mark matched by its shape over the whole frame held to ``logos.MARK_MIN_SHAPE_MATCH`` and a matte gate
#: read on guessed masks alone an advisory, dev:render-picture-check-gate-drift; 42: a matte gate on an insert whose mask is
#: guessed somewhere blocks only on a patch a viewer sees, a brand mark with no contrast in its box is no mark, a picture
#: finding on frames a passed render drew alike under encoder noise an advisory, dev:render-picture-check-gate-drift-2,
#: and the checks of a render name the version they were judged at; 43: a keyed insert's presence and guard read the
#: key's colour inside its quad past the edge strip along the bezel, the bezel's own glow never an insert missing or
#: dropped, and the finding names where the colour sat; 44: a pocket of the key left beside what stands in
#: front of a keyed insert fails the residual key; 45: a scene drawing a brand image overlay under the watermark, a
#: statement card included, read by ``duplicate_logo``; 46: that finding names the brand overlay, by its asset, only on
#: a frame inside the overlay's window; 47: a ``duplicate_logo`` place over a region an AI shot keeps from its start
#: picture counts only when the template's mark is matched inside it, and the finding names the file and score of each
#: place; 48: a ``duplicate_logo`` place counts only when the mark is matched again on its own box at full size, and a
#: scene the page draws no brand mark over (watermark off, no brand overlay) reports its footage's marks as a note; 49:
#: a cut lands on a whole output frame, no motion-blur shutter crosses it, and every cut join of a render and a preview
#: is read at its own frame rate, a frame of both scenes a ``double_image`` (``cut_blend``), issue #8721; 50: what
#: the cut gate read is stored as ``checks.cuts`` and said in a ``Cuts:`` fact, a clean pass told from a gate that never
#: ran, issue #8935; 51: a narration's subtitle speed judged on the reading pace, its voice not spoken yet, is an
#: advisory, and a narration whose words leave no length for both its subtitles and its pace says so, issue #9062; 52:
#: a frame the insert sweep frames failing a gate is that gate's finding or an advisory under its tolerance said so,
#: two inserts back to back on one surface leaving it bare are ``insert_gap``, and a line of the key along an edge of a
#: keyed insert fails ``insert_residual_key``, issue #9065; 53: perceptual gates, issue #10036: a narration that
#: sounds synthetic, ``synthetic_voice``, a time-stretched narration under speed 0.95, an AI shot frame whose flat
#: areas carry texture noise, ``texture_noise``, an insert's UI text judged whatever its legibility label, and a
#: recording painted under 15 fps refused as an insert source, ``insert_capture_floor``; each blocking on a brand film
#: or a product demo template, advice elsewhere; 54: lettering under a screen insert is never read, and a take
#: promoted with accept_attempt carries no invented word under its insert, issue #10056; 55: ``legibility: ambient``
#: honoured on scenery - an ambient recording insert is advice only when drawn at 0.3x or more, alone on its frame and
#: not under a statement, never counted by the render gate, issue #10093; 56: texture_noise reads uncorrelated noise,
#: never natural texture, judged against the shot's start picture when it has one and an absolute floor when the
#: region covers most of the frame, issue #10083; 57: a transition a scene shows drawn over 30% from keyframes or
#: held then cut, ``synthesised_transition``, blocking, issue #10867; 58: the grey the compositing drew over a keyed
#: insert past what stands in front, off the keyed surface where the footage had colour, fails ``insert_rim``, issue
#: #11166; 59: an unkeyed contain insert whose bands show the screen's chroma key colour fails
#: ``insert_band_key_colour``, issue #11271; 60: a recording scene whose kept seconds play its page still loading,
#: ``scene_loading_span``, issue #11377): a report kept by another
#: release is
#: never read back, nor shown again by the studio, which keys a revision's report by it (``quality_version`` of the
#: project).
QUALITY_REPORT_VERSION = 60
#: Findings shown to the agent per check - the worst first; the rest are counted.
MAX_REPORTED_FINDINGS = 8
#: A scene's text scaled below this to fit is dense, below the second
#: threshold overloaded: a subtitle-sized line no one reads on a phone.
FIT_DENSE_BELOW = 0.85
FIT_OVERLOADED_BELOW = 0.7
#: On-screen reading: a glance to find the text, then about 210 words a minute.
READ_SETTLE_SECONDS = 1.0
WORDS_READ_PER_SECOND = 3.5
#: How much shorter than its reading takes a text scene's words may stay on screen before ``reading`` finds it.
READING_TOLERANCE_SECONDS = 0.25
#: Subtitles a viewer follows comfortably, and the speed past which they cannot.
CAPTION_COMFORT_CPS = 17
CAPTION_MAX_CPS = 20
#: A narration read slower than this, in words a minute, reads slow (``mix_check.SLOW_WPM``), from this many words on
#: (``mix_check.PACE_MIN_WORDS``): with its subtitles under :data:`CAPTION_MAX_CPS`, a narration of more characters a
#: word than ``CAPTION_MAX_CPS * 60 / CAPTION_SLOW_WPM`` has no length that clears both.
CAPTION_SLOW_WPM = 110.0
CAPTION_PACE_MIN_WORDS = 6
#: Words over a video or image darkened less than this read on some frames only.
BACKDROP_MIN_DIM = 0.3
#: A locked-off shot's last frame keeps at least this much of its first's structure (SSIM, ``frame_qc``):
#: under it the camera moved.
LOCKED_SHOT_MIN_SSIM = 0.5
#: An image drawn more than this many times its own size reads soft; past the second, blurred.
UPSCALE_SOFT, UPSCALE_BLURRED = 1.1, 1.5
#: How far past the footage a screen insert's corner may stand before the frame cuts it (a share of the footage).
INSERT_EDGE_TOLERANCE = 0.005
#: A screen insert whose quad shows less than this share of itself in the frame is on a surface the frame does not
#: show: a finding the audience would see. One partly out of frame is drawn through its visible part: advice.
INSERT_MIN_VISIBLE_SHARE = 0.5
#: Footage still holding its last frame this long once the planner slowed it to fill its scene,
#: beyond the hold its author asked for: said, with what makes the footage long enough.
HELD_FRAME_SECONDS = 1.5

FindingKind = Literal[
    "overlap",
    "outside",
    "fit",
    "rushed",
    "reading",
    "caption_lines",
    "caption_speed",
    "backdrop",
    "overlay_short",
    "shot_state",
    "camera_moved",
    "image_resolution",
    "insert_outside",
    "music_short",
    "music_outgrown",
    "music_level",
    "narration_voice",
    "held_frame",
    "pronunciation",
    "brand_repeated",
    "voice_fallback",
    "brand_mark_twice",
    "frozen_hold",
    "insert_bezel",
    "insert_flatness",
    "insert_loop_seam",
    "insert_legibility",
    "insert_resolution",
    "take_frame_rate",
    "take_capture_rate",
    "stepping_motion",
    "take_loading_hold",
    "scene_loading_span",
    "pace",
    "demo_narration",
    "demo_camera",
    "demo_speed",
    "beat_sync_unsynced",
    "reference_drift",
    "invented_text",
    "insert_occluded",
    "doubled_ui",
    "off_scale_frames",
    "logo_not_integrated",
    "double_image",
    "synthetic_voice",
    "texture_noise",
    "insert_capture_floor",
    "synthesised_transition",
]
#: ``overloaded`` is a frame the audience cannot read, ``dense`` one they read
#: with difficulty; ``advisory`` reads fine and could be better.
Severity = Literal["overloaded", "dense", "advisory"]

_SEVERITY_ORDER: dict[str, int] = {"overloaded": 0, "dense": 1, "advisory": 2}
_TEXT_SCENES = frozenset({"title", "outro", "statement", "motion"})
_OVERLAYS = frozenset({"lower third", "badge", "callout", "caption overlay", "image overlay"})
_OVERLAY_NAMES = {
    "lower_third": "lower third",
    "badge": "badge",
    "callout": "callout",
    "caption": "caption overlay",
    "image": "image overlay",
}


@dataclass(frozen=True, slots=True)
class QualityFinding:
    """One thing the audience would see go wrong in one scene (or in the whole video)."""

    #: The scene's index in the spec as written (the one ``edit_video``
    #: operations use); ``None`` for a scene the template adds.
    scene_index: int | None
    scene_id: str | None
    scene_type: str | None
    label: str
    kind: FindingKind
    severity: Severity
    message: str
    #: ``content``: about the pictures and the sound rather than the text's layout - most read from the media
    #: (:func:`content_findings`) and kept when the layout is not measured, the brand mark drawn twice measured with
    #: it; the editor words its severity for that.
    group: Literal["layout", "content"] = "layout"
    #: The AI shot the finding reads, when it is about the shot itself (its camera, its state): a passing
    #: preview of every scene showing it clears it at the render gate (``render_checks.cleared_shots``).
    clip_id: str | None = None
    #: The screen insert surface and the source it is about (``insert_legibility.Legibility.identity``): a save's
    #: answer says its legibility advisory once (``insert_legibility.said_once``).
    insert: str | None = None
    #: What the finding measured, for the studio and the agent to act on: a ``reference_drift``'s reference,
    #: its measures and the crop they were taken on (``reference_drift.Drift.as_dict``); ``None`` for the rest.
    data: dict | None = None

    def as_dict(self) -> dict:
        return {field.name: getattr(self, field.name) for field in fields(self)}


@dataclass(frozen=True, slots=True)
class QualityReport:
    """The outcome of one check: findings, or the reason there are none."""

    scene_count: int
    findings: tuple[QualityFinding, ...] = ()
    #: Set when the layout was not measured; the findings are then the pictures' and the sound's only.
    skipped: str | None = None
    revision: int | None = None
    #: What a check does not judge because the video declares it so - an ambient screen insert's text, its size
    #: said as a fact (``insert_legibility.unchecked``): said, never a finding.
    unchecked: tuple[str, ...] = ()
    #: What the check says of the video as a whole, never a finding: its pace (``pace.PaceReport.summary``), the
    #: least its template's cards last (:func:`card_minimums`), a ``beat_sync`` that lands on no beat.
    notes: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return self.skipped is None and not self.findings

    def as_dict(self) -> dict:
        return {
            "scene_count": self.scene_count,
            "skipped": self.skipped,
            "revision": self.revision,
            "findings": [finding.as_dict() for finding in self.findings],
            "unchecked": list(self.unchecked),
            "notes": list(self.notes),
            "version": QUALITY_REPORT_VERSION,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> QualityReport:
        names = {field.name for field in fields(QualityFinding)}
        findings = tuple(
            QualityFinding(**{key: value for key, value in item.items() if key in names})
            for item in payload.get("findings") or []
            if isinstance(item, dict)
        )
        return cls(
            scene_count=int(payload.get("scene_count") or 0),
            findings=findings,
            skipped=payload.get("skipped"),
            revision=payload.get("revision"),
            unchecked=tuple(str(item) for item in payload.get("unchecked") or ()),
            notes=tuple(str(item) for item in payload.get("notes") or ()),
        )


def quality_check_enabled() -> bool:
    return bool(settings.video_quality_check_enabled)


# ── What a scene says ────────────────────────────────────────────────────


def _plain(text: object) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("*", "")).strip()


def _words(*texts: object) -> int:
    return sum(len(re.findall(r"\w+", _plain(text))) for text in texts)


def _scene_words(scene: dict) -> int:
    if scene.get("type") == "statement":
        return _words(
            scene.get("kicker"), scene.get("figure"), *(scene.get("lines") or []), *(scene.get("secondary") or [])
        ) + _words(*(scene.get("chips") or []))
    if scene.get("type") == "motion":
        # What its elements write, its composition's texts included.
        return _words(*((element.get("text") or {}).get("text") for element in scene.get("elements") or []))
    return _words(scene.get("kicker"), scene.get("title"), scene.get("subtitle"), scene.get("cta"), scene.get("url"))


def _label(scene: dict) -> str:
    kind = scene.get("type")
    if kind in ("title", "outro"):
        text = _plain(scene.get("title"))
    elif kind == "statement":
        text = " / ".join(_plain(line) for line in scene.get("lines") or [])
    elif kind == "motion":
        text = next((_plain((e.get("text") or {}).get("text")) for e in scene.get("elements") or [] if e.get("text")), "")
    else:
        text = ""
    return text if len(text) <= 60 else text[:57] + "..."


def _seconds(value: float) -> str:
    return f"{value:.1f} s"


def _suggest(seconds: float) -> float:
    """A duration to ask for: rounded up to the half second."""
    return math.ceil(seconds * 2) / 2


class _Scenes:
    """The planned scenes, each tied to its index in the spec as written."""

    def __init__(self, document: dict, authored: dict | None) -> None:
        self.planned: list[dict] = [s for s in document.get("scenes") or [] if isinstance(s, dict)]
        written = (authored or {}).get("scenes") or []
        self.positions = {s.get("id"): i for i, s in enumerate(written) if isinstance(s, dict) and s.get("id")}
        self.authored = authored is not None

    def finding(
        self,
        planned_index: int,
        kind: FindingKind,
        severity: Severity,
        message: str,
        *,
        content: bool = False,
        clip_id: str | None = None,
        insert: str | None = None,
        data: dict | None = None,
    ) -> QualityFinding:
        scene = self.planned[planned_index] if 0 <= planned_index < len(self.planned) else {}
        sid = scene.get("id")
        return QualityFinding(
            scene_index=self.positions.get(sid),
            scene_id=sid,
            scene_type=scene.get("type"),
            label=_label(scene),
            kind=kind,
            severity=severity,
            message=message,
            group="content" if content else "layout",
            clip_id=clip_id,
            insert=insert,
            data=data,
        )

    def length_field(self, planned_index: int) -> str:
        """The field that sets the planned scene's length: its ``duration``, else a template card's.

        A card the template adds is not in the spec as written: its length is
        ``template_scenes.<intro|outro>.duration``.
        """
        from app.services.video_studio.templates import TEMPLATE_CARDS, card_name

        scene = self.planned[planned_index] if 0 <= planned_index < len(self.planned) else {}
        added = self.authored and scene.get("id") not in self.positions and scene.get("type") in TEMPLATE_CARDS.values()
        return f"template_scenes.{card_name(scene.get('type'))}.duration" if added else "duration"

    def at(self, t: float) -> int:
        """The planned scene shown at time *t* (the later one during a transition)."""
        found = 0
        for index, scene in enumerate(self.planned):
            if float(scene.get("start") or 0) <= t:
                found = index
        return found


# ── What the timeline alone tells ────────────────────────────────────────


def _caption_speed(cps: float, said: list[str], estimated: bool) -> tuple[Severity, str]:
    """The severity and words of a scene whose subtitles run at *cps* characters a second, *said* its cue texts.

    Planned at the reading pace (*estimated*: its voice not spoken yet at its speed), the speed is the planner's guess,
    not the voice's: an advisory, judged again once spoken. A narration whose characters a word leave no length
    that keeps its subtitles readable and its pace over :data:`CAPTION_SLOW_WPM` says so, with the fix that is not
    its speed.
    """
    measured = f"its subtitles run at {cps:.0f} characters a second (comfortable: up to {CAPTION_COMFORT_CPS})"
    if estimated:
        return (
            "advisory",
            f"{measured} at the reading pace, its voice not spoken yet at its speed - judged again on the spoken "
            "voice; if it stays fast, shorten its narration or slow the voice (narration_speed, voiceover.speed)",
        )
    chars, words = sum(len(_plain(text)) for text in said), _words(*said)
    longest = words * 60.0 / CAPTION_SLOW_WPM
    shortest = chars / CAPTION_MAX_CPS
    if words >= CAPTION_PACE_MIN_WORDS and shortest > longest:
        return (
            "dense",
            f"{measured}, and no speed clears both its subtitles and its pace: its {words} words ({chars} characters) "
            f"need {shortest:.1f} s on screen to read, but read over more than {longest:.1f} s fall under "
            f"{CAPTION_SLOW_WPM:.0f} words a minute - reword it with shorter words, or more of them",
        )
    return "dense", f"{measured} - shorten its narration or slow the voice (narration_speed, voiceover.speed)"


def document_findings(
    document: dict, authored: dict | None, *, estimated: frozenset[str] | set[str] = frozenset()
) -> list[QualityFinding]:
    """Findings read off the planned document, no browser: subtitle speed, backdrops, overlay timing.

    *estimated*: the scenes whose narration is planned at the reading pace while the video has a voice that has not
    spoken it yet (:func:`estimated_narrations`): their subtitle speed is the planner's guess, an advisory.
    """
    scenes = _Scenes(document, authored)
    findings: list[QualityFinding] = []
    cues = [q for q in (document.get("captions") or {}).get("cues") or [] if isinstance(q, list) and len(q) >= 3]
    fastest: dict[int, float] = {}
    said: dict[int, list[str]] = {}
    for t0, t1, text in (q[:3] for q in cues):
        span = float(t1) - float(t0)
        if span <= 0:
            continue
        index = scenes.at(float(t0))
        fastest[index] = max(fastest.get(index, 0.0), len(_plain(text)) / span)
        said.setdefault(index, []).append(str(text))
    for index, cps in sorted(fastest.items()):
        if cps > CAPTION_MAX_CPS:
            scene_id = scenes.planned[index].get("id") if index < len(scenes.planned) else None
            severity, message = _caption_speed(cps, said.get(index, []), scene_id in estimated)
            findings.append(scenes.finding(index, "caption_speed", severity, message))
    for index, scene in enumerate(scenes.planned):
        backdrop = scene.get("backdrop") if scene.get("type") == "statement" else None
        if isinstance(backdrop, dict) and float(backdrop.get("dim") or 0) < BACKDROP_MIN_DIM:
            what = "video" if backdrop.get("kind") == "video" else "picture"
            findings.append(
                scenes.finding(
                    index,
                    "backdrop",
                    "advisory",
                    f"its words sit over a {what} darkened only to {float(backdrop.get('dim') or 0):.2f} - "
                    "raise dim to 0.45 or more so they read on every frame",
                )
            )
        for overlay in scene.get("overlays") or []:
            # A picture's text is its label, never read on screen.
            if not isinstance(overlay, dict) or overlay.get("type") in ("spotlight", "image"):
                continue
            words = _words(overlay.get("text"), overlay.get("subtext"))
            shown = float(overlay.get("t1") or 0) - float(overlay.get("t0") or 0)
            need = 0.6 + words / WORDS_READ_PER_SECOND
            if words and shown + 0.05 < need:
                name = _OVERLAY_NAMES.get(str(overlay.get("type")), "overlay")
                findings.append(
                    scenes.finding(
                        index,
                        "overlay_short",
                        "advisory",
                        f'the {name} "{_plain(overlay.get("text"))[:50]}" shows for {_seconds(shown)} - too short to '
                        f"read; give it {_suggest(need):g} s (its duration)",
                    )
                )
    return findings


# ── What the browser measured ────────────────────────────────────────────


def _overlap_remedy(block: str, other: str) -> str:
    pair = {block, other}
    if "watermark" in pair:
        return "move the watermark to another corner (theme.watermark) or the overlay elsewhere"
    if "subtitles" in pair and pair & _OVERLAYS:
        return "give the overlay another position or step, or show it while no one speaks"
    if "subtitles" in pair:
        return "shorten the scene's text so it fits above the subtitles, or put the subtitles at the top (captions.position)"
    if pair <= _OVERLAYS:
        return "show them one after the other (start, duration) or at different positions"
    if pair & _OVERLAYS:
        return "give the overlay another position or time, or drop it - the scene already says it"
    return "move or shorten one of them"


def _quoted(name: str, text: str) -> str:
    return f'the {name} "{text}"' if text else f"the {name}"


def _brand_mark(mark: dict, scene_id: object) -> tuple[str, str]:
    """``(what draws it, how it is dropped)`` of one brand mark the audit counted on a frame."""
    drawn = str(mark.get("mark") or "")
    where = mark.get("scene")
    own = where in (None, scene_id)
    whose = "its" if own else f"scene {where}'s"
    if drawn == "watermark":
        return "the watermark", "theme.watermark: none"
    if drawn == "logo":
        return f"{whose} logo", "show_logo: false" + ("" if own else f" on scene {where}")
    asset = drawn.removeprefix("overlay:")
    return f"{whose} image overlay of the template's {asset}", f"drop {whose} image overlay of the {asset}"


def _brand_twice(observed: dict, planned: dict, start: float) -> str:
    """The words of a frame drawing the template's brand mark twice: which marks, when, and how to keep one."""
    from app.services.video_studio.render_checks import clock

    marks = [_brand_mark(mark, planned.get("id")) for mark in observed.get("marks") or [] if isinstance(mark, dict)]
    at = float(observed.get("at") or 0.0)
    video_at = observed.get("video_at")
    when = clock(float(video_at) if isinstance(video_at, int | float) else start + at)
    drawn = " and ".join(what for what, _drop in marks) or "two marks"
    keep = "; ".join(drop for _what, drop in marks)
    return (
        f"brand mark drawn twice on the frame at {when} ({_seconds(at)} into the scene): {drawn} draw the brand on "
        f"the same frame - keep one ({keep})"
    )


def _readable_length(observed: dict, planned: dict) -> float:
    """How long the scene *observed* (an ``audit()`` scene) must last for its words to be read once all are in: its
    reveal, a glance, its words at the reading pace, and the end it is no longer shown at (a fade, the next scene's
    transition) - what the ``rushed`` and ``reading`` findings ask for, and a template card's minimum."""
    end = float(observed.get("end") or 0)
    until = float(observed.get("shown_until") or end)
    reveal_end = float(observed.get("reveal_end") or 0)
    return reveal_end + (READ_SETTLE_SECONDS + _scene_words(planned) / WORDS_READ_PER_SECOND) + (end - until)


@dataclass(frozen=True)
class CardLeast:
    """A card the template adds, and the least it lasts with its words read (:func:`card_leasts`)."""

    #: Its ``template_scenes`` entry (``intro``, ``outro``) and its scene id.
    card: str
    scene_id: str
    least: float
    lasts: float
    words: int


def card_leasts(audit: dict, document: dict, authored: dict | None) -> list[CardLeast]:
    """The least each card the template adds lasts with its words read, measured on *audit* as the ``reading``
    finding measures it - the shortest length it does not refuse, rounded up to the tenth, 1 s at least (a card's
    shortest ``duration``). None without such a card, or for a spec not given as written."""
    from app.services.video_studio.templates import TEMPLATE_CARDS, card_name

    scenes = _Scenes(document, authored)
    found: list[CardLeast] = []
    for observed in (audit or {}).get("scenes") or []:
        if not isinstance(observed, dict):
            continue
        index = int(observed.get("index") or 0)
        planned = scenes.planned[index] if index < len(scenes.planned) else {}
        added = scenes.authored and planned.get("id") not in scenes.positions
        if not added or planned.get("type") not in TEMPLATE_CARDS.values() or not (words := _scene_words(planned)):
            continue
        least = math.ceil((_readable_length(observed, planned) - READING_TOLERANCE_SECONDS) * 10 - 1e-6) / 10
        lasts = float(observed.get("end") or 0) - float(observed.get("start") or 0)
        found.append(CardLeast(card_name(planned.get("type")), str(planned.get("id") or ""), max(1.0, least), lasts, words))
    return found


def card_minimums(audit: dict, document: dict, authored: dict | None) -> str:
    """:func:`card_leasts` as every save says them, so ``template_scenes.<card>.duration`` is known before a
    save refuses a shorter one. Empty without such a card, or for a spec not given as written."""
    said = [
        f"template_scenes.{card.card}.duration at least {card.least:g} s for its {card.words} words "
        f"(it lasts {round(card.lasts, 1):g} s)"
        for card in card_leasts(audit, document, authored)
    ]
    if not said:
        return ""
    return "The template's cards, the least each lasts with its words read: " + "; ".join(said) + "."


def estimated_narrations(preview: dict) -> frozenset[str]:
    """The scenes of a *preview* (``preview.build_preview``) whose narration is planned at the reading pace while its
    voice is on and has not spoken it yet: what their subtitles' speed is a guess for. None with the voice off: the
    subtitles then play at the reading pace."""
    if (preview.get("voice") or {}).get("state") != "on":
        return frozenset()
    return frozenset(
        str(line.get("scene_id"))
        for line in preview.get("narration") or []
        if isinstance(line, dict) and line.get("state") != "ready"
    )


def findings_from_audit(
    audit: dict, document: dict, authored: dict | None, *, estimated: frozenset[str] | set[str] = frozenset()
) -> list[QualityFinding]:
    """Turn ``window.__xtm.audit()``'s observations and the planned document into findings.

    Pure: no browser, no database. Sorted worst first (overloaded before
    dense before advisory, then scene order) so a truncated report still
    shows what matters most. *estimated*: as :func:`document_findings`.
    """
    scenes = _Scenes(document, authored)
    cues = [q for q in (document.get("captions") or {}).get("cues") or [] if isinstance(q, list) and len(q) >= 3]
    findings: list[QualityFinding] = []
    for observed in (audit or {}).get("scenes") or []:
        if not isinstance(observed, dict):
            continue
        index = int(observed.get("index") or 0)
        planned = scenes.planned[index] if index < len(scenes.planned) else {}
        start, end = float(observed.get("start") or 0), float(observed.get("end") or 0)
        until = float(observed.get("shown_until") or end)
        reveal_end = float(observed.get("reveal_end") or 0)
        raw = [o for o in observed.get("findings") or [] if isinstance(o, dict)]
        rushed = False
        for o in raw:
            kind = o.get("kind")
            if kind == "overlap":
                block, other = str(o.get("block") or "text"), str(o.get("other") or "text")
                findings.append(
                    scenes.finding(
                        index,
                        "overlap",
                        "overloaded",
                        f"{_quoted(block, str(o.get('text') or ''))} covers {_quoted(other, str(o.get('other_text') or ''))} "
                        f"at {_seconds(float(o.get('at') or 0))} - {_overlap_remedy(block, other)}",
                    )
                )
            elif kind == "outside":
                past = int(o.get("amount") or 0)
                side = o.get("side") or "nearest"
                where = (
                    f"runs {past} px past the {side} edge of the frame"
                    if past > 0
                    else f"touches the {side} edge of the frame"
                )
                findings.append(
                    scenes.finding(
                        index,
                        "outside",
                        "overloaded" if past > 0 else "dense",
                        f"{_quoted(str(o.get('block') or 'text'), str(o.get('text') or ''))} {where} - shorten it",
                    )
                )
            elif kind == "brand_twice":
                findings.append(
                    scenes.finding(index, "brand_mark_twice", "dense", _brand_twice(o, planned, start), content=True)
                )
            elif kind == "rushed" and not rushed:
                rushed = True
                need = _readable_length(observed, planned)
                findings.append(
                    scenes.finding(
                        index,
                        "rushed",
                        "dense",
                        f"{_quoted(str(o.get('block') or 'text'), str(o.get('text') or ''))} is still coming in when the "
                        f"scene ends - make the scene at least {_suggest(need):g} s long ({scenes.length_field(index)}) "
                        "or cut words",
                    )
                )
        fit = float(observed.get("fit") or 1)
        if fit < FIT_DENSE_BELOW:
            subtitled = any(float(q[1]) > start and float(q[0]) < end for q in cues)
            findings.append(
                scenes.finding(
                    index,
                    "fit",
                    "overloaded" if fit < FIT_OVERLOADED_BELOW else "dense",
                    f"its text is scaled down to {round(fit * 100)}% to fit "
                    f"{'above the subtitles' if subtitled else 'the frame'} - cut words, split it into two scenes, "
                    "or say the detail in the narration",
                )
            )
        if planned.get("type") in _TEXT_SCENES and not rushed:
            words = _scene_words(planned)
            visible = until - start - reveal_end
            need = READ_SETTLE_SECONDS + words / WORDS_READ_PER_SECOND
            if words and visible + READING_TOLERANCE_SECONDS < need:
                findings.append(
                    scenes.finding(
                        index,
                        "reading",
                        "dense",
                        f"its {words} words are all on screen for {_seconds(max(visible, 0))}; reading them takes about "
                        f"{_seconds(need)} - make the scene {_suggest(_readable_length(observed, planned)):g} s long "
                        f"({scenes.length_field(index)}) or cut words",
                    )
                )
    for item in (audit or {}).get("captions") or []:
        if not isinstance(item, dict) or int(item.get("lines") or 0) <= 2:
            continue
        cue_index = int(item.get("index") or 0)
        if cue_index >= len(cues):
            continue
        t0, _t1, text = cues[cue_index][:3]
        findings.append(
            scenes.finding(
                scenes.at(float(t0)),
                "caption_lines",
                "dense",
                f'a subtitle runs over {int(item["lines"])} lines even at a smaller size ("{_plain(text)[:60]}") - '
                "shorten that sentence of the narration",
            )
        )
    findings.extend(document_findings(document, authored, estimated=estimated))
    return _worst_first(findings)


def _worst_first(findings: list[QualityFinding]) -> list[QualityFinding]:
    """*findings* sorted worst first (overloaded before dense before advisory), then in scene order."""
    order = {id(f): n for n, f in enumerate(findings)}
    return sorted(
        findings,
        key=lambda f: (
            _SEVERITY_ORDER.get(f.severity, 9),
            f.scene_index if f.scene_index is not None else 999,
            order[id(f)],
        ),
    )


# ── What the media tell ──────────────────────────────────────────


@dataclass(frozen=True)
class ShotFacts:
    """What the database says of an AI shot a video shows."""

    name: str
    #: Its job status, ``expired`` (its media gone with the retention), ``deleted``, or ``regenerating``
    #: (a ready shot generated again, its previous take playing meanwhile).
    status: str
    error: str | None = None
    width: int | None = None
    height: int | None = None
    #: The camera its prompt asks for (``content_drift.CAMERAS``), ``None`` when never read.
    camera: str | None = None
    #: Its last frame's similarity with its first (``frame_qc``), ``None`` when not measured.
    motion_ssim: float | None = None
    #: How far its camera moved, measured on the background (``frame_qc.CAMERA_MOTION``); ``None`` when not.
    camera_motion: float | None = None
    #: Why its last regeneration failed, its previous take still playing; ``None`` otherwise.
    regeneration_failed: str | None = None
    #: How much its last frame may change from its first (``frame_qc.MAX_CAMERA_MOTION``).
    max_camera_motion: float | None = None
    #: A screen replacement: its screen was followed frame by frame, so the camera moving is no defect.
    screen: bool = False
    #: The file of the take that plays: what a failed regeneration's advisory names.
    playing: str | None = None
    #: Lettering the take invents (``text_drift.Invented``, as its text check recorded them): gibberish pills and
    #: subtitles the start picture never had. Blocking, unless the shot was asked with ``text_check: false``.
    invented: tuple[Any, ...] = ()
    #: Whether the shot was asked with ``text_check: false``: its invented lettering is then advisory.
    text_check: bool = True
    #: The take kept no text check record (made before the check, or skipped): the quality check reads its frames
    #: itself (``text_backfill``) and :attr:`invented` holds what it found; ``None`` for a take checked as made.
    unchecked: Any = None
    #: What stands in front of the screen of a shot carrying a screen insert over its take, as measured when it was
    #: made (``frame_qc.FOREGROUND_OVERLAP``, ``insert_keep_out.Overlap.as_dict``); ``None`` when not measured.
    occlusion: dict | None = None
    #: How the logo drawn onto its take reads against its surface (``frame_qc.logo_gate``'s ``integration``,
    #: ``logo_integration.Integration.record``): composited or tracked; ``None`` when no logo was drawn onto it.
    logo_integration: dict | None = None
    #: How the logo reached the take (``clips.INGREDIENTS_PATH``): ``composited``, ``tracked``, ``native``.
    ingredients_path: str | None = None


def _logo_integration_words(shot: ShotFacts) -> tuple[Severity, str] | None:
    """``(severity, words)`` of a logo drawn onto *shot*'s take that is not integrated:
    ``dense`` - a render refuses it - unless the shot was asked with ``ingredients_quality: "flat"`` (then
    ``advisory``); ``None`` when its logo reads as part of its surface, or none was drawn onto it."""
    record = shot.logo_integration
    if not isinstance(record, dict) or record.get("passed", True):
        return None
    from app.services.video_studio import logo_integration

    path = f" (ingredients_path: {shot.ingredients_path})" if shot.ingredients_path else ""
    measure = str(record.get("failed") or "")
    where = f"frame {record.get('frame')} of the shot, {float(record.get('seconds') or 0.0):.2f} s"
    crop = f", frame `{record['frame_file_id']}`" if record.get("frame_file_id") else ""
    told = (
        f"draws {record.get('label') or 'its logo'} as a flat sticker{path}: "
        f"{logo_integration.MEASURE_WORDS.get(measure, measure)}, integration score "
        f"{float(record.get('score') or 0.0):.2f} under {logo_integration.MIN_SCORE:g} ({where}{crop})"
    )
    if record.get("waived"):
        return "advisory", f'{told} - accepted, the shot was asked with ingredients_quality: "flat"'
    return (
        "dense",
        f"{told} - the audience sees it pasted on; a render refuses it. Generate the shot again with a model that "
        'takes the logo as a picture, or set ingredients_quality: "flat" on the shot to accept it',
    )


def camera_moved(shot: ShotFacts) -> bool:
    """Whether *shot*'s camera moved more than it may: its own ``max_camera_motion``, else a locked-off prompt's.

    A screen replacement without a tolerance never: its insert follows the camera.
    """
    if shot.camera_motion is not None:
        # The camera itself, measured on the background: a subject crossing a still frame is no move.
        if shot.max_camera_motion is not None:
            return shot.camera_motion > shot.max_camera_motion
        return not shot.screen and shot.camera == "locked" and shot.camera_motion > 1.0 - LOCKED_SHOT_MIN_SSIM
    if shot.motion_ssim is None:
        return False
    if shot.max_camera_motion is not None:
        return 1.0 - shot.motion_ssim > shot.max_camera_motion
    return not shot.screen and shot.camera == "locked" and shot.motion_ssim < LOCKED_SHOT_MIN_SSIM


@dataclass
class ContentFacts:
    """What the content findings read from the database, gathered by :func:`content_facts`."""

    shots: dict[str, ShotFacts] = field(default_factory=dict)
    #: ``file id -> (width, height)`` of the raster pictures of image scenes.
    images: dict[str, tuple[int, int]] = field(default_factory=dict)
    #: How long the music's source plays (``music.playable_seconds``); ``None`` when it fits by nature or is unknown.
    music_seconds: float | None = None
    #: The AI track is still being composed, at :attr:`music_seconds` (the length it was asked for).
    music_composing: bool = False
    #: How long the video's current render of this revision lasts (``projects.rendered_seconds``): the length
    #: the music must cover when it came out longer than the plan; ``None`` without one.
    rendered_seconds: float | None = None
    #: Why a ``beat_sync`` video lands nothing on a beat at all - no music, music with no steady tempo
    #: (``beats.SyncsNothing``): the ``beat_sync_unsynced`` finding; ``None`` when it syncs, or does not ask.
    beat_unsynced: str | None = None
    #: How loud the music plays (``music_level.measure_planned``); ``None`` when none plays or it was not measured.
    music_level: object | None = None
    #: Why no voice can speak the narration (no speech model the person may use), ``None`` when one can.
    voice_unavailable: str | None = None
    #: The recordings made: the plan has their real length (one still being recorded is estimated).
    ready_takes: set[str] = field(default_factory=set)
    #: The frame a render draws at the default quality.
    output: tuple[int, int] = (1920, 1080)
    quality: str = "high"
    #: The pronunciation lexicon the narration is said with (the platform's entries under the video's own).
    lexicon: object | None = None
    #: The template the video wears, its brand marks read (``templates.load_video_template``); ``None``: free style.
    template: object | None = None
    #: ``scene id -> voice_check.VoiceChange`` of each narration placed in another voice than the video's.
    voice_changes: dict[str, object] = field(default_factory=dict)
    #: ``scene id -> (timbre, timbre at speed 1 or None)`` of each narration whose check kept one
    #: (``voice_check.narration_timbres``): what ``synthetic_voice`` reads.
    timbres: dict[str, tuple] = field(default_factory=dict)
    #: ``clip id -> (frame file id, perceptual.TextureNoise)`` of each AI shot whose frames carry texture noise.
    textures: dict[str, tuple] = field(default_factory=dict)
    #: Each narration placed with a fallback take of the video's own voice - slower, a term set apart
    #: (``voice_check.FallbackTake``): said as a fact with the fix that would clear it, never a finding.
    fallback_takes: list = field(default_factory=list)
    #: ``scene id -> seconds`` of each narration the plan placed at its spoken length
    #: (``preview.spoken_lengths``): the others at the reading pace.
    spoken: dict[str, float] = field(default_factory=dict)
    #: The recordings made, as the planner reads them (``timeline.TakeMedia``): the speed that fills a scene.
    takes: dict[str, object] = field(default_factory=dict)
    #: Each static screen insert measured on its first footage frame (``insert_bezels.Measured``).
    bezels: list = field(default_factory=list)
    #: ``clip id -> {render_id, revision, scenes}`` of each AI shot every scene showing it passed a preview of, as it
    #: is now (``render_checks.cleared_by``): its own findings are cleared, said with what cleared them.
    cleared: dict[str, dict] = field(default_factory=dict)
    #: Each screen insert surface showing a recording or a file, its text as drawn (``insert_legibility``).
    legibility: list = field(default_factory=list)
    #: Each surface a whole render would refuse as soft at a quality a video may be rendered at
    #: (``insert_checks.SoftAt``).
    soft: list = field(default_factory=list)
    #: ``scene id -> figure_fit.FigureFit`` of each figure whose still is drawn: judged as it is drawn in motion.
    figures: dict = field(default_factory=dict)
    #: ``scene id -> (start, end)`` of each narration as planned, in seconds of the video: what the pace
    #: advisory reads the still screen under (``pace.report``).
    narration: dict = field(default_factory=dict)
    #: ``scene id -> what its frames show`` while its narration is said (``demo_grammar.frame_texts``): the
    #: labels a demo's narration may read off its screen.
    frame_text: dict = field(default_factory=dict)
    #: ``clip id -> reference_drift.Drift`` of each AI shot whose kept frames drifted from one of the video's
    #: references its scene names in ``consistency`` (``reference_drift.drifts_of``): a shot read against none,
    #: or whose frames could not be read, is absent.
    reference_drifts: dict[str, list] = field(default_factory=dict)


_CORNERS = ("top left", "top right", "bottom right", "bottom left")
_SHOT_GONE = ("failed", "cancelled", "expired", "deleted")
_SHOT_MAKING = ("pending", "running")
_SHOT_REGENERATING = "regenerating"


def footage_window(footage: tuple[int, int] | None, frame: tuple[int, int], fit: str) -> tuple[float, float, float, float]:
    """``(x0, y0, x1, y1)``: the part of a scene's footage the frame shows, 0-1 across and down the footage.

    ``cover`` (a clip's default, a statement's backdrop) cuts what overflows
    the frame's shape; ``contain`` shows the whole picture.
    """
    if footage is None or fit != "cover" or not footage[0] or not footage[1]:
        return 0.0, 0.0, 1.0, 1.0
    picture, screen = footage[0] / footage[1], frame[0] / frame[1]
    if picture > screen:
        shown = screen / picture
        return 0.5 - shown / 2, 0.0, 0.5 + shown / 2, 1.0
    shown = picture / screen
    return 0.0, 0.5 - shown / 2, 1.0, 0.5 + shown / 2


def quad_visible_share(quad: Sequence[Sequence[float]], window: tuple[float, float, float, float]) -> float:
    """The share of *quad* (0-1 corners) inside *window* (``x0, y0, x1, y1``): its box clipped to the window, by area.

    A box stands for the quad: close enough to say whether a surface is partly out of frame or mostly so.
    """
    xs, ys = [float(p[0]) for p in quad], [float(p[1]) for p in quad]
    width, height = max(xs) - min(xs), max(ys) - min(ys)
    if width <= 0 or height <= 0:
        return 0.0
    x0, y0, x1, y1 = window
    shown_w = max(0.0, min(max(xs), x1) - max(min(xs), x0))
    shown_h = max(0.0, min(max(ys), y1) - max(min(ys), y0))
    return min(1.0, (shown_w * shown_h) / (width * height))


def held_seconds(planned: dict) -> float:
    """How long a planned scene holds its footage's last frame (the flat end of its ``time_map``), 0 when never."""
    points = planned.get("time_map") or []
    if len(points) < 2:
        return 0.0
    end = points[-1][1]
    i = len(points) - 1
    while i > 0 and points[i - 1][1] == end:
        i -= 1
    return float(points[-1][0]) - float(points[i][0])


def frozen_hold_seconds(document: dict, index: int) -> float:
    """How long the planned scene at *index* shows its footage's last frame frozen: ``extend_mode: hold`` past
    the footage's end, less the still beat its author asked for (``hold_end``), within the span the scene is on
    screen alone (``timeline.owned_span``: a frame of its transition out is a blend, never frozen). 0 otherwise."""
    from app.services.video_studio.timeline import owned_span

    planned = document["scenes"][index]
    if (planned.get("extend") or {}).get("mode") != "hold" or len(planned.get("time_map") or []) < 2:
        return 0.0
    start, length = float(planned.get("start") or 0.0), float(planned["time_map"][-1][0])
    frozen_from = start + length - held_seconds(planned)
    frozen_to = start + length - float(planned.get("hold_end") or 0.0)
    return max(0.0, min(frozen_to, owned_span(document, index)[1]) - frozen_from)


def _speed_fix(scene, planned: dict, facts: ContentFacts) -> str | None:
    """``speed 0.66 (it then plays 9.0 s)``: the speed at which *scene*'s footage lasts as long as its scene needs, as
    the planner plays it (``timeline.speed_to_fill``); ``None`` when none does or its length is not known."""
    from app.services.video_studio.timeline import EXTEND_MIN, filled_length, recording_seconds, speed_to_fill

    points = planned.get("time_map") or []
    if len(points) < 2:
        return None
    need = float(points[-1][0]) - float(getattr(scene, "hold_end", 0.0) or 0.0) - EXTEND_MIN
    zoom = getattr(scene, "extend_mode", None) == "zoom"
    if scene.type == "recording":
        take = facts.takes.get(scene.take_id)
        if take is None:
            return None

        def played_at(speed: float) -> float:
            return recording_seconds(take, scene, speed=speed, fill_to=need if zoom else 0.0)
    elif scene.type == "clip":
        trimmed = float(points[1][1]) - float(points[0][1])
        # The plan slowed it to fill the scene only where its own sound is not heard (``timeline.filled_length``).
        stretch = zoom and float(points[1][0]) > trimmed / max(scene.speed, 1e-6) + 1e-3

        def played_at(speed: float) -> float:
            return filled_length(trimmed, speed, need, stretch=stretch)
    else:
        return None
    speed = speed_to_fill(played_at, need, current=float(scene.speed))
    if speed is None or speed >= float(scene.speed):
        return None
    return f"speed {speed:g} (it then plays {played_at(speed):.1f} s)"


def _frozen_hold(scene, planned: dict, facts: ContentFacts, frozen: float, voice_speed: float) -> str:
    """The ``frozen_hold`` words for *scene*: its footage's length, its narration's, and what fills: the modes
    and the speed that does."""
    from app.services.video_studio.frame_freeze import FROZEN_SECONDS
    from app.services.video_studio.timeline import estimate_narration_seconds

    points = planned["time_map"]
    played = float(points[-1][0]) - held_seconds(planned)
    footage = {"clip": "its clip", "recording": "its recording"}.get(scene.type, "its footage")
    trimmed = f"trimmed {float(scene.trim_start or 0):g}-{'end' if scene.trim_end is None else f'{float(scene.trim_end):g}'} s"
    if scene.id in facts.spoken:
        narration = f"its narration, {facts.spoken[scene.id]:.1f} s as spoken"
    else:
        narration = f"its narration, about {estimate_narration_seconds(scene.narration, voice_speed):.1f} s at the reading pace (not spoken yet)"
    modes = "extend_mode zoom (it drifts in on what still holds)" + (
        " or loop (its last second, crossfaded)" if scene.type != "recording" else ""
    )
    slower = _speed_fix(scene, planned, facts)
    return (
        f"its last frame freezes for {frozen:.1f} s: {footage} plays {played:.1f} s ({trimmed}, at speed {scene.speed:g}) "
        f"under {narration}, and extend_mode hold holds that frame - past the {FROZEN_SECONDS:g} s the render's "
        f"frozen-picture check reports. Give it {modes}{f', or {slower}' if slower else ''}, a longer "
        f"{footage.removeprefix('its ')}, or a shorter narration"
    )


def _footage_known(scene, facts: ContentFacts) -> bool:
    """Whether the plan has *scene*'s footage at its real length: a ready recording, or an AI shot that plays (an
    upload is probed at render, a shot being made is not there yet)."""
    if scene.type == "recording":
        return scene.take_id in facts.ready_takes
    shot = facts.shots.get(scene.clip_id or "") if scene.type == "clip" else None
    return shot is not None and shot.status not in _SHOT_GONE and shot.status not in _SHOT_MAKING


def _held_frame(scene, planned: dict, facts: ContentFacts, held: float) -> str | None:
    """The ``held_frame`` words for *scene*, or ``None`` while its footage's length is only estimated."""
    slower = _speed_fix(scene, planned, facts)
    if scene.type == "recording":
        if scene.take_id not in facts.ready_takes:
            return None
        if scene.fit == "auto":
            # The save slowed it as far as a fit plays it: what is left is the footage's or the narration's.
            from app.services.video_studio.recording_fit import SLOWEST_FIT_SPEED

            if scene.speed > SLOWEST_FIT_SPEED:
                return (
                    f"its recording ends about {held:.1f} s before its narration does (its narration is longer, as "
                    "spoken, than when the video was saved): the video's next save slows it to fit (fit: auto), or "
                    "record more of the flow, or shorten the narration"
                )
            return (
                f"its recording ends about {held:.1f} s before its narration does, even with its pauses played slower "
                f"and at speed {scene.speed:g} (fit: auto slows it to {SLOWEST_FIT_SPEED:g} at most): its last frame "
                "holds, drifting in - record more of the flow, or shorten the narration"
            )
        return (
            f"its recording ends about {held:.1f} s before its narration does, even with its pauses played slower: "
            "its last frame holds, drifting in - record more of the flow, slow it "
            f"({slower or 'speed'}), or shorten the narration"
        )
    # An upload is probed at render: its length is not known here.
    shot = facts.shots.get(scene.clip_id or "") if scene.type == "clip" else None
    if shot is None or shot.status in _SHOT_GONE or shot.status in _SHOT_MAKING:
        return None
    points = planned["time_map"]
    rate = (points[1][1] - points[0][1]) / max(points[1][0] - points[0][0], 1e-6) / scene.speed
    how = f"slowed to {round(rate * 100)}% of its speed" if rate < 0.995 else "at its speed (its own sound is heard)"
    name = f' "{shot.name}"' if shot.name else ""
    return (
        f"its AI shot{name} is shorter than its narration: played {how}, its last frame still holds about "
        f"{held:.1f} s, drifting in - generate a longer one (duration_seconds), continue it with a shot made from "
        f"its last frame (reference_clip_id `{scene.clip_id}`), {f'slow it ({slower}), ' if slower else ''}or "
        "shorten the narration"
    )


def _loop_seam(scene, planned: dict) -> list[str]:
    """The ``insert_loop_seam`` words of each screen insert of a looping clip *scene* that runs past its seam:
    where the clip ends and its last second starts playing again under the insert, and what keeps it clear."""
    from app.services.video_studio.timeline import EXTEND_MIN

    extend = planned.get("extend") or {}
    points = planned.get("time_map") or []
    if scene.type != "clip" or extend.get("mode") != "loop" or not points:
        return []
    seam, period, length = float(extend["from"]), float(extend.get("period") or 1.0), float(points[-1][0])
    found = []
    for index, overlay in enumerate(scene.overlays):
        if overlay.type != "screen_insert":
            continue
        # Placed as the planner places it (``timeline.plan``): from its start, for its duration, within the scene.
        start = min(float(overlay.start), max(length - 0.3, 0.0))
        end = min(start + float(overlay.duration), length)
        if end <= seam + EXTEND_MIN:
            continue
        fix = (
            f"give it duration {math.floor((seam - start) * 10) / 10:g} (it ends at the seam)"
            if start < seam - 0.3
            else f"start it before {seam:.1f} s"
        )
        found.append(
            f"its screen insert {index} runs to {end:.1f} s of the scene, past its clip's loop seam at {seam:.1f} s: "
            f"the clip ends there and its last {period:g} s plays again and again, crossfaded, under the insert - "
            f"{fix}, or set the scene's extend_mode zoom (the clip plays slower to the scene's end, with no seam)"
        )
    return found


def _shown_takes(spec) -> list[tuple[str, str, str, str | None]]:
    """``(scene id, take id, where it plays, insert identity)`` of each recording a recording scene or a screen insert
    of *spec* plays, in the spec's order; a wall's panes each."""
    from app.services.video_studio.timeline import insert_key

    found: list[tuple[str, str, str, str | None]] = []
    for scene in spec.scenes:
        if scene.type == "recording" and scene.take_id:
            found.append((scene.id, scene.take_id, f"scene {scene.id}", None))
        for index, overlay in enumerate(scene.overlays):
            if overlay.type != "screen_insert":
                continue
            for pane, source in enumerate(overlay.shown_sources):
                take_id = getattr(source, "take_id", None)
                if take_id:
                    named = f" (pane {pane + 1})" if overlay.sources else ""
                    identity = f"{insert_key(scene.id, index)}|take:{take_id}"
                    found.append((scene.id, take_id, f"scene {scene.id}'s screen insert {index}{named}", identity))
    return found


def _insert_takes(spec) -> list[str]:
    """The recordings *spec*'s screen inserts play (:func:`_shown_takes`)."""
    return [take_id for _scene, take_id, _where, identity in _shown_takes(spec) if identity is not None]


def _insert_window(spec, insert: str) -> tuple[float, float] | None:
    """The seconds of its take a screen insert plays, ``(trim_start, trim_start + duration)``: the insert of identity
    *insert* (``insert/<scene>.<index>|take:<id>``, :func:`_shown_takes`) comes in on its source's ``trim_start``,
    plays it at its own rate for its ``duration`` and holds its last frame (``timeline._insert_payload``) - it never
    loops. ``None`` when the identity names no screen insert of *spec*."""
    key = insert.split("|", 1)[0].removeprefix("insert/")
    scene_id, _dot, rest = key.partition(".")
    try:
        index = int(rest.split(".", 1)[0])
    except ValueError:
        return None
    scene = next((s for s in spec.scenes if s.id == scene_id), None)
    overlays = list(getattr(scene, "overlays", None) or [])
    if not 0 <= index < len(overlays) or overlays[index].type != "screen_insert":
        return None
    overlay = overlays[index]
    start = float(overlay.trim_start or 0.0)
    return start, start + float(overlay.duration)


def _played(stretch: dict, start: float, end: float | None) -> bool:
    """Whether a stretch of a take (``{t: [start, end], ...}``) meets the window ``[start, end)`` a scene plays."""
    return float(stretch["t"][1]) > start and (end is None or float(stretch["t"][0]) < end)


def _shown_stretches(spec, scene_id: str, take, insert: str | None, stretches: list) -> list:
    """The stretches of *take* (``{t: [start, end], ...}``) that scene *scene_id* shows: a recording scene within what
    it keeps of the take, the screen insert of identity *insert* within the seconds of the take it plays
    (:func:`_insert_window`) - a stretch its ``trim_start`` skips or past its duration is never shown."""
    from app.services.video_studio.timeline import kept_end

    shown = list(stretches or [])
    if insert:
        window = _insert_window(spec, insert)
        if window is not None:
            shown = [s for s in shown if _played(s, *window)]
        return shown
    scene = next((s for s in spec.scenes if s.id == scene_id), None)
    if scene is not None and scene.type == "recording":
        start, end = float(scene.trim_start or 0.0), kept_end(take, scene)
        shown = [s for s in shown if _played(s, start, end)]
    return shown


def _shown_lows(spec, scene_id: str, take, insert: str | None) -> list[str]:
    """The words of each stretch *take* animated under 20 fps (``capture_rate.low_spans``) that scene
    *scene_id* shows (:func:`_shown_stretches`), but those it shows stepping (:func:`_shown_stepping`)."""
    from app.services.video_studio.capture_rate import besides, low_words

    lows = _shown_stretches(spec, scene_id, take, insert, getattr(take, "low", None) or [])
    stepping = _shown_stretches(spec, scene_id, take, insert, getattr(take, "stepping", None) or [])
    return low_words({"low": besides(lows, stepping)})


def _shown_stepping(spec, scene_id: str, take, insert: str | None, planned: dict | None = None) -> list[tuple]:
    """Each stretch of *take*'s motion mostly synthesised from keyframes (``capture_rate.stepping``) that scene
    *scene_id* shows (:func:`_shown_stretches`), graded as the scene draws it (``capture_rate.stepping_grade``):
    ``(its grade, its words)``. A recording scene plays it at the rate of its *planned* ``time_map`` there - slowed,
    it still steps: the compositor shows one take frame per output frame, never two blended. A screen insert
    plays it at its own rate."""
    from app.services.video_studio.capture_rate import stepping_grade, stepping_words
    from app.services.video_studio.timeline import map_inverse

    points: list[tuple[float, float]] = []
    if not insert:
        try:
            points = [(float(p[0]), float(p[1])) for p in (planned or {}).get("time_map") or []]
        except (TypeError, ValueError, IndexError):
            points = []
    found = []
    for stretch in _shown_stretches(spec, scene_id, take, insert, getattr(take, "stepping", None) or []):
        said = stepping_words({"stepping": [stretch]})
        try:
            start, end = (float(v) for v in stretch["t"])
        except (KeyError, TypeError, ValueError):
            continue
        rate = 1.0
        if len(points) >= 2:
            lo, hi = max(start, points[0][1]), min(end, points[-1][1])
            played = map_inverse(points, hi) - map_inverse(points, lo)
            if hi > lo and played > 1e-3:
                rate = (hi - lo) / played
        grade = stepping_grade(stretch, rate)
        if grade is not None and said:
            found.append((grade, said[0]))
    return found


def _shown_faults(spec, scene_id: str, take, insert: str | None) -> list[str]:
    """The words of each capture fault of *take* (``capture_rate.capture_fault``: frames drawn at another scale
    on a step holding the page) that scene *scene_id* shows (:func:`_shown_stretches`), with the frames it names."""
    from app.services.video_studio.capture_rate import CaptureFault

    said = []
    for raw in _shown_stretches(spec, scene_id, take, insert, getattr(take, "off_scale", None) or []):
        fault = CaptureFault.from_dict(raw)
        if fault is not None:
            frames = f": frames {', '.join(fault.frames)}" if fault.frames else ""
            said.append(f"{fault.words()[:-1]}{frames})")
    return said


def _shown_holds(spec, scene_id: str, take, insert: str | None) -> list:
    """The holds of *take* on a page still loading (``page_settle.loading_holds``) that scene *scene_id* shows:
    a recording scene within what it keeps of the take, a screen insert within the seconds it plays
    (:func:`_insert_window`)."""
    from app.services.video_studio.page_settle import loading_holds
    from app.services.video_studio.timeline import kept_end

    holds = loading_holds(take.events)
    if insert:
        window = _insert_window(spec, insert)
        if window is None:
            return holds
        start, end = window
    else:
        scene = next((s for s in spec.scenes if s.id == scene_id), None)
        if scene is None or scene.type != "recording":
            return holds
        start, end = float(scene.trim_start or 0.0), kept_end(take, scene)
    return [hold for hold in holds if hold.span[1] > start and (end is None or hold.span[0] < end)]


#: Output seconds of a page still loading a recording scene may play before the save says so: a click's own
#: response, never seen as loading.
LOADING_SPAN_TOLERANCE_SECONDS = 0.3


def _loading_overlap(scene, take) -> tuple[float, list, float] | None:
    """What recording *scene* plays of *take*'s page still loading (``page_settle.loading_spans``), read on the
    scene's own time map (its trim, its speed, its idle handling): ``(output seconds, the spans it meets, the last
    second of the take they reach)``; ``None`` under :data:`LOADING_SPAN_TOLERANCE_SECONDS`."""
    from app.services.video_studio.page_settle import loading_spans
    from app.services.video_studio.spec import IdleCompression
    from app.services.video_studio.timeline import build_time_map, kept_end, map_inverse

    spans = loading_spans(take.events or [])
    if not spans:
        return None
    idle = getattr(scene, "idle", None) or IdleCompression()
    start = float(getattr(scene, "trim_start", 0.0) or 0.0)
    end = kept_end(take, scene)
    end = float(take.duration) if end is None else min(float(end), float(take.duration))
    points = build_time_map(
        duration=float(take.duration),
        trim_start=start,
        trim_end=end,
        speed=float(getattr(scene, "speed", 1.0) or 1.0),
        idle_mode=idle.mode,
        idle_threshold=idle.threshold,
        idle_speed=idle.speed,
        events=take.events,
    )
    # Spans merged first, so two logs of one stretch (the click to its settle, the wait's own) count once.
    merged: list[list[float]] = []
    for span in spans:
        if merged and span.start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], span.end)
        else:
            merged.append([span.start, span.end])
    seconds = 0.0
    for a, b in merged:
        lo, hi = max(a, start), min(b, end)
        if hi > lo:
            seconds += map_inverse(points, hi) - map_inverse(points, lo)
    if seconds < LOADING_SPAN_TOLERANCE_SECONDS:
        return None
    met = [span for span in spans if span.end > start and span.start < end]
    return seconds, met, max(span.end for span in met)


def _occluded_words(measured: dict | None, trim_end: float | None) -> tuple[str, dict] | None:
    """What a save says of a shot's screen occluded on its take (``insert_keep_out.Overlap``), and the finding's data;
    ``None`` when nothing stands in front of it, or when the scene's *trim_end* already ends it before the first
    occluded frame."""
    from app.services.video_studio.insert_keep_out import Overlap

    overlap = Overlap.from_dict(measured)
    if overlap is None or not overlap.matted or overlap.first_frame is None or overlap.first_at is None:
        return None
    if trim_end is not None and trim_end <= overlap.first_at:
        return None
    box = overlap.first_box
    where = f", over x {box[0]:.2f}-{box[2]:.2f}, y {box[1]:.2f}-{box[3]:.2f} of the frame" if box else ""
    trim = overlap.trim_end()
    fix = (
        f"set trim_end: {trim:.2f} on the scene to end it one frame before, or generate the shot again in place"
        if trim is not None
        else "it stands there from the first frame, so no trim keeps the screen clear: generate the shot again in place"
    )
    message = (
        f"something stands in front of the screen its screen insert is drawn on, on {overlap.occluded_share:.0%} of the "
        f"take's frames, first at {overlap.first_at:.2f} s (frame {overlap.first_frame}){where}, at most "
        f"{overlap.max_share:.0%} of the screen - the insert is drawn under it; {fix}"
    )
    data = {
        "first_frame": overlap.first_frame,
        "first_at": overlap.first_at,
        "first_box": list(box) if box else None,
        "occluded_share": overlap.occluded_share,
        "max_share": overlap.max_share,
        "trim_end": trim,
    }
    return message, data


def content_findings(spec, document: dict, facts: ContentFacts, authored: dict | None) -> list[QualityFinding]:
    """Findings about the pictures and the sound of the effective *spec*, planned as *document* (no browser).

    Pure: what the database said is in *facts* (:func:`content_facts`).
    Each finding names the scene and the change that fixes it.
    """
    from app.services.video_studio.frame_freeze import FROZEN_SECONDS
    from app.services.video_studio.spec import design_size
    from app.services.video_studio.timeline import narration_speed

    scenes = _Scenes(document, authored)
    planned_at = {scene.get("id"): index for index, scene in enumerate(scenes.planned)}
    frame = design_size(spec.format.aspect_ratio)
    findings: list[QualityFinding] = []

    def add(
        scene_id: str | None,
        kind: FindingKind,
        severity: Severity,
        message: str,
        *,
        clip_id: str | None = None,
        insert: str | None = None,
        data: dict | None = None,
    ) -> None:
        if scene_id in planned_at:
            findings.append(
                scenes.finding(
                    planned_at[scene_id], kind, severity, message, content=True, clip_id=clip_id, insert=insert, data=data
                )
            )

    from app.services.video_studio.scroll_pacing import blended_zooms, misheld_zooms, take_rate

    # A shot's failed regeneration is said once, on the first scene showing it: a fact of the shot, never one
    # finding per scene or per revision.
    regeneration_said: set[str] = set()
    rate_said: set[str] = set()
    for scene in spec.scenes:
        sid = scene.id
        # The AI shots it plays: its picture, the shot behind a statement, a screen insert's source.
        shown: list[tuple[str, str, bool]] = []
        if scene.type == "clip" and scene.clip_id:
            shown.append(("its AI shot", scene.clip_id, True))
        if scene.type == "statement" and scene.backdrop is not None and scene.backdrop.clip_id:
            shown.append(("the AI shot behind it", scene.backdrop.clip_id, True))
        for index, overlay in enumerate(scene.overlays):
            for pane, source, _quad in overlay.insert_panes():
                if source.clip_id:
                    where = f"its screen insert {index}" + (f" (pane {pane + 1})" if pane is not None else "")
                    shown.append((f"the AI shot of {where}", source.clip_id, False))
        for what, clip_id, plays in shown:
            shot = facts.shots.get(clip_id)
            if shot is None:
                continue
            name = f' "{shot.name}"' if shot.name else ""
            if shot.status in _SHOT_GONE:
                add(
                    sid,
                    "shot_state",
                    "overloaded" if plays else "dense",
                    f"{what}{name} {shot.status} ({(shot.error or 'no reason given')[:160]}) - generate it again in "
                    f"place (generate_video_clip with clip_id `{clip_id}`) or replace it; a render refuses it",
                    clip_id=clip_id,
                )
                continue
            if shot.status in _SHOT_MAKING:
                add(
                    sid,
                    "shot_state",
                    "advisory",
                    f"{what}{name} is still being generated - render once it is ready "
                    f"(get_video_jobs(clip_ids=['{clip_id}']))",
                    clip_id=clip_id,
                )
                continue
            # The take that plays is measured below, also while (or after) it is generated again.
            if shot.status == _SHOT_REGENERATING:
                add(
                    sid,
                    "shot_state",
                    "advisory",
                    f"{what}{name} is being generated again - its previous take plays until the new one is ready "
                    f"and passes its checks, in a render too (get_video_jobs(clip_ids=['{clip_id}']))",
                    clip_id=clip_id,
                )
            elif shot.regeneration_failed and clip_id not in regeneration_said:
                regeneration_said.add(clip_id)
                take = f" (file `{shot.playing}`)" if shot.playing else ""
                add(
                    sid,
                    "shot_state",
                    "advisory",
                    f"{what}{name}: last regeneration failed: {shot.regeneration_failed[:160].rstrip('. ')}, the "
                    f"previous take{take} plays - nothing to fix for the render; generate it again in place "
                    f"(generate_video_clip with clip_id `{clip_id}`) only to replace that take",
                    clip_id=clip_id,
                )
            # Lettering the take invented (dev:invented-text-check): gibberish where the start picture had no word. A
            # finding of the shot, on every scene showing it, without its clip_id: a preview that passed the camera
            # check never clears it (``render_checks._quality_reasons``), the lettering does not change.
            if shot.invented:
                named = ", ".join(
                    f'"{item.word}" ({item.frame.replace("_", " ")}{", " + item.where if item.box else ""}'
                    f"{', crop `' + item.crop_file_id + '`' if item.crop_file_id else ''})"
                    for item in shot.invented[:4]
                )
                more = f" and {len(shot.invented) - 4} more" if len(shot.invented) > 4 else ""
                if shot.text_check:
                    add(
                        sid,
                        "invented_text",
                        "dense",
                        f"{what}{name} shows invented lettering: {named}{more} - letters that make no word, no brand "
                        f"term and no word of its start picture; a render refuses it. Generate it again in place "
                        f"(clip_id `{clip_id}`), keep that region from the start picture (keep_regions), or set "
                        "text_check: false on the shot to accept it",
                    )
                else:
                    add(
                        sid,
                        "invented_text",
                        "advisory",
                        f"{what}{name} shows invented lettering: {named}{more} - accepted, the shot was asked with "
                        "text_check: false",
                        clip_id=clip_id,
                    )
            # A logo drawn onto the take that reads as a flat sticker: lit flat on a shaded
            # surface, moving rigidly or keeping its size while the surface bends. A finding of the shot, on every scene
            # showing it, without its clip_id: a preview never clears it, the take does not change. Accepted flat, said.
            if plays and (flat := _logo_integration_words(shot)) is not None:
                level, words = flat
                add(
                    sid,
                    "logo_not_integrated",
                    "advisory" if level == "advisory" else "dense",
                    f"{what}{name} {words}",
                    clip_id=clip_id if level == "advisory" else None,
                )
            # The screen a screen insert is drawn on, occluded on the take (dev:screen-occlusion-check): said at save on
            # each clip scene carrying a screen insert over the shot, with the first occluded frame and the trim_end
            # that ends the scene before it - the render's insert_through_foreground gate would say it much later.
            if (
                plays
                and scene.type == "clip"
                and any(o.type == "screen_insert" for o in scene.overlays)
                and (said := _occluded_words(shot.occlusion, getattr(scene, "trim_end", None)))
            ):
                message, data = said
                add(sid, "insert_occluded", "advisory", f"{what}{name}: {message}", clip_id=clip_id, data=data)
            # A move the author asked for is no fault (``motion: intended``); a passing preview of every scene
            # showing the shot, as they are now, clears it - said with the render and the revision that did.
            intended = getattr(scene, "motion", None) == "intended"
            if plays and not intended and camera_moved(shot):
                asked = (
                    f"its max_camera_motion {shot.max_camera_motion:.2f}"
                    if shot.max_camera_motion is not None
                    else "its prompt asks for a still camera"
                )
                if shot.camera_motion is not None:
                    moved = (
                        f"camera moved on a locked-off shot: {what}{name}'s camera moved {shot.camera_motion:.2f} of the "
                        f"frame from its first frame to its last (measured on the background) though {asked}"
                    )
                else:
                    moved = (
                        f"camera moved on a locked-off shot: {what}{name} ends on a frame that keeps only "
                        f"{shot.motion_ssim:.2f} of its first (SSIM) though {asked}"
                    )
                if (cleared := facts.cleared.get(clip_id)) is not None:
                    revision = f" of revision {cleared['revision']}" if cleared.get("revision") is not None else ""
                    add(
                        sid,
                        "camera_moved",
                        "advisory",
                        f"{moved} - cleared: the preview `{cleared.get('render_id')}`{revision} of every scene showing it "
                        "passed, nothing to fix",
                        clip_id=clip_id,
                    )
                else:
                    add(
                        sid,
                        "camera_moved",
                        "dense",
                        f"{moved} - generate it again in place (clip_id `{clip_id}`) with the camera locked off and less "
                        "motion, cut it short (trim_end), set motion: intended on the scene if the move is wanted, or "
                        "preview the scenes showing it (render_video_scene): a preview that passes clears this",
                        clip_id=clip_id,
                    )
            # The shot's frames against the video's references its scene names: one finding per reference
            # drifted from (dense), the crop it was read on attached (``data``); a reference seen in none of the
            # frames as it shows it is said (advisory), never a drift: the shot may frame the prop from another angle.
            if plays and scene.type == "clip" and scene.consistency and clip_id in facts.reference_drifts:
                for drift in facts.reference_drifts[clip_id]:
                    if drift.reference.get("id") not in scene.consistency:
                        continue
                    if not drift.found:
                        add(
                            sid,
                            "reference_drift",
                            "advisory",
                            f"{drift.words()} - {drift.crop_words()} is the nearest; check the shot by eye, crop the "
                            f"reference on the prop as this shot frames it, or drop `{drift.reference.get('id')}` from "
                            "the scene's consistency",
                            clip_id=clip_id,
                            data=drift.as_dict(),
                        )
                        continue
                    add(
                        sid,
                        "reference_drift",
                        "dense",
                        f"{drift.words()} - {drift.crop_words()}; generate it again in place (clip_id `{clip_id}`) with the "
                        f"scene's consistency (the reference is sent with the shot, or named in its prompt), crop the "
                        f"reference tighter on what must stay the same, or drop `{drift.reference.get('id')}` from the "
                        "scene's consistency",
                        clip_id=clip_id,
                        data=drift.as_dict(),
                    )
        animation = scene.animate.clip_id if scene.type == "image" and scene.animate is not None else None
        if animation and (shot := facts.shots.get(animation)) is not None:
            # An animation that did not come out never stops the video: the scene plays its still.
            name = f' "{shot.name}"' if shot.name else ""
            if shot.status in _SHOT_GONE:
                add(
                    sid,
                    "shot_state",
                    "advisory",
                    f"plays as its still image: its animation{name} {shot.status} "
                    f"({(shot.error or 'no reason given')[:160].rstrip('. ')}) - nothing to fix for the render; "
                    f"generate it again in place (generate_video_clip with clip_id `{animation}`) with a calmer "
                    "camera, or drop animate",
                )
            elif shot.status in _SHOT_MAKING:
                add(
                    sid,
                    "shot_state",
                    "advisory",
                    f"plays its still image until its animation{name} is made "
                    f"(get_video_jobs(clip_ids=['{animation}']))",
                    clip_id=animation,
                )
        planned = scenes.planned[planned_at[sid]] if sid in planned_at else {}
        if scene.type == "image" and planned.get("type") == "image" and (size := facts.images.get(scene.file_id)):
            width, height = size
            out_w, out_h = facts.output
            ratios = (out_w / width, out_h / height)
            scale = max(ratios) if scene.fit == "cover" else min(ratios)
            if scale > UPSCALE_SOFT:
                add(
                    sid,
                    "image_resolution",
                    "dense" if scale >= UPSCALE_BLURRED else "advisory",
                    f"its picture is {width}x{height}, drawn {scale:.1f}x its size on the {out_w}x{out_h} frame of a "
                    f"{facts.quality} render: it will look soft - use a picture of at least "
                    f"{round(width * scale)}x{round(height * scale)} (or an SVG)",
                )
        held = held_seconds(planned) - float(getattr(scene, "hold_end", 0.0) or 0.0)
        if getattr(scene, "extend_mode", None) == "hold" and sid in planned_at:
            # A held end does not drift: past the render's frozen-picture threshold it is a frozen picture,
            # said before any render is paid for - its footage's length known (a ready shot or recording).
            frozen = frozen_hold_seconds({**document, "scenes": scenes.planned}, planned_at[sid])
            if frozen > FROZEN_SECONDS and _footage_known(scene, facts):
                add(sid, "frozen_hold", "dense", _frozen_hold(scene, planned, facts, frozen, narration_speed(scene, spec)))
        elif held >= HELD_FRAME_SECONDS and (words := _held_frame(scene, planned, facts, held)):
            add(sid, "held_frame", "advisory", words)
        for index, overlay in enumerate(scene.overlays):
            # A quad left to the render is the bezel its footage shows: inside the frame by construction.
            if overlay.type != "screen_insert" or overlay.quad is None or overlay.auto_quad:
                continue
            footage, fit = None, "contain"
            if scene.type == "clip":
                shot = facts.shots.get(scene.clip_id or "")
                footage, fit = ((shot.width, shot.height) if shot and shot.width else None), scene.fit
            elif scene.type == "statement" and scene.backdrop is not None:
                shot = facts.shots.get(scene.backdrop.clip_id or "")
                footage, fit = ((shot.width, shot.height) if shot and shot.width else None), "cover"
            x0, y0, x1, y1 = footage_window(footage, frame, fit)
            tol = INSERT_EDGE_TOLERANCE
            off = [
                f"{_CORNERS[n]} ({x:.2f}, {y:.2f})"
                for n, (x, y) in enumerate(overlay.quad)
                if not (x0 - tol <= x <= x1 + tol and y0 - tol <= y <= y1 + tol)
            ]
            if off:
                # A surface partly out of frame keeps its true corners outside it: the insert is drawn
                # through the visible part, never squeezed to the frame's edge. Said as advice; a surface mostly
                # outside the frame is a quad on something the frame does not show.
                shown = quad_visible_share(overlay.quad, (x0, y0, x1, y1))
                mostly_out = shown < INSERT_MIN_VISIBLE_SHARE
                add(
                    sid,
                    "insert_outside",
                    "dense" if mostly_out else "advisory",
                    f"its screen insert {index} {'is mostly out of' if mostly_out else 'runs past'} the frame: its "
                    f"{', '.join(off)} {'is' if len(off) == 1 else 'are'} outside the footage the frame shows "
                    f"(x {x0:.2f}-{x1:.2f}, y {y0:.2f}-{y1:.2f}), {shown:.0%} of its quad in it - "
                    + (
                        "give the quad on a surface the frame shows"
                        if mostly_out
                        else "keep the quad on the surface's true corners, outside the frame where the surface is: "
                        "the insert shows through the visible part; never squeeze it to the frame's edge, which "
                        "keystones it"
                    ),
                )
        for message in _loop_seam(scene, planned):
            add(sid, "insert_loop_seam", "dense", message)
    # What the footage under each static screen insert shows, measured on the frame it comes in on.
    from app.services.video_studio import insert_bezels

    for scene_id, kind, message, _shot in insert_bezels.findings(facts.bezels):
        add(scene_id, kind, "dense", message)
    # Whether each screen insert's text is drawn large enough to read: advice under 10 px of the 1080p
    # frame, a finding the render waits for under 6 px - both offering what brings it to 12 px and keeps the layout.
    from app.services.video_studio import insert_legibility

    for scene_id, message, severity, surface in insert_legibility.findings(facts.legibility):
        add(scene_id, "insert_legibility", severity, message, insert=surface)
    # Perceptual gates (issue #10036): blocking on a brand film or a product demo, advice elsewhere.
    from app.services.video_studio import perceptual, timeline

    perceptual_level: Severity = "dense" if perceptual.blocks(perceptual.purposes_of(facts.template)) else "advisory"
    # Declared scenery is honoured: only an ambient recording that cannot be scenery - drawn at 0.3x or more, alone on
    # its frame, not under a statement's text - is said as advice, never counted by the render gate (issue #10093).
    statements = {scene.id for scene in spec.scenes if scene.type == "statement"}
    for scene_id, message, surface in insert_legibility.ambient_overrides(facts.legibility, statements):
        add(scene_id, "insert_legibility", "advisory", message, insert=surface)
    # A narration that sounds synthetic: flat, buzzy or metallic against the same voice at speed 1 (or fixed
    # ceilings), and a narration slowed on a model that time-stretches.
    for scene in spec.scenes:
        if not scene.narration or not spec.voiceover.enabled:
            continue
        kept = facts.timbres.get(scene.id)
        if kept is not None and (measured := perceptual.Timbre.from_dict(kept[0])) is not None:
            why = perceptual.synthetic_voice(measured, perceptual.Timbre.from_dict(kept[1]))
            if why is not None:
                add(
                    scene.id,
                    "synthetic_voice",
                    perceptual_level,
                    f"its narration sounds synthetic: {why} - say it again at speed 1, with another voice of the "
                    "model, or with a speech model that speaks it natively",
                )
        stretched = perceptual.stretched_speed(spec.voiceover.model, timeline.narration_speed(scene, spec))
        if stretched is not None:
            add(scene.id, "synthetic_voice", "advisory", stretched)
    # An AI shot whose flat areas carry texture noise or over-sharpening: named with its region and its frame.
    textured: set[str] = set()
    for scene in spec.scenes:
        clip_id = getattr(scene, "clip_id", None)
        if not clip_id or clip_id in textured or clip_id not in facts.textures:
            continue
        textured.add(clip_id)
        frame_id, noise = facts.textures[clip_id]
        add(
            scene.id,
            "texture_noise",
            perceptual_level,
            f"AI shot `{clip_id[:8]}` carries texture noise over {noise.region()}: {noise.measure()} on "
            f"{noise.share:.0%} of it (frame `{frame_id}`) - generate it again asking for "
            "clean, soft surfaces, or with another video model",
        )
    # A source a whole render would refuse as soft at one of the qualities a video may be rendered at: said
    # before any preview, which draws at 1080p - advice, the render gate refusing at the quality asked.
    for soft in facts.soft:
        add(soft.scene_id, "insert_resolution", "advisory", soft.message(), insert=soft.identity)
    # A figure judged as it is drawn: drifted - the drift capped to the frame - and stood in space. One whose box
    # still crosses the frame's edge is cut on the frames that show it.
    for scene_id, fit in facts.figures.items():
        if fit.leaves:
            add(scene_id, "outside", "overloaded", fit.finding())
    # A recording whose scrolls or transitions play under 24 fps on a recording scene or a screen insert steps there:
    # said once, on the first scene showing it, naming the step, before any render is paid for.
    capture_said: set[str] = set()
    floor_said: set[str] = set()
    loading_said: set[str] = set()
    blend_said: set[str] = set()
    for scene_id, take_id, where, insert in _shown_takes(spec):
        take = facts.takes.get(take_id)
        if take is None:
            continue
        rate = take_rate(take.events, captured_frames=take.captured_frames, duration=take.duration)
        # A screen insert's recording painted under the floor: refused as an insert source (issue #10036).
        if (
            insert is not None
            and take_id not in floor_said
            and (
                painted := perceptual.under_capture_floor(
                    take.captured_frames, take.duration, getattr(take, "insert_rate", None)
                )
            )
            is not None
        ):
            floor_said.add(take_id)
            over = " while its page changed" if getattr(take, "insert_rate", None) else ""
            add(
                scene_id,
                "insert_capture_floor",
                perceptual_level,
                f"take {take_id[:8]} painted {painted:.1f} frames a second{over}, under the "
                f"{perceptual.MIN_INSERT_PAINTED_FPS:g} "
                f"an insert source needs: {where} steps and blurs - record it again on a GPU worker or with a lighter "
                "page",
                insert=insert,
            )
        if rate.steps and take_id not in rate_said:
            rate_said.add(take_id)
            add(
                scene_id,
                "take_frame_rate",
                "dense",
                f"take {take_id[:8]} carries {rate.fps:.1f} fps on {rate.where()}: {rate.stepping()} will step on "
                f"{where}; {rate.remedy()}",
                insert=insert,
            )
        # A canvas zoom drawn as a cross-dissolve of two keyframes: every label and node twice on its in-between
        # frames. Said once per take, naming each step and span; a repair redraws it by moving one keyframe at a time.
        if take_id not in blend_said and (blends := blended_zooms(take.events)):
            blend_said.add(take_id)
            spans = "; ".join(f"step {step} at {start:.2f}-{end:.2f} s" for step, start, end in blends[:4])
            add(
                scene_id,
                "double_image",
                "dense",
                f"take {take_id[:8]} zooms a canvas with its in-between frames blended from two keyframes ({spans}): "
                f"every label and node is drawn twice there, the screen unsteady on {where}; repair it "
                f"(get_video_recording(recording_id='{take_id}', repair=true) draws each frame from the nearer keyframe, "
                "never two blended) or record it again",
                insert=insert,
            )
        # A warped canvas zoom that held the boxes over its canvas against the canvas under them: from
        # another keyframe, or a label the zoom carries held unwarped - split or offset, a flicker. Said once per take.
        elif take_id not in blend_said and (misheld := misheld_zooms(take.events)):
            blend_said.add(take_id)
            spans = "; ".join(f"step {step} at {start:.2f}-{end:.2f} s" for step, start, end in misheld[:4])
            add(
                scene_id,
                "double_image",
                "dense",
                f"take {take_id[:8]} zooms a canvas with the boxes over it held against the canvas under them "
                f"({spans}): labels read split or offset there, flickering on {where}; repair it "
                f"(get_video_recording(recording_id='{take_id}', repair=true) draws a box moving with the canvas with "
                "it and holds only those standing still) or record it again",
                insert=insert,
            )
        # Drawn on SwiftShader: the same worker records it no faster; a GPU worker does.
        drawn_on_cpu = any(
            stretch.get("gl") == "software"
            for stretch in [*(getattr(take, "low", None) or []), *(getattr(take, "stepping", None) or [])]
        )
        again = (
            "record it again on a worker whose Chromium draws on a GPU (the deployment's: VIDEO_STUDIO_RECORDING_GL, "
            "a recording lane on GPU workers)"
            if drawn_on_cpu
            else "record it again (record_browser_demo measures the worker's capture rate before step 1 and records "
            "at a pixel ratio it sustains)"
        )
        # Or repaired without recording again: its passes run again on the CPU into a new take, the residual said.
        repair = (
            f", or repair it without recording again (get_video_recording(recording_id='{take_id}', repair=true) "
            "makes a new take and names what it could not repair)"
        )
        again += repair
        # Frames drawn at another scale while its page held still: a capture fault on every scene showing it,
        # the take to record again - never stepping motion, never slowed or recorded at another rate.
        if faults := _shown_faults(spec, scene_id, take, insert):
            add(
                scene_id,
                "off_scale_frames",
                "dense",
                f"take {take_id[:8]} plays {'; '.join(faults)} on {where}: a capture fault, not a motion of the page - "
                "the frames about each run are alike and those in it draw the page at another scale; record the take "
                f"again (record_browser_demo drops the frames painted at another size or scale){repair}",
                insert=insert,
            )
        # Its motion mostly synthesised from keyframes: said on every scene showing it, naming each step, graded
        # as the scene draws it - the stretches it covers are never said low as well.
        drawn_as = scenes.planned[planned_at[scene_id]] if insert is None and scene_id in planned_at else None
        steps = _shown_stepping(spec, scene_id, take, insert, drawn_as)
        for level in ("dense", "advisory"):
            graded = [(grade, words) for grade, words in steps if grade.severity == level]
            if not graded:
                continue
            if level == "advisory":
                # The page settling on a step that holds it: its widgets still drawing, said apart.
                settling = [(grade, words) for grade, words in graded if grade.settling]
                brief = [(grade, words) for grade, words in graded if not grade.settling]
                if brief:
                    said = "; ".join(f"{words[:-1]}; {grade.words()})" for grade, words in brief)
                    add(
                        scene_id,
                        "stepping_motion",
                        "advisory",
                        f"take {take_id[:8]} plays {said} on {where}: a brief transition painted fast enough to read "
                        "as motion, its keyframes held under a quarter of a second",
                        insert=insert,
                    )
                if settling:
                    said = "; ".join(words for _grade, words in settling)
                    add(
                        scene_id,
                        "stepping_motion",
                        "advisory",
                        f"take {take_id[:8]} plays {said} on {where}: the page settling on a step that holds it "
                        "(widgets still drawing), no motion the audience follows; wait longer before it to show it "
                        "settled",
                        insert=insert,
                    )
                continue
            said = "; ".join(words for _grade, words in graded)
            fix = again
            if drawn_on_cpu and float(getattr(take, "fps", 0) or 0) >= 50:
                fix += ", or record it at fps 30 (fps_fallback: fps)"
            # Slowing the scene never smooths it: each output frame shows one take frame, never two blended.
            if insert is None:
                fix += "; slowing the scene would not smooth it: a slowed scene shows one painted frame at a time"
            add(
                scene_id,
                "stepping_motion",
                "dense",
                f"take {take_id[:8]} plays {said} on {where}: more than half of those frames hold the keyframe "
                f"before, so its motion steps; {fix}",
                insert=insert,
            )
        # A transition mostly drawn from keyframes (over 30% synthesised) or held then cut: not what the page did on
        # screen, blocking on every scene showing it, the frames named - never an advisory, never silent.
        from app.services.video_studio.scroll_pacing import synthesised_transitions, synthesised_words

        made = [
            item
            for item in synthesised_transitions(take.events, fps=float(getattr(take, "fps", 0) or 0))
            if _shown_stretches(spec, scene_id, take, insert, [item])
        ]
        if made:
            add(
                scene_id,
                "synthesised_transition",
                "dense",
                f"take {take_id[:8]} plays {synthesised_words(made[:6])} on {where}: the transition shown is drawn "
                f"from keyframes, not captured at the take's rate; {again}",
                insert=insert,
                data={"transitions": made[:12]},
            )
        # An animation it captured under 20 fps: said once, on the first scene showing that stretch.
        if take_id not in capture_said and (lows := _shown_lows(spec, scene_id, take, insert)):
            capture_said.add(take_id)
            add(
                scene_id,
                "take_capture_rate",
                "dense",
                f"take {take_id[:8]} captured its page's animation too slowly - {'; '.join(lows)} - so it steps on "
                f"{where}; {again}",
                insert=insert,
            )
        # A recording scene whose kept seconds play its page still loading - a click to its wait's settled time, a
        # progress indicator, an empty canvas - under the narration: blocking, with the overlap and the trim that
        # removes it. The ``take_loading_hold`` advice on the take's holds stays as it was.
        scene = next((s for s in spec.scenes if s.id == scene_id), None) if insert is None else None
        if scene is not None and scene.type == "recording" and (overlap := _loading_overlap(scene, take)) is not None:
            seconds, met, settled = overlap
            idle = getattr(getattr(scene, "idle", None), "mode", "cut")
            fixes = [f"move trim_start to {settled:.2f} s (past the page settling)", "cut the scene in two around it"]
            if idle != "cut":
                fixes.append("set idle mode cut")
            add(
                scene_id,
                "scene_loading_span",
                "dense",
                f"take {take_id[:8]} plays its page still loading for {seconds:.1f} s on {where}: "
                f"{'; '.join(span.why + f' ({span.start:.1f}-{span.end:.1f} s)' for span in met[:4])}; fix: "
                f"{', '.join(fixes[:-1])}, or {fixes[-1]}",
                data={
                    "seconds": round(seconds, 2),
                    "spans": [[span.start, span.end] for span in met[:12]],
                    "trim_start": round(settled, 2),
                },
            )
        # A hold its page was still loading on: said once, on the first scene showing it.
        if take_id not in loading_said and (holds := _shown_holds(spec, scene_id, take, insert)):
            from app.services.video_studio.page_settle import REMEDY, hold_words

            loading_said.add(take_id)
            add(
                scene_id,
                "take_loading_hold",
                "advisory",
                f"take {take_id[:8]} shows its page still loading on {where}: {'; '.join(hold_words(h) for h in holds)}; "
                f"{REMEDY}, or trim the scene past it",
                insert=insert,
            )
    from app.services.video_studio import pace

    pacing = pace.report(document, facts.takes, facts.narration, spec)
    for scene_id, words in pacing.flagged():
        add(scene_id, "pace", "advisory", words)
    # A demo told as a product manager tells it: what its narration reads off the screen, a camera moving for
    # effect, a recording slowed to its narration.
    from app.services.video_studio import demo_grammar

    known = demo_grammar.known_names(spec, facts.template)
    for advice in demo_grammar.advise(spec, document, facts.takes, facts.narration, facts.frame_text, known):
        add(advice.scene_id, advice.kind, "advisory", advice.words)

    from app.services.video_studio import music as music_tracks

    music = spec.music
    # The length the music must cover: the plan, each narration at its spoken length, or the current render of
    # this revision when it came out longer.
    duration = music_tracks.video_seconds(float(document.get("duration") or 0.0), facts.rendered_seconds)
    ends_early = facts.music_seconds is not None and facts.music_seconds < duration - 0.5
    if music is not None and music.enabled and not music.loop and ends_early and scenes.planned:
        remedy = (
            "generate_music(video_id, refit: true) composes it at the video's length"
            if (music.source or ("", ""))[0] == "music"
            else "upload a longer track or start it earlier (music.start)"
        )
        # A track a few seconds short is held to the video with no model call, before any paid refit.
        if (music.source or ("", ""))[0] == "music" and duration - facts.music_seconds < music_tracks.PAD_PROPOSED_SECONDS:
            remedy = f"{music_tracks.pad_offer(facts.music_seconds, duration)}, or {remedy}"
        findings.append(
            scenes.finding(
                scenes.at(facts.music_seconds),
                "music_short",
                "dense",
                f"the music ends here, at {facts.music_seconds:.0f} s of the {duration:.0f} s video, with loop off: "
                f"{music_tracks.silent_tail_words(facts.music_seconds, duration)} - {remedy}, or set music.loop: true",
                content=True,
            )
        )
    # The video outgrew its looping AI track: said at the edit, never first heard after an ultra render.
    if music is not None and music.enabled and scenes.planned and music_tracks.outgrows(music, facts.music_seconds, duration):
        findings.append(
            scenes.finding(
                scenes.at(facts.music_seconds),
                "music_outgrown",
                "advisory",
                music_tracks.outgrown_words(facts.music_seconds, duration, composing=facts.music_composing),
                content=True,
            )
        )
    # A beat_sync that lands nothing on a beat: a warning of the save naming its two fixes, never a note alone.
    if facts.beat_unsynced and scenes.planned:
        findings.append(scenes.finding(scenes.at(0.0), "beat_sync_unsynced", "advisory", facts.beat_unsynced, content=True))
    if facts.music_level is not None and scenes.planned:
        for problem in facts.music_level.problems():
            findings.append(scenes.finding(scenes.at(problem.at), "music_level", "advisory", problem.message, content=True))

    narrated = [scene for scene in spec.scenes if scene.narration]
    if narrated and (not spec.voiceover.enabled or facts.voice_unavailable):
        if not spec.voiceover.enabled:
            why, fix = "the voice-over is off (voiceover.enabled: false)", "turn it on"
        else:
            why = f"no speech model can speak it ({facts.voice_unavailable[:160]})"
            fix = "pick a speech model from video_studio_capabilities (voiceover.provider_id and model)"
        # With captions the words still show; without them they are lost.
        shown_as = "it shows as captions only" if spec.captions.enabled else "it is neither heard nor shown"
        for scene in narrated:
            add(
                scene.id,
                "narration_voice",
                "advisory" if spec.captions.enabled else "dense",
                f"it has narration but no voice: {why}, {shown_as} - {fix}, or move the words on screen",
            )
    if spec.voiceover.enabled and facts.lexicon is not None:
        from app.services.video_studio.lexicon import brand_lexicon, pronunciation_warnings

        for scene in narrated:
            for message in pronunciation_warnings(scene.narration, facts.lexicon, brand_lexicon()):
                add(scene.id, "pronunciation", "advisory", message)
    # Another voice for one scene is heard by everyone: the render waits for it like any finding the audience notices.
    for scene in narrated:
        if (change := facts.voice_changes.get(scene.id)) is not None:
            add(scene.id, "voice_fallback", "dense", change.sentence(scene.id))
    for scene in spec.scenes:
        added = scenes.authored and scene.id not in scenes.positions
        if words := _brand_repeated(scene, spec, facts.template, added=added):
            add(scene.id, "brand_repeated", "dense", words)
    for scene_id, words in _doubled_pages(spec, scenes.planned):
        add(scene_id, "doubled_ui", "dense", words)
    return _worst_first(findings)


def _doubled_pages(spec, planned: list[dict]) -> list[tuple[str, str]]:
    """``(scene id, words)`` of each recording scene whose crossfade into the next scene, playing the same recording,
    blends two framings of its page (``doubled_ui.doubled_under_transition``): a camera still moving, or the two
    framed differently, under the dissolve - the title bar, the tabs and the toolbar drawn twice, offset."""
    from itertools import pairwise

    from app.services.video_studio.doubled_ui import doubled_under_transition

    takes = {scene.id: scene.take_id for scene in spec.scenes if scene.type == "recording" and scene.take_id}
    found = []
    for before, after in pairwise(planned):
        take_id = takes.get(before.get("id"))
        if take_id is None or takes.get(after.get("id")) != take_id:
            continue
        if doubled := doubled_under_transition(before, after):
            found.append(
                (
                    str(before.get("id")),
                    f"its crossfade into scene {after.get('id')} blends two framings of take {take_id[:8]}'s page from "
                    f"{doubled[0]:.2f} s to {doubled[-1]:.2f} s into the scene: its title bar, tabs and toolbar show "
                    "twice, offset; keep its camera still and wide before the transition out (camera.keyframes ending "
                    "at zoom 1 before it, as an automatic camera does), frame the next scene the same, or make the "
                    "transition a cut",
                )
            )
    return found


def _brand_repeated(scene, spec, template, *, added: bool) -> str | None:
    """The ``brand_repeated`` words for *scene*: a brand name its logo spells and its text writes again."""
    from app.services.video_studio.templates import brand_repeats, card_name

    repeats = brand_repeats(
        scene.model_dump(mode="json", exclude_none=True), template, spec.theme.model_dump(mode="json", exclude_none=True)
    )
    if not repeats:
        return None
    first = repeats[0]
    same = [repeat for repeat in repeats if repeat.name == first.name and repeat.mark == first.mark]
    written = ", ".join(dict.fromkeys(f'its {repeat.field} "{_plain(repeat.text)[:50]}"' for repeat in same))
    drawn = "logo" if first.mark == "show_logo" else f"logo overlay ({first.mark})"
    card = card_name(scene.type)
    if not added:
        hide = "hide its logo (show_logo: false)" if first.mark == "show_logo" else f"drop the logo overlay ({first.mark})"
        fix = f"take the name out of the words, or {hide}"
    elif all(repeat.field == "kicker" for repeat in same):
        fix = f"drop the kicker (template_scenes.{card}.kicker: null) or set another one there"
    elif scene.type == "title" and any(repeat.field == "title" and repeat.text == spec.title for repeat in same):
        fix = "retitle the video without the name (the card shows its title), or open on a title card of its own"
    else:
        fix = f"give the video its own {'opening' if card == 'intro' else 'closing'} card without the name"
    return f'its {drawn} already spells "{first.name}" and {written} writes it again - the audience reads it twice: {fix}'


async def content_facts(db, spec, user) -> ContentFacts:
    """What :func:`content_findings` reads, from the database: the shots, the pictures, the music, the voice."""
    from app.models.file_attachment import FileAttachment
    from app.models.video_studio import JOB_READY, VideoClip, VideoTake
    from app.services.video_studio import clips, content_drift, frame_qc, text_backfill, text_drift, voice
    from app.services.video_studio import music as music_tracks
    from app.services.video_studio.preview import overlay_picture_size
    from app.services.video_studio.spec import DEFAULT_QUALITY, QUALITY_PRESETS, output_size

    facts = ContentFacts(
        output=output_size(spec.format.aspect_ratio, QUALITY_PRESETS[DEFAULT_QUALITY].short_side), quality=DEFAULT_QUALITY
    )
    clip_ids: set[str] = set()
    for scene in spec.scenes:
        if scene.type == "clip" and scene.clip_id:
            clip_ids.add(scene.clip_id)
        if scene.type == "statement" and scene.backdrop is not None and scene.backdrop.clip_id:
            clip_ids.add(scene.backdrop.clip_id)
        for overlay in scene.overlays:
            clip_ids.update(source.clip_id for source in overlay.shown_sources if source.clip_id)
        if scene.type == "image" and scene.animate is not None and scene.animate.clip_id:
            clip_ids.add(scene.animate.clip_id)
        if scene.type == "image":
            row = await db.get(FileAttachment, uuid.UUID(scene.file_id))
            if row is not None and (row.content_type or "").startswith("image/") and "svg" not in (row.content_type or ""):
                size = await overlay_picture_size(db, row.id)
                if size is not None:
                    facts.images[scene.file_id] = size
    for clip_id in sorted(clip_ids):
        row = await db.get(VideoClip, uuid.UUID(clip_id))
        if row is None:
            facts.shots[clip_id] = ShotFacts("", "deleted", "the AI shot no longer exists")
            continue
        expired = row.expired_at is not None
        # The take that plays: a ready shot generated again keeps playing its previous one.
        played = clips.take_params(row.params)
        measures = frame_qc.summary(played)["frame_qc"] or {}
        regeneration = clips.regeneration_state(row) if not expired else None
        status = "expired" if expired else row.status
        if regeneration is not None and regeneration["state"] == "running":
            status = _SHOT_REGENERATING
        # The take that plays is judged by the tolerance it passed its own checks under: one a later
        # call stored on the shot - a regeneration that failed beside it, say - never applied to it.
        checked = frame_qc.checked_with(played.get(frame_qc.QC_KEY))
        tolerance = checked.get(frame_qc.MAX_CAMERA_MOTION) if checked is not None else played.get(frame_qc.MAX_CAMERA_MOTION)
        facts.shots[clip_id] = ShotFacts(
            name=(row.name or row.prompt or "")[:60],
            status=status,
            error="its media expired with the retention" if expired else row.error,
            regeneration_failed=(
                regeneration["reason"] or "no reason given"
                if regeneration is not None and regeneration["state"] == "failed"
                else None
            ),
            width=row.width,
            height=row.height,
            camera=content_drift.kept_camera(played),
            motion_ssim=measures.get("motion_ssim"),
            camera_motion=measures.get(frame_qc.CAMERA_MOTION),
            max_camera_motion=tolerance,
            screen=isinstance(played.get(clips.SCREEN), dict),
            playing=str(row.file_id) if row.file_id else None,
            invented=tuple(text_drift.invented_of(played)),
            text_check=bool(played.get("text_check", True)),
            unchecked=(
                text_backfill.unchecked_of(played, row.file_id, row.duration_ms)
                if not expired and row.status == JOB_READY
                else None
            ),
            occlusion=frame_qc.summary(played)["foreground_overlap"],
            logo_integration=(frame_qc.logo_gate(played.get(frame_qc.QC_KEY)) or {}).get("integration"),
            ingredients_path=played.get(clips.INGREDIENTS_PATH),
        )
    # Each AI shot a clip scene holds to the video's references: its kept frames read against each, measured
    # once per take and reference (``reference_drift.measure`` keeps them). A shot not ready has no frames to read.
    held: dict[str, list[dict]] = {}
    if spec.references:
        known = {item.id: item.model_dump(mode="json", exclude_none=True) for item in spec.references}
        for scene in spec.scenes:
            if scene.type == "clip" and scene.clip_id:
                listed = held.setdefault(scene.clip_id, [])
                listed.extend(known[item] for item in scene.consistency if item in known and known[item] not in listed)
    if held:
        import tempfile

        from app.services.video_studio import reference_drift

        with tempfile.TemporaryDirectory(prefix="xtm-reference-drift-") as workdir:
            for clip_id, references in held.items():
                shot = facts.shots.get(clip_id)
                if shot is None or shot.status != JOB_READY:
                    continue
                row = await db.get(VideoClip, uuid.UUID(clip_id))
                kept = (clips.take_params(row.params) if row is not None else {}).get(frame_qc.QC_KEY) or {}
                frames = {name: str(kept[name]) for name, _at in frame_qc.FRAMES if kept.get(name)}
                if not frames:
                    continue
                drifts = await reference_drift.drifts_of(references, frames, workdir)
                if drifts:
                    facts.reference_drifts[clip_id] = drifts
    # Each ready AI shot's kept frames read for texture noise over its flat areas (issue #10036), once per take.
    ready = [clip_id for clip_id, shot in facts.shots.items() if shot.status == JOB_READY]
    if ready:
        import tempfile

        from app.services.video_studio import perceptual

        with tempfile.TemporaryDirectory(prefix="xtm-texture-") as workdir:
            for clip_id in ready:
                row = await db.get(VideoClip, uuid.UUID(clip_id))
                kept = (clips.take_params(row.params) if row is not None else {}).get(frame_qc.QC_KEY) or {}
                frames = {name: str(kept[name]) for name, _at in frame_qc.FRAMES if kept.get(name)}
                start_id = clips.start_frame_file_id(row.params) if row is not None else None
                if frames and (noisy := await perceptual.shot_texture(frames, workdir, start_id)) is not None:
                    facts.textures[clip_id] = noisy
    from app.services.video_studio.projects import take_media

    # The recordings its recording scenes and its screen inserts play (an insert's frame rate is judged too).
    shown_takes = {scene.take_id for scene in spec.scenes if scene.type == "recording"} | set(_insert_takes(spec))
    for take_id in sorted(take_id for take_id in shown_takes if take_id):
        take = await db.get(VideoTake, uuid.UUID(take_id))
        if take is not None and take.status == JOB_READY:
            facts.ready_takes.add(take_id)
            facts.takes[take_id] = take_media(take)
    # A track asked to play once that ends early, and an AI track the video outgrew: its length is read.
    plays_ai_track = spec.music is not None and (spec.music.source or ("", ""))[0] == "music"
    if spec.music is not None and spec.music.enabled and (not spec.music.loop or plays_ai_track):
        measured = await music_tracks.playable_seconds(db, spec.music)
        facts.music_seconds, facts.music_composing = measured if measured else (None, False)
    if spec.voiceover.enabled and any(scene.narration for scene in spec.scenes):
        target = None
        try:
            target = await voice.resolve(db, user.id, spec.voiceover)
        except voice.VoiceUnavailableError as exc:
            facts.voice_unavailable = str(exc)
        from app.services.video_studio import lexicon_store, voice_check

        facts.lexicon = await lexicon_store.for_voiceover(db, spec.voiceover)
        if target is not None:
            try:
                facts.voice_changes = dict(await voice_check.voice_changes(target, spec, facts.lexicon))
                facts.timbres = await voice_check.narration_timbres(target, spec, facts.lexicon)
                facts.fallback_takes = await voice_check.fallback_takes(target, spec, facts.lexicon)
            except Exception as exc:  # check records that cannot be read say nothing: the render checks again
                logger.debug("Narration check records not read: %s", format_exception_message(exc))
    return facts


def fallback_note(takes: Sequence) -> str:
    """The quality check's fact of each narration placed with a fallback take: ``Narrations playing a fallback
    take (facts, nothing refused):`` then a line a scene, with the fix that would let it pass at its speed."""
    if not takes:
        return ""
    return "Narrations playing a fallback take (facts, nothing refused):" + "".join(f"\n- {take.fact()}." for take in takes)


async def _cleared_shots(db, project, spec, effective: dict) -> dict[str, dict]:
    """``clip id -> {render_id, revision, scenes}`` of the AI shots *project*'s passing previews clear as its scenes are
    now (``render_checks.cleared_by``); none when its drawings cannot be read."""
    from app.services.video_studio import lexicon_store, render_checks

    try:
        lexicon = await lexicon_store.for_voiceover(db, spec.voiceover)
        marks = await render_checks.digest_scenes(db, effective, lexicon)
        # Read as the digests are hashed now: an earlier release's drawing of a scene as it is now clears too.
        return render_checks.cleared_by(spec, render_checks.adopted(project.scene_previews, marks), marks.whole, marks.content)
    except Exception as exc:  # what cleared a finding is said when it can be read; the render gate clears it anyway
        logger.debug("Cleared shots of video %s not read: %s", getattr(project, "id", "?"), format_exception_message(exc))
        return {}


async def _figure_fits(db, user, applied, plan) -> dict:
    """``scene id -> figure_fit.FigureFit`` of each figure of the saved video whose still is drawn, as the save judges
    it (``figures.figure_fits``); none when they cannot be read."""
    from app.services.video_studio import figures, projects

    try:
        template_id = applied.template.id if applied.template is not None else None
        previews = await projects.figure_stills_of(db, user, applied.spec, template_id)
        return await figures.figure_fits(previews, projects.figure_timings_of(plan, applied.spec))
    except Exception as exc:  # the figures are judged when they can be read; the render fits their drift anyway
        logger.debug("Figure fits not read: %s", format_exception_message(exc))
        return {}


class ContentReport(NamedTuple):
    """What :func:`check_content` read of the saved video: its findings, what the video declares unchecked, and what
    is said of it as a whole (its pace)."""

    findings: list[QualityFinding]
    unchecked: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


async def backfill_text_checks(user_id: object, facts: ContentFacts, *, read: Any = None) -> list[str]:
    """Each shot of *facts* whose take kept no text check (``ShotFacts.unchecked``) read for invented lettering
    (``text_backfill.read_shots``, or *read* in tests): what it found set as the shot's ``invented``, and one facts line
    per shot - read with nothing found, or why it was not read. Never raises."""
    from dataclasses import replace

    from app.services.video_studio import text_backfill

    takes = {clip_id: shot.unchecked for clip_id, shot in facts.shots.items() if shot.unchecked is not None}
    if not takes:
        return []
    try:
        found = await (read or text_backfill.read_shots)(user_id, takes)
    except Exception as exc:  # each shot is said not checked below
        logger.info("Text backfill skipped: %s", format_exception_message(exc) or type(exc).__name__)
        found = {}
    lines: list[str] = []
    for clip_id in sorted(takes):
        result = found.get(clip_id)
        if result is not None and result.read and result.invented:
            facts.shots[clip_id] = replace(facts.shots[clip_id], invented=tuple(result.invented))
        lines.append(text_backfill.fact_line(clip_id, result))
    return lines


async def check_content(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    expected_revision: int | None = None,
    music: bool = True,
    read_frames: bool = True,
    authored: dict | None = None,
) -> ContentReport:
    """The content findings of the saved video as *user_id* sees it, read fresh: a shot finishing changes them.

    *music*: how loud the music plays is measured too (``music_level``), once the session is closed - the
    bed is prepared as the render prepares it. *read_frames*: the frames a demo's narration may read a label off are
    read when not read before (``demo_grammar.frame_texts``); else only those read before count. *authored*: the
    authored spec of a past revision to judge instead of the saved one (a ready render re-judged, ``render_rejudge``).
    Never raises: a video that cannot be read or planned has none.
    """
    from app.core.database import async_session
    from app.models.user import User
    from app.models.video_studio import VideoProject
    from app.services.video_studio import music_level, projects
    from app.services.video_studio.spec import parse_video_spec

    try:
        async with async_session() as db:
            project = await db.get(VideoProject, project_id)
            user = await db.get(User, user_id)
            if project is None or user is None:
                return ContentReport([])
            if expected_revision is not None and project.revision != expected_revision:
                return ContentReport([])
            applied = await (
                projects.effective_spec(db, project, user)
                if authored is None
                else projects.effective_spec(db, project, user, authored=authored)
            )
            spec = parse_video_spec(applied.spec)
            # Each narration at its spoken length where it was spoken, the cuts on the beats: what the
            # render places, so a clip shorter than its narration and a track shorter than the video are told here.
            placed = await projects.measured_plan(db, applied.spec, user)
            plan, spoken = placed.plan, placed.spoken
            facts = await content_facts(db, spec, user)
            facts.spoken = spoken
            facts.narration = dict(plan.narration_spans)
            facts.rendered_seconds = await projects.rendered_seconds(db, project)
            # Read back as the save measured them (``render_cache``): a shot made again since is measured now.
            from app.services.video_studio import insert_bezels, insert_legibility, render_checks

            facts.bezels = await insert_bezels.measure(db, spec, voices=spoken)
            facts.cleared = await _cleared_shots(db, project, spec, applied.spec)
            # The advice offers only a scale the source carries at the quality the video is rendered at.
            facts.legibility = await insert_legibility.measure(
                db, spec, quality=await render_checks.target_quality(db, project)
            )
            facts.soft = await render_checks.soft_qualities(db, spec)
            facts.figures = await _figure_fits(db, user, applied, plan)
            unchecked = tuple(
                f"scene `{sid}` {words}" for sid, words in insert_legibility.unchecked(spec, facts.legibility)
            )
            facts.template = applied.template
            playing = await music_level.planned_music(db, spec, plan) if music else None
            authored = dict(authored if authored is not None else project.spec or {})
        if playing is not None:
            facts.music_level = await music_level.measure_planned(playing)
        from app.services.video_studio import demo_grammar, pace

        facts.frame_text = await demo_grammar.frame_texts(
            user_id,
            spec,
            plan.document,
            facts.narration,
            demo_grammar.known_names(spec, facts.template),
            read=read_frames,
        )
        # A take that kept no text check is read here for invented lettering, never passed in silence.
        backfilled = await backfill_text_checks(user_id, facts)
        said = pace.report(plan.document, facts.takes, facts.narration, spec).summary()
        # A beat_sync video whose music lands nothing on a beat: said at the save, not first by the render - as
        # a finding when it can land none at all, as a note while its AI track is being made. A narration
        # placed with a fallback take, with its fix.
        from app.services.video_studio import beats

        unsynced = isinstance(placed.beat_note, beats.SyncsNothing)
        facts.beat_unsynced = str(placed.beat_note) if unsynced else None
        notes = tuple(
            note
            for note in (
                said,
                None if unsynced else placed.beat_note,
                fallback_note(facts.fallback_takes),
                *backfilled,
            )
            if note
        )
        return ContentReport(content_findings(spec, plan.document, facts, authored), unchecked, notes)
    except Exception as exc:
        logger.warning("Video content check of %s skipped: %s", project_id, format_exception_message(exc))
        return ContentReport([])


# ── Running the check ────────────────────────────────────────────────────


def _cache_key(project_id: uuid.UUID, user_id: uuid.UUID, revision: int) -> str:
    # Per user: the plan resolves the template and the media with the caller's visibility.
    return f"video:quality:v{QUALITY_REPORT_VERSION}:{project_id}:{user_id}:{revision}"


async def _cached_report(key: str) -> QualityReport | None:
    try:
        redis = await redis_client()
        raw = await redis.get(key)
    except Exception as exc:  # the cache is a courtesy - fail open to a render
        logger.debug("Video quality cache read skipped for %s: %s", key, format_exception_message(exc))
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        return QualityReport.from_dict(payload) if isinstance(payload, dict) else None
    except (TypeError, ValueError):
        return None


async def _remember_report(key: str, report: QualityReport) -> None:
    try:
        redis = await redis_client()
        await redis.set(key, json.dumps(report.as_dict()), ex=QUALITY_CHECK_CACHE_TTL_SECS)
    except Exception as exc:
        logger.debug("Video quality cache write skipped for %s: %s", key, format_exception_message(exc))


async def measure_document(document: dict) -> dict:
    """Play *document* in the compositor page and return its ``audit()``: no media, nothing leaves the page."""
    from playwright.async_api import async_playwright

    from app.services.builtin_tools.html_render import font_face_css
    from app.services.headless_render import RENDER_CHROMIUM_ARGS, RENDER_SERVICE_WORKERS
    from app.services.video_studio.compositor_page import ORIGIN, page_html

    html = page_html(font_face_css()).encode("utf-8")
    index = f"{ORIGIN}/index.html"

    async def handle(route) -> None:
        if route.request.url == index:
            await route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")
        else:
            await route.abort("blockedbyclient")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=list(RENDER_CHROMIUM_ARGS))
        try:
            context = await browser.new_context(
                viewport={"width": int(document["width"]), "height": int(document["height"])},
                service_workers=RENDER_SERVICE_WORKERS,
            )
            await context.route("**/*", handle)
            page = await context.new_page()
            await page.goto(index, wait_until="load")
            await page.evaluate("doc => window.__xtm.load(doc)", {**document, "preview_media": {}})
            return await page.evaluate("() => window.__xtm.audit()")
        finally:
            await browser.close()


async def check_video_quality(
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    expected_revision: int | None = None,
    timeout: float = QUALITY_CHECK_TIMEOUT_SECS,
    measure: bool = True,
    measure_layout: bool | None = None,
) -> QualityReport:
    """Plan the saved video as *user_id* sees it, play it in the compositor and grade what it measures.

    Never raises for a planning or render problem: the report says why it was
    skipped. The caller has already authorized *user_id* on the video. With
    *expected_revision*, a video saved again in between is skipped rather than
    reported against the wrong scenes, and a successful layout report is kept
    for ``QUALITY_CHECK_CACHE_TTL_SECS``. The pictures' and the sound's
    findings (:func:`check_content`) are read fresh on every call and kept
    when the layout could not be measured. *measure* ``False`` reads the
    layout kept for *expected_revision* only, never playing the video (the
    render gate): none kept, the layout is skipped; nor is the music's
    level measured, an advisory the gate never refuses on. *measure_layout*
    (the pre-render lint) measures the layout when none is kept, the
    music's level still unmeasured: a render never skips text fit and overlap.
    """
    if not quality_check_enabled():
        return QualityReport(scene_count=0, skipped="disabled")
    try:
        content = await asyncio.wait_for(
            check_content(project_id, user_id, expected_revision=expected_revision, music=measure, read_frames=measure),
            timeout=timeout,
        )
    except TimeoutError:
        logger.info("Video content check of %s did not finish within %.0fs", project_id, timeout)
        content = ContentReport([])
    layout = await _layout_report(
        project_id,
        user_id,
        expected_revision=expected_revision,
        timeout=timeout,
        measure=measure if measure_layout is None else measure_layout,
    )
    return QualityReport(
        scene_count=layout.scene_count,
        findings=tuple(_worst_first([*layout.findings, *content.findings])),
        skipped=layout.skipped,
        revision=layout.revision,
        unchecked=tuple(content.unchecked),
        notes=(*layout.notes, *content.notes),
    )


async def _layout_report(
    project_id: uuid.UUID, user_id: uuid.UUID, *, expected_revision: int | None, timeout: float, measure: bool = True
) -> QualityReport:
    """The layout half of the check: the saved video played in the compositor, graded (cached per revision)."""
    key = _cache_key(project_id, user_id, expected_revision) if expected_revision is not None else None
    if key is not None and (cached := await _cached_report(key)) is not None:
        return cached
    if not measure:
        return QualityReport(scene_count=0, skipped="not measured since the video was last saved", revision=expected_revision)
    from app.core.database import async_session
    from app.models.user import User
    from app.models.video_studio import VideoProject
    from app.services.video_studio.preview import build_preview
    from app.services.video_studio.spec import VideoSpecError

    try:
        async with async_session() as db:
            project = await db.get(VideoProject, project_id)
            user = await db.get(User, user_id)
            if project is None or user is None:
                return QualityReport(scene_count=0, skipped="the video is no longer there")
            if expected_revision is not None and project.revision != expected_revision:
                return QualityReport(
                    scene_count=0,
                    skipped=f"the video changed while it was checked (revision {expected_revision} -> {project.revision})",
                    revision=project.revision,
                )
            try:
                preview = await build_preview(db, project, user)
            except VideoSpecError as exc:
                return QualityReport(scene_count=0, skipped=f"the video does not plan: {exc}")
            authored = dict(project.spec or {})
            revision = project.revision
            estimated = estimated_narrations(preview)
    except Exception as exc:
        logger.warning("Video quality check of %s could not plan it: %s", project_id, format_exception_message(exc))
        return QualityReport(scene_count=0, skipped="the video could not be planned for the check")
    document = preview["document"]
    try:
        audit = await asyncio.wait_for(measure_document(document), timeout=timeout)
    except TimeoutError:
        logger.info("Video quality check of %s did not finish within %.0fs", project_id, timeout)
        return QualityReport(scene_count=0, skipped=f"the check did not finish within {timeout:.0f} seconds")
    except Exception as exc:
        # No Chromium on this host, a browser crash: the check is a courtesy, the save is not.
        logger.warning("Video quality check unavailable for %s: %s", project_id, format_exception_message(exc))
        return QualityReport(scene_count=0, skipped="the headless browser is not available on this deployment")
    minimums = card_minimums(audit, document, authored)
    report = QualityReport(
        scene_count=len(document.get("scenes") or []),
        findings=tuple(findings_from_audit(audit, document, authored, estimated=estimated)),
        revision=revision,
        notes=(minimums,) if minimums else (),
    )
    if key is not None:
        await _remember_report(key, report)
    return report


# ── The words the agent reads ────────────────────────────────────────────


def _where(finding: QualityFinding) -> str:
    kind = finding.scene_type or "scene"
    if finding.scene_index is None:
        where = f"the template's {kind} scene"
    else:
        where = f"scene {finding.scene_index} [{finding.scene_id}] {kind}"
    return f'{where} "{finding.label}"' if finding.label else where


def format_quality_report(report: QualityReport) -> str:
    """The ``Quality check`` section of a tool result (empty when the check is switched off).

    One line per finding: the scene's index in the spec as written (the one
    ``edit_video`` operations use) and its id, then what is wrong and what to
    change. The list ends with the instruction, because a finding is only
    useful acted on in the same turn. What the video declares unchecked
    (:attr:`QualityReport.unchecked`) follows under a heading of its own: no
    finding, nothing to fix.
    """
    if report.skipped == "disabled":
        return ""
    text = _findings_text(report)
    if report.unchecked:
        text += "\nNot judged, as the video declares (no finding):" + "".join(f"\n- {item}" for item in report.unchecked)
    if report.notes:
        text += "".join(f"\n{note}" for note in report.notes)
    return text


def _findings_text(report: QualityReport) -> str:
    if report.skipped and not report.findings:
        return f"Quality check skipped: {report.skipped}."
    if report.skipped:
        lines = [
            f"Quality check: the layout was not measured ({report.skipped}); {len(report.findings)} issue(s) in the "
            "pictures and the sound. Fix them and save again:"
        ]
        lines.extend(f"- {_where(finding)} ({finding.severity}): {finding.message}" for finding in report.findings[:MAX_REPORTED_FINDINGS])
        return "\n".join(lines)
    if not report.findings:
        scenes = "scene" if report.scene_count == 1 else "scenes"
        return (
            f"Quality check: in the {report.scene_count} {scenes}, nothing covers anything, every text fits the "
            "frame and stays on screen long enough to read."
        )
    lines = [f"Quality check: {len(report.findings)} issue(s) the audience would see. Fix them and save again:"]
    for finding in report.findings[:MAX_REPORTED_FINDINGS]:
        lines.append(f"- {_where(finding)} ({finding.severity}): {finding.message}")
    hidden = len(report.findings) - MAX_REPORTED_FINDINGS
    if hidden > 0:
        lines.append(f"- ...and {hidden} more; fix the above, save, and the next check lists the rest.")
    return "\n".join(lines)


__all__ = [
    "MAX_REPORTED_FINDINGS",
    "QUALITY_CHECK_TIMEOUT_SECS",
    "ContentFacts",
    "ContentReport",
    "QualityFinding",
    "QualityReport",
    "ShotFacts",
    "check_content",
    "check_video_quality",
    "content_facts",
    "content_findings",
    "document_findings",
    "estimated_narrations",
    "findings_from_audit",
    "format_quality_report",
    "measure_document",
    "quality_check_enabled",
]
